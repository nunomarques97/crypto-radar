import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.anomaly import (
    Features,
    compute_anomaly,
    compute_features,
    compute_return,
    historical_relative_btc_series,
    historical_return_series,
    lookup_past_spot,
    robust_z,
)
from radar_v08.store import SnapshotStore, SpotSnapshotInput

NOW = datetime(2026, 9, 13, 14, 0, 0, tzinfo=timezone.utc)


def spot_snap(ts, volume_today, trades_today, last, asset, pair):
    quote = "USDT" if pair.endswith("USDT") else pair[-3:]
    return SpotSnapshotInput(
        asset=asset, pair=pair, quote=quote, ts=ts, last=last, bid=last - 0.1, ask=last + 0.1,
        bid_size=1.0, ask_size=1.0, volume_today=volume_today, volume_24h=volume_today * 5,
        vwap_today=last, vwap_24h=last, trades_today=trades_today, trades_24h=trades_today * 5,
        high_today=last * 1.01, low_today=last * 0.99, high_24h=last * 1.02, low_24h=last * 0.98,
        open_today=last, status="online",
    )


def build_flat_history(store, asset, pair, now, hours=2, interval_minutes=5, price=100.0, volume=1000.0, trades=500):
    t = now - timedelta(hours=hours)
    while t < now:
        store.insert_spot_snapshot(spot_snap(t.isoformat(), volume, trades, price, asset, pair))
        t += timedelta(minutes=interval_minutes)
        volume += 50.0
        trades += 20


def build_return_history(store, asset, pair, now, returns):
    """Insert a pair-pure 15-minute return path ending at ``now - 15m``."""
    price = 100.0
    t = now - timedelta(minutes=15 * (len(returns) + 1))
    store.insert_spot_snapshot(spot_snap(t.isoformat(), 1000.0, 500, price, asset, pair))
    for index, return_pct in enumerate(returns, start=1):
        price *= 1.0 + return_pct / 100.0
        t = now - timedelta(minutes=15 * (len(returns) + 1 - index))
        store.insert_spot_snapshot(spot_snap(t.isoformat(), 1000.0 + index, 500 + index, price, asset, pair))
    return price


