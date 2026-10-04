"""Trend paper catch-up service and the guarded radar start hook.

Research only: paper books, no order, no account, no credential, no private endpoint. The market
data are Binance public daily klines (:mod:`radar_v08.adapters.binance_public_klines`).

:func:`catch_up` books every missed day under the ledger lock: it reads and verifies the ledger,
fetches only what the missed days and today's signal need, and appends the days in order, each
whole. Before the first paper day (2026-10-04 UTC) nothing can be due, so it makes no request.

Every request happens before the first append: a network failure,
a refused or rate-limited request, invalid rows or a clock behind the exchange raise before a byte
is written. Every day up to the clock's UTC date is due; when the public data do not settle all
of them yet (a candle not published yet, data that end early, a missing candle seen in one
response only) the result is ``WAITING_FOR_DATA`` and nothing is written. A missing candle that
a later candle follows is requested a second time (one separate request per symbol); only when
that request confirms it is the day recorded as skipped.

:func:`start_catch_up_thread` is what ``python radar.py --mode loop`` calls at start (behind
``RADAR_TREND_PAPER_ENABLED``, default on). It starts one daemon thread (none while the previous one
is still alive) and returns at once; the
thread logs and swallows every exception, so a network, data, ledger or lock failure never reaches
the radar. The PC is not on all the time, so each radar start catches up on the days it missed.

After a catch-up that booked days, :func:`alert_exposure_changes` shows
one local Windows toast per registered rule whose reference book rebalanced, through
:func:`radar_v08.notifications.send_windows_notification` only (no ntfy, no event, no SQLite, no
network). Keys are claimed in ``<state dir>/trend_paper/alerts.jsonl`` before the toast, so a key
never toasts twice. Its failure is logged and swallowed like the catch-up's; the manual
``scripts/run_trend_paper.py`` never alerts.

Kraken EUR books: after the Binance step and its alerts, whatever their
outcome, :func:`run_guarded` runs :func:`run_kraken_guarded` in its own try/except. :func:`kraken_catch_up`
settles ``<state dir>/trend_paper/kraken_ledger.jsonl`` (own chain and lock) with the same signals
(Binance USDT closes, through its own public Binance client) and fills at the Kraken public daily
open (:mod:`radar_v08.adapters.kraken_public_ohlc`). The failure rules are the ones above: every
request before the first append, ``WAITING_FOR_DATA`` with nothing written, confirmed skips only.
Any failure there is logged and swallowed and never touches ``ledger.jsonl`` or ``alerts.jsonl``;
Kraken books never alert.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .adapters.trend_alert_store import CLAIMED, SUPERSEDED, alerts_path, claim
from .adapters.trend_paper_store import LedgerWriter, ledger_path
from .domain.trend_engine import day_open_ms
from .domain.trend_paper import (
    ALL_SYMBOLS,
    PAPER_START,
    DailySeries,
    Ledger,
    Market,
    PaperError,
    PaperErrorCode,
    catch_up_days,
    fetch_since,
    first_day_to_book,
)
from .domain.trend_paper_kraken import (
    KRAKEN_PAIRS,
    KRAKEN_SYMBOLS,
    catch_up_kraken_days,
    extend_kraken_ledger,
    kraken_skipped,
)
from .domain.trend_paper_kraken import fetch_since as kraken_fetch_since
from .trend_paper_alerts import exposure_changes, format_alert, plan_alerts

logger = logging.getLogger("radar_v08.trend_paper")

THREAD_NAME = "trend-paper-catch-up"
KRAKEN_LEDGER_NAME = "kraken_ledger.jsonl"
GUIDE = "docs/guides/TREND-PAPER.md"
LEDGER_REFUSED_CODES = frozenset({PaperErrorCode.LEDGER_TORN, PaperErrorCode.LEDGER_EDITED, PaperErrorCode.LEDGER_INVALID})
Clock = Callable[[], datetime]
_start_lock = threading.Lock()
_active: threading.Thread | None = None  # the catch-up thread last started by start_catch_up_thread


class Fetcher(Protocol):
    def fetch_daily(self, symbol: str, since: date, now_ms: int) -> DailySeries: ...

    def close(self) -> None: ...


class CatchUpStatus(StrEnum):
    BOOKED = "BOOKED"
    UP_TO_DATE = "UP_TO_DATE"
    BEFORE_START = "BEFORE_START"
    WAITING_FOR_DATA = "WAITING_FOR_DATA"  # due by the calendar, not settled by the public data yet


@dataclass(frozen=True)
class CatchUpResult:
    status: CatchUpStatus
    booked: tuple[date, ...]
    records_appended: int  # book records appended (24 per booked day; a skipped day adds none)
    ledger: Ledger
    market: Market | None = field(default=None, repr=False)
    skipped: tuple[date, ...] = ()
    detail: str = ""  # why it waits (WAITING_FOR_DATA only)


def utc_now() -> datetime:
    return datetime.now(UTC)


def default_fetcher() -> Fetcher:
    from .adapters.binance_public_klines import BinancePublicKlines

    return BinancePublicKlines()


def default_kraken_fetchers() -> tuple[Fetcher, Fetcher]:
    """The Kraken step's own clients: Binance public klines (signals) and Kraken public OHLC (fills)."""
    from .adapters.kraken_public_ohlc import KrakenPublicOhlc

    binance = default_fetcher()
    try:
        return binance, KrakenPublicOhlc()
    except Exception:
        binance.close()
        raise


