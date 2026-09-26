"""Heartbeat and bridge use the atomic claim + reservation.

Regression: the heartbeat charged budget and started the cooldown before
its dedup, and the bridge charged the budget again before its claim, so one
opportunity cost 2 units. These tests run the real heartbeat (`run_heartbeat`
over the fake Kraken of `test_integrity_wiring`: no socket, fake Qwen, temporary
SQLite and log paths) and the real bridge cycle. The bridge's cloud-dispatch containment is
lifted only inside each test by patching `_dispatch_is_disabled`, and every model
call goes to a fake `create_fn`: nothing reaches a network or a cloud client.
"""

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from datetime import timedelta
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TESTS_DIR))
sys.path.insert(0, TESTS_DIR)

import test_integrity_wiring as wiring  # noqa: E402  (fake Kraken heartbeat harness)

from radar_v08 import budgets, claude_bridge, config  # noqa: E402
from radar_v08.domain.integrity import InstrumentKind  # noqa: E402
from radar_v08.domain.invocation import (  # noqa: E402
    Claimed,
    Direction,
    InvocationError,
    InvocationFailure,
    InvocationIdentity,
    InvocationRequest,
    TransitionStatus,
)
from radar_v08.events import create_event_if_new  # noqa: E402
from radar_v08.store import SnapshotStore  # noqa: E402

UTC = wiring.UTC
BRIDGE_NOW = wiring.T0 + timedelta(minutes=1)
LEASE = claude_bridge._lease_seconds()


def analysis_text(asset):
    return json.dumps(
        {
            "asset": asset, "market": "FUTURES", "direction": "LONG", "setup_type": "BREAKOUT",
            "thesis": "fake analysis for a test", "entry": None, "entry_range": None, "stop": None,
            "tp1": None, "tp2": None, "leverage": None, "margin": None, "notional": None, "max_loss": None,
            "expected_profit": None, "net_rr": None, "confidence": "LOW", "risks": [], "invalidation": "n/a",
            "alternatives": [], "capital_status": "n/a", "recommendation": "WAIT", "reasoning_summary": "n/a",
        }
    )


class FakeModel:
    """Stands in for the provider client: counts calls, never touches a network."""

    def __init__(self, fail_with=None, on_call=None):
        self.calls = []
        self.fail_with = fail_with
        self.on_call = on_call
        self._lock = threading.Lock()

    def __call__(self, model, max_tokens, system, user_content, schema):
        with self._lock:
            self.calls.append(model)
        if self.on_call is not None:
            self.on_call()
        if self.fail_with is not None:
            raise self.fail_with
        asset = re.search(r'"asset": "([^"]+)"', user_content).group(1)
        return types.SimpleNamespace(content=[{"type": "text", "text": analysis_text(asset)}], usage=None)


def dispatch_enabled():
    """Test-only: lift the cloud-dispatch guard so the cycle reaches the fake create_fn."""
    return mock.patch.object(claude_bridge, "_dispatch_is_disabled", return_value=False)


def other_identity(index):
    return InvocationIdentity(
        venue="kraken", market_kind=InstrumentKind.SPOT, native_instrument="OTHER/USD", setup="BREAKOUT",
        direction=Direction.LONG, evidence_hash="sha256:" + f"{index:064x}", policy_version="OC-1/test",
    )


