"""Exact itemised round-trip cost scenarios (radar_v08/domain/costs.py).

Pure domain: no I/O, no database, no configuration. Golden values below are computed by
hand in the comments, never by re-running the implementation.
"""

import ast
import os
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.domain import costs as c
from radar_v08.domain.costs import (
    CostComponent,
    CostErrorCode,
    CostInputError,
    CostInstrument,
    CostScenarioInput,
    CostStatus,
    DepthCoverage,
    FeeBasis,
    FeeInput,
    FundingInput,
    FxRate,
    Leg,
    Missing,
    MissingReason,
    MoneyUnavailableReason,
    NotApplicableReason,
    ScenarioSize,
    Side,
    SizeProvenance,
    SlippageBasis,
    SlippageInput,
    SpreadConvention,
    SpreadInput,
    TradeDirection,
    present,
    price_round_trip,
)
from radar_v08.domain.integrity import InstrumentKind

D = Decimal
NOTIONAL = D("1000")
FUTURES_USD = CostInstrument(kind=InstrumentKind.FUTURES, symbol="PF_XBTUSD", quote_currency="USD")
SPOT_USD = CostInstrument(kind=InstrumentKind.SPOT, symbol="XBT/USD", quote_currency="USD")


def slip(bps, basis=SlippageBasis.FROM_TOUCH, notional=NOTIONAL, coverage=DepthCoverage.FULL):
    return SlippageInput(D(bps), basis, notional, coverage, "hand-built book walk")


def golden(side=Side.LONG, **overrides):
    """Hypothetical futures round trip, hand-computed in the tests below.

    N = 1000 USD, spread 20 bps (h = 0.001), buy slippage 10 bps from the ask, sell
    slippage 5 bps from the bid, entry fee 26 bps, exit fee 16 bps, funding 1 bps per
    interval for 3 intervals, reported in EUR at 0.9 EUR per USD.
    """
    fields = dict(
        instrument=FUTURES_USD,
        side=side,
        size=ScenarioSize(NOTIONAL, SizeProvenance.HYPOTHETICAL_MANUAL),
        spread_convention=SpreadConvention.HALF_SPREAD_PLUS_TOUCH_SLIPPAGE,
        spread=SpreadInput(D("20"), "hand-built book"),
        buy_slippage=slip("10"),
        sell_slippage=slip("5"),
        entry_fee=FeeInput(D("26"), FeeBasis.UNCALIBRATED_ASSUMPTION, "assumed taker"),
        exit_fee=FeeInput(D("16"), FeeBasis.UNCALIBRATED_ASSUMPTION, "assumed maker"),
        funding_intervals=3,
        funding=FundingInput(D("1"), "hand-declared verified rate"),
        report_currency="EUR",
        fx=FxRate("USD", "EUR", D("0.9"), "hand-declared rate"),
    )
    fields.update(overrides)
    return CostScenarioInput(**fields)


def line(scenario, component, leg):
    matches = [ln for ln in scenario.lines if ln.component is component and ln.leg is leg]
    assert len(matches) == 1, matches
    return matches[0]


