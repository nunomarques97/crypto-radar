import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.anomaly import Features as L1Features
from radar_v08.http_client import ApiError
from radar_v08.kraken_spot import TradeRow
from radar_v08.l2 import L2Result
from radar_v08.l2_features import L2Features
from radar_v08.l3 import L3CandidateInput, run_l3, select_finalists
from radar_v08.opportunity import OpportunityResult
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


if __name__ == "__main__":
    unittest.main()
