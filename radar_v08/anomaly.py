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

import math
import sqlite3
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

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

    if asset != config.BTC_ASSET and f.return_15m is not None and btc_return_15m is not None:
        f.relative_return_vs_btc_15m = f.return_15m - btc_return_15m
    if asset != config.BTC_ASSET and f.return_1h is not None and btc_return_1h is not None:
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


# The series below pair each row with the nearest row about one horizon earlier.
# Rows further away than the tolerance can never be picked, so the search only
# visits a window found by bisection instead of every earlier row. The window is
# _SEARCH_SLACK_MICROS wider than the tolerance on each side and the unchanged
# float test still decides every row inside it, so results, ties (earliest row
# wins) and order stay exactly those of the full scan. Lists that are not in time
# order, mix naive and aware times or hold non-numeric values keep the full scan.
_MICROSECOND = timedelta(microseconds=1)
_MINUTE_MICROS = 60_000_000
_SEARCH_SLACK_MICROS = 1_000_000
_NAIVE_EPOCH = datetime(1970, 1, 1)
_AWARE_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class _TimeIndex(NamedTuple):
    epoch: datetime
    micros: list[int]


class _ReturnObservations(NamedTuple):
    values: list[tuple[datetime, float]]
    times: _TimeIndex | None


# One cycle's BTC return observations, keyed by (btc_pair, lookback_start).
# heartbeat creates one per cycle; it never outlives that cycle.
BtcHistoryCache = dict[tuple[str, str], _ReturnObservations]


def _epoch_for(times: list[datetime]) -> datetime | None:
    if all(t.tzinfo is None for t in times):
        return _NAIVE_EPOCH
    if all(type(t.tzinfo) is timezone for t in times):
        return _AWARE_EPOCH
    return None


