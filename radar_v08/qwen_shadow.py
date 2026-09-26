"""Qwen shadow reviews off the heartbeat's critical path, and review rows.

In shadow mode the heartbeat hands the finalists' payloads to ``QwenShadow.submit`` and
routes at once as if Qwen were SKIPPED. One background thread runs the review; it never
touches SQLite. It only posts the finished batch to a thread-safe completion queue. Every
cycle, full or not, the heartbeat drains that queue after routing and writes the rows on
its own thread. A late batch is therefore written by whichever cycle drains it, but under
the run id, cycle timestamp, deterministic setup, scores and router decisions of the cycle
that submitted it.

Admission never queues: while the previous shadow thread is alive, or the process's single
inference slot is still held by a timed-out call, a new batch is dropped (the heartbeat
counts it). A batch still in flight when the process exits is lost.

State is per process (``default_shadow``); ``reset_default_shadow`` and the heartbeat's
``qwen_shadow`` argument are the seams that keep tests apart.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import qwen
from .adapters.qwen_review_store import QwenReviewRow
from .qwen import QwenBatchResult

logger = logging.getLogger("radar_v08.qwen_shadow")

# batch_status of a row whose review function raised instead of returning a result.
ERROR_STATUS = "ERROR"

ReviewFn = Callable[[list[dict[str, Any]]], QwenBatchResult]
ThreadFactory = Callable[..., threading.Thread]


@dataclass(frozen=True, slots=True)
class ReviewedFinalist:
    """What the origin cycle decided deterministically for one submitted finalist."""

    asset: str
    setup_type: str
    direction: str
    anomaly_score: float | None
    opportunity_score: float | None
    tradeability_score: float | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ReviewedFinalist:
        return cls(
            asset=payload["asset"],
            setup_type=payload["setup_type"],
            direction=payload["direction"],
            anomaly_score=payload["anomaly_score"],
            opportunity_score=payload["opportunity_score"],
            tradeability_score=payload["tradeability_score"],
        )


@dataclass
class QwenReviewBatch:
    """One cycle's Qwen batch. ``router_decisions`` is filled by the origin cycle after
    routing; ``result`` / ``error_code`` by whoever ran the review (inline: the heartbeat,
    shadow: the background thread, before the batch is posted to the completion queue)."""

    run_id: str
    cycle_ts: str
    mode: str
    finalists: tuple[ReviewedFinalist, ...]
    router_decisions: dict[str, str] = field(default_factory=dict)
    result: QwenBatchResult | None = None
    error_code: str | None = None

    @classmethod
    def from_payloads(
        cls, *, run_id: str, cycle_ts: str, mode: str, payloads: Sequence[dict[str, Any]]
    ) -> QwenReviewBatch:
        return cls(
            run_id=run_id,
            cycle_ts=cycle_ts,
            mode=mode,
            finalists=tuple(ReviewedFinalist.from_payload(payload) for payload in payloads),
        )

    def rows(self) -> list[QwenReviewRow]:
        """One row per submitted finalist, IGNORE-routed ones included."""
        result = self.result
        status = result.status if result is not None else ERROR_STATUS
        error_code = result.error_code if result is not None else (self.error_code or "no_result")
        rows = []
        for finalist in self.finalists:
            review = result.reviews.get(finalist.asset) if result is not None and status == "OK" else None
            rows.append(
                QwenReviewRow(
                    run_id=self.run_id,
                    cycle_ts=self.cycle_ts,
                    mode=self.mode,
                    asset=finalist.asset,
                    setup_type=finalist.setup_type,
                    direction=finalist.direction,
                    anomaly_score=finalist.anomaly_score,
                    opportunity_score=finalist.opportunity_score,
                    tradeability_score=finalist.tradeability_score,
                    router_decision=self.router_decisions.get(finalist.asset),
                    batch_status=status,
                    veto=review.veto if review is not None else None,
                    confidence=review.confidence if review is not None else None,
                    review_direction=review.direction if review is not None else None,
                    call_sonnet=review.call_sonnet if review is not None else None,
                    call_fable=review.call_fable if review is not None else None,
                    elapsed_ms=result.elapsed_ms if result is not None else None,
                    attempts=result.attempts if result is not None else 0,
                    error_code=error_code,
                )
            )
        return rows


class QwenShadow:
    """At most one shadow batch in flight per instance; finished batches wait in a queue."""

    def __init__(
        self,
        *,
        thread_factory: ThreadFactory = threading.Thread,
        inference_active: Callable[[], bool] = qwen.inference_active,
    ) -> None:
        self._thread_factory = thread_factory
        self._inference_active = inference_active
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._completed: queue.SimpleQueue[QwenReviewBatch] = queue.SimpleQueue()

    def _thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def pending(self) -> bool:
        """True while the last submitted shadow batch is still running."""
        with self._lock:
            return self._thread_alive()

    def in_flight(self) -> bool:
        """The admission test: a shadow thread alive, or the inference slot still held."""
        with self._lock:
            return self._thread_alive() or self._inference_active()

    def submit(self, batch: QwenReviewBatch, payloads: Sequence[dict[str, Any]], review_fn: ReviewFn) -> bool:
        """Start the review in the background; ``False`` (nothing started) while one is in flight.

        ``review_fn`` is captured here, so the thread keeps the function the cycle chose.
        """
        with self._lock:
            if self._thread_alive() or self._inference_active():
                return False
            thread = self._thread_factory(
                target=self._run, args=(batch, list(payloads), review_fn), name="qwen-shadow", daemon=True
            )
            thread.start()
            self._thread = thread
        return True

    def _run(self, batch: QwenReviewBatch, payloads: list[dict[str, Any]], review_fn: ReviewFn) -> None:
        try:
            batch.result = review_fn(payloads)
        except BaseException as exc:  # recorded as an ERROR row; never reaches the heartbeat
            batch.error_code = type(exc).__name__
            logger.warning("Qwen shadow batch of %s raised %s: %s", batch.run_id, type(exc).__name__, exc)
        finally:
            self._completed.put(batch)

    def drain(self) -> list[QwenReviewBatch]:
        """Every batch finished since the last drain, in completion order."""
        batches = []
        while True:
            try:
                batches.append(self._completed.get_nowait())
            except queue.Empty:
                return batches

    def join(self, timeout: float) -> bool:
        """Wait at most ``timeout`` seconds for the running batch; ``True`` when none is running."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not (thread is not None and thread.is_alive())


_default_lock = threading.Lock()
_default: QwenShadow | None = None


def default_shadow() -> QwenShadow:
    """The process's shadow submitter, created on first use."""
    global _default
    with _default_lock:
        if _default is None:
            _default = QwenShadow()
        return _default


def reset_default_shadow(shadow: QwenShadow | None = None) -> QwenShadow | None:
    """Replace the process's submitter (``None``: a fresh one on next use); returns the old one.

    A batch still running in the old submitter finishes into the old queue, which nobody
    drains any more.
    """
    global _default
    with _default_lock:
        previous, _default = _default, shadow
        return previous
