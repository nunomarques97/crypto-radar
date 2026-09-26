"""tradeability_score (L3, finalists): a GATE, not a bonus added to
opportunity_score. An asset with inadequate liquidity must not reach Qwen
just because opportunity_score is high (task section 3, explicit).

States: TRADEABLE | CONSTRAINED | UNTRADEABLE.

Also builds the SPOT/FUTURES cost preview (task section 4) as a thin adapter
over radar_v08.domain.costs: two legs, both sides, spread + fee + slippage,
with `net_move_required` as the amplitude needed to clear the round-trip
costs - never a return prediction. A missing component makes the preview
COST_INCOMPLETE (no total), never a zero. Fees stay UNCALIBRATED until
validated; funding stays RAW_UNVERIFIED and never enters the score or the cost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Context, Decimal, Inexact
from typing import Any

from . import config
from .domain import costs as cost_domain
from .domain.integrity import InstrumentKind
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


#: Float round-off tolerance of microstructure's book walk (vwap from summed floats):
#: a slippage in [-1e-9, 0) bps is exactly zero; anything more negative is unusable.
_SLIPPAGE_FLOAT_NOISE_BPS = Decimal("1e-9")


def _legacy_decimal(value: float | None) -> Decimal | None:
    """Explicit float -> Decimal conversion of a live legacy measurement (shortest
    round-trip repr). The measurement stays a float statistic; this only makes the
    conversion visible at the monetary boundary. Non-finite or absent -> None."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return Decimal(repr(float(value)))


def _venue_scenarios(
    kind: InstrumentKind, depth: DepthMetrics | None, taker_fee_bps: float
) -> dict[cost_domain.Side, cost_domain.CostScenario]:
    """Long and short round-trip scenarios for one venue, priced by radar_v08.domain.costs.

    DepthMetrics carries no size of its own: its slippage was walked at
    config.REFERENCE_ORDER_SIZE_USD by the L3 caller (quote units of that book), so the
    scenario size is that same configured reference, never an account size. The
    instrument's symbol and quote currency are not known at this seam, so the scenario is
    priced in bps of the reference notional only (no money projection).
    """
    size_value = _legacy_decimal(config.REFERENCE_ORDER_SIZE_USD)
    if size_value is None:
        raise ValueError("config.REFERENCE_ORDER_SIZE_USD must be a finite number")
    size = cost_domain.ScenarioSize(size_value, cost_domain.SizeProvenance.REFERENCE_CONFIG)
    instrument = cost_domain.CostInstrument(kind=kind, symbol=None, quote_currency=None)

    unavailable = cost_domain.Missing(
        cost_domain.MissingReason.NOT_OBSERVED, "order book unavailable for this venue"
    )
    spread: cost_domain.SpreadInput | cost_domain.Missing = unavailable
    walked: dict[str, cost_domain.SlippageInput | cost_domain.Missing] = {
        "buy": unavailable,
        "sell": unavailable,
    }
    if depth is not None:
        coverage = (
            cost_domain.DepthCoverage.FULL
            if depth.depth_available_at_reference
            else cost_domain.DepthCoverage.PARTIAL
        )
        book_spread = _legacy_decimal(depth.spread_bps)
        if book_spread is not None:
            spread = cost_domain.SpreadInput(book_spread, "order book touch spread (legacy float measurement)")
        for name, raw, book_side in (
            ("buy", depth.slippage_buy_bps, "asks"),
            ("sell", depth.slippage_sell_bps, "bids"),
        ):
            value = _legacy_decimal(raw)
            if value is not None and value < 0:
                # A walk that starts at the touch cannot fill better than the touch; a
                # negative result is float noise of the legacy walk (|x| <= tolerance,
                # exactly zero slippage) or an inconsistent measurement (missing).
                value = Decimal(0) if value >= -_SLIPPAGE_FLOAT_NOISE_BPS else None
            if value is None:
                walked[name] = cost_domain.Missing(
                    cost_domain.MissingReason.NOT_OBSERVED,
                    f"no usable {name} slippage from the {book_side}",
                )
            else:
                walked[name] = cost_domain.SlippageInput(
                    bps=value,
                    basis=cost_domain.SlippageBasis.FROM_TOUCH,
                    measured_notional=size.notional,
                    coverage=coverage,
                    source=f"order book walk of the {book_side} from the touch (legacy float measurement)",
                )

    fee_value = _legacy_decimal(taker_fee_bps)
    fee: cost_domain.FeeInput | cost_domain.Missing
    if fee_value is None:
        fee = cost_domain.Missing(cost_domain.MissingReason.FEE_UNKNOWN, "taker fee not configured")
    else:
        fee = cost_domain.FeeInput(
            fee_value, cost_domain.FeeBasis.UNCALIBRATED_ASSUMPTION, "config.UNCALIBRATED_FEES taker rate"
        )

    return {
        side: cost_domain.price_round_trip(
            cost_domain.CostScenarioInput(
                instrument=instrument,
                side=side,
                size=size,
                spread_convention=cost_domain.SpreadConvention.HALF_SPREAD_PLUS_TOUCH_SLIPPAGE,
                spread=spread,
                buy_slippage=walked["buy"],
                sell_slippage=walked["sell"],
                entry_fee=fee,
                exit_fee=fee,
                # Immediate round trip: no funding timestamp is crossed. funding_raw
                # stays RAW_UNVERIFIED next to the scenario and is never costed.
                funding_intervals=0,
                funding=None,
            )
        )
        for side in cost_domain.Side
    }


