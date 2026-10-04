"""Read-only view of the pilot shadow for ``Api.get_pilot_state``.

The radar process owns every write to the ``pilot_*`` tables (``pilot_store.ensure_schema``,
``ensure_account``, ``open_candidates``, ``close_positions``, ``evaluate_locks``) and
``scripts/pilot_control.py`` the kill switch and the lock reviews. This module never calls
them: it opens its own SQLite connection with the URI ``mode=ro`` (plus ``PRAGMA
query_only``) for each read and reads only through the ``pilot_store`` readers, so opening
the Game tab cannot create a table or a row. The panel has no control: the kill switch and
the lock reviews stay in the CLI.

Everything shown comes from recorded rows: the envelope from ``pilot_account``, the equity
from ``pilot_store.account_state`` (assigned equity + realized nets + the conservative mark
of the open position at its latest recorded quote), the day-start and high-water marks, the
locks with the review that cleared each one, the latest kill switch row, the open position,
the decisions and their NO_TRADE reasons. The limits are the recorded envelope applied to
that equity by ``domain.risk.budget``, the same function the sizing uses. A value that was
not recorded is ``None``, never zero.

Reporting valuation
-----------------------------------

``valuation`` is a separate report of this pretend EUR PAPER account (not SHADOW_LIVE; no
real account value is read). The runtime ``account.equity`` above is kept as recorded for
compatibility: ``pilot_store.account_state`` falls back to the entry quote for the locks
and the sizing, and nothing here changes it or any runtime decision. The report instead
prices each open position only on a fresh reporting mark (``ui.paper_reader.reporting_mark``:
a valid online quote of exactly its pair observed in ``[entry, now]`` and no older than
``NOW_PRICE_MAX_AGE``):

* ``free_cash`` = assigned + realized nets - open cost basis (``risk.available_cash``);
  the cost basis already holds the entry fee (``risk.entry_cost_basis``).
* ``liquidation_value`` = quantity x mark bid - the exit fee rounded UP to the cent.
* ``open_net_pnl`` = liquidation - cost basis (``risk.unrealized_mark``), so the entry fee
  and the exit fee are each counted once.
* Identity: ``total_equity = free_cash + liquidation_value = realized_balance +
  open_net_pnl``, exact on the unrounded values.

Without a mark the position's liquidation and open net and the dependent totals are
``None`` with a typed reason and ``stale`` true, never zero.

Money crosses the bridge as decimal strings rounded half to even to the cent (display
only: the engine keeps exact values); prices, levels and lot-rounded quantities as the exact
recorded text; the four candidate quantities, which carry up to 50 digits, rounded down to
``CANDIDATE_PLACES`` decimals. Percentages are plain decimal strings in percent units.
Stored strings (assets, pairs, reasons, reviewers) are passed as data for the UI to insert as
text.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, localcontext
from pathlib import Path
from typing import Any

from radar_v08.adapters import paper_store, pilot_store
from radar_v08.domain import paper, risk
from ui import paper_reader, paper_texts

#: The kind of account the pilot shadow is: pretend money, never SHADOW_LIVE or real.
ACCOUNT_MODE = "PAPER"

REASON_NO_DATABASE = "no_database"
REASON_NO_TABLES = "no_tables"
REASON_NO_ACCOUNT = "no_account"
REASON_UNREADABLE = "unreadable"
REASON_DISABLED = "disabled"

#: Newest decisions listed under "recent decisions".
RECENT_LIMIT = 12
#: Newest locks listed (every active lock is always listed).
LOCK_LIMIT = 10
#: Decimals kept (rounded down) when a candidate quantity is shown.
CANDIDATE_PLACES = 8
_HUNDRED = Decimal(100)
#: Candidate quantities in the order the sizing takes their minimum (ties go to the earlier).
_CANDIDATES = (
    (risk.BindingConstraint.PER_ENTRY_LOSS, "qty_per_entry_loss", "per_entry_loss_cap"),
    (risk.BindingConstraint.AGGREGATE_LOSS, "qty_aggregate_loss", "aggregate_loss_remaining"),
    (risk.BindingConstraint.NOTIONAL, "qty_notional", "notional_remaining"),
    (risk.BindingConstraint.CASH, "qty_cash", "cash_room"),
)
#: A decision that reached the sizing recorded at least one of these.
_SIZING_COLUMNS = ("qty_per_entry_loss", "qty_aggregate_loss", "qty_notional", "qty_cash", "lot_quantity")


# --- helpers ------------------------------------------------------------------------------------


def money(value: Decimal | None) -> str | None:
    """Cents as a decimal string (``"240.00"``, ``"-0.35"``); zero is never signed."""
    if value is None:
        return None
    rounded = paper.cents(value)
    return "0.00" if rounded == 0 else str(rounded)


def exact(value: Decimal | None) -> str | None:
    """The exact recorded text of a price or quantity (no exponent)."""
    return None if value is None else pilot_store.decimal_text(value)


def percent(value: Decimal | None) -> str | None:
    """A recorded percentage in percent units, as recorded (``"0.25"``, ``"0.50"``, ``"10"``)."""
    return None if value is None else pilot_store.decimal_text(value)


def candidate_text(value: Decimal | None) -> str | None:
    """A candidate quantity rounded DOWN to ``CANDIDATE_PLACES`` decimals, for display."""
    if value is None:
        return None
    step = Decimal(1).scaleb(-CANDIDATE_PLACES)
    return pilot_store.decimal_text(value.quantize(step, rounding=ROUND_FLOOR, context=risk.QUANTITY_CONTEXT))


def _ts(moment: datetime) -> str:
    return paper_store.utc_text(moment)


def _readonly_uri(path: str) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


def _not_negative(value: Decimal) -> Decimal:
    return value if value > 0 else Decimal(0)


# --- the reader ---------------------------------------------------------------------------------


class PilotReader:
    """Builds the ``get_pilot_state`` payload from ``path`` without ever writing to it."""

    def __init__(self, path: str, *, enabled: bool, clock: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self.enabled = enabled
        self._clock = clock or (lambda: datetime.now(UTC))

    def connect(self) -> sqlite3.Connection:
        """A read-only connection; raises ``sqlite3.OperationalError`` when the file is missing."""
        conn = sqlite3.connect(_readonly_uri(self.path), uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def read(self, now: datetime | None = None) -> dict[str, Any]:
        moment = now or self._clock()
        payload = self._empty(moment)
        if not Path(self.path).is_file():
            return self._unavailable(payload, REASON_NO_DATABASE)
        try:
            conn = self.connect()
        except sqlite3.Error:
            return self._unavailable(payload, REASON_NO_DATABASE)
        try:
            if not pilot_store.schema_present(conn):
                return self._unavailable(payload, REASON_NO_TABLES)
            account = pilot_store.read_account(conn)
            if account is None:
                return self._unavailable(payload, REASON_NO_ACCOUNT)
            self._fill(conn, payload, account, moment)
        except (pilot_store.PilotStoreError, sqlite3.Error, ArithmeticError, risk.RiskInputError):
            return self._unavailable(self._empty(moment), REASON_UNREADABLE)
        finally:
            conn.close()
        if not self.enabled:
            payload["reason"] = REASON_DISABLED
            payload["reason_text"] = paper_texts.PILOT_REASONS[REASON_DISABLED]
        return payload

    # -- payload parts ----------------------------------------------------------------------

    def _empty(self, moment: datetime) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": False,
            "reason": None,
            "reason_text": None,
            "pretend_money": True,
            "direction": paper.Direction.LONG.value,
            "currency": None,
            "account": None,
            "account_mode": ACCOUNT_MODE,
            "account_text": paper_texts.PILOT_ACCOUNT_TEXT,
            "valuation": None,
            "limits": [],
            "kill_switch": None,
            "locks": {"active": [], "recent": [], "total": 0},
            "open_position": None,
            "open_count": 0,
            "last_sizing": None,
            "no_trade": {"total": 0, "counts": []},
            "decisions_total": 0,
            "opened_total": 0,
            "recent": [],
            "generated_at": _ts(moment),
        }

    @staticmethod
    def _unavailable(payload: dict[str, Any], reason: str) -> dict[str, Any]:
        payload["available"] = False
        payload["reason"] = reason
        payload["reason_text"] = paper_texts.PILOT_REASONS[reason]
        return payload

    def _fill(
        self, conn: sqlite3.Connection, payload: dict[str, Any], account: pilot_store.Account, moment: datetime
    ) -> None:
        envelope = account.envelope
        state = pilot_store.account_state(conn, now=moment)
        if state is None:  # the account row was read just before, on the same snapshot
            raise pilot_store.PilotStoreError(pilot_store.PilotStoreFailure.NO_ACCOUNT, "account vanished")
        day_start = pilot_store.day_start(conn, pilot_store.utc_day(moment))
        high_water = pilot_store.high_water(conn)
        decisions = pilot_store.read_decisions(conn)
        counts = pilot_store.no_trade_counts(conn)
        locks = pilot_store.read_locks(conn)
        switch = pilot_store.kill_switch(conn)
        payload["available"] = True
        payload["currency"] = envelope.currency
        payload["account"] = self._account(account, state, day_start, high_water)
        payload["valuation"] = self._valuation(conn, account, state, moment)
        payload["limits"] = self._limits(envelope, state, day_start, high_water)
        payload["kill_switch"] = {
            "engaged": switch.engaged,
            "reason": switch.reason,
            "actor": switch.actor,
            "recorded_at": switch.recorded_at,
        }
        active = [self._lock(lock) for lock in locks if lock.active]
        payload["locks"] = {
            "active": active,
            "recent": [self._lock(lock) for lock in reversed(locks[-LOCK_LIMIT:])],
            "total": len(locks),
        }
        positions = state.open_positions
        payload["open_position"] = self._position(positions[-1]) if positions else None
        payload["open_count"] = len(positions)
        sizing = next((d for d in reversed(decisions) if any(d.value(c) is not None for c in _SIZING_COLUMNS)), None)
        payload["last_sizing"] = None if sizing is None else self._sizing(sizing)
        payload["no_trade"] = {
            "total": sum(counts.values()),
            "counts": [{"reason": reason, "count": count} for reason, count in counts.items()],
        }
        payload["decisions_total"] = len(decisions)
        payload["opened_total"] = sum(1 for decision in decisions if decision.opened)
        payload["recent"] = [self._decision(decision) for decision in reversed(decisions[-RECENT_LIMIT:])]

    @staticmethod
    def _account(
        account: pilot_store.Account,
        state: pilot_store.AccountState,
        day_start: Decimal | None,
        high_water: Decimal | None,
    ) -> dict[str, Any]:
        assigned = account.envelope.equity
        with localcontext(risk.RISK_CONTEXT):
            change = state.equity - assigned
            day_change = None if day_start is None else state.equity - day_start
            drawdown_pct = (
                None
                if high_water is None or high_water <= 0
                else paper.cents(_not_negative(high_water - state.equity) / high_water * _HUNDRED)
            )
        return {
            "assigned": money(assigned),
            "equity": money(state.equity),
            "change": money(change),
            "realized": money(state.realized),
            "cash": money(state.cash),
            "day_start": money(day_start),
            "day_change": money(day_change),
            "high_water": money(high_water),
            "drawdown_pct": None if drawdown_pct is None else ("0.00" if drawdown_pct == 0 else str(drawdown_pct)),
            "recorded_at": account.recorded_at,
            "envelope_policy_id": risk.ENVELOPE_POLICY_ID,
            "envelope_sha256": account.envelope_sha256,
        }

    @staticmethod
    def _valuation(
        conn: sqlite3.Connection, account: pilot_store.Account, state: pilot_store.AccountState, moment: datetime
    ) -> dict[str, Any]:
        """The reporting valuation (module docstring); the runtime ``state`` supplies only the
        recorded realized nets, cash and open positions, never its entry-quote fallback mark."""
        assigned = account.envelope.equity
        positions = state.open_positions
        rows: list[dict[str, Any]] = []
        liquidations: list[Decimal] = []
        marks: list[Decimal] = []
        for position in positions:
            mark = paper_reader.reporting_mark(conn, position.pair, position.entry_ts, moment)
            liquidation: Decimal | None = None
            open_net: Decimal | None = None
            exit_fee: Decimal | None = None
            if mark.quote is not None:
                open_net = risk.unrealized_mark(position.quantity, mark.quote.bid, position.cost_basis, position.fee_bps)
                with localcontext(risk.RISK_CONTEXT):
                    proceeds = position.quantity * mark.quote.bid
                    exit_fee = risk.cents_up(proceeds * position.fee_bps / paper.BPS)
                    liquidation = proceeds - exit_fee
                liquidations.append(liquidation)
                marks.append(open_net)
            rows.append({
                "position_id": position.position_id,
                "pair": position.pair,
                "quantity": exact(position.quantity),
                "cost_basis": money(position.cost_basis),
                "mark": paper_reader.mark_payload(mark, moment),
                "exit_fee": money(exit_fee),
                "liquidation_value": money(liquidation),
                "open_net_pnl": money(open_net),
                **paper_reader.fee_payload(position.fee_bps),
            })
        unmarked = [
            {"position_id": row["position_id"], "pair": row["pair"], "status": row["mark"]["status"]}
            for row in rows
            if row["mark"]["stale"]
        ]
        with localcontext(risk.RISK_CONTEXT):
            cost_basis = sum((position.cost_basis for position in positions), Decimal(0))
            realized_balance = assigned + state.realized
            free_cash = realized_balance - cost_basis
            liquidation_total = None if unmarked else sum(liquidations, Decimal(0))
            open_net_total = None if unmarked else sum(marks, Decimal(0))
            equity = None if liquidation_total is None else free_cash + liquidation_total
        if not positions:
            status = paper_reader.NO_OPEN_POSITIONS
        else:
            status = paper_reader.UNAVAILABLE if unmarked else paper_reader.ALL_MARKED
        return {
            "account_mode": ACCOUNT_MODE,
            "account_text": paper_texts.PILOT_ACCOUNT_TEXT,
            "currency": account.envelope.currency,
            "as_of": _ts(moment),
            "valuation_basis": paper_reader.VALUATION_BASIS,
            "valuation_basis_text": paper_texts.VALUATION_BASIS_TEXT,
            "identity": paper_reader.VALUATION_IDENTITY,
            "freshness": paper_reader.freshness_payload(),
            "mark_status": status,
            "stale": bool(unmarked),
            "unmarked": unmarked,
            "open_count": len(positions),
            "assigned": money(assigned),
            "free_cash": money(free_cash),
            "open_cost_basis": money(cost_basis),
            "liquidation_value": money(liquidation_total),
            "realized_balance": money(realized_balance),
            "realized_pnl": money(state.realized),
            "open_net_pnl": money(open_net_total),
            "total_equity": money(equity),
            "positions": rows,
            "exact": {
                "free_cash": exact(free_cash),
                "open_cost_basis": exact(cost_basis),
                "liquidation_value": exact(liquidation_total),
                "open_net_pnl": exact(open_net_total),
                "total_equity": exact(equity),
            },
        }

    @staticmethod
    def _limits(
        envelope: risk.Envelope,
        state: pilot_store.AccountState,
        day_start: Decimal | None,
        high_water: Decimal | None,
    ) -> list[dict[str, Any]]:
        """Each EX-1 limit: its percentage, its amount at the current equity, what is in use
        and what is left. ``used``/``left`` are ``None`` where they do not apply."""
        if state.equity <= 0:
            room = None
        else:
            room = risk.budget(envelope, state.equity, state.cash, state.open_planned_loss, state.open_notional)
        per_entry_cap = None if room is None else room.per_entry_loss_cap
        aggregate_cap = None if room is None else room.aggregate_loss_cap
        aggregate_left = None if room is None else room.aggregate_loss_remaining
        notional_cap = None if room is None else room.notional_cap
        notional_left = None if room is None else room.notional_remaining
        cash_buffer = None if room is None else room.cash_buffer
        cash_room = None if room is None else room.cash_room
        open_cost = sum((position.cost_basis for position in state.open_positions), Decimal(0))
        largest_open_loss = max((position.planned_loss for position in state.open_positions), default=Decimal(0))

        def limit(ident: str, pct: Decimal | None, amount: Decimal | None, used: Decimal | None,
                  left: Decimal | None) -> dict[str, Any]:
            return {"id": ident, "pct": percent(pct), "amount": money(amount), "used": money(used), "left": money(left)}

        with localcontext(risk.RISK_CONTEXT):
            daily_amount = None if day_start is None else day_start * envelope.daily_loss_pct / _HUNDRED
            daily_used = None if day_start is None else _not_negative(day_start - state.equity)
            drawdown_amount = None if high_water is None else high_water * envelope.drawdown_pct / _HUNDRED
            drawdown_used = None if high_water is None else _not_negative(high_water - state.equity)
            rows = [
                limit("per_entry_loss", envelope.per_entry_loss_pct, per_entry_cap,
                      largest_open_loss if state.open_positions else None, None),
                limit("aggregate_loss", envelope.aggregate_loss_pct, aggregate_cap,
                      state.open_planned_loss, aggregate_left),
                limit("gross_notional", envelope.gross_notional_pct, notional_cap,
                      state.open_notional, notional_left),
                limit("cash_buffer", envelope.cash_buffer_pct, cash_buffer, open_cost,
                      cash_room),
                limit("daily_loss", envelope.daily_loss_pct, daily_amount, daily_used,
                      None if daily_amount is None or daily_used is None else _not_negative(daily_amount - daily_used)),
                limit("drawdown", envelope.drawdown_pct, drawdown_amount, drawdown_used,
                      None if drawdown_amount is None or drawdown_used is None
                      else _not_negative(drawdown_amount - drawdown_used)),
            ]
        rows[0]["cap"] = money(envelope.per_entry_abs_cap)
        rows.append({"id": "positions", "max": envelope.max_positions, "open": len(state.open_positions)})
        rows.append({"id": "leverage", "max": envelope.leverage})
        return rows

    @staticmethod
    def _lock(lock: pilot_store.StoredLock) -> dict[str, Any]:
        review = lock.review
        return {
            "lock_id": lock.lock_id,
            "kind": lock.kind.value,
            "active": lock.active,
            "equity": money(lock.equity),
            "reference": money(lock.reference),
            "limit_pct": percent(lock.limit_pct),
            "utc_day": lock.utc_day,
            "evaluated_on": lock.evaluated_on.value,
            "tripped_at": lock.tripped_at,
            "review": None
            if review is None
            else {
                "review_id": review.review_id,
                "reviewer": review.reviewer,
                "cause": review.cause,
                "rebase_equity": money(review.rebase_equity),
                "reviewed_at": review.reviewed_at,
            },
        }

    @staticmethod
    def _position(position: pilot_store.StoredPosition) -> dict[str, Any]:
        return {
            "position_id": position.position_id,
            "event_id": position.event_id,
            "asset": position.asset,
            "pair": position.pair,
            "quote": position.quote,
            "direction": position.direction.value,
            "quantity": exact(position.quantity),
            "entry": exact(position.entry_ask),
            "entry_bid": exact(position.entry_bid),
            "stop": exact(position.stop),
            "target": exact(position.target),
            "stress_exit": exact(position.stress_exit_price),
            "notional": money(position.notional),
            "cost_basis": money(position.cost_basis),
            "planned_loss": money(position.planned_loss),
            "fee_bps": percent(position.fee_bps),
            "fee_source": paper_reader.FEE_SOURCE,
            "account_tier_verified": False,
            "fee_text": paper_texts.fee_provenance_text(position.fee_bps),
            "exit_policy": position.exit_policy,
            "opened_at": _ts(position.entry_ts),
            "due_at": _ts(position.due_at),
        }

    @staticmethod
    def _checked_quantity(decision: pilot_store.StoredDecision) -> Decimal | None:
        """The last quantity the minimum checks saw: the final one, or the lot-rounded one
        reduced by one lot per downward pass (``domain.risk.plan_long_entry``)."""
        if decision.value("quantity") is not None:
            return decision.value("quantity")
        lot_quantity = decision.value("lot_quantity")
        if lot_quantity is None or decision.lot_decimals is None:
            return None
        with localcontext(risk.RISK_CONTEXT):
            return lot_quantity - decision.passes * risk.lot_step(decision.lot_decimals)

    def _sizing(self, decision: pilot_store.StoredDecision) -> dict[str, Any]:
        value = decision.value
        ask = value("ask")
        ordermin = value("ordermin")
        costmin = value("costmin")
        checked = self._checked_quantity(decision)
        order_ok: bool | None = None
        cost_ok: bool | None = None
        cost: Decimal | None = None
        if checked is not None and ordermin is not None:
            order_ok = checked > 0 and checked >= ordermin
            if order_ok and ask is not None and costmin is not None:
                with localcontext(risk.RISK_CONTEXT):
                    cost = checked * ask
                cost_ok = cost >= costmin
        return {
            "decision_id": decision.decision_id,
            "event_id": decision.event_id,
            "asset": decision.asset,
            "pair": decision.pair,
            "quote": decision.quote,
            "outcome": decision.outcome,
            "reason": None if decision.reason is None else decision.reason.value,
            "detail": decision.detail,
            "decided_at": decision.decided_at,
            "equity": money(value("equity")),
            "ask": exact(ask),
            "stop": exact(value("stop_price")),
            "stress_exit": exact(value("stress_exit_price")),
            "loss_per_unit": candidate_text(value("loss_per_unit")),
            "binding": None if decision.binding is None else decision.binding.value,
            "candidates": [
                {
                    "id": binding.value,
                    "quantity": candidate_text(value(column)),
                    "budget": money(value(budget_column)),
                    "binding": decision.binding is binding,
                }
                for binding, column, budget_column in _CANDIDATES
            ],
            "lot_decimals": decision.lot_decimals,
            "lot_quantity": exact(value("lot_quantity")),
            "passes": decision.passes,
            "quantity": exact(value("quantity")),
            "notional": money(value("notional")),
            "entry_fee": money(decision.entry_fee),
            "exit_fee": money(decision.exit_fee),
            "planned_loss": money(value("planned_loss")),
            "minimums": {
                "checked_quantity": exact(checked),
                "ordermin": exact(ordermin),
                "order_ok": order_ok,
                "cost": money(cost),
                "costmin": exact(costmin),
                "cost_ok": cost_ok,
            },
        }

    @staticmethod
    def _decision(decision: pilot_store.StoredDecision) -> dict[str, Any]:
        return {
            "decision_id": decision.decision_id,
            "event_id": decision.event_id,
            "asset": decision.asset,
            "pair": decision.pair,
            "quote": decision.quote,
            "outcome": decision.outcome,
            "reason": None if decision.reason is None else decision.reason.value,
            "detail": decision.detail,
            "decided_at": decision.decided_at,
            "quantity": exact(decision.value("quantity")),
        }


def from_config() -> PilotReader:
    """The reader of the real radar database, switched on or off like the radar's pilot step."""
    from radar_v08 import config

    return PilotReader(config.SQLITE_PATH, enabled=bool(config.RADAR_PILOT_ENABLED))
