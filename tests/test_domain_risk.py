"""Pilot shadow Risk Engine (radar_v08/domain/risk.py): hand-derived exact Decimal cases.

Unless said otherwise: envelope 240.00 EUR at the EX-1 values, spot taker fee 26 bps per
leg (f = 0.0026), stress exit slippage 50 bps (stressed exit = stop x 0.995), quote
99.9/100 and ATR 1. Then the EX-1 levels are stop = 100 - 2 x 1 = 98 and target =
100 + 2 x 2 = 104; with tick 0.01 they stay as they are, and

    stressed exit = 98 x 0.995 = 97.51
    loss per unit = (100 - 97.51) + 0.0026 x (100 + 97.51) = 2.49 + 0.513526 = 3.003526

The limits at equity 240.00: per entry 0.60, aggregate 1.20, notional 24.00, buffer 24.00.
"""

from __future__ import annotations

import unittest
from decimal import ROUND_FLOOR, Decimal

from radar_v08.domain import paper, risk
from radar_v08.domain.paper import Outcome
from radar_v08.domain.risk import (
    BindingConstraint,
    Envelope,
    EnvelopeError,
    LockKind,
    NoTrade,
    NoTradeReason,
)

D = Decimal
FEE = D("26")
EQUITY = D("240.00")


def envelope(**overrides: object) -> Envelope:
    return Envelope(EQUITY, "EUR", **overrides)  # type: ignore[arg-type]


def rules(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {"lot_decimals": 2, "ordermin": "0.01", "costmin": "0.50", "tick_size": "0.01"}
    entry.update(overrides)
    return entry


def plan(
    env: Envelope | None = None,
    *,
    currency: object = "EUR",
    bid: object = D("99.9"),
    ask: object = D("100"),
    status: object = "online",
    atr: object = D("1"),
    entry: object = None,
    equity: object = EQUITY,
    cash: object = EQUITY,
    open_planned_loss: str = "0",
    open_notional: str = "0",
) -> risk.EntryPlan:
    return risk.plan_long_entry(
        env or envelope(),
        quote_currency=currency,
        bid=bid,
        ask=ask,
        status=status,
        atr=atr,
        pair_entry=rules() if entry is None else entry,
        equity=equity,
        cash=cash,
        open_planned_loss=D(open_planned_loss),
        open_notional=D(open_notional),
        fee_bps=FEE,
    )


def floor10(value: Decimal | None) -> Decimal:
    assert value is not None
    return value.quantize(D("1E-10"), rounding=ROUND_FLOOR)


class TestEnvelope(unittest.TestCase):
    def test_defaults_equal_the_ex1_table(self):
        env = envelope()
        self.assertEqual(
            (env.per_entry_loss_pct, env.aggregate_loss_pct, env.gross_notional_pct, env.cash_buffer_pct),
            (D("0.25"), D("0.50"), D("10"), D("10")),
        )
        self.assertEqual((env.daily_loss_pct, env.drawdown_pct, env.max_positions, env.leverage), (D("1"), D("3"), 1, 0))
        self.assertIsNone(env.per_entry_abs_cap)

    def test_every_looser_value_is_refused(self):
        looser = {
            "per_entry_loss_pct": D("0.26"),
            "aggregate_loss_pct": D("0.51"),
            "gross_notional_pct": D("10.01"),
            "cash_buffer_pct": D("9.99"),
            "daily_loss_pct": D("1.01"),
            "drawdown_pct": D("3.01"),
            "max_positions": 2,
            "leverage": 1,
        }
        for field, value in looser.items():
            with self.subTest(field=field), self.assertRaises(EnvelopeError) as caught:
                envelope(**{field: value})
            self.assertEqual(caught.exception.field, field)

    def test_stricter_values_are_accepted(self):
        env = envelope(
            per_entry_loss_pct=D("0.10"),
            aggregate_loss_pct=D("0.20"),
            gross_notional_pct=D("5"),
            cash_buffer_pct=D("50"),
            daily_loss_pct=D("0.5"),
            drawdown_pct=D("2"),
            per_entry_abs_cap=D("0.30"),
        )
        self.assertEqual(env.per_entry_abs_cap, D("0.30"))

    def test_invalid_values_are_refused(self):
        cases = [
            ("equity", {"equity": D("240.001")}),
            ("equity", {"equity": D("0")}),
            ("equity", {"equity": 240}),
            ("currency", {"currency": ""}),
            ("currency", {"currency": " EUR"}),
            ("per_entry_loss_pct", {"per_entry_loss_pct": D("0")}),
            ("per_entry_loss_pct", {"per_entry_loss_pct": 0.25}),
            ("drawdown_pct", {"drawdown_pct": D("NaN")}),
            ("cash_buffer_pct", {"cash_buffer_pct": D("100")}),
            ("per_entry_abs_cap", {"per_entry_abs_cap": D("0")}),
            ("per_entry_abs_cap", {"per_entry_abs_cap": D("0.001")}),
            ("max_positions", {"max_positions": 0}),
            ("max_positions", {"max_positions": True}),
            ("leverage", {"leverage": -1}),
        ]
        for field, values in cases:
            arguments: dict[str, object] = {"equity": EQUITY, "currency": "EUR"}
            arguments.update(values)
            with self.subTest(values=values), self.assertRaises(EnvelopeError) as caught:
                Envelope(**arguments)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.field, field)

    def test_canonical_text_is_stable_and_detects_a_change(self):
        self.assertEqual(envelope().canonical_text(), envelope().canonical_text())
        self.assertIn('"policy_id":"ex1_envelope_v1"', envelope().canonical_text())
        self.assertNotEqual(envelope().canonical_text(), envelope(per_entry_abs_cap=D("0.50")).canonical_text())

    def test_budget_amounts(self):
        # 240 x 0.25% = 0.60; 0.50% = 1.20; 10% = 24.00; buffer 10% = 24.00.
        room = risk.budget(envelope(), EQUITY, D("200"), D("0.20"), D("5"))
        self.assertEqual(room.per_entry_loss_cap, D("0.6"))
        self.assertEqual((room.aggregate_loss_cap, room.aggregate_loss_remaining), (D("1.2"), D("1.0")))
        self.assertEqual((room.notional_cap, room.notional_remaining), (D("24"), D("19")))
        self.assertEqual((room.cash_buffer, room.cash_room), (D("24"), D("176")))

    def test_absolute_cap_applies_only_when_lower(self):
        self.assertEqual(risk.budget(envelope(per_entry_abs_cap=D("0.09")), EQUITY, EQUITY, D(0), D(0)).per_entry_loss_cap, D("0.09"))
        self.assertEqual(risk.budget(envelope(per_entry_abs_cap=D("1.00")), EQUITY, EQUITY, D(0), D(0)).per_entry_loss_cap, D("0.6"))


