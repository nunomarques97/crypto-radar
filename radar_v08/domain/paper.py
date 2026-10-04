"""Pretend-money plays of the paper game ("Jogo da IA", DESIGN.md).

A play stakes a fixed amount of pretend money on one direction of one spot pair, enters
at the touch and exits at the touch under the EX-1 initial paper exit policy (below). No
order is ever sent: this module only does the sums. Pure: no I/O, no wall clock, no
configuration, no adapter imports.

Exit policy (EX-1 initial paper policy, docs/EXECUTION_ARCHITECTURE.md section 5)
----------------------------------------------------------------------------------

Fixed in code, never tuned by configuration or a model: the stop sits
``EX1_STOP_ATR_MULTIPLE`` (2) x the 5-minute ATR14 of closed bars from the effective entry
price, the target ``EX1_TARGET_R_MULTIPLE`` (2) x R beyond it (R = entry-to-stop distance)
and the play closes at the latest after ``EX1_MAX_HOLD_MINUTES`` (24 h). LONG enters on the
ask and watches the bid; SHORT enters on the bid and watches the ask. The levels are frozen
on the play: no trailing, no widening. A play closes on the first observed quote that
touches a level, at that quote's executable price, never at the level itself; on one quote
the stop is checked before the target, and both before the time limit.

Numbers
-------

Every price and amount is a ``decimal.Decimal``. Arithmetic runs in ``PAPER_CONTEXT`` (50
significant digits, ROUND_HALF_EVEN); only divisions can round there. Money is then
rounded to cents (``CENT``, ROUND_HALF_EVEN) in exactly three places - the gain at the
mid, the spread cost and the fees - and the net is their exact difference, so the parts
shown to the user always add up to the net and the balance is the start plus the sum of
the nets.

Fills
-----

LONG buys at the entry ask and sells at the exit bid; SHORT sells at the entry bid and
buys back at the exit ask. With ``m`` the mids and ``S`` the stake::

    gross_mid    = S * (m_exit / m_entry - 1)                 LONG
                 = S * (1 - m_exit / m_entry)                 SHORT
    price_result = S * (exit_bid / entry_ask - 1)             LONG
                 = S * (entry_bid - exit_ask) / entry_bid     SHORT
    spread_cost  = gross_mid - price_result
    fees         = S * f + S * exit_price / entry_price * f,  f = fee_bps / 10000

Missing is never zero
---------------------

A quote is valid only when bid and ask are finite, positive, ask >= bid and the pair's
status (when present) is ``online``; anything else is a typed ``QuoteProblem``, never a
zero price. Refusing a new play is a typed ``SkipReason``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import Enum

PAPER_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
CENT = Decimal("0.01")
BPS = Decimal(10000)


class Direction(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class QuoteProblem(Enum):
    MISSING = "missing"
    NOT_A_NUMBER = "not_a_number"
    NOT_FINITE = "not_finite"
    NOT_POSITIVE = "not_positive"
    CROSSED = "crossed"
    NOT_ONLINE = "not_online"


class SkipReason(Enum):
    ALREADY_RECORDED = "already_recorded"
    NO_DIRECTION = "no_direction"
    INVALID_PRICE = "invalid_price"
    NO_VALID_ATR = "no_valid_atr"
    INVALID_LEVELS = "invalid_levels"
    ASSET_ALREADY_OPEN = "asset_already_open"
    MAX_OPEN = "max_open"
    INSUFFICIENT_CASH = "insufficient_cash"


class Outcome(Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    FLAT = "FLAT"


class ExitReason(Enum):
    STOP = "stop"
    TARGET = "target"
    TIME = "time"


#: Identity of the exit policy frozen on each new play.
EX1_POLICY_ID = "ex1_initial_paper_v1"
#: Stop distance in 5-minute ATR14 (closed bars) from the effective entry price.
EX1_STOP_ATR_MULTIPLE = Decimal(2)
#: Target distance in R, the entry-to-stop distance.
EX1_TARGET_R_MULTIPLE = Decimal(2)
#: Maximum hold of a play.
EX1_MAX_HOLD_MINUTES = 24 * 60


class PaperInputError(ValueError):
    """A caller passed a value this module cannot price; ``field`` names it."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(f"{field}: {detail}")
        self.field = field
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Quote:
    """A valid touch: finite, positive, ask >= bid. Build it with ``validate_quote``."""

    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        with localcontext(PAPER_CONTEXT):
            return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class PlayResult:
    """The cent amounts of one closed play; ``net == gross_mid - spread_cost - fees`` exactly."""

    entry_price: Decimal
    exit_price: Decimal
    gross_mid: Decimal
    spread_cost: Decimal
    fees: Decimal
    net: Decimal
    outcome: Outcome


