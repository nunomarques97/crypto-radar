"""Kraken-referenced corrected clock.

The local Windows clock can drift or step by close to a second, which on its
own pushes OC-1's clock bound (<= 500 ms) over the limit and suspends every
analysis. Instead of only bounding that offset, the radar estimates it
NTP-style from each Kraken Futures tickers round trip and reads a corrected
clock: local wall time + the offset of the best recent sample.

One round trip gives one sample:

* RTT from the monotonic clock at send and at receipt;
* offset = serverTime - the local wall time at the round trip's midpoint;
* uncertainty = RTT/2 + the 1 ms serverTime resolution + aging, where aging
  is `DRIFT_ALLOWANCE` x the monotonic age plus any wall-clock movement
  against the monotonic clock since the sample.

The corrected clock uses the valid sample of the window (the last
`WINDOW_SAMPLES`, none older than `MAX_SAMPLE_AGE`) with the lowest current
uncertainty. OC-1 still decides on that uncertainty with the same 500 ms.

Fail-closed rules:

* a cycle whose own measurement failed or had no serverTime stays UNKNOWN,
  whatever older samples exist (they never stand in for the current one),
  and its stamps are on the raw local clock;
* an offset beyond `MAX_PLAUSIBLE_OFFSET`, or one that contradicts the
  window's valid samples, is rejected and the clock does not PASS - a venue
  time must never move the radar's time arbitrarily;
* a wall-clock step against the monotonic clock beyond `JUMP_TOLERANCE`
  discards every sample taken before it. A backward step reports
  `clock_backward_jump` until a new valid measurement exists, so a jump
  suspends the radar until the next measurement, never permanently.

The estimator is stateful and thread-safe (L2/L3 read the clock from worker
threads). The heartbeat keeps one per process (`default_clock_reference`),
so the window persists across loop cycles.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .domain.integrity import ClockSample

SOURCE = "kraken-futures tickers serverTime"
# Last N accepted round trips considered by the min-uncertainty filter.
WINDOW_SAMPLES = 8
# A sample older than this (monotonic age) is never used.
MAX_SAMPLE_AGE = timedelta(seconds=300)
# Allowed rate error of the local monotonic clock against true time (100 ppm).
DRIFT_ALLOWANCE = 100e-6
# Kraken Futures' serverTime carries milliseconds.
SERVER_TIME_RESOLUTION = timedelta(milliseconds=1)
# A measured |offset| above this is not a local clock error the radar corrects:
# the reference is rejected (fail closed) instead of applied.
MAX_PLAUSIBLE_OFFSET = timedelta(seconds=60)
# A wall-clock move against the monotonic clock above this is a discontinuity
# (clock step); below it, the move is only added to the samples' aging.
JUMP_TOLERANCE = timedelta(milliseconds=100)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS = timedelta(milliseconds=1)

WallClock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


def _ms(value: timedelta) -> float:
    return round(value / _MS, 3)


@dataclass(frozen=True, slots=True)
class _Reading:
    wall: datetime
    mono: float


@dataclass(frozen=True, slots=True)
class _Sample:
    offset: timedelta
    rtt: timedelta
    wall: datetime  # local wall time at receipt
    mono: float  # monotonic time at receipt

    def age(self, reading: _Reading) -> timedelta:
        return timedelta(seconds=reading.mono - self.mono)

    def uncertainty(self, reading: _Reading) -> timedelta:
        age = self.age(reading)
        divergence = abs((reading.wall - self.wall) - age)
        return self.rtt / 2 + SERVER_TIME_RESOLUTION + age * DRIFT_ALLOWANCE + divergence


class ClockProbe:
    """One Kraken Futures tickers round trip in flight. `receipt` is the clock
    handed to the fetch: it records the raw receipt reading and returns the
    raw wall time."""

    def __init__(self, owner: ClockReference, sent: _Reading, epoch: int) -> None:
        self._owner = owner
        self.sent = sent
        self.epoch = epoch
        self.received: _Reading | None = None

    def receipt(self) -> datetime:
        self.received = self._owner._read()
        return self.received.wall


class ClockReference:
    """Stateful corrected-clock estimator (see the module docstring).

    `wall` returns aware datetimes. `monotonic` returns seconds; when it is
    omitted the monotonic time is derived from the same wall reading (an
    injected deterministic test clock), so every stamp costs one wall read
    and no discontinuity can be observed.
    """

    def __init__(
        self, wall: WallClock | None = None, monotonic: MonotonicClock | None = None
    ) -> None:
        self._wall: WallClock = wall if wall is not None else (lambda: datetime.now(timezone.utc))
        if monotonic is None and wall is None:
            monotonic = time.perf_counter
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._samples: deque[_Sample] = deque(maxlen=WINDOW_SAMPLES)
        self._anchor: _Reading | None = None
        self._epoch = 0  # bumped on every discontinuity
        self._backward_pending: timedelta | None = None
        self._cycle_status = "unavailable"
        self._cycle_jump: timedelta | None = None
        self._cycle_record: dict[str, Any] = {}

    # --- readings --------------------------------------------------------------------

    def _read(self) -> _Reading:
        """One paired wall/monotonic reading; detects clock discontinuities."""
        with self._lock:
            wall = self._wall()
            if wall.tzinfo is None or wall.utcoffset() is None:
                raise ValueError("wall clock must be timezone-aware (UTC)")
            wall = wall.astimezone(timezone.utc)
            mono = self._monotonic() if self._monotonic is not None else (wall - _EPOCH).total_seconds()
            reading = _Reading(wall, mono)
            anchor = self._anchor
            if anchor is not None:
                step = (wall - anchor.wall) - timedelta(seconds=mono - anchor.mono)
                if abs(step) > JUMP_TOLERANCE:
                    self._samples.clear()
                    self._epoch += 1
                    self._cycle_jump = step
                    if step < timedelta(0):
                        self._backward_pending = step
            self._anchor = reading
            return reading

    def _best(self, reading: _Reading) -> _Sample | None:
        valid = [s for s in self._samples if timedelta(0) <= s.age(reading) <= MAX_SAMPLE_AGE]
        return min(valid, key=lambda s: s.uncertainty(reading)) if valid else None

    def raw_now(self) -> datetime:
        return self._read().wall

    def now(self) -> datetime:
        """Corrected UTC time: wall + the best valid sample's offset. The raw
        wall time when the current cycle has no applied measurement of its own
        or no valid sample exists (the clock then never PASSes), so the offset
        in effect is always the one `record()` reports."""
        with self._lock:
            reading = self._read()
            best = self._best(reading) if self._cycle_status == "applied" else None
            return reading.wall + best.offset if best is not None else reading.wall

    # --- cycle and measurement -------------------------------------------------------

    def begin_cycle(self) -> None:
        """A new radar cycle: it must earn its own measurement."""
        with self._lock:
            self._cycle_status = "unavailable"
            self._cycle_jump = None
            self._cycle_record = {}
            self._read()  # a step since the previous cycle is this cycle's jump

    def start_probe(self) -> ClockProbe:
        with self._lock:
            sent = self._read()
            return ClockProbe(self, sent, self._epoch)

    def finish_probe(self, probe: ClockProbe, server_time: datetime | None) -> str:
        """Turn a completed round trip into a sample; returns the cycle status."""
        with self._lock:
            received = probe.received if probe.received is not None else self._read()
            rtt = timedelta(seconds=received.mono - probe.sent.mono)
            record: dict[str, Any] = {
                "sent_at": probe.sent.wall.isoformat(),
                "received_at": received.wall.isoformat(),
                "server_time": server_time.isoformat() if server_time is not None else None,
                "offset_bound_ms": None,
            }
            if server_time is not None:
                # The previous (uncorrected) bound, kept for comparison.
                bound = max(abs(server_time - probe.sent.wall), abs(received.wall - server_time))
                record["offset_bound_ms"] = _ms(bound + SERVER_TIME_RESOLUTION)
            self._cycle_record = record
            if server_time is None:
                status = "unavailable"
            elif probe.epoch != self._epoch or rtt < timedelta(0):
                status = "rejected_clock_jump"
            else:
                sample = _Sample(
                    offset=server_time - (probe.sent.wall + rtt / 2), rtt=rtt, wall=received.wall, mono=received.mono
                )
                record["measured_offset_ms"] = _ms(sample.offset)
                record["measured_rtt_ms"] = _ms(rtt)
                if abs(sample.offset) > MAX_PLAUSIBLE_OFFSET:
                    status = "rejected_implausible_offset"
                elif not self._consistent(sample, received):
                    status = "rejected_inconsistent_offset"
                else:
                    self._samples.append(sample)
                    self._backward_pending = None
                    status = "applied"
            self._cycle_status = status
            return status

    def _consistent(self, sample: _Sample, reading: _Reading) -> bool:
        """The new offset interval must overlap every valid window sample's."""
        own = sample.rtt / 2 + SERVER_TIME_RESOLUTION
        for other in self._samples:
            if not timedelta(0) <= other.age(reading) <= MAX_SAMPLE_AGE:
                continue
            if abs(sample.offset - other.offset) > own + other.uncertainty(reading):
                return False
        return True

    # --- evidence --------------------------------------------------------------------

    def sample(self) -> ClockSample:
        """The OC-1 clock evidence at this moment (re-derived: aging and jumps count)."""
        with self._lock:
            reading = self._read()
            jump = self._backward_pending
            if self._cycle_status == "unavailable":
                return ClockSample(None, None, None, backward_jump=jump)
            best = self._best(reading) if self._cycle_status == "applied" else None
            if best is None:
                return ClockSample(False, None, None, backward_jump=jump)
            return ClockSample(True, best.uncertainty(reading), None, backward_jump=jump)

    def record(self) -> dict[str, Any]:
        """JSON-safe `clock_reference` for this cycle's run record."""
        with self._lock:
            reading = self._read()
            status = self._cycle_status
            best = self._best(reading) if status == "applied" else None
            if status == "applied" and best is None:
                status = "no_valid_sample"  # a discontinuity since the measurement discarded it
            record: dict[str, Any] = {"source": SOURCE, **self._cycle_record}
            record.update(
                {
                    "correction_enabled": True,
                    "status": status,
                    "offset_ms": _ms(best.offset) if best is not None else None,
                    "uncertainty_ms": _ms(best.uncertainty(reading)) if best is not None else None,
                    "rtt_ms": _ms(best.rtt) if best is not None else None,
                    "sample_age_ms": _ms(best.age(reading)) if best is not None else None,
                    "samples_in_window": sum(
                        1 for s in self._samples if timedelta(0) <= s.age(reading) <= MAX_SAMPLE_AGE
                    ),
                    "jump_detected_ms": _ms(self._cycle_jump) if self._cycle_jump is not None else None,
                }
            )
            return record


def sample_source(sample: ClockSample | Callable[[], ClockSample] | None) -> Callable[[], ClockSample]:
    """A fixed sample, a re-derivable one (`ClockReference.sample`), or none at
    all - which is UNKNOWN, never "synchronised"."""
    if sample is None:
        unknown = ClockSample(None, None, None)
        return lambda: unknown
    if isinstance(sample, ClockSample):
        fixed = sample
        return lambda: fixed
    return sample


_default_lock = threading.Lock()
_default: ClockReference | None = None


def default_clock_reference() -> ClockReference:
    """The process's estimator (system wall + monotonic clocks), created on first use."""
    global _default
    with _default_lock:
        if _default is None:
            _default = ClockReference()
        return _default


def reset_default_clock_reference(estimator: ClockReference | None = None) -> ClockReference | None:
    """Replace the process's estimator (`None`: a fresh one on next use); returns the old one."""
    global _default
    with _default_lock:
        previous, _default = _default, estimator
        return previous
