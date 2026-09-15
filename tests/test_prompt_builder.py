import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.context_builder import UNAVAILABLE, build_event_context
from radar_v08.events import create_event_if_new
from radar_v08.prompt_builder import build_prompt_context, build_prompt_text
from radar_v08.store import SnapshotStore


@dataclass
class FakeL1Features:
    return_15m: float | None = 1.2
    return_1h: float | None = None
    volume_intensity_15m: float | None = 2.0
    futures_oi_delta_1h: float | None = None


class FakeOpportunity:
    score = 82.4
    breakdown = {"momentum": 0.8}
    derivatives_coherence = "COHERENT"


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
    score = 88.1
    state = "TRADEABLE"
    breakdown = {"spread": 0.9, "depth": 0.85}


class FakeL3Result:
    tradeability = FakeTradeability()
    cost_preview = {
        "spot": {"spread_bps": 12.0, "slippage_bps": 4.0, "total_cost_bps": 42.0},
        "futures": {
            "spread_bps": 5.0, "slippage_bps": 2.0, "total_cost_bps": 15.0,
            "funding_raw": 0.0001, "funding_semantics": "RAW_UNVERIFIED",
        },
    }


class FakeQwenReview:
    setup_type = "BREAKOUT"
    direction = "LONG"
    market = "FUTURES"
    veto = False
    call_sonnet = False
    call_fable = True
    confidence = "HIGH"
    reason = "momentum + volume confirmed, derivatives coherent"
    data_quality_notes = []


class FakeRouterResult:
    decision = "FABLE"
    model_demand_score = 91.0
    confidence = "HIGH"
    confirmations = ["momentum_2atr_coherent", "taker_imbalance"]
    reasons = ["opportunity>=70 with 2 confirmations"]


def make_full_context(**overrides):
    kwargs = dict(
        asset="PEPE", spot_pair="PEPE/USD", futures_symbol="PF_PEPEUSD", market="FUTURES",
        current_price=0.0000123, setup_type="BREAKOUT", direction="LONG", anomaly_score=70.0,
        l1_features=FakeL1Features(), l2_result=FakeL2Result(), l3_result=FakeL3Result(),
        futures_snapshot={"open_interest": 12_345_678.0, "funding_rate_raw": 0.0001, "basis": 0.002},
        qwen_review=FakeQwenReview(), router_result=FakeRouterResult(), flags=["BREAKOUT_WITH_VOLUME"],
    )
    kwargs.update(overrides)
    return build_event_context(**kwargs)


class _StoreTestCase(unittest.TestCase):
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
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)
        if os.path.exists(self.events_log):
            os.remove(self.events_log)

    def _make_event_row(self, context=None, **overrides):
        base = dict(
            ts="2026-09-14T00:42:15+00:00", type_="RADAR_ALERT", asset="PEPE",
            setup_type="BREAKOUT", direction="LONG", market="FUTURES",
            anomaly_score=70.0, opportunity_score=82.4, tradeability_score=88.1,
            confidence="HIGH", model_demand="FABLE", reason="opportunity>=70 with 2 confirmations",
            status="PROCESSED",
        )
        base.update(overrides)
        event_id, _created = create_event_if_new(self.store, context=context, **base)
        return self.store.get_event(event_id)


class TestPromptContainsCoreFields(_StoreTestCase):
    def test_event_id_is_included(self):
        row = self._make_event_row(context=make_full_context())
        text = build_prompt_text(row)
        self.assertIn(f"Event ID: {row['event_id']}", text)
        self.assertIn(row["event_id"], text)

    def test_header_fields_included(self):
        row = self._make_event_row(context=make_full_context())
        text = build_prompt_text(row)
        for expected in (
            "Asset: PEPE", "Market: FUTURES", "Direction: LONG", "Setup: BREAKOUT",
            "Anomaly Score: 70.00", "Opportunity Score: 82.40", "Tradeability Score: 88.10",
            "Confidence: HIGH", "Model Demand: FABLE",
        ):
            self.assertIn(expected, text)

    def test_spot_pair_and_futures_symbol_included_in_data_block(self):
        row = self._make_event_row(context=make_full_context())
        text = build_prompt_text(row)
        self.assertIn("PEPE/USD", text)
        self.assertIn("PF_PEPEUSD", text)

    def test_l1_l2_l3_and_cost_preview_data_included(self):
        row = self._make_event_row(context=make_full_context())
        text = build_prompt_text(row)
        self.assertIn("return_15m", text)          # L1
        self.assertIn("breakout_state", text)       # L2
        self.assertIn("tradeability_breakdown", text)  # L3
        self.assertIn("cost_preview", text)
        self.assertIn("funding_semantics", text)
        self.assertIn("RAW_UNVERIFIED", text)
        self.assertIn("open_interest", text)

    def test_qwen_result_included(self):
        row = self._make_event_row(context=make_full_context())
        text = build_prompt_text(row)
        self.assertIn('"qwen"', text)
        self.assertIn("momentum + volume confirmed, derivatives coherent", text)
        self.assertIn("call_fable", text)

    def test_reason_included(self):
        row = self._make_event_row(context=make_full_context())
        text = build_prompt_text(row)
        self.assertIn("opportunity>=70 with 2 confirmations", text)

    def test_structure_matches_required_template(self):
        row = self._make_event_row(context=make_full_context())
        text = build_prompt_text(row)
        self.assertIn("CRYPTO RADAR", text)
        self.assertIn("ALERTA", text)
        self.assertIn("DADOS DO RADAR", text)
        self.assertIn("INSTRUÇÕES PARA CLAUDE", text)
        self.assertIn("crypto-trading-system", text)
        self.assertIn("TRADING_STATE.md", text)
        self.assertIn("TRADING_HISTORY.md", text)
        self.assertIn("END CRYPTO RADAR ALERT", text)


