import logging
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import ntfy


class TestConfiguration(unittest.TestCase):
    def setUp(self):
        self._original_topic = ntfy.config.NTFY_TOPIC

    def tearDown(self):
        ntfy.config.NTFY_TOPIC = self._original_topic

    def test_missing_topic_is_not_configured(self):
        ntfy.config.NTFY_TOPIC = None
        self.assertFalse(ntfy.is_configured())

    def test_present_topic_is_configured(self):
        ntfy.config.NTFY_TOPIC = "my-secret-topic"
        self.assertTrue(ntfy.is_configured())

    def test_send_returns_disabled_when_not_configured(self):
        ntfy.config.NTFY_TOPIC = None
        with mock.patch("requests.post") as post:
            result = ntfy.send_ntfy_notification("title", "message")
        post.assert_not_called()
        self.assertEqual(result, ntfy.RESULT_DISABLED)


class TestPriorityMapping(unittest.TestCase):
    def test_medium_maps_to_default(self):
        self.assertEqual(ntfy.priority_for_level("MEDIUM"), "default")

    def test_high_maps_to_high(self):
        self.assertEqual(ntfy.priority_for_level("HIGH"), "high")

    def test_unknown_level_defaults(self):
        self.assertEqual(ntfy.priority_for_level("LOW"), "default")


class TestSendNtfyNotification(unittest.TestCase):
    def setUp(self):
        self._original_topic = ntfy.config.NTFY_TOPIC
        ntfy.config.NTFY_TOPIC = "unit-test-topic-secret"
        self._original_backoff = ntfy.config.NTFY_RETRY_BACKOFF_BASE
        ntfy.config.NTFY_RETRY_BACKOFF_BASE = 0.0  # keep tests fast

    def tearDown(self):
        ntfy.config.NTFY_TOPIC = self._original_topic
        ntfy.config.NTFY_RETRY_BACKOFF_BASE = self._original_backoff

    def test_success_posts_to_topic_url(self):
        fake_response = mock.Mock(status_code=200)
        with mock.patch("requests.post", return_value=fake_response) as post:
            result = ntfy.send_ntfy_notification("CRYPTO RADAR - PEPE", "BREAKOUT LONG | Opp: 82 | Trade: 88 | Modelo: FABLE")
        self.assertEqual(result, ntfy.RESULT_SUCCESS)
        post.assert_called_once()
        url = post.call_args[0][0]
        self.assertTrue(url.endswith("/unit-test-topic-secret"))
        self.assertIn("ntfy.sh", url)

    def test_timeout_is_retried_then_failed(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.exceptions.Timeout("timed out")) as post:
            result = ntfy.send_ntfy_notification("title", "message")
        self.assertEqual(result, ntfy.RESULT_FAILED)
        self.assertEqual(post.call_count, ntfy.config.NTFY_MAX_RETRIES + 1)

    def test_server_error_is_retried_then_failed(self):
        fake_response = mock.Mock(status_code=503)
        with mock.patch("requests.post", return_value=fake_response) as post:
            result = ntfy.send_ntfy_notification("title", "message")
        self.assertEqual(result, ntfy.RESULT_FAILED)
        self.assertGreater(post.call_count, 1)

    def test_client_error_is_not_retried(self):
        fake_response = mock.Mock(status_code=400)
        with mock.patch("requests.post", return_value=fake_response) as post:
            result = ntfy.send_ntfy_notification("title", "message")
        self.assertEqual(result, ntfy.RESULT_FAILED)
        self.assertEqual(post.call_count, 1)

    def test_success_after_a_retry(self):
        import requests
        responses = [requests.exceptions.ConnectionError("offline"), mock.Mock(status_code=200)]

        def fake_post(*args, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch("requests.post", side_effect=fake_post) as post:
            result = ntfy.send_ntfy_notification("title", "message")
        self.assertEqual(result, ntfy.RESULT_SUCCESS)
        self.assertEqual(post.call_count, 2)

    def test_notification_id_rides_the_x_id_header(self):
        fake_response = mock.Mock(status_code=200)
        with mock.patch("requests.post", return_value=fake_response) as post:
            ntfy.send_ntfy_notification("title", "message", notification_id="abc123")
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["X-ID"], "abc123")

    def test_no_notification_id_omits_the_x_id_header(self):
        fake_response = mock.Mock(status_code=200)
        with mock.patch("requests.post", return_value=fake_response) as post:
            ntfy.send_ntfy_notification("title", "message")
        headers = post.call_args.kwargs["headers"]
        self.assertNotIn("X-ID", headers)

    def test_non_latin1_notification_id_is_dropped_not_crashed(self):
        fake_response = mock.Mock(status_code=200)
        with mock.patch("requests.post", return_value=fake_response) as post:
            result = ntfy.send_ntfy_notification("title", "message", notification_id="日本語")
        self.assertEqual(result, ntfy.RESULT_SUCCESS)
        headers = post.call_args.kwargs["headers"]
        self.assertNotIn("X-ID", headers)

    def test_unsafe_or_overlong_notification_id_is_dropped_not_sent(self):
        fake_response = mock.Mock(status_code=200)
        for bad in ("abc\r\nX-Evil: 1", "has space", "a" * 65, "semi;colon"):
            with self.subTest(bad=bad):
                with mock.patch("requests.post", return_value=fake_response) as post:
                    result = ntfy.send_ntfy_notification("title", "message", notification_id=bad)
                self.assertEqual(result, ntfy.RESULT_SUCCESS)
                self.assertNotIn("X-ID", post.call_args.kwargs["headers"])

    def test_offline_failure_never_raises(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.exceptions.ConnectionError("no network")):
            try:
                result = ntfy.send_ntfy_notification("title", "message")
            except Exception as exc:  # noqa: BLE001
                self.fail(f"send_ntfy_notification raised {exc!r} instead of degrading to FAILED")
        self.assertEqual(result, ntfy.RESULT_FAILED)


class TestTopicNeverLogged(unittest.TestCase):
    def setUp(self):
        self._original_topic = ntfy.config.NTFY_TOPIC
        ntfy.config.NTFY_TOPIC = "super-secret-topic-value"
        self._original_backoff = ntfy.config.NTFY_RETRY_BACKOFF_BASE
        ntfy.config.NTFY_RETRY_BACKOFF_BASE = 0.0

    def tearDown(self):
        ntfy.config.NTFY_TOPIC = self._original_topic
        ntfy.config.NTFY_RETRY_BACKOFF_BASE = self._original_backoff

    def test_masked_topic_hides_the_middle(self):
        masked = ntfy.masked_topic()
        self.assertNotEqual(masked, ntfy.config.NTFY_TOPIC)
        self.assertNotIn(ntfy.config.NTFY_TOPIC, masked)

    def test_failure_log_never_contains_full_topic(self):
        import requests
        with self.assertLogs(ntfy.logger, level="WARNING") as captured:
            with mock.patch("requests.post", side_effect=requests.exceptions.ConnectionError("down")):
                ntfy.send_ntfy_notification("title", "message")
        full_log_text = "\n".join(captured.output)
        self.assertNotIn(ntfy.config.NTFY_TOPIC, full_log_text)


if __name__ == "__main__":
    unittest.main()
