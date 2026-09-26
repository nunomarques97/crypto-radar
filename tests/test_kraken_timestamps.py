"""Fixture tests for radar_v08.adapters.kraken_timestamps.

No network: every fetch is called with a fake, duck-typed session (same
pattern as tests/test_l2_ohlc.py) and an injected fake clock. Covers: source
time present/absent per fetch, seconds-not-milliseconds on Depth's per-level
timestamp, and one bad/missing futures serverTime staying isolated from the
rest of the payload. Also covers regressions for malformed, non-finite and
milliseconds-sized level/trade times (isolated to `None`, fetch never fails,
`fetch_depth` unchanged) and for the receipt clock being read after the
response, not before the request.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.adapters.kraken_timestamps import (
    FUTURES_QUOTE,
    MAX_EPOCH_SECONDS,
    VENUE_FUTURES,
    VENUE_SPOT,
    epoch_seconds_to_utc,
    fetch_futures_orderbook,
    fetch_futures_tickers,
    fetch_spot_depth,
    fetch_spot_ohlc,
    fetch_spot_ticker,
    fetch_spot_trades,
    futures_instrument,
    receipt_time,
    spot_instrument,
)
from radar_v08.domain.integrity import (
    OC1_POLICY,
    CheckStatus,
    InstrumentId,
    InstrumentKind,
    TickerObservation,
    TimeBasis,
    TradingStatus,
    evaluate_ticker,
)
from radar_v08.kraken_futures import fetch_orderbook, fetch_tickers
from radar_v08.kraken_spot import fetch_depth

UTC = timezone.utc
CLOCK_NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def fixed_clock(moment: datetime = CLOCK_NOW):
    return lambda: moment


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    """Duck-typed stand-in for GuardedSession.get - no network involved."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return FakeResponse(self.payloads.pop(0))


# --- receipt clock -----------------------------------------------------------------------


class TestReceiptTime(unittest.TestCase):
    def test_aware_clock_is_returned_normalized_to_utc(self):
        plus_two = timezone(timedelta(hours=2))
        moment = datetime(2026, 9, 18, 14, 0, 0, tzinfo=plus_two)

        received = receipt_time(lambda: moment)

        self.assertEqual(received, CLOCK_NOW)
        self.assertEqual(received.tzinfo, UTC)

    def test_naive_clock_raises(self):
        naive = datetime(2026, 9, 18, 12, 0, 0)
        with self.assertRaises(ValueError):
            receipt_time(lambda: naive)


# --- spot ticker: Kraken never times it, always RECEIPT_ONLY --------------------------


class TestSpotTicker(unittest.TestCase):
    def test_ticker_has_no_source_time(self):
        payload = {"error": [], "result": {"XXBTZUSD": {"a": ["50100.0"], "c": ["50000.0"]}}}
        session = FakeSession([payload])

        fetched = fetch_spot_ticker(session, fixed_clock())

        self.assertEqual(fetched.raw, payload["result"])
        self.assertEqual(fetched.timing.received_at, CLOCK_NOW)
        self.assertIsNone(fetched.timing.source_time)

    def test_spot_instrument_from_wsname(self):
        meta = {"wsname": "XBT/USD", "altname": "XBTUSD"}

        instrument = spot_instrument("XXBTZUSD", meta)

        self.assertEqual(
            instrument, InstrumentId(VENUE_SPOT, "XBT/USD", InstrumentKind.SPOT, "BTC", "USD", "BTC")
        )

    def test_spot_instrument_none_when_display_cannot_be_split(self):
        meta = {"wsname": "NOTAPAIR"}

        self.assertIsNone(spot_instrument("WEIRD", meta))


# --- spot OHLC: each bar already carries Kraken's own open time -----------------------


def _ohlc_payload(rows, last):
    return {"error": [], "result": {"XBTUSD": rows, "last": last}}


def _bar_row(epoch, o=100.0, h=101.0, low=99.0, c=100.5, vwap=100.2, vol=10.0, count=5):
    return [epoch, str(o), str(h), str(low), str(c), str(vwap), str(vol), count]


