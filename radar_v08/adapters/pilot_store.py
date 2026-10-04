"""SQLite store of the pilot shadow: a second pretend account sized by the Risk Engine.

Phase 2 of the path to real trading (docs/EXECUTION_ARCHITECTURE.md section 5). Pretend
money only: nothing here sends an order, reads an exchange account or calls a model. The
paper game's ``paper_*`` tables are never read or written here.

Nine append-only tables:

* ``pilot_account`` - one row (``account_id = 1``): the envelope (``domain.risk.Envelope``
  as its canonical text and SHA-256), recorded once. A later envelope that differs is not
  written over it: every new candidate is refused with ``envelope_changed`` instead.
* ``pilot_decisions`` - one row per evaluated event (``UNIQUE(event_id)``), opened or
  refused: the validated quote and ATR, the pair rules, the equity and cash it was sized
  on, the tick-rounded EX-1 levels, the four candidate quantities and the binding one,
  the lot-rounded and final quantity, the notional, the planned fees and stress loss, the
  envelope hash and policy ids, and the outcome ``OPENED`` or ``NO_TRADE`` with its typed
  reason and detail.
* ``pilot_positions`` - one row per opened decision (``UNIQUE(event_id)``): the exact
  quantity and prices as decimal text, the frozen fee, the EX-1 levels and the 24 h due.
* ``pilot_closes`` - one row per closed position (``UNIQUE(position_id)``): the exit
  reason (stop, target, time), the source, the record lag, gross/fees/net in cents and the
  realized slippage of the exit bid versus the stop (to calibrate the stress constant).
* ``pilot_exit_quotes`` - a quote from outside ``spot_snapshots`` that closed a position.
* ``pilot_equity_marks`` - the lock references: ``day_start`` (the equity at the first
  evaluation of each UTC day) and ``high_water`` (starts at the assigned equity, rises
  only with observed equity), each with its source; a review rebase is a mark too.
* ``pilot_locks`` - one row per lock trip (daily loss or drawdown).
* ``pilot_lock_reviews`` - the only thing that clears a lock (``UNIQUE(lock_id)``): the
  reviewer, the cause and the equity the reference was rebased to.
* ``pilot_kill_switch`` - ``ENGAGED``/``RELEASED`` rows; the latest wins, none is released.

Like ``paper_*`` the tables sit outside the schema-version ledger on purpose, so
``ensure_schema`` uses only ``CREATE ... IF NOT EXISTS`` in one ``BEGIN IMMEDIATE``
transaction and writes nothing when every object exists.

Equity is the assigned equity plus every realized net plus a conservative mark of each
open position (``domain.risk.unrealized_mark``) at the latest valid quote of its pair
observed from entry up to now (a recorded spot snapshot or an offered extra quote; the
entry quote when none is newer). Locks are evaluated under the write lock before every
entry, after every close and on each mark call. A lock stays active until a review row
names it: a new UTC day, a new connection or process and any number of evaluations leave
it as it is.

Every write is one ``BEGIN IMMEDIATE`` transaction that rolls back entirely on any error;
admission is decided under that write lock. Quantities and prices are stored as exact
decimal text, money that was rounded to the cent as integer cents, times as fixed-width
UTC text. The CHECK constraints and triggers are the backstop behind the validation here
and in ``domain.risk``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum

from ..domain import paper, risk
from ..domain.paper import Direction, ExitReason, Outcome
from ..domain.risk import Envelope, LockKind, NoTrade, NoTradeReason
from .paper_store import (
    SNAPSHOT_TEXT_MARGIN,
    SPOT_SNAPSHOT_SOURCE,
    ObservedQuote,
    utc_text,
)

ACCOUNT_TABLE = "pilot_account"
DECISION_TABLE = "pilot_decisions"
POSITION_TABLE = "pilot_positions"
CLOSE_TABLE = "pilot_closes"
EXIT_QUOTE_TABLE = "pilot_exit_quotes"
MARK_TABLE = "pilot_equity_marks"
LOCK_TABLE = "pilot_locks"
REVIEW_TABLE = "pilot_lock_reviews"
KILL_SWITCH_TABLE = "pilot_kill_switch"
TABLES = (
    ACCOUNT_TABLE,
    DECISION_TABLE,
    POSITION_TABLE,
    CLOSE_TABLE,
    EXIT_QUOTE_TABLE,
    MARK_TABLE,
    LOCK_TABLE,
    REVIEW_TABLE,
    KILL_SWITCH_TABLE,
)

OPENED = "OPENED"
NO_TRADE = "NO_TRADE"
ENGAGED = "ENGAGED"
RELEASED = "RELEASED"


class MarkKind(Enum):
    DAY_START = "day_start"
    HIGH_WATER = "high_water"


class MarkSource(Enum):
    #: The high-water mark recorded with the account: the assigned equity.
    ASSIGNED = "assigned"
    #: Observed by a lock evaluation (the first of a UTC day, or a new high).
    EVALUATION = "evaluation"
    #: The explicit rebase of a lock review.
    REVIEW = "review"


class LockTrigger(Enum):
    """Where a lock evaluation ran: before entries, after closes or on a mark call."""

    ENTRY = "entry"
    SETTLE = "settle"
    MARK = "mark"


def _append_only(table: str) -> tuple[str, str]:
    return (
        f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} rows are append-only');
END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} rows are never deleted');
END""",
    )


def _require_parent(table: str, name: str, condition: str, message: str) -> str:
    return f"""CREATE TRIGGER IF NOT EXISTS {table}_{name} BEFORE INSERT ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}')
    WHERE NOT EXISTS ({condition});
END"""


def _sql_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


_OUTCOMES = _sql_list([outcome.value for outcome in Outcome])
_EXIT_REASONS = _sql_list([reason.value for reason in ExitReason])
_NO_TRADE_REASONS = _sql_list([reason.value for reason in NoTradeReason])
_BINDINGS = _sql_list([binding.value for binding in risk.BindingConstraint])
_LOCK_KINDS = _sql_list([kind.value for kind in LockKind])
_MARK_KINDS = _sql_list([kind.value for kind in MarkKind])
_MARK_SOURCES = _sql_list([source.value for source in MarkSource])
_TRIGGERS = _sql_list([trigger.value for trigger in LockTrigger])


def _text_column(name: str, *, null: bool = False) -> str:
    if null:
        return f"{name} TEXT CHECK ({name} IS NULL OR length({name}) > 0)"
    return f"{name} TEXT NOT NULL CHECK (length({name}) > 0)"


def _cents_column(name: str, *, null: bool = False, nonnegative: bool = False) -> str:
    sign = f" AND {name} >= 0" if nonnegative else ""
    if null:
        return f"{name} INTEGER CHECK ({name} IS NULL OR (typeof({name}) = 'integer'{sign}))"
    return f"{name} INTEGER NOT NULL CHECK (typeof({name}) = 'integer'{sign})"


