"""T032b: local inference worker with a fake clock, a fake model and a disposable ledger.

D21: no Ollama, no worker process against a real model, no production database. The
worker runs over ``radar_v08.workflow.worker`` with:

* a fake clock (wall and monotonic time move only when a test says so);
* a fake local inference that returns scripted results after scripted latency, or
  blocks in a thread until released (cancellation, watchdog, collection independence);
* the real T031a store (``adapters.invocation_store``) on a throwaway SQLite file in a
  temporary folder, through the wiring ``radar_v08.worker.SqliteInvocationLedger``.
"""

import ast
import contextlib
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import worker as entry
from radar_v08.adapters import evidence_store, invocation_store
from radar_v08.adapters.clock import SystemClock
from radar_v08.domain.integrity import InstrumentKind
from radar_v08.domain.invocation import (
    Direction,
    InvocationIdentity,
    InvocationRequest,
    InvocationState,
    ModelBudget,
    TransitionStatus,
)
from radar_v08.workflow import worker as worker_module
from radar_v08.workflow.scheduler import (
    CallRefusal,
    Horizon,
    ItemState,
    OpportunityVersion,
    Role,
    SchedulerPolicy,
    StaleReason,
    TokenBasis,
    TokenBound,
)
from radar_v08.workflow.worker import (
    Accepted,
    FailurePolicy,
    InferenceFailed,
    InferenceFailure,
    InferenceProfile,
    InferenceReply,
    InferenceWorker,
    PreparedPrompt,
    ProfileBreaker,
    SinkUnavailable,
    SubmitRefusal,
    WatchdogAction,
    WorkerConfigError,
    WorkerExit,
    WorkOutcome,
)

REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
MODEL = "fixture-profile-model:1"
SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
KEEP = InferenceReply({"verdict": "KEEP"}, 100, 5)
VETO = InferenceReply({"verdict": "VETO"}, 100, 5)


def failed(code):
    return InferenceFailed(code)


class FakeClock:
    def __init__(self):
        self.wall = T0
        self.mono = 1000.0
        self.sleeps = []
        self.on_sleep = None
        self._lock = threading.Lock()

    def current(self):
        with self._lock:
            return self.wall

    def monotonic(self):
        with self._lock:
            return self.mono

    def advance(self, seconds, wall_extra=0.0):
        with self._lock:
            self.mono += seconds
            self.wall += timedelta(seconds=seconds + wall_extra)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)
        if self.on_sleep is not None:
            self.on_sleep()


class Step:
    def __init__(self, result=KEEP, latency=0.0, wall_extra=0.0, hook=None, block=False, honour_cancel=True):
        self.result = result
        self.latency = latency
        self.wall_extra = wall_extra
        self.hook = hook
        self.block = block
        self.honour_cancel = honour_cancel


class FakeInference:
    """Scripted local model: latency moves the fake clock; ``block`` waits in a thread."""

    def __init__(self, clock, steps):
        self.clock = clock
        self.steps = list(steps)
        self.calls = []
        self.entered = threading.Event()
        self.unblock = threading.Event()

    def infer(self, call, cancel):
        self.calls.append(call)
        step = self.steps.pop(0)
        if step.hook is not None:
            step.hook()
        if step.block:
            self.entered.set()
            while not self.unblock.wait(0.005):
                if step.honour_cancel and cancel.is_set():
                    return failed(InferenceFailure.CANCELLED)
        self.clock.advance(step.latency, step.wall_extra)
        if isinstance(step.result, BaseException):
            raise step.result
        return step.result


class Verdict(Enum):
    KEEP = "KEEP"
    VETO = "VETO"


class VerdictInterpreter:
    """Typed interpreter: exactly one key, a closed enum. Anything else is rejected."""

    def __init__(self, hook=None):
        self.hook = hook

    def interpret(self, payload):
        if self.hook is not None:
            self.hook()
        if set(payload) != {"verdict"} or payload["verdict"] not in ("KEEP", "VETO"):
            return None
        return Accepted(Verdict(payload["verdict"]))


class Prompts:
    def __init__(self, tokens=200, hook=None):
        self.tokens = tokens
        self.hook = hook
        self.calls = []

    def prepare(self, version, *, repair):
        self.calls.append((version.item_id, repair))
        if self.hook is not None:
            self.hook()
        return PreparedPrompt("system", f"facts for {version.item_id}", SCHEMA, TokenBound(self.tokens, TokenBasis.TOKENIZER_COUNT))


