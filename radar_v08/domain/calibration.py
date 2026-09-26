"""Regularized logistic calibration on the experiment-ledger splits, with Brier against the prevalence
baseline and a ten-bin calibration error on the held-out block (OPERATING_CONTRACTS.md §7).

Pure: no I/O, no wall clock, no configuration, no random draws. Nothing here is wired to the
runtime; scores, ranking, alerts and prompts never read it.

Input
-----

An experiment-ledger ``ExperimentEvaluation`` plus one ``EpisodeFeatures`` row per episode: the
deterministic score and/or features the caller declares in a ``FeatureSpec``, computed with
the cohort's ``feature_version``. There is no field for a model's self-confidence, and a
declared feature whose name says confidence or certainty is refused
(``SELF_CONFIDENCE_REFUSED``): a model's self-score is never calibrated as evidence. The
label is the benchmark target ``net_markout > 0`` of the episode's seal (the ledger already
excluded every missing net; a net of exactly 0 is a negative).

If the evaluation is ``UNCALIBRATED`` nothing is fitted, no feature row is read and the
result is ``UNCALIBRATED``.

Procedure (fixed policy ``CALIBRATION-1``, written in the manifest)
-------------------------------------------------------------------

1. Fit block only: each feature is standardized with the fit block's mean and population
   standard deviation (a constant feature keeps scale 1), then an L2-regularized logistic
   regression (penalty ``FIT_L2`` on the weights, intercept not penalized) is fitted by
   Newton's method from all-zero parameters, at most ``MAX_ITERATIONS`` iterations, stopping
   when the largest parameter change is ``<= TOLERANCE``. A step that does not decrease the
   objective is halved (Armijo backtracking), so each iteration is deterministic.
2. Calibration block only: the fit model's linear score ``z`` is mapped by a second, one
   feature L2 logistic regression ``p = sigmoid(c + a*z)`` (Platt mapping, penalty
   ``CALIBRATION_L2`` on ``a``), same solver and stopping rule.
3. Test block only: calibrated probabilities are scored. Brier = mean of ``(p - y)^2``.
   Prevalence baseline = the test block's own positive rate predicted for every episode
   (the best constant on the test block, so the strictest baseline). Calibration error =
   sample-weighted mean over ten equal-frequency bins of ``|mean p - positive rate|``;
   episodes are sorted by (probability, episode id) and bin ``k`` holds positions
   ``[k*n//10, (k+1)*n//10)``. The reliability curve is the per-bin mean probability and
   positive rate.
4. Probability is accepted (``PROBABILITY_AVAILABLE``) only if every bin has at least
   ``MIN_BIN_SAMPLES`` samples, Brier is strictly below the prevalence Brier and the
   calibration error is ``<= MAX_CALIBRATION_ERROR``. Otherwise ``PROBABILITY_UNAVAILABLE``
   with every failed reason, in fixed order; the caller's score and rank stay what they
   were (this module never produces or changes one).

A block that cannot be fitted is also ``PROBABILITY_UNAVAILABLE``, never a number: a split
episode without a feature row (``MISSING_FEATURES``, missing is not zero), an empty block
(``EMPTY_BLOCK``), a fit or calibration block with one class only (``SINGLE_CLASS_BLOCK``:
the unpenalized intercept has no finite optimum) and a solver that does not converge
(``NOT_CONVERGED``).

Manifest
--------

``calibrate`` returns a new ``ExperimentEvaluation`` whose canonical content is the ledger's
content plus one ``calibration`` key: policy, feature spec and the sha256 of the feature
rows used, status and reasons, sample and positive counts per block, standardization,
coefficients, iterations, metrics and reliability curve. The seed is the ledger's seed (no
step here draws a random number). Floats are written by ``json`` as their shortest
round-trip text, so the same inputs give the same bytes and the same ``content_sha256``,
and ``TrialLedger.register`` seals it as one attempt of the cohort.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from radar_v08.domain.experiments import (
    Episode,
    EvaluationStatus,
    ExperimentEvaluation,
    Split,
    canonical_json,
)

CALIBRATION_POLICY_VERSION = "CALIBRATION-1"
CALIBRATION_KEY = "calibration"

FIT_L2 = 1.0
CALIBRATION_L2 = 1.0
MAX_ITERATIONS = 100
TOLERANCE = 1e-9
ARMIJO_FRACTION = 1e-4
MAX_HALVINGS = 60
#: Relative float slack in the Armijo test: near the optimum the objective no longer moves
#: by more than rounding, so a step that "increases" it by a few ulps is still accepted.
OBJECTIVE_SLACK = 1e-12

BIN_COUNT = 10
MIN_BIN_SAMPLES = 20
MAX_CALIBRATION_ERROR = 0.05

MAX_FEATURES = 20
MAX_NAME_LENGTH = 64
_REFUSED_NAME_PARTS = ("confidence", "certainty")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CalibrationErrorCode(Enum):
    INVALID_FIELD = "invalid_field"
    SELF_CONFIDENCE_REFUSED = "self_confidence_refused"
    DUPLICATE_FEATURES = "duplicate_features"
    INCONSISTENT = "inconsistent"


class CalibrationInputError(ValueError):
    """A calibration input was refused; ``code`` says why."""

    def __init__(self, code: CalibrationErrorCode, field: str, detail: str) -> None:
        super().__init__(f"{code.value}: {field}: {detail}")
        self.code = code
        self.field = field
        self.detail = detail


def _invalid(field: str, detail: str) -> CalibrationInputError:
    return CalibrationInputError(CalibrationErrorCode.INVALID_FIELD, field, detail)


def _require_finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _invalid(field, "must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise _invalid(field, "must be finite")
    return number


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class CalibrationStatus(Enum):
    UNCALIBRATED = "UNCALIBRATED"  # sample-size minima failed: nothing fitted
    PROBABILITY_AVAILABLE = "PROBABILITY_AVAILABLE"  # every §7 gate passed on the test block
    PROBABILITY_UNAVAILABLE = "PROBABILITY_UNAVAILABLE"  # score/rank only, with typed reasons


class UnavailableReason(Enum):
    MISSING_FEATURES = "missing_features"
    EMPTY_BLOCK = "empty_block"
    SINGLE_CLASS_BLOCK = "single_class_block"
    NOT_CONVERGED = "not_converged"
    SMALL_BINS = "small_bins"
    BRIER_NOT_BETTER = "brier_not_better"
    CALIBRATION_ERROR_HIGH = "calibration_error_high"


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """The declared deterministic score/features, in the order of every ``values`` tuple."""

    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.names, tuple) or not 1 <= len(self.names) <= MAX_FEATURES:
            raise _invalid("FeatureSpec.names", f"must be a tuple of 1..{MAX_FEATURES} names")
        for name in self.names:
            if not isinstance(name, str) or not name or len(name) > MAX_NAME_LENGTH:
                raise _invalid("FeatureSpec.names", f"each name is 1..{MAX_NAME_LENGTH} chars of text")
            if not all(char.isascii() and (char.isalnum() or char in "_.-") for char in name):
                raise _invalid("FeatureSpec.names", f"{name!r}: only ASCII letters, digits, '_', '.', '-'")
            if any(part in name.lower() for part in _REFUSED_NAME_PARTS):
                raise CalibrationInputError(
                    CalibrationErrorCode.SELF_CONFIDENCE_REFUSED,
                    "FeatureSpec.names",
                    f"{name!r}: a model's self-confidence is never calibrated as evidence",
                )
        if len(set(self.names)) != len(self.names):
            raise CalibrationInputError(CalibrationErrorCode.DUPLICATE_FEATURES, "FeatureSpec.names", "repeated name")


@dataclass(frozen=True, slots=True)
class EpisodeFeatures:
    """The declared feature values of one episode (keyed by the ledger episode id)."""

    episode_id: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.episode_id, str) or not self.episode_id:
            raise _invalid("EpisodeFeatures.episode_id", "must be non-empty text")
        if not isinstance(self.values, tuple) or not self.values:
            raise _invalid("EpisodeFeatures.values", "must be a non-empty tuple")
        for value in self.values:
            _require_finite(value, "EpisodeFeatures.values")


# ---------------------------------------------------------------------------
# Numerics: regularized logistic regression by Newton's method
# ---------------------------------------------------------------------------


def sigmoid(z: float) -> float:
    """Logistic function without overflow."""
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _log_loss(z: float, y: int) -> float:
    """``-log p(y | z)`` computed stably: ``log(1 + e^z) - y*z``."""
    return max(z, 0.0) + math.log1p(math.exp(-abs(z))) - y * z


@dataclass(frozen=True, slots=True)
class LogisticFit:
    """``p = sigmoid(intercept + sum(weights * x))``; ``converged`` per the fixed stopping rule."""

    intercept: float
    weights: tuple[float, ...]
    iterations: int
    converged: bool

    def linear(self, row: Sequence[float]) -> float:
        return self.intercept + math.fsum(w * x for w, x in zip(self.weights, row, strict=True))

    def as_json(self) -> dict[str, object]:
        return {
            "intercept": self.intercept,
            "weights": list(self.weights),
            "iterations": self.iterations,
            "converged": self.converged,
        }


class _SingularSystem(ArithmeticError):
    pass


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (first largest pivot on ties)."""
    size = len(vector)
    a = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(a[r][column]))
        if not abs(a[pivot][column]) > 1e-300:
            raise _SingularSystem("singular Hessian")
        a[column], a[pivot] = a[pivot], a[column]
        for row in range(column + 1, size):
            factor = a[row][column] / a[column][column]
            if factor != 0.0:
                for k in range(column, size + 1):
                    a[row][k] -= factor * a[column][k]
    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        tail = math.fsum(a[row][k] * solution[k] for k in range(row + 1, size))
        solution[row] = (a[row][size] - tail) / a[row][row]
    return solution


