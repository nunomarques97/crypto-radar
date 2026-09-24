"""Pure tests for radar_v08.workflow.scheduler (task T032a, policy part of T032).

No SQLite, no files, no network, no wall clock, no model: a fake clock is injected and
every rule of OC-1 sections 1 to 3 is asserted one by one, with a perturbation test
showing that each configured limit changes behaviour.
"""

import ast
import hashlib
import os
import random
import sys
import unittest
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar_v08.workflow as workflow_package
from radar_v08.domain import invocation as invocation_domain
from radar_v08.domain.integrity import InstrumentKind
from radar_v08.domain.invocation import Direction, InvocationIdentity
from radar_v08.workflow import scheduler as sch
from radar_v08.workflow.scheduler import (
    OC1_HORIZON_LIMITS,
    OC1_ROLE_PROFILES,
    TERMINAL_ITEM_STATES,
    AdmissionStatus,
    Band,
    CallRefusal,
    DeadlineScheduler,
    Horizon,
    HorizonLimits,
    IdleReason,
    ItemState,
    OpportunityVersion,
    Role,
    SchedulerError,
    SchedulerFailure,
    SchedulerPolicy,
    StaleReason,
    TokenBasis,
    TokenBound,
    Transition,
    utf8_byte_upper_bound,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, at=T0):
        self.at = at

    def current(self):
        return self.at

    def at_seconds(self, seconds):
        self.at = T0 + timedelta(seconds=seconds)


def identity(instrument="XBTUSD", setup="BREAKOUT", direction=Direction.LONG, seed=None, policy_version="OC-1"):
    digest = hashlib.sha256((seed or f"{instrument}/{setup}/{direction.value}").encode()).hexdigest()
    return InvocationIdentity("kraken", InstrumentKind.SPOT, instrument, setup, direction, f"sha256:{digest}", policy_version)


def version(item_id, *, score=80.0, horizon=Horizon.MIN_15, sealed=0, instrument=None, setup="BREAKOUT",
            direction=Direction.LONG, seed=None, policy_version="OC-1", integrity_valid=True,
            deterministic=True, roles=(Role.SCREENER,), origin=None):
    return OpportunityVersion(
        item_id=item_id,
        identity=identity(instrument or item_id, setup, direction, seed or item_id, policy_version),
        horizon=horizon,
        opportunity_score=score,
        sealed_at=T0 + timedelta(seconds=sealed),
        integrity_valid=integrity_valid,
        deterministic_path_eligible=deterministic,
        requested_roles=roles,
        origin_sealed_at=None if origin is None else T0 + timedelta(seconds=origin),
    )


def tokens(amount):
    return TokenBound(amount, TokenBasis.TOKENIZER_COUNT)


def make(policy=None, at=0):
    clock = FakeClock(T0 + timedelta(seconds=at))
    return DeadlineScheduler(policy or SchedulerPolicy(), clock), clock


def states(transitions):
    return [(t.item_id, t.state) for t in transitions]


def run_to_dispatch(scheduler, item):
    result = scheduler.admit(item)
    assert result.status is AdmissionStatus.ADMITTED, result
    return scheduler.dispatch()


