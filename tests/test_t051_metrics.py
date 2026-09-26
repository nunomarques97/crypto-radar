"""T051e metrics tests: synthetic results JSONL in temporary directories only.

No test reads the real results under C:/Users/User/crypto-radar-t051 or calls any model: every
file here is written by the test into a ``tempfile.TemporaryDirectory`` and read back through
``scripts/summarize_t051.py`` (read-only), exactly as the real file is read.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(REPOSITORY_ROOT))

from radar_v08.workflow import t051_metrics as metrics  # noqa: E402
from radar_v08.workflow.t051_metrics import (  # noqa: E402
    NOT_APPLICABLE,
    NOT_MEASURED,
    WOULD_NOT_PASS,
    WOULD_PASS,
    MetricsInputError,
    Rate,
    parse_log,
    percentile,
    summarize,
    wilson_interval,
)

_SPEC = importlib.util.spec_from_file_location("summarize_t051", REPOSITORY_ROOT / "scripts" / "summarize_t051.py")
assert _SPEC is not None and _SPEC.loader is not None
summarizer = importlib.util.module_from_spec(_SPEC)
sys.modules["summarize_t051"] = summarizer
_SPEC.loader.exec_module(summarizer)

DIGEST = "d" * 64
SECRET_CASE_TEXT = "CASE-TEXT-MUST-NEVER-BE-PRINTED"
SECRET_ANSWER_TEXT = "ANSWER-TEXT-MUST-NEVER-BE-PRINTED"

_counter = iter(range(1, 1_000_000))


def body(content: str, total_duration: int) -> str:
    """An Ollama /api/chat envelope: same content, different timings, gives different bytes."""
    return json.dumps(
        {"model": "m", "message": {"role": "assistant", "content": content}, "done": True, "total_duration": total_duration}
    )


def call(
    *,
    model: str = "qwen3:14b",
    block: str = "holdout",
    case_id: str | None = None,
    repetition: int = 1,
    partition: str = "holdout",
    category: str = "invalid_or_stale",
    labelled: bool = True,
    abstain_expected: bool | None = None,
    outcome: str = "accepted",
    abstained: bool | None = True,
    classification: str | None = None,
    schema_valid: bool | None = None,
    wall: float | None = 1.0,
    timing: dict[str, object] | None = None,
    resources: dict[str, object] | None | str = "default",
    content: str = '{"abstain": true}',
    invocation: str = "inv-1",
    rationale_unverified: bool = False,
    min_free_ram: float = 8.0,
    min_free_vram: float = 4.0,
) -> dict[str, object]:
    """One call record shaped like the runner's (``scripts/run_t051_block.py``)."""
    if abstain_expected is None and labelled and category in metrics.DETERMINISTIC_CATEGORIES:
        abstain_expected = category != "admissible_no_edge"
    if schema_valid is None:
        schema_valid = outcome in ("accepted", "citation_out_of_scope")
    if classification is None and outcome in ("accepted", "citation_out_of_scope"):
        classification = category
    if outcome not in ("accepted", "citation_out_of_scope"):
        abstained = None
        classification = None
    record_timing: dict[str, object] | None
    if timing is not None:
        record_timing = timing
    elif wall is None:
        record_timing = None
    else:
        record_timing = {"wall_seconds": wall, "load_duration": 1_000_000, "prompt_eval_count": 100,
                         "prompt_eval_duration": 500_000_000, "eval_count": 50, "eval_duration": 1_000_000_000}
    if resources == "default":
        resources = {"gpu_used_mib": 9000, "gpu_total_mib": 16000, "free_vram_gib": min_free_vram, "free_ram_gib": min_free_ram,
                     "free_ram_bytes": 1, "ollama_process_ram_bytes": 50 * 1024 * 1024, "ollama_ps_size_bytes": 100,
                     "ollama_ps_size_vram_bytes": 100, "ollama_ps_digest": DIGEST[:12], "oom": outcome == "oom",
                     "not_measured": []}
    number = next(_counter)
    return {
        "record_type": "call",
        "schema": 1,
        "invocation_id": invocation,
        "freeze_sha256": "f" * 64,
        "freeze_hashes": {"screener_system_prompt": "a" * 64},
        "key": {"block": block, "model": model, "digest": DIGEST, "case_id": case_id or f"case-{number:05d}",
                "repetition": repetition},
        "warmup": block == "warmup",
        "for_block": block,
        "partition": partition,
        "category": category,
        "case_sha256": "c" * 64,
        "labelled": labelled,
        "profile_id": "p",
        "user": SECRET_CASE_TEXT,
        "request": {"model": model, "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 768},
                    "messages_sha256": "e" * 64, "keep_alive": 120, "stream": False},
        "timing": record_timing,
        "response": {"body": body(content + SECRET_ANSWER_TEXT, number), "body_truncated": False, "body_sha256": f"{number:064d}"},
        "harness": {
            "outcome": outcome,
            "failure_code": None,
            "first_pass_schema_valid": schema_valid,
            "abstain_expected": abstain_expected,
            "abstained": abstained,
            "classification": classification,
            "cited_count": 0,
            "min_free_vram_gib": min_free_vram,
            "min_free_ram_gib": min_free_ram,
            "gold_status": "gold_unavailable",
            "rationale_unverified": rationale_unverified,
        },
        "resources": None if resources is None else resources,
    }


