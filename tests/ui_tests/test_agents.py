import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from radar_v08 import config
from radar_v08.adapters.outbox_store import (
    Handoff,
    HandoffType,
    LifecycleState,
    OutboxError,
    OutboxFailure,
)
from radar_v08.claude_bridge import HEALTH_STATES
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore
from ui.agents import (
    AGENT_REGISTRY,
    VALID_STATUSES,
    AgentDefinition,
    build_agents,
    build_connections,
    collect_real_agent_communications,
    validate_agent_communications,
)

T0 = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def make_event_kwargs(**overrides):
    base = dict(
        ts=T0.isoformat(), type_="RADAR_ALERT", asset="BTC",
        setup_type="BREAKOUT", direction="LONG", market="SPOT",
        anomaly_score=70.0, opportunity_score=80.0, tradeability_score=85.0,
        confidence="HIGH", model_demand="FABLE", reason="test", status="PENDING",
    )
    base.update(overrides)
    return base


class AgentsTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)
        self._original_events_log_path = config.EVENTS_LOG_PATH
        config.EVENTS_LOG_PATH = self.path + ".events.jsonl"

    def tearDown(self):
        self.store.close()
        config.EVENTS_LOG_PATH = self._original_events_log_path
        for suffix in ("", "-wal", "-shm", ".events.jsonl"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)


class TestRedTeamHonesty(AgentsTestCase):
    def test_red_team_always_not_configured_even_with_a_busy_store(self):
        create_event_if_new(self.store, **make_event_kwargs())
        self.store.set_bridge_health("ONLINE", None, T0.isoformat())
        agents = build_agents(self.store, output_snapshot={"candidates": [{"asset": "BTC", "qwen": {}}]})
        red_team = next(a for a in agents if a.id == "qwen-red-team")
        self.assertEqual(red_team.status, "NOT_CONFIGURED")
        self.assertIsNone(red_team.model)
        self.assertEqual(red_team.events_processed, 0)


class TestBridgeModelHonesty(AgentsTestCase):
    def test_local_only_policy_yields_disabled_not_active(self):
        agents = build_agents(self.store)
        sonnet = next(a for a in agents if a.id == "sonnet")
        fable = next(a for a in agents if a.id == "fable")
        self.assertEqual(sonnet.status, "DISABLED")
        self.assertEqual(fable.status, "DISABLED")
        self.assertIn("local-only", sonnet.role)
        self.assertIsNone(sonnet.last_error)

    def test_stale_online_health_and_pending_demand_never_show_processing(self):
        create_event_if_new(self.store, **make_event_kwargs(model_demand="SONNET", status="PENDING"))
        self.store.set_bridge_health("ONLINE", "legacy record", T0.isoformat())
        agents = build_agents(self.store)
        sonnet = next(a for a in agents if a.id == "sonnet")
        self.assertEqual(sonnet.status, "DISABLED")
        self.assertIsNone(sonnet.current_event)
        self.assertNotEqual(sonnet.status, "PROCESSING")

    def test_status_never_outside_valid_set(self):
        for health in list(HEALTH_STATES) + [None]:
            if health is not None:
                self.store.set_bridge_health(health, None, T0.isoformat())
            agents = build_agents(self.store)
            for agent in agents:
                self.assertIn(agent.status, VALID_STATUSES)

    def test_events_processed_is_real_call_count_not_demand(self):
        event_id, _ = create_event_if_new(self.store, **make_event_kwargs(model_demand="FABLE"))
        self.store.insert_model_analysis(
            event_id=event_id, model=config.CLAUDE_BRIDGE_MODEL_IDS["FABLE"],
            model_version="bridge_prompt_v1", requested_at=T0.isoformat(),
            completed_at=T0.isoformat(), status="SUCCESS", response="{}",
            parsed_output_json="{}", latency_ms=1.0, input_tokens=1, output_tokens=1, error=None,
        )
        agents = build_agents(self.store, output_snapshot={"funnel": {"fable_demand": 4}})
        fable = next(a for a in agents if a.id == "fable")
        # Demand in the output.json funnel says 4; real calls made say 1 -
        # the agent must reflect the real count, never the demand number.
        self.assertEqual(fable.events_processed, 1)

    def test_open_legacy_demand_cannot_make_disabled_role_processing(self):
        create_event_if_new(self.store, **make_event_kwargs(model_demand="SONNET", status="PENDING"))
        self.store.set_bridge_health("ONLINE", None, T0.isoformat())
        agents = build_agents(self.store)
        sonnet = next(a for a in agents if a.id == "sonnet")
        fable = next(a for a in agents if a.id == "fable")
        self.assertEqual(sonnet.status, "DISABLED")
        self.assertEqual(fable.status, "DISABLED")