_DECISION_NULLABLE_TEXT = (
    "bid",
    "ask",
    "atr",
    "atr_pair",
    "ordermin",
    "costmin",
    "tick",
    "tick_source",
    "equity",
    "cash",
    "stop_price",
    "target_price",
    "stress_exit_price",
    "loss_per_unit",
    "per_entry_loss_cap",
    "aggregate_loss_remaining",
    "notional_remaining",
    "cash_room",
    "qty_per_entry_loss",
    "qty_aggregate_loss",
    "qty_notional",
    "qty_cash",
    "lot_quantity",
    "quantity",
    "notional",
    "planned_loss",
)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS {ACCOUNT_TABLE} (
    account_id INTEGER PRIMARY KEY CHECK (account_id = 1),
    envelope_json TEXT NOT NULL CHECK (json_valid(envelope_json) AND json_type(envelope_json) = 'object'),
    envelope_sha256 TEXT NOT NULL CHECK (length(envelope_sha256) = 64),
    {_text_column("envelope_policy_id")},
    equity_cents INTEGER NOT NULL CHECK (typeof(equity_cents) = 'integer' AND equity_cents > 0),
    {_text_column("currency")},
    {_text_column("recorded_at")}
)""",
    *_append_only(ACCOUNT_TABLE),
    f"""CREATE TABLE IF NOT EXISTS {DECISION_TABLE} (
    decision_id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE CHECK (length(event_id) > 0),
    {_text_column("run_id")},
    {_text_column("asset")},
    {_text_column("pair")},
    {_text_column("quote")},
    {_text_column("direction")},
    {_text_column("snapshot_ts")},
    {",\n    ".join(_text_column(name, null=True) for name in _DECISION_NULLABLE_TEXT)},
    lot_decimals INTEGER CHECK (lot_decimals IS NULL OR (typeof(lot_decimals) = 'integer' AND lot_decimals >= 0)),
    binding TEXT CHECK (binding IS NULL OR binding IN ({_BINDINGS})),
    passes INTEGER NOT NULL CHECK (typeof(passes) = 'integer' AND passes >= 0),
    {_cents_column("entry_fee_cents", null=True, nonnegative=True)},
    {_cents_column("exit_fee_cents", null=True, nonnegative=True)},
    {_text_column("fee_bps")},
    {_text_column("stress_bps")},
    envelope_sha256 TEXT NOT NULL CHECK (length(envelope_sha256) = 64),
    {_text_column("envelope_policy_id")},
    {_text_column("sizing_policy_id")},
    {_text_column("stress_policy_id")},
    {_text_column("exit_policy_id")},
    outcome TEXT NOT NULL CHECK (outcome IN ('{OPENED}', '{NO_TRADE}')),
    reason TEXT CHECK (reason IS NULL OR reason IN ({_NO_TRADE_REASONS})),
    detail TEXT NOT NULL,
    {_text_column("decided_at")},
    CHECK ((outcome = '{OPENED}' AND reason IS NULL AND detail = '' AND quantity IS NOT NULL
            AND notional IS NOT NULL AND planned_loss IS NOT NULL AND stop_price IS NOT NULL
            AND target_price IS NOT NULL AND entry_fee_cents IS NOT NULL AND exit_fee_cents IS NOT NULL)
        OR (outcome = '{NO_TRADE}' AND reason IS NOT NULL))
)""",
    *_append_only(DECISION_TABLE),
    f"""CREATE TABLE IF NOT EXISTS {POSITION_TABLE} (
    position_id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL UNIQUE REFERENCES {DECISION_TABLE} (decision_id),
    event_id TEXT NOT NULL UNIQUE CHECK (length(event_id) > 0),
    {_text_column("run_id")},
    {_text_column("asset")},
    {_text_column("pair")},
    {_text_column("quote")},
    direction TEXT NOT NULL CHECK (direction = '{Direction.LONG.value}'),
    {_text_column("quantity")},
    {_text_column("entry_bid")},
    {_text_column("entry_ask")},
    {_text_column("entry_ts")},
    due_at TEXT NOT NULL CHECK (due_at > entry_ts),
    {_text_column("fee_bps")},
    {_text_column("exit_policy")},
    {_text_column("atr")},
    {_text_column("stop_price")},
    {_text_column("target_price")},
    {_text_column("stress_exit_price")},
    {_text_column("planned_loss")},
    {_text_column("notional")},
    {_text_column("cost_basis")},
    {_text_column("opened_at")}
)""",
    *_append_only(POSITION_TABLE),
    _require_parent(
        POSITION_TABLE,
        "require_opened_decision",
        f"SELECT 1 FROM {DECISION_TABLE} d WHERE d.decision_id = NEW.decision_id "
        f"AND d.event_id = NEW.event_id AND d.outcome = '{OPENED}'",
        f"{POSITION_TABLE} row has no opened decision",
    ),
    f"""CREATE TABLE IF NOT EXISTS {EXIT_QUOTE_TABLE} (
    quote_id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL REFERENCES {POSITION_TABLE} (position_id),
    {_text_column("pair")},
    {_text_column("bid")},
    {_text_column("ask")},
    {_text_column("observed_at")},
    source TEXT NOT NULL CHECK (length(source) > 0 AND source <> '{SPOT_SNAPSHOT_SOURCE}'),
    recorded_at TEXT NOT NULL CHECK (recorded_at >= observed_at)
)""",
    *_append_only(EXIT_QUOTE_TABLE),
    _require_parent(
        EXIT_QUOTE_TABLE,
        "require_position",
        f"SELECT 1 FROM {POSITION_TABLE} WHERE position_id = NEW.position_id",
        f"{EXIT_QUOTE_TABLE} row has no position",
    ),
    f"""CREATE TABLE IF NOT EXISTS {CLOSE_TABLE} (
    close_id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL UNIQUE REFERENCES {POSITION_TABLE} (position_id),
    exit_reason TEXT NOT NULL CHECK (exit_reason IN ({_EXIT_REASONS})),
    {_text_column("exit_source")},
    exit_observation_id INTEGER NOT NULL CHECK (typeof(exit_observation_id) = 'integer'),
    {_text_column("exit_bid")},
    {_text_column("exit_ask")},
    {_text_column("exit_ts")},
    record_lag_seconds REAL NOT NULL CHECK (record_lag_seconds >= 0),
    {_text_column("quantity")},
    {_cents_column("gross_cents")},
    {_cents_column("entry_fee_cents", nonnegative=True)},
    {_cents_column("exit_fee_cents", nonnegative=True)},
    {_cents_column("fees_cents", nonnegative=True)},
    {_cents_column("net_cents")},
    outcome TEXT NOT NULL CHECK (outcome IN ({_OUTCOMES})),
    {_text_column("stop_price")},
    {_text_column("stop_slippage")},
    {_text_column("stop_slippage_bps")},
    {_text_column("closed_at")},
    CHECK (fees_cents = entry_fee_cents + exit_fee_cents),
    CHECK (net_cents = gross_cents - fees_cents),
    CHECK ((outcome = 'WIN' AND net_cents > 0) OR (outcome = 'LOSS' AND net_cents < 0)
        OR (outcome = 'FLAT' AND net_cents = 0))
)""",
    *_append_only(CLOSE_TABLE),
    _require_parent(
        CLOSE_TABLE,
        "require_position",
        f"SELECT 1 FROM {POSITION_TABLE} WHERE position_id = NEW.position_id",
        f"{CLOSE_TABLE} row has no position",
    ),
    f"""CREATE TABLE IF NOT EXISTS {LOCK_TABLE} (
    lock_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ({_LOCK_KINDS})),
    {_text_column("equity")},
    {_text_column("reference")},
    {_text_column("limit_pct")},
    {_text_column("utc_day")},
    evaluated_on TEXT NOT NULL CHECK (evaluated_on IN ({_TRIGGERS})),
    {_text_column("tripped_at")}
)""",
    *_append_only(LOCK_TABLE),
    f"""CREATE TABLE IF NOT EXISTS {REVIEW_TABLE} (
    review_id INTEGER PRIMARY KEY,
    lock_id INTEGER NOT NULL UNIQUE REFERENCES {LOCK_TABLE} (lock_id),
    {_text_column("reviewer")},
    {_text_column("cause")},
    {_text_column("rebase_equity")},
    {_text_column("reviewed_at")}
)""",
    *_append_only(REVIEW_TABLE),
    _require_parent(
        REVIEW_TABLE,
        "require_lock",
        f"SELECT 1 FROM {LOCK_TABLE} WHERE lock_id = NEW.lock_id",
        f"{REVIEW_TABLE} row has no lock",
    ),
    f"""CREATE TABLE IF NOT EXISTS {MARK_TABLE} (
    mark_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ({_MARK_KINDS})),
    utc_day TEXT CHECK (utc_day IS NULL OR length(utc_day) = 10),
    {_text_column("equity")},
    source TEXT NOT NULL CHECK (source IN ({_MARK_SOURCES})),
    lock_id INTEGER REFERENCES {LOCK_TABLE} (lock_id),
    {_text_column("recorded_at")},
    CHECK ((kind = '{MarkKind.DAY_START.value}') = (utc_day IS NOT NULL)),
    CHECK ((source = '{MarkSource.REVIEW.value}') = (lock_id IS NOT NULL)),
    CHECK (source <> '{MarkSource.ASSIGNED.value}' OR kind = '{MarkKind.HIGH_WATER.value}')
)""",
    *_append_only(MARK_TABLE),
    f"""CREATE UNIQUE INDEX IF NOT EXISTS {MARK_TABLE}_one_day_start ON {MARK_TABLE} (utc_day)
    WHERE kind = '{MarkKind.DAY_START.value}' AND source = '{MarkSource.EVALUATION.value}'""",
    f"""CREATE UNIQUE INDEX IF NOT EXISTS {MARK_TABLE}_one_assigned ON {MARK_TABLE} (source)
    WHERE source = '{MarkSource.ASSIGNED.value}'""",
    f"""CREATE TABLE IF NOT EXISTS {KILL_SWITCH_TABLE} (
    switch_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('{ENGAGED}', '{RELEASED}')),
    {_text_column("reason")},
    {_text_column("actor")},
    {_text_column("recorded_at")}
)""",
    *_append_only(KILL_SWITCH_TABLE),
)
SCHEMA_OBJECTS: tuple[tuple[str, str], ...] = (
    *(
        (kind, name)
        for table in TABLES
        for kind, name in (("table", table), ("trigger", f"{table}_no_update"), ("trigger", f"{table}_no_delete"))
    ),
    ("trigger", f"{POSITION_TABLE}_require_opened_decision"),
    ("trigger", f"{EXIT_QUOTE_TABLE}_require_position"),
    ("trigger", f"{CLOSE_TABLE}_require_position"),
    ("trigger", f"{REVIEW_TABLE}_require_lock"),
    ("index", f"{MARK_TABLE}_one_day_start"),
    ("index", f"{MARK_TABLE}_one_assigned"),
)

_DECISION_COLUMNS = (
    "event_id",
    "run_id",
    "asset",
    "pair",
    "quote",
    "direction",
    "snapshot_ts",
    *_DECISION_NULLABLE_TEXT,
    "lot_decimals",
    "binding",
    "passes",
    "entry_fee_cents",
    "exit_fee_cents",
    "fee_bps",
    "stress_bps",
    "envelope_sha256",
    "envelope_policy_id",
    "sizing_policy_id",
    "stress_policy_id",
    "exit_policy_id",
    "outcome",
    "reason",
    "detail",
    "decided_at",
)
_POSITION_COLUMNS = (
    "decision_id",
    "event_id",
    "run_id",
    "asset",
    "pair",
    "quote",
    "direction",
    "quantity",
    "entry_bid",
    "entry_ask",
    "entry_ts",
    "due_at",
    "fee_bps",
    "exit_policy",
    "atr",
    "stop_price",
    "target_price",
    "stress_exit_price",
    "planned_loss",
    "notional",
    "cost_basis",
    "opened_at",
)
_CLOSE_COLUMNS = (
    "position_id",
    "exit_reason",
    "exit_source",
    "exit_observation_id",
    "exit_bid",
    "exit_ask",
    "exit_ts",
    "record_lag_seconds",
    "quantity",
    "gross_cents",
    "entry_fee_cents",
    "exit_fee_cents",
    "fees_cents",
    "net_cents",
    "outcome",
    "stop_price",
    "stop_slippage",
    "stop_slippage_bps",
    "closed_at",
)

_HUNDRED = Decimal(100)


class PilotStoreFailure(Enum):
    INVALID_ROW = "invalid_row"
    INVALID_ARGUMENT = "invalid_argument"
    OPEN_TRANSACTION = "open_transaction"
    NO_ACCOUNT = "no_account"
    UNKNOWN_LOCK = "unknown_lock"
    LOCK_ALREADY_REVIEWED = "lock_already_reviewed"
    MALFORMED_ROW = "malformed_row"
    SQLITE_ERROR = "sqlite_error"


class PilotStoreError(RuntimeError):
    """Nothing was written (or it was rolled back); ``code`` says why."""

    def __init__(self, code: PilotStoreFailure, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# --- records ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PilotCandidate:
    """One new radar event the pilot evaluates: the game's candidate plus the pair rules.

    ``bid``/``ask``/``status`` are the touch recorded for ``pair`` at ``snapshot_ts``;
    ``atr`` is the 5-minute ATR14 of closed bars computed on ``atr_pair`` (it must be
    ``pair`` itself); ``pair_entry`` is the pair's raw public AssetPairs entry
    (``lot_decimals``, ``ordermin``, ``costmin``, ``tick_size``/``pair_decimals``). Any of
    these missing or invalid is a typed ``NO_TRADE``, never a zero.
    """

    event_id: str
    run_id: str
    asset: str
    pair: str
    quote: str
    direction: object
    bid: object
    ask: object
    snapshot_ts: str
    status: object
    atr: object = None
    atr_pair: object = None
    pair_entry: object = None


@dataclass(frozen=True, slots=True)
class Account:
    envelope: Envelope
    envelope_sha256: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class StoredDecision:
    decision_id: int
    event_id: str
    run_id: str
    asset: str
    pair: str
    quote: str
    direction: str
    snapshot_ts: str
    values: Mapping[str, Decimal | None]
    atr_pair: str | None
    tick_source: str | None
    lot_decimals: int | None
    binding: risk.BindingConstraint | None
    passes: int
    entry_fee: Decimal | None
    exit_fee: Decimal | None
    envelope_sha256: str
    envelope_policy_id: str
    sizing_policy_id: str
    stress_policy_id: str
    exit_policy_id: str
    outcome: str
    reason: NoTradeReason | None
    detail: str
    decided_at: str

    @property
    def opened(self) -> bool:
        return self.outcome == OPENED

    def value(self, name: str) -> Decimal | None:
        """A decimal column (``bid``, ``quantity``, ``planned_loss``...); ``None`` when not recorded."""
        return self.values[name]


@dataclass(frozen=True, slots=True)
class StoredPosition:
    position_id: int
    decision_id: int
    event_id: str
    run_id: str
    asset: str
    pair: str
    quote: str
    direction: Direction
    quantity: Decimal
    entry_bid: Decimal
    entry_ask: Decimal
    entry_ts: datetime
    due_at: datetime
    fee_bps: Decimal
    exit_policy: str
    atr: Decimal
    stop: Decimal
    target: Decimal
    stress_exit_price: Decimal
    planned_loss: Decimal
    notional: Decimal
    cost_basis: Decimal
    opened_at: str


@dataclass(frozen=True, slots=True)
class StoredClose:
    close_id: int
    position_id: int
    exit_reason: ExitReason
    exit_source: str
    exit_observation_id: int
    exit_bid: Decimal
    exit_ask: Decimal
    exit_ts: datetime
    record_lag_seconds: float
    quantity: Decimal
    gross: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    fees: Decimal
    net: Decimal
    outcome: Outcome
    stop: Decimal
    stop_slippage: Decimal
    stop_slippage_bps: Decimal
    closed_at: str


@dataclass(frozen=True, slots=True)
class StoredReview:
    review_id: int
    lock_id: int
    reviewer: str
    cause: str
    rebase_equity: Decimal
    reviewed_at: str


@dataclass(frozen=True, slots=True)
class StoredLock:
    lock_id: int
    kind: LockKind
    equity: Decimal
    reference: Decimal
    limit_pct: Decimal
    utc_day: str
    evaluated_on: LockTrigger
    tripped_at: str
    review: StoredReview | None = None

    @property
    def active(self) -> bool:
        return self.review is None


@dataclass(frozen=True, slots=True)
class StoredMark:
    mark_id: int
    kind: MarkKind
    utc_day: str | None
    equity: Decimal
    source: MarkSource
    lock_id: int | None
    recorded_at: str


@dataclass(frozen=True, slots=True)
class KillSwitch:
    engaged: bool
    reason: str | None = None
    actor: str | None = None
    recorded_at: str | None = None


@dataclass(frozen=True, slots=True)
class LockEvaluation:
    """The account at one evaluation: its equity, references and the locks in force."""

    equity: Decimal
    day_start: Decimal
    high_water: Decimal
    #: Locks this evaluation tripped (newly written rows).
    tripped: tuple[StoredLock, ...]
    #: Every lock active after this evaluation, oldest first.
    active: tuple[StoredLock, ...]

    @property
    def active_kinds(self) -> tuple[LockKind, ...]:
        return tuple(dict.fromkeys(lock.kind for lock in self.active))


@dataclass(frozen=True, slots=True)
class OpenReport:
    decisions: tuple[StoredDecision, ...]
    #: Event ids that already had a decision: nothing was written for them.
    already_recorded: tuple[str, ...]
    locks: LockEvaluation


@dataclass(frozen=True, slots=True)
class CloseReport:
    closed: tuple[StoredClose, ...]
    #: Positions past their due time with no valid exit quote yet; they stay open.
    pending: tuple[int, ...]
    locks: LockEvaluation


# --- helpers ------------------------------------------------------------------------------------


def _require_now(now: object) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "now must be a timezone-aware datetime")
    return now


def _parse_aware(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def utc_day(moment: datetime) -> str:
    """The UTC calendar day of ``moment`` as ``YYYY-MM-DD``."""
    return moment.astimezone(UTC).date().isoformat()


def decimal_text(value: Decimal) -> str:
    """Exact plain text of ``value`` (no exponent), as stored."""
    return format(value, "f")


def envelope_sha256(envelope: Envelope) -> str:
    return hashlib.sha256(envelope.canonical_text().encode("utf-8")).hexdigest()


def _to_cents(value: Decimal) -> int:
    if value != paper.cents(value):
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, f"{value} is not whole cents")
    return int(value * _HUNDRED)


def _from_cents(value: int) -> Decimal:
    return Decimal(value) / _HUNDRED


def _nonempty(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, f"{field} must be a non-empty string")
    return value


def _sqlite_error(what: str, error: sqlite3.Error) -> PilotStoreError:
    return PilotStoreError(PilotStoreFailure.SQLITE_ERROR, f"{what}: {error}")


# --- schema -------------------------------------------------------------------------------------


def _missing_objects(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    marks = ", ".join("?" for _ in TABLES)
    present = {
        (str(kind), str(name))
        for kind, name in conn.execute(
            f"SELECT type, name FROM sqlite_master WHERE tbl_name IN ({marks})", TABLES
        ).fetchall()
    }
    return [obj for obj in SCHEMA_OBJECTS if obj not in present]


def schema_present(conn: sqlite3.Connection) -> bool:
    """Whether every pilot table, trigger and index exists (read-only)."""
    try:
        return not _missing_objects(conn)
    except sqlite3.Error as error:
        raise _sqlite_error("reading sqlite_master", error) from error


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create the missing pilot tables, triggers and indexes; ``True`` when something was created.

    A read-only check comes first: a database that has everything is not written to at
    all. Otherwise one ``BEGIN IMMEDIATE`` transaction runs every ``CREATE ... IF NOT
    EXISTS`` and rolls back entirely on any error. No row is ever rewritten or deleted.
    """
    if conn.in_transaction:
        raise PilotStoreError(PilotStoreFailure.OPEN_TRANSACTION, "commit or roll back before ensure_schema")
    try:
        if not _missing_objects(conn):
            return False
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise _sqlite_error("creating pilot tables", error) from error
    return True


