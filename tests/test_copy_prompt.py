import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config, notifications
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
        self._original_copy_prompt_enabled = config.COPY_PROMPT_ENABLED
        self._original_popup_enabled = config.COPY_PROMPT_POPUP_ENABLED

    def tearDown(self):
        self.store.close()
        config.EVENTS_LOG_PATH = self._original_events_log_path
        config.COPY_PROMPT_ENABLED = self._original_copy_prompt_enabled
        config.COPY_PROMPT_POPUP_ENABLED = self._original_popup_enabled
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)
        if os.path.exists(self.events_log):
            os.remove(self.events_log)

    def _make_event_row(self, **overrides):
        base = dict(
            ts="2026-09-14T00:42:15+00:00", type_="RADAR_ALERT", asset="PEPE",
            setup_type="BREAKOUT", direction="LONG", market="FUTURES",
            anomaly_score=70.0, opportunity_score=82.4, tradeability_score=88.1,
            confidence="HIGH", model_demand="FABLE", reason="opportunity>=70", status="PROCESSED",
        )
        base.update(overrides)
        event_id, _created = create_event_if_new(self.store, **base)
        return event_id


class TestCopyPromptForEvent(_StoreTestCase):
    def test_success_prints_confirmation_and_copies(self):
        event_id = self._make_event_row()
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as copy_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True) as popup_fn:
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = notifications.copy_prompt_for_event({"event_id": event_id, "asset": "PEPE"}, store=self.store)
        self.assertTrue(result["copied"])
        self.assertTrue(result["popup_opened"])
        copy_fn.assert_called_once()
        popup_fn.assert_called_once()
        output = buf.getvalue()
        self.assertIn("Prompt copied to clipboard ✅", output)
        self.assertIn(f"Event: {event_id}", output)
        # the prompt itself (large JSON block) must never be dumped to stdout by default
        self.assertLess(len(output), 500)

    def test_failure_does_not_print_success_lines(self):
        event_id = self._make_event_row()
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=False), \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=False):
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = notifications.copy_prompt_for_event({"event_id": event_id}, store=self.store)
        self.assertFalse(result["copied"])
        self.assertNotIn("copied", buf.getvalue())

    def test_disabled_via_config_skips_clipboard_and_popup(self):
        event_id = self._make_event_row()
        config.COPY_PROMPT_ENABLED = False
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard") as copy_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup") as popup_fn:
            result = notifications.copy_prompt_for_event({"event_id": event_id}, store=self.store)
        self.assertFalse(result["copied"])
        self.assertFalse(result["popup_opened"])
        copy_fn.assert_not_called()
        popup_fn.assert_not_called()

    def test_popup_disabled_still_copies_to_clipboard(self):
        event_id = self._make_event_row()
        config.COPY_PROMPT_POPUP_ENABLED = False
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as copy_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup") as popup_fn:
            result = notifications.copy_prompt_for_event({"event_id": event_id}, store=self.store)
        self.assertTrue(result["copied"])
        self.assertFalse(result["popup_opened"])
        copy_fn.assert_called_once()
        popup_fn.assert_not_called()

    def test_no_store_is_a_noop(self):
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard") as copy_fn:
            result = notifications.copy_prompt_for_event({"event_id": "whatever"}, store=None)
        self.assertFalse(result["copied"])
        copy_fn.assert_not_called()

    def test_missing_event_id_is_a_noop(self):
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard") as copy_fn:
            result = notifications.copy_prompt_for_event({"asset": "PEPE"}, store=self.store)
        self.assertFalse(result["copied"])
        copy_fn.assert_not_called()

    def test_unknown_event_id_is_a_noop(self):
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard") as copy_fn:
            result = notifications.copy_prompt_for_event({"event_id": "does-not-exist"}, store=self.store)
        self.assertFalse(result["copied"])
        copy_fn.assert_not_called()

    def test_prompt_passed_to_clipboard_is_event_specific(self):
        event_id = self._make_event_row(asset="PEPE")
        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as copy_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True):
            notifications.copy_prompt_for_event({"event_id": event_id}, store=self.store)
        copied_text = copy_fn.call_args[0][0]
        self.assertIn(event_id, copied_text)
        self.assertIn("PEPE", copied_text)


class TestNotifyForEventTriggersCopyPrompt(_StoreTestCase):
    def test_low_never_copies_prompt(self):
        event_id = self._make_event_row(model_demand="IGNORE")
        with mock.patch.object(notifications, "send_windows_notification"), \
             mock.patch.object(notifications, "copy_prompt_for_event") as copy_prompt:
            notifications.notify_for_event({"event_id": event_id, "model": "IGNORE"}, store=self.store)
        copy_prompt.assert_not_called()

    def test_high_triggers_copy_prompt(self):
        event_id = self._make_event_row(model_demand="FABLE")
        with mock.patch.object(notifications, "send_windows_notification", return_value=True), \
             mock.patch.object(notifications.ntfy, "send_ntfy_notification", return_value="DISABLED"), \
             mock.patch.object(
                 notifications, "copy_prompt_for_event",
                 return_value={"copied": True, "popup_opened": True},
             ) as copy_prompt:
            result = notifications.notify_for_event({"event_id": event_id, "model": "FABLE"}, store=self.store)
        copy_prompt.assert_called_once()
        self.assertTrue(result["prompt_copied"])
        self.assertTrue(result["popup_opened"])


class TestSyntheticTestEventEndToEnd(_StoreTestCase):
    """TESTE REAL: a synthetic event explicitly marked
    TEST_EVENT=true never touches Kraken/Qwen/Claude and still produces a
    correctly-copied, event-specific prompt with a terminal confirmation.
    """

    def test_synthetic_test_event_flow(self):
        event_id = self._make_event_row(
            type_="TEST_EVENT", asset="TESTCOIN", reason="TEST_EVENT=true; synthetic acceptance test",
            model_demand="FABLE",
        )
        row = self.store.get_event(event_id)
        self.assertIsNotNone(row)  # 1. evento existe
        self.assertEqual(row["type"], "TEST_EVENT")

        with mock.patch.object(notifications.clipboard, "copy_text_to_clipboard", return_value=True) as copy_fn, \
             mock.patch.object(notifications.prompt_popup, "launch_copy_prompt_popup", return_value=True), \
             mock.patch("radar_v08.claude_bridge._anthropic_available") as anthropic_check, \
             mock.patch("radar_v08.qwen.review_finalists") as qwen_check:
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = notifications.copy_prompt_for_event({"event_id": event_id, "asset": "TESTCOIN"}, store=self.store)

        self.assertTrue(result["copied"])  # 3. prompt copiado
        copy_fn.assert_called_once()
        prompt_text = copy_fn.call_args[0][0]
        self.assertIn(event_id, prompt_text)  # 2. prompt gerado, específico deste evento
        self.assertIn("TESTCOIN", prompt_text)
        self.assertIn("TEST_EVENT=true", prompt_text)

        output = buf.getvalue()
        self.assertIn("Prompt copied to clipboard ✅", output)  # 4. terminal confirma
        self.assertIn(f"Event: {event_id}", output)

        anthropic_check.assert_not_called()  # 6. nenhum Qwen/7. Claude real necessário
        qwen_check.assert_not_called()
        # 5/8. nenhuma chamada privada à Kraken / nenhuma operação de trading:
        # this module never imports kraken_spot/kraken_futures at all.
        import radar_v08.notifications as notifications_module
        self.assertNotIn("kraken_spot", dir(notifications_module))
        self.assertNotIn("kraken_futures", dir(notifications_module))


if __name__ == "__main__":
    unittest.main()
