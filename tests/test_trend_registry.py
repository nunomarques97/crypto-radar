"""Pre-registration registry of the trend research: hash chain, refusals, holdout-once, trial
count, DSR variance floor, and the imported historical registry (read-only).

Registries under test are temporary files. The imported records under
docs/audit/2026-10-03-trend-feasibility/ are only read; the tests prove their bytes are unchanged
and that no event can be appended to them. Socket connections are refused for the whole module.
"""

import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from radar_v08.adapters import trend_registry_store as store
from radar_v08.domain import trend_registry as R
from radar_v08.domain.trend_metrics import performance
from radar_v08.domain.trend_strategies import REGISTERED

AUDIT_DIR = Path(__file__).resolve().parent.parent / "docs" / "audit" / "2026-10-03-trend-feasibility"
IMPORTED_NAMES = (
    "bh_btc", "ens_btc", "ens_vt_btc", "btc_trend5", "btc_trend5_vt", "bh_vt", "btc_usdc_6040", "dated_carry",
    "xs_mom10",
)
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_patches = []


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
        patcher = mock.patch(target, _refuse_network)
        patcher.start()
        _patches.append(patcher)


def tearDownModule():
    while _patches:
        _patches.pop().stop()


def clock():
    return datetime(2026, 10, 3, 20, 0, 0, tzinfo=UTC)


def sha(data):
    return hashlib.sha256(data).hexdigest()


SOURCE_A = b'NAME = "rule_a"\n'
SOURCE_B = b'NAME = "rule_b"\n'


class TempRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trend-registry-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "registry.jsonl"

    def register(self, name="rule_a", source=SOURCE_A):
        return store.register(
            self.path, name=name, source=source, file=f"strategies/{name}.py", rule="hold X", params={"n": 1},
            pass_criteria=[{"metric": "sharpe", "op": ">=", "value": 0.0}], universe=["XUSDT"], clock=clock,
        )

    def populated(self):
        store.append_event(self.path, R.prior_trials_payload(5, "before the harness"), clock=clock)
        self.register()
        self.register("rule_b", SOURCE_B)
        return self.path.read_bytes()

    def assertCode(self, code, fn):
        with self.assertRaises(R.RegistryError) as caught:
            fn()
        self.assertIs(caught.exception.code, code)


