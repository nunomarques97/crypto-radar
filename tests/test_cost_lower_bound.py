"""T051a / D64: fee-only round-trip cost lower bound (radar_v08/domain/costs.py, additive).

Pure domain: no I/O, no configuration. The expected numbers are computed by hand in the
comments; the property test compares the bound with the COMPLETE total that the T040
engine (``price_round_trip``) reports for arbitrary observed spreads and slippages.
"""

import ast
import dataclasses
import inspect
import os
import random
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.domain import costs as c  # noqa: E402
from radar_v08.domain.costs import (  # noqa: E402
    CostBoundError,
    CostBoundErrorCode,
    CostBoundKind,
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
    Leg,
    Missing,
    MissingReason,
    NotApplicableReason,
    ScenarioSize,
    Side,
    SizeProvenance,
    SlippageBasis,
    SlippageInput,
    SpreadConvention,
    SpreadInput,
    price_round_trip,
    round_trip_cost_lower_bound,
)
from radar_v08.domain.integrity import InstrumentKind  # noqa: E402

D = Decimal
NOTIONAL = D("1000")
SPOT = CostInstrument(kind=InstrumentKind.SPOT, symbol="XBTUSD", quote_currency="USD")
FUTURES = CostInstrument(kind=InstrumentKind.FUTURES, symbol="PF_XBTUSD", quote_currency="USD")
CONFIG_SOURCE = "radar_v08/config.py UNCALIBRATED_FEES['spot_taker_bps'] literal default"
NOT_OBSERVED = Missing(MissingReason.NOT_OBSERVED, "OHLCVT bars carry no order book")


def fee(bps: str = "26.0") -> FeeInput:
    return FeeInput(D(bps), FeeBasis.UNCALIBRATED_ASSUMPTION, CONFIG_SOURCE)


def unobserved(side=Side.LONG, convention=SpreadConvention.HALF_SPREAD_PLUS_TOUCH_SLIPPAGE, **overrides):
    values = dict(
        instrument=SPOT,
        side=side,
        size=ScenarioSize(NOTIONAL, SizeProvenance.HYPOTHETICAL_MANUAL),
        spread_convention=convention,
        spread=None if convention is SpreadConvention.SLIPPAGE_FROM_MID else NOT_OBSERVED,
        buy_slippage=NOT_OBSERVED,
        sell_slippage=NOT_OBSERVED,
        entry_fee=fee(),
        exit_fee=fee(),
        funding_intervals=0,
        funding=None,
    )
    values.update(overrides)
    return CostScenarioInput(**values)


def observed(side, convention, spread_bps, buy_bps, sell_bps, entry_fee, exit_fee):
    basis = SlippageBasis.FROM_MID if convention is SpreadConvention.SLIPPAGE_FROM_MID else SlippageBasis.FROM_TOUCH
    return CostScenarioInput(
        instrument=SPOT,
        side=side,
        size=ScenarioSize(NOTIONAL, SizeProvenance.HYPOTHETICAL_MANUAL),
        spread_convention=convention,
        spread=None if convention is SpreadConvention.SLIPPAGE_FROM_MID else SpreadInput(spread_bps, "hand"),
        buy_slippage=SlippageInput(buy_bps, basis, NOTIONAL, DepthCoverage.FULL, "hand"),
        sell_slippage=SlippageInput(sell_bps, basis, NOTIONAL, DepthCoverage.FULL, "hand"),
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        funding_intervals=0,
        funding=None,
    )