class LedgerAssertions:
    """Reads the counters straight from the temporary database."""

    def rows(self, sql, *args):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, args)]

    def reserved(self, model, now=BRIDGE_NOW):
        status = budgets.budget_status(self.store, model, now)
        return status["hourly_used"], status["daily_used"]

    def legacy_budget_rows(self):
        return self.rows("SELECT * FROM model_budget_usage")

    def invocations(self):
        return self.rows("SELECT * FROM invocations ORDER BY claimed_at, invocation_id")

    def demand(self, model):
        return {
            row["window_kind"]: (row["observed"], row["refused"])
            for row in self.rows("SELECT * FROM invocation_demand WHERE model = ?", model)
        }

    def cooldown_row(self, asset, model):
        row = self.store.get_cooldown(asset, model)
        return dict(row) if row is not None else None

    def analyses(self, event_id):
        return [dict(row) for row in self.store.get_model_analyses_for_event(event_id)]

    def fill_budget(self, model, now=BRIDGE_NOW):
        """Reserve every unit of the hour with other identities held by another owner."""
        budget = budgets.model_budget(model)
        for index in range(budget.hourly_limit):
            result = self.store.claim_invocation(
                InvocationRequest(other_identity(index), model), budget, "other-holder", LEASE, now=now
            )
            self.assertIsInstance(result, Claimed)


