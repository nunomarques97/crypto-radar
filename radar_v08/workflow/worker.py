"""OC-1 local inference worker (OC-1 sections 2 and 3).

Orchestration only: every effect comes in through an injected port (clock, local
inference, prompt source, result interpreter, invocation ledger, transition sink).
Nothing here performs HTTP, file or database I/O, reads the wall clock or names a
model: the model comes from the injected ``InferenceProfile``.

Rules:

* Consumes the scheduler policy (``DeadlineScheduler``) and the invocation lease/generation
  (``InvocationLedger`` port over ``adapters.invocation_store``). Concurrency is one:
  ``process_next`` runs at most one item, and the scheduler holds one running slot.
* Every model call is authorized by the scheduler first. Its hard limit is the
  scheduler's call deadline (profile hard timeout, which covers load, prompt
  processing and generation), and ``call deadline + finalization reserve`` never
  passes the opportunity deadline. The adapter enforces the limit with a monotonic
  clock; the worker re-checks both clocks after the call, so a reply that arrives late
  is discarded (``LATE``), never committed.
* Commit is fenced: the ledger's ``complete`` must apply under this worker's lease
  and generation, and the scheduler must still call the result publishable. A result
  after the deadline gets ``ABORT_STALE``; a result after lease loss gets ``FENCED``.
  Neither publishes anything.
* Cancellation is bounded: past the call limit the watchdog sets the call's cancel
  signal; if the call has still not returned after ``cancel_grace_seconds`` (at most
  the finalization reserve) the watchdog asks the caller to terminate the process and
  the worker refuses all further work (``RESTART_REQUIRED``).
* Two failed calls within 10 minutes (monotonic) disable the profile until an
  operator calls ``review_profile``. Admissions are then refused and queued work is
  failed without a model call.
* Collection is independent: ``submit`` holds the worker lock only for the in-memory
  admission and the transition write, never during a model call.
* Model output is untrusted data: the adapter returns a parsed JSON object or a typed
  failure (never free-text parsing), and only the injected ``ResultInterpreter`` can
  turn it into a typed value. Nothing from a model becomes a command, SQL or a state
  transition. Reports and failures carry codes only, never model or error text.
* Challenger and Deep Analyst profiles are refused (disabled in OC-1 production).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from radar_v08.domain.invocation import (
    MAX_LEASE_SECONDS,
    MIN_LEASE_SECONDS,
    Claimed,
    Duplicate,
    InvocationError,
    InvocationFailure,
    InvocationIdentity,
    Lease,
    Refused,
    ReleaseReason,
    TransitionStatus,
)
from radar_v08.workflow.scheduler import (
    PRODUCTION_DISABLED_ROLES,
    TERMINAL_ITEM_STATES,
    AdmissionResult,
    AdmissionStatus,
    CallRefusal,
    DeadlineScheduler,
    Dispatched,
    ItemState,
    OpportunityVersion,
    Role,
    SchedulerError,
    SchedulerPolicy,
    TokenBound,
    Transition,
)

MAX_TEXT_LENGTH = 200
DEFAULT_CANCEL_GRACE_SECONDS = 5.0


class WorkerConfigError(ValueError):
    """The worker, a profile or a call was configured outside OC-1. Nothing started."""


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise WorkerConfigError(f"{name} must be non-empty text without edge whitespace")
    if len(value) > MAX_TEXT_LENGTH or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise WorkerConfigError(f"{name} must be short text without control characters")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise WorkerConfigError(f"{name} must be a positive int")
    return value


def _require_seconds(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise WorkerConfigError(f"{name} must be a finite number of seconds above zero")
    return float(value)


# -- ports -------------------------------------------------------------------------------------


class WorkerClock(Protocol):
    """Wall clock (timezone-aware, for deadlines), monotonic clock (for elapsed time) and sleep."""

    def current(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class CancelSignal(Protocol):
    def is_set(self) -> bool: ...


class InferenceFailure(Enum):
    """Why a model call produced no usable reply. Codes only, no free text."""

    TIMEOUT = "timeout"  # the hard limit passed (adapter or watchdog)
    UNAVAILABLE = "unavailable"  # connection refused/reset, server down
    HTTP_STATUS = "http_status"  # a non-200, non-redirect status
    REDIRECT_REFUSED = "redirect_refused"  # a redirect was offered; never followed
    MALFORMED = "malformed"  # the envelope or the content is not the expected JSON object
    TOO_LARGE = "too_large"  # the body exceeded the adapter's byte cap
    CANCELLED = "cancelled"  # the cancel signal was seen
    INVALID_REQUEST = "invalid_request"  # the adapter could not encode the call
    ADAPTER_ERROR = "adapter_error"  # worker-assigned: the adapter raised instead of returning
    LATE = "late"  # worker-assigned: a reply arrived after the call limit
    OUTPUT_REJECTED = "output_rejected"  # worker-assigned: the typed interpreter refused the payload


@dataclass(frozen=True, slots=True)
class InferenceProfile:
    """A versioned local model profile. The model name lives here, never in orchestration."""

    profile_id: str
    model: str
    role: Role
    hard_timeout_seconds: int
    context_tokens: int
    output_cap_tokens: int
    think: bool = False

    def __post_init__(self) -> None:
        _require_text(self.profile_id, "profile_id")
        _require_text(self.model, "model")
        if not isinstance(self.role, Role):
            raise WorkerConfigError("role must be Role")
        if self.role in PRODUCTION_DISABLED_ROLES:
            raise WorkerConfigError(f"{self.role.value} is disabled in OC-1 production")
        _require_positive_int(self.hard_timeout_seconds, "hard_timeout_seconds")
        _require_positive_int(self.context_tokens, "context_tokens")
        _require_positive_int(self.output_cap_tokens, "output_cap_tokens")
        if self.output_cap_tokens >= self.context_tokens:
            raise WorkerConfigError("output_cap_tokens must leave room for input in context_tokens")
        if not isinstance(self.think, bool):
            raise WorkerConfigError("think must be bool")


@dataclass(frozen=True, slots=True)
class InferenceCall:
    """One bounded call. ``timeout_seconds`` is the whole call: load, prompt and generation."""

    model: str
    system: str
    user: str
    response_schema: Mapping[str, object]
    output_cap_tokens: int
    context_tokens: int
    think: bool
    timeout_seconds: float

    def __post_init__(self) -> None:
        _require_text(self.model, "model")
        if type(self.system) is not str or type(self.user) is not str:
            raise WorkerConfigError("system and user must be str")
        if not isinstance(self.response_schema, Mapping):
            raise WorkerConfigError("response_schema must be a mapping")
        _require_positive_int(self.output_cap_tokens, "output_cap_tokens")
        _require_positive_int(self.context_tokens, "context_tokens")
        _require_seconds(self.timeout_seconds, "timeout_seconds")


@dataclass(frozen=True, slots=True)
class InferenceReply:
    """The model's content parsed as a JSON object. Untrusted until interpreted."""

    payload: Mapping[str, object]
    prompt_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class InferenceFailed:
    failure: InferenceFailure


