"""T051 benchmark adapter: loopback Ollama HTTP with a route allowlist, and resource probes.

T051c (D61; TECHNOLOGY.md S6). Used only by ``scripts/run_t051_block.py``; the radar runtime
never imports it.

Network (fail closed, typed errors):

* The base URL must pass ``local_inference.loopback_base_url`` (unchanged) AND name the host
  ``127.0.0.1``; the runner always passes ``OLLAMA_BASE_URL`` (``http://127.0.0.1:11434``).
* Every request goes through ONE method, ``OllamaControl._request``, which refuses any
  (method, path) outside ``ALLOWED_ROUTES``: ``GET /api/tags``, ``POST /api/show``,
  ``GET /api/ps``, ``POST /api/generate``, ``POST /api/chat``. ``/api/pull``, ``/api/push``,
  ``/api/create``, ``/api/delete``, ``/api/copy`` and everything else raise
  ``T051NetworkRefused`` before any socket is opened. There is no subprocess of ``ollama``.
* The session ignores the environment (no proxy, no ``.netrc``), never follows a redirect,
  caps every body, and bounds every request by a deadline on the monotonic clock.
* A connection failure on a control call is ``OllamaUnavailable`` (the runner reports
  BLOCKED); a malformed control reply is ``OllamaProtocolError``.
* ``/api/generate`` is used only to unload a benchmark model (``keep_alive: 0``, no prompt).

Model calls (``T051Inference``, the harness ``LocalInference`` port) send the request of
``local_inference.chat_payload`` (temperature 0, ``num_predict`` / ``num_ctx`` from the
profile, structured output) with two recorded changes: ``think`` is sent (false) only to a
model whose ``/api/show`` capabilities include ``thinking`` and is left out otherwise, and
``keep_alive`` is set so the model stays loaded between calls of one invocation (the runner
unloads it explicitly). The reply is parsed by ``local_inference.parse_chat_reply`` (unchanged).
Everything actually sent (except the prompt text, kept as sha256) is recorded per call.

Resources (TECHNOLOGY.md S6): ``nvidia-smi --query-gpu=memory.used,memory.total
--format=csv,noheader,nounits``; ``GET /api/ps`` ``size`` / ``size_vram``; ``tasklist /FI
"IMAGENAME eq ollama.exe" /FO CSV`` (sum of the working sets); each subprocess with a fixed
argument list, no shell, a 5 s timeout. Both tools run by ABSOLUTE path, resolved once per probe
in the Windows system directory (``GetSystemDirectoryW``, not ``%SystemRoot%`` nor PATH, and not
``shutil.which``, which on Windows looks in the current directory first): a ``tasklist.exe`` or
``nvidia-smi.exe`` planted in the working directory or the Python folder is never run. A tool
that is not there is not measured. Free system RAM for the harness reserve gate comes
from ``GlobalMemoryStatusEx`` (stdlib ``ctypes``, as ``radar_v08/clipboard.py`` already uses
``kernel32``). A tool that fails, times out or prints something unreadable gives ``None``
("not measured"): never zero, never a previous value.
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import io
import json
import math
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import requests

from ..workflow.benchmark import ResourceReport
from ..workflow.worker import (
    CancelSignal,
    InferenceCall,
    InferenceFailed,
    InferenceFailure,
    InferenceResult,
)
from .local_inference import chat_payload, loopback_base_url, parse_chat_reply

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
ALLOWED_HOST = "127.0.0.1"
ALLOWED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/tags"),
        ("POST", "/api/show"),
        ("GET", "/api/ps"),
        ("POST", "/api/generate"),
        ("POST", "/api/chat"),
    }
)
CONTROL_TIMEOUT_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 2.0
MAX_CONTROL_BYTES = 4 * 1_048_576
MAX_CHAT_BYTES = 1_048_576
MAX_RECORDED_BODY_CHARS = 65_536
SUBPROCESS_TIMEOUT_SECONDS = 5
# Keeps the model loaded between the calls of one invocation; the runner unloads it explicitly
# when it changes model and at the end of every invocation, so this only matters if the process
# dies, and then the model leaves memory on its own after this many seconds.
CALL_KEEP_ALIVE_SECONDS = 120
NVIDIA_SMI_ARGS: tuple[str, ...] = ("nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits")
TASKLIST_ARGS: tuple[str, ...] = ("tasklist", "/FI", "IMAGENAME eq ollama.exe", "/FO", "CSV")
_CHUNK_BYTES = 8192
_OOM_MARKERS = ("out of memory", "requires more system memory", "cudamalloc failed", "insufficient memory")
_GIB = 1024**3


class NetworkRefusal(Enum):
    HOST_NOT_ALLOWED = "host_not_allowed"
    ROUTE_NOT_ALLOWED = "route_not_allowed"


class T051NetworkRefused(ValueError):
    """A request outside the T051 allowlist was asked for. Nothing was sent."""

    def __init__(self, code: NetworkRefusal, detail: str) -> None:
        super().__init__(f"T051 network refused: {code.value}: {detail}")
        self.code = code


class OllamaUnavailable(RuntimeError):
    """The loopback Ollama server did not answer a control call (down, refused, timed out)."""


class OllamaProtocolError(RuntimeError):
    """A control reply was not the JSON the runner expects."""


def t051_base_url(url: object) -> str:
    """``loopback_base_url`` (unchanged) plus: the host must be exactly ``127.0.0.1``."""
    base = loopback_base_url(url)
    if urlsplit(base).hostname != ALLOWED_HOST:
        raise T051NetworkRefused(NetworkRefusal.HOST_NOT_ALLOWED, "only 127.0.0.1")
    return base


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-finite JSON constant {name}")


def _strict_json(data: bytes) -> object:
    return json.loads(data, parse_constant=_reject_constant)


@dataclass(frozen=True, slots=True)
class HttpExchange:
    status: int | None
    body: bytes
    wall_seconds: float
    failure: InferenceFailure | None  # None when a complete body was read


@dataclass(frozen=True, slots=True)
class InstalledModel:
    name: str
    digest: str


@dataclass(frozen=True, slots=True)
class ModelDetails:
    name: str
    quantization: str | None
    context_length: int | None
    parameter_size: str | None
    family: str | None
    format: str | None
    parameters: str | None
    capabilities: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "quantization": self.quantization,
            "context_length": self.context_length,
            "parameter_size": self.parameter_size,
            "family": self.family,
            "format": self.format,
            "parameters": self.parameters,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class LoadedModel:
    name: str
    digest: str | None
    size: int | None
    size_vram: int | None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


class OllamaControl:
    """The only HTTP client of the T051 runner."""

    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        *,
        session: requests.Session | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base = t051_base_url(base_url)
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False
        self._monotonic = monotonic
        self._sleep = sleep
        self.sent: list[tuple[str, str]] = []  # every (method, path) actually sent, in order

    @property
    def base_url(self) -> str:
        return self._base

    def close(self) -> None:
        self._session.close()

    def _request(self, method: str, path: str, payload: object, timeout_seconds: float, max_bytes: int) -> HttpExchange:
        """The single choke point: allowlisted route, loopback host, bounded time and size."""
        if (method, path) not in ALLOWED_ROUTES:
            raise T051NetworkRefused(NetworkRefusal.ROUTE_NOT_ALLOWED, f"{method} {path}")
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
        started = self._monotonic()
        deadline = started + timeout_seconds
        self.sent.append((method, path))
        try:
            response = self._session.request(
                method,
                self._base + path,
                data=data,
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
                timeout=(min(CONNECT_TIMEOUT_SECONDS, timeout_seconds), timeout_seconds),
                allow_redirects=False,
                stream=True,
            )
        except requests.ConnectTimeout:
            return HttpExchange(None, b"", self._monotonic() - started, InferenceFailure.UNAVAILABLE)
        except requests.Timeout:
            return HttpExchange(None, b"", self._monotonic() - started, InferenceFailure.TIMEOUT)
        except requests.RequestException:
            failure = InferenceFailure.TIMEOUT if self._monotonic() >= deadline else InferenceFailure.UNAVAILABLE
            return HttpExchange(None, b"", self._monotonic() - started, failure)
        try:
            status = response.status_code
            if 300 <= status < 400 or response.history:
                return HttpExchange(status, b"", self._monotonic() - started, InferenceFailure.REDIRECT_REFUSED)
            body = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                    if self._monotonic() >= deadline:
                        return HttpExchange(status, bytes(body), self._monotonic() - started, InferenceFailure.TIMEOUT)
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        return HttpExchange(status, b"", self._monotonic() - started, InferenceFailure.TOO_LARGE)
            except requests.RequestException:
                failure = InferenceFailure.TIMEOUT if self._monotonic() >= deadline else InferenceFailure.UNAVAILABLE
                return HttpExchange(status, bytes(body), self._monotonic() - started, failure)
            elapsed = self._monotonic() - started
            if elapsed >= timeout_seconds:
                return HttpExchange(status, bytes(body), elapsed, InferenceFailure.TIMEOUT)
            return HttpExchange(status, bytes(body), elapsed, None)
        finally:
            response.close()

    def _control(self, method: str, path: str, payload: object = None) -> Mapping[str, object]:
        exchange = self._request(method, path, payload, CONTROL_TIMEOUT_SECONDS, MAX_CONTROL_BYTES)
        if exchange.failure in (InferenceFailure.UNAVAILABLE, InferenceFailure.TIMEOUT):
            raise OllamaUnavailable(f"{method} {path}: {exchange.failure.value}")
        if exchange.failure is not None or exchange.status != 200:
            code = exchange.failure.value if exchange.failure is not None else str(exchange.status)
            raise OllamaProtocolError(f"{method} {path}: {code}")
        try:
            document = _strict_json(exchange.body)
        except (ValueError, RecursionError):
            raise OllamaProtocolError(f"{method} {path}: not JSON") from None
        if not isinstance(document, dict):
            raise OllamaProtocolError(f"{method} {path}: not a JSON object")
        return document

    def tags(self) -> dict[str, InstalledModel]:
        """Installed models by name (``GET /api/tags``). No inference, nothing downloaded."""
        models = self._control("GET", "/api/tags").get("models")
        if not isinstance(models, list):
            raise OllamaProtocolError("GET /api/tags: no models list")
        found: dict[str, InstalledModel] = {}
        for item in models:
            if not isinstance(item, Mapping):
                raise OllamaProtocolError("GET /api/tags: malformed model entry")
            name = item.get("name", item.get("model"))
            digest = item.get("digest")
            if not isinstance(name, str) or not isinstance(digest, str) or not digest:
                raise OllamaProtocolError("GET /api/tags: model entry without name or digest")
            found[name] = InstalledModel(name=name, digest=digest)
        return found

    def show(self, model: str) -> ModelDetails:
        """Quantization, context, settings and capabilities (``POST /api/show``). No inference."""
        document = self._control("POST", "/api/show", {"model": model})
        details = document.get("details")
        details = details if isinstance(details, Mapping) else {}
        info = document.get("model_info")
        context: int | None = None
        if isinstance(info, Mapping):
            lengths = [value for key, value in info.items() if isinstance(key, str) and key.endswith(".context_length")]
            context = _optional_count(lengths[0]) if len(lengths) == 1 else None
        capabilities = document.get("capabilities")
        return ModelDetails(
            name=model,
            quantization=_optional_text(details.get("quantization_level")),
            context_length=context,
            parameter_size=_optional_text(details.get("parameter_size")),
            family=_optional_text(details.get("family")),
            format=_optional_text(details.get("format")),
            parameters=_optional_text(document.get("parameters")),
            capabilities=tuple(sorted(item for item in capabilities if isinstance(item, str)))
            if isinstance(capabilities, list)
            else (),
        )

    def ps(self) -> tuple[LoadedModel, ...]:
        """Models loaded right now (``GET /api/ps``)."""
        models = self._control("GET", "/api/ps").get("models")
        if not isinstance(models, list):
            raise OllamaProtocolError("GET /api/ps: no models list")
        loaded: list[LoadedModel] = []
        for item in models:
            if not isinstance(item, Mapping):
                raise OllamaProtocolError("GET /api/ps: malformed model entry")
            name = item.get("name", item.get("model"))
            if not isinstance(name, str):
                raise OllamaProtocolError("GET /api/ps: model entry without name")
            loaded.append(
                LoadedModel(
                    name=name,
                    digest=_optional_text(item.get("digest")),
                    size=_optional_count(item.get("size")),
                    size_vram=_optional_count(item.get("size_vram")),
                )
            )
        return tuple(loaded)

    def unload(self, model: str, wait_seconds: float) -> bool:
        """Unload ONE benchmark model (``keep_alive: 0``) and wait until ``/api/ps`` drops it."""
        self._control("POST", "/api/generate", {"model": model, "keep_alive": 0})
        deadline = self._monotonic() + wait_seconds
        while True:
            if all(item.name != model for item in self.ps()):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(0.25)

    def chat(self, payload: Mapping[str, object], timeout_seconds: float) -> HttpExchange:
        return self._request("POST", "/api/chat", dict(payload), timeout_seconds, MAX_CHAT_BYTES)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_oom(exchange: HttpExchange) -> bool:
    if exchange.status is None or exchange.status < 500:
        return False
    text = exchange.body[:MAX_RECORDED_BODY_CHARS].decode("utf-8", errors="replace").casefold()
    return any(marker in text for marker in _OOM_MARKERS)


@dataclass(frozen=True, slots=True)
class CallTelemetry:
    """What one model call sent and received, for the raw results file."""

    request: Mapping[str, object]
    status: int | None
    failure: str | None
    oom: bool
    wall_seconds: float
    envelope_metrics: Mapping[str, object]
    response_body: str
    response_body_truncated: bool
    response_body_sha256: str


_ENVELOPE_METRICS = (
    "total_duration", "load_duration", "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration",
    "done_reason",
)


def _envelope_metrics(body: bytes) -> dict[str, object]:
    try:
        envelope = _strict_json(body)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(envelope, dict):
        return {}
    metrics: dict[str, object] = {}
    for key in _ENVELOPE_METRICS:
        value = envelope.get(key)
        if (type(value) is int and value >= 0) or isinstance(value, str):
            metrics[key] = value
    return metrics


class T051Inference:
    """The harness ``LocalInference`` port over ``OllamaControl.chat``; keeps the last call's telemetry."""

    def __init__(self, control: OllamaControl, capabilities: Mapping[str, Sequence[str]]) -> None:
        self._control = control
        self._capabilities = {model: tuple(items) for model, items in capabilities.items()}
        self.last: CallTelemetry | None = None

    def request_for(self, call: InferenceCall) -> dict[str, object]:
        payload = chat_payload(call)
        if "thinking" not in self._capabilities.get(call.model, ()):
            payload.pop("think", None)  # "think false when applicable": only to thinking models
        payload["keep_alive"] = CALL_KEEP_ALIVE_SECONDS
        return payload

    def infer(self, call: InferenceCall, cancel: CancelSignal) -> InferenceResult:
        self.last = None
        if cancel.is_set():
            return InferenceFailed(InferenceFailure.CANCELLED)
        payload = self.request_for(call)
        recorded = {key: value for key, value in payload.items() if key not in ("messages", "format")}
        recorded["messages_sha256"] = _sha256_text(json.dumps(payload["messages"], sort_keys=True, ensure_ascii=False))
        recorded["format_sha256"] = _sha256_text(json.dumps(payload["format"], sort_keys=True, separators=(",", ":")))
        recorded["path"] = "/api/chat"
        recorded["timeout_seconds"] = call.timeout_seconds
        exchange = self._control.chat(payload, call.timeout_seconds)
        text = exchange.body.decode("utf-8", errors="replace")
        oom = _is_oom(exchange)
        if exchange.failure is not None:
            result: InferenceResult = InferenceFailed(exchange.failure)
        elif exchange.status != 200:
            result = InferenceFailed(InferenceFailure.HTTP_STATUS)
        else:
            result = parse_chat_reply(exchange.body, call.output_cap_tokens)
        self.last = CallTelemetry(
            request=recorded,
            status=exchange.status,
            failure=result.failure.value if isinstance(result, InferenceFailed) else None,
            oom=oom,
            wall_seconds=exchange.wall_seconds,
            envelope_metrics=_envelope_metrics(exchange.body) if exchange.failure is None else {},
            response_body=text[:MAX_RECORDED_BODY_CHARS],
            response_body_truncated=len(text) > MAX_RECORDED_BODY_CHARS,
            response_body_sha256=hashlib.sha256(exchange.body).hexdigest(),
        )
        return result


