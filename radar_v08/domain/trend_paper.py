"""Paper trading of the four ported trend rules. Research only.

PAPER ONLY: no order, account or credential exists anywhere in this module. Results are PRE-TAX
and NOT QUALIFIED: paper trading registered rules is a forward observation, not a new trial.

Books: {EUR, USDT} x six rules x fee {0.001, 0.004} per leg = 24 books, each starting at 7000 units
of its quote currency on :data:`PAPER_START` (2026-10-04 UTC). Earlier days are never booked.

* ENS family (``ENS``, ``ENS_VT``, comparator ``BH_5050``): the external ``paper.py`` exactly. Two
  sleeves of 3500 (BTC, ETH), each with its own cash and units. Targets follow robust.py: ENS = share
  of SMA lookbacks {20, 50, 100, 150, 200} with the previous close above the SMA; ENS_VT = ENS x
  min(1, 0.5 / 30-day realized vol). Band (robust.py semantics): trade when |target - held| >= 0.2,
  or target 0 while held > 0, where ``held`` is the fraction set at the last rebalance (price drift
  never triggers a trade). A rebalance sets units to target x sleeve equity / price. ``BH_5050``
  buys both sleeves fully on the first day and never trades again (as in ``paper.py`` its cash
  goes negative by the entry fee).
* Trend5 family (``BTC_TREND5``, ``BTC_TREND5_VT``, comparator ``BH_BTC``): the harness engine's
  units accounting on 100% BTC. The band compares the target with the drifted weight
  (0.15, 0.10 and 0.01 for the benchmark), sells run first and a buy is scaled so fees never
  borrow. Targets are the typed strategy ports (``BtcTrend5``, ``BtcTrend5Vt``, ``BuyAndHold``) evaluated on
  the full BTCUSDT daily history since listing (2017-08-17), bars before the day's open only.

Signals always come from the USDT closes of the days before the fill day. Fills happen at the
day's open: the USDT books at the BTCUSDT/ETHUSDT open, the EUR books at the BTCEUR/ETHEUR open,
or, if that candle is missing, at the USDT open divided by the EURUSDT open; the source is recorded.
Fees are charged on the traded notional; slippage is 0 (:data:`SLIPPAGE_BPS`) because the
reference paper trader has none. The still-open candle contributes its open only.

Ledger: append-only JSON lines, one record per (day, book), all 24 books of a day written together.
Each line is canonical JSON (sorted keys, no spaces, ASCII) with ``schema``, ``seq`` (0-based
position), ``prev`` (the previous record's ``sha256``, ``"genesis"`` first) and ``sha256`` (of the
canonical record without that key). :func:`parse_ledger` refuses a torn, edited, reordered or
incomplete ledger; nothing here ever rewrites one.

Data gaps: no candle is ever interpolated or forward-filled. A missing
BTCEUR/ETHEUR open falls back to the USDT open / EURUSDT open (the source is recorded), but only once
that absence is final by the same rule as a skip below; until then the day waits. A day whose
USDT fill candle, a USDT close of its signal window (the 201-close ENS window, or the full BTCUSDT
history of btc_trend5) or both its EUR pair and EURUSDT candles are missing books no record. It is
recorded as a *skip entry* (one chained line: ``kind`` ``"skip"``, ``date``, ``reason``, ``missing``
symbols and the earliest missing candle's ``gap_day``) only when the absence is final: a later
candle of the same symbol exists and a second, separate request confirms the absence
(:func:`plan_catch_up`, :func:`confirm_plan`). Any other shortfall (data that end before a due
day's open, an absence seen in one response only) is transient: :func:`apply_plan` writes nothing
and the next start retries. Books carry over a skipped day unchanged.

Pure: no I/O, clock or configuration. Floats, not Decimal, so the records reproduce ``paper.py``
and the trend engine to 1e-9 (same operations in the same order).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from .trend_engine import (
    MS_PER_DAY,
    Bar,
    History,
    Panel,
    Strategy,
    day_open_ms,
    utc_day,
)
from .trend_strategies import (
    BTCUSDT,
    ENS_BAND,
    ENS_VOL_TARGET,
    BtcTrend5,
    BtcTrend5Vt,
    BuyAndHold,
    Ens,
    StrategyError,
    ens_target,
    realized_vol,
    registration_of,
    trend_votes,
    weekly_vol_scale,
)

PAPER_START = date(2026, 10, 4)
CAPITAL = 7000.0
FEES: tuple[float, ...] = (0.001, 0.004)
QUOTES: tuple[str, ...] = ("EUR", "USDT")
SLIPPAGE_BPS = 0.0
LEDGER_SCHEMA = 1
GENESIS = "genesis"
BTCUSDT_LISTING_DAY = date(2017, 8, 17)
ENS_WINDOW = 201  # closes d-201 .. d-1: SMA200 plus the 30-day volatility
ENS_HISTORY_DAYS = 260  # closes fetched before the first day to book (window plus margin)
EURUSDT = "EURUSDT"
USDT_SYMBOL: Mapping[str, str] = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
EUR_SYMBOL: Mapping[str, str] = {"BTC": "BTCEUR", "ETH": "ETHEUR"}
ALL_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "BTCEUR", "ETHEUR", EURUSDT)
BANNERS: tuple[str, ...] = ("PAPER ONLY", "PRE-TAX", "NOT QUALIFIED")


class PaperErrorCode(StrEnum):
    MISSING_CANDLE = "MISSING_CANDLE"
    INCOMPLETE_HISTORY = "INCOMPLETE_HISTORY"
    INVALID_MARKET = "INVALID_MARKET"
    INVALID_SIGNAL = "INVALID_SIGNAL"
    LEDGER_TORN = "LEDGER_TORN"
    LEDGER_EDITED = "LEDGER_EDITED"
    LEDGER_INVALID = "LEDGER_INVALID"
    WAITING_FOR_DATA = "WAITING_FOR_DATA"


class PaperError(Exception):
    def __init__(self, code: PaperErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value + (f": {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Rules and books
# ---------------------------------------------------------------------------


class Rule(StrEnum):
    ENS = "ENS"
    ENS_VT = "ENS_VT"
    BH_5050 = "BH_5050"
    BTC_TREND5 = "BTC_TREND5"
    BTC_TREND5_VT = "BTC_TREND5_VT"
    BH_BTC = "BH_BTC"


class Family(StrEnum):
    ENS = "ENS"  # paper.py sleeves, band on the fraction set at the last rebalance
    TREND5 = "TREND5"  # harness engine units, band on the drifted weight


@dataclass(frozen=True)
class RuleSpec:
    rule: Rule
    family: Family
    comparator: Rule | None
    sleeves: Mapping[str, Strategy]  # asset -> typed strategy port (identity, band, weights)
    description: str

    @property
    def assets(self) -> tuple[str, ...]:
        return tuple(self.sleeves)


RULES: Mapping[Rule, RuleSpec] = {
    spec.rule: spec
    for spec in (
        RuleSpec(
            Rule.ENS, Family.ENS, Rule.BH_5050, {"BTC": Ens("BTCUSDT"), "ETH": Ens("ETHUSDT")},
            "robust.py ENS, 50/50 BTC/ETH sleeves, band 0.2 on the fraction at the last rebalance",
        ),
        RuleSpec(
            Rule.ENS_VT, Family.ENS, Rule.BH_5050,
            {"BTC": Ens("BTCUSDT", ENS_VOL_TARGET), "ETH": Ens("ETHUSDT", ENS_VOL_TARGET)},
            "robust.py ENS_VT (vol target 0.5), 50/50 BTC/ETH sleeves, band 0.2",
        ),
        RuleSpec(
            Rule.BH_5050, Family.ENS, None, {"BTC": BuyAndHold("BTCUSDT"), "ETH": BuyAndHold("ETHUSDT")},
            "buy and hold 50/50 BTC/ETH from the first day (ENS comparator)",
        ),
        RuleSpec(
            Rule.BTC_TREND5, Family.TREND5, Rule.BH_BTC, {"BTC": BtcTrend5()},
            "harness btc_trend5, 100% BTC, band 0.15 on the drifted weight",
        ),
        RuleSpec(
            Rule.BTC_TREND5_VT, Family.TREND5, Rule.BH_BTC, {"BTC": BtcTrend5Vt()},
            "harness btc_trend5_vt, 100% BTC, band 0.10 on the drifted weight",
        ),
        RuleSpec(
            Rule.BH_BTC, Family.TREND5, None, {"BTC": BuyAndHold(BTCUSDT)},
            "harness bh_btc, 100% BTC (trend5 comparator)",
        ),
    )
}


def book_id(quote: str, rule: Rule, fee: float) -> str:
    return f"{quote}|{rule.value}|{fee}"


@dataclass(frozen=True)
class Book:
    quote: str
    rule: Rule
    fee: float

    @property
    def id(self) -> str:
        return book_id(self.quote, self.rule, self.fee)

    @property
    def spec(self) -> RuleSpec:
        return RULES[self.rule]

    @property
    def comparator(self) -> str | None:
        comparator = self.spec.comparator
        return None if comparator is None else book_id(self.quote, comparator, self.fee)


BOOKS: tuple[Book, ...] = tuple(Book(q, r, f) for q in QUOTES for r in Rule for f in FEES)
BOOK_IDS: tuple[str, ...] = tuple(b.id for b in BOOKS)
BOOK_BY_ID: Mapping[str, Book] = {b.id: b for b in BOOKS}


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailySeries:
    """One symbol's closed daily bars (ascending) and, separately, the open of the still-open day."""

    symbol: str
    closed: tuple[Bar, ...]
    live_day: date | None = None
    live_open: float | None = None
    _close: dict[date, float] = field(init=False, repr=False, compare=False)
    _open: dict[date, float] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (self.live_day is None) != (self.live_open is None):
            raise PaperError(PaperErrorCode.INVALID_MARKET, f"{self.symbol}: live day and live open go together")
        last: int | None = None
        for bar in self.closed:
            if last is not None and bar.open_time_ms <= last:
                raise PaperError(PaperErrorCode.INVALID_MARKET, f"{self.symbol}: bars are not strictly ascending")
            last = bar.open_time_ms
        if self.live_open is not None and not (math.isfinite(self.live_open) and self.live_open > 0):
            raise PaperError(PaperErrorCode.INVALID_MARKET, f"{self.symbol}: invalid live open")
        if self.live_day is not None and last is not None and day_open_ms(self.live_day) <= last:
            raise PaperError(PaperErrorCode.INVALID_MARKET, f"{self.symbol}: live day is not after the closed bars")
        closes = {utc_day(b.open_time_ms): b.close for b in self.closed}
        opens = {utc_day(b.open_time_ms): b.open for b in self.closed}
        if self.live_day is not None and self.live_open is not None:
            opens[self.live_day] = self.live_open
        object.__setattr__(self, "_close", closes)
        object.__setattr__(self, "_open", opens)

    def close_on(self, day: date) -> float | None:
        return self._close.get(day)

    def open_on(self, day: date) -> float | None:
        return self._open.get(day)

    @property
    def last_open_day(self) -> date | None:
        return max(self._open) if self._open else None


