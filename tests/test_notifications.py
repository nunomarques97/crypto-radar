import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import notifications, ntfy
from radar_v08.adapters.outbox_store import OutboxKind
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


class TestNotificationBodyNeverCarriesRejectionOrError(unittest.TestCase):
    """T033b: a rejection/error message must never reach a notification body,
    even when the event dict handed to these builders happens to carry one
    (e.g. a caller reusing a row that also has `last_error`/`reason` set)."""

    _poison = dict(
        reason="invocation_identity_invalid: missing_instrument",
        last_error="budget_exhausted: FABLE_DAILY_CAP",
        error="ntfy_send_failed",
    )

    def test_windows_body_excludes_rejection_and_error_fields(self):
        event = {
            "asset": "DOGE", "setup_type": "BREAKOUT", "direction": "LONG", "model": "FABLE",
            "opportunity_score": 82.4, "tradeability_score": 87.1, "recommendation": "STRONG_OPPORTUNITY",
            **self._poison,
        }
        title, message = notifications.build_notification_text(event)
        for poisoned in self._poison.values():
            self.assertNotIn(poisoned, title)
            self.assertNotIn(poisoned, message)

    def test_mobile_body_excludes_rejection_and_error_fields(self):
        event = {
            "asset": "PEPE", "setup_type": "BREAKOUT", "direction": "LONG", "model": "FABLE",
            "opportunity_score": 82, "tradeability_score": 88,
            **self._poison,
        }
        title, message = notifications.build_mobile_notification_text(event)
        for poisoned in self._poison.values():
            self.assertNotIn(poisoned, title)
            self.assertNotIn(poisoned, message)


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


class TestStableNotificationId(unittest.TestCase):
    """Pure derivation (T033b): same delivery_id in, same id out, always."""

    def test_same_delivery_id_yields_the_same_notification_id(self):
        first = notifications.stable_notification_id("event:abc-1:2")
        second = notifications.stable_notification_id("event:abc-1:2")
        self.assertEqual(first, second)

    def test_different_delivery_ids_yield_different_notification_ids(self):
        one = notifications.stable_notification_id("event:abc-1:2")
        other = notifications.stable_notification_id("event:abc-1:3")
        self.assertNotEqual(one, other)

    def test_id_is_short_and_transport_safe(self):
        notification_id = notifications.stable_notification_id("event:some-event-id-with-lots-of-characters:42")
        self.assertLessEqual(len(notification_id), 16)
        notification_id.encode("latin-1")  # must never crash an HTTP header or a toast Tag


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


class TestNotificationIdForEvent(_StoreTestCase):
    """T033b: the stable id is derived from a real EVENT outbox row - never invented."""

    def test_none_without_a_store(self):
        self.assertIsNone(notifications.notification_id_for_event({"event_id": "e1"}, None))

    def test_none_without_an_event_id(self):
        self.assertIsNone(notifications.notification_id_for_event({}, self.store))

    def test_none_when_no_outbox_row_exists_for_the_event(self):
        # An event_id that was never created has no outbox row at all.
        self.assertIsNone(notifications.notification_id_for_event({"event_id": "ghost"}, self.store))

    def test_a_real_event_gets_a_stable_id_derived_from_its_outbox_row(self):
        event_id = self._make_event_row()
        entry = self.store.first_outbox_entry(OutboxKind.EVENT, event_id)
        self.assertIsNotNone(entry)
        expected = notifications.stable_notification_id(entry.delivery_id)
        self.assertEqual(notifications.notification_id_for_event({"event_id": event_id}, self.store), expected)

    def test_querying_the_same_event_twice_yields_the_same_id(self):
        event_id = self._make_event_row()
        first = notifications.notification_id_for_event({"event_id": event_id}, self.store)
        second = notifications.notification_id_for_event({"event_id": event_id}, self.store)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_the_id_does_not_move_when_later_writes_add_outbox_rows(self):
        # The reviewer's reproduction of attempt 1: the bridge calls
        # mark_event_notified right after the send, which appends a new EVENT
        # outbox row. The id must be the same before and after that row.
        event_id = self._make_event_row()
        before = notifications.notification_id_for_event({"event_id": event_id}, self.store)
        self.store.mark_event_notified(event_id, "2026-09-13T12:01:00+00:00")
        self.store.update_event_status(event_id, "PROCESSED")
        self.assertEqual(len(self.store.outbox_entries(kind=OutboxKind.EVENT)), 3)
        after = notifications.notification_id_for_event({"event_id": event_id}, self.store)
        self.assertIsNotNone(before)
        self.assertEqual(after, before)

    def test_a_store_that_cannot_answer_means_no_id_not_an_exception(self):
        # A malformed event_id is refused by the outbox (OutboxError); the id
        # degrades to None so the notification itself still goes out.
        self.assertIsNone(notifications.notification_id_for_event({"event_id": "bad id\n"}, self.store))


