"""Pure fixture tests for radar_v08.domain.integrity (OC-1 section 1, task T022a).

No network, no files, no wall clock: every check receives an explicit ``now``.
Boundary tests pin each limit exactly at the bound (PASS) and one microsecond above.
"""

import ast
import os
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.domain import integrity as ig
from radar_v08.domain.integrity import (
    OC1_POLICY,
    AtrTimeframe,
    Bar,
    BookLevel,
    BookSide,
    BookSnapshot,
    Capability,
    CapabilityResult,
    CheckStatus,
    ClockSample,
    FuturesExpectation,
    FuturesObservation,
    InstrumentId,
    InstrumentKind,
    MarketSnapshot,
    MetadataValue,
    OhlcSeries,
    Reason,
    ReasonCode,
    RequiredDepth,
    SourceTiming,
    TickerObservation,
    TimeBasis,
    Trade,
    TradeSide,
    TradesObservation,
    TradingStatus,
    complete_buckets,
    evaluate_atr,
    evaluate_book,
    evaluate_clock,
    evaluate_futures,
    evaluate_metadata,
    evaluate_ohlc,
    evaluate_snapshot,
    evaluate_ticker,
    evaluate_trades,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
US = timedelta(microseconds=1)
FIVE_MIN = timedelta(minutes=5)

BTC_USD = InstrumentId("kraken", "XBT/USD", InstrumentKind.SPOT, "BTC", "USD", "BTC")
BTC_EUR = InstrumentId("kraken", "XBT/EUR", InstrumentKind.SPOT, "BTC", "EUR", "BTC")
PF_XBT = InstrumentId("kraken-futures", "PF_XBTUSD", InstrumentKind.FUTURES, "BTC", "USD", "BTC")
PF_ETH = InstrumentId("kraken-futures", "PF_ETHUSD", InstrumentKind.FUTURES, "ETH", "USD", "ETH")


def codes(result):
    return {reason.code for reason in result.reasons}


def ticker(**overrides):
    base = dict(
        instrument=BTC_USD,
        bid=100.0,
        ask=100.5,
        last=100.2,
        price_unit="USD",
        timing=SourceTiming(received_at=NOW - timedelta(seconds=10)),
        status=TradingStatus.ONLINE,
    )
    base.update(overrides)
    return TickerObservation(**base)


def bars(count, last_open=None, interval=FIVE_MIN, price=100.0):
    """``count`` contiguous coherent closed bars ending at ``last_open``."""
    last_open = last_open if last_open is not None else NOW - 2 * interval
    return tuple(
        Bar(last_open - (count - 1 - i) * interval, price, price + 1, price - 1, price + 0.5, 3.0)
        for i in range(count)
    )


def series(bar_tuple=None, **overrides):
    base = dict(
        instrument=BTC_USD,
        interval=FIVE_MIN,
        bars=bar_tuple if bar_tuple is not None else bars(20),
        price_unit="USD",
        volume_unit="BTC",
        timing=SourceTiming(received_at=NOW - timedelta(seconds=5)),
    )
    base.update(overrides)
    return OhlcSeries(**base)


def book(**overrides):
    base = dict(
        instrument=BTC_USD,
        bids=(BookLevel(100.0, 1.0), BookLevel(99.5, 2.0)),
        asks=(BookLevel(100.5, 1.5), BookLevel(101.0, 0.0)),
        price_unit="USD",
        size_unit="BTC",
        timing=SourceTiming(received_at=NOW - timedelta(seconds=3)),
    )
    base.update(overrides)
    return BookSnapshot(**base)


def trades(trade_tuple=None, **overrides):
    base = dict(
        instrument=BTC_USD,
        trades=trade_tuple
        if trade_tuple is not None
        else (Trade(NOW - timedelta(seconds=30), 100.1, 0.5, TradeSide.BUY),),
        price_unit="USD",
        size_unit="BTC",
        timing=SourceTiming(received_at=NOW - timedelta(seconds=2)),
    )
    base.update(overrides)
    return TradesObservation(**base)


def futures(instrument=PF_XBT, **overrides):
    base = dict(
        instrument=instrument,
        bid=100.0,
        ask=100.2,
        last=100.1,
        price_unit="USD",
        timing=SourceTiming(received_at=NOW - timedelta(seconds=4)),
        last_trade_time=NOW - timedelta(seconds=20),
        quote_time=NOW - timedelta(seconds=5),
    )
    base.update(overrides)
    return FuturesObservation(**base)


GOOD_CLOCK = ClockSample(True, timedelta(milliseconds=100), NOW - timedelta(seconds=60))


def snapshot(**overrides):
    base = dict(
        selected=BTC_USD,
        ticker=ticker(),
        ohlc=series(),
        book=book(),
        trades=trades(),
        clock=GOOD_CLOCK,
    )
    base.update(overrides)
    return MarketSnapshot(**base)


class TestTypesAndContract(unittest.TestCase):
    def test_every_reason_code_maps_to_fail_or_unknown(self):
        for code in ReasonCode:
            self.assertIn(code.status, (CheckStatus.FAIL, CheckStatus.UNKNOWN), code)

    def test_hard_codes_are_fail(self):
        for code in ig.HARD_REASONS:
            self.assertIs(code.status, CheckStatus.FAIL)
        for code in (
            ReasonCode.INVALID_NUMBER,
            ReasonCode.UNIT_MISMATCH,
            ReasonCode.IDENTITY_MISMATCH,
            ReasonCode.OHLC_INCOHERENT,
        ):
            self.assertTrue(code.hard)
        self.assertFalse(ReasonCode.STALE_RECEIPT.hard)

    def test_models_are_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            BTC_USD.symbol = "XBT/EUR"
        result = evaluate_ticker(ticker(), BTC_USD, NOW)
        with self.assertRaises(FrozenInstanceError):
            result.status = CheckStatus.FAIL

    def test_status_must_match_reasons(self):
        reason = Reason(ReasonCode.STALE_RECEIPT, "x", "y")
        with self.assertRaises(ValueError):
            CapabilityResult(Capability.BOOK, "s", CheckStatus.PASS, (reason,), TimeBasis.NONE)
        with self.assertRaises(ValueError):
            CapabilityResult(Capability.BOOK, "s", CheckStatus.NOT_APPLICABLE, (reason,), TimeBasis.NONE)

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_ticker(ticker(), BTC_USD, NOW.replace(tzinfo=None))

    def test_deterministic_for_equal_inputs(self):
        self.assertEqual(evaluate_snapshot(snapshot(), NOW), evaluate_snapshot(snapshot(), NOW))

    def test_policy_defaults_are_oc1(self):
        self.assertEqual(OC1_POLICY.version, "OC-1")
        self.assertEqual(OC1_POLICY.ticker_max_age, timedelta(seconds=90))
        self.assertEqual(OC1_POLICY.ohlc_last_close_max_age, timedelta(minutes=6))
        self.assertEqual(OC1_POLICY.atr_min_bars, 15)
        self.assertEqual(OC1_POLICY.book_max_age, timedelta(seconds=15))
        self.assertEqual(OC1_POLICY.trades_receipt_max_age, timedelta(seconds=15))
        self.assertEqual(OC1_POLICY.last_trade_max_age, timedelta(seconds=60))
        self.assertEqual(OC1_POLICY.futures_max_age, timedelta(seconds=60))
        self.assertEqual(OC1_POLICY.clock_max_uncertainty, timedelta(milliseconds=500))


class TestPolicyPerturbation(unittest.TestCase):
    """Each configured limit changes the outcome when tightened by one microsecond or one bar."""

    def test_each_limit_is_live(self):
        cases = [
            ("ticker_max_age", lambda p: evaluate_ticker(ticker(), BTC_USD, NOW, p), timedelta(seconds=10)),
            ("ohlc_last_close_max_age", lambda p: evaluate_ohlc(series(), BTC_USD, NOW, p), FIVE_MIN),
            ("book_max_age", lambda p: evaluate_book(book(), BTC_USD, NOW, p), timedelta(seconds=3)),
            ("trades_receipt_max_age", lambda p: evaluate_trades(trades(), BTC_USD, NOW, p), timedelta(seconds=2)),
            ("last_trade_max_age", lambda p: evaluate_trades(trades(), BTC_USD, NOW, p), timedelta(seconds=30)),
            ("futures_max_age", lambda p: evaluate_futures(futures(), PF_XBT, NOW, p), timedelta(seconds=20)),
            (
                "clock_max_uncertainty",
                lambda p: evaluate_clock(GOOD_CLOCK, NOW, p),
                timedelta(milliseconds=100),
            ),
        ]
        for name, check, observed in cases:
            with self.subTest(limit=name):
                self.assertIs(check(replace(OC1_POLICY, **{name: observed})).status, CheckStatus.PASS)
                self.assertIsNot(check(replace(OC1_POLICY, **{name: observed - US})).status, CheckStatus.PASS)

    def test_atr_period_is_live(self):
        fifteen = series(bars(15))
        self.assertIs(evaluate_atr(fifteen, BTC_USD, NOW, AtrTimeframe.M5).status, CheckStatus.PASS)
        stricter = replace(OC1_POLICY, atr_period=15)
        self.assertIs(evaluate_atr(fifteen, BTC_USD, NOW, AtrTimeframe.M5, stricter).status, CheckStatus.UNKNOWN)


class TestPurity(unittest.TestCase):
    """The module must not read the wall clock nor import I/O (FAILURE_AND_QUALITY)."""

    def setUp(self):
        with open(ig.__file__, encoding="utf-8") as handle:
            self.source = handle.read()
        self.tree = ast.parse(self.source)

    def test_imports_are_stdlib_pure(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, {"__future__", "math", "dataclasses", "datetime", "decimal", "enum"})

    def test_no_wall_clock_calls(self):
        for name in ("now", "utcnow", "today", "time", "monotonic", "perf_counter"):
            for node in ast.walk(self.tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotEqual(node.func.attr, name, f"wall-clock call .{name}()")
        self.assertNotIn("open(", self.source)


class TestTicker(unittest.TestCase):
    def test_valid_ticker_passes_receipt_only(self):
        result = evaluate_ticker(ticker(), BTC_USD, NOW)
        self.assertIs(result.status, CheckStatus.PASS)
        self.assertIs(result.time_basis, TimeBasis.RECEIPT_ONLY)
        self.assertIsNone(result.source_time)

    def test_source_time_preserved(self):
        source = NOW - timedelta(seconds=12)
        timing = SourceTiming(NOW - timedelta(seconds=10), source)
        result = evaluate_ticker(ticker(timing=timing), BTC_USD, NOW)
        self.assertIs(result.time_basis, TimeBasis.SOURCE)
        self.assertEqual(result.source_time, source)

    def test_receipt_boundary_90s(self):
        at = evaluate_ticker(ticker(timing=SourceTiming(NOW - timedelta(seconds=90))), BTC_USD, NOW)
        above = evaluate_ticker(ticker(timing=SourceTiming(NOW - timedelta(seconds=90) - US)), BTC_USD, NOW)
        self.assertIs(at.status, CheckStatus.PASS)
        self.assertIs(above.status, CheckStatus.FAIL)
        self.assertEqual(codes(above), {ReasonCode.STALE_RECEIPT})
        self.assertFalse(above.hard_failure)

    def test_source_age_boundary_90s(self):
        received = NOW - timedelta(seconds=1)
        at = evaluate_ticker(ticker(timing=SourceTiming(received, NOW - timedelta(seconds=90))), BTC_USD, NOW)
        above = evaluate_ticker(
            ticker(timing=SourceTiming(received, NOW - timedelta(seconds=90) - US)), BTC_USD, NOW
        )
        self.assertIs(at.status, CheckStatus.PASS)
        self.assertEqual(codes(above), {ReasonCode.STALE_SOURCE})

    def test_nan_and_inf_fail_hard(self):
        for bad in (float("nan"), float("inf"), float("-inf"), Decimal("NaN"), True):
            result = evaluate_ticker(ticker(last=bad), BTC_USD, NOW)
            self.assertIs(result.status, CheckStatus.FAIL, bad)
            self.assertIn(ReasonCode.INVALID_NUMBER, codes(result))
            self.assertTrue(result.hard_failure)

    def test_crossed_and_nonpositive(self):
        self.assertIn(ReasonCode.CROSSED_QUOTE, codes(evaluate_ticker(ticker(bid=101.0), BTC_USD, NOW)))
        self.assertIn(ReasonCode.CROSSED_QUOTE, codes(evaluate_ticker(ticker(bid=100.5), BTC_USD, NOW)))
        self.assertIn(ReasonCode.NONPOSITIVE_PRICE, codes(evaluate_ticker(ticker(bid=0.0), BTC_USD, NOW)))

    def test_wrong_pair_and_swapped_unit(self):
        wrong = evaluate_ticker(ticker(instrument=BTC_EUR), BTC_USD, NOW)
        self.assertEqual(codes(wrong), {ReasonCode.IDENTITY_MISMATCH})
        unit = evaluate_ticker(ticker(price_unit="EUR"), BTC_USD, NOW)
        self.assertEqual(codes(unit), {ReasonCode.UNIT_MISMATCH})
        self.assertTrue(unit.hard_failure)

    def test_future_dated_and_naive(self):
        future = evaluate_ticker(ticker(timing=SourceTiming(NOW + US)), BTC_USD, NOW)
        self.assertEqual(codes(future), {ReasonCode.FUTURE_DATED})
        naive = evaluate_ticker(ticker(timing=SourceTiming(NOW.replace(tzinfo=None))), BTC_USD, NOW)
        self.assertEqual(codes(naive), {ReasonCode.NAIVE_TIMESTAMP})

    def test_source_future_tolerance_500ms(self):
        received = NOW - timedelta(seconds=1)
        at = evaluate_ticker(
            ticker(timing=SourceTiming(received, NOW + timedelta(milliseconds=500))), BTC_USD, NOW
        )
        above = evaluate_ticker(
            ticker(timing=SourceTiming(received, NOW + timedelta(milliseconds=500) + US)), BTC_USD, NOW
        )
        self.assertIs(at.status, CheckStatus.PASS)
        self.assertEqual(codes(above), {ReasonCode.FUTURE_DATED})

    def test_status_missing_is_unknown_and_offline_fails(self):
        self.assertEqual(codes(evaluate_ticker(ticker(status=None), BTC_USD, NOW)), {ReasonCode.MISSING_METADATA})
        offline = evaluate_ticker(ticker(status=TradingStatus.OFFLINE), BTC_USD, NOW)
        self.assertEqual(codes(offline), {ReasonCode.MARKET_NOT_ONLINE})
        self.assertIs(offline.status, CheckStatus.FAIL)

    def test_missing_ticker_is_unknown(self):
        result = evaluate_ticker(None, BTC_USD, NOW)
        self.assertIs(result.status, CheckStatus.UNKNOWN)
        self.assertEqual(codes(result), {ReasonCode.MISSING_OBSERVATION})


class TestOhlc(unittest.TestCase):
    def test_valid_series_passes(self):
        result = evaluate_ohlc(series(), BTC_USD, NOW, required_bars=20)
        self.assertIs(result.status, CheckStatus.PASS)
        self.assertEqual(result.source_time, NOW - FIVE_MIN)

    def test_last_close_boundary_6min(self):
        # Last bar 11:50-11:55; evaluated at 12:01 its close is exactly 6 minutes old.
        now = datetime(2026, 9, 18, 12, 1, tzinfo=UTC)
        at = series(bars(5, datetime(2026, 9, 18, 11, 50, tzinfo=UTC)), timing=SourceTiming(now))
        self.assertIs(evaluate_ohlc(at, BTC_USD, now).status, CheckStatus.PASS)
        above = evaluate_ohlc(at, BTC_USD, now + US)
        self.assertEqual(codes(above), {ReasonCode.STALE_SOURCE})
        self.assertFalse(above.hard_failure)

    def test_open_bar_fails(self):
        # Last bar closes one microsecond after the response was received.
        last_open = datetime(2026, 9, 18, 11, 55, tzinfo=UTC)
        at = series(bars(3, last_open), timing=SourceTiming(last_open + FIVE_MIN))
        open_bar = series(bars(3, last_open), timing=SourceTiming(last_open + FIVE_MIN - US))
        self.assertIs(evaluate_ohlc(at, BTC_USD, NOW).status, CheckStatus.PASS)
        result = evaluate_ohlc(open_bar, BTC_USD, NOW)
        self.assertEqual(codes(result), {ReasonCode.BAR_NOT_CLOSED})
        self.assertTrue(result.hard_failure)

    def test_incoherent_bar_fails_hard(self):
        good = bars(5)
        bad = replace(good[2], high=good[2].low - 1)
        result = evaluate_ohlc(series(good[:2] + (bad,) + good[3:]), BTC_USD, NOW)
        self.assertEqual(codes(result), {ReasonCode.OHLC_INCOHERENT})
        close_above_high = replace(good[2], close=good[2].high + 0.01)
        result = evaluate_ohlc(series(good[:2] + (close_above_high,) + good[3:]), BTC_USD, NOW)
        self.assertEqual(codes(result), {ReasonCode.OHLC_INCOHERENT})

    def test_nan_price_and_negative_volume(self):
        good = bars(5)
        nan_bar = replace(good[1], close=float("nan"))
        self.assertIn(
            ReasonCode.INVALID_NUMBER, codes(evaluate_ohlc(series(good[:1] + (nan_bar,) + good[2:]), BTC_USD, NOW))
        )
        neg = replace(good[1], volume=-0.1)
        self.assertEqual(codes(evaluate_ohlc(series(good[:1] + (neg,) + good[2:]), BTC_USD, NOW)),
                         {ReasonCode.INVALID_SIZE})
        zero = replace(good[1], volume=0.0)
        self.assertIs(evaluate_ohlc(series(good[:1] + (zero,) + good[2:]), BTC_USD, NOW).status, CheckStatus.PASS)

    def test_misaligned_and_unordered(self):
        good = bars(5)
        shifted = tuple(replace(bar, open_time=bar.open_time + timedelta(seconds=1)) for bar in good)
        result = evaluate_ohlc(series(shifted, timing=SourceTiming(NOW)), BTC_USD, NOW)
        self.assertIn(ReasonCode.BAR_MISALIGNED, codes(result))
        swapped = good[:1] + (good[2], good[1]) + good[3:]
        self.assertIn(ReasonCode.BAR_ORDER, codes(evaluate_ohlc(series(swapped), BTC_USD, NOW)))
        dup = good[:2] + (good[1],) + good[3:]
        self.assertIn(ReasonCode.BAR_ORDER, codes(evaluate_ohlc(series(dup), BTC_USD, NOW)))

    def test_gap_in_claimed_window_is_unknown(self):
        good = bars(10)
        gapped = good[:4] + good[5:]  # one missing bar, 5 contiguous bars at the end
        self.assertIs(evaluate_ohlc(series(gapped), BTC_USD, NOW, required_bars=5).status, CheckStatus.PASS)
        result = evaluate_ohlc(series(gapped), BTC_USD, NOW, required_bars=6)
        self.assertEqual(codes(result), {ReasonCode.BAR_GAP})
        self.assertIs(result.status, CheckStatus.UNKNOWN)

    def test_too_few_bars_and_empty(self):
        self.assertEqual(
            codes(evaluate_ohlc(series(bars(3)), BTC_USD, NOW, required_bars=4)), {ReasonCode.INSUFFICIENT_BARS}
        )
        self.assertEqual(codes(evaluate_ohlc(series(()), BTC_USD, NOW)), {ReasonCode.MISSING_OBSERVATION})

    def test_units_identity_interval(self):
        self.assertEqual(codes(evaluate_ohlc(series(price_unit="EUR"), BTC_USD, NOW)), {ReasonCode.UNIT_MISMATCH})
        self.assertEqual(codes(evaluate_ohlc(series(volume_unit="USD"), BTC_USD, NOW)), {ReasonCode.UNIT_MISMATCH})
        self.assertEqual(
            codes(evaluate_ohlc(series(interval=timedelta(minutes=1)), BTC_USD, NOW)), {ReasonCode.UNIT_MISMATCH}
        )
        self.assertEqual(codes(evaluate_ohlc(series(instrument=BTC_EUR), BTC_USD, NOW)),
                         {ReasonCode.IDENTITY_MISMATCH})


class TestAtr(unittest.TestCase):
    def test_boundary_15_bars(self):
        self.assertIs(evaluate_atr(series(bars(15)), BTC_USD, NOW, AtrTimeframe.M5).status, CheckStatus.PASS)
        below = evaluate_atr(series(bars(14)), BTC_USD, NOW, AtrTimeframe.M5)
        self.assertEqual(codes(below), {ReasonCode.INSUFFICIENT_BARS})
        self.assertIs(below.status, CheckStatus.UNKNOWN)

    def test_gap_limits_atr_to_contiguous_tail(self):
        good = bars(30)
        gapped = good[:15] + good[16:]  # 14 contiguous bars at the end
        self.assertEqual(codes(evaluate_atr(series(gapped), BTC_USD, NOW, AtrTimeframe.M5)),
                         {ReasonCode.INSUFFICIENT_BARS})

    def test_invalid_series_propagates_fail(self):
        good = bars(20)
        bad = replace(good[5], low=float("inf"))
        result = evaluate_atr(series(good[:5] + (bad,) + good[6:]), BTC_USD, NOW, AtrTimeframe.M5)
        self.assertIs(result.status, CheckStatus.FAIL)
        self.assertTrue(result.hard_failure)

    def test_1h_needs_own_complete_coverage(self):
        # Last complete hour closes at 11:00 when now is 12:00; bars through 11:55 open.
        last_open = datetime(2026, 9, 18, 11, 50, tzinfo=UTC)
        exact = bars(15 * 12 + 11, last_open)  # 15 complete hours + 11 bars of the current hour
        self.assertEqual(complete_buckets(exact, FIVE_MIN, timedelta(hours=1)), 15)
        self.assertIs(evaluate_atr(series(exact), BTC_USD, NOW, AtrTimeframe.H1).status, CheckStatus.PASS)
        short = exact[1:]  # first hour now partial: only 14 complete hours
        result = evaluate_atr(series(short), BTC_USD, NOW, AtrTimeframe.H1)
        self.assertEqual(codes(result), {ReasonCode.INSUFFICIENT_BARS})
        self.assertEqual(result.subject, "1h")

    def test_1h_never_relabels_5m_bars(self):
        # 15 valid 5m bars pass a 5m ATR but give zero complete hours.
        fifteen = bars(15)
        self.assertIs(evaluate_atr(series(fifteen), BTC_USD, NOW, AtrTimeframe.M5).status, CheckStatus.PASS)
        self.assertIs(evaluate_atr(series(fifteen), BTC_USD, NOW, AtrTimeframe.H1).status, CheckStatus.UNKNOWN)


class TestBook(unittest.TestCase):
    def test_valid_book(self):
        self.assertIs(evaluate_book(book(), BTC_USD, NOW).status, CheckStatus.PASS)

    def test_receipt_boundary_15s(self):
        at = evaluate_book(book(timing=SourceTiming(NOW - timedelta(seconds=15))), BTC_USD, NOW)
        above = evaluate_book(book(timing=SourceTiming(NOW - timedelta(seconds=15) - US)), BTC_USD, NOW)
        self.assertIs(at.status, CheckStatus.PASS)
        self.assertEqual(codes(above), {ReasonCode.STALE_RECEIPT})

    def test_source_age_checked_when_provided(self):
        received = NOW - timedelta(seconds=1)
        at = evaluate_book(book(timing=SourceTiming(received, NOW - timedelta(seconds=15))), BTC_USD, NOW)
        above = evaluate_book(book(timing=SourceTiming(received, NOW - timedelta(seconds=15) - US)), BTC_USD, NOW)
        self.assertIs(at.time_basis, TimeBasis.SOURCE)
        self.assertIs(at.status, CheckStatus.PASS)
        self.assertEqual(codes(above), {ReasonCode.STALE_SOURCE})

    def test_crossed_and_locked(self):
        crossed = book(bids=(BookLevel(101.0, 1.0),))
        self.assertEqual(codes(evaluate_book(crossed, BTC_USD, NOW)), {ReasonCode.CROSSED_QUOTE})
        locked = book(bids=(BookLevel(100.5, 1.0),))
        self.assertEqual(codes(evaluate_book(locked, BTC_USD, NOW)), {ReasonCode.CROSSED_QUOTE})

    def test_unordered_sides(self):
        bad_bids = book(bids=(BookLevel(99.0, 1.0), BookLevel(99.5, 1.0)))
        self.assertEqual(codes(evaluate_book(bad_bids, BTC_USD, NOW)), {ReasonCode.BOOK_SIDE_UNORDERED})
        dup_asks = book(asks=(BookLevel(100.5, 1.0), BookLevel(100.5, 1.0)))
        self.assertEqual(codes(evaluate_book(dup_asks, BTC_USD, NOW)), {ReasonCode.BOOK_SIDE_UNORDERED})

    def test_prices_and_sizes(self):
        self.assertEqual(codes(evaluate_book(book(asks=(BookLevel(0.0, 1.0),)), BTC_USD, NOW)),
                         {ReasonCode.NONPOSITIVE_PRICE})
        self.assertEqual(codes(evaluate_book(book(asks=(BookLevel(100.5, -1e-9),)), BTC_USD, NOW)),
                         {ReasonCode.INVALID_SIZE})
        self.assertIn(ReasonCode.INVALID_NUMBER,
                      codes(evaluate_book(book(asks=(BookLevel(float("nan"), 1.0),)), BTC_USD, NOW)))

    def test_empty_side_is_unknown(self):
        result = evaluate_book(book(asks=()), BTC_USD, NOW)
        self.assertEqual(codes(result), {ReasonCode.EMPTY_BOOK_SIDE})
        self.assertIs(result.status, CheckStatus.UNKNOWN)

    def test_required_depth_boundary(self):
        exact = RequiredDepth(BookSide.ASK, Decimal("1.5"))
        above = RequiredDepth(BookSide.ASK, Decimal("1.5000001"))
        self.assertIs(evaluate_book(book(), BTC_USD, NOW, required_depth=exact).status, CheckStatus.PASS)
        self.assertEqual(codes(evaluate_book(book(), BTC_USD, NOW, required_depth=above)),
                         {ReasonCode.SIZE_NOT_COVERED})
        bids = RequiredDepth(BookSide.BID, 3.0)
        self.assertIs(evaluate_book(book(), BTC_USD, NOW, required_depth=bids).status, CheckStatus.PASS)

    def test_units_and_identity(self):
        self.assertEqual(codes(evaluate_book(book(size_unit="USD"), BTC_USD, NOW)), {ReasonCode.UNIT_MISMATCH})
        self.assertEqual(codes(evaluate_book(book(instrument=BTC_EUR), BTC_USD, NOW)),
                         {ReasonCode.IDENTITY_MISMATCH})


class TestTrades(unittest.TestCase):
    def test_valid_trades(self):
        result = evaluate_trades(trades(), BTC_USD, NOW)
        self.assertIs(result.status, CheckStatus.PASS)
        self.assertEqual(result.source_time, NOW - timedelta(seconds=30))

    def test_receipt_boundary_15s(self):
        at = evaluate_trades(trades(timing=SourceTiming(NOW - timedelta(seconds=15))), BTC_USD, NOW)
        above = evaluate_trades(trades(timing=SourceTiming(NOW - timedelta(seconds=15) - US)), BTC_USD, NOW)
        self.assertIs(at.status, CheckStatus.PASS)
        self.assertEqual(codes(above), {ReasonCode.STALE_RECEIPT})

    def test_last_trade_boundary_60s(self):
        at = trades((Trade(NOW - timedelta(seconds=60), 100.0, 1.0, TradeSide.SELL),))
        above = trades((Trade(NOW - timedelta(seconds=60) - US, 100.0, 1.0, TradeSide.SELL),))
        self.assertIs(evaluate_trades(at, BTC_USD, NOW).status, CheckStatus.PASS)
        stale = evaluate_trades(above, BTC_USD, NOW)
        self.assertEqual(codes(stale), {ReasonCode.STALE_LAST_TRADE})
        self.assertIs(stale.status, CheckStatus.UNKNOWN)
        self.assertIs(evaluate_trades(above, BTC_USD, NOW, active_trade_claim=False).status, CheckStatus.PASS)

    def test_latest_trade_used_regardless_of_order(self):
        old = Trade(NOW - timedelta(minutes=5), 100.0, 1.0, TradeSide.SELL)
        new = Trade(NOW - timedelta(seconds=10), 100.0, 1.0, TradeSide.BUY)
        self.assertIs(evaluate_trades(trades((new, old)), BTC_USD, NOW).status, CheckStatus.PASS)

    def test_no_trades_is_unknown_never_zero(self):
        result = evaluate_trades(trades(()), BTC_USD, NOW)
        self.assertIs(result.status, CheckStatus.UNKNOWN)
        self.assertEqual(codes(result), {ReasonCode.NO_TRADES})
        self.assertIsNone(result.source_time)
        self.assertIs(result.time_basis, TimeBasis.RECEIPT_ONLY)

    def test_invalid_trades(self):
        zero = trades((Trade(NOW - timedelta(seconds=5), 100.0, 0.0, TradeSide.BUY),))
        self.assertEqual(codes(evaluate_trades(zero, BTC_USD, NOW)), {ReasonCode.INVALID_SIZE})
        future = trades((Trade(NOW + timedelta(seconds=1), 100.0, 1.0, TradeSide.BUY),))
        self.assertEqual(codes(evaluate_trades(future, BTC_USD, NOW)), {ReasonCode.FUTURE_DATED})
        swapped = trades(size_unit="USD", price_unit="BTC")
        self.assertEqual(codes(evaluate_trades(swapped, BTC_USD, NOW)), {ReasonCode.UNIT_MISMATCH})


class TestFutures(unittest.TestCase):
    def test_valid_future(self):
        result = evaluate_futures(futures(), PF_XBT, NOW)
        self.assertIs(result.status, CheckStatus.PASS)
        self.assertEqual(result.source_time, NOW - timedelta(seconds=5))

    def test_reported_time_boundary_60s(self):
        at = futures(last_trade_time=NOW - timedelta(seconds=60))
        above = futures(last_trade_time=NOW - timedelta(seconds=60) - US)
        self.assertIs(evaluate_futures(at, PF_XBT, NOW).status, CheckStatus.PASS)
        self.assertEqual(codes(evaluate_futures(above, PF_XBT, NOW)), {ReasonCode.STALE_SOURCE})

    def test_receipt_boundary_60s(self):
        at = futures(timing=SourceTiming(NOW - timedelta(seconds=60)))
        above = futures(timing=SourceTiming(NOW - timedelta(seconds=60) - US))
        self.assertIs(evaluate_futures(at, PF_XBT, NOW).status, CheckStatus.PASS)
        self.assertEqual(codes(evaluate_futures(above, PF_XBT, NOW)), {ReasonCode.STALE_RECEIPT})

    def test_no_reported_time_is_receipt_only_unknown(self):
        result = evaluate_futures(futures(last_trade_time=None, quote_time=None), PF_XBT, NOW)
        self.assertIs(result.status, CheckStatus.UNKNOWN)
        self.assertIs(result.time_basis, TimeBasis.RECEIPT_ONLY)
        self.assertIsNone(result.source_time)
        self.assertEqual(codes(result), {ReasonCode.SOURCE_TIME_ABSENT})

    def test_spot_expectation_or_wrong_contract_fails(self):
        self.assertIn(ReasonCode.IDENTITY_MISMATCH, codes(evaluate_futures(futures(), BTC_USD, NOW)))
        self.assertEqual(codes(evaluate_futures(futures(instrument=PF_ETH), PF_XBT, NOW)),
                         {ReasonCode.IDENTITY_MISMATCH})


class TestClock(unittest.TestCase):
    def test_uncertainty_boundary_500ms(self):
        at = ClockSample(True, timedelta(milliseconds=500), None)
        above = ClockSample(True, timedelta(milliseconds=500) + US, None)
        self.assertIs(evaluate_clock(at, NOW).status, CheckStatus.PASS)
        self.assertEqual(codes(evaluate_clock(above, NOW)), {ReasonCode.CLOCK_UNCERTAINTY_EXCEEDED})

    def test_backward_jump_fails(self):
        same = ClockSample(True, timedelta(0), NOW)
        back = ClockSample(True, timedelta(0), NOW + US)
        self.assertIs(evaluate_clock(same, NOW).status, CheckStatus.PASS)
        self.assertEqual(codes(evaluate_clock(back, NOW)), {ReasonCode.CLOCK_BACKWARD_JUMP})

    def test_unknown_sync_and_uncertainty(self):
        self.assertEqual(codes(evaluate_clock(ClockSample(None, timedelta(0), None), NOW)),
                         {ReasonCode.CLOCK_SYNC_UNKNOWN})
        self.assertEqual(codes(evaluate_clock(ClockSample(True, None, None), NOW)),
                         {ReasonCode.CLOCK_UNCERTAINTY_UNKNOWN})
        self.assertEqual(codes(evaluate_clock(ClockSample(False, timedelta(0), None), NOW)),
                         {ReasonCode.CLOCK_NOT_SYNCHRONIZED})


class TestMetadata(unittest.TestCase):
    def test_missing_metadata_is_unknown_for_that_claim(self):
        self.assertEqual(codes(evaluate_metadata(MetadataValue("beta", None))), {ReasonCode.MISSING_METADATA})
        self.assertIs(evaluate_metadata(MetadataValue("funding", Decimal("0.0001"))).status, CheckStatus.PASS)
        self.assertEqual(codes(evaluate_metadata(MetadataValue("beta", float("nan")))), {ReasonCode.INVALID_NUMBER})


class TestSnapshot(unittest.TestCase):
    def test_all_good_is_not_blocked(self):
        report = evaluate_snapshot(snapshot(), NOW)
        self.assertFalse(report.opportunity_blocked)
        self.assertEqual(report.policy_version, "OC-1")
        self.assertIs(report.result(Capability.ATR, "1h").status, CheckStatus.NOT_APPLICABLE)
        self.assertIs(report.result(Capability.FUTURES, "*").status, CheckStatus.NOT_APPLICABLE)
        self.assertEqual(report.eligible_futures, ())

    def test_bad_future_removes_only_that_future(self):
        report = evaluate_snapshot(
            snapshot(
                futures=(
                    FuturesExpectation(PF_XBT, futures(bid=float("nan"))),
                    FuturesExpectation(PF_ETH, futures(instrument=PF_ETH)),
                )
            ),
            NOW,
        )
        self.assertFalse(report.opportunity_blocked)
        self.assertEqual(report.eligible_futures, ("PF_ETHUSD",))
        self.assertIs(report.result(Capability.FUTURES, "PF_XBTUSD").status, CheckStatus.FAIL)

    def test_stale_future_never_aggregated_as_fresh(self):
        stale = futures(quote_time=NOW - timedelta(minutes=2), last_trade_time=None)
        report = evaluate_snapshot(
            snapshot(futures=(FuturesExpectation(PF_XBT, stale), FuturesExpectation(PF_ETH, None))), NOW
        )
        self.assertEqual(report.eligible_futures, ())
        self.assertIs(report.result(Capability.FUTURES, "PF_ETHUSD").status, CheckStatus.UNKNOWN)
        self.assertFalse(report.opportunity_blocked)

    def test_bad_selected_spot_blocks(self):
        report = evaluate_snapshot(snapshot(ticker=ticker(instrument=BTC_EUR)), NOW)
        self.assertTrue(report.opportunity_blocked)
        self.assertEqual([r.capability for r in report.blocking_results], [Capability.SPOT_TICKER])

    def test_missing_required_blocks_but_no_trades_does_not(self):
        self.assertTrue(evaluate_snapshot(snapshot(book=None), NOW).opportunity_blocked)
        no_trades = evaluate_snapshot(snapshot(trades=trades(())), NOW)
        self.assertFalse(no_trades.opportunity_blocked)
        self.assertIs(no_trades.result(Capability.TRADES, "XBT/USD").status, CheckStatus.UNKNOWN)

    def test_missing_metadata_does_not_block(self):
        report = evaluate_snapshot(snapshot(metadata=(MetadataValue("unlock", None),)), NOW)
        self.assertFalse(report.opportunity_blocked)
        self.assertIs(report.result(Capability.METADATA, "unlock").status, CheckStatus.UNKNOWN)

    def test_untrusted_clock_suspends_freshness(self):
        report = evaluate_snapshot(snapshot(clock=ClockSample(True, timedelta(0), NOW + timedelta(seconds=1))), NOW)
        self.assertTrue(report.opportunity_blocked)
        self.assertIs(report.clock.status, CheckStatus.FAIL)
        ticker_result = report.result(Capability.SPOT_TICKER, "XBT/USD")
        self.assertIs(ticker_result.status, CheckStatus.UNKNOWN)
        self.assertIn(ReasonCode.CLOCK_UNTRUSTED, codes(ticker_result))

    def test_short_history_makes_atr_unknown_without_blocking(self):
        report = evaluate_snapshot(snapshot(ohlc=series(bars(10))), NOW)
        self.assertIs(report.result(Capability.ATR, "5m").status, CheckStatus.UNKNOWN)
        self.assertFalse(report.opportunity_blocked)

    def test_claimed_1h_atr_evaluated(self):
        report = evaluate_snapshot(snapshot(claim_atr_1h=True), NOW)
        self.assertIs(report.result(Capability.ATR, "1h").status, CheckStatus.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