# --- reading ------------------------------------------------------------------------------------


def _malformed(field: str, value: object) -> PilotStoreError:
    return PilotStoreError(PilotStoreFailure.MALFORMED_ROW, f"stored {field} is {value!r}")


def _stored_int(field: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _malformed(field, value)
    return value


def _stored_str(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise _malformed(field, value)
    return value


def _stored_decimal(field: str, value: object) -> Decimal:
    try:
        number = Decimal(_stored_str(field, value))
    except InvalidOperation:
        raise _malformed(field, value) from None
    if not number.is_finite():
        raise _malformed(field, value)
    return number


def _optional_decimal(field: str, value: object) -> Decimal | None:
    return None if value is None else _stored_decimal(field, value)


def _optional_str(field: str, value: object) -> str | None:
    return None if value is None else _stored_str(field, value)


def _optional_int(field: str, value: object) -> int | None:
    return None if value is None else _stored_int(field, value)


def _stored_time(field: str, value: object) -> datetime:
    moment = _parse_aware(_stored_str(field, value))
    if moment is None:
        raise _malformed(field, value)
    return moment


def _stored_float(field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _malformed(field, value)
    return float(value)


def _stored_enum[E: Enum](kind: type[E], field: str, value: object) -> E:
    try:
        return kind(value)
    except ValueError:
        raise _malformed(field, value) from None


def _select(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> list[tuple[object, ...]]:
    try:
        return [tuple(record) for record in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error as error:
        raise _sqlite_error("reading pilot tables", error) from error


def parse_envelope(text: str) -> Envelope:
    """The ``Envelope`` of its canonical text (``Envelope.canonical_text``)."""
    try:
        loaded = json.loads(text)
        if not isinstance(loaded, dict) or loaded.get("policy_id") != risk.ENVELOPE_POLICY_ID:
            raise ValueError("unknown envelope policy")
        cap = loaded["per_entry_abs_cap"]
        envelope = Envelope(
            equity=Decimal(loaded["equity"]),
            currency=loaded["currency"],
            per_entry_loss_pct=Decimal(loaded["per_entry_loss_pct"]),
            aggregate_loss_pct=Decimal(loaded["aggregate_loss_pct"]),
            gross_notional_pct=Decimal(loaded["gross_notional_pct"]),
            cash_buffer_pct=Decimal(loaded["cash_buffer_pct"]),
            daily_loss_pct=Decimal(loaded["daily_loss_pct"]),
            drawdown_pct=Decimal(loaded["drawdown_pct"]),
            per_entry_abs_cap=None if cap is None else Decimal(cap),
            max_positions=loaded["max_positions"],
            leverage=loaded["leverage"],
        )
    except (ValueError, KeyError, TypeError, InvalidOperation) as error:
        raise _malformed("envelope_json", text) from error
    if envelope.canonical_text() != text:
        raise _malformed("envelope_json", text)
    return envelope


def read_account(conn: sqlite3.Connection) -> Account | None:
    """The recorded account, or ``None`` before it was recorded."""
    rows = _select(conn, f"SELECT envelope_json, envelope_sha256, recorded_at FROM {ACCOUNT_TABLE} WHERE account_id = 1")
    if not rows:
        return None
    text, sha, recorded_at = rows[0]
    envelope = parse_envelope(_stored_str("envelope_json", text))
    if envelope_sha256(envelope) != sha:
        raise _malformed("envelope_sha256", sha)
    return Account(envelope, _stored_str("envelope_sha256", sha), _stored_str("recorded_at", recorded_at))


_DECISION_DECIMALS = tuple(name for name in _DECISION_NULLABLE_TEXT if name not in {"atr_pair", "tick_source"})


def _decision(record: tuple[object, ...]) -> StoredDecision:
    value = dict(zip(("decision_id", *_DECISION_COLUMNS), record, strict=True))
    binding = value["binding"]
    reason = value["reason"]
    entry_fee = _optional_int("entry_fee_cents", value["entry_fee_cents"])
    exit_fee = _optional_int("exit_fee_cents", value["exit_fee_cents"])
    return StoredDecision(
        decision_id=_stored_int("decision_id", value["decision_id"]),
        event_id=_stored_str("event_id", value["event_id"]),
        run_id=_stored_str("run_id", value["run_id"]),
        asset=_stored_str("asset", value["asset"]),
        pair=_stored_str("pair", value["pair"]),
        quote=_stored_str("quote", value["quote"]),
        direction=_stored_str("direction", value["direction"]),
        snapshot_ts=_stored_str("snapshot_ts", value["snapshot_ts"]),
        values={name: _optional_decimal(name, value[name]) for name in _DECISION_DECIMALS},
        atr_pair=_optional_str("atr_pair", value["atr_pair"]),
        tick_source=_optional_str("tick_source", value["tick_source"]),
        lot_decimals=_optional_int("lot_decimals", value["lot_decimals"]),
        binding=None if binding is None else _stored_enum(risk.BindingConstraint, "binding", binding),
        passes=_stored_int("passes", value["passes"]),
        entry_fee=None if entry_fee is None else _from_cents(entry_fee),
        exit_fee=None if exit_fee is None else _from_cents(exit_fee),
        envelope_sha256=_stored_str("envelope_sha256", value["envelope_sha256"]),
        envelope_policy_id=_stored_str("envelope_policy_id", value["envelope_policy_id"]),
        sizing_policy_id=_stored_str("sizing_policy_id", value["sizing_policy_id"]),
        stress_policy_id=_stored_str("stress_policy_id", value["stress_policy_id"]),
        exit_policy_id=_stored_str("exit_policy_id", value["exit_policy_id"]),
        outcome=_stored_str("outcome", value["outcome"]),
        reason=None if reason is None else _stored_enum(NoTradeReason, "reason", reason),
        detail=_stored_str("detail", value["detail"]),
        decided_at=_stored_str("decided_at", value["decided_at"]),
    )


def read_decisions(conn: sqlite3.Connection, *, last: int | None = None) -> tuple[StoredDecision, ...]:
    """Every recorded decision oldest first, or only the ``last`` n (still oldest first)."""
    columns = ", ".join(("decision_id", *_DECISION_COLUMNS))
    if last is None:
        rows = _select(conn, f"SELECT {columns} FROM {DECISION_TABLE} ORDER BY decision_id")
    else:
        if isinstance(last, bool) or not isinstance(last, int) or last < 0:
            raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "last must be an int >= 0")
        rows = _select(conn, f"SELECT {columns} FROM {DECISION_TABLE} ORDER BY decision_id DESC LIMIT ?", (last,))
        rows.reverse()
    return tuple(_decision(row) for row in rows)


def no_trade_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """The number of recorded ``NO_TRADE`` decisions per reason; every reason is present."""
    counts = {reason.value: 0 for reason in NoTradeReason}
    for reason, count in _select(
        conn, f"SELECT reason, COUNT(*) FROM {DECISION_TABLE} WHERE outcome = '{NO_TRADE}' GROUP BY reason"
    ):
        counts[_stored_enum(NoTradeReason, "reason", reason).value] = _stored_int("count", count)
    return counts


def _position(record: tuple[object, ...]) -> StoredPosition:
    value = dict(zip(("position_id", *_POSITION_COLUMNS), record, strict=True))
    direction = paper.parse_direction(value["direction"])
    if direction is not Direction.LONG:
        raise _malformed("direction", value["direction"])
    return StoredPosition(
        position_id=_stored_int("position_id", value["position_id"]),
        decision_id=_stored_int("decision_id", value["decision_id"]),
        event_id=_stored_str("event_id", value["event_id"]),
        run_id=_stored_str("run_id", value["run_id"]),
        asset=_stored_str("asset", value["asset"]),
        pair=_stored_str("pair", value["pair"]),
        quote=_stored_str("quote", value["quote"]),
        direction=direction,
        quantity=_stored_decimal("quantity", value["quantity"]),
        entry_bid=_stored_decimal("entry_bid", value["entry_bid"]),
        entry_ask=_stored_decimal("entry_ask", value["entry_ask"]),
        entry_ts=_stored_time("entry_ts", value["entry_ts"]),
        due_at=_stored_time("due_at", value["due_at"]),
        fee_bps=_stored_decimal("fee_bps", value["fee_bps"]),
        exit_policy=_stored_str("exit_policy", value["exit_policy"]),
        atr=_stored_decimal("atr", value["atr"]),
        stop=_stored_decimal("stop_price", value["stop_price"]),
        target=_stored_decimal("target_price", value["target_price"]),
        stress_exit_price=_stored_decimal("stress_exit_price", value["stress_exit_price"]),
        planned_loss=_stored_decimal("planned_loss", value["planned_loss"]),
        notional=_stored_decimal("notional", value["notional"]),
        cost_basis=_stored_decimal("cost_basis", value["cost_basis"]),
        opened_at=_stored_str("opened_at", value["opened_at"]),
    )


_POSITION_SELECT = ", ".join(f"p.{column}" for column in ("position_id", *_POSITION_COLUMNS))


def read_positions(conn: sqlite3.Connection) -> tuple[StoredPosition, ...]:
    """Every recorded position, oldest first."""
    rows = _select(conn, f"SELECT {_POSITION_SELECT} FROM {POSITION_TABLE} p ORDER BY p.position_id")
    return tuple(_position(row) for row in rows)


def read_open_positions(conn: sqlite3.Connection) -> tuple[StoredPosition, ...]:
    """Positions without a close row, oldest first."""
    rows = _select(
        conn,
        f"SELECT {_POSITION_SELECT} FROM {POSITION_TABLE} p "
        f"WHERE NOT EXISTS (SELECT 1 FROM {CLOSE_TABLE} c WHERE c.position_id = p.position_id) "
        "ORDER BY p.position_id",
    )
    return tuple(_position(row) for row in rows)


def _close(record: tuple[object, ...]) -> StoredClose:
    value = dict(zip(("close_id", *_CLOSE_COLUMNS), record, strict=True))
    return StoredClose(
        close_id=_stored_int("close_id", value["close_id"]),
        position_id=_stored_int("position_id", value["position_id"]),
        exit_reason=_stored_enum(ExitReason, "exit_reason", value["exit_reason"]),
        exit_source=_stored_str("exit_source", value["exit_source"]),
        exit_observation_id=_stored_int("exit_observation_id", value["exit_observation_id"]),
        exit_bid=_stored_decimal("exit_bid", value["exit_bid"]),
        exit_ask=_stored_decimal("exit_ask", value["exit_ask"]),
        exit_ts=_stored_time("exit_ts", value["exit_ts"]),
        record_lag_seconds=_stored_float("record_lag_seconds", value["record_lag_seconds"]),
        quantity=_stored_decimal("quantity", value["quantity"]),
        gross=_from_cents(_stored_int("gross_cents", value["gross_cents"])),
        entry_fee=_from_cents(_stored_int("entry_fee_cents", value["entry_fee_cents"])),
        exit_fee=_from_cents(_stored_int("exit_fee_cents", value["exit_fee_cents"])),
        fees=_from_cents(_stored_int("fees_cents", value["fees_cents"])),
        net=_from_cents(_stored_int("net_cents", value["net_cents"])),
        outcome=_stored_enum(Outcome, "outcome", value["outcome"]),
        stop=_stored_decimal("stop_price", value["stop_price"]),
        stop_slippage=_stored_decimal("stop_slippage", value["stop_slippage"]),
        stop_slippage_bps=_stored_decimal("stop_slippage_bps", value["stop_slippage_bps"]),
        closed_at=_stored_str("closed_at", value["closed_at"]),
    )


def read_closes(conn: sqlite3.Connection) -> tuple[StoredClose, ...]:
    """Every recorded close, in the order they were written."""
    columns = ", ".join(("close_id", *_CLOSE_COLUMNS))
    return tuple(_close(row) for row in _select(conn, f"SELECT {columns} FROM {CLOSE_TABLE} ORDER BY close_id"))


def _review(record: tuple[object, ...]) -> StoredReview:
    review_id, lock_id, reviewer, cause, rebase, reviewed_at = record
    return StoredReview(
        review_id=_stored_int("review_id", review_id),
        lock_id=_stored_int("lock_id", lock_id),
        reviewer=_stored_str("reviewer", reviewer),
        cause=_stored_str("cause", cause),
        rebase_equity=_stored_decimal("rebase_equity", rebase),
        reviewed_at=_stored_str("reviewed_at", reviewed_at),
    )


def read_reviews(conn: sqlite3.Connection) -> tuple[StoredReview, ...]:
    """Every lock review, oldest first."""
    rows = _select(
        conn,
        f"SELECT review_id, lock_id, reviewer, cause, rebase_equity, reviewed_at FROM {REVIEW_TABLE} ORDER BY review_id",
    )
    return tuple(_review(row) for row in rows)


def read_locks(conn: sqlite3.Connection) -> tuple[StoredLock, ...]:
    """Every lock trip, oldest first, each with the review that cleared it (``None``: active)."""
    reviews = {review.lock_id: review for review in read_reviews(conn)}
    rows = _select(
        conn,
        f"SELECT lock_id, kind, equity, reference, limit_pct, utc_day, evaluated_on, tripped_at "
        f"FROM {LOCK_TABLE} ORDER BY lock_id",
    )
    locks: list[StoredLock] = []
    for lock_id, kind, equity, reference, limit_pct, day, evaluated_on, tripped_at in rows:
        ident = _stored_int("lock_id", lock_id)
        locks.append(
            StoredLock(
                lock_id=ident,
                kind=_stored_enum(LockKind, "kind", kind),
                equity=_stored_decimal("equity", equity),
                reference=_stored_decimal("reference", reference),
                limit_pct=_stored_decimal("limit_pct", limit_pct),
                utc_day=_stored_str("utc_day", day),
                evaluated_on=_stored_enum(LockTrigger, "evaluated_on", evaluated_on),
                tripped_at=_stored_str("tripped_at", tripped_at),
                review=reviews.get(ident),
            )
        )
    return tuple(locks)


def active_locks(conn: sqlite3.Connection) -> tuple[StoredLock, ...]:
    """Locks without a review, oldest first."""
    return tuple(lock for lock in read_locks(conn) if lock.active)


def read_marks(conn: sqlite3.Connection) -> tuple[StoredMark, ...]:
    """Every day-start and high-water mark, oldest first."""
    rows = _select(
        conn, f"SELECT mark_id, kind, utc_day, equity, source, lock_id, recorded_at FROM {MARK_TABLE} ORDER BY mark_id"
    )
    return tuple(
        StoredMark(
            mark_id=_stored_int("mark_id", mark_id),
            kind=_stored_enum(MarkKind, "kind", kind),
            utc_day=_optional_str("utc_day", day),
            equity=_stored_decimal("equity", equity),
            source=_stored_enum(MarkSource, "source", source),
            lock_id=_optional_int("lock_id", lock_id),
            recorded_at=_stored_str("recorded_at", recorded_at),
        )
        for mark_id, kind, day, equity, source, lock_id, recorded_at in rows
    )


def day_start(conn: sqlite3.Connection, day: str) -> Decimal | None:
    """The day reference of UTC ``day`` (the latest day-start mark of that day), if recorded."""
    rows = _select(
        conn,
        f"SELECT equity FROM {MARK_TABLE} WHERE kind = ? AND utc_day = ? ORDER BY mark_id DESC LIMIT 1",
        (MarkKind.DAY_START.value, day),
    )
    return _stored_decimal("equity", rows[0][0]) if rows else None


def high_water(conn: sqlite3.Connection) -> Decimal | None:
    """The current high-water mark (the latest high-water row), if the account is recorded."""
    rows = _select(
        conn,
        f"SELECT equity FROM {MARK_TABLE} WHERE kind = ? ORDER BY mark_id DESC LIMIT 1",
        (MarkKind.HIGH_WATER.value,),
    )
    return _stored_decimal("equity", rows[0][0]) if rows else None


def kill_switch(conn: sqlite3.Connection) -> KillSwitch:
    """The kill switch state: its latest row wins; with no row it is released."""
    rows = _select(
        conn, f"SELECT state, reason, actor, recorded_at FROM {KILL_SWITCH_TABLE} ORDER BY switch_id DESC LIMIT 1"
    )
    if not rows:
        return KillSwitch(engaged=False)
    state, reason, actor, recorded_at = rows[0]
    if state not in (ENGAGED, RELEASED):
        raise _malformed("state", state)
    return KillSwitch(
        engaged=state == ENGAGED,
        reason=_stored_str("reason", reason),
        actor=_stored_str("actor", actor),
        recorded_at=_stored_str("recorded_at", recorded_at),
    )


# --- writing: account, kill switch ------------------------------------------------------------


def _begin(conn: sqlite3.Connection, what: str) -> None:
    if conn.in_transaction:
        raise PilotStoreError(PilotStoreFailure.OPEN_TRANSACTION, f"commit or roll back before {what}")
    conn.execute("BEGIN IMMEDIATE")


def _insert(conn: sqlite3.Connection, table: str, values: Mapping[str, object]) -> int:
    columns = tuple(values)
    cursor = conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    if cursor.lastrowid is None:  # pragma: no cover - an INSERT always sets it
        raise PilotStoreError(PilotStoreFailure.SQLITE_ERROR, f"insert into {table} returned no row id")
    return cursor.lastrowid


def _insert_mark(
    conn: sqlite3.Connection,
    kind: MarkKind,
    equity: Decimal,
    source: MarkSource,
    recorded_at: str,
    *,
    day: str | None = None,
    lock_id: int | None = None,
) -> None:
    _insert(
        conn,
        MARK_TABLE,
        {
            "kind": kind.value,
            "utc_day": day,
            "equity": decimal_text(equity),
            "source": source.value,
            "lock_id": lock_id,
            "recorded_at": recorded_at,
        },
    )


def ensure_account(conn: sqlite3.Connection, *, envelope: Envelope, now: datetime) -> Account:
    """Record the envelope once, with the high-water mark at the assigned equity, and return it.

    An existing account is returned unchanged: history is never rewritten. A different
    ``envelope`` later refuses entries (``envelope_changed``) rather than replacing it.
    """
    if not isinstance(envelope, Envelope):
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "envelope must be an Envelope")
    recorded_at = utc_text(_require_now(now))
    existing = read_account(conn)
    if existing is not None:
        return existing
    try:
        _begin(conn, "ensure_account")
        try:
            if read_account(conn) is None:
                _insert(
                    conn,
                    ACCOUNT_TABLE,
                    {
                        "account_id": 1,
                        "envelope_json": envelope.canonical_text(),
                        "envelope_sha256": envelope_sha256(envelope),
                        "envelope_policy_id": risk.ENVELOPE_POLICY_ID,
                        "equity_cents": _to_cents(envelope.equity),
                        "currency": envelope.currency,
                        "recorded_at": recorded_at,
                    },
                )
                _insert_mark(conn, MarkKind.HIGH_WATER, envelope.equity, MarkSource.ASSIGNED, recorded_at)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise _sqlite_error(f"writing {ACCOUNT_TABLE}", error) from error
    account = read_account(conn)
    if account is None:  # pragma: no cover - the insert above committed
        raise PilotStoreError(PilotStoreFailure.NO_ACCOUNT, "account missing after insert")
    return account


def _set_kill_switch(conn: sqlite3.Connection, state: str, reason: object, actor: object, now: datetime) -> KillSwitch:
    values = {
        "state": state,
        "reason": _nonempty("reason", reason),
        "actor": _nonempty("actor", actor),
        "recorded_at": utc_text(_require_now(now)),
    }
    try:
        _begin(conn, f"setting the kill switch {state}")
        try:
            _insert(conn, KILL_SWITCH_TABLE, values)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise _sqlite_error(f"writing {KILL_SWITCH_TABLE}", error) from error
    return kill_switch(conn)


def engage_kill_switch(conn: sqlite3.Connection, *, reason: str, actor: str, now: datetime) -> KillSwitch:
    """Append an ``ENGAGED`` row: every new candidate is refused until a release."""
    return _set_kill_switch(conn, ENGAGED, reason, actor, now)


def release_kill_switch(conn: sqlite3.Connection, *, reason: str, actor: str, now: datetime) -> KillSwitch:
    """Append a ``RELEASED`` row: new candidates are evaluated again."""
    return _set_kill_switch(conn, RELEASED, reason, actor, now)


# --- equity and locks ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Observation:
    """A valid quote of one pair at one time: a ``spot_snapshots`` row or an extra quote."""

    observed_at: datetime
    #: 0 for a recorded snapshot, 1 for an extra quote: a recorded row wins an exact tie.
    rank: int
    #: The snapshot id, or the extra quote's position in the call.
    ident: int
    quote: paper.Quote
    extra: ObservedQuote | None

    @property
    def order(self) -> tuple[datetime, int, int]:
        return (self.observed_at, self.rank, self.ident)


def _spot_observations(
    conn: sqlite3.Connection, pair: str, start: datetime, now: datetime, *, include_start: bool
) -> list[_Observation]:
    """Valid spot quotes of exactly ``pair`` observed after ``start`` (at it too when
    ``include_start``) and up to ``now``, in time order."""
    rows = conn.execute(
        "SELECT id, ts, bid, ask, status FROM spot_snapshots WHERE pair = ? AND ts >= ? AND ts <= ? ORDER BY ts, id",
        (pair, utc_text(start - SNAPSHOT_TEXT_MARGIN), utc_text(now + SNAPSHOT_TEXT_MARGIN)),
    ).fetchall()
    found: list[_Observation] = []
    for record in rows:
        snapshot_id, ts, bid, ask, status = tuple(record)
        if isinstance(snapshot_id, bool) or not isinstance(snapshot_id, int) or not isinstance(ts, str):
            continue
        observed_at = _parse_aware(ts)
        if observed_at is None or observed_at.utcoffset() != timedelta(0):
            continue  # cannot be placed in time with the text order
        if observed_at > now or observed_at < start or (observed_at == start and not include_start):
            continue
        quote = paper.validate_quote(bid, ask, status)
        if isinstance(quote, paper.QuoteProblem):
            continue
        found.append(_Observation(observed_at, 0, snapshot_id, quote, None))
    found.sort(key=lambda observation: observation.order)
    return found


def _extra_observations(
    extras: Sequence[ObservedQuote], pair: str, start: datetime, now: datetime, *, include_start: bool
) -> list[_Observation]:
    """The extra quotes of exactly ``pair`` observed after ``start`` and up to ``now`` whose
    bid/ask are a valid quote; any other is ignored, never read as zero."""
    found: list[_Observation] = []
    for index, extra in enumerate(extras):
        moment = extra.observed_at
        if extra.pair != pair or moment > now or moment < start or (moment == start and not include_start):
            continue
        quote = paper.validate_quote(extra.bid, extra.ask)
        if isinstance(quote, paper.QuoteProblem):
            continue
        found.append(_Observation(moment.astimezone(UTC), 1, index, quote, extra))
    return found


def _check_extra_quotes(extra_quotes: object) -> tuple[ObservedQuote, ...]:
    if isinstance(extra_quotes, (str, bytes)) or not isinstance(extra_quotes, Sequence):
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "extra_quotes must be a sequence of ObservedQuote")
    for index, extra in enumerate(extra_quotes):
        where = f"extra_quotes[{index}]"
        if not isinstance(extra, ObservedQuote):
            raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, f"{where} must be an ObservedQuote")
        _nonempty(f"{where}.pair", extra.pair)
        if _nonempty(f"{where}.source", extra.source) == SPOT_SNAPSHOT_SOURCE:
            raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, f"{where}.source must not be {SPOT_SNAPSHOT_SOURCE!r}")
        moment = extra.observed_at
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, f"{where}.observed_at must be timezone-aware")
    return tuple(extra_quotes)


