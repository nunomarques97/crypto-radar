import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import budgets, claude_bridge, config
from radar_v08.domain.integrity import InstrumentKind
from radar_v08.domain.invocation import Direction, InvocationIdentity, InvocationRequest
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore

T0 = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


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
        for suffix in ("", "-wal", "-shm", ".events.jsonl"):
            candidate = self.path + suffix
            if os.path.exists(candidate):
                os.remove(candidate)

    def event_snapshot(self, event_id):
        return dict(self.store.get_event(event_id))

    def assert_disabled_cycle_is_read_only(self, event_ids, *, create_fn=None, notify_fn=None, **kwargs):
        before = {event_id: self.event_snapshot(event_id) for event_id in event_ids}
        before_analyses = {
            event_id: [dict(row) for row in self.store.get_model_analyses_for_event(event_id)]
            for event_id in event_ids
        }
        result = claude_bridge.run_bridge_cycle(
            self.store,
            now=T0,
            create_fn=create_fn,
            notify_fn=notify_fn,
            **kwargs,
        )
        self.assertEqual(result.health, "DISABLED")
        self.assertEqual(result.skipped_reason, "LOCAL_ONLY_POLICY")
        self.assertEqual(result.processed, [])
        for event_id in event_ids:
            self.assertEqual(self.event_snapshot(event_id), before[event_id])
            self.assertEqual(
                [dict(row) for row in self.store.get_model_analyses_for_event(event_id)],
                before_analyses[event_id],
            )
        if create_fn is not None:
            self.assertFalse(create_fn.called)
        if notify_fn is not None:
            self.assertFalse(notify_fn.called)
        return result


class TestCallModelContainment(unittest.TestCase):
    def assert_direct_call_is_disabled(self, demand, context):
        fake_client = mock.Mock(side_effect=AssertionError("cloud client must not run"))
        with mock.patch.object(claude_bridge, "build_user_message") as prompt_builder, mock.patch.object(
            claude_bridge, "_anthropic_available", return_value=True
        ) as available:
            result = claude_bridge.call_model(demand, context, create_fn=fake_client)
        self.assertEqual(result.status, "DISABLED")
        self.assertEqual(result.error, config.CLAUDE_BRIDGE_DISABLED_REASON)
        self.assertFalse(fake_client.called)
        self.assertFalse(prompt_builder.called)
        self.assertFalse(available.called)

    def test_successful_sonnet_dispatch_is_replaced_by_disabled_result(self):
        self.assert_direct_call_is_disabled("SONNET", {"asset": "BTC"})

    def test_successful_fable_dispatch_is_replaced_by_disabled_result(self):
        self.assert_direct_call_is_disabled("FABLE", {"asset": "ETH"})

    def test_invalid_model_demand_raises(self):
        for demand in ("IGNORE", "OPUS"):
            with self.subTest(demand=demand):
                with self.assertRaises(ValueError):
                    claude_bridge.call_model(demand, {"asset": "BTC"})

    def test_asset_mismatch_response_cannot_reach_a_client(self):
        self.assert_direct_call_is_disabled("SONNET", {"asset": "BTC", "response_asset": "NOTINPUT"})

    def test_malformed_json_response_cannot_reach_a_client(self):
        self.assert_direct_call_is_disabled("SONNET", {"asset": "BTC", "response": "not json {{{"})

    def test_bad_enum_response_cannot_reach_a_client(self):
        self.assert_direct_call_is_disabled("SONNET", {"asset": "BTC", "recommendation": "BUY_NOW"})

    def test_no_secret_or_prompt_content_can_leave_the_disabled_boundary(self):
        fake_client = mock.Mock(side_effect=AssertionError("cloud client must not run"))
        with mock.patch.object(claude_bridge, "build_user_message") as prompt_builder, mock.patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "present", "ANTHROPIC_AUTH_TOKEN": "present"}, clear=False
        ):
            result = claude_bridge.call_model(
                "SONNET",
                {"asset": "BTC", "KRAKEN_API_KEY": "test-only-secret"},
                create_fn=fake_client,
            )
        self.assertEqual(result.status, "DISABLED")
        self.assertFalse(fake_client.called)
        self.assertFalse(prompt_builder.called)


