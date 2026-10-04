"""Risk Engine of the pilot shadow: EX-1 envelope, pair rules, sizing, loss locks, settlement.

Phase 2 of the path to real trading (docs/EXECUTION_ARCHITECTURE.md section 5). Pretend
money only: this module never places an order and never reads an account. It only does
the sums that decide whether a LONG spot entry fits the account envelope and how large it
may be. Pure: no I/O, no wall clock, no configuration, no adapter imports.

Envelope (EX-1 ceilings)
------------------------

``Envelope`` holds the assigned equity (whole cents) and the limits, each a percentage of
the current account equity. The EX-1 table values are the defaults and also the loosest
values accepted: a configuration may only be stricter. Per-entry planned stress loss at
most 0.25 %, aggregate open planned loss at most 0.50 %, gross notional at most 10 %, a
free-cash buffer of at least 10 %, a daily loss lock at -1 % of the UTC day-start equity,
a drawdown lock 3 % below the high-water mark, one simultaneous position and no leverage.
An optional absolute per-entry cap applies as well when it is lower.

Sizing (LONG only)
------------------

The entry fills at the ask. The stop and the target are the EX-1 levels of
``domain.paper.exit_levels`` (2 x ATR, 2R, 24 h), each rounded DOWN to the pair tick. The
worst-case loss of one unit is::

    stress_exit   = stop * (1 - PILOT_STRESS_EXIT_SLIPPAGE_BPS / 10000)
    loss_per_unit = (ask - stop) + (stop - stress_exit) + ask * f + stress_exit * f

with ``f`` the spot taker fee per leg. The quantity is the minimum of four candidates -
per-entry loss cap / loss_per_unit, remaining aggregate loss / loss_per_unit, remaining
notional / ask and (cash - buffer) / (ask * (1 + f)) - rounded DOWN to the lot and
checked against the pair's ``ordermin`` (quantity) and ``costmin`` (quantity x ask); a
minimum is never met by rounding up. The planned loss is then recomputed on the final
quantity with each leg's fee rounded UP to the cent. When it (or the notional, or the
cash it needs) no longer fits, the quantity drops by one lot, at most
``MAX_DOWNWARD_PASSES`` times; still failing is ``sizing_inconsistent``.

Every division that yields a quantity rounds toward zero (``QUANTITY_CONTEXT``), so a
candidate is never larger than the exact value. Anything missing or invalid is a typed
``NoTrade``, never a zero default.

Locks and equity
----------------

Account equity = assigned equity + realized nets + a conservative mark of the open
position (quantity x bid, minus the exit fee on it rounded UP to the cent, minus the entry
cost basis). The daily lock trips when equity - day_start <= -daily% x day_start and the
drawdown lock when equity <= high_water x (1 - drawdown%). The functions here only
report a trip; nothing in this module clears a lock (only a recorded review does).

Settlement
----------

A LONG closes by selling the quantity at the observed exit bid. The gross move and each
leg's fee are rounded half to even to cents, like the paper game, and the net is their
exact difference.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    InvalidOperation,
    localcontext,
)
from enum import Enum

from radar_v08.domain.paper import (
    BPS,
    CENT,
    Direction,
    ExitLevels,
    Outcome,
    Quote,
    QuoteProblem,
    cents,
    exit_levels,
    validate_atr,
    validate_quote,
)

#: Exact arithmetic for amounts (only divisions can round, half to even).
RISK_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
#: Divisions that yield a quantity round toward zero, so a candidate never exceeds its bound.
QUANTITY_CONTEXT = Context(prec=50, rounding=ROUND_FLOOR)
HUNDRED = Decimal(100)

#: Identity of the EX-1 envelope ceilings enforced by ``Envelope``.
ENVELOPE_POLICY_ID = "ex1_envelope_v1"
#: Identity of the sizing rules of this module.
SIZING_POLICY_ID = "pilot_sizing_v1"

#: EX-1 ceilings (docs/EXECUTION_ARCHITECTURE.md section 5), percent of account equity.
EX1_PER_ENTRY_LOSS_PCT = Decimal("0.25")
EX1_AGGREGATE_LOSS_PCT = Decimal("0.50")
EX1_GROSS_NOTIONAL_PCT = Decimal("10")
#: A floor, not a ceiling: at least this much cash stays unreserved.
EX1_MIN_CASH_BUFFER_PCT = Decimal("10")
EX1_DAILY_LOSS_PCT = Decimal("1")
EX1_DRAWDOWN_PCT = Decimal("3")
EX1_MAX_POSITIONS = 1
EX1_LEVERAGE = 0

#: Stress exit slippage beyond the stop, in bps of the stop. Uncalibrated engineering input:
#: every close records its real exit-vs-stop slippage so it can be calibrated later; a new
#: value is a new policy id, never an environment setting.
PILOT_STRESS_EXIT_SLIPPAGE_BPS = Decimal(50)
PILOT_STRESS_POLICY_ID = "pilot_stress_exit_slippage_50bps_v1"

#: Downward-only passes of one lot each before the sizing gives up.
MAX_DOWNWARD_PASSES = 3
#: Largest lot/price decimals accepted from AssetPairs.
MAX_PAIR_DECIMALS = 18


class NoTradeReason(Enum):
    """Why the pilot refuses an entry. The values are stable strings stored and shown later."""

    UNSUPPORTED_DIRECTION = "unsupported_direction"
    ENVELOPE_CHANGED = "envelope_changed"
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    DAILY_LOSS_LOCK = "daily_loss_lock"
    DRAWDOWN_LOCK = "drawdown_lock"
    POSITION_ALREADY_OPEN = "position_already_open"
    QUOTE_CURRENCY_MISMATCH = "quote_currency_mismatch"
    INVALID_QUOTE = "invalid_quote"
    NO_VALID_ATR = "no_valid_atr"
    MISSING_PAIR_RULES = "missing_pair_rules"
    EQUITY_UNAVAILABLE = "equity_unavailable"
    INVALID_LEVELS = "invalid_levels"
    STOP_INVALID_AFTER_TICK = "stop_invalid_after_tick"
    NO_LOSS_BUDGET = "no_loss_budget"
    NO_NOTIONAL_ROOM = "no_notional_room"
    INSUFFICIENT_CASH = "insufficient_cash"
    BELOW_ORDER_MINIMUM = "below_order_minimum"
    BELOW_COST_MINIMUM = "below_cost_minimum"
    SIZING_INCONSISTENT = "sizing_inconsistent"


class LockKind(Enum):
    DAILY_LOSS = "daily_loss"
    DRAWDOWN = "drawdown"


class BindingConstraint(Enum):
    """Which of the four candidate quantities set the size (ties go to the earlier one)."""

    PER_ENTRY_LOSS = "per_entry_loss"
    AGGREGATE_LOSS = "aggregate_loss"
    NOTIONAL = "notional"
    CASH = "cash"


LOCK_REASONS = {
    LockKind.DAILY_LOSS: NoTradeReason.DAILY_LOSS_LOCK,
    LockKind.DRAWDOWN: NoTradeReason.DRAWDOWN_LOCK,
}


class RiskInputError(ValueError):
    """A caller passed a value this module cannot use; ``field`` names it."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(f"{field}: {detail}")
        self.field = field
        self.detail = detail


