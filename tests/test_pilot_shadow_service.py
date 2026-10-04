"""Pilot shadow service (radar_v08/pilot_shadow.py) next to the paper game.

Temporary SQLite files only; the real radar_state.sqlite is never opened and nothing
touches the network. The game's rows and results must be identical with and without the
pilot running beside it.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from radar_v08 import paper_game, pilot_shadow
from radar_v08.adapters import paper_store, pilot_store
from radar_v08.domain.risk import Envelope, NoTradeReason
from radar_v08.store import SnapshotStore

D = Decimal
T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
RULES = {"lot_decimals": 2, "ordermin": "0.01", "costmin": "0.50", "tick_size": "0.01", "altname": "XBTEUR"}
NO_TRADE = {reason.value: 0 for reason in NoTradeReason}


def game_candidate(event_id: str, asset: str = "BTC", *, direction: str = "LONG", quote: str = "EUR",
                   ts: datetime = T0) -> paper_store.PaperCandidate:
    """Quote 99.9/100, ATR 1 on the entry pair: LONG stop 98 / target 104 in both accounts."""
    pair = f"{asset}/{quote}"
    return paper_store.PaperCandidate(
        event_id=event_id, run_id="run-1", asset=asset, pair=pair, quote=quote, direction=direction,
        bid=99.9, ask=100.0, snapshot_ts=ts.isoformat(), status="online",
        why=paper_game.build_why(setup_type="BREAKOUT", direction=direction, scores={"final": 0.7}, features={}),
        atr=1.0, atr_pair=pair,
    )


def pilot_candidates(*candidates: paper_store.PaperCandidate) -> list[pilot_store.PilotCandidate]:
    return [pilot_shadow.candidate_from_paper(candidate, dict(RULES)) for candidate in candidates]


def rows(conn: sqlite3.Connection, prefix: str) -> dict[str, list[tuple[object, ...]]]:
    tables = [str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ? ORDER BY name", (f"{prefix}%",))]
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}


def schema(conn: sqlite3.Connection, prefix: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE tbl_name LIKE ? ORDER BY type, name", (f"{prefix}%",))]


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pilot-shadow-")
        self.addCleanup(self._tmp.cleanup)
        self.conn = self.database("radar_state.sqlite")

    def database(self, name: str) -> sqlite3.Connection:
        path = os.path.join(self._tmp.name, name)
        SnapshotStore(path).close()
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def add_spot(conn: sqlite3.Connection, pair: str, ts: datetime, bid: float, ask: float) -> None:
        asset, quote = pair.split("/")
        conn.execute(
            "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES (?, ?, ?, ?, ?, ?, 'online')",
            (asset, pair, quote, ts.isoformat(), bid, ask),
        )
        conn.commit()


class TestService(ServiceCase):
    def test_prepare_records_the_default_envelope_once(self):
        account = pilot_shadow.prepare(self.conn, T0)
        self.assertEqual(account.envelope, Envelope(D("240.00"), "EUR"))
        self.assertEqual(pilot_shadow.default_envelope(), Envelope(pilot_shadow.PILOT_ASSIGNED_EQUITY_EUR, "EUR"))
        before = rows(self.conn, "pilot_")
        changes = self.conn.total_changes
        self.assertEqual(pilot_shadow.prepare(self.conn, T0 + timedelta(days=1)), account)
        self.assertEqual((rows(self.conn, "pilot_"), self.conn.total_changes), (before, changes))

    def test_candidate_from_paper_keeps_every_field_and_adds_the_pair_rules(self):
        game = game_candidate("e1")
        pilot = pilot_shadow.candidate_from_paper(game, RULES)
        for field in ("event_id", "run_id", "asset", "pair", "quote", "direction", "bid", "ask", "snapshot_ts",
                      "status", "atr", "atr_pair"):
            self.assertEqual(getattr(pilot, field), getattr(game, field), field)
        self.assertIs(pilot.pair_entry, RULES)

    def test_counts_for_the_run_record(self):
        result = pilot_shadow.open_for_candidates(
            self.conn, pilot_candidates(game_candidate("e1"), game_candidate("e2", "ETH"),
                                        game_candidate("e3", "SOL", quote="USD")), T0)
        self.assertEqual(result, {
            "evaluated": 3, "opened": 1, "already_recorded": 0,
            # F5 comes before the currency check: the USD candidate is refused as position_already_open.
            "no_trade": {**NO_TRADE, "position_already_open": 2},
            "equity": "240.00", "locks_tripped": [], "locks_active": [],
        })
        again = pilot_shadow.open_for_candidates(self.conn, pilot_candidates(game_candidate("e1")), T0)
        self.assertEqual((again["evaluated"], again["already_recorded"], again["no_trade"]), (0, 1, NO_TRADE))
        # The default fee is the config's spot taker fee: 26 bps -> 0.19 lots at 100, planned loss 0.5731.
        (position,) = pilot_store.read_open_positions(self.conn)
        self.assertEqual((position.fee_bps, position.quantity, position.planned_loss), (D("26.0"), D("0.19"), D("0.5731")))
        self.add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=5), 87.0, 87.1)
        settled = pilot_shadow.settle(self.conn, T0 + timedelta(minutes=6))
        self.assertEqual(settled, {"closed": 1, "pending": 0, "equity": "237.44",
                                   "locks_tripped": ["daily_loss"], "locks_active": ["daily_loss"]})
        self.assertEqual(pilot_shadow.evaluate_locks(self.conn, T0 + timedelta(minutes=7)),
                         {"equity": "237.44", "locks_tripped": [], "locks_active": ["daily_loss"]})

    def test_usd_quoted_candidate_is_refused_without_fx(self):
        result = pilot_shadow.open_for_candidates(self.conn, pilot_candidates(game_candidate("u", quote="USD")), T0)
        self.assertEqual(result["no_trade"], {**NO_TRADE, "quote_currency_mismatch": 1})
        (decision,) = pilot_store.read_decisions(self.conn)
        self.assertEqual((decision.reason, decision.detail, decision.quote), (NoTradeReason.QUOTE_CURRENCY_MISMATCH, "USD", "USD"))

    def test_short_is_refused_while_the_game_still_opens_it(self):
        short = game_candidate("s1", direction="SHORT")
        game = paper_game.open_for_events(self.conn, [short], T0)
        pilot = pilot_shadow.open_for_candidates(self.conn, pilot_candidates(short), T0)
        self.assertEqual(game["opened"], 1)
        self.assertEqual((pilot["opened"], pilot["no_trade"]), (0, {**NO_TRADE, "unsupported_direction": 1}))
        self.assertEqual([play.direction.value for play in paper_store.read_plays(self.conn)], ["SHORT"])
        self.assertEqual(pilot_store.read_positions(self.conn), ())

    def test_kill_switch_and_lock_review_through_the_service(self):
        pilot_shadow.open_for_candidates(self.conn, pilot_candidates(game_candidate("e1")), T0)
        switch = pilot_shadow.engage(self.conn, "manual stop", "operator", T0 + timedelta(minutes=1))
        self.assertTrue(switch.engaged)
        blocked = pilot_shadow.open_for_candidates(self.conn, pilot_candidates(game_candidate("e2", "ETH")),
                                                   T0 + timedelta(minutes=1))
        self.assertEqual(blocked["no_trade"]["kill_switch_engaged"], 1)
        # Risk reduction continues: the open position still closes, and the loss trips the daily lock.
        self.add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=2), 87.0, 87.1)
        self.assertEqual(pilot_shadow.close_positions(self.conn, T0 + timedelta(minutes=3))["closed"], 1)
        self.assertFalse(pilot_shadow.release(self.conn, "resume", "operator", T0 + timedelta(minutes=4)).engaged)
        locked = pilot_shadow.open_for_candidates(self.conn, pilot_candidates(game_candidate("e3")), T0 + timedelta(minutes=4))
        self.assertEqual(locked["no_trade"]["daily_loss_lock"], 1)
        (lock,) = pilot_store.active_locks(self.conn)
        review = pilot_shadow.review_lock(self.conn, lock.lock_id, reviewer="operator", cause="gap", now=T0 + timedelta(minutes=5))
        self.assertEqual(review.rebase_equity, D("237.44"))
        opened = pilot_shadow.open_for_candidates(self.conn, pilot_candidates(game_candidate("e4")), T0 + timedelta(minutes=6))
        self.assertEqual((opened["opened"], opened["locks_active"]), (1, []))


class TestGameUntouched(ServiceCase):
    """The same game sequence in two databases, one with the pilot running beside it."""

    def run_game(self, conn: sqlite3.Connection, *, with_pilot: bool) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        first = [game_candidate("e1"), game_candidate("e2", "ETH", direction="SHORT"), game_candidate("e3", "SOL")]
        results.append(paper_game.open_for_events(conn, first, T0))
        if with_pilot:
            pilot_shadow.open_for_candidates(conn, pilot_candidates(*first), T0)
            pilot_shadow.engage(conn, "test", "operator", T0 + timedelta(minutes=1))
        # BTC gaps down: the game's BTC play and the pilot's BTC position both stop out.
        self.add_spot(conn, "BTC/EUR", T0 + timedelta(minutes=2), 87.0, 87.1)
        self.add_spot(conn, "ETH/EUR", T0 + timedelta(minutes=2), 99.0, 99.1)
        if with_pilot:
            pilot_shadow.close_positions(conn, T0 + timedelta(minutes=3))
            pilot_shadow.evaluate_locks(conn, T0 + timedelta(minutes=3))
            pilot_shadow.release(conn, "test", "operator", T0 + timedelta(minutes=3))
            (lock,) = pilot_store.active_locks(conn)
            pilot_shadow.review_lock(conn, lock.lock_id, reviewer="operator", cause="test", now=T0 + timedelta(minutes=3))
        results.append(paper_game.settle_due(conn, T0 + timedelta(minutes=3)))
        later = [game_candidate("e4", "ADA", ts=T0 + timedelta(minutes=4))]
        if with_pilot:
            pilot_shadow.open_for_candidates(conn, pilot_candidates(*later), T0 + timedelta(minutes=4))
        results.append(paper_game.open_for_events(conn, later, T0 + timedelta(minutes=4)))
        extra = [paper_store.ObservedQuote("ADA/EUR", 104.5, 104.6, T0 + timedelta(minutes=5), "ticker")]
        if with_pilot:
            pilot_shadow.close_positions(conn, T0 + timedelta(minutes=6), extra_quotes=extra)
        closed = paper_store.close_plays(conn, now=T0 + timedelta(minutes=6), extra_quotes=extra)
        results.append({"closed": [(close.play_id, close.net, close.exit_reason) for close in closed.closed]})
        results.append({"balance": paper_store.current_balance(conn)})
        return results

    def test_pilot_leaves_every_game_row_and_result_identical(self):
        alone = self.conn
        beside = self.database("with_pilot.sqlite")
        game_alone = self.run_game(alone, with_pilot=False)
        game_beside = self.run_game(beside, with_pilot=True)
        self.assertEqual(game_beside, game_alone)
        self.assertEqual(rows(beside, "paper_"), rows(alone, "paper_"))
        self.assertEqual(schema(beside, "paper_"), schema(alone, "paper_"))
        self.assertTrue(all(rows(alone, "paper_").values()))  # the game really played
        # The pilot really ran: two positions (BTC stopped out, ADA hit its target), a reviewed lock,
        # two kill switch rows and refusals for the SHORT and the position already open.
        self.assertEqual([close.exit_reason.value for close in pilot_store.read_closes(beside)], ["stop", "target"])
        self.assertEqual([lock.active for lock in pilot_store.read_locks(beside)], [False])
        self.assertEqual(len(rows(beside, "pilot_kill_switch")["pilot_kill_switch"]), 2)
        counts = pilot_store.no_trade_counts(beside)
        self.assertEqual((counts["unsupported_direction"], counts["position_already_open"]), (1, 1))
        self.assertEqual(rows(alone, "pilot_"), {})

    def test_pilot_rows_in_the_game_database_never_change_game_reads(self):
        paper_game.open_for_events(self.conn, [game_candidate("e1")], T0)
        before = (paper_store.read_plays(self.conn), paper_store.read_closes(self.conn),
                  paper_store.current_balance(self.conn), rows(self.conn, "paper_"))
        pilot_shadow.open_for_candidates(self.conn, pilot_candidates(game_candidate("e1")), T0)
        pilot_shadow.engage(self.conn, "x", "operator", T0)
        self.add_spot(self.conn, "BTC/EUR", T0 + timedelta(minutes=1), 99.0, 99.1)
        pilot_shadow.evaluate_locks(self.conn, T0 + timedelta(minutes=1))
        after = (paper_store.read_plays(self.conn), paper_store.read_closes(self.conn),
                 paper_store.current_balance(self.conn), rows(self.conn, "paper_"))
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
