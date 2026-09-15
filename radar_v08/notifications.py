"""Notification service: Windows toast + ntfy mobile push + COPIAR PROMPT.

RADAR -> QWEN -> DEMAND ROUTER -> EVENT -> NOTIFICATION SERVICE
                                              |-- Windows notification (this module)
                                              |-- ntfy mobile notification (ntfy.py)
                                              `-- COPIAR PROMPT: clipboard copy
                                                  (prompt_builder.py, clipboard.py)
                                                  + auxiliary window (prompt_popup.py)

Uses the built-in Windows.UI.Notifications WinRT API via a short PowerShell
script (powershell.exe ships with Windows; nothing is installed) for the
desktop toast, and a plain HTTP POST to https://ntfy.sh/<topic> (ntfy.py) for
the phone push. Toasts ride PowerShell's own registered AUMID so they land in
the real Action Center / notification history, not just a console line or a
tray balloon.

Categories (task section 4/13):
  LOW    -> terminal only, no toast, no ntfy push (this module never sends
            either for LOW)
  MEDIUM -> SONNET result: toast (no sound) + ntfy at default priority
  HIGH   -> FABLE result: toast + sound (where the platform honors it) +
            ntfy at high priority

Dedup: claude_bridge.py only invokes `notify_fn` once per event (gated on
`events.notified`), which covers the Windows toast. The ntfy push has its own
independent, SQLite-backed dedup by event_id (`events.ntfy_status`) so a
mobile push failure can be retried across cycles (see `retry_pending_ntfy`)
without ever re-sending the Windows toast or touching the event's own
analysis status.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from . import clipboard, config, ntfy, prompt_popup
from .prompt_builder import build_prompt_text
from .terminal import safe_print

if TYPE_CHECKING:
    from .store import SnapshotStore

logger = logging.getLogger("radar_v08.notifications")

# Riding PowerShell's own registered AUMID is the standard way to get a real,
# Action-Center-listed toast from an unpackaged script without installing a
# module (e.g. BurntToast) or registering our own shortcut.
_POWERSHELL_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# Invoked via `-File`, never `-Command`: a multi-line, quote-heavy script
# passed as a single -Command argument gets re-tokenized by Windows argv
# parsing when launched through subprocess (no shell) and silently corrupts
# embedded quotes - `-File` on a real .ps1 has none of that ambiguity.
_TOAST_SCRIPT = r"""
param([string]$TitleB64, [string]$MessageB64, [string]$AppId, [string]$Sound)
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

$title = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($TitleB64))
$message = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($MessageB64))

$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$textNodes = $template.GetElementsByTagName('text')
$textNodes.Item(0).AppendChild($template.CreateTextNode($title)) | Out-Null
$textNodes.Item(1).AppendChild($template.CreateTextNode($message)) | Out-Null

if ($Sound -eq '1') {
    $toastNode = $template.GetElementsByTagName('toast').Item(0)
    $audio = $template.CreateElement('audio')
    $audio.SetAttribute('src', 'ms-winsoundevent:Notification.Default')
    $toastNode.AppendChild($audio) | Out-Null
}

