"""Configuration for radar v0.8 - Phase 1.

Every threshold here is a starting point, not a calibrated value. Values
marked UNCALIBRATED should be revisited once `runs.jsonl` / `forward_returns`
have enough history to justify a number (see architecture doc section 5, 15-B).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from types import MappingProxyType

# --------------------------------------------------------------------------
# Endpoints (public only - see security.py for the enforcement of this)
# --------------------------------------------------------------------------
SPOT_URL = "https://api.kraken.com/0/public"
FUTURES_URL = "https://futures.kraken.com/derivatives/api/v3"

# T023a: exact public-HTTP allowlist enforced by security.assert_allowed_request
# for every GuardedSession call. Scheme https only, default port only, no
# userinfo, GET only, and only the exact paths the radar calls today
# (kraken_spot.py / kraken_futures.py). Hardcoded on purpose - never read from
# the environment. Ollama (OLLAMA_ALLOWED_HOSTS) and ntfy are NOT here: they
# keep their own guards and are never reachable through GuardedSession.
HTTP_ALLOWED_SCHEME = "https"
HTTP_ALLOWED_METHODS = frozenset({"GET"})
HTTP_PUBLIC_ALLOWLIST: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "api.kraken.com": frozenset(
            {
                "/0/public/AssetPairs",
                "/0/public/Ticker",
                "/0/public/OHLC",
                "/0/public/Depth",
                "/0/public/Trades",
            }
        ),
        "futures.kraken.com": frozenset(
            {
                "/derivatives/api/v3/tickers",
                "/derivatives/api/v3/orderbook",
            }
        ),
    }
)

HTTP_TIMEOUT = float(os.getenv("RADAR_HTTP_TIMEOUT", "20"))
HTTP_MAX_RETRIES = int(os.getenv("RADAR_HTTP_MAX_RETRIES", "3"))
HTTP_BACKOFF_BASE = float(os.getenv("RADAR_HTTP_BACKOFF_BASE", "0.5"))

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
STATE_DIR = os.getenv("RADAR_STATE_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SQLITE_PATH = os.getenv("RADAR_SQLITE_PATH", os.path.join(STATE_DIR, "radar_state.sqlite"))
ASSET_PAIRS_CACHE_PATH = os.getenv(
    "RADAR_ASSET_PAIRS_CACHE_PATH", os.path.join(STATE_DIR, "radar_v08_asset_pairs_cache.json")
)
ASSET_PAIRS_CACHE_TTL_SECONDS = int(os.getenv("RADAR_ASSET_PAIRS_CACHE_TTL_SECONDS", str(24 * 3600)))

# Snapshot retention (days). Configurable per architecture doc section "SNAPSHOT STORE".
SNAPSHOT_RETENTION_DAYS = int(os.getenv("RADAR_SNAPSHOT_RETENTION_DAYS", "7"))

RUN_LOG_PATH = os.getenv("RADAR_RUN_LOG_PATH", os.path.join(STATE_DIR, "runs.jsonl"))
TEXT_LOG_PATH = os.getenv("RADAR_TEXT_LOG_PATH", os.path.join(STATE_DIR, "radar.log"))
OUTPUT_V08_PATH = os.getenv("RADAR_OUTPUT_V08_PATH", os.path.join(STATE_DIR, "radar_v08_output.json"))
OUTPUT_V07_PATH = os.getenv("RADAR_OUTPUT_V07_PATH", os.path.join(STATE_DIR, "radar_v07_output.json"))
SHADOW_COMPARISON_PATH = os.getenv(
    "RADAR_SHADOW_COMPARISON_PATH", os.path.join(STATE_DIR, "shadow_comparison.json")
)

# --------------------------------------------------------------------------
# Universe / normalization (UNCALIBRATED where noted)
# --------------------------------------------------------------------------
# Legacy Kraken asset codes. Config-of-venue, not a coin whitelist: applied to
# BOTH spot and futures sides before matching (fixes v0.7 bug #1).
LEGACY_ASSET_CODES = {
    "XBT": "BTC",
    "XDG": "DOGE",
}

ALLOWED_QUOTES = {"USD", "USDT", "USDC", "EUR"}
QUOTE_PRIORITY = {"USD": 0, "USDT": 1, "USDC": 2, "EUR": 3}

# Quote-asset config of venue (acceptable per architecture doc section 2).
STABLE_ASSETS = {
    "USDT", "USDC", "USDS", "DAI", "PYUSD", "USDE", "EURC", "TUSD",
    "USDP", "FDUSD", "RLUSD", "USDG",
}
FIAT_ASSETS = {"USD", "EUR", "GBP", "CAD", "CHF", "JPY", "AUD", "NZD"}

# Dynamic stable-like detection (catches new stables without a code change).
STABLE_LIKE_PRICE_LOW = float(os.getenv("RADAR_STABLE_LIKE_PRICE_LOW", "0.97"))
STABLE_LIKE_PRICE_HIGH = float(os.getenv("RADAR_STABLE_LIKE_PRICE_HIGH", "1.03"))
STABLE_LIKE_MAX_RANGE_24H_PCT = float(os.getenv("RADAR_STABLE_LIKE_MAX_RANGE_24H_PCT", "1.0"))

# EUR -> USD conversion fallback if no live EUR/USD ticker is present in the
# same payload. Ranking/aggregation only, never execution (mirrors v0.7).
EUR_USD_FALLBACK_RATE = float(os.getenv("RADAR_EUR_USD_FALLBACK_RATE", "1.08"))

# Tradeable-universe gates (liquidity/activity/spread). Assets excluded here
# are still written to the snapshot store (per architecture doc section 2).
MIN_TRADEABLE_VOLUME_24H_USD = float(os.getenv("RADAR_MIN_TRADEABLE_VOLUME_24H_USD", "1_000_000"))
DEAD_MARKET_MAX_TRADES_24H = int(os.getenv("RADAR_DEAD_MARKET_MAX_TRADES_24H", "200"))
UNTRADEABLE_SPREAD_BPS = float(os.getenv("RADAR_UNTRADEABLE_SPREAD_BPS", "150"))
WIDE_SPREAD_FLAG_BPS = float(os.getenv("RADAR_WIDE_SPREAD_FLAG_BPS", "50"))

NON_TRADEABLE_STATUSES = {"post_only", "cancel_only", "limit_only", "reduce_only", "suspended"}

# Futures freshness (architecture doc section 9).
FUTURES_STALE_SECONDS = int(os.getenv("RADAR_FUTURES_STALE_SECONDS", "300"))

# --------------------------------------------------------------------------
# L1 anomaly detection (UNCALIBRATED)
# --------------------------------------------------------------------------
# Minimum history required before an asset's z-scores are trusted; below this
# the asset stays warmup=true and no z-score is fabricated.
ANOMALY_MIN_SAMPLES = int(os.getenv("RADAR_ANOMALY_MIN_SAMPLES", "10"))
ANOMALY_MIN_HISTORY_MINUTES = int(os.getenv("RADAR_ANOMALY_MIN_HISTORY_MINUTES", "15"))
ANOMALY_HISTORY_LOOKBACK_HOURS = int(os.getenv("RADAR_ANOMALY_HISTORY_LOOKBACK_HOURS", "48"))

# Snapshot-lookup tolerance windows (minutes) used to find "closest to Nm" bar.
LOOKUP_HORIZONS_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
LOOKUP_TOLERANCE_FRACTION = float(os.getenv("RADAR_LOOKUP_TOLERANCE_FRACTION", "0.5"))

# MAD -> sigma-equivalent scale factor (standard robust-stats constant).
MAD_SCALE = 1.4826
MAD_FLOOR = float(os.getenv("RADAR_MAD_FLOOR", "1e-9"))

# Weights for combining z-scores into anomaly_score (0-100). Direction-agnostic:
# only abs(z) is used. UNCALIBRATED.
ANOMALY_WEIGHTS = {
    "price_z": 0.30,
    "volume_z": 0.25,
    "trades_z": 0.15,
    "oi_z": 0.10,
    "relative_btc_z": 0.20,
}
# abs(z) at/above this maps to a 100 sub-score (clipped). UNCALIBRATED.
ANOMALY_Z_CLIP = float(os.getenv("RADAR_ANOMALY_Z_CLIP", "4.0"))

ANOMALY_SHORTLIST_SIZE = int(os.getenv("RADAR_ANOMALY_SHORTLIST_SIZE", "40"))

# --------------------------------------------------------------------------
# Heartbeat / run behaviour
# --------------------------------------------------------------------------
BTC_ASSET = "BTC"
RUN_ID_PREFIX = os.getenv("RADAR_RUN_ID_PREFIX", "")

# --------------------------------------------------------------------------
# L2: OHLC incremental, ATR, structure, setups, opportunity (Phase 2, UNCALIBRATED)
# --------------------------------------------------------------------------
# Only the L1 shortlist gets OHLC requests - never the whole universe.
OHLC_INTERVAL_MINUTES = int(os.getenv("RADAR_OHLC_INTERVAL_MINUTES", "5"))
# Bars kept per pair for feature computation (400 x 5m ~= 33h - enough for
# 24h structure + ATR14 + a volatility-percentile baseline with margin).
OHLC_WINDOW_BARS = int(os.getenv("RADAR_OHLC_WINDOW_BARS", "400"))
# Moderate concurrency for the shortlist's OHLC fetch (doc section 11/15-A):
# a handful of parallel GETs, never hundreds of sequential ones.
OHLC_FETCH_WORKERS = int(os.getenv("RADAR_OHLC_FETCH_WORKERS", "4"))

ATR_PERIOD = int(os.getenv("RADAR_ATR_PERIOD", "14"))
# Bars used for the "1h ATR" series: 12 x 5m bars == 1h; computed by
# resampling the 5m bar series rather than a second OHLC request.
ATR_1H_RESAMPLE_BARS = int(os.getenv("RADAR_ATR_1H_RESAMPLE_BARS", "12"))

# Structure lookbacks, in 5m bars (interval-agnostic count, not minutes).
STRUCTURE_4H_BARS = int(os.getenv("RADAR_STRUCTURE_4H_BARS", "48"))     # 4h / 5m
STRUCTURE_24H_BARS = int(os.getenv("RADAR_STRUCTURE_24H_BARS", "288"))  # 24h / 5m
STRUCTURE_TREND_BARS = int(os.getenv("RADAR_STRUCTURE_TREND_BARS", "12"))  # higher-high/low window

# Volatility percentile baseline: how many ATR samples (one per bar close) to
# rank the current ATR against, for compression/expansion detection.
VOLATILITY_PERCENTILE_LOOKBACK_BARS = int(os.getenv("RADAR_VOL_PCTILE_LOOKBACK_BARS", "288"))
SQUEEZE_PERCENTILE_THRESHOLD = float(os.getenv("RADAR_SQUEEZE_PERCENTILE_THRESHOLD", "25.0"))
RANGE_EXPANSION_ATR_MULT = float(os.getenv("RADAR_RANGE_EXPANSION_ATR_MULT", "1.5"))

# Minimum bars before L2 features are trusted (below this: l2_warmup=true,
# degrade gracefully rather than fabricate ATR/structure).
L2_MIN_BARS = int(os.getenv("RADAR_L2_MIN_BARS", str(ATR_PERIOD + 1)))

# --- Setup classification thresholds (all in ATR units unless noted) -------
SETUP_THRESHOLDS = {
    # Momentum must clear this many ATRs on the relevant horizon to count.
    "momentum_min_atr_15m": float(os.getenv("RADAR_SETUP_MOMENTUM_MIN_ATR_15M", "0.5")),
    "momentum_min_atr_1h": float(os.getenv("RADAR_SETUP_MOMENTUM_MIN_ATR_1H", "1.0")),
    # Breakout: close must clear the 24h/4h level by at least this many ATRs.
    "breakout_min_atr": float(os.getenv("RADAR_SETUP_BREAKOUT_MIN_ATR", "0.25")),
    # Volume must be at least this multiple of its 15m baseline to "confirm".
    "volume_confirm_intensity": float(os.getenv("RADAR_SETUP_VOLUME_CONFIRM_INTENSITY", "1.5")),
    # Exhaustion: |1h return| beyond this many ATRs, with volume fading.
    "exhaustion_min_atr_1h": float(os.getenv("RADAR_SETUP_EXHAUSTION_MIN_ATR_1H", "3.0")),
    "exhaustion_volume_fade_ratio": float(os.getenv("RADAR_SETUP_EXHAUSTION_VOLUME_FADE_RATIO", "0.7")),
    # Reversal: divergence between the 15m move and the 1h/4h trend, at a
    # range extreme, with a rejection wick.
    "reversal_min_atr_15m": float(os.getenv("RADAR_SETUP_REVERSAL_MIN_ATR_15M", "0.5")),
    # Wick-to-range ratio beyond which a bar counts as a rejection.
    "rejection_wick_ratio": float(os.getenv("RADAR_SETUP_REJECTION_WICK_RATIO", "0.5")),
}

# --- Opportunity score weights (sum to 1.0); all UNCALIBRATED --------------
OPPORTUNITY_WEIGHTS = {
    "momentum": 0.16,
    "momentum_coherence": 0.08,
    "acceleration": 0.08,
    "volume_confirmed": 0.14,
    "breakout_distance": 0.10,
    "freshness": 0.10,
    "relative_strength": 0.08,
    "derivatives_coherence": 0.08,
    "squeeze_release": 0.08,
    "cost_efficiency": 0.10,
}
OPPORTUNITY_EXHAUSTION_PENALTY = float(os.getenv("RADAR_OPPORTUNITY_EXHAUSTION_PENALTY", "25.0"))
# Feature normalization targets (value at which a sub-score saturates to 1.0).
OPPORTUNITY_MOMENTUM_ATR_TARGET = float(os.getenv("RADAR_OPPORTUNITY_MOMENTUM_ATR_TARGET", "2.0"))
OPPORTUNITY_BREAKOUT_ATR_TARGET = float(os.getenv("RADAR_OPPORTUNITY_BREAKOUT_ATR_TARGET", "1.5"))
OPPORTUNITY_COST_EFFICIENCY_TARGET = float(os.getenv("RADAR_OPPORTUNITY_COST_EFFICIENCY_TARGET", "3.0"))

# --- Cost preview (spread + fees) - NOT a full L3 tradeability score --------
# Deliberately named UNCALIBRATED_FEES: these are placeholder venue fee tiers.
# Override via env once the real account fee tier is known.
UNCALIBRATED_FEES = {
    "spot_taker_bps": float(os.getenv("RADAR_FEE_SPOT_TAKER_BPS", "26.0")),
    "spot_maker_bps": float(os.getenv("RADAR_FEE_SPOT_MAKER_BPS", "16.0")),
    "futures_taker_bps": float(os.getenv("RADAR_FEE_FUTURES_TAKER_BPS", "5.0")),
    "futures_maker_bps": float(os.getenv("RADAR_FEE_FUTURES_MAKER_BPS", "2.0")),
}

# --------------------------------------------------------------------------
# Forward-return labeling (calibration data only - never changes weights here)
# --------------------------------------------------------------------------
FORWARD_RETURN_HORIZONS_MINUTES = [15, 60, 240]
FORWARD_RETURN_LOOKUP_TOLERANCE_SECONDS = float(os.getenv("RADAR_FWD_RETURN_TOLERANCE_SECONDS", "120"))
FORWARD_RETURN_LABEL_BATCH_LIMIT = int(os.getenv("RADAR_FWD_RETURN_LABEL_BATCH_LIMIT", "500"))

# --------------------------------------------------------------------------
# Phase 3: L3 order book + trades (finalists only, UNCALIBRATED)
# --------------------------------------------------------------------------
L3_MAX_FINALISTS = int(os.getenv("RADAR_L3_MAX_FINALISTS", "8"))
L3_MIN_OPPORTUNITY_TO_CONSIDER = float(os.getenv("RADAR_L3_MIN_OPPORTUNITY", "40.0"))
L3_FETCH_WORKERS = int(os.getenv("RADAR_L3_FETCH_WORKERS", "4"))

DEPTH_BOOK_COUNT = int(os.getenv("RADAR_DEPTH_BOOK_COUNT", "25"))
# Reference order size for slippage/depth-at-size estimates - configurable per
# Sponsor's typical order (architecture doc section 3, "tamanho típico de
# ordem do Sponsor").
REFERENCE_ORDER_SIZE_USD = float(os.getenv("RADAR_REFERENCE_ORDER_SIZE_USD", "250.0"))
DEPTH_BAND_PCT_TIGHT = float(os.getenv("RADAR_DEPTH_BAND_PCT_TIGHT", "0.5"))  # +/-0.5%
DEPTH_BAND_PCT_WIDE = float(os.getenv("RADAR_DEPTH_BAND_PCT_WIDE", "1.0"))    # +/-1%

TRADES_FETCH_COUNT = int(os.getenv("RADAR_TRADES_FETCH_COUNT", "1000"))

# --------------------------------------------------------------------------
# Tradeability (L3, finalists) - a GATE, not an additive bonus term.
# --------------------------------------------------------------------------
TRADEABILITY_WEIGHTS = {
    "spread": 0.20,
    "depth": 0.20,
    "slippage": 0.15,
    "book_imbalance_penalty": 0.05,
    "activity": 0.15,
    "futures_available": 0.10,
    "futures_quality": 0.10,
    "freshness": 0.05,
}
TRADEABILITY_SLIPPAGE_TARGET_BPS = float(os.getenv("RADAR_TRADEABILITY_SLIPPAGE_TARGET_BPS", "20.0"))
TRADEABILITY_DEPTH_TARGET_USD = float(os.getenv("RADAR_TRADEABILITY_DEPTH_TARGET_USD", "5000.0"))
TRADEABILITY_SPREAD_TARGET_BPS = float(os.getenv("RADAR_TRADEABILITY_SPREAD_TARGET_BPS", "30.0"))
TRADEABILITY_ACTIVITY_TARGET_TRADES_PER_HOUR = float(
    os.getenv("RADAR_TRADEABILITY_ACTIVITY_TARGET", "50.0")
)

# Gate thresholds (0-100 score -> state).
TRADEABILITY_UNTRADEABLE_MAX = float(os.getenv("RADAR_TRADEABILITY_UNTRADEABLE_MAX", "30.0"))
TRADEABILITY_CONSTRAINED_MAX = float(os.getenv("RADAR_TRADEABILITY_CONSTRAINED_MAX", "60.0"))
# Hard vetoes regardless of score.
TRADEABILITY_HARD_MAX_SPREAD_BPS = float(os.getenv("RADAR_TRADEABILITY_HARD_MAX_SPREAD_BPS", "150.0"))

# --------------------------------------------------------------------------
# Qwen 3:14b (Ollama, local, structured output) - review/veto only, never a
# score calculator (architecture doc section 6).
# --------------------------------------------------------------------------
OLLAMA_URL = os.getenv("RADAR_OLLAMA_URL", "http://localhost:11434")
OLLAMA_ALLOWED_HOSTS = {"http://localhost:11434", "http://127.0.0.1:11434"}
QWEN_MODEL = os.getenv("RADAR_QWEN_MODEL", "qwen3:14b")
QWEN_TIMEOUT_SECONDS = float(os.getenv("RADAR_QWEN_TIMEOUT_SECONDS", "30.0"))
QWEN_MAX_RETRIES_ON_INVALID = int(os.getenv("RADAR_QWEN_MAX_RETRIES", "1"))
QWEN_THINK = False  # architecture doc section 6: think=false
QWEN_TEMPERATURE = float(os.getenv("RADAR_QWEN_TEMPERATURE", "0.0"))

# Deterministic pre-gate: which L3 finalists are even worth a Qwen call.
QWEN_PREGATE_MIN_OPPORTUNITY = float(os.getenv("RADAR_QWEN_PREGATE_MIN_OPPORTUNITY", "50.0"))

# --------------------------------------------------------------------------
# Demand router: IGNORE | SONNET | FABLE (never OPUS). UNCALIBRATED.
# --------------------------------------------------------------------------
ROUTER_SONNET_MIN_OPPORTUNITY = float(os.getenv("RADAR_ROUTER_SONNET_MIN_OPPORTUNITY", "50.0"))
ROUTER_FABLE_MIN_OPPORTUNITY = float(os.getenv("RADAR_ROUTER_FABLE_MIN_OPPORTUNITY", "70.0"))
ROUTER_FABLE_MIN_CONFIRMATIONS = int(os.getenv("RADAR_ROUTER_FABLE_MIN_CONFIRMATIONS", "2"))
# When Qwen is UNAVAILABLE, Fable needs one extra confirmation (architecture
# doc section 6/16-I: "exigir uma confirmação extra").
ROUTER_FABLE_MIN_CONFIRMATIONS_NO_QWEN = ROUTER_FABLE_MIN_CONFIRMATIONS + 1
ROUTER_TAKER_IMBALANCE_CONFIRM_RATIO = float(os.getenv("RADAR_ROUTER_TAKER_IMBALANCE_RATIO", "0.65"))

# --------------------------------------------------------------------------
# Cooldown (per asset, per model) - SQLite backed. UNCALIBRATED.
# --------------------------------------------------------------------------
COOLDOWN_HOURS = float(os.getenv("RADAR_COOLDOWN_HOURS", "4.0"))
COOLDOWN_OPPORTUNITY_JUMP = float(os.getenv("RADAR_COOLDOWN_OPPORTUNITY_JUMP", "15.0"))

# --------------------------------------------------------------------------
# Model budgets (per hour / per day). UNCALIBRATED - initial guesses only.
# --------------------------------------------------------------------------
MODEL_BUDGETS = {
    "SONNET": {
        "hourly": int(os.getenv("RADAR_SONNET_HOURLY_BUDGET", "8")),
        "daily": int(os.getenv("RADAR_SONNET_DAILY_BUDGET", "30")),
    },
    "FABLE": {
        "hourly": int(os.getenv("RADAR_FABLE_HOURLY_BUDGET", "2")),
        "daily": int(os.getenv("RADAR_FABLE_DAILY_BUDGET", "6")),
    },
}

# --------------------------------------------------------------------------
# Event queue
# --------------------------------------------------------------------------
EVENTS_LOG_PATH = os.getenv("RADAR_EVENTS_LOG_PATH", os.path.join(STATE_DIR, "events.jsonl"))
EVENT_STATUSES = ("PENDING", "PROCESSING", "PROCESSED", "DEFERRED", "FAILED")

# --------------------------------------------------------------------------
# Phase 4: Claude Bridge - analysis + delivery + notification only.
# Never trading: no order placement, cancellation, leverage, or transfers.
# Model choice is the Demand Router's alone: IGNORE | SONNET | FABLE, never OPUS.
# --------------------------------------------------------------------------
# T010 containment policy.  This is deliberately a source-level constant, not
# an environment setting: credentials, a present SDK, or an injected client
# must never grant a legacy cloud provider permission at runtime.
CLAUDE_BRIDGE_DISPATCH_ENABLED = False
CLAUDE_BRIDGE_DISABLED_REASON = "legacy Claude Bridge disabled; runtime analysis is local-only"

ANTHROPIC_SONNET_MODEL = os.getenv("RADAR_ANTHROPIC_SONNET_MODEL", "claude-sonnet-5")
ANTHROPIC_FABLE_MODEL = os.getenv("RADAR_ANTHROPIC_FABLE_MODEL", "claude-fable-5-1")
CLAUDE_BRIDGE_MODEL_IDS = {"SONNET": ANTHROPIC_SONNET_MODEL, "FABLE": ANTHROPIC_FABLE_MODEL}
MODEL_VERSION_TAG = {"SONNET": "bridge_prompt_v1", "FABLE": "bridge_prompt_v1"}

CLAUDE_BRIDGE_TIMEOUT_SECONDS = float(os.getenv("RADAR_CLAUDE_BRIDGE_TIMEOUT_SECONDS", "120.0"))
CLAUDE_BRIDGE_SONNET_MAX_TOKENS = int(os.getenv("RADAR_CLAUDE_BRIDGE_SONNET_MAX_TOKENS", "4096"))
CLAUDE_BRIDGE_FABLE_MAX_TOKENS = int(os.getenv("RADAR_CLAUDE_BRIDGE_FABLE_MAX_TOKENS", "8192"))

# How many actionable (PENDING/due-DEFERRED) events one bridge cycle drains -
# a cap, not a target, so one noisy cycle can't burn the whole hourly budget.
CLAUDE_BRIDGE_MAX_EVENTS_PER_CYCLE = int(os.getenv("RADAR_CLAUDE_BRIDGE_MAX_EVENTS_PER_CYCLE", "5"))
# A PROCESSING row older than this (process died mid-call) is recovered back
# to PENDING rather than stuck forever (task section 2: recovery after timeout).
CLAUDE_BRIDGE_PROCESSING_STALE_SECONDS = float(os.getenv("RADAR_CLAUDE_BRIDGE_PROCESSING_STALE_SECONDS", "600.0"))
# Backoff schedule (seconds) indexed by attempt number - never an aggressive
# tight retry loop (task section 11). Clamped to the last entry past its length.
CLAUDE_BRIDGE_RETRY_BACKOFF_SECONDS = [60, 300, 900, 1800, 3600]
# After this many attempts a still-failing event is escalated PENDING/DEFERRED
# -> FAILED, so a permanently broken provider/schema issue doesn't retry forever.
CLAUDE_BRIDGE_MAX_ATTEMPTS_BEFORE_FAILED = int(os.getenv("RADAR_CLAUDE_BRIDGE_MAX_ATTEMPTS", "6"))

# --------------------------------------------------------------------------
# Loop mode cadence (task section 16) - never a tight/aggressive poll.
# --------------------------------------------------------------------------
LOOP_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("RADAR_LOOP_HEARTBEAT_INTERVAL_SECONDS", "60.0"))
LOOP_FULL_INTERVAL_SECONDS = float(os.getenv("RADAR_LOOP_FULL_INTERVAL_SECONDS", "300.0"))

# --------------------------------------------------------------------------
# Windows notifications (task section 13). LOW = terminal only (no toast).
# MEDIUM (Sonnet) = toast. HIGH (Fable) = toast + sound where supported.
# --------------------------------------------------------------------------
NOTIFICATIONS_ENABLED = os.getenv("RADAR_NOTIFICATIONS_ENABLED", "1") not in ("0", "false", "False")
NOTIFICATION_APP_ID = os.getenv("RADAR_NOTIFICATION_APP_ID", "CryptoRadar")
NOTIFICATION_LEVEL_BY_MODEL = {"SONNET": "MEDIUM", "FABLE": "HIGH"}

# --------------------------------------------------------------------------
# ntfy.sh mobile push (mobile notifications task). The topic is read ONLY
# from CRYPTO_RADAR_NTFY_TOPIC - never hardcoded, never logged in full
# (see radar_v08/ntfy.py::masked_topic). Unset topic => ntfy stays disabled
# and the radar behaves exactly as before this feature existed.
# LOW is never pushed to the phone - only MEDIUM (Sonnet) and HIGH (Fable),
# mirroring NOTIFICATION_LEVEL_BY_MODEL above.
# --------------------------------------------------------------------------
NTFY_TOPIC = os.getenv("CRYPTO_RADAR_NTFY_TOPIC") or None
NTFY_URL_BASE = os.getenv("RADAR_NTFY_URL_BASE", "https://ntfy.sh")
NTFY_TIMEOUT_SECONDS = float(os.getenv("RADAR_NTFY_TIMEOUT_SECONDS", "10.0"))
NTFY_MAX_RETRIES = int(os.getenv("RADAR_NTFY_MAX_RETRIES", "2"))
NTFY_RETRY_BACKOFF_BASE = float(os.getenv("RADAR_NTFY_RETRY_BACKOFF_BASE", "1.0"))
NTFY_PRIORITY_BY_LEVEL = {"MEDIUM": "default", "HIGH": "high"}
# Cross-cycle retry budget for a FAILED send (never an unbounded retry loop):
# at most this many attempts, spaced at least this many seconds apart.
NTFY_MAX_RETRY_ATTEMPTS = int(os.getenv("RADAR_NTFY_MAX_RETRY_ATTEMPTS", "5"))
NTFY_RETRY_MIN_INTERVAL_SECONDS = float(os.getenv("RADAR_NTFY_RETRY_MIN_INTERVAL_SECONDS", "300.0"))

# --------------------------------------------------------------------------
# "COPIAR PROMPT" - copies a ready-to-paste analysis prompt for the event to
# the Windows clipboard whenever a MEDIUM/HIGH Windows notification fires, and
# opens a small auxiliary window with a COPIAR PROMPT button (the toast on
# this radar has no registered activation handler, so a real clickable toast
# action isn't reliably supported - see prompt_popup.py). Purely additive:
# never replaces the existing Windows toast, never touches Kraken/Qwen/Claude.
# --------------------------------------------------------------------------
COPY_PROMPT_ENABLED = os.getenv("RADAR_COPY_PROMPT_ENABLED", "1") not in ("0", "false", "False")
COPY_PROMPT_POPUP_ENABLED = os.getenv("RADAR_COPY_PROMPT_POPUP_ENABLED", "1") not in ("0", "false", "False")

# --------------------------------------------------------------------------
# Alert recovery (`--mode alerts` / `--mode prompt --event <id>`) - lets a
# past alert be found and its prompt recopied even if the Windows toast/ntfy
# push is long gone from view. Read-only on top of the existing `events`
# table; adds no new persistence of its own.
# --------------------------------------------------------------------------
ALERTS_HISTORY_LIMIT = int(os.getenv("RADAR_ALERTS_HISTORY_LIMIT", "20"))
