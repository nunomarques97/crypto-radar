"""Run record observability.

`data_quality.integrity.seal_block_capabilities` counts blocked finalists
per blocking capability at the evidence seal (the breakdown that was only in
radar.log), and `funnel.L3_sealed` counts finalists that passed the seal.

`latency_ms` records per-stage timings (`l2_ms`, `forward_labels_ms`,
`outcome_labels_ms`, `l3_ms`, `outcome_register_ms`), each key present only
when its stage ran.

Both are additive: no decision changes. These tests reuse the fake-Kraken
heartbeat harness of `test_integrity_wiring` (no socket, deterministic clock,
disposable SQLite, run records captured in memory instead of runs.jsonl).
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import urlsplit

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TESTS_DIR))
sys.path.insert(0, TESTS_DIR)

import test_integrity_wiring as wiring  # noqa: E402  (fake Kraken heartbeat harness)

from radar_v08 import config, heartbeat, kraken_spot  # noqa: E402
from radar_v08.domain.integrity import OC1_POLICY  # noqa: E402

STAGE_KEYS = ("l2_ms", "forward_labels_ms", "outcome_labels_ms", "l3_ms", "outcome_register_ms")
# The ticker is read at cycle start; the seal runs after L2 and L3. A cycle
# slower than ticker_max_age makes the ticker stale by the time of the seal.
SLOW_STAGE = OC1_POLICY.ticker_max_age + timedelta(seconds=10)


TICKER_PATH = "/0/public/Ticker"
WIRING_LOGGER = "radar_v08.test_integrity_wiring"  # the logger the harness injects
FINALIST_PAIRS = ",".join(wiring.ASSETS[asset][0] for asset in ("BTC", "ETH"))


def slow_cycle_kraken():
    """BTC and ETH, with L3 book, trades and futures book stamped after the
    slow stage: only the ticker read at cycle start ages past ticker_max_age."""
    kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
    later = wiring.T0 + SLOW_STAGE
    for asset in ("BTC", "ETH"):
        pair, _ws, symbol, _fp, _price = wiring.ASSETS[asset]
        book = kraken.depth[pair]["result"][pair]
        for level in book["bids"] + book["asks"]:
            level[2] = wiring._epoch(later - timedelta(seconds=2))
        for index, trade in enumerate(kraken.trades[pair]["result"][pair]):
            trade[2] = wiring._epoch(later - timedelta(seconds=3 + index))
        kraken.orderbook[symbol]["serverTime"] = wiring._iso_z(later)
    return kraken


def run_slow_cycle(test, kraken):
    """One full cycle whose L2 stage takes SLOW_STAGE on the injected clock."""
    clock = wiring.StepClock()
    real_run_l2 = heartbeat.run_l2

    def slow_run_l2(*args, **kwargs):
        result = real_run_l2(*args, **kwargs)
        with clock._lock:
            clock._now += SLOW_STAGE
        return result

    with mock.patch.object(heartbeat, "run_l2", slow_run_l2):
        return test.run_cycle(kraken, clock=clock)


def ticker_calls(kraken):
    return [call for call in kraken.calls if urlsplit(call["url"]).path == TICKER_PATH]


class SealBreakdownTests(wiring.IntegrityWiringBase):
    def run_record(self):
        self.assertEqual(len(self.run_records), 1)
        return self.run_records[0]

    def test_all_finalists_sealed_records_empty_breakdown(self):
        output = self.run_cycle(wiring.FakeKraken(assets=("BTC", "ETH")))

        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["seal_blocked"], 0)
        self.assertEqual(integrity["seal_block_capabilities"], {})
        self.assertEqual(output["funnel"]["L3_finalists"], 2)
        self.assertEqual(output["funnel"]["L3_sealed"], 2)
        self.assertEqual(self.run_record()["funnel"]["L3_sealed"], 2)

    def test_one_blocked_finalist_is_counted_under_its_blocking_capability(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        kraken.depth["XXBTZUSD"]["result"]["XXBTZUSD"]["bids"][0][0] = "60010.0"  # crossed BTC book

        output = self.run_cycle(kraken)

        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["seal_blocked"], 1)
        self.assertEqual(integrity["seal_block_capabilities"], {"book": 1})
        self.assertEqual(output["funnel"]["L3_finalists"], 2)
        self.assertEqual(output["funnel"]["L3_sealed"], 1)
        self.assertEqual(self.qwen.assets, ["ETH"])  # decisions unchanged: only the sealed finalist
        record = self.run_record()
        self.assertEqual(record["data_quality"]["integrity"]["seal_block_capabilities"], {"book": 1})
        self.assertEqual(record["funnel"]["L3_sealed"], 1)
        stored = self.count("SELECT data_quality_json FROM radar_runs")
        self.assertIn('"seal_block_capabilities": {"book": 1}', stored)

    def test_slow_cycle_makes_the_cycle_start_ticker_stale_at_the_seal(self):
        """The 09-21 production pattern: every finalist blocked on spot_ticker only.
        With the seal ticker refresh switched off this is still the behaviour."""
        kraken = slow_cycle_kraken()
        with mock.patch.object(config, "RADAR_SEAL_TICKER_REFRESH_ENABLED", False):
            output = run_slow_cycle(self, kraken)

        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["seal_blocked"], 2)
        self.assertEqual(integrity["seal_block_capabilities"], {"spot_ticker": 2})
        self.assertEqual(integrity["seal_ticker_refreshed"], 0)
        self.assertEqual(output["funnel"]["L3_finalists"], 2)
        self.assertEqual(output["funnel"]["L3_sealed"], 0)
        self.assertEqual(self.qwen.calls, [])
        self.assertEqual(len(ticker_calls(kraken)), 1)  # only the cycle-start Ticker

    def test_heartbeat_cycle_records_zero_sealed_and_empty_breakdown(self):
        session = wiring.GuardedSession(1.0, 0, 0.0, http_session=wiring.FakeKraken())
        output = heartbeat.run_heartbeat(
            mode="TEST", store=self.store, full=False, session=session, clock=wiring.StepClock()
        )

        self.assertEqual(output["data_quality"]["integrity"]["seal_block_capabilities"], {})
        self.assertEqual(output["data_quality"]["integrity"]["seal_ticker_refreshed"], 0)
        self.assertEqual(output["funnel"]["L3_sealed"], 0)


class NonJsonResponse(wiring.FakeResponse):
    """A 200 whose body is not JSON (e.g. an HTML error page)."""

    def json(self):
        raise json.JSONDecodeError("Expecting value", "<html>", 0)


class SealTickerRefreshTests(wiring.IntegrityWiringBase):
    """A stale cycle-start ticker is read again, once, for the L3
    finalists only, just before the seal. Nothing else changes."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(config, "RADAR_SEAL_TICKER_REFRESH_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def refresh_call(self, kraken):
        calls = ticker_calls(kraken)
        self.assertEqual(len(calls), 2, "the cycle-start Ticker plus exactly one refresh")
        self.assertEqual(calls[0]["params"], {})  # the global cycle-start call is unchanged
        return calls[1]

    def seal_ticker_record(self, asset):
        event = next(e for e in self.events() if e["asset"] == asset)
        record = json.loads(event["context_json"])["integrity"]
        return next(r for r in record["results"] if r["capability"] == "spot_ticker")

    def test_slow_cycle_refetches_the_finalists_ticker_and_seals(self):
        kraken = slow_cycle_kraken()

        output = run_slow_cycle(self, kraken)

        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["seal_blocked"], 0)
        self.assertEqual(integrity["seal_block_capabilities"], {})
        self.assertEqual(integrity["seal_ticker_refreshed"], 2)
        self.assertEqual(output["funnel"]["L3_finalists"], 2)
        self.assertEqual(output["funnel"]["L3_sealed"], 2)
        self.assertEqual(sorted(self.qwen.assets), ["BTC", "ETH"])
        self.assertEqual(self.refresh_call(kraken)["params"], {"pair": FINALIST_PAIRS})
        record = self.run_records[0]["data_quality"]["integrity"]
        self.assertEqual(record["seal_ticker_refreshed"], 2)
        self.assertIn('"seal_ticker_refreshed": 2', self.count("SELECT data_quality_json FROM radar_runs"))
        # The sealed evidence records the refetched receipt, after the slow stage.
        ticker = self.seal_ticker_record("BTC")
        self.assertEqual(ticker["status"], "PASS")
        self.assertGreaterEqual(datetime.fromisoformat(ticker["received_at"]), wiring.T0 + SLOW_STAGE)

    def test_refresh_requests_only_the_finalists_pairs(self):
        kraken = slow_cycle_kraken()

        with mock.patch.object(config, "L3_MAX_FINALISTS", 1):
            output = run_slow_cycle(self, kraken)

        self.assertEqual(output["funnel"]["L3_finalists"], 1)
        self.assertEqual(len(self.qwen.assets), 1)
        finalist_pair = wiring.ASSETS[self.qwen.assets[0]][0]
        self.assertEqual(self.refresh_call(kraken)["params"], {"pair": finalist_pair})
        self.assertEqual(output["data_quality"]["integrity"]["seal_ticker_refreshed"], 1)

    def assert_refresh_failure_keeps_the_block(self, response):
        kraken = slow_cycle_kraken()
        kraken.overrides[(TICKER_PATH, FINALIST_PAIRS)] = response

        with self.assertLogs(WIRING_LOGGER, level="WARNING") as logs:
            output = run_slow_cycle(self, kraken)

        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["seal_blocked"], 2)
        self.assertEqual(integrity["seal_block_capabilities"], {"spot_ticker": 2})
        self.assertEqual(integrity["seal_ticker_refreshed"], 0)
        self.assertEqual(output["funnel"]["L3_sealed"], 0)
        self.assertEqual(self.qwen.calls, [])
        self.refresh_call(kraken)
        self.assertEqual(len(self.run_records), 1)  # the cycle still completes
        self.assertTrue(any("Seal ticker refresh failed" in line for line in logs.output))

    def test_refresh_error_payload_keeps_the_seal_blocked(self):
        self.assert_refresh_failure_keeps_the_block(
            wiring.FakeResponse({"error": ["EService:Unavailable"], "result": {}})
        )

    def test_refresh_http_failure_keeps_the_seal_blocked(self):
        self.assert_refresh_failure_keeps_the_block(wiring.FakeResponse(status_code=503))

    def test_refresh_without_result_keeps_the_seal_blocked(self):
        self.assert_refresh_failure_keeps_the_block(wiring.FakeResponse({"error": []}))

    def test_refresh_non_object_payload_keeps_the_seal_blocked(self):
        self.assert_refresh_failure_keeps_the_block(wiring.FakeResponse(["x"]))

    def test_refresh_non_json_body_keeps_the_seal_blocked(self):
        self.assert_refresh_failure_keeps_the_block(NonJsonResponse())

    def test_partial_refresh_blocks_only_the_missing_finalist(self):
        kraken = slow_cycle_kraken()
        btc_pair, _ws, _symbol, _fp, btc_price = wiring.ASSETS["BTC"]
        kraken.overrides[(TICKER_PATH, FINALIST_PAIRS)] = wiring.FakeResponse(
            {"error": [], "result": {btc_pair: wiring._ticker_row(btc_price)}}
        )

        with self.assertLogs(WIRING_LOGGER, level="WARNING") as logs:
            output = run_slow_cycle(self, kraken)

        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["seal_blocked"], 1)
        self.assertEqual(integrity["seal_block_capabilities"], {"spot_ticker": 1})
        self.assertEqual(integrity["seal_ticker_refreshed"], 1)
        self.assertEqual(output["funnel"]["L3_sealed"], 1)
        self.assertEqual(self.qwen.assets, ["BTC"])
        self.assertTrue(any("Seal ticker refresh returned no usable row for XETHZUSD" in line for line in logs.output))

    def test_fast_cycle_makes_no_extra_ticker_request(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))

        output = self.run_cycle(kraken)

        self.assertEqual(len(ticker_calls(kraken)), 1)
        self.assertEqual(output["data_quality"]["integrity"]["seal_ticker_refreshed"], 0)
        self.assertEqual(output["funnel"]["L3_sealed"], 2)

    def test_slow_heartbeat_cycle_makes_no_extra_ticker_request(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        session = wiring.GuardedSession(1.0, 0, 0.0, http_session=kraken)
        clock = wiring.StepClock()
        real_run_l2 = heartbeat.run_l2

        def slow_run_l2(*args, **kwargs):
            result = real_run_l2(*args, **kwargs)
            with clock._lock:
                clock._now += SLOW_STAGE
            return result

        with mock.patch.object(heartbeat, "run_l2", slow_run_l2):
            output = heartbeat.run_heartbeat(mode="TEST", store=self.store, full=False, session=session, clock=clock)

        self.assertEqual(len(ticker_calls(kraken)), 1)
        self.assertEqual(output["data_quality"]["integrity"]["seal_ticker_refreshed"], 0)


class SpotTickerPairsTests(unittest.TestCase):
    def test_no_argument_call_is_the_unfiltered_global_ticker(self):
        kraken = wiring.FakeKraken(assets=("BTC", "ETH"))
        session = wiring.GuardedSession(1.0, 0, 0.0, http_session=kraken)

        rows = kraken_spot.fetch_ticker(session)
        kraken_spot.fetch_ticker(session, ["XXBTZUSD"])

        self.assertEqual(sorted(rows), ["XETHZUSD", "XXBTZUSD"])
        self.assertEqual([call["params"] for call in kraken.calls], [{}, {"pair": "XXBTZUSD"}])

    def test_empty_pair_list_is_refused_rather_than_turned_into_a_global_call(self):
        kraken = wiring.FakeKraken()
        session = wiring.GuardedSession(1.0, 0, 0.0, http_session=kraken)

        with self.assertRaises(ValueError):
            kraken_spot.fetch_ticker(session, [])
        self.assertEqual(kraken.calls, [])


class StageTimingTests(wiring.IntegrityWiringBase):
    def latency(self, full, tracking):
        session = wiring.GuardedSession(1.0, 0, 0.0, http_session=wiring.FakeKraken(assets=("BTC", "ETH")))
        with mock.patch.object(config, "RADAR_OUTCOME_TRACKING_ENABLED", tracking):
            heartbeat.run_heartbeat(mode="TEST", store=self.store, full=full, session=session, clock=wiring.StepClock())
        self.assertEqual(len(self.run_records), 1)
        return self.run_records[0]["latency_ms"]

    def assert_stage_keys(self, latency, expected):
        self.assertEqual({key for key in STAGE_KEYS if key in latency}, set(expected))
        for key in expected:
            self.assertIsInstance(latency[key], float)
            self.assertGreaterEqual(latency[key], 0.0)
            self.assertLessEqual(latency[key], latency["total_ms"])
        for key in ("asset_pairs_ms", "spot_ticker_ms", "futures_ticker_ms", "total_ms"):
            self.assertIn(key, latency)  # the existing keys stay

    def test_full_cycle_with_tracking_records_every_stage(self):
        self.assert_stage_keys(self.latency(full=True, tracking=True), STAGE_KEYS)

    def test_heartbeat_cycle_has_no_l3_timing(self):
        self.assert_stage_keys(
            self.latency(full=False, tracking=True),
            ("l2_ms", "forward_labels_ms", "outcome_labels_ms", "outcome_register_ms"),
        )

    def test_tracking_off_has_no_outcome_timings(self):
        self.assert_stage_keys(self.latency(full=True, tracking=False), ("l2_ms", "forward_labels_ms", "l3_ms"))

    def test_outcome_label_failure_still_records_its_timing(self):
        with mock.patch.object(heartbeat.outcome_store, "label_due_outcomes", side_effect=RuntimeError("boom")):
            latency = self.latency(full=False, tracking=True)
        self.assertGreaterEqual(latency["outcome_labels_ms"], 0.0)


if __name__ == "__main__":
    unittest.main()