def kraken_ledger_path(state_dir: str | Path) -> Path:
    return ledger_path(state_dir).with_name(KRAKEN_LEDGER_NAME)


def now_ms_of(now: datetime) -> int:
    utc = now.astimezone(UTC)
    return day_open_ms(utc.date()) + ((utc.hour * 60 + utc.minute) * 60 + utc.second) * 1000 + utc.microsecond // 1000


def fetch_market(fetcher: Fetcher, since: Mapping[str, date], now_ms: int) -> dict[str, DailySeries]:
    """The symbols of ``since`` in :data:`ALL_SYMBOLS` order; the first failure stops the rest."""
    return {symbol: fetcher.fetch_daily(symbol, since[symbol], now_ms) for symbol in ALL_SYMBOLS if symbol in since}


def catch_up(
    state_dir: str | Path,
    fetcher: Fetcher,
    *,
    clock: Clock = utc_now,
    start: date = PAPER_START,
    always_fetch: bool = False,
) -> CatchUpResult:
    """Settle every due day. Raises the typed store (``BUSY`` included), klines or paper errors,
    all before anything is written, except a store failure during an append (complete days
    appended before it are kept). Returns ``WAITING_FOR_DATA`` with nothing written when the
    public data do not settle every due day yet. ``always_fetch`` also fetches when nothing can
    be due (the ``run`` command uses that market for today's signal)."""
    now = clock().astimezone(UTC)
    path = ledger_path(state_dir)
    with LedgerWriter(path, start) as writer:
        ledger = writer.read()
        today = now.date()
        if first_day_to_book(ledger, start) > today and not always_fetch:
            return CatchUpResult(CatchUpStatus.BEFORE_START if today < start else CatchUpStatus.UP_TO_DATE, (), 0, ledger)
        now_ms = now_ms_of(now)
        market = fetch_market(fetcher, fetch_since(ledger, today, start), now_ms)
        ts = now.isoformat(timespec="seconds")
        try:
            booked = catch_up_days(
                ledger, market, ts, writer.appender(ledger), start, today=today,
                refetch=lambda since: fetch_market(fetcher, since, now_ms),
            )
        except PaperError as error:
            if error.code is not PaperErrorCode.WAITING_FOR_DATA:
                raise
            return CatchUpResult(CatchUpStatus.WAITING_FOR_DATA, (), 0, ledger, market, detail=error.detail)
        after = writer.read()
    skipped = tuple(s.day for s in after.skipped[len(ledger.skipped):])
    if booked:
        status = CatchUpStatus.BOOKED
    else:
        status = CatchUpStatus.BEFORE_START if today < start else CatchUpStatus.UP_TO_DATE
    return CatchUpResult(status, tuple(booked), len(after.records) - len(ledger.records), after, market, skipped)


def run_guarded(
    state_dir: str | Path,
    fetcher_factory: Callable[[], Fetcher] | None = None,
    clock: Clock | None = None,
    kraken_fetchers: Callable[[], tuple[Fetcher, Fetcher]] | None = None,
) -> CatchUpResult | None:
    """One catch-up that never raises: every failure is logged and swallowed. ``None`` means the
    module's :func:`default_fetcher`, :func:`default_kraken_fetchers` and :func:`utc_now`, looked up
    at call time. Returns the Binance step's result; the Kraken EUR step runs after it (and after its
    alerts) whatever its outcome, isolated in its own try/except."""
    result = _run_binance_guarded(state_dir, fetcher_factory, clock)
    try:
        run_kraken_guarded(state_dir, kraken_fetchers, clock)
    except Exception:  # run_kraken_guarded never raises; nothing may reach the radar either way
        logger.warning("Kraken EUR paper catch-up failed (the radar is unaffected)", exc_info=True)
    return result


