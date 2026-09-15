import io
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ui import process_manager
from ui.process_manager import ERROR, RUNNING, STOPPED, STOPPING, ProcessManager


class FakeProc:
    """Stands in for subprocess.Popen - exposes exactly what ProcessManager
    touches (pid, stdout, stderr, wait, terminate, kill, send_signal), never
    a real OS process.
    """

    _next_pid = 9000

    def __init__(self, terminate_hangs: bool = False):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.stdout = io.StringIO("heartbeat ok\ncycle complete\n")
        self.stderr = io.StringIO("")
        self._terminated = False
        self._killed = False
        self._terminate_hangs = terminate_hangs
        self.exit_code = 0

    def send_signal(self, sig):
        self._terminated = True

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._killed = True

    def wait(self, timeout=None):
        if self._terminate_hangs and not self._killed:
            raise subprocess.TimeoutExpired(cmd="radar.py", timeout=timeout)
        return self.exit_code


def always_dead(pid, windll_factory=None):
    return False


def always_alive(pid, windll_factory=None):
    return True


class ProcessManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.tmp_dir, "radar_ui_lock.json")
        self.fake_procs = []

    def tearDown(self):
        for name in os.listdir(self.tmp_dir):
            os.remove(os.path.join(self.tmp_dir, name))
        os.rmdir(self.tmp_dir)

    def make_manager(self, pid_alive_fn=always_dead, terminate_hangs=False, **kwargs):
        def popen_fn(cmd, **popen_kwargs):
            proc = FakeProc(terminate_hangs=terminate_hangs)
            self.fake_procs.append(proc)
            return proc

        params = dict(
            radar_py_path=os.path.join(self.tmp_dir, "radar.py"),
            python_exe=sys.executable,
            lock_path=self.lock_path,
            popen_fn=popen_fn,
            pid_alive_fn=pid_alive_fn,
            stop_timeout_seconds=0.2,
        )
        params.update(kwargs)
        return ProcessManager(**params)


class TestStartStop(ProcessManagerTestCase):
    def test_initial_state_is_stopped(self):
        mgr = self.make_manager()
        self.assertEqual(mgr.snapshot().state, STOPPED)

    def test_start_spawns_and_reports_running(self):
        mgr = self.make_manager()
        state = mgr.start()
        self.assertEqual(state.state, RUNNING)
        self.assertIsNotNone(state.pid)
        self.assertEqual(len(self.fake_procs), 1)

    def test_start_writes_lock_file(self):
        mgr = self.make_manager()
        mgr.start()
        self.assertTrue(os.path.exists(self.lock_path))

    def test_double_start_does_not_spawn_a_second_process(self):
        mgr = self.make_manager()
        mgr.start()
        mgr.start()
        self.assertEqual(len(self.fake_procs), 1)

    def test_stop_transitions_through_stopping_to_stopped(self):
        mgr = self.make_manager()
        mgr.start()
        mgr.stop()
        # stop() runs on a background thread - wait for it.
        deadline = time.time() + 2
        while mgr.snapshot().state not in (STOPPED, ERROR) and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(mgr.snapshot().state, STOPPED)
        self.assertTrue(self.fake_procs[0]._terminated)

    def test_stop_clears_lock_file(self):
        mgr = self.make_manager()
        mgr.start()
        mgr.stop()
        deadline = time.time() + 2
        while mgr.snapshot().state != STOPPED and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(os.path.exists(self.lock_path))

    def test_stop_escalates_to_kill_when_graceful_stop_hangs(self):
        mgr = self.make_manager(terminate_hangs=True)
        mgr.start()
        mgr.stop()
        deadline = time.time() + 3
        while mgr.snapshot().state != STOPPED and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(mgr.snapshot().state, STOPPED)
        self.assertTrue(self.fake_procs[0]._killed)

    def test_stop_when_already_stopped_is_a_no_op(self):
        mgr = self.make_manager()
        mgr.stop()  # never started
        self.assertEqual(mgr.snapshot().state, STOPPED)
        self.assertEqual(len(self.fake_procs), 0)


class TestAdoptExistingLock(ProcessManagerTestCase):
    def test_live_pid_in_lock_file_is_adopted_as_running(self):
        import json
        with open(self.lock_path, "w", encoding="utf-8") as fh:
            json.dump({"pid": 12345, "started_at": time.time()}, fh)
        mgr = self.make_manager(pid_alive_fn=always_alive)
        self.assertEqual(mgr.snapshot().state, RUNNING)

    def test_dead_pid_in_lock_file_is_cleared(self):
        import json
        with open(self.lock_path, "w", encoding="utf-8") as fh:
            json.dump({"pid": 12345, "started_at": time.time()}, fh)
        mgr = self.make_manager(pid_alive_fn=always_dead)
        self.assertEqual(mgr.snapshot().state, STOPPED)
        self.assertFalse(os.path.exists(self.lock_path))

    def test_adopted_running_state_prevents_a_second_spawn(self):
        import json
        with open(self.lock_path, "w", encoding="utf-8") as fh:
            json.dump({"pid": 12345, "started_at": time.time()}, fh)
        mgr = self.make_manager(pid_alive_fn=always_alive)
        mgr.start()
        self.assertEqual(len(self.fake_procs), 0)


class TestLockNeverTouchesSqlite(ProcessManagerTestCase):
    def test_lock_path_lives_under_ui_state_dir_not_sqlite(self):
        mgr = self.make_manager()
        self.assertNotIn("radar_state.sqlite", mgr.lock_path)
        self.assertTrue(mgr.lock_path.endswith("radar_ui_lock.json"))


class TestNoInterpreterFound(ProcessManagerTestCase):
    def test_missing_interpreter_reports_error_not_silent_hang(self):
        mgr = self.make_manager(pid_alive_fn=always_dead, python_exe=None)
        mgr.python_exe = None
        import ui.process_manager as pm_module
        original = pm_module._find_python_exe
        pm_module._find_python_exe = lambda: None
        try:
            state = mgr.start()
        finally:
            pm_module._find_python_exe = original
        self.assertEqual(state.state, ERROR)
        self.assertIsNotNone(state.last_error)


if __name__ == "__main__":
    unittest.main()