def preflight(model: str = "qwen3:14b") -> dict[str, object]:
    return {"record_type": "preflight", "model": model, "digest": DIGEST, "installed": True,
            "details": {"quantization": "Q4_K_M", "context_length": 40960, "parameter_size": "14.8B", "family": "qwen3",
                        "format": "gguf", "parameters": "temperature 0.6", "capabilities": ["completion"]}}


def jsonl(records: list[dict[str, object]]) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


class TempResults(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="t051-metrics-")
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "t051_results.jsonl"

    def write(self, text: str | bytes) -> Path:
        if isinstance(text, bytes):
            self.path.write_bytes(text)
        else:
            self.path.write_text(text, encoding="utf-8", newline="")
        return self.path

    def summary(self, records: list[dict[str, object]]) -> metrics.Summary:
        text, _, _ = summarizer.read_results(self.write(jsonl(records)))
        return summarize(text)

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = summarizer.main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def group(summary: metrics.Summary, model: str, block: str) -> metrics.GroupMetrics:
        return next(item for item in summary.groups if item.model == model and item.block == block)


class TestWilson(unittest.TestCase):
    """Reference values computed independently with 50-digit Decimal arithmetic."""

    def test_z_is_the_95_percent_normal_quantile(self) -> None:
        self.assertEqual(metrics.Z95, 1.959963984540054)

    def test_reference_values(self) -> None:
        references = {
            (0, 20): (0.0, 0.161125158052819),
            (20, 20): (0.838874841947181, 1.0),
            (3, 20): (0.052368745896217, 0.360418864740757),
            (16, 20): (0.583982567748106, 0.919342337420202),
            (8, 120): (0.034165306860906, 0.126051304035303),
            (54, 120): (0.363917350838746, 0.539184565803124),
            (5, 10): (0.236593090512564, 0.763406909487436),
            (1, 1): (0.206549314377237, 1.0),
        }
        for (successes, total), (low, high) in references.items():
            with self.subTest(successes=successes, total=total):
                interval = wilson_interval(successes, total)
                assert interval is not None
                self.assertAlmostEqual(interval[0], low, places=12)
                self.assertAlmostEqual(interval[1], high, places=12)

    def test_closed_forms_at_the_edges(self) -> None:
        z2 = metrics.Z95**2
        for total in (1, 7, 40, 120):
            low, high = wilson_interval(0, total) or (math.nan, math.nan)
            self.assertEqual(low, 0.0)
            self.assertAlmostEqual(high, z2 / (total + z2), places=14)
            low, high = wilson_interval(total, total) or (math.nan, math.nan)
            self.assertAlmostEqual(low, total / (total + z2), places=14)
            self.assertEqual(high, 1.0)

    def test_no_denominator_is_not_measured_never_zero(self) -> None:
        self.assertIsNone(wilson_interval(0, 0))
        rate = Rate(0, 0)
        self.assertFalse(rate.measured)
        self.assertIsNone(rate.rate)
        self.assertIsNone(rate.interval)
        self.assertEqual(summarizer.fmt_rate(rate), "not measured (0 calls)")

    def test_bad_counts_are_refused(self) -> None:
        for successes, total in ((3, 2), (-1, 4), (1, -1), (True, 3), (1.0, 3)):
            with self.subTest(successes=successes, total=total), self.assertRaises(MetricsInputError):
                wilson_interval(successes, total)  # type: ignore[arg-type]

    def test_rate_renders_count_rate_and_interval(self) -> None:
        self.assertEqual(summarizer.fmt_rate(Rate(3, 20)), "3/20 = 15.0% [5.2-36.0]")


