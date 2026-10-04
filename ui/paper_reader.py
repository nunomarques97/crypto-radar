"""Read-only view of the paper game ("AI Game", DESIGN.md) for ``Api.get_paper_state``.

The radar process owns every write to the paper tables (``paper_store.ensure_schema``,
``ensure_wallet``, ``open_candidates``, ``settle_due``). This module never calls them: it
opens its own SQLite connection with the URI ``mode=ro`` (plus ``PRAGMA query_only``) for
each read and issues SELECTs only, so opening the game screen cannot create a table or a
row in the real database. A database without the paper tables reads as "not available yet".

Everything shown is derived from recorded rows: the wallet from ``paper_wallet`` plus the
recorded close of every play, the open plays from ``paper_plays`` and the spot snapshots
recorded since their entry, the agents' activity from ``radar_runs``, real (non-mock)
``RADAR_ALERT`` events and the router decision recorded on them (``model_demand`` SONNET or
FABLE), ``qwen_reviews`` and the plays themselves. The Claude API stays off for paper trading
(DESIGN.md, decision of 2026-09-29), so the Strategist and the Boss react to the
router's decision only; ``model_analyses`` is never read. A value that was not recorded is
``None``, never zero.

Since the EX-1 exit policy an open play also carries its frozen exit plan
(policy id, stop, target, maximum hold and due time) and a closed play its recorded exit
reason, source and record lag. A legacy play (opened before the policy) or a database whose
paper tables were not migrated yet has ``None`` there: nothing is estimated for it. The stop
and target are shown at the price precision of the play's own entry quote (the finest the
pair was quoted at); the exit itself is decided on the full frozen value.

Valuation
-------------------------

``wallet.valuation`` reports the wallet at ``as_of`` without changing any legacy field:

* ``realized_balance`` = start + every recorded close net; ``realized_pnl`` = that - start.
* ``open_cost_basis`` = the stakes of the open plays. The paper game takes no fee at the
  open (``domain.paper.settle`` charges both leg fees at the close), so the stake is the
  whole cost basis and ``free_cash`` = realized balance - open cost basis.
* ``liquidation_value`` of an open play = stake + ``paper.settle(direction, stake, stored
  fee_bps, entry quote, mark).net``: closed now at the executable side of the mark, with
  the spread and the assumed fee of both legs counted exactly once (the entry fee sits in
  that net only, never also in the cost basis). ``open_net_pnl`` = that net.
* Identity: ``total_equity = free_cash + liquidation_value = realized_balance +
  open_net_pnl``.

A mark is the latest valid quote (``paper.validate_quote``: finite, positive, not crossed,
online) of exactly the play's pair recorded in ``[entry, as_of]`` and no older than
``NOW_PRICE_MAX_AGE`` (``reporting_mark``). Without one the play's liquidation and open net
and the three dependent totals are ``None`` with a typed reason (``missing_quote``,
``stale_quote``, ``invalid_quote``) and ``stale`` true, never zero; free cash, open cost
basis and realized P&L stay available. Every play carries its stored ``fee_bps`` per leg
as an ASSUMED fee (the account tier is not verified) and, when its quote currency is not
the wallet's, ``fx_excluded``: the EUR amounts scale the pair's price moves onto a EUR
stake with no exchange rate, a hypothetical simulation and not EUR inventory.

Money crosses the bridge as decimal strings in cents (``"1000.00"``); the UI formats them
but never recomputes a balance. Sentences come from ``ui.paper_texts`` as plain text; stored
strings (asset symbols, pairs) are passed as data for the UI to insert as text.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Any

from radar_v08 import alerts
from radar_v08.adapters import paper_store
from radar_v08.domain import paper
from ui import paper_texts

ACTIVITY_WINDOW = timedelta(hours=24)
ACTIVITY_LIMIT = 60
PRICE_LINE_POINTS = 120
HISTORY_LIMIT = 100
SERIES_LIMIT = 500
LAST_RESULTS = 10
#: A recorded spot quote older than this is not shown as the play's "now" price, and is not
#: a valuation mark either (the reporting freshness rule).
NOW_PRICE_MAX_AGE = timedelta(minutes=10)
#: Typed status of a valuation mark: priced, or why the position cannot be priced.
MARKED = "marked"
MISSING_QUOTE = "missing_quote"
STALE_QUOTE = "stale_quote"
INVALID_QUOTE = "invalid_quote"
#: Typed status of the whole valuation.
NO_OPEN_POSITIONS = "no_open_positions"
ALL_MARKED = "all_marked"
UNAVAILABLE = "unavailable"
#: How open positions are valued, and where each fee rate comes from.
VALUATION_BASIS = "conservative_liquidation"
FEE_SOURCE = "ASSUMED"
VALUATION_IDENTITY = "total_equity = free_cash + liquidation_value = realized_balance + open_net_pnl"
#: Text bounds are widened by this much; the exact bounds are checked on parsed times.
_TEXT_MARGIN = timedelta(minutes=1)
_HUNDRED = Decimal(100)

AGENTS: tuple[dict[str, str], ...] = (
    {"id": "scout", "name": "Scout", "color": "#5B9DF6", "source": "radar_runs + events (RADAR_ALERT)"},
    {"id": "analyst", "name": "Analyst", "color": "#A78BFA", "source": "qwen_reviews"},
    {"id": "strategist", "name": "Strategist", "color": "#F5B94D", "source": "events.model_demand = SONNET (router)"},
    {"id": "boss", "name": "Boss", "color": "#F0616B", "source": "events.model_demand = FABLE (router)"},
    {"id": "treasurer", "name": "Treasurer", "color": "#34D399", "source": "paper_plays + paper_closes"},
)

REASON_NO_DATABASE = paper_texts.REASON_NO_DATABASE
REASON_NO_TABLES = paper_texts.REASON_NO_TABLES
REASON_NO_WALLET = paper_texts.REASON_NO_WALLET
REASON_NO_PLAYS = paper_texts.REASON_NO_PLAYS
REASON_UNREADABLE = paper_texts.REASON_UNREADABLE
REASON_DISABLED = paper_texts.REASON_DISABLED
#: Router decisions (``events.model_demand``) that bring the Strategist or the Boss in.
ROUTED_KINDS = {"SONNET": "sonnet", "FABLE": "fable"}


# --- helpers ------------------------------------------------------------------------------------


def money(value: Decimal) -> str:
    """Cents as a decimal string, e.g. ``"1000.00"`` / ``"-0.35"``; zero is never signed."""
    rounded = paper.cents(value)
    return "0.00" if rounded == 0 else str(rounded)


def _time(value: object) -> datetime | None:
    """A recorded time as an aware datetime; ``None`` when it cannot be placed in time."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def _ts(moment: datetime) -> str:
    return paper_store.utc_text(moment)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def levels(play: paper_store.StoredPlay) -> tuple[Decimal | None, Decimal | None]:
    """The play's frozen (stop, target) at the price precision of its own entry quote;
    ``(None, None)`` for a legacy play. The ATR behind them can carry many more digits than
    the pair is quoted with, so they are rounded for display only."""
    if play.stop is None or play.target is None:
        return None, None
    exponents = [quote.as_tuple().exponent for quote in (play.entry_bid, play.entry_ask)]
    places = max([0, *(-exponent for exponent in exponents if isinstance(exponent, int))])
    step = Decimal(1).scaleb(-places)
    return play.stop.quantize(step, ROUND_HALF_EVEN), play.target.quantize(step, ROUND_HALF_EVEN)


