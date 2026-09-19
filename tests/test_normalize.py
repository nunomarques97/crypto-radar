import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.normalize import (
    is_fiat,
    is_known_stable,
    is_stable_like,
    normalize_asset,
)


class TestLegacyCodeNormalization(unittest.TestCase):
    def test_xbt_to_btc(self):
        self.assertEqual(normalize_asset("XBT"), "BTC")
        self.assertEqual(normalize_asset("xbt"), "BTC")

    def test_xdg_to_doge(self):
        self.assertEqual(normalize_asset("XDG"), "DOGE")
        self.assertEqual(normalize_asset("xdg"), "DOGE")

    def test_unmapped_code_passthrough(self):
        self.assertEqual(normalize_asset("ETH"), "ETH")


class TestFiatAndStableDetection(unittest.TestCase):
    def test_fiat_exclusion(self):
        for code in ("USD", "EUR", "GBP", "JPY"):
            self.assertTrue(is_fiat(code), f"{code} should be treated as fiat")
        self.assertFalse(is_fiat("BTC"))

    def test_known_stablecoin_exclusion(self):
        for code in ("USDT", "USDC", "DAI"):
            self.assertTrue(is_known_stable(code), f"{code} should be a known stable")
        self.assertFalse(is_known_stable("BTC"))

    def test_stable_like_dynamic_detection(self):
        # Pegged price + near-zero 24h range -> stable-like, even if not in
        # the static list (catches a brand-new stablecoin automatically).
        self.assertTrue(is_stable_like(price_vs_usd=1.001, range_24h_pct=0.2))
        self.assertTrue(is_stable_like(price_vs_usd=0.98, range_24h_pct=0.5))

    def test_stable_like_rejects_volatile_asset(self):
        # BTC-like: not pegged, and/or large range -> never flagged stable-like.
        self.assertFalse(is_stable_like(price_vs_usd=65000.0, range_24h_pct=3.5))
        self.assertFalse(is_stable_like(price_vs_usd=1.0, range_24h_pct=5.0))

    def test_stable_like_handles_missing_data(self):
        self.assertFalse(is_stable_like(None, None))
        self.assertFalse(is_stable_like(1.0, None))


if __name__ == "__main__":
    unittest.main()