def fit_logistic(
    rows: Sequence[Sequence[float]],
    labels: Sequence[int],
    *,
    l2: float,
    max_iterations: int = MAX_ITERATIONS,
    tolerance: float = TOLERANCE,
) -> LogisticFit:
    """Minimize ``sum(log loss) + l2/2 * |weights|^2`` (intercept free) from zero parameters.

    Deterministic: fixed start, fixed iteration order, ``math.fsum`` for every sum. Needs at
    least one row of each class; raises ``CalibrationInputError`` otherwise.
    """
    if len(rows) != len(labels) or not rows:
        raise _invalid("rows", "rows and labels must be non-empty and of equal length")
    width = len(rows[0])
    if width < 1 or any(len(row) != width for row in rows):
        raise _invalid("rows", "every row needs the same number (>= 1) of features")
    if any(label not in (0, 1) or isinstance(label, bool) for label in labels):
        raise _invalid("labels", "must be 0 or 1")
    if len(set(labels)) < 2:
        raise _invalid("labels", "both classes are needed for a finite intercept")
    penalty = _require_finite(l2, "l2")
    if penalty <= 0.0:
        raise _invalid("l2", "must be > 0")
    data = [[1.0, *(float(value) for value in row)] for row in rows]
    size = width + 1
    reg = [0.0] + [penalty] * width

    def objective(beta: list[float]) -> float:
        z = [math.fsum(b * x for b, x in zip(beta, row, strict=True)) for row in data]
        loss = math.fsum(_log_loss(zi, yi) for zi, yi in zip(z, labels, strict=True))
        return loss + 0.5 * math.fsum(r * b * b for r, b in zip(reg, beta, strict=True))

    beta = [0.0] * size
    current = objective(beta)
    for iteration in range(1, max_iterations + 1):
        probabilities = [sigmoid(math.fsum(b * x for b, x in zip(beta, row, strict=True))) for row in data]
        gradient = [
            math.fsum((p - y) * row[j] for p, y, row in zip(probabilities, labels, data, strict=True))
            + reg[j] * beta[j]
            for j in range(size)
        ]
        weights = [p * (1.0 - p) for p in probabilities]
        hessian = [
            [
                math.fsum(w * row[i] * row[j] for w, row in zip(weights, data, strict=True))
                + (reg[i] if i == j else 0.0)
                for j in range(size)
            ]
            for i in range(size)
        ]
        try:
            step = _solve(hessian, gradient)
        except _SingularSystem:
            return LogisticFit(beta[0], tuple(beta[1:]), iteration, False)
        decrease = math.fsum(g * s for g, s in zip(gradient, step, strict=True))
        scale = 1.0
        for _ in range(MAX_HALVINGS):
            candidate = [b - scale * s for b, s in zip(beta, step, strict=True)]
            value = objective(candidate)
            if value <= current - ARMIJO_FRACTION * scale * decrease + OBJECTIVE_SLACK * abs(current):
                break
            scale *= 0.5
        else:
            return LogisticFit(beta[0], tuple(beta[1:]), iteration, False)
        change = max(abs(scale * s) for s in step)
        beta, current = candidate, value
        if change <= tolerance:
            return LogisticFit(beta[0], tuple(beta[1:]), iteration, True)
    return LogisticFit(beta[0], tuple(beta[1:]), max_iterations, False)


