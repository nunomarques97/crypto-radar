"""scripts/audit_paper.py as a subprocess, on temporary SQLite fixtures only.

Every audit runs in a child process. Most runs go through a probe that installs a
``sys.addaudithook`` hook before the script starts: it records any socket, subprocess or
file-writing event and the modules the script imported, so the tests prove from the
outside that the audit uses no network, writes no file and imports no configuration,
runtime, adapter or UI module. SHA-256, mtime and the directory listing prove that a
fixture is left as it was.

Fixture (current schema, written through the stores' own ``ensure_schema``): wallet
1000.00 EUR, three closed plays (an EX-1 LONG stop loss, an EX-1 SHORT target win on a
USD pair and a legacy fixed-hold LONG loss without levels) and two open plays; the pilot
has three NO_TRADE decisions and no position.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_legacy as legacy  # noqa: E402  (pre-EX-1 rows, test fixture)

from radar_v08.adapters import paper_store, pilot_store  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY_ROOT / "scripts" / "audit_paper.py"
T0 = datetime(2026, 9, 29, 10, 15, tzinfo=UTC)
START_CENTS = 100000
SECRET_MARKER = "raw-context-marker-7f3a"
PROBE_TAG = "__AUDIT_PROBE__ "

#: Runs the script under an audit hook and reports, on the last stderr line, the events
#: that would break the read-only/no-network contract and the project modules imported.
PROBE = r"""
import json, os, runpy, sys
events = []
connects = []
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
FORBIDDEN_PREFIXES = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
                      "os.remove", "os.rename", "os.replace", "os.mkdir", "os.rmdir", "os.truncate",
                      "shutil.", "urllib.", "http.", "ftplib.", "smtplib.", "webbrowser.")
def hook(name, args):
    if name.startswith(FORBIDDEN_PREFIXES):
        events.append(name)
    elif name == "open":
        mode, flags = args[1], args[2]
        if isinstance(mode, str) and any(char in mode for char in "wax+"):
            events.append("open-write " + str(args[0]))
        elif mode is None and isinstance(flags, int) and flags & WRITE_FLAGS:
            events.append("open-write " + str(args[0]))
    elif name == "sqlite3.connect":
        connects.append(str(args[0]))
script = sys.argv[1]
sys.argv = [script, *sys.argv[2:]]
sys.addaudithook(hook)
code = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as stop:
    code = stop.code if isinstance(stop.code, int) else 1
sys.stdout.flush()
modules = sorted(name for name in sys.modules
                 if name.split(".")[0] in {"radar_v08", "ui", "radar", "config", "paper_game", "socket", "ssl",
                                           "http", "urllib3", "requests"})