Market = Mapping[str, DailySeries]


def _series(market: Market, symbol: str) -> DailySeries:
    series = market.get(symbol)
    if series is None:
        return DailySeries(symbol, ())
    if series.symbol != symbol:
        raise PaperError(PaperErrorCode.INVALID_MARKET, f"{symbol} holds {series.symbol}")
    return series


def check_btc_history(market: Market) -> None:
    """btc_trend5 needs the full BTCUSDT daily history since listing, with no gap."""
    bars = _series(market, "BTCUSDT").closed
    if not bars or utc_day(bars[0].open_time_ms) != BTCUSDT_LISTING_DAY:
        raise PaperError(PaperErrorCode.INCOMPLETE_HISTORY, f"BTCUSDT history must start at {BTCUSDT_LISTING_DAY}")
    for prev, bar in zip(bars, bars[1:], strict=False):
        if bar.open_time_ms - prev.open_time_ms != MS_PER_DAY:
            raise PaperError(
                PaperErrorCode.MISSING_CANDLE, f"BTCUSDT gap after {utc_day(prev.open_time_ms)}"
            )


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetSignal:
    """What the rules see for one asset on one day: the previous USDT close and the derived values."""

    asset: str
    symbol: str
    close_date: date
    close: float
    values: Mapping[str, float]


@dataclass(frozen=True)
class DaySignals:
    day: date
    ens: Mapping[str, AssetSignal]  # BTC, ETH: ENS, ENS_VT, vol30
    trend5: AssetSignal  # BTC: votes, vol_scale, BTC_TREND5, BTC_TREND5_VT


def _finite_weight(value: float, what: str) -> float:
    if not (math.isfinite(value) and 0.0 <= value <= 1.0):
        raise PaperError(PaperErrorCode.INVALID_SIGNAL, f"{what} = {value!r}")
    return value


