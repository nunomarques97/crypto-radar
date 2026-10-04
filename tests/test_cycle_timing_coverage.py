"""End-to-end cycle stage timings and per-cycle HTTP counters.

`latency_ms` partitions the whole cycle into the top-level stages of
`heartbeat.TOP_LEVEL_STAGE_KEYS`, so `unaccounted_ms` (total_ms minus the
stages present) is zero; nested L2 phases use the `l2.` prefix and stay out
of that sum. `data_quality.http` holds this cycle's Kraken request counters
per endpoint (delta from a snapshot at cycle start).

These tests reuse the fake-Kraken heartbeat harness of `test_integrity_wiring`
(no socket, deterministic clock, disposable SQLite, run records captured in
memory instead of runs.jsonl). The stage timer is a fake that only advances
inside simulated waits, so every wait is attributed exactly; the HTTP retry
sleep is injected, never real.
"""

import json
import os
import sqlite3
import sys
import threading
import unittest
from unittest import mock
from urllib.parse import urlsplit

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TESTS_DIR))
sys.path.insert(0, TESTS_DIR)

import test_integrity_wiring as wiring  # noqa: E402  (fake Kraken heartbeat harness)
import test_run_record_observability as observability  # noqa: E402  (slow-cycle helpers)

from radar_v08 import (  # noqa: E402
    config,
    heartbeat,
    http_client,
    paper_game,
    pilot_shadow,
)
from radar_v08.http_client import GuardedSession  # noqa: E402

TOP_LEVEL = set(heartbeat.TOP_LEVEL_STAGE_KEYS)
NESTED = {"l2.ohlc_fetch_ms", "l2.compute_ms"}
COMMON_STAGES = {
    "setup_ms", "asset_pairs_ms", "futures_ticker_ms", "spot_ticker_ms", "asset_pairs_refresh_ms",
    "ticker_gate_ms", "normalize_ms", "snapshot_write_ms", "prune_ms", "l1_ms", "shortlist_ms", "l2_ms",
    "forward_labels_ms", "outcome_labels_ms", "qwen_record_ms", "outcome_register_ms", "paper_ms", "pilot_ms",
    "alerts_ms", "output_ms", "between_stages_ms",
}
FULL_ONLY = {"finalists_ms", "l3_ms", "seal_ms", "qwen_ms", "router_ms"}
PRE_EXISTING = {
    "asset_pairs_ms", "futures_ticker_ms", "spot_ticker_ms", "l2_ms", "forward_labels_ms",
    "outcome_labels_ms", "l3_ms", "outcome_register_ms", "total_ms",
}
OHLC_ENDPOINT = "api.kraken.com/0/public/OHLC"

# Simulated waits (seconds) and the stage each one belongs to.
WAITS = {
    "snapshot_write_ms": 0.25,
    "prune_ms": 1.5,
    "l1_ms": 0.125,  # per L1 asset
    "ohlc": 2.0,  # per OHLC request, inside the transport
    "forward_labels_ms": 0.75,
    "seal_ms": 0.0625,  # per sealed finalist
    "paper_ms": 0.375,  # settling due paper plays
    "pilot_ms": 0.1875,  # settling pilot shadow positions
}


class FakeTimer:
    """perf_counter stand-in that only advances inside wait() (thread-safe)."""

    def __init__(self, start=1000.0):
        self._now = start
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self._now

    def wait(self, seconds):
        with self._lock:
            self._now += seconds