class TestGoldenCashflow(unittest.TestCase):
    def test_long_round_trip_cashflow_by_hand(self):
        s = price_round_trip(golden(Side.LONG))
        self.assertIs(s.status, CostStatus.COMPLETE)
        cf = s.cashflow
        # Entry BUY fills at mid*(1+0.001)*(1+0.001) = 1.002001 mid -> pays 1002.001.
        self.assertEqual(cf.entry_trade.amount, D("-1002.001"))
        # Entry fee 0.0026 * 1002.001 = 2.6052026.
        self.assertEqual(cf.entry_fee.amount, D("-2.6052026"))
        # Exit SELL fills at mid*(1-0.001)*(1-0.0005) = 0.9985005 mid -> receives 998.5005.
        self.assertEqual(cf.exit_trade.amount, D("998.5005"))
        # Exit fee 0.0016 * 998.5005 = 1.5976008.
        self.assertEqual(cf.exit_fee.amount, D("-1.5976008"))
        # Positive funding: long pays 0.0001 * 3 * 1000 = 0.3.
        self.assertEqual(cf.funding.amount, D("-0.3"))
        # Net: -1002.001 - 2.6052026 + 998.5005 - 1.5976008 - 0.3 = -8.0033034.
        self.assertEqual(cf.net.amount, D("-8.0033034"))
        self.assertEqual(cf.net.currency, "USD")
        self.assertEqual(s.total_quote.amount, D("8.0033034"))
        self.assertEqual(s.total_quote.currency, "USD")
        self.assertEqual(s.total_bps, D("80.033034"))
        # 8.0033034 USD * 0.9 = 7.20297306 EUR.
        self.assertEqual(s.total_report.amount, D("7.20297306"))
        self.assertEqual(s.total_report.currency, "EUR")

    def test_long_itemised_lines_by_hand(self):
        s = price_round_trip(golden(Side.LONG))
        self.assertEqual(line(s, CostComponent.SPREAD, Leg.ENTRY).fraction, D("0.001"))
        # Slippage beyond the ask is charged on the ask: 1.001 * 0.001.
        self.assertEqual(line(s, CostComponent.SLIPPAGE, Leg.ENTRY).fraction, D("0.001001"))
        self.assertEqual(line(s, CostComponent.SPREAD, Leg.EXIT).fraction, D("0.001"))
        # Slippage below the bid on the bid: 0.999 * 0.0005.
        self.assertEqual(line(s, CostComponent.SLIPPAGE, Leg.EXIT).fraction, D("0.0004995"))
        self.assertEqual(line(s, CostComponent.FEE, Leg.ENTRY).fraction, D("0.0026052026"))
        self.assertEqual(line(s, CostComponent.FEE, Leg.EXIT).fraction, D("0.0015976008"))
        self.assertEqual(line(s, CostComponent.FUNDING, None).fraction, D("0.0003"))
        self.assertIs(line(s, CostComponent.SLIPPAGE, Leg.ENTRY).direction, TradeDirection.BUY)
        self.assertIs(line(s, CostComponent.SLIPPAGE, Leg.EXIT).direction, TradeDirection.SELL)

    def test_short_round_trip_cashflow_by_hand(self):
        s = price_round_trip(golden(Side.SHORT))
        self.assertIs(s.status, CostStatus.COMPLETE)
        cf = s.cashflow
        # Entry SELL at 0.9985005 mid -> receives 998.5005; fee 0.0026 * 998.5005 = 2.5961013.
        self.assertEqual(cf.entry_trade.amount, D("998.5005"))
        self.assertEqual(cf.entry_fee.amount, D("-2.5961013"))
        # Exit BUY at 1.002001 mid -> pays 1002.001; fee 0.0016 * 1002.001 = 1.6032016.
        self.assertEqual(cf.exit_trade.amount, D("-1002.001"))
        self.assertEqual(cf.exit_fee.amount, D("-1.6032016"))
        # Positive funding: short receives 0.3.
        self.assertEqual(cf.funding.amount, D("0.3"))
        # Net: 998.5005 - 2.5961013 - 1002.001 - 1.6032016 + 0.3 = -7.3998029.
        self.assertEqual(cf.net.amount, D("-7.3998029"))
        self.assertEqual(s.total_quote.amount, D("7.3998029"))
        self.assertEqual(s.total_bps, D("73.998029"))
        self.assertEqual(s.total_report.amount, D("6.65982261"))
        self.assertEqual(line(s, CostComponent.FUNDING, None).fraction, D("-0.0003"))
        self.assertIs(line(s, CostComponent.SLIPPAGE, Leg.ENTRY).direction, TradeDirection.SELL)
        self.assertIs(line(s, CostComponent.SLIPPAGE, Leg.EXIT).direction, TradeDirection.BUY)

    def test_itemised_lines_sum_exactly_to_the_cashflow(self):
        for side in Side:
            with self.subTest(side=side):
                s = price_round_trip(golden(side))
                items = sum((ln.fraction for ln in s.lines), D(0)) * NOTIONAL
                self.assertEqual(items, -s.cashflow.net.amount)
                self.assertEqual(s.total_fraction, sum((ln.fraction for ln in s.lines), D(0)))

    def test_both_legs_used_never_the_max_slippage(self):
        s = price_round_trip(golden(Side.LONG))
        slippage = [ln for ln in s.lines if ln.component is CostComponent.SLIPPAGE]
        self.assertEqual(len(slippage), 2)
        # 10.01 bps (buy leg) + 4.995 bps (sell leg); max(10, 5) twice would be 20.
        self.assertEqual(sum((ln.bps for ln in slippage), D(0)), D("15.005"))

    def test_leg_directions(self):
        self.assertIs(c.leg_direction(Side.LONG, Leg.ENTRY), TradeDirection.BUY)
        self.assertIs(c.leg_direction(Side.LONG, Leg.EXIT), TradeDirection.SELL)
        self.assertIs(c.leg_direction(Side.SHORT, Leg.ENTRY), TradeDirection.SELL)
        self.assertIs(c.leg_direction(Side.SHORT, Leg.EXIT), TradeDirection.BUY)