def _mark_quote(
    conn: sqlite3.Connection, position: StoredPosition, extras: Sequence[ObservedQuote], now: datetime
) -> paper.Quote:
    """The latest valid quote of the position's pair observed from entry up to ``now``; the
    entry quote itself when nothing newer was observed."""
    observations = _spot_observations(conn, position.pair, position.entry_ts, now, include_start=True)
    observations.extend(_extra_observations(extras, position.pair, position.entry_ts, now, include_start=True))
    if observations:
        return max(observations, key=lambda observation: observation.order).quote
    entry = paper.validate_quote(position.entry_bid, position.entry_ask)
    if isinstance(entry, paper.QuoteProblem):
        raise _malformed("entry quote", (position.entry_bid, position.entry_ask))
    return entry


@dataclass(frozen=True, slots=True)
class AccountState:
    """The account at one moment, from the recorded rows and the latest quotes."""

    equity: Decimal
    cash: Decimal
    realized: Decimal
    open_positions: tuple[StoredPosition, ...]
    open_planned_loss: Decimal
    open_notional: Decimal


def _account_state(
    conn: sqlite3.Connection, account: Account, extras: Sequence[ObservedQuote], now: datetime
) -> AccountState:
    nets = [close.net for close in read_closes(conn)]
    positions = read_open_positions(conn)
    marks = [
        risk.unrealized_mark(position.quantity, _mark_quote(conn, position, extras, now).bid, position.cost_basis, position.fee_bps)
        for position in positions
    ]
    assigned = account.envelope.equity
    with localcontext(risk.RISK_CONTEXT):
        return AccountState(
            equity=risk.account_equity(assigned, nets, marks),
            cash=risk.available_cash(assigned, nets, [position.cost_basis for position in positions]),
            realized=sum(nets, Decimal(0)),
            open_positions=positions,
            open_planned_loss=sum((position.planned_loss for position in positions), Decimal(0)),
            open_notional=sum((position.notional for position in positions), Decimal(0)),
        )


