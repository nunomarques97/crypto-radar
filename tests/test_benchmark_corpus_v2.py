"""T051a / D64: additive corpus schema v2 in adapters/benchmark_corpus.py and workflow/benchmark.py.

Hand-built v2 corpora in temporary directories only (D31). The v1 fixtures and their tests
(tests/test_benchmark_harness.py) are untouched; the v1 checks here only prove that the
added GoldSource members are refused in a v1 case and that v1 and v2 files never mix.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(REPOSITORY_ROOT))

from radar_v08.adapters.benchmark_corpus import (  # noqa: E402
    CorpusError,
    CorpusErrorCode,
    CorpusMode,
    benchmark_profile,
    canonical_sha256,
    load_partition,
    prompt_fingerprint,
)
from radar_v08.adapters.model_profiles import load_model_profiles  # noqa: E402
from radar_v08.workflow.benchmark import (  # noqa: E402
    CaseCategory,
    GoldSource,
    ResourceReport,
    report_as_dict,
    run_benchmark,
)
from radar_v08.workflow.worker import InferenceReply  # noqa: E402

FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "benchmark_corpus"
ZIP = "Kraken_OHLCVT_Q1_2024.zip"
ZIP_SHA = "ab" * 32
SLUG = {
    "invalid_or_stale": "invalid",
    "insufficient_evidence": "insufficient",
    "admissible_no_edge": "no-edge",
    "admissible_positive": "positive",
    "conflicting_evidence": "conflict",
}
GOLD = {"invalid_or_stale": True, "insufficient_evidence": True, "admissible_no_edge": False}


def v2_case(partition: str, category: str, day: int) -> dict:
    prefix = "dev" if partition == "development" else "hold"
    gold = (
        {"source": "deterministic_rule", "rule": "rule:test", "abstain_expected": GOLD[category], "human_review": None}
        if category in GOLD
        else None
    )
    return {
        "schema_version": 2,
        "case_id": f"{prefix}-{SLUG[category]}-001",
        "partition": partition,
        "category": category,
        "role": "screener",
        "synthetic": False,
        "prompt": {"system": "Screener system text.", "user": f"Evidence for {partition} {category} on day {day}."},
        "evidence_ids": ["ev-bars", "ev-cost"],
        "gold": gold,
        "gold_status": "deterministic" if gold else "gold_unavailable",
        "construction": {"rule": "selection:test", "rules_version": "oc1-corpus-rules-1", "details": {"k": 1}},
        "provenance": {
            "venue": "kraken",
            "pair": "XBTUSD",
            "base": "XBT",
            "quote": "USD",
            "bar_seconds": 300,
            "window_start_utc": f"2024-01-{day:02d}T00:00:00Z",
            "window_end_utc": f"2024-01-{day:02d}T02:00:00Z",
            "decision_at_utc": f"2024-01-{day:02d}T02:00:00Z",
            "source": {"zip": ZIP, "zip_sha256": ZIP_SHA, "member": "XBTUSD_5.csv"},
            "fee": {
                "bps_per_leg": "26.0",
                "legs": 2,
                "basis": "uncalibrated_assumption",
                "origin_file": "radar_v08/config.py",
                "origin_key": "UNCALIBRATED_FEES['spot_taker_bps']",
            },
        },
    }


class Corpus:
    """A v2 corpus on disk: one case per category and partition (days 1-5 and 20-24)."""

    def __init__(self, test: unittest.TestCase) -> None:
        temp = tempfile.TemporaryDirectory(prefix="t051a-v2-")
        test.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cases = {"development": [], "holdout": []}
        for index, category in enumerate(SLUG):
            self.cases["development"].append(v2_case("development", category, 1 + index))
            self.cases["holdout"].append(v2_case("holdout", category, 20 + index))
        self.separation = {
            "development_end_utc": "2024-01-05T02:00:00Z",
            "holdout_start_utc": "2024-01-20T00:00:00Z",
            "gap_seconds": 14 * 86400 + 22 * 3600,
            "min_gap_seconds": 604800,
        }
        self.builder = {"rules_version": "oc1-corpus-rules-1", "seed": 20260919, "source_zips": [{"zip": ZIP, "sha256": ZIP_SHA}]}

    def lock(self) -> dict:
        partitions = {}
        for partition, cases in self.cases.items():
            table = {
                case["case_id"]: {
                    "sha256": canonical_sha256(case),
                    "prompt_sha256": prompt_fingerprint(case["prompt"]["system"], case["prompt"]["user"], case["evidence_ids"]),
                }
                for case in cases
            }
            partitions[partition] = {"partition_sha256": canonical_sha256(table), "cases": table}
        return {
            "schema_version": 2,
            "corpus_id": "oc1-test-v2",
            "synthetic": False,
            "partitions": partitions,
            "separation": self.separation,
            "builder": self.builder,
        }

    def write(self, lock: dict | None = None) -> str:
        lock = self.lock() if lock is None else lock
        for partition, cases in self.cases.items():
            directory = self.root / partition
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir()
            for case in cases:
                (directory / f"{case['case_id']}.json").write_text(json.dumps(case, indent=2), encoding="utf-8")
        (self.root / "lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
        return canonical_sha256(lock)

    def case(self, partition: str, category: str) -> dict:
        return next(case for case in self.cases[partition] if case["category"] == category)


class FakeModel:
    """Abstains on every case, and reports resources inside the envelope."""

    def infer(self, call, cancel):  # noqa: ANN001, ANN201
        payload = {"abstain": True, "classification": "insufficient_evidence", "cited_evidence_ids": [], "rationale": ""}
        return InferenceReply(payload=payload, prompt_tokens=10, output_tokens=10)

    def last_call_resources(self) -> ResourceReport:
        return ResourceReport(oom=False, min_free_vram_gib=100.0, min_free_ram_gib=100.0)


def default_profile():  # noqa: ANN201
    profiles = load_model_profiles()
    return benchmark_profile(profiles.get(profiles.default_profile_id))


class TestSchemaV2Loads(unittest.TestCase):
    def test_v2_corpus_loads_with_rule_gold_and_unavailable_gold(self) -> None:
        corpus = Corpus(self)
        pinned = corpus.write()
        holdout = load_partition(corpus.root, CorpusMode.HOLDOUT, expected_lock_sha256=pinned)
        development = load_partition(corpus.root, CorpusMode.DEVELOPMENT, expected_lock_sha256=pinned)
        self.assertEqual((len(development.cases), len(holdout.cases)), (5, 5))
        self.assertEqual(holdout.lock_sha256, pinned)
        by_category = {case.category.value: case for case in holdout.cases}
        for category, abstain in GOLD.items():
            self.assertIs(by_category[category].gold.source, GoldSource.DETERMINISTIC_RULE)
            self.assertIs(by_category[category].gold.abstain_expected, abstain)
        for category in ("admissible_positive", "conflicting_evidence"):
            self.assertIs(by_category[category].gold.source, GoldSource.GOLD_UNAVAILABLE)
            self.assertIsNone(by_category[category].gold.abstain_expected)

    def test_null_abstention_is_outside_both_denominators(self) -> None:
        corpus = Corpus(self)
        pinned = corpus.write()
        holdout = load_partition(corpus.root, CorpusMode.HOLDOUT, expected_lock_sha256=pinned)
        model = FakeModel()
        report = run_benchmark(holdout, default_profile(), model, model)
        # Recall: invalid + insufficient expect abstention (2), both abstained. False abstention:
        # only no_edge expects an answer (1) and it abstained. Positive/conflict count nowhere.
        self.assertEqual(report.abstention_recall, (2, 2))
        self.assertEqual(report.false_abstention, (1, 1))
        expected = {item["category"]: item["abstain_expected"] for item in report_as_dict(report)["cases"]}
        self.assertEqual(
            expected,
            {
                "invalid_or_stale": True,
                "insufficient_evidence": True,
                "admissible_no_edge": False,
                "admissible_positive": None,
                "conflicting_evidence": None,
            },
        )

    def test_development_mode_never_reads_the_holdout(self) -> None:
        corpus = Corpus(self)
        pinned = corpus.write()
        for path in (corpus.root / "holdout").iterdir():
            path.write_bytes(b"\xff garbage")
        (corpus.root / "holdout" / "extra.bin").write_bytes(b"x")
        self.assertEqual(len(load_partition(corpus.root, CorpusMode.DEVELOPMENT, expected_lock_sha256=pinned).cases), 5)


class TestSchemaV2Refusals(unittest.TestCase):
    def assertRefused(self, code: CorpusErrorCode, corpus: Corpus, mode: CorpusMode, lock: dict | None = None) -> None:  # noqa: N802
        pinned = corpus.write(lock)
        with self.assertRaises(CorpusError) as caught:
            load_partition(corpus.root, mode, expected_lock_sha256=pinned)
        self.assertIs(caught.exception.code, code, str(caught.exception))

    def test_gold_rules_per_category(self) -> None:
        edits = {
            "null gold in no_edge": ("admissible_no_edge", lambda case: case.update(gold=None, gold_status="gold_unavailable")),
            "null gold in invalid": ("invalid_or_stale", lambda case: case.update(gold=None, gold_status="gold_unavailable")),
            "null gold in insufficient": ("insufficient_evidence", lambda case: case.update(gold=None, gold_status="gold_unavailable")),
            "gold status without gold": ("admissible_no_edge", lambda case: case.update(gold_status="gold_unavailable")),
            "gold in positive": (
                "admissible_positive",
                lambda case: case.update(
                    gold={"source": "deterministic_rule", "rule": "r", "abstain_expected": False, "human_review": None},
                    gold_status="deterministic",
                ),
            ),
            "status deterministic without gold": ("conflicting_evidence", lambda case: case.update(gold_status="deterministic")),
            "fixture source in v2": ("admissible_no_edge", lambda case: case["gold"].update(source="deterministic_fixture")),
            "llm source": ("admissible_no_edge", lambda case: case["gold"].update(source="llm_judge")),
            "unavailable source as gold": ("admissible_no_edge", lambda case: case["gold"].update(source="gold_unavailable")),
            "abstention against category": ("admissible_no_edge", lambda case: case["gold"].update(abstain_expected=True)),
            "null abstention with gold": ("invalid_or_stale", lambda case: case["gold"].update(abstain_expected=None)),
            "human review present": ("invalid_or_stale", lambda case: case["gold"].update(human_review={"a": 1})),
            "unknown case key": ("invalid_or_stale", lambda case: case.update(profit_label=1)),
            "missing provenance": ("invalid_or_stale", lambda case: case.pop("provenance")),
            "zip not in the lock": ("invalid_or_stale", lambda case: case["provenance"]["source"].update(zip_sha256="cd" * 32)),
            "member of another pair": ("invalid_or_stale", lambda case: case["provenance"]["source"].update(member="ETHUSD_5.csv")),
            "window after decision": ("invalid_or_stale", lambda case: case["provenance"].update(window_end_utc="2024-01-01T03:00:00Z")),
            "bad timestamp": ("invalid_or_stale", lambda case: case["provenance"].update(window_start_utc="2024-01-01 00:00")),
            "rules version": ("invalid_or_stale", lambda case: case["construction"].update(rules_version="other")),
            "fee legs": ("invalid_or_stale", lambda case: case["provenance"]["fee"].update(legs=1)),
            "development case after the development end": (
                "invalid_or_stale",
                lambda case: case["provenance"].update(decision_at_utc="2024-01-06T00:00:00Z"),
            ),
            "v1 schema case under a v2 lock": ("invalid_or_stale", lambda case: case.update(schema_version=1)),
        }
        for name, (category, edit) in edits.items():
            with self.subTest(name):
                corpus = Corpus(self)
                edit(corpus.case("development", category))
                self.assertRefused(CorpusErrorCode.CASE_INVALID, corpus, CorpusMode.DEVELOPMENT)

    def test_holdout_case_before_the_holdout_start_is_refused(self) -> None:
        corpus = Corpus(self)
        corpus.case("holdout", "admissible_positive")["provenance"]["window_start_utc"] = "2024-01-19T00:00:00Z"
        self.assertRefused(CorpusErrorCode.CASE_INVALID, corpus, CorpusMode.HOLDOUT)

    def test_separation_is_checked_in_the_lock(self) -> None:
        edits = {
            "gap below seven days": lambda s: s.update(holdout_start_utc="2024-01-11T00:00:00Z", gap_seconds=5 * 86400 + 22 * 3600),
            "gap does not match": lambda s: s.update(gap_seconds=8 * 86400),
            "minimum changed": lambda s: s.update(min_gap_seconds=86400),
            "bad timestamp": lambda s: s.update(development_end_utc="yesterday"),
            "unknown key": lambda s: s.update(note="x"),
        }
        for name, edit in edits.items():
            with self.subTest(name):
                corpus = Corpus(self)
                edit(corpus.separation)
                self.assertRefused(CorpusErrorCode.LOCK_INVALID, corpus, CorpusMode.DEVELOPMENT)
        with self.subTest("duplicate source zip"):
            corpus = Corpus(self)
            corpus.builder["source_zips"].append({"zip": ZIP, "sha256": ZIP_SHA})
            self.assertRefused(CorpusErrorCode.LOCK_INVALID, corpus, CorpusMode.DEVELOPMENT)
        with self.subTest("missing separation"):
            corpus = Corpus(self)
            lock = corpus.lock()
            lock.pop("separation")
            self.assertRefused(CorpusErrorCode.LOCK_INVALID, corpus, CorpusMode.DEVELOPMENT, lock)

    def test_hashes_still_lock_v2_cases(self) -> None:
        corpus = Corpus(self)
        pinned = corpus.write()
        path = corpus.root / "holdout" / "hold-positive-001.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["prompt"]["user"] += " edited"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(CorpusError) as caught:
            load_partition(corpus.root, CorpusMode.HOLDOUT, expected_lock_sha256=pinned)
        self.assertIs(caught.exception.code, CorpusErrorCode.HOLDOUT_EDITED)
        corpus = Corpus(self)
        corpus.write()
        with self.assertRaises(CorpusError) as caught:
            load_partition(corpus.root, CorpusMode.DEVELOPMENT, expected_lock_sha256="0" * 64)
        self.assertIs(caught.exception.code, CorpusErrorCode.LOCK_CHANGED)


class TestV1Unchanged(unittest.TestCase):
    def copy_fixtures(self) -> Path:
        temp = tempfile.TemporaryDirectory(prefix="t051a-v1-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "corpus"
        shutil.copytree(FIXTURES, root)
        return root

    def relock(self, root: Path) -> str:
        lock = json.loads((root / "lock.json").read_text(encoding="utf-8"))
        for partition in ("development", "holdout"):
            table = lock["partitions"][partition]["cases"]
            for case_id in table:
                case = json.loads((root / partition / f"{case_id}.json").read_text(encoding="utf-8"))
                table[case_id]["sha256"] = canonical_sha256(case)
            lock["partitions"][partition]["partition_sha256"] = canonical_sha256(table)
        (root / "lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
        return canonical_sha256(lock)

    def test_new_gold_sources_are_refused_in_a_v1_case(self) -> None:
        for source in ("deterministic_rule", "gold_unavailable"):
            with self.subTest(source):
                root = self.copy_fixtures()
                path = root / "development" / "dev-no-edge-001.json"
                document = json.loads(path.read_text(encoding="utf-8"))
                document["gold"]["source"] = source
                path.write_text(json.dumps(document), encoding="utf-8")
                pinned = self.relock(root)
                with self.assertRaises(CorpusError) as caught:
                    load_partition(root, CorpusMode.DEVELOPMENT, expected_lock_sha256=pinned)
                self.assertIs(caught.exception.code, CorpusErrorCode.CASE_INVALID)

    def test_v2_case_under_a_v1_lock_is_refused(self) -> None:
        root = self.copy_fixtures()
        path = root / "development" / "dev-no-edge-001.json"
        document = v2_case("development", "admissible_no_edge", 1)
        document["case_id"] = "dev-no-edge-001"
        document["synthetic"] = True
        path.write_text(json.dumps(document), encoding="utf-8")
        pinned = self.relock(root)
        with self.assertRaises(CorpusError) as caught:
            load_partition(root, CorpusMode.DEVELOPMENT, expected_lock_sha256=pinned)
        self.assertIs(caught.exception.code, CorpusErrorCode.CASE_INVALID)

    def test_v1_fixtures_still_load_unchanged(self) -> None:
        lock = json.loads((FIXTURES / "lock.json").read_text(encoding="utf-8"))
        partition = load_partition(FIXTURES, CorpusMode.HOLDOUT, expected_lock_sha256=canonical_sha256(lock))
        self.assertEqual(len(partition.cases), 6)
        self.assertEqual({case.gold.source for case in partition.cases}, {GoldSource.DETERMINISTIC_FIXTURE})
        self.assertEqual({type(case.gold.abstain_expected) for case in partition.cases}, {bool})
        self.assertEqual({case.category for case in partition.cases}, set(CaseCategory))


if __name__ == "__main__":
    unittest.main()
