"""Security guards for radar v0.8 - read-only, public-data-only, absolute.

These guards exist because this radar must NEVER be able to trade, transfer,
or otherwise act on an account. See docs/RADAR_v0.8_ARCHITECTURE.md section 14
and the acceptance criteria in the phase 1 task for the exact rules enforced
here. Every guard is deliberately loud (raises) rather than silently skipping.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from . import config

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


class RedirectRefused(SecurityViolation):
    """Raised when a response redirects to a target outside the public allowlist.

    The redirect is never followed: GuardedSession sends every request with
    allow_redirects=False and raises this instead of reaching the target.
    """


def _has_unsafe_characters(url: str) -> bool:
    # urllib.parse silently strips tab/CR/LF and leading C0 controls/space, so
    # a URL carrying any of them could be parsed differently from how it is
    # sent. Backslashes are refused because some parsers treat them as "/".
    return any(ord(ch) <= 0x20 or ord(ch) == 0x7F or ch == "\\" for ch in url)


def assert_allowed_request(method: str, url: str) -> None:
    """Abort unless (method, url) matches the exact public HTTP allowlist.

    Allowed only when: method is GET; scheme is https; the authority is exactly
    an allowlisted host (no userinfo, no port - not even :443 - no trailing
    dot, no case variant); the path is exactly one allowlisted path for that
    host (no percent-encoding, dot segments, trailing slash or `;params`); and
    there is no query string or fragment in the URL itself (query parameters
    are passed separately and encoded by requests). Everything else raises
    SecurityViolation before any byte leaves the process.
    """
    if not isinstance(method, str) or method.upper() not in config.HTTP_ALLOWED_METHODS:
        raise SecurityViolation(
            f"Refusing non-GET request ({method!r} {url!r}). This radar may only issue public GET requests."
        )
    if not isinstance(url, str) or _has_unsafe_characters(url):
        raise SecurityViolation(f"Refusing malformed request URL: {url!r}.")
    if "/private/" in url.lower():
        raise SecurityViolation(
            f"Refusing request to a private endpoint: {url}. This radar may only use public market data endpoints."
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise SecurityViolation(f"Refusing unparsable request URL: {url!r} ({exc}).") from exc
    if parts.scheme != config.HTTP_ALLOWED_SCHEME or not url.startswith(f"{config.HTTP_ALLOWED_SCHEME}://"):
        raise SecurityViolation(f"Refusing non-https request URL: {url!r}.")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise SecurityViolation(f"Refusing request URL with userinfo: {url!r}.")
    if port is not None or ":" in parts.netloc:
        raise SecurityViolation(f"Refusing request URL with an explicit port: {url!r}.")
    allowed_paths = config.HTTP_PUBLIC_ALLOWLIST.get(parts.netloc)
    if allowed_paths is None:
        raise SecurityViolation(f"Refusing request to a host outside the public allowlist: {parts.netloc!r}.")
    if parts.path not in allowed_paths:
        raise SecurityViolation(
            f"Refusing request to a path outside the public allowlist: {parts.netloc}{parts.path!r}."
        )
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise SecurityViolation(
            f"Refusing request URL with an inline query or fragment: {url!r}. Pass query parameters separately."
        )


def run_all_guards(env: dict | None = None) -> None:
    """Convenience entry point: run every startup guard."""
    assert_no_private_credentials(env)