class TestPairRules(unittest.TestCase):
    def test_kraken_entry_parses_exactly(self):
        parsed = risk.parse_pair_rules(
            {"lot_decimals": 8, "ordermin": "0.00005", "costmin": "0.5", "tick_size": "0.1", "pair_decimals": 1}
        )
        self.assertEqual(parsed, risk.PairRules(8, D("0.00005"), D("0.5"), D("0.1"), "tick_size"))
        assert isinstance(parsed, risk.PairRules)
        self.assertEqual(parsed.lot, D("1E-8"))

    def test_tick_falls_back_to_pair_decimals(self):
        parsed = risk.parse_pair_rules({"lot_decimals": 0, "ordermin": "1", "costmin": "0", "pair_decimals": 4})
        self.assertEqual(parsed, risk.PairRules(0, D("1"), D("0"), D("0.0001"), "pair_decimals"))

    def assert_missing(self, entry: object, detail: str):
        self.assertEqual(risk.parse_pair_rules(entry), NoTrade(NoTradeReason.MISSING_PAIR_RULES, detail))

    def test_every_missing_or_invalid_field_is_typed_never_zero(self):
        self.assert_missing(None, "entry:missing")
        base = {"lot_decimals": 8, "ordermin": "0.0001", "costmin": "0.5", "tick_size": "0.1"}
        cases = [
            ("lot_decimals", None, "lot_decimals:missing"),
            ("lot_decimals", "8", "lot_decimals:not_an_integer"),
            ("lot_decimals", True, "lot_decimals:not_an_integer"),
            ("lot_decimals", 8.0, "lot_decimals:not_an_integer"),
            ("lot_decimals", -1, "lot_decimals:negative"),
            ("lot_decimals", 19, "lot_decimals:too_large"),
            ("ordermin", None, "ordermin:missing"),
            ("ordermin", "", "ordermin:missing"),
            ("ordermin", "abc", "ordermin:not_a_number"),
            ("ordermin", [], "ordermin:not_a_number"),
            ("ordermin", 0.0001, "ordermin:not_exact"),
            ("ordermin", "-0.0001", "ordermin:negative"),
            ("ordermin", "NaN", "ordermin:not_finite"),
            ("ordermin", "Infinity", "ordermin:not_finite"),
            ("costmin", None, "costmin:missing"),
            ("costmin", "x", "costmin:not_a_number"),
            ("costmin", 0.5, "costmin:not_exact"),
            ("costmin", "-1", "costmin:negative"),
            ("costmin", "-Infinity", "costmin:not_finite"),
            ("tick_size", "0", "tick_size:not_positive"),
            ("tick_size", "-0.1", "tick_size:negative"),
            ("tick_size", "tick", "tick_size:not_a_number"),
            ("tick_size", 0.1, "tick_size:not_exact"),
            ("tick_size", "sNaN", "tick_size:not_finite"),
        ]
        for field, value, detail in cases:
            entry = dict(base)
            if value is None:
                entry.pop(field)
            else:
                entry[field] = value
            with self.subTest(field=field, value=value):
                self.assert_missing(entry, detail)

    def test_missing_tick_and_invalid_pair_decimals(self):
        base = {"lot_decimals": 8, "ordermin": "0.0001", "costmin": "0.5"}
        self.assert_missing(base, "tick_size:missing")
        self.assert_missing({**base, "tick_size": None}, "tick_size:missing")
        self.assert_missing({**base, "pair_decimals": -2}, "pair_decimals:negative")
        self.assert_missing({**base, "pair_decimals": "1"}, "pair_decimals:not_an_integer")
        # A present but invalid tick_size is refused, not replaced by pair_decimals.
        self.assert_missing({**base, "tick_size": "", "pair_decimals": 1}, "tick_size:missing")


