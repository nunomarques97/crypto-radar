"""T5/T042 - the heartbeat labels matured outcome horizons each cycle.

Step 9b wires `radar_v08.adapters.outcome_store.label_due_outcomes` into
`run_heartbeat`, right after the legacy `label_forward_returns` call and
before this cycle's own new subjects are registered (step 11, T4). Both
calls sit behind the same `config.RADAR_OUTCOME_TRACKING_ENABLED` switch
(D2). These tests reuse the fake-Kraken heartbeat harness of
`test_integrity_wiring` and the outcome-subject helpers of
`test_outcome_wiring` (same pattern, no socket, deterministic clock,
disposable SQLite).
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TESTS_DIR))
sys.path.insert(0, TESTS_DIR)

import test_integrity_wiring as wiring  # noqa: E402  (fake Kraken heartbeat harness)
import test_outcome_wiring as owiring  # noqa: E402  (T4's OutcomeWiringBase, FROZEN)

from radar_v08 import config, heartbeat  # noqa: E402
from radar_v08.domain.outcomes import HORIZONS  # noqa: E402
from radar_v08.store import SpotSnapshotInput  # noqa: E402

FROZEN = owiring.FROZEN  # == test_integrity_wiring.T0, the fake futures serverTime anchor
D = Decimal


class LabelWiringBase(owiring.OutcomeWiringBase):
    """Adds outcome_labels helpers on top of T4's subject/cost helpers."""

    def label_rows(self, horizon=None):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM outcome_labels"
            args = ()
            if horizon is not None:
                sql += " WHERE horizon = ?"
                args = (horizon,)
            sql += " ORDER BY subject_id, target_at"
            return [dict(row) for row in conn.execute(sql, args)]

    def label_count(self):
        return self.count("SELECT COUNT(*) FROM outcome_labels")

    def insert_future_snapshot(self, pair, asset, ts, price):
        """Writes one spot_snapshots row stamped `ts`, bypassing the heartbeat's
        own ticker fetch - used only to plant a quote a real cycle could not
        yet have observed, to prove the labeler never reaches for it."""
        self.store.insert_spot_snapshots_batch(
            [
                SpotSnapshotInput(
                    asset=asset, pair=pair, quote="USD", ts=ts.isoformat(),
                    last=price, bid=price - 0.5, ask=price + 0.5,
                    bid_size=1.0, ask_size=1.0, volume_today=0.0, volume_24h=0.0,
                    vwap_today=price, vwap_24h=price, trades_today=0, trades_24h=0,
                    high_today=price, low_today=price, high_24h=price, low_24h=price,
                    open_today=price, status="online",
                )
            ]
        )

    def no_new_subjects(self):
        """Context manager-ish helper: mocks register_subject to a no-op so a
        follow-up cycle only exercises step 9's labeler, never adding rows to
        outcome_subjects/outcome_subject_costs."""
        return mock.patch.object(self.store, "register_subject", return_value=False)

    def run_synced_cycle(self, kraken, start, full=True):
        """A later cycle whose local clock has moved far past the fixture's
        pinned futures serverTime would otherwise fail OC-1's clock check
        (`clock_uncertainty_exceeded`) and suspend L1/L2 consumption - harmless
        for the labeling counters under test (spot_snapshots persistence is
        unconditional, per heartbeat.py step 6), but it would make the
        candidate/event/cost side of these fixtures unrealistic. Moving the
        fake venue's serverTime along with the injected clock (same +3 ms
        offset `FakeKraken.__init__` itself uses) keeps every stage a normal,
        passing cycle."""
        kraken.futures_tickers["serverTime"] = wiring._iso_z(start + timedelta(milliseconds=3))
        clock = wiring.StepClock(start=start)
        if full:
            return self.run_full_cycle(kraken, clock)
        return self.run_non_full_cycle(kraken, clock)


