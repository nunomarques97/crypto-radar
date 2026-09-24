"""Immutable, versioned, hash-bound evidence identities (T030 domain part, ARCHITECTURE.md
"Integrity and evidence").

Sealed evidence binds a run, one venue instrument, the facts it contains and the OC-1
integrity report (``radar_v08.domain.integrity``) to a deterministic content hash. The
evidence ID and every fact ID are derived from that canonical hash, so an identity can
never be reused for different content. Pure: no I/O, no SQLite, no wall-clock reads.

Canonical JSON (the only input to the hash):

* object keys sorted, separators ``,``/``:``, ASCII-only output, no NaN/Infinity;
* ``None``, ``bool``, ``str`` and ``int`` (signed 64-bit range) map to JSON directly;
* ``float`` is always rejected (``NON_CANONICAL_NUMBER``): binary floats have no single
  decimal text, so callers must pass ``Decimal`` (or ``int``) instead;
* ``Decimal`` must be finite and is written as ``{"$decimal": "<text>"}`` where
  ``<text>`` is the exact value with trailing zeros removed and ``-0`` written ``0``
  (``1.50`` and ``1.5`` are the same number and hash the same; no context rounding);
* ``datetime`` must be timezone-aware and is written as
  ``{"$datetime": "YYYY-MM-DDTHH:MM:SS.ffffffZ"}`` in UTC (naive -> ``NAIVE_TIMESTAMP``);
* tuples and lists become arrays; mappings need ``str`` keys that do not start with
  ``$`` (reserved for the tags above); every other type is ``UNSUPPORTED_TYPE``.

Strings are hashed by exact code points (no Unicode normalisation).

The evidence schema version (``EVIDENCE_SCHEMA_VERSION``) describes this record layout
and is kept separate from ``code_version`` (the deployed code that produced the
evidence, docs/FAILURE_AND_QUALITY.md). A record without a schema version is an old row:
it is surfaced as ``LegacyUnversionedEvidence`` with only the fields the row really
has, never with invented facts, hashes or integrity results, and it cannot be cited.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum

from radar_v08.domain.integrity import (
    Capability,
    CapabilityResult,
    CheckStatus,
    InstrumentId,
    InstrumentKind,
    IntegrityReport,
    Reason,
    ReasonCode,
    TimeBasis,
)

type CanonicalValue = (
    None
    | bool
    | int
    | str
    | Decimal
    | datetime
    | tuple[CanonicalValue, ...]
    | list[CanonicalValue]
    | Mapping[str, CanonicalValue]
)
type JsonValue = None | bool | int | str | list[JsonValue] | dict[str, JsonValue]

EVIDENCE_SCHEMA_VERSION = "evidence-v1"
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({EVIDENCE_SCHEMA_VERSION})
HASH_PREFIX = "sha256:"
EVIDENCE_ID_PREFIX = "evidence:"
FACT_ID_PREFIX = "fact:"

_DECIMAL_TAG = "$decimal"
_DATETIME_TAG = "$datetime"
_INT_MIN = -(2**63)
_INT_MAX = 2**63 - 1


class RejectionCode(Enum):
    NON_CANONICAL_NUMBER = "non_canonical_number"
    NON_FINITE_NUMBER = "non_finite_number"
    INT_OUT_OF_RANGE = "int_out_of_range"
    NAIVE_TIMESTAMP = "naive_timestamp"
    UNSUPPORTED_TYPE = "unsupported_type"
    INVALID_KEY = "invalid_key"
    MALFORMED_RECORD = "malformed_record"
    INVALID_FIELD = "invalid_field"
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"
    INSTRUMENT_MISMATCH = "instrument_mismatch"
    RUN_MISMATCH = "run_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    UNKNOWN_DEPENDENCY = "unknown_dependency"
    DUPLICATE_FACT = "duplicate_fact"
    LEGACY_UNVERSIONED = "legacy_unversioned"


class EvidenceRejected(ValueError):
    """Typed rejection: ``code`` says why, ``field`` says where, ``detail`` gives context."""

    def __init__(self, code: RejectionCode, field: str, detail: str) -> None:
        super().__init__(f"{code.value} at {field}: {detail}")
        self.code = code
        self.field = field
        self.detail = detail


class EvidenceVersionState(Enum):
    SEALED = "sealed"
    LEGACY_UNVERSIONED = "legacy-unversioned"


class FactKind(Enum):
    OBSERVATION = "observation"
    CALCULATION = "calculation"


# --- canonical JSON ---------------------------------------------------------------------


def _decimal_text(value: Decimal, path: str) -> str:
    if not value.is_finite():
        raise EvidenceRejected(RejectionCode.NON_FINITE_NUMBER, path, f"decimal {value!s} is not finite")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # pragma: no cover - finite decimals have int exponents
        raise EvidenceRejected(RejectionCode.NON_FINITE_NUMBER, path, "decimal exponent is not finite")
    kept = list(digits)
    while len(kept) > 1 and kept[-1] == 0:
        kept.pop()
        exponent += 1
    if kept == [0]:
        return "0"
    # Built from the exact tuple: no arithmetic context, so no rounding of long values.
    return str(Decimal((sign, tuple(kept), exponent)))


def _require_aware(moment: datetime, path: str) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise EvidenceRejected(RejectionCode.NAIVE_TIMESTAMP, path, "timestamp has no timezone")


def _datetime_text(moment: datetime, path: str) -> str:
    _require_aware(moment, path)
    utc = moment.astimezone(UTC)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond:06d}Z"
    )


def _encode(value: object, path: str) -> JsonValue:
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, int):
        if not _INT_MIN <= value <= _INT_MAX:
            raise EvidenceRejected(RejectionCode.INT_OUT_OF_RANGE, path, "integer outside signed 64-bit range")
        return value
    if isinstance(value, float):
        raise EvidenceRejected(
            RejectionCode.NON_CANONICAL_NUMBER, path, "float has no canonical decimal text; pass Decimal or int"
        )
    if isinstance(value, Decimal):
        return {_DECIMAL_TAG: _decimal_text(value, path)}
    if isinstance(value, datetime):
        return {_DATETIME_TAG: _datetime_text(value, path)}
    if isinstance(value, (tuple, list)):
        return [_encode(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        encoded: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceRejected(RejectionCode.INVALID_KEY, path, f"key {key!r} is not a string")
            if key.startswith("$"):
                raise EvidenceRejected(RejectionCode.INVALID_KEY, path, f"key {key!r} uses the reserved '$' prefix")
            encoded[key] = _encode(item, f"{path}.{key}")
        return encoded
    raise EvidenceRejected(RejectionCode.UNSUPPORTED_TYPE, path, f"type {type(value).__name__} is not canonical")


def canonical_json(value: CanonicalValue) -> str:
    """The single canonical text of ``value``; raises ``EvidenceRejected`` when there is none."""
    return json.dumps(
        _encode(value, "$"), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def canonical_hash(value: CanonicalValue) -> str:
    """``sha256:<hex>`` of the canonical JSON bytes."""
    digest = hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def _reject_float(text: str) -> object:
    raise EvidenceRejected(RejectionCode.NON_CANONICAL_NUMBER, "$", f"JSON float {text} is not canonical")


def _reject_constant(text: str) -> object:
    raise EvidenceRejected(RejectionCode.NON_FINITE_NUMBER, "$", f"JSON constant {text} is not allowed")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in pairs:
        if key in result:
            raise EvidenceRejected(RejectionCode.INVALID_KEY, "$", f"duplicate key {key!r}")
        result[key] = item
    return result


def _decode(value: object, path: str) -> CanonicalValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, list):
        return tuple(_decode(item, f"{path}[{index}]") for index, item in enumerate(value))
    if isinstance(value, dict):
        tags = [key for key in value if isinstance(key, str) and key.startswith("$")]
        if tags:
            if len(value) != 1 or tags[0] not in (_DECIMAL_TAG, _DATETIME_TAG):
                raise EvidenceRejected(RejectionCode.INVALID_KEY, path, f"unknown or mixed tag {tags[0]!r}")
            text = value[tags[0]]
            if not isinstance(text, str):
                raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, path, "tagged value is not a string")
            return _decode_decimal(text, path) if tags[0] == _DECIMAL_TAG else _decode_datetime(text, path)
        decoded: dict[str, CanonicalValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):  # pragma: no cover - JSON object keys are strings
                raise EvidenceRejected(RejectionCode.INVALID_KEY, path, "non-string key")
            decoded[key] = _decode(item, f"{path}.{key}")
        return decoded
    raise EvidenceRejected(RejectionCode.UNSUPPORTED_TYPE, path, f"type {type(value).__name__} is not canonical")


def _decode_decimal(text: str, path: str) -> Decimal:
    try:
        number = Decimal(text)
    except InvalidOperation as error:
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, path, f"bad decimal {text!r}") from error
    if not number.is_finite():
        raise EvidenceRejected(RejectionCode.NON_FINITE_NUMBER, path, f"decimal {text} is not finite")
    return number


def _decode_datetime(text: str, path: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, path, f"bad timestamp {text!r}") from error
    _require_aware(moment, path)
    return moment.astimezone(UTC)


def parse_canonical_json(text: str) -> CanonicalValue:
    """Parse JSON into canonical values (arrays -> tuples, tags -> Decimal/datetime).

    JSON floats, NaN/Infinity and duplicate keys are rejected, never coerced.
    """
    try:
        raw: object = json.loads(
            text, parse_float=_reject_float, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs
        )
    except json.JSONDecodeError as error:
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, "$", f"not JSON: {error.msg}") from error
    return _decode(raw, "$")


# --- identities -------------------------------------------------------------------------


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceRejected(RejectionCode.INVALID_FIELD, field, "must be a non-empty string")
    return value


def _require_schema(version: object, field: str) -> str:
    text = _require_text(version, field)
    if text not in SUPPORTED_SCHEMA_VERSIONS:
        raise EvidenceRejected(RejectionCode.SCHEMA_VERSION_UNSUPPORTED, field, f"unsupported schema {text!r}")
    return text


def _require_instrument(instrument: object, field: str) -> InstrumentId:
    if not isinstance(instrument, InstrumentId):
        raise EvidenceRejected(RejectionCode.INVALID_FIELD, field, "must be an InstrumentId")
    if not isinstance(instrument.kind, InstrumentKind):
        raise EvidenceRejected(RejectionCode.INVALID_FIELD, f"{field}.kind", "must be an InstrumentKind")
    for name in ("venue", "symbol", "base", "quote", "size_unit"):
        _require_text(getattr(instrument, name), f"{field}.{name}")
    return instrument


def instrument_record(instrument: InstrumentId) -> dict[str, CanonicalValue]:
    return {
        "venue": instrument.venue,
        "symbol": instrument.symbol,
        "kind": instrument.kind.value,
        "base": instrument.base,
        "quote": instrument.quote,
        "size_unit": instrument.size_unit,
    }


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    """Who sealed the evidence: run, venue instrument, schema version and code version."""

    run_id: str
    instrument: InstrumentId
    code_version: str
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        _require_instrument(self.instrument, "instrument")
        _require_text(self.code_version, "code_version")
        _require_schema(self.schema_version, "schema_version")

    @property
    def venue(self) -> str:
        return self.instrument.venue


def _fact_content(
    schema_version: str,
    run_id: str,
    instrument: InstrumentId,
    kind: FactKind,
    name: str,
    payload: CanonicalValue,
    depends_on: tuple[str, ...],
    calculation_version: str | None,
) -> dict[str, CanonicalValue]:
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "instrument": instrument_record(instrument),
        "kind": kind.value,
        "name": name,
        "payload": payload,
        "depends_on": depends_on,
        "calculation_version": calculation_version,
    }


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    """One sealed observation or calculation. ``fact_id`` is derived from its full content.

    The payload is held as canonical JSON text, so the fact stays immutable. Dependencies
    are sorted, unique fact IDs; a fact ID covers them, so a dependency cycle cannot be
    built without breaking a hash.
    """

    fact_id: str
    schema_version: str
    run_id: str
    instrument: InstrumentId
    kind: FactKind
    name: str
    payload_json: str
    depends_on: tuple[str, ...]
    calculation_version: str | None

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, "fact.schema_version")
        _require_text(self.run_id, "fact.run_id")
        _require_instrument(self.instrument, "fact.instrument")
        if not isinstance(self.kind, FactKind):
            raise EvidenceRejected(RejectionCode.INVALID_FIELD, "fact.kind", "must be a FactKind")
        _require_text(self.name, "fact.name")
        if self.kind is FactKind.CALCULATION:
            _require_text(self.calculation_version, "fact.calculation_version")
        elif self.calculation_version is not None:
            raise EvidenceRejected(
                RejectionCode.INVALID_FIELD, "fact.calculation_version", "an observation has no calculation version"
            )
        if not isinstance(self.depends_on, tuple):
            raise EvidenceRejected(RejectionCode.INVALID_FIELD, "fact.depends_on", "must be a tuple")
        for index, dependency in enumerate(self.depends_on):
            _require_text(dependency, f"fact.depends_on[{index}]")
        if list(self.depends_on) != sorted(set(self.depends_on)):
            raise EvidenceRejected(RejectionCode.INVALID_FIELD, "fact.depends_on", "must be sorted and unique")
        payload = self.payload
        if canonical_json(payload) != self.payload_json:
            raise EvidenceRejected(RejectionCode.INVALID_FIELD, "fact.payload_json", "payload is not canonical")
        expected = FACT_ID_PREFIX + canonical_hash(self._content(payload))
        if self.fact_id != expected:
            raise EvidenceRejected(RejectionCode.HASH_MISMATCH, "fact.fact_id", f"{self.fact_id!r} != {expected!r}")

    @property
    def payload(self) -> CanonicalValue:
        return parse_canonical_json(self.payload_json)

    def _content(self, payload: CanonicalValue) -> dict[str, CanonicalValue]:
        return _fact_content(
            self.schema_version,
            self.run_id,
            self.instrument,
            self.kind,
            self.name,
            payload,
            self.depends_on,
            self.calculation_version,
        )


def make_fact(
    scope: EvidenceScope,
    kind: FactKind,
    name: str,
    payload: CanonicalValue,
    depends_on: Iterable[str] = (),
    calculation_version: str | None = None,
) -> EvidenceFact:
    """Build a fact bound to ``scope``'s run and instrument; the ID is its content hash."""
    dependencies = tuple(depends_on)
    if len(set(dependencies)) != len(dependencies):
        raise EvidenceRejected(RejectionCode.INVALID_FIELD, "fact.depends_on", "duplicate dependency")
    ordered = tuple(sorted(dependencies))
    payload_json = canonical_json(payload)
    content = _fact_content(
        scope.schema_version,
        scope.run_id,
        scope.instrument,
        kind,
        name,
        parse_canonical_json(payload_json),
        ordered,
        calculation_version,
    )
    return EvidenceFact(
        fact_id=FACT_ID_PREFIX + canonical_hash(content),
        schema_version=scope.schema_version,
        run_id=scope.run_id,
        instrument=scope.instrument,
        kind=kind,
        name=name,
        payload_json=payload_json,
        depends_on=ordered,
        calculation_version=calculation_version,
    )


