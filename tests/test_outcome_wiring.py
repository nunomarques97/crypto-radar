"""T4/T042 - the heartbeat registers one outcome subject per L2/L3 candidate.

Wires `radar_v08.adapters.outcome_store.register_subject` into `run_heartbeat`
(step 11, after the `if full:` block resolves), behind
`config.RADAR_OUTCOME_TRACKING_ENABLED` (D2). These tests run the real
heartbeat over the fake Kraken harness of `test_integrity_wiring` (no socket,
fake Qwen, temporary SQLite and log paths, deterministic clock) - the same
pattern `test_invocation_wiring.py` and `test_outbox.py` already use.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TESTS_DIR))
sys.path.insert(0, TESTS_DIR)

import test_integrity_wiring as wiring  # noqa: E402  (fake Kraken heartbeat harness)

from radar_v08 import config, heartbeat  # noqa: E402
from radar_v08.adapters.outcome_store import (  # noqa: E402
    OutcomeStoreError,
    OutcomeStoreFailure,
)
from radar_v08.http_client import GuardedSession  # noqa: E402

UTC = wiring.UTC
T0 = wiring.T0
# The fake Kraken Futures serverTime is pinned near T0 (see FakeKraken); a run
# starting elsewhere would fail the OC-1 clock check for an unrelated reason,
# so "the same identity, twice" reuses T0 as the frozen start of each cycle.
FROZEN = T0

PAIR_OF_ASSET = {"BTC": "XXBTZUSD", "ETH": "XETHZUSD"}


def _finalists_excluding(asset):
    """Wrap the real `select_finalists` (radar_v08/l3.py) and drop one asset from
    its result, so a candidate can be forced through l2_results/l3_inputs
    without ever becoming a finalist - the D3 "screener-only, not escalated"
    case on a `full` cycle. The real selection/ranking still runs first."""

    def wrapped(candidates):
        return [c for c in wiring.l3.select_finalists(candidates) if c.asset != asset]

    return wrapped


class OutcomeWiringBase(wiring.IntegrityWiringBase):
    def subject_rows(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM outcome_subjects ORDER BY pair")]

    def subject_count(self):
        return self.count("SELECT COUNT(*) FROM outcome_subjects")

    def cost_count(self):
        return self.count("SELECT COUNT(*) FROM outcome_subject_costs")

    def run_full_cycle(self, kraken, clock):
        return self.run_cycle(kraken, clock=clock)

    def run_non_full_cycle(self, kraken, clock):
        session = GuardedSession(1.0, 0, 0.0, http_session=kraken)
        return heartbeat.run_heartbeat(mode="TEST", store=self.store, full=False, session=session, clock=clock)


class TestSubjectsRegisteredPerCandidate(OutcomeWiringBase):
    def test_registers_one_subject_per_l2_l3_candidate_this_cycle(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        self.assertEqual(self.subject_count(), 0, "outcome_subjects must be empty before the cycle")

        output = self.run_full_cycle(kraken, wiring.StepClock())

        rows = self.subject_rows()
        # N = the number of non-warmup L2/L3 candidates this cycle (both BTC and
        # ETH reach the model in this fixture, per test_integrity_wiring's own
        # "valid case" test) - D3 scope, whether or not they are escalated.
        self.assertEqual(len(rows), 2, f"expected 2 subjects (BTC, ETH), got {len(rows)}: {rows}")
        self.assertEqual(sorted(r["pair"] for r in rows), ["XETHZUSD", "XXBTZUSD"])
        for row in rows:
            self.assertEqual(row["direction"], "LONG")  # the fake setup classifies LONG
            self.assertEqual(row["instrument_kind"], "spot")
            self.assertIsNotNone(row["entry_mid"])
            # Domain rule (outcomes.py:342-345): entry_observed_at can never be
            # *after* decision_as_of. The wiring reuses the same `now` for both
            # (T042_WIRING_DESIGN.md (b)), so this is the real, non-tautological
            # check - it would fail if a future change made the quote's own
            # timestamp flow through instead and drift later than decision_as_of.
            entry_observed_at = datetime.fromisoformat(row["entry_observed_at"])
            decision_as_of = datetime.fromisoformat(row["decision_as_of"])
            self.assertLessEqual(entry_observed_at, decision_as_of)
            self.assertEqual(row["evidence_missing"], "not_recorded")
            self.assertIsNone(row["evidence_id"])
            self.assertEqual(row["arm_missing"], "not_recorded")
            self.assertIsNone(row["arm"])
            # Both candidates are escalated and get a real RADAR_ALERT event in
            # this fixture, but `decision` still must be NOT_RECORDED: no
            # DecisionKind.INVOCATION is obtainable from the heartbeat under
            # today's architecture (T042_WIRING_DESIGN.md section (c) - invocation
            # claiming only happens in the disabled Claude Bridge dispatch path
            # and the separate worker.py process), and the acceptance criterion
            # requires exactly that before `decision` can be anything else.
            self.assertEqual(row["decision_missing"], "not_recorded")
            self.assertIsNone(row["decision_ref"])
            self.assertIsNone(row["decision_kind"])
        self.assertEqual(output["funnel"]["events_created"], 2, "both still get a real event - just not linked back")

        # Both finalists cleared the book, so T3's cost_scenarios has a real
        # COST_COMPLETE spot/LONG scenario for each - costs=() is never used
        # here; every subject gets its 4 horizons, and no partial sum leaks in.
        self.assertEqual(self.cost_count(), 8, "2 subjects x 4 horizons, all with a real cost scenario")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            statuses = {r["status"] for r in conn.execute("SELECT DISTINCT status FROM outcome_subject_costs")}
            sides = {r["side"] for r in conn.execute("SELECT DISTINCT side FROM outcome_subject_costs")}
        self.assertEqual(statuses, {"COST_COMPLETE"})
        self.assertEqual(sides, {"long"})

    def test_screener_only_non_full_cycle_still_registers_every_candidate(self):
        """D3: on a non-`full` cycle there is no L3 stage at all, so every
        candidate that cycle is, definitionally, screener-only - it still gets
        a subject with L2-only fields (no cost, no decision, no evidence)."""
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))

        output = self.run_non_full_cycle(kraken, wiring.StepClock())

        self.assertEqual(output["funnel"].get("events_created", 0), 0)
        rows = self.subject_rows()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["decision_missing"], "not_recorded")
            self.assertIsNone(row["decision_ref"])
            self.assertIsNone(row["decision_kind"])
        self.assertEqual(
            self.cost_count(),
            0,
            "no L3 stage on a non-full cycle -> no cost scenario -> costs=()",
        )

    def test_full_cycle_candidate_not_selected_as_finalist_still_gets_a_subject(self):
        """D3's central case (flagged as untested by T4-a1-review, nit 1): on a
        `full` cycle, a candidate that reaches l2_results/l3_inputs but that
        `select_finalists` does NOT pick still gets an outcome subject. This
        distinguishes "one subject per candidate" (N=2) from "one subject per
        finalist/event" (N=1, which the previous attempt's only full-cycle test
        could not tell apart, since both its candidates were finalists)."""
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))

        with mock.patch.object(heartbeat, "select_finalists", side_effect=_finalists_excluding("ETH")):
            output = self.run_full_cycle(kraken, wiring.StepClock())

        self.assertEqual(output["funnel"]["events_created"], 1, "only BTC reaches finalist/event this cycle")
        rows = self.subject_rows()
        self.assertEqual(len(rows), 2, "ETH still gets a subject even though it was never a finalist")
        self.assertEqual(sorted(r["pair"] for r in rows), ["XETHZUSD", "XXBTZUSD"])
        for row in rows:
            # decision is NOT_RECORDED either way (see the test above); the
            # distinguishing signal here is the cost link, which only a real
            # finalist with a T3 cost scenario can carry.
            self.assertEqual(row["decision_missing"], "not_recorded")
        self.assertEqual(
            self.cost_count(), 4, "1 finalist (BTC) x 4 horizons; the non-finalist (ETH) has costs=()"
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            costed_pairs = {
                r["pair"]
                for r in conn.execute(
                    "SELECT DISTINCT s.pair FROM outcome_subjects s "
                    "JOIN outcome_subject_costs c ON c.subject_id = s.subject_id"
                )
            }
        self.assertEqual(costed_pairs, {"XXBTZUSD"})


class TestIdempotentWithinTheSameIdentity(OutcomeWiringBase):
    def test_a_second_cycle_with_the_same_identity_does_not_duplicate_or_alter(self):
        """Two cycles that land on the exact same decision_as_of/entry/links
        (a frozen clock, the same open event) produce the identical subject_id
        the second time - register_subject's own no-op, never a second row."""
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))

        self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN))
        first_rows = self.subject_rows()
        self.assertEqual(len(first_rows), 2)

        self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN))
        second_rows = self.subject_rows()

        self.assertEqual(len(second_rows), 2, "the second cycle must not add rows")
        self.assertEqual(first_rows, second_rows, "the second cycle must not alter any stored row")


