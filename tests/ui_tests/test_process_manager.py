import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ui import paths
from ui.process_manager import (
    ADOPTED,
    ERROR,
    FORCED_STOPPED,
    RUNNING,
    STOPPED,
    UNKNOWN,
    ProcessManager,
)


class FakeProc:
    _next_pid = 9000

    def __init__(self, *, wait_hangs=False, send_error=None, stdout="", stderr=""):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.wait_hangs = wait_hangs
        self.send_error = send_error
        self.signals = []
        self.killed = False
        self.exit_code = 0

    def send_signal(self, sig):
        if self.send_error:
            raise self.send_error
        self.signals.append(sig)

    def terminate(self):
        self.signals.append("TERMINATE")

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if self.wait_hangs and not self.killed:
            raise subprocess.TimeoutExpired(cmd="radar.py", timeout=timeout)
        return self.exit_code


class ProcessManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock_path = os.path.join(self.tmp.name, "radar_ui_lock.json")
        self.identities = {}
        self.procs = []
        self.python_exe = sys.executable

    def tearDown(self):
        self.tmp.cleanup()

    def identity_for(self, pid):
        return self.identities.get(pid)

    def make_manager(self, *, proc_options=None, register_identity=True, pid_alive_fn=None):
        options = proc_options or {}

        def popen_fn(command, **kwargs):
            proc = FakeProc(**options)
            self.procs.append(proc)
            if register_identity:
                self.identities[proc.pid] = paths.ProcessIdentity(
                    proc.pid,
                    paths.normalized_executable_identity(self.python_exe),
                    len(self.procs),
                )
            return proc

        return ProcessManager(
            radar_py_path=os.path.join(self.tmp.name, "radar.py"),
            python_exe=self.python_exe,
            lock_path=self.lock_path,
            popen_fn=popen_fn,
            pid_alive_fn=pid_alive_fn or (lambda pid: pid in self.identities),
            process_identity_fn=self.identity_for,
            stop_timeout_seconds=0.05,
        )

    def wait_for_terminal_state(self, manager):
        deadline = time.time() + 2
        while manager.snapshot().state not in (STOPPED, FORCED_STOPPED, ERROR, UNKNOWN) and time.time() < deadline:
            time.sleep(0.01)
        return manager.snapshot()


class TestOwnedLifecycle(ProcessManagerTestCase):
    def test_start_records_owned_pid_executable_start_and_popen_handle(self):
        manager = self.make_manager()
        state = manager.start()
        self.assertEqual(state.state, RUNNING)
        self.assertEqual(state.ownership, "OWNED")
        self.assertEqual(len(self.procs), 1)
        self.assertIs(manager._launch_record.proc, self.procs[0])
        self.assertEqual(manager._launch_record.identity, self.identities[state.pid])

    def test_graceful_stop_uses_sigbreak_and_reports_stopped(self):
        manager = self.make_manager()
        manager.start()
        manager.stop()
        state = self.wait_for_terminal_state(manager)
        self.assertEqual(state.state, STOPPED)
        self.assertEqual(state.last_exit_code, 0)
        self.assertEqual(self.procs[0].signals, [signal.CTRL_BREAK_EVENT])
        self.assertFalse(self.procs[0].killed)
        self.assertFalse(os.path.exists(self.lock_path))

    def test_timeout_force_stops_only_owned_child_and_reports_distinct_state(self):
        manager = self.make_manager(proc_options={"wait_hangs": True})
        manager.start()
        manager.stop()
        state = self.wait_for_terminal_state(manager)
        self.assertEqual(state.state, FORCED_STOPPED)
        self.assertTrue(self.procs[0].killed)
        self.assertEqual(self.procs[0].signals, [signal.CTRL_BREAK_EVENT])

    def test_identity_change_during_timeout_blocks_forced_kill(self):
        manager = self.make_manager(proc_options={"wait_hangs": True})
        state = manager.start()
        original_wait = self.procs[0].wait

        def wait_then_reuse(timeout=None):
            self.identities[state.pid] = paths.ProcessIdentity(
                state.pid,
                paths.normalized_executable_identity(self.python_exe),
                999,
            )
            return original_wait(timeout)

        self.procs[0].wait = wait_then_reuse
        manager.stop()
        state = self.wait_for_terminal_state(manager)
        self.assertEqual(state.state, UNKNOWN)
        self.assertFalse(self.procs[0].killed)
        self.assertTrue(os.path.exists(self.lock_path))

    def test_stop_failure_retains_owned_handle_and_lock(self):
        manager = self.make_manager(proc_options={"send_error": RuntimeError("signal failed")})
        manager.start()
        manager.stop()
        state = self.wait_for_terminal_state(manager)
        self.assertEqual(state.state, ERROR)
        self.assertEqual(state.ownership, "OWNED")
        self.assertIn("ownership retained", state.last_error)
        self.assertIsNotNone(manager._proc)
        self.assertTrue(os.path.exists(self.lock_path))

    def test_pid_reuse_or_start_identity_change_blocks_stop_without_signal(self):
        manager = self.make_manager()
        state = manager.start()
        self.identities[state.pid] = paths.ProcessIdentity(
            state.pid,
            paths.normalized_executable_identity(self.python_exe),
            999,
        )
        manager.stop()
        state = manager.snapshot()
        self.assertEqual(state.state, UNKNOWN)
        self.assertEqual(state.ownership, "UNKNOWN")
        self.assertEqual(self.procs[0].signals, [])
        self.assertTrue(os.path.exists(self.lock_path))

    def test_popen_handle_mismatch_blocks_stop_without_touching_recorded_child(self):
        manager = self.make_manager()
        manager.start()
        manager._proc = FakeProc()
        manager.stop()
        self.assertEqual(manager.snapshot().state, UNKNOWN)
        self.assertEqual(self.procs[0].signals, [])
        self.assertTrue(os.path.exists(self.lock_path))

    def test_executable_identity_mismatch_is_unknown_not_running(self):
        manager = self.make_manager()
        original = manager._popen_fn

        def mismatched_popen(command, **kwargs):
            proc = original(command, **kwargs)
            self.identities[proc.pid] = paths.ProcessIdentity(proc.pid, "c:/other/python.exe", 1)
            return proc

        manager._popen_fn = mismatched_popen
        state = manager.start()
        self.assertEqual(state.state, UNKNOWN)
        self.assertEqual(state.ownership, "UNKNOWN")
        self.assertEqual(self.procs[0].signals, [])

    def test_start_identity_race_is_unknown_and_does_not_spawn_again(self):
        manager = self.make_manager(register_identity=False)
        state = manager.start()
        self.assertEqual(state.state, UNKNOWN)
        manager.start()
        self.assertEqual(len(self.procs), 1)

    def test_unknown_launch_refuses_stop_without_signal(self):
        manager = self.make_manager(register_identity=False)
        manager.start()
        manager.stop()
        self.assertEqual(manager.snapshot().state, UNKNOWN)
        self.assertEqual(self.procs[0].signals, [])


