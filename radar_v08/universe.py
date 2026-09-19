"""Universe discovery: normalize, group by base asset, apply exclusion gates.

No coin whitelist anywhere in this module. What is hardcoded is only:
- the allowed quote currencies + their priority (venue config),
- the legacy asset-code map (venue config),
- known stablecoin/fiat asset codes (venue config),
all explicitly sanctioned as acceptable by the architecture doc (section 2).
Everything else (which coins exist) comes straight from AssetPairs/Ticker, so
a newly listed coin appears with zero code changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import config
from .kraken_futures import FuturesTickerRow
from .kraken_spot import SpotTickerRow
from .normalize import is_fiat, is_known_stable, is_stable_like, normalize_asset


@dataclass
class SpotMarket:
    pair_key: str
    display: str
    asset: str
    quote: str
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
    volume_24h_usd: float
    spread_bps: float
    range_24h_pct: float
    quote_fallback: bool = False
    non_crypto_like: bool = False


@dataclass
class AssetEntry:
    asset: str
    markets: list[SpotMarket] = field(default_factory=list)
    primary_market: SpotMarket | None = None
    aggregate_volume_24h_usd: float = 0.0
    futures: FuturesTickerRow | None = None
    eligible: bool = False
    tradeable: bool = False
    excluded_reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def find_meta(pairs: dict[str, dict[str, Any]], key: str) -> dict[str, Any] | None:
    meta = pairs.get(key)
    if meta is not None:
        return meta
    upper = str(key).upper()
    for pk, pm in pairs.items():
        if upper in {
            str(pk).upper(),
            str(pm.get("altname", "")).upper(),
            str(pm.get("wsname", "")).upper(),
        }:
            return pm
    return None


def _quote_to_usd(quote: str, value: float, eur_usd_rate: float) -> float:
    if quote in {"USD", "USDT", "USDC"}:
        return value
    if quote == "EUR":
        return value * eur_usd_rate
    return value


def _find_eur_usd_rate(rows: dict[str, SpotTickerRow]) -> float:
    for row in rows.values():
        if row.base_raw.upper() == "EUR" and row.quote_raw.upper() == "USD":
            return row.last
    return config.EUR_USD_FALLBACK_RATE


def _is_non_crypto_like(meta: dict[str, Any] | None) -> bool:
    if not meta:
        return False
    aclass = str(meta.get("aclass_base", "")).lower()
    if aclass and aclass != "currency":
        return True
    return False


def build_spot_markets(
    ticker_rows: dict[str, SpotTickerRow],
    asset_pairs: dict[str, dict[str, Any]],
) -> tuple[list[SpotMarket], dict[str, list[str]]]:
    """Turn raw ticker rows into normalized SpotMarket records.

    Returns (markets, excluded) where `excluded` maps a reason to the list of
    pair keys skipped for it (quotes we don't track, or metadata missing).
    """
    eur_usd_rate = _find_eur_usd_rate(ticker_rows)
    markets: list[SpotMarket] = []
    excluded: dict[str, list[str]] = {"unknown_quote": [], "no_metadata": [], "bad_data": []}

    for key, row in ticker_rows.items():
        meta = asset_pairs.get(key) or find_meta(asset_pairs, key)
        if meta is None:
            excluded["no_metadata"].append(key)
            continue

        quote = row.quote_raw
        if quote not in config.ALLOWED_QUOTES:
            excluded["unknown_quote"].append(key)
            continue

        asset = normalize_asset(row.base_raw)

        try:
            if min(row.ask, row.bid, row.last, row.high_24h, row.low_24h) <= 0:
                excluded["bad_data"].append(key)
                continue

            volume_24h_usd = _quote_to_usd(quote, row.volume_24h * row.last, eur_usd_rate)
            spread_bps = ((row.ask - row.bid) / ((row.ask + row.bid) / 2.0)) * 10_000.0
            range_24h_pct = ((row.high_24h - row.low_24h) / row.last) * 100.0 if row.last > 0 else 0.0

            markets.append(
                SpotMarket(
                    pair_key=row.key,
                    display=row.display,
                    asset=asset,
                    quote=quote,
                    status=row.status,
                    last=row.last,
                    bid=row.bid,
                    ask=row.ask,
                    bid_size=row.bid_size,
                    ask_size=row.ask_size,
                    volume_today=row.volume_today,
                    volume_24h=row.volume_24h,
                    vwap_today=row.vwap_today,
                    vwap_24h=row.vwap_24h,
                    trades_today=row.trades_today,
                    trades_24h=row.trades_24h,
                    high_today=row.high_today,
                    low_today=row.low_today,
                    high_24h=row.high_24h,
                    low_24h=row.low_24h,
                    open_today=row.open_today,
                    timestamp=row.timestamp,
                    volume_24h_usd=volume_24h_usd,
                    spread_bps=spread_bps,
                    range_24h_pct=range_24h_pct,
                    quote_fallback=quote not in {"USD"},
                    non_crypto_like=_is_non_crypto_like(meta),
                )
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            excluded["bad_data"].append(key)
            continue

    return markets, excluded


def _choose_primary(markets: list[SpotMarket]) -> SpotMarket:
    def rank(m: SpotMarket) -> tuple[int, float]:
        return (config.QUOTE_PRIORITY.get(m.quote, 99), -m.volume_24h_usd)

    return sorted(markets, key=rank)[0]


def _price_vs_usd(market: SpotMarket, eur_usd_rate: float) -> float | None:
    if market.quote in {"USD", "USDT", "USDC"}:
        return market.last
    if market.quote == "EUR":
        return market.last * eur_usd_rate
    return None


def group_assets(
    markets: list[SpotMarket],
    futures_rows: list[FuturesTickerRow],
    eur_usd_rate: float | None = None,
) -> dict[str, AssetEntry]:
    """Group spot markets by normalized base asset, apply exclusion gates,
    match against futures perpetuals by normalized base asset.
    """
    by_asset: dict[str, list[SpotMarket]] = {}
    for m in markets:
        by_asset.setdefault(m.asset, []).append(m)

    futures_by_asset = _index_futures(futures_rows)

    entries: dict[str, AssetEntry] = {}
    for asset, asset_markets in by_asset.items():
        entry = AssetEntry(asset=asset)
        entry.markets = asset_markets
        primary = _choose_primary(asset_markets)
        entry.primary_market = primary
        entry.aggregate_volume_24h_usd = sum(m.volume_24h_usd for m in asset_markets)
        entry.futures = futures_by_asset.get(asset)

        _apply_gates(entry, primary, eur_usd_rate or config.EUR_USD_FALLBACK_RATE)
        entries[asset] = entry

    return entries


def _index_futures(rows: list[FuturesTickerRow]) -> dict[str, FuturesTickerRow]:
    best: dict[str, FuturesTickerRow] = {}
    for row in rows:
        existing = best.get(row.asset)
        if existing is None:
            best[row.asset] = row
            continue
        existing_vol = existing.volume_quote or 0.0
        new_vol = row.volume_quote or 0.0
        if new_vol > existing_vol:
            best[row.asset] = row
    return best


def _apply_gates(entry: AssetEntry, primary: SpotMarket, eur_usd_rate: float) -> None:
    asset = entry.asset

    if is_fiat(asset):
        entry.excluded_reasons.append("fiat_base")
        entry.eligible = False
        entry.tradeable = False
        return

    if is_known_stable(asset):
        entry.excluded_reasons.append("stable_asset")
        entry.eligible = False
        entry.tradeable = False
        return

    # From here the asset is at least eligible (tracked in the snapshot store).
    entry.eligible = True

    price_vs_usd = _price_vs_usd(primary, eur_usd_rate)
    if is_stable_like(price_vs_usd, primary.range_24h_pct):
        entry.excluded_reasons.append("stable_like")
        entry.flags.append("stable_like")
        entry.tradeable = False
        return

    tradeable = True

    if primary.non_crypto_like:
        entry.excluded_reasons.append("non_crypto_like")
        entry.flags.append("non_crypto_like")
        tradeable = False

    if primary.status in config.NON_TRADEABLE_STATUSES:
        entry.excluded_reasons.append(f"status:{primary.status}")
        entry.flags.append(primary.status)
        tradeable = False

    if primary.trades_24h < config.DEAD_MARKET_MAX_TRADES_24H:
        entry.excluded_reasons.append("dead_market")
        entry.flags.append("dead")
        tradeable = False

    if entry.aggregate_volume_24h_usd < config.MIN_TRADEABLE_VOLUME_24H_USD:
        entry.excluded_reasons.append("low_volume")
        entry.flags.append("low_volume")
        tradeable = False

    if primary.spread_bps > config.UNTRADEABLE_SPREAD_BPS:
        entry.excluded_reasons.append("wide_spread")
        entry.flags.append("untradeable_spread")
        tradeable = False
    elif primary.spread_bps > config.WIDE_SPREAD_FLAG_BPS:
        entry.flags.append("wide_spread")

    entry.tradeable = tradeable


def parse_futures_last_time(last_time: str | None) -> datetime | None:
    if not last_time:
        return None
    try:
        cleaned = last_time.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def futures_is_stale(last_time: str | None, now: datetime, stale_seconds: int = config.FUTURES_STALE_SECONDS) -> bool:
    parsed = parse_futures_last_time(last_time)
    if parsed is None:
        return True
    age = (now - parsed).total_seconds()
    return age > stale_seconds