class TestHeartbeatAndBridgeChargeOnce(wiring.IntegrityWiringBase, LedgerAssertions):
    """One opportunity through the real heartbeat and the real bridge cycle."""

    def heartbeat(self, clock=None):
        return self.run_cycle(wiring.FakeKraken(assets=("BTC",)), clock=clock)

    def only_event(self):
        events = self.events()
        self.assertEqual(len(events), 1)
        return events[0]

    def test_one_opportunity_costs_exactly_one_budget_unit_per_genuine_call(self):
        self.heartbeat()
        event = self.only_event()
        self.assertEqual((event["asset"], event["model_demand"], event["status"]), ("BTC", "SONNET", "PENDING"))
        # The heartbeat only recorded demand: nothing charged, no cooldown, no invocation.
        self.assertEqual(self.reserved("SONNET"), (0, 0))
        self.assertEqual(self.legacy_budget_rows(), [])
        self.assertIsNone(self.cooldown_row("BTC", "SONNET"))
        self.assertEqual(self.invocations(), [])

        # Cloud-dispatch containment is intact without the test patch: nothing is charged.
        contained = claude_bridge.run_bridge_cycle(self.store, now=BRIDGE_NOW, create_fn=FakeModel())
        self.assertEqual(contained.health, "DISABLED")
        self.assertEqual(self.reserved("SONNET"), (0, 0))

        model, notify = FakeModel(), mock.Mock()
        with dispatch_enabled():
            result = claude_bridge.run_bridge_cycle(self.store, now=BRIDGE_NOW, create_fn=model, notify_fn=notify)

        self.assertEqual(len(model.calls), 1)
        self.assertEqual([p["outcome"] for p in result.processed], ["PROCESSED"])
        self.assertEqual(self.only_event()["status"], "PROCESSED")
        # Exactly 1 unit for 1 genuine call (the old code charged 2: heartbeat + bridge).
        self.assertEqual(self.reserved("SONNET"), (1, 1))
        self.assertEqual(self.legacy_budget_rows(), [])
        invocations = self.invocations()
        self.assertEqual(len(invocations), 1)
        self.assertEqual(
            (invocations[0]["state"], invocations[0]["attempt_count"], invocations[0]["generation"]),
            ("COMPLETED", 1, 1),
        )
        self.assertEqual(invocations[0]["native_instrument"], "PF_XBTUSD")
        self.assertEqual(invocations[0]["market_kind"], "futures")
        # The cooldown starts at the accepted claim, not in the heartbeat.
        self.assertEqual(self.cooldown_row("BTC", "SONNET")["last_sent_ts"], BRIDGE_NOW.isoformat())
        notify.assert_called_once()

        # A later cycle finds nothing to do and charges nothing more.
        with dispatch_enabled():
            claude_bridge.run_bridge_cycle(self.store, now=BRIDGE_NOW + timedelta(minutes=5), create_fn=model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.reserved("SONNET"), (1, 1))

    def test_dedup_hit_costs_nothing_and_leaves_cooldown_unchanged(self):
        # An old, expired cooldown lets the heartbeat through; it must stay byte-identical.
        old_sent = wiring.T0 - timedelta(hours=config.COOLDOWN_HOURS + 1)
        self.store.set_cooldown("BTC", "SONNET", old_sent.isoformat(), "BREAKOUT", "LONG", 80.0)
        before_cooldown = self.cooldown_row("BTC", "SONNET")

        self.heartbeat()
        event = self.only_event()
        second = self.heartbeat()

        self.assertEqual(self.only_event()["event_id"], event["event_id"])  # deduplicated, no second event
        candidate = second["candidates"][0]
        self.assertEqual(candidate["event_id"], event["event_id"])
        self.assertEqual(candidate["event_status"], "PENDING")
        self.assertIn("deduplicated_open_event", candidate["router_reasons"])
        self.assertEqual(self.reserved("SONNET"), (0, 0))
        self.assertEqual(self.legacy_budget_rows(), [])
        self.assertEqual(self.demand("SONNET"), {})
        self.assertEqual(self.invocations(), [])
        self.assertEqual(self.cooldown_row("BTC", "SONNET"), before_cooldown)

    def test_exhausted_budget_defers_the_event_without_cooldown_or_charge(self):
        self.heartbeat()
        event = self.only_event()
        self.fill_budget("SONNET")
        limit = budgets.model_budget("SONNET").hourly_limit
        model, notify = FakeModel(), mock.Mock()

        with dispatch_enabled():
            result = claude_bridge.run_bridge_cycle(self.store, now=BRIDGE_NOW, create_fn=model, notify_fn=notify)

        row = self.only_event()
        self.assertEqual(row["status"], "DEFERRED")
        self.assertEqual(row["last_error"], "budget_exhausted: hourly_budget_exhausted")
        self.assertIsNotNone(row["next_attempt_at"])
        self.assertEqual(result.processed[0]["outcome"], "DEFERRED")
        self.assertEqual(model.calls, [])
        notify.assert_not_called()
        self.assertEqual(self.reserved("SONNET"), (limit, limit))  # unchanged by the refusal
        self.assertEqual(self.demand("SONNET")["hour"], (limit + 1, 1))  # demand seen, refusal counted
        self.assertIsNone(self.cooldown_row("BTC", "SONNET"))
        self.assertEqual(len(self.invocations()), limit)  # only the other holder's rows

        # The next heartbeat deduplicates onto the DEFERRED event: still nothing charged or started.
        second = self.heartbeat()
        self.assertEqual(second["candidates"][0]["event_status"], "DEFERRED")
        self.assertEqual(self.only_event()["event_id"], event["event_id"])
        self.assertEqual(self.reserved("SONNET"), (limit, limit))
        self.assertIsNone(self.cooldown_row("BTC", "SONNET"))

    def test_event_claim_lost_to_another_connection_is_not_charged(self):
        self.heartbeat()
        event = self.only_event()
        other = SnapshotStore(self.db_path)
        self.addCleanup(other.close)
        original = self.store.find_actionable_events

        def read_then_lose_the_race(now_iso, limit):
            rows = original(now_iso, limit)
            self.assertTrue(other.claim_event_for_processing(event["event_id"], BRIDGE_NOW.isoformat()))
            return rows

        model = FakeModel()
        with dispatch_enabled(), mock.patch.object(self.store, "find_actionable_events", read_then_lose_the_race):
            result = claude_bridge.run_bridge_cycle(self.store, now=BRIDGE_NOW, create_fn=model)

        self.assertEqual(result.processed, [])
        self.assertEqual(model.calls, [])
        self.assertEqual(self.reserved("SONNET"), (0, 0))
        self.assertEqual(self.demand("SONNET"), {})
        self.assertEqual(self.invocations(), [])
        self.assertIsNone(self.cooldown_row("BTC", "SONNET"))

    def test_invocation_held_by_another_connection_is_not_charged(self):
        self.heartbeat()
        event = self.only_event()
        other = SnapshotStore(self.db_path)
        self.addCleanup(other.close)
        request = claude_bridge.invocation_request_for_event(self.store.get_event(event["event_id"]))
        held = other.claim_invocation(request, budgets.model_budget("SONNET"), "other-holder", LEASE, now=BRIDGE_NOW)
        self.assertIsInstance(held, Claimed)

        model = FakeModel()
        with dispatch_enabled():
            result = claude_bridge.run_bridge_cycle(self.store, now=BRIDGE_NOW, create_fn=model)

        self.assertEqual(result.processed[0]["reason"], "invocation_active_elsewhere")
        self.assertEqual(self.only_event()["status"], "DEFERRED")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.reserved("SONNET"), (1, 1))  # the other holder's unit only
        self.assertIsNone(self.cooldown_row("BTC", "SONNET"))


