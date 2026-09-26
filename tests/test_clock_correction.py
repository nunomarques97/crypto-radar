"""The Kraken-referenced corrected clock.

Unit tests drive `ClockReference` with an injected wall and monotonic clock.
Integration tests run `run_heartbeat(full=True)` over the fake Kraken server
of `tests.test_integrity_wiring` (no socket, no network, a disposable SQLite
store, a fake Qwen that is never Ollama) with a simulated clock: a true time
that the fake server stamps as `serverTime`, a local wall clock that is that
true time plus an adjustable skew, and a monotonic clock that ignores the
skew. The futures tickers round trip lasts a scripted RTT.
"""

import json
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import urlsplit

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TESTS_DIR))
sys.path.insert(0, TESTS_DIR)

from test_integrity_wiring import (  # noqa: E402  (fake Kraken heartbeat harness)
    T0,
    FakeKraken,
    FakeResponse,
    IntegrityWiringBase,
    StepClock,
    _iso_z,
)

from radar_v08 import clock_reference, config  # noqa: E402
from radar_v08 import heartbeat as heartbeat_module  # noqa: E402
from radar_v08.clock_reference import (  # noqa: E402
    MAX_PLAUSIBLE_OFFSET,
    MAX_SAMPLE_AGE,
    SOURCE,
    ClockReference,
    default_clock_reference,
    reset_default_clock_reference,
)
from radar_v08.domain.integrity import CheckStatus, evaluate_clock  # noqa: E402
from radar_v08.http_client import GuardedSession  # noqa: E402

MS = timedelta(milliseconds=1)
TICKERS = "/derivatives/api/v3/tickers"
SPOT_TICKER = "/0/public/Ticker"
HEAD_RECORD_KEYS = {"source", "sent_at", "received_at", "server_time", "offset_bound_ms"}


class SimClock:
    """True time advances 1 ms per wall read; local wall = true + skew;
    monotonic = true time, blind to the skew (thread-safe)."""

    def __init__(self, skew=timedelta(0), start=T0):
        self.true = start
        self.skew = skew
        self._lock = threading.Lock()

    def wall(self):
        with self._lock:
            value = self.true + self.skew
            self.true += MS
            return value

    def mono(self):
        with self._lock:
            return (self.true - T0).total_seconds() + 1000.0

    def advance(self, delta):
        with self._lock:
            self.true += delta

    def step_wall(self, delta):
        """A local clock step (e.g. a Windows time sync) - true time is unaffected."""
        with self._lock:
            self.skew += delta


class SimKraken(FakeKraken):
    """FakeKraken whose futures tickers round trip lasts `rtt` of simulated
    time, with `serverTime` stamped at its midpoint in true time. `hooks`
    run on a request path before it is answered."""

    def __init__(self, sim, rtt=timedelta(milliseconds=10), assets=("BTC",)):
        super().__init__(assets=assets)
        self.sim = sim
        self.rtt = rtt
        self.server_time = True
        self.server_shift = timedelta(0)
        self.hooks = {}

    def request(self, method, url, params=None, timeout=None, allow_redirects=True):
        path = urlsplit(url).path
        hook = self.hooks.pop(path, None)
        if hook is not None:
            hook()
        if path == TICKERS:
            self.sim.advance(self.rtt / 2)
            if self.server_time:
                self.futures_tickers["serverTime"] = _iso_z(self.sim.true + self.server_shift)
            else:
                self.futures_tickers.pop("serverTime", None)
            self.sim.advance(self.rtt / 2)
        return super().request(method, url, params=params, timeout=timeout, allow_redirects=allow_redirects)


class Probe:
    """Drives one round trip on a bare estimator."""

    def __init__(self, sim, estimator):
        self.sim = sim
        self.estimator = estimator

    def measure(self, rtt, server_shift=timedelta(0)):
        probe = self.estimator.start_probe()
        self.sim.advance(rtt / 2)
        server = self.sim.true + server_shift
        self.sim.advance(rtt / 2)
        probe.receipt()
        return self.estimator.finish_probe(probe, server)


