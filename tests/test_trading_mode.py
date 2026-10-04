"""TradingMode (radar_v08/domain/trading_mode.py): fixed order and strict parsing."""

from __future__ import annotations

import unittest

from radar_v08.domain.trading_mode import (
    DEFAULT_TRADING_MODE,
    PRIVATE_READ_MINIMUM_MODE,
    TradingMode,
    TradingModeError,
    allows_private_reads,
    parse_trading_mode,
)

ROADMAP_ORDER = (
    "ANALYSIS_ONLY",
    "RETROSPECTIVE",
    "PAPER",
    "SHADOW_LIVE",
    "MICRO_LIVE",
    "CONSTRAINED_LIVE",
    "APPROVED_ENVELOPE",
)


class TestOrder(unittest.TestCase):
    def test_members_follow_the_roadmap_progression(self):
        self.assertEqual(tuple(mode.value for mode in TradingMode), ROADMAP_ORDER)
        modes = [TradingMode(name) for name in ROADMAP_ORDER]
        for lower, higher in zip(modes, modes[1:]):
            self.assertLess(lower, higher)
            self.assertLessEqual(lower, higher)
            self.assertGreater(higher, lower)
            self.assertGreaterEqual(higher, lower)
            self.assertFalse(higher < lower)
        self.assertEqual(sorted(reversed(modes)), modes)

    def test_order_is_not_alphabetical(self):
        # Alphabetically APPROVED_ENVELOPE < PAPER; the roadmap order says the opposite.
        self.assertGreater(TradingMode.APPROVED_ENVELOPE, TradingMode.PAPER)
        self.assertLess(TradingMode.RETROSPECTIVE, TradingMode.PAPER)

    def test_comparison_with_other_types_is_refused(self):
        with self.assertRaises(TypeError):
            TradingMode.PAPER < 3  # noqa: B015
        with self.assertRaises(TypeError):
            TradingMode.PAPER >= "PAPER"  # noqa: B015
        self.assertNotEqual(TradingMode.PAPER, "PAPER")

    def test_default_is_the_lowest_mode_and_never_above_paper(self):
        self.assertIs(DEFAULT_TRADING_MODE, TradingMode.ANALYSIS_ONLY)
        self.assertLessEqual(DEFAULT_TRADING_MODE, TradingMode.PAPER)

    def test_private_reads_start_at_shadow_live(self):
        self.assertIs(PRIVATE_READ_MINIMUM_MODE, TradingMode.SHADOW_LIVE)
        allowed = {mode for mode in TradingMode if allows_private_reads(mode)}
        self.assertEqual(
            allowed,
            {
                TradingMode.SHADOW_LIVE,
                TradingMode.MICRO_LIVE,
                TradingMode.CONSTRAINED_LIVE,
                TradingMode.APPROVED_ENVELOPE,
            },
        )
        for value in ("SHADOW_LIVE", 3, None, True):
            self.assertFalse(allows_private_reads(value))  # type: ignore[arg-type]


class TestParsing(unittest.TestCase):
    def test_exact_names_parse(self):
        for name in ROADMAP_ORDER:
            self.assertIs(parse_trading_mode(name), TradingMode(name))

    def test_everything_else_is_refused(self):
        refused = [
            "",
            " ",
            "shadow_live",
            "Shadow_Live",
            "SHADOW_live",
            " SHADOW_LIVE",
            "SHADOW_LIVE ",
            "SHADOW_LIVE\n",
            "SHADOW-LIVE",
            "SHADOWLIVE",
            "LIVE",
            "PAPER,SHADOW_LIVE",
            "TradingMode.SHADOW_LIVE",
            "UNRESTRICTED",
            "3",
            None,
            3,
            True,
            b"PAPER",
            TradingMode.PAPER,
        ]
        for value in refused:
            with self.subTest(value=value):
                with self.assertRaises(TradingModeError):
                    parse_trading_mode(value)

    def test_refusal_does_not_echo_the_input(self):
        with self.assertRaises(TradingModeError) as caught:
            parse_trading_mode("hostile-input-9f3a")
        self.assertNotIn("hostile-input-9f3a", str(caught.exception))

    def test_string_subclass_is_refused(self):
        class Sneaky(str):
            def __eq__(self, other):
                return True

            __hash__ = str.__hash__

        with self.assertRaises(TradingModeError):
            parse_trading_mode(Sneaky("anything"))


if __name__ == "__main__":
    unittest.main()