class TestPercentile(unittest.TestCase):
    def test_linear_interpolation(self) -> None:
        self.assertEqual(percentile([4.0, 1.0, 3.0, 2.0], 0.5), 2.5)
        self.assertAlmostEqual(percentile([4.0, 1.0, 3.0, 2.0], 0.95) or 0.0, 3.85, places=12)
        self.assertAlmostEqual(percentile([0.0, 10.0], 0.95) or 0.0, 9.5, places=12)
        self.assertEqual(percentile([7.0], 0.95), 7.0)
        values = [float(v) for v in range(1, 21)]  # 1..20: position 18.05
        self.assertAlmostEqual(percentile(values, 0.95) or 0.0, 19.05, places=12)
        self.assertEqual(percentile(values, 0.5), 10.5)

    def test_nothing_is_not_measured(self) -> None:
        self.assertIsNone(percentile([], 0.95))
        spread = metrics.spread([None, None], 2)
        self.assertEqual((spread.count, spread.p50, spread.p95), (0, None, None))
        self.assertEqual(summarizer.fmt_spread(spread, "s"), "not measured")

    def test_fraction_out_of_range_is_refused(self) -> None:
        with self.assertRaises(MetricsInputError):
            percentile([1.0], 1.5)


class TestParsing(unittest.TestCase):
    def test_truncated_last_line_is_tolerated_and_reported(self) -> None:
        text = jsonl([preflight()]) + '{"record_type": "call", "ke'
        parsed = parse_log(text)
        self.assertEqual((len(parsed.records), parsed.truncated_last_line), (1, 2))

    def test_marked_truncated_line_is_tolerated(self) -> None:
        text = jsonl([preflight()]) + '{"broken\n' + json.dumps({"record_type": "truncated_tail", "line": 2}) + "\n"
        parsed = parse_log(text)
        self.assertEqual(parsed.marked_truncated_lines, (2,))

    def test_corrupt_middle_line_is_refused(self) -> None:
        with self.assertRaises(MetricsInputError):
            parse_log('{"broken\n' + jsonl([preflight()]))

    def test_non_finite_numbers_are_refused(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant), self.assertRaises(MetricsInputError):
                parse_log('{"record_type": "call", "x": ' + constant + "}\n" + jsonl([preflight()]))

    def test_duplicate_call_key_is_refused(self) -> None:
        record = call(case_id="same")
        with self.assertRaises(MetricsInputError):
            summarize(jsonl([record, dict(record)]))

    def test_malformed_key_is_refused(self) -> None:
        record = call()
        record["key"] = {"block": "holdout", "model": "m", "digest": DIGEST, "case_id": "x", "repetition": "1"}
        with self.assertRaises(MetricsInputError):
            summarize(jsonl([record]))