class TestDedupEventReuseNeverLeaksIntoDecision(OutcomeWiringBase):
    def setUp(self):
        super().setUp()
        # This oracle is "the cycle's now is the injected clock's first
        # reading", while the fake venue's serverTime stays pinned; the
        # corrected clock would re-anchor the cycle on that venue time. The
        # corrected clock is covered by tests/test_clock_correction.py.
        patcher = mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_full_cycle_reuses_the_open_event_but_the_new_subject_stays_not_recorded(self):
        """Production scenario this task's blocker 1 fixes (T4-a1-review): the
        heartbeat never calls `cooldown.record_send` (only the disabled Claude
        Bridge does - radar_v08/cooldown.py's own module docstring), so
        `check_cooldown` always allows, and with `CLAUDE_BRIDGE_DISPATCH_ENABLED
        = False` the first cycle's event stays PENDING. On an identical
        (asset, setup_type, direction, model_demand), the second cycle's
        `events.create_event_if_new` therefore hits the dedup branch
        (radar_v08/events.py:76-78, `store.find_open_event_by_dedup`,
        store.py:1092-1104) and reuses cycle 1's event_id - with a clock that
        keeps advancing (not frozen) between cycles, so the second cycle's
        subject has its own, later `decision_as_of`. The fix under test: that
        new subject's `decision` must stay NOT_RECORDED, never the reused,
        stale event_id from cycle 1.
        """
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        clock = wiring.StepClock()  # advances on every read - shared, not frozen, across both cycles

        self.run_full_cycle(kraken, clock)
        first_events = self.events()
        self.assertEqual(len(first_events), 2)
        first_event_ids = {e["asset"]: e["event_id"] for e in first_events}
        first_rows_by_pair = {r["pair"]: r for r in self.subject_rows()}
        self.assertEqual(len(first_rows_by_pair), 2)

        second_output = self.run_full_cycle(kraken, clock)  # same clock instance: time keeps moving forward

        second_events = self.events()
        self.assertEqual(len(second_events), 2, "the dedup branch reuses the existing rows, it inserts none")
        second_event_ids = {e["asset"]: e["event_id"] for e in second_events}
        self.assertEqual(
            second_event_ids, first_event_ids, "cycle 2 dedups onto the same still-PENDING events as cycle 1"
        )
        # "events_created" counts `event_by_asset` entries, dedup hits included
        # (heartbeat.py:975, `len(event_by_asset)`) - it stays 2 because both
        # assets are still associated with an (existing) event, not because
        # anything new was inserted. `self.events()` above is what proves that:
        # still 2 rows, same ids as cycle 1.
        self.assertEqual(second_output["funnel"]["events_created"], 2)

        second_rows = self.subject_rows()
        self.assertEqual(len(second_rows), 4, "cycle 2's moving clock gives 2 brand-new subject rows, not a no-op")
        new_rows_by_pair = {
            r["pair"]: r for r in second_rows if r["subject_id"] != first_rows_by_pair[r["pair"]]["subject_id"]
        }
        self.assertEqual(len(new_rows_by_pair), 2, "one new row per asset, distinct from cycle 1's")
        asset_of_pair = {pair: asset for asset, pair in PAIR_OF_ASSET.items()}
        for pair, row in new_rows_by_pair.items():
            self.assertNotEqual(
                row["decision_as_of"], first_rows_by_pair[pair]["decision_as_of"], "a genuinely new cycle, new `now`"
            )
            # The bug this fixes: decision must never carry the reused, stale
            # event_id from cycle 1 as if it were this cycle's own decision.
            self.assertEqual(row["decision_missing"], "not_recorded")
            self.assertIsNone(row["decision_ref"])
            self.assertIsNone(row["decision_kind"])
            self.assertNotEqual(row["decision_ref"], first_event_ids[asset_of_pair[pair]])


