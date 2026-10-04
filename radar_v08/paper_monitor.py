"""Paper position monitor: a light watch of the open paper plays between heartbeat cycles.

Pretend money only - nothing here sends an order or calls a private API. In the spirit of
the Position Manager of ``docs/EXECUTION_ARCHITECTURE.md`` section 8 (a deterministic
timer, no model), a daemon thread started by ``radar.py --mode loop`` ticks every
``config.PAPER_MONITOR_SECONDS``. Each tick:

1. reads the open plays that have EX-1 levels (legacy plays keep the old rule and are
   left to the heartbeat); with none open it sends no request;
2. sends exactly one public Ticker request filtered to their pairs (at most
   ``config.PAPER_MAX_OPEN``), through the allowlisted ``GuardedSession`` with a short
   timeout and no retry backoff;
3. takes, for each play, the quote under exactly its pair key; a missing, malformed,
   crossed or non-positive quote is ignored, never read as zero. Kraken's Ticker has no
   source time, so the observation time is the receipt time from the injected clock;
4. hands those quotes to ``paper_store.close_plays`` with source ``ticker``: the earliest
   touching observation among the recorded snapshots and this tick closes the play, at
   its executable price, with its reason, lag and costs.

With ``RADAR_PILOT_ENABLED`` the same tick also watches the open pilot shadow position:
its pair joins the one Ticker request (the pair cap is raised by one for it, the
pilot's single position) and every observed quote is offered to
``pilot_store.close_positions``, which closes a touched position (the quote is kept in
``pilot_exit_quotes``) and evaluates the loss locks on it. A pilot failure is logged and
counted (``pilot_failures``) without undoing or hiding the game's closes; the monitor
never creates the pilot tables.

The thread has its own SQLite connection (``mode=rw``: it never creates the database or
its tables) with a short busy timeout, and its own ``GuardedSession``; it never takes the
heartbeat store's lock. Both writers use short ``BEGIN IMMEDIATE`` transactions and a
play has at most one close (``UNIQUE(play_id)``). Every failure of a tick - network,
HTTP, malformed JSON, a security refusal, a busy or failing database - is caught, logged
and counted; no play is closed on it and the thread keeps running. ``MonitorHandle.stop``
ends the thread (stop event + join); it is a daemon, so it never keeps the process alive.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

from . import config, security
from .adapters import paper_store, pilot_store
from .adapters.kraken_timestamps import Clock, fetch_spot_ticker, receipt_time
from .clock_reference import default_clock_reference
from .domain import paper
from .http_client import ApiError, GuardedSession
from .kraken_spot import SpotApiError

logger = logging.getLogger("radar_v08.paper_monitor")

#: ``exit_source`` of a close taken from a monitor Ticker read.
TICKER_SOURCE = "ticker"
#: A Kraken spot pair key as stored on a play; anything else (a comma, a space) is never
#: put in the request filter.
_PAIR_KEY = re.compile(r"[A-Za-z0-9._-]{1,40}")
#: A Ticker price as Kraken sends it: plain decimal text.
_PRICE_TEXT = re.compile(r"[0-9]{1,20}(?:\.[0-9]{1,20})?")


class TickOutcome(Enum):
    #: No open play with EX-1 levels (or no paper tables yet): no request was sent.
    IDLE = "idle"
    #: One Ticker request was answered; zero or more plays were closed.
    WATCHED = "watched"
    #: Transport failure, timeout or non-2xx answer (``ApiError``/``OSError``).
    REQUEST_FAILED = "request_failed"
    #: The answer was not the expected JSON shape, or Kraken returned an error list.
    MALFORMED = "malformed"
    #: The allowlist refused the request or a redirect.
    SECURITY_REFUSED = "security_refused"
    #: The database was busy or locked past the short timeout: the tick was skipped.
    DB_BUSY = "db_busy"
    #: Any other database failure (including a database that cannot be opened).
    DB_ERROR = "db_error"
    #: Anything unexpected; caught so the thread keeps running.
    FAILED = "failed"


_FAILURES = frozenset(
    {
        TickOutcome.REQUEST_FAILED,
        TickOutcome.MALFORMED,
        TickOutcome.SECURITY_REFUSED,
        TickOutcome.DB_BUSY,
        TickOutcome.DB_ERROR,
        TickOutcome.FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class TickResult:
    outcome: TickOutcome
    #: The pairs the Ticker request was filtered to (empty when none was sent).
    requested_pairs: tuple[str, ...] = ()
    #: Pairs whose quote in the answer was valid and offered to the close.
    quoted_pairs: tuple[str, ...] = ()
    #: Play ids closed by this tick.
    closed: tuple[int, ...] = ()
    detail: str | None = None
    #: Pilot shadow position ids closed by this tick.
    pilot_closed: tuple[int, ...] = ()


def _is_busy(error: BaseException) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    text = str(error).lower()
    return "database is locked" in text or "database is busy" in text or "database table is locked" in text


def _db_outcome(error: BaseException) -> TickOutcome:
    store_errors = (paper_store.PaperStoreError, pilot_store.PilotStoreError)
    cause = error.__cause__ if isinstance(error, store_errors) else error
    if isinstance(cause, sqlite3.Error) and _is_busy(cause):
        return TickOutcome.DB_BUSY
    return TickOutcome.DB_ERROR


def _ticker_price(value: object) -> Decimal | None:
    if not isinstance(value, str) or not _PRICE_TEXT.fullmatch(value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:  # pragma: no cover - the pattern only lets digits through
        return None


def _first(value: object) -> object:
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return None


def ticker_quote(row: object) -> paper.Quote | None:
    """The bid/ask of one Ticker row (``b[0]``, ``a[0]``) when it is a valid quote, else ``None``."""
    if not isinstance(row, Mapping):
        return None
    bid = _ticker_price(_first(row.get("b")))
    ask = _ticker_price(_first(row.get("a")))
    quote = paper.validate_quote(bid, ask)
    return None if isinstance(quote, paper.QuoteProblem) else quote


def _wall_clock() -> datetime:
    return datetime.now(UTC)


def default_clock() -> Clock:
    """The clock the heartbeat stamps its snapshots with: the corrected clock when
    ``RADAR_CLOCK_CORRECTION_ENABLED``, else the UTC wall clock."""
    if config.RADAR_CLOCK_CORRECTION_ENABLED:
        return default_clock_reference().now
    return _wall_clock


def default_session() -> GuardedSession:
    """One attempt with a short timeout and no retry backoff: a tick stays near its interval."""
    return GuardedSession(config.PAPER_MONITOR_HTTP_TIMEOUT_SECONDS, 0, 0.0)


def _read_write_uri(path: str | Path) -> str:
    return Path(path).resolve().as_uri() + "?mode=rw"


class PaperMonitor:
    """One monitor: ``tick`` is one watch pass, ``run`` the thread body.

    ``connect`` opens the monitor's own connection (by default the database at
    ``db_path``, read-write, never created); it is opened lazily in the thread that
    ticks and reopened after a database failure.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        session: GuardedSession,
        clock: Clock,
        interval_seconds: float,
        busy_timeout_seconds: float = config.PAPER_MONITOR_BUSY_TIMEOUT_SECONDS,
        max_pairs: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        connect: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        if not interval_seconds > 0:
            raise ValueError("interval_seconds must be > 0")
        self.db_path = str(db_path)
        self.interval_seconds = float(interval_seconds)
        self._session = session
        self._clock = clock
        self._busy_timeout = busy_timeout_seconds
        self._max_pairs = max_pairs or (lambda: config.PAPER_MAX_OPEN)
        self._monotonic = monotonic
        self._connect = connect or self._default_connect
        self._conn: sqlite3.Connection | None = None
        self._stats_lock = threading.Lock()
        self._counts: dict[str, int] = {outcome.value: 0 for outcome in TickOutcome}
        self._counts.update(ticks=0, requests=0, closed=0, pilot_closed=0, pilot_failures=0)
        self._failing: TickOutcome | None = None
        # Warn once, not every tick: plays whose pair key is never requested, and a pair cap.
        self._warned_plays: set[int] = set()
        self._warned_positions: set[int] = set()
        self._capped = False

    # --- connection -------------------------------------------------------------------

    def _default_connect(self) -> sqlite3.Connection:
        return sqlite3.connect(_read_write_uri(self.db_path), uri=True, timeout=self._busy_timeout)

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        """Close the monitor's connection and session (called by ``run`` on exit)."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - closing an idle connection
                pass
        try:
            self._session.close()
        except Exception:  # pragma: no cover - closing a requests.Session
            pass

    def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()
            except sqlite3.Error:
                pass

    # --- one tick ---------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """A copy of the counters: ticks, requests, closed, pilot_closed, pilot_failures and
        one count per outcome."""
        with self._stats_lock:
            return dict(self._counts)

    def _watched(self, conn: sqlite3.Connection) -> list[paper_store.StoredPlay]:
        if not paper_store.schema_present(conn):
            return []
        plays = [play for play in paper_store.read_open_plays(conn) if play.has_levels]
        unsafe = {play.play_id for play in plays if not _PAIR_KEY.fullmatch(play.pair)}
        if unsafe - self._warned_plays:
            logger.warning(
                "Paper monitor: plays %s have a pair key it will not request; left to the heartbeat",
                sorted(unsafe - self._warned_plays),
            )
            self._warned_plays |= unsafe
        return [play for play in plays if play.play_id not in unsafe]

    def _pilot_failed(self, what: str, error: BaseException) -> None:
        with self._stats_lock:
            self._counts["pilot_failures"] += 1
        logger.warning("Paper monitor: pilot shadow %s failed: %s: %s", what, type(error).__name__, error)

    def _pilot_watched(self, conn: sqlite3.Connection) -> list[pilot_store.StoredPosition]:
        """The open pilot positions with a pair key it may request; none when the pilot is
        off, has no tables yet or cannot be read (counted, never raised)."""
        if not config.RADAR_PILOT_ENABLED:
            return []
        try:
            if not pilot_store.schema_present(conn):
                return []
            positions = list(pilot_store.read_open_positions(conn))
        except Exception as error:  # deliberately broad: the game's watch must go on
            self._pilot_failed("read", error)
            return []
        unsafe = {position.position_id for position in positions if not _PAIR_KEY.fullmatch(position.pair)}
        if unsafe - self._warned_positions:
            logger.warning(
                "Paper monitor: pilot positions %s have a pair key it will not request; left to the heartbeat",
                sorted(unsafe - self._warned_positions),
            )
            self._warned_positions |= unsafe
        return [position for position in positions if position.position_id not in unsafe]

    def _close_pilot(
        self,
        conn: sqlite3.Connection,
        positions: Sequence[pilot_store.StoredPosition],
        quotes: Sequence[paper_store.ObservedQuote],
        now: datetime,
    ) -> tuple[int, ...]:
        """Offer the tick's quotes to the pilot close path and its lock evaluation."""
        quoted = {quote.pair for quote in quotes}
        wanted = {position.position_id for position in positions if position.pair in quoted}
        if not wanted:
            return ()
        try:
            report = pilot_store.close_positions(conn, now=now, extra_quotes=quotes, position_ids=wanted)
        except Exception as error:  # deliberately broad: the game's closes stand
            self._pilot_failed("close", error)
            return ()
        for close in report.closed:
            logger.info(
                "Paper monitor: pilot position %d closed (%s) on %s bid=%s ask=%s net=%s",
                close.position_id,
                close.exit_reason.value,
                close.exit_source,
                close.exit_bid,
                close.exit_ask,
                close.net,
            )
        for lock in report.locks.tripped:
            logger.warning("Paper monitor: pilot shadow %s lock tripped at equity %s", lock.kind.value, lock.equity)
        return tuple(close.position_id for close in report.closed)

    @staticmethod
    def _pairs(plays: Sequence[paper_store.StoredPlay], limit: int) -> tuple[str, ...]:
        pairs: list[str] = []
        for play in plays:  # oldest play first
            if play.pair not in pairs:
                pairs.append(play.pair)
        return tuple(pairs[: max(0, limit)])

    def _watch(self) -> TickResult:
        conn = self._connection()
        plays = self._watched(conn)
        game_pairs = self._pairs(plays, self._max_pairs())
        positions = self._pilot_watched(conn)
        pilot_pairs: list[str] = []
        for position in positions:  # oldest first; the pilot holds at most one
            if position.pair not in game_pairs and position.pair not in pilot_pairs:
                pilot_pairs.append(position.pair)
        pairs = game_pairs + tuple(pilot_pairs[: config.PILOT_MAX_POSITIONS])
        if not pairs:
            return TickResult(TickOutcome.IDLE)
        capped = len(game_pairs) < len({play.pair for play in plays})
        if capped and not self._capped:
            logger.warning("Paper monitor: more open pairs than %d; the rest are left to the heartbeat", len(pairs))
        self._capped = capped
        with self._stats_lock:
            self._counts["requests"] += 1
        fetched = fetch_spot_ticker(self._session, self._clock, list(pairs))
        raw = fetched.raw
        if not isinstance(raw, Mapping):
            return TickResult(TickOutcome.MALFORMED, pairs, detail=f"Ticker result is {type(raw).__name__}")
        observed_at = fetched.timing.received_at
        quotes: list[paper_store.ObservedQuote] = []
        for pair in pairs:
            quote = ticker_quote(raw.get(pair))
            if quote is not None:
                quotes.append(paper_store.ObservedQuote(pair, quote.bid, quote.ask, observed_at, TICKER_SOURCE))
        quoted = tuple(quote.pair for quote in quotes)
        if not quotes:
            return TickResult(TickOutcome.WATCHED, pairs)
        now = max(receipt_time(self._clock), observed_at)
        play_ids = {play.play_id for play in plays if play.pair in quoted}
        closed: tuple[paper_store.StoredClose, ...] = ()
        if play_ids:
            closed = paper_store.close_plays(conn, now=now, extra_quotes=quotes, play_ids=play_ids).closed
        pilot_closed = self._close_pilot(conn, positions, quotes, now)
        for close in closed:
            logger.info(
                "Paper monitor: play %d closed (%s) on %s bid=%s ask=%s net=%s",
                close.play_id,
                close.exit_reason.value if close.exit_reason is not None else "legacy",
                close.exit_source,
                close.exit_bid,
                close.exit_ask,
                close.net,
            )
        return TickResult(
            TickOutcome.WATCHED, pairs, quoted, tuple(close.play_id for close in closed), pilot_closed=pilot_closed
        )

    def tick(self) -> TickResult:
        """One watch pass. Never raises: every failure is returned as its outcome."""
        try:
            result = self._watch()
        except security.SecurityViolation as error:
            result = TickResult(TickOutcome.SECURITY_REFUSED, detail=str(error))
        except SpotApiError as error:
            # Kraken answered with its own error list: an answer, but not a usable one.
            result = TickResult(TickOutcome.MALFORMED, detail=str(error))
        except (paper_store.PaperStoreError, sqlite3.Error) as error:
            outcome = _db_outcome(error)
            if outcome is TickOutcome.DB_ERROR:
                self._drop_connection()
            result = TickResult(outcome, detail=str(error))
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            # response.json() on a non-JSON body (requests' JSONDecodeError is a ValueError,
            # checked before RequestException), or a body without an object "result".
            result = TickResult(TickOutcome.MALFORMED, detail=f"{type(error).__name__}: {error}")
        except (ApiError, OSError) as error:
            # OSError covers requests.RequestException (an IOError subclass), e.g. the
            # HTTPError of a 4xx answer; only http_client may import requests.
            result = TickResult(TickOutcome.REQUEST_FAILED, detail=str(error))
        except Exception as error:  # deliberately broad: the monitor thread must keep running
            result = TickResult(TickOutcome.FAILED, detail=f"{type(error).__name__}: {error}")
        self._account(result)
        return result

    def _account(self, result: TickResult) -> None:
        with self._stats_lock:
            self._counts["ticks"] += 1
            self._counts[result.outcome.value] += 1
            self._counts["closed"] += len(result.closed)
            self._counts["pilot_closed"] += len(result.pilot_closed)
        if result.outcome in _FAILURES:
            # One warning per streak (or change of cause); the rest at debug, so a network
            # outage does not write a line every few seconds.
            level = logging.WARNING if self._failing is not result.outcome else logging.DEBUG
            logger.log(level, "Paper monitor tick skipped (%s): %s", result.outcome.value, result.detail)
            self._failing = result.outcome
        elif self._failing is not None:
            logger.info("Paper monitor recovered after %s", self._failing.value)
            self._failing = None

    # --- the thread -------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Tick, then wait out the rest of the interval, until ``stop`` is set."""
        try:
            while not stop.is_set():
                started = self._monotonic()
                self.tick()
                remaining = self.interval_seconds - (self._monotonic() - started)
                if stop.wait(max(0.0, remaining)):
                    break
        finally:
            self.close()


