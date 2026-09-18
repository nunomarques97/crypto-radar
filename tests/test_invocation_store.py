"""T031a: SQLite invocation claims, atomic budget reservation, lease fencing and migration v3.

Every database is a fixture in a fresh temporary directory. Nothing opens, reads or copies
``radar_state.sqlite`` or any other root state file. No network, model or notification.
"""

import ast
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import store
from radar_v08.adapters import evidence_store as es
from radar_v08.adapters import invocation_store as ivs
from radar_v08.adapters.evidence_store import (
    INVOCATION_MIGRATION,
    SCHEMA_MIGRATIONS,
    MigrationFailure,
    SchemaMigrationError,
)
from radar_v08.domain.integrity import InstrumentKind
from radar_v08.domain.invocation import (
    Claimed,
    Direction,
    Duplicate,
    InvocationError,
    InvocationFailure,
    InvocationIdentity,
    InvocationRequest,
    InvocationState,
    Lease,
    ModelBudget,
    RefusalReason,
    Refused,
    ReleaseReason,
    TransitionStatus,
)
from radar_v08.store import SnapshotStore

UTC = timezone.utc
T0 = datetime(2026, 9, 18, 12, 10, 0, tzinfo=UTC)
MODEL = "qwen3:14b"
ROOMY = ModelBudget(MODEL, 100, 1000)
NEW_TABLES = {"invocations", "invocation_budget", "invocation_demand"}
LEDGER_PLAN_TABLES = NEW_TABLES | {"schema_version_ledger", "evidence_versions", "event_evidence"}


def ev_hash(n):
    return "sha256:" + format(n, "064x")


def identity(n=1, **changes):
    fields = dict(
        venue="kraken",
        market_kind=InstrumentKind.SPOT,
        native_instrument="XBT/USD",
        setup="BREAKOUT",
        direction=Direction.LONG,
        evidence_hash=ev_hash(n),
        policy_version="OC-1",
    )
    fields.update(changes)
    return InvocationIdentity(**fields)


def request(n=1, **changes):
    return InvocationRequest(identity(n, **changes), MODEL)