class TestSpreadConvention(unittest.TestCase):
    def test_spread_charged_once_half_per_leg(self):
        s = price_round_trip(golden())
        spread = [ln for ln in s.lines if ln.component is CostComponent.SPREAD]
        self.assertEqual([ln.leg for ln in spread], [Leg.ENTRY, Leg.EXIT])
        self.assertEqual(sum((ln.bps for ln in spread), D(0)), D("20"))  # the quoted spread, once

    def test_slippage_from_mid_gives_same_total_without_spread_line(self):
        # From the mid: buy 1.002001 - 1 = 20.01 bps; sell 1 - 0.9985005 = 14.995 bps.
        for side in Side:
            with self.subTest(side=side):
                from_mid = price_round_trip(
                    golden(
                        side,
                        spread_convention=SpreadConvention.SLIPPAGE_FROM_MID,
                        spread=None,
                        buy_slippage=slip("20.01", SlippageBasis.FROM_MID),
                        sell_slippage=slip("14.995", SlippageBasis.FROM_MID),
                    )
                )
                self.assertIs(from_mid.status, CostStatus.COMPLETE)
                self.assertEqual(from_mid.total_bps, price_round_trip(golden(side)).total_bps)
                self.assertFalse(any(ln.component is CostComponent.SPREAD for ln in from_mid.lines))
                reasons = {(n.component, n.reason) for n in from_mid.not_applicable}
                self.assertIn(
                    (CostComponent.SPREAD, NotApplicableReason.SPREAD_INCLUDED_IN_SLIPPAGE_FROM_MID), reasons
                )

    def test_spread_plus_slippage_from_mid_is_rejected_as_double_count(self):
        with self.assertRaises(CostInputError) as ctx:
            price_round_trip(
                golden(
                    spread_convention=SpreadConvention.SLIPPAGE_FROM_MID,
                    buy_slippage=slip("20.01", SlippageBasis.FROM_MID),
                    sell_slippage=slip("14.995", SlippageBasis.FROM_MID),
                )
            )
        self.assertIs(ctx.exception.code, CostErrorCode.DOUBLE_COUNTED_SPREAD)

    def test_slippage_basis_must_match_convention(self):
        with self.assertRaises(CostInputError) as ctx:
            price_round_trip(golden(buy_slippage=slip("20.01", SlippageBasis.FROM_MID)))
        self.assertIs(ctx.exception.code, CostErrorCode.CONVENTION_MISMATCH)