def _readonly_uri(path: str) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


def _sample(items: Sequence[Any], limit: int) -> list[Any]:
    """At most ``limit`` items spread evenly, always keeping the first and the last."""
    if len(items) <= limit:
        return list(items)
    last = len(items) - 1
    return [items[round(index * last / (limit - 1))] for index in range(limit)]


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


@dataclass(frozen=True, slots=True)
class Mark:
    """The reporting mark of one open position: ``status`` is ``MARKED`` with the quote, or
    the typed reason there is none."""

    status: str
    observed_at: datetime | None = None
    quote: paper.Quote | None = None


def reporting_mark(conn: sqlite3.Connection, pair: str, since: datetime, moment: datetime) -> Mark:
    """The latest valid quote of exactly ``pair`` observed in ``[since, moment]`` and at most
    ``NOW_PRICE_MAX_AGE`` before ``moment``, else why not.

    ``missing_quote``: no recorded row of the pair in the window; ``invalid_quote``: rows
    within the freshness bound exist but none is a valid quote (crossed, not finite, not
    positive, not online); ``stale_quote``: the pair's only rows are older than the bound. A
    row whose time cannot be placed is ignored. Another pair's quote never counts.
    """
    rows = conn.execute(
        "SELECT id, ts, bid, ask, status FROM spot_snapshots WHERE pair = ? AND ts >= ? AND ts <= ? ORDER BY ts, id",
        (pair, _ts(since - _TEXT_MARGIN), _ts(moment + _TEXT_MARGIN)),
    ).fetchall()
    seen = fresh_seen = False
    latest: tuple[datetime, int, paper.Quote] | None = None
    for row in rows:
        observed = _time(row[1])
        if observed is None or not since <= observed <= moment:
            continue
        seen = True
        fresh = moment - observed <= NOW_PRICE_MAX_AGE
        fresh_seen = fresh_seen or fresh
        quote = paper.validate_quote(row[2], row[3], row[4])
        if isinstance(quote, paper.QuoteProblem) or not fresh:
            continue
        if latest is None or (observed, int(row[0])) > (latest[0], latest[1]):
            latest = (observed, int(row[0]), quote)
    if latest is not None:
        return Mark(MARKED, latest[0], latest[2])
    if not seen:
        return Mark(MISSING_QUOTE)
    return Mark(INVALID_QUOTE if fresh_seen else STALE_QUOTE)


