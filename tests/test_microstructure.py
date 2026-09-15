import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.kraken_spot import TradeRow
from radar_v08.microstructure import compute_depth_metrics, compute_trades_metrics


def book(bids, asks):
    return bids, asks


class TestDepth(unittest.TestCase):
    def test_depth_within_0_5_pct(self):
        # mid = 100. 0.5% band = [99.5, 100.5].
        bids = [(99.9, 10.0), (99.6, 5.0), (99.0, 100.0)]  # last one outside 0.5% band
        asks = [(100.1, 10.0), (100.4, 5.0), (101.0, 100.0)]
        metrics = compute_depth_metrics(bids, asks, reference_usd=250.0)
        self.assertIsNotNone(metrics)
        expected_bid = 99.9 * 10.0 + 99.6 * 5.0
        expected_ask = 100.1 * 10.0 + 100.4 * 5.0
        self.assertAlmostEqual(metrics.bid_depth_usd_0_5pct, expected_bid, places=4)
        self.assertAlmostEqual(metrics.ask_depth_usd_0_5pct, expected_ask, places=4)

    def test_depth_within_1_pct_includes_more(self):
        bids = [(99.9, 10.0), (99.2, 5.0)]  # 99.2 is within 1% (99.0) but not 0.5% (99.5)
        asks = [(100.1, 10.0), (100.8, 5.0)]
        metrics = compute_depth_metrics(bids, asks, reference_usd=250.0)
        self.assertGreater(metrics.bid_depth_usd_1pct, metrics.bid_depth_usd_0_5pct)
        self.assertGreater(metrics.ask_depth_usd_1pct, metrics.ask_depth_usd_0_5pct)

    def test_imbalance_sign_and_bounds(self):
        bids = [(99.9, 100.0)]  # heavy bid side
        asks = [(100.1, 1.0)]
        metrics = compute_depth_metrics(bids, asks, reference_usd=250.0)
        self.assertGreater(metrics.imbalance, 0.0)
        self.assertLessEqual(metrics.imbalance, 1.0)

        bids2 = [(99.9, 1.0)]
        asks2 = [(100.1, 100.0)]
        metrics2 = compute_depth_metrics(bids2, asks2, reference_usd=250.0)
        self.assertLess(metrics2.imbalance, 0.0)

    def test_slippage_estimate_for_reference_size(self):
        # Buying $250: best ask fills $100 at 100.1, needs another $150 at 101.0 (worse).
        bids = [(99.9, 10.0)]
        asks = [(100.1, 1.0), (101.0, 10.0)]
        metrics = compute_depth_metrics(bids, asks, reference_usd=250.0)
        self.assertIsNotNone(metrics.slippage_buy_bps)
        self.assertGreater(metrics.slippage_buy_bps, 0.0)
        self.assertTrue(metrics.depth_available_at_reference)

    def test_empty_book_returns_none(self):
        self.assertIsNone(compute_depth_metrics([], [], reference_usd=250.0))

    def test_thin_book_flagged_not_fabricated(self):
        bids = [(99.9, 0.001)]
        asks = [(100.1, 0.001)]
        metrics = compute_depth_metrics(bids, asks, reference_usd=250.0)
        self.assertFalse(metrics.depth_available_at_reference)
        self.assertEqual(metrics.quality, "THIN_BOOK")


def trade(price, volume, time_, side):
    return TradeRow(price=price, volume=volume, time=time_, side=side, order_type="market", misc="")


class TestTrades(unittest.TestCase):
    def test_trade_count_and_average_size(self):
        trades = [trade(100.0, 1.0, 1000.0, "b"), trade(100.0, 3.0, 1001.0, "s")]
        metrics = compute_trades_metrics(trades)
        self.assertEqual(metrics.trade_count, 2)
        self.assertAlmostEqual(metrics.avg_trade_size_base, 2.0)

    def test_taker_buy_sell_imbalance_is_approximate(self):
        trades = [trade(100.0, 3.0, 1000.0, "b"), trade(100.0, 1.0, 1001.0, "s")]
        metrics = compute_trades_metrics(trades)
        self.assertAlmostEqual(metrics.taker_buy_ratio, 0.75)
        self.assertEqual(metrics.classification, "APPROXIMATE")  # never claimed VERIFIED

    def test_trades_unavailable_when_empty(self):
        self.assertIsNone(compute_trades_metrics([]))


if __name__ == "__main__":
    unittest.main()
