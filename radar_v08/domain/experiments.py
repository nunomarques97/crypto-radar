"""Reproducible experiment ledger: cohorts, episodes, purged chronological splits, sample
minima and sealed manifests with trial counts (OPERATING_CONTRACTS.md §7 and §10).

Pure: no I/O, no wall clock, no configuration, no randomness drawn. ``evaluated_as_of`` and
the ``seed`` are always passed in. Nothing here is persisted (there is no ledger store yet); the
``TrialLedger`` is an immutable in-memory value that a later store can rebuild and verify.

Input
-----

``CohortObservation`` = one ``LinkedOutcome`` (subject + matured label at one horizon)
plus the two cohort attributes an outcome does not carry: the deterministic strategy ``setup`` and
the ``feature_version`` it was computed with. The ``CohortKey`` fixes setup, direction
(LONG or SHORT), horizon, feature version and outcome policy version. An observation that
does not match it, or was decided after ``evaluated_as_of``, is excluded with a typed,
counted reason (``OBSERVATION_REASONS``).

Episodes (§10)
--------------

Observations of the same instrument and pair are grouped chronologically (ties by subject
id). The earliest seals an episode; every later observation decided less than
``MAX_LABEL_HORIZON`` after the seal overlaps it and is linked to it as a member, never an
extra independent sample. The next observation at or after ``seal + MAX_LABEL_HORIZON``
seals a new episode (anchored on the seal, no chaining). The episode's outcome is the seal's
label and nothing else: a member can never replace a seal whose label is missing. An episode
is excluded with a typed, counted reason (``EPISODE_REASONS``) when the seal's label is not
available by ``evaluated_as_of``, its net markout is missing (``GROSS_UNAVAILABLE`` /
``COST_NOT_RECORDED`` / ``COST_INCOMPLETE`` of the outcome labels: never read as zero), or its net was
priced under another cost policy than the benchmark's.

Chronological split (§7)
------------------------

Eligible episodes are ordered by (seal decision time, episode id), so input order never
matters. With ``N`` of them, the calibration block starts at the decision time of episode
``N*60//100`` and the test block at that of episode ``N*80//100`` (integer arithmetic; a cut
at ``N`` means the block is empty and has no start). Blocks are by time: an episode decided
exactly at a start belongs to the later block. Block ends: fit ends at the calibration start,
calibration at the test start, test at ``evaluated_as_of`` (an absent start falls through to
the next end).

Purge: one full ``MAX_LABEL_HORIZON`` before each block start. An episode of the earlier
block decided in ``[start - MAX_LABEL_HORIZON, start)`` is purged (``BOUNDARY_WINDOW``), so
every member of a kept episode is decided before the next block starts: no episode crosses a
boundary. Independently, a kept episode must have ``label_available_at <= end of its own
block``; a later label is purged (``LABEL_AFTER_BLOCK_END``) and never used by that block.

Minima (§7)
-----------

On the kept (post-purge) episodes: at least ``MIN_MATURED_EPISODES`` in total,
``MIN_TEST_EPISODES`` in the test block and ``MIN_CALENDAR_DAYS`` calendar days (UTC dates of
the first and last kept seal, both counted). Any shortfall makes the result ``UNCALIBRATED``
with the typed list of failed minima; the split assignment is then withheld (``splits`` is
``None``) and no probability or score is produced by this module in any case.

Manifest
--------

Canonical JSON (sorted keys, ``(",", ":")`` separators, ASCII) of: policy version and
parameters, seed, evaluation time, benchmark and cohort keys, sha256 of the sorted input ids
(``subject_id|horizon``), counts per split, purged and excluded by reason, split boundaries,
sha256 of each split's episode ids, status and failed minima. Its sha256 is the content hash.
``TrialLedger.register`` seals it with the cohort's next attempt number; the sealed manifest
id is the sha256 of the sealed canonical JSON. Attempts per cohort never decrease, count
``UNCALIBRATED`` results, and re-registering the same content returns the existing manifest
without counting it again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

from radar_v08.domain.invocation import Direction
from radar_v08.domain.outcomes import (
    HORIZONS,
    Horizon,
    LinkedOutcome,
    MissingReason,
    utc_text,
)

EXPERIMENT_POLICY_VERSION = "EXPERIMENT-1"
MANIFEST_ID_PREFIX = "manifest:sha256:"
MAX_TEXT_LENGTH = 200

#: The longest outcome label horizon (24h): the purge width and the episode window.
MAX_LABEL_HORIZON: timedelta = max(horizon.duration for horizon in HORIZONS)

#: Chronological split in percent of eligible episodes: fit / calibration / test.
FIT_PERCENT = 60
CALIBRATION_PERCENT = 20
TEST_PERCENT = 20

MIN_MATURED_EPISODES = 1000
MIN_TEST_EPISODES = 200
MIN_CALENDAR_DAYS = 60

MAX_SEED = 2**64 - 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExperimentErrorCode(Enum):
    INVALID_FIELD = "invalid_field"
    DUPLICATE_OBSERVATION = "duplicate_observation"
    INCONSISTENT = "inconsistent"


class ExperimentInputError(ValueError):
    """An experiment input, manifest or ledger was refused; ``code`` says why."""

    def __init__(self, code: ExperimentErrorCode, field: str, detail: str) -> None:
        super().__init__(f"{code.value}: {field}: {detail}")
        self.code = code
        self.field = field
        self.detail = detail


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, field, "must be text")
    if not value or value != value.strip() or len(value) > MAX_TEXT_LENGTH:
        raise ExperimentInputError(
            ExperimentErrorCode.INVALID_FIELD, field, f"must be 1..{MAX_TEXT_LENGTH} chars without edge whitespace"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, field, "contains control characters")
    return value


def _require_enum[E: Enum](value: object, enum_type: type[E], field: str) -> E:
    if not isinstance(value, enum_type):
        raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, field, f"must be {enum_type.__name__}")
    return value


def require_aware(moment: object, field: str) -> datetime:
    if not isinstance(moment, datetime):
        raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, field, "must be a datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, field, "must be timezone-aware")
    return moment


def _require_count(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, field, f"must be an int >= {minimum}")
    return value


def _require_seed(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SEED:
        raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "seed", f"must be an int in 0..{MAX_SEED}")
    return value


def canonical_json(value: object) -> str:
    """Sorted keys, no whitespace, ASCII only: equal content gives equal bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Split(Enum):
    FIT = "fit"
    CALIBRATION = "calibration"
    TEST = "test"


