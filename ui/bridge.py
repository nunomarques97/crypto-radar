"""The `window.pywebview.api` surface exposed to the JS shell.

Deliberately thin: every method here delegates to `data_reader.py`,
`process_manager.py`, or a radar_v08 function directly (`alerts.recover_prompt`,
`clipboard.copy_text_to_clipboard`, `mock_alert.run_mock_alert`, ...). No
method reimplements backend logic - the UI never decides workflow, it only
observes state and forwards a handful of control actions (section 7 of the
task: the UI is not the orchestrator).

Every returned dict must be JSON-serializable (pywebview marshals it to JS)
and must never include a secret (NTFY topic, API keys) - see `_serialize_*`
helpers, which project only the fields DESIGN.md allows onto
the wire.
"""

from __future__ import annotations

import contextlib
import io
import os
import threading
import time
from typing import Any

from radar_v08 import (
    alerts,
    clipboard,
    config,
    mock_alert,
    notifications,
)
from radar_v08.cli import _run_notify_test
from radar_v08.store import SnapshotStore
from ui import (
    paper_reader,
    paths,
    pilot_reader,
    process_manager,
    trend_reader,
    ui_state,
)
from ui.agents import (
    AGENT_REGISTRY,
    Agent,
    build_connections,
    collect_real_agent_communications,
)
from ui.data_reader import DataReader


def _serialize_agent(agent: Agent) -> dict[str, Any]:
    return {
        "id": agent.id, "name": agent.name, "model": agent.model, "role": agent.role,
        "status": agent.status, "current_event": agent.current_event,
        "last_activity": agent.last_activity, "events_processed": agent.events_processed,
        "last_error": agent.last_error,
    }


def _serialize_alert_row(row: Any, lifecycle: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "ts": row["ts"],
        "asset": row["asset"],
        "setup_type": row["setup_type"],
        "direction": row["direction"],
        "opportunity_score": row["opportunity_score"],
        "tradeability_score": row["tradeability_score"],
        "model_demand": row["model_demand"],
        "ntfy_status": row["ntfy_status"] if "ntfy_status" in row.keys() else None,
        "lifecycle": lifecycle,
    }


