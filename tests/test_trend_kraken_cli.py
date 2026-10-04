"""``scripts/run_trend_paper_kraken.py``: catch-up, run and report with fake fetchers and a temporary
state dir.

* commands and exit codes 0/2/3/4/5 as ``run_trend_paper.py``;
* the report makes no network request and prints the 12 books with their labels, and the per-day,
  per-asset fill difference (Kraken open minus the Binance EUR book fill of ``ledger.jsonl``, with
  its source) in EUR and bps, or n/a when either side is missing, skipped or the main ledger is
  refused;
* ``ledger.jsonl`` is only read, never written.

No network (socket connections are refused for the whole module) and no write outside the
temporary state dir.
"""

from __future__ import annotations

import importlib.util
import io
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import trend_paper_fakes as F  # noqa: E402  (offline market data)
from trend_paper_fakes import (  # noqa: E402
    FakeFetcher,
    FakeKrakenFetcher,
    at,
    kraken_market,
    live_market,
)

from radar_v08 import trend_paper_hook  # noqa: E402
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


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load("run_trend_paper_kraken")
main_cli = _load("run_trend_paper")

BEFORE_START = at(date(2026, 10, 3), 20)
NOW = at(date(2026, 10, 6), 8)
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


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.path = trend_paper_hook.kraken_ledger_path(self.state_dir)
        self.main_path = store.ledger_path(self.state_dir)
        self.made: list = []

    def cli(self, *args, now=NOW, binance=None, kraken=None, b_error=None, k_error=None, factory=None):
        def fake_factory():
            pair = (
                FakeFetcher(binance if binance is not None else live_market(now), error=b_error),
                FakeKrakenFetcher(kraken if kraken is not None else kraken_market(now), error=k_error),
            )
            self.made.append(pair)
            return pair

        out = io.StringIO()
        code = cli.main(
            [*args, "--state-dir", str(self.state_dir)], fetchers_factory=factory or fake_factory,
            clock=lambda: now, out=out,
        )
        return code, out.getvalue()

    def main_catch_up(self, now=NOW, market=None):
        out = io.StringIO()
        code = main_cli.main(
            ["catch-up", "--state-dir", str(self.state_dir)],
            fetcher_factory=lambda: FakeFetcher(market if market is not None else live_market(now)),
            clock=lambda: now, out=out,
        )
        self.assertEqual(code, main_cli.EXIT_OK, out.getvalue())
        return self.main_path.read_bytes()

    def no_fetch(self):
        raise AssertionError("report must not create a market data client")

    def book_rows(self, text):
        return [line for line in text.splitlines() if line.startswith("KRAKEN EUR ")]

    def diff_rows(self, text):
        return [line for line in text.splitlines() if re.match(r"\d{4}-\d\d-\d\d (BTC|ETH) ", line)]

    def row(self, text, day, asset):
        (line,) = [r for r in self.diff_rows(text) if r.startswith(f"{day} {asset} ")]
        return line


