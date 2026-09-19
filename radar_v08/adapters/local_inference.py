"""Local Ollama inference adapter (T032b, OC-1 §3): loopback only, bounded, no redirects.

Implements ``workflow.worker.LocalInference`` with ``requests`` (already pinned), like
``radar_v08.qwen`` but stricter:

* The endpoint is fixed at construction and must be ``http://127.0.0.1:<port>`` or
  ``http://localhost:<port>`` with an explicit port and nothing else (no userinfo,
  path, query or fragment). Any other host is refused before a session exists. There
  is no second endpoint and no cloud fallback.
* The session ignores the environment (``trust_env = False``): no proxy variables and
  no ``.netrc`` credentials can reroute or decorate a loopback request.
* Redirects are never followed (``allow_redirects=False``); a 3xx is a typed failure.
* The whole call (connect, model load, prompt processing, generation and body read) is
  bounded by ``call.timeout_seconds`` on the monotonic clock, checked between reads;
  the body is capped at ``max_response_bytes``. The worker's watchdog bounds a read
  that blocks past the limit.
* The reply must be a JSON envelope with ``done: true`` and ``message.content`` that is
  itself one JSON object (Ollama structured output via ``format``). Anything else is
  ``MALFORMED``: there is no free-text parsing and no JSON extraction from prose.
  Non-finite numbers and duplicate keys are rejected. Failures carry codes only.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from enum import Enum
from urllib.parse import urlsplit

import requests

from ..workflow.worker import (
    CancelSignal,
    InferenceCall,
    InferenceFailed,
    InferenceFailure,
    InferenceReply,
    InferenceResult,
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
CHAT_PATH = "/api/chat"
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_CONNECT_TIMEOUT_SECONDS = 2.0
_CHUNK_BYTES = 8192


class EndpointRefusal(Enum):
    MALFORMED_URL = "malformed_url"
    NOT_HTTP = "not_http"
    USERINFO = "userinfo"
    NOT_LOOPBACK = "not_loopback"
    PORT_REQUIRED = "port_required"
    EXTRA_PARTS = "extra_parts"  # a path, query or fragment


class LocalEndpointRefused(ValueError):
    """The configured endpoint is not a plain loopback URL; no request can be made."""

    def __init__(self, code: EndpointRefusal) -> None:
        super().__init__(f"local inference endpoint refused: {code.value}")
        self.code = code


def loopback_base_url(url: object) -> str:
    """Return ``http://<loopback host>:<port>`` or raise ``LocalEndpointRefused``."""
    if not isinstance(url, str) or not url or any(ord(char) <= 0x20 or ord(char) == 0x7F or char == "\\" for char in url):
        raise LocalEndpointRefused(EndpointRefusal.MALFORMED_URL)
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise LocalEndpointRefused(EndpointRefusal.MALFORMED_URL) from None
    if parts.scheme != "http" or not url.startswith("http://"):
        raise LocalEndpointRefused(EndpointRefusal.NOT_HTTP)
    if "@" in parts.netloc:
        raise LocalEndpointRefused(EndpointRefusal.USERINFO)
    if host not in LOOPBACK_HOSTS:
        raise LocalEndpointRefused(EndpointRefusal.NOT_LOOPBACK)
    if port is None or port == 0:
        raise LocalEndpointRefused(EndpointRefusal.PORT_REQUIRED)
    if parts.netloc != f"{host}:{port}":  # case variants, trailing dots, padded ports
        raise LocalEndpointRefused(EndpointRefusal.NOT_LOOPBACK)
    if parts.path not in ("", "/") or parts.query or parts.fragment or "?" in url or "#" in url:
        raise LocalEndpointRefused(EndpointRefusal.EXTRA_PARTS)
    return f"http://{host}:{port}"


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-finite JSON constant {name}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_json(text: str | bytes) -> object:
    return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)