def _estimator(sim):
    return ClockReference(wall=sim.wall, monotonic=sim.mono)


class TestEstimator(unittest.TestCase):
    def test_offset_and_uncertainty_of_one_round_trip(self):
        sim = SimClock(skew=-900 * MS)
        est = _estimator(sim)
        est.begin_cycle()
        self.assertEqual(Probe(sim, est).measure(400 * MS), "applied")

        record = est.record()
        self.assertAlmostEqual(record["offset_ms"], 900, delta=2)
        self.assertAlmostEqual(record["rtt_ms"], 400, delta=2)
        self.assertAlmostEqual(record["uncertainty_ms"], 201, delta=2)
        corrected = est.now()
        self.assertLess(abs(corrected - sim.true), 3 * MS)
        sample = est.sample()
        self.assertTrue(sample.synchronized)
        self.assertEqual(evaluate_clock(sample, corrected).status, CheckStatus.PASS)

    def test_min_uncertainty_window_picks_lowest_rtt_and_drops_old_samples(self):
        sim = SimClock(skew=300 * MS)
        est = _estimator(sim)
        probe = Probe(sim, est)
        est.begin_cycle()
        self.assertEqual(probe.measure(60 * MS), "applied")
        sim.advance(timedelta(seconds=100))
        self.assertEqual(probe.measure(400 * MS), "applied")
        sim.advance(timedelta(seconds=10))
        self.assertEqual(probe.measure(250 * MS), "applied")

        record = est.record()
        self.assertEqual(record["samples_in_window"], 3)
        self.assertAlmostEqual(record["rtt_ms"], 60, delta=2)  # the lowest-RTT sample wins

        # Past MAX_SAMPLE_AGE the 60 ms sample is never used; the best of the rest is.
        sim.advance(MAX_SAMPLE_AGE - timedelta(seconds=105))
        record = est.record()
        self.assertEqual(record["samples_in_window"], 2)
        self.assertAlmostEqual(record["rtt_ms"], 250, delta=2)

        # All of them old: no sample, the clock never PASSes.
        sim.advance(MAX_SAMPLE_AGE)
        self.assertIsNone(est.record()["offset_ms"])
        self.assertIsNot(est.sample().synchronized, True)

    def test_aging_grows_the_uncertainty(self):
        sim = SimClock()
        est = _estimator(sim)
        est.begin_cycle()
        Probe(sim, est).measure(100 * MS)
        fresh = est.sample().offset_uncertainty
        sim.advance(timedelta(seconds=200))
        self.assertGreater(est.sample().offset_uncertainty, fresh + 15 * MS)  # 100 ppm x 200 s = 20 ms

    def test_implausible_offset_is_rejected_and_never_applied(self):
        sim = SimClock()
        est = _estimator(sim)
        est.begin_cycle()
        status = Probe(sim, est).measure(20 * MS, server_shift=MAX_PLAUSIBLE_OFFSET + timedelta(seconds=1))

        self.assertEqual(status, "rejected_implausible_offset")
        self.assertLess(abs(est.now() - sim.true), 3 * MS)  # the radar's time did not move
        self.assertEqual(evaluate_clock(est.sample(), est.now()).status, CheckStatus.FAIL)
        self.assertIsNone(est.record()["offset_ms"])

    def test_offset_contradicting_the_window_is_rejected(self):
        sim = SimClock()
        est = _estimator(sim)
        probe = Probe(sim, est)
        est.begin_cycle()
        probe.measure(20 * MS)
        est.begin_cycle()
        self.assertEqual(probe.measure(20 * MS, server_shift=timedelta(seconds=5)), "rejected_inconsistent_offset")
        self.assertEqual(evaluate_clock(est.sample(), est.now()).status, CheckStatus.FAIL)
        self.assertLess(abs(est.now() - sim.true), 3 * MS)

    def test_a_new_cycle_must_earn_its_own_measurement(self):
        sim = SimClock()
        est = _estimator(sim)
        est.begin_cycle()
        Probe(sim, est).measure(20 * MS)
        est.begin_cycle()  # no measurement this cycle

        result = evaluate_clock(est.sample(), est.now())
        self.assertEqual(result.status, CheckStatus.UNKNOWN)
        self.assertEqual([r.code.value for r in result.reasons], ["clock_sync_unknown", "clock_uncertainty_unknown"])

    def test_backward_jump_suspends_until_a_new_measurement(self):
        sim = SimClock()
        est = _estimator(sim)
        probe = Probe(sim, est)
        est.begin_cycle()
        probe.measure(20 * MS)
        sim.step_wall(-900 * MS)

        result = evaluate_clock(est.sample(), est.now())
        self.assertEqual(result.status, CheckStatus.FAIL)
        self.assertIn("clock_backward_jump", [r.code.value for r in result.reasons])
        self.assertAlmostEqual(est.record()["jump_detected_ms"], -900, delta=2)

        est.begin_cycle()
        self.assertIn("clock_backward_jump", [r.code.value for r in evaluate_clock(est.sample(), est.now()).reasons])
        self.assertEqual(probe.measure(20 * MS), "applied")
        self.assertEqual(evaluate_clock(est.sample(), est.now()).status, CheckStatus.PASS)
        self.assertAlmostEqual(est.record()["offset_ms"], 900, delta=2)

    def test_jump_during_the_round_trip_rejects_the_sample(self):
        sim = SimClock()
        est = _estimator(sim)
        est.begin_cycle()
        probe = est.start_probe()
        sim.step_wall(-900 * MS)
        server = sim.true
        probe.receipt()
        self.assertEqual(est.finish_probe(probe, server), "rejected_clock_jump")
        self.assertEqual(evaluate_clock(est.sample(), est.now()).status, CheckStatus.FAIL)

    def test_small_slew_is_aging_not_a_jump(self):
        sim = SimClock()
        est = _estimator(sim)
        est.begin_cycle()
        Probe(sim, est).measure(20 * MS)
        sim.step_wall(-40 * MS)  # below JUMP_TOLERANCE
        sample = est.sample()
        self.assertTrue(sample.synchronized)
        self.assertIsNone(sample.backward_jump)
        self.assertGreaterEqual(sample.offset_uncertainty, 40 * MS)

    def test_injected_wall_only_clock_derives_monotonic_time(self):
        est = ClockReference(wall=StepClock())
        est.begin_cycle()
        probe = est.start_probe()
        probe.receipt()
        self.assertEqual(est.finish_probe(probe, T0 + 3 * MS), "applied")
        self.assertEqual(est.sample().backward_jump, None)
        self.assertEqual(evaluate_clock(est.sample(), est.now()).status, CheckStatus.PASS)

    def test_process_default_persists_and_resets(self):
        previous = reset_default_clock_reference()
        self.addCleanup(reset_default_clock_reference, previous)
        first = default_clock_reference()
        self.assertIs(default_clock_reference(), first)
        injected = ClockReference(wall=StepClock())
        self.assertIs(reset_default_clock_reference(injected), first)
        self.assertIs(default_clock_reference(), injected)


