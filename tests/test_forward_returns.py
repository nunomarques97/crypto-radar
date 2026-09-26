import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
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


class TestForwardReturnQueueDoesNotStall(unittest.TestCase):
    """Regression: rows that can never be labeled must not block the queue."""

    NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
    BATCH = 500

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "fwd.sqlite")
        self.store = SnapshotStore(self.path)
        self.addCleanup(self.store.close)
        for name, value in (
            ("SNAPSHOT_RETENTION_DAYS", 7),
            ("FORWARD_RETURN_LABEL_BATCH_LIMIT", self.BATCH),
            ("FORWARD_RETURN_LOOKUP_TOLERANCE_SECONDS", 120.0),
        ):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def rows(self, where="1 = 1", params=()):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(f"SELECT * FROM forward_returns WHERE {where} ORDER BY id", params).fetchall()
        finally:
            conn.close()

    def count(self, where="1 = 1", params=()):
        return len(self.rows(where, params))

    def add_labelable(self, entries, start, pair="XXBTZUSD"):
        """`entries` 15-minute rows starting at `start`, each with a snapshot at its target."""
        for i in range(entries):
            entry = start + timedelta(minutes=i)
            self.store.create_forward_return_placeholders("BTC", pair, entry.isoformat(), 100.0, [15])
            self.store.insert_spot_snapshot(spot_snap((entry + timedelta(minutes=15)).isoformat(), 110.0, pair=pair))

    def test_impossible_rows_ahead_of_labelable_rows_no_longer_stall_labeling(self):
        # 2100 rows (700 entries x 3 horizons) whose targets are ~10 days old:
        # before the 7-day retention boundary and with no snapshot.
        old_start = self.NOW - timedelta(days=10)
        for i in range(700):
            self.store.create_forward_return_placeholders(
                "BTC", "XXBTZUSD", (old_start + timedelta(seconds=i)).isoformat(), 100.0, [15, 60, 240]
            )
        impossible = 2100
        labelable = 12
        self.add_labelable(labelable, self.NOW - timedelta(hours=2))
        total_before = self.count()
        self.assertEqual(total_before, impossible + labelable)

        # Every impossible row sorts ahead of every labelable one.
        max_passes = -(-impossible // self.BATCH) + 1  # ceil(2100 / 500) + 1 = 6
        labeled = 0
        passes = 0
        while passes < max_passes and labeled < labelable:
            passes += 1
            labeled += label_forward_returns(self.store, self.NOW)
        self.assertEqual(labeled, labelable, f"labeled {labeled} of {labelable} rows after {passes} passes")
        self.assertLessEqual(passes, 5)

        marked = self.rows("unlabelable_reason IS NOT NULL")
        self.assertEqual(len(marked), impossible)
        self.assertEqual({r["unlabelable_reason"] for r in marked}, {"target_outside_snapshot_retention"})
        self.assertEqual({r["unlabelable_at"] for r in marked}, {self.NOW.isoformat()})
        for r in marked:  # no invented return
            self.assertIsNone(r["return_pct"])
            self.assertIsNone(r["mfe_pct"])
            self.assertIsNone(r["mae_pct"])
            self.assertIsNone(r["labeled_at"])
            self.assertLess(r["ts"], (self.NOW - timedelta(days=7)).isoformat())
        self.assertEqual(self.count("return_pct IS NOT NULL AND unlabelable_reason IS NULL"), labelable)
        self.assertEqual(self.count(), total_before)  # nothing deleted

        # Marked rows are never returned again, and a further pass changes nothing.
        self.assertEqual(self.store.pending_forward_returns(self.NOW, limit=10_000), [])
        before = [tuple(r) for r in self.rows()]
        self.assertEqual(label_forward_returns(self.store, self.NOW), 0)
        self.assertEqual([tuple(r) for r in self.rows()], before)

    def test_due_boundary_is_exact_to_the_microsecond(self):
        entry = datetime(2026, 9, 25, 10, 0, 0, 123456, tzinfo=timezone.utc)
        whole_second = datetime(2026, 9, 25, 10, 1, 0, tzinfo=timezone.utc)  # isoformat() drops .000000
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry.isoformat(), 100.0, [15])
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", whole_second.isoformat(), 100.0, [60])
        one_us = timedelta(microseconds=1)

        target = entry + timedelta(minutes=15)
        self.assertEqual(self.store.pending_forward_returns(target - one_us), [])
        self.assertEqual([r["ts"] for r in self.store.pending_forward_returns(target)], [entry.isoformat()])

        target_60 = whole_second + timedelta(minutes=60)
        self.assertEqual([r["ts"] for r in self.store.pending_forward_returns(target_60 - one_us)], [entry.isoformat()])
        self.assertEqual([r["horizon_minutes"] for r in self.store.pending_forward_returns(target_60)], [15, 60])
        # An aware non-UTC `now` means the same instant.
        plus_two = timezone(timedelta(hours=2))
        self.assertEqual(len(self.store.pending_forward_returns(target.astimezone(plus_two))), 1)
        self.assertEqual(len(self.store.pending_forward_returns((target - one_us).astimezone(plus_two))), 0)

    def test_naive_now_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.pending_forward_returns(self.NOW.replace(tzinfo=None))

    def test_in_retention_gap_row_stays_pending_and_unmarked(self):
        # Due, inside retention, no snapshot near its target: retried, not marked.
        entry = self.NOW - timedelta(days=2)
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry.isoformat(), 100.0, [15])
        self.store.insert_spot_snapshot(spot_snap((self.NOW - timedelta(days=3)).isoformat(), 100.0))

        self.assertEqual(label_forward_returns(self.store, self.NOW), 0)
        self.assertEqual(self.count("unlabelable_reason IS NOT NULL"), 0)
        self.assertEqual(len(self.store.pending_forward_returns(self.NOW)), 1)

    def test_row_outside_retention_with_a_matching_snapshot_is_labeled_normally(self):
        entry = self.NOW - timedelta(days=10)
        target = entry + timedelta(minutes=15)
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry.isoformat(), 100.0, [15])
        self.store.insert_spot_snapshot(spot_snap(target.isoformat(), 105.0))  # not pruned yet

        self.assertEqual(label_forward_returns(self.store, self.NOW), 1)
        (row,) = self.rows()
        self.assertAlmostEqual(row["return_pct"], 5.0, places=4)
        self.assertIsNone(row["unlabelable_reason"])

    def test_with_long_retention_the_oldest_retained_snapshot_is_the_boundary(self):
        # RADAR_SNAPSHOT_RETENTION_DAYS=3650: the prune cutoff is years back,
        # so the oldest retained snapshot (3 days ago, another pair) bounds it.
        oldest = self.NOW - timedelta(days=3)
        with mock.patch.object(config, "SNAPSHOT_RETENTION_DAYS", 3650):
            self.store.insert_spot_snapshot(spot_snap(oldest.isoformat(), 2000.0, asset="ETH", pair="XETHZUSD"))
            before_oldest = self.NOW - timedelta(days=5)
            in_gap = self.NOW - timedelta(days=2)
            # Lookup window ends exactly at the oldest snapshot: not provably impossible.
            edge = oldest - timedelta(minutes=15, seconds=120)
            for entry in (before_oldest, in_gap, edge):
                self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry.isoformat(), 100.0, [15])

            self.assertEqual(label_forward_returns(self.store, self.NOW), 0)

        marked = self.rows("unlabelable_reason IS NOT NULL")
        self.assertEqual([r["ts"] for r in marked], [before_oldest.isoformat()])
        self.assertEqual(
            sorted(r["ts"] for r in self.store.pending_forward_returns(self.NOW)),
            sorted([edge.isoformat(), in_gap.isoformat()]),
        )

    def test_rows_without_pair_or_entry_price_are_skipped_without_being_marked(self):
        old = self.NOW - timedelta(days=10)
        self.store.insert_forward_return("BTC", old.isoformat(), 15, None)  # Phase-1 row: no pair, no entry price
        self.store.create_forward_return_placeholders("BTC", "", old.isoformat(), 100.0, [15])
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", old.isoformat(), 0.0, [15])
        self.add_labelable(1, self.NOW - timedelta(hours=1))

        self.assertEqual([r["pair"] for r in self.store.pending_forward_returns(self.NOW)], ["XXBTZUSD"])
        self.assertEqual(label_forward_returns(self.store, self.NOW), 1)
        self.assertEqual(self.count("unlabelable_reason IS NOT NULL"), 0)
        self.assertEqual(self.count("return_pct IS NULL"), 3)

    def test_marking_never_overwrites_a_labeled_or_marked_row(self):
        from radar_v08.store import ForwardReturnUnlabelable

        reason = ForwardReturnUnlabelable.TARGET_OUTSIDE_SNAPSHOT_RETENTION
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", self.NOW.isoformat(), 100.0, [15, 60])
        labeled_id, pending_id = (r["id"] for r in self.rows())
        self.store.label_forward_return(labeled_id, 1.0, 2.0, -1.0, self.NOW.isoformat())

        self.assertFalse(self.store.mark_forward_return_unlabelable(labeled_id, reason, "t1"))
        self.assertTrue(self.store.mark_forward_return_unlabelable(pending_id, reason, "t1"))
        self.assertFalse(self.store.mark_forward_return_unlabelable(pending_id, reason, "t2"))
        with self.assertRaises(ValueError):
            self.store.mark_forward_return_unlabelable(pending_id, "target_outside_snapshot_retention", "t3")
        labeled, marked = self.rows()
        self.assertIsNone(labeled["unlabelable_reason"])
        self.assertEqual((marked["unlabelable_reason"], marked["unlabelable_at"]), (reason.value, "t1"))


if __name__ == "__main__":
    unittest.main()
