"""Guarded HTTP client: the only way radar v0.8 code is allowed to reach the
network. Every call is checked against the security guards (GET only, no
`/private/` path) before it leaves the process, and 429/5xx responses are
retried with exponential backoff instead of silently failing or crashing the
whole run.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from . import security

logger = logging.getLogger("radar_v08.http")


class ApiError(RuntimeError):
    """Raised for non-retryable API failures (4xx other than 429, bad payload)."""


class GuardedSession:
    """A requests.Session wrapper that only ever performs public GET calls."""

    def __init__(
        self,
        timeout: float,
        max_retries: int,
        backoff_base: float,
    ) -> None:
        self._session = requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        security.assert_public_get("GET", url)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.request("GET", url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("GET %s failed (attempt %d): %s", url, attempt + 1, exc)
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base * (2**attempt))
                continue

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
