"""L2 market structure: ATR, realized volatility, breakout/rejection state.

Everything here operates on `OhlcBar`-like rows (bar_time/open/high/low/
close/vwap/volume/trades) already cached in SQLite by l2.py - never fetches
anything itself. Bars must be ascending by time.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Sequence

from . import config


@dataclass
class Bar:
    bar_time: str
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    trades: int


def bars_from_rows(rows: Sequence) -> list[Bar]:
    """Adapt sqlite3.Row (or OhlcBar) sequences into plain Bar objects."""
    return [
        Bar(
            bar_time=r["bar_time"], open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], vwap=r["vwap"], volume=r["volume"], trades=r["trades"],
        )
        for r in rows
    ]


def true_range(bar: Bar, prev_close: float) -> float:
    return max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))


def true_range_series(bars: list[Bar]) -> list[float]:
    """One TR per bar from index 1 onward (needs a previous close)."""
    return [true_range(bars[i], bars[i - 1].close) for i in range(1, len(bars))]


def wilder_atr(bars: list[Bar], period: int = config.ATR_PERIOD) -> float | None:
    """Classic Wilder ATR: seed with the SMA of the first `period` true
    ranges, then recursively smooth over the rest. None if there aren't
    enough bars yet - degrade gracefully, never fabricate a value.
    """
    trs = true_range_series(bars)
    if len(trs) < period:
        return None

    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def atr_series(bars: list[Bar], period: int = config.ATR_PERIOD) -> list[float]:
    """ATR value trailing each bar from the point it first becomes
    computable onward - used to build a volatility-percentile baseline.
    """
    trs = true_range_series(bars)
    if len(trs) < period:
        return []

    out = []
    atr = sum(trs[:period]) / period
    out.append(atr)
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
        out.append(atr)
    return out


def resample_bars(bars: list[Bar], group_size: int) -> list[Bar]:
    """Merge consecutive `group_size` bars into one coarser bar (e.g. 12 x 5m
    -> 1h) without a second OHLC request per architecture doc section 3.
    """
    out: list[Bar] = []
    for i in range(0, len(bars) - group_size + 1, group_size):
        group = bars[i : i + group_size]
        out.append(
            Bar(
                bar_time=group[0].bar_time,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                vwap=sum(b.vwap * b.volume for b in group) / max(sum(b.volume for b in group), 1e-12),
                volume=sum(b.volume for b in group),
                trades=sum(b.trades for b in group),
            )
        )
    return out


def realized_volatility(bars: list[Bar], window: int) -> float | None:
    """Stdev of close-to-close log-ish returns over the last `window` bars,
    in percent. None if there's not enough history.
    """
    closes = [b.close for b in bars[-(window + 1) :]]
    if len(closes) < 3:
        return None
    returns = [
        (closes[i] / closes[i - 1] - 1.0) * 100.0
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    if len(returns) < 2:
        return None
    return statistics.pstdev(returns)


def volatility_percentile(current_atr: float | None, atr_history: list[float]) -> float | None:
    """Percentile rank (0-100) of `current_atr` within `atr_history`. Low =
    the market has been compressing; high = already-expanded range.
    """
    if current_atr is None or len(atr_history) < 5:
        return None
    below = sum(1 for a in atr_history if a <= current_atr)
    return (below / len(atr_history)) * 100.0


def high_low_over(bars: list[Bar], count: int) -> tuple[float, float] | tuple[None, None]:
    window = bars[-count:]
    if not window:
        return None, None
    return max(b.high for b in window), min(b.low for b in window)


def breakout_distance_atr(price: float, level_high: float, level_low: float, atr: float | None) -> float | None:
    """Signed distance (in ATR units) from the [level_low, level_high] range:
    positive above the high (breakout up), negative below the low (breakdown),
    0 if price sits inside the range.
    """
    if atr is None or atr <= 0:
        return None
    if price > level_high:
        return (price - level_high) / atr
    if price < level_low:
        return (price - level_low) / atr
    return 0.0


def higher_high(bars: list[Bar], count: int = config.STRUCTURE_TREND_BARS) -> bool | None:
    window = bars[-count:]
    if len(window) < 2:
        return None
    return window[-1].high > max(b.high for b in window[:-1])


def higher_low(bars: list[Bar], count: int = config.STRUCTURE_TREND_BARS) -> bool | None:
    window = bars[-count:]
    if len(window) < 2:
        return None
    return window[-1].low > min(b.low for b in window[:-1])


def lower_high(bars: list[Bar], count: int = config.STRUCTURE_TREND_BARS) -> bool | None:
    window = bars[-count:]
    if len(window) < 2:
        return None
    return window[-1].high < max(b.high for b in window[:-1])


def lower_low(bars: list[Bar], count: int = config.STRUCTURE_TREND_BARS) -> bool | None:
    window = bars[-count:]
    if len(window) < 2:
        return None
    return window[-1].low < min(b.low for b in window[:-1])


def vwap_distance_pct(last: float, vwap_today: float | None) -> float | None:
    if not vwap_today or vwap_today <= 0:
        return None
    return (last / vwap_today - 1.0) * 100.0


def breakout_state(price: float, high_level: float, low_level: float, atr: float | None) -> str:
    dist = breakout_distance_atr(price, high_level, low_level, atr)
    threshold = config.SETUP_THRESHOLDS["breakout_min_atr"]
    if dist is None:
        return "UNKNOWN"
    if dist >= threshold:
        return "BREAKOUT_UP"
    if dist <= -threshold:
        return "BREAKOUT_DOWN"
    return "INSIDE_RANGE"


def rejection_state(bar: Bar) -> str:
    """A rejection wick: the bar closed well away from the extreme it
    printed, on a meaningful range. Direction names the extreme rejected,
    not a trade direction.
    """
    bar_range = bar.high - bar.low
    if bar_range <= 0:
        return "NONE"
    upper_wick = bar.high - max(bar.open, bar.close)
    lower_wick = min(bar.open, bar.close) - bar.low
    ratio = config.SETUP_THRESHOLDS["rejection_wick_ratio"]

    if upper_wick / bar_range >= ratio:
        return "REJECTION_AT_HIGH"
    if lower_wick / bar_range >= ratio:
        return "REJECTION_AT_LOW"
    return "NONE"


def range_compression(volatility_pctile: float | None) -> bool | None:
    if volatility_pctile is None:
        return None
    return volatility_pctile <= config.SQUEEZE_PERCENTILE_THRESHOLD


def range_expansion(bar: Bar, atr: float | None) -> bool | None:
    if atr is None or atr <= 0:
        return None
    return (bar.high - bar.low) >= atr * config.RANGE_EXPANSION_ATR_MULT
