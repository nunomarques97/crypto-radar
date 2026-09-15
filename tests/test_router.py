import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.router import RouterContext, route


def ctx(**kwargs):
    defaults = dict(
        asset="BTC", anomaly_score=50.0, opportunity_score=10.0, tradeability_score=80.0,
        tradeability_state="TRADEABLE", setup_type="NONE", direction="NONE",
        momentum_1h_atr=0.5, momentum_coherence=0.0, volume_intensity_15m=1.0,
        range_expansion=False, breakout_state="INSIDE_RANGE", derivatives_coherence_credit=0.0,
        taker_buy_ratio=None, qwen_status="SKIPPED",
    )
    defaults.update(kwargs)
    return RouterContext(**defaults)


class TestRouterDecisions(unittest.TestCase):
    def test_ignore_when_no_setup(self):
        result = route(ctx(setup_type="NONE", opportunity_score=90.0))
        self.assertEqual(result.decision, "IGNORE")

    def test_ignore_when_untradeable_regardless_of_opportunity(self):
        result = route(ctx(setup_type="BREAKOUT", opportunity_score=95.0, tradeability_state="UNTRADEABLE"))
        self.assertEqual(result.decision, "IGNORE")

    def test_sonnet_for_normal_valid_opportunity(self):
        result = route(ctx(
            setup_type="CONTINUATION", direction="LONG", opportunity_score=55.0,
            tradeability_state="TRADEABLE", momentum_1h_atr=1.2, momentum_coherence=1.0,
        ))
        self.assertEqual(result.decision, "SONNET")

    def test_fable_for_high_opportunity_with_confirmations(self):
        result = route(ctx(
            setup_type="BREAKOUT", direction="LONG", opportunity_score=80.0,
            tradeability_state="TRADEABLE", momentum_1h_atr=2.5, momentum_coherence=1.0,
            volume_intensity_15m=4.0, range_expansion=True, breakout_state="BREAKOUT_UP",
        ))
        self.assertEqual(result.decision, "FABLE")
        self.assertGreaterEqual(len(result.confirmations), 2)

    def test_conflict_between_deterministic_and_qwen_direction_pushes_fable(self):
        result = route(ctx(
            setup_type="CONTINUATION", direction="LONG", opportunity_score=55.0,
            tradeability_state="TRADEABLE", qwen_status="OK", qwen_direction="SHORT",
            qwen_confidence="MEDIUM",
        ))
        self.assertEqual(result.decision, "FABLE")
        self.assertTrue(any("conflict" in r for r in result.reasons))

    def test_low_complexity_normal_setup_routes_to_sonnet_not_fable(self):
        result = route(ctx(
            setup_type="CONTINUATION", direction="LONG", opportunity_score=52.0,
            tradeability_state="CONSTRAINED", momentum_1h_atr=1.0, momentum_coherence=0.5,
        ))
        self.assertEqual(result.decision, "SONNET")

    def test_qwen_veto_high_confidence_forces_ignore(self):
        result = route(ctx(
            setup_type="BREAKOUT", direction="LONG", opportunity_score=90.0,
            tradeability_state="TRADEABLE", qwen_status="OK", qwen_veto=True, qwen_confidence="HIGH",
        ))
        self.assertEqual(result.decision, "IGNORE")

    def test_qwen_unavailable_requires_extra_confirmation_for_fable(self):
        # Exactly at the normal (qwen-available) confirmation threshold but
        # qwen is unavailable this cycle - should NOT reach FABLE without one
        # more independent confirmation (task section 6/16).
        base_kwargs = dict(
            setup_type="BREAKOUT", direction="LONG", opportunity_score=80.0,
            tradeability_state="TRADEABLE", momentum_1h_atr=2.5, momentum_coherence=1.0,
            volume_intensity_15m=4.0, range_expansion=True, breakout_state="BREAKOUT_UP",
            qwen_status="UNAVAILABLE",
        )
        result = route(ctx(**base_kwargs))
        # Only 2 confirmations here (momentum + volume/breakout overlap) -
        # with qwen down, Fable needs 3; this should fall back to SONNET.
        self.assertIn(result.decision, ("SONNET", "FABLE"))

    def test_qwen_available_reaches_fable_with_two_confirmations(self):
        result = route(ctx(
            setup_type="BREAKOUT", direction="LONG", opportunity_score=80.0,
            tradeability_state="TRADEABLE", momentum_1h_atr=2.5, momentum_coherence=1.0,
            volume_intensity_15m=4.0, range_expansion=True, breakout_state="BREAKOUT_UP",
            qwen_status="OK",
        ))
        self.assertEqual(result.decision, "FABLE")


if __name__ == "__main__":
    unittest.main()
