"""Paper game ("Jogo da IA") wired into the heartbeat - pretend money only.

Each cycle, behind `config.RADAR_PAPER_ENABLED`, `run_heartbeat` closes the paper
plays whose EX-1 stop, target or 24 h limit shows in the recorded spot quotes (a legacy
play: its first valid quote after its due time), then opens one play per LONG/SHORT
event this cycle created, at the primary spot bid/ask written to `spot_snapshots` at
the cycle's `ts`, with levels from the event's L2 5-minute ATR of the same pair. It only adds the run record's `paper` object and its own `paper_ms`
latency stage: the output and every other run-record key are unchanged, on, off or
failing.

These tests reuse the fake-Kraken heartbeat harness of `test_integrity_wiring` and
`test_outcome_label_wiring` (no socket, injected clock, disposable SQLite in a temp
dir, run records captured in memory instead of runs.jsonl). No real radar file is
written and no running radar process is touched.
"""

import ast
import functools
import itertools
import os
import sqlite3
import sys
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_DIR)
sys.path.insert(0, TESTS_DIR)

import paper_legacy as legacy  # noqa: E402  (pre-EX-1 rows)
import test_integrity_wiring as wiring  # noqa: E402  (fake Kraken heartbeat harness)
import test_outcome_label_wiring as lwiring  # noqa: E402  (synced later cycles)

from radar_v08 import config, events, heartbeat, paper_game, paper_monitor  # noqa: E402
from radar_v08.adapters import paper_store  # noqa: E402
from radar_v08.domain import paper  # noqa: E402
from radar_v08.http_client import GuardedSession  # noqa: E402
from radar_v08.kraken_spot import get_asset_pairs as real_get_asset_pairs  # noqa: E402
from radar_v08.store import SnapshotStore, SpotSnapshotInput  # noqa: E402

T0 = wiring.T0
BTC_PAIR = wiring.ASSETS["BTC"][0]
ETH_PAIR = wiring.ASSETS["ETH"][0]
D = Decimal
LOGGER = "radar_v08.test_integrity_wiring"  # the logger the harness gives the heartbeat


def ticker_row(bid, ask):
    """A fake Kraken spot ticker row with this exact touch."""
    row = wiring._ticker_row((bid + ask) / 2)
    row["b"] = [str(bid), "1", "1.000"]
    row["a"] = [str(ask), "1", "1.000"]
    return row


def without_network_ms(value):
    """`data_quality.http.*.network_ms` is real wall time spent in the fake transport,
    different on every run whatever the paper switch; every other value is compared."""
    if isinstance(value, dict):
        return {key: None if key == "network_ms" else without_network_ms(item) for key, item in value.items()}
    if isinstance(value, list):
        return [without_network_ms(item) for item in value]
    return value


def without_volatile(record):
    return without_network_ms({key: value for key, value in record.items() if key not in ("paper", "latency_ms")})


def zero_counts(enabled):
    skipped = {reason.value: 0 for reason in paper.SkipReason}
    skipped["no_spot_market"] = 0
    return {
        "enabled": enabled, "opened": 0, "closed": 0, "pending": 0,
        "skipped": skipped, "open_now": 0, "failures": 0,
    }


class PaperHeartbeatBase(lwiring.LabelWiringBase):
    def setUp(self):
        super().setUp()
        for name, value in (
            ("RADAR_PAPER_ENABLED", True),
            ("PAPER_START_BALANCE_EUR", D("1000")),
            ("PAPER_STAKE_EUR", D("100")),
            ("PAPER_MAX_OPEN", 3),
        ):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fee_bps = D(repr(float(config.UNCALIBRATED_FEES["spot_taker_bps"])))

    def record(self, index=-1):
        return self.run_records[index]

    def rows(self, sql, *args, db_path=None):
        with sqlite3.connect(db_path or self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, args)]

    def plays(self):
        return {row["asset"]: row for row in self.rows("SELECT * FROM paper_plays ORDER BY play_id")}

    def closes(self):
        return {
            row["asset"]: row
            for row in self.rows(
                "SELECT c.*, p.asset FROM paper_closes c JOIN paper_plays p USING (play_id) ORDER BY close_id"
            )
        }

    def snapshot(self, pair, ts):
        rows = self.rows("SELECT bid, ask, status FROM spot_snapshots WHERE pair = ? AND ts = ?", pair, ts)
        self.assertEqual(len(rows), 1, (pair, ts))
        return rows[0]

    def paper_tables(self, db_path=None):
        with sqlite3.connect(db_path or self.db_path) as conn:
            return paper_store.schema_present(conn), sorted(
                name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'paper%'")
            )

    def plant_snapshot(self, pair, asset, ts, bid, ask, status):
        """One spot_snapshots row stamped `ts`, bypassing the heartbeat's own fetch."""
        self.store.insert_spot_snapshots_batch([
            SpotSnapshotInput(
                asset=asset, pair=pair, quote="USD", ts=ts.isoformat(),
                last=(bid + ask) / 2, bid=bid, ask=ask, bid_size=1.0, ask_size=1.0,
                volume_today=0.0, volume_24h=0.0, vwap_today=bid, vwap_24h=bid, trades_today=0,
                trades_24h=0, high_today=ask, low_today=bid, high_24h=ask, low_24h=bid,
                open_today=bid, status=status,
            )
        ])

    def open_first_plays(self, kraken):
        """A full cycle at T0 creating a LONG event for each asset; returns its ts."""
        output = self.run_full_cycle(kraken, wiring.StepClock(start=T0))
        self.assertEqual(output["funnel"]["events_created"], len(kraken.assets))
        return self.record()["ts"]


