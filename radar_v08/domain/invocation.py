"""Invocation identity, budget reservation and lease fencing (T031a, OC-1 §2).

An invocation is one planned piece of model work for one opportunity version. Its
identity is exactly the OC-1 §2 tuple ``(venue, market_kind, native_instrument, setup,
direction, evidence_hash, policy_version)``. Two requests with the same tuple are the
same invocation while one of them is active: the second gets the existing ID and no
new budget reservation. A different ``evidence_hash`` is a different invocation.
Exact hashes only: no approximate or semantic merging.

The store adapter (``radar_v08.adapters.invocation_store``) persists these types. This
module is pure: no I/O, no database, no wall-clock reads. Time is always passed in and
must be timezone-aware.

Counters kept apart on purpose:

* **demand**: every request observed, including duplicates and refused requests;
* **reserved**: budget units taken by winning claims (hour and day windows per model).
  The claim's unit covers the first genuine attempt; every further attempt on the same
  invocation (retry after crash recovery, repair) reserves one more unit or is refused.
  A reservation is never returned, so reserved >= attempts always holds: the budget can
  be over-reserved, never over-spent;
* **attempts**: genuine model calls recorded by the lease holder before the call is
  made. Never decremented.

Lease fencing: every claim carries a generation. Crash recovery of an expired lease
increments the generation and hands the lease to a new owner. A holder whose
generation, owner or lease no longer matches gets a typed ``TransitionStatus`` and
changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from radar_v08.domain.integrity import InstrumentKind

HASH_PREFIX = "sha256:"
_HEX = frozenset("0123456789abcdef")
MAX_TEXT_LENGTH = 200
MIN_LEASE_SECONDS = 1
MAX_LEASE_SECONDS = 3600
# Bounded retry schedule for a busy or locked database (seconds between attempts).
BUSY_RETRY_DELAYS: tuple[float, ...] = (0.05, 0.1, 0.2)


class Direction(Enum):
    """Mirrors ``radar_v08.setups.DIRECTIONS``; opposing directions are separate hypotheses."""

    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class InvocationState(Enum):
    CLAIMED = "CLAIMED"  # the only active state: holds the unique identity and a lease
    COMPLETED = "COMPLETED"
    RELEASED = "RELEASED"


ACTIVE_STATES: frozenset[InvocationState] = frozenset({InvocationState.CLAIMED})


class ReleaseReason(Enum):
    CANCELLED = "CANCELLED"
    DEADLINE_EXPIRED = "DEADLINE_EXPIRED"
    FAILED = "FAILED"


class WindowKind(Enum):
    HOUR = "hour"
    DAY = "day"


class RefusalReason(Enum):
    HOURLY_BUDGET_EXHAUSTED = "hourly_budget_exhausted"
    DAILY_BUDGET_EXHAUSTED = "daily_budget_exhausted"


class TransitionStatus(Enum):
    """Outcome of a lease-holder action. Only ``APPLIED`` changed anything."""

    APPLIED = "applied"
    FENCED = "fenced"  # stale generation or another owner: a newer holder exists
    LEASE_EXPIRED = "lease_expired"  # still this holder's generation, but the lease ran out
    NOT_ACTIVE = "not_active"  # the invocation is already completed or released
    NOT_FOUND = "not_found"
    BUDGET_EXHAUSTED = "budget_exhausted"  # a further attempt needs a reservation that does not fit


class InvocationFailure(Enum):
    INVALID_FIELD = "invalid_field"
    INVALID_CLOCK = "invalid_clock"
    OPEN_TRANSACTION = "open_transaction"
    SCHEMA_NOT_MIGRATED = "schema_not_migrated"
    BUSY = "busy"  # database stayed busy/locked through every bounded retry
    CORRUPT_ROW = "corrupt_row"
    STORAGE_ERROR = "storage_error"  # any other database error, rolled back


class InvocationError(RuntimeError):
    """Nothing was written (or it was rolled back); ``code`` says why. Never a silent success."""

    def __init__(self, code: InvocationFailure, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise InvocationError(InvocationFailure.INVALID_FIELD, f"{field} must be text")
    if not value or value != value.strip():
        raise InvocationError(InvocationFailure.INVALID_FIELD, f"{field} must be non-empty without edge whitespace")
    if len(value) > MAX_TEXT_LENGTH:
        raise InvocationError(InvocationFailure.INVALID_FIELD, f"{field} is longer than {MAX_TEXT_LENGTH}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InvocationError(InvocationFailure.INVALID_FIELD, f"{field} contains control characters")
    return value


def _require_enum[E: Enum](enum_type: type[E], value: object, field: str) -> E:
    if not isinstance(value, enum_type):
        raise InvocationError(InvocationFailure.INVALID_FIELD, f"{field} must be {enum_type.__name__}")
    return value


def _require_count(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvocationError(InvocationFailure.INVALID_FIELD, f"{field} must be a non-negative int")
    return value


def require_aware(moment: object, field: str = "now") -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise InvocationError(InvocationFailure.INVALID_CLOCK, f"{field} must be a timezone-aware datetime")
    return moment


def require_owner(owner: object) -> str:
    """A lease owner is plain bounded text (a worker/process name chosen by the caller)."""
    return _require_text(owner, "owner")


def utc_text(moment: datetime) -> str:
    """Fixed-width UTC text (``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``): text order = time order."""
    return require_aware(moment).astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class InvocationIdentity:
    """The OC-1 §2 identity. Field order is the column order of the active-identity index."""

    venue: str
    market_kind: InstrumentKind
    native_instrument: str
    setup: str
    direction: Direction
    evidence_hash: str
    policy_version: str

    def __post_init__(self) -> None:
        _require_text(self.venue, "venue")
        _require_enum(InstrumentKind, self.market_kind, "market_kind")
        _require_text(self.native_instrument, "native_instrument")
        _require_text(self.setup, "setup")
        _require_enum(Direction, self.direction, "direction")
        _require_text(self.policy_version, "policy_version")
        digest = _require_text(self.evidence_hash, "evidence_hash").removeprefix(HASH_PREFIX)
        if not self.evidence_hash.startswith(HASH_PREFIX) or len(digest) != 64 or not set(digest) <= _HEX:
            raise InvocationError(
                InvocationFailure.INVALID_FIELD, "evidence_hash must be sha256:<64 lowercase hex> (a sealed evidence hash)"
            )

    def columns(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.venue,
            self.market_kind.value,
            self.native_instrument,
            self.setup,
            self.direction.value,
            self.evidence_hash,
            self.policy_version,
        )


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    """A request to run ``identity`` on ``model``; the model's budget is the one reserved."""

    identity: InvocationIdentity
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, InvocationIdentity):
            raise InvocationError(InvocationFailure.INVALID_FIELD, "identity must be InvocationIdentity")
        _require_text(self.model, "model")


