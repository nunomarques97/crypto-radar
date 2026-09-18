"""Heartbeat orchestrator: L0 -> snapshot store -> L1 -> L2 -> shortlist -> output.

Phase 1 steps (L0/L1/shortlist/output, no Qwen/Fable) plus Phase 2's L2:
OHLC incremental, ATR, structure, setup classification, opportunity_score,
and forward-return bookkeeping - only ever for the L1 shortlist.
See docs/RADAR_v0.8_ARCHITECTURE.md.

Integrity (T023b, OC-1 in docs/OPERATING_CONTRACTS.md section 1): the
validator in `radar_v08.domain.integrity` runs before every consumption.
Spot ticker rows and futures ticker rows are checked right after receipt:
a hard-invalid row (non-finite, crossed, wrong identity, future-dated) is
neither persisted nor consumed; a row that is valid but not ready (stale,
market not online, clock untrusted) is persisted as the observation it is
but not consumed by L1/L2. Each futures instrument is judged on its own - a
rejected one only removes that instrument. L2 and L3 validate their own
OHLC/book/trades (see l2.py/l3.py). Before inference, every finalist's
evidence is sealed with `evaluate_snapshot`; a blocked snapshot never
reaches Qwen or the router, and a sealed one records its integrity report
additively in the event context (`context["integrity"]`).

The clock sample is measured each cycle against Kraken Futures'
`serverTime` (a UTC reference the tickers response already carries): the
local clock's offset from it is bounded by the request's send/receipt
times. No reference => synchronisation UNKNOWN => every freshness-sensitive
consumption is suspended for the cycle (fail closed, never assumed fresh).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, cooldown, events, security
from .adapters.kraken_timestamps import (
    fetch_futures_tickers,
    fetch_spot_ticker,
    futures_instrument,
    receipt_time,
    spot_instrument,
)
from .anomaly import compute_anomaly, compute_features, compute_return, lookup_past_spot
from .context_builder import build_event_context, deep_mark_unavailable
from .domain.integrity import (
    POLICY_VERSION,
    Capability,
    CapabilityResult,
    CheckStatus,
    ClockSample,
    FuturesExpectation,
    FuturesObservation,
    InstrumentId,
    IntegrityReport,
    MarketSnapshot,
    Reason,
    ReasonCode,
    SourceTiming,
    TickerObservation,
    TimeBasis,
    TradingStatus,
    evaluate_clock,
    evaluate_futures,
    evaluate_snapshot,
    evaluate_ticker,
    status_from_reasons,
)
from .http_client import ApiError, GuardedSession
from .kraken_futures import FuturesTickerRow, parse_perpetuals
from .kraken_spot import SpotTickerRow, get_asset_pairs, parse_ticker_row
from .l2 import L2CandidateInput, L2Result, label_forward_returns, run_l2
from .l3 import L3CandidateInput, L3Result, run_l3, select_finalists, with_extra_reasons
from .logging_setup import append_run_record, configure_logging
from .normalize import normalize_asset
from .output import build_candidate, build_output, write_json
from .qwen import review_finalists
from .router import RouterContext, route
from .store import FuturesSnapshotInput, SnapshotStore, SpotSnapshotInput
from .universe import (
    build_spot_markets,
    find_meta,
    group_assets,
    parse_futures_last_time,
)

# Kraken Futures' serverTime carries milliseconds: its own rounding is added
# to the measured offset bound.
_SERVER_TIME_RESOLUTION = timedelta(milliseconds=1)
_FUTURES_PREFIX = "PF_"
_OFFLINE_STATUSES = frozenset({"offline", "delisted", "disabled"})


def make_run_id(now: datetime) -> str:
    return f"{config.RUN_ID_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


# --- integrity wiring (T023b) ------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _venue_clock_sample(
    sent_at: datetime, timing: SourceTiming, previous: datetime
) -> tuple[ClockSample, dict[str, Any]]:
    """Clock evidence from one Kraken Futures tickers round trip.

    The server stamped `serverTime` while the request was in flight, i.e.
    between `sent_at` and `received_at` on a correct local clock, so the
    local clock's offset from the venue's UTC is at most
    max(|serverTime - sent_at|, |received_at - serverTime|) plus the
    serverTime resolution. That bound is the sample's uncertainty; OC-1
    (<= 500 ms) decides. No serverTime => synchronisation and uncertainty
    stay UNKNOWN; nothing is assumed.
    """
    reference = timing.source_time
    received = timing.received_at
    record: dict[str, Any] = {
        "source": "kraken-futures tickers serverTime",
        "sent_at": sent_at.isoformat(),
        "received_at": received.isoformat(),
        "server_time": reference.isoformat() if reference is not None else None,
        "offset_bound_ms": None,
    }
    if reference is None:
        return ClockSample(synchronized=None, offset_uncertainty=None, previous_wall_time=previous), record
    bound = max(abs(reference - sent_at), abs(received - reference)) + _SERVER_TIME_RESOLUTION
    record["offset_bound_ms"] = round(bound / timedelta(milliseconds=1), 3)
    return ClockSample(synchronized=True, offset_uncertainty=bound, previous_wall_time=previous), record


def _trading_status(raw: str) -> TradingStatus | None:
    status = raw.strip().lower()
    if not status:
        return None
    if status == "online":
        return TradingStatus.ONLINE
    if status in _OFFLINE_STATUSES:
        return TradingStatus.OFFLINE
    return TradingStatus.RESTRICTED


def _ticker_observation(row: SpotTickerRow, instrument: InstrumentId, timing: SourceTiming) -> TickerObservation:
    return TickerObservation(
        instrument=instrument,
        bid=row.bid,
        ask=row.ask,
        last=row.last,
        price_unit=normalize_asset(row.quote_raw),
        timing=timing,
        status=_trading_status(row.status),
    )


def _futures_expected(symbol: str) -> InstrumentId | None:
    """The identity a perpetual's symbol promises (PF_<BASE>USD), or None."""
    upper = symbol.upper()
    body = upper[len(_FUTURES_PREFIX):] if upper.startswith(_FUTURES_PREFIX) else ""
    quote = "USD"
    if len(body) <= len(quote) or not body.endswith(quote):
        return None
    return futures_instrument(upper, normalize_asset(body[: -len(quote)]))


