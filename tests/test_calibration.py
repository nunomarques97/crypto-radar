"""T042b: regularized logistic calibration on the T042a splits, Brier against the prevalence
baseline and the ten-bin calibration error (``radar_v08/domain/calibration.py``).

Every observation is synthetic and built in memory through the real T041 path
(``outcomes.label_horizon``) and the real T042a ``evaluate_cohort``; variety comes from
``random.Random(seed)``. Nothing reads or writes a file, a database, the network, a model or
the UI; no real backtest. Golden values are derived by hand in the comments.
"""

import ast
import dataclasses
import hashlib
import inspect
import json
import math
import os
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

from radar_v08.domain import calibration as cal  # noqa: E402
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
T0 = datetime(2026, 1, 1, tzinfo=UTC)
COST_POLICY = "COST-1"
COHORT = ex.CohortKey("breakout", Direction.LONG, Horizon.H1, "FEAT-1", OUTCOME_POLICY_VERSION)
BENCHMARK = ex.BenchmarkKey("prevalence-baseline", ex.BenchmarkTarget.NET_MARKOUT_POSITIVE, COST_POLICY)
SPEC = cal.FeatureSpec(("score", "noise"))
R = cal.UnavailableReason
S = cal.CalibrationStatus


def observation(pair, decided, positive, negative_mid=Decimal("99")):
    """LONG, entry mid 100, cost 0.002: exit mid 101 (net 0.01-0.002 > 0) when ``positive``, else
    ``negative_mid`` (99: net < 0; 100.2: net exactly 0)."""
    costs = (HorizonCost(Horizon.H1, RecordedCost(COST_POLICY, Side.LONG, InstrumentKind.SPOT, CostStatus.COMPLETE, Decimal("0.002"))),)
    subject = OutcomeSubject(
        instrument=InstrumentId("kraken", pair, InstrumentKind.SPOT, pair, "USD", pair),
        pair=pair,
        direction=Direction.LONG,
        decision_as_of=decided,
        entry_mid=Decimal("100"),
        entry_observed_at=decided,
        evidence=LinkMissingReason.NOT_RECORDED,
        decision=DecisionRef(DecisionKind.DECISION, f"d-{pair}-{decided.isoformat()}"),
        arm="L3-only",
        costs=costs,
    )
    exit_at = decided + Horizon.H1.duration
    mid = Decimal("101") if positive else negative_mid
    quotes = [QuoteObservation(exit_at, mid - Decimal("0.5"), mid + Decimal("0.5"), "fx")]
    label = label_horizon(subject, Horizon.H1, quotes, now=exit_at + timedelta(minutes=2), tolerance=timedelta(minutes=2))
    return ex.CohortObservation(LinkedOutcome(subject, label), "breakout", "FEAT-1")


def logistic_truth(slope=2.0):
    return lambda x, day: cal.sigmoid(slope * x)


def world(seed, days, truth, *, pairs=20, override=None, negative_mid=Decimal("99")):
    """``pairs`` x ``days`` single-opportunity episodes (one seal per pair per day, 67 minutes
    apart, so no episode merges and no tie at a cut). Each episode draws a score ``x ~ N(0,1)``,
    a label ``Bernoulli(truth(x, day))`` and an unrelated ``noise ~ N(0,1)`` feature, always in
    the same order from ``random.Random(seed)``. ``override(decided, x, noise, positive)`` may
    replace the drawn values of chosen episodes after the draw (the stream is unchanged)."""
    rng = random.Random(seed)
    items, rows = [], []
    for p in range(pairs):
        for day in range(days):
            decided = T0 + timedelta(days=day, minutes=67 * p)
            x = rng.gauss(0.0, 1.0)
            positive = rng.random() < truth(x, day)
            noise = rng.gauss(0.0, 1.0)
            if override is not None:
                x, noise, positive = override(decided, x, noise, positive)
            item = observation(f"P{p:02d}", decided, positive, negative_mid)
            items.append(item)
            rows.append(cal.EpisodeFeatures(item.outcome.label.subject_id, (x, noise)))
    return items, rows


def evaluate(items, days, seed=11):
    return ex.evaluate_cohort(
        items, cohort=COHORT, benchmark=BENCHMARK, seed=seed, evaluated_as_of=T0 + timedelta(days=days + 2)
    )


def run(seed, days, truth, **kwargs):
    items, rows = world(seed, days, truth, **kwargs)
    evaluation = evaluate(items, days)
    return evaluation, rows, cal.calibrate(evaluation, SPEC, rows)


