import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import claude_bridge, config
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore

T0 = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def valid_analysis(asset="BTC", recommendation="MODERATE_OPPORTUNITY"):
    return {
        "asset": asset, "market": "SPOT", "direction": "LONG", "setup_type": "BREAKOUT",
        "thesis": "Coherent breakout with volume confirmation.",
        "entry": 50000.0, "entry_range": {"low": 49800.0, "high": 50200.0},
        "stop": 49000.0, "tp1": 51000.0, "tp2": 52000.0,
        "leverage": None, "margin": None, "notional": None,
        "max_loss": 200.0, "expected_profit": 400.0, "net_rr": 2.0,
        "confidence": "MEDIUM", "risks": ["thin book"], "invalidation": "close back below 49000",
        "alternatives": ["wait for retest"], "capital_status": "UNAVAILABLE",
        "recommendation": recommendation, "reasoning_summary": "Breakout with volume; moderate confidence.",
    }


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeUsage:
    def __init__(self, input_tokens=111, output_tokens=222):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(self, payload, input_tokens=111, output_tokens=222):
        self.content = [FakeTextBlock(json.dumps(payload))]
        self.usage = FakeUsage(input_tokens, output_tokens)


def make_create_fn(response_or_exc):
    calls = []

    def create_fn(model, max_tokens, system, user_content, schema):
        calls.append({"model": model, "max_tokens": max_tokens, "system": system, "user_content": user_content, "schema": schema})
        if isinstance(response_or_exc, Exception):
            raise response_or_exc
        return response_or_exc

    create_fn.calls = calls
    return create_fn


def make_event_kwargs(**overrides):
    base = dict(
        ts=T0.isoformat(), type_="RADAR_ALERT", asset="BTC",
        setup_type="BREAKOUT", direction="LONG", market="SPOT",
        anomaly_score=70.0, opportunity_score=80.0, tradeability_score=85.0,
        confidence="HIGH", model_demand="FABLE", reason="test", status="PENDING",
        context={"asset": "BTC", "market": "SPOT"},
    )
    base.update(overrides)
    return base


class TestCallModel(unittest.TestCase):
    def test_successful_sonnet_dispatch(self):
        create_fn = make_create_fn(FakeResponse(valid_analysis("BTC")))
        result = claude_bridge.call_model("SONNET", {"asset": "BTC"}, create_fn=create_fn)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.parsed["asset"], "BTC")
        self.assertEqual(create_fn.calls[0]["model"], config.ANTHROPIC_SONNET_MODEL)

    def test_successful_fable_dispatch(self):
        create_fn = make_create_fn(FakeResponse(valid_analysis("ETH", "STRONG_OPPORTUNITY")))
        result = claude_bridge.call_model("FABLE", {"asset": "ETH"}, create_fn=create_fn)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.parsed["recommendation"], "STRONG_OPPORTUNITY")
        self.assertEqual(create_fn.calls[0]["model"], config.ANTHROPIC_FABLE_MODEL)

    def test_invalid_model_demand_raises(self):
        with self.assertRaises(ValueError):
            claude_bridge.call_model("IGNORE", {"asset": "BTC"})
        with self.assertRaises(ValueError):
            claude_bridge.call_model("OPUS", {"asset": "BTC"})

    def test_asset_mismatch_is_invalid_response(self):
        create_fn = make_create_fn(FakeResponse(valid_analysis("NOTINPUT")))
        result = claude_bridge.call_model("SONNET", {"asset": "BTC"}, create_fn=create_fn)
        self.assertEqual(result.status, "INVALID_RESPONSE")

    def test_malformed_json_is_invalid_response(self):
        response = FakeResponse(valid_analysis("BTC"))
        response.content = [FakeTextBlock("not json {{{")]
        create_fn = make_create_fn(response)
        result = claude_bridge.call_model("SONNET", {"asset": "BTC"}, create_fn=create_fn)
        self.assertEqual(result.status, "INVALID_RESPONSE")

    def test_bad_enum_is_invalid_response(self):
        payload = valid_analysis("BTC")
        payload["recommendation"] = "BUY_NOW"
        create_fn = make_create_fn(FakeResponse(payload))
        result = claude_bridge.call_model("SONNET", {"asset": "BTC"}, create_fn=create_fn)
        self.assertEqual(result.status, "INVALID_RESPONSE")

    def test_never_sends_kraken_credentials(self):
        os.environ["KRAKEN_API_KEY"] = "top-secret-do-not-leak"
        try:
            create_fn = make_create_fn(FakeResponse(valid_analysis("BTC")))
            claude_bridge.call_model("SONNET", {"asset": "BTC", "note": "context only"}, create_fn=create_fn)
            sent = create_fn.calls[0]["user_content"]
            self.assertNotIn("top-secret-do-not-leak", sent)
            self.assertNotIn("KRAKEN_API_KEY", sent)
        finally:
            del os.environ["KRAKEN_API_KEY"]


