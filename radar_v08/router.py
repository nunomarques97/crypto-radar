"""Demand Router: deterministic IGNORE | SONNET | FABLE decision.

Never OPUS (task section 7/13/20). This module never touches the network,
SQLite, or an LLM - it is a pure function over already-computed scores plus
the (optional) Qwen review, so it is fully unit-testable. Cooldown and
budget are applied by the caller (`heartbeat.py` via `cooldown.py`/`budgets.py`)
AFTER this decision, because they need `now` and the store.

`model_demand_score` (task section 10) answers "how much value would we
expect from a deeper analysis?" - it is explicitly NOT anomaly_score,
opportunity_score, or tradeability_score, and is documented as such wherever
it appears in the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config

DECISIONS = ("IGNORE", "SONNET", "FABLE")


@dataclass
class RouterContext:
    asset: str
    anomaly_score: float | None
    opportunity_score: float | None
    tradeability_score: float | None
    tradeability_state: str  # TRADEABLE | CONSTRAINED | UNTRADEABLE
    setup_type: str
    direction: str
    momentum_1h_atr: float | None
    momentum_coherence: float  # 0/0.5/1.0, from opportunity.breakdown
    volume_intensity_15m: float | None
    range_expansion: bool | None
    breakout_state: str
    derivatives_coherence_credit: float  # 0.0/1.0, from opportunity.breakdown
    taker_buy_ratio: float | None  # from L3 trades, APPROXIMATE
    qwen_status: str  # OK | INVALID_JSON | TIMEOUT | UNAVAILABLE
    qwen_veto: bool = False
    qwen_call_sonnet: bool = False
    qwen_call_fable: bool = False
    qwen_confidence: str | None = None
    qwen_direction: str | None = None


@dataclass
class RouterResult:
    decision: str
    model_demand_score: float
    confidence: str
    confirmations: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _direction_sign(direction: str) -> int:
    return {"LONG": 1, "SHORT": -1}.get(direction, 0)


def count_confirmations(ctx: RouterContext) -> tuple[int, list[str]]:
    """Independent confirmations (task section 7). Each is a distinct,
    already-computed signal - nothing here re-derives a number.
    """
    names: list[str] = []

    if ctx.momentum_1h_atr is not None and abs(ctx.momentum_1h_atr) >= 2.0 and ctx.momentum_coherence >= 1.0:
        names.append("momentum_2atr_coherent")

    if (
        ctx.volume_intensity_15m is not None
        and ctx.volume_intensity_15m >= 3.0
        and bool(ctx.range_expansion)
    ):
        names.append("volume_3x_range_confirmed")

    if ctx.breakout_state in ("BREAKOUT_UP", "BREAKOUT_DOWN") and (
        ctx.volume_intensity_15m is not None and ctx.volume_intensity_15m >= config.SETUP_THRESHOLDS["volume_confirm_intensity"]
    ):
        names.append("breakout_with_volume")

    if ctx.setup_type == "SQUEEZE_RELEASE":
        names.append("squeeze_release")

    if ctx.derivatives_coherence_credit >= 1.0:
        names.append("derivatives_coherent")

    direction_sign = _direction_sign(ctx.direction)
    if (
        ctx.taker_buy_ratio is not None
        and direction_sign != 0
        and (
            (direction_sign == 1 and ctx.taker_buy_ratio >= config.ROUTER_TAKER_IMBALANCE_CONFIRM_RATIO)
            or (direction_sign == -1 and (1.0 - ctx.taker_buy_ratio) >= config.ROUTER_TAKER_IMBALANCE_CONFIRM_RATIO)
        )
    ):
        names.append("taker_imbalance")

    if ctx.qwen_status == "OK" and ctx.qwen_call_fable and ctx.qwen_confidence == "HIGH":
        names.append("qwen_call_fable_high")

    return len(names), names


def compute_confidence(data_quality_ok: bool, confirmations_count: int) -> str:
    if not data_quality_ok:
        return "LOW"
    if confirmations_count >= 3:
        return "HIGH"
    if confirmations_count >= 1:
        return "MEDIUM"
    return "LOW"


def _model_demand_score(ctx: RouterContext, confirmations_count: int) -> float:
    """"How much value we expect from a deeper model" - deliberately built
    from confirmations + opportunity/tradeability quality, not a copy of
    opportunity_score itself (task section 10: must not be confused with it).
    """
    opp = ctx.opportunity_score or 0.0
    tradeability_component = {"TRADEABLE": 1.0, "CONSTRAINED": 0.5, "UNTRADEABLE": 0.0}.get(ctx.tradeability_state, 0.0)
    confirmation_component = min(confirmations_count / 4.0, 1.0)
    score = (0.5 * (opp / 100.0) + 0.3 * confirmation_component + 0.2 * tradeability_component) * 100.0
    return round(max(0.0, min(100.0, score)), 2)


def route(ctx: RouterContext, data_quality_ok: bool = True) -> RouterResult:
    reasons: list[str] = []

    if ctx.tradeability_state == "UNTRADEABLE":
        return RouterResult("IGNORE", 0.0, "LOW", [], ["tradeability_untradeable"])

    if ctx.setup_type == "NONE":
        return RouterResult("IGNORE", 0.0, "LOW", [], ["no_setup_confirmed"])

    if ctx.qwen_status == "OK" and ctx.qwen_veto and ctx.qwen_confidence == "HIGH":
        return RouterResult("IGNORE", 0.0, "LOW", [], ["qwen_veto_high_confidence"])

    confirmations_count, confirmation_names = count_confirmations(ctx)
    confidence = compute_confidence(data_quality_ok, confirmations_count)
    demand_score = _model_demand_score(ctx, confirmations_count)

    min_fable_confirmations = (
        config.ROUTER_FABLE_MIN_CONFIRMATIONS
        if ctx.qwen_status == "OK"
        else config.ROUTER_FABLE_MIN_CONFIRMATIONS_NO_QWEN
    )

    signal_conflict = bool(
        ctx.qwen_status == "OK"
        and ctx.qwen_direction not in (None, "NONE")
        and ctx.direction != "NONE"
        and ctx.qwen_direction != ctx.direction
    )
    if signal_conflict:
        reasons.append("signal_conflict_qwen_vs_deterministic")

    qwen_wants_fable = ctx.qwen_status == "OK" and ctx.qwen_call_fable
    qwen_wants_sonnet = ctx.qwen_status == "OK" and ctx.qwen_call_sonnet

    fable_by_score = (
        ctx.opportunity_score is not None
        and ctx.opportunity_score >= config.ROUTER_FABLE_MIN_OPPORTUNITY
        and confirmations_count >= min_fable_confirmations
    )

    if fable_by_score or qwen_wants_fable or signal_conflict:
        if fable_by_score:
            reasons.append(f"opportunity>={config.ROUTER_FABLE_MIN_OPPORTUNITY} with {confirmations_count} confirmations")
        if qwen_wants_fable:
            reasons.append("qwen_recommended_fable")
        if signal_conflict:
            reasons.append("material_conflict_requires_deep_analysis")
        reasons.extend(f"confirmation:{n}" for n in confirmation_names)
        return RouterResult("FABLE", demand_score, confidence, confirmation_names, reasons)

    sonnet_by_score = (
        ctx.opportunity_score is not None
        and ctx.opportunity_score >= config.ROUTER_SONNET_MIN_OPPORTUNITY
        and ctx.tradeability_state in ("TRADEABLE", "CONSTRAINED")
    )

    if sonnet_by_score or qwen_wants_sonnet:
        if sonnet_by_score:
            reasons.append(f"opportunity>={config.ROUTER_SONNET_MIN_OPPORTUNITY}, tradeability={ctx.tradeability_state}")
        if qwen_wants_sonnet:
            reasons.append("qwen_recommended_sonnet")
        reasons.extend(f"confirmation:{n}" for n in confirmation_names)
        return RouterResult("SONNET", demand_score, confidence, confirmation_names, reasons)

    reasons.append("below_router_thresholds")
    return RouterResult("IGNORE", demand_score, confidence, confirmation_names, reasons)
