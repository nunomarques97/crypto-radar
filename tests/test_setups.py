import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.anomaly import Features as L1Features
from radar_v08.l2_features import L2Features
from radar_v08.setups import classify_setup


def l1(**kwargs) -> L1Features:
    defaults = dict(volume_intensity_15m=1.0)
    defaults.update(kwargs)
    return L1Features(**defaults)


def l2(**kwargs) -> L2Features:
    defaults = dict(
        l2_warmup=False, return_5m_atr=0.0, return_15m_atr=0.0, return_1h_atr=0.0,
        exhaustion=False, breakout_state="INSIDE_RANGE", rejection_state="NONE",
        range_compression=False, range_expansion=False, volatility_percentile=50.0,
    )
    defaults.update(kwargs)
    return L2Features(**defaults)


class TestWarmup(unittest.TestCase):
    def test_l2_warmup_forces_none(self):
        result = classify_setup(l1(), l2(l2_warmup=True))
        self.assertEqual(result.setup_type, "NONE")
        self.assertEqual(result.direction, "NONE")


class TestContinuation(unittest.TestCase):
    def test_coherent_upward_momentum_is_continuation_long(self):
        result = classify_setup(
            l1(volume_intensity_15m=1.0),
            l2(return_5m_atr=0.8, return_15m_atr=1.0, return_1h_atr=1.5),
        )
        self.assertEqual(result.setup_type, "CONTINUATION")
        self.assertEqual(result.direction, "LONG")

    def test_coherent_downward_momentum_is_continuation_short(self):
        result = classify_setup(
            l1(volume_intensity_15m=1.0),
            l2(return_5m_atr=-0.8, return_15m_atr=-1.0, return_1h_atr=-1.5),
        )
        self.assertEqual(result.setup_type, "CONTINUATION")
        self.assertEqual(result.direction, "SHORT")

    def test_incoherent_signs_is_not_continuation(self):
        result = classify_setup(
            l1(volume_intensity_15m=1.0),
            l2(return_5m_atr=0.8, return_15m_atr=-1.0, return_1h_atr=1.5),
        )
        self.assertNotEqual(result.setup_type, "CONTINUATION")


class TestBreakout(unittest.TestCase):
    def test_breakout_up_with_volume_and_expansion(self):
        result = classify_setup(
            l1(volume_intensity_15m=2.0),
            l2(
                return_5m_atr=1.0, return_15m_atr=1.0, return_1h_atr=1.0,
                breakout_state="BREAKOUT_UP", range_expansion=True,
            ),
        )
        self.assertEqual(result.setup_type, "BREAKOUT")
        self.assertEqual(result.direction, "LONG")

    def test_breakout_state_without_volume_confirmation_is_not_breakout(self):
        result = classify_setup(
            l1(volume_intensity_15m=1.0),  # below the confirm threshold
            l2(breakout_state="BREAKOUT_UP", range_expansion=True),
        )
        self.assertNotEqual(result.setup_type, "BREAKOUT")


class TestReversal(unittest.TestCase):
    def test_divergence_at_extreme_with_volume_is_reversal(self):
        result = classify_setup(
            l1(volume_intensity_15m=2.0),
            l2(
                return_15m_atr=-1.0, return_1h_atr=1.0,  # divergent signs
                rejection_state="REJECTION_AT_HIGH",
            ),
        )
        self.assertEqual(result.setup_type, "REVERSAL")
        self.assertEqual(result.direction, "SHORT")

    def test_divergence_without_rejection_is_not_reversal(self):
        result = classify_setup(
            l1(volume_intensity_15m=2.0),
            l2(return_15m_atr=-1.0, return_1h_atr=1.0, rejection_state="NONE"),
        )
        self.assertNotEqual(result.setup_type, "REVERSAL")


class TestSqueezeRelease(unittest.TestCase):
    def test_prior_compression_plus_expansion_plus_volume_is_squeeze_release(self):
        result = classify_setup(
            l1(volume_intensity_15m=2.0),
            l2(return_15m_atr=1.0, return_1h_atr=1.0, range_compression=True, range_expansion=True),
        )
        self.assertEqual(result.setup_type, "SQUEEZE_RELEASE")
        self.assertEqual(result.direction, "LONG")

    def test_compression_alone_without_expansion_is_not_squeeze_release(self):
        result = classify_setup(
            l1(volume_intensity_15m=2.0),
            l2(range_compression=True, range_expansion=False),
        )
        self.assertNotEqual(result.setup_type, "SQUEEZE_RELEASE")


class TestExhaustion(unittest.TestCase):
    def test_exhaustion_alone_has_none_direction(self):
        result = classify_setup(l1(volume_intensity_15m=1.0), l2(exhaustion=True))
        self.assertEqual(result.setup_type, "EXHAUSTION")
        self.assertEqual(result.direction, "NONE")

    def test_exhaustion_with_reversal_confirmation_becomes_reversal(self):
        result = classify_setup(
            l1(volume_intensity_15m=2.0),
            l2(
                exhaustion=True, return_15m_atr=-1.0, return_1h_atr=1.0,
                rejection_state="REJECTION_AT_HIGH",
            ),
        )
        self.assertEqual(result.setup_type, "REVERSAL")
        self.assertIn("exhaustion_with_reversal_confirmation", result.notes)


class TestNone(unittest.TestCase):
    def test_flat_market_is_none(self):
        result = classify_setup(l1(), l2())
        self.assertEqual(result.setup_type, "NONE")
        self.assertEqual(result.direction, "NONE")


if __name__ == "__main__":
    unittest.main()