def _futures_result(
    symbol: str, reasons: tuple[Reason, ...], timing: SourceTiming
) -> CapabilityResult:
    return CapabilityResult(
        Capability.FUTURES,
        symbol,
        status_from_reasons(reasons),
        reasons,
        TimeBasis.NONE,
        timing.received_at,
        timing.source_time,
    )


def _evaluate_futures_row(
    row: FuturesTickerRow, timing: SourceTiming, now: datetime
) -> tuple[CapabilityResult, FuturesExpectation | None]:
    """One perpetual, on its own: identity from the symbol vs the row's own
    pair, finite uncrossed prices, receipt/serverTime/lastTime <= 60 s."""
    expected = _futures_expected(row.symbol)
    if expected is None:
        reason = Reason(ReasonCode.MISSING_METADATA, "symbol", f"not a PF_<BASE>USD perpetual: {row.symbol!r}")
        return _futures_result(row.symbol, (reason,), timing), None
    extra: list[Reason] = []
    last_trade = parse_futures_last_time(row.last_time)
    if row.last_time is not None and last_trade is None:
        extra.append(Reason(ReasonCode.MISSING_METADATA, "last_trade_time", f"unparsable: {row.last_time!r}"))
    if row.bid is None or row.ask is None or row.last is None:
        missing = tuple(
            Reason(ReasonCode.MISSING_OBSERVATION, name, "not supplied")
            for name, value in (("bid", row.bid), ("ask", row.ask), ("last", row.last))
            if value is None
        )
        return _futures_result(expected.symbol, missing + tuple(extra), timing), None
    observation = FuturesObservation(
        instrument=futures_instrument(row.symbol, row.asset),
        bid=row.bid,
        ask=row.ask,
        last=row.last,
        price_unit=expected.quote,
        timing=timing,
        last_trade_time=last_trade,
        quote_time=None,
    )
    result = with_extra_reasons(evaluate_futures(observation, expected, now), tuple(extra))
    return result, FuturesExpectation(instrument=expected, observation=observation)


def _consumable(result: CapabilityResult | None, clock: CapabilityResult) -> bool:
    """Only a PASS under a PASS clock is consumed; UNKNOWN is never fresh."""
    return result is not None and result.status is CheckStatus.PASS and clock.status is CheckStatus.PASS