class TestClassifyException(unittest.TestCase):
    """Fakes the anthropic exception hierarchy so this test suite runs
    without the `anthropic` package installed - the radar's Kraken/Qwen
    phases must keep working either way."""

    def setUp(self):
        import types
        fake = types.ModuleType("anthropic")

        class APIStatusError(Exception):
            def __init__(self, message="err", status_code=500, type=None):
                super().__init__(message)
                self.status_code = status_code
                self.type = type

        class AuthenticationError(APIStatusError):
            pass

        class PermissionDeniedError(APIStatusError):
            pass

        class RateLimitError(APIStatusError):
            pass

        class APIConnectionError(Exception):
            pass

        class APITimeoutError(APIConnectionError):
            pass

        fake.APIStatusError = APIStatusError
        fake.AuthenticationError = AuthenticationError
        fake.PermissionDeniedError = PermissionDeniedError
        fake.RateLimitError = RateLimitError
        fake.APIConnectionError = APIConnectionError
        fake.APITimeoutError = APITimeoutError
        self.fake_anthropic = fake
        self._orig = sys.modules.get("anthropic")
        sys.modules["anthropic"] = fake

    def tearDown(self):
        if self._orig is not None:
            sys.modules["anthropic"] = self._orig
        else:
            sys.modules.pop("anthropic", None)

    def test_auth_error(self):
        exc = self.fake_anthropic.AuthenticationError("bad key", status_code=401)
        self.assertEqual(claude_bridge.classify_exception(exc), "AUTH_ERROR")

    def test_permission_denied_is_auth_error(self):
        exc = self.fake_anthropic.PermissionDeniedError("forbidden", status_code=403)
        self.assertEqual(claude_bridge.classify_exception(exc), "AUTH_ERROR")

    def test_rate_limit(self):
        exc = self.fake_anthropic.RateLimitError("slow down", status_code=429)
        self.assertEqual(claude_bridge.classify_exception(exc), "RATE_LIMITED")

    def test_timeout(self):
        exc = self.fake_anthropic.APITimeoutError("timed out")
        self.assertEqual(claude_bridge.classify_exception(exc), "TIMEOUT")

    def test_billing_error_is_quota_exhausted(self):
        exc = self.fake_anthropic.APIStatusError("no credits", status_code=402, type="billing_error")
        self.assertEqual(claude_bridge.classify_exception(exc), "QUOTA_EXHAUSTED")

    def test_overloaded_is_provider_unavailable(self):
        exc = self.fake_anthropic.APIStatusError("overloaded", status_code=529, type="overloaded_error")
        self.assertEqual(claude_bridge.classify_exception(exc), "PROVIDER_UNAVAILABLE")

    def test_five_hundred_is_provider_unavailable(self):
        exc = self.fake_anthropic.APIStatusError("server error", status_code=500)
        self.assertEqual(claude_bridge.classify_exception(exc), "PROVIDER_UNAVAILABLE")

    def test_connection_error_is_provider_unavailable(self):
        exc = self.fake_anthropic.APIConnectionError("network down")
        self.assertEqual(claude_bridge.classify_exception(exc), "PROVIDER_UNAVAILABLE")

    def test_bad_request_is_invalid_response(self):
        exc = self.fake_anthropic.APIStatusError("bad schema", status_code=400)
        self.assertEqual(claude_bridge.classify_exception(exc), "INVALID_RESPONSE")

    def test_call_model_maps_exception_to_status(self):
        create_fn = make_create_fn(self.fake_anthropic.RateLimitError("slow down", status_code=429))
        result = claude_bridge.call_model("FABLE", {"asset": "BTC"}, create_fn=create_fn)
        self.assertEqual(result.status, "RATE_LIMITED")
        self.assertIsNone(result.parsed)


class BridgeCycleTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)
        self._original_events_log_path = config.EVENTS_LOG_PATH
        self.events_log = self.path + ".events.jsonl"
        config.EVENTS_LOG_PATH = self.events_log

    def tearDown(self):
        self.store.close()
        config.EVENTS_LOG_PATH = self._original_events_log_path
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(self.events_log):
            os.remove(self.events_log)


