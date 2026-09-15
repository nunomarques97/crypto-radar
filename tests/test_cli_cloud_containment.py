import os
import tempfile
import unittest
from unittest import mock

from radar_v08 import cli, config
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore


class CliCloudContainmentTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_sqlite_path = config.SQLITE_PATH
        self.original_events_log_path = config.EVENTS_LOG_PATH
        config.SQLITE_PATH = os.path.join(self.temporary_directory.name, "state.sqlite")
        config.EVENTS_LOG_PATH = os.path.join(self.temporary_directory.name, "events.jsonl")
        self.store = SnapshotStore(config.SQLITE_PATH)
        create_event_if_new(
            self.store,
            ts="2026-09-14T12:00:00+00:00", type_="RADAR_ALERT", asset="BTC",
            setup_type="BREAKOUT", direction="LONG", market="SPOT",
            anomaly_score=70.0, opportunity_score=80.0, tradeability_score=85.0,
            confidence="HIGH", model_demand="FABLE", reason="test", status="PENDING",
            context={"asset": "BTC", "market": "SPOT"},
        )

    def tearDown(self):
        self.store.close()
        config.SQLITE_PATH = self.original_sqlite_path
        config.EVENTS_LOG_PATH = self.original_events_log_path
        self.temporary_directory.cleanup()

    def _assert_event_is_untouched(self):
        row = self.store.latest_event()
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(self.store.get_model_analyses_for_event(row["event_id"]), [])

    def test_full_mode_drain_never_constructs_client_or_notifies(self):
        with mock.patch.object(cli, "run_and_write", return_value={"funnel": {}}), mock.patch.object(
            cli.claude_bridge, "_default_create", side_effect=AssertionError("cloud client must not run")
        ) as create, mock.patch.object(cli.notifications, "notify_for_event") as notify, mock.patch.object(
            cli.notifications, "retry_pending_ntfy"
        ) as retry, mock.patch.object(cli, "safe_print"):
            self.assertEqual(cli.run_mode("full"), 0)

        self.assertFalse(create.called)
        self.assertFalse(notify.called)
        self.assertFalse(retry.called)
        self._assert_event_is_untouched()

    def test_explicit_bridge_mode_never_constructs_client_or_notifies(self):
        with mock.patch.object(
            cli.claude_bridge, "_default_create", side_effect=AssertionError("cloud client must not run")
        ) as create, mock.patch.object(cli.notifications, "notify_for_event") as notify, mock.patch.object(
            cli.notifications, "retry_pending_ntfy"
        ) as retry, mock.patch("builtins.print"):
            self.assertEqual(cli.run_mode("bridge"), 0)

        self.assertFalse(create.called)
        self.assertFalse(notify.called)
        self.assertFalse(retry.called)
        self._assert_event_is_untouched()

    def test_loop_drain_never_constructs_client_or_notifies(self):
        with mock.patch.object(cli, "run_and_write", return_value={"funnel": {}}), mock.patch.object(
            cli.claude_bridge, "_default_create", side_effect=AssertionError("cloud client must not run")
        ) as create, mock.patch.object(cli.notifications, "notify_for_event") as notify, mock.patch.object(
            cli.notifications, "retry_pending_ntfy"
        ) as retry, mock.patch.object(cli, "safe_print"), mock.patch.object(
            cli.time, "monotonic", return_value=1.0
        ), mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.run_mode("loop"), 0)

        self.assertFalse(create.called)
        self.assertFalse(notify.called)
        self.assertFalse(retry.called)
        self._assert_event_is_untouched()


if __name__ == "__main__":
    unittest.main()
