"""opportunity_score: "is there structure here that could be a setup?"

This is explicitly NOT anomaly_score ("is this rare?"). It is built from
normalized, bounded [0,1] sub-features combined with configurable weights
(config.OPPORTUNITY_WEIGHTS, sums to 1.0) into a 0-100 number, with an
exhaustion penalty subtracted at the end. No LLM, no order book, no Qwen/Fable.

Every sub-feature function documents the rule it encodes and pulls its
thresholds from config - nothing arbitrary is hidden inline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .anomaly import Features as L1Features
from .l2_features import L2Features
from .l2_features import sign as _sign
from .setups import SetupResult


DERIVATIVES_COHERENCE_LABELS = {
    (1, 1): "PRICE_UP_OI_UP",
    (1, -1): "PRICE_UP_OI_DOWN",
    (-1, 1): "PRICE_DOWN_OI_UP",
    (-1, -1): "PRICE_DOWN_OI_DOWN",
}


@dataclass
class OpportunityResult:
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)
    cost_estimate_bps: float | None = None
    cost_efficiency: float | None = None
    derivatives_coherence: str = "UNKNOWN"
    flags: list[str] = field(default_factory=list)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _direction_sign(direction: str) -> int:
    return {"LONG": 1, "SHORT": -1}.get(direction, 0)


def momentum_credit(l2: L2Features) -> float:
    """abs(1h return in ATR units), scaled to saturate at the configured target."""
    if l2.return_1h_atr is None:
        return 0.0
    return _clip01(abs(l2.return_1h_atr) / config.OPPORTUNITY_MOMENTUM_ATR_TARGET)


def momentum_coherence_credit(l2: L2Features) -> float:
    """1.0 if 5m/15m/1h all agree in sign, 0.5 if two of three agree, else 0."""
    signs = [_sign(l2.return_5m_atr), _sign(l2.return_15m_atr), _sign(l2.return_1h_atr)]
    non_zero = [s for s in signs if s != 0]
    if len(non_zero) < 2:
        return 0.0
    agree = max(non_zero.count(1), non_zero.count(-1))
    if agree == len(non_zero):
        return 1.0
    if agree >= 2:
        return 0.5
    return 0.0


def acceleration_credit(l2: L2Features, direction: int) -> float:
    """Credit when the most recent leg (15m) is stronger than the trailing
    average implied by the 1h return, in the trade's own direction.
    """
    if l2.return_15m_atr is None or l2.return_1h_atr is None or direction == 0:
        return 0.0
    trailing_avg = (l2.return_1h_atr - l2.return_15m_atr) / 3.0  # remaining 3 quarters of the hour
    acceleration = (l2.return_15m_atr - trailing_avg) * direction
    if acceleration <= 0:
        return 0.0
    return _clip01(acceleration / config.OPPORTUNITY_MOMENTUM_ATR_TARGET)


def volume_confirmed_credit(l1: L1Features, l2: L2Features) -> float:
    """Volume expansion only counts if the bar's range also expanded -
    volume without range is absorption, not a confirmed move (doc section 5)."""
    intensity = l1.volume_intensity_15m
    if intensity is None:
        return 0.0
    base = _clip01((intensity - 1.0) / max(config.SETUP_THRESHOLDS["volume_confirm_intensity"] - 1.0, 1e-6))
    if not l2.range_expansion:
        base *= 0.3
    return _clip01(base)


def breakout_distance_credit(l2: L2Features, direction: int) -> float:
    if direction == 0:
        return 0.0
    candidates = [d for d in (l2.breakout_dist_4h_atr, l2.breakout_dist_24h_atr) if d is not None]
    if not candidates:
        return 0.0
    aligned = [d for d in candidates if _sign(d) == direction or d == 0]
    if not aligned:
        return 0.0
    best = max(aligned, key=abs)
    return _clip01(abs(best) / config.OPPORTUNITY_BREAKOUT_ATR_TARGET)


def freshness_credit(l2: L2Features) -> float:
    if l2.freshness is None:
        return 0.0
    return _clip01(l2.freshness)


def relative_strength_credit(l1: L1Features, direction: int) -> float:
    """Idiosyncratic strength vs BTC, credited only when it points the same
    way as the trade direction (an asset lagging BTC isn't relative strength
    for a LONG)."""
    rel = l1.relative_return_vs_btc_15m if l1.relative_return_vs_btc_15m is not None else l1.relative_return_vs_btc_1h
    if rel is None or direction == 0:
        return 0.0
    if _sign(rel) != direction:
        return 0.0
    return _clip01(abs(rel) / 5.0)  # 5 percentage points of relative strength saturates


def classify_derivatives_coherence(price_delta_sign: int, oi_delta: float | None) -> str:
    if oi_delta is None or price_delta_sign == 0:
        return "UNKNOWN"
    return DERIVATIVES_COHERENCE_LABELS.get((price_delta_sign, _sign(oi_delta)), "UNKNOWN")


def derivatives_coherence_credit(l1: L1Features, direction: int) -> tuple[float, str]:
    """New positions forming in the trade's direction (price up + OI up for a
    LONG, price down + OI up for a SHORT) is supportive context. Funding is
    deliberately NOT used here - RAW_UNVERIFIED must not create direction.
    """
    oi_delta = l1.futures_oi_delta_1h if l1.futures_oi_delta_1h is not None else l1.futures_oi_delta_15m
    label = classify_derivatives_coherence(direction, oi_delta)
    if label == "UNKNOWN" or direction == 0:
        return 0.0, label
    supportive = (direction == 1 and label == "PRICE_UP_OI_UP") or (direction == -1 and label == "PRICE_DOWN_OI_UP")
    return (1.0 if supportive else 0.0), label


def squeeze_release_credit(l2: L2Features, setup_type: str) -> float:
    if setup_type == "SQUEEZE_RELEASE":
        return 1.0
    if l2.range_compression and l2.volatility_percentile is not None:
        # Compression alone is not an opportunity by itself (doc explicit) -
        # only partial credit, mostly informative.
        return 0.2
    return 0.0


def estimate_cost_bps(spread_bps: float | None, market: str) -> float:
    fees = config.UNCALIBRATED_FEES
    if market == "FUTURES":
        round_trip_fee_bps = fees["futures_taker_bps"] * 2
    else:
        round_trip_fee_bps = fees["spot_taker_bps"] * 2
    return (spread_bps or 0.0) + round_trip_fee_bps


def cost_efficiency_credit(
    expected_move_pct: float | None, spread_bps: float | None, market: str
) -> tuple[float, float, float]:
    """Preview only (spread + configurable fees), not the full L3
    tradeability score. Returns (credit, cost_bps, efficiency_ratio)."""
    cost_bps = estimate_cost_bps(spread_bps, market)
    expected_move_bps = abs(expected_move_pct or 0.0) * 100.0
    efficiency = expected_move_bps / max(cost_bps, 1e-6)
    credit = _clip01(efficiency / config.OPPORTUNITY_COST_EFFICIENCY_TARGET)
    return credit, cost_bps, efficiency


def compute_opportunity_score(
    l1: L1Features,
    l2: L2Features,
    setup: SetupResult,
    market: str,
    spread_bps: float | None,
) -> OpportunityResult:
    if l2.l2_warmup:
        return OpportunityResult(score=0.0, flags=["l2_warmup"], derivatives_coherence="UNKNOWN")

    direction = _direction_sign(setup.direction)
    flags: list[str] = []

    deriv_credit, deriv_label = derivatives_coherence_credit(l1, direction)
    cost_credit, cost_bps, cost_efficiency = cost_efficiency_credit(l1.return_15m, spread_bps, market)
    if cost_efficiency < 1.0:
        flags.append("below_cost_estimate")

    breakdown = {
        "momentum": momentum_credit(l2),
        "momentum_coherence": momentum_coherence_credit(l2),
        "acceleration": acceleration_credit(l2, direction),
        "volume_confirmed": volume_confirmed_credit(l1, l2),
        "breakout_distance": breakout_distance_credit(l2, direction),
        "freshness": freshness_credit(l2),
        "relative_strength": relative_strength_credit(l1, direction),
        "derivatives_coherence": deriv_credit,
        "squeeze_release": squeeze_release_credit(l2, setup.setup_type),
        "cost_efficiency": cost_credit,
    }

    weighted = sum(breakdown[k] * config.OPPORTUNITY_WEIGHTS[k] for k in breakdown)
    score = weighted * 100.0

    if l2.exhaustion:
        score -= config.OPPORTUNITY_EXHAUSTION_PENALTY
        flags.append("exhaustion_penalty_applied")

    if setup.setup_type == "NONE":
        # No rule-based structure was confirmed - opportunity credit is capped
        # low regardless of raw feature values, so a stray high z-score alone
        # can't masquerade as a setup.
        score = min(score, 20.0)
        flags.append("no_setup_confirmed")

    score = max(0.0, min(100.0, round(score, 2)))

    return OpportunityResult(
        score=score,
        breakdown={k: round(v, 4) for k, v in breakdown.items()},
        cost_estimate_bps=round(cost_bps, 2),
        cost_efficiency=round(cost_efficiency, 3),
        derivatives_coherence=deriv_label,
        flags=flags,
    )
