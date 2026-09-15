"""Hourly/daily model budgets, SQLite-backed (task section 12).

Initial values are declared UNCALIBRATED in config.py. If a budget is
exhausted the event is never lost - the caller marks it DEFERRED and still
writes it to the event queue (task section 12/13), it just doesn't consume
that model's quota.
"""

from __future__ import annotations

from datetime import datetime

from . import config
from .store import SnapshotStore


def _hour_window_start(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:00:00")


def _day_window_start(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def budget_status(store: SnapshotStore, model: str, now: datetime) -> dict[str, int]:
    limits = config.MODEL_BUDGETS.get(model, {"hourly": 0, "daily": 0})
    hourly_used = store.get_budget_count(model, "hour", _hour_window_start(now))
    daily_used = store.get_budget_count(model, "day", _day_window_start(now))
    return {
        "hourly_used": hourly_used,
        "hourly_limit": limits["hourly"],
        "daily_used": daily_used,
        "daily_limit": limits["daily"],
    }


def try_consume_budget(store: SnapshotStore, model: str, now: datetime) -> tuple[bool, dict[str, int]]:
    """Atomically (within this process) checks both hourly and daily budgets
    and only increments if BOTH have room. Returns (allowed, status)."""
    status = budget_status(store, model, now)
    if status["hourly_used"] >= status["hourly_limit"] or status["daily_used"] >= status["daily_limit"]:
        return False, status

    store.increment_budget(model, "hour", _hour_window_start(now))
    store.increment_budget(model, "day", _day_window_start(now))
    status["hourly_used"] += 1
    status["daily_used"] += 1
    return True, status
