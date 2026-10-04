"""Binance public daily klines for the trend paper trader.

The only Binance call in the repository, scoped to the research-only trend paper module. It is a
documented exception to the radar's Kraken-only public allowlist: ``config.HTTP_PUBLIC_ALLOWLIST``
is not widened and ``GuardedSession`` is not used, because that guard serves the radar runtime.

Boundary, checked before any byte leaves the process:

* method GET, URL exactly ``https://api.binance.com/api/v3/klines`` (no userinfo, port, query or
  fragment in the URL; parameters go in ``params``);
* ``symbol`` from the fixed set :data:`ALLOWED_SYMBOLS`, ``interval`` exactly ``1d``, ``limit`` at
  most 1000 and ``startTime`` a non-negative UTC-midnight integer; no other parameter;
* no authentication: the session never reads the environment, ``.netrc`` or proxies
  (``trust_env = False``), sends no auth header, and this module reads no credential or
  environment variable;
* redirects are never followed (``allow_redirects=False``): any 3xx or redirect history raises
  :attr:`KlinesErrorCode.REDIRECT` after that one request;
* bounded: per-request timeout, at most ``max_retries`` retries on a network error, 429 or 5xx
  with exponential backoff, a response-size cap, a page cap, an overall deadline per fetch and a
  request budget per client (one client serves one catch-up);
* polite: a sleep never runs past the fetch deadline. A 429
  ``Retry-After`` (seconds) is honoured only when it fits the remaining deadline; otherwise the
  fetch fails at once with ``RATE_LIMITED``. A 418 (Binance's IP ban) is never retried: it raises
  ``BANNED`` after that one request, and the client then refuses every later request without
  sending it, as it does after ``RATE_LIMITED``, so a catch-up stops at the first of them.

Rows are validated: malformed, non-finite, non-positive, duplicate or non-monotonic rows raise a
typed :class:`KlinesError`. A candle whose day has not ended (by the caller's clock) is the
still-open candle: only its open is returned, never its high, low or close. A candle that opens
after the caller's clock means that clock is behind the exchange: ``CLOCK_SKEW``, nothing returned.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from datetime import date
from enum import StrEnum
from urllib.parse import urlsplit

import requests

from ..domain.trend_engine import MS_PER_DAY, Bar, day_open_ms, utc_day
from ..domain.trend_paper import DailySeries

KLINES_URL = "https://api.binance.com/api/v3/klines"
ALLOWED_HOST = "api.binance.com"
ALLOWED_PATH = "/api/v3/klines"
ALLOWED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "BTCEUR", "ETHEUR", "EURUSDT"})
INTERVAL = "1d"
PAGE_LIMIT = 1000
ALLOWED_PARAMS = frozenset({"symbol", "interval", "startTime", "limit"})
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_DEADLINE_SECONDS = 240.0
DEFAULT_MAX_REQUESTS = 64  # HTTP requests per client, retries included (a catch-up needs about 10)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_ROW_COLUMNS = 7


class KlinesErrorCode(StrEnum):
    REFUSED = "REFUSED"
    REDIRECT = "REDIRECT"
    HTTP_ERROR = "HTTP_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    BANNED = "BANNED"
    BUDGET = "BUDGET"
    CLOCK_SKEW = "CLOCK_SKEW"
    NETWORK = "NETWORK"
    DEADLINE = "DEADLINE"
    TOO_LARGE = "TOO_LARGE"
    MALFORMED = "MALFORMED"
    NON_FINITE = "NON_FINITE"
    DUPLICATE = "DUPLICATE"
    NON_MONOTONIC = "NON_MONOTONIC"


class KlinesError(Exception):
    def __init__(self, code: KlinesErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value + (f": {detail}" if detail else ""))


def _refuse(detail: str) -> KlinesError:
    return KlinesError(KlinesErrorCode.REFUSED, detail)


def assert_allowed_request(method: str, url: str, params: Mapping[str, object]) -> None:
    """Raise ``REFUSED`` unless this is exactly an allowed public klines GET."""
    if method != "GET":
        raise _refuse(f"method {method!r}")
    if url != KLINES_URL:
        raise _refuse(f"url {url!r}")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != ALLOWED_HOST
        or parts.hostname != ALLOWED_HOST
        or parts.port is not None
        or parts.username is not None
        or parts.path != ALLOWED_PATH
        or parts.query
        or parts.fragment
    ):
        raise _refuse(f"url {url!r}")
    if set(params) - ALLOWED_PARAMS or not {"symbol", "interval"} <= set(params):
        raise _refuse(f"parameters {sorted(params)!r}")
    symbol = params["symbol"]
    if not isinstance(symbol, str) or symbol not in ALLOWED_SYMBOLS:
        raise _refuse(f"symbol {symbol!r}")
    if params["interval"] != INTERVAL:
        raise _refuse(f"interval {params['interval']!r}")
    limit = params.get("limit", PAGE_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_LIMIT:
        raise _refuse(f"limit {limit!r}")
    start = params.get("startTime", 0)
    if isinstance(start, bool) or not isinstance(start, int) or start < 0 or start % MS_PER_DAY:
        raise _refuse(f"startTime {start!r}")


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise KlinesError(KlinesErrorCode.MALFORMED, f"{what} is not a number: {value!r}")
    try:
        number = float(value)
    except ValueError as error:
        raise KlinesError(KlinesErrorCode.MALFORMED, f"{what} is not a number: {value!r}") from error
    if not math.isfinite(number):
        raise KlinesError(KlinesErrorCode.NON_FINITE, f"{what} is not finite: {value!r}")
    if number <= 0:
        raise KlinesError(KlinesErrorCode.MALFORMED, f"{what} must be positive: {value!r}")
    return number


def _retry_after(response: requests.Response) -> float | None:
    """A ``Retry-After`` in whole seconds (``"30"``); ``None`` when absent or not that form."""
    value = response.headers.get("Retry-After") if response.headers is not None else None
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,6}", value.strip()) is None:
        return None
    return float(value.strip())


def _reject_constant(token: str) -> object:
    raise KlinesError(KlinesErrorCode.NON_FINITE, f"JSON constant {token}")


def parse_rows(
    symbol: str, payload: object, now_ms: int, after_ms: int | None = None
) -> tuple[list[Bar], float | None, int | None]:
    """Validate one page. Returns the closed bars, the open of the last row if it is still open,
    and the open time of the last row (``after_ms`` for an empty page).

    ``after_ms`` is the open time of the last bar already accepted (duplicates and going back in
    time across pages are refused too).
    """
    if not isinstance(payload, list):
        raise KlinesError(KlinesErrorCode.MALFORMED, f"{symbol}: payload is not a list")
    closed: list[Bar] = []
    live_open: float | None = None
    last = after_ms
    for n, row in enumerate(payload):
        if live_open is not None:
            t = row[0] if isinstance(row, list) and row else None
            if isinstance(t, int) and not isinstance(t, bool) and t > now_ms:
                # Still open by the local clock, yet the exchange has started the next day.
                raise KlinesError(
                    KlinesErrorCode.CLOCK_SKEW, f"{symbol}: a candle opens after the local clock (is it behind?)"
                )
            raise KlinesError(KlinesErrorCode.MALFORMED, f"{symbol}: a row follows the still-open candle")
        if not isinstance(row, list) or len(row) < MIN_ROW_COLUMNS:
            raise KlinesError(KlinesErrorCode.MALFORMED, f"{symbol}: row {n} is not a kline")
        t, close_time = row[0], row[6]
        if isinstance(t, bool) or not isinstance(t, int) or t < 0 or t % MS_PER_DAY:
            raise KlinesError(KlinesErrorCode.MALFORMED, f"{symbol}: row {n} open time {t!r} is not a UTC midnight")
        # The close time is the day's last millisecond, earlier only for a day cut short by an
        # exchange outage (BTCUSDT 2018-02-08); the row is still that day's candle.
        if isinstance(close_time, bool) or not isinstance(close_time, int) or not t < close_time < t + MS_PER_DAY:
            raise KlinesError(KlinesErrorCode.MALFORMED, f"{symbol}: row {n} is not a 1d candle")
        if last is not None and t == last:
            raise KlinesError(KlinesErrorCode.DUPLICATE, f"{symbol}: {utc_day(t)} appears twice")
        if last is not None and t < last:
            raise KlinesError(KlinesErrorCode.NON_MONOTONIC, f"{symbol}: {utc_day(t)} after {utc_day(last)}")
        if t > now_ms:
            raise KlinesError(
                KlinesErrorCode.CLOCK_SKEW, f"{symbol}: candle {utc_day(t)} opens after the local clock (is it behind?)"
            )
        o = _number(row[1], f"{symbol} {utc_day(t)} open")
        if t + MS_PER_DAY > now_ms:
            live_open = o  # still open: its high, low and close are not final and are never read
        else:
            h = _number(row[2], f"{symbol} {utc_day(t)} high")
            low = _number(row[3], f"{symbol} {utc_day(t)} low")
            c = _number(row[4], f"{symbol} {utc_day(t)} close")
            closed.append(Bar(t, o, h, low, c))
        last = t
    return closed, live_open, last


class BinancePublicKlines:
    """Daily klines of the allowed symbols over a dedicated unauthenticated session."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        sleep: Callable[[float], object] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False  # no proxy, .netrc or CA settings from the environment
        self._session.auth = None
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._deadline_seconds = deadline_seconds
        self._max_requests = max_requests
        self._requests = 0
        self._stopped: KlinesError | None = None  # BANNED or RATE_LIMITED: no later request is sent
        self._sleep = sleep
        self._monotonic = monotonic

    @property
    def requests_sent(self) -> int:
        return self._requests

    def close(self) -> None:
        self._session.close()

    def _get(self, params: Mapping[str, str | int], deadline: float) -> object:
        assert_allowed_request("GET", KLINES_URL, params)
        symbol = params["symbol"]
        if self._stopped is not None:
            raise KlinesError(self._stopped.code, f"{symbol}: not sent ({self._stopped.detail})")
        last = ""
        limited = False
        for attempt in range(self._max_retries + 1):
            if self._monotonic() >= deadline:
                raise KlinesError(KlinesErrorCode.DEADLINE, f"{symbol}: fetch deadline passed")
            if self._requests >= self._max_requests:
                raise KlinesError(KlinesErrorCode.BUDGET, f"{symbol}: request budget of {self._max_requests} spent")
            self._requests += 1
            retry_after: float | None = None
            limited = False
            try:
                response = self._session.request(
                    "GET",
                    KLINES_URL,
                    params=dict(params),
                    headers={"Accept": "application/json"},
                    timeout=self._timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as error:
                last = f"{type(error).__name__}: {error}"
            else:
                try:
                    status = response.status_code
                    if 300 <= status < 400 or response.history:
                        raise KlinesError(KlinesErrorCode.REDIRECT, f"HTTP {status}; redirects are never followed")
                    if status == 418:
                        self._stopped = KlinesError(KlinesErrorCode.BANNED, "HTTP 418: Binance banned this IP for now")
                        raise self._stopped
                    if status == 429:
                        last, limited, retry_after = "HTTP 429", True, _retry_after(response)
                    elif status >= 500:
                        last = f"HTTP {status}"
                    elif status != 200:
                        raise KlinesError(KlinesErrorCode.HTTP_ERROR, f"HTTP {status}")
                    else:
                        try:
                            return self._json(response)
                        except KlinesError as error:
                            if error.code is not KlinesErrorCode.NETWORK:
                                raise
                            last = error.detail  # the body was cut off in transit: retried
                finally:
                    response.close()
            if attempt == self._max_retries:
                break
            wait = self._backoff * (2**attempt)
            if retry_after is not None:
                wait = max(wait, retry_after)
            if wait >= deadline - self._monotonic():  # never sleep past the fetch deadline
                if limited:
                    self._stopped = KlinesError(
                        KlinesErrorCode.RATE_LIMITED, f"HTTP 429: a {wait:g} s wait does not fit the fetch deadline"
                    )
                    raise KlinesError(self._stopped.code, f"{symbol}: {self._stopped.detail}")
                raise KlinesError(KlinesErrorCode.DEADLINE, f"{symbol}: a {wait:g} s retry wait would pass the fetch deadline")
            self._sleep(wait)
        if limited:
            self._stopped = KlinesError(KlinesErrorCode.RATE_LIMITED, f"HTTP 429 after {self._max_retries + 1} attempts")
            raise KlinesError(self._stopped.code, f"{symbol}: {self._stopped.detail}")
        raise KlinesError(KlinesErrorCode.NETWORK, f"{symbol}: {self._max_retries + 1} attempts failed ({last})")

    @staticmethod
    def _json(response: requests.Response) -> object:
        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise KlinesError(KlinesErrorCode.TOO_LARGE, f"response over {MAX_RESPONSE_BYTES} bytes")
        except requests.RequestException as error:
            raise KlinesError(KlinesErrorCode.NETWORK, f"{type(error).__name__}: {error}") from error
        try:
            return json.loads(bytes(body), parse_constant=_reject_constant)
        except ValueError as error:
            raise KlinesError(KlinesErrorCode.MALFORMED, f"invalid JSON: {error}") from error

    def fetch_daily(self, symbol: str, since: date, now_ms: int) -> DailySeries:
        """Every daily candle of ``symbol`` from ``since`` (UTC) up to ``now_ms``."""
        if symbol not in ALLOWED_SYMBOLS:
            raise _refuse(f"symbol {symbol!r}")
        start_ms = max(0, day_open_ms(since))
        deadline = self._monotonic() + self._deadline_seconds
        max_pages = max(0, (now_ms - start_ms) // MS_PER_DAY) // PAGE_LIMIT + 2
        closed: list[Bar] = []
        live_open: float | None = None
        last: int | None = None
        for _ in range(max_pages):
            payload = self._get(
                {"symbol": symbol, "interval": INTERVAL, "startTime": start_ms, "limit": PAGE_LIMIT}, deadline
            )
            page, live_open, page_last = parse_rows(symbol, payload, now_ms, last)
            closed.extend(page)
            if page_last is None or page_last == last:
                break  # empty page
            last = page_last
            if live_open is not None or len(page) < PAGE_LIMIT:
                break
            start_ms = last + MS_PER_DAY
        else:
            raise KlinesError(KlinesErrorCode.MALFORMED, f"{symbol}: more pages than the requested range allows")
        live_day = utc_day(last) if live_open is not None and last is not None else None
        return DailySeries(symbol, tuple(closed), live_day, live_open)
