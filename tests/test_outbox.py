"""T033a: atomic lifecycle transition + outbox in SQLite, cursors, dedup, restart and JSONL export.

Every database and every JSONL file is a fixture in a fresh temporary directory, and
``config.EVENTS_LOG_PATH`` is patched to a temporary file in every case. Nothing opens,
reads or copies ``radar_state.sqlite``, and nothing writes the root ``events.jsonl`` or
``runs.jsonl``. No network, model, UI or notification: the pipeline test uses the fake
Kraken harness of ``test_integrity_wiring`` and the fake model of ``test_invocation_wiring``.
"""

import ast
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, TESTS_DIR)

import test_integrity_wiring as wiring  # noqa: E402  (fake Kraken heartbeat harness)
import test_invocation_wiring as bridge_wiring  # noqa: E402  (fake model, T010 guard lift)

from radar_v08 import claude_bridge, config, store  # noqa: E402
from radar_v08 import events as events_module  # noqa: E402
from radar_v08.adapters import evidence_store as es  # noqa: E402
from radar_v08.adapters import outbox_store as obx  # noqa: E402
from radar_v08.adapters.evidence_store import (  # noqa: E402
    OUTBOX_MIGRATION,
    SCHEMA_MIGRATIONS,
    MigrationFailure,
    SchemaMigrationError,
)
from radar_v08.adapters.outbox_store import (  # noqa: E402
    Handoff,
    HandoffType,
    LifecycleState,
    OutboxError,
    OutboxFailure,
    OutboxKind,
)
from radar_v08.events import (  # noqa: E402
    create_event_if_new,
    snapshot_to_jsonl,
    transition_event,
)
from radar_v08.store import SnapshotStore, StoreError  # noqa: E402
from radar_v08.workflow.scheduler import ItemState  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 9, 19, 10, 0, 0, tzinfo=UTC)
NEW_TABLES = {"lifecycle_items", "outbox", "outbox_cursors"}
LEGACY_DEDUP = "BTC|BREAKOUT|LONG|qwen"