def integrity_record(report: IntegrityReport) -> dict[str, CanonicalValue]:
    """Canonical form of an OC-1 report; result order is kept as evaluated."""
    results: list[CanonicalValue] = [
        {
            "capability": result.capability.value,
            "subject": result.subject,
            "status": result.status.value,
            "time_basis": result.time_basis.value,
            "received_at": result.received_at,
            "source_time": result.source_time,
            "reasons": tuple(
                {"code": reason.code.value, "field": reason.field, "detail": reason.detail}
                for reason in result.reasons
            ),
        }
        for result in report.results
    ]
    return {
        "evaluated_at": report.evaluated_at,
        "policy_version": report.policy_version,
        "results": results,
    }


def _fact_record(fact: EvidenceFact) -> dict[str, CanonicalValue]:
    record = fact._content(fact.payload)
    record["fact_id"] = fact.fact_id
    return record


def _evidence_content(
    scope: EvidenceScope, sealed_at: datetime, facts: tuple[EvidenceFact, ...], integrity: IntegrityReport
) -> dict[str, CanonicalValue]:
    return {
        "schema_version": scope.schema_version,
        "code_version": scope.code_version,
        "run_id": scope.run_id,
        "instrument": instrument_record(scope.instrument),
        "sealed_at": sealed_at,
        "facts": tuple(_fact_record(fact) for fact in facts),
        "integrity": integrity_record(integrity),
    }