SPLITS: tuple[Split, ...] = (Split.FIT, Split.CALIBRATION, Split.TEST)


class ExclusionReason(Enum):
    """Why an observation or an episode is not in any split. Counted, never a zero value."""

    # observation level: not this cohort, or not yet decided at the evaluation time
    SETUP_MISMATCH = "setup_mismatch"
    DIRECTION_MISMATCH = "direction_mismatch"
    HORIZON_MISMATCH = "horizon_mismatch"
    FEATURE_VERSION_MISMATCH = "feature_version_mismatch"
    OUTCOME_VERSION_MISMATCH = "outcome_version_mismatch"
    DECIDED_AFTER_AS_OF = "decided_after_as_of"
    # episode level: the seal's label cannot be used
    LABEL_NOT_MATURE = "label_not_mature"
    NET_GROSS_UNAVAILABLE = "net_gross_unavailable"
    NET_COST_NOT_RECORDED = "net_cost_not_recorded"
    NET_COST_INCOMPLETE = "net_cost_incomplete"
    COST_POLICY_MISMATCH = "cost_policy_mismatch"


OBSERVATION_REASONS: tuple[ExclusionReason, ...] = (
    ExclusionReason.SETUP_MISMATCH,
    ExclusionReason.DIRECTION_MISMATCH,
    ExclusionReason.HORIZON_MISMATCH,
    ExclusionReason.FEATURE_VERSION_MISMATCH,
    ExclusionReason.OUTCOME_VERSION_MISMATCH,
    ExclusionReason.DECIDED_AFTER_AS_OF,
)
EPISODE_REASONS: tuple[ExclusionReason, ...] = (
    ExclusionReason.LABEL_NOT_MATURE,
    ExclusionReason.NET_GROSS_UNAVAILABLE,
    ExclusionReason.NET_COST_NOT_RECORDED,
    ExclusionReason.NET_COST_INCOMPLETE,
    ExclusionReason.COST_POLICY_MISMATCH,
)

