"""L3 orchestrator: order book + trades, finalists only (<= config.L3_MAX_FINALISTS
per cycle, never the shortlist and never the whole universe - task sections
1/2/18). Builds tradeability_score/state (a GATE) and the SPOT/FUTURES cost
preview, then applies the deterministic pre-gate that decides who is even
worth a Qwen call (task section 5).

Integrity (T023b, OC-1): when `run_l3` is given a `clock`, the spot book,
the spot trades and the futures book are fetched with their receipt time
and validated by `radar_v08.domain.integrity` before any metric is derived
from them. A response keyed by another pair is an identity mismatch, never
silently used. A capability that does not PASS (or an untrusted clock)
yields no metric at all - unavailable, never zero - and the taker-buy
ratio is passed on only when the trades PASS and the ratio is a finite
value in [0, 1]. The validated observations are kept on the result for the
evidence seal the caller runs before inference. Without a `clock` the
legacy path is unchanged.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from . import config, kraken_spot
from .adapters.kraken_timestamps import (
    FUTURES_QUOTE,
    Clock,
    epoch_seconds_to_utc,
    receipt_time,
)
from .adapters.kraken_timestamps import (
    fetch_futures_orderbook as fetch_futures_orderbook_timed,
)
from .domain.integrity import (
    BookLevel,
    BookSnapshot,
    CapabilityResult,
    CheckStatus,
    ClockSample,
    InstrumentId,
    Reason,
    ReasonCode,
    SourceTiming,
    Trade,
    TradeSide,
    TradesObservation,
    evaluate_book,
    evaluate_clock,
    evaluate_trades,
    status_from_reasons,
)
from .http_client import ApiError, GuardedSession
from .kraken_futures import fetch_orderbook as fetch_futures_orderbook
from .kraken_spot import TradeRow, fetch_depth, fetch_trades
from .l2 import L2Result
from .microstructure import DepthMetrics, TradesMetrics, compute_depth_metrics, compute_trades_metrics
from .router import valid_taker_buy_ratio
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
    # T023b: identities the spot book/trades and the futures book must belong
    # to. `instrument` is required when run_l3 runs with a clock.
    instrument: InstrumentId | None = None
    futures_instrument: InstrumentId | None = None


@dataclass
class L3Result:
    asset: str
    tradeability: TradeabilityResult
    cost_preview: dict[str, Any]
    qwen_eligible: bool
    flags: list[str] = field(default_factory=list)
    # T023b fields (defaults on the legacy path). `taker_buy_ratio` is None
    # whenever it is unavailable - absent, invalid or not validated - never 0.
    taker_buy_ratio: float | None = None
    integrity: tuple[CapabilityResult, ...] = ()
    book_observation: BookSnapshot | None = None
    trades_observation: TradesObservation | None = None
    trades_extra_reasons: tuple[Reason, ...] = ()
    futures_book_result: CapabilityResult | None = None


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


_TRADE_SIDES = {"b": TradeSide.BUY, "s": TradeSide.SELL}
_Levels = list[tuple[float, float]]


@dataclass(frozen=True)
class _CheckedBook:
    """One book fetched with its receipt time (T023b); `error` when not collected."""

    snapshot: BookSnapshot | None
    bids: _Levels
    asks: _Levels
    error: str | None


@dataclass(frozen=True)
class _CheckedTrades:
    """Trades fetched with their receipt time (T023b); `error` when not collected.

    `rows` are Kraken's rows as parsed (used for metrics only after the
    observation PASSes); `extra` holds reasons for rows that could not be
    represented in the observation (implausible time, side not b/s).
    """

    observation: TradesObservation | None
    rows: tuple[TradeRow, ...]
    extra: tuple[Reason, ...]
    error: str | None


def _pair_payload(result: Any, pair: str, expected: InstrumentId, skip: tuple[str, ...]) -> tuple[InstrumentId, Any]:
    """The payload for `pair`, or - if Kraken keyed the result by another pair -
    that payload labelled with the other pair, so the validator sees an
    identity mismatch instead of silently accepting another market's data."""
    if pair in result:
        return expected, result[pair]
    keys = [key for key in result if key not in skip]
    if keys:
        return replace(expected, symbol=str(keys[0])), result[keys[0]]
    return expected, None