class TestPolicyDefaults(unittest.TestCase):
    def test_oc1_values(self):
        policy = SchedulerPolicy()
        self.assertEqual(policy.max_queued, 16)
        self.assertEqual((policy.high_band_min, policy.eligible_band_min), (70.0, 50.0))
        self.assertEqual(policy.fair_slot_every, 4)
        self.assertEqual(policy.finalization_reserve_seconds, 10)
        self.assertEqual((policy.max_invocations, policy.max_repairs), (4, 1))
        self.assertEqual((policy.max_prompt_tokens, policy.max_output_tokens), (15000, 4500))
        expected = {
            Horizon.MIN_15: (60, 15),
            Horizon.HOUR_1: (120, 30),
            Horizon.HOUR_4: (180, 45),
            Horizon.HOUR_24: (180, 45),
        }
        self.assertEqual(
            {h: (v.analysis_deadline_seconds, v.max_queued_age_seconds) for h, v in policy.horizon_limits.items()},
            expected,
        )
        screener = policy.role_profiles[Role.SCREENER]
        self.assertEqual((screener.hard_timeout_seconds, screener.max_input_tokens, screener.output_cap_tokens), (30, 2800, 768))
        self.assertTrue(screener.enabled)
        self.assertFalse(policy.role_profiles[Role.CHALLENGER].enabled)
        self.assertFalse(policy.role_profiles[Role.DEEP_ANALYST].enabled)

    def test_enabling_challenger_or_deep_is_refused(self):
        for role in (Role.CHALLENGER, Role.DEEP_ANALYST):
            with self.subTest(role=role):
                profiles = dict(OC1_ROLE_PROFILES)
                profiles[role] = replace(profiles[role], enabled=True)
                with self.assertRaises(SchedulerError) as caught:
                    SchedulerPolicy(role_profiles=profiles)
                self.assertIs(caught.exception.code, SchedulerFailure.INVALID_POLICY)

    def test_invalid_policies_are_refused(self):
        bad = [
            {"max_queued": 0},
            {"fair_slot_every": 1},
            {"max_invocations": -1},
            {"max_queued": True},
            {"high_band_min": 50.0},
            {"eligible_band_min": float("nan")},
            {"horizon_limits": {Horizon.MIN_15: HorizonLimits(60, 15)}},
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(SchedulerError) as caught:
                    SchedulerPolicy(**kwargs)
                self.assertIs(caught.exception.code, SchedulerFailure.INVALID_POLICY)

    def test_policy_maps_are_read_only_copies(self):
        limits = dict(OC1_HORIZON_LIMITS)
        policy = SchedulerPolicy(horizon_limits=limits)
        limits[Horizon.MIN_15] = HorizonLimits(999, 999)
        self.assertEqual(policy.horizon_limits[Horizon.MIN_15].analysis_deadline_seconds, 60)
        with self.assertRaises(TypeError):
            policy.horizon_limits[Horizon.MIN_15] = HorizonLimits(1, 1)


class TestDeadlines(unittest.TestCase):
    def test_absolute_deadline_from_seal_per_horizon(self):
        for horizon, seconds in ((Horizon.MIN_15, 60), (Horizon.HOUR_1, 120), (Horizon.HOUR_4, 180), (Horizon.HOUR_24, 180)):
            with self.subTest(horizon=horizon):
                scheduler, _ = make(at=5)
                result = scheduler.admit(version("a", horizon=horizon, sealed=0))
                self.assertEqual(result.deadline, T0 + timedelta(seconds=seconds))

    def test_deadline_not_extended_after_queue_wait(self):
        scheduler, clock = make()
        scheduler.admit(version("a", sealed=0))
        clock.at_seconds(12)
        dispatched = scheduler.dispatch().dispatched
        self.assertEqual(dispatched.deadline, T0 + timedelta(seconds=60))

    def test_deadline_not_extended_after_repair(self):
        scheduler, clock = make()
        run_to_dispatch(scheduler, version("a"))
        self.assertTrue(scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(1000)).allowed)
        clock.at_seconds(20)
        repair = scheduler.authorize_call("a", Role.SCREENER, repair=True, prompt=tokens(1000))
        self.assertTrue(repair.allowed)
        self.assertEqual(repair.call_deadline, T0 + timedelta(seconds=50))
        clock.at_seconds(21)  # 21 + 30 + 10 = 61 > 60: the repair did not move the deadline
        late = scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(1000))
        self.assertIs(late.refusal, CallRefusal.DEADLINE_UNFIT)

    def test_reobservation_keeps_origin_deadline(self):
        scheduler, _ = make(at=30)
        fresh = scheduler.admit(version("fresh", sealed=30))
        self.assertEqual(fresh.deadline, T0 + timedelta(seconds=90))
        reobserved = scheduler.admit(version("reobs", sealed=30, origin=0))
        self.assertEqual(reobserved.deadline, T0 + timedelta(seconds=60))
        self.assertIs(reobserved.state, ItemState.DETERMINISTIC_ONLY)  # 30 + 40 > 60

    def test_expired_at_admission_is_abort_stale_record(self):
        scheduler, _ = make(at=60)
        result = scheduler.admit(version("a", sealed=0))
        self.assertIs(result.status, AdmissionStatus.REFUSED)
        self.assertEqual(result.transitions, (Transition("a", ItemState.ABORT_STALE, T0 + timedelta(seconds=60), None, StaleReason.DEADLINE_PASSED),))

    def test_max_queued_age_per_horizon(self):
        for horizon, age in ((Horizon.MIN_15, 15), (Horizon.HOUR_1, 30), (Horizon.HOUR_4, 45), (Horizon.HOUR_24, 45)):
            with self.subTest(horizon=horizon):
                scheduler, clock = make()
                scheduler.admit(version("a", horizon=horizon))
                clock.at_seconds(age)
                self.assertEqual(scheduler.sweep(), ())
                clock.at_seconds(age + 1)
                swept = scheduler.sweep()
                self.assertEqual(states(swept), [("a", ItemState.ABORT_STALE)])
                self.assertIs(swept[0].stale_reason, StaleReason.QUEUED_AGE_EXCEEDED)
                self.assertEqual(scheduler.queued(), ())

    def test_deadline_crossing_while_running_is_abort_stale_and_never_published(self):
        scheduler, clock = make()
        run_to_dispatch(scheduler, version("a"))
        self.assertTrue(scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(2000)).allowed)
        clock.at_seconds(60)
        result = scheduler.complete("a")
        self.assertIs(result.state, ItemState.ABORT_STALE)
        self.assertFalse(result.publishable)
        self.assertEqual(states(result.transitions), [("a", ItemState.ABORT_STALE)])
        self.assertIs(result.transitions[0].stale_reason, StaleReason.LATE_RESULT)
        self.assertNotIn(ItemState.COMPLETED, [t.state for t in result.transitions])
        self.assertIs(scheduler.dispatch().idle, IdleReason.EMPTY)

    def test_expiry_during_run_is_recorded_once_and_holds_the_slot_until_released(self):
        scheduler, clock = make()
        run_to_dispatch(scheduler, version("a"))
        scheduler.admit(version("b"))
        clock.at_seconds(60)
        swept = scheduler.sweep()
        self.assertIn(("a", ItemState.ABORT_STALE), states(swept))
        self.assertIs(scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(10)).refusal, CallRefusal.DEADLINE_PASSED)
        self.assertIs(scheduler.dispatch().idle, IdleReason.BUSY)  # the worker still holds the one slot
        late = scheduler.complete("a")
        self.assertFalse(late.publishable)
        self.assertEqual(late.transitions, ())  # no second record, no COMPLETED
        with self.assertRaises(SchedulerError) as caught:
            scheduler.complete("a")
        self.assertIs(caught.exception.code, SchedulerFailure.UNKNOWN_ITEM)

    def test_completion_before_deadline_is_publishable(self):
        scheduler, clock = make()
        run_to_dispatch(scheduler, version("a"))
        clock.at_seconds(59)
        result = scheduler.complete("a")
        self.assertIs(result.state, ItemState.COMPLETED)
        self.assertTrue(result.publishable)

    def test_failure_frees_slot_and_is_not_publishable(self):
        scheduler, _ = make()
        run_to_dispatch(scheduler, version("a"))
        result = scheduler.fail("a")
        self.assertEqual((result.state, result.publishable), (ItemState.FAILED, False))
        self.assertIsNone(scheduler.running_item())


class TestAdmissionFit(unittest.TestCase):
    def test_screener_timeout_plus_reserve(self):
        scheduler, _ = make(at=20)  # 20 + 30 + 10 = 60 fits a 60 s deadline
        self.assertIs(scheduler.admit(version("a")).status, AdmissionStatus.ADMITTED)
        scheduler, _ = make(at=21)
        self.assertIs(scheduler.admit(version("a")).state, ItemState.DETERMINISTIC_ONLY)
        scheduler, _ = make(at=21)
        self.assertIs(scheduler.admit(version("a", deterministic=False)).state, ItemState.ABSTAIN)

    def test_queued_item_that_stops_fitting_leaves_the_queue(self):
        scheduler, clock = make(at=10)
        scheduler.admit(version("a", deterministic=False))
        clock.at_seconds(21)
        self.assertEqual(states(scheduler.sweep()), [("a", ItemState.ABSTAIN)])

    def test_disabled_roles_are_dropped_from_the_path(self):
        scheduler, _ = make()
        result = scheduler.admit(version("a", roles=(Role.SCREENER, Role.CHALLENGER, Role.DEEP_ANALYST)))
        self.assertEqual(result.path, (Role.SCREENER,))
        self.assertEqual(result.dropped_roles, (Role.CHALLENGER, Role.DEEP_ANALYST))
        only_challenger = scheduler.admit(version("b", roles=(Role.CHALLENGER,), deterministic=False))
        self.assertIs(only_challenger.state, ItemState.ABSTAIN)

    def test_integrity_invalid_and_not_eligible_get_records(self):
        scheduler, _ = make()
        self.assertEqual(states(scheduler.admit(version("a", integrity_valid=False)).transitions), [("a", ItemState.INTEGRITY_INVALID)])
        self.assertEqual(states(scheduler.admit(version("b", score=49.99)).transitions), [("b", ItemState.NOT_ELIGIBLE)])


