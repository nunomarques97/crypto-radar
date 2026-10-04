"""The trend paper catch-up hook on the radar's loop start path (``radar_v08.cli._run_loop``).

* non-blocking: ``_run_loop`` returns while the catch-up's fetch is still blocked;
* failure isolation: import, thread start, client start, network, ledger and lock failures are
  logged and swallowed, and the loop proceeds;
* the ``RADAR_TREND_PAPER_ENABLED`` flag (default on) and its off path;
* the start-path review: each step raising on the loop path,
  a hung Kraken fetch, deadline and timeout errors, a read-only state dir, refused ledgers on the
  loop path, a single catch-up at a time (in this process and against another process) and
  ``radar_state.sqlite`` never opened.

No network (socket connections are refused for the whole module); fake fetchers and a temporary
state dir only.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import requests

TESTS_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import trend_paper_fakes as F  # noqa: E402  (offline market data and a fake Binance)
from trend_paper_fakes import (  # noqa: E402
    FakeFetcher,
    at,
    live_market,
)

import radar_v08  # noqa: E402
from radar_v08 import cli, config, notifications, trend_paper_hook  # noqa: E402
from radar_v08.adapters import trend_alert_store as alert_store  # noqa: E402
from radar_v08.adapters import trend_paper_store as store  # noqa: E402
from radar_v08.adapters.binance_public_klines import (  # noqa: E402
    KlinesError,
    KlinesErrorCode,
)
from radar_v08.adapters.kraken_public_ohlc import (  # noqa: E402
    KrakenOhlcError,
    KrakenOhlcErrorCode,
)
from radar_v08.domain import trend_paper_kraken as K  # noqa: E402

NOW = at(date(2026, 10, 6))
_patches = []
_audit_events: list | None = None  # (thread name, event, path) while a recorder is active


def _audit(event, args):
    events = _audit_events
    if events is None or event not in ("open", "sqlite3.connect"):
        return
    events.append((threading.current_thread().name, event, args[0] if args else None))


sys.addaudithook(_audit)  # audit hooks cannot be removed; it records only inside ``audited()``


@contextlib.contextmanager
def audited():
    global _audit_events
    events: list = []
    _audit_events = events
    try:
        yield events
    finally:
        _audit_events = None


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
        patcher = mock.patch(target, _refuse_network)
        patcher.start()
        _patches.append(patcher)


def tearDownModule():
    while _patches:
        _patches.pop().stop()


def hook_threads():
    return [t for t in threading.enumerate() if t.name == trend_paper_hook.THREAD_NAME]


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.path = store.ledger_path(self.state_dir)
        self.fetchers: list[FakeFetcher] = []
        self.addCleanup(self.join_hook)  # after the release below: no catch-up outlives its test
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.unhandled: list[threading.ExceptHookArgs] = []
        previous = threading.excepthook
        threading.excepthook = self.unhandled.append
        self.addCleanup(setattr, threading, "excepthook", previous)
        for target, name, value in (
            (config, "STATE_DIR", str(self.state_dir)),
            (config, "RADAR_TREND_PAPER_ENABLED", True),
            (trend_paper_hook, "utc_now", lambda: NOW),
            # A booking catch-up raises the paper-only alert toast: never launch a real one here.
            (notifications, "_ensure_script_on_disk", lambda: str(self.state_dir / "toast.ps1")),
            (notifications.subprocess, "run", mock.Mock(return_value=mock.Mock(returncode=0, stderr=""))),
            # The Kraken EUR step that follows gets its own offline fakes.
            (trend_paper_hook, "default_kraken_fetchers", lambda: (
                FakeFetcher(live_market(NOW)), F.FakeKrakenFetcher(F.kraken_market(NOW)),
            )),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_fetcher(self, **kwargs):
        def factory():
            fetcher = FakeFetcher(live_market(NOW), **kwargs)
            self.fetchers.append(fetcher)
            return fetcher

        patcher = mock.patch.object(trend_paper_hook, "default_fetcher", factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_loop(self):
        """``_run_loop`` with the cycles and the paper monitor stubbed; returns (code, seconds, cycles)."""
        with mock.patch.object(cli, "_loop_cycles", return_value=0) as cycles, \
                mock.patch.object(cli.paper_monitor, "start_monitor", return_value=None):
            started = time.monotonic()
            code = cli._run_loop()
            elapsed = time.monotonic() - started
        return code, elapsed, cycles.call_count

    def join_hook(self):
        for thread in hook_threads():
            thread.join(30)
            self.assertFalse(thread.is_alive())


class NonBlockingStart(HookCase):
    def test_loop_start_returns_while_the_fetch_is_blocked(self):
        self.use_fetcher(block=self.release)
        code, elapsed, cycles = self.run_loop()
        self.assertEqual((code, cycles), (0, 1))
        self.assertLess(elapsed, 2.0)
        threads = hook_threads()
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].daemon)
        self.assertTrue(threads[0].is_alive())
        for _ in range(200):
            if self.fetchers and self.fetchers[0].calls:
                break
            time.sleep(0.01)
        self.assertTrue(self.fetchers[0].calls)  # blocked inside the fetch
        self.release.set()
        self.join_hook()
        self.assertEqual(self.unhandled, [])
        ledger = store.read_ledger(self.path)
        self.assertEqual(ledger.days, (date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)))
        self.assertTrue(self.fetchers[0].closed)

    def test_the_hook_runs_once_per_start_and_is_idempotent(self):
        self.use_fetcher()
        self.run_loop()
        self.join_hook()
        data = self.path.read_bytes()
        self.run_loop()
        self.join_hook()
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual(len(self.fetchers), 2)


class FailureIsolation(HookCase):
    def assert_loop_unaffected(self):
        with self.assertLogs("radar_v08", level="WARNING") as logs:
            code, elapsed, cycles = self.run_loop()
            self.join_hook()
        self.assertEqual((code, cycles), (0, 1))
        self.assertLess(elapsed, 2.0)
        self.assertEqual(self.unhandled, [])
        return "\n".join(logs.output)

    def test_network_failure(self):
        self.use_fetcher(error=KlinesError(KlinesErrorCode.NETWORK, "down"))
        self.assertIn("Trend paper catch-up failed", self.assert_loop_unaffected())
        self.assertFalse(self.path.exists())

    def test_unexpected_exception_inside_the_fetch(self):
        self.use_fetcher(error=RuntimeError("boom"))
        self.assertIn("boom", self.assert_loop_unaffected())

    def test_refused_ledger(self):
        self.use_fetcher()
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'{"torn":')
        logs = self.assert_loop_unaffected()
        self.assertIn("Trend paper ledger refused (LEDGER_TORN", logs)
        self.assertIn("nothing was written and the file is never rewritten; see docs/guides/TREND-PAPER.md", logs)
        self.assertEqual(self.path.read_bytes(), b'{"torn":')
        self.assertEqual(self.fetchers[0].calls, [])  # refused before any request

    def test_network_faults_never_reach_the_loop_and_write_nothing(self):
        for name in F.fault_scenarios():
            with self.subTest(fault=name):
                faults, kind = F.fault_scenarios()[name]
                exchange = F.FakeExchange(live_market(NOW), faults)
                with mock.patch.object(trend_paper_hook, "default_fetcher", F.adapter_factory(exchange)), \
                        self.assertLogs("radar_v08", level="INFO") as logs:
                    code, elapsed, cycles = self.run_loop()
                    self.join_hook()
                self.assertEqual((code, cycles), (0, 1))
                self.assertLess(elapsed, 2.0)
                self.assertEqual(self.unhandled, [])
                text = "\n".join(logs.output)
                if kind == "market":
                    self.assertIn("WARNING:radar_v08.trend_paper:Trend paper catch-up failed (the radar is unaffected)", text)
                else:
                    self.assertIn("Trend paper catch-up waiting for data (nothing written; the next start retries)", text)
                self.assertFalse(self.path.exists())
        self.use_fetcher()  # the next start, with good data, books normally
        self.run_loop()
        self.join_hook()
        self.assertEqual(len(store.read_ledger(self.path).days), 3)

    def test_lock_held_elsewhere(self):
        self.use_fetcher()
        with store.LedgerWriter(self.path):
            self.assertIn("BUSY", self.assert_loop_unaffected())
        self.assertEqual(self.fetchers[0].calls, [])

    def test_client_start_failure(self):
        def broken():
            raise OSError("no session")

        with mock.patch.object(trend_paper_hook, "default_fetcher", broken):
            self.assertIn("market data client failed to start", self.assert_loop_unaffected())

    def test_thread_start_failure(self):
        with mock.patch.object(trend_paper_hook, "start_catch_up_thread", side_effect=RuntimeError("can't start new thread")):
            self.assertIn("Trend paper catch-up not started", self.assert_loop_unaffected())

    def test_import_failure(self):
        saved = radar_v08.__dict__.pop("trend_paper_hook")
        self.addCleanup(setattr, radar_v08, "trend_paper_hook", saved)
        with mock.patch.dict(sys.modules, {"radar_v08.trend_paper_hook": None}):
            self.assertIn("Trend paper catch-up not started", self.assert_loop_unaffected())
        self.assertEqual(hook_threads(), [])

    def test_run_guarded_never_raises(self):
        for error in (KlinesError(KlinesErrorCode.DEADLINE), ValueError("bad"), store.TrendPaperStoreError(store.TrendPaperStoreErrorCode.BUSY)):
            with self.subTest(error=error):
                fetcher = FakeFetcher(live_market(NOW), error=error)
                with self.assertLogs("radar_v08.trend_paper", level="WARNING"):
                    self.assertIsNone(trend_paper_hook.run_guarded(self.state_dir, lambda f=fetcher: f, lambda: NOW))
                self.assertTrue(fetcher.closed)


# ---------------------------------------------------------------------------
# The gaps of the start-path isolation
# ---------------------------------------------------------------------------

LEDGER_NAMES = (store.LEDGER_NAME, trend_paper_hook.KRAKEN_LEDGER_NAME)
LOCK_NAMES = tuple(name + store.LOCK_SUFFIX for name in LEDGER_NAMES)
ALERT_NAMES = (alert_store.ALERTS_NAME, alert_store.ALERTS_NAME + store.LOCK_SUFFIX)

HOLDER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from radar_v08 import trend_paper_hook as hook
from radar_v08.adapters import trend_paper_store as store
from radar_v08.domain.trend_paper_kraken import extend_kraken_ledger
state = Path(sys.argv[2])
with store.LedgerWriter(store.ledger_path(state)), \\
        store.LedgerWriter(hook.kraken_ledger_path(state), extend=extend_kraken_ledger):
    print("locked", flush=True)
    sys.stdin.readline()
"""