class TestMissingIsIncomplete(unittest.TestCase):
    CASES = {
        "spread_missing": dict(spread=Missing(MissingReason.NOT_OBSERVED)),
        "spread_absent": dict(spread=None),
        "buy_slippage_missing": dict(buy_slippage=Missing(MissingReason.NOT_OBSERVED)),
        "sell_slippage_missing": dict(sell_slippage=Missing(MissingReason.NOT_OBSERVED)),
        "buy_book_not_covering_size": dict(buy_slippage=slip("10", coverage=DepthCoverage.PARTIAL)),
        "sell_book_not_covering_size": dict(sell_slippage=slip("5", coverage=DepthCoverage.PARTIAL)),
        "entry_fee_missing": dict(entry_fee=Missing(MissingReason.FEE_UNKNOWN)),
        "exit_fee_missing": dict(exit_fee=Missing(MissingReason.FEE_UNKNOWN)),
        "funding_missing": dict(funding=Missing(MissingReason.FUNDING_SEMANTICS_UNVERIFIED)),
        "funding_absent": dict(funding=None),
        "fx_missing": dict(fx=Missing(MissingReason.FX_RATE_UNAVAILABLE)),
        "fx_absent": dict(fx=None),
    }

    def test_each_missing_component_makes_the_scenario_incomplete(self):
        for name, override in self.CASES.items():
            for side in Side:
                with self.subTest(case=name, side=side):
                    s = price_round_trip(golden(side, **override))
                    self.assertIs(s.status, CostStatus.INCOMPLETE)
                    self.assertEqual(s.status.value, "COST_INCOMPLETE")
                    self.assertTrue(s.missing)
                    self.assertIsNone(s.total_fraction)
                    self.assertIsNone(s.total_bps)
                    self.assertIsNone(s.total_quote)
                    self.assertIsNone(s.total_report)
                    self.assertIsNone(s.cashflow)
                    self.assertIs(s.money_unavailable, MoneyUnavailableReason.SCENARIO_INCOMPLETE)

    def test_missing_slippage_lands_on_the_leg_of_its_direction(self):
        missing_buy = dict(buy_slippage=Missing(MissingReason.NOT_OBSERVED))
        long_s = price_round_trip(golden(Side.LONG, **missing_buy))
        short_s = price_round_trip(golden(Side.SHORT, **missing_buy))
        self.assertEqual([(m.component, m.leg) for m in long_s.missing], [(CostComponent.SLIPPAGE, Leg.ENTRY)])
        self.assertEqual([(m.component, m.leg) for m in short_s.missing], [(CostComponent.SLIPPAGE, Leg.EXIT)])

    def test_partial_book_reason_is_size_not_covered(self):
        s = price_round_trip(golden(sell_slippage=slip("5", coverage=DepthCoverage.PARTIAL)))
        self.assertEqual([m.reason for m in s.missing], [MissingReason.SIZE_NOT_COVERED])

    def test_known_lines_are_kept_but_no_partial_total(self):
        s = price_round_trip(golden(exit_fee=Missing(MissingReason.FEE_UNKNOWN)))
        self.assertTrue(any(ln.component is CostComponent.SPREAD for ln in s.lines))
        self.assertIsNone(s.total_bps)

    def test_funding_not_applicable_for_spot_and_zero_intervals(self):
        spot = price_round_trip(golden(instrument=SPOT_USD, funding=None, funding_intervals=0))
        self.assertIs(spot.status, CostStatus.COMPLETE)
        self.assertIn(
            (CostComponent.FUNDING, NotApplicableReason.SPOT_HAS_NO_FUNDING),
            {(n.component, n.reason) for n in spot.not_applicable},
        )
        zero = price_round_trip(golden(funding=Missing(MissingReason.FUNDING_SEMANTICS_UNVERIFIED), funding_intervals=0))
        self.assertIs(zero.status, CostStatus.COMPLETE)
        self.assertIn(
            (CostComponent.FUNDING, NotApplicableReason.ZERO_FUNDING_INTERVALS),
            {(n.component, n.reason) for n in zero.not_applicable},
        )
        # Golden long minus the 3 bps of funding.
        self.assertEqual(zero.total_bps, D("77.033034"))

    def test_spot_with_funding_is_rejected(self):
        with self.assertRaises(CostInputError) as ctx:
            price_round_trip(golden(instrument=SPOT_USD))
        self.assertIs(ctx.exception.code, CostErrorCode.FUNDING_NOT_APPLICABLE)