class TestDenominators(TempResults):
    def test_invalid_replies_and_timeouts_stay_in_every_denominator(self) -> None:
        records = [
            call(outcome="accepted", category="admissible_no_edge", abstained=False),
            call(outcome="timeout", category="admissible_no_edge", wall=30.2),
            call(outcome="schema_invalid", category="admissible_no_edge"),
            call(outcome="risk_authority_rejected", category="admissible_no_edge"),
        ]
        group = self.group(self.summary(records), "qwen3:14b", "holdout")
        self.assertEqual(group.calls, 4)
        self.assertEqual((group.accepted.successes, group.accepted.total), (1, 4))
        self.assertEqual((group.schema_first_pass.successes, group.schema_first_pass.total), (1, 4))
        self.assertEqual((group.timeouts.successes, group.timeouts.total), (1, 4))
        self.assertEqual((group.risk_rejected.successes, group.risk_rejected.total), (1, 4))
        self.assertEqual(group.schema_not_evaluated, 2)  # timeout and risk refusal stop before the schema gate
        self.assertAlmostEqual(group.wall_seconds.p95 or 0.0, 1.0 + (30.2 - 1.0) * 0.85, places=9)  # [1,1,1,30.2]

    def test_resource_gate_view_is_secondary_and_separate(self) -> None:
        records = [
            call(outcome="accepted", category="admissible_no_edge", abstained=False),
            call(outcome="resource_reserve_breached", min_free_ram=2.5),
            call(outcome="resource_reserve_breached", min_free_vram=1.0),
            call(outcome="risk_authority_rejected"),
        ]
        group = self.group(self.summary(records), "qwen3:14b", "holdout")
        self.assertEqual((group.accepted.successes, group.accepted.total), (1, 4))
        self.assertEqual((group.accepted_past_resource_gate.successes, group.accepted_past_resource_gate.total), (1, 2))
        self.assertEqual((group.reserve_breached_ram, group.reserve_breached_vram), (1, 1))

    def test_rates_and_resources_are_derived_from_the_envelope(self) -> None:
        records = [call(block="cold", partition="development", timing={
            "wall_seconds": 4.0, "load_duration": 2_500_000_000, "prompt_eval_count": 1000,
            "prompt_eval_duration": 250_000_000, "eval_count": 60, "eval_duration": 1_500_000_000},
            resources={"ollama_ps_size_bytes": 200, "ollama_ps_size_vram_bytes": 150, "gpu_used_mib": 12000,
                       "free_vram_gib": 3.5, "free_ram_gib": 9.0, "ollama_process_ram_bytes": 1024**3, "oom": False})]
        group = self.group(self.summary(records), "qwen3:14b", "cold")
        self.assertEqual(group.load_seconds.p50, 2.5)
        self.assertEqual(group.prompt_tokens_per_second.p50, 4000.0)
        self.assertEqual(group.generation_tokens_per_second.p50, 40.0)
        self.assertEqual(group.peak_offload_fraction.value, 0.25)
        self.assertEqual(group.peak_gpu_used_mib.value, 12000.0)
        self.assertEqual(group.min_free_vram_gib.value, 3.5)
        self.assertEqual(group.peak_process_ram_bytes.value, float(1024**3))


class TestNotMeasured(TempResults):
    def test_metric_without_data_is_not_measured_never_zero(self) -> None:
        records = [call(block="warm", partition="development", wall=None, resources=None,
                        category="admissible_positive", labelled=False)]
        summary = self.summary(records)
        group = self.group(summary, "qwen3:14b", "warm")
        for spread in (group.wall_seconds, group.load_seconds, group.prompt_tokens_per_second, group.generation_tokens_per_second):
            self.assertEqual((spread.count, spread.p50, spread.p95), (0, None, None))
        for extreme in (group.peak_gpu_used_mib, group.peak_ps_size_bytes, group.peak_process_ram_bytes,
                        group.peak_offload_fraction, group.min_free_ram_gib, group.min_free_vram_gib):
            self.assertIsNone(extreme.value)
            self.assertEqual(extreme.measured, 0)
        self.assertEqual(summary.gold, ())  # no gold at all: no gold row, never a 0% row
        self.assertEqual(summary.stability, ())
        self.assertEqual(summary.gates["qwen3:14b"][1].state, NOT_MEASURED)  # no holdout call
        text = summarizer.render(summary, self.path, 1, "0" * 64)
        self.assertIn("No call with deterministic gold: gold metrics not measured.", text)
        self.assertIn("No stability call: not measured.", text)
        row = next(line for line in text.splitlines() if line.startswith("| qwen3:14b | warm | not measured"))
        self.assertNotIn(" 0.00 ", row)

    def test_partial_resource_readings_say_how_many_calls_they_cover(self) -> None:
        records = [call(), call(resources=None)]
        group = self.group(self.summary(records), "qwen3:14b", "holdout")
        self.assertEqual((group.peak_gpu_used_mib.measured, group.peak_gpu_used_mib.total), (1, 2))
        self.assertEqual(summarizer.fmt_extreme(group.peak_gpu_used_mib, 1, "MiB", 0), "9000 MiB (1/2 calls)")

    def test_empty_file_says_nothing_was_measured(self) -> None:
        self.write("")
        code, out, err = self.run_main("--results", str(self.path))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("**No call record in the file: no model call was measured.**", out)
        self.assertIn("No measured call: every gate not measured.", out)
        summary = summarize("")
        self.assertEqual((summary.call_records, summary.groups, summary.gate5), (0, (), None))

    def test_preflight_only_file_is_all_not_measured(self) -> None:
        summary = self.summary([preflight()])
        self.assertEqual(summary.call_records, 0)
        self.assertEqual([info.quantization for info in summary.models], ["Q4_K_M"])
        self.assertEqual(summary.gates, {})


