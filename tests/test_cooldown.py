import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.cooldown import check_cooldown, record_send
from radar_v08.store import SnapshotStore

T0 = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


class TestCooldown(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    def test_no_prior_send_is_allowed(self):
        allowed, reason = check_cooldown(self.store, "BTC", "SONNET", T0, "BREAKOUT", "LONG", 60.0)
        self.assertTrue(allowed)
        self.assertEqual(reason, "no_prior_send")

    def test_normal_cooldown_blocks_repeat_send(self):
        record_send(self.store, "BTC", "SONNET", T0, "BREAKOUT", "LONG", 60.0)
        allowed, reason = check_cooldown(
            self.store, "BTC", "SONNET", T0 + timedelta(minutes=30), "BREAKOUT", "LONG", 60.0
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "in_cooldown")

    def test_cooldown_expires_after_configured_hours(self):
        record_send(self.store, "BTC", "SONNET", T0, "BREAKOUT", "LONG", 60.0)
        allowed, reason = check_cooldown(
            self.store, "BTC", "SONNET", T0 + timedelta(hours=5), "BREAKOUT", "LONG", 60.0
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "cooldown_expired")

    def test_setup_type_change_bypasses_cooldown(self):
        record_send(self.store, "BTC", "SONNET", T0, "BREAKOUT", "LONG", 60.0)
        allowed, reason = check_cooldown(
            self.store, "BTC", "SONNET", T0 + timedelta(minutes=10), "REVERSAL", "LONG", 60.0
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "setup_type_changed")

    def test_direction_change_bypasses_cooldown(self):
        record_send(self.store, "BTC", "SONNET", T0, "BREAKOUT", "LONG", 60.0)
        allowed, reason = check_cooldown(
            self.store, "BTC", "SONNET", T0 + timedelta(minutes=10), "BREAKOUT", "SHORT", 60.0
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "direction_changed")

    def test_opportunity_jump_bypasses_cooldown(self):
        record_send(self.store, "BTC", "SONNET", T0, "BREAKOUT", "LONG", 60.0)
        allowed, reason = check_cooldown(
            self.store, "BTC", "SONNET", T0 + timedelta(minutes=10), "BREAKOUT", "LONG", 80.0
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "opportunity_increase")

    def test_small_opportunity_change_still_blocked(self):
        record_send(self.store, "BTC", "SONNET", T0, "BREAKOUT", "LONG", 60.0)
        allowed, reason = check_cooldown(
            self.store, "BTC", "SONNET", T0 + timedelta(minutes=10), "BREAKOUT", "LONG", 65.0
        )
        self.assertFalse(allowed)

    def test_cooldown_is_per_model(self):
        record_send(self.store, "BTC", "SONNET", T0, "BREAKOUT", "LONG", 60.0)
        allowed, reason = check_cooldown(
            self.store, "BTC", "FABLE", T0 + timedelta(minutes=10), "BREAKOUT", "LONG", 60.0
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "no_prior_send")


if __name__ == "__main__":
    unittest.main()