class TestDecimalBoundary(unittest.TestCase):
    def assertRejected(self, build, code=CostErrorCode.NOT_DECIMAL):
        with self.assertRaises(CostInputError) as ctx:
            build()
        self.assertIs(ctx.exception.code, code)

    def test_float_rejected_everywhere(self):
        self.assertRejected(lambda: SpreadInput(20.0, "x"))
        self.assertRejected(lambda: SlippageInput(10.0, SlippageBasis.FROM_TOUCH, NOTIONAL, DepthCoverage.FULL, "x"))
        self.assertRejected(lambda: SlippageInput(D("1"), SlippageBasis.FROM_TOUCH, 1000.0, DepthCoverage.FULL, "x"))
        self.assertRejected(lambda: FeeInput(26.0, FeeBasis.UNCALIBRATED_ASSUMPTION, "x"))
        self.assertRejected(lambda: FundingInput(1.0, "x"))
        self.assertRejected(lambda: FxRate("USD", "EUR", 0.9, "x"))
        self.assertRejected(lambda: ScenarioSize(1000.0, SizeProvenance.HYPOTHETICAL_MANUAL))
        self.assertRejected(lambda: present(1.5, 2))
        # A bare float where a component object is expected is rejected, not read as missing.
        self.assertRejected(lambda: golden(spread=20.0))
        self.assertRejected(lambda: golden(funding=1.0))
        self.assertRejected(lambda: golden(fx=0.9))
        self.assertRejected(lambda: golden(buy_slippage=10.0))
        self.assertRejected(lambda: golden(entry_fee=26.0))

    def test_bool_and_int_rejected(self):
        self.assertRejected(lambda: SpreadInput(True, "x"))
        self.assertRejected(lambda: SpreadInput(20, "x"))
        self.assertRejected(lambda: ScenarioSize(1000, SizeProvenance.HYPOTHETICAL_MANUAL))
        self.assertRejected(lambda: golden(spread=True))
        self.assertRejected(lambda: golden(funding_intervals=True), CostErrorCode.OUT_OF_RANGE)

    def test_non_finite_rejected(self):
        for text in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(value=text):
                self.assertRejected(lambda: SpreadInput(D(text), "x"), CostErrorCode.NON_FINITE)
                self.assertRejected(lambda: FeeInput(D(text), FeeBasis.UNCALIBRATED_ASSUMPTION, "x"), CostErrorCode.NON_FINITE)

    def test_ranges_and_provenance(self):
        self.assertRejected(lambda: SpreadInput(D("-1"), "x"), CostErrorCode.OUT_OF_RANGE)
        self.assertRejected(lambda: SlippageInput(D("-0.1"), SlippageBasis.FROM_TOUCH, NOTIONAL, DepthCoverage.FULL, "x"), CostErrorCode.OUT_OF_RANGE)
        self.assertRejected(lambda: ScenarioSize(D("0"), SizeProvenance.HYPOTHETICAL_MANUAL), CostErrorCode.OUT_OF_RANGE)
        self.assertRejected(lambda: FxRate("USD", "EUR", D("0"), "x"), CostErrorCode.OUT_OF_RANGE)
        self.assertRejected(lambda: SpreadInput(D("1"), " "), CostErrorCode.EMPTY_FIELD)
        self.assertRejected(lambda: golden(funding_intervals=-1), CostErrorCode.OUT_OF_RANGE)

    def test_slippage_measured_at_another_size_is_rejected(self):
        with self.assertRaises(CostInputError) as ctx:
            price_round_trip(golden(buy_slippage=slip("10", notional=D("500"))))
        self.assertIs(ctx.exception.code, CostErrorCode.SIZE_MISMATCH)

    def test_fx_currency_mismatch_rejected(self):
        with self.assertRaises(CostInputError) as ctx:
            price_round_trip(golden(fx=FxRate("EUR", "USD", D("1.1"), "x")))
        self.assertIs(ctx.exception.code, CostErrorCode.CURRENCY_MISMATCH)

    def test_precision_is_never_rounded_silently(self):
        huge = D("1." + "3" * 150)
        with self.assertRaises(CostInputError) as ctx:
            price_round_trip(
                golden(
                    spread=SpreadInput(huge, "x"),
                    entry_fee=FeeInput(huge, FeeBasis.UNCALIBRATED_ASSUMPTION, "x"),
                )
            )
        self.assertIs(ctx.exception.code, CostErrorCode.PRECISION_EXCEEDED)

    def test_context_is_documented_and_traps_inexact(self):
        self.assertEqual(c.COST_CONTEXT.prec, 200)
        self.assertTrue(c.COST_CONTEXT.traps[c.Inexact])
        self.assertEqual(c.PRESENTATION_ROUNDING, c.ROUND_CEILING)

    def test_presentation_rounds_toward_more_cost(self):
        self.assertEqual(present(D("1.0001"), 3), D("1.001"))
        self.assertEqual(present(D("1.0000"), 3), D("1.000"))
        self.assertEqual(present(D("-1.0009"), 3), D("-1.000"))

    def test_frozen(self):
        s = price_round_trip(golden())
        with self.assertRaises(FrozenInstanceError):
            s.status = CostStatus.INCOMPLETE  # type: ignore[misc]


