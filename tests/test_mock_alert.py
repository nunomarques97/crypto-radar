import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config, mock_alert, notifications, ntfy
from radar_v08.context_builder import UNAVAILABLE
from radar_v08.prompt_builder import build_prompt_text
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


class TestCreateMockEvent(_StoreTestCase):
    def test_1_mock_event_is_created_in_the_real_events_table(self):
        event_id = mock_alert.create_mock_event(self.store)
        row = self.store.get_event(event_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["asset"], "PEPE")
        self.assertEqual(row["status"], "PROCESSED")

    def test_3_event_id_starts_with_mock_prefix(self):
        event_id = mock_alert.create_mock_event(self.store)
        self.assertTrue(event_id.startswith("MOCK-"))

    def test_15_event_is_identifiable_as_synthetic(self):
        event_id = mock_alert.create_mock_event(self.store)
        row = self.store.get_event(event_id)
        self.assertEqual(row["type"], "MOCK_TEST_EVENT")
        self.assertIn(mock_alert.MOCK_MARKER, row["reason"])
        self.assertIn("test_event=true", row["reason"])

    def test_2_context_json_carries_the_test_event_flag(self):
        event_id = mock_alert.create_mock_event(self.store)
        row = self.store.get_event(event_id)
        prompt_context = build_prompt_text(row)
        self.assertIn('"test_event": true', prompt_context)
        self.assertIn(mock_alert.MOCK_MARKER, prompt_context)

    def test_14_event_persists_and_does_not_collide_with_real_queue_lookups(self):
        event_id = mock_alert.create_mock_event(self.store)
        # PROCESSED, not PENDING/DEFERRED - invisible to real-pipeline queries.
        self.assertEqual(self.store.event_status_counts()["PROCESSED"], 1)
        self.assertEqual(len(self.store.find_actionable_events(now_iso="2099-01-01T00:00:00+00:00", limit=10)), 0)
        dedup_key = mock_alert.MOCK_ASSET + ":" + mock_alert.MOCK_SETUP_TYPE + ":" + mock_alert.MOCK_DIRECTION + ":" + mock_alert.MOCK_MODEL_DEMAND
        self.assertIsNone(self.store.find_open_event_by_dedup(dedup_key))

    def test_two_runs_produce_two_distinct_events(self):
        event_id_1 = mock_alert.create_mock_event(self.store)
        event_id_2 = mock_alert.create_mock_event(self.store)
        self.assertNotEqual(event_id_1, event_id_2)


class TestPromptGenerationForMockEvent(_StoreTestCase):
    def test_6_prompt_contains_event_data_and_l1_l2_l3_and_qwen(self):
        event_id = mock_alert.create_mock_event(self.store)
        row = self.store.get_event(event_id)
        text = build_prompt_text(row)
        self.assertIn(event_id, text)
        self.assertIn("test_event", text)
        self.assertIn("return_15m", text)            # L1
        self.assertIn("breakout_state", text)          # L2
        self.assertIn("tradeability_breakdown", text)  # L3
        self.assertIn('"qwen"', text)
        self.assertIn("call_fable", text)
        self.assertIn("crypto-trading-system", text)
        self.assertIn("TRADING_STATE.md", text)
        self.assertIn("TRADING_HISTORY.md", text)
        self.assertIn("Consulta o estado LIVE da Kraken", text)
        self.assertIn("Não executar nenhuma ordem", text)

    def test_13_no_credentials_in_prompt(self):
        event_id = mock_alert.create_mock_event(self.store)
        row = self.store.get_event(event_id)
        text = build_prompt_text(row)
        for marker in ("KRAKEN_API_KEY", "KRAKEN_SECRET", "api_key", "secret", "password", "Authorization", "cookie"):
            self.assertNotIn(marker, text)


