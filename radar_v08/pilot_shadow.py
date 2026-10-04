"""The pilot shadow step: a second pretend account, LONG only, sized by the Risk Engine.

Phase 2 of the path to real trading. Pretend money only - nothing here sends an order,
reads an exchange account, calls a private API or a model. The pilot shadow evaluates the
same candidates as the paper game next to it and never touches the game's tables: its
envelope (240.00 EUR by default, the EX-1 limits of ``domain.risk``), sizing, locks and
kill switch live in ``adapters.pilot_store``; the exits are the phase 1 EX-1 exits of
``domain.paper`` (stop 2 x ATR, target 2R, 24 h), with the stop and target rounded down
to the pair tick.

Every entry point first makes sure the tables and the account exist (no write when they
do), is idempotent - a repeated call with the same inputs writes nothing new - and
returns plain counts for the run record. Settle before opening within a cycle, so a close
and the lock evaluation after it are seen by the entries.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from . import paper_game
from .adapters import pilot_store
from .adapters.paper_store import ObservedQuote, PaperCandidate
from .adapters.pilot_store import (
    Account,
    CloseReport,
    KillSwitch,
    LockEvaluation,
    OpenReport,
    PilotCandidate,
    StoredReview,
)
from .domain.risk import Envelope, NoTradeReason

__all__ = [
    "PILOT_ASSIGNED_EQUITY_EUR",
    "PILOT_CURRENCY",
    "PilotCandidate",
    "candidate_from_paper",
    "close_positions",
    "default_envelope",
    "engage",
    "evaluate_locks",
    "open_for_candidates",
    "prepare",
    "release",
    "review_lock",
    "settle",
]

#: The pilot's envelope currency: a candidate quoted in anything else is refused, no FX.
PILOT_CURRENCY = "EUR"
#: The assigned equity of the pilot shadow (the 240 EUR real pilot decided in phase 1).
PILOT_ASSIGNED_EQUITY_EUR = Decimal("240.00")


def default_envelope() -> Envelope:
    """240.00 EUR at the EX-1 limits (``domain.risk`` defaults)."""
    return Envelope(PILOT_ASSIGNED_EQUITY_EUR, PILOT_CURRENCY)


def prepare(conn: sqlite3.Connection, now: datetime, *, envelope: Envelope | None = None) -> Account:
    """Create the pilot tables and record the account if missing; writes nothing otherwise."""
    pilot_store.ensure_schema(conn)
    return pilot_store.ensure_account(conn, envelope=envelope or default_envelope(), now=now)


def candidate_from_paper(candidate: PaperCandidate, pair_entry: object) -> PilotCandidate:
    """The game's candidate as the pilot sees it, with the entry pair's raw AssetPairs entry."""
    return PilotCandidate(
        event_id=candidate.event_id,
        run_id=candidate.run_id,
        asset=candidate.asset,
        pair=candidate.pair,
        quote=candidate.quote,
        direction=candidate.direction,
        bid=candidate.bid,
        ask=candidate.ask,
        snapshot_ts=candidate.snapshot_ts,
        status=candidate.status,
        atr=candidate.atr,
        atr_pair=candidate.atr_pair,
        pair_entry=pair_entry,
    )


def _lock_counts(locks: LockEvaluation) -> dict[str, Any]:
    return {
        "equity": pilot_store.decimal_text(locks.equity),
        "locks_tripped": [lock.kind.value for lock in locks.tripped],
        "locks_active": [kind.value for kind in locks.active_kinds],
    }


def open_counts(report: OpenReport) -> dict[str, Any]:
    by_reason = {reason.value: 0 for reason in NoTradeReason}
    opened = 0
    for decision in report.decisions:
        if decision.reason is None:
            opened += 1
        else:
            by_reason[decision.reason.value] += 1
    return {
        "evaluated": len(report.decisions),
        "opened": opened,
        "already_recorded": len(report.already_recorded),
        "no_trade": by_reason,
        **_lock_counts(report.locks),
    }


def close_counts(report: CloseReport) -> dict[str, Any]:
    return {"closed": len(report.closed), "pending": len(report.pending), **_lock_counts(report.locks)}


def open_for_candidates(
    conn: sqlite3.Connection,
    candidates: Sequence[PilotCandidate],
    now: datetime,
    *,
    envelope: Envelope | None = None,
    fee_bps: Decimal | None = None,
    extra_quotes: Sequence[ObservedQuote] = (),
) -> dict[str, Any]:
    """Evaluate each new candidate once: open the admitted ones, record every refusal."""
    offered = envelope or default_envelope()
    prepare(conn, now, envelope=offered)
    report = pilot_store.open_candidates(
        conn,
        candidates,
        envelope=offered,
        fee_bps=paper_game.fee_bps_per_leg() if fee_bps is None else fee_bps,
        now=now,
        extra_quotes=extra_quotes,
    )
    return open_counts(report)


def close_positions(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    extra_quotes: Sequence[ObservedQuote] = (),
    position_ids: Collection[int] | None = None,
    envelope: Envelope | None = None,
) -> dict[str, Any]:
    """Close each open position whose stop, target or time is observed by ``now`` (spot
    snapshots plus ``extra_quotes``), then evaluate the locks."""
    prepare(conn, now, envelope=envelope)
    report = pilot_store.close_positions(conn, now=now, extra_quotes=extra_quotes, position_ids=position_ids)
    return close_counts(report)


def settle(conn: sqlite3.Connection, now: datetime, *, envelope: Envelope | None = None) -> dict[str, Any]:
    """``close_positions`` on the recorded spot snapshots only."""
    return close_positions(conn, now, envelope=envelope)


def evaluate_locks(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    extra_quotes: Sequence[ObservedQuote] = (),
    envelope: Envelope | None = None,
) -> dict[str, Any]:
    """Mark the account at ``now`` and trip any lock due; nothing is ever cleared here."""
    prepare(conn, now, envelope=envelope)
    return _lock_counts(pilot_store.evaluate_locks(conn, now=now, extra_quotes=extra_quotes))


def engage(conn: sqlite3.Connection, reason: str, actor: str, now: datetime) -> KillSwitch:
    """Engage the kill switch: every new candidate is ``kill_switch_engaged``; open
    positions still close by their exits."""
    pilot_store.ensure_schema(conn)
    return pilot_store.engage_kill_switch(conn, reason=reason, actor=actor, now=now)


def release(conn: sqlite3.Connection, reason: str, actor: str, now: datetime) -> KillSwitch:
    """Release the kill switch: new candidates are evaluated again."""
    pilot_store.ensure_schema(conn)
    return pilot_store.release_kill_switch(conn, reason=reason, actor=actor, now=now)


def review_lock(
    conn: sqlite3.Connection,
    lock_id: int,
    *,
    reviewer: str,
    cause: str,
    now: datetime,
    extra_quotes: Sequence[ObservedQuote] = (),
) -> StoredReview:
    """Clear one lock with an explicit review that rebases its reference to the equity now."""
    return pilot_store.review_lock(
        conn, lock_id=lock_id, reviewer=reviewer, cause=cause, now=now, extra_quotes=extra_quotes
    )