class Commands(CliCase):
    def test_before_the_start_nothing_is_requested_or_written(self):
        code, text = self.cli("catch-up", now=BEFORE_START)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Kraken catch-up: BEFORE_START, appended 0 records for 0 day(s)", text)
        self.assertEqual([(b.calls, k.calls) for b, k in self.made], [([], [])])
        self.assertFalse(self.path.exists())
        code, text = self.cli("report", now=BEFORE_START, factory=self.no_fetch)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("No Kraken paper days yet: 2026-10-04 is the first paper day.", text)
        rows = self.book_rows(text)
        self.assertEqual(len(rows), 12)
        self.assertTrue(all("7000.00" in r for r in rows))
        self.assertIn("  (no paper day in either ledger yet)", text)
        self.assertEqual(sorted(p.name for p in self.state_dir.rglob("*") if p.is_file()), ["kraken_ledger.jsonl.lock"])

    def test_catch_up_is_idempotent_and_never_writes_the_main_ledger(self):
        main = self.main_catch_up()
        code, text = self.cli("catch-up")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn(
            "Kraken catch-up: BOOKED, appended 36 records for 3 day(s) (2026-10-04 .. 2026-10-06); "
            "Kraken ledger holds 3 paper day(s)", text,
        )
        data = self.path.read_bytes()
        code, text = self.cli("catch-up")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("UP_TO_DATE, appended 0 records for 0 day(s); Kraken ledger holds 3 paper day(s)", text)
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual(self.main_path.read_bytes(), main)

    def test_run_books_then_reports(self):
        code, text = self.cli("run")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("appended 36 records for 3 day(s)", text)
        self.assertIn("Kraken paper days booked: 3 (2026-10-04 .. 2026-10-06).", text)
        self.assertEqual(len(self.book_rows(text)), 12)

    def test_an_absence_is_backfilled_day_by_day(self):
        self.cli("catch-up", now=at(date(2026, 10, 4)))
        code, text = self.cli("catch-up", now=at(date(2026, 10, 9)))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("for 5 day(s) (2026-10-05 .. 2026-10-09)", text)
        self.assertEqual(len(store.read_ledger(self.path, extend=K.extend_kraken_ledger).days), 6)


