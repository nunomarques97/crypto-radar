"""Assembles the market-analysis context sent to Claude (task section 3/4).

Two halves:

- `build_event_context(...)` runs at EVENT-CREATION time (called from
  heartbeat.py, where the live L1/L2/L3/Qwen/router objects for this cycle
  still exist) and produces the JSON blob persisted on the event
  (`events.context_json`). This is what makes the event queue the canonical
  source and `events.jsonl` its audit log, rather than the Bridge needing any
  live radar state.
- `build_model_context(...)` runs at BRIDGE-PROCESSING time (possibly a later
  heartbeat, or after a restart) and reconstructs the context ONLY from what
  is persisted on the event row - never from in-memory objects that may no
  longer exist. It adds the explicit portfolio/trading-state block, which is
  always UNAVAILABLE in this phase (task section 4: no Kraken private access,
  never a fabricated position).

Every leaf value that is missing is the literal string "UNAVAILABLE" (task
section 3), never a fabricated number, so the model can tell "confirmed zero"
apart from "we don't know".

Evidence (T030b): when the caller passes the event's stored evidence
(`SnapshotStore.load_event_evidence`), `build_model_context` first checks the
event's claim against the sealed evidence - evidence id/hash, run, instrument
and the event's own asset - and raises `ContextEvidenceRejected` (a typed
`EvidenceRejected`) on any mismatch. It never returns an empty or partial
context in that case. An event with no link is labelled legacy-unversioned:
it keeps its old persisted context and gets no facts or hash.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from .adapters.evidence_store import EventEvidence, LinkedEvidence, verify_link
from .domain.evidence import (
    EvidenceRejected,
    LegacyUnversionedEvidence,
    RejectionCode,
    SealedEvidence,
    evidence_to_json,
)

UNAVAILABLE = "UNAVAILABLE"

PORTFOLIO_NOTE = (
    "Portfolio/trading state is not connected to this radar in Phase 4 "
    "(market analysis only - no Kraken private endpoints, no account access). "
    "Treat every field in this block as genuinely unknown, not as \"flat/no position\"."
)


class ContextEvidenceRejected(EvidenceRejected):
    """The context for `event_id` was refused because its evidence does not match."""

    def __init__(self, event_id: Any, code: RejectionCode, field: str, detail: str) -> None:
        super().__init__(code, field, f"event {event_id!r}: {detail}")
        self.event_id = event_id


def mark_unavailable(value: Any) -> Any:
    return UNAVAILABLE if value is None else value


def deep_mark_unavailable(obj: Any) -> Any:
    """Recursively replaces every `None` leaf with UNAVAILABLE. Dataclasses are
    converted to dicts first; lists/tuples/dicts are walked; everything else
    (numbers, strings, bools) is returned unchanged.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return deep_mark_unavailable(asdict(obj))
    if isinstance(obj, dict):
        return {k: deep_mark_unavailable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [deep_mark_unavailable(v) for v in obj]
    return mark_unavailable(obj)


def build_event_context(
    *,
    asset: str,
    spot_pair: str | None,
    futures_symbol: str | None,
    market: str,
    current_price: float | None,
    setup_type: str,
    direction: str,
    anomaly_score: float | None,
    l1_features: Any,
    l2_result: Any | None,
    l3_result: Any | None,
    futures_snapshot: dict[str, Any] | None,
    qwen_review: Any | None,
    router_result: Any,
    flags: list[str],
) -> dict[str, Any]:
    """Builds the persisted context blob. Called once, at event creation."""
    context: dict[str, Any] = {
        "asset": asset,
        "spot_pair": spot_pair,
        "futures_symbol": futures_symbol,
        "market": market,
        "current_price": current_price,
        "setup_type": setup_type,
        "direction": direction,
        "anomaly_score": anomaly_score,
        "l1_features": dict(vars(l1_features)) if l1_features is not None else None,
        "flags": list(flags),
    }

    if l2_result is not None:
        context["opportunity_score"] = l2_result.opportunity.score
        context["opportunity_breakdown"] = l2_result.opportunity.breakdown
        context["l2_features"] = {k: v for k, v in vars(l2_result.l2_features).items() if k != "flags"}
        context["derivatives_coherence"] = l2_result.opportunity.derivatives_coherence
        context["setup_notes"] = list(getattr(l2_result.setup, "notes", []) or [])
    else:
        context["opportunity_score"] = None
        context["opportunity_breakdown"] = None
        context["l2_features"] = None
        context["derivatives_coherence"] = None
        context["setup_notes"] = None

    if l3_result is not None:
        context["tradeability_score"] = l3_result.tradeability.score
        context["tradeability_state"] = l3_result.tradeability.state
        context["tradeability_breakdown"] = l3_result.tradeability.breakdown
        context["cost_preview"] = l3_result.cost_preview
    else:
        context["tradeability_score"] = None
        context["tradeability_state"] = None
        context["tradeability_breakdown"] = None
        context["cost_preview"] = None

    context["futures"] = futures_snapshot

    context["qwen"] = (
        {
            "setup_type": qwen_review.setup_type,
            "direction": qwen_review.direction,
            "market": qwen_review.market,
            "veto": qwen_review.veto,
            "call_sonnet": qwen_review.call_sonnet,
            "call_fable": qwen_review.call_fable,
            "confidence": qwen_review.confidence,
            "reason": qwen_review.reason,
            "data_quality_notes": qwen_review.data_quality_notes,
        }
        if qwen_review is not None
        else None
    )

    context["router"] = {
        "decision": router_result.decision,
        "model_demand_score": router_result.model_demand_score,
        "confidence": router_result.confidence,
        "confirmations": router_result.confirmations,
        "reasons": router_result.reasons,
    }

    return deep_mark_unavailable(context)


def _load_persisted_context(event_row: Any) -> dict[str, Any]:
    raw = None
    try:
        raw = event_row["context_json"]
    except (KeyError, IndexError):
        raw = None
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_get(event_row: Any, key: str) -> Any:
    try:
        return event_row[key]
    except (KeyError, IndexError):
        return None


def verify_event_evidence(event_row: Any, evidence: EventEvidence) -> SealedEvidence | LegacyUnversionedEvidence:
    """Checks an event's evidence claim; raises `ContextEvidenceRejected` on any mismatch."""
    event_id = _row_get(event_row, "event_id")
    if isinstance(evidence, LegacyUnversionedEvidence):
        if evidence.source_ref != f"events:{event_id}":
            raise ContextEvidenceRejected(
                event_id, RejectionCode.INVALID_FIELD, "evidence.source_ref", f"{evidence.source_ref!r} is another row"
            )
        return evidence
    if not isinstance(evidence, LinkedEvidence):
        raise ContextEvidenceRejected(event_id, RejectionCode.INVALID_FIELD, "evidence", "not an event evidence result")
    try:
        return verify_link(evidence.link, evidence.record, event_id=event_id, event_asset=_row_get(event_row, "asset"))
    except EvidenceRejected as error:
        raise ContextEvidenceRejected(event_id, error.code, error.field, error.detail) from error


def _evidence_block(verified: SealedEvidence | LegacyUnversionedEvidence) -> dict[str, Any]:
    if isinstance(verified, LegacyUnversionedEvidence):
        # Old row: say so, with only what the row really has. No facts, no hash.
        return {
            "state": verified.state.value,
            "source_ref": verified.source_ref,
            "citable": False,
            "fields_present": list(verified.fields_present),
        }
    block: dict[str, Any] = json.loads(evidence_to_json(verified))
    block["state"] = verified.state.value
    block["citable"] = True
    return block


def build_model_context(event_row: Any, *, evidence: EventEvidence | None = None) -> dict[str, Any]:
    """Reconstructs the context sent to Claude from PERSISTED data only
    (task section 2/3: the Bridge must reconstruct context from what is
    stored, never assume live radar state is still around).

    With `evidence`, the claim is verified first (`verify_event_evidence`) and
    the context gains an `evidence` block; a mismatch raises
    `ContextEvidenceRejected` instead of returning any context. Without it the
    output is unchanged from before T030b.
    """
    verified = verify_event_evidence(event_row, evidence) if evidence is not None else None
    persisted = _load_persisted_context(event_row)

    def row_get(key: str) -> Any:
        return _row_get(event_row, key)

    context: dict[str, Any] = {
        "event_id": row_get("event_id"),
        "asset": persisted.get("asset", row_get("asset")),
        "setup_type": persisted.get("setup_type", row_get("setup_type")),
        "direction": persisted.get("direction", row_get("direction")),
        "market": persisted.get("market", row_get("market")),
        "spot_pair": persisted.get("spot_pair"),
        "futures_symbol": persisted.get("futures_symbol"),
        "current_price": persisted.get("current_price"),
        "anomaly_score": persisted.get("anomaly_score", row_get("anomaly_score")),
        "opportunity_score": persisted.get("opportunity_score", row_get("opportunity_score")),
        "tradeability_score": persisted.get("tradeability_score", row_get("tradeability_score")),
        "tradeability_state": persisted.get("tradeability_state"),
        "opportunity_breakdown": persisted.get("opportunity_breakdown"),
        "tradeability_breakdown": persisted.get("tradeability_breakdown"),
        "l1_features": persisted.get("l1_features"),
        "l2_features": persisted.get("l2_features"),
        "derivatives_coherence": persisted.get("derivatives_coherence"),
        "setup_notes": persisted.get("setup_notes"),
        "cost_preview": persisted.get("cost_preview"),
        "futures": persisted.get("futures"),
        "flags": persisted.get("flags"),
        "qwen": persisted.get("qwen"),
        "router": persisted.get(
            "router",
            {
                "decision": row_get("model_demand"),
                "confidence": row_get("confidence"),
                "reasons": row_get("reason"),
            },
        ),
        "portfolio": {
            "existing_position": UNAVAILABLE,
            "pending_orders": UNAVAILABLE,
            "capital_constraints": UNAVAILABLE,
            "asset_already_in_analysis": UNAVAILABLE,
            "note": PORTFOLIO_NOTE,
        },
    }

    result: dict[str, Any] = deep_mark_unavailable(context)
    if verified is not None:
        # Added after deep_mark_unavailable so sealed evidence stays byte-faithful.
        result["evidence"] = _evidence_block(verified)
    return result


def build_verified_model_context(store: Any, event_id: str) -> dict[str, Any]:
    """Loads an event and its stored evidence from `store` and builds the verified context.

    Any evidence failure - tampered stored row, missing evidence, run/instrument/hash
    mismatch - raises `ContextEvidenceRejected`; there is no empty-context fallback.
    """
    row = store.get_event(event_id)
    if row is None:
        raise ContextEvidenceRejected(event_id, RejectionCode.UNKNOWN_DEPENDENCY, "event_id", "event is not stored")
    try:
        evidence = store.load_event_evidence(event_id)
    except ContextEvidenceRejected:
        raise
    except EvidenceRejected as error:
        raise ContextEvidenceRejected(event_id, error.code, error.field, error.detail) from error
    return build_model_context(row, evidence=evidence)