class SlowOhlcKraken(wiring.FakeKraken):
    """Fake Kraken whose OHLC requests take `ohlc_wait` on the fake timer, and
    whose OHLC for `throttled_pair` answers 429 once before succeeding."""

    def __init__(self, timer, assets=("BTC", "ETH"), ohlc_wait=0.0, throttled_pair=None):
        super().__init__(assets=assets)
        self._timer = timer
        self._ohlc_wait = ohlc_wait
        self._throttled_pair = throttled_pair

    def request(self, method, url, params=None, timeout=None, allow_redirects=True):
        if urlsplit(url).path == "/0/public/OHLC":
            self._timer.wait(self._ohlc_wait)
            if self._throttled_pair is not None and (params or {}).get("pair") == self._throttled_pair:
                self._throttled_pair = None
                with self._lock:
                    self.calls.append({"method": method, "url": url, "params": dict(params), "allow_redirects": False})
                return wiring.FakeResponse({}, status_code=429)
        return super().request(method, url, params=params, timeout=timeout, allow_redirects=allow_redirects)


class NoCountersSession:
    """A session without request_stats(): only get/close are delegated."""

    def __init__(self, inner):
        self._inner = inner

    def get(self, url, params=None):
        return self._inner.get(url, params)

    def close(self):
        self._inner.close()


def endpoint_counts(kraken):
    counts = {}
    for call in kraken.calls:
        parts = urlsplit(call["url"])
        key = f"{parts.netloc}{parts.path}"
        counts[key] = counts.get(key, 0) + 1
    return counts


class CycleTimingBase(wiring.IntegrityWiringBase):
    def setUp(self):
        super().setUp()
        self.timer = FakeTimer()
        # No real sleep may happen anywhere in these cycles.
        patcher = mock.patch.object(http_client.time, "sleep", side_effect=AssertionError("real sleep"))
        patcher.start()
        self.addCleanup(patcher.stop)
        # The paper game step (its own "paper_ms" stage) runs unless switched off.
        paper = mock.patch.object(config, "RADAR_PAPER_ENABLED", True)
        paper.start()
        self.addCleanup(paper.stop)
        # So does the pilot shadow step (its own "pilot_ms" stage).
        pilot = mock.patch.object(config, "RADAR_PILOT_ENABLED", True)
        pilot.start()
        self.addCleanup(pilot.stop)

    def session(self, kraken, max_retries=0, backoff_base=0.0):
        return GuardedSession(
            1.0, max_retries, backoff_base, http_session=kraken, timer=self.timer, sleep=self.timer.wait
        )

    def cycle(self, session, full, clock=None):
        return heartbeat.run_heartbeat(
            mode="TEST", store=self.store, full=full, session=session,
            clock=clock or wiring.StepClock(), timer=self.timer,
        )

    def record(self, index=-1):
        return self.run_records[index]

    def assert_partition(self, latency):
        top = [key for key in latency if key in TOP_LEVEL]
        self.assertAlmostEqual(latency["unaccounted_ms"], 0.0, delta=1e-6)
        self.assertGreaterEqual(latency["unaccounted_ms"], 0.0)
        self.assertAlmostEqual(sum(latency[key] for key in top), latency["total_ms"], delta=1e-6)
        for key, value in latency.items():
            self.assertIsInstance(value, float, key)
            self.assertGreaterEqual(value, 0.0, key)
            self.assertTrue(key in TOP_LEVEL or key in NESTED or key in ("total_ms", "unaccounted_ms"), key)