class TestLowerBound(unittest.TestCase):
    def test_two_uncalibrated_taker_legs_bound_the_round_trip_at_52_bps(self) -> None:
        for side in Side:
            for convention in SpreadConvention:
                with self.subTest(side=side, convention=convention):
                    bound = round_trip_cost_lower_bound(unobserved(side, convention))
                    # 26.0 bps per leg on the reference notional: 0.0026 + 0.0026 = 0.0052 = 52 bps.
                    self.assertEqual(bound.lower_bound_fraction, D("0.0052"))
                    self.assertEqual(bound.lower_bound_bps, D("52"))
                    self.assertEqual([line.fraction for line in bound.fee_lines], [D("0.0026"), D("0.0026")])
                    self.assertEqual([line.leg for line in bound.fee_lines], [Leg.ENTRY, Leg.EXIT])
                    self.assertIs(bound.kind, CostBoundKind.LOWER_BOUND)
                    self.assertTrue(bound.is_lower_bound)
                    self.assertIs(bound.status, CostStatus.INCOMPLETE)

    def test_result_has_no_total_and_lists_what_is_missing(self) -> None:
        names = {item.name for item in dataclasses.fields(c.CostLowerBound)}
        self.assertFalse([name for name in names if "total" in name or "cashflow" in name or "money" in name])
        self.assertEqual({member.value for member in CostBoundKind}, {"COST_LOWER_BOUND"})
        bound = round_trip_cost_lower_bound(unobserved())
        self.assertIsNot(bound.status, CostStatus.COMPLETE)
        self.assertEqual(
            [(line.component, line.leg, line.reason) for line in bound.missing],
            [
                (CostComponent.SPREAD, Leg.ENTRY, MissingReason.NOT_OBSERVED),
                (CostComponent.SPREAD, Leg.EXIT, MissingReason.NOT_OBSERVED),
                (CostComponent.SLIPPAGE, Leg.ENTRY, MissingReason.NOT_OBSERVED),
                (CostComponent.SLIPPAGE, Leg.EXIT, MissingReason.NOT_OBSERVED),
            ],
        )
        self.assertFalse([line for line in bound.fee_lines if line.component is not CostComponent.FEE])
        self.assertEqual(
            {line.reason for line in bound.not_applicable},
            {NotApplicableReason.SPOT_HAS_NO_FUNDING, NotApplicableReason.NO_MONEY_PROJECTION},
        )
        from_mid = round_trip_cost_lower_bound(unobserved(convention=SpreadConvention.SLIPPAGE_FROM_MID))
        self.assertEqual([line.component for line in from_mid.missing], [CostComponent.SLIPPAGE, CostComponent.SLIPPAGE])

    def test_fee_provenance_is_carried(self) -> None:
        bound = round_trip_cost_lower_bound(unobserved())
        self.assertEqual([item.basis for item in bound.fees], [FeeBasis.UNCALIBRATED_ASSUMPTION] * 2)
        self.assertEqual([item.bps for item in bound.fees], [D("26.0")] * 2)
        for line in bound.fee_lines:
            self.assertEqual(line.source, f"uncalibrated_assumption: {CONFIG_SOURCE}")

    def test_complete_total_is_never_below_the_bound(self) -> None:
        """Property: for any observed spread/slippage >= 0, both conventions and both sides."""
        generator = random.Random(20260919)
        extremes = [D("0"), D("0.01"), D("19999.99")]
        slip_extremes = [D("0"), D("0.01"), D("9999.99")]
        trials = 0
        for trial in range(1500):
            side = Side.LONG if trial % 2 == 0 else Side.SHORT
            convention = list(SpreadConvention)[(trial // 2) % 2]
            if trial < 36:
                spread = extremes[trial % 3]
                buy = slip_extremes[(trial // 3) % 3]
                sell = slip_extremes[(trial // 9) % 3]
            else:
                spread = D(generator.randrange(0, 2_000_000)) / 100
                buy = D(generator.randrange(0, 1_000_000)) / 100
                sell = D(generator.randrange(0, 1_000_000)) / 100
            fees = (fee(), fee()) if trial % 3 else (fee(str(D(generator.randrange(0, 999_999)) / 100)), fee("0"))
            complete = price_round_trip(observed(side, convention, spread, buy, sell, *fees))
            self.assertIs(complete.status, CostStatus.COMPLETE)
            bound = round_trip_cost_lower_bound(unobserved(side, convention, entry_fee=fees[0], exit_fee=fees[1]))
            self.assertGreaterEqual(complete.total_fraction, bound.lower_bound_fraction, (side, convention, spread, buy, sell))
            trials += 1
        self.assertEqual(trials, 1500)

    def test_zero_spread_and_slippage_equal_the_bound_exactly(self) -> None:
        # Spread 0, slippage 0: both fills at the mid, so the total is exactly the two fees.
        complete = price_round_trip(observed(Side.LONG, SpreadConvention.HALF_SPREAD_PLUS_TOUCH_SLIPPAGE, D(0), D(0), D(0), fee(), fee()))
        self.assertEqual(complete.total_fraction, D("0.0052"))

    def test_typed_errors(self) -> None:
        cases = {
            "missing entry fee": (unobserved(entry_fee=Missing(MissingReason.FEE_UNKNOWN)), CostBoundErrorCode.FEE_MISSING),
            "missing exit fee": (unobserved(exit_fee=Missing(MissingReason.FEE_UNKNOWN)), CostBoundErrorCode.FEE_MISSING),
            "negative fee": (unobserved(exit_fee=fee("-1")), CostBoundErrorCode.FEE_NEGATIVE),
            "observed spread": (unobserved(spread=SpreadInput(D(2), "book")), CostBoundErrorCode.COMPONENT_OBSERVED),
            "observed buy slippage": (
                unobserved(buy_slippage=SlippageInput(D(1), SlippageBasis.FROM_TOUCH, NOTIONAL, DepthCoverage.FULL, "b")),
                CostBoundErrorCode.COMPONENT_OBSERVED,
            ),
            "futures with funding": (
                unobserved(instrument=FUTURES, funding_intervals=3, funding=FundingInput(D(1), "venue")),
                CostBoundErrorCode.FUNDING_NOT_BOUNDED,
            ),
        }
        for name, (scenario, code) in cases.items():
            with self.subTest(name):
                with self.assertRaises(CostBoundError) as caught:
                    round_trip_cost_lower_bound(scenario)
                self.assertIs(caught.exception.code, code)
        with self.assertRaises(CostBoundError) as caught:
            round_trip_cost_lower_bound("scenario")  # type: ignore[arg-type]
        self.assertIs(caught.exception.code, CostBoundErrorCode.NOT_A_SCENARIO)
        with self.assertRaises(CostInputError) as caught_input:
            round_trip_cost_lower_bound(unobserved(funding=FundingInput(D(1), "venue")))
        self.assertIs(caught_input.exception.code, CostErrorCode.FUNDING_NOT_APPLICABLE)
        with self.assertRaises(CostInputError) as caught_input:
            round_trip_cost_lower_bound(
                unobserved(convention=SpreadConvention.SLIPPAGE_FROM_MID, spread=SpreadInput(D(2), "book"))
            )
        self.assertIs(caught_input.exception.code, CostErrorCode.DOUBLE_COUNTED_SPREAD)
        futures_no_funding = round_trip_cost_lower_bound(unobserved(instrument=FUTURES, funding_intervals=0))
        self.assertEqual(futures_no_funding.lower_bound_bps, D("52"))

    def test_existing_engine_behaviour_is_unchanged(self) -> None:
        # T040: without spread and slippage the engine still reports no line and no total.
        result = price_round_trip(unobserved())
        self.assertIs(result.status, CostStatus.INCOMPLETE)
        self.assertEqual(result.lines, ())
        self.assertIsNone(result.total_fraction)

    def test_function_is_pure(self) -> None:
        tree = ast.parse(inspect.getsource(round_trip_cost_lower_bound))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for token in ("os", "getenv", "environ", "open", "config", "socket", "requests", "print"):
            self.assertNotIn(token, names)


if __name__ == "__main__":
    unittest.main()