def _validate_bindings(
    scope: object, sealed_at: object, facts: object, integrity: object
) -> None:
    """Every sealed-evidence invariant except the content hash, most specific first."""
    if not isinstance(scope, EvidenceScope):
        raise EvidenceRejected(RejectionCode.INVALID_FIELD, "scope", "must be an EvidenceScope")
    if not isinstance(sealed_at, datetime):
        raise EvidenceRejected(RejectionCode.INVALID_FIELD, "sealed_at", "must be a datetime")
    _require_aware(sealed_at, "sealed_at")
    if not isinstance(facts, tuple) or not facts:
        raise EvidenceRejected(RejectionCode.INVALID_FIELD, "facts", "must be a non-empty tuple")
    known: list[str] = []
    checked: list[EvidenceFact] = []
    for index, fact in enumerate(facts):
        field = f"facts[{index}]"
        if not isinstance(fact, EvidenceFact):
            raise EvidenceRejected(RejectionCode.INVALID_FIELD, field, "must be an EvidenceFact")
        if fact.schema_version != scope.schema_version:
            raise EvidenceRejected(
                RejectionCode.SCHEMA_VERSION_MISMATCH, field, f"{fact.schema_version} != {scope.schema_version}"
            )
        if fact.run_id != scope.run_id:
            raise EvidenceRejected(RejectionCode.RUN_MISMATCH, field, f"{fact.run_id!r} != {scope.run_id!r}")
        if fact.instrument != scope.instrument:
            raise EvidenceRejected(
                RejectionCode.INSTRUMENT_MISMATCH,
                field,
                f"{fact.instrument.venue}:{fact.instrument.symbol} != "
                f"{scope.instrument.venue}:{scope.instrument.symbol}",
            )
        if fact.fact_id in known:
            raise EvidenceRejected(RejectionCode.DUPLICATE_FACT, field, fact.fact_id)
        known.append(fact.fact_id)
        checked.append(fact)
    if known != sorted(known):
        raise EvidenceRejected(RejectionCode.INVALID_FIELD, "facts", "must be sorted by fact_id")
    sealed = set(known)
    for index, fact in enumerate(checked):
        for dependency in fact.depends_on:
            if dependency not in sealed:
                raise EvidenceRejected(
                    RejectionCode.UNKNOWN_DEPENDENCY, f"facts[{index}].depends_on", f"{dependency} is not sealed here"
                )
    if not isinstance(integrity, IntegrityReport):
        raise EvidenceRejected(RejectionCode.INVALID_FIELD, "integrity", "must be an IntegrityReport")
    _require_aware(integrity.evaluated_at, "integrity.evaluated_at")
    if integrity.evaluated_at > sealed_at:
        raise EvidenceRejected(
            RejectionCode.INVALID_FIELD, "integrity.evaluated_at", "integrity was evaluated after the seal"
        )
    symbol = scope.instrument.symbol
    if not any(result.subject == symbol for result in integrity.results):
        raise EvidenceRejected(
            RejectionCode.INSTRUMENT_MISMATCH, "integrity.results", f"no integrity result for {symbol!r}"
        )