def section(result):
    return json.loads(result.evaluation.content_json)["calibration"]


def hexes(fit):
    return [fit.intercept.hex(), *(w.hex() for w in fit.weights), str(fit.iterations)]


class TestGoldenMetrics(unittest.TestCase):
    # 20 samples, 10 bins of 2, given shuffled. Sorted by probability (ties by id):
    # bin k holds two samples at p = 0.1*(k+1) with labels:
    #   0.1:(0,0) 0.2:(0,1) 0.3:(0,0) 0.4:(1,0) 0.5:(1,0) 0.6:(1,1) 0.7:(1,0) 0.8:(1,1) 0.9:(1,1) 1.0:(1,1)
    # gaps |p - rate|: .1 .3 .3 .1 0 .4 .2 .2 .1 0 -> ECE = mean = 1.7/10 = 0.17
    # (p-y)^2 sums per bin: .02 .68 .18 .52 .50 .32 .58 .08 .02 0 -> 2.9 -> Brier 0.145
    # positives 12/20 -> prevalence 0.6, prevalence Brier 0.6*0.4 = 0.24
    LABELS = [(0, 0), (0, 1), (0, 0), (1, 0), (1, 0), (1, 1), (1, 0), (1, 1), (1, 1), (1, 1)]

    def samples(self):
        items = []
        for k, pair in enumerate(self.LABELS):
            for j, y in enumerate(pair):
                items.append(((k + 1) / 10, y, f"id-{k:02d}-{j}"))
        random.Random(3).shuffle(items)
        return [p for p, _, _ in items], [y for _, y, _ in items], [i for _, _, i in items]

    def test_hand_brier_and_calibration_error_small_set(self):
        metrics = cal.calibration_metrics(*self.samples())
        self.assertAlmostEqual(metrics.brier, 0.145, places=12)
        self.assertAlmostEqual(metrics.prevalence, 0.6, places=12)
        self.assertAlmostEqual(metrics.prevalence_brier, 0.24, places=12)
        self.assertAlmostEqual(metrics.calibration_error, 0.17, places=12)
        self.assertEqual([b.count for b in metrics.bins], [2] * 10)
        self.assertEqual([b.positives for b in metrics.bins], [0, 1, 0, 1, 1, 2, 1, 2, 2, 2])
        self.assertEqual(metrics.small_bins, 10)
        self.assertEqual(cal.probability_decision(metrics), (R.SMALL_BINS, R.CALIBRATION_ERROR_HIGH))

    def test_hand_perfectly_calibrated_200(self):
        # 10 groups of 20 at p_k = 0.05 + 0.1k with 2k+1 positives: rate = p_k exactly -> ECE 0.
        # Brier = mean_k p_k(1-p_k) = 2*(.0475+.1275+.1875+.2275+.2475)/10 = 0.1675; prevalence
        # 100/200 = 0.5 -> 0.25. All bins hold 20: accepted.
        p, y, ids = self.grouped(20)
        metrics = cal.calibration_metrics(p, y, ids)
        self.assertAlmostEqual(metrics.brier, 0.1675, places=12)
        self.assertAlmostEqual(metrics.prevalence_brier, 0.25, places=12)
        self.assertAlmostEqual(metrics.calibration_error, 0.0, places=12)
        self.assertEqual([b.count for b in metrics.bins], [20] * 10)
        self.assertEqual(cal.probability_decision(metrics), ())

    def test_bin_with_fewer_than_20_samples_makes_probability_unavailable(self):
        # Same groups with one negative dropped each: 190 samples, bins of 19. Rates (2k+1)/19
        # differ from p_k by (2k+1)/380 -> ECE = 100/3800 ~ 0.026 (<= 0.05) and Brier still beats
        # the prevalence, so the only failed gate is the bin size.
        p, y, ids = self.grouped(19)
        metrics = cal.calibration_metrics(p, y, ids)
        self.assertEqual([b.count for b in metrics.bins], [19] * 10)
        self.assertAlmostEqual(metrics.calibration_error, 100 / 3800, places=12)
        self.assertLess(metrics.brier, metrics.prevalence_brier)
        self.assertEqual(cal.probability_decision(metrics), (R.SMALL_BINS,))

    @staticmethod
    def grouped(size):
        p, y, ids = [], [], []
        for k in range(10):
            positives = 2 * k + 1
            for j in range(size):
                p.append(0.05 + 0.1 * k)
                y.append(1 if j < positives else 0)
                ids.append(f"g{k}-{j:02d}")
        return p, y, ids

    def test_gate_bounds(self):
        bins = tuple(cal.ReliabilityBin(20, 10, 0.5, 0.5, 0.5, 0.5) for _ in range(10))
        at_limit = cal.CalibrationMetrics(200, 100, 0.2, 0.5, 0.25, 0.05, bins)
        self.assertEqual(cal.probability_decision(at_limit), ())  # error <= 0.05 is inclusive
        over = dataclasses.replace(at_limit, calibration_error=0.0500001)
        self.assertEqual(cal.probability_decision(over), (R.CALIBRATION_ERROR_HIGH,))
        tie = dataclasses.replace(at_limit, brier=0.25)  # must be strictly better
        self.assertEqual(cal.probability_decision(tie), (R.BRIER_NOT_BETTER,))

    def test_metric_inputs_are_validated(self):
        with self.assertRaises(cal.CalibrationInputError):
            cal.calibration_metrics([0.5] * 9, [0] * 9, [str(i) for i in range(9)])
        with self.assertRaises(cal.CalibrationInputError):
            cal.calibration_metrics([1.5] + [0.5] * 9, [0] * 10, [str(i) for i in range(10)])
        with self.assertRaises(cal.CalibrationInputError):
            cal.calibration_metrics([0.5] * 10, [0] * 10, ["x"] * 10)


