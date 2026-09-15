import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore

T0 = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def make_event_kwargs(**overrides):
    base = dict(
        ts=T0.isoformat(), type_="RADAR_ALERT", asset="BTC",
        setup_type="BREAKOUT", direction="LONG", market="SPOT",
        anomaly_score=70.0, opportunity_score=80.0, tradeability_score=85.0,
        confidence="HIGH", model_demand="FABLE", reason="test", status="PENDING",
    )
    base.update(overrides)
    return base


class BridgeStoreTestCase(unittest.TestCase):
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
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(self.events_log):
            os.remove(self.events_log)


class TestEventLifecycle(BridgeStoreTestCase):
    def test_new_columns_default_sanely(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        row = self.store.get_event(event_id)
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["last_error"])
        self.assertIsNone(row["context_json"])
        self.assertEqual(row["notified"], 0)

    def test_context_json_round_trips(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(context={"asset": "BTC", "foo": 1}))
        row = self.store.get_event(event_id)
        import json
        self.assertEqual(json.loads(row["context_json"]), {"asset": "BTC", "foo": 1})

    def test_find_actionable_events_includes_pending(self):
        create_event_if_new(self.store, **make_event_kwargs())
        actionable = self.store.find_actionable_events(T0.isoformat(), 10)
        self.assertEqual(len(actionable), 1)

    def test_find_actionable_events_excludes_processing_processed_failed(self):
        e1, _ = create_event_if_new(self.store, **make_event_kwargs(status="PROCESSING"))
        e2, _ = create_event_if_new(self.store, **make_event_kwargs(asset="ETH", status="PROCESSED"))
        e3, _ = create_event_if_new(self.store, **make_event_kwargs(asset="SOL", status="FAILED"))
        actionable = self.store.find_actionable_events(T0.isoformat(), 10)
        self.assertEqual(actionable, [])

    def test_deferred_event_excluded_before_next_attempt_at(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(status="DEFERRED"))
        future_attempt = (T0 + timedelta(hours=1)).isoformat()
        self.store.mark_event_deferred(event_id, T0.isoformat(), "rate_limited", future_attempt)
        actionable = self.store.find_actionable_events(T0.isoformat(), 10)
        self.assertEqual(actionable, [])

    def test_deferred_event_included_once_due(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(status="DEFERRED"))
        past_attempt = (T0 - timedelta(minutes=1)).isoformat()
        self.store.mark_event_deferred(event_id, T0.isoformat(), "rate_limited", past_attempt)
        actionable = self.store.find_actionable_events(T0.isoformat(), 10)
        self.assertEqual(len(actionable), 1)
        self.assertEqual(actionable[0]["attempts"], 1)

    def test_claim_for_processing_succeeds_once(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.assertTrue(self.store.claim_event_for_processing(event_id, T0.isoformat()))
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "PROCESSING")

    def test_claim_for_processing_fails_when_already_processing(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.assertTrue(self.store.claim_event_for_processing(event_id, T0.isoformat()))
        self.assertFalse(self.store.claim_event_for_processing(event_id, T0.isoformat()))

    def test_recover_stale_processing_resets_to_pending(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        stale_ts = (T0 - timedelta(minutes=20)).isoformat()
        self.store.claim_event_for_processing(event_id, stale_ts)
        cutoff = (T0 - timedelta(minutes=10)).isoformat()
        recovered = self.store.recover_stale_processing(cutoff, T0.isoformat())
        self.assertEqual(recovered, [event_id])
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["attempts"], 1)

    def test_recover_stale_processing_leaves_recent_processing_alone(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.store.claim_event_for_processing(event_id, T0.isoformat())
        cutoff = (T0 - timedelta(minutes=10)).isoformat()
        recovered = self.store.recover_stale_processing(cutoff, T0.isoformat())
        self.assertEqual(recovered, [])
        self.assertEqual(self.store.get_event(event_id)["status"], "PROCESSING")

    def test_mark_event_processed(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.store.claim_event_for_processing(event_id, T0.isoformat())
        self.store.mark_event_processed(event_id, T0.isoformat())
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "PROCESSED")
        self.assertIsNone(row["processing_started_at"])

    def test_mark_event_deferred_increments_attempts(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.store.claim_event_for_processing(event_id, T0.isoformat())
        self.store.mark_event_deferred(event_id, T0.isoformat(), "timeout: x", (T0 + timedelta(minutes=1)).isoformat())
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "DEFERRED")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("timeout", row["last_error"])

    def test_mark_event_failed(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.store.claim_event_for_processing(event_id, T0.isoformat())
        self.store.mark_event_failed(event_id, T0.isoformat(), "auth_error: bad key")
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "FAILED")

    def test_event_status_counts(self):
        create_event_if_new(self.store, **make_event_kwargs())
        create_event_if_new(self.store, **make_event_kwargs(asset="ETH", status="PROCESSED"))
        counts = self.store.event_status_counts()
        self.assertEqual(counts["PENDING"], 1)
        self.assertEqual(counts["PROCESSED"], 1)
        self.assertEqual(counts["FAILED"], 0)

    def test_latest_event(self):
        create_event_if_new(self.store, **make_event_kwargs())
        e2, _ = create_event_if_new(self.store, **make_event_kwargs(asset="ETH", ts=(T0 + timedelta(minutes=5)).isoformat()))
        latest = self.store.latest_event()
        self.assertEqual(latest["event_id"], e2)

    def test_notified_flag(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.assertEqual(self.store.get_event(event_id)["notified"], 0)
        self.store.mark_event_notified(event_id, T0.isoformat())
        self.assertEqual(self.store.get_event(event_id)["notified"], 1)


class TestModelAnalyses(BridgeStoreTestCase):
    def test_insert_and_query(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.store.insert_model_analysis(
            event_id=event_id, model="claude-fable-5-1", model_version="bridge_prompt_v1",
            requested_at=T0.isoformat(), completed_at=T0.isoformat(), status="SUCCESS",
            response="{}", parsed_output_json="{}", latency_ms=123.4,
            input_tokens=100, output_tokens=200, error=None,
        )
        rows = self.store.get_model_analyses_for_event(event_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "SUCCESS")
        self.assertTrue(self.store.has_successful_analysis(event_id))

    def test_has_successful_analysis_false_when_only_failures(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.store.insert_model_analysis(
            event_id=event_id, model="claude-sonnet-5", model_version="bridge_prompt_v1",
            requested_at=T0.isoformat(), completed_at=None, status="TIMEOUT",
            response=None, parsed_output_json=None, latency_ms=None,
            input_tokens=None, output_tokens=None, error="timeout",
        )
        self.assertFalse(self.store.has_successful_analysis(event_id))

    def test_call_counts_empty_when_no_analyses(self):
        self.assertEqual(self.store.model_analysis_call_counts(), {})

    def test_call_counts_group_by_model(self):
        e1, _ = create_event_if_new(self.store, **make_event_kwargs())
        e2, _ = create_event_if_new(self.store, **make_event_kwargs(asset="ETH"))
        self.store.insert_model_analysis(
            event_id=e1, model="claude-fable-5-1", model_version="bridge_prompt_v1",
            requested_at=T0.isoformat(), completed_at=T0.isoformat(), status="SUCCESS",
            response="{}", parsed_output_json="{}", latency_ms=100.0,
            input_tokens=10, output_tokens=20, error=None,
        )
        self.store.insert_model_analysis(
            event_id=e2, model="claude-fable-5-1", model_version="bridge_prompt_v1",
            requested_at=T0.isoformat(), completed_at=None, status="TIMEOUT",
            response=None, parsed_output_json=None, latency_ms=None,
            input_tokens=None, output_tokens=None, error="timeout",
        )
        self.store.insert_model_analysis(
            event_id=e1, model="claude-sonnet-5", model_version="bridge_prompt_v1",
            requested_at=T0.isoformat(), completed_at=T0.isoformat(), status="SUCCESS",
            response="{}", parsed_output_json="{}", latency_ms=50.0,
            input_tokens=5, output_tokens=10, error=None,
        )
        counts = self.store.model_analysis_call_counts()
        # Every attempt counts (SUCCESS or not) - this is "calls made", not "calls succeeded".
        self.assertEqual(counts, {"claude-fable-5-1": 2, "claude-sonnet-5": 1})


class TestBridgeHealth(BridgeStoreTestCase):
    def test_default_is_none(self):
        self.assertIsNone(self.store.get_bridge_health())

    def test_set_and_get(self):
        self.store.set_bridge_health("ONLINE", None, T0.isoformat())
        row = self.store.get_bridge_health()
        self.assertEqual(row["state"], "ONLINE")

    def test_overwrite_keeps_single_row(self):
        self.store.set_bridge_health("ONLINE", None, T0.isoformat())
        self.store.set_bridge_health("RATE_LIMITED", "429", (T0 + timedelta(minutes=1)).isoformat())
        row = self.store.get_bridge_health()
        self.assertEqual(row["state"], "RATE_LIMITED")
        self.assertEqual(row["detail"], "429")


if __name__ == "__main__":
    unittest.main()