def account_state(
    conn: sqlite3.Connection, *, now: datetime, extra_quotes: Sequence[ObservedQuote] = ()
) -> AccountState | None:
    """The account state at ``now`` (read-only), or ``None`` before the account is recorded."""
    moment = _require_now(now)
    extras = _check_extra_quotes(extra_quotes)
    account = read_account(conn)
    if account is None:
        return None
    try:
        return _account_state(conn, account, extras, moment)
    except sqlite3.Error as error:
        raise _sqlite_error("reading the pilot account", error) from error


def _evaluate_locks(
    conn: sqlite3.Connection,
    account: Account,
    state: AccountState,
    now: datetime,
    trigger: LockTrigger,
) -> LockEvaluation:
    """Record the day-start and a new high-water mark when due, and trip the locks the current
    equity breaks that are not already active. Runs under the caller's write lock; never
    clears anything."""
    recorded_at = utc_text(now)
    day = utc_day(now)
    equity = state.equity
    reference = day_start(conn, day)
    if reference is None:
        _insert_mark(conn, MarkKind.DAY_START, equity, MarkSource.EVALUATION, recorded_at, day=day)
        reference = equity
    water = high_water(conn)
    if water is None:
        raise _malformed(MARK_TABLE, "no high-water mark")
    raised = risk.next_high_water(water, equity)
    if raised != water:
        _insert_mark(conn, MarkKind.HIGH_WATER, raised, MarkSource.EVALUATION, recorded_at)
        water = raised
    envelope = account.envelope
    already = {lock.kind for lock in active_locks(conn)}
    tripped_ids: list[int] = []
    for kind in risk.tripped_locks(envelope, equity, reference, water):
        if kind in already:
            continue
        daily = kind is LockKind.DAILY_LOSS
        tripped_ids.append(
            _insert(
                conn,
                LOCK_TABLE,
                {
                    "kind": kind.value,
                    "equity": decimal_text(equity),
                    "reference": decimal_text(reference if daily else water),
                    "limit_pct": decimal_text(envelope.daily_loss_pct if daily else envelope.drawdown_pct),
                    "utc_day": day,
                    "evaluated_on": trigger.value,
                    "tripped_at": recorded_at,
                },
            )
        )
    locks = read_locks(conn)
    return LockEvaluation(
        equity=equity,
        day_start=reference,
        high_water=water,
        tripped=tuple(lock for lock in locks if lock.lock_id in tripped_ids),
        active=tuple(lock for lock in locks if lock.active),
    )


