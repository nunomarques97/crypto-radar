"""T030b: additive schema-version ledger, idempotent migration and the SQLite evidence adapter.

Every database here is a fixture created in a fresh temporary directory. The "old"
database is built the way the pre-T030b store built it (``store.SCHEMA`` plus the legacy
``ALTER TABLE`` columns), with rows in it, and without the ledger. Nothing reads or copies
``radar_state.sqlite`` or any other root state file.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config, store
from radar_v08.adapters import evidence_store as es
from radar_v08.adapters.evidence_store import (
    EVIDENCE_MIGRATION,
    LEDGER_MIGRATION,
    SCHEMA_MIGRATIONS,
    EvidenceStoreError,
    LinkedEvidence,
    Migration,
    MigrationFailure,
    SchemaMigrationError,
    StoreFailure,
)
from radar_v08.domain.evidence import (
    EvidenceRejected,
    EvidenceScope,
    FactKind,
    LegacyUnversionedEvidence,
    RejectionCode,
    make_fact,
    seal_evidence,
)
from radar_v08.domain.integrity import (
    Capability,
    CapabilityResult,
    CheckStatus,
    InstrumentId,
    InstrumentKind,
    IntegrityReport,
    TimeBasis,
)
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore

UTC = timezone.utc
NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
BTC_USD = InstrumentId("kraken", "XBT/USD", InstrumentKind.SPOT, "BTC", "USD", "BTC")
BTC_EUR = InstrumentId("kraken", "XBT/EUR", InstrumentKind.SPOT, "BTC", "EUR", "BTC")
ETH_USD = InstrumentId("kraken", "ETH/USD", InstrumentKind.SPOT, "ETH", "USD", "ETH")

LEGACY_CONTEXT = '{"asset": "BTC", "market": "SPOT", "current_price": 50000.0, "flags": ["WIDE_SPREAD"]}'
NEW_OBJECTS = {
    ("table", "schema_version_ledger"),
    ("trigger", "schema_version_ledger_no_update"),
    ("trigger", "schema_version_ledger_no_delete"),
    ("table", "evidence_versions"),
    ("index", "idx_evidence_versions_run_instrument"),
    ("trigger", "evidence_versions_immutable"),
    ("table", "event_evidence"),
    ("index", "idx_event_evidence_evidence"),
    ("trigger", "event_evidence_immutable"),
    # T031a, ledger version 3
    ("table", "invocations"),
    ("index", "uq_invocations_active_identity"),
    ("index", "idx_invocations_state_lease"),
    ("trigger", "invocations_identity_immutable"),
    ("trigger", "invocations_terminal_is_final"),
    ("trigger", "invocations_counters_never_decrease"),
    ("table", "invocation_budget"),
    ("trigger", "invocation_budget_never_decreases"),
    ("table", "invocation_demand"),
    ("trigger", "invocation_demand_never_decreases"),
}


def sealed_evidence(run_id="run-1", instrument=BTC_USD, bid="100.10"):
    scope = EvidenceScope(run_id=run_id, instrument=instrument, code_version="radar-0.8.0+test")
    ticker = make_fact(
        scope, FactKind.OBSERVATION, "ticker", {"bid": Decimal(bid), "ask": Decimal("100.5"), "unit": instrument.quote}
    )
    spread = make_fact(
        scope,
        FactKind.CALCULATION,
        "spread_bps",
        {"value": Decimal("39.88")},
        depends_on=(ticker.fact_id,),
        calculation_version="spread-v1",
    )
    result = CapabilityResult(
        Capability.SPOT_TICKER, instrument.symbol, CheckStatus.PASS, (), TimeBasis.RECEIPT_ONLY, NOW - timedelta(seconds=5)
    )
    report = IntegrityReport(evaluated_at=NOW, policy_version="OC-1", results=(result,))
    return seal_evidence(scope, (ticker, spread), report, NOW + timedelta(seconds=1))


def build_legacy_db(path):
    """A pre-T030b database: the old schema and old rows, no ledger, no evidence tables."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(store.SCHEMA)
        for column, sql_type in store._FORWARD_RETURNS_MIGRATION_COLUMNS.items():
            conn.execute(f"ALTER TABLE forward_returns ADD COLUMN {column} {sql_type}")
        for column, sql_type in store._EVENTS_MIGRATION_COLUMNS.items():
            conn.execute(f"ALTER TABLE events ADD COLUMN {column} {sql_type}")
        conn.execute(
            "INSERT INTO radar_runs (run_id, ts, mode, markets_seen) VALUES ('run-legacy', '2026-09-01T00:00:00+00:00', 'full', 12)"
        )
        conn.execute(
            "INSERT INTO events (event_id, dedup_key, ts, type, asset, setup_type, direction, market, anomaly_score, "
            "status, context_json, updated_ts) VALUES ('evt-legacy', 'k1', '2026-09-01T00:05:00+00:00', 'RADAR_ALERT', "
            "'BTC', 'BREAKOUT', 'LONG', 'SPOT', 70.0, 'PROCESSED', ?, '2026-09-01T00:06:00+00:00')",
            (LEGACY_CONTEXT,),
        )
        conn.execute(
            "INSERT INTO l2_feature_snapshots (run_id, asset, ts, entry_price, features_json) "
            "VALUES ('run-legacy', 'BTC', '2026-09-01T00:05:00+00:00', 50000.0, '{\"atr\": 1.5}')"
        )
        conn.execute(
            "INSERT INTO forward_returns (asset, ts, horizon_minutes, return_pct, pair) "
            "VALUES ('BTC', '2026-09-01T00:05:00+00:00', 15, 0.4, 'BTC/USD')"
        )
        conn.commit()
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
        return {
            (kind, name, sql)
            for kind, name, sql in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        }
    finally:
        conn.close()


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "fixture.sqlite")
        self.addCleanup(self.tmp.cleanup)
        self._stores = []
        self._conns = []

    def tearDown(self):
        for s in self._stores:
            s.close()
        for c in self._conns:
            c.close()

    def open_conn(self):
        conn = sqlite3.connect(self.path)
        self._conns.append(conn)
        return conn

    def open_store(self):
        s = SnapshotStore(self.path)
        self._stores.append(s)
        return s


