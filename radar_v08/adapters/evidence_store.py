"""SQLite persistence for sealed evidence and the schema-version ledger (T030b).

This adapter adds three things to ``radar_state.sqlite``. Every change is additive:
new tables, indexes and triggers only. No existing table or row is altered.

* ``schema_version_ledger``: one append-only row per applied migration (version, name,
  statement checksum, UTC time applied). A database with no ledger is a legacy,
  pre-ledger database. Its existing tables are left exactly as they are.
* ``evidence_versions``: one immutable row per ``SealedEvidence``. The canonical JSON
  (``radar_v08.domain.evidence``) is stored next to indexed copies of its identity
  columns. On load the hash is re-verified and each column is checked against it.
* ``event_evidence``: an event's claim that it was built from one evidence version,
  for one run and one instrument. An event with no link row is read as
  legacy-unversioned. Nothing is backfilled: old rows never get invented facts,
  hashes or links.

Version 3 (T031a, ``INVOCATION_MIGRATION``) adds ``invocations``, ``invocation_budget`` and
``invocation_demand`` for ``radar_v08.adapters.invocation_store``: new tables, one partial
unique index on the new ``invocations`` table, and guard triggers. No legacy table gets a
constraint, index, column or row change (DECISIONS.md D19).

Version 4 (T033a, ``OUTBOX_MIGRATION``) adds ``lifecycle_items``, ``outbox`` and
``outbox_cursors`` for ``radar_v08.adapters.outbox_store``: new tables, the delivery-ID
uniqueness on the new ``outbox`` table only, one plain index and guard triggers. Again no
legacy table (``events`` included) is altered, constrained or indexed.

Migrations (``apply_schema_migrations``):

* A fast read-only check returns at once when the ledger is current, so a second run
  changes nothing.
* Otherwise one short ``BEGIN IMMEDIATE`` transaction re-checks the ledger under the
  write lock. It then runs every pending statement and appends each ledger row, and
  commits once. Any failure rolls the whole transaction back, so the database is left
  exactly as it was.
* A ledger that is newer than the code, has a gap, or has a changed checksum is
  refused with a typed ``SchemaMigrationError``. It is never "repaired".
* Statements use plain ``CREATE`` (no ``IF NOT EXISTS``). An unexpected object that
  already has the same name therefore fails the migration instead of being silently
  adopted.

Rollback of the migration (documented in ``docs/tasks/results/T030.md``): drop the
triggers, then the two evidence tables and the ledger. Legacy tables are untouched
by the migration, so nothing else needs undoing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from ..domain.evidence import (
    EvidenceRecord,
    EvidenceRejected,
    LegacyUnversionedEvidence,
    RejectionCode,
    SealedEvidence,
    evidence_from_json,
    evidence_from_record,
    evidence_to_json,
)
from ..domain.integrity import InstrumentId, InstrumentKind

LEDGER_TABLE = "schema_version_ledger"
EVIDENCE_TABLE = "evidence_versions"
EVENT_LINK_TABLE = "event_evidence"


@dataclass(frozen=True, slots=True)
class Migration:
    """One additive schema step. ``checksum`` binds the ledger row to the exact statements."""

    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        text = "\n;\n".join(self.statements)
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


LEDGER_MIGRATION = Migration(
    version=1,
    name="schema_version_ledger",
    statements=(
        """CREATE TABLE schema_version_ledger (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)""",
        """CREATE TRIGGER schema_version_ledger_no_update BEFORE UPDATE ON schema_version_ledger
BEGIN SELECT RAISE(ABORT, 'schema_version_ledger is append-only'); END""",
        """CREATE TRIGGER schema_version_ledger_no_delete BEFORE DELETE ON schema_version_ledger
BEGIN SELECT RAISE(ABORT, 'schema_version_ledger is append-only'); END""",
    ),
)

EVIDENCE_MIGRATION = Migration(
    version=2,
    name="evidence_versions_and_event_links",
    statements=(
        """CREATE TABLE evidence_versions (
    evidence_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL,
    code_version TEXT NOT NULL,
    run_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    record_json TEXT NOT NULL,
    stored_at TEXT NOT NULL
)""",
        "CREATE INDEX idx_evidence_versions_run_instrument ON evidence_versions(run_id, venue, symbol)",
        """CREATE TRIGGER evidence_versions_immutable BEFORE UPDATE ON evidence_versions