sys.stderr.write("\n__AUDIT_PROBE__ " + json.dumps({"events": events, "connects": connects, "modules": modules}) + "\n")
sys.exit(code)
"""


def utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(path: Path) -> tuple[str, int, int, list[str]]:
    stat = path.stat()
    return sha(path), stat.st_mtime_ns, stat.st_size, sorted(os.listdir(path.parent))


# (direction, quote, stake, fee, bid, ask, entry, exit_policy, setup, close) where close is
# (seconds held, gross, spread, fees, outcome, exit_reason) or None for an open play.
PLAYS: tuple[tuple[Any, ...], ...] = (
    ("LONG", "EUR", 10000, "26", "100", "100.04", T0, "ex1_initial_paper_v1", "CONTINUATION",
     (600, -150, 40, 52, "LOSS", "stop")),
    ("SHORT", "USD", 10000, "26", "50", "50.05", T0.replace(hour=23), "ex1_initial_paper_v1", "BREAKOUT",
     (1800, 400, 50, 52, "WIN", "target")),
    ("LONG", "EUR", 10000, "26", "10", "10.05", T0 + timedelta(hours=2), None, "SQUEEZE_RELEASE",
     (3600, 0, 26, 52, "LOSS", None)),
    ("LONG", "EUR", 5000, "26", "1", "1.0003", T0 + timedelta(hours=3), "ex1_initial_paper_v1", "CONTINUATION", None),
    ("LONG", "EUR", 7000, "26", "2", "2.001", T0 + timedelta(hours=4), "ex1_initial_paper_v1", None, None),
)
REFUSALS = ("quote_currency_mismatch", "quote_currency_mismatch", "unsupported_direction")


def build_current(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        paper_store.ensure_schema(conn)
        pilot_store.ensure_schema(conn)
        conn.execute("INSERT INTO paper_wallet VALUES (1, ?, 'EUR', ?)", (START_CENTS, utc(T0)))
        for index, (direction, quote, stake, fee, bid, ask, entry, policy, setup, close) in enumerate(PLAYS):
            why: dict[str, Any] = {"direction": direction, "features": {"note": SECRET_MARKER}}
            if setup is not None:
                why["setup_type"] = setup
            levels = (policy, "1", "90", "120") if policy else (None, None, None, None)
            hold = 1440 if policy else 60
            play_id = conn.execute(
                "INSERT INTO paper_plays (event_id, run_id, asset, pair, quote, direction, stake_cents, fee_bps, "
                "hold_minutes, entry_bid, entry_ask, entry_ts, due_at, why_json, opened_at, exit_policy, atr, "
                "stop_price, target_price) VALUES (?, 'run-1', 'AAA', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"event-{index}", f"AAA/{quote}", quote, direction, stake, fee, hold, bid, ask, utc(entry),
                 utc(entry + timedelta(minutes=hold)), json.dumps(why), utc(entry), *levels),
            ).lastrowid
            if close is not None:
                seconds, gross, spread, fees, outcome, reason = close
                exit_at = entry + timedelta(seconds=seconds)
                conn.execute(
                    "INSERT INTO paper_closes (play_id, exit_bid, exit_ask, exit_ts, exit_snapshot_id, due_at, "
                    "delay_seconds, gross_mid_cents, spread_cost_cents, fees_cents, net_cents, outcome, closed_at, "
                    "exit_reason, exit_source, record_lag_seconds) VALUES (?, '1', '1', ?, 1, ?, 0, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?)",
                    (play_id, utc(exit_at), utc(exit_at), gross, spread, fees, gross - spread - fees, outcome,
                     utc(exit_at), reason, None if reason is None else "ticker", None if reason is None else 0.5),
                )
        for index, reason in enumerate(REFUSALS):
            conn.execute(
                "INSERT INTO pilot_decisions (event_id, run_id, asset, pair, quote, direction, snapshot_ts, passes, "
                "fee_bps, stress_bps, envelope_sha256, envelope_policy_id, sizing_policy_id, stress_policy_id, "
                "exit_policy_id, outcome, reason, detail, decided_at) VALUES (?, 'run-1', 'AAA', 'AAA/USD', 'USD', "
                "'LONG', ?, 0, '26', '10', ?, 'env', 'size', 'stress', 'ex1', 'NO_TRADE', ?, ?, ?)",
                (f"pilot-{index}", utc(T0), "a" * 64, reason, f"{SECRET_MARKER} detail", utc(T0)),
            )
        conn.commit()
    finally:
        conn.close()


def build_legacy(path: Path) -> None:
    """Paper tables as the pre-EX-1 code created them; no pilot tables."""
    conn = sqlite3.connect(path)
    try:
        legacy.create_legacy_schema(conn, start_balance_cents=START_CENTS, at=T0)
        play = legacy.insert_legacy_play(conn, "legacy-1", "BBB", "LONG", "10", "10.01", T0)
        due = T0 + timedelta(minutes=60)
        legacy.insert_legacy_close(conn, play, "9.9", "9.91", due, due, snapshot_id=1, gross=-100, spread=10,
                                   fees=52, outcome="LOSS", closed_at=due)
        legacy.insert_legacy_play(conn, "legacy-2", "CCC", "SHORT", "5", "5.01", T0 + timedelta(minutes=5))
    finally:
        conn.close()


def build_empty(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    finally:
        conn.close()


class AuditCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def run_audit(self, *args: str, probe: bool = True) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        command = [sys.executable, "-B"]
        command += ["-c", PROBE, str(SCRIPT)] if probe else [str(SCRIPT)]
        result = subprocess.run(
            [*command, *args], capture_output=True, text=True, timeout=60, cwd=self.dir, check=False
        )
        report: dict[str, Any] = {}
        if probe:
            lines = [line for line in result.stderr.splitlines() if line.startswith(PROBE_TAG)]
            self.assertEqual(len(lines), 1, result.stderr)
            report = json.loads(lines[0][len(PROBE_TAG):])
            self.assertEqual(report["events"], [], "network, subprocess or write event during the audit")
            for connect in report["connects"]:
                self.assertTrue(connect.startswith("file:") and connect.endswith("?mode=ro"), connect)
            for module in report["modules"]:
                self.assertTrue(module == "radar_v08" or module.startswith("radar_v08.domain"), module)
        return result, report

    def audit_json(self, db: Path) -> dict[str, Any]:
        result, probe = self.run_audit("--db", str(db), "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(probe["connects"]), 1)
        self.assertNotIn(SECRET_MARKER, result.stdout)
        return json.loads(result.stdout)


class TestRefusals(AuditCase):
    def test_missing_db_argument_exits_with_usage(self) -> None:
        result, _ = self.run_audit(probe=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertIn("--db", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(os.listdir(self.dir), [])

    def test_missing_database_is_refused_and_not_created(self) -> None:
        target = self.dir / "absent.sqlite"
        for probe in (False, True):
            for fmt in ("json", "text"):
                result, report = self.run_audit("--db", str(target), "--format", fmt, probe=probe)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not found", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(target.exists())
                self.assertEqual(os.listdir(self.dir), [])
                if probe:
                    self.assertEqual(report["connects"], [], "no connection attempt on a missing file")

    def test_uri_parameters_in_the_path_cannot_open_for_writing(self) -> None:
        for name in ("new.sqlite?mode=rwc", "file:new.sqlite?mode=rwc", "new.sqlite#x"):
            result, _ = self.run_audit("--db", str(self.dir / name) if "file:" not in name else name)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(os.listdir(self.dir), [], name)

    def test_directory_and_non_database_file_are_refused_unchanged(self) -> None:
        result, _ = self.run_audit("--db", str(self.dir))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(os.listdir(self.dir), [])
        junk = self.dir / "junk.sqlite"
        junk.write_bytes(b"this is not a SQLite database " * 64)
        before = snapshot(junk)
        result, _ = self.run_audit("--db", str(junk), "--format", "json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read the database", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(snapshot(junk), before)


class TestReadOnly(AuditCase):
    def test_fixture_bytes_mtime_and_directory_unchanged(self) -> None:
        db = self.dir / "fixture.sqlite"
        build_current(db)
        before = snapshot(db)
        for probe in (False, True):
            for fmt in ("json", "text"):
                result, _ = self.run_audit("--db", str(db), "--format", fmt, probe=probe)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout)
                self.assertEqual(snapshot(db), before)

    def test_help_exits_zero(self) -> None:
        result, _ = self.run_audit("--help", probe=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--db", result.stdout)
        self.assertIn("--format", result.stdout)


class TestImportBoundary(AuditCase):
    def test_probe_detects_writes_sockets_and_forbidden_imports(self) -> None:
        # Negative control: the probe must see what a misbehaving script would do.
        control = self.dir / "control.py"
        control.write_text(
            "import socket\nimport radar_v08.adapters.paper_store\n"
            "open('written.txt', 'w').close()\nsocket.socket().close()\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", PROBE, str(control)], capture_output=True, text=True, timeout=60,
            cwd=self.dir, env={**os.environ, "PYTHONPATH": str(REPOSITORY_ROOT)}, check=False,
        )
        line = next(line for line in result.stderr.splitlines() if line.startswith(PROBE_TAG))
        report = json.loads(line[len(PROBE_TAG):])
        self.assertTrue(any(event.startswith("open-write") for event in report["events"]), report)
        self.assertIn("socket.__new__", report["events"])
        self.assertIn("socket", report["modules"])
        self.assertIn("radar_v08.adapters.paper_store", report["modules"])

    def test_source_imports_only_stdlib_and_domain(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                modules.append(node.module or "")
        self.assertIn("radar_v08.domain", modules)
        for module in modules:
            if module.split(".")[0] in sys.stdlib_module_names:
                continue
            self.assertTrue(module.startswith("radar_v08.domain"), module)
        self.assertNotIn("__import__", SCRIPT.read_text(encoding="utf-8"))
        self.assertNotIn("importlib", SCRIPT.read_text(encoding="utf-8"))

    def test_runtime_modules_exclude_config_runtime_adapters_ui_and_network(self) -> None:
        db = self.dir / "fixture.sqlite"
        build_current(db)
        for fmt in ("json", "text"):
            result, probe = self.run_audit("--db", str(db), "--format", fmt)
            self.assertEqual(result.returncode, 0, result.stderr)
            modules = set(probe["modules"])
            self.assertIn("radar_v08.domain.paper", modules)
            for forbidden in ("radar_v08.config", "radar_v08.adapters", "radar_v08.paper_game",
                              "radar_v08.pilot_shadow", "radar_v08.adapters.kraken_private_read", "ui", "radar",
                              "socket", "ssl"):
                self.assertNotIn(forbidden, modules)


class TestCurrentFixture(AuditCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = self.dir / "fixture.sqlite"
        build_current(self.db)
        self.report = self.audit_json(self.db)
        self.paper = self.report["paper"]

    def test_header_units_and_coverage(self) -> None:
        self.assertEqual(self.report["format"], "paper_audit_v1")
        as_of = datetime.fromisoformat(self.report["as_of"])
        self.assertIsNotNone(as_of.tzinfo)
        for unit in ("money", "paper_money_basis", "fees", "open_plays", "groups", "spread", "entry_hour", "duration"):
            self.assertIn(unit, self.report["units"])
        self.assertIn("FX excluded", self.report["units"]["paper_money_basis"])
        self.assertIn("ASSUMED", self.report["units"]["fees"])
        self.assertTrue(all(entry["available"] for entry in self.report["coverage"]), self.report["coverage"])
        self.assertEqual(self.paper["currency"], "EUR")

    def test_cashflow_reconciles_with_fixture_rows(self) -> None:
        closes = [play[9] for play in PLAYS if play[9] is not None]
        gross = sum(close[1] for close in closes)
        spread = sum(close[2] for close in closes)
        fees = sum(close[3] for close in closes)
        net = gross - spread - fees
        open_stakes = sum(play[2] for play in PLAYS if play[9] is None)
        flow = self.paper["cashflow"]
        self.assertEqual(
            flow,
            {
                "start_balance_cents": START_CENTS,
                "realized_gross_mid_cents": gross,
                "realized_spread_cost_cents": spread,
                "realized_fees_cents": fees,
                "realized_net_cents": net,
                "open_stake_cents": open_stakes,
                "realized_balance_cents": START_CENTS + net,
                "free_cash_cents": START_CENTS + net - open_stakes,
                "identity_gross_minus_spread_minus_fees_equals_net": True,
            },
        )
        self.assertEqual((gross, spread, fees, net, open_stakes), (250, 116, 156, -22, 12000))
        # Cross-check against the database itself on an independent read-only connection.
        conn = sqlite3.connect(self.db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT sum(gross_mid_cents), sum(spread_cost_cents), sum(fees_cents), sum(net_cents) FROM paper_closes"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (gross, spread, fees, net))

    def test_counts_open_versus_closed(self) -> None:
        self.assertEqual(
            self.paper["counts"], {"plays": 5, "open": 2, "closed": 3, "wins": 1, "losses": 2, "flats": 0}
        )
        self.assertEqual(self.paper["quote_currencies"], {"EUR": 4, "USD": 1})
        self.assertEqual(self.paper["assumed_fee_bps_per_leg"], {"26": 5})

    def test_groups(self) -> None:
        groups = self.paper["groups"]

        def summary(name: str) -> dict[str, tuple[int, int, int, int]]:
            self.assertTrue(groups[name]["available"])
            return {key: (g["plays"], g["open"], g["closed"], g["net_cents"]) for key, g in groups[name]["groups"].items()}

        self.assertEqual(
            summary("exit_policy"), {"ex1_initial_paper_v1": (4, 2, 2, 56), "none_recorded": (1, 0, 1, -78)}
        )
        self.assertEqual(summary("direction"), {"LONG": (4, 2, 2, -320), "SHORT": (1, 0, 1, 298)})
        self.assertEqual(
            summary("setup"),
            {
                "BREAKOUT": (1, 0, 1, 298),
                "CONTINUATION": (2, 1, 1, -242),
                "SQUEEZE_RELEASE": (1, 0, 1, -78),
                "none_recorded": (1, 1, 0, 0),
            },
        )
        # 4 bps and 3 bps; 10 bps; 50 bps and 5 bps (2.001 / 2 - 1).
        self.assertEqual(
            summary("spread_bucket"),
            {"le_5bps": (3, 2, 1, -242), "gt_5_le_20bps": (1, 0, 1, 298), "gt_20bps": (1, 0, 1, -78)},
        )
        self.assertEqual(
            summary("entry_hour_utc"),
            {"10": (1, 0, 1, -242), "12": (1, 0, 1, -78), "13": (1, 1, 0, 0), "14": (1, 1, 0, 0),
             "23": (1, 0, 1, 298)},
        )
        for name, group in groups.items():
            self.assertEqual(sum(g["net_cents"] for g in group["groups"].values()), -22, name)
            self.assertEqual(sum(g["plays"] for g in group["groups"].values()), 5, name)

    def test_hold_durations_by_exit_reason(self) -> None:
        by_reason = self.paper["hold_durations_by_exit_reason"]
        self.assertTrue(by_reason["available"])
        self.assertEqual(by_reason["invalid_times"], 0)
        stats = {key: (value["closed"], value["mean_seconds"]) for key, value in by_reason["groups"].items()}
        self.assertEqual(stats, {"none_recorded": (1, 3600.0), "stop": (1, 600.0), "target": (1, 1800.0)})
        overall = self.paper["hold_durations_all_closed"]["groups"]["all_closed"]
        self.assertEqual((overall["closed"], overall["mean_seconds"], overall["median_seconds"]), (3, 2000.0, 1800.0))

    def test_pilot_refusals_by_reason(self) -> None:
        pilot = self.report["pilot"]
        self.assertTrue(pilot["available"])
        self.assertEqual(pilot["decisions"], {"total": 3, "by_outcome": {"NO_TRADE": 3}})
        self.assertEqual(pilot["no_trade_by_reason"], {"quote_currency_mismatch": 2, "unsupported_direction": 1})
        self.assertEqual(pilot["positions"], {"available": True, "total": 0, "open": 0, "closed": 0})
        self.assertNotIn("envelope", json.dumps(pilot))

    def test_text_format_is_readable_and_hides_raw_context(self) -> None:
        result, _ = self.run_audit("--db", str(self.db), "--format", "text")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout
        self.assertNotIn(SECRET_MARKER, text)
        self.assertNotIn("why_json", text)
        for expected in ("as of", "realized_net: -0.22 EUR", "open_stake: 120.00 EUR", "free_cash: 879.78 EUR",
                         "NO_TRADE by reason", "quote_currency_mismatch", "by spread_bucket", "stop: closed 1"):
            self.assertIn(expected, text)


class TestLegacyAndEmpty(AuditCase):
    def test_legacy_database_reports_missing_columns_and_tables_not_zero(self) -> None:
        db = self.dir / "legacy.sqlite"
        build_legacy(db)
        before = snapshot(db)
        report = self.audit_json(db)
        self.assertEqual(snapshot(db), before)
        paper = report["paper"]
        self.assertTrue(paper["available"])
        self.assertEqual(paper["counts"], {"plays": 2, "open": 1, "closed": 1, "wins": 0, "losses": 1, "flats": 0})
        self.assertEqual(paper["cashflow"]["realized_net_cents"], -162)
        self.assertEqual(
            paper["groups"]["exit_policy"],
            {"available": False, "reason": "missing_column", "table": "paper_plays", "column": "exit_policy"},
        )
        self.assertEqual(
            paper["hold_durations_by_exit_reason"],
            {"available": False, "reason": "missing_column", "table": "paper_closes", "column": "exit_reason"},
        )
        self.assertEqual(
            report["pilot"], {"available": False, "unavailable": {"available": False, "reason": "missing_table",
                                                                  "table": "pilot_decisions"}}
        )
        unavailable = {(e["section"], e.get("column") or e["table"]) for e in report["coverage"] if not e["available"]}
        self.assertEqual(
            unavailable,
            {("paper.groups.exit_policy", "exit_policy"), ("paper.hold_durations_by_exit_reason", "exit_reason"),
             ("pilot", "pilot_decisions")},
        )

    def test_empty_database_is_unavailable_everywhere(self) -> None:
        db = self.dir / "empty.sqlite"
        build_empty(db)
        before = snapshot(db)
        report = self.audit_json(db)
        self.assertEqual(snapshot(db), before)
        self.assertEqual(
            report["paper"],
            {"available": False, "unavailable": {"available": False, "reason": "missing_table", "table": "paper_wallet"}},
        )
        self.assertFalse(report["pilot"]["available"])
        self.assertEqual(report["pilot"]["unavailable"]["reason"], "missing_table")
        self.assertFalse(any(entry["available"] for entry in report["coverage"]))
        self.assertNotIn("cashflow", json.dumps(report))
        result, _ = self.run_audit("--db", str(db), "--format", "text")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unavailable (missing_table: paper_wallet)", result.stdout)
        self.assertIn("unavailable (missing_table: pilot_decisions)", result.stdout)

    def test_pilot_without_positions_table_is_unavailable_not_zero(self) -> None:
        db = self.dir / "partial.sqlite"
        build_legacy(db)
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE pilot_decisions (decision_id INTEGER PRIMARY KEY, outcome TEXT, reason TEXT)")
            conn.execute("INSERT INTO pilot_decisions (outcome, reason) VALUES ('NO_TRADE', 'invalid_quote')")
            conn.commit()
        finally:
            conn.close()
        pilot = self.audit_json(db)["pilot"]
        self.assertEqual(pilot["no_trade_by_reason"], {"invalid_quote": 1})
        self.assertEqual(pilot["positions"], {"available": False, "reason": "missing_table", "table": "pilot_positions"})

    def test_wallet_without_row_leaves_balance_unavailable(self) -> None:
        db = self.dir / "nowallet.sqlite"
        conn = sqlite3.connect(db)
        try:
            paper_store.ensure_schema(conn)
        finally:
            conn.close()
        paper = self.audit_json(db)["paper"]
        self.assertTrue(paper["available"])
        self.assertIsNone(paper["cashflow"]["start_balance_cents"])
        self.assertIsNone(paper["cashflow"]["realized_balance_cents"])
        self.assertIsNone(paper["cashflow"]["free_cash_cents"])
        self.assertEqual(paper["cashflow"]["unavailable"]["reason"], "missing_row")


if __name__ == "__main__":
    unittest.main()