@dataclass(frozen=True, slots=True)
class SealedEvidence:
    """A sealed, immutable evidence version. Every invariant is checked on construction."""

    evidence_id: str
    content_hash: str
    scope: EvidenceScope
    sealed_at: datetime
    facts: tuple[EvidenceFact, ...]
    integrity: IntegrityReport

    def __post_init__(self) -> None:
        _validate_bindings(self.scope, self.sealed_at, self.facts, self.integrity)
        expected = canonical_hash(_evidence_content(self.scope, self.sealed_at, self.facts, self.integrity))
        if self.content_hash != expected:
            raise EvidenceRejected(RejectionCode.HASH_MISMATCH, "content_hash", f"{self.content_hash!r} != {expected!r}")
        if self.evidence_id != EVIDENCE_ID_PREFIX + expected:
            raise EvidenceRejected(RejectionCode.HASH_MISMATCH, "evidence_id", "evidence_id is not bound to content_hash")

    @property
    def state(self) -> EvidenceVersionState:
        return EvidenceVersionState.SEALED

    @property
    def schema_version(self) -> str:
        return self.scope.schema_version

    @property
    def code_version(self) -> str:
        return self.scope.code_version

    @property
    def run_id(self) -> str:
        return self.scope.run_id

    @property
    def instrument(self) -> InstrumentId:
        return self.scope.instrument

    @property
    def venue(self) -> str:
        return self.scope.instrument.venue

    @property
    def fact_ids(self) -> tuple[str, ...]:
        return tuple(fact.fact_id for fact in self.facts)


