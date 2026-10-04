"""Read-only Pilot shadow reader (ui/pilot_reader.py) on temporary SQLite files only.

The rows are written through the real pilot store (radar_v08/adapters/pilot_store.py) with
the fixtures of tests/test_pilot_store.py: envelope 240.00 EUR at the EX-1 values, fee 26
bps per leg, quote 99.9/100, ATR 1, lot 2 decimals / ordermin 0.01 / costmin 0.50 / tick
0.01. The first entry is 0.19 BTC at 100 (stop 98, target 104, planned loss 0.5731, cost
basis 19.05); marked at the entry bid 99.9 the equity is 239.881.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any
from unittest import mock

from radar_v08.adapters import pilot_store as ps
from radar_v08.domain.risk import NoTradeReason, unrealized_mark
from radar_v08.store import SnapshotStore
from ui import paper_texts, pilot_reader
from ui.pilot_reader import PilotReader

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_pilot_store import ENVELOPE, FEE, T0, candidate, rules  # noqa: E402

LATER = T0 + timedelta(minutes=1)


def sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ReaderCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pilot-reader-")
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "radar_state.sqlite")
        SnapshotStore(self.path).close()  # every existing radar table, as in production
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(lambda: self.conn.close())

    def reader(self, *, enabled: bool = True) -> PilotReader:
        return PilotReader(self.path, enabled=enabled)

    def ready(self) -> None:
        ps.ensure_schema(self.conn)
        ps.ensure_account(self.conn, envelope=ENVELOPE, now=T0)

    def offer(self, *candidates: ps.PilotCandidate, now: datetime = T0) -> ps.OpenReport:
        return ps.open_candidates(self.conn, list(candidates), envelope=ENVELOPE, fee_bps=FEE, now=now)

    def add_spot(self, pair: str, ts: datetime, bid: float, ask: float) -> None:
        self.conn.execute(
            "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES (?, ?, ?, ?, ?, ?, 'online')",
            (pair.split("/")[0], pair, pair.split("/")[1], ts.isoformat(), bid, ask),
        )
        self.conn.commit()

    def snapshot(self) -> tuple[str, int, dict[str, int]]:
        """File hash, mtime and the row count of every table: what a read must not change."""
        names = [row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        counts = {name: self.conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in names}
        return sha(self.path), os.stat(self.path).st_mtime_ns, counts


class TestReadOnly(ReaderCase):
    def test_connection_refuses_writes(self) -> None:
        self.ready()
        conn = self.reader().connect()
        try:
            for sql in (
                "CREATE TABLE intruder (x INTEGER)",
                f"INSERT INTO {ps.KILL_SWITCH_TABLE} (state, reason, actor, recorded_at) VALUES ('engaged', 'x', 'y', 'z')",
                f"DELETE FROM {ps.ACCOUNT_TABLE}",
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.OperationalError):
                    conn.execute(sql)
            conn.execute("PRAGMA query_only = OFF")  # the URI mode=ro still refuses
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE intruder (x INTEGER)")
        finally:
            conn.close()

    def test_missing_database_is_not_created(self) -> None:
        missing = os.path.join(self._tmp.name, "absent.sqlite")
        state = PilotReader(missing, enabled=True).read(T0)
        self.assertFalse(os.path.exists(missing))
        self.assertEqual((state["available"], state["reason"]), (False, pilot_reader.REASON_NO_DATABASE))
        self.assertEqual(state["reason_text"], paper_texts.PILOT_REASONS["no_database"])

    def test_database_without_pilot_tables_gets_no_table(self) -> None:
        before = self.snapshot()
        state = self.reader().read(T0)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(ps.schema_present(self.conn))
        names = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master")}
        self.assertFalse(names & set(ps.TABLES))
        self.assertEqual((state["available"], state["reason"]), (False, pilot_reader.REASON_NO_TABLES))
        self.assertIsNone(state["account"])
        self.assertEqual((state["limits"], state["recent"]), ([], []))

    def test_tables_without_account_get_no_row(self) -> None:
        ps.ensure_schema(self.conn)
        before = self.snapshot()
        state = self.reader().read(T0)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(ps.read_account(self.conn))
        self.assertEqual((state["available"], state["reason"]), (False, pilot_reader.REASON_NO_ACCOUNT))
        self.assertIsNone(state["last_sizing"])

    def test_full_read_writes_nothing(self) -> None:
        self.ready()
        self.offer(candidate("e1"), candidate("e2", quote="USD"))
        ps.engage_kill_switch(self.conn, reason="checking", actor="operator", now=LATER)
        before = self.snapshot()
        for minute in (1, 2, 60 * 25):  # also a new UTC day: no day-start mark is written
            self.assertTrue(self.reader().read(T0 + timedelta(minutes=minute))["available"])
        self.assertEqual(self.snapshot(), before)

    def test_file_that_is_not_a_database_is_reported_not_raised(self) -> None:
        garbage = os.path.join(self._tmp.name, "garbage.sqlite")
        Path(garbage).write_bytes(b"not a database at all" * 100)
        before = sha(garbage)
        state = PilotReader(garbage, enabled=True).read(T0)
        self.assertEqual(sha(garbage), before)
        self.assertEqual((state["available"], state["reason"]), (False, pilot_reader.REASON_UNREADABLE))

    def test_store_error_mid_read_gives_an_empty_unreadable_payload(self) -> None:
        self.ready()
        self.offer(candidate("e1"))
        with mock.patch.object(ps, "read_decisions", side_effect=sqlite3.OperationalError("database is locked")):
            state = self.reader().read(LATER)
        self.assertEqual((state["available"], state["reason"]), (False, pilot_reader.REASON_UNREADABLE))
        self.assertEqual((state["account"], state["open_position"], state["limits"]), (None, None, []))

    def test_wal_database_is_read_while_the_writer_is_open(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.ready()
        self.offer(candidate("e1"))
        self.assertEqual(self.reader().read(LATER)["open_count"], 1)


class TestPayload(ReaderCase):
    def test_open_position_account_and_limits(self) -> None:
        self.ready()
        self.offer(candidate("e1"), candidate("e2", quote="USD"), candidate("e3", direction="SHORT"))
        state = self.reader().read(LATER)
        self.assertTrue(state["available"])
        self.assertEqual((state["reason"], state["currency"], state["direction"]), (None, "EUR", "LONG"))
        self.assertEqual(state["generated_at"], "2026-09-29T10:01:00.000000+00:00")
        account = state["account"]
        # Equity 239.881: 240 - 19.05 basis + 0.19 x 99.9 - 0.05 exit fee (0.049... up).
        self.assertEqual(
            {k: account[k] for k in ("assigned", "equity", "change", "realized", "cash", "day_start", "day_change",
                                     "high_water", "drawdown_pct")},
            {"assigned": "240.00", "equity": "239.88", "change": "-0.12", "realized": "0.00", "cash": "220.95",
             "day_start": "240.00", "day_change": "-0.12", "high_water": "240.00", "drawdown_pct": "0.05"},
        )
        self.assertEqual(account["envelope_policy_id"], "ex1_envelope_v1")
        limits = {limit["id"]: limit for limit in state["limits"]}
        self.assertEqual(list(limits), ["per_entry_loss", "aggregate_loss", "gross_notional", "cash_buffer",
                                        "daily_loss", "drawdown", "positions", "leverage"])
        expected = {
            # 239.881 x 0.25 % = 0.5997; the open planned loss 0.5731.
            "per_entry_loss": ("0.25", "0.60", "0.57", None),
            # 1.199405 - 0.5731 = 0.626305.
            "aggregate_loss": ("0.50", "1.20", "0.57", "0.63"),
            # 23.9881 - 19.00 = 4.9881.
            "gross_notional": ("10", "23.99", "19.00", "4.99"),
            # 220.95 cash - 23.9881 reserve = 196.9619; in use: the cost basis 19.05.
            "cash_buffer": ("10", "23.99", "19.05", "196.96"),
            # 240 x 1 % = 2.40; 240 - 239.881 = 0.119.
            "daily_loss": ("1", "2.40", "0.12", "2.28"),
            "drawdown": ("3", "7.20", "0.12", "7.08"),
        }
        for ident, (pct, amount, used, left) in expected.items():
            with self.subTest(limit=ident):
                row = limits[ident]
                self.assertEqual((row["pct"], row["amount"], row["used"], row["left"]), (pct, amount, used, left))
        self.assertIsNone(limits["per_entry_loss"]["cap"])
        self.assertEqual((limits["positions"]["max"], limits["positions"]["open"]), (1, 1))
        self.assertEqual(limits["leverage"]["max"], 0)
        position = state["open_position"]
        self.assertEqual(
            {k: position[k] for k in ("pair", "quantity", "entry", "stop", "target", "notional", "planned_loss",
                                      "stress_exit", "opened_at", "due_at")},
            {"pair": "BTC/EUR", "quantity": "0.19", "entry": "100", "stop": "98", "target": "104", "notional": "19.00",
             "planned_loss": "0.57", "stress_exit": "97.510", "opened_at": "2026-09-29T10:00:00.000000+00:00",
             "due_at": "2026-09-30T10:00:00.000000+00:00"},
        )
        self.assertEqual(state["open_count"], 1)
        self.assertEqual(state["kill_switch"], {"engaged": False, "reason": None, "actor": None, "recorded_at": None})
        self.assertEqual(state["locks"], {"active": [], "recent": [], "total": 0})

    def test_last_sizing_shows_the_four_quantities_and_the_minimums(self) -> None:
        self.ready()
        self.offer(candidate("e1"), candidate("e2", quote="USD"))
        sizing = self.reader().read(LATER)["last_sizing"]
        # e2 never reached the sizing, so the last sizing is e1's.
        self.assertEqual((sizing["event_id"], sizing["outcome"], sizing["binding"]), ("e1", "OPENED", "per_entry_loss"))
        self.assertEqual((sizing["equity"], sizing["ask"], sizing["stop"], sizing["loss_per_unit"]),
                         ("240.00", "100", "98", "3.00352600"))
        # 0.60 / 3.003526, 1.20 / 3.003526, 24 / 100 and 216 / 100.26, rounded down to 8 places.
        self.assertEqual(
            [(c["id"], c["budget"], c["quantity"], c["binding"]) for c in sizing["candidates"]],
            [("per_entry_loss", "0.60", "0.19976520", True), ("aggregate_loss", "1.20", "0.39953041", False),
             ("notional", "24.00", "0.24000000", False), ("cash", "216.00", "2.15439856", False)],
        )
        self.assertEqual((sizing["lot_decimals"], sizing["lot_quantity"], sizing["passes"], sizing["quantity"]),
                         (2, "0.19", 0, "0.19"))
        self.assertEqual((sizing["notional"], sizing["entry_fee"], sizing["exit_fee"], sizing["planned_loss"]),
                         ("19.00", "0.05", "0.05", "0.57"))
        self.assertEqual(sizing["minimums"], {"checked_quantity": "0.19", "ordermin": "0.01", "order_ok": True,
                                              "cost": "19.00", "costmin": "0.50", "cost_ok": True})

    def test_order_minimum_refusal_leaves_the_unreached_values_null(self) -> None:
        self.ready()
        self.offer(candidate("e1", entry=rules(ordermin="1")))
        state = self.reader().read(LATER)
        sizing = state["last_sizing"]
        self.assertEqual((sizing["reason"], sizing["detail"]), ("below_order_minimum", "0.19<1"))
        self.assertEqual((sizing["quantity"], sizing["notional"], sizing["planned_loss"], sizing["entry_fee"]),
                         (None, None, None, None))
        self.assertEqual(sizing["minimums"], {"checked_quantity": "0.19", "ordermin": "1", "order_ok": False,
                                              "cost": None, "costmin": "0.50", "cost_ok": None})
        self.assertIsNone(state["open_position"])

    def test_cost_minimum_refusal(self) -> None:
        self.ready()
        self.offer(candidate("e1", entry=rules(costmin="50")))
        minimums = self.reader().read(LATER)["last_sizing"]["minimums"]
        self.assertEqual(minimums, {"checked_quantity": "0.19", "ordermin": "0.01", "order_ok": True,
                                    "cost": "19.00", "costmin": "50", "cost_ok": False})

    def test_values_not_recorded_are_null_never_zero(self) -> None:
        self.ready()
        # The next UTC day before any evaluation: no day start recorded, no decision yet.
        state = self.reader().read(T0 + timedelta(days=1))
        self.assertIsNone(state["account"]["day_start"])
        self.assertIsNone(state["account"]["day_change"])
        daily = next(limit for limit in state["limits"] if limit["id"] == "daily_loss")
        self.assertEqual((daily["pct"], daily["amount"], daily["used"], daily["left"]), ("1", None, None, None))
        per_entry = state["limits"][0]
        self.assertEqual((per_entry["used"], per_entry["left"]), (None, None))
        self.assertEqual((state["open_position"], state["last_sizing"]), (None, None))
        self.assertEqual((state["decisions_total"], state["opened_total"], state["recent"]), (0, 0, []))
        self.assertEqual(state["no_trade"]["total"], 0)

    def test_no_trade_counts_cover_every_reason_and_recent_is_newest_first(self) -> None:
        self.ready()
        self.offer(candidate("e1"), candidate("e2", quote="USD"), candidate("e3", direction="SHORT"))
        state = self.reader().read(LATER)
        counts = {row["reason"]: row["count"] for row in state["no_trade"]["counts"]}
        self.assertEqual(set(counts), {reason.value for reason in NoTradeReason})
        self.assertEqual((counts["position_already_open"], counts["unsupported_direction"]), (1, 1))
        self.assertEqual(state["no_trade"]["total"], 2)
        self.assertEqual((state["decisions_total"], state["opened_total"]), (3, 1))
        self.assertEqual([(d["event_id"], d["outcome"], d["reason"], d["quantity"]) for d in state["recent"]],
                         [("e3", "NO_TRADE", "unsupported_direction", None),
                          ("e2", "NO_TRADE", "position_already_open", None),
                          ("e1", "OPENED", None, "0.19")])

    def test_locks_kill_switch_and_the_review_that_cleared_one(self) -> None:
        self.ready()
        self.offer(candidate("e1"))
        # A gap down to bid 60 closes at the stop's first touch: net -7.68, equity 232.32 trips both locks.
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=5), 60.0, 60.1)
        daily, drawdown = ps.close_positions(self.conn, now=T0 + timedelta(minutes=6)).locks.tripped
        ps.review_lock(self.conn, lock_id=drawdown.lock_id, reviewer="operator", cause="gap on BTC",
                       now=T0 + timedelta(minutes=7))
        ps.engage_kill_switch(self.conn, reason="stop after the gap", actor="operator", now=T0 + timedelta(minutes=8))
        state = self.reader().read(T0 + timedelta(minutes=9))
        self.assertEqual(state["kill_switch"], {"engaged": True, "reason": "stop after the gap", "actor": "operator",
                                                "recorded_at": "2026-09-29T10:08:00.000000+00:00"})
        self.assertEqual([lock["kind"] for lock in state["locks"]["active"]], ["daily_loss"])
        self.assertEqual(state["locks"]["total"], 2)
        recent = state["locks"]["recent"]
        self.assertEqual([(lock["kind"], lock["active"]) for lock in recent],
                         [("drawdown", False), ("daily_loss", True)])
        cleared = recent[0]
        self.assertEqual((cleared["equity"], cleared["reference"], cleared["limit_pct"]), ("232.32", "240.00", "3"))
        self.assertEqual(
            {k: cleared["review"][k] for k in ("reviewer", "cause", "rebase_equity", "reviewed_at")},
            {"reviewer": "operator", "cause": "gap on BTC", "rebase_equity": "232.32",
             "reviewed_at": "2026-09-29T10:07:00.000000+00:00"},
        )
        self.assertIsNone(recent[1]["review"])
        account = state["account"]
        self.assertEqual((account["equity"], account["realized"], account["high_water"], account["drawdown_pct"]),
                         ("232.32", "-7.68", "232.32", "0.00"))
        self.assertIsNone(state["open_position"])

    def test_switched_off_still_shows_the_recorded_data(self) -> None:
        self.ready()
        self.offer(candidate("e1"))
        state = self.reader(enabled=False).read(LATER)
        self.assertTrue(state["available"])
        self.assertFalse(state["enabled"])
        self.assertEqual(state["reason"], pilot_reader.REASON_DISABLED)
        self.assertEqual(state["reason_text"], paper_texts.PILOT_REASONS["disabled"])
        self.assertEqual(state["open_position"]["pair"], "BTC/EUR")

    def test_hostile_strings_are_passed_as_data(self) -> None:
        self.ready()
        hostile = "<img src=x onerror=alert(1)>"
        ps.engage_kill_switch(self.conn, reason=hostile, actor=hostile, now=T0)
        state: dict[str, Any] = self.reader().read(LATER)
        self.assertEqual((state["kill_switch"]["reason"], state["kill_switch"]["actor"]), (hostile, hostile))


class TestValuation(ReaderCase):
    """The reporting valuation of the pretend EUR PAPER account, in Decimal.

    The entry is 0.19 BTC at the 100 ask with a cost basis of 19.05 (19.00 + the 0.0494 entry
    fee rounded to 0.05). Every read must leave the file bytes, mtime and row counts as they were."""

    MONEY_KEYS = ("free_cash", "open_cost_basis", "liquidation_value", "realized_balance", "realized_pnl",
                  "open_net_pnl", "total_equity")

    def read(self, at: datetime) -> dict[str, Any]:
        before = self.snapshot()
        state = self.reader().read(at)
        self.assertEqual(self.snapshot(), before)
        return state

    def money_of(self, valuation: dict[str, Any]) -> dict[str, Any]:
        return {key: valuation[key] for key in self.MONEY_KEYS}

    def test_account_is_labelled_paper_not_shadow_live(self) -> None:
        self.ready()
        state = self.read(LATER)
        for payload in (state, state["valuation"]):
            self.assertEqual(payload["account_mode"], "PAPER")
            self.assertEqual(payload["account_text"], paper_texts.PILOT_ACCOUNT_TEXT)
        self.assertIn("not SHADOW_LIVE", paper_texts.PILOT_ACCOUNT_TEXT)
        self.assertIn("pretend EUR", paper_texts.PILOT_ACCOUNT_TEXT)
        self.assertIsNone(pilot_reader.PilotReader(os.path.join(self._tmp.name, "absent"), enabled=True)
                          .read(T0)["valuation"])

    def test_pilot_without_positions(self) -> None:
        self.ready()
        state = self.read(LATER)
        valuation = state["valuation"]
        self.assertEqual(
            self.money_of(valuation),
            {"free_cash": "240.00", "open_cost_basis": "0.00", "liquidation_value": "0.00",
             "realized_balance": "240.00", "realized_pnl": "0.00", "open_net_pnl": "0.00", "total_equity": "240.00"},
        )
        self.assertEqual((valuation["mark_status"], valuation["stale"], valuation["positions"], valuation["currency"]),
                         ("no_open_positions", False, [], "EUR"))
        self.assertEqual((state["account"]["equity"], state["account"]["cash"]), ("240.00", "240.00"))

    def test_fresh_mark_reconciles_with_the_exit_fee_rounded_up(self) -> None:
        self.ready()
        self.offer(candidate("e1"))
        self.add_spot("BTC/EUR", LATER, 99.9, 100.0)
        state = self.read(LATER + timedelta(minutes=1))
        valuation = state["valuation"]
        # Proceeds 0.19 x 99.9 = 18.981; exit fee 0.0493506 rounded UP to 0.05; liquidation
        # 18.931; open net 18.931 - 19.05 = -0.119; cash 240 - 19.05 = 220.95; equity 239.881.
        self.assertEqual(
            self.money_of(valuation),
            {"free_cash": "220.95", "open_cost_basis": "19.05", "liquidation_value": "18.93",
             "realized_balance": "240.00", "realized_pnl": "0.00", "open_net_pnl": "-0.12", "total_equity": "239.88"},
        )
        self.assertEqual(valuation["exact"], {"free_cash": "220.95", "open_cost_basis": "19.05",
                                              "liquidation_value": "18.931", "open_net_pnl": "-0.119",
                                              "total_equity": "239.881"})
        [row] = valuation["positions"]
        self.assertEqual((row["exit_fee"], row["cost_basis"], row["mark"]["status"], row["mark"]["bid"]),
                         ("0.05", "19.05", "marked", "99.9"))
        self.assertEqual((row["fee_bps"], row["fee_source"], row["account_tier_verified"]), ("26", "ASSUMED", False))
        exact_values = {key: D(value) for key, value in valuation["exact"].items()}
        self.assertEqual(exact_values["open_net_pnl"],
                         unrealized_mark(D("0.19"), D("99.9"), D("19.05"), FEE))
        self.assertEqual(exact_values["total_equity"], exact_values["free_cash"] + exact_values["liquidation_value"])
        self.assertEqual(exact_values["total_equity"], D("240") + exact_values["open_net_pnl"])
        self.assertEqual(exact_values["liquidation_value"],
                         exact_values["open_cost_basis"] + exact_values["open_net_pnl"])
        # The runtime account equity is the same here, where the mark is fresh.
        self.assertEqual(state["account"]["equity"], "239.88")

    def test_runtime_account_is_unchanged_while_the_report_has_no_mark(self) -> None:
        """No spot row since the entry: the runtime equity keeps its entry-quote mark (locks and
        sizing are untouched), the report shows the dependent totals as unavailable, not zero."""
        self.ready()
        self.offer(candidate("e1"))
        runtime = ps.account_state(self.conn, now=LATER)
        state = self.read(LATER)
        self.assertEqual((state["account"]["equity"], state["account"]["cash"]),
                         (pilot_reader.money(runtime.equity), pilot_reader.money(runtime.cash)))
        valuation = state["valuation"]
        self.assertEqual(
            self.money_of(valuation),
            {"free_cash": "220.95", "open_cost_basis": "19.05", "liquidation_value": None,
             "realized_balance": "240.00", "realized_pnl": "0.00", "open_net_pnl": None, "total_equity": None},
        )
        self.assertEqual((valuation["mark_status"], valuation["stale"]), ("unavailable", True))
        self.assertEqual(valuation["unmarked"], [{"position_id": 1, "pair": "BTC/EUR", "status": "missing_quote"}])
        [row] = valuation["positions"]
        self.assertEqual((row["liquidation_value"], row["open_net_pnl"], row["exit_fee"]), (None, None, None))
        self.assertEqual(row["mark"]["reason_text"], paper_texts.MARK_REASONS["missing_quote"])
        self.assertEqual(ps.account_state(self.conn, now=LATER), runtime)

    def test_stale_invalid_and_wrong_pair_quotes(self) -> None:
        for label, pair, bid, ask, at, expected in (
            ("stale", "BTC/EUR", 99.9, 100.0, T0, "stale_quote"),
            ("crossed", "BTC/EUR", 101.0, 100.0, T0 + timedelta(minutes=10), "invalid_quote"),
            ("infinite", "BTC/EUR", float("inf"), 100.0, T0 + timedelta(minutes=10), "invalid_quote"),
            ("other pair", "ETH/EUR", 99.9, 100.0, T0 + timedelta(minutes=10), "missing_quote"),
        ):
            with self.subTest(quote=label):
                self.conn.close()  # a fresh database per case, removed after the test
                tmp = tempfile.TemporaryDirectory(prefix="pilot-reader-")
                self.addCleanup(tmp.cleanup)
                self.addCleanup(lambda: self.conn.close())  # runs first: the file is closed before removal
                self.path = os.path.join(tmp.name, "radar_state.sqlite")
                SnapshotStore(self.path).close()
                self.conn = sqlite3.connect(self.path)
                self.ready()
                self.offer(candidate("e1"))
                self.add_spot(pair, at, bid, ask)
                valuation = self.read(T0 + timedelta(minutes=11))["valuation"]
                self.assertEqual(valuation["unmarked"][0]["status"], expected)
                self.assertEqual((valuation["liquidation_value"], valuation["open_net_pnl"], valuation["total_equity"]),
                                 (None, None, None))
                self.assertEqual((valuation["free_cash"], valuation["open_cost_basis"]), ("220.95", "19.05"))

    def test_losing_close_is_realized(self) -> None:
        self.ready()
        self.offer(candidate("e1"))
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=5), 60.0, 60.1)
        ps.close_positions(self.conn, now=T0 + timedelta(minutes=6))
        [close] = ps.read_closes(self.conn)
        state = self.read(T0 + timedelta(minutes=9))
        valuation = state["valuation"]
        self.assertEqual(close.net, D("-7.68"))
        self.assertEqual(
            self.money_of(valuation),
            {"free_cash": "232.32", "open_cost_basis": "0.00", "liquidation_value": "0.00",
             "realized_balance": "232.32", "realized_pnl": "-7.68", "open_net_pnl": "0.00", "total_equity": "232.32"},
        )
        self.assertEqual(state["account"]["equity"], valuation["total_equity"])

    def test_open_position_carries_its_fee_provenance(self) -> None:
        self.ready()
        self.offer(candidate("e1"))
        position = self.read(LATER)["open_position"]
        self.assertEqual({key: position[key] for key in ("fee_bps", "fee_source", "account_tier_verified", "fee_text")},
                         {"fee_bps": "26", "fee_source": "ASSUMED", "account_tier_verified": False,
                          "fee_text": paper_texts.fee_provenance_text(FEE)})


class TestFormatting(unittest.TestCase):
    def test_money_and_quantities(self) -> None:
        from decimal import Decimal as D

        self.assertEqual(pilot_reader.money(D("0.005")), "0.00")  # half to even, never "-0.00"
        self.assertEqual(pilot_reader.money(D("-0.004")), "0.00")
        self.assertEqual(pilot_reader.money(D("0.015")), "0.02")
        self.assertIsNone(pilot_reader.money(None))
        self.assertEqual(pilot_reader.candidate_text(D("0.199765209999")), "0.19976520")  # down, never up
        self.assertEqual(pilot_reader.exact(D("97.510")), "97.510")


class TestBridge(unittest.TestCase):
    def test_from_config_follows_the_pilot_switch(self) -> None:
        from radar_v08 import config

        with mock.patch.object(config, "RADAR_PILOT_ENABLED", False):
            reader = pilot_reader.from_config()
        self.assertEqual((reader.path, reader.enabled), (config.SQLITE_PATH, False))

    def test_api_method_returns_the_reader_payload(self) -> None:
        from ui.bridge import Api

        api = Api.__new__(Api)
        api._pilot = mock.Mock(read=mock.Mock(return_value={"available": False}))
        self.assertEqual(api.get_pilot_state(), {"available": False})


if __name__ == "__main__":
    unittest.main()
