"""Guarded HTTP client: the only way radar v0.8 code is allowed to reach the
Kraken public market-data network. Every call is checked against the exact
public allowlist (security.assert_allowed_request: https, exact host,
exact path, GET only, no userinfo/port) before it leaves the process.
Redirects are never followed (allow_redirects=False): a 3xx to a target
outside the allowlist raises security.RedirectRefused, and one to an
allowlisted target raises RedirectNotFollowed. 429/5xx responses are retried
with exponential backoff instead of silently failing or crashing the whole
run. Ollama and ntfy do not go through this client and are refused by it.

Each session also keeps per-endpoint request counters (observability only;
they never change what is sent, retried or raised): see request_stats().
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, NoReturn
from urllib.parse import urljoin, urlsplit

import requests

from . import security
from .security import SecurityViolation

logger = logging.getLogger("radar_v08.http")

# Per-endpoint counter names, in the order request_stats() reports them.
# requests: get() calls that passed the allowlist guard; attempts: transport
# calls started; network_ms: wall time inside the transport call, summed per
# attempt (so it can exceed wall time when threads share a session);
# backoff_ms: time spent in the retry sleep; failures: get() calls that ended
# in an exception after passing the guard.
REQUEST_STAT_FIELDS = ("requests", "attempts", "network_ms", "backoff_ms", "failures")

RequestStats = dict[str, dict[str, float]]


class ApiError(RuntimeError):
    """Raised for non-retryable API failures (4xx other than 429, bad payload)."""


class RedirectNotFollowed(ApiError):
    """A 3xx response was received and, by policy, not followed.

    Used when the redirect has no usable Location or points to an allowlisted
    target; a target outside the allowlist raises security.RedirectRefused.
    """


class GuardedSession:
    """A requests.Session wrapper that only ever performs allowlisted public GET calls."""

    def __init__(
        self,
        timeout: float,
        max_retries: int,
        backoff_base: float,
        http_session: requests.Session | None = None,
        *,
        timer: Callable[[], float] | None = None,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        # Every call below passes allow_redirects=False. max_redirects is left
        # at its default on purpose: requests raises TooManyRedirects from a
        # plain 3xx when it is 0, even with allow_redirects=False, which would
        # hide the typed redirect errors raised here.
        self._session = http_session if http_session is not None else requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        # None means time.perf_counter / time.sleep, looked up at call time so
        # a test that patches radar_v08.http_client.time.sleep still sees it.
        self._timer = timer
        self._sleep = sleep
        self._stats_lock = threading.Lock()
        self._stats: RequestStats = {}

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        security.assert_allowed_request("GET", url)

        endpoint = _endpoint_key(url)
        self._add_stat(endpoint, "requests", 1)
        try:
            return self._get_with_retries(url, params, endpoint)
        except Exception:
            self._add_stat(endpoint, "failures", 1)
            raise

    def request_stats(self) -> RequestStats:
        """Independent copy of the per-endpoint counters since this session was created.

        Keyed by scheme-less host + path (no query string or params). Take one
        snapshot before and one after a cycle and use request_stats_delta().
        """
        with self._stats_lock:
            return {endpoint: dict(counters) for endpoint, counters in self._stats.items()}

    def _get_with_retries(self, url: str, params: dict[str, Any] | None, endpoint: str) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._add_stat(endpoint, "attempts", 1)
            started = self._read_clock()
            try:
                try:
                    response = self._session.request(
                        "GET", url, params=params, timeout=self.timeout, allow_redirects=False
                    )
                finally:
                    self._add_elapsed(endpoint, "network_ms", started)
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("GET %s failed (attempt %d): %s", url, attempt + 1, exc)
                if attempt < self.max_retries:
                    self._backoff(endpoint, self.backoff_base * (2**attempt))
                continue

            if 300 <= response.status_code < 400 or getattr(response, "history", None):
                _refuse_redirect(url, response)

            if response.status_code == 429 or response.status_code >= 500:
                last_exc = ApiError(f"HTTP {response.status_code} from {url}")
                logger.warning(
                    "GET %s returned %d (attempt %d), retrying with backoff",
                    url,
                    response.status_code,
                    attempt + 1,
                )
                if attempt < self.max_retries:
                    self._backoff(endpoint, self.backoff_base * (2**attempt))
                continue

            response.raise_for_status()
            return response

        raise ApiError(f"GET {url} failed after {self.max_retries + 1} attempts: {last_exc}")

    def close(self) -> None:
        self._session.close()

    def _backoff(self, endpoint: str, seconds: float) -> None:
        started = self._read_clock()
        try:
            (self._sleep or time.sleep)(seconds)
        finally:
            self._add_elapsed(endpoint, "backoff_ms", started)

    # The counters are observability only: a failure here is swallowed so it
    # can never change what get() returns or raises.
    def _read_clock(self) -> float | None:
        try:
            return float((self._timer or time.perf_counter)())
        except Exception:
            return None

    def _add_elapsed(self, endpoint: str, field: str, started: float | None) -> None:
        if started is None:
            return
        ended = self._read_clock()
        if ended is None:
            return
        self._add_stat(endpoint, field, (ended - started) * 1000.0)

    def _add_stat(self, endpoint: str, field: str, amount: float) -> None:
        try:
            with self._stats_lock:
                counters = self._stats.get(endpoint)
                if counters is None:
                    counters = self._stats[endpoint] = {
                        name: (0.0 if name.endswith("_ms") else 0) for name in REQUEST_STAT_FIELDS
                    }
                counters[field] += amount
        except Exception:
            pass


def request_stats_delta(
    before: Mapping[str, Mapping[str, float]], after: Mapping[str, Mapping[str, float]]
) -> RequestStats:
    """Per-endpoint counters accumulated between two request_stats() snapshots.

    Endpoints with no activity in the window are left out.
    """
    delta: RequestStats = {}
    for endpoint, counters in after.items():
        base = before.get(endpoint, {})
        diff = {name: counters.get(name, 0) - base.get(name, 0) for name in REQUEST_STAT_FIELDS}
        if any(diff.values()):
            delta[endpoint] = diff
    return delta


def _endpoint_key(url: str) -> str:
    """Scheme-less host + path of an allowlisted URL; never the query or params."""
    try:
        parts = urlsplit(url)
        return f"{parts.netloc}{parts.path}"
    except Exception:
        return "unknown"


def _refuse_redirect(url: str, response: requests.Response) -> NoReturn:
    """Never follow a redirect; always raise a typed error, never retry."""
    status = response.status_code
    if getattr(response, "history", None):
        raise security.RedirectRefused(f"GET {url} came back through a redirect chain; redirects are never followed")
    location = response.headers.get("Location")
    if not location:
        raise RedirectNotFollowed(f"HTTP {status} from {url} without a Location; redirects are never followed")
    try:
        target = urljoin(url, location)
        # Classify on scheme/host/path only; a query on the target does not
        # make an allowlisted endpoint a foreign one.
        security.assert_allowed_request("GET", target.split("#", 1)[0].split("?", 1)[0])
    except (SecurityViolation, ValueError) as exc:
        raise security.RedirectRefused(
            f"HTTP {status} from {url} redirects outside the public allowlist ({location!r}); refused, not followed"
        ) from exc
    raise RedirectNotFollowed(f"HTTP {status} from {url} redirects to {target!r}; redirects are never followed")
