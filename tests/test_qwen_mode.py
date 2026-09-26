"""RADAR_QWEN_MODE (inline | shadow | off) and the qwen_reviews log.

Integration tests over the T023b wiring fixtures (fake Kraken transport, deterministic
clock, disposable SQLite store and JSONL paths). The model is always a fake review
function or ``qwen.review_finalists`` over a fake transport: no network, no Ollama.
Shadow batches run on a real thread, but every wait is on a ``threading.Event`` or a
bounded ``join`` that only guards against a hung test; nothing sleeps.
"""

import json
import logging
import os
import sqlite3
import threading
import unittest
from datetime import timedelta
from unittest import mock

import requests
import test_integrity_wiring as wiring
from test_qwen import ollama_response

from radar_v08 import config, heartbeat, qwen, qwen_shadow
from radar_v08.http_client import GuardedSession
from radar_v08.qwen import QwenBatchResult, QwenReview
from radar_v08.qwen_shadow import QwenShadow
from radar_v08.router import route
from radar_v08.store import SnapshotStore

# Seconds. Only a guard against a hung test; no assertion depends on elapsed time.
HANG_GUARD = 10.0

COUNTER_KEYS = {"batches_submitted", "rows_recorded", "batches_dropped_admission", "record_failures", "batch_pending"}

# Keys that differ between two otherwise identical runs (fresh ids per run).
VOLATILE_KEYS = {"id", "event_id", "run_id", "alert_id"}


def review(asset, *, veto=False, call_sonnet=False, call_fable=False, confidence="HIGH", direction="LONG"):
    return QwenReview(
        asset=asset, setup_type="BREAKOUT", direction=direction, market="FUTURES", veto=veto,
        call_sonnet=call_sonnet, call_fable=call_fable, confidence=confidence, reason="fake review",
    )


def forceful_batch(payloads):
    """HIGH-confidence opinions that change routing whenever they reach the router:
    a veto for BTC, fable + sonnet with the opposite direction for everything else."""
    reviews = {}
    for payload in payloads:
        asset = payload["asset"]
        if asset == "BTC":
            reviews[asset] = review(asset, veto=True)
        else:
            reviews[asset] = review(asset, call_sonnet=True, call_fable=True, direction="SHORT")
    return QwenBatchResult(status="OK", reviews=reviews, elapsed_ms=12.5, attempts=1)


class FakeReview:
    """Stands in for ``review_finalists``: records each call and the thread it ran on,
    optionally blocks on an unset Event, then returns ``respond(payloads)`` (or raises it)."""

    def __init__(self, respond=forceful_batch, *, block=False):
        self.respond = respond
        self.calls = []
        self.threads = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()

    def __call__(self, payloads):
        self.calls.append(payloads)
        self.threads.append(threading.current_thread())
        self.entered.set()
        if not self.release.wait(HANG_GUARD):
            raise AssertionError("the fake Qwen was never released")
        return self.respond(payloads)


def normalize(value):
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items() if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


