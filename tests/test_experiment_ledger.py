"""T042a: experiment ledger — cohorts, episodes, purged chronological splits, sample minima,
UNCALIBRATED results, sealed manifests and trial counts (``radar_v08/domain/experiments.py``).

Every observation is synthetic: built in memory through the real T041 path
(``outcomes.label_horizon``) from generated quotes, with ``random.Random(seed)`` where a
fixture needs variety. Nothing reads or writes a file, a database, the network, a model or
the UI; no real backtest. Expected counts are derived by hand in the comments from how each
fixture is built, never by re-running the implementation.
"""

import ast
import hashlib
import json
import os
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

from radar_v08.domain import experiments as ex  # noqa: E402
from radar_v08.domain.costs import CostStatus, Side  # noqa: E402
from radar_v08.domain.integrity import InstrumentId, InstrumentKind  # noqa: E402
from radar_v08.domain.invocation import Direction  # noqa: E402
from radar_v08.domain.outcomes import (  # noqa: E402
    OUTCOME_POLICY_VERSION,
    DecisionKind,
    DecisionRef,
    Horizon,
    HorizonCost,
    LinkedOutcome,
    LinkMissingReason,
    OutcomeSubject,
    QuoteObservation,
    RecordedCost,
    label_horizon,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)  # midnight UTC
COST_POLICY = "COST-1"
SETUP = "breakout"
FEATURES = "FEAT-1"
H = timedelta(hours=24)  # MAX_LABEL_HORIZON

COHORT = ex.CohortKey(SETUP, Direction.LONG, Horizon.H1, FEATURES, OUTCOME_POLICY_VERSION)
BENCHMARK = ex.BenchmarkKey("prevalence-baseline", ex.BenchmarkTarget.NET_MARKOUT_POSITIVE, COST_POLICY)


def instrument(pair):
    return InstrumentId("kraken", pair, InstrumentKind.SPOT, pair, "USD", pair)


def cost(status=CostStatus.COMPLETE, total=Decimal("0.002"), side=Side.LONG, policy=COST_POLICY):
    return RecordedCost(policy, side, InstrumentKind.SPOT, status, total if status is CostStatus.COMPLETE else None)


def observation(
    pair,
    decided,
    *,
    direction=Direction.LONG,
    horizon=Horizon.H1,
    exit_mid=Decimal("101"),
    exit_delay=timedelta(0),
    cost_status=CostStatus.COMPLETE,
    record_cost=True,
    cost_policy=COST_POLICY,
    quote_missing=False,
    setup=SETUP,
    features=FEATURES,
    tolerance=timedelta(minutes=2),
):
    """One labelled T041 outcome: entry mid 100 at ``decided``, exit quote at target + exit_delay."""
    side = Side.LONG if direction is Direction.LONG else Side.SHORT
    costs = (HorizonCost(horizon, cost(cost_status, side=side, policy=cost_policy)),) if record_cost else ()
    subject = OutcomeSubject(
        instrument=instrument(pair),
        pair=pair,
        direction=direction,
        decision_as_of=decided,
        entry_mid=Decimal("100"),
        entry_observed_at=decided,
        evidence=LinkMissingReason.NOT_RECORDED,
        decision=DecisionRef(DecisionKind.DECISION, f"d-{pair}-{decided.isoformat()}"),
        arm="L3-only",
        costs=costs,
    )
    target = decided + horizon.duration
    exit_at = target + exit_delay
    quotes = [] if quote_missing else [QuoteObservation(exit_at, exit_mid - Decimal("0.5"), exit_mid + Decimal("0.5"), "fx")]
    label = label_horizon(subject, horizon, quotes, now=exit_at + tolerance, tolerance=tolerance)
    return ex.CohortObservation(LinkedOutcome(subject, label), setup, features)


