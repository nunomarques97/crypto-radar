"""Containment tests for browser-only TEST MODE and refused legacy routes."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from radar_v08.store import SnapshotStore
from ui.agents import collect_real_agent_communications
from ui.bridge import Api


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestModeIsolationTestCase(unittest.TestCase):
    def test_legacy_test_mode_routes_refuse_before_any_effectful_dependency(self):
        api_without_initialization = Api.__new__(Api)
        calls: list[str] = []

        def called(*_args, **_kwargs):
            calls.append("effect")
            raise AssertionError("a refused TEST MODE route reached an effect")

        routes = (
            api_without_initialization.run_mock_alert,
            api_without_initialization.run_test_mode_mock_alert,
            api_without_initialization.run_notify_test,
            api_without_initialization.test_clipboard,
            api_without_initialization.list_mock_alerts,
        )
        with patch("ui.bridge.mock_alert.run_mock_alert", called), \
             patch("ui.bridge._run_notify_test", called), \
             patch("ui.bridge.clipboard.copy_text_to_clipboard", called), \
             patch("radar_v08.notifications.send_windows_notification", called), \
             patch("radar_v08.prompt_popup.launch_copy_prompt_popup", called), \
             patch("radar_v08.qwen.review_finalists", called):
            for route in routes:
                result = route()
                self.assertEqual(result["status"], "REFUSED")
                self.assertFalse(result["ok"])
                self.assertIn("browser-local", result["reason"])
        self.assertEqual(calls, [])

    def test_refused_routes_leave_sentinel_db_output_and_history_byte_identical(self):
        api_without_initialization = Api.__new__(Api)
        with tempfile.TemporaryDirectory(prefix="crypto-radar-t011-") as temporary:
            root = Path(temporary)
            sentinels = {
                root / "sentinel.sqlite": b"sqlite sentinel: TEST MODE must not open this",
                root / "radar_v08_output.json": b'{"sentinel":"output"}\n',
                root / "events.jsonl": b'{"sentinel":"history"}\n',
            }
            for path, contents in sentinels.items():
                path.write_bytes(contents)
            before = {path: digest(path) for path in sentinels}

            for route in (
                api_without_initialization.run_mock_alert,
                api_without_initialization.run_test_mode_mock_alert,
                api_without_initialization.run_notify_test,
                api_without_initialization.test_clipboard,
                api_without_initialization.list_mock_alerts,
            ):
                self.assertEqual(route()["status"], "REFUSED")

            self.assertEqual({path: digest(path) for path in sentinels}, before)

    def test_real_production_communication_collection_stays_empty(self):
        with tempfile.TemporaryDirectory(prefix="crypto-radar-t011-communications-") as temporary:
            store = SnapshotStore(str(Path(temporary) / "state.sqlite"))
            try:
                self.assertEqual(collect_real_agent_communications(store), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