class TestRounding(unittest.TestCase):
    def test_lot_floor(self):
        self.assertEqual(risk.floor_to_lot(D("0.123456789"), 8), D("0.12345678"))
        self.assertEqual(risk.floor_to_lot(D("0.123456789"), 0), D("0"))
        self.assertEqual(risk.floor_to_lot(D("7.999"), 0), D("7"))
        self.assertEqual(risk.floor_to_lot(D("0.19976520929"), 2), D("0.19"))
        self.assertEqual(risk.floor_to_lot(D("0.12"), 2), D("0.12"))

    def test_tick_floor(self):
        self.assertEqual(risk.floor_to_step(D("97.999"), D("0.01")), D("97.99"))
        self.assertEqual(risk.floor_to_step(D("97.999"), D("0.5")), D("97.5"))
        self.assertEqual(risk.floor_to_step(D("0.9"), D("1")), D("0"))
        self.assertEqual(risk.floor_to_step(D("98"), D("0.1")), D("98.0"))

    def test_plain_detail_text(self):
        self.assertEqual(risk.plain(D("0.0000")), "0")
        self.assertEqual(risk.plain(D("1E+1")), "10")
        self.assertEqual(risk.plain(D("0.00005000")), "0.00005")

    def test_cents_up(self):
        self.assertEqual(risk.cents_up(D("0.0494")), D("0.05"))
        self.assertEqual(risk.cents_up(D("0.05")), D("0.05"))
        self.assertEqual(risk.cents_up(D("0.050001")), D("0.06"))


class TestAdmission(unittest.TestCase):
    def refusal(self, direction: object = "LONG", *, kill: bool = False, locks=(), positions: int = 0):
        return risk.admission_refusal(envelope(), direction, kill_switch_engaged=kill, active_locks=locks, open_positions=positions)

    def test_long_with_nothing_blocking_is_admitted(self):
        self.assertIsNone(self.refusal())
        self.assertIsNone(self.refusal(paper.Direction.LONG))

    def test_only_long(self):
        for direction in ("SHORT", paper.Direction.SHORT, "long", None, ""):
            with self.subTest(direction=direction):
                self.assertEqual(self.refusal(direction).reason, NoTradeReason.UNSUPPORTED_DIRECTION)

    def test_order_kill_switch_then_locks_then_position(self):
        self.assertEqual(self.refusal("SHORT", kill=True).reason, NoTradeReason.UNSUPPORTED_DIRECTION)
        self.assertEqual(self.refusal(kill=True, locks=[LockKind.DAILY_LOSS], positions=1).reason, NoTradeReason.KILL_SWITCH_ENGAGED)
        both = [LockKind.DRAWDOWN, LockKind.DAILY_LOSS]
        self.assertEqual(self.refusal(locks=both, positions=1).reason, NoTradeReason.DAILY_LOSS_LOCK)
        self.assertEqual(self.refusal(locks=[LockKind.DRAWDOWN], positions=1).reason, NoTradeReason.DRAWDOWN_LOCK)
        self.assertEqual(self.refusal(positions=1), NoTrade(NoTradeReason.POSITION_ALREADY_OPEN, "1"))

    def test_bad_inputs_raise(self):
        with self.assertRaises(risk.RiskInputError):
            self.refusal(kill=1)  # type: ignore[arg-type]
        with self.assertRaises(risk.RiskInputError):
            self.refusal(positions=-1)
        with self.assertRaises(risk.RiskInputError):
            self.refusal(locks=["daily_loss"])


