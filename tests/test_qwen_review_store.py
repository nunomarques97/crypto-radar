"""The append-only qwen_reviews table, created outside the schema-version ledger.

Every database here is a fixture in a fresh temporary directory. The "HEAD" database is
built the way the current store builds it (``store.SCHEMA``, the legacy ``ALTER TABLE``
columns and ledger versions 1-5) with rows in the legacy and ledgered tables, but without
``qwen_reviews``. Nothing reads or copies ``radar_state.sqlite`` or any other root state file.
"""

import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config, qwen, router, setups, store
from radar_v08.adapters import evidence_store as es
from radar_v08.adapters import qwen_review_store as qrs
from radar_v08.adapters.qwen_review_store import (
    QwenReviewRow,
    QwenReviewStoreError,
    QwenReviewStoreFailure,
)
from radar_v08.store import SnapshotStore

UTC = timezone.utc
NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
CYCLE_A = "2026-09-25T11:55:00.123456+00:00"

# Ledger versions 1-5 exactly as HEAD recorded them: this task adds no ledger version.
HEAD_LEDGER = (
    (1, "schema_version_ledger", "sha256:61af0e60d5ef7a253cd55c0bd7c55846d74283ec363bf5db48d9b159ba6a3cc5"),
    (2, "evidence_versions_and_event_links", "sha256:3b1af70cbac9785d6809e419c88f8de474930903918959eab798d3504700579f"),
    (3, "invocations_budget_and_demand", "sha256:24c14fa98b58c1d317f254aa6868db376b09c6f39ff81ca1b3b241d0a8969d31"),
    (4, "lifecycle_outbox_and_cursors", "sha256:c360a99261f637357f2bcb1c209213e3656012bf589c63c97a6940a19f6e75e6"),
    (5, "outcome_subjects_costs_and_labels", "sha256:3e9d8573913e068fdbc2acd54ab9da3e9e70f8d9cc420ffd26723c394d38713d"),
)
QWEN_OBJECTS = {
    ("table", "qwen_reviews"),
    ("index", "sqlite_autoindex_qwen_reviews_1"),
    ("trigger", "qwen_reviews_no_update"),
    ("trigger", "qwen_reviews_no_delete"),
}
LEGACY_TABLES = ("events", "radar_runs", "forward_returns", "alerts", "outcome_subjects")


def review_row(**changes):
    base = QwenReviewRow(
        run_id="run-A",
        cycle_ts=CYCLE_A,
        mode="shadow",
        asset="BTC",
        setup_type="BREAKOUT",
        direction="LONG",
        anomaly_score=71.5,
        opportunity_score=64.0,
        tradeability_score=80.25,
        router_decision="SONNET",
        batch_status="OK",
        veto=False,
        confidence="HIGH",
        review_direction="LONG",
        call_sonnet=True,
        call_fable=False,
        elapsed_ms=2500.0,
        attempts=1,
        error_code=None,
    )
    return replace(base, **changes)


def failed_row(status, error_code, **changes):
    return review_row(
        batch_status=status,
        veto=None,
        confidence=None,
        review_direction=None,
        call_sonnet=None,
        call_fable=None,
        error_code=error_code,
        **changes,
    )


