"""SQLite lifecycle transitions, delivery outbox, consumer cursors and JSONL export (T033a).

Tables come from ledger migration version 4 (``evidence_store.OUTBOX_MIGRATION``):
``lifecycle_items``, ``outbox`` and ``outbox_cursors``. The only legacy table this module
touches is ``events``, and only to read the row a caller has just changed.

Three kinds of outbox row, never mixed:

* ``EVENT``: a snapshot of one legacy ``events`` row, written by the store in the same
  transaction as the change to that row (creation, status change, claim, recovery,
  notified flag). ``state`` is the row's status.
* ``LIFECYCLE``: a work item entering one real state: QUEUED, LOADING, RUNNING, FINISHED,
  FAILED, or the T032 outcomes ABORT_STALE, SUPERSEDED and DROPPED_BACKPRESSURE. The
  item's current state (``lifecycle_items``) and its outbox row are written in one
  transaction. The allowed moves form an acyclic graph, so an item enters each state at
  most once and ``lifecycle:<item>:<state>`` is a stable delivery ID.
* ``HANDOFF``: a real message between two agents. It needs a persisted sender and a
  persisted receiver, and they must differ; the domain check refuses anything else before
  SQL, and a table ``CHECK`` refuses it again if a writer skips the domain check. Nothing
  in the current pipeline records a handoff, so it emits no agent-to-agent traffic.

Delivery semantics (at-least-once, never exactly-once):

* Every row has a stable ``delivery_id`` that is unique on the outbox. Recording the same
  lifecycle transition or the same handoff again returns the stored row and writes
  nothing (``RecordResult.created`` is ``False``).
* ``seq`` is an ``AUTOINCREMENT`` key assigned under the SQLite write lock, so rows become
  visible in ``seq`` order and a consumer that reads ``seq > cursor`` never skips a row.
* A consumer reads rows after its cursor, handles them, then acknowledges. Cursors only
  move forward and cannot pass the last row. A crash between handling and acknowledging
  delivers the same rows again, with the same ``delivery_id``: **delivery is at least
  once**. A consumer that must not act twice keeps the delivery IDs it has handled.
* ``export_jsonl`` is such a consumer, one cursor per target file. It appends the pending
  rows, fsyncs the file, then advances the cursor, all inside one write transaction so two
  exporters never interleave. After a crash between the append and the cursor commit, the
  next export finds the delivery IDs already in the file (read from the byte offset of
  the last committed export) and does not write them again. A torn last line is left
  where it is and the next line starts on a fresh line. Re-exporting to a new path writes
  the whole outbox history again. If the file was edited by hand after the last export,
  duplicates remain possible; the ``delivery_id`` in each line is what a reader dedups on.

Transactions follow ``invocation_store``: one short ``BEGIN IMMEDIATE`` per call, no
callback inside it, bounded busy retries (50, 100, 200 ms) then a typed ``BUSY``, and a
caller that already holds a transaction is refused. The one exception is
``record_event_row``: it must run *inside* the caller's transaction, because its purpose
is to commit with the caller's change to ``events`` or not at all.

Error details are codes plus short technical text for logs. Nothing here builds a
notification, and no free text reaches a lifecycle or handoff row: reasons are codes.

Rollback of the schema is documented in ``docs/tasks/results/T033a.md``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType

from ..domain.invocation import BUSY_RETRY_DELAYS
from . import evidence_store

LIFECYCLE_TABLE = "lifecycle_items"
OUTBOX_TABLE = "outbox"
CURSOR_TABLE = "outbox_cursors"
EXPORT_SCHEMA = 1
DEFAULT_READ_LIMIT = 256
MAX_READ_LIMIT = 10_000

_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6

type Sleep = Callable[[float], None]


class OutboxKind(Enum):
    EVENT = "EVENT"
    LIFECYCLE = "LIFECYCLE"
    HANDOFF = "HANDOFF"


class LifecycleState(Enum):
    QUEUED = "QUEUED"
    LOADING = "LOADING"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    ABORT_STALE = "ABORT_STALE"
    SUPERSEDED = "SUPERSEDED"
    DROPPED_BACKPRESSURE = "DROPPED_BACKPRESSURE"


TERMINAL_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.FINISHED,
        LifecycleState.FAILED,
        LifecycleState.ABORT_STALE,
        LifecycleState.SUPERSEDED,
        LifecycleState.DROPPED_BACKPRESSURE,
    }
)
# An admission can be refused outright (T032: superseded, dropped or stale on arrival).
INITIAL_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.QUEUED,
        LifecycleState.SUPERSEDED,
        LifecycleState.DROPPED_BACKPRESSURE,
        LifecycleState.ABORT_STALE,
    }
)
ALLOWED_TRANSITIONS: Mapping[LifecycleState, frozenset[LifecycleState]] = MappingProxyType(
    {
        LifecycleState.QUEUED: frozenset(
            {
                LifecycleState.LOADING,
                LifecycleState.RUNNING,
                LifecycleState.SUPERSEDED,
                LifecycleState.DROPPED_BACKPRESSURE,
                LifecycleState.ABORT_STALE,
            }
        ),
        LifecycleState.LOADING: frozenset(
            {LifecycleState.RUNNING, LifecycleState.FAILED, LifecycleState.ABORT_STALE}
        ),
        LifecycleState.RUNNING: frozenset(
            {LifecycleState.FINISHED, LifecycleState.FAILED, LifecycleState.ABORT_STALE}
        ),
        **{state: frozenset() for state in TERMINAL_STATES},
    }
)


class HandoffType(Enum):
    """Only a real controller dispatch or a real acceptance is a handoff (ARCHITECTURE.md)."""

    DISPATCH = "DISPATCH"
    ACCEPT = "ACCEPT"


class OutboxFailure(Enum):
    INVALID_FIELD = "invalid_field"
    INVALID_CLOCK = "invalid_clock"
    INVALID_TRANSITION = "invalid_transition"
    HANDOFF_WITHOUT_PARTIES = "handoff_without_parties"
    HANDOFF_SAME_PARTY = "handoff_same_party"
    DELIVERY_CONFLICT = "delivery_conflict"
    UNKNOWN_EVENT = "unknown_event"
    CURSOR_BEYOND_OUTBOX = "cursor_beyond_outbox"
    NO_TRANSACTION = "no_transaction"
    OPEN_TRANSACTION = "open_transaction"
    SCHEMA_NOT_MIGRATED = "schema_not_migrated"
    CORRUPT_ROW = "corrupt_row"
    EXPORT_FAILED = "export_failed"
    BUSY = "busy"
    STORAGE_ERROR = "storage_error"


class OutboxError(RuntimeError):
    """Nothing was recorded, acknowledged or exported past the last commit; ``code`` says why."""

    def __init__(self, code: OutboxFailure, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Handoff:
    """One real message from ``sender`` to ``receiver`` about one opportunity."""

    communication_id: str
    run_id: str
    opportunity_id: str
    sender: str
    receiver: str
    handoff_type: HandoffType
    reason: str
    at: datetime
    invocation_id: str | None = None
    evidence_hash: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    seq: int
    delivery_id: str
    kind: OutboxKind
    subject_id: str
    state: str | None
    sender: str | None
    receiver: str | None
    payload_json: str
    recorded_at: str

    def payload(self) -> dict[str, object]:
        try:
            value = json.loads(self.payload_json)
        except ValueError as error:
            raise OutboxError(OutboxFailure.CORRUPT_ROW, f"outbox seq {self.seq}: payload is not JSON") from error
        if not isinstance(value, dict):
            raise OutboxError(OutboxFailure.CORRUPT_ROW, f"outbox seq {self.seq}: payload is not an object")
        return value

    def export_line(self) -> str:
        """One JSONL line: delivery metadata first, then the payload keys (event rows keep their columns)."""
        line: dict[str, object] = {
            "delivery_id": self.delivery_id,
            "outbox_seq": self.seq,
            "outbox_kind": self.kind.value,
            "outbox_schema": EXPORT_SCHEMA,
        }
        payload = self.payload()
        clash = sorted(set(line) & set(payload))
        if clash:
            raise OutboxError(OutboxFailure.CORRUPT_ROW, f"outbox seq {self.seq}: payload reuses {clash}")
        line.update(payload)
        return json.dumps(line, ensure_ascii=False, default=str)


@dataclass(frozen=True, slots=True)
class RecordResult:
    """``created`` is ``False`` when the same delivery was already recorded (nothing written)."""

    entry: OutboxEntry
    created: bool


@dataclass(frozen=True, slots=True)
class ExportResult:
    written: int
    already_in_file: int
    position: int


# --- validation -----------------------------------------------------------------------------

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}")
_ROLE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_EVIDENCE_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_CONSUMER_LENGTH = 1024


def _utc_text(now: datetime, field: str = "now") -> str:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise OutboxError(OutboxFailure.INVALID_CLOCK, f"{field} must be a timezone-aware datetime")
    return now.astimezone(UTC).isoformat()


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise OutboxError(OutboxFailure.INVALID_FIELD, f"{field} must be an identifier of 1..128 safe characters")
    return value


def _optional(value: object, field: str, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise OutboxError(OutboxFailure.INVALID_FIELD, f"{field} is malformed")
    return value


def _code(value: object, field: str) -> str:
    if type(value) is not str or _CODE.fullmatch(value) is None:
        raise OutboxError(OutboxFailure.INVALID_FIELD, f"{field} must be a lowercase code, not free text")
    return value


def _party(value: object, field: str) -> str:
    if value is None or (type(value) is str and not value.strip()):
        raise OutboxError(OutboxFailure.HANDOFF_WITHOUT_PARTIES, f"a handoff needs a real {field}")
    if type(value) is not str or _ROLE.fullmatch(value) is None:
        raise OutboxError(OutboxFailure.INVALID_FIELD, f"{field} must be a lowercase role identifier")
    return value


def _consumer(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_CONSUMER_LENGTH
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise OutboxError(OutboxFailure.INVALID_FIELD, "consumer must be 1..1024 printable characters")
    return value


def _limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_READ_LIMIT:
        raise OutboxError(OutboxFailure.INVALID_FIELD, f"limit must be an int in 1..{MAX_READ_LIMIT}")
    return value


def handoff_payload(handoff: Handoff) -> dict[str, object]:
    """Validate ``handoff`` and return its payload in the UI's ``{id, from, to, ts, type, reason}`` shape."""
    if not isinstance(handoff, Handoff):
        raise OutboxError(OutboxFailure.INVALID_FIELD, "handoff must be a Handoff")
    sender = _party(handoff.sender, "sender")
    receiver = _party(handoff.receiver, "receiver")
    if sender == receiver:
        raise OutboxError(OutboxFailure.HANDOFF_SAME_PARTY, "sender and receiver must be two different agents")
    if not isinstance(handoff.handoff_type, HandoffType):
        raise OutboxError(OutboxFailure.INVALID_FIELD, "handoff_type must be a HandoffType")
    return {
        "id": _identifier(handoff.communication_id, "communication_id"),
        "from": sender,
        "to": receiver,
        "ts": _utc_text(handoff.at, "at"),
        "type": handoff.handoff_type.value,
        "reason": _code(handoff.reason, "reason"),
        "run_id": _identifier(handoff.run_id, "run_id"),
        "opportunity_id": _identifier(handoff.opportunity_id, "opportunity_id"),
        "invocation_id": _optional(handoff.invocation_id, "invocation_id", _IDENTIFIER),
        "evidence_hash": _optional(handoff.evidence_hash, "evidence_hash", _EVIDENCE_HASH),
    }