def mark_payload(mark: Mark, moment: datetime) -> dict[str, Any]:
    """A mark as bridge data: status, the visible stale flag, its plain-English reason and
    the quote it priced on (``None`` when unmarked)."""
    quote, observed = mark.quote, mark.observed_at
    return {
        "status": mark.status,
        "stale": mark.status != MARKED,
        "reason_text": paper_texts.MARK_REASONS.get(mark.status),
        "bid": str(quote.bid) if quote is not None else None,
        "ask": str(quote.ask) if quote is not None else None,
        "ts": _ts(observed) if observed is not None else None,
        "age_seconds": (moment - observed).total_seconds() if observed is not None else None,
    }


def fee_payload(fee_bps: Decimal) -> dict[str, Any]:
    """The fee stored on a play or position: per leg, ASSUMED, the account tier unverified."""
    return {
        "fee_bps": str(fee_bps),
        "fee_source": FEE_SOURCE,
        "account_tier_verified": False,
        "fee_text": paper_texts.fee_provenance_text(fee_bps),
    }


def fx_payload(quote: str, currency: str) -> dict[str, Any]:
    """Whether the EUR amounts of a play on a ``quote``-priced pair leave out an exchange rate."""
    excluded = quote != currency
    return {
        "fx_excluded": excluded,
        "fx_text": paper_texts.fx_excluded_text(quote, currency) if excluded else None,
    }