class Sink:
    def __init__(self):
        self.batches = []
        self.fail = False

    def record(self, transitions, publication):
        if self.fail:
            raise SinkUnavailable("fake sink down")
        self.batches.append((transitions, publication))

    def states(self, item_id):
        return [t.state for batch, _ in self.batches for t in batch if t.item_id == item_id]

    def transitions(self, item_id):
        return [t for batch, _ in self.batches for t in batch if t.item_id == item_id]

    def publications(self):
        return [publication for _, publication in self.batches if publication is not None]


def identity(name, seed=None):
    digest = hashlib.sha256((seed or name).encode()).hexdigest()
    return InvocationIdentity("kraken", InstrumentKind.SPOT, name, "BREAKOUT", Direction.LONG, f"sha256:{digest}", "OC-1")


def profile(**changes):
    values = dict(profile_id="screener-fixture-v1", model=MODEL, role=Role.SCREENER, hard_timeout_seconds=30,
                  context_tokens=4096, output_cap_tokens=768)
    values.update(changes)
    return InferenceProfile(**values)


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "fixture.sqlite")
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.addCleanup(self.conn.close)
        evidence_store.apply_schema_migrations(self.conn, now=T0)
        self.clock = FakeClock()
        self.sink = Sink()

    def other_conn(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        self.addCleanup(conn.close)
        return conn

    def make(self, steps=(), *, budget=None, prompts=None, interpreter=None, prof=None, ledger=None, **kwargs):
        self.inference = FakeInference(self.clock, steps)
        self.prompts = prompts or Prompts()
        budget = budget or ModelBudget(MODEL, 100, 1000)
        self.ledger = ledger or entry.SqliteInvocationLedger(self.conn, budget, self.clock, "worker-a")
        self.worker = InferenceWorker(
            policy=kwargs.pop("policy", SchedulerPolicy()),
            clock=self.clock,
            profile=prof or profile(),
            inference=self.inference,
            prompts=self.prompts,
            interpreter=interpreter or VerdictInterpreter(),
            ledger=self.ledger,
            sink=self.sink,
            **kwargs,
        )
        return self.worker

    def version(self, item_id, *, sealed_ago=0.0, horizon=Horizon.MIN_15, score=80.0):
        return OpportunityVersion(
            item_id=item_id,
            identity=identity(item_id),
            horizon=horizon,
            opportunity_score=score,
            sealed_at=self.clock.current() - timedelta(seconds=sealed_ago),
            integrity_valid=True,
            deterministic_path_eligible=True,
        )

    def submit(self, item_id, **kwargs):
        result = self.worker.submit(self.version(item_id, **kwargs))
        self.assertNotIsInstance(result, SubmitRefusal)
        return result

    def row(self, invocation_id):
        return invocation_store.load_invocation(self.conn, invocation_id)

    def rows(self):
        return self.conn.execute("SELECT invocation_id, state, end_reason, generation, attempt_count FROM invocations").fetchall()

    def in_thread(self, fn):
        results = []
        self.addCleanup(self.inference.unblock.set)  # never leave a blocked fake call behind
        thread = threading.Thread(target=lambda: results.append(fn()), daemon=True)
        thread.start()
        self.assertTrue(self.inference.entered.wait(5), "the fake call never started")
        return thread, results


# --- happy path, profile and hard limit ------------------------------------------------------------


class TestHappyPath(WorkerCase):
    def test_publishes_typed_result_under_lease_and_completes_invocation(self):
        self.make([Step(KEEP, latency=12)])
        self.submit("a")
        report = self.worker.process_next()
        self.assertIs(report.outcome, WorkOutcome.PUBLISHED)
        self.assertIs(report.final_state, ItemState.COMPLETED)
        self.assertEqual(self.sink.states("a"), [ItemState.QUEUED, ItemState.RUNNING, ItemState.COMPLETED])
        [publication] = self.sink.publications()
        self.assertEqual((publication.item_id, publication.value, publication.model, publication.profile_id),
                         ("a", Verdict.KEEP, MODEL, "screener-fixture-v1"))
        record = self.row(report.invocation_id)
        self.assertIs(record.state, InvocationState.COMPLETED)
        self.assertEqual((record.attempt_count, record.generation, record.lease_owner, record.model), (1, 1, "worker-a", MODEL))
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.IDLE)

    def test_model_comes_from_the_injected_profile(self):
        self.make([Step(KEEP)], prof=profile(model="another-local-model:7b"),
                  budget=ModelBudget("another-local-model:7b", 10, 10))
        self.submit("a")
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.PUBLISHED)
        self.assertEqual([call.model for call in self.inference.calls], ["another-local-model:7b"])
        self.assertEqual(self.inference.calls[0].output_cap_tokens, 768)
        self.assertEqual(self.inference.calls[0].context_tokens, 4096)

    def test_orchestration_names_no_model(self):
        source = Path(worker_module.__file__).read_text(encoding="utf-8").lower()
        for name in ("qwen", "llama", "gpt-oss", "gpt", "mistral", "gemma", ":14b", ":20b", "11434"):
            self.assertNotIn(name, source)


