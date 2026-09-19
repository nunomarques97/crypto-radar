"""Exact, itemised round-trip cost scenarios (T040, RISK.md "Current defects").

A cost scenario prices one hypothetical round trip - an ENTRY leg and an EXIT leg - of a
declared size on one instrument, for one side (LONG or SHORT), at an unchanged reference
mid. It is a cost amplitude, never a return forecast, never a position size and never an
account figure. Pure: no I/O, no wall clock, no configuration, no adapter imports.

Numbers
-------

* Every rate and amount is a ``decimal.Decimal``. ``float``, ``bool``, ``int`` and
  non-finite Decimals are rejected at runtime at this boundary (``CostInputError``);
  callers holding legacy float measurements convert them explicitly and say so in the
  component's ``source``.
* Arithmetic runs in ``COST_CONTEXT``: 200 significant digits, ROUND_HALF_EVEN, and the
  ``Inexact`` signal trapped. Every intermediate result is therefore exact or the
  computation fails with ``PRECISION_EXCEEDED``; nothing is rounded silently. No
  division is needed: spreads and rates are basis points (divided by powers of ten).
* Presentation rounding is explicit and separate (``present``): ROUND_CEILING, i.e.
  toward more cost, so a rounded figure never understates a cost.

Units
-----

Every line is a fraction of the scenario's reference notional (``fraction``; ``bps`` is
the same value times 10,000). Money exists only when the instrument's quote currency is
identified: amounts are in that quote currency, and a report currency different from it
needs an explicit ``FxRate`` (from quote to report). Instrument, currency, side, size and
the spread convention travel with the scenario.

Spread convention (no double counting)
--------------------------------------

``SpreadConvention.HALF_SPREAD_PLUS_TOUCH_SLIPPAGE``: each taker leg pays half the quoted
spread (mid to touch) plus the slippage measured from the touch (best ask for a buy, best
bid for a sell) at the scenario size. A buy fills at ``mid*(1+h)*(1+s_buy)`` and a sell at
``mid*(1-h)*(1-s_sell)`` with ``h = spread/2``.

``SpreadConvention.SLIPPAGE_FROM_MID``: the slippage was measured from the mid and already
contains the half spread, so no spread line exists; supplying a spread under this
convention raises ``DOUBLE_COUNTED_SPREAD``.

Two legs, both directions
-------------------------

LONG enters with a BUY and exits with a SELL; SHORT enters with a SELL and exits with a
BUY. Each leg uses the slippage of its own direction - never the maximum of the two.
Fees are charged per leg on that leg's fill notional. Funding (futures only) is charged
on the reference notional for the declared number of funding intervals; a positive rate
means LONG pays and SHORT receives. Spot has no funding line; zero declared intervals
means no funding line either.

Missing is never zero
---------------------

A component that is not known is ``Missing`` with a reason. Any missing component makes
the scenario ``COST_INCOMPLETE``: it lists the known lines and the missing ones and has
no total, no money and no cashflow, so a partial sum can never be read as the cost of the
round trip (or as a net edge). Slippage measured on a book that did not cover the size is
missing (``SIZE_NOT_COVERED``). Fees can only be declared as an uncalibrated assumption or
as a published schedule; this module has no way to call a fee account-calibrated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Underflow,
    localcontext,
)
from enum import Enum

from radar_v08.domain.integrity import InstrumentKind

COST_POLICY_VERSION = "COST-1"

#: Arithmetic context of every cost computation. ``Inexact`` is trapped: a result that
#: would need rounding raises instead of being rounded (see module docstring).
COST_CONTEXT = Context(
    prec=200,
    rounding=ROUND_HALF_EVEN,
    Emin=-999_999,
    Emax=999_999,
    traps=[InvalidOperation, DivisionByZero, Overflow, Underflow, Inexact],
)

#: Presentation rounding: toward +infinity, so a presented cost is never understated.
PRESENTATION_ROUNDING = ROUND_CEILING

_BPS = Decimal(10_000)
_ONE = Decimal(1)
_TWO = Decimal(2)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CostErrorCode(Enum):
    NOT_DECIMAL = "not_decimal"
    NON_FINITE = "non_finite"
    OUT_OF_RANGE = "out_of_range"
    EMPTY_FIELD = "empty_field"
    DOUBLE_COUNTED_SPREAD = "double_counted_spread"
    CONVENTION_MISMATCH = "convention_mismatch"
    SIZE_MISMATCH = "size_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    FUNDING_NOT_APPLICABLE = "funding_not_applicable"
    PRECISION_EXCEEDED = "precision_exceeded"


class CostInputError(ValueError):
    """A scenario input is malformed. Raised, never converted into a cost of zero."""

    def __init__(self, code: CostErrorCode, field: str, detail: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code.value}: {field}: {detail}")


def require_decimal(value: object, field: str) -> Decimal:
    """Return ``value`` if it is a finite Decimal; reject float, bool, int and the rest."""
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise CostInputError(
            CostErrorCode.NOT_DECIMAL,
            field,
            f"expected Decimal, got {type(value).__name__}",
        )
    if not value.is_finite():
        raise CostInputError(CostErrorCode.NON_FINITE, field, f"{value} is not finite")
    return value


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CostInputError(CostErrorCode.EMPTY_FIELD, field, "a non-empty string is required")
    return value


def _require_optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _require_instance(value: object, types: tuple[type, ...], field: str) -> None:
    """Reject anything that is not one of the declared input types (a bare float included)."""
    if isinstance(value, bool) or not isinstance(value, types):
        raise CostInputError(
            CostErrorCode.NOT_DECIMAL,
            field,
            f"expected one of {[t.__name__ for t in types]}, got {type(value).__name__}",
        )


def _require_enum[E: Enum](value: object, enum_type: type[E], field: str) -> E:
    if not isinstance(value, enum_type):
        raise CostInputError(
            CostErrorCode.OUT_OF_RANGE,
            field,
            f"expected {enum_type.__name__}, got {type(value).__name__}",
        )
    return value


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Side(Enum):
    LONG = "long"
    SHORT = "short"


class Leg(Enum):
    ENTRY = "entry"
    EXIT = "exit"


class TradeDirection(Enum):
    BUY = "buy"
    SELL = "sell"


class CostComponent(Enum):
    SPREAD = "spread"
    SLIPPAGE = "slippage"
    FEE = "fee"
    FUNDING = "funding"
    FX = "fx"


class CostStatus(Enum):
    COMPLETE = "COST_COMPLETE"
    INCOMPLETE = "COST_INCOMPLETE"


class SpreadConvention(Enum):
    HALF_SPREAD_PLUS_TOUCH_SLIPPAGE = "half_spread_plus_touch_slippage"
    SLIPPAGE_FROM_MID = "slippage_from_mid"


class SlippageBasis(Enum):
    FROM_TOUCH = "from_touch"
    FROM_MID = "from_mid"


class DepthCoverage(Enum):
    FULL = "full"  # the walked book covered the whole scenario size
    PARTIAL = "partial"  # the book ran out before the size was filled


class FeeBasis(Enum):
    """Where a fee rate comes from. Deliberately no account-calibrated member."""

    UNCALIBRATED_ASSUMPTION = "uncalibrated_assumption"
    PUBLISHED_SCHEDULE = "published_schedule"


class SizeProvenance(Enum):
    REFERENCE_CONFIG = "reference_config"  # a configured reference size, not an account size
    HYPOTHETICAL_MANUAL = "hypothetical_manual"  # a hand-declared analysis size


class MissingReason(Enum):
    NOT_OBSERVED = "not_observed"
    SIZE_NOT_COVERED = "size_not_covered"
    FEE_UNKNOWN = "fee_unknown"
    FUNDING_SEMANTICS_UNVERIFIED = "funding_semantics_unverified"
    FX_RATE_UNAVAILABLE = "fx_rate_unavailable"


class NotApplicableReason(Enum):
    SPREAD_INCLUDED_IN_SLIPPAGE_FROM_MID = "spread_included_in_slippage_from_mid"
    SPOT_HAS_NO_FUNDING = "spot_has_no_funding"
    ZERO_FUNDING_INTERVALS = "zero_funding_intervals"
    REPORT_IN_QUOTE_CURRENCY = "report_in_quote_currency"
    NO_MONEY_PROJECTION = "no_money_projection"


class MoneyUnavailableReason(Enum):
    SCENARIO_INCOMPLETE = "scenario_incomplete"
    QUOTE_CURRENCY_UNIDENTIFIED = "quote_currency_unidentified"


_ENTRY_DIRECTION = {Side.LONG: TradeDirection.BUY, Side.SHORT: TradeDirection.SELL}
_EXIT_DIRECTION = {Side.LONG: TradeDirection.SELL, Side.SHORT: TradeDirection.BUY}


def leg_direction(side: Side, leg: Leg) -> TradeDirection:
    """LONG: entry BUY, exit SELL. SHORT: entry SELL, exit BUY."""
    return (_ENTRY_DIRECTION if leg is Leg.ENTRY else _EXIT_DIRECTION)[side]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Missing:
    """An input that is not known. Never read as zero."""

    reason: MissingReason
    detail: str = ""

    def __post_init__(self) -> None:
        _require_enum(self.reason, MissingReason, "Missing.reason")


@dataclass(frozen=True, slots=True)
class CostInstrument:
    """What is priced. ``symbol``/``quote_currency`` None = not identified at the caller's
    seam; bps can still be computed (a ratio), money cannot."""

    kind: InstrumentKind
    symbol: str | None
    quote_currency: str | None

    def __post_init__(self) -> None:
        _require_enum(self.kind, InstrumentKind, "CostInstrument.kind")
        _require_optional_text(self.symbol, "CostInstrument.symbol")
        _require_optional_text(self.quote_currency, "CostInstrument.quote_currency")


@dataclass(frozen=True, slots=True)
class ScenarioSize:
    """Reference notional in the instrument's quote currency, with where it came from."""

    notional: Decimal
    provenance: SizeProvenance

    def __post_init__(self) -> None:
        if require_decimal(self.notional, "ScenarioSize.notional") <= 0:
            raise CostInputError(CostErrorCode.OUT_OF_RANGE, "ScenarioSize.notional", "must be > 0")
        _require_enum(self.provenance, SizeProvenance, "ScenarioSize.provenance")


