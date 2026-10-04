"""Kraken EUR paper books. Research only.

PAPER ONLY: no order, account or credential exists anywhere in this module. Results are PRE-TAX
and NOT QUALIFIED: a second, read-only EUR price source for the registered trend rules, not a trial.

Books: the six rules of :mod:`radar_v08.domain.trend_paper` x fee {0.004 "maker assumption",
0.008 "taker sensitivity"} per leg = 12 books, each starting at 7000 EUR on
:data:`~radar_v08.domain.trend_paper.PAPER_START` (2026-10-04 UTC). The fee pair is the Kraken tier
reported for the account (0.4% maker / 0.8% taker), not verified here. Each comparator is the buy and
hold book of the same family, venue and fee.

* Signals: :func:`radar_v08.domain.trend_paper.day_signals` on the Binance USDT series, unchanged
  (the same targets as the Binance books of the same day). No new rule, parameter or registry event.
* Fills: the Kraken ``XBTEUR`` / ``ETHEUR`` open of the day, with the same accounting
  (:func:`~radar_v08.domain.trend_paper.step_fraction`, :func:`~radar_v08.domain.trend_paper.step_units`),
  fee on traded notional and slippage 0. There is no fallback: a Binance or EURUSDT price would not
  be a Kraken fill.

Ledger: its own file (``kraken_ledger.jsonl``), in the same chained canonical format as the main
ledger (:func:`~radar_v08.domain.trend_paper.encode_day`, :func:`~radar_v08.domain.trend_paper.verify_chain`),
a day written whole (12 records) or as one skip entry, consecutive from the start.

Data gaps, as in the main module: nothing is interpolated, forward-filled or substituted. A missing
Kraken open of the day (``NO_KRAKEN_OPEN``) or USDT close of the signal window (``NO_SIGNAL_CLOSE``)
becomes a skip entry only when a later candle of the same symbol exists and a second, separate
request confirms the absence. Kraken returns at most its latest 720 daily candles: a day before
the earliest returned candle (``history_start`` of the adapter's series) is unknown, never a
confirmed gap, and the catch-up waits. Every day up to the clock's UTC date is due, and the USDT
series must reach that day's open (so the signal's last close is final at the exchange).

Pure: no I/O, clock or configuration.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from .trend_paper import (
    BANNERS,
    BTCUSDT_LISTING_DAY,
    CAPITAL,
    ENS_HISTORY_DAYS,
    GENESIS,
    LEDGER_SCHEMA,
    PAPER_START,
    SLIPPAGE_BPS,
    USDT_SYMBOL,
    Book,
    BookState,
    DaySignals,
    Family,
    Gap,
    Ledger,
    Market,
    PaperError,
    PaperErrorCode,
    Rule,
    SkippedDay,
    Sleeve,
    _has_later,
    _iso_day,
    _registration,
    _series,
    _sha,
    _signal_for,
    _sleeves_of,
    _target,
    canonical,
    day_signals,
    encode_day,
    first_day_to_book,
    initial_state,
    last_bookable_day,
    signal_gaps,
    step_fraction,
    step_units,
    summarize,
    verify_chain,
)

VENUE = "KRAKEN"
QUOTE = "EUR"
MAKER_FEE = 0.004
TAKER_FEE = 0.008
KRAKEN_FEES: tuple[float, ...] = (MAKER_FEE, TAKER_FEE)
FEE_LABELS: Mapping[float, str] = {MAKER_FEE: "0.4% maker assumption", TAKER_FEE: "0.8% taker sensitivity"}
KRAKEN_PAIR: Mapping[str, str] = {"BTC": "XBTEUR", "ETH": "ETHEUR"}
KRAKEN_PAIRS: tuple[str, ...] = tuple(KRAKEN_PAIR.values())
SIGNAL_SYMBOLS: tuple[str, ...] = tuple(USDT_SYMBOL.values())
KRAKEN_SYMBOLS: tuple[str, ...] = SIGNAL_SYMBOLS + KRAKEN_PAIRS  # the fetch order of one catch-up
BOOK_QUOTE = f"{VENUE}_{QUOTE}"  # the id prefix of the Kraken books
#: Kraken pairs are requested from this many days before the first day they must show. The adapter
#: marks a history that starts after the requested day as truncated (``history_start``), so a
#: missing candle on the requested day itself would look truncated and could never be confirmed.
KRAKEN_LEAD_DAYS = 7
SKIP_KIND = "skip"
SKIP_KEYS = frozenset({"kind", "ts", "date", "reason", "missing", "gap_day", "schema", "seq", "prev", "sha256"})


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------


def kraken_book_id(rule: Rule, fee: float) -> str:
    return Book(BOOK_QUOTE, rule, fee).id


#: The main module's :class:`Book` with ``quote`` set to the venue-quote prefix ``KRAKEN_EUR``, so the
#: ids (``KRAKEN_EUR|ENS|0.004``), comparators and rule specs follow the main books exactly.
KRAKEN_BOOKS: tuple[Book, ...] = tuple(Book(BOOK_QUOTE, r, f) for r in Rule for f in KRAKEN_FEES)
KRAKEN_BOOK_IDS: tuple[str, ...] = tuple(b.id for b in KRAKEN_BOOKS)


def label(symbol: str) -> str:
    """``Kraken XBTEUR`` for a Kraken pair (Binance has an ``ETHEUR`` too), else the symbol."""
    return f"Kraken {symbol}" if symbol in KRAKEN_PAIRS else symbol


def history_start(market: Market, symbol: str) -> date | None:
    """Where a truncated Kraken history starts (``KrakenDailySeries.history_start``), or ``None``."""
    start = getattr(market.get(symbol), "history_start", None)
    return start if isinstance(start, date) else None


def _known(market: Market, gap: Gap) -> bool:
    start = history_start(market, gap.symbol)
    return start is None or gap.day >= start


def kraken_open(market: Market, asset: str, day: date) -> float:
    """The Kraken open of ``day`` for ``asset``; there is no substitute price."""
    pair = KRAKEN_PAIR[asset]
    price = _series(market, pair).open_on(day)
    if price is None:
        raise PaperError(PaperErrorCode.MISSING_CANDLE, f"Kraken {pair}: no candle on {day}")
    return price


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def book_kraken_day(
    market: Market,
    day: date,
    states: Mapping[str, BookState],
    ts: str,
    start: date = PAPER_START,
    signals: DaySignals | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Sleeve]]]:
    """The 12 records of ``day`` and the books' new states. Raises before returning anything if a
    Kraken open or a signal close is missing, so a day is booked whole or not at all."""
    if day < start:
        raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{day} is before the paper start {start}")
    sig = signals if signals is not None else day_signals(market, day)
    prices = {asset: kraken_open(market, asset, day) for asset in KRAKEN_PAIR}
    records: list[dict[str, Any]] = []
    new_states: dict[str, dict[str, Sleeve]] = {}
    first_day = not any(book.id in states for book in KRAKEN_BOOKS)  # the first booked day
    for book in KRAKEN_BOOKS:
        state = states.get(book.id) or initial_state(book)
        spec = book.spec
        assets: dict[str, Any] = {}
        sleeves: dict[str, Sleeve] = {}
        equity = 0.0
        equity_before = 0.0
        cash_total = 0.0
        for asset, strategy in spec.sleeves.items():
            px = prices[asset]
            tgt = _target(book, sig, asset)
            if spec.family is Family.ENS:
                fill = step_fraction(
                    state[asset], px, tgt, book.fee, buy_and_hold=book.rule is Rule.BH_5050, first_day=first_day
                )
            else:
                fill = step_units(state[asset], px, tgt, float(strategy.band), book.fee + SLIPPAGE_BPS / 10_000)
            asig = _signal_for(book, sig, asset)
            name, digest = _registration(strategy)
            assets[asset] = {
                "symbol": USDT_SYMBOL[asset],
                "pair": KRAKEN_PAIR[asset],
                "registered_name": name,
                "registered_sha256": digest,
                "signal_close_date": asig.close_date.isoformat(),
                "signal_close_usdt": asig.close,
                "signal": dict(asig.values),
                "fill_price": px,
                "fill_source": f"Kraken {KRAKEN_PAIR[asset]} open",
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
                "venue": VENUE,
                "quote": QUOTE,
                "rule": book.rule.value,
                "family": spec.family.value,
                "comparator": book.comparator,
                "fee_rate": book.fee,
                "fee_label": FEE_LABELS[book.fee],
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
# Skip entries and ledger verification
# ---------------------------------------------------------------------------


class KrakenSkipReason(StrEnum):
    NO_KRAKEN_OPEN = "NO_KRAKEN_OPEN"  # a Kraken XBTEUR/ETHEUR candle of the day itself
    NO_SIGNAL_CLOSE = "NO_SIGNAL_CLOSE"  # a USDT close the day's signal window needs


SKIP_SYMBOLS: Mapping[KrakenSkipReason, frozenset[str]] = {
    KrakenSkipReason.NO_KRAKEN_OPEN: frozenset(KRAKEN_PAIRS),
    KrakenSkipReason.NO_SIGNAL_CLOSE: frozenset(SIGNAL_SYMBOLS),
}


def skip_text(reason: KrakenSkipReason, missing: Sequence[str], gap_day: date) -> str:
    """A short plain-English reason, e.g. ``no Kraken XBTEUR candle on 2026-10-06 (no Kraken fill price)``."""
    if reason is KrakenSkipReason.NO_KRAKEN_OPEN:
        return f"no Kraken {' and '.join(missing)} candle on {gap_day} (no Kraken fill price)"
    return f"no {' and '.join(missing)} close on {gap_day} (needed by the signal)"


@dataclass(frozen=True)
class KrakenSkippedDay:
    """A Kraken paper day that booked no record because a public candle is missing for good."""

    day: date
    reason: KrakenSkipReason
    missing: tuple[str, ...]  # sorted symbols
    gap_day: date  # the earliest missing candle

    @property
    def text(self) -> str:
        return skip_text(self.reason, self.missing, self.gap_day)


@dataclass(frozen=True)
class KrakenDayGap:
    """Why a due day cannot be booked: the first blocking reason and its missing candles."""

    day: date
    reason: KrakenSkipReason
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


def encode_kraken_skip(ledger: Ledger, gap: KrakenDayGap, ts: str) -> bytes:
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


def _skip_of(obj: Mapping[str, Any], n: int) -> KrakenSkippedDay:
    where = f"line {n}"
    if set(obj) != SKIP_KEYS or obj.get("kind") != SKIP_KIND or not isinstance(obj.get("ts"), str):
        raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{where}: not a valid Kraken skip entry")
    try:
        reason = KrakenSkipReason(obj["reason"])
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
        or (reason is KrakenSkipReason.NO_KRAKEN_OPEN and gap_day != day)
        or (reason is KrakenSkipReason.NO_SIGNAL_CLOSE and gap_day == day)
    ):
        raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{where}: skip entry does not match its reason")
    return KrakenSkippedDay(day, reason, tuple(missing), gap_day)


EMPTY_KRAKEN_LEDGER = Ledger((), GENESIS, (), {}, 0)


def kraken_skipped(ledger: Ledger) -> tuple[KrakenSkippedDay, ...]:
    """The skipped days of a Kraken ledger read by :func:`extend_kraken_ledger`."""
    return tuple(s for s in cast(Iterable[object], ledger.skipped) if isinstance(s, KrakenSkippedDay))


def parse_kraken_ledger(data: bytes, start: date = PAPER_START) -> Ledger:
    """Verify every line and the day structure of a Kraken ledger; raise on any torn, edited or
    incomplete content."""
    return extend_kraken_ledger(EMPTY_KRAKEN_LEDGER, data, start)


def extend_kraken_ledger(ledger: Ledger, data: bytes, start: date = PAPER_START) -> Ledger:
    """``ledger`` followed by the lines in ``data``: the chain as in the main ledger, then the days,
    consecutive from ``start``, each the 12 Kraken books in order or one Kraken skip entry. Its
    ``skipped`` holds :class:`KrakenSkippedDay` entries (:func:`kraken_skipped`)."""
    if not data:
        return ledger
    base = ledger.entries
    lines, prev = verify_chain(ledger, data)
    records: list[Mapping[str, Any]] = []
    days = list(ledger.days)
    skipped: list[object] = list(ledger.skipped)
    states: dict[str, BookState] = dict(ledger.states)
    last = ledger.last_day
    k = 0
    while k < len(lines):
        expected = start if last is None else last + timedelta(days=1)
        if "kind" in lines[k]:
            skip = _skip_of(lines[k], base + k)
            if skip.day != expected:
                raise PaperError(PaperErrorCode.LEDGER_INVALID, f"line {base + k}: day {skip.day}, expected {expected}")
            skipped.append(skip)
            last = skip.day
            k += 1
            continue
        group = lines[k : k + len(KRAKEN_BOOKS)]
        if len(group) < len(KRAKEN_BOOKS) or any("kind" in record for record in group):
            raise PaperError(PaperErrorCode.LEDGER_INVALID, "a day does not hold all Kraken books")
        try:
            day = date.fromisoformat(str(group[0].get("date")))
        except ValueError as error:
            raise PaperError(PaperErrorCode.LEDGER_INVALID, f"line {base + k}: bad date") from error
        if day != expected:
            raise PaperError(PaperErrorCode.LEDGER_INVALID, f"line {base + k}: day {day}, expected {expected}")
        for book, record in zip(KRAKEN_BOOKS, group, strict=True):
            where = f"{day} {book.id}"
            if record.get("date") != day.isoformat() or record.get("book") != book.id:
                raise PaperError(
                    PaperErrorCode.LEDGER_INVALID, f"{where}: books not in their fixed sequence or incomplete day"
                )
            if record.get("venue") != VENUE or record.get("quote") != QUOTE:
                raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{where}: not a Kraken EUR record")
            states[book.id] = _sleeves_of(record, book, where)
        records.extend(group)
        days.append(day)
        last = day
        k += len(KRAKEN_BOOKS)
    # The Ledger container is shared with the main module; its skip entries here are Kraken ones.
    return Ledger(
        ledger.records + tuple(records), prev, tuple(days), states, ledger.size + len(data),
        cast(tuple[SkippedDay, ...], tuple(skipped)),
    )


# ---------------------------------------------------------------------------
# Catch-up planning
# ---------------------------------------------------------------------------


def fetch_since(ledger: Ledger, today: date, start: date = PAPER_START) -> dict[str, date]:
    """From which day each symbol must be fetched to settle every missed Kraken day."""
    first = min(first_day_to_book(ledger, start), today)
    return {
        "BTCUSDT": BTCUSDT_LISTING_DAY,
        "ETHUSDT": first - timedelta(days=ENS_HISTORY_DAYS),
        KRAKEN_PAIR["BTC"]: first - timedelta(days=KRAKEN_LEAD_DAYS),
        KRAKEN_PAIR["ETH"]: first - timedelta(days=KRAKEN_LEAD_DAYS),
    }


def kraken_day_gap(market: Market, day: date) -> KrakenDayGap | None:
    """The first reason ``day`` cannot be booked (Kraken open, then signal close), or ``None``."""
    fill = [Gap(pair, day) for pair in KRAKEN_PAIRS if _series(market, pair).open_on(day) is None]
    if fill:
        return KrakenDayGap(day, KrakenSkipReason.NO_KRAKEN_OPEN, tuple(fill))
    signal = signal_gaps(market, day)
    if signal:
        return KrakenDayGap(day, KrakenSkipReason.NO_SIGNAL_CLOSE, tuple(sorted(signal, key=lambda g: (g.day, g.symbol))))
    return None


@dataclass(frozen=True)
class KrakenPlan:
    """The due days of one Kraken catch-up in order, the ones that cannot be booked and, when the
    data do not settle every due day yet, why nothing may be written (``waiting``)."""

    days: tuple[date, ...]
    gaps: Mapping[date, KrakenDayGap] = field(default_factory=dict)
    waiting: str | None = None
    confirmed: bool = False  # every gap was seen again in a second, separate response

    def missing(self) -> list[Gap]:
        return [gap for day_gap in self.gaps.values() for gap in day_gap.gaps]

    def to_confirm(self) -> dict[str, date]:
        """Per symbol with a missing candle, where its second request starts: the earliest missing day
        (for a Kraken pair, :data:`KRAKEN_LEAD_DAYS` before it)."""
        since: dict[str, date] = {}
        for gap in self.missing():
            day = gap.day - timedelta(days=KRAKEN_LEAD_DAYS) if gap.symbol in KRAKEN_PAIRS else gap.day
            since[gap.symbol] = min(since.get(gap.symbol, day), day)
        return since


def _unsettled(market: Market, gap: Gap) -> str | None:
    if not _has_later(market, gap.symbol, gap.day):
        return f"no {label(gap.symbol)} candle on {gap.day} yet (none after it either)"
    if not _known(market, gap):
        return (
            f"Kraken {gap.symbol} history starts at {history_start(market, gap.symbol)} (its 720-candle cap): "
            f"{gap.day} is unknown, never a confirmed missing candle"
        )
    return None


def plan_kraken_catch_up(
    ledger: Ledger, market: Market, start: date = PAPER_START, today: date | None = None
) -> KrakenPlan:
    """The days the next Kraken catch-up must settle. With ``today`` (the UTC calendar day of the
    clock) every day up to ``today`` is due and both USDT series must reach ``today``'s open,
    otherwise the plan waits. A missing candle makes its day a gap only if a later candle of the
    same symbol exists and it lies inside the history Kraken returned; otherwise the plan waits."""
    first = first_day_to_book(ledger, start)
    horizon = last_bookable_day(market)
    last = today if today is not None else horizon
    if last is None or first > last:
        return KrakenPlan(())
    days = tuple(first + timedelta(days=k) for k in range((last - first).days + 1))
    if horizon is None or horizon < last:
        return KrakenPlan(days, waiting=f"the public USDT daily candles do not reach the {last} open yet")
    gaps: dict[date, KrakenDayGap] = {}
    for day in days:
        found = kraken_day_gap(market, day)
        if found is None:
            continue
        for gap in found.gaps:
            why = _unsettled(market, gap)
            if why is not None:
                return KrakenPlan(days, waiting=why)
        gaps[day] = found
    return KrakenPlan(days, gaps)


def confirm_kraken_plan(plan: KrakenPlan, second: Market) -> KrakenPlan:
    """``plan`` with its gaps confirmed by ``second`` (a separate response per symbol): every missing
    candle must be missing there too, inside its known history, with a later candle of the same
    symbol. Any disagreement makes the plan wait; nothing is merged."""
    for gap in plan.missing():
        series = second.get(gap.symbol)
        if series is None or series.symbol != gap.symbol or _unsettled(second, gap) is not None:
            return replace(
                plan, waiting=f"the absence of {label(gap.symbol)} on {gap.day} was not confirmed by a second request"
            )
        if series.open_on(gap.day) is not None:
            return replace(plan, waiting=f"two requests disagree on {label(gap.symbol)} {gap.day}; nothing was booked")
    return replace(plan, confirmed=True)


def apply_kraken_plan(
    ledger: Ledger,
    market: Market,
    plan: KrakenPlan,
    ts: str,
    append: Callable[[bytes], Ledger],
    start: date = PAPER_START,
) -> tuple[list[date], list[date]]:
    """Settle every day of ``plan`` in order: a booked day (12 records) or a confirmed skip entry.
    Raises ``WAITING_FOR_DATA`` before writing when the plan waits or a gap is unconfirmed. Every
    day is computed and verified in memory first; then each day is one ``append``."""
    if plan.waiting is not None:
        raise PaperError(PaperErrorCode.WAITING_FOR_DATA, plan.waiting)
    if plan.missing() and not plan.confirmed:
        raise PaperError(PaperErrorCode.WAITING_FOR_DATA, "a missing candle was seen in one response only")
    chunks: list[tuple[date, bytes, bool]] = []
    pending = ledger
    for day in plan.days:
        gap = plan.gaps.get(day)
        if gap is not None:
            data = encode_kraken_skip(pending, gap, ts)
        else:
            records, _ = book_kraken_day(market, day, pending.states, ts, start)
            data = encode_day(pending, records)
        pending = extend_kraken_ledger(pending, data, start)
        chunks.append((day, data, gap is not None))
    booked: list[date] = []
    skipped: list[date] = []
    for day, data, is_skip in chunks:
        ledger = append(data)
        if ledger.last_day != day:
            raise PaperError(PaperErrorCode.LEDGER_INVALID, f"{day} was not the last day after the append")
        (skipped if is_skip else booked).append(day)
    return booked, skipped


def catch_up_kraken_days(
    ledger: Ledger,
    market: Market,
    ts: str,
    append: Callable[[bytes], Ledger],
    start: date = PAPER_START,
    *,
    today: date | None = None,
    refetch: Callable[[Mapping[str, date]], Market] | None = None,
) -> list[date]:
    """Plan, confirm and settle every due Kraken day; returns the booked days. ``refetch`` makes the
    second, separate request per symbol that confirms a gap; without it no gap is ever confirmed and
    nothing is written. Raises ``WAITING_FOR_DATA`` (nothing written) when the data do not settle
    every due day yet."""
    plan = plan_kraken_catch_up(ledger, market, start, today)
    if plan.waiting is None and plan.missing() and refetch is not None:
        plan = confirm_kraken_plan(plan, refetch(plan.to_confirm()))
    return apply_kraken_plan(ledger, market, plan, ts, append, start)[0]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillDifference:
    """One day and asset: the Kraken open against the Binance EUR book fill of the main ledger."""

    day: date
    asset: str
    kraken: float | None
    binance: float | None
    binance_source: str | None
    note: str = ""  # why a side is n/a

    @property
    def diff_eur(self) -> float | None:
        return None if self.kraken is None or self.binance is None else self.kraken - self.binance

    @property
    def diff_bps(self) -> float | None:
        diff = self.diff_eur
        return None if diff is None or self.binance is None else diff / self.binance * 10_000


def _fills(records: Iterable[Mapping[str, Any]], quote: str) -> dict[tuple[str, str], tuple[float, str]]:
    """The first fill per (date, asset) among the book records of ``quote`` (all of a day share it)."""
    out: dict[tuple[str, str], tuple[float, str]] = {}
    for r in records:
        if "kind" in r or r.get("quote") != quote:
            continue
        for asset, v in r["assets"].items():
            out.setdefault((str(r["date"]), str(asset)), (float(v["fill_price"]), str(v["fill_source"])))
    return out


def fill_differences(kraken: Ledger, binance: Ledger | None, binance_note: str = "") -> list[FillDifference]:
    """Per day covered by either ledger and per asset: Kraken open minus the Binance EUR book fill,
    or n/a with the reason when either side is missing, skipped or the main ledger is unavailable."""
    covered = set(kraken.days) | {s.day for s in kraken_skipped(kraken)}
    if binance is not None:
        covered |= set(binance.days) | {s.day for s in binance.skipped}
    k_fills = _fills(kraken.records, QUOTE)
    b_fills = _fills(binance.records, "EUR") if binance is not None else {}
    k_skipped = {s.day for s in kraken_skipped(kraken)}
    b_skipped = {s.day for s in binance.skipped} if binance is not None else set()
    out: list[FillDifference] = []
    for day in sorted(covered):
        for asset in KRAKEN_PAIR:
            k = k_fills.get((day.isoformat(), asset))
            b = b_fills.get((day.isoformat(), asset))
            notes: list[str] = []
            if k is None:
                notes.append("Kraken day skipped" if day in k_skipped else "Kraken day not booked yet")
            if binance is None:
                notes.append(binance_note or "main ledger unavailable")
            elif b is None:
                notes.append("Binance day skipped" if day in b_skipped else "Binance day not booked yet")
            out.append(
                FillDifference(
                    day, asset, None if k is None else k[0], None if b is None else b[0],
                    None if b is None else b[1], "; ".join(notes),
                )
            )
    return out


def _cell(value: float | None, spec: str, width: int) -> str:
    return f"{'n/a':>{width}}" if value is None else f"{value:{width}{spec}}"


def render_kraken_report(
    ledger: Ledger,
    generated: datetime,
    binance: Ledger | None,
    *,
    binance_note: str = "",
    start: date = PAPER_START,
) -> str:
    skipped = kraken_skipped(ledger)
    lines = [
        " | ".join(BANNERS),
        "Kraken EUR paper books (research only): no order, no account, no credential, public market data only. "
        "Results are pre-tax and not qualified; paper trading registered rules is a forward observation, not a trial.",
        f"Generated {generated.isoformat(timespec='seconds')}. Start {start} UTC, capital {CAPITAL:.0f} EUR per book. "
        "Same signals as the Binance books (USDT closes before the fill day); fills at the Kraken XBTEUR/ETHEUR "
        f"daily open, no substitute price; slippage {SLIPPAGE_BPS:g}.",
        f"Fee per leg on traded notional: {FEE_LABELS[MAKER_FEE]} and {FEE_LABELS[TAKER_FEE]} (the Kraken tier "
        "reported for the account, not verified).",
    ]
    if ledger.days:
        lines.append(f"Kraken paper days booked: {len(ledger.days)} ({ledger.days[0]} .. {ledger.days[-1]}).")
    elif skipped:
        lines.append(
            f"No Kraken paper day booked yet: {len(skipped)} day(s) skipped for missing public candles (listed "
            f"below). Every book is at {CAPITAL:.2f}."
        )
    else:
        lines.append(f"No Kraken paper days yet: {start} is the first paper day. Every book is at {CAPITAL:.2f}.")
    if skipped:
        lines.append(
            f"Kraken paper days skipped: {len(skipped)} (a public candle is missing for good; no record, the books "
            "carry over unchanged):"
        )
        lines += [f"  {s.day}: {s.text}" for s in skipped]
    lines.append("")
    summ = summarize(ledger.records, KRAKEN_BOOK_IDS)
    hdr = (
        f"{'venue':10} {'rule':13} {'fee':22} | {'equity':>10} {'return':>8} {'maxDD':>7} {'trades':>6} {'fees':>8}"
        f" | {'comparator':10} {'cmp eq':>10} {'cmp ret':>8} {'cmp DD':>7}"
    )
    lines += [hdr, "-" * len(hdr)]
    for book in KRAKEN_BOOKS:
        m = summ[book.id]
        row = (
            f"{'KRAKEN EUR':10} {book.rule.value:13} {FEE_LABELS[book.fee]:22} | {m.equity:10.2f} {m.ret * 100:7.2f}% "
            f"{m.mdd * 100:6.2f}% {m.trades:6d} {m.fees:8.2f} | "
        )
        if book.comparator is None or book.spec.comparator is None:
            row += f"{'-':10}"
        else:
            c = summ[book.comparator]
            row += f"{book.spec.comparator.value:10} {c.equity:10.2f} {c.ret * 100:7.2f}% {c.mdd * 100:6.2f}%"
        lines.append(row.rstrip())
    lines.append("")
    lines.append(
        "Fill price difference per day: Kraken open minus the Binance EUR book fill from ledger.jsonl "
        "(EUR per coin; bps of the Binance fill; n/a when either side is missing or skipped):"
    )
    if binance is None:
        lines.append(f"  Binance EUR fills unavailable ({binance_note or 'main ledger unavailable'}): every difference is n/a.")
    diffs = fill_differences(ledger, binance, binance_note)
    dhdr = (
        f"{'date':10} {'asset':5} {'Kraken open':>12} {'Binance EUR':>12} {'Binance fill source':32} "
        f"{'diff EUR':>10} {'diff bps':>8}  note"
    )
    lines += [dhdr, "-" * len(dhdr)]
    if not diffs:
        lines.append("  (no paper day in either ledger yet)")
    for d in diffs:
        lines.append(
            (
                f"{d.day.isoformat():10} {d.asset:5} {_cell(d.kraken, '.2f', 12)} {_cell(d.binance, '.2f', 12)} "
                f"{d.binance_source or 'n/a':32} {_cell(d.diff_eur, '.2f', 10)} {_cell(d.diff_bps, '.1f', 8)}  {d.note}"
            ).rstrip()
        )
    return "\n".join(lines)
