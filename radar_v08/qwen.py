"""Qwen 3:14b review, via a local Ollama server only (task section 6).

Qwen does NOT calculate anomaly_score, opportunity_score or tradeability_score
- those are already deterministic. Its job, over <= config.L3_MAX_FINALISTS
finalists that already cleared the pre-gate: veto incoherent setups, confirm
or correct setup_type/direction within closed enums, recommend call_sonnet /
call_fable, flag data quality. `think=false`, temperature 0, structured
output via Ollama's `format` JSON schema (not the bare string "json"), one
retry on invalid JSON or timeout while time remains, then UNAVAILABLE/TIMEOUT - the radar must keep
running on the deterministic gate alone if Qwen is down (task section 6/16).

Model, endpoint, timeout, context/output limits, temperature and think come from config.QWEN_RUNTIME,
the default profile of radar_v08/model_profiles.toml plus validated overrides.
Without it nothing is sent and the batch is UNAVAILABLE. The batch
shares one deadline across the batch and rejects late replies. A timed-out
transport keeps the process's sole slot until it finishes; GPU cancellation is
not implied by caller timeout.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from . import config

logger = logging.getLogger("radar_v08.qwen")

# A timed-out HTTP call may still be running. Keep its slot until it actually
# finishes; subsequent cycles fall back instead of piling up model requests.
_INFERENCE_SLOT = threading.Lock()

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
    # ProfileErrorCode value when the model profile or an override was refused,
    # otherwise a short code for why the batch is not OK (telemetry).
    error_code: str | None = None
    # Observability only:
    # batch wall time on the injected monotonic clock and transport calls started.
    elapsed_ms: float | None = None
    attempts: int = 0


def inference_active() -> bool:
    """True while an inference still holds the process's sole slot, timed-out calls included."""
    return _INFERENCE_SLOT.locked()


def _assert_local_ollama(url: str) -> None:
    if url not in config.OLLAMA_ALLOWED_HOSTS:
        raise RuntimeError(f"Refusing to call non-local Ollama host: {url}")


def _runtime() -> config.QwenRuntime:
    """The resolved profile, or a refusal: nothing is sent without one."""
    runtime = config.QWEN_RUNTIME
    if runtime is None:
        raise RuntimeError(f"Refusing to call Ollama without a valid model profile: {config.QWEN_PROFILE_ERROR}")
    return runtime


def _default_post(payload: dict[str, Any], runtime: config.QwenRuntime, timeout_seconds: float) -> object:
    _assert_local_ollama(runtime.endpoint)
    response = requests.post(
        f"{runtime.endpoint}/api/chat", json=payload, timeout=timeout_seconds
    )
    response.raise_for_status()
    try:
        return response.json()
    except (ValueError, RecursionError) as exc:
        # Translate only decoder failures into the existing request retry path.
        raise requests.RequestException("invalid JSON in Ollama response") from exc


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
        "options": {
            "temperature": runtime.temperature,
            "num_ctx": runtime.context_tokens,
            "num_predict": runtime.output_cap_tokens,
        },
        "think": runtime.think,
        "stream": False,
    }


def _extract_content(raw_response: object) -> str:
    # The server envelope is untrusted too. An empty string enters the same
    # bounded invalid-JSON retry path as missing or malformed model content.
    if not isinstance(raw_response, dict):
        return ""
    message = raw_response.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _validate_reviews(parsed: object, valid_assets: set[str]) -> tuple[dict[str, QwenReview] | None, str | None]:
    if not isinstance(parsed, dict):
        return None, "response is not an object"
    reviews_raw = parsed.get("reviews")
    if not isinstance(reviews_raw, list):
        return None, "missing 'reviews' array"

    seen_assets: set[str] = set()
    out: dict[str, QwenReview] = {}
    for item in reviews_raw:
        if not isinstance(item, dict):
            return None, "review item is not an object"
        asset = item.get("asset")
        if not isinstance(asset, str):
            return None, "asset must be a string"
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

        reason = item.get("reason")
        notes = item.get("data_quality_notes")
        if not isinstance(reason, str):
            return None, "reason must be a string"
        if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
            return None, "data_quality_notes must be an array of strings"

        out[asset] = QwenReview(
            asset=asset,
            setup_type=item["setup_type"],
            direction=item["direction"],
            market=item["market"],
            veto=item["veto"],
            call_sonnet=item["call_sonnet"],
            call_fable=item["call_fable"],
            confidence=item["confidence"],
            reason=reason[:200],
            data_quality_notes=list(notes),
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
    post_fn: Callable[[dict[str, Any]], object] | None = None,
    *,
    monotonic: Callable[[], float] | None = None,
) -> QwenBatchResult:
    """Review up to L3_MAX_FINALISTS finalists. `post_fn` is injectable for
    tests (no real Ollama call needed); defaults to the real local call.
    Without a valid model profile (config.QWEN_RUNTIME is None) nothing is
    posted and the result is UNAVAILABLE with a typed `error_code`.
    A batch that reached the deadline start also carries `elapsed_ms` and
    `attempts`; the fail-closed and empty paths keep `elapsed_ms` None.
    """
    clock = monotonic or time.monotonic
    started: list[float] = []
    attempts = [0]
    result = _review_batch(finalists, post_fn, clock, started, attempts)
    if started:
        result.elapsed_ms = max(0.0, (clock() - started[0]) * 1000)
    result.attempts = attempts[0]
    return result


