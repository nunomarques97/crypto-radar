"""Truthful, bounded ownership of ``python radar.py --mode loop``.

Only a child launched by this ProcessManager is controllable. A persisted
PID from an earlier UI session is observable as ADOPTED but never treated as
our process: a PID alone cannot survive reuse and has no Popen handle.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from ui import paths

STOPPED = "STOPPED"
FORCED_STOPPED = "FORCED_STOPPED"
STARTING = "STARTING"
RUNNING = "RUNNING"
STOPPING = "STOPPING"
ADOPTED = "ADOPTED"
UNKNOWN = "UNKNOWN"
ERROR = "ERROR"

_LOCK_FILENAME = "radar_ui_lock.json"
_MAX_LOG_LINES = 500


def is_pid_alive(pid: int) -> bool:
    """Best-effort liveness for stale lock cleanup; not ownership proof."""
    return paths.current_process_identity(pid) is not None


def _find_python_exe() -> str | None:
    """Resolve a real Python interpreter, never a frozen UI executable."""
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
    ownership: str = "NONE"
    dropped_log_lines: int = 0


@dataclass(frozen=True)
class _LaunchRecord:
    proc: subprocess.Popen
    identity: paths.ProcessIdentity


class ProcessManager:
    def __init__(
        self,
        radar_py_path: str | None = None,
        python_exe: str | None = None,
        lock_path: str | None = None,
        popen_fn=subprocess.Popen,
        pid_alive_fn=is_pid_alive,
        process_identity_fn=paths.current_process_identity,
        stop_timeout_seconds: float = 10.0,
        clock_fn=time.time,
    ):
        self.radar_py_path = radar_py_path or paths.radar_py_path()
        self.python_exe = python_exe
        self.lock_path = lock_path or os.path.join(paths.ui_state_dir(), _LOCK_FILENAME)
        self._popen_fn = popen_fn
        self._pid_alive_fn = pid_alive_fn
        self._process_identity_fn = process_identity_fn
        self._stop_timeout_seconds = stop_timeout_seconds
        self._clock_fn = clock_fn

        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._launch_record: _LaunchRecord | None = None
        self._state = ProcessState()
        self._stdout_q: queue.Queue[str] = queue.Queue(maxsize=_MAX_LOG_LINES)
        self._stderr_q: queue.Queue[str] = queue.Queue(maxsize=_MAX_LOG_LINES)
        self._log_lines: list[str] = []
        self._worker: threading.Thread | None = None
        self._adopt_existing_lock()

    def snapshot(self) -> ProcessState:
        with self._lock:
            return ProcessState(**self._state.__dict__)

    def uptime_seconds(self) -> float | None:
        with self._lock:
            if self._state.state != RUNNING or self._state.started_at is None:
                return None
            return self._clock_fn() - self._state.started_at

    def recent_log_lines(self, max_lines: int = 100) -> list[str]:
        self._drain_queues()
        with self._lock:
            if max_lines <= 0:
                return []
            lines = self._log_lines[-max_lines:]
            if not self._state.dropped_log_lines:
                return lines
            notice = f"[log truncated: {self._state.dropped_log_lines} oldest line(s) discarded]"
            return [notice, *lines[-(max_lines - 1):]]

    def start(self) -> ProcessState:
        with self._lock:
            # Do not replace an adopted process or an unresolved launch/stop.
            if self._proc is not None or self._state.state in (STARTING, RUNNING, STOPPING, ADOPTED, UNKNOWN):
                return ProcessState(**self._state.__dict__)

            python_exe = self.python_exe or _find_python_exe()
            if not python_exe:
                self._state = ProcessState(
                    state=ERROR,
                    last_error="No Python interpreter found to run radar.py (set RADAR_PYTHON_EXE or ensure python is on PATH)",
                )
                return ProcessState(**self._state.__dict__)

            self._state = ProcessState(state=STARTING)
            command = [python_exe, self.radar_py_path, "--mode", "loop"]
            popen_kwargs = {
                "cwd": os.path.dirname(self.radar_py_path),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "bufsize": 1,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                proc = self._popen_fn(command, **popen_kwargs)
            except OSError as exc:
                self._state = ProcessState(state=ERROR, last_error=str(exc))
                return ProcessState(**self._state.__dict__)

            self._proc = proc
            self._start_pumps(proc)
            observed = self._process_identity_fn(proc.pid)
            expected_executable = paths.normalized_executable_identity(python_exe)
            if observed is None:
                self._state = ProcessState(
                    state=UNKNOWN,
                    pid=proc.pid,
                    ownership="UNKNOWN",
                    last_error="Launched child identity was unavailable; ownership is unknown",
                )
                return ProcessState(**self._state.__dict__)
            if observed.executable != expected_executable:
                self._state = ProcessState(
                    state=UNKNOWN,
                    pid=proc.pid,
                    ownership="UNKNOWN",
                    last_error="Launched child executable identity did not match the requested interpreter",
                )
                return ProcessState(**self._state.__dict__)

            self._launch_record = _LaunchRecord(proc=proc, identity=observed)
            self._state = ProcessState(
                state=RUNNING,
                pid=proc.pid,
                started_at=self._clock_fn(),
                ownership="OWNED",
            )
            self._write_lock()
            return ProcessState(**self._state.__dict__)

    def stop(self) -> None:
        with self._lock:
            if self._state.state == ADOPTED:
                self._state.last_error = "Adopted external process cannot be stopped by this Control Room"
                return
            if self._proc is None:
                return
            if not self._is_owned(self._proc):
                self._mark_unknown_locked("Ownership changed before stop; no signal was sent")
                return
            if self._state.state == STOPPING:
                return
            self._state.state = STOPPING
            proc = self._proc
        self._worker = threading.Thread(target=self._stop_worker, args=(proc,), daemon=True)
        self._worker.start()

    def restart(self) -> None:
        def _sequence() -> None:
            self.stop()
            if self._worker is not None:
                self._worker.join(timeout=self._stop_timeout_seconds + 5)
            self.start()

        threading.Thread(target=_sequence, daemon=True).start()

    def _stop_worker(self, proc: subprocess.Popen) -> None:
        forced = False
        try:
            with self._lock:
                if not self._is_owned(proc):
                    self._mark_unknown_locked("Ownership changed before graceful stop; no signal was sent")
                    return
            if sys.platform == "win32":
                # start() created a separate process group, so SIGBREAK is
                # confined to this verified owned child group.
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            try:
                exit_code = proc.wait(timeout=self._stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                with self._lock:
                    if not self._is_owned(proc):
                        self._mark_unknown_locked("Ownership changed before forced stop; no kill was sent")
                        return
                proc.kill()
                forced = True
                exit_code = proc.wait(timeout=self._stop_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - preserve unresolved ownership
            with self._lock:
                self._state.state = ERROR
                self._state.last_error = f"Stop failed; ownership retained: {exc}"
            return

        with self._lock:
            record = self._launch_record
            if self._proc is not proc or record is None or record.proc is not proc:
                self._mark_unknown_locked("Stop completed without a matching launch record")
                return
            dropped = self._state.dropped_log_lines
            self._state = ProcessState(
                state=FORCED_STOPPED if forced else STOPPED,
                last_exit_code=exit_code,
                dropped_log_lines=dropped,
            )
            self._proc = None
            self._launch_record = None
            self._clear_lock()

    def _is_owned(self, proc: subprocess.Popen) -> bool:
        record = self._launch_record
        if record is None or self._proc is not proc or record.proc is not proc:
            return False
        return self._process_identity_fn(record.identity.pid) == record.identity

    def _mark_unknown_locked(self, message: str) -> None:
        self._state.state = UNKNOWN
        self._state.ownership = "UNKNOWN"
        self._state.last_error = message

    def _start_pumps(self, proc: subprocess.Popen) -> None:
        def pump(stream, sink: queue.Queue[str]) -> None:
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    self._put_bounded(sink, line.rstrip("\n"))
            except (ValueError, OSError):
                pass

        threading.Thread(target=pump, args=(proc.stdout, self._stdout_q), daemon=True).start()
        threading.Thread(target=pump, args=(proc.stderr, self._stderr_q), daemon=True).start()

    def _put_bounded(self, sink: queue.Queue[str], line: str) -> None:
        try:
            sink.put_nowait(line)
            return
        except queue.Full:
            pass
        try:
            sink.get_nowait()  # deterministic oldest-line discard
        except queue.Empty:
            pass
        with self._lock:
            self._state.dropped_log_lines += 1
        try:
            sink.put_nowait(line)
        except queue.Full:
            with self._lock:
                self._state.dropped_log_lines += 1

    def _drain_queues(self) -> None:
        with self._lock:
            for sink in (self._stdout_q, self._stderr_q):
                while True:
                    try:
                        self._log_lines.append(sink.get_nowait())
                    except queue.Empty:
                        break
            if len(self._log_lines) > _MAX_LOG_LINES:
                discarded = len(self._log_lines) - _MAX_LOG_LINES
                del self._log_lines[:discarded]
                self._state.dropped_log_lines += discarded

    def _write_lock(self) -> None:
        record = self._launch_record
        if record is None:
            return
        try:
            with open(self.lock_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "pid": record.identity.pid,
                        "started_at": self._state.started_at,
                        "executable": record.identity.executable,
                        "start_identity": record.identity.start_identity,
                    },
                    fh,
                )
        except OSError:
            pass

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
        if isinstance(pid, int) and self._pid_alive_fn(pid):
            self._state = ProcessState(
                state=ADOPTED,
                pid=pid,
                started_at=data.get("started_at"),
                ownership="ADOPTED",
                last_error="External process adopted from a prior lock; it cannot be controlled by this session",
            )
        else:
            self._clear_lock()
