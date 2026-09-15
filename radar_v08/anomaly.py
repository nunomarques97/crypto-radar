"""L1 anomaly detection.

The only question this layer answers: "how unusual is this asset's current
activity relative to its OWN history?" It is deliberately direction-agnostic
and says nothing about profitability, quality, or expected return - see
`ANOMALY_SCORE_DOCS` below for the exact contract.

Uses robust statistics (median / MAD) rather than mean/stddev so a handful of
extreme past moves don't hide the next one, and compares assets against
themselves rather than raw percentages across assets (an asset's own noise
floor differs wildly - 1% in 5m is nothing for a memecoin and a lot for BTC).
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config
from .store import SnapshotStore, reset_aware_delta

ANOMALY_SCORE_DOCS = (
    "anomaly_score (0-100): how statistically unusual this asset's current "
    "return/volume/trade/OI activity is relative to its OWN recent history. "
    "It does NOT mean: probability of profit, trade quality, direction, or "
    "expected return. It is direction-agnostic (built from abs(z-scores))."
)


@dataclass
class Features:
    return_1m: float | None = None
    return_5m: float | None = None
    return_15m: float | None = None
    return_1h: float | None = None
    return_4h: float | None = None

    volume_5m: float | None = None
    volume_15m: float | None = None
    volume_1h: float | None = None

    trade_count_5m: float | None = None
    trade_count_15m: float | None = None
    trade_count_1h: float | None = None

    volume_intensity_15m: float | None = None
    trade_intensity_15m: float | None = None

    relative_return_vs_btc_15m: float | None = None
    relative_return_vs_btc_1h: float | None = None

    futures_oi_delta_15m: float | None = None
    futures_oi_delta_1h: float | None = None
    futures_basis_mark_index: float | None = None

    spread_bps: float | None = None
    depth_proxy_usd_bid: float | None = None
    depth_proxy_usd_ask: float | None = None


@dataclass
class AnomalyResult:
    asset: str
    warmup: bool
    sample_count: int
    history_minutes: float
    anomaly_score: float | None
    price_z: float | None
    volume_z: float | None
    trades_z: float | None
    oi_z: float | None
    relative_btc_z: float | None
    features: Features
    flags: list[str] = field(default_factory=list)


def _row_dt(row: sqlite3.Row) -> datetime:
    return datetime.fromisoformat(row["ts"])


def lookup_past_spot(store: SnapshotStore, pair: str, now_dt: datetime, minutes_ago: int) -> sqlite3.Row | None:
    target = now_dt - timedelta(minutes=minutes_ago)
    tolerance = max(minutes_ago * 60 * config.LOOKUP_TOLERANCE_FRACTION, 30)
    return store.nearest_spot_snapshot_by_pair(pair, target.isoformat(), tolerance)


def lookup_past_futures(store: SnapshotStore, asset: str, now_dt: datetime, minutes_ago: int) -> sqlite3.Row | None:
    target = now_dt - timedelta(minutes=minutes_ago)
    tolerance = max(minutes_ago * 60 * config.LOOKUP_TOLERANCE_FRACTION, 30)
    return store.nearest_futures_snapshot(asset, target.isoformat(), tolerance)


def compute_return(current_last: float, past_row: sqlite3.Row | None) -> float | None:
    if past_row is None:
        return None
    past_last = past_row["last"]
    if not past_last or past_last <= 0:
        return None
    return (current_last / past_last - 1.0) * 100.0


def compute_window_volume(current_volume_today: float, past_row: sqlite3.Row | None) -> float | None:
    if past_row is None:
        return None
    delta, _reset = reset_aware_delta(current_volume_today, past_row["volume_today"])
    return delta


def compute_window_trades(current_trades_today: float, past_row: sqlite3.Row | None) -> float | None:
    if past_row is None:
        return None
    delta, _reset = reset_aware_delta(current_trades_today, past_row["trades_today"])
    return delta


def compute_oi_delta(current_oi: float | None, past_row: sqlite3.Row | None) -> float | None:
    if past_row is None or current_oi is None or past_row["open_interest"] is None:
        return None
    return current_oi - past_row["open_interest"]


def compute_features(
    store: SnapshotStore,
    asset: str,
    pair: str,
    now_dt: datetime,
    current_last: float,
    current_volume_today: float,
    current_trades_today: float,
    current_spread_bps: float,
    current_bid: float,
    current_bid_size: float,
    current_ask: float,
    current_ask_size: float,
    btc_return_15m: float | None,
    btc_return_1h: float | None,
    current_oi: float | None = None,
    current_mark: float | None = None,
    current_index: float | None = None,
) -> Features:
    f = Features()

    past_1m = lookup_past_spot(store, pair, now_dt, 1)
    past_5m = lookup_past_spot(store, pair, now_dt, 5)
    past_15m = lookup_past_spot(store, pair, now_dt, 15)
    past_1h = lookup_past_spot(store, pair, now_dt, 60)
    past_4h = lookup_past_spot(store, pair, now_dt, 240)

    f.return_1m = compute_return(current_last, past_1m)
    f.return_5m = compute_return(current_last, past_5m)
    f.return_15m = compute_return(current_last, past_15m)
    f.return_1h = compute_return(current_last, past_1h)
    f.return_4h = compute_return(current_last, past_4h)

    f.volume_5m = compute_window_volume(current_volume_today, past_5m)
    f.volume_15m = compute_window_volume(current_volume_today, past_15m)
    f.volume_1h = compute_window_volume(current_volume_today, past_1h)

    f.trade_count_5m = compute_window_trades(current_trades_today, past_5m)
    f.trade_count_15m = compute_window_trades(current_trades_today, past_15m)
    f.trade_count_1h = compute_window_trades(current_trades_today, past_1h)

    if f.volume_1h and f.volume_1h > 0 and f.volume_15m is not None:
        baseline_15m = f.volume_1h / 4.0
        f.volume_intensity_15m = f.volume_15m / baseline_15m if baseline_15m > 0 else None
    if f.trade_count_1h and f.trade_count_1h > 0 and f.trade_count_15m is not None:
        baseline_15m = f.trade_count_1h / 4.0
        f.trade_intensity_15m = f.trade_count_15m / baseline_15m if baseline_15m > 0 else None

    if f.return_15m is not None and btc_return_15m is not None:
        f.relative_return_vs_btc_15m = f.return_15m - btc_return_15m
    if f.return_1h is not None and btc_return_1h is not None:
        f.relative_return_vs_btc_1h = f.return_1h - btc_return_1h

    if current_oi is not None:
        past_fut_15m = lookup_past_futures(store, asset, now_dt, 15)
        past_fut_1h = lookup_past_futures(store, asset, now_dt, 60)
        f.futures_oi_delta_15m = compute_oi_delta(current_oi, past_fut_15m)
        f.futures_oi_delta_1h = compute_oi_delta(current_oi, past_fut_1h)

    if current_mark is not None and current_index not in (None, 0):
        f.futures_basis_mark_index = (current_mark / current_index - 1.0) * 100.0

    f.spread_bps = current_spread_bps
    f.depth_proxy_usd_bid = current_bid * current_bid_size
    f.depth_proxy_usd_ask = current_ask * current_ask_size

    return f


def robust_z(value: float | None, historical: list[float]) -> float | None:
    """Robust z-score using median + MAD. None if there isn't enough history
    or the value itself is missing - warmup must degrade, never fabricate.
    """
    if value is None or len(historical) < 2:
        return None
    median = statistics.median(historical)
    mad = statistics.median(abs(x - median) for x in historical)
    scaled_mad = max(config.MAD_SCALE * mad, config.MAD_FLOOR)
    return (value - median) / scaled_mad


def historical_return_series(rows: list[sqlite3.Row], horizon_minutes: int) -> list[float]:
    """Build a return-of-length-horizon series from an ascending snapshot history,
    by pairing each row with the nearest earlier row ~horizon_minutes before it.
    """
    parsed = [(_row_dt(r), r["last"]) for r in rows]
    tolerance = timedelta(seconds=max(horizon_minutes * 60 * config.LOOKUP_TOLERANCE_FRACTION, 30))
    results: list[float] = []

    for i, (ts, price) in enumerate(parsed):
        if not price or price <= 0:
            continue
        target = ts - timedelta(minutes=horizon_minutes)
        best_diff: float | None = None
        best_price: float | None = None
        for pt, pp in parsed[:i]:
            diff = abs((pt - target).total_seconds())
            if diff <= tolerance.total_seconds() and (best_diff is None or diff < best_diff):
                best_diff = diff
                best_price = pp
        if best_price and best_price > 0:
            results.append((price / best_price - 1.0) * 100.0)

    return results


def historical_delta_series(rows: list[sqlite3.Row], field_name: str, horizon_minutes: int) -> list[float]:
    parsed = [(_row_dt(r), r[field_name]) for r in rows]
    tolerance = timedelta(seconds=max(horizon_minutes * 60 * config.LOOKUP_TOLERANCE_FRACTION, 30))
    results: list[float] = []

    for i, (ts, value) in enumerate(parsed):
        if value is None:
            continue
        target = ts - timedelta(minutes=horizon_minutes)
        best_diff: float | None = None
        best_value: float | None = None
        for pt, pv in parsed[:i]:
            if pv is None:
                continue
            diff = abs((pt - target).total_seconds())
            if diff <= tolerance.total_seconds() and (best_diff is None or diff < best_diff):
                best_diff = diff
                best_value = pv
        if best_value is not None:
            delta, reset = reset_aware_delta(value, best_value)
            if not reset:
                results.append(delta)

    return results


def compute_anomaly(
    store: SnapshotStore,
    asset: str,
    pair: str,
    now_dt: datetime,
    features: Features,
) -> AnomalyResult:
    lookback_start = (now_dt - timedelta(hours=config.ANOMALY_HISTORY_LOOKBACK_HOURS)).isoformat()
    history = store.spot_history_by_pair(pair, lookback_start)

    sample_count = len(history)
    history_minutes = 0.0
    if history:
        history_minutes = (now_dt - _row_dt(history[0])).total_seconds() / 60.0

    warmup = sample_count < config.ANOMALY_MIN_SAMPLES or history_minutes < config.ANOMALY_MIN_HISTORY_MINUTES

    if warmup:
        return AnomalyResult(
            asset=asset,
            warmup=True,
            sample_count=sample_count,
            history_minutes=history_minutes,
            anomaly_score=None,
            price_z=None,
            volume_z=None,
            trades_z=None,
            oi_z=None,
            relative_btc_z=None,
            features=features,
            flags=["warmup"],
        )

    price_history = historical_return_series(history, 15)
    volume_history = historical_delta_series(history, "volume_today", 15)
    trades_history = historical_delta_series(history, "trades_today", 15)

    price_z = robust_z(features.return_15m, price_history)
    volume_z = robust_z(features.volume_15m, volume_history)
    trades_z = robust_z(features.trade_count_15m, trades_history)
    oi_z = None  # needs futures history series; left None unless futures present
    relative_btc_z = robust_z(features.relative_return_vs_btc_15m, price_history)

    score = _combine_anomaly_score(
        {
            "price_z": price_z,
            "volume_z": volume_z,
            "trades_z": trades_z,
            "oi_z": oi_z,
            "relative_btc_z": relative_btc_z,
        }
    )

    flags: list[str] = []
    if oi_z is None and features.futures_oi_delta_15m is not None:
        flags.append("oi_z_unavailable")

    return AnomalyResult(
        asset=asset,
        warmup=False,
        sample_count=sample_count,
        history_minutes=history_minutes,
        anomaly_score=score,
        price_z=price_z,
        volume_z=volume_z,
        trades_z=trades_z,
        oi_z=oi_z,
        relative_btc_z=relative_btc_z,
        features=features,
        flags=flags,
    )


def _combine_anomaly_score(zscores: dict[str, float | None]) -> float | None:
    """0-100, direction-agnostic. Weighted average of clipped abs(z), scaled.
    None only if every component is unavailable (should not happen once past
    warmup, but degrade gracefully rather than fabricate a number).
    """
    total_weight = 0.0
    weighted_sum = 0.0

    for key, z in zscores.items():
        if z is None:
            continue
        weight = config.ANOMALY_WEIGHTS.get(key, 0.0)
        clipped = min(abs(z), config.ANOMALY_Z_CLIP) / config.ANOMALY_Z_CLIP
        weighted_sum += weight * clipped * 100.0
        total_weight += weight

    if total_weight <= 0:
        return None
    return round(weighted_sum / total_weight, 2)