_NET_REASON = {
    MissingReason.GROSS_UNAVAILABLE: ExclusionReason.NET_GROSS_UNAVAILABLE,
    MissingReason.COST_NOT_RECORDED: ExclusionReason.NET_COST_NOT_RECORDED,
    MissingReason.COST_INCOMPLETE: ExclusionReason.NET_COST_INCOMPLETE,
}


class PurgeReason(Enum):
    BOUNDARY_WINDOW = "boundary_window"  # decided within one max label horizon before the next block
    LABEL_AFTER_BLOCK_END = "label_after_block_end"  # label known only after its own block ended


PURGE_REASONS: tuple[PurgeReason, ...] = (PurgeReason.BOUNDARY_WINDOW, PurgeReason.LABEL_AFTER_BLOCK_END)


class FailedMinimum(Enum):
    MATURED_EPISODES = "matured_episodes"
    TEST_EPISODES = "test_episodes"
    CALENDAR_DAYS = "calendar_days"


class EvaluationStatus(Enum):
    SAMPLE_SUFFICIENT = "SAMPLE_SUFFICIENT"  # minima met; the splits may be used (calibration)
    UNCALIBRATED = "UNCALIBRATED"  # a minimum failed; no split, no number


class BenchmarkTarget(Enum):
    NET_MARKOUT_POSITIVE = "net_markout>0"  # OC-1 §7 target event


# ---------------------------------------------------------------------------
# Keys and inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CohortKey:
    """Fixed cohort: one strategy setup, direction, horizon and feature/outcome versions."""

    setup: str
    direction: Direction
    horizon: Horizon
    feature_version: str
    outcome_policy_version: str

    def __post_init__(self) -> None:
        _require_text(self.setup, "CohortKey.setup")
        _require_enum(self.direction, Direction, "CohortKey.direction")
        if self.direction is Direction.NONE:
            raise ExperimentInputError(
                ExperimentErrorCode.INVALID_FIELD, "CohortKey.direction", "a cohort is LONG or SHORT, never NONE"
            )
        _require_enum(self.horizon, Horizon, "CohortKey.horizon")
        _require_text(self.feature_version, "CohortKey.feature_version")
        _require_text(self.outcome_policy_version, "CohortKey.outcome_policy_version")

    def as_json(self) -> dict[str, str]:
        return {
            "setup": self.setup,
            "direction": self.direction.value,
            "horizon": self.horizon.value,
            "feature_version": self.feature_version,
            "outcome_policy_version": self.outcome_policy_version,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkKey:
    """Fixed benchmark: its name, the target event and the declared cost scenario policy."""

    name: str
    target: BenchmarkTarget
    cost_policy_version: str

    def __post_init__(self) -> None:
        _require_text(self.name, "BenchmarkKey.name")
        _require_enum(self.target, BenchmarkTarget, "BenchmarkKey.target")
        _require_text(self.cost_policy_version, "BenchmarkKey.cost_policy_version")

    def as_json(self) -> dict[str, str]:
        return {"name": self.name, "target": self.target.value, "cost_policy_version": self.cost_policy_version}


@dataclass(frozen=True, slots=True)
class CohortObservation:
    """One labelled outcome with the setup and feature version it was decided under."""

    outcome: LinkedOutcome
    setup: str
    feature_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, LinkedOutcome):
            raise ExperimentInputError(
                ExperimentErrorCode.INVALID_FIELD, "CohortObservation.outcome", "must be a LinkedOutcome"
            )
        _require_text(self.setup, "CohortObservation.setup")
        _require_text(self.feature_version, "CohortObservation.feature_version")


