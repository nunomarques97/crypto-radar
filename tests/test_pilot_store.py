"""Pilot shadow store (radar_v08/adapters/pilot_store.py) on temporary SQLite files only.

Additive schema outside the ledger, append-only rows and CHECK constraints, one decision
per evaluated event, every NO_TRADE reason, full open-then-close cycles for each exit
reason, persisted locks cleared only by a review, the kill switch, idempotency and one
BEGIN IMMEDIATE transaction per write that rolls back fully on error.

Unless said otherwise: envelope 240.00 EUR at the EX-1 values, fee 26 bps per leg, quote
99.9/100, ATR 1, pair rules lot 2 decimals / ordermin 0.01 / costmin 0.50 / tick 0.01.
Then (tests/test_domain_risk.py) stop 98, target 104, stressed exit 97.51, loss per unit
3.003526 and the per-entry cap 0.60 binds: 0.60 / 3.003526 = 0.1997... -> 0.19 lots,
notional 19.00, planned fees 0.05 + 0.05 (rounded up), planned loss 0.19 x 2.49 + 0.10 =
0.5731. The cost basis is 19.00 + 0.05 = 19.05.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any
from unittest import mock

from radar_v08.adapters import pilot_store as ps
from radar_v08.adapters.paper_store import ObservedQuote
from radar_v08.domain import risk
from radar_v08.domain.paper import Direction, ExitReason, Outcome
from radar_v08.domain.risk import BindingConstraint, Envelope, LockKind, NoTradeReason
from radar_v08.store import SnapshotStore

D = Decimal
T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
DUE = T0 + timedelta(hours=24)
FEE = D("26")
ENVELOPE = Envelope(D("240.00"), "EUR")
RULES: dict[str, object] = {"lot_decimals": 2, "ordermin": "0.01", "costmin": "0.50", "tick_size": "0.01"}


def rules(**overrides: object) -> dict[str, object]:
    return {**RULES, **overrides}


def candidate(
    event_id: str,
    *,
    asset: str = "BTC",
    quote: str = "EUR",
    direction: object = "LONG",
    bid: object = D("99.9"),
    ask: object = D("100"),
    status: object = "online",
    atr: object = D("1"),
    atr_pair: object = "same",
    entry: object = "default",
    ts: datetime = T0,
) -> ps.PilotCandidate:
    pair = f"{asset}/{quote}"
    return ps.PilotCandidate(
        event_id=event_id,
        run_id="run-1",
        asset=asset,
        pair=pair,
        quote=quote,
        direction=direction,
        bid=bid,
        ask=ask,
        snapshot_ts=ts.isoformat(),
        status=status,
        atr=atr,
        atr_pair=pair if atr_pair == "same" else atr_pair,
        pair_entry=rules() if entry == "default" else entry,
    )


def ticker(pair: str, at: datetime, bid: object, ask: object) -> ObservedQuote:
    return ObservedQuote(pair=pair, bid=bid, ask=ask, observed_at=at, source="ticker")


def floor10(value: Decimal | None) -> Decimal:
    assert value is not None
    return value.quantize(D("1E-10"), rounding=ROUND_FLOOR)


def all_rows(conn: sqlite3.Connection, tables: tuple[str, ...] = ps.TABLES) -> dict[str, list[tuple[object, ...]]]:
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}


class PilotDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pilot-store-")
        # Registered first, so it runs last: after every connection a test opened is closed.
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "radar_state.sqlite")
        SnapshotStore(self.path).close()  # every existing radar table, as in production
        self.conn = self.connect()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def ready(self, envelope: Envelope = ENVELOPE) -> ps.Account:
        ps.ensure_schema(self.conn)
        return ps.ensure_account(self.conn, envelope=envelope, now=T0)

    def add_spot(self, pair: str, ts: datetime, bid: object, ask: object, status: object = "online") -> int:
        cursor = self.conn.execute(
            "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pair.split("/")[0], pair, pair.split("/")[1], ts.isoformat(), bid, ask, status),
        )
        self.conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def open(self, *candidates: ps.PilotCandidate, envelope: Envelope = ENVELOPE, now: datetime = T0,
             conn: sqlite3.Connection | None = None) -> ps.OpenReport:
        return ps.open_candidates(conn or self.conn, list(candidates), envelope=envelope, fee_bps=FEE, now=now)

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def only_decision(self, report: ps.OpenReport) -> ps.StoredDecision:
        self.assertEqual(len(report.decisions), 1)
        return report.decisions[0]


class TestSchema(PilotDbCase):
    def test_ensure_schema_is_additive_and_outside_the_ledger(self):
        before = {tuple(row) for row in self.conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")}
        ledger = self.conn.execute("SELECT * FROM schema_version_ledger ORDER BY version").fetchall()
        self.assertFalse(ps.schema_present(self.conn))
        self.assertTrue(ps.ensure_schema(self.conn))
        self.assertTrue(ps.schema_present(self.conn))
        after = {tuple(row) for row in self.conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")}
        self.assertEqual({row for row in after if row[2] not in ps.TABLES}, before)
        self.assertEqual(self.conn.execute("SELECT * FROM schema_version_ledger ORDER BY version").fetchall(), ledger)
        created = {(row[0], row[1]) for row in after if row[2] in ps.TABLES and not row[1].startswith("sqlite_autoindex_")}
        self.assertEqual(created, set(ps.SCHEMA_OBJECTS))
        for statement in ps.SCHEMA_STATEMENTS:
            self.assertRegex(statement, r"^CREATE (TABLE|TRIGGER|UNIQUE INDEX) IF NOT EXISTS pilot_")
            self.assertNotIn("DROP", statement.upper())
            self.assertNotIn("ALTER", statement.upper())
            self.assertNotIn("paper_", statement)

    def test_ensure_schema_writes_nothing_when_everything_exists(self):
        self.assertTrue(ps.ensure_schema(self.conn))
        statements: list[str] = []
        changes = self.conn.total_changes
        self.conn.set_trace_callback(statements.append)
        self.assertFalse(ps.ensure_schema(self.conn))
        self.conn.set_trace_callback(None)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertTrue(statements)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements), statements)

    def test_ensure_schema_refuses_an_open_transaction(self):
        self.conn.execute("BEGIN")
        with self.assertRaises(ps.PilotStoreError) as caught:
            ps.ensure_schema(self.conn)
        self.assertIs(caught.exception.code, ps.PilotStoreFailure.OPEN_TRANSACTION)
        self.conn.rollback()

    def test_account_is_recorded_once_with_the_assigned_high_water(self):
        account = self.ready()
        self.assertEqual(account.envelope, ENVELOPE)
        self.assertEqual(account.envelope_sha256, ps.envelope_sha256(ENVELOPE))
        again = ps.ensure_account(self.conn, envelope=Envelope(D("500.00"), "EUR"), now=T0 + timedelta(days=1))
        self.assertEqual(again, account)  # history is never rewritten
        self.assertEqual(self.count(ps.ACCOUNT_TABLE), 1)
        row = self.conn.execute("SELECT equity_cents, currency, envelope_policy_id FROM pilot_account").fetchone()
        self.assertEqual(tuple(row), (24000, "EUR", risk.ENVELOPE_POLICY_ID))
        marks = ps.read_marks(self.conn)
        self.assertEqual([(m.kind, m.source, m.equity, m.utc_day) for m in marks],
                         [(ps.MarkKind.HIGH_WATER, ps.MarkSource.ASSIGNED, D("240.00"), None)])
        self.assertEqual(ps.high_water(self.conn), D("240.00"))

    def test_envelope_round_trips_through_its_canonical_text(self):
        envelope = Envelope(D("240.00"), "EUR", per_entry_loss_pct=D("0.20"), per_entry_abs_cap=D("0.50"))
        self.assertEqual(ps.parse_envelope(envelope.canonical_text()), envelope)
        with self.assertRaises(ps.PilotStoreError) as caught:
            ps.parse_envelope(envelope.canonical_text().replace('"0.20"', '"0.30"'))  # looser than EX-1
        self.assertIs(caught.exception.code, ps.PilotStoreFailure.MALFORMED_ROW)

    def test_every_table_is_append_only(self):
        self.ready()
        self.open(candidate("e1"))
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=5), 87.0, 87.1)
        ps.close_positions(self.conn, now=T0 + timedelta(minutes=6))
        lock = ps.read_locks(self.conn)[0]
        ps.review_lock(self.conn, lock_id=lock.lock_id, reviewer="operator", cause="gap test", now=T0 + timedelta(minutes=7))
        self.open(candidate("e2", ts=T0 + timedelta(minutes=8)), now=T0 + timedelta(minutes=8))
        ps.close_positions(self.conn, now=T0 + timedelta(minutes=9),
                           extra_quotes=[ticker("BTC/EUR", T0 + timedelta(minutes=9), 104.2, 104.3)])
        ps.engage_kill_switch(self.conn, reason="test", actor="operator", now=T0 + timedelta(minutes=10))
        for table in ps.TABLES:
            self.assertGreater(self.count(table), 0, table)
            with self.subTest(table=table):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    self.conn.execute(f"UPDATE {table} SET rowid = rowid")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "never deleted"):
                    self.conn.execute(f"DELETE FROM {table}")
                self.conn.rollback()

    def test_check_constraints_and_parent_triggers_are_the_backstop(self):
        self.ready()
        report = self.open(candidate("e1"), candidate("e2", direction="SHORT"))
        opened, refused = report.decisions
        bad = [
            # An opened decision must carry a quantity; a refusal must carry a reason.
            f"INSERT INTO pilot_decisions (event_id, run_id, asset, pair, quote, direction, snapshot_ts, passes, "
            f"fee_bps, stress_bps, envelope_sha256, envelope_policy_id, sizing_policy_id, stress_policy_id, "
            f"exit_policy_id, outcome, reason, detail, decided_at) VALUES ('x', 'r', 'A', 'A/EUR', 'EUR', 'LONG', 't', "
            f"0, '26', '50', '{'0' * 64}', 'e', 's', 's', 'x', 'OPENED', NULL, '', 't')",
            f"INSERT INTO pilot_decisions (event_id, run_id, asset, pair, quote, direction, snapshot_ts, passes, "
            f"fee_bps, stress_bps, envelope_sha256, envelope_policy_id, sizing_policy_id, stress_policy_id, "
            f"exit_policy_id, outcome, reason, detail, decided_at) VALUES ('y', 'r', 'A', 'A/EUR', 'EUR', 'LONG', 't', "
            f"0, '26', '50', '{'0' * 64}', 'e', 's', 's', 'x', 'NO_TRADE', 'made_up', '', 't')",
            # A position needs an OPENED decision of the same event.
            "INSERT INTO pilot_positions (decision_id, event_id, run_id, asset, pair, quote, direction, quantity, "
            "entry_bid, entry_ask, entry_ts, due_at, fee_bps, exit_policy, atr, stop_price, target_price, "
            "stress_exit_price, planned_loss, notional, cost_basis, opened_at) VALUES "
            f"({refused.decision_id}, 'e2', 'r', 'A', 'A/EUR', 'EUR', 'LONG', '1', '1', '1', 'a', 'b', '26', 'p', "
            "'1', '1', '1', '1', '1', '1', '1', 't')",
            # A close must add up: net = gross - fees.
            "INSERT INTO pilot_closes (position_id, exit_reason, exit_source, exit_observation_id, exit_bid, exit_ask, "
            "exit_ts, record_lag_seconds, quantity, gross_cents, entry_fee_cents, exit_fee_cents, fees_cents, "
            "net_cents, outcome, stop_price, stop_slippage, stop_slippage_bps, closed_at) VALUES "
            "(1, 'stop', 'ticker', 1, '1', '1', 't', 0, '1', -40, 5, 5, 10, -40, 'LOSS', '1', '0', '0', 't')",
            "INSERT INTO pilot_closes (position_id, exit_reason, exit_source, exit_observation_id, exit_bid, exit_ask, "
            "exit_ts, record_lag_seconds, quantity, gross_cents, entry_fee_cents, exit_fee_cents, fees_cents, "
            "net_cents, outcome, stop_price, stop_slippage, stop_slippage_bps, closed_at) VALUES "
            "(99, 'stop', 'ticker', 1, '1', '1', 't', 0, '1', -40, 5, 5, 10, -50, 'LOSS', '1', '0', '0', 't')",
            # A review mark needs its lock; a review needs a lock; an assigned mark is a high-water mark.
            "INSERT INTO pilot_equity_marks (kind, utc_day, equity, source, lock_id, recorded_at) "
            "VALUES ('high_water', NULL, '1', 'review', NULL, 't')",
            "INSERT INTO pilot_equity_marks (kind, utc_day, equity, source, lock_id, recorded_at) "
            "VALUES ('day_start', '2026-09-29', '1', 'assigned', NULL, 't')",
            "INSERT INTO pilot_equity_marks (kind, utc_day, equity, source, lock_id, recorded_at) "
            "VALUES ('day_start', '2026-09-29', '1', 'evaluation', NULL, 't')",  # one evaluation day start per day
            "INSERT INTO pilot_lock_reviews (lock_id, reviewer, cause, rebase_equity, reviewed_at) "
            "VALUES (42, 'n', 'c', '1', 't')",
            "INSERT INTO pilot_kill_switch (state, reason, actor, recorded_at) VALUES ('MAYBE', 'r', 'a', 't')",
            "INSERT INTO pilot_account (account_id, envelope_json, envelope_sha256, envelope_policy_id, equity_cents, "
            f"currency, recorded_at) VALUES (2, '{{}}', '{'0' * 64}', 'p', 100, 'EUR', 't')",
        ]
        self.assertTrue(opened.opened)
        for statement in bad:
            with self.subTest(statement=statement[:60]):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(statement)
                self.conn.rollback()


class TestOpening(PilotDbCase):
    def test_admitted_candidate_is_sized_and_recorded_exactly(self):
        self.ready()
        report = self.open(candidate("e1"))
        decision = self.only_decision(report)
        self.assertTrue(decision.opened)
        self.assertEqual((decision.outcome, decision.reason, decision.detail), ("OPENED", None, ""))
        v = decision.value
        self.assertEqual((v("bid"), v("ask"), v("atr")), (D("99.9"), D("100"), D("1")))
        self.assertEqual((decision.lot_decimals, v("ordermin"), v("costmin"), v("tick"), decision.tick_source),
                         (2, D("0.01"), D("0.50"), D("0.01"), "tick_size"))
        self.assertEqual((v("equity"), v("cash")), (D("240.00"), D("240.00")))
        self.assertEqual((v("stop_price"), v("target_price"), v("stress_exit_price"), v("loss_per_unit")),
                         (D("98"), D("104"), D("97.51"), D("3.003526")))
        self.assertEqual((v("per_entry_loss_cap"), v("aggregate_loss_remaining"), v("notional_remaining"), v("cash_room")),
                         (D("0.60"), D("1.20"), D("24.00"), D("216.00")))
        self.assertEqual(
            [floor10(v(name)) for name in ("qty_per_entry_loss", "qty_aggregate_loss", "qty_notional", "qty_cash")],
            [D("0.1997652092"), D("0.3995304185"), D("0.24"), D("2.1543985637")],
        )
        self.assertIs(decision.binding, BindingConstraint.PER_ENTRY_LOSS)
        self.assertEqual((v("lot_quantity"), v("quantity"), decision.passes), (D("0.19"), D("0.19"), 0))
        self.assertEqual((v("notional"), decision.entry_fee, decision.exit_fee, v("planned_loss")),
                         (D("19.00"), D("0.05"), D("0.05"), D("0.5731")))
        self.assertEqual(
            (decision.envelope_sha256, decision.envelope_policy_id, decision.sizing_policy_id,
             decision.stress_policy_id, decision.exit_policy_id),
            (ps.envelope_sha256(ENVELOPE), "ex1_envelope_v1", "pilot_sizing_v1",
             "pilot_stress_exit_slippage_50bps_v1", "ex1_initial_paper_v1"),
        )
        (position,) = ps.read_positions(self.conn)
        self.assertEqual(
            (position.decision_id, position.event_id, position.pair, position.quantity, position.entry_bid,
             position.entry_ask, position.entry_ts, position.due_at, position.fee_bps, position.exit_policy),
            (decision.decision_id, "e1", "BTC/EUR", D("0.19"), D("99.9"), D("100"), T0, DUE, FEE, "ex1_initial_paper_v1"),
        )
        self.assertEqual((position.atr, position.stop, position.target, position.stress_exit_price),
                         (D("1"), D("98"), D("104"), D("97.51")))
        self.assertEqual((position.planned_loss, position.notional, position.cost_basis),
                         (D("0.5731"), D("19.00"), D("19.05")))
        stored = self.conn.execute("SELECT quantity, stop_price, cost_basis FROM pilot_positions").fetchone()
        self.assertEqual(tuple(stored), ("0.19", "98", "19.05"))  # exact decimal text, no float

    def test_tick_rounds_levels_down_and_lots_round_down(self):
        # Tick 0.5, ATR 1.1: stop 97.8 -> 97.5, target 104.4 -> 104.0; lot 1 decimal: 0.19... -> 0.1.
        self.ready()
        decision = self.only_decision(self.open(candidate("e1", atr=D("1.1"), entry=rules(tick_size="0.5", lot_decimals=1))))
        self.assertTrue(decision.opened)
        self.assertEqual((decision.value("stop_price"), decision.value("target_price")), (D("97.5"), D("104.0")))
        self.assertEqual(decision.value("quantity"), D("0.1"))

    def test_sizing_takes_three_downward_passes_at_most(self):
        # Per-entry cap 0.06 with 3 lot decimals: 0.019 -> 0.016 after three passes (see test_domain_risk).
        envelope = Envelope(D("240.00"), "EUR", per_entry_abs_cap=D("0.06"))
        self.ready(envelope)
        decision = self.only_decision(
            self.open(candidate("e1", entry=rules(lot_decimals=3, ordermin="0", costmin="0")), envelope=envelope)
        )
        self.assertTrue(decision.opened)
        self.assertEqual((decision.value("lot_quantity"), decision.value("quantity"), decision.passes),
                         (D("0.019"), D("0.016"), 3))

    def test_one_decision_per_event_and_a_repeat_writes_nothing(self):
        self.ready()
        first = self.open(candidate("e1"), candidate("e2"), candidate("e3", direction="SHORT"))
        self.assertEqual([d.reason for d in first.decisions],
                         [None, NoTradeReason.POSITION_ALREADY_OPEN, NoTradeReason.UNSUPPORTED_DIRECTION])
        before = all_rows(self.conn)
        changes = self.conn.total_changes
        again = self.open(candidate("e1"), candidate("e2"), candidate("e3", direction="SHORT"))
        self.assertEqual(again.decisions, ())
        self.assertEqual(again.already_recorded, ("e1", "e2", "e3"))
        self.assertEqual(all_rows(self.conn), before)
        self.assertEqual(self.conn.total_changes, changes)
        other = self.connect()  # a second writer sees the same recorded events
        self.assertEqual(self.open(candidate("e2"), conn=other).already_recorded, ("e2",))
        self.assertEqual(all_rows(self.conn), before)

    def test_invalid_arguments_write_nothing(self):
        self.ready()
        before = all_rows(self.conn)
        cases = [
            ({"candidates": [candidate("")]}, ps.PilotStoreFailure.INVALID_ROW),
            ({"candidates": [candidate("e1"), "not a candidate"]}, ps.PilotStoreFailure.INVALID_ROW),
            ({"candidates": [ps.PilotCandidate("e1", "r", "BTC", "BTC/EUR", "EUR", "LONG", 1, 2, "noon", None)]},
             ps.PilotStoreFailure.INVALID_ROW),
            ({"now": datetime(2026, 9, 29, 10, 0)}, ps.PilotStoreFailure.INVALID_ARGUMENT),
            ({"fee_bps": 26.0}, ps.PilotStoreFailure.INVALID_ARGUMENT),
            ({"envelope": "240 EUR"}, ps.PilotStoreFailure.INVALID_ARGUMENT),
        ]
        for overrides, code in cases:
            arguments: dict[str, object] = {"candidates": [candidate("e1")], "envelope": ENVELOPE, "fee_bps": FEE, "now": T0}
            arguments.update(overrides)
            with self.subTest(overrides=list(overrides)):
                with self.assertRaises(ps.PilotStoreError) as caught:
                    ps.open_candidates(self.conn, arguments.pop("candidates"), **arguments)  # type: ignore[arg-type]
                self.assertIs(caught.exception.code, code)
        self.assertEqual(all_rows(self.conn), before)

    def test_no_account_refuses_and_writes_nothing(self):
        ps.ensure_schema(self.conn)
        with self.assertRaises(ps.PilotStoreError) as caught:
            self.open(candidate("e1"))
        self.assertIs(caught.exception.code, ps.PilotStoreFailure.NO_ACCOUNT)
        self.assertEqual({table: rows for table, rows in all_rows(self.conn).items() if rows}, {})


class TestNoTradeReasons(PilotDbCase):
    """Each NO_TRADE reason written as a decision row by ``open_candidates``.

    Equity, cash, open planned loss and open notional come from the recorded account. With
    one position at most (F5) the sizing only runs when nothing is open, so equity and cash
    are the assigned equity plus realized nets and no loss, notional or cash budget can be
    exhausted, nor equity missing. Those four refusals are driven through the store's
    ``_sizing_inputs`` seam, which feeds the real sizing and the real decision write.
    """

    def refused(self, report: ps.OpenReport, reason: NoTradeReason, detail: str | None = None) -> ps.StoredDecision:
        decision = self.only_decision(report)
        self.assertEqual((decision.outcome, decision.reason), ("NO_TRADE", reason))
        if detail is not None:
            self.assertEqual(decision.detail, detail)
        held = self.conn.execute("SELECT COUNT(*) FROM pilot_positions WHERE decision_id = ?", (decision.decision_id,))
        self.assertEqual(held.fetchone()[0], 0)
        return decision

    def gap_loss(self, bid: float) -> None:
        self.open(candidate("gap"))
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=5), bid, bid + 0.1)
        ps.close_positions(self.conn, now=T0 + timedelta(minutes=6))

    def sizing_inputs(self, equity: object, cash: object, loss: str = "0", notional: str = "0") -> Any:
        return mock.patch.object(ps, "_sizing_inputs", return_value=(equity, cash, D(loss), D(notional)))

    def test_every_reason_is_reachable(self):
        scenarios = {
            NoTradeReason.UNSUPPORTED_DIRECTION: self.case_short,
            NoTradeReason.ENVELOPE_CHANGED: self.case_envelope_changed,
            NoTradeReason.KILL_SWITCH_ENGAGED: self.case_kill_switch,
            NoTradeReason.DAILY_LOSS_LOCK: self.case_daily_lock,
            NoTradeReason.DRAWDOWN_LOCK: self.case_drawdown_lock,
            NoTradeReason.POSITION_ALREADY_OPEN: self.case_position_open,
            NoTradeReason.QUOTE_CURRENCY_MISMATCH: lambda: self.refused(
                self.open(candidate("u", quote="USD")), NoTradeReason.QUOTE_CURRENCY_MISMATCH, "USD"),
            NoTradeReason.INVALID_QUOTE: lambda: self.refused(
                self.open(candidate("q", bid=D("101"))), NoTradeReason.INVALID_QUOTE, "crossed"),
            NoTradeReason.NO_VALID_ATR: self.case_no_valid_atr,
            NoTradeReason.MISSING_PAIR_RULES: lambda: self.refused(
                self.open(candidate("r", entry=rules(ordermin=None))), NoTradeReason.MISSING_PAIR_RULES, "ordermin:missing"),
            NoTradeReason.EQUITY_UNAVAILABLE: self.case_equity_unavailable,
            NoTradeReason.INVALID_LEVELS: lambda: self.refused(
                self.open(candidate("l", atr=D("60"))), NoTradeReason.INVALID_LEVELS, "level_not_positive"),
            NoTradeReason.STOP_INVALID_AFTER_TICK: lambda: self.refused(
                self.open(candidate("t", bid=D("1.4"), ask=D("1.5"), atr=D("0.3"), entry=rules(tick_size="1"))),
                NoTradeReason.STOP_INVALID_AFTER_TICK, "stop:0"),
            NoTradeReason.NO_LOSS_BUDGET: self.case_no_loss_budget,
            NoTradeReason.NO_NOTIONAL_ROOM: self.case_no_notional_room,
            NoTradeReason.INSUFFICIENT_CASH: self.case_insufficient_cash,
            NoTradeReason.BELOW_ORDER_MINIMUM: lambda: self.refused(
                self.open(candidate("o", entry=rules(ordermin="0.2"))), NoTradeReason.BELOW_ORDER_MINIMUM, "0.19<0.2"),
            NoTradeReason.BELOW_COST_MINIMUM: lambda: self.refused(
                self.open(candidate("c", entry=rules(costmin="19.01"))), NoTradeReason.BELOW_COST_MINIMUM, "19<19.01"),
            NoTradeReason.SIZING_INCONSISTENT: self.case_sizing_inconsistent,
        }
        self.assertEqual(set(scenarios), set(NoTradeReason))
        for reason, scenario in scenarios.items():
            with self.subTest(reason=reason.value):
                self.setUp()
                self.ready()
                scenario()
                counts = ps.no_trade_counts(self.conn)
                self.assertGreaterEqual(counts[reason.value], 1)
                self.assertEqual(sum(counts.values()), counts[reason.value])  # no other refusal on the way

    def case_short(self) -> None:
        self.refused(self.open(candidate("s", direction="SHORT")), NoTradeReason.UNSUPPORTED_DIRECTION, "SHORT")

    def case_envelope_changed(self) -> None:
        stricter = Envelope(D("240.00"), "EUR", per_entry_loss_pct=D("0.20"))
        decision = self.refused(self.open(candidate("x"), envelope=stricter), NoTradeReason.ENVELOPE_CHANGED)
        self.assertEqual(decision.envelope_sha256, ps.envelope_sha256(stricter))  # what was offered
        self.assertEqual(ps.read_account(self.conn).envelope, ENVELOPE)  # type: ignore[union-attr]

    def case_kill_switch(self) -> None:
        ps.engage_kill_switch(self.conn, reason="manual stop", actor="operator", now=T0)
        self.refused(self.open(candidate("k")), NoTradeReason.KILL_SWITCH_ENGAGED)

    def case_daily_lock(self) -> None:
        self.gap_loss(87.0)
        self.refused(self.open(candidate("d", ts=T0 + timedelta(minutes=7)), now=T0 + timedelta(minutes=7)),
                     NoTradeReason.DAILY_LOSS_LOCK)

    def case_drawdown_lock(self) -> None:
        self.gap_loss(60.0)
        daily = next(lock for lock in ps.active_locks(self.conn) if lock.kind is LockKind.DAILY_LOSS)
        ps.review_lock(self.conn, lock_id=daily.lock_id, reviewer="operator", cause="gap", now=T0 + timedelta(minutes=7))
        self.refused(self.open(candidate("w", ts=T0 + timedelta(minutes=8)), now=T0 + timedelta(minutes=8)),
                     NoTradeReason.DRAWDOWN_LOCK)

    def case_position_open(self) -> None:
        self.open(candidate("first"))
        self.refused(self.open(candidate("second", asset="ETH")), NoTradeReason.POSITION_ALREADY_OPEN, "1")

    def case_no_valid_atr(self) -> None:
        decision = self.refused(self.open(candidate("a", atr_pair="BTC/USD")), NoTradeReason.NO_VALID_ATR,
                                "pair_mismatch:'BTC/USD'!='BTC/EUR'")
        self.assertEqual((decision.value("ask"), decision.value("atr"), decision.atr_pair), (D("100"), D("1"), "BTC/USD"))
        self.refused(self.open(candidate("a2", atr=None)), NoTradeReason.NO_VALID_ATR, "missing")

    def case_equity_unavailable(self) -> None:
        with self.sizing_inputs(None, D("240")):
            decision = self.refused(self.open(candidate("eq")), NoTradeReason.EQUITY_UNAVAILABLE, "equity:missing")
        self.assertIsNone(decision.value("equity"))  # missing is recorded as missing, never 0

    def case_no_loss_budget(self) -> None:
        with self.sizing_inputs(D("240.00"), D("240.00"), loss="1.20"):
            self.refused(self.open(candidate("lb")), NoTradeReason.NO_LOSS_BUDGET, "remaining:0")

    def case_no_notional_room(self) -> None:
        with self.sizing_inputs(D("240.00"), D("240.00"), notional="24"):
            self.refused(self.open(candidate("nr")), NoTradeReason.NO_NOTIONAL_ROOM, "remaining:0")

    def case_insufficient_cash(self) -> None:
        with self.sizing_inputs(D("240.00"), D("24.00")):
            self.refused(self.open(candidate("ic")), NoTradeReason.INSUFFICIENT_CASH, "room:0")

    def case_sizing_inconsistent(self) -> None:
        # Recorded envelope with a 0.05 absolute cap: 0.016 -> 0.013 still too big after 3 passes.
        self.setUp()
        envelope = Envelope(D("240.00"), "EUR", per_entry_abs_cap=D("0.05"))
        self.ready(envelope)
        decision = self.refused(
            self.open(candidate("si", entry=rules(lot_decimals=3, ordermin="0", costmin="0")), envelope=envelope),
            NoTradeReason.SIZING_INCONSISTENT, "passes:3",
        )
        self.assertEqual((decision.value("lot_quantity"), decision.passes, decision.value("quantity")),
                         (D("0.016"), 3, None))

    def test_admission_order(self):
        # SHORT beats every later check; a changed envelope beats the kill switch; the kill switch beats a
        # lock; a lock beats an open position; an open position beats a USD quote.
        self.ready()
        ps.engage_kill_switch(self.conn, reason="stop", actor="operator", now=T0)
        stricter = Envelope(D("240.00"), "EUR", drawdown_pct=D("2"))
        self.refused(self.open(candidate("a", direction="SHORT", quote="USD"), envelope=stricter),
                     NoTradeReason.UNSUPPORTED_DIRECTION)
        self.assertEqual(self.open(candidate("b", quote="USD"), envelope=stricter).decisions[0].reason,
                         NoTradeReason.ENVELOPE_CHANGED)
        self.assertEqual(self.open(candidate("c", quote="USD")).decisions[0].reason, NoTradeReason.KILL_SWITCH_ENGAGED)

    def test_lock_beats_open_position_and_open_position_beats_currency(self):
        self.ready()
        self.open(candidate("first"))
        self.assertEqual(self.open(candidate("usd", quote="USD")).decisions[0].reason,
                         NoTradeReason.POSITION_ALREADY_OPEN)
        # Mark the open position far down: the daily lock trips at the entry evaluation and wins.
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=1), 80.0, 80.1)
        # A stop touch closes on settle, so evaluate at the entry only (no settle in between).
        report = self.open(candidate("next", ts=T0 + timedelta(minutes=2)), now=T0 + timedelta(minutes=2))
        self.assertEqual(report.decisions[0].reason, NoTradeReason.DAILY_LOSS_LOCK)
        self.assertEqual([lock.evaluated_on for lock in report.locks.tripped], [ps.LockTrigger.ENTRY])


    def test_direction_enum_is_read_like_its_text(self):
        self.ready()
        report = self.open(candidate("s", direction=Direction.SHORT), candidate("l", direction=Direction.LONG))
        short, long = report.decisions
        self.assertEqual((short.reason, short.detail, short.direction), (NoTradeReason.UNSUPPORTED_DIRECTION, "SHORT", "SHORT"))
        self.assertEqual((long.opened, long.direction), (True, "LONG"))


class TestClosing(PilotDbCase):
    def opened(self) -> ps.StoredPosition:
        self.ready()
        self.open(candidate("e1"))
        (position,) = ps.read_open_positions(self.conn)
        return position

    def test_stop_exit_on_a_spot_snapshot(self):
        position = self.opened()
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=1), 98.5, 98.6)  # no touch
        snapshot_id = self.add_spot("BTC/EUR", T0 + timedelta(minutes=2), 97.9, 98.0)
        report = ps.close_positions(self.conn, now=T0 + timedelta(minutes=2, seconds=30))
        (close,) = report.closed
        self.assertEqual((close.position_id, close.exit_reason, close.exit_source, close.exit_observation_id),
                         (position.position_id, ExitReason.STOP, "spot_snapshot", snapshot_id))
        self.assertEqual((close.exit_bid, close.exit_ask, close.exit_ts, close.record_lag_seconds),
                         (D("97.9"), D("98.0"), T0 + timedelta(minutes=2), 30.0))
        # 0.19 x (97.9 - 100) = -0.399 -> -0.40; fees 0.0494 -> 0.05 and 0.0483626 -> 0.05.
        self.assertEqual((close.quantity, close.gross, close.entry_fee, close.exit_fee, close.fees, close.net, close.outcome),
                         (D("0.19"), D("-0.40"), D("0.05"), D("0.05"), D("0.10"), D("-0.50"), Outcome.LOSS))
        # Exit 0.1 below the stop: 0.1 / 98 x 10000 bps.
        self.assertEqual((close.stop, close.stop_slippage), (D("98"), D("0.1")))
        self.assertEqual(close.stop_slippage_bps.quantize(D("1E-10"), rounding=ROUND_HALF_EVEN), D("10.2040816327"))
        self.assertEqual(ps.read_open_positions(self.conn), ())
        self.assertEqual(self.count(ps.EXIT_QUOTE_TABLE), 0)
        self.assertEqual(report.locks.equity, D("239.50"))

    def test_target_exit_on_an_extra_quote_is_stored(self):
        position = self.opened()
        at = T0 + timedelta(minutes=3)
        report = ps.close_positions(self.conn, now=at + timedelta(seconds=2),
                                    extra_quotes=[ticker("ETH/EUR", at, 1.0, 1.1), ticker("BTC/EUR", at, 104.2, 104.3)])
        (close,) = report.closed
        (quote_id, position_id, pair, bid, ask, observed, source, recorded) = self.conn.execute(
            "SELECT * FROM pilot_exit_quotes").fetchone()
        self.assertEqual((position_id, pair, bid, ask, source), (position.position_id, "BTC/EUR", "104.2", "104.3", "ticker"))
        self.assertEqual((close.exit_reason, close.exit_source, close.exit_observation_id, close.record_lag_seconds),
                         (ExitReason.TARGET, "ticker", quote_id, 2.0))
        # 0.19 x 4.2 = 0.798 -> 0.80; exit fee 0.0514748 -> 0.05; net 0.70.
        self.assertEqual((close.gross, close.fees, close.net, close.outcome), (D("0.80"), D("0.10"), D("0.70"), Outcome.WIN))
        self.assertEqual(close.stop_slippage, D("-6.2"))
        self.assertEqual(report.locks.equity, D("240.70"))
        self.assertEqual(ps.high_water(self.conn), D("240.70"))  # the high-water mark rose

    def test_time_exit_at_the_due_time(self):
        self.opened()
        self.add_spot("BTC/EUR", DUE - timedelta(seconds=1), 100.5, 100.6)  # before due: stays open
        self.assertEqual(ps.close_positions(self.conn, now=DUE - timedelta(seconds=1)).closed, ())
        self.add_spot("BTC/EUR", DUE, 101.0, 101.1)
        (close,) = ps.close_positions(self.conn, now=DUE + timedelta(minutes=1)).closed
        # 0.19 x 1 = 0.19; exit fee 0.049894 -> 0.05; net 0.09.
        self.assertEqual((close.exit_reason, close.exit_ts, close.gross, close.fees, close.net),
                         (ExitReason.TIME, DUE, D("0.19"), D("0.10"), D("0.09")))

    def test_first_touch_wins_and_a_recorded_row_wins_a_tie(self):
        self.opened()
        at = T0 + timedelta(minutes=4)
        snapshot_id = self.add_spot("BTC/EUR", at, 97.0, 97.1)  # stop, recorded
        report = ps.close_positions(self.conn, now=at + timedelta(minutes=1), extra_quotes=[
            ticker("BTC/EUR", at, 105.0, 105.1),  # target at the same instant: the recorded row wins
            ticker("BTC/EUR", at + timedelta(seconds=30), 105.0, 105.1),
        ])
        (close,) = report.closed
        self.assertEqual((close.exit_reason, close.exit_observation_id, close.exit_source),
                         (ExitReason.STOP, snapshot_id, "spot_snapshot"))
        self.assertEqual(self.count(ps.EXIT_QUOTE_TABLE), 0)

    def test_ignored_quotes_and_pending(self):
        self.opened()
        self.add_spot("BTC/EUR", T0, 50.0, 50.1)  # at entry, not after it
        self.add_spot("BTC/EUR", DUE, 50.0, 49.0)  # crossed: invalid, never a touch
        extras = [ticker("BTC/EUR", DUE + timedelta(hours=1), 50.0, 50.1),  # after now
                  ticker("BTC/EUR", DUE, None, 50.1)]
        report = ps.close_positions(self.conn, now=DUE + timedelta(minutes=5), extra_quotes=extras)
        self.assertEqual((report.closed, report.pending), ((), (1,)))

    def test_repeat_and_second_writer_write_nothing(self):
        self.opened()
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=2), 97.9, 98.0)
        now = T0 + timedelta(minutes=3)
        self.assertEqual(len(ps.close_positions(self.conn, now=now).closed), 1)
        before = all_rows(self.conn)
        changes = self.conn.total_changes
        self.assertEqual(ps.close_positions(self.conn, now=now).closed, ())
        self.assertEqual(ps.close_positions(self.connect(), now=now).closed, ())
        self.assertEqual(all_rows(self.conn), before)
        self.assertEqual(self.conn.total_changes, changes)

    def test_open_position_is_marked_conservatively(self):
        self.opened()
        # No newer quote: the entry bid. 0.19 x 99.9 = 18.981, exit fee 0.0493506 -> 0.05 up, basis 19.05.
        state = ps.account_state(self.conn, now=T0 + timedelta(minutes=1))
        assert state is not None
        self.assertEqual((state.equity, state.cash, state.open_planned_loss, state.open_notional),
                         (D("239.881"), D("220.95"), D("0.5731"), D("19.00")))
        # Ticker bid 99: 18.81 - 0.048906 up to 0.05 - 19.05 = -0.29.
        evaluation = ps.evaluate_locks(self.conn, now=T0 + timedelta(minutes=2),
                                       extra_quotes=[ticker("BTC/EUR", T0 + timedelta(minutes=2), 99.0, 99.1)])
        self.assertEqual((evaluation.equity, evaluation.day_start, evaluation.high_water), (D("239.71"), D("240.00"), D("240.00")))
        self.assertEqual(evaluation.tripped, ())


class TestLocks(PilotDbCase):
    def trip_daily(self) -> ps.StoredLock:
        """Open at T0 and gap down to bid 87 at 23:00: -2.47 gross, 0.05 + 0.04 fees, net -2.56."""
        self.ready()
        self.open(candidate("e1"))
        late = datetime(2026, 9, 29, 23, 0, tzinfo=UTC)
        self.add_spot("BTC/EUR", late, 87.0, 87.1)
        report = ps.close_positions(self.conn, now=late + timedelta(seconds=5))
        self.assertEqual(report.closed[0].net, D("-2.56"))
        self.assertEqual((report.locks.equity, report.locks.day_start, report.locks.high_water),
                         (D("237.44"), D("240.00"), D("240.00")))
        (lock,) = report.locks.tripped
        self.assertEqual((lock.kind, lock.equity, lock.reference, lock.limit_pct, lock.utc_day, lock.evaluated_on),
                         (LockKind.DAILY_LOSS, D("237.44"), D("240.00"), D("1"), "2026-09-29", ps.LockTrigger.SETTLE))
        return lock

    def test_daily_lock_survives_midnight_restart_and_repeated_evaluation(self):
        lock = self.trip_daily()
        next_day = datetime(2026, 9, 30, 0, 0, 1, tzinfo=UTC)
        fresh = self.connect()  # a new process sees the persisted lock
        for step in range(5):
            evaluation = ps.evaluate_locks(fresh, now=next_day + timedelta(minutes=step))
            self.assertEqual([found.lock_id for found in evaluation.active], [lock.lock_id])
            self.assertEqual(evaluation.tripped, ())
        # The first evaluation of the new UTC day recorded its day start once; no new lock row.
        day_marks = [m for m in ps.read_marks(fresh) if m.kind is ps.MarkKind.DAY_START]
        self.assertEqual([(m.utc_day, m.equity, m.source) for m in day_marks],
                         [("2026-09-29", D("240.00"), ps.MarkSource.EVALUATION),
                          ("2026-09-30", D("237.44"), ps.MarkSource.EVALUATION)])
        self.assertEqual(self.count(ps.LOCK_TABLE), 1)
        report = self.open(candidate("e2", ts=next_day), now=next_day, conn=fresh)
        self.assertEqual(report.decisions[0].reason, NoTradeReason.DAILY_LOSS_LOCK)

    def test_only_a_review_clears_a_lock_and_rebases_the_day(self):
        lock = self.trip_daily()
        next_day = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
        ps.evaluate_locks(self.conn, now=next_day)
        review = ps.review_lock(self.conn, lock_id=lock.lock_id, reviewer="operator", cause="gap on BTC", now=next_day)
        self.assertEqual((review.lock_id, review.reviewer, review.cause, review.rebase_equity),
                         (lock.lock_id, "operator", "gap on BTC", D("237.44")))
        (stored,) = ps.read_locks(self.conn)
        self.assertFalse(stored.active)
        self.assertEqual(stored.review, review)
        last = ps.read_marks(self.conn)[-1]
        self.assertEqual((last.kind, last.utc_day, last.equity, last.source, last.lock_id),
                         (ps.MarkKind.DAY_START, "2026-09-30", D("237.44"), ps.MarkSource.REVIEW, lock.lock_id))
        self.assertEqual(ps.day_start(self.conn, "2026-09-30"), D("237.44"))
        # Entries resume, sized on the equity now: 237.44 x 0.25% = 0.5936 -> 0.19 lots still.
        report = self.open(candidate("e2", ts=next_day), now=next_day + timedelta(minutes=1))
        decision = report.decisions[0]
        self.assertTrue(decision.opened)
        self.assertEqual((decision.value("equity"), decision.value("per_entry_loss_cap"), decision.value("quantity")),
                         (D("237.44"), D("0.5936"), D("0.19")))
        with self.assertRaises(ps.PilotStoreError) as caught:
            ps.review_lock(self.conn, lock_id=lock.lock_id, reviewer="operator", cause="again", now=next_day)
        self.assertIs(caught.exception.code, ps.PilotStoreFailure.LOCK_ALREADY_REVIEWED)
        with self.assertRaises(ps.PilotStoreError) as caught:
            ps.review_lock(self.conn, lock_id=999, reviewer="operator", cause="none", now=next_day)
        self.assertIs(caught.exception.code, ps.PilotStoreFailure.UNKNOWN_LOCK)
        for reviewer, cause in (("", "c"), ("operator", "  ")):
            with self.assertRaises(ps.PilotStoreError):
                ps.review_lock(self.conn, lock_id=lock.lock_id, reviewer=reviewer, cause=cause, now=next_day)

    def test_drawdown_lock_and_its_review_rebase_the_high_water(self):
        self.ready()
        self.open(candidate("e1"))
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=5), 60.0, 60.1)
        report = ps.close_positions(self.conn, now=T0 + timedelta(minutes=6))
        # 0.19 x -40 = -7.60; fees 0.05 + 0.02964 -> 0.03: net -7.68, equity 232.32 <= 240 x 0.97 = 232.80.
        self.assertEqual(report.closed[0].net, D("-7.68"))
        self.assertEqual([(lock.kind, lock.reference) for lock in report.locks.tripped],
                         [(LockKind.DAILY_LOSS, D("240.00")), (LockKind.DRAWDOWN, D("240.00"))])
        daily, drawdown = report.locks.tripped
        ps.review_lock(self.conn, lock_id=drawdown.lock_id, reviewer="operator", cause="gap", now=T0 + timedelta(minutes=7))
        self.assertEqual(ps.high_water(self.conn), D("232.32"))
        self.assertEqual([lock.kind for lock in ps.active_locks(self.conn)], [LockKind.DAILY_LOSS])
        # The rebased references do not re-trip on the next evaluation.
        evaluation = ps.evaluate_locks(self.conn, now=T0 + timedelta(minutes=8))
        self.assertEqual((evaluation.tripped, evaluation.active_kinds), ((), (LockKind.DAILY_LOSS,)))
        ps.review_lock(self.conn, lock_id=daily.lock_id, reviewer="operator", cause="gap", now=T0 + timedelta(minutes=9))
        decision = self.open(candidate("e2", ts=T0 + timedelta(minutes=10)), now=T0 + timedelta(minutes=10)).decisions[0]
        self.assertTrue(decision.opened)

    def test_high_water_only_rises_and_never_resets(self):
        self.ready()
        self.open(candidate("e1"))
        ps.close_positions(self.conn, now=T0 + timedelta(minutes=2),
                           extra_quotes=[ticker("BTC/EUR", T0 + timedelta(minutes=1), 104.2, 104.3)])
        self.assertEqual(ps.high_water(self.conn), D("240.70"))
        self.open(candidate("e2", ts=T0 + timedelta(minutes=3)), now=T0 + timedelta(minutes=3))
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=4), 99.0, 99.1)
        for day in range(3):
            ps.evaluate_locks(self.conn, now=T0 + timedelta(days=day, minutes=5))
        self.assertEqual(ps.high_water(self.conn), D("240.70"))
        highs = [(m.equity, m.source) for m in ps.read_marks(self.conn) if m.kind is ps.MarkKind.HIGH_WATER]
        self.assertEqual(highs, [(D("240.00"), ps.MarkSource.ASSIGNED), (D("240.70"), ps.MarkSource.EVALUATION)])

    def test_unrealized_loss_trips_on_a_mark_call(self):
        self.ready()
        self.open(candidate("e1"))
        # Ticker bid 80: 15.2 - 0.03952 up to 0.04 - 19.05 = -3.89: equity 236.11 <= 237.60.
        evaluation = ps.evaluate_locks(self.conn, now=T0 + timedelta(minutes=1),
                                       extra_quotes=[ticker("BTC/EUR", T0 + timedelta(minutes=1), 80.0, 80.1)])
        self.assertEqual(evaluation.equity, D("236.11"))
        self.assertEqual([(lock.kind, lock.evaluated_on) for lock in evaluation.tripped],
                         [(LockKind.DAILY_LOSS, ps.LockTrigger.MARK)])
        self.assertEqual(len(ps.read_open_positions(self.conn)), 1)  # a mark call closes nothing


class TestKillSwitch(PilotDbCase):
    def test_default_released_and_latest_row_wins(self):
        self.ready()
        self.assertEqual(ps.kill_switch(self.conn), ps.KillSwitch(engaged=False))
        engaged = ps.engage_kill_switch(self.conn, reason="going on holiday", actor="operator", now=T0)
        self.assertEqual((engaged.engaged, engaged.reason, engaged.actor), (True, "going on holiday", "operator"))
        released = ps.release_kill_switch(self.conn, reason="back", actor="operator", now=T0 + timedelta(hours=1))
        self.assertFalse(released.engaged)
        self.assertEqual(self.count(ps.KILL_SWITCH_TABLE), 2)
        with self.assertRaises(ps.PilotStoreError):
            ps.engage_kill_switch(self.conn, reason="", actor="operator", now=T0)
        self.assertEqual(self.count(ps.KILL_SWITCH_TABLE), 2)

    def test_engaged_switch_blocks_entries_but_positions_still_close(self):
        self.ready()
        self.open(candidate("e1"))
        ps.engage_kill_switch(self.conn, reason="stop", actor="operator", now=T0 + timedelta(minutes=1))
        refused = self.open(candidate("e2", asset="ETH"), now=T0 + timedelta(minutes=1)).decisions[0]
        self.assertEqual(refused.reason, NoTradeReason.KILL_SWITCH_ENGAGED)
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=2), 97.9, 98.0)
        self.assertEqual(len(ps.close_positions(self.conn, now=T0 + timedelta(minutes=3)).closed), 1)
        self.assertEqual(self.open(candidate("e3"), now=T0 + timedelta(minutes=4)).decisions[0].reason,
                         NoTradeReason.KILL_SWITCH_ENGAGED)
        ps.release_kill_switch(self.conn, reason="resume", actor="operator", now=T0 + timedelta(minutes=5))
        self.assertTrue(self.open(candidate("e4"), now=T0 + timedelta(minutes=6)).decisions[0].opened)


class TestAtomicity(PilotDbCase):
    def test_open_rolls_back_entirely_on_an_injected_failure(self):
        self.ready()
        before = all_rows(self.conn)
        with mock.patch.object(ps, "_write_position", side_effect=RuntimeError("disk gone")):
            with self.assertRaisesRegex(RuntimeError, "disk gone"):
                self.open(candidate("skip", direction="SHORT"), candidate("e1"))
        # Neither the refusal written before the failure nor the day-start mark survived.
        self.assertEqual(all_rows(self.conn), before)
        self.assertFalse(self.conn.in_transaction)
        self.assertTrue(self.open(candidate("e1")).decisions[0].opened)

    def test_close_rolls_back_entirely_on_an_injected_failure(self):
        self.ready()
        self.open(candidate("e1"))
        before = all_rows(self.conn)
        at = T0 + timedelta(minutes=1)
        with mock.patch.object(ps, "_evaluate_locks", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(ps.PilotStoreError) as caught:
                ps.close_positions(self.conn, now=at, extra_quotes=[ticker("BTC/EUR", at, 97.0, 97.1)])
        self.assertIs(caught.exception.code, ps.PilotStoreFailure.SQLITE_ERROR)
        self.assertEqual(all_rows(self.conn), before)  # no close and no exit quote row
        self.assertEqual(len(ps.close_positions(self.conn, now=at, extra_quotes=[ticker("BTC/EUR", at, 97.0, 97.1)]).closed), 1)

    def test_review_rolls_back_entirely_on_an_injected_failure(self):
        self.ready()
        self.open(candidate("e1"))
        self.add_spot("BTC/EUR", T0 + timedelta(minutes=1), 87.0, 87.1)
        (lock,) = ps.close_positions(self.conn, now=T0 + timedelta(minutes=2)).locks.tripped
        before = all_rows(self.conn)
        with mock.patch.object(ps, "_insert_mark", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ps.review_lock(self.conn, lock_id=lock.lock_id, reviewer="operator", cause="c", now=T0 + timedelta(minutes=3))
        self.assertEqual(all_rows(self.conn), before)
        self.assertTrue(ps.active_locks(self.conn))


if __name__ == "__main__":
    unittest.main()
