import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.l2_features import (
    compute_exhaustion,
    compute_freshness,
    compute_l2_features,
    normalize_return_by_atr,
)
from radar_v08.structure import Bar

T0 = datetime(2026, 9, 13, 0, 0, 0, tzinfo=timezone.utc)


def make_bar(i, open_, high, low, close, volume=100.0, trades=10):
    return Bar(
        bar_time=(T0 + timedelta(minutes=5 * i)).isoformat(),
        open=open_, high=high, low=low, close=close, vwap=close, volume=volume, trades=trades,
    )


def flat_bars(n, level=100.0, half_range=1.0, volume=100.0):
    return [make_bar(i, level, level + half_range, level - half_range, level, volume=volume) for i in range(n)]


class TestAtrNormalization(unittest.TestCase):
    def test_normalize_return_by_atr(self):
        # 4% return with ATR at 2% of price -> 2 ATRs of movement.
        self.assertAlmostEqual(normalize_return_by_atr(4.0, 2.0), 2.0)

    def test_normalize_return_none_when_atr_missing(self):
        self.assertIsNone(normalize_return_by_atr(4.0, None))
        self.assertIsNone(normalize_return_by_atr(None, 2.0))

    def test_compute_l2_features_produces_atr_normalized_returns(self):
        bars = flat_bars(config.L2_MIN_BARS + 20)
        f = compute_l2_features(
            bars=bars, current_last=100.0, vwap_today=100.0,
            l1_return_5m_pct=2.0, l1_return_15m_pct=4.0, l1_return_1h_pct=6.0, l1_return_4h_pct=6.0,
        )
        self.assertFalse(f.l2_warmup)
        self.assertIsNotNone(f.return_15m_atr)
        # A larger raw % return should map to a larger ATR-normalized value.
        self.assertGreater(f.return_15m_atr, f.return_5m_atr)


class TestFreshness(unittest.TestCase):
    def test_large_old_move_has_low_freshness(self):
        # +20% over 24h but only +0.1% in the last hour.
        freshness = compute_freshness(return_1h_pct=0.1, return_24h_pct=20.0)
        self.assertLess(freshness, 0.05)

    def test_fresh_move_has_high_freshness(self):
        # +3% in 1h that IS essentially the whole 24h move.
        freshness = compute_freshness(return_1h_pct=3.0, return_24h_pct=3.1)
        self.assertGreater(freshness, 0.9)

    def test_freshness_none_when_inputs_missing(self):
        self.assertIsNone(compute_freshness(None, 20.0))
        self.assertIsNone(compute_freshness(1.0, None))


class TestExhaustion(unittest.TestCase):
    def test_exhaustion_true_on_extreme_move_with_fading_volume(self):
        bars = flat_bars(config.L2_MIN_BARS + 10, volume=100.0)
        # Fade the volume on the most recent bars.
        bars[-1].volume = 10.0
        bars[-2].volume = 100.0
        bars[-3].volume = 100.0
        threshold = config.SETUP_THRESHOLDS["exhaustion_min_atr_1h"]
        detected = compute_exhaustion(bars, return_1h_atr=threshold + 1.0)
        self.assertTrue(detected)

    def test_no_exhaustion_when_move_is_small(self):
        bars = flat_bars(config.L2_MIN_BARS + 10)
        bars[-1].volume = 10.0
        detected = compute_exhaustion(bars, return_1h_atr=0.2)
        self.assertFalse(detected)

    def test_no_exhaustion_when_volume_not_fading(self):
        bars = flat_bars(config.L2_MIN_BARS + 10, volume=100.0)
        threshold = config.SETUP_THRESHOLDS["exhaustion_min_atr_1h"]
        detected = compute_exhaustion(bars, return_1h_atr=threshold + 1.0)
        self.assertFalse(detected)  # volume flat, not fading


class TestL2Warmup(unittest.TestCase):
    def test_warmup_true_with_insufficient_bars(self):
        bars = flat_bars(config.L2_MIN_BARS - 1)
        f = compute_l2_features(
            bars=bars, current_last=100.0, vwap_today=100.0,
            l1_return_5m_pct=1.0, l1_return_15m_pct=1.0, l1_return_1h_pct=1.0, l1_return_4h_pct=1.0,
        )
        self.assertTrue(f.l2_warmup)
        self.assertIsNone(f.atr_5m)
        self.assertIn("l2_warmup", f.flags)

    def test_warmup_false_with_enough_bars(self):
        bars = flat_bars(config.L2_MIN_BARS + 5)
        f = compute_l2_features(
            bars=bars, current_last=100.0, vwap_today=100.0,
            l1_return_5m_pct=1.0, l1_return_15m_pct=1.0, l1_return_1h_pct=1.0, l1_return_4h_pct=1.0,
        )
        self.assertFalse(f.l2_warmup)
        self.assertIsNotNone(f.atr_5m)


if __name__ == "__main__":
    unittest.main()
