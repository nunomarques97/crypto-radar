"""Public Kraken Futures market data: global tickers, perpetuals only.

Only GET /derivatives/api/v3/tickers is used. Funding fields are kept RAW
and unverified in Phase 1 - no period, frequency, or direction is inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config
from .http_client import ApiError, GuardedSession
from .normalize import normalize_asset


class FuturesApiError(ApiError):
    pass


@dataclass
class FuturesTickerRow:
    symbol: str
    asset: str
    last: float | None
    mark_price: float | None
    index_price: float | None
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    volume_quote: float | None
    open_interest: float | None
    funding_rate_raw: float | None
    funding_prediction_raw: float | None
    open_24h: float | None
    last_time: str | None
    suspended: bool
    post_only: bool
    tag: str | None
    timestamp: str


def fetch_tickers(session: GuardedSession) -> list[dict[str, Any]]:
    rows, _server_time_raw = fetch_tickers_with_server_time(session)
    return rows


def fetch_tickers_with_server_time(session: GuardedSession) -> tuple[list[dict[str, Any]], str | None]:
    """Same request and caller contract as `fetch_tickers`, plus Kraken
    Futures' own `serverTime` (raw ISO8601 string, unparsed) when the
    envelope supplies one. T022b: exposes a field the response already
    carries and `fetch_tickers` used to discard; not wired to any consumer
    here (see radar_v08/adapters, T023b wires a validator).
    """
    response = session.get(f"{config.FUTURES_URL}/tickers")
    payload = response.json()
    if payload.get("result") != "success":
        raise FuturesApiError(f"Futures API error: {payload.get('error') or payload.get('result')}")
    rows = payload.get("tickers", [])
    if not isinstance(rows, list):
        raise FuturesApiError("Futures payload 'tickers' is not a list")
    server_time = payload.get("serverTime")
    return [row for row in rows if isinstance(row, dict)], (server_time if isinstance(server_time, str) else None)


def fetch_orderbook(session: GuardedSession, symbol: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Public Futures order book for one perpetual - FINALIST ONLY, and only
    when a matching perpetual exists (architecture doc section 3: "Futures:
    ... obter order book público do contrato"). Returns (bids, asks) as
    (price, volume) tuples, best price first.
    """
    bids, asks, _server_time_raw = fetch_orderbook_with_server_time(session, symbol)
    return bids, asks


def fetch_orderbook_with_server_time(
    session: GuardedSession, symbol: str
) -> tuple[list[tuple[float, float]], list[tuple[float, float]], str | None]:
    """Same request and caller contract as `fetch_orderbook`, plus Kraken
    Futures' own `serverTime` (raw ISO8601 string, unparsed) when the
    envelope supplies one. T022b: exposes a field the response already
    carries and `fetch_orderbook` used to discard; not wired to any consumer
    here (see radar_v08/adapters, T023b wires a validator).
    """
    response = session.get(f"{config.FUTURES_URL}/orderbook", params={"symbol": symbol})
    payload = response.json()
    if payload.get("result") != "success":
        raise FuturesApiError(f"Futures orderbook error for {symbol}: {payload.get('error') or payload.get('result')}")

    book = payload.get("orderBook", {})
    bids = [(float(p), float(v)) for p, v, *_ in book.get("bids", [])]
    asks = [(float(p), float(v)) for p, v, *_ in book.get("asks", [])]
    server_time = payload.get("serverTime")
    return bids, asks, (server_time if isinstance(server_time, str) else None)


def _base_from_pair(row: dict[str, Any]) -> str:
    pair = str(row.get("pair") or "")
    if pair:
        base = pair.split(":")[0].upper() if ":" in pair else pair.upper()
        return normalize_asset(base)
    # Fallback: derive from symbol PF_<BASE><QUOTE>, quote assumed USD-like.
    symbol = str(row.get("symbol") or "").upper()
    body = symbol[len("PF_"):] if symbol.startswith("PF_") else symbol
    for quote in ("USDT", "USD"):
        if body.endswith(quote):
            return normalize_asset(body[: -len(quote)])
    return normalize_asset(body)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_perpetuals(rows: list[dict[str, Any]], timestamp: str) -> list[FuturesTickerRow]:
    """Filter to PF_ perpetuals and parse into typed rows.

    Multiple raw rows could in principle map to the same normalized asset;
    callers are responsible for de-duplication (kept as a 1:1 pass-through
    here so no data is silently dropped before the universe layer decides).
    """
    out: list[FuturesTickerRow] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol.startswith("PF_"):
            continue

        asset = _base_from_pair(row)
        if not asset:
            continue

        out.append(
            FuturesTickerRow(
                symbol=symbol,
                asset=asset,
                last=_to_float(row.get("last")),
                mark_price=_to_float(row.get("markPrice")),
                index_price=_to_float(row.get("indexPrice")),
                bid=_to_float(row.get("bid")),
                ask=_to_float(row.get("ask")),
                bid_size=_to_float(row.get("bidSize")),
                ask_size=_to_float(row.get("askSize")),
                volume_quote=_to_float(row.get("volumeQuote")),
                open_interest=_to_float(row.get("openInterest")),
                funding_rate_raw=_to_float(row.get("fundingRate")),
                funding_prediction_raw=_to_float(row.get("fundingRatePrediction")),
                open_24h=_to_float(row.get("open24h")),
                last_time=str(row.get("lastTime")) if row.get("lastTime") is not None else None,
                suspended=bool(row.get("suspended", False)),
                post_only=bool(row.get("postOnly", False)),
                tag=str(row.get("tag")) if row.get("tag") is not None else None,
                timestamp=timestamp,
            )
        )
    return out
