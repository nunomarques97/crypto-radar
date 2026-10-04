"""The paper monitor also watches the pilot shadow position: fake Ticker, fake clock, temp SQLite.

With ``RADAR_PILOT_ENABLED`` one tick sends one Ticker request covering the union of the
game's pairs and the pilot's (the pair cap raised by one for the pilot's single position),
offers every observed quote to ``pilot_store.close_positions`` (close plus lock
evaluation; a closing quote is kept in ``pilot_exit_quotes``) and keeps the game's
failure isolation: a pilot failure is counted and never undoes the game's closes. No
socket is opened and every database is a temporary file.

Pilot position: envelope 240.00 EUR at EX-1, fee 26 bps, quote 99.9 / 100, ATR 1, pair
rules lot 2 / ordermin 0.01 / costmin 0.50 / tick 0.01 -> stop 98, target 104, quantity
0.19 (tests/test_pilot_store.py).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_DIR)
sys.path.insert(0, TESTS_DIR)

import test_paper_monitor as pm  # noqa: E402  (fake Ticker server, clock, monitor case)

from radar_v08 import config, pilot_shadow  # noqa: E402
from radar_v08.adapters import pilot_store  # noqa: E402
from radar_v08.domain.risk import Envelope, LockKind  # noqa: E402
from radar_v08.paper_monitor import TickOutcome  # noqa: E402

D = Decimal
T0 = pm.T0
BTC, ETH, SOL, ADA = pm.BTC, pm.ETH, pm.SOL, pm.ADA
DOT = "DOTEUR"
ENVELOPE = Envelope(D("240.00"), "EUR")
RULES = {"lot_decimals": 2, "ordermin": "0.01", "costmin": "0.50", "tick_size": "0.01"}


def pilot_candidate(event_id: str, pair: str) -> pilot_store.PilotCandidate:
    return pilot_store.PilotCandidate(
        event_id=event_id, run_id="run-1", asset=pair[:3], pair=pair, quote="EUR", direction="LONG",
        bid=D("99.9"), ask=D("100"), snapshot_ts=T0.isoformat(), status="online", atr=D("1"), atr_pair=pair,
        pair_entry=dict(RULES),
    )


class PilotMonitorCase(pm.MonitorCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(config, "RADAR_PILOT_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pconn = sqlite3.connect(self.path)
        self.addCleanup(self.pconn.close)

    def open_pilot(self, pair: str) -> pilot_store.StoredPosition:
        pilot_shadow.prepare(self.pconn, T0, envelope=ENVELOPE)
        report = pilot_store.open_candidates(
            self.pconn, [pilot_candidate(f"p-{pair}", pair)], envelope=ENVELOPE, fee_bps=D("26"), now=T0
        )
        self.assertTrue(report.decisions[0].opened, report.decisions[0].reason)
        (position,) = pilot_store.read_open_positions(self.pconn)
        self.assertEqual((position.stop, position.target, position.quantity), (D("98"), D("104"), D("0.19")))
        return position

    def pilot_closes(self) -> list[pilot_store.StoredClose]:
        return list(pilot_store.read_closes(self.pconn))

    def pilot_exit_quotes(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.conn.execute(f"SELECT * FROM {pilot_store.EXIT_QUOTE_TABLE}")]


class TestOneRequestForBoth(PilotMonitorCase):
    def test_the_request_is_the_union_with_the_pair_cap_raised_by_one_for_the_pilot(self):
        self.ready()
        self.open_plays(pm.candidate("e-1", BTC), pm.candidate("e-2", ETH), pm.candidate("e-3", SOL),
                        pm.candidate("e-4", ADA))
        self.open_pilot(DOT)
        server = pm.FakeTickerServer(default=pm.ticker({}))
        monitor = self.monitor(server, pm.Clock(T0 + timedelta(minutes=5)), max_pairs=lambda: 3)
        self.assertEqual(monitor.tick().requested_pairs, (BTC, ETH, SOL, DOT))
        self.assertEqual(server.requested_pairs(), [[BTC, ETH, SOL, DOT]])  # one request

    def test_a_pilot_pair_the_game_already_requests_adds_nothing(self):
        self.ready()
        self.open_plays(pm.candidate("e-1", BTC), pm.candidate("e-2", ETH))
        self.open_pilot(BTC)
        server = pm.FakeTickerServer(default=pm.ticker({}))
        monitor = self.monitor(server, pm.Clock(T0 + timedelta(minutes=5)))
        self.assertEqual(monitor.tick().requested_pairs, (BTC, ETH))

    def test_a_pilot_position_alone_is_watched_and_a_quote_inside_its_levels_closes_nothing(self):
        self.open_pilot(DOT)  # no paper table at all
        server = pm.FakeTickerServer(default=pm.ticker({DOT: pm.row("100.5", "100.6")}))
        monitor = self.monitor(server, pm.Clock(T0 + timedelta(minutes=5)))
        result = monitor.tick()
        self.assertEqual((result.outcome, result.requested_pairs, result.pilot_closed), (TickOutcome.WATCHED, (DOT,), ()))
        self.assertEqual(self.pilot_closes(), [])
        self.assertEqual(self.pilot_exit_quotes(), [])
        self.assertEqual(len(server.calls), 1)

    def test_the_pilot_switch_off_leaves_its_position_to_nobody_here(self):
        self.open_pilot(DOT)
        server = pm.FakeTickerServer(default=pm.ticker({DOT: pm.row("110", "111")}))
        monitor = self.monitor(server, pm.Clock(T0 + timedelta(minutes=5)))
        with mock.patch.object(config, "RADAR_PILOT_ENABLED", False):
            self.assertEqual(monitor.tick().outcome, TickOutcome.IDLE)
        self.assertEqual(server.calls, [])
        self.assertEqual(self.pilot_closes(), [])

    def test_the_monitor_never_creates_the_pilot_tables(self):
        self.ready()
        self.open_plays(pm.candidate("e-1", BTC))
        server = pm.FakeTickerServer(default=pm.ticker({BTC: pm.row("100", "100.5")}))
        monitor = self.monitor(server, pm.Clock(T0 + timedelta(minutes=5)))
        self.assertEqual(monitor.tick().outcome, TickOutcome.WATCHED)
        self.assertFalse(pilot_store.schema_present(self.pconn))
        self.assertEqual(monitor.stats()["pilot_failures"], 0)


class TestPilotCloses(PilotMonitorCase):
    def test_a_tick_closes_the_pilot_position_at_the_touching_quote_and_keeps_that_quote(self):
        position = self.open_pilot(DOT)
        clock = pm.Clock(T0 + timedelta(minutes=30), step=timedelta(milliseconds=400))
        server = pm.FakeTickerServer(pm.ticker({DOT: pm.row("104.5", "104.6")}))
        monitor = self.monitor(server, clock)

        result = monitor.tick()

        self.assertEqual((result.outcome, result.closed, result.pilot_closed), (TickOutcome.WATCHED, (), (position.position_id,)))
        (close,) = self.pilot_closes()
        observed = clock.reads[0]
        self.assertEqual((close.exit_reason.value, close.exit_source), ("target", "ticker"))
        self.assertEqual((close.exit_bid, close.exit_ask), (D("104.5"), D("104.6")))  # the quote, not the level
        self.assertEqual(close.exit_ts, observed)
        # 0.19 x (104.5 - 100) = 0.855 -> 0.86; fees 0.0494 -> 0.05 and 0.051623 -> 0.05.
        self.assertEqual((close.gross, close.entry_fee, close.exit_fee, close.net), (D("0.86"), D("0.05"), D("0.05"), D("0.76")))
        (quote,) = self.pilot_exit_quotes()
        self.assertEqual(close.exit_observation_id, quote["quote_id"])
        self.assertEqual((quote["pair"], quote["bid"], quote["ask"], quote["source"]), (DOT, "104.5", "104.6", "ticker"))
        self.assertEqual(monitor.stats()["pilot_closed"], 1)
        self.assertEqual(monitor.tick().outcome, TickOutcome.IDLE)  # nothing left to watch

    def test_a_stop_touch_records_the_slippage_versus_the_stop(self):
        self.open_pilot(DOT)
        monitor = self.monitor(pm.FakeTickerServer(pm.ticker({DOT: pm.row("97.9", "98.0")})), pm.Clock(T0 + timedelta(hours=1)))
        monitor.tick()
        (close,) = self.pilot_closes()
        self.assertEqual((close.exit_reason.value, close.stop, close.stop_slippage), ("stop", D("98"), D("0.1")))

    def test_game_and_pilot_on_the_same_pair_close_on_the_same_tick(self):
        self.ready()
        (play,) = self.open_plays(pm.candidate("e-btc", BTC))
        position = self.open_pilot(BTC)
        monitor = self.monitor(pm.FakeTickerServer(pm.ticker({BTC: pm.row("108", "108.5")})), pm.Clock(T0 + timedelta(hours=1)))
        result = monitor.tick()
        self.assertEqual((result.closed, result.pilot_closed), ((play.play_id,), (position.position_id,)))
        stats = monitor.stats()
        self.assertEqual((stats["closed"], stats["pilot_closed"], stats["requests"]), (1, 1, 1))

    def test_the_lock_evaluation_sees_the_tick_quote(self):
        self.open_pilot(DOT)
        # A gap to 80: the close loses 0.19 x 20 = 3.80 plus fees, more than 1 % of 240.00.
        monitor = self.monitor(pm.FakeTickerServer(pm.ticker({DOT: pm.row("80", "80.1")})), pm.Clock(T0 + timedelta(hours=1)))
        with self.assertLogs("radar_v08.paper_monitor", level="WARNING") as logs:
            monitor.tick()
        (lock,) = pilot_store.active_locks(self.pconn)
        self.assertEqual((lock.kind, lock.evaluated_on.value), (LockKind.DAILY_LOSS, "settle"))
        self.assertEqual(lock.equity, D("240.00") + self.pilot_closes()[0].net)
        self.assertTrue(any("daily_loss lock tripped" in line for line in logs.output))


class TestPilotFailureIsolation(PilotMonitorCase):
    def test_a_failing_pilot_close_is_counted_and_the_game_close_stands(self):
        self.ready()
        (play,) = self.open_plays(pm.candidate("e-btc", BTC))
        self.open_pilot(BTC)
        monitor = self.monitor(pm.FakeTickerServer(pm.ticker({BTC: pm.row("108", "108.5")})), pm.Clock(T0 + timedelta(hours=1)))
        failure = pilot_store.PilotStoreError(pilot_store.PilotStoreFailure.SQLITE_ERROR, "boom")
        with mock.patch.object(pilot_store, "close_positions", side_effect=failure), \
                self.assertLogs("radar_v08.paper_monitor", level="WARNING"):
            result = monitor.tick()
        self.assertEqual((result.outcome, result.closed, result.pilot_closed), (TickOutcome.WATCHED, (play.play_id,), ()))
        self.assertEqual(list(self.closes()), [play.play_id])
        self.assertEqual(self.pilot_closes(), [])
        stats = monitor.stats()
        self.assertEqual((stats["pilot_failures"], stats["closed"], stats["watched"]), (1, 1, 1))

    def test_an_unreadable_pilot_leaves_the_game_watch_as_before(self):
        self.ready()
        self.open_plays(pm.candidate("e-btc", BTC))
        self.open_pilot(DOT)
        server = pm.FakeTickerServer(default=pm.ticker({}))
        monitor = self.monitor(server, pm.Clock(T0 + timedelta(minutes=5)))
        with mock.patch.object(pilot_store, "read_open_positions", side_effect=sqlite3.OperationalError("disk")), \
                self.assertLogs("radar_v08.paper_monitor", level="WARNING"):
            result = monitor.tick()
        self.assertEqual((result.outcome, result.requested_pairs), (TickOutcome.WATCHED, (BTC,)))
        self.assertEqual(monitor.stats()["pilot_failures"], 1)

    def test_a_request_failure_closes_nothing_for_either(self):
        self.ready()
        self.open_plays(pm.candidate("e-btc", BTC))
        self.open_pilot(DOT)
        monitor = self.monitor(pm.FakeTickerServer(default=pm.Response({}, status_code=503)), pm.Clock(T0 + timedelta(hours=1)))
        self.assertEqual(monitor.tick().outcome, TickOutcome.REQUEST_FAILED)
        self.assertEqual((self.closes(), self.pilot_closes()), ({}, []))
        self.assertEqual(len(pilot_store.read_open_positions(self.pconn)), 1)


if __name__ == "__main__":
    unittest.main()