class TestQueue(unittest.TestCase):
    def test_one_running_inference_no_preemption(self):
        scheduler, _ = make()
        run_to_dispatch(scheduler, version("low", score=55))
        scheduler.admit(version("high", score=99))
        self.assertIs(scheduler.dispatch().idle, IdleReason.BUSY)
        self.assertEqual(scheduler.running_item(), "low")
        scheduler.complete("low")
        self.assertEqual(scheduler.dispatch().dispatched.item_id, "high")

    def test_sixteen_queued_then_backpressure_on_incoming_loser(self):
        scheduler, _ = make()
        for index in range(16):
            self.assertIs(scheduler.admit(version(f"q{index:02d}")).status, AdmissionStatus.ADMITTED)
        worse = scheduler.admit(version("q99"))
        self.assertIs(worse.state, ItemState.DROPPED_BACKPRESSURE)
        self.assertEqual(worse.transitions[-1].related_id, "q15")
        self.assertEqual(len(scheduler.queued()), 16)

    def test_backpressure_drops_worst_queued_when_incoming_is_better(self):
        scheduler, _ = make()
        for index in range(16):
            scheduler.admit(version(f"q{index:02d}"))
        better = scheduler.admit(version("q00a"))
        self.assertIs(better.status, AdmissionStatus.ADMITTED)
        self.assertEqual(states(better.transitions), [("q15", ItemState.DROPPED_BACKPRESSURE), ("q00a", ItemState.QUEUED)])
        self.assertEqual(better.transitions[0].related_id, "q00a")
        self.assertEqual(len(scheduler.queued()), 16)
        self.assertNotIn("q15", [entry.version.item_id for entry in scheduler.queued()])

    def test_eligible_band_loses_overflow_against_high_band(self):
        scheduler, _ = make()
        for index in range(16):
            scheduler.admit(version(f"q{index:02d}", score=80))
        low = scheduler.admit(version("a-low", score=60))
        self.assertIs(low.state, ItemState.DROPPED_BACKPRESSURE)

    def test_identical_identity_returns_existing_id(self):
        scheduler, _ = make()
        scheduler.admit(version("a", seed="same"))
        dup = replace(version("b", seed="same"), identity=identity("a", seed="same"))
        result = scheduler.admit(dup)
        self.assertEqual((result.status, result.item_id, result.state), (AdmissionStatus.DUPLICATE, "a", ItemState.QUEUED))
        self.assertEqual(result.transitions, ())
        scheduler.dispatch()
        again = scheduler.admit(dup)
        self.assertEqual((again.status, again.item_id, again.state), (AdmissionStatus.DUPLICATE, "a", ItemState.RUNNING))

    def test_identity_is_the_t031a_type(self):
        self.assertIs(sch.InvocationIdentity, invocation_domain.InvocationIdentity)
        self.assertIs(dict((f.name, f.type) for f in fields(OpportunityVersion))["identity"], "InvocationIdentity")

    def test_newer_version_supersedes_linked_both_ways(self):
        scheduler, clock = make()
        scheduler.admit(version("v1", instrument="XBTUSD", seed="h1", sealed=0))
        clock.at_seconds(3)
        result = scheduler.admit(version("v2", instrument="XBTUSD", seed="h2", sealed=2))
        self.assertIs(result.status, AdmissionStatus.ADMITTED)
        self.assertEqual(states(result.transitions), [("v1", ItemState.SUPERSEDED), ("v2", ItemState.QUEUED)])
        self.assertEqual(result.transitions[0].related_id, "v2")  # old -> new
        self.assertEqual(result.transitions[1].related_id, "v1")  # new -> old
        self.assertEqual([(e.version.item_id, e.supersedes) for e in scheduler.queued()], [("v2", "v1")])

    def test_older_or_same_seal_version_does_not_replace(self):
        scheduler, _ = make(at=5)
        scheduler.admit(version("v2", instrument="XBTUSD", seed="h2", sealed=4))
        for item_id, sealed in (("v1", 2), ("v1b", 4)):
            result = scheduler.admit(version(item_id, instrument="XBTUSD", seed=item_id, sealed=sealed))
            self.assertEqual((result.state, result.transitions[-1].related_id), (ItemState.SUPERSEDED, "v2"))
        self.assertEqual([e.version.item_id for e in scheduler.queued()], ["v2"])

    def test_opposite_directions_are_separate_hypotheses(self):
        scheduler, _ = make()
        scheduler.admit(version("long", instrument="XBTUSD", direction=Direction.LONG))
        scheduler.admit(version("short", instrument="XBTUSD", direction=Direction.SHORT))
        self.assertEqual(sorted(e.version.item_id for e in scheduler.queued()), ["long", "short"])

    def test_incompatible_policy_version_is_refused_not_merged(self):
        scheduler, _ = make(at=2)
        scheduler.admit(version("v1", instrument="XBTUSD", sealed=0))
        result = scheduler.admit(version("v2", instrument="XBTUSD", sealed=1, policy_version="OC-2"))
        self.assertEqual((result.state, result.transitions[-1].related_id), (ItemState.INCOMPATIBLE_VERSION, "v1"))


class TestRanking(unittest.TestCase):
    def test_band_then_deadline_then_seal_then_id(self):
        scheduler, _ = make(at=10)
        scheduler.admit(version("eligible-early", score=69.99, horizon=Horizon.MIN_15, sealed=0))
        scheduler.admit(version("high-late", score=70, horizon=Horizon.HOUR_4, sealed=0))
        scheduler.admit(version("high-early", score=70, horizon=Horizon.MIN_15, sealed=9))
        scheduler.admit(version("high-early-seal", score=95, horizon=Horizon.HOUR_1, sealed=0, origin=-51))  # deadline T0+69, like high-early
        scheduler.admit(version("high-b", score=70, horizon=Horizon.HOUR_1, sealed=5))
        scheduler.admit(version("high-a", score=71, horizon=Horizon.HOUR_1, sealed=5))
        order = [e.version.item_id for e in scheduler.queued()]
        # high-early-seal ties high-early on deadline (T0+69) and wins on the earlier seal, despite
        # its later ID; high-a/high-b tie on deadline and seal and are ordered by ID only.
        self.assertEqual(order, ["high-early-seal", "high-early", "high-a", "high-b", "high-late", "eligible-early"])

    def test_same_deadline_earlier_seal_first(self):
        scheduler, _ = make(at=10)
        scheduler.admit(version("z-old", sealed=0, horizon=Horizon.HOUR_1, origin=-60))  # deadline T0+60
        scheduler.admit(version("a-new", sealed=5, horizon=Horizon.MIN_15, origin=0))  # deadline T0+60
        self.assertEqual([e.version.item_id for e in scheduler.queued()], ["z-old", "a-new"])

    def test_band_boundaries(self):
        policy = SchedulerPolicy()
        self.assertIs(policy.band(70), Band.HIGH)
        self.assertIs(policy.band(69.99), Band.ELIGIBLE)
        self.assertIs(policy.band(50), Band.ELIGIBLE)
        self.assertIsNone(policy.band(49.99))

    def test_no_model_confidence_can_enter_scheduling(self):
        names = {f.name for f in fields(OpportunityVersion)}
        self.assertFalse([name for name in names if "confidence" in name or "llm" in name or "model" in name])
        with self.assertRaises(TypeError):
            OpportunityVersion(**{**{f.name: getattr(version("a"), f.name) for f in fields(OpportunityVersion)}, "confidence": 0.99})

    def test_invalid_scores_rejected(self):
        for score in (float("nan"), float("inf"), -1, 100.01, True, "80"):
            with self.subTest(score=score):
                with self.assertRaises(SchedulerError):
                    version("a", score=score)


