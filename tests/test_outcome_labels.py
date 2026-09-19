"""T041: prospective outcome labels at 15m/1h/4h/24h, their store/labeler and ledger version 5.

Every database is a fixture in a fresh temporary directory. Nothing opens, reads or copies
``radar_state.sqlite`` (or the Q2 copy), and ``config.EVENTS_LOG_PATH`` is patched to a
temporary file in every store case, so no root ``events.jsonl`` / ``runs.jsonl`` is written.
No network, model, UI, radar loop or notification. Golden values are computed by hand in
the comments, never by re-running the implementation.
"""

import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, TESTS_DIR)

import test_store_migration as smig  # noqa: E402  (sealed-evidence fixture builder)

from radar_v08 import config, store  # noqa: E402
from radar_v08.adapters import evidence_store as es  # noqa: E402
from radar_v08.adapters import invocation_store as ivs  # noqa: E402
from radar_v08.adapters import outcome_store as ost  # noqa: E402
from radar_v08.adapters.evidence_store import (  # noqa: E402
    OUTBOX_MIGRATION,
    OUTCOME_MIGRATION,
    SCHEMA_MIGRATIONS,
    MigrationFailure,
    SchemaMigrationError,
)
from radar_v08.adapters.kraken_timestamps import VENUE_SPOT  # noqa: E402
from radar_v08.adapters.outcome_store import (  # noqa: E402
    OutcomeStoreError,
    OutcomeStoreFailure,
)
from radar_v08.domain.costs import (  # noqa: E402
    CostInstrument,
    CostScenarioInput,
    CostStatus,
    DepthCoverage,
    FeeBasis,
    FeeInput,
    Missing,
    ScenarioSize,
    Side,
    SizeProvenance,
    SlippageBasis,
    SlippageInput,
    SpreadConvention,
    SpreadInput,
    price_round_trip,
)
from radar_v08.domain.costs import MissingReason as CostMissingReason  # noqa: E402
from radar_v08.domain.integrity import InstrumentId, InstrumentKind  # noqa: E402
from radar_v08.domain.invocation import (  # noqa: E402
    Direction,
    InvocationIdentity,
    InvocationRequest,
    ModelBudget,
)
from radar_v08.domain.outcomes import (  # noqa: E402
    HORIZONS,
    Availability,
    DecisionKind,
    DecisionRef,
    Horizon,
    HorizonCost,
    LinkedOutcome,
    LinkMissingReason,
    MissingReason,
    NotMature,
    OutcomeErrorCode,
    OutcomeInputError,
    OutcomeLabel,
    OutcomeSubject,
    QuoteObservation,
    RecordedCost,
    label_horizon,
    same_cost_for_every_horizon,
    visible_outcomes,
)
from radar_v08.store import SnapshotStore  # noqa: E402

UTC = timezone.utc
D = Decimal
T0 = datetime(2026, 9, 19, 10, 0, 0, tzinfo=UTC)
TOL = timedelta(seconds=120)
BTC = smig.BTC_USD  # InstrumentId("kraken", "XBT/USD", SPOT, "BTC", "USD", "BTC")
PAIR = "XXBTZUSD"
NEW_TABLES = {"outcome_subjects", "outcome_subject_costs", "outcome_labels"}
LEGACY_DEDUP = "BTC|BREAKOUT|LONG|qwen"
MODEL = "qwen3:14b"


def spot_cost(side=Side.LONG, **overrides):
    """The T040 hand-checked round trip on spot (no funding line).

    N = 1000 USD, spread 20 bps (h = 0.001), buy slippage 10 bps from the ask, sell slippage
    5 bps from the bid, entry fee 26 bps, exit fee 16 bps.
    LONG: entry BUY pays 1000*1.001*1.001 = 1002.001, fee 0.0026*1002.001 = 2.6052026;
          exit SELL gets 1000*0.999*0.9995 = 998.5005, fee 0.0016*998.5005 = 1.5976008;
          cost = 1002.001 + 2.6052026 - 998.5005 + 1.5976008 = 7.7033034 -> 0.0077033034.
    SHORT: entry SELL gets 998.5005, fee 0.0026*998.5005 = 2.5961013;
          exit BUY pays 1002.001, fee 0.0016*1002.001 = 1.6032016;
          cost = 1002.001 - 998.5005 + 2.5961013 + 1.6032016 = 7.6998029 -> 0.0076998029.
    """
    notional = D("1000")
    fields = dict(
        instrument=CostInstrument(kind=InstrumentKind.SPOT, symbol="XBT/USD", quote_currency="USD"),
        side=side,
        size=ScenarioSize(notional, SizeProvenance.HYPOTHETICAL_MANUAL),
        spread_convention=SpreadConvention.HALF_SPREAD_PLUS_TOUCH_SLIPPAGE,
        spread=SpreadInput(D("20"), "hand-built book"),
        buy_slippage=SlippageInput(D("10"), SlippageBasis.FROM_TOUCH, notional, DepthCoverage.FULL, "hand walk"),
        sell_slippage=SlippageInput(D("5"), SlippageBasis.FROM_TOUCH, notional, DepthCoverage.FULL, "hand walk"),
        entry_fee=FeeInput(D("26"), FeeBasis.UNCALIBRATED_ASSUMPTION, "assumed taker"),
        exit_fee=FeeInput(D("16"), FeeBasis.UNCALIBRATED_ASSUMPTION, "assumed maker"),
        funding_intervals=0,
        funding=None,
    )
    fields.update(overrides)
    return price_round_trip(CostScenarioInput(**fields))


def subject(**changes):
    fields = dict(
        instrument=BTC,
        pair=PAIR,
        direction=Direction.LONG,
        decision_as_of=T0,
        entry_mid=D("100"),
        entry_observed_at=T0 - timedelta(seconds=5),
        evidence=LinkMissingReason.NOT_RECORDED,
        decision=LinkMissingReason.NOT_RECORDED,
        arm=LinkMissingReason.NOT_RECORDED,
        costs=same_cost_for_every_horizon(spot_cost(Side.LONG)),
    )
    fields.update(changes)
    return OutcomeSubject(**fields)


def quote(at, bid, ask, source="fixture"):
    return QuoteObservation(at, None if bid is None else D(bid), None if ask is None else D(ask), source)


