"""SQLite persistence for invocation claims, budget reservations and lease fencing (T031a).

Tables come from ledger migration version 3 (``evidence_store.INVOCATION_MIGRATION``):
``invocations``, ``invocation_budget`` and ``invocation_demand``. Nothing here touches a
legacy table (``events``, ``model_budget_usage``, ``model_cooldowns``).

Transactions:

* Every write is one short ``BEGIN IMMEDIATE`` transaction inside a single function
  call. No callback runs inside it, so it is never open during network or model work.
  A caller that already holds a transaction is refused (``OPEN_TRANSACTION``).
* A claim observes demand, checks for an active identical invocation, checks both
  budget windows, reserves one unit in each and inserts the invocation, all in that
  one transaction. A refused claim writes only the demand counters: no reservation and
  no invocation row.
* ``SQLITE_BUSY`` / ``SQLITE_LOCKED`` roll the attempt back and retry after 50, 100 and
  200 ms; the last failure raises ``InvocationError(BUSY)``. Nothing is reported as
  done unless its transaction committed.
* The partial unique index on the active identity is a backstop: under the write lock
  the duplicate check already finds the active row, so a second active row cannot
  be committed by any path.

Holder actions (attempt, complete, release) present their ``Lease``. The row is checked
with ``domain.invocation.fence_status`` and the ``UPDATE`` repeats the generation and
owner in its ``WHERE`` clause, so a stale holder changes nothing and gets a typed
``Transition`` status instead.

Rollback: drop the guard triggers, then the three invocation tables. No legacy table
is touched by the migration.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

from ..domain.integrity import InstrumentKind
from ..domain.invocation import (
    BUSY_RETRY_DELAYS,
    BudgetUsage,
    BudgetWindows,
    Claimed,
    ClaimResult,
    DemandCounts,
    Direction,
    Duplicate,
    InvocationError,
    InvocationFailure,
    InvocationIdentity,
    InvocationRecord,
    InvocationRequest,
    InvocationState,
    Lease,
    ModelBudget,
    Refused,
    ReleaseReason,
    Transition,
    TransitionStatus,
    WindowKind,
    budget_windows,
    fence_status,
    lease_expiry,
    refusal_reason,
    require_aware,
    require_owner,
    utc_text,
)
from . import evidence_store

INVOCATION_TABLE = "invocations"
BUDGET_TABLE = "invocation_budget"
DEMAND_TABLE = "invocation_demand"

_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6

type Sleep = Callable[[float], None]
type IdFactory = Callable[[], str]


def new_invocation_id() -> str:
    return "inv-" + uuid.uuid4().hex


def _is_busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF in (_SQLITE_BUSY, _SQLITE_LOCKED):
        return True
    text = str(error).lower()
    return "database is locked" in text or "database is busy" in text or "database table is locked" in text


def _require_migrated(conn: sqlite3.Connection) -> None:
    try:
        pending = evidence_store.pending_migrations(conn)
    except evidence_store.SchemaMigrationError as error:
        raise InvocationError(InvocationFailure.SCHEMA_NOT_MIGRATED, str(error)) from error
    if pending:
        raise InvocationError(
            InvocationFailure.SCHEMA_NOT_MIGRATED,
            f"ledger versions {[migration.version for migration in pending]} are not applied",
        )


def _write[T](conn: sqlite3.Connection, work: Callable[[], T], sleep: Sleep) -> T:
    """Run ``work`` in one ``BEGIN IMMEDIATE`` transaction, with bounded busy retries."""
    if conn.in_transaction:
        raise InvocationError(InvocationFailure.OPEN_TRANSACTION, "commit or roll back before an invocation write")
    last: sqlite3.Error | None = None
    for attempt in range(len(BUSY_RETRY_DELAYS) + 1):
        try:
            if attempt == 0:
                _require_migrated(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = work()
                conn.commit()
                return result
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
        except sqlite3.Error as error:
            if not _is_busy(error):
                raise InvocationError(InvocationFailure.STORAGE_ERROR, str(error)) from error
            last = error
            if attempt < len(BUSY_RETRY_DELAYS):
                sleep(BUSY_RETRY_DELAYS[attempt])
    raise InvocationError(
        InvocationFailure.BUSY, f"database stayed busy after {len(BUSY_RETRY_DELAYS) + 1} tries: {last}"
    )


def _read[T](conn: sqlite3.Connection, work: Callable[[], T]) -> T:
    try:
        _require_migrated(conn)
        return work()
    except sqlite3.Error as error:
        code = InvocationFailure.BUSY if _is_busy(error) else InvocationFailure.STORAGE_ERROR
        raise InvocationError(code, str(error)) from error


def _one(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> tuple[object, ...] | None:
    row = conn.execute(sql, tuple(params)).fetchone()
    return None if row is None else tuple(row)


def _changed_one(cursor: sqlite3.Cursor, what: str) -> None:
    if cursor.rowcount != 1:
        raise InvocationError(InvocationFailure.STORAGE_ERROR, f"{what} changed {cursor.rowcount} rows, expected 1")


# --- counters ---------------------------------------------------------------------------------


def _window_rows(windows: BudgetWindows) -> tuple[tuple[str, str], tuple[str, str]]:
    return ((WindowKind.HOUR.value, windows.hour_start), (WindowKind.DAY.value, windows.day_start))


def _count(conn: sqlite3.Connection, sql: str, params: Sequence[object]) -> int:
    row = _one(conn, sql, params)
    if row is None:
        return 0
    value = row[0]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvocationError(InvocationFailure.CORRUPT_ROW, f"counter {value!r} is not a non-negative int")
    return value


def _usage(conn: sqlite3.Connection, budget: ModelBudget, windows: BudgetWindows) -> BudgetUsage:
    sql = f"SELECT reserved FROM {BUDGET_TABLE} WHERE model = ? AND window_kind = ? AND window_start = ?"
    (hour_kind, hour_start), (day_kind, day_start) = _window_rows(windows)
    return BudgetUsage(
        model=budget.model,
        windows=windows,
        hourly_reserved=_count(conn, sql, (budget.model, hour_kind, hour_start)),
        hourly_limit=budget.hourly_limit,
        daily_reserved=_count(conn, sql, (budget.model, day_kind, day_start)),
        daily_limit=budget.daily_limit,
    )


def _reserve(conn: sqlite3.Connection, model: str, windows: BudgetWindows) -> None:
    for kind, start in _window_rows(windows):
        conn.execute(
            f"INSERT INTO {BUDGET_TABLE} (model, window_kind, window_start, reserved) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(model, window_kind, window_start) DO UPDATE SET reserved = reserved + 1",
            (model, kind, start),
        )


def _observe_demand(conn: sqlite3.Connection, model: str, windows: BudgetWindows, *, refused: bool) -> None:
    for kind, start in _window_rows(windows):
        if refused:
            cursor = conn.execute(
                f"UPDATE {DEMAND_TABLE} SET refused = refused + 1 "
                "WHERE model = ? AND window_kind = ? AND window_start = ?",
                (model, kind, start),
            )
            _changed_one(cursor, "refused demand")
        else:
            conn.execute(
                f"INSERT INTO {DEMAND_TABLE} (model, window_kind, window_start, observed, refused) "
                "VALUES (?, ?, ?, 1, 0) "
                "ON CONFLICT(model, window_kind, window_start) DO UPDATE SET observed = observed + 1",
                (model, kind, start),
            )


# --- rows ---------------------------------------------------------------------------------------

_COLUMNS = (
    "invocation_id, venue, market_kind, native_instrument, setup, direction, evidence_hash, policy_version, "
    "model, state, generation, lease_owner, lease_expires_at, demand_count, attempt_count, hour_window, "
    "day_window, claimed_at, updated_at, ended_at, end_reason"
)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise InvocationError(InvocationFailure.CORRUPT_ROW, f"{INVOCATION_TABLE}.{field} is not text")
    return value


def _int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvocationError(InvocationFailure.CORRUPT_ROW, f"{INVOCATION_TABLE}.{field} is not an integer")
    return value


def _moment(value: object, field: str) -> datetime:
    try:
        moment = datetime.fromisoformat(_text(value, field))
    except ValueError as error:
        raise InvocationError(InvocationFailure.CORRUPT_ROW, f"{INVOCATION_TABLE}.{field}: {error}") from error
    if moment.tzinfo is None:
        raise InvocationError(InvocationFailure.CORRUPT_ROW, f"{INVOCATION_TABLE}.{field} is naive")
    return moment


def _record(row: tuple[object, ...]) -> InvocationRecord:
    (
        invocation_id,
        venue,
        market_kind,
        native_instrument,
        setup,
        direction,
        evidence_hash,
        policy_version,
        model,
        state,
        generation,
        lease_owner,
        lease_expires_at,
        demand_count,
        attempt_count,
        hour_window,
        day_window,
        claimed_at,
        updated_at,
        ended_at,
        end_reason,
    ) = row
    try:
        identity = InvocationIdentity(
            venue=_text(venue, "venue"),
            market_kind=InstrumentKind(_text(market_kind, "market_kind")),
            native_instrument=_text(native_instrument, "native_instrument"),
            setup=_text(setup, "setup"),
            direction=Direction(_text(direction, "direction")),
            evidence_hash=_text(evidence_hash, "evidence_hash"),
            policy_version=_text(policy_version, "policy_version"),
        )
        parsed_state = InvocationState(_text(state, "state"))
    except ValueError as error:
        raise InvocationError(InvocationFailure.CORRUPT_ROW, f"{INVOCATION_TABLE}: {error}") from error
    except InvocationError as error:
        raise InvocationError(InvocationFailure.CORRUPT_ROW, f"{INVOCATION_TABLE}: {error.detail}") from error
    return InvocationRecord(
        invocation_id=_text(invocation_id, "invocation_id"),
        identity=identity,
        model=_text(model, "model"),
        state=parsed_state,
        generation=_int(generation, "generation"),
        lease_owner=_text(lease_owner, "lease_owner"),
        lease_expires_at=_moment(lease_expires_at, "lease_expires_at"),
        demand_count=_int(demand_count, "demand_count"),
        attempt_count=_int(attempt_count, "attempt_count"),
        windows=BudgetWindows(_text(hour_window, "hour_window"), _text(day_window, "day_window")),
        claimed_at=_moment(claimed_at, "claimed_at"),
        updated_at=_moment(updated_at, "updated_at"),
        ended_at=None if ended_at is None else _moment(ended_at, "ended_at"),
        end_reason=None if end_reason is None else _text(end_reason, "end_reason"),
    )


def _load(conn: sqlite3.Connection, invocation_id: str) -> InvocationRecord | None:
    row = _one(conn, f"SELECT {_COLUMNS} FROM {INVOCATION_TABLE} WHERE invocation_id = ?", (invocation_id,))
    return None if row is None else _record(row)


# --- claim ----------------------------------------------------------------------------------------


def claim_invocation(
    conn: sqlite3.Connection,
    request: InvocationRequest,
    budget: ModelBudget,
    *,
    owner: str,
    now: datetime,
    lease_seconds: int,
    new_id: IdFactory = new_invocation_id,
    sleep: Sleep = time.sleep,
) -> ClaimResult:
    """Claim ``request`` and reserve its budget in one transaction.

    ``Duplicate`` when an identical invocation is active (no reservation), ``Refused``
    when either budget window is full (no reservation, no row), else ``Claimed``.
    """
    if not isinstance(request, InvocationRequest) or not isinstance(budget, ModelBudget):
        raise InvocationError(InvocationFailure.INVALID_FIELD, "request and budget must be domain types")
    if budget.model != request.model:
        raise InvocationError(InvocationFailure.INVALID_FIELD, f"budget is for {budget.model!r}, not {request.model!r}")
    now_text = utc_text(now)
    expires_at = lease_expiry(now, lease_seconds)
    lease_owner = require_owner(owner)
    windows = budget_windows(now)
    identity = request.identity.columns()

    def work() -> ClaimResult:
        _observe_demand(conn, request.model, windows, refused=False)
        active = _one(
            conn,
            f"SELECT invocation_id, demand_count FROM {INVOCATION_TABLE} WHERE state = 'CLAIMED' AND venue = ? "
            "AND market_kind = ? AND native_instrument = ? AND setup = ? AND direction = ? AND evidence_hash = ? "
            "AND policy_version = ?",
            identity,
        )
        if active is not None:
            invocation_id = _text(active[0], "invocation_id")
            demand = _int(active[1], "demand_count") + 1
            cursor = conn.execute(
                f"UPDATE {INVOCATION_TABLE} SET demand_count = ?, updated_at = ? WHERE invocation_id = ?",
                (demand, now_text, invocation_id),
            )
            _changed_one(cursor, "duplicate demand")
            return Duplicate(invocation_id=invocation_id, demand_count=demand)
        usage = _usage(conn, budget, windows)
        reason = refusal_reason(usage)
        if reason is not None:
            _observe_demand(conn, request.model, windows, refused=True)
            return Refused(reason=reason, usage=usage)
        invocation_id = new_id()
        lease = Lease(invocation_id, 1, lease_owner, expires_at)
        _reserve(conn, request.model, windows)
        conn.execute(
            f"INSERT INTO {INVOCATION_TABLE} ({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLAIMED', 1, ?, ?, 1, 0, ?, ?, ?, ?, NULL, NULL)",
            (
                invocation_id,
                *identity,
                request.model,
                lease_owner,
                utc_text(expires_at),
                windows.hour_start,
                windows.day_start,
                now_text,
                now_text,
            ),
        )
        return Claimed(lease=lease, usage=_usage(conn, budget, windows))

    return _write(conn, work, sleep)


# --- holder actions --------------------------------------------------------------------------------


def _holder_action(
    conn: sqlite3.Connection,
    lease: Lease,
    now: datetime,
    sleep: Sleep,
    apply: Callable[[InvocationRecord, str], Transition],
) -> Transition:
    if not isinstance(lease, Lease):
        raise InvocationError(InvocationFailure.INVALID_FIELD, "lease must be a Lease")
    now_text = utc_text(now)

    def work() -> Transition:
        record = _load(conn, lease.invocation_id)
        status = fence_status(record, lease, now)
        if status is not TransitionStatus.APPLIED or record is None:
            return Transition(status, lease.invocation_id, None if record is None else record.attempt_count)
        return apply(record, now_text)

    return _write(conn, work, sleep)


def _fenced_update(conn: sqlite3.Connection, lease: Lease, assignments: str, params: Sequence[object]) -> None:
    cursor = conn.execute(
        f"UPDATE {INVOCATION_TABLE} SET {assignments} "
        "WHERE invocation_id = ? AND generation = ? AND lease_owner = ? AND state = 'CLAIMED'",
        (*params, lease.invocation_id, lease.generation, lease.owner),
    )
    _changed_one(cursor, "fenced update")


def record_attempt(
    conn: sqlite3.Connection,
    lease: Lease,
    budget: ModelBudget,
    *,
    now: datetime,
    sleep: Sleep = time.sleep,
) -> Transition:
    """Record one genuine model call before it is made; the count never goes down.

    The claim's reservation covers the first attempt. A further attempt (after crash
    recovery, or a repair) reserves one more unit in the windows of ``now`` or returns
    ``BUDGET_EXHAUSTED`` without recording anything.
    """
    if not isinstance(budget, ModelBudget):
        raise InvocationError(InvocationFailure.INVALID_FIELD, "budget must be a ModelBudget")

    def apply(record: InvocationRecord, now_text: str) -> Transition:
        if record.model != budget.model:
            raise InvocationError(
                InvocationFailure.INVALID_FIELD, f"budget is for {budget.model!r}, invocation uses {record.model!r}"
            )
        if record.attempt_count >= 1:
            windows = budget_windows(now)
            if refusal_reason(_usage(conn, budget, windows)) is not None:
                return Transition(TransitionStatus.BUDGET_EXHAUSTED, record.invocation_id, record.attempt_count)
            _reserve(conn, record.model, windows)
        attempts = record.attempt_count + 1
        _fenced_update(conn, lease, "attempt_count = ?, updated_at = ?", (attempts, now_text))
        return Transition(TransitionStatus.APPLIED, record.invocation_id, attempts)

    return _holder_action(conn, lease, now, sleep, apply)


def complete_invocation(
    conn: sqlite3.Connection, lease: Lease, *, now: datetime, sleep: Sleep = time.sleep
) -> Transition:
    """Mark the invocation COMPLETED if ``lease`` still holds it."""

    def apply(record: InvocationRecord, now_text: str) -> Transition:
        _fenced_update(
            conn, lease, "state = 'COMPLETED', updated_at = ?, ended_at = ?, end_reason = NULL", (now_text, now_text)
        )
        return Transition(TransitionStatus.APPLIED, record.invocation_id, record.attempt_count)

    return _holder_action(conn, lease, now, sleep, apply)


def release_invocation(
    conn: sqlite3.Connection,
    lease: Lease,
    reason: ReleaseReason,
    *,
    now: datetime,
    sleep: Sleep = time.sleep,
) -> Transition:
    """Mark the invocation RELEASED with ``reason``. Reservations and attempts stay counted."""
    if not isinstance(reason, ReleaseReason):
        raise InvocationError(InvocationFailure.INVALID_FIELD, "reason must be a ReleaseReason")

    def apply(record: InvocationRecord, now_text: str) -> Transition:
        _fenced_update(
            conn,
            lease,
            "state = 'RELEASED', updated_at = ?, ended_at = ?, end_reason = ?",
            (now_text, now_text, reason.value),
        )
        return Transition(TransitionStatus.APPLIED, record.invocation_id, record.attempt_count)

    return _holder_action(conn, lease, now, sleep, apply)


def recover_expired(
    conn: sqlite3.Connection,
    *,
    owner: str,
    now: datetime,
    lease_seconds: int,
    limit: int = 16,
    sleep: Sleep = time.sleep,
) -> tuple[Lease, ...]:
    """Take over active invocations whose lease expired (the holder crashed or stalled).

    Each one gets ``generation + 1`` and a new lease for ``owner``; the old holder is
    fenced from then on. No budget is reserved and no counter changes.
    """
    now_text = utc_text(now)
    expires_at = lease_expiry(now, lease_seconds)
    new_owner = require_owner(owner)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise InvocationError(InvocationFailure.INVALID_FIELD, "limit must be an int in 1..1000")

    def work() -> tuple[Lease, ...]:
        rows = conn.execute(
            f"SELECT invocation_id, generation FROM {INVOCATION_TABLE} "
            "WHERE state = 'CLAIMED' AND lease_expires_at <= ? ORDER BY lease_expires_at, invocation_id LIMIT ?",
            (now_text, limit),
        ).fetchall()
        leases: list[Lease] = []
        for row in rows:
            invocation_id = _text(row[0], "invocation_id")
            generation = _int(row[1], "generation") + 1
            cursor = conn.execute(
                f"UPDATE {INVOCATION_TABLE} SET generation = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ? "
                "WHERE invocation_id = ? AND generation = ? AND state = 'CLAIMED'",
                (generation, new_owner, utc_text(expires_at), now_text, invocation_id, generation - 1),
            )
            _changed_one(cursor, "lease recovery")
            leases.append(Lease(invocation_id, generation, new_owner, expires_at))
        return tuple(leases)

    return _write(conn, work, sleep)


# --- reads ------------------------------------------------------------------------------------------


def load_invocation(conn: sqlite3.Connection, invocation_id: str) -> InvocationRecord | None:
    return _read(conn, lambda: _load(conn, invocation_id))


def budget_usage(conn: sqlite3.Connection, budget: ModelBudget, *, now: datetime) -> BudgetUsage:
    windows = budget_windows(require_aware(now))
    return _read(conn, lambda: _usage(conn, budget, windows))


def demand_counts(conn: sqlite3.Connection, model: str, *, now: datetime) -> DemandCounts:
    windows = budget_windows(require_aware(now))

    def work() -> DemandCounts:
        values: dict[str, tuple[int, int]] = {}
        for kind, start in _window_rows(windows):
            row = _one(
                conn,
                f"SELECT observed, refused FROM {DEMAND_TABLE} WHERE model = ? AND window_kind = ? AND window_start = ?",
                (model, kind, start),
            )
            values[kind] = (0, 0) if row is None else (_int(row[0], "observed"), _int(row[1], "refused"))
        hour, day = values[WindowKind.HOUR.value], values[WindowKind.DAY.value]
        return DemandCounts(model, windows, hour[0], hour[1], day[0], day[1])

    return _read(conn, work)