class TestSwitchOff(OutcomeWiringBase):
    def test_switch_off_writes_zero_and_leaves_existing_rows_intact(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))

        # One cycle with the switch on, to have pre-existing rows.
        self.run_full_cycle(kraken, wiring.StepClock())
        before = self.subject_rows()
        self.assertEqual(len(before), 2)

        with mock.patch.object(config, "RADAR_OUTCOME_TRACKING_ENABLED", False):
            self.run_full_cycle(kraken, wiring.StepClock(start=FROZEN))

        after = self.subject_rows()
        self.assertEqual(after, before, "the switch off writes nothing and never touches existing rows")


class TestRobustnessToARegistrationFailure(OutcomeWiringBase):
    def test_an_injected_registration_error_is_a_warning_and_the_cycle_still_completes(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        boom = OutcomeStoreError(OutcomeStoreFailure.SQLITE_ERROR, "injected failure for T4 robustness test")

        with mock.patch.object(self.store, "register_subject", side_effect=boom) as registered:
            output = self.run_full_cycle(kraken, wiring.StepClock())

        self.assertTrue(registered.called)
        # The cycle must complete and still produce its normal output and events,
        # never raise the injected error up through run_heartbeat.
        self.assertIn("funnel", output)
        self.assertEqual(output["funnel"]["events_created"], 2)
        self.assertEqual(self.events()[0]["asset"], "BTC")
        # Nothing is written for either candidate: the failure is per-candidate,
        # caught, logged as a warning, and the loop moves on.
        self.assertEqual(self.subject_count(), 0)


if __name__ == "__main__":
    unittest.main()
