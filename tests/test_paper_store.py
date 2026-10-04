"""Paper game store (radar_v08/adapters/paper_store.py) on temporary SQLite files only.

Additive schema outside the ledger, append-only rows, CHECK constraints, frozen terms and
EX-1 exit levels per play, the additive EX-1 migration of a pre-EX-1 database, closes at
the executable price of the observation that touches a level, and one BEGIN IMMEDIATE
transaction per write that rolls back fully on error.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_legacy as legacy  # noqa: E402  (pre-EX-1 rows, test fixture)

from radar_v08.adapters import paper_store as ps  # noqa: E402
from radar_v08.domain.paper import (  # noqa: E402
    Direction,
    ExitReason,
    Outcome,
    SkipReason,
)
from radar_v08.store import SnapshotStore  # noqa: E402

D = Decimal
T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
DUE = T0 + timedelta(hours=24)
TERMS = ps.PlayTerms(stake=D("100"), fee_bps=D("26"), max_open=3)


def candidate(event_id: str, asset: str = "BTC", *, direction: object = "LONG", bid: object = 99.0,
              ask: object = 101.0, ts: datetime = T0, status: object = "online", atr: object = 1.5,
              atr_pair: object = "same") -> ps.PaperCandidate:
    pair = f"{asset}/EUR"
    return ps.PaperCandidate(
        event_id=event_id,
        run_id="run-1",
        asset=asset,
        pair=pair,
        quote="EUR",
        direction=direction,
        bid=bid,
        ask=ask,
        snapshot_ts=ts.isoformat(),
        status=status,
        why={"setup_type": "BREAKOUT", "direction": direction, "scores": {"final": 0.8}, "features": {"rvol": 3.2}},
        atr=atr,
        atr_pair=pair if atr_pair == "same" else atr_pair,
    )


def add_spot(conn: sqlite3.Connection, pair: str, ts: datetime, bid: object, ask: object, status: object = "online") -> int:
    asset = pair.split("/")[0]
    cursor = conn.execute(
        "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES (?, ?, 'EUR', ?, ?, ?, ?)",
        (asset, pair, ts.isoformat(), bid, ask, status),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def ticker(pair: str, at: datetime, bid: object, ask: object) -> ps.ObservedQuote:
    return ps.ObservedQuote(pair=pair, bid=bid, ask=ask, observed_at=at, source="ticker")


def schema_rows(conn: sqlite3.Connection, *, paper: bool | None = None) -> set[tuple[str, str, str, str]]:
    rows = {tuple(row) for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()}
    if paper is None:
        return rows  # type: ignore[return-value]
    return {row for row in rows if (row[2] in ps.TABLES) is paper}  # type: ignore[misc]


def table_rows(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]


class PaperDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="paper-store-")
        # Registered first, so it runs last: after every connection a test opened is closed.
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "radar_state.sqlite")
        SnapshotStore(self.path).close()  # every existing radar table, as in production
        self.conn = self.connect()
        self.addCleanup(lambda: self.conn.close())  # the connection current at the end

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def ready(self, start: str = "1000") -> None:
        ps.ensure_schema(self.conn)
        ps.ensure_wallet(self.conn, start_balance=D(start), currency="EUR", now=T0)

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class TestSchema(PaperDbCase):
    def test_ensure_schema_is_additive_and_outside_the_ledger(self):
        before = schema_rows(self.conn)
        ledger = self.conn.execute("SELECT * FROM schema_version_ledger ORDER BY version").fetchall()
        self.assertFalse(ps.schema_present(self.conn))
        self.assertFalse(ps.schema_current(self.conn))
        self.assertTrue(ps.ensure_schema(self.conn))
        self.assertTrue(ps.schema_present(self.conn))
        self.assertTrue(ps.schema_current(self.conn))
        # Every existing table, index and trigger is byte-identical in schema.
        self.assertEqual(schema_rows(self.conn, paper=False), before)
        self.assertEqual(
            [tuple(row) for row in self.conn.execute("SELECT * FROM schema_version_ledger ORDER BY version")],
            [tuple(row) for row in ledger],
        )
        created = {
            (row[0], row[1]) for row in schema_rows(self.conn, paper=True) if not row[1].startswith("sqlite_autoindex_")
        }
        self.assertEqual(created, set(ps.SCHEMA_OBJECTS))
        for statement in ps.SCHEMA_STATEMENTS:
            self.assertRegex(statement, r"^CREATE (TABLE|TRIGGER) IF NOT EXISTS ")
            self.assertNotIn("ALTER", statement.upper())
            self.assertNotIn("DROP", statement.upper())
        for statement in ps.MIGRATION_STATEMENTS:
            self.assertRegex(statement, r"^ALTER TABLE paper_(plays|closes) ADD COLUMN \w+ (TEXT|REAL) CHECK \(\w+ IS NULL OR ")
            self.assertNotIn("NOT NULL", statement.upper())
            self.assertNotIn("DEFAULT", statement.upper())
        for table, columns in (
            ("paper_plays", {"exit_policy", "atr", "stop_price", "target_price"}),
            ("paper_closes", {"exit_reason", "exit_source", "record_lag_seconds"}),
        ):
            present = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            self.assertLessEqual(columns, present)

    def test_ensure_schema_writes_nothing_when_present(self):
        self.ready()
        self.conn.close()
        before = Path(self.path).read_bytes()
        self.conn = self.connect()
        changes = self.conn.total_changes
        self.assertFalse(ps.ensure_schema(self.conn))
        self.assertEqual(ps.ensure_wallet(self.conn, start_balance=D("5"), currency="USD", now=T0).start_balance, D("1000"))
        self.assertEqual(self.conn.total_changes, changes)
        self.assertFalse(self.conn.in_transaction)
        self.conn.close()
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.conn = self.connect()

    def test_snapshot_store_does_not_create_paper_tables(self):
        store = SnapshotStore(self.path)
        store.close()
        self.assertFalse(ps.schema_present(self.conn))
        self.assertEqual(schema_rows(self.conn, paper=True), set())

    def test_refuses_an_open_transaction(self):
        self.conn.execute("BEGIN")
        with self.assertRaises(ps.PaperStoreError) as caught:
            ps.ensure_schema(self.conn)
        self.assertIs(caught.exception.code, ps.PaperStoreFailure.OPEN_TRANSACTION)
        self.conn.rollback()


class TestLegacyMigration(PaperDbCase):
    """A database written by the pre-EX-1 code: open and closed legacy plays."""

    def build_legacy(self) -> tuple[int, int]:
        legacy.create_legacy_schema(self.conn, at=T0)
        closed = legacy.insert_legacy_play(self.conn, "old-1", "BTC", "LONG", 99.0, 101.0, T0)
        still_open = legacy.insert_legacy_play(self.conn, "old-2", "ETH", "SHORT", 199.0, 201.0, T0, hold_minutes=45)
        snapshot = add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=61), 109.0, 111.0)
        legacy.insert_legacy_close(
            self.conn, closed, "109.0", "111.0", T0 + timedelta(minutes=61), T0 + timedelta(minutes=60),
            snapshot_id=snapshot, gross=1000, spread=208, fees=54, outcome="WIN", closed_at=T0 + timedelta(minutes=62),
        )
        return closed, still_open

    def test_migration_adds_columns_and_keeps_every_old_value(self):
        self.build_legacy()
        self.assertTrue(ps.schema_present(self.conn))  # readable before the migration
        self.assertFalse(ps.schema_current(self.conn))
        ledger = table_rows(self.conn, "schema_version_ledger")
        before = {table: table_rows(self.conn, table) for table in ps.BASE_TABLES}
        other = schema_rows(self.conn, paper=False)

        self.assertTrue(ps.ensure_schema(self.conn))
        self.assertTrue(ps.schema_current(self.conn))
        for table, rows in before.items():
            with self.subTest(table=table):
                after = table_rows(self.conn, table)
                self.assertEqual(len(after), len(rows))
                added = sum(1 for column in ps.ADDED_COLUMNS if column[0] == table)
                for old, new in zip(rows, after, strict=True):
                    self.assertEqual(new[: len(old)], old)  # every old value unchanged
                    self.assertEqual(new[len(old):], (None,) * added)  # new columns NULL
        self.assertEqual(table_rows(self.conn, "schema_version_ledger"), ledger)
        self.assertEqual(schema_rows(self.conn, paper=False), other)
        self.assertEqual(self.count("paper_exit_quotes"), 0)

        # Idempotent: a second run writes nothing, not even to the file.
        self.conn.close()
        before_bytes = Path(self.path).read_bytes()
        self.conn = self.connect()
        changes = self.conn.total_changes
        self.assertFalse(ps.ensure_schema(self.conn))
        self.assertEqual(self.conn.total_changes, changes)
        self.conn.close()
        self.assertEqual(Path(self.path).read_bytes(), before_bytes)
        self.conn = self.connect()

    def test_checks_and_append_only_triggers_still_hold_after_migration(self):
        closed, _ = self.build_legacy()
        ps.ensure_schema(self.conn)
        for statement in (
            "UPDATE paper_plays SET stop_price = '1'",
            "DELETE FROM paper_plays",
            "UPDATE paper_closes SET exit_reason = 'stop'",
            "DELETE FROM paper_closes",
            "UPDATE paper_wallet SET currency = 'USD'",
            "INSERT INTO paper_exit_quotes (play_id, pair, bid, ask, observed_at, source, recorded_at) "
            "VALUES (1, 'BTC/EUR', '1', '1', 'b', 'spot_snapshot', 'c')",
            "INSERT INTO paper_exit_quotes (play_id, pair, bid, ask, observed_at, source, recorded_at) "
            "VALUES (1, 'BTC/EUR', '1', '1', 'b', 'ticker', 'a')",
            f"INSERT INTO paper_closes (play_id, exit_bid, exit_ask, exit_ts, exit_snapshot_id, due_at, delay_seconds, "
            f"gross_mid_cents, spread_cost_cents, fees_cents, net_cents, outcome, closed_at, exit_reason) VALUES "
            f"({closed + 1}, '1', '1', 'z', 1, 'a', 0, 0, 0, 0, 0, 'FLAT', 'x', 'trailing')",
            f"INSERT INTO paper_closes (play_id, exit_bid, exit_ask, exit_ts, exit_snapshot_id, due_at, delay_seconds, "
            f"gross_mid_cents, spread_cost_cents, fees_cents, net_cents, outcome, closed_at, record_lag_seconds) VALUES "
            f"({closed + 1}, '1', '1', 'z', 1, 'a', 0, 0, 0, 0, 0, 'FLAT', 'x', -1)",
            f"INSERT INTO paper_closes (play_id, exit_bid, exit_ask, exit_ts, exit_snapshot_id, due_at, delay_seconds, "
            f"gross_mid_cents, spread_cost_cents, fees_cents, net_cents, outcome, closed_at) VALUES "
            f"({closed + 1}, '1', '1', 'a', 1, 'z', 0, 0, 0, 0, 0, 'FLAT', 'x')",  # exit_ts < due_at
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(statement)
            self.conn.rollback()

    def test_unmigrated_database_reads_read_only_with_new_fields_none(self):
        closed, still_open = self.build_legacy()
        self.conn.close()
        before = Path(self.path).read_bytes()
        reader = sqlite3.connect(Path(self.path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            self.assertTrue(ps.schema_present(reader))
            self.assertFalse(ps.schema_current(reader))
            plays = ps.read_plays(reader)
            closes = ps.read_closes(reader)
            (open_play,) = ps.read_open_plays(reader)
            self.assertEqual(ps.current_balance(reader), D("1007.38"))
        finally:
            reader.close()
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.conn = self.connect()
        self.assertEqual([play.play_id for play in plays], [closed, still_open])
        self.assertEqual(open_play.play_id, still_open)
        for play in plays:
            self.assertEqual((play.exit_policy, play.atr, play.stop, play.target), (None, None, None, None))
            self.assertFalse(play.has_levels)
        self.assertEqual(plays[1].hold_minutes, 45)
        (close,) = closes
        self.assertEqual((close.exit_reason, close.exit_source, close.record_lag_seconds), (None, None, None))
        self.assertEqual((close.net, close.outcome), (D("7.38"), Outcome.WIN))

    def test_open_legacy_play_closes_by_the_old_rule_after_migration(self):
        _, still_open = self.build_legacy()
        ps.ensure_schema(self.conn)
        due = T0 + timedelta(minutes=45)
        add_spot(self.conn, "ETH/EUR", due - timedelta(seconds=1), 150.0, 151.0)  # touches nothing: before due
        # An extra quote is never used for a legacy play.
        self.assertEqual(
            ps.close_plays(self.conn, now=due + timedelta(minutes=1),
                           extra_quotes=[ticker("ETH/EUR", due + timedelta(seconds=5), 150.0, 151.0)]),
            ps.SettleReport((), (still_open,)),
        )
        add_spot(self.conn, "ETH/EUR", due + timedelta(minutes=2), 189.0, 191.0)
        (close,) = ps.settle_due(self.conn, now=due + timedelta(minutes=3)).closed
        self.assertEqual(close.play_id, still_open)
        self.assertEqual((close.exit_bid, close.exit_ask, close.exit_ts), (D("189.0"), D("191.0"), due + timedelta(minutes=2)))
        self.assertEqual((close.due_at, close.delay_seconds), (due, 120.0))
        self.assertEqual((close.exit_reason, close.exit_source, close.record_lag_seconds), (None, None, None))
        self.assertEqual(self.count("paper_exit_quotes"), 0)


class TestWallet(PaperDbCase):
    def test_start_balance_recorded_once(self):
        ps.ensure_schema(self.conn)
        self.assertIsNone(ps.read_wallet(self.conn))
        self.assertIsNone(ps.current_balance(self.conn))
        wallet = ps.ensure_wallet(self.conn, start_balance=D("1000"), currency="EUR", now=T0)
        self.assertEqual((wallet.start_balance, wallet.currency), (D("1000"), "EUR"))
        later = ps.ensure_wallet(self.conn, start_balance=D("2000"), currency="EUR", now=T0 + timedelta(days=1))
        self.assertEqual(later, wallet)
        self.assertEqual(self.count("paper_wallet"), 1)
        self.assertEqual(ps.current_balance(self.conn), D("1000"))

    def test_rejects_fractional_cents_and_bad_currency(self):
        ps.ensure_schema(self.conn)
        for bad in (D("10.001"), D("0"), D("-1"), D("1e400"), D("1000000000.01"), 1000):
            with self.subTest(bad=bad), self.assertRaises(ps.PaperStoreError):
                ps.ensure_wallet(self.conn, start_balance=bad, currency="EUR", now=T0)  # type: ignore[arg-type]
        with self.assertRaises(ps.PaperStoreError):
            ps.ensure_wallet(self.conn, start_balance=D("10"), currency=" ", now=T0)
        with self.assertRaises(ps.PaperStoreError):
            ps.ensure_wallet(self.conn, start_balance=D("10"), currency="EUR", now=datetime(2026, 9, 29))
        self.assertEqual(self.count("paper_wallet"), 0)

    def test_open_requires_a_wallet(self):
        ps.ensure_schema(self.conn)
        with self.assertRaises(ps.PaperStoreError) as caught:
            ps.open_candidates(self.conn, [candidate("e1")], terms=TERMS, now=T0)
        self.assertIs(caught.exception.code, ps.PaperStoreFailure.NO_WALLET)
        self.assertEqual(self.count("paper_plays"), 0)


class TestOpen(PaperDbCase):
    def test_play_freezes_its_terms_levels_and_why(self):
        self.ready()
        report = ps.open_candidates(self.conn, [candidate("e1", bid=99.5, ask=100.5, atr=1.25)], terms=TERMS, now=T0)
        self.assertEqual(report.skipped, ())
        (play,) = report.opened
        self.assertEqual(
            (play.event_id, play.asset, play.pair, play.quote, play.direction),
            ("e1", "BTC", "BTC/EUR", "EUR", Direction.LONG),
        )
        self.assertEqual((play.stake, play.fee_bps, play.hold_minutes), (D("100"), D("26"), 1440))
        self.assertEqual((play.entry_bid, play.entry_ask), (D("99.5"), D("100.5")))
        self.assertEqual((play.entry_ts, play.due_at), (T0, T0 + timedelta(hours=24)))
        # LONG: entry = ask 100.5; stop = 100.5 - 2*1.25 = 98.0; target = 100.5 + 2*2.5 = 105.5
        self.assertEqual(
            (play.exit_policy, play.atr, play.stop, play.target), ("ex1_initial_paper_v1", D("1.25"), D("98.00"), D("105.50"))
        )
        self.assertTrue(play.has_levels)
        self.assertEqual(play.why["setup_type"], "BREAKOUT")
        self.assertEqual(play.why["features"], {"rvol": 3.2})
        self.assertEqual(ps.read_open_plays(self.conn), (play,))

    def test_short_levels_are_mirrored_on_the_bid(self):
        self.ready()
        (play,) = ps.open_candidates(self.conn, [candidate("e1", direction="SHORT", atr=1.5)], terms=TERMS, now=T0).opened
        # SHORT: entry = bid 99; stop = 99 + 3 = 102; target = 99 - 6 = 93
        self.assertEqual((play.stop, play.target), (D("102.0"), D("93.0")))

    def test_skip_reasons(self):
        self.ready("250")
        report = ps.open_candidates(
            self.conn,
            [
                candidate("e1", "BTC"),
                candidate("e2", "BTC"),  # same asset already open
                candidate("e3", "ETH", direction="NONE"),
                candidate("e4", "ETH", bid=None),
                candidate("e5", "ETH", bid=0.0),
                candidate("e6", "ETH", bid=2.0, ask=1.0),
                candidate("e7", "ETH", status="offline"),
                candidate("a1", "ETH", atr=None),
                candidate("a2", "ETH", atr=float("nan")),
                candidate("a3", "ETH", atr=float("inf")),
                candidate("a4", "ETH", atr=0.0),
                candidate("a5", "ETH", atr=-1.0),
                candidate("a6", "ETH", atr="1.5"),
                candidate("a7", "ETH", atr_pair="XETHZEUR"),
                candidate("a8", "ETH", atr_pair=None),
                candidate("l1", "ETH", atr=50.5),  # LONG stop = 101 - 101 = 0
                candidate("l2", "ETH", direction="SHORT", atr=24.75),  # SHORT target = 99 - 99 = 0
                candidate("e8", "ETH", direction="SHORT"),
                candidate("e9", "SOL"),  # 250 - 200 open = 50 < 100
            ],
            terms=TERMS,
            now=T0,
        )
        self.assertEqual([play.event_id for play in report.opened], ["e1", "e8"])
        self.assertEqual(
            [(skip.event_id, skip.reason, skip.detail) for skip in report.skipped if not skip.event_id.startswith("l")],
            [
                ("e2", SkipReason.ASSET_ALREADY_OPEN, None),
                ("e3", SkipReason.NO_DIRECTION, None),
                ("e4", SkipReason.INVALID_PRICE, "missing"),
                ("e5", SkipReason.INVALID_PRICE, "not_positive"),
                ("e6", SkipReason.INVALID_PRICE, "crossed"),
                ("e7", SkipReason.INVALID_PRICE, "not_online"),
                ("a1", SkipReason.NO_VALID_ATR, "missing"),
                ("a2", SkipReason.NO_VALID_ATR, "not_finite"),
                ("a3", SkipReason.NO_VALID_ATR, "not_finite"),
                ("a4", SkipReason.NO_VALID_ATR, "not_positive"),
                ("a5", SkipReason.NO_VALID_ATR, "not_positive"),
                ("a6", SkipReason.NO_VALID_ATR, "not_a_number"),
                ("a7", SkipReason.NO_VALID_ATR, "pair_mismatch: atr of 'XETHZEUR', entry on 'ETH/EUR'"),
                ("a8", SkipReason.NO_VALID_ATR, "pair_mismatch: atr of None, entry on 'ETH/EUR'"),
                ("e9", SkipReason.INSUFFICIENT_CASH, None),
            ],
        )
        self.assertEqual(
            [(skip.event_id, skip.reason) for skip in report.skipped if skip.event_id.startswith("l")],
            [("l1", SkipReason.INVALID_LEVELS), ("l2", SkipReason.INVALID_LEVELS)],
        )

    def test_cap_of_open_plays(self):
        self.ready()
        report = ps.open_candidates(
            self.conn, [candidate(f"e{i}", asset) for i, asset in enumerate(("BTC", "ETH", "SOL", "XRP"))],
            terms=TERMS, now=T0,
        )
        self.assertEqual(len(report.opened), 3)
        self.assertEqual([(s.event_id, s.reason) for s in report.skipped], [("e3", SkipReason.MAX_OPEN)])
        later = ps.open_candidates(self.conn, [candidate("e9", "ADA")], terms=TERMS, now=T0)
        self.assertEqual([s.reason for s in later.skipped], [SkipReason.MAX_OPEN])

    def test_rerun_writes_nothing(self):
        self.ready()
        ps.open_candidates(self.conn, [candidate("e1")], terms=TERMS, now=T0)
        changes = self.conn.total_changes
        again = ps.open_candidates(self.conn, [candidate("e1")], terms=TERMS, now=T0 + timedelta(minutes=1))
        self.assertEqual(again.opened, ())
        self.assertEqual([s.reason for s in again.skipped], [SkipReason.ALREADY_RECORDED])
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(self.count("paper_plays"), 1)

    def test_invalid_candidate_rejects_the_whole_batch_before_writing(self):
        self.ready()
        bad = candidate("e2", "ETH")
        bad_ts = ps.PaperCandidate(**{**{f: getattr(bad, f) for f in bad.__slots__}, "snapshot_ts": "2026-09-29T10:00:00"})
        bad_why = ps.PaperCandidate(**{**{f: getattr(bad, f) for f in bad.__slots__}, "why": {"x": float("nan")}})
        for broken in (bad_ts, bad_why, "not a candidate"):
            with self.subTest(broken=broken), self.assertRaises(ps.PaperStoreError) as caught:
                ps.open_candidates(self.conn, [candidate("e1"), broken], terms=TERMS, now=T0)  # type: ignore[list-item]
            self.assertIs(caught.exception.code, ps.PaperStoreFailure.INVALID_ROW)
        self.assertEqual(self.count("paper_plays"), 0)

    def test_failed_write_leaves_no_partial_row(self):
        self.ready()
        self.conn.execute(
            "CREATE TEMP TRIGGER fail_eth BEFORE INSERT ON paper_plays WHEN NEW.asset = 'ETH' "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with self.assertRaises(ps.PaperStoreError) as caught:
            ps.open_candidates(self.conn, [candidate("e1", "BTC"), candidate("e2", "ETH")], terms=TERMS, now=T0)
        self.assertIs(caught.exception.code, ps.PaperStoreFailure.SQLITE_ERROR)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.count("paper_plays"), 0)


class TestSettle(PaperDbCase):
    """Levels of the default candidate: LONG entry 101, stop 98, target 107 (ATR 1.5);
    SHORT entry 99, stop 102, target 93."""

    def open_btc(self, direction: str = "LONG") -> ps.StoredPlay:
        self.ready()
        (play,) = ps.open_candidates(self.conn, [candidate("e1", direction=direction)], terms=TERMS, now=T0).opened
        return play

    def test_long_stop_closes_at_the_touching_bid_not_at_the_level(self):
        play = self.open_btc()
        add_spot(self.conn, "BTC/EUR", T0, 50.0, 51.0)  # the entry time itself: not after entry
        add_spot(self.conn, "ETH/EUR", T0 + timedelta(minutes=1), 1.0, 2.0)  # other pair
        add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=2), 98.5, 99.0)  # no touch
        add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=3), 97.0, 120.0, "cancel_only")  # invalid
        add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=4), 0.0, 99.0)  # invalid
        touched = T0 + timedelta(minutes=5)
        snapshot = add_spot(self.conn, "BTC/EUR", touched, 97.9, 98.3)  # bid 97.9 <= 98
        add_spot(self.conn, "BTC/EUR", touched + timedelta(minutes=1), 110.0, 111.0)  # later target: too late
        now = touched + timedelta(seconds=40)
        report = ps.settle_due(self.conn, now=now)
        self.assertEqual(report.pending, ())
        (close,) = report.closed
        self.assertEqual(close.play_id, play.play_id)
        self.assertIs(close.exit_reason, ExitReason.STOP)
        self.assertEqual((close.exit_bid, close.exit_ask, close.exit_ts), (D("97.9"), D("98.3"), touched))
        self.assertEqual((close.exit_snapshot_id, close.exit_source), (snapshot, "spot_snapshot"))
        self.assertEqual((close.due_at, close.delay_seconds, close.record_lag_seconds), (touched, 0.0, 40.0))
        # Costs as ever: buy at the ask 101, sell at the observed bid 97.9, 26 bps a leg.
        # gross_mid = 100*(98.1/100-1) = -1.90; price = 100*(97.9/101-1) = -3.0693... -> spread 1.17
        # fees = 0.26 + 100*97.9/101*0.0026 = 0.26 + 0.2520198 = 0.51; net = -1.90-1.17-0.51 = -3.58
        self.assertEqual((close.gross_mid, close.spread_cost, close.fees, close.net), (D("-1.9"), D("1.17"), D("0.51"), D("-3.58")))
        self.assertIs(close.outcome, Outcome.LOSS)
        self.assertEqual(ps.read_open_plays(self.conn), ())
        self.assertEqual(ps.current_balance(self.conn), D("996.42"))

    def test_gap_through_the_stop_closes_at_the_worse_observed_price(self):
        self.open_btc()
        add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=5), 90.0, 90.4)
        (close,) = ps.settle_due(self.conn, now=T0 + timedelta(minutes=6)).closed
        self.assertIs(close.exit_reason, ExitReason.STOP)
        self.assertEqual(close.exit_bid, D("90.0"))  # far below the 98 stop, as observed
        self.assertLess(close.net, D("-10"))

    def test_long_target_closes_at_the_touching_bid(self):
        self.open_btc()
        touched = T0 + timedelta(hours=3)
        add_spot(self.conn, "BTC/EUR", touched, 107.4, 107.6)
        add_spot(self.conn, "BTC/EUR", touched + timedelta(minutes=1), 97.0, 97.5)  # later stop: too late
        (close,) = ps.settle_due(self.conn, now=touched + timedelta(minutes=2)).closed
        self.assertIs(close.exit_reason, ExitReason.TARGET)
        self.assertEqual((close.exit_bid, close.exit_ts, close.due_at, close.delay_seconds), (D("107.4"), touched, touched, 0.0))
        self.assertEqual(close.record_lag_seconds, 120.0)
        self.assertIs(close.outcome, Outcome.WIN)

    def test_short_stop_and_target_watch_the_ask(self):
        play = self.open_btc("SHORT")
        add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=1), 92.0, 93.5)  # bid below 93, ask not: no exit
        add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=2), 101.0, 102.2)  # ask 102.2 >= 102: stop
        (close,) = ps.settle_due(self.conn, now=T0 + timedelta(minutes=3)).closed
        self.assertEqual(close.play_id, play.play_id)
        self.assertIs(close.exit_reason, ExitReason.STOP)
        self.assertEqual((close.exit_bid, close.exit_ask), (D("101.0"), D("102.2")))
        self.assertLess(close.net, 0)

        self.conn.close()
        os.remove(self.path)
        SnapshotStore(self.path).close()
        self.conn = self.connect()
        self.open_btc("SHORT")
        add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=2), 92.0, 92.9)  # ask 92.9 <= 93: target
        (close,) = ps.settle_due(self.conn, now=T0 + timedelta(minutes=3)).closed
        self.assertIs(close.exit_reason, ExitReason.TARGET)
        self.assertEqual(close.exit_ask, D("92.9"))
        self.assertIs(close.outcome, Outcome.WIN)

    def test_time_exit_on_the_first_valid_quote_at_or_after_24_hours(self):
        play = self.open_btc()
        self.assertEqual(play.due_at, DUE)
        add_spot(self.conn, "BTC/EUR", DUE - timedelta(seconds=1), 100.0, 101.0)  # before due, no touch
        # Before the due time with no touch the play is neither closed nor pending.
        self.assertEqual(ps.settle_due(self.conn, now=DUE - timedelta(seconds=1)), ps.SettleReport((), ()))
        add_spot(self.conn, "BTC/EUR", DUE + timedelta(seconds=10), None, None)  # invalid: ignored
        add_spot(self.conn, "BTC/EUR", DUE + timedelta(seconds=30), 102.0, 103.0)
        (close,) = ps.settle_due(self.conn, now=DUE + timedelta(minutes=1)).closed
        self.assertIs(close.exit_reason, ExitReason.TIME)
        self.assertEqual((close.exit_bid, close.exit_ts), (D("102.0"), DUE + timedelta(seconds=30)))
        self.assertEqual((close.due_at, close.delay_seconds, close.record_lag_seconds), (DUE, 30.0, 30.0))

    def test_stop_is_checked_before_time_on_the_same_quote(self):
        self.open_btc()
        add_spot(self.conn, "BTC/EUR", DUE + timedelta(seconds=5), 97.0, 97.2)
        (close,) = ps.settle_due(self.conn, now=DUE + timedelta(minutes=1)).closed
        self.assertIs(close.exit_reason, ExitReason.STOP)
        self.assertEqual((close.due_at, close.delay_seconds), (DUE + timedelta(seconds=5), 0.0))

    def test_stop_is_checked_before_target_on_the_same_quote(self):
        self.open_btc()
        with mock.patch.object(ps.paper, "exit_decision", wraps=ps.paper.exit_decision) as decide:
            add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=1), 100.0, 101.0)
            ps.settle_due(self.conn, now=T0 + timedelta(minutes=2))
        # The store delegates every observation to the domain rule, which checks stop first.
        self.assertEqual(decide.call_count, 1)
        _, stop, target, due, _, _ = decide.call_args.args
        self.assertEqual((stop, target, due), (D("98.0"), D("107.0"), DUE))

    def test_no_valid_quote_after_due_is_pending_and_writes_nothing(self):
        play = self.open_btc()
        add_spot(self.conn, "BTC/EUR", DUE + timedelta(seconds=10), 0.0, 101.0)
        changes = self.conn.total_changes
        self.assertEqual(ps.settle_due(self.conn, now=DUE + timedelta(minutes=2)), ps.SettleReport((), (play.play_id,)))
        self.assertEqual(self.conn.total_changes, changes)
        self.assertFalse(self.conn.in_transaction)

    def test_append_only_and_checks(self):
        play = self.open_btc("SHORT")
        add_spot(self.conn, "BTC/EUR", DUE, 89.0, 91.0)
        (close,) = ps.settle_due(self.conn, now=DUE).closed
        self.assertEqual(close.net, D("7.58"))
        for statement in (
            "UPDATE paper_wallet SET start_balance_cents = 1",
            "DELETE FROM paper_wallet",
            "UPDATE paper_plays SET stake_cents = 1",
            "DELETE FROM paper_plays",
            "UPDATE paper_closes SET net_cents = 1",
            "DELETE FROM paper_closes",
            # CHECK backstops
            "INSERT INTO paper_wallet VALUES (2, 100, 'EUR', 'x')",
            f"INSERT INTO paper_closes (play_id, exit_bid, exit_ask, exit_ts, exit_snapshot_id, due_at, delay_seconds, "
            f"gross_mid_cents, spread_cost_cents, fees_cents, net_cents, outcome, closed_at) VALUES "
            f"({play.play_id}, '1', '1', 'z', 1, 'a', 0, 100, 0, 0, 99, 'WIN', 'x')",
            "INSERT INTO paper_closes (play_id, exit_bid, exit_ask, exit_ts, exit_snapshot_id, due_at, delay_seconds, "
            "gross_mid_cents, spread_cost_cents, fees_cents, net_cents, outcome, closed_at) VALUES "
            "(999, '1', '1', 'z', 1, 'a', 0, 0, 0, 0, 0, 'FLAT', 'x')",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(statement)
            self.conn.rollback()
        self.assertEqual((self.count("paper_wallet"), self.count("paper_plays"), self.count("paper_closes")), (1, 1, 1))

    def test_failed_close_leaves_no_partial_row(self):
        self.ready()
        ps.open_candidates(self.conn, [candidate("e1", "BTC"), candidate("e2", "ETH")], terms=TERMS, now=T0)
        add_spot(self.conn, "BTC/EUR", DUE, 99.0, 101.0)
        add_spot(self.conn, "ETH/EUR", DUE, 99.0, 101.0)
        self.conn.execute(
            "CREATE TEMP TRIGGER fail_close BEFORE INSERT ON paper_closes WHEN NEW.play_id = 2 "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with self.assertRaises(ps.PaperStoreError):
            ps.settle_due(self.conn, now=DUE)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.count("paper_closes"), 0)
        self.conn.execute("DROP TRIGGER temp.fail_close")
        self.assertEqual(len(ps.settle_due(self.conn, now=DUE).closed), 2)


class TestExtraQuote(PaperDbCase):
    def open_btc(self) -> ps.StoredPlay:
        self.ready()
        (play,) = ps.open_candidates(self.conn, [candidate("e1")], terms=TERMS, now=T0).opened
        return play

    def test_an_extra_quote_closes_and_is_stored_with_its_source(self):
        play = self.open_btc()
        seen = T0 + timedelta(minutes=7)
        report = ps.close_plays(self.conn, now=seen + timedelta(seconds=2), extra_quotes=[
            ticker("ETH/EUR", seen, 1.0, 2.0),  # other pair
            ticker("BTC/EUR", seen - timedelta(seconds=5), 0.0, 101.0),  # invalid: never zero
            ticker("BTC/EUR", seen + timedelta(seconds=5), 90.0, 91.0),  # after now: not observed yet
            ticker("BTC/EUR", seen, 107.2, 107.3),
        ])
        (close,) = report.closed
        self.assertIs(close.exit_reason, ExitReason.TARGET)
        self.assertEqual((close.exit_bid, close.exit_ask, close.exit_ts), (D("107.2"), D("107.3"), seen))
        self.assertEqual((close.exit_source, close.record_lag_seconds, close.delay_seconds), ("ticker", 2.0, 0.0))
        rows = self.conn.execute("SELECT * FROM paper_exit_quotes").fetchall()
        self.assertEqual(len(rows), 1)
        row = dict(rows[0])
        self.assertEqual(close.exit_snapshot_id, row["quote_id"])
        self.assertEqual(
            {key: row[key] for key in ("play_id", "pair", "bid", "ask", "observed_at", "source", "recorded_at")},
            {"play_id": play.play_id, "pair": "BTC/EUR", "bid": "107.2", "ask": "107.3", "observed_at": ps.utc_text(seen),
             "source": "ticker", "recorded_at": ps.utc_text(seen + timedelta(seconds=2))},
        )
        for statement in ("UPDATE paper_exit_quotes SET bid = '1'", "DELETE FROM paper_exit_quotes"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(statement)
            self.conn.rollback()

    def test_the_earliest_touching_observation_wins_across_sources(self):
        self.open_btc()
        earlier = add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=5), 97.5, 97.9)  # stop, recorded first
        report = ps.close_plays(self.conn, now=T0 + timedelta(minutes=10),
                                extra_quotes=[ticker("BTC/EUR", T0 + timedelta(minutes=9), 108.0, 108.1)])
        (close,) = report.closed
        self.assertIs(close.exit_reason, ExitReason.STOP)
        self.assertEqual((close.exit_source, close.exit_snapshot_id), ("spot_snapshot", earlier))
        self.assertEqual(self.count("paper_exit_quotes"), 0)

    def test_an_extra_quote_without_a_touch_writes_nothing(self):
        self.open_btc()
        changes = self.conn.total_changes
        report = ps.close_plays(self.conn, now=T0 + timedelta(minutes=10),
                                extra_quotes=[ticker("BTC/EUR", T0 + timedelta(minutes=9), 100.0, 100.2)])
        self.assertEqual(report, ps.SettleReport((), ()))
        self.assertEqual(self.conn.total_changes, changes)

    def test_play_ids_limits_the_plays_considered(self):
        self.ready()
        btc, eth = ps.open_candidates(self.conn, [candidate("e1", "BTC"), candidate("e2", "ETH")], terms=TERMS, now=T0).opened
        add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=1), 90.0, 91.0)
        add_spot(self.conn, "ETH/EUR", T0 + timedelta(minutes=1), 90.0, 91.0)
        (close,) = ps.close_plays(self.conn, now=T0 + timedelta(minutes=2), play_ids={eth.play_id}).closed
        self.assertEqual(close.play_id, eth.play_id)
        self.assertEqual([play.play_id for play in ps.read_open_plays(self.conn)], [btc.play_id])

    def test_invalid_arguments_are_refused_before_any_write(self):
        self.open_btc()
        at = T0 + timedelta(minutes=1)
        for extra in (
            "BTC/EUR",
            [("BTC/EUR", 1, 2)],
            [ps.ObservedQuote(pair="", bid=1, ask=2, observed_at=at, source="ticker")],
            [ps.ObservedQuote(pair="BTC/EUR", bid=1, ask=2, observed_at=datetime(2026, 9, 29, 10, 1), source="ticker")],
            [ps.ObservedQuote(pair="BTC/EUR", bid=1, ask=2, observed_at=at, source="spot_snapshot")],
            [ps.ObservedQuote(pair="BTC/EUR", bid=1, ask=2, observed_at=at, source=" ")],
        ):
            with self.subTest(extra=extra), self.assertRaises(ps.PaperStoreError) as caught:
                ps.close_plays(self.conn, now=at, extra_quotes=extra)  # type: ignore[arg-type]
            self.assertIs(caught.exception.code, ps.PaperStoreFailure.INVALID_ARGUMENT)
        with self.assertRaises(ps.PaperStoreError):
            ps.close_plays(self.conn, now=at, play_ids=["1"])  # type: ignore[list-item]
        self.assertEqual(self.count("paper_closes"), 0)

    def test_a_concurrent_second_close_writes_nothing_and_raises_nothing(self):
        play = self.open_btc()
        add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=5), 97.0, 97.5)
        other = self.connect()
        self.addCleanup(other.close)
        real_begin = ps._begin
        raced: list[ps.SettleReport] = []

        def begin_after_the_other_writer(conn: sqlite3.Connection, what: str) -> None:
            # `other` evaluated the same exit before the lock; this writer closes first.
            if conn is other and not raced:
                raced.append(ps.close_plays(self.conn, now=T0 + timedelta(minutes=6)))
            real_begin(conn, what)

        with mock.patch.object(ps, "_begin", begin_after_the_other_writer):
            late = ps.close_plays(other, now=T0 + timedelta(minutes=6),
                                  extra_quotes=[ticker("BTC/EUR", T0 + timedelta(minutes=5, seconds=1), 96.0, 96.5)])
        self.assertEqual([len(report.closed) for report in raced], [1])
        self.assertEqual(late, ps.SettleReport((), ()))
        self.assertFalse(other.in_transaction)
        self.assertEqual(self.count("paper_closes"), 1)
        self.assertEqual(self.count("paper_exit_quotes"), 0)
        (close,) = ps.read_closes(self.conn)
        self.assertEqual((close.play_id, close.exit_bid, close.exit_source), (play.play_id, D("97.0"), "spot_snapshot"))


if __name__ == "__main__":
    unittest.main()