@dataclass(frozen=True, slots=True)
class Episode:
    """Overlapping same-pair opportunities; the seal's label is the episode's outcome."""

    seal: LinkedOutcome
    member_ids: tuple[str, ...]
    last_decision_as_of: datetime
    net_markout: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.seal, LinkedOutcome):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "Episode.seal", "must be LinkedOutcome")
        if self.seal.label.net_markout != self.net_markout or not isinstance(self.net_markout, Decimal):
            raise ExperimentInputError(ExperimentErrorCode.INCONSISTENT, "Episode.net_markout", "must be the seal's net")
        if require_aware(self.last_decision_as_of, "Episode.last_decision_as_of") < self.decision_as_of:
            raise ExperimentInputError(
                ExperimentErrorCode.INCONSISTENT, "Episode.last_decision_as_of", "before the seal"
            )

    @property
    def episode_id(self) -> str:
        return self.seal.label.subject_id

    @property
    def decision_as_of(self) -> datetime:
        return self.seal.subject.decision_as_of

    @property
    def label_available_at(self) -> datetime:
        return self.seal.label.label_available_at


@dataclass(frozen=True, slots=True)
class SplitAssignment:
    """Kept episodes per block, chronological; block starts in UTC (``None`` = empty block)."""

    fit: tuple[Episode, ...]
    calibration: tuple[Episode, ...]
    test: tuple[Episode, ...]
    calibration_start: datetime | None
    test_start: datetime | None

    def episodes(self, split: Split) -> tuple[Episode, ...]:
        _require_enum(split, Split, "split")
        if split is Split.FIT:
            return self.fit
        if split is Split.CALIBRATION:
            return self.calibration
        return self.test


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _check_consistent_result(
    status: EvaluationStatus, failed: tuple[FailedMinimum, ...], splits: SplitAssignment | None
) -> None:
    if status is EvaluationStatus.UNCALIBRATED:
        if not failed or splits is not None:
            raise ExperimentInputError(
                ExperimentErrorCode.INCONSISTENT, "status", "UNCALIBRATED needs failed minima and no splits"
            )
    elif failed or splits is None:
        raise ExperimentInputError(
            ExperimentErrorCode.INCONSISTENT, "status", "SAMPLE_SUFFICIENT needs splits and no failed minimum"
        )


@dataclass(frozen=True, slots=True)
class ExperimentEvaluation:
    """Result of ``evaluate_cohort``: status, typed failed minima, splits and manifest content."""

    cohort: CohortKey
    benchmark: BenchmarkKey
    seed: int
    status: EvaluationStatus
    failed_minima: tuple[FailedMinimum, ...]
    splits: SplitAssignment | None
    content_json: str
    content_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.cohort, CohortKey) or not isinstance(self.benchmark, BenchmarkKey):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "keys", "must be CohortKey and BenchmarkKey")
        _require_seed(self.seed)
        _require_enum(self.status, EvaluationStatus, "status")
        if not isinstance(self.failed_minima, tuple) or any(
            not isinstance(item, FailedMinimum) for item in self.failed_minima
        ):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "failed_minima", "must be FailedMinimum")
        _check_consistent_result(self.status, self.failed_minima, self.splits)
        if not isinstance(self.content_json, str) or not isinstance(self.content_sha256, str):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "content", "must be text")
        if _sha256(self.content_json) != self.content_sha256:
            raise ExperimentInputError(ExperimentErrorCode.INCONSISTENT, "content_sha256", "not the content's hash")