class TestStageKeys(CycleTimingBase):
    def keys(self, full, correction):
        kraken = SlowOhlcKraken(self.timer)
        with mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", correction):
            self.cycle(self.session(kraken), full=full)
        latency = self.record()["latency_ms"]
        self.assert_partition(latency)
        return set(latency)

    def test_full_cycle_has_every_stage(self):
        for correction in (True, False):
            with self.subTest(correction=correction):
                self.run_records.clear()
                self.assertEqual(
                    self.keys(full=True, correction=correction),
                    COMMON_STAGES | FULL_ONLY | NESTED | {"total_ms", "unaccounted_ms"},
                )

    def test_heartbeat_cycle_has_no_full_cycle_stage(self):
        for correction in (True, False):
            with self.subTest(correction=correction):
                self.run_records.clear()
                self.assertEqual(
                    self.keys(full=False, correction=correction),
                    COMMON_STAGES | NESTED | {"total_ms", "unaccounted_ms"},
                )

    def test_tracking_off_drops_only_the_outcome_stages(self):
        with mock.patch.object(config, "RADAR_OUTCOME_TRACKING_ENABLED", False):
            keys = self.keys(full=True, correction=True)
        expected = (COMMON_STAGES - {"outcome_labels_ms", "outcome_register_ms"}) | FULL_ONLY | NESTED
        self.assertEqual(keys, expected | {"total_ms", "unaccounted_ms"})

    def test_paper_off_drops_only_the_paper_stage(self):
        for full in (True, False):
            with self.subTest(full=full):
                self.run_records.clear()
                with mock.patch.object(config, "RADAR_PAPER_ENABLED", False):
                    keys = self.keys(full=full, correction=True)
                expected = (COMMON_STAGES - {"paper_ms"}) | (FULL_ONLY if full else set()) | NESTED
                self.assertEqual(keys, expected | {"total_ms", "unaccounted_ms"})

    def test_pilot_off_drops_only_the_pilot_stage(self):
        for full in (True, False):
            with self.subTest(full=full):
                self.run_records.clear()
                with mock.patch.object(config, "RADAR_PILOT_ENABLED", False):
                    keys = self.keys(full=full, correction=True)
                expected = (COMMON_STAGES - {"pilot_ms"}) | (FULL_ONLY if full else set()) | NESTED
                self.assertEqual(keys, expected | {"total_ms", "unaccounted_ms"})

    def test_every_pre_existing_key_stays(self):
        self.keys(full=True, correction=True)
        self.assertLessEqual(PRE_EXISTING, set(self.record()["latency_ms"]))

    def test_seal_ticker_refresh_key_only_when_the_refresh_ran(self):
        kraken = observability.slow_cycle_kraken()
        clock = wiring.StepClock()
        real_run_l2 = heartbeat.run_l2

        def slow_run_l2(*args, **kwargs):
            result = real_run_l2(*args, **kwargs)
            with clock._lock:
                clock._now += observability.SLOW_STAGE
            return result

        with mock.patch.object(heartbeat, "run_l2", slow_run_l2):
            self.cycle(self.session(kraken), full=True, clock=clock)
        latency = self.record()["latency_ms"]
        self.assertIn("seal_ticker_refresh_ms", latency)
        self.assertEqual(self.record()["data_quality"]["integrity"]["seal_ticker_refreshed"], 2)
        self.assert_partition(latency)

        self.cycle(self.session(SlowOhlcKraken(self.timer)), full=True)
        self.assertNotIn("seal_ticker_refresh_ms", self.record()["latency_ms"])

    def test_default_timer_still_partitions_the_cycle(self):
        session = GuardedSession(1.0, 0, 0.0, http_session=wiring.FakeKraken(assets=("BTC", "ETH")))
        heartbeat.run_heartbeat(mode="TEST", store=self.store, full=True, session=session, clock=wiring.StepClock())
        latency = self.record()["latency_ms"]
        self.assertAlmostEqual(latency["unaccounted_ms"], 0.0, delta=1e-6)
        self.assertLessEqual(COMMON_STAGES | FULL_ONLY, set(latency))


