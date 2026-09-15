"""Resolves the crypto-radar repo root, whether running from source
(`python -m ui`) or from a frozen PyInstaller build distributed inside the
project directory (per the task's packaging requirement: the .exe ships
inside the project, never standalone).

Kept deliberately tiny and side-effect-free so it is trivial to unit test
against monkeypatched `sys.frozen`/`sys.executable`.
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass

_MARKER_FILE = "radar.py"
_MAX_WALK_UP = 4


@dataclass(frozen=True)
class ProcessIdentity:
    """Stable Windows process facts used to reject PID reuse."""

    pid: int
    executable: str
    start_identity: int


def normalized_executable_identity(path: str) -> str:
    """Return the comparison form used for executable ownership checks."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _default_windll():
    return ctypes.windll


def current_process_identity(pid: int, windll_factory=_default_windll) -> ProcessIdentity | None:
    """Read executable and creation-time facts for a live Windows process.

    ``None`` is unknown, never evidence that a PID is owned.
    """
    if os.name != "nt":
        return None
    try:
        kernel32 = windll_factory().kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.c_ulong(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return None

            class _FileTime(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

            created = _FileTime()
            exited = _FileTime()
            kernel_time = _FileTime()
            user_time = _FileTime()
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel_time), ctypes.byref(user_time),
            ):
                return None
            start_identity = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return ProcessIdentity(pid, normalized_executable_identity(buffer.value), start_identity)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - an unreadable PID is unknown, never owned
        return None


def _looks_like_repo_root(candidate: str) -> bool:
    return os.path.isfile(os.path.join(candidate, _MARKER_FILE))


def repo_root() -> str:
    """Directory containing `radar.py` / `radar_v08/` / `radar_state.sqlite`.

    Frozen: walk up from the executable's directory (the exe is distributed
    inside the project, e.g. `dist/CryptoRadarControlRoom/CryptoRadar.exe`).
    Unfrozen: the parent of this `ui/` package (the repo root when running
    `python -m ui` from source).
    """
    if getattr(sys, "frozen", False):
        start = os.path.dirname(os.path.abspath(sys.executable))
    else:
        start = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    candidate = start
    for _ in range(_MAX_WALK_UP + 1):
        if _looks_like_repo_root(candidate):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent

    # Nothing found within the walk-up budget - fall back to the initial
    # guess rather than raising, so callers get a clear "file not found"
    # error at the actual point of use instead of an opaque path failure here.
    return start


def radar_py_path() -> str:
    return os.path.join(repo_root(), "radar.py")


def ensure_importable() -> None:
    """Puts the repo root on sys.path so `import radar_v08` works regardless
    of whether the UI is run from source or frozen. Callers that need
    `radar_v08.config`'s own path constants (SQLITE_PATH, TEXT_LOG_PATH,
    OUTPUT_V08_PATH) should import config directly after calling this -
    those constants are the single source of truth, never re-derived here.
    """
    root = repo_root()
    if root not in sys.path:
        sys.path.insert(0, root)


def ui_state_dir() -> str:
    """UI-only local state (window position, last tab, PID lock) - never
    radar domain state, never inside radar_state.sqlite.
    """
    path = os.path.join(repo_root(), "ui", ".state")
    os.makedirs(path, exist_ok=True)
    return path