def build_head_db(path):
    """The database the HEAD store leaves behind: legacy schema, ledger 1-5, rows, no qwen_reviews."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(store.SCHEMA)
        for column, sql_type in store._FORWARD_RETURNS_MIGRATION_COLUMNS.items():
            conn.execute(f"ALTER TABLE forward_returns ADD COLUMN {column} {sql_type}")
        for column, sql_type in store._EVENTS_MIGRATION_COLUMNS.items():
            conn.execute(f"ALTER TABLE events ADD COLUMN {column} {sql_type}")
        conn.commit()
        es.apply_schema_migrations(conn, now=NOW - timedelta(days=3))
        conn.execute(
            "INSERT INTO radar_runs (run_id, ts, mode, markets_seen, data_quality_json) "
            "VALUES ('run-old', '2026-09-24T10:00:00+00:00', 'full', 12, '{\"qwen\": \"OK\"}')"
        )
        conn.execute(
            "INSERT INTO events (event_id, dedup_key, ts, type, asset, setup_type, direction, market, anomaly_score, "
            "status, context_json, updated_ts) VALUES ('evt-old', 'k1', '2026-09-24T10:05:00+00:00', 'RADAR_ALERT', "
            "'BTC', 'BREAKOUT', 'LONG', 'SPOT', 70.0, 'PENDING', '{\"qwen\": {\"veto\": false}}', "
            "'2026-09-24T10:06:00+00:00')"
        )
        conn.execute(
            "INSERT INTO forward_returns (asset, ts, horizon_minutes, return_pct, pair) "
            "VALUES ('BTC', '2026-09-24T10:05:00+00:00', 15, 0.4, 'BTC/USD')"
        )
        conn.execute(
            "INSERT INTO forward_returns (asset, ts, horizon_minutes, return_pct, pair, unlabelable_reason) "
            "VALUES ('ETH', '2026-09-20T10:05:00+00:00', 60, NULL, 'ETH/USD', 'target_outside_snapshot_retention')"
        )
        conn.execute(
            "INSERT INTO alerts (run_id, asset, ts, anomaly_score, warmup, flags_json) "
            "VALUES ('run-old', 'BTC', '2026-09-24T10:05:00+00:00', 70.0, 0, '[\"VOLUME_SPIKE\"]')"
        )
        conn.execute(
            "INSERT INTO outcome_subjects (subject_id, policy_version, venue, symbol, instrument_kind, base, quote, "
            "size_unit, pair, direction, decision_as_of, entry_mid, entry_observed_at, evidence_missing, "
            "decision_missing, arm_missing, registered_at) VALUES ('subj-old', 'OUT-1', 'kraken', 'XBT/USD', 'spot', "
            "'BTC', 'USD', 'BTC', 'XBTUSD', 'LONG', '2026-09-24T10:05:00.000000+00:00', '50000.5', "
            "'2026-09-24T10:04:59.000000+00:00', 'not_recorded', 'not_recorded', 'not_recorded', "
            "'2026-09-24T10:05:01.000000+00:00')"
        )
        conn.commit()
    finally:
        conn.close()


def table_rows(path, tables=LEGACY_TABLES):
    conn = sqlite3.connect(path)
    try:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
    finally:
        conn.close()


def snapshot(path):
    """Full logical content plus SQLite's schema cookie: equal means nothing changed."""
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump()), conn.execute("PRAGMA schema_version").fetchone()[0]
    finally:
        conn.close()