BEGIN SELECT RAISE(ABORT, 'evidence_versions rows are immutable'); END""",
        """CREATE TABLE event_evidence (
    event_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES evidence_versions(evidence_id),
    run_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    instrument_kind TEXT NOT NULL,
    base TEXT NOT NULL,
    quote TEXT NOT NULL,
    size_unit TEXT NOT NULL,
    linked_at TEXT NOT NULL
)""",
        "CREATE INDEX idx_event_evidence_evidence ON event_evidence(evidence_id)",
        """CREATE TRIGGER event_evidence_immutable BEFORE UPDATE ON event_evidence
BEGIN SELECT RAISE(ABORT, 'event_evidence rows are immutable'); END""",
    ),
)

# T031a (D19): new tables only. Active-identity uniqueness is a partial unique index on the
# new ``invocations`` table (rows in state CLAIMED); no legacy table (``events`` included)
# gets a constraint or index. Counters are guarded by triggers so they can only grow.
INVOCATION_MIGRATION = Migration(
    version=3,
    name="invocations_budget_and_demand",
    statements=(
        """CREATE TABLE invocations (
    invocation_id TEXT PRIMARY KEY,
    venue TEXT NOT NULL,
    market_kind TEXT NOT NULL,
    native_instrument TEXT NOT NULL,
    setup TEXT NOT NULL,
    direction TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('CLAIMED', 'COMPLETED', 'RELEASED')),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    lease_owner TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    demand_count INTEGER NOT NULL CHECK (demand_count >= 1),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    hour_window TEXT NOT NULL,
    day_window TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT
)""",
        """CREATE UNIQUE INDEX uq_invocations_active_identity ON invocations(
    venue, market_kind, native_instrument, setup, direction, evidence_hash, policy_version
) WHERE state = 'CLAIMED'""",
        "CREATE INDEX idx_invocations_state_lease ON invocations(state, lease_expires_at)",
        """CREATE TRIGGER invocations_identity_immutable BEFORE UPDATE OF
    invocation_id, venue, market_kind, native_instrument, setup, direction, evidence_hash, policy_version,
    model, hour_window, day_window, claimed_at ON invocations
BEGIN SELECT RAISE(ABORT, 'invocation identity is immutable'); END""",
        """CREATE TRIGGER invocations_terminal_is_final BEFORE UPDATE ON invocations
WHEN OLD.state <> 'CLAIMED'
BEGIN SELECT RAISE(ABORT, 'a completed or released invocation is final'); END""",
        """CREATE TRIGGER invocations_counters_never_decrease BEFORE UPDATE ON invocations
WHEN NEW.attempt_count < OLD.attempt_count OR NEW.demand_count < OLD.demand_count
    OR NEW.generation < OLD.generation
BEGIN SELECT RAISE(ABORT, 'invocation counters and generation never decrease'); END""",
        """CREATE TABLE invocation_budget (
    model TEXT NOT NULL,
    window_kind TEXT NOT NULL CHECK (window_kind IN ('hour', 'day')),
    window_start TEXT NOT NULL,
    reserved INTEGER NOT NULL CHECK (reserved >= 0),
    PRIMARY KEY (model, window_kind, window_start)
)""",
        """CREATE TRIGGER invocation_budget_never_decreases BEFORE UPDATE ON invocation_budget
WHEN NEW.reserved < OLD.reserved
BEGIN SELECT RAISE(ABORT, 'budget reservations are never returned'); END""",
        """CREATE TABLE invocation_demand (
    model TEXT NOT NULL,
    window_kind TEXT NOT NULL CHECK (window_kind IN ('hour', 'day')),
    window_start TEXT NOT NULL,
    observed INTEGER NOT NULL CHECK (observed >= 0),
    refused INTEGER NOT NULL CHECK (refused >= 0),
    PRIMARY KEY (model, window_kind, window_start)
)""",
        """CREATE TRIGGER invocation_demand_never_decreases BEFORE UPDATE ON invocation_demand