class TestEntryOnNewEvents(PaperHeartbeatBase):
    def test_a_full_cycle_opens_each_new_long_event_at_the_ask_of_its_own_snapshot(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        ts = self.open_first_plays(kraken)

        record = self.record()
        expected = zero_counts(True)
        expected.update(opened=2, open_now=2)
        self.assertEqual(record["paper"], expected)
        self.assertIn("paper_ms", record["latency_ms"])

        plays = self.plays()
        self.assertEqual(sorted(plays), ["BTC", "ETH"])
        events_by_asset = {row["asset"]: row for row in self.events()}
        for asset, pair in (("BTC", BTC_PAIR), ("ETH", ETH_PAIR)):
            with self.subTest(asset=asset):
                play = plays[asset]
                snap = self.snapshot(pair, ts)
                self.assertEqual(play["event_id"], events_by_asset[asset]["event_id"])
                self.assertEqual(play["run_id"], record["run_id"])
                self.assertEqual(play["pair"], pair)
                self.assertEqual(play["quote"], "USD")
                self.assertEqual(play["direction"], "LONG")
                self.assertEqual(D(play["entry_ask"]), D(repr(snap["ask"])))
                self.assertEqual(D(play["entry_bid"]), D(repr(snap["bid"])))
                entry_ts = datetime.fromisoformat(play["entry_ts"])
                self.assertEqual(entry_ts, datetime.fromisoformat(ts))
                self.assertEqual(datetime.fromisoformat(play["due_at"]), entry_ts + timedelta(hours=24))
                self.assertEqual(play["stake_cents"], 10000)
                self.assertEqual(D(play["fee_bps"]), self.fee_bps)
                self.assertEqual(play["hold_minutes"], 1440)
                # EX-1 levels from the event's own L2 ATR (5-minute ATR14 of the same pair).
                atr = D(play["atr"])
                self.assertGreater(atr, 0)
                self.assertEqual(play["exit_policy"], "ex1_initial_paper_v1")
                self.assertEqual(D(play["stop_price"]), D(play["entry_ask"]) - 2 * atr)
                self.assertEqual(D(play["target_price"]), D(play["entry_ask"]) + 4 * atr)
        btc = paper_store.read_plays(self.store._conn)[0]
        self.assertEqual(btc.entry_ask, D("60000.5"))
        self.assertEqual(btc.why["features"]["l2"]["atr_5m"], float(btc.atr))
        self.assertEqual(btc.why["setup_type"], "BREAKOUT")
        self.assertEqual(btc.why["direction"], "LONG")
        self.assertEqual(btc.why["scores"]["opportunity_score"], 80.0)
        self.assertEqual(btc.why["scores"]["tradeability_score"], 80.0)
        self.assertEqual(btc.why["scores"]["anomaly_score"], 5.0)
        self.assertIn("confidence", btc.why["scores"])
        self.assertIsInstance(btc.why["features"]["l1"], dict)
        self.assertIsInstance(btc.why["features"]["l2"], dict)
        wallet = paper_store.read_wallet(self.store._conn)
        self.assertEqual((wallet.start_balance, wallet.currency), (D("1000"), "EUR"))

    def test_a_short_event_enters_at_the_bid(self):
        kraken = wiring.FakeKraken(assets=("BTC",))
        with mock.patch.object(wiring.l2, "classify_setup", lambda _l1, _l2f: wiring.SetupResult("REVERSAL", "SHORT", [])):
            ts = self.open_first_plays(kraken)
        play = self.plays()["BTC"]
        self.assertEqual(play["direction"], "SHORT")
        self.assertEqual(D(play["entry_bid"]), D("59999.5"))
        self.assertEqual(D(play["entry_bid"]), D(repr(self.snapshot(BTC_PAIR, ts)["bid"])))
        self.assertEqual(datetime.fromisoformat(play["entry_ts"]), datetime.fromisoformat(ts))
        # Mirrored on the bid: stop above, target 2R below.
        atr = D(play["atr"])
        self.assertEqual(D(play["stop_price"]), D("59999.5") + 2 * atr)
        self.assertEqual(D(play["target_price"]), D("59999.5") - 4 * atr)

    def test_a_deduplicated_event_never_opens_a_play(self):
        # The pattern of test_outcome_wiring's dedup test: one clock that keeps moving
        # across both full cycles, the corrected clock off, so the second cycle routes
        # again and create_event_if_new finds the first cycle's still-PENDING events.
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        clock = wiring.StepClock()
        with mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", False):
            self.run_full_cycle(kraken, clock)
            self.assertEqual(len(self.plays()), 2)

            created_flags = []
            real_create = events.create_event_if_new

            def spy_create(*args, **kwargs):
                event_id, created = real_create(*args, **kwargs)
                created_flags.append(created)
                return event_id, created

            opened_for = []
            real_open = paper_game.open_for_events

            def spy_open(conn, candidates, now, **kwargs):
                opened_for.extend(candidate.event_id for candidate in candidates)
                return real_open(conn, candidates, now, **kwargs)

            with mock.patch.object(heartbeat.events, "create_event_if_new", spy_create),                     mock.patch.object(paper_game, "open_for_events", spy_open):
                self.run_full_cycle(kraken, clock)

        self.assertEqual(created_flags, [False, False])
        self.assertEqual(opened_for, [])
        expected = zero_counts(True)
        expected["open_now"] = 2
        self.assertEqual(self.record()["paper"], expected)
        self.assertEqual(len(self.plays()), 2)

    def test_an_event_without_a_valid_atr_is_counted_and_opens_nothing(self):
        kraken = wiring.FakeKraken(assets=("BTC",))
        real_classify = wiring.l2.classify_setup

        def classify_then_drop_the_atr(l1_features, l2_features):
            setup = real_classify(l1_features, l2_features)
            l2_features.atr_5m = None  # as when the closed 5-minute bars do not cover ATR14
            return setup

        with mock.patch.object(wiring.l2, "classify_setup", classify_then_drop_the_atr):
            output = self.run_full_cycle(kraken, wiring.StepClock(start=T0))
        self.assertEqual(output["funnel"]["events_created"], 1)
        expected = zero_counts(True)
        expected["skipped"]["no_valid_atr"] = 1
        self.assertEqual(self.record()["paper"], expected)
        self.assertEqual(self.plays(), {})

    def test_the_candidate_carries_the_l2_atr_and_its_pair(self):
        kraken = wiring.FakeKraken(assets=("BTC",))
        seen = []
        real_candidate = heartbeat._paper_candidate

        def spy(*args, **kwargs):
            candidate = real_candidate(*args, **kwargs)
            seen.append(candidate)
            return candidate

        with mock.patch.object(heartbeat, "_paper_candidate", spy):
            self.open_first_plays(kraken)
        (candidate,) = seen
        self.assertEqual((candidate.pair, candidate.atr_pair), (BTC_PAIR, BTC_PAIR))
        self.assertIsInstance(candidate.atr, float)
        self.assertEqual(D(self.plays()["BTC"]["atr"]), D(repr(candidate.atr)))

    def test_a_new_event_without_direction_is_counted_and_opens_nothing(self):
        kraken = wiring.FakeKraken(assets=("BTC",))
        with mock.patch.object(wiring.l2, "classify_setup", lambda _l1, _l2f: wiring.SetupResult("BREAKOUT", "NONE", [])):
            output = self.run_full_cycle(kraken, wiring.StepClock(start=T0))
        if output["funnel"]["events_created"] == 0:
            self.skipTest("the router creates no event for a NONE direction in this fixture")
        self.assertEqual(self.record()["paper"]["skipped"]["no_direction"], output["funnel"]["events_created"])
        self.assertEqual(self.record()["paper"]["opened"], 0)
        self.assertEqual(self.plays(), {})


class TestExitOnLevelsAndTime(PaperHeartbeatBase):
    def test_target_and_stop_close_at_the_touching_snapshot_not_at_the_level(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        self.open_first_plays(kraken)
        plays = self.plays()

        # Inside both levels nothing closes and nothing is pending (not due yet).
        self.run_synced_cycle(kraken, T0 + timedelta(minutes=30), full=False)
        expected = zero_counts(True)
        expected["open_now"] = 2
        self.assertEqual(self.record()["paper"], expected)
        self.assertEqual(self.closes(), {})

        # BTC bid above its target, ETH bid gapped below its stop.
        kraken.ticker["result"][BTC_PAIR] = ticker_row(60590.0, 60610.0)
        kraken.ticker["result"][ETH_PAIR] = ticker_row(2969.5, 2970.5)
        self.assertGreaterEqual(D("60590.0"), D(plays["BTC"]["target_price"]))
        self.assertLess(D("2969.5"), D(plays["ETH"]["stop_price"]))
        self.run_synced_cycle(kraken, T0 + timedelta(minutes=61), full=False)
        record = self.record()
        exit_ts = datetime.fromisoformat(record["ts"])
        expected = zero_counts(True)
        expected.update(closed=2)
        self.assertEqual(record["paper"], expected)

        closes = self.closes()
        for asset, pair, reason, outcome in (("BTC", BTC_PAIR, "target", "WIN"), ("ETH", ETH_PAIR, "stop", "LOSS")):
            with self.subTest(asset=asset):
                close, play = closes[asset], plays[asset]
                snap = self.snapshot(pair, record["ts"])
                # The observed executable price, never the level itself.
                self.assertEqual(D(close["exit_bid"]), D(repr(snap["bid"])))
                self.assertEqual(D(close["exit_ask"]), D(repr(snap["ask"])))
                level = play["target_price" if reason == "target" else "stop_price"]
                self.assertNotEqual(D(close["exit_bid"]), D(level))
                self.assertEqual(datetime.fromisoformat(close["exit_ts"]), exit_ts)
                self.assertEqual(close["exit_reason"], reason)
                self.assertEqual(close["exit_source"], "spot_snapshot")
                # A level exit is due when touched: no delay; the lag to the write is recorded.
                self.assertEqual(datetime.fromisoformat(close["due_at"]), exit_ts)
                self.assertEqual(close["delay_seconds"], 0.0)
                self.assertEqual(close["record_lag_seconds"],
                                 (datetime.fromisoformat(close["closed_at"]) - exit_ts).total_seconds())
                self.assertGreaterEqual(close["record_lag_seconds"], 0.0)
                self.assertEqual(close["outcome"], outcome)
                entry = paper.Quote(D(play["entry_bid"]), D(play["entry_ask"]))
                exit_ = paper.Quote(D(close["exit_bid"]), D(close["exit_ask"]))
                result = paper.settle(paper.Direction.LONG, D("100"), self.fee_bps, entry, exit_)
                self.assertEqual(close["gross_mid_cents"], int(result.gross_mid * 100))
                self.assertEqual(close["spread_cost_cents"], int(result.spread_cost * 100))
                self.assertEqual(close["fees_cents"], int(result.fees * 100))
                self.assertEqual(close["net_cents"], close["gross_mid_cents"] - close["spread_cost_cents"] - close["fees_cents"])
                # LONG: bought at the entry ask, sold at the exit bid, a fee on each leg.
                fraction = self.fee_bps / D(10000)
                fees = D(100) * fraction + D(100) * exit_.bid / entry.ask * fraction
                self.assertEqual(D(close["fees_cents"]) / 100, paper.cents(fees))
                self.assertGreater(close["fees_cents"], 0)
        btc = closes["BTC"]
        self.assertEqual(D(btc["exit_bid"]), D("60590.0"))
        self.assertEqual(btc["gross_mid_cents"], 100)  # 60000 -> 60600 mid on 100
        self.assertEqual(btc["spread_cost_cents"], 2)
        self.assertEqual(paper_store.current_balance(self.store._conn), D("1000") + sum(
            D(row["net_cents"]) / 100 for row in closes.values()
        ))

        # Closed once: a later cycle writes no second close.
        self.run_synced_cycle(kraken, T0 + timedelta(minutes=62), full=False)
        self.assertEqual(self.record()["paper"], zero_counts(True))
        self.assertEqual(len(self.closes()), 2)

    def test_no_quote_after_24_hours_pends_then_a_time_exit_closes_late_with_the_delay(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        self.open_first_plays(kraken)
        btc_due = datetime.fromisoformat(self.plays()["BTC"]["due_at"])
        self.assertEqual(btc_due, datetime.fromisoformat(self.plays()["BTC"]["entry_ts"]) + timedelta(hours=24))

        # Due cycle: no BTC quote at all this cycle -> BTC pending; ETH (inside its levels) times out.
        btc_row = kraken.ticker["result"].pop(BTC_PAIR)
        self.run_synced_cycle(kraken, T0 + timedelta(hours=24, minutes=1), full=False)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM spot_snapshots WHERE pair = ? AND ts = ?",
                                   BTC_PAIR, self.record()["ts"])[0]["n"], 0)
        expected = zero_counts(True)
        expected.update(closed=1, pending=1, open_now=1)
        self.assertEqual(self.record()["paper"], expected)
        self.assertEqual(sorted(self.closes()), ["ETH"])
        self.assertEqual(self.closes()["ETH"]["exit_reason"], "time")

        # An invalid BTC quote (market not online) after the due time is never an exit, even through the stop.
        self.plant_snapshot(BTC_PAIR, "BTC", btc_due + timedelta(minutes=5), 50000.0, 50001.0, "cancel_only")
        self.run_synced_cycle(kraken, T0 + timedelta(hours=24, minutes=10), full=False)
        expected = zero_counts(True)
        expected.update(pending=1, open_now=1)
        self.assertEqual(self.record()["paper"], expected)
        self.assertNotIn("BTC", self.closes())

        # The quote comes back: the play closes on it by time, with the delay past its due time.
        kraken.ticker["result"][BTC_PAIR] = btc_row
        self.run_synced_cycle(kraken, T0 + timedelta(hours=24, minutes=20), full=False)
        late_ts = datetime.fromisoformat(self.record()["ts"])
        expected = zero_counts(True)
        expected.update(closed=1)
        self.assertEqual(self.record()["paper"], expected)
        close = self.closes()["BTC"]
        self.assertEqual(close["exit_reason"], "time")
        self.assertEqual(datetime.fromisoformat(close["exit_ts"]), late_ts)
        self.assertEqual(datetime.fromisoformat(close["due_at"]), btc_due)
        self.assertEqual(close["delay_seconds"], (late_ts - btc_due).total_seconds())
        self.assertGreater(close["delay_seconds"], 18 * 60)
        self.assertEqual(D(close["exit_bid"]), D("59999.5"))
        self.assertEqual(close["gross_mid_cents"], 0)
        self.assertEqual(close["outcome"], "LOSS")  # flat price: the spread and fees are the loss

    def test_a_legacy_open_play_is_migrated_and_closes_by_the_old_rule(self):
        # The database as the pre-EX-1 code left it: an open BTC play with a 60-minute hold, due before T0.
        opened = T0 - timedelta(hours=2)
        conn = sqlite3.connect(self.db_path)
        try:
            legacy.create_legacy_schema(conn, at=opened)
            play_id = legacy.insert_legacy_play(conn, "old-btc", "BTC", "LONG", 59000.0, 59001.0, opened,
                                                pair=BTC_PAIR, quote="USD")
        finally:
            conn.close()
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        output = self.run_full_cycle(kraken, wiring.StepClock(start=T0))
        ts = datetime.fromisoformat(self.record()["ts"])
        # The legacy play closes on this cycle's BTC snapshot (after its due time), then the
        # BTC and ETH events open new plays with levels.
        self.assertEqual(output["funnel"]["events_created"], 2)
        expected = zero_counts(True)
        expected.update(closed=1, opened=2, open_now=2)
        self.assertEqual(self.record()["paper"], expected)
        self.assertTrue(paper_store.schema_current(self.store._conn))
        old = self.rows("SELECT * FROM paper_plays WHERE play_id = ?", play_id)[0]
        self.assertEqual((old["hold_minutes"], old["stop_price"], old["target_price"], old["exit_policy"]),
                         (60, None, None, None))
        close = self.rows("SELECT * FROM paper_closes WHERE play_id = ?", play_id)[0]
        self.assertEqual((close["exit_reason"], close["exit_source"], close["record_lag_seconds"]), (None, None, None))
        self.assertEqual(datetime.fromisoformat(close["exit_ts"]), ts)
        due = opened + timedelta(minutes=60)
        self.assertEqual(datetime.fromisoformat(close["due_at"]), due)
        self.assertEqual(close["delay_seconds"], (ts - due).total_seconds())
        new_plays = self.rows("SELECT * FROM paper_plays WHERE play_id <> ?", play_id)
        self.assertEqual([row["exit_policy"] for row in new_plays], ["ex1_initial_paper_v1"] * 2)


class TestSwitchAndSchemaOwnership(PaperHeartbeatBase):
    def test_switch_off_runs_nothing_writes_nothing_and_reports_zero(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        with mock.patch.object(config, "RADAR_PAPER_ENABLED", False), \
                mock.patch.object(paper_game, "settle_due", side_effect=AssertionError("ran")), \
                mock.patch.object(paper_game, "open_for_events", side_effect=AssertionError("ran")):
            self.open_first_plays(kraken)
            self.run_synced_cycle(kraken, T0 + timedelta(minutes=61), full=False)
        for record in self.run_records:
            self.assertEqual(record["paper"], zero_counts(False))
            self.assertNotIn("paper_ms", record["latency_ms"])
        self.assertEqual(self.paper_tables(), (False, []))

    def test_opening_the_store_never_creates_paper_tables(self):
        path = os.path.join(self.tmp, "fresh.sqlite")
        SnapshotStore(path).close()
        SnapshotStore(path).close()
        self.assertEqual(self.paper_tables(path), (False, []))

    def test_only_the_heartbeat_step_and_the_monitor_reach_the_paper_writers(self):
        writers = {
            "ensure_schema", "prepare", "settle_due", "open_for_events", "open_candidates", "ensure_wallet",
            "close_plays",
        }
        package = os.path.join(REPO_DIR, "radar_v08")
        callers = {}
        for folder, _dirs, files in os.walk(package):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(folder, name)
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute) and node.attr in writers and isinstance(node.value, ast.Name) \
                            and node.value.id in ("paper_game", "paper_store"):
                        callers.setdefault(os.path.relpath(path, package), set()).add(node.attr)
        self.assertIn("heartbeat.py", callers)
        self.assertNotIn("store.py", callers)
        self.assertTrue(set(callers) <= {"heartbeat.py", "paper_game.py", "paper_monitor.py"}, callers)
        # The monitor only closes plays: it never creates tables, a wallet or a play.
        self.assertEqual(callers["paper_monitor.py"], {"close_plays"})

    def test_the_first_enabled_cycle_creates_the_tables(self):
        self.assertEqual(self.paper_tables(), (False, []))
        self.open_first_plays(wiring.FakeKraken(assets=("BTC",)))
        present, names = self.paper_tables()
        self.assertTrue(present)
        self.assertIn("paper_plays", names)


class TestNothingElseInTheCycleChanges(PaperHeartbeatBase):
    """Off, on, and on with the paper step failing: same output and same run record
    except `paper` and `latency_ms`, cycle by cycle, each on its own fresh database."""

    def run_variant(self, name, patches=()):
        folder = os.path.join(self.tmp, name)
        os.mkdir(folder)
        db_path = os.path.join(folder, "state.sqlite")
        store = SnapshotStore(db_path)
        self.addCleanup(store.close)
        ids = (uuid.UUID(int=n) for n in itertools.count(1))
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        first = len(self.run_records)
        outputs = []
        with mock.patch.object(heartbeat, "get_asset_pairs",
                               functools.partial(real_get_asset_pairs, cache_path=os.path.join(folder, "pairs.json"))), \
                mock.patch.object(config, "EVENTS_LOG_PATH", os.path.join(folder, "events.jsonl")), \
                mock.patch.object(uuid, "uuid4", side_effect=lambda: next(ids)), \
                mock.patch.object(self, "store", store):
            for patcher in patches:
                patcher.start()
            try:
                outputs.append(self.run_full_cycle(kraken, wiring.StepClock(start=T0)))
                kraken.ticker["result"][BTC_PAIR] = ticker_row(60590.0, 60610.0)
                outputs.append(self.run_synced_cycle(kraken, T0 + timedelta(minutes=61), full=False))
                outputs.append(self.run_synced_cycle(kraken, T0 + timedelta(minutes=65)))
            finally:
                for patcher in patches:
                    patcher.stop()
        return outputs, self.run_records[first:], db_path

    def assert_same_cycle(self, baseline, other):
        base_outputs, base_records, _ = baseline
        outputs, records, _ = other
        self.assertEqual(len(outputs), len(base_outputs))
        for index, (base, out) in enumerate(zip(base_outputs, outputs)):
            with self.subTest(cycle=index):
                self.assertEqual(without_network_ms(out), without_network_ms(base))
                self.assertEqual(without_volatile(records[index]), without_volatile(base_records[index]))
                self.assertEqual(
                    set(records[index]["latency_ms"]) - {"paper_ms"},
                    set(base_records[index]["latency_ms"]) - {"paper_ms"},
                )

    def test_off_on_and_failing_leave_output_and_record_identical(self):
        off = self.run_variant("off", [mock.patch.object(config, "RADAR_PAPER_ENABLED", False)])
        on = self.run_variant("on")
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            failing = self.run_variant("failing", [
                mock.patch.object(paper_game, "settle_due", side_effect=RuntimeError("settle boom")),
                mock.patch.object(paper_game, "open_for_events", side_effect=RuntimeError("open boom")),
                mock.patch.object(paper_store, "read_open_plays", side_effect=RuntimeError("count boom")),
            ])
        whole = self.run_variant("whole", [
            mock.patch.object(heartbeat, "_run_paper_step", side_effect=RuntimeError("step boom")),
        ])

        # The "on" variant really played: two opens, then a close on the later cycle.
        self.assertEqual([r["paper"]["opened"] for r in on[1]], [2, 0, 0])
        # The BTC bid passes its EX-1 target on the second cycle; ETH stays inside its levels.
        self.assertEqual([r["paper"]["closed"] for r in on[1]], [0, 1, 0])
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS n FROM paper_closes", db_path=on[2])[0]["n"], 1
        )
        for record in off[1]:
            self.assertEqual(record["paper"], zero_counts(False))
        # Failing: settle (1) + one per new event + the open-play count (1), counted and logged.
        self.assertEqual([r["paper"]["failures"] for r in failing[1]], [4, 2, 2])
        self.assertEqual([r["paper"]["open_now"] for r in failing[1]], [None, None, None])
        self.assertTrue(any("settle boom" in line for line in logs.output))
        self.assertTrue(any("open boom" in line for line in logs.output))
        self.assertTrue(any("count boom" in line for line in logs.output))
        self.assertEqual([r["paper"]["failures"] for r in whole[1]], [1, 1, 1])
        for record in failing[1] + whole[1]:
            self.assertIn("paper_ms", record["latency_ms"])
            self.assertAlmostEqual(record["latency_ms"]["unaccounted_ms"], 0.0, delta=1e-6)

        for name, variant in (("on", on), ("failing", failing), ("whole", whole)):
            with self.subTest(variant=name):
                self.assert_same_cycle(off, variant)