class TestFairness(unittest.TestCase):
    def _run(self, policy, dispatches):
        scheduler, clock = make(policy)
        clock.at_seconds(0)
        scheduler.admit(version("e-old", score=55, horizon=Horizon.HOUR_1))
        clock.at_seconds(1)
        scheduler.admit(version("e-new", score=65, horizon=Horizon.HOUR_1))
        for index in range(1, 8):
            clock.at_seconds(1 + index)
            scheduler.admit(version(f"h{index}", score=90, horizon=Horizon.HOUR_1))
        order = []
        for _ in range(dispatches):
            result = scheduler.dispatch().dispatched
            order.append((result.item_id, result.fair_slot))
            scheduler.complete(result.item_id)
        return order

    def test_every_fourth_slot_goes_to_oldest_lower_band_over_nine_dispatches(self):
        order = self._run(SchedulerPolicy(), 9)
        self.assertEqual(
            order,
            [("h1", False), ("h2", False), ("h3", False), ("e-old", True),
             ("h4", False), ("h5", False), ("h6", False), ("e-new", True), ("h7", False)],
        )

    def test_unused_reserved_slot_returns_to_high_band(self):
        scheduler, _ = make()
        for index in range(5):
            scheduler.admit(version(f"h{index}", score=90, horizon=Horizon.HOUR_1))
        seen = []
        for _ in range(5):
            dispatched = scheduler.dispatch().dispatched
            seen.append((dispatched.slot_number, dispatched.item_id, dispatched.fair_slot))
            scheduler.complete(dispatched.item_id)
        self.assertEqual(seen[3], (4, "h3", False))
        self.assertEqual([slot for slot, _, _ in seen], [1, 2, 3, 4, 5])

    def test_perturbing_the_fair_slot_changes_the_sequence(self):
        order = self._run(SchedulerPolicy(fair_slot_every=3), 9)
        self.assertEqual([item for item, _ in order][:3], ["h1", "h2", "e-old"])
        without = self._run(SchedulerPolicy(fair_slot_every=100), 9)
        self.assertEqual([item for item, _ in without], ["h1", "h2", "h3", "h4", "h5", "h6", "h7", "e-new", "e-old"])


class TestCallCeilings(unittest.TestCase):
    def _running(self, policy=None):
        scheduler, clock = make(policy)
        run_to_dispatch(scheduler, version("a", horizon=Horizon.HOUR_4))
        return scheduler, clock

    def test_at_most_four_invocations_and_one_repair(self):
        scheduler, _ = self._running()
        for _ in range(3):
            self.assertTrue(scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(100)).allowed)
        self.assertTrue(scheduler.authorize_call("a", Role.SCREENER, repair=True, prompt=tokens(100)).allowed)
        fifth = scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(100))
        self.assertIs(fifth.refusal, CallRefusal.INVOCATION_CAP)
        self.assertEqual((fifth.usage.invocations, fifth.usage.repairs), (4, 1))

    def test_second_repair_refused_and_repair_counts_as_invocation(self):
        scheduler, _ = self._running()
        first = scheduler.authorize_call("a", Role.SCREENER, repair=True, prompt=tokens(100))
        self.assertEqual((first.usage.invocations, first.usage.repairs), (1, 1))
        self.assertIs(scheduler.authorize_call("a", Role.SCREENER, repair=True, prompt=tokens(100)).refusal, CallRefusal.REPAIR_CAP)

    def test_input_over_role_cap_and_disabled_role(self):
        scheduler, _ = self._running()
        self.assertIs(scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(2801)).refusal, CallRefusal.INPUT_OVER_ROLE_CAP)
        self.assertIs(scheduler.authorize_call("a", Role.CHALLENGER, repair=False, prompt=tokens(10)).refusal, CallRefusal.ROLE_DISABLED)
        self.assertIs(scheduler.authorize_call("b", Role.SCREENER, repair=False, prompt=tokens(10)).refusal, CallRefusal.NOT_RUNNING)

    def test_prompt_token_ceiling_15000(self):
        profiles = dict(OC1_ROLE_PROFILES)
        profiles[Role.SCREENER] = replace(profiles[Role.SCREENER], max_input_tokens=5000)
        scheduler, _ = self._running(SchedulerPolicy(role_profiles=profiles))
        for _ in range(3):
            self.assertTrue(scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(5000)).allowed)
        fourth = scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(1))
        self.assertIs(fourth.refusal, CallRefusal.PROMPT_TOKEN_CAP)
        self.assertEqual(fourth.usage.prompt_tokens, 15000)

    def test_output_token_ceiling_4500_reserves_full_output_cap(self):
        profiles = dict(OC1_ROLE_PROFILES)
        profiles[Role.SCREENER] = replace(profiles[Role.SCREENER], output_cap_tokens=1500)
        scheduler, _ = self._running(SchedulerPolicy(role_profiles=profiles))
        for _ in range(3):
            self.assertTrue(scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(10)).allowed)
        fourth = scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(10))
        self.assertIs(fourth.refusal, CallRefusal.OUTPUT_TOKEN_CAP)
        self.assertEqual(fourth.usage.output_tokens, 4500)

    def test_refused_call_changes_no_usage(self):
        scheduler, _ = self._running()
        scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(100))
        before = scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(9999)).usage
        after = scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(9999)).usage
        self.assertEqual(before, after)
        self.assertEqual(before.invocations, 1)


class TestTokens(unittest.TestCase):
    def test_no_character_count_basis_exists(self):
        self.assertEqual({basis.value for basis in TokenBasis}, {"tokenizer_count", "utf8_byte_upper_bound"})

    def test_utf8_byte_bound_is_at_least_bytes_plus_special_tokens(self):
        bound = utf8_byte_upper_bound("abc", 2)
        self.assertEqual((bound.upper_bound, bound.basis), (5, TokenBasis.UTF8_BYTE_UPPER_BOUND))
        euro = utf8_byte_upper_bound("€", 0)  # one character, three UTF-8 bytes
        self.assertEqual(euro.upper_bound, 3)

    def test_invalid_bounds(self):
        for amount in (-1, 1.5, True):
            with self.subTest(amount=amount):
                with self.assertRaises(SchedulerError):
                    TokenBound(amount, TokenBasis.TOKENIZER_COUNT)
        with self.assertRaises(SchedulerError):
            TokenBound(3, "chars")
        with self.assertRaises(SchedulerError):
            utf8_byte_upper_bound("x", -1)