@dataclass(frozen=True, slots=True)
class LegacyUnversionedEvidence:
    """An old row with no evidence schema version.

    It keeps only what the row really holds: an opaque source reference, the run and
    symbol text if present, and the names of the fields it had. It has no facts, hash or
    integrity result, and ``resolve_fact`` refuses to cite it.
    """

    source_ref: str
    run_id: str | None = None
    symbol: str | None = None
    fields_present: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.source_ref, "source_ref")

    @property
    def state(self) -> EvidenceVersionState:
        return EvidenceVersionState.LEGACY_UNVERSIONED


type EvidenceRecord = SealedEvidence | LegacyUnversionedEvidence


def seal_evidence(
    scope: EvidenceScope, facts: Iterable[EvidenceFact], integrity: IntegrityReport, sealed_at: datetime
) -> SealedEvidence:
    """Seal facts plus their integrity report. Fact order does not change the hash."""
    given = tuple(facts)
    for index, fact in enumerate(given):
        if not isinstance(fact, EvidenceFact):
            raise EvidenceRejected(RejectionCode.INVALID_FIELD, f"facts[{index}]", "must be an EvidenceFact")
    ordered = tuple(sorted(given, key=lambda fact: fact.fact_id))
    _validate_bindings(scope, sealed_at, ordered, integrity)
    content_hash = canonical_hash(_evidence_content(scope, sealed_at, ordered, integrity))
    return SealedEvidence(
        evidence_id=EVIDENCE_ID_PREFIX + content_hash,
        content_hash=content_hash,
        scope=scope,
        sealed_at=sealed_at,
        facts=ordered,
        integrity=integrity,
    )