def _require_account(conn: sqlite3.Connection) -> Account:
    account = read_account(conn)
    if account is None:
        raise PilotStoreError(PilotStoreFailure.NO_ACCOUNT, "record the account (ensure_account) first")
    return account


def evaluate_locks(
    conn: sqlite3.Connection, *, now: datetime, extra_quotes: Sequence[ObservedQuote] = ()
) -> LockEvaluation:
    """Mark the account at ``now`` (spot snapshots plus ``extra_quotes``) and trip any lock due."""
    moment = _require_now(now)
    extras = _check_extra_quotes(extra_quotes)
    try:
        _begin(conn, "evaluate_locks")
        try:
            account = _require_account(conn)
            result = _evaluate_locks(conn, account, _account_state(conn, account, extras, moment), moment, LockTrigger.MARK)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise _sqlite_error(f"writing {LOCK_TABLE}", error) from error
    return result


def review_lock(
    conn: sqlite3.Connection,
    *,
    lock_id: int,
    reviewer: str,
    cause: str,
    now: datetime,
    extra_quotes: Sequence[ObservedQuote] = (),
) -> StoredReview:
    """Clear lock ``lock_id`` with an explicit review and rebase its reference.

    The review records the equity at ``now``; that equity becomes the new high-water mark
    (drawdown) or the day reference for the rest of the UTC day (daily loss), written as a
    mark with source ``review``, so the same loss does not trip the lock again at once.
    """
    if isinstance(lock_id, bool) or not isinstance(lock_id, int):
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "lock_id must be an int")
    values = {"reviewer": _nonempty("reviewer", reviewer), "cause": _nonempty("cause", cause)}
    moment = _require_now(now)
    extras = _check_extra_quotes(extra_quotes)
    reviewed_at = utc_text(moment)
    try:
        _begin(conn, "review_lock")
        try:
            account = _require_account(conn)
            lock = next((found for found in read_locks(conn) if found.lock_id == lock_id), None)
            if lock is None:
                raise PilotStoreError(PilotStoreFailure.UNKNOWN_LOCK, f"no lock {lock_id}")
            if lock.review is not None:
                raise PilotStoreError(PilotStoreFailure.LOCK_ALREADY_REVIEWED, f"lock {lock_id} was reviewed already")
            equity = _account_state(conn, account, extras, moment).equity
            review_id = _insert(
                conn,
                REVIEW_TABLE,
                {"lock_id": lock_id, **values, "rebase_equity": decimal_text(equity), "reviewed_at": reviewed_at},
            )
            if lock.kind is LockKind.DRAWDOWN:
                _insert_mark(conn, MarkKind.HIGH_WATER, equity, MarkSource.REVIEW, reviewed_at, lock_id=lock_id)
            else:
                _insert_mark(
                    conn, MarkKind.DAY_START, equity, MarkSource.REVIEW, reviewed_at, day=utc_day(moment), lock_id=lock_id
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise _sqlite_error(f"writing {REVIEW_TABLE}", error) from error
    return next(review for review in read_reviews(conn) if review.review_id == review_id)


# --- opening -------------------------------------------------------------------------------------


def _invalid(index: int, field: str, detail: str) -> PilotStoreError:
    return PilotStoreError(PilotStoreFailure.INVALID_ROW, f"candidate {index} {field}: {detail}")


def _check_candidate(index: int, candidate: object) -> tuple[PilotCandidate, datetime]:
    if not isinstance(candidate, PilotCandidate):
        raise _invalid(index, "candidate", f"must be a PilotCandidate, got {type(candidate).__name__}")
    for field in ("event_id", "run_id", "asset", "pair", "quote", "snapshot_ts"):
        value = getattr(candidate, field)
        if not isinstance(value, str) or not value.strip():
            raise _invalid(index, field, f"must be a non-empty string, got {value!r}")
    entry_ts = _parse_aware(candidate.snapshot_ts)
    if entry_ts is None:
        raise _invalid(index, "snapshot_ts", f"must be ISO-8601 text with a UTC offset, got {candidate.snapshot_ts!r}")
    return candidate, entry_ts


def _sizing_inputs(state: AccountState) -> tuple[object, object, Decimal, Decimal]:
    """What the sizing reads from the account: equity, cash, open planned loss, open notional.

    With one position at most (F5) the sizing only runs when nothing is open, so equity and
    cash equal the assigned equity plus the realized nets. Kept as one seam so a test can
    drive the refusals this state never produces (no budget, no room, no cash, no equity).
    """
    return state.equity, state.cash, state.open_planned_loss, state.open_notional


def _optional_text(value: Decimal | None) -> str | None:
    return None if value is None else decimal_text(value)


def _plan(candidate: PilotCandidate, envelope: Envelope, state: AccountState, fee_bps: Decimal) -> risk.EntryPlan:
    equity, cash, open_loss, open_notional = _sizing_inputs(state)
    same_pair = candidate.atr_pair == candidate.pair
    plan = risk.plan_long_entry(
        envelope,
        quote_currency=candidate.quote,
        bid=candidate.bid,
        ask=candidate.ask,
        status=candidate.status,
        atr=candidate.atr if same_pair else None,
        pair_entry=candidate.pair_entry,
        equity=equity,
        cash=cash,
        open_planned_loss=open_loss,
        open_notional=open_notional,
        fee_bps=fee_bps,
    )
    refusal = plan.no_trade
    if not same_pair and refusal is not None and refusal.reason is NoTradeReason.NO_VALID_ATR:
        detail = f"pair_mismatch:{candidate.atr_pair!r}!={candidate.pair!r}"
        plan = risk.EntryPlan(no_trade=NoTrade(NoTradeReason.NO_VALID_ATR, detail), fee_bps=plan.fee_bps, quote=plan.quote)
    return plan


def _decision_values(
    candidate: PilotCandidate,
    refusal: NoTrade | None,
    plan: risk.EntryPlan | None,
    state: AccountState,
    fee_bps: Decimal,
    sha: str,
    decided_at: str,
) -> dict[str, object]:
    quote = plan.quote if plan is not None else None
    if quote is None:
        checked = paper.validate_quote(candidate.bid, candidate.ask, candidate.status)
        quote = None if isinstance(checked, paper.QuoteProblem) else checked
    atr = plan.atr if plan is not None else None
    if atr is None:
        checked_atr = paper.validate_atr(candidate.atr)
        atr = None if isinstance(checked_atr, paper.QuoteProblem) else checked_atr
    rules = plan.rules if plan is not None else None
    room = plan.budget if plan is not None else None
    candidates = plan.candidates if plan is not None else None
    opened = plan is not None and plan.no_trade is None
    costs = plan.costs if opened and plan is not None else None
    no_trade = refusal if refusal is not None else (plan.no_trade if plan is not None else None)
    equity, cash, _, _ = _sizing_inputs(state)
    decimals: dict[str, Decimal | None] = {
        "bid": None if quote is None else quote.bid,
        "ask": None if quote is None else quote.ask,
        "atr": atr,
        "ordermin": None if rules is None else rules.ordermin,
        "costmin": None if rules is None else rules.costmin,
        "tick": None if rules is None else rules.tick,
        "equity": _decimal_or_none(equity),
        "cash": _decimal_or_none(cash),
        "stop_price": None if plan is None else plan.stop,
        "target_price": None if plan is None else plan.target,
        "stress_exit_price": None if plan is None else plan.stress_exit_price,
        "loss_per_unit": None if plan is None else plan.loss_per_unit,
        "per_entry_loss_cap": None if room is None else room.per_entry_loss_cap,
        "aggregate_loss_remaining": None if room is None else room.aggregate_loss_remaining,
        "notional_remaining": None if room is None else room.notional_remaining,
        "cash_room": None if room is None else room.cash_room,
        "qty_per_entry_loss": None if candidates is None else candidates.per_entry_loss,
        "qty_aggregate_loss": None if candidates is None else candidates.aggregate_loss,
        "qty_notional": None if candidates is None else candidates.notional,
        "qty_cash": None if candidates is None else candidates.cash,
        "lot_quantity": None if plan is None else plan.lot_quantity,
        "quantity": None if costs is None else costs.quantity,
        "notional": None if costs is None else costs.notional,
        "planned_loss": None if costs is None else costs.planned_loss,
    }
    values: dict[str, object] = {
        "event_id": candidate.event_id,
        "run_id": candidate.run_id,
        "asset": candidate.asset,
        "pair": candidate.pair,
        "quote": candidate.quote,
        "direction": _direction_text(candidate.direction),
        "snapshot_ts": candidate.snapshot_ts,
        **{name: _optional_text(value) for name, value in decimals.items()},
        "atr_pair": candidate.atr_pair if isinstance(candidate.atr_pair, str) and candidate.atr_pair else None,
        "tick_source": None if rules is None else rules.tick_source,
        "lot_decimals": None if rules is None else rules.lot_decimals,
        "binding": None if candidates is None else candidates.binding.value,
        "passes": 0 if plan is None else plan.passes,
        "entry_fee_cents": None if costs is None else _to_cents(costs.entry_fee),
        "exit_fee_cents": None if costs is None else _to_cents(costs.exit_fee),
        "fee_bps": decimal_text(fee_bps),
        "stress_bps": decimal_text(risk.PILOT_STRESS_EXIT_SLIPPAGE_BPS),
        "envelope_sha256": sha,
        "envelope_policy_id": risk.ENVELOPE_POLICY_ID,
        "sizing_policy_id": risk.SIZING_POLICY_ID,
        "stress_policy_id": risk.PILOT_STRESS_POLICY_ID,
        "exit_policy_id": paper.EX1_POLICY_ID,
        "outcome": OPENED if no_trade is None else NO_TRADE,
        "reason": None if no_trade is None else no_trade.reason.value,
        "detail": "" if no_trade is None else no_trade.detail,
        "decided_at": decided_at,
    }
    return values


def _direction_text(direction: object) -> str:
    """``LONG``/``SHORT`` for a string or a ``Direction``; any other value as its repr."""
    if isinstance(direction, Direction):
        return direction.value
    return direction if isinstance(direction, str) and direction else repr(direction)


def _decimal_or_none(value: object) -> Decimal | None:
    if isinstance(value, Decimal) and value.is_finite():
        return value
    return None


def _write_position(
    conn: sqlite3.Connection,
    decision_id: int,
    candidate: PilotCandidate,
    entry_ts: datetime,
    plan: risk.EntryPlan,
    opened_at: str,
) -> int:
    quote, levels, costs = plan.quote, plan.levels, plan.costs
    if quote is None or levels is None or costs is None or plan.stop is None or plan.target is None:
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "an opened plan must carry its quote, levels and costs")
    if plan.stress_exit_price is None:
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "an opened plan must carry its stress exit price")
    cost_basis = risk.entry_cost_basis(costs.quantity, quote.ask, plan.fee_bps)
    return _insert(
        conn,
        POSITION_TABLE,
        {
            "decision_id": decision_id,
            "event_id": candidate.event_id,
            "run_id": candidate.run_id,
            "asset": candidate.asset,
            "pair": candidate.pair,
            "quote": candidate.quote,
            "direction": Direction.LONG.value,
            "quantity": decimal_text(costs.quantity),
            "entry_bid": decimal_text(quote.bid),
            "entry_ask": decimal_text(quote.ask),
            "entry_ts": utc_text(entry_ts),
            "due_at": utc_text(paper.due_at(entry_ts, levels.hold_minutes)),
            "fee_bps": decimal_text(plan.fee_bps),
            "exit_policy": levels.policy_id,
            "atr": decimal_text(levels.atr),
            "stop_price": decimal_text(plan.stop),
            "target_price": decimal_text(plan.target),
            "stress_exit_price": decimal_text(plan.stress_exit_price),
            "planned_loss": decimal_text(costs.planned_loss),
            "notional": decimal_text(costs.notional),
            "cost_basis": decimal_text(cost_basis),
            "opened_at": opened_at,
        },
    )


