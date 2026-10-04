"""``scripts/run_trend_paper.py``: run, report and catch-up with fake fetchers and a temporary state dir.

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
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

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

from radar_v08.adapters import trend_paper_store as store  # noqa: E402
from radar_v08.adapters.binance_public_klines import (  # noqa: E402
    KlinesError,
    KlinesErrorCode,
)
from radar_v08.adapters.trend_registry_store import (  # noqa: E402
    RegistryStoreError,
    RegistryStoreErrorCode,
)
from radar_v08.domain import trend_engine as E  # noqa: E402
from radar_v08.domain import trend_paper as P  # noqa: E402

_spec = importlib.util.spec_from_file_location("run_trend_paper", REPOSITORY_ROOT / "scripts" / "run_trend_paper.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

BEFORE_START = at(date(2026, 10, 3), 20)
AFTER_START = at(date(2026, 10, 6), 8)
HOLDOUTS = "bh_btc, ens_btc, ens_vt_btc, btc_trend5, btc_trend5_vt, bh_vt, btc_usdc_6040, dated_carry, xs_mom10"

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
        self.path = store.ledger_path(self.state_dir)
        self.fetchers: list[FakeFetcher] = []

    def cli(self, *args, now=AFTER_START, market=None, error=None, factory=None):
        market = market if market is not None else live_market(now)

        def fake_factory():
            fetcher = FakeFetcher(market, error=error)
            self.fetchers.append(fetcher)
            return fetcher

        factory = factory or fake_factory
        out = io.StringIO()
        code = cli.main([*args, "--state-dir", str(self.state_dir)], fetcher_factory=factory, clock=lambda: now, out=out)
        return code, out.getvalue()

    def files(self):
        return sorted(p.relative_to(self.state_dir).as_posix() for p in self.state_dir.rglob("*"))


class BeforeTheFirstPaperDay(CliCase):
    def test_offline_report_is_read_only_and_states_there_are_no_paper_days(self):
        code, text = self.cli("report", "--offline", now=BEFORE_START)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("PAPER ONLY | PRE-TAX | NOT QUALIFIED", text)
        self.assertIn("No paper days yet: the first fill happens at the 2026-10-04 00:00 UTC open", text)
        self.assertIn(f"Registry: chain OK, 29 trials, holdouts consumed: {HOLDOUTS}.", text)
        self.assertIn("Current signal: not computed (--offline, no market data).", text)
        rows = [line for line in text.splitlines() if line.startswith(("EUR ", "USDT "))]
        self.assertEqual(len(rows), 16)
        self.assertTrue(all("7000.00" in line for line in rows))
        self.assertEqual(self.fetchers, [])
        self.assertEqual(self.files(), [])

    def test_online_report_shows_the_informational_signal(self):
        code, text = self.cli("report", now=BEFORE_START)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Signal for the 2026-10-03 open (decided on the 2026-10-02 close, USDT series):", text)
        self.assertIn("BTC  close     84518.01  ENS 1.00  vol30  41.0%  ENS_VT 1.00", text)
        self.assertIn("(Before 2026-10-04: informational only, not booked.)", text)
        self.assertEqual(len(self.fetchers), 1)
        self.assertTrue(self.fetchers[0].closed)
        self.assertEqual(self.files(), [])

    def test_run_books_nothing_before_the_start(self):
        code, text = self.cli("run", now=BEFORE_START)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("catch-up: BEFORE_START, appended 0 records for 0 day(s); ledger holds 0 paper day(s)", text)
        self.assertIn("No paper days yet", text)
        self.assertFalse(self.path.exists())

    def test_catch_up_before_the_start_makes_no_request(self):
        code, text = self.cli("catch-up", now=BEFORE_START)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("BEFORE_START", text)
        self.assertEqual(self.fetchers[0].calls, [])


class AfterTheFirstPaperDay(CliCase):
    def test_empty_report_after_the_start_never_says_the_first_fill_is_still_ahead(self):
        # Past 2026-10-04 00:00 UTC with nothing booked, the
        # report says the first day is due, not that its fill "happens" in the future.
        code, text = self.cli("report", "--offline")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn(
            "No paper days yet: 2026-10-04 is the first paper day, not booked yet; the next catch-up books it "
            "at its 00:00 UTC open (decided on the 2026-10-03 close). Every book is at 7000.00.",
            text,
        )
        self.assertNotIn("happens", text)
        self.assertEqual(self.files(), [])

    def test_catch_up_is_idempotent_and_report_shows_the_books(self):
        code, text = self.cli("catch-up")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("catch-up: BOOKED, appended 72 records for 3 day(s) (2026-10-04 .. 2026-10-06)", text)
        data = self.path.read_bytes()
        code, text = self.cli("catch-up")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("UP_TO_DATE, appended 0 records for 0 day(s); ledger holds 3 paper day(s)", text)
        self.assertEqual(self.path.read_bytes(), data)
        code, text = self.cli("report", "--offline")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.path.read_bytes(), data)
        self.assertIn("Paper days booked: 3 (2026-10-04 .. 2026-10-06).", text)
        self.assertIn("Last booked signal (2026-10-06 open):", text)
        self.assertNotIn("No paper days yet", text)
        ledger = store.read_ledger(self.path)
        summary = P.summarize(ledger.records)
        bh = summary["USDT|BH_BTC|0.001"]
        self.assertEqual(bh.trades, 1)
        self.assertNotEqual(bh.equity, 7000.0)
        line = next(x for x in text.splitlines() if x.startswith("USDT  BTC_TREND5     0.1%"))
        self.assertIn(f"{summary['USDT|BTC_TREND5|0.001'].equity:10.2f}", line)
        self.assertIn(f"{bh.equity:10.2f}", line)
        self.assertIn("BH_BTC", line)

    def test_run_backfills_then_reports_the_current_signal(self):
        code, text = self.cli("run")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("appended 72 records for 3 day(s)", text)
        self.assertIn("Signal for the 2026-10-06 open (decided on the 2026-10-05 close, USDT series):", text)
        self.assertIn("BTC_TREND5_VT", text)
        self.assertNotIn("Not booked yet", text)
        self.assertEqual(len(self.fetchers), 1)

    def test_an_absence_is_backfilled_day_by_day(self):
        self.cli("catch-up", now=at(date(2026, 10, 4)))
        self.assertEqual(store.read_ledger(self.path).days, (date(2026, 10, 4),))
        code, text = self.cli("catch-up", now=at(date(2026, 10, 9)))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("for 5 day(s) (2026-10-05 .. 2026-10-09)", text)
        self.assertEqual(len(store.read_ledger(self.path).days), 6)


class ExitCodes(CliCase):
    def test_usage_errors(self):
        self.assertEqual(self.cli("sell")[0], cli.EXIT_USAGE)
        code, text = self.cli("run", "--offline")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(self.files(), [])

    def test_busy(self):
        with store.LedgerWriter(self.path):
            code, text = self.cli("catch-up")
        self.assertEqual(code, cli.EXIT_BUSY)
        self.assertIn("BUSY", text)
        self.assertFalse(self.path.exists())

    def test_market_data_failure(self):
        code, text = self.cli("catch-up", error=KlinesError(KlinesErrorCode.NETWORK, "down"))
        self.assertEqual(code, cli.EXIT_MARKET)
        self.assertIn("catch-up failed: NETWORK: down", text)
        self.assertFalse(self.path.exists())
        code, text = self.cli("report", error=KlinesError(KlinesErrorCode.NETWORK, "down"))
        self.assertEqual(code, cli.EXIT_MARKET)
        self.assertIn("Current signal: unavailable (NETWORK: down).", text)

    def test_confirmed_missing_candle_is_a_listed_skipped_day(self):
        market = live_market(AFTER_START)
        gone = E.day_open_ms(date(2026, 10, 5))
        for symbol in ("BTCEUR", "EURUSDT"):
            s = market[symbol]
            market[symbol] = P.DailySeries(symbol, tuple(b for b in s.closed if b.open_time_ms != gone), s.live_day, s.live_open)
        code, text = self.cli("catch-up", market=market)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn(
            "catch-up: BOOKED, appended 48 records for 2 day(s) (2026-10-04 .. 2026-10-06); skipped 1 day(s) "
            "(2026-10-05); ledger holds 2 paper day(s)", text,
        )
        self.assertEqual([c[0] for c in self.fetchers[0].calls[len(P.ALL_SYMBOLS):]], ["BTCEUR", "EURUSDT"])
        data = self.path.read_bytes()
        code, text = self.cli("report", "--offline")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Paper days booked: 2 (2026-10-04 .. 2026-10-06).", text)
        self.assertIn("Paper days skipped: 1 (a public candle is missing for good; no record, the books carry over", text)
        self.assertIn("  2026-10-05: no BTCEUR or EURUSDT candle on 2026-10-05 (no EUR price)", text)
        self.assertEqual(self.path.read_bytes(), data)

    def test_waiting_for_data_exits_4_and_writes_nothing(self):
        just_after_midnight = AFTER_START.replace(hour=0, minute=0, second=30)
        market = F.without(live_market(just_after_midnight), "BTCUSDT", just_after_midnight.date())
        for args in (("catch-up",), ("run",)):
            with self.subTest(args=args):
                code, text = self.cli(*args, now=just_after_midnight, market=market)
                self.assertEqual(code, cli.EXIT_MARKET)
                self.assertIn(
                    "catch-up: WAITING_FOR_DATA (the public USDT daily candles do not reach the 2026-10-06 open yet); "
                    "nothing was written and the next start retries; ledger holds 0 paper day(s)", text,
                )
                self.assertNotIn("UP_TO_DATE", text)
                self.assertFalse(self.path.exists())
        code, text = self.cli("catch-up", now=just_after_midnight.replace(minute=5))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("for 3 day(s) (2026-10-04 .. 2026-10-06)", text)

    def test_network_faults_exit_4_and_write_nothing(self):
        self.cli("catch-up", now=at(date(2026, 10, 4)))
        before = self.path.read_bytes()
        for name in F.fault_scenarios():
            for args in (("catch-up",), ("run",)):
                with self.subTest(fault=name, args=args):
                    faults, kind = F.fault_scenarios()[name]
                    factory = F.adapter_factory(F.FakeExchange(live_market(AFTER_START), faults))
                    code, text = self.cli(*args, factory=factory)
                    self.assertEqual(code, cli.EXIT_MARKET)
                    self.assertIn("catch-up failed:" if kind == "market" else "catch-up: WAITING_FOR_DATA", text)
                    self.assertEqual(self.path.read_bytes(), before)
        code, text = self.cli("catch-up", factory=F.adapter_factory(F.FakeExchange(live_market(AFTER_START))))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("for 2 day(s) (2026-10-05 .. 2026-10-06)", text)

    def test_report_with_a_clock_ahead_never_shows_the_open_candle_close(self):
        exchange_at = AFTER_START.replace(hour=23, minute=55)
        local = exchange_at + timedelta(minutes=10)
        live = live_market(exchange_at)
        factory = F.adapter_factory(F.FakeExchange(live))
        for args in (("run",), ("report",)):
            with self.subTest(args=args):
                code, text = self.cli(*args, now=local, factory=factory)
                self.assertIn("Signal for the 2026-10-06 open (decided on the 2026-10-05 close, USDT series):", text)
                for symbol in P.USDT_SYMBOL.values():
                    self.assertNotIn(f"{live[symbol].live_open * F.LIVE_FACTOR:.2f}", text)
                self.assertFalse(self.path.exists())
        self.assertEqual(code, cli.EXIT_OK)

    def test_refused_ledger(self):
        self.cli("catch-up")
        torn = self.path.read_bytes()[:-5]
        self.path.write_bytes(torn)
        for args in (("catch-up",), ("run",), ("report", "--offline"), ("report",)):
            with self.subTest(args=args):
                code, text = self.cli(*args)
                self.assertEqual(code, cli.EXIT_LEDGER)
                self.assertIn("LEDGER_TORN", text)
                self.assertIn("ledger refused", text)
                self.assertIn("nothing was written and the file is never rewritten; see docs/guides/TREND-PAPER.md", text)
                self.assertNotIn(str(self.state_dir), text)
                self.assertEqual(self.path.read_bytes(), torn)
        self.assertEqual([f.calls for f in self.fetchers[1:] if f.calls], [])  # refused before any request

    def test_refused_ledger_in_or_around_a_skip_entry(self):
        market = live_market(AFTER_START)
        for symbol in ("BTCEUR", "EURUSDT"):
            market = F.without(market, symbol, date(2026, 10, 5))
        self.cli("catch-up", market=market)
        lines = self.path.read_bytes().split(b"\n")[:-1]
        self.assertIn(b'"kind":"skip"', lines[24])
        variants = {
            "LEDGER_TORN": b"\n".join(lines[:25]) + b"\n" + lines[25][:40],
            "LEDGER_EDITED": b"\n".join(lines[:24] + [lines[24].replace(b"NO_EUR_PRICE", b"NO_FILL_CANDLE")] + lines[25:])
            + b"\n",
        }
        for code_name, damaged in variants.items():
            self.path.write_bytes(damaged)
            for args in (("catch-up",), ("report", "--offline")):
                with self.subTest(code=code_name, args=args):
                    code, text = self.cli(*args)
                    self.assertEqual(code, cli.EXIT_LEDGER)
                    self.assertIn(code_name, text)
                    self.assertIn("docs/guides/TREND-PAPER.md", text)
                    self.assertEqual(self.path.read_bytes(), damaged)

    def test_report_while_a_catch_up_is_writing_is_busy_not_refused(self):
        self.cli("catch-up")
        torn = self.path.read_bytes()[:-5]
        self.path.write_bytes(torn)
        with mock.patch.object(store.time, "sleep"), store.LedgerWriter(self.path):
            code, text = self.cli("report", "--offline")
        self.assertEqual(code, cli.EXIT_BUSY)
        self.assertIn("ledger busy: a catch-up is writing the ledger right now", text)
        self.assertNotIn("refused", text)
        with mock.patch.object(store.time, "sleep"):
            self.assertEqual(self.cli("report", "--offline")[0], cli.EXIT_LEDGER)  # no writer: refused
        self.assertEqual(self.path.read_bytes(), torn)

    def test_a_number_edited_to_overflow_is_a_refused_ledger(self):
        self.cli("catch-up")
        edited = re.sub(rb'"fee_rate":[^,}]+', b'"fee_rate":1e400', self.path.read_bytes(), count=1)
        self.path.write_bytes(edited)
        for args in (("catch-up",), ("run",), ("report", "--offline"), ("report",)):
            with self.subTest(args=args):
                code, text = self.cli(*args)
                self.assertEqual(code, cli.EXIT_LEDGER)
                self.assertIn("LEDGER_EDITED", text)
                self.assertEqual(self.path.read_bytes(), edited)

    def test_registry_check_failure_still_prints_the_report(self):
        error = RegistryStoreError(RegistryStoreErrorCode.IMPORT_CHANGED, "registry.jsonl differs from the manifest")
        with mock.patch.object(cli, "load_imported_records", side_effect=error):
            code, text = self.cli("report", "--offline")
        self.assertEqual(code, cli.EXIT_REGISTRY)
        self.assertIn("Registry: CHECK FAILED (IMPORT_CHANGED: registry.jsonl differs from the manifest).", text)
        self.assertIn("PAPER ONLY | PRE-TAX | NOT QUALIFIED", text)

    def test_exit_codes_are_documented(self):
        doc = cli.__doc__
        for code, word in ((0, "done"), (2, "usage"), (3, "BUSY"), (4, "market data"), (5, "ledger refused"), (6, "registry")):
            self.assertRegex(doc, rf"\n\s+{code}\s+.*{word}")


if __name__ == "__main__":
    unittest.main()