$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
"""

_SCRIPT_PATH = os.path.join(config.STATE_DIR, "_radar_toast_notify.ps1")


def _ensure_script_on_disk() -> str:
    if not os.path.exists(_SCRIPT_PATH):
        with open(_SCRIPT_PATH, "w", encoding="utf-8") as fh:
            fh.write(_TOAST_SCRIPT)
    return _SCRIPT_PATH


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def send_windows_notification(title: str, message: str, sound: bool = False, app_id: str | None = None) -> bool:
    """Best-effort: True if the notification command ran without error. Never
    raises - a notification failure must never take the radar down with it
    (task section 13/16: the radar keeps working regardless).
    """
    if not config.NOTIFICATIONS_ENABLED:
        return False
    try:
        script_path = _ensure_script_on_disk()
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-File", script_path,
                "-TitleB64", _b64(title), "-MessageB64", _b64(message),
                "-AppId", app_id or _POWERSHELL_AUMID, "-Sound", "1" if sound else "0",
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.warning("Windows notification failed: %s", (result.stderr or "").strip())
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - notifications are best-effort, never fatal
        logger.warning("Windows notification failed: %s", exc)
        return False


def notification_level_for_model(model_demand: str) -> str:
    return config.NOTIFICATION_LEVEL_BY_MODEL.get(model_demand, "LOW")


def build_notification_text(event: dict[str, Any]) -> tuple[str, str]:
    """Short title + body (task section 13: never the full analysis - the
    terminal shows detail)."""
    asset = event.get("asset", "?")
    setup = event.get("setup_type", "NONE")
    direction = event.get("direction", "NONE")
    model = event.get("model", "?")
    opportunity = event.get("opportunity_score")
    tradeability = event.get("tradeability_score")
    recommendation = event.get("recommendation") or "?"

    title = f"CRYPTO RADAR - {asset} - {setup} {direction}"
    opp_txt = f"{opportunity:.0f}" if isinstance(opportunity, (int, float)) else "n/a"
    trd_txt = f"{tradeability:.0f}" if isinstance(tradeability, (int, float)) else "n/a"
    message = f"Opportunity: {opp_txt}  Tradeability: {trd_txt}\nModel: {model}  Recommendation: {recommendation}"
    return title, message


def build_mobile_notification_text(event: dict[str, Any]) -> tuple[str, str]:
    """Short title + one-line body for the ntfy push (task section 3) -
    shorter than the Windows toast text, and never the full analysis; the
    detail stays in the terminal/log/event as before.

    Example: title "CRYPTO RADAR - PEPE", body
    "BREAKOUT LONG | Opp: 82 | Trade: 88 | Modelo: FABLE".
    """
    asset = event.get("asset", "?")
    setup = event.get("setup_type") or "NONE"
    direction = event.get("direction") or "NONE"
    model = event.get("model", "?")
    opportunity = event.get("opportunity_score")
    tradeability = event.get("tradeability_score")

    title = f"CRYPTO RADAR - {asset}"
    if event.get("test_event"):
        # mock-alert only (task: never confuse a synthetic mobile push with a
        # real alert while away from the computer).
        title = f"[TESTE] {title}"
    opp_txt = f"{opportunity:.0f}" if isinstance(opportunity, (int, float)) else "n/a"
    trd_txt = f"{tradeability:.0f}" if isinstance(tradeability, (int, float)) else "n/a"
    message = f"{setup} {direction} | Opp: {opp_txt} | Trade: {trd_txt} | Modelo: {model}"
    return title, message


def mobile_notifications_enabled() -> bool:
    return ntfy.is_configured()


def send_mobile_notification(event: dict[str, Any], level: str, store: "SnapshotStore | None" = None) -> str:
    """Sends (or skips) the ntfy push for one event.

    Dedup (task section 5): if `store` is given and this event_id already has
    `ntfy_status == "SENT"`, nothing is sent again - the same event_id can
    never produce two ntfy notifications, including across reruns/reexecutions.
    A FAILED send never touches the event's analysis status; it only ever
    updates `ntfy_status`/`ntfy_last_error` so `retry_pending_ntfy` can pick
    it up later (task section 7).
    """
    if level == "LOW":
        return ntfy.RESULT_DISABLED  # never sent to the phone, task section 4

    event_id = event.get("event_id")
    if store is not None and event_id:
        existing = store.get_event(event_id)
        if existing is not None and existing["ntfy_status"] == "SENT":
            return "SENT"

    title, message = build_mobile_notification_text(event)
    priority = ntfy.priority_for_level(level)
    now_iso = datetime.now(timezone.utc).isoformat()

    if store is not None and event_id:
        store.set_ntfy_status(event_id, "PENDING", now_iso)

    result = ntfy.send_ntfy_notification(title, message, priority=priority)

    if store is not None and event_id:
        completed_iso = datetime.now(timezone.utc).isoformat()
        if result == ntfy.RESULT_SUCCESS:
            store.set_ntfy_status(event_id, "SENT", completed_iso)
        elif result == ntfy.RESULT_FAILED:
            store.set_ntfy_status(event_id, "FAILED", completed_iso, error="ntfy_send_failed")
        # RESULT_DISABLED: ntfy isn't configured - nothing to dedupe or retry.

    return result


def copy_prompt_for_event(event: dict[str, Any], store: "SnapshotStore | None" = None) -> dict[str, bool]:
    """Copies the full ready-to-paste analysis prompt for this event to the
    Windows clipboard, then opens a small auxiliary window with a COPIAR
    PROMPT button (see prompt_popup.py - the toast has no registered
    activation handler, so a real clickable toast action isn't reliably
    supported here). The clipboard copy happens immediately in this process,
    so pasting never depends on clicking anything first; the window is a
    visual anchor + a way to copy again later.

    Returns `{"copied": bool, "popup_opened": bool}`.

    `store` must be given (it is how the full persisted event row - L1/L2/L3/
    Qwen/router context - is looked up); without it, or without an event_id,
    or with the feature disabled, this is a no-op (both False). Best-effort
    and silent on failure otherwise: never raises, never blocks the radar
    loop, never prints the prompt text itself (only the two confirmation
    lines below).
    """
    result = {"copied": False, "popup_opened": False}
    if not config.COPY_PROMPT_ENABLED or store is None:
        return result

    event_id = event.get("event_id")
    if not event_id:
        return result

    event_row = store.get_event(event_id)
    if event_row is None:
        return result

    prompt_text = build_prompt_text(event_row)
    copied = clipboard.copy_text_to_clipboard(prompt_text)
    result["copied"] = copied
    if copied:
        safe_print("Prompt copied to clipboard ✅")
        safe_print(f"Event: {event_id}")
    else:
        logger.warning("Failed to copy prompt to clipboard for event %s", event_id)

    if config.COPY_PROMPT_POPUP_ENABLED:
        result["popup_opened"] = prompt_popup.launch_copy_prompt_popup(event_id, prompt_text)

    return result


def notify_for_event(event: dict[str, Any], store: "SnapshotStore | None" = None) -> dict[str, Any]:
    """Wraps level selection + text formatting + dispatch (Windows toast, ntfy
    mobile push, and the COPIAR PROMPT clipboard copy) for one PROCESSED
    Claude Bridge event. `event` is the dict claude_bridge.run_bridge_cycle
    passes to its `notify_fn` callback; `store` (when given) backs the ntfy
    dedup/retry state and the COPIAR PROMPT context lookup.

    A failure sending either notification is best-effort and never raises -
    the radar keeps running regardless (task section 7/16).
    """
    level = notification_level_for_model(event.get("model"))
    result: dict[str, Any] = {
        "level": level, "windows_sent": False, "ntfy_result": ntfy.RESULT_DISABLED,
        "prompt_copied": False, "popup_opened": False,
    }
    if level == "LOW":
        return result  # terminal only, task section 4/13

    title, message = build_notification_text(event)
    result["windows_sent"] = send_windows_notification(title, message, sound=(level == "HIGH"))
    result["ntfy_result"] = send_mobile_notification(event, level, store=store)
    prompt_result = copy_prompt_for_event(event, store=store)
    result["prompt_copied"] = prompt_result["copied"]
    result["popup_opened"] = prompt_result["popup_opened"]
    return result


def retry_pending_ntfy(store: "SnapshotStore", now: datetime | None = None) -> list[dict[str, Any]]:
    """Bounded, spaced-out cross-cycle retry for events whose ntfy push
    previously FAILED (task section 5/7) - never a tight retry loop, capped
    by `config.NTFY_MAX_RETRY_ATTEMPTS` and spaced at least
    `config.NTFY_RETRY_MIN_INTERVAL_SECONDS` apart. Never touches the event's
    own analysis status. A no-op (empty list) when ntfy isn't configured.
    """
    if not ntfy.is_configured():
        return []

    now = now or datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(seconds=config.NTFY_RETRY_MIN_INTERVAL_SECONDS)).isoformat()
    rows = store.find_ntfy_retry_candidates(cutoff_iso, config.NTFY_MAX_RETRY_ATTEMPTS)

    results = []
    for row in rows:
        level = notification_level_for_model(row["model_demand"])
        event = {
            "event_id": row["event_id"], "asset": row["asset"], "model": row["model_demand"],
            "setup_type": row["setup_type"], "direction": row["direction"],
            "opportunity_score": row["opportunity_score"], "tradeability_score": row["tradeability_score"],
        }
        result = send_mobile_notification(event, level, store=store)
        results.append({"event_id": row["event_id"], "ntfy_result": result})
    return results