def evaluate(observations, *, as_of=None, seed=7, cohort=COHORT, benchmark=BENCHMARK):
    return ex.evaluate_cohort(
        observations,
        cohort=cohort,
        benchmark=benchmark,
        seed=seed,
        evaluated_as_of=as_of if as_of is not None else T0 + timedelta(days=400),
    )


def content(evaluation):
    return json.loads(evaluation.content_json)


def synthetic(seed, pairs, days, *, member_rate=0.3):
    """``pairs`` x ``days`` episodes, plus random linked members, from ``random.Random(seed)``.

    Pair p seals one opportunity per day at ``T0 + day*24h + offset_p`` with 20 distinct offsets
    ``p*67 minutes`` (all before 23:00, so every seal of day d is on UTC date d). Consecutive
    seals of a pair are exactly 24h apart, so each seal starts its own episode (the window is
    ``< seal + 24h``). A member, when drawn, is 1..600 minutes after its seal: always linked.
    Hence episodes = pairs * days exactly; with distinct offsets there are no ties at a cut,
    and any 24h window before a boundary holds exactly one seal per pair: purged = 2 * pairs.
    """
    rng = random.Random(seed)
    items = []
    for p in range(pairs):
        pair = f"P{p:02d}"
        for day in range(days):
            decided = T0 + timedelta(days=day, minutes=67 * p)
            items.append(observation(pair, decided, exit_mid=Decimal(rng.randint(95, 105))))
            if rng.random() < member_rate:
                later = decided + timedelta(minutes=rng.randint(1, 600))
                items.append(observation(pair, later, exit_mid=Decimal(rng.randint(95, 105))))
    return items


def split_ids(evaluation):
    return {split: [e.episode_id for e in evaluation.splits.episodes(split)] for split in ex.SPLITS}