class TestSpotOhlc(unittest.TestCase):
    def test_source_time_is_the_last_bars_open_time(self):
        payload = _ohlc_payload([_bar_row(1700000000), _bar_row(1700000300)], last=1700000300)
        session = FakeSession([payload])

        fetched = fetch_spot_ohlc(session, "XBTUSD", fixed_clock())

        self.assertEqual(len(fetched.bars), 2)
        self.assertEqual(fetched.timing.received_at, CLOCK_NOW)
        self.assertEqual(fetched.timing.source_time, datetime.fromtimestamp(1700000300, tz=UTC))

    def test_no_bars_means_no_source_time(self):
        payload = _ohlc_payload([], last=None)
        session = FakeSession([payload])

        fetched = fetch_spot_ohlc(session, "XBTUSD", fixed_clock())

        self.assertEqual(fetched.bars, ())
        self.assertIsNone(fetched.timing.source_time)


# --- spot depth: per-level timestamp, seconds not milliseconds ------------------------


def _depth_payload(bids, asks):
    return {"error": [], "result": {"XBTUSD": {"bids": bids, "asks": asks}}}


class TestSpotDepth(unittest.TestCase):
    def test_per_level_timestamp_is_epoch_seconds_not_milliseconds(self):
        # 1700000000 s == 2023-11-14T22:13:20Z. If this were ever misread as
        # milliseconds the parsed year would be 1970, not 2023.
        payload = _depth_payload(
            bids=[["50000.0", "1.0", 1700000000]],
            asks=[["50010.0", "2.0", 1700000010]],
        )
        session = FakeSession([payload])

        fetched = fetch_spot_depth(session, "XBTUSD", fixed_clock())

        bid_price, bid_volume, bid_time = fetched.bids[0]
        self.assertEqual((bid_price, bid_volume), (50000.0, 1.0))
        self.assertEqual(bid_time, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC))
        # Fetch-level source_time is the freshest level: the ask at +10s.
        self.assertEqual(fetched.timing.source_time, datetime(2023, 11, 14, 22, 13, 30, tzinfo=UTC))
        self.assertEqual(fetched.timing.received_at, CLOCK_NOW)

    def test_level_without_a_timestamp_is_receipt_only(self):
        payload = _depth_payload(bids=[["50000.0", "1.0"]], asks=[["50010.0", "2.0"]])
        session = FakeSession([payload])

        fetched = fetch_spot_depth(session, "XBTUSD", fixed_clock())

        self.assertIsNone(fetched.bids[0][2])
        self.assertIsNone(fetched.timing.source_time)

    def test_fetch_depth_compatibility_unchanged_with_or_without_timestamps(self):
        """Compatibility: fetch_depth (existing consumers) is unaffected
        whether or not the fixture carries a third (timestamp) element.
        """
        with_ts = _depth_payload(bids=[["50000.0", "1.0", 1700000000]], asks=[["50010.0", "2.0", 1700000010]])
        without_ts = _depth_payload(bids=[["50000.0", "1.0"]], asks=[["50010.0", "2.0"]])

        bids1, asks1 = fetch_depth(FakeSession([with_ts]), "XBTUSD")
        bids2, asks2 = fetch_depth(FakeSession([without_ts]), "XBTUSD")

        self.assertEqual(bids1, [(50000.0, 1.0)])
        self.assertEqual(asks1, [(50010.0, 2.0)])
        self.assertEqual((bids1, asks1), (bids2, asks2))


# --- spot trades: never zero, never fabricated -----------------------------------------


def _trades_payload(rows, last="1700000300000000000"):
    return {"error": [], "result": {"XBTUSD": rows, "last": last}}


class TestSpotTrades(unittest.TestCase):
    def test_source_time_is_the_latest_trade(self):
        rows = [
            ["50000.0", "0.1", 1700000000.5, "b", "m", ""],
            ["50010.0", "0.2", 1700000030.25, "s", "l", ""],
        ]
        session = FakeSession([_trades_payload(rows)])

        fetched = fetch_spot_trades(session, "XBTUSD", fixed_clock())

        self.assertEqual(len(fetched.trades), 2)
        self.assertEqual(fetched.timing.source_time, datetime.fromtimestamp(1700000030.25, tz=UTC))

    def test_no_trades_means_no_source_time_not_zero(self):
        session = FakeSession([_trades_payload([])])

        fetched = fetch_spot_trades(session, "XBTUSD", fixed_clock())

        self.assertEqual(fetched.trades, ())
        self.assertIsNone(fetched.timing.source_time)