def failed_minima(total_episodes: int, test_episodes: int, calendar_days: int) -> tuple[FailedMinimum, ...]:
    """The §7 minima not met, in fixed order. Every bound is inclusive: exactly the minimum passes."""
    total = _require_count(total_episodes, "total_episodes")
    test = _require_count(test_episodes, "test_episodes")
    days = _require_count(calendar_days, "calendar_days")
    failed: list[FailedMinimum] = []
    if total < MIN_MATURED_EPISODES:
        failed.append(FailedMinimum.MATURED_EPISODES)
    if test < MIN_TEST_EPISODES:
        failed.append(FailedMinimum.TEST_EPISODES)
    if days < MIN_CALENDAR_DAYS:
        failed.append(FailedMinimum.CALENDAR_DAYS)
    return tuple(failed)


def calendar_days(moments: Iterable[datetime]) -> int:
    """UTC calendar dates from the first to the last moment, both counted (0 when empty)."""
    dates = [require_aware(moment, "moment").astimezone(UTC).date() for moment in moments]
    if not dates:
        return 0
    return (max(dates) - min(dates)).days + 1


def _observation_key(observation: CohortObservation) -> str:
    return f"{observation.outcome.label.subject_id}|{observation.outcome.label.horizon.value}"


def _pair_key(outcome: LinkedOutcome) -> tuple[str, str, str, str]:
    subject = outcome.subject
    return (subject.instrument.venue, subject.instrument.kind.value, subject.instrument.symbol, subject.pair)


def _cohort_exclusion(
    observation: CohortObservation, cohort: CohortKey, evaluated_as_of: datetime
) -> ExclusionReason | None:
    subject, label = observation.outcome.subject, observation.outcome.label
    if observation.setup != cohort.setup:
        return ExclusionReason.SETUP_MISMATCH
    if subject.direction is not cohort.direction:
        return ExclusionReason.DIRECTION_MISMATCH
    if label.horizon is not cohort.horizon:
        return ExclusionReason.HORIZON_MISMATCH
    if observation.feature_version != cohort.feature_version:
        return ExclusionReason.FEATURE_VERSION_MISMATCH
    if label.policy_version != cohort.outcome_policy_version:
        return ExclusionReason.OUTCOME_VERSION_MISMATCH
    if subject.decision_as_of > evaluated_as_of:
        return ExclusionReason.DECIDED_AFTER_AS_OF
    return None


def _episode_exclusion(seal: LinkedOutcome, benchmark: BenchmarkKey, evaluated_as_of: datetime) -> ExclusionReason | None:
    label = seal.label
    if label.label_available_at > evaluated_as_of:
        return ExclusionReason.LABEL_NOT_MATURE
    if label.net_markout is None:
        if label.net_missing is None:  # OutcomeLabel guarantees one of the two; defensive
            raise ExperimentInputError(ExperimentErrorCode.INCONSISTENT, "label.net_missing", "net and reason absent")
        return _NET_REASON[label.net_missing]
    if label.cost_policy_version != benchmark.cost_policy_version:
        return ExclusionReason.COST_POLICY_MISMATCH
    return None


def _group_episodes(outcomes: list[LinkedOutcome]) -> list[tuple[LinkedOutcome, list[LinkedOutcome]]]:
    """Anchored grouping per pair: (seal, later members decided before seal + MAX_LABEL_HORIZON)."""
    by_pair: dict[tuple[str, str, str, str], list[LinkedOutcome]] = {}
    for outcome in outcomes:
        by_pair.setdefault(_pair_key(outcome), []).append(outcome)
    groups: list[tuple[LinkedOutcome, list[LinkedOutcome]]] = []
    for pair in sorted(by_pair):
        ordered = sorted(by_pair[pair], key=lambda item: (item.subject.decision_as_of, item.label.subject_id))
        seal: LinkedOutcome | None = None
        members: list[LinkedOutcome] = []
        for outcome in ordered:
            if seal is not None and outcome.subject.decision_as_of < seal.subject.decision_as_of + MAX_LABEL_HORIZON:
                members.append(outcome)
                continue
            if seal is not None:
                groups.append((seal, members))
            seal, members = outcome, []
        if seal is not None:
            groups.append((seal, members))
    return groups