class TestLegacyMigration(TempDbCase):
    def setUp(self):
        super().setUp()
        build_legacy_db(self.path)

    def test_old_database_has_no_ledger_and_needs_both_migrations(self):
        conn = self.open_conn()
        self.assertEqual(es.read_ledger(conn), ())
        self.assertEqual(es.pending_migrations(conn), SCHEMA_MIGRATIONS)

    def test_migration_is_additive_only(self):
        before = objects(self.path)
        legacy_dump, _ = snapshot(self.path)
        conn = self.open_conn()
        self.assertEqual(es.apply_schema_migrations(conn, now=NOW), (1, 2, 3))
        after = objects(self.path)
        # Every pre-existing object is still there with the identical SQL; only new ones were added.
        self.assertTrue(before <= after)
        self.assertEqual({(kind, name) for kind, name, _ in after - before}, NEW_OBJECTS)
        # Every legacy row is still there, byte for byte.
        new_dump, _ = snapshot(self.path)
        legacy_rows = [line for line in legacy_dump if line.startswith("INSERT INTO")]
        self.assertTrue(legacy_rows)
        for line in legacy_rows:
            self.assertIn(line, new_dump)

    def test_ledger_records_each_version_with_checksum_and_real_time(self):
        conn = self.open_conn()
        es.apply_schema_migrations(conn, now=NOW + timedelta(hours=2, minutes=3))
        ledger = es.read_ledger(conn)
        self.assertEqual([entry.version for entry in ledger], [1, 2, 3])
        self.assertEqual(
            [entry.name for entry in ledger],
            ["schema_version_ledger", "evidence_versions_and_event_links", "invocations_budget_and_demand"],
        )
        self.assertEqual(ledger[0].checksum, LEDGER_MIGRATION.checksum)
        self.assertEqual(ledger[1].checksum, EVIDENCE_MIGRATION.checksum)
        self.assertTrue(ledger[1].checksum.startswith("sha256:") and len(ledger[1].checksum) == 71)
        self.assertEqual({entry.applied_at for entry in ledger}, {"2026-09-18T14:03:00+00:00"})

    def test_no_backfill_old_rows_get_no_evidence_or_links(self):
        s = self.open_store()
        conn = self.open_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0], 0)
        self.assertEqual([entry.version for entry in s.schema_ledger()], [1, 2, 3])

    def test_old_event_reads_as_legacy_unversioned_with_only_real_fields(self):
        s = self.open_store()
        record = s.load_event_evidence("evt-legacy")
        self.assertIsInstance(record, LegacyUnversionedEvidence)
        self.assertEqual(record.state.value, "legacy-unversioned")
        self.assertEqual(record.source_ref, "events:evt-legacy")
        # The events table has no run_id or symbol column: nothing is guessed from asset or context_json.
        self.assertIsNone(record.run_id)
        self.assertIsNone(record.symbol)
        self.assertIn("asset", record.fields_present)
        self.assertIn("context_json", record.fields_present)
        self.assertNotIn("opportunity_score", record.fields_present)  # NULL in the old row
        self.assertNotIn("schema_version", record.fields_present)
        # The old row itself is unchanged and still readable through the normal API.
        row = s.get_event("evt-legacy")
        self.assertEqual(row["context_json"], LEGACY_CONTEXT)
        self.assertEqual(row["status"], "PROCESSED")

    def test_second_run_changes_nothing(self):
        conn = self.open_conn()
        self.assertEqual(es.apply_schema_migrations(conn, now=NOW), (1, 2, 3))
        conn.close()
        self._conns.remove(conn)
        first = snapshot(self.path)
        conn = self.open_conn()
        changes_before = conn.total_changes
        self.assertEqual(es.apply_schema_migrations(conn, now=NOW + timedelta(days=1)), ())
        self.assertEqual(conn.total_changes, changes_before)
        self.assertFalse(conn.in_transaction)
        conn.close()
        self._conns.remove(conn)
        self.assertEqual(snapshot(self.path), first)  # same dump and same schema cookie

    def test_reopening_the_store_twice_changes_nothing(self):
        self.open_store().close()
        self._stores.clear()
        first = snapshot(self.path)
        s = self.open_store()
        self.assertEqual(s.migrate_schema(), ())
        s.close()
        self._stores.clear()
        self.assertEqual(snapshot(self.path), first)

    def test_second_connection_sees_current_ledger(self):
        a = self.open_conn()
        b = self.open_conn()
        self.assertEqual(es.apply_schema_migrations(a, now=NOW), (1, 2, 3))
        self.assertEqual(es.apply_schema_migrations(b, now=NOW), ())