class TestErrorsNeverLoseRecords(unittest.TestCase):
    """A raising call changes nothing: expired work keeps its ABORT_STALE for the next call."""

    def _expired_b_behind_running_a(self):
        scheduler, clock = make()
        run_to_dispatch(scheduler, version("a"))  # running, deadline 60 s
        scheduler.admit(version("b"))  # queued at 0 s, 15m queued age 15 s
        clock.at_seconds(20)  # b is now past its queued age
        return scheduler, clock

    def _assert_b_still_gets_its_record(self, scheduler):
        self.assertEqual([e.version.item_id for e in scheduler.queued()], ["b"])  # untouched by the error
        swept = scheduler.sweep()
        self.assertEqual(states(swept), [("b", ItemState.ABORT_STALE)])
        self.assertIs(swept[0].stale_reason, StaleReason.QUEUED_AGE_EXCEEDED)
        self.assertEqual(scheduler.running_item(), "a")

    def test_admit_with_colliding_item_id_loses_no_record(self):
        scheduler, _ = self._expired_b_behind_running_a()
        with self.assertRaises(SchedulerError) as caught:
            scheduler.admit(version("a", seed="another-evidence-hash"))
        self.assertIs(caught.exception.code, SchedulerFailure.INVALID_FIELD)
        self._assert_b_still_gets_its_record(scheduler)

    def test_complete_unknown_item_loses_no_record(self):
        scheduler, _ = self._expired_b_behind_running_a()
        with self.assertRaises(SchedulerError) as caught:
            scheduler.complete("nope")
        self.assertIs(caught.exception.code, SchedulerFailure.UNKNOWN_ITEM)
        self._assert_b_still_gets_its_record(scheduler)

    def test_fail_unknown_item_loses_no_record(self):
        scheduler, _ = self._expired_b_behind_running_a()
        with self.assertRaises(SchedulerError) as caught:
            scheduler.fail("nope")
        self.assertIs(caught.exception.code, SchedulerFailure.UNKNOWN_ITEM)
        self._assert_b_still_gets_its_record(scheduler)

    def test_complete_with_nothing_running_loses_no_record(self):
        scheduler, clock = make()
        scheduler.admit(version("a"))
        clock.at_seconds(20)
        with self.assertRaises(SchedulerError):
            scheduler.complete("nope")
        self.assertEqual(states(scheduler.sweep()), [("a", ItemState.ABORT_STALE)])

    def test_expired_running_item_not_marked_by_a_raising_call(self):
        scheduler, clock = make()
        run_to_dispatch(scheduler, version("a"))
        clock.at_seconds(61)
        with self.assertRaises(SchedulerError):
            scheduler.fail("nope")
        result = scheduler.complete("a")
        self.assertEqual(states(result.transitions), [("a", ItemState.ABORT_STALE)])  # recorded exactly once, now
        self.assertFalse(result.publishable)

    def test_raising_call_does_not_advance_the_clock_watermark(self):
        scheduler, clock = make()
        scheduler.admit(version("a"))
        clock.at_seconds(20)
        with self.assertRaises(SchedulerError):
            scheduler.complete("nope")
        clock.at_seconds(10)  # behind the failed call, ahead of the last successful one
        self.assertEqual(scheduler.sweep(), ())

    def test_replay_of_item_closed_by_the_same_sweep_gets_no_second_record(self):
        scheduler, clock = make()
        scheduler.admit(version("a"))
        clock.at_seconds(20)
        result = scheduler.admit(version("a"))
        self.assertIs(result.status, AdmissionStatus.REFUSED)
        self.assertIs(result.state, ItemState.ABORT_STALE)
        self.assertEqual(states(result.transitions), [("a", ItemState.ABORT_STALE)])
        self.assertEqual(scheduler.queued(), ())


DT_MAX = datetime.max.replace(tzinfo=UTC)
DT_MIN = datetime.min.replace(tzinfo=UTC)
ONE_US = timedelta(microseconds=1)


