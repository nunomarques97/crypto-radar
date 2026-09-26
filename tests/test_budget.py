"""Model budgets on the single charging path (atomic claim + reservation).

Earlier versions of these tests drove `budgets.try_consume_budget`, a check-then-increment
on the legacy `model_budget_usage` table that the heartbeat and the bridge each
called for the same opportunity (the double count). That
function was the defect and is gone; the same four limits are now asserted on
the atomic claim + reservation (`SnapshotStore.claim_invocation`) with the limits
`budgets.model_budget` maps from `config.MODEL_BUDGETS`, read back through
`budgets.budget_status`.
"""

import hashlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import budgets, config
from radar_v08.domain.integrity import InstrumentKind
from radar_v08.domain.invocation import (
    Claimed,
    Direction,
    InvocationIdentity,
    InvocationRequest,
    ModelBudget,
    RefusalReason,
    Refused,
)
from radar_v08.store import SnapshotStore

T0 = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def request(model, index):
    digest = hashlib.sha256(f"{model}-{index}".encode("ascii")).hexdigest()
    identity = InvocationIdentity(
        venue="kraken", market_kind=InstrumentKind.SPOT, native_instrument="XBT/USD", setup="BREAKOUT",
        direction=Direction.LONG, evidence_hash="sha256:" + digest, policy_version="OC-1/test",
    )
    return InvocationRequest(identity=identity, model=model)


class TestBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="crypto-radar-t031b-budget-")
        self.store = SnapshotStore(os.path.join(self.tmp.name, "state.sqlite"))
        self.claims = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def claim(self, model, now):
        self.claims += 1
        return self.store.claim_invocation(
            request(model, self.claims), budgets.model_budget(model), "test-owner", 60, now=now
        )

    def test_model_budget_maps_the_configured_limits(self):
        self.assertEqual(
            budgets.model_budget("FABLE"),
            ModelBudget("FABLE", config.MODEL_BUDGETS["FABLE"]["hourly"], config.MODEL_BUDGETS["FABLE"]["daily"]),
        )
        self.assertEqual(budgets.model_budget("OPUS"), ModelBudget("OPUS", 0, 0))

    def test_hourly_budget_allows_up_to_the_limit(self):
        limit = config.MODEL_BUDGETS["FABLE"]["hourly"]
        for _ in range(limit):
            self.assertIsInstance(self.claim("FABLE", T0), Claimed)

        result = self.claim("FABLE", T0)
        self.assertIsInstance(result, Refused)
        self.assertEqual(result.reason, RefusalReason.HOURLY_BUDGET_EXHAUSTED)
        self.assertEqual(budgets.budget_status(self.store, "FABLE", T0)["hourly_used"], limit)

    def test_daily_budget_caps_even_across_different_hours(self):
        daily_limit = config.MODEL_BUDGETS["FABLE"]["daily"]
        hourly_limit = config.MODEL_BUDGETS["FABLE"]["hourly"]
        consumed = 0
        hour = 0
        while consumed < daily_limit:
            now = T0 + timedelta(hours=hour)
            for _ in range(min(hourly_limit, daily_limit - consumed)):
                self.assertIsInstance(self.claim("FABLE", now), Claimed)
                consumed += 1
            hour += 1

        # Budget for the day is now exhausted, even in a brand new hour.
        later = T0 + timedelta(hours=hour)
        result = self.claim("FABLE", later)
        self.assertIsInstance(result, Refused)
        self.assertEqual(result.reason, RefusalReason.DAILY_BUDGET_EXHAUSTED)
        self.assertEqual(budgets.budget_status(self.store, "FABLE", later)["daily_used"], daily_limit)

    def test_budget_exhausted_is_reported_not_silently_dropped(self):
        limit = config.MODEL_BUDGETS["SONNET"]["hourly"]
        for _ in range(limit):
            self.claim("SONNET", T0)
        result = self.claim("SONNET", T0)
        self.assertIsInstance(result, Refused)
        status = budgets.budget_status(self.store, "SONNET", T0)
        self.assertEqual(status["hourly_used"], limit)
        self.assertEqual(status["hourly_limit"], limit)
        self.assertEqual(status["daily_limit"], config.MODEL_BUDGETS["SONNET"]["daily"])

    def test_budgets_are_independent_per_model(self):
        limit = config.MODEL_BUDGETS["FABLE"]["hourly"]
        for _ in range(limit):
            self.claim("FABLE", T0)
        self.assertIsInstance(self.claim("SONNET", T0), Claimed)
        self.assertEqual(budgets.budget_status(self.store, "SONNET", T0)["hourly_used"], 1)

    def test_budget_status_reads_the_reservations_not_the_legacy_table(self):
        self.store.increment_budget("FABLE", "hour", T0.strftime("%Y-%m-%dT%H:00:00"))
        self.assertEqual(budgets.budget_status(self.store, "FABLE", T0)["hourly_used"], 0)
        self.claim("FABLE", T0)
        self.assertEqual(budgets.budget_status(self.store, "FABLE", T0)["hourly_used"], 1)


if __name__ == "__main__":
    unittest.main()