# --- futures tickers/order book: serverTime, one bad field stays isolated -------------


def _futures_tickers_payload(rows, server_time="2026-09-18T12:00:05.000Z"):
    payload = {"result": "success", "tickers": rows}
    if server_time is not None:
        payload["serverTime"] = server_time
    return payload


class TestFuturesTickers(unittest.TestCase):
    def test_source_time_from_server_time(self):
        rows = [{"symbol": "PF_XBTUSD", "pair": "XBT:USD", "last": "50000"}]
        session = FakeSession([_futures_tickers_payload(rows)])

        fetched = fetch_futures_tickers(session, fixed_clock())

        self.assertEqual(fetched.rows, tuple(rows))
        self.assertEqual(fetched.timing.source_time, datetime(2026, 9, 18, 12, 0, 5, tzinfo=UTC))
        self.assertEqual(fetched.timing.received_at, CLOCK_NOW)

    def test_missing_server_time_is_isolated_receipt_only(self):
        rows = [{"symbol": "PF_ETHUSD", "pair": "ETH:USD", "last": "3000"}]
        session = FakeSession([_futures_tickers_payload(rows, server_time=None)])

        fetched = fetch_futures_tickers(session, fixed_clock())

        self.assertEqual(fetched.rows, tuple(rows))
        self.assertIsNone(fetched.timing.source_time)

    def test_malformed_server_time_is_isolated_receipt_only_rows_unaffected(self):
        rows = [{"symbol": "PF_ETHUSD", "pair": "ETH:USD", "last": "3000"}]
        session = FakeSession([_futures_tickers_payload(rows, server_time="not-a-timestamp")])

        fetched = fetch_futures_tickers(session, fixed_clock())

        self.assertEqual(fetched.rows, tuple(rows))
        self.assertIsNone(fetched.timing.source_time)

    def test_compatibility_fetch_tickers_unchanged(self):
        rows = [{"symbol": "PF_XBTUSD"}]
        session = FakeSession([_futures_tickers_payload(rows)])

        result = fetch_tickers(session)

        self.assertEqual(result, rows)

    def test_futures_instrument_mapping(self):
        instrument = futures_instrument("PF_XBTUSD", "BTC")

        self.assertEqual(
            instrument, InstrumentId(VENUE_FUTURES, "PF_XBTUSD", InstrumentKind.FUTURES, "BTC", FUTURES_QUOTE, "BTC")
        )


def _futures_orderbook_payload(bids, asks, server_time="2026-09-18T12:00:01.000Z"):
    payload = {"result": "success", "orderBook": {"bids": bids, "asks": asks}}
    if server_time is not None:
        payload["serverTime"] = server_time
    return payload


class TestFuturesOrderbook(unittest.TestCase):
    def test_source_time_from_server_time(self):
        session = FakeSession([_futures_orderbook_payload([[50000.0, 1.0]], [[50010.0, 2.0]])])

        fetched = fetch_futures_orderbook(session, "PF_XBTUSD", fixed_clock())

        self.assertEqual(fetched.bids, ((50000.0, 1.0),))
        self.assertEqual(fetched.timing.source_time, datetime(2026, 9, 18, 12, 0, 1, tzinfo=UTC))

    def test_missing_server_time_is_isolated_receipt_only(self):
        session = FakeSession(
            [_futures_orderbook_payload([[50000.0, 1.0]], [[50010.0, 2.0]], server_time=None)]
        )

        fetched = fetch_futures_orderbook(session, "PF_XBTUSD", fixed_clock())

        self.assertEqual(fetched.bids, ((50000.0, 1.0),))
        self.assertIsNone(fetched.timing.source_time)

    def test_compatibility_fetch_orderbook_unchanged(self):
        session = FakeSession([_futures_orderbook_payload([[50000.0, 1.0]], [[50010.0, 2.0]])])

        bids, asks = fetch_orderbook(session, "PF_XBTUSD")

        self.assertEqual(bids, [(50000.0, 1.0)])
        self.assertEqual(asks, [(50010.0, 2.0)])