class TestResendAfterRestartUsesTheSameNotificationId(_StoreTestCase):
    """T033b's required test: a resend of the same event (the ntfy cross-cycle
    retry, which is entirely SQLite-driven and therefore survives a restart)
    carries the same notification id as the original attempt."""

    def test_mobile_retry_after_a_failed_send_reuses_the_original_id(self):
        event_id = self._make_event_row()

        with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_FAILED) as first_sender:
            notifications.send_mobile_notification(
                {"event_id": event_id, "asset": "PEPE", "model": "FABLE"}, "HIGH", store=self.store,
            )
        first_id = first_sender.call_args.kwargs.get("notification_id")
        self.assertIsNotNone(first_id)
        # Back-date the failed attempt so it clears the cross-cycle retry's
        # spacing window - same fixture technique as
        # TestRetryPendingNtfy.test_retries_failed_event_and_succeeds.
        self.store.set_ntfy_status(event_id, "FAILED", "2020-01-01T00:00:00+00:00", error="boom")

        from radar_v08 import config
        original_topic = config.NTFY_TOPIC
        config.NTFY_TOPIC = "unit-test-topic"
        try:
            with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_SUCCESS) as retry_sender:
                notifications.retry_pending_ntfy(self.store)
        finally:
            config.NTFY_TOPIC = original_topic

        retry_id = retry_sender.call_args.kwargs.get("notification_id")
        self.assertEqual(retry_id, first_id)

    def test_real_bridge_sequence_then_restart_resends_under_the_same_id(self):
        """The reviewer's required flow for attempt 2, end to end on a
        disposable SQLite file: claim -> PROCESSED -> notify_for_event (toast
        ok, ntfy push fails) -> mark_event_notified (exactly what
        claude_bridge does right after notify_fn, adding an outbox row) ->
        close the store and open a new one on the same file (restart) ->
        retry_pending_ntfy. The retried push must carry the same X-ID the
        first attempt carried, and the same value the toast got as its Tag.
        Transports are fakes: subprocess.run and requests.post are mocked, so
        nothing real is shown or sent."""
        from datetime import datetime, timedelta, timezone

        from radar_v08 import config

        event_id = self._make_event_row(status="PENDING")
        now_iso = "2026-09-13T12:00:30+00:00"
        self.assertTrue(self.store.claim_event_for_processing(event_id, now_iso))
        self.store.mark_event_processed(event_id, now_iso)

        event = {
            "event_id": event_id, "asset": "PEPE", "model": "FABLE", "setup_type": "BREAKOUT",
            "direction": "LONG", "opportunity_score": 82.0, "tradeability_score": 88.0,
        }
        with mock.patch.object(config, "NOTIFICATIONS_ENABLED", True), \
             mock.patch.object(config, "NTFY_TOPIC", "unit-test-topic"), \
             mock.patch.object(ntfy.time, "sleep"), \
             mock.patch.object(
                 notifications, "copy_prompt_for_event", return_value={"copied": False, "popup_opened": False},
             ), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr="")) as toast_run, \
             mock.patch("requests.post", return_value=mock.Mock(status_code=500)) as first_post:
            first = notifications.notify_for_event(event, store=self.store)
        self.assertEqual(first["ntfy_result"], ntfy.RESULT_FAILED)
        self.store.mark_event_notified(event_id, now_iso)  # what the bridge does next

        first_id = first_post.call_args.kwargs["headers"]["X-ID"]
        toast_args = toast_run.call_args[0][0]
        self.assertEqual(toast_args[toast_args.index("-Tag") + 1], first_id)
        self.assertRegex(first_id, r"^[0-9a-f]{16}$")

        # Restart: a new SnapshotStore on the same file, the old one closed.
        self.store.close()
        self.store = SnapshotStore(self.path)

        later = datetime.now(timezone.utc) + timedelta(seconds=config.NTFY_RETRY_MIN_INTERVAL_SECONDS + 60)
        with mock.patch.object(config, "NTFY_TOPIC", "unit-test-topic"), \
             mock.patch("requests.post", return_value=mock.Mock(status_code=200)) as retry_post:
            results = notifications.retry_pending_ntfy(self.store, now=later)

        self.assertEqual(results, [{"event_id": event_id, "ntfy_result": ntfy.RESULT_SUCCESS}])
        self.assertEqual(retry_post.call_count, 1)
        self.assertEqual(retry_post.call_args.kwargs["headers"]["X-ID"], first_id)
        self.assertEqual(self.store.get_event(event_id)["ntfy_status"], "SENT")

    def test_notify_for_event_uses_one_id_for_both_channels(self):
        event_id = self._make_event_row()
        with mock.patch.object(notifications, "send_windows_notification", return_value=True) as win_sender, \
             mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_SUCCESS) as ntfy_sender, \
             mock.patch.object(
                 notifications, "copy_prompt_for_event", return_value={"copied": False, "popup_opened": False},
             ):
            result = notifications.notify_for_event(
                {"event_id": event_id, "asset": "PEPE", "model": "FABLE"}, store=self.store,
            )
        self.assertIsNotNone(result["notification_id"])
        self.assertEqual(win_sender.call_args.kwargs.get("notification_id"), result["notification_id"])
        self.assertEqual(ntfy_sender.call_args.kwargs.get("notification_id"), result["notification_id"])


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

    def test_notification_id_is_passed_as_the_toast_tag(self):
        fake_result = mock.Mock(returncode=0, stderr="")
        with mock.patch("subprocess.run", return_value=fake_result) as run:
            notifications.send_windows_notification("DOGE alert", "text", notification_id="abc123def4567890")
        args = run.call_args[0][0]
        self.assertIn("-Tag", args)
        self.assertEqual(args[args.index("-Tag") + 1], "abc123def4567890")

    def test_no_notification_id_sends_an_empty_tag_not_none(self):
        fake_result = mock.Mock(returncode=0, stderr="")
        with mock.patch("subprocess.run", return_value=fake_result) as run:
            notifications.send_windows_notification("DOGE alert", "text")
        args = run.call_args[0][0]
        self.assertEqual(args[args.index("-Tag") + 1], "")


if __name__ == "__main__":
    unittest.main()
