"""T051 Screener benchmark: fixed lists, fixed sequence, selection rule and resumable state.

Pure policy (T051c; D59 b/f, D61; docs/forja/DECISIONS.md). No I/O, no clock, no network,
no environment: the runner (``scripts/run_t051_block.py``) reads the corpus and the results
file and hands this module plain values. Everything here is a function of its inputs, so the
same corpus and the same results always give the same lists, the same next step and the same
selection.

Fixed lists (D59 b), chosen by ``sha256(FREEZE_SALT | list | case_id | case_sha256)`` order,
never by content and never by a model:

* ``probe``: 20 LABELLED development cases (deterministic gold), spread over the labelled
  categories in their name order (7/7/6 for 3 categories).
* ``warmup`` (1), ``cold`` (10) and ``warm`` (20): development cases, disjoint slices of one
  ordering. The 40 development cases without gold may appear here: these phases measure only
  schema validity and latency (D59 f).
* ``holdout_labelled``: the 120 labelled holdout cases, in id order, once per candidate.
* ``stability``: 20 labelled holdout cases, spread like the probe, 3 repetitions.
* ``holdout_sealed``: the 80 holdout cases WITHOUT gold. They are listed (id and sha256 only)
  so that the runner can refuse them; no step ever names one (``SealedCaseError``).

Sequence (D61, OC-1 section 6), one step at a time (``next_step``):

1. ``probe``: 20 cases per probe model (``PROBE_MODELS`` that are installed), in that order.
2. Selection (``select_candidates``) once every probe is complete: the baseline
   ``qwen3:14b`` always, plus at most ONE other probe model that passes the profile limits
   (no OOM, VRAM/RAM reserves respected on every call, p95 <= 30 s), the best by highest
   first-pass schema validity, then lowest p95, then lowest peak model memory (an unmeasured
   memory ranks last), then name. None passes: only the baseline.
3. Per candidate, in selection order: ``cold`` (10 loads), ``warm`` (20), ``holdout`` (the
   120 labelled holdout cases once), ``stability`` (20 x 3, repetition-major).
4. ``OPTIONAL_MODEL`` only with the explicit flag and only once steps 1-3 are complete: its
   own probe, then, only if it passes the same limits, the same candidate steps.

A step's idempotent key is ``(block, model, digest, case_id, repetition)``: a key already in
the results is never run again. Warm-up calls (one after each (re)load before a probe, warm,
holdout or stability call) are recorded under block ``warmup`` with the next free repetition;
they are outside every metric and never count as progress.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

FREEZE_SCHEMA_VERSION = 1
FREEZE_SALT = "t051-freeze-1"
RESULTS_SCHEMA_VERSION = 1

BASELINE_MODEL = "qwen3:14b"
PROBE_MODELS: tuple[str, ...] = (BASELINE_MODEL, "gpt-oss:20b", "llama3.2:latest")
OPTIONAL_MODEL = "qwen3-coder:30b"
BENCHMARK_MODELS: tuple[str, ...] = (*PROBE_MODELS, OPTIONAL_MODEL)

PROBE_CASES = 20
WARMUP_CASES = 1
COLD_LOADS = 10
WARM_RUNS = 20
HOLDOUT_LABELLED_CASES = 120
HOLDOUT_SEALED_CASES = 80
STABILITY_CASES = 20
STABILITY_REPEATS = 3

# Screener profile limits used by the selection rule (OC-1 section 3, model_profiles.toml).
CALL_HARD_SECONDS = 30
P95_LIMIT_SECONDS = 30.0
RESERVE_VRAM_GIB = 1.5
RESERVE_RAM_GIB = 4.0
MAX_BUDGET_SECONDS = 540
# Time kept back so that the explicit unload and the summary always fit after the last call.
END_RESERVE_SECONDS = 15
# Worst case for the explicit unload before a cold load (request plus /api/ps polling).
UNLOAD_WAIT_SECONDS = 10

LABELLED = "deterministic"
UNLABELLED = "gold_unavailable"


class Block(Enum):
    PREFLIGHT = "preflight"
    PROBE = "probe"
    WARMUP = "warmup"
    COLD = "cold"
    WARM = "warm"
    HOLDOUT = "holdout"
    STABILITY = "stability"


# Blocks whose timing must not include a model load: a warm-up call goes first after a (re)load.
WARM_BLOCKS = frozenset({Block.PROBE, Block.WARM, Block.HOLDOUT, Block.STABILITY})
# Outcomes of the accepted harness that disqualify a model in the selection rule.
DISQUALIFYING_OUTCOMES = frozenset({"oom", "resource_reserve_breached", "resources_unreported"})


class SequenceError(ValueError):
    """The inputs cannot produce the fixed lists or the sequence. Nothing ran."""


class SealedCaseError(RuntimeError):
    """A holdout case without gold was about to be sent to a model (D59 f). Never allowed."""


class ResultsCorrupt(ValueError):
    """The results file holds a line that is neither a record nor a tolerated truncated tail."""


@dataclass(frozen=True, slots=True)
class CaseRef:
    """What the lists need to know about a case: identity, hash, category and gold status."""

    case_id: str
    case_sha256: str
    partition: str  # "development" | "holdout"
    category: str
    labelled: bool


@dataclass(frozen=True, slots=True)
class FixedLists:
    probe: tuple[str, ...]
    warmup: tuple[str, ...]
    cold: tuple[str, ...]
    warm: tuple[str, ...]
    holdout_labelled: tuple[str, ...]
    stability: tuple[str, ...]
    holdout_sealed: tuple[str, ...]


def _order_key(list_name: str, case: CaseRef) -> str:
    text = f"{FREEZE_SALT}|{list_name}|{case.case_id}|{case.case_sha256}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ordered(list_name: str, cases: Iterable[CaseRef]) -> list[CaseRef]:
    return sorted(cases, key=lambda case: (_order_key(list_name, case), case.case_id))


def _stratified(list_name: str, cases: Sequence[CaseRef], total: int) -> tuple[str, ...]:
    """``total`` labelled cases spread over their categories (name order gets the remainder)."""
    categories = sorted({case.category for case in cases})
    if not categories:
        raise SequenceError(f"{list_name}: no labelled case")
    base, extra = divmod(total, len(categories))
    chosen: list[str] = []
    for index, category in enumerate(categories):
        want = base + (1 if index < extra else 0)
        pool = _ordered(list_name, (case for case in cases if case.category == category))
        if len(pool) < want:
            raise SequenceError(f"{list_name}: category {category} has {len(pool)} labelled cases, needs {want}")
        chosen.extend(case.case_id for case in pool[:want])
    return tuple(sorted(chosen))


def build_fixed_lists(development: Sequence[CaseRef], holdout: Sequence[CaseRef]) -> FixedLists:
    """The D59 b lists for a corpus. Deterministic: same cases, same lists."""
    ids = [case.case_id for case in (*development, *holdout)]
    if len(set(ids)) != len(ids):
        raise SequenceError("duplicate case id")
    if any(case.partition != "development" for case in development):
        raise SequenceError("a development case is not in development")
    if any(case.partition != "holdout" for case in holdout):
        raise SequenceError("a holdout case is not in holdout")
    labelled_dev = [case for case in development if case.labelled]
    latency_order = _ordered("latency", development)
    needed = WARMUP_CASES + COLD_LOADS + WARM_RUNS
    if len(latency_order) < needed:
        raise SequenceError(f"development has {len(latency_order)} cases, needs {needed}")
    warmup = tuple(case.case_id for case in latency_order[:WARMUP_CASES])
    cold = tuple(case.case_id for case in latency_order[WARMUP_CASES : WARMUP_CASES + COLD_LOADS])
    warm = tuple(case.case_id for case in latency_order[WARMUP_CASES + COLD_LOADS : needed])
    labelled_holdout = sorted(case.case_id for case in holdout if case.labelled)
    sealed = sorted(case.case_id for case in holdout if not case.labelled)
    return FixedLists(
        probe=_stratified("probe", labelled_dev, PROBE_CASES),
        warmup=warmup,
        cold=cold,
        warm=warm,
        holdout_labelled=tuple(labelled_holdout),
        stability=_stratified("stability", [case for case in holdout if case.labelled], STABILITY_CASES),
        holdout_sealed=tuple(sealed),
    )


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def freeze_document(
    *,
    corpus_id: str,
    lock_sha256: str,
    hashes: Mapping[str, str],
    lists: FixedLists,
    cases: Mapping[str, CaseRef],
) -> dict[str, object]:
    """The D59 b freeze: hashes of what the model sees and of the code, plus the fixed lists."""

    def listed(names: Sequence[str]) -> list[dict[str, str]]:
        return [{"case_id": name, "case_sha256": cases[name].case_sha256} for name in names]

    return {
        "freeze_schema_version": FREEZE_SCHEMA_VERSION,
        "freeze_salt": FREEZE_SALT,
        "corpus_id": corpus_id,
        "corpus_lock_sha256": lock_sha256,
        "hashes": dict(sorted(hashes.items())),
        "models": {"baseline": BASELINE_MODEL, "probe": list(PROBE_MODELS), "optional": OPTIONAL_MODEL},
        "counts": {
            "probe": PROBE_CASES,
            "cold": COLD_LOADS,
            "warm": WARM_RUNS,
            "holdout_labelled": HOLDOUT_LABELLED_CASES,
            "stability": STABILITY_CASES,
            "stability_repeats": STABILITY_REPEATS,
            "holdout_sealed": HOLDOUT_SEALED_CASES,
        },
        "lists": {
            "probe_development_labelled": listed(lists.probe),
            "warmup_development": listed(lists.warmup),
            "cold_development": listed(lists.cold),
            "warm_development": listed(lists.warm),
            "holdout_labelled": listed(lists.holdout_labelled),
            "stability_holdout_labelled": listed(lists.stability),
            "holdout_sealed_never_sent": listed(lists.holdout_sealed),
        },
    }


def freeze_differences(expected: Mapping[str, object], found: object) -> tuple[str, ...]:
    """Top-level keys (and hash names) where the committed freeze differs from the recomputed one."""
    if not isinstance(found, Mapping):
        return ("<document>",)
    differences: list[str] = []
    for key in sorted(set(expected) | set(found)):
        if key == "hashes" and isinstance(expected.get(key), Mapping) and isinstance(found.get(key), Mapping):
            left = expected[key]
            right = found[key]
            assert isinstance(left, Mapping) and isinstance(right, Mapping)
            differences.extend(
                f"hashes.{name}" for name in sorted(set(left) | set(right)) if left.get(name) != right.get(name)
            )
        elif canonical_json(expected.get(key)) != canonical_json(found.get(key)):
            differences.append(key)
    return tuple(differences)


# -- steps and keys ------------------------------------------------------------------------------


type CallKey = tuple[str, str, str, str, int]


@dataclass(frozen=True, slots=True)
class Step:
    block: Block
    model: str
    case_id: str
    repetition: int

    def key(self, digest: str) -> CallKey:
        return (self.block.value, self.model, digest, self.case_id, self.repetition)


def check_not_sealed(case_id: str, lists: FixedLists) -> None:
    """Last guard before a call: a sealed holdout case is never sent, whatever asked for it."""
    if case_id in lists.holdout_sealed:
        raise SealedCaseError(f"sealed holdout case {case_id} must never be sent (D59 f)")


def _candidate_steps(model: str, lists: FixedLists) -> list[Step]:
    steps = [Step(Block.COLD, model, case_id, 1) for case_id in lists.cold]
    steps += [Step(Block.WARM, model, case_id, 1) for case_id in lists.warm]
    steps += [Step(Block.HOLDOUT, model, case_id, 1) for case_id in lists.holdout_labelled]
    steps += [
        Step(Block.STABILITY, model, case_id, repetition)
        for repetition in range(1, STABILITY_REPEATS + 1)
        for case_id in lists.stability
    ]
    return steps


def _probe_steps(model: str, lists: FixedLists) -> list[Step]:
    return [Step(Block.PROBE, model, case_id, 1) for case_id in lists.probe]


# -- probe statistics and the selection rule -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    """One probe call as recorded: harness outcome, first-pass validity, time and model memory."""

    case_id: str
    outcome: str
    schema_valid: bool
    wall_seconds: float | None
    model_memory_bytes: int | None


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear interpolation on the sorted values (fraction 0.95 is p95)."""
    if not values:
        raise SequenceError("percentile of nothing")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass(frozen=True, slots=True)
