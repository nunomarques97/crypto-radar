"""Offline end-to-end test of the trend paper stack.

The scenario of ``trend_paper_fakes.E2E_STARTS`` (27 radar starts over the 60 paper days
2026-10-04 .. 2026-12-02) runs through the real radar start path,
``radar_v08.cli._start_trend_paper_catch_up``, with ``config.STATE_DIR`` set to a temporary state dir
and ``RADAR_TREND_PAPER_ENABLED`` on, joined on the catch-up thread. Only the seams are replaced: the
hook's clock (``trend_paper_hook.utc_now``), its market data clients (``default_fetcher`` and
``default_kraken_fetchers`` return the real Binance and Kraken adapters over fake HTTP sessions with a
fake sleep and monotonic clock) and the toast (``notifications.send_windows_notification``, recorded).

The scenario holds a signal flip of every registered rule, a Binance EUR pair candle and a Kraken
open missing for good, a start whose Binance requests all fail, a duplicate start on the same UTC
day and a 5-day PC-off gap. The produced ledgers, dedupe file, toasts and UI reader payload must
equal the goldens of ``tests/fixtures/trend/e2e/`` byte for byte, a second run in a fresh state dir
must produce the same bytes, and independent assertions check the booked days, skips, fallback
prices, the alert contract, the Kraken signals and fills, and that nothing is written outside the
state dir (an audit hook records every write-mode open and file system change).

No network: socket connections and name lookups are refused for the whole module.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import requests  # noqa: E402,F401  (imported before the audited runs: no import happens inside them)
import trend_paper_fakes as F  # noqa: E402

import ui.trend_reader  # noqa: E402,F401
from radar_v08 import cli, config, notifications, trend_paper_hook  # noqa: E402,F401
from radar_v08.adapters import (  # noqa: E402,F401
    binance_public_klines,
    kraken_public_ohlc,
)
from radar_v08.adapters import trend_alert_store as alert_store  # noqa: E402
from radar_v08.adapters import trend_paper_store as store  # noqa: E402
from radar_v08.domain import trend_engine as E  # noqa: E402
from radar_v08.domain import trend_paper as P  # noqa: E402
from radar_v08.domain import trend_paper_kraken as K  # noqa: E402
from radar_v08.trend_paper_alerts import ALERT_RULES, reference_book  # noqa: E402

E2E = F.E2E_DIR
_spec = importlib.util.spec_from_file_location("replay_trend_paper_for_e2e", REPOSITORY_ROOT / "scripts" / "replay_trend_paper.py")
replay_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay_tool)
PAPER_DAYS = [P.PAPER_START + timedelta(days=k) for k in range(60)]
FAILED_START = 10  # 1-based index into F.E2E_STARTS
DUPLICATE_START = 12
GAP_START = 21
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
# File system audit: every write-mode open and file system change while a recorder is active
# ---------------------------------------------------------------------------

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_CHANGE_EVENTS = frozenset({
    "os.mkdir", "os.rename", "os.replace", "os.remove", "os.rmdir", "os.truncate", "os.symlink", "os.link",
    "shutil.rmtree", "shutil.move", "shutil.copyfile", "shutil.copytree",
})
_recording: list[list] = []  # the active recorder (at most one); audit hooks cannot be removed


def _audit(event, args):
    if not _recording:
        return
    if event == "open" or event in _CHANGE_EVENTS or event == "sqlite3.connect":
        _recording[0].append((event, args))


sys.addaudithook(_audit)


def _path_text(value) -> str | None:
    if isinstance(value, int):  # an already open descriptor
        return None
    try:
        return os.fsdecode(os.fspath(value))
    except TypeError:
        return None


def _is_write(event, args) -> bool:
    if event != "open":
        return True
    mode, flags = args[1], args[2]
    if isinstance(mode, str):
        return any(c in mode for c in "wax+")
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _paths(event, args) -> list[str]:
    if event in ("os.rename", "os.replace", "os.symlink", "os.link", "shutil.move", "shutil.copyfile", "shutil.copytree"):
        candidates = args[:2]
    else:
        candidates = args[:1]
    return [p for p in (_path_text(c) for c in candidates) if p is not None]


def _inside(path: str, root: Path) -> bool:
    full = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    base = os.path.normcase(os.path.realpath(root))
    return full.startswith(base + os.sep)


def _run(state_dir: Path):
    events: list = []
    _recording.append(events)
    try:
        results = F.run_e2e_scenario(state_dir)
        produced = F.e2e_outputs(state_dir, results)
    finally:
        _recording.clear()
    return results, produced, events


# ---------------------------------------------------------------------------
# Helpers over the produced files
# ---------------------------------------------------------------------------


def _lines(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.splitlines()]


def _day(text: str) -> date:
    return date.fromisoformat(text)


class AuditHook(unittest.TestCase):
    """The containment oracle is not vacuous: it sees writes outside a root and SQLite opens."""

    def test_a_write_outside_the_root_and_a_sqlite_connect_are_seen(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as inside, tempfile.TemporaryDirectory() as outside:
            events: list = []
            _recording.append(events)
            try:
                (Path(inside) / "kept.txt").write_bytes(b"x")
                (Path(outside) / "leak.txt").write_bytes(b"x")
                os.mkdir(Path(outside) / "made")
                (Path(outside) / "leak.txt").read_bytes()
                sqlite3.connect(":memory:").close()
            finally:
                _recording.clear()
            writes = [(e, p) for e, a in events if _is_write(e, a) for p in _paths(e, a)]
            flagged = [os.path.basename(p) for _, p in writes if not _inside(p, Path(inside))]
            self.assertEqual(sorted(flagged), [":memory:", "leak.txt", "made"])  # any SQLite connect counts
            self.assertIn("kept.txt", [os.path.basename(p) for _, p in writes])
            self.assertIn("sqlite3.connect", [e for e, _ in events])


class FixtureFiles(unittest.TestCase):
    """The vendored scenario directory: manifest, generator output, window and the real fixtures."""

    def test_manifest_lists_every_file_with_its_bytes_and_sha256(self):
        manifest = json.loads((E2E / F.E2E_MANIFEST).read_text(encoding="ascii"))
        listed = {f["path"]: f for f in manifest["files"]}
        on_disk = sorted(p.name for p in E2E.iterdir() if p.name != F.E2E_MANIFEST)
        self.assertEqual(sorted(listed), on_disk)
        for name, entry in listed.items():
            data = (E2E / name).read_bytes()
            self.assertEqual(entry["bytes"], len(data), name)
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest(), name)
        self.assertIn("synthetic test data, not market history", manifest["synthetic"])

    def test_candle_files_are_the_generator_output(self):
        for name, data in F.e2e_candle_files().items():
            self.assertEqual((E2E / name).read_bytes(), data, name)

    def test_candle_windows_and_the_days_missing_on_purpose(self):
        def days(name):
            rows = json.loads((E2E / name).read_text(encoding="ascii"))
            return [E.utc_day(r[0]) for r in rows]

        extension = [date(2026, 10, 3) + timedelta(days=k) for k in range(61)]
        kraken = [date(2026, 9, 27) + timedelta(days=k) for k in range(67)]
        self.assertEqual(days("binance_BTCUSDT_extension.json"), extension)
        self.assertEqual(days("binance_ETHUSDT_extension.json"), extension)
        self.assertEqual(days("binance_EURUSDT.json"), PAPER_DAYS)
        self.assertEqual(days("binance_ETHEUR.json"), PAPER_DAYS)
        self.assertEqual(days("binance_BTCEUR.json"), [d for d in PAPER_DAYS if d != date(2026, 10, 9)])
        self.assertEqual(days("kraken_ETHEUR.json"), kraken)
        self.assertEqual(days("kraken_XBTEUR.json"), [d for d in kraken if d != date(2026, 10, 25)])

    def test_real_history_and_paper_fixtures_are_unchanged(self):
        for directory in (F.FIXTURES, F.FIXTURES / "paper"):
            manifest = json.loads((directory / "MANIFEST.json").read_text(encoding="utf-8"))
            for entry in manifest["files"]:
                data = (directory / entry["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"], entry["path"])

    def test_the_directory_is_stored_without_line_ending_conversion(self):
        rules = (REPOSITORY_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
        self.assertIn("tests/fixtures/trend/** -text", rules)


class Scenario(unittest.TestCase):
    """One audited run in a fresh state dir, then a second one for determinism."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.state_dir = root / "first"
        cls.second_dir = root / "second"
        cls.state_dir.mkdir()
        cls.second_dir.mkdir()
        cls.results, cls.produced, cls.events = _run(cls.state_dir)
        cls.second_results, cls.second_produced, cls.second_events = _run(cls.second_dir)
        cls.ledger = store.read_ledger(store.ledger_path(cls.state_dir))
        cls.kraken = store.read_ledger(trend_paper_hook.kraken_ledger_path(cls.state_dir), extend=K.extend_kraken_ledger)
        cls.alerts = _lines(alert_store.alerts_path(cls.state_dir).read_bytes())
        cls.candles = F.load_e2e_candles()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def result(self, index: int):
        return self.results[index - 1]

    # -- goldens -------------------------------------------------------------------------------

    def test_ledgers_and_dedupe_file_equal_the_goldens_byte_for_byte(self):
        for name, golden in F.E2E_STATE_GOLDENS.items():
            with self.subTest(file=name):
                produced = (self.state_dir / store.LEDGER_DIR_NAME / name).read_bytes()
                self.assertTrue(produced == (E2E / golden).read_bytes(), f"{name} differs from {golden}")

    def test_toasts_equal_the_golden_list(self):
        golden = json.loads((E2E / F.E2E_TOASTS_GOLDEN).read_text(encoding="ascii"))
        recorded = [{"start": r.index, "title": t, "body": b} for r in self.results for t, b in r.toasts]
        self.assertEqual(recorded, golden)

    def test_reader_payload_equals_the_golden_json(self):
        payload = trend_paper_reader_payload(self.state_dir)
        golden = (E2E / F.E2E_READER_GOLDEN).read_bytes()
        self.assertEqual(json.loads(json.dumps(payload)), json.loads(golden))
        self.assertEqual(F.e2e_json(payload), golden)

    def test_per_start_summary_equals_the_golden(self):
        self.assertEqual(self.produced[F.E2E_STARTS_GOLDEN], (E2E / F.E2E_STARTS_GOLDEN).read_bytes())

    def test_a_second_run_in_a_fresh_state_dir_produces_the_same_bytes(self):
        self.assertEqual(list(self.second_produced), list(self.produced))
        for name, data in self.produced.items():
            with self.subTest(file=name):
                self.assertTrue(self.second_produced[name] == data, f"{name} differs between two runs")
        self.assertEqual([r.summary() for r in self.second_results], [r.summary() for r in self.results])

    # -- booked days, independent of the goldens ------------------------------------------------

    def test_sixty_binance_days_of_twenty_four_records_and_no_skip(self):
        self.assertEqual(list(self.ledger.days), PAPER_DAYS)
        self.assertEqual(self.ledger.skipped, ())
        self.assertEqual(len(self.ledger.records), 60 * 24)
        for k, day in enumerate(PAPER_DAYS):
            group = self.ledger.records[k * 24 : (k + 1) * 24]
            self.assertEqual([r["book"] for r in group], list(P.BOOK_IDS))
            self.assertEqual({r["date"] for r in group}, {day.isoformat()})

    def test_kraken_books_every_day_but_the_missing_open_which_is_one_skip_line(self):
        skip = date(2026, 10, 25)
        self.assertEqual(list(self.kraken.days), [d for d in PAPER_DAYS if d != skip])
        self.assertEqual(len(self.kraken.records), 59 * 12)
        skipped = K.kraken_skipped(self.kraken)
        self.assertEqual(
            [(s.day, s.reason, s.missing, s.gap_day) for s in skipped],
            [(skip, K.KrakenSkipReason.NO_KRAKEN_OPEN, ("XBTEUR",), skip)],
        )
        self.assertEqual(skipped[0].text, "no Kraken XBTEUR candle on 2026-10-25 (no Kraken fill price)")
        self.assertNotIn(skip.isoformat(), {r["date"] for r in self.kraken.records})
        self.assertIn(skip, self.ledger.days)  # the Binance books are not affected
        raw = _lines((self.state_dir / store.LEDGER_DIR_NAME / "kraken_ledger.jsonl").read_bytes())
        self.assertEqual([r["date"] for r in raw if "kind" in r], [skip.isoformat()])

    def test_which_start_booked_which_days(self):
        last = P.PAPER_START - timedelta(days=1)
        for result in self.results:
            start = result.start
            with self.subTest(start=result.index):
                if start.binance_down:
                    expected = []
                else:
                    expected = [last + timedelta(days=k + 1) for k in range((start.moment.date() - last).days)]
                    last = max(last, start.moment.date())
                self.assertEqual(list(result.booked), expected)
                self.assertEqual(result.skipped, ())
                kraken = [d for d in expected if d != date(2026, 10, 25)]
                self.assertEqual(list(result.kraken_booked), kraken)
                self.assertEqual(list(result.kraken_skipped), [d for d in expected if d == date(2026, 10, 25)])
                status = "FAILED" if start.binance_down else ("BOOKED" if expected else "UP_TO_DATE")
                self.assertEqual((result.binance_status, result.kraken_status), (status, status))
        # The cases the scenario must hold, stated outright.
        self.assertTrue(self.result(FAILED_START).start.binance_down)
        self.assertEqual(self.result(FAILED_START + 1).booked, (date(2026, 10, 17), date(2026, 10, 18)))
        self.assertEqual(self.result(DUPLICATE_START).start.moment.date(), self.result(DUPLICATE_START - 1).start.moment.date())
        gap_before = self.result(GAP_START - 1).start.moment.date()
        self.assertEqual((self.result(GAP_START).start.moment.date() - gap_before).days, 6)  # 5 days without a start
        self.assertEqual(self.result(GAP_START).booked, tuple(date(2026, 11, 9) + timedelta(days=k) for k in range(6)))
        self.assertEqual(self.result(GAP_START).kraken_booked, self.result(GAP_START).booked)
        self.assertEqual(self.results[-1].booked[-1], date(2026, 12, 2))
        self.assertEqual(self.results[-1].kraken_booked[-1], date(2026, 12, 2))

    def test_the_failed_start_wrote_nothing_after_the_adapter_retries(self):
        result = self.result(FAILED_START)
        self.assertEqual(result.state_after, result.state_before)
        self.assertEqual((result.toasts, result.alert_lines), ((), 0))
        retries = binance_public_klines.DEFAULT_MAX_RETRIES + 1
        self.assertEqual(result.requests, {"binance": retries, "binance_signals": retries, "kraken": 0})
        self.assertNotEqual(self.result(FAILED_START + 1).state_after, result.state_after)

    def test_the_duplicate_start_changes_no_byte_and_sends_nothing(self):
        result = self.result(DUPLICATE_START)
        self.assertEqual(result.state_after, result.state_before)
        self.assertEqual((result.toasts, result.alert_lines), ((), 0))
        self.assertEqual(result.requests, {"binance": 0, "binance_signals": 0, "kraken": 0})

    def test_the_missing_eur_day_is_booked_at_the_eurusdt_fallback(self):
        missing = date(2026, 10, 9)
        usdt_open = self.open_of("binance", "BTCUSDT", missing)
        fx_open = self.open_of("binance", "EURUSDT", missing)
        for record in self.ledger.records:
            for asset, sleeve in record["assets"].items():
                day = _day(record["date"])
                if record["quote"] == "USDT":
                    expected = (self.open_of("binance", P.USDT_SYMBOL[asset], day), f"{P.USDT_SYMBOL[asset]} open")
                elif (asset, day) == ("BTC", missing):
                    expected = (usdt_open / fx_open, "BTCUSDT open / EURUSDT open")
                else:
                    expected = (self.open_of("binance", P.EUR_SYMBOL[asset], day), f"{P.EUR_SYMBOL[asset]} open")
                self.assertEqual((sleeve["fill_price"], sleeve["fill_source"]), expected, (record["book"], day))
        # The second request that confirmed the absence went out, and only for BTCEUR.
        self.assertEqual(self.result(4).requests["binance"], self.result(3).requests["binance"] + 1)

    def open_of(self, venue: str, symbol: str, day: date) -> float:
        return next(b.open for b in self.candles[venue][symbol] if E.utc_day(b.open_time_ms) == day)

    # -- the alert contract ---------------------------------------------------------------------

    def changes(self, days) -> list[tuple[str, str]]:
        """(rule, day) of every exposure change of a reference book on ``days``, from the ledger."""
        wanted = {d.isoformat() for d in days}
        out = []
        for record in self.ledger.records:
            for rule in ALERT_RULES:
                if record["book"] == reference_book(rule) and record["date"] in wanted:
                    if any(s["traded"] for s in record["assets"].values()):
                        out.append((rule.value, record["date"]))
        return out

    def test_every_exposure_change_is_handled_exactly_once_in_the_dedupe_file(self):
        keys = [(a["rule"], a["day"]) for a in self.alerts]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(sorted(keys), sorted(self.changes(PAPER_DAYS)))

    def test_each_catch_up_shows_the_latest_change_per_rule_and_supersedes_the_rest(self):
        position = 0
        for result in self.results:
            with self.subTest(start=result.index):
                by_rule = defaultdict(list)
                for rule, day in self.changes(result.booked):
                    by_rule[rule].append(day)
                expected = {(rule, max(days)): "claimed" for rule, days in by_rule.items()}
                expected |= {(rule, d): "superseded" for rule, days in by_rule.items() for d in days if d != max(days)}
                added = self.alerts[position : position + result.alert_lines]
                position += result.alert_lines
                self.assertEqual({(a["rule"], a["day"]): a["status"] for a in added}, expected)
                self.assertEqual({a["ts"] for a in added} or {result.start.moment.isoformat()}, {result.start.moment.isoformat()})
                toasted = [self.toast_key(t, b) for t, b in result.toasts]
                self.assertEqual(sorted(toasted), sorted(k for k, s in expected.items() if s == "claimed"))
                self.assertLessEqual(max(Counter(rule for rule, _ in toasted).values(), default=0), 1)
        self.assertEqual(position, len(self.alerts))
        # The burst limit was exercised: the gap's catch-up superseded earlier changes.
        self.assertTrue(any(a["status"] == "superseded" for a in self.alerts))

    def toast_key(self, title: str, body: str) -> tuple[str, str]:
        rule = title.removeprefix("Trend paper - ").removesuffix(" exposure change")
        last = body.split("\n")[-1]
        self.assertTrue(last.endswith("paper only — no order placed"), body)
        return rule, last.removeprefix("Paper day ")[:10]

    def test_each_claimed_key_is_toasted_exactly_once_across_all_starts(self):
        toasted = [self.toast_key(t, b) for r in self.results for t, b in r.toasts]
        claimed = [(a["rule"], a["day"]) for a in self.alerts if a["status"] == "claimed"]
        self.assertEqual(Counter(toasted), Counter(claimed))
        self.assertEqual(max(Counter(toasted).values()), 1)

    def test_every_rule_flips_and_one_flip_per_rule_is_toasted_alone(self):
        for rule in ALERT_RULES:
            with self.subTest(rule=rule.value):
                held = [
                    (r["date"], s["held_before"], s["held_after"])
                    for r in self.ledger.records
                    if r["book"] == reference_book(rule)
                    for s in r["assets"].values()
                    if s["traded"]
                ]
                self.assertTrue(any(after == 0 < before for _, before, after in held), "no exit to flat")
                self.assertTrue(
                    any(before == 0 < after and day > P.PAPER_START.isoformat() for day, before, after in held),
                    "no re-entry from flat",
                )
                alone = [
                    (r.index, day)
                    for r in self.results
                    for changed_rule, day in self.changes(r.booked)
                    if changed_rule == rule.value
                    and sum(c == rule.value for c, _ in self.changes(r.booked)) == 1
                    and day > P.PAPER_START.isoformat()
                ]
                self.assertTrue(alone, "no change of this rule alone in its catch-up")
                for index, day in alone:
                    self.assertIn((rule.value, day), [self.toast_key(t, b) for t, b in self.result(index).toasts])

    # -- Kraken: same signals, Kraken fills -------------------------------------------------------

    def test_kraken_records_use_the_binance_signals_and_the_kraken_opens(self):
        binance = defaultdict(list)
        for record in self.ledger.records:
            binance[(record["date"], record["rule"])].append(record)
        checked = 0
        for record in self.kraken.records:
            day = _day(record["date"])
            for asset, sleeve in record["assets"].items():
                for other in binance[(record["date"], record["rule"])]:
                    theirs = other["assets"][asset]
                    for field in ("signal", "target", "signal_close_date", "signal_close_usdt"):
                        self.assertEqual(sleeve[field], theirs[field], (record["book"], day, asset, field))
                pair = K.KRAKEN_PAIR[asset]
                kraken_open = self.open_of("kraken", pair, day)
                self.assertEqual((sleeve["fill_price"], sleeve["fill_source"]), (kraken_open, f"Kraken {pair} open"))
                usdt_open = self.open_of("binance", P.USDT_SYMBOL[asset], day)
                others = {usdt_open, usdt_open / self.open_of("binance", "EURUSDT", day)}
                if not (asset, day) == ("BTC", date(2026, 10, 9)):
                    others.add(self.open_of("binance", P.EUR_SYMBOL[asset], day))
                self.assertNotIn(sleeve["fill_price"], others)
                checked += 1
        self.assertEqual(checked, 59 * 18)  # 59 days x (3 ENS-family books x 2 sleeves + 3 trend5 books) x 2 fees

    # -- containment ----------------------------------------------------------------------------

    def test_nothing_is_written_outside_the_state_dir(self):
        for events, root in ((self.events, self.state_dir), (self.second_events, self.second_dir)):
            writes = [(event, args) for event, args in events if _is_write(event, args)]
            self.assertTrue(any(p.endswith("ledger.jsonl") for e, a in writes for p in _paths(e, a)))
            outside = [
                (event, path) for event, args in writes for path in _paths(event, args) if not _inside(path, root)
            ]
            self.assertEqual(outside, [])

    def test_radar_state_sqlite_and_dotenv_are_never_opened(self):
        for events in (self.events, self.second_events):
            self.assertNotIn("sqlite3.connect", {event for event, _ in events})
            opened = [os.path.basename(p) for event, args in events if event == "open" for p in _paths(event, args)]
            self.assertNotIn("radar_state.sqlite", opened)
            self.assertFalse([name for name in opened if name.startswith(os.extsep + "env")])

    def test_the_hook_thread_is_gone(self):
        self.assertEqual([t for t in threading.enumerate() if t.name == trend_paper_hook.THREAD_NAME], [])

    # -- the replay tool ------------------------------------------------------------------------

    def test_replay_check_matches_and_names_the_first_differing_file(self):
        out = io.StringIO()
        self.assertEqual(replay_tool.check(self.produced, out), 0)
        self.assertIn("Match", out.getvalue())
        with tempfile.TemporaryDirectory() as copy:
            for path in E2E.iterdir():
                shutil.copyfile(path, Path(copy) / path.name)
            golden = Path(copy) / "golden_kraken_ledger.jsonl"
            golden.write_bytes(golden.read_bytes()[:-3] + b"X}\n")  # the last ts edited
            for name in ("golden_alerts.jsonl", "golden_reader.json"):  # later in the comparison order
                (Path(copy) / name).write_bytes(b"")
            out = io.StringIO()
            with mock.patch.object(F, "E2E_DIR", Path(copy)):
                self.assertEqual(replay_tool.check(self.produced, out), 1)
            self.assertIn("MISMATCH: golden_kraken_ledger.jsonl differs", out.getvalue())
            (Path(copy) / "kraken_ETHEUR.json").write_bytes(b"[]\n")  # candles are compared first
            out = io.StringIO()
            with mock.patch.object(F, "E2E_DIR", Path(copy)):
                self.assertEqual(replay_tool.check(self.produced, out), 1)
            self.assertIn("MISMATCH: kraken_ETHEUR.json differs", out.getvalue())

    def test_replay_refuses_an_out_dir_that_is_not_new_or_empty(self):
        with tempfile.TemporaryDirectory() as busy:
            (Path(busy) / "radar_state.sqlite").write_bytes(b"")
            errors = io.StringIO()
            with mock.patch.object(F, "run_e2e_scenario") as scenario, contextlib.redirect_stderr(errors):
                self.assertEqual(replay_tool.main(["--out", busy], io.StringIO()), 2)
                self.assertEqual(replay_tool.main(["--out", str(config.STATE_DIR)], io.StringIO()), 2)
            scenario.assert_not_called()
            self.assertIn("is not a new or empty directory", errors.getvalue())
            self.assertIn("is the radar state dir", errors.getvalue())
            self.assertEqual(sorted(p.name for p in Path(busy).iterdir()), ["radar_state.sqlite"])

    def test_reader_payload_facts(self):
        payload = trend_paper_reader_payload(self.state_dir)
        self.assertEqual(payload["state"], "ok")
        self.assertEqual((payload["days_booked"], payload["days_skipped"], payload["skipped_days"]), (60, 0, []))
        self.assertEqual((payload["first_day"], payload["last_day"]), ("2026-10-04", "2026-12-02"))
        self.assertEqual(payload["last_record_ts"], self.results[-1].start.moment.isoformat())
        self.assertEqual([q["quote"] for q in payload["quotes"]], ["EUR", "USDT"])


def trend_paper_reader_payload(state_dir: Path) -> dict:
    return F.e2e_reader_payload(state_dir)


if __name__ == "__main__":
    unittest.main()