# Hand-made exit quotes, one per horizon, all inside [target, target + 120 s]:
#   15m at +30 s:  bid 101.4 / ask 101.6  -> mid 101.5  -> 101.5/100 - 1 = +0.015
#   1h  at +0 s:   bid 99.1  / ask 99.3   -> mid 99.2   -> 99.2/100 - 1  = -0.008
#   4h  at +90 s:  bid 102.2 / ask 102.3  -> mid 102.25 -> 102.25/100 - 1 = +0.0225
#   24h at +119 s: bid 96.95 / ask 97.05  -> mid 97     -> 97/100 - 1    = -0.03
GOLDEN_QUOTES = {
    Horizon.M15: (timedelta(minutes=15, seconds=30), "101.4", "101.6", D("101.5"), D("0.015")),
    Horizon.H1: (timedelta(hours=1), "99.1", "99.3", D("99.2"), D("-0.008")),
    Horizon.H4: (timedelta(hours=4, seconds=90), "102.2", "102.3", D("102.25"), D("0.0225")),
    Horizon.H24: (timedelta(hours=24, seconds=119), "96.95", "97.05", D("97"), D("-0.03")),
}
# LONG net = gross - 0.0077033034:
#   0.015 - 0.0077033034 = 0.0072966966;   -0.008 - 0.0077033034 = -0.0157033034
#   0.0225 - 0.0077033034 = 0.0147966966;  -0.03 - 0.0077033034 = -0.0377033034
LONG_NET = {
    Horizon.M15: D("0.0072966966"),
    Horizon.H1: D("-0.0157033034"),
    Horizon.H4: D("0.0147966966"),
    Horizon.H24: D("-0.0377033034"),
}
# SHORT gross = -market; net = gross - 0.0076998029:
#   -0.015 - 0.0076998029 = -0.0226998029;  0.008 - 0.0076998029 = 0.0003001971
#   -0.0225 - 0.0076998029 = -0.0301998029; 0.03 - 0.0076998029 = 0.0223001971
SHORT_NET = {
    Horizon.M15: D("-0.0226998029"),
    Horizon.H1: D("0.0003001971"),
    Horizon.H4: D("-0.0301998029"),
    Horizon.H24: D("0.0223001971"),
}


def golden_quotes():
    return [quote(T0 + offset, bid, ask, f"q-{h.value}") for h, (offset, bid, ask, _, _) in GOLDEN_QUOTES.items()]


# --- pure domain --------------------------------------------------------------------------------


class TestCostFixture(unittest.TestCase):
    def test_the_t040_scenarios_used_below_have_the_hand_totals(self):
        self.assertEqual(spot_cost(Side.LONG).total_fraction, D("0.0077033034"))
        self.assertEqual(spot_cost(Side.SHORT).total_fraction, D("0.0076998029"))


class TestGoldenPerHorizon(unittest.TestCase):
    def test_every_horizon_long_by_hand(self):
        s = subject()
        now = T0 + timedelta(days=2)
        for horizon, (offset, _, _, mid, market) in GOLDEN_QUOTES.items():
            with self.subTest(horizon=horizon.value):
                label = label_horizon(s, horizon, golden_quotes(), now=now, tolerance=TOL)
                self.assertIsInstance(label, OutcomeLabel)
                self.assertIs(label.status, Availability.AVAILABLE)
                self.assertEqual(label.target_at, T0 + horizon.duration)
                self.assertEqual(label.label_available_at, T0 + offset)
                self.assertEqual(label.exit_mid, mid)
                self.assertEqual(label.exit_source, f"q-{horizon.value}")
                self.assertEqual(label.market_return, market)
                self.assertEqual(label.gross_markout, market)
                self.assertEqual(label.net_markout, LONG_NET[horizon])
                self.assertEqual(label.cost_policy_version, "COST-1")
                self.assertIsNone(label.market_missing)
                self.assertIsNone(label.net_missing)

    def test_every_horizon_short_by_hand(self):
        s = subject(direction=Direction.SHORT, costs=same_cost_for_every_horizon(spot_cost(Side.SHORT)))
        for horizon, (_, _, _, _, market) in GOLDEN_QUOTES.items():
            with self.subTest(horizon=horizon.value):
                label = label_horizon(s, horizon, golden_quotes(), now=T0 + timedelta(days=2), tolerance=TOL)
                self.assertEqual(label.market_return, market)  # unsigned market move
                self.assertEqual(label.gross_markout, -market)  # a SHORT gains when the mid falls
                self.assertEqual(label.net_markout, SHORT_NET[horizon])

    def test_horizon_minutes_are_the_four_contract_horizons(self):
        self.assertEqual([h.minutes for h in HORIZONS], [15, 60, 240, 1440])
        self.assertEqual([h.value for h in HORIZONS], ["15m", "1h", "4h", "24h"])

    def test_the_first_valid_quote_in_the_window_is_the_exit(self):
        # 15m target 10:15:00. 10:14:59 is before the horizon; 10:15:10 is crossed (ask < bid);
        # 10:15:20 is the first valid one -> mid (100.9 + 101.1) / 2 = 101 -> +0.01.
        quotes = [
            quote(T0 + timedelta(minutes=14, seconds=59), "150", "150.2"),
            quote(T0 + timedelta(minutes=15, seconds=10), "101.2", "101.0"),
            quote(T0 + timedelta(minutes=15, seconds=20), "100.9", "101.1", "first-valid"),
            quote(T0 + timedelta(minutes=15, seconds=40), "80", "80.2"),
        ]
        label = label_horizon(subject(), Horizon.M15, quotes, now=T0 + timedelta(hours=1), tolerance=TOL)
        self.assertEqual((label.exit_source, label.exit_mid, label.market_return), ("first-valid", D("101"), D("0.01")))
        self.assertEqual(label.label_available_at, T0 + timedelta(minutes=15, seconds=20))

    def test_division_rounds_half_even_at_fifty_digits(self):
        # 4/3 - 1 = 0.333... : 1.3333... to 50 significant digits, minus 1.
        label = label_horizon(
            subject(entry_mid=D("3"), costs=()),
            Horizon.M15,
            [quote(T0 + timedelta(minutes=15), "3.9", "4.1")],
            now=T0 + timedelta(hours=1),
            tolerance=TOL,
        )
        self.assertEqual(label.market_return, D("0." + "3" * 49))


