"""SQLite store of the paper game: one pretend wallet, its plays and their closes.

Four append-only tables:

* ``paper_wallet`` - one row (``wallet_id = 1``): the start balance and currency, recorded
  once. A later configuration change never rewrites it.
* ``paper_plays`` - one row per opened play (``UNIQUE(event_id)``). Each play freezes its
  own stake, fee per leg, hold, pair, quote currency, entry bid/ask and snapshot time, the
  recorded facts of why it was opened (a JSON object) and, since the EX-1 exit policy, its
  exit plan: policy id, ATR, stop and target (``domain.paper.exit_levels``).
* ``paper_closes`` - one row per closed play (``UNIQUE(play_id)``): the exit bid/ask and
  the observation it came from, the due time, the delay past it and the cent amounts;
  since the EX-1 policy also the exit reason (stop, target, time), the exit source and the
  record lag. A play without a close row is open.
* ``paper_exit_quotes`` - a quote from outside ``spot_snapshots`` (e.g. a Ticker read by a
  monitor) that closed a play, stored with its source; only such closing quotes are kept.

Legacy plays (opened before the EX-1 policy) have no levels: they keep their frozen hold
and close by the old rule, on the first valid spot snapshot at or after their due time,
with no exit reason.

Like ``qwen_reviews`` the tables sit outside the schema-version ledger
(``evidence_store.SCHEMA_MIGRATIONS``) on purpose: a process still running earlier code
refuses a ledger version it does not know, while extra tables are invisible to it.
``ensure_schema`` therefore uses only ``CREATE ... IF NOT EXISTS`` and nullable
``ALTER TABLE ... ADD COLUMN`` (``ADDED_COLUMNS``), in one ``BEGIN IMMEDIATE`` transaction;
it never rewrites or deletes a row and writes nothing when every object and column
already exists. Only the radar process calls it; readers check ``schema_present`` instead
and read a column that was not added yet as ``None``.

Money is stored in integer cents, prices and the fee rate as exact decimal text; times as
fixed-width UTC text (text order is time order). Every write is one ``BEGIN IMMEDIATE``
transaction that rolls back entirely on any error; admission is decided under that write
lock, so two writers can never both spend the same cash. The CHECK constraints and
triggers are the backstop behind the validation done here and in ``domain.paper``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum

from ..domain import paper

WALLET_TABLE = "paper_wallet"
PLAY_TABLE = "paper_plays"
CLOSE_TABLE = "paper_closes"
EXIT_QUOTE_TABLE = "paper_exit_quotes"
TABLES = (WALLET_TABLE, PLAY_TABLE, CLOSE_TABLE, EXIT_QUOTE_TABLE)
#: The tables a reader needs; ``paper_exit_quotes`` came later with the EX-1 policy.
BASE_TABLES = (WALLET_TABLE, PLAY_TABLE, CLOSE_TABLE)
#: ``exit_source`` of a close taken from ``spot_snapshots``.
SPOT_SNAPSHOT_SOURCE = "spot_snapshot"

#: Spot rows are pre-selected as text with this margin, then their time is parsed exactly.
SNAPSHOT_TEXT_MARGIN = timedelta(minutes=1)
_HUNDRED = Decimal(100)
#: Largest start balance or stake: its cents stay far inside SQLite's 64-bit INTEGER.
MAX_AMOUNT = Decimal("1000000000")


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


_OUTCOMES = ", ".join(f"'{outcome.value}'" for outcome in paper.Outcome)
_DIRECTIONS = ", ".join(f"'{direction.value}'" for direction in paper.Direction)
_EXIT_REASONS = ", ".join(f"'{reason.value}'" for reason in paper.ExitReason)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS {WALLET_TABLE} (
    wallet_id INTEGER PRIMARY KEY CHECK (wallet_id = 1),
    start_balance_cents INTEGER NOT NULL
        CHECK (typeof(start_balance_cents) = 'integer' AND start_balance_cents > 0),
    currency TEXT NOT NULL CHECK (length(currency) > 0),
    recorded_at TEXT NOT NULL CHECK (length(recorded_at) > 0)
)""",
    *_append_only(WALLET_TABLE),
    f"""CREATE TABLE IF NOT EXISTS {PLAY_TABLE} (
    play_id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE CHECK (length(event_id) > 0),
    run_id TEXT NOT NULL CHECK (length(run_id) > 0),
    asset TEXT NOT NULL CHECK (length(asset) > 0),
    pair TEXT NOT NULL CHECK (length(pair) > 0),
    quote TEXT NOT NULL CHECK (length(quote) > 0),
    direction TEXT NOT NULL CHECK (direction IN ({_DIRECTIONS})),
    stake_cents INTEGER NOT NULL CHECK (typeof(stake_cents) = 'integer' AND stake_cents > 0),
    fee_bps TEXT NOT NULL CHECK (length(fee_bps) > 0),
    hold_minutes INTEGER NOT NULL CHECK (typeof(hold_minutes) = 'integer' AND hold_minutes > 0),
    entry_bid TEXT NOT NULL CHECK (length(entry_bid) > 0),
    entry_ask TEXT NOT NULL CHECK (length(entry_ask) > 0),
    entry_ts TEXT NOT NULL CHECK (length(entry_ts) > 0),
    due_at TEXT NOT NULL CHECK (due_at > entry_ts),
    why_json TEXT NOT NULL CHECK (json_valid(why_json) AND json_type(why_json) = 'object'),
    opened_at TEXT NOT NULL CHECK (length(opened_at) > 0)
)""",
    *_append_only(PLAY_TABLE),
    f"""CREATE TABLE IF NOT EXISTS {CLOSE_TABLE} (
    close_id INTEGER PRIMARY KEY,
    play_id INTEGER NOT NULL UNIQUE REFERENCES {PLAY_TABLE} (play_id),
    exit_bid TEXT NOT NULL CHECK (length(exit_bid) > 0),
    exit_ask TEXT NOT NULL CHECK (length(exit_ask) > 0),
    exit_ts TEXT NOT NULL CHECK (exit_ts >= due_at),
    exit_snapshot_id INTEGER NOT NULL,
    due_at TEXT NOT NULL CHECK (length(due_at) > 0),
    delay_seconds REAL NOT NULL CHECK (delay_seconds >= 0),
    gross_mid_cents INTEGER NOT NULL CHECK (typeof(gross_mid_cents) = 'integer'),
    spread_cost_cents INTEGER NOT NULL CHECK (typeof(spread_cost_cents) = 'integer' AND spread_cost_cents >= 0),
    fees_cents INTEGER NOT NULL CHECK (typeof(fees_cents) = 'integer' AND fees_cents >= 0),
    net_cents INTEGER NOT NULL CHECK (typeof(net_cents) = 'integer'),
    outcome TEXT NOT NULL CHECK (outcome IN ({_OUTCOMES})),
    closed_at TEXT NOT NULL CHECK (length(closed_at) > 0),
    CHECK (net_cents = gross_mid_cents - spread_cost_cents - fees_cents),
    CHECK ((outcome = 'WIN' AND net_cents > 0) OR (outcome = 'LOSS' AND net_cents < 0)
        OR (outcome = 'FLAT' AND net_cents = 0))
)""",
    *_append_only(CLOSE_TABLE),
    f"""CREATE TRIGGER IF NOT EXISTS {CLOSE_TABLE}_require_play BEFORE INSERT ON {CLOSE_TABLE}
BEGIN
    SELECT RAISE(ABORT, '{CLOSE_TABLE} row has no play')
    WHERE NOT EXISTS (SELECT 1 FROM {PLAY_TABLE} WHERE play_id = NEW.play_id);
END""",
    f"""CREATE TABLE IF NOT EXISTS {EXIT_QUOTE_TABLE} (
    quote_id INTEGER PRIMARY KEY,
    play_id INTEGER NOT NULL REFERENCES {PLAY_TABLE} (play_id),
    pair TEXT NOT NULL CHECK (length(pair) > 0),
    bid TEXT NOT NULL CHECK (length(bid) > 0),
    ask TEXT NOT NULL CHECK (length(ask) > 0),
    observed_at TEXT NOT NULL CHECK (length(observed_at) > 0),
    source TEXT NOT NULL CHECK (length(source) > 0 AND source <> '{SPOT_SNAPSHOT_SOURCE}'),
    recorded_at TEXT NOT NULL CHECK (recorded_at >= observed_at)
)""",
    *_append_only(EXIT_QUOTE_TABLE),
)
SCHEMA_OBJECTS: tuple[tuple[str, str], ...] = (
    *(
        (kind, name)
        for table in TABLES
        for kind, name in (("table", table), ("trigger", f"{table}_no_update"), ("trigger", f"{table}_no_delete"))
    ),
    ("trigger", f"{CLOSE_TABLE}_require_play"),
)
#: Nullable columns added to the first tables for the EX-1 exit policy, as
#: ``(table, column, declaration)``; NULL on every row written before (legacy rows).
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (PLAY_TABLE, "exit_policy", "TEXT CHECK (exit_policy IS NULL OR length(exit_policy) > 0)"),
    (PLAY_TABLE, "atr", "TEXT CHECK (atr IS NULL OR length(atr) > 0)"),
    (PLAY_TABLE, "stop_price", "TEXT CHECK (stop_price IS NULL OR length(stop_price) > 0)"),
    (PLAY_TABLE, "target_price", "TEXT CHECK (target_price IS NULL OR length(target_price) > 0)"),
    (CLOSE_TABLE, "exit_reason", f"TEXT CHECK (exit_reason IS NULL OR exit_reason IN ({_EXIT_REASONS}))"),
    (CLOSE_TABLE, "exit_source", "TEXT CHECK (exit_source IS NULL OR length(exit_source) > 0)"),
    (CLOSE_TABLE, "record_lag_seconds", "REAL CHECK (record_lag_seconds IS NULL OR record_lag_seconds >= 0)"),
)
MIGRATION_STATEMENTS: tuple[str, ...] = tuple(
    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}" for table, column, declaration in ADDED_COLUMNS
)