class HashChain(TempRegistry):
    def test_chain_is_verified_over_the_exact_line_bytes(self):
        data = self.populated()
        registry = R.parse_registry(data)
        lines = data.split(b"\n")[:-1]
        self.assertEqual([ev.seq for ev in registry.events], [0, 1, 2])
        self.assertEqual([ev.line_sha256 for ev in registry.events], [sha(line) for line in lines])
        self.assertEqual(json.loads(lines[0])["prev"], "genesis")
        self.assertEqual(json.loads(lines[2])["prev"], sha(lines[1]))
        self.assertEqual(registry.tip, sha(lines[2]))
        # CRLF terminators (as in the imported registry) give the same line hashes
        crlf = R.parse_registry(data.replace(b"\n", b"\r\n"))
        self.assertEqual([ev.line_sha256 for ev in crlf.events], [ev.line_sha256 for ev in registry.events])

    def test_appended_line_reads_back_identically(self):
        self.populated()
        event = store.append_event(self.path, R.prior_trials_payload(1, "x"), clock=clock)
        self.assertEqual(store.read_registry(self.path).events[-1], event)
        self.assertEqual(event.payload["ts"], "2026-10-03T20:00:00+00:00")

    def test_edited_line_is_refused(self):
        lines = self.populated().split(b"\n")
        lines[1] = lines[1].replace(b"hold X", b"hold Y")
        self.assertCode(R.RegistryErrorCode.CHAIN_BROKEN, lambda: R.parse_registry(b"\n".join(lines)))
        lines = self.populated_lines()
        lines[0] = lines[0].replace(b'"count": 5', b'"count": 50')
        self.assertCode(R.RegistryErrorCode.CHAIN_BROKEN, lambda: R.parse_registry(b"\n".join(lines)))

    def populated_lines(self):
        return self.path.read_bytes().split(b"\n")

    def test_removed_line_is_refused(self):
        lines = self.populated().split(b"\n")
        for drop in (0, 1):
            kept = lines[:drop] + lines[drop + 1:]
            self.assertCode(R.RegistryErrorCode.CHAIN_BROKEN, lambda kept=kept: R.parse_registry(b"\n".join(kept)))

    def test_reordered_lines_are_refused(self):
        lines = self.populated().split(b"\n")
        swapped = [lines[1], lines[0], *lines[2:]]
        self.assertCode(R.RegistryErrorCode.CHAIN_BROKEN, lambda: R.parse_registry(b"\n".join(swapped)))
        swapped = [lines[0], lines[2], lines[1], *lines[3:]]
        self.assertCode(R.RegistryErrorCode.CHAIN_BROKEN, lambda: R.parse_registry(b"\n".join(swapped)))

    def test_blank_torn_and_garbage_lines_are_refused(self):
        data = self.populated()
        self.assertCode(R.RegistryErrorCode.MALFORMED, lambda: R.parse_registry(data[:-1]))  # torn: no terminator
        self.assertCode(R.RegistryErrorCode.MALFORMED, lambda: R.parse_registry(data + b"\n"))
        self.assertCode(R.RegistryErrorCode.MALFORMED, lambda: R.parse_registry(data + b"not json\n"))
        self.assertCode(R.RegistryErrorCode.MALFORMED, lambda: R.parse_registry(data + b"[1]\n"))
        self.assertCode(R.RegistryErrorCode.MALFORMED, lambda: R.parse_registry(data + b"\xff\n"))

    def test_a_refused_file_is_never_appended_to(self):
        data = self.populated()
        tampered = data.replace(b"hold X", b"hold Y", 1)
        self.path.write_bytes(tampered)
        self.assertCode(R.RegistryErrorCode.CHAIN_BROKEN,
                        lambda: store.append_event(self.path, R.prior_trials_payload(1, "x"), clock=clock))
        self.assertEqual(self.path.read_bytes(), tampered)

    def test_payload_cannot_set_chain_fields_or_unknown_events(self):
        registry = R.parse_registry(self.populated())
        self.assertCode(R.RegistryErrorCode.INVALID_EVENT,
                        lambda: R.encode_event(registry, "t", {"event": "prior_trials", "count": 1, "seq": 9}))
        self.assertCode(R.RegistryErrorCode.UNKNOWN_EVENT, lambda: R.encode_event(registry, "t", {"event": "edit"}))
        self.assertCode(R.RegistryErrorCode.INVALID_EVENT,
                        lambda: R.encode_event(registry, "t", {"event": "prior_trials", "count": -1}))
        self.assertCode(R.RegistryErrorCode.INVALID_EVENT, lambda: R.encode_event(
            registry, "t", {"event": "holdout", "name": "rule_a", "sha256": sha(SOURCE_A), "slippage_bps": float("nan")}))


