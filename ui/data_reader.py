"""Read-only view over radar_v08's own state: SQLite (`SnapshotStore`) plus
the latest `radar_v08_output.json` snapshot. This is the ONLY place the UI
touches radar_v08 data directly - `bridge.py` calls into here, never into
`radar_v08.store`/`radar_v08.config` on its own, so there is exactly one
place that knows how "demand" and "calls" are kept apart.

Two independent cadences (see DESIGN.md / the plan): a ~1s "cheap tier" poll
of small SQLite aggregates, and a `radar_v08_output.json` mtime-gated re-read
of the (larger, overwritten-every-cycle) funnel/candidates snapshot. Both are
read on demand here; the actual timer/thread lives in `bridge.py` so this
module stays synchronous and trivially testable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from radar_v08 import alerts, budgets, config
from radar_v08.adapters.outbox_store import OutboxError
from radar_v08.claude_bridge import bridge_health_label
from radar_v08.store import SnapshotStore

from ui.agents import build_agents


class DataReader:
    def __init__(self, store: SnapshotStore):
        self.store = store
        self._output_mtime: float | None = None
        self._output_cache: dict[str, Any] | None = None

    # -- output.json (mtime-gated) -------------------------------------------

    def read_output_snapshot(self) -> dict[str, Any] | None:
        """Latest FULL/HEARTBEAT cycle snapshot - overwritten every cycle,
        never historical. Re-parses the file only when its mtime changed
        since the last read; returns the cached dict otherwise (or None if
        the file doesn't exist yet, e.g. before the first heartbeat).
        """
        path = config.OUTPUT_V08_PATH
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return self._output_cache  # keep last-known-good rather than flicker to None

        if mtime != self._output_mtime:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._output_cache = json.load(fh)
                self._output_mtime = mtime
            except (OSError, json.JSONDecodeError):
                # A cycle mid-write can race a read - keep last-known-good,
                # never surface a half-written file as if it were valid.
                return self._output_cache
        return self._output_cache

    # -- funnel: demand vs calls, kept explicitly separate -------------------

    def funnel(self) -> dict[str, Any]:
        """Every key read defensively via `.get()` - the funnel dict's key
        set is populated by heartbeat.py at call time, not fixed by schema
        (e.g. HEARTBEAT-mode cycles carry fewer keys than FULL-mode ones).
        """
        snapshot = self.read_output_snapshot() or {}
        raw_funnel = snapshot.get("funnel") or {}
        call_counts = self.store.model_analysis_call_counts()
        return {
            "assets_scanned": (snapshot.get("universe") or {}).get("pairs_seen"),
            "l1_shortlist": raw_funnel.get("L1_shortlist"),
            "l2_candidates": raw_funnel.get("L2_candidates"),
            "l3_finalists": raw_funnel.get("L3_finalists"),
            "qwen_reviewed": raw_funnel.get("qwen_reviewed"),
            "sonnet_demand": raw_funnel.get("sonnet_demand"),
            "sonnet_calls": call_counts.get(config.ANTHROPIC_SONNET_MODEL, 0),
            "fable_demand": raw_funnel.get("fable_demand"),
            "fable_calls": call_counts.get(config.ANTHROPIC_FABLE_MODEL, 0),
            "ignored": raw_funnel.get("ignored"),
            "deferred": raw_funnel.get("deferred"),
            "run_timestamp": snapshot.get("timestamp"),
            "warmup": snapshot.get("warmup"),
        }

    def next_full_cycle_eta_seconds(self) -> float | None:
        snapshot = self.read_output_snapshot()
        if not snapshot or not snapshot.get("timestamp"):
            return None
        try:
            last = datetime.fromisoformat(snapshot["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            return None
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return max(0.0, config.LOOP_FULL_INTERVAL_SECONDS - elapsed)

    # -- system status ---------------------------------------------------------

    def claude_bridge_health(self) -> str:
        return bridge_health_label(self.store)

    def qwen_status(self) -> str:
        """No live Ollama reachability check exists for the UI to call - the
        only honest signal is whether the latest cycle actually got a Qwen
        review on at least one candidate. Never fabricated to "OK".
        """
        snapshot = self.read_output_snapshot()
        if not snapshot:
            return "UNKNOWN"
        candidates = snapshot.get("candidates") or []
        if any(c.get("qwen") is not None for c in candidates):
            return "OK"
        data_quality_qwen = (snapshot.get("data_quality") or {}).get("qwen")
        return data_quality_qwen or "UNKNOWN"

    def futures_status(self) -> str:
        snapshot = self.read_output_snapshot() or {}
        return (snapshot.get("data_quality") or {}).get("futures_ticker") or "UNKNOWN"

    def budget_status(self, model: str) -> dict[str, int]:
        return budgets.budget_status(self.store, model, datetime.now(timezone.utc))

    # -- agents ------------------------------------------------------------

    def agents(self):
        return build_agents(self.store, self.read_output_snapshot())

    def lifecycle_state(self, item_id: str) -> str:
        """The real T033a lifecycle state for one work item - one of QUEUED,
        LOADING, RUNNING, FINISHED, FAILED, ABORT_STALE, SUPERSEDED or
        DROPPED_BACKPRESSURE - or "UNKNOWN" when no `lifecycle_items` row
        proves one yet. Never inferred from event/bridge status: only a real
        row recorded via `SnapshotStore.record_lifecycle_transition` can move
        this away from "UNKNOWN" (T033b). An id the outbox refuses (malformed)
        or a store that cannot answer (`OutboxError`) is also "UNKNOWN" - the
        reader never throws into the UI and never guesses.
        """
        try:
            state = self.store.lifecycle_state(item_id)
        except OutboxError:
            return "UNKNOWN"
        return state.value if state is not None else "UNKNOWN"

    # -- events / alerts -----------------------------------------------------

    def event_status_counts(self) -> dict[str, int]:
        return self.store.event_status_counts()

    def latest_event(self):
        return self.store.latest_event()

    def real_alerts(self, limit: int | None = None) -> list[Any]:
        rows = alerts.list_alerts(self.store, limit)
        return [row for row in rows if not alerts.is_mock_alert(row)]

    def mock_alerts(self, limit: int | None = None) -> list[Any]:
        rows = alerts.list_alerts(self.store, limit)
        return [row for row in rows if alerts.is_mock_alert(row)]

    def event_lifecycle(self, event_row: Any) -> dict[str, bool | str]:
        """Per-event lifecycle stepper, built only from fields that actually
        exist on the row - never a guessed stage. See DESIGN.md's rule: every
        pill must cite where its value came from.
        """
        has_qwen = False
        context_raw = event_row["context_json"] if "context_json" in event_row.keys() else None
        if context_raw:
            try:
                context = json.loads(context_raw)
                has_qwen = context.get("qwen") is not None
            except (json.JSONDecodeError, TypeError):
                has_qwen = False

        model_demand = event_row["model_demand"] if "model_demand" in event_row.keys() else None
        ntfy_status = event_row["ntfy_status"] if "ntfy_status" in event_row.keys() else None
        notified = bool(event_row["notified"]) if "notified" in event_row.keys() else False
        event_id = event_row["event_id"]

        return {
            "detected": True,  # the row exists - this is always true
            "qwen": has_qwen,
            "router": model_demand in ("SONNET", "FABLE"),
            "ntfy_status": ntfy_status or "N/A",
            "prompt_ready": notified,
            "claude_analysed": self.store.has_successful_analysis(event_id),
        }