# --- everything RECEIPT_ONLY-absent actually evaluates that way in domain -------------


# --- regressions: malformed / out-of-range times stay isolated -----------


class TestEpochSecondsToUtc(unittest.TestCase):
    def test_plain_seconds(self):
        self.assertEqual(epoch_seconds_to_utc(1700000000), datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC))

    def test_milliseconds_sized_value_is_none_not_rescaled(self):
        self.assertIsNone(epoch_seconds_to_utc(1700000000000.0))

    def test_non_finite_zero_negative_and_upper_bound_are_none(self):
        for value in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0, MAX_EPOCH_SECONDS, None):
            with self.subTest(value=value):
                self.assertIsNone(epoch_seconds_to_utc(value))

    def test_just_below_upper_bound_is_2099(self):
        self.assertEqual(
            epoch_seconds_to_utc(MAX_EPOCH_SECONDS - 1), datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC)
        )


class TestSpotDepthMalformedLevelTime(unittest.TestCase):
    def test_fetch_depth_ignores_non_numeric_third_element_as_before(self):
        # Regression: '' / 'x' made fetch_depth (l3.py) raise ValueError;
        # earlier versions returned [(1.0, 1.0)].
        for bad in ("", "x", None, "nan", [1]):
            with self.subTest(third=bad):
                bids, asks = fetch_depth(
                    FakeSession([_depth_payload([["1", "1", bad]], [["2", "3", bad]])]), "XBTUSD"
                )
                self.assertEqual(bids, [(1.0, 1.0)])
                self.assertEqual(asks, [(2.0, 3.0)])

    def test_fetch_depth_ignores_milliseconds_third_element_as_before(self):
        bids, asks = fetch_depth(
            FakeSession([_depth_payload([["1", "1", 1700000000000]], [["2", "3", 1700000000000]])]), "XBTUSD"
        )
        self.assertEqual((bids, asks), ([(1.0, 1.0)], [(2.0, 3.0)]))

    def test_non_numeric_level_time_is_none_rest_of_book_intact(self):
        payload = _depth_payload(
            bids=[["50000.0", "1.0", ""], ["49990.0", "2.0", 1700000000]],
            asks=[["50010.0", "3.0", "x"]],
        )

        fetched = fetch_spot_depth(FakeSession([payload]), "XBTUSD", fixed_clock())

        self.assertEqual(
            fetched.bids,
            ((50000.0, 1.0, None), (49990.0, 2.0, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC))),
        )
        self.assertEqual(fetched.asks, ((50010.0, 3.0, None),))
        self.assertEqual(fetched.timing.source_time, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC))

    def test_milliseconds_sized_level_time_is_none_rest_of_book_intact(self):
        # Regression: 1700000000000 raised OSError and
        # took down the whole depth fetch.
        payload = _depth_payload(
            bids=[["50000.0", "1.0", 1700000000000]],
            asks=[["50010.0", "2.0", 1700000010]],
        )

        fetched = fetch_spot_depth(FakeSession([payload]), "XBTUSD", fixed_clock())

        self.assertEqual(fetched.bids, ((50000.0, 1.0, None),))
        self.assertEqual(fetched.asks, ((50010.0, 2.0, datetime(2023, 11, 14, 22, 13, 30, tzinfo=UTC)),))
        self.assertEqual(fetched.timing.source_time, datetime(2023, 11, 14, 22, 13, 30, tzinfo=UTC))

    def test_non_finite_level_times_are_none_and_fetch_is_receipt_only(self):
        payload = _depth_payload(
            bids=[["50000.0", "1.0", float("nan")], ["49990.0", "1.0", "inf"]],
            asks=[["50010.0", "2.0", float("-inf")]],
        )

        fetched = fetch_spot_depth(FakeSession([payload]), "XBTUSD", fixed_clock())

        self.assertEqual([level[2] for level in (*fetched.bids, *fetched.asks)], [None, None, None])
        self.assertEqual([level[:2] for level in fetched.bids], [(50000.0, 1.0), (49990.0, 1.0)])
        self.assertIsNone(fetched.timing.source_time)
        self.assertEqual(fetched.timing.received_at, CLOCK_NOW)


