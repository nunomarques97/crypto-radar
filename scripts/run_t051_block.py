"""Run one resumable invocation of the T051 Screener benchmark (T051c, D59/D61).

Usage::

    python scripts/run_t051_block.py --corpus benchmarks/oc1_screener_v1 \
        --out <t051-workdir> --budget-seconds 540 [--include-optional]
    python scripts/run_t051_block.py --corpus benchmarks/oc1_screener_v1 --write-freeze

Each invocation (at most ``MAX_BUDGET_SECONDS`` = 540 s, so it fits one 10-minute shell call):

1. refuses ``--out`` inside the repository; results go to
   ``<out>/results/t051_results.jsonl`` (append only, one JSON object per line);
2. loads the locked corpus (lock sha256 pinned below), recomputes the D59 b freeze and aborts
   if it differs from the committed ``<corpus>/t051_freeze.json`` or from the freeze hash of
   any record already written;
3. ``GET /api/tags`` (Ollama down -> BLOCKED), ``GET /api/ps`` must be empty (else BLOCKED,
   nothing unloaded), preflight without inference (``POST /api/show``; a missing model is
   "not installed", never downloaded); a digest that differs from the first preflight aborts;
4. runs the next steps of the fixed sequence (``radar_v08/workflow/t051_sequence.py``) one call
   at a time through the accepted harness ``evaluate_case``, skipping every key already in the
   results, never starting a call the remaining budget cannot cover, one model loaded at a
   time, a warm-up call after each (re)load, an explicit unload of the benchmark model when the
   model changes and at the end;
5. appends an ``invocation`` summary record and prints it.

Exit codes: 0 COMPLETE, 3 PARTIAL (resume with the same command), 4 BLOCKED (Ollama down or
busy with another model: nothing was unloaded that is not the benchmark's), 5 ABORTED (freeze,
digest, corpus or results file diverged; nothing more was sent), 2 refused arguments.
Only ``http://127.0.0.1:11434`` is ever contacted (``radar_v08/adapters/t051_ollama.py``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from radar_v08.adapters import oc1_corpus_builder  # noqa: E402
from radar_v08.adapters.benchmark_corpus import (  # noqa: E402
    CorpusError,
    CorpusMode,
    benchmark_profile,
    load_partition,
)
from radar_v08.adapters.model_profiles import (  # noqa: E402
    ModelProfileError,
    load_model_profiles,
)
from radar_v08.adapters.t051_ollama import (  # noqa: E402
    OLLAMA_BASE_URL,
    LoadedModel,
    ModelDetails,
    OllamaControl,
    OllamaProtocolError,
    OllamaUnavailable,
    ResourceProbe,
    ResourceSnapshot,
    T051Inference,
    T051NetworkRefused,
    resource_report,
)
from radar_v08.workflow.benchmark import (  # noqa: E402
    SCREENER_OUTPUT_SCHEMA,
    BenchmarkCase,
    BenchmarkProfile,
    CaseResult,
    GoldSource,
    ResourceReport,
    evaluate_case,
)
from radar_v08.workflow.t051_sequence import (  # noqa: E402
    BASELINE_MODEL,
    BENCHMARK_MODELS,
    OPTIONAL_MODEL,
    PROBE_MODELS,
    RESULTS_SCHEMA_VERSION,
    UNLOAD_WAIT_SECONDS,
    WARM_BLOCKS,
    Block,
    CaseRef,
    FixedLists,
    ResultsCorrupt,
    SealedCaseError,
    SequenceError,
    SequenceState,
    Step,
    build_fixed_lists,
    canonical_json,
    canonical_sha256,
    check_not_sealed,
    done_keys,
    freeze_differences,
    freeze_document,
    next_warmup_repetition,
    parse_results,
    plan,
    probe_observations,
    seconds_needed,
    validate_budget,
)

# Lock sha256 of benchmarks/oc1_screener_v1 as committed in 2aaab59 (T051b, D59 a).
EXPECTED_LOCK_SHA256 = "25589250ad9dbe0c12e5982de1ed2df3d98c44f27ebf803e124e0f83d386b15a"
FREEZE_FILE_NAME = "t051_freeze.json"
CANDIDATE_PROFILES_FILE_NAME = "t051_candidate_profiles.toml"
RUNTIME_PROFILE_ID = "screener-qwen3-14b"
RESULTS_DIR_NAME = "results"
RESULTS_FILE_NAME = "t051_results.jsonl"
# Files whose sha256 the freeze holds (D59 b). CRLF is read as LF so a checkout with
# core.autocrlf does not change a hash; any other byte change does.
FROZEN_FILES: tuple[tuple[str, str], ...] = (
    ("model_profiles_toml", "radar_v08/model_profiles.toml"),
    ("harness_and_parser_benchmark_py", "radar_v08/workflow/benchmark.py"),
    ("chat_request_and_reply_parser_local_inference_py", "radar_v08/adapters/local_inference.py"),
    ("screener_view_builder_oc1_corpus_builder_py", "radar_v08/adapters/oc1_corpus_builder.py"),
    ("corpus_loader_benchmark_corpus_py", "radar_v08/adapters/benchmark_corpus.py"),
    ("model_profiles_loader_py", "radar_v08/adapters/model_profiles.py"),
    ("runner_sequence_t051_sequence_py", "radar_v08/workflow/t051_sequence.py"),
    ("runner_adapter_t051_ollama_py", "radar_v08/adapters/t051_ollama.py"),
    ("runner_cli_run_t051_block_py", "scripts/run_t051_block.py"),
    ("candidate_profiles_toml", "benchmarks/oc1_screener_v1/t051_candidate_profiles.toml"),
)


class Status(Enum):
    COMPLETE = 0
    REFUSED = 2
    PARTIAL = 3
    BLOCKED = 4
    ABORTED = 5


class Stop(Exception):
    """Ends the invocation with a status and a reason (codes and ids only, never case text)."""

    def __init__(self, status: Status, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def file_sha256(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def frozen_hashes(repository_root: Path = REPOSITORY_ROOT) -> dict[str, str]:
    hashes = {name: file_sha256(repository_root / relative) for name, relative in FROZEN_FILES}
    hashes["screener_system_prompt"] = hashlib.sha256(oc1_corpus_builder.SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    hashes["screener_output_schema"] = canonical_sha256(json.loads(json.dumps(dict(SCREENER_OUTPUT_SCHEMA))))
    return hashes


def _labelled(case: BenchmarkCase) -> bool:
    return case.gold.source is not GoldSource.GOLD_UNAVAILABLE


def _ref(case: BenchmarkCase) -> CaseRef:
    return CaseRef(
        case_id=case.case_id,
        case_sha256=case.case_sha256,
        partition=case.partition.value,
        category=case.category.value,
        labelled=_labelled(case),
    )


@dataclass(frozen=True, slots=True)
class Corpus:
    corpus_id: str
    lock_sha256: str
    cases: Mapping[str, BenchmarkCase]
    refs: Mapping[str, CaseRef]
    lists: FixedLists


def load_corpus(corpus_root: Path, expected_lock_sha256: str) -> Corpus:
    development = load_partition(corpus_root, CorpusMode.DEVELOPMENT, expected_lock_sha256=expected_lock_sha256)
    holdout = load_partition(corpus_root, CorpusMode.HOLDOUT, expected_lock_sha256=expected_lock_sha256)
    cases = {case.case_id: case for case in (*development.cases, *holdout.cases)}
    refs = {case_id: _ref(case) for case_id, case in cases.items()}
    lists = build_fixed_lists(
        [refs[case.case_id] for case in development.cases], [refs[case.case_id] for case in holdout.cases]
    )
    return Corpus(holdout.corpus_id, holdout.lock_sha256, cases, refs, lists)


def build_freeze(corpus: Corpus, repository_root: Path = REPOSITORY_ROOT) -> dict[str, object]:
    return freeze_document(
        corpus_id=corpus.corpus_id,
        lock_sha256=corpus.lock_sha256,
        hashes=frozen_hashes(repository_root),
        lists=corpus.lists,
        cases=corpus.refs,
    )


def freeze_json(document: Mapping[str, object]) -> str:
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"


def _inside(path: Path, parent: Path) -> bool:
    child = os.path.normcase(os.fspath(path))
    root = os.path.normcase(os.fspath(parent))
    return child == root or child.startswith(root.rstrip("\\/") + os.sep)


_DRIVE_LETTER = re.compile(r"^[A-Za-z]:$")
_INSIDE_REPOSITORY = "--out is inside the repository; raw results live outside it (D59, D61)"


def check_out_dir(out: str | os.PathLike[str], repository_root: Path = REPOSITORY_ROOT) -> Path:
    r"""The raw-output directory: absolute after resolution, never inside the repository.

    Three checks, all before anything is created:

    * on Windows the resolved path must be a plain drive-letter path (``C:\...``): UNC
      (``\\host\share``, ``//host/share``, admin shares such as ``\\localhost\C$``) and
      device forms (``\\?\``, ``\\.\``) are refused, since they can name the repository
      under a different spelling;
    * the resolved path is not the repository root nor below it (case-insensitive text check);
    * no existing ancestor of the target (the target included) is the same directory as the
      repository root by volume and file id (``os.path.samefile``): this catches aliases the
      text check cannot see (subst drives, junctions, other shares). An ancestor that cannot be
      compared is refused, fail closed.
    """
    target = Path(out).resolve()
    if os.name == "nt" and not _DRIVE_LETTER.fullmatch(target.drive):
        raise Stop(Status.REFUSED, r"--out must be a plain drive-letter path (UNC, \\?\ and \\.\ forms are refused)")
    root = repository_root.resolve()
    if _inside(target, root):
        raise Stop(Status.REFUSED, _INSIDE_REPOSITORY)
    for ancestor in (target, *target.parents):
        try:
            if not ancestor.exists():
                continue
            same = os.path.samefile(ancestor, root)
        except (OSError, ValueError) as error:
            raise Stop(Status.REFUSED, f"--out cannot be compared with the repository: {error}") from error
        if same or _inside(ancestor.resolve(), root):
            raise Stop(Status.REFUSED, _INSIDE_REPOSITORY)
    if target.exists() and not target.is_dir():
        raise Stop(Status.REFUSED, "--out exists and is not a directory")
    return target


@dataclass
class Clock:
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass
class Runtime:
    """Everything the invocation touches, injectable for the tests (fake server, fake tools)."""

    base_url: str = OLLAMA_BASE_URL
    repository_root: Path = REPOSITORY_ROOT
    expected_lock_sha256: str = EXPECTED_LOCK_SHA256
    freeze_path: Path | None = None
    probe: ResourceProbe = field(default_factory=ResourceProbe)
    clock: Clock = field(default_factory=Clock)
    sleep: Callable[[float], None] = time.sleep


class ResultsFile:
    """Append-only JSONL. Never rewrites a byte: a truncated tail gets a newline and a marker."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> str:
        try:
            with open(self.path, "rb") as handle:
                return handle.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def append(self, record: Mapping[str, object]) -> None:
        line = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def end_open_line(self) -> None:
        """A complete record whose newline was cut: add only the newline, so the next append
        starts its own line instead of joining this one."""
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n")

    def close_truncated_tail(self, line_number: int, at: str) -> None:
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n")
        self.append({"record_type": "truncated_tail", "line": line_number, "seen_at_utc": at})


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _case_result_dict(result: CaseResult) -> dict[str, object]:
    return {
        "outcome": result.outcome.value,
        "failure_code": result.failure_code,
        "first_pass_schema_valid": result.first_pass_schema_valid,
        "abstain_expected": result.abstain_expected,
        "abstained": result.abstained,
        "classification": None if result.classification is None else result.classification.value,
        "cited_count": result.cited_count,
        "prompt_token_bound": result.prompt_token_bound,
        "input_budget_tokens": result.input_budget_tokens,
        "min_free_vram_gib": result.min_free_vram_gib,
        "min_free_ram_gib": result.min_free_ram_gib,
        "gold_status": result.gold_status.value,
        "rationale_unverified": result.rationale_unverified,
    }


class _Reporter:
    """The harness ``ResourceReporter``: measures right after the call, keeps the snapshot."""

    def __init__(self, invocation: Invocation, model: str, inference: T051Inference) -> None:
        self._invocation = invocation
        self._model = model
        self._inference = inference
        self.snapshot: ResourceSnapshot | None = None

    def last_call_resources(self) -> ResourceReport | None:
        oom = self._inference.last.oom if self._inference.last is not None else False
        self.snapshot = self._invocation.measure(self._model, oom)
        return resource_report(self.snapshot)


class Invocation:
    def __init__(self, corpus_root: Path, out: Path, budget_seconds: int, include_optional: bool, runtime: Runtime) -> None:
        self.corpus_root = corpus_root
        self.out = out
        self.budget = budget_seconds
        self.include_optional = include_optional
        self.runtime = runtime
        self.started = runtime.clock.monotonic()
        self.invocation_id = _iso(runtime.clock.now())
        self.control: OllamaControl | None = None
        self.loaded: str | None = None  # the benchmark model this invocation loaded, if any
        self.calls = 0
        self.records: list[Mapping[str, object]] = []
        self.truncated_line: int | None = None
        self.unload_confirmed: bool | None = None
        self.freeze_sha256 = ""
        self.freeze_hashes: Mapping[str, str] = {}
        self.digests: dict[str, str] = {}
        self.details: dict[str, ModelDetails] = {}
        self.progress_note: dict[str, object] = {}

    # -- helpers --------------------------------------------------------------------------------

    def remaining(self) -> float:
        return self.budget - (self.runtime.clock.monotonic() - self.started)

    def ctl(self) -> OllamaControl:
        assert self.control is not None
        return self.control

    def measure(self, model: str, oom: bool) -> ResourceSnapshot:
        loaded: LoadedModel | None = None
        try:
            loaded = next((item for item in self.ctl().ps() if item.name == model), None)
        except (OllamaUnavailable, OllamaProtocolError):
            loaded = None
        return self.runtime.probe.snapshot(loaded, oom)

    def _stamp(self, record: dict[str, object]) -> dict[str, object]:
        record["schema"] = RESULTS_SCHEMA_VERSION
        record["invocation_id"] = self.invocation_id
        record["freeze_sha256"] = self.freeze_sha256
        record["freeze_hashes"] = dict(self.freeze_hashes)
        return record

    def append(self, results: ResultsFile, record: dict[str, object]) -> None:
        results.append(record)
        self.records.append(record)

    # -- phases ----------------------------------------------------------------------------------

    def load(self, results: ResultsFile) -> Corpus:
        try:
            corpus = load_corpus(self.corpus_root, self.runtime.expected_lock_sha256)
        except (CorpusError, SequenceError) as error:
            raise Stop(Status.ABORTED, f"corpus refused: {error}") from None
        freeze_path = self.runtime.freeze_path or self.corpus_root / FREEZE_FILE_NAME
        expected = build_freeze(corpus, self.runtime.repository_root)
        try:
            committed = json.loads(freeze_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Stop(Status.ABORTED, f"freeze file missing or unreadable: {freeze_path.name}") from None
        differences = freeze_differences(expected, committed)
        if differences:
            raise Stop(Status.ABORTED, "freeze differs from the committed one: " + ", ".join(differences))
        self.freeze_sha256 = canonical_sha256(expected)
        hashes = expected["hashes"]
        assert isinstance(hashes, dict)
        self.freeze_hashes = hashes
        try:
            text = results.read()
            parsed = parse_results(text)
            done_keys(parsed.records)
        except ResultsCorrupt as error:
            raise Stop(Status.ABORTED, f"results file refused: {error}") from None
        foreign = [
            record for record in parsed.records
            if record.get("record_type") in ("call", "preflight") and record.get("freeze_sha256") != self.freeze_sha256
        ]
        if foreign:
            raise Stop(Status.ABORTED, f"{len(foreign)} records were written under another freeze")
        self.records = list(parsed.records)
        if parsed.truncated_line is not None:
            self.truncated_line = parsed.truncated_line
            results.close_truncated_tail(parsed.truncated_line, _iso(self.runtime.clock.now()))
        elif text and not text.endswith("\n"):
            results.end_open_line()
        return corpus

    def preflight(self, results: ResultsFile) -> None:
        control = self.ctl()
        try:
            installed = control.tags()
            busy = control.ps()
        except OllamaUnavailable as error:
            raise Stop(Status.BLOCKED, f"Ollama is not answering on {control.base_url}: {error}") from None
        if busy:
            names = ", ".join(sorted(item.name for item in busy))
            raise Stop(Status.BLOCKED, f"/api/ps is not empty ({names}); nothing was unloaded")
        recorded = {
            str(record.get("model")): record for record in self.records if record.get("record_type") == "preflight"
        }
        checked = (*PROBE_MODELS, OPTIONAL_MODEL) if self.include_optional else PROBE_MODELS
        for model in BENCHMARK_MODELS:
            entry = installed.get(model)
            digest = entry.digest if entry is not None else "not_installed"
            previous = recorded.get(model)
            if previous is not None:
                if model in checked and previous.get("digest") != digest:
                    raise Stop(Status.ABORTED, f"digest of {model} differs from its preflight")
                if entry is not None:
                    self.digests[model] = digest
                    self.details[model] = self._details_from(previous, model)
                continue
            details = control.show(model) if entry is not None else None
            record = self._stamp(
                {
                    "record_type": "preflight",
                    "model": model,
                    "digest": digest,
                    "installed": entry is not None,
                    "details": None if details is None else details.as_dict(),
                    "at_utc": _iso(self.runtime.clock.now()),
                }
            )
            self.append(results, record)
            if entry is not None and details is not None:
                self.digests[model] = digest
                self.details[model] = details
        if BASELINE_MODEL not in self.digests:
            raise Stop(Status.BLOCKED, f"{BASELINE_MODEL} is not installed; nothing is downloaded")

    @staticmethod
    def _details_from(record: Mapping[str, object], model: str) -> ModelDetails:
        details = record.get("details")
        details = details if isinstance(details, Mapping) else {}
        capabilities = details.get("capabilities")
        return ModelDetails(
            name=model,
            quantization=None,
            context_length=None,
            parameter_size=None,
            family=None,
            format=None,
            parameters=None,
            capabilities=tuple(item for item in capabilities if isinstance(item, str))
            if isinstance(capabilities, list)
            else (),
        )

    def profiles(self) -> dict[str, BenchmarkProfile]:
        try:
            runtime = load_model_profiles(self.runtime.repository_root / "radar_v08" / "model_profiles.toml")
            candidates = load_model_profiles(self.corpus_root / CANDIDATE_PROFILES_FILE_NAME)
        except ModelProfileError as error:
            raise Stop(Status.ABORTED, f"profile refused: {error}") from None
        found = {BASELINE_MODEL: benchmark_profile(runtime.get(RUNTIME_PROFILE_ID))}
        for profile in candidates.profiles.values():
            found[profile.model] = benchmark_profile(profile)
        missing = [model for model in BENCHMARK_MODELS if model not in found]
        if missing or found[BASELINE_MODEL].inference.model != BASELINE_MODEL:
            raise Stop(Status.ABORTED, "a benchmark model has no profile: " + ", ".join(missing))
        return found

    def ensure_only(self, allowed: str | None) -> None:
        """Before a call: nothing but the benchmark model this invocation loaded may be in memory."""
        try:
            loaded = self.ctl().ps()
        except OllamaUnavailable as error:
            raise Stop(Status.BLOCKED, f"Ollama stopped answering: {error}") from None
        foreign = sorted(item.name for item in loaded if item.name != allowed)
        if foreign:
            raise Stop(Status.BLOCKED, "another model is loaded (" + ", ".join(foreign) + "); it was not unloaded")

    def unload_current(self) -> bool:
        if self.loaded is None:
            return True
        model = self.loaded
        try:
            confirmed = self.ctl().unload(model, UNLOAD_WAIT_SECONDS)
        except OllamaUnavailable as error:
            raise Stop(Status.BLOCKED, f"Ollama stopped answering while unloading {model}: {error}") from None
        if not confirmed:
            raise Stop(Status.BLOCKED, f"{model} was still loaded {UNLOAD_WAIT_SECONDS} s after the unload request")
        self.loaded = None
        return True

    def call(
        self,
        results: ResultsFile,
        corpus: Corpus,
        profiles: Mapping[str, BenchmarkProfile],
        step: Step,
        *,
        warmup_repetition: int | None = None,
    ) -> None:
        case_id = corpus.lists.warmup[0] if warmup_repetition is not None else step.case_id
        check_not_sealed(case_id, corpus.lists)  # D59 f: the last guard before anything is sent
        case = corpus.cases[case_id]
        if warmup_repetition is None and step.block is Block.PROBE and not _labelled(case):
            raise SealedCaseError(f"probe case {case_id} has no gold")
        model = step.model
        digest = self.digests[model]
        block = Block.WARMUP if warmup_repetition is not None else step.block
        repetition = warmup_repetition if warmup_repetition is not None else step.repetition
        before: ResourceSnapshot | None = None
        if block is Block.COLD:
            self.unload_current()
            self.ensure_only(None)
            before = self.runtime.probe.snapshot(None, False)
        else:
            self.ensure_only(self.loaded)
        inference = T051Inference(self.ctl(), {name: details.capabilities for name, details in self.details.items()})
        reporter = _Reporter(self, model, inference)
        started_at = _iso(self.runtime.clock.now())
        result = evaluate_case(case, profiles[model], inference, reporter)
        self.calls += 1 if inference.last is not None else 0
        if inference.last is not None:
            self.loaded = model  # a model call may have loaded it, whatever it returned
        telemetry = inference.last
        snapshot = reporter.snapshot
        record = self._stamp(
            {
                "record_type": "call",
                "key": {"block": block.value, "model": model, "digest": digest, "case_id": case_id, "repetition": repetition},
                "warmup": block is Block.WARMUP,
                "for_block": step.block.value,
                "partition": case.partition.value,
                "category": case.category.value,
                "case_sha256": case.case_sha256,
                "labelled": _labelled(case),
                "profile_id": profiles[model].inference.profile_id,
                "started_at_utc": started_at,
                "request": None if telemetry is None else dict(telemetry.request),
                "http": None if telemetry is None else {"status": telemetry.status, "failure": telemetry.failure},
                "timing": None
                if telemetry is None
                else {"wall_seconds": telemetry.wall_seconds, **dict(telemetry.envelope_metrics)},
                "response": None
                if telemetry is None
                else {
                    "body": telemetry.response_body,
                    "body_truncated": telemetry.response_body_truncated,
                    "body_sha256": telemetry.response_body_sha256,
                },
                "harness": _case_result_dict(result),
                "resources": None if snapshot is None else snapshot.as_dict(),
                "resources_before_load": None if before is None else before.as_dict(),
                "repair": {"policy": "none", "attempted": False},
            }
        )
        self.append(results, record)
        if snapshot is not None and snapshot.ollama_ps_digest is not None:
            observed = snapshot.ollama_ps_digest
            if not observed or not (digest.startswith(observed) or observed.startswith(digest)):
                raise Stop(Status.ABORTED, f"digest of the loaded {model} differs from its preflight")

    def run_sequence(self, results: ResultsFile, corpus: Corpus, profiles: Mapping[str, BenchmarkProfile]) -> Status:
        while True:
            done = done_keys(self.records)
            state = SequenceState(
                lists=corpus.lists,
                digests=self.digests,
                done=done,
                probe_observations=probe_observations(self.records, self.digests),
                include_optional=self.include_optional,
            )
            progress = plan(state)
            step = progress.next_step
            if step is None:
                return Status.COMPLETE
            if self.loaded is not None and self.loaded != step.model:
                if self.remaining() < UNLOAD_WAIT_SECONDS + 5:
                    return Status.PARTIAL
                self.unload_current()
            warmup = step.block in WARM_BLOCKS and self.loaded != step.model
            if self.remaining() < seconds_needed(step, warmup):
                return Status.PARTIAL
            if warmup:
                repetition = next_warmup_repetition(done, step.model, self.digests[step.model])
                self.call(results, corpus, profiles, step, warmup_repetition=repetition)
            self.call(results, corpus, profiles, step)

    def summary(self, status: Status, reason: str, corpus: Corpus | None) -> dict[str, object]:
        groups: list[dict[str, object]] = []
        notes: list[str] = []
        selection: dict[str, object] | None = None
        if corpus is not None and BASELINE_MODEL in self.digests:
            try:
                progress = plan(
                    SequenceState(
                        lists=corpus.lists,
                        digests=self.digests,
                        done=done_keys(self.records),
                        probe_observations=probe_observations(self.records, self.digests),
                        include_optional=self.include_optional,
                    )
                )
            except (SequenceError, ResultsCorrupt):
                progress = None
            if progress is not None:
                groups = [
                    {"block": group.block, "model": group.model, "done": group.done, "total": group.total}
                    for group in progress.groups
                ]
                notes = list(progress.notes)
                if progress.selection is not None:
                    selection = {
                        "candidates": list(progress.selection.candidates),
                        "rule": progress.selection.rule,
                        "probe": [
                            {
                                "model": item.model,
                                "calls": item.calls,
                                "schema_valid": item.schema_valid,
                                "p95_seconds": item.p95_seconds,
                                "peak_model_memory_bytes": item.peak_model_memory_bytes,
                                "disqualified": list(item.disqualified),
                            }
                            for item in progress.selection.stats
                        ],
                    }
                if status is Status.PARTIAL and progress.complete:
                    status = Status.COMPLETE
        return {
            "record_type": "invocation",
            "schema": RESULTS_SCHEMA_VERSION,
            "invocation_id": self.invocation_id,
            "status": status.name,
            "exit_code": status.value,
            "reason": reason,
            "budget_seconds": self.budget,
            "elapsed_seconds": round(self.runtime.clock.monotonic() - self.started, 3),
            "model_calls_this_invocation": self.calls,
            "truncated_line_reported": self.truncated_line,
            "unload_at_end_confirmed": self.unload_confirmed,
            "freeze_sha256": self.freeze_sha256 or None,
            "installed_digests": dict(sorted(self.digests.items())),
            "steps": groups,
            "pending": [group for group in groups if group["done"] != group["total"]],
            "selection": selection,
            "notes": notes,
        }


def run_invocation(
    corpus: str | os.PathLike[str],
    out: str | os.PathLike[str],
    budget_seconds: int,
    *,
    include_optional: bool = False,
    runtime: Runtime | None = None,
) -> tuple[Status, dict[str, object]]:
    """One invocation. Never raises for an expected stop: returns the status and the summary."""
    runtime = runtime if runtime is not None else Runtime()
    try:
        budget = validate_budget(budget_seconds)
    except SequenceError as error:
        return Status.REFUSED, {"status": Status.REFUSED.name, "exit_code": Status.REFUSED.value, "reason": str(error)}
    invocation = Invocation(Path(corpus), Path(out), budget, include_optional, runtime)
    try:
        target = check_out_dir(out, runtime.repository_root)
        try:
            (target / RESULTS_DIR_NAME).mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise Stop(Status.REFUSED, f"--out cannot be created: {error}") from error
    except Stop as stop:
        return stop.status, {"status": stop.status.name, "exit_code": stop.status.value, "reason": stop.reason}
    results = ResultsFile(target / RESULTS_DIR_NAME / RESULTS_FILE_NAME)
    corpus_data: Corpus | None = None
    status = Status.PARTIAL
    reason = ""
    try:
        corpus_data = invocation.load(results)
        profiles = invocation.profiles()
        invocation.control = OllamaControl(runtime.base_url, monotonic=runtime.clock.monotonic, sleep=runtime.sleep)
        invocation.preflight(results)
        status = invocation.run_sequence(results, corpus_data, profiles)
        reason = "every step of the sequence is recorded" if status is Status.COMPLETE else "budget reached"
    except Stop as stop:
        status, reason = stop.status, stop.reason
    except SealedCaseError as error:
        status, reason = Status.ABORTED, str(error)
    except T051NetworkRefused as error:
        status, reason = Status.ABORTED, str(error)
    except OllamaUnavailable as error:
        status, reason = Status.BLOCKED, f"Ollama stopped answering: {error}"
    except OllamaProtocolError as error:
        status, reason = Status.BLOCKED, f"unexpected Ollama reply: {error}"
    finally:
        if invocation.control is not None:
            if invocation.loaded is not None:
                try:
                    invocation.unload_confirmed = invocation.unload_current()
                except Stop as stop:
                    invocation.unload_confirmed = False
                    if status in (Status.COMPLETE, Status.PARTIAL):
                        status, reason = stop.status, stop.reason
            invocation.control.close()
    summary = invocation.summary(status, reason, corpus_data)
    status = Status[str(summary["status"])]
    if invocation.freeze_sha256:
        results.append(summary)
    return status, summary


def write_freeze(corpus: str | os.PathLike[str], runtime: Runtime | None = None) -> tuple[Status, str]:
    """Write ``<corpus>/t051_freeze.json`` (no network). Never replaces a different freeze."""
    runtime = runtime if runtime is not None else Runtime()
    root = Path(corpus)
    try:
        corpus_data = load_corpus(root, runtime.expected_lock_sha256)
    except (CorpusError, SequenceError) as error:
        return Status.ABORTED, f"corpus refused: {error}"
    text = freeze_json(build_freeze(corpus_data, runtime.repository_root))
    path = runtime.freeze_path or root / FREEZE_FILE_NAME
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return Status.COMPLETE, f"{path.name} already up to date sha256 {canonical_sha256(json.loads(text))}"
        return Status.REFUSED, f"{path.name} exists with other content; a committed freeze is never replaced"
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return Status.COMPLETE, f"{path.name} written; canonical sha256 {canonical_sha256(json.loads(text))}"


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One resumable invocation of the T051 Screener benchmark.")
    parser.add_argument("--corpus", required=True, help="locked corpus directory (benchmarks/oc1_screener_v1)")
    parser.add_argument("--out", help="raw results directory outside the repository (<t051-workdir>)")
    parser.add_argument("--budget-seconds", type=int, help="wall-clock budget of this invocation, 1..540")
    parser.add_argument("--include-optional", action="store_true", help=f"also run {OPTIONAL_MODEL} after every mandatory step")
    parser.add_argument("--write-freeze", action="store_true", help="write <corpus>/t051_freeze.json and exit (no network)")
    arguments = parser.parse_args(argv)
    if not arguments.write_freeze and (arguments.out is None or arguments.budget_seconds is None):
        parser.error("--out and --budget-seconds are required")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    if arguments.write_freeze:
        status, message = write_freeze(arguments.corpus)
        print(f"{status.name}: {message}")
        return status.value
    status, summary = run_invocation(
        arguments.corpus, arguments.out, arguments.budget_seconds, include_optional=arguments.include_optional
    )
    print(canonical_json(summary) if status is Status.REFUSED else json.dumps(summary, indent=2, sort_keys=True))
    return status.value


if __name__ == "__main__":
    raise SystemExit(main())