def build_legacy_db(path):
    """A pre-ledger database with legacy duplicates in ``events``: one dedup_key PENDING twice."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(store.SCHEMA)
        for column, sql_type in store._FORWARD_RETURNS_MIGRATION_COLUMNS.items():
            conn.execute(f"ALTER TABLE forward_returns ADD COLUMN {column} {sql_type}")
        for column, sql_type in store._EVENTS_MIGRATION_COLUMNS.items():
            conn.execute(f"ALTER TABLE events ADD COLUMN {column} {sql_type}")
        for event_id in ("evt-dup-1", "evt-dup-2"):
            conn.execute(
                "INSERT INTO events (event_id, dedup_key, ts, type, asset, setup_type, direction, status) "
                "VALUES (?, ?, '2026-09-01T00:05:00+00:00', 'RADAR_ALERT', 'BTC', 'BREAKOUT', 'LONG', 'PENDING')",
                (event_id, LEGACY_DEDUP),
            )
        conn.execute(
            "INSERT INTO radar_runs (run_id, ts, mode, markets_seen) VALUES ('run-legacy', '2026-09-01T00:00:00+00:00', "
            "'full', 12)"
        )
        conn.commit()
    finally:
        conn.close()


def snapshot(path):
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump()), conn.execute("PRAGMA schema_version").fetchone()[0]
    finally:
        conn.close()


def legacy_index_lists(path):
    """Every index (unique flag included, autoindexes too) of every table that is not new."""
    conn = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            if row[0] not in NEW_TABLES
        ]
        return {table: sorted(tuple(row) for row in conn.execute(f"PRAGMA index_list({table})")) for table in tables}
    finally:
        conn.close()


def same_tables(before, after):
    return {table: after.get(table) for table in before}


def event_kwargs(**overrides):
    base = dict(
        ts="2026-09-19T09:59:00+00:00", type_="RADAR_ALERT", asset="BTC", setup_type="BREAKOUT", direction="LONG",
        market="SPOT", anomaly_score=70.0, opportunity_score=80.0, tradeability_score=85.0, confidence="HIGH",
        model_demand="FABLE", reason="test", status="PENDING",
    )
    base.update(overrides)
    return base


def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "rb") as handle:
        return [line for line in handle.read().split(b"\n") if line.strip()]


def parsed_lines(path):
    return [json.loads(line.decode("utf-8")) for line in read_lines(path)]


def handoff(**changes):
    fields = dict(
        communication_id="comm-1",
        run_id="run-1",
        opportunity_id="opp-1",
        sender="controller",
        receiver="screener",
        handoff_type=HandoffType.DISPATCH,
        reason="deadline_fits",
        at=T0,
        invocation_id="inv-1",
        evidence_hash="sha256:" + "a" * 64,
    )
    fields.update(changes)
    return Handoff(**fields)


class TempCase(unittest.TestCase):
    """A disposable folder with a store, its own JSONL log, and the default log path patched to it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crypto-radar-t033a-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "state.sqlite")
        self.log = os.path.join(self.tmp, "events.jsonl")
        patcher = mock.patch.object(config, "EVENTS_LOG_PATH", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._stores = []
        self._conns = []

    def tearDown(self):
        for conn in self._conns:
            conn.close()
        for opened in self._stores:
            opened.close()

    def open_store(self):
        opened = SnapshotStore(self.path)
        self._stores.append(opened)
        return opened

    def close_store(self, opened):
        opened.close()
        self._stores.remove(opened)

    def connect(self, **kwargs):
        conn = sqlite3.connect(self.path, check_same_thread=False, **kwargs)
        self._conns.append(conn)
        return conn

    def forget(self, conn):
        conn.close()
        self._conns.remove(conn)

    def query(self, sql, *params):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def outbox(self):
        return self.query("SELECT seq, delivery_id, kind, subject_id, state, sender, receiver FROM outbox ORDER BY seq")

    def status(self, event_id):
        return self.query("SELECT status FROM events WHERE event_id = ?", event_id)[0][0]


# --- migration v4 (D19) -------------------------------------------------------------------------


class TestMigrationV4(TempCase):
    def test_v4_is_the_fourth_version_of_the_ledger_plan(self):
        # T041 appended version 5 after it; v4 itself is unchanged.
        self.assertIs(SCHEMA_MIGRATIONS[3], OUTBOX_MIGRATION)
        self.assertEqual(OUTBOX_MIGRATION.version, 4)
        self.assertEqual(OUTBOX_MIGRATION.name, "lifecycle_outbox_and_cursors")

    def test_statements_are_create_only_on_new_tables(self):
        for statement in OUTBOX_MIGRATION.statements:
            upper = statement.upper()
            self.assertTrue(statement.startswith("CREATE "), statement[:40])
            self.assertNotIn("IF NOT EXISTS", upper)
            self.assertNotIn("ALTER", upper)
            self.assertIsNone(re.search(r"DROP", upper))
            self.assertNotIn("INSERT", upper)
            self.assertNotIn("REFERENCES", upper)
            # UPDATE / DELETE appear only as trigger events, never as statements.
            self.assertEqual(len(re.findall(r"\bUPDATE\b", upper)), len(re.findall(r"\bBEFORE UPDATE\b", upper)))
            self.assertEqual(len(re.findall(r"\bDELETE\b", upper)), len(re.findall(r"\bBEFORE DELETE\b", upper)))
            for target in re.findall(r"\bON\s+(\w+)", statement):
                self.assertIn(target, NEW_TABLES, statement[:60])
            self.assertNotIn("CREATE UNIQUE INDEX", upper)

    def test_legacy_duplicates_in_events_open_without_error(self):
        build_legacy_db(self.path)
        before_indexes = legacy_index_lists(self.path)
        legacy_dump, _ = snapshot(self.path)
        opened = self.open_store()
        self.assertEqual([entry.version for entry in opened.schema_ledger()], [1, 2, 3, 4, 5])
        dupes = self.query(
            "SELECT event_id FROM events WHERE dedup_key = ? AND status = 'PENDING' ORDER BY 1", LEGACY_DEDUP
        )
        self.assertEqual(dupes, [("evt-dup-1",), ("evt-dup-2",)])
        # No index, unique or not, was added to (or removed from) any legacy table, events included.
        self.assertEqual(same_tables(before_indexes, legacy_index_lists(self.path)), before_indexes)
        self.assertIn("events", before_indexes)
        unique_on_new = [
            table
            for table in NEW_TABLES
            for row in self.query(f"PRAGMA index_list({table})")
            if row[2] == 1
        ]
        self.assertEqual(sorted(unique_on_new), ["lifecycle_items", "outbox", "outbox_cursors"])
        # Every legacy row survives byte for byte, and nothing is backfilled into the outbox.
        new_dump, _ = snapshot(self.path)
        legacy_rows = [line for line in legacy_dump if line.startswith("INSERT INTO")]
        self.assertEqual(len(legacy_rows), 3)
        for line in legacy_rows:
            self.assertIn(line, new_dump)
        self.assertEqual(self.outbox(), [])
        # A legacy duplicate can still move: its change and its outbox row commit together.
        opened.mark_event_processed("evt-dup-1", T0.isoformat())
        self.assertEqual(
            self.outbox(), [(1, "event:evt-dup-1:1", "EVENT", "evt-dup-1", "PROCESSED", None, None)]
        )
        self.assertEqual(self.status("evt-dup-2"), "PENDING")

    def test_version_4_is_recorded_with_its_checksum(self):
        build_legacy_db(self.path)
        conn = self.connect()
        self.assertEqual(es.apply_schema_migrations(conn, now=T0), (1, 2, 3, 4, 5))
        entry = es.read_ledger(conn)[3]
        self.assertEqual((entry.version, entry.name), (4, "lifecycle_outbox_and_cursors"))
        self.assertEqual(entry.checksum, OUTBOX_MIGRATION.checksum)
        self.assertEqual(entry.applied_at, "2026-09-19T10:00:00+00:00")

    def test_v3_database_upgrades_to_v4_only(self):
        build_legacy_db(self.path)
        conn = self.connect()
        self.assertEqual(es.apply_schema_migrations(conn, now=T0, migrations=SCHEMA_MIGRATIONS[:3]), (1, 2, 3))
        self.assertEqual(
            es.apply_schema_migrations(conn, now=T0 + timedelta(days=1), migrations=SCHEMA_MIGRATIONS[:4]), (4,)
        )
        self.assertEqual([entry.version for entry in es.read_ledger(conn)], [1, 2, 3, 4])

    def test_second_open_writes_nothing(self):
        build_legacy_db(self.path)
        self.close_store(self.open_store())
        first = snapshot(self.path)
        conn = self.connect()
        changes = conn.total_changes
        self.assertEqual(es.apply_schema_migrations(conn, now=T0 + timedelta(days=1)), ())
        self.assertEqual(conn.total_changes, changes)
        self.forget(conn)
        again = self.open_store()
        self.assertEqual(again.migrate_schema(), ())
        self.close_store(again)
        self.assertEqual(snapshot(self.path), first)

    def test_injected_failure_in_v4_rolls_back_everything(self):
        # outbox_cursors is statement 9 of 11: lifecycle_items, outbox, their index and
        # triggers are created first inside the transaction, then the name clash aborts.
        build_legacy_db(self.path)
        conn = self.connect()
        es.apply_schema_migrations(conn, now=T0, migrations=SCHEMA_MIGRATIONS[:3])
        conn.execute("CREATE TABLE outbox_cursors (unrelated TEXT)")
        conn.commit()
        self.forget(conn)
        before = snapshot(self.path)
        with self.assertRaises(SchemaMigrationError) as caught:
            SnapshotStore(self.path)
        self.assertEqual(caught.exception.code, MigrationFailure.STATEMENT_FAILED)
        self.assertEqual(caught.exception.version, 4)
        self.assertIn("statement 9/11", caught.exception.detail)
        self.assertEqual(snapshot(self.path), before)
        self.assertEqual([row[0] for row in self.query("SELECT version FROM schema_version_ledger ORDER BY 1")], [1, 2, 3])
        names = {row[0] for row in self.query("SELECT name FROM sqlite_master")}
        self.assertNotIn("lifecycle_items", names)
        self.assertNotIn("outbox", names)

    def test_outbox_calls_refuse_an_unmigrated_database(self):
        build_legacy_db(self.path)
        conn = self.connect()
        es.apply_schema_migrations(conn, now=T0, migrations=SCHEMA_MIGRATIONS[:3])
        with self.assertRaises(OutboxError) as caught:
            obx.record_lifecycle_transition(conn, "item-1", LifecycleState.QUEUED, now=T0)
        self.assertEqual(caught.exception.code, OutboxFailure.SCHEMA_NOT_MIGRATED)
        self.assertFalse(conn.in_transaction)


# --- atomic transition + outbox row -------------------------------------------------------------


class TestAtomicTransition(TempCase):
    def fail_outbox_inserts(self, opened):
        opened._conn.execute(
            "CREATE TEMP TRIGGER t033a_crash BEFORE INSERT ON main.outbox "
            "BEGIN SELECT RAISE(ABORT, 'injected crash before the outbox row'); END"
        )

    def test_event_change_and_its_outbox_row_commit_together(self):
        opened = self.open_store()
        event_id, created = create_event_if_new(opened, **event_kwargs())
        self.assertTrue(created)
        opened.mark_event_processed(event_id, T0.isoformat())
        self.assertEqual(
            self.outbox(),
            [
                (1, f"event:{event_id}:1", "EVENT", event_id, "PENDING", None, None),
                (2, f"event:{event_id}:2", "EVENT", event_id, "PROCESSED", None, None),
            ],
        )
        # The payload is the row exactly as it was committed with the change.
        second = opened.outbox_entries(after=1)[0].payload()
        self.assertEqual(second, dict(opened.get_event(event_id)))
        self.assertEqual((second["status"], second["updated_ts"]), ("PROCESSED", T0.isoformat()))

    def test_a_failed_outbox_insert_rolls_back_the_event_change(self):
        opened = self.open_store()
        event_id, _ = create_event_if_new(opened, **event_kwargs())
        self.fail_outbox_inserts(opened)
        for change in (
            lambda: opened.mark_event_processed(event_id, T0.isoformat()),
            lambda: opened.mark_event_failed(event_id, T0.isoformat(), "x"),
            lambda: opened.claim_event_for_processing(event_id, T0.isoformat()),
            lambda: opened.update_event_status(event_id, "PROCESSING"),
            lambda: opened.mark_event_notified(event_id, T0.isoformat()),
        ):
            with self.assertRaises(StoreError):
                change()
            self.assertFalse(opened._conn.in_transaction)
        self.assertEqual(self.query("SELECT status, notified FROM events"), [("PENDING", 0)])
        self.assertEqual(len(self.outbox()), 1)

    def test_a_failed_outbox_insert_rolls_back_the_event_creation(self):
        opened = self.open_store()
        self.fail_outbox_inserts(opened)
        with self.assertRaises(StoreError):
            create_event_if_new(opened, **event_kwargs())
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(0,)])
        self.assertEqual(self.outbox(), [])
        self.assertEqual(read_lines(self.log), [])

    def test_a_refused_outbox_row_rolls_back_too(self):
        opened = self.open_store()
        event_id, _ = create_event_if_new(opened, **event_kwargs())
        refused = OutboxError(OutboxFailure.STORAGE_ERROR, "injected")
        with mock.patch.object(obx, "record_event_row", side_effect=refused):
            with self.assertRaises(StoreError) as caught:
                opened.mark_event_processed(event_id, T0.isoformat())
        self.assertIsInstance(caught.exception.__cause__, OutboxError)
        self.assertFalse(opened._conn.in_transaction)
        self.assertEqual(self.status(event_id), "PENDING")

    def test_no_change_means_no_outbox_row(self):
        opened = self.open_store()
        event_id, _ = create_event_if_new(opened, **event_kwargs(status="PROCESSED"))
        self.assertFalse(opened.claim_event_for_processing(event_id, T0.isoformat()))
        opened.mark_event_processed("no-such-event", T0.isoformat())
        self.assertEqual(len(self.outbox()), 1)

    def test_event_row_must_share_the_callers_transaction(self):
        opened = self.open_store()
        event_id, _ = create_event_if_new(opened, **event_kwargs())
        conn = self.connect()
        with self.assertRaises(OutboxError) as caught:
            obx.record_event_row(conn, event_id, now=T0)
        self.assertEqual(caught.exception.code, OutboxFailure.NO_TRANSACTION)
        conn.execute("UPDATE events SET status = 'FAILED' WHERE event_id = ?", (event_id,))
        with self.assertRaises(OutboxError) as caught:
            obx.record_event_row(conn, "no-such-event", now=T0)
        self.assertEqual(caught.exception.code, OutboxFailure.UNKNOWN_EVENT)
        conn.rollback()
        self.assertEqual(self.status(event_id), "PENDING")

    def test_lifecycle_state_and_outbox_row_commit_together(self):
        opened = self.open_store()
        opened.record_lifecycle_transition("item-1", LifecycleState.QUEUED, now=T0)
        opened._conn.execute(
            "CREATE TEMP TRIGGER t033a_crash BEFORE INSERT ON main.outbox BEGIN SELECT RAISE(ABORT, 'crash'); END"
        )
        with self.assertRaises(OutboxError) as caught:
            opened.record_lifecycle_transition("item-1", LifecycleState.RUNNING, now=T0)
        self.assertEqual(caught.exception.code, OutboxFailure.STORAGE_ERROR)
        self.assertEqual(self.query("SELECT state FROM lifecycle_items"), [("QUEUED",)])
        opened._conn.execute("DROP TRIGGER temp.t033a_crash")
        opened._conn.execute(
            "CREATE TEMP TRIGGER t033a_state BEFORE UPDATE ON main.lifecycle_items BEGIN SELECT RAISE(ABORT, 'x'); END"
        )
        with self.assertRaises(OutboxError):
            opened.record_lifecycle_transition("item-1", LifecycleState.RUNNING, now=T0)
        self.assertEqual([row[1] for row in self.outbox()], ["lifecycle:item-1:QUEUED"])
        self.assertEqual(opened.lifecycle_state("item-1"), LifecycleState.QUEUED)