@dataclass(frozen=True, slots=True)
class SpreadInput:
    """Quoted spread (ask - bid) / mid, in bps, of the book the slippage was walked on."""

    bps: Decimal
    source: str

    def __post_init__(self) -> None:
        value = require_decimal(self.bps, "SpreadInput.bps")
        if value < 0 or value >= Decimal(20_000):
            raise CostInputError(CostErrorCode.OUT_OF_RANGE, "SpreadInput.bps", "must be in [0, 20000)")
        _require_text(self.source, "SpreadInput.source")


@dataclass(frozen=True, slots=True)
class SlippageInput:
    """Slippage of one trade direction at ``measured_notional``, in bps of its basis price."""

    bps: Decimal
    basis: SlippageBasis
    measured_notional: Decimal
    coverage: DepthCoverage
    source: str

    def __post_init__(self) -> None:
        value = require_decimal(self.bps, "SlippageInput.bps")
        if value < 0 or value >= _BPS:
            raise CostInputError(CostErrorCode.OUT_OF_RANGE, "SlippageInput.bps", "must be in [0, 10000)")
        _require_enum(self.basis, SlippageBasis, "SlippageInput.basis")
        if require_decimal(self.measured_notional, "SlippageInput.measured_notional") <= 0:
            raise CostInputError(
                CostErrorCode.OUT_OF_RANGE, "SlippageInput.measured_notional", "must be > 0"
            )
        _require_enum(self.coverage, DepthCoverage, "SlippageInput.coverage")
        _require_text(self.source, "SlippageInput.source")


