import json
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.domain import costs as cost_domain
from radar_v08.domain.integrity import InstrumentKind
from radar_v08.kraken_spot import TradeRow
from radar_v08.microstructure import (
    DepthMetrics,
    compute_depth_metrics,
    compute_trades_metrics,
)
from radar_v08.tradeability import (
    build_cost_preview,
    build_cost_scenario_detail,
    compute_tradeability,
    venue_cost_scenarios,
)


def good_book():
    bids = [(99.95, 50.0), (99.9, 50.0)]
    asks = [(100.05, 50.0), (100.1, 50.0)]
    return compute_depth_metrics(bids, asks, reference_usd=250.0)


def good_trades():
    trades = [TradeRow(price=100.0, volume=1.0, time=1000.0 + i, side="b", order_type="market", misc="") for i in range(50)]
    return compute_trades_metrics(trades)


class TestTradeabilityScore(unittest.TestCase):
    def test_good_liquidity_scores_tradeable(self):
        result = compute_tradeability(
            spread_bps=10.0, depth=good_book(), trades=good_trades(),
            bid_usd_l0=5000.0, ask_usd_l0=5000.0, market_status="online",
            futures_available=True, futures_spread_bps=5.0, futures_volume_24h_usd=1_000_000.0,
            freshness=0.8,
        )
        self.assertEqual(result.state, "TRADEABLE")
        self.assertGreaterEqual(result.score, 0.0)
        self.assertLessEqual(result.score, 100.0)

    def test_untradeable_on_wide_spread_hard_veto(self):
        result = compute_tradeability(
            spread_bps=500.0, depth=good_book(), trades=good_trades(),
            bid_usd_l0=5000.0, ask_usd_l0=5000.0, market_status="online",
            futures_available=False, futures_spread_bps=None, futures_volume_24h_usd=None,
            freshness=0.5,
        )
        self.assertEqual(result.state, "UNTRADEABLE")

    def test_constrained_state_for_middling_liquidity(self):
        thin_bids = [(99.9, 0.02)]
        thin_asks = [(100.1, 0.02)]
        thin_depth = compute_depth_metrics(thin_bids, thin_asks, reference_usd=250.0)
        result = compute_tradeability(
            spread_bps=40.0, depth=thin_depth, trades=None,
            bid_usd_l0=200.0, ask_usd_l0=200.0, market_status="online",
            futures_available=False, futures_spread_bps=None, futures_volume_24h_usd=None,
            freshness=0.5,
        )
        self.assertIn(result.state, ("CONSTRAINED", "UNTRADEABLE"))
        self.assertIn("TRADES_UNAVAILABLE", result.flags)

    def test_missing_order_book_flags_but_keeps_candidate(self):
        result = compute_tradeability(
            spread_bps=15.0, depth=None, trades=good_trades(),
            bid_usd_l0=3000.0, ask_usd_l0=3000.0, market_status="online",
            futures_available=False, futures_spread_bps=None, futures_volume_24h_usd=None,
            freshness=0.5,
        )
        self.assertIn("ORDER_BOOK_UNAVAILABLE", result.flags)
        self.assertIsInstance(result.score, float)  # never crashes / never None

    def test_non_tradeable_status_forces_untradeable(self):
        result = compute_tradeability(
            spread_bps=10.0, depth=good_book(), trades=good_trades(),
            bid_usd_l0=5000.0, ask_usd_l0=5000.0, market_status="post_only",
            futures_available=False, futures_spread_bps=None, futures_volume_24h_usd=None,
            freshness=0.5,
        )
        self.assertEqual(result.state, "UNTRADEABLE")