# --- lifecycle states, distinct from handoffs ---------------------------------------------------


class TestLifecycleStates(TempCase):
    def test_real_states_are_separate_and_include_the_t032_outcomes(self):
        self.assertEqual(
            {state.value for state in LifecycleState},
            {"QUEUED", "LOADING", "RUNNING", "FINISHED", "FAILED", "ABORT_STALE", "SUPERSEDED", "DROPPED_BACKPRESSURE"},
        )
        for name in ("QUEUED", "RUNNING", "ABORT_STALE", "SUPERSEDED", "DROPPED_BACKPRESSURE"):
            self.assertEqual(LifecycleState(ItemState[name].value).value, name)
        self.assertNotIn(LifecycleState.LOADING, obx.TERMINAL_STATES)
        self.assertNotIn("HANDOFF", {state.value for state in LifecycleState})

    def test_queued_loading_running_finished_are_recorded_in_order(self):
        opened = self.open_store()
        path = (LifecycleState.QUEUED, LifecycleState.LOADING, LifecycleState.RUNNING, LifecycleState.FINISHED)
        for step, state in enumerate(path):
            result = opened.record_lifecycle_transition("item-1", state, now=T0 + timedelta(seconds=step))
            self.assertTrue(result.created)
            self.assertEqual(opened.lifecycle_state("item-1"), state)
        rows = self.outbox()
        self.assertEqual(
            rows,
            [
                (1, "lifecycle:item-1:QUEUED", "LIFECYCLE", "item-1", "QUEUED", None, None),
                (2, "lifecycle:item-1:LOADING", "LIFECYCLE", "item-1", "LOADING", None, None),
                (3, "lifecycle:item-1:RUNNING", "LIFECYCLE", "item-1", "RUNNING", None, None),
                (4, "lifecycle:item-1:FINISHED", "LIFECYCLE", "item-1", "FINISHED", None, None),
            ],
        )
        payloads = [entry.payload() for entry in opened.outbox_entries()]
        self.assertEqual([p["previous_state"] for p in payloads], [None, "QUEUED", "LOADING", "RUNNING"])
        self.assertEqual(payloads[3]["at"], "2026-09-19T10:00:03+00:00")

    def test_invalid_transitions_are_refused_and_write_nothing(self):
        opened = self.open_store()
        cases = (
            ("new-running", (), LifecycleState.RUNNING),
            ("new-finished", (), LifecycleState.FINISHED),
            ("q-finished", (LifecycleState.QUEUED,), LifecycleState.FINISHED),
            ("q-failed", (LifecycleState.QUEUED,), LifecycleState.FAILED),
            ("l-finished", (LifecycleState.QUEUED, LifecycleState.LOADING), LifecycleState.FINISHED),
            ("done-stale", (LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.FINISHED), LifecycleState.ABORT_STALE),
            ("failed-running", (LifecycleState.QUEUED, LifecycleState.LOADING, LifecycleState.FAILED), LifecycleState.RUNNING),
            ("stale-running", (LifecycleState.ABORT_STALE,), LifecycleState.RUNNING),
        )
        for item_id, before, state in cases:
            with self.subTest(item=item_id):
                for earlier in before:
                    opened.record_lifecycle_transition(item_id, earlier, now=T0)
                count = len(self.outbox())
                with self.assertRaises(OutboxError) as caught:
                    opened.record_lifecycle_transition(item_id, state, now=T0)
                self.assertEqual(caught.exception.code, OutboxFailure.INVALID_TRANSITION)
                self.assertEqual(len(self.outbox()), count)
                self.assertEqual(opened.lifecycle_state(item_id), before[-1] if before else None)

    def test_superseded_and_dropped_link_the_other_item_with_a_code(self):
        opened = self.open_store()
        opened.record_lifecycle_transition("old", LifecycleState.QUEUED, now=T0)
        result = opened.record_lifecycle_transition(
            "old", LifecycleState.SUPERSEDED, related_id="new", reason="newer_version", now=T0
        )
        self.assertEqual(result.entry.payload()["related_id"], "new")
        self.assertEqual(result.entry.payload()["reason"], "newer_version")
        dropped = opened.record_lifecycle_transition("worst", LifecycleState.DROPPED_BACKPRESSURE, related_id="old", now=T0)
        self.assertEqual(dropped.entry.payload()["previous_state"], None)
        for bad in ("Deadline passed!", "database is locked: /tmp/x", "", "x" * 65):
            with self.assertRaises(OutboxError) as caught:
                opened.record_lifecycle_transition("other", LifecycleState.QUEUED, reason=bad, now=T0)
            self.assertEqual(caught.exception.code, OutboxFailure.INVALID_FIELD)
        with self.assertRaises(OutboxError) as caught:
            opened.record_lifecycle_transition("bad id with spaces", LifecycleState.QUEUED, now=T0)
        self.assertEqual(caught.exception.code, OutboxFailure.INVALID_FIELD)
        with self.assertRaises(OutboxError) as caught:
            opened.record_lifecycle_transition("other", LifecycleState.QUEUED, now=datetime(2026, 9, 19, 10, 0))
        self.assertEqual(caught.exception.code, OutboxFailure.INVALID_CLOCK)

    def test_a_finished_item_is_final_in_sqlite_too(self):
        opened = self.open_store()
        for state in (LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.FINISHED):
            opened.record_lifecycle_transition("item-1", state, now=T0)
        conn = self.connect()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE lifecycle_items SET state = 'RUNNING' WHERE item_id = 'item-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM lifecycle_items")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE outbox SET state = 'RUNNING'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM outbox")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO outbox (delivery_id, kind, subject_id, state, payload_json, recorded_at) "
                "VALUES ('x', 'LIFECYCLE', 'i', 'THINKING', '{}', 't')"
            )

    def test_lifecycle_rows_are_never_handoffs(self):
        opened = self.open_store()
        for state in (LifecycleState.QUEUED, LifecycleState.LOADING, LifecycleState.RUNNING):
            opened.record_lifecycle_transition("item-1", state, now=T0)
        self.assertEqual(opened.outbox_entries(kind=OutboxKind.HANDOFF), ())
        self.assertEqual({(row[5], row[6]) for row in self.outbox()}, {(None, None)})


