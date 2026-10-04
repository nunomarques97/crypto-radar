"""Per-endpoint request counters in GuardedSession (observability only).

No network: every test uses a duck-typed fake inner session, an injected fake
clock and a recording fake sleep, so nothing leaves the process and nothing
really waits.
"""

import os
import sys
import threading
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.http_client import (
    REQUEST_STAT_FIELDS,
    ApiError,
    GuardedSession,
    RedirectNotFollowed,
    request_stats_delta,
)
from radar_v08.security import RedirectRefused, SecurityViolation

SPOT = "https://api.kraken.com/0/public"
FUTURES = "https://futures.kraken.com/derivatives/api/v3"
OHLC_KEY = "api.kraken.com/0/public/OHLC"
TICKER_KEY = "api.kraken.com/0/public/Ticker"


class FakeClock:
    """perf_counter stand-in: only moves when a fake transport or sleep advances it."""

    def __init__(self):
        self.now = 100.0
        self._lock = threading.Lock()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        with self._lock:
            self.now += seconds


class RecordingSleep:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)
        self.clock.advance(seconds)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = dict(headers or {})
        self.history = []

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeTransport:
    """Stand-in for requests.Session: each call takes `latency` fake seconds."""

    def __init__(self, clock, results=(), latency=0.25):
        self.clock = clock
        self.results = list(results)
        self.latency = latency
        self.calls = []

    def request(self, method, url, params=None, timeout=None, allow_redirects=True):
        self.calls.append(
            {"method": method, "url": url, "params": params, "timeout": timeout, "allow_redirects": allow_redirects}
        )
        self.clock.advance(self.latency)
        if not self.results:
            raise AssertionError(f"unexpected extra request to {url}")
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def make_session(results=(), max_retries=3, backoff_base=0.5, latency=0.25):
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    transport = FakeTransport(clock, results, latency)
    session = GuardedSession(
        timeout=7,
        max_retries=max_retries,
        backoff_base=backoff_base,
        http_session=transport,
        timer=clock,
        sleep=sleep,
    )
    return session, transport, sleep


class TestSingleEndpointCounters(unittest.TestCase):
    def test_first_attempt_success(self):
        session, transport, sleep = make_session([FakeResponse(200, {"ok": 1})], latency=0.25)
        response = session.get(f"{SPOT}/OHLC", params={"pair": "XBTUSD", "interval": 5})
        self.assertEqual(response.json(), {"ok": 1})
        stats = session.request_stats()
        self.assertEqual(set(stats), {OHLC_KEY})
        self.assertEqual(stats[OHLC_KEY]["requests"], 1)
        self.assertEqual(stats[OHLC_KEY]["attempts"], 1)
        self.assertEqual(stats[OHLC_KEY]["failures"], 0)
        self.assertEqual(stats[OHLC_KEY]["backoff_ms"], 0)
        self.assertAlmostEqual(stats[OHLC_KEY]["network_ms"], 250.0, places=6)
        self.assertEqual(tuple(stats[OHLC_KEY]), REQUEST_STAT_FIELDS)
        self.assertEqual(sleep.calls, [])

    def test_429_then_200_counts_the_retry_and_its_backoff(self):
        session, transport, sleep = make_session(
            [FakeResponse(429), FakeResponse(200, {"ok": 2})], max_retries=2, backoff_base=0.5, latency=0.1
        )
        with self.assertLogs("radar_v08.http", level="WARNING") as logs:
            response = session.get(f"{SPOT}/Ticker", params={"pair": "XBTUSD"})
        self.assertEqual(response.json(), {"ok": 2})
        self.assertEqual(sleep.calls, [0.5])  # unchanged backoff_base * 2**attempt
        self.assertEqual(
            logs.output,
            [f"WARNING:radar_v08.http:GET {SPOT}/Ticker returned 429 (attempt 1), retrying with backoff"],
        )
        stats = session.request_stats()[TICKER_KEY]
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["attempts"], 2)
        self.assertEqual(stats["failures"], 0)
        self.assertAlmostEqual(stats["backoff_ms"], sum(sleep.calls) * 1000.0, places=6)
        self.assertAlmostEqual(stats["network_ms"], 200.0, places=6)
        self.assertEqual(len(transport.calls), 2)
        for call in transport.calls:
            self.assertEqual(call["timeout"], 7)
            self.assertIs(call["allow_redirects"], False)

    def test_request_exception_on_every_attempt(self):
        max_retries = 2
        session, transport, sleep = make_session(
            [requests.ConnectionError("boom")] * (max_retries + 1), max_retries=max_retries, backoff_base=0.25
        )
        with self.assertRaises(ApiError) as ctx:
            session.get(f"{FUTURES}/tickers")
        self.assertEqual(
            str(ctx.exception), f"GET {FUTURES}/tickers failed after {max_retries + 1} attempts: boom"
        )
        self.assertEqual(sleep.calls, [0.25, 0.5])
        stats = session.request_stats()["futures.kraken.com/derivatives/api/v3/tickers"]
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["attempts"], max_retries + 1)
        self.assertEqual(stats["failures"], 1)
        self.assertAlmostEqual(stats["backoff_ms"], 750.0, places=6)
        self.assertAlmostEqual(stats["network_ms"], 750.0, places=6)

    def test_other_4xx_counts_one_failure_without_retry(self):
        session, transport, sleep = make_session([FakeResponse(404)])
        with self.assertRaises(requests.HTTPError):
            session.get(f"{SPOT}/Trades", params={"pair": "XBTUSD"})
        stats = session.request_stats()["api.kraken.com/0/public/Trades"]
        self.assertEqual((stats["requests"], stats["attempts"], stats["failures"]), (1, 1, 1))
        self.assertEqual(sleep.calls, [])


