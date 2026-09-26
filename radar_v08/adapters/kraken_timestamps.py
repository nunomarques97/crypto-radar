"""Kraken public-adapter time mapping.

For every fetch `heartbeat`/`L2`/`L3` use - spot ticker, spot OHLC, spot
depth, spot trades, futures tickers, futures order book - this module
provides:

* the receipt time (UTC), read once from an injected `Clock` right after
  the response has been received and parsed (never before the request),
  never the wall clock directly;
* the source time, only when Kraken's own payload actually supplies one
  (spot Depth's per-level timestamp, Kraken Futures' `serverTime`, a spot
  OHLC bar's own open time, a trade's own time); absent -> `None`
  (`SourceTiming` then reads as `TimeBasis.RECEIPT_ONLY` once evaluated -
  see `radar_v08.domain.integrity`), never invented;
* the venue and the instrument identity (`InstrumentId`), built from data
  the caller already has (AssetPairs metadata for spot, the already
  normalized `asset` on a parsed futures ticker row).

Additive only: `radar_v08.kraken_spot` and `radar_v08.kraken_futures`'s
existing fetch functions and every current caller (heartbeat/l2/l3) are
unchanged and behave exactly as before. Nothing here is wired to a
consumer or to the OC-1 validator yet; this module has no callers
in production code.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import kraken_futures, kraken_spot
from ..domain.integrity import InstrumentId, InstrumentKind, SourceTiming
from ..http_client import GuardedSession
from ..kraken_spot import OhlcBar, TradeRow
from ..normalize import normalize_asset, split_display_pair

Clock = Callable[[], datetime]

VENUE_SPOT = "kraken"
# Kraken Futures perpetuals (`PF_...`) are USD-margined only - no other
# quote asset exists for this product (mirrors kraken_futures._base_from_pair).
VENUE_FUTURES = "kraken-futures"
FUTURES_QUOTE = "USD"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Plausible range for a Kraken epoch-seconds value: after the epoch and
# before 2100-01-01T00:00:00Z. Anything outside (a milliseconds-sized value
# such as 1700000000000, a negative number) is not rescaled or guessed - it
# becomes `None` (RECEIPT_ONLY), because a guessed unit is a fabricated time.
MAX_EPOCH_SECONDS = 4_102_444_800.0


def _require_aware(moment: datetime, name: str) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")


def receipt_time(clock: Clock) -> datetime:
    """Call the injected clock once and return it normalized to UTC.

    This is the only place in this module a clock is read; every fetch
    below takes `clock` as a parameter rather than reading time itself, and
    calls it only after the response has arrived and been parsed, so
    `received_at` is the receipt moment, not the request moment.
    """
    now = clock()
    _require_aware(now, "clock()")
    return now.astimezone(timezone.utc)


def epoch_seconds_to_utc(epoch_seconds: float | None) -> datetime | None:
    """Kraken epoch seconds -> aware UTC datetime, or `None`.

    `None` for an absent, non-finite (NaN, inf), non-positive or
    out-of-range value (see `MAX_EPOCH_SECONDS`). Never raises, never
    rescales milliseconds, and does not use `datetime.fromtimestamp` (its
    range and errors are platform-dependent: `OSError` on Windows).
    """
    if epoch_seconds is None or not math.isfinite(epoch_seconds):
        return None
    if not 0.0 < epoch_seconds < MAX_EPOCH_SECONDS:
        return None
    return _EPOCH + timedelta(seconds=epoch_seconds)


def _parse_kraken_server_time(raw: str | None) -> datetime | None:
    """Kraken Futures' `serverTime` (ISO8601, normally `Z`-suffixed).

    `None` when absent or unparsable - a malformed or missing `serverTime`
    never blocks the fetch and never fabricates a time; it only means this
    one field falls back to `None` (`RECEIPT_ONLY`).
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --- spot ticker -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpotTickerFetch:
    """Kraken's public Ticker never carries a timestamp: `source_time` is
    always `None` here, i.e. always `RECEIPT_ONLY` - never fabricated.
    """

    raw: dict[str, dict[str, Any]]
    timing: SourceTiming


def fetch_spot_ticker(session: GuardedSession, clock: Clock, pairs: list[str] | None = None) -> SpotTickerFetch:
    raw = kraken_spot.fetch_ticker(session) if pairs is None else kraken_spot.fetch_ticker(session, pairs)
    received = receipt_time(clock)
    return SpotTickerFetch(raw=raw, timing=SourceTiming(received_at=received, source_time=None))


def spot_instrument(key: str, meta: dict[str, Any]) -> InstrumentId | None:
    """Domain instrument identity for one spot Ticker/OHLC/Depth/Trades pair,
    from its AssetPairs metadata. `None` when the pair display can't be
    split into base/quote - the caller decides what to do; never a
    placeholder identity.
    """
    display = str(meta.get("wsname") or meta.get("altname") or key)
    split = split_display_pair(display)
    if split is None:
        return None
    base_raw, quote_raw = split
    base = normalize_asset(base_raw)
    quote = normalize_asset(quote_raw)
    return InstrumentId(
        venue=VENUE_SPOT, symbol=display, kind=InstrumentKind.SPOT, base=base, quote=quote, size_unit=base
    )