class TestHardLimit(WorkerCase):
    def test_call_limit_is_the_profile_timeout_and_fits_the_deadline(self):
        self.make([Step(KEEP)])
        self.submit("a", sealed_ago=5)  # 15m horizon: deadline = seal + 60 s
        self.worker.process_next()
        [call] = self.inference.calls
        self.assertEqual(call.timeout_seconds, 30.0)
        deadline = self.clock.current() + timedelta(seconds=55)
        self.assertLessEqual(self.clock.current() + timedelta(seconds=call.timeout_seconds + 10), deadline)

    def test_shorter_profile_timeout_is_the_limit(self):
        self.make([Step(KEEP)], prof=profile(hard_timeout_seconds=12))
        self.submit("a")
        self.worker.process_next()
        self.assertEqual(self.inference.calls[0].timeout_seconds, 12.0)

    def test_repair_that_cannot_fit_before_the_deadline_is_refused_without_a_call(self):
        self.make([Step(failed(InferenceFailure.MALFORMED), latency=21), Step(KEEP)])
        self.submit("a")
        report = self.worker.process_next()
        self.assertIs(report.outcome, WorkOutcome.FAILED)
        self.assertIs(report.call_refusal, CallRefusal.DEADLINE_UNFIT)
        self.assertEqual(len(self.inference.calls), 1)
        self.assertEqual(self.prompts.calls, [("a", False), ("a", True)])
        record = self.row(report.invocation_id)
        self.assertEqual((record.state, record.end_reason), (InvocationState.RELEASED, "DEADLINE_EXPIRED"))

    def test_perturbing_the_reserve_changes_the_limit(self):
        policy = SchedulerPolicy(finalization_reserve_seconds=25)
        self.make([Step(KEEP)], policy=policy)
        self.submit("a", horizon=Horizon.HOUR_1)  # deadline 120 s: 30 + 25 fits
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.PUBLISHED)
        self.make([Step(KEEP)], policy=policy)
        self.submit("b", horizon=Horizon.MIN_15, sealed_ago=10)  # 10 + 30 + 25 > 60: refused at admission
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.IDLE)
        self.assertIn(ItemState.DETERMINISTIC_ONLY, self.sink.states("b"))
        self.assertEqual(self.inference.calls, [])


# --- failures, late results, lease loss ----------------------------------------------------------------


class TestFailures(WorkerCase):
    def test_fake_latency_beyond_limit_is_discarded_as_late(self):
        self.make([Step(KEEP, latency=31)])
        self.submit("a")
        report = self.worker.process_next()
        self.assertIs(report.outcome, WorkOutcome.FAILED)
        self.assertEqual(report.failures, (InferenceFailure.TIMEOUT,))
        self.assertEqual(self.sink.publications(), [])
        self.assertIs(self.row(report.invocation_id).state, InvocationState.RELEASED)

    def test_timeout_failure_releases_and_fails(self):
        self.make([Step(failed(InferenceFailure.TIMEOUT), latency=30)])
        self.submit("a")
        report = self.worker.process_next()
        self.assertEqual((report.outcome, report.final_state, report.failures),
                         (WorkOutcome.FAILED, ItemState.FAILED, (InferenceFailure.TIMEOUT,)))
        record = self.row(report.invocation_id)
        self.assertEqual((record.state, record.end_reason), (InvocationState.RELEASED, "FAILED"))

    def test_malformed_then_repaired_publishes_with_two_attempts(self):
        self.make([Step(failed(InferenceFailure.MALFORMED), latency=3), Step(VETO, latency=3)])
        self.submit("a")
        report = self.worker.process_next()
        self.assertIs(report.outcome, WorkOutcome.PUBLISHED)
        self.assertEqual((report.calls, report.failures), (2, (InferenceFailure.MALFORMED,)))
        self.assertEqual(self.prompts.calls, [("a", False), ("a", True)])
        self.assertEqual(self.sink.publications()[0].value, Verdict.VETO)
        self.assertEqual(self.row(report.invocation_id).attempt_count, 2)

    def test_only_one_repair(self):
        self.make([Step(failed(InferenceFailure.MALFORMED)), Step(failed(InferenceFailure.MALFORMED)), Step(KEEP)],
                  failure_policy=FailurePolicy(max_failures=5))
        self.submit("a")
        report = self.worker.process_next()
        self.assertIs(report.outcome, WorkOutcome.FAILED)
        self.assertEqual(len(self.inference.calls), 2)

    def test_repair_needs_a_further_budget_reservation(self):
        self.make([Step(failed(InferenceFailure.MALFORMED)), Step(KEEP)], budget=ModelBudget(MODEL, 1, 10))
        self.submit("a")
        report = self.worker.process_next()
        self.assertEqual((report.outcome, report.lease_status, report.calls),
                         (WorkOutcome.BUDGET_REFUSED, TransitionStatus.BUDGET_EXHAUSTED, 1))

    def test_timeout_is_not_repaired(self):
        self.make([Step(failed(InferenceFailure.TIMEOUT)), Step(KEEP)], failure_policy=FailurePolicy(max_failures=5))
        self.submit("a")
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.FAILED)
        self.assertEqual(len(self.inference.calls), 1)

    def test_adapter_exception_is_a_typed_failure_without_its_text(self):
        self.make([Step(RuntimeError("model said: DROP TABLE invocations"))])
        self.submit("a")
        report = self.worker.process_next()
        self.assertEqual((report.outcome, report.failures), (WorkOutcome.FAILED, (InferenceFailure.ADAPTER_ERROR,)))
        self.assertNotIn("DROP", repr(report))
        self.assertNotIn("DROP", repr(self.sink.batches))

    def test_every_adapter_failure_code_ends_in_a_terminal_record(self):
        codes = [c for c in InferenceFailure if c not in (InferenceFailure.CANCELLED,)]
        for code in codes:
            with self.subTest(code=code):
                self.make([Step(failed(code)), Step(failed(code))], failure_policy=FailurePolicy(max_failures=99))
                self.submit(f"item-{code.value}")
                report = self.worker.process_next()
                self.assertIs(report.outcome, WorkOutcome.FAILED)
                self.assertEqual(self.sink.states(f"item-{code.value}")[-1], ItemState.FAILED)
                self.assertIs(self.row(report.invocation_id).state, InvocationState.RELEASED)