class TestCostPreview(unittest.TestCase):
    def test_cost_preview_separates_spot_and_futures(self):
        preview = build_cost_preview(
            market="SPOT", spot_spread_bps=10.0, spot_depth=good_book(),
            futures_available=True, futures_spread_bps=5.0, futures_depth=good_book(),
            funding_rate_raw=-0.0001,
        )
        self.assertIn("spot", preview)
        self.assertIn("futures", preview)
        self.assertIsNotNone(preview["futures"])
        self.assertEqual(preview["spot"]["fee_status"], "UNCALIBRATED")

    def test_futures_absent_when_no_perpetual(self):
        preview = build_cost_preview(
            market="SPOT", spot_spread_bps=10.0, spot_depth=good_book(),
            futures_available=False, futures_spread_bps=None, futures_depth=None,
            funding_rate_raw=None,
        )
        self.assertIsNone(preview["futures"])

    def test_net_move_required_is_a_cost_amplitude_not_a_forecast(self):
        preview = build_cost_preview(
            market="SPOT", spot_spread_bps=10.0, spot_depth=good_book(),
            futures_available=False, futures_spread_bps=None, futures_depth=None,
            funding_rate_raw=None,
        )
        self.assertEqual(preview["spot"]["cost_status"], "COST_COMPLETE")
        self.assertGreater(preview["spot"]["net_move_required_pct"], 0.0)

    def test_missing_book_is_incomplete_never_zero(self):
        # RISK.md defect: this used to report total_cost_bps = 20 + 52 with spread and
        # slippage silently added as zero.
        preview = build_cost_preview(
            market="SPOT", spot_spread_bps=20.0, spot_depth=None,
            futures_available=False, futures_spread_bps=None, futures_depth=None,
            funding_rate_raw=None,
        )
        spot = preview["spot"]
        self.assertEqual(spot["cost_status"], "COST_INCOMPLETE")
        self.assertIsNone(spot["total_cost_bps"])
        self.assertIsNone(spot["net_move_required_pct"])
        self.assertIsNone(spot["slippage_bps"])
        self.assertEqual(spot["slippage_source"], "UNAVAILABLE")
        self.assertEqual(spot["spread_bps"], 20.0)  # the quoted spread is still shown
        self.assertEqual(
            spot["missing_components"],
            ["slippage:entry:not_observed", "slippage:exit:not_observed",
             "spread:entry:not_observed", "spread:exit:not_observed"],
        )
        self.assertEqual(spot["total_cost_bps_by_side"], {"long": None, "short": None})
        detail = build_cost_scenario_detail(spot_depth=None, futures_available=False, futures_depth=None)
        for side in ("long", "short"):
            self.assertIsNone(detail["spot"]["sides"][side]["total_bps"])

    def test_funding_raw_unverified_never_labeled_as_direction(self):
        preview = build_cost_preview(
            market="FUTURES", spot_spread_bps=10.0, spot_depth=None,
            futures_available=True, futures_spread_bps=5.0, futures_depth=None,
            funding_rate_raw=0.0005,
        )
        self.assertEqual(preview["futures"]["funding_semantics"], "RAW_UNVERIFIED")
        # The preview carries the raw number and nothing that could be mistaken
        # for a directional signal (no "bullish"/"bearish" label anywhere).
        self.assertNotIn("direction", preview["futures"])
        self.assertNotIn("bias", preview["futures"])


def exact_book(spread_bps=10.0, buy=8.0, sell=2.0, covered=True):
    return DepthMetrics(
        mid=100.0, spread_bps=spread_bps, bid_depth_usd_0_5pct=1.0, ask_depth_usd_0_5pct=1.0,
        bid_depth_usd_1pct=1.0, ask_depth_usd_1pct=1.0, imbalance=0.0,
        slippage_buy_bps=buy, slippage_sell_bps=sell,
        depth_available_at_reference=covered, quality="OK" if covered else "THIN_BOOK",
    )


FEES = {"spot_taker_bps": 26.0, "spot_maker_bps": 16.0, "futures_taker_bps": 5.0, "futures_maker_bps": 2.0}


