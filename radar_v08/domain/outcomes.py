"""Prospective outcome labels at 15m/1h/4h/24h (T041, ARCHITECTURE.md "Outcome measurement").

An outcome *subject* is one decision-time observation of one instrument: the pair and
venue, the deterministic direction, the entry mid and when it was observed, the sealed
evidence it was built from (T030), the decision or invocation that acted on it (T031),
the ablation arm, and the hypothetical round-trip cost scenario for each horizon (T040).
Every link that is not known is a typed ``LinkMissingReason``; nothing is inferred.

An outcome *label* is the market outcome of one subject at one horizon, written once the
horizon has matured. Pure: no I/O, no wall clock, no configuration. ``now`` and the quote
window tolerance are always passed in.

Maturity and availability
-------------------------

* ``target_at = decision_as_of + horizon``.
* The exit quote is the first valid quote observed in ``[target_at, target_at + tolerance]``
  (and not after ``now``). Only quotes at or after the target count: nothing from before
  the horizon is used, and nothing past the window is looked at.
* A valid exit quote makes the label ``AVAILABLE`` and ``label_available_at`` is the time
  that quote was observed: the earliest moment the outcome could have been known.
* No valid quote once the whole window has passed makes the label ``UNAVAILABLE`` with a
  typed reason (``QUOTE_MISSING_AT_HORIZON`` or ``QUOTE_INVALID_AT_HORIZON``) and
  ``label_available_at = target_at + tolerance``: the moment the missingness is final.
* An instrument with no price source gives ``PRICE_SOURCE_UNSUPPORTED`` at ``target_at``.
* Before that, the horizon is ``NotMature`` and nothing may be written.

Joins against labels use ``label_available_at <= decision_as_of`` of the *consumer*
(``visible_outcomes``, OPERATING_CONTRACTS.md §4), never the subject's own timestamp.

Values
------

Prices are mids, ``(bid + ask) / 2``, as ``decimal.Decimal``. ``market_return`` is
``exit_mid / entry_mid - 1`` (unsigned). ``gross_markout`` is the direction-signed
``market_return`` (LONG: +, SHORT: -); a subject with direction NONE has no gross or net
markout (``NO_DIRECTION``). ``net_markout = gross_markout - total_fraction`` of the
horizon's T040 cost scenario, only when that scenario is ``COST_COMPLETE``; otherwise the
net markout is unavailable with a reason (``COST_NOT_RECORDED`` / ``COST_INCOMPLETE`` /
``GROSS_UNAVAILABLE``). A missing value is never zero.

Arithmetic uses ``OUTCOME_CONTEXT`` (50 significant digits, ROUND_HALF_EVEN). The mid is
exact; the return division is the only step that can round, at the 50th digit. Invalid
operations, division by zero and overflow raise.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from enum import Enum

from radar_v08.domain.costs import CostScenario, CostStatus, Side
from radar_v08.domain.integrity import InstrumentId, InstrumentKind
from radar_v08.domain.invocation import Direction

OUTCOME_POLICY_VERSION = "OUTCOME-1"
SUBJECT_ID_PREFIX = "outcome:sha256:"
MAX_TEXT_LENGTH = 200

#: Arithmetic context of every outcome value (see module docstring).
OUTCOME_CONTEXT = Context(
    prec=50,
    rounding=ROUND_HALF_EVEN,
    Emin=-999_999,
    Emax=999_999,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)

_TWO = Decimal(2)
_ONE = Decimal(1)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OutcomeErrorCode(Enum):
    INVALID_FIELD = "invalid_field"
    NAIVE_TIMESTAMP = "naive_timestamp"
    NOT_DECIMAL = "not_decimal"
    OUT_OF_RANGE = "out_of_range"
    INCONSISTENT = "inconsistent"


class OutcomeInputError(ValueError):
    """An outcome input or label was refused at construction; ``code`` says why."""

    def __init__(self, code: OutcomeErrorCode, field: str, detail: str) -> None:
        super().__init__(f"{code.value}: {field}: {detail}")
        self.code = code
        self.field = field
        self.detail = detail


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, field, "must be text")
    if not value or value != value.strip() or len(value) > MAX_TEXT_LENGTH:
        raise OutcomeInputError(
            OutcomeErrorCode.INVALID_FIELD, field, f"must be 1..{MAX_TEXT_LENGTH} chars without edge whitespace"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, field, "contains control characters")
    return value


def _require_enum[E: Enum](value: object, enum_type: type[E], field: str) -> E:
    if not isinstance(value, enum_type):
        raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, field, f"must be {enum_type.__name__}")
    return value


def require_aware(moment: object, field: str) -> datetime:
    if not isinstance(moment, datetime):
        raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, field, "must be a datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise OutcomeInputError(OutcomeErrorCode.NAIVE_TIMESTAMP, field, "must be timezone-aware")
    return moment


def utc_text(moment: datetime) -> str:
    """Fixed-width UTC text (``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``): text order = time order."""
    return require_aware(moment, "moment").astimezone(UTC).isoformat(timespec="microseconds")


def _require_decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise OutcomeInputError(OutcomeErrorCode.NOT_DECIMAL, field, f"must be Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise OutcomeInputError(OutcomeErrorCode.OUT_OF_RANGE, field, "must be finite")
    if positive and value <= 0:
        raise OutcomeInputError(OutcomeErrorCode.OUT_OF_RANGE, field, "must be > 0")
    return value


def _exact_or_rounded(field: str, left: Decimal, op: str, right: Decimal) -> Decimal:
    try:
        with localcontext(OUTCOME_CONTEXT):
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "/":
                return left / right
            raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, field, f"unknown operation {op!r}")
    except (InvalidOperation, DivisionByZero, Overflow) as error:
        raise OutcomeInputError(OutcomeErrorCode.OUT_OF_RANGE, field, str(error)) from error


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Horizon(Enum):
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    H24 = "24h"

    @property
    def minutes(self) -> int:
        return _HORIZON_MINUTES[self]

    @property
    def duration(self) -> timedelta:
        return timedelta(minutes=_HORIZON_MINUTES[self])


_HORIZON_MINUTES = {Horizon.M15: 15, Horizon.H1: 60, Horizon.H4: 240, Horizon.H24: 1440}

#: Every horizon a subject is labelled at, shortest first.
HORIZONS: tuple[Horizon, ...] = (Horizon.M15, Horizon.H1, Horizon.H4, Horizon.H24)


class Availability(Enum):
    AVAILABLE = "AVAILABLE"  # an exit quote was observed inside the window
    UNAVAILABLE = "UNAVAILABLE"  # the window closed without one (reason on market_missing)


class PriceBasis(Enum):
    MID = "mid"


class MissingReason(Enum):
    """Why one outcome value is absent. Never read as zero."""

    QUOTE_MISSING_AT_HORIZON = "quote_missing_at_horizon"
    QUOTE_INVALID_AT_HORIZON = "quote_invalid_at_horizon"
    PRICE_SOURCE_UNSUPPORTED = "price_source_unsupported"
    NO_DIRECTION = "no_direction"
    GROSS_UNAVAILABLE = "gross_unavailable"
    COST_NOT_RECORDED = "cost_not_recorded"
    COST_INCOMPLETE = "cost_incomplete"


_MARKET_REASONS = frozenset(
    {
        MissingReason.QUOTE_MISSING_AT_HORIZON,
        MissingReason.QUOTE_INVALID_AT_HORIZON,
        MissingReason.PRICE_SOURCE_UNSUPPORTED,
    }
)
_GROSS_REASONS = _MARKET_REASONS | {MissingReason.NO_DIRECTION}
_NET_REASONS = frozenset(
    {MissingReason.GROSS_UNAVAILABLE, MissingReason.COST_NOT_RECORDED, MissingReason.COST_INCOMPLETE}
)


class LinkMissingReason(Enum):
    """Why a subject has no evidence, decision or arm link. Never inferred afterwards."""

    NOT_RECORDED = "not_recorded"  # the caller had no value at decision time
    LEGACY_UNVERSIONED = "legacy_unversioned"  # evidence only: the event has no sealed evidence (T030)


class DecisionKind(Enum):
    INVOCATION = "invocation"  # an invocation id of radar_v08.adapters.invocation_store (T031)
    DECISION = "decision"  # a deterministic decision id with no invocation behind it


_SIDE_OF = {Direction.LONG: Side.LONG, Direction.SHORT: Side.SHORT}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecisionRef:
    kind: DecisionKind
    ref_id: str

    def __post_init__(self) -> None:
        _require_enum(self.kind, DecisionKind, "DecisionRef.kind")
        _require_text(self.ref_id, "DecisionRef.ref_id")


@dataclass(frozen=True, slots=True)
class RecordedCost:
    """What the outcome keeps of a T040 ``CostScenario``: side, kind, status and total.

    ``total_fraction`` is set exactly when the status is ``COST_COMPLETE``; an incomplete
    scenario keeps no partial sum.
    """

    policy_version: str
    side: Side
    kind: InstrumentKind
    status: CostStatus
    total_fraction: Decimal | None

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "RecordedCost.policy_version")
        _require_enum(self.side, Side, "RecordedCost.side")
        _require_enum(self.kind, InstrumentKind, "RecordedCost.kind")
        _require_enum(self.status, CostStatus, "RecordedCost.status")
        if self.status is CostStatus.COMPLETE:
            _require_decimal(self.total_fraction, "RecordedCost.total_fraction")
        elif self.total_fraction is not None:
            raise OutcomeInputError(
                OutcomeErrorCode.INCONSISTENT, "RecordedCost.total_fraction", "an incomplete cost has no total"
            )

    @classmethod
    def from_scenario(cls, scenario: CostScenario) -> RecordedCost:
        if not isinstance(scenario, CostScenario):
            raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "scenario", "must be a T040 CostScenario")
        total = scenario.total_fraction if scenario.status is CostStatus.COMPLETE else None
        return cls(scenario.policy_version, scenario.side, scenario.instrument.kind, scenario.status, total)


@dataclass(frozen=True, slots=True)
class HorizonCost:
    horizon: Horizon
    cost: RecordedCost

    def __post_init__(self) -> None:
        _require_enum(self.horizon, Horizon, "HorizonCost.horizon")
        if not isinstance(self.cost, RecordedCost):
            raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "HorizonCost.cost", "must be RecordedCost")


def same_cost_for_every_horizon(scenario: CostScenario) -> tuple[HorizonCost, ...]:
    """One scenario declared for all four horizons (e.g. spot, where no funding accrues)."""
    recorded = RecordedCost.from_scenario(scenario)
    return tuple(HorizonCost(horizon, recorded) for horizon in HORIZONS)


def _link_json(value: str | LinkMissingReason | DecisionRef) -> object:
    if isinstance(value, LinkMissingReason):
        return {"missing": value.value}
    if isinstance(value, DecisionRef):
        return {"kind": value.kind.value, "id": value.ref_id}
    return value


@dataclass(frozen=True, slots=True)
class OutcomeSubject:
    """One decision-time observation to be labelled at every horizon.

    ``instrument`` is the T030 venue identity; ``pair`` is the native key of the price
    source (the Kraken spot pair key, e.g. ``XXBTZUSD``, or the futures symbol).
    """

    instrument: InstrumentId
    pair: str
    direction: Direction
    decision_as_of: datetime
    entry_mid: Decimal
    entry_observed_at: datetime
    evidence: str | LinkMissingReason
    decision: DecisionRef | LinkMissingReason
    arm: str | LinkMissingReason
    costs: tuple[HorizonCost, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, InstrumentId):
            raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "instrument", "must be InstrumentId")
        for name in ("venue", "symbol", "base", "quote", "size_unit"):
            _require_text(getattr(self.instrument, name), f"instrument.{name}")
        _require_enum(self.instrument.kind, InstrumentKind, "instrument.kind")
        _require_text(self.pair, "pair")
        _require_enum(self.direction, Direction, "direction")
        require_aware(self.decision_as_of, "decision_as_of")
        require_aware(self.entry_observed_at, "entry_observed_at")
        if self.entry_observed_at > self.decision_as_of:
            raise OutcomeInputError(
                OutcomeErrorCode.INCONSISTENT, "entry_observed_at", "the entry quote cannot come after the decision"
            )
        _require_decimal(self.entry_mid, "entry_mid", positive=True)
        if not isinstance(self.evidence, LinkMissingReason):
            _require_text(self.evidence, "evidence")
        if isinstance(self.decision, LinkMissingReason):
            if self.decision is not LinkMissingReason.NOT_RECORDED:
                raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "decision", "only NOT_RECORDED can be missing")
        elif not isinstance(self.decision, DecisionRef):
            raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "decision", "must be DecisionRef or a reason")
        if isinstance(self.arm, LinkMissingReason):
            if self.arm is not LinkMissingReason.NOT_RECORDED:
                raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "arm", "only NOT_RECORDED can be missing")
        else:
            _require_text(self.arm, "arm")
        if not isinstance(self.costs, tuple):
            raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "costs", "must be a tuple of HorizonCost")
        seen: set[Horizon] = set()
        for entry in self.costs:
            if not isinstance(entry, HorizonCost):
                raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "costs", "must be a tuple of HorizonCost")
            if entry.horizon in seen:
                raise OutcomeInputError(OutcomeErrorCode.INCONSISTENT, "costs", f"{entry.horizon.value} declared twice")
            seen.add(entry.horizon)
            side = _SIDE_OF.get(self.direction)
            if side is None:
                raise OutcomeInputError(
                    OutcomeErrorCode.INCONSISTENT, "costs", "a subject with direction NONE has no acted-on cost"
                )
            if entry.cost.side is not side or entry.cost.kind is not self.instrument.kind:
                raise OutcomeInputError(
                    OutcomeErrorCode.INCONSISTENT,
                    "costs",
                    f"{entry.horizon.value} scenario is {entry.cost.side.value}/{entry.cost.kind.value}, "
                    f"subject is {self.direction.value}/{self.instrument.kind.value}",
                )

    @property
    def subject_id(self) -> str:
        """Hash of the identity: instrument, pair, direction, decision time and the three links.

        Entry and costs are content, not identity: re-registering the same identity with a
        different entry or cost is a conflict, never a second subject.
        """
        identity = {
            "policy": OUTCOME_POLICY_VERSION,
            "venue": self.instrument.venue,
            "symbol": self.instrument.symbol,
            "kind": self.instrument.kind.value,
            "base": self.instrument.base,
            "quote": self.instrument.quote,
            "size_unit": self.instrument.size_unit,
            "pair": self.pair,
            "direction": self.direction.value,
            "decision_as_of": utc_text(self.decision_as_of),
            "evidence": _link_json(self.evidence),
            "decision": _link_json(self.decision),
            "arm": _link_json(self.arm),
        }
        text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return SUBJECT_ID_PREFIX + hashlib.sha256(text.encode("ascii")).hexdigest()

    def cost_for(self, horizon: Horizon) -> RecordedCost | None:
        for entry in self.costs:
            if entry.horizon is horizon:
                return entry.cost
        return None

    def target_at(self, horizon: Horizon) -> datetime:
        return self.decision_as_of + horizon.duration


@dataclass(frozen=True, slots=True)
class QuoteObservation:
    """One observed top of book. ``None`` bid/ask = not observed (the quote is invalid)."""

    observed_at: datetime
    bid: Decimal | None
    ask: Decimal | None
    source: str

    def __post_init__(self) -> None:
        require_aware(self.observed_at, "QuoteObservation.observed_at")
        for name in ("bid", "ask"):
            value = getattr(self, name)
            if value is not None:
                _require_decimal(value, f"QuoteObservation.{name}")
        _require_text(self.source, "QuoteObservation.source")

    @property
    def mid(self) -> Decimal | None:
        """``(bid + ask) / 2`` for a positive, uncrossed book; ``None`` otherwise."""
        bid, ask = self.bid, self.ask
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            return None
        return _exact_or_rounded("mid", _exact_or_rounded("mid", bid, "+", ask), "/", _TWO)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def _pair_ok(value: Decimal | None, reason: MissingReason | None, allowed: frozenset[MissingReason], field: str) -> None:
    if (value is None) == (reason is None):
        raise OutcomeInputError(OutcomeErrorCode.INCONSISTENT, field, "exactly one of value and reason must be set")
    if value is not None:
        _require_decimal(value, field)
    elif reason not in allowed:
        raise OutcomeInputError(OutcomeErrorCode.INCONSISTENT, field, f"{reason} is not a reason for this value")


@dataclass(frozen=True, slots=True)
class OutcomeLabel:
    """The outcome of one subject at one horizon, final once written."""

    subject_id: str
    horizon: Horizon
    status: Availability
    target_at: datetime
    label_available_at: datetime
    exit_mid: Decimal | None
    exit_observed_at: datetime | None
    exit_source: str | None
    market_return: Decimal | None
    market_missing: MissingReason | None
    gross_markout: Decimal | None
    gross_missing: MissingReason | None
    net_markout: Decimal | None
    net_missing: MissingReason | None
    cost_policy_version: str | None
    policy_version: str = OUTCOME_POLICY_VERSION
    price_basis: PriceBasis = PriceBasis.MID

    def __post_init__(self) -> None:
        if not _require_text(self.subject_id, "subject_id").startswith(SUBJECT_ID_PREFIX):
            raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "subject_id", "not an outcome subject id")
        _require_enum(self.horizon, Horizon, "horizon")
        _require_enum(self.status, Availability, "status")
        _require_enum(self.price_basis, PriceBasis, "price_basis")
        _require_text(self.policy_version, "policy_version")
        require_aware(self.target_at, "target_at")
        require_aware(self.label_available_at, "label_available_at")
        if self.label_available_at < self.target_at:
            raise OutcomeInputError(
                OutcomeErrorCode.INCONSISTENT, "label_available_at", "a label cannot be known before its horizon"
            )
        exit_fields = (self.exit_mid, self.exit_observed_at, self.exit_source)
        if self.status is Availability.AVAILABLE:
            if any(value is None for value in exit_fields) or self.market_return is None:
                raise OutcomeInputError(OutcomeErrorCode.INCONSISTENT, "exit", "an available label needs its exit quote")
            _require_decimal(self.exit_mid, "exit_mid", positive=True)
            if require_aware(self.exit_observed_at, "exit_observed_at") != self.label_available_at:
                raise OutcomeInputError(
                    OutcomeErrorCode.INCONSISTENT, "label_available_at", "must be the exit quote's observation time"
                )
            _require_text(self.exit_source, "exit_source")
        elif any(value is not None for value in exit_fields) or self.market_return is not None:
            raise OutcomeInputError(OutcomeErrorCode.INCONSISTENT, "exit", "an unavailable label has no exit quote")
        _pair_ok(self.market_return, self.market_missing, _MARKET_REASONS, "market_return")
        _pair_ok(self.gross_markout, self.gross_missing, _GROSS_REASONS, "gross_markout")
        _pair_ok(self.net_markout, self.net_missing, _NET_REASONS, "net_markout")
        if self.cost_policy_version is not None:
            _require_text(self.cost_policy_version, "cost_policy_version")

    @property
    def net_available(self) -> bool:
        return self.net_markout is not None


@dataclass(frozen=True, slots=True)
class NotMature:
    """The horizon cannot be labelled yet; nothing may be written before ``not_before``."""

    subject_id: str
    horizon: Horizon
    not_before: datetime


@dataclass(frozen=True, slots=True)
class LinkedOutcome:
    """A label with every link of its subject: pair, venue, direction, evidence, decision, arm."""

    subject: OutcomeSubject
    label: OutcomeLabel

    def __post_init__(self) -> None:
        if self.label.subject_id != self.subject.subject_id:
            raise OutcomeInputError(OutcomeErrorCode.INCONSISTENT, "label.subject_id", "label belongs to another subject")
        if self.label.target_at != self.subject.target_at(self.label.horizon):
            raise OutcomeInputError(OutcomeErrorCode.INCONSISTENT, "label.target_at", "not decision_as_of + horizon")


def _require_tolerance(tolerance: object) -> timedelta:
    if not isinstance(tolerance, timedelta) or tolerance < timedelta(0):
        raise OutcomeInputError(OutcomeErrorCode.OUT_OF_RANGE, "tolerance", "must be a non-negative timedelta")
    return tolerance


def label_horizon(
    subject: OutcomeSubject,
    horizon: Horizon,
    quotes: Sequence[QuoteObservation] | None,
    *,
    now: datetime,
    tolerance: timedelta,
) -> OutcomeLabel | NotMature:
    """Label ``subject`` at ``horizon`` as of ``now``, or say it is not mature yet.

    ``quotes`` are observations of the subject's own price source (any range; only the
    window is read). ``None`` means the instrument has no price source.
    """
    if not isinstance(subject, OutcomeSubject):
        raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "subject", "must be OutcomeSubject")
    _require_enum(horizon, Horizon, "horizon")
    require_aware(now, "now")
    window = _require_tolerance(tolerance)
    subject_id = subject.subject_id
    target = subject.target_at(horizon)
    if now < target:
        return NotMature(subject_id, horizon, target)

    exit_quote: QuoteObservation | None = None
    exit_mid: Decimal | None = None
    market_missing: MissingReason | None = None
    available_at = target
    if quotes is None:
        market_missing = MissingReason.PRICE_SOURCE_UNSUPPORTED
    else:
        if any(not isinstance(q, QuoteObservation) for q in quotes):
            raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "quotes", "must be QuoteObservation")
        window_end = target + window
        in_window = sorted(
            (q for q in quotes if target <= q.observed_at <= window_end and q.observed_at <= now),
            key=lambda q: (q.observed_at, q.source),
        )
        for quote in in_window:
            mid = quote.mid
            if mid is not None:
                exit_quote, exit_mid = quote, mid
                break
        if exit_quote is None:
            if now < window_end:
                return NotMature(subject_id, horizon, window_end)
            market_missing = (
                MissingReason.QUOTE_INVALID_AT_HORIZON if in_window else MissingReason.QUOTE_MISSING_AT_HORIZON
            )
            available_at = window_end
        else:
            available_at = exit_quote.observed_at

    market_return: Decimal | None = None
    gross: Decimal | None = None
    gross_missing: MissingReason | None = market_missing
    if exit_mid is not None:
        ratio = _exact_or_rounded("market_return", exit_mid, "/", subject.entry_mid)
        market_return = _exact_or_rounded("market_return", ratio, "-", _ONE)
        if subject.direction is Direction.LONG:
            gross = market_return
        elif subject.direction is Direction.SHORT:
            gross = -market_return
        else:
            gross_missing = MissingReason.NO_DIRECTION

    cost = subject.cost_for(horizon)
    net: Decimal | None = None
    net_missing: MissingReason | None
    if gross is None:
        net_missing = MissingReason.GROSS_UNAVAILABLE
    elif cost is None:
        net_missing = MissingReason.COST_NOT_RECORDED
    elif cost.status is not CostStatus.COMPLETE or cost.total_fraction is None:
        net_missing = MissingReason.COST_INCOMPLETE
    else:
        net = _exact_or_rounded("net_markout", gross, "-", cost.total_fraction)
        net_missing = None

    return OutcomeLabel(
        subject_id=subject_id,
        horizon=horizon,
        status=Availability.AVAILABLE if exit_quote is not None else Availability.UNAVAILABLE,
        target_at=target,
        label_available_at=available_at,
        exit_mid=exit_mid,
        exit_observed_at=None if exit_quote is None else exit_quote.observed_at,
        exit_source=None if exit_quote is None else exit_quote.source,
        market_return=market_return,
        market_missing=market_missing,
        gross_markout=gross,
        gross_missing=None if gross is not None else gross_missing,
        net_markout=net,
        net_missing=net_missing,
        cost_policy_version=None if cost is None else cost.policy_version,
    )


def visible_outcomes(outcomes: Iterable[LinkedOutcome], decision_as_of: datetime) -> tuple[LinkedOutcome, ...]:
    """Outcomes a decision at ``decision_as_of`` may see: ``label_available_at <= decision_as_of``.

    The subject's own decision time is not the test: a label about a past event is still
    future information until it matured.
    """
    cutoff = require_aware(decision_as_of, "decision_as_of")
    kept: list[LinkedOutcome] = []
    for outcome in outcomes:
        if not isinstance(outcome, LinkedOutcome):
            raise OutcomeInputError(OutcomeErrorCode.INVALID_FIELD, "outcomes", "must be LinkedOutcome")
        if outcome.label.label_available_at <= cutoff:
            kept.append(outcome)
    return tuple(kept)