class BridgeStoreCase(unittest.TestCase, LedgerAssertions):
    """A disposable store with hand-made events (no heartbeat needed)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crypto-radar-t031b-")
        self.db_path = os.path.join(self.tmp, "state.sqlite")
        self.store = SnapshotStore(self.db_path)
        patcher = mock.patch.object(config, "EVENTS_LOG_PATH", os.path.join(self.tmp, "events.jsonl"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_event(self, asset="BTC", market="FUTURES", **context_overrides):
        context = {"asset": asset, "market": market, "spot_pair": f"{asset}/USD", "futures_symbol": f"PF_{asset}USD"}
        context.update(context_overrides)
        event_id, created = create_event_if_new(
            self.store, ts=wiring.T0.isoformat(), type_="RADAR_ALERT", asset=asset, setup_type="BREAKOUT",
            direction="LONG", market=market, anomaly_score=70.0, opportunity_score=80.0, tradeability_score=85.0,
            confidence="HIGH", model_demand="SONNET", reason="test", status="PENDING", context=context,
        )
        self.assertTrue(created)
        return event_id

    def event(self, event_id):
        return dict(self.store.get_event(event_id))


class TestRaceRetryAndFence(BridgeStoreCase):
    def test_two_connections_race_for_one_event_one_call_one_unit(self):
        for round_index in range(4):
            with self.subTest(round=round_index):
                event_id = self.make_event(asset=f"R{round_index}")
                stores = [self.store, SnapshotStore(self.db_path)]
                self.addCleanup(stores[1].close)
                barrier = threading.Barrier(2)
                models = [FakeModel(), FakeModel()]
                errors = []

                def run(index):
                    store = stores[index]
                    original = store.find_actionable_events

                    def both_read_before_either_claims(now_iso, limit):
                        rows = original(now_iso, limit)
                        barrier.wait(timeout=10)
                        return rows

                    try:
                        with mock.patch.object(store, "find_actionable_events", both_read_before_either_claims):
                            claude_bridge.run_bridge_cycle(store, now=BRIDGE_NOW, create_fn=models[index])
                    except BaseException as exc:  # surfaced below
                        errors.append(exc)

                with dispatch_enabled():
                    threads = [threading.Thread(target=run, args=(index,)) for index in (0, 1)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=30)

                self.assertEqual(errors, [])
                self.assertEqual(len(models[0].calls) + len(models[1].calls), 1)
                self.assertEqual(self.event(event_id)["status"], "PROCESSED")
                self.assertEqual(len(self.analyses(event_id)), 1)
                self.assertEqual(self.reserved("SONNET"), (round_index + 1, round_index + 1))

    def test_retry_after_a_failed_call_is_a_new_claim_and_costs_one_more_unit(self):
        event_id = self.make_event()
        failing, notify = FakeModel(fail_with=ConnectionError("provider down: detail text")), mock.Mock()
        with dispatch_enabled():
            first = claude_bridge.run_bridge_cycle(self.store, now=BRIDGE_NOW, create_fn=failing, notify_fn=notify)
        self.assertEqual(first.processed[0]["outcome"], "DEFERRED")
        self.assertEqual(self.event(event_id)["status"], "DEFERRED")
        self.assertEqual([a["status"] for a in self.analyses(event_id)], ["PROVIDER_UNAVAILABLE"])
        self.assertEqual(self.reserved("SONNET"), (1, 1))
        self.assertEqual([(i["state"], i["attempt_count"]) for i in self.invocations()], [("RELEASED", 1)])
        notify.assert_not_called()  # a failure is never notified

        later = BRIDGE_NOW + timedelta(minutes=2)  # past the first backoff (60 s)
        working = FakeModel()
        with dispatch_enabled():
            second = claude_bridge.run_bridge_cycle(self.store, now=later, create_fn=working, notify_fn=notify)
        self.assertEqual(second.processed[0]["outcome"], "PROCESSED")
        self.assertEqual(self.event(event_id)["status"], "PROCESSED")
        # Two genuine calls, two units, two invocations of one attempt each.
        self.assertEqual(len(failing.calls) + len(working.calls), 2)
        self.assertEqual(self.reserved("SONNET", later), (2, 2))
        self.assertEqual(
            [(i["state"], i["attempt_count"]) for i in self.invocations()], [("RELEASED", 1), ("COMPLETED", 1)]
        )
        notify.assert_called_once()
        self.assertNotIn("detail text", json.dumps(notify.call_args.args[0]))

    def test_recovered_stale_processing_cannot_complete_the_old_call(self):
        event_id = self.make_event()
        later = BRIDGE_NOW + timedelta(seconds=LEASE + 1)
        recoverer = SnapshotStore(self.db_path)
        self.addCleanup(recoverer.close)
        recovered = {}

        def crash_recovery_by_another_process():
            # While the call is in flight, another process sees the lease and the
            # PROCESSING row as stale and recovers both (fence first, then the event).
            recovered["leases"] = claude_bridge._recover_expired_leases(recoverer, "recoverer", later)
            cutoff = (later - timedelta(seconds=config.CLAUDE_BRIDGE_PROCESSING_STALE_SECONDS)).isoformat()
            recovered["events"] = recoverer.recover_stale_processing(cutoff, later.isoformat())

        slow, notify = FakeModel(on_call=crash_recovery_by_another_process), mock.Mock()
        with dispatch_enabled():
            result = claude_bridge.run_bridge_cycle(self.store, now=BRIDGE_NOW, create_fn=slow, notify_fn=notify)

        self.assertEqual((recovered["leases"], recovered["events"]), (1, [event_id]))
        self.assertEqual(result.processed[0]["outcome"], "FENCED")
        self.assertEqual(result.processed[0]["reason"], TransitionStatus.NOT_ACTIVE.value)
        self.assertEqual(result.health, "DEGRADED")
        # The old call concluded nothing: no analysis, no PROCESSED, no notification.
        self.assertEqual(self.analyses(event_id), [])
        row = self.event(event_id)
        self.assertEqual((row["status"], row["attempts"]), ("PENDING", 1))
        notify.assert_not_called()
        [invocation] = self.invocations()
        self.assertEqual(
            (invocation["state"], invocation["generation"], invocation["attempt_count"]), ("RELEASED", 2, 1)
        )

        # The recovered event is taken again with a new claim: a second genuine call, a second unit.
        again = FakeModel()
        with dispatch_enabled():
            claude_bridge.run_bridge_cycle(self.store, now=later, create_fn=again, notify_fn=notify)
        self.assertEqual(self.event(event_id)["status"], "PROCESSED")
        self.assertEqual(self.reserved("SONNET", later), (2, 2))
        self.assertEqual([i["attempt_count"] for i in self.invocations()], [1, 1])  # never decremented
        self.assertGreaterEqual(self.event(event_id)["attempts"], 1)
        notify.assert_called_once()

    def test_bridge_cycle_fences_a_crashed_holder_before_recovering_its_event(self):
        event_id = self.make_event()
        crashed = SnapshotStore(self.db_path)
        self.addCleanup(crashed.close)
        budget = budgets.model_budget("SONNET")
        self.assertTrue(crashed.claim_event_for_processing(event_id, BRIDGE_NOW.isoformat()))
        claim = crashed.claim_invocation(
            claude_bridge.invocation_request_for_event(self.store.get_event(event_id)), budget, "crashed", LEASE,
            now=BRIDGE_NOW,
        )
        self.assertTrue(crashed.record_invocation_attempt(claim.lease, budget, now=BRIDGE_NOW).applied)
        # ... the holder dies here, mid-call.

        later = BRIDGE_NOW + timedelta(seconds=LEASE + 1)
        model = FakeModel()
        with dispatch_enabled():
            result = claude_bridge.run_bridge_cycle(self.store, now=later, create_fn=model)

        self.assertEqual(result.recovered_stale, 1)
        self.assertEqual(self.event(event_id)["status"], "PROCESSED")
        self.assertEqual(len(model.calls), 1)
        # The crashed holder can never complete now.
        late = crashed.complete_invocation(claim.lease, now=later)
        self.assertEqual(late.status, TransitionStatus.NOT_ACTIVE)
        first, second = self.invocations()
        self.assertEqual((first["state"], first["generation"], first["attempt_count"]), ("RELEASED", 2, 1))
        self.assertEqual((second["state"], second["attempt_count"]), ("COMPLETED", 1))
        self.assertEqual(self.reserved("SONNET", later), (2, 2))  # one unit per genuine call


class TestFailClosed(BridgeStoreCase):
    def test_event_without_a_native_instrument_fails_without_charge(self):
        event_id = self.make_event(market="SPOT", spot_pair=None)
        model = FakeModel()
        with dispatch_enabled():
            result = claude_bridge.run_bridge_cycle(self.store, now=BRIDGE_NOW, create_fn=model)
        row = self.event(event_id)
        self.assertEqual((row["status"], row["last_error"]), ("FAILED", "invocation_identity_invalid: invalid_field"))
        self.assertEqual(result.processed[0]["outcome"], "FAILED")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.reserved("SONNET"), (0, 0))
        self.assertEqual(self.invocations(), [])

    def test_store_error_detail_never_reaches_the_event_or_a_notification(self):
        event_id = self.make_event()
        model, notify = FakeModel(), mock.Mock()
        busy = InvocationError(InvocationFailure.BUSY, "database is locked SECRET-DETAIL")
        with dispatch_enabled(), mock.patch.object(self.store, "claim_invocation", side_effect=busy):
            result = claude_bridge.run_bridge_cycle(self.store, now=BRIDGE_NOW, create_fn=model, notify_fn=notify)
        row = self.event(event_id)
        self.assertEqual((row["status"], row["last_error"]), ("DEFERRED", "invocation_store_busy"))
        self.assertEqual(result.health, "DEGRADED")
        self.assertEqual(model.calls, [])
        notify.assert_not_called()
        self.assertIsNone(self.cooldown_row("BTC", "SONNET"))
        with open(config.EVENTS_LOG_PATH, encoding="utf-8") as log:
            self.assertNotIn("SECRET-DETAIL", log.read())

    def test_invocation_identity_is_the_oc1_tuple_of_the_event(self):
        event_id = self.make_event()
        request = claude_bridge.invocation_request_for_event(self.store.get_event(event_id))
        identity = request.identity
        self.assertEqual(request.model, "SONNET")
        self.assertEqual(
            (identity.venue, identity.market_kind, identity.native_instrument, identity.setup, identity.direction),
            ("kraken", InstrumentKind.FUTURES, "PF_BTCUSD", "BREAKOUT", Direction.LONG),
        )
        self.assertEqual(identity.policy_version, "OC-1/bridge_prompt_v1")
        self.assertEqual(identity.evidence_hash, claude_bridge.event_evidence_hash(self.store.get_event(event_id)))
        other_id = self.make_event(asset="ETH")
        self.assertNotEqual(
            identity.evidence_hash, claude_bridge.event_evidence_hash(self.store.get_event(other_id))
        )


if __name__ == "__main__":
    unittest.main()