def kraken_ok():
    return FakeFetcher(live_market(NOW)), F.FakeKrakenFetcher(F.kraken_market(NOW))


@contextlib.contextmanager
def read_only(*, dirs=(), names=(), failing_writes=()):
    """A read-only state dir, simulated portably (directory permissions are not enforced on
    Windows): creating a directory named in ``dirs`` or opening a file named in ``names`` raises
    ``PermissionError``; a write to a file named in ``failing_writes`` raises ``OSError`` after a
    successful open."""
    real_open, real_write, real_close, real_mkdir = os.open, os.write, os.close, Path.mkdir
    refused_fds: set[int] = set()

    def fake_open(path, flags, *args, **kwargs):
        name = os.path.basename(os.fspath(path))
        if name in names:
            raise PermissionError(13, "Permission denied (read-only state dir)", os.fspath(path))
        fd = real_open(path, flags, *args, **kwargs)
        if name in failing_writes:
            refused_fds.add(fd)
        return fd

    def fake_write(fd, data):
        if fd in refused_fds:
            raise OSError(13, "Permission denied (read-only state dir)")
        return real_write(fd, data)

    def fake_close(fd):
        refused_fds.discard(fd)
        return real_close(fd)

    def fake_mkdir(self, *args, **kwargs):
        if self.name in dirs:
            raise PermissionError(13, "Permission denied (read-only state dir)", str(self))
        return real_mkdir(self, *args, **kwargs)

    with mock.patch("os.open", fake_open), mock.patch("os.write", fake_write), \
            mock.patch("os.close", fake_close), mock.patch.object(Path, "mkdir", fake_mkdir):
        yield