WHEN NEW.observed < OLD.observed OR NEW.refused < OLD.refused
BEGIN SELECT RAISE(ABORT, 'demand counters never decrease'); END""",
    ),
)

# T033a (D19): new tables only, for ``radar_v08.adapters.outbox_store``. Delivery-ID
# uniqueness lives on the new ``outbox`` table; ``events`` and every other legacy table get
# no constraint, index, column or row change. Outbox rows are immutable and never deleted;
# consumer cursors only move forward; a finished lifecycle item never changes again.
OUTBOX_MIGRATION = Migration(
    version=4,
    name="lifecycle_outbox_and_cursors",
    statements=(
        """CREATE TABLE lifecycle_items (
    item_id TEXT PRIMARY KEY CHECK (length(item_id) > 0),
    state TEXT NOT NULL CHECK (state IN (
        'QUEUED', 'LOADING', 'RUNNING', 'FINISHED', 'FAILED', 'ABORT_STALE', 'SUPERSEDED', 'DROPPED_BACKPRESSURE'
    )),
    entered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
        """CREATE TRIGGER lifecycle_items_identity_immutable BEFORE UPDATE OF item_id, entered_at ON lifecycle_items
BEGIN SELECT RAISE(ABORT, 'lifecycle item identity is immutable'); END""",
        """CREATE TRIGGER lifecycle_items_terminal_is_final BEFORE UPDATE ON lifecycle_items
WHEN OLD.state IN ('FINISHED', 'FAILED', 'ABORT_STALE', 'SUPERSEDED', 'DROPPED_BACKPRESSURE')
BEGIN SELECT RAISE(ABORT, 'a finished lifecycle item is final'); END""",
        """CREATE TRIGGER lifecycle_items_no_delete BEFORE DELETE ON lifecycle_items
BEGIN SELECT RAISE(ABORT, 'lifecycle items are never deleted'); END""",
        """CREATE TABLE outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE CHECK (length(delivery_id) > 0),
    kind TEXT NOT NULL CHECK (kind IN ('EVENT', 'LIFECYCLE', 'HANDOFF')),
    subject_id TEXT NOT NULL CHECK (length(subject_id) > 0),
    state TEXT,
    sender TEXT,
    receiver TEXT,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    CHECK (
        (kind = 'HANDOFF' AND state IS NULL AND sender IS NOT NULL AND receiver IS NOT NULL
            AND length(trim(sender)) > 0 AND length(trim(receiver)) > 0 AND sender <> receiver)
        OR (kind <> 'HANDOFF' AND sender IS NULL AND receiver IS NULL AND state IS NOT NULL)
    ),
    CHECK (kind <> 'LIFECYCLE' OR state IN (
        'QUEUED', 'LOADING', 'RUNNING', 'FINISHED', 'FAILED', 'ABORT_STALE', 'SUPERSEDED', 'DROPPED_BACKPRESSURE'
    ))
)""",
        "CREATE INDEX idx_outbox_kind_subject ON outbox(kind, subject_id, seq)",
        """CREATE TRIGGER outbox_rows_immutable BEFORE UPDATE ON outbox
BEGIN SELECT RAISE(ABORT, 'outbox rows are immutable'); END""",
        """CREATE TRIGGER outbox_rows_never_deleted BEFORE DELETE ON outbox
BEGIN SELECT RAISE(ABORT, 'outbox rows are never deleted'); END""",
        """CREATE TABLE outbox_cursors (
    consumer TEXT PRIMARY KEY CHECK (length(consumer) > 0),
    position INTEGER NOT NULL CHECK (position >= 0),
    byte_offset INTEGER CHECK (byte_offset IS NULL OR byte_offset >= 0),
    updated_at TEXT NOT NULL
)""",
        """CREATE TRIGGER outbox_cursors_never_move_back BEFORE UPDATE ON outbox_cursors
WHEN NEW.position < OLD.position OR NEW.consumer <> OLD.consumer
BEGIN SELECT RAISE(ABORT, 'an outbox cursor never moves back'); END""",
        """CREATE TRIGGER outbox_cursors_no_delete BEFORE DELETE ON outbox_cursors
BEGIN SELECT RAISE(ABORT, 'outbox cursors are never deleted'); END""",
    ),
)

SCHEMA_MIGRATIONS: tuple[Migration, ...] = (
    LEDGER_MIGRATION,
    EVIDENCE_MIGRATION,
    INVOCATION_MIGRATION,
    OUTBOX_MIGRATION,
)


# --- typed failures -----------------------------------------------------------------------


class MigrationFailure(Enum):
    INVALID_PLAN = "invalid_plan"
    INVALID_CLOCK = "invalid_clock"
    OPEN_TRANSACTION = "open_transaction"
    UNKNOWN_VERSION = "unknown_version"
    LEDGER_GAP = "ledger_gap"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    MALFORMED_LEDGER = "malformed_ledger"
    STATEMENT_FAILED = "statement_failed"
    SQLITE_ERROR = "sqlite_error"


