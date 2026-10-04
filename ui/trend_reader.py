"""Read-only view of the trend paper books for ``Api.get_trend_paper_state``.

Research only: paper books, no order, no account, no credential, no private endpoint. Results are
pre-tax and not qualified.

The ledger ``<config.STATE_DIR>/trend_paper/ledger.jsonl`` is written only by the catch-up
(:mod:`radar_v08.trend_paper_hook`) under its lock. :meth:`TrendReader.read` reads it only through
:func:`radar_v08.adapters.trend_paper_store.read_ledger_settled`: no lock is taken, no directory,
ledger or lock file is created when it is missing, and ``radar_state.sqlite`` is never opened. A
poll never touches the network.

Every figure comes from :func:`radar_v08.domain.trend_paper.summarize` and the last booked records;
nothing is recomputed another way. Money crosses the bridge as decimal strings rounded half to
even to the cent, in the book's quote currency; percentages are decimal strings in percent units.
A value that was not recorded is ``None``, never zero. Before the first paper day the payload is
``empty`` and shows no book, so no result is invented.

States: ``ok`` (books shown), ``empty`` (no paper day booked yet), ``refused`` (torn, edited,
invalid or unreadable ledger: the error code and a short detail, no figure) and ``unavailable``
(any other failure, or a refused read while a catch-up holds the writer lock: an append may be in
progress, so it is worded as transient and the next poll reads again). :meth:`TrendReader.read`
never raises. Every state carries ``days_skipped`` (int) and ``skipped_days`` (``[{day, reason}]``,
reason in plain English): paper days with no record because a public candle is missing for good.
``days_booked`` counts booked days only.

:meth:`TrendReader.catch_up` is the only action: it runs the existing
:func:`radar_v08.trend_paper_hook.catch_up` with the public Binance klines client
(:func:`radar_v08.trend_paper_hook.default_fetcher`), closes the client on every exit, and
returns ``{ok, status, days_booked, detail}``. One catch-up runs at a time in this process (a
second call gets ``IN_PROGRESS``); the ledger lock held by the radar's own catch-up gives ``BUSY``
and nothing is written. ``WAITING_FOR_DATA`` (not ok) means a paper day is due by the calendar but
the public candles do not settle it yet; nothing is written and the next start retries.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from radar_v08 import trend_paper_hook
from radar_v08.adapters.binance_public_klines import KlinesError
from radar_v08.adapters.trend_paper_store import (
    TrendPaperStoreError,
    TrendPaperStoreErrorCode,
    ledger_path,
    read_ledger_settled,
)
from radar_v08.domain.trend_engine import EngineError
from radar_v08.domain.trend_paper import (
    CAPITAL,
    FEES,
    PAPER_START,
    QUOTES,
    Book,
    Ledger,
    PaperError,
    PaperErrorCode,
    Rule,
    SkippedDay,
    summarize,
)

logger = logging.getLogger("ui.trend_reader")

HONESTY_LABEL = "Paper only — research, not qualified, no real orders"
PRE_TAX_NOTE = (
    "Results are pre-tax: fees are charged on traded notional, slippage is 0 and no tax is deducted."
)
FIRST_FILL_TEXT = (
    f"No paper day yet: the first fill is at the {PAPER_START.isoformat()} 00:00 UTC open "
    f"(decided on the {(PAPER_START - timedelta(days=1)).isoformat()} close)."
)
UNAVAILABLE_TEXT = "The trend paper ledger could not be read."
BEING_WRITTEN_TEXT = "A catch-up is writing the trend paper ledger right now; it shows again in a moment."

STATE_OK = "ok"
STATE_EMPTY = "empty"
STATE_REFUSED = "refused"
STATE_UNAVAILABLE = "unavailable"

#: The strategies shown, in order, with the names the research uses for them.
STRATEGIES: tuple[tuple[Rule, str], ...] = (
    (Rule.ENS, "ENS"),
    (Rule.ENS_VT, "ENS_VT"),
    (Rule.BTC_TREND5, "btc_trend5"),
    (Rule.BTC_TREND5_VT, "btc_trend5_vt"),
)
#: Quote currencies in display order: EUR first (the relevant one for an EEA user).
QUOTE_ORDER: tuple[str, ...] = tuple(sorted(QUOTES, key=lambda q: (q != "EUR", q)))

DETAIL_MAX = 160
_CENT = Decimal("0.01")
_SIGNAL_STEP = Decimal("0.0001")
_HUNDRED = Decimal(100)

#: Paper error codes that mean the ledger itself is refused (the others are market data failures).
_LEDGER_CODES = frozenset({PaperErrorCode.LEDGER_TORN, PaperErrorCode.LEDGER_EDITED, PaperErrorCode.LEDGER_INVALID})
#: Fixed texts for store errors: their own detail can carry a local path.
_STORE_TEXT: Mapping[TrendPaperStoreErrorCode, str] = {
    TrendPaperStoreErrorCode.BUSY: "another trend paper run holds the ledger lock",
    TrendPaperStoreErrorCode.UNREADABLE: "the ledger file could not be read",
    TrendPaperStoreErrorCode.CHANGED_DURING_APPEND: "the ledger changed during an append",
    TrendPaperStoreErrorCode.WRITE_FAILED: "the ledger append failed",
    TrendPaperStoreErrorCode.BEING_WRITTEN: "a catch-up is writing the ledger",
}


class CatchUpOutcome(StrEnum):
    BOOKED = "BOOKED"
    UP_TO_DATE = "UP_TO_DATE"
    BEFORE_START = "BEFORE_START"
    BUSY = "BUSY"
    MARKET_FAILED = "MARKET_FAILED"
    WAITING_FOR_DATA = "WAITING_FOR_DATA"
    LEDGER_REFUSED = "LEDGER_REFUSED"
    IN_PROGRESS = "IN_PROGRESS"


_OK_OUTCOMES = frozenset({CatchUpOutcome.BOOKED, CatchUpOutcome.UP_TO_DATE, CatchUpOutcome.BEFORE_START})


# --- formatting ---------------------------------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    """A recorded number as an exact Decimal of its shortest repr; ``None`` if it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = Decimal(repr(value))
    return number if number.is_finite() else None