def resolve_fact(
    record: EvidenceRecord, fact_id: str, *, evidence_id: str, run_id: str, instrument: InstrumentId
) -> EvidenceFact:
    """Resolve a cited fact, checking evidence hash, run and instrument before existence."""
    if isinstance(record, LegacyUnversionedEvidence):
        raise EvidenceRejected(
            RejectionCode.LEGACY_UNVERSIONED, "evidence", f"{record.source_ref} has no sealed facts to cite"
        )
    if record.evidence_id != evidence_id:
        raise EvidenceRejected(RejectionCode.HASH_MISMATCH, "evidence_id", f"{evidence_id!r} != {record.evidence_id!r}")
    if record.run_id != run_id:
        raise EvidenceRejected(RejectionCode.RUN_MISMATCH, "run_id", f"{run_id!r} != {record.run_id!r}")
    if record.instrument != instrument:
        raise EvidenceRejected(RejectionCode.INSTRUMENT_MISMATCH, "instrument", f"{instrument.symbol!r} differs")
    for fact in record.facts:
        if fact.fact_id == fact_id:
            return fact
    raise EvidenceRejected(RejectionCode.UNKNOWN_DEPENDENCY, "fact_id", f"{fact_id} is not in {record.evidence_id}")


# --- record round trip ------------------------------------------------------------------


