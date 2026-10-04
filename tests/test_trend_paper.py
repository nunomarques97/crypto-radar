"""Trend paper trader: parity, accounting, ledger and the Binance boundary.

* parity: the ENS, ENS_VT and BH_5050 records reproduce the feasibility paper.py output vendored in
  ``tests/fixtures/trend/paper`` within 1e-9; the BTC_TREND5, BTC_TREND5_VT and BH_BTC targets equal
  the trend engine weights and their equity path equals the engine's units simulation (same fee,
  0 slippage) within 1e-9.
* accounting: both band semantics, fees on traded notional, EUR fills and their fallback source.
* ledger: hash chain, idempotence, gap backfill, missing candles (no partial day), torn and edited
  ledgers refused and left untouched, the exclusive lock (BUSY) and its stale-lock policy.
* adapter: only GET https://api.binance.com/api/v3/klines for the fixed symbols; foreign host,
  path, symbol or parameters refused before transport; redirects never followed; bounded retries;
  row validation; the still-open candle's close is never read.

No network: socket connections are refused for the whole module; HTTP goes to fake transports.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

import requests
from requests.adapters import BaseAdapter

from radar_v08.adapters import binance_public_klines as K
from radar_v08.adapters import trend_paper_store as store
from radar_v08.domain import trend_engine as E
from radar_v08.domain import trend_paper as P
from radar_v08.domain import trend_strategies as S
from radar_v08.trend_paper_hook import catch_up

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))
import trend_paper_fakes as F  # noqa: E402  (offline market data and a fake Binance)

FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "trend"
GOLDEN_DIR = FIXTURES / "paper"
TOLERANCE = 1e-9
GOLDEN_BUDGET_BYTES = 100 * 1024
DAY_MS = E.MS_PER_DAY

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


def usdt_bars(symbol: str, end: date) -> tuple[E.Bar, ...]:
    rows = json.loads((FIXTURES / f"spot1d_{symbol}.json").read_text(encoding="ascii"))
    return E.bars_from_rows([r for r in rows if E.utc_day(r[0]) <= end])


def golden() -> dict:
    return json.loads((GOLDEN_DIR / "paper_golden.json").read_text(encoding="ascii"))


def golden_market() -> tuple[dict[str, P.DailySeries], date, date]:
    g = golden()
    start, end = date.fromisoformat(g["start"]), date.fromisoformat(g["end"])
    market = {s: P.DailySeries(s, usdt_bars(s, end)) for s in ("BTCUSDT", "ETHUSDT")}
    for symbol, rows in json.loads((GOLDEN_DIR / "eur_rows.json").read_text(encoding="ascii"))["rows"].items():
        market[symbol] = P.DailySeries(symbol, E.bars_from_rows(rows))
    return market, start, end


def trimmed(market: dict[str, P.DailySeries], last: date) -> dict[str, P.DailySeries]:
    cut = E.day_open_ms(last)
    return {s: P.DailySeries(s, tuple(b for b in v.closed if b.open_time_ms <= cut)) for s, v in market.items()}


def without_day(series: P.DailySeries, day: date) -> P.DailySeries:
    t = E.day_open_ms(day)
    return P.DailySeries(series.symbol, tuple(b for b in series.closed if b.open_time_ms != t))


SAME_DATA = object()


def book_in_memory(market, start, ledger=P.EMPTY_LEDGER, ts="2026-01-01T00:00:00+00:00", refetch=SAME_DATA):
    """Books every due day of ``market`` in memory. By default ``market`` itself answers the second,
    confirming request (golden window: BTCEUR, ETHEUR and EURUSDT each miss one mid-series candle);
    ``refetch=None`` means no confirming request at all."""
    if refetch is SAME_DATA:
        refetch = lambda since: market  # noqa: E731
    data = bytearray()
    state = {"ledger": ledger}

    def append(chunk: bytes) -> P.Ledger:
        state["ledger"] = P.extend_ledger(state["ledger"], chunk, start)
        data.extend(chunk)
        return state["ledger"]

    days = P.catch_up_days(ledger, market, ts, append, start, refetch=refetch)
    return days, state["ledger"], bytes(data)


class FakeFetcher:
    """Serves DailySeries from a market, as the adapter would at ``now_ms``. ``second`` (a market)
    answers every later request of a symbol (a gap confirmation); ``fail_second`` makes it raise."""

    def __init__(self, market, second=None, fail_second=None):
        self.market = market
        self.second = second
        self.fail_second = fail_second
        self.calls: list[tuple[str, date]] = []

    def fetch_daily(self, symbol, since, now_ms):
        repeat = any(c[0] == symbol for c in self.calls)
        self.calls.append((symbol, since))
        if repeat and self.fail_second is not None:
            raise self.fail_second
        s = (self.second if repeat and self.second is not None else self.market)[symbol]
        bars = tuple(b for b in s.closed if b.open_time_ms >= E.day_open_ms(since))
        return P.DailySeries(symbol, bars, s.live_day, s.live_open)

    def close(self):
        pass


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.path = store.ledger_path(self.state_dir)

    def run_catch_up(self, market, start, now, **kwargs):
        return catch_up(self.state_dir, FakeFetcher(market, **kwargs), clock=lambda: now, start=start)


def at(day: date, hour: int = 6) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures and books
# ---------------------------------------------------------------------------


class GoldenFixture(unittest.TestCase):
    def test_manifest_hashes_and_budget(self):
        manifest = json.loads((GOLDEN_DIR / "MANIFEST.json").read_text(encoding="ascii"))
        on_disk = {p.name for p in GOLDEN_DIR.iterdir() if p.is_file()} - {"MANIFEST.json"}
        self.assertEqual({f["path"] for f in manifest["files"]}, on_disk)
        for entry in manifest["files"]:
            data = (GOLDEN_DIR / entry["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"], entry["path"])
            self.assertEqual(len(data), entry["bytes"])
        total = sum(p.stat().st_size for p in GOLDEN_DIR.iterdir() if p.is_file())
        self.assertLessEqual(total, GOLDEN_BUDGET_BYTES)
        self.assertEqual(manifest["paper_py_sha256"], golden()["paper_py_sha256"])


class Books(unittest.TestCase):
    def test_twenty_four_books_each_starting_at_7000(self):
        self.assertEqual(len(P.BOOKS), 24)
        self.assertEqual(len(set(P.BOOK_IDS)), 24)
        self.assertEqual({b.quote for b in P.BOOKS}, {"EUR", "USDT"})
        self.assertEqual({b.fee for b in P.BOOKS}, {0.001, 0.004})
        self.assertEqual(
            {b.rule.value for b in P.BOOKS},
            {"ENS", "ENS_VT", "BH_5050", "BTC_TREND5", "BTC_TREND5_VT", "BH_BTC"},
        )
        for book in P.BOOKS:
            state = P.initial_state(book)
            self.assertEqual(sum(s.cash for s in state.values()), 7000.0)
            self.assertTrue(all(s.units == 0 and s.held == 0 for s in state.values()))
        self.assertEqual(P.PAPER_START, date(2026, 10, 4))
        self.assertEqual(P.SLIPPAGE_BPS, 0.0)

    def test_comparators_and_identity(self):
        self.assertEqual(P.BOOK_BY_ID["EUR|ENS|0.001"].comparator, "EUR|BH_5050|0.001")
        self.assertEqual(P.BOOK_BY_ID["USDT|BTC_TREND5_VT|0.004"].comparator, "USDT|BH_BTC|0.004")
        self.assertIsNone(P.BOOK_BY_ID["USDT|BH_BTC|0.004"].comparator)
        self.assertEqual(P.RULES[P.Rule.ENS].assets, ("BTC", "ETH"))
        self.assertEqual(P.RULES[P.Rule.BTC_TREND5].assets, ("BTC",))
        self.assertEqual(P.RULES[P.Rule.BTC_TREND5].sleeves["BTC"].band, 0.15)
        self.assertEqual(P.RULES[P.Rule.BTC_TREND5_VT].sleeves["BTC"].band, 0.10)

    def test_days_before_the_start_are_never_booked(self):
        market, start, end = golden_market()
        self.assertEqual(P.due_days(P.EMPTY_LEDGER, market, start)[0], start)
        with self.assertRaises(P.PaperError) as caught:
            P.book_day(market, start - timedelta(days=1), {}, "ts", start)
        self.assertEqual(caught.exception.code, P.PaperErrorCode.LEDGER_INVALID)
        # The real start: a market that ends before 2026-10-04 has nothing due.
        live = {s: P.DailySeries(s, usdt_bars(s, date(2026, 10, 2))) for s in ("BTCUSDT", "ETHUSDT")}
        self.assertEqual(P.due_days(P.EMPTY_LEDGER, live), [])


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


class PaperPyParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        market, cls.start, cls.end = golden_market()
        cls.days, cls.ledger, _ = book_in_memory(market, cls.start)

    def test_every_golden_record_matches_within_1e9(self):
        g = golden()
        fields = g["asset_fields"]
        rounded = set(g["rounded_by_paper_py"])
        mine = {(r["date"], r["book"]): r for r in self.ledger.records}
        self.assertEqual(len(g["records"]), 16 * 12)
        worst = 0.0
        for day, book, equity, assets in g["records"]:
            record = mine[(day, book)]
            worst = max(worst, abs(record["equity"] - equity))
            # paper.py's quote, strategy and fee_rate are the parts of its book name.
            quote, rule, fee = book.split("|")
            self.assertEqual((record["quote"], record["rule"], record["fee_rate"]), (quote, rule, float(fee)))
            self.assertEqual(set(record["assets"]), set(assets))
            for asset, values in assets.items():
                ours = record["assets"][asset]
                # paper.py's signal_close_date is always the day before the fill day (signals_for).
                self.assertEqual(ours["signal_close_date"], (date.fromisoformat(day) - timedelta(days=1)).isoformat())
                for name, theirs in zip(fields, values, strict=True):
                    value = ours["signal"]["vol30"] if name == "vol30" else ours[name]
                    if name in rounded:
                        value = round(value, 6)
                    if isinstance(theirs, bool):
                        self.assertIs(value, theirs, (day, book, asset, name))
                    else:
                        worst = max(worst, abs(value - theirs))
        self.assertLessEqual(worst, TOLERANCE)

    def test_window_exercises_trades_flat_days_and_the_eur_fallback(self):
        records = {(r["date"], r["book"]): r for r in self.ledger.records}
        trades = sum(v["traded"] for r in self.ledger.records if r["rule"] == "ENS" for v in r["assets"].values())
        self.assertGreaterEqual(trades, 20)
        self.assertTrue(any(v["target"] == 0.0 and v["traded"] for r in self.ledger.records for v in r["assets"].values()))
        self.assertEqual(records[("2025-10-12", "EUR|ENS|0.001")]["assets"]["BTC"]["fill_source"], "BTCUSDT open / EURUSDT open")
        self.assertEqual(records[("2025-10-12", "EUR|ENS|0.001")]["assets"]["ETH"]["fill_source"], "ETHEUR open")
        self.assertEqual(records[("2025-10-18", "EUR|ENS|0.001")]["assets"]["ETH"]["fill_source"], "ETHUSDT open / EURUSDT open")
        self.assertEqual(records[("2025-10-20", "EUR|ENS|0.001")]["assets"]["BTC"]["fill_source"], "BTCEUR open")
        self.assertEqual(records[("2025-10-12", "USDT|ENS|0.001")]["assets"]["BTC"]["fill_source"], "BTCUSDT open")

    def test_eur_and_usdt_books_share_the_usdt_signal(self):
        for r in self.ledger.records:
            if r["quote"] == "EUR":
                twin = next(x for x in self.ledger.records if x["date"] == r["date"] and x["book"] == "USDT" + r["book"][3:])
                for asset, v in r["assets"].items():
                    self.assertEqual(v["signal"], twin["assets"][asset]["signal"])
                    self.assertEqual(v["target"], twin["assets"][asset]["target"])


def scaled_from(series: P.DailySeries, day: date, factor: float) -> P.DailySeries:
    """``series`` with the high, low and close of ``day`` and every later bar scaled by ``factor``;
    only the open of ``day`` is kept."""
    cut = E.day_open_ms(day)
    bars = []
    for b in series.closed:
        if b.open_time_ms < cut:
            bars.append(b)
        else:
            o = b.open if b.open_time_ms == cut else b.open * factor
            bars.append(E.Bar(b.open_time_ms, o, b.high * factor, b.low * factor, b.close * factor))
    return P.DailySeries(series.symbol, tuple(bars))


class PaperLookahead(unittest.TestCase):
    """Every booked record of a day D, in all 24 books, depends
    only on closes before D and the opens of D, never on D's close or any later bar."""

    def test_records_ignore_the_fill_day_close_and_later_bars_on_every_day(self):
        market, start, end = golden_market()
        _, ledger, _ = book_in_memory(market, start)
        per_day = len(P.BOOKS)
        days = [start + timedelta(days=k) for k in range((end - start).days + 1)]
        changed_next_day = 0
        for k, day in enumerate(days):
            for factor in (3.0, 0.2):
                with self.subTest(day=day, factor=factor):
                    shifted = {s: scaled_from(v, day, factor) for s, v in market.items()}
                    _, other, _ = book_in_memory(shifted, start)
                    self.assertEqual(other.records[: per_day * (k + 1)], ledger.records[: per_day * (k + 1)])
                    if k + 1 < len(days) and other.records[per_day * (k + 1)]["assets"] != ledger.records[per_day * (k + 1)]["assets"]:
                        changed_next_day += 1
        # The scaling is visible from the next day on, so a rule reading D's close would fail above.
        self.assertGreaterEqual(changed_next_day, 2 * (len(days) - 1))