# ---------------------------------------------------------------------------
# Metrics on the held-out block
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """One equal-frequency bin of the reliability curve."""

    count: int
    positives: int
    mean_probability: float
    positive_rate: float
    lowest: float
    highest: float

    def as_json(self) -> dict[str, object]:
        return {
            "count": self.count,
            "positives": self.positives,
            "mean_probability": self.mean_probability,
            "positive_rate": self.positive_rate,
            "lowest": self.lowest,
            "highest": self.highest,
        }


@dataclass(frozen=True, slots=True)
class CalibrationMetrics:
    samples: int
    positives: int
    brier: float
    prevalence: float
    prevalence_brier: float
    calibration_error: float
    bins: tuple[ReliabilityBin, ...]

    @property
    def small_bins(self) -> int:
        return sum(1 for item in self.bins if item.count < MIN_BIN_SAMPLES)

    def as_json(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "positives": self.positives,
            "brier": self.brier,
            "prevalence": self.prevalence,
            "prevalence_brier": self.prevalence_brier,
            "calibration_error": self.calibration_error,
            "small_bins": self.small_bins,
            "reliability": [item.as_json() for item in self.bins],
        }


def calibration_metrics(
    probabilities: Sequence[float], labels: Sequence[int], ids: Sequence[str]
) -> CalibrationMetrics:
    """Brier, prevalence Brier, ten-bin equal-frequency calibration error and reliability bins."""
    n = len(probabilities)
    if n != len(labels) or n != len(ids):
        raise _invalid("probabilities", "probabilities, labels and ids must have equal length")
    if n < BIN_COUNT:
        raise _invalid("probabilities", f"needs at least {BIN_COUNT} samples, one per bin")
    if len(set(ids)) != n:
        raise _invalid("ids", "must be unique")
    for p in probabilities:
        if not 0.0 <= _require_finite(p, "probabilities") <= 1.0:
            raise _invalid("probabilities", "must be in [0, 1]")
    if any(isinstance(y, bool) or y not in (0, 1) for y in labels):
        raise _invalid("labels", "must be 0 or 1")
    positives = sum(labels)
    brier = math.fsum((p - y) ** 2 for p, y in zip(probabilities, labels, strict=True)) / n
    prevalence = positives / n
    prevalence_brier = math.fsum((prevalence - y) ** 2 for y in labels) / n
    ordered = sorted(zip(probabilities, ids, labels, strict=True), key=lambda item: (item[0], item[1]))
    bins: list[ReliabilityBin] = []
    gaps: list[float] = []
    for k in range(BIN_COUNT):
        chunk = ordered[k * n // BIN_COUNT : (k + 1) * n // BIN_COUNT]
        count = len(chunk)
        hits = sum(item[2] for item in chunk)
        mean_p = math.fsum(item[0] for item in chunk) / count
        rate = hits / count
        bins.append(ReliabilityBin(count, hits, mean_p, rate, chunk[0][0], chunk[-1][0]))
        gaps.append(count * abs(mean_p - rate))
    return CalibrationMetrics(
        samples=n,
        positives=positives,
        brier=brier,
        prevalence=prevalence,
        prevalence_brier=prevalence_brier,
        calibration_error=math.fsum(gaps) / n,
        bins=tuple(bins),
    )


def probability_decision(metrics: CalibrationMetrics) -> tuple[UnavailableReason, ...]:
    """The §7 gates that fail, in fixed order; empty means the probability is accepted."""
    if not isinstance(metrics, CalibrationMetrics):
        raise _invalid("metrics", "must be CalibrationMetrics")
    reasons: list[UnavailableReason] = []
    if metrics.small_bins:
        reasons.append(UnavailableReason.SMALL_BINS)
    if not metrics.brier < metrics.prevalence_brier:
        reasons.append(UnavailableReason.BRIER_NOT_BETTER)
    if not metrics.calibration_error <= MAX_CALIBRATION_ERROR:
        reasons.append(UnavailableReason.CALIBRATION_ERROR_HIGH)
    return tuple(reasons)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Standardization:
    means: tuple[float, ...]
    scales: tuple[float, ...]

    def apply(self, values: Sequence[float]) -> tuple[float, ...]:
        return tuple((x - m) / s for x, m, s in zip(values, self.means, self.scales, strict=True))

    def as_json(self) -> dict[str, object]:
        return {"means": list(self.means), "scales": list(self.scales)}


def _standardization(rows: Sequence[Sequence[float]]) -> Standardization:
    count = len(rows)
    means: list[float] = []
    scales: list[float] = []
    for column in zip(*rows, strict=True):
        mean = math.fsum(column) / count
        spread = math.sqrt(math.fsum((x - mean) ** 2 for x in column) / count)
        means.append(mean)
        scales.append(spread if spread > 0.0 else 1.0)
    return Standardization(tuple(means), tuple(scales))


def _label(episode: Episode) -> int:
    return 1 if episode.net_markout > 0 else 0


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """Status, typed reasons, the fitted pieces that exist, and the extended ledger evaluation."""

    status: CalibrationStatus
    reasons: tuple[UnavailableReason, ...]
    standardization: Standardization | None
    fit: LogisticFit | None
    mapping: LogisticFit | None
    metrics: CalibrationMetrics | None
    evaluation: ExperimentEvaluation

    def probability(self, values: Sequence[float]) -> float | None:
        """Calibrated ``P(net_markout > 0)`` for declared values; ``None`` unless accepted."""
        if self.status is not CalibrationStatus.PROBABILITY_AVAILABLE:
            return None
        assert self.standardization is not None and self.fit is not None and self.mapping is not None
        row = [_require_finite(value, "values") for value in values]
        z = self.fit.linear(self.standardization.apply(row))
        return sigmoid(self.mapping.linear((z,)))


def _policy_json() -> dict[str, object]:
    return {
        "model": "logistic_l2_newton",
        "fit_l2": FIT_L2,
        "calibration_l2": CALIBRATION_L2,
        "intercept_penalized": False,
        "max_iterations": MAX_ITERATIONS,
        "tolerance": TOLERANCE,
        "armijo_fraction": ARMIJO_FRACTION,
        "max_halvings": MAX_HALVINGS,
        "objective_slack": OBJECTIVE_SLACK,
        "start": "zeros",
        "standardization": "fit_block_mean_population_stdev",
        "mapping": "platt_on_fit_linear_score",
        "label": "net_markout>0",
        "bins": BIN_COUNT,
        "bin_rule": "equal_frequency_sorted_by_probability_then_episode_id",
        "min_bin_samples": MIN_BIN_SAMPLES,
        "max_calibration_error": MAX_CALIBRATION_ERROR,
        "baseline": "test_block_prevalence",
        "random_draws": 0,
    }


def _extend(evaluation: ExperimentEvaluation, section: dict[str, object]) -> ExperimentEvaluation:
    body = json.loads(evaluation.content_json)
    assert isinstance(body, dict)
    body[CALIBRATION_KEY] = section
    text = canonical_json(body)
    return ExperimentEvaluation(
        cohort=evaluation.cohort,
        benchmark=evaluation.benchmark,
        seed=evaluation.seed,
        status=evaluation.status,
        failed_minima=evaluation.failed_minima,
        splits=evaluation.splits,
        content_json=text,
        content_sha256=_sha256(text),
    )


def calibrate(
    evaluation: ExperimentEvaluation, spec: FeatureSpec, features: Iterable[EpisodeFeatures]
) -> CalibrationResult:
    """Fit on the fit block, map on the calibration block, judge on the test block (§7)."""
    if not isinstance(evaluation, ExperimentEvaluation):
        raise _invalid("evaluation", "must be an ExperimentEvaluation")
    if not isinstance(spec, FeatureSpec):
        raise _invalid("spec", "must be FeatureSpec")
    body = json.loads(evaluation.content_json)
    if not isinstance(body, dict) or CALIBRATION_KEY in body:
        raise CalibrationInputError(CalibrationErrorCode.INCONSISTENT, "evaluation", "already calibrated")

    section: dict[str, object] = {
        "policy_version": CALIBRATION_POLICY_VERSION,
        "policy": _policy_json(),
        "seed": evaluation.seed,
        "features": {"version": evaluation.cohort.feature_version, "names": list(spec.names)},
    }

    def finish(
        status: CalibrationStatus,
        reasons: tuple[UnavailableReason, ...],
        standardization: Standardization | None = None,
        fit: LogisticFit | None = None,
        mapping: LogisticFit | None = None,
        metrics: CalibrationMetrics | None = None,
    ) -> CalibrationResult:
        section["status"] = status.value
        section["reasons"] = [reason.value for reason in reasons]
        section["standardization"] = None if standardization is None else standardization.as_json()
        section["fit"] = None if fit is None else fit.as_json()
        section["mapping"] = None if mapping is None else mapping.as_json()
        section["metrics"] = None if metrics is None else metrics.as_json()
        return CalibrationResult(
            status, reasons, standardization, fit, mapping, metrics, _extend(evaluation, section)
        )

    splits = evaluation.splits
    if evaluation.status is EvaluationStatus.UNCALIBRATED or splits is None:
        return finish(CalibrationStatus.UNCALIBRATED, ())

    rows: dict[str, tuple[float, ...]] = {}
    for item in features:
        if not isinstance(item, EpisodeFeatures):
            raise _invalid("features", "must be EpisodeFeatures")
        if len(item.values) != len(spec.names):
            raise _invalid("features", f"{item.episode_id}: {len(item.values)} values for {len(spec.names)} names")
        if item.episode_id in rows:
            raise CalibrationInputError(CalibrationErrorCode.DUPLICATE_FEATURES, "features", item.episode_id)
        rows[item.episode_id] = tuple(float(value) for value in item.values)

    blocks = {split: splits.episodes(split) for split in (Split.FIT, Split.CALIBRATION, Split.TEST)}
    used = {episode.episode_id for block in blocks.values() for episode in block}
    missing = {split.value: sum(1 for e in block if e.episode_id not in rows) for split, block in blocks.items()}
    section["samples"] = {split.value: len(block) for split, block in blocks.items()}
    section["positives"] = {split.value: sum(_label(e) for e in block) for split, block in blocks.items()}
    section["missing_feature_rows"] = missing
    section["unused_feature_rows"] = sum(1 for key in rows if key not in used)
    section["features"] = {
        "version": evaluation.cohort.feature_version,
        "names": list(spec.names),
        "rows_sha256": _sha256(
            canonical_json([[key, [value.hex() for value in rows[key]]] for key in sorted(used & rows.keys())])
        ),
    }
    if any(missing.values()):
        return finish(CalibrationStatus.PROBABILITY_UNAVAILABLE, (UnavailableReason.MISSING_FEATURES,))
    if any(not block for block in blocks.values()):
        return finish(CalibrationStatus.PROBABILITY_UNAVAILABLE, (UnavailableReason.EMPTY_BLOCK,))
    for split in (Split.FIT, Split.CALIBRATION):
        if len({_label(e) for e in blocks[split]}) < 2:
            return finish(CalibrationStatus.PROBABILITY_UNAVAILABLE, (UnavailableReason.SINGLE_CLASS_BLOCK,))

    fit_block = blocks[Split.FIT]
    standardization = _standardization([rows[e.episode_id] for e in fit_block])
    fit = fit_logistic(
        [standardization.apply(rows[e.episode_id]) for e in fit_block],
        [_label(e) for e in fit_block],
        l2=FIT_L2,
    )
    if not fit.converged:
        return finish(CalibrationStatus.PROBABILITY_UNAVAILABLE, (UnavailableReason.NOT_CONVERGED,), standardization, fit)

    def score(episode: Episode) -> float:
        return fit.linear(standardization.apply(rows[episode.episode_id]))

    calibration_block = blocks[Split.CALIBRATION]
    mapping = fit_logistic(
        [(score(e),) for e in calibration_block], [_label(e) for e in calibration_block], l2=CALIBRATION_L2
    )
    if not mapping.converged:
        return finish(
            CalibrationStatus.PROBABILITY_UNAVAILABLE, (UnavailableReason.NOT_CONVERGED,), standardization, fit, mapping
        )

    test_block = blocks[Split.TEST]
    metrics = calibration_metrics(
        [sigmoid(mapping.linear((score(e),))) for e in test_block],
        [_label(e) for e in test_block],
        [e.episode_id for e in test_block],
    )
    reasons = probability_decision(metrics)
    status = CalibrationStatus.PROBABILITY_UNAVAILABLE if reasons else CalibrationStatus.PROBABILITY_AVAILABLE
    return finish(status, reasons, standardization, fit, mapping, metrics)
