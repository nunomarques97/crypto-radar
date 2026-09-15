"""Universe normalization: legacy Kraken asset codes, stable/fiat detection.

Rule from the architecture doc (section 2): an allowed-quotes list and a
legacy-code map are acceptable venue config; a coin whitelist is not. Nothing
in this module hardcodes which coins may exist - a newly listed asset is
picked up automatically as soon as it appears in AssetPairs/Ticker.
"""

from __future__ import annotations

from . import config


def normalize_asset(code: str) -> str:
    """Map legacy Kraken asset codes (XBT, XDG, ...) to their common ticker.

    Applied identically on the spot side and the futures side so the two can
    be matched by base asset (fixes v0.7 bug: BTC/DOGE never matched Futures
    because only the futures side was normalized).
    """
    upper = str(code).strip().upper()
    return config.LEGACY_ASSET_CODES.get(upper, upper)


def split_display_pair(display: str) -> tuple[str, str] | None:
    """Split a "BASE/QUOTE" display string (wsname/altname) into (base, quote)."""
    parts = display.split("/")
    if len(parts) != 2:
        return None
    return parts[0].upper(), parts[1].upper()


def is_fiat(asset: str) -> bool:
    return normalize_asset(asset) in config.FIAT_ASSETS


def is_known_stable(asset: str) -> bool:
    return normalize_asset(asset) in config.STABLE_ASSETS


def is_stable_like(price_vs_usd: float | None, range_24h_pct: float | None) -> bool:
    """Dynamic stable-like detection: pegged price + near-zero 24h range.

    Catches new stablecoins that are not yet in the static STABLE_ASSETS
    config, without needing a coin whitelist.
    """
    if price_vs_usd is None or range_24h_pct is None:
        return False
    return (
        config.STABLE_LIKE_PRICE_LOW <= price_vs_usd <= config.STABLE_LIKE_PRICE_HIGH
        and range_24h_pct < config.STABLE_LIKE_MAX_RANGE_24H_PCT
    )