class TestAdoptedAndBoundedState(ProcessManagerTestCase):
    def test_adopted_lock_is_explicit_and_stop_never_clears_or_signals(self):
        self.identities[12345] = paths.ProcessIdentity(12345, "c:/python.exe", 1)
        with open(self.lock_path, "w", encoding="utf-8") as fh:
            json.dump({"pid": 12345, "started_at": 1.0}, fh)
        manager = self.make_manager()
        self.assertEqual(manager.snapshot().state, ADOPTED)
        manager.stop()
        state = manager.snapshot()
        self.assertEqual(state.state, ADOPTED)
        self.assertEqual(state.ownership, "ADOPTED")
        self.assertEqual(self.procs, [])
        self.assertTrue(os.path.exists(self.lock_path))

    def test_adopted_process_prevents_starting_a_second_child(self):
        self.identities[12345] = paths.ProcessIdentity(12345, "c:/python.exe", 1)
        with open(self.lock_path, "w", encoding="utf-8") as fh:
            json.dump({"pid": 12345, "started_at": 1.0}, fh)
        manager = self.make_manager()
        manager.start()
        self.assertEqual(manager.snapshot().state, ADOPTED)
        self.assertEqual(self.procs, [])

    def test_stdout_stderr_and_log_buffers_are_bounded_and_report_oldest_loss(self):
        manager = self.make_manager()
        manager.start()
        for index in range(505):
            manager._put_bounded(manager._stdout_q, f"line-{index}")
        self.assertEqual(manager._stdout_q.maxsize, 500)
        self.assertEqual(manager._stderr_q.maxsize, 500)
        self.assertEqual(manager._stdout_q.qsize(), 500)
        lines = manager.recent_log_lines(max_lines=500)
        self.assertLessEqual(len(lines), 500)
        self.assertIn("oldest line(s) discarded", lines[0])
        self.assertIn("line-504", lines)
        self.assertNotIn("line-0", lines)
        self.assertGreaterEqual(manager.snapshot().dropped_log_lines, 5)

    def test_combined_drained_log_buffer_discards_oldest_lines_at_five_hundred(self):
        manager = self.make_manager()
        manager.start()
        for index in range(500):
            manager._stdout_q.put_nowait(f"stdout-{index}")
            manager._stderr_q.put_nowait(f"stderr-{index}")
        lines = manager.recent_log_lines(max_lines=500)
        self.assertLessEqual(len(lines), 500)
        self.assertIn("oldest line(s) discarded", lines[0])
        self.assertGreaterEqual(manager.snapshot().dropped_log_lines, 500)


if __name__ == "__main__":
    unittest.main()