class TestRunBridgeCycle(BridgeCycleTestCase):
    def test_pending_fable_event_is_processed_and_notified(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        create_fn = make_create_fn(FakeResponse(valid_analysis("BTC", "STRONG_OPPORTUNITY")))
        notified = []

        result = claude_bridge.run_bridge_cycle(
            self.store, now=T0, create_fn=create_fn, notify_fn=notified.append,
        )

        self.assertEqual(result.health, "ONLINE")
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "PROCESSED")
        self.assertEqual(row["notified"], 1)
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]["asset"], "BTC")
        analyses = self.store.get_model_analyses_for_event(event_id)
        self.assertEqual(len(analyses), 1)
        self.assertEqual(analyses[0]["status"], "SUCCESS")

    def test_sonnet_event_dispatches_to_sonnet_model(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(model_demand="SONNET"))
        create_fn = make_create_fn(FakeResponse(valid_analysis("BTC")))
        claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)
        self.assertEqual(create_fn.calls[0]["model"], config.ANTHROPIC_SONNET_MODEL)

    def test_ignore_demand_events_never_reach_the_bridge(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(model_demand="IGNORE", status="PENDING"))
        calls = []
        create_fn = make_create_fn(FakeResponse(valid_analysis("BTC")))
        claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)
        self.assertEqual(create_fn.calls, [])
        # An IGNORE event is never even a realistic case from heartbeat.py,
        # but the bridge must not touch it if it somehow appears.
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "PENDING")

    def test_provider_unavailable_keeps_event_actionable_as_deferred(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        create_fn = make_create_fn(ConnectionError("network down"))
        result = claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "DEFERRED")
        self.assertEqual(row["attempts"], 1)
        self.assertIsNotNone(row["next_attempt_at"])
        self.assertEqual(result.health, "OFFLINE")

    def test_timeout_defers_event(self):
        class FakeTimeout(Exception):
            pass

        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())

        def create_fn(*a, **k):
            raise TimeoutError("simulated timeout")

        # classify_exception falls back to PROVIDER_UNAVAILABLE without the
        # anthropic package installed, which is itself a DEFERRED-mapped
        # status - the key behaviour under test is "never FAILED, never lost".
        result = claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)
        row = self.store.get_event(event_id)
        self.assertIn(row["status"], ("DEFERRED",))

    def test_quota_exhausted_never_marked_no_trade_stays_deferred(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())

        class FakeQuotaError(Exception):
            status_code = 402
            type = "billing_error"

        # Force classify_exception to see this as billing_error by monkeypatching.
        original = claude_bridge.classify_exception
        claude_bridge.classify_exception = lambda exc: "QUOTA_EXHAUSTED"
        try:
            create_fn = make_create_fn(FakeQuotaError("no credits"))
            result = claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)
        finally:
            claude_bridge.classify_exception = original

        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "DEFERRED")
        self.assertEqual(result.health, "QUOTA_EXHAUSTED")

    def test_auth_error_marks_failed_not_deferred(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        original = claude_bridge.classify_exception
        claude_bridge.classify_exception = lambda exc: "AUTH_ERROR"
        try:
            create_fn = make_create_fn(RuntimeError("bad api key"))
            result = claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)
        finally:
            claude_bridge.classify_exception = original

        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(result.health, "AUTH_ERROR")

    def test_invalid_response_eventually_fails_after_max_attempts(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        bad_response = FakeResponse({"asset": "BTC"})  # missing required fields -> INVALID_RESPONSE
        create_fn = make_create_fn(bad_response)

        now = T0
        for _ in range(config.CLAUDE_BRIDGE_MAX_ATTEMPTS_BEFORE_FAILED):
            claude_bridge.run_bridge_cycle(self.store, now=now, create_fn=create_fn)
            row = self.store.get_event(event_id)
            if row["status"] == "FAILED":
                break
            # jump time forward past the backoff window so the DEFERRED event
            # becomes actionable again on the next cycle
            now = now + timedelta(hours=2)

        self.assertEqual(row["status"], "FAILED")

    def test_idempotency_processed_event_never_recalled(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        create_fn = make_create_fn(FakeResponse(valid_analysis("BTC")))
        claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)
        self.assertEqual(len(create_fn.calls), 1)

        # Even if something re-marks it PENDING (should never happen, but
        # has_successful_analysis is the idempotency backstop), the model
        # must never be billed again for the same event_id.
        self.store.update_event_status(event_id, "PENDING")
        claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)
        self.assertEqual(len(create_fn.calls), 1)
        self.assertEqual(self.store.get_event(event_id)["status"], "PROCESSED")

    def test_budget_exhausted_defers_never_fails(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(model_demand="FABLE"))
        # Exhaust FABLE's hourly budget before the bridge ever runs.
        limit = config.MODEL_BUDGETS["FABLE"]["hourly"]
        for _ in range(limit):
            self.store.increment_budget("FABLE", "hour", T0.strftime("%Y-%m-%dT%H:00:00"))
            self.store.increment_budget("FABLE", "day", T0.strftime("%Y-%m-%d"))

        create_fn = make_create_fn(FakeResponse(valid_analysis("BTC")))
        claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)

        self.assertEqual(create_fn.calls, [])  # never called the model at all
        row = self.store.get_event(event_id)
        self.assertEqual(row["status"], "DEFERRED")
        self.assertEqual(row["last_error"], "budget_exhausted")

    def test_stale_processing_is_recovered_before_dispatch(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        stale_ts = (T0 - timedelta(seconds=config.CLAUDE_BRIDGE_PROCESSING_STALE_SECONDS + 60)).isoformat()
        self.store.claim_event_for_processing(event_id, stale_ts)

        create_fn = make_create_fn(FakeResponse(valid_analysis("BTC")))
        result = claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)

        self.assertEqual(result.recovered_stale, 1)
        self.assertEqual(self.store.get_event(event_id)["status"], "PROCESSED")

    def test_max_events_per_cycle_is_respected(self):
        import re

        for i in range(5):
            create_event_if_new(
                self.store,
                **make_event_kwargs(asset=f"A{i}", ts=(T0 + timedelta(seconds=i)).isoformat(), context={"asset": f"A{i}"}),
            )
        seen = []

        def create_fn_dynamic(model, max_tokens, system, user_content, schema):
            seen.append(user_content)
            asset = re.search(r'"asset":\s*"([^"]+)"', user_content).group(1)
            return FakeResponse(valid_analysis(asset))

        result = claude_bridge.run_bridge_cycle(self.store, now=T0, max_events=2, create_fn=create_fn_dynamic)
        self.assertEqual(len(seen), 2)
        counts = self.store.event_status_counts()
        self.assertEqual(counts["PROCESSED"], 2)
        self.assertEqual(counts["PENDING"], 3)

    def test_loop_like_repeated_calls_do_not_duplicate_processing(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        create_fn = make_create_fn(FakeResponse(valid_analysis("BTC")))
        for _ in range(5):
            claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)
        self.assertEqual(len(create_fn.calls), 1)
        self.assertEqual(self.store.get_event(event_id)["status"], "PROCESSED")