def _check_fee(fee_bps: object) -> Decimal:
    if isinstance(fee_bps, bool) or not isinstance(fee_bps, Decimal) or not fee_bps.is_finite() or fee_bps < 0:
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "fee_bps must be a finite Decimal >= 0")
    return fee_bps


def open_candidates(
    conn: sqlite3.Connection,
    candidates: Sequence[PilotCandidate],
    *,
    envelope: Envelope,
    fee_bps: Decimal,
    now: datetime,
    extra_quotes: Sequence[ObservedQuote] = (),
) -> OpenReport:
    """Evaluate each candidate, in order, in one transaction; write one decision per new event.

    Every candidate is validated before anything is written (``INVALID_ROW``). Under the
    write lock the locks are evaluated first (``LockTrigger.ENTRY``), then each candidate
    is checked in order: event already decided (nothing written), LONG only, ``envelope``
    equal to the recorded one, kill switch released, no active lock, no open position
    (F5), quote currency equal to the envelope currency, valid quote, a valid ATR of the
    same pair, complete pair rules, then the sizing of ``domain.risk.plan_long_entry``.
    Each new event writes exactly one ``pilot_decisions`` row; an admitted one also writes
    its ``pilot_positions`` row. The account must be recorded (``ensure_account``).
    """
    moment = _require_now(now)
    decided_at = utc_text(moment)
    if not isinstance(envelope, Envelope):
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "envelope must be an Envelope")
    fee = _check_fee(fee_bps)
    extras = _check_extra_quotes(extra_quotes)
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "candidates must be a sequence of PilotCandidate")
    checked = [_check_candidate(index, candidate) for index, candidate in enumerate(candidates)]
    offered_sha = envelope_sha256(envelope)
    decision_ids: list[int] = []
    already: list[str] = []
    try:
        _begin(conn, "open_candidates")
        try:
            account = _require_account(conn)
            state = _account_state(conn, account, extras, moment)
            locks = _evaluate_locks(conn, account, state, moment, LockTrigger.ENTRY)
            engaged = kill_switch(conn).engaged
            recorded = {str(row[0]) for row in _select(conn, f"SELECT event_id FROM {DECISION_TABLE}")}
            for candidate, entry_ts in checked:
                if candidate.event_id in recorded:
                    already.append(candidate.event_id)
                    continue
                refusal: NoTrade | None
                plan: risk.EntryPlan | None = None
                if _direction_text(candidate.direction) != Direction.LONG.value:
                    refusal = NoTrade(NoTradeReason.UNSUPPORTED_DIRECTION, _direction_text(candidate.direction))
                elif offered_sha != account.envelope_sha256:
                    refusal = NoTrade(NoTradeReason.ENVELOPE_CHANGED, f"recorded:{account.envelope_sha256[:12]}")
                else:
                    refusal = risk.admission_refusal(
                        account.envelope,
                        Direction.LONG,
                        kill_switch_engaged=engaged,
                        active_locks=locks.active_kinds,
                        open_positions=len(state.open_positions),
                    )
                    if refusal is None:
                        plan = _plan(candidate, account.envelope, state, fee)
                values = _decision_values(candidate, refusal, plan, state, fee, offered_sha, decided_at)
                decision_id = _insert(conn, DECISION_TABLE, values)
                decision_ids.append(decision_id)
                recorded.add(candidate.event_id)
                if plan is not None and plan.opened:
                    _write_position(conn, decision_id, candidate, entry_ts, plan, decided_at)
                    state = _account_state(conn, account, extras, moment)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise _sqlite_error(f"writing {DECISION_TABLE}", error) from error
    wanted = set(decision_ids)
    decisions = tuple(decision for decision in read_decisions(conn) if decision.decision_id in wanted)
    return OpenReport(decisions, tuple(already), locks)


