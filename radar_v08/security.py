"""Security guards for radar v0.8 - read-only, public-data-only, absolute.

These guards exist because this radar must NEVER be able to trade, transfer,
or otherwise act on an account. See docs/RADAR_v0.8_ARCHITECTURE.md section 14
and the acceptance criteria in the phase 1 task for the exact rules enforced
here. Every guard is deliberately loud (raises) rather than silently skipping.
"""

from __future__ import annotations

import os

PRIVATE_ENV_VARS = ("KRAKEN_API_KEY", "KRAKEN_SECRET")


class SecurityViolation(RuntimeError):
    """Raised when the radar would otherwise touch private/trading capability."""


def assert_no_private_credentials(env: dict | None = None) -> None:
    """Abort if any private Kraken credential is present in the environment.

    Guard #1 and #2 from the task spec: if KRAKEN_API_KEY or KRAKEN_SECRET
    exist, abort immediately. This radar only ever needs public endpoints;
    the mere presence of credentials in-process is treated as a hazard.
    """
    source = env if env is not None else os.environ
    for var in PRIVATE_ENV_VARS:
        if source.get(var):
            raise SecurityViolation(
                f"Refusing to run: {var} is set in the environment. "
                "This radar is read-only and must never see trading credentials."
            )


def assert_public_get(method: str, url: str) -> None:
    """Abort if a request is not a GET, or targets a private endpoint.

    Guard #3 and #4 from the task spec.
    """
    if str(method).upper() != "GET":
        raise SecurityViolation(
            f"Refusing non-GET request ({method} {url}). "
            "This radar may only issue public GET requests."
        )
    if "/private/" in url:
        raise SecurityViolation(
            f"Refusing request to a private endpoint: {url}. "
            "This radar may only use public market data endpoints."
        )


def run_all_guards(env: dict | None = None) -> None:
    """Convenience entry point: run every startup guard."""
    assert_no_private_credentials(env)
