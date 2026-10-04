"""Paper-only exposure-change toasts of the trend paper catch-up.

* formatting (pure): one sleeve, two sleeves, entry, exit, the non-ASCII dash;
* detection (pure): the four registered rules on their USDT lowest-fee book, never a comparator;
* dedupe: one toast per (rule, decision day), claimed in ``<state>/trend_paper/alerts.jsonl``
  before the send; at most one toast per rule per catch-up; a corrupt or unreadable file sends
  nothing;
* channel: only ``notifications.send_windows_notification``; no ntfy, requests, SQLite, clipboard
  or popup, and ``RADAR_NOTIFICATIONS_ENABLED`` off sends nothing;
* failure isolation: detection, store and toast failures are logged and swallowed and the ledger
  is untouched; the manual ``scripts/run_trend_paper.py`` never alerts.

No network (socket connections are refused for the whole module), ``subprocess.run`` is patched
in every test, fake fetchers and a temporary state dir only.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import requests  # noqa: E402
import trend_paper_fakes as F  # noqa: E402
from trend_paper_fakes import (  # noqa: E402  (offline market data)
    FakeFetcher,
    at,
    live_market,
)

from radar_v08 import (  # noqa: E402
    cli,
    clipboard,
    config,
    notifications,
    ntfy,
    prompt_popup,
    trend_paper_alerts,
    trend_paper_hook,
)
from radar_v08.adapters import trend_alert_store as alert_store  # noqa: E402
from radar_v08.adapters import trend_paper_store as store  # noqa: E402
from radar_v08.domain import trend_paper as P  # noqa: E402
from radar_v08.trend_paper_alerts import (  # noqa: E402
    PAPER_ONLY,
    ExposureChange,
    SleeveChange,
    TrendAlertError,
    exposure_changes,
    format_alert,
    percent,
    plan_alerts,
)

DAY = date(2026, 10, 4)
NOW = at(date(2026, 10, 6))  # books 10-04 .. 10-06
LATER = at(date(2026, 10, 9))  # books 10-04 .. 10-09
_spec = importlib.util.spec_from_file_location("run_trend_paper_for_alerts", REPOSITORY_ROOT / "scripts" / "run_trend_paper.py")
paper_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(paper_cli)
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


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class Formatting(unittest.TestCase):
    def test_one_sleeve(self):
        change = ExposureChange(P.Rule.BTC_TREND5_VT, DAY, "USDT", (SleeveChange("BTC", 0.7, 0.884, 83992.654),))
        title, body = format_alert(change)
        self.assertEqual(title, "Trend paper - BTC_TREND5_VT exposure change")
        self.assertEqual(body, f"BTC 70% -> 88.4% @ 83992.65 USDT\nPaper day 2026-10-04 (open) - {PAPER_ONLY}")

    def test_two_sleeves_of_the_ens_family(self):
        change = ExposureChange(
            P.Rule.ENS, DAY, "USDT", (SleeveChange("BTC", 0.4, 1.0, 85532.23), SleeveChange("ETH", 1.0, 0.6, 2700.61))
        )
        title, body = format_alert(change)
        self.assertIn("ENS", title)
        self.assertIn("Trend paper", title)
        lines = body.split("\n")
        self.assertEqual(lines[0], "BTC 40% -> 100% @ 85532.23 USDT")
        self.assertEqual(lines[1], "ETH 100% -> 60% @ 2700.61 USDT")
        self.assertIn("2026-10-04", lines[2])
        self.assertTrue(lines[2].endswith("paper only — no order placed"))

    def test_entry_and_exit(self):
        entry = format_alert(ExposureChange(P.Rule.ENS_VT, DAY, "USDT", (SleeveChange("ETH", 0.0, 0.8, 2500.0),)))[1]
        self.assertTrue(entry.startswith("ETH 0% -> 80% @ 2500.00 USDT\n"))
        exit_ = format_alert(ExposureChange(P.Rule.BTC_TREND5, DAY, "USDT", (SleeveChange("BTC", 1.0, 0.0, 80000.5),)))[1]
        self.assertTrue(exit_.startswith("BTC 100% -> 0% @ 80000.50 USDT\n"))

    def test_the_paper_only_text_uses_the_em_dash(self):
        self.assertEqual(PAPER_ONLY, "paper only — no order placed")
        self.assertEqual(PAPER_ONLY.encode("utf-8"), b"paper only \xe2\x80\x94 no order placed")
        body = format_alert(ExposureChange(P.Rule.ENS, DAY, "USDT", (SleeveChange("BTC", 0.0, 1.0, 1.0),)))[1]
        self.assertIn(PAPER_ONLY, body)
        self.assertNotIn("paper only - no order", body)

    def test_percent(self):
        cases = {0.0: "0%", 1.0: "100%", 0.703: "70.3%", 0.7: "70%", -1e-13: "0%", 0.99996: "100%", 0.5004: "50%"}
        for value, text in cases.items():
            with self.subTest(value=value):
                self.assertEqual(percent(value), text)


# ---------------------------------------------------------------------------
# Detection and planning
# ---------------------------------------------------------------------------


def day_records(day: date, traded: dict[str, dict[str, tuple[float, float, float]]]) -> list[dict[str, Any]]:
    """All 24 records of ``day``; ``traded`` maps a book id to its traded sleeves."""
    out = []
    for book in P.BOOKS:
        assets = {}
        for asset in book.spec.assets:
            before, after, price = traded.get(book.id, {}).get(asset, (0.5, 0.5, 100.0))
            assets[asset] = {
                "traded": asset in traded.get(book.id, {}),
                "held_before": before,
                "held_after": after,
                "fill_price": price,
            }
        out.append({"date": day.isoformat(), "book": book.id, "quote": book.quote, "rule": book.rule.value, "assets": assets})
    return out


REF = {rule: trend_paper_alerts.reference_book(rule) for rule in P.Rule}


class Detection(unittest.TestCase):
    def test_reference_books_are_usdt_at_the_lowest_fee(self):
        self.assertEqual(REF[P.Rule.ENS], "USDT|ENS|0.001")
        self.assertEqual(trend_paper_alerts.ALERT_RULES, (P.Rule.ENS, P.Rule.ENS_VT, P.Rule.BTC_TREND5, P.Rule.BTC_TREND5_VT))

    def test_comparators_and_other_books_never_alert(self):
        records = day_records(DAY, {
            REF[P.Rule.BH_5050]: {"BTC": (0, 1, 1), "ETH": (0, 1, 1)},
            REF[P.Rule.BH_BTC]: {"BTC": (0, 1, 1)},
            P.book_id("EUR", P.Rule.ENS, 0.001): {"BTC": (0, 1, 1)},
            P.book_id("USDT", P.Rule.ENS, 0.004): {"BTC": (0, 1, 1)},
        })
        self.assertEqual(exposure_changes(records, [DAY]), [])

    def test_only_traded_sleeves_are_listed(self):
        records = day_records(DAY, {REF[P.Rule.ENS]: {"ETH": (1.0, 0.6, 2700.0)}})
        (change,) = exposure_changes(records, [DAY])
        self.assertEqual(change, ExposureChange(P.Rule.ENS, DAY, "USDT", (SleeveChange("ETH", 1.0, 0.6, 2700.0),)))
        self.assertEqual(change.key, ("ENS", "2026-10-04"))

    def test_changes_are_ordered_by_day_then_rule(self):
        d2 = DAY + timedelta(days=1)
        records = day_records(DAY, {REF[P.Rule.BTC_TREND5_VT]: {"BTC": (0, 0.7, 1)}, REF[P.Rule.ENS]: {"BTC": (0, 1, 1)}})
        records += day_records(d2, {REF[P.Rule.ENS_VT]: {"ETH": (0, 1, 1)}})
        keys = [c.key for c in exposure_changes(records, [DAY, d2])]
        self.assertEqual(keys, [("ENS", "2026-10-04"), ("BTC_TREND5_VT", "2026-10-04"), ("ENS_VT", "2026-10-05")])

    def test_a_missing_or_malformed_record_raises(self):
        with self.assertRaises(TrendAlertError):
            exposure_changes(day_records(DAY, {}), [DAY + timedelta(days=1)])
        records = day_records(DAY, {REF[P.Rule.ENS]: {"BTC": (0, 1, 1)}})
        records[[r["book"] for r in records].index(REF[P.Rule.ENS])]["assets"]["BTC"]["held_after"] = "1"
        with self.assertRaises(TrendAlertError):
            exposure_changes(records, [DAY])

    def test_real_ledger_first_day_enters_every_rule(self):
        ledger = P.parse_ledger(LEDGER_BYTES)
        changes = exposure_changes(ledger.records, [DAY])
        self.assertEqual([c.rule for c in changes], list(trend_paper_alerts.ALERT_RULES))
        self.assertTrue(all(s.held_before == 0 and s.held_after > 0 for c in changes for s in c.sleeves))
        self.assertEqual([len(c.sleeves) for c in changes], [2, 2, 1, 1])

    def test_plan_keeps_the_latest_change_per_rule(self):
        def change(rule, k):
            return ExposureChange(rule, DAY + timedelta(days=k), "USDT", (SleeveChange("BTC", 0, 1, 1),))

        changes = [change(r, k) for k in range(5) for r in trend_paper_alerts.ALERT_RULES]
        plan = plan_alerts(reversed(changes))
        self.assertEqual(len(plan.shown), 4)
        self.assertTrue(all(c.day == DAY + timedelta(days=4) for c in plan.shown))
        self.assertEqual(len(plan.superseded), 16)
        self.assertEqual(plan_alerts([]), trend_paper_alerts.AlertPlan((), ()))


# ---------------------------------------------------------------------------
# Dedupe store
# ---------------------------------------------------------------------------


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = alert_store.alerts_path(tmp.name)

    def test_path_is_under_the_gitignored_trend_paper_dir(self):
        self.assertEqual(self.path.parent.name, "trend_paper")
        self.assertIn("/trend_paper/", (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_claim_once(self):
        key = ("ENS", "2026-10-04")
        self.assertEqual(alert_store.claim(self.path, [(key, alert_store.CLAIMED)], "t1"), [key])
        data = self.path.read_bytes()
        self.assertEqual(alert_store.claim(self.path, [(key, alert_store.CLAIMED)], "t2"), [])
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual(alert_store.read_handled(self.path), {key})
        self.assertEqual(data, b'{"day":"2026-10-04","rule":"ENS","status":"claimed","ts":"t1"}\n')

    def test_corrupt_files_are_refused_and_never_rewritten(self):
        good = b'{"day":"2026-10-04","rule":"ENS","status":"claimed","ts":"t"}\n'
        for data in (
            b'{"day":"2026-10-04"',
            good[:-1],
            b"[]\n",
            good.replace(b"ENS", b"BH_XXX"),
            good.replace(b"claimed", b"sent"),
            good.replace(b"2026-10-04", b"2026-13-04"),
            good.replace(b'"ts":"t"', b'"ts":1'),
            good.replace(b",", b", ", 1),
            good + b"\xff\n",
        ):
            with self.subTest(data=data):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(data)
                with self.assertRaises(alert_store.TrendAlertStoreError) as caught:
                    alert_store.claim(self.path, [(("ENS_VT", "2026-10-05"), alert_store.CLAIMED)], "t")
                self.assertIs(caught.exception.code, alert_store.TrendAlertStoreErrorCode.CORRUPT)
                self.assertEqual(self.path.read_bytes(), data)

    def test_unreadable_file(self):
        self.path.mkdir(parents=True)
        with self.assertRaises(alert_store.TrendAlertStoreError) as caught:
            alert_store.claim(self.path, [(("ENS", "2026-10-04"), alert_store.CLAIMED)], "t")
        self.assertIs(caught.exception.code, alert_store.TrendAlertStoreErrorCode.UNREADABLE)

    def test_busy_lock_claims_nothing(self):
        with alert_store._Lock(self.path):
            with self.assertRaises(alert_store.TrendAlertStoreError) as caught:
                alert_store.claim(self.path, [(("ENS", "2026-10-04"), alert_store.CLAIMED)], "t")
        self.assertIs(caught.exception.code, alert_store.TrendAlertStoreErrorCode.BUSY)
        self.assertFalse(self.path.exists())


# ---------------------------------------------------------------------------
# The alert step in the radar start hook
# ---------------------------------------------------------------------------


def _build_ledger(now) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        trend_paper_hook.catch_up(tmp, FakeFetcher(live_market(now)), clock=lambda: now)
        return store.ledger_path(tmp).read_bytes()


LEDGER_BYTES = _build_ledger(LATER)
LEDGER_NOW_BYTES = _build_ledger(NOW)  # what a catch-up at NOW writes, without any alert step


class AlertCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        self.ledger_path = store.ledger_path(self.state_dir)
        self.alerts_path = alert_store.alerts_path(self.state_dir)
        self.run = mock.Mock(return_value=mock.Mock(returncode=0, stderr=""))
        self.forbidden = {
            "ntfy": mock.Mock(return_value=ntfy.RESULT_SUCCESS),
            "requests": mock.Mock(side_effect=AssertionError("requests used")),
            "sqlite": mock.Mock(side_effect=AssertionError("sqlite opened")),
            "clipboard": mock.Mock(return_value=True),
            "popup": mock.Mock(return_value=True),
        }
        for target, name, value in (
            (config, "STATE_DIR", str(self.state_dir)),
            (config, "NOTIFICATIONS_ENABLED", True),
            (config, "RADAR_TREND_PAPER_ENABLED", True),
            (trend_paper_hook, "utc_now", lambda: NOW),
            (notifications, "_ensure_script_on_disk", lambda: str(self.state_dir / "toast.ps1")),
            (notifications.subprocess, "run", self.run),
            (ntfy, "send_ntfy_notification", self.forbidden["ntfy"]),
            (requests.sessions.Session, "request", self.forbidden["requests"]),
            (notifications.clipboard, "copy_text_to_clipboard", self.forbidden["clipboard"]),
            (prompt_popup, "launch_copy_prompt_popup", self.forbidden["popup"]),
            # The Kraken EUR step after the alerts gets its own offline fakes.
            (trend_paper_hook, "default_kraken_fetchers", lambda: (
                FakeFetcher(live_market(NOW)), F.FakeKrakenFetcher(F.kraken_market(NOW)),
            )),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("sqlite3.connect", self.forbidden["sqlite"])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIs(notifications.clipboard, clipboard)

    def tearDown(self):
        for name, spy in self.forbidden.items():
            with self.subTest(forbidden=name):
                spy.assert_not_called()
        self.assertEqual(list(self.state_dir.rglob("*.sqlite*")), [])

    def toasts(self) -> list[tuple[str, str]]:
        out = []
        for call in self.run.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[:2], ["powershell", "-NoProfile"])

            def arg(flag, argv=argv):
                return base64.b64decode(argv[argv.index(flag) + 1]).decode("utf-8")

            self.assertEqual(argv[argv.index("-Sound") + 1], "0")
            out.append((arg("-TitleB64"), arg("-MessageB64")))
        return out

    def catch_up(self, now=NOW, fetcher=None):
        fetcher = fetcher or FakeFetcher(live_market(now))
        return trend_paper_hook.run_guarded(self.state_dir, lambda: fetcher, lambda: now)

    def result_for(self, days: int) -> trend_paper_hook.CatchUpResult:
        """A booking result of the first ``days`` paper days, without a catch-up."""
        lines = LEDGER_BYTES.split(b"\n")[: days * len(P.BOOKS)]
        ledger = P.parse_ledger(b"\n".join(lines) + b"\n")
        return trend_paper_hook.CatchUpResult(trend_paper_hook.CatchUpStatus.BOOKED, ledger.days, len(ledger.records), ledger)

    def handled(self):
        return alert_store.read_handled(self.alerts_path)


class Dedupe(AlertCase):
    def test_first_catch_up_toasts_each_rule_once(self):
        result = self.catch_up()
        self.assertEqual(result.booked, (DAY, DAY + timedelta(1), DAY + timedelta(2)))
        toasts = self.toasts()
        self.assertEqual(
            [t[0] for t in toasts],
            [f"Trend paper - {r} exposure change" for r in ("ENS", "ENS_VT", "BTC_TREND5", "BTC_TREND5_VT")],
        )
        self.assertTrue(all(t[1].endswith(PAPER_ONLY) for t in toasts))
        self.assertIn("BTC 70% -> 88.4% @ 83992.65 USDT\nPaper day 2026-10-05", toasts[3][1])
        # BTC_TREND5_VT also entered on 10-04: recorded as superseded, no toast.
        lines = self.alerts_path.read_bytes().decode("ascii").splitlines()
        self.assertEqual(len(lines), 5)
        self.assertIn('"rule":"BTC_TREND5_VT","status":"superseded"', lines[0])

    def test_same_key_twice_sends_once(self):
        result = self.result_for(1)
        self.assertEqual(len(trend_paper_hook.alert_exposure_changes(self.state_dir, result, lambda: NOW)), 4)
        self.assertEqual(trend_paper_hook.alert_exposure_changes(self.state_dir, result, lambda: NOW), [])
        self.assertEqual(len(self.toasts()), 4)

    def test_a_new_day_sends_again(self):
        self.catch_up(NOW)
        self.run.reset_mock()
        result = self.catch_up(LATER)
        self.assertEqual(result.booked[0], date(2026, 10, 7))
        self.assertEqual(
            [t[0] for t in self.toasts()],
            ["Trend paper - BTC_TREND5 exposure change", "Trend paper - BTC_TREND5_VT exposure change"],
        )
        self.assertIn(("BTC_TREND5", "2026-10-09"), self.handled())

    def test_restart_and_rebooking_with_the_dedupe_file_present_never_resend(self):
        self.catch_up(NOW)
        self.assertEqual(len(self.toasts()), 4)
        data = self.alerts_path.read_bytes()
        self.run.reset_mock()
        self.assertEqual(self.catch_up(NOW).booked, ())  # restart, nothing new booked
        self.ledger_path.unlink()  # the ledger is rebuilt from scratch: the same days book again
        self.assertEqual(len(self.catch_up(NOW).booked), 3)
        self.run.assert_not_called()
        self.assertEqual(self.alerts_path.read_bytes(), data)

    def test_a_failed_toast_is_never_retried(self):
        self.run.return_value = mock.Mock(returncode=1, stderr="toast refused")
        with self.assertLogs("radar_v08", level="WARNING") as logs:
            self.catch_up()
        self.assertIn("Windows notification failed", "\n".join(logs.output))
        self.assertEqual(self.run.call_count, 4)
        self.run.reset_mock()
        self.ledger_path.unlink()
        self.catch_up()
        self.run.assert_not_called()

    def test_corrupt_dedupe_file_sends_nothing_and_warns(self):
        self.alerts_path.parent.mkdir(parents=True)
        self.alerts_path.write_bytes(b"not json\n")
        with self.assertLogs("radar_v08.trend_paper", level="WARNING") as logs:
            result = self.catch_up()
        self.assertEqual(len(result.booked), 3)
        self.run.assert_not_called()
        self.assertIn("Trend paper alert step failed", "\n".join(logs.output))
        self.assertIn("CORRUPT", "\n".join(logs.output))
        self.assertEqual(self.alerts_path.read_bytes(), b"not json\n")

    def test_unreadable_dedupe_file_sends_nothing_and_warns(self):
        self.alerts_path.mkdir(parents=True)
        with self.assertLogs("radar_v08.trend_paper", level="WARNING") as logs:
            self.assertIsNotNone(self.catch_up())
        self.run.assert_not_called()
        self.assertIn("UNREADABLE", "\n".join(logs.output))

    def test_long_gap_shows_at_most_four(self):
        result = self.result_for(6)
        toasted = trend_paper_hook.alert_exposure_changes(self.state_dir, result, lambda: LATER)
        self.assertEqual(len(toasted), 4)
        self.assertEqual(len(self.toasts()), 4)
        self.assertIn(("BTC_TREND5", "2026-10-09"), toasted)
        self.assertIn(("BTC_TREND5", "2026-10-04"), self.handled())  # superseded, recorded as handled

    def test_a_skipped_day_never_alerts(self):
        # 10-04 has no EUR price (confirmed), so it is skipped and
        # 10-05 is the first booked day.
        market = live_market(NOW)
        for symbol in ("BTCEUR", "ETHEUR", "EURUSDT"):
            market = F.without(market, symbol, DAY)
        result = self.catch_up(fetcher=FakeFetcher(market))
        self.assertEqual((result.booked, result.skipped), ((DAY + timedelta(days=1), DAY + timedelta(days=2)), (DAY,)))
        handled = self.handled()
        self.assertTrue(handled)
        self.assertFalse(any(day == DAY.isoformat() for _, day in handled))
        self.assertTrue(all(DAY.isoformat() not in body for _, body in self.toasts()))
        raw = [json.loads(line) for line in self.ledger_path.read_bytes().splitlines()]
        self.assertEqual(sum("kind" in r for r in raw), 1)
        self.assertEqual(
            trend_paper_alerts.exposure_changes(raw, result.booked),
            trend_paper_alerts.exposure_changes(result.ledger.records, result.booked),
        )
        with self.assertRaises(TrendAlertError):  # a skipped day has no book record to alert on
            trend_paper_alerts.exposure_changes(raw, [DAY])

    def test_no_change_no_toast_and_no_file(self):
        result = self.result_for(4)
        quiet = trend_paper_hook.CatchUpResult(result.status, (date(2026, 10, 7),), 24, result.ledger)
        self.assertEqual(trend_paper_alerts.exposure_changes(result.ledger.records, quiet.booked), [])
        self.assertEqual(trend_paper_hook.alert_exposure_changes(self.state_dir, quiet, lambda: NOW), [])
        self.run.assert_not_called()
        self.assertFalse(self.alerts_path.exists())


class Channel(AlertCase):
    def test_notifications_off_sends_nothing(self):
        with mock.patch.object(config, "NOTIFICATIONS_ENABLED", False):
            self.catch_up()
        self.run.assert_not_called()
        self.assertEqual(len(self.handled()), 5)

    def test_only_the_local_toast_function_is_used(self):
        with mock.patch.object(notifications, "send_windows_notification", wraps=notifications.send_windows_notification) as send:
            self.catch_up()
        self.assertEqual(send.call_count, 4)
        for call in send.call_args_list:
            self.assertEqual(len(call.args), 2)
            self.assertEqual(call.kwargs, {})

    def test_the_toast_carries_the_non_ascii_dash_through_base64(self):
        self.catch_up()
        self.assertTrue(all("—" in body for _, body in self.toasts()))

    def test_manual_cli_catch_up_and_run_stay_silent(self):
        for command in ("catch-up", "run"):
            with self.subTest(command=command):
                out = io.StringIO()
                code = paper_cli.main(
                    [command, "--state-dir", str(self.state_dir)],
                    fetcher_factory=lambda: FakeFetcher(live_market(NOW)),
                    clock=lambda: NOW,
                    out=out,
                )
                self.assertEqual(code, paper_cli.EXIT_OK, out.getvalue())
        self.assertEqual(len(P.parse_ledger(self.ledger_path.read_bytes()).days), 3)
        self.run.assert_not_called()
        self.assertFalse(self.alerts_path.exists())

    def test_trend_paper_flag_off_starts_no_thread_and_no_toast(self):
        with mock.patch.object(config, "RADAR_TREND_PAPER_ENABLED", False), \
                mock.patch.object(trend_paper_hook, "default_fetcher", lambda: FakeFetcher(live_market(NOW))), \
                mock.patch.object(cli, "_loop_cycles", return_value=0), \
                mock.patch.object(cli.paper_monitor, "start_monitor", return_value=None):
            self.assertEqual(cli._run_loop(), 0)
        self.assertEqual([t for t in threading.enumerate() if t.name == trend_paper_hook.THREAD_NAME], [])
        self.run.assert_not_called()


class FailureIsolation(AlertCase):
    def assert_isolated(self, expected_log: str, *, toasts: int = 0):
        with self.assertLogs("radar_v08", level="WARNING") as logs:
            result = self.catch_up()
        self.assertIsInstance(result, trend_paper_hook.CatchUpResult)
        self.assertEqual(len(result.booked), 3)
        self.assertEqual(self.ledger_path.read_bytes(), LEDGER_NOW_BYTES)
        self.assertIn(expected_log, "\n".join(logs.output))
        self.assertEqual(self.run.call_count, toasts)
        return result

    def test_detection_failure(self):
        with mock.patch.object(trend_paper_hook, "exposure_changes", side_effect=TrendAlertError("broken record")):
            self.assert_isolated("broken record")
        self.assertFalse(self.alerts_path.exists())

    def test_store_failure(self):
        error = alert_store.TrendAlertStoreError(alert_store.TrendAlertStoreErrorCode.WRITE_FAILED, "disk full")
        with mock.patch.object(trend_paper_hook, "claim", side_effect=error):
            self.assert_isolated("disk full")

    def test_toast_function_raises(self):
        with mock.patch.object(notifications, "send_windows_notification", side_effect=RuntimeError("toast boom")) as send:
            self.assert_isolated("Trend paper alert toast failed")
        self.assertEqual(send.call_count, 4)  # one failure does not stop the others
        self.assertEqual(len(self.handled()), 5)

    def test_toast_subprocess_failure_and_timeout(self):
        for effect in (OSError("powershell missing"), subprocess.TimeoutExpired("powershell", 15)):
            with self.subTest(effect=type(effect).__name__):
                self.ledger_path.unlink(missing_ok=True)
                if self.alerts_path.exists():
                    self.alerts_path.unlink()
                self.run.reset_mock()
                self.run.side_effect = effect
                self.assert_isolated("Windows notification failed", toasts=4)

    def test_notifications_import_failure(self):
        saved = sys.modules.pop("radar_v08.notifications")
        self.addCleanup(sys.modules.__setitem__, "radar_v08.notifications", saved)
        import radar_v08

        saved_attr = radar_v08.__dict__.pop("notifications")
        self.addCleanup(setattr, radar_v08, "notifications", saved_attr)
        with mock.patch.dict(sys.modules, {"radar_v08.notifications": None}):
            self.assert_isolated("Trend paper alert step failed")
        self.assertFalse(self.alerts_path.exists())  # nothing claimed before the toast path loaded

    def test_alerting_runs_on_the_daemon_thread_and_never_blocks_the_loop(self):
        release = threading.Event()
        self.addCleanup(release.set)
        entered = threading.Event()

        def slow_toast(*args, **kwargs):
            entered.set()
            release.wait(30)
            return mock.Mock(returncode=0, stderr="")

        self.run.side_effect = slow_toast
        with mock.patch.object(trend_paper_hook, "default_fetcher", lambda: FakeFetcher(live_market(NOW))), \
                mock.patch.object(cli, "_loop_cycles", return_value=0), \
                mock.patch.object(cli.paper_monitor, "start_monitor", return_value=None):
            started = time.monotonic()
            self.assertEqual(cli._run_loop(), 0)
            elapsed = time.monotonic() - started
            self.assertTrue(entered.wait(30))  # the toast is in progress on the hook thread
        self.assertLess(elapsed, 2.0)
        (thread,) = [t for t in threading.enumerate() if t.name == trend_paper_hook.THREAD_NAME]
        self.assertTrue(thread.daemon)
        release.set()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.run.call_count, 4)


if __name__ == "__main__":
    unittest.main()
