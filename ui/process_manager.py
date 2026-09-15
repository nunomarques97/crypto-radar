"""Starts/stops/restarts `python radar.py --mode loop` as a controlled
subprocess. The UI owns its own PID lock file (radar_v08 has no PID-file
mechanism of its own) - the lock never lives inside radar_state.sqlite, only
under `ui/.state/` (see paths.ui_state_dir()).

Windows-specific stop sequence: `_run_loop()` in radar_v08/cli.py only
handles a real `KeyboardInterrupt`, and a plain `terminate()` maps to
`TerminateProcess` (not graceful) on Windows. The documented-safe path is to
spawn with `CREATE_NEW_PROCESS_GROUP` and send `CTRL_BREAK_EVENT`, which
Python's signal handling raises as `KeyboardInterrupt` in the child - then
escalate to a hard kill only if it doesn't exit within the timeout.

No blocking I/O happens on the caller's thread: stdout/stderr are drained by
daemon threads into bounded queues, and stop() runs its wait/kill sequence on
a background thread, updating `self.state` as it goes.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from ui import paths

STOPPED = "STOPPED"
STARTING = "STARTING"
RUNNING = "RUNNING"
STOPPING = "STOPPING"
ERROR = "ERROR"

_LOCK_FILENAME = "radar_ui_lock.json"
_MAX_LOG_LINES = 500
_STILL_ACTIVE = 259


def _default_windll():
    return ctypes.windll  # AttributeError off-Windows - caller decides what to do with it


def is_pid_alive(pid: int, windll_factory=_default_windll) -> bool:
    """True if `pid` refers to a live, still-running process. Uses the raw
    Win32 API (no new dependency) rather than `os.kill(pid, 0)`, which does
    not work for liveness checks on Windows.
    """
    try:
        windll = windll_factory()
        kernel32 = windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - liveness check is best-effort, never fatal
        return False


def _find_python_exe() -> str | None:
    """Resolves a real Python interpreter to run radar.py - the frozen UI
    exe's own sys.executable is NOT a Python interpreter, so this must never
    assume `sys.executable` works. Order: explicit override env var, the
    venv/interpreter currently running this code (only valid when unfrozen),
    then PATH lookup.
    """
    override = os.environ.get("RADAR_PYTHON_EXE")
    if override and os.path.isfile(override):
        return override
    if not getattr(sys, "frozen", False):
        return sys.executable
    import shutil
    return shutil.which("python") or shutil.which("python3")


@dataclass
class ProcessState:
    state: str = STOPPED
    pid: int | None = None
    started_at: float | None = None
    last_exit_code: int | None = None
    last_error: str | None = None


class ProcessManager:
    def __init__(
        self,
        radar_py_path: str | None = None,
        python_exe: str | None = None,
        lock_path: str | None = None,
        popen_fn=subprocess.Popen,
        pid_alive_fn=is_pid_alive,
        stop_timeout_seconds: float = 10.0,
    ):
        self.radar_py_path = radar_py_path or paths.radar_py_path()
        self.python_exe = python_exe
        self.lock_path = lock_path or os.path.join(paths.ui_state_dir(), _LOCK_FILENAME)
        self._popen_fn = popen_fn
        self._pid_alive_fn = pid_alive_fn
        self._stop_timeout_seconds = stop_timeout_seconds

        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._state = ProcessState()
        self._stdout_q: queue.Queue[str] = queue.Queue()
        self._stderr_q: queue.Queue[str] = queue.Queue()
        self._log_lines: list[str] = []
        self._worker: threading.Thread | None = None

        self._adopt_existing_lock()

    # -- state read ----------------------------------------------------------

    def snapshot(self) -> ProcessState:
        with self._lock:
            return ProcessState(**self._state.__dict__)

    def uptime_seconds(self) -> float | None:
        with self._lock:
            if self._state.state != RUNNING or self._state.started_at is None:
                return None
            return time.time() - self._state.started_at

    def recent_log_lines(self, max_lines: int = 100) -> list[str]:
        self._drain_queues()
        with self._lock:
            return self._log_lines[-max_lines:]

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> ProcessState:
        with self._lock:
            if self._state.state in (RUNNING, STARTING):
                return ProcessState(**self._state.__dict__)

            if self._state.pid and self._pid_alive_fn(self._state.pid):
                # A live process already exists (e.g. from a previous UI
                # session) - adopt it rather than spawning a second one.
                self._state.state = RUNNING
                return ProcessState(**self._state.__dict__)

            python_exe = self.python_exe or _find_python_exe()
            if not python_exe:
                self._state = ProcessState(
                    state=ERROR, last_error="No Python interpreter found to run radar.py "
                    "(set RADAR_PYTHON_EXE or ensure 'python' is on PATH)",
                )
                return ProcessState(**self._state.__dict__)

            self._state.state = STARTING
            self._state.last_error = None
            cmd = [python_exe, self.radar_py_path, "--mode", "loop"]

            popen_kwargs = dict(
                cwd=os.path.dirname(self.radar_py_path),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            try:
                proc = self._popen_fn(cmd, **popen_kwargs)
            except OSError as exc:
                self._state = ProcessState(state=ERROR, last_error=str(exc))
                return ProcessState(**self._state.__dict__)

            self._proc = proc
            self._state = ProcessState(state=RUNNING, pid=proc.pid, started_at=time.time())
            self._write_lock()
            self._start_pumps(proc)
            return ProcessState(**self._state.__dict__)

    def stop(self) -> None:
        with self._lock:
            if self._state.state not in (RUNNING, STARTING) or self._proc is None:
                if self._state.state != STOPPED:
                    self._state = ProcessState()
                    self._clear_lock()
                return
            self._state.state = STOPPING
            proc = self._proc

        self._worker = threading.Thread(target=self._stop_worker, args=(proc,), daemon=True)
        self._worker.start()

    def restart(self) -> None:
        def _sequence():
            self.stop()
            if self._worker is not None:
                self._worker.join(timeout=self._stop_timeout_seconds + 5)
            self.start()

        threading.Thread(target=_sequence, daemon=True).start()

    def _stop_worker(self, proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32" and hasattr(__import__("signal"), "CTRL_BREAK_EVENT"):
                import signal
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            exit_code = proc.wait(timeout=self._stop_timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            exit_code = proc.wait(timeout=5)
        except Exception as exc:  # noqa: BLE001 - stop must never leave state stuck
            with self._lock:
                self._state = ProcessState(state=ERROR, last_error=str(exc))
                self._clear_lock()
            return

        with self._lock:
            self._state = ProcessState(state=STOPPED, last_exit_code=exit_code)
            self._proc = None
            self._clear_lock()

    # -- log pumps -------------------------------------------------------------

    def _start_pumps(self, proc: subprocess.Popen) -> None:
        def pump(stream, sink: queue.Queue):
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    sink.put(line.rstrip("\n"))
            except (ValueError, OSError):
                pass  # stream closed under us during shutdown - not an error

        threading.Thread(target=pump, args=(proc.stdout, self._stdout_q), daemon=True).start()
        threading.Thread(target=pump, args=(proc.stderr, self._stderr_q), daemon=True).start()

    def _drain_queues(self) -> None:
        with self._lock:
            for q in (self._stdout_q, self._stderr_q):
                while True:
                    try:
                        self._log_lines.append(q.get_nowait())
                    except queue.Empty:
                        break
            if len(self._log_lines) > _MAX_LOG_LINES:
                self._log_lines = self._log_lines[-_MAX_LOG_LINES:]

    # -- lock file (UI-owned, never radar_state.sqlite) -------------------------

    def _write_lock(self) -> None:
        try:
            with open(self.lock_path, "w", encoding="utf-8") as fh:
                json.dump({"pid": self._state.pid, "started_at": self._state.started_at}, fh)
        except OSError:
            pass  # best-effort - a missing lock file just disables double-start detection

    def _clear_lock(self) -> None:
        try:
            if os.path.exists(self.lock_path):
                os.remove(self.lock_path)
        except OSError:
            pass

    def _adopt_existing_lock(self) -> None:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        pid = data.get("pid")
        if pid and self._pid_alive_fn(pid):
            self._state = ProcessState(state=RUNNING, pid=pid, started_at=data.get("started_at"))
        else:
            self._clear_lock()
