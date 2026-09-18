"""Per-(asset, model) cooldown, SQLite-backed (task section 11).

Default 4h, configurable. Exceptions that bypass an active cooldown: the
deterministic setup_type changed, direction changed, or opportunity_score
jumped by >= config.COOLDOWN_OPPORTUNITY_JUMP points - all spelled out in
config.py, none hidden inline.

When a cooldown starts (T031b): `record_send` is called only after the intended
transition was accepted - the Claude Bridge took the event (PENDING/DEFERRED ->
PROCESSING) and won the atomic claim + budget reservation for it. It is never
called for a deduplicated event, a claim refused for budget (the event is
DEFERRED), a claim lost to another connection, or anything the heartbeat does:
the heartbeat only reads the cooldown (`check_cooldown`) before recording demand.
"""

from __future__ import annotations

from datetime import datetime

from . import config
from .store import SnapshotStore


def check_cooldown(
    store: SnapshotStore,
    asset: str,
    model: str,
    now: datetime,
    setup_type: str,
    direction: str,
    opportunity_score: float | None,
) -> tuple[bool, str]:
    """Returns (allowed, reason)."""
    row = store.get_cooldown(asset, model)
    if row is None:
        return True, "no_prior_send"

    last_sent = datetime.fromisoformat(row["last_sent_ts"])
    elapsed_hours = (now - last_sent).total_seconds() / 3600.0
    if elapsed_hours >= config.COOLDOWN_HOURS:
        return True, "cooldown_expired"

    if row["setup_type"] != setup_type:
        return True, "setup_type_changed"
    if row["direction"] != direction:
        return True, "direction_changed"

    prev_opportunity = row["opportunity_score"] if row["opportunity_score"] is not None else 0.0
    if opportunity_score is not None and (opportunity_score - prev_opportunity) >= config.COOLDOWN_OPPORTUNITY_JUMP:
        return True, "opportunity_increase"

    return False, "in_cooldown"


def record_send(
    store: SnapshotStore,
    asset: str,
    model: str,
    now: datetime,
    setup_type: str,
    direction: str,
    opportunity_score: float | None,
) -> None:
    """Start (or restart) the cooldown for (asset, model) at `now`. See the module note for when."""
    store.set_cooldown(asset, model, now.isoformat(), setup_type, direction, opportunity_score)
