"""Dedupe file of the trend paper exposure-change alerts.

Location: ``<radar state dir>/trend_paper/alerts.jsonl`` (gitignored runtime state, next to the
ledger). It never touches the ledger, ``radar_state.sqlite`` or any other radar file.

One canonical JSON line per handled key ``(rule, day)``: ``{"day", "rule", "status", "ts"}`` with
status ``claimed`` (a toast was attempted after this line was written) or ``superseded`` (a later
change of the same rule in the same catch-up was shown instead). The file is append-only and never
rewritten. A key is claimed before its toast is sent, so a rerun, a restart or a failed toast never
shows a second toast for it (at most once: a lost toast is preferred to a duplicate).

Claims happen under an exclusive, non-blocking lock on ``alerts.jsonl.lock`` (the ledger's lock
mechanism); a second claimer gets ``BUSY`` and claims nothing. An unreadable, torn or edited file
is refused (``UNREADABLE`` or ``CORRUPT``) and nothing is claimed, so nothing is sent.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import TracebackType

from ..domain.trend_paper import Rule
from .trend_paper_store import _BINARY, LEDGER_DIR_NAME, LOCK_SUFFIX, _try_lock, _unlock

ALERTS_NAME = "alerts.jsonl"
CLAIMED = "claimed"
SUPERSEDED = "superseded"
STATUSES = (CLAIMED, SUPERSEDED)

Key = tuple[str, str]  # (rule, ISO day)


class TrendAlertStoreErrorCode(StrEnum):
    BUSY = "BUSY"
    UNREADABLE = "UNREADABLE"
    CORRUPT = "CORRUPT"
    WRITE_FAILED = "WRITE_FAILED"


class TrendAlertStoreError(Exception):
    def __init__(self, code: TrendAlertStoreErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value + (f": {detail}" if detail else ""))


def alerts_path(state_dir: str | os.PathLike[str]) -> Path:
    return Path(state_dir) / LEDGER_DIR_NAME / ALERTS_NAME


def _line(rule: str, day: str, status: str, ts: str) -> bytes:
    return json.dumps(
        {"day": day, "rule": rule, "status": status, "ts": ts}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii") + b"\n"


def _corrupt(n: int, why: str) -> TrendAlertStoreError:
    return TrendAlertStoreError(TrendAlertStoreErrorCode.CORRUPT, f"{ALERTS_NAME} line {n}: {why}")


def parse_handled(data: bytes) -> frozenset[Key]:
    """The keys recorded in ``data``; raises ``CORRUPT`` on a torn, edited or unknown line."""
    if not data:
        return frozenset()
    if not data.endswith(b"\n"):
        raise _corrupt(data.count(b"\n"), "the last line is not terminated")
    keys: set[Key] = set()
    rules = {rule.value for rule in Rule}
    for n, raw in enumerate(data[:-1].split(b"\n")):
        try:
            obj = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise _corrupt(n, "not a JSON record") from None
        if not isinstance(obj, dict) or set(obj) != {"day", "rule", "status", "ts"}:
            raise _corrupt(n, "unexpected fields")
        rule, day, status, ts = obj["rule"], obj["day"], obj["status"], obj["ts"]
        if rule not in rules or status not in STATUSES or not isinstance(ts, str) or not isinstance(day, str):
            raise _corrupt(n, "unexpected values")
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError(day)
        except ValueError:
            raise _corrupt(n, "bad day") from None
        if _line(rule, day, status, ts) != raw + b"\n":
            raise _corrupt(n, "not canonical")
        keys.add((rule, day))
    return frozenset(keys)


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as error:
        raise TrendAlertStoreError(TrendAlertStoreErrorCode.UNREADABLE, f"{path.name}: {error}") from error


def read_handled(path: Path) -> frozenset[Key]:
    """Read the handled keys without locking or creating anything (a missing file is empty)."""
    return parse_handled(_read_bytes(path))


class _Lock:
    def __init__(self, path: Path) -> None:
        self._path = path.with_name(path.name + LOCK_SUFFIX)
        self._fd: int | None = None

    def __enter__(self) -> _Lock:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT | _BINARY, 0o600)
        except OSError as error:
            raise TrendAlertStoreError(TrendAlertStoreErrorCode.UNREADABLE, f"{self._path.name}: {error}") from error
        try:
            _try_lock(fd)
        except OSError as error:
            os.close(fd)
            raise TrendAlertStoreError(TrendAlertStoreErrorCode.BUSY, "another alert step holds the lock") from error
        self._fd = fd
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass  # closing the handle releases it as well
        finally:
            os.close(fd)


def claim(path: Path, entries: Sequence[tuple[Key, str]], ts: str) -> list[Key]:
    """Record every ``(key, status)`` not handled yet and return those keys, in order. Nothing is
    returned (so nothing may be sent) unless their lines are written and fsynced."""
    for _, status in entries:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
    with _Lock(path):
        handled = set(read_handled(path))
        new: list[Key] = []
        data = bytearray()
        for key, status in entries:
            if key in handled:
                continue
            handled.add(key)
            new.append(key)
            data += _line(key[0], key[1], status, ts)
        if not data:
            return []
        parse_handled(bytes(data))  # what is written must read back as valid
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _BINARY, 0o600)
        except OSError as error:
            raise TrendAlertStoreError(TrendAlertStoreErrorCode.WRITE_FAILED, f"{path.name}: {error}") from error
        try:
            view = memoryview(bytes(data))
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except OSError as error:
            raise TrendAlertStoreError(TrendAlertStoreErrorCode.WRITE_FAILED, f"{path.name}: {error}") from error
        finally:
            os.close(fd)
    return new
