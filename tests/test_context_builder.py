import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.context_builder import (
    UNAVAILABLE,
    build_event_context,
    build_model_context,
    deep_mark_unavailable,
)
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore


@dataclass
class FakeL1Features:
    return_15m: float | None = 1.2
    return_1h: float | None = None
    volume_intensity_15m: float | None = 2.0
    futures_oi_delta_1h: float | None = None
    futures_oi_delta_15m: float | None = None
    futures_basis_mark_index: float | None = None


class FakeOpportunity:
    score = 75.0
    breakdown = {"momentum": 0.8}
    derivatives_coherence = "UNKNOWN"


@dataclass
class FakeL2Features:
    return_1h_atr: float = 2.1
    breakout_state: str = "BREAKOUT_UP"
    flags: list = None

    def __post_init__(self):
        if self.flags is None:
            self.flags = ["l2_warmup_gone"]


class FakeSetup:
    notes = ["breakout: range clear + volume"]


class FakeL2Result:
    opportunity = FakeOpportunity()
    l2_features = FakeL2Features()
    setup = FakeSetup()


class FakeTradeability:
    score = 82.0
    state = "TRADEABLE"
    breakdown = {"spread": 0.9}


class FakeL3Result:
    tradeability = FakeTradeability()
    cost_preview = {"spot": {"total_cost_bps": 50.0}}


class FakeQwenReview:
    setup_type = "BREAKOUT"
    direction = "LONG"
    market = "SPOT"
    veto = False
    call_sonnet = False
    call_fable = True
    confidence = "HIGH"
    reason = "coherent"
    data_quality_notes = []


class FakeRouterResult:
    decision = "FABLE"
    model_demand_score = 88.0
    confidence = "HIGH"
    confirmations = ["momentum_2atr_coherent"]
    reasons = ["opportunity>=70 with 3 confirmations"]


class TestDeepMarkUnavailable(unittest.TestCase):
    def test_none_leaf_becomes_unavailable(self):
        self.assertEqual(deep_mark_unavailable(None), UNAVAILABLE)

    def test_nested_none_in_dict(self):
        result = deep_mark_unavailable({"a": 1, "b": None, "c": {"d": None}})
        self.assertEqual(result, {"a": 1, "b": UNAVAILABLE, "c": {"d": UNAVAILABLE}})

    def test_list_of_nones(self):
        self.assertEqual(deep_mark_unavailable([1, None, 2]), [1, UNAVAILABLE, 2])

    def test_real_values_untouched(self):
        self.assertEqual(deep_mark_unavailable({"a": 0, "b": False, "c": ""}), {"a": 0, "b": False, "c": ""})


class TestBuildEventContext(unittest.TestCase):
    def test_missing_data_is_marked_unavailable_not_fabricated(self):
        context = build_event_context(
            asset="BTC", spot_pair="BTC/USD", futures_symbol=None, market="SPOT",
            current_price=50000.0, setup_type="BREAKOUT", direction="LONG", anomaly_score=70.0,
            l1_features=FakeL1Features(), l2_result=FakeL2Result(), l3_result=FakeL3Result(),
            futures_snapshot=None, qwen_review=FakeQwenReview(), router_result=FakeRouterResult(),
            flags=["WIDE_SPREAD"],
        )
        self.assertEqual(context["futures_symbol"], UNAVAILABLE)
        self.assertEqual(context["futures"], UNAVAILABLE)
        self.assertEqual(context["l1_features"]["return_1h"], UNAVAILABLE)
        self.assertEqual(context["l1_features"]["return_15m"], 1.2)

    def test_l2_l3_absent_marks_scores_unavailable(self):
        context = build_event_context(
            asset="BTC", spot_pair="BTC/USD", futures_symbol=None, market="SPOT",
            current_price=50000.0, setup_type="NONE", direction="NONE", anomaly_score=None,
            l1_features=FakeL1Features(), l2_result=None, l3_result=None,
            futures_snapshot=None, qwen_review=None, router_result=FakeRouterResult(),
            flags=[],
        )
        self.assertEqual(context["opportunity_score"], UNAVAILABLE)
        self.assertEqual(context["tradeability_score"], UNAVAILABLE)
        self.assertEqual(context["qwen"], UNAVAILABLE)

    def test_no_kraken_credentials_ever_appear(self):
        os.environ["KRAKEN_API_KEY"] = "super-secret-value-should-never-leak"
        try:
            context = build_event_context(
                asset="BTC", spot_pair="BTC/USD", futures_symbol="PF_XBTUSD", market="FUTURES",
                current_price=50000.0, setup_type="BREAKOUT", direction="LONG", anomaly_score=70.0,
                l1_features=FakeL1Features(), l2_result=FakeL2Result(), l3_result=FakeL3Result(),
                futures_snapshot={"open_interest": 100.0, "funding_rate_raw": 0.001, "funding_semantics": "RAW_UNVERIFIED"},
                qwen_review=FakeQwenReview(), router_result=FakeRouterResult(), flags=[],
            )
            serialized = json.dumps(context)
            self.assertNotIn("super-secret-value-should-never-leak", serialized)
            self.assertNotIn("KRAKEN_API_KEY", serialized)
        finally:
            del os.environ["KRAKEN_API_KEY"]