class TestQwenScreener(AgentsTestCase):
    def test_idle_when_no_output_snapshot(self):
        agents = build_agents(self.store, output_snapshot=None)
        qwen = next(a for a in agents if a.id == "qwen-14b")
        self.assertEqual(qwen.status, "IDLE")
        self.assertIsNone(qwen.current_event)

    def test_model_comes_from_config_not_hardcoded(self):
        agents = build_agents(self.store)
        qwen = next(a for a in agents if a.id == "qwen-14b")
        self.assertEqual(qwen.model, config.QWEN_MODEL)

    def test_current_event_reflects_latest_reviewed_candidate(self):
        snapshot = {
            "timestamp": "2026-09-14T09:21:03Z",
            "candidates": [
                {"asset": "ETH", "qwen": {"veto": False}},
                {"asset": "LSK", "qwen": {"veto": False}},
            ],
        }
        agents = build_agents(self.store, output_snapshot=snapshot)
        qwen = next(a for a in agents if a.id == "qwen-14b")
        self.assertEqual(qwen.current_event, "LSK")
        self.assertEqual(qwen.events_processed, 2)


class TestConnections(AgentsTestCase):
    def test_current_registry_has_exactly_the_one_known_relationship(self):
        self.assertEqual(build_connections(), [{"from": "qwen-14b", "to": "qwen-red-team"}])

    def test_registry_order_alone_does_not_create_connections(self):
        """Four agents with no `connects_to` declared at all must produce ZERO
        connections, even though they sit consecutively in the registry list
        exactly like qwen-14b/qwen-red-team do - proving the topology comes
        only from AgentDefinition.connects_to, never from list position.
        """
        registry_no_relationships = [
            AgentDefinition(id="a", name="A", role="r", kind="not_configured"),
            AgentDefinition(id="b", name="B", role="r", kind="not_configured"),
            AgentDefinition(id="c", name="C", role="r", kind="not_configured"),
        ]
        self.assertEqual(build_connections(registry_no_relationships), [])

    def test_reordering_the_registry_does_not_change_the_connections(self):
        forward = build_connections(AGENT_REGISTRY)
        reversed_registry = list(reversed(AGENT_REGISTRY))
        self.assertEqual(build_connections(reversed_registry), forward)

    def test_a_connects_to_id_that_does_not_exist_is_dropped_not_invented(self):
        registry = [
            AgentDefinition(id="a", name="A", role="r", kind="not_configured", connects_to=("ghost",)),
        ]
        self.assertEqual(build_connections(registry), [])

    def test_appending_a_future_agent_with_no_declared_connection_adds_none(self):
        custom_registry = list(AGENT_REGISTRY) + [
            AgentDefinition(id="future-liquidity", name="Liquidity Agent", role="Future", kind="not_configured"),
        ]
        self.assertEqual(build_connections(custom_registry), build_connections(AGENT_REGISTRY))

    def test_a_future_agent_can_declare_its_own_outgoing_connection(self):
        custom_registry = list(AGENT_REGISTRY) + [
            AgentDefinition(
                id="future-liquidity", name="Liquidity Agent", role="Future", kind="not_configured",
                connects_to=("sonnet",),
            ),
        ]
        conns = build_connections(custom_registry)
        self.assertIn({"from": "future-liquidity", "to": "sonnet"}, conns)
        # and the existing relationship is untouched
        self.assertIn({"from": "qwen-14b", "to": "qwen-red-team"}, conns)