@dataclass(frozen=True, slots=True)
class FeeInput:
    """Fee rate of one leg in bps of that leg's fill notional (negative = rebate)."""

    bps: Decimal
    basis: FeeBasis
    source: str

    def __post_init__(self) -> None:
        value = require_decimal(self.bps, "FeeInput.bps")
        if abs(value) >= _BPS:
            raise CostInputError(CostErrorCode.OUT_OF_RANGE, "FeeInput.bps", "must be in (-10000, 10000)")
        _require_enum(self.basis, FeeBasis, "FeeInput.basis")
        _require_text(self.source, "FeeInput.source")


@dataclass(frozen=True, slots=True)
class FundingInput:
    """Verified funding rate per interval, in bps of notional; positive = LONG pays."""

    bps_per_interval: Decimal
    source: str

    def __post_init__(self) -> None:
        value = require_decimal(self.bps_per_interval, "FundingInput.bps_per_interval")
        if abs(value) >= _BPS:
            raise CostInputError(
                CostErrorCode.OUT_OF_RANGE, "FundingInput.bps_per_interval", "must be in (-10000, 10000)"
            )
        _require_text(self.source, "FundingInput.source")


@dataclass(frozen=True, slots=True)
class FxRate:
    """1 unit of ``from_currency`` = ``rate`` units of ``to_currency``."""

    from_currency: str
    to_currency: str
    rate: Decimal
    source: str

    def __post_init__(self) -> None:
        _require_text(self.from_currency, "FxRate.from_currency")
        _require_text(self.to_currency, "FxRate.to_currency")
        if require_decimal(self.rate, "FxRate.rate") <= 0:
            raise CostInputError(CostErrorCode.OUT_OF_RANGE, "FxRate.rate", "must be > 0")
        _require_text(self.source, "FxRate.source")