class TestClassifyException(unittest.TestCase):
    def setUp(self):
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
        self._original = sys.modules.get("anthropic")
        sys.modules["anthropic"] = fake

    def tearDown(self):
        if self._original is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self._original

    def test_auth_error(self):
        self.assertEqual(
            claude_bridge.classify_exception(self.fake_anthropic.AuthenticationError("bad key", status_code=401)),
            "AUTH_ERROR",
        )

    def test_permission_denied_is_auth_error(self):
        self.assertEqual(
            claude_bridge.classify_exception(self.fake_anthropic.PermissionDeniedError("forbidden", status_code=403)),
            "AUTH_ERROR",
        )

    def test_rate_limit(self):
        self.assertEqual(
            claude_bridge.classify_exception(self.fake_anthropic.RateLimitError("slow down", status_code=429)),
            "RATE_LIMITED",
        )

    def test_timeout(self):
        self.assertEqual(claude_bridge.classify_exception(self.fake_anthropic.APITimeoutError("timed out")), "TIMEOUT")

    def test_billing_error_is_quota_exhausted(self):
        self.assertEqual(
            claude_bridge.classify_exception(
                self.fake_anthropic.APIStatusError("no credits", status_code=402, type="billing_error")
            ),
            "QUOTA_EXHAUSTED",
        )

    def test_overloaded_is_provider_unavailable(self):
        self.assertEqual(
            claude_bridge.classify_exception(
                self.fake_anthropic.APIStatusError("overloaded", status_code=529, type="overloaded_error")
            ),
            "PROVIDER_UNAVAILABLE",
        )

    def test_five_hundred_is_provider_unavailable(self):
        self.assertEqual(
            claude_bridge.classify_exception(self.fake_anthropic.APIStatusError("server error", status_code=500)),
            "PROVIDER_UNAVAILABLE",
        )

    def test_connection_error_is_provider_unavailable(self):
        self.assertEqual(
            claude_bridge.classify_exception(self.fake_anthropic.APIConnectionError("network down")),
            "PROVIDER_UNAVAILABLE",
        )

    def test_bad_request_is_invalid_response(self):
        self.assertEqual(
            claude_bridge.classify_exception(self.fake_anthropic.APIStatusError("bad schema", status_code=400)),
            "INVALID_RESPONSE",
        )

    def test_call_model_maps_a_would_be_provider_error_to_disabled_without_calling(self):
        fake_client = mock.Mock(
            side_effect=self.fake_anthropic.RateLimitError("slow down", status_code=429)
        )
        result = claude_bridge.call_model("FABLE", {"asset": "BTC"}, create_fn=fake_client)
        self.assertEqual(result.status, "DISABLED")
        self.assertIsNone(result.parsed)
        self.assertFalse(fake_client.called)


