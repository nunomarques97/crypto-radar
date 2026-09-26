"""SQLite store for every Qwen finalist review, inline or shadow.

One append-only table, ``qwen_reviews``: one row per submitted finalist of one cycle
(``UNIQUE(run_id, asset)``), finalists routed IGNORE included. A row keeps the origin
cycle's deterministic setup, direction and scores, the router decision of that cycle, the
batch outcome and, when the batch validated a review for the asset, the review fields. A
late shadow result is written with the run id and cycle timestamp of the cycle that
submitted it, never of the cycle that happens to write it.

The table sits outside the schema-version ledger (``evidence_store.SCHEMA_MIGRATIONS``) on
purpose: a process still running earlier code opens the same database and refuses any
ledger version it does not know, while an extra table is invisible to it. ``ensure_schema``
therefore uses only ``CREATE ... IF NOT EXISTS``, alters no existing table or row, and
writes nothing when every object already exists.

Writes run in one ``BEGIN IMMEDIATE`` transaction and roll back entirely on any error.
Every row is validated before anything is written; the CHECK constraints are the backstop.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

TABLE = "qwen_reviews"

MODES = ("inline", "shadow")
BATCH_STATUSES = ("OK", "TIMEOUT", "UNAVAILABLE", "INVALID_JSON", "ERROR")
# Mirror radar_v08.setups / radar_v08.qwen; a test keeps them in step.
SETUP_TYPES = ("BREAKOUT", "CONTINUATION", "REVERSAL", "SQUEEZE_RELEASE", "EXHAUSTION", "NONE")
DIRECTIONS = ("LONG", "SHORT", "NONE")
ROUTER_DECISIONS = ("IGNORE", "SONNET", "FABLE")
CONFIDENCES = ("LOW", "MEDIUM", "HIGH")


def _sql_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL CHECK (length(run_id) > 0),
    cycle_ts TEXT NOT NULL CHECK (length(cycle_ts) > 0),
    mode TEXT NOT NULL CHECK (mode IN ({_sql_list(MODES)})),
    asset TEXT NOT NULL CHECK (length(asset) > 0),
    setup_type TEXT NOT NULL CHECK (setup_type IN ({_sql_list(SETUP_TYPES)})),
    direction TEXT NOT NULL CHECK (direction IN ({_sql_list(DIRECTIONS)})),
    anomaly_score REAL,
    opportunity_score REAL,
    tradeability_score REAL,
    router_decision TEXT CHECK (router_decision IS NULL OR router_decision IN ({_sql_list(ROUTER_DECISIONS)})),
    batch_status TEXT NOT NULL CHECK (batch_status IN ({_sql_list(BATCH_STATUSES)})),
    veto INTEGER CHECK (veto IS NULL OR veto IN (0, 1)),
    confidence TEXT CHECK (confidence IS NULL OR confidence IN ({_sql_list(CONFIDENCES)})),
    review_direction TEXT CHECK (review_direction IS NULL OR review_direction IN ({_sql_list(DIRECTIONS)})),
    call_sonnet INTEGER CHECK (call_sonnet IS NULL OR call_sonnet IN (0, 1)),
    call_fable INTEGER CHECK (call_fable IS NULL OR call_fable IN (0, 1)),
    elapsed_ms REAL CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    error_code TEXT,
    recorded_at TEXT NOT NULL,
    CHECK (
        (veto IS NULL AND confidence IS NULL AND review_direction IS NULL
            AND call_sonnet IS NULL AND call_fable IS NULL)
        OR (batch_status = 'OK' AND veto IS NOT NULL AND confidence IS NOT NULL
            AND review_direction IS NOT NULL AND call_sonnet IS NOT NULL AND call_fable IS NOT NULL)
    ),
    UNIQUE (run_id, asset)
)""",
    f"""CREATE TRIGGER IF NOT EXISTS {TABLE}_no_update BEFORE UPDATE ON {TABLE}
BEGIN
    SELECT RAISE(ABORT, '{TABLE} rows are append-only');
END""",
    f"""CREATE TRIGGER IF NOT EXISTS {TABLE}_no_delete BEFORE DELETE ON {TABLE}
BEGIN
    SELECT RAISE(ABORT, '{TABLE} rows are never deleted');
END""",
)
SCHEMA_OBJECTS = (("table", TABLE), ("trigger", f"{TABLE}_no_update"), ("trigger", f"{TABLE}_no_delete"))

_COLUMNS = (
    "run_id",
    "cycle_ts",
    "mode",
    "asset",
    "setup_type",
    "direction",
    "anomaly_score",
    "opportunity_score",
    "tradeability_score",
    "router_decision",
    "batch_status",
    "veto",
    "confidence",
    "review_direction",
    "call_sonnet",
    "call_fable",
    "elapsed_ms",
    "attempts",
    "error_code",
    "recorded_at",
)
_INSERT = (
    f"INSERT INTO {TABLE} ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)}) "
    "ON CONFLICT (run_id, asset) DO NOTHING"
)