# -- resources ---------------------------------------------------------------------------------


class ProcessRunner(Protocol):
    def __call__(
        self, args: Sequence[str], *, capture_output: bool, text: bool, timeout: float, check: bool, shell: bool
    ) -> subprocess.CompletedProcess[str]: ...


def _run_subprocess(
    args: Sequence[str], *, capture_output: bool, text: bool, timeout: float, check: bool, shell: bool
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=capture_output, text=text, timeout=timeout, check=check, shell=shell)


_SYSTEM_DIRECTORY_BUFFER = 260
# Tool name (first item of the S6 argument list) -> executable file in the system directory.
# Current NVIDIA drivers install nvidia-smi.exe there; an older layout is simply not measured.
TOOL_EXECUTABLES: Mapping[str, str] = {"nvidia-smi": "nvidia-smi.exe", "tasklist": "tasklist.exe"}


def windows_system_directory() -> Path | None:
    r"""``GetSystemDirectoryW`` (e.g. ``C:\Windows\System32``), or ``None`` off Windows / on failure."""
    try:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        buffer = ctypes.create_unicode_buffer(_SYSTEM_DIRECTORY_BUFFER)
        length = int(windll.kernel32.GetSystemDirectoryW(buffer, _SYSTEM_DIRECTORY_BUFFER))
        if not 0 < length < _SYSTEM_DIRECTORY_BUFFER:
            return None
        return Path(buffer.value)
    except (AttributeError, OSError, ValueError):
        return None


