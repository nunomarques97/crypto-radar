"""T050b: locked benchmark corpus and OC-1 benchmark harness (fake adapter only).

D31/D33/D38: the only repository files read are the synthetic fixtures under
``tests/fixtures/benchmark_corpus``, the two harness sources (inspected as text) and the
T4 snapshot ``docs/forja/archive/R-20260919-7c62-T4-sobras-codigo-ORIGINAL.patch`` (read
only, to prove the refused-phrase tuples only grew); every mutation happens on a copy in a temporary
directory, and every report is written to a temporary directory. The harness runs only
against the in-process ``FakeModel`` below: no model is run or downloaded, no request
reaches Ollama, and the tests prove that ``radar_v08/workflow/benchmark.py`` cannot reach
the network (no ``requests``/``socket``/``OllamaLocalInference`` import, no ``__main__``,
no new CLI).

The fixture corpus is SYNTHETIC: it proves the harness, it is not the 300-case corpus of
OPERATING_CONTRACTS.md section 6, and T051 stays blocked on that corpus.
"""

import ast
import builtins
import dataclasses
import inspect
import json
import math
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(REPOSITORY_ROOT))

from radar_v08.adapters import benchmark_corpus as bc  # noqa: E402
from radar_v08.adapters.benchmark_corpus import (  # noqa: E402
    CorpusError,
    CorpusErrorCode,
    CorpusMode,
    benchmark_profile,
    canonical_sha256,
    load_partition,
    prompt_fingerprint,
    write_report,
)
from radar_v08.adapters.model_profiles import (  # noqa: E402
    ModelProfileError,
    load_model_profiles,
)
from radar_v08.workflow import benchmark as bm  # noqa: E402
from radar_v08.workflow.benchmark import (  # noqa: E402
    SCREENER_OUTPUT_SCHEMA,
    BenchmarkCase,
    BenchmarkProfile,
    BlockReason,
    CaseCategory,
    CaseOutcome,
    CorpusPartition,
    DeterministicGold,
    GoldSource,
    GoldStatus,
    LockedPartition,
    PromotionStatus,
    ResourceReport,
    invades_risk_domain,
    parse_screener_answer,
    prompt_token_bound,
    report_json,
    report_sha256,
    run_benchmark,
)
from radar_v08.workflow.scheduler import Role  # noqa: E402
from radar_v08.workflow.worker import (  # noqa: E402
    InferenceCall,
    InferenceFailed,
    InferenceFailure,
    InferenceProfile,
    InferenceReply,
)

CORPUS = REPOSITORY_ROOT / "tests" / "fixtures" / "benchmark_corpus"
# Pinned lock of the synthetic corpus. Changing any case or the lock changes this value.
LOCK_SHA256 = "3f1adce74d1d4a83a074e938ccdec687575597630b68d3f3bf5e3efe9ea56246"
BENCHMARK_SOURCE = REPOSITORY_ROOT / "radar_v08" / "workflow" / "benchmark.py"
CORPUS_SOURCE = REPOSITORY_ROOT / "radar_v08" / "adapters" / "benchmark_corpus.py"
OK_RESOURCES = ResourceReport(oom=False, min_free_vram_gib=3.0, min_free_ram_gib=12.0)


def default_profile() -> BenchmarkProfile:
    """The T050a default profile from the versioned radar_v08/model_profiles.toml."""
    profiles = load_model_profiles()
    return benchmark_profile(profiles.get(profiles.default_profile_id))


def answer(case: BenchmarkCase, **overrides: object) -> dict[str, object]:
    """A schema-valid answer that matches the case's deterministic gold."""
    payload: dict[str, object] = {
        "abstain": case.gold.abstain_expected,
        "classification": case.category.value,
        "cited_evidence_ids": list(case.evidence_ids[:1]),
        "rationale": "The supplied synthetic evidence supports this classification.",
    }
    payload.update(overrides)
    return payload


class FakeModel:
    """In-process stand-in for the LocalInference and ResourceReporter ports.

    ``script`` maps a case's user text to ``(result, resources)``; ``result`` is an
    ``InferenceReply``/``InferenceFailed`` or an exception to raise.
    """

    def __init__(self, script: Mapping[str, tuple[object, object]]) -> None:
        self.script = dict(script)
        self.calls: list[InferenceCall] = []
        self._last: object = None

    def infer(self, call, cancel):  # noqa: ANN001 - duck-typed port
        self.calls.append(call)
        assert cancel.is_set() is False
        result, self._last = self.script[call.user]
        if isinstance(result, BaseException):
            raise result
        return result

    def last_call_resources(self):  # noqa: ANN201
        return self._last


def reply(payload: object, output_tokens: int | None = 120) -> InferenceReply:
    return InferenceReply(payload=payload, prompt_tokens=900, output_tokens=output_tokens)  # type: ignore[arg-type]


def valid_script(partition: LockedPartition) -> dict[str, tuple[object, object]]:
    return {case.user: (reply(answer(case)), OK_RESOURCES) for case in partition.cases}


def holdout() -> LockedPartition:
    return load_partition(CORPUS, CorpusMode.HOLDOUT, expected_lock_sha256=LOCK_SHA256)


def development() -> LockedPartition:
    return load_partition(CORPUS, CorpusMode.DEVELOPMENT, expected_lock_sha256=LOCK_SHA256)


def by_id(partition: LockedPartition, case_id: str) -> BenchmarkCase:
    return next(case for case in partition.cases if case.case_id == case_id)


class CorpusCopy:
    """A writable copy of the fixture corpus in a temporary directory."""

    def __init__(self, test: unittest.TestCase) -> None:
        directory = tempfile.TemporaryDirectory(prefix="t050b-corpus-")
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "corpus"
        shutil.copytree(CORPUS, self.root)

    def case_path(self, partition: str, case_id: str) -> Path:
        return self.root / partition / f"{case_id}.json"

    def read(self, partition: str, case_id: str) -> dict[str, object]:
        return json.loads(self.case_path(partition, case_id).read_text(encoding="utf-8"))

    def write(self, partition: str, case_id: str, document: object) -> None:
        self.case_path(partition, case_id).write_text(json.dumps(document, indent=2), encoding="utf-8")

    def lock(self) -> dict[str, object]:
        return json.loads((self.root / "lock.json").read_text(encoding="utf-8"))

    def write_lock(self, lock: object) -> None:
        (self.root / "lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")

    def relock(self) -> str:
        """Rebuild every hash from the files on disk (what someone re-locking would do)."""
        lock = self.lock()
        for partition in ("development", "holdout"):
            table = {}
            for path in sorted((self.root / partition).glob("*.json")):
                case = json.loads(path.read_text(encoding="utf-8"))
                prompt = case["prompt"]
                table[case["case_id"]] = {
                    "sha256": canonical_sha256(case),
                    "prompt_sha256": prompt_fingerprint(prompt["system"], prompt["user"], case["evidence_ids"]),
                }
            lock["partitions"][partition] = {"partition_sha256": canonical_sha256(table), "cases": table}
        self.write_lock(lock)
        return canonical_sha256(lock)

    def load(self, mode: CorpusMode, lock_sha256: str = LOCK_SHA256) -> LockedPartition:
        return load_partition(self.root, mode, expected_lock_sha256=lock_sha256)


# -- corpus loader -----------------------------------------------------------------------------


class TestFixtureCorpus(unittest.TestCase):
    def test_development_mode_returns_only_development_cases(self) -> None:
        partition = development()
        self.assertIs(partition.partition, CorpusPartition.DEVELOPMENT)
        self.assertEqual(
            [case.case_id for case in partition.cases],
            ["dev-conflict-001", "dev-insufficient-001", "dev-invalid-stale-001", "dev-no-edge-001", "dev-positive-001"],
        )
        self.assertTrue(all(case.partition is CorpusPartition.DEVELOPMENT for case in partition.cases))
        self.assertEqual(partition.corpus_id, "synthetic-harness-v1")
        self.assertEqual(partition.lock_sha256, LOCK_SHA256)
        self.assertTrue(partition.synthetic)

    def test_holdout_has_every_oc1_category_and_deterministic_gold(self) -> None:
        partition = holdout()
        self.assertIs(partition.partition, CorpusPartition.HOLDOUT)
        self.assertEqual(len(partition.cases), 6)
        self.assertEqual({case.category for case in partition.cases}, set(CaseCategory))
        for case in partition.cases:
            self.assertIs(case.gold.source, GoldSource.DETERMINISTIC_FIXTURE)
            self.assertIs(case.role, Role.SCREENER)
            self.assertEqual(case.case_sha256, canonical_sha256(json.loads((CORPUS / "holdout" / f"{case.case_id}.json").read_text("utf-8"))))
        abstaining = {case.case_id for case in partition.cases if case.gold.abstain_expected}
        self.assertEqual(abstaining, {"hold-invalid-stale-001", "hold-insufficient-001"})

    def test_lock_has_a_sha256_per_case_and_per_partition(self) -> None:
        lock = json.loads((CORPUS / "lock.json").read_text(encoding="utf-8"))
        self.assertEqual(canonical_sha256(lock), LOCK_SHA256)
        self.assertIs(lock["synthetic"], True)
        for name, count in (("development", 5), ("holdout", 6)):
            table = lock["partitions"][name]
            self.assertEqual(len(table["cases"]), count)
            self.assertEqual(canonical_sha256(table["cases"]), table["partition_sha256"])
            self.assertEqual({path.stem for path in (CORPUS / name).iterdir()}, set(table["cases"]))

    def test_line_endings_do_not_change_hashes(self) -> None:
        copy = CorpusCopy(self)
        path = copy.case_path("holdout", "hold-positive-001")
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertEqual(len(copy.load(CorpusMode.HOLDOUT).cases), 6)


