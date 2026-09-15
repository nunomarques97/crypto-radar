"""L2 feature computation: ATR-normalized returns, volatility, structure.

Pure functions over an OHLC bar window already cached in SQLite (l2.py owns
fetching/caching) plus a handful of already-computed L1 values (percentage
returns, which are more current than 5m bar boundaries since they come from
live ticker snapshots). Nothing here makes a network call.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from . import config
from .structure import (
    Bar,
    atr_series,
    breakout_distance_atr,
    breakout_state,
    higher_high,
    higher_low,
    lower_high,
    lower_low,
    range_expansion,
    realized_volatility,
    rejection_state,
    resample_bars,
    vwap_distance_pct,
    volatility_percentile,
    wilder_atr,
)


@dataclass
class L2Features:
    l2_warmup: bool = True
    ohlc_bar_count: int = 0

    return_24h_pct: float | None = None  # raw, rolling 24h - NOT the v0.7 "since 00:00 UTC" bug

    atr_5m: float | None = None
    atr_1h: float | None = None
    realized_vol_24h_pct: float | None = None
    volatility_percentile: float | None = None
    volatility_uncalibrated: bool = True

    return_5m_atr: float | None = None
    return_15m_atr: float | None = None
    return_1h_atr: float | None = None
    return_4h_atr: float | None = None

    high_4h: float | None = None
    low_4h: float | None = None
    high_24h: float | None = None
    low_24h: float | None = None
    breakout_dist_4h_atr: float | None = None
    breakout_dist_24h_atr: float | None = None

    higher_high: bool | None = None
    higher_low: bool | None = None
    lower_high: bool | None = None
    lower_low: bool | None = None

    range_compression: bool | None = None
    range_expansion: bool | None = None
    vwap_distance_pct: float | None = None
    breakout_state: str = "UNKNOWN"
    rejection_state: str = "NONE"

    freshness: float | None = None
    exhaustion: bool = False

    flags: list[str] = field(default_factory=list)


def sign(x: float | None) -> int:
    if x is None:
        return 0
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def _atr_as_pct(atr: float | None, price: float) -> float | None:
    if atr is None or price <= 0:
        return None
    return (atr / price) * 100.0


def normalize_return_by_atr(return_pct: float | None, atr_pct: float | None) -> float | None:
    if return_pct is None or atr_pct is None or atr_pct <= 0:
        return None
    return return_pct / atr_pct


def compute_freshness(return_1h_pct: float | None, return_24h_pct: float | None) -> float | None:
    """Fraction of the (24h) move that happened in the last hour.

    +20% in 24h but +0.1% in 1h -> freshness near 0 (large OLD move).
    +3% in 1h that IS most of the 24h move -> freshness near 1 (NEW move).
    Formula: recent / (recent + older), where "older" is what's left of the
    24h move once the last-hour portion is accounted for.
    """
    if return_1h_pct is None or return_24h_pct is None:
        return None
    recent = abs(return_1h_pct)
    older = max(abs(return_24h_pct) - recent, 0.0)
    denom = recent + older
    if denom <= 1e-9:
        return 0.0
    return recent / denom


def compute_exhaustion(bars: list[Bar], return_1h_atr: float | None) -> bool:
    """Extreme move (in ATR) + fading volume on the most recent bar.
    Deliberately says nothing about direction - see setups.py for how this
    flag is turned into (or kept out of) a setup.
    """
    threshold = config.SETUP_THRESHOLDS["exhaustion_min_atr_1h"]
    if return_1h_atr is None or abs(return_1h_atr) < threshold:
        return False
    if len(bars) < 5:
        return False
    recent_avg_volume = statistics.mean(b.volume for b in bars[-4:-1])
    if recent_avg_volume <= 0:
        return False
    fade_ratio = config.SETUP_THRESHOLDS["exhaustion_volume_fade_ratio"]
    return bars[-1].volume < recent_avg_volume * fade_ratio


def compute_l2_features(
    bars: list[Bar],
    current_last: float,
    vwap_today: float | None,
    l1_return_5m_pct: float | None,
    l1_return_15m_pct: float | None,
    l1_return_1h_pct: float | None,
    l1_return_4h_pct: float | None,
) -> L2Features:
    f = L2Features(ohlc_bar_count=len(bars))

    if len(bars) < config.L2_MIN_BARS:
        f.l2_warmup = True
        f.flags.append("l2_warmup")
        return f

    f.l2_warmup = False

    atr_5m = wilder_atr(bars, config.ATR_PERIOD)
    f.atr_5m = atr_5m

    hourly_bars = resample_bars(bars, config.ATR_1H_RESAMPLE_BARS)
    atr_1h = wilder_atr(hourly_bars, config.ATR_PERIOD) if len(hourly_bars) > config.ATR_PERIOD else None
    f.atr_1h = atr_1h

    atr_5m_pct = _atr_as_pct(atr_5m, current_last)
    atr_1h_pct = _atr_as_pct(atr_1h, current_last)

    f.return_5m_atr = normalize_return_by_atr(l1_return_5m_pct, atr_5m_pct)
    f.return_15m_atr = normalize_return_by_atr(l1_return_15m_pct, atr_5m_pct)
    f.return_1h_atr = normalize_return_by_atr(l1_return_1h_pct, atr_1h_pct if atr_1h_pct else atr_5m_pct)
    f.return_4h_atr = normalize_return_by_atr(l1_return_4h_pct, atr_1h_pct if atr_1h_pct else atr_5m_pct)

    bars_24h = bars[-config.STRUCTURE_24H_BARS :]
    if len(bars_24h) >= 2 and bars_24h[0].close > 0:
        f.return_24h_pct = (current_last / bars_24h[0].close - 1.0) * 100.0

    f.realized_vol_24h_pct = realized_volatility(bars_24h, window=len(bars_24h))

    atr_hist = atr_series(bars[-config.VOLATILITY_PERCENTILE_LOOKBACK_BARS :], config.ATR_PERIOD)
    f.volatility_uncalibrated = len(atr_hist) < 30
    if atr_hist:
        f.volatility_percentile = volatility_percentile(atr_5m, atr_hist[:-1] or atr_hist)

    # "Was compressed just before now" (not "is compressed now") - this is
    # what squeeze-release logic needs: the percentile of the PRIOR bar's
    # ATR against the history up to that point, so a squeeze that is
    # releasing THIS bar still reads as having been compressed a moment ago.
    if len(atr_hist) >= 2:
        prior_atr = atr_hist[-2]
        prior_baseline = atr_hist[:-2] or atr_hist[:-1]
        prior_percentile = volatility_percentile(prior_atr, prior_baseline)
        f.range_compression = (
            prior_percentile is not None and prior_percentile <= config.SQUEEZE_PERCENTILE_THRESHOLD
        )
    else:
        f.range_compression = None

    high_4h, low_4h = _high_low(bars, config.STRUCTURE_4H_BARS)
    high_24h, low_24h = _high_low(bars, config.STRUCTURE_24H_BARS)
    f.high_4h, f.low_4h = high_4h, low_4h
    f.high_24h, f.low_24h = high_24h, low_24h

    if high_4h is not None:
        f.breakout_dist_4h_atr = breakout_distance_atr(current_last, high_4h, low_4h, atr_5m)
    if high_24h is not None:
        f.breakout_dist_24h_atr = breakout_distance_atr(current_last, high_24h, low_24h, atr_1h or atr_5m)

    f.higher_high = higher_high(bars)
    f.higher_low = higher_low(bars)
    f.lower_high = lower_high(bars)
    f.lower_low = lower_low(bars)

    f.range_expansion = range_expansion(bars[-1], atr_5m)
    f.vwap_distance_pct = vwap_distance_pct(current_last, vwap_today)

    if high_24h is not None:
        f.breakout_state = breakout_state(current_last, high_24h, low_24h, atr_1h or atr_5m)
    f.rejection_state = rejection_state(bars[-1])

    f.freshness = compute_freshness(l1_return_1h_pct, f.return_24h_pct)
    f.exhaustion = compute_exhaustion(bars, f.return_1h_atr)

    return f


def _high_low(bars: list[Bar], count: int) -> tuple[float | None, float | None]:
    window = bars[-count:]
    if not window:
        return None, None
    return max(b.high for b in window), min(b.low for b in window)