class TestHandPurgeAtBoundaries(unittest.TestCase):
    """Ten single-opportunity episodes on ten pairs (no merging), cohort horizon 1h.

    Seal times (hours after T0): e0=0, e1=48, e2=96, e3=144 (poisoned: exit quote only at
    241h), e4=215h59m, e5=216, e6=240, e7=264, e8=288, e9=336. N=10, so the calibration start
    is episode 10*60//100=6 -> 240h and the test start is episode 10*80//100=8 -> 288h.
    Purge windows: [216h, 240h) and [264h, 288h).
    - e5 (216h, exactly 240-24) and e7 (264h, exactly 288-24): BOUNDARY_WINDOW (inclusive start).
    - e4 (215h59m) is one minute outside the window: kept in fit (label at 216h59m <= 240h).
    - e3 is in fit and outside the window, but its label is only available at 241h > 240h,
      the end of fit: LABEL_AFTER_BLOCK_END.
    Fit = e0, e1, e2, e4; calibration = e6; test = e8, e9.
    """

    HOURS = [
        timedelta(hours=0),
        timedelta(hours=48),
        timedelta(hours=96),
        timedelta(hours=144),
        timedelta(hours=215, minutes=59),
        timedelta(hours=216),
        timedelta(hours=240),
        timedelta(hours=264),
        timedelta(hours=288),
        timedelta(hours=336),
    ]

    def build(self):
        items = []
        for index, offset in enumerate(self.HOURS):
            if index == 3:
                # exit quote 96h late (tolerance 100h): a legitimate T041 label known at 144+1+96 = 241h
                items.append(
                    observation("E3", T0 + offset, exit_delay=timedelta(hours=96), tolerance=timedelta(hours=100))
                )
            else:
                items.append(observation(f"E{index}", T0 + offset))
        return items

    def test_hand_example(self):
        items = self.build()
        evaluation = evaluate(items, as_of=T0 + timedelta(days=30))
        body = content(evaluation)
        ids = [item.outcome.label.subject_id for item in items]
        self.assertEqual(body["boundaries"]["calibration_start"], "2026-01-11T00:00:00.000000+00:00")  # 240h
        self.assertEqual(body["boundaries"]["test_start"], "2026-01-13T00:00:00.000000+00:00")  # 288h
        self.assertEqual(body["purged"], {"boundary_window": 2, "label_after_block_end": 1})
        self.assertEqual(body["splits"], {"fit": 4, "calibration": 1, "test": 2})
        self.assertEqual(body["episodes"], {"formed": 10, "linked_observations": 0, "eligible": 10})
        # the assignment itself is withheld (UNCALIBRATED) but its ids are hashed in the manifest
        expected_fit = hashlib.sha256("\n".join(sorted([ids[0], ids[1], ids[2], ids[4]])).encode()).hexdigest()
        self.assertEqual(body["split_ids_sha256"]["fit"], expected_fit)
        expected_cal = hashlib.sha256(ids[6].encode()).hexdigest()
        self.assertEqual(body["split_ids_sha256"]["calibration"], expected_cal)
        expected_test = hashlib.sha256("\n".join(sorted([ids[8], ids[9]])).encode()).hexdigest()
        self.assertEqual(body["split_ids_sha256"]["test"], expected_test)
        self.assertEqual(evaluation.status, ex.EvaluationStatus.UNCALIBRATED)
        self.assertIsNone(evaluation.splits)

    def test_exact_window_edges(self):
        # the same example with the poisoned e3 replaced by a normal label: only the window purges
        items = [observation(f"E{index}", T0 + offset) for index, offset in enumerate(self.HOURS)]
        body = content(evaluate(items, as_of=T0 + timedelta(days=30)))
        self.assertEqual(body["purged"], {"boundary_window": 2, "label_after_block_end": 0})
        self.assertEqual(body["splits"], {"fit": 5, "calibration": 1, "test": 2})

    def test_ties_at_a_cut_go_to_the_later_block(self):
        # five episodes at 0h, 48h, 96h and two at 144h (pairs A, B). N=5: calibration start is
        # episode 5*60//100=3 -> 144h, test start is episode 5*80//100=4 -> also 144h. Both 144h
        # seals are test (by time), calibration is empty; 96h is before the window [120h, 144h): fit.
        times = [0, 48, 96, 144, 144]
        items = [observation(pair, T0 + timedelta(hours=h)) for pair, h in zip("VWXAB", times, strict=True)]
        body = content(evaluate(items, as_of=T0 + timedelta(days=30)))
        self.assertEqual(body["boundaries"]["calibration_start"], body["boundaries"]["test_start"])
        self.assertEqual(body["splits"], {"fit": 3, "calibration": 0, "test": 2})
        self.assertEqual(body["purged"], {"boundary_window": 0, "label_after_block_end": 0})


class TestEpisodes(unittest.TestCase):
    def test_overlapping_same_pair_opportunities_form_one_episode_sealed_by_the_earliest(self):
        # pair X at 0h, 5h, 23h59m (all < 0h + 24h) -> one episode sealed at 0h, two members;
        # 24h exactly starts a new episode; 30h joins it (< 48h). Pair Y at 5h is its own.
        x = [observation("X", T0 + timedelta(hours=h)) for h in (5, 0)]
        x.append(observation("X", T0 + timedelta(hours=23, minutes=59)))
        x.append(observation("X", T0 + timedelta(hours=24)))
        x.append(observation("X", T0 + timedelta(hours=30)))
        y = [observation("Y", T0 + timedelta(hours=5))]
        body = content(evaluate(x + y, as_of=T0 + timedelta(days=30)))
        self.assertEqual(body["episodes"], {"formed": 3, "linked_observations": 3, "eligible": 3})

    def test_members_never_rescue_a_seal_with_missing_net(self):
        # the seal's cost is incomplete; a later member of the same episode has a complete net.
        seal = observation("X", T0, cost_status=CostStatus.INCOMPLETE)
        member = observation("X", T0 + timedelta(hours=2))
        body = content(evaluate([member, seal], as_of=T0 + timedelta(days=30)))
        self.assertEqual(body["episodes"], {"formed": 1, "linked_observations": 1, "eligible": 0})
        self.assertEqual(body["excluded_episodes"]["net_cost_incomplete"], 1)
        self.assertEqual(body["splits"], {"fit": 0, "calibration": 0, "test": 0})

    def test_no_kept_episode_crosses_a_boundary(self):
        evaluation = evaluate(synthetic(11, 20, 62, member_rate=0.9))
        self.assertEqual(evaluation.status, ex.EvaluationStatus.SAMPLE_SUFFICIENT)
        splits = evaluation.splits
        for episode in splits.fit:
            self.assertLess(episode.last_decision_as_of, splits.calibration_start)
            self.assertLessEqual(episode.label_available_at, splits.calibration_start)
        for episode in splits.calibration:
            self.assertGreaterEqual(episode.decision_as_of, splits.calibration_start)
            self.assertLess(episode.last_decision_as_of, splits.test_start)
            self.assertLessEqual(episode.label_available_at, splits.test_start)
        for episode in splits.test:
            self.assertGreaterEqual(episode.decision_as_of, splits.test_start)
        self.assertTrue(any(episode.member_ids for episode in splits.fit))