# --- first_for_subject: a stable read for a consumer that keeps no cursor (T033b) ----------------


class TestFirstForSubject(TempCase):
    def test_none_when_nothing_was_ever_recorded_for_the_subject(self):
        opened = self.open_store()
        self.assertIsNone(opened.first_outbox_entry(OutboxKind.EVENT, "no-such-event"))

    def test_returns_the_first_row_even_after_later_writes_to_the_same_subject(self):
        opened = self.open_store()
        event_id, _created = create_event_if_new(opened, **event_kwargs())
        opened.mark_event_processed(event_id, "2026-09-19T10:05:00+00:00")
        opened.mark_event_notified(event_id, "2026-09-19T10:06:00+00:00")
        first = opened.first_outbox_entry(OutboxKind.EVENT, event_id)
        self.assertIsNotNone(first)
        self.assertEqual(first.delivery_id, f"event:{event_id}:1")  # the insert row, not processed (2) or notified (3)
        self.assertEqual(len(opened.outbox_entries(kind=OutboxKind.EVENT)), 3)

    def test_the_first_row_survives_closing_and_reopening_the_store(self):
        opened = self.open_store()
        event_id, _created = create_event_if_new(opened, **event_kwargs())
        before = opened.first_outbox_entry(OutboxKind.EVENT, event_id)
        self.close_store(opened)
        reopened = self.open_store()
        reopened.mark_event_notified(event_id, "2026-09-19T10:06:00+00:00")
        after = reopened.first_outbox_entry(OutboxKind.EVENT, event_id)
        self.assertEqual(after.delivery_id, before.delivery_id)
        self.assertEqual(after.seq, before.seq)

    def test_kinds_are_never_mixed(self):
        opened = self.open_store()
        opened.record_lifecycle_transition("item-1", LifecycleState.QUEUED, now=T0)
        self.assertIsNone(opened.first_outbox_entry(OutboxKind.EVENT, "item-1"))
        self.assertIsNone(opened.first_outbox_entry(OutboxKind.HANDOFF, "item-1"))
        self.assertIsNotNone(opened.first_outbox_entry(OutboxKind.LIFECYCLE, "item-1"))

    def test_a_malformed_subject_id_is_refused_not_queried(self):
        opened = self.open_store()
        with self.assertRaises(OutboxError) as caught:
            opened.first_outbox_entry(OutboxKind.EVENT, "bad id\n")
        self.assertEqual(caught.exception.code, OutboxFailure.INVALID_FIELD)


