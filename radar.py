#!/usr/bin/env python3
"""
Kraken -> Qwen crypto opportunity radar v0.7

Read-only market radar:
- Public Kraken Spot market data.
- Public Kraken Futures market data.
- Local Ollama Qwen3:14b.
- No private API keys.
- No account endpoints.
- No trading, transfers, cancellations, or leverage changes.

v0.7 goals:
- Keep the dynamic full-universe approach.
- Fix the Qwen gate so promising candidates actually reach Qwen.
- Treat Futures funding as RAW DATA unless its unit/semantics are verified.
- Add an explicit DATA QUALITY block.
- Report stale/weak Futures quotes instead of blindly rewarding them.
- Keep Spot/Futures matching by base asset.
- Avoid duplicate quote markets for the same asset.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import requests

SPOT_URL = "https://api.kraken.com/0/public"
FUTURES_URL = "https://futures.kraken.com/derivatives/api/v3"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:14b")

MIN_SPOT_24H_USD = float(os.getenv("MIN_SPOT_24H_USD", "1000000"))
MIN_FUTURES_24H_USD = float(os.getenv("MIN_FUTURES_24H_USD", "25000"))
PREFILTER_COUNT = int(os.getenv("PREFILTER_COUNT", "35"))
TOP_CANDIDATES = int(os.getenv("TOP_CANDIDATES", "12"))

# Deliberately low: Python produces a broad shortlist; Qwen is the gatekeeper.
QWEN_THRESHOLD = float(os.getenv("QWEN_THRESHOLD", "25"))

HTTP_TIMEOUT = 20

ALLOWED_QUOTES = {"USD", "USDT", "USDC", "EUR"}
QUOTE_PRIORITY = {"USD": 0, "USDT": 1, "USDC": 2, "EUR": 3}

STABLES = {
    "USDT", "USDC", "USDS", "DAI", "PYUSD", "USDE", "EURC", "TUSD",
    "USDP", "FDUSD", "RLUSD", "USDG", "EUR", "GBP", "CAD", "CHF", "JPY",
}
FIAT_BASES = {"USD", "EUR", "GBP", "CAD", "CHF", "JPY", "AUD", "NZD"}


@dataclass
class Candidate:
    spot_key: str
    display: str
    base: str
    quote: str
    spot_price: float

    spot_change_5m: float = 0.0
    spot_change_15m: float = 0.0
    spot_change_1h: float = 0.0
    spot_change_24h: float = 0.0

    spot_volume_15m_usd: float = 0.0
    spot_volume_1h_usd: float = 0.0
    spot_volume_24h_usd: float = 0.0

    spot_range_24h_pct: float = 0.0
    spot_range_position_pct: float = 50.0
    spot_spread_bps: float = 0.0

    futures_symbol: str | None = None
    futures_price: float | None = None
    futures_mark: float | None = None
    futures_change_24h_pct: float | None = None
    futures_volume_24h_usd: float | None = None
    futures_open_interest: float | None = None

    # RAW Futures funding fields. Do not convert or label as hourly/daily here.
    futures_funding_rate_raw: float | None = None
    futures_funding_prediction_raw: float | None = None

    futures_spread_bps: float | None = None
    futures_basis_pct: float | None = None

    futures_quote_fresh: bool = False
    futures_last_time: str | None = None

    score: float = 0.0
    signals: list[str] | None = None


def get_spot_json(path: str, params: dict[str, Any] | None = None) -> Any:
    r = requests.get(f"{SPOT_URL}/{path}", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    if payload.get("error"):
        raise RuntimeError(f"Spot API error: {payload['error']}")
    return payload["result"]


def get_futures_payload(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r = requests.get(f"{FUTURES_URL}/{path}", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    if payload.get("result") != "success":
        raise RuntimeError(f"Futures API error: {payload.get('error') or payload.get('result')}")
    return payload


def spot_pairs() -> dict[str, dict[str, Any]]:
    return get_spot_json("AssetPairs")


def spot_ticker() -> dict[str, dict[str, Any]]:
    return get_spot_json("Ticker")


def spot_ohlc(pair: str, interval: int = 5) -> list[list[Any]]:
    result = get_spot_json("OHLC", {"pair": pair, "interval": interval})
    if pair in result:
        return result[pair]
    keys = [k for k in result if k != "last"]
    return result[keys[0]] if keys else []


def futures_tickers() -> list[dict[str, Any]]:
    payload = get_futures_payload("tickers")
    rows = payload.get("tickers", [])
    if not isinstance(rows, list):
        raise RuntimeError("Futures payload 'tickers' is not a list")
    return [row for row in rows if isinstance(row, dict)]


def display_parts(meta: dict[str, Any], key: str) -> tuple[str, str, str]:
    display = str(meta.get("wsname") or meta.get("altname") or key)
    parts = display.split("/")
    if len(parts) != 2:
        return display, "", ""
    return display, parts[0].upper(), parts[1].upper()


def is_crypto(meta: dict[str, Any], key: str) -> tuple[bool, str, str, str]:
    display, base, quote = display_parts(meta, key)
    status = str(meta.get("status", "online")).lower()
    if quote not in ALLOWED_QUOTES:
        return False, display, base, quote
    if base in FIAT_BASES or base in STABLES:
        return False, display, base, quote
    if status not in {"online", "post_only"}:
        return False, display, base, quote
    return True, display, base, quote


def ticker_metrics(row: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
    ask = float(row["a"][0])
    bid = float(row["b"][0])
    last = float(row["c"][0])
    vol_base = float(row["v"][1])
    high = float(row["h"][1])
    low = float(row["l"][1])
    return ask, bid, last, vol_base, high, low


def quote_to_usd(quote: str, value: float) -> float:
    if quote in {"USD", "USDT", "USDC"}:
        return value
    if quote == "EUR":
        # Approximation used for ranking only, never execution.
        return value * 1.16
    return value


def ohlc_change(rows: list[list[Any]], minutes: int) -> float:
    if not rows:
        return 0.0
    latest = float(rows[-1][4])
    bars = max(1, minutes // 5)
    idx = max(0, len(rows) - 1 - bars)
    earlier = float(rows[idx][4])
    if earlier <= 0:
        return 0.0
    return (latest / earlier - 1.0) * 100.0


def ohlc_volume_usd(rows: list[list[Any]], bars: int, quote: str, price: float) -> float:
    if not rows:
        return 0.0
    base_volume = sum(float(r[6]) for r in rows[-bars:])
    return quote_to_usd(quote, base_volume * price)


def prefilter_score(change_24h: float, volume_24h: float, spread_bps: float, range_pct: float) -> float:
    score = min(abs(change_24h) * 1.2, 22.0)
    if volume_24h >= 20_000_000:
        score += 14
    elif volume_24h >= 10_000_000:
        score += 11
    elif volume_24h >= 5_000_000:
        score += 7
    else:
        score += 3

    if spread_bps <= 5:
        score += 8
    elif spread_bps <= 10:
        score += 5
    elif spread_bps <= 25:
        score += 2
    elif spread_bps > 75:
        score -= 12

    if range_pct >= 15:
        score += 7
    elif range_pct >= 8:
        score += 4
    return max(score, 0.0)


def choose_spot_market(grouped: dict[str, Candidate], candidate: Candidate) -> None:
    existing = grouped.get(candidate.base)
    if existing is None:
        grouped[candidate.base] = candidate
        return

    old_rank = (
        QUOTE_PRIORITY.get(existing.quote, 99),
        -existing.spot_volume_24h_usd,
    )
    new_rank = (
        QUOTE_PRIORITY.get(candidate.quote, 99),
        -candidate.spot_volume_24h_usd,
    )

    if new_rank < old_rank:
        grouped[candidate.base] = candidate


def build_spot_candidates() -> list[Candidate]:
    pairs = spot_pairs()
    tickers = spot_ticker()
    grouped: dict[str, Candidate] = {}

    for key, row in tickers.items():
        meta = pairs.get(key)

        if meta is None:
            upper = str(key).upper()
            for pk, pm in pairs.items():
                if upper in {
                    str(pk).upper(),
                    str(pm.get("altname", "")).upper(),
                    str(pm.get("wsname", "")).upper(),
                }:
                    meta = pm
                    break

        if meta is None:
            continue

        ok, display, base, quote = is_crypto(meta, str(key))
        if not ok:
            continue

        try:
            ask, bid, last, vol_base, high, low = ticker_metrics(row)
            if min(ask, bid, last, high, low) <= 0:
                continue

            volume_usd = quote_to_usd(quote, vol_base * last)
            if volume_usd < MIN_SPOT_24H_USD:
                continue

            open_24h = float(row["o"])
            change_24h = (last / open_24h - 1.0) * 100.0 if open_24h > 0 else 0.0
            spread = ((ask - bid) / ((ask + bid) / 2.0)) * 10_000.0
            range_pct = ((high - low) / last) * 100.0
            range_pos = ((last - low) / max(high - low, 1e-12)) * 100.0

            choose_spot_market(
                grouped,
                Candidate(
                    spot_key=str(key),
                    display=display,
                    base=base,
                    quote=quote,
                    spot_price=last,
                    spot_change_24h=change_24h,
                    spot_volume_24h_usd=volume_usd,
                    spot_range_24h_pct=range_pct,
                    spot_range_position_pct=range_pos,
                    spot_spread_bps=spread,
                    score=prefilter_score(change_24h, volume_usd, spread, range_pct),
                ),
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue

    raw = sorted(grouped.values(), key=lambda c: c.score, reverse=True)[:PREFILTER_COUNT]

    enriched: list[Candidate] = []
    for c in raw:
        try:
            rows = spot_ohlc(c.spot_key, 5)
            if not rows:
                continue

            c.spot_change_5m = ohlc_change(rows, 5)
            c.spot_change_15m = ohlc_change(rows, 15)
            c.spot_change_1h = ohlc_change(rows, 60)
            c.spot_volume_15m_usd = ohlc_volume_usd(rows, 3, c.quote, c.spot_price)
            c.spot_volume_1h_usd = ohlc_volume_usd(rows, 12, c.quote, c.spot_price)
            enriched.append(c)
        except Exception:
            continue

    return enriched


def normalize_future_base(pair: str) -> str:
    base = pair.split(":")[0].upper() if ":" in pair else pair.upper()
    return "BTC" if base == "XBT" else base


def index_perpetuals(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}

    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol.startswith("PF_"):
            continue

        base = normalize_future_base(str(row.get("pair") or ""))
        if not base:
            continue

        try:
            volume_quote = float(row.get("volumeQuote") or 0)
        except (TypeError, ValueError):
            volume_quote = 0.0

        if volume_quote < MIN_FUTURES_24H_USD:
            continue

        existing = best.get(base)
        if existing is None:
            best[base] = row
            continue

        try:
            existing_volume = float(existing.get("volumeQuote") or 0)
        except (TypeError, ValueError):
            existing_volume = 0.0

        if volume_quote > existing_volume:
            best[base] = row

    return best


def enrich_futures(candidates: list[Candidate], rows: list[dict[str, Any]]) -> None:
    by_base = index_perpetuals(rows)

    for c in candidates:
        row = by_base.get(c.base.upper())
        if not row:
            continue

        try:
            c.futures_symbol = str(row.get("symbol") or "")
            c.futures_price = float(row["last"]) if row.get("last") is not None else None
            c.futures_mark = float(row["markPrice"]) if row.get("markPrice") is not None else None
            c.futures_volume_24h_usd = float(row.get("volumeQuote") or 0)
            c.futures_open_interest = float(row["openInterest"]) if row.get("openInterest") is not None else None

            # Keep funding explicitly RAW. No hourly/daily conversion here.
            c.futures_funding_rate_raw = (
                float(row["fundingRate"]) if row.get("fundingRate") is not None else None
            )
            c.futures_funding_prediction_raw = (
                float(row["fundingRatePrediction"])
                if row.get("fundingRatePrediction") is not None else None
            )

            c.futures_change_24h_pct = (
                ((float(row["last"]) / float(row["open24h"])) - 1.0) * 100.0
                if row.get("open24h") not in (None, 0) else None
            )

            bid = float(row["bid"])
            ask = float(row["ask"])
            c.futures_spread_bps = ((ask - bid) / ((ask + bid) / 2.0)) * 10_000.0

            if c.spot_price > 0 and c.futures_price is not None:
                c.futures_basis_pct = (c.futures_price / c.spot_price - 1.0) * 100.0

            c.futures_last_time = str(row.get("lastTime") or "")
            # This is a lightweight quality flag, not a perfect freshness model.
            c.futures_quote_fresh = bool(c.futures_last_time)

        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue


def score_candidate(c: Candidate) -> None:
    score = 0.0
    signals: list[str] = []

    # Current activity dominates historical movement.
    score += min(abs(c.spot_change_5m) * 7.0, 20.0)
    score += min(abs(c.spot_change_15m) * 4.0, 20.0)
    score += min(abs(c.spot_change_1h) * 2.0, 15.0)

    if abs(c.spot_change_5m) >= 0.75:
        signals.append(f"5m {c.spot_change_5m:+.2f}%")
    if abs(c.spot_change_15m) >= 1.25:
        signals.append(f"15m {c.spot_change_15m:+.2f}%")
    if abs(c.spot_change_1h) >= 3.0:
        signals.append(f"1h {c.spot_change_1h:+.2f}%")

    acceleration = abs(c.spot_change_15m) - abs(c.spot_change_1h / 4.0)
    if acceleration >= 0.5:
        score += min(acceleration * 6.0, 12.0)
        signals.append("momentum accelerating")

    if c.spot_volume_24h_usd > 0:
        h_baseline = max(c.spot_volume_24h_usd / 24.0, 1.0)
        h_ratio = c.spot_volume_1h_usd / h_baseline
        if h_ratio >= 3:
            score += 15
            signals.append(f"1h volume {h_ratio:.1f}x baseline")
        elif h_ratio >= 2:
            score += 10
            signals.append(f"1h volume {h_ratio:.1f}x baseline")
        elif h_ratio >= 1.5:
            score += 5

        m15_baseline = max(c.spot_volume_1h_usd / 4.0, 1.0)
        m15_ratio = c.spot_volume_15m_usd / m15_baseline
        if m15_ratio >= 2:
            score += 12
            signals.append("15m volume accelerating")
        elif m15_ratio >= 1.5:
            score += 6

    # Historical context only.
    if abs(c.spot_change_24h) >= 15:
        score += 3
        signals.append(f"24h context {c.spot_change_24h:+.1f}%")
    elif abs(c.spot_change_24h) >= 7:
        score += 1

    if c.spot_change_15m > 0 and c.spot_range_position_pct >= 92:
        score += 6
        signals.append("near 24h high")
    elif c.spot_change_15m < 0 and c.spot_range_position_pct <= 8:
        score += 6
        signals.append("near 24h low")

    # Spot execution quality.
    if c.spot_spread_bps <= 8:
        score += 8
    elif c.spot_spread_bps <= 20:
        score += 4
    elif c.spot_spread_bps > 60:
        score -= 15
        signals.append(f"wide Spot spread {c.spot_spread_bps:.1f}bps")

    # Futures context is additive, never mandatory.
    if c.futures_symbol:
        signals.append(f"Futures {c.futures_symbol}")

        if c.futures_spread_bps is not None:
            if c.futures_spread_bps <= 10:
                score += 4
            elif c.futures_spread_bps <= 25:
                score += 2
            elif c.futures_spread_bps > 60:
                score -= 8
                signals.append(f"wide Futures spread {c.futures_spread_bps:.1f}bps")

        if c.futures_open_interest is not None and c.futures_open_interest > 0:
            score += 2

        # Funding is a RAW contextual signal only.
        if c.futures_funding_rate_raw is not None:
            signals.append(f"funding(raw) {c.futures_funding_rate_raw:.8g}")

        if c.futures_basis_pct is not None and abs(c.futures_basis_pct) >= 0.5:
            signals.append(f"basis {c.futures_basis_pct:+.2f}%")

        if c.futures_quote_fresh:
            score += 1

    # Old pump with dead current momentum should not dominate.
    if abs(c.spot_change_24h) >= 20 and abs(c.spot_change_15m) < 0.5:
        score -= 10
        signals.append("extended 24h move / weak current momentum")

    c.score = max(0.0, min(score, 100.0))
    c.signals = signals


def build_qwen_payload(candidates: list[Candidate]) -> list[dict[str, Any]]:
    selected = sorted(candidates, key=lambda c: c.score, reverse=True)
    selected = [c for c in selected if c.score >= QWEN_THRESHOLD][:TOP_CANDIDATES]

    payload: list[dict[str, Any]] = []
    for c in selected:
        item: dict[str, Any] = {
            "symbol": c.display,
            "spot": {
                "price": c.spot_price,
                "5m_pct": round(c.spot_change_5m, 4),
                "15m_pct": round(c.spot_change_15m, 4),
                "1h_pct": round(c.spot_change_1h, 4),
                "24h_pct": round(c.spot_change_24h, 4),
                "volume_15m_usd": round(c.spot_volume_15m_usd, 0),
                "volume_1h_usd": round(c.spot_volume_1h_usd, 0),
                "volume_24h_usd": round(c.spot_volume_24h_usd, 0),
                "spread_bps": round(c.spot_spread_bps, 3),
                "range_24h_pct": round(c.spot_range_24h_pct, 3),
                "range_position_pct": round(c.spot_range_position_pct, 2),
            },
            "local_radar_score": round(c.score, 1),
            "signals": c.signals or [],
            "data_quality": {
                "spot_ok": True,
                "futures_ok": bool(c.futures_symbol),
                "funding_semantics": "RAW_UNVERIFIED",
            },
        }

        if c.futures_symbol:
            item["futures"] = {
                "symbol": c.futures_symbol,
                "price": c.futures_price,
                "mark": c.futures_mark,
                "change24h_pct": c.futures_change_24h_pct,
                "volume_24h_usd": c.futures_volume_24h_usd,
                "open_interest": c.futures_open_interest,
                "funding_rate_raw": c.futures_funding_rate_raw,
                "funding_prediction_raw": c.futures_funding_prediction_raw,
                "spread_bps": c.futures_spread_bps,
                "basis_pct": c.futures_basis_pct,
                "last_time": c.futures_last_time,
            }

        payload.append(item)

    return payload


def qwen_review(candidates: list[Candidate]) -> tuple[dict[str, Any], int]:
    items = build_qwen_payload(candidates)

    if not items:
        return (
            {
                "alerts": [],
                "meta": {
                    "status": "NO_ALERTS",
                    "reason": "No candidate passed the local radar threshold."
                },
            },
            0,
        )

    system = (
        "You are a crypto opportunity RADAR, not an execution agent. "
        "Use only supplied data. Never invent news, catalysts, OI, funding semantics, "
        "orderbook facts, or liquidity facts. "
        "Funding fields are RAW_UNVERIFIED unless explicitly labeled otherwise. "
        "A high-beta or memecoin asset can be valid. "
        "Focus on current 5m/15m/1h activity and volume; 24h is context. "
        "Return JSON only."
    )

    prompt = (
        'Return ONLY valid JSON with this exact shape: '
        '{"alerts":[{"symbol":"...","score":0,"direction":"LONG|SHORT|NONE",'
        '"market":"SPOT|FUTURES|BOTH","reason":"..."}]}'
        "\n"
        "Include only candidates that genuinely deserve a deeper Fable 5.1 analysis. "
        "A large 24h move alone is insufficient. "
        "Do not use unverified funding semantics to create a directional trade. "
        "Prefer recent momentum, acceleration, recent volume, executable spreads, "
        "and coherent Futures context where supplied.\n\n"
        + json.dumps(items, separators=(",", ":"), ensure_ascii=False)
    )

    body = {
        "model": OLLAMA_MODEL,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": "json",
        "keep_alive": "5m",
    }

    response = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json=body,
        timeout=120,
    )
    response.raise_for_status()

    result = response.json()
    parsed = json.loads(str(result.get("response", "")).strip())

    if not isinstance(parsed, dict) or not isinstance(parsed.get("alerts"), list):
        raise RuntimeError(f"Unexpected Qwen JSON: {result.get('response')}")

    return parsed, len(items)


def main() -> int:
    started = time.perf_counter()

    print("=== KRAKEN QWEN RADAR v0.7 ===")
    print(f"Model: {OLLAMA_MODEL}")
    print("Safety: PUBLIC market data only; no API keys; no trading capability.")
    print(f"Spot liquidity threshold: ${MIN_SPOT_24H_USD:,.0f}")
    print(f"Futures liquidity threshold: ${MIN_FUTURES_24H_USD:,.0f}")
    print(f"Prefilter: {PREFILTER_COUNT} | Qwen candidates max: {TOP_CANDIDATES} | Qwen threshold: {QWEN_THRESHOLD}")
    print()

    try:
        candidates = build_spot_candidates()
        print(f"Recent-structure Spot candidates: {len(candidates)}")

        futures = []
        try:
            futures = futures_tickers()
            print(
                "Futures perpetual tickers received: "
                + str(sum(1 for r in futures if str(r.get("symbol", "")).upper().startswith("PF_")))
            )
        except Exception as exc:
            print(f"Futures public data unavailable: {exc}")

        enrich_futures(candidates, futures)

        for c in candidates:
            score_candidate(c)

        candidates.sort(key=lambda c: c.score, reverse=True)

        print("\nTop candidates:")
        for i, c in enumerate(candidates[:TOP_CANDIDATES], 1):
            fut = c.futures_symbol or "-"
            oi = f"{c.futures_open_interest:.0f}" if c.futures_open_interest is not None else "-"
            fund = f"{c.futures_funding_rate_raw:.8g}" if c.futures_funding_rate_raw is not None else "-"
            print(
                f"{i:>2}. {c.display:<16} "
                f"score={c.score:>5.1f} "
                f"5m={c.spot_change_5m:+6.2f}% "
                f"15m={c.spot_change_15m:+6.2f}% "
                f"1h={c.spot_change_1h:+7.2f}% "
                f"24h={c.spot_change_24h:+8.2f}% "
                f"spr={c.spot_spread_bps:>5.1f}bps "
                f"fut={fut:<14} "
                f"OI={oi:<12} "
                f"fund(raw)={fund}"
            )

        qwen, qwen_count = qwen_review(candidates)

        print(f"\nCandidates sent to Qwen: {qwen_count}")
        print("\n=== DATA QUALITY ===")
        print("Spot public data: OK")
        print(f"Futures public data: {'OK' if futures else 'UNAVAILABLE'}")
        print("Funding semantics: RAW_UNVERIFIED (not converted or interpreted as hourly/daily)")
        print("Trading/account credentials used: NO")

        print("\n=== QWEN RESULT ===")
        print(json.dumps(qwen, indent=2, ensure_ascii=False))

        elapsed = time.perf_counter() - started
        print(f"\nElapsed: {elapsed:.2f}s")
        return 0

    except requests.RequestException as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"Qwen JSON parse error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _parse_mode(argv: list[str]) -> str:
    """v0.8 adds `--mode heartbeat|shadow` on top of the plain v0.7 CLI.
    No flag (or `--mode v07`) runs v0.7's own main() below, unchanged.
    """
    if "--mode" in argv:
        idx = argv.index("--mode")
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return "v07"


if __name__ == "__main__":
    _mode = _parse_mode(sys.argv[1:])
    if _mode == "v07":
        raise SystemExit(main())
    else:
        from radar_v08.cli import run_mode

        raise SystemExit(run_mode(_mode))