def venue_cost_scenarios(
    kind: InstrumentKind, depth: DepthMetrics | None, taker_fee_bps: float
) -> dict[cost_domain.Side, cost_domain.CostScenario]:
    """Public accessor for outcome tracking: the same `cost_domain.CostScenario` objects
    `_venue_scenarios` builds for `build_cost_preview` and `build_cost_scenario_detail`,
    one per `cost_domain.Side`, with nothing recalculated and no change to either
    function's signature or return shape. A caller that keeps the returned objects (e.g.
    to later derive `RecordedCost` for outcome tracking) still gets an `INCOMPLETE`
    `CostScenario` as-is when a component is missing - it is never partially summed or
    filled in from the other side."""
    return _venue_scenarios(kind, depth, taker_fee_bps)


def _decimal_text(value: Decimal) -> str:
    """Exact plain-text form of a Decimal (trailing zeros dropped, never rounded)."""
    exact = Context(prec=cost_domain.COST_CONTEXT.prec, traps=[Inexact])
    return format(value.normalize(context=exact), "f")


def _leg_name(leg: cost_domain.Leg | None) -> str | None:
    return leg.value if leg is not None else None


#: Decimal places of every presented cost figure, rounded toward more cost (ROUND_CEILING).
_PRESENTED_BPS_PLACES = 3
_PRESENTED_PCT_PLACES = 4


def _presented_bps(value: Decimal) -> float:
    """Presentation projection of an exact bps value: ROUND_CEILING to 3 places."""
    return float(cost_domain.present(value, _PRESENTED_BPS_PLACES))


def _scenario_dict(scenario: cost_domain.CostScenario) -> dict[str, Any]:
    """JSON-safe itemisation of one side: exact Decimals as strings (never floats), each
    next to its presented projection (string, ROUND_CEILING to 3 places)."""
    total = scenario.total_bps
    return {
        "status": scenario.status.value,
        "total_bps": _decimal_text(total) if total is not None else None,
        "total_bps_presented": (
            str(cost_domain.present(total, _PRESENTED_BPS_PLACES)) if total is not None else None
        ),
        "lines": [
            {
                "component": line.component.value,
                "leg": _leg_name(line.leg),
                "direction": line.direction.value if line.direction is not None else None,
                "bps": _decimal_text(line.bps),
                "bps_presented": str(cost_domain.present(line.bps, _PRESENTED_BPS_PLACES)),
                "source": line.source,
            }
            for line in scenario.lines
        ],
        "missing": [
            {
                "component": item.component.value,
                "leg": _leg_name(item.leg),
                "reason": item.reason.value,
                "detail": item.detail,
            }
            for item in scenario.missing
        ],
        "not_applicable": [
            {"component": item.component.value, "leg": _leg_name(item.leg), "reason": item.reason.value}
            for item in scenario.not_applicable
        ],
    }


def _venue_preview(
    kind: InstrumentKind,
    spread_bps: float | None,
    depth: DepthMetrics | None,
    taker_fee_bps: float,
) -> dict[str, Any]:
    """Compact per-venue preview: the legacy keys plus cost_status, missing_components and
    the presented total of each side. This dict travels in `cost_preview` to the output
    file, the model context and every finalist of the live local Qwen payload
    (heartbeat._build_qwen_payload), so it carries no itemisation, no per-line sources and
    no exact strings - those live in build_cost_scenario_detail, which nothing sends."""
    scenarios = _venue_scenarios(kind, depth, taker_fee_bps)
    totals = [s.total_bps for s in scenarios.values()]

    total_bps: float | None = None
    net_move_required_pct: float | None = None
    slippage_round_trip: float | None = None
    complete_totals = [t for t in totals if t is not None]
    complete = len(complete_totals) == len(totals)
    if complete:
        # Direction is not known at preview time: report the worse side.
        worst = max(complete_totals)
        total_bps = _presented_bps(worst)
        net_move_required_pct = float(cost_domain.present(worst.scaleb(-2), _PRESENTED_PCT_PLACES))
        slippage_by_side = [
            sum(
                (line.bps for line in s.lines if line.component is cost_domain.CostComponent.SLIPPAGE),
                Decimal(0),
            )
            for s in scenarios.values()
        ]
        slippage_round_trip = _presented_bps(max(slippage_by_side))

    missing = sorted(
        {
            f"{item.component.value}:{_leg_name(item.leg) or '-'}:{item.reason.value}"
            for s in scenarios.values()
            for item in s.missing
        }
    )
    buy_bps = depth.slippage_buy_bps if depth is not None else None
    sell_bps = depth.slippage_sell_bps if depth is not None else None
    return {
        "spread_bps": round(spread_bps, 3) if spread_bps is not None else None,
        "fee_bps": round(taker_fee_bps * 2.0, 3),
        "slippage_bps": slippage_round_trip,
        "slippage_buy_bps": round(buy_bps, 3) if buy_bps is not None else None,
        "slippage_sell_bps": round(sell_bps, 3) if sell_bps is not None else None,
        "slippage_source": "order_book" if (buy_bps is not None or sell_bps is not None) else "UNAVAILABLE",
        "total_cost_bps": total_bps,
        "net_move_required_pct": net_move_required_pct,
        "fee_status": "UNCALIBRATED",
        "cost_status": (cost_domain.CostStatus.COMPLETE if complete else cost_domain.CostStatus.INCOMPLETE).value,
        "missing_components": missing,
        "total_cost_bps_by_side": {
            side.value: (_presented_bps(s.total_bps) if s.total_bps is not None else None)
            for side, s in scenarios.items()
        },
    }


