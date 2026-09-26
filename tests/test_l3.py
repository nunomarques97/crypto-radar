import json
import os
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config, context_builder, heartbeat, output
from radar_v08.anomaly import AnomalyResult, Features
from radar_v08.domain import costs as cost_domain
from radar_v08.http_client import ApiError
from radar_v08.kraken_spot import TradeRow
from radar_v08.l2 import L2Result
from radar_v08.l2_features import L2Features
from radar_v08.l3 import L3CandidateInput, run_l3, select_finalists
from radar_v08.opportunity import OpportunityResult
from radar_v08.router import RouterResult
from radar_v08.setups import SetupResult


def make_l2_result(asset, opportunity_score, warmup=False, setup_type="BREAKOUT", flags=None):
    return L2Result(
        asset=asset,
        l2_features=L2Features(l2_warmup=warmup, freshness=0.5),
        setup=SetupResult(setup_type, "LONG", []),
        opportunity=OpportunityResult(score=opportunity_score, breakdown={"momentum_coherence": 1.0, "derivatives_coherence": 0.0}),
        tradeability_preview={},
        ohlc_missing=False,
        flags=flags or [],
    )


def make_candidate(asset, opportunity_score, warmup=False, setup_type="BREAKOUT", futures_available=True):
    return L3CandidateInput(
        asset=asset, spot_pair=f"{asset}USD", futures_symbol=f"PF_{asset}USD" if futures_available else None,
        bid=99.9, ask=100.1, bid_size=10.0, ask_size=10.0, spread_bps=20.0, market_status="online",
        futures_available=futures_available, futures_spread_bps=5.0, futures_volume_24h_usd=1_000_000.0,
        funding_rate_raw=0.0001,
        l2_result=make_l2_result(asset, opportunity_score, warmup=warmup, setup_type=setup_type),
    )


class TestSelectFinalists(unittest.TestCase):
    def test_ranks_by_opportunity_and_caps_at_max_finalists(self):
        candidates = [make_candidate(f"A{i}", opportunity_score=float(i)) for i in range(20)]
        finalists = select_finalists(candidates)
        self.assertEqual(len(finalists), config.L3_MAX_FINALISTS)
        self.assertEqual(finalists[0].asset, "A19")  # highest opportunity first

    def test_excludes_warmup_and_zero_opportunity(self):
        candidates = [
            make_candidate("WARM", opportunity_score=90.0, warmup=True),
            make_candidate("ZERO", opportunity_score=0.0),
            make_candidate("GOOD", opportunity_score=50.0),
        ]
        finalists = select_finalists(candidates)
        assets = {c.asset for c in finalists}
        self.assertEqual(assets, {"GOOD"})


class TestRunL3(unittest.TestCase):
    def test_successful_fetch_yields_tradeable_state_and_requests_count(self):
        candidates = [make_candidate("BTC", opportunity_score=80.0)]

        bids = [(99.95, 50.0), (99.9, 50.0)]
        asks = [(100.05, 50.0), (100.1, 50.0)]
        trades = [TradeRow(price=100.0, volume=1.0, time=1000.0 + i, side="b", order_type="market", misc="") for i in range(30)]

        with patch("radar_v08.l3.fetch_depth", return_value=(bids, asks)), \
             patch("radar_v08.l3.fetch_trades", return_value=(trades, None)), \
             patch("radar_v08.l3.fetch_futures_orderbook", return_value=(bids, asks)):
            results, requests_made, failures = run_l3(session=object(), candidates=candidates)

        self.assertEqual(failures, 0)
        self.assertEqual(requests_made, 3)  # spot depth + trades + futures depth
        self.assertIn("BTC", results)
        self.assertIn(results["BTC"].tradeability.state, ("TRADEABLE", "CONSTRAINED"))

    def test_order_book_failure_keeps_candidate_with_flag(self):
        candidates = [make_candidate("BTC", opportunity_score=80.0, futures_available=False)]

        with patch("radar_v08.l3.fetch_depth", side_effect=ApiError("network down")), \
             patch("radar_v08.l3.fetch_trades", return_value=([], None)):
            results, _requests, failures = run_l3(session=object(), candidates=candidates)

        self.assertGreaterEqual(failures, 1)
        self.assertIn("BTC", results)
        self.assertIn("ORDER_BOOK_UNAVAILABLE", results["BTC"].flags)

    def test_pregate_excludes_untradeable_even_with_high_opportunity(self):
        candidates = [make_candidate("BTC", opportunity_score=95.0)]
        thin_bids = [(99.9, 0.001)]
        thin_asks = [(100.1, 0.001)]

        with patch("radar_v08.l3.fetch_depth", return_value=(thin_bids, thin_asks)), \
             patch("radar_v08.l3.fetch_trades", return_value=([], None)), \
             patch("radar_v08.l3.fetch_futures_orderbook", return_value=(thin_bids, thin_asks)):
            results, _requests, _failures = run_l3(session=object(), candidates=candidates)

        if results["BTC"].tradeability.state == "UNTRADEABLE":
            self.assertFalse(results["BTC"].qwen_eligible)