class TestRunBridgeCycleContainment(BridgeCycleTestCase):
    def test_pending_fable_event_is_not_processed_or_notified(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.assert_disabled_cycle_is_read_only(
            [event_id],
            create_fn=mock.Mock(side_effect=AssertionError("cloud client must not run")),
            notify_fn=mock.Mock(side_effect=AssertionError("notification must not run")),
        )

    def test_sonnet_event_never_reaches_the_sonnet_model(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(model_demand="SONNET"))
        self.assert_disabled_cycle_is_read_only(
            [event_id], create_fn=mock.Mock(side_effect=AssertionError("cloud client must not run"))
        )

    def test_ignore_demand_events_never_reach_the_bridge(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(model_demand="IGNORE", status="PENDING"))
        self.assert_disabled_cycle_is_read_only(
            [event_id], create_fn=mock.Mock(side_effect=AssertionError("cloud client must not run"))
        )

    def test_provider_unavailable_path_does_not_defer_or_schedule_a_retry(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.assert_disabled_cycle_is_read_only(
            [event_id], create_fn=mock.Mock(side_effect=ConnectionError("network down"))
        )

    def test_timeout_path_does_not_defer_or_schedule_a_retry(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.assert_disabled_cycle_is_read_only(
            [event_id], create_fn=mock.Mock(side_effect=TimeoutError("simulated timeout"))
        )

    def test_quota_path_does_not_defer_or_schedule_a_retry(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.assert_disabled_cycle_is_read_only(
            [event_id], create_fn=mock.Mock(side_effect=RuntimeError("no credits"))
        )

    def test_auth_path_does_not_fail_or_mutate_event(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.assert_disabled_cycle_is_read_only(
            [event_id], create_fn=mock.Mock(side_effect=RuntimeError("bad api key"))
        )

    def test_invalid_response_path_cannot_insert_an_analysis(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.assert_disabled_cycle_is_read_only(
            [event_id], create_fn=mock.Mock(return_value={"asset": "BTC"})
        )

    def test_previously_analyzed_pending_event_is_not_recalled_or_reclassified(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.store.insert_model_analysis(
            event_id=event_id, model=config.CLAUDE_BRIDGE_MODEL_IDS["FABLE"], model_version="bridge_prompt_v1",
            requested_at=T0.isoformat(), completed_at=T0.isoformat(), status="SUCCESS", response="{}",
            parsed_output_json="{}", latency_ms=1.0, input_tokens=1, output_tokens=1, error=None,
        )
        before_analysis = [dict(row) for row in self.store.get_model_analyses_for_event(event_id)]
        self.assert_disabled_cycle_is_read_only(
            [event_id], create_fn=mock.Mock(side_effect=AssertionError("cloud client must not run"))
        )
        self.assertEqual([dict(row) for row in self.store.get_model_analyses_for_event(event_id)], before_analysis)

    def test_budget_exhausted_event_is_not_deferred_or_charged(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(model_demand="FABLE"))
        # The budget is the invocation reservation counter (the legacy
        # model_budget_usage table is no longer charged), so exhaust that one.
        for index in range(config.MODEL_BUDGETS["FABLE"]["hourly"]):
            identity = InvocationIdentity(
                venue="kraken", market_kind=InstrumentKind.SPOT, native_instrument="XBT/USD", setup="BREAKOUT",
                direction=Direction.LONG, evidence_hash="sha256:" + f"{index:064x}", policy_version="OC-1/test",
            )
            self.store.claim_invocation(
                InvocationRequest(identity, "FABLE"), budgets.model_budget("FABLE"), "other-holder", 600, now=T0
            )
        before_budget = budgets.budget_status(self.store, "FABLE", T0)
        self.assertEqual(before_budget["hourly_used"], before_budget["hourly_limit"])
        self.assert_disabled_cycle_is_read_only(
            [event_id], create_fn=mock.Mock(side_effect=AssertionError("cloud client must not run"))
        )
        self.assertEqual(budgets.budget_status(self.store, "FABLE", T0), before_budget)

    def test_stale_processing_is_not_recovered_or_dispatched(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        stale_ts = (T0 - timedelta(seconds=config.CLAUDE_BRIDGE_PROCESSING_STALE_SECONDS + 60)).isoformat()
        self.store.claim_event_for_processing(event_id, stale_ts)
        result = self.assert_disabled_cycle_is_read_only(
            [event_id], create_fn=mock.Mock(side_effect=AssertionError("cloud client must not run"))
        )
        self.assertEqual(result.recovered_stale, 0)

    def test_max_events_per_cycle_does_not_mutate_any_candidate(self):
        event_ids = []
        for index in range(5):
            event_id, _ = create_event_if_new(
                self.store,
                **make_event_kwargs(
                    asset=f"A{index}", ts=(T0 + timedelta(seconds=index)).isoformat(), context={"asset": f"A{index}"}
                ),
            )
            event_ids.append(event_id)
        self.assert_disabled_cycle_is_read_only(
            event_ids,
            max_events=2,
            create_fn=mock.Mock(side_effect=AssertionError("cloud client must not run")),
        )

    def test_loop_like_repeated_calls_do_not_mutate_queue_or_dispatch(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        fake_client = mock.Mock(side_effect=AssertionError("cloud client must not run"))
        before = self.event_snapshot(event_id)
        for _ in range(5):
            result = claude_bridge.run_bridge_cycle(self.store, now=T0, create_fn=fake_client)
            self.assertEqual(result.health, "DISABLED")
            self.assertEqual(result.skipped_reason, "LOCAL_ONLY_POLICY")
        self.assertEqual(self.event_snapshot(event_id), before)
        self.assertFalse(fake_client.called)
        self.assertEqual(self.store.get_model_analyses_for_event(event_id), [])


class TestBridgeHealthDetection(BridgeCycleTestCase):
    def test_no_sdk_and_no_events_reports_disabled_without_sdk_probe(self):
        with mock.patch.object(claude_bridge, "_anthropic_available", return_value=False) as available:
            result = claude_bridge.run_bridge_cycle(self.store, now=T0, env={})
        self.assertEqual(result.health, "DISABLED")
        self.assertEqual(result.skipped_reason, "LOCAL_ONLY_POLICY")
        self.assertFalse(available.called)

    def test_sdk_and_credentials_present_still_report_disabled_without_sdk_probe(self):
        with mock.patch.object(claude_bridge, "_anthropic_available", return_value=True) as available:
            result = claude_bridge.run_bridge_cycle(
                self.store,
                now=T0,
                env={"ANTHROPIC_API_KEY": "present", "ANTHROPIC_AUTH_TOKEN": "present"},
            )
        self.assertEqual(result.health, "DISABLED")
        self.assertEqual(result.skipped_reason, "LOCAL_ONLY_POLICY")
        self.assertFalse(available.called)

    def test_health_and_stored_analysis_remain_readable(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs())
        self.store.insert_model_analysis(
            event_id=event_id, model=config.CLAUDE_BRIDGE_MODEL_IDS["FABLE"], model_version="bridge_prompt_v1",
            requested_at=T0.isoformat(), completed_at=T0.isoformat(), status="SUCCESS", response='{"asset":"BTC"}',
            parsed_output_json='{"asset":"BTC"}', latency_ms=1.0, input_tokens=1, output_tokens=1, error=None,
        )
        self.store.set_bridge_health("ONLINE", "legacy record", T0.isoformat())
        self.assertEqual(claude_bridge.bridge_health_label(self.store), "ONLINE")
        self.assertEqual(len(self.store.get_model_analyses_for_event(event_id)), 1)

    def test_unknown_before_first_cycle_remains_readable(self):
        self.assertEqual(claude_bridge.bridge_health_label(self.store), "UNKNOWN")


class TestSecurityBoundaries(unittest.TestCase):
    def test_default_create_blocks_before_constructing_anthropic_client(self):
        fake_sdk = types.ModuleType("anthropic")
        fake_sdk.Anthropic = mock.Mock(side_effect=AssertionError("SDK client must not be constructed"))
        with mock.patch.dict(sys.modules, {"anthropic": fake_sdk}):
            with self.assertRaisesRegex(RuntimeError, "local-only"):
                claude_bridge._default_create("legacy", 1, "system", "context", {})
        self.assertFalse(fake_sdk.Anthropic.called)

    def test_bridge_never_imports_trading_endpoints(self):
        with open(claude_bridge.__file__, encoding="utf-8") as file_handle:
            source = file_handle.read()
        for forbidden in ("import kraken_spot", "import kraken_futures", "/private/", "newOrder", "cancelOrder"):
            self.assertNotIn(forbidden, source)

    def test_context_builder_never_imports_trading_endpoints(self):
        import radar_v08.context_builder as context_builder

        with open(context_builder.__file__, encoding="utf-8") as file_handle:
            source = file_handle.read()
        for forbidden in ("import kraken_spot", "import kraken_futures", "/private/", "newOrder", "cancelOrder"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
