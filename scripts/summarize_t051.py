"""Print the measured metrics of the T051 Screener runs as Markdown.

Reads ONE file, read-only: the runner's append-only results
``C:/Users/User/crypto-radar-t051/results/t051_results.jsonl`` (``--results`` points it at
another file, which the tests use with a synthetic file in a temporary directory). Writes
nothing anywhere: the report goes to stdout. The file comes from outside the repository and is
treated as untrusted input: size capped, strict UTF-8, strict JSON (no NaN/Infinity), every
field type-checked, and every string that reaches the output reduced to printable ASCII with
Markdown table characters neutralised. Model reply bodies and case contents never reach the
output: only outcomes, counts, timings, resources, model names and hashes.

Exit codes: 0 printed; 2 input refused (missing, not a regular file, too large, not UTF-8, or
not the runner's records).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from radar_v08.workflow.t051_metrics import (  # noqa: E402
    ALL_CATEGORIES,
    DETERMINISTIC_CATEGORIES,
    NOT_ACCEPTED,
    Extreme,
    GateVerdict,
    MetricsInputError,
    Rate,
    Spread,
    Summary,
    summarize,
)

DEFAULT_RESULTS = Path("C:/Users/User/crypto-radar-t051/results/t051_results.jsonl")
MAX_RESULTS_BYTES = 64 * 1024 * 1024
MAX_FIELD_CHARS = 160
NOT_MEASURED_TEXT = "not measured"
_GIB = 1024**3


class InputRefused(ValueError):
    """The results file cannot be read safely."""


def read_results(path: Path) -> tuple[str, int, str]:
    """(text, size in bytes, sha256) of the results file, opened read-only in binary."""
    try:
        info = os.stat(path)
    except OSError as error:
        raise InputRefused(f"cannot stat the results file: {error.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise InputRefused("the results path is not a regular file")
    if info.st_size > MAX_RESULTS_BYTES:
        raise InputRefused(f"the results file is larger than {MAX_RESULTS_BYTES} bytes")
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_RESULTS_BYTES + 1)
    except OSError as error:
        raise InputRefused(f"cannot read the results file: {error.strerror}") from None
    if len(data) > MAX_RESULTS_BYTES:
        raise InputRefused(f"the results file is larger than {MAX_RESULTS_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise InputRefused("the results file is not UTF-8") from None
    return text, len(data), hashlib.sha256(data).hexdigest()


# -- formatting ------------------------------------------------------------------------------


def safe(value: object, limit: int = MAX_FIELD_CHARS) -> str:
    """Printable ASCII only, Markdown table and code characters neutralised, length capped."""
    if value is None:
        return NOT_MEASURED_TEXT
    text = str(value).replace("\r\n", "\n").replace("\n", "; ")
    text = "".join(char if " " <= char <= "~" else "?" for char in text)
    text = text.replace("|", "/").replace("`", "'").replace("<", "(").replace(">", ")")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def fmt_rate(rate: Rate) -> str:
    if not rate.measured:
        return f"{NOT_MEASURED_TEXT} (0 calls)"
    value = rate.rate
    interval = rate.interval
    assert value is not None and interval is not None
    return f"{rate.successes}/{rate.total} = {value * 100:.1f}% [{interval[0] * 100:.1f}-{interval[1] * 100:.1f}]"


def fmt_spread(item: Spread, unit: str, digits: int = 2) -> str:
    if item.count == 0 or item.p50 is None or item.p95 is None:
        return NOT_MEASURED_TEXT
    coverage = "" if item.count == item.total else f" ({item.count}/{item.total} calls)"
    return f"{item.p50:.{digits}f} / {item.p95:.{digits}f} {unit}{coverage}"


def fmt_extreme(item: Extreme, scale: float, unit: str, digits: int = 2) -> str:
    if item.value is None:
        return NOT_MEASURED_TEXT
    coverage = "" if item.measured == item.total else f" ({item.measured}/{item.total} calls)"
    return f"{item.value / scale:.{digits}f} {unit}{coverage}"


def fmt_fraction(item: Extreme) -> str:
    if item.value is None:
        return NOT_MEASURED_TEXT
    coverage = "" if item.measured == item.total else f" ({item.measured}/{item.total} calls)"
    return f"{item.value * 100:.1f}% on CPU{coverage}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _state(verdict: GateVerdict) -> str:
    return {
        "would_pass": "would pass on the measured metrics",
        "would_not_pass": "would not pass on the measured metrics",
        "not_measured": "not measured",
        "not_applicable": "not applicable",
    }.get(verdict.state, safe(verdict.state))


# -- the report ------------------------------------------------------------------------------


def render(summary: Summary, path: Path, size: int, sha256: str) -> str:
    out: list[str] = []
    out.append("# T051 measured metrics (generated by scripts/summarize_t051.py)")
    out.append("")
    out.append(f"- Input (read-only): `{safe(path.as_posix())}`, {size} bytes, sha256 `{sha256}`")
    counts = ", ".join(f"{safe(name)} {count}" for name, count in summary.record_counts.items()) or "none"
    out.append(f"- Lines: {summary.lines}; records by type: {counts}")
    if summary.truncated_last_line is not None:
        out.append(f"- Truncated last line {summary.truncated_last_line}: tolerated, not counted")
    if summary.marked_truncated_lines:
        out.append("- Lines marked truncated by the runner: " + ", ".join(str(n) for n in summary.marked_truncated_lines))
    out.append("- Freeze sha256 in the records: " + (", ".join(f"`{safe(v)}`" for v in summary.freeze_sha256) or NOT_MEASURED_TEXT))
    if len(summary.freeze_hashes) > 1:
        out.append(f"- WARNING: {len(summary.freeze_hashes)} different freeze hash sets in the records")
    for hashes in summary.freeze_hashes[:1]:
        for name, value in sorted(hashes.items()):
            out.append(f"  - `{safe(name)}` `{safe(value)}`")
    out.append("")
    if summary.call_records == 0:
        out.append("**No call record in the file: no model call was measured.** Every metric below is not measured.")
        out.append("")

    out.append("## Models (preflight, no inference)")
    out.append("")
    rows = [
        [
            safe(info.model), f"`{safe(info.digest)}`", safe(info.installed), safe(info.quantization),
            safe(info.context_length), safe(info.parameter_size), safe(info.family), safe(", ".join(info.capabilities)),
            safe(info.parameters, 240),
        ]
        for info in summary.models
    ]
    out += _table(
        ["Model", "Digest", "Installed", "Quantization", "Native context", "Parameters", "Family", "Capabilities",
         "Model default options in Ollama (the request sets temperature, num_ctx, num_predict)"],
        rows,
    ) if rows else ["No preflight record."]
    out.append("")
    out.append("Request settings actually sent (all calls of the model; prompt only as a hash):")
    out.append("")
    for model, settings in summary.settings_per_model.items():
        digests = ", ".join(f"`{safe(d)}`" for d in summary.digests_per_model.get(model, ()))
        out.append(f"- {safe(model)} (digests in call keys: {digests}; warm-up calls outside every metric: "
                   f"{summary.warmups_per_model.get(model, 0)}):")
        out += [f"  - `{safe(item, 400)}`" for item in settings] or ["  - none recorded"]
    out.append("")

    out.append("## Invocations")
    out.append("")
    out += _table(
        ["Invocation id", "Status", "Exit", "Budget (s)", "Elapsed (s)", "Model calls", "Call records", "Notes"],
        [
            [safe(item.invocation_id), safe(item.status), safe(item.exit_code), safe(item.budget_seconds),
             safe(item.elapsed_seconds), safe(item.model_calls), str(item.call_records), safe("; ".join(item.notes)) or "-"]
            for item in summary.invocations
        ],
    ) if summary.invocations else ["No invocation record."]
    out.append("")
    if summary.selection is not None:
        candidates = summary.selection.get("candidates")
        names = [safe(name) for name in candidates] if isinstance(candidates, list) else []
        out.append("Selection as the last invocation recorded it: candidates " + (", ".join(names) or "none"))
        probe = summary.selection.get("probe")
        if isinstance(probe, list):
            for entry in probe:
                if isinstance(entry, Mapping):
                    disqualified = entry.get("disqualified")
                    reasons = ", ".join(safe(r) for r in disqualified) if isinstance(disqualified, list) and disqualified else "-"
                    out.append(f"- {safe(entry.get('model'))}: schema valid {safe(entry.get('schema_valid'))}/"
                               f"{safe(entry.get('calls'))}, p95 {safe(entry.get('p95_seconds'))} s, peak model memory "
                               f"{safe(entry.get('peak_model_memory_bytes'))} bytes, disqualified: {reasons}")
        out.append("")

    out.append("## Outcomes per model and step (warm-up excluded; invalid replies and timeouts in every denominator)")
    out.append("")
    out.append("Rates are `k/n = rate [Wilson 95% lower-upper]`, n = every recorded call of the group.")
    out.append("")
    out += _table(
        ["Model", "Step", "Calls", "Final accepted", "First-pass schema valid", "Schema not evaluated (earlier gate)",
         "Risk hard refusals", "Citation out of scope", "Timeouts", "OOM", "Reserve breached (RAM/VRAM)"],
        [
            [safe(g.model), safe(g.block), str(g.calls), fmt_rate(g.accepted), fmt_rate(g.schema_first_pass),
             str(g.schema_not_evaluated), fmt_rate(g.risk_rejected), fmt_rate(g.citation_out_of_scope),
             fmt_rate(g.timeouts), fmt_rate(g.ooms),
             f"{fmt_rate(g.reserve_breached)} ({g.reserve_breached_ram}/{g.reserve_breached_vram})"]
            for g in summary.groups
        ],
    ) if summary.groups else ["No measured call."]
    out.append("")
    out.append("Secondary view, not the OC-1 denominator: calls the resource gate let through (that gate runs before")
    out.append("the reply is read and depends on the free memory of the whole machine).")
    out.append("")
    out += _table(
        ["Model", "Step", "Calls past the resource gate", "Accepted among them", "Risk hard refusals among them"],
        [
            [safe(g.model), safe(g.block), str(g.accepted_past_resource_gate.total), fmt_rate(g.accepted_past_resource_gate),
             fmt_rate(g.risk_rejected_past_resource_gate)]
            for g in summary.groups
        ],
    ) if summary.groups else ["No measured call."]
    out.append("")
    out.append("## Time and resources per model and step (p50 / p95; peaks over the calls that measured them)")
    out.append("")
    out += _table(
        ["Model", "Step", "End-to-end wall", "Ollama load_duration", "Prompt tok/s", "Generation tok/s",
         "Peak GPU used (nvidia-smi)", "Peak /api/ps size", "Peak /api/ps size_vram", "Peak offload",
         "Peak ollama.exe RAM", "Min free VRAM", "Min free RAM"],
        [
            [safe(g.model), safe(g.block), fmt_spread(g.wall_seconds, "s"), fmt_spread(g.load_seconds, "s"),
             fmt_spread(g.prompt_tokens_per_second, "", 0), fmt_spread(g.generation_tokens_per_second, "", 1),
             fmt_extreme(g.peak_gpu_used_mib, 1, "MiB", 0), fmt_extreme(g.peak_ps_size_bytes, _GIB, "GiB"),
             fmt_extreme(g.peak_ps_size_vram_bytes, _GIB, "GiB"), fmt_fraction(g.peak_offload_fraction),
             fmt_extreme(g.peak_process_ram_bytes, _GIB, "GiB"), fmt_extreme(g.min_free_vram_gib, 1, "GiB"),
             fmt_extreme(g.min_free_ram_gib, 1, "GiB")]
            for g in summary.groups
        ],
    ) if summary.groups else ["No measured call."]
    out.append("")

    out.append("## Gold (the three deterministic categories only)")
    out.append("")
    if not summary.gold:
        out.append("No call with deterministic gold: gold metrics not measured.")
    else:
        out += _table(
            ["Model", "Step", "Gold calls", "Rejection recall (invalid/stale)", "Abstention recall (insufficient)",
             "Rejection+abstention recall", "False abstention (no edge)", "Gold agreement", "Left out (no gold)"],
            [
                [safe(g.model), safe(g.block), str(g.gold_calls), fmt_rate(g.rejection_recall),
                 fmt_rate(g.abstention_recall), fmt_rate(g.combined_recall), fmt_rate(g.false_abstention),
                 fmt_rate(g.agreement), str(g.excluded_without_gold)]
                for g in summary.gold
            ],
        )
        for g in summary.gold:
            out.append("")
            out.append(f"Confusion matrix, {safe(g.model)} / {safe(g.block)} (rows gold category, columns accepted classification):")
            out.append("")
            columns = (*ALL_CATEGORIES, NOT_ACCEPTED)
            out += _table(
                ["Gold \\ answer", *columns],
                [[category, *(str(g.confusion[category][column]) for column in columns)] for category in DETERMINISTIC_CATEGORIES],
            )
    out.append("")

    out.append("## Stability (20 fixed holdout cases x 3)")
    out.append("")
    out += _table(
        ["Model", "Cases", "Cases with 3 repetitions", "Same harness decision in all 3",
         "Same answer content in all 3 (sha256)"],
        [
            [safe(s.model), str(s.cases), str(s.complete_cases), fmt_rate(s.identical_decision), fmt_rate(s.identical_answer)]
            for s in summary.stability
        ],
    ) if summary.stability else ["No stability call: not measured."]
    out.append("")

    out.append("## Holdout coverage")
    out.append("")
    out += _table(
        ["Model", "Distinct labelled holdout cases (holdout step)", "Labelled holdout calls", "Sealed (no gold) holdout cases sent"],
        [[safe(c.model), str(c.labelled_distinct), str(c.labelled_calls), str(c.sealed_sent)] for c in summary.coverage],
    ) if summary.coverage else ["No call."]
    out.append("")

    out.append("## OC-1 section 6 gates 1-5: would pass / would not pass on the measured metrics (never a promotion)")
    out.append("")
    rows = [
        [safe(model), f"{verdict.gate}", _state(verdict), safe("; ".join(verdict.reasons), 300)]
        for model, verdicts in summary.gates.items()
        for verdict in verdicts
    ]
    if summary.gate5 is not None:
        rows.append(["(all)", "5", _state(summary.gate5), safe("; ".join(summary.gate5.reasons), 300)])
    out += _table(["Model", "Gate", "State", "Why"], rows) if rows else ["No measured call: every gate not measured."]
    out.append("")
    return "\n".join(out)


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print the T051 measured metrics as Markdown (reads one file, writes nothing).")
    parser.add_argument("--results", default=str(DEFAULT_RESULTS), help="the runner's results JSONL (read-only)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    path = Path(arguments.results)
    try:
        text, size, sha256 = read_results(path)
        summary = summarize(text)
    except (InputRefused, MetricsInputError) as error:
        print(f"REFUSED: {safe(error)}", file=sys.stderr)
        return 2
    sys.stdout.write(render(summary, path, size, sha256) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