class QwenModeBase(wiring.IntegrityWiringBase):
    def setUp(self):
        super().setUp()
        self.threads_started = 0
        self.slot_held = False
        self.fakes = []
        self.results = []

        def thread_factory(**kwargs):
            self.threads_started += 1
            return threading.Thread(**kwargs)

        self.shadow = QwenShadow(thread_factory=thread_factory, inference_active=lambda: self.slot_held)
        previous = qwen_shadow.reset_default_shadow()
        self.addCleanup(qwen_shadow.reset_default_shadow, previous)
        self.addCleanup(self._release_and_join)

        def spy_route(ctx, data_quality_ok=True):
            result = route(ctx, data_quality_ok=data_quality_ok)
            self.routed.append(ctx)
            self.results.append((ctx.asset, result))
            return result

        self.patch(heartbeat, "route", spy_route)

    def _release_and_join(self):
        for fake in self.fakes:
            fake.release.set()
        self.shadow.join(HANG_GUARD)

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_mode(self, mode):
        self.patch(config, "RADAR_QWEN_MODE", mode)

    def fake(self, respond=forceful_batch, *, block=False):
        fake = FakeReview(respond, block=block)
        self.fakes.append(fake)
        return fake

    def cycle(self, review_fn=None, *, full=True, clock=None, assets=("BTC", "ETH")):
        self.routed.clear()
        self.results.clear()
        session = GuardedSession(1.0, 0, 0.0, http_session=wiring.FakeKraken(assets=assets))
        with mock.patch.object(heartbeat, "review_finalists", review_fn if review_fn is not None else self.qwen):
            return heartbeat.run_heartbeat(
                mode="TEST", store=self.store, full=full, session=session,
                clock=clock or wiring.StepClock(), qwen_shadow=self.shadow,
            )

    def finish_shadow(self, fake):
        fake.release.set()
        self.assertTrue(self.shadow.join(HANG_GUARD), "the shadow batch did not finish")

    def use_store(self, name):
        self.db_path = os.path.join(self.tmp, name)
        self.store = SnapshotStore(self.db_path)
        self.addCleanup(self.store.close)

    def reviews_for(self, run_id):
        return {stored.row.asset: stored.row for stored in self.store.qwen_reviews_for_run(run_id)}

    def review_row_count(self):
        return self.count("SELECT COUNT(*) FROM qwen_reviews")

    def decisions(self):
        return {asset: result.decision for asset, result in self.results}

    def rows(self, table):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY asset")]
        for row in rows:
            if "context_json" in row:
                row["context_json"] = json.loads(row["context_json"])
        return normalize(rows)

    def counters(self, output):
        return output["data_quality"]["qwen_reviews"]

    def assert_skipped_contexts(self):
        self.assertTrue(self.routed, "nothing reached the router")
        for ctx in self.routed:
            self.assertEqual(ctx.qwen_status, "SKIPPED")
            self.assertFalse(ctx.qwen_veto)
            self.assertFalse(ctx.qwen_call_sonnet)
            self.assertFalse(ctx.qwen_call_fable)
            self.assertIsNone(ctx.qwen_confidence)
            self.assertIsNone(ctx.qwen_direction)

    def all_ignore(self):
        self.patch(config, "ROUTER_SONNET_MIN_OPPORTUNITY", 1000.0)
        self.patch(config, "ROUTER_FABLE_MIN_OPPORTUNITY", 1000.0)


