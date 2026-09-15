import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.events import create_event_if_new, transition_event
from radar_v08.store import SnapshotStore


def make_event_kwargs(**overrides):
    base = dict(
        ts="2026-09-13T12:00:00+00:00", type_="RADAR_ALERT", asset="BTC",
        setup_type="BREAKOUT", direction="LONG", market="SPOT",
        anomaly_score=70.0, opportunity_score=80.0, tradeability_score=85.0,
        confidence="HIGH", model_demand="FABLE", reason="test", status="PENDING",
    )
    base.update(overrides)
    return base


class TestEventCreation(unittest.TestCase):
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

    def test_event_creation_writes_sqlite_row_and_jsonl_line(self):
        event_id, created = create_event_if_new(self.store, **make_event_kwargs())
        self.assertTrue(created)
        row = self.store.get_event(event_id)
        self.assertEqual(row["asset"], "BTC")
        self.assertEqual(row["status"], "PENDING")
        self.assertTrue(os.path.exists(self.events_log))
        with open(self.events_log) as fh:
            lines = fh.readlines()
        self.assertEqual(len(lines), 1)

    def test_deduplication_skips_a_second_identical_open_event(self):
        event_id_1, created_1 = create_event_if_new(self.store, **make_event_kwargs())
        event_id_2, created_2 = create_event_if_new(self.store, **make_event_kwargs(ts="2026-09-13T12:05:00+00:00"))
        self.assertTrue(created_1)
        self.assertFalse(created_2)
        self.assertEqual(event_id_1, event_id_2)

    def test_changed_setup_creates_a_new_event(self):
        event_id_1, _ = create_event_if_new(self.store, **make_event_kwargs())
        event_id_2, created_2 = create_event_if_new(self.store, **make_event_kwargs(setup_type="REVERSAL"))
        self.assertTrue(created_2)
        self.assertNotEqual(event_id_1, event_id_2)

    def test_state_transition_updates_status(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        transition_event(self.store, event_id, "PROCESSING")
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "PROCESSING")
        transition_event(self.store, event_id, "PROCESSED")
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "PROCESSED")

    def test_deferred_event_is_created_when_budget_exhausted(self):
        event_id, created = create_event_if_new(self.store, **make_event_kwargs(status="DEFERRED"))
        self.assertTrue(created)
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "DEFERRED")

    def test_resolved_event_allows_a_fresh_one_later(self):
        event_id_1, _ = create_event_if_new(self.store, **make_event_kwargs())
        transition_event(self.store, event_id_1, "PROCESSED")
        event_id_2, created_2 = create_event_if_new(self.store, **make_event_kwargs())
        self.assertTrue(created_2)
        self.assertNotEqual(event_id_1, event_id_2)

    def test_failed_event_status_is_settable(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        transition_event(self.store, event_id, "FAILED")
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "FAILED")

    def test_invalid_status_is_rejected(self):
        with self.assertRaises(ValueError):
            create_event_if_new(self.store, **make_event_kwargs(status="BOGUS"))


if __name__ == "__main__":
    unittest.main()