@mock.patch.dict(config.UNCALIBRATED_FEES, FEES)
class TestCostPreviewDomainAdapter(unittest.TestCase):
    """T040: build_cost_preview priced by radar_v08.domain.costs (two legs, both sides)."""

    def preview(self, depth, futures_depth=None, futures=False, funding=None):
        return build_cost_preview(
            market="FUTURES" if futures else "SPOT", spot_spread_bps=12.0, spot_depth=depth,
            futures_available=futures, futures_spread_bps=4.0 if futures else None,
            futures_depth=futures_depth, funding_rate_raw=funding,
        )

    def detail(self, depth, futures_depth=None, futures=False):
        return build_cost_scenario_detail(
            spot_depth=depth, futures_available=futures, futures_depth=futures_depth,
        )

    def test_two_leg_total_by_hand_not_max_slippage(self):
        spot = self.preview(exact_book())["spot"]
        # h = 0.0005. Long: buy fills 1.0005*1.0008 = 1.0013004, sell 0.9995*0.9998 = 0.9993001.
        # spread 5+5, slippage 1.0005*8 = 8.004 and 0.9995*2 = 1.999, fees
        # 26*1.0013004 = 26.0338104 and 26*0.9993001 = 25.9818026 -> 72.018613 bps.
        # The short side mirrors the legs with equal taker fees: same total.
        detail = self.detail(exact_book())["spot"]
        for side in ("long", "short"):
            self.assertEqual(detail["sides"][side]["total_bps"], "72.018613")
            self.assertEqual(detail["sides"][side]["total_bps_presented"], "72.019")
        self.assertEqual(spot["total_cost_bps_by_side"], {"long": 72.019, "short": 72.019})
        self.assertEqual(spot["cost_status"], "COST_COMPLETE")
        self.assertEqual(spot["total_cost_bps"], 72.019)  # ROUND_CEILING to 3 places
        self.assertEqual(spot["net_move_required_pct"], 0.7202)  # ROUND_CEILING to 4 places
        self.assertEqual(spot["slippage_bps"], 10.003)  # both legs, not max(8, 2)
        self.assertEqual(spot["slippage_buy_bps"], 8.0)
        self.assertEqual(spot["slippage_sell_bps"], 2.0)
        self.assertEqual(spot["fee_bps"], 52.0)
        self.assertEqual(spot["spread_bps"], 12.0)  # the caller's quoted spread, unchanged
        # Legacy defect value: 12 + 52 + max(8, 2) = 72.0 with a single slippage leg.
        self.assertNotEqual(spot["total_cost_bps"], 72.0)

    def test_scenario_itemisation_and_labels(self):
        scenario = self.detail(exact_book())["spot"]
        self.assertEqual(scenario["policy_version"], "COST-1")
        self.assertEqual(scenario["spread_convention"], "half_spread_plus_touch_slippage")
        self.assertEqual(scenario["size"]["provenance"], "reference_config")
        self.assertEqual(scenario["size"]["notional"], "250")
        self.assertEqual(scenario["instrument"], {"kind": "spot", "symbol": None, "quote_currency": None})
        self.assertFalse(scenario["fees_calibrated"])
        lines = {(ln["component"], ln["leg"]): ln["bps"] for ln in scenario["sides"]["long"]["lines"]}
        self.assertEqual(lines[("spread", "entry")], "5")
        self.assertEqual(lines[("spread", "exit")], "5")
        self.assertEqual(lines[("slippage", "entry")], "8.004")
        self.assertEqual(lines[("slippage", "exit")], "1.999")
        self.assertEqual(lines[("fee", "entry")], "26.0338104")
        self.assertEqual(lines[("fee", "exit")], "25.9818026")
        presented = {(ln["component"], ln["leg"]): ln["bps_presented"] for ln in scenario["sides"]["long"]["lines"]}
        self.assertEqual(presented[("fee", "entry")], "26.034")  # ROUND_CEILING, toward more cost
        self.assertEqual(presented[("fee", "exit")], "25.982")
        self.assertEqual(presented[("slippage", "exit")], "1.999")
        json.dumps(scenario)  # JSON-safe

    def test_no_float_inside_the_exact_scenario(self):
        def walk(value):
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            else:
                self.assertNotIsInstance(value, float)

        walk(self.detail(exact_book(), exact_book(), futures=True)["futures"])

    def test_each_missing_book_input_is_incomplete(self):
        cases = {
            "no_spread": (exact_book(spread_bps=None), "spread:entry:not_observed"),
            "no_buy_slippage": (exact_book(buy=None), "slippage:entry:not_observed"),
            "no_sell_slippage": (exact_book(sell=None), "slippage:exit:not_observed"),
            "book_not_covering_size": (exact_book(covered=False), "slippage:entry:size_not_covered"),
            "inconsistent_negative_slippage": (exact_book(sell=-0.5), "slippage:exit:not_observed"),
        }
        for name, (depth, expected) in cases.items():
            with self.subTest(case=name):
                spot = self.preview(depth)["spot"]
                self.assertEqual(spot["cost_status"], "COST_INCOMPLETE")
                self.assertIsNone(spot["total_cost_bps"])
                self.assertIsNone(spot["net_move_required_pct"])
                self.assertIn(expected, spot["missing_components"])

    def test_float_noise_slippage_is_zero_not_missing(self):
        spot = self.preview(exact_book(sell=-2.220446049250313e-12))["spot"]
        self.assertEqual(spot["cost_status"], "COST_COMPLETE")
        detail = self.detail(exact_book(sell=-2.220446049250313e-12))["spot"]
        lines = {(ln["component"], ln["leg"]): ln["bps"] for ln in detail["sides"]["long"]["lines"]}
        self.assertEqual(lines[("slippage", "exit")], "0")

    def test_futures_funding_raw_is_never_costed(self):
        with_funding = self.preview(exact_book(), exact_book(), futures=True, funding=0.0005)["futures"]
        without = self.preview(exact_book(), exact_book(), futures=True, funding=None)["futures"]
        self.assertEqual(with_funding["funding_raw"], 0.0005)
        self.assertEqual(with_funding["funding_semantics"], "RAW_UNVERIFIED")
        self.assertEqual(with_funding["total_cost_bps"], without["total_cost_bps"])
        detail = self.detail(exact_book(), exact_book(), futures=True)["futures"]
        self.assertEqual(detail["funding_intervals"], 0)
        long_side = detail["sides"]["long"]
        reasons = {(n["component"], n["reason"]) for n in long_side["not_applicable"]}
        self.assertIn(("funding", "zero_funding_intervals"), reasons)
        # Futures taker 5 bps per leg: 10 + 8.004 + 1.999 + 5.006502 + 4.9965005 = 30.0060025.
        self.assertEqual(long_side["total_bps"], "30.0060025")
        self.assertEqual(with_funding["total_cost_bps"], 30.007)

    def test_futures_without_book_is_incomplete(self):
        futures = self.preview(exact_book(), None, futures=True)["futures"]
        self.assertEqual(futures["cost_status"], "COST_INCOMPLETE")
        self.assertIsNone(futures["total_cost_bps"])

    def test_cost_preview_stays_compact_for_the_qwen_payload(self):
        # cost_preview reaches every finalist of the live local Qwen payload
        # (heartbeat._build_qwen_payload). HEAD before T040: 237 bytes spot, 479 spot+futures;
        # T040 attempt 1 with the itemised scenario inside: 3332 / 6678 bytes.
        awkward = DepthMetrics(
            mid=1.2345, spread_bps=7.123456789, bid_depth_usd_0_5pct=1.0, ask_depth_usd_0_5pct=1.0,
            bid_depth_usd_1pct=1.0, ask_depth_usd_1pct=1.0, imbalance=0.0,
            slippage_buy_bps=3.14159265, slippage_sell_bps=2.7182818,
            depth_available_at_reference=True, quality="OK",
        )
        cases = {
            "spot_complete": (build_cost_preview(
                market="SPOT", spot_spread_bps=7.123456789, spot_depth=awkward, futures_available=False,
                futures_spread_bps=None, futures_depth=None, funding_rate_raw=None), 512),
            "spot_futures_complete": (self.preview(awkward, awkward, futures=True, funding=0.0001), 1024),
            "spot_futures_no_books": (self.preview(None, None, futures=True, funding=0.0001), 1200),
        }
        allowed = {
            "spread_bps", "fee_bps", "slippage_bps", "slippage_buy_bps", "slippage_sell_bps",
            "slippage_source", "total_cost_bps", "net_move_required_pct", "fee_status",
            "cost_status", "missing_components", "total_cost_bps_by_side",
        }
        for name, (preview, limit) in cases.items():
            with self.subTest(case=name):
                self.assertLessEqual(len(json.dumps(preview)), limit)
                for venue in ("spot", "futures"):
                    block = preview[venue]
                    if block is None:
                        continue
                    extra = {"funding_raw", "funding_semantics"} if venue == "futures" else set()
                    self.assertEqual(set(block), allowed | extra)
                    for value in (block["total_cost_bps"], block["net_move_required_pct"],
                                  *block["total_cost_bps_by_side"].values()):
                        if value is not None:
                            self.assertLessEqual(len(repr(value)), 8)  # presented, not exact

    def test_detail_is_never_inside_cost_preview(self):
        preview = self.preview(exact_book(), exact_book(), futures=True)
        text = json.dumps(preview)
        for marker in ("cost_scenario", "\"sides\"", "\"source\"", "\"lines\"", "legacy float"):
            self.assertNotIn(marker, text)
        self.assertEqual(preview["spot"]["total_cost_bps"], 72.019)
        self.assertEqual(self.detail(exact_book())["spot"]["sides"]["long"]["total_bps"], "72.018613")

    def test_tradeability_score_unchanged_by_cost_adapter(self):
        # The score keeps its own slippage credit (worst of buy/sell); only the cost preview changed.
        result = compute_tradeability(
            spread_bps=10.0, depth=exact_book(), trades=None,
            bid_usd_l0=5000.0, ask_usd_l0=5000.0, market_status="online",
            futures_available=False, futures_spread_bps=None, futures_volume_24h_usd=None,
            freshness=0.5,
        )
        expected = max(0.0, min(1.0, 1.0 - 8.0 / (config.TRADEABILITY_SLIPPAGE_TARGET_BPS * 2.0)))
        self.assertEqual(result.breakdown["slippage"], round(expected, 4))


