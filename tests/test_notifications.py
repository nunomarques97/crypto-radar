import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import notifications, ntfy
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore


class TestNotificationLevel(unittest.TestCase):
    def test_sonnet_is_medium(self):
        self.assertEqual(notifications.notification_level_for_model("SONNET"), "MEDIUM")

    def test_fable_is_high(self):
        self.assertEqual(notifications.notification_level_for_model("FABLE"), "HIGH")

    def test_unknown_model_defaults_low(self):
        self.assertEqual(notifications.notification_level_for_model("IGNORE"), "LOW")


class TestBuildNotificationText(unittest.TestCase):
    def test_text_is_short_and_excludes_full_analysis(self):
        title, message = notifications.build_notification_text({
            "asset": "DOGE", "setup_type": "BREAKOUT", "direction": "LONG", "model": "FABLE",
            "opportunity_score": 82.4, "tradeability_score": 87.1, "recommendation": "STRONG_OPPORTUNITY",
        })
        self.assertIn("DOGE", title)
        self.assertIn("BREAKOUT", title)
        self.assertLess(len(title), 100)
        self.assertIn("82", message)
        self.assertIn("87", message)
        self.assertLess(len(message), 200)


class TestNotifyForEvent(unittest.TestCase):
    def test_low_level_sends_nothing(self):
        with mock.patch.object(notifications, "send_windows_notification") as win_sender, \
             mock.patch.object(notifications.ntfy, "send_ntfy_notification") as ntfy_sender:
            result = notifications.notify_for_event({"asset": "BTC", "model": "IGNORE"})
        win_sender.assert_not_called()
        ntfy_sender.assert_not_called()
        self.assertEqual(result["level"], "LOW")
        self.assertFalse(result["windows_sent"])
        self.assertEqual(result["ntfy_result"], notifications.ntfy.RESULT_DISABLED)

    def test_sonnet_sends_without_sound(self):
        with mock.patch.object(notifications, "send_windows_notification", return_value=True) as sender, \
             mock.patch.object(notifications.ntfy, "send_ntfy_notification", return_value="SUCCESS"):
            notifications.notify_for_event({"asset": "BTC", "model": "SONNET", "setup_type": "BREAKOUT", "direction": "LONG"})
        sender.assert_called_once()
        self.assertFalse(sender.call_args.kwargs.get("sound"))

    def test_fable_sends_with_sound(self):
        with mock.patch.object(notifications, "send_windows_notification", return_value=True) as sender, \
             mock.patch.object(notifications.ntfy, "send_ntfy_notification", return_value="SUCCESS"):
            notifications.notify_for_event({"asset": "BTC", "model": "FABLE", "setup_type": "BREAKOUT", "direction": "LONG"})
        sender.assert_called_once()
        self.assertTrue(sender.call_args.kwargs.get("sound"))

    def test_windows_failure_does_not_block_ntfy(self):
        with mock.patch.object(notifications, "send_windows_notification", return_value=False), \
             mock.patch.object(notifications.ntfy, "send_ntfy_notification", return_value="SUCCESS") as ntfy_sender:
            result = notifications.notify_for_event({"asset": "BTC", "model": "FABLE", "setup_type": "BREAKOUT", "direction": "LONG"})
        ntfy_sender.assert_called_once()
        self.assertEqual(result["ntfy_result"], "SUCCESS")


class TestBuildMobileNotificationText(unittest.TestCase):
    def test_text_matches_short_mobile_format(self):
        title, message = notifications.build_mobile_notification_text({
            "asset": "PEPE", "setup_type": "BREAKOUT", "direction": "LONG", "model": "FABLE",
            "opportunity_score": 82, "tradeability_score": 88,
        })
        self.assertIn("PEPE", title)
        self.assertLess(len(title), 40)
        self.assertEqual(message, "BREAKOUT LONG | Opp: 82 | Trade: 88 | Modelo: FABLE")
        self.assertLess(len(message), 100)


class _StoreTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)
        from radar_v08 import config
        self._original_events_log_path = config.EVENTS_LOG_PATH
        self.events_log = self.path + ".events.jsonl"
        config.EVENTS_LOG_PATH = self.events_log

    def tearDown(self):
        from radar_v08 import config
        self.store.close()
        config.EVENTS_LOG_PATH = self._original_events_log_path
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)
        if os.path.exists(self.events_log):
            os.remove(self.events_log)

    def _make_event_row(self, **overrides):
        base = dict(
            ts="2026-09-13T12:00:00+00:00", type_="RADAR_ALERT", asset="PEPE",
            setup_type="BREAKOUT", direction="LONG", market="SPOT",
            anomaly_score=70.0, opportunity_score=82.0, tradeability_score=88.0,
            confidence="HIGH", model_demand="FABLE", reason="test", status="PROCESSED",
        )
        base.update(overrides)
        event_id, _created = create_event_if_new(self.store, **base)
        return event_id