class MonitorTickerServer:
    """The monitor's fake public Ticker (a duck-typed requests.Session under GuardedSession):
    ``rows(n, pairs)`` gives the answer rows of the n-th request. Records every request."""

    def __init__(self, rows):
        self._rows = rows
        self._lock = threading.Lock()
        self.calls = []

    def request(self, method, url, params=None, timeout=None, allow_redirects=True):
        with self._lock:
            index = len(self.calls)
            self.calls.append({"method": method, "url": url, "params": dict(params or {})})
        pairs = str((params or {}).get("pair", "")).split(",")
        return wiring.FakeResponse({"error": [], "result": self._rows(index, pairs)})

    def close(self):
        pass


class TestMonitorBesideTheHeartbeat(PaperHeartbeatBase):
    """The monitor and the heartbeat's paper step on one database file (temp dir)."""

    def test_heartbeat_cycles_and_the_monitor_thread_share_the_database_without_lock_failures(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        self.open_first_plays(kraken)
        plays = self.plays()
        self.assertGreaterEqual(D("60590.0"), D(plays["BTC"]["target_price"]))
        self.assertLess(D("2969.5"), D(plays["ETH"]["stop_price"]))

        def rows(index, pairs):
            answer = {}
            for pair in pairs:
                if pair == BTC_PAIR:  # inside the levels, then through the BTC target from the 30th request
                    answer[pair] = ticker_row(60590.0, 60610.0) if index >= 30 else ticker_row(59999.5, 60000.5)
                elif pair == ETH_PAIR:  # always inside the ETH levels
                    answer[pair] = ticker_row(2999.5, 3000.5)
            return answer

        server = MonitorTickerServer(rows)
        handle = paper_monitor.start_monitor(
            self.db_path,
            session=GuardedSession(1.0, 0, 0.0, http_session=server),
            clock=wiring.StepClock(start=T0 + timedelta(minutes=30)),
            interval_seconds=0.002,
        )
        self.assertIsNotNone(handle)
        records = []
        try:
            deadline = time.monotonic() + 60
            cycle = 0
            while cycle < 8 or handle.monitor.stats()["closed"] < 1:
                self.assertLess(time.monotonic(), deadline, "the monitor never closed the BTC play")
                if cycle == 3:  # the heartbeat's own quotes take ETH through its stop
                    kraken.ticker["result"][ETH_PAIR] = ticker_row(2969.5, 2970.5)
                self.run_synced_cycle(kraken, T0 + timedelta(minutes=31 + cycle), full=False)
                records.append(self.record())
                cycle += 1
        finally:
            self.assertTrue(handle.stop(10.0))
        self.assertFalse(handle.thread.is_alive())
        # One more cycle after the monitor stopped: it sees both plays closed.
        self.run_synced_cycle(kraken, T0 + timedelta(minutes=31 + cycle), full=False)
        records.append(self.record())

        # Every cycle completed with no paper failure (a locked database would be one).
        self.assertEqual([record["paper"]["failures"] for record in records], [0] * len(records))
        stats = handle.monitor.stats()
        self.assertEqual((stats["db_error"], stats["failed"], stats["malformed"]), (0, 0, 0))
        self.assertGreaterEqual(stats["requests"], 31)
        # One close per play: BTC by the monitor's Ticker read, ETH by the heartbeat's snapshot.
        counts = self.rows("SELECT play_id, COUNT(*) AS n FROM paper_closes GROUP BY play_id")
        self.assertEqual([row["n"] for row in counts], [1, 1])
        closes = self.closes()
        self.assertEqual((closes["BTC"]["exit_reason"], closes["BTC"]["exit_source"]), ("target", "ticker"))
        self.assertEqual(D(closes["BTC"]["exit_bid"]), D("60590.0"))
        self.assertEqual((closes["ETH"]["exit_reason"], closes["ETH"]["exit_source"]), ("stop", "spot_snapshot"))
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM paper_exit_quotes")[0]["n"], 1)
        # The counts agree with the rows.
        self.assertEqual(sum(record["paper"]["closed"] for record in records) + stats["closed"], 2)
        self.assertEqual(records[-1]["paper"]["open_now"], 0)
        self.assertEqual(paper_store.read_open_plays(self.store._conn), ())
        self.assertEqual(
            paper_store.current_balance(self.store._conn),
            D("1000") + sum(D(row["net_cents"]) / 100 for row in closes.values()),
        )
        # Every monitor request was one filtered Ticker call for the open plays' pairs.
        for call in server.calls:
            self.assertTrue(call["url"].endswith("/0/public/Ticker"))
            self.assertIn(call["params"]["pair"], (f"{BTC_PAIR},{ETH_PAIR}", BTC_PAIR, ETH_PAIR))

    def test_settle_and_monitor_closes_racing_on_two_connections_close_each_play_once(self):
        pairs = {"BTC": BTC_PAIR, "ETH": ETH_PAIR, "XRP": "XXRPZUSD"}
        heartbeat_conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        self.addCleanup(heartbeat_conn.close)
        paper_game.prepare(heartbeat_conn, T0)
        terms = paper_store.PlayTerms(stake=D("100"), fee_bps=self.fee_bps, max_open=3)
        # LONG at 99.5 / 100.5 with ATR 1: stop 98.5, target 104.5.
        ticker_target = {pair: ticker_row(105.0, 105.5) for pair in pairs.values()}
        errors = []
        outcomes = []
        monitor_won = 0
        rounds = 20
        for number in range(rounds):
            entry = T0 + timedelta(hours=number)
            opened = paper_store.open_candidates(
                heartbeat_conn,
                [
                    paper_store.PaperCandidate(
                        event_id=f"race-{number}-{asset}", run_id="race", asset=asset, pair=pair, quote="USD",
                        direction="LONG", bid=99.5, ask=100.5, snapshot_ts=entry.isoformat(), status="online",
                        why={}, atr=1.0, atr_pair=pair,
                    )
                    for asset, pair in pairs.items()
                ],
                terms=terms,
                now=entry,
            ).opened
            self.assertEqual(len(opened), 3)
            for asset, pair in pairs.items():  # the heartbeat's snapshot through the stop, 60 s in
                self.plant_snapshot(pair, asset, entry + timedelta(seconds=60), 98.0, 98.5, "online")
            # The monitor's Ticker read through the target, 30 s in: whichever writes first closes.
            # The barrier is inside the answer, so the monitor has already read the open plays
            # when the settle starts: both then evaluate and write at the same time.
            barrier = threading.Barrier(2)

            def answer(_index, wanted, barrier=barrier):
                barrier.wait(5)
                return {pair: ticker_target[pair] for pair in wanted}

            monitor = paper_monitor.PaperMonitor(
                self.db_path,
                session=GuardedSession(1.0, 0, 0.0, http_session=MonitorTickerServer(answer)),
                clock=wiring.StepClock(start=entry + timedelta(seconds=30)),
                interval_seconds=5,
            )
            settle_at = entry + timedelta(minutes=2)

            def settle(barrier=barrier, settle_at=settle_at):
                try:
                    barrier.wait(5)
                    paper_game.settle_due(heartbeat_conn, settle_at)
                except Exception as error:  # collected and asserted below
                    errors.append(error)

            def tick(monitor=monitor):
                try:
                    outcomes.append(monitor.tick())
                except Exception as error:  # collected and asserted below
                    errors.append(error)
                finally:
                    monitor.close()  # its connection was opened in this thread

            threads = [threading.Thread(target=settle), threading.Thread(target=tick)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [], f"round {number}")
            monitor_won += len(outcomes[-1].closed)
            ids = [play.play_id for play in opened]
            marks = ",".join("?" for _ in ids)
            counts = dict(heartbeat_conn.execute(
                f"SELECT play_id, COUNT(*) FROM paper_closes WHERE play_id IN ({marks}) GROUP BY play_id", ids
            ).fetchall())
            self.assertEqual(counts, {play_id: 1 for play_id in ids}, f"round {number}")

        self.assertEqual(len(outcomes), rounds)
        allowed = (paper_monitor.TickOutcome.WATCHED, paper_monitor.TickOutcome.DB_BUSY)
        self.assertTrue(all(result.outcome in allowed for result in outcomes), [r.outcome for r in outcomes])
        closes = paper_store.read_closes(heartbeat_conn)
        self.assertEqual(len(closes), 3 * rounds)
        self.assertEqual(len({close.play_id for close in closes}), 3 * rounds)
        from_ticker = sum(close.exit_source == "ticker" for close in closes)
        self.assertEqual(from_ticker, monitor_won)
        self.assertEqual(heartbeat_conn.execute("SELECT COUNT(*) FROM paper_exit_quotes").fetchone()[0], from_ticker)
        for close in closes:  # the monitor closes on the ticker target, the heartbeat on the snapshot stop
            if close.exit_source == "ticker":
                self.assertEqual(close.exit_reason, paper.ExitReason.TARGET)
            else:
                self.assertEqual((close.exit_source, close.exit_reason), ("spot_snapshot", paper.ExitReason.STOP))
        self.assertEqual(paper_store.read_open_plays(heartbeat_conn), ())
        balance = paper_store.current_balance(heartbeat_conn)
        self.assertEqual(balance, D("1000") + sum(close.net for close in closes))


if __name__ == "__main__":
    unittest.main()
