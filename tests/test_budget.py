import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.budgets import try_consume_budget
from radar_v08.store import SnapshotStore

T0 = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


class TestBudget(unittest.TestCase):
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

    def test_hourly_budget_allows_up_to_the_limit(self):
        limit = config.MODEL_BUDGETS["FABLE"]["hourly"]
        for _ in range(limit):
            allowed, _status = try_consume_budget(self.store, "FABLE", T0)
            self.assertTrue(allowed)

        allowed, status = try_consume_budget(self.store, "FABLE", T0)
        self.assertFalse(allowed)
        self.assertEqual(status["hourly_used"], limit)

    def test_daily_budget_caps_even_across_different_hours(self):
        from datetime import timedelta
        daily_limit = config.MODEL_BUDGETS["FABLE"]["daily"]
        hourly_limit = config.MODEL_BUDGETS["FABLE"]["hourly"]
        consumed = 0
        hour = 0
        while consumed < daily_limit:
            now = T0 + timedelta(hours=hour)
            for _ in range(min(hourly_limit, daily_limit - consumed)):
                allowed, _ = try_consume_budget(self.store, "FABLE", now)
                self.assertTrue(allowed)
                consumed += 1
            hour += 1

        # Budget for the day is now exhausted, even in a brand new hour.
        allowed, status = try_consume_budget(self.store, "FABLE", T0 + timedelta(hours=hour))
        self.assertFalse(allowed)
        self.assertEqual(status["daily_used"], daily_limit)

    def test_budget_exhausted_is_reported_not_silently_dropped(self):
        limit = config.MODEL_BUDGETS["SONNET"]["hourly"]
        for _ in range(limit):
            try_consume_budget(self.store, "SONNET", T0)
        allowed, status = try_consume_budget(self.store, "SONNET", T0)
        self.assertFalse(allowed)
        self.assertIn("hourly_used", status)
        self.assertIn("hourly_limit", status)

    def test_budgets_are_independent_per_model(self):
        limit = config.MODEL_BUDGETS["FABLE"]["hourly"]
        for _ in range(limit):
            try_consume_budget(self.store, "FABLE", T0)
        allowed, _status = try_consume_budget(self.store, "SONNET", T0)
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