class TestBuildModelContext(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)
        self._original_events_log_path = config.EVENTS_LOG_PATH
        self.events_log = self.path + ".events.jsonl"
        config.EVENTS_LOG_PATH = self.events_log

    def tearDown(self):
        self.store.close()
        config.EVENTS_LOG_PATH = self._original_events_log_path
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(self.events_log):
            os.remove(self.events_log)

    def test_reconstructs_from_persisted_context_json(self):
        context = build_event_context(
            asset="ETH", spot_pair="ETH/USD", futures_symbol="PF_ETHUSD", market="FUTURES",
            current_price=3000.0, setup_type="CONTINUATION", direction="LONG", anomaly_score=60.0,
            l1_features=FakeL1Features(), l2_result=FakeL2Result(), l3_result=FakeL3Result(),
            futures_snapshot={"open_interest": 500.0, "funding_semantics": "RAW_UNVERIFIED"},
            qwen_review=FakeQwenReview(), router_result=FakeRouterResult(), flags=["BREAKOUT_WITH_VOLUME"],
        )
        event_id, _ = create_event_if_new(
            self.store, ts="2026-09-14T00:00:00+00:00", type_="RADAR_ALERT", asset="ETH",
            setup_type="CONTINUATION", direction="LONG", market="FUTURES",
            anomaly_score=60.0, opportunity_score=75.0, tradeability_score=82.0,
            confidence="HIGH", model_demand="FABLE", reason="opportunity>=70", status="PENDING",
            context=context,
        )
        row = self.store.get_event(event_id)
        model_context = build_model_context(row)

        self.assertEqual(model_context["asset"], "ETH")
        self.assertEqual(model_context["futures_symbol"], "PF_ETHUSD")
        self.assertEqual(model_context["opportunity_score"], 75.0)
        self.assertIn("portfolio", model_context)
        self.assertEqual(model_context["portfolio"]["existing_position"], UNAVAILABLE)
        self.assertEqual(model_context["portfolio"]["pending_orders"], UNAVAILABLE)
        self.assertTrue(model_context["portfolio"]["note"])

    def test_missing_context_json_falls_back_to_row_columns(self):
        event_id, _ = create_event_if_new(
            self.store, ts="2026-09-14T00:00:00+00:00", type_="RADAR_ALERT", asset="SOL",
            setup_type="BREAKOUT", direction="SHORT", market="SPOT",
            anomaly_score=55.0, opportunity_score=71.0, tradeability_score=60.0,
            confidence="MEDIUM", model_demand="SONNET", reason="test", status="PENDING",
        )
        row = self.store.get_event(event_id)
        model_context = build_model_context(row)
        self.assertEqual(model_context["asset"], "SOL")
        self.assertEqual(model_context["direction"], "SHORT")
        self.assertEqual(model_context["opportunity_score"], 71.0)

    def test_portfolio_never_fabricated_even_when_absent_from_persisted_context(self):
        event_id, _ = create_event_if_new(
            self.store, ts="2026-09-14T00:00:00+00:00", type_="RADAR_ALERT", asset="DOGE",
            setup_type="BREAKOUT", direction="LONG", market="SPOT",
            anomaly_score=55.0, opportunity_score=71.0, tradeability_score=60.0,
            confidence="MEDIUM", model_demand="SONNET", reason="test", status="PENDING",
        )
        row = self.store.get_event(event_id)
        model_context = build_model_context(row)
        for key in ("existing_position", "pending_orders", "capital_constraints", "asset_already_in_analysis"):
            self.assertEqual(model_context["portfolio"][key], UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
