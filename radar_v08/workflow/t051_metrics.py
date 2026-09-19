"""T051e: metrics of the T051 Screener runs, computed from the append-only results records.

Pure (D62: the report measures, it never decides). No I/O, no clock, no network, no
environment: the caller (``scripts/summarize_t051.py``) reads the results file and hands this
module its text. Everything here is a function of that text, so the same file always gives the
same numbers.

Honesty rules, all enforced here and covered by ``tests/test_t051_metrics.py``:

* **Not measured is never zero.** A proportion with no denominator, a percentile with no
  value, a peak with no reading: each is ``None`` and is rendered "not measured". A resource
  reading that is missing on some calls is reported with how many calls it covers.
* **Invalid answers and timeouts stay in every denominator.** Rates are over every recorded
  call of the group, whatever its outcome; nothing is filtered out after the fact.
* **Warm-up calls are outside every measured metric.** They are counted apart, per model.
* **Gold only in the three deterministic categories** (invalid/stale, insufficient evidence,
  no edge after costs) and only on records that carry a boolean ``abstain_expected``. The
  two categories without gold (admissible positive, conflicting evidence) never enter a gold
  denominator, even if a record claims a label for them.
* **Wilson score interval, 95 %,** ``z = 1.959963984540054``, closed form, clamped to [0, 1].
* **Percentiles by linear interpolation** on the sorted values (fraction 0.95 is p95).
* **No case or answer text leaves this module.** Reply bodies are read only to hash their
  ``message.content`` for the 20 x 3 stability comparison; the text is dropped at once.

The OC-1 section 6 gates are evaluated only as "would pass / would not pass / not measured on
the measured metrics"; nothing here promotes, selects or recommends a model.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

Z95 = 1.959963984540054

DETERMINISTIC_CATEGORIES: tuple[str, ...] = ("invalid_or_stale", "insufficient_evidence", "admissible_no_edge")
UNLABELLED_CATEGORIES: tuple[str, ...] = ("admissible_positive", "conflicting_evidence")
ALL_CATEGORIES: tuple[str, ...] = (*DETERMINISTIC_CATEGORIES, *UNLABELLED_CATEGORIES)
REJECTION_CATEGORY = "invalid_or_stale"  # gold: reject the input (abstain expected)
ABSTENTION_CATEGORY = "insufficient_evidence"  # gold: abstain
NO_EDGE_CATEGORY = "admissible_no_edge"  # gold: answer (no abstention expected)
NOT_ACCEPTED = "not_accepted"  # confusion-matrix column for calls with no accepted answer

WARMUP_BLOCK = "warmup"
MEASURED_BLOCKS: tuple[str, ...] = ("probe", "cold", "warm", "holdout", "stability")
STABILITY_BLOCK = "stability"
STABILITY_REPEATS = 3

ACCEPTED = "accepted"
TIMEOUT = "timeout"
OOM = "oom"
RISK_REJECTED = "risk_authority_rejected"
CITATION_OUT_OF_SCOPE = "citation_out_of_scope"
RESERVE_BREACHED = "resource_reserve_breached"
RESOURCES_UNREPORTED = "resources_unreported"
# Harness outcomes decided before the output-schema gate runs: for these the first-pass schema
# validity of the reply was never evaluated (the harness records it as false).
BEFORE_SCHEMA_GATE = frozenset(
    {
        "role_mismatch", "context_unfit", RESOURCES_UNREPORTED, OOM, RESERVE_BREACHED, TIMEOUT,
        "inference_failed", "output_cap_exceeded", "response_budget_exceeded", RISK_REJECTED,
    }
)

RESOURCE_GATE_OUTCOMES = frozenset({RESOURCES_UNREPORTED, OOM, RESERVE_BREACHED})

# OC-1 section 3 (Screener) and section 6 thresholds.
RESERVE_VRAM_GIB = 1.5
RESERVE_RAM_GIB = 4.0
HARD_CALL_SECONDS = 30.0
WARM_P95_TARGET_SECONDS = 20.0
MIN_SCHEMA_PERCENT = 99.0
MIN_RECALL_PERCENT = 95.0
MAX_FALSE_ABSTENTION_PERCENT = 15.0
MIN_GOLD_AGREEMENT_PERCENT = 85.0

WOULD_PASS = "would_pass"
WOULD_NOT_PASS = "would_not_pass"
NOT_MEASURED = "not_measured"
NOT_APPLICABLE = "not_applicable"

_NANOSECONDS = 1_000_000_000


class MetricsInputError(ValueError):
    """The results text cannot be read as the runner's records. Nothing is guessed."""


