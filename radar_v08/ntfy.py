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

Zero Kraken private endpoints, zero API keys, zero trading, zero Claude/
Anthropic calls - the only network destination here is https://ntfy.sh/.
No authentication is used at this phase (ntfy topics are unauthenticated).
"""

from __future__ import annotations

import logging
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


def send_ntfy_notification(title: str, message: str, priority: str = "default") -> str:
    """POSTs one push notification to ntfy.sh. Never raises: offline,
    timeout, and HTTP error responses all degrade to RESULT_FAILED after a
    limited number of retries. Returns one of RESULT_SUCCESS/RESULT_FAILED/
    RESULT_DISABLED.
    """
    if not is_configured():
        return RESULT_DISABLED

    url = f"{config.NTFY_URL_BASE.rstrip('/')}/{config.NTFY_TOPIC}"
    headers = {"Title": _safe_title_header(title), "Priority": priority}

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
