"""Structured output schema for Sonnet/Fable analysis responses (task section 9).

The LLM only ever produces the analysis fields; `event_id`, `model`,
`model_version` and `timestamp` are metadata the Bridge already knows and
attaches itself after the call - never asked of (or trusted from) the model,
so it can't invent its own event_id or claim a different model ran it.

`recommendation` is a closed enum (task section 9: "É uma PROPOSTA DE ANÁLISE"
- never an execution authorization) and deliberately includes weak/negative
outcomes so the model is never forced to manufacture a trade idea.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DIRECTIONS = ("LONG", "SHORT", "NONE")
MARKETS = ("SPOT", "FUTURES", "BOTH", "NONE")
SETUP_TYPES = ("BREAKOUT", "CONTINUATION", "REVERSAL", "SQUEEZE_RELEASE", "EXHAUSTION", "NONE")
CONFIDENCES = ("LOW", "MEDIUM", "HIGH")
RECOMMENDATIONS = (
    "STRONG_OPPORTUNITY",
    "MODERATE_OPPORTUNITY",
    "WEAK_OPPORTUNITY",
    "INVALIDATED",
    "WAIT",
    "NO_TRADE",
)

_NULLABLE_NUMBER = {"type": ["number", "null"]}

ANALYSIS_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "asset": {"type": "string"},
        "market": {"type": "string", "enum": list(MARKETS)},
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "setup_type": {"type": "string", "enum": list(SETUP_TYPES)},
        "thesis": {"type": "string"},
        "entry": _NULLABLE_NUMBER,
        "entry_range": {
            "type": ["object", "null"],
            "properties": {"low": _NULLABLE_NUMBER, "high": _NULLABLE_NUMBER},
            "required": ["low", "high"],
            "additionalProperties": False,
        },
        "stop": _NULLABLE_NUMBER,
        "tp1": _NULLABLE_NUMBER,
        "tp2": _NULLABLE_NUMBER,
        "leverage": _NULLABLE_NUMBER,
        "margin": _NULLABLE_NUMBER,
        "notional": _NULLABLE_NUMBER,
        "max_loss": _NULLABLE_NUMBER,
        "expected_profit": _NULLABLE_NUMBER,
        "net_rr": _NULLABLE_NUMBER,
        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
        "risks": {"type": "array", "items": {"type": "string"}},
        "invalidation": {"type": "string"},
        "alternatives": {"type": "array", "items": {"type": "string"}},
        "capital_status": {"type": "string"},
        "recommendation": {"type": "string", "enum": list(RECOMMENDATIONS)},
        "reasoning_summary": {"type": "string"},
    },
    "required": [
        "asset", "market", "direction", "setup_type", "thesis", "entry", "entry_range",
        "stop", "tp1", "tp2", "leverage", "margin", "notional", "max_loss", "expected_profit",
        "net_rr", "confidence", "risks", "invalidation", "alternatives", "capital_status",
        "recommendation", "reasoning_summary",
    ],
    "additionalProperties": False,
}


@dataclass
class ModelAnalysis:
    asset: str
    market: str
    direction: str
    setup_type: str
    thesis: str
    entry: float | None
    entry_range: dict[str, float | None] | None
    stop: float | None
    tp1: float | None
    tp2: float | None
    leverage: float | None
    margin: float | None
    notional: float | None
    max_loss: float | None
    expected_profit: float | None
    net_rr: float | None
    confidence: str
    risks: list[str]
    invalidation: str
    alternatives: list[str]
    capital_status: str
    recommendation: str
    reasoning_summary: str


def validate_analysis(parsed: Any, expected_asset: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Defense in depth beyond `output_config.format` schema enforcement:
    confirms the model didn't quietly answer for a different asset, and that
    every enum is one of the closed values this radar understands. Returns
    (validated_dict, None) or (None, error_message).
    """
    if not isinstance(parsed, dict):
        return None, "response is not a JSON object"

    if expected_asset is not None and parsed.get("asset") != expected_asset:
        return None, f"response asset {parsed.get('asset')!r} does not match requested asset {expected_asset!r}"

    for field_name, allowed in (
        ("market", MARKETS),
        ("direction", DIRECTIONS),
        ("setup_type", SETUP_TYPES),
        ("confidence", CONFIDENCES),
        ("recommendation", RECOMMENDATIONS),
    ):
        if parsed.get(field_name) not in allowed:
            return None, f"invalid {field_name}: {parsed.get(field_name)!r}"

    if not isinstance(parsed.get("risks"), list) or not isinstance(parsed.get("alternatives"), list):
        return None, "risks/alternatives must be arrays"

    return parsed, None