class TestExtensibility(AgentsTestCase):
    def test_adding_a_registry_entry_produces_a_new_agent_with_no_other_change(self):
        custom_registry = list(AGENT_REGISTRY) + [
            AgentDefinition(
                id="risk-agent-preview", name="Risk Agent", role="Future: risk budget",
                kind="not_configured",
            )
        ]
        agents = build_agents(self.store, registry=custom_registry)
        self.assertEqual(len(agents), len(AGENT_REGISTRY) + 1)
        new_agent = next(a for a in agents if a.id == "risk-agent-preview")
        self.assertEqual(new_agent.status, "NOT_CONFIGURED")


class TestAgentCommunications(AgentsTestCase):
    """Phase 4: real agent-to-agent communication events."""

    def test_production_has_no_real_source_so_it_stays_empty(self):
        create_event_if_new(self.store, **make_event_kwargs())
        self.store.set_bridge_health("ONLINE", None, T0.isoformat())
        self.assertEqual(collect_real_agent_communications(self.store), [])

    def test_well_formed_event_on_a_real_topology_edge_passes_through(self):
        raw = [{"id": "c1", "from": "qwen-14b", "to": "qwen-red-team", "ts": T0.isoformat()}]
        self.assertEqual(
            validate_agent_communications(raw),
            [{"id": "c1", "from": "qwen-14b", "to": "qwen-red-team", "ts": T0.isoformat()}],
        )

    def test_optional_type_and_reason_are_preserved(self):
        raw = [{
            "id": "c1", "from": "qwen-14b", "to": "qwen-red-team", "ts": T0.isoformat(),
            "type": "REVIEW_REQUEST", "reason": "veto check",
        }]
        out = validate_agent_communications(raw)
        self.assertEqual(out[0]["type"], "REVIEW_REQUEST")
        self.assertEqual(out[0]["reason"], "veto check")

    def test_duplicate_id_is_collapsed_to_one_entry(self):
        raw = [
            {"id": "c1", "from": "qwen-14b", "to": "qwen-red-team", "ts": T0.isoformat()},
            {"id": "c1", "from": "qwen-14b", "to": "qwen-red-team", "ts": (T0 + timedelta(seconds=5)).isoformat()},
        ]
        out = validate_agent_communications(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ts"], T0.isoformat())

    def test_unknown_agent_ids_are_ignored_safely(self):
        raw = [{"id": "c1", "from": "ghost-agent", "to": "qwen-red-team", "ts": T0.isoformat()}]
        self.assertEqual(validate_agent_communications(raw), [])

    def test_event_without_matching_topology_edge_is_ignored(self):
        # sonnet/fable are real registered agents, but no connects_to edge
        # declares a route between them - so an event claiming one is dropped.
        raw = [{"id": "c1", "from": "sonnet", "to": "fable", "ts": T0.isoformat()}]
        self.assertEqual(validate_agent_communications(raw), [])

    def test_malformed_events_missing_required_fields_are_dropped(self):
        raw = [
            {"from": "qwen-14b", "to": "qwen-red-team", "ts": T0.isoformat()},  # no id
            {"id": "c2", "to": "qwen-red-team", "ts": T0.isoformat()},  # no from
            {"id": "c3", "from": "qwen-14b", "ts": T0.isoformat()},  # no to
            {"id": "c4", "from": "qwen-14b", "to": "qwen-red-team"},  # no ts
        ]
        self.assertEqual(validate_agent_communications(raw), [])

    def test_a_real_persisted_handoff_is_returned(self):
        """T033b: collect_real_agent_communications now runs a real query over
        the T033a outbox. The outbox's role charset (lowercase, underscores
        only - see docs/tasks/results/T033a.md) does not accept the current
        hyphenated AGENT_REGISTRY ids, so this proves the wiring with a
        custom registry (the same extensibility seam TestConnections and
        TestExtensibility already use), not by inventing a handoff on the
        real registry.
        """
        registry = [
            AgentDefinition(id="agent_a", name="A", role="r", kind="not_configured", connects_to=("agent_b",)),
            AgentDefinition(id="agent_b", name="B", role="r", kind="not_configured"),
        ]
        self.store.record_handoff(
            Handoff(
                communication_id="comm-1", run_id="run-1", opportunity_id="opp-1",
                sender="agent_a", receiver="agent_b", handoff_type=HandoffType.DISPATCH,
                reason="deadline_fits", at=T0,
            ),
            now=T0,
        )
        comms = collect_real_agent_communications(self.store, registry=registry)
        self.assertEqual(len(comms), 1)
        self.assertEqual(comms[0]["id"], "comm-1")
        self.assertEqual(comms[0]["from"], "agent_a")
        self.assertEqual(comms[0]["to"], "agent_b")
        self.assertEqual(comms[0]["ts"], T0.isoformat())
        self.assertEqual(comms[0]["type"], "DISPATCH")
        self.assertEqual(comms[0]["reason"], "deadline_fits")

    def test_a_persisted_handoff_off_the_real_topology_is_still_dropped(self):
        """Even a real, persisted row is filtered by the topology-edge and
        dedup rules exactly like a live event would be - a real source is not
        a license to skip validate_agent_communications()."""
        registry = [
            AgentDefinition(id="agent_a", name="A", role="r", kind="not_configured"),
            AgentDefinition(id="agent_b", name="B", role="r", kind="not_configured"),
        ]
        self.store.record_handoff(
            Handoff(
                communication_id="comm-1", run_id="run-1", opportunity_id="opp-1",
                sender="agent_a", receiver="agent_b", handoff_type=HandoffType.DISPATCH,
                reason="deadline_fits", at=T0,
            ),
            now=T0,
        )
        self.assertEqual(collect_real_agent_communications(self.store, registry=registry), [])

    def test_a_full_pipeline_with_lifecycle_rows_but_no_handoff_is_zero_communications(self):
        """Events, a claim, PROCESSED and a full lifecycle walk all write real
        outbox rows (EVENT/LIFECYCLE) - none of them is a handoff, so none of
        them may surface as a communication."""
        event_id, _created = create_event_if_new(self.store, **make_event_kwargs())
        self.store.claim_event_for_processing(event_id, T0.isoformat())
        self.store.mark_event_processed(event_id, T0.isoformat())
        for state in (LifecycleState.QUEUED, LifecycleState.LOADING, LifecycleState.RUNNING, LifecycleState.FINISHED):
            self.store.record_lifecycle_transition("item-1", state, now=T0)
        self.assertGreater(len(self.store.outbox_entries()), 0)
        self.assertEqual(collect_real_agent_communications(self.store), [])

    def test_handoffs_beyond_one_read_page_are_all_returned(self):
        """Every page is read, so the newest handoffs are never cut off behind
        the first page (the page size is shrunk to 2 to prove it with 5 rows)."""
        registry = [
            AgentDefinition(id="agent_a", name="A", role="r", kind="not_configured", connects_to=("agent_b",)),
            AgentDefinition(id="agent_b", name="B", role="r", kind="not_configured"),
        ]
        for n in range(5):
            self.store.record_handoff(
                Handoff(
                    communication_id=f"comm-{n}", run_id="run-1", opportunity_id="opp-1",
                    sender="agent_a", receiver="agent_b", handoff_type=HandoffType.DISPATCH,
                    reason="deadline_fits", at=T0,
                ),
                now=T0,
            )
        with mock.patch("ui.agents.MAX_READ_LIMIT", 2):
            comms = collect_real_agent_communications(self.store, registry=registry)
        self.assertEqual([c["id"] for c in comms], [f"comm-{n}" for n in range(5)])

    def test_an_unreadable_outbox_yields_zero_communications_not_an_exception(self):
        failure = OutboxError(OutboxFailure.STORAGE_ERROR, "disk I/O error")
        with mock.patch.object(self.store, "outbox_entries", side_effect=failure):
            self.assertEqual(collect_real_agent_communications(self.store), [])


if __name__ == "__main__":
    unittest.main()