def locate_tool(name: str, system_directory: Callable[[], Path | None] = windows_system_directory) -> str | None:
    """Absolute path of a measurement tool in the system directory, or ``None`` (not measured)."""
    executable = TOOL_EXECUTABLES.get(name)
    directory = system_directory()
    if executable is None or directory is None or not directory.is_absolute():
        return None
    candidate = directory / executable
    return str(candidate) if candidate.is_file() else None


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def system_free_ram_bytes() -> int | None:
    """Available physical memory (``GlobalMemoryStatusEx``), or ``None`` when not measured."""
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        windll = getattr(ctypes, "windll", None)
        if windll is None or not windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPhys)
    except (AttributeError, OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class GpuMemory:
    used_mib: int
    total_mib: int


def parse_nvidia_smi(output: str) -> GpuMemory | None:
    """First GPU line ``used, total`` in MiB; anything else is not measured."""
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    if not lines:
        return None
    parts = [part.strip() for part in lines[0].split(",")]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    used, total = int(parts[0]), int(parts[1])
    if total <= 0 or used > total:
        return None
    return GpuMemory(used_mib=used, total_mib=total)


def parse_tasklist(output: str) -> int | None:
    """Sum of the working sets of every ``ollama.exe`` row, in bytes; None when there is none."""
    total = 0
    rows = 0
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 5 or row[0].strip().casefold() != "ollama.exe":
            continue
        digits = "".join(char for char in row[4] if char.isdigit())
        unit = row[4].strip()[-1:].upper()
        if not digits or unit != "K":
            return None
        total += int(digits) * 1024
        rows += 1
    return total if rows else None


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    gpu_used_mib: int | None
    gpu_total_mib: int | None
    free_vram_gib: float | None
    free_ram_bytes: int | None
    free_ram_gib: float | None
    ollama_process_ram_bytes: int | None
    ollama_ps_size_bytes: int | None
    ollama_ps_size_vram_bytes: int | None
    ollama_ps_digest: str | None
    oom: bool

    def as_dict(self) -> dict[str, object]:
        values: dict[str, object] = {
            "gpu_used_mib": self.gpu_used_mib,
            "gpu_total_mib": self.gpu_total_mib,
            "free_vram_gib": self.free_vram_gib,
            "free_ram_bytes": self.free_ram_bytes,
            "free_ram_gib": self.free_ram_gib,
            "ollama_process_ram_bytes": self.ollama_process_ram_bytes,
            "ollama_ps_size_bytes": self.ollama_ps_size_bytes,
            "ollama_ps_size_vram_bytes": self.ollama_ps_size_vram_bytes,
            "ollama_ps_digest": self.ollama_ps_digest,
            "oom": self.oom,
        }
        values["not_measured"] = sorted(name for name, value in values.items() if value is None)
        return values


class ResourceProbe:
    """nvidia-smi, tasklist and free RAM; ``None`` for anything that could not be measured."""

    def __init__(
        self,
        run: ProcessRunner = _run_subprocess,
        free_ram: Callable[[], int | None] = system_free_ram_bytes,
        locate: Callable[[str], str | None] = locate_tool,
    ) -> None:
        self._run = run
        self._free_ram = free_ram
        self._executables: dict[str, str | None] = {}
        for name in TOOL_EXECUTABLES:
            try:
                path = locate(name)
            except (OSError, ValueError):
                path = None
            self._executables[name] = path if isinstance(path, str) and Path(path).is_absolute() else None

    @property
    def executables(self) -> Mapping[str, str | None]:
        return dict(self._executables)

    def _output(self, args: Sequence[str]) -> str | None:
        executable = self._executables.get(args[0])
        if executable is None:
            return None
        try:
            completed = self._run(
                [executable, *args[1:]], capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS, check=False, shell=False
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if completed.returncode != 0 or not isinstance(completed.stdout, str):
            return None
        return completed.stdout

    def gpu(self) -> GpuMemory | None:
        output = self._output(NVIDIA_SMI_ARGS)
        return None if output is None else parse_nvidia_smi(output)

    def ollama_process_ram(self) -> int | None:
        output = self._output(TASKLIST_ARGS)
        return None if output is None else parse_tasklist(output)

    def free_ram(self) -> int | None:
        try:
            value = self._free_ram()
        except Exception:
            return None
        return value if type(value) is int and value >= 0 else None

    def snapshot(self, loaded: LoadedModel | None, oom: bool) -> ResourceSnapshot:
        gpu = self.gpu()
        free_vram = None if gpu is None else (gpu.total_mib - gpu.used_mib) / 1024
        free_ram = self.free_ram()
        return ResourceSnapshot(
            gpu_used_mib=None if gpu is None else gpu.used_mib,
            gpu_total_mib=None if gpu is None else gpu.total_mib,
            free_vram_gib=free_vram,
            free_ram_bytes=free_ram,
            free_ram_gib=None if free_ram is None else free_ram / _GIB,
            ollama_process_ram_bytes=self.ollama_process_ram(),
            ollama_ps_size_bytes=None if loaded is None else loaded.size,
            ollama_ps_size_vram_bytes=None if loaded is None else loaded.size_vram,
            ollama_ps_digest=None if loaded is None else loaded.digest,
            oom=oom,
        )


def resource_report(snapshot: ResourceSnapshot | None) -> ResourceReport | None:
    """The harness report: ``None`` (not measured, RESOURCES_UNREPORTED) unless both numbers exist.

    An OOM is reported even when the numbers are missing, as the harness expects.
    """
    if snapshot is None:
        return None
    if snapshot.oom:
        return ResourceReport(
            oom=True,
            min_free_vram_gib=snapshot.free_vram_gib if snapshot.free_vram_gib is not None else math.nan,
            min_free_ram_gib=snapshot.free_ram_gib if snapshot.free_ram_gib is not None else math.nan,
        )
    if snapshot.free_vram_gib is None or snapshot.free_ram_gib is None:
        return None
    return ResourceReport(oom=False, min_free_vram_gib=snapshot.free_vram_gib, min_free_ram_gib=snapshot.free_ram_gib)