def _fetch_spot_book_checked(
    session: GuardedSession, pair: str, expected: InstrumentId, clock: Clock
) -> _CheckedBook:
    try:
        result = kraken_spot._get_json(session, "Depth", {"pair": pair, "count": config.DEPTH_BOOK_COUNT})
        received = receipt_time(clock)
        observed, book = _pair_payload(result, pair, expected, ())
        if book is None:
            return _CheckedBook(None, [], [], "empty_depth_response")
        bids = [kraken_spot._parse_book_level(entry) for entry in book.get("bids", [])]
        asks = [kraken_spot._parse_book_level(entry) for entry in book.get("asks", [])]
    except ApiError as exc:
        return _CheckedBook(None, [], [], str(exc))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _CheckedBook(None, [], [], f"malformed_depth_response: {exc}")
    known = [moment for moment in (epoch_seconds_to_utc(epoch) for _, _, epoch in (*bids, *asks)) if moment is not None]
    snapshot = BookSnapshot(
        instrument=observed,
        bids=tuple(BookLevel(price, size) for price, size, _ in bids),
        asks=tuple(BookLevel(price, size) for price, size, _ in asks),
        price_unit=expected.quote,
        size_unit=expected.size_unit,
        timing=SourceTiming(received_at=received, source_time=max(known) if known else None),
    )
    return _CheckedBook(snapshot, [(p, v) for p, v, _ in bids], [(p, v) for p, v, _ in asks], None)


def _fetch_futures_book_checked(
    session: GuardedSession, symbol: str, expected: InstrumentId, clock: Clock
) -> _CheckedBook:
    try:
        fetched = fetch_futures_orderbook_timed(session, symbol, clock)
    except ApiError as exc:
        return _CheckedBook(None, [], [], str(exc))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _CheckedBook(None, [], [], f"malformed_futures_orderbook: {exc}")
    snapshot = BookSnapshot(
        instrument=expected,
        bids=tuple(BookLevel(price, size) for price, size in fetched.bids),
        asks=tuple(BookLevel(price, size) for price, size in fetched.asks),
        price_unit=FUTURES_QUOTE,
        size_unit=expected.size_unit,
        timing=fetched.timing,
    )
    return _CheckedBook(snapshot, list(fetched.bids), list(fetched.asks), None)


def _fetch_trades_checked(
    session: GuardedSession, pair: str, expected: InstrumentId, clock: Clock
) -> _CheckedTrades:
    try:
        result = kraken_spot._get_json(session, "Trades", {"pair": pair})
        received = receipt_time(clock)
        observed, raw = _pair_payload(result, pair, expected, ("last",))
        rows = tuple(kraken_spot._parse_trade_row(row) for row in (raw or []))
    except ApiError as exc:
        return _CheckedTrades(None, (), (), str(exc))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _CheckedTrades(None, (), (), f"malformed_trades_response: {exc}")
    trades: list[Trade] = []
    extra: list[Reason] = []
    for index, row in enumerate(rows):
        moment = epoch_seconds_to_utc(row.time)
        side = _TRADE_SIDES.get(row.side)
        if moment is None:
            extra.append(
                Reason(ReasonCode.INVALID_NUMBER, f"trades[{index}].time", f"not a plausible epoch time: {row.time!r}")
            )
            continue
        if side is None:
            extra.append(
                Reason(ReasonCode.MISSING_METADATA, f"trades[{index}].side", f"aggressor side not b/s: {row.side!r}")
            )
            continue
        trades.append(Trade(time=moment, price=row.price, size=row.volume, side=side))
    latest: datetime | None = max((trade.time for trade in trades), default=None)
    observation = TradesObservation(
        instrument=observed,
        trades=tuple(trades),
        price_unit=expected.quote,
        size_unit=expected.size_unit,
        timing=SourceTiming(received_at=received, source_time=latest),
    )
    return _CheckedTrades(observation, rows, tuple(extra), None)


def with_extra_reasons(result: CapabilityResult, extra: tuple[Reason, ...]) -> CapabilityResult:
    """`result` with `extra` reasons appended and its status re-derived."""
    if not extra:
        return result
    reasons = result.reasons + extra
    return CapabilityResult(
        result.capability,
        result.subject,
        status_from_reasons(reasons),
        reasons,
        result.time_basis,
        result.received_at,
        result.source_time,
    )


def run_l3(
    session: GuardedSession,
    candidates: list[L3CandidateInput],
    clock: Clock | None = None,
    clock_sample: ClockSample | None = None,
) -> tuple[dict[str, L3Result], int, int]:
    """Returns (results_by_asset, requests_made, failures). Only ever called
    on the already-selected finalist list (see `select_finalists`).

    With `clock` (T023b) every book/trades response is validated before any
    metric is derived from it (see the module docstring).
    """
    if not candidates:
        return {}, 0, 0
    if clock is not None:
        return _run_l3_checked(session, candidates, clock, clock_sample)

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