@dataclass(frozen=True, slots=True)
class ExitLevels:
    """The exit plan frozen on a new play; every price is positive."""

    policy_id: str
    atr: Decimal
    entry_price: Decimal
    stop: Decimal
    target: Decimal
    hold_minutes: int


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """What admission needs to know about a play that has no close yet."""

    asset: str
    stake: Decimal


def _price(value: object) -> Decimal | QuoteProblem:
    if value is None:
        return QuoteProblem.MISSING
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        return QuoteProblem.NOT_A_NUMBER
    if isinstance(value, float):
        if not math.isfinite(value):
            return QuoteProblem.NOT_FINITE
        value = Decimal(repr(value))
    number = Decimal(value)
    if not number.is_finite():
        return QuoteProblem.NOT_FINITE
    if number <= 0:
        return QuoteProblem.NOT_POSITIVE
    return number


def validate_quote(bid: object, ask: object, status: object = None) -> Quote | QuoteProblem:
    """A ``Quote`` when the touch can be traded on paper, else why not.

    ``status`` is the pair's trading status as recorded; ``None`` or blank means not
    recorded. A float is taken at its shortest repr (the value SQLite gave back).
    """
    if status is not None:
        if not isinstance(status, str):
            return QuoteProblem.NOT_ONLINE
        if status.strip() and status.strip().lower() != "online":
            return QuoteProblem.NOT_ONLINE
    checked_bid = _price(bid)
    if isinstance(checked_bid, QuoteProblem):
        return checked_bid
    checked_ask = _price(ask)
    if isinstance(checked_ask, QuoteProblem):
        return checked_ask
    if checked_ask < checked_bid:
        return QuoteProblem.CROSSED
    return Quote(checked_bid, checked_ask)


def parse_direction(value: object) -> Direction | None:
    """``LONG``/``SHORT`` exactly, else ``None`` (no play: ``NO_DIRECTION``)."""
    if isinstance(value, str):
        for direction in Direction:
            if value == direction.value:
                return direction
    return None


def _require_amount(field: str, value: object, *, zero_allowed: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise PaperInputError(field, f"must be a finite Decimal, got {value!r}")
    if value < 0 or (value == 0 and not zero_allowed):
        raise PaperInputError(field, f"must be {'>= 0' if zero_allowed else '> 0'}, got {value}")
    return value


def cents(value: Decimal) -> Decimal:
    """``value`` rounded to cents, half to even."""
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN, context=PAPER_CONTEXT)


def settle(direction: Direction, stake: Decimal, fee_bps: Decimal, entry: Quote, exit_: Quote) -> PlayResult:
    """The result of a play that entered on ``entry`` and exits on ``exit_``."""
    if not isinstance(direction, Direction):
        raise PaperInputError("direction", f"must be a Direction, got {direction!r}")
    _require_amount("stake", stake)
    _require_amount("fee_bps", fee_bps, zero_allowed=True)
    for field, quote in (("entry", entry), ("exit", exit_)):
        if not isinstance(quote, Quote):
            raise PaperInputError(field, f"must be a validated Quote, got {quote!r}")
    with localcontext(PAPER_CONTEXT):
        fraction = fee_bps / BPS
        move = exit_.mid / entry.mid
        if direction is Direction.LONG:
            entry_price, exit_price = entry.ask, exit_.bid
            gross_mid = stake * (move - 1)
            price_result = stake * (exit_price / entry_price - 1)
        else:
            entry_price, exit_price = entry.bid, exit_.ask
            gross_mid = stake * (1 - move)
            price_result = stake * (entry_price - exit_price) / entry_price
        exit_notional = stake * exit_price / entry_price
        fees = stake * fraction + exit_notional * fraction
        gross_cents = cents(gross_mid)
        spread_cents = cents(gross_mid - price_result)
        fee_cents = cents(fees)
        net = gross_cents - spread_cents - fee_cents
    outcome = Outcome.WIN if net > 0 else Outcome.LOSS if net < 0 else Outcome.FLAT
    return PlayResult(entry_price, exit_price, gross_cents, spread_cents, fee_cents, net, outcome)


def balance(start_balance: Decimal, nets: Iterable[Decimal]) -> Decimal:
    """The recorded start plus the recorded net of every closed play; nothing else."""
    _require_amount("start_balance", start_balance)
    total = start_balance
    with localcontext(PAPER_CONTEXT):
        for index, net in enumerate(nets):
            if isinstance(net, bool) or not isinstance(net, Decimal) or not net.is_finite():
                raise PaperInputError(f"nets[{index}]", f"must be a finite Decimal, got {net!r}")
            total += net
    return total