def objects(path):
    conn = sqlite3.connect(path)
    try:
        return set(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
    finally:
        conn.close()


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "fixture.sqlite")
        self.assertNotEqual(os.path.abspath(self.path), os.path.abspath(config.SQLITE_PATH))
        self._closers = []

    def tearDown(self):
        for close in reversed(self._closers):
            close()

    def open_store(self):
        s = SnapshotStore(self.path)
        self._closers.append(s.close)
        return s

    def connect(self):
        conn = sqlite3.connect(self.path)
        self._closers.append(conn.close)
        return conn

    def count(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute("SELECT COUNT(*) FROM qwen_reviews").fetchone()[0]
        finally:
            conn.close()


class TestAdditiveCreation(TempDbCase):
    def setUp(self):
        super().setUp()
        build_head_db(self.path)

    def test_head_database_has_ledger_1_to_5_and_no_review_table(self):
        conn = self.connect()
        self.assertEqual(tuple((e.version, e.name, e.checksum) for e in es.read_ledger(conn)), HEAD_LEDGER)
        self.assertEqual(es.pending_migrations(conn), ())
        self.assertNotIn("qwen_reviews", {name for _, name, _ in objects(self.path)})
        self.assertTrue(all(table_rows(self.path).values()))  # every legacy table holds rows

    def test_opening_adds_only_the_review_objects_and_keeps_every_row(self):
        before_objects = objects(self.path)
        before_rows = table_rows(self.path)
        before_dump, _ = snapshot(self.path)

        self.open_store()

        after_objects = objects(self.path)
        self.assertTrue(before_objects <= after_objects)  # nothing altered or dropped
        self.assertEqual({(kind, name) for kind, name, _ in after_objects - before_objects}, QWEN_OBJECTS)
        self.assertEqual(table_rows(self.path), before_rows)  # full rows, same counts
        after_dump, _ = snapshot(self.path)
        old_inserts = [line for line in before_dump if line.startswith("INSERT INTO")]
        new_inserts = [line for line in after_dump if line.startswith("INSERT INTO")]
        self.assertEqual(new_inserts, old_inserts)  # no row added, changed or removed anywhere
        self.assertEqual(self.count(), 0)

    def test_ledger_stays_at_versions_1_to_5_and_head_code_sees_nothing_pending(self):
        s = self.open_store()
        self.assertEqual(tuple((e.version, e.name, e.checksum) for e in s.schema_ledger()), HEAD_LEDGER)
        self.assertEqual(
            tuple((m.version, m.name, m.checksum) for m in es.SCHEMA_MIGRATIONS), HEAD_LEDGER
        )
        conn = self.connect()
        self.assertEqual(es.pending_migrations(conn), ())
        changes = conn.total_changes
        self.assertEqual(es.apply_schema_migrations(conn, now=NOW), ())
        self.assertEqual(conn.total_changes, changes)
        self.assertEqual([row[0] for row in conn.execute("SELECT version FROM schema_version_ledger")], [1, 2, 3, 4, 5])

    def test_reopening_is_a_no_op(self):
        self.open_store().close()
        self._closers.clear()
        first = snapshot(self.path)
        s = self.open_store()
        self.assertEqual(s.migrate_schema(), ())
        s.close()
        self._closers.clear()
        self.assertEqual(snapshot(self.path), first)  # same dump and schema cookie

        conn = self.connect()
        changes = conn.total_changes
        self.assertFalse(qrs.ensure_schema(conn))
        self.assertEqual(conn.total_changes, changes)
        self.assertFalse(conn.in_transaction)
        self.assertEqual(snapshot(self.path), first)

    def test_rows_survive_reopening_and_head_ledger_check(self):
        s = self.open_store()
        self.assertEqual(s.record_qwen_reviews([review_row()], now=NOW), 1)
        s.close()
        self._closers.clear()
        s = self.open_store()
        self.assertEqual([r.row for r in s.qwen_reviews_for_run("run-A")], [review_row()])
        self.assertEqual(es.pending_migrations(self.connect()), ())

    def test_fresh_database_gets_the_table_too(self):
        path = os.path.join(self.tmp.name, "fresh.sqlite")
        s = SnapshotStore(path)
        self._closers.append(s.close)
        self.assertEqual({(k, n) for k, n, _ in objects(path) if n.startswith(("qwen_", "sqlite_autoindex_qwen"))}, QWEN_OBJECTS)
        self.assertEqual([e.version for e in s.schema_ledger()], [1, 2, 3, 4, 5])

    def test_partial_objects_are_completed(self):
        conn = self.connect()
        conn.execute(qrs.SCHEMA_STATEMENTS[0])
        conn.commit()
        self.assertTrue(qrs.ensure_schema(conn))
        names = {name for _, name, _ in objects(self.path)}
        self.assertTrue({"qwen_reviews_no_update", "qwen_reviews_no_delete"} <= names)
        self.assertFalse(qrs.ensure_schema(conn))


class TestSchemaStatements(unittest.TestCase):
    def test_only_create_if_not_exists_on_the_new_table(self):
        for statement in qrs.SCHEMA_STATEMENTS:
            with self.subTest(statement=statement[:50]):
                upper = " ".join(statement.upper().split())
                self.assertRegex(upper, r"^CREATE (TABLE|INDEX|TRIGGER) IF NOT EXISTS QWEN_REVIEWS")
                self.assertNotRegex(upper, r"\bALTER\b|\bDROP\b|\bINSERT\b|\bDELETE\s+FROM\b|\bUPDATE\s+\w+\s+SET\b")
                referenced = set(re.findall(r"\bON (\w+)", upper)) | set(re.findall(r"EXISTS (\w+)", upper))
                self.assertLessEqual(
                    referenced, {"QWEN_REVIEWS", "QWEN_REVIEWS_NO_UPDATE", "QWEN_REVIEWS_NO_DELETE", "CONFLICT"}
                )

    def test_enums_match_the_radar(self):
        self.assertEqual(qrs.SETUP_TYPES, setups.SETUP_TYPES)
        self.assertEqual(qrs.SETUP_TYPES, qwen.SETUP_TYPES)
        self.assertEqual(qrs.DIRECTIONS, setups.DIRECTIONS)
        self.assertEqual(qrs.DIRECTIONS, qwen.DIRECTIONS)
        self.assertEqual(qrs.CONFIDENCES, qwen.CONFIDENCES)
        self.assertEqual(qrs.ROUTER_DECISIONS, router.DECISIONS)


class TestRecordBatch(TempDbCase):
    def setUp(self):
        super().setUp()
        self.store = self.open_store()

    def test_ok_batch_round_trips_ignore_finalists_included(self):
        rows = [
            review_row(),
            review_row(asset="ETH", direction="SHORT", review_direction="SHORT", router_decision="IGNORE",
                       veto=True, confidence="LOW", call_sonnet=False),
            review_row(asset="SOL", setup_type="EXHAUSTION", direction="NONE", review_direction="NONE",
                       router_decision=None, anomaly_score=None, call_fable=True),
        ]
        self.assertEqual(self.store.record_qwen_reviews(rows, now=NOW), 3)
        stored = self.store.qwen_reviews_for_run("run-A")
        self.assertEqual([r.row for r in stored], rows)
        self.assertEqual({r.recorded_at for r in stored}, {"2026-09-25T12:00:00.000000+00:00"})
        self.assertEqual(len({r.review_id for r in stored}), 3)
        self.assertIs(stored[1].row.veto, True)
        self.assertIsNone(stored[2].row.router_decision)

    def test_timeout_schema_failure_and_error_rows_keep_review_fields_null(self):
        rows = [
            failed_row("TIMEOUT", "deadline_exceeded", asset="BTC", elapsed_ms=30000.0, attempts=1),
            failed_row("UNAVAILABLE", "schema_invalid", asset="ETH", elapsed_ms=5000.0, attempts=2),
            failed_row("INVALID_JSON", "invalid_json", asset="SOL", attempts=2),
            failed_row("ERROR", "RuntimeError", asset="ADA", elapsed_ms=None, attempts=0),
        ]
        self.assertEqual(self.store.record_qwen_reviews(rows, now=NOW), 4)
        stored = [r.row for r in self.store.qwen_reviews_for_run("run-A")]
        self.assertEqual(stored, rows)
        for row in stored:
            self.assertEqual((row.veto, row.confidence, row.review_direction, row.call_sonnet, row.call_fable), (None,) * 5)

    def test_ok_batch_without_a_review_for_the_asset_is_stored_null(self):
        row = failed_row("OK", None)
        self.assertEqual(self.store.record_qwen_reviews([row], now=NOW), 1)
        self.assertEqual(self.store.qwen_reviews_for_run("run-A")[0].row, row)

    def test_repeated_run_and_asset_adds_nothing_and_raises_nothing(self):
        self.assertEqual(self.store.record_qwen_reviews([review_row()], now=NOW), 1)
        first = self.store.qwen_reviews_for_run("run-A")
        self.assertEqual(self.store.record_qwen_reviews([review_row(veto=True)], now=NOW + timedelta(minutes=5)), 0)
        self.assertEqual(self.store.qwen_reviews_for_run("run-A"), first)  # first row kept as written
        mixed = [review_row(), review_row(asset="ETH")]
        self.assertEqual(self.store.record_qwen_reviews(mixed, now=NOW), 1)
        same_batch = [review_row(asset="XRP"), review_row(asset="XRP", veto=True)]
        self.assertEqual(self.store.record_qwen_reviews(same_batch, now=NOW), 1)
        self.assertEqual(self.count(), 3)

    def test_same_asset_in_another_run_is_a_new_row(self):
        self.store.record_qwen_reviews([review_row()], now=NOW)
        self.store.record_qwen_reviews([review_row(run_id="run-B", cycle_ts="2026-09-25T12:00:00+00:00")], now=NOW)
        self.assertEqual([r.row.run_id for r in self.store.qwen_reviews_for_run("run-A")], ["run-A"])
        self.assertEqual([r.row.cycle_ts for r in self.store.qwen_reviews_for_run("run-B")], ["2026-09-25T12:00:00+00:00"])
        self.assertEqual(self.store.qwen_reviews_for_run("run-missing"), ())

    def test_empty_batch_writes_nothing(self):
        before = snapshot(self.path)
        self.assertEqual(self.store.record_qwen_reviews([], now=NOW), 0)
        self.assertEqual(snapshot(self.path), before)

    def test_invalid_values_raise_a_typed_error_and_write_nothing(self):
        invalid = {
            "mode": review_row(mode="off"),
            "setup_type": review_row(setup_type="PUMP"),
            "direction": review_row(direction="long"),
            "router_decision": review_row(router_decision="WATCH"),
            "batch_status": review_row(batch_status="SKIPPED"),
            "confidence": review_row(confidence="VERY_HIGH"),
            "review_direction": review_row(review_direction="UP"),
            "veto": review_row(veto=1),
            "call_fable": review_row(call_fable="yes"),
            "anomaly_score": review_row(anomaly_score=math.nan),
            "opportunity_score": review_row(opportunity_score=True),
            "tradeability_score": review_row(tradeability_score="80"),
            "elapsed_ms": review_row(elapsed_ms=-1.0),
            "attempts": review_row(attempts=-1),
            "attempts_bool": review_row(attempts=True),
            "error_code": review_row(error_code=7),
            "run_id": review_row(run_id=""),
            "asset": review_row(asset=" "),
            "cycle_ts_naive": review_row(cycle_ts="2026-09-25T11:55:00"),
            "cycle_ts_text": review_row(cycle_ts="yesterday"),
            "partial_review": review_row(confidence=None),
            "review_on_timeout": review_row(batch_status="TIMEOUT"),
            "not_a_row": {"run_id": "run-A"},
        }
        for name, bad in invalid.items():
            with self.subTest(name):
                with self.assertRaises(QwenReviewStoreError) as caught:
                    self.store.record_qwen_reviews([review_row(asset="GOOD"), bad], now=NOW)
                self.assertEqual(caught.exception.code, QwenReviewStoreFailure.INVALID_ROW)
                self.assertEqual(self.count(), 0)

    def test_invalid_arguments(self):
        with self.assertRaises(QwenReviewStoreError) as caught:
            self.store.record_qwen_reviews([review_row()], now=datetime(2026, 9, 25, 12, 0))
        self.assertEqual(caught.exception.code, QwenReviewStoreFailure.INVALID_ARGUMENT)
        with self.assertRaises(QwenReviewStoreError) as caught:
            self.store.qwen_reviews_for_run("")
        self.assertEqual(caught.exception.code, QwenReviewStoreFailure.INVALID_ARGUMENT)
        self.assertEqual(self.count(), 0)

    def test_failure_mid_batch_rolls_back_every_row(self):
        conn = self.connect()
        conn.execute(
            "CREATE TRIGGER test_refuse_boom BEFORE INSERT ON qwen_reviews WHEN NEW.asset = 'BOOM' "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
        conn.commit()
        rows = [review_row(), review_row(asset="ETH"), review_row(asset="BOOM")]
        with self.assertRaises(QwenReviewStoreError) as caught:
            self.store.record_qwen_reviews(rows, now=NOW)
        self.assertEqual(caught.exception.code, QwenReviewStoreFailure.SQLITE_ERROR)
        self.assertIn("injected failure", caught.exception.detail)
        self.assertEqual(self.count(), 0)
        # The store stays usable: the next batch commits normally.
        self.assertEqual(self.store.record_qwen_reviews(rows[:2], now=NOW), 2)

    def test_open_transaction_is_refused(self):
        conn = self.connect()
        conn.execute("BEGIN")
        with self.assertRaises(QwenReviewStoreError) as caught:
            qrs.record_batch(conn, [review_row()], now=NOW)
        self.assertEqual(caught.exception.code, QwenReviewStoreFailure.OPEN_TRANSACTION)
        with self.assertRaises(QwenReviewStoreError) as caught:
            qrs.ensure_schema(conn)
        self.assertEqual(caught.exception.code, QwenReviewStoreFailure.OPEN_TRANSACTION)
        conn.rollback()

    def test_concurrent_writers_through_one_store_lose_nothing(self):
        start = threading.Event()
        errors = []

        def writer(n):
            start.wait(10)
            try:
                for batch in range(5):
                    rows = [review_row(run_id=f"run-{n}-{batch}", asset=a) for a in ("BTC", "ETH")]
                    self.store.record_qwen_reviews(rows, now=NOW)
            except BaseException as exc:  # recorded and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(30)
            self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.count(), 4 * 5 * 2)


class TestAppendOnly(TempDbCase):
    def setUp(self):
        super().setUp()
        self.store = self.open_store()
        self.store.record_qwen_reviews([review_row()], now=NOW)
        self.conn = self.connect()

    def test_update_and_delete_abort(self):
        for sql in ("UPDATE qwen_reviews SET veto = 1", "DELETE FROM qwen_reviews"):
            with self.subTest(sql):
                with self.assertRaises(sqlite3.DatabaseError):
                    self.conn.execute(sql)
                self.conn.rollback()
        self.assertEqual([r.row for r in self.store.qwen_reviews_for_run("run-A")], [review_row()])

    def test_check_constraints_back_up_the_validation(self):
        columns = ("run_id", "cycle_ts", "mode", "asset", "setup_type", "direction", "batch_status", "attempts",
                   "recorded_at")
        good = {"run_id": "run-X", "cycle_ts": CYCLE_A, "mode": "inline", "asset": "BTC", "setup_type": "NONE",
                "direction": "NONE", "batch_status": "TIMEOUT", "attempts": 0, "recorded_at": CYCLE_A}
        sql = f"INSERT INTO qwen_reviews ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
        self.conn.execute(sql, tuple(good[c] for c in columns))
        self.conn.rollback()
        bad_values = {
            "mode": "off", "setup_type": "PUMP", "direction": "UP", "batch_status": "SKIPPED", "attempts": -1,
            "run_id": "", "asset": "",
        }
        for column, value in bad_values.items():
            with self.subTest(column):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(sql, tuple(value if c == column else good[c] for c in columns))
                self.conn.rollback()
        extras = {
            "router_decision = 'WATCH'": ("router_decision", "WATCH"),
            "veto = 2": ("veto", 2),
            "elapsed_ms < 0": ("elapsed_ms", -5.0),
            "review with TIMEOUT": ("veto", 0),
        }
        for label, (column, value) in extras.items():
            with self.subTest(label):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(
                        f"INSERT INTO qwen_reviews ({', '.join(columns)}, {column}) "
                        f"VALUES ({', '.join('?' for _ in columns)}, ?)",
                        (*[good[c] for c in columns], value),
                    )
                self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
