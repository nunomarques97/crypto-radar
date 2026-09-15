"""L3 microstructure: order book depth/imbalance/slippage, trades aggression.

Pure functions over already-fetched Depth/Trades rows - no network calls here
(kraken_spot.py owns fetching). Only ever called for L3 finalists (architecture
doc + task section 1/2: "O order book só deve ser consultado para finalistas").

Nothing here invents the aggressor side: Kraken's Trades `side` field is used
as-is and the resulting ratio is always labeled APPROXIMATE, never VERIFIED
fact (task section 2: "Não inventar o lado agressor").
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass
class DepthMetrics:
    mid: float
    spread_bps: float | None
    bid_depth_usd_0_5pct: float
    ask_depth_usd_0_5pct: float
    bid_depth_usd_1pct: float
    ask_depth_usd_1pct: float
    imbalance: float | None  # (-1 all ask heavy .. +1 all bid heavy), 1% band
    slippage_buy_bps: float | None
    slippage_sell_bps: float | None
    depth_available_at_reference: bool
    quality: str = "OK"  # OK | THIN_BOOK | UNAVAILABLE


@dataclass
class TradesMetrics:
    trade_count: int
    avg_trade_size_base: float | None
    avg_trade_size_usd: float | None
    median_trade_size_usd: float | None
    taker_buy_ratio: float | None  # APPROXIMATE - see module docstring
    time_covered_seconds: float | None
    classification: str = "APPROXIMATE"  # APPROXIMATE | UNAVAILABLE


def _depth_usd_within_band(levels: Sequence[tuple[float, float]], mid: float, band_pct: float, side: str) -> float:
    """Sum USD notional of levels within `band_pct`% of mid.

    side="bid": counts levels priced >= mid*(1-band). side="ask": counts
    levels priced <= mid*(1+band).
    """
    if mid <= 0:
        return 0.0
    lower = mid * (1.0 - band_pct / 100.0)
    upper = mid * (1.0 + band_pct / 100.0)
    total = 0.0
    for price, volume in levels:
        if side == "bid" and price >= lower:
            total += price * volume
        elif side == "ask" and price <= upper:
            total += price * volume
    return total


def _walk_book_for_fill(levels: Sequence[tuple[float, float]], reference_usd: float) -> tuple[float | None, bool]:
    """Walk levels (best price first) accumulating USD notional until
    `reference_usd` is filled. Returns (vwap_fill_price, fully_filled).
    """
    if not levels:
        return None, False
    filled_usd = 0.0
    filled_base = 0.0
    for price, volume in levels:
        level_usd = price * volume
        if filled_usd + level_usd >= reference_usd:
            remaining_usd = reference_usd - filled_usd
            remaining_base = remaining_usd / price if price > 0 else 0.0
            filled_base += remaining_base
            filled_usd += remaining_usd
            return (filled_usd / filled_base if filled_base > 0 else None), True
        filled_usd += level_usd
        filled_base += volume
    # Book exhausted before reference size filled.
    return (filled_usd / filled_base if filled_base > 0 else None), False


def compute_depth_metrics(
    bids: Sequence[tuple[float, float]],
    asks: Sequence[tuple[float, float]],
    reference_usd: float,
) -> DepthMetrics | None:
    """`bids`/`asks` are (price, volume) tuples, best price first. None if
    the book is empty (caller marks UNAVAILABLE, never fabricates a value).
    """
    if not bids or not asks:
        return None

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None

    mid = (best_bid + best_ask) / 2.0
    spread_bps = ((best_ask - best_bid) / mid) * 10_000.0 if mid > 0 else None

    bid_0_5 = _depth_usd_within_band(bids, mid, 0.5, "bid")
    ask_0_5 = _depth_usd_within_band(asks, mid, 0.5, "ask")
    bid_1 = _depth_usd_within_band(bids, mid, 1.0, "bid")
    ask_1 = _depth_usd_within_band(asks, mid, 1.0, "ask")

    total_1pct = bid_1 + ask_1
    imbalance = (bid_1 - ask_1) / total_1pct if total_1pct > 0 else None

    slip_buy_price, buy_filled = _walk_book_for_fill(asks, reference_usd)
    slip_sell_price, sell_filled = _walk_book_for_fill(bids, reference_usd)

    slippage_buy_bps = ((slip_buy_price / best_ask - 1.0) * 10_000.0) if slip_buy_price else None
    slippage_sell_bps = ((1.0 - slip_sell_price / best_bid) * 10_000.0) if slip_sell_price else None

    depth_available_at_reference = buy_filled and sell_filled

    quality = "OK"
    if not depth_available_at_reference:
        quality = "THIN_BOOK"

    return DepthMetrics(
        mid=mid,
        spread_bps=spread_bps,
        bid_depth_usd_0_5pct=bid_0_5,
        ask_depth_usd_0_5pct=ask_0_5,
        bid_depth_usd_1pct=bid_1,
        ask_depth_usd_1pct=ask_1,
        imbalance=imbalance,
        slippage_buy_bps=slippage_buy_bps,
        slippage_sell_bps=slippage_sell_bps,
        depth_available_at_reference=depth_available_at_reference,
        quality=quality,
    )


def compute_trades_metrics(trades: Sequence[Any]) -> TradesMetrics | None:
    """`trades` are TradeRow-like objects (price/volume/time/side). None if
    there are no trades at all (caller marks UNAVAILABLE).
    """
    if not trades:
        return None

    sizes_usd = [t.price * t.volume for t in trades]
    avg_base = statistics.mean(t.volume for t in trades)
    avg_usd = statistics.mean(sizes_usd)
    median_usd = statistics.median(sizes_usd)

    buy_volume = sum(t.volume for t in trades if t.side == "b")
    sell_volume = sum(t.volume for t in trades if t.side == "s")
    total_volume = buy_volume + sell_volume
    taker_buy_ratio = (buy_volume / total_volume) if total_volume > 0 else None

    times = [t.time for t in trades if t.time is not None]
    time_covered = (max(times) - min(times)) if len(times) >= 2 else None

    return TradesMetrics(
        trade_count=len(trades),
        avg_trade_size_base=avg_base,
        avg_trade_size_usd=avg_usd,
        median_trade_size_usd=median_usd,
        taker_buy_ratio=taker_buy_ratio,
        time_covered_seconds=time_covered,
        classification="APPROXIMATE",
    )