class TestWaitsAreAttributedToTheirStage(CycleTimingBase):
    def run_with_waits(self, full):
        timer = self.timer
        store = self.store
        real_insert = store.insert_spot_snapshots_batch
        real_prune = store.prune
        real_anomaly = heartbeat.compute_anomaly
        real_label = heartbeat.label_forward_returns
        real_seal = heartbeat._seal_evidence
        real_settle = paper_game.settle_due
        real_pilot_close = pilot_shadow.close_positions

        def slow_insert(*args, **kwargs):
            timer.wait(WAITS["snapshot_write_ms"])
            return real_insert(*args, **kwargs)

        def slow_prune(*args, **kwargs):
            timer.wait(WAITS["prune_ms"])
            return real_prune(*args, **kwargs)

        def slow_anomaly(*args, **kwargs):
            timer.wait(WAITS["l1_ms"])
            return real_anomaly(*args, **kwargs)

        def slow_label(*args, **kwargs):
            timer.wait(WAITS["forward_labels_ms"])
            return real_label(*args, **kwargs)

        def slow_seal(*args, **kwargs):
            timer.wait(WAITS["seal_ms"])
            return real_seal(*args, **kwargs)

        def slow_settle(*args, **kwargs):
            timer.wait(WAITS["paper_ms"])
            return real_settle(*args, **kwargs)

        def slow_pilot_close(*args, **kwargs):
            timer.wait(WAITS["pilot_ms"])
            return real_pilot_close(*args, **kwargs)

        kraken = SlowOhlcKraken(timer, ohlc_wait=WAITS["ohlc"])
        with mock.patch.object(store, "insert_spot_snapshots_batch", slow_insert), \
                mock.patch.object(store, "prune", slow_prune), \
                mock.patch.object(heartbeat, "compute_anomaly", slow_anomaly), \
                mock.patch.object(heartbeat, "label_forward_returns", slow_label), \
                mock.patch.object(heartbeat, "_seal_evidence", slow_seal), \
                mock.patch.object(paper_game, "settle_due", slow_settle),                 mock.patch.object(pilot_shadow, "close_positions", slow_pilot_close):
            self.cycle(self.session(kraken), full=full)
        return self.record()["latency_ms"], kraken

    def assert_attributed(self, latency, full):
        ohlc_requests = self.record()["L2_ohlc_requests"]
        self.assertEqual(ohlc_requests, 2)
        expected = {
            "snapshot_write_ms": WAITS["snapshot_write_ms"] * 1000,
            "prune_ms": WAITS["prune_ms"] * 1000,
            "l1_ms": WAITS["l1_ms"] * 2 * 1000,
            "l2_ms": WAITS["ohlc"] * ohlc_requests * 1000,
            "l2.ohlc_fetch_ms": WAITS["ohlc"] * ohlc_requests * 1000,
            "forward_labels_ms": WAITS["forward_labels_ms"] * 1000,
            "paper_ms": WAITS["paper_ms"] * 1000,
            "pilot_ms": WAITS["pilot_ms"] * 1000,
        }
        if full:
            expected["seal_ms"] = WAITS["seal_ms"] * self.record()["funnel"]["L3_finalists"] * 1000
        for key, value in expected.items():
            self.assertAlmostEqual(latency[key], value, delta=1e-6, msg=key)
        others = set(latency) - set(expected) - {"total_ms", "unaccounted_ms"}
        for key in others:
            self.assertEqual(latency[key], 0.0, key)
        self.assertAlmostEqual(latency["total_ms"], sum(expected[k] for k in expected if k in TOP_LEVEL), delta=1e-6)
        self.assert_partition(latency)

    def test_full_cycle(self):
        latency, _kraken = self.run_with_waits(full=True)
        self.assertEqual(self.record()["funnel"]["L3_finalists"], 2)
        self.assert_attributed(latency, full=True)

    def test_heartbeat_cycle(self):
        latency, _kraken = self.run_with_waits(full=False)
        self.assertNotIn("seal_ms", latency)
        self.assert_attributed(latency, full=False)

    def test_nested_l2_phases_are_outside_the_sum(self):
        latency, _kraken = self.run_with_waits(full=False)
        top_sum = sum(value for key, value in latency.items() if key in TOP_LEVEL)
        self.assertAlmostEqual(top_sum, latency["total_ms"], delta=1e-6)
        self.assertGreater(latency["l2.ohlc_fetch_ms"], 0.0)
        # Were they counted, unaccounted_ms would be negative by that amount.
        self.assertAlmostEqual(latency["unaccounted_ms"], 0.0, delta=1e-6)


