import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from radar_v08 import config, mock_alert
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore
from ui.data_reader import DataReader

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


class DataReaderTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)
        self.reader = DataReader(self.store)

        self._original_events_log_path = config.EVENTS_LOG_PATH
        config.EVENTS_LOG_PATH = self.path + ".events.jsonl"

        self._original_output_path = config.OUTPUT_V08_PATH
        self.output_path = self.path + ".output.json"
        config.OUTPUT_V08_PATH = self.output_path

    def tearDown(self):
        self.store.close()
        config.EVENTS_LOG_PATH = self._original_events_log_path
        config.OUTPUT_V08_PATH = self._original_output_path
        for suffix in ("", "-wal", "-shm", ".events.jsonl", ".output.json"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)

    def write_output(self, payload: dict) -> None:
        with open(self.output_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)


class TestOutputSnapshotReading(DataReaderTestCase):
    def test_missing_file_returns_none(self):
        self.assertIsNone(self.reader.read_output_snapshot())

    def test_reads_written_file(self):
        self.write_output({"timestamp": "2026-09-14T09:21:03Z", "funnel": {"L1_shortlist": 40}})
        snapshot = self.reader.read_output_snapshot()
        self.assertEqual(snapshot["funnel"]["L1_shortlist"], 40)

    def test_caches_until_mtime_changes(self):
        self.write_output({"timestamp": "t1"})
        os.utime(self.output_path, (1_000_000, 1_000_000))
        first = self.reader.read_output_snapshot()
        self.assertEqual(first["timestamp"], "t1")

        # Two fast writes can land on the same filesystem mtime tick - force
        # a distinct one so this proves the mtime-gated re-read, not luck.
        self.write_output({"timestamp": "t2"})
        os.utime(self.output_path, (2_000_000, 2_000_000))
        second = self.reader.read_output_snapshot()
        self.assertEqual(second["timestamp"], "t2")

    def test_malformed_json_keeps_last_known_good(self):
        self.write_output({"timestamp": "good"})
        self.reader.read_output_snapshot()
        with open(self.output_path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        # Force a distinct mtime so the reader actually attempts a re-read.
        os.utime(self.output_path, (0, 0))
        snapshot = self.reader.read_output_snapshot()
        self.assertEqual(snapshot["timestamp"], "good")


class TestFunnelDemandVsCalls(DataReaderTestCase):
    def test_demand_and_calls_never_conflated(self):
        self.write_output({
            "timestamp": T0.isoformat(),
            "universe": {"pairs_seen": 312},
            "funnel": {"sonnet_demand": 4, "fable_demand": 1, "L1_shortlist": 40},
        })
        # Zero model_analyses rows exist - calls must read as 0, not silently
        # mirror demand.
        funnel = self.reader.funnel()
        self.assertEqual(funnel["sonnet_demand"], 4)
        self.assertEqual(funnel["sonnet_calls"], 0)
        self.assertEqual(funnel["fable_demand"], 1)
        self.assertEqual(funnel["fable_calls"], 0)

    def test_real_calls_reflected_once_made(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(model_demand="FABLE"))
        self.store.insert_model_analysis(
            event_id=event_id, model=config.ANTHROPIC_FABLE_MODEL, model_version="bridge_prompt_v1",
            requested_at=T0.isoformat(), completed_at=T0.isoformat(), status="SUCCESS",
            response="{}", parsed_output_json="{}", latency_ms=1.0, input_tokens=1, output_tokens=1, error=None,
        )
        self.write_output({"timestamp": T0.isoformat(), "funnel": {"fable_demand": 1}})
        funnel = self.reader.funnel()
        self.assertEqual(funnel["fable_calls"], 1)

    def test_missing_funnel_keys_read_as_none_not_zero(self):
        self.write_output({"timestamp": T0.isoformat(), "funnel": {}})
        funnel = self.reader.funnel()
        self.assertIsNone(funnel["l3_finalists"])  # absent key, not fabricated 0


class TestSystemStatus(DataReaderTestCase):
    def test_bridge_health_unknown_before_any_write(self):
        self.assertEqual(self.reader.claude_bridge_health(), "UNKNOWN")

    def test_bridge_health_reflects_store(self):
        self.store.set_bridge_health("AUTH_ERROR", "no key", T0.isoformat())
        self.assertEqual(self.reader.claude_bridge_health(), "AUTH_ERROR")

    def test_qwen_status_unknown_without_output(self):
        self.assertEqual(self.reader.qwen_status(), "UNKNOWN")

    def test_qwen_status_ok_when_a_candidate_was_reviewed(self):
        self.write_output({"candidates": [{"asset": "BTC", "qwen": {"veto": False}}]})
        self.assertEqual(self.reader.qwen_status(), "OK")

    def test_futures_status_from_data_quality(self):
        self.write_output({"data_quality": {"futures_ticker": "STALE"}})
        self.assertEqual(self.reader.futures_status(), "STALE")


class TestAlertsMockIsolation(DataReaderTestCase):
    def test_mock_alert_excluded_from_real_alerts(self):
        mock_alert.create_mock_event(self.store, now=T0)
        create_event_if_new(self.store, **make_event_kwargs(asset="ETH"))
        real = self.reader.real_alerts()
        mocks = self.reader.mock_alerts()
        self.assertEqual(len(real), 1)
        self.assertEqual(real[0]["asset"], "ETH")
        self.assertEqual(len(mocks), 1)
        self.assertTrue(str(mocks[0]["event_id"]).startswith("MOCK-"))


class TestEventLifecycle(DataReaderTestCase):
    def test_lifecycle_fields_sourced_from_real_columns_only(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(model_demand="FABLE"))
        self.store.set_ntfy_status(event_id, "SENT", T0.isoformat())
        row = self.store.get_event(event_id)
        lifecycle = self.reader.event_lifecycle(row)
        self.assertTrue(lifecycle["detected"])
        self.assertFalse(lifecycle["qwen"])  # no context_json -> honestly false, not guessed
        self.assertTrue(lifecycle["router"])  # model_demand == FABLE
        self.assertEqual(lifecycle["ntfy_status"], "SENT")
        self.assertFalse(lifecycle["claude_analysed"])  # no model_analyses row yet

    def test_claude_analysed_true_after_successful_analysis(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(model_demand="FABLE"))
        self.store.insert_model_analysis(
            event_id=event_id, model=config.ANTHROPIC_FABLE_MODEL, model_version="bridge_prompt_v1",
            requested_at=T0.isoformat(), completed_at=T0.isoformat(), status="SUCCESS",
            response="{}", parsed_output_json="{}", latency_ms=1.0, input_tokens=1, output_tokens=1, error=None,
        )
        row = self.store.get_event(event_id)
        lifecycle = self.reader.event_lifecycle(row)
        self.assertTrue(lifecycle["claude_analysed"])


if __name__ == "__main__":
    unittest.main()
