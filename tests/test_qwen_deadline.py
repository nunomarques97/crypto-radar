"""Qwen profile limits, one deadline and bounded admission, all offline."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests
import test_integrity_wiring as wiring
from test_qwen import finalists, ollama_response, valid_review

from radar_v08 import config, heartbeat, qwen


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class DeadlineAfterEntry:
    """Expire caller waiting only after the worker is inside the blocked call."""

    def __init__(self, entered):
        self.entered = entered
        self.caller = threading.get_ident()
        self.reads = 0

    def __call__(self):
        if threading.get_ident() != self.caller:
            return 0.0
        self.reads += 1
        if self.reads == 1:
            return 0.0
        if not self.entered.wait(5):
            raise AssertionError("worker did not enter controlled transport")
        return 30.0


class TestQwenLimits(unittest.TestCase):
    def test_profile_file_changes_both_payload_limits(self):
        source = Path(config.__file__).with_name("model_profiles.toml").read_text(encoding="utf8")
        source = source.replace("context_tokens = 4096", "context_tokens = 3584")
        source = source.replace("output_cap_tokens = 768", "output_cap_tokens = 512")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.toml"
            path.write_text(source, encoding="utf8")
            runtime = config.resolve_qwen_runtime({}, path)
        self.assertEqual((runtime.context_tokens, runtime.output_cap_tokens), (3584, 512))
        post = mock.Mock(return_value=ollama_response(valid_review()))
        with mock.patch.object(config, "QWEN_RUNTIME", runtime):
            result = qwen.review_finalists(finalists("BTC"), post_fn=post)
        self.assertEqual(result.status, "OK")
        self.assertEqual(post.call_args.args[0]["options"], {"temperature": 0.0, "num_ctx": 3584, "num_predict": 512})

    def test_truncated_or_over_cap_response_is_rejected(self):
        for metadata in ({"done": False}, {"done_reason": "length"}, {"eval_count": 769},
                         {"eval_count": -1}, {"eval_count": True}, {"eval_count": None}):
            with self.subTest(metadata=metadata):
                response = ollama_response(valid_review()) | metadata
                post = mock.Mock(return_value=response)
                result = qwen.review_finalists(finalists("BTC"), post_fn=post)
                self.assertEqual((result.status, result.reviews, post.call_count), ("UNAVAILABLE", {}, 2))

    def test_output_exactly_at_cap_is_accepted(self):
        response = ollama_response(valid_review()) | {"done": True, "done_reason": "stop", "eval_count": 768}
        self.assertEqual(qwen.review_finalists(finalists("BTC"), post_fn=lambda _: response).status, "OK")


class TestQwenDeadline(unittest.TestCase):
    def tearDown(self):
        # A timeout returns before the worker's finally block; synchronize its
        # completion before another test replaces clocks or model globals.
        acquired = qwen._INFERENCE_SLOT.acquire(timeout=3)
        if acquired:
            qwen._INFERENCE_SLOT.release()
        self.assertTrue(acquired)

    def test_http_retry_receives_remaining_budget(self):
        clock = Clock()
        timeouts = []

        def post(_url, **kwargs):
            timeouts.append(kwargs["timeout"])
            clock.now += 10
            response = mock.Mock()
            response.json.return_value = ollama_response(None if len(timeouts) == 1 else valid_review())
            return response

        with mock.patch.object(qwen.requests, "post", post):
            result = qwen.review_finalists(finalists("BTC"), monotonic=clock)
        self.assertEqual(result.status, "OK")
        self.assertEqual(timeouts, [30.0, 20.0])

    def test_timeout_exhausting_budget_never_retries(self):
        clock = Clock()

        def post(_):
            clock.now = 30.0
            raise requests.Timeout("fake transport consumed budget")

        transport = mock.Mock(side_effect=post)
        result = qwen.review_finalists(finalists("BTC"), post_fn=transport, monotonic=clock)
        self.assertEqual((result.status, result.error_code, result.reviews), ("TIMEOUT", "deadline_exceeded", {}))
        self.assertEqual(transport.call_count, 1)

    def test_success_at_deadline_is_discarded(self):
        clock = Clock()

        def post(_):
            clock.now = 30.0
            return ollama_response(valid_review())

        result = qwen.review_finalists(finalists("BTC"), post_fn=post, monotonic=clock)
        self.assertEqual((result.status, result.reviews), ("TIMEOUT", {}))

    def test_validation_time_is_part_of_budget(self):
        clock = Clock()
        validate = qwen._validate_reviews

        def slow_validation(*args):
            result = validate(*args)
            clock.now = 30.0
            return result

        with mock.patch.object(qwen, "_validate_reviews", slow_validation):
            result = qwen.review_finalists(finalists("BTC"), post_fn=lambda _: ollama_response(valid_review()), monotonic=clock)
        self.assertEqual((result.status, result.reviews), ("TIMEOUT", {}))

    def test_expired_preparation_posts_nothing(self):
        clock = Clock()
        build = qwen._build_payload

        def prepare(*args):
            payload = build(*args)
            clock.now = 30.0
            return payload

        post = mock.Mock()
        with mock.patch.object(qwen, "_build_payload", prepare):
            result = qwen.review_finalists(finalists("BTC"), post_fn=post, monotonic=clock)
        self.assertEqual(result.status, "TIMEOUT")
        post.assert_not_called()

    def test_blocked_transport_returns_and_holds_slot_until_completion(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked(_):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test failed to release transport")
            return ollama_response(valid_review("OLD"))

        transport = mock.Mock(side_effect=blocked)
        try:
            started = time.monotonic()
            result = qwen.review_finalists(finalists("OLD"), post_fn=transport, monotonic=DeadlineAfterEntry(entered))
            elapsed = time.monotonic() - started
            self.assertTrue(entered.is_set())
            self.assertLess(elapsed, 2.0)  # generous ceiling; events control ordering
            self.assertEqual((result.status, result.reviews), ("TIMEOUT", {}))
            post = mock.Mock()
            for _ in range(3):
                busy = qwen.review_finalists(finalists("BTC"), post_fn=post)
                self.assertEqual(busy.error_code, "inference_busy")
            post.assert_not_called()
            self.assertEqual(qwen.review_finalists([]).status, "OK")
            self.assertEqual(transport.call_count, 1)
        finally:
            release.set()
            acquired = qwen._INFERENCE_SLOT.acquire(timeout=3)
            if acquired:
                qwen._INFERENCE_SLOT.release()
            self.assertTrue(acquired, "worker did not release its admission slot")
        fresh = qwen.review_finalists(finalists("BTC"), post_fn=lambda _: ollama_response(valid_review()))
        self.assertEqual((fresh.status, set(fresh.reviews)), ("OK", {"BTC"}))
        self.assertEqual(result.reviews, {})  # late OLD result cannot mutate caller output

    def test_thread_start_failure_does_not_leak_slot(self):
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")):
            with self.assertRaisesRegex(RuntimeError, "no thread"):
                qwen.review_finalists(finalists("BTC"))
        result = qwen.review_finalists(finalists("BTC"), post_fn=lambda _: ollama_response(valid_review()))
        self.assertEqual(result.status, "OK")

    def test_interrupted_start_keeps_running_workers_slot(self):
        entered = threading.Event()
        release = threading.Event()
        real_start = threading.Thread.start
        workers = []

        def interrupted_start(worker):
            workers.append(worker)
            real_start(worker)
            if not entered.wait(5):
                raise AssertionError("worker did not start")
            raise KeyboardInterrupt()

        def blocked(_):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release worker")
            return ollama_response(valid_review())

        try:
            with mock.patch.object(threading.Thread, "start", interrupted_start):
                with self.assertRaises(KeyboardInterrupt):
                    qwen.review_finalists(finalists("BTC"), post_fn=blocked)
            post = mock.Mock()
            busy = qwen.review_finalists(finalists("BTC"), post_fn=post)
            self.assertEqual(busy.error_code, "inference_busy")
            post.assert_not_called()
        finally:
            release.set()
            for worker in workers:
                worker.join(3)
                self.assertFalse(worker.is_alive())
        result = qwen.review_finalists(finalists("BTC"), post_fn=lambda _: ollama_response(valid_review()))
        self.assertEqual(result.status, "OK")


class TestQwenDeadlineWiring(wiring.IntegrityWiringBase):
    def tearDown(self):
        acquired = qwen._INFERENCE_SLOT.acquire(timeout=3)
        if acquired:
            qwen._INFERENCE_SLOT.release()
        try:
            self.assertTrue(acquired)
        finally:
            super().tearDown()

    def test_expired_model_review_finishes_heartbeat_deterministically(self):
        clock = Clock()

        def post(_):
            clock.now = 30.0
            return ollama_response(valid_review())

        def review(candidates):
            return qwen.review_finalists(candidates, post_fn=post, monotonic=clock)

        with mock.patch.object(heartbeat, "review_finalists", review):
            output = self.run_cycle(wiring.FakeKraken())
        self.assertEqual(output["data_quality"]["qwen"], "TIMEOUT")
        self.assertEqual(self.routed_assets(), ["BTC"])
        self.assertFalse(self.routed[0].qwen_call_sonnet)
        self.assertEqual(len(self.run_records), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM radar_runs"), 1)

    def test_next_cycle_falls_back_until_lingering_call_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        clock = DeadlineAfterEntry(entered)
        late = valid_review()
        late["reviews"][0]["direction"] = "SHORT"

        def blocked(_):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release worker")
            return ollama_response(late)

        post = mock.Mock(side_effect=blocked)

        def review(candidates):
            return qwen.review_finalists(candidates, post_fn=post, monotonic=clock)

        try:
            with mock.patch.object(heartbeat, "review_finalists", review):
                first = self.run_cycle(wiring.FakeKraken())
                second = self.run_cycle(wiring.FakeKraken())
            self.assertEqual(first["data_quality"]["qwen"], "TIMEOUT")
            self.assertEqual(second["data_quality"]["qwen"], "UNAVAILABLE")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(self.routed_assets(), ["BTC", "BTC"])
            self.assertTrue(all(not ctx.qwen_call_sonnet and ctx.qwen_direction is None for ctx in self.routed))
            self.assertEqual(len(self.run_records), 2)
        finally:
            release.set()
            acquired = qwen._INFERENCE_SLOT.acquire(timeout=3)
            if acquired:
                qwen._INFERENCE_SLOT.release()
            self.assertTrue(acquired)

        def fresh_review(candidates):
            return qwen.review_finalists(candidates, post_fn=lambda _: ollama_response(valid_review()))

        with mock.patch.object(heartbeat, "review_finalists", fresh_review):
            third = self.run_cycle(wiring.FakeKraken())
        self.assertEqual(third["data_quality"]["qwen"], "OK")
        self.assertEqual(self.routed[-1].qwen_direction, "LONG")
        self.assertEqual(len(self.run_records), 3)
        self.assertEqual(self.count("SELECT COUNT(*) FROM radar_runs"), 3)