class EnvelopeError(RiskInputError):
    """An envelope value is invalid or looser than an EX-1 ceiling."""


@dataclass(frozen=True, slots=True)
class NoTrade:
    """A refused entry: the typed reason plus a short machine-readable detail."""

    reason: NoTradeReason
    detail: str = ""


def _require_decimal(field: str, value: object, *, error: type[RiskInputError] = RiskInputError) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise error(field, f"must be a finite Decimal, got {value!r}")
    return value


def _require_nonnegative(field: str, value: object) -> Decimal:
    number = _require_decimal(field, value)
    if number < 0:
        raise RiskInputError(field, f"must be >= 0, got {number}")
    return number


def _require_positive(field: str, value: object) -> Decimal:
    number = _require_decimal(field, value)
    if number <= 0:
        raise RiskInputError(field, f"must be > 0, got {number}")
    return number


def _is_whole_cents(value: Decimal) -> bool:
    return value == value.quantize(CENT, rounding=ROUND_HALF_EVEN, context=RISK_CONTEXT)


def plain(value: Decimal) -> str:
    """``value`` as plain text without exponent or trailing zeros (for NO_TRADE details)."""
    return format(value.normalize(RISK_CONTEXT), "f")


def cents_up(value: Decimal) -> Decimal:
    """``value`` rounded UP (toward +infinity) to the cent."""
    return value.quantize(CENT, rounding=ROUND_CEILING, context=RISK_CONTEXT)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """The largest multiple of ``step`` (> 0) that is <= ``value``."""
    with localcontext(QUANTITY_CONTEXT):
        steps = (value / step).to_integral_value(rounding=ROUND_FLOOR)
        return steps * step