def _time_index(times: list[datetime]) -> _TimeIndex | None:
    """Microseconds since the epoch, or None unless ``times`` never goes back."""
    epoch = _epoch_for(times)
    if epoch is None:
        return None
    micros = [(t - epoch) // _MICROSECOND for t in times]
    if any(later < earlier for earlier, later in zip(micros, micros[1:])):
        return None
    return _TimeIndex(epoch, micros)


def _plain_numbers(values: list[object]) -> bool:
    return all(v is None or type(v) is float or type(v) is int for v in values)


def _nearby(index: _TimeIndex | None, target: int, tolerance: int, stop: int) -> range:
    """Positions below ``stop`` that may lie within ``tolerance`` microseconds of ``target``."""
    if index is None:
        return range(stop)
    slack = tolerance + _SEARCH_SLACK_MICROS
    lo = bisect_left(index.micros, target - slack, 0, stop)
    return range(lo, bisect_right(index.micros, target + slack, lo, stop))


def historical_return_series(rows: list[sqlite3.Row], horizon_minutes: int) -> list[float]:
    """Build a return-of-length-horizon series from an ascending snapshot history,
    by pairing each row with the nearest earlier row ~horizon_minutes before it.
    """
    parsed = [(_row_dt(r), r["last"]) for r in rows]
    tolerance = timedelta(seconds=max(horizon_minutes * 60 * config.LOOKUP_TOLERANCE_FRACTION, 30))
    index = _time_index([ts for ts, _ in parsed])
    horizon_micros = horizon_minutes * _MINUTE_MICROS
    tolerance_micros = tolerance // _MICROSECOND
    results: list[float] = []

    for i, (ts, price) in enumerate(parsed):
        if not price or price <= 0:
            continue
        target = ts - timedelta(minutes=horizon_minutes)
        best_diff: float | None = None
        best_price: float | None = None
        target_micros = index.micros[i] - horizon_micros if index is not None else 0
        for j in _nearby(index, target_micros, tolerance_micros, i):
            pt, pp = parsed[j]
            diff = abs((pt - target).total_seconds())
            if diff <= tolerance.total_seconds() and (best_diff is None or diff < best_diff):
                best_diff = diff
                best_price = pp
        if best_price and best_price > 0:
            results.append((price / best_price - 1.0) * 100.0)

    return results


def _historical_return_observations(
    rows: list[sqlite3.Row], horizon_minutes: int
) -> list[tuple[datetime, float]]:
    """Return timestamped, pair-pure returns for matching distributions."""
    parsed = [(_row_dt(r), r["last"]) for r in rows]
    tolerance = timedelta(seconds=max(horizon_minutes * 60 * config.LOOKUP_TOLERANCE_FRACTION, 30))
    index = _time_index([ts for ts, _ in parsed]) if _plain_numbers([p for _, p in parsed]) else None
    horizon_micros = horizon_minutes * _MINUTE_MICROS
    tolerance_micros = tolerance // _MICROSECOND
    results: list[tuple[datetime, float]] = []

    for i, (ts, price) in enumerate(parsed):
        if not price or price <= 0:
            continue
        target = ts - timedelta(minutes=horizon_minutes)
        best_diff: float | None = None
        best_price: float | None = None
        target_micros = index.micros[i] - horizon_micros if index is not None else 0
        for j in _nearby(index, target_micros, tolerance_micros, i):
            pt, pp = parsed[j]
            if not pp or pp <= 0:
                continue
            diff = abs((pt - target).total_seconds())
            if diff <= tolerance.total_seconds() and (best_diff is None or diff < best_diff):
                best_diff = diff
                best_price = pp
        if best_price is not None:
            results.append((ts, (price / best_price - 1.0) * 100.0))

    return results


def _indexed_observations(rows: list[sqlite3.Row], horizon_minutes: int) -> _ReturnObservations:
    values = _historical_return_observations(rows, horizon_minutes)
    return _ReturnObservations(values, _time_index([ts for ts, _ in values]))


def _relative_residuals(
    asset_returns: list[tuple[datetime, float]], btc: _ReturnObservations, horizon_minutes: int
) -> list[float]:
    tolerance_seconds = max(horizon_minutes * 60 * config.LOOKUP_TOLERANCE_FRACTION, 30)
    btc_returns = btc.values
    index = btc.times
    if index is not None and (
        not math.isfinite(tolerance_seconds) or _epoch_for([ts for ts, _ in asset_returns]) is not index.epoch
    ):
        index = None
    tolerance_micros = int(tolerance_seconds * 1_000_000) if index is not None else 0
    results: list[float] = []

    for asset_ts, asset_return in asset_returns:
        best_diff: float | None = None
        best_btc_return: float | None = None
        target_micros = (asset_ts - index.epoch) // _MICROSECOND if index is not None else 0
        for j in _nearby(index, target_micros, tolerance_micros, len(btc_returns)):
            btc_ts, btc_return = btc_returns[j]
            diff = abs((btc_ts - asset_ts).total_seconds())
            if diff <= tolerance_seconds and (best_diff is None or diff < best_diff):
                best_diff = diff
                best_btc_return = btc_return
        if best_btc_return is not None:
            results.append(asset_return - best_btc_return)

    return results


def historical_relative_btc_series(
    asset_rows: list[sqlite3.Row], btc_rows: list[sqlite3.Row], horizon_minutes: int
) -> list[float]:
    """Build aligned asset-minus-BTC historical return residuals.

    Both legs must be valid returns over the same horizon, and their endpoints
    must match within the existing lookup tolerance. Missing pair-pure data on
    either leg therefore removes that observation instead of borrowing raw
    asset-return history.
    """
    asset_returns = _historical_return_observations(asset_rows, horizon_minutes)
    btc = _indexed_observations(btc_rows, horizon_minutes)
    return _relative_residuals(asset_returns, btc, horizon_minutes)


def historical_delta_series(rows: list[sqlite3.Row], field_name: str, horizon_minutes: int) -> list[float]:
    parsed = [(_row_dt(r), r[field_name]) for r in rows]
    tolerance = timedelta(seconds=max(horizon_minutes * 60 * config.LOOKUP_TOLERANCE_FRACTION, 30))
    index = _time_index([ts for ts, _ in parsed]) if _plain_numbers([v for _, v in parsed]) else None
    horizon_micros = horizon_minutes * _MINUTE_MICROS
    tolerance_micros = tolerance // _MICROSECOND
    results: list[float] = []

    for i, (ts, value) in enumerate(parsed):
        if value is None:
            continue
        target = ts - timedelta(minutes=horizon_minutes)
        best_diff: float | None = None
        best_value: float | None = None
        target_micros = index.micros[i] - horizon_micros if index is not None else 0
        for j in _nearby(index, target_micros, tolerance_micros, i):
            pt, pv = parsed[j]
            if pv is None:
                continue
            diff = abs((pt - target).total_seconds())
            if diff <= tolerance.total_seconds() and (best_diff is None or diff < best_diff):
                best_diff = diff
                best_value = pv
        if best_value is not None:
            delta, reset = reset_aware_delta(value, best_value)
            if not reset and delta is not None:
                results.append(delta)

    return results


def compute_anomaly(
    store: SnapshotStore,
    asset: str,
    pair: str,
    now_dt: datetime,
    features: Features,
    btc_pair: str | None = None,
    *,
    btc_cache: BtcHistoryCache | None = None,
) -> AnomalyResult:
    """``btc_cache`` (optional) keeps one cycle's BTC return observations, so the
    BTC history is fetched and paired once per cycle instead of once per asset.
    Pass a new dict each cycle; the store must not change while it is in use.
    """
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
    relative_btc_z = None
    if asset != config.BTC_ASSET and btc_pair is not None:
        asset_returns = _historical_return_observations(history, 15)
        btc = btc_cache.get((btc_pair, lookback_start)) if btc_cache is not None else None
        if btc is None:
            btc = _indexed_observations(store.spot_history_by_pair(btc_pair, lookback_start), 15)
            if btc_cache is not None:
                btc_cache[(btc_pair, lookback_start)] = btc
        relative_history = _relative_residuals(asset_returns, btc, 15)
        relative_btc_z = robust_z(features.relative_return_vs_btc_15m, relative_history)

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
    if (
        asset != config.BTC_ASSET
        and features.relative_return_vs_btc_15m is not None
        and relative_btc_z is None
    ):
        flags.append("relative_btc_history_unavailable")

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