class TestSizingWorkedExamples(unittest.TestCase):
    def assert_opened(self, result: risk.EntryPlan, quantity: str, notional: str, entry_fee: str, exit_fee: str, planned: str):
        self.assertIsNone(result.no_trade)
        self.assertTrue(result.opened)
        assert result.costs is not None
        self.assertEqual(result.quantity, D(quantity))
        self.assertEqual(
            (result.costs.quantity, result.costs.notional, result.costs.entry_fee, result.costs.exit_fee, result.costs.planned_loss),
            (D(quantity), D(notional), D(entry_fee), D(exit_fee), D(planned)),
        )

    def test_levels_and_loss_per_unit(self):
        result = plan()
        assert result.levels is not None
        self.assertEqual((result.levels.stop, result.levels.target, result.levels.policy_id), (D("98"), D("104"), "ex1_initial_paper_v1"))
        self.assertEqual((result.stop, result.target), (D("98.00"), D("104.00")))
        self.assertEqual(result.stress_exit_price, D("97.51"))
        self.assertEqual(result.loss_per_unit, D("3.003526"))
        self.assertEqual((result.stress_bps, result.stress_policy_id), (D("50"), "pilot_stress_exit_slippage_50bps_v1"))
        self.assertEqual(result.sizing_policy_id, "pilot_sizing_v1")

    def test_per_entry_loss_binding(self):
        # 0.60 / 3.003526 = 0.19976520929...; 1.20 / 3.003526 = 0.39953041858...
        # 24 / 100 = 0.24; (240 - 24) / (100 x 1.0026) = 216 / 100.26 = 2.15439856373...
        # min = 0.1997... -> lot 0.19. notional 19.00; entry fee 19 x 0.0026 = 0.0494 -> 0.05;
        # exit fee 0.19 x 97.51 x 0.0026 = 0.04816994 -> 0.05; planned = 0.19 x 2.49 + 0.10 = 0.5731.
        result = plan()
        candidates = result.candidates
        assert candidates is not None
        self.assertEqual(floor10(candidates.per_entry_loss), D("0.1997652092"))
        self.assertEqual(floor10(candidates.aggregate_loss), D("0.3995304185"))
        self.assertEqual(candidates.notional, D("0.24"))
        self.assertEqual(floor10(candidates.cash), D("2.1543985637"))
        self.assertEqual(candidates.binding, BindingConstraint.PER_ENTRY_LOSS)
        # Rounded toward zero: the candidate never supports more than the cap.
        self.assertLessEqual(candidates.per_entry_loss * D("3.003526"), D("0.6"))
        self.assertEqual((result.lot_quantity, result.passes), (D("0.19"), 0))
        self.assert_opened(result, "0.19", "19.00", "0.05", "0.05", "0.5731")

    def test_aggregate_loss_binding(self):
        # Open planned loss 1.00 leaves 0.20: 0.20 / 3.003526 = 0.06658840309... -> 0.06.
        # notional 6.00; fees 0.0156 -> 0.02 and 0.06 x 97.51 x 0.0026 = 0.01521156 -> 0.02;
        # planned = 0.06 x 2.49 + 0.04 = 0.1894 <= 0.20.
        result = plan(open_planned_loss="1.00")
        assert result.candidates is not None
        self.assertEqual(result.candidates.binding, BindingConstraint.AGGREGATE_LOSS)
        self.assertEqual(floor10(result.candidates.aggregate_loss), D("0.0665884030"))
        self.assert_opened(result, "0.06", "6.00", "0.02", "0.02", "0.1894")

    def test_notional_binding(self):
        # ATR 0.25: stop 99.5, target 101, stressed 99.5 x 0.995 = 99.0025,
        # loss per unit = 0.9975 + 0.0026 x 199.0025 = 1.5149065.
        # 0.60 / 1.5149065 = 0.396064...; 24 / 100 = 0.24 (binding) -> 0.24.
        # notional 24.00; fees 0.0624 -> 0.07 and 0.24 x 99.0025 x 0.0026 = 0.0617775... -> 0.07;
        # planned = 0.24 x 0.9975 + 0.14 = 0.3794.
        result = plan(atr=D("0.25"))
        assert result.candidates is not None
        self.assertEqual(result.loss_per_unit, D("1.5149065"))
        self.assertEqual(floor10(result.candidates.per_entry_loss), D("0.3960640475"))
        self.assertEqual(result.candidates.binding, BindingConstraint.NOTIONAL)
        self.assert_opened(result, "0.24", "24.00", "0.07", "0.07", "0.3794")

    def test_cash_binding(self):
        # Cash 40: room 40 - 24 = 16; 16 / 100.26 = 0.15958507879... -> 0.15.
        # notional 15.00; fees 0.039 -> 0.04 and 0.15 x 99.0025 x 0.0026 = 0.03861... -> 0.04;
        # planned = 0.15 x 0.9975 + 0.08 = 0.229625; cash needed 15.04 <= 16.
        result = plan(atr=D("0.25"), cash=D("40"))
        assert result.candidates is not None
        self.assertEqual(result.candidates.binding, BindingConstraint.CASH)
        self.assertEqual(floor10(result.candidates.cash), D("0.1595850787"))
        self.assert_opened(result, "0.15", "15.00", "0.04", "0.04", "0.229625")

    def test_integer_lots(self):
        # lot_decimals 0 floors 0.1997... to 0 -> below the order minimum, never up to 1.
        result = plan(entry=rules(lot_decimals=0, ordermin="0", costmin="0"))
        self.assertEqual(result.lot_quantity, D("0"))
        self.assertEqual(result.no_trade, NoTrade(NoTradeReason.BELOW_ORDER_MINIMUM, "0<0"))

    def test_absolute_cap_tighter_than_percentage(self):
        # Cap 0.09 < 0.60: 0.09 / 3.003526 = 0.0299647813... -> lot 3 -> 0.029 (see the passes below).
        result = plan(envelope(per_entry_abs_cap=D("0.09")), entry=rules(lot_decimals=3, ordermin="0", costmin="0"))
        assert result.budget is not None and result.candidates is not None
        self.assertEqual(result.budget.per_entry_loss_cap, D("0.09"))
        self.assertEqual(result.candidates.binding, BindingConstraint.PER_ENTRY_LOSS)
        self.assertEqual(floor10(result.candidates.per_entry_loss), D("0.0299647813"))