type InferenceResult = InferenceReply | InferenceFailed


class LocalInference(Protocol):
    """Local model adapter. Returns within ``call.timeout_seconds`` or reports a failure code."""

    def infer(self, call: InferenceCall, cancel: CancelSignal) -> InferenceResult: ...


@dataclass(frozen=True, slots=True)
class PreparedPrompt:
    """Deterministic prompt for one call; ``prompt_bound`` is an upper bound, never a character count."""

    system: str
    user: str
    response_schema: Mapping[str, object]
    prompt_bound: TokenBound


class PromptSource(Protocol):
    def prepare(self, version: OpportunityVersion, *, repair: bool) -> PreparedPrompt: ...


@dataclass(frozen=True, slots=True)
class Accepted[R]:
    value: R


class ResultInterpreter[R](Protocol):
    """Typed interpretation of untrusted model output; ``None`` rejects it."""

    def interpret(self, payload: Mapping[str, object]) -> Accepted[R] | None: ...


class InvocationLedger(Protocol):
    """Invocation claim/lease/fence operations; implementations raise ``InvocationError`` on storage failure."""

    def claim(self, identity: InvocationIdentity, model: str, lease_seconds: int) -> Claimed | Duplicate | Refused: ...

    def record_attempt(self, lease: Lease) -> TransitionStatus: ...

    def complete(self, lease: Lease) -> TransitionStatus: ...

    def release(self, lease: Lease, reason: ReleaseReason) -> TransitionStatus: ...

    def recover_expired(self) -> tuple[Lease, ...]: ...