@dataclass(slots=True)
class MonitorHandle:
    monitor: PaperMonitor
    thread: threading.Thread
    stop_event: threading.Event

    def stop(self, timeout: float | None = None) -> bool:
        """Ask the thread to stop and join it; ``True`` when it has ended."""
        self.stop_event.set()
        if timeout is None:
            timeout = config.PAPER_MONITOR_HTTP_TIMEOUT_SECONDS * 2 + self.monitor.interval_seconds + 2.0
        self.thread.join(timeout)
        return not self.thread.is_alive()


def monitor_enabled() -> bool:
    return config.RADAR_PAPER_ENABLED and config.RADAR_PAPER_MONITOR_ENABLED


def start_monitor(
    db_path: str | Path | None = None,
    *,
    session: GuardedSession | None = None,
    clock: Clock | None = None,
    interval_seconds: float | None = None,
    connect: Callable[[], sqlite3.Connection] | None = None,
) -> MonitorHandle | None:
    """Start the monitor thread when ``RADAR_PAPER_ENABLED`` and ``RADAR_PAPER_MONITOR_ENABLED``
    are both on; ``None`` (no thread, nothing requested) otherwise."""
    if not monitor_enabled():
        logger.info("Paper monitor off (RADAR_PAPER_ENABLED / RADAR_PAPER_MONITOR_ENABLED)")
        return None
    monitor = PaperMonitor(
        db_path if db_path is not None else config.SQLITE_PATH,
        session=session if session is not None else default_session(),
        clock=clock if clock is not None else default_clock(),
        interval_seconds=interval_seconds if interval_seconds is not None else config.PAPER_MONITOR_SECONDS,
        connect=connect,
    )
    stop_event = threading.Event()
    thread = threading.Thread(target=monitor.run, args=(stop_event,), name="paper-monitor", daemon=True)
    thread.start()
    logger.info("Paper monitor started: every %.0fs", monitor.interval_seconds)
    return MonitorHandle(monitor, thread, stop_event)
