"""Paper game service (radar_v08/paper_game.py) and its RADAR_PAPER_* configuration.

Temporary SQLite files only; the real radar_state.sqlite is never opened. Configuration
cases run in a child process, same idiom as tests/test_config_outcome_tracking.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from radar_v08 import config, paper_game
from radar_v08.adapters import paper_store as ps
from radar_v08.domain import paper
from radar_v08.store import SnapshotStore

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
D = Decimal
T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
TERMS = ps.PlayTerms(stake=D("100"), fee_bps=D("26"), max_open=3)
DUE = T0 + timedelta(hours=24)
NO_SKIPS = {
    "already_recorded": 0,
    "no_direction": 0,
    "invalid_price": 0,
    "no_valid_atr": 0,
    "invalid_levels": 0,
    "asset_already_open": 0,
    "max_open": 0,
    "insufficient_cash": 0,
}


def counts(opened: int = 0, closed: int = 0, pending: int = 0, **skipped: int) -> dict[str, object]:
    return {"opened": opened, "closed": closed, "pending": pending, "skipped": {**NO_SKIPS, **skipped}}


def candidate(event_id: str, asset: str = "BTC", *, direction: str = "LONG", bid: object = 99.0,
              ask: object = 101.0, atr: object = 1.5, atr_pair: str | None = None) -> ps.PaperCandidate:
    """ATR 1.5 on the entry pair: LONG stop 98 / target 107 (ask 101), SHORT stop 102 / target 93 (bid 99)."""
    return ps.PaperCandidate(
        event_id=event_id, run_id="run-1", asset=asset, pair=f"{asset}/EUR", quote="EUR", direction=direction,
        bid=bid, ask=ask, snapshot_ts=T0.isoformat(), status="online",
        why=paper_game.build_why(setup_type="BREAKOUT", direction=direction, scores={"final": 0.7}, features={}),
        atr=atr, atr_pair=f"{asset}/EUR" if atr_pair is None else atr_pair,
    )


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="paper-game-")
        path = os.path.join(self._tmp.name, "radar_state.sqlite")
        SnapshotStore(path).close()
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def add_spot(self, pair: str, ts: datetime, bid: object, ask: object, status: object = "online") -> None:
        self.conn.execute(
            "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES (?, ?, 'EUR', ?, ?, ?, ?)",
            (pair.split("/")[0], pair, ts.isoformat(), bid, ask, status),
        )
        self.conn.commit()


class TestService(ServiceCase):
    def test_first_call_prepares_tables_and_wallet_from_config(self):
        self.assertEqual(paper_game.settle_due(self.conn, T0), counts())
        wallet = ps.read_wallet(self.conn)
        assert wallet is not None
        self.assertEqual((wallet.start_balance, wallet.currency), (config.PAPER_START_BALANCE_EUR, "EUR"))

    def test_default_terms_come_from_config(self):
        terms = paper_game.default_terms()
        self.assertEqual(terms.stake, config.PAPER_STAKE_EUR)
        self.assertFalse(hasattr(terms, "hold_minutes"))  # the hold is the EX-1 policy, not a term
        self.assertEqual(config.PAPER_HOLD_MINUTES, 1440)
        self.assertEqual(terms.max_open, config.PAPER_MAX_OPEN)
        self.assertEqual(terms.fee_bps, D(repr(config.UNCALIBRATED_FEES["spot_taker_bps"])))

    def test_open_counts_and_idempotent_rerun(self):
        batch = [candidate("e1", "BTC"), candidate("e2", "BTC"), candidate("e3", "ETH", direction="NONE"),
                 candidate("e4", "SOL", bid=float("nan")), candidate("e5", "XRP", direction="SHORT"),
                 candidate("e6", "ADA"), candidate("e7", "DOT")]
        first = paper_game.open_for_events(self.conn, batch, T0, terms=TERMS)
        self.assertEqual(first, counts(opened=3, asset_already_open=1, no_direction=1, invalid_price=1, max_open=1))
        changes = self.conn.total_changes
        again = paper_game.open_for_events(self.conn, batch, T0, terms=TERMS)
        self.assertEqual(again["opened"], 0)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(len(ps.read_plays(self.conn)), 3)

    def test_atr_and_level_skips_are_counted(self):
        batch = [candidate("e1", "BTC", atr=None), candidate("e2", "ETH", atr=float("nan")),
                 candidate("e3", "SOL", atr=0.0), candidate("e4", "XRP", atr_pair="XRP/USD"),
                 candidate("e5", "ADA", atr=60.0), candidate("e6", "DOT", direction="SHORT", atr=30.0)]
        self.assertEqual(paper_game.open_for_events(self.conn, batch, T0, terms=TERMS),
                         counts(no_valid_atr=4, invalid_levels=2))
        self.assertEqual(ps.read_plays(self.conn), ())

    def test_insufficient_cash(self):
        ps.ensure_schema(self.conn)
        ps.ensure_wallet(self.conn, start_balance=D("150"), currency="EUR", now=T0)
        result = paper_game.open_for_events(self.conn, [candidate("e1", "BTC"), candidate("e2", "ETH")], T0, terms=TERMS)
        self.assertEqual(result, counts(opened=1, insufficient_cash=1))

    def test_pending_then_late_time_close_with_delay(self):
        paper_game.open_for_events(self.conn, [candidate("e1", "BTC")], T0, terms=TERMS)
        (play,) = ps.read_plays(self.conn)
        self.assertEqual((play.hold_minutes, play.due_at, play.stop, play.target), (1440, DUE, D("98.0"), D("107.0")))
        due = DUE
        self.add_spot("BTC/EUR", due - timedelta(hours=1), 100.0, 101.0)  # inside the levels, before due
        self.assertEqual(paper_game.settle_due(self.conn, due - timedelta(seconds=1)), counts())
        self.add_spot("BTC/EUR", due + timedelta(seconds=10), 0.0, 101.0)  # invalid price
        self.add_spot("BTC/EUR", due + timedelta(seconds=20), 90.0, 91.0, "maintenance")  # not online
        self.add_spot("BTC/EUR", due + timedelta(seconds=30), None, None)  # missing
        now = due + timedelta(minutes=2)
        self.assertEqual(paper_game.settle_due(self.conn, now), counts(pending=1))
        changes = self.conn.total_changes
        self.assertEqual(paper_game.settle_due(self.conn, now), counts(pending=1))
        self.assertEqual(self.conn.total_changes, changes)  # pending writes nothing
        self.assertEqual(ps.read_closes(self.conn), ())

        self.add_spot("BTC/EUR", due + timedelta(minutes=5), 104.0, 106.0)
        self.assertEqual(paper_game.settle_due(self.conn, due + timedelta(minutes=6)), counts(closed=1))
        (close,) = ps.read_closes(self.conn)
        self.assertIs(close.exit_reason, paper.ExitReason.TIME)
        self.assertEqual(close.delay_seconds, 300.0)
        self.assertEqual(close.exit_ts, due + timedelta(minutes=5))
        # Real spread of both legs plus UNCALIBRATED_FEES' spot taker fee per leg.
        expected = paper.settle(paper.Direction.LONG, D("100"), D("26"), paper.Quote(D("99.0"), D("101.0")),
                                paper.Quote(D("104.0"), D("106.0")))
        self.assertEqual((close.gross_mid, close.spread_cost, close.fees, close.net),
                         (expected.gross_mid, expected.spread_cost, expected.fees, expected.net))
        self.assertEqual(ps.current_balance(self.conn), config.PAPER_START_BALANCE_EUR + close.net)
        changes = self.conn.total_changes
        self.assertEqual(paper_game.settle_due(self.conn, due + timedelta(minutes=6)), counts())
        self.assertEqual(self.conn.total_changes, changes)

    def test_settle_frees_cash_and_slot_for_a_new_play(self):
        batch = [candidate("e1", "BTC"), candidate("e2", "ETH"), candidate("e3", "SOL")]
        paper_game.open_for_events(self.conn, batch, T0, terms=TERMS)
        self.assertEqual(paper_game.open_for_events(self.conn, [candidate("e4", "BTC")], T0, terms=TERMS),
                         counts(asset_already_open=1))
        hit = T0 + timedelta(minutes=5)
        self.add_spot("BTC/EUR", hit, 94.0, 96.0)  # through the BTC stop (98)
        self.assertEqual(paper_game.settle_due(self.conn, hit), counts(closed=1))
        later = ps.PaperCandidate(**{**{f: getattr(candidate("e5", "BTC"), f) for f in ps.PaperCandidate.__slots__},
                                     "snapshot_ts": hit.isoformat()})
        self.assertEqual(paper_game.open_for_events(self.conn, [later], hit, terms=TERMS), counts(opened=1))

    def test_balance_reconciles_with_recorded_nets(self):
        paper_game.open_for_events(self.conn, [candidate("e1", "BTC"), candidate("e2", "ETH", direction="SHORT")],
                                   T0, terms=TERMS)
        hit = T0 + timedelta(minutes=5)
        self.add_spot("BTC/EUR", hit, 94.0, 96.0)  # BTC LONG stop (bid <= 98)
        self.add_spot("ETH/EUR", hit, 89.0, 91.0)  # ETH SHORT target (ask <= 93)
        paper_game.settle_due(self.conn, hit)
        closes = ps.read_closes(self.conn)
        self.assertEqual([close.exit_reason for close in closes], [paper.ExitReason.STOP, paper.ExitReason.TARGET])
        nets = [close.net for close in closes]
        self.assertEqual(ps.current_balance(self.conn), config.PAPER_START_BALANCE_EUR + sum(nets, D(0)))

    def test_build_why_keeps_only_recorded_json_values(self):
        why = paper_game.build_why(
            setup_type="SQUEEZE_RELEASE", direction="SHORT",
            scores={"final": 0.9, "bad": float("inf")},
            features={"rvol": 2.5, "flags": ["a", 1], "obj": object(), 3: "non-string key"},
        )
        self.assertEqual(why, {
            "setup_type": "SQUEEZE_RELEASE", "direction": "SHORT",
            "scores": {"final": 0.9, "bad": None}, "features": {"rvol": 2.5, "flags": ["a", 1]},
        })
        json.dumps(why, allow_nan=False)


_READ_CONFIG = (
    "import json; from radar_v08 import config, paper_game; "
    "print(json.dumps([config.RADAR_PAPER_ENABLED, str(config.PAPER_START_BALANCE_EUR), str(config.PAPER_STAKE_EUR), "
    "config.PAPER_MAX_OPEN, config.PAPER_HOLD_MINUTES, str(paper_game.default_terms().fee_bps)]))"
)


def _child(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="paper-config-state-") as state_dir:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("RADAR_")}
        environment["RADAR_STATE_DIR"] = state_dir
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.update(extra_env)
        return subprocess.run(
            [sys.executable, "-c", _READ_CONFIG], cwd=REPOSITORY_ROOT, env=environment,
            capture_output=True, text=True, timeout=60,
        )


def _read(extra_env: dict[str, str] | None = None) -> list[object]:
    completed = _child(extra_env or {})
    if completed.returncode != 0:
        raise AssertionError(f"child failed: {completed.stderr}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


class TestPaperConfig(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(_read(), [True, "1000", "100", 3, 1440, "26.0"])

    def test_switch_off_values(self):
        for value in ("0", "false", "False"):
            with self.subTest(value=value):
                self.assertFalse(_read({"RADAR_PAPER_ENABLED": value})[0])
        self.assertTrue(_read({"RADAR_PAPER_ENABLED": "yes"})[0])

    def test_overrides_and_fee_from_uncalibrated_fees(self):
        values = _read({
            "RADAR_PAPER_START_BALANCE_EUR": "500.50", "RADAR_PAPER_STAKE_EUR": "25",
            "RADAR_PAPER_MAX_OPEN": "5", "RADAR_PAPER_HOLD_MINUTES": "15", "RADAR_FEE_SPOT_TAKER_BPS": "10",
        })
        # The hold is fixed by the EX-1 policy: RADAR_PAPER_HOLD_MINUTES is no longer read.
        self.assertEqual(values, [True, "500.50", "25", 5, 1440, "10.0"])

    def test_hold_minutes_variable_is_ignored(self):
        for value in ("15", "-1", "abc"):
            with self.subTest(value=value):
                self.assertEqual(_read({"RADAR_PAPER_HOLD_MINUTES": value})[4], 1440)

    def test_invalid_values_refuse_to_load(self):
        for name, value in (
            ("RADAR_PAPER_START_BALANCE_EUR", "abc"), ("RADAR_PAPER_START_BALANCE_EUR", "0"),
            ("RADAR_PAPER_STAKE_EUR", "10.001"), ("RADAR_PAPER_STAKE_EUR", "NaN"), ("RADAR_PAPER_STAKE_EUR", "-5"),
            ("RADAR_PAPER_MAX_OPEN", "0"), ("RADAR_PAPER_MAX_OPEN", "2.5"),
        ):
            with self.subTest(name=name, value=value):
                completed = _child({name: value})
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(f"ValueError: {name}", completed.stderr)

    def test_parsers_in_process(self):
        self.assertEqual(config.parse_paper_money("X", None, "1000"), D("1000"))
        self.assertEqual(config.parse_paper_money("X", " 12.5 ", "1"), D("12.5"))
        self.assertEqual(config.parse_paper_count("X", None, 3), 3)
        self.assertEqual(config.parse_paper_count("X", " 7 ", 3), 7)
        for raw in ("", "inf", "1e400", "0.001", "1000000000.01"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                config.parse_paper_money("X", raw, "1")
        for raw in ("", "-3", "٣", "1.0"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                config.parse_paper_count("X", raw, 3)


if __name__ == "__main__":
    unittest.main()
