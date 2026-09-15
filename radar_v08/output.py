"""Output JSON writer: radar_v08_output.json (schema per architecture doc).

Phase 2 adds opportunity_score, setup{type,direction}, L2 features and a
tradeability preview (not the full L3 tradeability_score) on top of the
Phase 1 anomaly_score/features. Still no Qwen, Fable, or order book fields.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

from .anomaly import AnomalyResult
from .l2 import L2Result

try:  # Phase 3 types - optional so Phase 1/2 callers/tests are unaffected.
    from .l3 import L3Result
    from .qwen import QwenReview
    from .router import RouterResult
except ImportError:  # pragma: no cover
    L3Result = Any  # type: ignore[assignment,misc]
    QwenReview = Any  # type: ignore[assignment,misc]
    RouterResult = Any  # type: ignore[assignment,misc]


def build_candidate(
    asset: str,
    spot_pair: str | None,
    futures_symbol: str | None,
    result: AnomalyResult,
    flags: list[str],
    l2_result: L2Result | None = None,
    l3_result: "L3Result | None" = None,
    qwen_review: "QwenReview | None" = None,
    router_result: "RouterResult | None" = None,
    event_id: str | None = None,
    event_status: str | None = None,
) -> dict[str, Any]:
    features = {k: v for k, v in asdict(result.features).items()}
    all_flags = set(flags) | set(result.flags)
    candidate: dict[str, Any] = {
        "asset": asset,
        "spot_pair": spot_pair,
        "futures_symbol": futures_symbol,
        "anomaly_score": result.anomaly_score,
        "opportunity_score": None,
        "warmup": result.warmup,
        "setup": {"type": "NONE", "direction": "NONE"},
        "features": features,
        "tradeability_preview": None,
        "tradeability_score": None,
        "tradeability_state": None,
        "qwen": None,
        "model_demand": None,
        "model_demand_score": None,
        "flags": [],
    }

    if l2_result is not None:
        features.update({k: v for k, v in l2_result.l2_features.__dict__.items() if k != "flags"})
        candidate["opportunity_score"] = l2_result.opportunity.score
        candidate["opportunity_breakdown"] = l2_result.opportunity.breakdown
        candidate["cost_preview"] = {
            "cost_estimate_bps": l2_result.opportunity.cost_estimate_bps,
            "cost_efficiency": l2_result.opportunity.cost_efficiency,
        }
        candidate["derivatives_coherence"] = l2_result.opportunity.derivatives_coherence
        candidate["setup"] = {"type": l2_result.setup.setup_type, "direction": l2_result.setup.direction}
        candidate["tradeability_preview"] = l2_result.tradeability_preview
        all_flags |= set(l2_result.flags) | set(l2_result.opportunity.flags)

    if l3_result is not None:
        candidate["tradeability_score"] = l3_result.tradeability.score
        candidate["tradeability_state"] = l3_result.tradeability.state
        candidate["tradeability_breakdown"] = l3_result.tradeability.breakdown
        candidate["cost_preview"] = l3_result.cost_preview
        all_flags |= set(l3_result.flags) | set(l3_result.tradeability.flags)

    if qwen_review is not None:
        candidate["qwen"] = {
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

    if router_result is not None:
        candidate["model_demand"] = router_result.decision
        candidate["model_demand_score"] = router_result.model_demand_score
        candidate["confidence"] = router_result.confidence
        candidate["router_reasons"] = router_result.reasons
        candidate["router_confirmations"] = router_result.confirmations

    if event_id is not None:
        candidate["event_id"] = event_id
        candidate["event_status"] = event_status

    candidate["flags"] = sorted(all_flags)
    return candidate


def build_output(
    run_id: str,
    timestamp: str,
    mode: str,
    warmup: bool,
    universe: dict[str, Any],
    funnel: dict[str, Any],
    data_quality: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "0.8",
        "run_id": run_id,
        "timestamp": timestamp,
        "mode": mode,
        "warmup": warmup,
        "universe": universe,
        "funnel": funnel,
        "data_quality": data_quality,
        "candidates": candidates,
    }


def write_json(payload: dict[str, Any], path: str) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp_path, path)