class SchemaMigrationError(RuntimeError):
    """The migration did not run or was rolled back; ``code`` says why."""

    def __init__(self, code: MigrationFailure, detail: str, version: int | None = None) -> None:
        where = "" if version is None else f" (version {version})"
        super().__init__(f"{code.value}{where}: {detail}")
        self.code = code
        self.version = version
        self.detail = detail


class StoreFailure(Enum):
    SCHEMA_NOT_MIGRATED = "schema_not_migrated"
    EVENT_NOT_FOUND = "event_not_found"
    LINK_CONFLICT = "link_conflict"
    OPEN_TRANSACTION = "open_transaction"
    INVALID_CLOCK = "invalid_clock"
    SQLITE_ERROR = "sqlite_error"


class EvidenceStoreError(RuntimeError):
    """A store-level refusal that is not about evidence content (that is ``EvidenceRejected``)."""

    def __init__(self, code: StoreFailure, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# --- ledger ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    version: int
    name: str
    checksum: str
    applied_at: str


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _fetchall(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> list[tuple[object, ...]]:
    return [tuple(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _fetchone(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> tuple[object, ...] | None:
    row = conn.execute(sql, tuple(params)).fetchone()
    return None if row is None else tuple(row)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return _fetchone(conn, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)) is not None


def read_ledger(conn: sqlite3.Connection) -> tuple[LedgerEntry, ...]:
    """The applied migrations in version order; empty for a legacy (pre-ledger) database."""
    if not _table_exists(conn, LEDGER_TABLE):
        return ()
    entries: list[LedgerEntry] = []
    for row in _fetchall(conn, "SELECT version, name, checksum, applied_at FROM schema_version_ledger ORDER BY version"):
        version, name, checksum, applied_at = row
        if (
            not isinstance(version, int)
            or not isinstance(name, str)
            or not isinstance(checksum, str)
            or not isinstance(applied_at, str)
        ):
            raise SchemaMigrationError(MigrationFailure.MALFORMED_LEDGER, f"ledger row {row!r} has wrong column types")
        entries.append(LedgerEntry(version, name, checksum, applied_at))
    return tuple(entries)


def _check_plan(migrations: Sequence[Migration]) -> None:
    if not migrations or migrations[0] != LEDGER_MIGRATION:
        raise SchemaMigrationError(MigrationFailure.INVALID_PLAN, "the first migration must create the ledger")
    for index, migration in enumerate(migrations, start=1):
        if migration.version != index:
            raise SchemaMigrationError(
                MigrationFailure.INVALID_PLAN, f"versions must be 1..n in order, got {migration.version} at {index}"
            )
        if not migration.statements or not migration.name:
            raise SchemaMigrationError(MigrationFailure.INVALID_PLAN, "empty migration", migration.version)


def _pending(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    ledger = read_ledger(conn)
    known = {migration.version: migration for migration in migrations}
    for position, entry in enumerate(ledger, start=1):
        migration = known.get(entry.version)
        if migration is None:
            raise SchemaMigrationError(
                MigrationFailure.UNKNOWN_VERSION,
                f"database has version {entry.version}; this code knows 1..{len(migrations)} - refusing to downgrade",
                entry.version,
            )
        if entry.version != position:
            raise SchemaMigrationError(MigrationFailure.LEDGER_GAP, f"version {position} missing from ledger", position)
        if entry.name != migration.name or entry.checksum != migration.checksum:
            raise SchemaMigrationError(
                MigrationFailure.CHECKSUM_MISMATCH,
                f"ledger says {entry.name!r} {entry.checksum}, code says {migration.name!r} {migration.checksum}",
                entry.version,
            )
    return tuple(migrations[len(ledger) :])


def pending_migrations(
    conn: sqlite3.Connection, migrations: Sequence[Migration] = SCHEMA_MIGRATIONS
) -> tuple[Migration, ...]:
    """Migrations the database still lacks; raises on a ledger the code cannot trust."""
    _check_plan(migrations)
    try:
        return _pending(conn, migrations)
    except sqlite3.Error as error:
        raise SchemaMigrationError(MigrationFailure.SQLITE_ERROR, str(error)) from error


def apply_schema_migrations(
    conn: sqlite3.Connection, *, now: datetime, migrations: Sequence[Migration] = SCHEMA_MIGRATIONS
) -> tuple[int, ...]:
    """Apply pending migrations in one short transaction; return the versions applied.

    Returns ``()`` without writing anything when the ledger is already current.
    """
    _check_plan(migrations)
    if now.tzinfo is None or now.utcoffset() is None:
        raise SchemaMigrationError(MigrationFailure.INVALID_CLOCK, "migration time must be timezone-aware")
    if conn.in_transaction:
        raise SchemaMigrationError(MigrationFailure.OPEN_TRANSACTION, "commit or roll back before migrating")
    try:
        if not _pending(conn, migrations):
            return ()
        conn.execute("BEGIN IMMEDIATE")
        try:
            todo = _pending(conn, migrations)  # re-checked under the write lock
            applied_at = _utc_text(now)
            for migration in todo:
                for number, statement in enumerate(migration.statements, start=1):
                    try:
                        conn.execute(statement)
                    except sqlite3.Error as error:
                        raise SchemaMigrationError(
                            MigrationFailure.STATEMENT_FAILED,
                            f"statement {number}/{len(migration.statements)} of {migration.name!r}: {error}",
                            migration.version,
                        ) from error
                conn.execute(
                    "INSERT INTO schema_version_ledger (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                    (migration.version, migration.name, migration.checksum, applied_at),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise SchemaMigrationError(MigrationFailure.SQLITE_ERROR, str(error)) from error
    return tuple(migration.version for migration in todo)


# --- evidence rows ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceLink:
    """What an event claims about the evidence it was built from."""

    event_id: str
    evidence_id: str
    run_id: str
    instrument: InstrumentId
    linked_at: datetime


@dataclass(frozen=True, slots=True)
class LinkedEvidence:
    """An event's link plus the stored evidence it names (``None`` when that row is missing)."""

    link: EvidenceLink
    record: EvidenceRecord | None


type EventEvidence = LinkedEvidence | LegacyUnversionedEvidence


def _require_migrated(conn: sqlite3.Connection) -> None:
    if pending_migrations(conn):
        raise EvidenceStoreError(StoreFailure.SCHEMA_NOT_MIGRATED, "apply_schema_migrations has not been run")


def _require_clock(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise EvidenceStoreError(StoreFailure.INVALID_CLOCK, "store time must be timezone-aware")
    return _utc_text(now)


def _begin(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise EvidenceStoreError(StoreFailure.OPEN_TRANSACTION, "commit or roll back before writing evidence")
    conn.execute("BEGIN IMMEDIATE")


def _stored_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, field, "stored column is not text")
    return value


_COLUMN_CODES = {
    "evidence_id": RejectionCode.HASH_MISMATCH,
    "content_hash": RejectionCode.HASH_MISMATCH,
    "schema_version": RejectionCode.SCHEMA_VERSION_MISMATCH,
    "code_version": RejectionCode.HASH_MISMATCH,
    "run_id": RejectionCode.RUN_MISMATCH,
    "venue": RejectionCode.INSTRUMENT_MISMATCH,
    "symbol": RejectionCode.INSTRUMENT_MISMATCH,
    "sealed_at": RejectionCode.HASH_MISMATCH,
}


def _identity_columns(evidence: SealedEvidence) -> dict[str, str]:
    return {
        "evidence_id": evidence.evidence_id,
        "content_hash": evidence.content_hash,
        "schema_version": evidence.schema_version,
        "code_version": evidence.code_version,
        "run_id": evidence.run_id,
        "venue": evidence.instrument.venue,
        "symbol": evidence.instrument.symbol,
        "sealed_at": _utc_text(evidence.sealed_at),
    }


def _load_evidence(conn: sqlite3.Connection, evidence_id: str) -> SealedEvidence | None:
    row = _fetchone(
        conn,
        "SELECT evidence_id, content_hash, schema_version, code_version, run_id, venue, symbol, sealed_at, record_json "
        "FROM evidence_versions WHERE evidence_id = ?",
        (evidence_id,),
    )
    if row is None:
        return None
    columns = dict(zip(_COLUMN_CODES, row[:-1], strict=True))
    source_ref = f"{EVIDENCE_TABLE}:{evidence_id}"
    record = evidence_from_json(_stored_text(row[-1], f"{EVIDENCE_TABLE}.record_json"), source_ref=source_ref)
    if isinstance(record, LegacyUnversionedEvidence):
        raise EvidenceRejected(RejectionCode.LEGACY_UNVERSIONED, "record_json", f"{source_ref} has no schema version")
    for column, expected in _identity_columns(record).items():
        stored = _stored_text(columns[column], f"{EVIDENCE_TABLE}.{column}")
        if stored != expected:
            raise EvidenceRejected(
                _COLUMN_CODES[column], f"{EVIDENCE_TABLE}.{column}", f"{stored!r} is not bound to the stored record"
            )
    return record


def save_evidence(conn: sqlite3.Connection, evidence: SealedEvidence, *, now: datetime) -> bool:
    """Store one sealed evidence version. ``True`` if inserted, ``False`` if already stored identically."""
    if not isinstance(evidence, SealedEvidence):
        raise EvidenceRejected(RejectionCode.LEGACY_UNVERSIONED, "evidence", "only sealed evidence can be stored")
    stored_at = _require_clock(now)
    record_json = evidence_to_json(evidence)
    if evidence_from_json(record_json, source_ref="save") != evidence:
        raise EvidenceRejected(RejectionCode.HASH_MISMATCH, "evidence", "record does not round-trip")
    try:
        _require_migrated(conn)
        _begin(conn)
        try:
            existing = _fetchone(
                conn, "SELECT record_json FROM evidence_versions WHERE evidence_id = ?", (evidence.evidence_id,)
            )
            if existing is not None:
                conn.rollback()
                if existing[0] != record_json:
                    raise EvidenceRejected(
                        RejectionCode.HASH_MISMATCH, "evidence_id", "a different record is stored under this id"
                    )
                return False
            columns = _identity_columns(evidence)
            conn.execute(
                "INSERT INTO evidence_versions (evidence_id, content_hash, schema_version, code_version, run_id, "
                "venue, symbol, sealed_at, record_json, stored_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*columns.values(), record_json, stored_at),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    except sqlite3.Error as error:
        raise EvidenceStoreError(StoreFailure.SQLITE_ERROR, str(error)) from error
    return True


def load_evidence(conn: sqlite3.Connection, evidence_id: str) -> SealedEvidence | None:
    """Load and re-verify one stored evidence version; ``None`` if absent, ``EvidenceRejected`` if tampered."""
    try:
        _require_migrated(conn)
        return _load_evidence(conn, evidence_id)
    except sqlite3.Error as error:
        raise EvidenceStoreError(StoreFailure.SQLITE_ERROR, str(error)) from error


def verify_link(link: EvidenceLink, record: EvidenceRecord | None, *, event_id: str, event_asset: object) -> SealedEvidence:
    """Check an event's claim against the stored evidence; raise ``EvidenceRejected`` on any mismatch."""
    if link.event_id != event_id:
        raise EvidenceRejected(RejectionCode.INVALID_FIELD, "event_id", f"link is for {link.event_id!r}, not {event_id!r}")
    if record is None:
        raise EvidenceRejected(RejectionCode.UNKNOWN_DEPENDENCY, "evidence_id", f"{link.evidence_id} is not stored")
    if isinstance(record, LegacyUnversionedEvidence):
        raise EvidenceRejected(RejectionCode.LEGACY_UNVERSIONED, "evidence", f"{record.source_ref} is not sealed")
    if record.evidence_id != link.evidence_id:
        raise EvidenceRejected(
            RejectionCode.HASH_MISMATCH, "evidence_id", f"{link.evidence_id!r} != {record.evidence_id!r}"
        )
    if record.run_id != link.run_id:
        raise EvidenceRejected(RejectionCode.RUN_MISMATCH, "run_id", f"{link.run_id!r} != {record.run_id!r}")
    if record.instrument != link.instrument:
        raise EvidenceRejected(
            RejectionCode.INSTRUMENT_MISMATCH,
            "instrument",
            f"{link.instrument.venue}:{link.instrument.symbol} != {record.venue}:{record.instrument.symbol}",
        )
    if event_asset != record.instrument.base:
        raise EvidenceRejected(
            RejectionCode.INSTRUMENT_MISMATCH, "event.asset", f"{event_asset!r} != {record.instrument.base!r}"
        )
    return record


def _event_row(conn: sqlite3.Connection, event_id: str) -> dict[str, object] | None:
    cursor = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    names = [str(column[0]) for column in cursor.description]
    return dict(zip(names, tuple(row), strict=True))


def link_event_evidence(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    evidence_id: str,
    run_id: str,
    instrument: InstrumentId,
    now: datetime,
) -> bool:
    """Record that an event was built from one stored evidence version.

    The claim is verified against the stored evidence before it is written. ``True`` if
    inserted, ``False`` if the identical link already exists. A different link for the same
    event is refused (links are immutable).
    """
    linked_at = _require_clock(now)
    try:
        _require_migrated(conn)
        _begin(conn)
        try:
            event = _event_row(conn, event_id)
            if event is None:
                raise EvidenceStoreError(StoreFailure.EVENT_NOT_FOUND, f"no event {event_id!r}")
            link = EvidenceLink(event_id, evidence_id, run_id, instrument, datetime.fromisoformat(linked_at))
            verify_link(link, _load_evidence(conn, evidence_id), event_id=event_id, event_asset=event.get("asset"))
            existing = _load_link(conn, event_id)
            if existing is not None:
                conn.rollback()
                same = (existing.evidence_id, existing.run_id, existing.instrument) == (evidence_id, run_id, instrument)
                if not same:
                    raise EvidenceStoreError(
                        StoreFailure.LINK_CONFLICT, f"event {event_id!r} is already linked to {existing.evidence_id}"
                    )
                return False
            conn.execute(
                "INSERT INTO event_evidence (event_id, evidence_id, run_id, venue, symbol, instrument_kind, base, "
                "quote, size_unit, linked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    evidence_id,
                    run_id,
                    instrument.venue,
                    instrument.symbol,
                    instrument.kind.value,
                    instrument.base,
                    instrument.quote,
                    instrument.size_unit,
                    linked_at,
                ),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    except sqlite3.Error as error:
        raise EvidenceStoreError(StoreFailure.SQLITE_ERROR, str(error)) from error
    return True


def _load_link(conn: sqlite3.Connection, event_id: str) -> EvidenceLink | None:
    row = _fetchone(
        conn,
        "SELECT event_id, evidence_id, run_id, venue, symbol, instrument_kind, base, quote, size_unit, linked_at "
        "FROM event_evidence WHERE event_id = ?",
        (event_id,),
    )
    if row is None:
        return None
    names = ("event_id", "evidence_id", "run_id", "venue", "symbol", "instrument_kind", "base", "quote", "size_unit")
    text = {name: _stored_text(value, f"{EVENT_LINK_TABLE}.{name}") for name, value in zip(names, row, strict=False)}
    linked_at_text = _stored_text(row[-1], f"{EVENT_LINK_TABLE}.linked_at")
    try:
        kind = InstrumentKind(text["instrument_kind"])
        linked_at = datetime.fromisoformat(linked_at_text)
    except ValueError as error:
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, EVENT_LINK_TABLE, str(error)) from error
    if linked_at.tzinfo is None:
        raise EvidenceRejected(RejectionCode.NAIVE_TIMESTAMP, f"{EVENT_LINK_TABLE}.linked_at", "stored time is naive")
    instrument = InstrumentId(text["venue"], text["symbol"], kind, text["base"], text["quote"], text["size_unit"])
    return EvidenceLink(text["event_id"], text["evidence_id"], text["run_id"], instrument, linked_at)


def _legacy_event(event_id: str, event: Mapping[str, object]) -> LegacyUnversionedEvidence:
    # Only columns that really hold a value are reported; nothing is derived or invented.
    present = {name: value for name, value in event.items() if value is not None}
    record = evidence_from_record(present, source_ref=f"events:{event_id}")
    if not isinstance(record, LegacyUnversionedEvidence):
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, "events", "an events row cannot carry a schema version")
    return record


def load_event_evidence(conn: sqlite3.Connection, event_id: str) -> EventEvidence:
    """The evidence an event claims, or ``LegacyUnversionedEvidence`` for an event with no link.

    Never verifies the claim itself (``verify_link`` / the context builder do); a stored
    evidence row whose hash or identity columns do not match raises ``EvidenceRejected``.
    """
    try:
        _require_migrated(conn)
        event = _event_row(conn, event_id)
        if event is None:
            raise EvidenceStoreError(StoreFailure.EVENT_NOT_FOUND, f"no event {event_id!r}")
        link = _load_link(conn, event_id)
        if link is None:
            return _legacy_event(event_id, event)
        return LinkedEvidence(link, _load_evidence(conn, link.evidence_id))
    except sqlite3.Error as error:
        raise EvidenceStoreError(StoreFailure.SQLITE_ERROR, str(error)) from error
