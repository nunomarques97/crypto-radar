"""Extensible agent abstraction for the Agents panel and the future 'Agent
Control Room'.

Every field on `Agent` must be sourced from a real radar_v08 signal - never
fabricated to make an agent look more alive than the backend actually knows.
Adding a future agent (Market Regime, Liquidity, Position/Trade Monitor,
specialist agents, ...) means adding one `AgentDefinition` to
`AGENT_REGISTRY` - and, only if it needs a genuinely new way of deriving its
fields, one new branch in `_build_one`. It never means touching the UI
templates or JS: they iterate `build_agents()`'s output generically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from radar_v08 import budgets, config
from radar_v08.adapters.outbox_store import MAX_READ_LIMIT, OutboxError, OutboxKind
from radar_v08.claude_bridge import HEALTH_STATES, bridge_health_label
from radar_v08.store import SnapshotStore

# Display statuses that exist outside HEALTH_STATES because they describe an
# agent concept the backend has no bridge-health equivalent for. An agent's
# `status` must always be one of HEALTH_STATES or one of these - never an
# invented value.
DISPLAY_ONLY_STATUSES = ("NOT_CONFIGURED", "IDLE", "PROCESSING", "COMPLETED", "UNKNOWN")
VALID_STATUSES = set(HEALTH_STATES) | set(DISPLAY_ONLY_STATUSES)


@dataclass
class Agent:
    id: str
    name: str
    model: str | None
    role: str
    status: str
    current_event: str | None
    last_activity: str | None
    events_processed: int
    last_error: str | None


@dataclass
class AgentDefinition:
    id: str
    name: str
    role: str
    kind: str  # "qwen_screener" | "claude_bridge_model" | "not_configured"
    model_config_key: str | None = None
    bridge_model_key: str | None = None  # "SONNET" | "FABLE"
    # Explicit, one-directional communication topology owned by the SENDING
    # agent - e.g. `connects_to=("qwen-red-team",)` on qwen-14b means "Qwen
    # hands off to Red Team". This is deliberately independent of where an
    # agent sits in AGENT_REGISTRY: reordering or inserting registry entries
    # must never create or remove a connection (see build_connections() and
    # tests/ui_tests/test_agents.py::TestConnections). Empty by default - a
    # new agent has no relationship until one is explicitly declared here.
    connects_to: tuple[str, ...] = ()


AGENT_REGISTRY: list[AgentDefinition] = [
    AgentDefinition(
        id="qwen-14b", name="Qwen 14B", role="Screener",
        kind="qwen_screener", model_config_key="QWEN_MODEL",
        connects_to=("qwen-red-team",),
    ),
    AgentDefinition(
        id="qwen-red-team", name="Qwen Red Team", role="Adversarial review",
        kind="not_configured",
    ),
    AgentDefinition(
        id="sonnet", name="Sonnet", role="Orchestrator",
        kind="claude_bridge_model", bridge_model_key="SONNET",
    ),
    AgentDefinition(
        id="fable", name="Fable 5.1", role="Trading analyst",
        kind="claude_bridge_model", bridge_model_key="FABLE",
    ),
]


def _latest_qwen_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    for candidate in reversed(candidates):
        if candidate.get("qwen") is not None:
            return candidate
    return None


def _build_qwen_screener(defn: AgentDefinition, output_snapshot: dict[str, Any] | None) -> Agent:
    """`radar_v08_output.json` is only written after a cycle finishes
    (`run_and_write` calls `write_json` at the end of the run) - there is no
    signal in it that could honestly mean "Qwen is processing right now".
    So this agent is either IDLE (nothing to show yet) or COMPLETED (showing
    the last cycle's result) - never a fabricated PROCESSING.
    """
    model = getattr(config, defn.model_config_key) if defn.model_config_key else None
    candidates = (output_snapshot or {}).get("candidates") or []
    reviewed = _latest_qwen_candidate(candidates)
    qwen_reviewed_count = sum(1 for c in candidates if c.get("qwen") is not None)

    if not candidates:
        status = "IDLE"
        current_event = None
    else:
        status = "COMPLETED"
        current_event = (reviewed or candidates[-1]).get("asset")

    return Agent(
        id=defn.id, name=defn.name, model=model, role=defn.role,
        status=status, current_event=current_event,
        last_activity=(output_snapshot or {}).get("timestamp"),
        # No lifetime Qwen counter exists in the backend - this is a
        # this-cycle count only, never implied to be a running total.
        events_processed=qwen_reviewed_count,
        last_error=None,
    )


def _build_not_configured(defn: AgentDefinition) -> Agent:
    return Agent(
        id=defn.id, name=defn.name, model=None, role=defn.role,
        status="NOT_CONFIGURED", current_event=None, last_activity=None,
        events_processed=0, last_error=None,
    )


def _latest_model_analysis(store: SnapshotStore, model: str) -> dict[str, Any] | None:
    """UI-only convenience read over the existing `model_analyses` table -
    not a new domain concept, so it stays local here rather than becoming a
    new SnapshotStore method (unlike `model_analysis_call_counts`, which is a
    genuinely reusable aggregate worth adding to store.py itself).
    """
    with store._cursor() as cur:  # noqa: SLF001 - read-only convenience query, same pattern as store.py's own methods
        cur.execute(
            "SELECT * FROM model_analyses WHERE model = ? ORDER BY id DESC LIMIT 1",
            (model,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None


def _build_claude_bridge_model(
    defn: AgentDefinition, store: SnapshotStore, call_counts: dict[str, int]
) -> Agent:
    model_id = config.CLAUDE_BRIDGE_MODEL_IDS.get(defn.bridge_model_key)
    latest_analysis = _latest_model_analysis(store, model_id) if model_id else None

    # T010 is a runtime policy, not a health failure.  Old bridge rows remain
    # readable below, but no stale ONLINE row or queued legacy demand may make
    # a disabled cloud role look active in the Control Room.
    if not config.CLAUDE_BRIDGE_DISPATCH_ENABLED:
        last_activity = latest_analysis["completed_at"] or latest_analysis["requested_at"] if latest_analysis else None
        return Agent(
            id=defn.id, name=defn.name, model=model_id,
            role=f"{defn.role} (legacy cloud disabled; local-only runtime)",
            status="DISABLED", current_event=None, last_activity=last_activity,
            events_processed=call_counts.get(model_id, 0) if model_id else 0,
            last_error=None,
        )

    health = bridge_health_label(store)
    latest_event_row = store.latest_event()

    # Only claim "PROCESSING" when the most recent radar event actually
    # demands this model and is still open - never inferred from health alone.
    is_processing = (
        health in ("ONLINE", "DEGRADED")
        and latest_event_row is not None
        and latest_event_row["model_demand"] == defn.bridge_model_key
        and latest_event_row["status"] in ("PENDING", "PROCESSING")
    )
    status = "PROCESSING" if is_processing else health

    current_event = latest_event_row["event_id"] if is_processing else None
    last_activity = latest_analysis["completed_at"] or latest_analysis["requested_at"] if latest_analysis else None
    last_error = latest_analysis["error"] if latest_analysis else None

    return Agent(
        id=defn.id, name=defn.name, model=model_id, role=defn.role,
        status=status, current_event=current_event, last_activity=last_activity,
        events_processed=call_counts.get(model_id, 0) if model_id else 0,
        last_error=last_error,
    )


def _build_one(
    defn: AgentDefinition, store: SnapshotStore, output_snapshot: dict[str, Any] | None,
    call_counts: dict[str, int],
) -> Agent:
    if defn.kind == "qwen_screener":
        return _build_qwen_screener(defn, output_snapshot)
    if defn.kind == "not_configured":
        return _build_not_configured(defn)
    if defn.kind == "claude_bridge_model":
        return _build_claude_bridge_model(defn, store, call_counts)
    raise ValueError(f"Unknown AgentDefinition.kind: {defn.kind!r}")


@dataclass
class AgentCommunication:
    """The Phase 4 real-communication contract: one genuine responsibility
    handoff between two registered agents. `id` must be stable across polls
    (it's the frontend's dedup key - see agent_room.js processCommunications)
    and `(from_agent, to_agent)` must match a declared `connects_to` edge;
    topology says who is *allowed* to talk, this says they *did*, just now.
    """

    id: str
    from_agent: str
    to_agent: str
    ts: str
    type: str | None = None
    reason: str | None = None


def validate_agent_communications(
    raw_events: list[dict[str, Any]],
    registry: list[AgentDefinition] | None = None,
) -> list[dict[str, Any]]:
    """Sanitizes candidate communication events from a real backend emitter
    before they reach get_state(). Never invents or completes a malformed
    entry - anything that fails validation is silently dropped, exactly like
    build_connections() drops a dangling `connects_to` id.

    Rules:
    - id/from/to/ts are all required; a malformed entry is dropped.
    - (from, to) must match an existing topology edge (build_connections()) -
      a real event for a route nobody declared is dropped, not invented into
      a new edge.
    - duplicate ids (by the *id* alone, never by timestamp) collapse to the
      first occurrence, so the same backend event polled twice never becomes
      two entries.
    """
    reg = registry if registry is not None else AGENT_REGISTRY
    valid_edges = {(c["from"], c["to"]) for c in build_connections(reg)}
    seen_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in raw_events:
        cid, frm, to, ts = raw.get("id"), raw.get("from"), raw.get("to"), raw.get("ts")
        if not cid or not frm or not to or not ts:
            continue
        if cid in seen_ids:
            continue
        if (frm, to) not in valid_edges:
            continue
        seen_ids.add(cid)
        entry: dict[str, Any] = {"id": cid, "from": frm, "to": to, "ts": ts}
        if raw.get("type") is not None:
            entry["type"] = raw["type"]
        if raw.get("reason") is not None:
            entry["reason"] = raw["reason"]
        out.append(entry)
    return out


def collect_real_agent_communications(
    store: SnapshotStore, registry: list[AgentDefinition] | None = None
) -> list[dict[str, Any]]:
    """The Phase 4 backend hook point for `get_state()["agent_communications"]`.

    T033b: reads every persisted HANDOFF row from the T033a delivery outbox
    (`SnapshotStore.outbox_entries`, `radar_v08/adapters/outbox_store.py`) and
    runs it through `validate_agent_communications()` exactly like any other
    candidate event - same id/from/to/ts requirement, same topology-edge
    check, same dedup by id. This is a real query now, not a hardcoded [].

    It still returns [] today: nothing in the current pipeline calls
    `SnapshotStore.record_handoff` (the T032b worker/controller wiring that
    would is later work - see docs/tasks/results/T033a.md "Not wired yet").
    A handoff's `sender`/`receiver` are also validated by the outbox as
    lowercase `[a-z][a-z0-9_]*` role identifiers, which the current
    hyphenated `AgentDefinition.id`s (e.g. `qwen-14b`) do not match - that
    projection is explicitly deferred to T033b/T070 in the same note, and is
    still open (see docs/tasks/results/T033.md). Nothing here fabricates a
    handoff to paper over either gap: a real, valid row still passes through
    once one is ever recorded, and only that.

    Every HANDOFF row is read, page by page (`MAX_READ_LIMIT` rows per page),
    so the newest handoffs are never cut off behind a first page. An outbox
    that cannot be read (busy, broken, or a corrupt payload: `OutboxError`)
    yields [] - "nothing proven" - rather than an exception into the UI.
    """
    raw_events: list[dict[str, Any]] = []
    after = 0
    try:
        while True:
            entries = store.outbox_entries(after=after, kind=OutboxKind.HANDOFF, limit=MAX_READ_LIMIT)
            raw_events.extend(dict(entry.payload()) for entry in entries)
            if len(entries) < MAX_READ_LIMIT:
                break
            after = entries[-1].seq
    except OutboxError:
        return []
    return validate_agent_communications(raw_events, registry)


def build_connections(registry: list[AgentDefinition] | None = None) -> list[dict[str, str]]:
    """The Agent Room's communication topology - each pair is `{"from", "to"}`
    of agent ids, sourced only from `AgentDefinition.connects_to`, never from
    list position. A `connects_to` id that doesn't match a real registry
    entry (a typo, or an agent that got renamed/removed) is silently dropped
    rather than surfaced as a dangling connection - never invented, never a
    crash.
    """
    reg = registry if registry is not None else AGENT_REGISTRY
    valid_ids = {defn.id for defn in reg}
    connections: list[dict[str, str]] = []
    for defn in reg:
        for target_id in defn.connects_to:
            if target_id in valid_ids:
                connections.append({"from": defn.id, "to": target_id})
    return connections


def build_agents(
    store: SnapshotStore, output_snapshot: dict[str, Any] | None = None,
    registry: list[AgentDefinition] | None = None,
) -> list[Agent]:
    """Builds the current list of agents from real backend state only.

    `registry` defaults to AGENT_REGISTRY; tests may pass a custom list to
    prove the abstraction is extensible without any other code change.
    """
    call_counts = store.model_analysis_call_counts()
    agents = [
        _build_one(defn, store, output_snapshot, call_counts)
        for defn in (registry if registry is not None else AGENT_REGISTRY)
    ]
    for agent in agents:
        assert agent.status in VALID_STATUSES, f"invented status {agent.status!r} for agent {agent.id!r}"
    return agents
