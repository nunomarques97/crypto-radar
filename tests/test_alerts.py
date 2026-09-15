import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import alerts, config, mock_alert, notifications, ntfy
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore


class _StoreTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)
        self._original_events_log_path = config.EVENTS_LOG_PATH
        self.events_log = self.path + ".events.jsonl"
        config.EVENTS_LOG_PATH = self.events_log

    def tearDown(self):
        self.store.close()
        config.EVENTS_LOG_PATH = self._original_events_log_path
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)
        if os.path.exists(self.events_log):
            os.remove(self.events_log)

    def _make_real_event(self, **overrides):
        base = dict(
            ts="2026-09-14T00:14:22+00:00", type_="RADAR_ALERT", asset="PEPE",
            setup_type="BREAKOUT", direction="LONG", market="FUTURES",
            anomaly_score=91.0, opportunity_score=82.0, tradeability_score=88.0,
            confidence="HIGH", model_demand="FABLE", reason="opportunity>=70", status="PROCESSED",
        )
        base.update(overrides)
        event_id, _created = create_event_if_new(self.store, **base)
        return event_id


class TestListAlerts(_StoreTestCase):
    def test_1_lists_relevant_alerts(self):
        self._make_real_event(asset="PEPE")
        self._make_real_event(asset="DOGE", setup_type="CONTINUATION", ts="2026-09-14T00:19:47+00:00")
        rows = alerts.list_alerts(self.store)
        self.assertEqual(len(rows), 2)

    def test_ignore_demand_events_are_excluded(self):
        self._make_real_event(model_demand="IGNORE", setup_type="NONE", direction="NONE")
        rows = alerts.list_alerts(self.store)
        self.assertEqual(len(rows), 0)

    def test_2_ordered_most_recent_first(self):
        self._make_real_event(asset="BTC", ts="2026-09-14T00:26:03+00:00", setup_type="CONTINUATION")
        self._make_real_event(asset="PEPE", ts="2026-09-14T00:14:22+00:00")
        self._make_real_event(asset="DOGE", ts="2026-09-14T00:19:47+00:00", setup_type="CONTINUATION")
        rows = alerts.list_alerts(self.store)
        self.assertEqual([row["asset"] for row in rows], ["BTC", "DOGE", "PEPE"])

    def test_limit_is_respected(self):
        for i in range(5):
            self._make_real_event(asset=f"COIN{i}", ts=f"2026-09-14T00:{10 + i:02d}:00+00:00")
        rows = alerts.list_alerts(self.store, limit=2)
        self.assertEqual(len(rows), 2)


class TestFormatAlertHistory(_StoreTestCase):
    def test_format_contains_required_fields(self):
        event_id = self._make_real_event()
        rows = alerts.list_alerts(self.store)
        text = alerts.format_alert_history(rows)
        self.assertIn("ALERT HISTORY", text)
        self.assertIn("[1]", text)
        self.assertIn("00:14:22", text)
        self.assertIn("PEPE", text)
        self.assertIn("BREAKOUT / LONG", text)
        self.assertIn("Opportunity: 82", text)
        self.assertIn("Tradeability: 88", text)
        self.assertIn("Model: FABLE", text)
        self.assertIn(f"Event: {event_id}", text)
        self.assertIn("ntfy:", text)

    def test_empty_history_does_not_crash(self):
        text = alerts.format_alert_history([])
        self.assertIn("ALERT HISTORY", text)
        self.assertIn("no alerts", text.lower())

    def test_does_not_dump_full_event_context(self):
        event = mock_alert.build_mock_context()
        event_id = self._make_real_event(context=event)
        rows = alerts.list_alerts(self.store)
        text = alerts.format_alert_history(rows)
        # "just enough to identify" - the full L1/L2/L3 JSON block must NOT appear here
        self.assertNotIn("tradeability_breakdown", text)
        self.assertNotIn("l2_features", text)
        self.assertIn(event_id, text)