class TestMinimums(unittest.TestCase):
    # The worked quantity is 0.19 at ask 100: cost 19.00.

    def test_order_minimum_boundaries(self):
        self.assertTrue(plan(entry=rules(ordermin="0.18")).opened)
        self.assertTrue(plan(entry=rules(ordermin="0.19")).opened)
        result = plan(entry=rules(ordermin="0.20"))
        self.assertEqual(result.no_trade, NoTrade(NoTradeReason.BELOW_ORDER_MINIMUM, "0.19<0.2"))
        self.assertEqual(result.quantity, D("0.19"))
        self.assertIsNone(result.costs)

    def test_order_minimum_just_above_by_one_digit(self):
        self.assertEqual(plan(entry=rules(ordermin="0.190000001")).no_trade.reason, NoTradeReason.BELOW_ORDER_MINIMUM)  # type: ignore[union-attr]

    def test_cost_minimum_boundaries(self):
        self.assertTrue(plan(entry=rules(costmin="18.99")).opened)
        self.assertTrue(plan(entry=rules(costmin="19.00")).opened)
        result = plan(entry=rules(costmin="19.01"))
        self.assertEqual(result.no_trade, NoTrade(NoTradeReason.BELOW_COST_MINIMUM, "19<19.01"))
        self.assertEqual(result.quantity, D("0.19"))