def _seal_evidence(
    selected: InstrumentId,
    ticker: TickerObservation | None,
    l2_result: L2Result,
    l3_result: L3Result,
    futures: tuple[FuturesExpectation, ...],
    clock_sample: ClockSample,
    now: datetime,
) -> IntegrityReport:
    """OC-1 evidence seal for one finalist, re-evaluated at `now` (just before
    inference): ticker, the closed-bar OHLC window L2 consumed, ATR claims,
    the L3 book and trades, this asset's futures and the clock."""
    snapshot = MarketSnapshot(
        selected=selected,
        ticker=ticker,
        ohlc=l2_result.ohlc_observation,
        book=l3_result.book_observation,
        trades=l3_result.trades_observation,
        clock=clock_sample,
        futures=futures,
        claim_atr_1h=l2_result.l2_features.atr_1h is not None,
    )
    report = evaluate_snapshot(snapshot, now)
    if not l3_result.trades_extra_reasons:
        return report
    results = tuple(
        with_extra_reasons(item, l3_result.trades_extra_reasons) if item.capability is Capability.TRADES else item
        for item in report.results
    )
    return IntegrityReport(evaluated_at=report.evaluated_at, policy_version=report.policy_version, results=results)


def _result_record(result: CapabilityResult) -> dict[str, Any]:
    return {
        "capability": result.capability.value,
        "subject": result.subject,
        "status": result.status.value,
        "time_basis": result.time_basis.value,
        "received_at": result.received_at.isoformat() if result.received_at is not None else None,
        "source_time": result.source_time.isoformat() if result.source_time is not None else None,
        "reasons": [
            {"code": reason.code.value, "field": reason.field, "detail": reason.detail} for reason in result.reasons
        ],
    }


def _integrity_record(
    report: IntegrityReport, clock_reference: dict[str, Any], futures_book: CapabilityResult | None
) -> dict[str, Any]:
    """Additive, JSON-safe record of a sealed evidence report (event context)."""
    return {
        "policy_version": report.policy_version,
        "evaluated_at": report.evaluated_at.isoformat(),
        "opportunity_blocked": report.opportunity_blocked,
        "eligible_futures": list(report.eligible_futures),
        "clock_reference": dict(clock_reference),
        "results": [_result_record(item) for item in report.results],
        "futures_book": _result_record(futures_book) if futures_book is not None else None,
    }