def _text(value: Decimal, step: Decimal) -> str:
    rounded = value.quantize(step, rounding=ROUND_HALF_EVEN)
    return str(abs(rounded) if rounded == 0 else rounded)  # zero is never signed


def money(value: object) -> str | None:
    """Cents as a decimal string (``"7012.35"``, ``"-0.35"``); ``None`` when not recorded."""
    number = _decimal(value)
    return None if number is None else _text(number, _CENT)


def percent(fraction: object) -> str | None:
    """A fraction in percent units with two decimals (``0.01234`` -> ``"1.23"``)."""
    number = _decimal(fraction)
    return None if number is None else _text(number * _HUNDRED, _CENT)


def points(fraction: object, comparator: object) -> str | None:
    """The difference of two returns in percentage points, rounded once from the exact values."""
    a, b = _decimal(fraction), _decimal(comparator)
    return None if a is None or b is None else _text((a - b) * _HUNDRED, _CENT)


def signal_value(value: object) -> str | None:
    """A recorded signal value (a weight, a vote share, a volatility) with four decimals."""
    number = _decimal(value)
    return None if number is None else _text(number, _SIGNAL_STEP)


def fee_percent(fee: float) -> str:
    """A fee rate per leg in percent (``0.001`` -> ``"0.1"``)."""
    return _text(Decimal(repr(fee)) * _HUNDRED, Decimal("0.1"))


def short(text: object) -> str:
    """One line of at most ``DETAIL_MAX`` characters, for an error detail shown as text."""
    line = " ".join(str(text).split())
    return line if len(line) <= DETAIL_MAX else line[: DETAIL_MAX - 1] + "…"