def lot_step(lot_decimals: int) -> Decimal:
    """One lot: 10^-lot_decimals."""
    return Decimal(1).scaleb(-lot_decimals)


def floor_to_lot(quantity: Decimal, lot_decimals: int) -> Decimal:
    """``quantity`` rounded DOWN to ``lot_decimals`` decimals (0 floors to an integer)."""
    return quantity.quantize(lot_step(lot_decimals), rounding=ROUND_FLOOR, context=QUANTITY_CONTEXT)


# --------------------------------------------------------------------------- envelope


@dataclass(frozen=True, slots=True)
class Envelope:
    """The account envelope of the pilot. Construction refuses any value looser than EX-1."""

    equity: Decimal
    currency: str
    per_entry_loss_pct: Decimal = EX1_PER_ENTRY_LOSS_PCT
    aggregate_loss_pct: Decimal = EX1_AGGREGATE_LOSS_PCT
    gross_notional_pct: Decimal = EX1_GROSS_NOTIONAL_PCT
    cash_buffer_pct: Decimal = EX1_MIN_CASH_BUFFER_PCT
    daily_loss_pct: Decimal = EX1_DAILY_LOSS_PCT
    drawdown_pct: Decimal = EX1_DRAWDOWN_PCT
    per_entry_abs_cap: Decimal | None = None
    max_positions: int = EX1_MAX_POSITIONS
    leverage: int = EX1_LEVERAGE

    def __post_init__(self) -> None:
        equity = _require_decimal("equity", self.equity, error=EnvelopeError)
        if equity <= 0 or not _is_whole_cents(equity):
            raise EnvelopeError("equity", f"must be a positive amount in whole cents, got {equity}")
        if not isinstance(self.currency, str) or not self.currency.strip() or self.currency != self.currency.strip():
            raise EnvelopeError("currency", f"must be a non-empty code, got {self.currency!r}")
        ceilings = (
            ("per_entry_loss_pct", self.per_entry_loss_pct, EX1_PER_ENTRY_LOSS_PCT),
            ("aggregate_loss_pct", self.aggregate_loss_pct, EX1_AGGREGATE_LOSS_PCT),
            ("gross_notional_pct", self.gross_notional_pct, EX1_GROSS_NOTIONAL_PCT),
            ("daily_loss_pct", self.daily_loss_pct, EX1_DAILY_LOSS_PCT),
            ("drawdown_pct", self.drawdown_pct, EX1_DRAWDOWN_PCT),
        )
        for field, value, ceiling in ceilings:
            number = _require_decimal(field, value, error=EnvelopeError)
            if number <= 0:
                raise EnvelopeError(field, f"must be > 0, got {number}")
            if number > ceiling:
                raise EnvelopeError(field, f"{number} is looser than the EX-1 ceiling {ceiling}")
        buffer = _require_decimal("cash_buffer_pct", self.cash_buffer_pct, error=EnvelopeError)
        if buffer < EX1_MIN_CASH_BUFFER_PCT:
            raise EnvelopeError("cash_buffer_pct", f"{buffer} is below the EX-1 minimum {EX1_MIN_CASH_BUFFER_PCT}")
        if buffer >= HUNDRED:
            raise EnvelopeError("cash_buffer_pct", f"must be below 100, got {buffer}")
        if self.per_entry_abs_cap is not None:
            cap = _require_decimal("per_entry_abs_cap", self.per_entry_abs_cap, error=EnvelopeError)
            if cap <= 0 or not _is_whole_cents(cap):
                raise EnvelopeError("per_entry_abs_cap", f"must be a positive amount in whole cents, got {cap}")
        if isinstance(self.max_positions, bool) or self.max_positions != EX1_MAX_POSITIONS:
            raise EnvelopeError("max_positions", f"must be {EX1_MAX_POSITIONS}, got {self.max_positions!r}")
        if isinstance(self.leverage, bool) or self.leverage != EX1_LEVERAGE:
            raise EnvelopeError("leverage", f"must be {EX1_LEVERAGE}, got {self.leverage!r}")

    def canonical_text(self) -> str:
        """A stable text of every value, used to record the envelope and detect a change."""
        return json.dumps(
            {
                "policy_id": ENVELOPE_POLICY_ID,
                "equity": str(self.equity),
                "currency": self.currency,
                "per_entry_loss_pct": str(self.per_entry_loss_pct),
                "aggregate_loss_pct": str(self.aggregate_loss_pct),
                "gross_notional_pct": str(self.gross_notional_pct),
                "cash_buffer_pct": str(self.cash_buffer_pct),
                "daily_loss_pct": str(self.daily_loss_pct),
                "drawdown_pct": str(self.drawdown_pct),
                "per_entry_abs_cap": None if self.per_entry_abs_cap is None else str(self.per_entry_abs_cap),
                "max_positions": self.max_positions,
                "leverage": self.leverage,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def _percent_of(base: Decimal, pct: Decimal) -> Decimal:
    with localcontext(RISK_CONTEXT):
        return base * pct / HUNDRED


@dataclass(frozen=True, slots=True)
class Budget:
    """The room left under each limit at the current account equity (exact, not rounded)."""

    per_entry_loss_cap: Decimal
    aggregate_loss_cap: Decimal
    aggregate_loss_remaining: Decimal
    notional_cap: Decimal
    notional_remaining: Decimal
    cash_buffer: Decimal
    cash_room: Decimal


def budget(
    envelope: Envelope,
    equity: Decimal,
    cash: Decimal,
    open_planned_loss: Decimal,
    open_notional: Decimal,
) -> Budget:
    """The limits of ``envelope`` applied to the current ``equity``, net of what is open."""
    _require_positive("equity", equity)
    _require_decimal("cash", cash)
    _require_nonnegative("open_planned_loss", open_planned_loss)
    _require_nonnegative("open_notional", open_notional)
    per_entry = _percent_of(equity, envelope.per_entry_loss_pct)
    if envelope.per_entry_abs_cap is not None:
        per_entry = min(per_entry, envelope.per_entry_abs_cap)
    aggregate = _percent_of(equity, envelope.aggregate_loss_pct)
    notional = _percent_of(equity, envelope.gross_notional_pct)
    buffer = _percent_of(equity, envelope.cash_buffer_pct)
    with localcontext(RISK_CONTEXT):
        return Budget(
            per_entry_loss_cap=per_entry,
            aggregate_loss_cap=aggregate,
            aggregate_loss_remaining=aggregate - open_planned_loss,
            notional_cap=notional,
            notional_remaining=notional - open_notional,
            cash_buffer=buffer,
            cash_room=cash - buffer,
        )


# --------------------------------------------------------------------------- pair rules


@dataclass(frozen=True, slots=True)
class PairRules:
    """The venue rules of one spot pair (Kraken public AssetPairs)."""

    lot_decimals: int
    ordermin: Decimal
    costmin: Decimal
    tick: Decimal
    tick_source: str

    @property
    def lot(self) -> Decimal:
        return lot_step(self.lot_decimals)


def _missing(field: str, problem: str) -> NoTrade:
    return NoTrade(NoTradeReason.MISSING_PAIR_RULES, f"{field}:{problem}")


def _decimals_field(entry: Mapping[str, object], field: str) -> int | NoTrade:
    if field not in entry or entry[field] is None:
        return _missing(field, "missing")
    value = entry[field]
    if isinstance(value, bool) or not isinstance(value, int):
        return _missing(field, "not_an_integer")
    if value < 0:
        return _missing(field, "negative")
    if value > MAX_PAIR_DECIMALS:
        return _missing(field, "too_large")
    return value


def _decimal_text_field(entry: Mapping[str, object], field: str) -> Decimal | NoTrade:
    """A decimal string (or exact int/Decimal) parsed exactly; a float is refused, never converted."""
    if field not in entry or entry[field] is None:
        return _missing(field, "missing")
    value = entry[field]
    if isinstance(value, bool) or isinstance(value, float):
        return _missing(field, "not_exact")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return _missing(field, "missing")
        try:
            number = Decimal(text)
        except InvalidOperation:
            return _missing(field, "not_a_number")
    elif isinstance(value, (int, Decimal)):
        number = Decimal(value)
    else:
        return _missing(field, "not_a_number")
    if not number.is_finite():
        return _missing(field, "not_finite")
    if number < 0:
        return _missing(field, "negative")
    return number


def parse_pair_rules(entry: object) -> PairRules | NoTrade:
    """The rules of one raw AssetPairs entry, or ``missing_pair_rules`` naming the bad field.

    ``lot_decimals`` is an int >= 0; ``ordermin`` and ``costmin`` are decimal strings >= 0;
    the tick is ``tick_size`` (> 0) when present, else 10^-``pair_decimals``. A present but
    invalid ``tick_size`` is refused rather than replaced by ``pair_decimals``.
    """
    if not isinstance(entry, Mapping):
        return _missing("entry", "missing")
    lot_decimals = _decimals_field(entry, "lot_decimals")
    if isinstance(lot_decimals, NoTrade):
        return lot_decimals
    ordermin = _decimal_text_field(entry, "ordermin")
    if isinstance(ordermin, NoTrade):
        return ordermin
    costmin = _decimal_text_field(entry, "costmin")
    if isinstance(costmin, NoTrade):
        return costmin
    if entry.get("tick_size") is not None:
        tick = _decimal_text_field(entry, "tick_size")
        if isinstance(tick, NoTrade):
            return tick
        if tick == 0:
            return _missing("tick_size", "not_positive")
        source = "tick_size"
    else:
        pair_decimals = _decimals_field(entry, "pair_decimals")
        if isinstance(pair_decimals, NoTrade):
            return _missing("tick_size", "missing") if "pair_decimals" not in entry else pair_decimals
        tick = lot_step(pair_decimals)
        source = "pair_decimals"
    return PairRules(lot_decimals, ordermin, costmin, tick, source)


# --------------------------------------------------------------------------- admission


def admission_refusal(
    envelope: Envelope,
    direction: object,
    *,
    kill_switch_engaged: bool,
    active_locks: Iterable[LockKind],
    open_positions: int,
) -> NoTrade | None:
    """``None`` when a new entry may be sized, else the first rule it breaks.

    Order: LONG only, kill switch released, no active lock (daily before drawdown), fewer
    open positions than the envelope allows (F5: one).
    """
    if not isinstance(kill_switch_engaged, bool):
        raise RiskInputError("kill_switch_engaged", f"must be a bool, got {kill_switch_engaged!r}")
    if isinstance(open_positions, bool) or not isinstance(open_positions, int) or open_positions < 0:
        raise RiskInputError("open_positions", f"must be an int >= 0, got {open_positions!r}")
    locks = set(active_locks)
    for lock in locks:
        if not isinstance(lock, LockKind):
            raise RiskInputError("active_locks", f"must hold LockKind values, got {lock!r}")
    if direction != Direction.LONG.value and direction is not Direction.LONG:
        return NoTrade(NoTradeReason.UNSUPPORTED_DIRECTION, str(direction))
    if kill_switch_engaged:
        return NoTrade(NoTradeReason.KILL_SWITCH_ENGAGED)
    for kind in LockKind:
        if kind in locks:
            return NoTrade(LOCK_REASONS[kind])
    if open_positions >= envelope.max_positions:
        return NoTrade(NoTradeReason.POSITION_ALREADY_OPEN, str(open_positions))
    return None


# --------------------------------------------------------------------------- sizing


@dataclass(frozen=True, slots=True)
class Candidates:
    """The four candidate quantities (exact, rounded toward zero) and the binding one."""

    per_entry_loss: Decimal
    aggregate_loss: Decimal
    notional: Decimal
    cash: Decimal
    binding: BindingConstraint

    @property
    def quantity(self) -> Decimal:
        return min(self.per_entry_loss, self.aggregate_loss, self.notional, self.cash)


@dataclass(frozen=True, slots=True)
class FinalCosts:
    """The planned amounts of one quantity; fees are rounded UP to the cent."""

    quantity: Decimal
    notional: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    planned_loss: Decimal


@dataclass(frozen=True, slots=True)
class EntryPlan:
    """The sizing of one LONG entry, filled as far as it got.

    ``no_trade`` is ``None`` when the entry fits; then ``quantity`` and ``costs`` are set.
    On a refusal every value computed before the refusal is still recorded.
    """

    no_trade: NoTrade | None
    fee_bps: Decimal
    stress_bps: Decimal = PILOT_STRESS_EXIT_SLIPPAGE_BPS
    stress_policy_id: str = PILOT_STRESS_POLICY_ID
    sizing_policy_id: str = SIZING_POLICY_ID
    quote: Quote | None = None
    atr: Decimal | None = None
    rules: PairRules | None = None
    levels: ExitLevels | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    stress_exit_price: Decimal | None = None
    loss_per_unit: Decimal | None = None
    budget: Budget | None = None
    candidates: Candidates | None = None
    lot_quantity: Decimal | None = None
    passes: int = 0
    quantity: Decimal | None = None
    costs: FinalCosts | None = None

    @property
    def opened(self) -> bool:
        return self.no_trade is None


def stress_exit_price(stop: Decimal) -> Decimal:
    """The stressed exit price: ``PILOT_STRESS_EXIT_SLIPPAGE_BPS`` below the stop."""
    with localcontext(RISK_CONTEXT):
        return stop * (1 - PILOT_STRESS_EXIT_SLIPPAGE_BPS / BPS)


def loss_per_unit(ask: Decimal, stop: Decimal, fee_bps: Decimal) -> Decimal:
    """Worst-case LONG loss of one unit: stop distance, stress slippage and both fees."""
    stressed = stress_exit_price(stop)
    with localcontext(RISK_CONTEXT):
        fee = fee_bps / BPS
        return (ask - stop) + (stop - stressed) + ask * fee + stressed * fee


def final_costs(quantity: Decimal, ask: Decimal, stressed_exit: Decimal, fee_bps: Decimal) -> FinalCosts:
    """The planned loss of ``quantity``: price loss to the stressed exit plus fees rounded UP."""
    with localcontext(RISK_CONTEXT):
        fee = fee_bps / BPS
        notional = quantity * ask
        entry_fee = cents_up(notional * fee)
        exit_fee = cents_up(quantity * stressed_exit * fee)
        planned = quantity * (ask - stressed_exit) + entry_fee + exit_fee
    return FinalCosts(quantity, notional, entry_fee, exit_fee, planned)


def _fits(costs: FinalCosts, room: Budget) -> bool:
    with localcontext(RISK_CONTEXT):
        return (
            costs.planned_loss <= room.per_entry_loss_cap
            and costs.planned_loss <= room.aggregate_loss_remaining
            and costs.notional <= room.notional_remaining
            and costs.notional + costs.entry_fee <= room.cash_room
        )


def _below_minimum(quantity: Decimal, ask: Decimal, rules: PairRules) -> NoTrade | None:
    if quantity <= 0 or quantity < rules.ordermin:
        return NoTrade(NoTradeReason.BELOW_ORDER_MINIMUM, f"{plain(quantity)}<{plain(rules.ordermin)}")
    with localcontext(RISK_CONTEXT):
        cost = quantity * ask
    if cost < rules.costmin:
        return NoTrade(NoTradeReason.BELOW_COST_MINIMUM, f"{plain(cost)}<{plain(rules.costmin)}")
    return None


def candidate_quantities(room: Budget, ask: Decimal, per_unit_loss: Decimal, fee_bps: Decimal) -> Candidates:
    """The four quantities allowed by the budgets, each rounded toward zero."""
    with localcontext(QUANTITY_CONTEXT):
        fee = fee_bps / BPS
        values = (
            (BindingConstraint.PER_ENTRY_LOSS, room.per_entry_loss_cap / per_unit_loss),
            (BindingConstraint.AGGREGATE_LOSS, room.aggregate_loss_remaining / per_unit_loss),
            (BindingConstraint.NOTIONAL, room.notional_remaining / ask),
            (BindingConstraint.CASH, room.cash_room / (ask * (1 + fee))),
        )
    binding, _ = min(values, key=lambda item: item[1])
    return Candidates(values[0][1], values[1][1], values[2][1], values[3][1], binding)


def plan_long_entry(
    envelope: Envelope,
    *,
    quote_currency: object,
    bid: object,
    ask: object,
    status: object,
    atr: object,
    pair_entry: object,
    equity: object,
    cash: object,
    open_planned_loss: Decimal,
    open_notional: Decimal,
    fee_bps: Decimal,
) -> EntryPlan:
    """Size one LONG entry on the pair quoted at ``bid``/``ask`` inside ``envelope``.

    Checks, in order: the pair is quoted in the envelope currency, the quote is valid, the
    ATR is valid, the pair rules are complete, the equity and cash are known, the EX-1
    levels exist and survive tick rounding, every budget is positive, the lot-rounded
    quantity meets ``ordermin`` and ``costmin``, and the final quantity fits every limit
    within ``MAX_DOWNWARD_PASSES`` one-lot reductions. Admission (direction, kill switch,
    locks, open position) is ``admission_refusal``'s job and comes first.
    """
    fee = _require_nonnegative("fee_bps", fee_bps)
    _require_nonnegative("open_planned_loss", open_planned_loss)
    _require_nonnegative("open_notional", open_notional)
    plan = EntryPlan(no_trade=None, fee_bps=fee)

    def refuse(reached: EntryPlan, reason: NoTradeReason, detail: str = "") -> EntryPlan:
        return replace(reached, no_trade=NoTrade(reason, detail))

    if not isinstance(quote_currency, str) or quote_currency != envelope.currency:
        return refuse(plan, NoTradeReason.QUOTE_CURRENCY_MISMATCH, str(quote_currency))
    checked = validate_quote(bid, ask, status)
    if isinstance(checked, QuoteProblem):
        return refuse(plan, NoTradeReason.INVALID_QUOTE, checked.value)
    plan = replace(plan, quote=checked)
    checked_atr = validate_atr(atr)
    if isinstance(checked_atr, QuoteProblem):
        return refuse(plan, NoTradeReason.NO_VALID_ATR, checked_atr.value)
    plan = replace(plan, atr=checked_atr)
    rules = parse_pair_rules(pair_entry)
    if isinstance(rules, NoTrade):
        return refuse(plan, rules.reason, rules.detail)
    plan = replace(plan, rules=rules)
    if isinstance(equity, bool) or not isinstance(equity, Decimal) or not equity.is_finite():
        return refuse(plan, NoTradeReason.EQUITY_UNAVAILABLE, "equity:missing")
    if equity <= 0:
        return refuse(plan, NoTradeReason.EQUITY_UNAVAILABLE, "equity:not_positive")
    if isinstance(cash, bool) or not isinstance(cash, Decimal) or not cash.is_finite():
        return refuse(plan, NoTradeReason.EQUITY_UNAVAILABLE, "cash:missing")

    levels = exit_levels(Direction.LONG, checked, checked_atr)
    if levels is None:
        return refuse(plan, NoTradeReason.INVALID_LEVELS, "level_not_positive")
    stop = floor_to_step(levels.stop, rules.tick)
    target = floor_to_step(levels.target, rules.tick)
    plan = replace(plan, levels=levels, stop=stop, target=target)
    if stop <= 0 or stop >= checked.ask:
        return refuse(plan, NoTradeReason.STOP_INVALID_AFTER_TICK, f"stop:{plain(stop)}")
    if target <= checked.ask:
        return refuse(plan, NoTradeReason.INVALID_LEVELS, f"target_not_above_entry:{plain(target)}")
    stressed = stress_exit_price(stop)
    per_unit = loss_per_unit(checked.ask, stop, fee)
    plan = replace(plan, stress_exit_price=stressed, loss_per_unit=per_unit)
    if stressed <= 0 or per_unit <= 0:
        return refuse(plan, NoTradeReason.INVALID_LEVELS, "nonpositive_denominator")

    room = budget(envelope, equity, cash, open_planned_loss, open_notional)
    plan = replace(plan, budget=room)
    if room.per_entry_loss_cap <= 0 or room.aggregate_loss_remaining <= 0:
        return refuse(plan, NoTradeReason.NO_LOSS_BUDGET, f"remaining:{plain(room.aggregate_loss_remaining)}")
    if room.notional_remaining <= 0:
        return refuse(plan, NoTradeReason.NO_NOTIONAL_ROOM, f"remaining:{plain(room.notional_remaining)}")
    if room.cash_room <= 0:
        return refuse(plan, NoTradeReason.INSUFFICIENT_CASH, f"room:{plain(room.cash_room)}")

    candidates = candidate_quantities(room, checked.ask, per_unit, fee)
    quantity = floor_to_lot(candidates.quantity, rules.lot_decimals)
    plan = replace(plan, candidates=candidates, lot_quantity=quantity)
    passes = 0
    while True:
        plan = replace(plan, passes=passes, quantity=quantity, costs=None)
        minimum = _below_minimum(quantity, checked.ask, rules)
        if minimum is not None:
            return refuse(plan, minimum.reason, minimum.detail)
        costs = final_costs(quantity, checked.ask, stressed, fee)
        plan = replace(plan, costs=costs)
        if _fits(costs, room):
            return plan
        if passes == MAX_DOWNWARD_PASSES:
            return refuse(plan, NoTradeReason.SIZING_INCONSISTENT, f"passes:{passes}")
        passes += 1
        quantity -= rules.lot


# --------------------------------------------------------------------------- equity and locks


def entry_cost_basis(quantity: Decimal, entry_ask: Decimal, fee_bps: Decimal) -> Decimal:
    """What buying ``quantity`` at ``entry_ask`` took from cash: notional plus the entry fee in cents."""
    _require_positive("quantity", quantity)
    _require_positive("entry_ask", entry_ask)
    _require_nonnegative("fee_bps", fee_bps)
    with localcontext(RISK_CONTEXT):
        notional = quantity * entry_ask
        return notional + cents(notional * fee_bps / BPS)


def unrealized_mark(quantity: Decimal, bid: Decimal, cost_basis: Decimal, fee_bps: Decimal) -> Decimal:
    """Conservative mark of an open LONG: quantity x bid, minus the exit fee rounded UP, minus the cost basis."""
    _require_positive("quantity", quantity)
    _require_positive("bid", bid)
    _require_positive("cost_basis", cost_basis)
    _require_nonnegative("fee_bps", fee_bps)
    with localcontext(RISK_CONTEXT):
        proceeds = quantity * bid
        return proceeds - cents_up(proceeds * fee_bps / BPS) - cost_basis


def _sum(field: str, values: Iterable[Decimal]) -> Decimal:
    total = Decimal(0)
    with localcontext(RISK_CONTEXT):
        for index, value in enumerate(values):
            total += _require_decimal(f"{field}[{index}]", value)
    return total


def account_equity(assigned: Decimal, realized_nets: Iterable[Decimal], marks: Iterable[Decimal]) -> Decimal:
    """Assigned equity + every realized net + the conservative mark of each open position."""
    _require_positive("assigned", assigned)
    with localcontext(RISK_CONTEXT):
        return assigned + _sum("realized_nets", realized_nets) + _sum("marks", marks)


def available_cash(assigned: Decimal, realized_nets: Iterable[Decimal], open_cost_bases: Iterable[Decimal]) -> Decimal:
    """Assigned equity + realized nets - the cost basis of each open position."""
    _require_positive("assigned", assigned)
    with localcontext(RISK_CONTEXT):
        return assigned + _sum("realized_nets", realized_nets) - _sum("open_cost_bases", open_cost_bases)


def daily_lock_tripped(envelope: Envelope, equity: Decimal, day_start_equity: Decimal) -> bool:
    """True when equity - day_start <= -daily% x day_start."""
    _require_decimal("equity", equity)
    _require_positive("day_start_equity", day_start_equity)
    with localcontext(RISK_CONTEXT):
        return equity - day_start_equity <= -_percent_of(day_start_equity, envelope.daily_loss_pct)


def drawdown_lock_tripped(envelope: Envelope, equity: Decimal, high_water: Decimal) -> bool:
    """True when equity <= high_water x (1 - drawdown%)."""
    _require_decimal("equity", equity)
    _require_positive("high_water", high_water)
    with localcontext(RISK_CONTEXT):
        return equity <= high_water * (1 - envelope.drawdown_pct / HUNDRED)


def tripped_locks(envelope: Envelope, equity: Decimal, day_start_equity: Decimal, high_water: Decimal) -> tuple[LockKind, ...]:
    """The locks the current equity trips (daily first). Reporting only: nothing here clears a lock."""
    tripped: list[LockKind] = []
    if daily_lock_tripped(envelope, equity, day_start_equity):
        tripped.append(LockKind.DAILY_LOSS)
    if drawdown_lock_tripped(envelope, equity, high_water):
        tripped.append(LockKind.DRAWDOWN)
    return tuple(tripped)


def next_high_water(high_water: Decimal, equity: Decimal) -> Decimal:
    """The high-water mark after observing ``equity``: it only rises."""
    _require_positive("high_water", high_water)
    _require_decimal("equity", equity)
    return max(high_water, equity)


# --------------------------------------------------------------------------- settlement


@dataclass(frozen=True, slots=True)
class Settlement:
    """The cent amounts of one closed LONG; ``net == gross - entry_fee - exit_fee`` exactly."""

    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    gross: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    fees: Decimal
    net: Decimal
    outcome: Outcome


def settle_long(quantity: Decimal, entry_ask: Decimal, exit_bid: Decimal, fee_bps: Decimal) -> Settlement:
    """Buy ``quantity`` at ``entry_ask``, sell it at ``exit_bid``; fees per leg at ``fee_bps``."""
    _require_positive("quantity", quantity)
    _require_positive("entry_ask", entry_ask)
    _require_positive("exit_bid", exit_bid)
    _require_nonnegative("fee_bps", fee_bps)
    with localcontext(RISK_CONTEXT):
        fee = fee_bps / BPS
        gross = cents(quantity * (exit_bid - entry_ask))
        entry_fee = cents(quantity * entry_ask * fee)
        exit_fee = cents(quantity * exit_bid * fee)
        fees = entry_fee + exit_fee
        net = gross - fees
    outcome = Outcome.WIN if net > 0 else Outcome.LOSS if net < 0 else Outcome.FLAT
    return Settlement(quantity, entry_ask, exit_bid, gross, entry_fee, exit_fee, fees, net, outcome)
