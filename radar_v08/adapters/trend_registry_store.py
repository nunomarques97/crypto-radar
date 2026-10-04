"""File I/O of the trend research registry and of the imported historical records.

The rules live in :mod:`radar_v08.domain.trend_registry`; this adapter only reads and appends bytes.

Appending is compare-and-append under an exclusive lock file (``<registry>.lock``, created with
``O_EXCL``): the file is read and verified, the next line is built and checked against the verified
bytes plus that line, then written with one append, flush and fsync. A second writer while the lock
exists is refused as ``BUSY`` and writes nothing; a lock left by a crash must be removed by hand
after checking the registry (no automatic takeover of a research record). The imported historical
registry is read-only: appending to a file whose bytes are that registry is refused.

A holdout is recorded before it computes (:func:`run_holdout`), so a crash during the computation
still consumes it and a second attempt is refused.

The imported records (``docs/audit/2026-10-03-trend-feasibility/``) are byte-identical copies of the
feasibility workspace's registry, paper ledger and registered strategy sources, listed with their
sha256 in ``MANIFEST.json``. :func:`load_imported_records` verifies every listed hash, pins the
registry hash and never writes. Strategy sources are read as bytes for hashing only, never executed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TypeVar

from ..domain.trend_registry import (
    IMPORTED_REGISTRY_SHA256,
    Registry,
    RegistryEvent,
    encode_event,
    holdout_payload,
    parse_registry,
    registration_payload,
    sha256_hex,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AUDIT_DIR = REPOSITORY_ROOT / "docs" / "audit" / "2026-10-03-trend-feasibility"
MANIFEST_NAME = "MANIFEST.json"
REGISTRY_NAME = "registry.jsonl"
PAPER_LEDGER_NAME = "paper_ledger.jsonl"
SOURCES_DIR = "strategy_sources"
SOURCE_SUFFIX = ".py.txt"
MANIFEST_FORMAT_VERSION = 1

T = TypeVar("T")
Clock = Callable[[], datetime]


class RegistryStoreErrorCode(StrEnum):
    UNREADABLE = "UNREADABLE"
    BUSY = "BUSY"
    READ_ONLY_HISTORY = "READ_ONLY_HISTORY"
    CHANGED_DURING_APPEND = "CHANGED_DURING_APPEND"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    IMPORT_CHANGED = "IMPORT_CHANGED"


class RegistryStoreError(Exception):
    def __init__(self, code: RegistryStoreErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value + (f": {detail}" if detail else ""))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as error:
        raise RegistryStoreError(RegistryStoreErrorCode.UNREADABLE, f"{path.name}: {error}") from error


def read_registry(path: Path) -> Registry:
    """Read and verify a registry file; a missing file is an empty registry."""
    return parse_registry(_read_bytes(path))


class _Lock:
    def __init__(self, path: Path) -> None:
        self._path = path.with_name(path.name + ".lock")
        self._fd: int | None = None

    def __enter__(self) -> _Lock:
        try:
            self._fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise RegistryStoreError(RegistryStoreErrorCode.BUSY, f"{self._path.name} exists") from error
        except OSError as error:
            raise RegistryStoreError(RegistryStoreErrorCode.UNREADABLE, f"{self._path.name}: {error}") from error
        return self

    def __exit__(self, *_: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._path.unlink(missing_ok=True)


def _refuse_imported_history(data: bytes) -> None:
    if sha256_hex(data) == IMPORTED_REGISTRY_SHA256:
        raise RegistryStoreError(
            RegistryStoreErrorCode.READ_ONLY_HISTORY, "the imported historical registry is never appended to"
        )


def append_event(path: Path, payload: Mapping[str, object], *, clock: Clock = _utc_now) -> RegistryEvent:
    """Append one verified event and return it as parsed back from the new bytes."""
    _refuse_imported_history(_read_bytes(path))  # before the lock: no lock file beside the history
    with _Lock(path):
        before = _read_bytes(path)
        _refuse_imported_history(before)
        registry = parse_registry(before)
        ts = clock().astimezone(UTC).isoformat(timespec="seconds")
        line = encode_event(registry, ts, payload)
        appended = parse_registry(before + line).events[-1]
        try:
            with path.open("ab") as handle:
                if handle.tell() != len(before):
                    raise RegistryStoreError(RegistryStoreErrorCode.CHANGED_DURING_APPEND, path.name)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise RegistryStoreError(RegistryStoreErrorCode.UNREADABLE, f"{path.name}: {error}") from error
        return appended


def register(
    path: Path,
    *,
    name: str,
    source: bytes,
    file: str,
    rule: str,
    params: Mapping[str, object],
    pass_criteria: list[Mapping[str, object]],
    universe: list[str],
    is_benchmark: bool = False,
    clock: Clock = _utc_now,
) -> RegistryEvent:
    """Register a rule by the sha256 of its exact source bytes (refuses a duplicate name or hash)."""
    payload = registration_payload(
        read_registry(path),
        name=name,
        source_sha256=sha256_hex(source),
        file=file,
        rule=rule,
        params=params,
        pass_criteria=pass_criteria,
        universe=universe,
        is_benchmark=is_benchmark,
    )
    return append_event(path, payload, clock=clock)


def begin_holdout(
    path: Path, *, name: str, source: bytes, slippage_bps: float, clock: Clock = _utc_now
) -> RegistryEvent:
    """Record the holdout of ``name`` before any computation; refuses changed source or a used holdout."""
    payload = holdout_payload(read_registry(path), name, sha256_hex(source), slippage_bps)
    return append_event(path, payload, clock=clock)


def run_holdout(
    path: Path,
    *,
    name: str,
    source: bytes,
    slippage_bps: float,
    compute: Callable[[], T],
    clock: Clock = _utc_now,
) -> T:
    """Record the holdout, then compute. If ``compute`` raises, the holdout stays consumed."""
    begin_holdout(path, name=name, source=source, slippage_bps=slippage_bps, clock=clock)
    return compute()


# ---------------------------------------------------------------------------
# Imported historical records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportedRecords:
    """The verified imported records. ``sources`` maps a registered name to its source bytes."""

    imported_at_utc: str
    file_sha256: Mapping[str, str]
    registry_bytes: bytes
    registry: Registry
    paper_ledger_bytes: bytes
    sources: Mapping[str, bytes]


def _manifest_path(entry: object) -> PurePosixPath:
    if not isinstance(entry, str) or not entry or "\\" in entry or ":" in entry:
        raise RegistryStoreError(RegistryStoreErrorCode.MANIFEST_INVALID, f"path {entry!r}")
    relative = PurePosixPath(entry)
    if relative.is_absolute() or ".." in relative.parts:
        raise RegistryStoreError(RegistryStoreErrorCode.MANIFEST_INVALID, f"path {entry!r} is not relative")
    return relative


def load_imported_records(audit_dir: Path = AUDIT_DIR) -> ImportedRecords:
    """Verify every file listed in the import manifest and return the records; writes nothing."""
    try:
        manifest = json.loads((audit_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RegistryStoreError(RegistryStoreErrorCode.MANIFEST_INVALID, str(error)) from error
    if not isinstance(manifest, dict) or manifest.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise RegistryStoreError(RegistryStoreErrorCode.MANIFEST_INVALID, "unsupported manifest")
    imported_at = manifest.get("imported_at_utc")
    entries = manifest.get("files")
    if not isinstance(imported_at, str) or not isinstance(entries, list):
        raise RegistryStoreError(RegistryStoreErrorCode.MANIFEST_INVALID, "missing import time or file list")
    contents: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RegistryStoreError(RegistryStoreErrorCode.MANIFEST_INVALID, f"entry {entry!r}")
        relative = _manifest_path(entry.get("path"))
        original = entry.get("original")
        if original is not None:
            _manifest_path(original)
        expected = entry.get("sha256")
        try:
            data = (audit_dir / Path(*relative.parts)).read_bytes()
        except OSError as error:
            raise RegistryStoreError(RegistryStoreErrorCode.IMPORT_CHANGED, f"{relative}: {error}") from error
        if sha256_hex(data) != expected or entry.get("bytes") != len(data):
            raise RegistryStoreError(RegistryStoreErrorCode.IMPORT_CHANGED, f"{relative} differs from the manifest")
        contents[relative.as_posix()] = data
        hashes[relative.as_posix()] = sha256_hex(data)
    if REGISTRY_NAME not in contents or PAPER_LEDGER_NAME not in contents:
        raise RegistryStoreError(RegistryStoreErrorCode.MANIFEST_INVALID, "registry or paper ledger not listed")
    registry_bytes = contents[REGISTRY_NAME]
    if sha256_hex(registry_bytes) != IMPORTED_REGISTRY_SHA256:
        raise RegistryStoreError(RegistryStoreErrorCode.IMPORT_CHANGED, "the imported registry hash is not the pinned one")
    sources = {
        PurePosixPath(key).name.removesuffix(SOURCE_SUFFIX): data
        for key, data in contents.items()
        if PurePosixPath(key).parent.as_posix() == SOURCES_DIR and key.endswith(SOURCE_SUFFIX)
    }
    return ImportedRecords(
        imported_at_utc=imported_at,
        file_sha256=hashes,
        registry_bytes=registry_bytes,
        registry=parse_registry(registry_bytes),
        paper_ledger_bytes=contents[PAPER_LEDGER_NAME],
        sources=sources,
    )
