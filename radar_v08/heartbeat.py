"""Heartbeat orchestrator: L0 -> snapshot store -> L1 -> L2 -> shortlist -> output.

Phase 1 steps (L0/L1/shortlist/output, no Qwen/Fable) plus Phase 2's L2:
OHLC incremental, ATR, structure, setup classification, opportunity_score,
and forward-return bookkeeping - only ever for the L1 shortlist.
See docs/RADAR_v0.8_ARCHITECTURE.md.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from . import budgets, config, cooldown, events, security
from .anomaly import compute_anomaly, compute_features, compute_return, lookup_past_spot
from .context_builder import build_event_context
from .http_client import ApiError, GuardedSession
from .kraken_futures import fetch_tickers as fetch_futures_tickers
from .kraken_futures import parse_perpetuals
from .kraken_spot import fetch_ticker, get_asset_pairs, parse_ticker_row
from .l2 import L2CandidateInput, L2Result, label_forward_returns, run_l2
from .l3 import L3CandidateInput, select_finalists, run_l3
from .logging_setup import append_run_record, configure_logging
from .output import build_candidate, build_output, write_json
from .qwen import review_finalists
from .router import RouterContext, route
from .store import FuturesSnapshotInput, SnapshotStore, SpotSnapshotInput
from .universe import build_spot_markets, find_meta, futures_is_stale, group_assets


def make_run_id(now: datetime) -> str:
    return f"{config.RUN_ID_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def _build_qwen_payload(asset: str, anomaly_score, l1_features, l2_result: L2Result, l3_result) -> dict[str, Any]:
    """Compact, already-computed features for one L3 finalist - Qwen never
    sees raw bars or the whole universe, only this per-asset summary.
    """
    return {
        "asset": asset,
        "setup_type": l2_result.setup.setup_type,
        "direction": l2_result.setup.direction,
        "anomaly_score": anomaly_score,
        "opportunity_score": l2_result.opportunity.score,
        "tradeability_score": l3_result.tradeability.score,
        "tradeability_state": l3_result.tradeability.state,
        "momentum_1h_atr": l2_result.l2_features.return_1h_atr,
        "momentum_15m_atr": l2_result.l2_features.return_15m_atr,
        "breakout_state": l2_result.l2_features.breakout_state,
        "rejection_state": l2_result.l2_features.rejection_state,
        "range_expansion": l2_result.l2_features.range_expansion,
        "range_compression": l2_result.l2_features.range_compression,
        "freshness": l2_result.l2_features.freshness,
        "exhaustion": l2_result.l2_features.exhaustion,
        "volume_intensity_15m": l1_features.volume_intensity_15m,
        "relative_return_vs_btc_15m": l1_features.relative_return_vs_btc_15m,
        "derivatives_coherence": l2_result.opportunity.derivatives_coherence,
        "cost_preview": l3_result.cost_preview,
        "flags": sorted(set(l2_result.flags) | set(l3_result.flags)),
    }


def run_heartbeat(
    mode: str = "HEARTBEAT", store: SnapshotStore | None = None, full: bool = False
) -> dict[str, Any]:
    started = time.perf_counter()
    logger = configure_logging()

    # Step 1: security validation. Aborts loudly if credentials or private
    # capability would otherwise be reachable.
    security.run_all_guards()

    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    run_id = make_run_id(now)

    owns_store = store is None
    store = store or SnapshotStore(config.SQLITE_PATH)

    api_failures = 0
    latency_ms: dict[str, float] = {}
    session = GuardedSession(config.HTTP_TIMEOUT, config.HTTP_MAX_RETRIES, config.HTTP_BACKOFF_BASE)

    try:
        # Step 2: AssetPairs, cached 24h.
        t0 = time.perf_counter()
        asset_pairs, refreshed = get_asset_pairs(session, ticker_keys=None)
        latency_ms["asset_pairs_ms"] = (time.perf_counter() - t0) * 1000
        logger.info("AssetPairs loaded (%d pairs, refreshed=%s)", len(asset_pairs), refreshed)

        # Step 3: Spot Ticker (global, single request). Fatal if it fails -
        # spot data is the backbone of this radar; there is no spot-only
        # degraded mode below it.
        t0 = time.perf_counter()
        ticker_raw = fetch_ticker(session)
        latency_ms["spot_ticker_ms"] = (time.perf_counter() - t0) * 1000

        # Force AssetPairs refresh if the Ticker mentions a symbol we don't
        # have metadata for yet (new listing).
        asset_pairs, force_refreshed = get_asset_pairs(session, ticker_keys=set(ticker_raw.keys()))
        if force_refreshed:
            logger.info("AssetPairs force-refreshed: new symbol(s) in Ticker not in cache")

        parsed_rows = {}
        missing_meta = 0
        for key, row in ticker_raw.items():
            meta = asset_pairs.get(key) or find_meta(asset_pairs, key)
            if meta is None:
                missing_meta += 1
                continue
            try:
                parsed_rows[key] = parse_ticker_row(key, row, meta, ts)
            except (KeyError, TypeError, ValueError):
                missing_meta += 1
                continue

        # Step 4: Futures Tickers. Optional - degrade to spot-only on failure.
        futures_perpetuals = []
        futures_status = "OK"
        t0 = time.perf_counter()
        try:
            futures_raw = fetch_futures_tickers(session)
            futures_perpetuals = parse_perpetuals(futures_raw, ts)
        except ApiError as exc:
            api_failures += 1
            futures_status = "UNAVAILABLE"
            logger.warning("Futures public data unavailable: %s", exc)
        latency_ms["futures_ticker_ms"] = (time.perf_counter() - t0) * 1000

        stale_futures = sum(1 for r in futures_perpetuals if futures_is_stale(r.last_time, now))
        if futures_perpetuals and stale_futures == len(futures_perpetuals):
            futures_status = "STALE"

        if missing_meta:
            logger.warning("%d ticker symbols had no resolvable AssetPairs metadata", missing_meta)

        # Step 5: normalization + grouping.
        markets, excluded_markets = build_spot_markets(parsed_rows, asset_pairs)
        for reason, keys in excluded_markets.items():
            if keys:
                logger.info("excluded %d spot markets (%s)", len(keys), reason)
        assets = group_assets(markets, futures_perpetuals)

        assets_eligible = [a for a in assets.values() if a.eligible]
        assets_tradeable = [a for a in assets.values() if a.tradeable]

        # Step 6: snapshot persistence (batch).
        spot_inputs: list[SpotSnapshotInput] = []
        for entry in assets_eligible:
            for m in entry.markets:
                spot_inputs.append(
                    SpotSnapshotInput(
                        asset=entry.asset, pair=m.pair_key, quote=m.quote, ts=ts,
                        last=m.last, bid=m.bid, ask=m.ask, bid_size=m.bid_size, ask_size=m.ask_size,
                        volume_today=m.volume_today, volume_24h=m.volume_24h,
                        vwap_today=m.vwap_today, vwap_24h=m.vwap_24h,
                        trades_today=m.trades_today, trades_24h=m.trades_24h,
                        high_today=m.high_today, low_today=m.low_today,
                        high_24h=m.high_24h, low_24h=m.low_24h,
                        open_today=m.open_today, status=m.status,
                    )
                )
            store.upsert_asset(entry.asset, ts, is_stable=False, is_fiat=False, excluded_reason=None)

        futures_inputs = [
            FuturesSnapshotInput(
                symbol=r.symbol, asset=r.asset, ts=ts, last=r.last, mark_price=r.mark_price,
                index_price=r.index_price, bid=r.bid, ask=r.ask, bid_size=r.bid_size,
                ask_size=r.ask_size, volume_quote=r.volume_quote, open_interest=r.open_interest,
                funding_rate_raw=r.funding_rate_raw, funding_prediction_raw=r.funding_prediction_raw,
                open_24h=r.open_24h, last_time=r.last_time, suspended=r.suspended,
                post_only=r.post_only, tag=r.tag,
            )
            for r in futures_perpetuals
        ]

        store.insert_spot_snapshots_batch(spot_inputs)
        store.insert_futures_snapshots_batch(futures_inputs)
        snapshot_count = len(spot_inputs) + len(futures_inputs)

        pruned_spot, pruned_futures = store.prune(config.SNAPSHOT_RETENTION_DAYS, now=now)
        if pruned_spot or pruned_futures:
            logger.info("Pruned %d spot / %d futures snapshots past retention", pruned_spot, pruned_futures)

        # Step 7: L1 anomaly detection.
        btc_entry = assets.get(config.BTC_ASSET)
        btc_return_15m = None
        btc_return_1h = None
        if btc_entry is not None and btc_entry.primary_market is not None:
            btc_last = btc_entry.primary_market.last
            btc_return_15m = compute_return(btc_last, lookup_past_spot(store, config.BTC_ASSET, now, 15))
            btc_return_1h = compute_return(btc_last, lookup_past_spot(store, config.BTC_ASSET, now, 60))

        l1_by_asset: dict[str, tuple] = {}
        any_non_warmup = False
        for entry in assets_eligible:
            m = entry.primary_market
            fut = entry.futures
            features = compute_features(
                store=store,
                asset=entry.asset,
                now_dt=now,
                current_last=m.last,
                current_volume_today=m.volume_today,
                current_trades_today=m.trades_today,
                current_spread_bps=m.spread_bps,
                current_bid=m.bid,
                current_bid_size=m.bid_size,
                current_ask=m.ask,
                current_ask_size=m.ask_size,
                btc_return_15m=btc_return_15m,
                btc_return_1h=btc_return_1h,
                current_oi=fut.open_interest if fut else None,
                current_mark=fut.mark_price if fut else None,
                current_index=fut.index_price if fut else None,
            )
            result = compute_anomaly(store, entry.asset, now, features)
            any_non_warmup = any_non_warmup or not result.warmup
            l1_by_asset[entry.asset] = (entry, features, result)

        # Step 8: shortlist - top N by anomaly_score (warmup/None sorted last).
        # This is the ONLY set of assets that goes on to L2 - never the whole
        # universe (architecture doc: "CANDIDATE ONLY").
        ranked_assets = sorted(
            l1_by_asset.keys(),
            key=lambda a: (l1_by_asset[a][2].anomaly_score is None, -(l1_by_asset[a][2].anomaly_score or 0)),
        )
        shortlist_assets = ranked_assets[: config.ANOMALY_SHORTLIST_SIZE]

        run_warmup = not any_non_warmup

        # Step 8b: L2 - OHLC incremental, ATR, structure, setup, opportunity.
        l2_inputs = []
        for asset in shortlist_assets:
            entry, _features, result = l1_by_asset[asset]
            m = entry.primary_market
            fut = entry.futures
            l2_inputs.append(
                L2CandidateInput(
                    asset=asset, pair=m.pair_key, current_last=m.last, vwap_today=m.vwap_today,
                    spread_bps=m.spread_bps, bid=m.bid, ask=m.ask, bid_size=m.bid_size, ask_size=m.ask_size,
                    market_status=m.status, futures_available=fut is not None,
                    futures_spread_bps=(
                        ((fut.ask - fut.bid) / ((fut.ask + fut.bid) / 2.0)) * 10_000.0
                        if fut and fut.ask and fut.bid else None
                    ),
                    futures_volume_24h_usd=fut.volume_quote if fut else None,
                    l1_features=result.features, anomaly_score=result.anomaly_score,
                )
            )

        l2_results, ohlc_requests_made, ohlc_failures_count = run_l2(store, session, l2_inputs, now, run_id)
        api_failures += ohlc_failures_count

        # Step 9: forward-return labeling (calibration data only).
        forward_returns_labeled = label_forward_returns(store, now)

        # Step 10 (Phase 3, `full` cycles only): L3 order book/trades on a
        # small finalist set, Qwen review, Demand Router, cooldown/budget,
        # event queue. Never runs on the plain heartbeat cadence.
        l3_results: dict[str, Any] = {}
        qwen_reviews: dict[str, Any] = {}
        router_results: dict[str, Any] = {}
        event_by_asset: dict[str, tuple[str, str]] = {}
        l3_requests_made = l3_failures_count = 0
        qwen_status = "SKIPPED"
        l2_candidates_count = 0
        finalists: list[L3CandidateInput] = []

        if full:
            l3_inputs: list[L3CandidateInput] = []
            for asset in shortlist_assets:
                l2_result = l2_results.get(asset)
                if l2_result is None:
                    continue
                entry, _features, result = l1_by_asset[asset]
                m = entry.primary_market
                fut = entry.futures
                l3_inputs.append(
                    L3CandidateInput(
                        asset=asset, spot_pair=m.pair_key, futures_symbol=fut.symbol if fut else None,
                        bid=m.bid, ask=m.ask, bid_size=m.bid_size, ask_size=m.ask_size,
                        spread_bps=m.spread_bps, market_status=m.status,
                        futures_available=fut is not None,
                        futures_spread_bps=(
                            ((fut.ask - fut.bid) / ((fut.ask + fut.bid) / 2.0)) * 10_000.0
                            if fut and fut.ask and fut.bid else None
                        ),
                        futures_volume_24h_usd=fut.volume_quote if fut else None,
                        funding_rate_raw=fut.funding_rate_raw if fut else None,
                        l2_result=l2_result,
                    )
                )

            l2_candidates_count = sum(
                1 for c in l3_inputs
                if not c.l2_result.l2_features.l2_warmup and (c.l2_result.opportunity.score or 0) > 0
            )
            finalists = select_finalists(l3_inputs)
            l3_results, l3_requests_made, l3_failures_count = run_l3(session, finalists)
            api_failures += l3_failures_count

            qwen_candidates = [c for c in finalists if l3_results[c.asset].qwen_eligible]
            if qwen_candidates:
                qwen_payloads = [
                    _build_qwen_payload(
                        c.asset, l1_by_asset[c.asset][2].anomaly_score,
                        l1_by_asset[c.asset][2].features, c.l2_result, l3_results[c.asset],
                    )
                    for c in qwen_candidates
                ]
                qwen_batch = review_finalists(qwen_payloads)
                qwen_status = qwen_batch.status
                qwen_reviews = qwen_batch.reviews
                if qwen_batch.status != "OK":
                    logger.warning("Qwen unavailable this cycle: %s", qwen_batch.error)
            else:
                qwen_status = "SKIPPED"

            for c in finalists:
                l3r = l3_results[c.asset]
                l1f = l1_by_asset[c.asset][2].features
                qreview = qwen_reviews.get(c.asset)
                data_quality_ok = not (set(c.l2_result.flags) | set(l3r.flags)) & {
                    "OHLC_MISSING", "ORDER_BOOK_UNAVAILABLE", "TRADES_UNAVAILABLE",
                }
                ctx = RouterContext(
                    asset=c.asset,
                    anomaly_score=l1_by_asset[c.asset][2].anomaly_score,
                    opportunity_score=c.l2_result.opportunity.score,
                    tradeability_score=l3r.tradeability.score,
                    tradeability_state=l3r.tradeability.state,
                    setup_type=c.l2_result.setup.setup_type,
                    direction=c.l2_result.setup.direction,
                    momentum_1h_atr=c.l2_result.l2_features.return_1h_atr,
                    momentum_coherence=c.l2_result.opportunity.breakdown.get("momentum_coherence", 0.0),
                    volume_intensity_15m=l1f.volume_intensity_15m,
                    range_expansion=c.l2_result.l2_features.range_expansion,
                    breakout_state=c.l2_result.l2_features.breakout_state,
                    derivatives_coherence_credit=c.l2_result.opportunity.breakdown.get("derivatives_coherence", 0.0),
                    taker_buy_ratio=None,
                    qwen_status=(qwen_status if qreview is not None else "SKIPPED"),
                    qwen_veto=qreview.veto if qreview else False,
                    qwen_call_sonnet=qreview.call_sonnet if qreview else False,
                    qwen_call_fable=qreview.call_fable if qreview else False,
                    qwen_confidence=qreview.confidence if qreview else None,
                    qwen_direction=qreview.direction if qreview else None,
                )
                router_result = route(ctx, data_quality_ok=data_quality_ok)
                router_results[c.asset] = router_result

                if router_result.decision in ("SONNET", "FABLE"):
                    allowed, cooldown_reason = cooldown.check_cooldown(
                        store, c.asset, router_result.decision, now,
                        c.l2_result.setup.setup_type, c.l2_result.setup.direction, c.l2_result.opportunity.score,
                    )
                    if not allowed:
                        router_result.reasons.append(f"suppressed_by_cooldown:{cooldown_reason}")
                        continue

                    budget_ok, _budget_status = budgets.try_consume_budget(store, router_result.decision, now)
                    event_status = "PENDING" if budget_ok else "DEFERRED"
                    if not budget_ok:
                        router_result.reasons.append("budget_exhausted")

                    cooldown.record_send(
                        store, c.asset, router_result.decision, now,
                        c.l2_result.setup.setup_type, c.l2_result.setup.direction, c.l2_result.opportunity.score,
                    )

                    entry = l1_by_asset[c.asset][0]
                    fut = entry.futures
                    futures_snapshot = (
                        {
                            "open_interest": fut.open_interest,
                            "oi_delta_15m": l1f.futures_oi_delta_15m,
                            "oi_delta_1h": l1f.futures_oi_delta_1h,
                            "basis_mark_index_pct": l1f.futures_basis_mark_index,
                            "funding_rate_raw": fut.funding_rate_raw,
                            "funding_prediction_raw": fut.funding_prediction_raw,
                            "funding_semantics": "RAW_UNVERIFIED",
                        }
                        if fut is not None
                        else None
                    )
                    event_context = build_event_context(
                        asset=c.asset,
                        spot_pair=entry.primary_market.display if entry.primary_market else None,
                        futures_symbol=fut.symbol if fut else None,
                        market=("FUTURES" if c.futures_available else "SPOT"),
                        current_price=entry.primary_market.last if entry.primary_market else None,
                        setup_type=c.l2_result.setup.setup_type,
                        direction=c.l2_result.setup.direction,
                        anomaly_score=l1_by_asset[c.asset][2].anomaly_score,
                        l1_features=l1f,
                        l2_result=c.l2_result,
                        l3_result=l3r,
                        futures_snapshot=futures_snapshot,
                        qwen_review=qreview,
                        router_result=router_result,
                        flags=sorted(set(c.l2_result.flags) | set(l3r.flags)),
                    )

                    event_id, _created = events.create_event_if_new(
                        store, ts=ts, type_="RADAR_ALERT", asset=c.asset,
                        setup_type=c.l2_result.setup.setup_type, direction=c.l2_result.setup.direction,
                        market=("FUTURES" if c.futures_available else "SPOT"),
                        anomaly_score=l1_by_asset[c.asset][2].anomaly_score,
                        opportunity_score=c.l2_result.opportunity.score,
                        tradeability_score=l3r.tradeability.score,
                        confidence=router_result.confidence, model_demand=router_result.decision,
                        reason="; ".join(router_result.reasons), status=event_status,
                        context=event_context,
                    )
                    event_by_asset[c.asset] = (event_id, event_status)

        shortlist = []
        for asset in shortlist_assets:
            entry, _features, result = l1_by_asset[asset]
            m = entry.primary_market
            fut = entry.futures
            event_id, event_status = event_by_asset.get(asset, (None, None))
            candidate = build_candidate(
                asset=asset,
                spot_pair=m.display,
                futures_symbol=fut.symbol if fut else None,
                result=result,
                flags=list(entry.flags),
                l2_result=l2_results.get(asset),
                l3_result=l3_results.get(asset),
                qwen_review=qwen_reviews.get(asset),
                router_result=router_results.get(asset),
                event_id=event_id,
                event_status=event_status,
            )
            shortlist.append(candidate)

            store.insert_alert(
                run_id=run_id, asset=candidate["asset"], ts=ts,
                anomaly_score=candidate["anomaly_score"], warmup=candidate["warmup"], flags=candidate["flags"],
            )

        data_quality = {
            "spot_ticker": "OK",
            "futures_ticker": futures_status,
            "funding_semantics": "RAW_UNVERIFIED",
            "qwen": qwen_status,
            "credentials_used": False,
        }

        universe_stats = {
            "pairs_seen": len(ticker_raw),
            "assets_eligible": len(assets_eligible),
            "assets_tradeable": len(assets_tradeable),
            "futures_perpetuals": len(futures_perpetuals),
        }
        decisions = [r.decision for r in router_results.values()]
        funnel = {
            "L1_shortlist": len(shortlist),
            "L2_ohlc_requests": ohlc_requests_made,
            "L2_ohlc_failures": ohlc_failures_count,
            "forward_returns_labeled": forward_returns_labeled,
            "L2_candidates": l2_candidates_count,
            "L3_finalists": len(finalists),
            "L3_requests": l3_requests_made,
            "L3_failures": l3_failures_count,
            "qwen_reviewed": len(qwen_reviews),
            "sonnet_demand": decisions.count("SONNET"),
            "fable_demand": decisions.count("FABLE"),
            "ignored": decisions.count("IGNORE"),
            "deferred": sum(1 for _e, s in event_by_asset.values() if s == "DEFERRED"),
            "events_created": len(event_by_asset),
        }

        output = build_output(
            run_id=run_id, timestamp=ts, mode=mode, warmup=run_warmup,
            universe=universe_stats, funnel=funnel, data_quality=data_quality,
            candidates=shortlist,
        )

        elapsed_ms = (time.perf_counter() - started) * 1000
        latency_ms["total_ms"] = elapsed_ms

        run_record = {
            "run_id": run_id, "ts": ts, "mode": mode,
            "markets_seen": len(ticker_raw),
            "assets_eligible": len(assets_eligible),
            "assets_tradeable": len(assets_tradeable),
            "snapshot_count": snapshot_count,
            "L1_shortlist": len(shortlist),
            "L2_ohlc_requests": ohlc_requests_made,
            "L2_ohlc_failures": ohlc_failures_count,
            "forward_returns_labeled": forward_returns_labeled,
            "warmup": run_warmup,
            "latency_ms": latency_ms,
            "api_failures": api_failures,
            "data_quality": data_quality,
            "funnel": funnel,
        }
        append_run_record(run_record)
        store.insert_run(
            {
                "run_id": run_id, "ts": ts, "mode": mode,
                "markets_seen": len(ticker_raw),
                "assets_eligible": len(assets_eligible),
                "assets_tradeable": len(assets_tradeable),
                "futures_perpetuals": len(futures_perpetuals),
                "snapshot_count": snapshot_count,
                "shortlist_count": len(shortlist),
                "warmup": run_warmup,
                "latency_ms": elapsed_ms,
                "api_failures": api_failures,
                "data_quality_json": json.dumps(data_quality),
            }
        )

        logger.info(
            "run %s mode=%s full=%s markets=%d eligible=%d tradeable=%d shortlist=%d warmup=%s "
            "ohlc_requests=%d ohlc_failures=%d fwd_labeled=%d l3_finalists=%d qwen=%s "
            "sonnet=%d fable=%d events=%d latency_ms=%.1f",
            run_id, mode, full, len(ticker_raw), len(assets_eligible), len(assets_tradeable),
            len(shortlist), run_warmup, ohlc_requests_made, ohlc_failures_count,
            forward_returns_labeled, len(finalists), qwen_status,
            funnel.get("sonnet_demand", 0), funnel.get("fable_demand", 0),
            funnel.get("events_created", 0), elapsed_ms,
        )

        return output
    finally:
        session.close()
        if owns_store:
            store.close()


def run_and_write(
    mode: str = "HEARTBEAT", output_path: str = config.OUTPUT_V08_PATH, full: bool = False
) -> dict[str, Any]:
    output = run_heartbeat(mode=mode, full=full)
    write_json(output, output_path)
    return output
