"""Hourly/daily model budgets (task section 12), charged once per genuine call (T031b).

The only way a budget unit is spent is the atomic claim + reservation of T031a
(`SnapshotStore.claim_invocation`, one `BEGIN IMMEDIATE` transaction in
`radar_v08/adapters/invocation_store.py`), made by the Claude Bridge when it takes
an event for a model call. The heartbeat never charges a budget: it only records
the router's demand as an event (created or deduplicated). A claim refused for
budget writes no reservation; the bridge then marks the event DEFERRED, so the
event is never lost (task section 12/13).

This module maps `config.MODEL_BUDGETS` (UNCALIBRATED initial values) onto the
domain `ModelBudget` and reads the reserved counters back for display. The old
check-then-increment `try_consume_budget` on the legacy `model_budget_usage`
table was removed in T031b: it was not atomic across connections and made the
heartbeat and the bridge each charge the same opportunity (TAKEOVER_AUDIT P1).
The legacy table is kept (never dropped, D19) but is no longer written.
"""

from __future__ import annotations

from datetime import datetime

from . import config
from .domain.invocation import ModelBudget
from .store import SnapshotStore


def model_budget(model: str) -> ModelBudget:
    """The configured limits for `model`; an unknown model gets 0/0 (every claim refused)."""
    limits = config.MODEL_BUDGETS.get(model, {"hourly": 0, "daily": 0})
    return ModelBudget(model=model, hourly_limit=int(limits["hourly"]), daily_limit=int(limits["daily"]))


def budget_status(store: SnapshotStore, model: str, now: datetime) -> dict[str, int]:
    """Reserved units vs limits in the UTC hour and day windows containing `now`.

    Reads the T031a reservation counters, the ones a claim actually charges.
    """
    usage = store.invocation_budget_usage(model_budget(model), now=now)
    return {
        "hourly_used": usage.hourly_reserved,
        "hourly_limit": usage.hourly_limit,
        "daily_used": usage.daily_reserved,
        "daily_limit": usage.daily_limit,
    }