class TestLateResult(WorkerCase):
    def test_reply_after_the_deadline_is_abort_stale_and_never_committed(self):
        # The wall clock passes the opportunity deadline while the call runs.
        self.make([Step(KEEP, latency=5, wall_extra=60)])
        self.submit("a")
        report = self.worker.process_next()
        self.assertIs(report.final_state, ItemState.ABORT_STALE)
        self.assertEqual(report.failures, (InferenceFailure.LATE,))
        self.assertEqual(self.sink.publications(), [])
        self.assertNotIn(ItemState.COMPLETED, self.sink.states("a"))
        record = self.row(report.invocation_id)
        self.assertEqual((record.state, record.end_reason), (InvocationState.RELEASED, "DEADLINE_EXPIRED"))

    def test_deadline_crossed_between_reply_and_commit_is_abort_stale(self):
        self.make([Step(KEEP, latency=10)], interpreter=VerdictInterpreter(hook=lambda: self.clock.advance(55)))
        self.submit("a")
        report = self.worker.process_next()
        self.assertIs(report.outcome, WorkOutcome.ABORT_STALE)
        [stale] = [t for t in self.sink.transitions("a") if t.state is ItemState.ABORT_STALE]
        self.assertIs(stale.stale_reason, StaleReason.LATE_RESULT)
        self.assertEqual(self.sink.publications(), [])
        self.assertEqual(self.row(report.invocation_id).end_reason, "DEADLINE_EXPIRED")