def _count(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("token count must be a non-negative int")
    return value


def parse_chat_reply(body: bytes, output_cap_tokens: int) -> InferenceResult:
    """Parse a non-streamed ``/api/chat`` body into a JSON-object payload, or ``MALFORMED``."""
    malformed = InferenceFailed(InferenceFailure.MALFORMED)
    try:
        envelope = _strict_json(body)
        if not isinstance(envelope, dict) or envelope.get("done") is not True:
            return malformed
        if envelope.get("done_reason", "stop") != "stop":  # e.g. "length": truncated by the output cap
            return malformed
        message = envelope.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return malformed
        content = message.get("content")
        if not isinstance(content, str):
            return malformed
        payload = _strict_json(content)
        if not isinstance(payload, dict):
            return malformed
        prompt_tokens = _count(envelope.get("prompt_eval_count"))
        output_tokens = _count(envelope.get("eval_count"))
    except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return malformed
    if output_tokens is not None and output_tokens > output_cap_tokens:
        return malformed
    return InferenceReply(payload=payload, prompt_tokens=prompt_tokens, output_tokens=output_tokens)


def chat_payload(call: InferenceCall) -> dict[str, object]:
    """The ``/api/chat`` request for one call; the model comes from the call's profile."""
    return {
        "model": call.model,
        "messages": [
            {"role": "system", "content": call.system},
            {"role": "user", "content": call.user},
        ],
        "format": dict(call.response_schema),
        "options": {
            "temperature": 0,
            "num_predict": call.output_cap_tokens,
            "num_ctx": call.context_tokens,
        },
        "think": call.think,
        "stream": False,
    }


class OllamaLocalInference:
    """``LocalInference`` over a loopback Ollama server."""

    def __init__(
        self,
        base_url: str,
        *,
        session: requests.Session | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._endpoint = loopback_base_url(base_url) + CHAT_PATH
        if not math.isfinite(connect_timeout_seconds) or connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be finite and above zero")
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive int")
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False
        self._monotonic = monotonic
        self._connect_timeout = float(connect_timeout_seconds)
        self._max_bytes = max_response_bytes

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def close(self) -> None:
        self._session.close()

    def infer(self, call: InferenceCall, cancel: CancelSignal) -> InferenceResult:
        if cancel.is_set():
            return InferenceFailed(InferenceFailure.CANCELLED)
        deadline = self._monotonic() + call.timeout_seconds
        try:
            data = json.dumps(chat_payload(call), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            return InferenceFailed(InferenceFailure.INVALID_REQUEST)
        try:
            response = self._session.post(
                self._endpoint,
                data=data,
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
                timeout=(min(self._connect_timeout, call.timeout_seconds), call.timeout_seconds),
                allow_redirects=False,
                stream=True,
            )
        except requests.ConnectTimeout:  # nothing reached a server: it is not there
            return InferenceFailed(InferenceFailure.UNAVAILABLE)
        except requests.Timeout:
            return InferenceFailed(InferenceFailure.TIMEOUT)
        except requests.RequestException:
            return InferenceFailed(InferenceFailure.TIMEOUT if self._monotonic() >= deadline else InferenceFailure.UNAVAILABLE)
        try:
            return self._read(response, call, cancel, deadline)
        finally:
            response.close()

    def _read(
        self, response: requests.Response, call: InferenceCall, cancel: CancelSignal, deadline: float
    ) -> InferenceResult:
        if 300 <= response.status_code < 400 or response.history:
            return InferenceFailed(InferenceFailure.REDIRECT_REFUSED)
        if response.status_code != 200:
            return InferenceFailed(InferenceFailure.HTTP_STATUS)
        declared = response.headers.get("Content-Length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
            return InferenceFailed(InferenceFailure.TOO_LARGE)
        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                if cancel.is_set():
                    return InferenceFailed(InferenceFailure.CANCELLED)
                if self._monotonic() >= deadline:
                    return InferenceFailed(InferenceFailure.TIMEOUT)
                body.extend(chunk)
                if len(body) > self._max_bytes:
                    return InferenceFailed(InferenceFailure.TOO_LARGE)
        except requests.RequestException:
            return InferenceFailed(InferenceFailure.TIMEOUT if self._monotonic() >= deadline else InferenceFailure.UNAVAILABLE)
        if cancel.is_set():
            return InferenceFailed(InferenceFailure.CANCELLED)
        if self._monotonic() >= deadline:
            return InferenceFailed(InferenceFailure.TIMEOUT)
        return parse_chat_reply(bytes(body), call.output_cap_tokens)
