"""Pilot shadow wired into the heartbeat - pretend money only.

Each cycle, behind `config.RADAR_PILOT_ENABLED` and after the paper step, `run_heartbeat`
settles the pilot positions, evaluates the loss locks and offers the pilot the same new
events as the paper game (the primary spot touch written at the cycle's `ts`, the L2 ATR
and the pair's raw AssetPairs entry). It only adds the run record's `pilot` object and
its own `pilot_ms` stage: the paper step, its `paper` object and every other key are
unchanged, on or off.

The fake Kraken of `test_integrity_wiring` is used with its pairs renamed to EUR quotes
(`XXBTZEUR`) and AssetPairs entries carrying the pair rules; no socket is opened, the
clock is injected, SQLite lives in a temp dir and run records are captured in memory. The
kill switch and the lock review go through `scripts/pilot_control.py` on that temp
database. No real radar file is written and no running radar process is touched.

Numbers: BTC touch 59999.5 / 60000.5, L2 ATR 119.99999999999272 (every fake 5-minute bar
spans price x 0.999 .. x 1.001, in floats), so on the 0.1 tick the stop is
60000.5 - 239.99999999998544 -> 59760.5 and the target 60000.5 + 479.99999999997088 ->
60480.4 (both rounded down). With 240.00 EUR the notional room (10 %) binds:
24.00 / 60000.5 = 0.000399996... -> 0.00039999 at 8 lot decimals.

Later full cycles that must create a new event run inside the fixture's 5-minute bar, or
on a fake Kraken whose whole timeline is shifted (`kraken_for(..., shift=...)`).
"""

import importlib.util
import io
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_DIR)
sys.path.insert(0, TESTS_DIR)

import test_integrity_wiring as wiring  # noqa: E402  (fake Kraken heartbeat harness)
import test_paper_heartbeat as paper_wiring  # noqa: E402  (paper heartbeat helpers)

from radar_v08 import config, pilot_shadow  # noqa: E402
from radar_v08.adapters import pilot_store  # noqa: E402
from radar_v08.domain import risk  # noqa: E402
from radar_v08.domain.risk import Envelope, NoTradeReason  # noqa: E402
from radar_v08.store import SnapshotStore  # noqa: E402

T0 = wiring.T0
D = Decimal
BTC_EUR, ETH_EUR = "XXBTZEUR", "XETHZEUR"
EUR_ASSETS = {
    "BTC": (BTC_EUR, "XBT/EUR", "PF_XBTUSD", "XBT:USD", 60000.0),
    "ETH": (ETH_EUR, "ETH/EUR", "PF_ETHUSD", "ETH:USD", 3000.0),
}
PAIR_RULES = {"lot_decimals": 8, "ordermin": "0.00005", "costmin": "0.5", "tick_size": "0.1", "pair_decimals": 1}
BTC_QTY = D("0.00039999")