class TestLeaseFence(WorkerCase):
    def steal(self):
        stolen = invocation_store.recover_expired(
            self.other_conn(), owner="worker-b", now=self.clock.current() + timedelta(hours=1), lease_seconds=60
        )
        self.assertEqual(len(stolen), 1)

    def test_result_after_lease_loss_is_fenced_and_not_published(self):
        self.make([Step(KEEP, latency=5, hook=self.steal)])
        self.submit("a")
        report = self.worker.process_next()
        self.assertEqual((report.outcome, report.lease_status, report.final_state),
                         (WorkOutcome.FENCED, TransitionStatus.FENCED, ItemState.FAILED))
        self.assertEqual(self.sink.publications(), [])
        record = self.row(report.invocation_id)
        self.assertEqual((record.state, record.generation, record.lease_owner), (InvocationState.CLAIMED, 2, "worker-b"))

    def test_lease_lost_before_the_call_makes_no_call(self):
        self.make([Step(KEEP)], prompts=Prompts(hook=self.steal))
        self.submit("a")
        report = self.worker.process_next()
        self.assertEqual((report.outcome, report.lease_status), (WorkOutcome.FENCED, TransitionStatus.FENCED))
        self.assertEqual(self.inference.calls, [])

    def test_expired_lease_is_fenced(self):
        class SkewedLedger(entry.SqliteInvocationLedger):
            def complete(inner, lease):
                return invocation_store.complete_invocation(
                    self.conn, lease, now=self.clock.current() + timedelta(hours=1)).status

        clock_ledger = SkewedLedger(self.conn, ModelBudget(MODEL, 10, 10), self.clock, "worker-a")
        self.make([Step(KEEP)], ledger=clock_ledger)
        self.submit("a")
        report = self.worker.process_next()
        self.assertEqual((report.outcome, report.lease_status), (WorkOutcome.FENCED, TransitionStatus.LEASE_EXPIRED))
        self.assertEqual(self.sink.publications(), [])

    def test_identity_held_by_another_worker_is_not_run(self):
        self.make([Step(KEEP)])
        invocation_store.claim_invocation(self.other_conn(), InvocationRequest(identity("a"), MODEL),
                                          ModelBudget(MODEL, 10, 10), owner="worker-b", now=T0, lease_seconds=60)
        self.submit("a")
        report = self.worker.process_next()
        self.assertEqual((report.outcome, report.final_state), (WorkOutcome.CLAIM_DUPLICATE, ItemState.FAILED))
        self.assertEqual(self.inference.calls, [])

    def test_budget_refusal_makes_no_call(self):
        self.make([Step(KEEP)], budget=ModelBudget(MODEL, 0, 0))
        self.submit("a")
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.BUDGET_REFUSED)
        self.assertEqual(self.inference.calls, [])
        self.assertEqual(self.rows(), [])

    def test_lease_outlives_the_deadline_by_the_reserve(self):
        self.make([Step(KEEP)])
        self.submit("a", sealed_ago=4)  # deadline in 56 s
        report = self.worker.process_next()
        record = self.row(report.invocation_id)
        self.assertEqual(record.lease_expires_at, T0 + timedelta(seconds=56 + 10))

    def test_recover_releases_a_crashed_holders_expired_claim(self):
        self.make()
        claimed = invocation_store.claim_invocation(self.conn, InvocationRequest(identity("z"), MODEL),
                                                    ModelBudget(MODEL, 10, 10), owner="crashed", now=T0, lease_seconds=5)
        self.clock.advance(6)
        report = self.worker.recover()
        self.assertEqual((report.recovered, report.released, report.ledger_failure), (1, 1, None))
        record = self.row(claimed.lease.invocation_id)
        self.assertEqual((record.state, record.end_reason, record.generation, record.lease_owner),
                         (InvocationState.RELEASED, "DEADLINE_EXPIRED", 2, "worker-a"))


# --- cancellation, watchdog, restart ---------------------------------------------------------------------


