import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.kraken_spot import TradeRow
from radar_v08.microstructure import compute_depth_metrics, compute_trades_metrics
from radar_v08.tradeability import build_cost_preview, compute_tradeability


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
            market="SPOT", spot_spread_bps=20.0, spot_depth=None,
            futures_available=False, futures_spread_bps=None, futures_depth=None,
            funding_rate_raw=None,
        )
        self.assertIn("net_move_required_pct", preview["spot"])
        self.assertGreater(preview["spot"]["net_move_required_pct"], 0.0)

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


if __name__ == "__main__":
    unittest.main()
