"""Daily spot backtest engine of the trend research.

A port of the 2026-10-03 feasibility harness (``harness/engine.py``), restricted to spot: every
ported rule and the buy-and-hold benchmark trade spot only, so perpetuals, dated futures, funding
and 4h bars are not ported and an instrument written ``KIND:SYMBOL`` is refused.

Timing: on day ``i`` the strategy sees only bars that opened before day ``i``'s open (they have
closed by then) and returns target weights; the engine trades at day ``i``'s open. The daily return
of day ``i`` is equity(open ``i+1``) / equity(open ``i``, before trading) - 1, so a day's fees count in
that day's return.

Accounting:

* ``UNITS`` (realistic): positions are held in units and drift with prices between rebalances.
  An instrument trades only when ``|target - drifted weight| >= band`` or the target is 0 while a
  position is held. Fee plus slippage is charged per leg on the traded notional. Sells run before
  buys so their cash funds the buys, and buys are scaled down so fees never borrow. Cash earns 0.
* ``CONSTANT_WEIGHT``: robust.py's model, where the held fraction stays constant every day at no
  cost. It exists only to prove parity with robust.py and must not be used for decisions.

Tax (approximate, Portugal): see :class:`FifoTax`. A year's tax is paid at the first open of the
next year and the final partial year at the end of the run, selling spot pro rata when cash is
short. It is computed in the quote currency (USDT, not EUR); unrealized gains at the end are not
taxed, and neither is the gain realized by a sale that funds the final bill (reference behaviour).

Pure: no I/O, no clock, no configuration. Floats, not Decimal, so the parity tests can reproduce
the reference implementation to 1e-9 (same operations in the same order, ``sum`` included).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Protocol

from .trend_metrics import Performance, performance

MS_PER_DAY = 86_400_000
EPOCH = date(1970, 1, 1)
DEV_END = date(2025, 10, 2)
HOLDOUT_START = date(2025, 10, 3)
PT_SHORT_TERM_RATE = 0.28
PT_EXEMPT_HOLDING_DAYS = 365
GROSS_TOLERANCE = 1e-9
LOT_EPSILON = 1e-15
AUDIT_TOLERANCE = 1e-12


class EngineErrorCode(StrEnum):
    INVALID_BAR = "INVALID_BAR"
    NON_MONOTONIC = "NON_MONOTONIC"
    NOT_SPOT = "NOT_SPOT"
    UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
    INVALID_WEIGHT = "INVALID_WEIGHT"
    NO_BAR_TO_TRADE = "NO_BAR_TO_TRADE"
    GROSS_EXCEEDED = "GROSS_EXCEEDED"
    WINDOW_TOO_SHORT = "WINDOW_TOO_SHORT"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    LOOKAHEAD = "LOOKAHEAD"


class EngineError(ValueError):
    def __init__(self, code: EngineErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value + (f": {detail}" if detail else ""))


def utc_day(open_time_ms: int) -> date:
    return EPOCH + timedelta(days=open_time_ms // MS_PER_DAY)


def day_open_ms(day: date) -> int:
    return (day - EPOCH).days * MS_PER_DAY


def is_spot(instrument: str) -> bool:
    return ":" not in instrument


# ---------------------------------------------------------------------------
# Bars and panel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    """One UTC daily candle: open time (ms, midnight-aligned), open, high, low, close."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float