class TestBridgeHealthDetection(BridgeCycleTestCase):
    @mock.patch.object(claude_bridge, "_anthropic_available", return_value=False)
    def test_no_sdk_and_no_events_reports_offline_without_crashing(self, _available):
        result = claude_bridge.run_bridge_cycle(self.store, now=T0, env={})
        self.assertEqual(result.health, "OFFLINE")
        self.assertEqual(result.skipped_reason, "NO_SDK")

    @mock.patch.object(claude_bridge, "_anthropic_available", return_value=True)
    def test_sdk_without_explicit_credentials_reports_auth_error(self, _available):
        result = claude_bridge.run_bridge_cycle(
            self.store,
            now=T0,
            env={"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""},
        )
        self.assertEqual(result.health, "AUTH_ERROR")
        self.assertEqual(result.skipped_reason, "NO_CREDENTIALS")

    def test_health_persists_across_bridge_health_lookup(self):
        create_fn = make_create_fn(FakeResponse(valid_analysis("BTC")))
        create_event_if_new(self.store, **make_event_kwargs())
        claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=create_fn)
        self.assertEqual(claude_bridge.bridge_health_label(self.store), "ONLINE")

    def test_unknown_before_first_cycle(self):
        self.assertEqual(claude_bridge.bridge_health_label(self.store), "UNKNOWN")


class TestSecurityBoundaries(unittest.TestCase):
    """The Bridge is allowed to *document*, in comments/docstrings, that it
    has no Kraken access (that's the point) - what must never appear is an
    actual import of a Kraken module, a private-endpoint string, or a
    trading verb. Checked structurally, not by banning the word "Kraken".
    """

    def test_bridge_never_imports_trading_endpoints(self):
        import radar_v08.claude_bridge as m
        with open(m.__file__, encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in ("import kraken_spot", "import kraken_futures", "/private/", "newOrder", "cancelOrder"):
            self.assertNotIn(forbidden, source)

    def test_context_builder_never_imports_trading_endpoints(self):
        import radar_v08.context_builder as m
        with open(m.__file__, encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in ("import kraken_spot", "import kraken_futures", "/private/", "newOrder", "cancelOrder"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