class TestMissingIsTyped(unittest.TestCase):
    NOW = T0 + timedelta(days=2)

    def test_no_quote_in_the_window_is_quote_missing_never_zero(self):
        quotes = [
            quote(T0 + timedelta(minutes=14, seconds=59), "100", "100.2"),  # before the horizon
            quote(T0 + timedelta(minutes=17, seconds=1), "100", "100.2"),  # after target + 120 s
        ]
        label = label_horizon(subject(), Horizon.M15, quotes, now=self.NOW, tolerance=TOL)
        self.assertIs(label.status, Availability.UNAVAILABLE)
        self.assertEqual(label.label_available_at, T0 + timedelta(minutes=17))  # target + tolerance
        self.assertIsNone(label.market_return)
        self.assertIsNone(label.gross_markout)
        self.assertIsNone(label.net_markout)
        self.assertIs(label.market_missing, MissingReason.QUOTE_MISSING_AT_HORIZON)
        self.assertIs(label.gross_missing, MissingReason.QUOTE_MISSING_AT_HORIZON)
        self.assertIs(label.net_missing, MissingReason.GROSS_UNAVAILABLE)

    def test_only_invalid_quotes_in_the_window_is_quote_invalid(self):
        quotes = [
            quote(T0 + timedelta(minutes=15, seconds=5), "0", "100.2"),
            quote(T0 + timedelta(minutes=15, seconds=6), None, "100.2"),
            quote(T0 + timedelta(minutes=15, seconds=7), "100.3", "100.2"),
        ]
        label = label_horizon(subject(), Horizon.M15, quotes, now=self.NOW, tolerance=TOL)
        self.assertIs(label.market_missing, MissingReason.QUOTE_INVALID_AT_HORIZON)
        self.assertIs(label.net_missing, MissingReason.GROSS_UNAVAILABLE)

    def test_no_price_source_is_price_source_unsupported(self):
        label = label_horizon(subject(), Horizon.H1, None, now=self.NOW, tolerance=TOL)
        self.assertIs(label.market_missing, MissingReason.PRICE_SOURCE_UNSUPPORTED)
        self.assertEqual(label.label_available_at, T0 + timedelta(hours=1))

    def test_direction_none_keeps_the_market_move_but_no_markout(self):
        s = subject(direction=Direction.NONE, costs=())
        label = label_horizon(s, Horizon.M15, golden_quotes(), now=self.NOW, tolerance=TOL)
        self.assertEqual(label.market_return, D("0.015"))
        self.assertIsNone(label.gross_markout)
        self.assertIs(label.gross_missing, MissingReason.NO_DIRECTION)
        self.assertIs(label.net_missing, MissingReason.GROSS_UNAVAILABLE)

    def test_no_recorded_cost_means_net_unavailable_not_gross(self):
        label = label_horizon(subject(costs=()), Horizon.M15, golden_quotes(), now=self.NOW, tolerance=TOL)
        self.assertEqual(label.gross_markout, D("0.015"))
        self.assertIsNone(label.net_markout)
        self.assertIs(label.net_missing, MissingReason.COST_NOT_RECORDED)
        self.assertIsNone(label.cost_policy_version)

    def test_cost_recorded_for_other_horizons_only_is_not_borrowed(self):
        only_1h = (HorizonCost(Horizon.H1, RecordedCost.from_scenario(spot_cost())),)
        s = subject(costs=only_1h)
        self.assertIs(
            label_horizon(s, Horizon.M15, golden_quotes(), now=self.NOW, tolerance=TOL).net_missing,
            MissingReason.COST_NOT_RECORDED,
        )
        self.assertEqual(
            label_horizon(s, Horizon.H1, golden_quotes(), now=self.NOW, tolerance=TOL).net_markout, LONG_NET[Horizon.H1]
        )

    def test_incomplete_cost_means_net_unavailable_never_a_partial_sum(self):
        incomplete = spot_cost(spread=Missing(CostMissingReason.NOT_OBSERVED, "no book"))
        self.assertIs(incomplete.status, CostStatus.INCOMPLETE)
        s = subject(costs=same_cost_for_every_horizon(incomplete))
        label = label_horizon(s, Horizon.M15, golden_quotes(), now=self.NOW, tolerance=TOL)
        self.assertEqual(label.gross_markout, D("0.015"))
        self.assertIsNone(label.net_markout)
        self.assertIs(label.net_missing, MissingReason.COST_INCOMPLETE)
        self.assertEqual(label.cost_policy_version, "COST-1")
        self.assertIsNone(RecordedCost.from_scenario(incomplete).total_fraction)

    def test_a_cost_for_the_other_side_or_kind_is_refused(self):
        with self.assertRaises(OutcomeInputError) as caught:
            subject(costs=same_cost_for_every_horizon(spot_cost(Side.SHORT)))
        self.assertIs(caught.exception.code, OutcomeErrorCode.INCONSISTENT)
        with self.assertRaises(OutcomeInputError):
            subject(direction=Direction.NONE)  # NONE has no acted-on round trip
        futures = InstrumentId("kraken-futures", "PF_XBTUSD", InstrumentKind.FUTURES, "BTC", "USD", "contracts")
        with self.assertRaises(OutcomeInputError):
            subject(instrument=futures, pair="PF_XBTUSD")  # spot cost on a futures subject

    def test_missing_links_stay_typed_reasons(self):
        s = subject()
        self.assertIs(s.evidence, LinkMissingReason.NOT_RECORDED)
        self.assertIs(s.decision, LinkMissingReason.NOT_RECORDED)
        self.assertIs(s.arm, LinkMissingReason.NOT_RECORDED)
        with self.assertRaises(OutcomeInputError):
            subject(arm=LinkMissingReason.LEGACY_UNVERSIONED)  # only evidence can be legacy-unversioned
        with self.assertRaises(OutcomeInputError):
            subject(arm="")

    def test_bad_inputs_are_refused(self):
        with self.assertRaises(OutcomeInputError) as caught:
            subject(entry_mid=100.0)
        self.assertIs(caught.exception.code, OutcomeErrorCode.NOT_DECIMAL)
        with self.assertRaises(OutcomeInputError):
            subject(entry_mid=D("0"))
        with self.assertRaises(OutcomeInputError) as caught:
            subject(decision_as_of=datetime(2026, 9, 19, 10, 0))
        self.assertIs(caught.exception.code, OutcomeErrorCode.NAIVE_TIMESTAMP)
        with self.assertRaises(OutcomeInputError):
            subject(entry_observed_at=T0 + timedelta(seconds=1))  # entry after the decision
        with self.assertRaises(OutcomeInputError):
            QuoteObservation(T0, D("NaN"), D("1"), "x")
        with self.assertRaises(OutcomeInputError):
            label_horizon(subject(), Horizon.M15, [], now=T0, tolerance=timedelta(seconds=-1))