class Test8And10MockVsRealDistinction(_StoreTestCase):
    def test_8_mock_event_appears_in_history(self):
        mock_event_id = mock_alert.create_mock_event(self.store)
        rows = alerts.list_alerts(self.store)
        event_ids = [row["event_id"] for row in rows]
        self.assertIn(mock_event_id, event_ids)

    def test_10_mock_and_real_are_distinguished(self):
        real_id = self._make_real_event(asset="PEPE")
        mock_id = mock_alert.create_mock_event(self.store)
        rows = alerts.list_alerts(self.store)
        by_id = {row["event_id"]: row for row in rows}
        self.assertFalse(alerts.is_mock_alert(by_id[real_id]))
        self.assertTrue(alerts.is_mock_alert(by_id[mock_id]))

        text = alerts.format_alert_history(rows)
        entries = text.split("\n\n")
        real_block = next(e for e in entries if f"Event: {real_id}" in e)
        mock_block = next(e for e in entries if f"Event: {mock_id}" in e)
        self.assertNotIn("[TESTE]", real_block)
        self.assertIn("[TESTE]", mock_block)


class TestRecoverPrompt(_StoreTestCase):
    def test_3_recovers_by_event_id(self):
        event_id = self._make_real_event()
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as clip_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True) as popup_fn:
            buf = io.StringIO()
            with redirect_stdout(buf):
                found = alerts.recover_prompt(self.store, event_id)
        self.assertTrue(found)
        clip_fn.assert_called_once()
        popup_fn.assert_called_once()
        output = buf.getvalue()
        self.assertIn("Prompt copied to clipboard ✅", output)
        self.assertIn(f"Event: {event_id}", output)

    def test_4_unknown_event_id_reports_not_found_without_crashing(self):
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard") as clip_fn:
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    found = alerts.recover_prompt(self.store, "does-not-exist")
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"recover_prompt raised {exc!r} instead of reporting not-found")
        self.assertFalse(found)
        self.assertIn("Event not found", buf.getvalue())
        clip_fn.assert_not_called()

    def test_5_reconstructed_prompt_is_event_specific(self):
        event_id = self._make_real_event(asset="DOGE", setup_type="CONTINUATION")
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as clip_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
            alerts.recover_prompt(self.store, event_id)
        copied_text = clip_fn.call_args[0][0]
        self.assertIn(event_id, copied_text)
        self.assertIn("DOGE", copied_text)

    def test_6_clipboard_is_the_existing_clipboard_module(self):
        event_id = self._make_real_event()
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as clip_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
            alerts.recover_prompt(self.store, event_id)
        clip_fn.assert_called_once()

    def test_7_popup_is_the_existing_popup_module(self):
        event_id = self._make_real_event()
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True), \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True) as popup_fn:
            alerts.recover_prompt(self.store, event_id)
        popup_fn.assert_called_once()
        self.assertEqual(popup_fn.call_args[0][0], event_id)

    def test_8_recovers_a_mock_event_too(self):
        mock_event_id = mock_alert.create_mock_event(self.store)
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as clip_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
            found = alerts.recover_prompt(self.store, mock_event_id)
        self.assertTrue(found)
        copied_text = clip_fn.call_args[0][0]
        self.assertIn(mock_event_id, copied_text)
        self.assertIn("test_event", copied_text)

    def test_9_no_credentials_in_recovered_prompt(self):
        event_id = self._make_real_event()
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as clip_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
            alerts.recover_prompt(self.store, event_id)
        copied_text = clip_fn.call_args[0][0]
        for marker in ("KRAKEN_API_KEY", "KRAKEN_SECRET", "api_key", "secret", "password", "Authorization", "cookie"):
            self.assertNotIn(marker, copied_text)

    def test_no_second_prompt_is_persisted(self):
        """Section 3: the event/SQLite stays the only source of data - there
        is no new table/column storing a rendered prompt string."""
        event_id = self._make_real_event()
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True), \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
            alerts.recover_prompt(self.store, event_id)
            alerts.recover_prompt(self.store, event_id)
        row = self.store.get_event(event_id)
        self.assertNotIn("prompt_text", row.keys())
        self.assertNotIn("rendered_prompt", row.keys())


