"""Guarded HTTP client: the only way radar v0.8 code is allowed to reach the
Kraken public market-data network. Every call is checked against the exact
public allowlist (security.assert_allowed_request: https, exact host,
exact path, GET only, no userinfo/port) before it leaves the process.
Redirects are never followed (allow_redirects=False): a 3xx to a target
outside the allowlist raises security.RedirectRefused, and one to an
allowlisted target raises RedirectNotFollowed. 429/5xx responses are retried
with exponential backoff instead of silently failing or crashing the whole
run. Ollama and ntfy do not go through this client and are refused by it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, NoReturn
from urllib.parse import urljoin

import requests

from . import security
from .security import SecurityViolation

logger = logging.getLogger("radar_v08.http")


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
    ) -> None:
        # Every call below passes allow_redirects=False. max_redirects is left
        # at its default on purpose: requests raises TooManyRedirects from a
        # plain 3xx when it is 0, even with allow_redirects=False, which would
        # hide the typed redirect errors raised here.
        self._session = http_session if http_session is not None else requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        security.assert_allowed_request("GET", url)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.request(
                    "GET", url, params=params, timeout=self.timeout, allow_redirects=False
                )
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("GET %s failed (attempt %d): %s", url, attempt + 1, exc)
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base * (2**attempt))
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
                    time.sleep(self.backoff_base * (2**attempt))
                continue

            response.raise_for_status()
            return response

        raise ApiError(f"GET {url} failed after {self.max_retries + 1} attempts: {last_exc}")

    def close(self) -> None:
        self._session.close()


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
