"""Public Kraken Spot market data: AssetPairs (cached) + global Ticker.

Only GET /0/public/AssetPairs and GET /0/public/Ticker are used, both public,
both called once per heartbeat (Ticker) or once per cache TTL (AssetPairs).
No per-asset requests happen here (architecture doc: "CHEAP GLOBAL").

`fetch_ohlc` below IS a per-asset request (architecture doc: "CANDIDATE
ONLY") - it must only ever be called for the L1 shortlist, incrementally via
`since`, never for the whole universe.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import config
from .http_client import ApiError, GuardedSession


class SpotApiError(ApiError):
    pass


@dataclass
class SpotTickerRow:
    key: str
    display: str
    base_raw: str
    quote_raw: str
    status: str
    last: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    volume_today: float
    volume_24h: float
    vwap_today: float
    vwap_24h: float
    trades_today: int
    trades_24h: int
    high_today: float
    low_today: float
    high_24h: float
    low_24h: float
    open_today: float
    timestamp: str


def _get_json(session: GuardedSession, path: str, params: dict[str, Any] | None = None) -> Any:
    response = session.get(f"{config.SPOT_URL}/{path}", params=params)
    payload = response.json()
    if payload.get("error"):
        raise SpotApiError(f"Spot API error on {path}: {payload['error']}")
    return payload["result"]


def fetch_asset_pairs(session: GuardedSession) -> dict[str, dict[str, Any]]:
    return _get_json(session, "AssetPairs")


def fetch_ticker(session: GuardedSession) -> dict[str, dict[str, Any]]:
    return _get_json(session, "Ticker")


def load_asset_pairs_cache(
    path: str = config.ASSET_PAIRS_CACHE_PATH,
) -> tuple[dict[str, Any] | None, float, set[str]]:
    if not os.path.exists(path):
        return None, 0.0, set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        missing_keys = set(payload.get("missing_keys", []))
        return payload.get("data"), float(payload.get("fetched_at", 0.0)), missing_keys
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return None, 0.0, set()


def save_asset_pairs_cache(
    data: dict[str, Any],
    path: str = config.ASSET_PAIRS_CACHE_PATH,
    missing_keys: set[str] | None = None,
) -> None:
    payload = {
        "fetched_at": time.time(),
        "data": data,
        "missing_keys": sorted(missing_keys or []),
    }
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp_path, path)


def get_asset_pairs(
    session: GuardedSession,
    ticker_keys: set[str] | None = None,
    cache_path: str = config.ASSET_PAIRS_CACHE_PATH,
    ttl_seconds: int = config.ASSET_PAIRS_CACHE_TTL_SECONDS,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Return (asset_pairs, refreshed). Cached 24h; force-refreshed if a
    symbol seen in the Ticker payload is missing from the cache (new listing).

    Symbols that AssetPairs genuinely never describes (e.g. margin-only or
    futures-linked tickers with no spot pair metadata) are remembered as
    `missing_keys` so they don't trigger a fresh refresh on every single
    heartbeat - only an actual new, previously-unseen listing does.
    """
    cached, fetched_at, known_missing = load_asset_pairs_cache(cache_path)
    age = time.time() - fetched_at

    if cached is not None and age <= ttl_seconds:
        unseen = (ticker_keys - set(cached.keys()) - known_missing) if ticker_keys else set()
        if not unseen:
            return cached, False

    data = fetch_asset_pairs(session)
    missing_keys = (ticker_keys - set(data.keys())) if ticker_keys else set()
    save_asset_pairs_cache(data, cache_path, missing_keys=missing_keys)
    return data, True


def parse_ticker_row(key: str, row: dict[str, Any], meta: dict[str, Any], timestamp: str) -> SpotTickerRow:
    display = str(meta.get("wsname") or meta.get("altname") or key)
    parts = display.split("/")
    base_raw = parts[0].upper() if len(parts) == 2 else ""
    quote_raw = parts[1].upper() if len(parts) == 2 else ""
    status = str(meta.get("status", "online")).lower()

    ask = row["a"]
    bid = row["b"]
    close = row["c"]
    vol = row["v"]
    vwap = row["p"]
    trades = row["t"]
    high = row["h"]
    low = row["l"]

    return SpotTickerRow(
        key=str(key),
        display=display,
        base_raw=base_raw,
        quote_raw=quote_raw,
        status=status,
        last=float(close[0]),
        bid=float(bid[0]),
        ask=float(ask[0]),
        bid_size=float(bid[2]),
        ask_size=float(ask[2]),
        volume_today=float(vol[0]),
        volume_24h=float(vol[1]),
        vwap_today=float(vwap[0]),
        vwap_24h=float(vwap[1]),
        trades_today=int(trades[0]),
        trades_24h=int(trades[1]),
        high_today=float(high[0]),
        low_today=float(low[0]),
        high_24h=float(high[1]),
        low_24h=float(low[1]),
        open_today=float(row["o"]),
        timestamp=timestamp,
    )


@dataclass
class OhlcBar:
    bar_time: str  # ISO8601 UTC, the bar's open time
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    trades: int