class TestMaturity(unittest.TestCase):
    def test_before_the_horizon_nothing_is_labelled(self):
        result = label_horizon(subject(), Horizon.H1, golden_quotes(), now=T0 + timedelta(minutes=59), tolerance=TOL)
        self.assertEqual(result, NotMature(subject().subject_id, Horizon.H1, T0 + timedelta(hours=1)))

    def test_inside_the_window_without_a_quote_it_waits_until_the_window_closes(self):
        now = T0 + timedelta(minutes=16)
        result = label_horizon(subject(), Horizon.M15, [], now=now, tolerance=TOL)
        self.assertEqual(result, NotMature(subject().subject_id, Horizon.M15, T0 + timedelta(minutes=17)))
        closed = label_horizon(subject(), Horizon.M15, [], now=T0 + timedelta(minutes=17), tolerance=TOL)
        self.assertIs(closed.market_missing, MissingReason.QUOTE_MISSING_AT_HORIZON)

    def test_a_quote_from_after_now_is_never_used(self):
        # A poisoned future observation (10:15:50) already sits in the input at 10:15:10.
        poisoned = [quote(T0 + timedelta(minutes=15, seconds=50), "120", "120.2", "future")]
        result = label_horizon(subject(), Horizon.M15, poisoned, now=T0 + timedelta(minutes=15, seconds=10), tolerance=TOL)
        self.assertIsInstance(result, NotMature)

    def test_an_available_label_is_known_at_its_exit_quote_never_earlier(self):
        label = label_horizon(subject(), Horizon.M15, golden_quotes(), now=T0 + timedelta(hours=1), tolerance=TOL)
        with self.assertRaises(OutcomeInputError):
            replace(label, label_available_at=label.target_at - timedelta(seconds=1))
        with self.assertRaises(OutcomeInputError):
            replace(label, label_available_at=label.label_available_at + timedelta(seconds=1))


class TestVisibility(unittest.TestCase):
    def test_future_label_is_invisible_to_an_earlier_decision_even_about_a_past_event(self):
        s = subject()
        label = label_horizon(s, Horizon.H1, golden_quotes(), now=T0 + timedelta(days=2), tolerance=TOL)
        outcome = LinkedOutcome(s, label)
        # The subject's own decision (10:00) is long past at 10:30, but its 1h label is known at 11:00.
        self.assertEqual(visible_outcomes([outcome], T0 + timedelta(minutes=30)), ())
        self.assertEqual(visible_outcomes([outcome], T0 + timedelta(minutes=59, seconds=59)), ())
        self.assertEqual(visible_outcomes([outcome], T0 + timedelta(hours=1)), (outcome,))


class TestSubjectIdentity(unittest.TestCase):
    def test_identity_is_stable_and_covers_every_link(self):
        base = subject()
        self.assertEqual(base.subject_id, subject().subject_id)
        self.assertTrue(base.subject_id.startswith("outcome:sha256:"))
        for change in (
            dict(arm="A1"),
            dict(evidence="evidence:sha256:" + "a" * 64),
            dict(decision=DecisionRef(DecisionKind.DECISION, "dec-1")),
            dict(direction=Direction.SHORT, costs=()),
            dict(pair="XBTUSDT"),
            dict(decision_as_of=T0 + timedelta(seconds=1)),
        ):
            with self.subTest(change=sorted(change)):
                self.assertNotEqual(subject(**change).subject_id, base.subject_id)
        # Entry and costs are content: same identity, so a changed one is a conflict, not a new subject.
        self.assertEqual(subject(entry_mid=D("101"), costs=()).subject_id, base.subject_id)


# --- store, labeler and ledger version 5 ----------------------------------------------------------


def build_legacy_db(path):
    """A pre-ledger database: old schema, legacy duplicates in ``events`` (one dedup_key PENDING
    twice), and raw legacy ``forward_returns`` rows (one labelled, one still pending)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(store.SCHEMA)
        for column, sql_type in store._FORWARD_RETURNS_MIGRATION_COLUMNS.items():
            conn.execute(f"ALTER TABLE forward_returns ADD COLUMN {column} {sql_type}")
        for column, sql_type in store._EVENTS_MIGRATION_COLUMNS.items():
            conn.execute(f"ALTER TABLE events ADD COLUMN {column} {sql_type}")
        for event_id in ("evt-dup-1", "evt-dup-2"):
            conn.execute(
                "INSERT INTO events (event_id, dedup_key, ts, type, asset, setup_type, direction, status) "
                "VALUES (?, ?, '2026-09-01T00:05:00+00:00', 'RADAR_ALERT', 'BTC', 'BREAKOUT', 'LONG', 'PENDING')",
                (event_id, LEGACY_DEDUP),
            )
        conn.execute(
            "INSERT INTO forward_returns (asset, ts, horizon_minutes, return_pct, entry_price, mfe_pct, mae_pct, "
            "labeled_at, pair) VALUES ('BTC', '2026-09-01T00:05:00+00:00', 15, 0.4, 50000.0, 0.6, -0.1, "
            "'2026-09-01T00:21:00+00:00', 'XXBTZUSD')"
        )
        conn.execute(
            "INSERT INTO forward_returns (asset, ts, horizon_minutes, return_pct, entry_price, pair) "
            "VALUES ('BTC', '2026-09-01T00:05:00+00:00', 60, NULL, 50000.0, 'XXBTZUSD')"
        )
        conn.commit()
    finally:
        conn.close()


def snapshot(path):
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump()), conn.execute("PRAGMA schema_version").fetchone()[0]
    finally:
        conn.close()


def legacy_index_lists(path):
    conn = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        ]
        return {table: sorted(tuple(row) for row in conn.execute(f"PRAGMA index_list({table})")) for table in tables}
    finally:
        conn.close()


class TempCase(unittest.TestCase):
    """A disposable folder, a patched event-log path, and helpers to open fixtures."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crypto-radar-t041-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "state.sqlite")
        for name in ("EVENTS_LOG_PATH", "OUTPUT_V08_PATH"):
            if hasattr(config, name):
                patcher = mock.patch.object(config, name, os.path.join(self.tmp, name.lower()))
                patcher.start()
                self.addCleanup(patcher.stop)
        self._conns = []

    def tearDown(self):
        for conn in self._conns:
            conn.close()

    def connect(self):
        conn = sqlite3.connect(self.path)
        self._conns.append(conn)
        return conn

    def forget(self, conn):
        conn.close()
        self._conns.remove(conn)

    def migrated(self):
        build_legacy_db(self.path)
        conn = self.connect()
        es.apply_schema_migrations(conn, now=T0)
        return conn

    def query(self, sql, *params):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def add_quote(self, conn, at, bid, ask, pair=PAIR, ts=None):
        conn.execute(
            "INSERT INTO spot_snapshots (asset, pair, quote, ts, last, bid, ask) VALUES ('BTC', ?, 'USD', ?, ?, ?, ?)",
            (pair, ts if ts is not None else at.isoformat(), ask, bid, ask),
        )
        conn.commit()

    def add_golden_quotes(self, conn):
        for offset, bid, ask, _, _ in GOLDEN_QUOTES.values():
            self.add_quote(conn, T0 + offset, float(bid), float(ask))