class TestExclusions(unittest.TestCase):
    AS_OF = T0 + timedelta(days=30)

    def test_missing_net_is_excluded_with_its_typed_reason_never_zero(self):
        items = [
            observation("A", T0),  # kept
            observation("B", T0, cost_status=CostStatus.INCOMPLETE),
            observation("C", T0, record_cost=False),
            observation("D", T0, quote_missing=True),  # gross unavailable -> net unavailable
            observation("E", T0, cost_policy="COST-2"),  # net priced under another scenario
        ]
        evaluation = evaluate(items, as_of=self.AS_OF)
        body = content(evaluation)
        self.assertEqual(
            body["excluded_episodes"],
            {
                "label_not_mature": 0,
                "net_gross_unavailable": 1,
                "net_cost_not_recorded": 1,
                "net_cost_incomplete": 1,
                "cost_policy_mismatch": 1,
            },
        )
        self.assertEqual(body["episodes"]["eligible"], 1)
        self.assertEqual(sum(body["splits"].values()), 1)
        # the incomplete label really has no net (T041), and nothing was substituted for it
        self.assertIsNone(items[1].outcome.label.net_markout)
        self.assertEqual(items[0].outcome.label.net_markout, Decimal("0.008"))  # 101/100-1-0.002

    def test_net_zero_is_a_value_not_a_missing_net(self):
        # exit 100.2: gross 0.002, net exactly 0 -> eligible (a real zero is kept, a missing one is not)
        item = observation("Z", T0, exit_mid=Decimal("100.2"))
        self.assertEqual(item.outcome.label.net_markout, Decimal("0"))
        self.assertEqual(content(evaluate([item], as_of=self.AS_OF))["episodes"]["eligible"], 1)

    def test_cohort_mismatches_are_counted_by_reason(self):
        items = [
            observation("A", T0, setup="mean-revert"),
            observation("B", T0, direction=Direction.SHORT),
            observation("C", T0, horizon=Horizon.H4),
            observation("D", T0, features="FEAT-2"),
            observation("F", self.AS_OF + timedelta(hours=1)),
            observation("G", T0),
        ]
        body = content(evaluate(items, as_of=self.AS_OF))
        self.assertEqual(
            body["excluded_observations"],
            {
                "setup_mismatch": 1,
                "direction_mismatch": 1,
                "horizon_mismatch": 1,
                "feature_version_mismatch": 1,
                "outcome_version_mismatch": 0,
                "decided_after_as_of": 1,
            },
        )
        self.assertEqual(body["input"]["observations"], 6)
        self.assertEqual(body["episodes"]["eligible"], 1)

    def test_other_outcome_policy_version_is_another_cohort(self):
        cohort = ex.CohortKey(SETUP, Direction.LONG, Horizon.H1, FEATURES, "OUTCOME-2")
        body = content(evaluate([observation("A", T0)], as_of=self.AS_OF, cohort=cohort))
        self.assertEqual(body["excluded_observations"]["outcome_version_mismatch"], 1)

    def test_duplicate_observation_is_refused(self):
        item = observation("A", T0)
        with self.assertRaises(ex.ExperimentInputError) as caught:
            evaluate([item, item], as_of=self.AS_OF)
        self.assertEqual(caught.exception.code, ex.ExperimentErrorCode.DUPLICATE_OBSERVATION)