def freshness_payload() -> dict[str, Any]:
    """The reporting freshness rule a mark must meet."""
    return {
        "max_age_seconds": int(NOW_PRICE_MAX_AGE.total_seconds()),
        "text": paper_texts.freshness_text(int(NOW_PRICE_MAX_AGE.total_seconds() // 60)),
    }


@dataclass(frozen=True, slots=True)
class PlayValuation:
    """An open play valued at its mark; ``result`` is ``None`` when it has no mark."""

    mark: Mark
    result: paper.PlayResult | None


# --- the reader ---------------------------------------------------------------------------------


class PaperReader:
    """Builds the ``get_paper_state`` payload from ``path`` without ever writing to it."""

    def __init__(
        self,
        path: str,
        *,
        enabled: bool,
        qwen_enabled: bool,
        default_params: Mapping[str, object],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self.qwen_enabled = qwen_enabled
        self.default_params = dict(default_params)
        self._clock = clock or (lambda: datetime.now(UTC))

    def connect(self) -> sqlite3.Connection:
        """A read-only connection; raises ``sqlite3.OperationalError`` when the file is missing."""
        conn = sqlite3.connect(_readonly_uri(self.path), uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    def read(self, now: datetime | None = None) -> dict[str, Any]:
        moment = now or self._clock()
        payload = self._empty(moment)
        if not Path(self.path).is_file():
            payload["reason"] = REASON_NO_DATABASE
            return payload
        try:
            conn = self.connect()
        except sqlite3.Error:
            payload["reason"] = REASON_NO_DATABASE
            return payload
        try:
            try:
                tables = _tables(conn)
            except sqlite3.Error:  # not a database, locked, or a WAL file it may not open
                payload["reason"] = REASON_UNREADABLE
                return payload
            plays: tuple[paper_store.StoredPlay, ...] = ()
            closes: tuple[paper_store.StoredClose, ...] = ()
            try:
                if paper_store.schema_present(conn):
                    wallet = paper_store.read_wallet(conn)
                    plays = paper_store.read_plays(conn)
                    closes = paper_store.read_closes(conn)
                    if wallet is None:
                        payload["reason"] = REASON_NO_WALLET
                    else:
                        self._fill_game(conn, payload, wallet, plays, closes, moment)
                else:
                    payload["reason"] = REASON_NO_TABLES
            except (paper_store.PaperStoreError, paper.PaperInputError, sqlite3.Error, ArithmeticError):
                payload.update(self._empty(moment))
                payload["reason"] = REASON_UNREADABLE
                plays, closes = (), ()
            try:
                activity = self._activity(conn, tables, plays, closes, moment)
            except sqlite3.Error:
                activity = []
            payload["activity"] = activity
            payload["agents"] = self._agents(activity)
        finally:
            conn.close()
        if payload["available"] and not self.enabled:
            payload["reason"] = REASON_DISABLED
        return payload

    # -- payload parts ----------------------------------------------------------------------

    def _empty(self, moment: datetime) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": False,
            "reason": None,
            "pretend_money": True,
            "currency": "EUR",
            "params": self._params(None, ()),
            "params_source": "config",
            "wallet": None,
            "open_plays": [],
            "history": [],
            "history_total": 0,
            "agents": self._agents([]),
            "activity": [],
            "generated_at": _ts(moment),
        }

    def _params(self, wallet: paper_store.Wallet | None, plays: Sequence[paper_store.StoredPlay]) -> dict[str, Any]:
        defaults = self.default_params
        latest = plays[-1] if plays else None
        start = wallet.start_balance if wallet is not None else defaults.get("start_balance")
        stake = latest.stake if latest is not None else defaults.get("stake")
        fee = latest.fee_bps if latest is not None else defaults.get("fee_bps")
        # A legacy play's fixed hold is not the rule new plays open under.
        hold = latest.hold_minutes if latest is not None and latest.has_levels else defaults.get("hold_minutes")
        return {
            "start_balance": money(start) if isinstance(start, Decimal) else None,
            "stake": money(stake) if isinstance(stake, Decimal) else None,
            "max_open": _int(defaults.get("max_open")),
            "hold_minutes": _int(hold),
            "fee_bps": str(fee) if isinstance(fee, Decimal) else None,
        }

    def _fill_game(
        self,
        conn: sqlite3.Connection,
        payload: dict[str, Any],
        wallet: paper_store.Wallet,
        plays: tuple[paper_store.StoredPlay, ...],
        closes: tuple[paper_store.StoredClose, ...],
        moment: datetime,
    ) -> None:
        by_id = {play.play_id: play for play in plays}
        closed_ids = {close.play_id for close in closes}
        open_plays = [play for play in plays if play.play_id not in closed_ids]
        payload["available"] = True
        payload["reason"] = None if plays else REASON_NO_PLAYS
        payload["currency"] = wallet.currency
        payload["params"] = self._params(wallet, plays)
        payload["params_source"] = "recorded" if plays else "wallet"
        valuations = {play.play_id: self._value_play(conn, play, moment) for play in open_plays}
        payload["wallet"] = self._wallet(wallet, plays, open_plays, closes)
        payload["wallet"]["valuation"] = self._valuation(wallet, closes, open_plays, valuations, moment)
        payload["open_plays"] = [
            self._open_play(conn, play, moment, valuations[play.play_id], wallet.currency) for play in open_plays
        ]
        history = [
            self._history_item(close, by_id[close.play_id], wallet.currency)
            for close in reversed(closes)
            if close.play_id in by_id
        ]
        payload["history"] = history[:HISTORY_LIMIT]
        payload["history_total"] = len(history)

    def _wallet(
        self,
        wallet: paper_store.Wallet,
        plays: Sequence[paper_store.StoredPlay],
        open_plays: Sequence[paper_store.StoredPlay],
        closes: Sequence[paper_store.StoredClose],
    ) -> dict[str, Any]:
        start = wallet.start_balance
        balance = paper.balance(start, (close.net for close in closes))
        change = balance - start
        open_stakes = sum((play.stake for play in open_plays), Decimal(0))
        with localcontext(paper.PAPER_CONTEXT):
            change_pct = paper.cents(change / start * _HUNDRED)
        series = [{"ts": wallet.recorded_at, "balance": money(start)}]
        running = start
        points = []
        for close in closes:
            running += close.net
            points.append({"ts": close.closed_at, "balance": money(running)})
        series.extend(points[-(SERIES_LIMIT - 1):])
        outcomes = [close.outcome.value for close in closes]
        return {
            "balance": money(balance),
            "start_balance": money(start),
            "change": money(change),
            "change_pct": "0.00" if change_pct == 0 else str(change_pct),
            "open_stakes": money(open_stakes),
            "available_cash": money(balance - open_stakes),
            "wins": outcomes.count("WIN"),
            "losses": outcomes.count("LOSS"),
            "flats": outcomes.count("FLAT"),
            "fees_total": money(sum((close.fees for close in closes), Decimal(0))),
            "series": series,
            "last_results": outcomes[-LAST_RESULTS:],
            "cost_sentence": paper_texts.cost_sentence(
                [play.fee_bps for play in plays] or [self.default_params.get("fee_bps")],
                [close.spread_cost for close in closes],
            ),
        }

    @staticmethod
    def _value_play(conn: sqlite3.Connection, play: paper_store.StoredPlay, moment: datetime) -> PlayValuation:
        """``play`` closed now on paper at its reporting mark (``domain.paper.settle`` with the
        stored fee); no result without a fresh valid mark."""
        entry = paper.validate_quote(play.entry_bid, play.entry_ask)
        if isinstance(entry, paper.QuoteProblem):
            raise paper_store.PaperStoreError(paper_store.PaperStoreFailure.MALFORMED_ROW, "stored entry quote")
        mark = reporting_mark(conn, play.pair, play.entry_ts, moment)
        if mark.quote is None:
            return PlayValuation(mark, None)
        return PlayValuation(mark, paper.settle(play.direction, play.stake, play.fee_bps, entry, mark.quote))

    @staticmethod
    def _valuation(
        wallet: paper_store.Wallet,
        closes: Sequence[paper_store.StoredClose],
        open_plays: Sequence[paper_store.StoredPlay],
        valuations: Mapping[int, PlayValuation],
        moment: datetime,
    ) -> dict[str, Any]:
        """The wallet at ``moment``: ``total_equity = free_cash + liquidation_value =
        realized_balance + open_net_pnl`` (module docstring). The liquidation, open net and
        equity are ``None`` as soon as one open play has no mark; never a zero fill."""
        start = wallet.start_balance
        realized_balance = paper.balance(start, (close.net for close in closes))
        cost_basis = sum((play.stake for play in open_plays), Decimal(0))
        free_cash = realized_balance - cost_basis
        unmarked = [
            {"play_id": play.play_id, "pair": play.pair, "status": valuations[play.play_id].mark.status}
            for play in open_plays
            if valuations[play.play_id].result is None
        ]
        liquidation: Decimal | None = None
        open_net: Decimal | None = None
        equity: Decimal | None = None
        if not unmarked:
            results = [valuations[play.play_id].result for play in open_plays]
            open_net = sum((result.net for result in results if result is not None), Decimal(0))
            liquidation = cost_basis + open_net
            equity = free_cash + liquidation
        status = NO_OPEN_POSITIONS if not open_plays else UNAVAILABLE if unmarked else ALL_MARKED
        return {
            "currency": wallet.currency,
            "as_of": _ts(moment),
            "valuation_basis": VALUATION_BASIS,
            "valuation_basis_text": paper_texts.VALUATION_BASIS_TEXT,
            "identity": VALUATION_IDENTITY,
            "freshness": freshness_payload(),
            "mark_status": status,
            "stale": bool(unmarked),
            "unmarked": unmarked,
            "open_count": len(open_plays),
            "free_cash": money(free_cash),
            "open_cost_basis": money(cost_basis),
            "liquidation_value": None if liquidation is None else money(liquidation),
            "realized_balance": money(realized_balance),
            "realized_pnl": money(realized_balance - start),
            "open_net_pnl": None if open_net is None else money(open_net),
            "total_equity": None if equity is None else money(equity),
        }

    def _quotes_since(
        self, conn: sqlite3.Connection, pair: str, since: datetime, moment: datetime
    ) -> list[tuple[datetime, paper.Quote]]:
        """Valid recorded quotes of exactly ``pair`` observed in ``[since, moment]``, in time order."""
        rows = conn.execute(
            "SELECT id, ts, bid, ask, status FROM spot_snapshots WHERE pair = ? AND ts >= ? AND ts <= ? ORDER BY ts, id",
            (pair, _ts(since - _TEXT_MARGIN), _ts(moment + _TEXT_MARGIN)),
        ).fetchall()
        found: list[tuple[datetime, int, paper.Quote]] = []
        for row in rows:
            observed = _time(row["ts"])
            if observed is None or not since <= observed <= moment:
                continue
            quote = paper.validate_quote(row["bid"], row["ask"], row["status"])
            if isinstance(quote, paper.QuoteProblem):
                continue
            found.append((observed, int(row["id"]), quote))
        found.sort(key=lambda item: (item[0], item[1]))
        return [(observed, quote) for observed, _, quote in found]

    def _run_eligible(self, conn: sqlite3.Connection, run_id: str) -> int | None:
        try:
            row = conn.execute("SELECT assets_eligible FROM radar_runs WHERE run_id = ?", (run_id,)).fetchone()
        except sqlite3.Error:
            return None
        return _int(row["assets_eligible"]) if row is not None else None

    def _open_play(
        self,
        conn: sqlite3.Connection,
        play: paper_store.StoredPlay,
        moment: datetime,
        valuation: PlayValuation,
        currency: str,
    ) -> dict[str, Any]:
        entry = paper.validate_quote(play.entry_bid, play.entry_ask)
        if isinstance(entry, paper.QuoteProblem):
            raise paper_store.PaperStoreError(paper_store.PaperStoreFailure.MALFORMED_ROW, "stored entry quote")
        quotes = self._quotes_since(conn, play.pair, play.entry_ts, moment)
        current = quotes[-1] if quotes and moment - quotes[-1][0] <= NOW_PRICE_MAX_AGE else None
        gross_now: str | None = None
        if current is not None:
            with localcontext(paper.PAPER_CONTEXT):
                move = current[1].mid / entry.mid - 1
                gross = play.stake * (move if play.direction is paper.Direction.LONG else -move)
            gross_now = money(gross)
        pending = play.due_at <= moment
        scores = play.why.get("scores")
        stop, target = levels(play)
        return {
            "play_id": play.play_id,
            "asset": play.asset,
            "pair": play.pair,
            "quote": play.quote,
            "direction": play.direction.value,
            "direction_text": paper_texts.direction_text(play.direction),
            "opened_at": _ts(play.entry_ts),
            "due_at": _ts(play.due_at),
            "status": "PENDING_EXIT" if pending else "OPEN",
            **self._exit_plan(play),
            "stake": money(play.stake),
            "entry_bid": str(entry.bid),
            "entry_ask": str(entry.ask),
            "entry_mid": str(entry.mid),
            "now_bid": str(current[1].bid) if current else None,
            "now_ask": str(current[1].ask) if current else None,
            "now_ts": _ts(current[0]) if current else None,
            "gross_now": gross_now,
            "price_line": [
                {"ts": _ts(observed), "mid": str(quote.mid)} for observed, quote in _sample(quotes, PRICE_LINE_POINTS)
            ],
            "why": paper_texts.why_sentence(play.why, asset=play.asset, direction=play.direction),
            "steps": paper_texts.decision_steps(
                asset=play.asset,
                direction=play.direction,
                assets_eligible=self._run_eligible(conn, play.run_id),
                scores=scores if isinstance(scores, Mapping) else None,
                hold_minutes=play.hold_minutes,
                pending=pending,
                stop=stop,
                target=target,
            ),
            **self._play_valuation(play, valuation, moment),
            **fee_payload(play.fee_bps),
            **fx_payload(play.quote, currency),
        }

    @staticmethod
    def _play_valuation(play: paper_store.StoredPlay, valuation: PlayValuation, moment: datetime) -> dict[str, Any]:
        """The play closed now at its mark: cost basis (the stake), the parts of the net and
        the liquidation value; every priced field ``None`` without a mark."""
        result = valuation.result
        return {
            "cost_basis": money(play.stake),
            "mark": mark_payload(valuation.mark, moment),
            "liquidation_value": None if result is None else money(play.stake + result.net),
            "open_net_pnl": None if result is None else money(result.net),
            "open_gross_mid": None if result is None else money(result.gross_mid),
            "open_spread_cost": None if result is None else money(result.spread_cost),
            "open_fees": None if result is None else money(result.fees),
        }

    @staticmethod
    def _exit_plan(play: paper_store.StoredPlay) -> dict[str, Any]:
        """The EX-1 exit plan frozen on ``play``; every field ``None`` on a legacy play."""
        stop, target = levels(play)
        known = stop is not None and target is not None
        return {
            "exit_policy": play.exit_policy if known else None,
            "stop": str(stop) if known else None,
            "target": str(target) if known else None,
            "max_hold_minutes": play.hold_minutes if known else None,
            "exit_due_at": _ts(play.due_at) if known else None,
        }

    @staticmethod
    def _history_item(close: paper_store.StoredClose, play: paper_store.StoredPlay, currency: str) -> dict[str, Any]:
        stop, target = levels(play)
        return {
            **fee_payload(play.fee_bps),
            **fx_payload(play.quote, currency),
            "play_id": play.play_id,
            "asset": play.asset,
            "direction": play.direction.value,
            "opened_at": _ts(play.entry_ts),
            "closed_at": _ts(close.exit_ts),
            "delay_seconds": close.delay_seconds,
            "gross_mid": money(close.gross_mid),
            "spread_cost": money(close.spread_cost),
            "fees": money(close.fees),
            "net": money(close.net),
            "outcome": close.outcome.value,
            "exit_reason": close.exit_reason.value if close.exit_reason is not None else None,
            "exit_reason_text": paper_texts.exit_reason_label(close.exit_reason, hold_minutes=play.hold_minutes),
            "exit_source": close.exit_source,
            "record_lag_seconds": close.record_lag_seconds,
            "stop": str(stop) if stop is not None else None,
            "target": str(target) if target is not None else None,
            "sentence": paper_texts.result_sentence(
                asset=play.asset,
                direction=play.direction,
                outcome=close.outcome,
                net=close.net,
                gross_mid=close.gross_mid,
                spread_cost=close.spread_cost,
                fees=close.fees,
                entry_bid=play.entry_bid,
                entry_ask=play.entry_ask,
                exit_bid=close.exit_bid,
                exit_ask=close.exit_ask,
                delay_seconds=close.delay_seconds,
                exit_reason=close.exit_reason,
                stop=stop,
                target=target,
                hold_minutes=play.hold_minutes,
            ),
        }

    # -- activity ---------------------------------------------------------------------------

    def _activity(
        self,
        conn: sqlite3.Connection,
        tables: set[str],
        plays: Sequence[paper_store.StoredPlay],
        closes: Sequence[paper_store.StoredClose],
        moment: datetime,
    ) -> list[dict[str, Any]]:
        since = moment - ACTIVITY_WINDOW
        # Text bounds narrow the scan; the exact window is checked on parsed times. Each
        # source may return more rows than the output keeps, so a few unplaceable rows
        # cannot crowd out real ones.
        bound = (_ts(since - _TEXT_MARGIN), _ts(moment + _TEXT_MARGIN))
        scan = ACTIVITY_LIMIT * 2
        found: dict[str, dict[str, Any]] = {}

        def add(entry_id: str, ts: object, kind: str, asset: object, facts: Mapping[str, object]) -> None:
            moment_of = _time(ts)
            if moment_of is None or not since <= moment_of <= moment + _TEXT_MARGIN:
                return
            name = asset if isinstance(asset, str) and asset.strip() else None
            found[entry_id] = {
                "id": entry_id,
                "ts": _ts(moment_of),
                "agent": paper_texts.AGENT_FOR_KIND[kind],
                "kind": kind,
                "asset": name,
                "text": paper_texts.activity_line(kind, facts),
            }

        if "radar_runs" in tables:
            for row in conn.execute(
                "SELECT run_id, ts, assets_eligible, shortlist_count, warmup, api_failures FROM radar_runs "
                "WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT ?",
                (*bound, scan),
            ):
                add(f"cycle:{row['run_id']}", row["ts"], "cycle", None, dict(row))
        if "events" in tables:
            for row in conn.execute(
                "SELECT event_id, ts, type, asset, direction, model_demand FROM events WHERE type = 'RADAR_ALERT' "
                "AND substr(event_id, 1, 5) != 'MOCK-' AND ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT ?",
                (*bound, scan),
            ):
                if alerts.is_mock_alert(row):
                    continue
                add(f"alert:{row['event_id']}", row["ts"], "alert", row["asset"], dict(row))
                # The router's decision on that same real alert, recorded with it: the
                # Strategist (SONNET) or the Boss (FABLE) joins. IGNORE or anything else: nobody.
                decision = row["model_demand"]
                routed = ROUTED_KINDS.get(decision) if isinstance(decision, str) else None
                if routed is not None:
                    add(
                        f"route:{row['event_id']}",
                        row["ts"],
                        routed,
                        row["asset"],
                        {"decision": decision, "asset": row["asset"]},
                    )
        if "qwen_reviews" in tables:
            for row in conn.execute(
                "SELECT run_id, asset, batch_status, veto, confidence, recorded_at FROM qwen_reviews "
                "WHERE recorded_at >= ? AND recorded_at <= ? ORDER BY recorded_at DESC, id DESC LIMIT ?",
                (*bound, scan),
            ):
                entry_id = f"qwen:{row['run_id']}:{row['asset']}"
                if entry_id not in found:  # newest row of that (run, asset) wins
                    add(entry_id, row["recorded_at"], "qwen", row["asset"], dict(row))
        for play in plays:
            add(
                f"play_open:{play.play_id}",
                play.opened_at,
                "play_open",
                play.asset,
                {"asset": play.asset, "direction": play.direction, "stake": play.stake},
            )
        by_id = {play.play_id: play for play in plays}
        for close in closes:
            closed = by_id.get(close.play_id)
            add(
                f"play_close:{close.play_id}",
                close.closed_at,
                "play_close",
                closed.asset if closed else None,
                {
                    "asset": closed.asset if closed else None,
                    "outcome": close.outcome,
                    "net": close.net,
                    "exit_reason": close.exit_reason,
                    "hold_minutes": closed.hold_minutes if closed else None,
                },
            )
        newest = sorted(found.values(), key=lambda item: (item["ts"], item["id"]))[-ACTIVITY_LIMIT:]
        return newest

    def _agents(self, activity: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        last: dict[str, str] = {}
        for entry in activity:
            last[entry["agent"]] = max(last.get(entry["agent"], ""), entry["ts"])
        # ``enabled`` = the source that drives the agent is being recorded. The router
        # decision is stored on every alert whether or not the Claude API is on, so the
        # Strategist and the Boss can always be brought in by a real SONNET/FABLE decision.
        enabled = {
            "scout": True,
            "analyst": self.qwen_enabled,
            "strategist": True,
            "boss": True,
            "treasurer": self.enabled,
        }
        return [
            {**agent, "enabled": enabled[agent["id"]], "last_activity_ts": last.get(agent["id"])} for agent in AGENTS
        ]


def from_config() -> PaperReader:
    """The reader of the real radar database, with the parameters of ``radar_v08.config``."""
    from radar_v08 import config, paper_game

    return PaperReader(
        config.SQLITE_PATH,
        enabled=bool(config.RADAR_PAPER_ENABLED),
        qwen_enabled=config.RADAR_QWEN_MODE != "off",
        default_params={
            "start_balance": config.PAPER_START_BALANCE_EUR,
            "stake": config.PAPER_STAKE_EUR,
            "max_open": config.PAPER_MAX_OPEN,
            "hold_minutes": config.PAPER_HOLD_MINUTES,  # the EX-1 maximum hold (24 h)
            "fee_bps": paper_game.fee_bps_per_leg(),
        },
    )