# --- handoffs need a real sender and a real receiver --------------------------------------------


class TestHandoff(TempCase):
    def test_handoff_without_sender_or_receiver_is_rejected(self):
        opened = self.open_store()
        for field in ("sender", "receiver"):
            for missing in (None, "", "   "):
                with self.subTest(field=field, value=missing):
                    with self.assertRaises(OutboxError) as caught:
                        opened.record_handoff(handoff(**{field: missing}), now=T0)
                    self.assertEqual(caught.exception.code, OutboxFailure.HANDOFF_WITHOUT_PARTIES)
        with self.assertRaises(OutboxError) as caught:
            opened.record_handoff(handoff(receiver="controller"), now=T0)
        self.assertEqual(caught.exception.code, OutboxFailure.HANDOFF_SAME_PARTY)
        self.assertEqual(self.outbox(), [])
        self.assertFalse(opened._conn.in_transaction)

    def test_sqlite_refuses_a_handoff_row_without_two_parties(self):
        self.close_store(self.open_store())
        conn = self.connect()
        insert = (
            "INSERT INTO outbox (delivery_id, kind, subject_id, state, sender, receiver, payload_json, recorded_at) "
            "VALUES (?, 'HANDOFF', 'opp', NULL, ?, ?, '{}', 't')"
        )
        for sender, receiver in ((None, "screener"), ("controller", None), ("", "screener"), ("a", "a"), (" ", "b")):
            with self.subTest(sender=sender, receiver=receiver):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(insert, (f"h-{sender}-{receiver}", sender, receiver))
        with self.assertRaises(sqlite3.IntegrityError):  # a lifecycle row cannot carry parties
            conn.execute(
                "INSERT INTO outbox (delivery_id, kind, subject_id, state, sender, receiver, payload_json, recorded_at) "
                "VALUES ('l', 'LIFECYCLE', 'i', 'QUEUED', 'a', 'b', '{}', 't')"
            )
        conn.rollback()

    def test_a_real_handoff_persists_both_parties_in_the_ui_shape(self):
        opened = self.open_store()
        result = opened.record_handoff(handoff(), now=T0 + timedelta(seconds=1))
        self.assertTrue(result.created)
        self.assertEqual(self.outbox(), [(1, "handoff:comm-1", "HANDOFF", "opp-1", None, "controller", "screener")])
        payload = result.entry.payload()
        self.assertEqual(
            {key: payload[key] for key in ("id", "from", "to", "ts", "type", "reason")},
            {
                "id": "comm-1", "from": "controller", "to": "screener", "ts": "2026-09-19T10:00:00+00:00",
                "type": "DISPATCH", "reason": "deadline_fits",
            },
        )
        self.assertEqual((payload["run_id"], payload["invocation_id"]), ("run-1", "inv-1"))

    def test_a_repeated_handoff_is_stored_once_and_a_changed_one_conflicts(self):
        opened = self.open_store()
        first = opened.record_handoff(handoff(), now=T0)
        again = opened.record_handoff(handoff(), now=T0 + timedelta(minutes=1))
        self.assertFalse(again.created)
        self.assertEqual(again.entry, first.entry)
        with self.assertRaises(OutboxError) as caught:
            opened.record_handoff(handoff(receiver="deep_analyst"), now=T0)
        self.assertEqual(caught.exception.code, OutboxFailure.DELIVERY_CONFLICT)
        self.assertEqual(len(self.outbox()), 1)

    def test_free_text_and_malformed_fields_are_refused(self):
        opened = self.open_store()
        for changes in (
            {"reason": "model said: sell now"},
            {"sender": "Controller"},
            {"communication_id": ""},
            {"evidence_hash": "md5:abc"},
            {"handoff_type": "DISPATCH"},
            {"at": datetime(2026, 9, 19, 10, 0)},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(OutboxError) as caught:
                    opened.record_handoff(handoff(**changes), now=T0)
                self.assertIn(caught.exception.code, (OutboxFailure.INVALID_FIELD, OutboxFailure.INVALID_CLOCK))
        self.assertEqual(self.outbox(), [])


# --- cursors, dedup and restart -----------------------------------------------------------------


class TestCursorAndRestart(TempCase):
    def fill(self, opened, count):
        for index in range(count):
            opened.record_lifecycle_transition(f"item-{index}", LifecycleState.QUEUED, now=T0)

    def test_cursor_after_restart_resumes_without_losing_or_inventing(self):
        opened = self.open_store()
        self.fill(opened, 5)
        first = opened.read_outbox("reader", limit=3)
        self.assertEqual([entry.seq for entry in first], [1, 2, 3])
        self.assertEqual(opened.acknowledge_outbox("reader", 3, now=T0), 3)
        self.close_store(opened)  # restart

        reopened = self.open_store()
        self.assertEqual(reopened.outbox_cursor("reader"), 3)
        pending = reopened.read_outbox("reader")
        self.assertEqual(
            [entry.delivery_id for entry in pending], ["lifecycle:item-3:QUEUED", "lifecycle:item-4:QUEUED"]
        )
        # Acknowledging behind the cursor is harmless and never moves it back.
        self.assertEqual(reopened.acknowledge_outbox("reader", 1, now=T0), 3)
        # Acknowledging past the last row would invent rows: refused.
        with self.assertRaises(OutboxError) as caught:
            reopened.acknowledge_outbox("reader", 6, now=T0)
        self.assertEqual(caught.exception.code, OutboxFailure.CURSOR_BEYOND_OUTBOX)
        self.assertEqual(reopened.acknowledge_outbox("reader", 5, now=T0), 5)
        self.assertEqual(reopened.read_outbox("reader"), ())
        # A new row after the restart is delivered next, and only it.
        reopened.record_lifecycle_transition("item-9", LifecycleState.QUEUED, now=T0)
        self.assertEqual([entry.seq for entry in reopened.read_outbox("reader")], [6])

    def test_cursors_are_per_consumer_and_monotonic_in_sqlite(self):
        opened = self.open_store()
        self.fill(opened, 3)
        opened.acknowledge_outbox("a", 2, now=T0)
        self.assertEqual((opened.outbox_cursor("a"), opened.outbox_cursor("b")), (2, 0))
        self.assertEqual(len(opened.read_outbox("b")), 3)
        conn = self.connect()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE outbox_cursors SET position = 1 WHERE consumer = 'a'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM outbox_cursors")
        for bad in ("", " padded", "tab\tname", 7):
            with self.assertRaises(OutboxError):
                opened.read_outbox(bad)
        with self.assertRaises(OutboxError):
            opened.acknowledge_outbox("a", -1, now=T0)

    def test_duplicate_does_not_duplicate_in_the_consumer(self):
        opened = self.open_store()
        self.assertTrue(opened.record_lifecycle_transition("item-1", LifecycleState.QUEUED, now=T0).created)
        # The same transition recorded again (a retry after a crash) writes nothing.
        again = opened.record_lifecycle_transition("item-1", LifecycleState.QUEUED, now=T0 + timedelta(seconds=9))
        self.assertFalse(again.created)
        self.assertEqual(again.entry.seq, 1)
        opened.record_lifecycle_transition("item-1", LifecycleState.RUNNING, now=T0)
        # A late retry of an earlier state is still recognised, not re-applied.
        self.assertFalse(opened.record_lifecycle_transition("item-1", LifecycleState.QUEUED, now=T0).created)
        with self.assertRaises(OutboxError) as caught:
            opened.record_lifecycle_transition("item-1", LifecycleState.QUEUED, reason="other", now=T0)
        self.assertEqual(caught.exception.code, OutboxFailure.DELIVERY_CONFLICT)
        opened.record_handoff(handoff(), now=T0)
        opened.record_handoff(handoff(), now=T0)
        self.assertEqual(len(self.outbox()), 3)

        # At-least-once: the consumer handles rows, crashes before acknowledging, restarts,
        # and gets the same rows again with the same delivery IDs; it dedups on them.
        handled = []

        def consume(entries):
            for entry in entries:
                if entry.delivery_id not in handled:
                    handled.append(entry.delivery_id)

        consume(opened.read_outbox("dedup-consumer"))
        self.close_store(opened)
        reopened = self.open_store()
        redelivered = reopened.read_outbox("dedup-consumer")
        self.assertEqual([entry.delivery_id for entry in redelivered], handled)
        consume(redelivered)
        self.assertEqual(
            handled, ["lifecycle:item-1:QUEUED", "lifecycle:item-1:RUNNING", "handoff:comm-1"]
        )
        reopened.acknowledge_outbox("dedup-consumer", redelivered[-1].seq, now=T0)
        self.assertEqual(reopened.read_outbox("dedup-consumer"), ())

    def test_busy_database_retries_then_fails_typed_and_writes_nothing(self):
        self.close_store(self.open_store())
        conn = self.connect(timeout=0)
        blocker = self.connect(timeout=0, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        slept = []
        with self.assertRaises(OutboxError) as caught:
            obx.record_lifecycle_transition(conn, "item-1", LifecycleState.QUEUED, now=T0, sleep=slept.append)
        self.assertEqual(caught.exception.code, OutboxFailure.BUSY)
        self.assertEqual(slept, [0.05, 0.1, 0.2])
        blocker.execute("ROLLBACK")
        self.assertFalse(conn.in_transaction)
        self.assertEqual(self.outbox(), [])

    def test_a_caller_holding_a_transaction_is_refused(self):
        self.close_store(self.open_store())
        conn = self.connect()
        conn.execute("INSERT INTO radar_runs (run_id, ts, mode) VALUES ('r', 't', 'm')")
        with self.assertRaises(OutboxError) as caught:
            obx.record_lifecycle_transition(conn, "item-1", LifecycleState.QUEUED, now=T0)
        self.assertEqual(caught.exception.code, OutboxFailure.OPEN_TRANSACTION)
        conn.rollback()


# --- JSONL export from the outbox ---------------------------------------------------------------


class TestJsonlExport(TempCase):
    def test_export_writes_each_outbox_row_once_with_its_delivery_id(self):
        opened = self.open_store()
        event_id, _ = create_event_if_new(opened, **event_kwargs())
        transition_event(opened, event_id, "PROCESSING")
        transition_event(opened, event_id, "PROCESSED")
        lines = parsed_lines(self.log)
        self.assertEqual(
            [line["delivery_id"] for line in lines],
            [f"event:{event_id}:1", f"event:{event_id}:2", f"event:{event_id}:3"],
        )
        self.assertEqual([line["status"] for line in lines], ["PENDING", "PROCESSING", "PROCESSED"])
        self.assertEqual([line["outbox_seq"] for line in lines], [1, 2, 3])
        self.assertEqual({line["outbox_kind"] for line in lines}, {"EVENT"})
        # Old event-log consumers keep every column they read before.
        for key in ("event_id", "dedup_key", "ts", "type", "asset", "status", "context_json"):
            self.assertIn(key, lines[0])
        again = snapshot_to_jsonl(opened, event_id)
        self.assertEqual((again.written, again.already_in_file, again.position), (0, 0, 3))
        self.assertEqual(len(read_lines(self.log)), 3)
        self.assertEqual(opened.outbox_cursor(obx.jsonl_consumer(self.log)), 3)

    def test_crash_between_transition_and_export_keeps_state_and_reexports_same_id(self):
        opened = self.open_store()
        event_id, _ = create_event_if_new(opened, **event_kwargs())
        # The transition commits, then the process dies before any export.
        opened.mark_event_processed(event_id, T0.isoformat())
        self.close_store(opened)
        self.assertEqual(len(read_lines(self.log)), 1)

        reopened = self.open_store()
        self.assertEqual(self.status(event_id), "PROCESSED")  # durable state survived
        pending = reopened.read_outbox(obx.jsonl_consumer(self.log))
        self.assertEqual([entry.delivery_id for entry in pending], [f"event:{event_id}:2"])
        result = snapshot_to_jsonl(reopened, event_id)
        self.assertEqual((result.written, result.position), (1, 2))
        lines = parsed_lines(self.log)
        self.assertEqual(lines[-1]["delivery_id"], f"event:{event_id}:2")
        self.assertEqual(lines[-1]["status"], "PROCESSED")
        self.assertEqual(snapshot_to_jsonl(reopened).written, 0)
        self.assertEqual(len(read_lines(self.log)), 2)

    def test_export_failure_after_a_transition_loses_nothing(self):
        opened = self.open_store()
        event_id, _ = create_event_if_new(opened, **event_kwargs())
        full = OSError(28, "No space left on device")
        with mock.patch.object(obx, "_append", side_effect=full):
            with self.assertRaises(OutboxError) as caught:
                transition_event(opened, event_id, "FAILED")
        self.assertEqual(caught.exception.code, OutboxFailure.EXPORT_FAILED)
        self.assertNotIn("No space", caught.exception.detail)
        self.assertEqual(self.status(event_id), "FAILED")
        self.assertEqual(opened.outbox_cursor(obx.jsonl_consumer(self.log)), 1)
        snapshot_to_jsonl(opened)
        self.assertEqual(
            [line["delivery_id"] for line in parsed_lines(self.log)], [f"event:{event_id}:1", f"event:{event_id}:2"]
        )

    def test_crash_after_append_before_cursor_commit_does_not_duplicate(self):
        opened = self.open_store()
        event_id, _ = create_event_if_new(opened, **event_kwargs())
        opened.mark_event_processed(event_id, T0.isoformat())
        opened.mark_event_notified(event_id, T0.isoformat())
        opened._conn.execute(
            "CREATE TEMP TRIGGER t033a_crash BEFORE UPDATE ON main.outbox_cursors "
            "BEGIN SELECT RAISE(ABORT, 'crash before the cursor commit'); END"
        )
        with self.assertRaises(OutboxError):
            snapshot_to_jsonl(opened)
        # The lines reached the file, the cursor did not move.
        self.assertEqual(len(read_lines(self.log)), 3)
        self.assertEqual(opened.outbox_cursor(obx.jsonl_consumer(self.log)), 1)
        opened._conn.execute("DROP TRIGGER temp.t033a_crash")
        result = snapshot_to_jsonl(opened)
        self.assertEqual((result.written, result.already_in_file, result.position), (0, 2, 3))
        ids = [line["delivery_id"] for line in parsed_lines(self.log)]
        self.assertEqual(ids, [f"event:{event_id}:1", f"event:{event_id}:2", f"event:{event_id}:3"])

    def test_a_torn_last_line_is_kept_and_the_next_record_starts_clean(self):
        opened = self.open_store()
        event_id, _ = create_event_if_new(opened, **event_kwargs())
        with open(self.log, "ab") as handle:
            handle.write(b'{"delivery_id": "event:torn')
        transition_event(opened, event_id, "PROCESSING")
        raw = read_lines(self.log)
        self.assertEqual(raw[1], b'{"delivery_id": "event:torn')
        self.assertEqual(json.loads(raw[2])["delivery_id"], f"event:{event_id}:2")

    def test_export_to_a_new_path_recovers_the_whole_history(self):
        opened = self.open_store()
        event_id, _ = create_event_if_new(opened, **event_kwargs())
        transition_event(opened, event_id, "PROCESSING")
        opened.record_lifecycle_transition("item-1", LifecycleState.QUEUED, now=T0)
        snapshot_to_jsonl(opened)
        recovered = os.path.join(self.tmp, "recovered", "events.jsonl")
        os.makedirs(os.path.dirname(recovered))
        result = snapshot_to_jsonl(opened, path=recovered)
        self.assertEqual((result.written, result.position), (3, 3))
        self.assertEqual(read_lines(recovered), read_lines(self.log))
        self.assertEqual(snapshot_to_jsonl(opened, path=recovered).written, 0)
        self.assertEqual(parsed_lines(recovered)[2]["outbox_kind"], "LIFECYCLE")

    def test_the_default_path_is_the_patched_config_path_never_the_root_log(self):
        self.assertTrue(os.path.abspath(config.EVENTS_LOG_PATH).startswith(os.path.abspath(self.tmp)))
        self.assertNotEqual(
            obx.jsonl_consumer(config.EVENTS_LOG_PATH), obx.jsonl_consumer(os.path.join(REPO_ROOT, "events.jsonl"))
        )
        opened = self.open_store()
        create_event_if_new(opened, **event_kwargs())
        self.assertEqual(len(read_lines(self.log)), 1)

    def test_delivery_semantics_are_documented_as_at_least_once(self):
        for module in (obx, events_module):
            doc = module.__doc__.lower()
            self.assertIn("at-least-once", doc)
            self.assertIn("exactly-once", doc)  # stated as *not* promised
        self.assertIn("never exactly-once", obx.__doc__)


# --- the current pipeline has no handoff --------------------------------------------------------


class TestPipelineWithoutHandoff(wiring.IntegrityWiringBase):
    def test_heartbeat_and_bridge_emit_no_agent_to_agent_traffic(self):
        self.run_cycle(wiring.FakeKraken(assets=("BTC",)))
        model, notify = bridge_wiring.FakeModel(), mock.Mock()
        with bridge_wiring.dispatch_enabled():
            result = claude_bridge.run_bridge_cycle(
                self.store, now=bridge_wiring.BRIDGE_NOW, create_fn=model, notify_fn=notify
            )
        self.assertEqual([p["outcome"] for p in result.processed], ["PROCESSED"])
        entries = self.store.outbox_entries()
        self.assertTrue(entries)
        self.assertEqual({entry.kind for entry in entries}, {OutboxKind.EVENT})
        self.assertEqual(self.store.outbox_entries(kind=OutboxKind.HANDOFF), ())
        self.assertEqual(self.store.outbox_entries(kind=OutboxKind.LIFECYCLE), ())
        self.assertEqual({(entry.sender, entry.receiver) for entry in entries}, {(None, None)})
        self.assertEqual([entry.state for entry in entries][:3], ["PENDING", "PROCESSING", "PROCESSED"])
        # The event log is exactly the exported outbox: every row once, no handoff line.
        log = parsed_lines(config.EVENTS_LOG_PATH)
        self.assertEqual([line["delivery_id"] for line in log], [entry.delivery_id for entry in entries])
        self.assertNotIn("HANDOFF", {line["outbox_kind"] for line in log})
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox WHERE sender IS NOT NULL").fetchone(), (0,))


# --- module boundary ----------------------------------------------------------------------------


class TestModuleBoundary(unittest.TestCase):
    def test_adapter_imports_only_stdlib_domain_and_the_ledger(self):
        path = os.path.join(REPO_ROOT, "radar_v08", "adapters", "outbox_store.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(("." * node.level) + (node.module or ""))
        self.assertEqual(
            imported,
            {
                "__future__", "json", "os", "re", "sqlite3", "time", "collections.abc", "dataclasses", "datetime",
                "enum", "types", "..domain.invocation", ".",
            },
        )


if __name__ == "__main__":
    unittest.main()