def _run_binance_guarded(
    state_dir: str | Path,
    fetcher_factory: Callable[[], Fetcher] | None,
    clock: Clock | None,
) -> CatchUpResult | None:
    try:
        fetcher = (fetcher_factory or default_fetcher)()
    except Exception:
        logger.warning("Trend paper catch-up not run: the market data client failed to start", exc_info=True)
        return None
    try:
        result = catch_up(state_dir, fetcher, clock=clock or utc_now)
    except PaperError as error:
        if error.code in LEDGER_REFUSED_CODES:
            logger.warning(
                "Trend paper ledger refused (%s): nothing was written and the file is never rewritten; "
                "see %s (the radar is unaffected)", error, GUIDE,
            )
        else:
            logger.warning("Trend paper catch-up failed (the radar is unaffected): %s", error, exc_info=True)
        return None
    except Exception as error:
        logger.warning("Trend paper catch-up failed (the radar is unaffected): %s", error, exc_info=True)
        return None
    finally:
        try:
            fetcher.close()
        except Exception:
            logger.debug("Trend paper fetcher close failed", exc_info=True)
    if result.status is CatchUpStatus.WAITING_FOR_DATA:
        logger.info("Trend paper catch-up waiting for data (nothing written; the next start retries): %s", result.detail)
    else:
        logger.info(
            "Trend paper catch-up: %s, %d day(s) booked, %d day(s) skipped, %d record(s) appended",
            result.status.value, len(result.booked), len(result.skipped), result.records_appended,
        )
    if result.booked:
        try:
            alert_exposure_changes(state_dir, result, clock or utc_now)
        except Exception as error:
            logger.warning("Trend paper alert step failed (no toast; the radar is unaffected): %s", error, exc_info=True)
    return result


def alert_exposure_changes(state_dir: str | Path, result: CatchUpResult, clock: Clock = utc_now) -> list[tuple[str, str]]:
    """Toast each rule's latest exposure change in ``result.booked`` that was never handled, and
    return the keys toasted (attempted). Every key is claimed in the dedupe file before any toast;
    earlier changes of the same rule are recorded as superseded without a toast. Raises the
    detection or dedupe store errors before anything is sent; a failing toast is logged."""
    plan = plan_alerts(exposure_changes(result.ledger.records, result.booked))
    entries = [(c.key, SUPERSEDED) for c in plan.superseded] + [(c.key, CLAIMED) for c in plan.shown]
    if not entries:
        logger.info("Trend paper alerts: no exposure change on the booked day(s)")
        return []
    # The existing local toast path, imported only when needed.
    from . import notifications

    ts = clock().astimezone(UTC).isoformat(timespec="seconds")
    claimed = set(claim(alerts_path(state_dir), entries, ts))
    for change in plan.superseded:
        logger.info(
            "Trend paper alert: %s exposure change on %s superseded by a later change (no toast)",
            change.rule.value, change.day,
        )
    toasted: list[tuple[str, str]] = []
    for change in plan.shown:
        if change.key not in claimed:
            logger.info("Trend paper alert: %s on %s already handled (no toast)", change.rule.value, change.day)
            continue
        toasted.append(change.key)
        title, body = format_alert(change)
        try:
            sent = notifications.send_windows_notification(title, body)
        except Exception:
            logger.warning("Trend paper alert toast failed for %s on %s", change.rule.value, change.day, exc_info=True)
            continue
        if sent:
            logger.info("Trend paper alert toast shown: %s on %s (paper only)", change.rule.value, change.day)
        else:
            logger.info(
                "Trend paper alert toast not shown for %s on %s (notifications off or the toast failed)",
                change.rule.value, change.day,
            )
    return toasted


def start_catch_up_thread(
    state_dir: str | Path,
    fetcher_factory: Callable[[], Fetcher] | None = None,
    clock: Clock | None = None,
    kraken_fetchers: Callable[[], tuple[Fetcher, Fetcher]] | None = None,
) -> threading.Thread:
    """Start one guarded catch-up (Binance, then Kraken EUR) on a daemon thread and return immediately.
    At most one runs in this process: while the previous one is alive, it is returned and nothing
    new starts. The ledger locks still guard other processes."""
    global _active
    with _start_lock:
        if _active is not None and _active.is_alive():
            logger.info("Trend paper catch-up already running; no second one started")
            return _active
        thread = threading.Thread(
            target=run_guarded, args=(state_dir, fetcher_factory, clock, kraken_fetchers), name=THREAD_NAME, daemon=True
        )
        thread.start()
        _active = thread
    return thread


# ---------------------------------------------------------------------------
# Kraken EUR books
# ---------------------------------------------------------------------------


