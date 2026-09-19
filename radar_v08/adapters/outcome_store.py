"""SQLite store and L2 labeler for prospective outcome labels (T041).

Persists ``radar_v08.domain.outcomes`` in the three tables of ledger version 5
(``evidence_store.OUTCOME_MIGRATION``): ``outcome_subjects``, ``outcome_subject_costs`` and
``outcome_labels``. The legacy ``forward_returns`` table and its labeler
(``radar_v08.l2.label_forward_returns``) are neither read nor written here: legacy labels
stay raw.

* ``register_subject`` records one decision-time observation with its links. A sealed
  evidence id must exist in ``evidence_versions`` for the same instrument; an invocation id
  must exist in ``invocations`` for the same venue, market kind, native pair and direction,
  and, when evidence is linked too, for the same evidence hash. A missing link is stored as
  its typed reason; nothing is looked up or inferred to fill it.
* ``label_due_outcomes`` is the labeler: for every subject horizon that has no label yet it
  reads the subject's own Kraken spot pair from ``spot_snapshots`` and asks the domain for
  the label as of ``now``. Only matured horizons are written; a label is written once and
  never changed (immutability triggers), so repeating a run writes nothing new.
* ``outcomes_known_as_of`` is the join for consumers: only labels with
  ``label_available_at <= decision_as_of`` are returned (filtered in SQL, then again by
  ``visible_outcomes``).

Times are stored as fixed-width UTC text, so text order is time order. ``spot_snapshots.ts``
is written by the heartbeat as UTC ISO text; rows are pre-selected as text with a one-minute
margin and then parsed exactly. A row whose ``ts`` is not UTC ISO text is skipped as
malformed (it cannot be placed in time).

Writes run in one ``BEGIN IMMEDIATE`` transaction each and roll back entirely on any error.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum

from ..domain.costs import CostStatus, Side
from ..domain.evidence import EvidenceRejected
from ..domain.integrity import InstrumentId, InstrumentKind
from ..domain.invocation import Direction, InvocationError
from ..domain.outcomes import (
    HORIZONS,
    OUTCOME_POLICY_VERSION,
    Availability,
    DecisionKind,
    DecisionRef,
    Horizon,
    HorizonCost,
    LinkedOutcome,
    LinkMissingReason,
    MissingReason,
    NotMature,
    OutcomeInputError,
    OutcomeLabel,
    OutcomeSubject,
    PriceBasis,
    QuoteObservation,
    RecordedCost,
    label_horizon,
    utc_text,
    visible_outcomes,
)
from . import evidence_store, invocation_store

#: The venue whose spot quotes ``spot_snapshots`` holds (``kraken_timestamps.VENUE_SPOT``).
SPOT_SNAPSHOT_VENUE = "kraken"
SNAPSHOT_TEXT_MARGIN = timedelta(minutes=1)
DEFAULT_LABEL_BATCH = 500

SUBJECT_TABLE = "outcome_subjects"
COST_TABLE = "outcome_subject_costs"
LABEL_TABLE = "outcome_labels"


class OutcomeStoreFailure(Enum):
    SCHEMA_NOT_MIGRATED = "schema_not_migrated"
    OPEN_TRANSACTION = "open_transaction"
    INVALID_ARGUMENT = "invalid_argument"
    SUBJECT_CONFLICT = "subject_conflict"
    SUBJECT_NOT_FOUND = "subject_not_found"
    LABEL_CONFLICT = "label_conflict"
    LINK_NOT_FOUND = "link_not_found"
    LINK_MISMATCH = "link_mismatch"
    MALFORMED_ROW = "malformed_row"
    SQLITE_ERROR = "sqlite_error"


class OutcomeStoreError(RuntimeError):
    """Nothing was written (or it was rolled back); ``code`` says why."""

    def __init__(self, code: OutcomeStoreFailure, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class LabelRun:
    """What one labeler pass did. ``not_mature`` horizons were left for a later pass."""

    horizons_considered: int
    labeled_available: int
    labeled_unavailable: int
    not_mature: int

    @property
    def written(self) -> int:
        return self.labeled_available + self.labeled_unavailable


# --- plumbing -----------------------------------------------------------------------------------


def _require_migrated(conn: sqlite3.Connection) -> None:
    try:
        pending = evidence_store.pending_migrations(conn)
    except evidence_store.SchemaMigrationError as error:
        raise OutcomeStoreError(OutcomeStoreFailure.SCHEMA_NOT_MIGRATED, str(error)) from error
    if pending:
        raise OutcomeStoreError(
            OutcomeStoreFailure.SCHEMA_NOT_MIGRATED,
            f"ledger versions {[migration.version for migration in pending]} are not applied",
        )


def _clock(now: object, field: str = "now") -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise OutcomeStoreError(OutcomeStoreFailure.INVALID_ARGUMENT, f"{field} must be a timezone-aware datetime")
    return now.astimezone(UTC)


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> list[tuple[object, ...]]:
    return [tuple(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> tuple[object, ...] | None:
    row = conn.execute(sql, tuple(params)).fetchone()
    return None if row is None else tuple(row)


def _begin(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise OutcomeStoreError(OutcomeStoreFailure.OPEN_TRANSACTION, "commit or roll back before an outcome write")
    _require_migrated(conn)
    conn.execute("BEGIN IMMEDIATE")


def _malformed(where: str, detail: str) -> OutcomeStoreError:
    return OutcomeStoreError(OutcomeStoreFailure.MALFORMED_ROW, f"{where}: {detail}")


def _text(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise _malformed(where, "stored column is not text")
    return value


def _opt_text(value: object, where: str) -> str | None:
    return None if value is None else _text(value, where)


def _decimal(value: object, where: str) -> Decimal:
    try:
        parsed = Decimal(_text(value, where))
    except InvalidOperation as error:
        raise _malformed(where, "not a decimal") from error
    if not parsed.is_finite():
        raise _malformed(where, "not finite")
    return parsed


def _opt_decimal(value: object, where: str) -> Decimal | None:
    return None if value is None else _decimal(value, where)


def _moment(value: object, where: str) -> datetime:
    try:
        moment = datetime.fromisoformat(_text(value, where))
    except ValueError as error:
        raise _malformed(where, "not an ISO time") from error
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise _malformed(where, "not UTC")
    return moment


def _opt_moment(value: object, where: str) -> datetime | None:
    return None if value is None else _moment(value, where)


def _enum[E: Enum](enum_type: type[E], value: object, where: str) -> E:
    try:
        return enum_type(value)
    except ValueError as error:
        raise _malformed(where, f"{value!r} is not a {enum_type.__name__}") from error


def _opt_enum[E: Enum](enum_type: type[E], value: object, where: str) -> E | None:
    return None if value is None else _enum(enum_type, value, where)


def _dec_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


# --- subjects -----------------------------------------------------------------------------------

_SUBJECT_COLUMNS = (
    "subject_id",
    "policy_version",
    "venue",
    "symbol",
    "instrument_kind",
    "base",
    "quote",
    "size_unit",
    "pair",
    "direction",
    "decision_as_of",
    "entry_mid",
    "entry_observed_at",
    "evidence_id",
    "evidence_missing",
    "decision_kind",
    "decision_ref",
    "decision_missing",
    "arm",
    "arm_missing",
)
_SUBJECT_SELECT = f"SELECT {', '.join(_SUBJECT_COLUMNS)} FROM {SUBJECT_TABLE}"


def _subject_row(subject: OutcomeSubject) -> tuple[object, ...]:
    instrument = subject.instrument
    evidence, decision, arm = subject.evidence, subject.decision, subject.arm
    return (
        subject.subject_id,
        OUTCOME_POLICY_VERSION,
        instrument.venue,
        instrument.symbol,
        instrument.kind.value,
        instrument.base,
        instrument.quote,
        instrument.size_unit,
        subject.pair,
        subject.direction.value,
        utc_text(subject.decision_as_of),
        str(subject.entry_mid),
        utc_text(subject.entry_observed_at),
        None if isinstance(evidence, LinkMissingReason) else evidence,
        evidence.value if isinstance(evidence, LinkMissingReason) else None,
        decision.kind.value if isinstance(decision, DecisionRef) else None,
        decision.ref_id if isinstance(decision, DecisionRef) else None,
        decision.value if isinstance(decision, LinkMissingReason) else None,
        None if isinstance(arm, LinkMissingReason) else arm,
        arm.value if isinstance(arm, LinkMissingReason) else None,
    )


def _cost_rows(subject: OutcomeSubject) -> set[tuple[object, ...]]:
    return {
        (
            subject.subject_id,
            entry.horizon.value,
            entry.cost.policy_version,
            entry.cost.side.value,
            entry.cost.kind.value,
            entry.cost.status.value,
            _dec_text(entry.cost.total_fraction),
        )
        for entry in subject.costs
    }


def _stored_cost_rows(conn: sqlite3.Connection, subject_id: str) -> set[tuple[object, ...]]:
    return set(
        _rows(
            conn,
            f"SELECT subject_id, horizon, policy_version, side, instrument_kind, status, total_fraction "
            f"FROM {COST_TABLE} WHERE subject_id = ?",
            (subject_id,),
        )
    )


def _subject_from_row(row: tuple[object, ...], cost_rows: set[tuple[object, ...]]) -> OutcomeSubject:
    values = dict(zip(_SUBJECT_COLUMNS, row, strict=True))
    where = f"{SUBJECT_TABLE}:{values['subject_id']!r}"
    if values["policy_version"] != OUTCOME_POLICY_VERSION:
        raise _malformed(where, f"unknown outcome policy {values['policy_version']!r}")
    evidence: str | LinkMissingReason
    if values["evidence_id"] is not None:
        evidence = _text(values["evidence_id"], f"{where}.evidence_id")
    else:
        evidence = _enum(LinkMissingReason, values["evidence_missing"], f"{where}.evidence_missing")
    decision: DecisionRef | LinkMissingReason
    if values["decision_ref"] is not None:
        decision = DecisionRef(
            _enum(DecisionKind, values["decision_kind"], f"{where}.decision_kind"),
            _text(values["decision_ref"], f"{where}.decision_ref"),
        )
    else:
        decision = _enum(LinkMissingReason, values["decision_missing"], f"{where}.decision_missing")
    arm: str | LinkMissingReason
    if values["arm"] is not None:
        arm = _text(values["arm"], f"{where}.arm")
    else:
        arm = _enum(LinkMissingReason, values["arm_missing"], f"{where}.arm_missing")
    costs: list[HorizonCost] = []
    for cost_row in sorted(cost_rows, key=lambda r: _enum(Horizon, r[1], where).minutes):
        _, horizon, policy, side, kind, status, total = cost_row
        costs.append(
            HorizonCost(
                _enum(Horizon, horizon, f"{where}.cost.horizon"),
                RecordedCost(
                    _text(policy, f"{where}.cost.policy_version"),
                    _enum(Side, side, f"{where}.cost.side"),
                    _enum(InstrumentKind, kind, f"{where}.cost.instrument_kind"),
                    _enum(CostStatus, status, f"{where}.cost.status"),
                    _opt_decimal(total, f"{where}.cost.total_fraction"),
                ),
            )
        )
    try:
        subject = OutcomeSubject(
            instrument=InstrumentId(
                _text(values["venue"], f"{where}.venue"),
                _text(values["symbol"], f"{where}.symbol"),
                _enum(InstrumentKind, values["instrument_kind"], f"{where}.instrument_kind"),
                _text(values["base"], f"{where}.base"),
                _text(values["quote"], f"{where}.quote"),
                _text(values["size_unit"], f"{where}.size_unit"),
            ),
            pair=_text(values["pair"], f"{where}.pair"),
            direction=_enum(Direction, values["direction"], f"{where}.direction"),
            decision_as_of=_moment(values["decision_as_of"], f"{where}.decision_as_of"),
            entry_mid=_decimal(values["entry_mid"], f"{where}.entry_mid"),
            entry_observed_at=_moment(values["entry_observed_at"], f"{where}.entry_observed_at"),
            evidence=evidence,
            decision=decision,
            arm=arm,
            costs=tuple(costs),
        )
    except OutcomeInputError as error:
        raise _malformed(where, str(error)) from error
    if subject.subject_id != values["subject_id"]:
        raise _malformed(where, "subject_id is not bound to the stored identity")
    return subject


def _load_subject(conn: sqlite3.Connection, subject_id: str) -> OutcomeSubject | None:
    row = _one(conn, f"{_SUBJECT_SELECT} WHERE subject_id = ?", (subject_id,))
    if row is None:
        return None
    return _subject_from_row(row, _stored_cost_rows(conn, subject_id))


def _verify_links(conn: sqlite3.Connection, subject: OutcomeSubject) -> None:
    content_hash: str | None = None
    if isinstance(subject.evidence, str):
        try:
            evidence = evidence_store.load_evidence(conn, subject.evidence)
        except EvidenceRejected as error:
            raise OutcomeStoreError(OutcomeStoreFailure.LINK_MISMATCH, f"evidence: {error}") from error
        if evidence is None:
            raise OutcomeStoreError(OutcomeStoreFailure.LINK_NOT_FOUND, f"no sealed evidence {subject.evidence!r}")
        if evidence.instrument != subject.instrument:
            raise OutcomeStoreError(
                OutcomeStoreFailure.LINK_MISMATCH,
                f"evidence is for {evidence.instrument.venue}:{evidence.instrument.symbol}, "
                f"subject is {subject.instrument.venue}:{subject.instrument.symbol}",
            )
        content_hash = evidence.content_hash
    decision = subject.decision
    if isinstance(decision, DecisionRef) and decision.kind is DecisionKind.INVOCATION:
        try:
            record = invocation_store.load_invocation(conn, decision.ref_id)
        except InvocationError as error:
            raise OutcomeStoreError(OutcomeStoreFailure.LINK_MISMATCH, f"invocation: {error}") from error
        if record is None:
            raise OutcomeStoreError(OutcomeStoreFailure.LINK_NOT_FOUND, f"no invocation {decision.ref_id!r}")
        identity = record.identity
        expected = (subject.instrument.venue, subject.instrument.kind, subject.pair, subject.direction)
        actual = (identity.venue, identity.market_kind, identity.native_instrument, identity.direction)
        if actual != expected:
            raise OutcomeStoreError(
                OutcomeStoreFailure.LINK_MISMATCH, f"invocation is for {actual!r}, subject is {expected!r}"
            )
        if content_hash is not None and identity.evidence_hash != content_hash:
            raise OutcomeStoreError(
                OutcomeStoreFailure.LINK_MISMATCH, "invocation was claimed on different evidence than the subject's"
            )


def register_subject(conn: sqlite3.Connection, subject: OutcomeSubject, *, now: datetime) -> bool:
    """Record a subject. ``True`` if inserted, ``False`` if the identical subject exists.

    The same identity with different content (entry or costs) is refused with
    ``SUBJECT_CONFLICT``; the stored subject is never changed.
    """
    if not isinstance(subject, OutcomeSubject):
        raise OutcomeStoreError(OutcomeStoreFailure.INVALID_ARGUMENT, "subject must be OutcomeSubject")
    registered_at = utc_text(_clock(now))
    row = _subject_row(subject)
    costs = _cost_rows(subject)
    try:
        _begin(conn)
        try:
            existing = _one(conn, f"{_SUBJECT_SELECT} WHERE subject_id = ?", (subject.subject_id,))
            if existing is not None:
                same = existing == row and _stored_cost_rows(conn, subject.subject_id) == costs
                conn.rollback()
                if not same:
                    raise OutcomeStoreError(
                        OutcomeStoreFailure.SUBJECT_CONFLICT, f"{subject.subject_id} is stored with other content"
                    )
                return False
            _verify_links(conn, subject)
            conn.execute(
                f"INSERT INTO {SUBJECT_TABLE} ({', '.join(_SUBJECT_COLUMNS)}, registered_at) "
                f"VALUES ({', '.join('?' * (len(_SUBJECT_COLUMNS) + 1))})",
                (*row, registered_at),
            )
            conn.executemany(
                f"INSERT INTO {COST_TABLE} (subject_id, horizon, policy_version, side, instrument_kind, status, "
                "total_fraction) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [tuple(cost) for cost in sorted(costs, key=lambda r: str(r[1]))],
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    except sqlite3.Error as error:
        raise OutcomeStoreError(OutcomeStoreFailure.SQLITE_ERROR, str(error)) from error
    return True


def load_subject(conn: sqlite3.Connection, subject_id: str) -> OutcomeSubject | None:
    try:
        _require_migrated(conn)
        return _load_subject(conn, subject_id)
    except sqlite3.Error as error:
        raise OutcomeStoreError(OutcomeStoreFailure.SQLITE_ERROR, str(error)) from error


# --- labels -------------------------------------------------------------------------------------

_LABEL_COLUMNS = (
    "subject_id",
    "horizon",
    "policy_version",
    "status",
    "price_basis",
    "target_at",
    "label_available_at",
    "exit_mid",
    "exit_observed_at",
    "exit_source",
    "market_return",
    "market_missing",
    "gross_markout",
    "gross_missing",
    "net_markout",
    "net_missing",
    "cost_policy_version",
)
_LABEL_SELECT = f"SELECT {', '.join(_LABEL_COLUMNS)} FROM {LABEL_TABLE}"


def _label_row(label: OutcomeLabel) -> tuple[object, ...]:
    return (
        label.subject_id,
        label.horizon.value,
        label.policy_version,
        label.status.value,
        label.price_basis.value,
        utc_text(label.target_at),
        utc_text(label.label_available_at),
        _dec_text(label.exit_mid),
        None if label.exit_observed_at is None else utc_text(label.exit_observed_at),
        label.exit_source,
        _dec_text(label.market_return),
        None if label.market_missing is None else label.market_missing.value,
        _dec_text(label.gross_markout),
        None if label.gross_missing is None else label.gross_missing.value,
        _dec_text(label.net_markout),
        None if label.net_missing is None else label.net_missing.value,
        label.cost_policy_version,
    )


def _label_from_row(row: tuple[object, ...]) -> OutcomeLabel:
    values = dict(zip(_LABEL_COLUMNS, row, strict=True))
    where = f"{LABEL_TABLE}:{values['subject_id']!r}/{values['horizon']!r}"
    try:
        return OutcomeLabel(
            subject_id=_text(values["subject_id"], where),
            horizon=_enum(Horizon, values["horizon"], f"{where}.horizon"),
            status=_enum(Availability, values["status"], f"{where}.status"),
            target_at=_moment(values["target_at"], f"{where}.target_at"),
            label_available_at=_moment(values["label_available_at"], f"{where}.label_available_at"),
            exit_mid=_opt_decimal(values["exit_mid"], f"{where}.exit_mid"),
            exit_observed_at=_opt_moment(values["exit_observed_at"], f"{where}.exit_observed_at"),
            exit_source=_opt_text(values["exit_source"], f"{where}.exit_source"),
            market_return=_opt_decimal(values["market_return"], f"{where}.market_return"),
            market_missing=_opt_enum(MissingReason, values["market_missing"], f"{where}.market_missing"),
            gross_markout=_opt_decimal(values["gross_markout"], f"{where}.gross_markout"),
            gross_missing=_opt_enum(MissingReason, values["gross_missing"], f"{where}.gross_missing"),
            net_markout=_opt_decimal(values["net_markout"], f"{where}.net_markout"),
            net_missing=_opt_enum(MissingReason, values["net_missing"], f"{where}.net_missing"),
            cost_policy_version=_opt_text(values["cost_policy_version"], f"{where}.cost_policy_version"),
            policy_version=_text(values["policy_version"], f"{where}.policy_version"),
            price_basis=_enum(PriceBasis, values["price_basis"], f"{where}.price_basis"),
        )
    except OutcomeInputError as error:
        raise _malformed(where, str(error)) from error


def _insert_label(conn: sqlite3.Connection, label: OutcomeLabel, labeled_at: str) -> bool:
    row = _label_row(label)
    existing = _one(conn, f"{_LABEL_SELECT} WHERE subject_id = ? AND horizon = ?", (label.subject_id, row[1]))
    if existing is not None:
        if existing != row:
            raise OutcomeStoreError(
                OutcomeStoreFailure.LABEL_CONFLICT,
                f"{label.subject_id}/{label.horizon.value} is already labelled with other values",
            )
        return False
    conn.execute(
        f"INSERT INTO {LABEL_TABLE} ({', '.join(_LABEL_COLUMNS)}, labeled_at) "
        f"VALUES ({', '.join('?' * (len(_LABEL_COLUMNS) + 1))})",
        (*row, labeled_at),
    )
    return True


def save_label(conn: sqlite3.Connection, label: OutcomeLabel, *, now: datetime) -> bool:
    """Record one label. ``True`` if inserted, ``False`` if the identical label exists.

    A different label for the same subject and horizon is ``LABEL_CONFLICT``; the stored
    label is never changed. The subject must exist and the label must match it.
    """
    if not isinstance(label, OutcomeLabel):
        raise OutcomeStoreError(OutcomeStoreFailure.INVALID_ARGUMENT, "label must be OutcomeLabel")
    labeled_at = utc_text(_clock(now))
    try:
        _begin(conn)
        try:
            subject = _load_subject(conn, label.subject_id)
            if subject is None:
                raise OutcomeStoreError(OutcomeStoreFailure.SUBJECT_NOT_FOUND, f"no subject {label.subject_id!r}")
            try:
                LinkedOutcome(subject, label)
            except OutcomeInputError as error:
                raise OutcomeStoreError(OutcomeStoreFailure.INVALID_ARGUMENT, str(error)) from error
            inserted = _insert_label(conn, label, labeled_at)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    except sqlite3.Error as error:
        raise OutcomeStoreError(OutcomeStoreFailure.SQLITE_ERROR, str(error)) from error
    return inserted


def _linked(conn: sqlite3.Connection, rows: list[tuple[object, ...]]) -> tuple[LinkedOutcome, ...]:
    subjects: dict[str, OutcomeSubject] = {}
    outcomes: list[LinkedOutcome] = []
    for row in rows:
        label = _label_from_row(row)
        subject = subjects.get(label.subject_id)
        if subject is None:
            loaded = _load_subject(conn, label.subject_id)
            if loaded is None:
                raise _malformed(LABEL_TABLE, f"label for unknown subject {label.subject_id!r}")
            subject = subjects[label.subject_id] = loaded
        try:
            outcomes.append(LinkedOutcome(subject, label))
        except OutcomeInputError as error:
            raise _malformed(LABEL_TABLE, str(error)) from error
    outcomes.sort(
        key=lambda o: (o.label.label_available_at, o.subject.decision_as_of, o.label.subject_id, o.label.horizon.minutes)
    )
    return tuple(outcomes)


def load_outcomes(conn: sqlite3.Connection, subject_id: str) -> tuple[LinkedOutcome, ...]:
    """Every stored label of one subject, with the subject's links. No visibility cut-off."""
    try:
        _require_migrated(conn)
        return _linked(conn, _rows(conn, f"{_LABEL_SELECT} WHERE subject_id = ?", (subject_id,)))
    except sqlite3.Error as error:
        raise OutcomeStoreError(OutcomeStoreFailure.SQLITE_ERROR, str(error)) from error