class TestRedirectsStillRefused(unittest.TestCase):
    def test_refused_redirect_is_counted_and_not_retried(self):
        session, transport, sleep = make_session([FakeResponse(302, headers={"Location": "https://evil.test/x"})])
        with self.assertRaises(RedirectRefused):
            session.get(f"{SPOT}/Ticker")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleep.calls, [])
        stats = session.request_stats()[TICKER_KEY]
        self.assertEqual((stats["requests"], stats["attempts"], stats["failures"]), (1, 1, 1))
        self.assertEqual(stats["backoff_ms"], 0)

    def test_allowlisted_redirect_is_counted_and_not_followed(self):
        session, transport, sleep = make_session([FakeResponse(301, headers={"Location": f"{SPOT}/OHLC"})])
        with self.assertRaises(RedirectNotFollowed):
            session.get(f"{SPOT}/Ticker")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(session.request_stats()[TICKER_KEY]["failures"], 1)
        self.assertNotIn(OHLC_KEY, session.request_stats())


class TestEndpointKeys(unittest.TestCase):
    def test_endpoints_counted_separately_without_query_params(self):
        session, transport, sleep = make_session([FakeResponse(200)] * 3)
        session.get(f"{SPOT}/OHLC", params={"pair": "XBTUSD", "interval": 5})
        session.get(f"{SPOT}/OHLC", params={"pair": "ETHUSD", "interval": 5, "since": 123})
        session.get(f"{SPOT}/Ticker", params={"pair": "XBTUSD,ETHUSD"})
        stats = session.request_stats()
        self.assertEqual(set(stats), {OHLC_KEY, TICKER_KEY})
        self.assertEqual(stats[OHLC_KEY]["requests"], 2)
        self.assertEqual(stats[TICKER_KEY]["requests"], 1)
        for key in stats:
            self.assertNotIn("?", key)
            self.assertNotIn("XBTUSD", key)
            self.assertNotIn("https", key)

    def test_disallowed_url_raises_before_any_transport_call_and_is_not_counted(self):
        session, transport, sleep = make_session([FakeResponse(200)])
        for url in (
            "https://evil.test/0/public/Ticker",
            f"{SPOT}/Ticker?pair=XBTUSD",
            "https://api.kraken.com/0/private/Balance",
            "http://api.kraken.com/0/public/Ticker",
        ):
            with self.subTest(url=url):
                with self.assertRaises(SecurityViolation):
                    session.get(url)
        self.assertEqual(transport.calls, [])
        self.assertEqual(session.request_stats(), {})


