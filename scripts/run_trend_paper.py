"""Trend paper trading of the four ported trend rules. Research only.

PAPER ONLY, PRE-TAX, NOT QUALIFIED: no order, account, credential or private endpoint. Market data
are Binance public daily klines. The ledger is ``<state dir>/trend_paper/ledger.jsonl``.

Commands:

    python scripts/run_trend_paper.py catch-up          book every missed day (idempotent)
    python scripts/run_trend_paper.py report            read-only report with today's signal
    python scripts/run_trend_paper.py report --offline  read-only report, no network
    python scripts/run_trend_paper.py run               catch-up, then report

``--state-dir DIR`` overrides the radar state dir (default ``RADAR_STATE_DIR`` or the repository).

Exit codes:
    0  done
    2  usage error
    3  BUSY: another run holds the ledger lock (or, for report, is writing it); nothing was written
    4  market data failed (network, rate limit or ban, invalid rows, clock behind the exchange) or
       waiting for data (a due day is not settled by the public candles yet); nothing was written
       and the next start retries
    5  ledger refused (torn, edited, invalid or unreadable); nothing was written
    6  the imported registry check failed (the report is still printed)

Skipped days (a public candle missing for good, confirmed by a second request) are listed by
``report``. A refused ledger is never rewritten: see docs/guides/TREND-PAPER.md.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from radar_v08.adapters.binance_public_klines import KlinesError  # noqa: E402
from radar_v08.adapters.trend_paper_store import (  # noqa: E402
    TrendPaperStoreError,
    TrendPaperStoreErrorCode,
    ledger_path,
    read_ledger,
    read_ledger_settled,
)
from radar_v08.adapters.trend_registry_store import (  # noqa: E402
    RegistryStoreError,
    load_imported_records,
)
from radar_v08.domain.trend_paper import (  # noqa: E402
    DaySignals,
    Ledger,
    Market,
    PaperError,
    PaperErrorCode,
    RegistryStatus,
    day_signals,
    fetch_since,
    last_bookable_day,
    render_report,
)
from radar_v08.domain.trend_registry import RegistryError  # noqa: E402
from radar_v08.trend_paper_hook import (  # noqa: E402
    GUIDE,
    CatchUpStatus,
    Clock,
    Fetcher,
    catch_up,
    default_fetcher,
    fetch_market,
    now_ms_of,
    utc_now,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BUSY = 3
EXIT_MARKET = 4
EXIT_LEDGER = 5
EXIT_REGISTRY = 6
LEDGER_CODES = frozenset({PaperErrorCode.LEDGER_TORN, PaperErrorCode.LEDGER_EDITED, PaperErrorCode.LEDGER_INVALID})


def registry_status() -> RegistryStatus:
    try:
        registry = load_imported_records().registry
    except (RegistryStoreError, RegistryError) as error:
        return RegistryStatus(False, None, (), str(error))
    names = tuple(n for n in registry.registered_names() if registry.holdout_used(n))
    return RegistryStatus(True, registry.trial_count(), names)


REFUSED_TEXT = f"nothing was written and the file is never rewritten; see {GUIDE}"
BEING_WRITTEN_TEXT = "a catch-up is writing the ledger right now; nothing was read or written. Try again in a moment."


def exit_code_for(error: Exception) -> int:
    if isinstance(error, TrendPaperStoreError):
        busy = (TrendPaperStoreErrorCode.BUSY, TrendPaperStoreErrorCode.BEING_WRITTEN)
        return EXIT_BUSY if error.code in busy else EXIT_LEDGER
    if isinstance(error, PaperError):
        return EXIT_LEDGER if error.code in LEDGER_CODES else EXIT_MARKET
    return EXIT_MARKET


def _signals(market: Market) -> DaySignals | None:
    day = last_bookable_day(market)
    return None if day is None else day_signals(market, day)


def _report(
    out: TextIO, ledger: Ledger, now: datetime, market: Market | None, *, offline: bool, signal_error: str | None
) -> int:
    status = registry_status()
    signals: DaySignals | None = None
    if market is not None:
        try:
            signals = _signals(market)
        except PaperError as error:
            signal_error = str(error)
    out.write(render_report(ledger, status, now, signals, offline=offline, signal_error=signal_error) + "\n")
    return EXIT_OK if status.chain_ok else EXIT_REGISTRY


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher_factory: Callable[[], Fetcher] = default_fetcher,
    clock: Clock = utc_now,
    out: TextIO | None = None,
) -> int:
    out = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(
        prog="run_trend_paper.py", description="Trend paper trading (research only; no orders)."
    )
    parser.add_argument("command", choices=("run", "report", "catch-up"))
    parser.add_argument("--offline", action="store_true", help="report only: skip the network")
    parser.add_argument("--state-dir", help="radar state dir (default: RADAR_STATE_DIR or the repository)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return EXIT_USAGE if exit_.code else EXIT_OK
    if args.offline and args.command != "report":
        out.write("--offline applies to report only\n")
        return EXIT_USAGE
    if args.state_dir:
        state_dir = Path(args.state_dir)
    else:
        from radar_v08 import config

        state_dir = Path(config.STATE_DIR)
    path = ledger_path(state_dir)

    if args.command == "report":
        try:
            ledger = read_ledger_settled(path)
        except TrendPaperStoreError as error:
            if error.code is TrendPaperStoreErrorCode.BEING_WRITTEN:
                out.write(f"ledger busy: {BEING_WRITTEN_TEXT}\n")
            else:
                out.write(f"ledger refused ({error}): {REFUSED_TEXT}\n")
            return exit_code_for(error)
        except PaperError as error:
            out.write(f"ledger refused ({error}): {REFUSED_TEXT}\n")
            return exit_code_for(error)
        now = clock().astimezone(UTC)
        market: Market | None = None
        signal_error: str | None = None
        code = EXIT_OK
        if not args.offline:
            fetcher = fetcher_factory()
            try:
                market = fetch_market(fetcher, fetch_since(ledger, now.date()), now_ms_of(now))
            except (KlinesError, PaperError) as error:
                signal_error = str(error)
                code = EXIT_MARKET
            finally:
                fetcher.close()
        report_code = _report(out, ledger, now, market, offline=args.offline, signal_error=signal_error)
        return code or report_code

    fetcher = fetcher_factory()
    try:
        result = catch_up(state_dir, fetcher, clock=clock, always_fetch=args.command == "run")
    except (TrendPaperStoreError, KlinesError, PaperError) as error:
        out.write(f"catch-up failed: {error}\n")
        code = exit_code_for(error)
        if code == EXIT_LEDGER:
            out.write(f"ledger refused: {REFUSED_TEXT}\n")
            return code
        try:
            booked = read_ledger(path)
            out.write(f"ledger now holds {len(booked.days)} paper day(s)\n")
        except (TrendPaperStoreError, PaperError):
            pass
        return code
    finally:
        fetcher.close()
    days = result.booked
    if result.status is CatchUpStatus.WAITING_FOR_DATA:
        out.write(
            f"catch-up: WAITING_FOR_DATA ({result.detail}); nothing was written and the next start retries; "
            f"ledger holds {len(result.ledger.days)} paper day(s)\n"
        )
    else:
        span = f" ({days[0]} .. {days[-1]})" if days else ""
        skipped = result.skipped
        skip_text = f"; skipped {len(skipped)} day(s) ({', '.join(d.isoformat() for d in skipped)})" if skipped else ""
        out.write(
            f"catch-up: {result.status.value}, appended {result.records_appended} records for {len(days)} day(s){span}"
            f"{skip_text}; ledger holds {len(result.ledger.days)} paper day(s)\n"
        )
    code = EXIT_MARKET if result.status is CatchUpStatus.WAITING_FOR_DATA else EXIT_OK
    if args.command == "catch-up":
        return code
    out.write("\n")
    report_code = _report(out, result.ledger, clock().astimezone(UTC), result.market, offline=False, signal_error=None)
    return code or report_code


if __name__ == "__main__":
    sys.exit(main())
