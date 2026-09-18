import json
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config
from radar_v08.adapters.evidence_store import LinkedEvidence
from radar_v08.context_builder import (
    UNAVAILABLE,
    ContextEvidenceRejected,
    build_event_context,
    build_model_context,
    build_verified_model_context,
    deep_mark_unavailable,
)
from radar_v08.domain.evidence import (
    EvidenceRejected,
    EvidenceScope,
    FactKind,
    LegacyUnversionedEvidence,
    RejectionCode,
    make_fact,
    seal_evidence,
)
from radar_v08.domain.integrity import (
    Capability,
    CapabilityResult,
    CheckStatus,
    InstrumentId,
    InstrumentKind,
    IntegrityReport,
    TimeBasis,
)
from radar_v08.events import create_event_if_new
from radar_v08.store import SnapshotStore

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
BTC_USD = InstrumentId("kraken", "XBT/USD", InstrumentKind.SPOT, "BTC", "USD", "BTC")
BTC_EUR = InstrumentId("kraken", "XBT/EUR", InstrumentKind.SPOT, "BTC", "EUR", "BTC")


def sealed_evidence(run_id="run-1", instrument=BTC_USD):
    scope = EvidenceScope(run_id=run_id, instrument=instrument, code_version="radar-0.8.0+test")
    ticker = make_fact(scope, FactKind.OBSERVATION, "ticker", {"bid": Decimal("100.10"), "ask": Decimal("100.5")})
    spread = make_fact(
        scope, FactKind.CALCULATION, "spread_bps", {"value": Decimal("39.88")},
        depends_on=(ticker.fact_id,), calculation_version="spread-v1",
    )
    result = CapabilityResult(
        Capability.SPOT_TICKER, instrument.symbol, CheckStatus.PASS, (), TimeBasis.RECEIPT_ONLY, NOW - timedelta(seconds=5)
    )
    report = IntegrityReport(evaluated_at=NOW, policy_version="OC-1", results=(result,))
    return seal_evidence(scope, (ticker, spread), report, NOW + timedelta(seconds=1))


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