class TestMissingFieldsBecomeUnavailable(_StoreTestCase):
    def test_no_context_json_falls_back_to_unavailable(self):
        row = self._make_event_row(context=None, setup_type=None, direction=None)
        text = build_prompt_text(row)
        self.assertIn(UNAVAILABLE, text)
        context = build_prompt_context(row)
        self.assertEqual(context["l1_features"], UNAVAILABLE)
        self.assertEqual(context["qwen"], UNAVAILABLE)
        self.assertEqual(context["cost_preview"], UNAVAILABLE)

    def test_partial_context_only_marks_missing_leaves(self):
        context = make_full_context(qwen_review=None, l3_result=None)
        row = self._make_event_row(context=context)
        prompt_context = build_prompt_context(row)
        self.assertEqual(prompt_context["qwen"], UNAVAILABLE)
        self.assertEqual(prompt_context["cost_preview"], UNAVAILABLE)
        self.assertIsInstance(prompt_context["l1_features"], dict)  # still present

    def test_no_field_is_fabricated_as_a_number(self):
        row = self._make_event_row(context=None)
        prompt_context = build_prompt_context(row)
        self.assertNotIn(0, [prompt_context.get("opportunity_breakdown")])


class TestPromptIsEventSpecific(_StoreTestCase):
    def test_two_events_produce_different_prompts(self):
        row_a = self._make_event_row(context=make_full_context(asset="PEPE"), asset="PEPE")
        row_b = self._make_event_row(
            context=make_full_context(asset="DOGE", spot_pair="DOGE/USD", futures_symbol="PF_DOGEUSD"),
            asset="DOGE", setup_type="REVERSAL",
        )
        text_a = build_prompt_text(row_a)
        text_b = build_prompt_text(row_b)
        self.assertNotEqual(text_a, text_b)
        self.assertIn(row_a["event_id"], text_a)
        self.assertNotIn(row_a["event_id"], text_b)
        self.assertIn("PEPE", text_a)
        self.assertIn("DOGE", text_b)


class TestPromptSecurity(_StoreTestCase):
    def test_no_credential_markers_present(self):
        row = self._make_event_row(context=make_full_context())
        text = build_prompt_text(row)
        for marker in ("KRAKEN_API_KEY", "KRAKEN_SECRET", "api_key", "secret", "password", "Authorization"):
            self.assertNotIn(marker, text)

    def test_a_leaked_sensitive_key_is_redacted_defensively(self):
        context = make_full_context()
        context["l1_features"]["api_key"] = "sk-should-never-appear"
        context["router"]["auth_token"] = "should-never-appear-either"
        row = self._make_event_row(context=context)
        text = build_prompt_text(row)
        self.assertNotIn("sk-should-never-appear", text)
        self.assertNotIn("should-never-appear-either", text)
        self.assertIn("REDACTED", text)

    def test_prompt_only_instructs_analysis_never_execution(self):
        row = self._make_event_row(context=make_full_context())
        text = build_prompt_text(row)
        self.assertIn("Não executar nenhuma ordem", text)
        self.assertIn("Não cancelar ordens", text)
        self.assertIn("Não alterar leverage", text)
        self.assertIn("Não transferir fundos", text)


class TestUnicodeAndLargeEvents(_StoreTestCase):
    def test_unicode_and_special_characters_survive(self):
        context = make_full_context(qwen_review=FakeQwenReview())
        context["qwen"]["reason"] = "razão com acentuação — 100% ✅ \U0001F680 <tag> & \"quotes\""
        row = self._make_event_row(context=context)
        text = build_prompt_text(row)
        self.assertIn("razão com acentuação", text)
        self.assertIn("\U0001F680", text)
        parsed = json.loads(text.split("DADOS DO RADAR\n" + "=" * 50 + "\n\n", 1)[1].split("\n\n" + "=" * 50, 1)[0])
        self.assertIn("quotes", parsed["qwen"]["reason"])

    def test_large_event_does_not_crash_and_stays_valid_json(self):
        context = make_full_context()
        context["l2_features"]["huge_series"] = list(range(20_000))
        context["setup_notes"] = [f"note-{i}" for i in range(5_000)]
        row = self._make_event_row(context=context)
        text = build_prompt_text(row)
        self.assertGreater(len(text), 100_000)
        data_block = text.split("DADOS DO RADAR\n" + "=" * 50 + "\n\n", 1)[1].split("\n\n" + "=" * 50, 1)[0]
        parsed = json.loads(data_block)
        self.assertEqual(len(parsed["l2_features"]["huge_series"]), 20_000)


if __name__ == "__main__":
    unittest.main()