# --- spot OHLC ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpotOhlcFetch:
    """`source_time` is the last returned bar's own open time (every bar
    already carries Kraken's epoch) - `None` only when no bar came back.

    Caution for a future consumer: Kraken's last OHLC bar is normally the one still
    forming, so this is its *open* time - up to one full interval older
    than the data in it. It is not a data-freshness measure; OC-1 OHLC
    checks use their own `last_close` (`evaluate_ohlc`).
    """

    bars: tuple[OhlcBar, ...]
    last: int | None
    timing: SourceTiming


def fetch_spot_ohlc(
    session: GuardedSession, pair: str, clock: Clock, interval: int = 5, since: int | None = None
) -> SpotOhlcFetch:
    bars, last = kraken_spot.fetch_ohlc(session, pair, interval=interval, since=since)
    received = receipt_time(clock)
    source_time = datetime.fromisoformat(bars[-1].bar_time) if bars else None
    return SpotOhlcFetch(
        bars=tuple(bars), last=last, timing=SourceTiming(received_at=received, source_time=source_time)
    )


# --- spot depth --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpotDepthFetch:
    """`bids`/`asks` are `(price, volume, source_time)` triplets, best price
    first; `source_time` is `None` for a level Kraken did not timestamp or
    whose time is not a plausible epoch-seconds value (non-numeric,
    NaN/inf, milliseconds-sized - see `epoch_seconds_to_utc`); the other
    levels are unaffected.
    The fetch-level `timing.source_time` is the freshest of every level's
    time (the book is at least that fresh), or `None` if none was supplied.
    """

    bids: tuple[tuple[float, float, datetime | None], ...]
    asks: tuple[tuple[float, float, datetime | None], ...]
    timing: SourceTiming


def fetch_spot_depth(session: GuardedSession, pair: str, clock: Clock, count: int = 25) -> SpotDepthFetch:
    raw_bids, raw_asks = kraken_spot.fetch_depth_with_times(session, pair, count)
    received = receipt_time(clock)
    bids = tuple((price, volume, epoch_seconds_to_utc(epoch)) for price, volume, epoch in raw_bids)
    asks = tuple((price, volume, epoch_seconds_to_utc(epoch)) for price, volume, epoch in raw_asks)
    known_times = [moment for _, _, moment in (*bids, *asks) if moment is not None]
    source_time = max(known_times) if known_times else None
    return SpotDepthFetch(bids=bids, asks=asks, timing=SourceTiming(received_at=received, source_time=source_time))


# --- spot trades -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpotTradesFetch:
    """`source_time` is the latest valid trade time, or `None` when Kraken
    returned no trades (unavailable, never zero - see
    `radar_v08.domain.integrity.evaluate_trades`, `NO_TRADES`) or none of
    the trade times is a plausible epoch-seconds value
    (`epoch_seconds_to_utc`). `trades` is passed through unchanged.
    """

    trades: tuple[TradeRow, ...]
    last: str | None
    timing: SourceTiming


def fetch_spot_trades(session: GuardedSession, pair: str, clock: Clock, since: float | None = None) -> SpotTradesFetch:
    trades, last = kraken_spot.fetch_trades(session, pair, since=since)
    received = receipt_time(clock)
    known_times = [moment for moment in (epoch_seconds_to_utc(trade.time) for trade in trades) if moment is not None]
    source_time = max(known_times) if known_times else None
    return SpotTradesFetch(
        trades=tuple(trades), last=last, timing=SourceTiming(received_at=received, source_time=source_time)
    )


# --- futures tickers -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FuturesTickersFetch:
    """`source_time` is Kraken Futures' own `serverTime`, when the envelope
    supplies one and it parses; `None` otherwise (RECEIPT_ONLY) - a
    malformed `serverTime` never contaminates `rows`.
    """

    rows: tuple[dict[str, Any], ...]
    timing: SourceTiming


def fetch_futures_tickers(session: GuardedSession, clock: Clock) -> FuturesTickersFetch:
    rows, server_time_raw = kraken_futures.fetch_tickers_with_server_time(session)
    received = receipt_time(clock)
    source_time = _parse_kraken_server_time(server_time_raw)
    return FuturesTickersFetch(
        rows=tuple(rows), timing=SourceTiming(received_at=received, source_time=source_time)
    )


def futures_instrument(symbol: str, asset: str) -> InstrumentId:
    """Domain instrument identity for one futures perpetual. `asset` is the
    already-normalized base (`radar_v08.kraken_futures.parse_perpetuals`'s
    `FuturesTickerRow.asset`) - this function does not re-derive it.
    """
    return InstrumentId(
        venue=VENUE_FUTURES,
        symbol=symbol,
        kind=InstrumentKind.FUTURES,
        base=asset,
        quote=FUTURES_QUOTE,
        size_unit=asset,
    )


# --- futures order book --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FuturesOrderbookFetch:
    """`source_time` is Kraken Futures' own `serverTime` for this response,
    when supplied and parseable; `None` otherwise (RECEIPT_ONLY).
    """

    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    timing: SourceTiming


def fetch_futures_orderbook(session: GuardedSession, symbol: str, clock: Clock) -> FuturesOrderbookFetch:
    bids, asks, server_time_raw = kraken_futures.fetch_orderbook_with_server_time(session, symbol)
    received = receipt_time(clock)
    source_time = _parse_kraken_server_time(server_time_raw)
    return FuturesOrderbookFetch(
        bids=tuple(bids), asks=tuple(asks), timing=SourceTiming(received_at=received, source_time=source_time)
    )