class TestLockRejections(unittest.TestCase):
    def assertRefused(self, code: CorpusErrorCode, copy: CorpusCopy, mode: CorpusMode, lock: str = LOCK_SHA256) -> None:
        with self.assertRaises(CorpusError) as caught:
            copy.load(mode, lock)
        self.assertIs(caught.exception.code, code)

    def test_changed_development_case_hash(self) -> None:
        edits = {
            "user text": lambda doc: doc["prompt"].__setitem__("user", doc["prompt"]["user"] + " "),
            "evidence ids": lambda doc: doc["evidence_ids"].reverse(),
            "category": lambda doc: doc.__setitem__("category", "admissible_no_edge"),
        }
        for name, edit in edits.items():
            with self.subTest(name):
                copy = CorpusCopy(self)
                document = copy.read("development", "dev-conflict-001")
                edit(document)
                copy.write("development", "dev-conflict-001", document)
                for mode in CorpusMode:
                    self.assertRefused(CorpusErrorCode.CASE_HASH_MISMATCH, copy, mode)

    def test_case_that_changed_partition(self) -> None:
        with self.subTest("development case moved into holdout"):
            copy = CorpusCopy(self)
            shutil.move(copy.case_path("development", "dev-positive-001"), copy.root / "holdout")
            self.assertRefused(CorpusErrorCode.PARTITION_CHANGED, copy, CorpusMode.HOLDOUT)
        with self.subTest("holdout case moved into development"):
            copy = CorpusCopy(self)
            shutil.move(copy.case_path("holdout", "hold-positive-001"), copy.root / "development")
            self.assertRefused(CorpusErrorCode.PARTITION_CHANGED, copy, CorpusMode.DEVELOPMENT)
            self.assertRefused(CorpusErrorCode.PARTITION_CHANGED, copy, CorpusMode.HOLDOUT)
        with self.subTest("partition label edited in place"):
            copy = CorpusCopy(self)
            document = copy.read("development", "dev-positive-001")
            document["partition"] = "holdout"
            copy.write("development", "dev-positive-001", document)
            self.assertRefused(CorpusErrorCode.PARTITION_CHANGED, copy, CorpusMode.DEVELOPMENT)
        with self.subTest("case moved and relocked"):
            copy = CorpusCopy(self)
            document = copy.read("holdout", "hold-positive-002")
            document["partition"] = "development"
            copy.case_path("holdout", "hold-positive-002").unlink()
            copy.write("development", "hold-positive-002", document)
            relocked = copy.relock()
            self.assertRefused(CorpusErrorCode.LOCK_CHANGED, copy, CorpusMode.DEVELOPMENT)
            self.assertNotEqual(relocked, LOCK_SHA256)

    def test_edited_holdout(self) -> None:
        with self.subTest("content edited"):
            copy = CorpusCopy(self)
            document = copy.read("holdout", "hold-no-edge-001")
            document["prompt"]["user"] = document["prompt"]["user"].replace("0.4", "0.5")
            copy.write("holdout", "hold-no-edge-001", document)
            self.assertRefused(CorpusErrorCode.HOLDOUT_EDITED, copy, CorpusMode.HOLDOUT)
        with self.subTest("case removed"):
            copy = CorpusCopy(self)
            copy.case_path("holdout", "hold-positive-002").unlink()
            self.assertRefused(CorpusErrorCode.HOLDOUT_EDITED, copy, CorpusMode.HOLDOUT)
        with self.subTest("case added"):
            copy = CorpusCopy(self)
            document = copy.read("holdout", "hold-positive-002")
            document["case_id"] = "hold-positive-003"
            copy.write("holdout", "hold-positive-003", document)
            self.assertRefused(CorpusErrorCode.HOLDOUT_EDITED, copy, CorpusMode.HOLDOUT)
        with self.subTest("holdout table in the lock edited"):
            copy = CorpusCopy(self)
            lock = copy.lock()
            del lock["partitions"]["holdout"]["cases"]["hold-positive-002"]
            copy.write_lock(lock)
            self.assertRefused(CorpusErrorCode.HOLDOUT_EDITED, copy, CorpusMode.HOLDOUT)
            self.assertRefused(CorpusErrorCode.HOLDOUT_EDITED, copy, CorpusMode.DEVELOPMENT)
        with self.subTest("holdout edited and relocked"):
            copy = CorpusCopy(self)
            document = copy.read("holdout", "hold-no-edge-001")
            document["prompt"]["user"] += " (tuned)"
            copy.write("holdout", "hold-no-edge-001", document)
            copy.relock()
            self.assertRefused(CorpusErrorCode.LOCK_CHANGED, copy, CorpusMode.HOLDOUT)
            self.assertRefused(CorpusErrorCode.LOCK_CHANGED, copy, CorpusMode.DEVELOPMENT)

    def test_duplicate_case(self) -> None:
        with self.subTest("same case id locked in both partitions"):
            copy = CorpusCopy(self)
            lock = copy.lock()
            lock["partitions"]["development"]["cases"]["hold-positive-001"] = lock["partitions"]["holdout"]["cases"]["hold-positive-001"]
            copy.write_lock(lock)
            self.assertRefused(CorpusErrorCode.DUPLICATE_CASE, copy, CorpusMode.DEVELOPMENT)
        with self.subTest("holdout prompt copied into development and relocked"):
            copy = CorpusCopy(self)
            document = copy.read("holdout", "hold-positive-001")
            document.update(case_id="dev-copy-001", partition="development")
            copy.write("development", "dev-copy-001", document)
            relocked = copy.relock()
            self.assertRefused(CorpusErrorCode.DUPLICATE_CASE, copy, CorpusMode.DEVELOPMENT, relocked)
        with self.subTest("two development cases with the same prompt"):
            copy = CorpusCopy(self)
            document = copy.read("development", "dev-positive-001")
            document["case_id"] = "dev-positive-002"
            copy.write("development", "dev-positive-002", document)
            relocked = copy.relock()
            self.assertRefused(CorpusErrorCode.DUPLICATE_CASE, copy, CorpusMode.DEVELOPMENT, relocked)

    def test_unlocked_missing_and_unexpected_entries(self) -> None:
        with self.subTest("file not in the lock"):
            copy = CorpusCopy(self)
            shutil.copy(copy.case_path("development", "dev-positive-001"), copy.root / "development" / "dev-extra-001.json")
            self.assertRefused(CorpusErrorCode.UNLOCKED_CASE, copy, CorpusMode.DEVELOPMENT)
        with self.subTest("locked file missing"):
            copy = CorpusCopy(self)
            copy.case_path("development", "dev-positive-001").unlink()
            self.assertRefused(CorpusErrorCode.MISSING_CASE, copy, CorpusMode.DEVELOPMENT)
        for name in ("notes.txt", "Dev-Upper.json", "sub"):
            with self.subTest(f"unexpected entry {name}"):
                copy = CorpusCopy(self)
                target = copy.root / "development" / name
                if name == "sub":
                    target.mkdir()
                else:
                    target.write_text("{}", encoding="utf-8")
                self.assertRefused(CorpusErrorCode.UNEXPECTED_ENTRY, copy, CorpusMode.DEVELOPMENT)
        with self.subTest("case id does not match the file name"):
            copy = CorpusCopy(self)
            document = copy.read("development", "dev-positive-001")
            document["case_id"] = "dev-no-edge-001"
            copy.write("development", "dev-positive-001", document)
            self.assertRefused(CorpusErrorCode.CASE_INVALID, copy, CorpusMode.DEVELOPMENT)

    def test_lock_file_problems(self) -> None:
        with self.subTest("missing"):
            copy = CorpusCopy(self)
            (copy.root / "lock.json").unlink()
            self.assertRefused(CorpusErrorCode.LOCK_MISSING, copy, CorpusMode.DEVELOPMENT)
        with self.subTest("pinned sha differs"):
            copy = CorpusCopy(self)
            self.assertRefused(CorpusErrorCode.LOCK_CHANGED, copy, CorpusMode.DEVELOPMENT, "0" * 64)
        for bad in ("", "ABC", LOCK_SHA256.upper(), LOCK_SHA256 + "0", None):
            with self.subTest(f"malformed pinned sha {bad!r}"):
                with self.assertRaises(CorpusError) as caught:
                    load_partition(CORPUS, CorpusMode.DEVELOPMENT, expected_lock_sha256=bad)  # type: ignore[arg-type]
                self.assertIs(caught.exception.code, CorpusErrorCode.LOCK_INVALID)
        with self.subTest("unknown key"):
            copy = CorpusCopy(self)
            lock = copy.lock()
            lock["tuned_on_holdout"] = False
            copy.write_lock(lock)
            self.assertRefused(CorpusErrorCode.LOCK_INVALID, copy, CorpusMode.DEVELOPMENT)
        with self.subTest("duplicate JSON key"):
            copy = CorpusCopy(self)
            text = (copy.root / "lock.json").read_text(encoding="utf-8")
            (copy.root / "lock.json").write_text(text.replace('"synthetic": true', '"synthetic": true, "synthetic": false'), encoding="utf-8")
            self.assertRefused(CorpusErrorCode.LOCK_INVALID, copy, CorpusMode.DEVELOPMENT)
        with self.subTest("partition hash of development"):
            copy = CorpusCopy(self)
            lock = copy.lock()
            lock["partitions"]["development"]["partition_sha256"] = "f" * 64
            copy.write_lock(lock)
            self.assertRefused(CorpusErrorCode.PARTITION_HASH_MISMATCH, copy, CorpusMode.DEVELOPMENT)

    def test_case_content_rules(self) -> None:
        edits = {
            "llm gold source": lambda doc: doc["gold"].__setitem__("source", "llm_judge"),
            "human gold present": lambda doc: doc["gold"].__setitem__("human_review", {"reviewers": ["a", "b"]}),
            "abstention contradicts category": lambda doc: doc["gold"].__setitem__("abstain_expected", True),
            "unknown key": lambda doc: doc.__setitem__("profit_label", 1),
            "unknown category": lambda doc: doc.__setitem__("category", "profitable"),
            "unknown role": lambda doc: doc.__setitem__("role", "trader"),
            "not synthetic": lambda doc: doc.__setitem__("synthetic", False),
            "empty user": lambda doc: doc["prompt"].__setitem__("user", ""),
            "duplicate evidence id": lambda doc: doc["evidence_ids"].append(doc["evidence_ids"][0]),
            "schema version": lambda doc: doc.__setitem__("schema_version", 2),
        }
        for name, edit in edits.items():
            with self.subTest(name):
                copy = CorpusCopy(self)
                document = copy.read("development", "dev-positive-001")
                edit(document)
                copy.write("development", "dev-positive-001", document)
                self.assertRefused(CorpusErrorCode.CASE_INVALID, copy, CorpusMode.DEVELOPMENT)
        for name, text in (("NaN", '{"a": NaN}'), ("duplicate key", '{"a": 1, "a": 2}'), ("not UTF-8", b"\xff\xfe")):
            with self.subTest(name):
                copy = CorpusCopy(self)
                path = copy.case_path("development", "dev-positive-001")
                path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
                self.assertRefused(CorpusErrorCode.CASE_INVALID, copy, CorpusMode.DEVELOPMENT)
        with self.subTest("oversized"):
            copy = CorpusCopy(self)
            copy.case_path("development", "dev-positive-001").write_bytes(b" " * (bc.MAX_CASE_BYTES + 1))
            self.assertRefused(CorpusErrorCode.FILE_TOO_LARGE, copy, CorpusMode.DEVELOPMENT)

    def test_holdout_must_cover_every_category(self) -> None:
        copy = CorpusCopy(self)
        copy.case_path("holdout", "hold-conflict-001").unlink()
        relocked = copy.relock()
        self.assertRefused(CorpusErrorCode.HOLDOUT_CATEGORY_MISSING, copy, CorpusMode.HOLDOUT, relocked)


class TestDevelopmentModeCannotReadHoldout(unittest.TestCase):
    def test_no_holdout_path_is_opened_or_listed(self) -> None:
        opened: list[str] = []
        scanned: list[str] = []
        real_open, real_scandir = builtins.open, os.scandir

        def recording_open(file, *args, **kwargs):  # noqa: ANN001, ANN202
            opened.append(os.fspath(file))
            return real_open(file, *args, **kwargs)

        def recording_scandir(path):  # noqa: ANN001, ANN202
            scanned.append(os.fspath(path))
            return real_scandir(path)

        with mock.patch("builtins.open", recording_open), mock.patch.object(bc.os, "scandir", recording_scandir):
            partition = development()
        self.assertEqual(len(partition.cases), 5)
        touched = opened + scanned
        self.assertTrue(touched)
        self.assertFalse([path for path in touched if "holdout" in Path(path).parts])
        self.assertEqual(sorted(Path(path).parent.name for path in opened if path.endswith(".json") and "development" in path), ["development"] * 5)

    def test_development_mode_works_without_the_holdout_directory(self) -> None:
        copy = CorpusCopy(self)
        shutil.rmtree(copy.root / "holdout")
        self.assertEqual(len(copy.load(CorpusMode.DEVELOPMENT).cases), 5)
        with self.assertRaises(CorpusError) as caught:
            copy.load(CorpusMode.HOLDOUT)
        self.assertIs(caught.exception.code, CorpusErrorCode.HOLDOUT_EDITED)

    def test_development_mode_never_returns_a_holdout_case(self) -> None:
        holdout_ids = {case.case_id for case in holdout().cases}
        self.assertFalse(holdout_ids & {case.case_id for case in development().cases})


