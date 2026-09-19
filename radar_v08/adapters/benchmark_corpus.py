"""Locked benchmark corpus loader and report writer (T050b, OC section 6, D33/D38).

Reads a corpus directory laid out as::

    <corpus_root>/lock.json                 manifest: sha256 per case and per partition
    <corpus_root>/development/<case_id>.json
    <corpus_root>/holdout/<case_id>.json

and returns a verified ``workflow.benchmark.LockedPartition``. It fails closed: every
problem raises a typed ``CorpusError`` and nothing is returned.

Locking rules:

* Hashes are sha256 over canonical JSON (sorted keys, no whitespace, UTF-8), so a
  checkout that rewrites line endings does not change them, while any change of content
  does. ``case sha256`` covers the whole case file (including its partition and gold);
  ``prompt_sha256`` covers what the model sees (system, user, evidence ids) and makes
  cases non-overlapping; ``partition_sha256`` covers a partition's case table; the lock
  sha256 covers the whole manifest and must equal the value the caller pins
  (``expected_lock_sha256``), so the lock cannot be silently regenerated.
* Refused with a typed code: a case whose content hash differs (``CASE_HASH_MISMATCH``),
  a case found in, or labelled with, the other partition (``PARTITION_CHANGED``), any
  change to the holdout (``HOLDOUT_EDITED``: edited, added or removed case, or partition
  hash), a duplicated case id or prompt (``DUPLICATE_CASE``), a file not in the lock
  (``UNLOCKED_CASE``) and a changed manifest (``LOCK_CHANGED``).
* ``CorpusMode.DEVELOPMENT`` reads only ``lock.json`` and ``development/``: it never
  lists or opens anything under ``holdout/``, so development work cannot tune on the
  holdout. ``CorpusMode.HOLDOUT`` verifies both partitions and returns the holdout.
* Gold is deterministic only (``gold.source = "deterministic_fixture"``, expected
  abstention fixed by the category); ``gold.human_review`` must be ``null`` in schema
  v1: human gold (two independent reviews, adjudicated) has no format yet, so the
  harness reports ``GOLD_UNAVAILABLE`` and blocks promotion. No LLM gold exists.
* Files are opened ``"rb"``, size-capped, regular files only (no symlinks, no
  subdirectories); JSON is strict (no NaN/Infinity, no duplicate keys).

Reports are written only by ``write_report`` into a directory the caller passes; there
is no default path, and the repository (root or any directory inside it) is refused.
No socket, no model, no environment read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..workflow.benchmark import (
    BenchmarkCase,
    BenchmarkProfile,
    BenchmarkReport,
    CaseCategory,
    CorpusPartition,
    DeterministicGold,
    GoldSource,
    LockedPartition,
    report_json,
    report_sha256,
)
from ..workflow.scheduler import Role
from .model_profiles import ModelProfile

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
LOCK_FILE_NAME = "lock.json"
CORPUS_SCHEMA_VERSION = 1
MAX_LOCK_BYTES = 1_048_576
MAX_CASE_BYTES = 65_536
MAX_CASES_PER_PARTITION = 1000
MAX_PROMPT_CHARS = 32_768
MAX_EVIDENCE_IDS = 32

# Deterministic abstention label by category; conflicting evidence is labelled per case.
ABSTAIN_BY_CATEGORY: Mapping[CaseCategory, bool] = {
    CaseCategory.INVALID_OR_STALE: True,
    CaseCategory.INSUFFICIENT_EVIDENCE: True,
    CaseCategory.ADMISSIBLE_POSITIVE: False,
    CaseCategory.ADMISSIBLE_NO_EDGE: False,
}

_CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_CORPUS_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_EVIDENCE_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOCK_KEYS = frozenset({"schema_version", "corpus_id", "synthetic", "partitions"})
_PARTITION_KEYS = frozenset({"partition_sha256", "cases"})
_LOCK_ENTRY_KEYS = frozenset({"sha256", "prompt_sha256"})
_CASE_KEYS = frozenset(
    {"schema_version", "case_id", "partition", "category", "role", "synthetic", "prompt", "evidence_ids", "gold"}
)
_PROMPT_KEYS = frozenset({"system", "user"})
_GOLD_KEYS = frozenset({"source", "abstain_expected", "human_review"})


class CorpusMode(Enum):
    DEVELOPMENT = "development"  # reads development only; the holdout is never touched
    HOLDOUT = "holdout"  # verifies both partitions, returns the holdout


class CorpusErrorCode(Enum):
    LOCK_MISSING = "lock_missing"
    LOCK_INVALID = "lock_invalid"
    LOCK_CHANGED = "lock_changed"
    FILE_UNREADABLE = "file_unreadable"
    FILE_TOO_LARGE = "file_too_large"
    UNEXPECTED_ENTRY = "unexpected_entry"
    CASE_INVALID = "case_invalid"
    CASE_HASH_MISMATCH = "case_hash_mismatch"
    PARTITION_HASH_MISMATCH = "partition_hash_mismatch"
    PARTITION_CHANGED = "partition_changed"
    HOLDOUT_EDITED = "holdout_edited"
    HOLDOUT_CATEGORY_MISSING = "holdout_category_missing"
    DUPLICATE_CASE = "duplicate_case"
    UNLOCKED_CASE = "unlocked_case"
    MISSING_CASE = "missing_case"
    OUTPUT_DIR_REFUSED = "output_dir_refused"
    OUTPUT_EXISTS = "output_exists"


class CorpusError(ValueError):
    """The corpus (or a write target) is not usable. Nothing is returned or written."""

    def __init__(self, code: CorpusErrorCode, where: str, detail: str = "") -> None:
        message = f"benchmark corpus refused: {code.value} at {where}"
        super().__init__(f"{message}: {detail}" if detail else message)
        self.code = code
        self.where = where


@dataclass(frozen=True, slots=True)
class _LockEntry:
    sha256: str
    prompt_sha256: str


@dataclass(frozen=True, slots=True)
class _Lock:
    corpus_id: str
    synthetic: bool
    lock_sha256: str
    entries: Mapping[CorpusPartition, Mapping[str, _LockEntry]]


def canonical_sha256(value: object) -> str:
    """sha256 of canonical JSON: sorted keys, no whitespace, UTF-8, finite numbers only."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_fingerprint(system: str, user: str, evidence_ids: list[str]) -> str:
    """What the model sees for a case; two cases with the same fingerprint overlap."""
    return canonical_sha256({"system": system, "user": user, "evidence_ids": sorted(evidence_ids)})


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-finite JSON constant {name}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_json(path: Path, limit: int, invalid: CorpusErrorCode) -> object:
    where = os.fspath(path)
    try:
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > limit:
                raise CorpusError(CorpusErrorCode.FILE_TOO_LARGE, where, f"over {limit} bytes")
            data = handle.read(limit + 1)
    except FileNotFoundError:
        missing = CorpusErrorCode.LOCK_MISSING if invalid is CorpusErrorCode.LOCK_INVALID else CorpusErrorCode.MISSING_CASE
        raise CorpusError(missing, where) from None
    except OSError as error:
        raise CorpusError(CorpusErrorCode.FILE_UNREADABLE, where, type(error).__name__) from None
    if len(data) > limit:
        raise CorpusError(CorpusErrorCode.FILE_TOO_LARGE, where, f"over {limit} bytes")
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise CorpusError(invalid, where, "not strict UTF-8 JSON") from None


