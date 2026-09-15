"""Event queue: events.jsonl (append-only log) + SQLite `events` table (current
status, dedup). Task section 13: a persistent queue preparing for a future
Claude Bridge - this phase never sends events onward, it only creates and
transitions them.

Deduplication: the same (asset, setup_type, direction, model_demand) tuple
does not spawn a new event every heartbeat while an existing PENDING/
PROCESSING/DEFERRED event for it is still open; a genuinely new situation
(setup/direction/model_demand changed, or the prior event was resolved)
does create a fresh one.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from . import config
from .store import SnapshotStore

EVENT_STATUSES = config.EVENT_STATUSES


def make_dedup_key(asset: str, setup_type: str, direction: str, model_demand: str) -> str:
    return f"{asset}:{setup_type}:{direction}:{model_demand}"


def append_event_jsonl(event: dict[str, Any], path: str | None = None) -> None:
    with open(path or config.EVENTS_LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def create_event_if_new(
    store: SnapshotStore,
    *,
    ts: str,
    type_: str,
    asset: str,
    setup_type: str,
    direction: str,
    market: str,
    anomaly_score: float | None,
    opportunity_score: float | None,
    tradeability_score: float | None,
    confidence: str,
    model_demand: str,
    reason: str,
    status: str,
    context: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> tuple[str, bool]:
    """Returns (event_id, created). `created=False` means an equivalent open
    event already existed and was returned instead of duplicating it.

    `context` is the Phase 4 Claude Bridge payload: a JSON-serializable snapshot
    of everything already computed for this candidate (L1/L2/L3 features, Qwen
    review, router reasoning) captured at the moment the event was created, so
    the Bridge can reconstruct context from persisted data instead of the live
    (and by then stale) in-memory objects.

    `event_id` is normally left unset (a fresh uuid4 hex is generated) - real
    radar events never choose their own id. It exists only so a synthetic
    event (mock-alert, notify-test) can use a clearly-tagged id like
    `MOCK-...` instead of an indistinguishable uuid.
    """
    if status not in EVENT_STATUSES:
        raise ValueError(f"invalid event status: {status!r}")

    dedup_key = make_dedup_key(asset, setup_type, direction, model_demand)
    existing = store.find_open_event_by_dedup(dedup_key)
    if existing is not None:
        return existing["event_id"], False

    event_id = event_id or uuid.uuid4().hex
    event = {
        "event_id": event_id,
        "dedup_key": dedup_key,
        "ts": ts,
        "type": type_,
        "asset": asset,
        "setup_type": setup_type,
        "direction": direction,
        "market": market,
        "anomaly_score": anomaly_score,
        "opportunity_score": opportunity_score,
        "tradeability_score": tradeability_score,
        "confidence": confidence,
        "model_demand": model_demand,
        "reason": reason,
        "status": status,
        "context_json": json.dumps(context, ensure_ascii=False, default=str) if context is not None else None,
    }
    store.insert_event(event)
    append_event_jsonl(event)
    return event_id, True


def transition_event(store: SnapshotStore, event_id: str, new_status: str) -> None:
    if new_status not in EVENT_STATUSES:
        raise ValueError(f"invalid event status: {new_status!r}")
    store.update_event_status(event_id, new_status)
    snapshot_to_jsonl(store, event_id)


def snapshot_to_jsonl(store: SnapshotStore, event_id: str) -> None:
    """Appends the event's CURRENT row to events.jsonl. Used after every
    Phase 4 lifecycle transition (claim/processed/deferred/failed/recovered)
    so the append-only log stays the full audit trail of every status change,
    not just creation (architecture doc: "events.jsonl é o log/auditoria").
    """
    row = store.get_event(event_id)
    if row is not None:
        append_event_jsonl(dict(row))