class CorrectedClockCycleBase(IntegrityWiringBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cycle(self, kraken, estimator):
        session = GuardedSession(1.0, 0, 0.0, http_session=kraken)
        return heartbeat_module.run_heartbeat(
            mode="TEST", store=self.store, full=True, session=session, clock=kraken.sim.wall, clock_estimator=estimator
        )

    def integrity(self, output):
        return output["data_quality"]["integrity"]


class TestCorrectedClockCycle(CorrectedClockCycleBase):
    def test_a_local_clock_900ms_behind_with_rtt_400ms_passes(self):
        sim = SimClock(skew=-900 * MS)
        kraken = SimKraken(sim, rtt=400 * MS)

        output = self.cycle(kraken, _estimator(sim))

        integrity = self.integrity(output)
        self.assertEqual(integrity["clock"], "PASS")
        self.assertEqual(integrity["clock_reasons"], [])
        ref = integrity["clock_reference"]
        self.assertEqual(ref["source"], SOURCE)
        self.assertTrue(ref["correction_enabled"])
        self.assertEqual(ref["status"], "applied")
        self.assertAlmostEqual(ref["offset_ms"], 900, delta=3)
        self.assertAlmostEqual(ref["uncertainty_ms"], 201, delta=3)
        self.assertAlmostEqual(ref["rtt_ms"], 400, delta=3)
        self.assertGreaterEqual(ref["sample_age_ms"], 0)
        self.assertEqual(ref["samples_in_window"], 1)
        self.assertIsNone(ref["jump_detected_ms"])
        # The previous keys are kept (the uncorrected bound, for comparison).
        self.assertTrue(HEAD_RECORD_KEYS <= set(ref))
        self.assertGreater(ref["offset_bound_ms"], 500)
        self.assertEqual(self.qwen.assets, ["BTC"])
        # The cycle stamp is on the corrected clock, not the local one.
        self.assertLess(abs(datetime.fromisoformat(output["timestamp"]) - T0), 450 * MS)
        # The measurement comes before the other stamped receipts.
        paths = kraken.paths()
        self.assertLess(paths.index(TICKERS), paths.index(SPOT_TICKER))

    def test_rtt_too_large_still_fails(self):
        sim = SimClock(skew=-900 * MS)
        kraken = SimKraken(sim, rtt=1000 * MS)  # RTT/2 + 1 ms > 500 ms

        output = self.cycle(kraken, _estimator(sim))

        self.assert_blocked_before_model(output)
        self.assertEqual(self.integrity(output)["clock"], "FAIL")
        self.assertEqual(self.integrity(output)["clock_reasons"], ["clock_uncertainty_exceeded"])

    def test_unavailable_reference_stays_unknown_even_with_older_samples(self):
        sim = SimClock(skew=-900 * MS)
        estimator = _estimator(sim)
        self.assertEqual(self.integrity(self.cycle(SimKraken(sim, rtt=100 * MS), estimator))["clock"], "PASS")

        for breakage in ("503", "no_server_time"):
            with self.subTest(breakage=breakage):
                self.qwen.calls.clear()
                self.routed.clear()
                kraken = SimKraken(sim, rtt=100 * MS)
                if breakage == "503":
                    kraken.overrides[(TICKERS, None)] = FakeResponse(status_code=503)
                else:
                    kraken.server_time = False
                start = sim.true

                output = self.cycle(kraken, estimator)

                integrity = self.integrity(output)
                self.assertEqual(integrity["clock"], "UNKNOWN")
                self.assertEqual(integrity["clock_reasons"], ["clock_sync_unknown", "clock_uncertainty_unknown"])
                self.assertEqual(integrity["l1_blocked"], 1)
                self.assertEqual(self.qwen.calls, [])
                self.assertEqual(self.routed, [])
                # The older sample's +900 ms is not applied: the record says
                # no offset and the cycle is stamped on the raw local clock.
                ref = integrity["clock_reference"]
                self.assertIsNone(ref["offset_ms"])
                self.assertEqual(ref["samples_in_window"], 1)
                stamp = datetime.fromisoformat(output["timestamp"])
                self.assertLess(abs(stamp - (start + sim.skew)), 400 * MS)

    def test_rejected_measurement_stamps_on_the_raw_clock(self):
        sim = SimClock(skew=-900 * MS)
        estimator = _estimator(sim)
        self.assertEqual(self.integrity(self.cycle(SimKraken(sim, rtt=100 * MS), estimator))["clock"], "PASS")

        for status, shift in (
            ("rejected_inconsistent_offset", timedelta(seconds=5)),
            ("rejected_implausible_offset", MAX_PLAUSIBLE_OFFSET + timedelta(seconds=1)),
        ):
            with self.subTest(status=status):
                kraken = SimKraken(sim, rtt=100 * MS)
                kraken.server_shift = shift
                self.qwen.calls.clear()
                self.routed.clear()
                start = sim.true

                output = self.cycle(kraken, estimator)

                self.assertEqual(self.qwen.calls, [])
                self.assertEqual(self.routed, [])
                integrity = self.integrity(output)
                self.assertEqual(integrity["clock"], "FAIL")
                ref = integrity["clock_reference"]
                self.assertEqual(ref["status"], status)
                self.assertIsNone(ref["offset_ms"])
                stamp = datetime.fromisoformat(output["timestamp"])
                self.assertLess(abs(stamp - (start + sim.skew)), 400 * MS)

    def test_backward_jump_after_the_measurement_fails_the_cycle_then_recovers(self):
        sim = SimClock()
        estimator = _estimator(sim)
        kraken = SimKraken(sim, rtt=100 * MS)
        kraken.hooks[SPOT_TICKER] = lambda: sim.step_wall(-900 * MS)

        output = self.cycle(kraken, estimator)

        self.assert_blocked_before_model(output)
        integrity = self.integrity(output)
        self.assertEqual(integrity["clock"], "FAIL")
        self.assertIn("clock_backward_jump", integrity["clock_reasons"])
        self.assertAlmostEqual(integrity["clock_reference"]["jump_detected_ms"], -900, delta=3)

        output = self.cycle(SimKraken(sim, rtt=100 * MS), estimator)

        integrity = self.integrity(output)
        self.assertEqual(integrity["clock"], "PASS")
        self.assertEqual(integrity["clock_reasons"], [])
        self.assertAlmostEqual(integrity["clock_reference"]["offset_ms"], 900, delta=3)
        self.assertEqual(self.qwen.assets, ["BTC"])

    def test_jump_between_cycles_discards_pre_jump_samples(self):
        sim = SimClock()
        estimator = _estimator(sim)
        first = self.integrity(self.cycle(SimKraken(sim, rtt=40 * MS), estimator))
        self.assertEqual(first["clock"], "PASS")
        self.assertAlmostEqual(first["clock_reference"]["offset_ms"], 0, delta=3)

        sim.step_wall(-900 * MS)
        second = self.integrity(self.cycle(SimKraken(sim, rtt=300 * MS), estimator))

        self.assertEqual(second["clock"], "PASS")
        ref = second["clock_reference"]
        self.assertEqual(ref["samples_in_window"], 1)  # the 40 ms pre-jump sample is gone
        self.assertAlmostEqual(ref["rtt_ms"], 300, delta=3)
        self.assertAlmostEqual(ref["offset_ms"], 900, delta=3)
        self.assertAlmostEqual(ref["jump_detected_ms"], -900, delta=3)

    def test_freshness_is_judged_on_the_corrected_clock(self):
        # 900 ms ahead: every receipt is stamped on the corrected clock, so a
        # fresh ticker is neither future-dated nor aged by the skew.
        sim = SimClock(skew=900 * MS)
        kraken = SimKraken(sim, rtt=100 * MS)

        output = self.cycle(kraken, _estimator(sim))

        integrity = self.integrity(output)
        self.assertEqual(integrity["clock"], "PASS")
        self.assertEqual(integrity["spot_rows_rejected"], 0)
        self.assertEqual(integrity["futures_eligible"], 1)
        self.assertEqual(self.qwen.assets, ["BTC"])
        record = json.loads(self.events()[0]["context_json"])["integrity"]
        evaluated = datetime.fromisoformat(record["evaluated_at"])
        ticker = next(r for r in record["results"] if r["capability"] == "spot_ticker")
        self.assertEqual(ticker["status"], "PASS")
        received = datetime.fromisoformat(ticker["received_at"])
        # On the true (corrected) basis: within the simulated elapsed time of T0, not 900 ms ahead.
        self.assertLess(abs(received - T0), 150 * MS)
        self.assertLess(abs(evaluated - sim.true), 50 * MS)

    def test_futures_source_age_uses_the_corrected_clock(self):
        # 45 s behind: on the raw clock the venue's last-trade time (T0 - 5 s)
        # would be 40 s in the future; on the corrected clock it is 5 s old.
        sim = SimClock(skew=timedelta(seconds=-45))
        kraken = SimKraken(sim, rtt=100 * MS)

        output = self.cycle(kraken, _estimator(sim))

        integrity = self.integrity(output)
        self.assertEqual(integrity["clock"], "PASS")
        self.assertEqual(integrity["futures_eligible"], 1)
        self.assertEqual(integrity["futures_rejected"], 0)
        self.assertEqual(self.qwen.assets, ["BTC"])

    def test_implausible_venue_time_is_rejected_and_blocks(self):
        sim = SimClock()
        kraken = SimKraken(sim, rtt=40 * MS)
        kraken.server_shift = timedelta(minutes=5)  # a bogus serverTime

        output = self.cycle(kraken, _estimator(sim))

        self.assert_blocked_before_model(output)
        integrity = self.integrity(output)
        self.assertEqual(integrity["clock"], "FAIL")
        ref = integrity["clock_reference"]
        self.assertEqual(ref["status"], "rejected_implausible_offset")
        self.assertIsNone(ref["offset_ms"])
        # The radar's time was not moved by the venue.
        self.assertLess(abs(datetime.fromisoformat(output["timestamp"]) - T0), 150 * MS)


class TestSwitchOffIsHead(IntegrityWiringBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No estimator at all with the switch off: the process default is never touched.
        sentinel = mock.Mock()
        previous = reset_default_clock_reference(sentinel)
        self.addCleanup(reset_default_clock_reference, previous)
        self.addCleanup(lambda: self.assertEqual(sentinel.mock_calls, []))

    def test_skewed_clock_fails_with_the_per_cycle_bound(self):
        kraken = FakeKraken()
        kraken.futures_tickers["serverTime"] = _iso_z(T0 + timedelta(seconds=2))

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["clock"], "FAIL")
        self.assertEqual(integrity["clock_reasons"], ["clock_uncertainty_exceeded"])
        ref = integrity["clock_reference"]
        self.assertEqual(set(ref), HEAD_RECORD_KEYS)
        sent = datetime.fromisoformat(ref["sent_at"])
        received = datetime.fromisoformat(ref["received_at"])
        server = T0 + timedelta(seconds=2)
        expected = max(abs(server - sent), abs(received - server)) + MS
        self.assertEqual(ref["offset_bound_ms"], round(expected / MS, 3))
        # HEAD order and stamps: the raw clock's first reading is the cycle stamp,
        # the spot ticker precedes the futures tickers.
        self.assertEqual(output["timestamp"], T0.isoformat())
        paths = kraken.paths()
        self.assertLess(paths.index(SPOT_TICKER), paths.index(TICKERS))

    def test_unavailable_reference_is_unknown(self):
        kraken = FakeKraken()
        kraken.overrides[(TICKERS, None)] = FakeResponse(status_code=503)

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["clock"], "UNKNOWN")
        self.assertEqual(integrity["clock_reasons"], ["clock_sync_unknown", "clock_uncertainty_unknown"])
        self.assertEqual(integrity["clock_reference"], {"source": SOURCE, "status": "unavailable"})
        self.assertEqual(output["timestamp"], T0.isoformat())

    def test_synchronised_clock_passes_on_the_raw_clock(self):
        output = self.run_cycle(FakeKraken())

        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["clock"], "PASS")
        self.assertEqual(set(integrity["clock_reference"]), HEAD_RECORD_KEYS)
        self.assertEqual(output["timestamp"], T0.isoformat())


class TestModuleSurface(unittest.TestCase):
    def test_switch_defaults_on(self):
        if "RADAR_CLOCK_CORRECTION_ENABLED" in os.environ:
            self.skipTest("switch set in the environment")
        self.assertIs(config.RADAR_CLOCK_CORRECTION_ENABLED, True)

    def test_limit_is_unchanged(self):
        from radar_v08.domain.integrity import OC1_POLICY

        self.assertEqual(OC1_POLICY.clock_max_uncertainty, timedelta(milliseconds=500))
        self.assertEqual(clock_reference.MAX_PLAUSIBLE_OFFSET, timedelta(seconds=60))


if __name__ == "__main__":
    unittest.main()