class TestCancellation(WorkerCase):
    def test_stop_cancels_the_running_call_and_releases_as_cancelled(self):
        self.make([Step(block=True)])
        self.submit("a")
        thread, results = self.in_thread(self.worker.process_next)
        self.worker.request_stop()
        thread.join(5)
        [report] = results
        self.assertEqual((report.outcome, report.failures), (WorkOutcome.CANCELLED, ()))
        self.assertEqual(self.row(report.invocation_id).end_reason, "CANCELLED")
        self.assertFalse(self.worker.profile_disabled)
        self.assertIs(self.worker.submit(self.version("b")), SubmitRefusal.STOPPED)

    def test_watchdog_cancels_at_the_limit(self):
        self.make([Step(block=True)])
        self.submit("a")
        thread, results = self.in_thread(self.worker.process_next)
        self.clock.advance(29.9)
        self.assertIs(self.worker.watchdog_tick(), WatchdogAction.NONE)
        self.clock.advance(0.1)
        self.assertIs(self.worker.watchdog_tick(), WatchdogAction.CANCEL)
        thread.join(5)
        [report] = results
        self.assertEqual((report.outcome, report.failures), (WorkOutcome.FAILED, (InferenceFailure.TIMEOUT,)))
        self.assertFalse(self.worker.restart_required)

    def test_cancellation_that_does_not_end_the_call_requires_a_restart(self):
        self.make([Step(block=True, honour_cancel=False)])
        self.submit("a")
        self.submit("b")
        thread, results = self.in_thread(self.worker.process_next)
        self.clock.advance(30)
        self.assertIs(self.worker.watchdog_tick(), WatchdogAction.CANCEL)
        self.clock.advance(4.9)
        self.assertIs(self.worker.watchdog_tick(), WatchdogAction.CANCEL)
        self.clock.advance(0.1)
        self.assertIs(self.worker.watchdog_tick(), WatchdogAction.TERMINATE)
        self.assertTrue(self.worker.restart_required)
        self.inference.unblock.set()  # the stuck call finally returns a reply: it must not commit
        thread.join(5)
        [report] = results
        self.assertIs(report.outcome, WorkOutcome.RESTART_REQUIRED)
        self.assertEqual(self.sink.publications(), [])
        self.assertIs(self.row(report.invocation_id).state, InvocationState.RELEASED)
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.RESTART_REQUIRED)
        self.assertIs(self.worker.submit(self.version("c")), SubmitRefusal.RESTART_REQUIRED)
        self.assertEqual(len(self.inference.calls), 1)

    def test_watch_loop_terminates_once_after_the_grace(self):
        self.make([Step(block=True, honour_cancel=False)], cancel_grace_seconds=3)
        self.submit("a")
        thread, _ = self.in_thread(self.worker.process_next)
        terminated = []
        stop = threading.Event()

        def terminate():
            terminated.append(self.clock.monotonic())
            self.inference.unblock.set()

        started = self.clock.monotonic()
        self.clock.on_sleep = lambda: stop.set() if self.clock.monotonic() - started > 100 else None  # safety stop
        action = self.worker.watch(stop, terminate, poll_seconds=1)
        thread.join(5)
        self.assertIs(action, WatchdogAction.TERMINATE)
        self.assertEqual(len(terminated), 1)
        self.assertEqual(terminated[0] - started, 33)  # 30 s limit + 3 s grace

    def test_run_returns_restart_required(self):
        def overrun():
            self.clock.advance(36)
            self.worker.watchdog_tick()

        self.make([Step(KEEP, hook=overrun)])
        self.submit("a")
        self.assertIs(self.worker.run(idle_seconds=1), WorkerExit.RESTART_REQUIRED)
        self.assertEqual(self.sink.publications(), [])

    def test_grace_must_fit_inside_the_finalization_reserve(self):
        for grace in (10.5, 0, -1, float("inf"), True):
            with self.subTest(grace=grace), self.assertRaises(WorkerConfigError):
                self.make(cancel_grace_seconds=grace)
        self.make(cancel_grace_seconds=10)


# --- profile disablement --------------------------------------------------------------------------------


class TestFailureBreaker(WorkerCase):
    def test_two_failures_within_ten_minutes_disable_until_review(self):
        breaker = ProfileBreaker(FailurePolicy())
        self.assertFalse(breaker.record_failure(0.0))
        self.assertTrue(breaker.record_failure(600.0))
        self.assertTrue(breaker.disabled)
        self.assertTrue(breaker.record_failure(5000.0))  # stays disabled: only review re-enables
        breaker.review()
        self.assertFalse(breaker.disabled)

    def test_failures_further_apart_do_not_disable(self):
        breaker = ProfileBreaker(FailurePolicy())
        self.assertFalse(breaker.record_failure(0.0))
        self.assertFalse(breaker.record_failure(600.5))
        self.assertTrue(breaker.record_failure(1100.0))

    def test_perturbed_policy_changes_behaviour(self):
        breaker = ProfileBreaker(FailurePolicy(max_failures=3, window_seconds=60))
        self.assertFalse(breaker.record_failure(0))
        self.assertFalse(breaker.record_failure(30))
        self.assertTrue(breaker.record_failure(60))
        for bad in (dict(max_failures=0), dict(window_seconds=0), dict(window_seconds=float("nan"))):
            with self.subTest(bad=bad), self.assertRaises(WorkerConfigError):
                FailurePolicy(**bad)

    def test_disabled_profile_refuses_admission_and_drains_without_calls(self):
        self.make([Step(failed(InferenceFailure.UNAVAILABLE)), Step(failed(InferenceFailure.UNAVAILABLE))])
        self.submit("a")
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.FAILED)
        self.assertFalse(self.worker.profile_disabled)
        self.clock.advance(300)
        self.submit("b")
        self.submit("c")
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.FAILED)
        self.assertTrue(self.worker.profile_disabled)
        self.assertIs(self.worker.submit(self.version("d")), SubmitRefusal.PROFILE_DISABLED)
        report = self.worker.process_next()
        self.assertEqual((report.outcome, report.item_id, report.final_state),
                         (WorkOutcome.PROFILE_DISABLED, "c", ItemState.FAILED))
        self.assertEqual(len(self.inference.calls), 2)
        self.worker.review_profile()
        self.assertNotIsInstance(self.worker.submit(self.version("e")), SubmitRefusal)

    def test_malformed_and_failed_repair_count_as_two_failures(self):
        self.make([Step(failed(InferenceFailure.MALFORMED)), Step(failed(InferenceFailure.MALFORMED))])
        self.submit("a")
        self.worker.process_next()
        self.assertTrue(self.worker.profile_disabled)

    def test_stop_cancellation_is_not_a_profile_failure(self):
        self.make([Step(failed(InferenceFailure.CANCELLED))] * 3)
        for name in ("a", "b"):
            self.submit(name)
            self.assertIs(self.worker.process_next().outcome, WorkOutcome.CANCELLED)
        self.assertFalse(self.worker.profile_disabled)


