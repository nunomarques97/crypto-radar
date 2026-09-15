import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import cli, ntfy


class TestNotifyTestMode(unittest.TestCase):
    def setUp(self):
        self._original_topic = ntfy.config.NTFY_TOPIC

    def tearDown(self):
        ntfy.config.NTFY_TOPIC = self._original_topic

    def test_disabled_when_topic_missing(self):
        ntfy.config.NTFY_TOPIC = None
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.run_mode("notify-test")
        self.assertEqual(code, 0)
        self.assertIn("DISABLED", buf.getvalue())

    def test_success_path_marks_message_as_test(self):
        ntfy.config.NTFY_TOPIC = "unit-test-topic"
        with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_SUCCESS) as sender:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.run_mode("notify-test")
        self.assertEqual(code, 0)
        self.assertIn("ENABLED", buf.getvalue())
        self.assertIn("SUCCESS", buf.getvalue())
        title = sender.call_args[0][0]
        self.assertIn("TEST", title)

    def test_failure_path_returns_nonzero(self):
        ntfy.config.NTFY_TOPIC = "unit-test-topic"
        with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_FAILED):
            code = cli.run_mode("notify-test")
        self.assertEqual(code, 1)

    def test_notify_test_never_touches_kraken_qwen_or_claude(self):
        ntfy.config.NTFY_TOPIC = "unit-test-topic"
        with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_SUCCESS), \
             mock.patch("radar_v08.cli.run_and_write") as heartbeat, \
             mock.patch("radar_v08.cli.claude_bridge.run_bridge_cycle") as bridge:
            cli.run_mode("notify-test")
        heartbeat.assert_not_called()
        bridge.assert_not_called()

    def test_synthetic_message_is_not_a_real_event(self):
        """The notify-test payload must never look like a real router-produced
        event: no event_id, no asset ticker being sent as a real alert."""
        ntfy.config.NTFY_TOPIC = "unit-test-topic"
        with mock.patch.object(ntfy, "send_ntfy_notification", return_value=ntfy.RESULT_SUCCESS) as sender:
            cli.run_mode("notify-test")
        title, message = sender.call_args[0][0], sender.call_args[0][1]
        self.assertIn("TEST", title)
        self.assertIn("Synthetic", message)


if __name__ == "__main__":
    unittest.main()
