"""Qwen batch telemetry.

`QwenBatchResult` carries `elapsed_ms` (injected monotonic clock) and
`attempts` (transport calls started), and every non-OK batch carries an
`error_code`. The heartbeat writes them to `data_quality.qwen_batch` only when
a batch was requested. Status, retry, deadline and profile semantics are
unchanged. Everything is offline: a fake transport and a fake clock, no Ollama.
"""

import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TESTS_DIR))
sys.path.insert(0, TESTS_DIR)

import requests  # noqa: E402
import test_integrity_wiring as wiring  # noqa: E402  (fake Kraken heartbeat harness)
from test_qwen import finalists, ollama_response, valid_review  # noqa: E402

from radar_v08 import config, heartbeat, qwen  # noqa: E402
from radar_v08.qwen import QwenBatchResult  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Transport:
    """Returns (or raises) each scripted reply in turn, advancing the clock."""

    def __init__(self, clock, replies, seconds=2.5):
        self.clock = clock
        self.replies = list(replies)
        self.seconds = seconds
        self.calls = 0

    def __call__(self, _payload):
        self.calls += 1
        self.clock.now += self.seconds
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class TestBatchTelemetry(unittest.TestCase):
    def tearDown(self):
        # A deadline timeout returns before the worker's finally block.
        acquired = qwen._INFERENCE_SLOT.acquire(timeout=3)
        if acquired:
            qwen._INFERENCE_SLOT.release()
        self.assertTrue(acquired)

    def review(self, replies, seconds=2.5):
        clock = Clock()
        transport = Transport(clock, replies, seconds)
        result = qwen.review_finalists(finalists("BTC"), post_fn=transport, monotonic=clock)
        return result, transport

    def test_ok_on_first_attempt(self):
        result, transport = self.review([ollama_response(valid_review())])
        self.assertEqual(result.status, "OK")
        self.assertEqual((result.elapsed_ms, result.attempts, result.error_code), (2500.0, 1, None))
        self.assertEqual(transport.calls, 1)

    def test_validation_retry_then_ok_counts_both_attempts(self):
        missing = ollama_response({"reviews": []})
        result, transport = self.review([missing, ollama_response(valid_review())])
        self.assertEqual((result.status, set(result.reviews)), ("OK", {"BTC"}))
        self.assertEqual((result.elapsed_ms, result.attempts, result.error_code), (5000.0, 2, None))
        self.assertEqual(transport.calls, 2)

    def test_transport_timeouts_exhaust_retries(self):
        result, _ = self.review([requests.Timeout("slow"), requests.Timeout("slow")], seconds=5)
        self.assertEqual((result.status, result.reviews), ("TIMEOUT", {}))
        self.assertEqual((result.elapsed_ms, result.attempts, result.error_code), (10000.0, 2, "timeout"))

    def test_batch_deadline_exceeded(self):
        result, transport = self.review([requests.Timeout("slow")], seconds=30)
        self.assertEqual((result.status, result.error_code), ("TIMEOUT", "deadline_exceeded"))
        self.assertEqual((result.elapsed_ms, result.attempts), (30000.0, 1))
        self.assertEqual(transport.calls, 1)

    def test_exhausted_retries_carry_a_code_per_failure_kind(self):
        cases = {
            "schema_invalid": ollama_response({"reviews": []}),
            "invalid_json": {"message": {"content": "not json"}},
            "incomplete_response": ollama_response(valid_review()) | {"done_reason": "length"},
            "request_error": requests.ConnectionError("refused"),
        }
        for code, reply in cases.items():
            with self.subTest(code=code):
                result, _ = self.review([reply, reply])
                self.assertEqual((result.status, result.reviews), ("UNAVAILABLE", {}))
                self.assertEqual((result.attempts, result.error_code, result.elapsed_ms), (2, code, 5000.0))

    def test_busy_slot_posts_nothing(self):
        self.assertTrue(qwen._INFERENCE_SLOT.acquire(blocking=False))
        try:
            result, transport = self.review([])
        finally:
            qwen._INFERENCE_SLOT.release()
        self.assertEqual((result.status, result.error_code), ("UNAVAILABLE", "inference_busy"))
        self.assertEqual((result.attempts, result.elapsed_ms, transport.calls), (0, 0.0, 0))

    def test_refused_profile_and_empty_batch_have_no_elapsed_time(self):
        with mock.patch.object(config, "QWEN_RUNTIME", None):
            refused, transport = self.review([])
        self.assertEqual((refused.status, refused.attempts, refused.elapsed_ms), ("UNAVAILABLE", 0, None))
        self.assertEqual(transport.calls, 0)
        empty = qwen.review_finalists([], monotonic=Clock())
        self.assertEqual((empty.status, empty.attempts, empty.elapsed_ms), ("OK", 0, None))


class TestRunRecordTelemetry(wiring.IntegrityWiringBase):
    def cycle(self, full, batch=None):
        if batch is not None:
            self.qwen = mock.Mock(return_value=batch)
            patcher = mock.patch.object(heartbeat, "review_finalists", self.qwen)
            patcher.start()
            self.addCleanup(patcher.stop)
        session = wiring.GuardedSession(1.0, 0, 0.0, http_session=wiring.FakeKraken(assets=("BTC", "ETH")))
        heartbeat.run_heartbeat(mode="TEST", store=self.store, full=full, session=session, clock=wiring.StepClock())
        self.assertEqual(len(self.run_records), 1)
        return self.run_records[0]["data_quality"]

    def test_requested_batch_is_recorded(self):
        batch = QwenBatchResult(
            status="TIMEOUT", error="timeout: slow", error_code="timeout", elapsed_ms=61234.5, attempts=2
        )
        data_quality = self.cycle(full=True, batch=batch)
        self.qwen.assert_called_once()
        self.assertEqual(data_quality["qwen"], "TIMEOUT")  # the status string is unchanged
        self.assertEqual(data_quality["qwen_batch"], {"elapsed_ms": 61234.5, "attempts": 2, "error_code": "timeout"})

    def test_ok_batch_records_null_error_code(self):
        batch = QwenBatchResult(status="OK", elapsed_ms=812.0, attempts=1)
        data_quality = self.cycle(full=True, batch=batch)
        self.assertEqual(data_quality["qwen_batch"], {"elapsed_ms": 812.0, "attempts": 1, "error_code": None})

    def test_no_batch_requested_has_no_telemetry(self):
        data_quality = self.cycle(full=False)
        self.assertEqual(data_quality["qwen"], "SKIPPED")
        self.assertNotIn("qwen_batch", data_quality)

    def test_all_finalists_blocked_at_the_seal_has_no_telemetry(self):
        kraken = wiring.FakeKraken(assets=("BTC",))
        kraken.depth["XXBTZUSD"]["result"]["XXBTZUSD"]["bids"][0][0] = "60010.0"  # crossed BTC book
        output = self.run_cycle(kraken)
        self.assertEqual(self.qwen.calls, [])
        self.assertEqual(output["data_quality"]["qwen"], "SKIPPED")
        self.assertNotIn("qwen_batch", output["data_quality"])


if __name__ == "__main__":
    unittest.main()
