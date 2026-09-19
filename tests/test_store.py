import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.store import (
    FuturesSnapshotInput,
    SnapshotStore,
    SpotSnapshotInput,
    reset_aware_delta,
)

T0 = datetime(2026, 9, 13, 23, 59, 0, tzinfo=timezone.utc)


def spot_snap(ts, volume_today, trades_today, last=100.0, asset="BTC", pair="XBTUSD"):
    return SpotSnapshotInput(
        asset=asset, pair=pair, quote="USD", ts=ts, last=last, bid=last - 0.1, ask=last + 0.1,
        bid_size=1.0, ask_size=1.0, volume_today=volume_today, volume_24h=volume_today * 5,
        vwap_today=last, vwap_24h=last, trades_today=trades_today, trades_24h=trades_today * 5,
        high_today=last * 1.01, low_today=last * 0.99, high_24h=last * 1.02, low_24h=last * 0.98,
        open_today=last, status="online",
    )


class TestResetAwareDelta(unittest.TestCase):
    def test_normal_increase(self):
        delta, reset = reset_aware_delta(150.0, 100.0)
        self.assertEqual(delta, 50.0)
        self.assertFalse(reset)

    def test_reset_detected_when_current_less_than_previous(self):
        delta, reset = reset_aware_delta(50.0, 900_000.0)
        self.assertEqual(delta, 50.0)  # not a negative spike
        self.assertTrue(reset)

    def test_none_previous_yields_none_delta(self):
        delta, reset = reset_aware_delta(50.0, None)
        self.assertIsNone(delta)
        self.assertFalse(reset)