def evidence_to_record(evidence: SealedEvidence) -> dict[str, CanonicalValue]:
    record = _evidence_content(evidence.scope, evidence.sealed_at, evidence.facts, evidence.integrity)
    record["content_hash"] = evidence.content_hash
    record["evidence_id"] = evidence.evidence_id
    return record


def evidence_to_json(evidence: SealedEvidence) -> str:
    return canonical_json(evidence_to_record(evidence))


_EVIDENCE_KEYS = frozenset(
    {"schema_version", "code_version", "run_id", "instrument", "sealed_at", "facts", "integrity"}
    | {"content_hash", "evidence_id"}
)
_FACT_KEYS = frozenset(
    {"fact_id", "schema_version", "run_id", "instrument", "kind", "name", "payload", "depends_on"}
    | {"calculation_version"}
)
_INSTRUMENT_KEYS = frozenset({"venue", "symbol", "kind", "base", "quote", "size_unit"})
_INTEGRITY_KEYS = frozenset({"evaluated_at", "policy_version", "results"})
_RESULT_KEYS = frozenset({"capability", "subject", "status", "time_basis", "received_at", "source_time", "reasons"})
_REASON_KEYS = frozenset({"code", "field", "detail"})


def _mapping(value: object, keys: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, field, "must be an object")
    present = {key for key in value if isinstance(key, str)}
    if present != keys or len(present) != len(value):
        missing = sorted(keys - present)
        extra = sorted(str(key) for key in value if key not in keys)
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, field, f"missing {missing}, unexpected {extra}")
    return value


def _sequence(value: object, field: str) -> tuple[object, ...]:
    if not isinstance(value, (tuple, list)):
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, field, "must be an array")
    return tuple(value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, field, "must be a string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _moment(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, field, "must be a tagged timestamp")
    _require_aware(value, field)
    return value


def _optional_moment(value: object, field: str) -> datetime | None:
    return None if value is None else _moment(value, field)


def _enum_value[E: Enum](enum_type: type[E], value: object, field: str) -> E:
    try:
        return enum_type(_text(value, field))
    except ValueError as error:
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, field, f"unknown {enum_type.__name__} {value!r}") from error


def _instrument_from(value: object, field: str) -> InstrumentId:
    data = _mapping(value, _INSTRUMENT_KEYS, field)
    return InstrumentId(
        venue=_text(data["venue"], f"{field}.venue"),
        symbol=_text(data["symbol"], f"{field}.symbol"),
        kind=_enum_value(InstrumentKind, data["kind"], f"{field}.kind"),
        base=_text(data["base"], f"{field}.base"),
        quote=_text(data["quote"], f"{field}.quote"),
        size_unit=_text(data["size_unit"], f"{field}.size_unit"),
    )


def _fact_from(value: object, field: str) -> EvidenceFact:
    data = _mapping(value, _FACT_KEYS, field)
    payload = data["payload"]
    return EvidenceFact(
        fact_id=_text(data["fact_id"], f"{field}.fact_id"),
        schema_version=_text(data["schema_version"], f"{field}.schema_version"),
        run_id=_text(data["run_id"], f"{field}.run_id"),
        instrument=_instrument_from(data["instrument"], f"{field}.instrument"),
        kind=_enum_value(FactKind, data["kind"], f"{field}.kind"),
        name=_text(data["name"], f"{field}.name"),
        payload_json=canonical_json(_as_canonical(payload, f"{field}.payload")),
        depends_on=tuple(
            _text(item, f"{field}.depends_on[{index}]")
            for index, item in enumerate(_sequence(data["depends_on"], f"{field}.depends_on"))
        ),
        calculation_version=_optional_text(data["calculation_version"], f"{field}.calculation_version"),
    )


