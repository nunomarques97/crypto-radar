"""Kraken EUR trend paper books. Research only.

PAPER ONLY, PRE-TAX, NOT QUALIFIED: no order, account, credential or private endpoint. The 12 books
use the same signals as ``scripts/run_trend_paper.py`` (Binance public USDT daily closes, unchanged)
and fill at the Kraken public XBTEUR/ETHEUR daily open (no keys), with a fee per leg of 0.4% (maker
assumption) or 0.8% (taker sensitivity). The ledger is ``<state dir>/trend_paper/kraken_ledger.jsonl``;
``ledger.jsonl`` (the Binance books) is only ever read, by ``report``.

Commands:

    python scripts/run_trend_paper_kraken.py catch-up   book every missed Kraken day (idempotent)
    python scripts/run_trend_paper_kraken.py report     read-only report, no network: the 12 books and
                                                        the per-day fill difference vs the Binance EUR books
    python scripts/run_trend_paper_kraken.py run        catch-up, then report

``--state-dir DIR`` overrides the radar state dir (default ``RADAR_STATE_DIR`` or the repository).

Exit codes:
    0  done
    2  usage error
    3  BUSY: another run holds the Kraken ledger lock (or, for report, is writing it); nothing was written
    4  market data failed (network, rate limit, invalid rows, clock behind the exchange) or waiting
       for data (a due day is not settled by the public candles yet); nothing was written and the
       next start retries
    5  ledger refused (the Kraken ledger is torn, edited, invalid or unreadable); nothing was written

Skipped days (a Kraken open or a USDT signal close missing for good, confirmed by a second request)
are listed by ``report``. A refused ledger is never rewritten: see docs/guides/TREND-PAPER.md.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC
from pathlib import Path
from typing import TextIO

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from radar_v08.adapters.binance_public_klines import KlinesError  # noqa: E402
from radar_v08.adapters.kraken_public_ohlc import KrakenOhlcError  # noqa: E402
from radar_v08.adapters.trend_paper_store import (  # noqa: E402
    TrendPaperStoreError,
    TrendPaperStoreErrorCode,
    ledger_path,
    read_ledger,
    read_ledger_settled,
)
from radar_v08.domain.trend_paper import (  # noqa: E402
    Ledger,
    PaperError,
    PaperErrorCode,
)
from radar_v08.domain.trend_paper_kraken import (  # noqa: E402
    extend_kraken_ledger,
    render_kraken_report,
)
from radar_v08.trend_paper_hook import (  # noqa: E402
    GUIDE,
    CatchUpStatus,
    Clock,
    Fetcher,
    default_kraken_fetchers,
    kraken_catch_up,
    kraken_ledger_path,
    utc_now,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BUSY = 3
EXIT_MARKET = 4
EXIT_LEDGER = 5
LEDGER_CODES = frozenset({PaperErrorCode.LEDGER_TORN, PaperErrorCode.LEDGER_EDITED, PaperErrorCode.LEDGER_INVALID})
REFUSED_TEXT = f"nothing was written and the file is never rewritten; see {GUIDE}"
BEING_WRITTEN_TEXT = (
    "a Kraken catch-up is writing the Kraken ledger right now; nothing was read or written. Try again in a moment."
)


def exit_code_for(error: Exception) -> int:
    if isinstance(error, TrendPaperStoreError):
        busy = (TrendPaperStoreErrorCode.BUSY, TrendPaperStoreErrorCode.BEING_WRITTEN)
        return EXIT_BUSY if error.code in busy else EXIT_LEDGER
    if isinstance(error, PaperError):
        return EXIT_LEDGER if error.code in LEDGER_CODES else EXIT_MARKET
    return EXIT_MARKET


def binance_ledger(state_dir: Path) -> tuple[Ledger | None, str]:
    """The Binance books' ledger (read only, never locked or created), or ``None`` and why not."""
    try:
        return read_ledger_settled(ledger_path(state_dir)), ""
    except TrendPaperStoreError as error:
        if error.code is TrendPaperStoreErrorCode.BEING_WRITTEN:
            return None, "ledger.jsonl is being written"
        return None, f"ledger.jsonl unreadable ({error.code.value})"
    except PaperError as error:
        return None, f"ledger.jsonl refused ({error.code.value})"