@dataclass(frozen=True, slots=True)
class ModelBudget:
    """Reservation limits for one model. A limit of 0 refuses every claim."""

    model: str
    hourly_limit: int
    daily_limit: int

    def __post_init__(self) -> None:
        _require_text(self.model, "model")
        _require_count(self.hourly_limit, "hourly_limit")
        _require_count(self.daily_limit, "daily_limit")


@dataclass(frozen=True, slots=True)
class BudgetWindows:
    hour_start: str
    day_start: str


def budget_windows(now: datetime) -> BudgetWindows:
    """UTC hour and day windows containing ``now``."""
    moment = require_aware(now).astimezone(UTC)
    hour = moment.replace(minute=0, second=0, microsecond=0)
    return BudgetWindows(hour_start=utc_text(hour), day_start=moment.date().isoformat())


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """Reserved units in the windows of one claim decision (before or after it)."""

    model: str
    windows: BudgetWindows
    hourly_reserved: int
    hourly_limit: int
    daily_reserved: int
    daily_limit: int


@dataclass(frozen=True, slots=True)
class DemandCounts:
    """Requests observed per model and window (duplicates and refusals included)."""

    model: str
    windows: BudgetWindows
    hourly_observed: int
    hourly_refused: int
    daily_observed: int
    daily_refused: int


