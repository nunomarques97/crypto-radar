"""OC-1 admission, queue and deadline policy (OC-1 sections 1 to 3).

Pure policy: no I/O, no database, no network, no wall-clock reads. The only effect
is the injected ``Clock`` port; every operation reads it once. Every state change is
returned to the caller as a ``Transition`` value (the caller persists it: the worker and the outbox);
nothing is written from here, so nothing can be half-written from here.

Rules (docs/OPERATING_CONTRACTS.md, OC-1):

* Absolute analysis deadline from the evidence seal, per horizon: 15m 60 s, 1h 120 s,
  4h and 24h 180 s. A re-observed version keeps the deadline of its origin seal. The
  deadline is computed once at admission and never extended after a queue wait, a
  repair or a re-observation.
* Maximum queued age per horizon: 15 s, 30 s, 45 s, counted from admission.
* One running inference and at most 16 queued opportunity versions.
* Identity is the ``InvocationIdentity`` of ``domain.invocation``: an identical active identity returns
  the existing ID; a different evidence hash is a different invocation.
* Ranking: deterministic opportunity-score band (high >= 70, eligible 50 to 69), then
  earliest deadline, then earliest seal, then stable ID. No model confidence exists in
  any type read here. Every fourth dispatch slot goes to the oldest admitted item of
  the lower band that still fits its deadline; an unused reserved slot returns to the
  higher band.
* Overflow: the new item is compared with the worst queued item by the same ranking;
  the loser gets ``DROPPED_BACKPRESSURE``. Expired work gets ``ABORT_STALE``, never a
  missing record.
* At most one queued version per venue/kind/instrument/setup/direction: a genuinely
  newer compatible version supersedes the queued one (``SUPERSEDED``, linked both ways).
* Admission and every model call use the profile hard timeout (Screener 30 s) plus a
  10 s finalization reserve. A path that does not fit falls back to the deterministic
  path when the caller says one is eligible, otherwise ``ABSTAIN``.
* Per-opportunity ceilings: at most 4 invocations, at most 1 repair (a repair is an
  invocation), 15,000 prompt tokens and 4,500 output tokens. Token amounts are upper
  bounds with a stated basis, never character counts presented as exact.
* Challenger and Deep Analyst are disabled in OC-1 production: a policy that enables
  them is refused, and requested disabled roles are dropped from the path.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from radar_v08.domain.integrity import InstrumentKind
from radar_v08.domain.invocation import Direction, InvocationIdentity

POLICY_VERSION = "OC-1"
MAX_TEXT_LENGTH = 200
# Upper bound of every configured duration (deadline, queued age, timeout, reserve).
# It bounds all datetime arithmetic: a seal is admitted only if seal + this still fits
# in a datetime, and a clock reading only if now + the policy span still fits.
MAX_POLICY_SECONDS = 86_400


class SchedulerFailure(Enum):
    INVALID_FIELD = "invalid_field"
    INVALID_POLICY = "invalid_policy"
    INVALID_CLOCK = "invalid_clock"
    CLOCK_WENT_BACKWARDS = "clock_went_backwards"
    UNKNOWN_ITEM = "unknown_item"


class SchedulerError(RuntimeError):
    """Nothing changed; ``code`` says why. Never a silent success.

    Every public operation validates everything that can raise (clock, arguments,
    item IDs, every datetime it will compute) before it records the clock reading or
    sweeps expired work, so an error never leaves a removed item without its
    ``ABORT_STALE`` record: the next call sweeps and returns it. After that commit
    point only exact built-in values (str, int, float, plain UTC datetime, enums) are
    compared, hashed or added, and every datetime sum is bounded in advance by
    ``MAX_POLICY_SECONDS`` (seals) or ``SchedulerPolicy.max_span_seconds`` (clock).
    """

    def __init__(self, code: SchedulerFailure, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class Horizon(Enum):
    MIN_15 = "15m"
    HOUR_1 = "1h"
    HOUR_4 = "4h"
    HOUR_24 = "24h"


class Band(Enum):
    HIGH = "high"
    ELIGIBLE = "eligible"


class Role(Enum):
    SCREENER = "screener"
    CHALLENGER = "challenger"
    DEEP_ANALYST = "deep_analyst"


# OC-1 §3: disabled until their quality/latency gates pass; no production routing.
PRODUCTION_DISABLED_ROLES: frozenset[Role] = frozenset({Role.CHALLENGER, Role.DEEP_ANALYST})


class ItemState(Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    DROPPED_BACKPRESSURE = "DROPPED_BACKPRESSURE"
    ABORT_STALE = "ABORT_STALE"
    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"
    ABSTAIN = "ABSTAIN"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    INTEGRITY_INVALID = "INTEGRITY_INVALID"
    INCOMPATIBLE_VERSION = "INCOMPATIBLE_VERSION"


ACTIVE_ITEM_STATES: frozenset[ItemState] = frozenset({ItemState.QUEUED, ItemState.RUNNING})
TERMINAL_ITEM_STATES: frozenset[ItemState] = frozenset(set(ItemState) - ACTIVE_ITEM_STATES)


class StaleReason(Enum):
    DEADLINE_PASSED = "deadline_passed"
    QUEUED_AGE_EXCEEDED = "queued_age_exceeded"
    LATE_RESULT = "late_result"


class AdmissionStatus(Enum):
    ADMITTED = "admitted"
    DUPLICATE = "duplicate"  # identical active identity: existing ID, nothing new
    REFUSED = "refused"  # the incoming item got a terminal state (in ``transitions``)


class IdleReason(Enum):
    BUSY = "busy"  # the one inference slot is occupied
    EMPTY = "empty"


class CallRefusal(Enum):
    NOT_RUNNING = "not_running"
    DEADLINE_PASSED = "deadline_passed"
    ROLE_DISABLED = "role_disabled"
    INPUT_OVER_ROLE_CAP = "input_over_role_cap"
    INVOCATION_CAP = "invocation_cap"
    REPAIR_CAP = "repair_cap"
    PROMPT_TOKEN_CAP = "prompt_token_cap"
    OUTPUT_TOKEN_CAP = "output_token_cap"
    DEADLINE_UNFIT = "deadline_unfit"


class TokenBasis(Enum):
    """How a token amount was obtained. There is deliberately no character-count basis."""

    TOKENIZER_COUNT = "tokenizer_count"  # the target model's own tokenizer
    UTF8_BYTE_UPPER_BOUND = "utf8_byte_upper_bound"  # conservative bound, see utf8_byte_upper_bound


class Clock(Protocol):
    """Injected time source; must return timezone-aware datetimes."""

    def current(self) -> datetime: ...


def _require_text(value: object, name: str) -> str:
    # Exact str only: a subclass could override hashing or comparison, which the
    # scheduler performs after its commit point.
    if type(value) is not str or not value or value != value.strip():
        raise SchedulerError(SchedulerFailure.INVALID_FIELD, f"{name} must be non-empty text without edge whitespace")
    if len(value) > MAX_TEXT_LENGTH or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SchedulerError(SchedulerFailure.INVALID_FIELD, f"{name} must be short text without control characters")
    return value


def _require_int(value: object, name: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}" if maximum is None else f"in {minimum}..{maximum}"
        raise SchedulerError(SchedulerFailure.INVALID_POLICY, f"{name} must be an int {bounds}")
    return value


def _require_aware(value: object, name: str, failure: SchedulerFailure) -> datetime:
    """A timezone-aware datetime, returned as a plain ``datetime`` in UTC.

    Rebuilt as the exact built-in type: nothing a caller-supplied subclass or tzinfo
    does can run after the scheduler's commit point. Offsets that push the value out
    of the datetime range are refused here, not discovered later.
    """
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SchedulerError(failure, f"{name} must be a timezone-aware datetime")
    try:
        if value.utcoffset() is None:
            raise SchedulerError(failure, f"{name} must be a timezone-aware datetime")
        moment = value.astimezone(UTC)
        return datetime(
            moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second, moment.microsecond, tzinfo=UTC
        )
    except (ArithmeticError, ValueError, TypeError):
        raise SchedulerError(failure, f"{name} is not representable in UTC") from None


def _require_room(moment: datetime, seconds: int, name: str, failure: SchedulerFailure) -> None:
    """Refuse ``moment`` when ``moment + seconds`` would leave the datetime range."""
    try:
        moment + timedelta(seconds=seconds)
    except OverflowError:
        raise SchedulerError(failure, f"{name} is too close to the end of the datetime range") from None


@dataclass(frozen=True, slots=True)
class TokenBound:
    """An upper bound on a token amount with its basis; never presented as a character count."""

    upper_bound: int
    basis: TokenBasis

    def __post_init__(self) -> None:
        if type(self.upper_bound) is not int or self.upper_bound < 0:
            raise SchedulerError(SchedulerFailure.INVALID_FIELD, "upper_bound must be a non-negative int")
        if not isinstance(self.basis, TokenBasis):
            raise SchedulerError(SchedulerFailure.INVALID_FIELD, "basis must be TokenBasis")


def utf8_byte_upper_bound(text: str, special_tokens: int) -> TokenBound:
    """Conservative token bound without a tokenizer: UTF-8 bytes plus template tokens.

    Byte-level BPE and byte-fallback tokenizers (the installed Qwen, gpt-oss and Llama
    families) emit tokens that each cover at least one byte of the text, so the text
    can never need more tokens than it has UTF-8 bytes. Special/template tokens cover
    no text bytes and are added explicitly by the caller.
    """
    if not isinstance(text, str):
        raise SchedulerError(SchedulerFailure.INVALID_FIELD, "text must be str")
    if type(special_tokens) is not int or special_tokens < 0:
        raise SchedulerError(SchedulerFailure.INVALID_FIELD, "special_tokens must be a non-negative int")
    return TokenBound(len(text.encode("utf-8")) + special_tokens, TokenBasis.UTF8_BYTE_UPPER_BOUND)


@dataclass(frozen=True, slots=True)
class HorizonLimits:
    analysis_deadline_seconds: int
    max_queued_age_seconds: int


@dataclass(frozen=True, slots=True)
class RoleProfile:
    """Hard call limit (includes load, prompt processing and generation) and token caps."""

    hard_timeout_seconds: int
    max_input_tokens: int
    output_cap_tokens: int
    enabled: bool


OC1_HORIZON_LIMITS: Mapping[Horizon, HorizonLimits] = MappingProxyType(
    {
        Horizon.MIN_15: HorizonLimits(analysis_deadline_seconds=60, max_queued_age_seconds=15),
        Horizon.HOUR_1: HorizonLimits(analysis_deadline_seconds=120, max_queued_age_seconds=30),
        Horizon.HOUR_4: HorizonLimits(analysis_deadline_seconds=180, max_queued_age_seconds=45),
        Horizon.HOUR_24: HorizonLimits(analysis_deadline_seconds=180, max_queued_age_seconds=45),
    }
)

OC1_ROLE_PROFILES: Mapping[Role, RoleProfile] = MappingProxyType(
    {
        Role.SCREENER: RoleProfile(hard_timeout_seconds=30, max_input_tokens=2800, output_cap_tokens=768, enabled=True),
        Role.CHALLENGER: RoleProfile(hard_timeout_seconds=45, max_input_tokens=5600, output_cap_tokens=1536, enabled=False),
        Role.DEEP_ANALYST: RoleProfile(hard_timeout_seconds=90, max_input_tokens=5600, output_cap_tokens=2048, enabled=False),
    }
)


@dataclass(frozen=True, slots=True)
class SchedulerPolicy:
    """OC-1 limits. Every field is validated; a perturbed value changes behaviour."""

    max_queued: int = 16
    high_band_min: float = 70.0
    eligible_band_min: float = 50.0
    fair_slot_every: int = 4
    finalization_reserve_seconds: int = 10
    max_invocations: int = 4
    max_repairs: int = 1
    max_prompt_tokens: int = 15000
    max_output_tokens: int = 4500
    horizon_limits: Mapping[Horizon, HorizonLimits] = field(default=OC1_HORIZON_LIMITS)
    role_profiles: Mapping[Role, RoleProfile] = field(default=OC1_ROLE_PROFILES)

    def __post_init__(self) -> None:
        _require_int(self.max_queued, "max_queued", 1)
        _require_int(self.fair_slot_every, "fair_slot_every", 2)
        _require_int(self.finalization_reserve_seconds, "finalization_reserve_seconds", 0, MAX_POLICY_SECONDS)
        _require_int(self.max_invocations, "max_invocations", 0)
        _require_int(self.max_repairs, "max_repairs", 0)
        _require_int(self.max_prompt_tokens, "max_prompt_tokens", 0)
        _require_int(self.max_output_tokens, "max_output_tokens", 0)
        for name, bound in (("high_band_min", self.high_band_min), ("eligible_band_min", self.eligible_band_min)):
            if type(bound) not in (int, float) or not math.isfinite(bound):
                raise SchedulerError(SchedulerFailure.INVALID_POLICY, f"{name} must be a finite number")
        if not 0 <= self.eligible_band_min < self.high_band_min <= 100:
            raise SchedulerError(SchedulerFailure.INVALID_POLICY, "bands need 0 <= eligible_band_min < high_band_min <= 100")
        if set(self.horizon_limits) != set(Horizon):
            raise SchedulerError(SchedulerFailure.INVALID_POLICY, "horizon_limits must cover every Horizon")
        for horizon, limits in self.horizon_limits.items():
            if type(limits) is not HorizonLimits:
                raise SchedulerError(SchedulerFailure.INVALID_POLICY, f"{horizon.value} limits must be HorizonLimits")
            _require_int(limits.analysis_deadline_seconds, f"{horizon.value} deadline", 1, MAX_POLICY_SECONDS)
            _require_int(limits.max_queued_age_seconds, f"{horizon.value} queued age", 0, MAX_POLICY_SECONDS)
        if set(self.role_profiles) != set(Role):
            raise SchedulerError(SchedulerFailure.INVALID_POLICY, "role_profiles must cover every Role")
        for role, profile in self.role_profiles.items():
            if type(profile) is not RoleProfile or not isinstance(profile.enabled, bool):
                raise SchedulerError(SchedulerFailure.INVALID_POLICY, f"{role.value} profile must be RoleProfile")
            _require_int(profile.hard_timeout_seconds, f"{role.value} hard timeout", 1, MAX_POLICY_SECONDS)
            _require_int(profile.max_input_tokens, f"{role.value} input cap", 0)
            _require_int(profile.output_cap_tokens, f"{role.value} output cap", 0)
            if role in PRODUCTION_DISABLED_ROLES and profile.enabled:
                raise SchedulerError(SchedulerFailure.INVALID_POLICY, f"{role.value} is disabled in OC-1 production")
        # Frozen read-only copies: a caller's dict mutated later cannot change the policy.
        object.__setattr__(self, "horizon_limits", MappingProxyType(dict(self.horizon_limits)))
        object.__setattr__(self, "role_profiles", MappingProxyType(dict(self.role_profiles)))

    @property
    def max_span_seconds(self) -> int:
        """Largest amount ever added to a clock reading: every role's hard timeout plus the
        reserve (a path repeats no role), or the longest deadline if that is larger."""
        path_span = sum(profile.hard_timeout_seconds for profile in self.role_profiles.values())
        path_span += self.finalization_reserve_seconds
        return max(path_span, max(limits.analysis_deadline_seconds for limits in self.horizon_limits.values()))

    def band(self, score: float) -> Band | None:
        if score >= self.high_band_min:
            return Band.HIGH
        if score >= self.eligible_band_min:
            return Band.ELIGIBLE
        return None


@dataclass(frozen=True, slots=True)
class OpportunityVersion:
    """One sealed opportunity version offered for model analysis.

    ``opportunity_score`` is the deterministic score (0 to 100). There is no model
    confidence field: nothing a model says can reach the ranking. ``origin_sealed_at``
    is the seal time of the original version when this one is a re-observation; its
    deadline stays the original one.
    """

    item_id: str
    identity: InvocationIdentity
    horizon: Horizon
    opportunity_score: float
    sealed_at: datetime
    integrity_valid: bool
    deterministic_path_eligible: bool
    requested_roles: tuple[Role, ...] = (Role.SCREENER,)
    origin_sealed_at: datetime | None = None

    def __post_init__(self) -> None:
        invalid = SchedulerFailure.INVALID_FIELD
        _require_text(self.item_id, "item_id")
        # Exact type: the scheduler hashes and compares identities after its commit point.
        if type(self.identity) is not InvocationIdentity:
            raise SchedulerError(invalid, "identity must be InvocationIdentity")
        for name in ("venue", "native_instrument", "setup", "evidence_hash", "policy_version"):
            if type(getattr(self.identity, name)) is not str:
                raise SchedulerError(invalid, f"identity.{name} must be plain text")
        if not isinstance(self.horizon, Horizon):
            raise SchedulerError(invalid, "horizon must be Horizon")
        score = self.opportunity_score
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100:
            raise SchedulerError(invalid, "opportunity_score must be a finite number in 0..100")
        sealed_at = _require_aware(self.sealed_at, "sealed_at", invalid)
        # Every deadline is seal + at most MAX_POLICY_SECONDS: it must be representable.
        _require_room(sealed_at, MAX_POLICY_SECONDS, "sealed_at", invalid)
        object.__setattr__(self, "sealed_at", sealed_at)
        if not isinstance(self.integrity_valid, bool) or not isinstance(self.deterministic_path_eligible, bool):
            raise SchedulerError(invalid, "integrity_valid and deterministic_path_eligible must be bool")
        roles = self.requested_roles
        if type(roles) is not tuple or not roles or not all(isinstance(role, Role) for role in roles):
            raise SchedulerError(invalid, "requested_roles must be a non-empty tuple of Role")
        if len(set(roles)) != len(roles):
            raise SchedulerError(invalid, "requested_roles must not repeat a role")
        if self.origin_sealed_at is not None:
            origin = _require_aware(self.origin_sealed_at, "origin_sealed_at", invalid)
            if origin > sealed_at:
                raise SchedulerError(invalid, "origin_sealed_at cannot be after sealed_at")
            object.__setattr__(self, "origin_sealed_at", origin)

    @property
    def group_key(self) -> tuple[str, InstrumentKind, str, str, Direction]:
        """Instrument/setup/direction slot: at most one queued version per key."""
        identity = self.identity
        return (identity.venue, identity.market_kind, identity.native_instrument, identity.setup, identity.direction)


@dataclass(frozen=True, slots=True)
class Transition:
    """One state change for the caller to persist. Codes only, no free text."""

    item_id: str
    state: ItemState
    at: datetime
    related_id: str | None = None  # the other side of SUPERSEDED / DROPPED_BACKPRESSURE
    stale_reason: StaleReason | None = None


@dataclass(frozen=True, slots=True)
class QueuedEntry:
    version: OpportunityVersion
    band: Band
    deadline: datetime
    enqueued_at: datetime
    path: tuple[Role, ...]
    supersedes: str | None


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    status: AdmissionStatus
    item_id: str  # for DUPLICATE: the existing active item's ID
    state: ItemState
    path: tuple[Role, ...]
    dropped_roles: tuple[Role, ...]
    deadline: datetime | None
    transitions: tuple[Transition, ...]


@dataclass(frozen=True, slots=True)
class Dispatched:
    item_id: str
    path: tuple[Role, ...]
    deadline: datetime
    slot_number: int
    fair_slot: bool  # True when the reserved lower-band slot was used for this item


@dataclass(frozen=True, slots=True)
class DispatchResult:
    dispatched: Dispatched | None
    idle: IdleReason | None
    transitions: tuple[Transition, ...]


@dataclass(frozen=True, slots=True)
class CallUsage:
    invocations: int
    repairs: int
    prompt_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class CallDecision:
    """``allowed`` only when ``refusal`` is None. ``call_deadline`` is the hard stop of the call."""

    item_id: str
    refusal: CallRefusal | None
    call_deadline: datetime | None
    usage: CallUsage
    transitions: tuple[Transition, ...]

    @property
    def allowed(self) -> bool:
        return self.refusal is None


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """``publishable`` is False for anything that is not a completion before the deadline."""

    item_id: str
    state: ItemState
    publishable: bool
    transitions: tuple[Transition, ...]


@dataclass(slots=True)
class _Running:
    entry: QueuedEntry
    dispatched_at: datetime
    invocations: int = 0
    repairs: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    aborted: bool = False  # ABORT_STALE already recorded; the slot stays held until the worker lets go

    def usage(self) -> CallUsage:
        return CallUsage(self.invocations, self.repairs, self.prompt_tokens, self.output_tokens)


def _rank_key(entry: QueuedEntry) -> tuple[int, datetime, datetime, str]:
    band_order = 0 if entry.band is Band.HIGH else 1
    return (band_order, entry.deadline, entry.version.sealed_at, entry.version.item_id)


def _fair_key(entry: QueuedEntry) -> tuple[datetime, datetime, str]:
    return (entry.enqueued_at, entry.version.sealed_at, entry.version.item_id)


class DeadlineScheduler:
    """In-memory OC-1 scheduler state. One instance per worker; the caller persists transitions."""

    def __init__(self, policy: SchedulerPolicy, clock: Clock) -> None:
        if type(policy) is not SchedulerPolicy:
            raise SchedulerError(SchedulerFailure.INVALID_POLICY, "policy must be SchedulerPolicy")
        self._policy = policy
        self._clock = clock
        self._last_now: datetime | None = None
        self._queue: dict[str, QueuedEntry] = {}
        self._by_identity: dict[InvocationIdentity, str] = {}
        self._by_group: dict[tuple[str, InstrumentKind, str, str, Direction], str] = {}
        self._running: _Running | None = None
        self._slots_used = 0

    @property
    def policy(self) -> SchedulerPolicy:
        return self._policy

    # -- clock -----------------------------------------------------------------

    def _read_clock(self) -> datetime:
        """Read and validate the clock without recording it (a later raise changes nothing).

        A reading is refused unless ``now + policy.max_span_seconds`` is representable:
        every sum computed from ``now`` after the commit point is at most that span.
        """
        moment = _require_aware(self._clock.current(), "clock", SchedulerFailure.INVALID_CLOCK)
        _require_room(moment, self._policy.max_span_seconds, "clock", SchedulerFailure.INVALID_CLOCK)
        if self._last_now is not None and moment < self._last_now:
            raise SchedulerError(SchedulerFailure.CLOCK_WENT_BACKWARDS, "clock moved backwards; admissions suspended")
        return moment

    def _begin(self, moment: datetime) -> list[Transition]:
        """Commit point of every public operation: nothing after this may raise."""
        self._last_now = moment
        return self._sweep(moment)

    # -- helpers ---------------------------------------------------------------

    def _deadline(self, version: OpportunityVersion) -> datetime:
        """Called before the commit point; the seal check makes the sum representable."""
        origin = version.origin_sealed_at or version.sealed_at
        try:
            return origin + timedelta(seconds=self._policy.horizon_limits[version.horizon].analysis_deadline_seconds)
        except OverflowError:  # only reachable if a frozen version was tampered with after validation
            raise SchedulerError(SchedulerFailure.INVALID_FIELD, "deadline is not representable") from None

    def _enabled_path(self, roles: Iterable[Role]) -> tuple[tuple[Role, ...], tuple[Role, ...]]:
        kept = tuple(role for role in roles if self._policy.role_profiles[role].enabled)
        dropped = tuple(role for role in roles if not self._policy.role_profiles[role].enabled)
        return kept, dropped

    def _path_fits(self, path: tuple[Role, ...], now: datetime, deadline: datetime) -> bool:
        policy = self._policy
        if not path or len(path) > policy.max_invocations:
            return False
        profiles = [policy.role_profiles[role] for role in path]
        if sum(profile.max_input_tokens for profile in profiles) > policy.max_prompt_tokens:
            return False
        if sum(profile.output_cap_tokens for profile in profiles) > policy.max_output_tokens:
            return False
        seconds = sum(profile.hard_timeout_seconds for profile in profiles) + policy.finalization_reserve_seconds
        return now + timedelta(seconds=seconds) <= deadline

    def _unfit_state(self, version: OpportunityVersion) -> ItemState:
        return ItemState.DETERMINISTIC_ONLY if version.deterministic_path_eligible else ItemState.ABSTAIN

    def _remove(self, item_id: str) -> QueuedEntry:
        entry = self._queue.pop(item_id)
        del self._by_identity[entry.version.identity]
        if self._by_group.get(entry.version.group_key) == item_id:
            del self._by_group[entry.version.group_key]
        return entry

    def _insert(self, entry: QueuedEntry) -> None:
        item_id = entry.version.item_id
        self._queue[item_id] = entry
        self._by_identity[entry.version.identity] = item_id
        self._by_group[entry.version.group_key] = item_id

    def _sweep(self, now: datetime) -> list[Transition]:
        """Expired work becomes ABORT_STALE; work whose path no longer fits leaves the queue."""
        out: list[Transition] = []
        running = self._running
        if running is not None and not running.aborted and now >= running.entry.deadline:
            running.aborted = True
            out.append(Transition(running.entry.version.item_id, ItemState.ABORT_STALE, now, stale_reason=StaleReason.DEADLINE_PASSED))
        for entry in sorted(self._queue.values(), key=_rank_key):
            item_id = entry.version.item_id
            max_age = self._policy.horizon_limits[entry.version.horizon].max_queued_age_seconds
            if now >= entry.deadline:
                self._remove(item_id)
                out.append(Transition(item_id, ItemState.ABORT_STALE, now, stale_reason=StaleReason.DEADLINE_PASSED))
            elif now - entry.enqueued_at > timedelta(seconds=max_age):
                self._remove(item_id)
                out.append(Transition(item_id, ItemState.ABORT_STALE, now, stale_reason=StaleReason.QUEUED_AGE_EXCEEDED))
            elif not self._path_fits(entry.path, now, entry.deadline):
                self._remove(item_id)
                out.append(Transition(item_id, self._unfit_state(entry.version), now))
        return out

    def _active_identity(self, item_id: str) -> InvocationIdentity | None:
        if item_id in self._queue:
            return self._queue[item_id].version.identity
        if self._running is not None and self._running.entry.version.item_id == item_id:
            return self._running.entry.version.identity
        return None

    def _require_running(self, item_id: str) -> _Running:
        running = self._running
        if running is None or running.entry.version.item_id != item_id:
            raise SchedulerError(SchedulerFailure.UNKNOWN_ITEM, "no running item with this id")
        return running

    # -- public API --------------------------------------------------------------

    def sweep(self) -> tuple[Transition, ...]:
        return tuple(self._begin(self._read_clock()))

    def queued(self) -> tuple[QueuedEntry, ...]:
        """Queued entries in ranking order (best first)."""
        return tuple(sorted(self._queue.values(), key=_rank_key))

    def running_item(self) -> str | None:
        return None if self._running is None else self._running.entry.version.item_id

    def admit(self, version: OpportunityVersion) -> AdmissionResult:
        if type(version) is not OpportunityVersion:
            raise SchedulerError(SchedulerFailure.INVALID_FIELD, "version must be OpportunityVersion")
        now = self._read_clock()
        item_id = version.item_id
        # Everything that can raise happens before the sweep (the commit point): an
        # item_id naming a different active item, and the pure path/deadline computation.
        current_identity = self._active_identity(item_id)
        if current_identity is not None and current_identity != version.identity:
            raise SchedulerError(SchedulerFailure.INVALID_FIELD, "item_id already names another active item")
        path, dropped = self._enabled_path(version.requested_roles)
        deadline = self._deadline(version)
        out = self._begin(now)
        policy = self._policy
        if current_identity is not None and self._active_identity(item_id) is None:
            # A replay of an item the sweep just closed: its record is the sweep's, never a second one.
            # (Only a queued item can be closed by the sweep, and it always emits a record.)
            closed = next((t.state for t in out if t.item_id == item_id), ItemState.ABORT_STALE)
            return AdmissionResult(AdmissionStatus.REFUSED, item_id, closed, (), (), None, tuple(out))

        def refused(state: ItemState, related: str | None = None, reason: StaleReason | None = None) -> AdmissionResult:
            out.append(Transition(item_id, state, now, related_id=related, stale_reason=reason))
            return AdmissionResult(AdmissionStatus.REFUSED, item_id, state, (), dropped, deadline, tuple(out))

        # 1. identical active identity: the same invocation, nothing new recorded.
        existing = self._by_identity.get(version.identity)
        state = ItemState.QUEUED
        running = self._running
        if existing is None and running is not None and running.entry.version.identity == version.identity:
            existing = running.entry.version.item_id
            # Its ABORT_STALE is already recorded when aborted: report that, not RUNNING.
            state = ItemState.ABORT_STALE if running.aborted else ItemState.RUNNING
        if existing is not None:
            return AdmissionResult(AdmissionStatus.DUPLICATE, existing, state, (), dropped, None, tuple(out))
        # The pre-sweep check guarantees item_id names no other active item here: the
        # sweep only removes items, and a same-identity item_id was caught just above.

        # 2. invalid, not eligible, expired, or no bounded path.
        if not version.integrity_valid:
            return refused(ItemState.INTEGRITY_INVALID)
        band = policy.band(version.opportunity_score)
        if band is None:
            return refused(ItemState.NOT_ELIGIBLE)
        if now >= deadline:
            return refused(ItemState.ABORT_STALE, reason=StaleReason.DEADLINE_PASSED)
        if not self._path_fits(path, now, deadline):
            return refused(self._unfit_state(version))

        # 3. one queued version per instrument/setup/direction.
        supersedes: str | None = None
        incumbent_id = self._by_group.get(version.group_key)
        if incumbent_id is not None:
            incumbent = self._queue[incumbent_id]
            compatible = (
                incumbent.version.identity.policy_version == version.identity.policy_version
                and incumbent.version.horizon == version.horizon
            )
            if not compatible:
                return refused(ItemState.INCOMPATIBLE_VERSION, related=incumbent_id)
            if version.sealed_at <= incumbent.version.sealed_at:
                return refused(ItemState.SUPERSEDED, related=incumbent_id)
            self._remove(incumbent_id)
            out.append(Transition(incumbent_id, ItemState.SUPERSEDED, now, related_id=item_id))
            supersedes = incumbent_id

        entry = QueuedEntry(version, band, deadline, now, path, supersedes)

        # 4. bounded queue: the loser against the worst queued item is dropped.
        if len(self._queue) >= policy.max_queued:
            worst = max(self._queue.values(), key=_rank_key)
            worst_id = worst.version.item_id
            if _rank_key(entry) < _rank_key(worst):
                self._remove(worst_id)
                out.append(Transition(worst_id, ItemState.DROPPED_BACKPRESSURE, now, related_id=item_id))
            else:
                return refused(ItemState.DROPPED_BACKPRESSURE, related=worst_id)

        self._insert(entry)
        out.append(Transition(item_id, ItemState.QUEUED, now, related_id=supersedes))
        return AdmissionResult(AdmissionStatus.ADMITTED, item_id, ItemState.QUEUED, path, dropped, deadline, tuple(out))

    def dispatch(self) -> DispatchResult:
        now = self._read_clock()
        out = self._begin(now)
        if self._running is not None:
            return DispatchResult(None, IdleReason.BUSY, tuple(out))
        if not self._queue:
            return DispatchResult(None, IdleReason.EMPTY, tuple(out))
        slot = self._slots_used + 1
        chosen: QueuedEntry | None = None
        fair = False
        if slot % self._policy.fair_slot_every == 0:
            lower = [
                entry
                for entry in self._queue.values()
                if entry.band is Band.ELIGIBLE and self._path_fits(entry.path, now, entry.deadline)
            ]
            if lower:
                chosen = min(lower, key=_fair_key)
                fair = True
        if chosen is None:  # ordinary slot, or an unused reserved slot returned to the higher band
            chosen = min(self._queue.values(), key=_rank_key)
        self._slots_used = slot
        self._remove(chosen.version.item_id)
        self._running = _Running(chosen, now)
        out.append(Transition(chosen.version.item_id, ItemState.RUNNING, now))
        return DispatchResult(Dispatched(chosen.version.item_id, chosen.path, chosen.deadline, slot, fair), None, tuple(out))

    def authorize_call(self, item_id: str, role: Role, *, repair: bool, prompt: TokenBound) -> CallDecision:
        """Reserve one model call for the running item, or refuse it with a typed reason.

        Reserved amounts are never returned: the prompt bound and the role's full output
        cap count against the per-opportunity ceilings whether or not the call succeeds.
        """
        _require_text(item_id, "item_id")
        if not isinstance(role, Role) or not isinstance(repair, bool) or type(prompt) is not TokenBound:
            raise SchedulerError(SchedulerFailure.INVALID_FIELD, "role, repair and prompt must be Role, bool and TokenBound")
        now = self._read_clock()
        out = self._begin(now)
        running = self._running
        if running is None or running.entry.version.item_id != item_id:
            return CallDecision(item_id, CallRefusal.NOT_RUNNING, None, CallUsage(0, 0, 0, 0), tuple(out))
        policy = self._policy
        profile = policy.role_profiles[role]

        def refuse(reason: CallRefusal) -> CallDecision:
            return CallDecision(item_id, reason, None, running.usage(), tuple(out))

        if running.aborted:  # ABORT_STALE already recorded (now or earlier)
            return refuse(CallRefusal.DEADLINE_PASSED)
        if not profile.enabled:
            return refuse(CallRefusal.ROLE_DISABLED)
        if prompt.upper_bound > profile.max_input_tokens:
            return refuse(CallRefusal.INPUT_OVER_ROLE_CAP)
        if running.invocations + 1 > policy.max_invocations:
            return refuse(CallRefusal.INVOCATION_CAP)
        if repair and running.repairs + 1 > policy.max_repairs:
            return refuse(CallRefusal.REPAIR_CAP)
        if running.prompt_tokens + prompt.upper_bound > policy.max_prompt_tokens:
            return refuse(CallRefusal.PROMPT_TOKEN_CAP)
        if running.output_tokens + profile.output_cap_tokens > policy.max_output_tokens:
            return refuse(CallRefusal.OUTPUT_TOKEN_CAP)
        call_deadline = now + timedelta(seconds=profile.hard_timeout_seconds)
        if call_deadline + timedelta(seconds=policy.finalization_reserve_seconds) > running.entry.deadline:
            return refuse(CallRefusal.DEADLINE_UNFIT)
        running.invocations += 1
        running.repairs += 1 if repair else 0
        running.prompt_tokens += prompt.upper_bound
        running.output_tokens += profile.output_cap_tokens
        return CallDecision(item_id, None, call_deadline, running.usage(), tuple(out))

    def complete(self, item_id: str) -> CompletionResult:
        """The worker returned a result. Only a result before the deadline is publishable."""
        _require_text(item_id, "item_id")
        now = self._read_clock()
        running = self._require_running(item_id)  # raises before any state changes
        self._last_now = now
        out: list[Transition] = []
        if not running.aborted and now >= running.entry.deadline:
            running.aborted = True  # the result arrived after the deadline: it can never commit
            out.append(Transition(item_id, ItemState.ABORT_STALE, now, stale_reason=StaleReason.LATE_RESULT))
        out.extend(self._sweep(now))
        self._running = None  # the sweep never clears the running slot
        if running.aborted:
            return CompletionResult(item_id, ItemState.ABORT_STALE, False, tuple(out))
        out.append(Transition(item_id, ItemState.COMPLETED, now))
        return CompletionResult(item_id, ItemState.COMPLETED, True, tuple(out))

    def fail(self, item_id: str) -> CompletionResult:
        """The worker gave up (error, cancellation acknowledged). Frees the slot."""
        _require_text(item_id, "item_id")
        now = self._read_clock()
        running = self._require_running(item_id)  # raises before any state changes
        out = self._begin(now)
        self._running = None  # the sweep never clears the running slot
        if running.aborted:
            return CompletionResult(item_id, ItemState.ABORT_STALE, False, tuple(out))
        out.append(Transition(item_id, ItemState.FAILED, now))
        return CompletionResult(item_id, ItemState.FAILED, False, tuple(out))
