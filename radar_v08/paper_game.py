"""The paper game step: settle pretend plays, then open new ones on new radar events.

Pretend money only - nothing here sends an order or calls a private API. The stake, cap
and start balance come from ``config`` (``PAPER_*``) and the fee from
``UNCALIBRATED_FEES["spot_taker_bps"]`` at call time; the exit plan is the EX-1 initial
paper policy fixed in ``domain.paper`` (stop 2 x ATR14 of closed 5-minute bars, target 2R,
24 h at most), never configuration. ``adapters.paper_store`` freezes all of it on each play;
the sums live in ``domain.paper``.

Both entry points first make sure the tables and the wallet exist (no write when they do),
are idempotent - a repeated call with the same inputs writes nothing new - and return the
same count shape: ``{"opened", "closed", "pending", "skipped": {reason: n}}``. Settle
before opening within a cycle, so cash freed by a close is available to a new play.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from . import config
from .adapters import paper_store
from .adapters.paper_store import (
    OpenReport,
    PaperCandidate,
    PlayTerms,
    SettleReport,
    Wallet,
)
from .domain.paper import SkipReason

__all__ = [
    "PaperCandidate",
    "build_why",
    "default_terms",
    "fee_bps_per_leg",
    "open_for_events",
    "prepare",
    "settle_due",
]


def fee_bps_per_leg() -> Decimal:
    """The spot taker fee per leg, read from ``config.UNCALIBRATED_FEES`` (never copied)."""
    return Decimal(repr(float(config.UNCALIBRATED_FEES["spot_taker_bps"])))


def default_terms() -> PlayTerms:
    return PlayTerms(stake=config.PAPER_STAKE_EUR, fee_bps=fee_bps_per_leg(), max_open=config.PAPER_MAX_OPEN)


def prepare(conn: sqlite3.Connection, now: datetime) -> Wallet:
    """Create the paper tables and record the wallet if missing; writes nothing otherwise."""
    paper_store.ensure_schema(conn)
    return paper_store.ensure_wallet(
        conn, start_balance=config.PAPER_START_BALANCE_EUR, currency=config.PAPER_CURRENCY, now=now
    )


def _counts(*, opened: int = 0, closed: int = 0, pending: int = 0, skipped: Sequence[SkipReason] = ()) -> dict[str, Any]:
    by_reason = {reason.value: 0 for reason in SkipReason}
    for reason in skipped:
        by_reason[reason.value] += 1
    return {"opened": opened, "closed": closed, "pending": pending, "skipped": by_reason}


def settle_counts(report: SettleReport) -> dict[str, Any]:
    return _counts(closed=len(report.closed), pending=len(report.pending))


def open_counts(report: OpenReport) -> dict[str, Any]:
    return _counts(opened=len(report.opened), skipped=[skip.reason for skip in report.skipped])


def settle_due(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Close every play whose exit (stop, target or time; the due time for a legacy play)
    is observed in the recorded spot snapshots by ``now``; due plays without one stay pending."""
    prepare(conn, now)
    return settle_counts(paper_store.settle_due(conn, now=now))


def open_for_events(
    conn: sqlite3.Connection,
    candidates: Sequence[PaperCandidate],
    now: datetime,
    *,
    terms: PlayTerms | None = None,
) -> dict[str, Any]:
    """Open one play per new event candidate that passes admission; count the rest by reason."""
    prepare(conn, now)
    report = paper_store.open_candidates(conn, candidates, terms=terms or default_terms(), now=now)
    return open_counts(report)


def _json_safe(value: object) -> tuple[bool, object]:
    """``(keep, value)``: JSON scalars as they are, non-finite floats as ``None``, others dropped."""
    if value is None or isinstance(value, (str, bool, int)):
        return True, value
    if isinstance(value, float):
        return True, value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            keep, safe = _json_safe(item)
            if keep and isinstance(key, str):
                out[key] = safe
        return True, out
    if isinstance(value, (list, tuple)):
        return True, [safe for keep, safe in map(_json_safe, value) if keep]
    return False, None


def build_why(
    *,
    setup_type: str | None,
    direction: str | None,
    scores: Mapping[str, object],
    features: Mapping[str, object],
) -> dict[str, object]:
    """The recorded facts behind an event as a JSON object: nothing is added or estimated.

    A non-finite number becomes ``null`` (it was not a usable value); a value of a type
    JSON cannot hold is left out rather than converted.
    """
    _, safe_scores = _json_safe(scores)
    _, safe_features = _json_safe(features)
    return {
        "setup_type": setup_type,
        "direction": direction,
        "scores": safe_scores,
        "features": safe_features,
    }
