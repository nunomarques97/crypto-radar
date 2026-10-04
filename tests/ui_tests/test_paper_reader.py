"""ui/paper_reader.py and Api.get_paper_state on temporary databases only (no network).

The databases are built with the real SnapshotStore and the paper store API on temp
paths; radar_runs, events and qwen_reviews rows are inserted directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_legacy as legacy  # noqa: E402  (plays with a fixed hold and no EX-1 levels)

from radar_v08 import config
from radar_v08.adapters import paper_store as ps
from radar_v08.adapters import qwen_review_store as qrs
from radar_v08.store import SnapshotStore
from ui import paper_reader, paper_texts
from ui.paper_reader import PaperReader

D = Decimal
T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
DEFAULTS = {"start_balance": D("1000"), "stake": D("100"), "max_open": 3, "hold_minutes": 60, "fee_bps": D("26")}
TERMS = ps.PlayTerms(stake=D("100"), fee_bps=D("26"), max_open=3)
#: The EX-1 exit plan of the payload, all absent (a legacy play or a database not migrated yet).
NO_PLAN = {"exit_policy": None, "stop": None, "target": None, "max_hold_minutes": None, "exit_due_at": None}
#: The EX-1 close fields of a history item, all absent (a legacy close).
NO_REASON = {"exit_reason": None, "exit_reason_text": None, "exit_source": None, "record_lag_seconds": None,
             "stop": None, "target": None}
#: The stored 26 bps fee per leg of a play, labelled as an assumption; a EUR-quoted pair.
FEE_26 = {"fee_bps": "26", "fee_source": "ASSUMED", "account_tier_verified": False,
          "fee_text": paper_texts.fee_provenance_text(D("26"))}
EUR_QUOTED = {"fx_excluded": False, "fx_text": None}


def minutes(count: float) -> datetime:
    return T0 + timedelta(minutes=count)


def why(direction: str = "LONG") -> dict[str, object]:
    return {
        "setup_type": "BREAKOUT",
        "direction": direction,
        "scores": {"opportunity_score": 80.0, "tradeability_score": 85.0, "confidence": "HIGH"},
        "features": {"l1": {"return_15m": 2.3, "volume_intensity_15m": 3.1}, "l2": {}},
    }


def sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ReaderCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="paper-reader-")
        self.path = os.path.join(self._tmp.name, "radar_state.sqlite")
        SnapshotStore(self.path).close()  # every existing radar table, as in production
        self.conn = sqlite3.connect(self.path)  # the test's writer, standing in for the radar

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def reader(self, **overrides: object) -> PaperReader:
        options: dict[str, object] = {
            "enabled": True, "qwen_enabled": True, "default_params": DEFAULTS,
        }
        options.update(overrides)
        return PaperReader(self.path, **options)  # type: ignore[arg-type]

    def wallet(self, start: str = "1000") -> None:
        ps.ensure_schema(self.conn)
        ps.ensure_wallet(self.conn, start_balance=D(start), currency="EUR", now=T0)

    def spot(self, asset: str, at: datetime, bid: object, ask: object, status: str = "online") -> None:
        self.conn.execute(
            "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES (?, ?, 'EUR', ?, ?, ?, ?)",
            (asset, f"{asset}/EUR", at.isoformat(), bid, ask, status),
        )
        self.conn.commit()

    def open_play(self, event_id: str, asset: str, direction: str, bid: float, ask: float, at: datetime) -> int:
        """A play with a fixed 60-minute hold and no EX-1 levels (a legacy play), entering on
        the spot row written at ``at``; plays with levels are the store's own tests."""
        self.spot(asset, at, bid, ask)
        return legacy.insert_legacy_play(self.conn, event_id, asset, direction, bid, ask, at, run_id="run-1",
                                         why=why(direction))


class TestReadOnly(ReaderCase):
    def test_connection_refuses_writes(self) -> None:
        conn = self.reader().connect()
        try:
            for sql in (
                "CREATE TABLE intruder (x INTEGER)",
                "INSERT INTO radar_runs (run_id, ts, mode) VALUES ('x', '2026-09-29', 'FULL')",
                "DELETE FROM radar_runs",
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.OperationalError):
                    conn.execute(sql)
            conn.execute("PRAGMA query_only = OFF")  # the URI mode=ro still refuses
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE intruder (x INTEGER)")
        finally:
            conn.close()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM radar_runs").fetchone()[0], 0)

    def test_database_without_paper_tables_is_left_untouched(self) -> None:
        self.conn.close()
        before_hash, before_mtime = sha(self.path), os.stat(self.path).st_mtime_ns
        state = self.reader().read(T0)
        self.assertEqual((sha(self.path), os.stat(self.path).st_mtime_ns), (before_hash, before_mtime))
        conn = sqlite3.connect(self.path)
        try:
            self.assertFalse(ps.schema_present(conn))
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
        self.assertFalse(names & set(ps.TABLES))
        self.conn = sqlite3.connect(self.path)
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], paper_reader.REASON_NO_TABLES)
        self.assertIsNone(state["wallet"])
        self.assertEqual((state["open_plays"], state["history"]), ([], []))
        self.assertEqual(state["params"]["start_balance"], "1000.00")
        self.assertEqual(state["params_source"], "config")

    def test_missing_database_is_not_created(self) -> None:
        missing = os.path.join(self._tmp.name, "absent.sqlite")
        state = PaperReader(
            missing, enabled=True, qwen_enabled=True, default_params=DEFAULTS
        ).read(T0)
        self.assertFalse(os.path.exists(missing))
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], paper_reader.REASON_NO_DATABASE)

    def test_wal_database_is_read_while_the_writer_is_open(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        state = self.reader().read(minutes(1))
        self.assertTrue(state["available"])
        self.assertEqual(len(state["open_plays"]), 1)

    def test_file_that_is_not_a_database_is_reported_not_raised(self) -> None:
        garbage = os.path.join(self._tmp.name, "garbage.sqlite")
        Path(garbage).write_bytes(b"not a database at all" * 100)
        before = sha(garbage)
        state = PaperReader(
            garbage, enabled=True, qwen_enabled=True, default_params=DEFAULTS
        ).read(T0)
        self.assertEqual(sha(garbage), before)
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], paper_reader.REASON_UNREADABLE)
        self.assertEqual(state["activity"], [])
        json.dumps(state)

    def test_wallet_not_recorded_yet(self) -> None:
        ps.ensure_schema(self.conn)
        state = self.reader().read(T0)
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], paper_reader.REASON_NO_WALLET)

    def test_source_has_no_write_path(self) -> None:
        source = Path(paper_reader.__file__).read_text(encoding="utf-8")
        code = re.sub(r'"""(.|\n)*?"""', "", source)
        for word in ("ensure_schema", "ensure_wallet", "open_candidates", "settle_due", ".commit(",
                     "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"):
            self.assertNotIn(word, code)
        self.assertIn("?mode=ro", code)