@dataclass(frozen=True, slots=True)
class Publication[R]:
    """A result that may be published: completed before its deadline under a live lease."""

    item_id: str
    invocation_id: str
    profile_id: str
    model: str
    value: R


class SinkUnavailable(RuntimeError):
    """The sink could not persist a batch; nothing of that batch was written."""


class WorkerSink[R](Protocol):
    """Persists scheduler transitions (and a publication with them) atomically per call.

    Raises ``SinkUnavailable`` when nothing was written; the worker keeps the batch and
    retries it, in order, before doing anything else.
    """

    def record(self, transitions: tuple[Transition, ...], publication: Publication[R] | None) -> None: ...


# -- outcomes ----------------------------------------------------------------------------------


class SubmitRefusal(Enum):
    PROFILE_DISABLED = "profile_disabled"
    SINK_UNAVAILABLE = "sink_unavailable"
    RESTART_REQUIRED = "restart_required"
    SCHEDULER_ERROR = "scheduler_error"
    STOPPED = "stopped"


class WorkOutcome(Enum):
    IDLE = "idle"
    PUBLISHED = "published"
    ABORT_STALE = "abort_stale"  # deadline passed: nothing committed
    FENCED = "fenced"  # the lease was lost or expired: nothing committed
    FAILED = "failed"
    CANCELLED = "cancelled"  # stop requested
    CONTEXT_UNFIT = "context_unfit"
    CLAIM_DUPLICATE = "claim_duplicate"  # another holder has this identity active
    BUDGET_REFUSED = "budget_refused"
    CALL_REFUSED = "call_refused"
    PROFILE_DISABLED = "profile_disabled"
    RESTART_REQUIRED = "restart_required"
    SINK_UNAVAILABLE = "sink_unavailable"
    SCHEDULER_ERROR = "scheduler_error"
    LEDGER_ERROR = "ledger_error"
    STOPPED = "stopped"


class WatchdogAction(Enum):
    NONE = "none"
    CANCEL = "cancel"  # the call passed its limit: its cancel signal is set
    TERMINATE = "terminate"  # cancellation did not end it within the grace: terminate the process


class WorkerExit(Enum):
    STOPPED = "stopped"
    RESTART_REQUIRED = "restart_required"


@dataclass(frozen=True, slots=True)
class WorkReport:
    outcome: WorkOutcome
    item_id: str | None = None
    invocation_id: str | None = None
    final_state: ItemState | None = None
    calls: int = 0
    failures: tuple[InferenceFailure, ...] = ()
    call_refusal: CallRefusal | None = None
    lease_status: TransitionStatus | None = None
    ledger_failure: InvocationFailure | None = None


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    recovered: int
    released: int
    ledger_failure: InvocationFailure | None


# -- failure breaker ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailurePolicy:
    """``max_failures`` failed calls within ``window_seconds`` disable the profile until review."""

    max_failures: int = 2
    window_seconds: float = 600.0

    def __post_init__(self) -> None:
        _require_positive_int(self.max_failures, "max_failures")
        _require_seconds(self.window_seconds, "window_seconds")