def _build_qwen_payload(
    asset: str, anomaly_score, l1_features, l2_result: L2Result, l3_result, futures_ok: bool = True
) -> dict[str, Any]:
    """Compact, already-computed features for one L3 finalist - Qwen never
    sees raw bars or the whole universe, only this per-asset summary.
    `futures_ok` False (T023b: this asset's futures failed the seal) marks
    the futures-derived coherence UNAVAILABLE instead of passing it on.
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
        "derivatives_coherence": l2_result.opportunity.derivatives_coherence if futures_ok else "UNAVAILABLE",
        "cost_preview": l3_result.cost_preview,
        "flags": sorted(set(l2_result.flags) | set(l3_result.flags)),
    }


def run_heartbeat(
    mode: str = "HEARTBEAT",
    store: SnapshotStore | None = None,
    full: bool = False,
    session: GuardedSession | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """One radar cycle. `session` and `clock` are injectable for tests (a
    GuardedSession over a fake transport, a deterministic aware clock); the
    defaults are a fresh GuardedSession and the UTC wall clock."""
    started = time.perf_counter()
    logger = configure_logging()

    # Step 1: security validation. Aborts loudly if credentials or private
    # capability would otherwise be reachable.
    security.run_all_guards()

    read_clock: Callable[[], datetime] = clock if clock is not None else _utc_now
    now = receipt_time(read_clock)  # aware UTC reading of the injected clock
    ts = now.isoformat()
    run_id = make_run_id(now)

    owns_store = store is None
    store = store or SnapshotStore(config.SQLITE_PATH)

    api_failures = 0
    latency_ms: dict[str, float] = {}
    owns_session = session is None
    if session is None:
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
        ticker_fetch = fetch_spot_ticker(session, read_clock)
        ticker_raw = ticker_fetch.raw
        latency_ms["spot_ticker_ms"] = (time.perf_counter() - t0) * 1000

        # Force AssetPairs refresh if the Ticker mentions a symbol we don't
        # have metadata for yet (new listing).
        asset_pairs, force_refreshed = get_asset_pairs(session, ticker_keys=set(ticker_raw.keys()))
        if force_refreshed:
            logger.info("AssetPairs force-refreshed: new symbol(s) in Ticker not in cache")

        parsed_rows: dict[str, SpotTickerRow] = {}
        spot_instruments: dict[str, InstrumentId] = {}
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
            instrument = spot_instrument(key, meta)
            if instrument is not None:
                spot_instruments[key] = instrument

        # Step 4: Futures Tickers. Optional - degrade to spot-only on failure.
        # Its serverTime is also this cycle's clock reference (T023b).
        futures_perpetuals: list[FuturesTickerRow] = []
        futures_timing: SourceTiming | None = None
        futures_status = "OK"
        clock_sample = ClockSample(synchronized=None, offset_uncertainty=None, previous_wall_time=now)
        clock_reference: dict[str, Any] = {"source": "kraken-futures tickers serverTime", "status": "unavailable"}
        t0 = time.perf_counter()
        try:
            sent_at = receipt_time(read_clock)
            futures_fetch = fetch_futures_tickers(session, read_clock)
            futures_perpetuals = parse_perpetuals(list(futures_fetch.rows), ts)
            futures_timing = futures_fetch.timing
            clock_sample, clock_reference = _venue_clock_sample(sent_at, futures_fetch.timing, sent_at)
        except ApiError as exc:
            api_failures += 1
            futures_status = "UNAVAILABLE"
            logger.warning("Futures public data unavailable: %s", exc)
        latency_ms["futures_ticker_ms"] = (time.perf_counter() - t0) * 1000

        # Step 4b (T023b): OC-1 integrity before any persistence or L1/L2
        # consumption. Hard-invalid rows are dropped here; valid-but-not-ready
        # rows are kept for persistence but never consumed below.
        gate_now = receipt_time(read_clock)
        clock_result = evaluate_clock(clock_sample, gate_now)
        if clock_result.status is not CheckStatus.PASS:
            logger.warning(
                "Integrity: clock is %s (%s) - freshness-sensitive consumption suspended this cycle",
                clock_result.status.value,
                ", ".join(reason.code.value for reason in clock_result.reasons),
            )
        ticker_results: dict[str, CapabilityResult] = {}
        ticker_observations: dict[str, TickerObservation] = {}
        spot_rows_rejected = 0
        for key in list(parsed_rows):
            spot_id = spot_instruments.get(key)
            if spot_id is None:
                continue  # no base/quote identity: build_spot_markets excludes it, nothing is consumed
            observation = _ticker_observation(parsed_rows[key], spot_id, ticker_fetch.timing)
            ticker_result = evaluate_ticker(observation, spot_id, gate_now)
            if ticker_result.hard_failure:
                spot_rows_rejected += 1
                del parsed_rows[key]
                logger.warning(
                    "Integrity: spot ticker %s rejected (%s)",
                    key,
                    ", ".join(reason.code.value for reason in ticker_result.reasons),
                )
                continue
            ticker_results[key] = ticker_result
            ticker_observations[key] = observation

        futures_expectations: dict[str, FuturesExpectation] = {}
        persisted_futures: list[FuturesTickerRow] = []
        eligible_futures: list[FuturesTickerRow] = []
        if futures_timing is not None:
            for fut_row in futures_perpetuals:
                fut_result, expectation = _evaluate_futures_row(fut_row, futures_timing, gate_now)
                if fut_result.hard_failure:
                    continue  # invalid observation: never persisted, never consumed
                persisted_futures.append(fut_row)
                if expectation is not None and _consumable(fut_result, clock_result):
                    eligible_futures.append(fut_row)
                    futures_expectations[fut_row.symbol] = expectation
        futures_rejected = len(futures_perpetuals) - len(eligible_futures)
        if futures_perpetuals and not eligible_futures:
            futures_status = "STALE"

        if missing_meta:
            logger.warning("%d ticker symbols had no resolvable AssetPairs metadata", missing_meta)

        # Step 5: normalization + grouping.
        markets, excluded_markets = build_spot_markets(parsed_rows, asset_pairs)
        for reason, keys in excluded_markets.items():
            if keys:
                logger.info("excluded %d spot markets (%s)", len(keys), reason)
        # Only futures that passed on their own can be matched to an asset.
        assets = group_assets(markets, eligible_futures)

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
            for r in persisted_futures
        ]

        store.insert_spot_snapshots_batch(spot_inputs)
        store.insert_futures_snapshots_batch(futures_inputs)
        snapshot_count = len(spot_inputs) + len(futures_inputs)

        pruned_spot, pruned_futures = store.prune(config.SNAPSHOT_RETENTION_DAYS, now=now)
        if pruned_spot or pruned_futures:
            logger.info("Pruned %d spot / %d futures snapshots past retention", pruned_spot, pruned_futures)

        # Step 7: L1 anomaly detection.
        # T023b: only assets whose selected spot ticker PASSes (under a PASS
        # clock) are consumed by L1; the BTC reference likewise, or it is
        # unavailable (None), never an unvalidated number.
        l1_entries = []
        l1_blocked: list[str] = []
        for entry in assets_eligible:
            primary = entry.primary_market
            if primary is not None and _consumable(ticker_results.get(primary.pair_key), clock_result):
                l1_entries.append(entry)
            else:
                l1_blocked.append(entry.asset)
        if l1_blocked:
            logger.warning("Integrity: %d eligible assets not consumed by L1 this cycle", len(l1_blocked))

        btc_entry = assets.get(config.BTC_ASSET)
        btc_pair = None
        btc_return_15m = None
        btc_return_1h = None
        if (
            btc_entry is not None
            and btc_entry.primary_market is not None
            and _consumable(ticker_results.get(btc_entry.primary_market.pair_key), clock_result)
        ):
            btc_last = btc_entry.primary_market.last
            btc_pair = btc_entry.primary_market.pair_key
            btc_return_15m = compute_return(btc_last, lookup_past_spot(store, btc_pair, now, 15))
            btc_return_1h = compute_return(btc_last, lookup_past_spot(store, btc_pair, now, 60))

        l1_by_asset: dict[str, tuple] = {}
        any_non_warmup = False
        for entry in l1_entries:
            m = entry.primary_market
            fut = entry.futures
            features = compute_features(
                store=store,
                asset=entry.asset,
                pair=m.pair_key,
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
            result = compute_anomaly(store, entry.asset, m.pair_key, now, features, btc_pair)
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
                    instrument=spot_instruments.get(m.pair_key),
                )
            )

        l2_integrity: dict[str, tuple[CapabilityResult, ...]] = {}
        l2_results, ohlc_requests_made, ohlc_failures_count = run_l2(
            store, session, l2_inputs, now, run_id,
            clock=read_clock, clock_sample=clock_sample, integrity_out=l2_integrity,
        )
        api_failures += ohlc_failures_count
        integrity_flags: dict[str, list[str]] = {
            asset: ["integrity_blocked:l2"] for asset in l2_integrity if asset not in l2_results
        }

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
                        instrument=spot_instruments.get(m.pair_key),
                        futures_instrument=(
                            futures_expectations[fut.symbol].instrument
                            if fut is not None and fut.symbol in futures_expectations
                            else None
                        ),
                    )
                )

            l2_candidates_count = sum(
                1 for c in l3_inputs
                if not c.l2_result.l2_features.l2_warmup and (c.l2_result.opportunity.score or 0) > 0
            )
            finalists = select_finalists(l3_inputs)
            l3_results, l3_requests_made, l3_failures_count = run_l3(
                session, finalists, clock=read_clock, clock_sample=clock_sample
            )
            api_failures += l3_failures_count

            # Evidence seal (T023b): re-evaluate every finalist's evidence just
            # before inference. A blocked snapshot reaches neither Qwen nor the
            # router; a futures instrument that no longer passes is dropped for
            # that opportunity only.
            seal_now = receipt_time(read_clock)
            seal_reports: dict[str, IntegrityReport] = {}
            futures_ok: dict[str, bool] = {}
            sealed: list[L3CandidateInput] = []
            for c in finalists:
                seal_entry = l1_by_asset[c.asset][0]
                selected = spot_instruments.get(c.spot_pair)
                seal_fut = seal_entry.futures
                expectation = futures_expectations.get(seal_fut.symbol) if seal_fut is not None else None
                if selected is None:
                    integrity_flags.setdefault(c.asset, []).append("integrity_blocked:seal")
                    continue
                report = _seal_evidence(
                    selected, ticker_observations.get(c.spot_pair), c.l2_result, l3_results[c.asset],
                    (expectation,) if expectation is not None else (), clock_sample, seal_now,
                )
                seal_reports[c.asset] = report
                futures_ok[c.asset] = seal_fut is not None and seal_fut.symbol in report.eligible_futures
                if report.opportunity_blocked:
                    integrity_flags.setdefault(c.asset, []).append("integrity_blocked:seal")
                    logger.warning(
                        "Integrity: %s blocked at evidence seal (%s)",
                        c.asset,
                        ", ".join(
                            f"{item.capability.value}:{item.subject}={item.status.value}"
                            for item in report.blocking_results
                        ),
                    )
                    continue
                if seal_fut is not None and not futures_ok[c.asset]:
                    integrity_flags.setdefault(c.asset, []).append("integrity_futures_dropped")
                sealed.append(c)

            qwen_candidates = [c for c in sealed if l3_results[c.asset].qwen_eligible]
            if qwen_candidates:
                qwen_payloads = [
                    _build_qwen_payload(
                        c.asset, l1_by_asset[c.asset][2].anomaly_score,
                        l1_by_asset[c.asset][2].features, c.l2_result, l3_results[c.asset],
                        futures_ok=futures_ok.get(c.asset, False),
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

            for c in sealed:
                l3r = l3_results[c.asset]
                c_futures_ok = futures_ok.get(c.asset, False)
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
                    derivatives_coherence_credit=(
                        c.l2_result.opportunity.breakdown.get("derivatives_coherence", 0.0) if c_futures_ok else 0.0
                    ),
                    taker_buy_ratio=l3r.taker_buy_ratio,
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
                    # T031b: the heartbeat only records demand. It reads the
                    # cooldown, then creates (or deduplicates) the event. It never
                    # charges a budget and never starts a cooldown: the bridge
                    # does both, once, when its atomic claim + reservation for
                    # this event is accepted (budgets.py, cooldown.py).
                    allowed, cooldown_reason = cooldown.check_cooldown(
                        store, c.asset, router_result.decision, now,
                        c.l2_result.setup.setup_type, c.l2_result.setup.direction, c.l2_result.opportunity.score,
                    )
                    if not allowed:
                        router_result.reasons.append(f"suppressed_by_cooldown:{cooldown_reason}")
                        continue

                    entry = l1_by_asset[c.asset][0]
                    fut = entry.futures if c_futures_ok else None
                    event_market = "FUTURES" if (c.futures_available and c_futures_ok) else "SPOT"
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
                        market=event_market,
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
                    # T023b: additive integrity record (no schema change - T030b).
                    event_context["integrity"] = deep_mark_unavailable(
                        _integrity_record(seal_reports[c.asset], clock_reference, l3r.futures_book_result)
                    )

                    event_id, created = events.create_event_if_new(
                        store, ts=ts, type_="RADAR_ALERT", asset=c.asset,
                        setup_type=c.l2_result.setup.setup_type, direction=c.l2_result.setup.direction,
                        market=event_market,
                        anomaly_score=l1_by_asset[c.asset][2].anomaly_score,
                        opportunity_score=c.l2_result.opportunity.score,
                        tradeability_score=l3r.tradeability.score,
                        confidence=router_result.confidence, model_demand=router_result.decision,
                        reason="; ".join(router_result.reasons), status="PENDING",
                        context=event_context,
                    )
                    if created:
                        event_status = "PENDING"
                    else:
                        # Dedup hit: an equivalent open event already exists. Report
                        # its real status; nothing is charged or started for it.
                        router_result.reasons.append("deduplicated_open_event")
                        existing = store.get_event(event_id)
                        event_status = existing["status"] if existing is not None else "UNKNOWN"
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
                flags=list(entry.flags) + integrity_flags.get(asset, []),
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
            "integrity": {
                "policy_version": POLICY_VERSION,
                "clock": clock_result.status.value,
                "clock_reasons": [reason.code.value for reason in clock_result.reasons],
                "clock_reference": clock_reference,
                "spot_rows_rejected": spot_rows_rejected,
                "l1_blocked": len(l1_blocked),
                "futures_eligible": len(eligible_futures),
                "futures_rejected": futures_rejected,
                "l2_blocked": sum(1 for flags in integrity_flags.values() if "integrity_blocked:l2" in flags),
                "seal_blocked": sum(1 for flags in integrity_flags.values() if "integrity_blocked:seal" in flags),
            },
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
        if owns_session:
            session.close()
        if owns_store:
            store.close()


def run_and_write(
    mode: str = "HEARTBEAT", output_path: str = config.OUTPUT_V08_PATH, full: bool = False
) -> dict[str, Any]:
    output = run_heartbeat(mode=mode, full=full)
    write_json(output, output_path)
    return output