def _venue_detail(kind: InstrumentKind, depth: DepthMetrics | None, taker_fee_bps: float) -> dict[str, Any]:
    scenarios = _venue_scenarios(kind, depth, taker_fee_bps)
    reference = scenarios[cost_domain.Side.LONG]
    return {
        "policy_version": reference.policy_version,
        "spread_convention": reference.spread_convention.value,
        "instrument": {"kind": kind.value, "symbol": None, "quote_currency": None},
        "size": {
            "notional": _decimal_text(reference.size.notional),
            "unit": "quote currency of the walked order book (not identified at this seam)",
            "provenance": reference.size.provenance.value,
        },
        "funding_intervals": reference.funding_intervals,
        "fees_calibrated": reference.fees_calibrated,
        "rounding": (
            "exact values are Decimal strings of legacy float measurements (float noise "
            "included); *_presented values are rounded toward more cost (ROUND_CEILING, 3 places)"
        ),
        "sides": {side.value: _scenario_dict(s) for side, s in scenarios.items()},
    }


def build_cost_preview(
    market: str,
    spot_spread_bps: float | None,
    spot_depth: DepthMetrics | None,
    futures_available: bool,
    futures_spread_bps: float | None,
    futures_depth: DepthMetrics | None,
    funding_rate_raw: float | None,
) -> dict[str, Any]:
    """Preliminary cost preview, SPOT vs FUTURES kept separate (task section 4),
    priced by the exact two-leg domain in radar_v08.domain.costs.

    `total_cost_bps` / `net_move_required_pct` are the amplitude needed to clear the
    round-trip costs of the worse side (long or short) at the reference size - never a
    return forecast. When any component is unknown (no book, no slippage on a side, a
    book that did not cover the size) `cost_status` is COST_INCOMPLETE and both are None:
    a missing cost is never summed as zero (RISK.md "Current defects"). `spread_bps` is
    the caller's quoted spread, shown as before; the scenario itself uses the spread of
    the same book the slippage was walked on. `slippage_bps` is the round-trip slippage
    (entry + exit leg), no longer the maximum of buy and sell.

    The result is deliberately compact: it reaches every finalist of the live local Qwen
    payload. The itemised scenario is build_cost_scenario_detail, kept apart.
    """
    fees = config.UNCALIBRATED_FEES

    spot = _venue_preview(InstrumentKind.SPOT, spot_spread_bps, spot_depth, fees["spot_taker_bps"])

    futures = None
    if futures_available:
        futures = _venue_preview(
            InstrumentKind.FUTURES, futures_spread_bps, futures_depth, fees["futures_taker_bps"]
        )
        futures["funding_raw"] = funding_rate_raw
        futures["funding_semantics"] = "RAW_UNVERIFIED"

    return {
        "spot": spot,
        "futures": futures,
        "preferred_venue": market,
    }


def build_cost_scenario_detail(
    spot_depth: DepthMetrics | None,
    futures_available: bool,
    futures_depth: DepthMetrics | None,
) -> dict[str, Any]:
    """Itemised two-leg cost scenarios for the same book inputs build_cost_preview
    prices: per side, every line with its leg, direction, exact Decimal string, presented
    projection and source; every missing and not-applicable component with its reason.

    Kept apart from `cost_preview` on purpose: no production path calls it, so nothing of
    it reaches the output file, the model context or the Qwen payload. Fees are the same
    config.UNCALIBRATED_FEES assumptions; funding is never costed (immediate round trip).
    """
    fees = config.UNCALIBRATED_FEES
    return {
        "spot": _venue_detail(InstrumentKind.SPOT, spot_depth, fees["spot_taker_bps"]),
        "futures": (
            _venue_detail(InstrumentKind.FUTURES, futures_depth, fees["futures_taker_bps"])
            if futures_available
            else None
        ),
    }