def _review_batch(
    finalists: list[dict[str, Any]],
    post_fn: Callable[[dict[str, Any]], object] | None,
    clock: Callable[[], float],
    started: list[float],
    attempts: list[int],
) -> QwenBatchResult:
    runtime = config.QWEN_RUNTIME
    if runtime is None:
        if finalists and post_fn is None and config.OLLAMA_URL is not None:
            # An endpoint override outside OLLAMA_ALLOWED_HOSTS keeps today's refusal
            # (RuntimeError, before any call): the stricter rule wins.
            _assert_local_ollama(config.OLLAMA_URL)
        return _profile_unavailable()

    if not finalists:
        return QwenBatchResult(status="OK", reviews={})

    # The deadline read is also the telemetry start: no extra clock read.
    started.append(clock())
    deadline = started[0] + runtime.timeout_seconds
    if not _INFERENCE_SLOT.acquire(blocking=False):
        return QwenBatchResult(status="UNAVAILABLE", error="previous inference still active", error_code="inference_busy")

    done = threading.Event()
    cancelled = threading.Event()
    results: list[QwenBatchResult] = []
    errors: list[BaseException] = []
    ownership = threading.Lock()
    work_started = False
    slot_released = False

    def release_slot(*, only_unstarted: bool = False) -> None:
        nonlocal slot_released
        with ownership:
            if not slot_released and (not only_unstarted or not work_started):
                slot_released = True
                _INFERENCE_SLOT.release()

    def work() -> None:
        nonlocal work_started
        with ownership:
            # start() can be interrupted after launching the native thread.
            # A relinquished slot must never be used by a late-starting worker.
            if slot_released:
                done.set()
                return
            work_started = True
        try:
            results.append(_review_until_deadline(finalists, runtime, post_fn, clock, deadline, cancelled, attempts))
        except BaseException as exc:
            # Relay internal errors to the caller; do not disguise bugs as model
            # unavailability. The deadline still fences errors arriving late.
            errors.append(exc)
        finally:
            release_slot()
            done.set()

    try:
        worker = threading.Thread(target=work, name="qwen-batch", daemon=True)
        worker.start()
    except BaseException:
        cancelled.set()
        release_slot(only_unstarted=True)
        raise
    try:
        finished = done.wait(max(0.0, deadline - clock()))
    except BaseException:
        cancelled.set()
        raise
    if not finished or clock() >= deadline:
        cancelled.set()
        return _deadline_timeout()
    if errors:
        raise errors[0]
    return results[0]


def _deadline_timeout() -> QwenBatchResult:
    return QwenBatchResult(status="TIMEOUT", error="timeout: batch deadline exceeded", error_code="deadline_exceeded")


def _review_until_deadline(
    finalists: list[dict[str, Any]],
    runtime: config.QwenRuntime,
    post_fn: Callable[[dict[str, Any]], object] | None,
    clock: Callable[[], float],
    deadline: float,
    cancelled: threading.Event,
    attempts: list[int],
) -> QwenBatchResult:
    valid_assets = {f["asset"] for f in finalists}
    payload = _build_payload(finalists, runtime)

    last_error: str | None = None
    last_error_code: str | None = None
    for attempt in range(config.QWEN_MAX_RETRIES_ON_INVALID + 1):
        remaining = deadline - clock()
        if cancelled.is_set() or remaining <= 0:
            return _deadline_timeout()
        attempts[0] += 1
        try:
            raw_response = post_fn(payload) if post_fn is not None else _default_post(payload, runtime, remaining)
        except requests.Timeout as exc:
            last_error, last_error_code = f"timeout: {exc}", "timeout"
            logger.warning("Qwen call timed out (attempt %d): %s", attempt + 1, exc)
            continue
        except requests.RequestException as exc:
            last_error, last_error_code = f"request error: {exc}", "request_error"
            logger.warning("Qwen call failed (attempt %d): %s", attempt + 1, exc)
            continue

        if cancelled.is_set() or clock() >= deadline:
            return _deadline_timeout()
        if isinstance(raw_response, dict):
            count = raw_response.get("eval_count")
            if (
                raw_response.get("done") is False
                or raw_response.get("done_reason", "stop") != "stop"
                or ("eval_count" in raw_response and (type(count) is not int or count < 0 or count > runtime.output_cap_tokens))
            ):
                last_error, last_error_code = "incomplete response or invalid output token count", "incomplete_response"
                continue

        content = _extract_content(raw_response)
        try:
            parsed = json.loads(content)
        except (ValueError, RecursionError) as exc:
            last_error, last_error_code = f"invalid JSON: {exc}", "invalid_json"
            logger.warning("Qwen returned invalid JSON (attempt %d): %s", attempt + 1, exc)
            continue

        reviews, validation_error = _validate_reviews(parsed, valid_assets)
        if reviews is None:
            last_error, last_error_code = f"schema validation failed: {validation_error}", "schema_invalid"
            logger.warning("Qwen response failed validation (attempt %d): %s", attempt + 1, validation_error)
            continue

        if cancelled.is_set() or clock() >= deadline:
            return _deadline_timeout()
        return QwenBatchResult(status="OK", reviews=reviews)

    if cancelled.is_set() or clock() >= deadline:
        return _deadline_timeout()
    status = "TIMEOUT" if last_error and last_error.startswith("timeout") else "UNAVAILABLE"
    return QwenBatchResult(status=status, reviews={}, error=last_error, error_code=last_error_code)