class TestGold(TempResults):
    def test_gold_only_in_the_three_deterministic_categories(self) -> None:
        records = [
            call(category="invalid_or_stale", abstained=True),
            call(category="insufficient_evidence", abstained=True),
            call(category="admissible_no_edge", abstained=True),
            # Forged labels on the two categories without gold: never counted as gold.
            call(category="admissible_positive", labelled=True, abstain_expected=True, abstained=True),
            call(category="conflicting_evidence", labelled=True, abstain_expected=False, abstained=True),
            call(category="admissible_positive", labelled=False, abstained=False),
        ]
        gold = next(item for item in self.summary(records).gold if item.block == "holdout")
        self.assertEqual(gold.gold_calls, 3)
        self.assertEqual(gold.excluded_without_gold, 3)
        self.assertEqual(set(gold.confusion), set(metrics.DETERMINISTIC_CATEGORIES))
        self.assertEqual((gold.combined_recall.successes, gold.combined_recall.total), (2, 2))
        self.assertEqual((gold.false_abstention.successes, gold.false_abstention.total), (1, 1))

    def test_a_label_that_contradicts_its_category_is_not_gold(self) -> None:
        records = [call(category="invalid_or_stale", abstain_expected=False), call(category="admissible_no_edge", abstain_expected=True)]
        self.assertEqual(self.summary(records).gold, ())

    def test_recall_false_abstention_agreement_and_confusion(self) -> None:
        records = [
            call(category="invalid_or_stale", abstained=True),  # hit, agrees
            call(category="invalid_or_stale", abstained=True, classification="insufficient_evidence"),  # hit, disagrees
            call(category="invalid_or_stale", outcome="resource_reserve_breached"),  # miss: failures stay in
            call(category="insufficient_evidence", abstained=False),  # miss
            call(category="insufficient_evidence", outcome="timeout"),  # miss
            call(category="admissible_no_edge", abstained=False),  # answered, agrees
            call(category="admissible_no_edge", abstained=True, classification="insufficient_evidence"),  # false abstention
            call(category="admissible_no_edge", outcome="schema_invalid"),  # not an abstention, still in the denominator
            call(category="admissible_no_edge", outcome="citation_out_of_scope", abstained=True),  # abstained, not accepted
        ]
        gold = next(item for item in self.summary(records).gold if item.block == "holdout")
        self.assertEqual((gold.rejection_recall.successes, gold.rejection_recall.total), (2, 3))
        self.assertEqual((gold.abstention_recall.successes, gold.abstention_recall.total), (0, 2))
        self.assertEqual((gold.combined_recall.successes, gold.combined_recall.total), (2, 5))
        self.assertEqual((gold.false_abstention.successes, gold.false_abstention.total), (2, 4))
        self.assertEqual((gold.agreement.successes, gold.agreement.total), (3, 9))
        self.assertEqual(gold.confusion["invalid_or_stale"],
                         {"invalid_or_stale": 1, "insufficient_evidence": 1, "admissible_no_edge": 0,
                          "admissible_positive": 0, "conflicting_evidence": 0, "not_accepted": 1})
        self.assertEqual(gold.confusion["admissible_no_edge"]["not_accepted"], 2)
        self.assertEqual(sum(sum(row.values()) for row in gold.confusion.values()), 9)