@dataclass(frozen=True, slots=True)
class CostScenarioInput:
    instrument: CostInstrument
    side: Side
    size: ScenarioSize
    spread_convention: SpreadConvention
    spread: SpreadInput | Missing | None
    buy_slippage: SlippageInput | Missing
    sell_slippage: SlippageInput | Missing
    entry_fee: FeeInput | Missing
    exit_fee: FeeInput | Missing
    funding_intervals: int
    funding: FundingInput | Missing | None
    report_currency: str | None = None
    fx: FxRate | Missing | None = None

    def __post_init__(self) -> None:
        _require_enum(self.side, Side, "CostScenarioInput.side")
        _require_enum(self.spread_convention, SpreadConvention, "CostScenarioInput.spread_convention")
        if isinstance(self.funding_intervals, bool) or not isinstance(self.funding_intervals, int):
            raise CostInputError(
                CostErrorCode.OUT_OF_RANGE, "CostScenarioInput.funding_intervals", "must be an int"
            )
        if self.funding_intervals < 0:
            raise CostInputError(
                CostErrorCode.OUT_OF_RANGE, "CostScenarioInput.funding_intervals", "must be >= 0"
            )
        _require_optional_text(self.report_currency, "CostScenarioInput.report_currency")
        _require_instance(self.instrument, (CostInstrument,), "instrument")
        _require_instance(self.size, (ScenarioSize,), "size")
        _require_instance(self.spread, (SpreadInput, Missing, type(None)), "spread")
        _require_instance(self.buy_slippage, (SlippageInput, Missing), "buy_slippage")
        _require_instance(self.sell_slippage, (SlippageInput, Missing), "sell_slippage")
        _require_instance(self.entry_fee, (FeeInput, Missing), "entry_fee")
        _require_instance(self.exit_fee, (FeeInput, Missing), "exit_fee")
        _require_instance(self.funding, (FundingInput, Missing, type(None)), "funding")
        _require_instance(self.fx, (FxRate, Missing, type(None)), "fx")


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class CostLine:
    """One known cost, as a fraction of the reference notional (positive = paid)."""

    component: CostComponent
    leg: Leg | None
    direction: TradeDirection | None
    fraction: Decimal
    source: str

    @property
    def bps(self) -> Decimal:
        return _exact(lambda: self.fraction * _BPS, "CostLine.bps")