def ens_signal(market: Market, asset: str, day: date) -> AssetSignal:
    """ENS and ENS_VT for ``day``, decided on the close of the day before (201 consecutive closes)."""
    symbol = USDT_SYMBOL[asset]
    series = _series(market, symbol)
    prev = day - timedelta(days=1)
    window = [prev - timedelta(days=k) for k in range(ENS_WINDOW - 1, -1, -1)]
    closes: list[float] = []
    for d in window:
        c = series.close_on(d)
        if c is None:
            raise PaperError(PaperErrorCode.MISSING_CANDLE, f"{symbol}: no closed candle on {d} (needed for {day})")
        closes.append(c)
    try:
        ens = ens_target(closes)
        ens_vt = ens_target(closes, ENS_VOL_TARGET)
        vol = realized_vol(closes)
    except (StrategyError, ValueError, ZeroDivisionError) as error:
        raise PaperError(PaperErrorCode.INVALID_SIGNAL, f"{symbol} {day}: {error}") from error
    values = {"ENS": _finite_weight(ens, "ENS"), "ENS_VT": _finite_weight(ens_vt, "ENS_VT"), "vol30": vol}
    return AssetSignal(asset, symbol, prev, closes[-1], values)


def trend5_signal(market: Market, day: date) -> AssetSignal:
    """btc_trend5 and btc_trend5_vt for ``day`` from the BTCUSDT bars that opened before its open."""
    series = _series(market, "BTCUSDT")
    prev = day - timedelta(days=1)
    close = series.close_on(prev)
    if close is None:
        raise PaperError(PaperErrorCode.MISSING_CANDLE, f"BTCUSDT: no closed candle on {prev} (needed for {day})")
    cutoff = day_open_ms(day)
    bars = [b for b in series.closed if b.open_time_ms < cutoff]
    panel = Panel({BTCUSDT: bars}, (BTCUSDT,), end_time=cutoff)
    h = History(panel, len(panel.times) - 1)
    try:
        t5 = float(BtcTrend5().weights(h)[BTCUSDT])
        t5vt = float(BtcTrend5Vt().weights(h)[BTCUSDT])
        votes = trend_votes(h.closes(BTCUSDT))
        scale = weekly_vol_scale(h.bars(BTCUSDT, 208), h.today)
    except (StrategyError, ValueError, ZeroDivisionError) as error:
        raise PaperError(PaperErrorCode.INVALID_SIGNAL, f"BTCUSDT {day}: {error}") from error
    values = {
        "votes": votes,
        "vol_scale": scale,
        "BTC_TREND5": _finite_weight(t5, "BTC_TREND5"),
        "BTC_TREND5_VT": _finite_weight(t5vt, "BTC_TREND5_VT"),
    }
    return AssetSignal("BTC", "BTCUSDT", prev, close, values)


def day_signals(market: Market, day: date) -> DaySignals:
    return DaySignals(day, {a: ens_signal(market, a, day) for a in USDT_SYMBOL}, trend5_signal(market, day))


def _target(book: Book, signals: DaySignals, asset: str) -> float:
    if book.rule in (Rule.ENS, Rule.ENS_VT):
        return signals.ens[asset].values[book.rule.value]
    if book.rule in (Rule.BTC_TREND5, Rule.BTC_TREND5_VT):
        return signals.trend5.values[book.rule.value]
    return 1.0  # buy and hold


def _signal_for(book: Book, signals: DaySignals, asset: str) -> AssetSignal:
    return signals.ens[asset] if book.spec.family is Family.ENS else signals.trend5


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


def fill_price(market: Market, asset: str, quote: str, day: date) -> tuple[float, str]:
    """The day's open in the book's quote currency and where it came from."""
    usdt_symbol = USDT_SYMBOL[asset]
    usdt_open = _series(market, usdt_symbol).open_on(day)
    if usdt_open is None:
        raise PaperError(PaperErrorCode.MISSING_CANDLE, f"{usdt_symbol}: no candle on {day}")
    if quote == "USDT":
        return usdt_open, f"{usdt_symbol} open"
    eur_symbol = EUR_SYMBOL[asset]
    eur_open = _series(market, eur_symbol).open_on(day)
    if eur_open is not None:
        return eur_open, f"{eur_symbol} open"
    fx = _series(market, EURUSDT).open_on(day)
    if fx is not None:
        return usdt_open / fx, f"{usdt_symbol} open / {EURUSDT} open"
    raise PaperError(PaperErrorCode.MISSING_CANDLE, f"no {eur_symbol} or {EURUSDT} candle on {day}")


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sleeve:
    cash: float
    units: float
    held: float  # ENS family: fraction set at the last rebalance; trend5: weight after trading


BookState = Mapping[str, Sleeve]


def initial_state(book: Book) -> dict[str, Sleeve]:
    assets = book.spec.assets
    share = CAPITAL / len(assets)
    return {a: Sleeve(share, 0.0, 0.0) for a in assets}


@dataclass(frozen=True)
class Fill:
    sleeve: Sleeve
    target: float
    held_before: float
    traded: bool
    notional: float
    fee: float
    equity_before: float
    equity_after: float


def step_fraction(sleeve: Sleeve, px: float, tgt: float, fee: float, *, buy_and_hold: bool, first_day: bool) -> Fill:
    """``paper.py`` ``step_book`` for one sleeve (robust.py band on the last rebalanced fraction)."""
    cash, units, held_before = sleeve.cash, sleeve.units, sleeve.held
    sleeve_eq = cash + units * px
    if buy_and_hold:
        tgt = 1.0 if first_day or held_before > 0 else 0.0
        trade = first_day and held_before == 0
    else:
        trade = abs(tgt - held_before) >= ENS_BAND or (tgt == 0 and held_before > 0)
    held = held_before
    notional = fee_paid = 0.0
    if trade:
        want_units = tgt * sleeve_eq / px
        notional = abs(want_units - units) * px
        fee_paid = notional * fee
        cash -= (want_units - units) * px + fee_paid
        units = want_units
        held = tgt
    eq_after = cash + units * px
    return Fill(Sleeve(cash, units, held), tgt, held_before, trade, notional, fee_paid, sleeve_eq, eq_after)