def _ids_sha256(ids: Iterable[str]) -> str:
    return _sha256("\n".join(sorted(ids)))


def evaluate_cohort(
    observations: Iterable[CohortObservation],
    *,
    cohort: CohortKey,
    benchmark: BenchmarkKey,
    seed: int,
    evaluated_as_of: datetime,
) -> ExperimentEvaluation:
    """Episodes, purged 60/20/20 chronological split, minima and manifest content for one cohort."""
    if not isinstance(cohort, CohortKey):
        raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "cohort", "must be CohortKey")
    if not isinstance(benchmark, BenchmarkKey):
        raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "benchmark", "must be BenchmarkKey")
    seed = _require_seed(seed)
    as_of = require_aware(evaluated_as_of, "evaluated_as_of")

    items = list(observations)
    keys: set[str] = set()
    for observation in items:
        if not isinstance(observation, CohortObservation):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "observations", "must be CohortObservation")
        key = _observation_key(observation)
        if key in keys:
            raise ExperimentInputError(ExperimentErrorCode.DUPLICATE_OBSERVATION, "observations", key)
        keys.add(key)

    excluded_observations = {reason: 0 for reason in OBSERVATION_REASONS}
    candidates: list[LinkedOutcome] = []
    for observation in items:
        reason = _cohort_exclusion(observation, cohort, as_of)
        if reason is None:
            candidates.append(observation.outcome)
        else:
            excluded_observations[reason] += 1

    groups = _group_episodes(candidates)
    excluded_episodes = {reason: 0 for reason in EPISODE_REASONS}
    eligible: list[Episode] = []
    linked = 0
    for seal, members in groups:
        linked += len(members)
        reason = _episode_exclusion(seal, benchmark, as_of)
        if reason is not None:
            excluded_episodes[reason] += 1
            continue
        net = seal.label.net_markout
        assert net is not None  # _episode_exclusion refused a missing net
        eligible.append(
            Episode(
                seal=seal,
                member_ids=tuple(member.label.subject_id for member in members),
                last_decision_as_of=members[-1].subject.decision_as_of if members else seal.subject.decision_as_of,
                net_markout=net,
            )
        )
    eligible.sort(key=lambda episode: (episode.decision_as_of, episode.episode_id))

    count = len(eligible)
    fit_cut = count * FIT_PERCENT // 100
    test_cut = count * (FIT_PERCENT + CALIBRATION_PERCENT) // 100
    calibration_start = eligible[fit_cut].decision_as_of if fit_cut < count else None
    test_start = eligible[test_cut].decision_as_of if test_cut < count else None

    blocks: dict[Split, list[Episode]] = {split: [] for split in SPLITS}
    purged = {reason: 0 for reason in PURGE_REASONS}
    for episode in eligible:
        moment = episode.decision_as_of
        if test_start is not None and moment >= test_start:
            split, next_start, block_end = Split.TEST, None, as_of
        elif calibration_start is not None and moment >= calibration_start:
            split, next_start = Split.CALIBRATION, test_start
            block_end = as_of if test_start is None else test_start
        else:
            split, next_start = Split.FIT, calibration_start
            block_end = as_of if calibration_start is None else calibration_start
        if next_start is not None and moment >= next_start - MAX_LABEL_HORIZON:
            purged[PurgeReason.BOUNDARY_WINDOW] += 1
        elif episode.label_available_at > block_end:
            purged[PurgeReason.LABEL_AFTER_BLOCK_END] += 1
        else:
            blocks[split].append(episode)

    kept = [episode for split in SPLITS for episode in blocks[split]]
    days = calendar_days(episode.decision_as_of for episode in kept)
    failed = failed_minima(len(kept), len(blocks[Split.TEST]), days)
    status = EvaluationStatus.UNCALIBRATED if failed else EvaluationStatus.SAMPLE_SUFFICIENT
    splits = (
        None
        if failed
        else SplitAssignment(
            fit=tuple(blocks[Split.FIT]),
            calibration=tuple(blocks[Split.CALIBRATION]),
            test=tuple(blocks[Split.TEST]),
            calibration_start=calibration_start,
            test_start=test_start,
        )
    )

    content: dict[str, object] = {
        "policy_version": EXPERIMENT_POLICY_VERSION,
        "policy": {
            "split_percent": {
                Split.FIT.value: FIT_PERCENT,
                Split.CALIBRATION.value: CALIBRATION_PERCENT,
                Split.TEST.value: TEST_PERCENT,
            },
            "purge_minutes": MAX_LABEL_HORIZON // timedelta(minutes=1),
            "episode_window_minutes": MAX_LABEL_HORIZON // timedelta(minutes=1),
            "min_matured_episodes": MIN_MATURED_EPISODES,
            "min_test_episodes": MIN_TEST_EPISODES,
            "min_calendar_days": MIN_CALENDAR_DAYS,
        },
        "seed": seed,
        "evaluated_as_of": utc_text(as_of),
        "cohort": cohort.as_json(),
        "benchmark": benchmark.as_json(),
        "input": {"observations": len(items), "ids_sha256": _ids_sha256(keys)},
        "excluded_observations": {reason.value: n for reason, n in excluded_observations.items()},
        "episodes": {"formed": len(groups), "linked_observations": linked, "eligible": count},
        "excluded_episodes": {reason.value: n for reason, n in excluded_episodes.items()},
        "boundaries": {
            "calibration_start": None if calibration_start is None else utc_text(calibration_start),
            "test_start": None if test_start is None else utc_text(test_start),
        },
        "purged": {reason.value: n for reason, n in purged.items()},
        "splits": {split.value: len(blocks[split]) for split in SPLITS},
        "split_ids_sha256": {
            split.value: _ids_sha256(episode.episode_id for episode in blocks[split]) for split in SPLITS
        },
        "calendar_days": days,
        "status": status.value,
        "failed_minima": [item.value for item in failed],
    }
    text = canonical_json(content)
    return ExperimentEvaluation(
        cohort=cohort,
        benchmark=benchmark,
        seed=seed,
        status=status,
        failed_minima=failed,
        splits=splits,
        content_json=text,
        content_sha256=_sha256(text),
    )