def available_cash(current_balance: Decimal, open_positions: Sequence[OpenPosition]) -> Decimal:
    """The balance minus the stakes of the plays still open."""
    with localcontext(PAPER_CONTEXT):
        return current_balance - sum((position.stake for position in open_positions), Decimal(0))


def admit(
    direction: object,
    asset: str,
    open_positions: Sequence[OpenPosition],
    current_balance: Decimal,
    stake: Decimal,
    max_open: int,
) -> SkipReason | None:
    """``None`` when a new play may open, else the first rule it breaks.

    Rules, in order: a LONG/SHORT direction; no open play on the same asset; fewer than
    ``max_open`` open plays; available cash (balance - open stakes) >= stake.
    """
    _require_amount("stake", stake)
    if isinstance(max_open, bool) or not isinstance(max_open, int) or max_open <= 0:
        raise PaperInputError("max_open", f"must be a positive int, got {max_open!r}")
    if parse_direction(direction) is None:
        return SkipReason.NO_DIRECTION
    if any(position.asset == asset for position in open_positions):
        return SkipReason.ASSET_ALREADY_OPEN
    if len(open_positions) >= max_open:
        return SkipReason.MAX_OPEN
    if available_cash(current_balance, open_positions) < stake:
        return SkipReason.INSUFFICIENT_CASH
    return None


def due_at(entry_ts: datetime, hold_minutes: int) -> datetime:
    """When a play entered at ``entry_ts`` is due to close."""
    if entry_ts.tzinfo is None or entry_ts.utcoffset() is None:
        raise PaperInputError("entry_ts", "must be timezone-aware")
    if isinstance(hold_minutes, bool) or not isinstance(hold_minutes, int) or hold_minutes <= 0:
        raise PaperInputError("hold_minutes", f"must be a positive int, got {hold_minutes!r}")
    return entry_ts + timedelta(minutes=hold_minutes)


def validate_atr(value: object) -> Decimal | QuoteProblem:
    """The ATR as a ``Decimal`` when finite and positive, else why not (never a zero)."""
    return _price(value)


def exit_levels(direction: Direction, entry: Quote, atr: Decimal) -> ExitLevels | None:
    """The EX-1 exit plan of a play entering on ``entry``; ``None`` when a level is not positive.

    LONG: entry = ask, stop = entry - 2 x ATR, target = entry + 2R. SHORT mirrored on the bid:
    stop = entry + 2 x ATR, target = entry - 2R. R = |entry - stop|.
    """
    if not isinstance(direction, Direction):
        raise PaperInputError("direction", f"must be a Direction, got {direction!r}")
    if not isinstance(entry, Quote):
        raise PaperInputError("entry", f"must be a validated Quote, got {entry!r}")
    _require_amount("atr", atr)
    with localcontext(PAPER_CONTEXT):
        distance = EX1_STOP_ATR_MULTIPLE * atr
        reward = EX1_TARGET_R_MULTIPLE * distance
        if direction is Direction.LONG:
            entry_price = entry.ask
            stop, target = entry_price - distance, entry_price + reward
        else:
            entry_price = entry.bid
            stop, target = entry_price + distance, entry_price - reward
    if stop <= 0 or target <= 0:
        return None
    return ExitLevels(EX1_POLICY_ID, atr, entry_price, stop, target, EX1_MAX_HOLD_MINUTES)


def exit_decision(
    direction: Direction,
    stop: Decimal,
    target: Decimal,
    due: datetime,
    observed_at: datetime,
    quote: Quote,
) -> ExitReason | None:
    """Why the quote observed at ``observed_at`` closes the play, or ``None`` when it does not.

    LONG watches the bid (stop when bid <= stop, target when bid >= target); SHORT watches
    the ask (stop when ask >= stop, target when ask <= target). Stop is checked before
    target, both before the time limit (``observed_at >= due``).
    """
    if not isinstance(direction, Direction):
        raise PaperInputError("direction", f"must be a Direction, got {direction!r}")
    if not isinstance(quote, Quote):
        raise PaperInputError("quote", f"must be a validated Quote, got {quote!r}")
    _require_amount("stop", stop)
    _require_amount("target", target)
    for field, moment in (("due", due), ("observed_at", observed_at)):
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise PaperInputError(field, "must be a timezone-aware datetime")
    if direction is Direction.LONG:
        if quote.bid <= stop:
            return ExitReason.STOP
        if quote.bid >= target:
            return ExitReason.TARGET
    else:
        if quote.ask >= stop:
            return ExitReason.STOP
        if quote.ask <= target:
            return ExitReason.TARGET
    if observed_at >= due:
        return ExitReason.TIME
    return None