class TestSpotTradesOutOfRangeTime(unittest.TestCase):
    def test_milliseconds_and_non_finite_trade_times_are_skipped(self):
        rows = [
            ["50000.0", "0.1", 1700000000000, "b", "m", ""],
            ["50010.0", "0.2", "nan", "s", "l", ""],
            ["50020.0", "0.3", 1700000030, "b", "l", ""],
        ]

        fetched = fetch_spot_trades(FakeSession([_trades_payload(rows)]), "XBTUSD", fixed_clock())

        self.assertEqual(len(fetched.trades), 3)  # payload passed through unchanged
        self.assertEqual(fetched.timing.source_time, datetime(2023, 11, 14, 22, 13, 50, tzinfo=UTC))

    def test_all_trade_times_invalid_means_no_source_time(self):
        rows = [["50000.0", "0.1", 1700000000000, "b", "m", ""], ["50010.0", "0.2", "inf", "s", "l", ""]]

        fetched = fetch_spot_trades(FakeSession([_trades_payload(rows)]), "XBTUSD", fixed_clock())

        self.assertEqual(len(fetched.trades), 2)
        self.assertIsNone(fetched.timing.source_time)


class TestReceiptReadAfterResponse(unittest.TestCase):
    """The injected clock is read only after the session returned the
    response, so `received_at` is the receipt moment, not the request moment.
    """

    def _clock_after(self, session):
        seen = []

        def clock():
            seen.append(len(session.calls))
            return CLOCK_NOW

        return clock, seen

    def test_every_wrapper_reads_the_clock_once_after_get(self):
        cases = [
            ("ticker", {"error": [], "result": {"XXBTZUSD": {"c": ["1", "1"]}}},
             lambda s, c: fetch_spot_ticker(s, c)),
            ("ohlc", _ohlc_payload([_bar_row(1700000000)], 1700000000),
             lambda s, c: fetch_spot_ohlc(s, "XBTUSD", c)),
            ("depth", _depth_payload([["1", "1", 1700000000]], [["2", "1", 1700000000]]),
             lambda s, c: fetch_spot_depth(s, "XBTUSD", c)),
            ("trades", _trades_payload([["1", "1", 1700000000, "b", "m", ""]]),
             lambda s, c: fetch_spot_trades(s, "XBTUSD", c)),
            ("futures tickers", _futures_tickers_payload([]),
             lambda s, c: fetch_futures_tickers(s, c)),
            ("futures orderbook", _futures_orderbook_payload([[1, 1]], [[2, 1]]),
             lambda s, c: fetch_futures_orderbook(s, "PF_XBTUSD", c)),
        ]
        for name, payload, call in cases:
            with self.subTest(fetch=name):
                session = FakeSession([payload])
                clock, seen = self._clock_after(session)

                fetched = call(session, clock)

                self.assertEqual(seen, [1])
                self.assertEqual(fetched.timing.received_at, CLOCK_NOW)


class TestTimeBasisIntegration(unittest.TestCase):
    """Cross-check against radar_v08.domain.integrity: a SourceTiming with no
    source_time is exactly the RECEIPT_ONLY case the domain layer expects.
    """

    def test_ticker_timing_is_receipt_only_once_evaluated(self):
        payload = {
            "error": [],
            "result": {"XXBTZUSD": {"a": ["50100.0"], "b": ["50090.0"], "c": ["50095.0"]}},
        }
        session = FakeSession([payload])
        fetched = fetch_spot_ticker(session, fixed_clock())
        instrument = spot_instrument("XXBTZUSD", {"wsname": "XBT/USD"})
        self.assertIsNotNone(instrument)
        row = fetched.raw["XXBTZUSD"]
        observation = TickerObservation(
            instrument=instrument,
            bid=float(row["b"][0]),
            ask=float(row["a"][0]),
            last=float(row["c"][0]),
            price_unit=instrument.quote,
            timing=fetched.timing,
            status=TradingStatus.ONLINE,
        )

        result = evaluate_ticker(observation, instrument, CLOCK_NOW + timedelta(seconds=1), OC1_POLICY)

        self.assertEqual(result.time_basis, TimeBasis.RECEIPT_ONLY)
        self.assertEqual(result.status, CheckStatus.PASS)


if __name__ == "__main__":
    unittest.main()