class TestWallet(ReaderCase):
    def test_empty_wallet(self) -> None:
        self.wallet()
        state = self.reader().read(minutes(5))
        self.assertTrue(state["available"])
        self.assertEqual(state["reason"], paper_texts.REASON_NO_PLAYS)
        self.assertTrue(state["pretend_money"])
        self.assertEqual(state["currency"], "EUR")
        self.assertEqual(state["params_source"], "wallet")
        self.assertEqual(
            state["params"],
            {"start_balance": "1000.00", "stake": "100.00", "max_open": 3, "hold_minutes": 60, "fee_bps": "26"},
        )
        wallet = state["wallet"]
        self.assertEqual(
            {key: wallet[key] for key in ("balance", "start_balance", "change", "change_pct", "open_stakes",
                                          "available_cash", "wins", "losses", "flats", "fees_total", "last_results")},
            {"balance": "1000.00", "start_balance": "1000.00", "change": "0.00", "change_pct": "0.00",
             "open_stakes": "0.00", "available_cash": "1000.00", "wins": 0, "losses": 0, "flats": 0,
             "fees_total": "0.00", "last_results": []},
        )
        self.assertEqual(wallet["series"], [{"ts": ps.utc_text(T0), "balance": "1000.00"}])
        self.assertEqual(wallet["cost_sentence"], paper_texts.cost_sentence([D("26")]))
        self.assertEqual((state["open_plays"], state["history"], state["history_total"]), ([], [], 0))
        json.dumps(state)

    def test_disabled_switch_is_reported_but_history_still_read(self) -> None:
        self.wallet()
        state = self.reader(enabled=False).read(T0)
        self.assertFalse(state["enabled"])
        self.assertTrue(state["available"])
        self.assertEqual(state["reason"], paper_reader.REASON_DISABLED)
        treasurer = next(agent for agent in state["agents"] if agent["id"] == "treasurer")
        self.assertFalse(treasurer["enabled"])


class TestPlays(ReaderCase):
    def build(self) -> None:
        """BTC long won, ETH short lost, SOL long still open with a later quote."""
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        self.open_play("ev-eth", "ETH", "SHORT", 199.0, 201.0, minutes(1))
        self.spot("BTC", minutes(61), 104.0, 106.0)
        self.spot("ETH", minutes(62), 203.0, 205.0)
        self.settled = ps.settle_due(self.conn, now=minutes(63)).closed
        self.assertEqual(len(self.settled), 2)
        self.open_play("ev-sol", "SOL", "LONG", 9.9, 10.1, minutes(64))
        self.spot("SOL", minutes(70), 10.4, 10.6)

    def test_balance_reconciles_with_history_exactly(self) -> None:
        self.build()
        state = self.reader().read(minutes(71))
        wallet, history = state["wallet"], state["history"]
        nets = [D(item["net"]) for item in history]
        self.assertEqual(D(wallet["start_balance"]) + sum(nets), D(wallet["balance"]))
        expected = ps.current_balance(self.conn)
        self.assertEqual(D(wallet["balance"]), expected)
        self.assertEqual(D(wallet["change"]), expected - D("1000"))
        self.assertEqual(wallet["open_stakes"], "100.00")
        self.assertEqual(D(wallet["available_cash"]), expected - D("100"))
        self.assertEqual((wallet["wins"], wallet["losses"], wallet["flats"]), (1, 1, 0))
        self.assertEqual(wallet["last_results"], ["WIN", "LOSS"])
        self.assertEqual(D(wallet["fees_total"]), sum(close.fees for close in self.settled))
        self.assertEqual([point["balance"] for point in wallet["series"]][0], "1000.00")
        self.assertEqual(wallet["series"][-1]["balance"], wallet["balance"])
        self.assertEqual(len(wallet["series"]), 3)
        for item in history:
            for key in ("gross_mid", "spread_cost", "fees", "net"):
                self.assertRegex(item[key], r"^-?\d+\.\d\d$")
            self.assertEqual(D(item["net"]), D(item["gross_mid"]) - D(item["spread_cost"]) - D(item["fees"]))

    def test_history_fields(self) -> None:
        self.build()
        state = self.reader().read(minutes(71))
        self.assertEqual(state["history_total"], 2)
        self.assertEqual([item["asset"] for item in state["history"]], ["ETH", "BTC"])  # newest first
        eth, close = state["history"][0], self.settled[1]
        self.assertEqual(
            {key: value for key, value in eth.items() if key != "sentence"},
            {"play_id": close.play_id, "asset": "ETH", "direction": "SHORT", "opened_at": ps.utc_text(minutes(1)),
             "closed_at": ps.utc_text(minutes(62)), "delay_seconds": 60.0, "gross_mid": str(close.gross_mid.quantize(D("0.01"))),
             "spread_cost": str(close.spread_cost.quantize(D("0.01"))), "fees": str(close.fees.quantize(D("0.01"))),
             "net": str(close.net.quantize(D("0.01"))), "outcome": "LOSS", **NO_REASON, **FEE_26, **EUR_QUOTED},
        )
        self.assertEqual(
            eth["sentence"],
            paper_texts.result_sentence(
                asset="ETH", direction="SHORT", outcome="LOSS", net=close.net, gross_mid=close.gross_mid,
                spread_cost=close.spread_cost, fees=close.fees, entry_bid=D("199.0"), entry_ask=D("201.0"),
                exit_bid=close.exit_bid, exit_ask=close.exit_ask, delay_seconds=60.0,
            ),
        )
        self.assertIn("lost", eth["sentence"])

    def test_open_play_fields(self) -> None:
        self.build()
        state = self.reader().read(minutes(71))
        [play] = state["open_plays"]
        self.assertEqual(
            {key: value for key, value in play.items() if key not in ("price_line", "why", "steps", "play_id")},
            {"asset": "SOL", "pair": "SOL/EUR", "quote": "EUR", "direction": "LONG", "direction_text": "betting it goes up",
             "opened_at": ps.utc_text(minutes(64)), "due_at": ps.utc_text(minutes(124)), "status": "OPEN",
             "stake": "100.00", "entry_bid": "9.9", "entry_ask": "10.1", "entry_mid": "10.0",
             "now_bid": "10.4", "now_ask": "10.6", "now_ts": ps.utc_text(minutes(70)), "gross_now": "5.00",
             **NO_PLAN,
             # Closed now on paper at the 10.4 bid: gross 5.00 - spread 2.03 (5 - 100 x (10.4/10.1 - 1))
             # - fees 0.53 (0.26 + 100 x 10.4/10.1 x 0.26 %) = 2.44 net; liquidation = stake + net.
             "cost_basis": "100.00", "liquidation_value": "102.44", "open_net_pnl": "2.44",
             "open_gross_mid": "5.00", "open_spread_cost": "2.03", "open_fees": "0.53",
             "mark": {"status": "marked", "stale": False, "reason_text": None, "bid": "10.4", "ask": "10.6",
                      "ts": ps.utc_text(minutes(70)), "age_seconds": 60.0},
             **FEE_26, **EUR_QUOTED},
        )
        self.assertEqual(play["price_line"], [
            {"ts": ps.utc_text(minutes(64)), "mid": "10.0"}, {"ts": ps.utc_text(minutes(70)), "mid": "10.5"},
        ])
        self.assertEqual(play["why"], paper_texts.why_sentence(why("LONG"), asset="SOL", direction="LONG"))
        self.assertEqual([step["title"] for step in play["steps"]], ["The radar noticed", "The AI checked", "Now it waits"])
        self.assertEqual([step["state"] for step in play["steps"]], ["done", "done", "now"])

    def test_short_gross_now_is_signed_by_direction(self) -> None:
        self.wallet()
        self.open_play("ev-eth", "ETH", "SHORT", 199.0, 201.0, T0)
        self.spot("ETH", minutes(10), 203.0, 205.0)  # mid 200 -> 204: a short loses 2%
        [play] = self.reader().read(minutes(11))["open_plays"]
        self.assertEqual(play["gross_now"], "-2.00")

    def test_no_valid_current_price_gives_null_not_zero(self) -> None:
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        self.spot("BTC", minutes(20), 105.0, 104.0)  # crossed
        self.spot("BTC", minutes(21), 0.0, 101.0)  # not positive
        self.spot("BTC", minutes(22), 99.0, 101.0, status="cancel_only")
        [play] = self.reader().read(minutes(25))["open_plays"]  # entry quote is 25 min old
        self.assertIsNone(play["gross_now"])
        self.assertEqual((play["now_bid"], play["now_ask"], play["now_ts"]), (None, None, None))
        self.assertEqual(play["price_line"], [{"ts": ps.utc_text(T0), "mid": "100.0"}])

    def test_pending_exit(self) -> None:
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        self.spot("BTC", minutes(59), 100.0, 102.0)
        self.assertEqual(ps.settle_due(self.conn, now=minutes(65)).pending, (1,))
        [play] = self.reader().read(minutes(65))["open_plays"]
        self.assertEqual(play["status"], "PENDING_EXIT")
        self.assertEqual(play["now_ts"], ps.utc_text(minutes(59)))
        self.assertEqual(play["gross_now"], "1.00")
        self.assertEqual(play["steps"][2]["title"], "Waiting for a price")

    def test_price_line_is_capped_and_keeps_both_ends(self) -> None:
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        for second in range(1, 300):
            self.spot("BTC", T0 + timedelta(seconds=10 * second), 99.0 + second / 100, 101.0 + second / 100)
        [play] = self.reader().read(minutes(50))["open_plays"]
        line = play["price_line"]
        self.assertEqual(len(line), paper_reader.PRICE_LINE_POINTS)
        self.assertEqual(line[0], {"ts": ps.utc_text(T0), "mid": "100.0"})
        self.assertEqual(line[-1]["ts"], ps.utc_text(T0 + timedelta(seconds=2990)))
        self.assertEqual([point["ts"] for point in line], sorted({point["ts"] for point in line}))

    def test_eligible_count_from_the_play_run(self) -> None:
        self.wallet()
        self.conn.execute(
            "INSERT INTO radar_runs (run_id, ts, mode, assets_eligible) VALUES ('run-1', ?, 'FULL', 612)", (T0.isoformat(),)
        )
        self.conn.commit()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        [play] = self.reader().read(minutes(1))["open_plays"]
        self.assertEqual(play["steps"][0]["text"], "Looked at 612 coins; BTC was moving unusually.")

    def test_hostile_asset_is_passed_as_data(self) -> None:
        self.wallet()
        self.open_play("ev-x", "<img src=x onerror=alert(1)>", "LONG", 99.0, 101.0, T0)
        state = self.reader().read(minutes(1))
        self.assertEqual(state["open_plays"][0]["asset"], "<img src=x onerror=alert(1)>")
        self.assertEqual(state["activity"][-1]["asset"], "<img src=x onerror=alert(1)>")