# --- profiles and roles ---------------------------------------------------------------------------------


class TestProfiles(WorkerCase):
    def test_challenger_and_deep_analyst_profiles_are_refused(self):
        for role in (Role.CHALLENGER, Role.DEEP_ANALYST):
            with self.subTest(role=role), self.assertRaises(WorkerConfigError):
                profile(role=role)

    def test_profile_must_fit_the_policy_role(self):
        for changes in (dict(hard_timeout_seconds=31), dict(output_cap_tokens=769)):
            with self.subTest(changes=changes), self.assertRaises(WorkerConfigError):
                self.make(prof=profile(**changes))

    def test_profile_fields_are_validated(self):
        for changes in (dict(model=""), dict(model=" m"), dict(profile_id="x\n"), dict(hard_timeout_seconds=0),
                        dict(context_tokens=768), dict(output_cap_tokens=True), dict(think="no")):
            with self.subTest(changes=changes), self.assertRaises(WorkerConfigError):
                profile(**changes)

    def test_context_that_cannot_hold_prompt_and_output_makes_no_call(self):
        self.make([Step(KEEP)], prof=profile(context_tokens=3000), prompts=Prompts(tokens=2500))
        self.submit("a")
        report = self.worker.process_next()
        self.assertEqual((report.outcome, report.final_state), (WorkOutcome.CONTEXT_UNFIT, ItemState.FAILED))
        self.assertEqual(self.inference.calls, [])


# --- untrusted output ------------------------------------------------------------------------------------


class TestUntrustedOutput(WorkerCase):
    def test_output_only_becomes_a_typed_value_or_is_rejected(self):
        hostile = InferenceReply({"verdict": "KEEP; DROP TABLE invocations"}, 1, 1)
        extra = InferenceReply({"verdict": "VETO", "command": "rm -rf /", "state": "COMPLETED"}, 1, 1)
        self.make([Step(hostile), Step(extra)])
        self.submit("a")
        report = self.worker.process_next()
        self.assertEqual((report.outcome, report.failures),
                         (WorkOutcome.FAILED, (InferenceFailure.OUTPUT_REJECTED, InferenceFailure.OUTPUT_REJECTED)))
        self.assertEqual(self.sink.publications(), [])
        self.assertEqual(self.sink.states("a")[-1], ItemState.FAILED)
        self.assertEqual(len(self.rows()), 1)

    def test_worker_module_has_no_effectful_imports_or_dynamic_code(self):
        tree = ast.parse(Path(worker_module.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(imported, {"__future__", "math", "threading", "collections.abc", "dataclasses", "datetime",
                                    "enum", "typing", "radar_v08.domain.invocation", "radar_v08.workflow.scheduler"})
        calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse(calls & {"eval", "exec", "compile", "open", "__import__", "getattr", "setattr"})

    def test_reports_carry_codes_not_text(self):
        report_fields = {f for f in worker_module.WorkReport.__dataclass_fields__}
        self.assertEqual(report_fields, {"outcome", "item_id", "invocation_id", "final_state", "calls", "failures",
                                         "call_refusal", "lease_status", "ledger_failure"})


# --- collection independence, concurrency one, sink --------------------------------------------------------


class TestIndependence(WorkerCase):
    def test_submission_never_waits_for_a_running_call(self):
        self.make([Step(block=True), Step(KEEP)])
        self.submit("a")
        thread, results = self.in_thread(self.worker.process_next)
        started = time.monotonic()
        admitted = self.worker.submit(self.version("b"))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(admitted.state, ItemState.QUEUED)
        self.assertTrue(thread.is_alive())  # the call is still running
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.IDLE)  # concurrency one
        self.assertEqual(len(self.inference.calls), 1)
        self.inference.unblock.set()
        thread.join(5)
        self.assertIs(results[0].outcome, WorkOutcome.PUBLISHED)
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.PUBLISHED)

    def test_sink_outage_keeps_every_record_in_order(self):
        self.make([Step(KEEP)])
        self.sink.fail = True
        self.submit("a")
        self.assertEqual(self.worker.pending_records(), 1)
        self.assertIs(self.worker.submit(self.version("b")), SubmitRefusal.SINK_UNAVAILABLE)
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.SINK_UNAVAILABLE)
        self.assertEqual(self.inference.calls, [])
        self.sink.fail = False
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.PUBLISHED)
        self.assertEqual(self.sink.states("a"), [ItemState.QUEUED, ItemState.RUNNING, ItemState.COMPLETED])
        self.assertEqual(self.worker.pending_records(), 0)

    def test_publication_is_kept_when_the_sink_fails_at_commit(self):
        def sink_down():
            self.sink.fail = True

        self.make([Step(KEEP, hook=sink_down)])
        self.submit("a")
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.PUBLISHED)
        self.assertEqual(self.sink.publications(), [])
        self.sink.fail = False
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.IDLE)
        self.assertEqual(len(self.sink.publications()), 1)

    def test_clock_backwards_at_commit_loses_no_record(self):
        self.make([Step(KEEP, latency=5, wall_extra=-20)])
        self.submit("a")
        report = self.worker.process_next()
        self.assertIs(report.outcome, WorkOutcome.SCHEDULER_ERROR)
        self.assertEqual(self.sink.publications(), [])
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.SCHEDULER_ERROR)  # still backwards
        self.clock.advance(25)
        self.assertIs(self.worker.process_next().outcome, WorkOutcome.IDLE)
        self.assertEqual(self.sink.states("a")[-1], ItemState.FAILED)

    def test_run_loop_processes_then_sleeps_and_stops(self):
        self.make([Step(KEEP), Step(VETO)])
        self.submit("a")
        self.submit("b")
        self.clock.on_sleep = self.worker.request_stop
        self.assertIs(self.worker.run(idle_seconds=2), WorkerExit.STOPPED)
        self.assertEqual([p.value for p in self.sink.publications()], [Verdict.KEEP, Verdict.VETO])
        self.assertEqual(self.clock.sleeps, [2.0])


