"""Pre-registration registry of the trend research.

A port of the feasibility harness registry (``harness/registry.py``): an append-only JSON-lines log
where every line carries ``seq`` (its 0-based position), ``ts``, ``prev`` (the sha256 of the previous
line's exact bytes without its terminator, ``"genesis"`` for the first line) and the event data.

Events:

* ``prior_trials``: strategies tested before the harness existed; ``count`` adds to the trial count.
* ``register``: a rule (name, source sha256, rule text, parameters, pass criteria) declared before
  any run. A name and a source hash can each be registered only once.
* ``run``: a development-split evaluation with its logged summary metrics.
* ``holdout``: the single allowed holdout evaluation of a name, recorded *before* it computes, so a
  crash still consumes it.
* ``holdout_result``: the metrics of that holdout evaluation.

This module is pure: it parses and checks bytes and builds the next line; the file I/O is in
``radar_v08.adapters.trend_registry_store``. Its parser verifies the hash chain over the exact line
bytes (LF or CRLF terminators; the imported historical registry uses CRLF) and refuses an edited,
removed, reordered, blank or torn line. Nothing chains to the last line, so an edit or removal of
only the last line is invisible to the chain itself; the imported registry is therefore also pinned
by the sha256 of its whole file (``IMPORTED_REGISTRY_SHA256``). Trial count = registrations + declared prior trials. The
Deflated Sharpe Ratio of a logged event is recomputed from the registry prefix in force when it
was written.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from .trend_metrics import Performance, deflated_sharpe, trial_sharpe_variance

GENESIS = "genesis"
IMPORTED_REGISTRY_SHA256 = "6f4dbaba82e628a97911a93381a694ffc4dbc4059e064a0647564c77232e5f4a"
EVENT_KINDS = frozenset({"prior_trials", "register", "run", "holdout", "holdout_result"})
CHAIN_KEYS = frozenset({"seq", "ts", "prev"})
SPLITS = frozenset({"dev", "holdout"})
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_ANNUALIZATION_DAYS = 365


class RegistryErrorCode(StrEnum):
    MALFORMED = "MALFORMED"
    CHAIN_BROKEN = "CHAIN_BROKEN"
    UNKNOWN_EVENT = "UNKNOWN_EVENT"
    INVALID_EVENT = "INVALID_EVENT"
    DUPLICATE_NAME = "DUPLICATE_NAME"
    DUPLICATE_HASH = "DUPLICATE_HASH"
    NOT_REGISTERED = "NOT_REGISTERED"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    HOLDOUT_CONSUMED = "HOLDOUT_CONSUMED"


class RegistryError(Exception):
    def __init__(self, code: RegistryErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value + (f": {detail}" if detail else ""))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class RegistryEvent:
    """One verified line: its position, kind, the sha256 of its bytes and its parsed content."""

    seq: int
    kind: str
    line_sha256: str
    payload: Mapping[str, object]

    def text(self, key: str) -> str:
        value = self.payload.get(key)
        if not isinstance(value, str):
            raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"seq {self.seq}: {key!r} is not a string")
        return value

    @property
    def name(self) -> str:
        return self.text("name")

    @property
    def sha256(self) -> str:
        return self.text("sha256")


@dataclass(frozen=True)
class Registry:
    """The verified events of a registry file and the hash its next line must chain to."""

    events: tuple[RegistryEvent, ...]
    tip: str

    def registrations(self) -> tuple[RegistryEvent, ...]:
        return tuple(ev for ev in self.events if ev.kind == "register")

    def registered_names(self) -> tuple[str, ...]:
        return tuple(ev.name for ev in self.registrations())

    def registration_for(self, name: str) -> RegistryEvent | None:
        for ev in self.registrations():
            if ev.name == name:
                return ev
        return None

    def holdout_used(self, name: str) -> bool:
        return holdout_used(self.events, name)

    def trial_count(self) -> int:
        return trial_count(self.events)


# ---------------------------------------------------------------------------
# Parsing and encoding
# ---------------------------------------------------------------------------


def _check_shape(seq: int, obj: Mapping[str, object]) -> str:
    kind = obj.get("event")
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        raise RegistryError(RegistryErrorCode.UNKNOWN_EVENT, f"seq {seq}: event {kind!r}")
    if not isinstance(obj.get("ts"), str):
        raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"seq {seq}: missing ts")
    if kind == "prior_trials":
        count = obj.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"seq {seq}: prior_trials count {count!r}")
        return kind
    name, digest = obj.get("name"), obj.get("sha256")
    if not isinstance(name, str) or not name:
        raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"seq {seq}: missing name")
    if not isinstance(digest, str) or not _SHA256.match(digest):
        raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"seq {seq}: invalid sha256 {digest!r}")
    if kind in ("run", "holdout_result"):
        if obj.get("split") not in SPLITS:
            raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"seq {seq}: split {obj.get('split')!r}")
        if not isinstance(obj.get("summary"), dict) or not isinstance(obj.get("daily_sharpe"), dict):
            raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"seq {seq}: missing summary or daily_sharpe")
    return kind


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _freeze_mapping(value: Mapping[object, object]) -> Mapping[str, object]:
    return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})


def parse_registry(data: bytes) -> Registry:
    """Verify the hash chain over the exact line bytes and return the events.

    Every line must end with LF or CRLF; a blank, undecodable, non-object, out-of-sequence or
    wrongly chained line (an edited, removed or reordered line) is refused, as is a torn last line.
    """
    if not data:
        return Registry((), GENESIS)
    if not data.endswith(b"\n"):
        raise RegistryError(RegistryErrorCode.MALFORMED, "the last line has no terminator (torn write)")
    events: list[RegistryEvent] = []
    prev = GENESIS
    for n, raw in enumerate(data.split(b"\n")[:-1], 1):
        line = raw[:-1] if raw.endswith(b"\r") else raw
        if not line.strip():
            raise RegistryError(RegistryErrorCode.MALFORMED, f"line {n} is blank")
        if b"\r" in line:
            raise RegistryError(RegistryErrorCode.MALFORMED, f"line {n} has a stray carriage return")
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise RegistryError(RegistryErrorCode.MALFORMED, f"line {n} is not JSON: {error}") from error
        if not isinstance(obj, dict):
            raise RegistryError(RegistryErrorCode.MALFORMED, f"line {n} is not a JSON object")
        if obj.get("prev") != prev or obj.get("seq") != len(events) or isinstance(obj.get("seq"), bool):
            raise RegistryError(RegistryErrorCode.CHAIN_BROKEN, f"chain broken at line {n}: the file was edited")
        kind = _check_shape(len(events), obj)
        digest = sha256_hex(line)
        events.append(RegistryEvent(len(events), kind, digest, _freeze_mapping(obj)))
        prev = digest
    return Registry(tuple(events), prev)


def encode_event(registry: Registry, ts: str, payload: Mapping[str, object]) -> bytes:
    """The next line (LF-terminated) for ``payload``, chained to ``registry``."""
    reserved = CHAIN_KEYS & set(payload)
    if reserved:
        raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"payload may not set {sorted(reserved)}")
    event = {"seq": len(registry.events), "ts": ts, "prev": registry.tip, **payload}
    _check_shape(len(registry.events), event)
    try:
        text = json.dumps(event, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"payload is not plain JSON: {error}") from error
    return text.encode("utf-8") + b"\n"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def holdout_used(events: Sequence[RegistryEvent], name: str) -> bool:
    return any(ev.kind == "holdout" and ev.name == name for ev in events)


def trial_count(events: Sequence[RegistryEvent]) -> int:
    """Strategies ever tested: every registration plus the declared prior (pre-harness) trials."""
    regs = sum(ev.kind == "register" for ev in events)
    prior = 0
    for ev in events:
        if ev.kind == "prior_trials":
            count = ev.payload.get("count")
            if isinstance(count, bool) or not isinstance(count, int):
                raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"seq {ev.seq}: prior_trials count {count!r}")
            prior += count
    return regs + prior


def registration_payload(
    registry: Registry,
    *,
    name: str,
    source_sha256: str,
    file: str,
    rule: str,
    params: Mapping[str, object],
    pass_criteria: Sequence[Mapping[str, object]],
    universe: Sequence[str],
    is_benchmark: bool = False,
) -> dict[str, object]:
    """A ``register`` event; refuses a name or a source hash that is already registered."""
    for ev in registry.registrations():
        if ev.name == name:
            raise RegistryError(
                RegistryErrorCode.DUPLICATE_NAME, f"{name!r} is already registered; a changed rule needs a new name"
            )
        if ev.sha256 == source_sha256:
            raise RegistryError(RegistryErrorCode.DUPLICATE_HASH, f"identical source already registered as {ev.name!r}")
    return {
        "event": "register",
        "name": name,
        "file": file,
        "sha256": source_sha256,
        "rule": rule,
        "params": dict(params),
        "pass_criteria": [dict(c) for c in pass_criteria],
        "universe": list(universe),
        "is_benchmark": is_benchmark,
    }


def verify_source(registry: Registry, name: str, source_sha256: str) -> RegistryEvent:
    """The registration of ``name``; refuses an unregistered name or a source changed since."""
    registration = registry.registration_for(name)
    if registration is None:
        raise RegistryError(RegistryErrorCode.NOT_REGISTERED, f"{name!r} is not registered")
    if registration.sha256 != source_sha256:
        raise RegistryError(
            RegistryErrorCode.SOURCE_CHANGED, f"{name!r} source changed since registration; refusing to run"
        )
    return registration


def holdout_payload(registry: Registry, name: str, source_sha256: str, slippage_bps: float) -> dict[str, object]:
    """The ``holdout`` event that must be recorded before a holdout computes; once per name, ever."""
    registration = verify_source(registry, name, source_sha256)
    if registry.holdout_used(name):
        raise RegistryError(RegistryErrorCode.HOLDOUT_CONSUMED, f"holdout already used for {name!r}; it runs only once")
    return {"event": "holdout", "name": name, "sha256": registration.sha256, "slippage_bps": slippage_bps}


def prior_trials_payload(count: int, note: str) -> dict[str, object]:
    return {"event": "prior_trials", "count": count, "note": note}


# ---------------------------------------------------------------------------
# Deflated Sharpe from the registry
# ---------------------------------------------------------------------------


def fee_key(fee: float) -> str:
    """The key the harness used for a fee scenario (``f"{fee}"``, e.g. ``"0.001"``)."""
    return f"{fee}"


def dev_sharpes(events: Sequence[RegistryEvent], fee: float) -> dict[str, float]:
    """Latest logged development daily Sharpe per name at ``fee``, in first-logged order."""
    key = fee_key(fee)
    latest: dict[str, float] = {}
    for ev in events:
        if ev.kind != "run" or ev.payload.get("split") != "dev":
            continue
        sharpes = ev.payload.get("daily_sharpe")
        if not isinstance(sharpes, Mapping):
            raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"seq {ev.seq}: daily_sharpe is not an object")
        value = sharpes.get(key)
        if isinstance(value, float | int) and not isinstance(value, bool):
            latest[ev.name] = float(value)
    return latest


@dataclass(frozen=True)
class DsrResult:
    dsr: float
    hurdle_sharpe_annual: float
    n_trials: int
    n_trial_sharpes: int
    variance: float


def deflated_sharpe_at(
    events: Sequence[RegistryEvent], name: str, split: str, fee: float, perf: Performance, *, floor: bool = True
) -> DsrResult:
    """DSR of a result evaluated with ``events`` as the registry in force.

    For a development run the result's own Sharpe joins the logged ones (replacing its earlier
    value); a holdout uses only the logged development Sharpes. ``floor=False`` reproduces the
    events logged before the harness adopted the 1/T variance floor.
    """
    if split not in SPLITS:
        raise RegistryError(RegistryErrorCode.INVALID_EVENT, f"split {split!r}")
    n_trials = trial_count(events)
    sharpes = dev_sharpes(events, fee)
    if split == "dev":
        sharpes[name] = perf.daily_sharpe
    variance = trial_sharpe_variance(list(sharpes.values()), perf.days, floor=floor)
    dsr, sr0 = deflated_sharpe(perf.daily_sharpe, perf.days, perf.skew, perf.kurtosis, n_trials, variance)
    return DsrResult(dsr, sr0 * math.sqrt(_ANNUALIZATION_DAYS), n_trials, len(sharpes), variance)