class TestExitPolicy(ReaderCase):
    """Plays opened under the EX-1 exit policy (stop, target, 24 h) next to legacy plays."""

    def open_ex1(self, event_id: str, asset: str, direction: str, bid: float, ask: float, at: datetime,
                 atr: float = 2.0) -> ps.StoredPlay:
        self.spot(asset, at, bid, ask)
        candidate = ps.PaperCandidate(
            event_id=event_id, run_id="run-1", asset=asset, pair=f"{asset}/EUR", quote="EUR", direction=direction,
            bid=bid, ask=ask, snapshot_ts=at.isoformat(), status="online", why=why(direction), atr=atr,
            atr_pair=f"{asset}/EUR",
        )
        report = ps.open_candidates(self.conn, [candidate], terms=TERMS, now=at)
        self.assertEqual(len(report.opened), 1, report.skipped)
        return report.opened[0]

    def ticker(self, asset: str, bid: float, ask: float, at: datetime) -> ps.ObservedQuote:
        return ps.ObservedQuote(pair=f"{asset}/EUR", bid=bid, ask=ask, observed_at=at, source="ticker")

    def test_open_play_carries_its_frozen_exit_plan(self) -> None:
        self.wallet()
        play = self.open_ex1("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)  # entry 101, stop 97, target 109
        state = self.reader().read(minutes(5))
        [item] = state["open_plays"]
        self.assertEqual(
            {key: item[key] for key in NO_PLAN},
            {"exit_policy": "ex1_initial_paper_v1", "stop": str(play.stop), "target": str(play.target),
             "max_hold_minutes": 1440, "exit_due_at": ps.utc_text(T0 + timedelta(hours=24))},
        )
        self.assertEqual((D(item["stop"]), D(item["target"])), (D("97"), D("109")))
        self.assertEqual(item["due_at"], item["exit_due_at"])
        self.assertEqual(state["params"]["hold_minutes"], 1440)  # recorded on the latest play
        wait = item["steps"][2]["text"]
        self.assertIn("loss limit of 97", wait)
        self.assertIn("profit goal of 109", wait)
        self.assertIn("after 24 hours", wait)
        self.assertNotIn("60", wait)

    def test_levels_are_shown_at_the_entry_quote_precision(self) -> None:
        self.wallet()
        # A float ATR carries far more digits than the pair is quoted with.
        play = self.open_ex1("ev-sol", "SOL", "LONG", 142.298615, 142.321385, T0, atr=0.35577142857142857)
        self.assertGreater(-play.stop.as_tuple().exponent, 6)  # the frozen value keeps every digit
        state = self.reader().read(minutes(5))
        [item] = state["open_plays"]
        self.assertEqual((item["stop"], item["target"]), ("141.609842", "143.744471"))
        self.assertIn("loss limit of 141.609842", item["steps"][2]["text"])
        ps.close_plays(self.conn, now=minutes(9), extra_quotes=[self.ticker("SOL", 141.5, 141.52, minutes(8))])
        [closed] = self.reader().read(minutes(10))["history"]
        self.assertEqual((closed["stop"], closed["target"]), ("141.609842", "143.744471"))
        self.assertIn("loss limit of 141.609842", closed["sentence"])

    def test_short_levels_are_mirrored(self) -> None:
        self.wallet()
        self.open_ex1("ev-eth", "ETH", "SHORT", 199.0, 201.0, T0)  # entry 199, stop 203, target 191
        [item] = self.reader().read(minutes(5))["open_plays"]
        self.assertEqual((D(item["stop"]), D(item["target"])), (D("203"), D("191")))

    def test_each_close_reason_is_read_with_source_and_lag(self) -> None:
        self.wallet()
        stop = self.open_ex1("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        target = self.open_ex1("ev-eth", "ETH", "LONG", 199.0, 201.0, minutes(1))
        timed = self.open_ex1("ev-sol", "SOL", "SHORT", 9.9, 10.1, minutes(2), atr=0.5)
        # BTC: a ticker quote at 10:30 touches the stop (bid 96.5 <= 97), written 4 s later.
        ps.close_plays(self.conn, now=minutes(30) + timedelta(seconds=4),
                       extra_quotes=[self.ticker("BTC", 96.5, 96.7, minutes(30))])
        # ETH: a recorded spot snapshot touches the target (bid 210 >= 209).
        self.spot("ETH", minutes(40), 210.0, 210.4)
        ps.settle_due(self.conn, now=minutes(40))
        # SOL: nothing touched in 24 h; the first quote after the limit closes it.
        self.spot("SOL", minutes(2) + timedelta(hours=24, seconds=20), 10.0, 10.2)
        ps.settle_due(self.conn, now=minutes(3) + timedelta(hours=24))
        state = self.reader().read(minutes(4) + timedelta(hours=24))
        self.assertEqual(state["open_plays"], [])
        by_asset = {item["asset"]: item for item in state["history"]}
        closes = {close.play_id: close for close in ps.read_closes(self.conn)}

        btc = by_asset["BTC"]
        self.assertEqual((btc["exit_reason"], btc["exit_reason_text"], btc["exit_source"]),
                         ("stop", "Hit the stop", "ticker"))
        self.assertEqual(btc["record_lag_seconds"], 4.0)
        self.assertEqual(btc["closed_at"], ps.utc_text(minutes(30)))
        self.assertEqual(btc["delay_seconds"], 0.0)
        self.assertEqual((btc["stop"], btc["target"]), (str(stop.stop), str(stop.target)))
        self.assertIn("It hit the stop: the price fell to the loss limit of 97", btc["sentence"])
        self.assertIn("sold at 96.5", btc["sentence"])  # the observed bid, never the level
        self.assertIn("commissions cost", btc["sentence"])

        eth = by_asset["ETH"]
        self.assertEqual((eth["exit_reason"], eth["exit_reason_text"], eth["exit_source"]),
                         ("target", "Hit the target", "spot_snapshot"))
        self.assertEqual(eth["record_lag_seconds"], 0.0)
        self.assertIn("It hit the target: the price rose to the profit goal of", eth["sentence"])
        self.assertIn("sold at 210", eth["sentence"])
        self.assertEqual(D(eth["target"]), target.target)

        sol = by_asset["SOL"]
        self.assertEqual((sol["exit_reason"], sol["exit_reason_text"], sol["exit_source"]),
                         ("time", "Closed at the 24-hour limit", "spot_snapshot"))
        self.assertEqual(sol["delay_seconds"], 20.0)
        self.assertEqual(D(sol["stop"]), timed.stop)
        self.assertIn("It reached the 24-hour limit without hitting the stop or the target", sol["sentence"])
        self.assertIn("bought back at 10.2", sol["sentence"])
        self.assertEqual(sol["sentence"], paper_texts.result_sentence(
            asset="SOL", direction="SHORT", outcome=closes[timed.play_id].outcome.value,
            net=closes[timed.play_id].net, gross_mid=closes[timed.play_id].gross_mid,
            spread_cost=closes[timed.play_id].spread_cost, fees=closes[timed.play_id].fees,
            entry_bid=D("9.9"), entry_ask=D("10.1"), exit_bid=D("10.0"), exit_ask=D("10.2"), delay_seconds=20.0,
            exit_reason="time", stop=timed.stop, target=timed.target, hold_minutes=1440,
        ))

        speech = {entry["asset"]: entry["text"] for entry in state["activity"] if entry["kind"] == "play_close"}
        self.assertTrue(speech["BTC"].startswith("I closed the BTC play at the stop: lost"))
        self.assertTrue(speech["ETH"].startswith("I closed the ETH play at the target: won"))
        self.assertTrue(speech["SOL"].startswith("I closed the SOL play at the 24-hour limit:"))

    def test_legacy_play_next_to_an_ex1_play_keeps_the_old_wording(self) -> None:
        self.wallet()
        self.open_play("ev-old", "ADA", "LONG", 0.99, 1.01, T0)  # legacy: fixed 60 min, no levels
        self.spot("ADA", minutes(61), 1.04, 1.06)
        ps.settle_due(self.conn, now=minutes(62))
        self.open_play("ev-open", "DOT", "LONG", 4.9, 5.1, minutes(63))  # legacy and still open
        self.open_ex1("ev-new", "BTC", "LONG", 99.0, 101.0, minutes(64))
        state = self.reader().read(minutes(65))
        [old] = state["history"]
        self.assertEqual({key: old[key] for key in NO_REASON}, NO_REASON)
        self.assertNotIn("stop", old["sentence"])
        self.assertNotIn("limit", old["sentence"])
        dot, btc = state["open_plays"]
        self.assertEqual({key: dot[key] for key in NO_PLAN}, NO_PLAN)
        self.assertEqual(dot["due_at"], ps.utc_text(minutes(123)))  # its own recorded hold
        self.assertEqual(dot["steps"][2]["text"], "It closes by itself after 1 hour, win or lose.")
        self.assertEqual(btc["max_hold_minutes"], 1440)
        close_line = [entry["text"] for entry in state["activity"] if entry["kind"] == "play_close"]
        self.assertEqual(close_line, [paper_texts.play_close_line(asset="ADA", outcome=old["outcome"], net=old["net"])])

    def test_latest_legacy_play_does_not_set_the_shown_hold(self) -> None:
        self.wallet()
        self.open_play("ev-old", "ADA", "LONG", 0.99, 1.01, T0)
        state = self.reader(default_params={**DEFAULTS, "hold_minutes": 1440}).read(minutes(1))
        self.assertEqual(state["params"]["hold_minutes"], 1440)

    def test_default_hold_is_the_24_hour_policy(self) -> None:
        self.assertEqual(paper_reader.from_config().default_params["hold_minutes"], 24 * 60)
        state = self.reader(default_params={**DEFAULTS, "hold_minutes": 24 * 60}).read(T0)
        self.assertEqual(state["params"]["hold_minutes"], 1440)

    def test_database_not_migrated_reads_every_new_field_as_none(self) -> None:
        self.conn.close()
        os.remove(self.path)
        self.conn = sqlite3.connect(self.path)
        SnapshotStore(self.path).close()
        legacy.create_legacy_schema(self.conn, at=T0)
        self.spot("BTC", T0, 99.0, 101.0)
        legacy.insert_legacy_play(self.conn, "ev-old", "BTC", "LONG", 99.0, 101.0, T0)
        legacy.insert_legacy_play(self.conn, "ev-open", "ETH", "LONG", 199.0, 201.0, minutes(2))
        legacy.insert_legacy_close(self.conn, 1, "104.0", "106.0", minutes(61), minutes(60), snapshot_id=1, gross=500,
                                   spread=100, fees=52, outcome="WIN", closed_at=minutes(61))
        before = sha(self.path)
        state = self.reader().read(minutes(62))
        self.assertEqual(sha(self.path), before)
        self.assertTrue(state["available"])
        [item] = state["history"]
        self.assertEqual({key: item[key] for key in NO_REASON}, NO_REASON)
        [play] = state["open_plays"]
        self.assertEqual({key: play[key] for key in NO_PLAN}, NO_PLAN)
        conn = sqlite3.connect(self.path)
        try:
            self.assertFalse(ps.schema_current(conn))  # the reader added no column
        finally:
            conn.close()


class TestValuation(ReaderCase):
    """wallet.valuation and the per-play marks, reconciled in Decimal cents.

    Every read is wrapped by ``read``: the database file bytes, its mtime and its table list
    must be the same after the read."""

    def read(self, at: datetime) -> dict[str, object]:
        conn = sqlite3.connect(self.path)
        try:
            names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master ORDER BY name")]
        finally:
            conn.close()
        before = (sha(self.path), os.stat(self.path).st_mtime_ns, names)
        state = self.reader().read(at)
        conn = sqlite3.connect(self.path)
        try:
            after_names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master ORDER BY name")]
        finally:
            conn.close()
        self.assertEqual((sha(self.path), os.stat(self.path).st_mtime_ns, after_names), before)
        json.dumps(state)
        return state

    def legacy_play(self, event_id: str, asset: str, direction: str, bid: float, ask: float, at: datetime,
                    **options: object) -> int:
        """A play with no spot row at its entry (the caller records the quotes)."""
        return legacy.insert_legacy_play(self.conn, event_id, asset, direction, bid, ask, at, **options)  # type: ignore[arg-type]

    def assert_identity(self, valuation: dict[str, object], plays: list[dict[str, object]]) -> None:
        free_cash, cost = D(str(valuation["free_cash"])), D(str(valuation["open_cost_basis"]))
        realized_balance = D(str(valuation["realized_balance"]))
        self.assertEqual(free_cash, realized_balance - cost)
        self.assertEqual(cost, sum((D(str(play["cost_basis"])) for play in plays), D(0)))
        liquidation, open_net, equity = (D(str(valuation[key])) for key in
                                         ("liquidation_value", "open_net_pnl", "total_equity"))
        self.assertEqual(equity, free_cash + liquidation)
        self.assertEqual(equity, realized_balance + open_net)
        self.assertEqual(liquidation, sum((D(str(play["liquidation_value"])) for play in plays), D(0)))
        self.assertEqual(open_net, sum((D(str(play["open_net_pnl"])) for play in plays), D(0)))
        for play in plays:
            net = D(str(play["open_net_pnl"]))
            self.assertEqual(D(str(play["liquidation_value"])), D(str(play["cost_basis"])) + net)
            self.assertEqual(net, D(str(play["open_gross_mid"])) - D(str(play["open_spread_cost"]))
                             - D(str(play["open_fees"])))

    def assert_unmarked(self, state: dict[str, object], status: str) -> None:
        wallet = state["wallet"]
        assert isinstance(wallet, dict)
        valuation = wallet["valuation"]
        self.assertEqual((valuation["mark_status"], valuation["stale"]), (paper_reader.UNAVAILABLE, True))
        self.assertEqual({key: valuation[key] for key in ("liquidation_value", "open_net_pnl", "total_equity")},
                         {"liquidation_value": None, "open_net_pnl": None, "total_equity": None})
        self.assertIn(status, [item["status"] for item in valuation["unmarked"]])
        # Known cash, cost basis and realized P&L stay available.
        for key in ("free_cash", "open_cost_basis", "realized_balance", "realized_pnl"):
            self.assertRegex(valuation[key], r"^-?\d+\.\d\d$")
        plays = state["open_plays"]
        assert isinstance(plays, list)
        unmarked = [play for play in plays if play["mark"]["status"] == status]
        self.assertTrue(unmarked)
        for play in unmarked:
            self.assertTrue(play["mark"]["stale"])
            self.assertEqual(play["mark"]["reason_text"], paper_texts.MARK_REASONS[status])
            self.assertEqual({key: play[key] for key in ("liquidation_value", "open_net_pnl", "open_gross_mid",
                                                       "open_spread_cost", "open_fees")},
                             dict.fromkeys(("liquidation_value", "open_net_pnl", "open_gross_mid",
                                            "open_spread_cost", "open_fees")))
            self.assertEqual(play["cost_basis"], "100.00")

    def two_open_after_a_loss(self) -> None:
        """ETH short lost 3.55; then BTC long and SOL short open, both marked at minute 68."""
        self.wallet()
        self.open_play("ev-eth", "ETH", "SHORT", 199.0, 201.0, T0)
        self.spot("ETH", minutes(61), 203.0, 205.0)
        [close] = ps.settle_due(self.conn, now=minutes(63)).closed
        self.assertEqual(close.net, D("-3.55"))
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, minutes(64))
        self.open_play("ev-sol", "SOL", "SHORT", 9.9, 10.1, minutes(64))
        self.spot("BTC", minutes(68), 104.0, 106.0)
        self.spot("SOL", minutes(68), 9.4, 9.6)

    def test_two_open_positions_after_a_losing_close_reconcile_exactly(self) -> None:
        self.two_open_after_a_loss()
        state = self.read(minutes(70))
        wallet = state["wallet"]
        valuation = wallet["valuation"]
        # ETH short: gross -2.00 (mid 200 -> 204), spread 1.02 (-2 - 100 x (199 - 205)/199),
        # fees 0.53 (0.26 + 100 x 205/199 x 0.26 %): net -3.55. Balance 996.45.
        # BTC long at the 104 bid: 5.00 - 2.03 - 0.53 = 2.44 -> liquidation 102.44.
        # SOL short at the 9.6 ask: gross 5.00 (mid 10 -> 9.5), spread 1.97 (5 - 100 x (9.9 - 9.6)/9.9),
        # fees 0.51 (0.26 + 100 x 9.6/9.9 x 0.26 %) = 2.52 -> liquidation 102.52.
        self.assertEqual(
            {key: valuation[key] for key in ("free_cash", "open_cost_basis", "liquidation_value", "realized_balance",
                                             "realized_pnl", "open_net_pnl", "total_equity", "mark_status", "stale",
                                             "open_count", "unmarked", "currency", "as_of", "valuation_basis",
                                             "identity")},
            {"free_cash": "796.45", "open_cost_basis": "200.00", "liquidation_value": "204.96",
             "realized_balance": "996.45", "realized_pnl": "-3.55", "open_net_pnl": "4.96", "total_equity": "1001.41",
             "mark_status": paper_reader.ALL_MARKED, "stale": False, "open_count": 2, "unmarked": [],
             "currency": "EUR", "as_of": ps.utc_text(minutes(70)), "valuation_basis": "conservative_liquidation",
             "identity": "total_equity = free_cash + liquidation_value = realized_balance + open_net_pnl"},
        )
        self.assertEqual(valuation["freshness"], {"max_age_seconds": 600, "text": paper_texts.freshness_text(10)})
        self.assertEqual(valuation["valuation_basis_text"], paper_texts.VALUATION_BASIS_TEXT)
        by_asset = {play["asset"]: play for play in state["open_plays"]}
        self.assertEqual(
            {asset: (play["open_gross_mid"], play["open_spread_cost"], play["open_fees"], play["open_net_pnl"],
                     play["liquidation_value"]) for asset, play in by_asset.items()},
            {"BTC": ("5.00", "2.03", "0.53", "2.44", "102.44"), "SOL": ("5.00", "1.97", "0.51", "2.52", "102.52")},
        )
        self.assertEqual(by_asset["SOL"]["mark"]["ask"], "9.6")  # a short is bought back at the ask
        self.assert_identity(valuation, state["open_plays"])
        # Every legacy wallet field keeps its value.
        self.assertEqual(
            {key: wallet[key] for key in ("balance", "start_balance", "change", "change_pct", "open_stakes",
                                          "available_cash", "fees_total")},
            {"balance": "996.45", "start_balance": "1000.00", "change": "-3.55", "change_pct": "-0.36",
             "open_stakes": "200.00", "available_cash": "796.45", "fees_total": "0.53"},
        )
        [loss] = state["history"]
        self.assertEqual((loss["net"], loss["fees"], loss["outcome"]), ("-3.55", "0.53", "LOSS"))

    def test_the_entry_fee_is_counted_once(self) -> None:
        """Marked on its own entry quote, an open play is worth the stake minus the spread and
        the assumed fee of both legs, each once; the cost basis holds no fee."""
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        state = self.read(minutes(5))
        [play] = state["open_plays"]
        # gross 0; spread 1.98 (0 - 100 x (99/101 - 1)); fees 0.51 (0.26 + 100 x 99/101 x 0.26 % = 0.5149).
        self.assertEqual((play["cost_basis"], play["open_gross_mid"], play["open_spread_cost"], play["open_fees"],
                          play["open_net_pnl"], play["liquidation_value"]),
                         ("100.00", "0.00", "1.98", "0.51", "-2.49", "97.51"))
        valuation = state["wallet"]["valuation"]
        self.assertEqual((valuation["free_cash"], valuation["total_equity"]), ("900.00", "997.51"))
        self.assert_identity(valuation, state["open_plays"])

    def test_cent_rounding_keeps_the_shown_parts_adding_up(self) -> None:
        """The parts are rounded to the cent one by one and the net is their exact difference,
        so the liquidation equals stake + the shown parts, not the cent of the unrounded net."""
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        self.spot("BTC", minutes(4), 99.05, 99.15)
        [play] = self.read(minutes(5))["open_plays"]
        # gross -0.90 (mid 100 -> 99.1); spread 1.030693 -> 1.03; fees 0.514980 -> 0.51.
        # Unrounded net -2.445673 would be -2.45; the shown parts give -2.44.
        self.assertEqual((play["open_gross_mid"], play["open_spread_cost"], play["open_fees"], play["open_net_pnl"],
                          play["liquidation_value"]), ("-0.90", "1.03", "0.51", "-2.44", "97.56"))

    def test_missing_quote(self) -> None:
        self.wallet()
        self.legacy_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)  # no spot row at all
        self.assert_unmarked(self.read(minutes(5)), paper_reader.MISSING_QUOTE)

    def test_quote_of_another_pair_is_not_a_mark(self) -> None:
        self.wallet()
        self.legacy_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        self.conn.execute(
            "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES "
            "('BTC', 'BTC/USD', 'USD', ?, 104.0, 106.0, 'online'), ('ETH', 'ETH/EUR', 'EUR', ?, 104.0, 106.0, 'online')",
            (minutes(4).isoformat(), minutes(4).isoformat()),
        )
        self.conn.commit()
        state = self.read(minutes(5))
        self.assert_unmarked(state, paper_reader.MISSING_QUOTE)
        self.assertIsNone(state["open_plays"][0]["mark"]["bid"])

    def test_stale_quote(self) -> None:
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)  # its entry row is the only quote
        self.assertEqual(self.read(minutes(10))["open_plays"][0]["mark"]["status"], paper_reader.MARKED)
        state = self.read(minutes(10) + timedelta(seconds=1))
        self.assert_unmarked(state, paper_reader.STALE_QUOTE)
        self.assertIsNone(state["open_plays"][0]["gross_now"])  # the legacy "now" price agrees

    def test_crossed_non_finite_and_not_positive_quotes_are_invalid(self) -> None:
        for label, bid, ask, status in (
            ("crossed", 105.0, 104.0, "online"),
            ("infinite", float("inf"), 101.0, "online"),
            ("nan text", "NaN", 101.0, "online"),
            ("zero", 0.0, 101.0, "online"),
            ("negative", -1.0, 101.0, "online"),
            ("not trading", 99.0, 101.0, "cancel_only"),
        ):
            with self.subTest(quote=label):
                self.tearDown()
                self.setUp()
                self.wallet()
                self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)  # old by now: not a mark
                self.spot("BTC", minutes(24), bid, ask, status)
                state = self.read(minutes(25))
                self.assert_unmarked(state, paper_reader.INVALID_QUOTE)

    def test_older_valid_quote_does_not_hide_an_unusable_fresh_one(self) -> None:
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        self.spot("BTC", minutes(20), 105.0, 104.0)  # crossed, fresh
        self.spot("BTC", minutes(21), 100.0, 101.0)  # valid, fresh: the mark
        [play] = self.read(minutes(25))["open_plays"]
        self.assertEqual((play["mark"]["status"], play["mark"]["bid"]), (paper_reader.MARKED, "100.0"))

    def test_one_unmarked_play_makes_every_dependent_total_unavailable(self) -> None:
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, minutes(20))  # fresh entry row: marked
        self.legacy_play("ev-eth", "ETH", "LONG", 199.0, 201.0, minutes(20))  # no quote
        state = self.read(minutes(25))
        self.assert_unmarked(state, paper_reader.MISSING_QUOTE)
        valuation = state["wallet"]["valuation"]
        self.assertEqual((valuation["free_cash"], valuation["open_cost_basis"]), ("800.00", "200.00"))
        self.assertEqual(valuation["unmarked"], [{"play_id": 2, "pair": "ETH/EUR", "status": "missing_quote"}])
        btc = state["open_plays"][0]
        self.assertEqual((btc["mark"]["status"], btc["liquidation_value"]), (paper_reader.MARKED, "97.51"))

    def test_no_open_play(self) -> None:
        self.wallet()
        valuation = self.read(minutes(5))["wallet"]["valuation"]
        self.assertEqual(
            {key: valuation[key] for key in ("free_cash", "open_cost_basis", "liquidation_value", "realized_balance",
                                             "realized_pnl", "open_net_pnl", "total_equity", "mark_status", "stale")},
            {"free_cash": "1000.00", "open_cost_basis": "0.00", "liquidation_value": "0.00",
             "realized_balance": "1000.00", "realized_pnl": "0.00", "open_net_pnl": "0.00", "total_equity": "1000.00",
             "mark_status": paper_reader.NO_OPEN_POSITIONS, "stale": False},
        )

    def test_empty_database(self) -> None:
        self.conn.close()
        os.remove(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA user_version = 0")  # an existing but empty database file
        state = self.read(T0)
        self.assertEqual((state["available"], state["reason"], state["wallet"]),
                         (False, paper_reader.REASON_NO_TABLES, None))

    def test_stored_fee_per_leg_and_fx_label(self) -> None:
        """Each play shows its own stored fee (26 stays 26, a 40 bps row stays 40), ASSUMED
        and unverified; a USD-quoted play is marked as a hypothetical FX-excluded simulation."""
        self.wallet()
        self.legacy_play("ev-old", "BTC", "LONG", 99.0, 101.0, T0, fee_bps="26", quote="USD")
        self.spot_pair("BTC/USD", minutes(61), 104.0, 106.0)
        ps.settle_due(self.conn, now=minutes(62))
        self.legacy_play("ev-new", "ETH", "LONG", 199.0, 201.0, minutes(63), fee_bps="40")
        self.spot("ETH", minutes(64), 199.0, 201.0)
        state = self.read(minutes(65))
        [item] = state["history"]
        [play] = state["open_plays"]
        fx_text = paper_texts.fx_excluded_text("USD", "EUR")
        self.assertEqual({key: item[key] for key in ("fee_bps", "fee_source", "account_tier_verified", "fee_text",
                                                     "fx_excluded", "fx_text")},
                         {**FEE_26, "fx_excluded": True, "fx_text": fx_text})
        self.assertEqual({key: play[key] for key in ("fee_bps", "fee_source", "account_tier_verified", "fx_excluded",
                                                     "fx_text")},
                         {"fee_bps": "40", "fee_source": "ASSUMED", "account_tier_verified": False,
                          "fx_excluded": False, "fx_text": None})
        self.assertIn("0.4%", play["fee_text"])
        self.assertIn("not EUR inventory", fx_text)
        self.assertIn("FX excluded", fx_text)
        # The 40 bps play is valued with its own stored fee: 0.40 + 100 x 199/201 x 0.40 % = 0.796 -> 0.80.
        self.assertEqual(play["open_fees"], "0.80")

    def test_a_stored_fee_that_cannot_be_priced_is_reported_not_raised(self) -> None:
        self.wallet()
        self.open_play("ev-ok", "ETH", "LONG", 199.0, 201.0, T0)
        self.legacy_play("ev-bad", "BTC", "LONG", 99.0, 101.0, T0, fee_bps="-1")
        self.spot("BTC", minutes(1), 99.0, 101.0)
        state = self.read(minutes(2))
        self.assertEqual((state["available"], state["reason"], state["wallet"]),
                         (False, paper_reader.REASON_UNREADABLE, None))

    def spot_pair(self, pair: str, at: datetime, bid: float, ask: float) -> None:
        asset, quote = pair.split("/")
        self.conn.execute(
            "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES (?, ?, ?, ?, ?, ?, 'online')",
            (asset, pair, quote, at.isoformat(), bid, ask),
        )
        self.conn.commit()

    def test_legacy_database_without_ex1_columns_is_valued(self) -> None:
        self.conn.close()
        os.remove(self.path)
        self.conn = sqlite3.connect(self.path)
        SnapshotStore(self.path).close()
        legacy.create_legacy_schema(self.conn, at=T0)
        self.spot("BTC", T0, 99.0, 101.0)
        legacy.insert_legacy_play(self.conn, "ev-old", "BTC", "LONG", 99.0, 101.0, T0)
        legacy.insert_legacy_play(self.conn, "ev-open", "ETH", "LONG", 199.0, 201.0, minutes(2))
        legacy.insert_legacy_close(self.conn, 1, "104.0", "106.0", minutes(61), minutes(60), snapshot_id=1, gross=500,
                                   spread=100, fees=52, outcome="WIN", closed_at=minutes(61))
        self.spot("ETH", minutes(61), 199.0, 201.0)
        state = self.read(minutes(62))
        valuation = state["wallet"]["valuation"]
        # Realized +3.48 (5.00 - 1.00 - 0.52); ETH at its entry quote: gross 0, spread 1.00
        # (100 x (1 - 199/201) = 0.995025), fees 0.52 (0.26 + 100 x 199/201 x 0.26 %) = -1.52.
        self.assertEqual(
            {key: valuation[key] for key in ("free_cash", "open_cost_basis", "liquidation_value", "realized_pnl",
                                             "open_net_pnl", "total_equity")},
            {"free_cash": "903.48", "open_cost_basis": "100.00", "liquidation_value": "98.48", "realized_pnl": "3.48",
             "open_net_pnl": "-1.52", "total_equity": "1001.96"},
        )
        self.assert_identity(valuation, state["open_plays"])
        self.assertEqual(state["history"][0]["fee_bps"], "26")
        conn = sqlite3.connect(self.path)
        try:
            self.assertFalse(ps.schema_current(conn))  # still not migrated: the reader added no column
        finally:
            conn.close()


class TestActivity(ReaderCase):
    def event(self, event_id: str, at: datetime, asset: str, demand: str | None, kind: str = "RADAR_ALERT") -> None:
        self.conn.execute(
            "INSERT INTO events (event_id, dedup_key, ts, type, asset, direction, model_demand, status) "
            "VALUES (?, ?, ?, ?, ?, 'LONG', ?, 'PENDING')",
            (event_id, f"key-{event_id}", at.isoformat(), kind, asset, demand),
        )
        self.conn.commit()

    def rows(self) -> None:
        run = "INSERT INTO radar_runs (run_id, ts, mode, assets_eligible, shortlist_count, warmup, api_failures) VALUES (?, ?, 'FULL', 600, 2, 0, 0)"
        self.conn.execute(run, ("run-a", minutes(1).isoformat()))
        self.conn.execute(run, ("run-old", (T0 - timedelta(hours=25)).isoformat()))
        self.conn.commit()
        self.event("ev-real", minutes(2), "SOL", "FABLE")
        self.event("MOCK-1", minutes(3), "BTC", "FABLE")
        self.event("ev-mock", minutes(3), "BTC", "FABLE", kind="MOCK_TEST_EVENT")
        self.event("ev-sonnet", minutes(4), "ETH", "SONNET")
        self.event("ev-ignore", minutes(5), "ADA", "IGNORE")
        self.event("ev-none", minutes(5), "XRP", None)
        # An analysis row is not a router decision and is never read (the Claude API is off).
        self.conn.execute(
            "INSERT INTO model_analyses (event_id, model, model_version, requested_at, completed_at, status) "
            "VALUES ('ev-ignore', ?, 'v1', ?, NULL, 'PENDING')",
            (config.CLAUDE_BRIDGE_MODEL_IDS["FABLE"], minutes(6).isoformat()),
        )
        self.conn.commit()
        qrs.record_batch(self.conn, [qrs.QwenReviewRow(
            run_id="run-a", cycle_ts=minutes(1).isoformat(), mode="shadow", asset="SOL", setup_type="BREAKOUT",
            direction="LONG", anomaly_score=None, opportunity_score=None, tradeability_score=None, router_decision=None,
            batch_status="OK", veto=False, confidence="HIGH", review_direction="LONG", call_sonnet=True, call_fable=False,
            attempts=1,
        )], now=minutes(3))

    def test_every_source_with_stable_ids(self) -> None:
        self.wallet()
        self.rows()
        self.open_play("ev-real", "SOL", "LONG", 9.9, 10.1, minutes(7))
        reader = self.reader()
        first, second = reader.read(minutes(8)), reader.read(minutes(9))
        self.assertEqual(first["activity"], second["activity"])
        activity = first["activity"]
        self.assertEqual(
            [(entry["id"], entry["agent"], entry["kind"], entry["asset"]) for entry in activity],
            [("cycle:run-a", "scout", "cycle", None),
             ("alert:ev-real", "scout", "alert", "SOL"), ("route:ev-real", "boss", "fable", "SOL"),
             ("qwen:run-a:SOL", "analyst", "qwen", "SOL"),
             ("alert:ev-sonnet", "scout", "alert", "ETH"), ("route:ev-sonnet", "strategist", "sonnet", "ETH"),
             ("alert:ev-ignore", "scout", "alert", "ADA"), ("alert:ev-none", "scout", "alert", "XRP"),
             ("play_open:1", "treasurer", "play_open", "SOL")],
        )
        self.assertEqual([entry["ts"] for entry in activity], sorted(entry["ts"] for entry in activity))
        texts = {entry["id"]: entry["text"] for entry in activity}
        self.assertEqual(texts["cycle:run-a"], paper_texts.cycle_line(assets_eligible=600, shortlist_count=2, warmup=0, api_failures=0))
        self.assertEqual(texts["alert:ev-real"], paper_texts.alert_line(asset="SOL", direction="LONG"))
        self.assertEqual(texts["qwen:run-a:SOL"], paper_texts.qwen_line(asset="SOL", batch_status="OK", veto=0, confidence="HIGH"))
        self.assertEqual(texts["route:ev-real"], paper_texts.router_line(decision="FABLE", asset="SOL"))
        self.assertEqual(texts["route:ev-sonnet"], paper_texts.router_line(decision="SONNET", asset="ETH"))
        self.assertEqual(texts["play_open:1"], paper_texts.play_open_line(asset="SOL", direction="LONG", stake=D("100")))
        self.assertFalse(any(entry["id"].startswith("model:") for entry in activity))
        self.assertFalse(any("BTC" == entry["asset"] for entry in activity))  # both mock alerts excluded
        agents = {agent["id"]: agent for agent in first["agents"]}
        self.assertEqual(set(agents), {"scout", "analyst", "strategist", "boss", "treasurer"})
        self.assertEqual(agents["boss"]["last_activity_ts"], ps.utc_text(minutes(2)))
        self.assertEqual(agents["strategist"]["last_activity_ts"], ps.utc_text(minutes(4)))
        self.assertEqual(agents["scout"]["color"], "#5B9DF6")

    def test_router_decision_brings_in_only_its_agent(self) -> None:
        cases = [("SONNET", ["alert", "sonnet"]), ("FABLE", ["alert", "fable"]), ("IGNORE", ["alert"]), (None, ["alert"])]
        for index, (demand, kinds) in enumerate(cases):
            with self.subTest(demand=demand):
                self.conn.execute("DELETE FROM events")
                self.event(f"ev-{index}", minutes(1), "SOL", demand)
                activity = self.reader().read(minutes(2))["activity"]
                self.assertEqual([entry["kind"] for entry in activity], kinds)

    def test_mock_alert_routed_to_fable_does_not_wake_the_boss(self) -> None:
        self.event("MOCK-9", minutes(1), "SOL", "FABLE")
        self.event("ev-test", minutes(1), "SOL", "FABLE", kind="MOCK_TEST_EVENT")
        self.event("ev-other", minutes(1), "SOL", "FABLE", kind="SOMETHING_ELSE")
        state = self.reader().read(minutes(2))
        self.assertEqual(state["activity"], [])
        boss = next(agent for agent in state["agents"] if agent["id"] == "boss")
        self.assertIsNone(boss["last_activity_ts"])

    def test_analysis_rows_alone_produce_no_activity(self) -> None:
        self.conn.execute(
            "INSERT INTO model_analyses (event_id, model, model_version, requested_at, completed_at, status) "
            "VALUES ('ev-x', ?, 'v1', ?, ?, 'SUCCESS')",
            (config.CLAUDE_BRIDGE_MODEL_IDS["FABLE"], minutes(1).isoformat(), minutes(1).isoformat()),
        )
        self.conn.commit()
        self.assertEqual(self.reader().read(minutes(2))["activity"], [])

    def test_close_activity(self) -> None:
        self.wallet()
        self.open_play("ev-btc", "BTC", "LONG", 99.0, 101.0, T0)
        self.spot("BTC", minutes(61), 104.0, 106.0)
        [close] = ps.settle_due(self.conn, now=minutes(62)).closed
        entry = self.reader().read(minutes(63))["activity"][-1]
        self.assertEqual((entry["id"], entry["kind"], entry["ts"]), ("play_close:1", "play_close", ps.utc_text(minutes(62))))
        self.assertEqual(entry["text"], paper_texts.play_close_line(asset="BTC", outcome="WIN", net=close.net))
        self.assertEqual(entry["text"], f"I closed the BTC play: won {paper_texts.format_money(close.net)}.")

    def test_nothing_is_synthesised_without_rows(self) -> None:
        with mock.patch.object(config, "CLAUDE_BRIDGE_DISPATCH_ENABLED", False):
            state = paper_reader.PaperReader(
                self.path, enabled=True, qwen_enabled=True, default_params=DEFAULTS
            ).read(T0)
        self.assertEqual(state["activity"], [])
        for agent in state["agents"]:
            self.assertIsNone(agent["last_activity_ts"])
        agents = {agent["id"]: agent for agent in state["agents"]}
        # Enabled = the router decision that drives them is recorded; asleep = no activity.
        self.assertTrue(agents["boss"]["enabled"])
        self.assertTrue(agents["strategist"]["enabled"])
        self.assertFalse(self.reader(qwen_enabled=False).read(T0)["agents"][1]["enabled"])

    def test_window_and_limit(self) -> None:
        for index in range(70):
            self.conn.execute(
                "INSERT INTO radar_runs (run_id, ts, mode) VALUES (?, ?, 'HEARTBEAT')", (f"r{index:02d}", minutes(index).isoformat())
            )
        self.conn.execute("INSERT INTO radar_runs (run_id, ts, mode) VALUES ('future', ?, 'FULL')", (minutes(500).isoformat(),))
        self.conn.execute("INSERT INTO radar_runs (run_id, ts, mode) VALUES ('naive', '2026-09-29T11:00:00', 'FULL')")
        self.conn.commit()
        activity = self.reader().read(minutes(80))["activity"]
        self.assertEqual(len(activity), paper_reader.ACTIVITY_LIMIT)
        self.assertEqual(activity[0]["id"], "cycle:r10")
        self.assertEqual(activity[-1]["id"], "cycle:r69")
        late = self.reader().read(minutes(80) + timedelta(hours=24))["activity"]
        self.assertEqual([entry["id"] for entry in late], ["cycle:future"])
        self.assertTrue(all(entry["id"] not in ("cycle:future", "cycle:naive") for entry in activity))


class TestDecisionSteps(unittest.TestCase):
    def test_missing_facts_drop_their_clause(self) -> None:
        steps = paper_texts.decision_steps()
        self.assertEqual([step["text"] for step in steps], [
            "A coin was moving unusually.", "Decided to open the play.", "It closes by itself at the planned time, win or lose.",
        ])

    def test_fixed_wording_has_no_digits(self) -> None:
        steps = paper_texts.decision_steps(
            asset="ADA", direction="SHORT", assets_eligible=7, scores={"opportunity_score": 51.4}, hold_minutes=45,
        )
        text = " ".join(step["text"] for step in steps)
        self.assertEqual(sorted(re.findall(r"\d+", text)), ["45", "51", "7"])
        self.assertIn("betting it goes down", text)


class TestBridge(unittest.TestCase):
    STATE_KEYS = {
        "process", "system_status", "funnel", "next_full_cycle_eta_seconds", "agents", "agent_connections",
        "agent_communications", "event_queue", "latest_event", "alerts_preview", "log_lines",
    }

    def test_get_paper_state_is_read_only_and_get_state_unchanged(self) -> None:
        from ui import bridge

        with tempfile.TemporaryDirectory(prefix="paper-bridge-") as tmp:
            path = os.path.join(tmp, "radar_state.sqlite")
            with mock.patch.object(config, "SQLITE_PATH", path), \
                    mock.patch.object(config, "OUTPUT_V08_PATH", os.path.join(tmp, "output.json")), \
                    mock.patch.object(bridge.process_manager, "ProcessManager"), \
                    mock.patch.object(bridge.ui_state, "load", return_value={}):
                api = bridge.Api()
                try:
                    state = api.get_state()
                    paper = api.get_paper_state()
                finally:
                    api.close()
            self.assertEqual(set(state), self.STATE_KEYS)
            json.dumps(paper)
            self.assertFalse(paper["available"])
            self.assertEqual(paper["reason"], paper_reader.REASON_NO_TABLES)
            conn = sqlite3.connect(path)
            try:
                self.assertFalse(ps.schema_present(conn))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