class TestCliIntegration(_StoreTestCase):
    def setUp(self):
        super().setUp()
        self._original_sqlite_path = config.SQLITE_PATH
        config.SQLITE_PATH = self.path

    def tearDown(self):
        config.SQLITE_PATH = self._original_sqlite_path
        super().tearDown()

    def test_alerts_mode_prints_history_non_interactively(self):
        self._make_real_event()
        from radar_v08 import cli
        with mock.patch("sys.stdin.isatty", return_value=False):
            buf = io.StringIO()
            with redirect_stdout(buf):
                exit_code = cli.run_mode("alerts", argv=[])
        self.assertEqual(exit_code, 0)
        self.assertIn("ALERT HISTORY", buf.getvalue())

    def test_prompt_mode_without_event_flag_prints_usage(self):
        from radar_v08 import cli
        exit_code = cli.run_mode("prompt", argv=[])
        self.assertEqual(exit_code, 2)

    def test_prompt_mode_recovers_by_event_id(self):
        event_id = self._make_real_event()
        from radar_v08 import cli
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True), \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                exit_code = cli.run_mode("prompt", argv=["--event", event_id])
        self.assertEqual(exit_code, 0)
        self.assertIn("Prompt copied to clipboard ✅", buf.getvalue())
        self.assertIn(f"Event: {event_id}", buf.getvalue())

    def test_prompt_mode_unknown_event_returns_error_exit_code(self):
        from radar_v08 import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli.run_mode("prompt", argv=["--event", "nope"])
        self.assertEqual(exit_code, 1)
        self.assertIn("Event not found", buf.getvalue())

    def test_interactive_picker_copies_the_chosen_alert(self):
        event_id = self._make_real_event()
        from radar_v08 import cli
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="1"), \
             mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as clip_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.run_mode("alerts", argv=[])
        clip_fn.assert_called_once()
        self.assertIn(event_id, clip_fn.call_args[0][0])

    def test_interactive_picker_empty_input_does_not_copy_anything(self):
        self._make_real_event()
        from radar_v08 import cli
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch.object(notifications.clipboard, "copy_text_to_clipboard") as clip_fn:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.run_mode("alerts", argv=[])
        clip_fn.assert_not_called()

    def test_no_kraken_qwen_or_claude_touched_by_alerts_mode(self):
        self._make_real_event()
        from radar_v08 import cli
        with mock.patch("sys.stdin.isatty", return_value=False), \
             mock.patch("radar_v08.kraken_spot.fetch_ticker") as kraken_spot_call, \
             mock.patch("radar_v08.kraken_futures.fetch_tickers") as kraken_futures_call, \
             mock.patch("radar_v08.qwen.review_finalists") as qwen_call, \
             mock.patch("radar_v08.claude_bridge.call_model") as claude_call:
            cli.run_mode("alerts", argv=[])
        kraken_spot_call.assert_not_called()
        kraken_futures_call.assert_not_called()
        qwen_call.assert_not_called()
        claude_call.assert_not_called()


class TestMockAlertRecoveryEndToEnd(_StoreTestCase):
    """TESTE FINAL (task section 9): the mock event created by --mode
    mock-alert is visible in --mode alerts and recoverable by --mode prompt.
    """

    def test_mock_event_listed_then_recovered(self):
        with mock.patch.object(notifications, "send_windows_notification", return_value=True), \
             mock.patch.object(ntfy, "send_ntfy_notification", return_value="DISABLED"), \
             mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True), \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = mock_alert.run_mock_alert(self.store)
        mock_event_id = result["event_id"]

        rows = alerts.list_alerts(self.store)
        self.assertIn(mock_event_id, [row["event_id"] for row in rows])

        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as clip_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True) as popup_fn, \
             mock.patch("radar_v08.kraken_spot.fetch_ticker") as kraken_call, \
             mock.patch("radar_v08.qwen.review_finalists") as qwen_call:
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                found = alerts.recover_prompt(self.store, mock_event_id)

        self.assertTrue(found)
        clip_fn.assert_called_once()
        popup_fn.assert_called_once()
        kraken_call.assert_not_called()
        qwen_call.assert_not_called()
        self.assertIn("Prompt copied to clipboard ✅", buf2.getvalue())
        self.assertIn(f"Event: {mock_event_id}", buf2.getvalue())


if __name__ == "__main__":
    unittest.main()
