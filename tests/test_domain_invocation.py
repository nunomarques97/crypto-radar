"""T031a: pure invocation identity, budget decision and lease fencing (radar_v08/domain/invocation.py).

No database, no I/O: the store adapter is covered by tests/test_invocation_store.py.
"""

import ast
import os
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.domain import invocation as inv
from radar_v08.domain.integrity import InstrumentKind
from radar_v08.domain.invocation import (
    BUSY_RETRY_DELAYS,
    BudgetUsage,
    BudgetWindows,
    Direction,
    InvocationError,
    InvocationFailure,
    InvocationIdentity,
    InvocationRecord,
    InvocationRequest,
    InvocationState,
    Lease,
    ModelBudget,
    RefusalReason,
    TransitionStatus,
    budget_windows,
    fence_status,
    lease_expiry,
    refusal_reason,
    utc_text,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 18, 12, 34, 56, 789000, tzinfo=UTC)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def identity(**changes):
    base = InvocationIdentity(
        venue="kraken",
        market_kind=InstrumentKind.SPOT,
        native_instrument="XBT/USD",
        setup="BREAKOUT",
        direction=Direction.LONG,
        evidence_hash=HASH_A,
        policy_version="OC-1",
    )
    return replace(base, **changes)


def record(**changes):
    base = InvocationRecord(
        invocation_id="inv-1",
        identity=identity(),
        model="qwen3:14b",
        state=InvocationState.CLAIMED,
        generation=1,
        lease_owner="worker-a",
        lease_expires_at=NOW + timedelta(seconds=30),
        demand_count=1,
        attempt_count=0,
        windows=budget_windows(NOW),
        claimed_at=NOW,
        updated_at=NOW,
        ended_at=None,
        end_reason=None,
    )
    return replace(base, **changes)


LEASE = Lease("inv-1", 1, "worker-a", NOW + timedelta(seconds=30))