class TestSnapshotAndDelta(unittest.TestCase):
    def test_snapshot_is_an_independent_copy(self):
        session, transport, sleep = make_session([FakeResponse(200)] * 2)
        session.get(f"{SPOT}/OHLC")
        snapshot = session.request_stats()
        snapshot[OHLC_KEY]["requests"] = 99
        snapshot["x"] = {}
        self.assertEqual(session.request_stats()[OHLC_KEY]["requests"], 1)
        self.assertNotIn("x", session.request_stats())
        before = session.request_stats()
        session.get(f"{SPOT}/OHLC")
        self.assertEqual(before[OHLC_KEY]["requests"], 1)

    def test_delta_between_snapshots_counts_only_the_window(self):
        session, transport, sleep = make_session([FakeResponse(200), FakeResponse(503), FakeResponse(200), FakeResponse(200)])
        session.get(f"{SPOT}/Ticker")
        before = session.request_stats()
        session.get(f"{SPOT}/OHLC")
        session.get(f"{SPOT}/OHLC")
        delta = request_stats_delta(before, session.request_stats())
        self.assertEqual(set(delta), {OHLC_KEY})
        self.assertEqual(delta[OHLC_KEY]["requests"], 2)
        self.assertEqual(delta[OHLC_KEY]["attempts"], 3)
        self.assertAlmostEqual(delta[OHLC_KEY]["backoff_ms"], 500.0, places=6)
        self.assertEqual(request_stats_delta(session.request_stats(), session.request_stats()), {})


class TestConcurrency(unittest.TestCase):
    def test_threads_sharing_one_session_count_every_request(self):
        threads_n, calls_n = 8, 5
        session, transport, sleep = make_session([FakeResponse(200)] * (threads_n * calls_n), latency=0.001)
        start = threading.Barrier(threads_n)
        errors = []

        def worker():
            try:
                start.wait()
                for _ in range(calls_n):
                    session.get(f"{SPOT}/OHLC", params={"pair": "XBTUSD"})
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        # Force frequent thread switches so unsynchronised updates would be lost.
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            workers = [threading.Thread(target=worker) for _ in range(threads_n)]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join()
        finally:
            sys.setswitchinterval(old_interval)
        self.assertEqual(errors, [])
        stats = session.request_stats()[OHLC_KEY]
        self.assertEqual(stats["requests"], threads_n * calls_n)
        self.assertEqual(stats["attempts"], threads_n * calls_n)
        self.assertEqual(stats["failures"], 0)


class TestCountersNeverChangeBehaviour(unittest.TestCase):
    def test_defaults_still_use_module_time_sleep(self):
        transport = FakeTransport(FakeClock(), [FakeResponse(429), FakeResponse(200, {"ok": 3})])
        session = GuardedSession(timeout=5, max_retries=1, backoff_base=0.1, http_session=transport)
        with mock.patch("radar_v08.http_client.time.sleep") as patched_sleep:
            self.assertEqual(session.get(f"{SPOT}/Depth").json(), {"ok": 3})
        self.assertEqual([c.args[0] for c in patched_sleep.call_args_list], [0.1])
        stats = session.request_stats()["api.kraken.com/0/public/Depth"]
        self.assertEqual((stats["requests"], stats["attempts"]), (1, 2))
        self.assertGreaterEqual(stats["network_ms"], 0.0)

    def test_broken_timer_does_not_leak_into_get(self):
        def broken_timer():
            raise RuntimeError("clock failure")

        transport = FakeTransport(FakeClock(), [FakeResponse(200, {"ok": 4})])
        session = GuardedSession(
            timeout=5, max_retries=0, backoff_base=0.1, http_session=transport, timer=broken_timer
        )
        self.assertEqual(session.get(f"{SPOT}/AssetPairs").json(), {"ok": 4})
        stats = session.request_stats()["api.kraken.com/0/public/AssetPairs"]
        self.assertEqual((stats["requests"], stats["attempts"], stats["network_ms"]), (1, 1, 0.0))


if __name__ == "__main__":
    unittest.main()
