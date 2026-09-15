import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.l2 import label_forward_returns
from radar_v08.store import SnapshotStore, SpotSnapshotInput

T0 = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def spot_snap(ts, last, asset="BTC", pair="XXBTZUSD"):
    return SpotSnapshotInput(
        asset=asset, pair=pair, quote="USD", ts=ts, last=last, bid=last - 0.1, ask=last + 0.1,
        bid_size=1.0, ask_size=1.0, volume_today=100.0, volume_24h=500.0,
        vwap_today=last, vwap_24h=last, trades_today=50, trades_24h=200,
        high_today=last * 1.01, low_today=last * 0.99, high_24h=last * 1.02, low_24h=last * 0.98,
        open_today=last, status="online",
    )


class TestForwardReturnLabeling(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    def test_placeholder_is_pending_until_horizon_and_snapshot_are_due(self):
        entry_ts = T0.isoformat()
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry_ts, 100.0, [15])

        # Not due yet - "now" is only 5 minutes after entry.
        pending_early = self.store.pending_forward_returns(T0 + timedelta(minutes=5))
        self.assertEqual(len(pending_early), 0)

        # Due now - 15 minutes have passed.
        pending_due = self.store.pending_forward_returns(T0 + timedelta(minutes=15))
        self.assertEqual(len(pending_due), 1)
        self.assertEqual(pending_due[0]["asset"], "BTC")
        self.assertIsNone(pending_due[0]["return_pct"])

    def test_labeling_computes_return_pct_from_the_matching_snapshot(self):
        entry_ts = T0.isoformat()
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry_ts, 100.0, [15])

        target_time = T0 + timedelta(minutes=15)
        self.store.insert_spot_snapshot(spot_snap(target_time.isoformat(), last=110.0))

        labeled_count = label_forward_returns(self.store, now=target_time)

        self.assertEqual(labeled_count, 1)
        pending_after = self.store.pending_forward_returns(target_time)
        self.assertEqual(len(pending_after), 0)  # no longer pending

    def test_labeling_computes_mfe_and_mae_from_the_intervening_window(self):
        entry_ts = T0.isoformat()
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry_ts, 100.0, [15])

        # Price wiggles up to 115 and down to 95 before settling at 105.
        self.store.insert_spot_snapshot(spot_snap((T0 + timedelta(minutes=5)).isoformat(), last=115.0))
        self.store.insert_spot_snapshot(spot_snap((T0 + timedelta(minutes=10)).isoformat(), last=95.0))
        target_time = T0 + timedelta(minutes=15)
        self.store.insert_spot_snapshot(spot_snap(target_time.isoformat(), last=105.0))

        label_forward_returns(self.store, now=target_time)

        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM forward_returns WHERE asset = 'BTC'").fetchone()
        conn.close()

        self.assertAlmostEqual(row["return_pct"], 5.0, places=4)
        self.assertAlmostEqual(row["mfe_pct"], 15.0, places=4)  # best case: 115 vs entry 100
        self.assertAlmostEqual(row["mae_pct"], -5.0, places=4)  # worst case: 95 vs entry 100

    def test_due_but_no_snapshot_yet_stays_pending(self):
        entry_ts = T0.isoformat()
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry_ts, 100.0, [15])

        # Horizon is due but no snapshot exists anywhere near the target time.
        labeled_count = label_forward_returns(self.store, now=T0 + timedelta(minutes=15))

        self.assertEqual(labeled_count, 0)
        pending = self.store.pending_forward_returns(T0 + timedelta(minutes=15))
        self.assertEqual(len(pending), 1)

    def test_different_horizons_are_labeled_independently(self):
        entry_ts = T0.isoformat()
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry_ts, 100.0, [15, 60])

        target_15 = T0 + timedelta(minutes=15)
        self.store.insert_spot_snapshot(spot_snap(target_15.isoformat(), last=110.0))

        labeled_count = label_forward_returns(self.store, now=target_15)

        self.assertEqual(labeled_count, 1)  # only the 15m horizon is due
        pending = self.store.pending_forward_returns(target_15)
        self.assertEqual(len(pending), 0)  # 60m horizon not due yet at t=15m


if __name__ == "__main__":
    unittest.main()
