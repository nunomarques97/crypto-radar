"""Kraken EUR paper books: domain, ledger, catch-up and hook isolation.

* domain: 12 books (six rules x 0.4% maker assumption / 0.8% taker sensitivity), the same signals
  and targets as the main ledger's EUR books, fills at the Kraken open with the main accounting, no
  substitute price;
* ledger: ``kraken_ledger.jsonl`` canonical, chained, append-only, own lock, a day whole or not at
  all, torn/edited/invalid content refused and never rewritten; the main ledger format untouched;
* catch-up: every request before the first append; network, rate-limit, invalid-row and clock-skew
  failures write nothing; WAITING_FOR_DATA; idempotent; in-order backfill; confirmed skips only; a
  gap before Kraken's truncated history is never confirmed;
* hook: the Kraken step runs after the Binance step in its own try/except, never reaches the radar,
  never touches ledger.jsonl or alerts.jsonl, runs when the Binance step fails, never alerts, and
  the off flag still starts nothing.

No network: socket connections are refused for the whole module; fake fetchers or fake HTTP
sessions and a temporary state dir only.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import requests  # noqa: E402
import trend_paper_fakes as F  # noqa: E402  (offline market data and fake exchanges)
from trend_paper_fakes import (  # noqa: E402
    FakeFetcher,
    FakeKrakenFetcher,
    at,
    kraken_market,
    live_market,
)

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
from radar_v08.domain import trend_paper as P  # noqa: E402
from radar_v08.domain import trend_paper_kraken as K  # noqa: E402

NOW = at(date(2026, 10, 6))
START = P.PAPER_START
TS = "2026-10-06T08:00:00+00:00"
_patches = []


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


def k_market(now=NOW, kraken=None):
    """The USDT signal series of ``live_market(now)`` plus the Kraken pairs (no Binance EUR series)."""
    market = live_market(now)
    return {**{s: market[s] for s in K.SIGNAL_SYMBOLS}, **(kraken if kraken is not None else kraken_market(now))}


def in_memory(extend, plan_and_apply):
    """Run ``plan_and_apply(ledger, append)`` against an in-memory ledger; returns (ledger, data)."""
    state = {"ledger": extend(P.EMPTY_LEDGER, b"", START), "data": b""}

    def append(data: bytes):
        state["ledger"] = extend(state["ledger"], data, START)
        state["data"] += data
        return state["ledger"]

    plan_and_apply(state["ledger"], append)
    return state["ledger"], state["data"]


def kraken_ledger(market=None, today=NOW.date(), refetch=None):
    market = market if market is not None else k_market()
    return in_memory(
        K.extend_kraken_ledger,
        lambda ledger, append: K.catch_up_kraken_days(ledger, market, TS, append, START, today=today, refetch=refetch),
    )


def main_ledger(market=None, today=NOW.date()):
    market = market if market is not None else live_market(NOW)
    return in_memory(
        P.extend_ledger, lambda ledger, append: P.catch_up_days(ledger, market, TS, append, START, today=today)
    )


def records_of(ledger, book_id):
    return {r["date"]: r for r in ledger.records if r["book"] == book_id}


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


class Books(unittest.TestCase):
    def test_twelve_books_each_starting_at_7000_eur(self):
        self.assertEqual(len(K.KRAKEN_BOOKS), 12)
        self.assertEqual({(b.rule, b.fee) for b in K.KRAKEN_BOOKS}, {(r, f) for r in P.Rule for f in (0.004, 0.008)})
        self.assertEqual(K.FEE_LABELS, {0.004: "0.4% maker assumption", 0.008: "0.8% taker sensitivity"})
        self.assertEqual(K.KRAKEN_BOOK_IDS[0], "KRAKEN_EUR|ENS|0.004")
        for book in K.KRAKEN_BOOKS:
            self.assertEqual(sum(s.cash for s in P.initial_state(book).values()), 7000.0)

    def test_comparators_stay_inside_the_same_venue_and_fee(self):
        ids = set(K.KRAKEN_BOOK_IDS)
        for book in K.KRAKEN_BOOKS:
            if book.rule in (P.Rule.BH_5050, P.Rule.BH_BTC):
                self.assertIsNone(book.comparator)
                continue
            self.assertIn(book.comparator, ids)
            self.assertTrue(book.comparator.startswith("KRAKEN_EUR|"))
            self.assertTrue(book.comparator.endswith(f"|{book.fee}"))

    def test_the_main_books_are_unchanged(self):
        self.assertEqual(len(P.BOOKS), 24)
        self.assertEqual(P.FEES, (0.001, 0.004))
        self.assertFalse(any("KRAKEN" in b for b in P.BOOK_IDS))
        self.assertEqual(P.BOOK_IDS[0], "EUR|ENS|0.001")

    def test_no_new_rule_parameter_or_registry_event(self):
        for book in K.KRAKEN_BOOKS:
            self.assertIs(book.spec, P.RULES[book.rule])
        source = (REPOSITORY_ROOT / "radar_v08" / "domain" / "trend_paper_kraken.py").read_text(encoding="utf-8")
        for word in ("trend_registry", "append_event", "registry_store", "import os", "datetime.now", "time.time"):
            self.assertNotIn(word, source)


class SignalsAndFills(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kraken, _ = kraken_ledger()
        cls.main, _ = main_ledger()
        cls.market = k_market()

    def test_signals_and_targets_equal_the_main_eur_records(self):
        self.assertEqual(self.kraken.days, self.main.days)
        self.assertEqual(len(self.kraken.days), 3)
        for rule in P.Rule:
            for k_fee in K.KRAKEN_FEES:
                k_records = records_of(self.kraken, K.kraken_book_id(rule, k_fee))
                for m_fee in P.FEES:
                    m_records = records_of(self.main, P.book_id("EUR", rule, m_fee))
                    for day in self.main.days:
                        k, m = k_records[day.isoformat()], m_records[day.isoformat()]
                        for asset in m["assets"]:
                            with self.subTest(rule=rule, day=day, asset=asset, fees=(k_fee, m_fee)):
                                ka, ma = k["assets"][asset], m["assets"][asset]
                                for key in ("signal", "target", "signal_close_date", "signal_close_usdt", "symbol"):
                                    self.assertEqual(ka[key], ma[key], key)
                                self.assertEqual(ka["registered_sha256"], ma["registered_sha256"])

    def test_fills_at_the_kraken_open_with_the_main_accounting(self):
        day = START
        record = records_of(self.kraken, K.kraken_book_id(P.Rule.ENS, 0.008))[day.isoformat()]
        self.assertEqual((record["venue"], record["quote"], record["fee_label"]), ("KRAKEN", "EUR", "0.8% taker sensitivity"))
        self.assertEqual(record["slippage_bps"], 0.0)
        signals = P.day_signals(self.market, day)
        equity = 0.0
        for asset, pair in K.KRAKEN_PAIR.items():
            v = record["assets"][asset]
            px = self.market[pair].open_on(day)
            self.assertEqual((v["fill_price"], v["fill_source"], v["pair"]), (px, f"Kraken {pair} open", pair))
            fill = P.step_fraction(
                P.Sleeve(3500.0, 0.0, 0.0), px, signals.ens[asset].values["ENS"], 0.008, buy_and_hold=False, first_day=True
            )
            self.assertEqual(v["equity_after"], fill.equity_after)
            self.assertEqual(v["fee"], fill.fee)
            equity += fill.equity_after
        self.assertEqual(record["equity"], equity)
        t5 = records_of(self.kraken, K.kraken_book_id(P.Rule.BTC_TREND5, 0.004))[day.isoformat()]["assets"]["BTC"]
        fill = P.step_units(
            P.Sleeve(7000.0, 0.0, 0.0), self.market["XBTEUR"].open_on(day), signals.trend5.values["BTC_TREND5"], 0.15, 0.004
        )
        self.assertEqual((t5["equity_after"], t5["fee"]), (fill.equity_after, fill.fee))

    def test_the_kraken_price_differs_from_the_binance_eur_fill(self):
        k = records_of(self.kraken, "KRAKEN_EUR|ENS|0.004")["2026-10-04"]["assets"]["BTC"]["fill_price"]
        m = records_of(self.main, "EUR|ENS|0.001")["2026-10-04"]["assets"]["BTC"]["fill_price"]
        self.assertNotEqual(k, m)

    def test_a_missing_kraken_open_is_never_substituted(self):
        day = date(2026, 10, 5)
        market = {**live_market(NOW), **F.without(kraken_market(NOW), "XBTEUR", day)}  # Binance BTCEUR, EURUSDT present
        self.assertIsNotNone(market["BTCEUR"].open_on(day))
        with self.assertRaises(P.PaperError) as caught:
            K.book_kraken_day(market, day, {}, TS)
        self.assertIs(caught.exception.code, P.PaperErrorCode.MISSING_CANDLE)
        self.assertIn("Kraken XBTEUR", str(caught.exception))

    def test_days_before_the_start_are_never_booked(self):
        with self.assertRaises(P.PaperError):
            K.book_kraken_day(self.market, START - timedelta(days=1), {}, TS)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class KrakenLedgerFormat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger, cls.data = kraken_ledger()

    def test_canonical_chained_lines_twelve_per_day(self):
        lines = self.data.split(b"\n")[:-1]
        self.assertEqual(len(lines), 36)
        prev = P.GENESIS
        for n, line in enumerate(lines):
            obj = json.loads(line)
            self.assertEqual(P.canonical(obj), line)
            self.assertEqual((obj["seq"], obj["prev"], obj["schema"]), (n, prev, 1))
            body = {k: v for k, v in obj.items() if k != "sha256"}
            self.assertEqual(obj["sha256"], hashlib.sha256(P.canonical(body)).hexdigest())
            self.assertEqual(obj["book"], K.KRAKEN_BOOK_IDS[n % 12])
            prev = obj["sha256"]
        self.assertEqual(K.parse_kraken_ledger(self.data).records, self.ledger.records)

    def test_torn_edited_or_invalid_content_is_refused(self):
        lines = self.data.split(b"\n")[:-1]
        edited = lines[3].replace(b'"rule":"ENS_VT"', b'"rule":"ENS"')
        self.assertNotEqual(edited, lines[3])
        main_bytes = main_ledger()[1]
        cases = {
            "LEDGER_TORN": self.data[:-7],
            "LEDGER_EDITED": b"\n".join(lines[:3] + [edited] + lines[4:]) + b"\n",
            "LEDGER_INVALID": b"\n".join(lines[:11]) + b"\n",  # a day without all 12 books
        }
        for code, data in cases.items():
            with self.subTest(code=code), self.assertRaises(P.PaperError) as caught:
                K.parse_kraken_ledger(data)
            self.assertEqual(caught.exception.code.value, code)
        with self.assertRaises(P.PaperError) as caught:  # the main ledger is not a Kraken ledger
            K.parse_kraken_ledger(main_bytes)
        self.assertIs(caught.exception.code, P.PaperErrorCode.LEDGER_INVALID)
        with self.assertRaises(P.PaperError) as caught:  # and the other way round
            P.parse_ledger(self.data)
        self.assertIs(caught.exception.code, P.PaperErrorCode.LEDGER_INVALID)

    def test_skip_entries_are_verified(self):
        gap = K.KrakenDayGap(START, K.KrakenSkipReason.NO_KRAKEN_OPEN, (P.Gap("XBTEUR", START),))
        data = K.encode_kraken_skip(P.EMPTY_LEDGER, gap, TS)
        ledger = K.parse_kraken_ledger(data)
        self.assertEqual(ledger.last_day, START)
        self.assertEqual(K.kraken_skipped(ledger)[0].text, "no Kraken XBTEUR candle on 2026-10-04 (no Kraken fill price)")
        obj = json.loads(data)
        for change in ({"reason": "NO_EUR_PRICE"}, {"missing": ["BTCEUR"]}, {"gap_day": "2026-10-03"}):
            body = {k: v for k, v in {**obj, **change}.items() if k != "sha256"}
            line = P.canonical({**body, "sha256": hashlib.sha256(P.canonical(body)).hexdigest()}) + b"\n"
            with self.subTest(change=change), self.assertRaises(P.PaperError) as caught:
                K.parse_kraken_ledger(line)
            self.assertIs(caught.exception.code, P.PaperErrorCode.LEDGER_INVALID)
        with self.assertRaises(P.PaperError):  # a Kraken skip entry is not a main skip entry
            P.parse_ledger(data)

    def test_the_store_default_still_reads_the_main_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = store.ledger_path(tmp)
            path.parent.mkdir(parents=True)
            path.write_bytes(main_ledger()[1])
            self.assertEqual(len(store.read_ledger(path).days), 3)
            path.write_bytes(self.data)
            with self.assertRaises(P.PaperError):
                store.read_ledger(path)
            self.assertEqual(len(store.read_ledger(path, extend=K.extend_kraken_ledger).days), 3)


# ---------------------------------------------------------------------------
# Catch-up through the hook's kraken_catch_up (fake fetchers, temporary state dir)
# ---------------------------------------------------------------------------


class Recorder:
    """Wraps a fetcher; records the Kraken and main ledger sizes at every request."""

    def __init__(self, inner, paths, sizes):
        self.inner, self.paths, self.sizes = inner, paths, sizes

    def fetch_daily(self, symbol, since, now_ms):
        self.sizes.append(tuple(p.stat().st_size if p.exists() else -1 for p in self.paths))
        return self.inner.fetch_daily(symbol, since, now_ms)

    def close(self):
        self.inner.close()


class CatchUpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.path = trend_paper_hook.kraken_ledger_path(self.state_dir)
        self.main_path = store.ledger_path(self.state_dir)

    def run_kraken(self, now=NOW, binance=None, kraken=None):
        self.binance = binance if binance is not None else FakeFetcher(live_market(now))
        self.kraken = kraken if kraken is not None else FakeKrakenFetcher(kraken_market(now))
        return trend_paper_hook.kraken_catch_up(self.state_dir, self.binance, self.kraken, clock=lambda: now)


class CatchUp(CatchUpCase):
    def test_path_is_next_to_the_main_ledger_and_gitignored(self):
        self.assertEqual(self.path, self.state_dir / "trend_paper" / "kraken_ledger.jsonl")
        self.assertIn("/trend_paper/", (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_books_every_due_day_and_reruns_are_idempotent(self):
        result = self.run_kraken()
        self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.BOOKED)
        self.assertEqual(result.booked, (date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)))
        self.assertEqual(result.records_appended, 36)
        self.assertEqual([c[0] for c in self.binance.calls], ["BTCUSDT", "ETHUSDT"])
        lead = START - timedelta(days=K.KRAKEN_LEAD_DAYS)
        self.assertEqual([(c[0], c[1]) for c in self.kraken.calls], [("XBTEUR", lead), ("ETHEUR", lead)])
        data = self.path.read_bytes()
        self.assertFalse(self.main_path.exists())  # the main ledger is never written here
        result = self.run_kraken()
        self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.UP_TO_DATE)
        self.assertEqual((self.binance.calls, self.kraken.calls), ([], []))
        self.assertEqual(self.path.read_bytes(), data)

    def test_before_the_start_nothing_is_requested_or_written(self):
        result = self.run_kraken(now=at(date(2026, 10, 3), 20))
        self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.BEFORE_START)
        self.assertEqual((self.binance.calls, self.kraken.calls), ([], []))
        self.assertFalse(self.path.exists())

    def test_missed_days_are_backfilled_in_order(self):
        self.run_kraken(now=at(date(2026, 10, 4)))
        self.assertEqual(store.read_ledger(self.path, extend=K.extend_kraken_ledger).days, (START,))
        later = at(date(2026, 10, 9))
        result = self.run_kraken(now=later)
        self.assertEqual(result.booked, tuple(date(2026, 10, d) for d in range(5, 10)))
        self.assertEqual(self.kraken.calls[0][1], date(2026, 10, 5) - timedelta(days=K.KRAKEN_LEAD_DAYS))
        with tempfile.TemporaryDirectory() as other:
            trend_paper_hook.kraken_catch_up(
                other, FakeFetcher(live_market(later)), FakeKrakenFetcher(kraken_market(later)), clock=lambda: later
            )
            single = trend_paper_hook.kraken_ledger_path(other).read_bytes()
        self.assertEqual(F.strip_ts(self.path.read_bytes()), F.strip_ts(single))

    def test_every_request_happens_before_the_first_append(self):
        gone = date(2026, 10, 5)
        sizes: list[tuple[int, int]] = []
        paths = (self.path, self.main_path)
        result = self.run_kraken(
            binance=Recorder(FakeFetcher(live_market(NOW)), paths, sizes),
            kraken=Recorder(FakeKrakenFetcher(F.without(kraken_market(NOW), "XBTEUR", gone)), paths, sizes),
        )
        self.assertEqual(result.skipped, (gone,))
        self.assertEqual(len(sizes), 5)  # BTCUSDT, ETHUSDT, XBTEUR, ETHEUR, then the XBTEUR confirmation
        self.assertEqual(set(sizes), {(-1, -1)})
        self.assertGreater(self.path.stat().st_size, 0)

    def test_market_failures_write_nothing(self):
        failures = {
            "binance network": (KlinesError(KlinesErrorCode.NETWORK, "down"), None),
            "binance ban": (KlinesError(KlinesErrorCode.BANNED, "418"), None),
            "kraken network": (None, KrakenOhlcError(KrakenOhlcErrorCode.NETWORK, "down")),
            "kraken rate limit": (None, KrakenOhlcError(KrakenOhlcErrorCode.RATE_LIMITED, "EAPI:Rate limit exceeded")),
            "kraken invalid rows": (None, KrakenOhlcError(KrakenOhlcErrorCode.NON_FINITE, "NaN")),
            "kraken clock skew": (None, KrakenOhlcError(KrakenOhlcErrorCode.CLOCK_SKEW, "behind")),
        }
        for name, (b_error, k_error) in failures.items():
            with self.subTest(failure=name), self.assertRaises((KlinesError, KrakenOhlcError)):
                self.run_kraken(
                    binance=FakeFetcher(live_market(NOW), error=b_error),
                    kraken=FakeKrakenFetcher(kraken_market(NOW), error=k_error),
                )
            self.assertFalse(self.path.exists())
        self.assertEqual(self.run_kraken().records_appended, 36)

    def test_a_failed_confirmation_request_writes_nothing(self):
        class FailsOnSecond(FakeKrakenFetcher):
            def fetch_daily(self, symbol, since, now_ms):
                if any(c[0] == symbol for c in self.calls):
                    self.calls.append((symbol, since, now_ms))
                    raise KrakenOhlcError(KrakenOhlcErrorCode.NETWORK, "second request failed")
                return super().fetch_daily(symbol, since, now_ms)

        with self.assertRaises(KrakenOhlcError):
            self.run_kraken(kraken=FailsOnSecond(F.without(kraken_market(NOW), "ETHEUR", date(2026, 10, 5))))
        self.assertFalse(self.path.exists())

    def test_real_adapters_failures_write_nothing(self):
        """The real Kraken and Binance adapters over fake HTTP sessions: each fault raises a typed
        market error or waits, and nothing is written; then a clean run books."""
        k_faults = {
            "kraken http 5xx": ({"XBTEUR": [F.KrakenResponse(503) for _ in range(3)]}, "market"),
            "kraken connection error": ({"ETHEUR": [requests.ConnectionError("refused") for _ in range(3)]}, "market"),
            "kraken rate limit envelope": (
                {"XBTEUR": [F.KrakenResponse(200, b'{"error":["EAPI:Rate limit exceeded"],"result":{}}')]}, "market",
            ),
            "kraken invalid json": ({"ETHEUR": [F.KrakenResponse(200, b"<html>busy</html>")]}, "market"),
            "kraken NaN open": ({"XBTEUR": [lambda rows: self.envelope("XBTEUR", [[rows[0][0], "NaN", *rows[0][2:]], *rows[1:]])]}, "market"),
            "kraken duplicate row": ({"ETHEUR": [lambda rows: self.envelope("ETHEUR", [rows[0], *rows])]}, "market"),
            "kraken data ending before today": ({"XBTEUR": [lambda rows: self.envelope("XBTEUR", rows[:-1])]}, "waiting"),
        }
        b_faults = {
            name: spec for name, spec in F.fault_scenarios().items()
            if set(spec[0]) <= {"BTCUSDT", "ETHUSDT", "*"}
        }
        self.assertGreaterEqual(len(b_faults), 6)
        for name, (faults, kind) in list(k_faults.items()) + [(f"binance {n}", s) for n, s in b_faults.items()]:
            with self.subTest(fault=name):
                k_exchange = F.FakeKrakenExchange(kraken_market(NOW), faults if name.startswith("kraken") else None)
                b_exchange = F.FakeExchange(live_market(NOW), faults if name.startswith("binance") else None)
                binance = F.adapter_factory(b_exchange)()
                kraken = F.kraken_adapter_factory(k_exchange)()
                if kind == "market":
                    with self.assertRaises((KlinesError, KrakenOhlcError)):
                        trend_paper_hook.kraken_catch_up(self.state_dir, binance, kraken, clock=lambda: NOW)
                else:
                    result = trend_paper_hook.kraken_catch_up(self.state_dir, binance, kraken, clock=lambda: NOW)
                    self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.WAITING_FOR_DATA)
                self.assertFalse(self.path.exists())
        k_exchange = F.FakeKrakenExchange(kraken_market(NOW))
        result = trend_paper_hook.kraken_catch_up(
            self.state_dir, F.adapter_factory(F.FakeExchange(live_market(NOW)))(),
            F.kraken_adapter_factory(k_exchange)(), clock=lambda: NOW,
        )
        self.assertEqual(result.records_appended, 36)
        for record in store.read_ledger(self.path, extend=K.extend_kraken_ledger).records:
            for v in record["assets"].values():  # the still-open candle's wrong high/close is never a fill
                self.assertLess(v["fill_price"], 1e6)

    @staticmethod
    def envelope(pair, rows):
        return F.KrakenResponse(200, F.FakeKrakenExchange(kraken_market(NOW)).envelope(pair, rows))

    def test_a_clock_behind_kraken_is_clock_skew_and_writes_nothing(self):
        local = NOW.replace(hour=0) - timedelta(minutes=30)  # 2026-10-05 23:30, the exchange is at 10-06
        k_exchange = F.FakeKrakenExchange(kraken_market(NOW))
        with self.assertRaises(KrakenOhlcError) as caught:
            trend_paper_hook.kraken_catch_up(
                self.state_dir, FakeFetcher(live_market(NOW)), F.kraken_adapter_factory(k_exchange)(), clock=lambda: local
            )
        self.assertIs(caught.exception.code, KrakenOhlcErrorCode.CLOCK_SKEW)
        self.assertFalse(self.path.exists())

    def test_waiting_for_data_writes_nothing(self):
        just_after_midnight = NOW.replace(hour=0, minute=0, second=30)
        cases = {
            "USDT not at today's open": (
                F.without(live_market(just_after_midnight), "BTCUSDT", just_after_midnight.date()),
                kraken_market(just_after_midnight),
                "the public USDT daily candles do not reach the 2026-10-06 open yet",
            ),
            "Kraken not at today's open": (
                live_market(just_after_midnight),
                F.without(kraken_market(just_after_midnight), "ETHEUR", just_after_midnight.date()),
                "no Kraken ETHEUR candle on 2026-10-06 yet (none after it either)",
            ),
            "clock ahead across midnight UTC": (
                F.ending(F.ending(live_market(NOW), "BTCUSDT", date(2026, 10, 5)), "ETHUSDT", date(2026, 10, 5)),
                F.ending(F.ending(kraken_market(NOW), "XBTEUR", date(2026, 10, 5)), "ETHEUR", date(2026, 10, 5)),
                "do not reach the 2026-10-06 open yet",
            ),
        }
        for name, (b_market, k_market_, detail) in cases.items():
            with self.subTest(case=name):
                result = self.run_kraken(now=just_after_midnight, binance=FakeFetcher(b_market), kraken=FakeKrakenFetcher(k_market_))
                self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.WAITING_FOR_DATA)
                self.assertIn(detail, result.detail)
                self.assertFalse(self.path.exists())
        self.assertEqual(self.run_kraken(now=just_after_midnight.replace(minute=5)).records_appended, 36)


class Skips(CatchUpCase):
    def test_confirmed_missing_kraken_open_is_a_chained_skip_entry(self):
        gone = date(2026, 10, 5)
        result = self.run_kraken(kraken=FakeKrakenFetcher(F.without(kraken_market(NOW), "XBTEUR", gone)))
        self.assertEqual((result.booked, result.skipped), ((START, date(2026, 10, 6)), (gone,)))
        confirm_since = gone - timedelta(days=K.KRAKEN_LEAD_DAYS)
        self.assertEqual([(c[0], c[1]) for c in self.kraken.calls[2:]], [("XBTEUR", confirm_since)])  # the confirmation
        ledger = store.read_ledger(self.path, extend=K.extend_kraken_ledger)
        (skip,) = K.kraken_skipped(ledger)
        self.assertEqual((skip.reason, skip.missing, skip.gap_day), (K.KrakenSkipReason.NO_KRAKEN_OPEN, ("XBTEUR",), gone))
        lines = self.path.read_bytes().split(b"\n")[:-1]
        self.assertEqual(len(lines), 25)
        self.assertIn(b'"reason":"NO_KRAKEN_OPEN"', lines[12])
        # The books carry over the skipped day unchanged (nothing imputed): 10-06 starts from the
        # 10-04 sleeves valued at the 10-06 Kraken open.
        for book in K.KRAKEN_BOOKS:
            r1, r3 = (records_of(ledger, book.id)[d] for d in ("2026-10-04", "2026-10-06"))
            before = sum(
                r1["assets"][a]["cash_after"] + r1["assets"][a]["units_after"] * v["fill_price"]
                for a, v in r3["assets"].items()
            )
            self.assertAlmostEqual(r3["equity_before"], before, places=9)
            if book.spec.family is P.Family.ENS:
                for asset, v in r3["assets"].items():
                    self.assertEqual(v["held_before"], r1["assets"][asset]["held_after"])
        self.assertEqual(self.run_kraken().status, trend_paper_hook.CatchUpStatus.UP_TO_DATE)

    def test_missing_usdt_signal_close_is_a_skip_entry(self):
        gone = START  # the ETHUSDT close of 2026-10-04 feeds the 10-05 and 10-06 signals
        result = self.run_kraken(binance=FakeFetcher(F.without(live_market(NOW), "ETHUSDT", gone)))
        self.assertEqual((result.booked, result.skipped), ((START,), (date(2026, 10, 5), date(2026, 10, 6))))
        skips = K.kraken_skipped(result.ledger)
        self.assertEqual({s.reason for s in skips}, {K.KrakenSkipReason.NO_SIGNAL_CLOSE})
        self.assertEqual(skips[0].text, "no ETHUSDT close on 2026-10-04 (needed by the signal)")
        self.assertEqual([c[0] for c in self.binance.calls[2:]], ["ETHUSDT"])

    def test_an_absence_seen_in_one_response_only_writes_nothing(self):
        gone = date(2026, 10, 5)
        result = self.run_kraken(
            kraken=FakeKrakenFetcher(F.without(kraken_market(NOW), "XBTEUR", gone), second=kraken_market(NOW))
        )
        self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.WAITING_FOR_DATA)
        self.assertIn("two requests disagree on Kraken XBTEUR 2026-10-05", result.detail)
        self.assertFalse(self.path.exists())
        market = k_market(kraken=F.without(kraken_market(NOW), "XBTEUR", gone))
        with self.assertRaises(P.PaperError) as caught:  # no second request: never confirmed
            kraken_ledger(market)
        self.assertIs(caught.exception.code, P.PaperErrorCode.WAITING_FOR_DATA)

    def test_a_gap_at_the_end_of_the_data_waits(self):
        result = self.run_kraken(kraken=FakeKrakenFetcher(F.ending(kraken_market(NOW), "XBTEUR", date(2026, 10, 4))))
        self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.WAITING_FOR_DATA)
        self.assertFalse(self.path.exists())

    def test_a_gap_before_krakens_truncated_history_is_never_confirmed(self):
        cut = date(2026, 10, 6)  # Kraken returns nothing before 10-06 (its 720-candle cap)
        for second in (None, kraken_market(NOW)):
            with self.subTest(second=second is not None):
                result = self.run_kraken(kraken=FakeKrakenFetcher(kraken_market(NOW), history_from=cut, second=second))
                self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.WAITING_FOR_DATA)
                self.assertIn("history starts at 2026-10-06", result.detail)
                self.assertIn("never a confirmed missing candle", result.detail)
                self.assertFalse(self.path.exists())
        plan = K.plan_kraken_catch_up(P.EMPTY_LEDGER, k_market(), START, NOW.date())
        self.assertEqual(plan.gaps, {})
        truncated = FakeKrakenFetcher(kraken_market(NOW), history_from=cut)
        second = {p: truncated.fetch_daily(p, START, 0) for p in K.KRAKEN_PAIRS}
        gap_plan = K.KrakenPlan(plan.days, {START: K.KrakenDayGap(START, K.KrakenSkipReason.NO_KRAKEN_OPEN, (P.Gap("XBTEUR", START),))})
        self.assertIsNotNone(K.confirm_kraken_plan(gap_plan, second).waiting)


class Locks(CatchUpCase):
    def test_a_second_writer_is_busy_and_writes_nothing(self):
        with store.LedgerWriter(self.path, extend=K.extend_kraken_ledger):
            with self.assertRaises(store.TrendPaperStoreError) as caught:
                self.run_kraken()
        self.assertIs(caught.exception.code, store.TrendPaperStoreErrorCode.BUSY)
        self.assertEqual((self.binance.calls, self.kraken.calls), ([], []))
        self.assertFalse(self.path.exists())

    def test_the_main_lock_does_not_block_the_kraken_ledger(self):
        with store.LedgerWriter(self.main_path):
            self.assertEqual(self.run_kraken().records_appended, 36)
        self.assertFalse(self.main_path.exists())

    def test_refused_ledgers_are_never_rewritten(self):
        self.run_kraken()
        good = self.path.read_bytes()
        for damaged in (good[:-3], good.replace(b'"fee_rate":0.004', b'"fee_rate":0.005', 1), main_ledger()[1]):
            self.path.write_bytes(damaged)
            with self.subTest(size=len(damaged)), self.assertRaises(P.PaperError) as caught:
                self.run_kraken(now=at(date(2026, 10, 8)))
            self.assertIn(caught.exception.code, trend_paper_hook.LEDGER_REFUSED_CODES)
            self.assertEqual(self.path.read_bytes(), damaged)
            self.assertEqual((self.binance.calls, self.kraken.calls), ([], []))


# ---------------------------------------------------------------------------
# The start hook: the Kraken step after the Binance step, isolated
# ---------------------------------------------------------------------------


class HookIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.path = trend_paper_hook.kraken_ledger_path(self.state_dir)
        self.main_path = store.ledger_path(self.state_dir)
        self.alerts_path = alert_store.alerts_path(self.state_dir)
        self.toast = mock.Mock(return_value=True)
        self.unhandled: list = []
        previous = threading.excepthook
        threading.excepthook = self.unhandled.append
        self.addCleanup(setattr, threading, "excepthook", previous)
        for target, name, value in (
            (config, "STATE_DIR", str(self.state_dir)),
            (config, "RADAR_TREND_PAPER_ENABLED", True),
            (trend_paper_hook, "utc_now", lambda: NOW),
            (notifications, "send_windows_notification", self.toast),
            (trend_paper_hook, "default_fetcher", lambda: FakeFetcher(live_market(NOW))),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def kraken_ok(self):
        return FakeFetcher(live_market(NOW)), FakeKrakenFetcher(kraken_market(NOW))

    def failing(self):
        def factory_raises():
            raise OSError("no session")

        def refused():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(b'{"torn":')
            return self.kraken_ok()

        return {
            "kraken network": lambda: (FakeFetcher(live_market(NOW)), FakeKrakenFetcher(
                kraken_market(NOW), error=KrakenOhlcError(KrakenOhlcErrorCode.NETWORK, "down"))),
            "binance network in the kraken step": lambda: (
                FakeFetcher(live_market(NOW), error=KlinesError(KlinesErrorCode.NETWORK, "down")),
                FakeKrakenFetcher(kraken_market(NOW)),
            ),
            "unexpected exception": lambda: (FakeFetcher(live_market(NOW)), FakeKrakenFetcher(
                kraken_market(NOW), error=RuntimeError("boom"))),
            "client start failure": factory_raises,
            "refused kraken ledger": refused,
        }

    def test_a_kraken_step_failure_is_swallowed_and_leaves_the_main_files_byte_identical(self):
        with tempfile.TemporaryDirectory() as reference:
            with mock.patch.object(config, "STATE_DIR", reference):
                ref = trend_paper_hook.run_guarded(reference, None, lambda: NOW, self.kraken_ok)
            self.assertEqual(ref.status, trend_paper_hook.CatchUpStatus.BOOKED)
            ref_main = store.ledger_path(reference).read_bytes()
            ref_alerts = alert_store.alerts_path(reference).read_bytes()
            self.assertTrue(trend_paper_hook.kraken_ledger_path(reference).exists())
        self.toast.reset_mock()
        for name, factory in self.failing().items():
            with self.subTest(failure=name):
                for p in (self.main_path, self.alerts_path, self.path):
                    p.unlink(missing_ok=True)
                with self.assertLogs("radar_v08.trend_paper", level="WARNING") as logs:
                    result = trend_paper_hook.run_guarded(self.state_dir, None, lambda: NOW, factory)
                self.assertIsNotNone(result)
                self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.BOOKED)  # the Binance step's result
                self.assertIn("Kraken EUR paper", "\n".join(logs.output))
                self.assertEqual(self.main_path.read_bytes(), ref_main)
                self.assertEqual(self.alerts_path.read_bytes(), ref_alerts)
                if name == "refused kraken ledger":
                    self.assertIn("Kraken EUR paper ledger refused (LEDGER_TORN", "\n".join(logs.output))
                    self.assertEqual(self.path.read_bytes(), b'{"torn":')
                else:
                    self.assertFalse(self.path.exists())
        self.assertEqual(self.unhandled, [])

    def test_the_kraken_step_never_touches_existing_main_files(self):
        trend_paper_hook.run_guarded(self.state_dir, None, lambda: NOW, self.kraken_ok)
        main, alerts = self.main_path.read_bytes(), self.alerts_path.read_bytes()
        self.path.unlink()
        for factory in (self.kraken_ok, *self.failing().values()):
            self.path.unlink(missing_ok=True)
            trend_paper_hook.run_kraken_guarded(self.state_dir, factory, lambda: NOW)
            self.assertEqual((self.main_path.read_bytes(), self.alerts_path.read_bytes()), (main, alerts))

    def test_a_failed_binance_step_does_not_stop_the_kraken_step(self):
        for error in (KlinesError(KlinesErrorCode.NETWORK, "down"), RuntimeError("boom")):
            with self.subTest(error=error):
                self.path.unlink(missing_ok=True)
                with self.assertLogs("radar_v08.trend_paper", level="INFO") as logs:
                    result = trend_paper_hook.run_guarded(
                        self.state_dir, lambda e=error: FakeFetcher(live_market(NOW), error=e), lambda: NOW, self.kraken_ok
                    )
                self.assertIsNone(result)
                self.assertFalse(self.main_path.exists())
                self.assertEqual(len(store.read_ledger(self.path, extend=K.extend_kraken_ledger).days), 3)
                self.assertIn("Kraken EUR paper catch-up: BOOKED", "\n".join(logs.output))
        with store.LedgerWriter(self.main_path):  # the Binance step BUSY
            self.path.unlink()
            trend_paper_hook.run_guarded(self.state_dir, None, lambda: NOW, self.kraken_ok)
        self.assertTrue(self.path.exists())

    def test_kraken_books_never_toast(self):
        trend_paper_hook.catch_up(self.state_dir, FakeFetcher(live_market(NOW)), clock=lambda: NOW)  # no alert step
        with mock.patch.object(trend_paper_hook, "alert_exposure_changes") as alert:
            result = trend_paper_hook.run_guarded(self.state_dir, None, lambda: NOW, self.kraken_ok)
        self.assertEqual(result.status, trend_paper_hook.CatchUpStatus.UP_TO_DATE)
        alert.assert_not_called()
        self.toast.assert_not_called()
        ledger = store.read_ledger(self.path, extend=K.extend_kraken_ledger)
        self.assertTrue(any(v["traded"] for r in ledger.records for v in r["assets"].values()))
        self.assertFalse(self.alerts_path.exists())

    def run_loop(self):
        with mock.patch.object(cli, "_loop_cycles", return_value=0) as cycles, \
                mock.patch.object(cli.paper_monitor, "start_monitor", return_value=None):
            started = time.monotonic()
            code = cli._run_loop()
            elapsed = time.monotonic() - started
        for thread in threading.enumerate():
            if thread.name == trend_paper_hook.THREAD_NAME:
                thread.join(30)
        return code, elapsed, cycles.call_count

    def test_the_loop_runs_the_kraken_step_and_never_sees_its_failure(self):
        with mock.patch.object(trend_paper_hook, "default_kraken_fetchers", self.kraken_ok):
            self.assertEqual(self.run_loop()[::2], (0, 1))
        self.assertEqual(len(store.read_ledger(self.path, extend=K.extend_kraken_ledger).days), 3)
        self.path.unlink()
        broken = self.failing()["kraken network"]
        with mock.patch.object(trend_paper_hook, "default_kraken_fetchers", broken), \
                self.assertLogs("radar_v08", level="WARNING") as logs:
            code, elapsed, cycles = self.run_loop()
        self.assertEqual((code, cycles), (0, 1))
        self.assertLess(elapsed, 2.0)
        self.assertIn("Kraken EUR paper catch-up failed (the radar is unaffected)", "\n".join(logs.output))
        self.assertEqual(self.unhandled, [])
        self.assertFalse(self.path.exists())

    def test_flag_off_starts_nothing(self):
        factory = mock.Mock(side_effect=self.kraken_ok)
        with mock.patch.object(config, "RADAR_TREND_PAPER_ENABLED", False), \
                mock.patch.object(trend_paper_hook, "default_kraken_fetchers", factory), \
                mock.patch.object(trend_paper_hook, "run_kraken_guarded") as kraken_step:
            self.assertEqual(self.run_loop()[::2], (0, 1))
        factory.assert_not_called()
        kraken_step.assert_not_called()
        self.assertFalse(self.path.parent.exists())


if __name__ == "__main__":
    unittest.main()