class TestUnitsAndProvenance(unittest.TestCase):
    def test_identity_travels_with_the_scenario(self):
        s = price_round_trip(golden(Side.SHORT))
        self.assertEqual(s.instrument, FUTURES_USD)
        self.assertIs(s.side, Side.SHORT)
        self.assertEqual(s.size.notional, NOTIONAL)
        self.assertIs(s.size.provenance, SizeProvenance.HYPOTHETICAL_MANUAL)
        self.assertIs(s.spread_convention, SpreadConvention.HALF_SPREAD_PLUS_TOUCH_SLIPPAGE)
        self.assertEqual(s.policy_version, "COST-1")
        self.assertEqual(s.funding_intervals, 3)

    def test_no_money_without_an_identified_quote_currency(self):
        unidentified = CostInstrument(kind=InstrumentKind.FUTURES, symbol=None, quote_currency=None)
        s = price_round_trip(golden(instrument=unidentified))
        self.assertIs(s.status, CostStatus.COMPLETE)
        self.assertEqual(s.total_bps, D("80.033034"))  # a ratio: currency cancels
        self.assertIsNone(s.total_quote)
        self.assertIsNone(s.cashflow)
        self.assertIs(s.money_unavailable, MoneyUnavailableReason.QUOTE_CURRENCY_UNIDENTIFIED)

    def test_report_in_quote_currency_needs_no_fx(self):
        s = price_round_trip(golden(report_currency="USD", fx=None))
        self.assertIs(s.status, CostStatus.COMPLETE)
        self.assertEqual(s.total_report.amount, D("8.0033034"))
        self.assertEqual(s.total_report.currency, "USD")

    def test_fees_never_calibrated(self):
        self.assertFalse(price_round_trip(golden()).fees_calibrated)
        self.assertEqual({b.name for b in FeeBasis}, {"UNCALIBRATED_ASSUMPTION", "PUBLISHED_SCHEDULE"})
        self.assertNotIn("ACCOUNT", {p.name for p in SizeProvenance})
        self.assertIn("uncalibrated_assumption", line(price_round_trip(golden()), CostComponent.FEE, Leg.ENTRY).source)


class TestPerturbation(unittest.TestCase):
    PERTURBATIONS = {
        "spread": dict(spread=SpreadInput(D("21"), "x")),
        "buy_slippage": dict(buy_slippage=slip("11")),
        "sell_slippage": dict(sell_slippage=slip("6")),
        "entry_fee": dict(entry_fee=FeeInput(D("27"), FeeBasis.UNCALIBRATED_ASSUMPTION, "x")),
        "exit_fee": dict(exit_fee=FeeInput(D("17"), FeeBasis.UNCALIBRATED_ASSUMPTION, "x")),
        "funding_rate": dict(funding=FundingInput(D("2"), "x")),
        "funding_intervals": dict(funding_intervals=4),
    }

    def test_each_component_changes_the_total(self):
        for side in Side:
            base = price_round_trip(golden(side)).total_bps
            for name, override in self.PERTURBATIONS.items():
                with self.subTest(side=side, component=name):
                    changed = price_round_trip(golden(side, **override)).total_bps
                    self.assertIsNotNone(changed)
                    self.assertNotEqual(changed, base)
                    if name.startswith("funding") and side is Side.SHORT:
                        self.assertLess(changed, base)  # short receives more positive funding
                    else:
                        self.assertGreater(changed, base)

    def test_size_changes_money_and_fx_changes_report(self):
        base = price_round_trip(golden())
        bigger = golden()
        bigger = replace(
            bigger,
            size=ScenarioSize(D("2000"), SizeProvenance.HYPOTHETICAL_MANUAL),
            buy_slippage=slip("10", notional=D("2000")),
            sell_slippage=slip("5", notional=D("2000")),
        )
        self.assertEqual(price_round_trip(bigger).total_quote.amount, D("16.0066068"))
        fx = price_round_trip(golden(fx=FxRate("USD", "EUR", D("0.8"), "x")))
        self.assertEqual(fx.total_report.amount, D("6.40264272"))
        self.assertNotEqual(fx.total_report.amount, base.total_report.amount)


class TestPurity(unittest.TestCase):
    def test_imports_only_stdlib_and_domain(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "radar_v08", "domain", "costs.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module)
        self.assertEqual(
            modules, {"__future__", "collections.abc", "dataclasses", "decimal", "enum", "radar_v08.domain.integrity"}
        )


if __name__ == "__main__":
    unittest.main()
