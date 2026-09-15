import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.anomaly import Features as L1Features
from radar_v08.l2_features import L2Features
from radar_v08.opportunity import (
    compute_opportunity_score,
    momentum_coherence_credit,
    volume_confirmed_credit,
)
from radar_v08.setups import SetupResult, classify_setup


def l1(**kwargs) -> L1Features:
    defaults = dict(volume_intensity_15m=1.0, return_15m=1.0)
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


class TestOpportunityScoreRange(unittest.TestCase):
    def test_warmup_gives_zero(self):
        result = compute_opportunity_score(l1(), l2(l2_warmup=True), SetupResult("NONE", "NONE", []), "SPOT", 10.0)
        self.assertEqual(result.score, 0.0)

    def test_score_always_within_0_100(self):
        strong_l2 = l2(
            return_5m_atr=3.0, return_15m_atr=3.0, return_1h_atr=3.0,
            breakout_state="BREAKOUT_UP", range_expansion=True,
            breakout_dist_4h_atr=2.0, breakout_dist_24h_atr=2.0, freshness=1.0,
        )
        strong_l1 = l1(volume_intensity_15m=5.0, relative_return_vs_btc_15m=10.0, futures_oi_delta_1h=100.0)
        setup = classify_setup(strong_l1, strong_l2)
        result = compute_opportunity_score(strong_l1, strong_l2, setup, "FUTURES", 1.0)
        self.assertGreaterEqual(result.score, 0.0)
        self.assertLessEqual(result.score, 100.0)

    def test_none_setup_caps_score_low(self):
        # Even with some raw signal, no confirmed setup keeps the score capped -
        # a stray anomaly shouldn't masquerade as a setup.
        result = compute_opportunity_score(
            l1(volume_intensity_15m=1.0), l2(return_1h_atr=1.9), SetupResult("NONE", "NONE", []), "SPOT", 10.0,
        )
        self.assertLessEqual(result.score, 20.0)

    def test_exhaustion_penalty_reduces_score(self):
        base_l2 = l2(return_5m_atr=1.0, return_15m_atr=1.0, return_1h_atr=1.5)
        setup = SetupResult("CONTINUATION", "LONG", [])
        without_exhaustion = compute_opportunity_score(l1(), base_l2, setup, "SPOT", 5.0)

        exhausted_l2 = l2(return_5m_atr=1.0, return_15m_atr=1.0, return_1h_atr=1.5, exhaustion=True)
        with_exhaustion = compute_opportunity_score(l1(), exhausted_l2, setup, "SPOT", 5.0)

        self.assertLess(with_exhaustion.score, without_exhaustion.score)


class TestMomentumCoherenceCredit(unittest.TestCase):
    def test_full_credit_when_all_three_agree(self):
        credit = momentum_coherence_credit(l2(return_5m_atr=1.0, return_15m_atr=1.0, return_1h_atr=1.0))
        self.assertEqual(credit, 1.0)

    def test_partial_credit_when_two_of_three_agree(self):
        credit = momentum_coherence_credit(l2(return_5m_atr=1.0, return_15m_atr=1.0, return_1h_atr=-1.0))
        self.assertEqual(credit, 0.5)

    def test_zero_credit_with_insufficient_signal(self):
        credit = momentum_coherence_credit(l2(return_5m_atr=0.0, return_15m_atr=0.0, return_1h_atr=0.0))
        self.assertEqual(credit, 0.0)


class TestVolumeExpansionConfirmed(unittest.TestCase):
    def test_full_intensity_with_range_expansion(self):
        credit = volume_confirmed_credit(l1(volume_intensity_15m=3.0), l2(range_expansion=True))
        self.assertGreater(credit, 0.5)

    def test_volume_without_range_expansion_is_penalized(self):
        """Volume expansion without range expansion is absorption, not a
        confirmed move (architecture doc section 5) - credit should be much
        lower than the same volume WITH range expansion."""
        with_range = volume_confirmed_credit(l1(volume_intensity_15m=3.0), l2(range_expansion=True))
        without_range = volume_confirmed_credit(l1(volume_intensity_15m=3.0), l2(range_expansion=False))
        self.assertLess(without_range, with_range)

    def test_no_volume_intensity_gives_zero_credit(self):
        credit = volume_confirmed_credit(l1(volume_intensity_15m=None), l2(range_expansion=True))
        self.assertEqual(credit, 0.0)


if __name__ == "__main__":
    unittest.main()