# -- harness -----------------------------------------------------------------------------------


class TestProfilesFromT050a(unittest.TestCase):
    def test_default_profile_envelope(self) -> None:
        profile = default_profile()
        self.assertEqual(profile.inference.model, "qwen3:14b")
        self.assertIs(profile.inference.role, Role.SCREENER)
        self.assertEqual((profile.inference.context_tokens, profile.inference.output_cap_tokens), (4096, 768))
        self.assertEqual((profile.min_free_vram_gib, profile.min_free_ram_gib), (1.5, 4.0))
        self.assertEqual(bm.input_budget_tokens(profile.inference), 2800)

    def test_disabled_profile_is_refused(self) -> None:
        profiles = load_model_profiles()
        disabled = profiles.get(profiles.default_profile_id)
        disabled = dataclasses.replace(disabled, enabled=False, inference=None)
        with self.assertRaises(ModelProfileError):
            benchmark_profile(disabled)


class TestAllValidRun(unittest.TestCase):
    def test_valid_answers_are_accepted_and_promotion_stays_blocked(self) -> None:
        partition = holdout()
        model = FakeModel(valid_script(partition))
        report = run_benchmark(partition, default_profile(), model, model)
        self.assertEqual([item.outcome for item in report.cases], [CaseOutcome.ACCEPTED] * 6)
        self.assertEqual((report.denominator, report.accepted, report.first_pass_schema_valid), (6, 6, 6))
        self.assertEqual(report.abstention_recall, (2, 2))
        self.assertEqual(report.false_abstention, (0, 4))
        self.assertIs(report.gold_status, GoldStatus.GOLD_UNAVAILABLE)
        self.assertTrue(all(item.gold_status is GoldStatus.GOLD_UNAVAILABLE for item in report.cases))
        self.assertIs(report.promotion, PromotionStatus.BLOCKED)
        self.assertEqual(
            report.block_reasons,
            (
                BlockReason.SYNTHETIC_CORPUS,
                BlockReason.CORPUS_BELOW_OC6_SIZE,
                BlockReason.GOLD_UNAVAILABLE,
                BlockReason.LATENCY_NOT_MEASURED,
                BlockReason.RATIONALE_UNVERIFIED,
            ),
        )
        self.assertEqual(report.rationale_unverified, 6)
        self.assertTrue(all(item.rationale_unverified for item in report.cases))
        document = json.loads(report_json(report))
        self.assertEqual((document["oc6_corpus"], document["synthetic_corpus"], document["promotion"]), (False, True, "blocked"))

    def test_development_run_is_marked_not_holdout(self) -> None:
        partition = development()
        model = FakeModel(valid_script(partition))
        report = run_benchmark(partition, default_profile(), model, model)
        self.assertIn(BlockReason.NOT_HOLDOUT, report.block_reasons)
        self.assertIs(report.partition, CorpusPartition.DEVELOPMENT)

    def test_each_call_is_built_from_the_profile_without_truncation(self) -> None:
        partition = holdout()
        model = FakeModel(valid_script(partition))
        run_benchmark(partition, default_profile(), model, model)
        self.assertEqual(len(model.calls), 6)
        ordered = sorted(partition.cases, key=lambda case: case.case_id)
        for call, case in zip(model.calls, ordered):
            self.assertEqual((call.system, call.user), (case.system, case.user))
            self.assertEqual(call.model, "qwen3:14b")
            self.assertEqual((call.context_tokens, call.output_cap_tokens, call.timeout_seconds, call.think), (4096, 768, 30.0, False))
            self.assertIs(call.response_schema, SCREENER_OUTPUT_SCHEMA)


