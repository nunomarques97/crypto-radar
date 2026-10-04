import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.l2 import label_forward_returns
from radar_v08.store import ForwardReturnUnlabelable, SnapshotStore, SpotSnapshotInput

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


class TestForwardReturnGapMarking(unittest.TestCase):
    """In-retention gap rows are marked once a later
    snapshot proves their window can no longer fill, and a pass drains the
    backlog in bounded batches."""

    NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    TOL = 120.0
    MARGIN = 5.0
    GAP = "target_in_snapshot_gap"
    RETENTION = "target_outside_snapshot_retention"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "fwd.sqlite")
        self.store = SnapshotStore(self.path)
        self.addCleanup(self.store.close)
        self.patch_config(
            SNAPSHOT_RETENTION_DAYS=7,
            FORWARD_RETURN_LABEL_BATCH_LIMIT=500,
            FORWARD_RETURN_LOOKUP_TOLERANCE_SECONDS=self.TOL,
            FORWARD_RETURN_GAP_MARGIN_SECONDS=self.MARGIN,
            FORWARD_RETURN_MARK_GAPS_ENABLED=True,
            FORWARD_RETURN_MAX_BATCHES_PER_PASS=20,
            FORWARD_RETURN_PASS_BUDGET_SECONDS=2.0,
        )

    def patch_config(self, **values):
        for name, value in values.items():
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

    def snap(self, when, last=100.0, pair="XXBTZUSD"):
        asset = pair[1:4] if pair.startswith("X") else pair[:3]
        self.store.insert_spot_snapshot(spot_snap(when.isoformat(), last, asset=asset, pair=pair))

    def gap_row(self, entry, horizon=15):
        """A row inside retention with no snapshot of its pair near its target;
        the pair's history starts before it (3 days ago), so retention never applies."""
        if not getattr(self, "_history", False):
            self.snap(self.NOW - timedelta(days=3))
            self._history = True
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry.isoformat(), 100.0, [horizon])
        return entry + timedelta(minutes=horizon)

    def label(self, **kwargs):
        marked = {}
        labeled = label_forward_returns(self.store, self.NOW, marked, **kwargs)
        return labeled, marked

    def assert_no_return(self, rows):
        for r in rows:
            self.assertIsNone(r["return_pct"])
            self.assertIsNone(r["mfe_pct"])
            self.assertIsNone(r["mae_pct"])
            self.assertIsNone(r["labeled_at"])

    def test_gap_row_with_a_later_snapshot_of_any_pair_is_marked(self):
        self.gap_row(self.NOW - timedelta(days=2))
        self.snap(self.NOW, 2000.0, pair="XETHZUSD")  # current cycle stamp, another pair

        self.assertEqual(self.label(), (0, {self.RETENTION: 0, self.GAP: 1}))
        (row,) = self.rows()
        self.assertEqual((row["unlabelable_reason"], row["unlabelable_at"]), (self.GAP, self.NOW.isoformat()))
        self.assert_no_return([row])
        self.assertEqual(self.store.pending_forward_returns(self.NOW), [])

    def test_margin_boundary_is_strict(self):
        target = self.gap_row(self.NOW - timedelta(days=2))
        boundary = target + timedelta(seconds=self.TOL + self.MARGIN)

        # After the window but inside the margin, then exactly at the boundary: not proof.
        for later in (target + timedelta(seconds=self.TOL + 1), boundary):
            self.snap(later, 2000.0, pair="XETHZUSD")
            self.assertEqual(self.label(), (0, {self.RETENTION: 0, self.GAP: 0}))
            self.assertEqual(self.count("unlabelable_reason IS NOT NULL"), 0)
            self.assertEqual(len(self.store.pending_forward_returns(self.NOW)), 1)

        self.snap(boundary + timedelta(microseconds=1), 2000.0, pair="XETHZUSD")
        self.assertEqual(self.label(), (0, {self.RETENTION: 0, self.GAP: 1}))
        self.assertEqual(self.count("unlabelable_reason = ?", (self.GAP,)), 1)

    def test_gap_row_without_a_later_snapshot_is_retried_until_one_exists(self):
        self.gap_row(self.NOW - timedelta(days=2))
        for _ in range(2):
            self.assertEqual(self.label(), (0, {self.RETENTION: 0, self.GAP: 0}))
            self.assertEqual(len(self.store.pending_forward_returns(self.NOW)), 1)
        self.assertEqual(self.count("unlabelable_reason IS NOT NULL"), 0)

        self.snap(self.NOW)  # the next cycle writes its snapshot: the window can no longer fill
        self.assertEqual(self.label(), (0, {self.RETENTION: 0, self.GAP: 1}))

    def test_row_with_a_snapshot_in_its_window_is_labeled_exactly_as_with_the_switch_off(self):
        results = {}
        for enabled in (True, False):
            with self.subTest(mark_gaps=enabled), mock.patch.object(config, "FORWARD_RETURN_MARK_GAPS_ENABLED", enabled):
                store_path = os.path.join(self.tmp.name, f"window-{enabled}.sqlite")
                store = SnapshotStore(store_path)
                try:
                    entry = self.NOW - timedelta(days=2)
                    store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry.isoformat(), 100.0, [15])
                    for minutes, last in ((5, 115.0), (10, 95.0)):
                        store.insert_spot_snapshot(spot_snap((entry + timedelta(minutes=minutes)).isoformat(), last))
                    # 100 s after the target: inside the window, not the exact target.
                    store.insert_spot_snapshot(spot_snap((entry + timedelta(minutes=15, seconds=100)).isoformat(), 105.0))
                    store.insert_spot_snapshot(spot_snap(self.NOW.isoformat(), 90.0))  # later snapshot exists
                    marked = {}
                    self.assertEqual(label_forward_returns(store, self.NOW, marked), 1)
                    self.assertEqual(marked, {self.RETENTION: 0, self.GAP: 0})
                finally:
                    store.close()
                conn = sqlite3.connect(store_path)
                conn.row_factory = sqlite3.Row
                (row,) = conn.execute("SELECT * FROM forward_returns").fetchall()
                conn.close()
                self.assertIsNone(row["unlabelable_reason"])
                results[enabled] = (row["return_pct"], row["mfe_pct"], row["mae_pct"], row["labeled_at"])
        self.assertEqual(results[True], results[False])
        return_pct, mfe_pct, mae_pct, labeled_at = results[True]
        self.assertAlmostEqual(return_pct, 5.0, places=4)
        self.assertAlmostEqual(mfe_pct, 15.0, places=4)
        self.assertAlmostEqual(mae_pct, -5.0, places=4)
        self.assertEqual(labeled_at, self.NOW.isoformat())

    def test_retention_reason_takes_precedence_over_the_gap_reason(self):
        old = self.NOW - timedelta(days=10)  # window before the 7-day prune cutoff
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", old.isoformat(), 100.0, [15])
        self.snap(self.NOW)  # a later snapshot also exists

        self.assertEqual(self.label(), (0, {self.RETENTION: 1, self.GAP: 0}))
        (row,) = self.rows()
        self.assertEqual(row["unlabelable_reason"], self.RETENTION)

    def test_marked_out_lists_every_reason_with_zeros_and_replaces_stale_values(self):
        marked = {self.GAP: 99, self.RETENTION: 7}
        self.assertEqual(label_forward_returns(self.store, self.NOW, marked), 0)
        self.assertEqual(marked, {reason.value: 0 for reason in ForwardReturnUnlabelable})
        self.assertEqual(label_forward_returns(self.store, self.NOW), 0)  # marked_out stays optional

    def test_batched_marking_never_overwrites_deletes_or_writes_a_return(self):
        self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", self.NOW.isoformat(), 100.0, [15, 60, 240])
        labeled_id, retained_id, pending_id = (r["id"] for r in self.rows())
        self.store.label_forward_return(labeled_id, 1.0, 2.0, -1.0, "t0")
        self.store.mark_forward_return_unlabelable(retained_id, ForwardReturnUnlabelable.TARGET_OUTSIDE_SNAPSHOT_RETENTION, "t0")

        gap = ForwardReturnUnlabelable.TARGET_IN_SNAPSHOT_GAP
        self.assertEqual(self.store.mark_forward_returns_unlabelable([labeled_id, retained_id, pending_id], gap, "t1"), 1)
        self.assertEqual(self.store.mark_forward_returns_unlabelable([pending_id], gap, "t2"), 0)
        self.assertEqual(self.store.mark_forward_returns_unlabelable([], gap, "t3"), 0)
        with self.assertRaises(ValueError):
            self.store.mark_forward_returns_unlabelable([pending_id], self.GAP, "t4")
        labeled, retained, pending = self.rows()
        self.assertEqual((labeled["return_pct"], labeled["unlabelable_reason"]), (1.0, None))
        self.assertEqual((retained["unlabelable_reason"], retained["unlabelable_at"]), (self.RETENTION, "t0"))
        self.assertEqual((pending["unlabelable_reason"], pending["unlabelable_at"]), (self.GAP, "t1"))
        self.assert_no_return([retained, pending])

    def add_gap_rows(self, entries, start, step=timedelta(minutes=1)):
        for i in range(entries):
            self.gap_row(start + i * step)

    def counting_batches(self):
        return mock.patch.object(self.store, "pending_forward_returns", wraps=self.store.pending_forward_returns)

    def test_a_pass_keeps_requesting_full_batches_up_to_the_batch_cap(self):
        self.patch_config(FORWARD_RETURN_LABEL_BATCH_LIMIT=10, FORWARD_RETURN_MAX_BATCHES_PER_PASS=3)
        self.add_gap_rows(45, self.NOW - timedelta(days=2))
        self.snap(self.NOW)

        with self.counting_batches() as fetch:
            self.assertEqual(self.label(monotonic=lambda: 0.0), (0, {self.RETENTION: 0, self.GAP: 30}))
        self.assertEqual(fetch.call_count, 3)
        with self.counting_batches() as fetch:  # 15 left: one full batch, then a short one ends the pass
            self.assertEqual(self.label(monotonic=lambda: 0.0), (0, {self.RETENTION: 0, self.GAP: 15}))
        self.assertEqual(fetch.call_count, 2)

    def test_no_new_batch_starts_once_the_time_budget_is_spent(self):
        self.patch_config(FORWARD_RETURN_LABEL_BATCH_LIMIT=10, FORWARD_RETURN_PASS_BUDGET_SECONDS=2.0)
        self.add_gap_rows(100, self.NOW - timedelta(days=2))
        self.snap(self.NOW)
        readings = iter([100.0, 100.5, 101.0, 101.99, 102.0, 500.0])  # pass start, then before each new batch
        calls = []

        def clock():
            calls.append(next(readings))
            return calls[-1]

        with self.counting_batches() as fetch:
            self.assertEqual(self.label(monotonic=clock), (0, {self.RETENTION: 0, self.GAP: 40}))
        # Batches 2-4 start at 0.5 s, 1.0 s and 1.99 s; at 2.0 s the budget is spent.
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(calls, [100.0, 100.5, 101.0, 101.99, 102.0])

    def test_a_full_batch_that_moves_nothing_ends_the_pass(self):
        self.patch_config(FORWARD_RETURN_LABEL_BATCH_LIMIT=10)
        self.add_gap_rows(25, self.NOW - timedelta(days=2))  # no later snapshot: all stay pending

        with self.counting_batches() as fetch:
            self.assertEqual(self.label(monotonic=lambda: 0.0), (0, {self.RETENTION: 0, self.GAP: 0}))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.count("unlabelable_reason IS NOT NULL"), 0)

    def test_switch_off_keeps_retention_marking_and_one_batch_per_pass(self):
        self.patch_config(FORWARD_RETURN_MARK_GAPS_ENABLED=False, FORWARD_RETURN_LABEL_BATCH_LIMIT=10)
        old = self.NOW - timedelta(days=10)
        for i in range(4):  # outside retention
            self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", (old + timedelta(seconds=i)).isoformat(), 100.0, [15])
        self.add_gap_rows(20, self.NOW - timedelta(days=2))
        self.snap(self.NOW)
        gap_ids = [r["id"] for r in self.rows("ts > ?", ((self.NOW - timedelta(days=7)).isoformat(),))]

        with self.counting_batches() as fetch:
            self.assertEqual(self.label(monotonic=lambda: 0.0), (0, {self.RETENTION: 4, self.GAP: 0}))
            for _ in range(3):
                self.assertEqual(self.label(monotonic=lambda: 0.0), (0, {self.RETENTION: 0, self.GAP: 0}))
        self.assertEqual(fetch.call_count, 4)  # one batch per pass
        self.assertEqual(self.count("unlabelable_reason = ?", (self.GAP,)), 0)
        self.assertEqual(self.count("unlabelable_reason = ?", (self.RETENTION,)), 4)
        self.assertEqual(sorted(r["id"] for r in self.store.pending_forward_returns(self.NOW, limit=100)), gap_ids)

        # Back on, the same rows drain.
        with mock.patch.object(config, "FORWARD_RETURN_MARK_GAPS_ENABLED", True):
            self.assertEqual(self.label(monotonic=lambda: 0.0), (0, {self.RETENTION: 0, self.GAP: 20}))

    # -- backlog drain ------------------------------------------------------

    DRAIN_ENTRIES = 15_000  # x 3 horizons = 45,000 pending in-gap rows
    DRAIN_PAIRS = ("XXBTZUSD", "XETHZUSD", "SOLUSD", "ADAUSD", "DOTUSD")
    TODAY_ROWS = 30

    def build_drain_fixture(self):
        """45,000 in-gap rows over five past days (inside the 7-day retention),
        each pair with history 6.5 days back and the current cycle's snapshot,
        then TODAY_ROWS labelable rows from today with a snapshot at their target."""
        for pair in self.DRAIN_PAIRS:
            self.snap(self.NOW - timedelta(days=6, hours=12), pair=pair)
            self.snap(self.NOW, pair=pair)
        start = self.NOW - timedelta(days=6)
        step = timedelta(days=5) / self.DRAIN_ENTRIES
        placeholders = [
            ("A", self.DRAIN_PAIRS[i % len(self.DRAIN_PAIRS)], (start + i * step).isoformat(), horizon, 100.0)
            for i in range(self.DRAIN_ENTRIES)
            for horizon in (15, 60, 240)
        ]
        conn = sqlite3.connect(self.path)
        try:
            conn.executemany(
                "INSERT INTO forward_returns (asset, pair, ts, horizon_minutes, return_pct, entry_price) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                placeholders,
            )
            conn.commit()
        finally:
            conn.close()
        for i in range(self.TODAY_ROWS):
            entry = self.NOW - timedelta(hours=3) + timedelta(minutes=i)
            self.store.create_forward_return_placeholders("BTC", "XXBTZUSD", entry.isoformat(), 100.0, [15])
            self.snap(entry + timedelta(minutes=15), 110.0)
        return len(placeholders)

    def drain(self, max_passes=10):
        """Run passes until nothing is pending; per pass: rows moved, seconds, longest batch."""
        passes = []
        real_fetch = self.store.pending_forward_returns
        for _ in range(max_passes):
            batch_starts = []

            def fetch(now, limit=500):
                batch_starts.append(time.perf_counter())
                return real_fetch(now, limit=limit)

            with mock.patch.object(self.store, "pending_forward_returns", side_effect=fetch):
                started = time.perf_counter()
                labeled, marked = self.label()
                ended = time.perf_counter()
            bounds = batch_starts + [ended]
            passes.append({
                "labeled": labeled,
                "marked": marked,
                "rows": labeled + sum(marked.values()),
                "batches": len(batch_starts),
                "seconds": ended - started,
                "longest_batch": max(b - a for a, b in zip(bounds, bounds[1:])),
            })
            if not real_fetch(self.NOW, limit=1):
                break
        return passes

    def test_a_45000_row_gap_backlog_drains_in_few_bounded_passes(self):
        in_gap = self.build_drain_fixture()
        self.assertEqual(in_gap, 45_000)
        total = self.count()

        passes = self.drain()
        stats = [(p["rows"], p["batches"], round(p["seconds"], 3)) for p in passes]
        self.assertEqual(self.store.pending_forward_returns(self.NOW, limit=1), [], f"not drained: {stats}")
        self.assertLessEqual(len(passes), 10, stats)
        cap_rows = config.FORWARD_RETURN_MAX_BATCHES_PER_PASS * config.FORWARD_RETURN_LABEL_BATCH_LIMIT
        for p in passes:
            self.assertLessEqual(p["rows"], cap_rows, stats)
            self.assertLessEqual(p["batches"], config.FORWARD_RETURN_MAX_BATCHES_PER_PASS, stats)
            self.assertLessEqual(p["seconds"], config.FORWARD_RETURN_PASS_BUDGET_SECONDS + p["longest_batch"], stats)
            self.assertEqual(p["marked"][self.RETENTION], 0)
        self.assertEqual(sum(p["marked"][self.GAP] for p in passes), in_gap)
        self.assertEqual(sum(p["labeled"] for p in passes), self.TODAY_ROWS)

        self.assertEqual(self.count(), total)  # nothing deleted
        self.assertEqual(self.count("unlabelable_reason = ?", (self.GAP,)), in_gap)
        self.assertEqual(self.count("unlabelable_reason IS NOT NULL AND (return_pct IS NOT NULL OR labeled_at IS NOT NULL)"), 0)
        today = self.rows("ts >= ?", ((self.NOW - timedelta(days=1)).isoformat(),))
        self.assertEqual(len(today), self.TODAY_ROWS)
        for r in today:
            self.assertIsNone(r["unlabelable_reason"])
            self.assertAlmostEqual(r["return_pct"], 10.0, places=4)

    def test_with_the_switch_off_the_same_backlog_never_drains(self):
        self.patch_config(FORWARD_RETURN_MARK_GAPS_ENABLED=False)
        in_gap = self.build_drain_fixture()
        with self.counting_batches() as fetch:
            for _ in range(3):
                self.assertEqual(self.label(), (0, {self.RETENTION: 0, self.GAP: 0}))
        self.assertEqual(fetch.call_count, 3)  # one batch per pass, as at HEAD
        self.assertEqual(self.count("unlabelable_reason IS NOT NULL"), 0)
        self.assertEqual(self.count("return_pct IS NULL"), in_gap + self.TODAY_ROWS)


if __name__ == "__main__":
    unittest.main()