class TestRunMockAlertPipeline(_StoreTestCase):
    def _run_with_mocks(self, *, windows_ok=True, ntfy_result="SUCCESS", clipboard_ok=True, popup_ok=True):
        with mock.patch.object(notifications, "send_windows_notification", return_value=windows_ok) as win_fn, \
             mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy_result) as ntfy_fn, \
             mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=clipboard_ok) as clip_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=popup_ok) as popup_fn:
            buf = io.StringIO()
            with redirect_stdout(buf):
                outcome = mock_alert.run_mock_alert(self.store)
        return outcome, buf.getvalue(), win_fn, ntfy_fn, clip_fn, popup_fn

    def test_4_windows_notification_invoked_with_expected_title_and_body(self):
        outcome, _output, win_fn, *_ = self._run_with_mocks()
        title, body = win_fn.call_args[0][0], win_fn.call_args[0][1]
        self.assertEqual(title, "CRYPTO RADAR - PEPE - BREAKOUT LONG")
        self.assertIn("Opportunity: 82", body)
        self.assertIn("Tradeability: 88", body)
        self.assertTrue(outcome["notify_result"]["windows_sent"])

    def test_5_ntfy_notification_invoked_and_marked_as_test(self):
        outcome, _output, _win, ntfy_fn, *_ = self._run_with_mocks()
        ntfy_fn.assert_called_once()
        # build_mobile_notification_text via notify_for_event -> send_mobile_notification -> ntfy.send_ntfy_notification
        title = ntfy_fn.call_args[0][0]
        self.assertIn("[TESTE]", title)
        self.assertIn("PEPE", title)
        self.assertEqual(outcome["notify_result"]["ntfy_result"], "SUCCESS")

    def test_7_clipboard_copy_invoked_with_event_specific_prompt(self):
        outcome, _output, *_ , clip_fn, _popup_fn = self._run_with_mocks()
        copied_text = clip_fn.call_args[0][0]
        self.assertIn(outcome["event_id"], copied_text)
        self.assertIn("test_event", copied_text)
        self.assertTrue(outcome["notify_result"]["prompt_copied"])

    def test_8_popup_invoked(self):
        outcome, _output, *_, popup_fn = self._run_with_mocks()
        popup_fn.assert_called_once()
        args = popup_fn.call_args[0]
        self.assertEqual(args[0], outcome["event_id"])
        self.assertTrue(outcome["notify_result"]["popup_opened"])

    def test_terminal_report_matches_expected_shape(self):
        outcome, output, *_ = self._run_with_mocks()
        self.assertIn("CRYPTO RADAR", output)
        self.assertIn("MOCK TEST", output)
        self.assertIn(f"Event: {outcome['event_id']}", output)
        self.assertIn("Asset: PEPE", output)
        self.assertIn("Setup: BREAKOUT LONG", output)
        self.assertIn("Opportunity: 82", output)
        self.assertIn("Tradeability: 88", output)
        self.assertIn("Model: FABLE", output)
        self.assertIn("Windows notification: SENT", output)
        self.assertIn("NTFY: SENT", output)
        self.assertIn("Prompt: COPIED", output)
        self.assertIn("Popup: OPENED", output)
        self.assertIn("Nenhum acesso", output)
        self.assertIn("Kraken", output)
        self.assertIn("Nenhuma chamada Qwen", output)
        self.assertIn("Nenhuma chamada Claude", output)
        self.assertIn("Nenhuma opera", output)

    def test_ntfy_disabled_is_reported_without_being_treated_as_a_crash(self):
        outcome, output, *_ = self._run_with_mocks(ntfy_result="DISABLED")
        self.assertEqual(outcome["notify_result"]["ntfy_result"], "DISABLED")
        self.assertIn("NTFY: DISABLED", output)

    def test_9_10_11_no_kraken_qwen_or_claude_modules_are_touched(self):
        with mock.patch("radar_v08.kraken_spot.fetch_ticker") as kraken_spot_call, \
             mock.patch("radar_v08.kraken_futures.fetch_tickers") as kraken_futures_call, \
             mock.patch("radar_v08.qwen.review_finalists") as qwen_call, \
             mock.patch("radar_v08.claude_bridge.call_model") as claude_call, \
             mock.patch.object(notifications, "send_windows_notification", return_value=True), \
             mock.patch.object(ntfy, "send_ntfy_notification", return_value="DISABLED"), \
             mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True), \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
            mock_alert.run_mock_alert(self.store)
        kraken_spot_call.assert_not_called()
        kraken_futures_call.assert_not_called()
        qwen_call.assert_not_called()
        claude_call.assert_not_called()

    def test_12_no_trading_execution_module_is_imported(self):
        """mock_alert.py may talk ABOUT Kraken/Qwen/Claude in comments/
        docstrings (it explicitly explains it avoids them) but must never
        actually import any of those modules - checked via the AST, not a
        substring match, so prose mentions don't produce a false failure."""
        import ast

        import radar_v08.mock_alert as mock_alert_module

        with open(mock_alert_module.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=mock_alert_module.__file__)

        forbidden_modules = {"kraken_spot", "kraken_futures", "qwen", "claude_bridge"}
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name.split(".")[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module.split(".")[-1])
                imported_modules.update(alias.name for alias in node.names)

        self.assertEqual(imported_modules & forbidden_modules, set())


class TestMockAlertCliIntegration(_StoreTestCase):
    def test_cli_mode_runs_end_to_end_and_returns_success_exit_code(self):
        self._original_sqlite_path = config.SQLITE_PATH
        config.SQLITE_PATH = self.path
        try:
            from radar_v08 import cli
            with mock.patch.object(notifications, "send_windows_notification", return_value=True), \
                 mock.patch.object(ntfy, "send_ntfy_notification", return_value="DISABLED"), \
                 mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True), \
                 mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = cli.run_mode("mock-alert")
        finally:
            config.SQLITE_PATH = self._original_sqlite_path
        self.assertEqual(exit_code, 0)
        self.assertIn("MOCK TEST", buf.getvalue())

    def test_cli_mode_never_calls_the_real_heartbeat_or_bridge(self):
        self._original_sqlite_path = config.SQLITE_PATH
        config.SQLITE_PATH = self.path
        try:
            from radar_v08 import cli
            with mock.patch.object(notifications, "send_windows_notification", return_value=True), \
                 mock.patch.object(ntfy, "send_ntfy_notification", return_value="DISABLED"), \
                 mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True), \
                 mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True), \
                 mock.patch("radar_v08.cli.run_and_write") as heartbeat_fn, \
                 mock.patch("radar_v08.cli.claude_bridge.run_bridge_cycle") as bridge_fn:
                cli.run_mode("mock-alert")
        finally:
            config.SQLITE_PATH = self._original_sqlite_path
        heartbeat_fn.assert_not_called()
        bridge_fn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