class TestPerCaseGates(unittest.TestCase):
    def run_one(self, case_id: str, result: object, resources: object = OK_RESOURCES):
        partition = holdout()
        script = valid_script(partition)
        script[by_id(partition, case_id).user] = (result, resources)
        model = FakeModel(script)
        report = run_benchmark(partition, default_profile(), model, model)
        return report, next(item for item in report.cases if item.case_id == case_id)

    def test_invalid_outputs_count_in_the_denominator(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        invalid = {
            "missing key": {k: v for k, v in answer(case).items() if k != "rationale"},
            "extra key": {**answer(case), "confidence": "HIGH"},
            "abstain as string": answer(case, abstain="false"),
            "abstain as int": answer(case, abstain=0),
            "unknown classification": answer(case, classification="strong_buy"),
            "cited ids not a list": answer(case, cited_evidence_ids="ev-h3a"),
            "cited id malformed": answer(case, cited_evidence_ids=["EV H3A"]),
            "duplicate cited id": answer(case, cited_evidence_ids=["ev-h3a", "ev-h3a"]),
            "rationale too long": answer(case, rationale="a" * 601),
            "not an object": ["abstain", False],
        }
        for name, payload in invalid.items():
            with self.subTest(name):
                report, item = self.run_one("hold-positive-001", reply(payload))
                self.assertIs(item.outcome, CaseOutcome.SCHEMA_INVALID)
                self.assertFalse(item.first_pass_schema_valid)
                self.assertEqual((report.denominator, report.first_pass_schema_valid, report.accepted), (6, 5, 5))
                self.assertIn(BlockReason.FIRST_PASS_SCHEMA_BELOW_99, report.block_reasons)
        with self.subTest("adapter reports malformed"):
            report, item = self.run_one("hold-positive-001", InferenceFailed(InferenceFailure.MALFORMED))
            self.assertIs(item.outcome, CaseOutcome.SCHEMA_INVALID)
            self.assertEqual(item.failure_code, "malformed")
            self.assertEqual(report.first_pass_schema_valid, 5)

    def test_risk_domain_output_is_a_hard_rejection(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        risky = {
            "leverage key": {**answer(case), "leverage": 5},
            "nested stop loss": {**answer(case), "plan": {"stop_loss": 101.5}},
            "take profit camelCase": {**answer(case), "takeProfit": 120},
            "position size in a list": {**answer(case), "legs": [{"position_size": 0.1}]},
            "quantity": {**answer(case), "qty": 3},
            "sizing": {**answer(case), "Sizing": "half"},
            "tp1": {**answer(case), "tp1": 110},
            "rationale leverage": answer(case, rationale="Enter with 10x leverage."),
            "rationale stop": answer(case, rationale="Place the stop loss at 98.2."),
            "rationale take profit": answer(case, rationale="Take-profit near the prior high."),
            "rationale position sizing": answer(case, rationale="Use normal position sizing."),
            "rationale multiplier": answer(case, rationale="Worth 3x."),
            "rationale stop at": answer(case, rationale="Stops at 97 keep it safe."),
            "risk text in a cited id": answer(case, cited_evidence_ids=["sl-97"]),
        }
        for name, payload in risky.items():
            with self.subTest(name):
                self.assertTrue(invades_risk_domain(payload))
                report, item = self.run_one("hold-positive-001", reply(payload))
                self.assertIs(item.outcome, CaseOutcome.RISK_AUTHORITY_REJECTED)
                self.assertFalse(item.first_pass_schema_valid)
                self.assertIsNone(item.classification)
                self.assertEqual(report.outcome_counts[CaseOutcome.RISK_AUTHORITY_REJECTED], 1)
                self.assertIn(BlockReason.RISK_AUTHORITY_OUTPUT, report.block_reasons)
        for text in (
            "The last bar is 1860 seconds old against a 600 second freshness bound.",
            "Breakout above the 20-bar high with spread of 4 bps.",
            "Regime summary is unknown; coverage is below the floor.",
        ):
            with self.subTest(f"ordinary rationale: {text}"):
                self.assertFalse(invades_risk_domain(answer(case, rationale=text)))

    # T4 attempt 1 REJECT (docs/forja/reports/T4-a1-review.md): phrasings found accepted, verbatim.
    ATTEMPT1_PHRASES = (
        "Enter now, stop 97, target 120, size 2 BTC",
        "Buy 0.5 BTC",
        "SL 97 TP 120",
        "Stop: 97.0",
        "Target price 120",
        "Exit at 120",
        "Risk 1% of equity per trade",
        "Allocate 30% of the account",
        "Buy 0.5 BTC now, stop 97, target 120, SL 97 TP 120.",
    )
    # T4 attempt 2 REJECT (docs/forja/reports/T4-a2-review.md): every phrasing and payload, verbatim.
    ATTEMPT2_PHRASES = (
        "Stop below the swing low at 97", "Place your stop under the recent low of 97", "Stop just under support at 97",
        "Stop-out at 97", "sl97", "s/l 97", "Cut losses under 97", "Get out below 97",
        "Take gains at 120", "Book profit at 120", "Scale out at 120 and 130", "Sell half at 120", "Trim at 120",
        "t/p 120", "PT 120",
        "Risk no more than 2 percent", "2% risk", "Use 1/10 of your capital", "Go all in", "Use full size",
        "Double your position",
        "Buy with 1000 USDT", "Spend 500 USD", "Put in 500 USD", "Hedge with 2 BTC", "Collateral 3 BTC",
        "Liquidation at 90",
        "Stop below the swing low at 97, take gains at 120, risk no more than 2 percent.",
        "Place your stop under the recent low of 97; scale out at 120.",
        "s/l 97, t/p 120",
        "stop≈97", "stop,97",
    )
    # Own variants: casing, no spaces, separators, look-alikes, other scripts, numbers as words,
    # fractions, level words and words the attempt-2 patterns never listed.
    OWN_VARIANTS = (
        "STOP97", "stop97target120", "sl97tp120", "st0p 97", "s t o p 97", "s.t.o.p 97", "sto p loss",
        "le verage 3", "1everage 5x", "5x", "x10", "2R", "ｓｔｏｐ ９７",
        "st​op 97", "ѕtор at 97", "стоп 97", "Stöp at 97",
        "stop at ninety-seven", "stop ninety seven", "Take gains at one hundred twenty", "take‑profit one-twenty",
        "Buy half a bitcoin", "Use a tenth of the stack", "Put a quarter in now", "Sell ½ at 120",
        "Stop. 97.", "Stop;\n97", "Exit.", "Close at 120", "Bail at 97", "Cash out at 120", "Invalidation at 97",
        "Floor at 97", "Aim for 120", "Upside 120", "Unwind at 120", "Out at 120", "Borrow 500", "Weight 20%",
        "@97", "Bid 97", "sell everything at 120", "Level to watch: 97", "alvo 120", "Compra 0,5 BTC",
        "Go long here", "Risk-reward 1:3", "R:R 3", "Size up now", "Keep your position", "trailing stop",
        # Execution or sizing imperatives with no number at all.
        "Sell everything now", "Buy the dip", "Close the trade", "You should buy", "I recommend buying",
        "Hold for now", "Evidence is clean, buy here", "Take it", "Short it", "Cover now",
    )
    # Analytic wording a correct screener answer may carry and the hard gate lets through. It is
    # still never trusted: an accepted answer carrying it is flagged rationale_unverified.
    ORDINARY_RATIONALES = (
        "The quantity of evidence is insufficient for a verdict.",
        "Evidence ev-h3a and ev-h3b conflict: one source shows 97.5, the other 101.2.",
        "Stale: the last bar is 31 minutes old.",
        "Sources disagree: 97 versus 101.",
        "The last bar is 1860 seconds old against a 600 second freshness bound.",
        "Breakout above the 20-bar high with spread of 4 bps.",
        "Regime summary is unknown; coverage is below the floor.",
        "Insufficient evidence: only 1 of 3 required sources is present.",
        "Momentum score is 0.72 on a 1-hour basis.",
        "The squeeze released after a Bollinger width percentile of 3.",
        "Depth within 10 bps clears the liquidity threshold.",
        "All venues are fresh and valid; no conflict between 3 feeds.",
        "Price rose 4 bars in a row without a clear edge.",
        "The supplied synthetic evidence supports this classification.",
    )
    # Analytic on their face, refused ON PURPOSE by the fail-closed rule: a numeral anywhere in
    # the string together with a risk or execution term anywhere (short, long, sell, close,
    # BTC, %, percent, limit, stopped ...). These are the attempt-2 "ordinary" rationales that
    # the co-occurrence rule now refuses; a false refusal costs a measured answer, never safety.
    CONSERVATIVE_REFUSALS = (
        "Short interest rose 12% in a week.",
        "Long 3-day consolidation below resistance.",
        "Long 30 days of range trading; no edge.",
        "The move stopped at 97 before reversing.",
        "Sell pressure eased after 3 days.",
        "Whale wallets moved 500 BTC to exchanges.",
        "Funding rate 0.01% and open interest up 8% over 3 days.",
        "Price closed above the 20-bar high at 104.2 with volume up 35% over 24h.",
        "Sources disagree on the close: 97 versus 101.",
        "The last closed bar is 1860 seconds old against a 600 second limit.",
        "Breakout close above the 20-bar high with spread of 4 bps.",
        "Regime summary is unknown with coverage of 40 percent.",
    )

    # Known gap, kept visible: a short word matched exactly ("hold", "cover") is not found
    # when the whole phrase is glued ("Holdfornow"); the rationale_unverified net still applies.
    GLUED_NOT_CAUGHT = ("Hold for now", "Cover now")

    def test_every_rejected_risk_phrasing_is_refused(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        phrases = self.ATTEMPT1_PHRASES + self.ATTEMPT2_PHRASES + self.OWN_VARIANTS
        self.assertEqual(len(set(phrases)), len(phrases))
        for glued in self.GLUED_NOT_CAUGHT:
            self.assertFalse(bm.risk_wording(glued.replace(" ", "")))
        for text in phrases:
            variants = {
                "as written": text,
                "lower": text.lower(),
                "upper": text.upper(),
                "no spaces": text if text in self.GLUED_NOT_CAUGHT else text.replace(" ", ""),
                "line breaks": text.replace(" ", "\n"),
                "inside a sentence": f"Setup is admissible. {text}.",
                "after analytic text": f"Evidence ev-h3a is fresh and valid; {text}",
            }
            for name, variant in variants.items():
                with self.subTest(ascii(text), variant=name):
                    self.assertTrue(bm.risk_wording(variant, case.evidence_ids))
                    self.assertTrue(invades_risk_domain(answer(case, rationale=variant), case.evidence_ids))

    def test_ordinary_analytic_rationales_pass_the_hard_gate(self) -> None:
        partition = holdout()
        for text in self.ORDINARY_RATIONALES:
            with self.subTest(text):
                self.assertFalse(bm.risk_wording(text, ("ev-h3a", "ev-h3b")))
        for item in partition.cases:  # every category and every evidence id of the fixtures
            for evidence_id in item.evidence_ids:
                with self.subTest(item.case_id, evidence_id=evidence_id):
                    self.assertFalse(invades_risk_domain(answer(item, cited_evidence_ids=[evidence_id]), item.evidence_ids))
                    self.assertFalse(invades_risk_domain(answer(item, cited_evidence_ids=[evidence_id])))

    def test_conservative_refusals_are_by_design(self) -> None:
        for text in self.CONSERVATIVE_REFUSALS:
            with self.subTest(text):
                self.assertTrue(bm.risk_wording(text))

    def test_cited_ids_in_the_rationale_are_not_numerals(self) -> None:
        text = "ev-h3a shows sell pressure."
        self.assertFalse(bm.risk_wording(text, ("ev-h3a",)))
        self.assertTrue(bm.risk_wording(text))  # an id the case does not own is just text with a digit
        self.assertTrue(bm.risk_wording("ev-h3a shows sell pressure at 97.", ("ev-h3a",)))

    def test_risky_rationales_are_rejected_end_to_end(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        payloads = (
            "Buy 0.5 BTC now, stop 97, target 120, SL 97 TP 120.",
            "Stop below the swing low at 97, take gains at 120, risk no more than 2 percent.",
            "Place your stop under the recent low of 97; scale out at 120.",
            "s/l 97, t/p 120",
        )
        rationales = tuple(dict.fromkeys(payloads + self.ATTEMPT2_PHRASES + self.OWN_VARIANTS))
        self.assertEqual(len(rationales), 94)
        for rationale in rationales:
            with self.subTest(ascii(rationale)):
                report, item = self.run_one("hold-positive-001", reply(answer(case, rationale=rationale)))
                self.assertIs(item.outcome, CaseOutcome.RISK_AUTHORITY_REJECTED)
                self.assertFalse(item.first_pass_schema_valid)
                self.assertGreaterEqual(report.outcome_counts[CaseOutcome.RISK_AUTHORITY_REJECTED], 1)
                self.assertEqual(report.accepted, 5)
                self.assertIn(BlockReason.RISK_AUTHORITY_OUTPUT, report.block_reasons)
                self.assertIs(report.promotion, PromotionStatus.BLOCKED)
                self.assertNotIn(rationale, report_json(report))

    def test_surviving_free_text_is_never_verified_and_blocks_promotion(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        with self.subTest("a rationale the hard gate cannot read as risk"):
            # "97 -> 120" states a plan with no word at all: the hard gate lets it through,
            # the rationale_unverified net still blocks promotion.
            report, item = self.run_one("hold-positive-001", reply(answer(case, rationale="97 -> 120")))
            self.assertIs(item.outcome, CaseOutcome.ACCEPTED)
            self.assertTrue(item.rationale_unverified)
            self.assertIn(BlockReason.RATIONALE_UNVERIFIED, report.block_reasons)
            self.assertIs(report.promotion, PromotionStatus.BLOCKED)
        partition = holdout()
        script = {c.user: (reply(answer(c, rationale=" \n")), OK_RESOURCES) for c in partition.cases}
        model = FakeModel(script)
        report = run_benchmark(partition, default_profile(), model, model)
        with self.subTest("no free text: the net is not raised"):
            self.assertEqual((report.accepted, report.rationale_unverified), (6, 0))
            self.assertFalse(any(item.rationale_unverified for item in report.cases))
            self.assertNotIn(BlockReason.RATIONALE_UNVERIFIED, report.block_reasons)
        script[case.user] = (reply(answer(case, rationale="Fresh and valid.")), OK_RESOURCES)
        model = FakeModel(script)
        report = run_benchmark(partition, default_profile(), model, model)
        with self.subTest("one accepted rationale is enough to block"):
            self.assertEqual(report.rationale_unverified, 1)
            self.assertIn(BlockReason.RATIONALE_UNVERIFIED, report.block_reasons)
            self.assertEqual(json.loads(report_json(report))["rationale_unverified"], 1)

    def test_extra_risk_keys_are_classified_as_risk_not_schema(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        for key in ("size_usd", "targets", "risk", "stopLevel", "allocation_pct", "marginMode", "exposure_usd"):
            with self.subTest(key):
                report, item = self.run_one("hold-positive-001", reply({**answer(case), key: 1}))
                self.assertIs(item.outcome, CaseOutcome.RISK_AUTHORITY_REJECTED)
        with self.subTest("unknown non-risk key stays schema-invalid"):
            report, item = self.run_one("hold-positive-001", reply({**answer(case), "confidence": 0.7}))
            self.assertIs(item.outcome, CaseOutcome.SCHEMA_INVALID)

    def test_risk_wording_scan_stays_linear_on_hostile_text(self) -> None:
        cap = bm.MAX_SCANNED_TEXT_CHARS
        within = ("1" * cap, "stop" + " " * (cap - 4), "a " * (cap // 2), "1." * (cap // 2), "buy the " * (cap // 8),
                  "ab" * (cap // 2), "s " * (cap // 2))
        for text in within:
            with self.subTest(text[:12]):
                started = time.perf_counter()
                bm.risk_wording(text)
                self.assertLess(time.perf_counter() - started, 5.0)
        for text in ("1" * 200_000, "the " * 50_000, " " * (cap + 1)):
            with self.subTest(f"over the cap: {text[:8]!r}"):
                started = time.perf_counter()
                self.assertTrue(bm.risk_wording(text))  # refused unread, never half scanned
                self.assertLess(time.perf_counter() - started, 1.0)

    def test_timeout_and_other_failures(self) -> None:
        for failure, outcome in (
            (InferenceFailure.TIMEOUT, CaseOutcome.TIMEOUT),
            (InferenceFailure.LATE, CaseOutcome.TIMEOUT),
            (InferenceFailure.UNAVAILABLE, CaseOutcome.INFERENCE_FAILED),
            (InferenceFailure.TOO_LARGE, CaseOutcome.INFERENCE_FAILED),
        ):
            with self.subTest(failure.value):
                report, item = self.run_one("hold-insufficient-001", InferenceFailed(failure))
                self.assertIs(item.outcome, outcome)
                self.assertEqual(item.failure_code, failure.value)
                self.assertIsNone(item.abstained)
                self.assertEqual(report.denominator, 6)
                self.assertEqual(report.abstention_recall, (1, 2))  # a failed call is a missed abstention
        report, _ = self.run_one("hold-insufficient-001", InferenceFailed(InferenceFailure.TIMEOUT))
        self.assertIn(BlockReason.HARD_LIMIT_FAILURES, report.block_reasons)
        with self.subTest("adapter raises"):
            report, item = self.run_one("hold-insufficient-001", RuntimeError("boom"))
            self.assertIs(item.outcome, CaseOutcome.INFERENCE_FAILED)
            self.assertEqual(item.failure_code, "adapter_error")

    def test_resource_gate_against_the_profile_envelope(self) -> None:
        case = by_id(holdout(), "hold-positive-002")
        good = reply(answer(case))
        cases = {
            "oom": (ResourceReport(oom=True, min_free_vram_gib=3.0, min_free_ram_gib=12.0), CaseOutcome.OOM),
            "oom without numbers": (ResourceReport(oom=True, min_free_vram_gib=math.nan, min_free_ram_gib=-1.0), CaseOutcome.OOM),
            "vram below reserve": (ResourceReport(oom=False, min_free_vram_gib=1.49, min_free_ram_gib=12.0), CaseOutcome.RESOURCE_RESERVE_BREACHED),
            "ram below reserve": (ResourceReport(oom=False, min_free_vram_gib=3.0, min_free_ram_gib=3.99), CaseOutcome.RESOURCE_RESERVE_BREACHED),
            "exactly at reserve": (ResourceReport(oom=False, min_free_vram_gib=1.5, min_free_ram_gib=4.0), CaseOutcome.ACCEPTED),
            "not reported": (None, CaseOutcome.RESOURCES_UNREPORTED),
            "wrong type": ({"oom": False}, CaseOutcome.RESOURCES_UNREPORTED),
            "non-finite": (ResourceReport(oom=False, min_free_vram_gib=math.inf, min_free_ram_gib=12.0), CaseOutcome.RESOURCES_UNREPORTED),
            "bool as number": (ResourceReport(oom=False, min_free_vram_gib=True, min_free_ram_gib=12.0), CaseOutcome.RESOURCES_UNREPORTED),
            "oom flag not bool": (ResourceReport(oom=0, min_free_vram_gib=3.0, min_free_ram_gib=12.0), CaseOutcome.RESOURCES_UNREPORTED),  # type: ignore[arg-type]
        }
        for name, (resources, outcome) in cases.items():
            with self.subTest(name):
                report, item = self.run_one("hold-positive-002", good, resources)
                self.assertIs(item.outcome, outcome)
                if outcome is CaseOutcome.OOM:
                    self.assertIn(BlockReason.OOM_OBSERVED, report.block_reasons)
                if outcome is not CaseOutcome.ACCEPTED:
                    self.assertFalse(item.first_pass_schema_valid)
        with self.subTest("oom wins over a valid reply and over a timeout"):
            _, item = self.run_one("hold-positive-002", InferenceFailed(InferenceFailure.TIMEOUT), cases["oom"][0])
            self.assertIs(item.outcome, CaseOutcome.OOM)

    def test_output_cap_and_citation_scope(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        _, item = self.run_one("hold-positive-001", reply(answer(case), output_tokens=769))
        self.assertIs(item.outcome, CaseOutcome.OUTPUT_CAP_EXCEEDED)
        _, item = self.run_one("hold-positive-001", reply(answer(case), output_tokens=768))
        self.assertIs(item.outcome, CaseOutcome.ACCEPTED)
        report, item = self.run_one("hold-positive-001", reply(answer(case, cited_evidence_ids=["ev-h3a", "ev-h1a"])))
        self.assertIs(item.outcome, CaseOutcome.CITATION_OUT_OF_SCOPE)
        self.assertTrue(item.first_pass_schema_valid)
        self.assertIn(BlockReason.CITATION_OUT_OF_SCOPE, report.block_reasons)

    def test_wrong_abstention_is_measured(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        report, item = self.run_one("hold-positive-001", reply(answer(case, abstain=True)))
        self.assertIs(item.outcome, CaseOutcome.ACCEPTED)
        self.assertEqual(report.false_abstention, (1, 4))
        self.assertIn(BlockReason.FALSE_ABSTENTION_ABOVE_15, report.block_reasons)
        stale = by_id(holdout(), "hold-invalid-stale-001")
        report, _ = self.run_one("hold-invalid-stale-001", reply(answer(stale, abstain=False)))
        self.assertEqual(report.abstention_recall, (1, 2))
        self.assertIn(BlockReason.ABSTENTION_RECALL_BELOW_95, report.block_reasons)


class TestRiskGateNumberWordsAreLinear(unittest.TestCase):
    """T4 attempt 3 REJECT (docs/forja/reports/T4-a3-review.md): _NUMBER_WORD backtracked 2^n."""

    ORIGINAL_SNAPSHOT = REPOSITORY_ROOT / "docs" / "forja" / "archive" / "R-20260919-7c62-T4-sobras-codigo-ORIGINAL.patch"
    PHRASE_TUPLES = ("ATTEMPT1_PHRASES", "ATTEMPT2_PHRASES", "OWN_VARIANTS", "ORDINARY_RATIONALES")
    # The verdict's hostile tokens: a word that is both a unit and a unit plus "th", repeated,
    # then a letter no unit accepts, so every split has to be ruled out.
    HOSTILE_WORDS = ("fourth", "tenth", "sixth", "seventh")
    HOSTILE_SIZES = (600, 4096)
    SECONDS = 1.0  # loose bound (TECHNOLOGY.md S3); the fixed code takes a few milliseconds

    def original_test_source(self) -> str:
        lines = self.ORIGINAL_SNAPSHOT.read_text(encoding="utf-8").splitlines()
        start = lines.index("+++ b/tests/test_benchmark_harness.py") + 2  # skip the hunk header
        body: list[str] = []
        for line in lines[start:]:
            if line.startswith("diff --git "):
                break
            body.append(line[1:])
        return "\n".join(body)

    @staticmethod
    def class_tuples(source: str, class_name: str, names: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        found: dict[str, tuple[str, ...]] = {}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for statement in node.body:
                    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                        target = statement.targets[0]
                        if isinstance(target, ast.Name) and target.id in names:
                            found[target.id] = ast.literal_eval(statement.value)
        return found

    def test_phrase_tuples_only_grew_against_the_original_snapshot(self) -> None:
        original = self.class_tuples(self.original_test_source(), "TestPerCaseGates", self.PHRASE_TUPLES)
        current = self.class_tuples(Path(__file__).read_text(encoding="utf-8"), "TestPerCaseGates", self.PHRASE_TUPLES)
        self.assertEqual(set(original), set(self.PHRASE_TUPLES))
        self.assertEqual(set(current), set(self.PHRASE_TUPLES))
        for name in self.PHRASE_TUPLES:
            with self.subTest(name):
                self.assertGreater(len(original[name]), 0)
                self.assertEqual(current[name][: len(original[name])], original[name])  # may only append
                self.assertEqual(getattr(TestPerCaseGates, name), current[name])

    def test_rejected_phrasings_still_refused_and_ordinary_text_still_passes(self) -> None:
        for text in TestPerCaseGates.ATTEMPT1_PHRASES + TestPerCaseGates.ATTEMPT2_PHRASES + TestPerCaseGates.OWN_VARIANTS:
            with self.subTest(ascii(text)):
                self.assertTrue(bm.risk_wording(text, ("ev-h3a", "ev-h3b")))
        for text in TestPerCaseGates.ORDINARY_RATIONALES:
            with self.subTest(text):
                self.assertFalse(bm.risk_wording(text, ("ev-h3a", "ev-h3b")))

    @staticmethod
    def hostile(word: str, size: int) -> str:
        return word * ((size - 1) // len(word)) + "q"

    def test_hostile_number_words_finish_fast_through_the_public_gate_and_the_tokenizer(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        for word in self.HOSTILE_WORDS:
            for size in self.HOSTILE_SIZES:
                text = self.hostile(word, size)
                self.assertLessEqual(len(text), size)
                self.assertGreater(len(text), size - len(word) - 1)
                with self.subTest(word=word, size=size, path="invades_risk_domain"):
                    payload = answer(case, rationale=text)
                    started = time.perf_counter()
                    refused = invades_risk_domain(payload, case.evidence_ids)
                    self.assertLess(time.perf_counter() - started, self.SECONDS)
                    self.assertFalse(refused)  # a number word but no risk term: same verdict as before the fix
                with self.subTest(word=word, size=size, path="tokenizer"):
                    started = time.perf_counter()
                    matched = bm._is_number_word(text)
                    self.assertLess(time.perf_counter() - started, self.SECONDS)
                    self.assertFalse(matched)
                with self.subTest(word=word, size=size, path="unit regex"):
                    started = time.perf_counter()
                    unit = bm._NUMBER_WORD.fullmatch(text)
                    self.assertLess(time.perf_counter() - started, self.SECONDS)
                    self.assertIsNone(unit)
                with self.subTest(word=word, size=size, path="with a risk term"):
                    started = time.perf_counter()
                    refused = bm.risk_wording("Borrow " + text[: size - 7])
                    self.assertLess(time.perf_counter() - started, self.SECONDS)
                    self.assertTrue(refused)
                with self.subTest(word=word, size=size, path="glued without the stray letter"):
                    self.assertTrue(bm._is_number_word(word * ((size - 1) // len(word))))

    def test_number_word_pattern_has_no_repeater(self) -> None:
        """Structural proof, no clock: the regex is one unit and cannot backtrack exponentially."""
        sre_parse = re._parser  # type: ignore[attr-defined]  # stdlib parser behind re.compile (3.11+)

        repeats = {sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT, sre_parse.POSSESSIVE_REPEAT}

        def walk(items: object) -> list[tuple[object, object]]:
            found: list[tuple[object, object]] = []
            for op, arg in items:  # type: ignore[attr-defined]
                if op in repeats:
                    found.append((op, arg[1]))
                    found.extend(walk(arg[2]))
                elif op is sre_parse.SUBPATTERN:
                    found.extend(walk(arg[-1]))
                elif op is sre_parse.BRANCH:
                    for branch in arg[1]:
                        found.extend(walk(branch))
            return found

        parsed = sre_parse.parse(bm._NUMBER_WORD.pattern)
        self.assertEqual(walk(parsed), [(sre_parse.MAX_REPEAT, 1)])  # only the optional suffix "?"
        self.assertEqual(bm._NUMBER_SUFFIXES, ("s", "th", "ths", "and", "e"))
        self.assertIn("fourth", bm._NUMBER_UNITS)
        self.assertIn("four", bm._NUMBER_UNITS)
        self.assertEqual(len(bm._NUMBER_UNITS), 84)
        self.assertEqual(bm._NUMBER_STEMS, tuple(unit for unit in bm._NUMBER_UNITS if len(unit) >= 3))
        grouped = sorted(unit for group in bm._NUMBER_UNITS_BY_INITIAL.values() for unit in group)
        self.assertEqual(grouped, sorted(bm._NUMBER_UNITS))

    def test_tokenizer_is_one_left_to_right_pass(self) -> None:
        """Structural proof by inspection: one pass over the token, inner loops over fixed tuples."""
        function = ast.parse(inspect.getsource(bm._is_number_word)).body[0]
        self.assertIsInstance(function, ast.FunctionDef)
        loops = [node for node in ast.walk(function) if isinstance(node, (ast.For, ast.While, ast.comprehension))]
        self.assertFalse(any(isinstance(node, ast.While) for node in loops))
        iterated = sorted(ast.unparse(node.iter) for node in loops if not isinstance(node, ast.While))
        self.assertEqual(iterated, ["_NUMBER_SUFFIXES", "_NUMBER_UNITS_BY_INITIAL.get(initial, ())", "enumerate(token)"])
        called = {ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)}
        self.assertEqual(called, {"enumerate", "_NUMBER_UNITS_BY_INITIAL.get", "token.startswith", "len"})
        has_numeral = inspect.getsource(bm._has_numeral)
        self.assertIn("_is_number_word(token)", has_numeral)
        self.assertNotIn("fullmatch", has_numeral)
        self.assertNotIn("_NUMBER_WORD", has_numeral)

    def test_tokenizer_reads_the_same_language_as_the_repeated_pattern(self) -> None:
        repeated = re.compile("(?:" + bm._NUMBER_WORD.pattern + ")+")  # the old form; safe on these short tokens
        for token, expected in (
            ("ninetyseven", True), ("fourteen", True), ("fourth", True), ("fourthfourth", True), ("tenths", True),
            ("twentyand", True), ("seventhq", False), ("", False), ("th", False), ("quarter", True), ("dozens", True),
            ("vinteetres", True), ("stop", False), ("fourthly", False),
        ):
            with self.subTest(token):
                self.assertIs(bm._is_number_word(token), expected)
                self.assertIs(repeated.fullmatch(token) is not None, expected)
        rng = random.Random(20260919)
        pieces = (*bm._NUMBER_UNITS, *bm._NUMBER_SUFFIXES, "q", "a", "t", "h", "y")
        for _ in range(5000):
            token = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 4)))
            self.assertIs(bm._is_number_word(token), repeated.fullmatch(token) is not None, token)


class TestAlnumRunIsLinearAndTokensUnchanged(unittest.TestCase):
    """T050c (docs/forja/reports/T2-a1-security.md): ``_ALNUM_RUN`` wrapped its mandatory
    "[a-z]" in "*" on both sides over the SAME class, so a run with no letter at all made
    ``findall`` back off one character at a time from every position a run could start,
    O(run^2). Security Reviewer measured 0.435 s on 4096 x U+2152 (NFKC-folds to runs of
    digits) and 0.046 s on 4096 x "1"; this repository's re-measurement before the fix,
    same shapes, was 0.468 s and 0.052 s (see docs/tasks/results/T050.md, section T050c).
    """

    # The old pattern, kept here read-only as the equivalence reference. Never imported
    # back into radar_v08/workflow/benchmark.py.
    OLD_ALNUM_RUN = re.compile(r"[a-z0-9$@]*[a-z][a-z0-9$@]*")

    CORPUS = (
        TestPerCaseGates.ATTEMPT1_PHRASES
        + TestPerCaseGates.ATTEMPT2_PHRASES
        + TestPerCaseGates.OWN_VARIANTS
        + TestPerCaseGates.ORDINARY_RATIONALES
        + TestPerCaseGates.CONSERVATIVE_REFUSALS
    )

    def old_tokens(self, joined: str) -> set[str]:
        tokens: set[str] = set()
        for word in self.OLD_ALNUM_RUN.findall(joined):
            if any(char.isdigit() or char in "$@" for char in word):
                tokens.update(word.translate(table) for table in bm._LEET_VARIANTS)
        return tokens

    def new_tokens(self, joined: str) -> set[str]:
        tokens: set[str] = set()
        for word in bm._ALNUM_RUN.findall(joined):
            if any(char.isalpha() for char in word) and any(char.isdigit() or char in "$@" for char in word):
                tokens.update(word.translate(table) for table in bm._LEET_VARIANTS)
        return tokens

    def test_no_repeater_wraps_the_mandatory_letter(self) -> None:
        """Structural proof, no clock: one "+" over one class, nothing to backtrack."""
        sre_parse = re._parser  # type: ignore[attr-defined]  # stdlib parser behind re.compile (3.11+)
        self.assertEqual(bm._ALNUM_RUN.pattern, "[a-z0-9$@]+")
        parsed = sre_parse.parse(bm._ALNUM_RUN.pattern)
        repeats = [(op, arg[1]) for op, arg in parsed if op is sre_parse.MAX_REPEAT]  # type: ignore[attr-defined]
        self.assertEqual(repeats, [(sre_parse.MAX_REPEAT, sre_parse.MAXREPEAT)])  # exactly one "+", unbounded
        self.assertNotIn(sre_parse.MIN_REPEAT, [op for op, _ in parsed])  # type: ignore[attr-defined]

    def test_phrase_tuples_did_not_shrink(self) -> None:
        # Pinned to the counts named in the task: 102 refused (9 + 32 + 61), 14 accepted
        # rationales (D42), 12 refused on purpose. A shrink here would silently narrow the
        # equivalence corpus below.
        self.assertEqual(len(TestPerCaseGates.ATTEMPT1_PHRASES), 9)
        self.assertEqual(len(TestPerCaseGates.ATTEMPT2_PHRASES), 32)
        self.assertEqual(len(TestPerCaseGates.OWN_VARIANTS), 61)
        self.assertEqual(len(TestPerCaseGates.ORDINARY_RATIONALES), 14)
        self.assertEqual(len(TestPerCaseGates.CONSERVATIVE_REFUSALS), 12)

    def test_tokens_and_verdicts_match_the_old_pattern_on_the_whole_corpus(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        for text in self.CORPUS:
            with self.subTest(ascii(text)):
                folded = bm._fold_text(text)
                joined = bm._INTRA_WORD_MARKS.sub("", folded)
                self.assertEqual(self.new_tokens(joined), self.old_tokens(joined))
                new_verdict = bm.risk_wording(text, case.evidence_ids)
                new_invades = bm.invades_risk_domain(answer(case, rationale=text), case.evidence_ids)
                with mock.patch.object(bm, "_ALNUM_RUN", self.OLD_ALNUM_RUN):
                    old_verdict = bm.risk_wording(text, case.evidence_ids)
                    old_invades = bm.invades_risk_domain(answer(case, rationale=text), case.evidence_ids)
                self.assertIs(new_verdict, old_verdict)
                self.assertIs(new_invades, old_invades)

    def test_hostile_runs_with_no_letter_finish_fast_through_the_public_gate(self) -> None:
        # Below MAX_SCANNED_TEXT_CHARS (4096): must be scanned, not refused unread. NFKC
        # turns U+2152 into "1/10"-shaped runs of digits with no letter (the OLD pattern's
        # worst case); "1" x 4096 is one long digit run with no letter either.
        cap = bm.MAX_SCANNED_TEXT_CHARS
        self.assertEqual(cap, 4096)
        case = by_id(holdout(), "hold-positive-001")
        seconds = 1.0  # below the existing ReDoS test's 5.0 s within-cap bound (line ~806)
        for label, text in (("4096 x U+2152", "⅒" * cap), ("4096 x '1'", "1" * cap)):
            with self.subTest(f"{label}: risk_wording"):
                started = time.perf_counter()
                verdict = bm.risk_wording(text)
                elapsed = time.perf_counter() - started
                self.assertLess(elapsed, seconds)
                self.assertFalse(verdict)  # no risk term in either shape: same verdict as before the fix
            with self.subTest(f"{label}: invades_risk_domain"):
                started = time.perf_counter()
                refused = bm.invades_risk_domain(answer(case, rationale=text), case.evidence_ids)
                elapsed = time.perf_counter() - started
                self.assertLess(elapsed, seconds)
                self.assertFalse(refused)


class TestResponseScanBudget(unittest.TestCase):
    """T050c1b (D54 b; docs/forja/reports/T2-a1-security.md nits 1 and 2): one character budget
    for the whole reply, checked before the risk gate reads anything, fail closed.
    """

    # Distinct, 64 characters, no digit and no risk term: the risk gate reads them as plain text.
    MAX_IDS = tuple(f"ev-{'z' * k}{'q' * (61 - k)}" for k in range(bm.MAX_CITED_IDS))
    WORST_NFKC = "ﷺ"  # NFKC turns it into 18 Arabic letters, no digit
    # Security Reviewer worst case: 16 strings of 4096 x U+2152 in one reply (6.79 s before).
    SECURITY_WORST = ("⅒" * 4096,) * 16
    # Existing ReDoS test (TestPerCaseGates.test_risk_wording_scan_stays_linear_on_hostile_text):
    # 5.0 s within the per-string cap, 1.0 s for text refused unread.
    REDOS_WITHIN_CAP_SECONDS = 5.0
    REDOS_REFUSED_UNREAD_SECONDS = 1.0

    def largest_valid(self, rationale: str | None = None) -> dict[str, object]:
        return {
            "abstain": False,
            "classification": "insufficient_evidence",  # the longest category value (21)
            "cited_evidence_ids": list(self.MAX_IDS),
            "rationale": self.WORST_NFKC * bm.MAX_RATIONALE_CHARS if rationale is None else rationale,
        }

    def owning_case(self) -> BenchmarkCase:
        template = by_id(holdout(), "hold-positive-001")
        return dataclasses.replace(template, evidence_ids=self.MAX_IDS)

    def run_one(self, case_id: str, result: object):
        """Same as ``TestPerCaseGates.run_one``: one scripted reply in an otherwise valid run."""
        partition = holdout()
        script = valid_script(partition)
        script[by_id(partition, case_id).user] = (result, OK_RESOURCES)
        model = FakeModel(script)
        report = run_benchmark(partition, default_profile(), model, model)
        return report, next(item for item in report.cases if item.case_id == case_id)

    def run_single(self, case: BenchmarkCase, payload: object):
        partition = LockedPartition("synthetic-harness-v1", LOCK_SHA256, True, CorpusPartition.HOLDOUT, (case,))
        model = FakeModel({case.user: (reply(payload), OK_RESOURCES)})
        return run_benchmark(partition, default_profile(), model, model).cases[0]

    def test_budget_value_is_the_largest_valid_answer_after_worst_nfkc(self) -> None:
        self.assertEqual(bm.MAX_SCANNED_PAYLOAD_CHARS, 12_920)
        self.assertEqual(bm.MAX_SCANNED_PAYLOAD_CHARS, 1 + 48 + 1 + 21 + 1 + 32 * 64 + 600 * 18)
        self.assertEqual(sum(len(key) for key in bm._OUTPUT_KEYS), 48)
        self.assertEqual(max(len(category.value) for category in CaseCategory), 21)
        self.assertIsNotNone(bm._EVIDENCE_ID.fullmatch("a" * 64))
        self.assertIsNone(bm._EVIDENCE_ID.fullmatch("a" * 65))
        self.assertEqual(default_profile().inference.output_cap_tokens, 768)  # the OC-1 cap the value is argued from

    def test_nfkc_expands_one_character_to_at_most_18(self) -> None:
        # The budget multiplies the rationale by 18; prove no code point expands further here.
        worst = max(
            len(unicodedata.normalize("NFKC", chr(code)))
            for code in range(sys.maxunicode + 1)
            if not 0xD800 <= code <= 0xDFFF
        )
        self.assertEqual(worst, 18)
        self.assertEqual(len(unicodedata.normalize("NFKC", self.WORST_NFKC)), 18)
        for identifier in self.MAX_IDS:  # ids are ASCII: NFKC leaves them as they are
            self.assertEqual(unicodedata.normalize("NFKC", identifier), identifier)

    def test_largest_valid_answer_passes_and_limit_plus_one_is_refused(self) -> None:
        largest = self.largest_valid()
        self.assertEqual(len(set(self.MAX_IDS)), 32)
        self.assertTrue(all(len(identifier) == 64 for identifier in self.MAX_IDS))
        self.assertIsNotNone(parse_screener_answer(largest))  # schema-valid, every field at its maximum
        self.assertFalse(bm.exceeds_scan_budget(largest))  # exactly MAX_SCANNED_PAYLOAD_CHARS units
        with self.subTest("the case owns every cited id: ACCEPTED"):
            item = self.run_single(self.owning_case(), largest)
            self.assertIs(item.outcome, CaseOutcome.ACCEPTED)
            self.assertEqual(item.cited_count, 32)
            self.assertTrue(item.first_pass_schema_valid)
        with self.subTest("the fixture case owns none of them: CITATION_OUT_OF_SCOPE"):
            _, item = self.run_one("hold-positive-001", reply(largest))
            self.assertIs(item.outcome, CaseOutcome.CITATION_OUT_OF_SCOPE)
            self.assertTrue(item.first_pass_schema_valid)
        with self.subTest("an ASCII rationale at its maximum passes too"):
            item = self.run_single(self.owning_case(), self.largest_valid("Fresh and valid. " * 35 + "Calm."))
            self.assertIs(item.outcome, CaseOutcome.ACCEPTED)
        over = self.largest_valid(self.WORST_NFKC * bm.MAX_RATIONALE_CHARS + "a")  # 12,921 units
        with self.subTest("limit + 1 after NFKC: the new typed code, before the schema gate"):
            self.assertTrue(bm.exceeds_scan_budget(over))
            item = self.run_single(self.owning_case(), over)
            self.assertIs(item.outcome, CaseOutcome.RESPONSE_BUDGET_EXCEEDED)
            self.assertEqual(item.outcome.value, "response_budget_exceeded")
            self.assertFalse(item.first_pass_schema_valid)
            self.assertIsNone(item.classification)  # never partly accepted
            self.assertIsNone(item.cited_count)
            self.assertFalse(item.rationale_unverified)
        with self.subTest("limit and limit + 1 counted raw"):
            self.assertFalse(bm.exceeds_scan_budget("a" * bm.MAX_SCANNED_PAYLOAD_CHARS))
            self.assertTrue(bm.exceeds_scan_budget("a" * (bm.MAX_SCANNED_PAYLOAD_CHARS + 1)))
            # NFKC shrinks "e" + U+0301 to one character: the raw count still applies.
            self.assertTrue(bm.exceeds_scan_budget("é" * (bm.MAX_SCANNED_PAYLOAD_CHARS // 2 + 1)))

    def test_gate_order_is_unchanged(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        with self.subTest("within budget, risk text is still RISK_AUTHORITY_REJECTED"):
            risky = answer(case, rationale="Stop at 97. " + "a" * 4000)
            self.assertFalse(bm.exceeds_scan_budget(risky))
            _, item = self.run_one("hold-positive-001", reply(risky))
            self.assertIs(item.outcome, CaseOutcome.RISK_AUTHORITY_REJECTED)
        with self.subTest("the 14 risk payloads and the 94 end-to-end phrases stay within the budget"):
            phrases = tuple(
                dict.fromkeys(TestPerCaseGates.ATTEMPT1_PHRASES + TestPerCaseGates.ATTEMPT2_PHRASES + TestPerCaseGates.OWN_VARIANTS)
            )
            for text in phrases:
                self.assertFalse(bm.exceeds_scan_budget(answer(case, rationale=text)))
            self.assertFalse(bm.exceeds_scan_budget({**answer(case), "legs": [{"position_size": 0.1}]}))
        with self.subTest("the output cap still decides before the budget"):
            _, item = self.run_one(
                "hold-positive-001", reply(answer(case, rationale="a" * 20_000), output_tokens=769)
            )
            self.assertIs(item.outcome, CaseOutcome.OUTPUT_CAP_EXCEEDED)
        with self.subTest("the budget decides before the risk gate reads anything"):
            over = answer(case, rationale=list(self.SECURITY_WORST))
            real_gate = bm.invades_risk_domain
            read: list[object] = []

            def watched_gate(payload: object, allowed_ids: Sequence[str] = (), _depth: int = 0) -> bool:
                read.append(payload)  # recursive calls also land here: the patch is module-wide
                return real_gate(payload, allowed_ids, _depth)

            with mock.patch.object(bm, "invades_risk_domain", side_effect=watched_gate):
                _, item = self.run_one("hold-positive-001", reply(over))
            self.assertIs(item.outcome, CaseOutcome.RESPONSE_BUDGET_EXCEEDED)
            self.assertTrue(read)  # the five valid cases were still read by the risk gate
            self.assertFalse(any(payload is over or payload is over["rationale"] for payload in read))
        source = inspect.getsource(bm.evaluate_case)
        self.assertLess(source.index("exceeds_scan_budget("), source.index("invades_risk_domain("))
        self.assertLess(source.index("invades_risk_domain("), source.index("parse_screener_answer("))
        self.assertLess(source.index("OUTPUT_CAP_EXCEEDED"), source.index("exceeds_scan_budget("))

    def test_security_reviewer_worst_case_is_fast(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        shapes = {
            "16 strings in one list": answer(case, rationale=list(self.SECURITY_WORST)),
            "16 extra string values": {**answer(case), **{f"note{i}": text for i, text in enumerate(self.SECURITY_WORST)}},
            "16 strings as keys": {**answer(case), **{text + chr(0x41 + i): 1 for i, text in enumerate(self.SECURITY_WORST)}},
        }
        for name, payload in shapes.items():
            with self.subTest(name):
                started = time.perf_counter()
                refused = bm.exceeds_scan_budget(payload)
                elapsed = time.perf_counter() - started
                self.assertTrue(refused)
                self.assertLess(elapsed, self.REDOS_REFUSED_UNREAD_SECONDS)
                started = time.perf_counter()
                report, item = self.run_one("hold-positive-001", reply(payload))
                elapsed = time.perf_counter() - started
                self.assertIs(item.outcome, CaseOutcome.RESPONSE_BUDGET_EXCEEDED)
                self.assertLess(elapsed, self.REDOS_REFUSED_UNREAD_SECONDS)
        with self.subTest("the linear scan alone (T050c1a) is also under the ReDoS limit"):
            started = time.perf_counter()
            bm.invades_risk_domain(shapes["16 strings in one list"], case.evidence_ids)
            self.assertLess(time.perf_counter() - started, self.REDOS_WITHIN_CAP_SECONDS)

    def test_hostile_shapes_are_refused_without_a_long_walk(self) -> None:
        deep: list[object] = []
        node = deep
        for _ in range(200_000):  # far past the recursion limit
            child: list[object] = []
            node.append(child)
            node = child
        cyclic: list[object] = []
        cyclic.append(cyclic)
        hostile = {
            "deep nesting": deep,
            "cycle": cyclic,
            "many empty strings": [""] * (bm.MAX_SCANNED_PAYLOAD_CHARS + 1),
            "many numbers": list(range(1_000_000)),
            "wide object": {f"k{i}": None for i in range(bm.MAX_SCANNED_PAYLOAD_CHARS // 2 + 1)},
        }
        for name, payload in hostile.items():
            with self.subTest(name):
                started = time.perf_counter()
                self.assertTrue(bm.exceeds_scan_budget(payload))
                self.assertLess(time.perf_counter() - started, self.REDOS_REFUSED_UNREAD_SECONDS)
        self.assertFalse(bm.exceeds_scan_budget([""] * (bm.MAX_SCANNED_PAYLOAD_CHARS - 1)))  # 1 + (budget - 1)
        self.assertFalse(bm.exceeds_scan_budget(None))
        self.assertFalse(bm.exceeds_scan_budget({1: "non-string key"}))  # the risk gate refuses it, not the budget

    def test_budget_rejection_counts_in_the_denominator_and_blocks_promotion(self) -> None:
        case = by_id(holdout(), "hold-positive-001")
        over = answer(case, rationale=list(self.SECURITY_WORST))
        report, item = self.run_one("hold-positive-001", reply(over))
        self.assertIs(item.outcome, CaseOutcome.RESPONSE_BUDGET_EXCEEDED)
        self.assertEqual((report.denominator, report.accepted, report.first_pass_schema_valid), (6, 5, 5))
        self.assertEqual(report.outcome_counts[CaseOutcome.RESPONSE_BUDGET_EXCEEDED], 1)
        self.assertIn(BlockReason.HARD_LIMIT_FAILURES, report.block_reasons)
        self.assertIn(BlockReason.FIRST_PASS_SCHEMA_BELOW_99, report.block_reasons)
        self.assertIs(report.promotion, PromotionStatus.BLOCKED)
        document = json.loads(report_json(report))
        self.assertEqual(document["outcome_counts"]["response_budget_exceeded"], 1)
        self.assertEqual(document["denominator"], 6)
        self.assertNotIn(self.SECURITY_WORST[0][:64], report_json(report))  # no model text in the report

    def test_report_is_byte_identical_for_the_same_input(self) -> None:
        partition = holdout()

        def script() -> dict[str, tuple[object, object]]:
            table = valid_script(partition)
            over_case = by_id(partition, "hold-no-edge-001")
            table[over_case.user] = (reply(answer(over_case, rationale=list(self.SECURITY_WORST))), OK_RESOURCES)
            return table

        first_model = FakeModel(script())
        first = run_benchmark(partition, default_profile(), first_model, first_model)
        shuffled_cases = list(partition.cases)
        random.Random(11).shuffle(shuffled_cases)
        shuffled = LockedPartition(
            partition.corpus_id, partition.lock_sha256, partition.synthetic, partition.partition, tuple(shuffled_cases)
        )
        second_model = FakeModel(script())
        second = run_benchmark(shuffled, default_profile(), second_model, second_model)
        self.assertEqual(report_json(first).encode("utf-8"), report_json(second).encode("utf-8"))
        self.assertEqual(report_sha256(first), report_sha256(second))
        self.assertEqual(
            {outcome.value: count for outcome, count in first.outcome_counts.items() if count},
            {"accepted": 5, "response_budget_exceeded": 1},
        )


class TestContextAndRoleGates(unittest.TestCase):
    def synthetic_case(self, user: str, role: Role = Role.SCREENER) -> BenchmarkCase:
        template = by_id(holdout(), "hold-positive-001")
        return BenchmarkCase(
            case_id="hold-positive-001",
            partition=CorpusPartition.HOLDOUT,
            category=template.category,
            role=role,
            system=template.system,
            user=user,
            evidence_ids=template.evidence_ids,
            gold=DeterministicGold(GoldSource.DETERMINISTIC_FIXTURE, abstain_expected=False),
            case_sha256="0" * 64,
        )

    def run_single(self, case: BenchmarkCase, profile: BenchmarkProfile | None = None):
        partition = LockedPartition("synthetic-harness-v1", LOCK_SHA256, True, CorpusPartition.HOLDOUT, (case,))
        model = FakeModel({case.user: (reply(answer(case)), OK_RESOURCES)})
        report = run_benchmark(partition, profile or default_profile(), model, model)
        return report.cases[0], model

    def test_context_bound_counts_utf8_bytes_and_never_truncates(self) -> None:
        base = prompt_token_bound(self.synthetic_case("x"))
        fitting = self.synthetic_case("x" * (1 + 2800 - base))
        self.assertEqual(prompt_token_bound(fitting), 2800)
        item, model = self.run_single(fitting)
        self.assertIs(item.outcome, CaseOutcome.ACCEPTED)
        self.assertEqual(model.calls[0].user, fitting.user)
        over = self.synthetic_case("x" * (2800 - base) + "é")  # one character, two UTF-8 bytes
        self.assertEqual(prompt_token_bound(over), 2801)
        item, model = self.run_single(over)
        self.assertIs(item.outcome, CaseOutcome.CONTEXT_UNFIT)
        self.assertEqual((item.prompt_token_bound, item.input_budget_tokens), (2801, 2800))
        self.assertEqual(model.calls, [])

    def test_smaller_profile_context_is_unfit_without_a_call(self) -> None:
        small = BenchmarkProfile(
            InferenceProfile("screener-small", "qwen3:14b", Role.SCREENER, 30, 1536, 768), 1.5, 4.0
        )
        item, model = self.run_single(self.synthetic_case("short synthetic evidence"), small)
        self.assertIs(item.outcome, CaseOutcome.CONTEXT_UNFIT)
        self.assertEqual(item.input_budget_tokens, 768)
        self.assertEqual(model.calls, [])

    def test_role_mismatch_makes_no_call(self) -> None:
        item, model = self.run_single(self.synthetic_case("synthetic evidence", role=Role.CHALLENGER))
        self.assertIs(item.outcome, CaseOutcome.ROLE_MISMATCH)
        self.assertEqual(model.calls, [])

    def test_harness_refuses_inconsistent_inputs(self) -> None:
        case = self.synthetic_case("synthetic evidence")
        model = FakeModel({})
        for partition in (
            LockedPartition("c", LOCK_SHA256, True, CorpusPartition.HOLDOUT, (case, case)),
            LockedPartition("c", LOCK_SHA256, True, CorpusPartition.DEVELOPMENT, (case,)),
        ):
            with self.assertRaises(bm.BenchmarkConfigError):
                run_benchmark(partition, default_profile(), model, model)
        for bad in (math.nan, -0.1, True):
            with self.assertRaises(bm.BenchmarkConfigError):
                BenchmarkProfile(default_profile().inference, bad, 4.0)


class TestDeterminismAndReports(unittest.TestCase):
    def mixed_script(self, partition: LockedPartition) -> dict[str, tuple[object, object]]:
        script = valid_script(partition)
        script[by_id(partition, "hold-conflict-001").user] = (InferenceFailed(InferenceFailure.TIMEOUT), OK_RESOURCES)
        script[by_id(partition, "hold-no-edge-001").user] = (reply({"leverage": 3}), OK_RESOURCES)
        script[by_id(partition, "hold-positive-002").user] = (
            reply(answer(by_id(partition, "hold-positive-002"))),
            ResourceReport(oom=True, min_free_vram_gib=0.2, min_free_ram_gib=9.0),
        )
        return script

    def test_same_input_gives_the_same_report(self) -> None:
        partition = holdout()
        first_model = FakeModel(self.mixed_script(partition))
        first = run_benchmark(partition, default_profile(), first_model, first_model)
        shuffled_cases = list(partition.cases)
        random.Random(7).shuffle(shuffled_cases)
        shuffled = LockedPartition(
            partition.corpus_id, partition.lock_sha256, partition.synthetic, partition.partition, tuple(shuffled_cases)
        )
        second_model = FakeModel(self.mixed_script(partition))
        second = run_benchmark(shuffled, default_profile(), second_model, second_model)
        self.assertEqual(report_json(first), report_json(second))
        self.assertEqual(report_sha256(first), report_sha256(second))
        self.assertEqual(
            {outcome.value: count for outcome, count in first.outcome_counts.items() if count},
            {"accepted": 3, "timeout": 1, "risk_authority_rejected": 1, "oom": 1},
        )
        self.assertEqual((first.denominator, first.first_pass_schema_valid), (6, 3))

    def test_report_carries_no_model_text(self) -> None:
        partition = holdout()
        script = valid_script(partition)
        secret = "UNIQUE-RATIONALE-MARKER"
        case = by_id(partition, "hold-positive-001")
        script[case.user] = (reply(answer(case, rationale=secret)), OK_RESOURCES)
        model = FakeModel(script)
        self.assertNotIn(secret, report_json(run_benchmark(partition, default_profile(), model, model)))

    def test_reports_are_written_only_to_the_callers_directory(self) -> None:
        partition = holdout()
        model = FakeModel(valid_script(partition))
        report = run_benchmark(partition, default_profile(), model, model)
        with tempfile.TemporaryDirectory(prefix="t050b-report-") as directory:
            path = write_report(report, directory)
            self.assertEqual(path.parent, Path(directory).resolve())
            self.assertEqual([entry.name for entry in Path(directory).iterdir()], [path.name])
            self.assertEqual(path.read_bytes(), report_json(report).encode("utf-8"))
            self.assertEqual(path.name, f"benchmark-holdout-{report_sha256(report)[:16]}.json")
            with self.assertRaises(CorpusError) as caught:
                write_report(report, directory)
            self.assertIs(caught.exception.code, CorpusErrorCode.OUTPUT_EXISTS)
            missing = Path(directory) / "missing"
            refused = (REPOSITORY_ROOT, REPOSITORY_ROOT / "tests" / "fixtures", CORPUS, missing, path)
            for target in refused:
                with self.subTest(os.fspath(target)):
                    with self.assertRaises(CorpusError) as caught:
                        write_report(report, target)
                    self.assertIs(caught.exception.code, CorpusErrorCode.OUTPUT_DIR_REFUSED)
        self.assertIs(inspect.signature(write_report).parameters["output_dir"].default, inspect.Parameter.empty)
        self.assertEqual(sorted(entry.name for entry in CORPUS.iterdir()), ["development", "holdout", "lock.json"])


# -- isolation ---------------------------------------------------------------------------------


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            names.add(prefix)
            names.update(f"{prefix}.{alias.name}" for alias in node.names)
    return names


def identifiers(path: Path) -> set[str]:
    """Names, attributes and string constants used in code (docstrings excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.alias):
            used.update(node.name.split("."))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            used.add(node.value)
    return used


class TestHarnessCannotReachAModel(unittest.TestCase):
    FORBIDDEN = ("requests", "socket", "http", "urllib", "ssl", "subprocess", "os", "pathlib", "time", "datetime",
                 "sqlite3", "ollama", "radar_v08.adapters", "radar_v08.qwen", "radar_v08.config", "radar_v08.http_client")

    def test_benchmark_module_imports(self) -> None:
        modules = imported_modules(BENCHMARK_SOURCE)
        bad = sorted(name for name in modules if any(name == f or name.startswith(f + ".") for f in self.FORBIDDEN))
        self.assertEqual(bad, [])
        self.assertFalse([name for name in modules if name.startswith(".")])
        used = identifiers(BENCHMARK_SOURCE)
        for token in ("OllamaLocalInference", "local_inference", "__main__", "argparse", "open", "getenv", "environ", "print"):
            self.assertNotIn(token, used)

    def test_benchmark_import_loads_no_network_module(self) -> None:
        # Text/AST scan of radar_v08/config.py (never imported here, only its source read):
        # every os.getenv("RADAR_..._PATH", ...) call site. This is how the child process's
        # environment below is built, so a future RADAR_*_PATH is covered automatically.
        config_source = (REPOSITORY_ROOT / "radar_v08" / "config.py").read_text(encoding="utf-8")
        config_tree = ast.parse(config_source, filename="radar_v08/config.py")
        path_env_vars: set[str] = set()
        for node in ast.walk(config_tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and re.fullmatch(r"RADAR_[A-Z0-9_]*_PATH", node.args[0].value)
            ):
                path_env_vars.add(node.args[0].value)
        minimum_expected = {"RADAR_EVENTS_LOG_PATH", "RADAR_OUTPUT_V08_PATH", "RADAR_SQLITE_PATH"}
        self.assertTrue(
            minimum_expected <= path_env_vars,
            f"scan of radar_v08/config.py missed some of {minimum_expected}: found {sorted(path_env_vars)}",
        )

        code = (
            "import sys; import radar_v08.workflow.benchmark; "
            "print(sorted(m for m in ('requests', 'socket', 'urllib3', 'http.client', "
            "'radar_v08.adapters.local_inference', 'radar_v08.qwen') if m in sys.modules))"
        )
        with tempfile.TemporaryDirectory(prefix="t050c-child-") as state:
            environment = {k: v for k, v in os.environ.items() if not k.startswith("RADAR_")}
            environment.update(RADAR_STATE_DIR=state, PYTHONDONTWRITEBYTECODE="1")
            for name in sorted(path_env_vars):  # every RADAR_*_PATH found above, not just the minimum
                environment[name] = os.path.join(state, name)
            completed = subprocess.run(
                [sys.executable, "-c", code], cwd=REPOSITORY_ROOT, env=environment, capture_output=True, text=True, timeout=60
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "[]")

    def test_corpus_adapter_has_no_network_or_model_path(self) -> None:
        modules = imported_modules(CORPUS_SOURCE)
        for name in ("requests", "socket", "urllib", "http", ".local_inference", "radar_v08.qwen", "subprocess"):
            self.assertFalse([m for m in modules if m == name or m.startswith(name + ".")], name)
        used = identifiers(CORPUS_SOURCE)
        for token in ("OllamaLocalInference", "loopback_base_url", "__main__", "argparse", "getenv", "environ", "print"):
            self.assertNotIn(token, used)

    def test_no_entry_point_reaches_the_harness(self) -> None:
        for package in ("workflow", "adapters"):
            self.assertFalse((REPOSITORY_ROOT / "radar_v08" / package / "__main__.py").exists())
        self.assertFalse((REPOSITORY_ROOT / "radar_v08" / "__main__.py").exists())
        self.assertNotIn("benchmark", (REPOSITORY_ROOT / "radar_v08" / "cli.py").read_text(encoding="utf-8"))
        self.assertNotIn("[project.scripts]", (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        for script in sorted((REPOSITORY_ROOT / "scripts").glob("*.py")):
            with self.subTest(script.name):
                self.assertNotIn("benchmark", script.read_text(encoding="utf-8"))

    def test_a_full_run_opens_no_socket(self) -> None:
        partition = holdout()
        model = FakeModel(valid_script(partition))

        def refuse(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError("network access attempted")

        with mock.patch.object(socket, "socket", refuse), mock.patch.object(socket, "create_connection", refuse):
            report = run_benchmark(partition, default_profile(), model, model)
        self.assertEqual(report.accepted, 6)
        self.assertNotIsInstance(model, type(None))
        self.assertEqual(type(model).__name__, "FakeModel")


class TestSchemaHelpers(unittest.TestCase):
    def test_parse_valid_answer(self) -> None:
        parsed = parse_screener_answer(
            {"abstain": True, "classification": "invalid_or_stale", "cited_evidence_ids": ["ev-h1a"], "rationale": "stale"}
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual((parsed.abstain, parsed.classification, parsed.cited_evidence_ids), (True, CaseCategory.INVALID_OR_STALE, ("ev-h1a",)))

    def test_output_schema_matches_the_validator(self) -> None:
        self.assertEqual(set(SCREENER_OUTPUT_SCHEMA["required"]), set(SCREENER_OUTPUT_SCHEMA["properties"]))  # type: ignore[arg-type]
        self.assertIs(SCREENER_OUTPUT_SCHEMA["additionalProperties"], False)
        self.assertEqual(SCREENER_OUTPUT_SCHEMA["properties"]["classification"]["enum"], [c.value for c in CaseCategory])  # type: ignore[index]
        risk_names = {name for name in SCREENER_OUTPUT_SCHEMA["properties"] if invades_risk_domain({name: None})}  # type: ignore[union-attr]
        self.assertEqual(risk_names, set())


if __name__ == "__main__":
    unittest.main()