class TestWarmup(TempResults):
    def test_warmup_records_stay_out_of_the_warm_metrics(self) -> None:
        records = [
            call(block="warmup", partition="development", outcome="timeout", wall=29.9, category="admissible_positive", labelled=False),
            call(block="warm", partition="development", wall=1.0, category="admissible_no_edge", abstained=False),
            call(block="warm", partition="development", wall=3.0, category="admissible_no_edge", abstained=False),
        ]
        summary = self.summary(records)
        warm = self.group(summary, "qwen3:14b", "warm")
        self.assertEqual(warm.calls, 2)
        self.assertEqual((warm.timeouts.successes, warm.wall_seconds.maximum), (0, 3.0))
        self.assertAlmostEqual(warm.wall_seconds.p95 or 0.0, 2.9, places=12)
        self.assertFalse([group for group in summary.groups if group.block == "warmup"])
        self.assertEqual(summary.warmups_per_model, {"qwen3:14b": 1})
        self.assertEqual(summary.gates["qwen3:14b"][3].state, WOULD_PASS)  # the warm-up timeout is not counted


class TestStabilityAndCoverage(TempResults):
    def test_same_answer_content_is_compared_not_envelope_bytes(self) -> None:
        records = []
        for repetition in (1, 2, 3):
            records.append(call(block="stability", case_id="s1", repetition=repetition, content='{"a": 1}'))
            records.append(call(block="stability", case_id="s2", repetition=repetition,
                                content='{"a": %d}' % repetition,
                                outcome="resource_reserve_breached" if repetition == 2 else "accepted"))
        records.append(call(block="stability", case_id="s3", repetition=1))
        stability = self.summary(records).stability[0]
        self.assertEqual((stability.cases, stability.complete_cases), (3, 2))
        self.assertEqual((stability.identical_decision.successes, stability.identical_decision.total), (1, 2))
        self.assertEqual((stability.identical_answer.successes, stability.identical_answer.total), (1, 2))

    def test_truncated_body_is_not_hashed(self) -> None:
        self.assertIsNone(metrics.answer_sha256({"body": body("x", 1), "body_truncated": True}))
        self.assertIsNone(metrics.answer_sha256({"body": "not json", "body_truncated": False}))
        self.assertIsNotNone(metrics.answer_sha256({"body": body("x", 1), "body_truncated": False}))

    def test_sealed_holdout_calls_are_counted(self) -> None:
        records = [call(case_id=f"h{n}") for n in range(3)] + [
            call(case_id="sealed", category="admissible_positive", labelled=False, block="holdout")
        ]
        coverage = self.summary(records).coverage[0]
        self.assertEqual((coverage.labelled_distinct, coverage.labelled_calls, coverage.sealed_sent), (3, 3, 1))


class TestGates(TempResults):
    def perfect(self) -> list[dict[str, object]]:
        records = [call(block="warm", partition="development", wall=2.0, category="admissible_no_edge", abstained=False)]
        for category in metrics.DETERMINISTIC_CATEGORIES:
            for _ in range(5):
                records.append(call(category=category, abstained=category != "admissible_no_edge"))
        return records

    def test_perfect_measured_metrics_still_never_promote(self) -> None:
        summary = self.summary(self.perfect())
        states = [verdict.state for verdict in summary.gates["qwen3:14b"]]
        self.assertEqual(states, [WOULD_PASS, NOT_MEASURED, WOULD_PASS, WOULD_PASS])  # claims need human labels
        assert summary.gate5 is not None
        self.assertEqual(summary.gate5.state, NOT_APPLICABLE)

    def test_failures_are_would_not_pass(self) -> None:
        records = self.perfect() + [call(outcome="oom", category="admissible_no_edge"),
                                    call(block="warm", partition="development", wall=25.0, category="admissible_no_edge", abstained=False)]
        gates = self.summary(records).gates["qwen3:14b"]
        self.assertEqual(gates[0].state, WOULD_NOT_PASS)
        self.assertEqual(gates[1].state, WOULD_NOT_PASS)  # 15/16 first-pass valid is below 99 %
        self.assertEqual(gates[3].state, WOULD_NOT_PASS)
        self.assertIn("warm p95", gates[3].reasons[0])

    def test_unverified_free_text_and_no_accepted_answer_are_not_measured(self) -> None:
        unverified = self.summary([call(rationale_unverified=True)]).gates["qwen3:14b"][0]
        self.assertEqual(unverified.state, NOT_MEASURED)
        nothing = self.summary([call(outcome="resource_reserve_breached", min_free_vram=1.0)]).gates["qwen3:14b"]
        self.assertEqual(nothing[0].state, NOT_MEASURED)
        self.assertEqual(nothing[3].state, WOULD_NOT_PASS)