def _ts(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _error_of(error: Exception) -> tuple[str, str]:
    """The code and a short, path-free detail of a typed paper, store or market error."""
    if isinstance(error, TrendPaperStoreError):
        return error.code.value, _STORE_TEXT.get(error.code, "ledger file error")
    if isinstance(error, (PaperError, KlinesError, EngineError)):
        return error.code.value, short(error.detail or error.code.value)
    return type(error).__name__, ""


# --- the reader ---------------------------------------------------------------------------------


FetcherFactory = Callable[[], trend_paper_hook.Fetcher]
Clock = Callable[[], datetime]


class TrendReader:
    """Builds the ``get_trend_paper_state`` payload and runs the guarded catch-up for one state dir."""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        fetcher_factory: FetcherFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self._fetcher_factory = fetcher_factory
        self._clock = clock
        self._running = threading.Lock()

    @property
    def path(self) -> Path:
        return ledger_path(self.state_dir)

    def _now(self) -> datetime:
        return (self._clock or trend_paper_hook.utc_now)()

    # -- read ---------------------------------------------------------------------------------

    def read(self, now: datetime | None = None) -> dict[str, Any]:
        """The payload; never raises."""
        try:
            moment = now or self._now()
            generated = _ts(moment)
        except Exception:
            logger.warning("Trend paper reader clock failed", exc_info=True)
            generated = None
        try:
            ledger = read_ledger_settled(self.path)
        except TrendPaperStoreError as error:
            if error.code is TrendPaperStoreErrorCode.BEING_WRITTEN:
                payload = base_payload(STATE_UNAVAILABLE, generated)
                payload["reason"] = BEING_WRITTEN_TEXT
                return payload
            return refused_payload(error, generated)
        except PaperError as error:
            return refused_payload(error, generated)
        except Exception:
            logger.warning("Trend paper ledger read failed", exc_info=True)
            return unavailable_payload(generated)
        try:
            return self._payload(ledger, generated)
        except Exception:
            logger.warning("Trend paper payload could not be built", exc_info=True)
            return unavailable_payload(generated)

    def _payload(self, ledger: Ledger, generated: str | None) -> dict[str, Any]:
        skipped = _skipped(ledger.skipped)
        if not ledger.days:
            payload = base_payload(STATE_EMPTY, generated)
            payload["reason"] = (
                f"No paper day booked yet: {len(skipped)} paper day(s) skipped because a public candle is missing."
                if skipped else FIRST_FILL_TEXT
            )
            payload["days_booked"] = 0
            payload["days_skipped"] = len(skipped)
            payload["skipped_days"] = skipped
            return payload
        summary = summarize(ledger.records)
        last: dict[str, Mapping[str, Any]] = {}
        for record in ledger.records:  # verified ledger: whole days in order, so the last wins
            last[str(record["book"])] = record
        payload = base_payload(STATE_OK, generated)
        payload["days_booked"] = len(ledger.days)
        payload["days_skipped"] = len(skipped)
        payload["skipped_days"] = skipped
        payload["first_day"] = ledger.days[0].isoformat()
        payload["last_day"] = ledger.days[-1].isoformat()
        ts = ledger.records[-1].get("ts")
        payload["last_record_ts"] = ts if isinstance(ts, str) else None
        payload["quotes"] = [
            {
                "quote": quote,
                "currency": quote,
                "rows": [
                    _row(Book(quote, rule, fee), label, summary, last)
                    for rule, label in STRATEGIES
                    for fee in FEES
                ],
            }
            for quote in QUOTE_ORDER
        ]
        return payload

    # -- catch-up -----------------------------------------------------------------------------

    def catch_up(self) -> dict[str, Any]:
        """Book every missed paper day now. A second call while one runs returns ``IN_PROGRESS``
        and starts nothing. An unexpected exception (not a typed market, ledger or lock error)
        propagates after the client is closed and the single-flight lock released."""
        if not self._running.acquire(blocking=False):
            return catch_up_result(CatchUpOutcome.IN_PROGRESS, 0, "A catch-up is already running.")
        try:
            return self._catch_up()
        finally:
            self._running.release()

    def _catch_up(self) -> dict[str, Any]:
        try:
            fetcher = (self._fetcher_factory or trend_paper_hook.default_fetcher)()
        except Exception as error:
            logger.warning("Trend paper market data client failed to start", exc_info=True)
            return catch_up_result(
                CatchUpOutcome.MARKET_FAILED, 0,
                f"The public market data client did not start ({type(error).__name__}); nothing was written.",
            )
        try:
            result = trend_paper_hook.catch_up(self.state_dir, fetcher, clock=self._clock or trend_paper_hook.utc_now)
        except TrendPaperStoreError as error:
            if error.code is TrendPaperStoreErrorCode.BUSY:
                return catch_up_result(
                    CatchUpOutcome.BUSY, 0,
                    "The radar's own catch-up holds the ledger lock; nothing was written. Try again shortly.",
                )
            code, detail = _error_of(error)
            return catch_up_result(CatchUpOutcome.LEDGER_REFUSED, None, _refused_text(code, detail))
        except PaperError as error:
            code, detail = _error_of(error)
            if error.code in _LEDGER_CODES:
                return catch_up_result(CatchUpOutcome.LEDGER_REFUSED, None, _refused_text(code, detail))
            # Every request and every day's computation happen before the first append.
            return catch_up_result(CatchUpOutcome.MARKET_FAILED, 0, _market_text(code, detail))
        except (KlinesError, EngineError) as error:
            code, detail = _error_of(error)
            return catch_up_result(CatchUpOutcome.MARKET_FAILED, 0, _market_text(code, detail))
        finally:
            try:
                fetcher.close()
            except Exception:
                logger.debug("Trend paper fetcher close failed", exc_info=True)
        booked = result.booked
        status = trend_paper_hook.CatchUpStatus
        skip_note = (
            f" Skipped {len(result.skipped)} paper day(s) because a public candle is missing: "
            f"{', '.join(d.isoformat() for d in result.skipped)}."
            if result.skipped else ""
        )
        if result.status is status.WAITING_FOR_DATA:
            return catch_up_result(
                CatchUpOutcome.WAITING_FOR_DATA, 0,
                f"Waiting for public data ({short(result.detail)}); nothing was written. "
                "The next start or Catch up now tries again.",
            )
        if result.status is status.BOOKED and booked:
            span = booked[0].isoformat() if len(booked) == 1 else f"{booked[0].isoformat()} to {booked[-1].isoformat()}"
            return catch_up_result(
                CatchUpOutcome.BOOKED, len(booked), f"Booked {len(booked)} paper day(s): {span}.{skip_note}"
            )
        if result.skipped:
            return catch_up_result(CatchUpOutcome.UP_TO_DATE, 0, f"No paper day could be booked.{skip_note}")
        if result.status is status.BEFORE_START:
            return catch_up_result(
                CatchUpOutcome.BEFORE_START, 0,
                f"Nothing to book before the first fill at the {PAPER_START.isoformat()} 00:00 UTC open.",
            )
        return catch_up_result(CatchUpOutcome.UP_TO_DATE, 0, "Every due paper day is already booked.")


def _refused_text(code: str, detail: str) -> str:
    return f"The ledger was refused ({code}: {detail}); it is never rewritten."


def _market_text(code: str, detail: str) -> str:
    return f"Public market data failed ({code}: {detail}); nothing was written. The next start tries again."


def catch_up_result(outcome: CatchUpOutcome, days_booked: int | None, detail: str) -> dict[str, Any]:
    """``days_booked`` is ``None`` when a failure may have come after some complete days were kept."""
    return {"ok": outcome in _OK_OUTCOMES, "status": outcome.value, "days_booked": days_booked, "detail": detail}


# --- payload parts ------------------------------------------------------------------------------


def base_payload(state: str, generated: str | None) -> dict[str, Any]:
    return {
        "state": state,
        "honesty_label": HONESTY_LABEL,
        "pre_tax_note": PRE_TAX_NOTE,
        "paper_start": PAPER_START.isoformat(),
        "capital": money(CAPITAL),
        "reason": None,
        "error": None,
        "days_booked": None,
        "days_skipped": 0,
        "skipped_days": [],
        "first_day": None,
        "last_day": None,
        "last_record_ts": None,
        "quotes": [],
        "generated_at": generated,
    }


def refused_payload(error: Exception, generated: str | None = None) -> dict[str, Any]:
    code, detail = _error_of(error)
    payload = base_payload(STATE_REFUSED, generated)
    payload["reason"] = "The trend paper ledger was refused; it is never rewritten."
    payload["error"] = {"code": code, "detail": detail}
    return payload


def unavailable_payload(generated: str | None = None) -> dict[str, Any]:
    payload = base_payload(STATE_UNAVAILABLE, generated)
    payload["reason"] = UNAVAILABLE_TEXT
    return payload


def _skipped(skipped: tuple[SkippedDay, ...]) -> list[dict[str, str]]:
    """The skipped paper days in order, each with its plain-English reason."""
    return [{"day": s.day.isoformat(), "reason": short(s.text)} for s in skipped]


def _row(book: Book, label: str, summary: Mapping[str, Any], last: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    mine = summary[book.id]
    comparator_rule = book.spec.comparator
    comparator_id = book.comparator
    theirs = summary[comparator_id] if comparator_id is not None else None
    record = last.get(book.id)
    return {
        "book": book.id,
        "rule": book.rule.value,
        "label": label,
        "fee_pct": fee_percent(book.fee),
        "currency": book.quote,
        "days": mine.days,
        "last_date": mine.last,
        "equity": money(mine.equity) if mine.days else None,
        "return_pct": percent(mine.ret) if mine.days else None,
        "max_drawdown_pct": percent(mine.mdd) if mine.days else None,
        "trades": mine.trades if mine.days else None,
        "fees": money(mine.fees) if mine.days else None,
        "comparator": {
            "rule": None if comparator_rule is None else comparator_rule.value,
            "book": comparator_id,
            "equity": money(theirs.equity) if theirs is not None and theirs.days else None,
            "return_pct": percent(theirs.ret) if theirs is not None and theirs.days else None,
        },
        "vs_buy_hold_pp": points(mine.ret, theirs.ret) if theirs is not None and mine.days and theirs.days else None,
        "assets": _assets(record),
    }


def _assets(record: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Per asset of the last booked record: exposure (``held_after``), target and the signal."""
    if record is None:
        return []
    assets = record.get("assets")
    if not isinstance(assets, Mapping):
        return []
    out: list[dict[str, Any]] = []
    for asset, values in assets.items():
        if not isinstance(values, Mapping):
            continue
        signal = values.get("signal")
        close_date = values.get("signal_close_date")
        out.append({
            "asset": str(asset),
            "exposure_pct": percent(values.get("held_after")),
            "target_pct": percent(values.get("target")),
            "signal_close_date": close_date if isinstance(close_date, str) else None,
            "signal": [
                {"name": str(name), "value": signal_value(value)}
                for name, value in (signal.items() if isinstance(signal, Mapping) else ())
            ],
        })
    return out


def from_config() -> TrendReader:
    """The reader of the radar's state dir (``config.STATE_DIR``) with the public Binance client."""
    from radar_v08 import config

    return TrendReader(config.STATE_DIR)
