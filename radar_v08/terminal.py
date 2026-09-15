"""Terminal status panel (task section 14). No web UI - the terminal IS the
main panel, and it must make obvious whether the radar is alive and whether
it would (or would not) send anything to a cloud model.
"""

from __future__ import annotations

import sys
from typing import Any

BAR = "=" * 50
SEP = "-" * 32


def safe_print(text: str) -> None:
    """print() that never crashes on a legacy (non-UTF-8) Windows console
    codepage - e.g. plain cmd.exe defaulting to cp1252, which cannot encode
    a checkmark/emoji. Falls back to a '?'-substituted encoding instead of
    raising UnicodeEncodeError and taking the radar down over a cosmetic
    console glyph.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def _fmt_score(value: Any) -> str:
    return f"{value:.2f}" if isinstance(value, (int, float)) else "n/a"


def render_terminal(
    output: dict[str, Any],
    uptime_seconds: float | None = None,
    bridge_health: str | None = None,
    event_queue_counts: dict[str, int] | None = None,
    last_event: dict[str, Any] | None = None,
    mobile_notifications_enabled: bool | None = None,
) -> str:
    dq = output.get("data_quality", {})
    funnel = output.get("funnel", {})
    candidates = output.get("candidates", [])

    lines: list[str] = []
    lines.append(BAR)
    lines.append("CRYPTO RADAR")
    lines.append(BAR)
    lines.append("")
    lines.append("STATUS")
    lines.append(SEP)
    lines.append(f"Mode: {output.get('mode', 'UNKNOWN')}")
    lines.append(f"Uptime: {uptime_seconds:.0f}s" if uptime_seconds is not None else "Uptime: n/a")
    lines.append(f"Last heartbeat: {output.get('timestamp', 'n/a')}")
    lines.append("")
    lines.append(f"Kraken:           {dq.get('spot_ticker', 'UNKNOWN')}")
    lines.append(f"Kraken Futures:   {dq.get('futures_ticker', 'UNKNOWN')}")
    lines.append("SQLite:           OK")
    lines.append(f"Qwen:             {dq.get('qwen', 'SKIPPED')}")
    lines.append(f"Claude Bridge:    {bridge_health or 'UNKNOWN'}")
    lines.append(f"Mobile notifications: {'ENABLED' if mobile_notifications_enabled else 'DISABLED'}")
    lines.append(SEP)
    lines.append("")
    lines.append("FUNNEL")
    lines.append(SEP)
    lines.append(f"Assets scanned:      {output.get('universe', {}).get('assets_eligible', 0)}")
    lines.append(f"L1 anomalies:        {funnel.get('L1_shortlist', 0)}")
    lines.append(f"L2 opportunities:    {funnel.get('L2_candidates', 0)}")
    lines.append(f"L3 finalists:        {funnel.get('L3_finalists', 0)}")
    lines.append(f"Qwen reviews:        {funnel.get('qwen_reviewed', 0)}")
    lines.append(f"Sonnet calls:        {funnel.get('sonnet_demand', 0)}")
    lines.append(f"Fable calls:         {funnel.get('fable_demand', 0)}")
    lines.append(f"Ignored:             {funnel.get('ignored', 0)}")
    lines.append(SEP)
    lines.append("")
    lines.append("EVENT QUEUE")
    lines.append(SEP)
    counts = event_queue_counts or {}
    lines.append(f"Pending:             {counts.get('PENDING', 0)}")
    lines.append(f"Processing:          {counts.get('PROCESSING', 0)}")
    lines.append(f"Processed:           {counts.get('PROCESSED', 0)}")
    lines.append(f"Deferred:            {counts.get('DEFERRED', 0)}")
    lines.append(f"Failed:              {counts.get('FAILED', 0)}")
    lines.append(SEP)
    lines.append("")
    lines.append("ULTIMO EVENTO")
    lines.append(SEP)
    if last_event:
        lines.append(f"Asset:        {last_event.get('asset', 'n/a')}")
        lines.append(f"Opportunity:  {_fmt_score(last_event.get('opportunity_score'))}")
        lines.append(f"Tradeability: {_fmt_score(last_event.get('tradeability_score'))}")
        lines.append(f"Model:        {last_event.get('model_demand', 'n/a')}")
        lines.append(f"Status:       {last_event.get('status', 'n/a')}")
        lines.append(f"Event:        {last_event.get('event_id', 'n/a')}")
        ntfy_status = last_event.get('ntfy_status')
        if ntfy_status:
            lines.append(f"ntfy:         {'SENT ✅' if ntfy_status == 'SENT' else ntfy_status}")
        lines.append(f"Timestamp:    {last_event.get('updated_ts') or last_event.get('ts', 'n/a')}")
    else:
        lines.append("(no events yet)")
    lines.append(SEP)
    lines.append("")
    lines.append("ALERTS")
    lines.append("")

    alerts = [c for c in candidates if c.get("model_demand") in ("SONNET", "FABLE")]
    if not alerts:
        lines.append("No new actionable opportunities.")
        lines.append("")
        lines.append(f"Qwen: {'SKIPPED' if funnel.get('qwen_reviewed', 0) == 0 else dq.get('qwen', 'OK')}")
        lines.append("Claude: NO EVENT")
    else:
        ts = output.get("timestamp", "")
        time_part = ts[11:19] if len(ts) >= 19 else ts
        for c in alerts:
            setup = c.get("setup", {})
            qwen = c.get("qwen") or {}
            qwen_summary = "SKIPPED" if not qwen else ("CALL_FABLE" if qwen.get("call_fable") else ("CALL_SONNET" if qwen.get("call_sonnet") else "REVIEWED"))
            lines.append(f"[{time_part}]")
            lines.append(f"{c.get('asset')}")
            lines.append(f"Qwen -> {qwen_summary}")
            lines.append(f"Demand Router -> {c.get('model_demand')}")
            lines.append(f"{setup.get('type', 'NONE')} / {setup.get('direction', 'NONE')}")
            lines.append(f"Opportunity: {_fmt_score(c.get('opportunity_score'))}")
            lines.append(f"Tradeability: {_fmt_score(c.get('tradeability_score'))}")
            event_status = c.get("event_status")
            if event_status:
                lines.append(f"Claude Bridge -> {event_status}")
            lines.append("")

    lines.append(BAR)
    return "\n".join(lines)
