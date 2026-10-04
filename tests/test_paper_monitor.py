"""Paper position monitor (radar_v08/paper_monitor.py): fake Ticker server, fake clock, temp SQLite.

One tick reads the open plays with EX-1 levels, sends at most one public Ticker request
filtered to their pairs and closes a play whose stop, target or 24 h limit the answer
touches, through ``paper_store.close_plays`` with source ``ticker``. Every failure is
caught per tick and nothing is closed on it. No socket is opened: the ``GuardedSession``
runs over a scripted fake transport. Every thread a test starts is joined.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import urlsplit

import requests

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_DIR)
sys.path.insert(0, TESTS_DIR)

import paper_legacy as legacy  # noqa: E402  (pre-EX-1 rows, test fixture)

from radar_v08 import cli, config, paper_monitor  # noqa: E402
from radar_v08.adapters import paper_store as ps  # noqa: E402
from radar_v08.http_client import GuardedSession  # noqa: E402
from radar_v08.paper_monitor import PaperMonitor, TickOutcome  # noqa: E402
from radar_v08.store import SnapshotStore  # noqa: E402

D = Decimal
T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
TERMS = ps.PlayTerms(stake=D("100"), fee_bps=D("26"), max_open=4)
BTC, ETH, SOL, ADA = "XXBTZEUR", "XETHZEUR", "SOLEUR", "ADAEUR"
TICKER_PATH = "/0/public/Ticker"
# LONG at bid 99 / ask 101 with ATR 1.5: stop 101 - 3 = 98, target 101 + 6 = 107.
# SHORT at the same touch: entry on the bid 99, stop 102, target 93.


class Clock:
    """Aware UTC clock: returns ``start``, then ``start + step``, ... (thread-safe)."""

    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        self._next = start
        self._step = step
        self._lock = threading.Lock()
        self.reads: list[datetime] = []

    def __call__(self) -> datetime:
        with self._lock:
            value = self._next
            self._next += self._step
            self.reads.append(value)
            return value


class Response:
    def __init__(self, payload=None, status_code=200, headers=None, history=(), bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = dict(headers or {})
        self.history = list(history)
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def row(bid: object, ask: object) -> dict[str, object]:
    return {"a": [ask, "1", "1.000"], "b": [bid, "1", "1.000"], "c": ["100.0", "0.1"]}


def ticker(rows: dict[str, object]) -> Response:
    return Response({"error": [], "result": rows})


class FakeTickerServer:
    """Duck-typed ``requests.Session`` under ``GuardedSession``: scripted answers, recorded calls.

    Each call takes the next scripted answer (or ``default``): a ``Response``, an exception
    to raise, or a callable of ``params`` returning a ``Response``.
    """

    def __init__(self, *script: object, default: object = None) -> None:
        self.script = list(script)
        self.default = default if default is not None else ticker({})
        self.calls: list[dict[str, object]] = []
        self.closed = False
        self._lock = threading.Lock()

    def request(self, method, url, params=None, timeout=None, allow_redirects=True):
        with self._lock:
            self.calls.append(
                {"method": method, "url": url, "params": dict(params or {}), "timeout": timeout,
                 "allow_redirects": allow_redirects}
            )
            answer = self.script.pop(0) if self.script else self.default
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(dict(params or {}))
        return answer

    def close(self):
        self.closed = True

    def requested_pairs(self) -> list[list[str]]:
        return [str(call["params"]["pair"]).split(",") for call in self.calls]


def candidate(event_id: str, pair: str, *, direction: str = "LONG", asset: str | None = None) -> ps.PaperCandidate:
    return ps.PaperCandidate(
        event_id=event_id, run_id="run-1", asset=asset or pair[:3], pair=pair, quote="EUR", direction=direction,
        bid=99.0, ask=101.0, snapshot_ts=T0.isoformat(), status="online",
        why={"setup_type": "BREAKOUT", "direction": direction}, atr=1.5, atr_pair=pair,
    )


class MonitorCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="paper-monitor-")
        self.addCleanup(self._tmp.cleanup)  # registered first: runs after every close below
        self.path = os.path.join(self._tmp.name, "radar_state.sqlite")
        SnapshotStore(self.path).close()  # every existing radar table, as in production
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    def ready(self) -> None:
        ps.ensure_schema(self.conn)
        ps.ensure_wallet(self.conn, start_balance=D("1000"), currency="EUR", now=T0)

    def open_plays(self, *candidates: ps.PaperCandidate) -> list[ps.StoredPlay]:
        report = ps.open_candidates(self.conn, list(candidates), terms=TERMS, now=T0)
        self.assertEqual(report.skipped, ())
        return list(report.opened)

    def monitor(self, server: FakeTickerServer, clock: Clock, **kwargs) -> PaperMonitor:
        kwargs.setdefault("interval_seconds", 5)
        kwargs.setdefault("busy_timeout_seconds", 0.1)
        monitor = PaperMonitor(
            self.path, session=GuardedSession(1.0, 0, 0.0, http_session=server), clock=clock, **kwargs
        )
        self.addCleanup(monitor.close)
        return monitor

    def closes(self) -> dict[int, dict[str, object]]:
        return {row["play_id"]: dict(row) for row in self.conn.execute("SELECT * FROM paper_closes ORDER BY close_id")}

    def exit_quotes(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM paper_exit_quotes ORDER BY quote_id")]

    def add_spot(self, pair: str, ts: datetime, bid: float, ask: float) -> int:
        cursor = self.conn.execute(
            "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES (?, ?, 'EUR', ?, ?, ?, 'online')",
            (pair[:3], pair, ts.isoformat(), bid, ask),
        )
        self.conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid


class TestNothingToWatch(MonitorCase):
    def test_no_paper_tables_no_open_play_or_only_legacy_plays_send_no_request(self):
        server = FakeTickerServer(default=ticker({BTC: row("1", "2")}))
        monitor = self.monitor(server, Clock(T0 + timedelta(days=3)))

        self.assertEqual(monitor.tick().outcome, TickOutcome.IDLE)  # no paper tables yet
        self.assertEqual(ps.schema_present(self.conn), False)  # and the monitor created none
        self.ready()
        self.assertEqual(monitor.tick().outcome, TickOutcome.IDLE)  # tables, no play
        legacy_id = legacy.insert_legacy_play(self.conn, "legacy-1", "BTC", "LONG", 99.0, 101.0, T0, pair=BTC)
        # A legacy play long past its due time: still not the monitor's (old rule, heartbeat only).
        self.assertEqual(monitor.tick().outcome, TickOutcome.IDLE)

        self.assertEqual(server.calls, [])
        self.assertEqual(self.closes(), {})
        self.assertEqual([p.play_id for p in ps.read_open_plays(self.conn)], [legacy_id])
        stats = monitor.stats()
        self.assertEqual((stats["ticks"], stats["idle"], stats["requests"]), (3, 3, 0))

    def test_a_missing_database_is_never_created(self):
        missing = os.path.join(self._tmp.name, "nothing-here.sqlite")
        server = FakeTickerServer()
        monitor = PaperMonitor(
            missing, session=GuardedSession(1.0, 0, 0.0, http_session=server), clock=Clock(T0), interval_seconds=5
        )
        self.addCleanup(monitor.close)
        self.assertEqual(monitor.tick().outcome, TickOutcome.DB_ERROR)
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(server.calls, [])


class TestOneFilteredRequest(MonitorCase):
    def test_one_request_per_tick_filtered_to_the_pairs_of_the_plays_with_levels(self):
        self.ready()
        self.open_plays(candidate("e-btc", BTC), candidate("e-eth", ETH, direction="SHORT"), candidate("e-sol", SOL))
        legacy.insert_legacy_play(self.conn, "legacy-ada", "ADA", "LONG", 99.0, 101.0, T0, pair=ADA)
        inside = {pair: row("100", "100.5") for pair in (BTC, ETH, SOL, ADA)}
        server = FakeTickerServer(default=ticker(inside))
        monitor = self.monitor(server, Clock(T0 + timedelta(minutes=5)))

        first = monitor.tick()
        second = monitor.tick()

        for result in (first, second):
            self.assertEqual(result.outcome, TickOutcome.WATCHED)
            self.assertEqual(result.requested_pairs, (BTC, ETH, SOL))
            self.assertEqual(result.closed, ())
        self.assertEqual(len(server.calls), 2)  # exactly one request per tick
        for call in server.calls:
            self.assertEqual(call["method"], "GET")
            self.assertEqual(urlsplit(str(call["url"])).netloc, "api.kraken.com")
            self.assertEqual(urlsplit(str(call["url"])).path, TICKER_PATH)
            self.assertEqual(call["params"], {"pair": f"{BTC},{ETH},{SOL}"})  # never the legacy ADA
            self.assertFalse(call["allow_redirects"])
        self.assertEqual(self.closes(), {})
        self.assertEqual(self.exit_quotes(), [])  # a tick that closes nothing stores nothing

    def test_at_most_max_open_pairs_oldest_first_and_one_request_for_plays_sharing_a_pair(self):
        self.ready()
        self.open_plays(
            candidate("e-1", BTC), candidate("e-2", ETH), candidate("e-3", SOL), candidate("e-4", ADA),
        )
        server = FakeTickerServer(default=ticker({}))
        monitor = self.monitor(server, Clock(T0 + timedelta(minutes=5)), max_pairs=lambda: 3)
        self.assertEqual(monitor.tick().requested_pairs, (BTC, ETH, SOL))
        self.assertEqual(server.requested_pairs(), [[BTC, ETH, SOL]])

    def test_the_default_session_is_one_short_attempt_without_backoff(self):
        session = paper_monitor.default_session()
        self.addCleanup(session.close)
        self.assertEqual(session.timeout, config.PAPER_MONITOR_HTTP_TIMEOUT_SECONDS)
        self.assertLessEqual(session.timeout, 5)
        self.assertEqual((session.max_retries, session.backoff_base), (0, 0.0))

    def test_a_failing_server_gets_one_attempt_per_tick_and_no_backoff_sleep(self):
        self.ready()
        self.open_plays(candidate("e-btc", BTC))
        server = FakeTickerServer(default=Response({}, status_code=503))
        sleeps: list[float] = []
        session = GuardedSession(
            config.PAPER_MONITOR_HTTP_TIMEOUT_SECONDS, 0, 0.0, http_session=server, sleep=sleeps.append
        )
        monitor = PaperMonitor(self.path, session=session, clock=Clock(T0 + timedelta(minutes=5)), interval_seconds=5)
        self.addCleanup(monitor.close)
        self.assertEqual(monitor.tick().outcome, TickOutcome.REQUEST_FAILED)
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(server.calls[0]["timeout"], config.PAPER_MONITOR_HTTP_TIMEOUT_SECONDS)
        self.assertEqual(sleeps, [])


class TestCloses(MonitorCase):
    def test_a_target_touch_closes_at_the_ticker_bid_at_its_receipt_time_with_lag_and_costs(self):
        self.ready()
        (play,) = self.open_plays(candidate("e-btc", BTC))
        self.assertEqual((play.stop, play.target), (D("98.0"), D("107.0")))
        clock = Clock(T0 + timedelta(minutes=30), step=timedelta(milliseconds=400))
        server = FakeTickerServer(ticker({BTC: row("107.5", "108.0")}))
        monitor = self.monitor(server, clock)

        result = monitor.tick()

        self.assertEqual(result.outcome, TickOutcome.WATCHED)
        self.assertEqual(result.closed, (play.play_id,))
        observed, written = clock.reads[0], clock.reads[1]  # receipt (after the answer), then the write
        close = self.closes()[play.play_id]
        self.assertEqual(close["exit_reason"], "target")
        self.assertEqual(close["exit_source"], "ticker")
        self.assertEqual((D(close["exit_bid"]), D(close["exit_ask"])), (D("107.5"), D("108.0")))  # not the level
        self.assertEqual(datetime.fromisoformat(close["exit_ts"]), observed)
        self.assertEqual(datetime.fromisoformat(close["due_at"]), observed)
        self.assertEqual(close["delay_seconds"], 0.0)
        self.assertEqual(datetime.fromisoformat(close["closed_at"]), written)
        self.assertAlmostEqual(close["record_lag_seconds"], 0.4)
        self.assertGreater(close["fees_cents"], 0)
        self.assertGreater(close["spread_cost_cents"], 0)
        self.assertEqual(close["net_cents"], close["gross_mid_cents"] - close["spread_cost_cents"] - close["fees_cents"])
        self.assertEqual(close["outcome"], "WIN")
        (quote,) = self.exit_quotes()
        self.assertEqual(close["exit_snapshot_id"], quote["quote_id"])
        self.assertEqual(
            (quote["play_id"], quote["pair"], quote["bid"], quote["ask"], quote["source"]),
            (play.play_id, BTC, "107.5", "108.0", "ticker"),
        )
        self.assertEqual(datetime.fromisoformat(quote["observed_at"]), observed)
        self.assertEqual(monitor.stats()["closed"], 1)

        # Closed once: a later tick sends no request for it and writes nothing.
        self.assertEqual(monitor.tick().outcome, TickOutcome.IDLE)
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(len(self.closes()), 1)

    def test_long_stop_on_the_bid_short_stop_on_the_ask(self):
        self.ready()
        long_play, short_play = self.open_plays(
            candidate("e-btc", BTC), candidate("e-eth", ETH, direction="SHORT")
        )
        self.assertEqual((short_play.stop, short_play.target), (D("102.0"), D("93.0")))
        server = FakeTickerServer(ticker({BTC: row("97.9", "98.4"), ETH: row("101.8", "102.2")}))
        result = self.monitor(server, Clock(T0 + timedelta(hours=1))).tick()
        self.assertEqual(sorted(result.closed), sorted([long_play.play_id, short_play.play_id]))
        closes = self.closes()
        self.assertEqual(closes[long_play.play_id]["exit_reason"], "stop")
        self.assertEqual(D(closes[long_play.play_id]["exit_bid"]), D("97.9"))
        self.assertEqual(closes[short_play.play_id]["exit_reason"], "stop")
        self.assertEqual(D(closes[short_play.play_id]["exit_ask"]), D("102.2"))
        self.assertEqual({row["outcome"] for row in closes.values()}, {"LOSS"})

    def test_a_quote_inside_the_levels_closes_nothing_before_24_hours_and_by_time_after(self):
        self.ready()
        (play,) = self.open_plays(candidate("e-btc", BTC))
        inside = ticker({BTC: row("100", "100.5")})
        server = FakeTickerServer(inside, inside)
        early = self.monitor(server, Clock(play.due_at - timedelta(seconds=10))).tick()
        self.assertEqual((early.outcome, early.quoted_pairs, early.closed), (TickOutcome.WATCHED, (BTC,), ()))
        late_clock = Clock(play.due_at + timedelta(seconds=7))
        late = self.monitor(server, late_clock).tick()
        self.assertEqual(late.closed, (play.play_id,))
        close = self.closes()[play.play_id]
        self.assertEqual(close["exit_reason"], "time")
        self.assertEqual(close["exit_source"], "ticker")
        self.assertEqual(datetime.fromisoformat(close["due_at"]), play.due_at)
        self.assertEqual(close["delay_seconds"], 7.0)
        self.assertEqual(D(close["exit_bid"]), D("100"))

    def test_an_earlier_recorded_snapshot_that_touches_wins_over_the_tick(self):
        self.ready()
        (play,) = self.open_plays(candidate("e-btc", BTC))
        snapshot_id = self.add_spot(BTC, T0 + timedelta(minutes=10), 97.5, 98.0)  # stop, 10 min in
        server = FakeTickerServer(ticker({BTC: row("108", "108.5")}))  # target, 30 min in
        result = self.monitor(server, Clock(T0 + timedelta(minutes=30))).tick()
        self.assertEqual(result.closed, (play.play_id,))
        close = self.closes()[play.play_id]
        self.assertEqual((close["exit_reason"], close["exit_source"]), ("stop", "spot_snapshot"))
        self.assertEqual(close["exit_snapshot_id"], snapshot_id)
        self.assertEqual(self.exit_quotes(), [])

    def test_the_answer_key_must_be_the_play_pair_exactly(self):
        self.ready()
        (play,) = self.open_plays(candidate("e-btc", BTC))
        touching = row("97", "97.5")
        server = FakeTickerServer(
            ticker({"XBTEUR": touching, BTC.lower(): touching, f"{BTC} ": touching, "XXBTZUSD": touching})
        )
        result = self.monitor(server, Clock(T0 + timedelta(minutes=30))).tick()
        self.assertEqual((result.outcome, result.quoted_pairs, result.closed), (TickOutcome.WATCHED, (), ()))
        self.assertEqual(self.closes(), {})
        self.assertIn(play.play_id, [p.play_id for p in ps.read_open_plays(self.conn)])

    def test_missing_malformed_crossed_or_non_positive_quotes_are_ignored_never_zero(self):
        self.ready()
        (play,) = self.open_plays(candidate("e-btc", BTC))
        # Read as zero, each of these bids would be through the LONG stop (98).
        bad_rows = [
            None, "97", [], {}, {"a": ["97.5"]}, {"b": ["97"]}, row(None, "97.5"), row("97", None),
            row("0", "97.5"), row("0.0", "0.0"), row("-97", "97.5"), row("97", "-1"), row("", "97.5"),
            row("NaN", "97.5"), row("97", "Infinity"), row("9.7e1", "97.5"), row(" 97", "97.5"),
            row(97.0, 97.5), row(True, "97.5"), row(["97"], "97.5"), row("97.6", "97.5"),  # crossed
            {"a": "97.5", "b": "97"}, {"a": [], "b": []},
        ]
        server = FakeTickerServer(*(ticker({BTC: bad}) for bad in bad_rows))
        monitor = self.monitor(server, Clock(T0 + timedelta(minutes=30)))
        for bad in bad_rows:
            with self.subTest(row=bad):
                result = monitor.tick()
                self.assertEqual((result.outcome, result.quoted_pairs, result.closed), (TickOutcome.WATCHED, (), ()))
        self.assertEqual(self.closes(), {})
        self.assertEqual(self.exit_quotes(), [])
        self.assertEqual(len(server.calls), len(bad_rows))
        self.assertEqual(paper_monitor.ticker_quote(row("97", "97.5")), ps.paper.Quote(D("97"), D("97.5")))

    def test_an_unexpected_pair_key_on_a_play_is_never_put_in_the_request(self):
        self.ready()
        good, bad = self.open_plays(candidate("e-btc", BTC), candidate("e-evil", "XETHZEUR,XXBTZUSD", asset="EVL"))
        server = FakeTickerServer(default=ticker({BTC: row("100", "100.5")}))
        monitor = self.monitor(server, Clock(T0 + timedelta(minutes=30)))
        with self.assertLogs("radar_v08.paper_monitor", "WARNING"):
            result = monitor.tick()
        with self.assertNoLogs("radar_v08.paper_monitor", "WARNING"):
            monitor.tick()  # one warning per play, not one per tick
        self.assertEqual(result.requested_pairs, (BTC,))
        self.assertEqual(server.calls[0]["params"], {"pair": BTC})
        self.assertEqual(result.quoted_pairs, (BTC,))
        self.assertNotIn(bad.play_id, result.closed)
        self.assertEqual(good.pair, BTC)
        self.assertEqual([call["params"] for call in server.calls], [{"pair": BTC}, {"pair": BTC}])


class TestFailuresCloseNothing(MonitorCase):
    def setUp(self) -> None:
        super().setUp()
        self.ready()
        (self.play,) = self.open_plays(candidate("e-btc", BTC))
        self.touching = ticker({BTC: row("97", "97.5")})  # through the stop

    def assert_failed_then_recovers(self, answer: object, outcome: TickOutcome) -> None:
        server = FakeTickerServer(answer, self.touching)
        monitor = self.monitor(server, Clock(T0 + timedelta(minutes=30)))
        with self.assertLogs("radar_v08.paper_monitor", "WARNING") as logs:
            failed = monitor.tick()
        self.assertEqual(failed.outcome, outcome)
        self.assertEqual(failed.closed, ())
        self.assertIn(outcome.value, "\n".join(logs.output))
        self.assertEqual(self.closes(), {})
        self.assertEqual(self.exit_quotes(), [])
        self.assertEqual(monitor.stats()[outcome.value], 1)
        # The next tick works again: the failure closed nothing and stopped nothing.
        self.assertEqual(monitor.tick().closed, (self.play.play_id,))
        self.assertEqual(self.closes()[self.play.play_id]["exit_reason"], "stop")

    def test_network_error(self):
        self.assert_failed_then_recovers(requests.ConnectionError("connection reset"), TickOutcome.REQUEST_FAILED)

    def test_timeout(self):
        self.assert_failed_then_recovers(requests.Timeout("read timed out"), TickOutcome.REQUEST_FAILED)

    def test_http_server_error(self):
        self.assert_failed_then_recovers(Response({}, status_code=502), TickOutcome.REQUEST_FAILED)

    def test_http_rate_limit(self):
        self.assert_failed_then_recovers(Response({}, status_code=429), TickOutcome.REQUEST_FAILED)

    def test_http_client_error(self):
        self.assert_failed_then_recovers(Response({}, status_code=404), TickOutcome.REQUEST_FAILED)

    def test_body_that_is_not_json(self):
        self.assert_failed_then_recovers(Response(bad_json=True), TickOutcome.MALFORMED)

    def test_json_that_is_not_an_object(self):
        self.assert_failed_then_recovers(Response(["not", "an", "object"]), TickOutcome.MALFORMED)

    def test_json_without_result(self):
        self.assert_failed_then_recovers(Response({"error": []}), TickOutcome.MALFORMED)

    def test_result_that_is_not_an_object(self):
        self.assert_failed_then_recovers(Response({"error": [], "result": [1, 2]}), TickOutcome.MALFORMED)

    def test_kraken_error_list(self):
        self.assert_failed_then_recovers(Response({"error": ["EQuery:Unknown asset pair"]}), TickOutcome.MALFORMED)

    def test_redirect_outside_the_allowlist_is_a_security_refusal(self):
        answer = Response({}, status_code=302, headers={"Location": "https://evil.example/0/public/Ticker"})
        self.assert_failed_then_recovers(answer, TickOutcome.SECURITY_REFUSED)

    def test_a_response_that_came_through_a_redirect_chain_is_a_security_refusal(self):
        answer = Response({"error": [], "result": {BTC: row("97", "97.5")}}, history=[object()])
        self.assert_failed_then_recovers(answer, TickOutcome.SECURITY_REFUSED)

    def test_a_request_outside_the_allowlist_is_refused_before_any_transport_call(self):
        server = FakeTickerServer(default=self.touching)
        monitor = self.monitor(server, Clock(T0 + timedelta(minutes=30)))
        with mock.patch.object(config, "SPOT_URL", "https://evil.example/0/public"), \
                self.assertLogs("radar_v08.paper_monitor", "WARNING"):
            result = monitor.tick()
        self.assertEqual(result.outcome, TickOutcome.SECURITY_REFUSED)
        self.assertEqual(server.calls, [])
        self.assertEqual(self.closes(), {})

    def test_a_busy_database_skips_the_tick_quickly_and_closes_nothing(self):
        server = FakeTickerServer(self.touching, self.touching)
        monitor = self.monitor(server, Clock(T0 + timedelta(minutes=30)), busy_timeout_seconds=0.2)
        blocker = sqlite3.connect(self.path, timeout=0)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")  # the heartbeat holding the write lock
        try:
            started = time.monotonic()
            with self.assertLogs("radar_v08.paper_monitor", "WARNING"):
                result = monitor.tick()
            elapsed = time.monotonic() - started
        finally:
            blocker.rollback()
        self.assertEqual(result.outcome, TickOutcome.DB_BUSY)
        self.assertLess(elapsed, 3.0)
        self.assertEqual(self.closes(), {})
        self.assertEqual(self.exit_quotes(), [])
        self.assertEqual(monitor.tick().closed, (self.play.play_id,))  # lock released: the next tick closes

    def test_a_store_failure_is_counted_and_the_connection_is_reopened(self):
        server = FakeTickerServer(self.touching, self.touching)
        monitor = self.monitor(server, Clock(T0 + timedelta(minutes=30)))
        broken = ps.PaperStoreError(ps.PaperStoreFailure.SQLITE_ERROR, "disk I/O error")
        with mock.patch.object(ps, "close_plays", side_effect=broken), \
                self.assertLogs("radar_v08.paper_monitor", "WARNING"):
            self.assertEqual(monitor.tick().outcome, TickOutcome.DB_ERROR)
        self.assertEqual(self.closes(), {})
        self.assertEqual(monitor.tick().closed, (self.play.play_id,))

    def test_an_unexpected_error_is_caught(self):
        server = FakeTickerServer(self.touching)
        monitor = self.monitor(server, Clock(T0 + timedelta(minutes=30)))
        with mock.patch.object(ps, "close_plays", side_effect=RuntimeError("boom")), \
                self.assertLogs("radar_v08.paper_monitor", "WARNING"):
            self.assertEqual(monitor.tick().outcome, TickOutcome.FAILED)
        self.assertEqual(self.closes(), {})

    def test_a_failure_streak_logs_one_warning_then_recovery_once(self):
        outage = requests.ConnectionError("down")
        server = FakeTickerServer(outage, outage, outage, ticker({BTC: row("100", "100.5")}))
        monitor = self.monitor(server, Clock(T0 + timedelta(minutes=30)))
        with self.assertLogs("radar_v08.paper_monitor", "DEBUG") as logs:
            for _ in range(4):
                monitor.tick()
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(sum("recovered" in line for line in logs.output), 1)
        self.assertEqual(monitor.stats()["request_failed"], 3)


class TestThread(MonitorCase):
    def start(self, server: FakeTickerServer, clock: Clock, interval: float = 0.01) -> paper_monitor.MonitorHandle:
        handle = paper_monitor.start_monitor(
            self.path, session=GuardedSession(1.0, 0, 0.0, http_session=server), clock=clock, interval_seconds=interval
        )
        assert handle is not None
        self.addCleanup(handle.stop, 5.0)
        return handle

    def wait_for(self, condition, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not reached in time")
            time.sleep(0.01)

    def test_the_thread_keeps_running_through_failures_closes_then_stops_cleanly(self):
        self.ready()
        (play,) = self.open_plays(candidate("e-btc", BTC))
        failures = [requests.ConnectionError("down"), Response({}, status_code=500), Response(bad_json=True)]
        server = FakeTickerServer(*failures, default=ticker({BTC: row("108", "108.5")}))
        with self.assertLogs("radar_v08.paper_monitor", "INFO"):
            handle = self.start(server, Clock(T0 + timedelta(minutes=30), step=timedelta(milliseconds=5)))
            self.assertTrue(handle.thread.daemon)
            self.assertEqual(handle.thread.name, "paper-monitor")
            self.wait_for(lambda: handle.monitor.stats()["closed"] == 1)
            self.assertTrue(handle.stop(5.0))
        self.assertFalse(handle.thread.is_alive())
        self.assertTrue(server.closed)  # the session is closed with the thread
        stats = handle.monitor.stats()
        self.assertEqual((stats["request_failed"], stats["malformed"]), (2, 1))
        self.assertEqual(self.closes()[play.play_id]["exit_reason"], "target")
        calls = len(server.calls)
        time.sleep(0.05)
        self.assertEqual(len(server.calls), calls)  # nothing is requested after stop

    def test_stop_interrupts_the_wait_between_ticks(self):
        self.ready()
        server = FakeTickerServer()
        handle = self.start(server, Clock(T0), interval=60)
        self.wait_for(lambda: handle.monitor.stats()["ticks"] >= 1)
        started = time.monotonic()
        self.assertTrue(handle.stop(5.0))
        self.assertLess(time.monotonic() - started, 5.0)

    def test_either_switch_off_starts_no_thread_and_requests_nothing(self):
        self.ready()
        self.open_plays(candidate("e-btc", BTC))
        for paper_on, monitor_on in ((False, True), (True, False), (False, False)):
            with self.subTest(paper=paper_on, monitor=monitor_on):
                server = FakeTickerServer(default=ticker({BTC: row("97", "97.5")}))
                before = {thread.ident for thread in threading.enumerate()}
                with mock.patch.object(config, "RADAR_PAPER_ENABLED", paper_on), \
                        mock.patch.object(config, "RADAR_PAPER_MONITOR_ENABLED", monitor_on):
                    handle = paper_monitor.start_monitor(
                        self.path, session=GuardedSession(1.0, 0, 0.0, http_session=server), clock=Clock(T0),
                        interval_seconds=0.01,
                    )
                self.assertIsNone(handle)
                self.assertEqual({thread.ident for thread in threading.enumerate()} - before, set())
                time.sleep(0.03)
                self.assertEqual(server.calls, [])
        self.assertEqual(self.closes(), {})


class TestLoopWiring(unittest.TestCase):
    def setUp(self):
        # Loop mode also starts the trend paper catch-up (Binance public data); stubbed so these
        # tests open no network connection (it is covered by tests/test_trend_hook.py).
        hook = mock.patch.object(cli, "_start_trend_paper_catch_up")
        hook.start()
        self.addCleanup(hook.stop)

    def test_loop_mode_starts_the_monitor_and_stops_it_when_the_loop_ends(self):
        handle = mock.Mock()
        handle.stop.return_value = True
        with mock.patch.object(cli.paper_monitor, "start_monitor", return_value=handle) as start, \
                mock.patch.object(cli, "run_and_write", return_value={"funnel": {}}), \
                mock.patch.object(cli, "_run_bridge_and_render"), \
                mock.patch.object(cli, "SnapshotStore"), \
                mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.run_mode("loop"), 0)
        start.assert_called_once_with(config.SQLITE_PATH)
        handle.stop.assert_called_once_with()

    def test_the_monitor_is_stopped_when_a_cycle_raises(self):
        handle = mock.Mock()
        handle.stop.return_value = True
        with mock.patch.object(cli.paper_monitor, "start_monitor", return_value=handle), \
                mock.patch.object(cli, "run_and_write", side_effect=RuntimeError("cycle failed")):
            with self.assertRaises(RuntimeError):
                cli.run_mode("loop")
        handle.stop.assert_called_once_with()

    def test_loop_mode_with_the_monitor_off_starts_no_thread(self):
        with mock.patch.object(config, "RADAR_PAPER_MONITOR_ENABLED", False), \
                mock.patch.object(cli, "run_and_write", return_value={"funnel": {}}), \
                mock.patch.object(cli, "_run_bridge_and_render"), \
                mock.patch.object(cli, "SnapshotStore"), \
                mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt), \
                mock.patch.object(paper_monitor, "PaperMonitor") as monitor:
            self.assertEqual(cli.run_mode("loop"), 0)
        monitor.assert_not_called()
        self.assertNotIn("paper-monitor", [thread.name for thread in threading.enumerate()])


class TestSwitches(unittest.TestCase):
    def test_interval_parse(self):
        self.assertEqual(config.parse_paper_monitor_seconds(None), 5)
        self.assertEqual(config.parse_paper_monitor_seconds("2"), 2)
        self.assertEqual(config.parse_paper_monitor_seconds(" 30 "), 30)
        for raw in ("1", "0", "-5", "2.5", "5s", "", " ", "abc", "٣"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                config.parse_paper_monitor_seconds(raw)

    def load(self, **env: str) -> str:
        with tempfile.TemporaryDirectory(prefix="paper-monitor-config-") as state_dir:
            environment = {k: v for k, v in os.environ.items() if not k.startswith("RADAR_")}
            environment.update(RADAR_STATE_DIR=state_dir, PYTHONDONTWRITEBYTECODE="1", **env)
            result = subprocess.run(
                [sys.executable, "-B", "-c",
                 "from radar_v08 import config as c; print(c.RADAR_PAPER_MONITOR_ENABLED, c.PAPER_MONITOR_SECONDS)"],
                cwd=REPO_DIR, env=environment, capture_output=True, text=True, timeout=60,
            )
        return result.stdout.strip() if result.returncode == 0 else f"exit {result.returncode}"

    def test_environment_defaults_and_values(self):
        self.assertEqual(self.load(), "True 5")
        self.assertEqual(self.load(RADAR_PAPER_MONITOR_ENABLED="0"), "False 5")
        self.assertEqual(self.load(RADAR_PAPER_MONITOR_ENABLED="false"), "False 5")
        self.assertEqual(self.load(RADAR_PAPER_MONITOR_SECONDS="12"), "True 12")
        self.assertEqual(self.load(RADAR_PAPER_MONITOR_SECONDS="1"), "exit 1")  # refuses to load


if __name__ == "__main__":
    unittest.main()