class Trend5EngineParity(unittest.TestCase):
    START, END = date(2025, 10, 3), date(2026, 1, 31)

    @classmethod
    def setUpClass(cls):
        cls.bars = usdt_bars("BTCUSDT", date(2026, 10, 2))
        last = cls.END + timedelta(days=1)  # the engine marks the equity at the next open
        market = {s: P.DailySeries(s, usdt_bars(s, cls.END)) for s in ("BTCUSDT", "ETHUSDT")}
        fx = tuple(E.Bar(b.open_time_ms, 1.1, 1.1, 1.1, 1.1) for b in market["BTCUSDT"].closed)
        market["EURUSDT"] = P.DailySeries("EURUSDT", fx)
        for asset in ("BTC", "ETH"):  # EUR books are not compared; a missing EUR pair would wait
            usdt = market[f"{asset}USDT"].closed
            market[f"{asset}EUR"] = P.DailySeries(f"{asset}EUR", tuple(E.Bar(b.open_time_ms, b.open / 1.1, b.high / 1.1, b.low / 1.1, b.close / 1.1) for b in usdt))
        cls.days, cls.ledger, _ = book_in_memory(market, cls.START)
        cls.next_open = {E.utc_day(b.open_time_ms): b.open for b in cls.bars if E.utc_day(b.open_time_ms) <= last}
        cls.panel = E.Panel({"BTCUSDT": cls.bars}, ("BTCUSDT",))

    def test_targets_and_equity_path_match_the_engine(self):
        self.assertEqual(len(self.days), (self.END - self.START).days + 1)
        cases = ((P.Rule.BTC_TREND5, S.BtcTrend5()), (P.Rule.BTC_TREND5_VT, S.BtcTrend5Vt()), (P.Rule.BH_BTC, S.BuyAndHold("BTCUSDT")))
        for rule, strategy in cases:
            for fee in P.FEES:
                with self.subTest(rule=rule, fee=fee):
                    sim = E.simulate(strategy, self.panel, E.Window(self.START, self.END), fee, 0.0)
                    recs = [r for r in self.ledger.records if r["book"] == P.book_id("USDT", rule, fee)]
                    self.assertEqual([date.fromisoformat(r["date"]) for r in recs], list(sim.days))
                    for r, target in zip(recs, sim.targets, strict=True):
                        self.assertEqual(r["assets"]["BTC"]["target"], target.get("BTCUSDT", 0.0))
                    for k, r in enumerate(recs):
                        self.assertLessEqual(abs(r["equity_before"] / P.CAPITAL - sim.equity[k]), TOLERANCE)
                        nxt = self.next_open[date.fromisoformat(r["date"]) + timedelta(days=1)]
                        marked = r["cash"] + r["assets"]["BTC"]["units_after"] * nxt
                        self.assertLessEqual(abs(marked / P.CAPITAL - sim.equity[k + 1]), TOLERANCE)
                    self.assertEqual(sum(r["assets"]["BTC"]["traded"] for r in recs), sim.trades)
        trades = sum(r["assets"]["BTC"]["traded"] for r in self.ledger.records if r["book"] == "USDT|BTC_TREND5|0.001")
        self.assertGreater(trades, 3)

    def test_signals_use_the_full_history_and_registered_identity(self):
        r = next(x for x in self.ledger.records if x["book"] == "USDT|BTC_TREND5_VT|0.001")
        btc = r["assets"]["BTC"]
        self.assertEqual(btc["registered_name"], "btc_trend5_vt")
        self.assertEqual(btc["registered_sha256"], S.REGISTERED["btc_trend5_vt"].sha256)
        self.assertEqual(btc["signal"]["BTC_TREND5_VT"], btc["signal"]["votes"] * btc["signal"]["vol_scale"])
        self.assertEqual(btc["signal_close_date"], (self.START - timedelta(days=1)).isoformat())
        ens = next(x for x in self.ledger.records if x["book"] == "USDT|ENS|0.001")["assets"]
        self.assertEqual(ens["BTC"]["registered_name"], "ens_btc")
        self.assertIsNone(ens["ETH"]["registered_name"])
        market = {"BTCUSDT": P.DailySeries("BTCUSDT", self.bars[1:])}
        with self.assertRaises(P.PaperError) as caught:
            P.check_btc_history(market)
        self.assertEqual(caught.exception.code, P.PaperErrorCode.INCOMPLETE_HISTORY)
        gap = {"BTCUSDT": without_day(P.DailySeries("BTCUSDT", self.bars), date(2020, 3, 1))}
        with self.assertRaises(P.PaperError) as caught:
            P.check_btc_history(gap)
        self.assertEqual(caught.exception.code, P.PaperErrorCode.MISSING_CANDLE)


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