def build_legacy_db(path, *, duplicate_pending=True):
    """A pre-ledger database with the old schema, old rows, and (by default) legacy
    duplicates in ``events``: the same dedup_key in PENDING twice."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(store.SCHEMA)
        for column, sql_type in store._FORWARD_RETURNS_MIGRATION_COLUMNS.items():
            conn.execute(f"ALTER TABLE forward_returns ADD COLUMN {column} {sql_type}")
        for column, sql_type in store._EVENTS_MIGRATION_COLUMNS.items():
            conn.execute(f"ALTER TABLE events ADD COLUMN {column} {sql_type}")
        copies = ("evt-dup-1", "evt-dup-2") if duplicate_pending else ("evt-dup-1",)
        for event_id in copies:
            conn.execute(
                "INSERT INTO events (event_id, dedup_key, ts, type, asset, setup_type, direction, status) "
                "VALUES (?, 'BTC|BREAKOUT|LONG|qwen', '2026-09-01T00:05:00+00:00', 'RADAR_ALERT', 'BTC', "
                "'BREAKOUT', 'LONG', 'PENDING')",
                (event_id,),
            )
        conn.execute(
            "INSERT INTO model_budget_usage (model, window_kind, window_start, count) "
            "VALUES ('qwen', 'hour', '2026-09-01T00:00:00', 3)"
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


def legacy_indexes(path):
    conn = sqlite3.connect(path)
    try:
        return {
            row
            for row in conn.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
            if row[1] not in LEDGER_PLAN_TABLES
        }
    finally:
        conn.close()


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "fixture.sqlite")
        self._conns = []
        self._stores = []

    def tearDown(self):
        for conn in self._conns:
            conn.close()
        for s in self._stores:
            s.close()

    def connect(self, path=None, **kwargs):
        conn = sqlite3.connect(path or self.path, check_same_thread=False, **kwargs)
        self._conns.append(conn)
        return conn

    def forget(self, conn):
        conn.close()
        self._conns.remove(conn)

    def migrated(self):
        build_legacy_db(self.path)
        conn = self.connect()
        es.apply_schema_migrations(conn, now=T0)
        return conn

    def rows(self, conn, table):
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3").fetchall()

    def reserved(self, conn, kind="hour"):
        return conn.execute(
            "SELECT COALESCE(SUM(reserved), 0) FROM invocation_budget WHERE model = ? AND window_kind = ?",
            (MODEL, kind),
        ).fetchone()[0]

    def invocation_count(self, conn):
        return conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]

    def claim(self, conn, n=1, budget=ROOMY, owner="worker-a", now=T0, lease_seconds=30, **kwargs):
        return ivs.claim_invocation(
            conn, request(n), budget, owner=owner, now=now, lease_seconds=lease_seconds, **kwargs
        )


# --- migration v3 (D19) -------------------------------------------------------------------------


class TestMigrationV3(TempDbCase):
    def test_v3_is_the_last_version_of_the_ledger_plan(self):
        self.assertIs(SCHEMA_MIGRATIONS[-1], INVOCATION_MIGRATION)
        self.assertEqual(INVOCATION_MIGRATION.version, len(SCHEMA_MIGRATIONS))

    def test_statements_are_create_only(self):
        for statement in INVOCATION_MIGRATION.statements:
            self.assertTrue(statement.startswith("CREATE "), statement[:40])
            upper = statement.upper()
            self.assertNotIn("ALTER", upper)
            self.assertNotIn("DROP", upper)
            self.assertNotIn("DELETE", upper)
            self.assertNotIn("IF NOT EXISTS", upper)
            # UPDATE appears only as the trigger event, never as a statement.
            self.assertEqual(len(re.findall(r"\bUPDATE\b", upper)), len(re.findall(r"\bBEFORE UPDATE\b", upper)))
            for target in re.findall(r"\bON\s+(\w+)", statement):
                self.assertIn(target, NEW_TABLES | {"CONFLICT"}, statement[:60])

    def test_legacy_duplicates_in_events_open_without_error(self):
        build_legacy_db(self.path)
        before_indexes = legacy_indexes(self.path)
        legacy_dump, _ = snapshot(self.path)
        s = SnapshotStore(self.path)
        self._stores.append(s)
        self.assertEqual([entry.version for entry in s.schema_ledger()], [1, 2, 3])
        conn = self.connect()
        dupes = conn.execute(
            "SELECT event_id FROM events WHERE dedup_key = 'BTC|BREAKOUT|LONG|qwen' AND status = 'PENDING' ORDER BY 1"
        ).fetchall()
        self.assertEqual(dupes, [("evt-dup-1",), ("evt-dup-2",)])
        # No index (unique or not) was added to, or removed from, any legacy table.
        self.assertEqual(legacy_indexes(self.path), before_indexes)
        unique_new = conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND sql LIKE 'CREATE UNIQUE%'"
        ).fetchall()
        self.assertEqual(unique_new, [("uq_invocations_active_identity", "invocations")])
        # Every legacy row survives byte for byte (the legacy budget table included).
        new_dump, _ = snapshot(self.path)
        legacy_rows = [line for line in legacy_dump if line.startswith("INSERT INTO")]
        self.assertEqual(len(legacy_rows), 3)
        for line in legacy_rows:
            self.assertIn(line, new_dump)

    def test_version_3_is_recorded_with_its_checksum(self):
        conn = self.migrated()
        entry = es.read_ledger(conn)[-1]
        self.assertEqual((entry.version, entry.name), (3, "invocations_budget_and_demand"))
        self.assertEqual(entry.checksum, INVOCATION_MIGRATION.checksum)
        self.assertEqual(entry.applied_at, "2026-09-18T12:10:00+00:00")

    def test_second_open_writes_nothing(self):
        build_legacy_db(self.path)
        SnapshotStore(self.path).close()
        first = snapshot(self.path)
        conn = self.connect()
        changes = conn.total_changes
        self.assertEqual(es.apply_schema_migrations(conn, now=T0 + timedelta(days=1)), ())
        self.assertEqual(conn.total_changes, changes)
        self.forget(conn)
        again = SnapshotStore(self.path)
        self.assertEqual(again.migrate_schema(), ())
        again.close()
        self.assertEqual(snapshot(self.path), first)

    def test_t030b_database_upgrades_to_v3_only(self):
        build_legacy_db(self.path)
        conn = self.connect()
        es.apply_schema_migrations(conn, now=T0, migrations=SCHEMA_MIGRATIONS[:2])
        self.assertEqual(es.apply_schema_migrations(conn, now=T0), (3,))
        self.assertEqual([entry.version for entry in es.read_ledger(conn)], [1, 2, 3])

    def test_injected_failure_in_v3_rolls_back_everything(self):
        # invocation_demand is statement 9 of 10: the invocations and budget tables, their
        # index and triggers are created first inside the transaction, then the clash aborts.
        build_legacy_db(self.path)
        conn = self.connect()
        es.apply_schema_migrations(conn, now=T0, migrations=SCHEMA_MIGRATIONS[:2])
        conn.execute("CREATE TABLE invocation_demand (unrelated TEXT)")
        conn.commit()
        self.forget(conn)
        before = snapshot(self.path)
        with self.assertRaises(SchemaMigrationError) as caught:
            SnapshotStore(self.path)
        self.assertEqual(caught.exception.code, MigrationFailure.STATEMENT_FAILED)
        self.assertEqual(caught.exception.version, 3)
        self.assertIn("statement 9/10", caught.exception.detail)
        self.assertEqual(snapshot(self.path), before)
        conn = self.connect()
        self.assertEqual([entry.version for entry in es.read_ledger(conn)], [1, 2])
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("invocations", names)
        self.assertNotIn("invocation_budget", names)

    def test_injected_failure_on_a_legacy_db_rolls_back_all_three_versions(self):
        build_legacy_db(self.path)
        conn = self.connect()
        conn.execute("CREATE TABLE invocation_budget (unrelated TEXT)")
        conn.commit()
        before = snapshot(self.path)
        with self.assertRaises(SchemaMigrationError) as caught:
            es.apply_schema_migrations(conn, now=T0)
        self.assertEqual(caught.exception.version, 3)
        self.assertFalse(conn.in_transaction)
        self.forget(conn)
        self.assertEqual(snapshot(self.path), before)


# --- claim, dedup, budget ------------------------------------------------------------------------


class TestClaim(TempDbCase):
    def test_identical_request_returns_existing_id_without_new_reservation(self):
        conn = self.migrated()
        first = self.claim(conn, 1)
        self.assertIsInstance(first, Claimed)
        self.assertEqual(first.lease.generation, 1)
        self.assertEqual((first.usage.hourly_reserved, first.usage.daily_reserved), (1, 1))
        second = self.claim(conn, 1, owner="worker-b")
        self.assertEqual(second, Duplicate(first.lease.invocation_id, 2))
        self.assertEqual(self.invocation_count(conn), 1)
        self.assertEqual((self.reserved(conn, "hour"), self.reserved(conn, "day")), (1, 1))
        record = ivs.load_invocation(conn, first.lease.invocation_id)
        self.assertEqual((record.demand_count, record.attempt_count, record.lease_owner), (2, 0, "worker-a"))

    def test_different_evidence_hash_is_a_different_invocation(self):
        conn = self.migrated()
        a = self.claim(conn, 1)
        b = self.claim(conn, 2)
        self.assertIsInstance(b, Claimed)
        self.assertNotEqual(a.lease.invocation_id, b.lease.invocation_id)
        self.assertEqual(self.reserved(conn), 2)

    def test_uniqueness_is_only_for_active_invocations(self):
        conn = self.migrated()
        first = self.claim(conn, 1)
        done = ivs.complete_invocation(conn, first.lease, now=T0 + timedelta(seconds=5))
        self.assertIs(done.status, TransitionStatus.APPLIED)
        again = self.claim(conn, 1, now=T0 + timedelta(seconds=6))
        self.assertIsInstance(again, Claimed)
        self.assertNotEqual(again.lease.invocation_id, first.lease.invocation_id)
        self.assertEqual(self.reserved(conn), 2)

    def test_partial_unique_index_is_the_backstop_for_active_rows(self):
        conn = self.migrated()
        claimed = self.claim(conn, 1)
        copy = (
            "INSERT INTO invocations SELECT ?, venue, market_kind, native_instrument, setup, direction, evidence_hash, "
            "policy_version, model, ?, generation, lease_owner, lease_expires_at, demand_count, attempt_count, "
            "hour_window, day_window, claimed_at, updated_at, ended_at, end_reason FROM invocations WHERE invocation_id = ?"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(copy, ("inv-copy", "CLAIMED", claimed.lease.invocation_id))
        conn.rollback()
        conn.execute(copy, ("inv-old", "COMPLETED", claimed.lease.invocation_id))  # inactive: allowed
        conn.rollback()

    def test_every_call_leaves_no_open_transaction_and_refuses_one(self):
        conn = self.migrated()
        result = self.claim(conn, 1)
        self.assertFalse(conn.in_transaction)
        ivs.record_attempt(conn, result.lease, ROOMY, now=T0)
        self.assertFalse(conn.in_transaction)
        conn.execute("INSERT INTO radar_runs (run_id, ts, mode) VALUES ('r', 't', 'm')")  # implicit transaction
        with self.assertRaises(InvocationError) as caught:
            self.claim(conn, 2)
        self.assertEqual(caught.exception.code, InvocationFailure.OPEN_TRANSACTION)
        conn.rollback()

    def test_unmigrated_database_is_refused(self):
        build_legacy_db(self.path)
        conn = self.connect()
        es.apply_schema_migrations(conn, now=T0, migrations=SCHEMA_MIGRATIONS[:2])
        before = snapshot(self.path)
        with self.assertRaises(InvocationError) as caught:
            self.claim(conn, 1)
        self.assertEqual(caught.exception.code, InvocationFailure.SCHEMA_NOT_MIGRATED)
        self.forget(conn)
        self.assertEqual(snapshot(self.path), before)

    def test_budget_for_another_model_is_refused(self):
        conn = self.migrated()
        with self.assertRaises(InvocationError) as caught:
            self.claim(conn, 1, budget=ModelBudget("gpt-oss:20b", 5, 5))
        self.assertEqual(caught.exception.code, InvocationFailure.INVALID_FIELD)
        self.assertEqual(self.rows(conn, "invocation_demand"), [])

    def test_crash_inside_the_claim_transaction_leaves_nothing(self):
        conn = self.migrated()

        def dying_id():
            raise KeyboardInterrupt("process killed mid-claim")

        with self.assertRaises(KeyboardInterrupt):
            self.claim(conn, 1, new_id=dying_id)
        self.assertFalse(conn.in_transaction)
        self.assertEqual(self.rows(conn, "invocations"), [])
        self.assertEqual(self.rows(conn, "invocation_budget"), [])
        self.assertEqual(self.rows(conn, "invocation_demand"), [])


class TestFailedClaimDoesNotCharge(TempDbCase):
    def test_hourly_exhausted_claim_is_refused_without_charge_or_row(self):
        conn = self.migrated()
        budget = ModelBudget(MODEL, 1, 10)
        self.assertIsInstance(self.claim(conn, 1, budget=budget), Claimed)
        budget_rows = self.rows(conn, "invocation_budget")
        refused = self.claim(conn, 2, budget=budget)
        self.assertIsInstance(refused, Refused)
        self.assertIs(refused.reason, RefusalReason.HOURLY_BUDGET_EXHAUSTED)
        self.assertEqual((refused.usage.hourly_reserved, refused.usage.hourly_limit), (1, 1))
        self.assertEqual(self.rows(conn, "invocation_budget"), budget_rows)
        self.assertEqual(self.invocation_count(conn), 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM invocations WHERE evidence_hash = ?", (ev_hash(2),)).fetchone()[0], 0
        )
        demand = ivs.demand_counts(conn, MODEL, now=T0)
        self.assertEqual((demand.hourly_observed, demand.hourly_refused), (2, 1))
        self.assertEqual((demand.daily_observed, demand.daily_refused), (2, 1))

    def test_daily_exhausted_and_zero_limit(self):
        conn = self.migrated()
        daily = ModelBudget(MODEL, 10, 1)
        self.claim(conn, 1, budget=daily)
        refused = self.claim(conn, 2, budget=daily)
        self.assertIs(refused.reason, RefusalReason.DAILY_BUDGET_EXHAUSTED)
        self.assertEqual(self.reserved(conn, "day"), 1)
        closed = ModelBudget(MODEL, 0, 0)
        self.assertIsInstance(self.claim(conn, 3, budget=closed), Refused)
        self.assertEqual(self.invocation_count(conn), 1)
        self.assertEqual((self.reserved(conn, "hour"), self.reserved(conn, "day")), (1, 1))

    def test_duplicate_of_an_active_claim_is_not_refused_or_charged_when_budget_is_full(self):
        conn = self.migrated()
        budget = ModelBudget(MODEL, 1, 1)
        first = self.claim(conn, 1, budget=budget)
        again = self.claim(conn, 1, budget=budget)
        self.assertEqual(again, Duplicate(first.lease.invocation_id, 2))
        self.assertEqual(self.reserved(conn), 1)


class TestParameterPerturbation(TempDbCase):
    """The same request sequence under different limits gives a different, predicted outcome."""

    def run_sequence(self, hourly, daily):
        path = os.path.join(self.tmp.name, f"perturb-{hourly}-{daily}.sqlite")
        build_legacy_db(path)
        conn = self.connect(path)
        es.apply_schema_migrations(conn, now=T0)
        budget = ModelBudget(MODEL, hourly, daily)
        outcomes = []
        # Three distinct invocations in hour 12, then two in hour 13 of the same UTC day.
        moments = [T0, T0 + timedelta(minutes=1), T0 + timedelta(minutes=2), T0 + timedelta(hours=1), T0 + timedelta(hours=1, minutes=1)]
        for n, moment in enumerate(moments, start=1):
            result = ivs.claim_invocation(conn, request(n), budget, owner="w", now=moment, lease_seconds=30)
            outcomes.append("claimed" if isinstance(result, Claimed) else result.reason.value)
        return outcomes

    def test_limits_change_the_result(self):
        hourly_full = RefusalReason.HOURLY_BUDGET_EXHAUSTED.value
        daily_full = RefusalReason.DAILY_BUDGET_EXHAUSTED.value
        self.assertEqual(self.run_sequence(3, 5), ["claimed"] * 5)
        self.assertEqual(self.run_sequence(2, 5), ["claimed", "claimed", hourly_full, "claimed", "claimed"])
        self.assertEqual(self.run_sequence(3, 4), ["claimed", "claimed", "claimed", "claimed", daily_full])
        self.assertEqual(self.run_sequence(2, 3), ["claimed", "claimed", hourly_full, "claimed", daily_full])
        self.assertEqual(self.run_sequence(1, 1), ["claimed", hourly_full, hourly_full, daily_full, daily_full])


# --- two-connection race ------------------------------------------------------------------------


class TestTwoConnectionRace(TempDbCase):
    ROUNDS = 12

    def race(self, requests_and_budget):
        """Two threads, each with its own sqlite3 connection to the same file, claim at once."""
        barrier = threading.Barrier(2)
        results = [None, None]
        errors = []

        def contender(index, req, budget):
            conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            # Pause 2 ms as every statement starts, so the two claims really interleave:
            # without the single BEGIN IMMEDIATE both would read "free" before either writes.
            conn.set_trace_callback(lambda _statement: time.sleep(0.002))
            try:
                barrier.wait(timeout=5)
                results[index] = ivs.claim_invocation(conn, req, budget, owner=f"worker-{index}", now=T0, lease_seconds=30)
            except BaseException as error:  # surfaced below, never swallowed
                errors.append(error)
            finally:
                conn.close()

        threads = [threading.Thread(target=contender, args=(i, *requests_and_budget[i])) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(errors, [])
        return results

    def test_identical_requests_one_reservation_wins(self):
        self.migrated()
        for round_number in range(self.ROUNDS):
            req = request(1000 + round_number)
            results = self.race([(req, ROOMY), (req, ROOMY)])
            claimed = [r for r in results if isinstance(r, Claimed)]
            duplicates = [r for r in results if isinstance(r, Duplicate)]
            self.assertEqual((len(claimed), len(duplicates)), (1, 1), results)
            self.assertEqual(duplicates[0].invocation_id, claimed[0].lease.invocation_id)
        conn = self.connect()
        self.assertEqual(self.invocation_count(conn), self.ROUNDS)
        self.assertEqual((self.reserved(conn, "hour"), self.reserved(conn, "day")), (self.ROUNDS, self.ROUNDS))
        demand = ivs.demand_counts(conn, MODEL, now=T0)
        self.assertEqual(demand.hourly_observed, 2 * self.ROUNDS)

    def test_last_budget_unit_goes_to_exactly_one_contender(self):
        for round_number in range(self.ROUNDS):
            self.path = os.path.join(self.tmp.name, f"race-{round_number}.sqlite")
            build_legacy_db(self.path)
            setup = sqlite3.connect(self.path)
            es.apply_schema_migrations(setup, now=T0)
            setup.close()
            budget = ModelBudget(MODEL, 1, 1)
            results = self.race([(request(1), budget), (request(2), budget)])
            kinds = sorted(type(r).__name__ for r in results)
            self.assertEqual(kinds, ["Claimed", "Refused"], results)
            conn = sqlite3.connect(self.path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT window_kind, reserved FROM invocation_budget ORDER BY 1").fetchall(),
                    [("day", 1), ("hour", 1)],
                )
            finally:
                conn.close()


# --- crash recovery, generation fence, attempts ------------------------------------------------


class TestCrashRecoveryFence(TempDbCase):
    def test_crash_between_claim_and_completion_then_recovery_fences_the_old_holder(self):
        build_legacy_db(self.path)
        a = self.connect()
        es.apply_schema_migrations(a, now=T0)
        claimed = self.claim(a, 1, owner="worker-a", lease_seconds=30)
        old = claimed.lease
        first = ivs.record_attempt(a, old, ROOMY, now=T0 + timedelta(seconds=1))
        self.assertEqual((first.status, first.attempt_count), (TransitionStatus.APPLIED, 1))
        self.forget(a)  # worker-a dies: no completion, no release

        b = self.connect()
        self.assertEqual(ivs.recover_expired(b, owner="worker-b", now=T0 + timedelta(seconds=29), lease_seconds=30), ())
        recovered = ivs.recover_expired(b, owner="worker-b", now=T0 + timedelta(seconds=31), lease_seconds=30)
        self.assertEqual(len(recovered), 1)
        new = recovered[0]
        self.assertEqual((new.invocation_id, new.generation, new.owner), (old.invocation_id, 2, "worker-b"))
        self.assertEqual(self.reserved(b), 1)  # recovery reserves nothing

        # worker-a comes back with its generation-1 lease: every action is fenced, nothing changes.
        a_again = self.connect()
        before = self.rows(a_again, "invocations"), self.rows(a_again, "invocation_budget")
        late = T0 + timedelta(seconds=32)
        for action in (
            lambda: ivs.complete_invocation(a_again, old, now=late),
            lambda: ivs.release_invocation(a_again, old, ReleaseReason.FAILED, now=late),
            lambda: ivs.record_attempt(a_again, old, ROOMY, now=late),
        ):
            result = action()
            self.assertIs(result.status, TransitionStatus.FENCED)
            self.assertFalse(result.applied)
            self.assertEqual(result.attempt_count, 1)
        self.assertEqual((self.rows(a_again, "invocations"), self.rows(a_again, "invocation_budget")), before)

        # worker-b retries the call: a second genuine attempt reserves a second unit.
        retry = ivs.record_attempt(b, new, ROOMY, now=late)
        self.assertEqual((retry.status, retry.attempt_count), (TransitionStatus.APPLIED, 2))
        self.assertEqual(self.reserved(b), 2)
        done = ivs.complete_invocation(b, new, now=late + timedelta(seconds=1))
        self.assertIs(done.status, TransitionStatus.APPLIED)
        record = ivs.load_invocation(b, new.invocation_id)
        self.assertEqual(
            (record.state, record.generation, record.lease_owner, record.attempt_count),
            (InvocationState.COMPLETED, 2, "worker-b", 2),
        )
        # After completion even the old holder only sees NOT_ACTIVE.
        self.assertIs(ivs.complete_invocation(a_again, old, now=late).status, TransitionStatus.NOT_ACTIVE)

    def test_expired_lease_cannot_complete_before_recovery(self):
        conn = self.migrated()
        lease = self.claim(conn, 1, lease_seconds=10).lease
        result = ivs.complete_invocation(conn, lease, now=T0 + timedelta(seconds=10))
        self.assertIs(result.status, TransitionStatus.LEASE_EXPIRED)
        self.assertIs(ivs.load_invocation(conn, lease.invocation_id).state, InvocationState.CLAIMED)

    def test_unknown_invocation_is_not_found(self):
        conn = self.migrated()
        ghost = Lease("inv-ghost", 1, "worker-a", T0 + timedelta(seconds=30))
        self.assertIs(ivs.complete_invocation(conn, ghost, now=T0).status, TransitionStatus.NOT_FOUND)

    def test_recovery_only_takes_expired_active_leases_and_is_repeatable(self):
        conn = self.migrated()
        short = self.claim(conn, 1, lease_seconds=5).lease
        long = self.claim(conn, 2, lease_seconds=600).lease
        done = self.claim(conn, 3, lease_seconds=5).lease
        ivs.complete_invocation(conn, done, now=T0 + timedelta(seconds=1))
        first = ivs.recover_expired(conn, owner="r1", now=T0 + timedelta(seconds=6), lease_seconds=5)
        self.assertEqual([(got.invocation_id, got.generation) for got in first], [(short.invocation_id, 2)])
        second = ivs.recover_expired(conn, owner="r2", now=T0 + timedelta(seconds=12), lease_seconds=5)
        self.assertEqual([(got.invocation_id, got.generation, got.owner) for got in second], [(short.invocation_id, 3, "r2")])
        self.assertEqual(ivs.load_invocation(conn, long.invocation_id).generation, 1)


class TestAttemptsNeverDecrement(TempDbCase):
    def test_genuine_attempt_is_never_discounted(self):
        conn = self.migrated()
        lease = self.claim(conn, 1).lease
        ivs.record_attempt(conn, lease, ROOMY, now=T0 + timedelta(seconds=1))
        released = ivs.release_invocation(conn, lease, ReleaseReason.CANCELLED, now=T0 + timedelta(seconds=2))
        self.assertEqual((released.status, released.attempt_count), (TransitionStatus.APPLIED, 1))
        record = ivs.load_invocation(conn, lease.invocation_id)
        self.assertEqual((record.state, record.attempt_count, record.end_reason), (InvocationState.RELEASED, 1, "CANCELLED"))
        self.assertEqual(self.reserved(conn), 1)
        # A new claim of the same identity starts a new invocation; the old attempt stays counted.
        again = self.claim(conn, 1, now=T0 + timedelta(seconds=3))
        self.assertIsInstance(again, Claimed)
        self.assertEqual(ivs.load_invocation(conn, lease.invocation_id).attempt_count, 1)
        self.assertEqual(self.reserved(conn), 2)

    def test_database_refuses_any_decrement(self):
        conn = self.migrated()
        lease = self.claim(conn, 1).lease
        ivs.record_attempt(conn, lease, ROOMY, now=T0)
        for statement in (
            "UPDATE invocations SET attempt_count = 0",
            "UPDATE invocations SET generation = 0",
            "UPDATE invocations SET demand_count = 0",
            "UPDATE invocation_budget SET reserved = 0",
            "UPDATE invocation_demand SET observed = 0",
            "UPDATE invocations SET evidence_hash = 'sha256:x'",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                conn.execute(statement)
            conn.rollback()
        ivs.complete_invocation(conn, lease, now=T0 + timedelta(seconds=1))
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE invocations SET state = 'CLAIMED'")
        conn.rollback()
        self.assertEqual(ivs.load_invocation(conn, lease.invocation_id).attempt_count, 1)

    def test_further_attempt_without_budget_is_refused_and_not_recorded(self):
        conn = self.migrated()
        tight = ModelBudget(MODEL, 1, 1)
        lease = self.claim(conn, 1, budget=tight).lease
        self.assertEqual(ivs.record_attempt(conn, lease, tight, now=T0).attempt_count, 1)  # covered by the claim
        repair = ivs.record_attempt(conn, lease, tight, now=T0 + timedelta(seconds=1))
        self.assertEqual((repair.status, repair.attempt_count), (TransitionStatus.BUDGET_EXHAUSTED, 1))
        self.assertEqual(self.reserved(conn), 1)
        self.assertEqual(ivs.load_invocation(conn, lease.invocation_id).attempt_count, 1)

    def test_reserved_never_below_attempts(self):
        conn = self.migrated()
        lease = self.claim(conn, 1).lease
        for second in range(1, 5):
            ivs.record_attempt(conn, lease, ROOMY, now=T0 + timedelta(seconds=second))
        attempts = ivs.load_invocation(conn, lease.invocation_id).attempt_count
        self.assertEqual(attempts, 4)
        self.assertEqual((self.reserved(conn, "hour"), self.reserved(conn, "day")), (4, 4))


# --- busy / locked --------------------------------------------------------------------------------


class TestBusyRetry(TempDbCase):
    def setUp(self):
        super().setUp()
        self.migrated()
        self.locker = self.connect()
        self.locker.execute("BEGIN IMMEDIATE")  # another writer holds the lock
        self.claimant = self.connect(timeout=0)

    def test_busy_is_retried_50_100_200_ms_then_fails_typed(self):
        delays = []
        with self.assertRaises(InvocationError) as caught:
            self.claim(self.claimant, 1, sleep=delays.append)
        self.assertEqual(caught.exception.code, InvocationFailure.BUSY)
        self.assertEqual(delays, [0.05, 0.1, 0.2])
        self.assertFalse(self.claimant.in_transaction)
        self.locker.rollback()
        self.assertEqual(self.rows(self.claimant, "invocations"), [])
        self.assertEqual(self.rows(self.claimant, "invocation_budget"), [])
        self.assertEqual(self.rows(self.claimant, "invocation_demand"), [])

    def test_lock_released_during_retries_then_claim_succeeds(self):
        delays = []

        def sleep(seconds):
            delays.append(seconds)
            if len(delays) == 2:
                self.locker.rollback()

        result = self.claim(self.claimant, 1, sleep=sleep)
        self.assertIsInstance(result, Claimed)
        self.assertEqual(delays, [0.05, 0.1])
        self.assertEqual(self.reserved(self.claimant), 1)

    def test_real_sleep_waits_the_bounded_schedule(self):
        started = time.monotonic()
        with self.assertRaises(InvocationError) as caught:
            self.claim(self.claimant, 1)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, InvocationFailure.BUSY)
        self.assertGreaterEqual(elapsed, 0.34)
        self.assertLess(elapsed, 5)
        self.locker.rollback()

    def test_holder_actions_fail_typed_when_busy(self):
        self.locker.rollback()
        lease = self.claim(self.claimant, 1).lease
        self.locker.execute("BEGIN IMMEDIATE")
        delays = []
        with self.assertRaises(InvocationError) as caught:
            ivs.complete_invocation(self.claimant, lease, now=T0 + timedelta(seconds=1), sleep=delays.append)
        self.assertEqual(caught.exception.code, InvocationFailure.BUSY)
        self.assertEqual(len(delays), 3)
        self.locker.rollback()
        self.assertIs(ivs.load_invocation(self.claimant, lease.invocation_id).state, InvocationState.CLAIMED)


# --- SnapshotStore seam ---------------------------------------------------------------------------


class TestStoreSeam(TempDbCase):
    def test_store_methods_claim_attempt_complete_and_load(self):
        s = SnapshotStore(self.path)
        self._stores.append(s)
        budget = ModelBudget(MODEL, 2, 5)
        result = s.claim_invocation(request(7), budget, "worker-a", 60)
        self.assertIsInstance(result, Claimed)
        self.assertIsInstance(s.claim_invocation(request(7), budget, "worker-b", 60), Duplicate)
        self.assertTrue(s.record_invocation_attempt(result.lease, budget).applied)
        self.assertEqual(s.recover_expired_invocations("worker-r", 60), ())
        self.assertTrue(s.complete_invocation(result.lease).applied)
        record = s.load_invocation(result.lease.invocation_id)
        self.assertEqual(record.identity, identity(7))
        self.assertEqual((record.model, record.state, record.demand_count, record.attempt_count), (MODEL, InvocationState.COMPLETED, 2, 1))
        other = s.claim_invocation(request(8), budget, "worker-a", 60)
        self.assertTrue(s.release_invocation(other.lease, ReleaseReason.DEADLINE_EXPIRED).applied)
        self.assertIsNone(s.load_invocation("inv-none"))


class TestAdapterBoundary(unittest.TestCase):
    def test_adapter_imports_no_notification_network_or_model_module(self):
        with open(ivs.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(("." * node.level) + (node.module or ""))
        self.assertEqual(
            imported,
            {"__future__", "sqlite3", "time", "uuid", "collections.abc", "datetime", "..domain.integrity",
             "..domain.invocation", "."},
        )


if __name__ == "__main__":
    unittest.main()