class TestDownwardPasses(unittest.TestCase):
    """Lot 0.001, stop 98, stressed 97.51: each lot adds 0.00249 of price loss; both fees stay 0.01 each.

    planned(q) = q x 2.49 + 0.01 + 0.01 for these small quantities.
    """

    def sized(self, cap: str, **entry: object) -> risk.EntryPlan:
        pair = rules(lot_decimals=3, ordermin="0", costmin="0")
        pair.update(entry)
        return plan(envelope(per_entry_abs_cap=D(cap)), entry=pair)

    def test_one_pass(self):
        # 0.09 / 3.003526 -> 0.029: 0.07221 + 0.02 = 0.09221 > 0.09; 0.028: 0.06972 + 0.02 = 0.08972.
        result = self.sized("0.09")
        self.assertEqual((result.lot_quantity, result.quantity, result.passes), (D("0.029"), D("0.028"), 1))
        assert result.costs is not None
        self.assertEqual(result.costs.planned_loss, D("0.08972"))
        self.assertTrue(result.opened)

    def test_two_passes(self):
        # 0.08 / 3.003526 -> 0.026: 0.08474; 0.025: 0.08225; 0.024: 0.05976 + 0.02 = 0.07976 <= 0.08.
        result = self.sized("0.08")
        self.assertEqual((result.lot_quantity, result.quantity, result.passes), (D("0.026"), D("0.024"), 2))
        self.assertTrue(result.opened)

    def test_three_passes_still_open(self):
        # 0.06 -> 0.019: 0.06731; 0.018: 0.06482; 0.017: 0.06233; 0.016: 0.05984 <= 0.06.
        result = self.sized("0.06")
        self.assertEqual((result.lot_quantity, result.quantity, result.passes), (D("0.019"), D("0.016"), 3))
        self.assertTrue(result.opened)

    def test_more_than_three_passes_is_sizing_inconsistent(self):
        # 0.05 -> 0.016: 0.05984; 0.015: 0.05735; 0.014: 0.05486; 0.013: 0.05237 > 0.05 -> give up.
        result = self.sized("0.05")
        self.assertEqual(result.no_trade, NoTrade(NoTradeReason.SIZING_INCONSISTENT, "passes:3"))
        self.assertEqual((result.lot_quantity, result.quantity, result.passes), (D("0.016"), D("0.013"), 3))
        assert result.costs is not None
        self.assertEqual(result.costs.planned_loss, D("0.05237"))

    def test_a_pass_that_falls_below_the_minimum_refuses_never_rounds_up(self):
        # 0.08 needs 0.024, but the order minimum 0.026 is left on the first pass (0.025).
        result = self.sized("0.08", ordermin="0.026")
        self.assertEqual(result.no_trade, NoTrade(NoTradeReason.BELOW_ORDER_MINIMUM, "0.025<0.026"))
        self.assertEqual(result.passes, 1)


class TestSizingRefusals(unittest.TestCase):
    def assert_reason(self, result: risk.EntryPlan, reason: NoTradeReason, detail: str | None = None):
        assert result.no_trade is not None
        self.assertEqual(result.no_trade.reason, reason)
        if detail is not None:
            self.assertEqual(result.no_trade.detail, detail)
        self.assertFalse(result.opened)
        self.assertIsNone(result.costs)

    def test_quote_currency(self):
        self.assert_reason(plan(currency="USD"), NoTradeReason.QUOTE_CURRENCY_MISMATCH, "USD")
        self.assert_reason(plan(currency=None), NoTradeReason.QUOTE_CURRENCY_MISMATCH, "None")

    def test_invalid_quote(self):
        self.assert_reason(plan(bid=None), NoTradeReason.INVALID_QUOTE, "missing")
        self.assert_reason(plan(bid=D("101")), NoTradeReason.INVALID_QUOTE, "crossed")
        self.assert_reason(plan(status="cancel_only"), NoTradeReason.INVALID_QUOTE, "not_online")
        self.assert_reason(plan(ask=D("0")), NoTradeReason.INVALID_QUOTE, "not_positive")

    def test_no_valid_atr(self):
        self.assert_reason(plan(atr=None), NoTradeReason.NO_VALID_ATR, "missing")
        self.assert_reason(plan(atr=D("0")), NoTradeReason.NO_VALID_ATR, "not_positive")
        self.assert_reason(plan(atr=D("Infinity")), NoTradeReason.NO_VALID_ATR, "not_finite")

    def test_missing_pair_rules(self):
        self.assert_reason(plan(entry={}), NoTradeReason.MISSING_PAIR_RULES, "lot_decimals:missing")
        self.assert_reason(plan(entry=rules(costmin=None)), NoTradeReason.MISSING_PAIR_RULES, "costmin:missing")

    def test_equity_unavailable(self):
        self.assert_reason(plan(equity=None), NoTradeReason.EQUITY_UNAVAILABLE, "equity:missing")
        self.assert_reason(plan(equity=D("NaN")), NoTradeReason.EQUITY_UNAVAILABLE, "equity:missing")
        self.assert_reason(plan(equity=D("0")), NoTradeReason.EQUITY_UNAVAILABLE, "equity:not_positive")
        self.assert_reason(plan(cash=None), NoTradeReason.EQUITY_UNAVAILABLE, "cash:missing")

    def test_invalid_levels(self):
        # ATR 60: stop = 100 - 120 < 0.
        self.assert_reason(plan(atr=D("60")), NoTradeReason.INVALID_LEVELS, "level_not_positive")

    def test_tick_rounding_invalidates_the_stop(self):
        # Tick 1, ask 1.5, ATR 0.3: stop 0.9 floors to 0.
        result = plan(bid=D("1.4"), ask=D("1.5"), atr=D("0.3"), entry=rules(tick_size="1"))
        self.assert_reason(result, NoTradeReason.STOP_INVALID_AFTER_TICK, "stop:0")
        self.assertEqual(result.levels.stop if result.levels else None, D("0.9"))

    def test_tick_rounding_invalidates_the_target(self):
        # Tick 1, ask 1.5, ATR 0.05: stop 1.4 -> 1, target 1.7 -> 1 <= ask.
        result = plan(bid=D("1.4"), ask=D("1.5"), atr=D("0.05"), entry=rules(tick_size="1"))
        self.assert_reason(result, NoTradeReason.INVALID_LEVELS, "target_not_above_entry:1")

    def test_tick_rounding_moves_the_stop_down(self):
        # Tick 0.5, ATR 1.1: stop 97.8 -> 97.5, target 104.4 -> 104.0; stressed 97.5 x 0.995 = 97.0125.
        result = plan(atr=D("1.1"), entry=rules(tick_size="0.5"))
        self.assertEqual((result.stop, result.target, result.stress_exit_price), (D("97.5"), D("104.0"), D("97.0125")))

    def test_budgets_exhausted(self):
        self.assert_reason(plan(open_planned_loss="1.20"), NoTradeReason.NO_LOSS_BUDGET, "remaining:0")
        self.assert_reason(plan(open_planned_loss="1.21"), NoTradeReason.NO_LOSS_BUDGET)
        self.assert_reason(plan(open_notional="24"), NoTradeReason.NO_NOTIONAL_ROOM, "remaining:0")
        self.assert_reason(plan(cash=D("24")), NoTradeReason.INSUFFICIENT_CASH, "room:0")
        self.assert_reason(plan(cash=D("-5")), NoTradeReason.INSUFFICIENT_CASH)

    def test_refusal_keeps_what_was_computed(self):
        result = plan(entry=rules(ordermin="1"))
        self.assertIsNotNone(result.candidates)
        self.assertEqual(result.loss_per_unit, D("3.003526"))
        self.assertEqual(result.fee_bps, FEE)

    def test_programming_errors_raise(self):
        with self.assertRaises(risk.RiskInputError):
            risk.plan_long_entry(
                envelope(), quote_currency="EUR", bid=D(1), ask=D(1), status=None, atr=D(1), pair_entry=rules(),
                equity=EQUITY, cash=EQUITY, open_planned_loss=D(0), open_notional=D(0), fee_bps=26.0,  # type: ignore[arg-type]
            )