class TestPoisonedFutureLabel(unittest.TestCase):
    def test_label_known_after_the_evaluation_time_never_enters(self):
        # label at 1h + 96h late = 97h after T0; evaluated at 50h: LABEL_NOT_MATURE, in no split
        poison = observation("P", T0, exit_delay=timedelta(hours=96), tolerance=timedelta(hours=100))
        clean = observation("C", T0)
        body = content(evaluate([poison, clean], as_of=T0 + timedelta(hours=50)))
        self.assertEqual(body["excluded_episodes"]["label_not_mature"], 1)
        self.assertEqual(sum(body["splits"].values()), 1)
        expected = hashlib.sha256(clean.outcome.label.subject_id.encode()).hexdigest()
        kept = [h for h in body["split_ids_sha256"].values() if h == expected]
        self.assertEqual(len(kept), 1)

    def test_poisoned_label_inside_a_sufficient_sample_is_never_in_a_split(self):
        items = synthetic(5, 20, 62)
        # a fit-period opportunity (day 3) on its own pair whose label is only known on day 43, after
        # the calibration start (episode 1241*60//100 = 744 of 1241, on day 37)
        poison = observation(
            "POISON", T0 + timedelta(days=3), exit_delay=timedelta(days=40), tolerance=timedelta(days=41)
        )
        evaluation = evaluate(items + [poison])
        self.assertEqual(evaluation.status, ex.EvaluationStatus.SAMPLE_SUFFICIENT)
        poison_id = poison.outcome.label.subject_id
        for split in ex.SPLITS:
            self.assertNotIn(poison_id, [e.episode_id for e in evaluation.splits.episodes(split)])
        self.assertEqual(content(evaluation)["purged"]["label_after_block_end"], 1)