def _as_canonical(value: object, field: str) -> CanonicalValue:
    # Re-validates an already decoded value; _encode rejects anything non-canonical.
    return _decode(_encode(value, field), field)


def _reason_from(value: object, field: str) -> Reason:
    data = _mapping(value, _REASON_KEYS, field)
    return Reason(
        code=_enum_value(ReasonCode, data["code"], f"{field}.code"),
        field=_text(data["field"], f"{field}.field"),
        detail=_text(data["detail"], f"{field}.detail"),
    )


def _result_from(value: object, field: str) -> CapabilityResult:
    data = _mapping(value, _RESULT_KEYS, field)
    try:
        return CapabilityResult(
            capability=_enum_value(Capability, data["capability"], f"{field}.capability"),
            subject=_text(data["subject"], f"{field}.subject"),
            status=_enum_value(CheckStatus, data["status"], f"{field}.status"),
            reasons=tuple(
                _reason_from(item, f"{field}.reasons[{index}]")
                for index, item in enumerate(_sequence(data["reasons"], f"{field}.reasons"))
            ),
            time_basis=_enum_value(TimeBasis, data["time_basis"], f"{field}.time_basis"),
            received_at=_optional_moment(data["received_at"], f"{field}.received_at"),
            source_time=_optional_moment(data["source_time"], f"{field}.source_time"),
        )
    except EvidenceRejected:
        raise
    except ValueError as error:
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, field, str(error)) from error


def _integrity_from(value: object, field: str) -> IntegrityReport:
    data = _mapping(value, _INTEGRITY_KEYS, field)
    return IntegrityReport(
        evaluated_at=_moment(data["evaluated_at"], f"{field}.evaluated_at"),
        policy_version=_text(data["policy_version"], f"{field}.policy_version"),
        results=tuple(
            _result_from(item, f"{field}.results[{index}]")
            for index, item in enumerate(_sequence(data["results"], f"{field}.results"))
        ),
    )


def evidence_from_record(record: Mapping[str, object], *, source_ref: str) -> EvidenceRecord:
    """Rebuild evidence from a decoded record, verifying every hash and binding.

    A record without ``schema_version`` (or with ``None``) is legacy-unversioned; a record
    with an unknown schema version is rejected rather than guessed.
    """
    if not isinstance(record, Mapping):
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, "$", "must be an object")
    if record.get("schema_version") is None:
        run_id = record.get("run_id")
        symbol = record.get("symbol")
        return LegacyUnversionedEvidence(
            source_ref=source_ref,
            run_id=run_id if isinstance(run_id, str) and run_id else None,
            symbol=symbol if isinstance(symbol, str) and symbol else None,
            fields_present=tuple(sorted(str(key) for key in record)),
        )
    _require_schema(record["schema_version"], "schema_version")
    data = _mapping(record, _EVIDENCE_KEYS, "$")
    scope = EvidenceScope(
        run_id=_text(data["run_id"], "run_id"),
        instrument=_instrument_from(data["instrument"], "instrument"),
        code_version=_text(data["code_version"], "code_version"),
        schema_version=_text(data["schema_version"], "schema_version"),
    )
    return SealedEvidence(
        evidence_id=_text(data["evidence_id"], "evidence_id"),
        content_hash=_text(data["content_hash"], "content_hash"),
        scope=scope,
        sealed_at=_moment(data["sealed_at"], "sealed_at"),
        facts=tuple(
            _fact_from(item, f"facts[{index}]") for index, item in enumerate(_sequence(data["facts"], "facts"))
        ),
        integrity=_integrity_from(data["integrity"], "integrity"),
    )


def evidence_from_json(text: str, *, source_ref: str) -> EvidenceRecord:
    """Parse canonical JSON produced by ``evidence_to_json`` (or a legacy JSON row)."""
    decoded = parse_canonical_json(text)
    if not isinstance(decoded, Mapping):
        raise EvidenceRejected(RejectionCode.MALFORMED_RECORD, "$", "must be an object")
    return evidence_from_record(decoded, source_ref=source_ref)