class Report(CliCase):
    def test_report_shows_the_twelve_labelled_books_without_network(self):
        self.main_catch_up()
        self.cli("catch-up")
        data = self.path.read_bytes()
        code, text = self.cli("report", factory=self.no_fetch)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.path.read_bytes(), data)
        self.assertIn("PAPER ONLY | PRE-TAX | NOT QUALIFIED", text)
        self.assertIn("Results are pre-tax and not qualified", text)
        self.assertIn("0.4% maker assumption and 0.8% taker sensitivity", text)
        self.assertIn("not verified", text)
        rows = self.book_rows(text)
        self.assertEqual(len(rows), 12)
        summary = P.summarize(store.read_ledger(self.path, extend=K.extend_kraken_ledger).records, K.KRAKEN_BOOK_IDS)
        for book, line in zip(K.KRAKEN_BOOKS, rows, strict=True):
            with self.subTest(book=book.id):
                m = summary[book.id]
                self.assertIn(f" {book.rule.value} ", line)
                self.assertIn(K.FEE_LABELS[book.fee], line)
                for value in (f"{m.equity:10.2f}", f"{m.ret * 100:7.2f}%", f"{m.mdd * 100:6.2f}%", f"{m.trades:6d}",
                              f"{m.fees:8.2f}"):
                    self.assertIn(value, line)
                if book.comparator is None:
                    self.assertTrue(line.endswith("| -"))
                else:
                    self.assertIn(f"{book.spec.comparator.value:10} {summary[book.comparator].equity:10.2f}", line)

    def test_the_difference_table_joins_the_binance_eur_fills_by_date(self):
        self.main_catch_up()
        self.cli("catch-up")
        code, text = self.cli("report", factory=self.no_fetch)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Kraken open minus the Binance EUR book fill from ledger.jsonl", text)
        self.assertEqual(len(self.diff_rows(text)), 6)
        k_market, b_market = kraken_market(NOW), live_market(NOW)
        for day in (date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)):
            for asset, pair in K.KRAKEN_PAIR.items():
                with self.subTest(day=day, asset=asset):
                    k = k_market[pair].open_on(day)
                    b = b_market[P.EUR_SYMBOL[asset]].open_on(day)
                    line = self.row(text, day, asset)
                    self.assertIn(f"{k:12.2f} {b:12.2f} {P.EUR_SYMBOL[asset] + ' open':32} {k - b:10.2f} "
                                  f"{(k - b) / b * 10_000:8.1f}", line)
                    self.assertNotIn("n/a", line)

    def test_the_eur_fallback_source_is_shown(self):
        gone = date(2026, 10, 5)
        self.main_catch_up(market=F.without(live_market(NOW), "BTCEUR", gone))
        self.cli("catch-up")
        _, text = self.cli("report", factory=self.no_fetch)
        line = self.row(text, gone, "BTC")
        self.assertIn("BTCUSDT open / EURUSDT open", line)
        k = kraken_market(NOW)["XBTEUR"].open_on(gone)
        b = live_market(NOW)["BTCUSDT"].open_on(gone) / F.FX
        self.assertIn(f"{k - b:10.2f}", line)

    def test_na_when_the_binance_side_is_missing_or_skipped(self):
        self.cli("catch-up")
        _, text = self.cli("report", factory=self.no_fetch)  # no ledger.jsonl at all
        self.assertEqual(len(self.diff_rows(text)), 6)
        for line in self.diff_rows(text):
            self.assertRegex(line, r"\s+n/a n/a\s+n/a\s+n/a  Binance day not booked yet$")
        self.assertFalse(self.main_path.exists())
        gone = date(2026, 10, 5)
        market = F.without(F.without(live_market(NOW), "BTCEUR", gone), "EURUSDT", gone)
        self.main_catch_up(market=market)  # 10-05 skipped there (NO_EUR_PRICE)
        _, text = self.cli("report", factory=self.no_fetch)
        self.assertIn("n/a  Binance day skipped", self.row(text, gone, "ETH"))
        self.assertNotIn("n/a", self.row(text, date(2026, 10, 4), "BTC"))

    def test_na_when_the_kraken_side_is_skipped_or_not_booked(self):
        self.main_catch_up()
        gone = date(2026, 10, 5)
        self.cli("catch-up", kraken=F.without(kraken_market(NOW), "ETHEUR", gone))
        _, text = self.cli("report", factory=self.no_fetch)
        self.assertIn("Kraken paper days skipped: 1", text)
        self.assertIn("  2026-10-05: no Kraken ETHEUR candle on 2026-10-05 (no Kraken fill price)", text)
        for asset in ("BTC", "ETH"):
            line = self.row(text, gone, asset)
            self.assertRegex(line, r"^2026-10-05 \w+ +n/a ")
            self.assertTrue(line.endswith("Kraken day skipped"))
        self.main_catch_up(now=at(date(2026, 10, 7)))  # the main ledger is a day ahead
        _, text = self.cli("report", factory=self.no_fetch)
        self.assertTrue(self.row(text, date(2026, 10, 7), "BTC").endswith("Kraken day not booked yet"))

    def test_na_when_the_main_ledger_is_refused(self):
        torn = self.main_catch_up()[:-5]
        self.main_path.write_bytes(torn)
        self.cli("catch-up")
        with mock.patch.object(store.time, "sleep"):
            code, text = self.cli("report", factory=self.no_fetch)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Binance EUR fills unavailable (ledger.jsonl refused (LEDGER_TORN)): every difference is n/a.", text)
        rows = self.diff_rows(text)
        self.assertEqual(len(rows), 6)
        for line in rows:
            self.assertIn("n/a", line)
            self.assertTrue(line.endswith("ledger.jsonl refused (LEDGER_TORN)"))
        self.assertEqual(len(self.book_rows(text)), 12)
        self.assertEqual(self.main_path.read_bytes(), torn)