class TestIdentity(unittest.TestCase):
    def test_identity_is_exactly_the_oc1_tuple_in_order(self):
        self.assertEqual(
            identity().columns(), ("kraken", "spot", "XBT/USD", "BREAKOUT", "LONG", HASH_A, "OC-1")
        )
        self.assertEqual(
            [field for field in InvocationIdentity.__dataclass_fields__],
            ["venue", "market_kind", "native_instrument", "setup", "direction", "evidence_hash", "policy_version"],
        )

    def test_equal_fields_equal_identity_and_changed_evidence_hash_is_different(self):
        self.assertEqual(identity(), identity())
        self.assertEqual(hash(identity()), hash(identity()))
        self.assertNotEqual(identity(), identity(evidence_hash=HASH_B))

    def test_every_field_changes_the_identity(self):
        variants = [
            identity(venue="binance"),
            identity(market_kind=InstrumentKind.FUTURES),
            identity(native_instrument="XBT/EUR"),
            identity(setup="SQUEEZE"),
            identity(direction=Direction.SHORT),
            identity(evidence_hash=HASH_B),
            identity(policy_version="OC-2"),
        ]
        self.assertEqual(len({v.columns() for v in variants} | {identity().columns()}), 8)

    def test_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            identity().venue = "other"  # type: ignore[misc]

    def test_invalid_fields_are_typed_failures(self):
        bad = [
            {"venue": ""},
            {"venue": " kraken"},
            {"venue": "kra\nken"},
            {"venue": "k" * 201},
            {"setup": 7},
            {"market_kind": "spot"},
            {"direction": "LONG"},
            {"evidence_hash": "a" * 64},
            {"evidence_hash": "sha256:" + "A" * 64},
            {"evidence_hash": "sha256:" + "a" * 63},
            {"evidence_hash": "sha256:" + "g" * 64},
            {"policy_version": ""},
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(InvocationError) as caught:
                identity(**change)
            self.assertEqual(caught.exception.code, InvocationFailure.INVALID_FIELD)

    def test_request_and_budget_validation(self):
        self.assertEqual(InvocationRequest(identity(), "qwen3:14b").model, "qwen3:14b")
        for build in (
            lambda: InvocationRequest(identity(), ""),
            lambda: InvocationRequest(("kraken",), "m"),
            lambda: ModelBudget("m", -1, 5),
            lambda: ModelBudget("m", 1, True),
            lambda: ModelBudget("m", 1.0, 5),
        ):
            with self.assertRaises(InvocationError) as caught:
                build()
            self.assertEqual(caught.exception.code, InvocationFailure.INVALID_FIELD)


class TestWindowsAndBudget(unittest.TestCase):
    def test_windows_are_utc_hour_and_day(self):
        self.assertEqual(budget_windows(NOW), BudgetWindows("2026-09-18T12:00:00.000000+00:00", "2026-09-18"))
        lisbon = timezone(timedelta(hours=1))
        late = datetime(2026, 9, 19, 0, 30, tzinfo=lisbon)  # 23:30 UTC on the 18th
        self.assertEqual(budget_windows(late), BudgetWindows("2026-09-18T23:00:00.000000+00:00", "2026-09-18"))

    def test_naive_clock_is_refused(self):
        for call in (lambda: budget_windows(datetime(2026, 9, 18)), lambda: utc_text(datetime(2026, 9, 18))):
            with self.assertRaises(InvocationError) as caught:
                call()
            self.assertEqual(caught.exception.code, InvocationFailure.INVALID_CLOCK)

    def test_utc_text_is_fixed_width_so_text_order_is_time_order(self):
        early = utc_text(datetime(2026, 9, 18, 9, 0, 0, tzinfo=UTC))
        later = utc_text(datetime(2026, 9, 18, 9, 0, 0, 1, tzinfo=UTC))
        self.assertEqual(len(early), len(later))
        self.assertLess(early, later)

    def usage(self, hourly, daily, hourly_limit=2, daily_limit=5):
        return BudgetUsage("m", budget_windows(NOW), hourly, hourly_limit, daily, daily_limit)

    def test_refusal_reason_perturbation(self):
        self.assertIsNone(refusal_reason(self.usage(1, 4)))
        self.assertEqual(refusal_reason(self.usage(2, 4)), RefusalReason.HOURLY_BUDGET_EXHAUSTED)
        self.assertEqual(refusal_reason(self.usage(1, 5)), RefusalReason.DAILY_BUDGET_EXHAUSTED)
        self.assertEqual(refusal_reason(self.usage(2, 5)), RefusalReason.HOURLY_BUDGET_EXHAUSTED)
        # Raising a limit by one changes the answer.
        self.assertIsNone(refusal_reason(self.usage(2, 4, hourly_limit=3)))
        self.assertIsNone(refusal_reason(self.usage(1, 5, daily_limit=6)))
        # A limit of zero refuses everything.
        self.assertEqual(refusal_reason(self.usage(0, 0, 0, 0)), RefusalReason.HOURLY_BUDGET_EXHAUSTED)

    def test_lease_expiry_bounds(self):
        self.assertEqual(lease_expiry(NOW, 30), NOW + timedelta(seconds=30))
        for seconds in (0, 3601, True, 1.5):
            with self.assertRaises(InvocationError):
                lease_expiry(NOW, seconds)

    def test_busy_retry_schedule_is_50_100_200_ms(self):
        self.assertEqual(BUSY_RETRY_DELAYS, (0.05, 0.1, 0.2))


class TestFence(unittest.TestCase):
    def test_current_holder_inside_lease_applies(self):
        self.assertIs(fence_status(record(), LEASE, NOW), TransitionStatus.APPLIED)

    def test_stale_generation_is_fenced(self):
        self.assertIs(fence_status(record(generation=2), LEASE, NOW), TransitionStatus.FENCED)

    def test_other_owner_is_fenced(self):
        self.assertIs(fence_status(record(lease_owner="worker-b"), LEASE, NOW), TransitionStatus.FENCED)

    def test_expired_lease_cannot_act_even_before_recovery(self):
        at_expiry = NOW + timedelta(seconds=30)
        self.assertIs(fence_status(record(), LEASE, at_expiry), TransitionStatus.LEASE_EXPIRED)

    def test_terminal_and_missing(self):
        for state in (InvocationState.COMPLETED, InvocationState.RELEASED):
            self.assertIs(fence_status(record(state=state), LEASE, NOW), TransitionStatus.NOT_ACTIVE)
        self.assertIs(fence_status(None, LEASE, NOW), TransitionStatus.NOT_FOUND)
        self.assertIs(fence_status(record(invocation_id="inv-2"), LEASE, NOW), TransitionStatus.NOT_FOUND)

    def test_order_terminal_before_fence_before_expiry(self):
        late = NOW + timedelta(hours=1)
        stale_terminal = record(state=InvocationState.COMPLETED, generation=3)
        self.assertIs(fence_status(stale_terminal, LEASE, late), TransitionStatus.NOT_ACTIVE)
        self.assertIs(fence_status(record(generation=3), LEASE, late), TransitionStatus.FENCED)

    def test_lease_validation(self):
        for build in (
            lambda: Lease("inv-1", 0, "w", NOW),
            lambda: Lease("", 1, "w", NOW),
            lambda: Lease("inv-1", 1, "", NOW),
            lambda: Lease("inv-1", 1, "w", datetime(2026, 9, 18)),
        ):
            with self.assertRaises(InvocationError):
                build()


class TestPurity(unittest.TestCase):
    """Domain module: stdlib plus the integrity domain only, no I/O, no wall clock."""

    def setUp(self):
        with open(inv.__file__, encoding="utf-8") as handle:
            self.source = handle.read()
        self.tree = ast.parse(self.source)

    def test_imports_are_stdlib_pure(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertLessEqual(imported, {"__future__", "dataclasses", "datetime", "enum", "radar_v08.domain.integrity"})

    def test_no_wall_clock_or_io_calls(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                self.assertNotIn(name, {"now", "utcnow", "today", "time", "monotonic", "sleep", "open"})
        self.assertFalse("sqlite" in self.source.lower(), "the domain module must not mention the database engine")


if __name__ == "__main__":
    unittest.main()