class TestInjectedFailure(TempDbCase):
    BAD = Migration(3, "injected_failure", ("CREATE TABLE injected_ok (x INTEGER)", "INSERT INTO no_such_table VALUES (1)"))
    # T031a: the next version after the real plan (now 4), for failures on a migrated db.
    BAD_NEXT = Migration(len(SCHEMA_MIGRATIONS) + 1, "injected_failure", BAD.statements)

    def test_failure_in_the_middle_of_first_migration_leaves_old_db_untouched(self):
        build_legacy_db(self.path)
        before = snapshot(self.path)
        conn = self.open_conn()
        with self.assertRaises(SchemaMigrationError) as caught:
            es.apply_schema_migrations(conn, now=NOW, migrations=(LEDGER_MIGRATION, EVIDENCE_MIGRATION, self.BAD))
        self.assertEqual(caught.exception.code, MigrationFailure.STATEMENT_FAILED)
        self.assertEqual(caught.exception.version, 3)
        self.assertIn("statement 2/2", caught.exception.detail)
        self.assertFalse(conn.in_transaction)
        conn.close()
        self._conns.remove(conn)
        # Versions 1 and 2 ran in the same transaction; all of it is rolled back.
        self.assertEqual(snapshot(self.path), before)

    def test_failure_on_an_already_migrated_db_keeps_its_ledger(self):
        build_legacy_db(self.path)
        conn = self.open_conn()
        es.apply_schema_migrations(conn, now=NOW)
        conn.close()
        self._conns.remove(conn)
        before = snapshot(self.path)
        conn = self.open_conn()
        with self.assertRaises(SchemaMigrationError):
            es.apply_schema_migrations(conn, now=NOW, migrations=(*SCHEMA_MIGRATIONS, self.BAD_NEXT))
        self.assertEqual([entry.version for entry in es.read_ledger(conn)], [1, 2, 3])
        conn.close()
        self._conns.remove(conn)
        self.assertEqual(snapshot(self.path), before)

    def test_unexpected_existing_object_fails_mid_migration_and_rolls_back(self):
        # event_evidence is the 4th statement of version 2: the ledger and evidence_versions
        # are created first inside the transaction, then the clash aborts everything.
        build_legacy_db(self.path)
        conn = self.open_conn()
        conn.execute("CREATE TABLE event_evidence (unrelated TEXT)")
        conn.commit()
        conn.close()
        self._conns.remove(conn)
        before = snapshot(self.path)
        with self.assertRaises(SchemaMigrationError) as caught:
            SnapshotStore(self.path)
        self.assertEqual(caught.exception.code, MigrationFailure.STATEMENT_FAILED)
        self.assertEqual(caught.exception.version, 2)
        self.assertIn("statement 4/6", caught.exception.detail)
        self.assertEqual(snapshot(self.path), before)
        names = {name for _, name, _ in objects(self.path)}
        self.assertNotIn("schema_version_ledger", names)
        self.assertNotIn("evidence_versions", names)