# ---------------------------------------------------------------------------
# Sealed manifests and the trial ledger
# ---------------------------------------------------------------------------

_SEAL_FIELDS = ("attempt", "content_sha256")


@dataclass(frozen=True, slots=True)
class SealedManifest:
    """An evaluation's content sealed with its cohort attempt number; verifies itself."""

    cohort: CohortKey
    attempt: int
    content_sha256: str
    canonical_json: str
    manifest_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.cohort, CohortKey):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "cohort", "must be CohortKey")
        _require_count(self.attempt, "attempt", minimum=1)
        if not isinstance(self.canonical_json, str) or not isinstance(self.manifest_id, str):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "manifest", "must be text")
        if MANIFEST_ID_PREFIX + _sha256(self.canonical_json) != self.manifest_id:
            raise ExperimentInputError(ExperimentErrorCode.INCONSISTENT, "manifest_id", "not the manifest's hash")
        try:
            body = json.loads(self.canonical_json)
        except ValueError as error:
            raise ExperimentInputError(ExperimentErrorCode.INCONSISTENT, "canonical_json", str(error)) from error
        if not isinstance(body, dict) or canonical_json(body) != self.canonical_json:
            raise ExperimentInputError(ExperimentErrorCode.INCONSISTENT, "canonical_json", "not canonical")
        if body.get("attempt") != self.attempt or body.get("content_sha256") != self.content_sha256:
            raise ExperimentInputError(ExperimentErrorCode.INCONSISTENT, "canonical_json", "seal fields differ")
        content = {key: value for key, value in body.items() if key not in _SEAL_FIELDS}
        if _sha256(canonical_json(content)) != self.content_sha256:
            raise ExperimentInputError(ExperimentErrorCode.INCONSISTENT, "content_sha256", "not the content's hash")
        if content.get("cohort") != self.cohort.as_json():
            raise ExperimentInputError(ExperimentErrorCode.INCONSISTENT, "cohort", "manifest is for another cohort")

    def as_dict(self) -> dict[str, object]:
        """A fresh copy of the manifest body; changing it never changes the manifest."""
        body = json.loads(self.canonical_json)
        assert isinstance(body, dict)
        return body

    @classmethod
    def seal(cls, evaluation: ExperimentEvaluation, attempt: int) -> SealedManifest:
        if not isinstance(evaluation, ExperimentEvaluation):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "evaluation", "must be ExperimentEvaluation")
        body = json.loads(evaluation.content_json)
        assert isinstance(body, dict)
        body["attempt"] = _require_count(attempt, "attempt", minimum=1)
        body["content_sha256"] = evaluation.content_sha256
        text = canonical_json(body)
        return cls(evaluation.cohort, attempt, evaluation.content_sha256, text, MANIFEST_ID_PREFIX + _sha256(text))


