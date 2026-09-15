import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.anomaly import compute_anomaly, compute_features
from radar_v08.store import SnapshotStore, SpotSnapshotInput

NOW = datetime(2026, 9, 13, 14, 0, 0, tzinfo=timezone.utc)


def spot_snap(ts, volume_today, trades_today, last, asset, pair):
    return SpotSnapshotInput(
        asset=asset, pair=pair, quote="USD", ts=ts, last=last, bid=last - 0.1, ask=last + 0.1,
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
            current_last=101.0, current_volume_today=110.0, current_trades_today=55,
            current_spread_bps=5.0, current_bid=100.9, current_bid_size=1.0,
            current_ask=101.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        result = compute_anomaly(self.store, "ETH", NOW, features)
        self.assertTrue(result.warmup)
        self.assertIsNone(result.anomaly_score)
        self.assertIn("warmup", result.flags)

    def test_score_exists_and_bounded_with_enough_history(self):
        build_flat_history(self.store, "BTC", "XBTUSD", NOW, price=100.0)
        current_last = 100.0 + 20.0  # sharp jump vs a flat 2h history

        features = compute_features(
            store=self.store, asset="BTC", now_dt=NOW,
            current_last=current_last, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=current_last - 0.1, current_bid_size=1.0,
            current_ask=current_last + 0.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        result = compute_anomaly(self.store, "BTC", NOW, features)

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
            current_last=115.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=114.9, current_bid_size=1.0,
            current_ask=115.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )
        down_features = compute_features(
            store=self.store, asset="DOWN", now_dt=NOW,
            current_last=85.0, current_volume_today=5000.0, current_trades_today=3000,
            current_spread_bps=5.0, current_bid=84.9, current_bid_size=1.0,
            current_ask=85.1, current_ask_size=1.0,
            btc_return_15m=None, btc_return_1h=None,
        )

        up_result = compute_anomaly(self.store, "UP", NOW, up_features)
        down_result = compute_anomaly(self.store, "DOWN", NOW, down_features)

        self.assertIsNotNone(up_result.anomaly_score)
        self.assertIsNotNone(down_result.anomaly_score)
        self.assertAlmostEqual(up_result.anomaly_score, down_result.anomaly_score, delta=0.5)


if __name__ == "__main__":
    unittest.main()