class TestAnomaly(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)

    def test_warmup_true_with_insufficient_history(self):
        self.store.insert_spot_snapshot(
            spot_snap((NOW - timedelta(minutes=5)).isoformat(), 100.0, 50, 100.0, "ETH", "ETHUSD")
        )
        features = compute_features(
            store=self.store, asset="ETH", now_dt=NOW,
            pair="ETHUSD",
            current_last=101.0, current_volume_today=110.0, current_trades_today=55,
            current_spread_bps=5.0, current_bid=100.9, current_bid_size=1.0,
            current_ask=101.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        result = compute_anomaly(self.store, "ETH", "ETHUSD", NOW, features)
        self.assertTrue(result.warmup)
        self.assertIsNone(result.anomaly_score)
        self.assertIn("warmup", result.flags)


class TestT021BtcRelativeHistory(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)

    def _matched_history(self):
        residuals = [1.0, 2.0, 3.0, 4.0, 1.0, 2.0, 3.0, 4.0, 1.0, 2.0, 3.0]
        build_return_history(self.store, "ETH", "ETHUSD", NOW, [1.0 + value for value in residuals])
        build_return_history(self.store, "BTC", "XBTUSD", NOW, [1.0] * len(residuals))
        return residuals

    def test_matched_pair_pure_residual_history_has_hand_calculated_robust_z(self):
        residuals = self._matched_history()
        result = compute_anomaly(
            self.store, "ETH", "ETHUSD", NOW,
            Features(relative_return_vs_btc_15m=5.0), btc_pair="XBTUSD",
        )

        # median([1,2,3,4,1,2,3,4,1,2,3]) = 2; MAD = 1.
        self.assertAlmostEqual(result.relative_btc_z, (5.0 - 2.0) / config.MAD_SCALE)
        asset_history = self.store.spot_history_by_pair("ETHUSD", (NOW - timedelta(hours=48)).isoformat())
        self.assertNotEqual(result.relative_btc_z, robust_z(5.0, historical_return_series(asset_history, 15)))
        self.assertEqual(residuals.count(2.0), 3)

    def test_missing_btc_or_asset_history_leaves_relative_statistic_unavailable(self):
        build_return_history(self.store, "ETH", "ETHUSD", NOW, [2.0] * 11)
        missing_btc = compute_anomaly(
            self.store, "ETH", "ETHUSD", NOW,
            Features(relative_return_vs_btc_15m=1.0), btc_pair="XBTUSD",
        )
        self.assertIsNone(missing_btc.relative_btc_z)
        self.assertIn("relative_btc_history_unavailable", missing_btc.flags)

        build_return_history(self.store, "BTC", "XBTUSD", NOW, [1.0] * 11)
        missing_asset = compute_anomaly(
            self.store, "SOL", "SOLUSD", NOW,
            Features(relative_return_vs_btc_15m=1.0), btc_pair="XBTUSD",
        )
        self.assertTrue(missing_asset.warmup)
        self.assertIsNone(missing_asset.relative_btc_z)

    def test_mismatched_endpoints_do_not_create_residual_observations(self):
        # Each row has a valid 15m return, but the endpoint series are 10m apart
        # and therefore outside the existing 7.5-minute matching tolerance.
        build_return_history(self.store, "ETH", "ETHUSD", NOW, [2.0, 2.0])
        btc_now = NOW - timedelta(minutes=25)
        build_return_history(self.store, "BTC", "XBTUSD", btc_now, [1.0])
        asset_rows = self.store.spot_history_by_pair("ETHUSD", (NOW - timedelta(hours=48)).isoformat())
        btc_rows = self.store.spot_history_by_pair("XBTUSD", (NOW - timedelta(hours=48)).isoformat())
        self.assertEqual(historical_relative_btc_series(asset_rows, btc_rows, 15), [])

    def test_btc_has_no_independent_btc_relative_residual(self):
        self._matched_history()
        features = compute_features(
            store=self.store, asset="BTC", pair="XBTUSD", now_dt=NOW,
            current_last=101.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=100.9, current_bid_size=1.0,
            current_ask=101.1, current_ask_size=1.0,
            btc_return_15m=1.0, btc_return_1h=1.0,
        )
        result = compute_anomaly(self.store, "BTC", "XBTUSD", NOW, features, btc_pair="XBTUSD")
        self.assertIsNone(features.relative_return_vs_btc_15m)
        self.assertIsNone(result.relative_btc_z)

    def test_score_exists_and_bounded_with_enough_history(self):
        build_flat_history(self.store, "BTC", "XBTUSD", NOW, price=100.0)
        current_last = 100.0 + 20.0  # sharp jump vs a flat 2h history

        features = compute_features(
            store=self.store, asset="BTC", now_dt=NOW,
            pair="XBTUSD",
            current_last=current_last, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=current_last - 0.1, current_bid_size=1.0,
            current_ask=current_last + 0.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        result = compute_anomaly(self.store, "BTC", "XBTUSD", NOW, features)

        self.assertFalse(result.warmup)
        self.assertIsNotNone(result.anomaly_score)
        self.assertGreaterEqual(result.anomaly_score, 0.0)
        self.assertLessEqual(result.anomaly_score, 100.0)
        self.assertGreater(result.anomaly_score, 5.0)

    def test_anomaly_score_is_direction_agnostic(self):
        build_flat_history(self.store, "UP", "UPUSD", NOW, price=100.0)
        build_flat_history(self.store, "DOWN", "DOWNUSD", NOW, price=100.0)

        up_features = compute_features(
            store=self.store, asset="UP", now_dt=NOW,
            pair="UPUSD",
            current_last=115.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=114.9, current_bid_size=1.0,
            current_ask=115.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        down_features = compute_features(
            store=self.store, asset="DOWN", now_dt=NOW,
            pair="DOWNUSD",
            current_last=85.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=84.9, current_bid_size=1.0,
            current_ask=85.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )

        up_result = compute_anomaly(self.store, "UP", "UPUSD", NOW, up_features)
        down_result = compute_anomaly(self.store, "DOWN", "DOWNUSD", NOW, down_features)

        self.assertIsNotNone(up_result.anomaly_score)
        self.assertIsNotNone(down_result.anomaly_score)
        self.assertAlmostEqual(up_result.anomaly_score, down_result.anomaly_score, delta=0.5)

    def test_usd_l1_history_ignores_conflicting_eur_and_usdt_rows(self):
        build_flat_history(self.store, "ETH", "ETHUSD", NOW, price=100.0)
        baseline_features = compute_features(
            store=self.store, asset="ETH", pair="ETHUSD", now_dt=NOW,
            current_last=120.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=119.9, current_bid_size=1.0,
            current_ask=120.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        baseline = compute_anomaly(self.store, "ETH", "ETHUSD", NOW, baseline_features)

        build_flat_history(self.store, "ETH", "ETHEUR", NOW, price=10.0)
        build_flat_history(self.store, "ETH", "ETHUSDT", NOW, price=1_000.0)
        isolated_features = compute_features(
            store=self.store, asset="ETH", pair="ETHUSD", now_dt=NOW,
            current_last=120.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=119.9, current_bid_size=1.0,
            current_ask=120.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        isolated = compute_anomaly(self.store, "ETH", "ETHUSD", NOW, isolated_features)

        self.assertEqual(isolated_features.return_15m, baseline_features.return_15m)
        self.assertEqual(isolated.sample_count, baseline.sample_count)
        self.assertEqual(isolated.history_minutes, baseline.history_minutes)
        self.assertEqual(isolated.warmup, baseline.warmup)
        self.assertEqual(isolated.price_z, baseline.price_z)
        self.assertEqual(isolated.anomaly_score, baseline.anomaly_score)

    def test_eur_l1_history_ignores_conflicting_usd_and_usdt_rows(self):
        build_flat_history(self.store, "ETH", "ETHEUR", NOW, price=100.0)
        baseline_features = compute_features(
            store=self.store, asset="ETH", pair="ETHEUR", now_dt=NOW,
            current_last=120.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=119.9, current_bid_size=1.0,
            current_ask=120.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        baseline = compute_anomaly(self.store, "ETH", "ETHEUR", NOW, baseline_features)

        build_flat_history(self.store, "ETH", "ETHUSD", NOW, price=10.0)
        build_flat_history(self.store, "ETH", "ETHUSDT", NOW, price=1_000.0)
        isolated_features = compute_features(
            store=self.store, asset="ETH", pair="ETHEUR", now_dt=NOW,
            current_last=120.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=119.9, current_bid_size=1.0,
            current_ask=120.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        isolated = compute_anomaly(self.store, "ETH", "ETHEUR", NOW, isolated_features)

        self.assertEqual(isolated_features.return_15m, baseline_features.return_15m)
        self.assertEqual(isolated.sample_count, baseline.sample_count)
        self.assertEqual(isolated.history_minutes, baseline.history_minutes)
        self.assertEqual(isolated.warmup, baseline.warmup)
        self.assertEqual(isolated.price_z, baseline.price_z)
        self.assertEqual(isolated.anomaly_score, baseline.anomaly_score)

    def test_btc_comparator_uses_selected_xbt_usd_pair(self):
        build_flat_history(self.store, "BTC", "XBTUSD", NOW, price=100.0)
        build_flat_history(self.store, "BTC", "XXBTZEUR", NOW, price=10.0)
        build_flat_history(self.store, "ETH", "ETHUSD", NOW, price=100.0)

        btc_past = lookup_past_spot(self.store, "XBTUSD", NOW, 15)
        btc_return = compute_return(110.0, btc_past)
        features = compute_features(
            store=self.store, asset="ETH", pair="ETHUSD", now_dt=NOW,
            current_last=110.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=109.9, current_bid_size=1.0,
            current_ask=110.1, current_ask_size=1.0,
            btc_return_15m=btc_return, btc_return_1h=None,
        )

        self.assertEqual(btc_past["asset"], "BTC")
        self.assertEqual(btc_past["pair"], "XBTUSD")
        self.assertAlmostEqual(btc_return, 10.0)
        self.assertAlmostEqual(features.relative_return_vs_btc_15m, 0.0)

    def test_missing_selected_pair_history_stays_unknown_and_warmup(self):
        build_flat_history(self.store, "ETH", "ETHEUR", NOW, price=100.0)
        build_flat_history(self.store, "ETH", "ETHUSDT", NOW, price=1_000.0)

        features = compute_features(
            store=self.store, asset="ETH", pair="ETHUSD", now_dt=NOW,
            current_last=120.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=119.9, current_bid_size=1.0,
            current_ask=120.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        result = compute_anomaly(self.store, "ETH", "ETHUSD", NOW, features)

        self.assertIsNone(features.return_15m)
        self.assertTrue(result.warmup)
        self.assertEqual(result.sample_count, 0)
        self.assertIsNone(result.anomaly_score)


if __name__ == "__main__":
    unittest.main()