class TestLabelsGrowByHorizonStageThenRepeatIsANoOp(LabelWiringBase):
    def test_outcome_labels_grow_in_stages_and_a_second_pass_at_the_same_instant_writes_zero(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        self.assertEqual(self.label_count(), 0, "outcome_labels must be empty before any cycle")

        first_output = self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN))
        self.assertEqual(self.subject_count(), 2, "cycle 1 registers BTC and ETH")
        # Step 9 runs before step 11's registration: this cycle's own new
        # subjects are never seen by this same cycle's label_due_outcomes call.
        self.assertEqual(self.label_count(), 0)
        first_funnel = first_output["funnel"]
        self.assertEqual(
            (
                first_funnel["outcome_labels_considered"], first_funnel["outcome_labels_available"],
                first_funnel["outcome_labels_unavailable"], first_funnel["outcome_labels_not_mature"],
            ),
            (0, 0, 0, 0),
        )

        # One minute past each horizon's own target, well inside the config
        # tolerance window (120 s) - four stages, 15m/1h/4h/24h.
        stage_starts = [FROZEN + h.duration + timedelta(minutes=1) for h in HORIZONS]
        running_counts = [self.label_count()]
        stage_funnels = []
        with self.no_new_subjects():
            for start in stage_starts:
                output = self.run_synced_cycle(kraken, start)
                running_counts.append(self.label_count())
                stage_funnels.append(output["funnel"])

        self.assertEqual(
            running_counts, [0, 2, 4, 6, 8],
            f"expected 2 new label rows per horizon stage (2 subjects x 4 horizons), got {running_counts}",
        )
        self.assertEqual(self.subject_count(), 2, "the mocked registration must add nothing across stages")

        # Each stage matures exactly its own horizon for both subjects, and
        # nothing else (the already-labeled horizons are excluded from `due`).
        for horizon, funnel in zip(HORIZONS, stage_funnels):
            with self.subTest(horizon=horizon.value):
                self.assertEqual(funnel["outcome_labels_considered"], 2)
                self.assertEqual(funnel["outcome_labels_available"] + funnel["outcome_labels_unavailable"], 2)
                self.assertEqual(funnel["outcome_labels_not_mature"], 0)
                # The counters reach the run record alongside forward_returns_labeled
                # (the legacy labeler's own count is out of scope here - see
                # TestLegacyForwardReturnsLabelerIsUnaffected for its regression proof).
                self.assertIn("forward_returns_labeled", funnel)

        run_records_with_labels = [
            r for r in self.run_records if r.get("outcome_labels_considered") not in (None, 0)
        ]
        self.assertEqual(len(run_records_with_labels), 4, "one run record per maturing stage")
        for record in run_records_with_labels:
            self.assertIn("forward_returns_labeled", record)
            self.assertIn("outcome_labels_available", record)
            self.assertIn("outcome_labels_unavailable", record)
            self.assertIn("outcome_labels_not_mature", record)

        before_repeat = self.label_rows()
        with self.no_new_subjects():
            repeat_output = self.run_synced_cycle(kraken, stage_starts[-1])
        after_repeat = self.label_rows()

        self.assertEqual(self.label_count(), 8, "a second pass at the same instant writes nothing new")
        self.assertEqual(before_repeat, after_repeat, "no existing label row is altered by the repeat pass")
        repeat_funnel = repeat_output["funnel"]
        self.assertEqual(
            (
                repeat_funnel["outcome_labels_considered"], repeat_funnel["outcome_labels_available"],
                repeat_funnel["outcome_labels_unavailable"], repeat_funnel["outcome_labels_not_mature"],
            ),
            (0, 0, 0, 0),
        )


class TestSwitchOffLabelsZeroAndLeavesRowsIntact(LabelWiringBase):
    def test_switch_off_writes_no_new_labels_and_leaves_existing_ones_untouched(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN))
        with self.no_new_subjects():
            self.run_synced_cycle(kraken, FROZEN + timedelta(minutes=16))
        before = self.label_rows()
        self.assertEqual(len(before), 2, "both subjects' 15m horizon matured with the switch on")

        with self.no_new_subjects(), mock.patch.object(config, "RADAR_OUTCOME_TRACKING_ENABLED", False):
            output = self.run_synced_cycle(kraken, FROZEN + timedelta(hours=1, minutes=1))

        after = self.label_rows()
        self.assertEqual(after, before, "the switch off writes nothing and never touches existing rows")
        funnel = output["funnel"]
        self.assertEqual(
            (
                funnel["outcome_labels_considered"], funnel["outcome_labels_available"],
                funnel["outcome_labels_unavailable"], funnel["outcome_labels_not_mature"],
            ),
            (0, 0, 0, 0),
        )


class TestRobustnessToALabelingFailure(LabelWiringBase):
    def test_an_injected_labeling_error_is_a_warning_and_the_cycle_still_completes(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN))
        boom = RuntimeError("injected failure for T5 robustness test")

        with self.no_new_subjects(), mock.patch.object(heartbeat.outcome_store, "label_due_outcomes", side_effect=boom) as labeler:
            output = self.run_synced_cycle(kraken, FROZEN + timedelta(minutes=16))

        self.assertTrue(labeler.called)
        self.assertIn("funnel", output)
        self.assertIn("candidates", output)  # the cycle produced its normal output, not a half-built one
        funnel = output["funnel"]
        self.assertEqual(
            (
                funnel["outcome_labels_considered"], funnel["outcome_labels_available"],
                funnel["outcome_labels_unavailable"], funnel["outcome_labels_not_mature"],
            ),
            (0, 0, 0, 0),
            "the injected failure leaves the counters at their safe default, never a partial count",
        )
        self.assertEqual(self.label_count(), 0, "nothing is written when the labeler call itself blows up")


class TestLegacyForwardReturnsLabelerIsUnaffected(LabelWiringBase):
    def test_forward_returns_labeled_is_identical_whether_the_switch_is_on_or_off(self):
        """Regression: step 9's legacy `label_forward_returns(store, now)` call
        is untouched by this task - same call, same arguments, same position
        before the new step 9b. Toggling D2's switch must never change its
        count (a fresh store has nothing pending either way: 0)."""
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))

        on_output = self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN))
        self.assertEqual(on_output["funnel"]["forward_returns_labeled"], 0)

        with mock.patch.object(config, "RADAR_OUTCOME_TRACKING_ENABLED", False):
            off_output = self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN))

        self.assertEqual(off_output["funnel"]["forward_returns_labeled"], 0)
        self.assertEqual(
            on_output["funnel"]["forward_returns_labeled"], off_output["funnel"]["forward_returns_labeled"]
        )