class TestParseQwenMode(unittest.TestCase):
    def test_unset_means_shadow(self):
        self.assertEqual(config.QWEN_MODES, ("inline", "shadow", "off"))
        self.assertEqual(config.QWEN_MODE_DEFAULT, "shadow")
        self.assertEqual(config.parse_qwen_mode(None), "shadow")

    def test_trims_and_ignores_case(self):
        for raw, expected in [
            ("inline", "inline"), (" Inline ", "inline"), ("SHADOW", "shadow"), ("\tOff\n", "off"), ("off", "off"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(config.parse_qwen_mode(raw), expected)

    def test_any_other_value_raises_naming_the_allowed_values(self):
        for raw in ["", "   ", "fast", "none", "0", "inline,shadow", "in line"]:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    config.parse_qwen_mode(raw)
                self.assertIn("inline, shadow, off", str(caught.exception))

    def test_config_value_comes_from_the_helper(self):
        self.assertEqual(config.RADAR_QWEN_MODE, config.parse_qwen_mode(os.environ.get("RADAR_QWEN_MODE")))


class TestOffMode(QwenModeBase):
    def test_off_never_calls_qwen_and_routes_without_it(self):
        self.use_mode("off")

        output = self.cycle()

        self.assertEqual(self.qwen.calls, [])
        self.assertEqual(self.threads_started, 0)
        self.assertEqual(sorted(self.routed_assets()), ["BTC", "ETH"])
        self.assert_skipped_contexts()
        data_quality = output["data_quality"]
        self.assertEqual(data_quality["qwen"], "SKIPPED")
        self.assertNotIn("qwen_batch", data_quality)
        self.assertEqual(data_quality["qwen_mode"], "off")
        self.assertEqual(
            self.counters(output),
            {"batches_submitted": 0, "rows_recorded": 0, "batches_dropped_admission": 0,
             "record_failures": 0, "batch_pending": False},
        )
        self.assertEqual(output["funnel"]["qwen_reviewed"], 0)
        self.assertEqual(self.review_row_count(), 0)


class TestInlineRecordsEveryFinalist(QwenModeBase):
    def test_inline_rows_include_ignore_finalists_with_the_cycle_router_decision(self):
        self.use_mode("inline")
        fake = self.fake()

        output = self.cycle(fake)

        self.assertEqual(fake.threads, [threading.main_thread()])  # in the cycle, as at HEAD
        self.assertEqual(self.threads_started, 0)
        decisions = self.decisions()
        self.assertEqual(decisions["BTC"], "IGNORE")  # the HIGH veto reached the router
        self.assertNotEqual(decisions["ETH"], "IGNORE")
        self.assertEqual(output["data_quality"]["qwen"], "OK")
        self.assertEqual(output["data_quality"]["qwen_mode"], "inline")

        rows = self.reviews_for(output["run_id"])
        self.assertEqual(sorted(rows), ["BTC", "ETH"])
        for asset, row in rows.items():
            self.assertEqual(row.mode, "inline")
            self.assertEqual(row.cycle_ts, output["timestamp"])
            self.assertEqual(row.router_decision, decisions[asset])
            self.assertEqual((row.setup_type, row.direction), ("BREAKOUT", "LONG"))
            self.assertEqual((row.anomaly_score, row.opportunity_score, row.tradeability_score), (5.0, 80.0, 80.0))
            self.assertEqual((row.batch_status, row.elapsed_ms, row.attempts, row.error_code), ("OK", 12.5, 1, None))
        self.assertEqual((rows["BTC"].veto, rows["BTC"].confidence, rows["BTC"].review_direction), (True, "HIGH", "LONG"))
        self.assertEqual(
            (rows["ETH"].veto, rows["ETH"].call_sonnet, rows["ETH"].call_fable, rows["ETH"].review_direction),
            (False, True, True, "SHORT"),
        )
        counters = self.counters(output)
        self.assertEqual((counters["batches_submitted"], counters["rows_recorded"]), (1, 2))
        self.assertEqual(self.run_records[-1]["data_quality"]["qwen_reviews"], counters)


class TestShadowDoesNotBlock(QwenModeBase):
    def test_cycle_returns_while_the_review_is_blocked_with_the_inline_payloads(self):
        self.use_mode("inline")
        inline_fake = self.fake()
        self.cycle(inline_fake)
        self.use_mode("shadow")
        fake = self.fake(block=True)

        output = self.cycle(fake)

        self.assertTrue(fake.entered.wait(HANG_GUARD))
        self.assertFalse(fake.release.is_set())  # still blocked: the cycle did not wait for it
        self.assertEqual(fake.calls, inline_fake.calls)
        self.assertEqual(sorted(p["asset"] for p in fake.calls[0]), ["BTC", "ETH"])
        self.assertIsNot(fake.threads[0], threading.main_thread())
        self.assertEqual(self.threads_started, 1)
        self.assertEqual(output["data_quality"]["qwen_mode"], "shadow")
        self.assertEqual(
            self.counters(output),
            {"batches_submitted": 1, "rows_recorded": 0, "batches_dropped_admission": 0,
             "record_failures": 0, "batch_pending": True},
        )
        self.assertEqual(self.reviews_for(output["run_id"]), {})
        self.finish_shadow(fake)

    def test_heartbeat_defaults_to_the_process_submitter(self):
        self.use_mode("shadow")
        fake = self.fake(block=True)
        session = GuardedSession(1.0, 0, 0.0, http_session=wiring.FakeKraken())

        with mock.patch.object(heartbeat, "review_finalists", fake):
            output = heartbeat.run_heartbeat(
                mode="TEST", store=self.store, full=True, session=session, clock=wiring.StepClock()
            )

        submitter = qwen_shadow.default_shadow()
        self.assertTrue(fake.entered.wait(HANG_GUARD))
        self.assertTrue(submitter.pending())
        self.assertTrue(self.counters(output)["batch_pending"])
        fake.release.set()
        self.assertTrue(submitter.join(HANG_GUARD))
        self.assertIsNot(qwen_shadow.reset_default_shadow(), None)
        self.assertIsNot(qwen_shadow.default_shadow(), submitter)


class TestShadowRoutingEqualsOff(QwenModeBase):
    def run_mode(self, mode, store_name):
        self.use_store(store_name)
        self.use_mode(mode)
        fake = self.fake(block=True)
        output = self.cycle(fake)
        contexts, results = list(self.routed), list(self.results)
        if mode == "shadow":
            self.assertTrue(fake.entered.wait(HANG_GUARD))
            self.finish_shadow(fake)
        else:
            self.assertEqual(fake.calls, [])
        return {
            "contexts": contexts,
            "results": results,
            "events": self.rows("events"),
            "alerts": self.rows("alerts"),
            "candidates": normalize(output["candidates"]),
            "funnel": output["funnel"],
            "qwen": output["data_quality"]["qwen"],
            "has_qwen_batch": "qwen_batch" in output["data_quality"],
        }

    def test_forceful_shadow_reviews_change_nothing(self):
        off = self.run_mode("off", "off.sqlite")
        shadow = self.run_mode("shadow", "shadow.sqlite")

        self.assertEqual(sorted(asset for asset, _ in shadow["results"]), ["BTC", "ETH"])
        for ctx in shadow["contexts"]:
            self.assertEqual(ctx.qwen_status, "SKIPPED")
        self.assertEqual(shadow["contexts"], off["contexts"])
        self.assertEqual(
            [(asset, r.decision, r.reasons) for asset, r in shadow["results"]],
            [(asset, r.decision, r.reasons) for asset, r in off["results"]],
        )
        self.assertEqual(shadow["results"], off["results"])
        # No Qwen: the FABLE bar needs ROUTER_FABLE_MIN_CONFIRMATIONS_NO_QWEN, and no veto applies.
        self.assertNotIn("qwen_veto_high_confidence", [reason for _a, r in shadow["results"] for reason in r.reasons])
        self.assertTrue(shadow["events"], "the fixture must create events to compare")
        self.assertEqual(shadow["events"], off["events"])
        self.assertTrue(shadow["alerts"])
        self.assertEqual(shadow["alerts"], off["alerts"])
        self.assertEqual(shadow["candidates"], off["candidates"])
        for candidate in shadow["candidates"]:
            self.assertIsNone(candidate["qwen"])
        self.assertEqual(shadow["funnel"], off["funnel"])
        self.assertEqual(shadow["funnel"]["qwen_reviewed"], 0)
        self.assertEqual((shadow["qwen"], shadow["has_qwen_batch"]), ("SKIPPED", False))
        self.assertEqual((off["qwen"], off["has_qwen_batch"]), ("SKIPPED", False))


class TestShadowAdmission(QwenModeBase):
    def test_a_batch_in_flight_drops_the_next_one_instead_of_queueing_it(self):
        self.use_mode("shadow")
        first = self.fake(block=True)
        first_output = self.cycle(first)
        self.assertTrue(first.entered.wait(HANG_GUARD))

        dropped_fake = self.fake()
        dropped = self.cycle(dropped_fake)

        self.assertEqual(dropped_fake.calls, [])
        self.assertEqual(self.threads_started, 1)
        self.assertEqual(
            self.counters(dropped),
            {"batches_submitted": 0, "rows_recorded": 0, "batches_dropped_admission": 1,
             "record_failures": 0, "batch_pending": True},
        )
        self.assert_skipped_contexts()

        self.finish_shadow(first)
        self.assertEqual(dropped_fake.calls, [])  # nothing was queued behind the first batch
        third = self.fake(block=True)
        resumed = self.cycle(third)

        self.assertTrue(third.entered.wait(HANG_GUARD))
        self.assertEqual(len(third.calls), 1)
        self.assertEqual(self.threads_started, 2)
        counters = self.counters(resumed)
        self.assertEqual((counters["batches_submitted"], counters["batches_dropped_admission"]), (1, 0))
        self.assertEqual(counters["rows_recorded"], 2)  # the first batch, drained late
        self.assertEqual(sorted(self.reviews_for(first_output["run_id"])), ["BTC", "ETH"])
        self.assertEqual(self.reviews_for(dropped["run_id"]), {})
        self.finish_shadow(third)

    def test_a_held_inference_slot_drops_without_starting_a_thread(self):
        self.use_mode("shadow")
        self.slot_held = True
        fake = self.fake()

        dropped = self.cycle(fake)

        self.assertEqual(fake.calls, [])
        self.assertEqual(self.threads_started, 0)
        self.assertEqual(self.counters(dropped)["batches_dropped_admission"], 1)
        self.assertFalse(self.counters(dropped)["batch_pending"])

        self.slot_held = False
        submitted = self.cycle(fake)

        self.assertTrue(fake.entered.wait(HANG_GUARD))
        self.assertEqual(self.threads_started, 1)
        self.assertEqual(self.counters(submitted)["batches_submitted"], 1)

    def test_default_admission_reads_the_audit_qwen_02_slot(self):
        self.assertTrue(qwen._INFERENCE_SLOT.acquire(timeout=HANG_GUARD))
        try:
            self.assertTrue(qwen.inference_active())
            self.assertTrue(QwenShadow().in_flight())
        finally:
            qwen._INFERENCE_SLOT.release()
        self.assertFalse(qwen.inference_active())
        self.assertFalse(QwenShadow().in_flight())


class TestLateResults(QwenModeBase):
    def setUp(self):
        super().setUp()
        # This oracle is "the cycle's now is the injected clock's first
        # reading", while the fake venue's serverTime stays pinned; the
        # corrected clock would re-anchor the cycle on that venue time. The
        # corrected clock is covered by tests/test_clock_correction.py.
        patcher = mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_late_batch_is_written_under_its_origin_cycle(self):
        self.use_mode("shadow")
        origin_fake = self.fake(block=True)
        with mock.patch.object(config, "ROUTER_SONNET_MIN_OPPORTUNITY", 1000.0), \
                mock.patch.object(config, "ROUTER_FABLE_MIN_OPPORTUNITY", 1000.0):
            origin = self.cycle(origin_fake)
        origin_decisions = self.decisions()
        self.assertEqual(set(origin_decisions.values()), {"IGNORE"})
        self.assertTrue(origin_fake.entered.wait(HANG_GUARD))
        self.assertEqual(self.reviews_for(origin["run_id"]), {})
        self.finish_shadow(origin_fake)  # completed after the origin cycle returned

        later_fake = self.fake(block=True)
        later = self.cycle(later_fake, clock=wiring.StepClock(start=wiring.T0 + timedelta(milliseconds=20)))

        self.assert_skipped_contexts()
        later_decisions = self.decisions()
        self.assertNotIn("IGNORE", later_decisions.values())
        self.assertNotEqual(later["timestamp"], origin["timestamp"])
        self.assertEqual(self.counters(later)["rows_recorded"], 2)
        rows = self.reviews_for(origin["run_id"])
        payloads = {p["asset"]: p for p in origin_fake.calls[0]}
        self.assertEqual(sorted(rows), ["BTC", "ETH"])
        for asset, row in rows.items():
            self.assertEqual(row.mode, "shadow")
            self.assertEqual(row.cycle_ts, origin["timestamp"])
            self.assertEqual(row.router_decision, origin_decisions[asset])
            self.assertEqual(row.direction, payloads[asset]["direction"])
            self.assertEqual(
                (row.anomaly_score, row.opportunity_score, row.tradeability_score),
                (payloads[asset]["anomaly_score"], payloads[asset]["opportunity_score"],
                 payloads[asset]["tradeability_score"]),
            )
        self.assertEqual(self.reviews_for(later["run_id"]), {})
        self.assertEqual(self.review_row_count(), 2)

        # The later cycle's own batch is written by a heartbeat-only cycle, under the later cycle.
        self.finish_shadow(later_fake)
        drain = self.cycle(full=False, clock=wiring.StepClock(start=wiring.T0 + timedelta(milliseconds=40)))
        self.assertEqual(self.counters(drain)["rows_recorded"], 2)
        later_rows = self.reviews_for(later["run_id"])
        self.assertEqual({a: r.router_decision for a, r in later_rows.items()}, later_decisions)
        self.assertEqual({r.cycle_ts for r in later_rows.values()}, {later["timestamp"]})
        self.assertEqual(self.reviews_for(drain["run_id"]), {})


class TestPersistedOutcomes(QwenModeBase):
    def shadow_rows(self, respond, *, log_warning=None):
        self.use_mode("shadow")
        self.all_ignore()
        fake = self.fake(respond, block=True)
        origin = self.cycle(fake)
        self.assertTrue(fake.entered.wait(HANG_GUARD))
        if log_warning is None:
            self.finish_shadow(fake)
        else:
            with self.assertLogs("radar_v08.qwen_shadow", logging.WARNING) as logs:
                self.finish_shadow(fake)
            self.assertIn(log_warning, "\n".join(logs.output))
        drain = self.cycle(full=False)
        self.assertEqual(self.counters(drain)["rows_recorded"], 2)
        rows = self.reviews_for(origin["run_id"])
        self.assertEqual(sorted(rows), ["BTC", "ETH"])
        for row in rows.values():
            self.assertEqual((row.mode, row.router_decision, row.cycle_ts), ("shadow", "IGNORE", origin["timestamp"]))
        return rows

    def assert_no_review_fields(self, row):
        self.assertEqual(
            (row.veto, row.confidence, row.review_direction, row.call_sonnet, row.call_fable),
            (None, None, None, None, None),
        )

    def test_ok_batch_records_the_review_fields_of_ignore_finalists(self):
        rows = self.shadow_rows(forceful_batch)

        for row in rows.values():
            self.assertEqual((row.batch_status, row.elapsed_ms, row.attempts, row.error_code), ("OK", 12.5, 1, None))
        self.assertEqual(
            (rows["BTC"].veto, rows["BTC"].confidence, rows["BTC"].review_direction, rows["BTC"].call_fable),
            (True, "HIGH", "LONG", False),
        )
        self.assertEqual(
            (rows["ETH"].veto, rows["ETH"].call_sonnet, rows["ETH"].call_fable, rows["ETH"].review_direction),
            (False, True, True, "SHORT"),
        )

    def test_timeout_records_null_review_fields_with_the_error_code(self):
        def timed_out(payloads):
            def post(_request):
                raise requests.Timeout("fake transport timeout")

            return qwen.review_finalists(payloads, post_fn=post)

        rows = self.shadow_rows(timed_out)

        for row in rows.values():
            self.assertEqual((row.batch_status, row.error_code), ("TIMEOUT", "timeout"))
            self.assertEqual(row.attempts, config.QWEN_MAX_RETRIES_ON_INVALID + 1)
            self.assertIsNotNone(row.elapsed_ms)
            self.assert_no_review_fields(row)

    def test_schema_failure_records_unavailable_schema_invalid(self):
        def wrong_shape(payloads):
            return qwen.review_finalists(payloads, post_fn=lambda _request: ollama_response(None))

        rows = self.shadow_rows(wrong_shape)

        for row in rows.values():
            self.assertEqual((row.batch_status, row.error_code), ("UNAVAILABLE", "schema_invalid"))
            self.assertEqual(row.attempts, 2)
            self.assertIsNotNone(row.elapsed_ms)
            self.assert_no_review_fields(row)

    def test_an_exception_in_the_shadow_thread_records_error_rows(self):
        def explode(_payloads):
            raise RuntimeError("fake model crash")

        rows = self.shadow_rows(explode, log_warning="RuntimeError")

        for row in rows.values():
            self.assertEqual((row.batch_status, row.error_code, row.attempts, row.elapsed_ms), ("ERROR", "RuntimeError", 0, None))
            self.assert_no_review_fields(row)


class TestPersistenceFailure(QwenModeBase):
    def run_inline(self, store_name, *, broken):
        self.use_store(store_name)
        self.use_mode("inline")
        if broken:
            self.patch(self.store, "record_qwen_reviews", mock.Mock(side_effect=sqlite3.OperationalError("disk I/O error")))
        output = self.cycle(self.fake())
        return output, list(self.results), self.rows("events"), self.rows("alerts")

    def test_a_failed_write_is_logged_and_counted_and_changes_nothing_else(self):
        control, control_results, control_events, control_alerts = self.run_inline("control.sqlite", broken=False)

        with self.assertLogs("radar_v08.test_integrity_wiring", logging.WARNING) as logs:
            output, results, events, alerts = self.run_inline("broken.sqlite", broken=True)

        self.assertIn("Qwen reviews: failed to record", "\n".join(logs.output))
        counters = self.counters(output)
        self.assertEqual((counters["batches_submitted"], counters["rows_recorded"], counters["record_failures"]), (1, 0, 1))
        self.assertEqual(len(self.run_records), 2)  # the cycle finished and wrote its record
        self.assertEqual(results, control_results)
        self.assertEqual(events, control_events)
        self.assertEqual(alerts, control_alerts)
        self.assertEqual(output["funnel"], control["funnel"])
        self.assertEqual(self.review_row_count(), 0)

    def test_a_failed_late_write_does_not_abort_a_heartbeat_cycle(self):
        self.use_mode("shadow")
        fake = self.fake(block=True)
        self.cycle(fake)
        self.finish_shadow(fake)
        self.patch(self.store, "record_qwen_reviews", mock.Mock(side_effect=RuntimeError("store closed")))

        drain = self.cycle(full=False)

        self.assertEqual(self.counters(drain)["record_failures"], 1)
        self.assertEqual(self.counters(drain)["rows_recorded"], 0)
        self.assertEqual(len(self.run_records), 2)


class TestRunRecordCounters(QwenModeBase):
    def assert_counters(self, data_quality, mode):
        self.assertEqual(data_quality["qwen_mode"], mode)
        self.assertEqual(set(data_quality["qwen_reviews"]), COUNTER_KEYS)

    def test_every_cycle_records_mode_and_counters(self):
        for mode in ("shadow", "inline", "off"):
            for full in (True, False):
                with self.subTest(mode=mode, full=full):
                    self.use_mode(mode)
                    self.cycle(self.fake(), full=full)
                    self.assertTrue(self.shadow.join(HANG_GUARD))
                    self.assert_counters(self.run_records[-1]["data_quality"], mode)
                    with sqlite3.connect(self.db_path) as conn:
                        stored = conn.execute(
                            "SELECT data_quality_json FROM radar_runs ORDER BY rowid DESC LIMIT 1"
                        ).fetchone()[0]
                    self.assert_counters(json.loads(stored), mode)

    def test_a_heartbeat_cycle_reports_zero_counters(self):
        self.use_mode("shadow")

        output = self.cycle(full=False)

        self.assertEqual(self.qwen.calls, [])
        self.assertEqual(
            self.counters(output),
            {"batches_submitted": 0, "rows_recorded": 0, "batches_dropped_admission": 0,
             "record_failures": 0, "batch_pending": False},
        )


if __name__ == "__main__":
    unittest.main()
