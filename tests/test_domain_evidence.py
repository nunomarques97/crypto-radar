"""Pure tests for radar_v08.domain.evidence (the domain part of versioned evidence).

No SQLite, no files, no network, no wall clock: fixtures are built in memory with an
explicit ``NOW``. Every rejection is asserted by its typed ``RejectionCode``.
"""

import ast
import hashlib
import json
import os
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.domain import evidence as ev
from radar_v08.domain.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceRejected,
    EvidenceScope,
    EvidenceVersionState,
    FactKind,
    LegacyUnversionedEvidence,
    RejectionCode,
    SealedEvidence,
    canonical_hash,
    canonical_json,
    evidence_from_json,
    evidence_from_record,
    evidence_to_json,
    evidence_to_record,
    make_fact,
    parse_canonical_json,
    resolve_fact,
    seal_evidence,
)
from radar_v08.domain.integrity import (
    Capability,
    CapabilityResult,
    CheckStatus,
    ClockSample,
    InstrumentId,
    InstrumentKind,
    IntegrityReport,
    Reason,
    ReasonCode,
    SourceTiming,
    TickerObservation,
    TimeBasis,
    TradingStatus,
    evaluate_clock,
    evaluate_ticker,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
BTC_USD = InstrumentId("kraken", "XBT/USD", InstrumentKind.SPOT, "BTC", "USD", "BTC")
BTC_EUR = InstrumentId("kraken", "XBT/EUR", InstrumentKind.SPOT, "BTC", "EUR", "BTC")
ETH_USD = InstrumentId("kraken", "ETH/USD", InstrumentKind.SPOT, "ETH", "USD", "ETH")
HEX64 = "0123456789abcdef" * 4


def scope(run_id="run-1", instrument=BTC_USD, code_version="radar-0.8.0+abc123", **extra):
    return EvidenceScope(run_id=run_id, instrument=instrument, code_version=code_version, **extra)


def report(evaluated_at=NOW, subject="XBT/USD", policy_version="OC-1", extra=()):
    ticker = CapabilityResult(
        Capability.SPOT_TICKER, subject, CheckStatus.PASS, (), TimeBasis.RECEIPT_ONLY, NOW - timedelta(seconds=10)
    )
    clock = CapabilityResult(Capability.CLOCK, "utc", CheckStatus.PASS, (), TimeBasis.NONE)
    return IntegrityReport(evaluated_at=evaluated_at, policy_version=policy_version, results=(ticker, *extra, clock))


def stale_book(subject="XBT/USD", detail="age 20.0s > 15.0s"):
    return CapabilityResult(
        Capability.BOOK,
        subject,
        CheckStatus.FAIL,
        (Reason(ReasonCode.STALE_RECEIPT, "timing.received_at", detail),),
        TimeBasis.RECEIPT_ONLY,
        NOW - timedelta(seconds=20),
    )


def facts_for(sc):
    ticker = make_fact(
        sc,
        FactKind.OBSERVATION,
        "ticker",
        {"bid": Decimal("100.10"), "ask": Decimal("100.5"), "unit": "USD", "received_at": NOW - timedelta(seconds=10)},
    )
    spread = make_fact(
        sc,
        FactKind.CALCULATION,
        "spread_bps",
        {"value": Decimal("39.88"), "window": (1, 2, 3)},
        depends_on=(ticker.fact_id,),
        calculation_version="spread-v1",
    )
    return ticker, spread


def sealed(sc=None, integrity=None, sealed_at=NOW + timedelta(seconds=1)):
    sc = sc or scope()
    return seal_evidence(sc, facts_for(sc), integrity or report(), sealed_at)


class RejectsWith:
    """Mixin: assert that a call raises EvidenceRejected with exactly this code."""

    def assertRejected(self, code, call, *args, **kwargs):
        with self.assertRaises(EvidenceRejected) as caught:
            call(*args, **kwargs)
        self.assertIs(caught.exception.code, code, str(caught.exception))
        return caught.exception


class TestCanonicalJson(RejectsWith, unittest.TestCase):
    def test_keys_sorted_compact_ascii(self):
        self.assertEqual(canonical_json({"b": 1, "a": "é", "c": [True, None]}), '{"a":"\\u00e9","b":1,"c":[true,null]}')

    def test_hash_is_sha256_of_canonical_bytes(self):
        expected = "sha256:" + hashlib.sha256(b'{"a":1,"b":"x"}').hexdigest()
        self.assertEqual(canonical_hash({"b": "x", "a": 1}), expected)

    def test_same_input_same_hash_and_mapping_order_irrelevant(self):
        first = {"price": Decimal("1.5"), "at": NOW, "tags": ("a", "b")}
        second = {"tags": ["a", "b"], "at": NOW, "price": Decimal("1.5")}
        self.assertEqual(canonical_hash(first), canonical_hash(second))

    def test_floats_are_rejected_everywhere(self):
        for value in (1.5, 1.0, 0.0, {"a": 0.1}, (Decimal("1"), 2.5)):
            with self.subTest(value=value):
                self.assertRejected(RejectionCode.NON_CANONICAL_NUMBER, canonical_json, value)

    def test_non_finite_decimals_are_rejected(self):
        for text in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(text=text):
                self.assertRejected(RejectionCode.NON_FINITE_NUMBER, canonical_json, Decimal(text))

    def test_decimal_text_is_exact_and_unambiguous(self):
        self.assertEqual(canonical_json(Decimal("1.50")), '{"$decimal":"1.5"}')
        self.assertEqual(canonical_json(Decimal("1.50")), canonical_json(Decimal("1.5")))
        self.assertEqual(canonical_json(Decimal("-0.000")), '{"$decimal":"0"}')
        self.assertEqual(canonical_json(Decimal("100")), '{"$decimal":"1E+2"}')
        self.assertEqual(canonical_json(Decimal("0.00012")), '{"$decimal":"0.00012"}')
        long_value = "1.2345678901234567890123456789012345"  # 35 digits, beyond the 28-digit context
        self.assertEqual(canonical_json(Decimal(long_value)), '{"$decimal":"%s"}' % long_value)

    def test_decimal_int_and_string_hash_differently(self):
        hashes = {canonical_hash(Decimal("1")), canonical_hash(1), canonical_hash("1"), canonical_hash(True)}
        self.assertEqual(len(hashes), 4)

    def test_datetimes_are_utc_microsecond_text(self):
        plus_one = datetime(2026, 9, 18, 13, 0, 0, 5, tzinfo=timezone(timedelta(hours=1)))
        self.assertEqual(canonical_json(plus_one), '{"$datetime":"2026-09-18T12:00:00.000005Z"}')
        self.assertEqual(canonical_hash(plus_one), canonical_hash(NOW + timedelta(microseconds=5)))

    def test_naive_datetime_is_rejected(self):
        self.assertRejected(RejectionCode.NAIVE_TIMESTAMP, canonical_json, {"at": datetime(2026, 9, 18, 12)})

    def test_int_range_is_signed_64_bit(self):
        self.assertEqual(canonical_json(2**63 - 1), str(2**63 - 1))
        self.assertEqual(canonical_json(-(2**63)), str(-(2**63)))
        self.assertRejected(RejectionCode.INT_OUT_OF_RANGE, canonical_json, 2**63)
        self.assertRejected(RejectionCode.INT_OUT_OF_RANGE, canonical_json, -(2**63) - 1)

    def test_unsupported_types_are_rejected(self):
        for value in ({1, 2}, b"raw", date(2026, 9, 18), object(), FactKind.OBSERVATION):
            with self.subTest(value=value):
                self.assertRejected(RejectionCode.UNSUPPORTED_TYPE, canonical_json, value)

    def test_invalid_keys_are_rejected(self):
        self.assertRejected(RejectionCode.INVALID_KEY, canonical_json, {1: "a"})
        self.assertRejected(RejectionCode.INVALID_KEY, canonical_json, {"$decimal": "1"})

    def test_parse_rejects_floats_constants_duplicates_and_garbage(self):
        self.assertRejected(RejectionCode.NON_CANONICAL_NUMBER, parse_canonical_json, '{"a":1.5}')
        self.assertRejected(RejectionCode.NON_CANONICAL_NUMBER, parse_canonical_json, "[1e3]")
        self.assertRejected(RejectionCode.NON_FINITE_NUMBER, parse_canonical_json, "[NaN]")
        self.assertRejected(RejectionCode.NON_FINITE_NUMBER, parse_canonical_json, '{"$decimal":"Infinity"}')
        self.assertRejected(RejectionCode.INVALID_KEY, parse_canonical_json, '{"a":1,"a":2}')
        self.assertRejected(RejectionCode.INVALID_KEY, parse_canonical_json, '{"$other":"1"}')
        self.assertRejected(RejectionCode.INVALID_KEY, parse_canonical_json, '{"$decimal":"1","b":2}')
        self.assertRejected(RejectionCode.MALFORMED_RECORD, parse_canonical_json, "{not json")
        self.assertRejected(RejectionCode.MALFORMED_RECORD, parse_canonical_json, '{"$decimal":"abc"}')
        self.assertRejected(RejectionCode.NAIVE_TIMESTAMP, parse_canonical_json, '{"$datetime":"2026-09-18T12:00:00"}')

    def test_parse_round_trip_restores_exact_values(self):
        value = {"p": Decimal("123.4500"), "at": NOW, "n": 7, "flag": False, "nested": [{"x": None}]}
        text = canonical_json(value)
        parsed = parse_canonical_json(text)
        self.assertEqual(parsed, {"p": Decimal("123.45"), "at": NOW, "n": 7, "flag": False, "nested": ({"x": None},)})
        self.assertIsInstance(parsed["p"], Decimal)
        self.assertEqual(canonical_json(parsed), text)


class TestFacts(RejectsWith, unittest.TestCase):
    def test_same_input_same_fact_id(self):
        first, second = facts_for(scope()), facts_for(scope())
        self.assertEqual(first, second)
        self.assertRegex(first[0].fact_id, r"^fact:sha256:[0-9a-f]{64}$")

    def test_every_fact_field_changes_the_id(self):
        sc = scope()
        base = make_fact(sc, FactKind.CALCULATION, "atr", {"v": Decimal("2")}, ("fact:a",), "atr-v1")
        variants = {
            "run_id": make_fact(scope(run_id="run-2"), FactKind.CALCULATION, "atr", {"v": Decimal("2")}, ("fact:a",), "atr-v1"),
            "kind": make_fact(sc, FactKind.OBSERVATION, "atr", {"v": Decimal("2")}, ("fact:a",)),
            "name": make_fact(sc, FactKind.CALCULATION, "atr14", {"v": Decimal("2")}, ("fact:a",), "atr-v1"),
            "payload": make_fact(sc, FactKind.CALCULATION, "atr", {"v": Decimal("2.1")}, ("fact:a",), "atr-v1"),
            "depends_on": make_fact(sc, FactKind.CALCULATION, "atr", {"v": Decimal("2")}, ("fact:b",), "atr-v1"),
            "no_depends_on": make_fact(sc, FactKind.CALCULATION, "atr", {"v": Decimal("2")}, (), "atr-v1"),
            "calculation_version": make_fact(sc, FactKind.CALCULATION, "atr", {"v": Decimal("2")}, ("fact:a",), "atr-v2"),
        }
        for field in ("venue", "symbol", "base", "quote", "size_unit"):
            other = replace(BTC_USD, **{field: getattr(BTC_USD, field) + "X"})
            variants[f"instrument.{field}"] = make_fact(
                scope(instrument=other), FactKind.CALCULATION, "atr", {"v": Decimal("2")}, ("fact:a",), "atr-v1"
            )
        variants["instrument.kind"] = make_fact(
            scope(instrument=replace(BTC_USD, kind=InstrumentKind.FUTURES)),
            FactKind.CALCULATION,
            "atr",
            {"v": Decimal("2")},
            ("fact:a",),
            "atr-v1",
        )
        for field, variant in variants.items():
            with self.subTest(field=field):
                self.assertNotEqual(variant.fact_id, base.fact_id)
        self.assertEqual(len({variant.fact_id for variant in variants.values()}), len(variants))

    def test_code_version_is_not_part_of_fact_identity(self):
        # A fact is what was observed/calculated; the deploying code version belongs to the seal.
        self.assertEqual(facts_for(scope(code_version="a")), facts_for(scope(code_version="b")))

    def test_dependency_order_is_irrelevant_and_duplicates_rejected(self):
        sc = scope()
        forward = make_fact(sc, FactKind.CALCULATION, "x", 1, ("fact:b", "fact:a"), "v1")
        backward = make_fact(sc, FactKind.CALCULATION, "x", 1, ("fact:a", "fact:b"), "v1")
        self.assertEqual(forward.fact_id, backward.fact_id)
        self.assertEqual(forward.depends_on, ("fact:a", "fact:b"))
        self.assertRejected(RejectionCode.INVALID_FIELD, make_fact, sc, FactKind.CALCULATION, "x", 1, ("fact:a", "fact:a"), "v1")

    def test_calculation_version_rules(self):
        sc = scope()
        self.assertRejected(RejectionCode.INVALID_FIELD, make_fact, sc, FactKind.CALCULATION, "x", 1)
        self.assertRejected(RejectionCode.INVALID_FIELD, make_fact, sc, FactKind.OBSERVATION, "x", 1, (), "v1")

    def test_float_payload_is_rejected(self):
        self.assertRejected(RejectionCode.NON_CANONICAL_NUMBER, make_fact, scope(), FactKind.OBSERVATION, "x", {"p": 1.5})

    def test_forged_or_stale_fact_id_is_a_hash_mismatch(self):
        fact = facts_for(scope())[0]
        self.assertRejected(RejectionCode.HASH_MISMATCH, replace, fact, fact_id="fact:sha256:" + HEX64)
        self.assertRejected(RejectionCode.HASH_MISMATCH, replace, fact, name="book")
        self.assertRejected(RejectionCode.HASH_MISMATCH, replace, fact, payload_json='{"bid":{"$decimal":"1"}}')
        self.assertRejected(RejectionCode.HASH_MISMATCH, replace, fact, run_id="run-2")

    def test_non_canonical_payload_text_is_rejected(self):
        fact = facts_for(scope())[0]
        self.assertRejected(RejectionCode.INVALID_FIELD, replace, fact, payload_json='{"b": 1, "a": 2}')

    def test_fact_is_frozen_and_payload_decodes(self):
        fact = facts_for(scope())[0]
        with self.assertRaises(FrozenInstanceError):
            fact.name = "other"
        self.assertEqual(fact.payload["bid"], Decimal("100.1"))
        self.assertEqual(fact.payload["received_at"], NOW - timedelta(seconds=10))


class TestScope(RejectsWith, unittest.TestCase):
    def test_schema_version_is_separate_from_code_version(self):
        sc = scope(code_version="radar-0.8.0+abc123")
        self.assertEqual(sc.schema_version, EVIDENCE_SCHEMA_VERSION)
        self.assertEqual(sc.code_version, "radar-0.8.0+abc123")
        self.assertEqual(sc.venue, "kraken")

    def test_invalid_scope_fields(self):
        self.assertRejected(RejectionCode.INVALID_FIELD, scope, run_id="")
        self.assertRejected(RejectionCode.INVALID_FIELD, scope, code_version="")
        self.assertRejected(RejectionCode.INVALID_FIELD, scope, instrument=replace(BTC_USD, venue=""))
        self.assertRejected(RejectionCode.INVALID_FIELD, scope, instrument="XBT/USD")
        self.assertRejected(RejectionCode.SCHEMA_VERSION_UNSUPPORTED, scope, schema_version="evidence-v9")


class TestSealing(RejectsWith, unittest.TestCase):
    def test_same_input_same_hash_and_id_bound_to_hash(self):
        first, second = sealed(), sealed()
        self.assertEqual(first, second)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertRegex(first.content_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first.evidence_id, "evidence:" + first.content_hash)
        self.assertIs(first.state, EvidenceVersionState.SEALED)
        self.assertEqual((first.run_id, first.venue, first.instrument), ("run-1", "kraken", BTC_USD))

    def test_hash_is_sha256_of_the_record_without_its_ids(self):
        evidence = sealed()
        record = evidence_to_record(evidence)
        del record["content_hash"], record["evidence_id"]
        self.assertEqual(evidence.content_hash, canonical_hash(record))

    def test_fact_order_does_not_change_the_hash(self):
        sc = scope()
        ticker, spread = facts_for(sc)
        self.assertEqual(
            seal_evidence(sc, (ticker, spread), report(), NOW).content_hash,
            seal_evidence(sc, (spread, ticker), report(), NOW).content_hash,
        )

    def test_every_evidence_field_changes_the_hash(self):
        base = sealed()
        sc = scope()
        ticker, spread = facts_for(sc)
        extra = make_fact(sc, FactKind.OBSERVATION, "status", "online")
        variants = {
            "code_version": sealed(sc=scope(code_version="radar-0.8.1")),
            "run_id": sealed(sc=scope(run_id="run-2")),
            "instrument": sealed(sc=scope(instrument=BTC_EUR), integrity=report(subject="XBT/EUR")),
            "sealed_at": sealed(sealed_at=NOW + timedelta(seconds=1, microseconds=1)),
            "facts.added": seal_evidence(sc, (ticker, spread, extra), report(), NOW + timedelta(seconds=1)),
            "facts.removed": seal_evidence(sc, (ticker,), report(), NOW + timedelta(seconds=1)),
            "integrity.evaluated_at": sealed(integrity=report(evaluated_at=NOW - timedelta(microseconds=1))),
            "integrity.policy_version": sealed(integrity=report(policy_version="OC-2")),
            "integrity.result_added": sealed(integrity=report(extra=(stale_book(),))),
            "integrity.reason_detail": sealed(integrity=report(extra=(stale_book(detail="age 21.0s > 15.0s"),))),
        }
        for field, variant in variants.items():
            with self.subTest(field=field):
                self.assertNotEqual(variant.content_hash, base.content_hash)
                self.assertNotEqual(variant.evidence_id, base.evidence_id)
        self.assertEqual(len({variant.content_hash for variant in variants.values()}), len(variants))

    def test_schema_version_is_part_of_both_hashes(self):
        with mock.patch.object(ev, "SUPPORTED_SCHEMA_VERSIONS", frozenset({EVIDENCE_SCHEMA_VERSION, "evidence-v2"})):
            v2 = sealed(sc=scope(schema_version="evidence-v2"))
        v1 = sealed()
        self.assertNotEqual(v2.content_hash, v1.content_hash)
        self.assertNotEqual(set(v2.fact_ids), set(v1.fact_ids))
        self.assertEqual(v2.schema_version, "evidence-v2")
        self.assertEqual(v2.code_version, v1.code_version)

    def test_fact_from_another_schema_version_is_rejected(self):
        with mock.patch.object(ev, "SUPPORTED_SCHEMA_VERSIONS", frozenset({EVIDENCE_SCHEMA_VERSION, "evidence-v2"})):
            v2_facts = facts_for(scope(schema_version="evidence-v2"))
            self.assertRejected(RejectionCode.SCHEMA_VERSION_MISMATCH, seal_evidence, scope(), v2_facts, report(), NOW)

    def test_fact_from_another_run_is_rejected(self):
        foreign = facts_for(scope(run_id="run-2"))[0]
        own = facts_for(scope())
        error = self.assertRejected(RejectionCode.RUN_MISMATCH, seal_evidence, scope(), (*own, foreign), report(), NOW)
        self.assertIn("run-2", error.detail)

    def test_fact_for_another_instrument_is_rejected(self):
        foreign = facts_for(scope(instrument=ETH_USD))[0]
        self.assertRejected(RejectionCode.INSTRUMENT_MISMATCH, seal_evidence, scope(), (foreign,), report(), NOW)
        same_symbol_other_quote = facts_for(scope(instrument=replace(BTC_USD, quote="USDT")))[0]
        self.assertRejected(
            RejectionCode.INSTRUMENT_MISMATCH, seal_evidence, scope(), (same_symbol_other_quote,), report(), NOW
        )

    def test_integrity_for_another_instrument_is_rejected(self):
        self.assertRejected(
            RejectionCode.INSTRUMENT_MISMATCH, seal_evidence, scope(), facts_for(scope()), report(subject="ETH/USD"), NOW
        )

    def test_unknown_dependency_is_rejected(self):
        sc = scope()
        ticker, spread = facts_for(sc)
        error = self.assertRejected(RejectionCode.UNKNOWN_DEPENDENCY, seal_evidence, sc, (spread,), report(), NOW)
        self.assertIn(ticker.fact_id, error.detail)
        dangling = make_fact(sc, FactKind.CALCULATION, "x", 1, ("fact:sha256:" + HEX64,), "v1")
        self.assertRejected(RejectionCode.UNKNOWN_DEPENDENCY, seal_evidence, sc, (ticker, dangling), report(), NOW)

    def test_duplicate_and_empty_facts_are_rejected(self):
        sc = scope()
        ticker, _ = facts_for(sc)
        self.assertRejected(RejectionCode.DUPLICATE_FACT, seal_evidence, sc, (ticker, ticker), report(), NOW)
        self.assertRejected(RejectionCode.INVALID_FIELD, seal_evidence, sc, (), report(), NOW)
        self.assertRejected(RejectionCode.INVALID_FIELD, seal_evidence, sc, ("fact:x",), report(), NOW)

    def test_seal_time_rules(self):
        sc = scope()
        self.assertRejected(RejectionCode.NAIVE_TIMESTAMP, seal_evidence, sc, facts_for(sc), report(), datetime(2026, 9, 18, 12))
        self.assertRejected(
            RejectionCode.INVALID_FIELD, seal_evidence, sc, facts_for(sc), report(), NOW - timedelta(microseconds=1)
        )
        self.assertEqual(seal_evidence(sc, facts_for(sc), report(), NOW).sealed_at, NOW)

    def test_integrity_with_a_naive_timestamp_cannot_be_sealed(self):
        naive = replace(report().results[0], received_at=datetime(2026, 9, 18, 11, 59))
        integrity = IntegrityReport(NOW, "OC-1", (naive,))
        self.assertRejected(RejectionCode.NAIVE_TIMESTAMP, seal_evidence, scope(), facts_for(scope()), integrity, NOW)

    def test_tampered_hash_or_id_is_rejected(self):
        evidence = sealed()
        self.assertRejected(RejectionCode.HASH_MISMATCH, replace, evidence, content_hash="sha256:" + HEX64)
        self.assertRejected(RejectionCode.HASH_MISMATCH, replace, evidence, evidence_id="evidence:sha256:" + HEX64)
        self.assertRejected(RejectionCode.HASH_MISMATCH, replace, evidence, sealed_at=evidence.sealed_at + timedelta(seconds=1))
        self.assertRejected(RejectionCode.HASH_MISMATCH, replace, evidence, scope=scope(code_version="other"))
        with self.assertRaises(FrozenInstanceError):
            evidence.sealed_at = NOW

    def test_failed_integrity_is_recorded_not_hidden(self):
        evidence = sealed(integrity=report(extra=(stale_book(),)))
        book = evidence.integrity.result(Capability.BOOK, "XBT/USD")
        self.assertIs(book.status, CheckStatus.FAIL)
        self.assertTrue(evidence.integrity.opportunity_blocked)


class TestRoundTrip(RejectsWith, unittest.TestCase):
    def test_json_round_trip_is_identical(self):
        evidence = sealed(integrity=report(extra=(stale_book(),)))
        text = evidence_to_json(evidence)
        restored = evidence_from_json(text, source_ref="test:1")
        self.assertIsInstance(restored, SealedEvidence)
        self.assertEqual(restored, evidence)
        self.assertEqual(evidence_to_json(restored), text)
        self.assertEqual(restored.facts[0].payload, evidence.facts[0].payload)

    def test_round_trip_with_real_oc1_results(self):
        ticker = TickerObservation(
            BTC_USD,
            Decimal("100.1"),
            Decimal("100.5"),
            Decimal("100.2"),
            "USD",
            SourceTiming(NOW - timedelta(seconds=5), NOW - timedelta(seconds=6)),
            TradingStatus.ONLINE,
        )
        clock = ClockSample(synchronized=None, offset_uncertainty=None, previous_wall_time=None)
        integrity = IntegrityReport(NOW, "OC-1", (evaluate_ticker(ticker, BTC_USD, NOW), evaluate_clock(clock, NOW)))
        evidence = seal_evidence(scope(), facts_for(scope()), integrity, NOW)
        restored = evidence_from_json(evidence_to_json(evidence), source_ref="test:2")
        self.assertEqual(restored, evidence)
        self.assertIs(restored.integrity.clock.status, CheckStatus.UNKNOWN)
        self.assertEqual(restored.integrity.results[0].source_time, NOW - timedelta(seconds=6))

    def test_tampered_payload_in_json_is_a_hash_mismatch(self):
        text = evidence_to_json(sealed())
        tampered = text.replace('"$decimal":"100.1"', '"$decimal":"100.2"', 1)
        self.assertNotEqual(tampered, text)
        self.assertRejected(RejectionCode.HASH_MISMATCH, evidence_from_json, tampered, source_ref="t")

    def test_tampered_record_fields(self):
        record = evidence_to_record(sealed())
        cases = {
            "sealed_at": (dict(record, sealed_at=NOW + timedelta(seconds=2)), RejectionCode.HASH_MISMATCH),
            "code_version": (dict(record, code_version="other"), RejectionCode.HASH_MISMATCH),
            "evidence_id": (dict(record, evidence_id="evidence:sha256:" + HEX64), RejectionCode.HASH_MISMATCH),
            "run_id": (dict(record, run_id="run-2"), RejectionCode.RUN_MISMATCH),
            "extra_key": (dict(record, note="x"), RejectionCode.MALFORMED_RECORD),
            "schema": (dict(record, schema_version="evidence-v9"), RejectionCode.SCHEMA_VERSION_UNSUPPORTED),
        }
        missing = dict(record)
        del missing["integrity"]
        cases["missing_key"] = (missing, RejectionCode.MALFORMED_RECORD)
        for name, (candidate, code) in cases.items():
            with self.subTest(name=name):
                self.assertRejected(code, evidence_from_record, candidate, source_ref="t")

    def test_facts_swapped_from_other_run_or_instrument_are_rejected(self):
        record = evidence_to_record(sealed())
        other_run = evidence_to_record(sealed(sc=scope(run_id="run-2")))
        other_instrument = evidence_to_record(sealed(sc=scope(instrument=ETH_USD), integrity=report(subject="ETH/USD")))
        self.assertRejected(
            RejectionCode.RUN_MISMATCH, evidence_from_record, dict(record, facts=other_run["facts"]), source_ref="t"
        )
        self.assertRejected(
            RejectionCode.INSTRUMENT_MISMATCH,
            evidence_from_record,
            dict(record, facts=other_instrument["facts"]),
            source_ref="t",
        )

    def test_json_floats_in_a_record_are_rejected(self):
        text = evidence_to_json(sealed()).replace('{"$decimal":"100.1"}', "100.1", 1)
        self.assertRejected(RejectionCode.NON_CANONICAL_NUMBER, evidence_from_json, text, source_ref="t")

    def test_malformed_integrity_status_is_rejected(self):
        record = json.loads(evidence_to_json(sealed()))
        record["integrity"]["results"][0]["status"] = "FAIL"  # FAIL without reasons contradicts OC-1
        self.assertRejected(RejectionCode.MALFORMED_RECORD, evidence_from_json, json.dumps(record), source_ref="t")


class TestLegacyUnversioned(RejectsWith, unittest.TestCase):
    def test_row_without_schema_version_is_legacy_and_keeps_only_what_it_has(self):
        row = {"id": 42, "run_id": "old-run", "symbol": "XBTUSD", "score": Decimal("61.5"), "payload": "{...}"}
        legacy = evidence_from_record(row, source_ref="events:42")
        self.assertIsInstance(legacy, LegacyUnversionedEvidence)
        self.assertIs(legacy.state, EvidenceVersionState.LEGACY_UNVERSIONED)
        self.assertEqual(legacy.source_ref, "events:42")
        self.assertEqual((legacy.run_id, legacy.symbol), ("old-run", "XBTUSD"))
        self.assertEqual(legacy.fields_present, ("id", "payload", "run_id", "score", "symbol"))
        for invented in ("facts", "content_hash", "evidence_id", "integrity", "instrument"):
            self.assertFalse(hasattr(legacy, invented), invented)

    def test_missing_or_non_text_fields_stay_unknown(self):
        legacy = evidence_from_record({"schema_version": None, "run_id": 7, "symbol": ""}, source_ref="alerts:1")
        self.assertIsInstance(legacy, LegacyUnversionedEvidence)
        self.assertIsNone(legacy.run_id)
        self.assertIsNone(legacy.symbol)
        self.assertEqual(evidence_from_record({}, source_ref="alerts:2").fields_present, ())

    def test_legacy_json_row_and_source_ref_required(self):
        legacy = evidence_from_json('{"symbol":"ETHUSD","z":1}', source_ref="events:9")
        self.assertEqual(legacy.symbol, "ETHUSD")
        self.assertRejected(RejectionCode.INVALID_FIELD, evidence_from_record, {}, source_ref="")

    def test_legacy_cannot_be_cited(self):
        legacy = LegacyUnversionedEvidence("events:42", "run-1", "XBT/USD")
        self.assertRejected(
            RejectionCode.LEGACY_UNVERSIONED,
            resolve_fact,
            legacy,
            "fact:sha256:" + HEX64,
            evidence_id="evidence:sha256:" + HEX64,
            run_id="run-1",
            instrument=BTC_USD,
        )


class TestResolveFact(RejectsWith, unittest.TestCase):
    def setUp(self):
        self.evidence = sealed()
        self.ticker = self.evidence.facts[0]

    def resolve(self, fact_id=None, **overrides):
        kwargs = dict(evidence_id=self.evidence.evidence_id, run_id="run-1", instrument=BTC_USD)
        kwargs.update(overrides)
        return resolve_fact(self.evidence, fact_id or self.ticker.fact_id, **kwargs)

    def test_resolves_a_sealed_fact(self):
        self.assertEqual(self.resolve(), self.ticker)

    def test_each_mismatch_has_its_code(self):
        self.assertRejected(RejectionCode.HASH_MISMATCH, self.resolve, evidence_id="evidence:sha256:" + HEX64)
        self.assertRejected(RejectionCode.RUN_MISMATCH, self.resolve, run_id="run-2")
        self.assertRejected(RejectionCode.INSTRUMENT_MISMATCH, self.resolve, instrument=BTC_EUR)
        self.assertRejected(RejectionCode.UNKNOWN_DEPENDENCY, self.resolve, fact_id="fact:sha256:" + HEX64)

    def test_fact_from_another_evidence_version_is_unknown_here(self):
        other = sealed(sealed_at=NOW + timedelta(seconds=5), integrity=report(extra=(stale_book(),)))
        foreign = make_fact(scope(), FactKind.OBSERVATION, "only-elsewhere", 1)
        elsewhere = seal_evidence(scope(), (*other.facts, foreign), other.integrity, other.sealed_at)
        self.assertRejected(RejectionCode.UNKNOWN_DEPENDENCY, self.resolve, fact_id=foreign.fact_id)
        self.assertEqual(
            resolve_fact(elsewhere, foreign.fact_id, evidence_id=elsewhere.evidence_id, run_id="run-1", instrument=BTC_USD),
            foreign,
        )


class TestPurity(unittest.TestCase):
    """Domain module: stdlib plus the integrity domain only, no I/O, no wall clock."""

    def setUp(self):
        with open(ev.__file__, encoding="utf-8") as handle:
            self.source = handle.read()
        self.tree = ast.parse(self.source)

    def test_imports_are_stdlib_pure(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertLessEqual(
            imported,
            {
                "__future__",
                "hashlib",
                "json",
                "collections.abc",
                "dataclasses",
                "datetime",
                "decimal",
                "enum",
                "radar_v08.domain.integrity",
            },
        )

    def test_no_wall_clock_or_io_calls(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                self.assertNotIn(name, {"now", "utcnow", "today", "time", "monotonic", "perf_counter", "open"})
        self.assertNotIn("sqlite", self.source)


if __name__ == "__main__":
    unittest.main()
