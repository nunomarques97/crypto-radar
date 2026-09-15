import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.kraken_futures import parse_perpetuals
from radar_v08.kraken_spot import parse_ticker_row
from radar_v08.universe import build_spot_markets, futures_is_stale, group_assets

NOW = "2026-09-13T12:00:00+00:00"


def raw_ticker(
    ask=100.0, bid=99.0, last=99.5, ask_size=10.0, bid_size=10.0,
    vol_today=1000.0, vol_24h=5000.0, vwap_today=99.0, vwap_24h=99.0,
    trades_today=500, trades_24h=2000, high_today=101.0, high_24h=102.0,
    low_today=98.0, low_24h=97.0, open_price=99.0,
):
    return {
        "a": [str(ask), "0", str(ask_size)],
        "b": [str(bid), "0", str(bid_size)],
        "c": [str(last), "0"],
        "v": [str(vol_today), str(vol_24h)],
        "p": [str(vwap_today), str(vwap_24h)],
        "t": [trades_today, trades_24h],
        "h": [str(high_today), str(high_24h)],
        "l": [str(low_today), str(low_24h)],
        "o": str(open_price),
    }


def meta(wsname, status="online", aclass_base="currency"):
    return {"wsname": wsname, "altname": wsname.replace("/", ""), "status": status, "aclass_base": aclass_base}


class TestQuoteDedup(unittest.TestCase):
    def test_quote_dedup_prefers_usd_then_aggregates_volume(self):
        rows = {
            "XXBTZUSD": parse_ticker_row("XXBTZUSD", raw_ticker(vol_today=10, vol_24h=100, last=50000), meta("XBT/USD"), NOW),
            "XXBTZEUR": parse_ticker_row("XXBTZEUR", raw_ticker(vol_today=10, vol_24h=50, last=46000), meta("XBT/EUR"), NOW),
        }
        pairs = {"XXBTZUSD": meta("XBT/USD"), "XXBTZEUR": meta("XBT/EUR")}

        markets, _excluded = build_spot_markets(rows, pairs)
        assets = group_assets(markets, [])

        self.assertIn("BTC", assets)
        btc = assets["BTC"]
        # Only one primary market chosen (no duplicate BTC), and it must be
        # the USD one per quote priority.
        self.assertEqual(btc.primary_market.quote, "USD")
        # Aggregate liquidity still reflects both quote markets (EUR leg
        # converted to USD via the live/fallback EUR-USD rate).
        from radar_v08 import config as radar_config

        expected = 100 * 50000 + (50 * 46000) * radar_config.EUR_USD_FALLBACK_RATE
        self.assertAlmostEqual(btc.aggregate_volume_24h_usd, expected, delta=1.0)
        self.assertEqual(len(btc.markets), 2)


class TestBtcDogeFuturesMatch(unittest.TestCase):
    def _build(self, spot_wsname, futures_pair, futures_symbol, last=100.0):
        rows = {"K": parse_ticker_row("K", raw_ticker(last=last), meta(spot_wsname), NOW)}
        pairs = {"K": meta(spot_wsname)}
        markets, _ = build_spot_markets(rows, pairs)

        futures_raw = [
            {
                "symbol": futures_symbol,
                "pair": futures_pair,
                "last": last,
                "markPrice": last,
                "indexPrice": last,
                "bid": last - 0.5,
                "ask": last + 0.5,
                "bidSize": 1.0,
                "askSize": 1.0,
                "volumeQuote": 1_000_000.0,
                "openInterest": 500.0,
                "fundingRate": 0.0001,
                "fundingRatePrediction": 0.0001,
                "open24h": last,
                "lastTime": NOW,
                "suspended": False,
                "postOnly": False,
                "tag": "perpetual",
            }
        ]
        futures_rows = parse_perpetuals(futures_raw, NOW)
        return group_assets(markets, futures_rows)

    def test_btc_spot_futures_match(self):
        assets = self._build("XBT/USD", "XBT:USD", "PF_XBTUSD")
        self.assertIn("BTC", assets)
        self.assertIsNotNone(assets["BTC"].futures)
        self.assertEqual(assets["BTC"].futures.symbol, "PF_XBTUSD")

    def test_doge_spot_futures_match(self):
        assets = self._build("XDG/USD", "XDG:USD", "PF_XDGUSD")
        self.assertIn("DOGE", assets)
        self.assertIsNotNone(assets["DOGE"].futures)
        self.assertEqual(assets["DOGE"].futures.symbol, "PF_XDGUSD")


class TestStatusHandling(unittest.TestCase):
    def test_post_only_status_marks_untradeable(self):
        rows = {"K": parse_ticker_row("K", raw_ticker(last=50000, vol_24h=100, trades_24h=2000), meta("XBT/USD", status="post_only"), NOW)}
        pairs = {"K": meta("XBT/USD", status="post_only")}
        markets, _ = build_spot_markets(rows, pairs)
        assets = group_assets(markets, [])
        entry = assets["BTC"]
        self.assertTrue(entry.eligible)
        self.assertFalse(entry.tradeable)
        self.assertIn("post_only", entry.flags)

    def test_suspended_futures_does_not_block_spot_tradeability_but_is_flagged(self):
        rows = {"K": parse_ticker_row("K", raw_ticker(last=50000, vol_24h=100, trades_24h=2000), meta("XBT/USD"), NOW)}
        pairs = {"K": meta("XBT/USD")}
        markets, _ = build_spot_markets(rows, pairs)

        futures_raw = [
            {
                "symbol": "PF_XBTUSD", "pair": "XBT:USD", "last": 50000, "markPrice": 50000,
                "indexPrice": 50000, "bid": 49990, "ask": 50010, "bidSize": 1.0, "askSize": 1.0,
                "volumeQuote": 1_000_000.0, "openInterest": 500.0, "fundingRate": 0.0001,
                "fundingRatePrediction": 0.0001, "open24h": 50000, "lastTime": NOW,
                "suspended": True, "postOnly": False, "tag": "perpetual",
            }
        ]
        futures_rows = parse_perpetuals(futures_raw, NOW)
        assets = group_assets(markets, futures_rows)
        entry = assets["BTC"]
        self.assertTrue(entry.futures.suspended)


class TestFuturesFreshness(unittest.TestCase):
    def test_stale_futures_ticker_detected(self):
        now = datetime.now(timezone.utc)
        stale_time = (now - timedelta(minutes=30)).isoformat()
        self.assertTrue(futures_is_stale(stale_time, now))

    def test_fresh_futures_ticker_not_stale(self):
        now = datetime.now(timezone.utc)
        fresh_time = (now - timedelta(seconds=10)).isoformat()
        self.assertFalse(futures_is_stale(fresh_time, now))

    def test_missing_last_time_is_stale(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(futures_is_stale(None, now))


if __name__ == "__main__":
    unittest.main()