# --- adapters and entry point --------------------------------------------------------------------------------


class TestSystemClock(unittest.TestCase):
    def test_wall_clock_is_aware_utc_and_monotonic_does_not_go_back(self):
        clock = SystemClock()
        now = clock.current()
        self.assertEqual(now.utcoffset(), timedelta(0))
        self.assertLess(abs(now.timestamp() - time.time()), 5)
        first = clock.monotonic()
        clock.sleep(0)
        self.assertGreaterEqual(clock.monotonic(), first)

    def test_sleep_is_bounded(self):
        for bad in (-1, float("nan"), float("inf"), True, 3601):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SystemClock().sleep(bad)


class TestEntryPoint(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = entry.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_no_arguments_starts_nothing(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("Nothing was started", out)

    def test_module_entry_starts_nothing(self):
        result = subprocess.run([sys.executable, "-m", "radar_v08.worker"], cwd=REPO, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing was started", result.stdout)

    def test_check_validates_offline(self):
        code, out, _ = self.run_main("--check", "--model", MODEL, "--profile-id", "screener-v1")
        self.assertEqual(code, 0)
        self.assertIn("endpoint=http://127.0.0.1:11434", out)
        self.assertIn(f"model={MODEL}", out)

    def test_check_refuses_non_loopback_and_disabled_profiles(self):
        cases = {
            ("--ollama-url", "https://ollama.com"): "not_http",
            ("--ollama-url", "http://10.0.0.5:11434"): "not_loopback",
            ("--role", "challenger"): "profile",
            ("--role", "deep_analyst"): "profile",
            ("--hard-timeout", "31"): "role limits",
        }
        for extra, needle in cases.items():
            with self.subTest(extra=extra):
                code, out, err = self.run_main("--check", "--model", MODEL, "--profile-id", "p", *extra)
                self.assertEqual(code, 2)
                self.assertIn(needle, err)
                self.assertEqual(out, "")
        code, _, _ = self.run_main("--check", "--profile-id", "p")  # no model: nothing is assumed
        self.assertEqual(code, 2)

    def test_worker_is_not_wired_to_the_loop_cli_or_ui(self):
        forbidden = {"radar_v08.worker", "radar_v08.workflow.worker", "radar_v08.adapters.local_inference",
                     "radar_v08.adapters.clock"}
        files = [REPO / "radar.py", *sorted((REPO / "radar_v08").glob("*.py")), *sorted((REPO / "ui").rglob("*.py"))]
        for path in files:
            if path.name in ("worker.py", "__main__.py") and path.parent.name == "radar_v08":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level and path.parent.name == "radar_v08":
                        base = "radar_v08" + ("." + base if base else "")
                    targets = [base] + [f"{base}.{alias.name}" for alias in node.names]
                for target in targets:
                    self.assertNotIn(target, forbidden, f"{path.name} imports {target}")


if __name__ == "__main__":
    unittest.main()