class TestMinima(unittest.TestCase):
    def test_minimum_function_each_bound_inclusive(self):
        self.assertEqual(ex.failed_minima(1000, 200, 60), ())
        self.assertEqual(ex.failed_minima(999, 200, 60), (ex.FailedMinimum.MATURED_EPISODES,))
        self.assertEqual(ex.failed_minima(1000, 199, 60), (ex.FailedMinimum.TEST_EPISODES,))
        self.assertEqual(ex.failed_minima(1000, 200, 59), (ex.FailedMinimum.CALENDAR_DAYS,))
        self.assertEqual(ex.failed_minima(0, 0, 0), tuple(ex.FailedMinimum))

    def test_calendar_days_counts_both_utc_dates(self):
        self.assertEqual(ex.calendar_days([]), 0)
        self.assertEqual(ex.calendar_days([T0]), 1)
        # 1 Jan .. 1 Mar 2026 = 31 + 28 + 1 = 60 dates
        self.assertEqual(ex.calendar_days([T0, datetime(2026, 3, 1, 23, 59, tzinfo=UTC)]), 60)
        # 00:30 on 2 Jan in UTC+01:00 is 23:30 on 1 Jan UTC
        self.assertEqual(ex.calendar_days([datetime(2026, 1, 2, 0, 30, tzinfo=timezone(timedelta(hours=1)))]), 1)

    def assert_uncalibrated(self, evaluation, failed):
        self.assertEqual(evaluation.status, ex.EvaluationStatus.UNCALIBRATED)
        self.assertEqual(evaluation.failed_minima, failed)
        self.assertIsNone(evaluation.splits)
        body = content(evaluation)
        self.assertEqual(body["status"], "UNCALIBRATED")
        self.assertEqual(body["failed_minima"], [item.value for item in failed])

    def test_sufficient_sample(self):
        # 20 pairs x 62 days = 1240 episodes; test = 1240 - 1240*80//100 = 1240 - 992 = 248;
        # purged 2*20 = 40 -> kept 1200; calendar 62 days.
        evaluation = evaluate(synthetic(1, 20, 62))
        self.assertEqual(evaluation.status, ex.EvaluationStatus.SAMPLE_SUFFICIENT)
        self.assertEqual(evaluation.failed_minima, ())
        body = content(evaluation)
        self.assertEqual(body["episodes"]["eligible"], 1240)
        self.assertEqual(body["purged"], {"boundary_window": 40, "label_after_block_end": 0})
        # fit: 744 before the calibration cut minus 20 purged; calibration: 248 minus 20
        self.assertEqual(body["splits"], {"fit": 724, "calibration": 228, "test": 248})
        self.assertEqual(body["calendar_days"], 62)

    def test_too_few_calendar_days(self):
        # 20 x 59 = 1180 episodes, kept 1140, test 1180-944 = 236, but 59 days
        self.assert_uncalibrated(evaluate(synthetic(2, 20, 59)), (ex.FailedMinimum.CALENDAR_DAYS,))

    def test_exactly_sixty_days_passes_the_calendar_minimum(self):
        # 20 x 60 = 1200 episodes, kept 1160, test 1200-960 = 240, 60 days
        self.assertEqual(evaluate(synthetic(2, 20, 60)).status, ex.EvaluationStatus.SAMPLE_SUFFICIENT)

    def test_too_few_matured_episodes(self):
        # 17 x 60 = 1020 episodes; purged 34 -> kept 986 < 1000; test 1020-816 = 204; 60 days
        self.assert_uncalibrated(evaluate(synthetic(3, 17, 60)), (ex.FailedMinimum.MATURED_EPISODES,))

    def test_too_few_test_episodes(self):
        # 10 x 60 = 600 episodes; test 600-480 = 120 < 200 and kept 580 < 1000; 60 days.
        # With the 60/20/20 cut, >= 1000 kept episodes imply >= 200 test ones, so the test
        # minimum fails together with the total here; alone it is covered by the function test.
        self.assert_uncalibrated(
            evaluate(synthetic(4, 10, 60)),
            (ex.FailedMinimum.MATURED_EPISODES, ex.FailedMinimum.TEST_EPISODES),
        )

    def test_empty_input_is_uncalibrated_with_every_minimum(self):
        self.assert_uncalibrated(evaluate([]), tuple(ex.FailedMinimum))

    def test_uncalibrated_carries_no_number_beyond_counts(self):
        body = content(evaluate(synthetic(4, 10, 60)))
        numeric_keys = {"probability", "brier", "prevalence", "calibration_error", "net_mean", "score"}
        self.assertFalse(numeric_keys & set(body))