@dataclass(frozen=True, slots=True)
class MissingLine:
    component: CostComponent
    leg: Leg | None
    reason: MissingReason
    detail: str


@dataclass(frozen=True, slots=True)
class NotApplicableLine:
    component: CostComponent
    leg: Leg | None
    reason: NotApplicableReason


@dataclass(frozen=True, slots=True)
class HypotheticalCashflow:
    """Signed quote-currency cash of the round trip at an unchanged mid (+ = received)."""

    entry_trade: Money
    entry_fee: Money
    exit_trade: Money
    exit_fee: Money
    funding: Money
    net: Money


@dataclass(frozen=True, slots=True)
class CostScenario:
    policy_version: str
    instrument: CostInstrument
    side: Side
    size: ScenarioSize
    spread_convention: SpreadConvention
    funding_intervals: int
    status: CostStatus
    lines: tuple[CostLine, ...]
    missing: tuple[MissingLine, ...]
    not_applicable: tuple[NotApplicableLine, ...]
    total_fraction: Decimal | None  # None unless COMPLETE
    total_quote: Money | None
    total_report: Money | None
    cashflow: HypotheticalCashflow | None
    money_unavailable: MoneyUnavailableReason | None

    @property
    def total_bps(self) -> Decimal | None:
        fraction = self.total_fraction
        if fraction is None:
            return None
        return _exact(lambda: fraction * _BPS, "CostScenario.total_bps")

    @property
    def fees_calibrated(self) -> bool:
        """Always False: no FeeBasis can express an account-calibrated fee."""
        return False


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def _exact(compute: Callable[[], Decimal], field: str) -> Decimal:
    try:
        with localcontext(COST_CONTEXT):
            result = compute()
    except Inexact as exc:
        raise CostInputError(
            CostErrorCode.PRECISION_EXCEEDED, field, "exact result exceeds COST_CONTEXT precision"
        ) from exc
    except (InvalidOperation, Overflow, Underflow, DivisionByZero) as exc:
        raise CostInputError(CostErrorCode.OUT_OF_RANGE, field, str(exc)) from exc
    return result


def present(value: Decimal, places: int) -> Decimal:
    """Round ``value`` to ``places`` decimals toward more cost (ROUND_CEILING)."""
    require_decimal(value, "present.value")
    if isinstance(places, bool) or not isinstance(places, int) or places < 0:
        raise CostInputError(CostErrorCode.OUT_OF_RANGE, "present.places", "must be an int >= 0")
    context = Context(prec=COST_CONTEXT.prec, rounding=PRESENTATION_ROUNDING, Emin=-999_999, Emax=999_999)
    return value.quantize(Decimal(1).scaleb(-places), context=context)


@dataclass(frozen=True, slots=True)
class _Fill:
    """Fill notional of one leg as a fraction of the reference notional, and its lines."""

    notional_fraction: Decimal
    spread: Decimal | None  # None when not applicable (SLIPPAGE_FROM_MID)
    slippage: Decimal


def _fill(
    direction: TradeDirection, convention: SpreadConvention, half_spread: Decimal | None, slippage: Decimal
) -> _Fill:
    sign = _ONE if direction is TradeDirection.BUY else -_ONE
    if convention is SpreadConvention.SLIPPAGE_FROM_MID:
        fill = _exact(lambda: _ONE + sign * slippage, "fill")
        return _Fill(notional_fraction=fill, spread=None, slippage=slippage)
    assert half_spread is not None
    touch = _exact(lambda: _ONE + sign * half_spread, "touch")
    fill = _exact(lambda: touch * (_ONE + sign * slippage), "fill")
    # Cost of the leg = |fill - 1|, itemised as mid->touch (half spread) and
    # touch->fill (slippage beyond the touch, on the touch price).
    return _Fill(notional_fraction=fill, spread=half_spread, slippage=_exact(lambda: touch * slippage, "slip"))


