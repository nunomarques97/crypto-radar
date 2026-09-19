"""Qwen 3:14b review, via a local Ollama server only (task section 6).

Qwen does NOT calculate anomaly_score, opportunity_score or tradeability_score
- those are already deterministic. Its job, over <= config.L3_MAX_FINALISTS
finalists that already cleared the pre-gate: veto incoherent setups, confirm
or correct setup_type/direction within closed enums, recommend call_sonnet /
call_fable, flag data quality. `think=false`, temperature 0, structured
output via Ollama's `format` JSON schema (not the bare string "json"), one
retry on invalid JSON or timeout, then UNAVAILABLE - the radar must keep
running on the deterministic gate alone if Qwen is down (task section 6/16).

Model, endpoint, timeout, temperature and think come from config.QWEN_RUNTIME,
the default profile of radar_v08/model_profiles.toml plus validated overrides
(T050c). Without it nothing is sent and the batch is UNAVAILABLE.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from . import config

logger = logging.getLogger("radar_v08.qwen")

SETUP_TYPES = ("BREAKOUT", "CONTINUATION", "REVERSAL", "SQUEEZE_RELEASE", "EXHAUSTION", "NONE")
DIRECTIONS = ("LONG", "SHORT", "NONE")
MARKETS = ("SPOT", "FUTURES", "BOTH", "NONE")
CONFIDENCES = ("LOW", "MEDIUM", "HIGH")

SYSTEM_PROMPT = (
    "You are the final gatekeeper of a read-only crypto radar. You receive up to "
    f"{config.L3_MAX_FINALISTS} finalists with numeric features already computed and a "
    "rule-based setup label. You do NOT compute scores (anomaly_score, opportunity_score, "
    "tradeability_score are already final). Your job, for EACH finalist: "
    "(1) veto it if the rule-based setup is contradicted by the supplied features; "
    "(2) confirm or correct setup_type and direction using ONLY the closed enums given; "
    "(3) recommend whether a deeper analysis (call_sonnet) or a much deeper one (call_fable) "
    "is worth its cost; (4) note data quality issues you notice (funding raw, quote fallback, "
    "warmup, thin book). Use ONLY the supplied data - never invent numbers or symbols. Funding "
    "fields labeled RAW_UNVERIFIED must NEVER be used to determine direction. Memecoins and "
    "high-beta assets are valid, ordinary candidates. Output MUST match the JSON schema exactly, "
    "with exactly one review per finalist, and `asset` must be copied verbatim from the input - "
    "never a symbol that wasn't given to you."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asset": {"type": "string"},
                    "setup_type": {"type": "string", "enum": list(SETUP_TYPES)},
                    "direction": {"type": "string", "enum": list(DIRECTIONS)},
                    "market": {"type": "string", "enum": list(MARKETS)},
                    "veto": {"type": "boolean"},
                    "call_sonnet": {"type": "boolean"},
                    "call_fable": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                    "reason": {"type": "string"},
                    "data_quality_notes": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "asset", "setup_type", "direction", "market", "veto",
                    "call_sonnet", "call_fable", "confidence", "reason", "data_quality_notes",
                ],
            },
        },
    },
    "required": ["reviews"],
}


@dataclass
class QwenReview:
    asset: str
    setup_type: str
    direction: str
    market: str
    veto: bool
    call_sonnet: bool
    call_fable: bool
    confidence: str
    reason: str
    data_quality_notes: list[str] = field(default_factory=list)


@dataclass
class QwenBatchResult:
    status: str  # OK | INVALID_JSON | TIMEOUT | UNAVAILABLE
    reviews: dict[str, QwenReview] = field(default_factory=dict)
    error: str | None = None
    # ProfileErrorCode value when the model profile or an override was refused (T050c).
    error_code: str | None = None


def _assert_local_ollama(url: str) -> None:
    if url not in config.OLLAMA_ALLOWED_HOSTS:
        raise RuntimeError(f"Refusing to call non-local Ollama host: {url}")


def _runtime() -> config.QwenRuntime:
    """The resolved profile, or a refusal: nothing is sent without one (T050c)."""
    runtime = config.QWEN_RUNTIME
    if runtime is None:
        raise RuntimeError(f"Refusing to call Ollama without a valid model profile: {config.QWEN_PROFILE_ERROR}")
    return runtime


def _default_post(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime()
    _assert_local_ollama(runtime.endpoint)
    response = requests.post(
        f"{runtime.endpoint}/api/chat", json=payload, timeout=runtime.timeout_seconds
    )
    response.raise_for_status()
    return response.json()


def _build_payload(finalists: list[dict[str, Any]], runtime: config.QwenRuntime) -> dict[str, Any]:
    user_content = (
        "Here are the finalists (JSON). Return JSON matching the schema, one review per "
        f"finalist, `asset` copied verbatim:\n{json.dumps(finalists, default=str)}"
    )
    return {
        "model": runtime.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "format": RESPONSE_SCHEMA,
        # The profile's context_tokens / output_cap_tokens are NOT sent (no num_ctx /
        # num_predict): options stay temperature only until T051 (T050c, D55(3)).
        "options": {"temperature": runtime.temperature},
        "think": runtime.think,
        "stream": False,
    }


def _extract_content(raw_response: dict[str, Any]) -> str:
    return raw_response.get("message", {}).get("content", "")


def _validate_reviews(parsed: dict[str, Any], valid_assets: set[str]) -> tuple[dict[str, QwenReview] | None, str | None]:
    reviews_raw = parsed.get("reviews")
    if not isinstance(reviews_raw, list):
        return None, "missing 'reviews' array"

    seen_assets: set[str] = set()
    out: dict[str, QwenReview] = {}
    for item in reviews_raw:
        if not isinstance(item, dict):
            return None, "review item is not an object"
        asset = item.get("asset")
        if asset not in valid_assets:
            return None, f"unknown symbol in review: {asset!r}"
        if asset in seen_assets:
            return None, f"duplicate review for {asset!r}"
        seen_assets.add(asset)

        if item.get("setup_type") not in SETUP_TYPES:
            return None, f"invalid setup_type: {item.get('setup_type')!r}"
        if item.get("direction") not in DIRECTIONS:
            return None, f"invalid direction: {item.get('direction')!r}"
        if item.get("market") not in MARKETS:
            return None, f"invalid market: {item.get('market')!r}"
        if item.get("confidence") not in CONFIDENCES:
            return None, f"invalid confidence: {item.get('confidence')!r}"
        if not isinstance(item.get("veto"), bool) or not isinstance(item.get("call_sonnet"), bool) or not isinstance(item.get("call_fable"), bool):
            return None, "veto/call_sonnet/call_fable must be booleans"

        out[asset] = QwenReview(
            asset=asset,
            setup_type=item["setup_type"],
            direction=item["direction"],
            market=item["market"],
            veto=item["veto"],
            call_sonnet=item["call_sonnet"],
            call_fable=item["call_fable"],
            confidence=item["confidence"],
            reason=str(item.get("reason", ""))[:200],
            data_quality_notes=[str(n) for n in (item.get("data_quality_notes") or [])],
        )

    missing = valid_assets - seen_assets
    if missing:
        return None, f"missing review(s) for: {sorted(missing)}"

    return out, None


def _profile_unavailable() -> QwenBatchResult:
    """Fail closed: no valid profile (or a refused override) means no call at all."""
    error = config.QWEN_PROFILE_ERROR
    code = error.code.value if error is not None else "profile_unavailable"
    logger.warning("Qwen UNAVAILABLE: model profile refused, Ollama not called: %s", error)
    return QwenBatchResult(
        status="UNAVAILABLE", reviews={}, error=f"model profile refused: {error}", error_code=code
    )


def review_finalists(
    finalists: list[dict[str, Any]],
    post_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> QwenBatchResult:
    """Review up to L3_MAX_FINALISTS finalists. `post_fn` is injectable for
    tests (no real Ollama call needed); defaults to the real local call.
    Without a valid model profile (config.QWEN_RUNTIME is None) nothing is
    posted and the result is UNAVAILABLE with a typed `error_code`.
    """
    runtime = config.QWEN_RUNTIME
    if runtime is None:
        if finalists and post_fn is None and config.OLLAMA_URL is not None:
            # An endpoint override outside OLLAMA_ALLOWED_HOSTS keeps today's refusal
            # (RuntimeError, before any call): the stricter rule wins.
            _assert_local_ollama(config.OLLAMA_URL)
        return _profile_unavailable()

    if not finalists:
        return QwenBatchResult(status="OK", reviews={})

    post = post_fn or _default_post
    valid_assets = {f["asset"] for f in finalists}
    payload = _build_payload(finalists, runtime)

    last_error: str | None = None
    for attempt in range(config.QWEN_MAX_RETRIES_ON_INVALID + 1):
        try:
            raw_response = post(payload)
        except requests.Timeout as exc:
            last_error = f"timeout: {exc}"
            logger.warning("Qwen call timed out (attempt %d): %s", attempt + 1, exc)
            continue
        except requests.RequestException as exc:
            last_error = f"request error: {exc}"
            logger.warning("Qwen call failed (attempt %d): %s", attempt + 1, exc)
            continue

        content = _extract_content(raw_response)
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            last_error = f"invalid JSON: {exc}"
            logger.warning("Qwen returned invalid JSON (attempt %d): %s", attempt + 1, exc)
            continue

        reviews, validation_error = _validate_reviews(parsed, valid_assets)
        if reviews is None:
            last_error = f"schema validation failed: {validation_error}"
            logger.warning("Qwen response failed validation (attempt %d): %s", attempt + 1, validation_error)
            continue

        return QwenBatchResult(status="OK", reviews=reviews)

    status = "TIMEOUT" if last_error and last_error.startswith("timeout") else "UNAVAILABLE"
    return QwenBatchResult(status=status, reviews={}, error=last_error)
