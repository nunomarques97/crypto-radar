import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.anomaly import Features as L1Features
from radar_v08.http_client import ApiError
from radar_v08.kraken_spot import fetch_ohlc
from radar_v08.l2 import L2CandidateInput, run_l2
from radar_v08.store import SnapshotStore

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


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


class FailingSession:
    def get(self, url, params=None):
        raise ApiError("simulated network failure")


def _ohlc_payload(rows, last):
    return {"error": [], "result": {"XBTUSD": rows, "last": last}}


def _bar_row(epoch, o, h, low, c, vwap=None, vol=10.0, count=5):
    return [epoch, str(o), str(h), str(low), str(c), str(vwap if vwap is not None else c), str(vol), count]


class TestOhlcIncrementalSince(unittest.TestCase):
    def test_fetch_ohlc_passes_since_through_and_parses_bars(self):
        payload = _ohlc_payload(
            [_bar_row(1700000000, 100, 101, 99, 100.5), _bar_row(1700000300, 100.5, 102, 100, 101.5)],
            last=1700000300,
        )
        session = FakeSession([payload])

        bars, last = fetch_ohlc(session, "XBTUSD", interval=5, since=1699999000)

        self.assertEqual(len(bars), 2)
        self.assertEqual(last, 1700000300)
        self.assertEqual(bars[1].close, 101.5)
        _, params = session.calls[0]
        self.assertEqual(params["since"], 1699999000)

    def test_fetch_ohlc_without_since_omits_the_param(self):
        payload = _ohlc_payload([_bar_row(1700000000, 100, 101, 99, 100.5)], last=1700000000)
        session = FakeSession([payload])

        fetch_ohlc(session, "XBTUSD", interval=5, since=None)

        _, params = session.calls[0]
        self.assertNotIn("since", params)

    def test_cursor_persists_and_next_call_uses_it(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(path)
        store = SnapshotStore(path)
        try:
            self.assertIsNone(store.get_ohlc_cursor("XBTUSD", 5))
            store.set_ohlc_cursor("XBTUSD", 5, 1700000300, NOW.isoformat())
            self.assertEqual(store.get_ohlc_cursor("XBTUSD", 5), 1700000300)
        finally:
            store.close()
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)

    def test_incremental_insert_does_not_duplicate_refetched_bar(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(path)
        store = SnapshotStore(path)
        try:
            payload1 = _ohlc_payload([_bar_row(1700000000, 100, 101, 99, 100.5)], last=1700000000)
            bars1, last1 = fetch_ohlc(FakeSession([payload1]), "XBTUSD", interval=5, since=None)
            store.insert_ohlc_bars_batch("XBTUSD", 5, bars1)

            # Same (still-forming) bar re-fetched with an updated close.
            payload2 = _ohlc_payload([_bar_row(1700000000, 100, 101, 99, 100.9)], last=1700000000)
            bars2, last2 = fetch_ohlc(FakeSession([payload2]), "XBTUSD", interval=5, since=1699999999)
            store.insert_ohlc_bars_batch("XBTUSD", 5, bars2)

            window = store.get_ohlc_window("XBTUSD", 5, 100)
            self.assertEqual(len(window), 1)  # replaced, not duplicated
            self.assertEqual(window[0]["close"], 100.9)
        finally:
            store.close()
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)


def _candidate(asset="BTC", pair="XXBTZUSD"):
    return L2CandidateInput(
        asset=asset, pair=pair, current_last=100.0, vwap_today=100.0, spread_bps=5.0,
        bid=99.9, ask=100.1, bid_size=10.0, ask_size=10.0, market_status="online",
        futures_available=False, futures_spread_bps=None, futures_volume_24h_usd=None,
        l1_features=L1Features(return_5m=0.1, return_15m=0.2, return_1h=0.3, return_4h=0.4),
        anomaly_score=42.0,
    )


class TestMissingOhlc(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    def test_ohlc_failure_flags_candidate_but_does_not_drop_it(self):
        results, requests_made, failures = run_l2(self.store, FailingSession(), [_candidate()], NOW, "run-1")

        self.assertEqual(requests_made, 1)
        self.assertEqual(failures, 1)
        self.assertIn("BTC", results)
        result = results["BTC"]
        self.assertTrue(result.ohlc_missing)
        self.assertIn("OHLC_MISSING", result.flags)
        # No history at all yet -> L2 must degrade to warmup, never fabricate.
        self.assertTrue(result.l2_features.l2_warmup)

    def test_successful_fetch_updates_cursor_and_is_not_flagged_missing(self):
        rows = [_bar_row(1700000000 + i * 300, 100 + i * 0.01, 100.5 + i * 0.01, 99.5 + i * 0.01, 100 + i * 0.01) for i in range(30)]
        payload = _ohlc_payload(rows, last=1700000000 + 29 * 300)
        session = FakeSession([payload])

        results, requests_made, failures = run_l2(self.store, session, [_candidate()], NOW, "run-1")

        self.assertEqual(failures, 0)
        result = results["BTC"]
        self.assertFalse(result.ohlc_missing)
        self.assertNotIn("OHLC_MISSING", result.flags)
        self.assertEqual(self.store.get_ohlc_cursor("XXBTZUSD", config.OHLC_INTERVAL_MINUTES), 1700000000 + 29 * 300)


if __name__ == "__main__":
    unittest.main()