class TestNoTradeReasons(unittest.TestCase):
    def test_stable_strings(self):
        self.assertEqual(
            {reason.value for reason in NoTradeReason},
            {
                "unsupported_direction", "envelope_changed", "kill_switch_engaged", "daily_loss_lock", "drawdown_lock",
                "position_already_open", "quote_currency_mismatch", "invalid_quote", "no_valid_atr", "missing_pair_rules",
                "equity_unavailable", "invalid_levels", "stop_invalid_after_tick", "no_loss_budget", "no_notional_room",
                "insufficient_cash", "below_order_minimum", "below_cost_minimum", "sizing_inconsistent",
            },
        )
        self.assertEqual({kind.value for kind in LockKind}, {"daily_loss", "drawdown"})
        self.assertEqual({kind.value for kind in BindingConstraint}, {"per_entry_loss", "aggregate_loss", "notional", "cash"})


class TestEquityAndLocks(unittest.TestCase):
    def test_cost_basis_and_conservative_mark(self):
        # 0.19 at 100: 19.00 + fee 0.0494 -> 0.05 = 19.05.
        basis = risk.entry_cost_basis(D("0.19"), D("100"), FEE)
        self.assertEqual(basis, D("19.05"))
        # Bid 99: proceeds 18.81, exit fee 0.048906 rounded UP -> 0.05; mark = 18.81 - 0.05 - 19.05 = -0.29.
        mark = risk.unrealized_mark(D("0.19"), D("99"), basis, FEE)
        self.assertEqual(mark, D("-0.29"))
        self.assertEqual(risk.account_equity(EQUITY, [D("-1.00")], [mark]), D("238.71"))
        self.assertEqual(risk.account_equity(EQUITY, [], []), EQUITY)
        self.assertEqual(risk.available_cash(EQUITY, [D("-1.00"), D("0.30")], [basis]), D("220.25"))

    def test_daily_lock_boundary(self):
        env = envelope()
        # 1% of 240.00 = 2.40: trips at 237.60 and below.
        self.assertTrue(risk.daily_lock_tripped(env, D("237.60"), EQUITY))
        self.assertTrue(risk.daily_lock_tripped(env, D("237.59"), EQUITY))
        self.assertFalse(risk.daily_lock_tripped(env, D("237.61"), EQUITY))
        # A stricter envelope trips earlier: 0.5% of 240 = 1.20.
        self.assertTrue(risk.daily_lock_tripped(envelope(daily_loss_pct=D("0.5")), D("238.80"), EQUITY))

    def test_drawdown_lock_boundary(self):
        env = envelope()
        # 250 x 0.97 = 242.50.
        high = D("250.00")
        self.assertTrue(risk.drawdown_lock_tripped(env, D("242.50"), high))
        self.assertTrue(risk.drawdown_lock_tripped(env, D("242.49"), high))
        self.assertFalse(risk.drawdown_lock_tripped(env, D("242.51"), high))
        # 240 x 0.97 = 232.80.
        self.assertTrue(risk.drawdown_lock_tripped(env, D("232.80"), EQUITY))
        self.assertFalse(risk.drawdown_lock_tripped(env, D("232.81"), EQUITY))

    def test_tripped_locks_report_only(self):
        env = envelope()
        self.assertEqual(risk.tripped_locks(env, D("240"), EQUITY, EQUITY), ())
        self.assertEqual(risk.tripped_locks(env, D("237.60"), EQUITY, EQUITY), (LockKind.DAILY_LOSS,))
        # Day start 235 (a loss of 0 today) but 3% under the 250 high-water.
        self.assertEqual(risk.tripped_locks(env, D("235"), D("235"), D("250")), (LockKind.DRAWDOWN,))
        self.assertEqual(risk.tripped_locks(env, D("232.80"), EQUITY, EQUITY), (LockKind.DAILY_LOSS, LockKind.DRAWDOWN))
        # Recovering equity does not un-trip anything here: there is no clearing function at all.
        self.assertFalse(any(name.startswith(("clear", "release", "reset")) for name in dir(risk)))

    def test_high_water_only_rises(self):
        self.assertEqual(risk.next_high_water(EQUITY, D("241.30")), D("241.30"))
        self.assertEqual(risk.next_high_water(D("241.30"), D("230")), D("241.30"))

    def test_bad_inputs_raise(self):
        with self.assertRaises(risk.RiskInputError):
            risk.daily_lock_tripped(envelope(), 237.6, EQUITY)  # type: ignore[arg-type]
        with self.assertRaises(risk.RiskInputError):
            risk.account_equity(EQUITY, [0.5], [])  # type: ignore[list-item]