class TestLedgerTrust(TempDbCase):
    def setUp(self):
        super().setUp()
        build_legacy_db(self.path)
        self.conn = self.open_conn()
        es.apply_schema_migrations(self.conn, now=NOW)

    def test_ledger_is_append_only(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE schema_version_ledger SET checksum = 'x' WHERE version = 1")
        self.conn.rollback()
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM schema_version_ledger WHERE version = 2")
        self.conn.rollback()
        self.assertEqual(len(es.read_ledger(self.conn)), len(SCHEMA_MIGRATIONS))

    def test_newer_database_is_refused_not_downgraded(self):
        newer = Migration(len(SCHEMA_MIGRATIONS) + 1, "future", ("CREATE TABLE future_table (x INTEGER)",))
        es.apply_schema_migrations(self.conn, now=NOW, migrations=(*SCHEMA_MIGRATIONS, newer))
        with self.assertRaises(SchemaMigrationError) as caught:
            es.apply_schema_migrations(self.conn, now=NOW)
        self.assertEqual(caught.exception.code, MigrationFailure.UNKNOWN_VERSION)
        with self.assertRaises(SchemaMigrationError):
            SnapshotStore(self.path)

    def test_changed_checksum_is_refused(self):
        self.conn.execute("DROP TRIGGER schema_version_ledger_no_update")
        self.conn.execute("UPDATE schema_version_ledger SET checksum = 'sha256:forged' WHERE version = 2")
        self.conn.commit()
        with self.assertRaises(SchemaMigrationError) as caught:
            es.apply_schema_migrations(self.conn, now=NOW)
        self.assertEqual(caught.exception.code, MigrationFailure.CHECKSUM_MISMATCH)
        self.assertEqual(caught.exception.version, 2)

    def test_ledger_gap_is_refused(self):
        self.conn.execute("DROP TRIGGER schema_version_ledger_no_delete")
        self.conn.execute("DELETE FROM schema_version_ledger WHERE version = 1")
        self.conn.commit()
        with self.assertRaises(SchemaMigrationError) as caught:
            es.pending_migrations(self.conn)
        self.assertEqual(caught.exception.code, MigrationFailure.LEDGER_GAP)

    def test_open_transaction_naive_clock_and_bad_plan_are_refused(self):
        fresh = os.path.join(self.tmp.name, "fresh.sqlite")
        conn = sqlite3.connect(fresh)
        self._conns.append(conn)
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")  # opens an implicit transaction
        with self.assertRaises(SchemaMigrationError) as caught:
            es.apply_schema_migrations(conn, now=NOW)
        self.assertEqual(caught.exception.code, MigrationFailure.OPEN_TRANSACTION)
        conn.commit()
        with self.assertRaises(SchemaMigrationError) as caught:
            es.apply_schema_migrations(conn, now=datetime(2026, 9, 18, 12, 0))
        self.assertEqual(caught.exception.code, MigrationFailure.INVALID_CLOCK)
        for plan in ((EVIDENCE_MIGRATION,), (LEDGER_MIGRATION, Migration(3, "skip", ("SELECT 1",)))):
            with self.assertRaises(SchemaMigrationError) as caught:
                es.apply_schema_migrations(conn, now=NOW, migrations=plan)
            self.assertEqual(caught.exception.code, MigrationFailure.INVALID_PLAN)
        self.assertEqual(es.read_ledger(conn), ())


class TestEvidenceRows(TempDbCase):
    def setUp(self):
        super().setUp()
        self._events_log = config.EVENTS_LOG_PATH
        config.EVENTS_LOG_PATH = os.path.join(self.tmp.name, "events.jsonl")
        self.store = self.open_store()
        self.evidence = sealed_evidence()
        self.event_id, _ = create_event_if_new(
            self.store, ts="2026-09-18T12:00:00+00:00", type_="RADAR_ALERT", asset="BTC",
            setup_type="BREAKOUT", direction="LONG", market="SPOT",
            anomaly_score=70.0, opportunity_score=80.0, tradeability_score=85.0,
            confidence="HIGH", model_demand="FABLE", reason="test", status="PENDING",
            context={"asset": "BTC", "market": "SPOT"},
        )

    def tearDown(self):
        config.EVENTS_LOG_PATH = self._events_log
        super().tearDown()

    def raw(self, sql, params=()):
        conn = self.open_conn()
        conn.execute(sql, params)
        conn.commit()

    def test_save_is_idempotent_and_load_round_trips(self):
        self.assertTrue(self.store.save_evidence(self.evidence))
        self.assertFalse(self.store.save_evidence(self.evidence))
        self.assertEqual(self.store.load_evidence(self.evidence.evidence_id), self.evidence)
        self.assertIsNone(self.store.load_evidence("evidence:sha256:" + "0" * 64))
        row = self.open_conn().execute(
            "SELECT run_id, venue, symbol, schema_version FROM evidence_versions"
        ).fetchall()
        self.assertEqual(row, [("run-1", "kraken", "XBT/USD", "evidence-v1")])

    def test_stored_evidence_is_immutable(self):
        self.store.save_evidence(self.evidence)
        conn = self.open_conn()
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE evidence_versions SET run_id = 'run-2'")
        conn.rollback()

    def test_tampered_record_or_identity_column_is_rejected_on_load(self):
        self.store.save_evidence(self.evidence)
        self.raw("DROP TRIGGER evidence_versions_immutable")
        self.raw("UPDATE evidence_versions SET run_id = 'run-2'")
        with self.assertRaises(EvidenceRejected) as caught:
            self.store.load_evidence(self.evidence.evidence_id)
        self.assertEqual(caught.exception.code, RejectionCode.RUN_MISMATCH)
        self.assertEqual(caught.exception.field, "evidence_versions.run_id")
        self.raw("UPDATE evidence_versions SET run_id = 'run-1', symbol = 'XBT/EUR'")
        with self.assertRaises(EvidenceRejected) as caught:
            self.store.load_evidence(self.evidence.evidence_id)
        self.assertEqual(caught.exception.code, RejectionCode.INSTRUMENT_MISMATCH)
        self.raw("UPDATE evidence_versions SET symbol = 'XBT/USD', record_json = replace(record_json, '100.1', '100.2')")
        with self.assertRaises(EvidenceRejected) as caught:
            self.store.load_evidence(self.evidence.evidence_id)
        self.assertEqual(caught.exception.code, RejectionCode.HASH_MISMATCH)

    def test_only_sealed_evidence_can_be_stored(self):
        with self.assertRaises(EvidenceRejected) as caught:
            self.store.save_evidence(LegacyUnversionedEvidence(source_ref="events:x"))
        self.assertEqual(caught.exception.code, RejectionCode.LEGACY_UNVERSIONED)

    def test_evidence_calls_on_an_unmigrated_db_are_refused(self):
        legacy = os.path.join(self.tmp.name, "legacy.sqlite")
        build_legacy_db(legacy)
        conn = sqlite3.connect(legacy)
        self._conns.append(conn)
        for call in (
            lambda: es.save_evidence(conn, self.evidence, now=NOW),
            lambda: es.load_evidence(conn, self.evidence.evidence_id),
            lambda: es.load_event_evidence(conn, "evt-legacy"),
        ):
            with self.assertRaises(EvidenceStoreError) as caught:
                call()
            self.assertEqual(caught.exception.code, StoreFailure.SCHEMA_NOT_MIGRATED)

    def test_link_is_verified_before_it_is_written(self):
        self.store.save_evidence(self.evidence)
        cases = (
            ("run-2", BTC_USD, self.evidence.evidence_id, RejectionCode.RUN_MISMATCH),
            ("run-1", BTC_EUR, self.evidence.evidence_id, RejectionCode.INSTRUMENT_MISMATCH),
            ("run-1", BTC_USD, "evidence:sha256:" + "f" * 64, RejectionCode.UNKNOWN_DEPENDENCY),
        )
        for run_id, instrument, evidence_id, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(EvidenceRejected) as caught:
                    self.store.link_event_evidence(self.event_id, evidence_id, run_id, instrument)
                self.assertEqual(caught.exception.code, code)
        count = self.open_conn().execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0]
        self.assertEqual(count, 0)

    def test_link_rejects_evidence_for_another_asset_and_unknown_event(self):
        eth = sealed_evidence(instrument=ETH_USD)
        self.store.save_evidence(eth)
        with self.assertRaises(EvidenceRejected) as caught:
            self.store.link_event_evidence(self.event_id, eth.evidence_id, "run-1", ETH_USD)
        self.assertEqual(caught.exception.code, RejectionCode.INSTRUMENT_MISMATCH)
        self.assertEqual(caught.exception.field, "event.asset")
        with self.assertRaises(EvidenceStoreError) as caught:
            self.store.link_event_evidence("no-such-event", eth.evidence_id, "run-1", ETH_USD)
        self.assertEqual(caught.exception.code, StoreFailure.EVENT_NOT_FOUND)

    def test_link_is_idempotent_and_immutable(self):
        self.store.save_evidence(self.evidence)
        other = sealed_evidence(bid="100.20")
        self.store.save_evidence(other)
        self.assertTrue(self.store.link_event_evidence(self.event_id, self.evidence.evidence_id, "run-1", BTC_USD))
        self.assertFalse(self.store.link_event_evidence(self.event_id, self.evidence.evidence_id, "run-1", BTC_USD))
        with self.assertRaises(EvidenceStoreError) as caught:
            self.store.link_event_evidence(self.event_id, other.evidence_id, "run-1", BTC_USD)
        self.assertEqual(caught.exception.code, StoreFailure.LINK_CONFLICT)
        linked = self.store.load_event_evidence(self.event_id)
        self.assertIsInstance(linked, LinkedEvidence)
        self.assertEqual(linked.record, self.evidence)
        self.assertEqual(linked.link.run_id, "run-1")
        self.assertEqual(linked.link.instrument, BTC_USD)
        self.assertIsNotNone(linked.link.linked_at.tzinfo)

    def test_unlinked_new_event_is_legacy_unversioned(self):
        record = self.store.load_event_evidence(self.event_id)
        self.assertIsInstance(record, LegacyUnversionedEvidence)
        self.assertEqual(record.source_ref, f"events:{self.event_id}")
        with self.assertRaises(EvidenceStoreError) as caught:
            self.store.load_event_evidence("no-such-event")
        self.assertEqual(caught.exception.code, StoreFailure.EVENT_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