def check_transition(current: LifecycleState | None, new: LifecycleState) -> None:
    """Raise ``INVALID_TRANSITION`` unless ``current -> new`` is an allowed lifecycle move."""
    if not isinstance(new, LifecycleState):
        raise OutboxError(OutboxFailure.INVALID_FIELD, "state must be a LifecycleState")
    allowed = INITIAL_STATES if current is None else ALLOWED_TRANSITIONS[current]
    if new not in allowed:
        before = "(new item)" if current is None else current.value
        raise OutboxError(OutboxFailure.INVALID_TRANSITION, f"{before} -> {new.value} is not allowed")


def jsonl_consumer(path: str | os.PathLike[str]) -> str:
    """The cursor name of the JSONL export to ``path`` (absolute, case-normalised)."""
    return "jsonl:" + os.path.normcase(os.path.abspath(os.fspath(path)))


# --- transactions ---------------------------------------------------------------------------


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
        raise OutboxError(OutboxFailure.SCHEMA_NOT_MIGRATED, str(error)) from error
    if pending:
        raise OutboxError(
            OutboxFailure.SCHEMA_NOT_MIGRATED,
            f"ledger versions {[migration.version for migration in pending]} are not applied",
        )


def _write[T](conn: sqlite3.Connection, work: Callable[[], T], sleep: Sleep) -> T:
    """Run ``work`` in one ``BEGIN IMMEDIATE`` transaction, with bounded busy retries."""
    if conn.in_transaction:
        raise OutboxError(OutboxFailure.OPEN_TRANSACTION, "commit or roll back before an outbox write")
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
                raise OutboxError(OutboxFailure.STORAGE_ERROR, str(error)) from error
            last = error
            if attempt < len(BUSY_RETRY_DELAYS):
                sleep(BUSY_RETRY_DELAYS[attempt])
    raise OutboxError(OutboxFailure.BUSY, f"database stayed busy after {len(BUSY_RETRY_DELAYS) + 1} tries: {last}")