def _parse_ohlc_row(row: list[Any]) -> OhlcBar:
    epoch, open_, high, low, close, vwap, volume, trades = row[:8]
    bar_time = datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    return OhlcBar(
        bar_time=bar_time, open=float(open_), high=float(high), low=float(low),
        close=float(close), vwap=float(vwap), volume=float(volume), trades=int(trades),
    )


def fetch_ohlc(
    session: GuardedSession, pair: str, interval: int = 5, since: int | None = None
) -> tuple[list[OhlcBar], int | None]:
    """OHLC for a single pair - CANDIDATE ONLY, never called for the whole
    universe. `since` (epoch seconds) makes this incremental: Kraken returns
    only bars newer than `since`, plus a `last` cursor to pass next time.
    Without `since` (first time a pair is shortlisted) Kraken backfills up to
    ~720 5m bars (~60h) in one call - a one-time cost, not a per-heartbeat one.
    """
    params: dict[str, Any] = {"pair": pair, "interval": interval}
    if since is not None:
        params["since"] = since

    result = _get_json(session, "OHLC", params)

    last = result.get("last")
    keys = [k for k in result if k != "last"]
    pair_key = pair if pair in result else (keys[0] if keys else None)
    raw_rows = result.get(pair_key, []) if pair_key else []

    bars = [_parse_ohlc_row(r) for r in raw_rows]
    return bars, (int(last) if last is not None else None)


@dataclass
class TradeRow:
    price: float
    volume: float
    time: float  # epoch seconds
    side: str  # "b" | "s" - as reported by Kraken, never re-derived
    order_type: str
    misc: str


def _parse_trade_row(row: list[Any]) -> TradeRow:
    price, volume, time_, side, order_type, misc = row[:6]
    return TradeRow(
        price=float(price), volume=float(volume), time=float(time_),
        side=str(side), order_type=str(order_type), misc=str(misc),
    )


def _parse_book_level(entry: list[Any]) -> tuple[float, float, float | None]:
    """One Depth bid/ask entry: ``[price, volume]`` or ``[price, volume,
    timestamp]`` (epoch seconds). Kraken includes the per-level update time
    on this endpoint; a missing third element is ``None``, never a
    fabricated 0 (T022b: radar_v08/adapters maps this into ``SourceTiming``).
    """
    price, volume, *rest = entry
    return float(price), float(volume), _level_epoch(rest[0] if rest else None)


def _level_epoch(raw: Any) -> float | None:
    """Kraken's per-level time as a finite float, or ``None``.

    Tolerant on purpose: the level's time is an extra field, so an absent,
    empty, non-numeric (``''``, ``'x'``), boolean or non-finite (NaN, inf)
    value makes only that level's time unknown - it never fails the fetch.
    Range/unit checks (e.g. a milliseconds-sized value) belong to
    ``radar_v08.adapters.kraken_timestamps``, which never rescales.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fetch_depth(session: GuardedSession, pair: str, count: int = 25) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Order book for a single pair - FINALIST ONLY (architecture doc), never
    called for the shortlist or the whole universe. Returns (bids, asks) as
    (price, volume) tuples, best price first (Kraken already orders them so).
    """
    result = _get_json(session, "Depth", {"pair": pair, "count": count})
    keys = list(result.keys())
    pair_key = pair if pair in result else (keys[0] if keys else None)
    book = result.get(pair_key, {}) if pair_key else {}

    bids = [(float(p), float(v)) for p, v, *_ in book.get("bids", [])]
    asks = [(float(p), float(v)) for p, v, *_ in book.get("asks", [])]
    return bids, asks


def fetch_depth_with_times(
    session: GuardedSession, pair: str, count: int = 25
) -> tuple[list[tuple[float, float, float | None]], list[tuple[float, float, float | None]]]:
    """Same request and caller contract as `fetch_depth`, plus each level's
    Kraken-reported update time (epoch seconds) when the response supplies
    one. T022b: exposes a field the response already carries and
    `fetch_depth` used to discard; not wired to any consumer here (see
    radar_v08/adapters, T023b wires a validator). `fetch_depth` keeps its
    own original parsing and does not go through this function, so a
    malformed level time can never change what current consumers get.
    """
    result = _get_json(session, "Depth", {"pair": pair, "count": count})
    keys = list(result.keys())
    pair_key = pair if pair in result else (keys[0] if keys else None)
    book = result.get(pair_key, {}) if pair_key else {}

    bids = [_parse_book_level(entry) for entry in book.get("bids", [])]
    asks = [_parse_book_level(entry) for entry in book.get("asks", [])]
    return bids, asks


def fetch_trades(
    session: GuardedSession, pair: str, since: float | None = None
) -> tuple[list[TradeRow], str | None]:
    """Recent public trades for a single pair - FINALIST ONLY. Returns
    (trades, last) where `last` is Kraken's opaque pagination cursor.
    """
    params: dict[str, Any] = {"pair": pair}
    if since is not None:
        params["since"] = since

    result = _get_json(session, "Trades", params)
    last = result.get("last")
    keys = [k for k in result if k != "last"]
    pair_key = pair if pair in result else (keys[0] if keys else None)
    raw_rows = result.get(pair_key, []) if pair_key else []

    trades = [_parse_trade_row(r) for r in raw_rows]
    return trades, (str(last) if last is not None else None)