class Accounting(unittest.TestCase):
    def test_fraction_band_ignores_drift_and_charges_fee_on_notional(self):
        s = P.Sleeve(3500.0, 0.0, 0.0)
        fill = P.step_fraction(s, 100.0, 0.6, 0.001, buy_and_hold=False, first_day=False)
        self.assertTrue(fill.traded)
        self.assertAlmostEqual(fill.notional, 2100.0)
        self.assertAlmostEqual(fill.fee, 2.1)
        self.assertAlmostEqual(fill.sleeve.units, 21.0)
        self.assertAlmostEqual(fill.sleeve.cash, 3500.0 - 2100.0 - 2.1)
        self.assertEqual(fill.sleeve.held, 0.6)
        # Price doubles: the drifted weight is far from 0.6, but the band compares the last fraction.
        later = P.step_fraction(fill.sleeve, 200.0, 0.45, 0.001, buy_and_hold=False, first_day=False)
        self.assertFalse(later.traded)
        self.assertEqual(later.sleeve, fill.sleeve)
        flat = P.step_fraction(fill.sleeve, 200.0, 0.0, 0.004, buy_and_hold=False, first_day=False)
        self.assertTrue(flat.traded)
        self.assertEqual(flat.sleeve.units, 0.0)
        self.assertAlmostEqual(flat.fee, 21.0 * 200.0 * 0.004)

    def test_buy_and_hold_buys_once(self):
        s = P.Sleeve(3500.0, 0.0, 0.0)
        first = P.step_fraction(s, 50.0, 0.0, 0.004, buy_and_hold=True, first_day=True)
        self.assertTrue(first.traded)
        self.assertEqual(first.sleeve.units, 70.0)
        self.assertAlmostEqual(first.sleeve.cash, -14.0)  # paper.py: the entry fee is owed in cash
        again = P.step_fraction(first.sleeve, 80.0, 0.0, 0.004, buy_and_hold=True, first_day=False)
        self.assertFalse(again.traded)
        self.assertEqual(again.target, 1.0)

    def test_units_band_reacts_to_drift_and_never_borrows_fees(self):
        s = P.Sleeve(7000.0, 0.0, 0.0)
        buy = P.step_units(s, 100.0, 1.0, 0.15, 0.001)
        self.assertTrue(buy.traded)
        self.assertGreaterEqual(buy.sleeve.cash, -1e-9)
        self.assertAlmostEqual(buy.sleeve.units * 100.0 + buy.fee, 7000.0)
        self.assertAlmostEqual(buy.fee, buy.notional * 0.001)
        # Target 0.8 against a drifted weight of 1.0: |0.8 - 1.0| >= 0.15 trades.
        sell = P.step_units(buy.sleeve, 100.0, 0.8, 0.15, 0.001)
        self.assertTrue(sell.traded)
        self.assertAlmostEqual(sell.held_before, 1.0, places=9)
        self.assertAlmostEqual(sell.sleeve.held, 0.8, delta=0.001)
        # Inside the band: no trade.
        hold = P.step_units(sell.sleeve, 100.0, 0.9, 0.15, 0.001)
        self.assertFalse(hold.traded)
        # Drift alone moves the weight out of the band: it trades back.
        drift = P.step_units(sell.sleeve, 1000.0, 0.8, 0.15, 0.001)
        self.assertGreater(drift.held_before, 0.95)
        self.assertTrue(drift.traded)
        # Target 0 sells everything.
        out = P.step_units(drift.sleeve, 1000.0, 0.0, 0.15, 0.001)
        self.assertEqual(out.sleeve.units, 0.0)
        self.assertFalse(P.step_units(out.sleeve, 1000.0, 0.0, 0.15, 0.001).traded)

    def test_fill_price_sources(self):
        day = date(2026, 1, 5)
        t = E.day_open_ms(day)

        def series(symbol, price):
            return P.DailySeries(symbol, (E.Bar(t, price, price, price, price),))

        market = {"BTCUSDT": series("BTCUSDT", 110.0), "BTCEUR": series("BTCEUR", 95.0), "EURUSDT": series("EURUSDT", 1.1)}
        self.assertEqual(P.fill_price(market, "BTC", "USDT", day), (110.0, "BTCUSDT open"))
        self.assertEqual(P.fill_price(market, "BTC", "EUR", day), (95.0, "BTCEUR open"))
        del market["BTCEUR"]
        self.assertEqual(P.fill_price(market, "BTC", "EUR", day), (110.0 / 1.1, "BTCUSDT open / EURUSDT open"))
        del market["EURUSDT"]
        with self.assertRaises(P.PaperError) as caught:
            P.fill_price(market, "BTC", "EUR", day)
        self.assertEqual(caught.exception.code, P.PaperErrorCode.MISSING_CANDLE)

    def test_the_live_candle_contributes_its_open_only(self):
        bars = usdt_bars("BTCUSDT", date(2026, 10, 2))
        series = P.DailySeries("BTCUSDT", bars, date(2026, 10, 3), 84518.0)
        self.assertEqual(series.open_on(date(2026, 10, 3)), 84518.0)
        self.assertIsNone(series.close_on(date(2026, 10, 3)))
        self.assertEqual(series.last_open_day, date(2026, 10, 3))
        with self.assertRaises(P.PaperError):
            P.DailySeries("BTCUSDT", bars, date(2026, 10, 2), 1.0)  # live day overlaps a closed bar
        with self.assertRaises(P.PaperError):
            P.DailySeries("BTCUSDT", bars, date(2026, 10, 3), None)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class LedgerChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        market, cls.start, end = golden_market()
        cls.market = trimmed(market, cls.start + timedelta(days=2))
        _, cls.ledger, cls.data = book_in_memory(cls.market, cls.start)

    def lines(self):
        return self.data.split(b"\n")[:-1]

    def test_chain_and_schema(self):
        self.assertEqual(len(self.ledger.records), 3 * 24)
        self.assertEqual(self.ledger.days, tuple(self.start + timedelta(days=k) for k in range(3)))
        first = self.ledger.records[0]
        self.assertEqual((first["schema"], first["seq"], first["prev"]), (1, 0, "genesis"))
        for prev, record in zip(self.ledger.records, self.ledger.records[1:], strict=False):
            self.assertEqual(record["prev"], prev["sha256"])
        for name in ("ts", "date", "book", "rule", "fee_rate", "equity", "cash"):
            self.assertIn(name, first)
        for name in (
            "signal_close_date", "signal_close_usdt", "signal", "fill_price", "fill_source", "target", "held_before",
            "held_after", "traded_notional", "fee", "cash_after", "units_after", "registered_sha256",
        ):
            self.assertIn(name, first["assets"]["BTC"])
        self.assertEqual(P.parse_ledger(self.data, self.start).tip, self.ledger.tip)

    def assert_refused(self, data, code):
        with self.assertRaises(P.PaperError) as caught:
            P.parse_ledger(data, self.start)
        self.assertEqual(caught.exception.code, code)

    def test_torn_ledgers_are_refused(self):
        self.assert_refused(self.data[:-1], P.PaperErrorCode.LEDGER_TORN)
        self.assert_refused(self.data[:-200] + b"\n", P.PaperErrorCode.LEDGER_TORN)
        self.assert_refused(self.data + b'{"half":', P.PaperErrorCode.LEDGER_TORN)

    def test_edited_ledgers_are_refused(self):
        lines = self.lines()
        edited = lines[5].replace(b'"equity":', b'"equity":1', 1)
        self.assert_refused(b"\n".join(lines[:5] + [edited] + lines[6:]) + b"\n", P.PaperErrorCode.LEDGER_EDITED)
        last = lines[-1].replace(b'"cash":', b'"cash":9', 1)
        self.assert_refused(b"\n".join(lines[:-1] + [last]) + b"\n", P.PaperErrorCode.LEDGER_EDITED)
        spaced = json.dumps(json.loads(lines[0]), sort_keys=True).encode()
        self.assert_refused(b"\n".join([spaced] + lines[1:]) + b"\n", P.PaperErrorCode.LEDGER_EDITED)
        swapped = [lines[1], lines[0]] + lines[2:]
        self.assert_refused(b"\n".join(swapped) + b"\n", P.PaperErrorCode.LEDGER_EDITED)
        self.assert_refused(b"\n".join(lines[24:]) + b"\n", P.PaperErrorCode.LEDGER_EDITED)  # first day removed
        self.assert_refused(b"\n".join(lines[:-1]) + b"\n", P.PaperErrorCode.LEDGER_INVALID)  # incomplete day

    def test_edits_that_break_the_json_encoder_are_refused_as_edited(self):
        lines = self.lines()
        overflow = re.sub(rb'"fee_rate":[^,}]+', b'"fee_rate":1e400', lines[3], count=1)  # parses as inf
        self.assertNotEqual(overflow, lines[3])
        self.assert_refused(b"\n".join(lines[:3] + [overflow] + lines[4:]) + b"\n", P.PaperErrorCode.LEDGER_EDITED)
        nested = b"[" * 100_000 + b"]" * 100_000
        self.assert_refused(b"\n".join([nested] + lines[1:]) + b"\n", P.PaperErrorCode.LEDGER_TORN)

    def test_a_record_cannot_set_chain_fields(self):
        with self.assertRaises(P.PaperError):
            P.encode_day(P.EMPTY_LEDGER, [{"seq": 9}])


