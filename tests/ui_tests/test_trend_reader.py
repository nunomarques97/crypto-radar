"""Read-only trend paper reader and guarded catch-up (ui/trend_reader.py) on temporary state dirs.

Ledgers are built through the real catch-up, domain and store (radar_v08/trend_paper_hook.py,
radar_v08/domain/trend_paper.py, radar_v08/adapters/trend_paper_store.py) with the offline market
of tests/trend_paper_fakes.py: the vendored daily history extended with deterministic synthetic
candles. No network (socket connections are refused for the whole module); the real ledger and
radar_state.sqlite are never touched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trend_paper_fakes as F  # noqa: E402
from trend_paper_fakes import FakeFetcher, at, live_market  # noqa: E402

from radar_v08 import config, trend_paper_hook  # noqa: E402
from radar_v08.adapters import trend_paper_store as store  # noqa: E402
from radar_v08.adapters.binance_public_klines import (  # noqa: E402
    KlinesError,
    KlinesErrorCode,
)
from radar_v08.domain import trend_paper as P  # noqa: E402
from ui import trend_reader  # noqa: E402
from ui.trend_reader import TrendReader  # noqa: E402

BEFORE = at(date(2026, 10, 3), 21)
DAY2 = at(date(2026, 10, 5))  # books 2026-10-04 and 2026-10-05
DAY3 = at(date(2026, 10, 6))
_patches: list[Any] = []


def _refuse_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("network access attempted in an offline test")


def _refuse_sqlite(*args: object, **kwargs: object) -> None:
    raise AssertionError("the trend reader must never open a SQLite database")


def setUpModule() -> None:
    for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
        patcher = mock.patch(target, _refuse_network)
        patcher.start()
        _patches.append(patcher)


def tearDownModule() -> None:
    while _patches:
        _patches.pop().stop()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cents(value: float) -> str:
    return str(Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


class TrendCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="trend-reader-")
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.path = store.ledger_path(self.state_dir)
        self.fetchers: list[FakeFetcher] = []
        sqlite_guard = mock.patch.object(sqlite3, "connect", _refuse_sqlite)
        sqlite_guard.start()
        self.addCleanup(sqlite_guard.stop)

    def fetcher(self, now: datetime, **kwargs: Any) -> FakeFetcher:
        fake = FakeFetcher(live_market(now), **kwargs)
        self.fetchers.append(fake)
        return fake

    def book(self, now: datetime) -> trend_paper_hook.CatchUpResult:
        """Write the ledger up to ``now`` through the real catch-up, store and domain."""
        return trend_paper_hook.catch_up(self.state_dir, self.fetcher(now), clock=lambda: now)

    def reader(self, now: datetime, **kwargs: Any) -> TrendReader:
        kwargs.setdefault("fetcher_factory", lambda: self.fetcher(now))
        return TrendReader(self.state_dir, clock=lambda: now, **kwargs)

    def row(self, payload: dict[str, Any], quote: str, label: str, fee_pct: str) -> dict[str, Any]:
        group = next(g for g in payload["quotes"] if g["quote"] == quote)
        return next(r for r in group["rows"] if r["label"] == label and r["fee_pct"] == fee_pct)


class TestReadOnly(TrendCase):
    def test_missing_ledger_is_not_created(self) -> None:
        payload = self.reader(DAY2).read()
        self.assertEqual(payload["state"], trend_reader.STATE_EMPTY)
        self.assertFalse((self.state_dir / store.LEDGER_DIR_NAME).exists())
        self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_read_takes_no_lock_and_changes_nothing(self) -> None:
        self.book(DAY2)
        before = {p.name: sha(p) for p in self.path.parent.iterdir()}
        with mock.patch.object(store.LedgerWriter, "__enter__", side_effect=AssertionError("lock taken")):
            for _ in range(3):
                self.assertEqual(self.reader(DAY3).read()["state"], trend_reader.STATE_OK)
        self.assertEqual({p.name: sha(p) for p in self.path.parent.iterdir()}, before)

    def test_reads_the_config_state_dir_and_nothing_else(self) -> None:
        with mock.patch.object(config, "STATE_DIR", str(self.state_dir)):
            reader = trend_reader.from_config()
        self.assertEqual(reader.path, self.state_dir / "trend_paper" / "ledger.jsonl")

    def test_poll_never_fetches(self) -> None:
        self.book(DAY2)
        factory = mock.Mock(side_effect=AssertionError("fetch on a poll"))
        reader = TrendReader(self.state_dir, fetcher_factory=factory, clock=lambda: DAY3)
        self.assertEqual(reader.read()["state"], trend_reader.STATE_OK)
        factory.assert_not_called()


class TestEmpty(TrendCase):
    def assert_empty(self, payload: dict[str, Any]) -> None:
        self.assertEqual(payload["state"], "empty")
        self.assertIn("2026-10-04 00:00 UTC", payload["reason"])
        self.assertEqual(payload["quotes"], [])
        self.assertEqual(payload["days_booked"], 0)
        self.assertIsNone(payload["last_day"])
        self.assertIsNone(payload["last_record_ts"])
        self.assertIsNone(payload["error"])
        self.assertEqual((payload["days_skipped"], payload["skipped_days"]), (0, []))
        self.assertEqual(payload["honesty_label"], "Paper only — research, not qualified, no real orders")
        self.assertIn("pre-tax", payload["pre_tax_note"])
        self.assertEqual(payload["paper_start"], "2026-10-04")

    def test_before_the_first_paper_day(self) -> None:
        result = self.book(BEFORE)
        self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.BEFORE_START)
        self.assert_empty(self.reader(BEFORE).read())

    def test_empty_ledger_file(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"")
        self.assert_empty(self.reader(DAY2).read())

    def test_payload_is_json(self) -> None:
        json.dumps(self.reader(BEFORE).read(), allow_nan=False)


class TestPopulated(TrendCase):
    def setUp(self) -> None:
        super().setUp()
        self.result = self.book(DAY2)
        self.payload = self.reader(DAY2).read()
        self.ledger = store.read_ledger(self.path)
        self.summary = P.summarize(self.ledger.records)

    def test_two_days_header(self) -> None:
        p = self.payload
        self.assertEqual(self.result.booked, (date(2026, 10, 4), date(2026, 10, 5)))
        self.assertEqual(p["state"], "ok")
        self.assertEqual(p["honesty_label"], trend_reader.HONESTY_LABEL)
        self.assertEqual((p["days_booked"], p["first_day"], p["last_day"]), (2, "2026-10-04", "2026-10-05"))
        self.assertEqual(p["last_record_ts"], "2026-10-05T08:00:00+00:00")
        self.assertEqual(p["last_record_ts"], self.ledger.records[-1]["ts"])
        self.assertEqual(p["capital"], "7000.00")
        self.assertIsNone(p["error"])
        json.dumps(p, allow_nan=False)

    def test_order_quote_rule_fee(self) -> None:
        self.assertEqual([g["quote"] for g in self.payload["quotes"]], ["EUR", "USDT"])
        for group in self.payload["quotes"]:
            self.assertEqual(group["currency"], group["quote"])
            self.assertEqual(
                [(r["label"], r["fee_pct"]) for r in group["rows"]],
                [(label, fee) for label in ("ENS", "ENS_VT", "btc_trend5", "btc_trend5_vt") for fee in ("0.1", "0.4")],
            )
            self.assertEqual(
                [r["rule"] for r in group["rows"]][::2], ["ENS", "ENS_VT", "BTC_TREND5", "BTC_TREND5_VT"]
            )
            for r in group["rows"]:
                self.assertEqual(r["currency"], group["quote"])
                self.assertEqual((r["days"], r["last_date"]), (2, "2026-10-05"))

    def test_figures_come_from_summarize(self) -> None:
        for group in self.payload["quotes"]:
            for r in group["rows"]:
                s = self.summary[r["book"]]
                c = self.summary[r["comparator"]["book"]]
                with self.subTest(book=r["book"]):
                    self.assertEqual(r["equity"], cents(s.equity))
                    self.assertEqual(r["fees"], cents(s.fees))
                    self.assertEqual(r["trades"], s.trades)
                    self.assertEqual(r["return_pct"], cents(s.ret * 100))
                    self.assertEqual(r["comparator"]["equity"], cents(c.equity))

    def test_a_loss_is_shown_signed(self) -> None:
        r = self.row(self.payload, "EUR", "ENS", "0.4")
        self.assertEqual((r["equity"], r["return_pct"], r["max_drawdown_pct"]), ("6846.00", "-2.20", "-2.20"))
        self.assertEqual((r["trades"], r["fees"]), (2, "28.00"))

    def test_comparator_difference(self) -> None:
        r = self.row(self.payload, "EUR", "btc_trend5_vt", "0.1")
        self.assertEqual((r["equity"], r["return_pct"], r["max_drawdown_pct"]), ("6905.22", "-1.35", "-1.35"))
        self.assertEqual(r["comparator"], {
            "rule": "BH_BTC", "book": "EUR|BH_BTC|0.001", "equity": "6867.13", "return_pct": "-1.90",
        })
        self.assertEqual(r["vs_buy_hold_pp"], "0.54")
        s, c = self.summary["EUR|BTC_TREND5_VT|0.001"], self.summary["EUR|BH_BTC|0.001"]
        exact = (Decimal(repr(s.ret)) - Decimal(repr(c.ret))) * 100
        self.assertEqual(r["vs_buy_hold_pp"], str(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)))
        ens = self.row(self.payload, "USDT", "ENS", "0.1")
        self.assertEqual((ens["comparator"]["rule"], ens["vs_buy_hold_pp"]), ("BH_5050", "0.00"))

    def test_exposure_target_and_signal_from_the_last_record(self) -> None:
        r = self.row(self.payload, "EUR", "btc_trend5_vt", "0.1")
        last = next(x for x in reversed(self.ledger.records) if x["book"] == "EUR|BTC_TREND5_VT|0.001")
        self.assertEqual(last["date"], "2026-10-05")
        [btc] = r["assets"]
        self.assertEqual((btc["asset"], btc["exposure_pct"], btc["target_pct"]), ("BTC", "88.41", "88.39"))
        self.assertEqual(btc["exposure_pct"], cents(last["assets"]["BTC"]["held_after"] * 100))
        self.assertEqual(btc["signal_close_date"], "2026-10-04")
        self.assertEqual(
            [s["name"] for s in btc["signal"]], sorted(last["assets"]["BTC"]["signal"])
        )
        self.assertEqual(
            {s["name"]: s["value"] for s in btc["signal"]}["BTC_TREND5_VT"],
            str(Decimal(repr(last["assets"]["BTC"]["signal"]["BTC_TREND5_VT"])).quantize(Decimal("0.0001"))),
        )
        ens = self.row(self.payload, "USDT", "ENS", "0.1")
        self.assertEqual([a["asset"] for a in ens["assets"]], ["BTC", "ETH"])
        self.assertEqual({s["name"] for s in ens["assets"][0]["signal"]}, {"ENS", "ENS_VT", "vol30"})


class TestFormatting(unittest.TestCase):
    def test_money_rounds_half_even_to_cents(self) -> None:
        cases = [
            (7000.0, "7000.00"), (6905.224, "6905.22"), (0.015, "0.02"), (0.025, "0.02"),
            (0.005, "0.00"), (-0.004, "0.00"), (-0.35, "-0.35"), (1234567.891, "1234567.89"), (3, "3.00"),
        ]
        for value, text in cases:
            with self.subTest(value=value):
                self.assertEqual(trend_reader.money(value), text)

    def test_not_recorded_is_none_never_zero(self) -> None:
        for fn in (trend_reader.money, trend_reader.percent, trend_reader.signal_value):
            for value in (None, True, "12.5", float("nan"), float("inf")):
                with self.subTest(fn=fn.__name__, value=value):
                    self.assertIsNone(fn(value))
        self.assertIsNone(trend_reader.points(0.01, None))

    def test_percent_and_points(self) -> None:
        self.assertEqual(trend_reader.percent(-0.0123456), "-1.23")
        self.assertEqual(trend_reader.percent(0.0), "0.00")
        self.assertEqual(trend_reader.percent(-0.00001), "0.00")
        self.assertEqual(trend_reader.percent(0.88405), "88.40")
        self.assertEqual(trend_reader.points(0.01, 0.0145), "-0.45")
        self.assertEqual(trend_reader.points(-0.0135, -0.019), "0.55")

    def test_fee_and_signal(self) -> None:
        self.assertEqual([trend_reader.fee_percent(f) for f in P.FEES], ["0.1", "0.4"])
        self.assertEqual(trend_reader.signal_value(0.70349), "0.7035")
        self.assertEqual(trend_reader.signal_value(1.0), "1.0000")

    def test_short_detail(self) -> None:
        self.assertEqual(trend_reader.short("a\n  b\tc"), "a b c")
        long = trend_reader.short("x" * 500)
        self.assertEqual(len(long), trend_reader.DETAIL_MAX)
        self.assertTrue(long.endswith("…"))


class TestRefused(TrendCase):
    def setUp(self) -> None:
        super().setUp()
        self.book(DAY2)

    def assert_refused(self, code: str) -> dict[str, Any]:
        before = sha(self.path)
        payload = self.reader(DAY3).read()
        self.assertEqual(sha(self.path), before)
        self.assertEqual(payload["state"], "refused")
        self.assertEqual(payload["error"]["code"], code)
        self.assertLessEqual(len(payload["error"]["detail"]), trend_reader.DETAIL_MAX)
        self.assertEqual(payload["quotes"], [])
        self.assertIsNone(payload["days_booked"])
        self.assertEqual((payload["days_skipped"], payload["skipped_days"]), (0, []))
        self.assertIsNone(payload["last_day"])
        self.assertEqual(payload["honesty_label"], trend_reader.HONESTY_LABEL)
        json.dumps(payload, allow_nan=False)
        return payload

    def test_torn_ledger(self) -> None:
        data = self.path.read_bytes()
        self.path.write_bytes(data[:-10])
        self.assert_refused("LEDGER_TORN")

    def test_edited_ledger(self) -> None:
        lines = self.path.read_bytes().split(b"\n")
        lines[3] = lines[3].replace(b'"equity":', b'"equity":1', 1)
        self.path.write_bytes(b"\n".join(lines))
        self.assert_refused("LEDGER_EDITED")

    def test_invalid_ledger(self) -> None:
        lines = self.path.read_bytes().split(b"\n")
        self.path.write_bytes(b"\n".join(lines[:5]) + b"\n")  # chain intact, day incomplete
        self.assert_refused("LEDGER_INVALID")

    def test_unreadable_ledger_detail_has_no_path(self) -> None:
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError(13, "denied", str(self.path))):
            payload = self.reader(DAY3).read()
        self.assertEqual((payload["state"], payload["error"]["code"]), ("refused", "UNREADABLE"))
        self.assertNotIn(str(self.state_dir), json.dumps(payload))
        self.assertNotIn("ledger.jsonl", payload["error"]["detail"])


class TestSkippedDays(TrendCase):
    """The fixed payload contract for skipped days and the transient
    state while a catch-up writes."""

    def book_with_gap(self, now: datetime, *days: date) -> None:
        market = live_market(now)
        for symbol in ("BTCEUR", "ETHEUR", "EURUSDT"):
            market = F.without(market, symbol, *days)
        trend_paper_hook.catch_up(self.state_dir, FakeFetcher(market), clock=lambda: now)

    def test_ok_payload_lists_the_skipped_days(self) -> None:
        self.book_with_gap(DAY3, date(2026, 10, 5))
        payload = self.reader(DAY3).read()
        self.assertEqual(payload["state"], "ok")
        self.assertEqual((payload["days_booked"], payload["days_skipped"]), (2, 1))
        self.assertEqual(payload["skipped_days"], [
            {"day": "2026-10-05", "reason": "no BTCEUR or ETHEUR or EURUSDT candle on 2026-10-05 (no EUR price)"},
        ])
        self.assertEqual((payload["first_day"], payload["last_day"]), ("2026-10-04", "2026-10-06"))
        rows = [r for g in payload["quotes"] for r in g["rows"]]
        self.assertTrue(all(r["days"] == 2 for r in rows))
        json.dumps(payload, allow_nan=False)

    def test_only_skipped_days_is_an_empty_payload_that_says_so(self) -> None:
        market = F.without(live_market(DAY2), "ETHUSDT", date(2026, 9, 1))  # in every signal window
        trend_paper_hook.catch_up(self.state_dir, FakeFetcher(market), clock=lambda: DAY2)
        payload = self.reader(DAY2).read()
        self.assertEqual((payload["state"], payload["days_booked"], payload["days_skipped"]), ("empty", 0, 2))
        self.assertEqual([d["day"] for d in payload["skipped_days"]], ["2026-10-04", "2026-10-05"])
        self.assertEqual(payload["skipped_days"][0]["reason"], "no ETHUSDT close on 2026-09-01 (needed by the signal)")
        self.assertIn("2 paper day(s) skipped", payload["reason"])
        self.assertNotIn("first fill is at", payload["reason"])

    def test_every_state_carries_the_skipped_fields(self) -> None:
        payloads = {"empty": self.reader(DAY2).read()}
        self.book_with_gap(DAY3, date(2026, 10, 5))
        payloads["ok"] = self.reader(DAY3).read()
        with mock.patch.object(trend_reader, "summarize", side_effect=RuntimeError("x")), \
                self.assertLogs("ui.trend_reader", "WARNING"):
            payloads["unavailable"] = self.reader(DAY3).read()
        torn = self.path.read_bytes()[:-7]
        self.path.write_bytes(torn)
        with mock.patch.object(store.time, "sleep"):
            payloads["refused"] = self.reader(DAY3).read()
            with store.LedgerWriter(self.path):
                payloads["being written"] = self.reader(DAY3).read()
        for name, payload in payloads.items():
            with self.subTest(state=name):
                self.assertIsInstance(payload["days_skipped"], int)
                self.assertIsInstance(payload["skipped_days"], list)
                self.assertEqual(payload["days_skipped"], len(payload["skipped_days"]))
        self.assertEqual(payloads["ok"]["days_skipped"], 1)
        self.assertEqual(payloads["refused"]["state"], "refused")
        self.assertEqual(sha(self.path), hashlib.sha256(torn).hexdigest())

    def test_torn_tail_while_a_catch_up_writes_is_transient_not_refused(self) -> None:
        self.book(DAY2)
        torn = self.path.read_bytes()[:-9]
        self.path.write_bytes(torn)
        with mock.patch.object(store.time, "sleep") as pause, store.LedgerWriter(self.path):
            payload = self.reader(DAY3).read()
        self.assertEqual(pause.call_count, store.SETTLE_ATTEMPTS - 1)  # a bounded re-read first
        self.assertEqual(payload["state"], "unavailable")
        self.assertEqual(payload["reason"], trend_reader.BEING_WRITTEN_TEXT)
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["quotes"], [])
        with mock.patch.object(store.time, "sleep"):
            self.assertEqual(self.reader(DAY3).read()["error"]["code"], "LEDGER_TORN")  # no writer: refused
        self.assertEqual(sha(self.path), hashlib.sha256(torn).hexdigest())

    def test_the_reader_never_creates_a_lock_file(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'{"torn":')
        with mock.patch.object(store.time, "sleep"):
            self.assertEqual(self.reader(DAY3).read()["state"], "refused")
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["ledger.jsonl"])

    def test_the_preview_trend_samples_carry_the_skipped_fields(self) -> None:
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location("paper_game_preview_for_trend", root / "scripts" / "paper_game_preview.py")
        preview = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preview)
        workdir = self.state_dir / "preview"
        workdir.mkdir()
        payloads = preview.build_trend_payloads(workdir)
        preview.validate_trend_payloads(payloads, workdir)
        # Only the "skipped" sample records skipped days, through the real catch-up.
        expected = {n: 0 for n in payloads} | {"skipped": 2}
        self.assertEqual({name: p["days_skipped"] for name, p in payloads.items()}, expected)
        self.assertEqual([d["day"] for d in payloads["skipped"]["skipped_days"]], ["2026-10-08", "2026-10-12"])


class TestUnavailable(TrendCase):
    def test_unexpected_error_while_building(self) -> None:
        self.book(DAY2)
        with mock.patch.object(trend_reader, "summarize", side_effect=RuntimeError("secret C:/path")),                 self.assertLogs("ui.trend_reader", "WARNING"):
            payload = self.reader(DAY3).read()
        self.assertEqual(payload["state"], "unavailable")
        self.assertEqual(payload["quotes"], [])
        self.assertNotIn("secret", json.dumps(payload))

    def test_unexpected_error_while_reading(self) -> None:
        with mock.patch.object(trend_reader, "read_ledger_settled", side_effect=MemoryError()),                 self.assertLogs("ui.trend_reader", "WARNING"):
            self.assertEqual(self.reader(DAY3).read()["state"], "unavailable")

    def test_failing_clock(self) -> None:
        reader = TrendReader(self.state_dir, clock=mock.Mock(side_effect=OSError("clock")))
        with self.assertLogs("ui.trend_reader", "WARNING"):
            payload = reader.read()
        self.assertEqual((payload["state"], payload["generated_at"]), ("empty", None))


class TestCatchUp(TrendCase):
    def test_booked_then_up_to_date(self) -> None:
        reader = self.reader(DAY2)
        self.assertEqual(reader.catch_up(), {
            "ok": True, "status": "BOOKED", "days_booked": 2,
            "detail": "Booked 2 paper day(s): 2026-10-04 to 2026-10-05.",
        })
        self.assertEqual(store.read_ledger(self.path).days, (date(2026, 10, 4), date(2026, 10, 5)))
        again = reader.catch_up()
        self.assertEqual((again["ok"], again["status"], again["days_booked"]), (True, "UP_TO_DATE", 0))
        one_more = self.reader(DAY3).catch_up()
        self.assertEqual((one_more["status"], one_more["days_booked"]), ("BOOKED", 1))
        self.assertIn("2026-10-06", one_more["detail"])
        self.assertEqual(self.reader(DAY3).read()["days_booked"], 3)
        self.assertTrue(self.fetchers and all(f.closed for f in self.fetchers))

    def test_before_start(self) -> None:
        result = self.reader(BEFORE).catch_up()
        self.assertEqual((result["ok"], result["status"], result["days_booked"]), (True, "BEFORE_START", 0))
        self.assertIn("2026-10-04 00:00 UTC", result["detail"])
        self.assertFalse(self.path.exists())
        self.assertEqual([f.calls for f in self.fetchers], [[]])  # no request before the start
        self.assertTrue(self.fetchers[0].closed)

    def test_busy_while_the_radar_holds_the_ledger_lock(self) -> None:
        self.book(DAY2)
        before = sha(self.path)
        with store.LedgerWriter(self.path):
            result = self.reader(DAY3).catch_up()
        self.assertEqual((result["ok"], result["status"], result["days_booked"]), (False, "BUSY", 0))
        self.assertEqual(sha(self.path), before)
        self.assertEqual(self.fetchers[-1].calls, [])
        self.assertTrue(self.fetchers[-1].closed)

    def test_market_failed(self) -> None:
        error = KlinesError(KlinesErrorCode.NETWORK, "BTCUSDT: 4 attempts failed")
        reader = self.reader(DAY2, fetcher_factory=lambda: self.fetcher(DAY2, error=error))
        result = reader.catch_up()
        self.assertEqual((result["ok"], result["status"], result["days_booked"]), (False, "MARKET_FAILED", 0))
        self.assertIn("NETWORK", result["detail"])
        self.assertIn("nothing was written", result["detail"])
        self.assertFalse(self.path.exists())
        self.assertTrue(self.fetchers[0].closed)

    def test_market_client_that_does_not_start(self) -> None:
        reader = self.reader(DAY2, fetcher_factory=mock.Mock(side_effect=OSError("no client")))
        with self.assertLogs("ui.trend_reader", "WARNING"):
            result = reader.catch_up()
        self.assertEqual((result["status"], result["days_booked"]), ("MARKET_FAILED", 0))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.reader(DAY2).catch_up()["status"], "BOOKED")  # lock released

    def test_eur_prices_missing_at_the_end_of_the_data_wait(self) -> None:
        market = live_market(DAY2)
        market["BTCEUR"] = P.DailySeries("BTCEUR", ())
        market["EURUSDT"] = P.DailySeries("EURUSDT", ())
        fake = FakeFetcher(market)
        result = self.reader(DAY2, fetcher_factory=lambda: fake).catch_up()
        self.assertEqual((result["ok"], result["status"], result["days_booked"]), (False, "WAITING_FOR_DATA", 0))
        self.assertIn("nothing was written", result["detail"])
        self.assertFalse(self.path.exists())
        self.assertTrue(fake.closed)

    def test_waiting_for_data_just_after_midnight(self) -> None:
        moment = DAY2.replace(hour=0, minute=0, second=20)
        market = F.without(live_market(moment), "ETHUSDT", moment.date())
        result = self.reader(moment, fetcher_factory=lambda: FakeFetcher(market)).catch_up()
        self.assertEqual(result, {
            "ok": False, "status": "WAITING_FOR_DATA", "days_booked": 0,
            "detail": "Waiting for public data (the public USDT daily candles do not reach the 2026-10-05 open yet); "
                      "nothing was written. The next start or Catch up now tries again.",
        })
        self.assertFalse(self.path.exists())
        self.assertEqual(self.reader(moment.replace(minute=3)).catch_up()["status"], "BOOKED")

    def test_network_faults_write_nothing_and_are_retried(self) -> None:
        self.book(at(date(2026, 10, 4)))
        before = sha(self.path)
        for name in F.fault_scenarios():
            with self.subTest(fault=name):
                faults, kind = F.fault_scenarios()[name]
                exchange = F.FakeExchange(live_market(DAY3), faults)
                result = self.reader(DAY3, fetcher_factory=F.adapter_factory(exchange)).catch_up()
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "MARKET_FAILED" if kind == "market" else "WAITING_FOR_DATA")
                self.assertEqual(result["days_booked"], 0)
                self.assertIn("nothing was written", result["detail"])
                self.assertNotIn(str(self.state_dir), result["detail"])
                self.assertEqual(sha(self.path), before)
        good = self.reader(DAY3, fetcher_factory=F.adapter_factory(F.FakeExchange(live_market(DAY3)))).catch_up()
        self.assertEqual((good["status"], good["days_booked"]), ("BOOKED", 2))

    def test_booked_with_a_skipped_day(self) -> None:
        market = live_market(DAY3)
        for symbol in ("BTCEUR", "EURUSDT"):
            market = F.without(market, symbol, date(2026, 10, 5))
        result = self.reader(DAY3, fetcher_factory=lambda: FakeFetcher(market)).catch_up()
        self.assertEqual(result, {
            "ok": True, "status": "BOOKED", "days_booked": 2,
            "detail": "Booked 2 paper day(s): 2026-10-04 to 2026-10-06. Skipped 1 paper day(s) because a public "
                      "candle is missing: 2026-10-05.",
        })

    def test_ledger_refused(self) -> None:
        self.book(DAY2)
        self.path.write_bytes(self.path.read_bytes()[:-3])
        before = sha(self.path)
        result = self.reader(DAY3).catch_up()
        self.assertEqual((result["ok"], result["status"]), (False, "LEDGER_REFUSED"))
        self.assertIn("LEDGER_TORN", result["detail"])
        self.assertEqual(sha(self.path), before)
        self.assertTrue(self.fetchers[-1].closed)

    def test_in_progress_on_overlapping_calls(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        factory = mock.Mock(side_effect=lambda: self.fetcher(DAY2, block=release))
        reader = self.reader(DAY2, fetcher_factory=factory)
        results: list[dict[str, Any]] = []
        first = threading.Thread(target=lambda: results.append(reader.catch_up()))
        first.start()
        for _ in range(500):
            if self.fetchers and self.fetchers[0].calls:
                break
            threading.Event().wait(0.01)
        self.assertTrue(self.fetchers[0].calls, "the first catch-up never reached the fetch")
        second = reader.catch_up()
        self.assertEqual(second, {
            "ok": False, "status": "IN_PROGRESS", "days_booked": 0, "detail": "A catch-up is already running.",
        })
        self.assertEqual(factory.call_count, 1)
        release.set()
        first.join(30)
        self.assertFalse(first.is_alive())
        self.assertEqual(results[0]["status"], "BOOKED")
        self.assertTrue(self.fetchers[0].closed)
        self.assertEqual(reader.catch_up()["status"], "UP_TO_DATE")  # lock released after success

    def test_lock_released_after_an_unexpected_exception(self) -> None:
        reader = self.reader(DAY2, fetcher_factory=lambda: self.fetcher(DAY2, error=RuntimeError("boom")))
        with self.assertRaises(RuntimeError):
            reader.catch_up()
        self.assertTrue(self.fetchers[0].closed)
        self.assertFalse(self.path.exists())
        reader._fetcher_factory = lambda: self.fetcher(DAY2)
        self.assertEqual(reader.catch_up()["status"], "BOOKED")

    def test_default_client_is_the_public_binance_one(self) -> None:
        fake = self.fetcher(DAY2)
        with mock.patch.object(trend_paper_hook, "default_fetcher", return_value=fake) as default:
            result = TrendReader(self.state_dir, clock=lambda: DAY2).catch_up()
        default.assert_called_once_with()
        self.assertEqual(result["status"], "BOOKED")
        self.assertTrue(fake.closed)
        from radar_v08.adapters.binance_public_klines import BinancePublicKlines

        with mock.patch.object(BinancePublicKlines, "__init__", return_value=None) as init:
            client = trend_paper_hook.default_fetcher()
        init.assert_called_once_with()
        self.assertIsInstance(client, BinancePublicKlines)


class TestBridge(TrendCase):
    def api(self, reader: Any) -> Any:
        from ui.bridge import Api

        api = Api.__new__(Api)
        api._trend = reader
        return api

    def test_api_delegates_to_the_reader(self) -> None:
        api = self.api(self.reader(DAY2))
        self.assertEqual(api.get_trend_paper_state()["state"], "empty")
        result = api.trend_paper_catch_up()
        self.assertEqual((result["status"], result["days_booked"]), ("BOOKED", 2))
        state = api.get_trend_paper_state()
        self.assertEqual((state["state"], state["days_booked"]), ("ok", 2))

    def test_get_state_never_raises(self) -> None:
        api = self.api(mock.Mock(read=mock.Mock(side_effect=RuntimeError("boom"))))
        self.assertEqual(api.get_trend_paper_state()["state"], "unavailable")

    def test_api_init_builds_the_reader_from_config(self) -> None:
        from ui import bridge

        with mock.patch.object(config, "STATE_DIR", str(self.state_dir)), \
                mock.patch.object(bridge, "SnapshotStore"), mock.patch.object(bridge, "DataReader"), \
                mock.patch.object(bridge.process_manager, "ProcessManager"), \
                mock.patch.object(bridge.ui_state, "load", return_value={}), \
                mock.patch.object(bridge.paper_reader, "from_config"), \
                mock.patch.object(bridge.pilot_reader, "from_config"):
            api = bridge.Api()
        self.assertIsInstance(api._trend, TrendReader)
        self.assertEqual(api._trend.path, self.path)
        self.assertEqual(api.get_trend_paper_state()["state"], "empty")
        self.assertFalse((self.state_dir / store.LEDGER_DIR_NAME).exists())

    def test_only_the_catch_up_writes(self) -> None:
        from ui.bridge import Api

        trend_methods = {name for name in vars(Api) if "trend" in name}
        self.assertEqual(trend_methods, {"get_trend_paper_state", "trend_paper_catch_up"})


if __name__ == "__main__":
    unittest.main()