def step_units(sleeve: Sleeve, px: float, tgt: float, band: float, cost: float) -> Fill:
    """The harness engine's day for one spot instrument: drifted-weight band, sell first, buys
    scaled so fees never borrow. Same operations in the same order as ``trend_engine``."""
    cash, units = sleeve.cash, sleeve.units
    e = cash + units * px
    cur = units * px / e if e > 0 else 0.0
    notional = fee_paid = 0.0
    traded = False
    if e > 0 and (units != 0 or tgt != 0):
        if abs(tgt - cur) >= band or (tgt == 0 and cur != 0):
            dq = tgt * e / px - units
            if dq > 0:
                need = 0 + dq * px * (1 + cost)
                scale = min(1.0, max(cash, 0.0) / need) if need > max(cash, 0.0) else 1.0
                dq = dq * scale
            if dq != 0:
                trade_notional = dq * px
                fee_paid = abs(trade_notional) * cost
                cash -= trade_notional + fee_paid
                units = units + dq
                notional = abs(trade_notional)
                traded = True
    eq_after = cash + units * px
    held_after = units * px / eq_after if eq_after > 0 else 0.0
    return Fill(Sleeve(cash, units, held_after), tgt, cur, traded, notional, fee_paid, e, eq_after)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _registration(strategy: Strategy) -> tuple[str | None, str | None]:
    reg = registration_of(strategy)
    return (reg.name, reg.sha256) if reg is not None else (None, None)


def book_day(
    market: Market,
    day: date,
    states: Mapping[str, BookState],
    ts: str,
    start: date = PAPER_START,
    signals: DaySignals | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Sleeve]]]:
    """The 24 records of ``day`` and the books' new states. Raises before returning anything if a
    price or signal is missing, so a day is booked whole or not at all."""
    if day < start:
        raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{day} is before the paper start {start}")
    sig = signals if signals is not None else day_signals(market, day)
    prices = {(a, q): fill_price(market, a, q, day) for a in USDT_SYMBOL for q in QUOTES}
    open_usdt = {a: prices[(a, "USDT")][0] for a in USDT_SYMBOL}
    records: list[dict[str, Any]] = []
    new_states: dict[str, dict[str, Sleeve]] = {}
    first_day = not any(book.id in states for book in BOOKS)  # the first booked day (the start unless skipped)
    for book in BOOKS:
        state = states.get(book.id) or initial_state(book)
        spec = book.spec
        assets: dict[str, Any] = {}
        sleeves: dict[str, Sleeve] = {}
        equity = 0.0
        equity_before = 0.0
        cash_total = 0.0
        for asset, strategy in spec.sleeves.items():
            px, source = prices[(asset, book.quote)]
            tgt = _target(book, sig, asset)
            if spec.family is Family.ENS:
                fill = step_fraction(
                    state[asset], px, tgt, book.fee, buy_and_hold=book.rule is Rule.BH_5050, first_day=first_day
                )
            else:
                fill = step_units(state[asset], px, tgt, float(strategy.band), book.fee + SLIPPAGE_BPS / 10_000)
            asig = _signal_for(book, sig, asset)
            name, sha = _registration(strategy)
            assets[asset] = {
                "symbol": USDT_SYMBOL[asset],
                "registered_name": name,
                "registered_sha256": sha,
                "signal_close_date": asig.close_date.isoformat(),
                "signal_close_usdt": asig.close,
                "signal": dict(asig.values),
                "open_usdt": open_usdt[asset],
                "fill_price": px,
                "fill_source": source,
                "target": fill.target,
                "held_before": fill.held_before,
                "held_after": fill.sleeve.held,
                "traded": fill.traded,
                "traded_notional": fill.notional,
                "fee": fill.fee,
                "cash_after": fill.sleeve.cash,
                "units_after": fill.sleeve.units,
                "equity_after": fill.equity_after,
            }
            sleeves[asset] = fill.sleeve
            equity += fill.equity_after
            equity_before += fill.equity_before
            cash_total += fill.sleeve.cash
        records.append(
            {
                "ts": ts,
                "date": day.isoformat(),
                "book": book.id,
                "quote": book.quote,
                "rule": book.rule.value,
                "family": spec.family.value,
                "comparator": book.comparator,
                "fee_rate": book.fee,
                "slippage_bps": SLIPPAGE_BPS,
                "assets": assets,
                "equity_before": equity_before,
                "cash": cash_total,
                "equity": equity,
            }
        )
        new_states[book.id] = sleeves
    return records, new_states


# ---------------------------------------------------------------------------
# Ledger encoding and verification
# ---------------------------------------------------------------------------


def _reject_constant(token: str) -> float:
    raise PaperError(PaperErrorCode.LEDGER_EDITED, f"JSON constant {token}")