@mock.patch.dict(config.UNCALIBRATED_FEES, FEES)
class TestVenueCostScenariosExposure(unittest.TestCase):
    """T3/T042(e): venue_cost_scenarios is the public accessor to the exact
    cost_domain.CostScenario objects _venue_scenarios builds for build_cost_preview -
    same values, nothing recalculated, and no change to build_cost_preview's own
    signature or dict shape."""

    def test_same_two_sides_as_the_private_builder(self):
        scenarios = venue_cost_scenarios(InstrumentKind.SPOT, exact_book(), 26.0)
        self.assertEqual(set(scenarios), {cost_domain.Side.LONG, cost_domain.Side.SHORT})
        for scenario in scenarios.values():
            self.assertIsInstance(scenario, cost_domain.CostScenario)

    def test_totals_match_build_cost_preview_by_hand_value(self):
        # Same golden numbers as test_two_leg_total_by_hand_not_max_slippage.
        scenarios = venue_cost_scenarios(InstrumentKind.SPOT, exact_book(), 26.0)
        for side in (cost_domain.Side.LONG, cost_domain.Side.SHORT):
            self.assertEqual(scenarios[side].total_bps, Decimal("72.018613"))
            self.assertEqual(float(cost_domain.present(scenarios[side].total_bps, 3)), 72.019)
        preview = build_cost_preview(
            market="SPOT", spot_spread_bps=12.0, spot_depth=exact_book(),
            futures_available=False, futures_spread_bps=None, futures_depth=None,
            funding_rate_raw=None,
        )
        self.assertEqual(preview["spot"]["total_cost_bps_by_side"], {"long": 72.019, "short": 72.019})

    def test_missing_book_is_incomplete_as_is_never_summed_or_borrowed(self):
        scenarios = venue_cost_scenarios(InstrumentKind.SPOT, None, 26.0)
        for side, scenario in scenarios.items():
            with self.subTest(side=side):
                self.assertIs(scenario.status, cost_domain.CostStatus.INCOMPLETE)
                self.assertIsNone(scenario.total_bps)

    def test_calling_the_public_accessor_does_not_change_build_cost_preview(self):
        depth = exact_book()
        before = build_cost_preview(
            market="SPOT", spot_spread_bps=12.0, spot_depth=depth, futures_available=False,
            futures_spread_bps=None, futures_depth=None, funding_rate_raw=None,
        )
        venue_cost_scenarios(InstrumentKind.SPOT, depth, 26.0)
        venue_cost_scenarios(InstrumentKind.FUTURES, depth, 5.0)
        after = build_cost_preview(
            market="SPOT", spot_spread_bps=12.0, spot_depth=depth, futures_available=False,
            futures_spread_bps=None, futures_depth=None, funding_rate_raw=None,
        )
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