class TestNoExceptionAfterCommit(unittest.TestCase):
    """A security-review finding, closed as a class: every public entry point that
    raises does so before its commit point. For each one, a raising call leaves the queue,
    the running slot, the slot counter and the clock watermark unchanged, and the expired
    queued item still gets its ABORT_STALE from the next sweep."""

    def _scenario(self):
        scheduler, clock = make()
        run_to_dispatch(scheduler, version("a"))  # running, deadline T0+60
        scheduler.admit(version("b"))  # queued at T0, 15m queued age 15 s
        clock.at_seconds(20)  # b is past its queued age; nothing has swept it yet
        return scheduler, clock

    def _snapshot(self, scheduler):
        running = scheduler._running
        usage = None if running is None else (running.usage(), running.aborted)
        return (scheduler.queued(), scheduler.running_item(), usage, scheduler._slots_used, scheduler._last_now)

    def _assert_raises_and_changes_nothing(self, scheduler, clock, call, code):
        before = self._snapshot(scheduler)
        with self.assertRaises(SchedulerError) as caught:
            call()
        self.assertIs(caught.exception.code, code)
        self.assertEqual(self._snapshot(scheduler), before)
        clock.at_seconds(20)  # a valid reading again
        swept = scheduler.sweep()
        self.assertEqual(states(swept), [("b", ItemState.ABORT_STALE)])
        self.assertIs(swept[0].stale_reason, StaleReason.QUEUED_AGE_EXCEEDED)
        self.assertEqual(scheduler.running_item(), "a")

    def _entry_points(self, scheduler):
        return {
            "sweep": lambda: scheduler.sweep(),
            "admit": lambda: scheduler.admit(version("c")),
            "dispatch": lambda: scheduler.dispatch(),
            "authorize_call": lambda: scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(10)),
            "complete": lambda: scheduler.complete("a"),
            "fail": lambda: scheduler.fail("a"),
        }

    def test_every_entry_point_refuses_a_clock_without_room_and_changes_nothing(self):
        bad_readings = {
            "one second before datetime.max": DT_MAX - timedelta(seconds=1),
            "offset pushes past datetime.max": datetime.max.replace(tzinfo=timezone(-timedelta(hours=1))),
            "offset pushes before datetime.min": datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
            "naive": datetime(2026, 9, 18, 12, 0, 20),
        }
        for label, reading in bad_readings.items():
            for name in self._entry_points(None):
                with self.subTest(clock=label, entry_point=name):
                    scheduler, clock = self._scenario()
                    call = self._entry_points(scheduler)[name]
                    clock.at = reading
                    self._assert_raises_and_changes_nothing(scheduler, clock, call, SchedulerFailure.INVALID_CLOCK)

    def test_every_entry_point_with_a_backwards_clock_changes_nothing(self):
        for name in self._entry_points(None):
            with self.subTest(entry_point=name):
                scheduler, clock = self._scenario()
                call = self._entry_points(scheduler)[name]
                clock.at = T0 - ONE_US
                self._assert_raises_and_changes_nothing(scheduler, clock, call, SchedulerFailure.CLOCK_WENT_BACKWARDS)

    def test_every_entry_point_with_an_invalid_argument_changes_nothing(self):
        class Text(str):
            pass

        class Bound(TokenBound):
            pass

        class Version(OpportunityVersion):
            pass

        cases = {
            "admit: item_id names another active item": (lambda s: s.admit(version("a", seed="other")), SchedulerFailure.INVALID_FIELD),
            "admit: not an OpportunityVersion": (lambda s: s.admit("c"), SchedulerFailure.INVALID_FIELD),
            "admit: OpportunityVersion subclass": (
                lambda s: s.admit(Version(**{f.name: getattr(version("c"), f.name) for f in fields(OpportunityVersion)})),
                SchedulerFailure.INVALID_FIELD,
            ),
            "authorize_call: str subclass id": (
                lambda s: s.authorize_call(Text("a"), Role.SCREENER, repair=False, prompt=tokens(10)),
                SchedulerFailure.INVALID_FIELD,
            ),
            "authorize_call: TokenBound subclass": (
                lambda s: s.authorize_call("a", Role.SCREENER, repair=False, prompt=Bound(10, TokenBasis.TOKENIZER_COUNT)),
                SchedulerFailure.INVALID_FIELD,
            ),
            "authorize_call: non-bool repair": (
                lambda s: s.authorize_call("a", Role.SCREENER, repair=1, prompt=tokens(10)),
                SchedulerFailure.INVALID_FIELD,
            ),
            "complete: unknown id": (lambda s: s.complete("nope"), SchedulerFailure.UNKNOWN_ITEM),
            "complete: str subclass id": (lambda s: s.complete(Text("a")), SchedulerFailure.INVALID_FIELD),
            "complete: not text": (lambda s: s.complete(None), SchedulerFailure.INVALID_FIELD),
            "fail: unknown id": (lambda s: s.fail("nope"), SchedulerFailure.UNKNOWN_ITEM),
            "fail: str subclass id": (lambda s: s.fail(Text("a")), SchedulerFailure.INVALID_FIELD),
        }
        for label, (call, code) in cases.items():
            with self.subTest(case=label):
                scheduler, clock = self._scenario()
                self._assert_raises_and_changes_nothing(scheduler, clock, lambda: call(scheduler), code)

    def test_admit_computes_the_deadline_before_the_sweep(self):
        # (b) A version whose frozen fields were overwritten after validation: the
        # deadline overflow is found before the commit point, so b keeps its record.
        tampered = version("c")
        object.__setattr__(tampered, "sealed_at", DT_MAX - timedelta(seconds=1))
        scheduler, clock = self._scenario()
        self._assert_raises_and_changes_nothing(scheduler, clock, lambda: scheduler.admit(tampered), SchedulerFailure.INVALID_FIELD)

    def test_probe_seal_near_datetime_max_is_refused_at_construction(self):
        # (a) The attack: a is queued, the clock moves 20 s, a version sealed at
        # datetime.max - 1 s is offered. It can no longer be built, so admit never runs.
        scheduler, clock = make()
        scheduler.admit(version("a"))
        clock.at_seconds(20)
        with self.assertRaises(SchedulerError) as caught:
            replace(version("x"), sealed_at=DT_MAX - timedelta(seconds=1))
        self.assertIs(caught.exception.code, SchedulerFailure.INVALID_FIELD)
        swept = scheduler.sweep()
        self.assertEqual(states(swept), [("a", ItemState.ABORT_STALE)])

    def test_seal_room_boundary_is_max_policy_seconds(self):
        limit = DT_MAX - timedelta(seconds=sch.MAX_POLICY_SECONDS)
        self.assertEqual(replace(version("x"), sealed_at=limit).sealed_at, limit)
        for label, sealed in {
            "one microsecond past the room": limit + ONE_US,
            "offset pushes past datetime.max": datetime.max.replace(tzinfo=timezone(-timedelta(hours=1))),
            "offset pushes before datetime.min": datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
        }.items():
            with self.subTest(sealed=label):
                with self.assertRaises(SchedulerError) as caught:
                    replace(version("x"), sealed_at=sealed)
                self.assertIs(caught.exception.code, SchedulerFailure.INVALID_FIELD)
        with self.assertRaises(SchedulerError):
            replace(version("x"), origin_sealed_at=datetime.min.replace(tzinfo=timezone(timedelta(hours=1))))

    def test_clock_room_boundary_is_the_policy_span(self):
        policy = SchedulerPolicy()
        self.assertEqual(policy.max_span_seconds, 180)  # max(30+45+90+10, 180)
        edge = DT_MAX - timedelta(seconds=180)
        scheduler, clock = make()
        clock.at = edge
        self.assertEqual(scheduler.sweep(), ())
        clock.at = edge + ONE_US
        with self.assertRaises(SchedulerError) as caught:
            scheduler.sweep()
        self.assertIs(caught.exception.code, SchedulerFailure.INVALID_CLOCK)
        # Perturbing the span moves the boundary.
        profiles = dict(OC1_ROLE_PROFILES)
        profiles[Role.DEEP_ANALYST] = replace(profiles[Role.DEEP_ANALYST], hard_timeout_seconds=200)
        wider = SchedulerPolicy(role_profiles=profiles)
        self.assertEqual(wider.max_span_seconds, 285)
        scheduler, clock = make(wider)
        clock.at = edge
        with self.assertRaises(SchedulerError):
            scheduler.sweep()

    def test_full_lifecycle_at_the_end_of_the_datetime_range_never_raises(self):
        # Largest admissible seal, 24h horizon: every sum the scheduler computes still fits.
        sealed = DT_MAX - timedelta(seconds=sch.MAX_POLICY_SECONDS)
        start = sealed + timedelta(seconds=1)
        scheduler = DeadlineScheduler(SchedulerPolicy(), FakeClock(start))
        clock = scheduler._clock
        first = replace(version("a", horizon=Horizon.HOUR_24), sealed_at=sealed)
        self.assertIs(scheduler.admit(first).status, AdmissionStatus.ADMITTED)
        self.assertIs(scheduler.admit(replace(version("b", horizon=Horizon.HOUR_24), sealed_at=sealed)).status, AdmissionStatus.ADMITTED)
        self.assertEqual(scheduler.dispatch().dispatched.item_id, "a")
        self.assertTrue(scheduler.authorize_call("a", Role.SCREENER, repair=False, prompt=tokens(10)).allowed)
        clock.at = start + timedelta(seconds=60)
        swept = scheduler.sweep()
        self.assertEqual(states(swept), [("b", ItemState.ABORT_STALE)])
        clock.at = sealed + timedelta(seconds=180)
        late = scheduler.complete("a")
        self.assertEqual((late.state, late.publishable), (ItemState.ABORT_STALE, False))

    def test_policy_durations_are_capped(self):
        cap = sch.MAX_POLICY_SECONDS
        profiles = dict(OC1_ROLE_PROFILES)
        profiles[Role.SCREENER] = replace(profiles[Role.SCREENER], hard_timeout_seconds=cap + 1)
        limits_deadline = dict(OC1_HORIZON_LIMITS)
        limits_deadline[Horizon.MIN_15] = HorizonLimits(cap + 1, 15)
        limits_age = dict(OC1_HORIZON_LIMITS)
        limits_age[Horizon.MIN_15] = HorizonLimits(60, cap + 1)
        for kwargs in (
            {"finalization_reserve_seconds": cap + 1},
            {"role_profiles": profiles},
            {"horizon_limits": limits_deadline},
            {"horizon_limits": limits_age},
        ):
            with self.subTest(kwargs=sorted(kwargs)):
                with self.assertRaises(SchedulerError) as caught:
                    SchedulerPolicy(**kwargs)
                self.assertIs(caught.exception.code, SchedulerFailure.INVALID_POLICY)
        self.assertEqual(SchedulerPolicy(finalization_reserve_seconds=cap).finalization_reserve_seconds, cap)

    def test_only_exact_builtin_values_are_stored(self):
        class Stamp(datetime):
            pass

        class Identity(InvocationIdentity):
            pass

        stamped = replace(version("x"), sealed_at=Stamp(2026, 9, 18, 13, 0, tzinfo=timezone(timedelta(hours=1))))
        self.assertIs(type(stamped.sealed_at), datetime)
        self.assertEqual(stamped.sealed_at, T0)
        self.assertIs(stamped.sealed_at.tzinfo, UTC)
        base = identity("x")
        for label, changes in {
            "str subclass item_id": {"item_id": type("Text", (str,), {})("x")},
            "identity subclass": {"identity": Identity(**{f.name: getattr(base, f.name) for f in fields(InvocationIdentity)})},
            "str subclass inside identity": {"identity": replace(base, venue=type("Text", (str,), {})("kraken"))},
            "list of roles": {"requested_roles": [Role.SCREENER]},
            "float subclass score": {"opportunity_score": type("Score", (float,), {})(80.0)},
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(SchedulerError) as caught:
                    replace(version("x"), **changes)
                self.assertIs(caught.exception.code, SchedulerFailure.INVALID_FIELD)
        with self.assertRaises(SchedulerError):
            TokenBound(type("Count", (int,), {})(5), TokenBasis.TOKENIZER_COUNT)

    def test_replay_of_an_aborted_running_identity_reports_abort_stale(self):
        # The duplicate reports the recorded state, not RUNNING.
        scheduler, clock = make()
        run_to_dispatch(scheduler, version("a"))
        clock.at_seconds(60)
        self.assertEqual(states(scheduler.sweep()), [("a", ItemState.ABORT_STALE)])
        replay = scheduler.admit(version("a"))
        self.assertEqual((replay.status, replay.item_id, replay.state), (AdmissionStatus.DUPLICATE, "a", ItemState.ABORT_STALE))
        self.assertEqual(replay.transitions, ())


class TestClock(unittest.TestCase):
    def test_naive_clock_is_refused(self):
        scheduler, clock = make()
        clock.at = datetime(2026, 9, 18, 12, 0, 0)
        with self.assertRaises(SchedulerError) as caught:
            scheduler.dispatch()
        self.assertIs(caught.exception.code, SchedulerFailure.INVALID_CLOCK)

    def test_backward_clock_suspends_and_changes_nothing(self):
        scheduler, clock = make(at=5)
        scheduler.admit(version("a"))
        clock.at_seconds(4)
        with self.assertRaises(SchedulerError) as caught:
            scheduler.admit(version("b"))
        self.assertIs(caught.exception.code, SchedulerFailure.CLOCK_WENT_BACKWARDS)
        self.assertEqual([e.version.item_id for e in scheduler.queued()], ["a"])

    def test_transitions_carry_codes_only(self):
        self.assertEqual({f.name for f in fields(Transition)}, {"item_id", "state", "at", "related_id", "stale_reason"})


class TestPerturbation(unittest.TestCase):
    """Every configured limit changes behaviour when moved (docs/FAILURE_AND_QUALITY.md)."""

    def test_queue_capacity(self):
        scheduler, _ = make(SchedulerPolicy(max_queued=15))
        results = [scheduler.admit(version(f"q{index:02d}")).state for index in range(16)]
        self.assertEqual(results[-1], ItemState.DROPPED_BACKPRESSURE)
        scheduler, _ = make(SchedulerPolicy(max_queued=17))
        results = [scheduler.admit(version(f"q{index:02d}")).state for index in range(17)]
        self.assertEqual(results[-1], ItemState.QUEUED)

    def test_high_band_threshold(self):
        def order(policy):
            scheduler, _ = make(policy)
            scheduler.admit(version("a75", score=75, horizon=Horizon.HOUR_1))
            scheduler.admit(version("b65", score=65, horizon=Horizon.MIN_15))
            return [e.version.item_id for e in scheduler.queued()]

        self.assertEqual(order(SchedulerPolicy()), ["a75", "b65"])
        self.assertEqual(order(SchedulerPolicy(high_band_min=80)), ["b65", "a75"])

    def test_eligible_band_threshold(self):
        scheduler, _ = make()
        self.assertIs(scheduler.admit(version("a", score=50)).state, ItemState.QUEUED)
        scheduler, _ = make(SchedulerPolicy(eligible_band_min=51))
        self.assertIs(scheduler.admit(version("a", score=50)).state, ItemState.NOT_ELIGIBLE)

    def test_queued_age_and_deadline_limits(self):
        limits = dict(OC1_HORIZON_LIMITS)
        limits[Horizon.MIN_15] = HorizonLimits(analysis_deadline_seconds=60, max_queued_age_seconds=16)
        scheduler, clock = make(SchedulerPolicy(horizon_limits=limits))
        scheduler.admit(version("a"))
        clock.at_seconds(16)
        self.assertEqual(scheduler.sweep(), ())
        limits[Horizon.MIN_15] = HorizonLimits(analysis_deadline_seconds=61, max_queued_age_seconds=15)
        scheduler, _ = make(SchedulerPolicy(horizon_limits=limits), at=21)
        self.assertIs(scheduler.admit(version("a")).state, ItemState.QUEUED)

    def test_finalization_reserve_and_hard_timeout(self):
        scheduler, _ = make(SchedulerPolicy(finalization_reserve_seconds=11), at=20)
        self.assertIs(scheduler.admit(version("a")).state, ItemState.DETERMINISTIC_ONLY)
        profiles = dict(OC1_ROLE_PROFILES)
        profiles[Role.SCREENER] = replace(profiles[Role.SCREENER], hard_timeout_seconds=31)
        scheduler, _ = make(SchedulerPolicy(role_profiles=profiles), at=20)
        self.assertIs(scheduler.admit(version("a")).state, ItemState.DETERMINISTIC_ONLY)

    def test_invocation_repair_and_token_ceilings(self):
        cases = [
            (SchedulerPolicy(max_invocations=1), [False, False], CallRefusal.INVOCATION_CAP),
            (SchedulerPolicy(max_repairs=0), [True], CallRefusal.REPAIR_CAP),
            (SchedulerPolicy(max_prompt_tokens=5599), [False, False], CallRefusal.PROMPT_TOKEN_CAP),
            (SchedulerPolicy(max_output_tokens=1535), [False, False], CallRefusal.OUTPUT_TOKEN_CAP),
        ]
        for policy, calls, expected in cases:
            with self.subTest(expected=expected):
                default, _ = make()
                run_to_dispatch(default, version("a", horizon=Horizon.HOUR_4))
                perturbed, _ = make(policy)
                run_to_dispatch(perturbed, version("a", horizon=Horizon.HOUR_4))
                for repair in calls:
                    ok = default.authorize_call("a", Role.SCREENER, repair=repair, prompt=tokens(2800))
                    last = perturbed.authorize_call("a", Role.SCREENER, repair=repair, prompt=tokens(2800))
                    self.assertTrue(ok.allowed)
                self.assertIs(last.refusal, expected)

    def test_zero_invocations_leaves_no_model_path(self):
        scheduler, _ = make(SchedulerPolicy(max_invocations=0))
        self.assertIs(scheduler.admit(version("a")).state, ItemState.DETERMINISTIC_ONLY)


class TestConservation(unittest.TestCase):
    """Seeded state-machine run: every admitted item ends in exactly one record, bounds hold."""

    def test_random_sequences_never_lose_or_double_record(self):
        for seed in range(20):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                scheduler, clock = make()
                seconds = 0.0
                terminal = {}
                admitted = set()
                deadlines = {}
                for step in range(250):
                    seconds += rng.choice((0, 0, 0.5, 1, 3, 7))
                    clock.at = T0 + timedelta(seconds=seconds)
                    action = rng.random()
                    if action < 0.5:
                        item = version(
                            f"i{step}",
                            score=rng.choice((30, 55, 65, 72, 90)),
                            horizon=rng.choice(list(Horizon)),
                            sealed=seconds - rng.choice((0, 2, 10, 30)),
                            instrument=rng.choice(("XBTUSD", "ETHUSD", "SOLUSD", "XDGUSD")),
                            direction=rng.choice((Direction.LONG, Direction.SHORT)),
                            seed=f"s{step}",
                        )
                        admission = scheduler.admit(item)
                        if admission.status is AdmissionStatus.ADMITTED:
                            deadlines[item.item_id] = admission.deadline
                        transitions = admission.transitions
                    elif action < 0.6:
                        # Error paths: each must raise and change nothing (queue, slot).
                        before = (scheduler.queued(), scheduler.running_item())
                        active_ids = [e.version.item_id for e in before[0]] + ([before[1]] if before[1] else [])
                        tampered = version(f"t{step}")
                        object.__setattr__(tampered, "sealed_at", DT_MAX - timedelta(seconds=1))
                        calls = [
                            lambda: scheduler.complete("nope"),
                            lambda: scheduler.fail("nope"),
                            lambda: scheduler.admit(tampered),
                        ]
                        if active_ids:
                            victim = rng.choice(active_ids)
                            calls.append(lambda: scheduler.admit(version(victim, seed="collision")))
                        entry = rng.choice((scheduler.sweep, scheduler.dispatch, lambda: scheduler.admit(version(f"c{step}"))))
                        calls.append(lambda: entry())  # with a clock that has no room left
                        pick = rng.randrange(len(calls))
                        if pick == len(calls) - 1:
                            clock.at = DT_MAX - timedelta(seconds=1)
                        try:
                            calls[pick]()
                        except SchedulerError:
                            self.assertEqual((scheduler.queued(), scheduler.running_item()), before)
                            transitions = ()
                        else:
                            self.fail("an error path did not raise")
                        clock.at = T0 + timedelta(seconds=seconds)
                    elif action < 0.8:
                        transitions = scheduler.dispatch().transitions
                    elif scheduler.running_item() is not None:
                        running = scheduler.running_item()
                        done = scheduler.complete(running) if rng.random() < 0.8 else scheduler.fail(running)
                        if done.publishable:
                            self.assertEqual(done.state, ItemState.COMPLETED)
                        transitions = done.transitions
                    else:
                        transitions = scheduler.sweep()
                    for transition in transitions:
                        if transition.state is ItemState.QUEUED:
                            admitted.add(transition.item_id)
                        if transition.state in TERMINAL_ITEM_STATES:
                            self.assertNotIn(transition.item_id, terminal, f"double terminal record for {transition.item_id}")
                            terminal[transition.item_id] = transition
                    self.assertLessEqual(len(scheduler.queued()), 16)
                    queued_keys = [e.version.group_key for e in scheduler.queued()]
                    self.assertEqual(len(queued_keys), len(set(queued_keys)))
                active = {e.version.item_id for e in scheduler.queued()} | {scheduler.running_item()} - {None}
                for item_id in admitted:
                    self.assertTrue(item_id in terminal or item_id in active, f"{item_id} has no record")
                for item_id, transition in terminal.items():
                    if transition.state is ItemState.COMPLETED:
                        self.assertLess(transition.at, deadlines[item_id])


class TestPurity(unittest.TestCase):
    """Workflow package: stdlib plus the pure domain only; no I/O, no wall clock, no adapters/UI."""

    ALLOWED = {
        "__future__",
        "math",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "types",
        "typing",
        "radar_v08.domain.integrity",
        "radar_v08.domain.invocation",
    }

    def _trees(self):
        for module in (sch, workflow_package):
            with open(module.__file__, encoding="utf-8") as handle:
                source = handle.read()
            yield module.__name__, source, ast.parse(source)

    def test_imports_are_stdlib_and_domain_only(self):
        for name, _, tree in self._trees():
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add(node.module or "")
            with self.subTest(module=name):
                self.assertLessEqual(imported, self.ALLOWED)

    def test_no_wall_clock_io_or_sleep_calls(self):
        forbidden = {"now", "utcnow", "today", "time", "monotonic", "perf_counter", "open", "sleep", "connect", "print"}
        for name, source, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    with self.subTest(module=name, call=called):
                        self.assertNotIn(called, forbidden)
            self.assertNotIn("sqlite", source)
            self.assertNotIn("import requests", source)


if __name__ == "__main__":
    unittest.main()