def price_round_trip(scenario: CostScenarioInput) -> CostScenario:
    """Price one hypothetical round trip. Deterministic; raises only on malformed input."""
    if not isinstance(scenario, CostScenarioInput):
        raise CostInputError(CostErrorCode.NOT_DECIMAL, "scenario", "expected CostScenarioInput")
    convention = scenario.spread_convention
    expected_basis = (
        SlippageBasis.FROM_MID
        if convention is SpreadConvention.SLIPPAGE_FROM_MID
        else SlippageBasis.FROM_TOUCH
    )

    missing: list[MissingLine] = []
    not_applicable: list[NotApplicableLine] = []

    # Spread
    half_spread: Decimal | None = None
    spread_missing = False
    if convention is SpreadConvention.SLIPPAGE_FROM_MID:
        if isinstance(scenario.spread, SpreadInput):
            raise CostInputError(
                CostErrorCode.DOUBLE_COUNTED_SPREAD,
                "spread",
                "slippage measured from the mid already contains the half spread",
            )
        for leg in Leg:
            not_applicable.append(
                NotApplicableLine(
                    CostComponent.SPREAD, leg, NotApplicableReason.SPREAD_INCLUDED_IN_SLIPPAGE_FROM_MID
                )
            )
    elif isinstance(scenario.spread, SpreadInput):
        spread_bps = scenario.spread.bps
        half_spread = _exact(lambda: spread_bps / _TWO / _BPS, "spread")
    else:
        spread_missing = True
        reason = scenario.spread.reason if isinstance(scenario.spread, Missing) else MissingReason.NOT_OBSERVED
        detail = scenario.spread.detail if isinstance(scenario.spread, Missing) else "spread not supplied"
        for leg in Leg:
            missing.append(MissingLine(CostComponent.SPREAD, leg, reason, detail))

    # Slippage, per direction
    slippage: dict[TradeDirection, SlippageInput | None] = {}
    for direction, value in (
        (TradeDirection.BUY, scenario.buy_slippage),
        (TradeDirection.SELL, scenario.sell_slippage),
    ):
        if isinstance(value, SlippageInput):
            if value.basis is not expected_basis:
                raise CostInputError(
                    CostErrorCode.CONVENTION_MISMATCH,
                    f"{direction.value}_slippage",
                    f"basis {value.basis.value} does not match convention {convention.value}",
                )
            if value.measured_notional != scenario.size.notional:
                raise CostInputError(
                    CostErrorCode.SIZE_MISMATCH,
                    f"{direction.value}_slippage",
                    f"measured at {value.measured_notional}, scenario size {scenario.size.notional}",
                )
            slippage[direction] = value
        elif isinstance(value, Missing):
            slippage[direction] = None
        else:
            raise CostInputError(
                CostErrorCode.NOT_DECIMAL, f"{direction.value}_slippage", "expected SlippageInput or Missing"
            )

    lines: list[CostLine] = []
    fills: dict[Leg, _Fill | None] = {}
    for leg in Leg:
        direction = leg_direction(scenario.side, leg)
        slip = slippage[direction]
        if slip is None:
            source = scenario.buy_slippage if direction is TradeDirection.BUY else scenario.sell_slippage
            assert isinstance(source, Missing)
            missing.append(MissingLine(CostComponent.SLIPPAGE, leg, source.reason, source.detail))
            fills[leg] = None
            continue
        if slip.coverage is DepthCoverage.PARTIAL:
            missing.append(
                MissingLine(
                    CostComponent.SLIPPAGE,
                    leg,
                    MissingReason.SIZE_NOT_COVERED,
                    f"book did not cover {scenario.size.notional} on the {direction.value} side",
                )
            )
            fills[leg] = None
            continue
        if spread_missing:
            fills[leg] = None
            continue
        slip_bps = slip.bps
        fraction = _exact(lambda: slip_bps / _BPS, "slippage")
        fill = _fill(direction, convention, half_spread, fraction)
        fills[leg] = fill
        if fill.spread is not None:
            spread_source = scenario.spread.source if isinstance(scenario.spread, SpreadInput) else ""
            lines.append(CostLine(CostComponent.SPREAD, leg, direction, fill.spread, spread_source))
        lines.append(CostLine(CostComponent.SLIPPAGE, leg, direction, fill.slippage, slip.source))

    # Fees, per leg on that leg's fill notional
    fee_fractions: dict[Leg, Decimal] = {}
    for leg, fee in ((Leg.ENTRY, scenario.entry_fee), (Leg.EXIT, scenario.exit_fee)):
        if isinstance(fee, Missing):
            missing.append(MissingLine(CostComponent.FEE, leg, fee.reason, fee.detail))
            continue
        if not isinstance(fee, FeeInput):
            raise CostInputError(CostErrorCode.NOT_DECIMAL, f"{leg.value}_fee", "expected FeeInput or Missing")
        leg_fill = fills[leg]
        if leg_fill is None:
            continue  # its base (the fill notional) is unknown: the leg is already missing
        fee_bps = fee.bps
        notional_fraction = leg_fill.notional_fraction
        fee_fraction = _exact(lambda: fee_bps / _BPS * notional_fraction, "fee")
        fee_fractions[leg] = fee_fraction
        lines.append(
            CostLine(
                CostComponent.FEE,
                leg,
                leg_direction(scenario.side, leg),
                fee_fraction,
                f"{fee.basis.value}: {fee.source}",
            )
        )

    # Funding (futures only, per declared interval, on the reference notional)
    funding_fraction = Decimal(0)
    if scenario.instrument.kind is InstrumentKind.SPOT:
        if isinstance(scenario.funding, FundingInput):
            raise CostInputError(CostErrorCode.FUNDING_NOT_APPLICABLE, "funding", "spot has no funding")
        not_applicable.append(NotApplicableLine(CostComponent.FUNDING, None, NotApplicableReason.SPOT_HAS_NO_FUNDING))
    elif scenario.funding_intervals == 0:
        not_applicable.append(
            NotApplicableLine(CostComponent.FUNDING, None, NotApplicableReason.ZERO_FUNDING_INTERVALS)
        )
    elif isinstance(scenario.funding, FundingInput):
        rate = scenario.funding.bps_per_interval
        intervals = Decimal(scenario.funding_intervals)
        sign = _ONE if scenario.side is Side.LONG else -_ONE
        funding_fraction = _exact(lambda: sign * rate / _BPS * intervals, "funding")
        lines.append(CostLine(CostComponent.FUNDING, None, None, funding_fraction, scenario.funding.source))
    else:
        reason = scenario.funding.reason if isinstance(scenario.funding, Missing) else MissingReason.NOT_OBSERVED
        detail = scenario.funding.detail if isinstance(scenario.funding, Missing) else "funding not supplied"
        missing.append(MissingLine(CostComponent.FUNDING, None, reason, detail))

    # FX (only when money is reported in a currency other than the quote currency)
    quote = scenario.instrument.quote_currency
    report = scenario.report_currency
    fx_rate: Decimal | None = None
    if report is None or quote is None:
        not_applicable.append(NotApplicableLine(CostComponent.FX, None, NotApplicableReason.NO_MONEY_PROJECTION))
    elif report == quote:
        not_applicable.append(
            NotApplicableLine(CostComponent.FX, None, NotApplicableReason.REPORT_IN_QUOTE_CURRENCY)
        )
    elif isinstance(scenario.fx, FxRate):
        if scenario.fx.from_currency != quote or scenario.fx.to_currency != report:
            raise CostInputError(
                CostErrorCode.CURRENCY_MISMATCH,
                "fx",
                f"rate is {scenario.fx.from_currency}->{scenario.fx.to_currency}, need {quote}->{report}",
            )
        fx_rate = scenario.fx.rate
    else:
        reason = scenario.fx.reason if isinstance(scenario.fx, Missing) else MissingReason.FX_RATE_UNAVAILABLE
        detail = scenario.fx.detail if isinstance(scenario.fx, Missing) else f"no {quote}->{report} rate"
        missing.append(MissingLine(CostComponent.FX, None, reason, detail))

    if missing:
        return CostScenario(
            policy_version=COST_POLICY_VERSION,
            instrument=scenario.instrument,
            side=scenario.side,
            size=scenario.size,
            spread_convention=convention,
            funding_intervals=scenario.funding_intervals,
            status=CostStatus.INCOMPLETE,
            lines=tuple(lines),
            missing=tuple(missing),
            not_applicable=tuple(not_applicable),
            total_fraction=None,
            total_quote=None,
            total_report=None,
            cashflow=None,
            money_unavailable=MoneyUnavailableReason.SCENARIO_INCOMPLETE,
        )

    known = [line.fraction for line in lines]
    total = _exact(lambda: sum(known, Decimal(0)), "total")

    total_quote: Money | None = None
    total_report: Money | None = None
    cashflow: HypotheticalCashflow | None = None
    money_unavailable: MoneyUnavailableReason | None = MoneyUnavailableReason.QUOTE_CURRENCY_UNIDENTIFIED
    if quote is not None:
        money_unavailable = None
        notional = scenario.size.notional
        total_quote = Money(_exact(lambda: total * notional, "total_quote"), quote)
        if report is not None:
            if fx_rate is None:
                total_report = Money(total_quote.amount, quote)
            else:
                amount = total_quote.amount
                rate_value = fx_rate
                total_report = Money(_exact(lambda: amount * rate_value, "total_report"), report)
        cashflow = _cashflow(scenario, fills, fee_fractions, funding_fraction, quote)

    return CostScenario(
        policy_version=COST_POLICY_VERSION,
        instrument=scenario.instrument,
        side=scenario.side,
        size=scenario.size,
        spread_convention=convention,
        funding_intervals=scenario.funding_intervals,
        status=CostStatus.COMPLETE,
        lines=tuple(lines),
        missing=(),
        not_applicable=tuple(not_applicable),
        total_fraction=total,
        total_quote=total_quote,
        total_report=total_report,
        cashflow=cashflow,
        money_unavailable=money_unavailable,
    )