def _cohort_text(cohort: CohortKey) -> str:
    return canonical_json(cohort.as_json())


@dataclass(frozen=True, slots=True)
class TrialLedger:
    """Append-only record of sealed manifests; attempts per cohort are 1, 2, 3, … in order.

    There is no removal: a new ledger is only ever the old one plus a manifest, so the
    attempt count of a cohort never decreases. Rebuilding one from stored manifests
    re-verifies every hash and the numbering.
    """

    manifests: tuple[SealedManifest, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.manifests, tuple):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "manifests", "must be a tuple")
        seen: set[str] = set()
        counts: dict[str, int] = {}
        for manifest in self.manifests:
            if not isinstance(manifest, SealedManifest):
                raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "manifests", "must be SealedManifest")
            if manifest.content_sha256 in seen:
                raise ExperimentInputError(ExperimentErrorCode.INCONSISTENT, "manifests", "same content sealed twice")
            seen.add(manifest.content_sha256)
            cohort = _cohort_text(manifest.cohort)
            expected = counts.get(cohort, 0) + 1
            if manifest.attempt != expected:
                raise ExperimentInputError(
                    ExperimentErrorCode.INCONSISTENT, "manifests", f"attempt {manifest.attempt}, expected {expected}"
                )
            counts[cohort] = expected

    def attempts(self, cohort: CohortKey) -> int:
        """Distinct evaluations registered for ``cohort``, UNCALIBRATED ones included."""
        if not isinstance(cohort, CohortKey):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "cohort", "must be CohortKey")
        text = _cohort_text(cohort)
        return sum(1 for manifest in self.manifests if _cohort_text(manifest.cohort) == text)

    def find(self, content_sha256: str) -> SealedManifest | None:
        for manifest in self.manifests:
            if manifest.content_sha256 == content_sha256:
                return manifest
        return None

    def register(self, evaluation: ExperimentEvaluation) -> tuple[TrialLedger, SealedManifest]:
        """Seal ``evaluation`` as the cohort's next attempt; the same content again is a no-op."""
        if not isinstance(evaluation, ExperimentEvaluation):
            raise ExperimentInputError(ExperimentErrorCode.INVALID_FIELD, "evaluation", "must be ExperimentEvaluation")
        existing = self.find(evaluation.content_sha256)
        if existing is not None:
            return self, existing
        sealed = SealedManifest.seal(evaluation, self.attempts(evaluation.cohort) + 1)
        return TrialLedger(self.manifests + (sealed,)), sealed