def _load_control():
    spec = importlib.util.spec_from_file_location("pilot_control", os.path.join(REPO_DIR, "scripts", "pilot_control.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pilot_control = _load_control()


def zero_pilot(enabled):
    no_trade = {reason.value: 0 for reason in NoTradeReason}
    no_trade["no_spot_market"] = 0
    return {
        "enabled": enabled, "evaluated": 0, "opened": 0, "closed": 0, "pending": 0, "no_trade": no_trade,
        "open_now": 0, "equity": None, "kill_switch": None, "locks_active": [], "failures": 0,
    }


def kraken_for(assets, *, eur=True, shift=timedelta(0)):
    """The fake Kraken with EUR-quoted pairs (or the harness's USD ones) and pair rules;
    ``shift`` moves its whole timeline (bars, book, trades, futures) by that much."""
    with mock.patch.dict(wiring.ASSETS, EUR_ASSETS if eur else {}),             mock.patch.multiple(wiring, T0=wiring.T0 + shift, FORMING_OPEN=wiring.FORMING_OPEN + shift):
        kraken = wiring.FakeKraken(assets=assets)
    for entry in kraken.asset_pairs["result"].values():
        entry.update(PAIR_RULES)
    return kraken


def setup(setup_type, direction):
    return mock.patch.object(wiring.l2, "classify_setup", lambda _l1, _l2f: wiring.SetupResult(setup_type, direction, []))


class PilotHeartbeatBase(paper_wiring.PaperHeartbeatBase):
    def setUp(self):
        super().setUp()
        for name, value in (
            ("RADAR_PILOT_ENABLED", True),
            ("PILOT_ENVELOPE", Envelope(D("240.00"), "EUR")),
        ):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def control(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = pilot_control.main(["--db", self.db_path, *argv], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def pilot(self, index=-1):
        return self.record(index)["pilot"]

    def decisions(self):
        return pilot_store.read_decisions(self.store._conn)

    def positions(self):
        return pilot_store.read_positions(self.store._conn)

    def pilot_tables(self, db_path=None):
        with sqlite3.connect(db_path or self.db_path) as conn:
            return sorted(name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'pilot%'"))

    def process_events(self):
        """Mark every event processed, so the next cycle may create an equivalent one."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE events SET status = 'PROCESSED'")

    def expected(self, **changes):
        expected = zero_pilot(True)
        no_trade = changes.pop("no_trade", {})
        expected.update(changes)
        expected["no_trade"] = {**expected["no_trade"], **no_trade}
        return expected


class TestEntries(PilotHeartbeatBase):
    def test_a_eur_long_event_opens_a_sized_pilot_position_next_to_the_game_play(self):
        output = self.run_full_cycle(kraken_for(("BTC",)), wiring.StepClock(start=T0))
        self.assertEqual(output["funnel"]["events_created"], 1)
        record = self.record()
        self.assertEqual(record["paper"]["opened"], 1)
        self.assertEqual(len(self.plays()), 1)
        self.assertIn("pilot_ms", record["latency_ms"])

        (decision,) = self.decisions()
        (position,) = self.positions()
        ts = record["ts"]
        self.assertEqual(decision.event_id, self.events()[0]["event_id"])
        self.assertEqual((decision.outcome, decision.reason), (pilot_store.OPENED, None))
        self.assertEqual((position.pair, position.quote, position.direction.value), (BTC_EUR, "EUR", "LONG"))
        self.assertEqual(position.entry_ts, datetime.fromisoformat(ts))
        self.assertEqual((position.entry_bid, position.entry_ask), (D("59999.5"), D("60000.5")))
        self.assertEqual(position.atr, D("119.99999999999272"))
        self.assertEqual((decision.value("tick"), decision.tick_source), (D("0.1"), "tick_size"))
        self.assertEqual((decision.value("equity"), decision.value("notional_remaining")), (D("240.00"), D("24.00")))
        self.assertEqual(decision.value("lot_quantity"), BTC_QTY)
        # Both EX-1 levels rounded DOWN to the 0.1 tick.
        self.assertEqual((position.stop, position.target), (D("59760.5"), D("60480.4")))
        self.assertEqual(decision.binding, risk.BindingConstraint.NOTIONAL)
        self.assertEqual(decision.lot_decimals, 8)
        self.assertEqual(position.quantity, BTC_QTY)
        self.assertEqual(position.notional, BTC_QTY * D("60000.5"))
        self.assertEqual(position.notional, D("23.999599995"))
        self.assertEqual(position.due_at, position.entry_ts + timedelta(hours=24))
        self.assertEqual(position.fee_bps, self.fee_bps)

        state = pilot_store.account_state(self.store._conn, now=datetime.fromisoformat(ts))
        self.assertEqual(
            self.pilot(),
            self.expected(
                evaluated=1, opened=1, open_now=1, equity=pilot_store.decimal_text(state.equity),
                kill_switch="released",
            ),
        )
        self.assertLess(state.equity, D("240"))  # the open position is marked at bid minus the exit fee

    def test_the_second_eur_event_of_a_cycle_is_no_trade_position_already_open(self):
        self.run_full_cycle(kraken_for(("BTC", "ETH")), wiring.StepClock(start=T0))
        self.assertEqual(self.record()["paper"]["opened"], 2)
        pilot = self.pilot()
        self.assertEqual((pilot["evaluated"], pilot["opened"], pilot["open_now"]), (2, 1, 1))
        self.assertEqual(pilot["no_trade"]["position_already_open"], 1)
        self.assertEqual(sum(pilot["no_trade"].values()), 1)

    def test_a_usd_quoted_event_is_no_trade_quote_currency_mismatch(self):
        self.run_full_cycle(kraken_for(("BTC",), eur=False), wiring.StepClock(start=T0))
        self.assertEqual(self.record()["paper"]["opened"], 1)
        (decision,) = self.decisions()
        self.assertEqual((decision.outcome, decision.reason), (pilot_store.NO_TRADE, NoTradeReason.QUOTE_CURRENCY_MISMATCH))
        self.assertEqual((decision.pair, decision.quote), (wiring.ASSETS["BTC"][0], "USD"))
        self.assertEqual(self.positions(), ())
        self.assertEqual(
            self.pilot(),
            self.expected(evaluated=1, equity="240.00", kill_switch="released",
                          no_trade={"quote_currency_mismatch": 1}),
        )

    def test_a_short_event_is_no_trade_unsupported_direction_while_the_game_plays_it(self):
        with setup("REVERSAL", "SHORT"):
            self.run_full_cycle(kraken_for(("BTC",)), wiring.StepClock(start=T0))
        self.assertEqual(self.plays()["BTC"]["direction"], "SHORT")
        (decision,) = self.decisions()
        self.assertEqual(decision.reason, NoTradeReason.UNSUPPORTED_DIRECTION)
        self.assertEqual(
            self.pilot(),
            self.expected(evaluated=1, equity="240.00", kill_switch="released",
                          no_trade={"unsupported_direction": 1}),
        )

    def test_missing_pair_rules_are_no_trade_never_a_default(self):
        kraken = kraken_for(("BTC",))
        for entry in kraken.asset_pairs["result"].values():
            del entry["ordermin"]
        self.run_full_cycle(kraken, wiring.StepClock(start=T0))
        (decision,) = self.decisions()
        self.assertEqual(decision.reason, NoTradeReason.MISSING_PAIR_RULES)
        self.assertEqual(self.positions(), ())

    def test_a_repeated_event_is_never_evaluated_twice(self):
        kraken = kraken_for(("BTC",))
        clock = wiring.StepClock()
        with mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", False):
            self.run_full_cycle(kraken, clock)
            self.run_full_cycle(kraken, clock)  # the dedup hit creates no new event
        self.assertEqual(len(self.decisions()), 1)
        self.assertEqual((self.pilot()["evaluated"], self.pilot()["open_now"]), (0, 1))


class TestPaperUnchanged(PilotHeartbeatBase):
    def cycle_on_fresh_db(self, name, pilot_on):
        cache = os.path.join(self.tmp, "pairs.json")
        if os.path.exists(cache):
            os.remove(cache)  # both runs fetch AssetPairs, so their HTTP counters compare
        path = os.path.join(self.tmp, f"{name}.sqlite")
        store = SnapshotStore(path)
        self.addCleanup(store.close)
        self.store, original = store, self.store
        try:
            with mock.patch.object(config, "RADAR_PILOT_ENABLED", pilot_on):
                self.run_full_cycle(kraken_for(("BTC", "ETH")), wiring.StepClock(start=T0))
        finally:
            self.store = original
        return self.record(), path

    def test_the_paper_step_its_rows_and_every_other_record_key_are_the_same_on_or_off(self):
        on, on_path = self.cycle_on_fresh_db("on", True)
        off, off_path = self.cycle_on_fresh_db("off", False)
        self.assertEqual(on["pilot"]["opened"], 1)
        self.assertEqual(off["pilot"], zero_pilot(False))
        self.assertEqual(on["paper"], off["paper"])

        def comparable(record):
            return paper_wiring.without_network_ms(
                {key: value for key, value in record.items() if key not in ("pilot", "latency_ms", "run_id")}
            )

        self.assertEqual(comparable(on), comparable(off))
        columns = "event_id, asset, pair, quote, direction, entry_bid, entry_ask, entry_ts, due_at, stake_cents, " \
                  "fee_bps, atr, stop_price, target_price, exit_policy"
        on_plays = self.rows(f"SELECT {columns} FROM paper_plays ORDER BY play_id", db_path=on_path)
        off_plays = self.rows(f"SELECT {columns} FROM paper_plays ORDER BY play_id", db_path=off_path)
        strip = [{key: value for key, value in row.items() if key != "event_id"} for row in on_plays]
        self.assertEqual(strip, [{key: value for key, value in row.items() if key != "event_id"} for row in off_plays])
        self.assertEqual(len(on_plays), 2)

    def test_the_flag_off_writes_no_pilot_table(self):
        with mock.patch.object(config, "RADAR_PILOT_ENABLED", False):
            self.run_full_cycle(kraken_for(("BTC",)), wiring.StepClock(start=T0))
            self.run_non_full_cycle(kraken_for(("BTC",)), wiring.StepClock(start=T0 + timedelta(minutes=5)))
        self.assertEqual(self.record(0)["paper"]["opened"], 1)
        for index in (0, 1):
            self.assertEqual(self.record(index)["pilot"], zero_pilot(False))
            self.assertNotIn("pilot_ms", self.record(index)["latency_ms"])
        self.assertEqual(self.pilot_tables(), [])

    def test_a_failing_pilot_step_is_logged_and_counted_never_raised(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        with mock.patch.object(pilot_shadow, "close_positions", boom), \
                mock.patch.object(pilot_shadow, "open_for_candidates", boom), \
                self.assertLogs(paper_wiring.LOGGER, level="WARNING") as logs:
            output = self.run_full_cycle(kraken_for(("BTC",)), wiring.StepClock(start=T0))
        self.assertEqual(output["funnel"]["events_created"], 1)
        self.assertEqual(self.record()["paper"]["opened"], 1)
        pilot = self.pilot()
        # The settle, the one event and the account read (no pilot table was ever created).
        self.assertEqual(pilot["failures"], 3)
        self.assertEqual((pilot["open_now"], pilot["equity"]), (None, None))
        self.assertEqual(pilot["opened"], 0)
        self.assertTrue(any("Pilot shadow" in line for line in logs.output))

    def test_a_non_full_cycle_runs_the_pilot_step_too(self):
        self.run_non_full_cycle(kraken_for(("BTC",)), wiring.StepClock(start=T0))
        self.assertIn("pilot_ms", self.record()["latency_ms"])
        self.assertEqual(self.pilot(), self.expected(equity="240.00", kill_switch="released"))
        self.assertLessEqual(set(pilot_store.TABLES), set(self.pilot_tables()))


class TestKillSwitchAndLocks(PilotHeartbeatBase):
    def test_an_engaged_kill_switch_blocks_the_next_entry_until_released(self):
        kraken = kraken_for(("BTC",))
        self.assertEqual(self.control("kill", "--reason", "x")[0], 2)  # no pilot tables yet: refused
        self.assertEqual(self.pilot_tables(), [])
        with mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", False):
            self.run_non_full_cycle(kraken, wiring.StepClock(start=T0 - timedelta(seconds=20)))
        code, out, err = self.control("kill", "--reason", "operator pause")
        self.assertEqual(code, 0, err)

        self.run_synced_cycle(kraken, T0)
        self.assertEqual(self.record()["paper"]["opened"], 1)  # the game is not affected
        self.assertEqual(
            self.pilot(),
            self.expected(evaluated=1, equity="240.00", kill_switch="engaged", no_trade={"kill_switch_engaged": 1}),
        )

        self.assertEqual(self.control("release", "--reason", "resume")[0], 0)
        self.process_events()
        later = timedelta(minutes=10)
        self.run_synced_cycle(kraken_for(("BTC",), shift=later), T0 + later)
        pilot = self.pilot()
        self.assertEqual((pilot["opened"], pilot["kill_switch"]), (1, "released"))

    def test_a_tripped_lock_survives_a_new_heartbeat_instance_and_clears_only_after_review(self):
        kraken = kraken_for(("BTC",))
        self.run_full_cycle(kraken, wiring.StepClock(start=T0))
        self.assertEqual(self.pilot()["opened"], 1)
        # A gap far below the stop: the first touching quote closes the position at its bid.
        self.plant_snapshot(BTC_EUR, "BTC", T0 + timedelta(minutes=1), 51000.0, 51001.0, "online")
        self.run_synced_cycle(kraken, T0 + timedelta(minutes=2), full=False)
        pilot = self.pilot()
        self.assertEqual((pilot["closed"], pilot["open_now"]), (1, 0))
        self.assertEqual(pilot["locks_active"], ["daily_loss"])
        (close,) = pilot_store.read_closes(self.store._conn)
        self.assertEqual(close.exit_bid, D("51000"))
        self.assertEqual(close.gross, D("-3.60"))  # 0.00039999 x (51000 - 60000.5) = -3.5999...
        self.assertLess(D(pilot["equity"]), D("237.60"))  # more than 1 % below the 240.00 day start

        # A new heartbeat instance (new store connection), the next UTC day: still locked.
        self.store.close()
        self.store = SnapshotStore(self.db_path)
        self.process_events()
        next_day = T0 + timedelta(days=1)
        kraken = kraken_for(("BTC",), shift=timedelta(days=1))
        self.run_synced_cycle(kraken, next_day)
        self.assertEqual(
            self.pilot(),
            self.expected(evaluated=1, equity=pilot["equity"], kill_switch="released",
                          locks_active=["daily_loss"], no_trade={"daily_loss_lock": 1}),
        )

        (lock,) = pilot_store.active_locks(self.store._conn)
        self.assertEqual(self.control("review-lock", "--lock-id", str(lock.lock_id + 99), "--cause", "c",
                                      "--reviewer", "r")[0], 2)
        code, out, err = self.control("review-lock", "--lock-id", str(lock.lock_id), "--cause", "gap below stop",
                                      "--reviewer", "operator")
        self.assertEqual(code, 0, err)
        self.assertEqual(pilot_store.active_locks(self.store._conn), ())

        self.process_events()
        later = timedelta(days=1, minutes=10)
        self.run_synced_cycle(kraken_for(("BTC",), shift=later), T0 + later)
        after = self.pilot()
        self.assertEqual((after["opened"], after["locks_active"]), (1, []))


if __name__ == "__main__":
    unittest.main()