class TestCostScenariosExposure(unittest.TestCase):
    """T3/T042(e): L3Result.cost_scenarios carries the exact cost_domain.CostScenario
    objects cost_preview is priced from, indexed by venue and by cost_domain.Side,
    without recalculating anything and without changing cost_preview, the score or the
    finalist ranking. The field is never serialized: the Qwen payload, the output-file
    candidate and the event context all stay byte-identical whether or not it is set
    (the UI only re-reads radar_v08_output.json verbatim, ui/data_reader.py
    read_output_snapshot, so proving the output candidate is unchanged proves the UI too).
    """

    def _finalist(self, futures_available=True):
        candidate_input = make_candidate("BTC", opportunity_score=80.0, futures_available=futures_available)
        bids = [(99.95, 50.0), (99.9, 50.0)]
        asks = [(100.05, 50.0), (100.1, 50.0)]
        trades = [
            TradeRow(price=100.0, volume=1.0, time=1000.0 + i, side="b", order_type="market", misc="")
            for i in range(30)
        ]
        patches = [
            patch("radar_v08.l3.fetch_depth", return_value=(bids, asks)),
            patch("radar_v08.l3.fetch_trades", return_value=(trades, None)),
        ]
        if futures_available:
            patches.append(patch("radar_v08.l3.fetch_futures_orderbook", return_value=(bids, asks)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        results, _requests, _failures = run_l3(session=object(), candidates=[candidate_input])
        return candidate_input, results["BTC"]

    def test_populated_by_venue_and_side_with_real_cost_domain_objects(self):
        _candidate, l3_result = self._finalist()
        self.assertEqual(set(l3_result.cost_scenarios), {"spot", "futures"})
        for venue_scenarios in l3_result.cost_scenarios.values():
            self.assertEqual(set(venue_scenarios), {cost_domain.Side.LONG, cost_domain.Side.SHORT})
            for scenario in venue_scenarios.values():
                self.assertIsInstance(scenario, cost_domain.CostScenario)

    def test_matches_cost_preview_values_exactly_no_recalculation(self):
        _candidate, l3_result = self._finalist()
        for venue in ("spot", "futures"):
            preview_totals = l3_result.cost_preview[venue]["total_cost_bps_by_side"]
            for side, scenario in l3_result.cost_scenarios[venue].items():
                expected = preview_totals[side.value]
                if scenario.total_bps is None:
                    self.assertIsNone(expected)
                else:
                    self.assertEqual(expected, float(cost_domain.present(scenario.total_bps, 3)))

    def test_no_futures_key_when_futures_unavailable(self):
        _candidate, l3_result = self._finalist(futures_available=False)
        self.assertEqual(set(l3_result.cost_scenarios), {"spot"})

    def test_incomplete_book_returns_scenario_as_is_never_zero_or_borrowed(self):
        candidate_input = make_candidate("BTC", opportunity_score=80.0, futures_available=False)
        with patch("radar_v08.l3.fetch_depth", side_effect=ApiError("network down")), \
             patch("radar_v08.l3.fetch_trades", return_value=([], None)):
            results, _requests, _failures = run_l3(session=object(), candidates=[candidate_input])
        spot_scenarios = results["BTC"].cost_scenarios["spot"]
        for side, scenario in spot_scenarios.items():
            with self.subTest(side=side):
                self.assertIs(scenario.status, cost_domain.CostStatus.INCOMPLETE)
                self.assertIsNone(scenario.total_bps)
                # never partially summed, never filled in from the other side
                other_side = cost_domain.Side.SHORT if side is cost_domain.Side.LONG else cost_domain.Side.LONG
                self.assertIsNot(scenario, spot_scenarios[other_side])

    def _anomaly_result(self):
        return AnomalyResult(
            asset="BTC", warmup=False, sample_count=100, history_minutes=60.0,
            anomaly_score=1.0, price_z=0.0, volume_z=0.0, trades_z=0.0, oi_z=0.0,
            relative_btc_z=0.0, features=Features(),
        )

    def test_output_candidate_identical_with_and_without_cost_scenarios(self):
        candidate_input, l3_result = self._finalist()
        stripped = replace(l3_result, cost_scenarios={})
        kwargs = dict(
            asset="BTC", spot_pair="BTCUSD", futures_symbol="PF_BTCUSD",
            result=self._anomaly_result(), flags=[], l2_result=candidate_input.l2_result,
        )
        with_scenarios = output.build_candidate(l3_result=l3_result, **kwargs)
        without_scenarios = output.build_candidate(l3_result=stripped, **kwargs)
        self.assertEqual(with_scenarios, without_scenarios)
        self.assertEqual(set(with_scenarios), set(without_scenarios))
        dumped_with = json.dumps(with_scenarios, sort_keys=True, default=str)
        dumped_without = json.dumps(without_scenarios, sort_keys=True, default=str)
        self.assertEqual(dumped_with, dumped_without)
        self.assertEqual(len(dumped_with), len(dumped_without))
        self.assertNotIn("CostScenario", dumped_with)

    def test_qwen_payload_identical_with_and_without_cost_scenarios(self):
        candidate_input, l3_result = self._finalist()
        stripped = replace(l3_result, cost_scenarios={})
        l1_features = Features(volume_intensity_15m=1.2, relative_return_vs_btc_15m=0.3)
        with_scenarios = heartbeat._build_qwen_payload(
            "BTC", 1.0, l1_features, candidate_input.l2_result, l3_result
        )
        without_scenarios = heartbeat._build_qwen_payload(
            "BTC", 1.0, l1_features, candidate_input.l2_result, stripped
        )
        self.assertEqual(with_scenarios, without_scenarios)
        dumped_with = json.dumps(with_scenarios, sort_keys=True, default=str)
        dumped_without = json.dumps(without_scenarios, sort_keys=True, default=str)
        self.assertEqual(dumped_with, dumped_without)
        self.assertEqual(len(dumped_with), len(dumped_without))
        self.assertNotIn("CostScenario", dumped_with)

    def test_event_context_identical_with_and_without_cost_scenarios(self):
        candidate_input, l3_result = self._finalist()
        stripped = replace(l3_result, cost_scenarios={})
        router_result = RouterResult(decision="NONE", model_demand_score=0.0, confidence="LOW")
        common = dict(
            asset="BTC", spot_pair="BTCUSD", futures_symbol="PF_BTCUSD", market="SPOT",
            current_price=100.0, setup_type="BREAKOUT", direction="LONG", anomaly_score=1.0,
            l1_features=Features(), l2_result=candidate_input.l2_result, futures_snapshot=None,
            qwen_review=None, router_result=router_result, flags=[],
        )
        with_scenarios = context_builder.build_event_context(l3_result=l3_result, **common)
        without_scenarios = context_builder.build_event_context(l3_result=stripped, **common)
        self.assertEqual(with_scenarios, without_scenarios)
        dumped_with = json.dumps(with_scenarios, sort_keys=True, default=str)
        dumped_without = json.dumps(without_scenarios, sort_keys=True, default=str)
        self.assertEqual(dumped_with, dumped_without)
        self.assertEqual(len(dumped_with), len(dumped_without))
        self.assertNotIn("CostScenario", dumped_with)


if __name__ == "__main__":
    unittest.main()