# -- statistics ------------------------------------------------------------------------------


def wilson_interval(successes: int, total: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for ``successes`` out of ``total``; ``None`` when ``total`` is 0."""
    for value in (successes, total):
        if isinstance(value, bool) or not isinstance(value, int):
            raise MetricsInputError("wilson_interval needs integer counts")
    if total < 0 or successes < 0 or successes > total:
        raise MetricsInputError("wilson_interval needs 0 <= successes <= total")
    if total == 0:
        return None
    p = successes / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = (p + z2 / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / denominator
    # Exactly 0 (1) when there is no success (failure): the closed form is, float rounding is not.
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == total else min(1.0, centre + half)
    return (low, high)


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Linear interpolation on the sorted values; ``None`` (not measured) when there are none."""
    if not 0.0 <= fraction <= 1.0:
        raise MetricsInputError("percentile fraction must be within [0, 1]")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass(frozen=True, slots=True)
class Rate:
    """``successes`` out of ``total``; no total means not measured, never zero."""

    successes: int
    total: int

    @property
    def measured(self) -> bool:
        return self.total > 0

    @property
    def rate(self) -> float | None:
        return self.successes / self.total if self.total else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson_interval(self.successes, self.total)


@dataclass(frozen=True, slots=True)
class Spread:
    """A distribution over the calls that measured it (``count`` of ``total`` calls)."""

    count: int
    total: int
    p50: float | None
    p95: float | None
    minimum: float | None
    maximum: float | None


def spread(values: Sequence[float | None], total: int) -> Spread:
    present = [value for value in values if value is not None]
    return Spread(
        count=len(present),
        total=total,
        p50=percentile(present, 0.5),
        p95=percentile(present, 0.95),
        minimum=min(present) if present else None,
        maximum=max(present) if present else None,
    )


@dataclass(frozen=True, slots=True)
class Extreme:
    """A peak (or a minimum) over the calls that measured it; ``None`` when no call did."""

    value: float | None
    measured: int
    total: int


def _extreme(values: Sequence[float | None], total: int, *, highest: bool) -> Extreme:
    present = [value for value in values if value is not None]
    if not present:
        return Extreme(None, 0, total)
    return Extreme(max(present) if highest else min(present), len(present), total)


# -- reading the records ---------------------------------------------------------------------


def _refuse_constant(name: str) -> object:
    raise ValueError(f"non-finite number {name}")


@dataclass(frozen=True, slots=True)
class ParsedLog:
    records: tuple[Mapping[str, object], ...]
    lines: int
    truncated_last_line: int | None  # an unmarked truncated last line (tolerated, reported)
    marked_truncated_lines: tuple[int, ...]


def parse_log(text: str) -> ParsedLog:
    """The runner's JSONL. A truncated last line, or one the runner marked, is tolerated.

    Any other line that is not a JSON object (NaN and Infinity included) is refused.
    """
    if not isinstance(text, str):
        raise MetricsInputError("results text must be a string")
    lines = text.split("\n") if text else []
    if text.endswith("\n"):
        lines = lines[:-1]
    records: list[Mapping[str, object]] = []
    bad: list[int] = []
    for number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line, parse_constant=_refuse_constant)
        except (ValueError, RecursionError):
            bad.append(number)
            continue
        if not isinstance(value, dict):
            bad.append(number)
            continue
        records.append(value)
    marked = {record.get("line") for record in records if record.get("record_type") == "truncated_tail"}
    truncated: int | None = None
    for number in bad:
        if number in marked:
            continue
        if number == len(lines):
            truncated = number
            continue
        raise MetricsInputError(f"line {number} is not a JSON object and is not a marked truncated tail")
    return ParsedLog(
        records=tuple(records),
        lines=len(lines),
        truncated_last_line=truncated,
        marked_truncated_lines=tuple(sorted(number for number in bad if number in marked)),
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class Call:
    """One call record, reduced to what the metrics use. Never holds case or answer text."""

    block: str
    model: str
    digest: str
    case_id: str
    repetition: int
    invocation_id: str | None
    partition: str | None
    category: str | None
    labelled: bool | None
    outcome: str
    first_pass_schema_valid: bool
    abstain_expected: bool | None
    abstained: bool | None
    classification: str | None
    rationale_unverified: bool
    min_free_vram_gib: float | None
    min_free_ram_gib: float | None
    wall_seconds: float | None
    load_seconds: float | None
    prompt_tokens_per_second: float | None
    generation_tokens_per_second: float | None
    gpu_used_mib: float | None
    ps_size_bytes: float | None
    ps_size_vram_bytes: float | None
    process_ram_bytes: float | None
    free_vram_gib: float | None
    free_ram_gib: float | None
    oom: bool
    answer_sha256: str | None  # sha256 of the reply's message content; the text itself is never kept
    settings: str | None  # canonical JSON of the request settings (no prompt, only its hash)

    @property
    def offload_fraction(self) -> float | None:
        size, vram = self.ps_size_bytes, self.ps_size_vram_bytes
        if size is None or vram is None or size <= 0 or vram > size:
            return None
        return (size - vram) / size


def _rate_per_second(count: object, duration_ns: object) -> float | None:
    tokens = _count(count)
    nanoseconds = _count(duration_ns)
    if tokens is None or nanoseconds is None or nanoseconds == 0:
        return None
    return tokens / (nanoseconds / _NANOSECONDS)


_SETTINGS_EXCLUDED = frozenset({"messages_sha256", "model"})
MAX_BODY_CHARS = 65_536  # the runner never records more than this of a reply body


def answer_sha256(response: Mapping[str, object]) -> str | None:
    """sha256 of ``message.content`` of a recorded, complete reply body; ``None`` otherwise.

    The envelope around the content carries timings, so hashing the whole body would never
    repeat; only the content is compared. The content is hashed and dropped, never returned.
    """
    body = response.get("body")
    if response.get("body_truncated") is not False or not isinstance(body, str) or len(body) > MAX_BODY_CHARS:
        return None
    try:
        envelope = json.loads(body, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        return None
    message = envelope.get("message") if isinstance(envelope, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return None
    return hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).hexdigest()


def _call(record: Mapping[str, object]) -> Call:
    key = record.get("key")
    if not isinstance(key, Mapping):
        raise MetricsInputError("a call record has no key")
    block, model, digest, case_id, repetition = (
        key.get("block"), key.get("model"), key.get("digest"), key.get("case_id"), key.get("repetition")
    )
    if not all(isinstance(item, str) and item for item in (block, model, digest, case_id)) or type(repetition) is not int:
        raise MetricsInputError("a call record has a malformed key")
    assert isinstance(block, str) and isinstance(model, str) and isinstance(digest, str) and isinstance(case_id, str)
    harness = _mapping(record.get("harness"))
    timing = _mapping(record.get("timing"))
    resources = _mapping(record.get("resources"))
    response = _mapping(record.get("response"))
    request = record.get("request")
    settings: str | None = None
    if isinstance(request, Mapping):
        kept = {str(name): value for name, value in request.items() if name not in _SETTINGS_EXCLUDED}
        settings = json.dumps(kept, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    outcome = _text(harness.get("outcome")) or "unknown"
    expected = harness.get("abstain_expected")
    abstained = harness.get("abstained")
    labelled = record.get("labelled")
    load_ns = _count(timing.get("load_duration"))
    return Call(
        block=block,
        model=model,
        digest=digest,
        case_id=case_id,
        repetition=repetition,
        invocation_id=_text(record.get("invocation_id")),
        partition=_text(record.get("partition")),
        category=_text(record.get("category")),
        labelled=labelled if isinstance(labelled, bool) else None,
        outcome=outcome,
        first_pass_schema_valid=harness.get("first_pass_schema_valid") is True,
        abstain_expected=expected if isinstance(expected, bool) else None,
        abstained=abstained if isinstance(abstained, bool) else None,
        classification=_text(harness.get("classification")),
        rationale_unverified=harness.get("rationale_unverified") is True,
        min_free_vram_gib=_number(harness.get("min_free_vram_gib")),
        min_free_ram_gib=_number(harness.get("min_free_ram_gib")),
        wall_seconds=_number(timing.get("wall_seconds")),
        load_seconds=None if load_ns is None else load_ns / _NANOSECONDS,
        prompt_tokens_per_second=_rate_per_second(timing.get("prompt_eval_count"), timing.get("prompt_eval_duration")),
        generation_tokens_per_second=_rate_per_second(timing.get("eval_count"), timing.get("eval_duration")),
        gpu_used_mib=_number(resources.get("gpu_used_mib")),
        ps_size_bytes=_number(resources.get("ollama_ps_size_bytes")),
        ps_size_vram_bytes=_number(resources.get("ollama_ps_size_vram_bytes")),
        process_ram_bytes=_number(resources.get("ollama_process_ram_bytes")),
        free_vram_gib=_number(resources.get("free_vram_gib")),
        free_ram_gib=_number(resources.get("free_ram_gib")),
        oom=outcome == OOM or resources.get("oom") is True,
        answer_sha256=answer_sha256(response),
        settings=settings,
    )


# -- per-group metrics -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroupMetrics:
    """OC-1 section 6 metrics of one (model, block) group of measured calls (no warm-up)."""

    model: str
    block: str
    calls: int
    outcomes: Mapping[str, int]
    schema_first_pass: Rate
    schema_not_evaluated: int  # stopped at an earlier gate: validity of the reply never evaluated
    citation_out_of_scope: Rate
    risk_rejected: Rate
    timeouts: Rate
    ooms: Rate
    reserve_breached: Rate
    reserve_breached_ram: int
    reserve_breached_vram: int
    resources_unreported: Rate
    accepted: Rate
    accepted_rationale_unverified: int
    # Secondary view, NOT the OC-1 denominator: calls the resource gate let through (it runs
    # before the reply is read, and it depends on the whole machine's free memory).
    accepted_past_resource_gate: Rate
    risk_rejected_past_resource_gate: Rate
    wall_seconds: Spread
    load_seconds: Spread
    prompt_tokens_per_second: Spread
    generation_tokens_per_second: Spread
    peak_gpu_used_mib: Extreme
    peak_ps_size_bytes: Extreme
    peak_ps_size_vram_bytes: Extreme
    peak_process_ram_bytes: Extreme
    peak_offload_fraction: Extreme
    min_free_vram_gib: Extreme
    min_free_ram_gib: Extreme


def group_metrics(model: str, block: str, calls: Sequence[Call]) -> GroupMetrics:
    total = len(calls)
    outcomes: dict[str, int] = {}
    for item in calls:
        outcomes[item.outcome] = outcomes.get(item.outcome, 0) + 1
    breached = [item for item in calls if item.outcome == RESERVE_BREACHED]
    past_gate = [item for item in calls if item.outcome not in RESOURCE_GATE_OUTCOMES]

    def count(outcome: str) -> Rate:
        return Rate(outcomes.get(outcome, 0), total)

    return GroupMetrics(
        model=model,
        block=block,
        calls=total,
        outcomes=dict(sorted(outcomes.items())),
        schema_first_pass=Rate(sum(1 for item in calls if item.first_pass_schema_valid), total),
        schema_not_evaluated=sum(1 for item in calls if item.outcome in BEFORE_SCHEMA_GATE),
        citation_out_of_scope=count(CITATION_OUT_OF_SCOPE),
        risk_rejected=count(RISK_REJECTED),
        timeouts=count(TIMEOUT),
        ooms=Rate(sum(1 for item in calls if item.oom), total),
        reserve_breached=count(RESERVE_BREACHED),
        reserve_breached_ram=sum(
            1 for item in breached if item.min_free_ram_gib is not None and item.min_free_ram_gib < RESERVE_RAM_GIB
        ),
        reserve_breached_vram=sum(
            1 for item in breached if item.min_free_vram_gib is not None and item.min_free_vram_gib < RESERVE_VRAM_GIB
        ),
        resources_unreported=count(RESOURCES_UNREPORTED),
        accepted=count(ACCEPTED),
        accepted_rationale_unverified=sum(1 for item in calls if item.outcome == ACCEPTED and item.rationale_unverified),
        accepted_past_resource_gate=Rate(sum(1 for item in past_gate if item.outcome == ACCEPTED), len(past_gate)),
        risk_rejected_past_resource_gate=Rate(sum(1 for item in past_gate if item.outcome == RISK_REJECTED), len(past_gate)),
        wall_seconds=spread([item.wall_seconds for item in calls], total),
        load_seconds=spread([item.load_seconds for item in calls], total),
        prompt_tokens_per_second=spread([item.prompt_tokens_per_second for item in calls], total),
        generation_tokens_per_second=spread([item.generation_tokens_per_second for item in calls], total),
        peak_gpu_used_mib=_extreme([item.gpu_used_mib for item in calls], total, highest=True),
        peak_ps_size_bytes=_extreme([item.ps_size_bytes for item in calls], total, highest=True),
        peak_ps_size_vram_bytes=_extreme([item.ps_size_vram_bytes for item in calls], total, highest=True),
        peak_process_ram_bytes=_extreme([item.process_ram_bytes for item in calls], total, highest=True),
        peak_offload_fraction=_extreme([item.offload_fraction for item in calls], total, highest=True),
        min_free_vram_gib=_extreme([item.free_vram_gib for item in calls], total, highest=False),
        min_free_ram_gib=_extreme([item.free_ram_gib for item in calls], total, highest=False),
    )


@dataclass(frozen=True, slots=True)
class GoldMetrics:
    """Gold on the three deterministic categories only (D60)."""

    model: str
    block: str
    gold_calls: int
    rejection_recall: Rate  # invalid/stale: accepted abstention over every such call
    abstention_recall: Rate  # insufficient evidence: accepted abstention over every such call
    combined_recall: Rate  # both categories together (the harness's abstention recall)
    false_abstention: Rate  # no edge: abstentions over every such call
    agreement: Rate  # accepted answer whose classification is the case category
    confusion: Mapping[str, Mapping[str, int]]  # gold category -> predicted column -> count
    excluded_without_gold: int  # calls of this group left out of every gold denominator


def _has_gold(item: Call) -> bool:
    if item.labelled is not True or item.category not in DETERMINISTIC_CATEGORIES or item.abstain_expected is None:
        return False
    # The label must be the one its category fixes by construction (D60); anything else is not gold.
    return item.abstain_expected is (item.category != NO_EDGE_CATEGORY)


def gold_metrics(model: str, block: str, calls: Sequence[Call]) -> GoldMetrics:
    gold = [item for item in calls if _has_gold(item)]

    def correct_abstention(items: Iterable[Call]) -> Rate:
        chosen = list(items)
        hits = sum(1 for item in chosen if item.outcome == ACCEPTED and item.abstained is True)
        return Rate(hits, len(chosen))

    columns = (*ALL_CATEGORIES, NOT_ACCEPTED)
    confusion: dict[str, dict[str, int]] = {category: dict.fromkeys(columns, 0) for category in DETERMINISTIC_CATEGORIES}
    agreed = 0
    for item in gold:
        accepted = item.outcome == ACCEPTED and item.classification in ALL_CATEGORIES
        column = item.classification if accepted and item.classification is not None else NOT_ACCEPTED
        assert item.category is not None
        confusion[item.category][column] += 1
        if accepted and item.classification == item.category:
            agreed += 1
    no_edge = [item for item in gold if item.category == NO_EDGE_CATEGORY]
    return GoldMetrics(
        model=model,
        block=block,
        gold_calls=len(gold),
        rejection_recall=correct_abstention(item for item in gold if item.category == REJECTION_CATEGORY),
        abstention_recall=correct_abstention(item for item in gold if item.category == ABSTENTION_CATEGORY),
        combined_recall=correct_abstention(item for item in gold if item.category != NO_EDGE_CATEGORY),
        false_abstention=Rate(sum(1 for item in no_edge if item.abstained is True), len(no_edge)),
        agreement=Rate(agreed, len(gold)),
        confusion=confusion,
        excluded_without_gold=len(calls) - len(gold),
    )


@dataclass(frozen=True, slots=True)
class StabilityMetrics:
    """The 20 fixed holdout cases x 3 repetitions: same decision, same answer content."""

    model: str
    cases: int
    complete_cases: int  # cases with every repetition 1..3 recorded
    identical_decision: Rate  # (outcome, abstained, classification) equal in all 3
    identical_answer: Rate  # sha256 of the reply content equal in all 3 (cases with 3 readable replies)


def stability_metrics(model: str, calls: Sequence[Call]) -> StabilityMetrics:
    by_case: dict[str, dict[int, Call]] = {}
    for item in calls:
        by_case.setdefault(item.case_id, {})[item.repetition] = item
    complete = [runs for runs in by_case.values() if set(runs) >= set(range(1, STABILITY_REPEATS + 1))]
    same_decision = 0
    hashed = 0
    same_reply = 0
    for runs in complete:
        chosen = [runs[repetition] for repetition in range(1, STABILITY_REPEATS + 1)]
        if len({(item.outcome, item.abstained, item.classification) for item in chosen}) == 1:
            same_decision += 1
        hashes = [item.answer_sha256 for item in chosen]
        if all(value is not None for value in hashes):
            hashed += 1
            if len(set(hashes)) == 1:
                same_reply += 1
    return StabilityMetrics(
        model=model,
        cases=len(by_case),
        complete_cases=len(complete),
        identical_decision=Rate(same_decision, len(complete)),
        identical_answer=Rate(same_reply, hashed),
    )


@dataclass(frozen=True, slots=True)
class HoldoutCoverage:
    model: str
    labelled_distinct: int  # distinct labelled holdout cases in the holdout block
    labelled_calls: int
    sealed_sent: int  # holdout calls on a case without gold, any block: must be 0 (D59 f)


def holdout_coverage(model: str, calls: Sequence[Call]) -> HoldoutCoverage:
    holdout_block = [item for item in calls if item.block == "holdout" and item.labelled is True]
    return HoldoutCoverage(
        model=model,
        labelled_distinct=len({item.case_id for item in holdout_block}),
        labelled_calls=len(holdout_block),
        sealed_sent=sum(1 for item in calls if item.partition == "holdout" and item.labelled is not True),
    )


# -- gates, only as "would pass / would not pass / not measured" -----------------------------


@dataclass(frozen=True, slots=True)
class GateVerdict:
    gate: int
    state: str  # WOULD_PASS | WOULD_NOT_PASS | NOT_MEASURED | NOT_APPLICABLE
    reasons: tuple[str, ...]


def _percent(rate: Rate) -> float | None:
    value = rate.rate
    return None if value is None else value * 100.0


def _merge(groups: Sequence[GroupMetrics]) -> tuple[int, int, int, int, int, int]:
    """(calls, ooms, timeouts, reserve breaches, resources unreported, accepted) over measured groups."""
    return (
        sum(group.calls for group in groups),
        sum(group.ooms.successes for group in groups),
        sum(group.timeouts.successes for group in groups),
        sum(group.reserve_breached.successes for group in groups),
        sum(group.resources_unreported.successes for group in groups),
        sum(group.accepted.successes for group in groups),
    )


def gate_verdicts(
    groups: Sequence[GroupMetrics], holdout_gold: GoldMetrics | None
) -> tuple[GateVerdict, GateVerdict, GateVerdict, GateVerdict]:
    """Gates 1-4 of OC-1 section 6 for ONE model, on its measured groups. Never a promotion."""
    calls, ooms, timeouts, breaches, unreported, accepted = _merge(groups)
    breaches_ram = sum(group.reserve_breached_ram for group in groups)
    breaches_vram = sum(group.reserve_breached_vram for group in groups)
    by_block = {group.block: group for group in groups}
    unverified = sum(group.accepted_rationale_unverified for group in groups)

    if calls == 0:
        gate1 = GateVerdict(1, NOT_MEASURED, ("no measured call",))
    elif ooms:
        gate1 = GateVerdict(1, WOULD_NOT_PASS, (f"{ooms} OOM",))
    elif not accepted:
        gate1 = GateVerdict(
            1, NOT_MEASURED, ("0 OOM; no accepted answer, so the acceptance criteria were never exercised",)
        )
    elif unverified:
        gate1 = GateVerdict(
            1, NOT_MEASURED,
            ("0 OOM; 0 accepted out-of-scope citation; "
             f"{unverified} accepted answers carry free text the harness never verifies as free of risk authority",),
        )
    else:
        gate1 = GateVerdict(1, WOULD_PASS, ("0 OOM; 0 accepted out-of-scope citation; no accepted free text",))

    holdout = by_block.get("holdout")
    if holdout is None or not holdout.calls:
        gate2 = GateVerdict(2, NOT_MEASURED, ("no holdout call",))
    else:
        schema = _percent(holdout.schema_first_pass)
        assert schema is not None
        if schema < MIN_SCHEMA_PERCENT:
            gate2 = GateVerdict(
                2, WOULD_NOT_PASS, (f"first-pass schema validity {schema:.1f}% is below {MIN_SCHEMA_PERCENT:.0f}%",)
            )
        else:
            gate2 = GateVerdict(2, NOT_MEASURED, ("unsupported material claim rate needs human labels",))

    if holdout_gold is None or not holdout_gold.combined_recall.measured or not holdout_gold.false_abstention.measured:
        gate3 = GateVerdict(3, NOT_MEASURED, ("no deterministic gold measured on the holdout",))
    else:
        failures: list[str] = []
        recall = _percent(holdout_gold.combined_recall)
        false = _percent(holdout_gold.false_abstention)
        agreement = _percent(holdout_gold.agreement)
        assert recall is not None and false is not None and agreement is not None
        if recall < MIN_RECALL_PERCENT:
            failures.append(f"rejection/abstention recall {recall:.1f}% is below {MIN_RECALL_PERCENT:.0f}%")
        if false > MAX_FALSE_ABSTENTION_PERCENT:
            failures.append(f"false abstention {false:.1f}% is above {MAX_FALSE_ABSTENTION_PERCENT:.0f}%")
        if agreement < MIN_GOLD_AGREEMENT_PERCENT:
            failures.append(f"gold agreement {agreement:.1f}% is below {MIN_GOLD_AGREEMENT_PERCENT:.0f}%")
        if failures:
            gate3 = GateVerdict(3, WOULD_NOT_PASS, tuple(failures))
        else:
            gate3 = GateVerdict(3, WOULD_PASS, ("deterministic categories only; human-gold categories not measured",))

    warm = by_block.get("warm")
    reasons4: list[str] = []
    if timeouts:
        reasons4.append(f"{timeouts} timeouts")
    if breaches:
        reasons4.append(
            f"{breaches} resource-reserve breaches (free RAM below {RESERVE_RAM_GIB} GiB: {breaches_ram}; "
            f"free VRAM below {RESERVE_VRAM_GIB} GiB: {breaches_vram})"
        )
    if unreported:
        reasons4.append(f"{unreported} calls with resources unreported")
    warm_p95 = None if warm is None else warm.wall_seconds.p95
    if warm_p95 is not None and warm_p95 > WARM_P95_TARGET_SECONDS:
        reasons4.append(f"warm p95 {warm_p95:.2f} s is above {WARM_P95_TARGET_SECONDS:.0f} s")
    if calls == 0:
        gate4 = GateVerdict(4, NOT_MEASURED, ("no measured call",))
    elif reasons4:
        gate4 = GateVerdict(4, WOULD_NOT_PASS, tuple(reasons4))
    elif warm_p95 is None:
        gate4 = GateVerdict(4, NOT_MEASURED, ("warm p95 not measured",))
    else:
        gate4 = GateVerdict(4, WOULD_PASS, (f"warm p95 {warm_p95:.2f} s; 0 timeouts; reserves respected",))
    return gate1, gate2, gate3, gate4


def gate5_verdict(per_model: Mapping[str, Sequence[GateVerdict]]) -> GateVerdict:
    """Gate 5 only applies among profiles that would pass gates 1-4; it is never a selection here."""
    passing = sorted(model for model, verdicts in per_model.items() if all(item.state == WOULD_PASS for item in verdicts))
    if not passing:
        return GateVerdict(5, NOT_APPLICABLE, ("no profile would pass gates 1-4 on the measured metrics",))
    return GateVerdict(5, NOT_MEASURED, ("would enter the comparison: " + ", ".join(passing),))


# -- the whole file --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelInfo:
    model: str
    digest: str | None
    installed: bool | None
    quantization: str | None
    context_length: int | None
    parameter_size: str | None
    family: str | None
    format: str | None
    parameters: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InvocationInfo:
    invocation_id: str | None
    status: str | None
    exit_code: int | None
    budget_seconds: float | None
    elapsed_seconds: float | None
    model_calls: int | None
    call_records: int  # call records in the file stamped with this invocation id
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Summary:
    lines: int
    record_counts: Mapping[str, int]
    truncated_last_line: int | None
    marked_truncated_lines: tuple[int, ...]
    freeze_sha256: tuple[str, ...]
    freeze_hashes: tuple[Mapping[str, str], ...]
    models: tuple[ModelInfo, ...]
    invocations: tuple[InvocationInfo, ...]
    selection: Mapping[str, object] | None  # as the last invocation recorded it
    digests_per_model: Mapping[str, tuple[str, ...]]
    settings_per_model: Mapping[str, tuple[str, ...]]
    warmups_per_model: Mapping[str, int]
    groups: tuple[GroupMetrics, ...]
    gold: tuple[GoldMetrics, ...]
    stability: tuple[StabilityMetrics, ...]
    coverage: tuple[HoldoutCoverage, ...]
    gates: Mapping[str, tuple[GateVerdict, ...]]
    gate5: GateVerdict | None

    @property
    def call_records(self) -> int:
        return int(self.record_counts.get("call", 0))


def _model_info(record: Mapping[str, object]) -> ModelInfo:
    details = _mapping(record.get("details"))
    capabilities = details.get("capabilities")
    installed = record.get("installed")
    return ModelInfo(
        model=_text(record.get("model")) or "",
        digest=_text(record.get("digest")),
        installed=installed if isinstance(installed, bool) else None,
        quantization=_text(details.get("quantization")),
        context_length=_count(details.get("context_length")),
        parameter_size=_text(details.get("parameter_size")),
        family=_text(details.get("family")),
        format=_text(details.get("format")),
        parameters=_text(details.get("parameters")),
        capabilities=tuple(item for item in capabilities if isinstance(item, str)) if isinstance(capabilities, list) else (),
    )


def _invocation_info(record: Mapping[str, object], call_records: int) -> InvocationInfo:
    notes = record.get("notes")
    exit_code = record.get("exit_code")
    return InvocationInfo(
        invocation_id=_text(record.get("invocation_id")),
        status=_text(record.get("status")),
        exit_code=exit_code if type(exit_code) is int else None,
        budget_seconds=_number(record.get("budget_seconds")),
        elapsed_seconds=_number(record.get("elapsed_seconds")),
        model_calls=_count(record.get("model_calls_this_invocation")),
        call_records=call_records,
        notes=tuple(item for item in notes if isinstance(item, str)) if isinstance(notes, list) else (),
    )


def _model_order(names: Iterable[str], preflight: Sequence[ModelInfo]) -> list[str]:
    order = [info.model for info in preflight]
    rest = sorted(set(names) - set(order))
    return [name for name in order if name in set(names)] + rest


def summarize(text: str) -> Summary:
    """Every metric of the results text. Raises ``MetricsInputError`` on a file it cannot trust."""
    parsed = parse_log(text)
    counts: dict[str, int] = {}
    for record in parsed.records:
        kind = _text(record.get("record_type")) or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    calls = [_call(record) for record in parsed.records if record.get("record_type") == "call"]
    keys = [(item.block, item.model, item.digest, item.case_id, item.repetition) for item in calls]
    if len(set(keys)) != len(keys):
        raise MetricsInputError("duplicate call key: the file was not written by the runner")
    preflight = [_model_info(record) for record in parsed.records if record.get("record_type") == "preflight"]
    per_invocation: dict[str | None, int] = {}
    for item in calls:
        per_invocation[item.invocation_id] = per_invocation.get(item.invocation_id, 0) + 1
    invocation_records = [record for record in parsed.records if record.get("record_type") == "invocation"]
    invocations = [
        _invocation_info(record, per_invocation.get(_text(record.get("invocation_id")), 0)) for record in invocation_records
    ]
    selection: Mapping[str, object] | None = None
    for record in reversed(invocation_records):
        found = record.get("selection")
        if isinstance(found, Mapping):
            selection = found
            break
    freeze = sorted(
        {value for record in parsed.records if (value := _text(record.get("freeze_sha256"))) is not None}
    )
    hash_sets: list[Mapping[str, str]] = []
    for record in parsed.records:
        hashes = record.get("freeze_hashes")
        if isinstance(hashes, Mapping):
            clean = {str(name): value for name, value in hashes.items() if isinstance(value, str)}
            if clean not in hash_sets:
                hash_sets.append(clean)

    models = _model_order({item.model for item in calls}, preflight)
    measured = [item for item in calls if item.block != WARMUP_BLOCK]
    groups: list[GroupMetrics] = []
    gold: list[GoldMetrics] = []
    stability: list[StabilityMetrics] = []
    coverage: list[HoldoutCoverage] = []
    gates: dict[str, tuple[GateVerdict, ...]] = {}
    for model in models:
        own = [item for item in measured if item.model == model]
        model_groups: list[GroupMetrics] = []
        holdout_gold: GoldMetrics | None = None
        blocks = [block for block in MEASURED_BLOCKS if any(item.block == block for item in own)]
        blocks += sorted({item.block for item in own} - set(MEASURED_BLOCKS))
        for block in blocks:
            chosen = [item for item in own if item.block == block]
            model_groups.append(group_metrics(model, block, chosen))
            block_gold = gold_metrics(model, block, chosen)
            if block_gold.gold_calls:
                gold.append(block_gold)
            if block == "holdout":
                holdout_gold = block_gold
            if block == STABILITY_BLOCK:
                stability.append(stability_metrics(model, chosen))
        groups.extend(model_groups)
        coverage.append(holdout_coverage(model, [item for item in calls if item.model == model]))
        if own:
            gates[model] = gate_verdicts(model_groups, holdout_gold)
    return Summary(
        lines=parsed.lines,
        record_counts=dict(sorted(counts.items())),
        truncated_last_line=parsed.truncated_last_line,
        marked_truncated_lines=parsed.marked_truncated_lines,
        freeze_sha256=tuple(freeze),
        freeze_hashes=tuple(hash_sets),
        models=tuple(preflight),
        invocations=tuple(invocations),
        selection=selection,
        digests_per_model={model: tuple(sorted({item.digest for item in calls if item.model == model})) for model in models},
        settings_per_model={
            model: tuple(sorted({item.settings for item in calls if item.model == model and item.settings is not None}))
            for model in models
        },
        warmups_per_model={model: sum(1 for item in calls if item.model == model and item.block == WARMUP_BLOCK) for model in models},
        groups=tuple(groups),
        gold=tuple(gold),
        stability=tuple(stability),
        coverage=tuple(coverage),
        gates=gates,
        gate5=gate5_verdict(gates) if gates else None,
    )