def _read[T](conn: sqlite3.Connection, work: Callable[[], T]) -> T:
    try:
        _require_migrated(conn)
        return work()
    except sqlite3.Error as error:
        code = OutboxFailure.BUSY if _is_busy(error) else OutboxFailure.STORAGE_ERROR
        raise OutboxError(code, str(error)) from error


# --- rows -----------------------------------------------------------------------------------

_COLUMNS = "seq, delivery_id, kind, subject_id, state, sender, receiver, payload_json, recorded_at"


def _one(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> tuple[object, ...] | None:
    row = conn.execute(sql, tuple(params)).fetchone()
    return None if row is None else tuple(row)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise OutboxError(OutboxFailure.CORRUPT_ROW, f"{field} is not text")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise OutboxError(OutboxFailure.CORRUPT_ROW, f"{field} is not an integer")
    return value


def _entry(row: tuple[object, ...]) -> OutboxEntry:
    seq, delivery_id, kind, subject_id, state, sender, receiver, payload_json, recorded_at = row
    try:
        parsed_kind = OutboxKind(_text(kind, "outbox.kind"))
    except ValueError as error:
        raise OutboxError(OutboxFailure.CORRUPT_ROW, f"outbox.kind {kind!r} is unknown") from error
    return OutboxEntry(
        seq=_int(seq, "outbox.seq"),
        delivery_id=_text(delivery_id, "outbox.delivery_id"),
        kind=parsed_kind,
        subject_id=_text(subject_id, "outbox.subject_id"),
        state=_optional_text(state, "outbox.state"),
        sender=_optional_text(sender, "outbox.sender"),
        receiver=_optional_text(receiver, "outbox.receiver"),
        payload_json=_text(payload_json, "outbox.payload_json"),
        recorded_at=_text(recorded_at, "outbox.recorded_at"),
    )


def _by_delivery(conn: sqlite3.Connection, delivery_id: str) -> OutboxEntry | None:
    row = _one(conn, f"SELECT {_COLUMNS} FROM {OUTBOX_TABLE} WHERE delivery_id = ?", (delivery_id,))
    return None if row is None else _entry(row)


def _insert(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    kind: OutboxKind,
    subject_id: str,
    state: str | None,
    sender: str | None,
    receiver: str | None,
    payload_json: str,
    recorded_at: str,
) -> OutboxEntry:
    cursor = conn.execute(
        f"INSERT INTO {OUTBOX_TABLE} (delivery_id, kind, subject_id, state, sender, receiver, payload_json, "
        "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (delivery_id, kind.value, subject_id, state, sender, receiver, payload_json, recorded_at),
    )
    seq = cursor.lastrowid
    if not isinstance(seq, int):
        raise OutboxError(OutboxFailure.STORAGE_ERROR, "the outbox insert returned no row id")
    return OutboxEntry(seq, delivery_id, kind, subject_id, state, sender, receiver, payload_json, recorded_at)


def _entries_after(conn: sqlite3.Connection, position: int, limit: int | None) -> tuple[OutboxEntry, ...]:
    sql = f"SELECT {_COLUMNS} FROM {OUTBOX_TABLE} WHERE seq > ? ORDER BY seq"
    params: tuple[object, ...] = (position,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (position, limit)
    return tuple(_entry(tuple(row)) for row in conn.execute(sql, params).fetchall())


def _cursor_row(conn: sqlite3.Connection, consumer: str) -> tuple[int, int | None] | None:
    row = _one(conn, f"SELECT position, byte_offset FROM {CURSOR_TABLE} WHERE consumer = ?", (consumer,))
    if row is None:
        return None
    position = _int(row[0], "outbox_cursors.position")
    offset = None if row[1] is None else _int(row[1], "outbox_cursors.byte_offset")
    if position < 0 or (offset is not None and offset < 0):
        raise OutboxError(OutboxFailure.CORRUPT_ROW, f"cursor {consumer!r} is negative")
    return position, offset


def _save_cursor(
    conn: sqlite3.Connection, consumer: str, position: int, byte_offset: int | None, now_text: str
) -> None:
    conn.execute(
        f"INSERT INTO {CURSOR_TABLE} (consumer, position, byte_offset, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(consumer) DO UPDATE SET position = excluded.position, byte_offset = excluded.byte_offset, "
        "updated_at = excluded.updated_at",
        (consumer, position, byte_offset, now_text),
    )


def _last_seq(conn: sqlite3.Connection) -> int:
    row = _one(conn, f"SELECT MAX(seq) FROM {OUTBOX_TABLE}")
    return 0 if row is None or row[0] is None else _int(row[0], "outbox.seq")


# --- EVENT rows (inside the caller's transaction) -------------------------------------------


def record_event_row(conn: sqlite3.Connection, event_id: str, *, now: datetime) -> OutboxEntry:
    """Snapshot the current ``events`` row into the outbox, inside the caller's open transaction.

    The caller has just changed that row in the same transaction; both commit together or
    neither does. The delivery ID ``event:<event_id>:<n>`` counts this event's outbox rows,
    so it is stable once written and never reused.
    """
    recorded_at = _utc_text(now)
    if not conn.in_transaction:
        raise OutboxError(OutboxFailure.NO_TRANSACTION, "an event outbox row must share the caller's transaction")
    if type(event_id) is not str or not event_id:
        raise OutboxError(OutboxFailure.INVALID_FIELD, "event_id must be non-empty text")
    _require_migrated(conn)
    try:
        cursor = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
        row = cursor.fetchone()
        if row is None:
            raise OutboxError(OutboxFailure.UNKNOWN_EVENT, f"no event {event_id!r}")
        names = [str(column[0]) for column in cursor.description]
        payload = dict(zip(names, tuple(row), strict=True))
        status = payload.get("status")
        if not isinstance(status, str) or not status:
            raise OutboxError(OutboxFailure.CORRUPT_ROW, f"event {event_id!r} has no status")
        count_row = _one(
            conn,
            f"SELECT COUNT(*) FROM {OUTBOX_TABLE} WHERE kind = ? AND subject_id = ?",
            (OutboxKind.EVENT.value, event_id),
        )
        number = (0 if count_row is None else _int(count_row[0], "count")) + 1
        return _insert(
            conn,
            delivery_id=f"event:{event_id}:{number}",
            kind=OutboxKind.EVENT,
            subject_id=event_id,
            state=status,
            sender=None,
            receiver=None,
            payload_json=json.dumps(payload, ensure_ascii=False, default=str),
            recorded_at=recorded_at,
        )
    except sqlite3.Error as error:
        code = OutboxFailure.BUSY if _is_busy(error) else OutboxFailure.STORAGE_ERROR
        raise OutboxError(code, str(error)) from error


# --- LIFECYCLE and HANDOFF rows (own transaction) -------------------------------------------


def _lifecycle_state(conn: sqlite3.Connection, item_id: str) -> LifecycleState | None:
    row = _one(conn, f"SELECT state FROM {LIFECYCLE_TABLE} WHERE item_id = ?", (item_id,))
    if row is None:
        return None
    try:
        return LifecycleState(_text(row[0], "lifecycle_items.state"))
    except ValueError as error:
        raise OutboxError(OutboxFailure.CORRUPT_ROW, f"lifecycle item {item_id!r} has an unknown state") from error


def record_lifecycle_transition(
    conn: sqlite3.Connection,
    item_id: str,
    state: LifecycleState,
    *,
    now: datetime,
    related_id: str | None = None,
    reason: str | None = None,
    sleep: Sleep = time.sleep,
) -> RecordResult:
    """Move ``item_id`` into ``state`` and write its outbox row in one transaction.

    ``related_id`` names the other item of a SUPERSEDED / DROPPED_BACKPRESSURE pair;
    ``reason`` is a lowercase code (never free text). Recording a state the item already
    entered, with the same details, returns the stored row and writes nothing.
    """
    item = _identifier(item_id, "item_id")
    if not isinstance(state, LifecycleState):
        raise OutboxError(OutboxFailure.INVALID_FIELD, "state must be a LifecycleState")
    related = _optional(related_id, "related_id", _IDENTIFIER)
    code = None if reason is None else _code(reason, "reason")
    now_text = _utc_text(now)
    delivery_id = f"lifecycle:{item}:{state.value}"

    def work() -> RecordResult:
        existing = _by_delivery(conn, delivery_id)
        if existing is not None:
            stored = existing.payload()
            if (stored.get("related_id"), stored.get("reason")) != (related, code):
                raise OutboxError(
                    OutboxFailure.DELIVERY_CONFLICT, f"{delivery_id} is already recorded with other details"
                )
            return RecordResult(existing, False)
        current = _lifecycle_state(conn, item)
        check_transition(current, state)
        if current is None:
            conn.execute(
                f"INSERT INTO {LIFECYCLE_TABLE} (item_id, state, entered_at, updated_at) VALUES (?, ?, ?, ?)",
                (item, state.value, now_text, now_text),
            )
        else:
            cursor = conn.execute(
                f"UPDATE {LIFECYCLE_TABLE} SET state = ?, updated_at = ? WHERE item_id = ? AND state = ?",
                (state.value, now_text, item, current.value),
            )
            if cursor.rowcount != 1:
                raise OutboxError(OutboxFailure.STORAGE_ERROR, f"lifecycle update changed {cursor.rowcount} rows")
        payload = {
            "item_id": item,
            "state": state.value,
            "previous_state": None if current is None else current.value,
            "at": now_text,
            "related_id": related,
            "reason": code,
        }
        entry = _insert(
            conn,
            delivery_id=delivery_id,
            kind=OutboxKind.LIFECYCLE,
            subject_id=item,
            state=state.value,
            sender=None,
            receiver=None,
            payload_json=json.dumps(payload, ensure_ascii=False),
            recorded_at=now_text,
        )
        return RecordResult(entry, True)

    return _write(conn, work, sleep)


def record_handoff(
    conn: sqlite3.Connection, handoff: Handoff, *, now: datetime, sleep: Sleep = time.sleep
) -> RecordResult:
    """Persist one real handoff. No sender or no receiver is refused before any SQL runs.

    The same ``communication_id`` with the same content returns the stored row and writes
    nothing; with different content it is a ``DELIVERY_CONFLICT``.
    """
    payload = handoff_payload(handoff)
    payload_json = json.dumps(payload, ensure_ascii=False)
    now_text = _utc_text(now)
    delivery_id = f"handoff:{payload['id']}"

    def work() -> RecordResult:
        existing = _by_delivery(conn, delivery_id)
        if existing is not None:
            if existing.payload_json != payload_json:
                raise OutboxError(
                    OutboxFailure.DELIVERY_CONFLICT, f"{delivery_id} is already recorded with other content"
                )
            return RecordResult(existing, False)
        entry = _insert(
            conn,
            delivery_id=delivery_id,
            kind=OutboxKind.HANDOFF,
            subject_id=handoff.opportunity_id,
            state=None,
            sender=handoff.sender,
            receiver=handoff.receiver,
            payload_json=payload_json,
            recorded_at=now_text,
        )
        return RecordResult(entry, True)

    return _write(conn, work, sleep)


# --- consumers --------------------------------------------------------------------------------


def cursor_position(conn: sqlite3.Connection, consumer: str) -> int:
    """How far ``consumer`` has acknowledged (0 = nothing yet)."""
    name = _consumer(consumer)

    def work() -> int:
        row = _cursor_row(conn, name)
        return 0 if row is None else row[0]

    return _read(conn, work)


def read_pending(
    conn: sqlite3.Connection, consumer: str, *, limit: int = DEFAULT_READ_LIMIT
) -> tuple[OutboxEntry, ...]:
    """Rows after ``consumer``'s cursor, in ``seq`` order. Reading never moves the cursor."""
    name = _consumer(consumer)
    count = _limit(limit)

    def work() -> tuple[OutboxEntry, ...]:
        row = _cursor_row(conn, name)
        return _entries_after(conn, 0 if row is None else row[0], count)

    return _read(conn, work)


def acknowledge(
    conn: sqlite3.Connection, consumer: str, seq: int, *, now: datetime, sleep: Sleep = time.sleep
) -> int:
    """Move ``consumer``'s cursor forward to ``seq``; return the cursor afterwards.

    An acknowledgement at or behind the cursor changes nothing (a replay after a restart is
    harmless). Acknowledging past the last outbox row is refused: nothing is invented.
    """
    name = _consumer(consumer)
    if type(seq) is not int or seq < 0:
        raise OutboxError(OutboxFailure.INVALID_FIELD, "seq must be a non-negative int")
    now_text = _utc_text(now)

    def work() -> int:
        last = _last_seq(conn)
        if seq > last:
            raise OutboxError(OutboxFailure.CURSOR_BEYOND_OUTBOX, f"seq {seq} is past the last outbox row {last}")
        row = _cursor_row(conn, name)
        current = 0 if row is None else row[0]
        if seq <= current:
            return current
        _save_cursor(conn, name, seq, None if row is None else row[1], now_text)
        return seq

    return _write(conn, work, sleep)


def load_entries(
    conn: sqlite3.Connection,
    *,
    after: int = 0,
    limit: int = DEFAULT_READ_LIMIT,
    kind: OutboxKind | None = None,
) -> tuple[OutboxEntry, ...]:
    """Outbox rows after ``after`` in ``seq`` order, optionally of one kind (read only)."""
    if type(after) is not int or after < 0:
        raise OutboxError(OutboxFailure.INVALID_FIELD, "after must be a non-negative int")
    count = _limit(limit)
    if kind is not None and not isinstance(kind, OutboxKind):
        raise OutboxError(OutboxFailure.INVALID_FIELD, "kind must be an OutboxKind")

    def work() -> tuple[OutboxEntry, ...]:
        if kind is None:
            return _entries_after(conn, after, count)
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM {OUTBOX_TABLE} WHERE seq > ? AND kind = ? ORDER BY seq LIMIT ?",
            (after, kind.value, count),
        ).fetchall()
        return tuple(_entry(tuple(row)) for row in rows)

    return _read(conn, work)


def lifecycle_state(conn: sqlite3.Connection, item_id: str) -> LifecycleState | None:
    item = _identifier(item_id, "item_id")
    return _read(conn, lambda: _lifecycle_state(conn, item))


# --- JSONL export -----------------------------------------------------------------------------


def _delivery_ids_in(target: str, offset: int) -> set[str]:
    """Delivery IDs already in ``target`` from ``offset`` on (the whole file if it shrank)."""
    try:
        size = os.path.getsize(target)
    except FileNotFoundError:
        return set()
    start = offset if offset <= size else 0
    with open(target, "rb") as handle:
        handle.seek(start)
        data = handle.read()
    found: set[str] = set()
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue  # a torn or foreign line is never ours to trust or to repair
        if isinstance(value, dict):
            delivery_id = value.get("delivery_id")
            if type(delivery_id) is str:
                found.add(delivery_id)
    return found


def _append(target: str, entries: Sequence[OutboxEntry], offset: int) -> tuple[int, int, int]:
    """Append the entries not yet in ``target``; fsync; return (written, skipped, new size)."""
    seen = _delivery_ids_in(target, offset)
    lines = [entry.export_line() for entry in entries]  # a corrupt row fails before any write
    written = skipped = 0
    with open(target, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size > 0:
            handle.seek(size - 1)
            if handle.read(1) != b"\n":
                handle.write(b"\n")  # keep a torn last line apart from the next record
        for entry, line in zip(entries, lines, strict=True):
            if entry.delivery_id in seen:
                skipped += 1
                continue
            handle.write(line.encode("utf-8") + b"\n")
            seen.add(entry.delivery_id)
            written += 1
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0, os.SEEK_END)
        return written, skipped, handle.tell()


def export_jsonl(
    conn: sqlite3.Connection, path: str | os.PathLike[str], *, now: datetime, sleep: Sleep = time.sleep
) -> ExportResult:
    """Append every outbox row after this file's cursor to ``path`` (JSONL), then advance the cursor.

    Idempotent: running it again after success writes nothing; after a crash between the
    append and the cursor commit it writes only the rows that are not in the file yet.
    At-least-once, not exactly-once: see the module docstring.
    """
    target = os.path.abspath(os.fspath(path))
    consumer = jsonl_consumer(target)
    now_text = _utc_text(now)

    def work() -> ExportResult:
        row = _cursor_row(conn, consumer)
        position = 0 if row is None else row[0]
        offset = 0 if row is None or row[1] is None else row[1]
        entries = _entries_after(conn, position, None)
        if not entries:
            return ExportResult(0, 0, position)
        try:
            written, skipped, size = _append(target, entries, offset)
        except OSError as error:
            raise OutboxError(
                OutboxFailure.EXPORT_FAILED, f"{type(error).__name__} errno={error.errno} while appending the export"
            ) from error
        _save_cursor(conn, consumer, entries[-1].seq, size, now_text)
        return ExportResult(written, skipped, entries[-1].seq)

    return _write(conn, work, sleep)