class TestSnapshotStore(unittest.TestCase):
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

    def test_volume_delta_normal(self):
        t1 = T0.isoformat()
        t2 = (T0 + timedelta(minutes=1)).isoformat()
        self.store.insert_spot_snapshot(spot_snap(t1, volume_today=100.0, trades_today=50))
        self.store.insert_spot_snapshot(spot_snap(t2, volume_today=150.0, trades_today=80))

        row = self.store.latest_spot_snapshot("XBTUSD")
        self.assertEqual(row["delta_volume"], 50.0)
        self.assertEqual(row["session_reset"], 0)

    def test_trade_count_delta_normal(self):
        t1 = T0.isoformat()
        t2 = (T0 + timedelta(minutes=1)).isoformat()
        self.store.insert_spot_snapshot(spot_snap(t1, volume_today=100.0, trades_today=50))
        self.store.insert_spot_snapshot(spot_snap(t2, volume_today=150.0, trades_today=80))

        row = self.store.latest_spot_snapshot("XBTUSD")
        self.assertEqual(row["delta_trades"], 30)

    def test_midnight_volume_reset(self):
        before_midnight = T0.isoformat()  # 23:59 UTC, volume_today near daily max
        after_midnight = (T0 + timedelta(minutes=2)).isoformat()  # 00:01 UTC, reset to near 0

        self.store.insert_spot_snapshot(spot_snap(before_midnight, volume_today=900_000.0, trades_today=40_000))
        self.store.insert_spot_snapshot(spot_snap(after_midnight, volume_today=120.0, trades_today=30))

        row = self.store.latest_spot_snapshot("XBTUSD")
        self.assertEqual(row["session_reset"], 1)
        # No artificial negative spike: delta is the post-reset cumulative value.
        self.assertEqual(row["delta_volume"], 120.0)
        self.assertGreaterEqual(row["delta_volume"], 0.0)

    def test_midnight_trade_reset(self):
        before_midnight = T0.isoformat()
        after_midnight = (T0 + timedelta(minutes=2)).isoformat()

        self.store.insert_spot_snapshot(spot_snap(before_midnight, volume_today=900_000.0, trades_today=40_000))
        self.store.insert_spot_snapshot(spot_snap(after_midnight, volume_today=120.0, trades_today=15))

        row = self.store.latest_spot_snapshot("XBTUSD")
        self.assertEqual(row["session_reset"], 1)
        self.assertEqual(row["delta_trades"], 15)
        self.assertGreaterEqual(row["delta_trades"], 0)

    def test_oi_delta(self):
        t1 = T0.isoformat()
        t2 = (T0 + timedelta(minutes=1)).isoformat()
        self.store.insert_futures_snapshot(
            FuturesSnapshotInput(
                symbol="PF_XBTUSD", asset="BTC", ts=t1, last=50000, mark_price=50000, index_price=50000,
                bid=49990, ask=50010, bid_size=1, ask_size=1, volume_quote=1_000_000, open_interest=1000.0,
                funding_rate_raw=0.0001, funding_prediction_raw=0.0001, open_24h=50000, last_time=t1,
                suspended=False, post_only=False, tag="perpetual",
            )
        )
        self.store.insert_futures_snapshot(
            FuturesSnapshotInput(
                symbol="PF_XBTUSD", asset="BTC", ts=t2, last=50100, mark_price=50100, index_price=50090,
                bid=50090, ask=50110, bid_size=1, ask_size=1, volume_quote=1_100_000, open_interest=1200.0,
                funding_rate_raw=0.0001, funding_prediction_raw=0.0001, open_24h=50000, last_time=t2,
                suspended=False, post_only=False, tag="perpetual",
            )
        )
        row = self.store.latest_futures_snapshot("PF_XBTUSD")
        self.assertEqual(row["oi_delta"], 200.0)

    def test_missing_snapshot_returns_none(self):
        self.assertIsNone(self.store.latest_spot_snapshot("NOPE"))
        self.assertIsNone(self.store.nearest_spot_snapshot("NOPE", T0.isoformat(), tolerance_seconds=60))

        # A snapshot exists, but far outside the lookup tolerance window.
        self.store.insert_spot_snapshot(spot_snap(T0.isoformat(), volume_today=100.0, trades_today=10))
        far_target = (T0 + timedelta(hours=5)).isoformat()
        self.assertIsNone(self.store.nearest_spot_snapshot("BTC", far_target, tolerance_seconds=60))

    def test_pair_specific_spot_queries_exclude_other_quotes(self):
        t1 = T0.isoformat()
        t2 = (T0 + timedelta(minutes=15)).isoformat()
        self.store.insert_spot_snapshot(spot_snap(t1, 100.0, 10, last=100.0, asset="BTC", pair="XBTUSD"))
        self.store.insert_spot_snapshot(spot_snap(t2, 200.0, 20, last=110.0, asset="BTC", pair="XBTUSD"))
        self.store.insert_spot_snapshot(spot_snap(t1, 100.0, 10, last=10.0, asset="BTC", pair="XXBTZEUR"))
        self.store.insert_spot_snapshot(spot_snap(t2, 200.0, 20, last=1_000.0, asset="BTC", pair="XBTUSDT"))

        nearest = self.store.nearest_spot_snapshot_by_pair("XBTUSD", t1, tolerance_seconds=60)
        history = self.store.spot_history_by_pair("XBTUSD", t1)

        self.assertEqual(nearest["pair"], "XBTUSD")
        self.assertEqual(nearest["last"], 100.0)
        self.assertEqual([row["pair"] for row in history], ["XBTUSD", "XBTUSD"])
        self.assertEqual([row["last"] for row in history], [100.0, 110.0])

    def test_pair_specific_queries_do_not_borrow_missing_history(self):
        self.store.insert_spot_snapshot(
            spot_snap(T0.isoformat(), 100.0, 10, last=100.0, asset="ETH", pair="ETHEUR")
        )

        self.assertIsNone(self.store.nearest_spot_snapshot_by_pair("ETHUSD", T0.isoformat(), tolerance_seconds=60))
        self.assertEqual(self.store.spot_history_by_pair("ETHUSD", T0.isoformat()), [])


if __name__ == "__main__":
    unittest.main()