class RegistrationPolicy(TempRegistry):
    def test_duplicate_name_is_refused(self):
        self.register()
        before = self.path.read_bytes()
        self.assertCode(R.RegistryErrorCode.DUPLICATE_NAME, lambda: self.register("rule_a", SOURCE_A + b"# tweak\n"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_identical_source_hash_is_refused(self):
        self.register()
        self.assertCode(R.RegistryErrorCode.DUPLICATE_HASH, lambda: self.register("rule_copy", SOURCE_A))

    def test_changed_or_unregistered_source_is_refused(self):
        self.register()
        registry = store.read_registry(self.path)
        self.assertEqual(R.verify_source(registry, "rule_a", sha(SOURCE_A)).name, "rule_a")
        self.assertCode(R.RegistryErrorCode.SOURCE_CHANGED, lambda: R.verify_source(registry, "rule_a", sha(SOURCE_B)))
        self.assertCode(R.RegistryErrorCode.NOT_REGISTERED, lambda: R.verify_source(registry, "rule_z", sha(SOURCE_A)))
        before = self.path.read_bytes()
        self.assertCode(R.RegistryErrorCode.SOURCE_CHANGED, lambda: store.begin_holdout(
            self.path, name="rule_a", source=SOURCE_A + b"# edited\n", slippage_bps=0.0, clock=clock))
        self.assertEqual(self.path.read_bytes(), before)

    def test_trial_count_is_registrations_plus_prior_trials(self):
        self.populated()
        store.append_event(self.path, R.prior_trials_payload(3, "more"), clock=clock)
        registry = store.read_registry(self.path)
        self.assertEqual(registry.trial_count(), 2 + 5 + 3)
        # runs and holdouts do not add trials
        store.begin_holdout(self.path, name="rule_a", source=SOURCE_A, slippage_bps=0.0, clock=clock)
        self.assertEqual(store.read_registry(self.path).trial_count(), 10)


class HoldoutOnce(TempRegistry):
    def test_holdout_is_recorded_before_compute_and_a_crash_still_consumes_it(self):
        self.register()
        observed = []

        def crashing_compute():
            last = store.read_registry(self.path).events[-1]
            observed.append((last.kind, last.name))
            raise RuntimeError("crash during the holdout computation")

        with self.assertRaises(RuntimeError):
            store.run_holdout(self.path, name="rule_a", source=SOURCE_A, slippage_bps=5.0, compute=crashing_compute,
                              clock=clock)
        self.assertEqual(observed, [("holdout", "rule_a")])
        after_crash = self.path.read_bytes()
        self.assertTrue(store.read_registry(self.path).holdout_used("rule_a"))

        calls = []
        self.assertCode(R.RegistryErrorCode.HOLDOUT_CONSUMED, lambda: store.run_holdout(
            self.path, name="rule_a", source=SOURCE_A, slippage_bps=5.0, compute=lambda: calls.append(1), clock=clock))
        self.assertEqual(calls, [])
        self.assertEqual(self.path.read_bytes(), after_crash)

    def test_successful_holdout_returns_the_result_once(self):
        self.register()
        self.assertEqual(store.run_holdout(self.path, name="rule_a", source=SOURCE_A, slippage_bps=0.0,
                                           compute=lambda: 42, clock=clock), 42)
        self.assertCode(R.RegistryErrorCode.HOLDOUT_CONSUMED, lambda: store.begin_holdout(
            self.path, name="rule_a", source=SOURCE_A, slippage_bps=0.0, clock=clock))


class StoreConcurrency(TempRegistry):
    def test_a_second_writer_is_busy_and_writes_nothing(self):
        before = self.populated()
        lock = self.path.with_name(self.path.name + ".lock")
        lock.write_bytes(b"")
        with self.assertRaises(store.RegistryStoreError) as caught:
            store.append_event(self.path, R.prior_trials_payload(1, "x"), clock=clock)
        self.assertIs(caught.exception.code, store.RegistryStoreErrorCode.BUSY)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(lock.exists())  # never taken over automatically
        lock.unlink()
        store.append_event(self.path, R.prior_trials_payload(1, "x"), clock=clock)
        self.assertFalse(lock.exists())


class DsrFromRegistry(TempRegistry):
    def test_variance_floor_and_trial_count_come_from_the_prefix(self):
        self.populated()
        rets = [0.01, -0.005, 0.002, 0.0] * 50
        perf = performance(rets, [datetime(2025, 1, 1).date()] * len(rets))
        events = store.read_registry(self.path).events
        dev = R.deflated_sharpe_at(events, "rule_a", "dev", 0.001, perf)
        self.assertEqual(dev.n_trials, 7)
        self.assertEqual(dev.n_trial_sharpes, 1)
        self.assertEqual(dev.variance, 1 / 200)  # one Sharpe: observed variance 0, floored at 1/T
        unfloored = R.deflated_sharpe_at(events, "rule_a", "dev", 0.001, perf, floor=False)
        self.assertEqual(unfloored.variance, 0.0)
        self.assertEqual(unfloored.hurdle_sharpe_annual, 0.0)
        self.assertGreater(unfloored.dsr, dev.dsr)
        holdout = R.deflated_sharpe_at(events, "rule_a", "holdout", 0.001, perf)
        self.assertEqual(holdout.n_trial_sharpes, 0)


class ImportedRegistry(unittest.TestCase):
    def setUp(self):
        self.registry_path = AUDIT_DIR / "registry.jsonl"
        self.before = self.registry_path.read_bytes()
        self.addCleanup(self.assert_unchanged)

    def assert_unchanged(self):
        after = self.registry_path.read_bytes()
        self.assertEqual(after, self.before)
        self.assertEqual(sha(after), R.IMPORTED_REGISTRY_SHA256)
        self.assertFalse(self.registry_path.with_name("registry.jsonl.lock").exists())

    def test_imported_registry_is_intact(self):
        records = store.load_imported_records(AUDIT_DIR)
        registry = records.registry
        self.assertEqual(sha(records.registry_bytes), "6f4dbaba82e628a97911a93381a694ffc4dbc4059e064a0647564c77232e5f4a")
        self.assertEqual(len(registry.events), 40)
        self.assertEqual(registry.trial_count(), 29)
        self.assertEqual(sum(ev.kind == "prior_trials" for ev in registry.events), 1)
        self.assertEqual(sorted(registry.registered_names()), sorted(IMPORTED_NAMES))
        for name in IMPORTED_NAMES:
            self.assertTrue(registry.holdout_used(name), name)
        self.assertEqual(sha(records.paper_ledger_bytes), EMPTY_SHA256)
        self.assertEqual(set(records.sources), set(REGISTERED))

    def test_no_holdout_can_be_authorized_for_any_imported_name(self):
        registry = store.load_imported_records(AUDIT_DIR).registry
        for name in IMPORTED_NAMES:
            digest = registry.registration_for(name).sha256
            with self.assertRaises(R.RegistryError) as caught:
                R.holdout_payload(registry, name, digest, 0.0)
            self.assertIs(caught.exception.code, R.RegistryErrorCode.HOLDOUT_CONSUMED, name)
        for name in REGISTERED:
            source = (AUDIT_DIR / "strategy_sources" / f"{name}.py.txt").read_bytes()
            with self.assertRaises(R.RegistryError) as caught:
                store.begin_holdout(self.registry_path, name=name, source=source, slippage_bps=0.0, clock=clock)
            self.assertIs(caught.exception.code, R.RegistryErrorCode.HOLDOUT_CONSUMED, name)

    def test_nothing_can_be_appended_to_the_imported_registry(self):
        # Refused before the lock is taken: no lock file is ever created in the versioned audit dir.
        with mock.patch.object(store, "_Lock", side_effect=AssertionError("lock taken")) as lock:
            with self.assertRaises(store.RegistryStoreError) as caught:
                store.append_event(self.registry_path, R.prior_trials_payload(1, "x"), clock=clock)
        self.assertIs(caught.exception.code, store.RegistryStoreErrorCode.READ_ONLY_HISTORY)
        lock.assert_not_called()
        with tempfile.TemporaryDirectory(prefix="trend-imported-") as tmp:  # a copy is read-only history too
            copy = Path(tmp) / "registry.jsonl"
            copy.write_bytes(self.before)
            with self.assertRaises(store.RegistryStoreError) as caught:
                store.register(copy, name="new_rule", source=b"x", file="f", rule="r", params={}, pass_criteria=[],
                               universe=["BTCUSDT"], clock=clock)
            self.assertIs(caught.exception.code, store.RegistryStoreErrorCode.READ_ONLY_HISTORY)
            self.assertEqual(copy.read_bytes(), self.before)

    def test_manifest_lists_every_file_with_hashes_and_no_private_paths(self):
        text = (AUDIT_DIR / "MANIFEST.json").read_text(encoding="utf-8")
        manifest = json.loads(text)
        datetime.fromisoformat(manifest["imported_at_utc"])
        listed = {entry["path"] for entry in manifest["files"]}
        on_disk = {p.relative_to(AUDIT_DIR).as_posix() for p in AUDIT_DIR.rglob("*") if p.is_file()} - {"MANIFEST.json"}
        self.assertEqual(listed, on_disk)
        for marker in (":\\", ":/", "Users", "/home/", "\\\\"):
            self.assertNotIn(marker, text)
        for name in REGISTERED:
            self.assertFalse((AUDIT_DIR / "strategy_sources" / f"{name}.py").exists())

    def test_a_changed_import_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="trend-imported-") as tmp:
            copy = Path(tmp) / "audit"
            shutil.copytree(AUDIT_DIR, copy)
            store.load_imported_records(copy)
            source = copy / "strategy_sources" / "btc_trend5.py.txt"
            source.write_bytes(source.read_bytes() + b"\n")
            with self.assertRaises(store.RegistryStoreError) as caught:
                store.load_imported_records(copy)
            self.assertIs(caught.exception.code, store.RegistryStoreErrorCode.IMPORT_CHANGED)


if __name__ == "__main__":
    unittest.main()