class LedgerStore(TempState):
    @classmethod
    def setUpClass(cls):
        cls.full, cls.start, cls.end = golden_market()

    def market_until(self, last):
        return trimmed(self.full, last)

    def test_idempotent_rerun_and_gap_backfill(self):
        d2 = self.start + timedelta(days=1)
        first = self.run_catch_up(self.market_until(d2), self.start, at(d2))
        self.assertEqual(first.booked, (self.start, d2))
        self.assertEqual(first.records_appended, 48)
        before = self.path.read_bytes()
        again = self.run_catch_up(self.market_until(d2), self.start, at(d2, 9))
        self.assertEqual((again.booked, again.records_appended), ((), 0))
        self.assertEqual(self.path.read_bytes(), before)
        # After an absence, every missed day is booked in order.
        last = self.start + timedelta(days=6)
        later = self.run_catch_up(self.market_until(last), self.start, at(last))
        self.assertEqual(later.booked, tuple(self.start + timedelta(days=k) for k in range(2, 7)))
        ledger = store.read_ledger(self.path, self.start)
        self.assertEqual(len(ledger.records), 7 * 24)
        self.assertEqual(len({(r["date"], r["book"]) for r in ledger.records}), 7 * 24)
        # Same numbers as booking all seven days in one pass.
        _, one_pass, _ = book_in_memory(self.market_until(last), self.start)
        strip = ("ts", "sha256", "prev")
        self.assertEqual(
            [{k: v for k, v in r.items() if k not in strip} for r in ledger.records],
            [{k: v for k, v in r.items() if k not in strip} for r in one_pass.records],
        )

    def test_missing_candle_books_no_partial_day(self):
        # A missing EUR price with a later candle, confirmed by a
        # second request, is one skipped day; the other days are booked whole.
        last = self.start + timedelta(days=3)  # before the golden BTCEUR gap on day 4
        broken_day = self.start + timedelta(days=2)  # BTCEUR trades again after it
        market = self.market_until(last)
        market["BTCEUR"] = without_day(market["BTCEUR"], broken_day)
        market["EURUSDT"] = without_day(market["EURUSDT"], broken_day)
        fetcher = FakeFetcher(market)
        result = catch_up(self.state_dir, fetcher, clock=lambda: at(last), start=self.start)
        days = tuple(self.start + timedelta(days=k) for k in range(4))
        self.assertEqual(result.booked, days[:2] + days[3:])
        self.assertEqual(result.skipped, (broken_day,))
        self.assertEqual(result.records_appended, 3 * 24)
        ledger = store.read_ledger(self.path, self.start)
        self.assertEqual(ledger.days, days[:2] + days[3:])
        self.assertEqual(len(ledger.records), 3 * 24)
        (skip,) = ledger.skipped
        self.assertEqual((skip.day, skip.reason, skip.gap_day), (broken_day, P.SkipReason.NO_EUR_PRICE, broken_day))
        self.assertTrue({"BTCEUR", "EURUSDT"} <= set(skip.missing))
        # The confirmation is a second, separate request per missing symbol, from the gap day.
        self.assertEqual(sorted(c for c in fetcher.calls if c[1] == broken_day), [("BTCEUR", broken_day), ("EURUSDT", broken_day)])
        self.assertEqual(len(fetcher.calls), len(P.ALL_SYMBOLS) + 2)

    def test_torn_or_edited_ledger_is_refused_and_never_rewritten(self):
        d2 = self.start + timedelta(days=1)
        self.run_catch_up(self.market_until(d2), self.start, at(d2))
        good = self.path.read_bytes()
        last = self.start + timedelta(days=4)
        for damaged in (good[:-10], good.replace(b'"fee_rate":0.001', b'"fee_rate":0.002', 1)):
            self.path.write_bytes(damaged)
            with self.assertRaises(P.PaperError):
                self.run_catch_up(self.market_until(last), self.start, at(last))
            self.assertEqual(self.path.read_bytes(), damaged)

    def test_second_writer_is_busy_and_writes_nothing(self):
        last = self.start + timedelta(days=2)
        fetcher = FakeFetcher(self.market_until(last))
        with store.LedgerWriter(self.path, self.start):
            with self.assertRaises(store.TrendPaperStoreError) as caught:
                catch_up(self.state_dir, fetcher, clock=lambda: at(last), start=self.start)
        self.assertEqual(caught.exception.code, store.TrendPaperStoreErrorCode.BUSY)
        self.assertEqual(fetcher.calls, [])
        self.assertFalse(self.path.exists())

    def test_stale_lock_policy(self):
        last = self.start + timedelta(days=1)
        # A lock file left behind by a crash is not a lock.
        self.path.parent.mkdir(parents=True)
        self.path.with_name(self.path.name + ".lock").write_bytes(b"left by a crashed run")
        self.assertEqual(len(self.run_catch_up(self.market_until(last), self.start, at(last)).booked), 2)
        # A live holder in another process makes the run BUSY; once that process dies (here it is
        # killed while holding the lock) the operating system releases the lock.
        code = (
            "import sys; from pathlib import Path; from radar_v08.adapters import trend_paper_store as s;"
            "w = s.LedgerWriter(Path(sys.argv[1])); w.__enter__(); print('locked', flush=True); sys.stdin.read()"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", code, str(self.path)], cwd=REPOSITORY_ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            later = self.start + timedelta(days=3)
            with self.assertRaises(store.TrendPaperStoreError) as caught:
                self.run_catch_up(self.market_until(later), self.start, at(later))
            self.assertEqual(caught.exception.code, store.TrendPaperStoreErrorCode.BUSY)
        finally:
            holder.kill()
            holder.wait(timeout=30)
            holder.stdin.close()
            holder.stdout.close()
        result = self.run_catch_up(self.market_until(later), self.start, at(later))
        self.assertEqual(len(result.booked), 2)

    def test_failed_write_is_undone_and_reported(self):
        last = self.start + timedelta(days=1)
        self.run_catch_up(self.market_until(self.start), self.start, at(self.start))
        good = self.path.read_bytes()
        real_write = os.write
        calls = {"n": 0}

        def failing_write(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd, bytes(data[:100]))
            raise OSError(28, "No space left on device")

        with mock.patch.object(store.os, "write", failing_write):
            with self.assertRaises(store.TrendPaperStoreError) as caught:
                self.run_catch_up(self.market_until(last), self.start, at(last))
        self.assertEqual(caught.exception.code, store.TrendPaperStoreErrorCode.WRITE_FAILED)
        self.assertEqual(self.path.read_bytes(), good)
        self.assertEqual(len(self.run_catch_up(self.market_until(last), self.start, at(last)).booked), 1)

    def test_append_refuses_a_ledger_changed_under_it(self):
        self.run_catch_up(self.market_until(self.start), self.start, at(self.start))
        with store.LedgerWriter(self.path, self.start) as writer:
            ledger = writer.read()
            records, _ = P.book_day(self.full, self.start + timedelta(days=1), ledger.states, "ts", self.start)
            data = P.encode_day(ledger, records)
            with self.path.open("ab") as handle:
                handle.write(b"x")
            with self.assertRaises(store.TrendPaperStoreError) as caught:
                writer.append(data, ledger)
        self.assertEqual(caught.exception.code, store.TrendPaperStoreErrorCode.CHANGED_DURING_APPEND)

    def test_ledger_lives_under_the_state_dir_only(self):
        self.assertEqual(store.ledger_path(self.state_dir), self.state_dir / "trend_paper" / "ledger.jsonl")
        self.run_catch_up(self.market_until(self.start), self.start, at(self.start))
        names = {p.relative_to(self.state_dir).as_posix() for p in self.state_dir.rglob("*")}
        self.assertEqual(names, {"trend_paper", "trend_paper/ledger.jsonl", "trend_paper/ledger.jsonl.lock"})


# ---------------------------------------------------------------------------
# Binance public klines adapter
# ---------------------------------------------------------------------------


NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
NOW_MS = E.day_open_ms(NOW.date()) + 12 * 3_600_000


def kline(day: date, o="1.0", h="2.0", low="0.5", c="1.5", close_time=None):
    t = E.day_open_ms(day)
    return [t, o, h, low, c, "10.0", t + DAY_MS - 1 if close_time is None else close_time, "0", 1, "0", "0", "0"]


class FakeResponse:
    def __init__(self, status=200, body=b"[]", headers=None, history=()):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.history = list(history)
        self.closed = False

    def iter_content(self, chunk_size=1):
        for k in range(0, len(self._body), chunk_size):
            yield self._body[k : k + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    """A requests.Session stand-in that records every request and replays responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True
        self.auth = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def client(responses, **kwargs):
    session = FakeSession(responses)
    sleeps = []
    k = K.BinancePublicKlines(session=session, sleep=sleeps.append, **kwargs)
    return k, session, sleeps


def body(rows):
    return json.dumps(rows).encode()


class AdapterBoundary(unittest.TestCase):
    def test_allowed_request_shape(self):
        k, session, _ = client([FakeResponse(body=body([kline(date(2026, 10, 1)), kline(date(2026, 10, 2))]))])
        series = k.fetch_daily("BTCEUR", date(2026, 10, 1), NOW_MS)
        self.assertEqual(len(session.calls), 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("GET", "https://api.binance.com/api/v3/klines"))
        self.assertEqual(
            kwargs["params"], {"symbol": "BTCEUR", "interval": "1d", "startTime": E.day_open_ms(date(2026, 10, 1)), "limit": 1000}
        )
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"], K.DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(set(kwargs["headers"]), {"Accept"})
        self.assertIs(session.trust_env, False)
        self.assertIsNone(session.auth)
        self.assertEqual(len(series.closed), 2)
        self.assertIsNone(series.live_day)

    def test_foreign_host_path_symbol_or_parameters_are_refused_before_transport(self):
        good = {"symbol": "BTCUSDT", "interval": "1d", "startTime": 0, "limit": 1000}
        urls = (
            "https://api.binance.com.evil.test/api/v3/klines",
            "https://evil.test/api/v3/klines",
            "https://api.binance.us/api/v3/klines",
            "https://user@api.binance.com/api/v3/klines",
            "https://api.binance.com:443/api/v3/klines",
            "http://api.binance.com/api/v3/klines",
            "https://api.binance.com/api/v3/account",
            "https://api.binance.com/sapi/v1/capital/config/getall",
            "https://api.binance.com/api/v3/order",
            "https://api.binance.com/api/v3/klines/",
            "https://api.binance.com/api/v3/klines?symbol=BTCUSDT",
            "https://API.binance.com/api/v3/klines",
            "https://api.kraken.com/0/public/OHLC",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(K.KlinesError) as caught:
                K.assert_allowed_request("GET", url, good)
            self.assertEqual(caught.exception.code, K.KlinesErrorCode.REFUSED)
        for method in ("POST", "PUT", "DELETE", "HEAD", "get"):
            with self.subTest(method=method), self.assertRaises(K.KlinesError):
                K.assert_allowed_request(method, K.KLINES_URL, good)
        bad_params = (
            {**good, "symbol": "BNBUSDT"},
            {**good, "symbol": "btcusdt"},
            {**good, "symbol": "BTCUSDT&signature=x"},
            {**good, "interval": "4h"},
            {**good, "limit": 1001},
            {**good, "limit": True},
            {**good, "startTime": -86_400_000},
            {**good, "startTime": 5},
            {**good, "timestamp": 1},
            {**good, "signature": "x"},
            {"symbol": "BTCUSDT"},
        )
        for params in bad_params:
            with self.subTest(params=params), self.assertRaises(K.KlinesError):
                K.assert_allowed_request("GET", K.KLINES_URL, params)
        k, session, _ = client([])
        for symbol in ("BNBUSDT", "BTCUSDC", "", "BTCUSDT "):
            with self.subTest(symbol=symbol), self.assertRaises(K.KlinesError) as caught:
                k.fetch_daily(symbol, date(2026, 10, 1), NOW_MS)
            self.assertEqual(caught.exception.code, K.KlinesErrorCode.REFUSED)
        self.assertEqual(session.calls, [])

    def test_the_guard_sits_on_the_transport_path(self):
        for url in ("https://evil.test/api/v3/klines", "https://api.binance.com/api/v3/order",
                    "https://api.binance.com:8443/api/v3/klines", "http://api.binance.com/api/v3/klines"):
            k, session, _ = client([FakeResponse(body=b"[]")])
            with self.subTest(url=url), mock.patch.object(K, "KLINES_URL", url):
                with self.assertRaises(K.KlinesError) as caught:
                    k.fetch_daily("BTCUSDT", date(2026, 10, 1), NOW_MS)
                self.assertEqual(caught.exception.code, K.KlinesErrorCode.REFUSED)
            self.assertEqual(session.calls, [])

    def test_redirects_are_refused_and_never_followed(self):
        for status in (301, 302, 303, 307, 308):
            k, session, sleeps = client([FakeResponse(status, headers={"Location": "https://evil.test/x"})])
            with self.subTest(status=status), self.assertRaises(K.KlinesError) as caught:
                k.fetch_daily("BTCUSDT", date(2026, 10, 1), NOW_MS)
            self.assertEqual(caught.exception.code, K.KlinesErrorCode.REDIRECT)
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(sleeps, [])
        k, session, _ = client([FakeResponse(200, body=b"[]", history=[object()])])
        with self.assertRaises(K.KlinesError) as caught:
            k.fetch_daily("BTCUSDT", date(2026, 10, 1), NOW_MS)
        self.assertEqual(caught.exception.code, K.KlinesErrorCode.REDIRECT)

    def test_a_real_session_sends_one_request_for_a_redirect(self):
        class Adapter(BaseAdapter):
            def __init__(self):
                super().__init__()
                self.sent = []

            def send(self, request, **kwargs):
                self.sent.append((request.method, request.url, dict(request.headers), kwargs))
                response = requests.Response()
                response.status_code = 302
                response.headers["Location"] = "https://evil.test/steal"
                response.url = request.url
                response.request = request
                response.raw = io.BytesIO(b"")
                return response

            def close(self):
                pass

        adapter = Adapter()
        session = requests.Session()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.evil.test:8080", "NETRC": "x"}):
            k = K.BinancePublicKlines(session=session, sleep=lambda s: None)
            with self.assertRaises(K.KlinesError) as caught:
                k.fetch_daily("ETHEUR", date(2026, 10, 1), NOW_MS)
        self.assertEqual(caught.exception.code, K.KlinesErrorCode.REDIRECT)
        self.assertEqual(len(adapter.sent), 1)
        method, url, headers, kwargs = adapter.sent[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.startswith("https://api.binance.com/api/v3/klines?symbol=ETHEUR&interval=1d&startTime="))
        self.assertNotIn("Authorization", headers)
        self.assertFalse(any(h.lower().startswith("x-mbx") for h in headers))
        self.assertFalse(kwargs.get("proxies"))

    def test_retries_are_bounded(self):
        k, session, sleeps = client(
            [FakeResponse(429), FakeResponse(503), requests.ConnectionError("down")], max_retries=2, backoff_seconds=0.5
        )
        with self.assertRaises(K.KlinesError) as caught:
            k.fetch_daily("BTCUSDT", date(2026, 10, 1), NOW_MS)
        self.assertEqual(caught.exception.code, K.KlinesErrorCode.NETWORK)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        k, session, sleeps = client([FakeResponse(500), FakeResponse(body=body([kline(date(2026, 10, 1))]))])
        self.assertEqual(len(k.fetch_daily("BTCUSDT", date(2026, 10, 1), NOW_MS).closed), 1)
        for status, code in ((400, "HTTP_ERROR"), (403, "HTTP_ERROR"), (404, "HTTP_ERROR"), (418, "BANNED")):
            k, session, sleeps = client([FakeResponse(status)])
            with self.subTest(status=status), self.assertRaises(K.KlinesError) as caught:
                k.fetch_daily("BTCUSDT", date(2026, 10, 1), NOW_MS)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(sleeps, [])

    def test_deadline_and_size_caps(self):
        clock = iter([0.0, 0.0, 1000.0])
        k, session, _ = client([FakeResponse(503), FakeResponse(503)], deadline_seconds=10, monotonic=lambda: next(clock))
        with self.assertRaises(K.KlinesError) as caught:
            k.fetch_daily("BTCUSDT", date(2026, 10, 1), NOW_MS)
        self.assertEqual(caught.exception.code, K.KlinesErrorCode.DEADLINE)
        self.assertEqual(len(session.calls), 1)
        k, _, _ = client([FakeResponse(body=b"[" + b" " * (K.MAX_RESPONSE_BYTES + 1) + b"]")])
        with self.assertRaises(K.KlinesError) as caught:
            k.fetch_daily("BTCUSDT", date(2026, 10, 1), NOW_MS)
        self.assertEqual(caught.exception.code, K.KlinesErrorCode.TOO_LARGE)

    def test_paging_follows_the_last_open_time(self):
        first = [kline(date(2020, 1, 1) + timedelta(days=k)) for k in range(1000)]
        second = [kline(date(2020, 1, 1) + timedelta(days=1000 + k)) for k in range(3)]
        k, session, _ = client([FakeResponse(body=body(first)), FakeResponse(body=body(second))])
        series = k.fetch_daily("BTCUSDT", date(2020, 1, 1), NOW_MS)
        self.assertEqual(len(series.closed), 1003)
        self.assertEqual(session.calls[1][2]["params"]["startTime"], first[-1][0] + DAY_MS)
        k, session, _ = client([FakeResponse(body=body(first)), FakeResponse(body=body(first[-1:] + second))])
        with self.assertRaises(K.KlinesError) as caught:
            k.fetch_daily("BTCUSDT", date(2020, 1, 1), NOW_MS)
        self.assertEqual(caught.exception.code, K.KlinesErrorCode.DUPLICATE)

    def test_invalid_rows_raise_typed_failures(self):
        d1, d2 = date(2026, 9, 30), date(2026, 10, 1)
        cases = {
            K.KlinesErrorCode.MALFORMED: [
                {"not": "a list"},
                [[1, 2]],
                [kline(d1)[:5] + ["x", "y"]],
                [[E.day_open_ms(d1) + 5] + kline(d1)[1:]],
                [kline(d1, o="abc")],
                [kline(d1, c="-1")],
                [kline(d1, o=True)],
                [kline(d1, close_time=E.day_open_ms(d1) + DAY_MS)],
                [kline(date(2026, 10, 3)), kline(date(2026, 10, 2))],
            ],
            K.KlinesErrorCode.CLOCK_SKEW: [
                [kline(date(2026, 10, 4))],
                [kline(d1), kline(date(2026, 10, 4))],
                [kline(date(2026, 10, 3)), kline(date(2026, 10, 4))],  # after the candle still open locally
            ],
            K.KlinesErrorCode.NON_FINITE: [[kline(d1, h="NaN")], [kline(d1, c="inf")], [kline(d1, low="-Infinity")]],
            K.KlinesErrorCode.DUPLICATE: [[kline(d1), kline(d1)]],
            K.KlinesErrorCode.NON_MONOTONIC: [[kline(d2), kline(d1)]],
        }
        for code, payloads in cases.items():
            for payload in payloads:
                with self.subTest(code=code, payload=str(payload)[:60]):
                    k, _, _ = client([FakeResponse(body=body(payload))])
                    with self.assertRaises(K.KlinesError) as caught:
                        k.fetch_daily("BTCUSDT", d1, NOW_MS)
                    self.assertEqual(caught.exception.code, code)
        k, _, _ = client([FakeResponse(body=b"[[1, NaN]]")])
        with self.assertRaises(K.KlinesError) as caught:
            k.fetch_daily("BTCUSDT", d1, NOW_MS)
        self.assertEqual(caught.exception.code, K.KlinesErrorCode.NON_FINITE)

    def test_the_still_open_candle_close_is_never_read(self):
        today = NOW.date()
        rows = [kline(today - timedelta(days=1)), kline(today, o="84518.0", h="garbage", low="x", c="NaN")]
        k, _, _ = client([FakeResponse(body=body(rows))])
        series = k.fetch_daily("BTCUSDT", today - timedelta(days=1), NOW_MS)
        self.assertEqual(len(series.closed), 1)
        self.assertEqual((series.live_day, series.live_open), (today, 84518.0))
        self.assertIsNone(series.close_on(today))
        # An outage-shortened day (BTCUSDT 2018-02-08) is still a closed candle.
        short = kline(date(2018, 2, 8), close_time=E.day_open_ms(date(2018, 2, 8)) + 1_694_788)
        k, _, _ = client([FakeResponse(body=body([short]))])
        self.assertEqual(len(k.fetch_daily("BTCUSDT", date(2018, 2, 8), NOW_MS).closed), 1)

    def test_the_radar_allowlist_is_not_widened(self):
        from radar_v08 import config

        self.assertEqual(set(config.HTTP_PUBLIC_ALLOWLIST), {"api.kraken.com", "futures.kraken.com"})
        self.assertFalse(any("binance" in host for host in config.HTTP_PUBLIC_ALLOWLIST))
        self.assertEqual(K.ALLOWED_SYMBOLS, {"BTCUSDT", "ETHUSDT", "BTCEUR", "ETHEUR", "EURUSDT"})

    def test_module_reads_no_environment_or_credentials(self):
        source = Path(K.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        self.assertNotIn("os", names | imported)
        self.assertFalse({"environ", "getenv"} & attrs)
        for word in ("api_key", "API-Key", "X-MBX-APIKEY", "secret", os.extsep + "env", "signature"):
            self.assertNotIn(word.lower(), source.lower().replace("no credential", ""))


class GapPolicy(TempState):
    """Data-gap policy: EUR fallback, confirmed skips, no imputation and
    transient absences that write nothing."""

    @classmethod
    def setUpClass(cls):
        cls.full, cls.start, cls.end = golden_market()

    def day(self, k):
        return self.start + timedelta(days=k)

    def lines(self):
        return self.path.read_bytes().split(b"\n")[:-1]

    def test_missing_eur_pair_falls_back_to_eurusdt_and_is_not_skipped(self):
        market = trimmed(self.full, self.day(2))
        market["BTCEUR"] = without_day(market["BTCEUR"], self.day(1))
        result = self.run_catch_up(market, self.start, at(self.day(2)))
        self.assertEqual((len(result.booked), result.skipped), (3, ()))
        record = next(r for r in result.ledger.records if r["date"] == self.day(1).isoformat() and r["quote"] == "EUR")
        self.assertEqual(record["assets"]["BTC"]["fill_source"], "BTCUSDT open / EURUSDT open")
        usdt = market["BTCUSDT"].open_on(self.day(1))
        self.assertEqual(record["assets"]["BTC"]["fill_price"], usdt / market["EURUSDT"].open_on(self.day(1)))

    def test_eur_fallback_is_confirmed_by_a_second_request_of_the_eur_pair(self):
        market = trimmed(self.full, self.day(2))
        market["BTCEUR"] = without_day(market["BTCEUR"], self.day(1))
        fetcher = FakeFetcher(market)
        result = catch_up(self.state_dir, fetcher, clock=lambda: at(self.day(2)), start=self.start)
        self.assertEqual(result.status.value, "BOOKED")
        self.assertEqual([c for c in fetcher.calls if c[1] == self.day(1)], [("BTCEUR", self.day(1))])
        self.assertEqual(len(fetcher.calls), len(P.ALL_SYMBOLS) + 1)

    def test_eur_pair_that_ends_early_waits_instead_of_falling_back(self):
        # Review probe: an empty or short EUR pair page, or a live EUR candle not yet published
        # just after 00:00 UTC, must not book the EURUSDT fallback for good.
        full = trimmed(self.full, self.day(2))
        cases = {
            "empty page": P.DailySeries("BTCEUR", ()),
            "short page": P.DailySeries("BTCEUR", tuple(b for b in full["BTCEUR"].closed
                                                        if E.utc_day(b.open_time_ms) < self.day(1))),
            "today not published": without_day(full["BTCEUR"], self.day(2)),
        }
        _, _, reference = book_in_memory(full, self.start)
        for name, series in cases.items():
            with self.subTest(case=name):
                self.path.unlink(missing_ok=True)
                market = {**full, "BTCEUR": series}
                fetcher = FakeFetcher(market)
                result = catch_up(self.state_dir, fetcher, clock=lambda: at(self.day(2), 0), start=self.start)
                self.assertEqual((result.status.value, result.booked, result.records_appended),
                                 ("WAITING_FOR_DATA", (), 0))
                self.assertIn("BTCEUR", result.detail)
                self.assertFalse(self.path.exists())
                self.assertEqual(len(fetcher.calls), len(P.ALL_SYMBOLS))  # no confirmation of a transient absence
                later = self.run_catch_up(full, self.start, at(self.day(2), 9))
                self.assertEqual(len(later.booked), 3)
                self.assertEqual(F.strip_ts(self.path.read_bytes()), F.strip_ts(reference))
                self.assertNotIn(b"/ EURUSDT open", self.path.read_bytes())

    def test_eur_fallback_seen_in_one_response_only_writes_nothing(self):
        market = trimmed(self.full, self.day(3))
        broken = {**market, "ETHEUR": without_day(market["ETHEUR"], self.day(1))}
        result = self.run_catch_up(broken, self.start, at(self.day(3)), second=market)
        self.assertEqual(result.status.value, "WAITING_FOR_DATA")
        self.assertIn("disagree on ETHEUR", result.detail)
        self.assertFalse(self.path.exists())
        with self.assertRaises(K.KlinesError):
            self.run_catch_up(broken, self.start, at(self.day(3)),
                              fail_second=K.KlinesError(K.KlinesErrorCode.NETWORK, "down"))
        self.assertFalse(self.path.exists())
        plan = P.plan_catch_up(P.EMPTY_LEDGER, broken, self.start, today=self.day(3))
        self.assertEqual((plan.gaps, plan.fallbacks), ({}, (P.Gap("ETHEUR", self.day(1)),)))
        with self.assertRaises(P.PaperError) as caught:
            P.apply_plan(P.EMPTY_LEDGER, broken, plan, "ts", lambda data: P.EMPTY_LEDGER, self.start)
        self.assertEqual(caught.exception.code, P.PaperErrorCode.WAITING_FOR_DATA)

    def test_skipped_day_carries_the_books_over_without_imputation(self):
        market = trimmed(self.full, self.day(3))
        market["BTCEUR"] = without_day(market["BTCEUR"], self.day(1))
        market["EURUSDT"] = without_day(market["EURUSDT"], self.day(1))
        result = self.run_catch_up(market, self.start, at(self.day(3)))
        self.assertEqual(result.skipped, (self.day(1),))
        lines = self.lines()
        self.assertEqual(len(lines), 3 * 24 + 1)
        before = P.parse_ledger(b"\n".join(lines[:24]) + b"\n", self.start)
        after_skip = P.parse_ledger(b"\n".join(lines[:25]) + b"\n", self.start)
        self.assertEqual(after_skip.states, before.states)  # unchanged over the skipped day
        self.assertEqual(after_skip.last_day, self.day(1))
        ts = json.loads(lines[25])["ts"]
        expected, _ = P.book_day(market, self.day(2), before.states, ts, self.start)
        strip = ("schema", "seq", "prev", "sha256")
        self.assertEqual([{k: v for k, v in json.loads(x).items() if k not in strip} for x in lines[25:49]], expected)
        self.assertFalse(any(r["date"] == self.day(1).isoformat() for r in result.ledger.records))

    def test_missing_usdt_fill_candle_skips_its_day_and_every_later_trend5_window(self):
        market = trimmed(self.full, self.day(3))
        market["BTCUSDT"] = without_day(market["BTCUSDT"], self.day(1))
        result = self.run_catch_up(market, self.start, at(self.day(3)))
        self.assertEqual(result.booked, (self.start,))
        self.assertEqual(result.skipped, (self.day(1), self.day(2), self.day(3)))
        skips = store.read_ledger(self.path, self.start).skipped
        self.assertEqual(
            [(s.reason, s.missing, s.gap_day) for s in skips],
            [(P.SkipReason.NO_FILL_CANDLE, ("BTCUSDT",), self.day(1))]
            + [(P.SkipReason.NO_SIGNAL_CLOSE, ("BTCUSDT",), self.day(1))] * 2,
        )
        self.assertEqual(skips[1].text, f"no BTCUSDT close on {self.day(1)} (needed by the signal)")

    def test_missing_usdt_close_in_the_signal_window_skips_the_day(self):
        market = trimmed(self.full, self.day(1))
        gone = self.start - timedelta(days=150)
        market["ETHUSDT"] = without_day(market["ETHUSDT"], gone)
        result = self.run_catch_up(market, self.start, at(self.day(1)))
        self.assertEqual((result.booked, result.skipped), ((), (self.start, self.day(1))))
        self.assertEqual(result.status.value, "UP_TO_DATE")
        ledger = store.read_ledger(self.path, self.start)
        self.assertEqual(ledger.records, ())
        self.assertEqual({(s.reason, s.missing, s.gap_day) for s in ledger.skipped},
                         {(P.SkipReason.NO_SIGNAL_CLOSE, ("ETHUSDT",), gone)})

    def test_buy_and_hold_buys_on_the_first_booked_day_after_a_skipped_start(self):
        market = trimmed(self.full, self.day(1))
        market["ETHEUR"] = without_day(market["ETHEUR"], self.start)
        market["EURUSDT"] = without_day(market["EURUSDT"], self.start)
        result = self.run_catch_up(market, self.start, at(self.day(1)))
        self.assertEqual((result.booked, result.skipped), ((self.day(1),), (self.start,)))
        for quote in P.QUOTES:
            for fee in P.FEES:
                record = next(r for r in result.ledger.records if r["book"] == P.book_id(quote, P.Rule.BH_5050, fee))
                self.assertTrue(all(v["traded"] and v["held_after"] == 1.0 for v in record["assets"].values()))

    def test_absence_at_the_end_of_the_data_is_transient(self):
        market = trimmed(self.full, self.day(2))
        for symbol in ("BTCEUR", "ETHEUR"):
            market[symbol] = without_day(market[symbol], self.day(2))
        market["EURUSDT"] = P.DailySeries("EURUSDT", tuple(b for b in market["EURUSDT"].closed
                                                           if E.utc_day(b.open_time_ms) < self.day(2)))
        fetcher = FakeFetcher(market)
        result = catch_up(self.state_dir, fetcher, clock=lambda: at(self.day(2)), start=self.start)
        self.assertEqual(result.status.value, "WAITING_FOR_DATA")
        self.assertIn(f"candle on {self.day(2)} yet", result.detail)
        self.assertFalse(self.path.exists())  # the earlier, complete days are not booked either
        self.assertEqual(len(fetcher.calls), len(P.ALL_SYMBOLS))  # no confirmation for a transient absence

    def test_absence_seen_in_one_response_only_writes_nothing_and_is_retried(self):
        market = trimmed(self.full, self.day(3))
        broken = dict(market)
        broken["BTCEUR"] = without_day(market["BTCEUR"], self.day(1))
        broken["EURUSDT"] = without_day(market["EURUSDT"], self.day(1))
        result = self.run_catch_up(broken, self.start, at(self.day(3)), second=market)
        self.assertEqual(result.status.value, "WAITING_FOR_DATA")
        self.assertIn("disagree", result.detail)
        self.assertFalse(self.path.exists())
        # The next start with complete data books exactly what an uninterrupted run books.
        later = self.run_catch_up(market, self.start, at(self.day(3), 9))
        self.assertEqual(len(later.booked), 4)
        _, one_pass, data = book_in_memory(market, self.start)
        self.assertEqual(F.strip_ts(self.path.read_bytes()), F.strip_ts(data))

    def test_failed_or_short_confirmation_writes_nothing(self):
        market = trimmed(self.full, self.day(3))
        market["BTCEUR"] = without_day(market["BTCEUR"], self.day(1))
        market["EURUSDT"] = without_day(market["EURUSDT"], self.day(1))
        with self.assertRaises(K.KlinesError):
            self.run_catch_up(market, self.start, at(self.day(3)),
                              fail_second=K.KlinesError(K.KlinesErrorCode.NETWORK, "down"))
        self.assertFalse(self.path.exists())
        short = {s: P.DailySeries(s, tuple(b for b in v.closed if E.utc_day(b.open_time_ms) < self.day(1)))
                 for s, v in market.items()}
        result = self.run_catch_up(market, self.start, at(self.day(3)), second=short)
        self.assertEqual(result.status.value, "WAITING_FOR_DATA")
        self.assertIn("not confirmed", result.detail)
        self.assertFalse(self.path.exists())

    def test_in_memory_catch_up_never_skips_without_a_confirming_request(self):
        market = trimmed(self.full, self.day(2))
        market["BTCEUR"] = without_day(market["BTCEUR"], self.day(1))
        market["EURUSDT"] = without_day(market["EURUSDT"], self.day(1))
        with self.assertRaises(P.PaperError) as caught:
            book_in_memory(market, self.start, refetch=None)
        self.assertEqual(caught.exception.code, P.PaperErrorCode.WAITING_FOR_DATA)
        days, ledger, _ = book_in_memory(market, self.start, refetch=lambda since: market)
        self.assertEqual((days, [s.day for s in ledger.skipped]), ([self.start, self.day(2)], [self.day(1)]))

    def test_plan_waits_until_both_usdt_series_reach_today(self):
        market = trimmed(self.full, self.day(2))
        plan = P.plan_catch_up(P.EMPTY_LEDGER, market, self.start, today=self.day(3))
        self.assertEqual(plan.days, tuple(self.day(k) for k in range(4)))
        self.assertIn(f"do not reach the {self.day(3)} open", plan.waiting)
        self.assertEqual(P.plan_catch_up(P.EMPTY_LEDGER, market, self.start, today=self.day(2)).waiting, None)
        self.assertEqual(P.plan_catch_up(P.EMPTY_LEDGER, market, self.start).days, tuple(self.day(k) for k in range(3)))
        self.assertEqual(P.plan_catch_up(P.EMPTY_LEDGER, market, self.start, today=self.start - timedelta(days=1)).days, ())
        with self.assertRaises(P.PaperError) as caught:
            P.apply_plan(P.EMPTY_LEDGER, market, plan, "ts", lambda data: P.EMPTY_LEDGER, self.start)
        self.assertEqual(caught.exception.code, P.PaperErrorCode.WAITING_FOR_DATA)


def rechain(objs) -> bytes:
    """``objs`` (ledger lines as dicts) chained again from genesis with fresh hashes, as a forger would."""
    out = bytearray()
    prev = P.GENESIS
    for n, obj in enumerate(objs):
        body = {k: v for k, v in obj.items() if k != "sha256"}
        body.update(seq=n, prev=prev)
        prev = hashlib.sha256(P.canonical(body)).hexdigest()
        out += P.canonical({**body, "sha256": prev}) + b"\n"
    return bytes(out)


class SkipEntries(unittest.TestCase):
    """Skip entries: one chained, verified line per skipped day; consecutive days; the golden
    format of book records unchanged."""

    #: sha256 of the golden window's ledger bytes written by the code before the data-gap policy.
    GOLDEN_LEDGER_SHA256 = "0fee135ce0b670d0ac160a68c56ea665382ec51413b50e9789c770e06dd2c5b5"

    @classmethod
    def setUpClass(cls):
        full, cls.start, _ = golden_market()
        cls.market = trimmed(full, cls.start + timedelta(days=3))
        cls.market["BTCEUR"] = without_day(cls.market["BTCEUR"], cls.start + timedelta(days=1))
        cls.market["EURUSDT"] = without_day(cls.market["EURUSDT"], cls.start + timedelta(days=1))
        _, cls.ledger, cls.data = book_in_memory(cls.market, cls.start, refetch=lambda since: cls.market)
        cls.objs = [json.loads(x) for x in cls.data.split(b"\n")[:-1]]

    def refused(self, data, code):
        with self.assertRaises(P.PaperError) as caught:
            P.parse_ledger(data, self.start)
        self.assertEqual(caught.exception.code, code)

    def test_skip_entry_is_one_chained_canonical_line(self):
        skip = self.objs[24]
        self.assertEqual(set(skip), set(P.SKIP_KEYS))
        self.assertEqual((skip["kind"], skip["date"], skip["reason"], skip["gap_day"]),
                         ("skip", (self.start + timedelta(days=1)).isoformat(), "NO_EUR_PRICE", skip["date"]))
        self.assertEqual((skip["seq"], skip["prev"], skip["schema"]), (24, self.objs[23]["sha256"], 1))
        self.assertEqual(self.objs[25]["prev"], skip["sha256"])
        self.assertEqual(self.objs[25]["seq"], 25)
        ledger = P.parse_ledger(self.data, self.start)
        self.assertEqual(ledger.days, (self.start, self.start + timedelta(days=2), self.start + timedelta(days=3)))
        self.assertEqual([s.day for s in ledger.skipped], [self.start + timedelta(days=1)])
        self.assertEqual((ledger.entries, len(ledger.records), ledger.tip), (73, 72, self.objs[-1]["sha256"]))
        self.assertEqual(ledger.last_day, self.start + timedelta(days=3))
        self.assertTrue(ledger.skipped[0].text.endswith("(no EUR price)"))

    def test_tampering_in_or_around_a_skip_entry_is_refused(self):
        lines = self.data.split(b"\n")[:-1]

        def joined(parts):
            return b"\n".join(parts) + b"\n"

        self.refused(joined(lines[:25])[:-3], P.PaperErrorCode.LEDGER_TORN)  # torn skip line at the tail
        self.refused(joined(lines[:24] + [lines[24].replace(b"NO_EUR_PRICE", b"NO_FILL_CANDLE")] + lines[25:]),
                     P.PaperErrorCode.LEDGER_EDITED)
        self.refused(joined(lines[:24] + lines[25:]), P.PaperErrorCode.LEDGER_EDITED)  # skip line removed
        self.refused(joined(lines[:24] + [lines[24], lines[24]] + lines[25:]), P.PaperErrorCode.LEDGER_EDITED)
        forged = [dict(o) for o in self.objs]
        bad_reason = forged[:24] + [{**forged[24], "reason": "BORED"}] + forged[25:]
        self.refused(rechain(bad_reason), P.PaperErrorCode.LEDGER_INVALID)
        bad_symbol = forged[:24] + [{**forged[24], "missing": ["BTCUSDT"]}] + forged[25:]
        self.refused(rechain(bad_symbol), P.PaperErrorCode.LEDGER_INVALID)
        extra_key = forged[:24] + [{**forged[24], "note": "x"}] + forged[25:]
        self.refused(rechain(extra_key), P.PaperErrorCode.LEDGER_INVALID)
        duplicate_day = forged[:25] + [forged[24]] + forged[25:]
        self.refused(rechain(duplicate_day), P.PaperErrorCode.LEDGER_INVALID)
        booked_and_skipped = forged[:24] + [{**forged[24], "date": self.start.isoformat(), "gap_day": self.start.isoformat()}] + forged[25:]
        self.refused(rechain(booked_and_skipped), P.PaperErrorCode.LEDGER_INVALID)
        inside_a_day = forged[:10] + [forged[24]] + forged[10:24] + forged[25:]
        self.refused(rechain(inside_a_day), P.PaperErrorCode.LEDGER_INVALID)
        skipped_gap = forged[:24] + forged[25:]  # day 2 follows day 0: a day is missing
        self.refused(rechain(skipped_gap), P.PaperErrorCode.LEDGER_INVALID)
        unknown_kind = forged[:24] + [{**forged[24], "kind": "note"}] + forged[25:]
        self.refused(rechain(unknown_kind), P.PaperErrorCode.LEDGER_INVALID)
        self.assertEqual(rechain(forged), self.data)  # the forger's tool reproduces the real ledger

    def test_ledger_without_skip_entries_is_byte_for_byte_unchanged(self):
        market, start, _ = golden_market()
        _, ledger, data = book_in_memory(market, start)
        self.assertEqual(hashlib.sha256(data).hexdigest(), self.GOLDEN_LEDGER_SHA256)
        self.assertEqual(ledger.skipped, ())
        self.assertEqual(ledger.entries, len(ledger.records))
        reparsed = P.parse_ledger(data, start)
        self.assertEqual((reparsed.records, reparsed.tip, reparsed.days, reparsed.size),
                         (ledger.records, ledger.tip, ledger.days, len(data)))

    def test_consumers_ignore_skip_entries_as_book_records(self):
        summary = P.summarize(self.ledger.records)
        self.assertEqual(P.summarize(list(self.ledger.records) + [self.objs[24]]), summary)
        self.assertTrue(all(v.days == 3 for v in summary.values()))
        status = P.RegistryStatus(True, 29, ())
        text = P.render_report(self.ledger, status, at(self.start + timedelta(days=3)), None, start=self.start, offline=True)
        day1 = self.start + timedelta(days=1)
        self.assertIn(f"Paper days booked: 3 ({self.start} .. {self.start + timedelta(days=3)}).", text)
        self.assertIn("Paper days skipped: 1", text)
        self.assertIn(f"  {day1}: no ", text)
        self.assertIn(f"candle on {day1} (no EUR price)", text)
        only_skips = P.parse_ledger(rechain([self.objs[24] | {"date": self.start.isoformat(), "gap_day": self.start.isoformat()}]), self.start)
        text = P.render_report(only_skips, status, at(self.start + timedelta(days=1)), None, start=self.start, offline=True)
        self.assertIn("No paper day booked yet: 1 day(s) skipped", text)
        self.assertNotIn("not booked yet; the next catch-up books it", text)


# ---------------------------------------------------------------------------
# Network faults, backoff, clock skew, settled reads
# ---------------------------------------------------------------------------


class PoliteBackoff(unittest.TestCase):
    def client(self, responses, **kwargs):
        session = FakeSession(responses)
        clock = F.FakeMonotonic()
        k = K.BinancePublicKlines(session=session, sleep=clock.sleep, monotonic=clock, **kwargs)
        return k, session, clock

    def fetch(self, k, symbol="BTCUSDT"):
        return k.fetch_daily(symbol, date(2026, 10, 1), NOW_MS)

    def assert_code(self, k, code, symbol="BTCUSDT"):
        with self.assertRaises(K.KlinesError) as caught:
            self.fetch(k, symbol)
        self.assertEqual(caught.exception.code, code)

    def ok(self):
        return F.FakeResponse(body=body([kline(date(2026, 10, 1))]))

    def test_429_backs_off_exponentially(self):
        k, session, clock = self.client([F.FakeResponse(429), F.FakeResponse(429), self.ok()], backoff_seconds=1.0)
        self.assertEqual(len(self.fetch(k).closed), 1)
        self.assertEqual((len(session.calls), clock.sleeps), (3, [1.0, 2.0]))

    def test_retry_after_is_honoured_only_when_it_fits_the_deadline(self):
        k, session, clock = self.client([F.FakeResponse(429, headers={"Retry-After": "5"}), self.ok()], deadline_seconds=30)
        self.fetch(k)
        self.assertEqual(clock.sleeps, [5.0])
        k, session, clock = self.client([F.FakeResponse(429, headers={"Retry-After": "31"})], deadline_seconds=30)
        self.assert_code(k, K.KlinesErrorCode.RATE_LIMITED)
        self.assertEqual((len(session.calls), clock.sleeps), (1, []))  # failed at once, no sleep
        self.assert_code(k, K.KlinesErrorCode.RATE_LIMITED, "ETHUSDT")  # the client sends nothing more
        self.assertEqual(len(session.calls), 1)

    def test_a_retry_after_that_is_not_whole_seconds_falls_back_to_the_backoff(self):
        for value in ("Wed, 21 Oct 2015 07:28:00 GMT", "-5", "1.5", "²", "9999999", ""):
            with self.subTest(value=value):
                k, _, clock = self.client([F.FakeResponse(429, headers={"Retry-After": value}), self.ok()])
                self.fetch(k)
                self.assertEqual(clock.sleeps, [K.DEFAULT_BACKOFF_SECONDS])

    def test_no_sleep_runs_past_the_fetch_deadline(self):
        k, session, clock = self.client([F.FakeResponse(503)] * 3, deadline_seconds=2.5, backoff_seconds=1.0)
        self.assert_code(k, K.KlinesErrorCode.DEADLINE)
        self.assertEqual((len(session.calls), clock.sleeps), (2, [1.0]))  # the 2 s wait would pass 2.5 s
        self.assertLessEqual(clock.now, 2.5)

    def test_429_after_every_retry_stops_the_client(self):
        k, session, clock = self.client([F.FakeResponse(429)] * 3)
        self.assert_code(k, K.KlinesErrorCode.RATE_LIMITED)
        self.assertEqual(len(session.calls), 3)
        self.assert_code(k, K.KlinesErrorCode.RATE_LIMITED, "BTCEUR")
        self.assertEqual(len(session.calls), 3)

    def test_418_is_never_retried_and_stops_the_client(self):
        k, session, clock = self.client([F.FakeResponse(418, headers={"Retry-After": "1"})])
        self.assert_code(k, K.KlinesErrorCode.BANNED)
        self.assertEqual((len(session.calls), clock.sleeps), (1, []))
        for symbol in ("ETHUSDT", "EURUSDT"):
            self.assert_code(k, K.KlinesErrorCode.BANNED, symbol)
        self.assertEqual(len(session.calls), 1)

    def test_requests_per_client_are_budgeted(self):
        k, session, _ = self.client([F.FakeResponse(503)] * 5, max_requests=2)
        self.assert_code(k, K.KlinesErrorCode.BUDGET)
        self.assertEqual((len(session.calls), k.requests_sent), (2, 2))

    def test_a_body_cut_off_in_transit_is_retried_but_a_truncated_body_is_refused(self):
        rows = body([kline(date(2026, 10, 1))])
        k, session, _ = self.client([F.FakeResponse(body=rows, cut_after=10), F.FakeResponse(body=rows)])
        self.assertEqual(len(self.fetch(k).closed), 1)
        self.assertEqual(len(session.calls), 2)
        for broken in (rows[:-7], b"", b"<html>rate limited</html>"):
            k, session, _ = self.client([F.FakeResponse(body=broken)])
            with self.subTest(body=broken):
                self.assert_code(k, K.KlinesErrorCode.MALFORMED)
                self.assertEqual(len(session.calls), 1)


class NetworkFailures(unittest.TestCase):
    """Every fault of the real adapter, through the hook's catch-up: the ledger stays byte-identical
    (or absent), and the next start books what an uninterrupted run books."""

    FIRST = F.at(date(2026, 10, 4))
    NOW = F.at(date(2026, 10, 6))

    @classmethod
    def setUpClass(cls):
        cls.market = F.live_market(cls.NOW)
        with tempfile.TemporaryDirectory() as ref:
            cls.good(ref, cls.FIRST)
            cls.good(ref, cls.NOW)
            cls.reference = store.ledger_path(ref).read_bytes()

    @staticmethod
    def good(state_dir, now):
        fetcher = F.adapter_factory(F.FakeExchange(F.live_market(now)))()
        result = catch_up(state_dir, fetcher, clock=lambda: now)
        assert result.status.value == "BOOKED", result
        return result

    def test_every_fault_leaves_the_ledger_unchanged_and_the_next_start_books_normally(self):
        for name in F.fault_scenarios():
            for existing in (False, True):
                with self.subTest(fault=name, existing_ledger=existing), tempfile.TemporaryDirectory() as tmp:
                    path = store.ledger_path(tmp)
                    if existing:
                        self.good(tmp, self.FIRST)
                    before = path.read_bytes() if existing else None
                    faults, kind = F.fault_scenarios()[name]
                    exchange = F.FakeExchange(self.market, faults)
                    made: list = []
                    fetcher = F.adapter_factory(exchange, made)()
                    if kind == "market":
                        with self.assertRaises(K.KlinesError):
                            catch_up(tmp, fetcher, clock=lambda: self.NOW)
                    else:
                        result = catch_up(tmp, fetcher, clock=lambda: self.NOW)
                        self.assertEqual((result.status.value, result.booked, result.records_appended),
                                         ("WAITING_FOR_DATA", (), 0))
                        self.assertTrue(result.detail)
                    self.assertEqual(path.read_bytes() if path.exists() else None, before)
                    self.assertLessEqual(made[0].requests_sent, K.DEFAULT_MAX_REQUESTS)
                    self.assertLess(sum(made[0].fake_clock.sleeps), K.DEFAULT_DEADLINE_SECONDS)
                    for method, url, params in exchange.calls:
                        K.assert_allowed_request(method, url, params)
                    self.good(tmp, self.NOW + timedelta(hours=1))
                    self.assertEqual(F.strip_ts(path.read_bytes()), F.strip_ts(self.reference))

    def test_418_stops_the_whole_catch_up_after_one_request(self):
        exchange = F.FakeExchange(self.market, {"ETHUSDT": [F.FakeResponse(418)]})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(K.KlinesError) as caught:
                catch_up(tmp, F.adapter_factory(exchange)(), clock=lambda: self.NOW)
            self.assertEqual(caught.exception.code, K.KlinesErrorCode.BANNED)
            self.assertFalse(store.ledger_path(tmp).exists())
        symbols = [p["symbol"] for _, _, p in exchange.calls]
        self.assertEqual(symbols.count("ETHUSDT"), 1)
        self.assertEqual(symbols[-1], "ETHUSDT")
        self.assertEqual(set(symbols), {"BTCUSDT", "ETHUSDT"})  # nothing after the 418

    def test_requests_per_catch_up_stay_bounded(self):
        exchange = F.FakeExchange(self.market)
        made: list = []
        with tempfile.TemporaryDirectory() as tmp:
            catch_up(tmp, F.adapter_factory(exchange, made)(), clock=lambda: self.NOW)
        self.assertLessEqual(len(exchange.calls), 10)
        gap = F.without(F.without(self.market, "BTCEUR", date(2026, 10, 5)), "EURUSDT", date(2026, 10, 5))
        exchange = F.FakeExchange(gap)
        with tempfile.TemporaryDirectory() as tmp:
            result = catch_up(tmp, F.adapter_factory(exchange, made)(), clock=lambda: self.NOW)
        self.assertEqual(result.skipped, (date(2026, 10, 5),))
        self.assertLessEqual(len(exchange.calls), 12)
        confirm = [p for p in exchange.symbol_calls("EURUSDT")][1:]
        self.assertEqual([p["startTime"] for p in confirm], [E.day_open_ms(date(2026, 10, 5))])
        always_down = F.FakeExchange(self.market, {"*": [F.FakeResponse(503)] * 1000})
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(K.KlinesError):
            catch_up(tmp, F.adapter_factory(always_down)(), clock=lambda: self.NOW)
        self.assertEqual(len(always_down.calls), K.DEFAULT_MAX_RETRIES + 1)


class ClockSkew(unittest.TestCase):
    """The local clock up to a day behind or ahead of the exchange, around 00:00 UTC."""

    D = date(2026, 10, 6)

    def at(self, day, hour, minute=0):
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.path = store.ledger_path(self.state_dir)
        first = self.at(date(2026, 10, 4), 8)
        catch_up(self.state_dir, F.adapter_factory(F.FakeExchange(F.live_market(first)))(), clock=lambda: first)
        self.before = self.path.read_bytes()

    def run_at(self, exchange_at, local_at):
        exchange = F.FakeExchange(F.live_market(exchange_at))
        return catch_up(self.state_dir, F.adapter_factory(exchange)(), clock=lambda: local_at)

    def assert_final_only(self, exchange_at):
        published = exchange_at.date()  # the last day whose open exists at the exchange
        ledger = store.read_ledger(self.path)
        for record in ledger.records:
            self.assertLessEqual(date.fromisoformat(record["date"]), published)
            for values in record["assets"].values():
                self.assertLess(date.fromisoformat(values["signal_close_date"]), published)
        data = self.path.read_bytes()
        live = F.live_market(exchange_at)
        for symbol in P.ALL_SYMBOLS:
            self.assertNotIn(repr(live[symbol].live_open * F.LIVE_FACTOR).encode(), data)

    def test_a_clock_behind_the_exchange_writes_nothing_then_books_normally(self):
        cases = {
            "10 minutes": (self.at(self.D, 0, 5), timedelta(minutes=-10)),
            "1 hour": (self.at(self.D, 0, 30), timedelta(hours=-1)),
            "1 day": (self.at(self.D, 0, 5), timedelta(days=-1)),
        }
        for name, (exchange_at, offset) in cases.items():
            with self.subTest(behind=name):
                self.path.write_bytes(self.before)
                with self.assertRaises(K.KlinesError) as caught:
                    self.run_at(exchange_at, exchange_at + offset)
                self.assertEqual(caught.exception.code, K.KlinesErrorCode.CLOCK_SKEW)
                self.assertEqual(self.path.read_bytes(), self.before)
                result = self.run_at(exchange_at, exchange_at)  # the next start, clock right
                self.assertEqual(result.booked, (date(2026, 10, 5), self.D))
                self.assert_final_only(exchange_at)

    def test_a_clock_ahead_of_the_exchange_never_books_a_still_open_close(self):
        cases = {
            "10 minutes": (self.at(self.D, 23, 55), timedelta(minutes=10)),
            "1 hour": (self.at(self.D, 23, 30), timedelta(hours=1)),
            "1 day": (self.at(self.D, 12), timedelta(days=1)),
        }
        for name, (exchange_at, offset) in cases.items():
            with self.subTest(ahead=name):
                self.path.write_bytes(self.before)
                result = self.run_at(exchange_at, exchange_at + offset)
                self.assertEqual(result.status.value, "WAITING_FOR_DATA")
                self.assertEqual(self.path.read_bytes(), self.before)
                # The market it saw holds the still-open candle as if closed; no booked day reads it.
                self.assertEqual(result.market["BTCUSDT"].close_on(self.D),
                                 F.live_market(exchange_at)["BTCUSDT"].live_open * F.LIVE_FACTOR)
                result = self.run_at(exchange_at, exchange_at)
                self.assertEqual(result.booked, (date(2026, 10, 5), self.D))
                self.assert_final_only(exchange_at)

    def test_skew_within_the_same_utc_day_books_final_candles_only(self):
        for offset in (timedelta(minutes=-10), timedelta(minutes=10), timedelta(hours=-1), timedelta(hours=1)):
            exchange_at = self.at(self.D, 12)
            with self.subTest(offset=offset):
                self.path.write_bytes(self.before)
                result = self.run_at(exchange_at, exchange_at + offset)
                self.assertEqual(result.booked, (date(2026, 10, 5), self.D))
                self.assert_final_only(exchange_at)


class SettledRead(TempState):
    """Lock-free reads (report, UI): bounded re-read; a refusal while a writer holds the lock is
    BEING_WRITTEN; nothing is ever created."""

    def setUp(self):
        super().setUp()
        self.market, self.start, _ = golden_market()

    def test_the_writer_probe_never_creates_anything(self):
        self.assertFalse(store.writer_active(self.path))
        self.assertEqual(store.read_ledger_settled(self.path), P.EMPTY_LEDGER)
        self.assertFalse(self.path.parent.exists())

    def test_refused_reads_with_and_without_a_writer(self):
        self.run_catch_up(trimmed(self.market, self.start + timedelta(days=1)), self.start, at(self.start + timedelta(days=1)))
        good = self.path.read_bytes()
        torn = good[:-40]
        self.path.write_bytes(torn)
        sleeps = []
        with self.assertRaises(P.PaperError) as caught:
            store.read_ledger_settled(self.path, self.start, sleep=sleeps.append)
        self.assertEqual(caught.exception.code, P.PaperErrorCode.LEDGER_TORN)
        self.assertEqual(sleeps, [store.SETTLE_PAUSE_SECONDS] * (store.SETTLE_ATTEMPTS - 1))
        with store.LedgerWriter(self.path, self.start):
            self.assertTrue(store.writer_active(self.path))
            with self.assertRaises(store.TrendPaperStoreError) as caught:
                store.read_ledger_settled(self.path, self.start, sleep=lambda s: None)
            self.assertEqual(caught.exception.code, store.TrendPaperStoreErrorCode.BEING_WRITTEN)
        self.assertFalse(store.writer_active(self.path))
        self.assertEqual(self.path.read_bytes(), torn)
        # An append caught half written that completes during the re-reads is read normally.
        ledger = store.read_ledger_settled(self.path, self.start, sleep=lambda s: self.path.write_bytes(good))
        self.assertEqual(len(ledger.days), 2)


if __name__ == "__main__":
    unittest.main()
