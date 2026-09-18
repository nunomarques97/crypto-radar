"""Pure OC-1 integrity rules for market observations (docs/OPERATING_CONTRACTS.md section 1).

Every check returns a deterministic ``CapabilityResult`` with status PASS, FAIL,
UNKNOWN or N/A and structured reasons. The wall clock is never read: callers pass
``now`` (timezone-aware) explicitly. No I/O, no adapter, UI, model or configuration
imports (docs/FAILURE_AND_QUALITY.md, Architectural quality contract).

Status semantics:

* FAIL - the observation is invalid (hard: non-finite, unit, identity, OHLC, crossed or
  unordered book, future-dated) or not ready (stale receipt/source, clock fault).
* UNKNOWN - the claim cannot be supported (missing observation or metadata, gap,
  insufficient bars, no trades, no source time where one is required). UNKNOWN is never
  converted into a value such as zero.
* N/A - the claim was not made (for example no 1h ATR claim, no futures configured).

Receipt time only bounds when a response was received. When the source supplies no
time the result is labelled ``TimeBasis.RECEIPT_ONLY`` and ``source_time`` stays None;
no exchange timestamp is ever fabricated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

type Number = float | int | Decimal

POLICY_VERSION = "OC-1"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class CheckStatus(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "N/A"


class Capability(Enum):
    SPOT_TICKER = "spot_ticker"
    PAIR_OHLC = "pair_ohlc"
    ATR = "atr"
    BOOK = "book"
    TRADES = "trades"
    FUTURES = "futures"
    CLOCK = "clock"
    METADATA = "metadata"


class TimeBasis(Enum):
    SOURCE = "SOURCE"
    RECEIPT_ONLY = "RECEIPT_ONLY"
    NONE = "NONE"


class InstrumentKind(Enum):
    SPOT = "spot"
    FUTURES = "futures"


class TradingStatus(Enum):
    ONLINE = "online"
    RESTRICTED = "restricted"
    OFFLINE = "offline"


class TradeSide(Enum):
    BUY = "buy"
    SELL = "sell"


class BookSide(Enum):
    BID = "bid"
    ASK = "ask"


class AtrTimeframe(Enum):
    M5 = "5m"
    H1 = "1h"

    @property
    def span(self) -> timedelta:
        match self:
            case AtrTimeframe.M5:
                return timedelta(minutes=5)
            case AtrTimeframe.H1:
                return timedelta(hours=1)


class ReasonCode(Enum):
    # Hard data violations (FAIL, observation invalid).
    INVALID_NUMBER = "invalid_number"
    NONPOSITIVE_PRICE = "nonpositive_price"
    INVALID_SIZE = "invalid_size"
    UNIT_MISMATCH = "unit_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    OHLC_INCOHERENT = "ohlc_incoherent"
    BAR_NOT_CLOSED = "bar_not_closed"
    BAR_MISALIGNED = "bar_misaligned"
    BAR_ORDER = "bar_order"
    CROSSED_QUOTE = "crossed_quote"
    BOOK_SIDE_UNORDERED = "book_side_unordered"
    NAIVE_TIMESTAMP = "naive_timestamp"
    FUTURE_DATED = "future_dated"
    # Readiness failures (FAIL, a new observation may pass).
    STALE_RECEIPT = "stale_receipt"
    STALE_SOURCE = "stale_source"
    MARKET_NOT_ONLINE = "market_not_online"
    CLOCK_NOT_SYNCHRONIZED = "clock_not_synchronized"
    CLOCK_UNCERTAINTY_EXCEEDED = "clock_uncertainty_exceeded"
    CLOCK_BACKWARD_JUMP = "clock_backward_jump"
    # Claim unavailable (UNKNOWN).
    MISSING_OBSERVATION = "missing_observation"
    MISSING_METADATA = "missing_metadata"
    EMPTY_BOOK_SIDE = "empty_book_side"
    SIZE_NOT_COVERED = "size_not_covered"
    BAR_GAP = "bar_gap"
    INSUFFICIENT_BARS = "insufficient_bars"
    NO_TRADES = "no_trades"
    STALE_LAST_TRADE = "stale_last_trade"
    SOURCE_TIME_ABSENT = "source_time_absent"
    CLOCK_SYNC_UNKNOWN = "clock_sync_unknown"
    CLOCK_UNCERTAINTY_UNKNOWN = "clock_uncertainty_unknown"
    CLOCK_UNTRUSTED = "clock_untrusted"

    @property
    def status(self) -> CheckStatus:
        return _REASON_STATUS[self]

    @property
    def hard(self) -> bool:
        return self in HARD_REASONS


HARD_REASONS: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.INVALID_NUMBER,
        ReasonCode.NONPOSITIVE_PRICE,
        ReasonCode.INVALID_SIZE,
        ReasonCode.UNIT_MISMATCH,
        ReasonCode.IDENTITY_MISMATCH,
        ReasonCode.OHLC_INCOHERENT,
        ReasonCode.BAR_NOT_CLOSED,
        ReasonCode.BAR_MISALIGNED,
        ReasonCode.BAR_ORDER,
        ReasonCode.CROSSED_QUOTE,
        ReasonCode.BOOK_SIDE_UNORDERED,
        ReasonCode.NAIVE_TIMESTAMP,
        ReasonCode.FUTURE_DATED,
    }
)
_READINESS_FAILURES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.STALE_RECEIPT,
        ReasonCode.STALE_SOURCE,
        ReasonCode.MARKET_NOT_ONLINE,
        ReasonCode.CLOCK_NOT_SYNCHRONIZED,
        ReasonCode.CLOCK_UNCERTAINTY_EXCEEDED,
        ReasonCode.CLOCK_BACKWARD_JUMP,
    }
)
_REASON_STATUS: dict[ReasonCode, CheckStatus] = {
    code: (
        CheckStatus.FAIL if code in HARD_REASONS or code in _READINESS_FAILURES else CheckStatus.UNKNOWN
    )
    for code in ReasonCode
}


@dataclass(frozen=True, slots=True)
class Reason:
    code: ReasonCode
    field: str
    detail: str


@dataclass(frozen=True, slots=True)
class InstrumentId:
    """Venue instrument identity. Prices are in ``quote``; sizes in ``size_unit``."""

    venue: str
    symbol: str
    kind: InstrumentKind
    base: str
    quote: str
    size_unit: str


@dataclass(frozen=True, slots=True)
class SourceTiming:
    received_at: datetime
    source_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class IntegrityPolicy:
    """OC-1 limits. Every bound is inclusive: exactly at the limit passes."""

    version: str = POLICY_VERSION
    ticker_max_age: timedelta = timedelta(seconds=90)
    ohlc_interval: timedelta = timedelta(minutes=5)
    ohlc_last_close_max_age: timedelta = timedelta(minutes=6)
    atr_period: int = 14
    book_max_age: timedelta = timedelta(seconds=15)
    trades_receipt_max_age: timedelta = timedelta(seconds=15)
    last_trade_max_age: timedelta = timedelta(seconds=60)
    futures_max_age: timedelta = timedelta(seconds=60)
    clock_max_uncertainty: timedelta = timedelta(milliseconds=500)
    source_future_tolerance: timedelta = timedelta(milliseconds=500)

    @property
    def atr_min_bars(self) -> int:
        return self.atr_period + 1


OC1_POLICY = IntegrityPolicy()


@dataclass(frozen=True, slots=True)
class TickerObservation:
    instrument: InstrumentId
    bid: Number
    ask: Number
    last: Number
    price_unit: str
    timing: SourceTiming
    status: TradingStatus | None


@dataclass(frozen=True, slots=True)
class Bar:
    open_time: datetime
    open: Number
    high: Number
    low: Number
    close: Number
    volume: Number


@dataclass(frozen=True, slots=True)
class OhlcSeries:
    instrument: InstrumentId
    interval: timedelta
    bars: tuple[Bar, ...]
    price_unit: str
    volume_unit: str
    timing: SourceTiming


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Number
    size: Number


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    instrument: InstrumentId
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    price_unit: str
    size_unit: str
    timing: SourceTiming


@dataclass(frozen=True, slots=True)
class RequiredDepth:
    side: BookSide
    size: Number


@dataclass(frozen=True, slots=True)
class Trade:
    time: datetime
    price: Number
    size: Number
    side: TradeSide


@dataclass(frozen=True, slots=True)
class TradesObservation:
    instrument: InstrumentId
    trades: tuple[Trade, ...]
    price_unit: str
    size_unit: str
    timing: SourceTiming


@dataclass(frozen=True, slots=True)
class FuturesObservation:
    instrument: InstrumentId
    bid: Number
    ask: Number
    last: Number
    price_unit: str
    timing: SourceTiming
    last_trade_time: datetime | None
    quote_time: datetime | None


@dataclass(frozen=True, slots=True)
class FuturesExpectation:
    instrument: InstrumentId
    observation: FuturesObservation | None


@dataclass(frozen=True, slots=True)
class ClockSample:
    synchronized: bool | None
    offset_uncertainty: timedelta | None
    previous_wall_time: datetime | None


@dataclass(frozen=True, slots=True)
class MetadataValue:
    claim: str
    value: Number | None


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    capability: Capability
    subject: str
    status: CheckStatus
    reasons: tuple[Reason, ...]
    time_basis: TimeBasis
    received_at: datetime | None = None
    source_time: datetime | None = None

    def __post_init__(self) -> None:
        if self.status is CheckStatus.NOT_APPLICABLE:
            if self.reasons:
                raise ValueError("N/A result cannot carry reasons")
        elif self.status is not status_from_reasons(self.reasons):
            raise ValueError("status does not match reasons")

    @property
    def hard_failure(self) -> bool:
        return any(reason.code.hard for reason in self.reasons)


def status_from_reasons(reasons: tuple[Reason, ...]) -> CheckStatus:
    statuses = {reason.code.status for reason in reasons}
    if CheckStatus.FAIL in statuses:
        return CheckStatus.FAIL
    if CheckStatus.UNKNOWN in statuses:
        return CheckStatus.UNKNOWN
    return CheckStatus.PASS


def _result(
    capability: Capability,
    subject: str,
    reasons: list[Reason],
    time_basis: TimeBasis,
    received_at: datetime | None = None,
    source_time: datetime | None = None,
) -> CapabilityResult:
    frozen = tuple(reasons)
    return CapabilityResult(
        capability=capability,
        subject=subject,
        status=status_from_reasons(frozen),
        reasons=frozen,
        time_basis=time_basis,
        received_at=received_at,
        source_time=source_time,
    )


def not_applicable(capability: Capability, subject: str) -> CapabilityResult:
    return CapabilityResult(capability, subject, CheckStatus.NOT_APPLICABLE, (), TimeBasis.NONE)


def _missing(capability: Capability, subject: str, what: str) -> CapabilityResult:
    return _result(
        capability, subject, [Reason(ReasonCode.MISSING_OBSERVATION, what, "not collected")], TimeBasis.NONE
    )


# --- primitive checks -------------------------------------------------------------------


def _require_now(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (UTC)")


def _is_aware(moment: datetime) -> bool:
    return moment.tzinfo is not None and moment.utcoffset() is not None


def _finite(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    return False


def _as_decimal(value: Number) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(value)


def _price(value: Number, name: str, reasons: list[Reason]) -> bool:
    if not _finite(value):
        reasons.append(Reason(ReasonCode.INVALID_NUMBER, name, f"not a finite number: {value!r}"))
        return False
    if value <= 0:
        reasons.append(Reason(ReasonCode.NONPOSITIVE_PRICE, name, f"price must be > 0: {value!r}"))
        return False
    return True


def _size(value: Number, name: str, reasons: list[Reason], *, strictly_positive: bool) -> bool:
    if not _finite(value):
        reasons.append(Reason(ReasonCode.INVALID_NUMBER, name, f"not a finite number: {value!r}"))
        return False
    if value < 0 or (strictly_positive and value == 0):
        bound = "> 0" if strictly_positive else ">= 0"
        reasons.append(Reason(ReasonCode.INVALID_SIZE, name, f"size must be {bound}: {value!r}"))
        return False
    return True


def _identity(actual: InstrumentId, expected: InstrumentId, reasons: list[Reason]) -> None:
    if actual != expected:
        reasons.append(
            Reason(
                ReasonCode.IDENTITY_MISMATCH,
                "instrument",
                f"expected {expected.venue}:{expected.symbol} ({expected.kind.value}), "
                f"got {actual.venue}:{actual.symbol} ({actual.kind.value})",
            )
        )


def _unit(actual: str, expected: str, name: str, reasons: list[Reason]) -> None:
    if actual != expected:
        reasons.append(Reason(ReasonCode.UNIT_MISMATCH, name, f"expected {expected}, got {actual}"))


def _age(now: datetime, moment: datetime) -> timedelta:
    return now - moment


def _source_moment(
    moment: datetime,
    name: str,
    now: datetime,
    policy: IntegrityPolicy,
    max_age: timedelta | None,
    reasons: list[Reason],
    stale_code: ReasonCode = ReasonCode.STALE_SOURCE,
) -> bool:
    """Check an exchange-reported time; returns True when it is aware and not future-dated."""
    if not _is_aware(moment):
        reasons.append(Reason(ReasonCode.NAIVE_TIMESTAMP, name, "timestamp has no timezone"))
        return False
    if moment - now > policy.source_future_tolerance:
        reasons.append(Reason(ReasonCode.FUTURE_DATED, name, f"{moment.isoformat()} is after {now.isoformat()}"))
        return False
    age = _age(now, moment)
    if max_age is not None and age > max_age:
        reasons.append(Reason(stale_code, name, f"age {age.total_seconds()}s > {max_age.total_seconds()}s"))
    return True


def _timing(
    timing: SourceTiming,
    now: datetime,
    policy: IntegrityPolicy,
    receipt_max: timedelta | None,
    source_max: timedelta | None,
    reasons: list[Reason],
) -> TimeBasis:
    received = timing.received_at
    if not _is_aware(received):
        reasons.append(Reason(ReasonCode.NAIVE_TIMESTAMP, "timing.received_at", "timestamp has no timezone"))
    elif received > now:
        reasons.append(
            Reason(ReasonCode.FUTURE_DATED, "timing.received_at", f"{received.isoformat()} is after {now.isoformat()}")
        )
    elif receipt_max is not None and _age(now, received) > receipt_max:
        age = _age(now, received)
        reasons.append(
            Reason(
                ReasonCode.STALE_RECEIPT,
                "timing.received_at",
                f"age {age.total_seconds()}s > {receipt_max.total_seconds()}s",
            )
        )
    if timing.source_time is None:
        return TimeBasis.RECEIPT_ONLY
    _source_moment(timing.source_time, "timing.source_time", now, policy, source_max, reasons)
    return TimeBasis.SOURCE


def _bid_ask(bid: Number, ask: Number, reasons: list[Reason]) -> None:
    bid_ok = _price(bid, "bid", reasons)
    ask_ok = _price(ask, "ask", reasons)
    if bid_ok and ask_ok and bid >= ask:
        reasons.append(Reason(ReasonCode.CROSSED_QUOTE, "bid", f"bid {bid!r} >= ask {ask!r}"))


# --- capability checks ------------------------------------------------------------------


def evaluate_ticker(
    ticker: TickerObservation | None,
    expected: InstrumentId,
    now: datetime,
    policy: IntegrityPolicy = OC1_POLICY,
) -> CapabilityResult:
    """Spot ticker/status: receipt <= 90 s, source age <= 90 s when supplied, uncrossed and finite."""
    _require_now(now)
    subject = expected.symbol
    if ticker is None:
        return _missing(Capability.SPOT_TICKER, subject, "ticker")
    reasons: list[Reason] = []
    _identity(ticker.instrument, expected, reasons)
    _unit(ticker.price_unit, expected.quote, "price_unit", reasons)
    basis = _timing(ticker.timing, now, policy, policy.ticker_max_age, policy.ticker_max_age, reasons)
    _bid_ask(ticker.bid, ticker.ask, reasons)
    _price(ticker.last, "last", reasons)
    if ticker.status is None:
        reasons.append(Reason(ReasonCode.MISSING_METADATA, "status", "trading status not supplied"))
    elif ticker.status is not TradingStatus.ONLINE:
        reasons.append(Reason(ReasonCode.MARKET_NOT_ONLINE, "status", ticker.status.value))
    return _result(
        Capability.SPOT_TICKER, subject, reasons, basis, ticker.timing.received_at, ticker.timing.source_time
    )


def _contiguous_tail(bars: tuple[Bar, ...], interval: timedelta) -> tuple[Bar, ...]:
    if not bars:
        return ()
    start = len(bars) - 1
    while start > 0 and bars[start].open_time - bars[start - 1].open_time == interval:
        start -= 1
    return bars[start:]


def _bar_reasons(series: OhlcSeries, index: int, reasons: list[Reason]) -> None:
    bar = series.bars[index]
    prefix = f"bars[{index}]"
    prices_ok = all(
        [
            _price(bar.open, f"{prefix}.open", reasons),
            _price(bar.high, f"{prefix}.high", reasons),
            _price(bar.low, f"{prefix}.low", reasons),
            _price(bar.close, f"{prefix}.close", reasons),
        ]
    )
    _size(bar.volume, f"{prefix}.volume", reasons, strictly_positive=False)
    if prices_ok and not (bar.low <= min(bar.open, bar.close) and bar.high >= max(bar.open, bar.close)):
        reasons.append(
            Reason(
                ReasonCode.OHLC_INCOHERENT,
                prefix,
                f"o={bar.open!r} h={bar.high!r} l={bar.low!r} c={bar.close!r}",
            )
        )
    if not _is_aware(bar.open_time):
        reasons.append(Reason(ReasonCode.NAIVE_TIMESTAMP, f"{prefix}.open_time", "timestamp has no timezone"))
        return
    if (bar.open_time - _EPOCH) % series.interval != timedelta(0):
        reasons.append(
            Reason(ReasonCode.BAR_MISALIGNED, f"{prefix}.open_time", f"{bar.open_time.isoformat()} off the grid")
        )
    close_time = bar.open_time + series.interval
    received = series.timing.received_at
    if _is_aware(received) and close_time > received:
        reasons.append(
            Reason(
                ReasonCode.BAR_NOT_CLOSED,
                f"{prefix}.open_time",
                f"closes {close_time.isoformat()} after receipt {received.isoformat()}",
            )
        )
    if index > 0:
        previous = series.bars[index - 1].open_time
        if _is_aware(previous) and bar.open_time <= previous:
            reasons.append(Reason(ReasonCode.BAR_ORDER, f"{prefix}.open_time", "not strictly increasing"))


def _series_reasons(
    series: OhlcSeries,
    expected: InstrumentId,
    now: datetime,
    policy: IntegrityPolicy,
    required_bars: int,
) -> list[Reason]:
    reasons: list[Reason] = []
    _identity(series.instrument, expected, reasons)
    _unit(series.price_unit, expected.quote, "price_unit", reasons)
    _unit(series.volume_unit, expected.size_unit, "volume_unit", reasons)
    if series.interval != policy.ohlc_interval:
        reasons.append(
            Reason(
                ReasonCode.UNIT_MISMATCH,
                "interval",
                f"expected {policy.ohlc_interval.total_seconds()}s, got {series.interval.total_seconds()}s",
            )
        )
        return reasons
    _timing(series.timing, now, policy, None, None, reasons)
    if not series.bars:
        reasons.append(Reason(ReasonCode.MISSING_OBSERVATION, "bars", "no bars"))
        return reasons
    for index in range(len(series.bars)):
        _bar_reasons(series, index, reasons)
    if any(reason.code.hard for reason in reasons):
        return reasons
    last_close = series.bars[-1].open_time + series.interval
    _source_moment(
        last_close, "bars[-1].close_time", now, policy, policy.ohlc_last_close_max_age, reasons
    )
    tail = _contiguous_tail(series.bars, series.interval)
    if len(tail) < required_bars:
        code = ReasonCode.INSUFFICIENT_BARS if len(series.bars) < required_bars else ReasonCode.BAR_GAP
        reasons.append(
            Reason(code, "bars", f"{len(tail)} contiguous closed bars, {required_bars} required")
        )
    return reasons


def evaluate_ohlc(
    series: OhlcSeries | None,
    expected: InstrumentId,
    now: datetime,
    policy: IntegrityPolicy = OC1_POLICY,
    required_bars: int = 1,
) -> CapabilityResult:
    """Selected-pair OHLC: closed coherent 5m bars, last close <= 6 min, contiguous claimed window."""
    _require_now(now)
    if series is None:
        return _missing(Capability.PAIR_OHLC, expected.symbol, "ohlc")
    reasons = _series_reasons(series, expected, now, policy, required_bars)
    last_close = series.bars[-1].open_time + series.interval if series.bars else None
    return _result(
        Capability.PAIR_OHLC, expected.symbol, reasons, TimeBasis.SOURCE, series.timing.received_at, last_close
    )


def complete_buckets(bars: tuple[Bar, ...], interval: timedelta, span: timedelta) -> int:
    """Consecutive complete ``span`` buckets built only from the contiguous tail of ``bars``.

    A partial bucket at either end of the tail is not counted: a 1h bucket needs all of
    its own twelve 5m bars, never a relabelled 5m bar.
    """
    if span < interval or span % interval != timedelta(0):
        return 0
    per_bucket = span // interval
    counts: dict[datetime, int] = {}
    for bar in _contiguous_tail(bars, interval):
        start = bar.open_time - ((bar.open_time - _EPOCH) % span)
        counts[start] = counts.get(start, 0) + 1
    return sum(1 for count in counts.values() if count == per_bucket)


def evaluate_atr(
    series: OhlcSeries | None,
    expected: InstrumentId,
    now: datetime,
    timeframe: AtrTimeframe,
    policy: IntegrityPolicy = OC1_POLICY,
) -> CapabilityResult:
    """ATR readiness: >= period+1 valid closed bars at the timeframe, 1h only from its own coverage."""
    _require_now(now)
    subject = timeframe.value
    if series is None:
        return _missing(Capability.ATR, subject, "ohlc")
    reasons = _series_reasons(series, expected, now, policy, 1)
    last_close = series.bars[-1].open_time + series.interval if series.bars else None
    if status_from_reasons(tuple(reasons)) is CheckStatus.PASS:
        buckets = complete_buckets(series.bars, series.interval, timeframe.span)
        if buckets < policy.atr_min_bars:
            reasons.append(
                Reason(
                    ReasonCode.INSUFFICIENT_BARS,
                    "bars",
                    f"{buckets} complete contiguous {subject} bars, {policy.atr_min_bars} required",
                )
            )
    return _result(Capability.ATR, subject, reasons, TimeBasis.SOURCE, series.timing.received_at, last_close)


def _book_side(levels: tuple[BookLevel, ...], side: BookSide, reasons: list[Reason]) -> bool:
    name = f"{side.value}s"
    if not levels:
        reasons.append(Reason(ReasonCode.EMPTY_BOOK_SIDE, name, "no levels"))
        return False
    valid = True
    for index, level in enumerate(levels):
        valid = _price(level.price, f"{name}[{index}].price", reasons) and valid
        valid = _size(level.size, f"{name}[{index}].size", reasons, strictly_positive=False) and valid
    if not valid:
        return False
    for index in range(1, len(levels)):
        previous, current = levels[index - 1].price, levels[index].price
        ordered = current < previous if side is BookSide.BID else current > previous
        if not ordered:
            reasons.append(
                Reason(ReasonCode.BOOK_SIDE_UNORDERED, f"{name}[{index}].price", f"{current!r} after {previous!r}")
            )
            return False
    return True


def evaluate_book(
    book: BookSnapshot | None,
    expected: InstrumentId,
    now: datetime,
    policy: IntegrityPolicy = OC1_POLICY,
    required_depth: RequiredDepth | None = None,
) -> CapabilityResult:
    """Book: receipt (and source, if supplied) <= 15 s, ordered sides, uncrossed, depth covered."""
    _require_now(now)
    subject = expected.symbol
    if book is None:
        return _missing(Capability.BOOK, subject, "book")
    reasons: list[Reason] = []
    _identity(book.instrument, expected, reasons)
    _unit(book.price_unit, expected.quote, "price_unit", reasons)
    _unit(book.size_unit, expected.size_unit, "size_unit", reasons)
    basis = _timing(book.timing, now, policy, policy.book_max_age, policy.book_max_age, reasons)
    bids_ok = _book_side(book.bids, BookSide.BID, reasons)
    asks_ok = _book_side(book.asks, BookSide.ASK, reasons)
    if bids_ok and asks_ok and book.bids[0].price >= book.asks[0].price:
        reasons.append(
            Reason(
                ReasonCode.CROSSED_QUOTE,
                "bids[0].price",
                f"best bid {book.bids[0].price!r} >= best ask {book.asks[0].price!r}",
            )
        )
    if required_depth is not None:
        levels, side_ok = (book.bids, bids_ok) if required_depth.side is BookSide.BID else (book.asks, asks_ok)
        if _size(required_depth.size, "required_depth.size", reasons, strictly_positive=True) and side_ok:
            depth = sum((_as_decimal(level.size) for level in levels), Decimal(0))
            if depth < _as_decimal(required_depth.size):
                reasons.append(
                    Reason(
                        ReasonCode.SIZE_NOT_COVERED,
                        f"{required_depth.side.value}s",
                        f"depth {depth} < required {required_depth.size!r}",
                    )
                )
    return _result(Capability.BOOK, subject, reasons, basis, book.timing.received_at, book.timing.source_time)


def evaluate_trades(
    trades: TradesObservation | None,
    expected: InstrumentId,
    now: datetime,
    policy: IntegrityPolicy = OC1_POLICY,
    active_trade_claim: bool = True,
) -> CapabilityResult:
    """Trades: receipt <= 15 s; latest trade <= 60 s for active-trade claims; none is UNKNOWN."""
    _require_now(now)
    subject = expected.symbol
    if trades is None:
        return _missing(Capability.TRADES, subject, "trades")
    reasons: list[Reason] = []
    _identity(trades.instrument, expected, reasons)
    _unit(trades.price_unit, expected.quote, "price_unit", reasons)
    _unit(trades.size_unit, expected.size_unit, "size_unit", reasons)
    _timing(trades.timing, now, policy, policy.trades_receipt_max_age, None, reasons)
    latest: datetime | None = None
    for index, trade in enumerate(trades.trades):
        _price(trade.price, f"trades[{index}].price", reasons)
        _size(trade.size, f"trades[{index}].size", reasons, strictly_positive=True)
        if _source_moment(trade.time, f"trades[{index}].time", now, policy, None, reasons):
            latest = trade.time if latest is None or trade.time > latest else latest
    if not trades.trades:
        reasons.append(Reason(ReasonCode.NO_TRADES, "trades", "no trades: unavailable, not zero"))
    elif active_trade_claim and latest is not None:
        _source_moment(
            latest, "latest_trade", now, policy, policy.last_trade_max_age, reasons, ReasonCode.STALE_LAST_TRADE
        )
    basis = TimeBasis.SOURCE if latest is not None else TimeBasis.RECEIPT_ONLY
    return _result(Capability.TRADES, subject, reasons, basis, trades.timing.received_at, latest)


def evaluate_futures(
    futures: FuturesObservation | None,
    expected: InstrumentId,
    now: datetime,
    policy: IntegrityPolicy = OC1_POLICY,
) -> CapabilityResult:
    """One futures instrument, independently: receipt and reported trade/quote times <= 60 s."""
    _require_now(now)
    subject = expected.symbol
    if futures is None:
        return _missing(Capability.FUTURES, subject, "futures")
    reasons: list[Reason] = []
    if expected.kind is not InstrumentKind.FUTURES:
        reasons.append(Reason(ReasonCode.IDENTITY_MISMATCH, "instrument.kind", "expected a futures instrument"))
    _identity(futures.instrument, expected, reasons)
    _unit(futures.price_unit, expected.quote, "price_unit", reasons)
    _timing(futures.timing, now, policy, policy.futures_max_age, policy.futures_max_age, reasons)
    _bid_ask(futures.bid, futures.ask, reasons)
    _price(futures.last, "last", reasons)
    reported: list[datetime] = []
    for name, moment in (("quote_time", futures.quote_time), ("last_trade_time", futures.last_trade_time)):
        if moment is not None and _source_moment(moment, name, now, policy, policy.futures_max_age, reasons):
            reported.append(moment)
    if futures.quote_time is None and futures.last_trade_time is None and futures.timing.source_time is None:
        reasons.append(
            Reason(ReasonCode.SOURCE_TIME_ABSENT, "quote_time", "no reported time: current activity unproven")
        )
        basis = TimeBasis.RECEIPT_ONLY
    else:
        basis = TimeBasis.SOURCE
    source_time = max(reported) if reported else futures.timing.source_time
    return _result(Capability.FUTURES, subject, reasons, basis, futures.timing.received_at, source_time)


def evaluate_clock(sample: ClockSample, now: datetime, policy: IntegrityPolicy = OC1_POLICY) -> CapabilityResult:
    """Clock: synchronised, UTC offset uncertainty <= 500 ms, no backward wall-clock jump."""
    _require_now(now)
    reasons: list[Reason] = []
    if sample.synchronized is None:
        reasons.append(Reason(ReasonCode.CLOCK_SYNC_UNKNOWN, "synchronized", "synchronisation unknown"))
    elif not sample.synchronized:
        reasons.append(Reason(ReasonCode.CLOCK_NOT_SYNCHRONIZED, "synchronized", "clock not synchronised"))
    uncertainty = sample.offset_uncertainty
    if uncertainty is None:
        reasons.append(Reason(ReasonCode.CLOCK_UNCERTAINTY_UNKNOWN, "offset_uncertainty", "not measured"))
    elif uncertainty < timedelta(0):
        reasons.append(Reason(ReasonCode.INVALID_NUMBER, "offset_uncertainty", "negative uncertainty"))
    elif uncertainty > policy.clock_max_uncertainty:
        reasons.append(
            Reason(
                ReasonCode.CLOCK_UNCERTAINTY_EXCEEDED,
                "offset_uncertainty",
                f"{uncertainty.total_seconds()}s > {policy.clock_max_uncertainty.total_seconds()}s",
            )
        )
    previous = sample.previous_wall_time
    if previous is not None:
        if not _is_aware(previous):
            reasons.append(Reason(ReasonCode.NAIVE_TIMESTAMP, "previous_wall_time", "timestamp has no timezone"))
        elif now < previous:
            reasons.append(
                Reason(
                    ReasonCode.CLOCK_BACKWARD_JUMP,
                    "previous_wall_time",
                    f"now {now.isoformat()} < previous {previous.isoformat()}",
                )
            )
    return _result(Capability.CLOCK, "utc", reasons, TimeBasis.NONE)


def evaluate_metadata(value: MetadataValue) -> CapabilityResult:
    """Optional metadata (beta, unlock, listing, funding): missing is UNKNOWN for that claim only."""
    reasons: list[Reason] = []
    if value.value is None:
        reasons.append(Reason(ReasonCode.MISSING_METADATA, value.claim, "not supplied"))
    elif not _finite(value.value):
        reasons.append(Reason(ReasonCode.INVALID_NUMBER, value.claim, f"not a finite number: {value.value!r}"))
    return _result(Capability.METADATA, value.claim, reasons, TimeBasis.NONE)


# --- snapshot report --------------------------------------------------------------------

FRESHNESS_SENSITIVE: frozenset[Capability] = frozenset(
    {
        Capability.SPOT_TICKER,
        Capability.PAIR_OHLC,
        Capability.ATR,
        Capability.BOOK,
        Capability.TRADES,
        Capability.FUTURES,
    }
)
_SPOT_REQUIRED: frozenset[Capability] = frozenset({Capability.SPOT_TICKER, Capability.PAIR_OHLC, Capability.BOOK})
_SPOT_CAPABILITIES: frozenset[Capability] = _SPOT_REQUIRED | {Capability.ATR, Capability.TRADES}


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    selected: InstrumentId
    ticker: TickerObservation | None
    ohlc: OhlcSeries | None
    book: BookSnapshot | None
    trades: TradesObservation | None
    clock: ClockSample
    futures: tuple[FuturesExpectation, ...] = ()
    metadata: tuple[MetadataValue, ...] = ()
    ohlc_required_bars: int = 1
    claim_atr_1h: bool = False
    active_trade_claim: bool = True
    required_depth: RequiredDepth | None = None


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    evaluated_at: datetime
    policy_version: str
    results: tuple[CapabilityResult, ...] = field(default_factory=tuple)

    def result(self, capability: Capability, subject: str) -> CapabilityResult:
        for item in self.results:
            if item.capability is capability and item.subject == subject:
                return item
        raise KeyError(f"{capability.value}:{subject}")

    @property
    def clock(self) -> CapabilityResult:
        return self.result(Capability.CLOCK, "utc")

    @property
    def blocking_results(self) -> tuple[CapabilityResult, ...]:
        """Results that block the selected spot opportunity; futures and metadata never do."""
        blocking: list[CapabilityResult] = []
        for item in self.results:
            required = item.capability in _SPOT_REQUIRED or item.capability is Capability.CLOCK
            if required and item.status is not CheckStatus.PASS:
                blocking.append(item)
            elif item.capability in _SPOT_CAPABILITIES and item.status is CheckStatus.FAIL:
                blocking.append(item)
        return tuple(blocking)

    @property
    def opportunity_blocked(self) -> bool:
        return bool(self.blocking_results)

    @property
    def eligible_futures(self) -> tuple[str, ...]:
        """Futures instruments that passed on their own; never 'some fresh' treated as all."""
        return tuple(
            item.subject
            for item in self.results
            if item.capability is Capability.FUTURES and item.status is CheckStatus.PASS
        )


def _clock_untrusted(result: CapabilityResult, clock: CapabilityResult) -> CapabilityResult:
    if result.capability not in FRESHNESS_SENSITIVE or result.status is not CheckStatus.PASS:
        return result
    reasons = list(result.reasons)
    reasons.append(Reason(ReasonCode.CLOCK_UNTRUSTED, "clock", f"clock is {clock.status.value}"))
    return _result(
        result.capability, result.subject, reasons, result.time_basis, result.received_at, result.source_time
    )


def evaluate_snapshot(
    snapshot: MarketSnapshot, now: datetime, policy: IntegrityPolicy = OC1_POLICY
) -> IntegrityReport:
    """Evaluate every capability of one snapshot. Deterministic for equal inputs and ``now``."""
    _require_now(now)
    selected = snapshot.selected
    clock = evaluate_clock(snapshot.clock, now, policy)
    results: list[CapabilityResult] = [
        evaluate_ticker(snapshot.ticker, selected, now, policy),
        evaluate_ohlc(snapshot.ohlc, selected, now, policy, snapshot.ohlc_required_bars),
        evaluate_atr(snapshot.ohlc, selected, now, AtrTimeframe.M5, policy),
        (
            evaluate_atr(snapshot.ohlc, selected, now, AtrTimeframe.H1, policy)
            if snapshot.claim_atr_1h
            else not_applicable(Capability.ATR, AtrTimeframe.H1.value)
        ),
        evaluate_book(snapshot.book, selected, now, policy, snapshot.required_depth),
        evaluate_trades(snapshot.trades, selected, now, policy, snapshot.active_trade_claim),
    ]
    if snapshot.futures:
        results.extend(
            evaluate_futures(item.observation, item.instrument, now, policy) for item in snapshot.futures
        )
    else:
        results.append(not_applicable(Capability.FUTURES, "*"))
    results.extend(evaluate_metadata(item) for item in snapshot.metadata)
    if clock.status is not CheckStatus.PASS:
        results = [_clock_untrusted(item, clock) for item in results]
    results.append(clock)
    return IntegrityReport(evaluated_at=now, policy_version=policy.version, results=tuple(results))
