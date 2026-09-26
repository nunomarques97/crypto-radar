"""System clock adapter: UTC wall clock, monotonic clock and a bounded sleep.

The only place the worker reads real time. Wall time is timezone-aware UTC (for OC-1
deadlines, which the scheduler refuses to run backwards); elapsed time uses the
monotonic clock, which a wall-clock jump cannot move (OC-1 §1: "monotonic elapsed
deadlines remain independent"). Satisfies ``workflow.worker.WorkerClock`` and
``workflow.scheduler.Clock``.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

MAX_SLEEP_SECONDS = 3600.0


class SystemClock:
    def current(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            raise ValueError("sleep seconds must be a finite number")
        if not 0 <= seconds <= MAX_SLEEP_SECONDS:
            raise ValueError(f"sleep seconds must be in 0..{MAX_SLEEP_SECONDS:g}")
        time.sleep(seconds)