class ProfileBreaker:
    """Counts failed calls on the monotonic clock; once tripped it stays open until ``review``."""

    def __init__(self, policy: FailurePolicy) -> None:
        if type(policy) is not FailurePolicy:
            raise WorkerConfigError("policy must be FailurePolicy")
        self._policy = policy
        self._failures: list[float] = []
        self._disabled = False

    @property
    def disabled(self) -> bool:
        return self._disabled

    def record_failure(self, at: float) -> bool:
        """Record one failure at monotonic time ``at``; True when the profile is (now) disabled."""
        window = self._policy.window_seconds
        self._failures = [moment for moment in self._failures if at - moment <= window]
        self._failures.append(at)
        if len(self._failures) >= self._policy.max_failures:
            self._disabled = True
        return self._disabled

    def review(self) -> None:
        self._disabled = False
        self._failures = []


# -- worker ------------------------------------------------------------------------------------


@dataclass(slots=True)
class _ActiveCall:
    started: float
    limit_seconds: float
    cancel: threading.Event
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class _Attempted[R]:
    """End of the call loop: either a value to commit, or the outcome that ends the item."""

    outcome: WorkOutcome
    calls: int
    failures: tuple[InferenceFailure, ...]
    value: Accepted[R] | None = None
    refusal: CallRefusal | None = None
    lease_status: TransitionStatus | None = None
    release_reason: ReleaseReason = ReleaseReason.FAILED


_DEADLINE_REFUSALS = frozenset({CallRefusal.DEADLINE_PASSED, CallRefusal.DEADLINE_UNFIT})