class TestHonestyNoRecordedCostMeansNetUnavailable(LabelWiringBase):
    def test_a_screener_only_subject_matures_with_net_unavailable_typed_never_zero_or_gross(self):
        """D3/screener-only (T4): a non-`full` cycle never has an L3 cost
        scenario, so `costs=()` for every subject registered there. FakeKraken's
        price never moves, so the market itself really is flat (gross_markout a
        real, measured 0) - the honesty property under test is that net_markout
        still comes back None with a typed reason, never silently copying that
        real zero and never equal to gross."""
        kraken = wiring.FakeKraken(assets=("BTC",))
        self.run_non_full_cycle(kraken, wiring.StepClock(start=FROZEN))
        self.assertEqual(self.cost_count(), 0, "non-full cycle: no L3 stage, so costs=()")

        with self.no_new_subjects():
            self.run_synced_cycle(kraken, FROZEN + timedelta(minutes=16), full=False)

        rows = self.label_rows(horizon="15m")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "AVAILABLE", "the market quote itself is real and available")
        self.assertEqual(D(row["market_return"]), D("0"), "FakeKraken's price never moves between cycles")
        self.assertEqual(D(row["gross_markout"]), D("0"), "gross tracks the real (flat) market move")
        self.assertIsNone(row["net_markout"])
        self.assertEqual(row["net_missing"], "cost_not_recorded")
        self.assertNotEqual(row["net_markout"], row["gross_markout"])
        self.assertNotEqual(row["net_missing"], None)


class TestHonestyQuoteMissingAtHorizonWhenTheWindowCloses(LabelWiringBase):
    def setUp(self):
        super().setUp()
        # This oracle is "the cycle's now is the injected clock's first
        # reading", while the fake venue's serverTime stays pinned; the
        # corrected clock would re-anchor the cycle on that venue time. The
        # corrected clock is covered by tests/test_clock_correction.py.
        patcher = mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_window_that_closes_with_no_quote_is_unavailable_quote_missing_at_the_windows_end(self):
        kraken = wiring.FakeKraken(assets=("BTC",))
        self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN))
        self.assertEqual(self.label_count(), 0)

        # Skips straight past the 15m target + config tolerance (120 s): the
        # next real snapshot this cycle produces lands outside the window, and
        # nothing else was ever written inside it.
        window_close = FROZEN + timedelta(minutes=15) + timedelta(seconds=config.FORWARD_RETURN_LOOKUP_TOLERANCE_SECONDS)
        with self.no_new_subjects():
            self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN + timedelta(minutes=20)))

        rows = self.label_rows(horizon="15m")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "UNAVAILABLE")
        self.assertEqual(row["market_missing"], "quote_missing_at_horizon")
        self.assertIsNone(row["market_return"])
        self.assertIsNone(row["exit_mid"])
        self.assertEqual(datetime.fromisoformat(row["label_available_at"]), window_close)


class TestHonestyNoLabelEverUsesAQuoteAfterNow(LabelWiringBase):
    def setUp(self):
        super().setUp()
        # This oracle is "the cycle's now is the injected clock's first
        # reading", while the fake venue's serverTime stays pinned; the
        # corrected clock would re-anchor the cycle on that venue time. The
        # corrected clock is covered by tests/test_clock_correction.py.
        patcher = mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_snapshot_stamped_after_the_labeling_cycles_own_now_is_never_the_exit_quote(self):
        kraken = wiring.FakeKraken(assets=("BTC",))
        self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN))
        pair = self.subject_rows()[0]["pair"]
        self.assertEqual(pair, "XXBTZUSD")

        labeling_now = FROZEN + timedelta(minutes=15, seconds=30)  # inside the 15m window, due
        poison_ts = FROZEN + timedelta(minutes=16)  # inside the 15m window too, but AFTER labeling_now
        poison_price = 999999.0
        self.insert_future_snapshot(pair, "BTC", poison_ts, poison_price)

        with self.no_new_subjects():
            self.run_full_cycle(kraken, wiring.StepClock(start=labeling_now))

        rows = self.label_rows(horizon="15m")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "AVAILABLE", "this cycle's own real quote (at labeling_now) is in range")
        exit_mid = D(row["exit_mid"])
        self.assertNotAlmostEqual(float(exit_mid), poison_price, delta=1000.0)
        self.assertAlmostEqual(float(exit_mid), 60000.0, delta=1.0)  # BTC's real fixture price
        exit_observed_at = datetime.fromisoformat(row["exit_observed_at"])
        self.assertLessEqual(exit_observed_at, labeling_now)
        self.assertLess(exit_observed_at, poison_ts, "the poisoned, later-timestamped quote was never used")


if __name__ == "__main__":
    unittest.main()