def _report(out: TextIO, state_dir: Path, ledger: Ledger, clock: Clock) -> int:
    binance, note = binance_ledger(state_dir)
    now = clock().astimezone(UTC)
    out.write(render_kraken_report(ledger, now, binance, binance_note=note) + "\n")
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    fetchers_factory: Callable[[], tuple[Fetcher, Fetcher]] = default_kraken_fetchers,
    clock: Clock = utc_now,
    out: TextIO | None = None,
) -> int:
    out = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(
        prog="run_trend_paper_kraken.py", description="Kraken EUR trend paper books (research only; no orders)."
    )
    parser.add_argument("command", choices=("run", "report", "catch-up"))
    parser.add_argument("--state-dir", help="radar state dir (default: RADAR_STATE_DIR or the repository)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return EXIT_USAGE if exit_.code else EXIT_OK
    if args.state_dir:
        state_dir = Path(args.state_dir)
    else:
        from radar_v08 import config

        state_dir = Path(config.STATE_DIR)
    path = kraken_ledger_path(state_dir)

    if args.command == "report":
        try:
            ledger = read_ledger_settled(path, extend=extend_kraken_ledger)
        except TrendPaperStoreError as error:
            if error.code is TrendPaperStoreErrorCode.BEING_WRITTEN:
                out.write(f"Kraken ledger busy: {BEING_WRITTEN_TEXT}\n")
            else:
                out.write(f"Kraken ledger refused ({error}): {REFUSED_TEXT}\n")
            return exit_code_for(error)
        except PaperError as error:
            out.write(f"Kraken ledger refused ({error}): {REFUSED_TEXT}\n")
            return exit_code_for(error)
        return _report(out, state_dir, ledger, clock)

    binance, kraken = fetchers_factory()
    try:
        result = kraken_catch_up(state_dir, binance, kraken, clock=clock)
    except (TrendPaperStoreError, KlinesError, KrakenOhlcError, PaperError) as error:
        out.write(f"Kraken catch-up failed: {error}\n")
        code = exit_code_for(error)
        if code == EXIT_LEDGER:
            out.write(f"Kraken ledger refused: {REFUSED_TEXT}\n")
            return code
        try:
            booked = read_ledger(path, extend=extend_kraken_ledger)
            out.write(f"Kraken ledger now holds {len(booked.days)} paper day(s)\n")
        except (TrendPaperStoreError, PaperError):
            pass
        return code
    finally:
        binance.close()
        kraken.close()
    days = result.booked
    if result.status is CatchUpStatus.WAITING_FOR_DATA:
        out.write(
            f"Kraken catch-up: WAITING_FOR_DATA ({result.detail}); nothing was written and the next start retries; "
            f"Kraken ledger holds {len(result.ledger.days)} paper day(s)\n"
        )
    else:
        span = f" ({days[0]} .. {days[-1]})" if days else ""
        skipped = result.skipped
        skip_text = f"; skipped {len(skipped)} day(s) ({', '.join(d.isoformat() for d in skipped)})" if skipped else ""
        out.write(
            f"Kraken catch-up: {result.status.value}, appended {result.records_appended} records for {len(days)} "
            f"day(s){span}{skip_text}; Kraken ledger holds {len(result.ledger.days)} paper day(s)\n"
        )
    code = EXIT_MARKET if result.status is CatchUpStatus.WAITING_FOR_DATA else EXIT_OK
    if args.command == "catch-up":
        return code
    out.write("\n")
    report_code = _report(out, state_dir, result.ledger, clock)
    return code or report_code


if __name__ == "__main__":
    sys.exit(main())