class TestMigrationV5(TempCase):
    def test_v5_is_the_last_version_of_the_ledger_plan(self):
        self.assertIs(SCHEMA_MIGRATIONS[-1], OUTCOME_MIGRATION)
        self.assertEqual((OUTCOME_MIGRATION.version, len(SCHEMA_MIGRATIONS)), (5, 5))
        self.assertEqual(OUTCOME_MIGRATION.name, "outcome_subjects_costs_and_labels")
        self.assertIs(SCHEMA_MIGRATIONS[3], OUTBOX_MIGRATION)

    def test_statements_are_create_only_on_new_tables(self):
        for statement in OUTCOME_MIGRATION.statements:
            upper = statement.upper()
            self.assertTrue(statement.startswith("CREATE "), statement[:40])
            self.assertNotIn("IF NOT EXISTS", upper)
            self.assertNotIn("ALTER", upper)
            self.assertNotIn("DROP", upper)
            self.assertNotIn("INSERT", upper)
            self.assertNotIn("CREATE UNIQUE INDEX", upper)
            self.assertNotIn("FORWARD_RETURNS", upper)
            self.assertNotIn("EVENTS", upper)
            self.assertEqual(len(re.findall(r"\bUPDATE\b", upper)), len(re.findall(r"\bBEFORE UPDATE\b", upper)))
            self.assertEqual(len(re.findall(r"\bDELETE\b", upper)), len(re.findall(r"\bBEFORE DELETE\b", upper)))
            for target in re.findall(r"\b(?:ON|REFERENCES)\s+(\w+)", statement):
                self.assertIn(target, NEW_TABLES, statement[:60])

    def test_legacy_duplicates_in_events_open_without_error(self):
        build_legacy_db(self.path)
        before_indexes = legacy_index_lists(self.path)
        legacy_dump, _ = snapshot(self.path)
        opened = SnapshotStore(self.path)
        try:
            self.assertEqual([entry.version for entry in opened.schema_ledger()], [1, 2, 3, 4, 5])
        finally:
            opened.close()
        dupes = self.query("SELECT event_id FROM events WHERE dedup_key = ? AND status = 'PENDING' ORDER BY 1", LEGACY_DEDUP)
        self.assertEqual(dupes, [("evt-dup-1",), ("evt-dup-2",)])
        after_indexes = legacy_index_lists(self.path)
        # No index, unique or not, was added to or removed from any pre-existing table (events included).
        self.assertEqual({table: after_indexes[table] for table in before_indexes}, before_indexes)
        self.assertIn("events", before_indexes)
        self.assertIn("forward_returns", before_indexes)
        # The only unique indexes of version 5 are the primary keys of its new tables.
        unique_v5 = sorted(table for table in NEW_TABLES for row in after_indexes[table] if row[2] == 1)
        self.assertEqual(unique_v5, sorted(NEW_TABLES))
        new_dump, _ = snapshot(self.path)
        legacy_rows = [line for line in legacy_dump if line.startswith("INSERT INTO")]
        self.assertEqual(len(legacy_rows), 5)  # 2 events, 2 forward_returns, 1 sqlite_sequence
        for line in legacy_rows:
            self.assertIn(line, new_dump)

    def test_second_open_writes_nothing(self):
        build_legacy_db(self.path)
        SnapshotStore(self.path).close()
        first = snapshot(self.path)
        conn = self.connect()
        changes = conn.total_changes
        self.assertEqual(es.apply_schema_migrations(conn, now=T0 + timedelta(days=1)), ())
        self.assertEqual(conn.total_changes, changes)
        self.forget(conn)
        again = SnapshotStore(self.path)
        try:
            self.assertEqual(again.migrate_schema(), ())
        finally:
            again.close()
        self.assertEqual(snapshot(self.path), first)

    def test_version_5_is_recorded_with_its_checksum(self):
        conn = self.migrated()
        entry = es.read_ledger(conn)[-1]
        self.assertEqual((entry.version, entry.name), (5, "outcome_subjects_costs_and_labels"))
        self.assertEqual(entry.checksum, OUTCOME_MIGRATION.checksum)
        self.assertEqual(entry.applied_at, "2026-09-19T10:00:00+00:00")

    def test_v4_database_upgrades_to_v5_only(self):
        build_legacy_db(self.path)
        conn = self.connect()
        self.assertEqual(es.apply_schema_migrations(conn, now=T0, migrations=SCHEMA_MIGRATIONS[:4]), (1, 2, 3, 4))
        self.assertEqual(es.apply_schema_migrations(conn, now=T0 + timedelta(days=1)), (5,))
        self.assertEqual([entry.version for entry in es.read_ledger(conn)], [1, 2, 3, 4, 5])

    def test_injected_failure_in_v5_rolls_back_everything(self):
        # outcome_labels is statement 8 of 11: outcome_subjects, outcome_subject_costs, the first
        # index and four triggers are created inside the transaction first, then the clash aborts.
        build_legacy_db(self.path)
        conn = self.connect()
        es.apply_schema_migrations(conn, now=T0, migrations=SCHEMA_MIGRATIONS[:4])
        conn.execute("CREATE TABLE outcome_labels (unrelated TEXT)")
        conn.commit()
        self.forget(conn)
        before = snapshot(self.path)
        with self.assertRaises(SchemaMigrationError) as caught:
            SnapshotStore(self.path)
        self.assertEqual(caught.exception.code, MigrationFailure.STATEMENT_FAILED)
        self.assertEqual(caught.exception.version, 5)
        self.assertIn("statement 8/11", caught.exception.detail)
        self.assertEqual(snapshot(self.path), before)
        self.assertEqual([row[0] for row in self.query("SELECT version FROM schema_version_ledger ORDER BY 1")], [1, 2, 3, 4])
        names = {row[0] for row in self.query("SELECT name FROM sqlite_master")}
        self.assertNotIn("outcome_subjects", names)
        self.assertNotIn("outcome_subject_costs", names)

    def test_outcome_calls_refuse_an_unmigrated_database(self):
        build_legacy_db(self.path)
        conn = self.connect()
        es.apply_schema_migrations(conn, now=T0, migrations=SCHEMA_MIGRATIONS[:4])
        for call in (
            lambda: ost.register_subject(conn, subject(), now=T0),
            lambda: ost.label_due_outcomes(conn, now=T0, tolerance=TOL),
            lambda: ost.outcomes_known_as_of(conn, T0),
        ):
            with self.assertRaises(OutcomeStoreError) as caught:
                call()
            self.assertIs(caught.exception.code, OutcomeStoreFailure.SCHEMA_NOT_MIGRATED)
            self.assertFalse(conn.in_transaction)