def refusal_reason(usage: BudgetUsage) -> RefusalReason | None:
    """``None`` when one more reservation fits both windows; the hour is checked first."""
    if usage.hourly_reserved >= usage.hourly_limit:
        return RefusalReason.HOURLY_BUDGET_EXHAUSTED
    if usage.daily_reserved >= usage.daily_limit:
        return RefusalReason.DAILY_BUDGET_EXHAUSTED
    return None


def lease_expiry(now: datetime, lease_seconds: int) -> datetime:
    if (
        not isinstance(lease_seconds, int)
        or isinstance(lease_seconds, bool)
        or not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS
    ):
        raise InvocationError(
            InvocationFailure.INVALID_FIELD, f"lease_seconds must be an int in {MIN_LEASE_SECONDS}..{MAX_LEASE_SECONDS}"
        )
    return require_aware(now).astimezone(UTC) + timedelta(seconds=lease_seconds)


@dataclass(frozen=True, slots=True)
class Lease:
    """What a claim or a recovery hands to its holder. Every holder action presents it."""

    invocation_id: str
    generation: int
    owner: str
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.invocation_id, "invocation_id")
        if _require_count(self.generation, "generation") < 1:
            raise InvocationError(InvocationFailure.INVALID_FIELD, "generation starts at 1")
        _require_text(self.owner, "owner")
        require_aware(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class Claimed:
    """This request won: a new invocation row, one reserved unit in each window."""

    lease: Lease
    usage: BudgetUsage


@dataclass(frozen=True, slots=True)
class Duplicate:
    """An identical invocation is already active: its ID, no new reservation, no lease."""

    invocation_id: str
    demand_count: int


@dataclass(frozen=True, slots=True)
class Refused:
    """Budget exhausted: no reservation, no invocation row. Only demand was counted."""

    reason: RefusalReason
    usage: BudgetUsage


type ClaimResult = Claimed | Duplicate | Refused


@dataclass(frozen=True, slots=True)
class Transition:
    """Result of a holder action; ``attempt_count`` is the stored value after the action."""

    status: TransitionStatus
    invocation_id: str
    attempt_count: int | None

    @property
    def applied(self) -> bool:
        return self.status is TransitionStatus.APPLIED


@dataclass(frozen=True, slots=True)
class InvocationRecord:
    """One stored invocation, as read back and re-validated."""

    invocation_id: str
    identity: InvocationIdentity
    model: str
    state: InvocationState
    generation: int
    lease_owner: str
    lease_expires_at: datetime
    demand_count: int
    attempt_count: int
    windows: BudgetWindows
    claimed_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    end_reason: str | None


def fence_status(record: InvocationRecord | None, lease: Lease, now: datetime) -> TransitionStatus:
    """Whether ``lease`` may still act on ``record`` at ``now``.

    Order: missing row, terminal state, generation/owner fence, then expiry. An expired
    lease that nobody has recovered yet still cannot act: a result after lease loss
    never commits (OC-1 §2).
    """
    moment = require_aware(now)
    if record is None or record.invocation_id != lease.invocation_id:
        return TransitionStatus.NOT_FOUND
    if record.state not in ACTIVE_STATES:
        return TransitionStatus.NOT_ACTIVE
    if record.generation != lease.generation or record.lease_owner != lease.owner:
        return TransitionStatus.FENCED
    if moment >= record.lease_expires_at:
        return TransitionStatus.LEASE_EXPIRED
    return TransitionStatus.APPLIED