def _price(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EngineError(EngineErrorCode.INVALID_BAR, f"{what} is not a number: {value!r}")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise EngineError(EngineErrorCode.INVALID_BAR, f"{what} must be finite and positive: {value!r}")
    return number


def bars_from_rows(rows: Sequence[Sequence[object]]) -> tuple[Bar, ...]:
    """Validate ``[openTimeMs, open, high, low, close]`` rows (extra columns ignored)."""
    out: list[Bar] = []
    for n, row in enumerate(rows):
        if len(row) < 5:
            raise EngineError(EngineErrorCode.INVALID_BAR, f"row {n} has {len(row)} columns")
        t = row[0]
        if isinstance(t, bool) or not isinstance(t, int) or t % MS_PER_DAY:
            raise EngineError(EngineErrorCode.INVALID_BAR, f"row {n} open time is not a UTC midnight: {t!r}")
        if out and t <= out[-1].open_time_ms:
            raise EngineError(EngineErrorCode.NON_MONOTONIC, f"row {n} open time {t} after {out[-1].open_time_ms}")
        out.append(Bar(t, _price(row[1], "open"), _price(row[2], "high"), _price(row[3], "low"), _price(row[4], "close")))
    return tuple(out)


class Panel:
    """Daily bars of a set of instruments aligned on the union of the tradeable instruments' open times."""

    def __init__(
        self, rows: Mapping[str, Sequence[Bar]], tradeable: Sequence[str], end_time: int | None = None
    ) -> None:
        for inst in rows:
            if not is_spot(inst):
                raise EngineError(EngineErrorCode.NOT_SPOT, f"{inst}: only spot instruments are ported")
        for inst in tradeable:
            if inst not in rows:
                raise EngineError(EngineErrorCode.UNKNOWN_INSTRUMENT, f"{inst} has no rows")
        self.rows: dict[str, tuple[Bar, ...]] = {k: tuple(v) for k, v in rows.items()}
        self.tradeable = tuple(tradeable)
        times = sorted({b.open_time_ms for k in self.tradeable for b in self.rows[k]})
        if end_time is not None:  # truncated panel for the lookahead audit
            times = [t for t in times if t < end_time] + [end_time]
        self.times = tuple(times)
        self.dates = tuple(utc_day(t) for t in times)
        self._t = {k: [b.open_time_ms for b in v] for k, v in self.rows.items()}
        self.open_at: dict[str, tuple[float | None, ...]] = {}
        for k in self.tradeable:
            by_time = {b.open_time_ms: b.open for b in self.rows[k]}
            self.open_at[k] = tuple(by_time.get(t) for t in self.times)

    def truncated(self, i: int) -> Panel:
        """A panel that physically contains only the bars that opened before day ``i``'s open."""
        t = self.times[i]
        return Panel({k: [b for b in v if b.open_time_ms < t] for k, v in self.rows.items()}, self.tradeable, end_time=t)

    def price(self, inst: str, i: int) -> float:
        """Open of day ``i``, forward-filled from the previous close if the instrument has no bar that day."""
        p = self.open_at[inst][i]
        if p is not None:
            return p
        k = bisect_left(self._t[inst], self.times[i])
        if not k:
            raise EngineError(EngineErrorCode.NO_BAR_TO_TRADE, f"{inst} has no price on {self.dates[i]}")
        return self.rows[inst][k - 1].close

    def window_days(self, window: Window, warmup: int) -> list[int]:
        """Indices of the evaluated days: from ``warmup``, inside the window, each with a next open."""
        lo = window.start or date.min
        hi = window.end or date.max
        return [i for i in range(warmup, len(self.times) - 1) if lo <= self.dates[i] <= hi]


@dataclass(frozen=True)
class Window:
    """Inclusive range of evaluated days; ``None`` is open-ended."""

    start: date | None = None
    end: date | None = None


DEV = Window(None, DEV_END)
HOLDOUT = Window(HOLDOUT_START, None)
ALL = Window(None, None)
SPLITS: Mapping[str, Window] = {"dev": DEV, "holdout": HOLDOUT, "all": ALL}


class History:
    """What a strategy may see on ``today``: copies of the bars that opened before today's open."""

    def __init__(self, panel: Panel, i: int) -> None:
        self._panel = panel
        self._i = i
        self.today = panel.dates[i]
        self._now = panel.times[i]

    def _k(self, inst: str) -> int:
        return bisect_left(self._panel._t[inst], self._now)

    def nbars(self, inst: str) -> int:
        return self._k(inst)

    def bars(self, inst: str, n: int | None = None) -> list[Bar]:
        k = self._k(inst)
        return list(self._panel.rows[inst][(0 if n is None else max(0, k - n)) : k])

    def closes(self, inst: str, n: int | None = None) -> list[float]:
        return [b.close for b in self.bars(inst, n)]

    def opens(self, inst: str, n: int | None = None) -> list[float]:
        return [b.open for b in self.bars(inst, n)]


class Strategy(Protocol):
    """A pure daily rule: same history, same weights; no hidden state."""

    @property
    def name(self) -> str: ...

    @property
    def universe(self) -> tuple[str, ...]: ...

    @property
    def warmup_days(self) -> int: ...

    @property
    def band(self) -> float: ...

    @property
    def max_gross(self) -> float: ...

    def weights(self, h: History) -> Mapping[str, float]: ...


# ---------------------------------------------------------------------------
# Tax
# ---------------------------------------------------------------------------


@dataclass
class TaxLot:
    """An open FIFO lot: signed quantity, unit cost including the buy fee, and acquisition day."""

    qty: float
    unit_cost: float
    day: date


class FifoTax:
    """Approximate Portuguese tax on crypto (spot).

    Realized gains on lots held under 365 days are taxed at 28%; lots held 365 days or more are
    exempt, and so are their losses. Lots are matched FIFO, fees reduce the gain, and realized
    results net per calendar year: losses offset gains only within the same year (no carry-forward).
    Quote currency (USDT), not EUR. Crypto-to-crypto deferral and holding-period resets are not
    modelled (unverified rules).
    """

    def __init__(self) -> None:
        self.lots: dict[str, deque[TaxLot]] = {}
        self.realized: dict[int, float] = {}

    def _book(self, year: int, amount: float) -> None:
        self.realized[year] = self.realized.get(year, 0.0) + amount

    def trade(self, inst: str, qty: float, price: float, fee_cost: float, day: date) -> None:
        if qty == 0:
            return
        q = self.lots.setdefault(inst, deque())
        fee_unit = fee_cost / abs(qty)
        rem = qty
        while rem and q and (q[0].qty > 0) != (rem > 0):
            lot = q[0]
            sign = 1 if lot.qty > 0 else -1
            take = min(abs(rem), abs(lot.qty))
            gain = sign * take * (price - lot.unit_cost) - take * fee_unit
            if (day - lot.day).days < PT_EXEMPT_HOLDING_DAYS:
                self._book(day.year, gain)
            lot.qty -= sign * take
            rem += sign * take
            if abs(lot.qty) < LOT_EPSILON:
                q.popleft()
            if abs(rem) < LOT_EPSILON:
                rem = 0
        if rem:
            sign = 1 if rem > 0 else -1
            q.append(TaxLot(rem, price + sign * fee_unit, day))

    def tax_for(self, year: int) -> float:
        return PT_SHORT_TERM_RATE * max(0.0, self.realized.get(year, 0.0))


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


class Accounting(StrEnum):
    UNITS = "units"
    CONSTANT_WEIGHT = "constant_weight"


@dataclass(frozen=True)
class Simulation:
    """Daily returns and their days; ``equity`` holds the open marks (one more than the returns)."""

    rets: tuple[float, ...]
    days: tuple[date, ...]
    turnover: float
    exposure_sum: float
    tax_paid: float
    trades: int
    realized_by_year: Mapping[int, float]
    targets: tuple[Mapping[str, float], ...]
    equity: tuple[float, ...]


def _targets(strategy: Strategy, h: History) -> dict[str, float]:
    return {k: float(v) for k, v in strategy.weights(h).items() if v}


def _validate(target: Mapping[str, float], strategy: Strategy, h: History, max_gross: float) -> None:
    gross = 0.0
    for inst, w in target.items():
        if inst not in strategy.universe:
            raise EngineError(EngineErrorCode.UNKNOWN_INSTRUMENT, f"{strategy.name}: weight for {inst} outside its universe")
        if not is_spot(inst):
            raise EngineError(EngineErrorCode.NOT_SPOT, f"{strategy.name}: {inst} is not a spot instrument")
        if not math.isfinite(w):
            raise EngineError(EngineErrorCode.INVALID_WEIGHT, f"{strategy.name}: non-finite weight for {inst} on {h.today}")
        if w < 0:
            raise EngineError(EngineErrorCode.INVALID_WEIGHT, f"{strategy.name}: negative spot weight for {inst}")
        if w != 0 and h._panel.open_at[inst][h._i] is None:
            raise EngineError(EngineErrorCode.NO_BAR_TO_TRADE, f"{strategy.name}: weight for {inst} on {h.today} without a bar")
        gross += abs(w)
    if gross > max_gross + GROSS_TOLERANCE:
        raise EngineError(EngineErrorCode.GROSS_EXCEEDED, f"{strategy.name}: gross {gross:.4f} > {max_gross}")


def simulate(
    strategy: Strategy,
    panel: Panel,
    window: Window,
    fee: float,
    slippage_bps: float = 0.0,
    accounting: Accounting = Accounting.UNITS,
    tax: bool = False,
) -> Simulation:
    """Run a strategy over the window's days."""
    for value, what in ((fee, "fee"), (slippage_bps, "slippage_bps")):
        if not math.isfinite(value) or value < 0:
            raise EngineError(EngineErrorCode.INVALID_PARAMETER, f"{what} must be finite and >= 0")
    for inst in strategy.universe:
        if inst not in panel.tradeable:
            raise EngineError(EngineErrorCode.UNKNOWN_INSTRUMENT, f"{strategy.name}: {inst} is not in the panel")
    days_idx = panel.window_days(window, int(strategy.warmup_days))
    if len(days_idx) < 2:
        raise EngineError(EngineErrorCode.WINDOW_TOO_SHORT, "the window has fewer than 2 days")
    band = float(strategy.band)
    max_gross = float(strategy.max_gross)
    cost = fee + slippage_bps / 10_000
    if accounting is Accounting.CONSTANT_WEIGHT:
        if tax:
            raise EngineError(EngineErrorCode.INVALID_PARAMETER, "constant_weight is untaxed (robust.py parity only)")
        return _simulate_constant(strategy, panel, days_idx, band, max_gross, cost)
    return _simulate_units(strategy, panel, days_idx, band, max_gross, cost, tax)


def _simulate_units(
    strategy: Strategy, panel: Panel, days_idx: list[int], band: float, max_gross: float, cost: float, tax: bool
) -> Simulation:
    cash = 1.0
    qty: dict[str, float] = {}
    taxbook = FifoTax()
    paid_years: set[int] = set()
    tax_paid, turnover, expo, trades = 0.0, 0.0, 0.0, 0
    equity_marks: list[float] = []
    targets: list[Mapping[str, float]] = []

    def equity(i: int) -> float:
        return cash + sum(q * panel.price(k, i) for k, q in qty.items())

    def trade(inst: str, dq: float, i: int) -> float:
        nonlocal cash, trades
        px = panel.price(inst, i)
        notional = dq * px
        fee_cost = abs(notional) * cost
        cash -= notional + fee_cost
        qty[inst] = qty.get(inst, 0.0) + dq
        trades += 1
        if tax:
            taxbook.trade(inst, dq, px, fee_cost, panel.dates[i])
        return abs(notional)

    def pay_tax(year: int, i: int) -> None:
        nonlocal cash, tax_paid
        t = taxbook.tax_for(year)
        paid_years.add(year)
        if t <= 0:
            return
        cash -= t
        tax_paid += t
        if cash < 0:  # sell spot pro rata to fund the tax bill
            spot = {k: q for k, q in qty.items() if q > 0}
            value = sum(q * panel.price(k, i) for k, q in spot.items())
            if value > 0:
                frac = min(1.0, -cash / (value * (1 - cost)))
                for k, q in spot.items():
                    trade(k, -q * frac, i)

    for n, i in enumerate(days_idx):
        if tax and n and panel.dates[i].year != panel.dates[i - 1].year:
            pay_tax(panel.dates[i - 1].year, i)
        e = equity(i)
        equity_marks.append(e)
        h = History(panel, i)
        target = _targets(strategy, h)
        _validate(target, strategy, h, max_gross)
        targets.append(target)
        traded_today = 0.0
        if e > 0:
            orders: list[tuple[str, float, float]] = []
            for inst in sorted(set(target) | {k for k, q in qty.items() if q}):
                tgt = target.get(inst, 0.0)
                px = panel.price(inst, i)
                cur = qty.get(inst, 0.0) * px / e
                if abs(tgt - cur) >= band or (tgt == 0 and cur != 0):
                    if panel.open_at[inst][i] is None:
                        continue  # no market today: keep the position
                    orders.append((inst, tgt * e / px - qty.get(inst, 0.0), px))
            for inst, dq, _ in orders:  # sells first, so their cash funds the buys
                if dq < 0:
                    traded_today += trade(inst, dq, i)
            buys = [(inst, dq, px) for inst, dq, px in orders if dq > 0]
            need = sum(dq * px * (1 + cost) for _, dq, px in buys)
            scale = min(1.0, max(cash, 0.0) / need) if need > max(cash, 0.0) else 1.0  # fees never borrow
            for inst, dq, _ in buys:
                traded_today += trade(inst, dq * scale, i)
        turnover += traded_today / e if e > 0 else 0.0
        expo += sum(abs(q * panel.price(k, i)) for k, q in qty.items()) / e if e > 0 else 0.0
    last = days_idx[-1] + 1
    if tax:
        for y in sorted(taxbook.realized):
            if y not in paid_years:
                pay_tax(y, last)
    equity_marks.append(equity(last))
    rets = tuple(equity_marks[k + 1] / equity_marks[k] - 1 for k in range(len(days_idx)))
    return Simulation(
        rets=rets,
        days=tuple(panel.dates[i] for i in days_idx),
        turnover=turnover,
        exposure_sum=expo,
        tax_paid=tax_paid,
        trades=trades,
        realized_by_year=dict(taxbook.realized),
        targets=tuple(targets),
        equity=tuple(equity_marks),
    )


def _simulate_constant(
    strategy: Strategy, panel: Panel, days_idx: list[int], band: float, max_gross: float, cost: float
) -> Simulation:
    held: dict[str, float] = {}
    rets: list[float] = []
    targets: list[Mapping[str, float]] = []
    turnover, expo, trades = 0.0, 0.0, 0
    for i in days_idx:
        h = History(panel, i)
        target = _targets(strategy, h)
        _validate(target, strategy, h, max_gross)
        targets.append(target)
        r = 0.0
        for inst in sorted(set(target) | set(held)):
            tgt, cur = target.get(inst, 0.0), held.get(inst, 0.0)
            if abs(tgt - cur) >= band or (tgt == 0 and cur > 0):
                r -= abs(tgt - cur) * cost
                turnover += abs(tgt - cur)
                trades += 1
                held[inst] = tgt
        for inst, w in held.items():
            if w:
                r += w * (panel.price(inst, i + 1) / panel.price(inst, i) - 1)
        expo += sum(abs(w) for w in held.values())
        rets.append(r)
    equity = [1.0]
    for r in rets:
        equity.append(equity[-1] * (1 + r))
    return Simulation(
        rets=tuple(rets),
        days=tuple(panel.dates[i] for i in days_idx),
        turnover=turnover,
        exposure_sum=expo,
        tax_paid=0.0,
        trades=trades,
        realized_by_year={},
        targets=tuple(targets),
        equity=tuple(equity),
    )


def audit_lookahead(strategy: Strategy, panel: Panel, days_idx: Sequence[int], samples: int = 8) -> None:
    """Recompute weights on physically truncated panels and compare.

    Any difference means the strategy reads data it should not see or keeps hidden state, and the
    run is refused.
    """
    if not days_idx:
        return
    step = max(1, len(days_idx) // samples)
    for i in list(days_idx[::step][:samples]) + [days_idx[-1]]:
        full = _targets(strategy, History(panel, i))
        cut = _targets(strategy, History(panel.truncated(i), i))
        if full.keys() != cut.keys() or any(abs(full[k] - cut[k]) > AUDIT_TOLERANCE for k in full):
            raise EngineError(
                EngineErrorCode.LOOKAHEAD, f"{strategy.name}: lookahead or state on {panel.dates[i]}: {full} != {cut}"
            )


# ---------------------------------------------------------------------------
# Evaluation (harness ``evaluate``)
# ---------------------------------------------------------------------------

SUMMARY_METRICS: tuple[str, ...] = (
    "cagr",
    "max_dd",
    "sharpe",
    "sortino",
    "pos12m",
    "turnover_yr",
    "avg_exposure",
    "worst_year",
    "after_tax_cagr",
)


@dataclass(frozen=True)
class Evaluation:
    """Pre-tax metrics of one rule on one window and fee, plus the after-tax CAGR."""

    fee: float
    slippage_bps: float
    pre_tax: Performance
    after_tax: Performance
    tax_paid: float
    trades: int

    @property
    def after_tax_cagr(self) -> float:
        return self.after_tax.cagr

    def summary(self) -> dict[str, float]:
        """The per-fee summary the harness logged in each registry run event (DSR excluded)."""
        p = self.pre_tax
        return {
            "cagr": p.cagr,
            "max_dd": p.max_dd,
            "sharpe": p.sharpe,
            "sortino": p.sortino,
            "pos12m": p.pos12m,
            "turnover_yr": p.turnover_yr,
            "avg_exposure": p.avg_exposure,
            "worst_year": p.worst_year,
            "after_tax_cagr": self.after_tax.cagr,
        }


def evaluate(strategy: Strategy, panel: Panel, window: Window, fee: float, slippage_bps: float = 0.0) -> Evaluation:
    pre = simulate(strategy, panel, window, fee, slippage_bps)
    post = simulate(strategy, panel, window, fee, slippage_bps, tax=True)
    return Evaluation(
        fee=fee,
        slippage_bps=slippage_bps,
        pre_tax=performance(pre.rets, pre.days, pre.turnover, pre.exposure_sum),
        after_tax=performance(post.rets, post.days),
        tax_paid=post.tax_paid,
        trades=pre.trades,
    )