class TestDeterminismAndReplay(unittest.TestCase):
    def test_shuffled_input_gives_the_same_splits_and_hash(self):
        items = synthetic(21, 20, 62, member_rate=0.5)
        first = evaluate(items)
        shuffled = list(items)
        random.Random(99).shuffle(shuffled)
        self.assertNotEqual([o.outcome.label.subject_id for o in shuffled], [o.outcome.label.subject_id for o in items])
        second = evaluate(shuffled)
        self.assertEqual(split_ids(first), split_ids(second))
        self.assertEqual(first.content_sha256, second.content_sha256)
        self.assertEqual(first.content_json, second.content_json)

    def test_same_seed_replays_the_manifest_byte_for_byte(self):
        _, first = ex.TrialLedger().register(evaluate(synthetic(42, 20, 62), seed=1234))
        _, second = ex.TrialLedger().register(evaluate(synthetic(42, 20, 62), seed=1234))
        self.assertEqual(first.canonical_json.encode("ascii"), second.canonical_json.encode("ascii"))
        self.assertEqual(first.manifest_id, second.manifest_id)

    def test_another_seed_is_another_manifest(self):
        items = synthetic(42, 5, 10)
        a = evaluate(items, seed=1)
        b = evaluate(items, seed=2)
        self.assertNotEqual(a.content_sha256, b.content_sha256)
        self.assertEqual(content(a)["seed"], 1)

    def test_input_ids_hash_is_the_sorted_subject_horizon_ids(self):
        items = [observation("A", T0), observation("B", T0 + timedelta(hours=1))]
        keys = sorted(f"{item.outcome.label.subject_id}|1h" for item in items)
        expected = hashlib.sha256("\n".join(keys).encode()).hexdigest()
        self.assertEqual(content(evaluate(items))["input"]["ids_sha256"], expected)


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.evaluation = evaluate(synthetic(8, 20, 62), seed=77)
        _, self.manifest = ex.TrialLedger().register(self.evaluation)

    def test_manifest_is_canonical_and_hashed(self):
        text = self.manifest.canonical_json
        body = json.loads(text)
        self.assertEqual(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True), text)
        self.assertEqual(self.manifest.manifest_id, "manifest:sha256:" + hashlib.sha256(text.encode()).hexdigest())
        content_only = {k: v for k, v in body.items() if k not in ("attempt", "content_sha256")}
        self.assertEqual(body["content_sha256"], hashlib.sha256(self.evaluation.content_json.encode()).hexdigest())
        self.assertEqual(
            json.dumps(content_only, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
            self.evaluation.content_json,
        )

    def test_manifest_records_every_required_field(self):
        body = self.manifest.as_dict()
        self.assertEqual(body["seed"], 77)
        self.assertEqual(body["policy_version"], "EXPERIMENT-1")
        self.assertEqual(
            body["cohort"],
            {
                "setup": SETUP,
                "direction": "LONG",
                "horizon": "1h",
                "feature_version": FEATURES,
                "outcome_policy_version": OUTCOME_POLICY_VERSION,
            },
        )
        self.assertEqual(
            body["benchmark"], {"name": "prevalence-baseline", "target": "net_markout>0", "cost_policy_version": COST_POLICY}
        )
        self.assertEqual(body["attempt"], 1)
        self.assertEqual(body["policy"]["purge_minutes"], 1440)
        self.assertEqual(body["policy"]["split_percent"], {"fit": 60, "calibration": 20, "test": 20})
        for key in ("input", "splits", "purged", "excluded_observations", "excluded_episodes", "split_ids_sha256"):
            self.assertIn(key, body)

    def test_manifest_is_immutable(self):
        body = self.manifest.as_dict()
        body["seed"] = 0
        self.assertEqual(self.manifest.as_dict()["seed"], 77)
        with self.assertRaises(AttributeError):
            self.manifest.attempt = 5

    def test_tampered_manifest_is_refused(self):
        forged = self.manifest.canonical_json.replace('"seed":77', '"seed":78')
        with self.assertRaises(ex.ExperimentInputError):
            ex.SealedManifest(self.manifest.cohort, 1, self.manifest.content_sha256, forged, self.manifest.manifest_id)
        rehashed = "manifest:sha256:" + hashlib.sha256(forged.encode()).hexdigest()
        with self.assertRaises(ex.ExperimentInputError):  # content hash no longer matches
            ex.SealedManifest(self.manifest.cohort, 1, self.manifest.content_sha256, forged, rehashed)


class TestTrialLedger(unittest.TestCase):
    def test_attempts_count_uncalibrated_never_decrease_and_do_not_double_count(self):
        ledger = ex.TrialLedger()
        small = evaluate(synthetic(4, 3, 5), seed=1)
        self.assertEqual(small.status, ex.EvaluationStatus.UNCALIBRATED)
        history = []
        ledger, first = ledger.register(small)
        history.append(ledger.attempts(COHORT))
        ledger, again = ledger.register(small)  # same manifest again
        history.append(ledger.attempts(COHORT))
        self.assertEqual(first, again)
        ledger, second = ledger.register(evaluate(synthetic(4, 3, 5), seed=2))
        history.append(ledger.attempts(COHORT))
        ledger, third = ledger.register(evaluate(synthetic(4, 20, 62), seed=2))
        history.append(ledger.attempts(COHORT))
        self.assertEqual(history, [1, 1, 2, 3])
        self.assertEqual([first.attempt, second.attempt, third.attempt], [1, 2, 3])
        self.assertEqual(json.loads(third.canonical_json)["attempt"], 3)

    def test_attempts_are_per_cohort(self):
        short = ex.CohortKey(SETUP, Direction.SHORT, Horizon.H1, FEATURES, OUTCOME_POLICY_VERSION)
        ledger, _ = ex.TrialLedger().register(evaluate([observation("A", T0)], seed=1))
        ledger, other = ledger.register(evaluate([observation("A", T0)], seed=1, cohort=short))
        self.assertEqual(other.attempt, 1)
        self.assertEqual(ledger.attempts(COHORT), 1)
        self.assertEqual(ledger.attempts(short), 1)

    def test_rebuilt_ledger_is_verified(self):
        ledger, one = ex.TrialLedger().register(evaluate([observation("A", T0)], seed=1))
        ledger, two = ledger.register(evaluate([observation("A", T0)], seed=2))
        self.assertEqual(ex.TrialLedger((one, two)).attempts(COHORT), 2)
        with self.assertRaises(ex.ExperimentInputError):  # numbering would go 2 then 1
            ex.TrialLedger((two, one))
        with self.assertRaises(ex.ExperimentInputError):  # a gap: attempt 2 without 1
            ex.TrialLedger((two,))
        with self.assertRaises(ex.ExperimentInputError):  # same content twice
            ex.TrialLedger((one, one))


class TestInputValidation(unittest.TestCase):
    def test_bad_arguments_are_refused(self):
        with self.assertRaises(ex.ExperimentInputError):
            ex.CohortKey(SETUP, Direction.NONE, Horizon.H1, FEATURES, OUTCOME_POLICY_VERSION)
        with self.assertRaises(ex.ExperimentInputError):
            evaluate([], seed=-1)
        with self.assertRaises(ex.ExperimentInputError):
            evaluate([], seed=True)
        with self.assertRaises(ex.ExperimentInputError):
            ex.evaluate_cohort([], cohort=COHORT, benchmark=BENCHMARK, seed=1, evaluated_as_of=datetime(2026, 1, 1))
        with self.assertRaises(ex.ExperimentInputError):
            evaluate([object()])

    def test_evaluation_cannot_claim_splits_while_uncalibrated(self):
        text = ex.canonical_json({"x": 1})
        digest = hashlib.sha256(text.encode()).hexdigest()
        empty = ex.SplitAssignment((), (), (), None, None)
        with self.assertRaises(ex.ExperimentInputError):
            ex.ExperimentEvaluation(
                COHORT, BENCHMARK, 1, ex.EvaluationStatus.UNCALIBRATED, (ex.FailedMinimum.CALENDAR_DAYS,), empty, text, digest
            )
        with self.assertRaises(ex.ExperimentInputError):
            ex.ExperimentEvaluation(COHORT, BENCHMARK, 1, ex.EvaluationStatus.SAMPLE_SUFFICIENT, (), None, text, digest)


class TestModuleBoundary(unittest.TestCase):
    def test_domain_module_imports_no_io(self):
        with open(os.path.join(REPO_ROOT, "radar_v08", "domain", "experiments.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(
            imported,
            {
                "__future__", "hashlib", "json", "collections.abc", "dataclasses", "datetime", "decimal", "enum",
                "radar_v08.domain.invocation", "radar_v08.domain.outcomes",
            },
        )


if __name__ == "__main__":
    unittest.main()