def outcomes_known_as_of(
    conn: sqlite3.Connection,
    decision_as_of: datetime,
    *,
    venue: str | None = None,
    pair: str | None = None,
) -> tuple[LinkedOutcome, ...]:
    """Labels a decision at ``decision_as_of`` may join: ``label_available_at <= decision_as_of``."""
    cutoff = _clock(decision_as_of, "decision_as_of")
    sql = (
        f"SELECT {', '.join('l.' + column for column in _LABEL_COLUMNS)} FROM {LABEL_TABLE} l "
        f"JOIN {SUBJECT_TABLE} s ON s.subject_id = l.subject_id WHERE l.label_available_at <= ?"
    )
    params: list[object] = [utc_text(cutoff)]
    if venue is not None:
        sql += " AND s.venue = ?"
        params.append(venue)
    if pair is not None:
        sql += " AND s.pair = ?"
        params.append(pair)
    try:
        _require_migrated(conn)
        return visible_outcomes(_linked(conn, _rows(conn, sql, params)), cutoff)
    except sqlite3.Error as error:
        raise OutcomeStoreError(OutcomeStoreFailure.SQLITE_ERROR, str(error)) from error


# --- labeler ------------------------------------------------------------------------------------


def _finite_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return Decimal(str(value))


def _spot_quotes(conn: sqlite3.Connection, pair: str, start: datetime, end: datetime) -> list[QuoteObservation]:
    """Spot snapshots of exactly ``pair`` observed in ``[start, end]`` (text margin, then exact)."""
    rows = _rows(
        conn,
        "SELECT id, ts, bid, ask FROM spot_snapshots WHERE pair = ? AND ts >= ? AND ts <= ? ORDER BY ts, id",
        (pair, utc_text(start - SNAPSHOT_TEXT_MARGIN), utc_text(end + SNAPSHOT_TEXT_MARGIN)),
    )
    quotes: list[QuoteObservation] = []
    for row_id, ts, bid, ask in rows:
        if not isinstance(ts, str):
            continue
        try:
            observed_at = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
            continue
        if start <= observed_at <= end:
            quotes.append(QuoteObservation(observed_at, _finite_decimal(bid), _finite_decimal(ask), f"spot_snapshots:{row_id}"))
    return quotes