class TestSettleLong(unittest.TestCase):
    def assert_settled(self, result: risk.Settlement, gross: str, entry_fee: str, exit_fee: str, net: str, outcome: Outcome):
        self.assertEqual((result.gross, result.entry_fee, result.exit_fee, result.net), (D(gross), D(entry_fee), D(exit_fee), D(net)))
        self.assertEqual(result.fees, result.entry_fee + result.exit_fee)
        self.assertEqual(result.gross - result.fees, result.net)
        self.assertEqual(result.outcome, outcome)

    def test_win_at_target(self):
        # 0.19 x (104 - 100) = 0.76; fees 0.0494 -> 0.05 and 0.19 x 104 x 0.0026 = 0.051376 -> 0.05; net 0.66.
        self.assert_settled(risk.settle_long(D("0.19"), D("100"), D("104"), FEE), "0.76", "0.05", "0.05", "0.66", Outcome.WIN)

    def test_loss_at_stressed_stop(self):
        # 0.19 x (97.51 - 100) = -0.4731 -> -0.47; exit fee 0.04816994 -> 0.05; net -0.57.
        self.assert_settled(risk.settle_long(D("0.19"), D("100"), D("97.51"), FEE), "-0.47", "0.05", "0.05", "-0.57", Outcome.LOSS)

    def test_flat_and_half_even(self):
        self.assert_settled(risk.settle_long(D("1"), D("100"), D("100"), D("0")), "0.00", "0.00", "0.00", "0.00", Outcome.FLAT)
        # 0.005 -> 0.00 and 0.015 -> 0.02 (half to even, like the game).
        self.assert_settled(risk.settle_long(D("1"), D("100"), D("100.005"), D("0")), "0.00", "0.00", "0.00", "0.00", Outcome.FLAT)
        self.assert_settled(risk.settle_long(D("1"), D("100"), D("100.015"), D("0")), "0.02", "0.00", "0.00", "0.02", Outcome.WIN)

    def test_bad_inputs_raise(self):
        for quantity in (D("0"), D("-1"), 1.0):
            with self.subTest(quantity=quantity), self.assertRaises(risk.RiskInputError):
                risk.settle_long(quantity, D("100"), D("101"), FEE)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
