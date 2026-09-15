"""tradeability_score (L3, finalists): a GATE, not a bonus added to
opportunity_score. An asset with inadequate liquidity must not reach Qwen
just because opportunity_score is high (task section 3, explicit).

States: TRADEABLE | CONSTRAINED | UNTRADEABLE.

Also builds the SPOT/FUTURES cost preview (task section 4): spread + fee +
slippage, with `net_move_required` as the amplitude needed to clear known
costs - never a return prediction. Fees stay UNCALIBRATED until validated;
funding stays RAW_UNVERIFIED and never enters the score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import config
from .microstructure import DepthMetrics, TradesMetrics

TRADEABILITY_STATES = ("TRADEABLE", "CONSTRAINED", "UNTRADEABLE")


@dataclass
class TradeabilityResult:
    score: float
    state: str
    breakdown: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _spread_credit(spread_bps: float | None) -> float:
    if spread_bps is None:
        return 0.0
    return _clip01(1.0 - spread_bps / (config.TRADEABILITY_SPREAD_TARGET_BPS * 2.0))


def _depth_credit(depth: DepthMetrics | None, bid_usd_l0: float, ask_usd_l0: float) -> tuple[float, list[str]]:
    flags: list[str] = []
    if depth is not None:
        usable = min(depth.bid_depth_usd_1pct, depth.ask_depth_usd_1pct)
        if depth.quality == "THIN_BOOK":
            flags.append("THIN_BOOK")
        return _clip01(usable / config.TRADEABILITY_DEPTH_TARGET_USD), flags
    flags.append("ORDER_BOOK_UNAVAILABLE")
    # Level-1-only proxy from the cheap global ticker - degraded confidence,
    # never treated as equivalent to a real book (doc section 16: keep the
    # candidate with a data-quality flag, never fabricate the missing data).
    proxy = min(bid_usd_l0, ask_usd_l0)
    return _clip01(proxy / config.TRADEABILITY_DEPTH_TARGET_USD) * 0.5, flags


def _slippage_credit(depth: DepthMetrics | None) -> float:
    if depth is None:
        return 0.0
    candidates = [s for s in (depth.slippage_buy_bps, depth.slippage_sell_bps) if s is not None]
    if not candidates:
        return 0.0
    worst = max(candidates)
    return _clip01(1.0 - worst / (config.TRADEABILITY_SLIPPAGE_TARGET_BPS * 2.0))


def _imbalance_credit(depth: DepthMetrics | None) -> float:
    if depth is None or depth.imbalance is None:
        return 0.7  # neutral default - absence of data is not itself a penalty
    return _clip01(1.0 - abs(depth.imbalance))


def _activity_credit(trades: TradesMetrics | None) -> tuple[float, list[str]]:
    flags: list[str] = []
    if trades is None:
        flags.append("TRADES_UNAVAILABLE")
        return 0.0, flags
    if trades.time_covered_seconds and trades.time_covered_seconds > 0:
        trades_per_hour = trades.trade_count / (trades.time_covered_seconds / 3600.0)
    else:
        trades_per_hour = float(trades.trade_count)
    return _clip01(trades_per_hour / config.TRADEABILITY_ACTIVITY_TARGET_TRADES_PER_HOUR), flags


def _futures_available_credit(futures_available: bool) -> float:
    return 1.0 if futures_available else 0.0


def _futures_quality_credit(
    futures_available: bool, futures_spread_bps: float | None, futures_volume_24h_usd: float | None
) -> float:
    if not futures_available:
        return 0.0
    spread_component = _spread_credit(futures_spread_bps) if futures_spread_bps is not None else 0.5
    volume_component = _clip01((futures_volume_24h_usd or 0.0) / config.TRADEABILITY_DEPTH_TARGET_USD / 10.0)
    return (spread_component + volume_component) / 2.0


def _freshness_credit(freshness: float | None) -> float:
    return 0.5 if freshness is None else _clip01(freshness)


def compute_tradeability(
    spread_bps: float | None,
    depth: DepthMetrics | None,
    trades: TradesMetrics | None,
    bid_usd_l0: float,
    ask_usd_l0: float,
    market_status: str,
    futures_available: bool,
    futures_spread_bps: float | None,
    futures_volume_24h_usd: float | None,
    freshness: float | None,
) -> TradeabilityResult:
    flags: list[str] = []

    depth_credit, depth_flags = _depth_credit(depth, bid_usd_l0, ask_usd_l0)
    activity_credit, activity_flags = _activity_credit(trades)
    flags.extend(depth_flags)
    flags.extend(activity_flags)

    breakdown = {
        "spread": _spread_credit(spread_bps),
        "depth": depth_credit,
        "slippage": _slippage_credit(depth),
        "book_imbalance_penalty": _imbalance_credit(depth),
        "activity": activity_credit,
        "futures_available": _futures_available_credit(futures_available),
        "futures_quality": _futures_quality_credit(futures_available, futures_spread_bps, futures_volume_24h_usd),
        "freshness": _freshness_credit(freshness),
    }

    weighted = sum(breakdown[k] * config.TRADEABILITY_WEIGHTS[k] for k in breakdown)
    score = max(0.0, min(100.0, round(weighted * 100.0, 2)))

    hard_veto = (
        (spread_bps is not None and spread_bps > config.TRADEABILITY_HARD_MAX_SPREAD_BPS)
        or market_status in config.NON_TRADEABLE_STATUSES
    )

    if hard_veto or score <= config.TRADEABILITY_UNTRADEABLE_MAX:
        state = "UNTRADEABLE"
    elif score <= config.TRADEABILITY_CONSTRAINED_MAX:
        state = "CONSTRAINED"
    else:
        state = "TRADEABLE"

    if hard_veto:
        flags.append("hard_veto")

    return TradeabilityResult(
        score=score,
        state=state,
        breakdown={k: round(v, 4) for k, v in breakdown.items()},
        flags=flags,
    )


def build_cost_preview(
    market: str,
    spot_spread_bps: float | None,
    spot_depth: DepthMetrics | None,
    futures_available: bool,
    futures_spread_bps: float | None,
    futures_depth: DepthMetrics | None,
    funding_rate_raw: float | None,
) -> dict[str, Any]:
    """Preliminary cost preview, SPOT vs FUTURES kept separate (task section
    4). `net_move_required_pct` is the amplitude needed to clear KNOWN costs
    for the executable venue - never a return forecast.
    """
    fees = config.UNCALIBRATED_FEES

    def _venue_preview(spread_bps, depth, taker_fee_bps):
        round_trip_fee_bps = taker_fee_bps * 2.0
        slippage_candidates = [
            s for s in ((depth.slippage_buy_bps if depth else None), (depth.slippage_sell_bps if depth else None))
            if s is not None
        ]
        slippage_bps = max(slippage_candidates) if slippage_candidates else None
        total_bps = (spread_bps or 0.0) + round_trip_fee_bps + (slippage_bps or 0.0)
        return {
            "spread_bps": round(spread_bps, 3) if spread_bps is not None else None,
            "fee_bps": round(round_trip_fee_bps, 3),
            "slippage_bps": round(slippage_bps, 3) if slippage_bps is not None else None,
            "slippage_source": "order_book" if slippage_bps is not None else "UNAVAILABLE",
            "total_cost_bps": round(total_bps, 3),
            "net_move_required_pct": round(total_bps / 100.0, 4),
            "fee_status": "UNCALIBRATED",
        }

    spot = _venue_preview(spot_spread_bps, spot_depth, fees["spot_taker_bps"])

    futures = None
    if futures_available:
        futures = _venue_preview(futures_spread_bps, futures_depth, fees["futures_taker_bps"])
        futures["funding_raw"] = funding_rate_raw
        futures["funding_semantics"] = "RAW_UNVERIFIED"

    return {
        "spot": spot,
        "futures": futures,
        "preferred_venue": market,
    }