class QwenReviewStoreFailure(Enum):
    INVALID_ROW = "invalid_row"
    INVALID_ARGUMENT = "invalid_argument"
    OPEN_TRANSACTION = "open_transaction"
    MALFORMED_ROW = "malformed_row"
    SQLITE_ERROR = "sqlite_error"


class QwenReviewStoreError(RuntimeError):
    """Nothing was written (or it was rolled back); ``code`` says why."""

    def __init__(self, code: QwenReviewStoreFailure, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class QwenReviewRow:
    """One finalist of one cycle as submitted to Qwen, and what came back for it.

    ``veto``, ``confidence``, ``review_direction``, ``call_sonnet`` and ``call_fable`` are
    all ``None`` unless the batch is OK and validated a review for this asset.
    """

    run_id: str
    cycle_ts: str
    mode: str
    asset: str
    setup_type: str
    direction: str
    anomaly_score: float | None
    opportunity_score: float | None
    tradeability_score: float | None
    router_decision: str | None
    batch_status: str
    veto: bool | None = None
    confidence: str | None = None
    review_direction: str | None = None
    call_sonnet: bool | None = None
    call_fable: bool | None = None
    elapsed_ms: float | None = None
    attempts: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class StoredQwenReview:
    review_id: int
    row: QwenReviewRow
    recorded_at: str


# --- validation ---------------------------------------------------------------------------------


def _invalid(index: int, field: str, detail: str) -> QwenReviewStoreError:
    return QwenReviewStoreError(QwenReviewStoreFailure.INVALID_ROW, f"row {index} {field}: {detail}")


def _text(index: int, field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(index, field, f"must be a non-empty string, got {value!r}")
    return value


def _choice(index: int, field: str, value: object, allowed: Sequence[str], *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise _invalid(index, field, f"must be one of {tuple(allowed)}, got {value!r}")
    return value


def _number(index: int, field: str, value: object, *, non_negative: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _invalid(index, field, f"must be a finite number or None, got {value!r}")
    if non_negative and value < 0:
        raise _invalid(index, field, f"must not be negative, got {value!r}")
    return float(value)


def _flag(index: int, field: str, value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _invalid(index, field, f"must be a bool or None, got {value!r}")
    return int(value)


def _aware_text(index: int, field: str, value: object) -> str:
    text = _text(index, field, value)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise _invalid(index, field, f"must be ISO-8601 text, got {text!r}") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise _invalid(index, field, f"must carry a UTC offset, got {text!r}")
    return text


def _params(index: int, row: object, recorded_at: str) -> tuple[object, ...]:
    if not isinstance(row, QwenReviewRow):
        raise _invalid(index, "row", f"must be a QwenReviewRow, got {type(row).__name__}")
    status = _choice(index, "batch_status", row.batch_status, BATCH_STATUSES)
    review = (
        _flag(index, "veto", row.veto),
        _choice(index, "confidence", row.confidence, CONFIDENCES, nullable=True),
        _choice(index, "review_direction", row.review_direction, DIRECTIONS, nullable=True),
        _flag(index, "call_sonnet", row.call_sonnet),
        _flag(index, "call_fable", row.call_fable),
    )
    present = [value is not None for value in review]
    if any(present) and not (all(present) and status == "OK"):
        raise _invalid(index, "review", "review fields are all set (batch OK) or all None")
    if isinstance(row.attempts, bool) or not isinstance(row.attempts, int) or row.attempts < 0:
        raise _invalid(index, "attempts", f"must be a non-negative int, got {row.attempts!r}")
    if row.error_code is not None and not isinstance(row.error_code, str):
        raise _invalid(index, "error_code", f"must be a string or None, got {row.error_code!r}")
    return (
        _text(index, "run_id", row.run_id),
        _aware_text(index, "cycle_ts", row.cycle_ts),
        _choice(index, "mode", row.mode, MODES),
        _text(index, "asset", row.asset),
        _choice(index, "setup_type", row.setup_type, SETUP_TYPES),
        _choice(index, "direction", row.direction, DIRECTIONS),
        _number(index, "anomaly_score", row.anomaly_score),
        _number(index, "opportunity_score", row.opportunity_score),
        _number(index, "tradeability_score", row.tradeability_score),
        _choice(index, "router_decision", row.router_decision, ROUTER_DECISIONS, nullable=True),
        status,
        *review,
        _number(index, "elapsed_ms", row.elapsed_ms, non_negative=True),
        row.attempts,
        row.error_code,
        recorded_at,
    )


# --- schema -------------------------------------------------------------------------------------


def _missing_objects(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    present = {
        (str(kind), str(name))
        for kind, name in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE tbl_name = ?", (TABLE,)
        ).fetchall()
    }
    return [obj for obj in SCHEMA_OBJECTS if obj not in present]


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create the table and its triggers if any is missing; ``True`` when something was created.

    A read-only check comes first: an up-to-date database is not written to at all.
    """
    if conn.in_transaction:
        raise QwenReviewStoreError(QwenReviewStoreFailure.OPEN_TRANSACTION, "commit or roll back before ensure_schema")
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
        raise QwenReviewStoreError(QwenReviewStoreFailure.SQLITE_ERROR, f"creating {TABLE}: {error}") from error
    return True


# --- rows ---------------------------------------------------------------------------------------


def record_batch(conn: sqlite3.Connection, rows: Sequence[QwenReviewRow], *, now: datetime) -> int:
    """Write the rows of one batch in one transaction; return how many were inserted.

    A row whose ``(run_id, asset)`` is already stored is skipped without error. An invalid
    row raises ``QwenReviewStoreError`` before anything is written.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise QwenReviewStoreError(QwenReviewStoreFailure.INVALID_ARGUMENT, "now must be a timezone-aware datetime")
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise QwenReviewStoreError(QwenReviewStoreFailure.INVALID_ARGUMENT, "rows must be a sequence of QwenReviewRow")
    recorded_at = now.astimezone(UTC).isoformat(timespec="microseconds")
    params = [_params(index, row, recorded_at) for index, row in enumerate(rows)]
    if not params:
        return 0
    if conn.in_transaction:
        raise QwenReviewStoreError(QwenReviewStoreFailure.OPEN_TRANSACTION, "commit or roll back before record_batch")
    inserted = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for values in params:
                inserted += conn.execute(_INSERT, values).rowcount
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as error:
        raise QwenReviewStoreError(QwenReviewStoreFailure.SQLITE_ERROR, f"writing {TABLE}: {error}") from error
    return inserted


def _malformed(field: str, value: object) -> QwenReviewStoreError:
    return QwenReviewStoreError(QwenReviewStoreFailure.MALFORMED_ROW, f"stored {field} is {value!r}")


def _stored_int(field: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _malformed(field, value)
    return value


def _stored_str(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise _malformed(field, value)
    return value


def _optional_str(field: str, value: object) -> str | None:
    return None if value is None else _stored_str(field, value)


def _optional_bool(field: str, value: object) -> bool | None:
    return None if value is None else bool(_stored_int(field, value))


def _optional_float(field: str, value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _malformed(field, value)
    return float(value)


def reviews_for_run(conn: sqlite3.Connection, run_id: str) -> tuple[StoredQwenReview, ...]:
    """Every stored row of one origin run id, in insertion order."""
    if not isinstance(run_id, str) or not run_id:
        raise QwenReviewStoreError(QwenReviewStoreFailure.INVALID_ARGUMENT, "run_id must be a non-empty string")
    try:
        records = conn.execute(
            f"SELECT id, {', '.join(_COLUMNS)} FROM {TABLE} WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    except sqlite3.Error as error:
        raise QwenReviewStoreError(QwenReviewStoreFailure.SQLITE_ERROR, f"reading {TABLE}: {error}") from error
    out: list[StoredQwenReview] = []
    for record in records:
        value = dict(zip(("id", *_COLUMNS), tuple(record), strict=True))
        row = QwenReviewRow(
            run_id=_stored_str("run_id", value["run_id"]),
            cycle_ts=_stored_str("cycle_ts", value["cycle_ts"]),
            mode=_stored_str("mode", value["mode"]),
            asset=_stored_str("asset", value["asset"]),
            setup_type=_stored_str("setup_type", value["setup_type"]),
            direction=_stored_str("direction", value["direction"]),
            anomaly_score=_optional_float("anomaly_score", value["anomaly_score"]),
            opportunity_score=_optional_float("opportunity_score", value["opportunity_score"]),
            tradeability_score=_optional_float("tradeability_score", value["tradeability_score"]),
            router_decision=_optional_str("router_decision", value["router_decision"]),
            batch_status=_stored_str("batch_status", value["batch_status"]),
            veto=_optional_bool("veto", value["veto"]),
            confidence=_optional_str("confidence", value["confidence"]),
            review_direction=_optional_str("review_direction", value["review_direction"]),
            call_sonnet=_optional_bool("call_sonnet", value["call_sonnet"]),
            call_fable=_optional_bool("call_fable", value["call_fable"]),
            elapsed_ms=_optional_float("elapsed_ms", value["elapsed_ms"]),
            attempts=_stored_int("attempts", value["attempts"]),
            error_code=_optional_str("error_code", value["error_code"]),
        )
        out.append(
            StoredQwenReview(
                review_id=_stored_int("id", value["id"]),
                row=row,
                recorded_at=_stored_str("recorded_at", value["recorded_at"]),
            )
        )
    return tuple(out)