class ExitCodes(CliCase):
    def test_usage_errors(self):
        self.assertEqual(self.cli("sell")[0], cli.EXIT_USAGE)
        self.assertEqual(self.cli("report", "--offline")[0], cli.EXIT_USAGE)
        self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_busy(self):
        with store.LedgerWriter(self.path, extend=K.extend_kraken_ledger):
            code, text = self.cli("catch-up")
        self.assertEqual(code, cli.EXIT_BUSY)
        self.assertIn("BUSY", text)
        self.assertFalse(self.path.exists())

    def test_report_while_a_catch_up_is_writing_is_busy_not_refused(self):
        self.cli("catch-up")
        torn = self.path.read_bytes()[:-5]
        self.path.write_bytes(torn)
        with mock.patch.object(store.time, "sleep"), store.LedgerWriter(self.path, extend=K.extend_kraken_ledger):
            code, text = self.cli("report", factory=self.no_fetch)
        self.assertEqual(code, cli.EXIT_BUSY)
        self.assertIn("Kraken ledger busy:", text)
        with mock.patch.object(store.time, "sleep"):
            self.assertEqual(self.cli("report", factory=self.no_fetch)[0], cli.EXIT_LEDGER)
        self.assertEqual(self.path.read_bytes(), torn)

    def test_market_data_failures_exit_4_and_write_nothing(self):
        for kwargs in (
            {"b_error": KlinesError(KlinesErrorCode.NETWORK, "down")},
            {"k_error": KrakenOhlcError(KrakenOhlcErrorCode.RATE_LIMITED, "EAPI:Rate limit exceeded")},
            {"k_error": KrakenOhlcError(KrakenOhlcErrorCode.CLOCK_SKEW, "behind")},
        ):
            for args in (("catch-up",), ("run",)):
                with self.subTest(args=args, **{k: str(v) for k, v in kwargs.items()}):
                    code, text = self.cli(*args, **kwargs)
                    self.assertEqual(code, cli.EXIT_MARKET)
                    self.assertIn("Kraken catch-up failed:", text)
                    self.assertFalse(self.path.exists())
                    self.assertTrue(all(b.closed and k.closed for b, k in self.made))

    def test_waiting_for_data_exits_4_and_writes_nothing(self):
        just_after_midnight = NOW.replace(hour=0, minute=0, second=30)
        kraken = F.without(kraken_market(just_after_midnight), "XBTEUR", just_after_midnight.date())
        for args in (("catch-up",), ("run",)):
            with self.subTest(args=args):
                code, text = self.cli(*args, now=just_after_midnight, kraken=kraken)
                self.assertEqual(code, cli.EXIT_MARKET)
                self.assertIn(
                    "Kraken catch-up: WAITING_FOR_DATA (no Kraken XBTEUR candle on 2026-10-06 yet (none after it "
                    "either)); nothing was written and the next start retries; Kraken ledger holds 0 paper day(s)", text,
                )
                self.assertFalse(self.path.exists())

    def test_refused_kraken_ledger_exits_5_and_is_never_rewritten(self):
        self.cli("catch-up")
        good = self.path.read_bytes()
        variants = {
            "LEDGER_TORN": good[:-5],
            "LEDGER_EDITED": re.sub(rb'"fee_rate":[^,}]+', b'"fee_rate":1e400', good, count=1),
            "LEDGER_INVALID": b"\n".join(good.split(b"\n")[:5]) + b"\n",
        }
        for name, damaged in variants.items():
            self.path.write_bytes(damaged)
            for args in (("catch-up",), ("run",), ("report",)):
                with self.subTest(code=name, args=args), mock.patch.object(store.time, "sleep"):
                    self.made.clear()
                    code, text = self.cli(*args)
                    self.assertEqual(code, cli.EXIT_LEDGER)
                    self.assertIn(name, text)
                    self.assertIn("Kraken ledger refused", text)
                    self.assertIn("nothing was written and the file is never rewritten; see docs/guides/TREND-PAPER.md", text)
                    self.assertNotIn(str(self.state_dir), text)
                    self.assertEqual(self.path.read_bytes(), damaged)
                    self.assertEqual([(b.calls, k.calls) for b, k in self.made if b.calls or k.calls], [])

    def test_exit_codes_are_documented(self):
        doc = cli.__doc__
        for code, word in ((0, "done"), (2, "usage"), (3, "BUSY"), (4, "market data"), (5, "ledger refused")):
            self.assertRegex(doc, rf"\n\s+{code}\s+.*{word}")
        self.assertEqual(
            (cli.EXIT_OK, cli.EXIT_USAGE, cli.EXIT_BUSY, cli.EXIT_MARKET, cli.EXIT_LEDGER),
            (main_cli.EXIT_OK, main_cli.EXIT_USAGE, main_cli.EXIT_BUSY, main_cli.EXIT_MARKET, main_cli.EXIT_LEDGER),
        )


if __name__ == "__main__":
    unittest.main()