_PLAY_COLUMNS = (
    "event_id",
    "run_id",
    "asset",
    "pair",
    "quote",
    "direction",
    "stake_cents",
    "fee_bps",
    "hold_minutes",
    "entry_bid",
    "entry_ask",
    "entry_ts",
    "due_at",
    "why_json",
    "opened_at",
)
_PLAY_LEVEL_COLUMNS = ("exit_policy", "atr", "stop_price", "target_price")
_CLOSE_COLUMNS = (
    "play_id",
    "exit_bid",
    "exit_ask",
    "exit_ts",
    "exit_snapshot_id",
    "due_at",
    "delay_seconds",
    "gross_mid_cents",
    "spread_cost_cents",
    "fees_cents",
    "net_cents",
    "outcome",
    "closed_at",
)
_CLOSE_EXIT_COLUMNS = ("exit_reason", "exit_source", "record_lag_seconds")


class PaperStoreFailure(Enum):
    INVALID_ROW = "invalid_row"
    INVALID_ARGUMENT = "invalid_argument"
    OPEN_TRANSACTION = "open_transaction"
    NO_WALLET = "no_wallet"
    MALFORMED_ROW = "malformed_row"
    SQLITE_ERROR = "sqlite_error"


class PaperStoreError(RuntimeError):
    """Nothing was written (or it was rolled back); ``code`` says why."""

    def __init__(self, code: PaperStoreFailure, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PaperCandidate:
    """One new radar event that may open a play, with the touch it would enter on.

    ``bid``, ``ask`` and ``status`` are the values recorded for ``pair`` at
    ``snapshot_ts`` (they are validated by ``domain.paper.validate_quote``, so a missing
    or invalid price is a skip, never a zero). ``why`` holds the recorded facts behind the
    event (setup, direction, scores, feature values) and must be a JSON object.
    ``atr`` is the 5-minute ATR14 of closed bars computed on the spot pair ``atr_pair``;
    it sets the exit levels and must belong to ``pair`` itself (``NO_VALID_ATR`` otherwise).
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
    why: Mapping[str, object]
    atr: object = None
    atr_pair: object = None


@dataclass(frozen=True, slots=True)
class PlayTerms:
    """The configurable rules a new play is opened under; stake and fee are frozen on the play.

    The hold and exit levels are not terms: they come from the EX-1 policy fixed in
    ``domain.paper`` and are frozen on the play too.
    """

    stake: Decimal
    fee_bps: Decimal
    max_open: int


@dataclass(frozen=True, slots=True)
class Wallet:
    start_balance: Decimal
    currency: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class StoredPlay:
    play_id: int
    event_id: str
    run_id: str
    asset: str
    pair: str
    quote: str
    direction: paper.Direction
    stake: Decimal
    fee_bps: Decimal
    hold_minutes: int
    entry_bid: Decimal
    entry_ask: Decimal
    entry_ts: datetime
    due_at: datetime
    why: Mapping[str, object]
    opened_at: str
    #: EX-1 exit plan; all ``None`` on a legacy play (and on a database not migrated yet).
    exit_policy: str | None = None
    atr: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None

    @property
    def has_levels(self) -> bool:
        return self.stop is not None and self.target is not None


@dataclass(frozen=True, slots=True)
class StoredClose:
    close_id: int
    play_id: int
    exit_bid: Decimal
    exit_ask: Decimal
    exit_ts: datetime
    exit_snapshot_id: int
    due_at: datetime
    delay_seconds: float
    gross_mid: Decimal
    spread_cost: Decimal
    fees: Decimal
    net: Decimal
    outcome: paper.Outcome
    closed_at: str
    #: ``None`` on a legacy close (and on a database not migrated yet).
    exit_reason: paper.ExitReason | None = None
    #: ``spot_snapshot`` (``exit_snapshot_id`` is a ``spot_snapshots`` id) or the source of
    #: a ``paper_exit_quotes`` row (``exit_snapshot_id`` is its ``quote_id``).
    exit_source: str | None = None
    #: Seconds from the closing observation to the write of this close.
    record_lag_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ObservedQuote:
    """A quote observed outside ``spot_snapshots`` (e.g. a public Ticker read), offered to
    ``close_plays`` next to the recorded snapshots. ``observed_at`` is when it was observed
    (receipt time when the source has no timestamp); ``source`` names where it came from."""

    pair: str
    bid: object
    ask: object
    observed_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class Skip:
    event_id: str
    reason: paper.SkipReason
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class OpenReport:
    opened: tuple[StoredPlay, ...]
    skipped: tuple[Skip, ...]


@dataclass(frozen=True, slots=True)
class SettleReport:
    closed: tuple[StoredClose, ...]
    #: Plays past their due time with no valid exit quote yet; they stay open.
    pending: tuple[int, ...]


# --- time and number helpers --------------------------------------------------------------------


def utc_text(moment: datetime) -> str:
    """Fixed-width UTC text (``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``): text order is time order."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _require_now(now: object) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, "now must be a timezone-aware datetime")
    return now


def _parse_aware(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def _to_cents(value: Decimal) -> int:
    return int(value * _HUNDRED)


def _from_cents(value: int) -> Decimal:
    return Decimal(value) / _HUNDRED


def _whole_cents(field: str, value: object) -> Decimal:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not 0 < value <= MAX_AMOUNT
        or value != paper.cents(value)
    ):
        raise PaperStoreError(
            PaperStoreFailure.INVALID_ARGUMENT, f"{field} must be a positive Decimal in whole cents, at most {MAX_AMOUNT}"
        )
    return value


# --- schema -------------------------------------------------------------------------------------


_BASE_OBJECTS = tuple(obj for obj in SCHEMA_OBJECTS if not obj[1].startswith(EXIT_QUOTE_TABLE))


def _missing_objects(
    conn: sqlite3.Connection, wanted: Sequence[tuple[str, str]] = SCHEMA_OBJECTS
) -> list[tuple[str, str]]:
    marks = ", ".join("?" for _ in TABLES)
    present = {
        (str(kind), str(name))
        for kind, name in conn.execute(
            f"SELECT type, name FROM sqlite_master WHERE tbl_name IN ({marks})", TABLES
        ).fetchall()
    }
    return [obj for obj in wanted if obj not in present]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _missing_columns(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    present = {table: _columns(conn, table) for table in (PLAY_TABLE, CLOSE_TABLE)}
    return [added for added in ADDED_COLUMNS if added[1] not in present[added[0]]]


def schema_present(conn: sqlite3.Connection) -> bool:
    """Whether the wallet, play and close tables and their triggers exist, so the game can be
    read (read-only). A database not migrated to the EX-1 columns yet is still readable."""
    try:
        return not _missing_objects(conn, _BASE_OBJECTS)
    except sqlite3.Error as error:
        raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, f"reading sqlite_master: {error}") from error


def schema_current(conn: sqlite3.Connection) -> bool:
    """Whether every paper table, trigger and added EX-1 column exists (read-only)."""
    try:
        return not _missing_objects(conn) and not _missing_columns(conn)
    except sqlite3.Error as error:
        raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, f"reading the paper schema: {error}") from error


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create the missing paper tables and triggers and add the missing EX-1 columns;
    ``True`` when something was created or added.

    A read-only check comes first: an up-to-date database is not written to at all.
    Otherwise one ``BEGIN IMMEDIATE`` transaction re-checks under the write lock, runs
    ``CREATE ... IF NOT EXISTS`` and adds each missing nullable column. It never rewrites
    or deletes a row and rolls back entirely on any error.
    """
    if conn.in_transaction:
        raise PaperStoreError(PaperStoreFailure.OPEN_TRANSACTION, "commit or roll back before ensure_schema")
    try:
        if not _missing_objects(conn) and not _missing_columns(conn):
            return False
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            for table, column, declaration in _missing_columns(conn):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, f"creating paper tables: {error}") from error
    return True


# --- reading ------------------------------------------------------------------------------------


def _malformed(field: str, value: object) -> PaperStoreError:
    return PaperStoreError(PaperStoreFailure.MALFORMED_ROW, f"stored {field} is {value!r}")


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


def _stored_time(field: str, value: object) -> datetime:
    moment = _parse_aware(_stored_str(field, value))
    if moment is None:
        raise _malformed(field, value)
    return moment


def _stored_float(field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _malformed(field, value)
    return float(value)


def _stored_why(value: object) -> Mapping[str, object]:
    try:
        loaded = json.loads(_stored_str("why_json", value))
    except ValueError:
        raise _malformed("why_json", value) from None
    if not isinstance(loaded, dict):
        raise _malformed("why_json", value)
    return loaded


def _select(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> list[tuple[object, ...]]:
    try:
        return [tuple(record) for record in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error as error:
        raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, f"reading paper tables: {error}") from error


def read_wallet(conn: sqlite3.Connection) -> Wallet | None:
    """The recorded wallet, or ``None`` before it was recorded."""
    rows = _select(conn, f"SELECT start_balance_cents, currency, recorded_at FROM {WALLET_TABLE} WHERE wallet_id = 1")
    if not rows:
        return None
    cents, currency, recorded_at = rows[0]
    return Wallet(
        start_balance=_from_cents(_stored_int("start_balance_cents", cents)),
        currency=_stored_str("currency", currency),
        recorded_at=_stored_str("recorded_at", recorded_at),
    )


def _optional_decimal(field: str, value: object) -> Decimal | None:
    return None if value is None else _stored_decimal(field, value)


def _optional_str(field: str, value: object) -> str | None:
    return None if value is None else _stored_str(field, value)


def _optional_float(field: str, value: object) -> float | None:
    return None if value is None else _stored_float(field, value)


def _select_list(
    conn: sqlite3.Connection, table: str, alias: str, columns: Sequence[str], optional: Sequence[str]
) -> str:
    """The column list of a read; an EX-1 column not added yet reads as NULL."""
    try:
        present = _columns(conn, table)
    except sqlite3.Error as error:
        raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, f"reading paper tables: {error}") from error
    return ", ".join(
        [f"{alias}.{column}" for column in columns]
        + [f"{alias}.{column}" if column in present else f"NULL AS {column}" for column in optional]
    )


def _play(record: tuple[object, ...]) -> StoredPlay:
    value = dict(zip(("play_id", *_PLAY_COLUMNS, *_PLAY_LEVEL_COLUMNS), record, strict=True))
    direction = paper.parse_direction(value["direction"])
    if direction is None:
        raise _malformed("direction", value["direction"])
    return StoredPlay(
        play_id=_stored_int("play_id", value["play_id"]),
        event_id=_stored_str("event_id", value["event_id"]),
        run_id=_stored_str("run_id", value["run_id"]),
        asset=_stored_str("asset", value["asset"]),
        pair=_stored_str("pair", value["pair"]),
        quote=_stored_str("quote", value["quote"]),
        direction=direction,
        stake=_from_cents(_stored_int("stake_cents", value["stake_cents"])),
        fee_bps=_stored_decimal("fee_bps", value["fee_bps"]),
        hold_minutes=_stored_int("hold_minutes", value["hold_minutes"]),
        entry_bid=_stored_decimal("entry_bid", value["entry_bid"]),
        entry_ask=_stored_decimal("entry_ask", value["entry_ask"]),
        entry_ts=_stored_time("entry_ts", value["entry_ts"]),
        due_at=_stored_time("due_at", value["due_at"]),
        why=_stored_why(value["why_json"]),
        opened_at=_stored_str("opened_at", value["opened_at"]),
        exit_policy=_optional_str("exit_policy", value["exit_policy"]),
        atr=_optional_decimal("atr", value["atr"]),
        stop=_optional_decimal("stop_price", value["stop_price"]),
        target=_optional_decimal("target_price", value["target_price"]),
    )


def _close(record: tuple[object, ...]) -> StoredClose:
    value = dict(zip(("close_id", *_CLOSE_COLUMNS, *_CLOSE_EXIT_COLUMNS), record, strict=True))
    try:
        outcome = paper.Outcome(value["outcome"])
    except ValueError:
        raise _malformed("outcome", value["outcome"]) from None
    exit_reason: paper.ExitReason | None = None
    if value["exit_reason"] is not None:
        try:
            exit_reason = paper.ExitReason(value["exit_reason"])
        except ValueError:
            raise _malformed("exit_reason", value["exit_reason"]) from None
    return StoredClose(
        close_id=_stored_int("close_id", value["close_id"]),
        play_id=_stored_int("play_id", value["play_id"]),
        exit_bid=_stored_decimal("exit_bid", value["exit_bid"]),
        exit_ask=_stored_decimal("exit_ask", value["exit_ask"]),
        exit_ts=_stored_time("exit_ts", value["exit_ts"]),
        exit_snapshot_id=_stored_int("exit_snapshot_id", value["exit_snapshot_id"]),
        due_at=_stored_time("due_at", value["due_at"]),
        delay_seconds=_stored_float("delay_seconds", value["delay_seconds"]),
        gross_mid=_from_cents(_stored_int("gross_mid_cents", value["gross_mid_cents"])),
        spread_cost=_from_cents(_stored_int("spread_cost_cents", value["spread_cost_cents"])),
        fees=_from_cents(_stored_int("fees_cents", value["fees_cents"])),
        net=_from_cents(_stored_int("net_cents", value["net_cents"])),
        outcome=outcome,
        closed_at=_stored_str("closed_at", value["closed_at"]),
        exit_reason=exit_reason,
        exit_source=_optional_str("exit_source", value["exit_source"]),
        record_lag_seconds=_optional_float("record_lag_seconds", value["record_lag_seconds"]),
    )


def _play_list(conn: sqlite3.Connection) -> str:
    return _select_list(conn, PLAY_TABLE, "p", ("play_id", *_PLAY_COLUMNS), _PLAY_LEVEL_COLUMNS)


def read_plays(conn: sqlite3.Connection) -> tuple[StoredPlay, ...]:
    """Every recorded play, oldest first."""
    rows = _select(conn, f"SELECT {_play_list(conn)} FROM {PLAY_TABLE} p ORDER BY p.play_id")
    return tuple(_play(row) for row in rows)


def read_closes(conn: sqlite3.Connection) -> tuple[StoredClose, ...]:
    """Every recorded close, in the order they were written."""
    columns = _select_list(conn, CLOSE_TABLE, "c", ("close_id", *_CLOSE_COLUMNS), _CLOSE_EXIT_COLUMNS)
    rows = _select(conn, f"SELECT {columns} FROM {CLOSE_TABLE} c ORDER BY c.close_id")
    return tuple(_close(row) for row in rows)


def read_open_plays(conn: sqlite3.Connection) -> tuple[StoredPlay, ...]:
    """Plays without a close row, oldest first."""
    rows = _select(
        conn,
        f"SELECT {_play_list(conn)} FROM {PLAY_TABLE} p "
        f"WHERE NOT EXISTS (SELECT 1 FROM {CLOSE_TABLE} c WHERE c.play_id = p.play_id) ORDER BY p.play_id",
    )
    return tuple(_play(row) for row in rows)


def current_balance(conn: sqlite3.Connection) -> Decimal | None:
    """Recorded start plus the sum of recorded nets; ``None`` before the wallet exists."""
    wallet = read_wallet(conn)
    if wallet is None:
        return None
    return paper.balance(wallet.start_balance, (close.net for close in read_closes(conn)))


# --- writing ------------------------------------------------------------------------------------


def _begin(conn: sqlite3.Connection, what: str) -> None:
    if conn.in_transaction:
        raise PaperStoreError(PaperStoreFailure.OPEN_TRANSACTION, f"commit or roll back before {what}")
    conn.execute("BEGIN IMMEDIATE")


def ensure_wallet(conn: sqlite3.Connection, *, start_balance: Decimal, currency: str, now: datetime) -> Wallet:
    """Record the wallet once and return it; an existing wallet is returned unchanged.

    ``start_balance`` and ``currency`` only matter the first time: history is never rewritten.
    """
    _whole_cents("start_balance", start_balance)
    if not isinstance(currency, str) or not currency.strip():
        raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, "currency must be a non-empty string")
    recorded_at = utc_text(_require_now(now))
    existing = read_wallet(conn)
    if existing is not None:
        return existing
    try:
        _begin(conn, "ensure_wallet")
        try:
            conn.execute(
                f"INSERT INTO {WALLET_TABLE} (wallet_id, start_balance_cents, currency, recorded_at) "
                "VALUES (1, ?, ?, ?) ON CONFLICT (wallet_id) DO NOTHING",
                (_to_cents(start_balance), currency, recorded_at),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, f"writing {WALLET_TABLE}: {error}") from error
    wallet = read_wallet(conn)
    if wallet is None:  # pragma: no cover - the insert above committed
        raise PaperStoreError(PaperStoreFailure.NO_WALLET, "wallet missing after insert")
    return wallet


def _invalid(index: int, field: str, detail: str) -> PaperStoreError:
    return PaperStoreError(PaperStoreFailure.INVALID_ROW, f"candidate {index} {field}: {detail}")


def _candidate_text(index: int, field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(index, field, f"must be a non-empty string, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class _Checked:
    candidate: PaperCandidate
    entry_ts: datetime
    why_json: str


def _check_candidate(index: int, candidate: object) -> _Checked:
    if not isinstance(candidate, PaperCandidate):
        raise _invalid(index, "candidate", f"must be a PaperCandidate, got {type(candidate).__name__}")
    for field in ("event_id", "run_id", "asset", "pair", "quote", "snapshot_ts"):
        _candidate_text(index, field, getattr(candidate, field))
    entry_ts = _parse_aware(candidate.snapshot_ts)
    if entry_ts is None:
        raise _invalid(index, "snapshot_ts", f"must be ISO-8601 text with a UTC offset, got {candidate.snapshot_ts!r}")
    if not isinstance(candidate.why, Mapping):
        raise _invalid(index, "why", f"must be a mapping, got {type(candidate.why).__name__}")
    try:
        why_json = json.dumps(dict(candidate.why), allow_nan=False, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise _invalid(index, "why", f"must be JSON without NaN/Infinity: {error}") from error
    return _Checked(candidate, entry_ts, why_json)


def _check_terms(terms: object) -> PlayTerms:
    if not isinstance(terms, PlayTerms):
        raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, "terms must be PlayTerms")
    _whole_cents("stake", terms.stake)
    if not isinstance(terms.fee_bps, Decimal) or not terms.fee_bps.is_finite() or terms.fee_bps < 0:
        raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, "fee_bps must be a finite Decimal >= 0")
    if isinstance(terms.max_open, bool) or not isinstance(terms.max_open, int) or terms.max_open <= 0:
        raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, "max_open must be a positive int")
    return terms


def _exit_plan(candidate: PaperCandidate, direction: paper.Direction, quote: paper.Quote) -> paper.ExitLevels | Skip:
    """The EX-1 levels of a candidate, or why it cannot open: an ATR that is missing, not a
    finite positive number or not computed on the pair the play enters on, or a level <= 0."""
    atr = paper.validate_atr(candidate.atr)
    if isinstance(atr, paper.QuoteProblem):
        return Skip(candidate.event_id, paper.SkipReason.NO_VALID_ATR, atr.value)
    if candidate.atr_pair != candidate.pair:
        return Skip(
            candidate.event_id,
            paper.SkipReason.NO_VALID_ATR,
            f"pair_mismatch: atr of {candidate.atr_pair!r}, entry on {candidate.pair!r}",
        )
    levels = paper.exit_levels(direction, quote, atr)
    if levels is None:
        return Skip(candidate.event_id, paper.SkipReason.INVALID_LEVELS, f"atr {atr} leaves a level <= 0")
    return levels


def open_candidates(
    conn: sqlite3.Connection, candidates: Sequence[PaperCandidate], *, terms: PlayTerms, now: datetime
) -> OpenReport:
    """Open one play per admitted candidate, in order, in one transaction.

    Every candidate is validated before anything is written (``INVALID_ROW``). Under the
    write lock each one is then checked, in order: event already has a play
    (``ALREADY_RECORDED``), LONG/SHORT direction, valid entry quote, a valid ATR of the
    same pair (``NO_VALID_ATR``), positive EX-1 levels (``INVALID_LEVELS``), then the
    admission rules of ``domain.paper.admit`` against the plays already open (including
    the ones opened earlier in this call). Each play freezes its EX-1 policy id, ATR, stop,
    target and 24 h hold. The wallet must already be recorded (``ensure_wallet``) and the
    schema current (``ensure_schema``).
    """
    opened_at = utc_text(_require_now(now))
    _check_terms(terms)
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, "candidates must be a sequence of PaperCandidate")
    checked = [_check_candidate(index, candidate) for index, candidate in enumerate(candidates)]
    if not checked:
        return OpenReport((), ())
    opened_ids: list[int] = []
    skipped: list[Skip] = []
    columns = (*_PLAY_COLUMNS, *_PLAY_LEVEL_COLUMNS)
    try:
        _begin(conn, "open_candidates")
        try:
            wallet = read_wallet(conn)
            if wallet is None:
                raise PaperStoreError(PaperStoreFailure.NO_WALLET, "record the wallet before opening plays")
            balance = paper.balance(wallet.start_balance, (close.net for close in read_closes(conn)))
            positions = [paper.OpenPosition(play.asset, play.stake) for play in read_open_plays(conn)]
            recorded = {str(row[0]) for row in _select(conn, f"SELECT event_id FROM {PLAY_TABLE}")}
            for item in checked:
                candidate = item.candidate
                if candidate.event_id in recorded:
                    skipped.append(Skip(candidate.event_id, paper.SkipReason.ALREADY_RECORDED))
                    continue
                direction = paper.parse_direction(candidate.direction)
                if direction is None:
                    skipped.append(Skip(candidate.event_id, paper.SkipReason.NO_DIRECTION))
                    continue
                quote = paper.validate_quote(candidate.bid, candidate.ask, candidate.status)
                if isinstance(quote, paper.QuoteProblem):
                    skipped.append(Skip(candidate.event_id, paper.SkipReason.INVALID_PRICE, quote.value))
                    continue
                plan = _exit_plan(candidate, direction, quote)
                if isinstance(plan, Skip):
                    skipped.append(plan)
                    continue
                refusal = paper.admit(
                    direction.value, candidate.asset, positions, balance, terms.stake, terms.max_open
                )
                if refusal is not None:
                    skipped.append(Skip(candidate.event_id, refusal))
                    continue
                due = paper.due_at(item.entry_ts, plan.hold_minutes)
                cursor = conn.execute(
                    f"INSERT INTO {PLAY_TABLE} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    (
                        candidate.event_id,
                        candidate.run_id,
                        candidate.asset,
                        candidate.pair,
                        candidate.quote,
                        direction.value,
                        _to_cents(terms.stake),
                        str(terms.fee_bps),
                        plan.hold_minutes,
                        str(quote.bid),
                        str(quote.ask),
                        utc_text(item.entry_ts),
                        utc_text(due),
                        item.why_json,
                        opened_at,
                        plan.policy_id,
                        str(plan.atr),
                        str(plan.stop),
                        str(plan.target),
                    ),
                )
                if cursor.lastrowid is None:  # pragma: no cover - an INSERT always sets it
                    raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, "insert returned no row id")
                opened_ids.append(cursor.lastrowid)
                recorded.add(candidate.event_id)
                positions.append(paper.OpenPosition(candidate.asset, terms.stake))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, f"writing {PLAY_TABLE}: {error}") from error
    wanted = set(opened_ids)
    opened = tuple(play for play in read_plays(conn) if play.play_id in wanted)
    return OpenReport(opened, tuple(skipped))


# --- closing ------------------------------------------------------------------------------------


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


@dataclass(frozen=True, slots=True)
class _Exit:
    play: StoredPlay
    observation: _Observation
    #: ``None`` for a legacy play closed by the old rule.
    reason: paper.ExitReason | None


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


def _check_extra_quotes(extra_quotes: object) -> tuple[ObservedQuote, ...]:
    if isinstance(extra_quotes, (str, bytes)) or not isinstance(extra_quotes, Sequence):
        raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, "extra_quotes must be a sequence of ObservedQuote")
    for index, extra in enumerate(extra_quotes):
        where = f"extra_quotes[{index}]"
        if not isinstance(extra, ObservedQuote):
            raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, f"{where} must be an ObservedQuote")
        if not isinstance(extra.pair, str) or not extra.pair.strip():
            raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, f"{where}.pair must be a non-empty string")
        if not isinstance(extra.source, str) or not extra.source.strip() or extra.source == SPOT_SNAPSHOT_SOURCE:
            raise PaperStoreError(
                PaperStoreFailure.INVALID_ARGUMENT,
                f"{where}.source must be a non-empty string other than {SPOT_SNAPSHOT_SOURCE!r}",
            )
        moment = extra.observed_at
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, f"{where}.observed_at must be timezone-aware")
    return tuple(extra_quotes)


def _extra_observations(extras: Sequence[ObservedQuote], pair: str, entry_ts: datetime, now: datetime) -> list[_Observation]:
    """The extra quotes of exactly ``pair`` observed strictly after entry and up to ``now``
    whose bid/ask are a valid quote; any other is ignored, never read as zero."""
    found: list[_Observation] = []
    for index, extra in enumerate(extras):
        if extra.pair != pair or not entry_ts < extra.observed_at <= now:
            continue
        quote = paper.validate_quote(extra.bid, extra.ask)
        if isinstance(quote, paper.QuoteProblem):
            continue
        found.append(_Observation(extra.observed_at.astimezone(UTC), 1, index, quote, extra))
    return found


def _find_exit(
    conn: sqlite3.Connection, play: StoredPlay, extras: Sequence[ObservedQuote], now: datetime
) -> _Exit | None:
    """The observation that closes ``play`` by ``now``, or ``None`` while it stays open.

    A play with EX-1 levels closes on the first valid quote of its pair observed strictly
    after entry that touches its stop or target, or else is at or after its due time
    (``domain.paper.exit_decision``). A legacy play closes by the old rule: the first valid
    spot snapshot at or after its due time; extra quotes are not used for it.
    """
    if play.stop is None or play.target is None:
        if play.due_at > now:
            return None
        spot = _spot_observations(conn, play.pair, play.due_at, now, include_start=True)
        return _Exit(play, spot[0], None) if spot else None
    observations = _spot_observations(conn, play.pair, play.entry_ts, now, include_start=False)
    observations.extend(_extra_observations(extras, play.pair, play.entry_ts, now))
    observations.sort(key=lambda observation: observation.order)
    for observation in observations:
        reason = paper.exit_decision(
            play.direction, play.stop, play.target, play.due_at, observation.observed_at, observation.quote
        )
        if reason is not None:
            return _Exit(play, observation, reason)
    return None


def _write_close(conn: sqlite3.Connection, found: _Exit, now: datetime, closed_at: str) -> int | None:
    """Insert the close of ``found`` under the caller's write lock; ``None`` when the play
    already has a close (another writer got there first): nothing is written then."""
    play, observation = found.play, found.observation
    if conn.execute(f"SELECT 1 FROM {CLOSE_TABLE} WHERE play_id = ?", (play.play_id,)).fetchone() is not None:
        return None
    entry = paper.validate_quote(play.entry_bid, play.entry_ask)
    if isinstance(entry, paper.QuoteProblem):
        raise _malformed("entry quote", (play.entry_bid, play.entry_ask))
    result = paper.settle(play.direction, play.stake, play.fee_bps, entry, observation.quote)
    observed_text = utc_text(observation.observed_at)
    exit_id = observation.ident
    source: str | None = None
    lag: float | None = None
    due_text = utc_text(play.due_at)
    delay = (observation.observed_at - play.due_at).total_seconds()
    if found.reason is not None:
        source = SPOT_SNAPSHOT_SOURCE
        lag = (now - observation.observed_at).total_seconds()
        if observation.extra is not None:
            source = observation.extra.source
            cursor = conn.execute(
                f"INSERT INTO {EXIT_QUOTE_TABLE} (play_id, pair, bid, ask, observed_at, source, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    play.play_id,
                    play.pair,
                    str(observation.quote.bid),
                    str(observation.quote.ask),
                    observed_text,
                    source,
                    closed_at,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - an INSERT always sets it
                raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, "insert returned no row id")
            exit_id = cursor.lastrowid
        if found.reason is not paper.ExitReason.TIME:
            # A stop/target exit is due when it was touched: exit_ts >= due_at holds, no delay.
            due_text, delay = observed_text, 0.0
    columns = (*_CLOSE_COLUMNS, *_CLOSE_EXIT_COLUMNS)
    cursor = conn.execute(
        f"INSERT INTO {CLOSE_TABLE} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        (
            play.play_id,
            str(observation.quote.bid),
            str(observation.quote.ask),
            observed_text,
            exit_id,
            due_text,
            delay,
            _to_cents(result.gross_mid),
            _to_cents(result.spread_cost),
            _to_cents(result.fees),
            _to_cents(result.net),
            result.outcome.value,
            closed_at,
            None if found.reason is None else found.reason.value,
            source,
            lag,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - an INSERT always sets it
        raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, "insert returned no row id")
    return cursor.lastrowid


def close_plays(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    extra_quotes: Sequence[ObservedQuote] = (),
    play_ids: Collection[int] | None = None,
) -> SettleReport:
    """Close every open play (or only those in ``play_ids``) whose exit is observed by ``now``.

    ``extra_quotes`` are quotes observed outside ``spot_snapshots`` (e.g. one public Ticker
    read per pair): each is matched to the plays of exactly its pair and merged with the
    recorded snapshots, so the earliest touching observation still wins; the one that
    closes a play is stored in ``paper_exit_quotes`` with its source.

    Every quote is evaluated before the write lock. One ``BEGIN IMMEDIATE`` transaction then
    only re-checks that each play is still open and inserts its close: a play another writer
    closed meanwhile is left as it is, without error (``UNIQUE(play_id)`` is the backstop).
    A play past its due time with no exit observation is reported as pending.
    """
    _require_now(now)
    closed_at = utc_text(now)
    extras = _check_extra_quotes(extra_quotes)
    wanted_ids: set[int] | None = None
    if play_ids is not None:
        if isinstance(play_ids, (str, bytes)) or not all(
            isinstance(play_id, int) and not isinstance(play_id, bool) for play_id in play_ids
        ):
            raise PaperStoreError(PaperStoreFailure.INVALID_ARGUMENT, "play_ids must be a collection of int")
        wanted_ids = set(play_ids)
    if conn.in_transaction:
        raise PaperStoreError(PaperStoreFailure.OPEN_TRANSACTION, "commit or roll back before close_plays")
    exits: list[_Exit] = []
    pending: list[int] = []
    try:
        for play in read_open_plays(conn):
            if wanted_ids is not None and play.play_id not in wanted_ids:
                continue
            found = _find_exit(conn, play, extras, now)
            if found is not None:
                exits.append(found)
            elif play.due_at <= now:
                pending.append(play.play_id)
    except sqlite3.Error as error:
        raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, f"reading exit quotes: {error}") from error
    if not exits:
        return SettleReport((), tuple(pending))
    closed_ids: list[int] = []
    try:
        _begin(conn, "close_plays")
        try:
            for found in exits:
                close_id = _write_close(conn, found, now, closed_at)
                if close_id is not None:
                    closed_ids.append(close_id)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise PaperStoreError(PaperStoreFailure.SQLITE_ERROR, f"writing {CLOSE_TABLE}: {error}") from error
    wanted = set(closed_ids)
    closed = tuple(close for close in read_closes(conn) if close.close_id in wanted)
    return SettleReport(closed, tuple(pending))


def settle_due(conn: sqlite3.Connection, *, now: datetime) -> SettleReport:
    """Close every open play whose exit is observed in the recorded spot snapshots by ``now``.

    Same as ``close_plays`` without extra quotes: a play with EX-1 levels closes on the
    first snapshot that touches its stop or target or is at or after its due time; a legacy
    play on the first valid snapshot at or after its due time. A due play with no valid
    quote stays open and is reported as pending; it closes on a later call.
    """
    return close_plays(conn, now=now)