class Api:
    def __init__(self):
        paths.ensure_importable()
        self._store = SnapshotStore(config.SQLITE_PATH)
        self._reader = DataReader(self._store)
        self._process = process_manager.ProcessManager()
        self._ui_state = ui_state.load()
        self._lock = threading.Lock()
        # Static architecture data (who can hand off to whom) - never changes
        # at runtime, so it's computed once here rather than every tick().
        self._agent_connections = build_connections(AGENT_REGISTRY)
        # The paper game reads through its own read-only connection (mode=ro), never
        # through self._store, so opening the Game tab cannot write the radar database.
        self._paper = paper_reader.from_config()
        # The pilot shadow likewise: its own mode=ro connection, no control action.
        self._pilot = pilot_reader.from_config()
        # The trend paper books: read through trend_paper_store.read_ledger only (no lock, no
        # file created); the network is touched only by trend_paper_catch_up.
        self._trend = trend_reader.from_config()

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._store.close()

    # -- state (polled ~1s by app.js) -------------------------------------------

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            proc = self._process.snapshot()
            uptime = self._process.uptime_seconds()
            funnel = self._reader.funnel()
            agents = [_serialize_agent(a) for a in self._reader.agents()]
            counts = self._reader.event_status_counts()
            latest = self._reader.latest_event()
            preview_rows = self._reader.real_alerts(limit=3)
            preview = [
                _serialize_alert_row(row, self._reader.event_lifecycle(row))
                for row in preview_rows
            ]

            return {
                "process": {
                    "state": proc.state, "pid": proc.pid, "uptime_seconds": uptime,
                    "last_exit_code": proc.last_exit_code, "last_error": proc.last_error,
                },
                "system_status": {
                    "kraken": "OK" if funnel.get("assets_scanned") else "UNKNOWN",
                    "kraken_futures": self._reader.futures_status(),
                    "sqlite": "OK",  # this call itself proves the connection is alive
                    "qwen": self._reader.qwen_status(),
                    "ntfy_enabled": notifications.mobile_notifications_enabled(),
                    "claude_bridge": self._reader.claude_bridge_health(),
                },
                "funnel": funnel,
                "next_full_cycle_eta_seconds": self._reader.next_full_cycle_eta_seconds(),
                "agents": agents,
                "agent_connections": self._agent_connections,
                # Communication/handoff EVENTS (Phase 2/4 contract) - distinct
                # from the static topology above. Each entry is
                # {"id", "from", "to", "ts"} (+ optional "type"/"reason"), see
                # ui.agents.AgentCommunication. Derived (never fabricated) by
                # collect_real_agent_communications(), which today returns []
                # because no radar_v08 signal records a real from_agent/
                # to_agent handoff (see the Phase 4 audit in ui/agents.py's
                # docstring on that function). The frontend
                # (agent_room.js's processCommunications) is already built to
                # consume this the moment a real emitter is wired in there.
                "agent_communications": collect_real_agent_communications(self._store),
                "event_queue": counts,
                "latest_event": dict(latest) if latest is not None else None,
                "alerts_preview": preview,
                "log_lines": self._process.recent_log_lines(max_lines=20),
            }

    # -- paper game (polled by app.js only while the Game tab is visible) -----------

    def get_paper_state(self) -> dict[str, Any]:
        """The pretend wallet, its plays and the agents' recorded activity (read-only).

        Money is decimal strings in cents; see ui/paper_reader.py for every field."""
        return self._paper.read()

    def get_pilot_state(self) -> dict[str, Any]:
        """The pilot shadow's envelope, equity, limits, locks, kill switch, open position,
        last sizing and NO_TRADE reasons (read-only; polled only while the Game tab is
        visible). Money is decimal strings in cents; see ui/pilot_reader.py for every field."""
        return self._pilot.read()

    def get_trend_paper_state(self) -> dict[str, Any]:
        """The trend paper books (research only, pre-tax, no real orders), read-only; polled
        only while the Game tab is visible. Never raises; see ui/trend_reader.py for every field."""
        try:
            return self._trend.read()
        except Exception:
            return trend_reader.unavailable_payload()

    def trend_paper_catch_up(self) -> dict[str, Any]:
        """Run the existing trend paper catch-up now (public Binance klines only, paper books
        only): ``{ok, status, days_booked, detail}``. One at a time; the radar's ledger lock gives
        BUSY and nothing is written. The only Api method that writes the trend paper ledger."""
        return self._trend.catch_up()

    # -- process control ---------------------------------------------------------

    def start_radar(self) -> dict[str, Any]:
        state = self._process.start()
        return {"state": state.state, "pid": state.pid, "last_error": state.last_error}

    def stop_radar(self) -> dict[str, Any]:
        self._process.stop()
        return {"state": self._process.snapshot().state}

    def restart_radar(self) -> dict[str, Any]:
        self._process.restart()
        return {"state": self._process.snapshot().state}

    # -- alerts / history -----------------------------------------------------------

    def list_alerts(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._reader.real_alerts(limit=limit)
            return [_serialize_alert_row(row, self._reader.event_lifecycle(row)) for row in rows]

    def list_history(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.list_alerts(limit=limit)

    def copy_prompt(self, event_id: str) -> dict[str, Any]:
        """Reuses `alerts.recover_prompt` exactly - the same function
        `--mode prompt --event <id>` calls - never a second prompt/clipboard
        implementation.
        """
        with self._lock:
            found = alerts.recover_prompt(self._store, event_id)
        return {"copied": found}

    # -- operational diagnostics --------------------------------------------------
    # These legacy diagnostics are deliberately effectful and separate from
    # browser TEST MODE. Old Test Mode route names refuse below before touching
    # instance state, so stale or hidden controls cannot bypass this boundary.

    def list_operational_mock_alerts(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._reader.mock_alerts(limit=limit)
            return [_serialize_alert_row(row, self._reader.event_lifecycle(row)) for row in rows]

    def run_operational_notify_test(self) -> dict[str, Any]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = _run_notify_test()
        return {"exit_code": exit_code, "output": buf.getvalue()}

    def run_operational_mock_alert(self) -> dict[str, Any]:
        with self._lock:
            outcome = mock_alert.run_mock_alert(self._store)
        return {
            "event_id": outcome["event_id"],
            "report": mock_alert.format_mock_alert_report(outcome["event_id"], outcome["notify_result"]),
        }

    def run_operational_clipboard_test(self) -> dict[str, Any]:
        text = f"Crypto Radar UI clipboard test — {time.strftime('%Y-%m-%d %H:%M:%S')}"
        copied = clipboard.copy_text_to_clipboard(text)
        return {"copied": copied, "text": text}

    @staticmethod
    def _refused_test_mode_effect(action: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "REFUSED",
            "reason": f"TEST MODE is browser-local and visual-only; {action} is disabled.",
        }

    # Deprecated names were reachable from the old TEST MODE Mocks tab. Keep
    # their API boundary closed rather than relying on a removed/hidden button.
    def list_mock_alerts(self, limit: int | None = None) -> dict[str, Any]:
        return self._refused_test_mode_effect("mock-alert history access")

    def run_notify_test(self) -> dict[str, Any]:
        return self._refused_test_mode_effect("notification diagnostics")

    def run_mock_alert(self) -> dict[str, Any]:
        return self._refused_test_mode_effect("mock-alert diagnostics")

    def test_clipboard(self) -> dict[str, Any]:
        return self._refused_test_mode_effect("clipboard diagnostics")

    def run_test_mode_mock_alert(self) -> dict[str, Any]:
        """Explicitly refuse a guessed/new TEST MODE route as well."""
        return self._refused_test_mode_effect("mock-alert diagnostics")

    # -- system / logs ------------------------------------------------------------

    def system_info(self) -> dict[str, Any]:
        return {
            "sqlite_path": config.SQLITE_PATH,
            "log_path": config.TEXT_LOG_PATH,
            "repo_root": paths.repo_root(),
        }

    def open_log_file(self) -> dict[str, bool]:
        try:
            os.startfile(config.TEXT_LOG_PATH)  # noqa: S606 - local file, user-triggered, Windows-only
            return {"opened": True}
        except OSError:
            return {"opened": False}

    def open_project_folder(self) -> dict[str, bool]:
        try:
            os.startfile(paths.repo_root())  # noqa: S606
            return {"opened": True}
        except OSError:
            return {"opened": False}

    # -- ui-only local state ---------------------------------------------------

    def get_ui_state(self) -> dict[str, Any]:
        return self._ui_state

    def save_ui_state(self, patch: dict[str, Any]) -> dict[str, Any]:
        self._ui_state.update(patch)
        ui_state.save(self._ui_state)
        return self._ui_state
