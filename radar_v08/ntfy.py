"""ntfy.sh mobile push notifications - mobile notifications extension.

RADAR -> QWEN -> DEMAND ROUTER -> EVENT -> NOTIFICATION SERVICE
                                              |-- Windows notification (notifications.py)
                                              `-- ntfy mobile notification (this module)

Sends one short HTTP POST to https://ntfy.sh/<topic> for MEDIUM/HIGH events
only (LOW never reaches this module - see notifications.py). The topic comes
ONLY from the CRYPTO_RADAR_NTFY_TOPIC environment variable: never hardcoded,
never written to a log line in full (see `masked_topic`).

This module's only job is the HTTP transport: timeout, limited retry, error
handling, and a structured SUCCESS/FAILED/DISABLED result. It never raises -
ntfy.sh being unreachable, slow, or erroring must never take the radar down
(offline/failure handling requirement). Dedup by event_id and cross-cycle
retry live in notifications.py, backed by SQLite (`events.ntfy_status`).

`notification_id`, when given, rides the `X-ID` header ntfy's publish
API accepts for a caller-chosen message ID (see https://docs.ntfy.sh/publish/
#message-id). It is derived in notifications.py from a real outbox delivery
ID and is the same value on a cross-cycle retry of the same event - this
module does not rely on ntfy.sh itself deduplicating on it; it only makes the
resend identifiable as the same notification rather than a fresh one.

Zero Kraken private endpoints, zero API keys, zero trading, zero Claude/
Anthropic calls - the only network destination here is https://ntfy.sh/.
No authentication is used at this phase (ntfy topics are unauthenticated).
"""

from __future__ import annotations

import logging
import re
import time

import requests

from . import config

logger = logging.getLogger("radar_v08.ntfy")

RESULT_SUCCESS = "SUCCESS"
RESULT_FAILED = "FAILED"
RESULT_DISABLED = "DISABLED"


def is_configured() -> bool:
    return bool(config.NTFY_TOPIC)


def masked_topic() -> str:
    """Safe-for-logs form of the topic - never the full value (mobile
    notifications task section 1/12: never expose the topic/credential)."""
    topic = config.NTFY_TOPIC
    if not topic:
        return "(unset)"
    if len(topic) <= 4:
        return "*" * len(topic)
    return f"{topic[:2]}***{topic[-2:]}"


def priority_for_level(level: str) -> str:
    return config.NTFY_PRIORITY_BY_LEVEL.get(level, "default")


def _safe_title_header(title: str) -> str:
    """HTTP header values must be latin-1; fall back rather than crash the
    send over a non-ASCII asset symbol or similar edge case."""
    try:
        title.encode("latin-1")
        return title
    except UnicodeEncodeError:
        return "CRYPTO RADAR"


_NOTIFICATION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _safe_notification_id(notification_id: str | None) -> str | None:
    """`None` unless `notification_id` is 1..64 characters of `[A-Za-z0-9_-]` - a header-safe
    value that can never smuggle a CR/LF or a separator into the request. Dropping it rather
    than crashing the send is the same degrade-gracefully rule as `_safe_title_header`: a
    bad/missing ID never blocks a real send; it just means this one resend isn't identifiable
    as the same notification. (The ID from notifications.py is always 16 hex characters.)"""
    if not isinstance(notification_id, str) or _NOTIFICATION_ID.fullmatch(notification_id) is None:
        return None
    return notification_id


def send_ntfy_notification(
    title: str, message: str, priority: str = "default", notification_id: str | None = None
) -> str:
    """POSTs one push notification to ntfy.sh. Never raises: offline,
    timeout, and HTTP error responses all degrade to RESULT_FAILED after a
    limited number of retries. Returns one of RESULT_SUCCESS/RESULT_FAILED/
    RESULT_DISABLED.

    `notification_id` is a stable ID derived from a real outbox
    delivery ID; see the module docstring. Never fabricated here - the caller
    passes `None` when it has no real outbox row to derive one from, and this
    function simply sends without the header in that case.
    """
    if not is_configured():
        return RESULT_DISABLED

    url = f"{config.NTFY_URL_BASE.rstrip('/')}/{config.NTFY_TOPIC}"
    headers = {"Title": _safe_title_header(title), "Priority": priority}
    safe_id = _safe_notification_id(notification_id)
    if safe_id is not None:
        headers["X-ID"] = safe_id

    total_attempts = config.NTFY_MAX_RETRIES + 1
    last_error: str | None = None

    for attempt in range(1, total_attempts + 1):
        try:
            response = requests.post(
                url, data=message.encode("utf-8"), headers=headers, timeout=config.NTFY_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            logger.warning(
                "ntfy POST failed (topic=%s, attempt %d/%d): %s",
                masked_topic(), attempt, total_attempts, last_error,
            )
        else:
            if response.status_code == 200:
                return RESULT_SUCCESS
            last_error = f"HTTP {response.status_code}"
            logger.warning(
                "ntfy POST returned %s (topic=%s, attempt %d/%d)",
                last_error, masked_topic(), attempt, total_attempts,
            )
            if response.status_code != 429 and response.status_code < 500:
                break  # non-retryable client error (e.g. malformed topic) - stop early

        if attempt < total_attempts:
            time.sleep(config.NTFY_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))

    logger.warning("ntfy notification failed after %d attempt(s): %s", total_attempts, last_error)
    return RESULT_FAILED
