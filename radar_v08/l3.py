"""L3 orchestrator: order book + trades, finalists only (<= config.L3_MAX_FINALISTS
per cycle, never the shortlist and never the whole universe - task sections
1/2/18). Builds tradeability_score/state (a GATE) and the SPOT/FUTURES cost
preview, then applies the deterministic pre-gate that decides who is even
worth a Qwen call (task section 5).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import config
from .http_client import ApiError, GuardedSession
from .kraken_futures import fetch_orderbook as fetch_futures_orderbook
from .kraken_spot import fetch_depth, fetch_trades
from .l2 import L2Result
from .microstructure import DepthMetrics, TradesMetrics, compute_depth_metrics, compute_trades_metrics
from .tradeability import TradeabilityResult, build_cost_preview, compute_tradeability

logger = logging.getLogger("radar_v08.l3")


@dataclass
class L3CandidateInput:
    asset: str
    spot_pair: str
    futures_symbol: str | None
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    spread_bps: float
    market_status: str
    futures_available: bool
    futures_spread_bps: float | None
    futures_volume_24h_usd: float | None
    funding_rate_raw: float | None
    l2_result: L2Result


@dataclass
class L3Result:
    asset: str
    tradeability: TradeabilityResult
    cost_preview: dict[str, Any]
    qwen_eligible: bool
    flags: list[str] = field(default_factory=list)


def select_finalists(candidates: list[L3CandidateInput]) -> list[L3CandidateInput]:
    """Rank L2 candidates by opportunity_score, take the top N (task section
    1: max 8/cycle). Never burns an order-book request on a candidate with
    no confirmed opportunity or still in l2_warmup.
    """
    eligible = [
        c for c in candidates
        if not c.l2_result.l2_features.l2_warmup
        and c.l2_result.opportunity.score is not None
        and c.l2_result.opportunity.score > 0
    ]
    eligible.sort(key=lambda c: c.l2_result.opportunity.score, reverse=True)
    return eligible[: config.L3_MAX_FINALISTS]


def _fetch_spot_depth(session: GuardedSession, pair: str) -> tuple[DepthMetrics | None, str | None]:
    try:
        bids, asks = fetch_depth(session, pair, config.DEPTH_BOOK_COUNT)
    except ApiError as exc:
        return None, str(exc)
    metrics = compute_depth_metrics(bids, asks, config.REFERENCE_ORDER_SIZE_USD)
    return metrics, (None if metrics is not None else "empty_book")


def _fetch_futures_depth(session: GuardedSession, symbol: str) -> tuple[DepthMetrics | None, str | None]:
    try:
        bids, asks = fetch_futures_orderbook(session, symbol)
    except ApiError as exc:
        return None, str(exc)
    metrics = compute_depth_metrics(bids, asks, config.REFERENCE_ORDER_SIZE_USD)
    return metrics, (None if metrics is not None else "empty_book")


def _fetch_trades(session: GuardedSession, pair: str) -> tuple[TradesMetrics | None, str | None]:
    try:
        trades, _last = fetch_trades(session, pair)
    except ApiError as exc:
        return None, str(exc)
    metrics = compute_trades_metrics(trades)
    return metrics, (None if metrics is not None else "no_trades")


def passes_qwen_pregate(candidate: L3CandidateInput, tradeability: TradeabilityResult) -> bool:
    """Deterministic pre-gate (task section 5): only finalists that clear
    opportunity, tradeability, a valid setup and clean data quality are worth
    a Qwen call. Never a bonus - any single failing condition excludes.
    """
    opp = candidate.l2_result.opportunity.score
    setup_ok = candidate.l2_result.setup.setup_type != "NONE"
    data_quality_ok = "OHLC_MISSING" not in candidate.l2_result.flags
    return bool(
        opp is not None
        and opp >= config.QWEN_PREGATE_MIN_OPPORTUNITY
        and tradeability.state != "UNTRADEABLE"
        and setup_ok
        and data_quality_ok
    )


def run_l3(
    session: GuardedSession, candidates: list[L3CandidateInput]
) -> tuple[dict[str, L3Result], int, int]:
    """Returns (results_by_asset, requests_made, failures). Only ever called
    on the already-selected finalist list (see `select_finalists`).
    """
    if not candidates:
        return {}, 0, 0

    depth_by_asset: dict[str, tuple[DepthMetrics | None, str | None]] = {}
    futures_depth_by_asset: dict[str, tuple[DepthMetrics | None, str | None]] = {}
    trades_by_asset: dict[str, tuple[TradesMetrics | None, str | None]] = {}
    requests_made = 0
    failures = 0

    with ThreadPoolExecutor(max_workers=config.L3_FETCH_WORKERS) as pool:
        depth_futures = {pool.submit(_fetch_spot_depth, session, c.spot_pair): c.asset for c in candidates}
        trades_futures = {pool.submit(_fetch_trades, session, c.spot_pair): c.asset for c in candidates}
        fut_depth_futures = {
            pool.submit(_fetch_futures_depth, session, c.futures_symbol): c.asset
            for c in candidates if c.futures_available and c.futures_symbol
        }

        for future, asset in depth_futures.items():
            metrics, err = future.result()
            requests_made += 1
            depth_by_asset[asset] = (metrics, err)
            if err:
                failures += 1
                logger.warning("Depth fetch failed for %s: %s", asset, err)

        for future, asset in trades_futures.items():
            metrics, err = future.result()
            requests_made += 1
            trades_by_asset[asset] = (metrics, err)
            if err:
                failures += 1
                logger.warning("Trades fetch failed for %s: %s", asset, err)

        for future, asset in fut_depth_futures.items():
            metrics, err = future.result()
            requests_made += 1
            futures_depth_by_asset[asset] = (metrics, err)
            if err:
                failures += 1
                logger.warning("Futures orderbook fetch failed for %s: %s", asset, err)

    results: dict[str, L3Result] = {}
    for c in candidates:
        depth, depth_err = depth_by_asset.get(c.asset, (None, "not_fetched"))
        trades, trades_err = trades_by_asset.get(c.asset, (None, "not_fetched"))
        futures_depth, futures_depth_err = futures_depth_by_asset.get(c.asset, (None, None))

        flags: list[str] = []
        if depth_err:
            flags.append("ORDER_BOOK_UNAVAILABLE")
        if trades_err:
            flags.append("TRADES_UNAVAILABLE")
        if c.futures_available and futures_depth_err:
            flags.append("FUTURES_ORDER_BOOK_UNAVAILABLE")

        tradeability = compute_tradeability(
            spread_bps=c.spread_bps,
            depth=depth,
            trades=trades,
            bid_usd_l0=c.bid * c.bid_size,
            ask_usd_l0=c.ask * c.ask_size,
            market_status=c.market_status,
            futures_available=c.futures_available,
            futures_spread_bps=c.futures_spread_bps,
            futures_volume_24h_usd=c.futures_volume_24h_usd,
            freshness=c.l2_result.l2_features.freshness,
        )
        flags.extend(f for f in tradeability.flags if f not in flags)

        market = "FUTURES" if c.futures_available else "SPOT"
        cost_preview = build_cost_preview(
            market=market,
            spot_spread_bps=c.spread_bps,
            spot_depth=depth,
            futures_available=c.futures_available,
            futures_spread_bps=c.futures_spread_bps,
            futures_depth=futures_depth,
            funding_rate_raw=c.funding_rate_raw,
        )

        qwen_eligible = passes_qwen_pregate(c, tradeability)

        results[c.asset] = L3Result(
            asset=c.asset,
            tradeability=tradeability,
            cost_preview=cost_preview,
            qwen_eligible=qwen_eligible,
            flags=flags,
        )

    return results, requests_made, failures