def _cashflow(
    scenario: CostScenarioInput,
    fills: dict[Leg, _Fill | None],
    fees: dict[Leg, Decimal],
    funding_fraction: Decimal,
    currency: str,
) -> HypotheticalCashflow:
    """Signed cash of each leg: a BUY pays its fill notional, a SELL receives it."""
    notional = scenario.size.notional

    def money(value: Decimal, field: str) -> Money:
        return Money(_exact(lambda: value * notional, field), currency)

    trades: dict[Leg, Decimal] = {}
    for leg in Leg:
        fill = fills[leg]
        assert fill is not None
        sign = -_ONE if leg_direction(scenario.side, leg) is TradeDirection.BUY else _ONE
        fraction = fill.notional_fraction
        trades[leg] = _exact(lambda: sign * fraction, "trade")
    entry_fee = _exact(lambda: -fees[Leg.ENTRY], "entry_fee")
    exit_fee = _exact(lambda: -fees[Leg.EXIT], "exit_fee")
    funding = _exact(lambda: -funding_fraction, "funding_cash")
    net = _exact(lambda: trades[Leg.ENTRY] + trades[Leg.EXIT] + entry_fee + exit_fee + funding, "net")
    return HypotheticalCashflow(
        entry_trade=money(trades[Leg.ENTRY], "entry_trade"),
        entry_fee=money(entry_fee, "entry_fee"),
        exit_trade=money(trades[Leg.EXIT], "exit_trade"),
        exit_fee=money(exit_fee, "exit_fee"),
        funding=money(funding, "funding"),
        net=money(net, "net"),
    )
