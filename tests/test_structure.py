import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.structure import (
    Bar,
    breakout_distance_atr,
    breakout_state,
    higher_high,
    higher_low,
    high_low_over,
    lower_high,
    lower_low,
    range_expansion,
    rejection_state,
    resample_bars,
    wilder_atr,
)

T0 = datetime(2026, 9, 13, 0, 0, 0, tzinfo=timezone.utc)


def make_bar(i, open_, high, low, close, vwap=None, volume=100.0, trades=10):
    return Bar(
        bar_time=(T0 + timedelta(minutes=5 * i)).isoformat(),
        open=open_, high=high, low=low, close=close,
        vwap=vwap if vwap is not None else close, volume=volume, trades=trades,
    )


def flat_bars(n, level=100.0, half_range=1.0):
    """Constant range=2*half_range bars, closes equal to `level` (no gaps) -
    every true range should equal 2*half_range once warmed up."""
    return [make_bar(i, level, level + half_range, level - half_range, level) for i in range(n)]


class TestAtr(unittest.TestCase):
    def test_atr_matches_constant_true_range(self):
        bars = flat_bars(config.ATR_PERIOD + 5, level=100.0, half_range=1.0)
        atr = wilder_atr(bars, period=config.ATR_PERIOD)
        self.assertIsNotNone(atr)
        self.assertAlmostEqual(atr, 2.0, places=6)

    def test_atr_none_when_insufficient_bars(self):
        bars = flat_bars(config.ATR_PERIOD - 1)
        self.assertIsNone(wilder_atr(bars, period=config.ATR_PERIOD))

    def test_atr_reacts_to_a_wider_bar(self):
        bars = flat_bars(config.ATR_PERIOD + 5, half_range=1.0)
        wide_bar = make_bar(len(bars), 100.0, 110.0, 90.0, 100.0)
        bars_with_spike = bars + [wide_bar]
        atr_before = wilder_atr(bars, period=config.ATR_PERIOD)
        atr_after = wilder_atr(bars_with_spike, period=config.ATR_PERIOD)
        self.assertGreater(atr_after, atr_before)


class TestResample(unittest.TestCase):
    def test_resample_groups_bars_and_preserves_extremes(self):
        bars = [make_bar(i, 100, 100 + i, 100 - i, 100, volume=10) for i in range(24)]
        hourly = resample_bars(bars, group_size=12)
        self.assertEqual(len(hourly), 2)
        self.assertEqual(hourly[0].high, max(b.high for b in bars[:12]))
        self.assertEqual(hourly[0].low, min(b.low for b in bars[:12]))
        self.assertEqual(hourly[0].volume, sum(b.volume for b in bars[:12]))


class TestStructure4hAnd24h(unittest.TestCase):
    def test_4h_high_low(self):
        bars = flat_bars(config.STRUCTURE_4H_BARS, level=100.0, half_range=1.0)
        bars[10] = make_bar(10, 100, 150.0, 60.0, 100)  # an outlier inside the 4h window
        high, low = high_low_over(bars, config.STRUCTURE_4H_BARS)
        self.assertEqual(high, 150.0)
        self.assertEqual(low, 60.0)

    def test_24h_high_low_ignores_bars_outside_window(self):
        bars = flat_bars(config.STRUCTURE_24H_BARS + 10, level=100.0, half_range=1.0)
        bars[0] = make_bar(0, 100, 500.0, 1.0, 100)  # outside the last STRUCTURE_24H_BARS
        high, low = high_low_over(bars, config.STRUCTURE_24H_BARS)
        self.assertLess(high, 500.0)
        self.assertGreater(low, 1.0)


class TestBreakoutDetection(unittest.TestCase):
    def test_breakout_up(self):
        state = breakout_state(price=112.0, high_level=100.0, low_level=90.0, atr=2.0)
        self.assertEqual(state, "BREAKOUT_UP")

    def test_breakout_down(self):
        state = breakout_state(price=80.0, high_level=100.0, low_level=90.0, atr=2.0)
        self.assertEqual(state, "BREAKOUT_DOWN")

    def test_inside_range_is_not_a_breakout(self):
        state = breakout_state(price=95.0, high_level=100.0, low_level=90.0, atr=2.0)
        self.assertEqual(state, "INSIDE_RANGE")

    def test_breakout_distance_atr_sign_and_magnitude(self):
        self.assertAlmostEqual(breakout_distance_atr(106.0, 100.0, 90.0, 2.0), 3.0)
        self.assertAlmostEqual(breakout_distance_atr(84.0, 100.0, 90.0, 2.0), -3.0)
        self.assertEqual(breakout_distance_atr(95.0, 100.0, 90.0, 2.0), 0.0)


class TestTrendAndRejection(unittest.TestCase):
    def test_higher_high_and_higher_low(self):
        bars = [make_bar(i, 100, 100 + i, 90 + i, 100) for i in range(12)]
        self.assertTrue(higher_high(bars, count=12))
        self.assertTrue(higher_low(bars, count=12))
        self.assertFalse(lower_high(bars, count=12))
        self.assertFalse(lower_low(bars, count=12))

    def test_rejection_at_high(self):
        # Big upper wick, closes near the low of the bar's range.
        bar = make_bar(0, open_=100, high=110, low=99, close=100)
        self.assertEqual(rejection_state(bar), "REJECTION_AT_HIGH")

    def test_rejection_at_low(self):
        bar = make_bar(0, open_=100, high=101, low=90, close=100)
        self.assertEqual(rejection_state(bar), "REJECTION_AT_LOW")

    def test_no_rejection_on_a_trend_bar(self):
        # Opens near the low, closes near the high - small wicks both ends.
        bar = make_bar(0, open_=99.1, high=101, low=99, close=100.9)
        self.assertEqual(rejection_state(bar), "NONE")


class TestRangeExpansion(unittest.TestCase):
    def test_expansion_detected_when_bar_range_far_exceeds_atr(self):
        wide_bar = make_bar(0, 100, 110, 90, 100)  # range 20
        self.assertTrue(range_expansion(wide_bar, atr=2.0))

    def test_no_expansion_on_a_normal_bar(self):
        normal_bar = make_bar(0, 100, 101, 99, 100)  # range 2
        self.assertFalse(range_expansion(normal_bar, atr=2.0))


if __name__ == "__main__":
    unittest.main()