class StartPathCase(HookCase):
    def setUp(self):
        super().setUp()
        self.kraken_path = trend_paper_hook.kraken_ledger_path(self.state_dir)
        self.alerts_path = alert_store.alerts_path(self.state_dir)
        self.toast_run = notifications.subprocess.run  # HookCase's stub of the toast subprocess

    def use_kraken(self, factory):
        patcher = mock.patch.object(trend_paper_hook, "default_kraken_fetchers", factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_loop_unaffected(self):
        with self.assertLogs("radar_v08", level="WARNING") as logs:
            code, elapsed, cycles = self.run_loop()
            self.join_hook()
        self.assertEqual((code, cycles), (0, 1))
        self.assertLess(elapsed, 2.0)
        self.assertEqual(self.unhandled, [])
        return "\n".join(logs.output)

    def reference(self):
        """The ledger bytes of a single catch-up at ``NOW`` in a fresh state dir."""
        with tempfile.TemporaryDirectory() as other:
            trend_paper_hook.run_guarded(other, lambda: FakeFetcher(live_market(NOW)), lambda: NOW, kraken_ok)
            self.toast_run.reset_mock()  # its toasts are not the test's
            return store.ledger_path(other).read_bytes(), trend_paper_hook.kraken_ledger_path(other).read_bytes()

    def clear(self):
        for path in (self.path, self.kraken_path, self.alerts_path):
            path.unlink(missing_ok=True)

    def assert_no_ledger_bytes(self):
        for path in (self.path, self.kraken_path):
            self.assertEqual(path.read_bytes() if path.exists() else b"", b"", path.name)

    def wait_for(self, condition):
        for _ in range(1000):
            if condition():
                return
            time.sleep(0.01)
        self.fail("condition not reached")


class EachStepRaising(StartPathCase):
    """Raise: the Binance fetch raising is ``test_unexpected_exception_inside_the_fetch``; here the
    Kraken fetch, the Kraken step itself and the alert step raise on the loop path."""

    def test_the_kraken_and_alert_steps_raising_never_reach_the_loop(self):
        def kraken_raises():
            return FakeFetcher(live_market(NOW)), F.FakeKrakenFetcher(F.kraken_market(NOW), error=RuntimeError("kraken boom"))

        main_ref, _ = self.reference()
        scenarios = {
            "kraken fetch": (
                mock.patch.object(trend_paper_hook, "default_kraken_fetchers", kraken_raises),
                "Kraken EUR paper catch-up failed (the radar is unaffected): kraken boom",
            ),
            "kraken step": (
                mock.patch.object(trend_paper_hook, "run_kraken_guarded", side_effect=RuntimeError("kraken step boom")),
                "Kraken EUR paper catch-up failed (the radar is unaffected)",
            ),
            "alert step": (
                mock.patch.object(trend_paper_hook, "alert_exposure_changes", side_effect=RuntimeError("alert boom")),
                "Trend paper alert step failed (no toast; the radar is unaffected): alert boom",
            ),
        }
        self.use_fetcher()
        for name, (patch, expected) in scenarios.items():
            with self.subTest(step=name):
                self.clear()
                self.toast_run.reset_mock()
                with patch:
                    logs = self.assert_loop_unaffected()
                self.assertIn(expected, logs)
                self.assertEqual(self.path.read_bytes(), main_ref)  # the Binance step booked whole
                if name == "alert step":
                    self.toast_run.assert_not_called()
                    self.assertFalse(self.alerts_path.exists())
                    self.assertEqual(len(store.read_ledger(self.kraken_path, extend=K.extend_kraken_ledger).days), 3)
                else:
                    self.assertEqual(self.toast_run.call_count, 4)
                    self.assertFalse(self.kraken_path.exists())


class Hang(StartPathCase):
    """Hang: the blocked Binance fetch is ``test_loop_start_returns_while_the_fetch_is_blocked``."""

    def test_loop_start_returns_while_a_kraken_fetch_is_blocked(self):
        self.use_fetcher()
        kraken = F.FakeKrakenFetcher(F.kraken_market(NOW), block=self.release)
        self.use_kraken(lambda: (FakeFetcher(live_market(NOW)), kraken))
        code, elapsed, cycles = self.run_loop()
        self.assertEqual((code, cycles), (0, 1))
        self.assertLess(elapsed, 2.0)
        (thread,) = hook_threads()
        self.assertTrue(thread.daemon)
        self.wait_for(lambda: kraken.calls)  # blocked inside the Kraken fetch
        self.assertTrue(thread.is_alive())
        self.assertFalse(self.kraken_path.exists())
        self.release.set()
        self.join_hook()
        self.assertEqual(self.unhandled, [])
        self.assertEqual(len(store.read_ledger(self.kraken_path, extend=K.extend_kraken_ledger).days), 3)
        self.assertTrue(kraken.closed)

    def test_deadline_and_timeout_errors_are_logged_swallowed_and_write_nothing(self):
        """The real adapters over fake HTTP: Binance gives ``DEADLINE`` (a read timeout whose retry
        wait would pass the fetch deadline), Kraken a read timeout after its retries (``NETWORK``)."""
        binance = F.FakeExchange(live_market(NOW), {"BTCUSDT": [requests.exceptions.ReadTimeout("read timed out")]})
        kraken = F.FakeKrakenExchange(
            F.kraken_market(NOW), {"XBTEUR": [requests.exceptions.ReadTimeout("read timed out") for _ in range(3)]}
        )
        with mock.patch.object(trend_paper_hook, "default_fetcher", F.adapter_factory(binance, deadline_seconds=0.5)):
            self.use_kraken(lambda: (FakeFetcher(live_market(NOW)), F.kraken_adapter_factory(kraken)()))
            logs = self.assert_loop_unaffected()
        self.assertIn("Trend paper catch-up failed (the radar is unaffected): DEADLINE: BTCUSDT: a 1 s retry wait", logs)
        self.assertEqual(len(binance.symbol_calls("BTCUSDT")), 1)
        self.assertIn("Kraken EUR paper catch-up failed (the radar is unaffected): NETWORK", logs)
        self.assertIn("read timed out", logs)
        self.assertEqual(len(kraken.pair_calls("XBTEUR")), 3)  # bounded retries, then the error
        self.assertFalse(self.path.exists())
        self.assertFalse(self.kraken_path.exists())
        self.assertFalse(self.alerts_path.exists())
        self.toast_run.assert_not_called()


class ReadOnlyStateDir(StartPathCase):
    def test_a_read_only_state_dir_never_reaches_the_loop_and_writes_no_ledger(self):
        self.use_fetcher()
        self.use_kraken(kraken_ok)
        scenarios = {
            "trend_paper directory": dict(dirs=(store.LEDGER_DIR_NAME,)),
            "lock files": dict(names=LOCK_NAMES),
            "ledger append open": dict(names=LEDGER_NAMES),
            "ledger append write": dict(failing_writes=LEDGER_NAMES),
            "every file": dict(names=LEDGER_NAMES + LOCK_NAMES + ALERT_NAMES),
        }
        for name, refusal in scenarios.items():
            with self.subTest(refused=name):
                self.clear()
                with read_only(**refusal):
                    logs = self.assert_loop_unaffected()
                self.assertIn("WARNING:radar_v08.trend_paper:Trend paper catch-up failed (the radar is unaffected)", logs)
                self.assertIn("WARNING:radar_v08.trend_paper:Kraken EUR paper catch-up failed (the radar is unaffected)", logs)
                self.assertIn("Permission denied (read-only state dir)", logs)
                self.assert_no_ledger_bytes()
                self.assertFalse(self.alerts_path.exists())
                self.toast_run.assert_not_called()
        self.clear()  # the next start with a writable state dir books normally
        code, _, cycles = self.run_loop()
        self.join_hook()
        self.assertEqual((code, cycles), (0, 1))
        self.assertEqual(len(store.read_ledger(self.path).days), 3)
        self.assertEqual(len(store.read_ledger(self.kraken_path, extend=K.extend_kraken_ledger).days), 3)

    def test_a_read_only_alerts_file_books_the_ledgers_and_sends_no_toast(self):
        main_ref, kraken_ref = self.reference()
        self.use_fetcher()
        self.use_kraken(kraken_ok)
        for name, refusal in {"alerts lock": (ALERT_NAMES[1],), "alerts file": (ALERT_NAMES[0],)}.items():
            with self.subTest(refused=name):
                self.clear()
                with read_only(names=refusal):
                    logs = self.assert_loop_unaffected()
                self.assertIn("Trend paper alert step failed (no toast; the radar is unaffected)", logs)
                self.assertIn("Permission denied (read-only state dir)", logs)
                self.toast_run.assert_not_called()
                self.assertFalse(self.alerts_path.exists())
                self.assertEqual((self.path.read_bytes(), self.kraken_path.read_bytes()), (main_ref, kraken_ref))

    def test_read_only_file_attributes(self):
        """Real read-only attributes (enforced on Windows for files): read-only lock files, then
        read-only empty ledgers under writable locks."""
        self.use_fetcher()
        self.use_kraken(kraken_ok)
        directory = self.path.parent
        directory.mkdir(parents=True)

        def make_read_only(names):
            for name in names:
                path = directory / name
                path.write_bytes(b"")
                os.chmod(path, stat.S_IREAD)
                self.addCleanup(os.chmod, path, stat.S_IREAD | stat.S_IWRITE)
            try:
                os.close(os.open(directory / names[0], os.O_RDWR))
            except PermissionError:
                return
            self.skipTest("read-only file attributes are not enforced here (e.g. running as root)")

        make_read_only(LOCK_NAMES)
        self.assertIn("UNREADABLE", self.assert_loop_unaffected())
        self.assertFalse(self.path.exists() or self.kraken_path.exists())
        for name in LOCK_NAMES:
            os.chmod(directory / name, stat.S_IREAD | stat.S_IWRITE)
        make_read_only(LEDGER_NAMES)
        self.assertIn("WRITE_FAILED", self.assert_loop_unaffected())
        self.assert_no_ledger_bytes()
        self.toast_run.assert_not_called()


class RefusedOnTheLoopPath(StartPathCase):
    """Ledger refused: ``test_refused_ledger`` covers a torn ``ledger.jsonl``; here both ledgers,
    torn and edited, on the loop path."""

    def test_torn_or_edited_ledgers_are_refused_left_byte_identical_and_the_loop_runs(self):
        main_ref, kraken_ref = self.reference()
        self.use_fetcher()
        self.use_kraken(kraken_ok)
        damaged = {
            "torn": (main_ref[:-3], kraken_ref[:-3]),
            "edited": (main_ref.replace(b"2026-10-05", b"2026-10-07", 1), kraken_ref.replace(b"2026-10-05", b"2026-10-07", 1)),
        }
        self.path.parent.mkdir(parents=True)
        for name, (main, kraken) in damaged.items():
            with self.subTest(damage=name):
                self.assertNotEqual((main, kraken), (main_ref, kraken_ref))
                self.path.write_bytes(main)
                self.kraken_path.write_bytes(kraken)
                self.fetchers.clear()
                logs = self.assert_loop_unaffected()
                self.assertIn("Trend paper ledger refused (", logs)
                self.assertIn("Kraken EUR paper ledger refused (", logs)
                self.assertEqual((self.path.read_bytes(), self.kraken_path.read_bytes()), (main, kraken))
                self.assertEqual(self.fetchers[0].calls, [])  # refused before any request
                self.toast_run.assert_not_called()


class SingleInstance(StartPathCase):
    def test_a_second_start_while_one_is_alive_starts_no_second_catch_up(self):
        main_ref, kraken_ref = self.reference()
        self.use_fetcher(block=self.release)
        kraken_factory = mock.Mock(side_effect=kraken_ok)
        self.use_kraken(kraken_factory)
        first = trend_paper_hook.start_catch_up_thread(self.state_dir)
        self.wait_for(lambda: self.fetchers and self.fetchers[0].calls)  # blocked inside the Binance fetch
        code, elapsed, cycles = self.run_loop()  # a second radar start through the real loop path
        self.assertEqual((code, cycles), (0, 1))
        self.assertLess(elapsed, 2.0)
        self.assertIs(trend_paper_hook.start_catch_up_thread(self.state_dir), first)  # and a third, directly
        self.assertEqual(hook_threads(), [first])
        self.assertEqual(len(self.fetchers), 1)
        kraken_factory.assert_not_called()  # no second catch-up runs the Kraken step meanwhile
        self.release.set()
        self.join_hook()
        self.assertEqual(self.unhandled, [])
        self.assertEqual(kraken_factory.call_count, 1)
        self.assertEqual((self.path.read_bytes(), self.kraken_path.read_bytes()), (main_ref, kraken_ref))
        self.use_fetcher()  # once it has ended, the next start runs again
        self.assertIsNot(trend_paper_hook.start_catch_up_thread(self.state_dir), first)
        self.join_hook()
        self.assertEqual(kraken_factory.call_count, 2)

    def test_two_concurrent_runs_leave_exactly_one_writer_per_ledger(self):
        """Below the start guard, the per-ledger OS lock: a run blocked inside a fetch holds the
        lock; a second run of the same step gets BUSY before any request and writes nothing."""
        main_ref, kraken_ref = self.reference()
        for step in ("binance", "kraken"):
            with self.subTest(step=step):
                self.clear()
                release = threading.Event()
                self.addCleanup(release.set)
                if step == "binance":
                    blocked = FakeFetcher(live_market(NOW), block=release)
                    first = threading.Thread(
                        target=trend_paper_hook.run_guarded, args=(self.state_dir, lambda: blocked, lambda: NOW, kraken_ok)
                    )
                else:
                    blocked = F.FakeKrakenFetcher(F.kraken_market(NOW), block=release)
                    first = threading.Thread(
                        target=trend_paper_hook.run_kraken_guarded,
                        args=(self.state_dir, lambda: (FakeFetcher(live_market(NOW)), blocked), lambda: NOW),
                    )
                first.start()
                self.wait_for(lambda: blocked.calls)
                second_binance, (k_signals, k_kraken) = FakeFetcher(live_market(NOW)), kraken_ok()
                with self.assertLogs("radar_v08.trend_paper", level="INFO") as logs:
                    if step == "binance":
                        result = trend_paper_hook.run_guarded(
                            self.state_dir, lambda: second_binance, lambda: NOW, lambda: (k_signals, k_kraken)
                        )
                        self.assertIsNone(result)
                        self.assertEqual(second_binance.calls, [])
                        self.assertFalse(self.path.exists())
                    else:
                        result = trend_paper_hook.run_kraken_guarded(self.state_dir, lambda: (k_signals, k_kraken), lambda: NOW)
                        self.assertIsNone(result)
                        self.assertEqual((k_signals.calls, k_kraken.calls), ([], []))
                        self.assertFalse(self.kraken_path.exists())
                self.assertIn("BUSY: another trend paper run holds the lock", "\n".join(logs.output))
                release.set()
                first.join(30)
                self.assertFalse(first.is_alive())
                if step == "binance":
                    self.assertEqual(self.path.read_bytes(), main_ref)
                    self.assertEqual(self.kraken_path.read_bytes(), kraken_ref)  # booked by the second run alone
                else:
                    self.assertEqual(self.kraken_path.read_bytes(), kraken_ref)
        self.assertEqual(self.unhandled, [])

    def test_a_lock_holder_in_another_process_makes_both_steps_busy_and_the_loop_runs(self):
        main_ref, kraken_ref = self.reference()
        self.use_fetcher()
        made: list = []
        self.use_kraken(lambda: made.append(kraken_ok()) or made[-1])
        holder = subprocess.Popen(
            [sys.executable, "-B", "-c", HOLDER, str(REPOSITORY_ROOT), str(self.state_dir)],
            cwd=REPOSITORY_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        for cleanup in (lambda: holder.wait(30), holder.kill, holder.stdin.close, holder.stdout.close):
            self.addCleanup(cleanup)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        logs = self.assert_loop_unaffected()
        self.assertIn("Trend paper catch-up failed (the radar is unaffected): BUSY", logs)
        self.assertIn("Kraken EUR paper catch-up failed (the radar is unaffected): BUSY", logs)
        self.assertEqual(self.fetchers[0].calls, [])
        self.assertEqual([(s.calls, k.calls) for s, k in made], [([], [])])  # BUSY before any request
        self.assertFalse(self.path.exists() or self.kraken_path.exists() or self.alerts_path.exists())
        holder.stdin.write("\n")
        holder.stdin.flush()
        self.assertEqual(holder.wait(30), 0)
        code, _, cycles = self.run_loop()  # the holder is gone: the next start books
        self.join_hook()
        self.assertEqual((code, cycles), (0, 1))
        self.assertEqual((self.path.read_bytes(), self.kraken_path.read_bytes()), (main_ref, kraken_ref))


class RadarStateSqlite(StartPathCase):
    """``radar_state.sqlite`` (``config.SQLITE_PATH``, a sentinel in the temporary state dir) is never
    opened on the start path, whether the catch-up fails or books; no thread connects to SQLite."""

    def test_radar_state_sqlite_is_never_opened_on_failure_or_booking(self):
        sentinel = self.state_dir / "radar_state.sqlite"
        content = b"SQLite format 3\x00 sentinel: the radar's own database"
        sentinel.write_bytes(content)
        os.utime(sentinel, ns=(1_000_000_000, 1_000_000_000))
        patcher = mock.patch.object(config, "SQLITE_PATH", str(sentinel))
        patcher.start()
        self.addCleanup(patcher.stop)

        def network_down():
            return FakeFetcher(live_market(NOW), error=KlinesError(KlinesErrorCode.NETWORK, "down"))

        def kraken_down():
            return FakeFetcher(live_market(NOW)), F.FakeKrakenFetcher(
                F.kraken_market(NOW), error=KrakenOhlcError(KrakenOhlcErrorCode.NETWORK, "down")
            )

        good = lambda: FakeFetcher(live_market(NOW))  # noqa: E731
        scenarios = {
            "network down": (network_down, kraken_down, contextlib.nullcontext()),
            "read-only state dir": (good, kraken_ok, read_only(names=LOCK_NAMES)),
            "booking with alerts": (good, kraken_ok, contextlib.nullcontext()),
        }
        target = os.path.normcase(os.path.abspath(sentinel))
        for name, (binance, kraken, context) in scenarios.items():
            with self.subTest(scenario=name):
                self.clear()
                self.toast_run.reset_mock()
                with mock.patch.object(trend_paper_hook, "default_fetcher", binance), \
                        mock.patch.object(trend_paper_hook, "default_kraken_fetchers", kraken), context, \
                        self.assertLogs("radar_v08", level="INFO"), audited() as events:
                    code, _, cycles = self.run_loop()
                    self.join_hook()
                self.assertEqual((code, cycles), (0, 1))
                self.assertEqual(self.unhandled, [])
                hook_events = [e for e in events if e[0] == trend_paper_hook.THREAD_NAME]
                self.assertTrue(any(e[1] == "open" for e in hook_events))  # the recorder sees the hook thread
                self.assertEqual([e for e in events if e[1] == "sqlite3.connect"], [])
                opened = [
                    e for e in events
                    if e[1] == "open" and isinstance(e[2], (str, bytes, os.PathLike))
                    and os.path.normcase(os.path.abspath(os.fsdecode(e[2]))) == target
                ]
                self.assertEqual(opened, [])
                self.assertEqual(sentinel.read_bytes(), content)
                self.assertEqual(sentinel.stat().st_mtime_ns, 1_000_000_000)
                if name == "booking with alerts":
                    self.assertEqual(self.toast_run.call_count, 4)
                    self.assertEqual(len(store.read_ledger(self.path).days), 3)
                    self.assertEqual(len(store.read_ledger(self.kraken_path, extend=K.extend_kraken_ledger).days), 3)
                else:
                    self.assert_no_ledger_bytes()
        self.assertEqual(sorted(p.name for p in self.state_dir.glob("*.sqlite*")), ["radar_state.sqlite"])


class Flag(HookCase):
    def test_flag_defaults_on(self):
        source = (REPOSITORY_ROOT / "radar_v08" / "config.py").read_text(encoding="utf-8")
        self.assertIn('RADAR_TREND_PAPER_ENABLED = os.getenv("RADAR_TREND_PAPER_ENABLED", "1")', source)

    def test_flag_off_starts_nothing(self):
        self.use_fetcher()
        with mock.patch.object(config, "RADAR_TREND_PAPER_ENABLED", False), \
                mock.patch.object(trend_paper_hook, "start_catch_up_thread") as start, \
                self.assertLogs("radar_v08.cli", level="INFO") as logs:
            code, _, cycles = self.run_loop()
        self.assertEqual((code, cycles), (0, 1))
        start.assert_not_called()
        self.assertEqual(hook_threads(), [])
        self.assertEqual(self.fetchers, [])
        self.assertFalse(self.path.parent.exists())
        self.assertIn("Trend paper catch-up off", "\n".join(logs.output))

    def test_loop_mode_calls_the_hook_before_the_cycles(self):
        order = []
        with mock.patch.object(cli, "_start_trend_paper_catch_up", side_effect=lambda: order.append("hook")), \
                mock.patch.object(cli.paper_monitor, "start_monitor", return_value=None), \
                mock.patch.object(cli, "_loop_cycles", side_effect=lambda: order.append("cycles") or 0):
            self.assertEqual(cli.run_mode("loop"), 0)
        self.assertEqual(order, ["hook", "cycles"])


class FlagParsing(unittest.TestCase):
    """The real environment parsing, in a fresh interpreter
    (outside ``HookCase``, which stubs ``subprocess.run`` for the toast)."""

    def test_flag_is_read_from_the_environment(self):
        code = "from radar_v08 import config; print(config.RADAR_TREND_PAPER_ENABLED)"
        base = {k: v for k, v in os.environ.items() if k != "RADAR_TREND_PAPER_ENABLED"}
        for value, expected in ((None, "True"), ("1", "True"), ("0", "False"), ("false", "False"), ("False", "False")):
            env = dict(base) if value is None else {**base, "RADAR_TREND_PAPER_ENABLED": value}
            with self.subTest(value=value):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", code], cwd=REPOSITORY_ROOT, env=env,
                    capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