def _table(value: object, keys: frozenset[str], code: CorpusErrorCode, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CorpusError(code, where, "expected an object")
    if set(value) != keys:
        raise CorpusError(code, where, "keys must be exactly " + ", ".join(sorted(keys)))
    return value


def _text(value: object, pattern: re.Pattern[str], code: CorpusErrorCode, where: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise CorpusError(code, where, "malformed value")
    return value


def _partition_named(name: str) -> CorpusPartition:
    return CorpusPartition(name)


def _parse_lock(document: object, where: str) -> _Lock:
    invalid = CorpusErrorCode.LOCK_INVALID
    top = _table(document, _LOCK_KEYS, invalid, where)
    if type(top["schema_version"]) is not int or top["schema_version"] != CORPUS_SCHEMA_VERSION:
        raise CorpusError(invalid, f"{where}.schema_version", "unsupported")
    corpus_id = _text(top["corpus_id"], _CORPUS_ID, invalid, f"{where}.corpus_id")
    synthetic = top["synthetic"]
    if type(synthetic) is not bool:
        raise CorpusError(invalid, f"{where}.synthetic", "expected a boolean")
    partitions = _table(top["partitions"], frozenset(p.value for p in CorpusPartition), invalid, f"{where}.partitions")
    entries: dict[CorpusPartition, dict[str, _LockEntry]] = {}
    seen_ids: dict[str, CorpusPartition] = {}
    seen_hashes: set[str] = set()
    seen_prompts: set[str] = set()
    declared_hashes: dict[CorpusPartition, tuple[str, str, str]] = {}
    for name in sorted(partitions):
        partition = _partition_named(name)
        part_where = f"{where}.partitions.{name}"
        table = _table(partitions[name], _PARTITION_KEYS, invalid, part_where)
        declared = _text(table["partition_sha256"], _SHA256, invalid, f"{part_where}.partition_sha256")
        cases = table["cases"]
        if not isinstance(cases, dict) or not cases or len(cases) > MAX_CASES_PER_PARTITION:
            raise CorpusError(invalid, f"{part_where}.cases", f"expected 1..{MAX_CASES_PER_PARTITION} cases")
        parsed: dict[str, _LockEntry] = {}
        for case_id in sorted(cases):
            entry_where = f"{part_where}.cases.{case_id}"
            _text(case_id, _CASE_ID, invalid, entry_where)
            entry = _table(cases[case_id], _LOCK_ENTRY_KEYS, invalid, entry_where)
            sha = _text(entry["sha256"], _SHA256, invalid, f"{entry_where}.sha256")
            prompt_sha = _text(entry["prompt_sha256"], _SHA256, invalid, f"{entry_where}.prompt_sha256")
            if case_id in seen_ids:
                raise CorpusError(CorpusErrorCode.DUPLICATE_CASE, entry_where, f"also in {seen_ids[case_id].value}")
            if sha in seen_hashes or prompt_sha in seen_prompts:
                raise CorpusError(CorpusErrorCode.DUPLICATE_CASE, entry_where, "same content as another case")
            seen_ids[case_id] = partition
            seen_hashes.add(sha)
            seen_prompts.add(prompt_sha)
            parsed[case_id] = _LockEntry(sha256=sha, prompt_sha256=prompt_sha)
        entries[partition] = parsed
        declared_hashes[partition] = (declared, canonical_sha256(cases), part_where)
    # Partition hashes after every entry is parsed, so a duplicate is reported as such.
    for partition, (declared, actual, part_where) in declared_hashes.items():
        if actual != declared:
            code = (
                CorpusErrorCode.HOLDOUT_EDITED if partition is CorpusPartition.HOLDOUT else CorpusErrorCode.PARTITION_HASH_MISMATCH
            )
            raise CorpusError(code, f"{part_where}.partition_sha256")
    return _Lock(corpus_id=corpus_id, synthetic=synthetic, lock_sha256=canonical_sha256(document), entries=entries)


def _scan(directory: Path, partition: CorpusPartition) -> dict[str, Path]:
    """Regular ``<case_id>.json`` files in ``directory``; anything else is refused."""
    where = os.fspath(directory)
    found: dict[str, Path] = {}
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                stem = entry.name[: -len(".json")] if entry.name.endswith(".json") else ""
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False) or _CASE_ID.fullmatch(stem) is None:
                    raise CorpusError(CorpusErrorCode.UNEXPECTED_ENTRY, os.fspath(Path(directory, entry.name)))
                found[stem] = Path(directory, entry.name)
                if len(found) > MAX_CASES_PER_PARTITION:
                    raise CorpusError(CorpusErrorCode.FILE_TOO_LARGE, where, f"over {MAX_CASES_PER_PARTITION} cases")
    except FileNotFoundError:
        return {}
    except NotADirectoryError:
        raise CorpusError(CorpusErrorCode.UNEXPECTED_ENTRY, where, "not a directory") from None
    except OSError as error:
        raise CorpusError(CorpusErrorCode.FILE_UNREADABLE, where, type(error).__name__) from None
    return found


def _case(document: object, path: Path, partition: CorpusPartition, lock: _Lock) -> tuple[BenchmarkCase, str]:
    where = os.fspath(path)
    invalid = CorpusErrorCode.CASE_INVALID
    top = _table(document, _CASE_KEYS, invalid, where)
    if type(top["schema_version"]) is not int or top["schema_version"] != CORPUS_SCHEMA_VERSION:
        raise CorpusError(invalid, f"{where}.schema_version", "unsupported")
    case_id = _text(top["case_id"], _CASE_ID, invalid, f"{where}.case_id")
    if path.name != f"{case_id}.json":
        raise CorpusError(invalid, f"{where}.case_id", "must match the file name")
    if top["partition"] != partition.value:
        raise CorpusError(CorpusErrorCode.PARTITION_CHANGED, f"{where}.partition", f"file is in {partition.value}")
    category_text = top["category"]
    category = next((item for item in CaseCategory if item.value == category_text), None)
    if category is None:
        raise CorpusError(invalid, f"{where}.category", "unknown category")
    role = next((item for item in Role if item.value == top["role"]), None)
    if role is None:
        raise CorpusError(invalid, f"{where}.role", "unknown role")
    if type(top["synthetic"]) is not bool or top["synthetic"] is not lock.synthetic:
        raise CorpusError(invalid, f"{where}.synthetic", "must match the lock")
    prompt = _table(top["prompt"], _PROMPT_KEYS, invalid, f"{where}.prompt")
    system, user = prompt["system"], prompt["user"]
    if type(system) is not str or type(user) is not str or not system or not user:
        raise CorpusError(invalid, f"{where}.prompt", "system and user must be non-empty strings")
    if len(system) > MAX_PROMPT_CHARS or len(user) > MAX_PROMPT_CHARS:
        raise CorpusError(invalid, f"{where}.prompt", f"over {MAX_PROMPT_CHARS} characters")
    evidence = top["evidence_ids"]
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_IDS:
        raise CorpusError(invalid, f"{where}.evidence_ids", f"expected a list of at most {MAX_EVIDENCE_IDS}")
    evidence_ids = [_text(item, _EVIDENCE_ID, invalid, f"{where}.evidence_ids") for item in evidence]
    if len(set(evidence_ids)) != len(evidence_ids):
        raise CorpusError(invalid, f"{where}.evidence_ids", "duplicate evidence id")
    gold = _table(top["gold"], _GOLD_KEYS, invalid, f"{where}.gold")
    source = next((item for item in GoldSource if item.value == gold["source"]), None)
    if source is None:
        raise CorpusError(invalid, f"{where}.gold.source", "only deterministic_fixture gold is accepted")
    if gold["human_review"] is not None:
        raise CorpusError(invalid, f"{where}.gold.human_review", "human gold has no format in schema v1")
    abstain = gold["abstain_expected"]
    if type(abstain) is not bool:
        raise CorpusError(invalid, f"{where}.gold.abstain_expected", "expected a boolean")
    if category in ABSTAIN_BY_CATEGORY and ABSTAIN_BY_CATEGORY[category] is not abstain:
        raise CorpusError(invalid, f"{where}.gold.abstain_expected", f"fixed by category {category.value}")
    case_sha = canonical_sha256(document)
    entry = lock.entries[partition][case_id]
    if case_sha != entry.sha256:
        code = CorpusErrorCode.HOLDOUT_EDITED if partition is CorpusPartition.HOLDOUT else CorpusErrorCode.CASE_HASH_MISMATCH
        raise CorpusError(code, where, "content differs from the lock")
    if prompt_fingerprint(system, user, evidence_ids) != entry.prompt_sha256:
        raise CorpusError(CorpusErrorCode.LOCK_INVALID, where, "prompt_sha256 does not match the locked case")
    case = BenchmarkCase(
        case_id=case_id,
        partition=partition,
        category=category,
        role=role,
        system=system,
        user=user,
        evidence_ids=tuple(evidence_ids),
        gold=DeterministicGold(source=source, abstain_expected=abstain),
        case_sha256=case_sha,
    )
    return case, case_sha


def load_partition(
    corpus_root: str | os.PathLike[str], mode: CorpusMode, *, expected_lock_sha256: str
) -> LockedPartition:
    """Verify the locked corpus for ``mode`` and return the partition it may use."""
    if not isinstance(mode, CorpusMode):
        raise CorpusError(CorpusErrorCode.LOCK_INVALID, "mode", "expected a CorpusMode")
    if type(expected_lock_sha256) is not str or _SHA256.fullmatch(expected_lock_sha256) is None:
        raise CorpusError(CorpusErrorCode.LOCK_INVALID, "expected_lock_sha256", "expected 64 lowercase hex characters")
    root = Path(corpus_root)
    lock_path = root / LOCK_FILE_NAME
    lock = _parse_lock(_read_json(lock_path, MAX_LOCK_BYTES, CorpusErrorCode.LOCK_INVALID), os.fspath(lock_path))
    if lock.lock_sha256 != expected_lock_sha256:
        raise CorpusError(CorpusErrorCode.LOCK_CHANGED, os.fspath(lock_path), "does not match the pinned lock sha256")
    scanned = (
        (CorpusPartition.DEVELOPMENT,)
        if mode is CorpusMode.DEVELOPMENT
        else (CorpusPartition.DEVELOPMENT, CorpusPartition.HOLDOUT)
    )
    files = {partition: _scan(root / partition.value, partition) for partition in scanned}
    # Placement first: a case in the wrong partition is reported as such, not as missing.
    for partition, found in files.items():
        for case_id, path in sorted(found.items()):
            other = next(p for p in CorpusPartition if p is not partition)
            if case_id in lock.entries[other]:
                raise CorpusError(CorpusErrorCode.PARTITION_CHANGED, os.fspath(path), f"locked in {other.value}")
            if case_id not in lock.entries[partition]:
                code = (
                    CorpusErrorCode.HOLDOUT_EDITED if partition is CorpusPartition.HOLDOUT else CorpusErrorCode.UNLOCKED_CASE
                )
                raise CorpusError(code, os.fspath(path), "not in the lock")
    for partition, found in files.items():
        missing = sorted(set(lock.entries[partition]) - set(found))
        if missing:
            code = CorpusErrorCode.HOLDOUT_EDITED if partition is CorpusPartition.HOLDOUT else CorpusErrorCode.MISSING_CASE
            raise CorpusError(code, os.fspath(root / partition.value), "missing " + ", ".join(missing))
    loaded: dict[CorpusPartition, tuple[BenchmarkCase, ...]] = {}
    for partition, found in files.items():
        cases: list[BenchmarkCase] = []
        seen: set[str] = set()
        for case_id in sorted(found):
            path = found[case_id]
            case, case_sha = _case(_read_json(path, MAX_CASE_BYTES, CorpusErrorCode.CASE_INVALID), path, partition, lock)
            if case_sha in seen:
                raise CorpusError(CorpusErrorCode.DUPLICATE_CASE, os.fspath(path))
            seen.add(case_sha)
            cases.append(case)
        loaded[partition] = tuple(cases)
    returned = CorpusPartition.DEVELOPMENT if mode is CorpusMode.DEVELOPMENT else CorpusPartition.HOLDOUT
    if returned is CorpusPartition.HOLDOUT:
        present = {case.category for case in loaded[returned]}
        absent = [category.value for category in CaseCategory if category not in present]
        if absent:
            raise CorpusError(CorpusErrorCode.HOLDOUT_CATEGORY_MISSING, os.fspath(root / "holdout"), ", ".join(absent))
    return LockedPartition(
        corpus_id=lock.corpus_id,
        lock_sha256=lock.lock_sha256,
        synthetic=lock.synthetic,
        partition=returned,
        cases=loaded[returned],
    )


def benchmark_profile(profile: ModelProfile) -> BenchmarkProfile:
    """The harness profile for a T050a model profile (disabled profiles are refused)."""
    return BenchmarkProfile(
        inference=profile.inference_profile(),
        min_free_vram_gib=profile.reserve_vram_gib,
        min_free_ram_gib=profile.reserve_ram_gib,
    )


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def write_report(report: BenchmarkReport, output_dir: str | os.PathLike[str]) -> Path:
    """Write the canonical report JSON into ``output_dir`` (required, outside the repository)."""
    where = os.fspath(output_dir)
    try:
        target = Path(output_dir).resolve(strict=True)
    except (OSError, RuntimeError):
        raise CorpusError(CorpusErrorCode.OUTPUT_DIR_REFUSED, where, "must be an existing directory") from None
    if not target.is_dir():
        raise CorpusError(CorpusErrorCode.OUTPUT_DIR_REFUSED, where, "must be an existing directory")
    if _inside(target, REPOSITORY_ROOT):
        raise CorpusError(CorpusErrorCode.OUTPUT_DIR_REFUSED, where, "inside the repository")
    path = target / f"benchmark-{report.partition.value}-{report_sha256(report)[:16]}.json"
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(report_json(report))
    except FileExistsError:
        raise CorpusError(CorpusErrorCode.OUTPUT_EXISTS, os.fspath(path)) from None
    except OSError as error:
        raise CorpusError(CorpusErrorCode.OUTPUT_DIR_REFUSED, where, type(error).__name__) from None
    return path