def _run_l3_checked(
    session: GuardedSession,
    candidates: list[L3CandidateInput],
    clock: Clock,
    clock_sample: ClockSample | None,
) -> tuple[dict[str, L3Result], int, int]:
    """T023b path of `run_l3`: validate every book/trades response before use."""
    # No clock evidence at all is UNKNOWN (never "synchronised").
    sample = clock_sample if clock_sample is not None else ClockSample(None, None, None)
    books: dict[str, _CheckedBook] = {}
    futures_books: dict[str, _CheckedBook] = {}
    trades_by_asset: dict[str, _CheckedTrades] = {}
    requests_made = 0
    failures = 0

    with ThreadPoolExecutor(max_workers=config.L3_FETCH_WORKERS) as pool:
        book_jobs = {
            c.asset: pool.submit(_fetch_spot_book_checked, session, c.spot_pair, c.instrument, clock)
            for c in candidates
            if c.instrument is not None
        }
        trades_jobs = {
            c.asset: pool.submit(_fetch_trades_checked, session, c.spot_pair, c.instrument, clock)
            for c in candidates
            if c.instrument is not None
        }
        futures_jobs = {
            c.asset: pool.submit(_fetch_futures_book_checked, session, c.futures_symbol, c.futures_instrument, clock)
            for c in candidates
            if c.futures_available and c.futures_symbol and c.futures_instrument is not None
        }
        for asset, job in book_jobs.items():
            books[asset] = job.result()
        for asset, trades_job in trades_jobs.items():
            trades_by_asset[asset] = trades_job.result()
        for asset, job in futures_jobs.items():
            futures_books[asset] = job.result()

    for label, outcomes in (("Depth", books), ("Futures orderbook", futures_books)):
        for asset, fetched_book in outcomes.items():
            requests_made += 1
            if fetched_book.error:
                failures += 1
                logger.warning("%s fetch failed for %s: %s", label, asset, fetched_book.error)
    for asset, fetched_trades in trades_by_asset.items():
        requests_made += 1
        if fetched_trades.error:
            failures += 1
            logger.warning("Trades fetch failed for %s: %s", asset, fetched_trades.error)

    results: dict[str, L3Result] = {}
    for c in candidates:
        evaluated_at = receipt_time(clock)  # an aware UTC reading of the injected clock
        clock_result = evaluate_clock(sample, evaluated_at)
        clock_ok = clock_result.status is CheckStatus.PASS
        flags: list[str] = []
        if not clock_ok:
            flags.append(f"INTEGRITY_CLOCK_{clock_result.status.value}")

        depth: DepthMetrics | None = None
        trades: TradesMetrics | None = None
        integrity: tuple[CapabilityResult, ...] = (clock_result,)
        book = books.get(c.asset)
        fetched = trades_by_asset.get(c.asset)
        if c.instrument is not None and book is not None and fetched is not None:
            book_result = evaluate_book(book.snapshot, c.instrument, evaluated_at)
            trades_result = with_extra_reasons(
                evaluate_trades(fetched.observation, c.instrument, evaluated_at), fetched.extra
            )
            integrity = (book_result, trades_result, clock_result)
            if clock_ok and book_result.status is CheckStatus.PASS:
                depth = compute_depth_metrics(book.bids, book.asks, config.REFERENCE_ORDER_SIZE_USD)
            else:
                flags.append(f"INTEGRITY_BOOK_{book_result.status.value}")
            if clock_ok and trades_result.status is CheckStatus.PASS:
                trades = compute_trades_metrics(fetched.rows)
            else:
                flags.append(f"INTEGRITY_TRADES_{trades_result.status.value}")
        else:
            flags.append("INTEGRITY_NO_INSTRUMENT")
            book, fetched = None, None

        futures_depth: DepthMetrics | None = None
        futures_result: CapabilityResult | None = None
        futures_book = futures_books.get(c.asset)
        if c.futures_instrument is not None and futures_book is not None:
            futures_result = evaluate_book(futures_book.snapshot, c.futures_instrument, evaluated_at)
            if clock_ok and futures_result.status is CheckStatus.PASS:
                futures_depth = compute_depth_metrics(
                    futures_book.bids, futures_book.asks, config.REFERENCE_ORDER_SIZE_USD
                )
            else:
                flags.append(f"INTEGRITY_FUTURES_BOOK_{futures_result.status.value}")

        if depth is None:
            flags.append("ORDER_BOOK_UNAVAILABLE")
        if trades is None:
            flags.append("TRADES_UNAVAILABLE")
        if c.futures_available and futures_depth is None:
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

        cost_preview = build_cost_preview(
            market="FUTURES" if c.futures_available else "SPOT",
            spot_spread_bps=c.spread_bps,
            spot_depth=depth,
            futures_available=c.futures_available,
            futures_spread_bps=c.futures_spread_bps,
            futures_depth=futures_depth,
            funding_rate_raw=c.funding_rate_raw,
        )

        results[c.asset] = L3Result(
            asset=c.asset,
            tradeability=tradeability,
            cost_preview=cost_preview,
            qwen_eligible=passes_qwen_pregate(c, tradeability),
            flags=flags,
            taker_buy_ratio=valid_taker_buy_ratio(trades.taker_buy_ratio) if trades is not None else None,
            integrity=integrity,
            book_observation=book.snapshot if book is not None else None,
            trades_observation=fetched.observation if fetched is not None else None,
            trades_extra_reasons=fetched.extra if fetched is not None else (),
            futures_book_result=futures_result,
        )

    return results, requests_made, failures
