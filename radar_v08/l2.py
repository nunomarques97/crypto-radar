"""L2 orchestrator: OHLC incremental fetch/cache -> structure/ATR features ->
setup classification -> opportunity_score -> tradeability preview data ->
forward-return bookkeeping.

Only ever runs against the L1 shortlist (architecture doc: "CANDIDATE ONLY").
No Qwen, no Fable, no order book, no Trades endpoint - those are later phases.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import config
from .anomaly import Features as L1Features
from .http_client import ApiError, GuardedSession
from .kraken_spot import fetch_ohlc
from .l2_features import L2Features, compute_l2_features
from .opportunity import OpportunityResult, compute_opportunity_score
from .setups import SetupResult, classify_setup
from .store import SnapshotStore
from .structure import Bar, bars_from_rows

logger = logging.getLogger("radar_v08.l2")


@dataclass
class L2CandidateInput:
    asset: str
    pair: str
    current_last: float
    vwap_today: float | None
    spread_bps: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    market_status: str
    futures_available: bool
    futures_spread_bps: float | None
    futures_volume_24h_usd: float | None
    l1_features: L1Features
    anomaly_score: float | None


@dataclass
class L2Result:
    asset: str
    l2_features: L2Features
    setup: SetupResult
    opportunity: OpportunityResult
    tradeability_preview: dict[str, Any]
    ohlc_missing: bool
    ohlc_error: str | None = None
    flags: list[str] = field(default_factory=list)


def _fetch_worker(
    store: SnapshotStore, session: GuardedSession, pair: str, interval: int, now_iso: str
) -> tuple[bool, str | None]:
    try:
        since = store.get_ohlc_cursor(pair, interval)
        raw_bars, last = fetch_ohlc(session, pair, interval=interval, since=since)
        if raw_bars:
            store.insert_ohlc_bars_batch(pair, interval, raw_bars)
        if last is not None:
            store.set_ohlc_cursor(pair, interval, last, now_iso)
        return True, None
    except ApiError as exc:
        return False, str(exc)


def _build_tradeability_preview(c: L2CandidateInput) -> dict[str, Any]:
    return {
        "spread_bps": round(c.spread_bps, 3),
        "bid_size": c.bid_size,
        "ask_size": c.ask_size,
        "bid_usd": round(c.bid * c.bid_size, 2),
        "ask_usd": round(c.ask * c.ask_size, 2),
        "market_status": c.market_status,
        "futures_available": c.futures_available,
        "futures_spread_bps": round(c.futures_spread_bps, 3) if c.futures_spread_bps is not None else None,
        "futures_volume_24h_usd": c.futures_volume_24h_usd,
    }


def run_l2(
    store: SnapshotStore,
    session: GuardedSession,
    candidates: list[L2CandidateInput],
    now: datetime,
    run_id: str,
) -> tuple[dict[str, L2Result], int, int]:
    """Returns (results_by_asset, ohlc_requests_made, ohlc_failures)."""
    if not candidates:
        return {}, 0, 0

    now_iso = now.isoformat()
    interval = config.OHLC_INTERVAL_MINUTES

    fetch_ok: dict[str, bool] = {}
    fetch_err: dict[str, str | None] = {}
    ohlc_requests_made = 0

    with ThreadPoolExecutor(max_workers=config.OHLC_FETCH_WORKERS) as pool:
        future_map = {
            pool.submit(_fetch_worker, store, session, c.pair, interval, now_iso): c for c in candidates
        }
        for future in as_completed(future_map):
            c = future_map[future]
            ok, err = future.result()
            ohlc_requests_made += 1
            fetch_ok[c.asset] = ok
            fetch_err[c.asset] = err
            if not ok:
                logger.warning("OHLC fetch failed for %s (%s): %s", c.asset, c.pair, err)

    ohlc_failures = sum(1 for ok in fetch_ok.values() if not ok)

    results: dict[str, L2Result] = {}
    for c in candidates:
        rows = store.get_ohlc_window(c.pair, interval, config.OHLC_WINDOW_BARS)
        bars: list[Bar] = bars_from_rows(rows)
        ohlc_missing = not fetch_ok.get(c.asset, False)

        l2f = compute_l2_features(
            bars=bars,
            current_last=c.current_last,
            vwap_today=c.vwap_today,
            l1_return_5m_pct=c.l1_features.return_5m,
            l1_return_15m_pct=c.l1_features.return_15m,
            l1_return_1h_pct=c.l1_features.return_1h,
            l1_return_4h_pct=c.l1_features.return_4h,
            as_of=now,
        )

        setup = classify_setup(c.l1_features, l2f)
        market = "FUTURES" if c.futures_available else "SPOT"
        opportunity = compute_opportunity_score(c.l1_features, l2f, setup, market, c.spread_bps)

        flags: list[str] = []
        if ohlc_missing:
            flags.append("OHLC_MISSING")
            if fetch_err.get(c.asset):
                flags.append(f"ohlc_error:{fetch_err[c.asset]}")
        if l2f.l2_warmup:
            flags.append("l2_warmup")

        results[c.asset] = L2Result(
            asset=c.asset,
            l2_features=l2f,
            setup=setup,
            opportunity=opportunity,
            tradeability_preview=_build_tradeability_preview(c),
            ohlc_missing=ohlc_missing,
            ohlc_error=fetch_err.get(c.asset),
            flags=flags,
        )

        _store_l2_snapshot(store, run_id, c, l2f, opportunity, setup, now_iso)

    return results, ohlc_requests_made, ohlc_failures


def _store_l2_snapshot(
    store: SnapshotStore,
    run_id: str,
    c: L2CandidateInput,
    l2f: L2Features,
    opportunity: OpportunityResult,
    setup: SetupResult,
    now_iso: str,
) -> None:
    """Persist the L2 feature snapshot and open forward-return placeholders
    for later labeling. Calibration-only - never changes weights immediately.
    """
    if l2f.l2_warmup:
        return  # nothing meaningful to calibrate against yet

    features_payload = {
        "l1": {
            "return_5m": c.l1_features.return_5m,
            "return_15m": c.l1_features.return_15m,
            "return_1h": c.l1_features.return_1h,
            "return_4h": c.l1_features.return_4h,
            "volume_intensity_15m": c.l1_features.volume_intensity_15m,
            "relative_return_vs_btc_15m": c.l1_features.relative_return_vs_btc_15m,
        },
        "l2": {k: v for k, v in l2f.__dict__.items() if k != "flags"},
        "opportunity_breakdown": opportunity.breakdown,
    }

    store.insert_l2_feature_snapshot(
        run_id=run_id,
        asset=c.asset,
        ts=now_iso,
        entry_price=c.current_last,
        anomaly_score=c.anomaly_score,
        opportunity_score=opportunity.score,
        setup_type=setup.setup_type,
        direction=setup.direction,
        features_json=json.dumps(features_payload, default=str),
    )
    store.create_forward_return_placeholders(
        asset=c.asset,
        pair=c.pair,
        ts=now_iso,
        entry_price=c.current_last,
        horizons_minutes=config.FORWARD_RETURN_HORIZONS_MINUTES,
    )


def label_forward_returns(store: SnapshotStore, now: datetime) -> int:
    """Fill in return_pct/mfe_pct/mae_pct for any forward-return placeholder
    whose horizon is now due. Never used to change scoring weights directly -
    purely calibration data for a future phase.
    """
    due = store.pending_forward_returns(now, limit=config.FORWARD_RETURN_LABEL_BATCH_LIMIT)
    labeled = 0

    for row in due:
        pair = row["pair"]
        entry_price = row["entry_price"]
        if not pair or not entry_price:
            continue

        target_dt = datetime.fromisoformat(row["ts"]) + timedelta(minutes=row["horizon_minutes"])
        snap = store.nearest_spot_snapshot_by_pair(
            pair, target_dt.isoformat(), config.FORWARD_RETURN_LOOKUP_TOLERANCE_SECONDS
        )
        if snap is None:
            continue  # horizon due but no snapshot close enough yet - retry next heartbeat

        return_pct = (snap["last"] / entry_price - 1.0) * 100.0

        window_rows = store.spot_snapshots_between_pair(pair, row["ts"], target_dt.isoformat())
        if window_rows:
            prices = [r["last"] for r in window_rows if r["last"]]
            mfe_pct = (max(prices) / entry_price - 1.0) * 100.0 if prices else None
            mae_pct = (min(prices) / entry_price - 1.0) * 100.0 if prices else None
        else:
            mfe_pct = mae_pct = None

        store.label_forward_return(row["id"], return_pct, mfe_pct, mae_pct, now.isoformat())
        labeled += 1

    return labeled
