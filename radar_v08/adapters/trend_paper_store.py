"""File I/O of the trend paper ledger.

The ledger rules (records, hash chain, verification) live in :mod:`radar_v08.domain.trend_paper`;
this adapter only reads, locks and appends bytes.

Location: ``<radar state dir>/trend_paper/ledger.jsonl`` (gitignored runtime state). It never
touches ``radar_state.sqlite`` or any other radar file. The Kraken EUR books
keep their own ledger, ``kraken_ledger.jsonl`` next to it, with its own
lock, through the same functions and their ``extend`` parameter (the default is the 24 books).

Writing happens only under an exclusive, non-blocking operating-system lock on
``ledger.jsonl.lock`` (``msvcrt.locking`` on Windows, ``flock`` elsewhere). A second writer, in
this or another process, gets ``BUSY`` and writes nothing. Stale-lock policy: the lock is held by
the open file handle, so the operating system releases it when the holder exits or crashes; a
leftover lock *file* is not a lock and never blocks a later run, and nothing ever deletes it. A
hung holder that is still alive keeps the lock until its process ends.

Each day is one append: the new lines are verified as a continuation of the ledger read and
verified under the lock, the file size is checked against it, the lines are written with
``O_APPEND`` and fsynced, and the appended bytes are read back and compared. If the write itself fails, the bytes of that failed append (and only
those) are truncated away under the same lock; a torn or edited ledger found on read is refused and
never rewritten.

Readers without the lock (the report and the UI) use :func:`read_ledger_settled`: a refusal can be
an append caught half written, so the file is re-read a bounded number of times, and if it is still
refused while a writer holds the lock (:func:`writer_active`, which never creates the lock file)
the read raises ``BEING_WRITTEN`` instead (transient: nothing is wrong yet). With no writer, the
refusal stands.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import TracebackType

from ..domain.trend_paper import (
    EMPTY_LEDGER,
    PAPER_START,
    Ledger,
    PaperError,
    PaperErrorCode,
    extend_ledger,
)

LEDGER_DIR_NAME = "trend_paper"
LEDGER_NAME = "ledger.jsonl"
LOCK_SUFFIX = ".lock"
SETTLE_ATTEMPTS = 3
SETTLE_PAUSE_SECONDS = 0.15
_REFUSED_CODES = frozenset({PaperErrorCode.LEDGER_TORN, PaperErrorCode.LEDGER_EDITED, PaperErrorCode.LEDGER_INVALID})
#: Verifies new lines as a continuation of a ledger: :func:`extend_ledger` (the 24 books) by default,
#: or another book set's own parser (``radar_v08.domain.trend_paper_kraken.extend_kraken_ledger``).
Extend = Callable[[Ledger, bytes, date], Ledger]


class TrendPaperStoreErrorCode(StrEnum):
    BUSY = "BUSY"
    UNREADABLE = "UNREADABLE"
    CHANGED_DURING_APPEND = "CHANGED_DURING_APPEND"
    BEING_WRITTEN = "BEING_WRITTEN"
    WRITE_FAILED = "WRITE_FAILED"


class TrendPaperStoreError(Exception):
    def __init__(self, code: TrendPaperStoreErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value + (f": {detail}" if detail else ""))


def ledger_path(state_dir: str | os.PathLike[str]) -> Path:
    return Path(state_dir) / LEDGER_DIR_NAME / LEDGER_NAME


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as error:
        raise TrendPaperStoreError(TrendPaperStoreErrorCode.UNREADABLE, f"{path.name}: {error}") from error


def read_ledger(path: Path, start: date = PAPER_START, extend: Extend = extend_ledger) -> Ledger:
    """Read and verify the ledger without locking or creating anything (a missing file is empty)."""
    return extend(EMPTY_LEDGER, _read_bytes(path), start)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


_BINARY = getattr(os, "O_BINARY", 0)


def lock_path(path: Path) -> Path:
    return path.with_name(path.name + LOCK_SUFFIX)


def writer_active(path: Path) -> bool:
    """Whether a writer holds the lock of the ledger at ``path`` right now. Opens the lock file
    only if it exists (it never creates it, its directory or the ledger) and releases its own
    probe at once. A missing or unopenable lock file means no writer."""
    try:
        fd = os.open(lock_path(path), os.O_RDWR | _BINARY)
    except OSError:
        return False
    try:
        try:
            _try_lock(fd)
        except OSError:
            return True
        try:
            _unlock(fd)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


def read_ledger_settled(
    path: Path,
    start: date = PAPER_START,
    *,
    attempts: int = SETTLE_ATTEMPTS,
    pause: float = SETTLE_PAUSE_SECONDS,
    sleep: Callable[[float], object] | None = None,
    extend: Extend = extend_ledger,
) -> Ledger:
    """:func:`read_ledger` for a reader without the lock. A refused read is retried up to
    ``attempts`` times ``pause`` seconds apart; if it is still refused while a writer holds the
    lock, raises ``BEING_WRITTEN`` (an append may be in progress) instead of the refusal."""
    for attempt in range(max(1, attempts)):
        try:
            return read_ledger(path, start, extend)
        except PaperError as error:
            if error.code not in _REFUSED_CODES:
                raise
            if attempt + 1 < attempts:
                (sleep or time.sleep)(pause)
                continue
            if writer_active(path):
                raise TrendPaperStoreError(
                    TrendPaperStoreErrorCode.BEING_WRITTEN, "a catch-up is writing the ledger"
                ) from error
            raise
    raise AssertionError("unreachable")


class LedgerWriter:
    """The exclusive writer of one ledger file; use as a context manager."""

    def __init__(self, path: Path, start: date = PAPER_START, extend: Extend = extend_ledger) -> None:
        self.path = path
        self.start = start
        self.extend = extend
        self._lock_path = lock_path(path)
        self._fd: int | None = None

    def __enter__(self) -> LedgerWriter:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | _BINARY, 0o600)
        except OSError as error:
            raise TrendPaperStoreError(TrendPaperStoreErrorCode.UNREADABLE, f"{self._lock_path.name}: {error}") from error
        try:
            _try_lock(fd)
        except OSError as error:
            os.close(fd)
            raise TrendPaperStoreError(TrendPaperStoreErrorCode.BUSY, "another trend paper run holds the lock") from error
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

    def _require_lock(self) -> None:
        if self._fd is None:
            raise TrendPaperStoreError(TrendPaperStoreErrorCode.WRITE_FAILED, "the ledger lock is not held")

    def read(self) -> Ledger:
        self._require_lock()
        return read_ledger(self.path, self.start, self.extend)

    def append(self, data: bytes, expected: Ledger) -> Ledger:
        """Append one day's encoded lines to ``expected`` (the ledger this writer last verified)
        and return the extended ledger once the written bytes are read back unchanged."""
        self._require_lock()
        extended = self.extend(expected, data, self.start)  # verified before a byte is written
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | _BINARY, 0o600)
        except OSError as error:
            raise TrendPaperStoreError(TrendPaperStoreErrorCode.WRITE_FAILED, f"{self.path.name}: {error}") from error
        try:
            if os.fstat(fd).st_size != expected.size:
                raise TrendPaperStoreError(TrendPaperStoreErrorCode.CHANGED_DURING_APPEND, self.path.name)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            except OSError as error:
                try:
                    os.ftruncate(fd, expected.size)  # undo only this failed append
                    os.fsync(fd)
                except OSError:
                    pass
                raise TrendPaperStoreError(TrendPaperStoreErrorCode.WRITE_FAILED, f"{self.path.name}: {error}") from error
            os.lseek(fd, expected.size, os.SEEK_SET)
            back = bytearray()
            while chunk := os.read(fd, 1 << 20):
                back += chunk
        finally:
            os.close(fd)
        if bytes(back) != data:
            raise TrendPaperStoreError(TrendPaperStoreErrorCode.CHANGED_DURING_APPEND, f"{self.path.name}: read-back differs")
        return extended

    def appender(self, expected: Ledger) -> Callable[[bytes], Ledger]:
        """An ``append`` callback for :func:`radar_v08.domain.trend_paper.catch_up_days` that tracks the
        ledger it last verified."""
        state = {"ledger": expected}

        def append(data: bytes) -> Ledger:
            state["ledger"] = self.append(data, state["ledger"])
            return state["ledger"]

        return append