class TestLabelerGoldens(TempCase):
    def test_labeler_writes_every_horizon_with_the_hand_values_and_all_links(self):
        conn = self.migrated()
        s = subject(arm="baseline-L3")
        self.assertTrue(ost.register_subject(conn, s, now=T0))
        self.add_golden_quotes(conn)
        run = ost.label_due_outcomes(conn, now=T0 + timedelta(hours=25), tolerance=TOL)
        self.assertEqual((run.labeled_available, run.labeled_unavailable, run.not_mature), (4, 0, 0))
        outcomes = ost.load_outcomes(conn, s.subject_id)
        self.assertEqual([o.label.horizon for o in outcomes], list(HORIZONS))
        for outcome in outcomes:
            horizon = outcome.label.horizon
            offset, _, _, mid, market = GOLDEN_QUOTES[horizon]
            with self.subTest(horizon=horizon.value):
                self.assertEqual(outcome.label.exit_mid, mid)
                self.assertEqual(outcome.label.market_return, market)
                self.assertEqual(outcome.label.gross_markout, market)
                self.assertEqual(outcome.label.net_markout, LONG_NET[horizon])
                self.assertEqual(outcome.label.label_available_at, T0 + offset)
                self.assertTrue(outcome.label.exit_source.startswith("spot_snapshots:"))
                # Every label carries its links: pair, venue, direction, evidence, decision, arm.
                self.assertEqual((outcome.subject.pair, outcome.subject.instrument.venue), (PAIR, "kraken"))
                self.assertIs(outcome.subject.direction, Direction.LONG)
                self.assertIs(outcome.subject.evidence, LinkMissingReason.NOT_RECORDED)
                self.assertIs(outcome.subject.decision, LinkMissingReason.NOT_RECORDED)
                self.assertEqual(outcome.subject.arm, "baseline-L3")
        stored = [
            (horizon, D(market), D(net), available)
            for horizon, market, net, available in self.query(
                "SELECT horizon, market_return, net_markout, label_available_at FROM outcome_labels ORDER BY target_at"
            )
        ]
        self.assertEqual(
            stored,
            [
                ("15m", D("0.015"), D("0.0072966966"), "2026-09-19T10:15:30.000000+00:00"),
                ("1h", D("-0.008"), D("-0.0157033034"), "2026-09-19T11:00:00.000000+00:00"),
                ("4h", D("0.0225"), D("0.0147966966"), "2026-09-19T14:01:30.000000+00:00"),
                ("24h", D("-0.03"), D("-0.0377033034"), "2026-09-20T10:01:59.000000+00:00"),
            ],
        )

    def test_the_spot_venue_is_the_kraken_adapter_venue(self):
        self.assertEqual(ost.SPOT_SNAPSHOT_VENUE, VENUE_SPOT)

    def test_another_pair_of_the_same_asset_never_labels_the_subject(self):
        conn = self.migrated()
        s = subject()
        ost.register_subject(conn, s, now=T0)
        self.add_quote(conn, T0 + timedelta(minutes=15, seconds=5), 90.0, 90.2, pair="XXBTZEUR")
        ost.label_due_outcomes(conn, now=T0 + timedelta(minutes=30), tolerance=TOL)
        (outcome,) = ost.load_outcomes(conn, s.subject_id)
        self.assertIs(outcome.label.market_missing, MissingReason.QUOTE_MISSING_AT_HORIZON)

    def test_missing_invalid_and_unsupported_are_stored_as_typed_reasons(self):
        conn = self.migrated()
        spot = subject()
        futures = InstrumentId("kraken-futures", "PF_XBTUSD", InstrumentKind.FUTURES, "BTC", "USD", "contracts")
        fut = subject(instrument=futures, pair="PF_XBTUSD", costs=())
        ost.register_subject(conn, spot, now=T0)
        ost.register_subject(conn, fut, now=T0)
        self.add_quote(conn, T0 + timedelta(minutes=15, seconds=1), 0.0, 100.2)  # invalid bid at 15m
        self.add_quote(conn, T0 + timedelta(hours=1, seconds=10), float("nan"), 100.2)  # NaN bid at 1h (REAL NULL)
        run = ost.label_due_outcomes(conn, now=T0 + timedelta(hours=2), tolerance=TOL)
        self.assertEqual((run.labeled_available, run.labeled_unavailable), (0, 4))
        rows = self.query(
            "SELECT s.pair, l.horizon, l.status, l.market_return, l.market_missing, l.net_markout, l.net_missing "
            "FROM outcome_labels l JOIN outcome_subjects s USING (subject_id) ORDER BY s.pair, l.target_at"
        )
        self.assertEqual(
            rows,
            [
                ("PF_XBTUSD", "15m", "UNAVAILABLE", None, "price_source_unsupported", None, "gross_unavailable"),
                ("PF_XBTUSD", "1h", "UNAVAILABLE", None, "price_source_unsupported", None, "gross_unavailable"),
                ("XXBTZUSD", "15m", "UNAVAILABLE", None, "quote_invalid_at_horizon", None, "gross_unavailable"),
                ("XXBTZUSD", "1h", "UNAVAILABLE", None, "quote_invalid_at_horizon", None, "gross_unavailable"),
            ],
        )

    def test_non_utc_or_malformed_snapshot_times_are_never_placed_in_time(self):
        conn = self.migrated()
        s = subject()
        ost.register_subject(conn, s, now=T0)
        self.add_quote(conn, None, 101.4, 101.6, ts="2026-09-19T10:15:30")  # naive
        self.add_quote(conn, None, 101.4, 101.6, ts="2026-09-19T10:15:40+01:00")  # not UTC text
        self.add_quote(conn, None, 101.4, 101.6, ts="2026-09-19T10:15:5")  # malformed
        ost.label_due_outcomes(conn, now=T0 + timedelta(minutes=30), tolerance=TOL)
        (outcome,) = ost.load_outcomes(conn, s.subject_id)
        self.assertIs(outcome.label.market_missing, MissingReason.QUOTE_MISSING_AT_HORIZON)


