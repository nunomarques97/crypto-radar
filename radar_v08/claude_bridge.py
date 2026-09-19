"""Claude Bridge (Phase 4): turns PENDING events into Sonnet/Fable analysis.

RADAR LOCAL -> EVENT -> CLAUDE BRIDGE -> MODEL (SONNET | FABLE) -> ANALYSIS
-> RESULT -> PERSISTENCE -> WINDOWS NOTIFICATION.

This module never knows Kraken trading rules and never executes anything on
a market - it only reads events the radar already created, calls a model,
and writes the result back. It respects the Demand Router's decision as the
sole authority for model choice: only SONNET and FABLE are ever dispatched,
never OPUS, and never a model choice made by the LLM itself.

SQLite (the `events` table) is the source of state; `events.jsonl` stays the
append-only audit log (events.py already writes both). This module only ever
transitions events through their SQLite row - never edits the jsonl log.

Budget, claim and cooldown (T031b, TAKEOVER_AUDIT P1): an event is taken in
this order - (1) the event row moves PENDING/DEFERRED -> PROCESSING (a lost
race means someone else has it: skip, nothing charged); (2) the T031a atomic
claim + budget reservation for the event's invocation identity, which is the
only place a budget unit is spent (refused -> event DEFERRED, nothing charged;
an identical active invocation held elsewhere -> event DEFERRED, nothing
charged); (3) only then the cooldown starts; (4) the attempt is recorded
before the call. After the call the result commits only if the lease still
holds (`complete_invocation` / `release_invocation` return APPLIED): a holder
fenced by crash recovery writes no analysis, no event transition and no
notification. Every retry is a new claim, so each genuine call costs exactly
one unit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import budgets, config, cooldown, events
from .analysis_schema import ANALYSIS_RESPONSE_SCHEMA, validate_analysis
from .context_builder import build_model_context
from .domain.integrity import POLICY_VERSION, InstrumentKind
from .domain.invocation import (
    HASH_PREFIX,
    MAX_LEASE_SECONDS,
    MIN_LEASE_SECONDS,
    Direction,
    Duplicate,
    InvocationError,
    InvocationFailure,
    InvocationIdentity,
    InvocationRequest,
    Lease,
    Refused,
    ReleaseReason,
    TransitionStatus,
)
from .prompts import (
    BRIDGE_SYSTEM_PROMPT_V1,
    MODEL_TASK_INSTRUCTIONS,
    build_user_message,
)
from .store import SnapshotStore

logger = logging.getLogger("radar_v08.claude_bridge")

CALL_STATUSES = (
    "DISABLED",
    "SUCCESS", "TIMEOUT", "RATE_LIMITED", "QUOTA_EXHAUSTED", "AUTH_ERROR",
    "PROVIDER_UNAVAILABLE", "INVALID_RESPONSE",
)
HEALTH_STATES = ("DISABLED", "ONLINE", "OFFLINE", "RATE_LIMITED", "QUOTA_EXHAUSTED", "AUTH_ERROR", "DEGRADED")

# Higher = worse. Used to pick the single health state a cycle reports when
# several events land on different outcomes (task section 15).
_HEALTH_SEVERITY = {
    "DISABLED": 6,
    "ONLINE": 0,
    "DEGRADED": 1,
    "RATE_LIMITED": 2,
    "QUOTA_EXHAUSTED": 3,
    "OFFLINE": 4,
    "AUTH_ERROR": 5,
}

_STATUS_TO_HEALTH = {
    "DISABLED": "DISABLED",
    "SUCCESS": "ONLINE",
    "TIMEOUT": "DEGRADED",
    "RATE_LIMITED": "RATE_LIMITED",
    "QUOTA_EXHAUSTED": "QUOTA_EXHAUSTED",
    "AUTH_ERROR": "AUTH_ERROR",
    "PROVIDER_UNAVAILABLE": "OFFLINE",
    "INVALID_RESPONSE": "DEGRADED",
}


@dataclass
class CallResult:
    status: str
    parsed: dict[str, Any] | None = None
    raw_text: str | None = None
    error: str | None = None
    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class BridgeCycleResult:
    health: str
    processed: list[dict[str, Any]] = field(default_factory=list)
    recovered_stale: int = 0
    skipped_reason: str | None = None


def _anthropic_available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _api_key_present(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return bool(source.get("ANTHROPIC_API_KEY") or source.get("ANTHROPIC_AUTH_TOKEN"))


def _dispatch_is_disabled() -> bool:
    """Return the non-configurable T010 runtime containment decision."""
    return not config.CLAUDE_BRIDGE_DISPATCH_ENABLED


def _default_create(model: str, max_tokens: int, system: str, user_content: str, schema: dict[str, Any]) -> Any:
    # Keep this legacy helper importable for compatibility, but make a direct
    # call fail before importing/constructing the Anthropic SDK client.
    if _dispatch_is_disabled():
        raise RuntimeError(config.CLAUDE_BRIDGE_DISABLED_REASON)
    import anthropic  # lazy: Kraken/Qwen/router phases must work with no anthropic package installed

    client = anthropic.Anthropic(timeout=config.CLAUDE_BRIDGE_TIMEOUT_SECONDS)
    return client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )


def classify_exception(exc: Exception) -> str:
    """Maps an SDK exception to one of CALL_STATUSES. Falls back to
    PROVIDER_UNAVAILABLE for anything unrecognized (including the anthropic
    package not being installed) rather than crashing the bridge cycle.
    """
    try:
        import anthropic
    except ImportError:
        return "PROVIDER_UNAVAILABLE"

    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "AUTH_ERROR"
    if isinstance(exc, anthropic.RateLimitError):
        return "RATE_LIMITED"
    if isinstance(exc, getattr(anthropic, "APITimeoutError", ())):
        return "TIMEOUT"
    if isinstance(exc, anthropic.APIStatusError):
        etype = getattr(exc, "type", None)
        status_code = getattr(exc, "status_code", None)
        if etype == "billing_error" or status_code == 402:
            return "QUOTA_EXHAUSTED"
        if etype == "overloaded_error" or (status_code is not None and status_code >= 500):
            return "PROVIDER_UNAVAILABLE"
        return "INVALID_RESPONSE"
    if isinstance(exc, anthropic.APIConnectionError):
        return "PROVIDER_UNAVAILABLE"
    return "PROVIDER_UNAVAILABLE"


def _extract_text(response: Any) -> str | None:
    for block in getattr(response, "content", []) or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                return block.get("text")
            continue
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", None)
    return None


def call_model(
    model_demand: str,
    context: dict[str, Any],
    create_fn: Callable[..., Any] | None = None,
) -> CallResult:
    """Dispatches ONE event's context to the model the Demand Router already
    chose. `model_demand` must be SONNET or FABLE - the router's output is the
    sole authority for model selection (task section 5); this function never
    picks a model itself and never accepts OPUS.
    """
    if model_demand not in ("SONNET", "FABLE"):
        raise ValueError(f"Claude Bridge only dispatches SONNET or FABLE, never {model_demand!r}")

    # This guard is intentionally inside the bridge dispatch boundary.  It
    # runs before prompt construction, SDK checks, client construction, and
    # injected fake-client calls, so no caller can opt into cloud inference.
    if _dispatch_is_disabled():
        return CallResult(status="DISABLED", error=config.CLAUDE_BRIDGE_DISABLED_REASON)

    model = config.CLAUDE_BRIDGE_MODEL_IDS[model_demand]
    max_tokens = (
        config.CLAUDE_BRIDGE_SONNET_MAX_TOKENS if model_demand == "SONNET" else config.CLAUDE_BRIDGE_FABLE_MAX_TOKENS
    )
    task_instructions = MODEL_TASK_INSTRUCTIONS[model_demand]
    user_content = build_user_message(task_instructions, context)

    def _call() -> Any:
        if create_fn is not None:
            return create_fn(model, max_tokens, BRIDGE_SYSTEM_PROMPT_V1, user_content, ANALYSIS_RESPONSE_SCHEMA)
        return _default_create(model, max_tokens, BRIDGE_SYSTEM_PROMPT_V1, user_content, ANALYSIS_RESPONSE_SCHEMA)

    started = time.perf_counter()
    try:
        response = _call()
    except ImportError:
        return CallResult(
            status="PROVIDER_UNAVAILABLE",
            error="anthropic SDK not installed (pip install anthropic)",
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001 - classified below into a typed status, never swallowed silently
        status = classify_exception(exc)
        logger.warning("Claude Bridge call failed (%s): %s", status, exc)
        return CallResult(status=status, error=str(exc), latency_ms=(time.perf_counter() - started) * 1000)

    latency_ms = (time.perf_counter() - started) * 1000
    text = _extract_text(response)
    if text is None:
        return CallResult(status="INVALID_RESPONSE", error="no text block in response", latency_ms=latency_ms)

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        return CallResult(status="INVALID_RESPONSE", error=f"invalid JSON: {exc}", raw_text=text, latency_ms=latency_ms)

    validated, validation_error = validate_analysis(parsed, expected_asset=context.get("asset"))
    if validated is None:
        return CallResult(status="INVALID_RESPONSE", error=validation_error, raw_text=text, latency_ms=latency_ms)

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None) if usage is not None else None
    output_tokens = getattr(usage, "output_tokens", None) if usage is not None else None

    return CallResult(
        status="SUCCESS", parsed=validated, raw_text=text, latency_ms=latency_ms,
        input_tokens=input_tokens, output_tokens=output_tokens,
    )


def _backoff_seconds(attempts: int) -> float:
    schedule = config.CLAUDE_BRIDGE_RETRY_BACKOFF_SECONDS
    index = min(max(attempts - 1, 0), len(schedule) - 1)
    return float(schedule[index])


def _worse_health(a: str, b: str) -> str:
    return a if _HEALTH_SEVERITY.get(a, 0) >= _HEALTH_SEVERITY.get(b, 0) else b


def _apply_outcome(store: SnapshotStore, event_row: Any, result: CallResult, now: datetime, now_iso: str) -> str:
    event_id = event_row["event_id"]
    attempts = (event_row["attempts"] or 0) + 1

    if result.status == "SUCCESS":
        store.mark_event_processed(event_id, now_iso)
        events.snapshot_to_jsonl(store, event_id)
        return "PROCESSED"

    # Auth problems are a configuration issue, not a transient outage -
    # retrying without fixing the credential just wastes attempts, so this
    # is the one status that goes straight to FAILED (task section 11).
    if result.status == "AUTH_ERROR":
        store.mark_event_failed(event_id, now_iso, f"auth_error: {result.error}")
        events.snapshot_to_jsonl(store, event_id)
        return "FAILED"

    if attempts >= config.CLAUDE_BRIDGE_MAX_ATTEMPTS_BEFORE_FAILED:
        store.mark_event_failed(event_id, now_iso, f"max_attempts_exceeded ({result.status}): {result.error}")
        events.snapshot_to_jsonl(store, event_id)
        return "FAILED"

    next_attempt_at = (now + timedelta(seconds=_backoff_seconds(attempts))).isoformat()
    store.mark_event_deferred(event_id, now_iso, f"{result.status.lower()}: {result.error}", next_attempt_at)
    events.snapshot_to_jsonl(store, event_id)
    return "DEFERRED"


VENUE = "kraken"
# Identity field for the prompt/routing policy an invocation runs under.
INVOCATION_POLICY_VERSION = f"{POLICY_VERSION}/{config.MODEL_VERSION_TAG['SONNET']}"


def _lease_seconds() -> int:
    """The invocation lease matches the stale-PROCESSING window (bounded by T031a's limits)."""
    return min(MAX_LEASE_SECONDS, max(MIN_LEASE_SECONDS, int(config.CLAUDE_BRIDGE_PROCESSING_STALE_SECONDS)))


def _new_owner() -> str:
    return f"bridge-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def event_evidence_hash(event_row: Any) -> str:
    """``sha256:<hex>`` of the event's persisted, immutable evidence version.

    The hash covers the event ID and the context JSON exactly as stored when the
    event was created (never rewritten afterwards). It is not a T030 sealed
    evidence hash: sealed evidence is not attached to events yet (T032/T033).
    """
    payload = json.dumps(
        {"event_id": event_row["event_id"], "context_json": event_row["context_json"]},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return HASH_PREFIX + hashlib.sha256(payload.encode("ascii")).hexdigest()


def invocation_request_for_event(event_row: Any) -> InvocationRequest:
    """The OC-1 invocation identity of one event, for the model the router chose.

    Raises `InvocationError(INVALID_FIELD)` when the persisted event does not name
    its market, native instrument, setup or direction: an unknown identity is
    never guessed (fail closed).
    """
    market = event_row["market"]
    try:
        persisted = json.loads(event_row["context_json"]) if event_row["context_json"] else {}
    except (TypeError, ValueError) as exc:
        raise InvocationError(InvocationFailure.INVALID_FIELD, "event context is not valid JSON") from exc
    if not isinstance(persisted, dict):
        raise InvocationError(InvocationFailure.INVALID_FIELD, "event context is not a JSON object")
    if market == "FUTURES":
        kind, native = InstrumentKind.FUTURES, persisted.get("futures_symbol")
    elif market == "SPOT":
        kind, native = InstrumentKind.SPOT, persisted.get("spot_pair")
    else:
        raise InvocationError(InvocationFailure.INVALID_FIELD, "event market is neither SPOT nor FUTURES")
    if not isinstance(native, str):
        raise InvocationError(InvocationFailure.INVALID_FIELD, "event context does not name its native instrument")
    try:
        direction = Direction(event_row["direction"])
    except ValueError as exc:
        raise InvocationError(InvocationFailure.INVALID_FIELD, "event direction is not LONG/SHORT/NONE") from exc
    identity = InvocationIdentity(
        venue=VENUE,
        market_kind=kind,
        native_instrument=native,
        setup=event_row["setup_type"],
        direction=direction,
        evidence_hash=event_evidence_hash(event_row),
        policy_version=INVOCATION_POLICY_VERSION,
    )
    return InvocationRequest(identity=identity, model=event_row["model_demand"])


def _recover_expired_leases(store: SnapshotStore, owner: str, now: datetime) -> int:
    """Fence every expired invocation lease (generation + 1), then release it.

    The crashed or stalled holder can no longer complete: its generation is stale
    and the invocation is RELEASED. Nothing is reserved or refunded and no
    attempt count changes; the event's next claim is a new invocation.
    """
    leases = store.recover_expired_invocations(owner, _lease_seconds(), limit=1000, now=now)
    for lease in leases:
        store.release_invocation(lease, ReleaseReason.FAILED, now=now)
    return len(leases)


def _defer_uncharged(
    store: SnapshotStore, row: Any, now: datetime, now_iso: str, reason: str, processed: list[dict[str, Any]]
) -> None:
    """Put a taken event back as DEFERRED with a backoff; no budget unit was spent for it."""
    event_id = row["event_id"]
    next_attempt_at = (now + timedelta(seconds=_backoff_seconds((row["attempts"] or 0) + 1))).isoformat()
    store.mark_event_deferred(event_id, now_iso, reason, next_attempt_at)
    events.snapshot_to_jsonl(store, event_id)
    processed.append({"event_id": event_id, "asset": row["asset"], "outcome": "DEFERRED", "reason": reason})


def run_bridge_cycle(
    store: SnapshotStore,
    now: datetime | None = None,
    max_events: int | None = None,
    create_fn: Callable[..., Any] | None = None,
    notify_fn: Callable[[dict[str, Any]], None] | None = None,
    env: dict | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    owner: str | None = None,
) -> BridgeCycleResult:
    """One pass over the actionable event queue. Safe to call repeatedly
    (loop mode) or once (single `--mode full`/`--mode bridge` run). Never
    raises on provider trouble - every failure mode ends in a typed event
    status and a bridge health state, never an uncaught exception.

    `clock` (aware UTC) times claims, leases, budget windows and the cooldown;
    by default it is the wall clock, or the fixed `now` when only `now` is given.
    `owner` names this cycle's lease holder (default: a fresh per-cycle name).
    """
    # The cycle boundary is a second, independent guard for direct bridge
    # mode and full/loop queue drains.  Do not claim/recover/defer queued rows
    # or overwrite historical health: containment is read-only apart from its
    # returned blocked result, preserving legacy analysis/history inspection.
    if _dispatch_is_disabled():
        return BridgeCycleResult(
            health="DISABLED",
            skipped_reason="LOCAL_ONLY_POLICY",
        )

    if clock is None:
        fixed = now
        clock = (lambda: fixed) if fixed is not None else (lambda: datetime.now(timezone.utc))
    now = now or clock()
    now_iso = now.isoformat()
    max_events = max_events if max_events is not None else config.CLAUDE_BRIDGE_MAX_EVENTS_PER_CYCLE
    owner = owner or _new_owner()

    # Fence first: an expired invocation lease gets a new generation and is
    # released, so its old holder can no longer complete. Only then are stale
    # PROCESSING events handed back to the queue.
    try:
        fenced = _recover_expired_leases(store, owner, clock())
    except InvocationError as exc:
        logger.warning("Invocation store unavailable during lease recovery (%s)", exc.code.value)
        store.set_bridge_health("DEGRADED", f"invocation_store_{exc.code.value}", now_iso)
        return BridgeCycleResult(health="DEGRADED", skipped_reason="INVOCATION_STORE_UNAVAILABLE")
    if fenced:
        logger.warning("Fenced and released %d expired invocation lease(s)", fenced)

    cutoff_iso = (now - timedelta(seconds=config.CLAUDE_BRIDGE_PROCESSING_STALE_SECONDS)).isoformat()
    recovered_ids = store.recover_stale_processing(cutoff_iso, now_iso)
    recovered = len(recovered_ids)
    if recovered:
        logger.warning("Recovered %d stale PROCESSING event(s) back to PENDING", recovered)
        for recovered_id in recovered_ids:
            events.snapshot_to_jsonl(store, recovered_id)

    if create_fn is None and not _anthropic_available():
        store.set_bridge_health("OFFLINE", "anthropic SDK not installed", now_iso)
        return BridgeCycleResult(health="OFFLINE", recovered_stale=recovered, skipped_reason="NO_SDK")

    if create_fn is None and not _api_key_present(env):
        store.set_bridge_health("AUTH_ERROR", "no ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN in environment", now_iso)
        return BridgeCycleResult(health="AUTH_ERROR", recovered_stale=recovered, skipped_reason="NO_CREDENTIALS")

    candidates = store.find_actionable_events(now_iso, max_events)
    processed: list[dict[str, Any]] = []
    cycle_health = "ONLINE"

    for row in candidates:
        event_id = row["event_id"]
        model_demand = row["model_demand"]

        if model_demand not in ("SONNET", "FABLE"):
            # IGNORE-demand events are never queued for the Bridge in the
            # first place, but guard anyway - never call a model for one.
            continue

        if store.has_successful_analysis(event_id):
            # Idempotency backstop: a PROCESSED-looking candidate that
            # somehow re-entered the actionable set is never re-billed.
            store.mark_event_processed(event_id, now_iso)
            events.snapshot_to_jsonl(store, event_id)
            continue

        # (1) Take the event. A lost race means another connection has it:
        # nothing was charged, nothing is touched.
        if not store.claim_event_for_processing(event_id, clock().isoformat()):
            continue
        events.snapshot_to_jsonl(store, event_id)

        try:
            request = invocation_request_for_event(row)
        except InvocationError as exc:
            # The persisted event does not name its instrument/direction: never
            # guessed, never dispatched, never charged.
            store.mark_event_failed(event_id, now_iso, f"invocation_identity_invalid: {exc.code.value}")
            events.snapshot_to_jsonl(store, event_id)
            processed.append(
                {"event_id": event_id, "asset": row["asset"], "outcome": "FAILED", "reason": "invocation_identity_invalid"}
            )
            continue

        budget = budgets.model_budget(model_demand)
        try:
            # (2) The one and only budget charge: the atomic claim + reservation.
            claim = store.claim_invocation(request, budget, owner, _lease_seconds(), now=clock())
            if isinstance(claim, Refused):
                _defer_uncharged(store, row, now, now_iso, f"budget_exhausted: {claim.reason.value}", processed)
                continue
            if isinstance(claim, Duplicate):
                _defer_uncharged(store, row, now, now_iso, "invocation_active_elsewhere", processed)
                continue
            lease: Lease = claim.lease

            # (3) The intended transition was accepted: only now does the cooldown start.
            cooldown.record_send(
                store, row["asset"], model_demand, clock(),
                row["setup_type"], row["direction"], row["opportunity_score"],
            )

            # (4) Record the genuine attempt before the call (the claim's unit covers it).
            attempt = store.record_invocation_attempt(lease, budget, now=clock())
            if attempt.status is not TransitionStatus.APPLIED:
                logger.warning("Lease lost before the call for event %s (%s)", event_id, attempt.status.value)
                cycle_health = _worse_health(cycle_health, "DEGRADED")
                processed.append(
                    {"event_id": event_id, "asset": row["asset"], "outcome": "FENCED", "reason": attempt.status.value}
                )
                continue

            context = build_model_context(row)
            requested_at = datetime.now(timezone.utc).isoformat()
            result = call_model(model_demand, context, create_fn=create_fn)
            completed_at = datetime.now(timezone.utc).isoformat()

            # The result commits only while this lease still holds the invocation.
            if result.status == "SUCCESS":
                transition = store.complete_invocation(lease, now=clock())
            else:
                transition = store.release_invocation(lease, ReleaseReason.FAILED, now=clock())
        except InvocationError as exc:
            # Storage trouble (busy, not migrated, ...): fail closed and stop the
            # cycle. Only the typed code is recorded; the detail stays in the log.
            logger.warning("Invocation store unavailable for event %s (%s)", event_id, exc.code.value)
            logger.debug("Invocation store detail: %s", exc.detail)
            cycle_health = _worse_health(cycle_health, "DEGRADED")
            current = store.get_event(event_id)
            if current is not None and current["status"] == "PROCESSING":
                _defer_uncharged(store, row, now, now_iso, f"invocation_store_{exc.code.value}", processed)
            break

        if transition.status is not TransitionStatus.APPLIED:
            # Fenced by crash recovery (or the lease ran out): this old holder
            # writes no analysis, no event transition and no notification.
            logger.warning(
                "Discarded %s result for event %s: invocation lease %s", result.status, event_id, transition.status.value
            )
            cycle_health = _worse_health(cycle_health, "DEGRADED")
            processed.append(
                {
                    "event_id": event_id, "asset": row["asset"], "model": model_demand,
                    "outcome": "FENCED", "status": result.status, "reason": transition.status.value,
                }
            )
            continue

        store.insert_model_analysis(
            event_id=event_id,
            model=config.CLAUDE_BRIDGE_MODEL_IDS[model_demand],
            model_version=config.MODEL_VERSION_TAG[model_demand],
            requested_at=requested_at,
            completed_at=completed_at,
            status=result.status,
            response=result.raw_text,
            parsed_output_json=json.dumps(result.parsed, ensure_ascii=False) if result.parsed is not None else None,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            error=result.error,
        )

        outcome = _apply_outcome(store, row, result, now, now_iso)
        cycle_health = _worse_health(cycle_health, _STATUS_TO_HEALTH.get(result.status, "DEGRADED"))

        entry = {
            "event_id": event_id, "asset": row["asset"], "model": model_demand,
            "outcome": outcome, "status": result.status, "parsed": result.parsed,
            "error": result.error, "latency_ms": result.latency_ms,
        }
        processed.append(entry)

        if outcome == "PROCESSED" and notify_fn is not None and not row["notified"]:
            notify_fn(
                {
                    "event_id": event_id, "asset": row["asset"], "model": model_demand,
                    "setup_type": row["setup_type"], "direction": row["direction"],
                    "opportunity_score": row["opportunity_score"], "tradeability_score": row["tradeability_score"],
                    "recommendation": (result.parsed or {}).get("recommendation"),
                }
            )
            store.mark_event_notified(event_id, now_iso)
            events.snapshot_to_jsonl(store, event_id)

    store.set_bridge_health(cycle_health, None, now_iso)
    return BridgeCycleResult(health=cycle_health, processed=processed, recovered_stale=recovered)


def bridge_health_label(store: SnapshotStore) -> str:
    """Last-known health, surviving process restarts (task section 15)."""
    row = store.get_bridge_health()
    return row["state"] if row is not None else "UNKNOWN"