def fetch_kraken_market(
    binance: Fetcher, kraken: Fetcher, since: Mapping[str, date], now_ms: int
) -> dict[str, DailySeries]:
    """The symbols of ``since`` in :data:`KRAKEN_SYMBOLS` order: the USDT series from ``binance``, the
    Kraken pairs from ``kraken``; the first failure stops the rest."""
    return {
        symbol: (kraken if symbol in KRAKEN_PAIRS else binance).fetch_daily(symbol, since[symbol], now_ms)
        for symbol in KRAKEN_SYMBOLS
        if symbol in since
    }


def kraken_catch_up(
    state_dir: str | Path,
    binance: Fetcher,
    kraken: Fetcher,
    *,
    clock: Clock = utc_now,
    start: date = PAPER_START,
) -> CatchUpResult:
    """Settle every due Kraken EUR day under the Kraken ledger's own lock, as :func:`catch_up` does
    for the Binance books: every request before the first append, ``WAITING_FOR_DATA`` with nothing
    written when the data do not settle every due day, confirmed skips only. Raises the typed store
    (``BUSY`` included), Binance, Kraken or paper errors before anything is written."""
    now = clock().astimezone(UTC)
    path = kraken_ledger_path(state_dir)
    with LedgerWriter(path, start, extend_kraken_ledger) as writer:
        ledger = writer.read()
        today = now.date()
        if first_day_to_book(ledger, start) > today:
            return CatchUpResult(CatchUpStatus.BEFORE_START if today < start else CatchUpStatus.UP_TO_DATE, (), 0, ledger)
        now_ms = now_ms_of(now)
        market = fetch_kraken_market(binance, kraken, kraken_fetch_since(ledger, today, start), now_ms)
        ts = now.isoformat(timespec="seconds")
        try:
            booked = catch_up_kraken_days(
                ledger, market, ts, writer.appender(ledger), start, today=today,
                refetch=lambda since: fetch_kraken_market(binance, kraken, since, now_ms),
            )
        except PaperError as error:
            if error.code is not PaperErrorCode.WAITING_FOR_DATA:
                raise
            return CatchUpResult(CatchUpStatus.WAITING_FOR_DATA, (), 0, ledger, market, detail=error.detail)
        after = writer.read()
    skipped = tuple(s.day for s in kraken_skipped(after)[len(kraken_skipped(ledger)):])
    if booked:
        status = CatchUpStatus.BOOKED
    else:
        status = CatchUpStatus.BEFORE_START if today < start else CatchUpStatus.UP_TO_DATE
    return CatchUpResult(status, tuple(booked), len(after.records) - len(ledger.records), after, market, skipped)


def run_kraken_guarded(
    state_dir: str | Path,
    fetchers_factory: Callable[[], tuple[Fetcher, Fetcher]] | None = None,
    clock: Clock | None = None,
) -> CatchUpResult | None:
    """One Kraken EUR catch-up that never raises and never alerts: every failure is logged and
    swallowed. ``None`` means :func:`default_kraken_fetchers` and :func:`utc_now`, looked up at call time."""
    try:
        binance, kraken = (fetchers_factory or default_kraken_fetchers)()
    except Exception:
        logger.warning(
            "Kraken EUR paper catch-up not run: a market data client failed to start (the radar is unaffected)",
            exc_info=True,
        )
        return None
    try:
        result = kraken_catch_up(state_dir, binance, kraken, clock=clock or utc_now)
    except PaperError as error:
        if error.code in LEDGER_REFUSED_CODES:
            logger.warning(
                "Kraken EUR paper ledger refused (%s): nothing was written and the file is never rewritten; "
                "see %s (the radar is unaffected)", error, GUIDE,
            )
        else:
            logger.warning("Kraken EUR paper catch-up failed (the radar is unaffected): %s", error, exc_info=True)
        return None
    except Exception as error:
        logger.warning("Kraken EUR paper catch-up failed (the radar is unaffected): %s", error, exc_info=True)
        return None
    finally:
        for client in (binance, kraken):
            try:
                client.close()
            except Exception:
                logger.debug("Kraken EUR paper fetcher close failed", exc_info=True)
    if result.status is CatchUpStatus.WAITING_FOR_DATA:
        logger.info(
            "Kraken EUR paper catch-up waiting for data (nothing written; the next start retries): %s", result.detail
        )
    else:
        logger.info(
            "Kraken EUR paper catch-up: %s, %d day(s) booked, %d day(s) skipped, %d record(s) appended (no alert)",
            result.status.value, len(result.booked), len(result.skipped), result.records_appended,
        )
    return result