def canonical(obj: Mapping[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _sha(obj: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


SKIP_KIND = "skip"
SKIP_KEYS = frozenset({"kind", "ts", "date", "reason", "missing", "gap_day", "schema", "seq", "prev", "sha256"})


class SkipReason(StrEnum):
    NO_FILL_CANDLE = "NO_FILL_CANDLE"  # a BTCUSDT/ETHUSDT candle of the day itself
    NO_SIGNAL_CLOSE = "NO_SIGNAL_CLOSE"  # a USDT close the day's signal window needs
    NO_EUR_PRICE = "NO_EUR_PRICE"  # neither the EUR pair nor EURUSDT on the day


SKIP_SYMBOLS: Mapping[SkipReason, frozenset[str]] = {
    SkipReason.NO_FILL_CANDLE: frozenset(USDT_SYMBOL.values()),
    SkipReason.NO_SIGNAL_CLOSE: frozenset(USDT_SYMBOL.values()),
    SkipReason.NO_EUR_PRICE: frozenset(EUR_SYMBOL.values()) | {EURUSDT},
}


def skip_text(reason: SkipReason, missing: Sequence[str], gap_day: date) -> str:
    """A short plain-English reason, e.g. ``no BTCUSDT candle on 2026-10-06 (no fill price)``."""
    if reason is SkipReason.NO_FILL_CANDLE:
        return f"no {' and '.join(missing)} candle on {gap_day} (no fill price)"
    if reason is SkipReason.NO_SIGNAL_CLOSE:
        return f"no {' and '.join(missing)} close on {gap_day} (needed by the signal)"
    return f"no {' or '.join(missing)} candle on {gap_day} (no EUR price)"


@dataclass(frozen=True)
class SkippedDay:
    """A paper day that booked no record because a public candle is missing for good."""

    day: date
    reason: SkipReason
    missing: tuple[str, ...]  # sorted symbols
    gap_day: date  # the earliest missing candle

    @property
    def text(self) -> str:
        return skip_text(self.reason, self.missing, self.gap_day)


@dataclass(frozen=True)
class Gap:
    """One candle a due day needs that the market data do not hold."""

    symbol: str
    day: date


@dataclass(frozen=True)
class DayGap:
    """Why a due day cannot be booked: the first blocking reason and its missing candles."""

    day: date
    reason: SkipReason
    gaps: tuple[Gap, ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({g.symbol for g in self.gaps}))

    @property
    def gap_day(self) -> date:
        return min(g.day for g in self.gaps)

    @property
    def text(self) -> str:
        return skip_text(self.reason, self.symbols, self.gap_day)


@dataclass(frozen=True)
class Ledger:
    """A verified ledger: its book records in order, its skipped days, the next ``prev`` and the
    books' last states. ``days`` are the booked days only; ``last_day`` is the last day the ledger
    covers, booked or skipped (the next catch-up starts after it)."""

    records: tuple[Mapping[str, Any], ...]
    tip: str
    days: tuple[date, ...]
    states: Mapping[str, BookState]
    size: int
    skipped: tuple[SkippedDay, ...] = ()

    @property
    def entries(self) -> int:
        """The number of lines: one per book record and one per skipped day."""
        return len(self.records) + len(self.skipped)

    @property
    def last_day(self) -> date | None:
        covered = self.days[-1:] + tuple(s.day for s in self.skipped[-1:])
        return max(covered) if covered else None


EMPTY_LEDGER = Ledger((), GENESIS, (), {}, 0)


def encode_day(ledger: Ledger, records: Sequence[Mapping[str, Any]]) -> bytes:
    """The lines that append ``records`` to ``ledger`` (chained, canonical, LF-terminated)."""
    out = bytearray()
    prev = ledger.tip
    seq = ledger.entries
    for record in records:
        if any(k in record for k in ("schema", "seq", "prev", "sha256")):
            raise PaperError(PaperErrorCode.LEDGER_INVALID, "a record may not set chain fields")
        body = {**record, "schema": LEDGER_SCHEMA, "seq": seq, "prev": prev}
        digest = _sha(body)
        out += canonical({**body, "sha256": digest}) + b"\n"
        prev = digest
        seq += 1
    return bytes(out)


def encode_skip(ledger: Ledger, gap: DayGap, ts: str) -> bytes:
    """The one chained line that records ``gap.day`` as skipped after ``ledger``."""
    body = {
        "kind": SKIP_KIND,
        "ts": ts,
        "date": gap.day.isoformat(),
        "reason": gap.reason.value,
        "missing": list(gap.symbols),
        "gap_day": gap.gap_day.isoformat(),
        "schema": LEDGER_SCHEMA,
        "seq": ledger.entries,
        "prev": ledger.tip,
    }
    return canonical({**body, "sha256": _sha(body)}) + b"\n"


def _iso_day(value: object, what: str) -> date:
    try:
        day = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        day = None
    if day is None or day.isoformat() != value:
        raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{what} is not a YYYY-MM-DD date")
    return day


def _skip_of(obj: Mapping[str, Any], n: int) -> SkippedDay:
    where = f"line {n}"
    if set(obj) != SKIP_KEYS or obj.get("kind") != SKIP_KIND or not isinstance(obj.get("ts"), str):
        raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{where}: not a valid skip entry")
    try:
        reason = SkipReason(obj["reason"])
    except ValueError as error:
        raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{where}: unknown skip reason") from error
    day = _iso_day(obj["date"], f"{where} date")
    gap_day = _iso_day(obj["gap_day"], f"{where} gap_day")
    missing = obj["missing"]
    if (
        not isinstance(missing, list)
        or not missing
        or not all(isinstance(m, str) and m in SKIP_SYMBOLS[reason] for m in missing)
        or missing != sorted(set(missing))
        or gap_day > day
        or (reason is not SkipReason.NO_SIGNAL_CLOSE and gap_day != day)
        or (reason is SkipReason.NO_SIGNAL_CLOSE and gap_day == day)
    ):
        raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{where}: skip entry does not match its reason")
    return SkippedDay(day, reason, tuple(missing), gap_day)


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{what} is not a finite number")
    return float(value)


def _sleeves_of(record: Mapping[str, Any], book: Book, where: str) -> dict[str, Sleeve]:
    assets = record.get("assets")
    if not isinstance(assets, dict) or set(assets) != set(book.spec.assets):
        raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{where}: sleeves do not match {book.id}")
    out: dict[str, Sleeve] = {}
    for asset in book.spec.assets:
        v = assets[asset]
        if not isinstance(v, dict):
            raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{where}: sleeve {asset}")
        out[asset] = Sleeve(
            _number(v.get("cash_after"), f"{where} {asset} cash"),
            _number(v.get("units_after"), f"{where} {asset} units"),
            _number(v.get("held_after"), f"{where} {asset} held"),
        )
    _number(record.get("equity"), f"{where} equity")
    return out


def parse_ledger(data: bytes, start: date = PAPER_START) -> Ledger:
    """Verify every line and the day structure; raise on any torn, edited or incomplete content."""
    return extend_ledger(EMPTY_LEDGER, data, start)


def extend_ledger(ledger: Ledger, data: bytes, start: date = PAPER_START) -> Ledger:
    """``ledger`` followed by the lines in ``data``, verified as :func:`parse_ledger` would verify the
    whole file (chain, canonical form, hashes, complete consecutive days); only ``data`` is parsed."""
    if not data:
        return ledger
    base = ledger.entries
    lines, prev = verify_chain(ledger, data)
    records: list[Mapping[str, Any]] = []
    days = list(ledger.days)
    skipped = list(ledger.skipped)
    states: dict[str, BookState] = dict(ledger.states)
    last = ledger.last_day
    k = 0
    while k < len(lines):  # booked and skipped days, consecutive from the start, each exactly once
        expected = start if last is None else last + timedelta(days=1)
        if "kind" in lines[k]:  # a skipped day: one line, the books carry over unchanged
            skip = _skip_of(lines[k], base + k)
            if skip.day != expected:
                raise PaperError(PaperErrorCode.LEDGER_INVALID, f"line {base + k}: day {skip.day}, expected {expected}")
            skipped.append(skip)
            last = skip.day
            k += 1
            continue
        group = lines[k : k + len(BOOKS)]
        if len(group) < len(BOOKS) or any("kind" in record for record in group):
            raise PaperError(PaperErrorCode.LEDGER_INVALID, "a day does not hold all books")
        try:
            day = date.fromisoformat(str(group[0].get("date")))
        except ValueError as error:
            raise PaperError(PaperErrorCode.LEDGER_INVALID, f"line {base + k}: bad date") from error
        if day != expected:
            raise PaperError(PaperErrorCode.LEDGER_INVALID, f"line {base + k}: day {day}, expected {expected}")
        for book, record in zip(BOOKS, group, strict=True):
            where = f"{day} {book.id}"
            if record.get("date") != day.isoformat() or record.get("book") != book.id:
                raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{where}: books out of order or incomplete day")
            states[book.id] = _sleeves_of(record, book, where)
        records.extend(group)
        days.append(day)
        last = day
        k += len(BOOKS)
    return Ledger(
        ledger.records + tuple(records), prev, tuple(days), states, ledger.size + len(data), tuple(skipped)
    )


def verify_chain(ledger: Ledger, data: bytes) -> tuple[list[Mapping[str, Any]], str]:
    """The lines of non-empty ``data`` checked as a continuation of ``ledger``'s chain (terminated,
    canonical ASCII JSON objects, schema, ``seq``, ``prev`` and ``sha256``) and the new tip. The
    day structure is the caller's to check (:func:`extend_ledger` for the 24 books)."""
    if not data.endswith(b"\n"):
        raise PaperError(PaperErrorCode.LEDGER_TORN, "the last line is not terminated")
    base = ledger.entries
    lines: list[Mapping[str, Any]] = []
    prev = ledger.tip
    for k, line in enumerate(data[:-1].split(b"\n")):
        n = base + k
        try:
            obj = json.loads(line.decode("ascii"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise PaperError(PaperErrorCode.LEDGER_TORN, f"line {n} is not a JSON record") from error
        if not isinstance(obj, dict):
            raise PaperError(PaperErrorCode.LEDGER_EDITED, f"line {n} is not an object")
        try:
            encoded = canonical(obj)
        except (ValueError, RecursionError) as error:  # e.g. a number edited to overflow to inf
            raise PaperError(PaperErrorCode.LEDGER_EDITED, f"line {n} is not canonical") from error
        if encoded != line:
            raise PaperError(PaperErrorCode.LEDGER_EDITED, f"line {n} is not canonical")
        if obj.get("schema") != LEDGER_SCHEMA:
            raise PaperError(PaperErrorCode.LEDGER_INVALID, f"line {n}: unsupported schema {obj.get('schema')!r}")
        if obj.get("seq") != n or obj.get("prev") != prev:
            raise PaperError(PaperErrorCode.LEDGER_EDITED, f"line {n}: chain broken")
        digest = obj.get("sha256")
        body = {key: value for key, value in obj.items() if key != "sha256"}
        if digest != _sha(body):
            raise PaperError(PaperErrorCode.LEDGER_EDITED, f"line {n}: hash mismatch")
        prev = digest
        lines.append(obj)
    return lines, prev


def ledger_states(ledger: Ledger) -> dict[str, BookState]:
    return dict(ledger.states)


# ---------------------------------------------------------------------------
# Catch-up planning
# ---------------------------------------------------------------------------


def first_day_to_book(ledger: Ledger, start: date = PAPER_START) -> date:
    return start if ledger.last_day is None else ledger.last_day + timedelta(days=1)


def fetch_since(ledger: Ledger, today: date, start: date = PAPER_START) -> dict[str, date]:
    """From which day each symbol must be fetched to book every missed day and show today's signal."""
    first = min(first_day_to_book(ledger, start), today)
    history = first - timedelta(days=ENS_HISTORY_DAYS)
    return {
        "BTCUSDT": BTCUSDT_LISTING_DAY,
        "ETHUSDT": history,
        "BTCEUR": first,
        "ETHEUR": first,
        EURUSDT: first,
    }


def last_bookable_day(market: Market) -> date | None:
    """The last day whose open both USDT series show (a closed candle or today's still-open one)."""
    days = [_series(market, s).last_open_day for s in USDT_SYMBOL.values()]
    return None if any(d is None for d in days) else min(d for d in days if d is not None)


def due_days(ledger: Ledger, market: Market, start: date = PAPER_START) -> list[date]:
    last = last_bookable_day(market)
    first = first_day_to_book(ledger, start)
    if last is None or last < first:
        return []
    return [first + timedelta(days=k) for k in range((last - first).days + 1)]


@dataclass(frozen=True)
class CatchUpPlan:
    """The due days of one catch-up in order, the ones that cannot be booked and, when the
    market data do not settle every due day yet, why nothing may be written (``waiting``)."""

    days: tuple[date, ...]
    gaps: Mapping[date, DayGap] = field(default_factory=dict)
    waiting: str | None = None
    confirmed: bool = False  # every gap and fallback was seen again in a second, separate response
    fallbacks: tuple[Gap, ...] = ()  # missing EUR pair opens of booked days (EURUSDT fallback)

    def missing(self) -> list[Gap]:
        """Every missing candle the plan relies on: the gaps of skipped days, then the fallbacks."""
        return [gap for day_gap in self.gaps.values() for gap in day_gap.gaps] + list(self.fallbacks)

    def to_confirm(self) -> dict[str, date]:
        """Per symbol with a missing candle, the earliest missing day (where a second request starts)."""
        since: dict[str, date] = {}
        for gap in self.missing():
            since[gap.symbol] = min(since.get(gap.symbol, gap.day), gap.day)
        return since


def _has_later(market: Market, symbol: str, day: date) -> bool:
    last = _series(market, symbol).last_open_day
    return last is not None and last > day


def day_gap(market: Market, day: date) -> DayGap | None:
    """The first reason ``day`` cannot be booked from ``market`` (fill, then signal, then EUR price),
    or ``None`` when every candle it needs is there. Never imputes a candle."""
    fill = [Gap(s, day) for s in USDT_SYMBOL.values() if _series(market, s).open_on(day) is None]
    if fill:
        return DayGap(day, SkipReason.NO_FILL_CANDLE, tuple(fill))
    signal = signal_gaps(market, day)
    if signal:
        return DayGap(day, SkipReason.NO_SIGNAL_CLOSE, tuple(sorted(signal, key=lambda g: (g.day, g.symbol))))
    eur: list[Gap] = []
    for asset in USDT_SYMBOL:
        if _series(market, EUR_SYMBOL[asset]).open_on(day) is None and _series(market, EURUSDT).open_on(day) is None:
            eur += [Gap(EUR_SYMBOL[asset], day), Gap(EURUSDT, day)]
    return DayGap(day, SkipReason.NO_EUR_PRICE, tuple(dict.fromkeys(eur))) if eur else None


def signal_gaps(market: Market, day: date) -> set[Gap]:
    """The USDT closes :func:`day_signals` needs for ``day`` that the market data do not hold.
    Raises ``INCOMPLETE_HISTORY`` when the BTCUSDT history does not start at its listing day."""
    prev = day - timedelta(days=1)
    signal: set[Gap] = set()
    for symbol in USDT_SYMBOL.values():  # the ENS windows (they hold the previous close too)
        series = _series(market, symbol)
        for k in range(ENS_WINDOW):
            if series.close_on(prev - timedelta(days=k)) is None:
                signal.add(Gap(symbol, prev - timedelta(days=k)))
    # btc_trend5 reads the full BTCUSDT history since listing that opened before the day.
    cutoff = day_open_ms(day)
    bars = [b for b in _series(market, "BTCUSDT").closed if b.open_time_ms < cutoff]
    if not bars or utc_day(bars[0].open_time_ms) != BTCUSDT_LISTING_DAY:
        raise PaperError(PaperErrorCode.INCOMPLETE_HISTORY, f"BTCUSDT history must start at {BTCUSDT_LISTING_DAY}")
    for before, bar in zip(bars, bars[1:], strict=False):
        missing = (bar.open_time_ms - before.open_time_ms) // MS_PER_DAY - 1
        signal.update(Gap("BTCUSDT", utc_day(before.open_time_ms) + timedelta(days=k + 1)) for k in range(missing))
    return signal


def eur_fallbacks(market: Market, day: date) -> tuple[Gap, ...]:
    """The EUR pairs whose ``day`` open is missing while EURUSDT has one: :func:`fill_price` would
    price them at the USDT open / EURUSDT open."""
    if _series(market, EURUSDT).open_on(day) is None:
        return ()
    return tuple(Gap(s, day) for s in EUR_SYMBOL.values() if _series(market, s).open_on(day) is None)


def plan_catch_up(ledger: Ledger, market: Market, start: date = PAPER_START, today: date | None = None) -> CatchUpPlan:
    """The days the next catch-up must settle. With ``today`` (the UTC calendar day of the clock)
    every day up to ``today`` is due, and both USDT series must reach ``today``'s open, otherwise
    the plan waits. Without it the due days end where the USDT data end. A missing candle makes
    its day a gap only if a later candle of the same symbol exists; one at the end of the data
    makes the plan wait (it may still be published). The same holds for a missing EUR pair open of
    a booked day: the EURUSDT fallback is used only once that absence is final and confirmed."""
    first = first_day_to_book(ledger, start)
    horizon = last_bookable_day(market)
    last = today if today is not None else horizon
    if last is None or first > last:
        return CatchUpPlan(())
    days = tuple(first + timedelta(days=k) for k in range((last - first).days + 1))
    if horizon is None or horizon < last:
        return CatchUpPlan(days, waiting=f"the public USDT daily candles do not reach the {last} open yet")
    gaps: dict[date, DayGap] = {}
    fallbacks: list[Gap] = []
    for day in days:
        found = day_gap(market, day)
        missing = found.gaps if found is not None else eur_fallbacks(market, day)
        for gap in missing:
            if not _has_later(market, gap.symbol, gap.day):
                return CatchUpPlan(days, waiting=f"no {gap.symbol} candle on {gap.day} yet (none after it either)")
        if found is not None:
            gaps[day] = found
        else:
            fallbacks.extend(missing)
    return CatchUpPlan(days, gaps, fallbacks=tuple(fallbacks))


def confirm_plan(plan: CatchUpPlan, second: Market) -> CatchUpPlan:
    """``plan`` with its gaps confirmed by ``second`` (a separate response per symbol, from
    :meth:`CatchUpPlan.to_confirm`): every missing candle must be missing there too, with a later
    candle of the same symbol. Any disagreement makes the plan wait; nothing is merged."""
    for gap in plan.missing():
        series = second.get(gap.symbol)
        if series is None or series.symbol != gap.symbol or not _has_later(second, gap.symbol, gap.day):
            return replace(plan, waiting=f"the absence of {gap.symbol} on {gap.day} was not confirmed by a second request")
        if series.open_on(gap.day) is not None:
            return replace(plan, waiting=f"two requests disagree on {gap.symbol} {gap.day}; nothing was booked")
    return replace(plan, confirmed=True)


def apply_plan(
    ledger: Ledger,
    market: Market,
    plan: CatchUpPlan,
    ts: str,
    append: Callable[[bytes], Ledger],
    start: date = PAPER_START,
) -> tuple[list[date], list[date]]:
    """Settle every day of ``plan`` in order: a booked day (24 records) or a confirmed skip entry.
    Raises ``WAITING_FOR_DATA`` before writing when the plan waits or a gap is unconfirmed. Every
    day is computed and verified in memory first, so a missing price or invalid signal writes
    nothing; then each day is one ``append`` (which returns the ledger re-read after the write).
    Returns the booked and the skipped days."""
    if plan.waiting is not None:
        raise PaperError(PaperErrorCode.WAITING_FOR_DATA, plan.waiting)
    if plan.missing() and not plan.confirmed:
        raise PaperError(PaperErrorCode.WAITING_FOR_DATA, "a missing candle was seen in one response only")
    chunks: list[tuple[date, bytes, bool]] = []
    pending = ledger
    for day in plan.days:
        gap = plan.gaps.get(day)
        if gap is not None:
            data = encode_skip(pending, gap, ts)
        else:
            records, _ = book_day(market, day, pending.states, ts, start)
            data = encode_day(pending, records)
        pending = extend_ledger(pending, data, start)
        chunks.append((day, data, gap is not None))
    booked: list[date] = []
    skipped: list[date] = []
    for day, data, is_skip in chunks:
        ledger = append(data)
        if ledger.last_day != day:
            raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{day} was not the last day after the append")
        (skipped if is_skip else booked).append(day)
    return booked, skipped


def catch_up_days(
    ledger: Ledger,
    market: Market,
    ts: str,
    append: Callable[[bytes], Ledger],
    start: date = PAPER_START,
    *,
    today: date | None = None,
    refetch: Callable[[Mapping[str, date]], Market] | None = None,
) -> list[date]:
    """Plan, confirm and settle every due day; returns the booked days (skipped days are in the
    ledger). ``refetch`` makes the second, separate request per symbol that confirms a gap or an
    EURUSDT fallback; without it neither is ever confirmed and nothing is written. Raises ``WAITING_FOR_DATA``
    (nothing written) when the data do not settle every due day yet."""
    plan = plan_catch_up(ledger, market, start, today)
    if plan.waiting is None and plan.missing() and refetch is not None:
        plan = confirm_plan(plan, refetch(plan.to_confirm()))
    return apply_plan(ledger, market, plan, ts, append, start)[0]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BookSummary:
    days: int
    last: str | None
    equity: float
    ret: float
    mdd: float
    trades: int
    fees: float
    held: Mapping[str, float]


def summarize(records: Iterable[Mapping[str, Any]], book_ids: Sequence[str] = BOOK_IDS) -> dict[str, BookSummary]:
    by_book: dict[str, list[Mapping[str, Any]]] = {}
    for r in records:
        if "kind" in r:  # a skip entry is not a book record
            continue
        by_book.setdefault(str(r["book"]), []).append(r)
    out: dict[str, BookSummary] = {}
    for b in book_ids:
        rs = sorted(by_book.get(b, []), key=lambda r: str(r["date"]))
        peak, mdd = CAPITAL, 0.0
        for r in rs:
            peak = max(peak, float(r["equity"]))
            mdd = min(mdd, float(r["equity"]) / peak - 1)
        equity = float(rs[-1]["equity"]) if rs else CAPITAL
        out[b] = BookSummary(
            days=len(rs),
            last=str(rs[-1]["date"]) if rs else None,
            equity=equity,
            ret=equity / CAPITAL - 1,
            mdd=mdd,
            trades=sum(bool(v["traded"]) for r in rs for v in r["assets"].values()),
            fees=sum(float(v["fee"]) for r in rs for v in r["assets"].values()),
            held={a: float(v["held_after"]) for a, v in rs[-1]["assets"].items()} if rs else {},
        )
    return out


@dataclass(frozen=True)
class RegistryStatus:
    chain_ok: bool
    trials: int | None
    holdouts_consumed: tuple[str, ...]
    detail: str = ""


def render_report(
    ledger: Ledger,
    registry: RegistryStatus,
    generated: datetime,
    signals: DaySignals | None,
    *,
    start: date = PAPER_START,
    offline: bool = False,
    signal_error: str | None = None,
) -> str:
    lines = [
        " | ".join(BANNERS),
        "Trend paper trading (research only): no order, no account, no credential. Results are pre-tax "
        "and not qualified; paper trading registered rules is a forward observation, not a trial.",
        f"Generated {generated.isoformat(timespec='seconds')}. Start {start} UTC, capital {CAPITAL:.0f} per book "
        f"(EUR or USDT), fills at the daily open, fee per leg on traded notional, slippage {SLIPPAGE_BPS:g}.",
    ]
    if registry.chain_ok:
        lines.append(
            f"Registry: chain OK, {registry.trials} trials, holdouts consumed: "
            f"{', '.join(registry.holdouts_consumed) or 'none'}."
        )
    else:
        lines.append(f"Registry: CHECK FAILED ({registry.detail}).")
    summ = summarize(ledger.records)
    if not ledger.days and ledger.skipped:
        lines.append(
            f"No paper day booked yet: {len(ledger.skipped)} day(s) skipped for missing public candles (listed "
            f"below). Every book is at {CAPITAL:.2f}."
        )
    elif not ledger.days and generated.date() < start:
        lines.append(
            f"No paper days yet: the first fill happens at the {start} 00:00 UTC open (decided on the "
            f"{start - timedelta(days=1)} close). Every book is at {CAPITAL:.2f}."
        )
    elif not ledger.days:  # past the start: say what is due, never that a fill already happened
        lines.append(
            f"No paper days yet: {start} is the first paper day, not booked yet; the next catch-up books it "
            f"at its 00:00 UTC open (decided on the {start - timedelta(days=1)} close). Every book is at "
            f"{CAPITAL:.2f}."
        )
    else:
        lines.append(f"Paper days booked: {len(ledger.days)} ({ledger.days[0]} .. {ledger.days[-1]}).")
    if ledger.skipped:
        lines.append(
            f"Paper days skipped: {len(ledger.skipped)} (a public candle is missing for good; no record, "
            "the books carry over unchanged):"
        )
        lines += [f"  {s.day}: {s.text}" for s in ledger.skipped]
    lines.append("")
    hdr = (
        f"{'book':5} {'rule':13} {'fee':>5} | {'equity':>10} {'return':>8} {'maxDD':>7} {'trades':>6} {'fees':>8}"
        f" | {'comparator':10} {'cmp eq':>10} {'cmp ret':>8} {'cmp DD':>7}"
    )
    lines += [hdr, "-" * len(hdr)]
    for book in BOOKS:
        if book.comparator is None:
            continue
        m = summ[book.id]
        c = summ[book.comparator]
        comparator = book.spec.comparator.value if book.spec.comparator is not None else ""
        lines.append(
            f"{book.quote:5} {book.rule.value:13} {book.fee * 100:4.1f}% | {m.equity:10.2f} {m.ret * 100:7.2f}% "
            f"{m.mdd * 100:6.2f}% {m.trades:6d} {m.fees:8.2f} | {comparator:10} {c.equity:10.2f} "
            f"{c.ret * 100:7.2f}% {c.mdd * 100:6.2f}%"
        )
    lines.append("")
    if signals is not None:
        lines.append(
            f"Signal for the {signals.day} open (decided on the {signals.day - timedelta(days=1)} close, USDT series):"
        )
        for asset, s in signals.ens.items():
            v = s.values
            lines.append(
                f"  {asset:4} close {s.close:12.2f}  ENS {v['ENS']:.2f}  vol30 {v['vol30'] * 100:5.1f}%  "
                f"ENS_VT {v['ENS_VT']:.2f}"
            )
        t = signals.trend5.values
        lines.append(
            f"  BTC  votes {t['votes']:.2f}  weekly vol scale {t['vol_scale']:.3f}  BTC_TREND5 {t['BTC_TREND5']:.2f}  "
            f"BTC_TREND5_VT {t['BTC_TREND5_VT']:.3f}"
        )
        if signals.day < start:
            lines.append(f"  (Before {start}: informational only, not booked.)")
        elif any(s.day == signals.day for s in ledger.skipped):
            lines.append("  (Skipped day: not booked.)")
        elif ledger.last_day is None or signals.day > ledger.last_day:
            lines.append("  (Not booked yet: run catch-up.)")
    elif offline:
        lines.append("Current signal: not computed (--offline, no market data).")
        if ledger.records:
            last = ledger.records[-len(BOOKS):]
            lines.append(f"Last booked signal ({last[0]['date']} open):")
            for r in last:
                if r["book"] in (book_id("USDT", Rule.ENS, FEES[0]), book_id("USDT", Rule.BTC_TREND5, FEES[0])):
                    for asset, v in r["assets"].items():
                        lines.append(f"  {r['rule']:10} {asset:4} {json.dumps(v['signal'], sort_keys=True)}")
    else:
        lines.append(f"Current signal: unavailable ({signal_error or 'no market data'}).")
    return "\n".join(lines)