def _price_source(subject: OutcomeSubject) -> bool:
    return subject.instrument.kind is InstrumentKind.SPOT and subject.instrument.venue == SPOT_SNAPSHOT_VENUE


def label_due_outcomes(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    tolerance: timedelta,
    limit: int = DEFAULT_LABEL_BATCH,
) -> LabelRun:
    """Write every matured, still unlabelled horizon: per horizon, up to ``limit`` subjects, oldest first.

    One transaction; any error rolls the whole pass back. Horizons that are not mature are
    left untouched for a later pass. Existing labels are never read back into the
    computation and never changed, so a repeated pass writes nothing.
    """
    clock = _clock(now)
    if not isinstance(tolerance, timedelta) or tolerance < timedelta(0):
        raise OutcomeStoreError(OutcomeStoreFailure.INVALID_ARGUMENT, "tolerance must be a non-negative timedelta")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise OutcomeStoreError(OutcomeStoreFailure.INVALID_ARGUMENT, "limit must be an int >= 1")
    labeled_at = utc_text(clock)
    considered = available = unavailable = not_mature = 0
    try:
        _begin(conn)
        try:
            subjects: dict[str, OutcomeSubject] = {}
            for horizon in HORIZONS:
                due = [
                    _text(row[0], SUBJECT_TABLE)
                    for row in _rows(
                        conn,
                        f"SELECT s.subject_id FROM {SUBJECT_TABLE} s WHERE s.decision_as_of <= ? AND NOT EXISTS "
                        f"(SELECT 1 FROM {LABEL_TABLE} l WHERE l.subject_id = s.subject_id AND l.horizon = ?) "
                        "ORDER BY s.decision_as_of, s.subject_id LIMIT ?",
                        (utc_text(clock - horizon.duration), horizon.value, limit),
                    )
                ]
                for subject_id in due:
                    subject = subjects.get(subject_id)
                    if subject is None:
                        loaded = _load_subject(conn, subject_id)
                        if loaded is None:  # pragma: no cover - selected in this transaction
                            raise _malformed(SUBJECT_TABLE, f"{subject_id!r} vanished")
                        subject = subjects[subject_id] = loaded
                    considered += 1
                    quotes: list[QuoteObservation] | None = None
                    if _price_source(subject):
                        target = subject.target_at(horizon)
                        quotes = _spot_quotes(conn, subject.pair, target, min(target + tolerance, clock))
                    result = label_horizon(subject, horizon, quotes, now=clock, tolerance=tolerance)
                    if isinstance(result, NotMature):
                        not_mature += 1
                        continue
                    _insert_label(conn, result, labeled_at)
                    if result.status is Availability.AVAILABLE:
                        available += 1
                    else:
                        unavailable += 1
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    except sqlite3.Error as error:
        raise OutcomeStoreError(OutcomeStoreFailure.SQLITE_ERROR, str(error)) from error
    return LabelRun(considered, available, unavailable, not_mature)
