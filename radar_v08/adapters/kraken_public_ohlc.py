"""Kraken public daily OHLC for the Kraken EUR trend paper books.

Research only: a second, read-only EUR price source. It reuses the radar's existing Kraken public
path: every request goes through :class:`radar_v08.http_client.GuardedSession` to
``https://api.kraken.com/0/public/OHLC``, already in ``config.HTTP_PUBLIC_ALLOWLIST`` (not widened).
It never imports or calls the private Kraken adapter or any authenticated endpoint, and reads no
credential, environment file or environment variable.

Boundary, checked before any request (:func:`assert_allowed_params`):

* parameters ``pair``, ``interval`` and ``since`` only; ``pair`` from :data:`ALLOWED_PAIRS`,
  ``interval`` exactly ``1440`` (daily) and ``since`` a non-negative UTC-midnight epoch second;
* the inner ``requests`` session ignores the environment (``trust_env = False``: no proxy,
  ``.netrc`` or CA settings) and sends no auth;
* bounded: the session's timeout and retries (network error, 429 or 5xx) with backoff, a response
  size cap and a request budget per client (one client serves one catch-up). A Kraken rate-limit
  error stops the client: it sends nothing more.

Rows are validated here, not by ``kraken_spot._parse_ohlc_row`` (which accepts NaN): a Kraken error
envelope, invalid JSON, a missing or ambiguous result key, malformed, non-numeric, non-finite,
non-positive, duplicate, non-ascending or not-UTC-midnight rows raise one typed
:class:`KrakenOhlcError` and return nothing. A candle whose UTC day has not ended by the caller's
clock is the still-open candle: only its open is returned, never its high, low or close. A candle
that opens after the caller's clock means that clock is behind the exchange: ``CLOCK_SKEW``.

Kraken returns at most the latest 720 candles. When the earliest returned candle is after the
requested ``since``, :attr:`KrakenDailySeries.history_start` says so: no day before it is known,
so a caller must never treat such a day as a confirmed missing candle.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from .. import http_client
from ..domain.trend_engine import MS_PER_DAY, Bar, day_open_ms, utc_day
from ..domain.trend_paper import DailySeries
from ..http_client import ApiError, GuardedSession, RedirectNotFollowed
from ..security import RedirectRefused

OHLC_URL = "https://api.kraken.com/0/public/OHLC"
# Requested pair -> the result keys Kraken may use for it (its canonical key, or the pair itself).
RESULT_KEYS: Mapping[str, frozenset[str]] = {
    "XBTEUR": frozenset({"XXBTZEUR", "XBTEUR"}),
    "ETHEUR": frozenset({"XETHZEUR", "ETHEUR"}),
}
ALLOWED_PAIRS = frozenset(RESULT_KEYS)
INTERVAL = 1440  # minutes: one UTC day
ALLOWED_PARAMS = frozenset({"pair", "interval", "since"})
SECONDS_PER_DAY = MS_PER_DAY // 1000
KRAKEN_MAX_ROWS = 720  # Kraken returns at most the latest 720 candles
ROW_COLUMNS = 8  # time, open, high, low, close, vwap, volume, count
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_REQUESTS = 8  # get() calls per client; each makes at most max_retries + 1 attempts
MAX_RESPONSE_BYTES = 1024 * 1024
RATE_LIMIT_ERRORS = ("EAPI:Rate limit exceeded", "EGeneral:Too many requests", "EService:Throttled")


class KrakenOhlcErrorCode(StrEnum):
    REFUSED = "REFUSED"
    REDIRECT = "REDIRECT"
    HTTP_ERROR = "HTTP_ERROR"
    NETWORK = "NETWORK"
    RATE_LIMITED = "RATE_LIMITED"
    API_ERROR = "API_ERROR"
    BUDGET = "BUDGET"
    TOO_LARGE = "TOO_LARGE"
    INVALID_JSON = "INVALID_JSON"
    MALFORMED = "MALFORMED"
    RESULT_KEY = "RESULT_KEY"
    EMPTY = "EMPTY"
    NON_FINITE = "NON_FINITE"
    NON_POSITIVE = "NON_POSITIVE"
    NOT_MIDNIGHT = "NOT_MIDNIGHT"
    DUPLICATE = "DUPLICATE"
    NON_ASCENDING = "NON_ASCENDING"
    CLOCK_SKEW = "CLOCK_SKEW"


class KrakenOhlcError(Exception):
    def __init__(self, code: KrakenOhlcErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value + (f": {detail}" if detail else ""))


@dataclass(frozen=True)
class KrakenDailySeries(DailySeries):
    """A :class:`DailySeries` plus where Kraken's history starts when it does not reach ``since``.

    ``history_start`` is ``None`` when the response reaches back to the requested ``since``;
    otherwise it is the earliest returned UTC day, and no day before it is known (Kraken's 720-candle
    cap), so none of those days may be treated as a confirmed missing candle.
    """

    history_start: date | None = None


def _refuse(detail: str) -> KrakenOhlcError:
    return KrakenOhlcError(KrakenOhlcErrorCode.REFUSED, detail)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def assert_allowed_params(params: Mapping[str, object]) -> None:
    """Raise ``REFUSED`` unless these are exactly allowed daily OHLC parameters."""
    if not isinstance(params, Mapping):
        raise _refuse("parameters are not a mapping")
    keys = set(params)
    if keys - ALLOWED_PARAMS or not {"pair", "interval"} <= keys:
        raise _refuse(f"parameters {sorted(map(str, keys))!r}")
    pair = params["pair"]
    if not isinstance(pair, str) or pair not in ALLOWED_PAIRS:
        raise _refuse(f"pair {pair!r}")
    interval = params["interval"]
    if not _is_int(interval) or interval != INTERVAL:
        raise _refuse(f"interval {interval!r}")
    if "since" in params:
        since = params["since"]
        if isinstance(since, bool) or not isinstance(since, int) or since < 0 or since % SECONDS_PER_DAY:
            raise _refuse(f"since {since!r}")


def _number(value: object, what: str, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{what} is not a number: {value!r}")
    try:
        number = float(value)
    except ValueError as error:
        raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{what} is not a number: {value!r}") from error
    if not math.isfinite(number):
        raise KrakenOhlcError(KrakenOhlcErrorCode.NON_FINITE, f"{what} is not finite: {value!r}")
    if number < 0 or (positive and number == 0):
        raise KrakenOhlcError(KrakenOhlcErrorCode.NON_POSITIVE, f"{what} must be positive: {value!r}")
    return number


def _reject_constant(token: str) -> object:
    raise KrakenOhlcError(KrakenOhlcErrorCode.NON_FINITE, f"JSON constant {token}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise KrakenOhlcError(KrakenOhlcErrorCode.RESULT_KEY, f"JSON key {key!r} appears twice")
        out[key] = value
    return out


def parse_body(body: bytes) -> object:
    """Strict JSON: no NaN/Infinity constants, no repeated object key."""
    try:
        return json.loads(body, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as error:
        raise KrakenOhlcError(KrakenOhlcErrorCode.INVALID_JSON, f"invalid JSON: {error}") from error


def result_rows(pair: str, payload: object) -> list[object]:
    """The candle rows of one OHLC envelope; a Kraken error or an unexpected shape raises."""
    if not isinstance(payload, dict) or set(payload) - {"error", "result"} or "error" not in payload:
        raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{pair}: not a Kraken response envelope")
    errors = payload["error"]
    if not isinstance(errors, list) or not all(isinstance(e, str) for e in errors):
        raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{pair}: the error field is not a list of strings")
    if errors:
        if any(e.startswith(RATE_LIMIT_ERRORS) for e in errors):
            raise KrakenOhlcError(KrakenOhlcErrorCode.RATE_LIMITED, f"{pair}: {errors!r}")
        raise KrakenOhlcError(KrakenOhlcErrorCode.API_ERROR, f"{pair}: {errors!r}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{pair}: no result object")
    keys = [k for k in result if k != "last"]
    if len(keys) != 1:
        raise KrakenOhlcError(KrakenOhlcErrorCode.RESULT_KEY, f"{pair}: expected one result key, got {sorted(keys)!r}")
    if keys[0] not in RESULT_KEYS[pair]:
        raise KrakenOhlcError(KrakenOhlcErrorCode.RESULT_KEY, f"{pair}: unexpected result key {keys[0]!r}")
    if "last" in result and not _is_int(result["last"]):
        raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{pair}: last {result['last']!r} is not an integer")
    rows = result[keys[0]]
    if not isinstance(rows, list):
        raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{pair}: the rows are not a list")
    return rows


def parse_rows(pair: str, rows: list[object], since: date, now_ms: int) -> KrakenDailySeries:
    """Validate every row, keep the days from ``since`` on and split off the still-open candle."""
    if not rows:
        raise KrakenOhlcError(KrakenOhlcErrorCode.EMPTY, f"{pair}: no candle returned")
    since_ms = day_open_ms(since)
    closed: list[Bar] = []
    live_day: date | None = None
    live_open: float | None = None
    first: int | None = None
    last: int | None = None
    for n, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != ROW_COLUMNS:
            raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{pair}: row {n} is not an OHLC row")
        seconds = row[0]
        if not _is_int(seconds) or seconds < 0:
            raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{pair}: row {n} time {seconds!r} is not an epoch second")
        if seconds % SECONDS_PER_DAY:
            raise KrakenOhlcError(KrakenOhlcErrorCode.NOT_MIDNIGHT, f"{pair}: row {n} time {seconds!r} is not 00:00 UTC")
        t = seconds * 1000
        if t > now_ms:
            raise KrakenOhlcError(
                KrakenOhlcErrorCode.CLOCK_SKEW, f"{pair}: candle {utc_day(t)} opens after the local clock (is it behind?)"
            )
        if live_open is not None:
            raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{pair}: a row follows the still-open candle")
        if last is not None and t == last:
            raise KrakenOhlcError(KrakenOhlcErrorCode.DUPLICATE, f"{pair}: {utc_day(t)} appears twice")
        if last is not None and t < last:
            raise KrakenOhlcError(KrakenOhlcErrorCode.NON_ASCENDING, f"{pair}: {utc_day(t)} after {utc_day(last)}")
        day = utc_day(t)
        o = _number(row[1], f"{pair} {day} open", positive=True)
        _number(row[5], f"{pair} {day} vwap", positive=False)
        _number(row[6], f"{pair} {day} volume", positive=False)
        if not _is_int(row[7]) or row[7] < 0:
            raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{pair} {day} trade count {row[7]!r}")
        if t + MS_PER_DAY > now_ms:
            live_open = o  # still open: its high, low and close are not final and are never read
            if t >= since_ms:
                live_day = day
        else:
            h = _number(row[2], f"{pair} {day} high", positive=True)
            low = _number(row[3], f"{pair} {day} low", positive=True)
            c = _number(row[4], f"{pair} {day} close", positive=True)
            if not low <= min(o, c) <= max(o, c) <= h:
                raise KrakenOhlcError(KrakenOhlcErrorCode.MALFORMED, f"{pair} {day}: open/close outside low..high")
            if t >= since_ms:
                closed.append(Bar(t, o, h, low, c))
        first = t if first is None else first
        last = t
    history_start = utc_day(first) if first is not None and first > since_ms else None
    return KrakenDailySeries(
        pair,
        tuple(closed),
        live_day,
        live_open if live_day is not None else None,
        history_start=history_start,
    )


def _plain_session() -> object:
    # The radar's own `requests` (through http_client, the one module allowed to own the transport),
    # with every environment setting off: no proxy, .netrc or CA bundle from the environment.
    session = http_client.requests.Session()
    session.trust_env = False
    return session


class KrakenPublicOhlc:
    """Daily OHLC of XBTEUR and ETHEUR over the guarded public Kraken path (the trend Fetcher protocol)."""

    def __init__(
        self,
        *,
        http_session: object | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        sleep: Callable[[float], object] = time.sleep,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        inner = http_session if http_session is not None else _plain_session()
        inner.trust_env = False  # type: ignore[attr-defined]
        inner.auth = None  # type: ignore[attr-defined]
        self._session = GuardedSession(
            timeout, max_retries, backoff_seconds, http_session=inner, timer=timer, sleep=sleep  # type: ignore[arg-type]
        )
        self._max_requests = max_requests
        self._requests = 0
        self._stopped: KrakenOhlcError | None = None  # rate limited: no later request is sent

    @property
    def requests_sent(self) -> int:
        """Guarded ``get`` calls made (each is at most ``max_retries + 1`` transport attempts)."""
        return self._requests

    def close(self) -> None:
        self._session.close()

    def _get(self, params: Mapping[str, object]) -> object:
        assert_allowed_params(params)
        pair = params["pair"]
        if self._stopped is not None:
            raise KrakenOhlcError(self._stopped.code, f"{pair}: not sent ({self._stopped.detail})")
        if self._requests >= self._max_requests:
            raise KrakenOhlcError(KrakenOhlcErrorCode.BUDGET, f"{pair}: request budget of {self._max_requests} spent")
        self._requests += 1
        try:
            response = self._session.get(OHLC_URL, params=dict(params))
        except (RedirectRefused, RedirectNotFollowed) as error:
            raise KrakenOhlcError(KrakenOhlcErrorCode.REDIRECT, f"{pair}: {error}") from error
        except ApiError as error:  # network error, timeout, 429 or 5xx after the session's retries
            if "HTTP 429" in str(error):
                self._stopped = KrakenOhlcError(KrakenOhlcErrorCode.RATE_LIMITED, "HTTP 429 after the session's retries")
                raise KrakenOhlcError(self._stopped.code, f"{pair}: {self._stopped.detail}") from error
            raise KrakenOhlcError(KrakenOhlcErrorCode.NETWORK, f"{pair}: {error}") from error
        except OSError as error:  # requests.HTTPError (another 4xx) and other transport errors
            raise KrakenOhlcError(KrakenOhlcErrorCode.HTTP_ERROR, f"{pair}: {type(error).__name__}: {error}") from error
        try:
            body = response.content
        except OSError as error:
            raise KrakenOhlcError(KrakenOhlcErrorCode.NETWORK, f"{pair}: {type(error).__name__}: {error}") from error
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
        if not isinstance(body, bytes):
            raise KrakenOhlcError(KrakenOhlcErrorCode.INVALID_JSON, f"{pair}: response body is not bytes")
        if len(body) > MAX_RESPONSE_BYTES:
            raise KrakenOhlcError(KrakenOhlcErrorCode.TOO_LARGE, f"{pair}: response over {MAX_RESPONSE_BYTES} bytes")
        payload = parse_body(body)
        try:
            return result_rows(str(pair), payload)
        except KrakenOhlcError as error:
            if error.code is KrakenOhlcErrorCode.RATE_LIMITED:
                self._stopped = error
            raise

    def fetch_daily(self, symbol: str, since: date, now_ms: int) -> KrakenDailySeries:
        """Every daily candle of ``symbol`` (``XBTEUR`` or ``ETHEUR``) from ``since`` (UTC) up to ``now_ms``."""
        if not isinstance(symbol, str) or symbol not in ALLOWED_PAIRS:
            raise _refuse(f"pair {symbol!r}")
        if isinstance(since, datetime) or not isinstance(since, date):
            raise _refuse(f"since {since!r} is not a date")
        if not _is_int(now_ms) or now_ms < 0:
            raise _refuse(f"now_ms {now_ms!r}")
        # One day earlier, so the day `since` itself is returned whether Kraken reads `since` as
        # inclusive or exclusive; days before `since` are dropped.
        since_seconds = max(0, day_open_ms(since) // 1000 - SECONDS_PER_DAY)
        rows = self._get({"pair": symbol, "interval": INTERVAL, "since": since_seconds})
        return parse_rows(symbol, rows, since, now_ms)  # type: ignore[arg-type]