class TestEvidenceVerifiedContext(unittest.TestCase):
    """T030b: the builder refuses evidence whose run/instrument/hash does not match (typed)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "fixture.sqlite")
        self.store = SnapshotStore(self.path)
        self._original_events_log_path = config.EVENTS_LOG_PATH
        config.EVENTS_LOG_PATH = os.path.join(self.tmp.name, "events.jsonl")
        self.evidence = sealed_evidence()
        self.store.save_evidence(self.evidence)
        self.event_id = self._event("BTC")

    def tearDown(self):
        self.store.close()
        config.EVENTS_LOG_PATH = self._original_events_log_path

    def _event(self, asset):
        event_id, _ = create_event_if_new(
            self.store, ts="2026-09-18T12:00:00+00:00", type_="RADAR_ALERT", asset=asset,
            setup_type="BREAKOUT", direction="LONG", market="SPOT",
            anomaly_score=70.0, opportunity_score=80.0, tradeability_score=85.0,
            confidence="HIGH", model_demand="FABLE", reason="test", status="PENDING",
            context={"asset": asset, "market": "SPOT", "current_price": 50000.0},
        )
        return event_id

    def _raw_link(self, event_id, evidence_id, run_id="run-1", instrument=BTC_USD):
        # Writes a link row directly, bypassing the adapter's write-time check, as a
        # corrupted or foreign writer could; the builder must still refuse it.
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "INSERT INTO event_evidence (event_id, evidence_id, run_id, venue, symbol, instrument_kind, base, "
                "quote, size_unit, linked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, evidence_id, run_id, instrument.venue, instrument.symbol, instrument.kind.value,
                 instrument.base, instrument.quote, instrument.size_unit, "2026-09-18T12:00:02+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def _assert_rejected(self, code, field=None):
        with self.assertRaises(ContextEvidenceRejected) as caught:
            build_verified_model_context(self.store, self.event_id)
        self.assertIsInstance(caught.exception, EvidenceRejected)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.event_id, self.event_id)
        if field is not None:
            self.assertEqual(caught.exception.field, field)

    def test_matching_evidence_builds_context_with_sealed_block(self):
        self.store.link_event_evidence(self.event_id, self.evidence.evidence_id, "run-1", BTC_USD)
        context = build_verified_model_context(self.store, self.event_id)
        self.assertEqual(context["asset"], "BTC")
        self.assertEqual(context["current_price"], 50000.0)
        block = context["evidence"]
        self.assertEqual(block["state"], "sealed")
        self.assertTrue(block["citable"])
        self.assertEqual(block["evidence_id"], self.evidence.evidence_id)
        self.assertEqual(block["content_hash"], self.evidence.content_hash)
        self.assertEqual(block["run_id"], "run-1")
        self.assertEqual(block["instrument"]["symbol"], "XBT/USD")
        self.assertEqual(sorted(f["fact_id"] for f in block["facts"]), sorted(self.evidence.fact_ids))
        self.assertEqual(json.loads(json.dumps(context))["evidence"]["evidence_id"], self.evidence.evidence_id)

    def test_run_mismatch_is_rejected(self):
        self._raw_link(self.event_id, self.evidence.evidence_id, run_id="run-OTHER")
        self._assert_rejected(RejectionCode.RUN_MISMATCH, "run_id")

    def test_instrument_mismatch_is_rejected(self):
        self._raw_link(self.event_id, self.evidence.evidence_id, instrument=BTC_EUR)
        self._assert_rejected(RejectionCode.INSTRUMENT_MISMATCH, "instrument")

    def test_evidence_for_another_asset_is_rejected(self):
        self.event_id = self._event("ETH")
        self._raw_link(self.event_id, self.evidence.evidence_id)
        self._assert_rejected(RejectionCode.INSTRUMENT_MISMATCH, "event.asset")

    def test_missing_evidence_is_rejected(self):
        self._raw_link(self.event_id, "evidence:sha256:" + "a" * 64)
        self._assert_rejected(RejectionCode.UNKNOWN_DEPENDENCY, "evidence_id")

    def test_tampered_stored_evidence_is_rejected_as_hash_mismatch(self):
        self._raw_link(self.event_id, self.evidence.evidence_id)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DROP TRIGGER evidence_versions_immutable")
            conn.execute("UPDATE evidence_versions SET record_json = replace(record_json, '39.88', '39.99')")
            conn.commit()
        finally:
            conn.close()
        self._assert_rejected(RejectionCode.HASH_MISMATCH)

    def test_link_hash_that_does_not_match_the_record_is_rejected(self):
        self.store.link_event_evidence(self.event_id, self.evidence.evidence_id, "run-1", BTC_USD)
        linked = self.store.load_event_evidence(self.event_id)
        # Same run and instrument, different content: only the hash-bound id tells them apart.
        different = self._different_content_evidence()
        self.assertEqual((different.run_id, different.instrument), ("run-1", BTC_USD))
        swapped = LinkedEvidence(linked.link, different)
        row = self.store.get_event(self.event_id)
        with self.assertRaises(ContextEvidenceRejected) as caught:
            build_model_context(row, evidence=swapped)
        self.assertEqual(caught.exception.code, RejectionCode.HASH_MISMATCH)
        self.assertEqual(caught.exception.field, "evidence_id")

    def _different_content_evidence(self):
        scope = self.evidence.scope
        fact = make_fact(scope, FactKind.OBSERVATION, "ticker", {"bid": Decimal("1"), "ask": Decimal("2")})
        return seal_evidence(scope, (fact,), self.evidence.integrity, self.evidence.sealed_at)

    def test_evidence_of_another_event_is_rejected(self):
        self.store.link_event_evidence(self.event_id, self.evidence.evidence_id, "run-1", BTC_USD)
        linked = self.store.load_event_evidence(self.event_id)
        row = self.store.get_event(self._event("SOL"))
        with self.assertRaises(ContextEvidenceRejected) as caught:
            build_model_context(row, evidence=linked)
        self.assertEqual(caught.exception.code, RejectionCode.INVALID_FIELD)
        self.assertEqual(caught.exception.field, "event_id")
        with self.assertRaises(ContextEvidenceRejected) as caught:
            build_model_context(row, evidence=LegacyUnversionedEvidence(source_ref=f"events:{self.event_id}"))
        self.assertEqual(caught.exception.field, "evidence.source_ref")

    def test_unlinked_event_is_labelled_legacy_unversioned_not_empty(self):
        context = build_verified_model_context(self.store, self.event_id)
        self.assertEqual(context["asset"], "BTC")
        self.assertEqual(context["current_price"], 50000.0)
        block = context["evidence"]
        self.assertEqual(block["state"], "legacy-unversioned")
        self.assertFalse(block["citable"])
        self.assertEqual(block["source_ref"], f"events:{self.event_id}")
        for invented in ("facts", "content_hash", "evidence_id", "run_id", "integrity"):
            self.assertNotIn(invented, block)

    def test_unknown_event_is_rejected(self):
        self.event_id = "no-such-event"
        self._assert_rejected(RejectionCode.UNKNOWN_DEPENDENCY, "event_id")

    def test_context_without_evidence_argument_is_unchanged(self):
        row = self.store.get_event(self.event_id)
        self.assertNotIn("evidence", build_model_context(row))


if __name__ == "__main__":
    unittest.main()