class TestScript(TempResults):
    def test_output_never_carries_case_or_answer_text(self) -> None:
        self.write(jsonl([preflight(), *TestGates.perfect(self)]))
        code, out, _ = self.run_main("--results", str(self.path))
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET_CASE_TEXT, out)
        self.assertNotIn(SECRET_ANSWER_TEXT, out)
        self.assertIn("| qwen3:14b | holdout | 15 |", out)

    def test_untrusted_strings_are_neutralised(self) -> None:
        hostile = preflight("evil|model\n# heading `x` <b>\u202e")
        self.write(jsonl([hostile, call(model="evil|model\n# heading `x` <b>\u202e")]))
        code, out, _ = self.run_main("--results", str(self.path))
        self.assertEqual(code, 0)
        self.assertIn("evil/model; # heading 'x' (b)?", out)
        self.assertNotIn("\u202e", out)
        self.assertNotIn("\n# heading", out)
        self.assertTrue(out.isascii())

    def test_refused_inputs_exit_2(self) -> None:
        code, out, err = self.run_main("--results", str(self.path))  # missing
        self.assertEqual((code, out), (2, ""))
        self.assertIn("REFUSED", err)
        self.write(b"\xff\xfe not utf-8\n")
        self.assertEqual(self.run_main("--results", str(self.path))[0], 2)
        self.write('{"broken\n' + jsonl([preflight()]))
        self.assertEqual(self.run_main("--results", str(self.path))[0], 2)
        self.assertEqual(self.run_main("--results", str(self.path.parent))[0], 2)  # a directory
        self.write(jsonl([preflight()]))
        with mock.patch.object(summarizer, "MAX_RESULTS_BYTES", 10):
            self.assertEqual(self.run_main("--results", str(self.path))[0], 2)

    def test_reads_only_and_writes_nothing(self) -> None:
        self.write(jsonl([preflight(), call()]))
        before = self.path.read_bytes()
        folder = sorted(os.listdir(self.path.parent))
        root = sorted(os.listdir(REPOSITORY_ROOT))
        real_open = open
        modes: list[str] = []

        def spy(file, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            modes.append(mode)
            return real_open(file, mode, *args, **kwargs)

        with mock.patch("builtins.open", spy):
            code, _, _ = self.run_main("--results", str(self.path))
        self.assertEqual(code, 0)
        self.assertEqual(modes, ["rb"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.path.parent)), folder)
        self.assertEqual(sorted(os.listdir(REPOSITORY_ROOT)), root)

    def test_default_input_is_the_designated_results_file(self) -> None:
        self.assertEqual(summarizer.DEFAULT_RESULTS.as_posix(), "C:/Users/User/crypto-radar-t051/results/t051_results.jsonl")
        self.assertEqual(summarizer.parse_arguments([]).results, str(summarizer.DEFAULT_RESULTS))

    def test_module_is_pure(self) -> None:
        source = (REPOSITORY_ROOT / "radar_v08" / "workflow" / "t051_metrics.py").read_text(encoding="utf-8")
        for forbidden in ("import os", "pathlib", "open(", "requests", "socket", "subprocess", "time.", "datetime"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