class ProbeStats:
    model: str
    calls: int
    schema_valid: int
    p95_seconds: float | None
    peak_model_memory_bytes: int | None
    disqualified: tuple[str, ...]

    @property
    def passes_limits(self) -> bool:
        return not self.disqualified


def probe_stats(model: str, observations: Sequence[ProbeObservation]) -> ProbeStats:
    reasons: list[str] = []
    outcomes = {item.outcome for item in observations}
    if "oom" in outcomes:
        reasons.append("oom")
    if outcomes & {"resource_reserve_breached", "resources_unreported"}:
        reasons.append("reserve_not_respected")
    times = [item.wall_seconds for item in observations]
    p95: float | None = None
    if not observations or any(value is None for value in times):
        reasons.append("latency_not_measured")
    else:
        p95 = percentile([value for value in times if value is not None], 0.95)
        if p95 > P95_LIMIT_SECONDS:
            reasons.append("p95_above_limit")
    memories = [item.model_memory_bytes for item in observations]
    peak = None if not memories or any(value is None for value in memories) else max(v for v in memories if v is not None)
    return ProbeStats(
        model=model,
        calls=len(observations),
        schema_valid=sum(1 for item in observations if item.schema_valid),
        p95_seconds=p95,
        peak_model_memory_bytes=peak,
        disqualified=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class Selection:
    candidates: tuple[str, ...]
    stats: tuple[ProbeStats, ...]
    rule: str = (
        "qwen3:14b always; at most one other probe model with no OOM, reserves respected on every "
        "call and p95 <= 30 s; highest first-pass schema validity, then lowest p95, then lowest "
        "peak model memory (unmeasured last), then name"
    )


def select_candidates(stats: Sequence[ProbeStats]) -> Selection:
    """The D61 rule, recomputable from the results file. The baseline is always first."""
    others = [item for item in stats if item.model != BASELINE_MODEL and item.passes_limits]

    def rank(item: ProbeStats) -> tuple[int, float, int, int, str]:
        p95 = item.p95_seconds if item.p95_seconds is not None else math.inf
        memory_missing = 1 if item.peak_model_memory_bytes is None else 0
        memory = item.peak_model_memory_bytes if item.peak_model_memory_bytes is not None else 0
        return (-item.schema_valid, p95, memory_missing, memory, item.model)

    best = sorted(others, key=rank)[:1]
    return Selection(candidates=(BASELINE_MODEL, *(item.model for item in best)), stats=tuple(stats))


# -- the sequence ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SequenceState:
    lists: FixedLists
    digests: Mapping[str, str]  # installed benchmark models -> digest recorded at preflight
    done: frozenset[CallKey]
    probe_observations: Mapping[str, tuple[ProbeObservation, ...]]
    include_optional: bool = False


@dataclass(frozen=True, slots=True)
class GroupProgress:
    block: str
    model: str
    done: int
    total: int


@dataclass(frozen=True, slots=True)
class Progress:
    next_step: Step | None
    selection: Selection | None
    groups: tuple[GroupProgress, ...]
    notes: tuple[str, ...] = field(default=())

    @property
    def complete(self) -> bool:
        return self.next_step is None


def _is_done(state: SequenceState, step: Step) -> bool:
    return step.key(state.digests[step.model]) in state.done


def _stats_for(state: SequenceState, model: str) -> ProbeStats:
    return probe_stats(model, state.probe_observations.get(model, ()))


def plan(state: SequenceState) -> Progress:
    """Every group of the fixed sequence with its progress, and the first step not yet done."""
    if BASELINE_MODEL not in state.digests:
        raise SequenceError(f"{BASELINE_MODEL} is not installed: the benchmark cannot run")
    groups: list[GroupProgress] = []
    notes: list[str] = []
    first: Step | None = None

    def visit(steps: Sequence[Step]) -> bool:
        nonlocal first
        if not steps:
            return True
        done = sum(1 for step in steps if _is_done(state, step))
        groups.append(GroupProgress(steps[0].block.value, steps[0].model, done, len(steps)))
        if first is None:
            first = next((step for step in steps if not _is_done(state, step)), None)
        return done == len(steps)

    probe_models = [model for model in PROBE_MODELS if model in state.digests]
    notes.extend(f"{model} not installed: not probed" for model in PROBE_MODELS if model not in state.digests)
    probes_complete = all([visit(_probe_steps(model, state.lists)) for model in probe_models])
    selection: Selection | None = None
    if probes_complete:
        selection = select_candidates([_stats_for(state, model) for model in probe_models])
        for model in selection.candidates:
            visit(_candidate_steps(model, state.lists))
    else:
        notes.append("selection pending: the probe of every installed probe model must complete first")
    mandatory_complete = first is None
    if state.include_optional:
        if OPTIONAL_MODEL not in state.digests:
            notes.append(f"{OPTIONAL_MODEL} not installed: optional step skipped")
        elif not mandatory_complete:
            notes.append(f"{OPTIONAL_MODEL} waits until every mandatory step is complete")
        elif visit(_probe_steps(OPTIONAL_MODEL, state.lists)):
            optional_stats = _stats_for(state, OPTIONAL_MODEL)
            if optional_stats.passes_limits:
                visit(_candidate_steps(OPTIONAL_MODEL, state.lists))
            else:
                notes.append(f"{OPTIONAL_MODEL} fails the profile limits: " + ", ".join(optional_stats.disqualified))
    return Progress(next_step=first, selection=selection, groups=tuple(groups), notes=tuple(notes))


def next_warmup_repetition(done: Iterable[CallKey], model: str, digest: str) -> int:
    used = [key[4] for key in done if key[0] == Block.WARMUP.value and key[1] == model and key[2] == digest]
    return max(used, default=0) + 1


def seconds_needed(step: Step, warmup: bool) -> int:
    """Wall time a step may take in the worst case, so that it is never started without it."""
    needed = CALL_HARD_SECONDS + END_RESERVE_SECONDS
    if step.block is Block.COLD:
        needed += UNLOAD_WAIT_SECONDS
    if warmup:
        needed += CALL_HARD_SECONDS
    return needed


def validate_budget(seconds: int) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= MAX_BUDGET_SECONDS:
        raise SequenceError(f"budget must be an integer number of seconds in 1..{MAX_BUDGET_SECONDS}")
    return seconds


# -- the results file ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedResults:
    records: tuple[Mapping[str, object], ...]
    truncated_line: int | None  # 1-based number of a truncated last line still unmarked
    marked_truncated_lines: tuple[int, ...]


def parse_results(text: str) -> ParsedResults:
    """Parse the append-only JSONL. A truncated LAST line is tolerated and reported.

    A line that is not a JSON object is accepted only (a) as the last line of the file (a
    write cut short), or (b) when the very next line is a ``truncated_tail`` marker naming
    it (the runner appends that marker before writing after such a line). Anything else is
    ``ResultsCorrupt``: the runner stops rather than guess.
    """
    lines = text.split("\n") if text else []
    if text.endswith("\n"):
        lines = lines[:-1]
    records: list[Mapping[str, object]] = []
    bad: list[int] = []
    for number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except ValueError:
            bad.append(number)
            continue
        if not isinstance(value, dict):
            bad.append(number)
            continue
        records.append(value)
    marked = {
        record.get("line")
        for record in records
        if record.get("record_type") == "truncated_tail"
    }
    truncated: int | None = None
    for number in bad:
        if number in marked:
            continue
        if number == len(lines):
            truncated = number
            continue
        raise ResultsCorrupt(f"line {number} of the results file is not a JSON object and is not a marked tail")
    return ParsedResults(
        records=tuple(records),
        truncated_line=truncated,
        marked_truncated_lines=tuple(sorted(number for number in bad if number in marked)),
    )


def call_key(record: Mapping[str, object]) -> CallKey:
    key = record.get("key")
    if not isinstance(key, Mapping):
        raise ResultsCorrupt("a call record has no key")
    block, model, digest, case_id, repetition = (
        key.get("block"), key.get("model"), key.get("digest"), key.get("case_id"), key.get("repetition")
    )
    if not all(isinstance(item, str) for item in (block, model, digest, case_id)) or type(repetition) is not int:
        raise ResultsCorrupt("a call record has a malformed key")
    assert isinstance(block, str) and isinstance(model, str) and isinstance(digest, str) and isinstance(case_id, str)
    return (block, model, digest, case_id, repetition)


def done_keys(records: Iterable[Mapping[str, object]]) -> frozenset[CallKey]:
    """Keys of every call record; a key seen twice means the file was not written by this runner."""
    seen: set[CallKey] = set()
    for record in records:
        if record.get("record_type") != "call":
            continue
        key = call_key(record)
        if key in seen:
            raise ResultsCorrupt(f"duplicate key {key}")
        seen.add(key)
    return frozenset(seen)


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def probe_observations(
    records: Iterable[Mapping[str, object]], digests: Mapping[str, str]
) -> dict[str, tuple[ProbeObservation, ...]]:
    """Probe observations per model, only for the digest recorded at preflight."""
    found: dict[str, list[ProbeObservation]] = {}
    for record in records:
        if record.get("record_type") != "call":
            continue
        block, model, digest, case_id, _ = call_key(record)
        if block != Block.PROBE.value or digests.get(model) != digest:
            continue
        harness = record.get("harness")
        timing = record.get("timing")
        resources = record.get("resources")
        harness = harness if isinstance(harness, Mapping) else {}
        timing = timing if isinstance(timing, Mapping) else {}
        resources = resources if isinstance(resources, Mapping) else {}
        outcome = harness.get("outcome")
        found.setdefault(model, []).append(
            ProbeObservation(
                case_id=case_id,
                outcome=outcome if isinstance(outcome, str) else "unknown",
                schema_valid=harness.get("first_pass_schema_valid") is True,
                wall_seconds=_optional_float(timing.get("wall_seconds")),
                model_memory_bytes=_optional_int(resources.get("ollama_ps_size_bytes")),
            )
        )
    return {model: tuple(sorted(items, key=lambda item: item.case_id)) for model, items in found.items()}
