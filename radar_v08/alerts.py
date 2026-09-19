"""Alert recovery: `python radar.py --mode alerts` and
`python radar.py --mode prompt --event <EVENT_ID>`.

A Crypto Radar alert must never be lost just because the Windows toast or the
ntfy push scrolled out of view. Every SONNET/FABLE-demand event is already
sitting in SQLite (`events` table) with everything needed to rebuild its
prompt (`events.context_json`) - this module only ever reads that table and
re-runs the exact same prompt-building/clipboard/popup path a real alert
already uses. It never stores a second copy of the prompt and never adds a
new persistence mechanism: the event row is the only source of truth, exactly
as before this feature existed.

No L0/L1/L2/L3, Qwen, Demand Router, Claude Bridge, ntfy client, Kraken, or
trading code is touched or reimplemented here - `list_alert_events` is a
plain read against the existing `events` table, and `recover_prompt` reuses
`notifications.copy_prompt_for_event` (the same function a real MEDIUM/HIGH
notification already calls) rather than duplicating its clipboard/popup logic.
"""

from __future__ import annotations

from typing import Any

from .store import SnapshotStore
from .terminal import safe_print

BAR = "=" * 40


def is_mock_alert(event_row: Any) -> bool:
    """MOCK-* events (mock_alert.py / `--mode mock-alert`) must never be
    confused with a real alert (task section 7)."""
    event_id = event_row["event_id"] if "event_id" in event_row.keys() else None
    event_type = event_row["type"] if "type" in event_row.keys() else None
    return bool(event_id and str(event_id).startswith("MOCK-")) or event_type == "MOCK_TEST_EVENT"


def list_alerts(store: SnapshotStore, limit: int | None = None) -> list[Any]:
    """Recent SONNET/FABLE-demand events, most recent first - real alerts and
    MOCK-* test alerts alike (see `is_mock_alert` for how callers tell them
    apart). `limit` defaults to config.ALERTS_HISTORY_LIMIT.
    """
    from . import config

    return store.list_alert_events(limit if limit is not None else config.ALERTS_HISTORY_LIMIT)


def _fmt_score(value: Any) -> str:
    return f"{value:.0f}" if isinstance(value, (int, float)) else "n/a"


def format_alert_history(alerts_rows: list[Any]) -> str:
    """Just enough to identify and recover an alert (task section 1) - never
    the full event/context dump; that lives behind `--mode prompt --event`.
    """
    lines = [BAR, "CRYPTO RADAR — ALERT HISTORY", BAR, ""]

    if not alerts_rows:
        lines.append("(no alerts recorded yet)")
        lines.append("")
        lines.append(BAR)
        return "\n".join(lines)

    for index, row in enumerate(alerts_rows, start=1):
        ts = row["ts"] or ""
        time_part = ts[11:19] if len(ts) >= 19 else ts
        test_marker = " [TESTE]" if is_mock_alert(row) else ""
        ntfy_status = row["ntfy_status"] if "ntfy_status" in row.keys() else None

        lines.append(f"[{index}] {time_part}{test_marker}")
        lines.append(f"{row['asset']}")
        lines.append(f"{row['setup_type'] or 'NONE'} / {row['direction'] or 'NONE'}")
        lines.append(f"Opportunity: {_fmt_score(row['opportunity_score'])}")
        lines.append(f"Tradeability: {_fmt_score(row['tradeability_score'])}")
        lines.append(f"Model: {row['model_demand']}")
        lines.append(f"Event: {row['event_id']}")
        lines.append(f"ntfy: {ntfy_status or 'N/A'}")
        lines.append("")

    lines.append(BAR)
    return "\n".join(lines)


def recover_prompt(store: SnapshotStore, event_id: str) -> bool:
    """`--mode prompt --event <id>` (task section 2): finds the persisted
    event, and - if found - hands off to `notifications.copy_prompt_for_event`,
    the SAME function a real MEDIUM/HIGH notification already calls, so the
    prompt is rebuilt with the same prompt_builder.py, copied with the same
    clipboard code, and opens the same popup (task section 3/4: no second
    prompt/clipboard/popup implementation). Prints "Event not found" and
    returns False, without raising, when the event_id does not exist.
    """
    event_row = store.get_event(event_id)
    if event_row is None:
        safe_print("Event not found")
        return False

    # local import: only pulls in the full notification stack when actually used
    from . import notifications

    result = notifications.copy_prompt_for_event({"event_id": event_id}, store=store)
    return result["copied"]