# --- closing -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Exit:
    position: StoredPosition
    observation: _Observation
    reason: ExitReason


def _find_exit(
    conn: sqlite3.Connection, position: StoredPosition, extras: Sequence[ObservedQuote], now: datetime
) -> _Exit | None:
    """The first valid quote of the position's pair observed strictly after entry and up to
    ``now`` that touches its stop or target or is at/after its due time
    (``domain.paper.exit_decision``, the phase 1 rule); ``None`` while it stays open."""
    observations = _spot_observations(conn, position.pair, position.entry_ts, now, include_start=False)
    observations.extend(_extra_observations(extras, position.pair, position.entry_ts, now, include_start=False))
    observations.sort(key=lambda observation: observation.order)
    for observation in observations:
        reason = paper.exit_decision(
            Direction.LONG, position.stop, position.target, position.due_at, observation.observed_at, observation.quote
        )
        if reason is not None:
            return _Exit(position, observation, reason)
    return None


def _write_close(conn: sqlite3.Connection, found: _Exit, now: datetime, closed_at: str) -> int | None:
    """Insert the close of ``found`` under the caller's write lock; ``None`` when the position
    already has a close (another writer got there first): nothing is written then."""
    position, observation = found.position, found.observation
    if conn.execute(f"SELECT 1 FROM {CLOSE_TABLE} WHERE position_id = ?", (position.position_id,)).fetchone():
        return None
    result = risk.settle_long(position.quantity, position.entry_ask, observation.quote.bid, position.fee_bps)
    observed_text = utc_text(observation.observed_at)
    source = SPOT_SNAPSHOT_SOURCE
    exit_id = observation.ident
    if observation.extra is not None:
        source = observation.extra.source
        exit_id = _insert(
            conn,
            EXIT_QUOTE_TABLE,
            {
                "position_id": position.position_id,
                "pair": position.pair,
                "bid": decimal_text(observation.quote.bid),
                "ask": decimal_text(observation.quote.ask),
                "observed_at": observed_text,
                "source": source,
                "recorded_at": closed_at,
            },
        )
    with localcontext(risk.RISK_CONTEXT):
        slippage = position.stop - observation.quote.bid
        slippage_bps = slippage / position.stop * paper.BPS
    return _insert(
        conn,
        CLOSE_TABLE,
        {
            "position_id": position.position_id,
            "exit_reason": found.reason.value,
            "exit_source": source,
            "exit_observation_id": exit_id,
            "exit_bid": decimal_text(observation.quote.bid),
            "exit_ask": decimal_text(observation.quote.ask),
            "exit_ts": observed_text,
            "record_lag_seconds": (now - observation.observed_at).total_seconds(),
            "quantity": decimal_text(position.quantity),
            "gross_cents": _to_cents(result.gross),
            "entry_fee_cents": _to_cents(result.entry_fee),
            "exit_fee_cents": _to_cents(result.exit_fee),
            "fees_cents": _to_cents(result.fees),
            "net_cents": _to_cents(result.net),
            "outcome": result.outcome.value,
            "stop_price": decimal_text(position.stop),
            "stop_slippage": decimal_text(slippage),
            "stop_slippage_bps": decimal_text(slippage_bps),
            "closed_at": closed_at,
        },
    )


def close_positions(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    extra_quotes: Sequence[ObservedQuote] = (),
    position_ids: Collection[int] | None = None,
) -> CloseReport:
    """Close every open position (or only those in ``position_ids``) whose exit is observed
    by ``now``, then evaluate the locks, in one transaction.

    ``extra_quotes`` are quotes observed outside ``spot_snapshots`` (e.g. a public Ticker
    read by the monitor): merged with the recorded snapshots, the earliest touching
    observation wins (a recorded row wins an exact tie); the one that closes a position is
    stored in ``pilot_exit_quotes``. A position closed by another writer meanwhile is left
    as it is. Past its due time with no valid quote, a position stays open as pending.
    Closing is risk reduction: it runs with the kill switch engaged and locks active.
    """
    moment = _require_now(now)
    closed_at = utc_text(moment)
    extras = _check_extra_quotes(extra_quotes)
    wanted_ids: set[int] | None = None
    if position_ids is not None:
        if isinstance(position_ids, (str, bytes)) or not all(
            isinstance(ident, int) and not isinstance(ident, bool) for ident in position_ids
        ):
            raise PilotStoreError(PilotStoreFailure.INVALID_ARGUMENT, "position_ids must be a collection of int")
        wanted_ids = set(position_ids)
    if conn.in_transaction:
        raise PilotStoreError(PilotStoreFailure.OPEN_TRANSACTION, "commit or roll back before close_positions")
    exits: list[_Exit] = []
    pending: list[int] = []
    try:
        for position in read_open_positions(conn):
            if wanted_ids is not None and position.position_id not in wanted_ids:
                continue
            found = _find_exit(conn, position, extras, moment)
            if found is not None:
                exits.append(found)
            elif position.due_at <= moment:
                pending.append(position.position_id)
    except sqlite3.Error as error:
        raise _sqlite_error("reading exit quotes", error) from error
    closed_ids: list[int] = []
    try:
        _begin(conn, "close_positions")
        try:
            account = _require_account(conn)
            for found in exits:
                close_id = _write_close(conn, found, moment, closed_at)
                if close_id is not None:
                    closed_ids.append(close_id)
            state = _account_state(conn, account, extras, moment)
            locks = _evaluate_locks(conn, account, state, moment, LockTrigger.SETTLE)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise _sqlite_error(f"writing {CLOSE_TABLE}", error) from error
    wanted = set(closed_ids)
    closed = tuple(close for close in read_closes(conn) if close.close_id in wanted)
    return CloseReport(closed, tuple(pending), locks)