class TestSendMobileNotificationDedup(_StoreTestCase):
    def test_low_never_sends(self):
        event_id = self._make_event_row()
        with mock.patch.object(ntfy, "send_ntfy_notification") as sender:
            result = notifications.send_mobile_notification({"event_id": event_id}, "LOW", store=self.store)
        sender.assert_not_called()
        self.assertEqual(result, ntfy.RESULT_DISABLED)

    def test_high_uses_high_priority(self):
        event_id = self._make_event_row()
        with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_SUCCESS) as sender:
            notifications.send_mobile_notification({"event_id": event_id, "asset": "PEPE", "model": "FABLE"}, "HIGH", store=self.store)
        self.assertEqual(sender.call_args.kwargs.get("priority"), "high")

    def test_medium_uses_default_priority(self):
        event_id = self._make_event_row(model_demand="SONNET")
        with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_SUCCESS) as sender:
            notifications.send_mobile_notification({"event_id": event_id, "asset": "PEPE", "model": "SONNET"}, "MEDIUM", store=self.store)
        self.assertEqual(sender.call_args.kwargs.get("priority"), "default")

    def test_successful_send_persists_sent_state(self):
        event_id = self._make_event_row()
        with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_SUCCESS):
            notifications.send_mobile_notification({"event_id": event_id, "asset": "PEPE", "model": "FABLE"}, "HIGH", store=self.store)
        row = self.store.get_event(event_id)
        self.assertEqual(row["ntfy_status"], "SENT")

    def test_failed_send_persists_failed_state_without_touching_event_status(self):
        event_id = self._make_event_row()
        with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_FAILED):
            notifications.send_mobile_notification({"event_id": event_id, "asset": "PEPE", "model": "FABLE"}, "HIGH", store=self.store)
        row = self.store.get_event(event_id)
        self.assertEqual(row["ntfy_status"], "FAILED")
        self.assertEqual(row["ntfy_attempts"], 1)
        self.assertEqual(row["status"], "PROCESSED")  # analysis status untouched

    def test_same_event_id_never_sends_twice(self):
        event_id = self._make_event_row()
        with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_SUCCESS) as sender:
            notifications.send_mobile_notification({"event_id": event_id, "asset": "PEPE", "model": "FABLE"}, "HIGH", store=self.store)
            # A rerun/reexecution for the same event_id must not fire a second push.
            notifications.send_mobile_notification({"event_id": event_id, "asset": "PEPE", "model": "FABLE"}, "HIGH", store=self.store)
        sender.assert_called_once()


class TestRetryPendingNtfy(_StoreTestCase):
    def test_retries_failed_event_and_succeeds(self):
        event_id = self._make_event_row()
        self.store.set_ntfy_status(event_id, "FAILED", "2020-01-01T00:00:00+00:00", error="boom")

        from radar_v08 import config
        original_topic = config.NTFY_TOPIC
        config.NTFY_TOPIC = "unit-test-topic"
        try:
            with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_SUCCESS) as sender:
                results = notifications.retry_pending_ntfy(self.store)
        finally:
            config.NTFY_TOPIC = original_topic

        sender.assert_called_once()
        self.assertEqual(results[0]["event_id"], event_id)
        row = self.store.get_event(event_id)
        self.assertEqual(row["ntfy_status"], "SENT")

    def test_no_candidates_when_ntfy_not_configured(self):
        from radar_v08 import config
        original_topic = config.NTFY_TOPIC
        config.NTFY_TOPIC = None
        try:
            with mock.patch.object(ntfy, "send_ntfy_notification") as sender:
                results = notifications.retry_pending_ntfy(self.store)
        finally:
            config.NTFY_TOPIC = original_topic
        sender.assert_not_called()
        self.assertEqual(results, [])

    def test_exhausted_retry_budget_is_not_retried_again(self):
        event_id = self._make_event_row()
        self.store.set_ntfy_status(event_id, "FAILED", "2020-01-01T00:00:00+00:00")
        from radar_v08 import config
        for _ in range(config.NTFY_MAX_RETRY_ATTEMPTS - 1):
            self.store.set_ntfy_status(event_id, "FAILED", "2020-01-01T00:00:00+00:00")

        original_topic = config.NTFY_TOPIC
        config.NTFY_TOPIC = "unit-test-topic"
        try:
            with mock.patch.object(ntfy, "send_ntfy_notification") as sender:
                notifications.retry_pending_ntfy(self.store)
        finally:
            config.NTFY_TOPIC = original_topic
        sender.assert_not_called()


class TestSendWindowsNotificationSafety(unittest.TestCase):
    def test_disabled_flag_skips_subprocess(self):
        original = notifications.config.NOTIFICATIONS_ENABLED
        notifications.config.NOTIFICATIONS_ENABLED = False
        try:
            with mock.patch("subprocess.run") as run:
                result = notifications.send_windows_notification("title", "message")
            run.assert_not_called()
            self.assertFalse(result)
        finally:
            notifications.config.NOTIFICATIONS_ENABLED = original

    def test_subprocess_failure_returns_false_without_raising(self):
        with mock.patch("subprocess.run", side_effect=OSError("no powershell")):
            result = notifications.send_windows_notification("title", "message")
        self.assertFalse(result)

    def test_nonzero_exit_returns_false(self):
        fake_result = mock.Mock(returncode=1, stderr="boom")
        with mock.patch("subprocess.run", return_value=fake_result):
            result = notifications.send_windows_notification("title", "message")
        self.assertFalse(result)

    def test_success_returns_true_and_encodes_text_as_base64(self):
        fake_result = mock.Mock(returncode=0, stderr="")
        with mock.patch("subprocess.run", return_value=fake_result) as run:
            result = notifications.send_windows_notification("DOGE alert", "Opportunity: 80", sound=True)
        self.assertTrue(result)
        args = run.call_args[0][0]
        self.assertIn("-TitleB64", args)
        self.assertIn("-Sound", args)
        self.assertIn("1", args)  # sound flag value


if __name__ == "__main__":
    unittest.main()