class TestPerCycleHttpCounters(CycleTimingBase):
    def test_counts_every_request_of_the_cycle_per_endpoint(self):
        kraken = SlowOhlcKraken(self.timer, ohlc_wait=WAITS["ohlc"])
        output = self.cycle(self.session(kraken), full=True)
        http = self.record()["data_quality"]["http"]
        counts = endpoint_counts(kraken)
        self.assertEqual(set(http), set(counts))
        for endpoint in (
            "api.kraken.com/0/public/AssetPairs",
            "api.kraken.com/0/public/Ticker",
            OHLC_ENDPOINT,
            "futures.kraken.com/derivatives/api/v3/tickers",
        ):
            self.assertIn(endpoint, http)
        for endpoint, stats in http.items():
            self.assertEqual(stats["requests"], counts[endpoint], endpoint)
            self.assertEqual(stats["attempts"], counts[endpoint], endpoint)
            self.assertEqual(stats["failures"], 0, endpoint)
            self.assertEqual(stats["backoff_ms"], 0.0, endpoint)
        self.assertEqual(http[OHLC_ENDPOINT]["requests"], 2)
        self.assertAlmostEqual(http[OHLC_ENDPOINT]["network_ms"], 2 * WAITS["ohlc"] * 1000, delta=1e-6)
        # The same section in the output and in radar_runs.data_quality_json.
        self.assertEqual(output["data_quality"]["http"], http)
        with sqlite3.connect(self.db_path) as conn:
            stored = json.loads(conn.execute("SELECT data_quality_json FROM radar_runs").fetchone()[0])
        self.assertEqual(stored["http"], http)

    def test_429_then_retry_counts_two_attempts_and_the_backoff(self):
        pair = wiring.ASSETS["BTC"][0]
        kraken = SlowOhlcKraken(self.timer, assets=("BTC",), throttled_pair=pair)
        self.cycle(self.session(kraken, max_retries=1, backoff_base=0.5), full=False)
        ohlc = self.record()["data_quality"]["http"][OHLC_ENDPOINT]
        self.assertEqual(ohlc["requests"], 1)
        self.assertEqual(ohlc["attempts"], 2)
        self.assertEqual(ohlc["failures"], 0)
        self.assertAlmostEqual(ohlc["backoff_ms"], 500.0, delta=1e-6)
        self.assertEqual(self.record()["L2_ohlc_failures"], 0)
        # The injected sleep advanced only the fake timer: the backoff sits inside L2.
        self.assertAlmostEqual(self.record()["latency_ms"]["l2_ms"], 500.0, delta=1e-6)
        self.assert_partition(self.record()["latency_ms"])

    def test_reused_session_counts_only_this_cycle(self):
        session = self.session(SlowOhlcKraken(self.timer))
        self.cycle(session, full=False)
        first = self.record()["data_quality"]["http"]
        self.cycle(session, full=False)
        second = self.record()["data_quality"]["http"]
        self.assertEqual(second[OHLC_ENDPOINT]["requests"], first[OHLC_ENDPOINT]["requests"])
        self.assertEqual(second[OHLC_ENDPOINT]["requests"], 2)
        self.assertEqual(session.request_stats()[OHLC_ENDPOINT]["requests"], 4)

    def test_absent_when_the_session_keeps_no_counters(self):
        session = NoCountersSession(self.session(SlowOhlcKraken(self.timer)))
        output = self.cycle(session, full=True)
        self.assertNotIn("http", self.record()["data_quality"])
        self.assertNotIn("http", output["data_quality"])
        self.assert_partition(self.record()["latency_ms"])

    def test_existing_data_quality_and_funnel_keys_are_unchanged(self):
        kraken = SlowOhlcKraken(self.timer)
        self.cycle(self.session(kraken), full=True)
        with_counters = self.record()
        self.cycle(NoCountersSession(self.session(SlowOhlcKraken(self.timer))), full=True)
        without_counters = self.record()
        self.assertEqual(set(with_counters["data_quality"]) - {"http"}, set(without_counters["data_quality"]))
        self.assertEqual(set(with_counters["funnel"]), set(without_counters["funnel"]))


if __name__ == "__main__":
    unittest.main()