class InferenceWorker[R]:
    """One local inference worker: the scheduler, one profile, one running call at a time."""

    def __init__(
        self,
        *,
        policy: SchedulerPolicy,
        clock: WorkerClock,
        profile: InferenceProfile,
        inference: LocalInference,
        prompts: PromptSource,
        interpreter: ResultInterpreter[R],
        ledger: InvocationLedger,
        sink: WorkerSink[R],
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
        failure_policy: FailurePolicy | None = None,
    ) -> None:
        if type(profile) is not InferenceProfile:
            raise WorkerConfigError("profile must be InferenceProfile")
        self._scheduler = DeadlineScheduler(policy, clock)
        role_profile = policy.role_profiles[profile.role]
        if not role_profile.enabled:
            raise WorkerConfigError(f"{profile.role.value} is not enabled by the policy")
        if profile.hard_timeout_seconds > role_profile.hard_timeout_seconds:
            raise WorkerConfigError("profile hard timeout exceeds the policy's role hard timeout")
        if profile.output_cap_tokens > role_profile.output_cap_tokens:
            raise WorkerConfigError("profile output cap exceeds the policy's role output cap")
        grace = _require_seconds(cancel_grace_seconds, "cancel_grace_seconds")
        if grace > policy.finalization_reserve_seconds:
            raise WorkerConfigError("cancel_grace_seconds must fit inside the finalization reserve")
        self._policy = policy
        self._clock = clock
        self._profile = profile
        self._inference = inference
        self._prompts = prompts
        self._interpreter = interpreter
        self._ledger = ledger
        self._sink = sink
        self._grace = grace
        self._breaker = ProfileBreaker(failure_policy or FailurePolicy())
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._versions: dict[str, OpportunityVersion] = {}
        self._pending: list[tuple[tuple[Transition, ...], Publication[R] | None]] = []
        self._pending_fail: str | None = None
        self._active: _ActiveCall | None = None
        self._restart_required = False

    # -- state -----------------------------------------------------------------------------

    @property
    def profile(self) -> InferenceProfile:
        return self._profile

    @property
    def profile_disabled(self) -> bool:
        with self._lock:
            return self._breaker.disabled

    @property
    def restart_required(self) -> bool:
        with self._lock:
            return self._restart_required

    def pending_records(self) -> int:
        """Batches waiting for the sink (0 when everything is persisted)."""
        with self._lock:
            return len(self._pending)

    def review_profile(self) -> None:
        """Operator review: re-enable a profile the failure breaker disabled."""
        with self._lock:
            self._breaker.review()

    def request_stop(self) -> None:
        """Stop after the current item; a running call gets its cancel signal now."""
        with self._lock:
            self._stop.set()
            if self._active is not None:
                self._active.cancel.set()

    # -- sink ------------------------------------------------------------------------------

    def _flush_locked(self) -> bool:
        while self._pending:
            transitions, publication = self._pending[0]
            try:
                self._sink.record(transitions, publication)
            except SinkUnavailable:
                return False
            self._pending.pop(0)
        return True

    def _emit_locked(self, transitions: tuple[Transition, ...], publication: Publication[R] | None = None) -> bool:
        if transitions or publication is not None:
            self._pending.append((transitions, publication))
            for transition in transitions:
                if transition.state in TERMINAL_ITEM_STATES:
                    self._versions.pop(transition.item_id, None)
        return self._flush_locked()

    # -- admission (collection side) -------------------------------------------------------

    def submit(self, version: OpportunityVersion) -> AdmissionResult | SubmitRefusal:
        """Offer one sealed version. Never waits for a model call; returns a typed result."""
        with self._lock:
            if self._restart_required:
                return SubmitRefusal.RESTART_REQUIRED
            if self._stop.is_set():
                return SubmitRefusal.STOPPED
            if self._breaker.disabled:
                return SubmitRefusal.PROFILE_DISABLED
            if not self._flush_locked():
                return SubmitRefusal.SINK_UNAVAILABLE
            try:
                result = self._scheduler.admit(version)
            except SchedulerError:
                return SubmitRefusal.SCHEDULER_ERROR
            if result.status is AdmissionStatus.ADMITTED:
                self._versions[result.item_id] = version
            self._emit_locked(result.transitions)
            return result

    # -- dispatch (worker side) ------------------------------------------------------------

    def _fail_item_locked(self, item_id: str) -> ItemState | None:
        """Close the running item; if the scheduler cannot now, retry before the next dispatch."""
        try:
            result = self._scheduler.fail(item_id)
        except SchedulerError:
            self._pending_fail = item_id
            return None
        self._emit_locked(result.transitions)
        return result.state

    def process_next(self) -> WorkReport:
        """Dispatch and run at most one item. Never raises for an expected failure."""
        with self._lock:
            if self._restart_required:
                return WorkReport(WorkOutcome.RESTART_REQUIRED)
            if not self._flush_locked():
                return WorkReport(WorkOutcome.SINK_UNAVAILABLE)
            if self._pending_fail is not None:
                if self._fail_item_locked(self._pending_fail) is None:
                    return WorkReport(WorkOutcome.SCHEDULER_ERROR, item_id=self._pending_fail)
                self._pending_fail = None
            try:
                dispatch = self._scheduler.dispatch()
            except SchedulerError:
                return WorkReport(WorkOutcome.SCHEDULER_ERROR)
            self._emit_locked(dispatch.transitions)
            dispatched = dispatch.dispatched
            if dispatched is None:
                return WorkReport(WorkOutcome.IDLE)
            item_id = dispatched.item_id
            version = self._versions.get(item_id)
            stop_outcome: WorkOutcome | None = None
            if self._breaker.disabled:
                stop_outcome = WorkOutcome.PROFILE_DISABLED
            elif self._stop.is_set():
                stop_outcome = WorkOutcome.STOPPED
            elif version is None or dispatched.path != (self._profile.role,):
                stop_outcome = WorkOutcome.FAILED  # a path this worker's single profile cannot serve
            if stop_outcome is not None or version is None:
                outcome = stop_outcome or WorkOutcome.FAILED
                return WorkReport(outcome, item_id=item_id, final_state=self._fail_item_locked(item_id))
        return self._run_item(dispatched, version)

    def _lease_seconds(self, deadline: datetime) -> int:
        """The lease outlives the opportunity deadline by the reserve, so a crash is recoverable."""
        remaining = (deadline - self._clock.current()).total_seconds()
        seconds = math.ceil(max(remaining, 0.0)) + self._policy.finalization_reserve_seconds
        return min(max(seconds, MIN_LEASE_SECONDS), MAX_LEASE_SECONDS)

    def _run_item(self, dispatched: Dispatched, version: OpportunityVersion) -> WorkReport:
        item_id = dispatched.item_id
        try:
            claim = self._ledger.claim(version.identity, self._profile.model, self._lease_seconds(dispatched.deadline))
        except InvocationError as error:
            with self._lock:
                state = self._fail_item_locked(item_id)
            return WorkReport(WorkOutcome.LEDGER_ERROR, item_id=item_id, final_state=state, ledger_failure=error.code)
        if not isinstance(claim, Claimed):
            outcome = WorkOutcome.CLAIM_DUPLICATE if isinstance(claim, Duplicate) else WorkOutcome.BUDGET_REFUSED
            with self._lock:
                state = self._fail_item_locked(item_id)
            return WorkReport(outcome, item_id=item_id, final_state=state)
        lease = claim.lease
        try:
            attempted = self._attempts(item_id, version, lease)
        except SchedulerError:
            state = self._abandon(item_id, lease, ReleaseReason.FAILED)
            return WorkReport(WorkOutcome.SCHEDULER_ERROR, item_id, lease.invocation_id, state)
        except InvocationError as error:
            state = self._abandon(item_id, lease, ReleaseReason.FAILED)
            return WorkReport(WorkOutcome.LEDGER_ERROR, item_id, lease.invocation_id, state, ledger_failure=error.code)
        except BaseException:
            self._abandon(item_id, lease, ReleaseReason.FAILED)
            raise
        value = attempted.value
        if value is None:
            reason = attempted.release_reason
            if reason is ReleaseReason.FAILED and self._clock.current() >= dispatched.deadline:
                reason = ReleaseReason.DEADLINE_EXPIRED
            state = self._abandon(item_id, lease, reason)
            return WorkReport(
                attempted.outcome,
                item_id,
                lease.invocation_id,
                state,
                attempted.calls,
                attempted.failures,
                attempted.refusal,
                attempted.lease_status,
            )
        return self._commit(dispatched, lease, value, attempted)

    def _abandon(self, item_id: str, lease: Lease, reason: ReleaseReason) -> ItemState | None:
        """Release the lease (best effort: an unreleased lease expires and is recovered) and fail the item."""
        try:
            self._ledger.release(lease, reason)
        except InvocationError:
            pass  # the claim stays CLAIMED until its lease expires; recover() then releases it
        with self._lock:
            return self._fail_item_locked(item_id)

    def _attempts(self, item_id: str, version: OpportunityVersion, lease: Lease) -> _Attempted[R]:
        profile = self._profile
        calls = 0
        failures: list[InferenceFailure] = []
        repair = False
        while True:
            if self._stop.is_set():
                return _Attempted(WorkOutcome.CANCELLED, calls, tuple(failures), release_reason=ReleaseReason.CANCELLED)
            prepared = self._prompts.prepare(version, repair=repair)
            if prepared.prompt_bound.upper_bound + profile.output_cap_tokens > profile.context_tokens:
                return _Attempted(WorkOutcome.CONTEXT_UNFIT, calls, tuple(failures))
            with self._lock:
                decision = self._scheduler.authorize_call(item_id, profile.role, repair=repair, prompt=prepared.prompt_bound)
                self._emit_locked(decision.transitions)
            if decision.refusal is not None or decision.call_deadline is None:
                reason = ReleaseReason.DEADLINE_EXPIRED if decision.refusal in _DEADLINE_REFUSALS else ReleaseReason.FAILED
                outcome = WorkOutcome.FAILED if repair else WorkOutcome.CALL_REFUSED
                return _Attempted(outcome, calls, tuple(failures), refusal=decision.refusal, release_reason=reason)
            status = self._ledger.record_attempt(lease)
            if status is not TransitionStatus.APPLIED:
                outcome = WorkOutcome.BUDGET_REFUSED if status is TransitionStatus.BUDGET_EXHAUSTED else WorkOutcome.FENCED
                return _Attempted(outcome, calls, tuple(failures), lease_status=status)
            call_deadline = decision.call_deadline
            limit = min(float(profile.hard_timeout_seconds), (call_deadline - self._clock.current()).total_seconds())
            if limit <= 0:
                return _Attempted(
                    WorkOutcome.CALL_REFUSED, calls, tuple(failures), refusal=CallRefusal.DEADLINE_UNFIT,
                    release_reason=ReleaseReason.DEADLINE_EXPIRED,
                )
            call = InferenceCall(
                model=profile.model,
                system=prepared.system,
                user=prepared.user,
                response_schema=prepared.response_schema,
                output_cap_tokens=profile.output_cap_tokens,
                context_tokens=profile.context_tokens,
                think=profile.think,
                timeout_seconds=limit,
            )
            result, timed_out, restart = self._call(call)
            calls += 1
            if restart:
                failures.append(InferenceFailure.TIMEOUT)
                with self._lock:
                    self._breaker.record_failure(self._clock.monotonic())
                return _Attempted(WorkOutcome.RESTART_REQUIRED, calls, tuple(failures))
            if isinstance(result, InferenceFailed) and result.failure is InferenceFailure.CANCELLED and not timed_out:
                return _Attempted(WorkOutcome.CANCELLED, calls, tuple(failures), release_reason=ReleaseReason.CANCELLED)
            failure: InferenceFailure | None = None
            accepted: Accepted[R] | None = None
            if timed_out:
                failure = InferenceFailure.TIMEOUT
            elif isinstance(result, InferenceFailed):
                failure = result.failure
            elif self._clock.current() > call_deadline:
                failure = InferenceFailure.LATE
            else:
                accepted = self._interpreter.interpret(result.payload)
                if accepted is None:
                    failure = InferenceFailure.OUTPUT_REJECTED
            if failure is None:
                if accepted is not None:
                    return _Attempted(WorkOutcome.PUBLISHED, calls, tuple(failures), value=accepted)
                failure = InferenceFailure.OUTPUT_REJECTED  # unreachable: a None interpretation sets it above
            failures.append(failure)
            with self._lock:
                disabled = self._breaker.record_failure(self._clock.monotonic())
            repairable = failure in (InferenceFailure.MALFORMED, InferenceFailure.OUTPUT_REJECTED)
            if repairable and not repair and not disabled:
                repair = True  # one bounded repair; the scheduler still enforces every ceiling
                continue
            return _Attempted(WorkOutcome.FAILED, calls, tuple(failures))

    def _call(self, call: InferenceCall) -> tuple[InferenceResult, bool, bool]:
        """Run one adapter call under the watchdog. Returns (result, timed out, restart required)."""
        active = _ActiveCall(self._clock.monotonic(), call.timeout_seconds, threading.Event())
        with self._lock:
            if self._stop.is_set():
                active.cancel.set()
            self._active = active
        try:
            result: InferenceResult = self._inference.infer(call, active.cancel)
        except Exception:  # an adapter bug becomes a typed failure and a safe transition
            result = InferenceFailed(InferenceFailure.ADAPTER_ERROR)
        finally:
            with self._lock:
                self._active = None
                restart = self._restart_required
        elapsed = self._clock.monotonic() - active.started
        if not isinstance(result, (InferenceReply, InferenceFailed)):
            result = InferenceFailed(InferenceFailure.ADAPTER_ERROR)
        return result, active.timed_out or elapsed > call.timeout_seconds, restart

    def _commit(self, dispatched: Dispatched, lease: Lease, value: Accepted[R], attempted: _Attempted[R]) -> WorkReport:
        item_id = dispatched.item_id
        base = (attempted.calls, attempted.failures)
        if self._clock.current() >= dispatched.deadline:
            # Late result: never committed. The scheduler records ABORT_STALE (LATE_RESULT).
            try:
                self._ledger.release(lease, ReleaseReason.DEADLINE_EXPIRED)
            except InvocationError:
                pass  # recovered after lease expiry
            with self._lock:
                try:
                    late = self._scheduler.complete(item_id)
                except SchedulerError:
                    self._pending_fail = item_id
                    return WorkReport(WorkOutcome.ABORT_STALE, item_id, lease.invocation_id, None, *base)
                self._emit_locked(late.transitions)
            return WorkReport(WorkOutcome.ABORT_STALE, item_id, lease.invocation_id, late.state, *base)
        try:
            status = self._ledger.complete(lease)
        except InvocationError as error:
            state = self._abandon(item_id, lease, ReleaseReason.FAILED)
            return WorkReport(WorkOutcome.LEDGER_ERROR, item_id, lease.invocation_id, state, *base, ledger_failure=error.code)
        if status is not TransitionStatus.APPLIED:
            with self._lock:
                state = self._fail_item_locked(item_id)
            return WorkReport(WorkOutcome.FENCED, item_id, lease.invocation_id, state, *base, lease_status=status)
        with self._lock:
            try:
                completion = self._scheduler.complete(item_id)
            except SchedulerError:
                self._pending_fail = item_id
                return WorkReport(WorkOutcome.SCHEDULER_ERROR, item_id, lease.invocation_id, None, *base)
            publication: Publication[R] | None = None
            if completion.publishable:
                publication = Publication(item_id, lease.invocation_id, self._profile.profile_id, self._profile.model, value.value)
            self._emit_locked(completion.transitions, publication)
        outcome = WorkOutcome.PUBLISHED if completion.publishable else WorkOutcome.ABORT_STALE
        return WorkReport(outcome, item_id, lease.invocation_id, completion.state, *base, lease_status=status)

    # -- watchdog, recovery and loop -------------------------------------------------------

    def watchdog_tick(self) -> WatchdogAction:
        """Bounded cancellation: cancel at the limit, ask for termination after the grace."""
        with self._lock:
            active = self._active
            if active is None:
                return WatchdogAction.NONE
            elapsed = self._clock.monotonic() - active.started
            if elapsed >= active.limit_seconds + self._grace:
                active.timed_out = True
                active.cancel.set()
                self._restart_required = True
                return WatchdogAction.TERMINATE
            if elapsed >= active.limit_seconds:
                active.timed_out = True
                active.cancel.set()
                return WatchdogAction.CANCEL
            return WatchdogAction.NONE

    def watch(self, stop: CancelSignal, terminate: Callable[[], None], poll_seconds: float) -> WatchdogAction:
        """Watchdog loop for its own thread; calls ``terminate`` once when cancellation failed."""
        poll = _require_seconds(poll_seconds, "poll_seconds")
        while not stop.is_set():
            if self.watchdog_tick() is WatchdogAction.TERMINATE:
                terminate()
                return WatchdogAction.TERMINATE
            self._clock.sleep(poll)
        return WatchdogAction.NONE

    def recover(self) -> RecoveryReport:
        """Take over invocations whose lease expired (a crashed holder) and release them.

        Their queue state died with the old process, so they are never resumed: each is
        released as DEADLINE_EXPIRED (its lease outlived its deadline by the reserve).
        """
        try:
            leases = self._ledger.recover_expired()
        except InvocationError as error:
            return RecoveryReport(0, 0, error.code)
        released = 0
        for lease in leases:
            try:
                if self._ledger.release(lease, ReleaseReason.DEADLINE_EXPIRED) is TransitionStatus.APPLIED:
                    released += 1
            except InvocationError as error:
                return RecoveryReport(len(leases), released, error.code)
        return RecoveryReport(len(leases), released, None)

    def run(self, idle_seconds: float) -> WorkerExit:
        """Worker loop: recover, then process items until stopped or a restart is required."""
        idle = _require_seconds(idle_seconds, "idle_seconds")
        self.recover()
        waiting = frozenset(
            {WorkOutcome.IDLE, WorkOutcome.SINK_UNAVAILABLE, WorkOutcome.SCHEDULER_ERROR, WorkOutcome.LEDGER_ERROR}
        )
        while not self._stop.is_set():
            report = self.process_next()
            if report.outcome is WorkOutcome.RESTART_REQUIRED:
                return WorkerExit.RESTART_REQUIRED
            if report.outcome in waiting:
                self._clock.sleep(idle)
        return WorkerExit.STOPPED