class TestFitLogistic(unittest.TestCase):
    def test_optimality_condition_on_a_hand_example(self):
        # x=+1: a=30 positives, b=10 negatives; x=-1 mirrored (10 positives, 30 negatives).
        # By symmetry the intercept is 0; the gradient in w is 2(a+b)sigmoid(w) - 2a + l2*w = 0.
        rows = [(1.0,)] * 40 + [(-1.0,)] * 40
        labels = [1] * 30 + [0] * 10 + [1] * 10 + [0] * 30
        fit = cal.fit_logistic(rows, labels, l2=1.0)
        self.assertTrue(fit.converged)
        self.assertAlmostEqual(fit.intercept, 0.0, places=10)
        w = fit.weights[0]
        self.assertAlmostEqual(80 * cal.sigmoid(w) - 60 + 1.0 * w, 0.0, places=9)
        self.assertLess(w, math.log(3))  # the L2 penalty shrinks it below the unpenalized log(3)
        almost_free = cal.fit_logistic(rows, labels, l2=1e-9)
        self.assertAlmostEqual(almost_free.weights[0], math.log(3), places=6)

    def test_single_class_is_refused(self):
        with self.assertRaises(cal.CalibrationInputError):
            cal.fit_logistic([(1.0,), (2.0,)], [1, 1], l2=1.0)

    def test_fit_is_bit_for_bit_deterministic(self):
        rng = random.Random(5)
        rows = [(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(300)]
        labels = [1 if rng.random() < cal.sigmoid(r[0]) else 0 for r in rows]
        self.assertEqual(hexes(cal.fit_logistic(rows, labels, l2=1.0)), hexes(cal.fit_logistic(rows, labels, l2=1.0)))


class TestUncalibratedPassThrough(unittest.TestCase):
    def test_uncalibrated_fits_nothing_and_reads_no_feature(self):
        items, _ = world(1, 10, logistic_truth())  # 200 episodes over 10 days: minima fail
        evaluation = evaluate(items, 10)
        self.assertIs(evaluation.status, ex.EvaluationStatus.UNCALIBRATED)

        def never_read():
            raise AssertionError("features read for an UNCALIBRATED evaluation")
            yield  # pragma: no cover

        result = cal.calibrate(evaluation, SPEC, never_read())
        self.assertIs(result.status, S.UNCALIBRATED)
        self.assertEqual(result.reasons, ())
        self.assertIsNone(result.fit)
        self.assertIsNone(result.mapping)
        self.assertIsNone(result.metrics)
        self.assertIsNone(result.probability((0.0, 0.0)))
        body = section(result)
        self.assertEqual(body["status"], "UNCALIBRATED")
        for key in ("fit", "mapping", "metrics", "standardization"):
            self.assertIsNone(body[key])
        self.assertIs(result.evaluation.status, ex.EvaluationStatus.UNCALIBRATED)


class TestAcceptance(unittest.TestCase):
    def test_well_calibrated_synthetic_data_is_accepted(self):
        # truth P(y=1|x) = sigmoid(2x): a logistic model on the score is the right family.
        evaluation, _, result = run(21, 300, logistic_truth())
        self.assertIs(evaluation.status, ex.EvaluationStatus.SAMPLE_SUFFICIENT)
        self.assertIs(result.status, S.PROBABILITY_AVAILABLE, result.reasons)
        self.assertEqual(result.reasons, ())
        m = result.metrics
        self.assertGreaterEqual(min(b.count for b in m.bins), 20)
        self.assertLess(m.brier, m.prevalence_brier)
        self.assertLessEqual(m.calibration_error, 0.05)
        self.assertEqual(m.samples, len(evaluation.splits.test))
        score_weight, noise_weight = result.fit.weights
        self.assertGreater(score_weight, 1.0)
        self.assertLess(abs(noise_weight), 0.2)
        p_high, p_low = result.probability((2.0, 0.0)), result.probability((-2.0, 0.0))
        self.assertGreater(p_high, 0.9)
        self.assertLess(p_low, 0.1)

    def test_signal_free_data_is_unavailable(self):
        _, _, result = run(22, 100, lambda x, day: 0.5)
        self.assertIs(result.status, S.PROBABILITY_UNAVAILABLE)
        self.assertIn(R.BRIER_NOT_BETTER, result.reasons)
        self.assertIsNone(result.probability((2.0, 0.0)))

    def test_miscalibrated_test_block_is_unavailable_with_typed_reasons(self):
        # the relation flips from day 85 on (inside the test block only): the mapping learned on
        # the calibration block is badly wrong on the held-out block.
        _, _, result = run(23, 100, lambda x, day: cal.sigmoid(2 * x if day < 85 else -2 * x))
        self.assertIs(result.status, S.PROBABILITY_UNAVAILABLE)
        self.assertEqual(result.reasons, (R.BRIER_NOT_BETTER, R.CALIBRATION_ERROR_HIGH))
        self.assertGreater(result.metrics.calibration_error, 0.05)
        self.assertIsNone(result.probability((2.0, 0.0)))
        body = section(result)
        self.assertEqual(body["status"], "PROBABILITY_UNAVAILABLE")
        self.assertEqual(body["reasons"], ["brier_not_better", "calibration_error_high"])
        self.assertIsNotNone(body["metrics"])  # the numbers are reported, the probability is not

    def test_prevalence_shift_only_fails_the_calibration_error(self):
        # same slope, but from day 85 on the base rate shifts up (+2.5 on the log-odds): ranking
        # still works (Brier can beat the prevalence) but the probabilities are off.
        _, _, result = run(24, 100, lambda x, day: cal.sigmoid(2 * x + (0.0 if day < 85 else 2.5)))
        self.assertIs(result.status, S.PROBABILITY_UNAVAILABLE)
        self.assertIn(R.CALIBRATION_ERROR_HIGH, result.reasons)

    def test_score_and_inputs_are_untouched(self):
        items, rows = world(21, 100, logistic_truth())
        before = [(r.episode_id, r.values) for r in rows]
        evaluation = evaluate(items, 100)
        content_before = evaluation.content_json
        cal.calibrate(evaluation, SPEC, rows)
        self.assertEqual([(r.episode_id, r.values) for r in rows], before)
        self.assertEqual(evaluation.content_json, content_before)
        names = {f.name for f in dataclasses.fields(cal.CalibrationResult)}
        self.assertFalse({"score", "rank", "ranking"} & names)


class TestNoLeakage(unittest.TestCase):
    def split_starts(self, seed, days):
        items, _ = world(seed, days, logistic_truth())
        splits = evaluate(items, days).splits
        return splits.calibration_start, splits.test_start

    def poisoned(self, seed, days, start, end=None):
        def override(decided, x, noise, positive):
            if decided >= start and (end is None or decided < end):
                return -50.0 * x, 1e6, not positive  # absurd features, flipped labels
            return x, noise, positive

        return run(seed, days, logistic_truth(), override=override)

    def test_poisoned_test_block_does_not_change_the_fit_or_the_mapping(self):
        _, test_start = self.split_starts(31, 100)
        _, _, clean = run(31, 100, logistic_truth())
        _, _, dirty = self.poisoned(31, 100, test_start)
        self.assertEqual(hexes(clean.fit), hexes(dirty.fit))
        self.assertEqual(hexes(clean.mapping), hexes(dirty.mapping))
        self.assertEqual(clean.standardization, dirty.standardization)
        self.assertNotEqual(clean.metrics.brier, dirty.metrics.brier)  # the test block is really poisoned
        self.assertIs(dirty.status, S.PROBABILITY_UNAVAILABLE)

    def test_poisoned_calibration_block_changes_only_the_mapping(self):
        calibration_start, test_start = self.split_starts(32, 100)
        _, _, clean = run(32, 100, logistic_truth())
        _, _, dirty = self.poisoned(32, 100, calibration_start, test_start)
        self.assertEqual(hexes(clean.fit), hexes(dirty.fit))
        self.assertNotEqual(hexes(clean.mapping), hexes(dirty.mapping))


class TestReplayAndManifest(unittest.TestCase):
    def test_same_seed_replays_coefficients_and_hash(self):
        _, _, first = run(41, 100, logistic_truth())
        _, _, second = run(41, 100, logistic_truth())
        self.assertEqual(hexes(first.fit), hexes(second.fit))
        self.assertEqual(hexes(first.mapping), hexes(second.mapping))
        self.assertEqual(first.evaluation.content_json, second.evaluation.content_json)
        self.assertEqual(first.evaluation.content_sha256, second.evaluation.content_sha256)
        _, _, other = run(42, 100, logistic_truth())
        self.assertNotEqual(first.evaluation.content_sha256, other.evaluation.content_sha256)

    def test_input_order_does_not_change_the_result(self):
        items, rows = world(43, 100, logistic_truth())
        first = cal.calibrate(evaluate(items, 100), SPEC, rows)
        rng = random.Random(1)
        rng.shuffle(items)
        rng.shuffle(rows)
        second = cal.calibrate(evaluate(items, 100), SPEC, rows)
        self.assertEqual(first.evaluation.content_sha256, second.evaluation.content_sha256)

    def test_manifest_extends_the_t042a_manifest(self):
        evaluation, rows, result = run(44, 100, logistic_truth())
        base = json.loads(evaluation.content_json)
        body = json.loads(result.evaluation.content_json)
        calibration = body.pop("calibration")
        self.assertEqual(body, base)  # every T042a field unchanged
        text = result.evaluation.content_json
        self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=True))
        self.assertEqual(result.evaluation.content_sha256, hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(result.evaluation.seed, evaluation.seed)
        self.assertEqual(calibration["seed"], 11)
        self.assertEqual(calibration["policy_version"], "CALIBRATION-1")
        policy = calibration["policy"]
        self.assertEqual(
            (policy["fit_l2"], policy["calibration_l2"], policy["max_iterations"], policy["tolerance"]),
            (1.0, 1.0, 100, 1e-9),
        )
        self.assertEqual((policy["bins"], policy["min_bin_samples"], policy["max_calibration_error"]), (10, 20, 0.05))
        self.assertEqual(policy["random_draws"], 0)
        self.assertEqual(calibration["features"]["names"], ["score", "noise"])
        self.assertEqual(calibration["features"]["version"], "FEAT-1")
        self.assertEqual(calibration["fit"]["intercept"], result.fit.intercept)
        self.assertEqual(calibration["fit"]["weights"], list(result.fit.weights))
        self.assertEqual(calibration["mapping"]["weights"], list(result.mapping.weights))
        self.assertEqual(calibration["metrics"]["brier"], result.metrics.brier)
        self.assertEqual(len(calibration["metrics"]["reliability"]), 10)
        self.assertEqual(calibration["samples"], {s.value: len(evaluation.splits.episodes(s)) for s in ex.SPLITS})
        # sealed as one attempt by the unchanged T042a ledger, and verified on construction
        ledger, sealed = ex.TrialLedger().register(result.evaluation)
        self.assertEqual(sealed.attempt, 1)
        self.assertEqual(ledger.attempts(COHORT), 1)
        self.assertEqual(sealed.as_dict()["calibration"], calibration)

    def test_already_calibrated_evaluation_is_refused(self):
        _, rows, result = run(45, 100, logistic_truth())
        with self.assertRaises(cal.CalibrationInputError) as caught:
            cal.calibrate(result.evaluation, SPEC, rows)
        self.assertIs(caught.exception.code, cal.CalibrationErrorCode.INCONSISTENT)


class TestLabel(unittest.TestCase):
    def test_net_exactly_zero_is_a_negative(self):
        # every negative exits at 100.2: gross 0.002 - cost 0.002 = net exactly 0 (not > 0)
        items, rows = world(71, 100, logistic_truth(), negative_mid=Decimal("100.2"))
        zero_nets = [i for i in items if i.outcome.label.net_markout == 0]
        self.assertTrue(zero_nets)
        evaluation = evaluate(items, 100)
        result = cal.calibrate(evaluation, SPEC, rows)
        expected = {
            s.value: sum(1 for e in evaluation.splits.episodes(s) if e.net_markout > Decimal("0.001")) for s in ex.SPLITS
        }
        self.assertEqual(section(result)["positives"], expected)
        _, _, reference = run(71, 100, logistic_truth())  # same draws, negatives at 99
        self.assertEqual(hexes(result.fit), hexes(reference.fit))


class TestUnfittableBlocks(unittest.TestCase):
    def test_missing_feature_row_is_unavailable_never_zero(self):
        items, rows = world(51, 100, logistic_truth())
        evaluation = evaluate(items, 100)
        first_test = evaluation.splits.test[0].episode_id
        kept = [r for r in rows if r.episode_id != first_test]
        extra = cal.EpisodeFeatures("not-an-episode", (0.0, 0.0))
        result = cal.calibrate(evaluation, SPEC, [*kept, extra])
        self.assertIs(result.status, S.PROBABILITY_UNAVAILABLE)
        self.assertEqual(result.reasons, (R.MISSING_FEATURES,))
        self.assertIsNone(result.fit)
        body = section(result)
        self.assertEqual(body["missing_feature_rows"], {"fit": 0, "calibration": 0, "test": 1})
        # unused: the extra row plus the rows of the 40 purged episodes (one per pair in each
        # 24h window before the two boundaries: 2 * 20 pairs)
        purged = json.loads(evaluation.content_json)["purged"]
        self.assertEqual(purged, {"boundary_window": 40, "label_after_block_end": 0})
        self.assertEqual(body["unused_feature_rows"], 41)

    def test_single_class_fit_block_is_unavailable(self):
        # no positive before day 62: the whole fit block (days 0..~59) is negative
        _, _, result = run(52, 100, lambda x, day: 0.0 if day < 62 else cal.sigmoid(2 * x))
        self.assertIs(result.status, S.PROBABILITY_UNAVAILABLE)
        self.assertEqual(result.reasons, (R.SINGLE_CLASS_BLOCK,))
        self.assertEqual(section(result)["positives"]["fit"], 0)


class TestInputContract(unittest.TestCase):
    def test_self_confidence_is_never_a_declared_feature(self):
        for name in ("model_confidence", "Confidence", "self_certainty"):
            with self.assertRaises(cal.CalibrationInputError) as caught:
                cal.FeatureSpec(("score", name))
            self.assertIs(caught.exception.code, cal.CalibrationErrorCode.SELF_CONFIDENCE_REFUSED)
        self.assertEqual([f.name for f in dataclasses.fields(cal.EpisodeFeatures)], ["episode_id", "values"])
        self.assertEqual(list(inspect.signature(cal.calibrate).parameters), ["evaluation", "spec", "features"])

    def test_invalid_values_are_refused(self):
        for values in ((float("nan"),), (float("inf"),), (True,), ("1",), ()):
            with self.assertRaises(cal.CalibrationInputError):
                cal.EpisodeFeatures("e", values)
        for names in ((), ("a", "a"), ("bad name",), tuple(f"f{i}" for i in range(21))):
            with self.assertRaises(cal.CalibrationInputError):
                cal.FeatureSpec(names)

    def test_wrong_width_and_duplicate_rows_are_refused(self):
        items, rows = world(61, 100, logistic_truth())
        evaluation = evaluate(items, 100)
        with self.assertRaises(cal.CalibrationInputError):
            cal.calibrate(evaluation, cal.FeatureSpec(("score",)), rows)
        with self.assertRaises(cal.CalibrationInputError) as caught:
            cal.calibrate(evaluation, SPEC, [*rows, rows[0]])
        self.assertIs(caught.exception.code, cal.CalibrationErrorCode.DUPLICATE_FEATURES)


class TestModuleBoundary(unittest.TestCase):
    def test_domain_module_imports_no_io_and_no_numeric_library(self):
        with open(os.path.join(REPO_ROOT, "radar_v08", "domain", "calibration.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(
            imported,
            {"__future__", "hashlib", "json", "math", "collections.abc", "dataclasses", "enum", "radar_v08.domain.experiments"},
        )


if __name__ == "__main__":
    unittest.main()