class TestIdempotence(TempCase):
    def test_repeating_the_labeler_neither_duplicates_nor_alters(self):
        conn = self.migrated()
        ost.register_subject(conn, subject(), now=T0)
        self.add_golden_quotes(conn)
        first = ost.label_due_outcomes(conn, now=T0 + timedelta(hours=25), tolerance=TOL)
        self.assertEqual(first.written, 4)
        self.forget(conn)
        before = snapshot(self.path)
        conn = self.connect()
        changes = conn.total_changes
        again = ost.label_due_outcomes(conn, now=T0 + timedelta(hours=25), tolerance=TOL)
        self.assertEqual((again.horizons_considered, again.written), (0, 0))
        # A late, different snapshot inside an old window does not rewrite a written label.
        self.add_quote(conn, T0 + timedelta(minutes=15, seconds=1), 150.0, 150.2)
        changes += 1
        later = ost.label_due_outcomes(conn, now=T0 + timedelta(days=3), tolerance=TOL)
        self.assertEqual(later.written, 0)
        self.assertEqual(conn.total_changes, changes)
        self.assertEqual(self.query("SELECT COUNT(*) FROM outcome_labels")[0][0], 4)
        self.assertEqual(
            [line for line in snapshot(self.path)[0] if "outcome_labels" in line],
            [line for line in before[0] if "outcome_labels" in line],
        )

    def test_labels_are_written_in_stages_as_each_horizon_matures(self):
        conn = self.migrated()
        s = subject()
        ost.register_subject(conn, s, now=T0)
        self.add_golden_quotes(conn)
        stages = []
        for now in (
            T0 + timedelta(minutes=14),
            T0 + timedelta(minutes=15, seconds=10),  # target passed, quote at +30 s not observed yet
            T0 + timedelta(minutes=16),
            T0 + timedelta(hours=5),
            T0 + timedelta(hours=5),
            T0 + timedelta(hours=24, seconds=119),
        ):
            run = ost.label_due_outcomes(conn, now=now, tolerance=TOL)
            stages.append((run.written, run.not_mature))
        self.assertEqual(stages, [(0, 0), (0, 1), (1, 0), (2, 0), (0, 0), (1, 0)])

    def test_registering_twice_is_a_no_op_and_changed_content_conflicts(self):
        conn = self.migrated()
        s = subject()
        self.assertTrue(ost.register_subject(conn, s, now=T0))
        self.assertFalse(ost.register_subject(conn, s, now=T0 + timedelta(hours=1)))
        self.assertEqual(self.query("SELECT COUNT(*) FROM outcome_subjects")[0][0], 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM outcome_subject_costs")[0][0], 4)
        for changed in (replace(s, entry_mid=D("100.5")), replace(s, costs=())):
            with self.assertRaises(OutcomeStoreError) as caught:
                ost.register_subject(conn, changed, now=T0)
            self.assertIs(caught.exception.code, OutcomeStoreFailure.SUBJECT_CONFLICT)
            self.assertFalse(conn.in_transaction)
        self.assertEqual(ost.load_subject(conn, s.subject_id), s)

    def test_saving_a_label_twice_is_a_no_op_and_a_changed_label_conflicts(self):
        conn = self.migrated()
        s = subject()
        ost.register_subject(conn, s, now=T0)
        label = label_horizon(s, Horizon.M15, golden_quotes(), now=T0 + timedelta(hours=1), tolerance=TOL)
        self.assertTrue(ost.save_label(conn, label, now=T0 + timedelta(hours=1)))
        self.assertFalse(ost.save_label(conn, label, now=T0 + timedelta(hours=2)))
        with self.assertRaises(OutcomeStoreError) as caught:
            ost.save_label(conn, replace(label, net_markout=D("0.5")), now=T0 + timedelta(hours=2))
        self.assertIs(caught.exception.code, OutcomeStoreFailure.LABEL_CONFLICT)
        (stored,) = ost.load_outcomes(conn, s.subject_id)
        self.assertEqual(stored.label, label)

    def test_sqlite_refuses_to_update_or_delete_subjects_costs_and_labels(self):
        conn = self.migrated()
        s = subject()
        ost.register_subject(conn, s, now=T0)
        self.add_golden_quotes(conn)
        ost.label_due_outcomes(conn, now=T0 + timedelta(hours=1, minutes=5), tolerance=TOL)
        for sql in (
            "UPDATE outcome_labels SET net_markout = '0'",
            "DELETE FROM outcome_labels",
            "UPDATE outcome_subjects SET arm = 'B'",
            "DELETE FROM outcome_subjects",
            "UPDATE outcome_subject_costs SET total_fraction = '0'",
            "DELETE FROM outcome_subject_costs",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                conn.execute(sql)
            conn.rollback()
        self.assertEqual(self.query("SELECT COUNT(*) FROM outcome_labels")[0][0], 2)

    def test_a_failure_mid_pass_rolls_back_every_label_of_the_pass(self):
        conn = self.migrated()
        ost.register_subject(conn, subject(), now=T0)
        ost.register_subject(conn, subject(arm="B"), now=T0)
        self.add_golden_quotes(conn)
        conn.execute(
            "CREATE TEMP TRIGGER t041_crash BEFORE INSERT ON main.outcome_labels "
            "WHEN (SELECT COUNT(*) FROM main.outcome_labels) >= 3 "
            "BEGIN SELECT RAISE(ABORT, 'injected crash on the fourth label'); END"
        )
        with self.assertRaises(OutcomeStoreError) as caught:
            ost.label_due_outcomes(conn, now=T0 + timedelta(hours=25), tolerance=TOL)
        self.assertIs(caught.exception.code, OutcomeStoreFailure.SQLITE_ERROR)
        self.assertFalse(conn.in_transaction)
        self.assertEqual(self.query("SELECT COUNT(*) FROM outcome_labels")[0][0], 0)


class TestNoFutureLeakage(TempCase):
    def test_poisoned_future_label_is_invisible_before_maturity(self):
        conn = self.migrated()
        s = subject()
        ost.register_subject(conn, s, now=T0)
        # A label written ahead of time (poison): its 1h outcome is only knowable at 11:00:30.
        poison = label_horizon(
            s, Horizon.H1, [quote(T0 + timedelta(hours=1, seconds=30), "150", "150.2", "poison")],
            now=T0 + timedelta(days=1), tolerance=TOL,
        )
        ost.save_label(conn, poison, now=T0)
        for cutoff in (T0, T0 + timedelta(minutes=30), T0 + timedelta(hours=1, seconds=29)):
            with self.subTest(cutoff=cutoff.isoformat()):
                self.assertEqual(ost.outcomes_known_as_of(conn, cutoff), ())
                self.assertEqual(ost.outcomes_known_as_of(conn, cutoff, venue="kraken", pair=PAIR), ())
        visible = ost.outcomes_known_as_of(conn, T0 + timedelta(hours=1, seconds=30), venue="kraken", pair=PAIR)
        self.assertEqual([o.label for o in visible], [poison])
        # The same cut-off seen through another time zone is the same instant.
        plus_two = timezone(timedelta(hours=2))
        self.assertEqual(ost.outcomes_known_as_of(conn, (T0 + timedelta(minutes=30)).astimezone(plus_two)), ())
        self.assertEqual(ost.outcomes_known_as_of(conn, T0 + timedelta(hours=2), pair="XXBTZEUR"), ())

    def test_a_snapshot_from_after_now_never_matures_a_label(self):
        conn = self.migrated()
        s = subject()
        ost.register_subject(conn, s, now=T0)
        self.add_quote(conn, T0 + timedelta(minutes=15, seconds=50), 120.0, 120.2)  # future relative to now
        run = ost.label_due_outcomes(conn, now=T0 + timedelta(minutes=15, seconds=10), tolerance=TOL)
        self.assertEqual((run.written, run.not_mature), (0, 1))
        self.assertEqual(self.query("SELECT COUNT(*) FROM outcome_labels")[0][0], 0)


class TestLegacyForwardReturnsUntouched(TempCase):
    def test_legacy_rows_and_table_are_unchanged_by_migration_register_and_labeling(self):
        build_legacy_db(self.path)
        before_rows = self.query("SELECT * FROM forward_returns ORDER BY id")
        before_sql = self.query("SELECT sql FROM sqlite_master WHERE tbl_name = 'forward_returns' ORDER BY name")
        before_idx = legacy_index_lists(self.path)["forward_returns"]
        SnapshotStore(self.path).close()
        conn = self.connect()
        ost.register_subject(conn, subject(), now=T0)
        self.add_golden_quotes(conn)
        ost.label_due_outcomes(conn, now=T0 + timedelta(hours=25), tolerance=TOL)
        self.assertEqual(self.query("SELECT * FROM forward_returns ORDER BY id"), before_rows)
        self.assertEqual(
            self.query("SELECT sql FROM sqlite_master WHERE tbl_name = 'forward_returns' ORDER BY name"), before_sql
        )
        self.assertEqual(legacy_index_lists(self.path)["forward_returns"], before_idx)
        # The pending legacy placeholder is still pending: nothing was inferred into it.
        self.assertEqual(self.query("SELECT return_pct FROM forward_returns WHERE horizon_minutes = 60"), [(None,)])


class TestLinks(TempCase):
    def claim(self, conn, evidence_hash, direction=Direction.LONG, native=PAIR):
        identity = InvocationIdentity(
            venue="kraken",
            market_kind=InstrumentKind.SPOT,
            native_instrument=native,
            setup="BREAKOUT",
            direction=direction,
            evidence_hash=evidence_hash,
            policy_version="OC-1",
        )
        result = ivs.claim_invocation(
            conn, InvocationRequest(identity, MODEL), ModelBudget(MODEL, 100, 1000), owner="test", now=T0,
            lease_seconds=60, new_id=lambda: "inv-" + direction.value.lower() + "-" + native.lower(),
        )
        return result.lease.invocation_id

    def test_sealed_evidence_and_invocation_links_are_verified_and_kept(self):
        conn = self.migrated()
        evidence = smig.sealed_evidence()
        es.save_evidence(conn, evidence, now=T0)
        invocation_id = self.claim(conn, evidence.content_hash)
        s = subject(
            evidence=evidence.evidence_id,
            decision=DecisionRef(DecisionKind.INVOCATION, invocation_id),
            arm="A0",
        )
        self.assertTrue(ost.register_subject(conn, s, now=T0))
        loaded = ost.load_subject(conn, s.subject_id)
        self.assertEqual((loaded.evidence, loaded.decision, loaded.arm), (evidence.evidence_id, s.decision, "A0"))
        row = self.query("SELECT evidence_id, evidence_missing, decision_kind, decision_ref, arm, arm_missing FROM outcome_subjects")
        self.assertEqual(row, [(evidence.evidence_id, None, "invocation", invocation_id, "A0", None)])

    def test_missing_links_are_stored_as_reasons_never_filled(self):
        conn = self.migrated()
        s = subject(evidence=LinkMissingReason.LEGACY_UNVERSIONED)
        ost.register_subject(conn, s, now=T0)
        row = self.query(
            "SELECT evidence_id, evidence_missing, decision_kind, decision_ref, decision_missing, arm, arm_missing "
            "FROM outcome_subjects"
        )
        self.assertEqual(row, [(None, "legacy_unversioned", None, None, "not_recorded", None, "not_recorded")])
        self.assertEqual(ost.load_subject(conn, s.subject_id), s)

    def test_dangling_or_mismatched_links_are_refused_and_write_nothing(self):
        conn = self.migrated()
        btc = smig.sealed_evidence()
        eth = smig.sealed_evidence(instrument=smig.ETH_USD)
        es.save_evidence(conn, btc, now=T0)
        es.save_evidence(conn, eth, now=T0)
        long_on_btc = self.claim(conn, btc.content_hash)
        short_on_btc = self.claim(conn, btc.content_hash, direction=Direction.SHORT)
        long_on_eth = self.claim(conn, eth.content_hash, native="XETHZUSD")
        cases = (
            (dict(evidence="evidence:sha256:" + "0" * 64), OutcomeStoreFailure.LINK_NOT_FOUND),
            (dict(evidence=eth.evidence_id), OutcomeStoreFailure.LINK_MISMATCH),
            (dict(decision=DecisionRef(DecisionKind.INVOCATION, "inv-missing")), OutcomeStoreFailure.LINK_NOT_FOUND),
            (dict(decision=DecisionRef(DecisionKind.INVOCATION, short_on_btc)), OutcomeStoreFailure.LINK_MISMATCH),
            (dict(decision=DecisionRef(DecisionKind.INVOCATION, long_on_eth)), OutcomeStoreFailure.LINK_MISMATCH),
            (
                dict(evidence=btc.evidence_id, decision=DecisionRef(DecisionKind.INVOCATION, long_on_eth)),
                OutcomeStoreFailure.LINK_MISMATCH,
            ),
        )
        for change, code in cases:
            with self.subTest(change=change), self.assertRaises(OutcomeStoreError) as caught:
                ost.register_subject(conn, subject(**change), now=T0)
            self.assertIs(caught.exception.code, code)
            self.assertFalse(conn.in_transaction)
        self.assertEqual(self.query("SELECT COUNT(*) FROM outcome_subjects")[0][0], 0)
        ok = subject(evidence=btc.evidence_id, decision=DecisionRef(DecisionKind.INVOCATION, long_on_btc))
        self.assertTrue(ost.register_subject(conn, ok, now=T0))


class TestModuleBoundary(unittest.TestCase):
    def test_domain_module_imports_no_io(self):
        import ast

        with open(os.path.join(REPO_ROOT, "radar_v08", "domain", "outcomes.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(
            imported,
            {
                "__future__", "hashlib", "json", "collections.abc", "dataclasses", "datetime", "decimal", "enum",
                "radar_v08.domain.costs", "radar_v08.domain.integrity", "radar_v08.domain.invocation",
            },
        )


if __name__ == "__main__":
    unittest.main()
