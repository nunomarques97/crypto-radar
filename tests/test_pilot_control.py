"""scripts/pilot_control.py: the pilot shadow's kill switch, lock reviews and status.

Only temporary databases are used. The script refuses a missing database or one without
the pilot tables (creating neither), writes only through ``pilot_store`` (one appended row
per command, nothing else changes), opens ``status`` read-only and never opens a socket.

Pilot position: envelope 240.00 EUR at EX-1, fee 26 bps, quote 99.9 / 100, ATR 1, pair
rules lot 2 / ordermin 0.01 / costmin 0.50 / tick 0.01 -> stop 98, target 104, 0.19 units.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
SCRIPT = os.path.join(REPO_DIR, "scripts", "pilot_control.py")
sys.path.insert(0, REPO_DIR)

from radar_v08 import pilot_shadow  # noqa: E402
from radar_v08.adapters import pilot_store  # noqa: E402
from radar_v08.adapters.paper_store import ObservedQuote  # noqa: E402
from radar_v08.domain.risk import Envelope, LockKind, NoTradeReason  # noqa: E402
from radar_v08.store import SnapshotStore  # noqa: E402

D = Decimal
T0 = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
ENVELOPE = Envelope(D("240.00"), "EUR")
PAIR = "DOTEUR"
RULES = {"lot_decimals": 2, "ordermin": "0.01", "costmin": "0.50", "tick_size": "0.01"}


def _load():
    spec = importlib.util.spec_from_file_location("pilot_control", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pilot_control = _load()


def candidate(event_id: str, at: datetime = T0) -> pilot_store.PilotCandidate:
    return pilot_store.PilotCandidate(
        event_id=event_id, run_id="run-1", asset="DOT", pair=PAIR, quote="EUR", direction="LONG",
        bid=D("99.9"), ask=D("100"), snapshot_ts=at.isoformat(), status="online", atr=D("1"), atr_pair=PAIR,
        pair_entry=dict(RULES),
    )


class ControlCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="pilot-control-")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "radar_state.sqlite")
        SnapshotStore(self.path).close()  # every radar table, as in production
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        # No command may open a socket.
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("network"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ready(self) -> None:
        pilot_shadow.prepare(self.conn, T0, envelope=ENVELOPE)

    def run_control(self, *argv: str, db: str | None = None, at: datetime = T0 + timedelta(hours=1)):
        out, err = io.StringIO(), io.StringIO()
        code = pilot_control.main(["--db", db or self.path, *argv], clock=lambda: at, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def open_candidates(self, *candidates: pilot_store.PilotCandidate, at: datetime = T0):
        return pilot_store.open_candidates(self.conn, list(candidates), envelope=ENVELOPE, fee_bps=D("26"), now=at)

    def snapshot(self) -> dict[str, list[tuple[object, ...]]]:
        """Every row of every table, to prove what a command changed."""
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            names = [name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            return {name: conn.execute(f'SELECT * FROM "{name}"').fetchall() for name in names}

    def changed(self, before, after) -> dict[str, int]:
        return {name: len(after[name]) - len(before.get(name, [])) for name in after if after[name] != before.get(name)}

    def trip_daily_lock(self) -> pilot_store.StoredLock:
        self.ready()
        self.assertTrue(self.open_candidates(candidate("e-1")).decisions[0].opened)
        gap = ObservedQuote(PAIR, D("80"), D("80.1"), T0 + timedelta(minutes=10), "ticker")
        pilot_store.close_positions(self.conn, now=T0 + timedelta(minutes=10), extra_quotes=[gap])
        (lock,) = pilot_store.active_locks(self.conn)
        self.assertEqual(lock.kind, LockKind.DAILY_LOSS)
        return lock


class TestRefusals(ControlCase):
    def test_a_missing_database_is_refused_and_never_created(self):
        missing = os.path.join(self.dir, "nothing-here.sqlite")
        for argv in (["status"], ["kill", "--reason", "x"], ["release", "--reason", "x"],
                     ["review-lock", "--lock-id", "1", "--cause", "c", "--reviewer", "r"]):
            with self.subTest(argv=argv):
                code, _out, err = self.run_control(*argv, db=missing)
                self.assertEqual(code, 2)
                self.assertIn("no database", err)
                self.assertFalse(os.path.exists(missing))

    def test_a_database_without_the_pilot_tables_is_refused_and_left_untouched(self):
        before = self.snapshot()
        for argv in (["status"], ["kill", "--reason", "x"],
                     ["review-lock", "--lock-id", "1", "--cause", "c", "--reviewer", "r"]):
            with self.subTest(argv=argv):
                code, _out, err = self.run_control(*argv)
                self.assertEqual(code, 2)
                self.assertIn("no pilot shadow tables", err)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(pilot_store.schema_present(self.conn))

    def test_empty_text_is_refused_by_the_store_and_writes_nothing(self):
        self.ready()
        before = self.snapshot()
        for argv in (["kill", "--reason", "  "], ["review-lock", "--lock-id", "1", "--cause", "", "--reviewer", "r"]):
            with self.subTest(argv=argv):
                code, _out, err = self.run_control(*argv)
                self.assertEqual(code, 2)
                self.assertIn("invalid_argument", err)
        self.assertEqual(self.snapshot(), before)

    def test_missing_arguments_are_a_usage_error(self):
        for argv in (["kill"], ["review-lock", "--lock-id", "1", "--cause", "c"], ["review-lock", "--lock-id", "x",
                     "--cause", "c", "--reviewer", "r"], ["unknown"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    pilot_control.main(["--db", self.path, *argv])
                self.assertEqual(caught.exception.code, 2)


class TestKillSwitch(ControlCase):
    def test_kill_appends_one_row_and_blocks_the_next_entry_until_release(self):
        self.ready()
        before = self.snapshot()
        code, out, err = self.run_control("kill", "--reason", "operator pause")
        self.assertEqual(code, 0, err)
        self.assertIn("engaged", out)
        self.assertEqual(self.changed(before, self.snapshot()), {pilot_store.KILL_SWITCH_TABLE: 1})
        switch = pilot_store.kill_switch(self.conn)
        self.assertEqual((switch.engaged, switch.reason, switch.actor), (True, "operator pause", pilot_control.ACTOR))
        self.assertEqual(switch.recorded_at, "2026-09-30T11:00:00.000000+00:00")

        (decision,) = self.open_candidates(candidate("e-1")).decisions
        self.assertEqual(decision.reason, NoTradeReason.KILL_SWITCH_ENGAGED)

        code, _out, err = self.run_control("release", "--reason", "resume")
        self.assertEqual(code, 0, err)
        self.assertFalse(pilot_store.kill_switch(self.conn).engaged)
        self.assertTrue(self.open_candidates(candidate("e-2")).decisions[0].opened)


class TestLockReview(ControlCase):
    def test_review_lock_is_the_only_thing_that_clears_a_lock_and_it_rebases(self):
        lock = self.trip_daily_lock()
        # A restart (a new connection), a new UTC day and more evaluations leave it active.
        with contextlib.closing(sqlite3.connect(self.path)) as restarted:
            pilot_store.evaluate_locks(restarted, now=T0 + timedelta(days=1))
        self.assertEqual([found.lock_id for found in pilot_store.active_locks(self.conn)], [lock.lock_id])
        (refused,) = self.open_candidates(candidate("e-2", T0 + timedelta(days=1)), at=T0 + timedelta(days=1)).decisions
        self.assertEqual(refused.reason, NoTradeReason.DAILY_LOSS_LOCK)

        at = T0 + timedelta(days=1, hours=1)
        before = self.snapshot()
        code, out, err = self.run_control(
            "review-lock", "--lock-id", str(lock.lock_id), "--cause", "gap below the stop", "--reviewer", "operator", at=at
        )
        self.assertEqual(code, 0, err)
        self.assertIn(f"Lock {lock.lock_id} cleared", out)
        self.assertEqual(
            self.changed(before, self.snapshot()), {pilot_store.REVIEW_TABLE: 1, pilot_store.MARK_TABLE: 1}
        )
        (review,) = pilot_store.read_reviews(self.conn)
        equity = pilot_store.account_state(self.conn, now=at).equity
        self.assertEqual((review.lock_id, review.reviewer, review.cause, review.rebase_equity),
                         (lock.lock_id, "operator", "gap below the stop", equity))
        self.assertEqual(pilot_store.day_start(self.conn, pilot_store.utc_day(at)), equity)
        self.assertEqual(pilot_store.active_locks(self.conn), ())
        self.assertTrue(self.open_candidates(candidate("e-3", at), at=at).decisions[0].opened)

    def test_an_unknown_or_already_reviewed_lock_is_refused(self):
        lock = self.trip_daily_lock()
        code, _out, err = self.run_control("review-lock", "--lock-id", str(lock.lock_id + 1), "--cause", "c",
                                           "--reviewer", "r")
        self.assertEqual((code, "unknown_lock" in err), (2, True))
        self.assertEqual(self.run_control("review-lock", "--lock-id", str(lock.lock_id), "--cause", "c",
                                          "--reviewer", "r")[0], 0)
        code, _out, err = self.run_control("review-lock", "--lock-id", str(lock.lock_id), "--cause", "c",
                                           "--reviewer", "r")
        self.assertEqual((code, "lock_already_reviewed" in err), (2, True))
        self.assertEqual(len(pilot_store.read_reviews(self.conn)), 1)


class TestStatus(ControlCase):
    def test_status_is_read_only_and_reports_the_account(self):
        lock = self.trip_daily_lock()
        self.run_control("kill", "--reason", "pause")
        self.assertTrue(self.open_candidates(candidate("e-2"), at=T0 + timedelta(minutes=20)).decisions[0].reason)
        before = self.snapshot()
        code, out, err = self.run_control("status", "--json", at=T0 + timedelta(minutes=30))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before)
        status = json.loads(out)
        (close,) = pilot_store.read_closes(self.conn)
        equity = D("240.00") + close.net
        self.assertEqual(status["account"]["assigned_equity"], "240.00")
        self.assertEqual(D(status["equity"]), equity)
        self.assertEqual(D(status["realized"]), close.net)
        self.assertEqual(D(status["day_start"]), D("240.00"))
        self.assertEqual(D(status["high_water"]), D("240.00"))
        self.assertEqual(status["kill_switch"]["state"], "engaged")
        self.assertEqual([found["lock_id"] for found in status["locks_active"]], [lock.lock_id])
        self.assertEqual(status["locks_active"][0]["kind"], "daily_loss")
        self.assertEqual(status["open_positions"], [])
        self.assertEqual(status["no_trade"], {"kill_switch_engaged": 1})

        code, text, _ = self.run_control("status", at=T0 + timedelta(minutes=30))
        self.assertEqual(code, 0)
        self.assertIn("Kill switch: engaged", text)
        self.assertIn(f"Lock {lock.lock_id}: daily_loss", text)

    def test_status_with_an_open_position(self):
        self.ready()
        self.open_candidates(candidate("e-1"))
        code, out, err = self.run_control("status", "--json")
        self.assertEqual(code, 0, err)
        (position,) = json.loads(out)["open_positions"]
        self.assertEqual((position["pair"], D(position["quantity"]), D(position["stop"]), D(position["target"])),
                         (PAIR, D("0.19"), D("98"), D("104")))

    def test_the_script_runs_as_a_program(self):
        self.ready()
        environment = {key: value for key, value in os.environ.items() if not key.startswith("RADAR_")}
        environment["RADAR_STATE_DIR"] = self.dir
        result = subprocess.run(
            [sys.executable, "-B", SCRIPT, "--db", self.path, "status"],
            capture_output=True, text=True, env=environment, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Pilot shadow - pretend money, no orders, LONG only", result.stdout)
        self.assertIn("Kill switch: released", result.stdout)


if __name__ == "__main__":
    unittest.main()
