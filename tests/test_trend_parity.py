"""Parity of the trend research port with the 2026-10-03 feasibility references.

* robust.py: every cell of the vendored robust_results.json (BTCUSDT and ETHUSDT x full, 2022+
  and last 12 months x B&H, ENS and ENS_VT at 0.1% and 0.4%), recomputed on the vendored rows with
  the constant-weight model robust.py uses.
* harness: every logged summary of bh_btc, ens_btc, ens_vt_btc, btc_trend5 and btc_trend5_vt in the
  imported registry (dev and holdout, both fees), recomputed with the realistic units model on the
  vendored rows up to 2026-10-02 (the harness data end), with the DSR recomputed from the registry
  prefix in force when each event was written. Seq 4-6 were logged before the harness adopted the
  1/T variance floor and are recomputed without it.
* the typed ports are bound to the registered sources by sha256.

No network (socket connections are refused for the whole module) and no file outside the repository.
"""

import hashlib
import json
import math
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from radar_v08.adapters import trend_registry_store as store
from radar_v08.domain import trend_engine as E
from radar_v08.domain import trend_registry as R
from radar_v08.domain import trend_strategies as S
from radar_v08.domain.trend_metrics import performance

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "trend"
AUDIT_DIR = REPOSITORY_ROOT / "docs" / "audit" / "2026-10-03-trend-feasibility"
TOLERANCE = 1e-9
FIXTURE_BUDGET_BYTES = 400 * 1024
HARNESS_DATA_END = date(2026, 10, 2)
PARTIAL_DAY = date(2026, 10, 3)
ROBUST_SLICES = (("full", None), ("2022", date(2022, 1, 1)), ("last12m", date(2025, 10, 3)))
ROBUST_METRICS = ("cagr", "mdd", "sharpe", "pos12m", "turnover_yr", "avg_exposure")
FEES = (0.001, 0.004)
# seq -> (name, split, slippage bps) of every logged result of a ported rule
HARNESS_EVENTS = {
    4: ("bh_btc", "dev", 0.0), 5: ("ens_btc", "dev", 0.0), 6: ("ens_vt_btc", "dev", 0.0),
    7: ("bh_btc", "dev", 0.0), 8: ("ens_btc", "dev", 0.0), 9: ("ens_vt_btc", "dev", 0.0),
    11: ("bh_btc", "holdout", 0.0), 13: ("ens_btc", "holdout", 0.0), 15: ("ens_vt_btc", "holdout", 0.0),
    22: ("btc_trend5", "dev", 5.0), 23: ("btc_trend5_vt", "dev", 5.0),
    29: ("btc_trend5", "holdout", 5.0), 31: ("btc_trend5_vt", "holdout", 5.0),
}
PRE_FLOOR_SEQS = frozenset({4, 5, 6})

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


def load_rows(symbol):
    return json.loads((FIXTURES / f"spot1d_{symbol}.json").read_text(encoding="ascii"))


def close_enough(ours, theirs):
    if isinstance(theirs, float) and math.isnan(theirs):
        return math.isnan(ours)
    return abs(ours - theirs) <= TOLERANCE


class Fixtures(unittest.TestCase):
    def test_fixture_manifest_hashes_size_and_range(self):
        manifest = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
        listed = {entry["path"]: entry for entry in manifest["files"]}
        on_disk = {p.name for p in FIXTURES.iterdir() if p.is_file()} - {"MANIFEST.json"}
        self.assertEqual(set(listed), on_disk)
        for name, entry in listed.items():
            data = (FIXTURES / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"], name)
            self.assertEqual(len(data), entry["bytes"], name)
        total = sum(p.stat().st_size for p in FIXTURES.iterdir() if p.is_file())
        self.assertLessEqual(total, FIXTURE_BUDGET_BYTES)
        for symbol in ("BTCUSDT", "ETHUSDT"):
            bars = E.bars_from_rows(load_rows(symbol))
            self.assertEqual(E.utc_day(bars[0].open_time_ms), date(2017, 8, 17))
            self.assertEqual(E.utc_day(bars[-1].open_time_ms), PARTIAL_DAY)
            self.assertEqual(len(bars), (PARTIAL_DAY - date(2017, 8, 17)).days + 1)  # no gap
        self.assertIn("never used", manifest["partial_last_row"])


class RobustParity(unittest.TestCase):
    """Constant-weight model on the full vendored rows: the partial 2026-10-03 row only supplies its open."""

    @classmethod
    def setUpClass(cls):
        cls.expected = json.loads((FIXTURES / "robust_results.json").read_text(encoding="utf-8"))
        cls.panels = {s: E.Panel({s: E.bars_from_rows(load_rows(s))}, [s]) for s in ("BTCUSDT", "ETHUSDT")}

    def _cell(self, rule, symbol, start, fee):
        sim = E.simulate(rule, self.panels[symbol], E.Window(start, None), fee, accounting=E.Accounting.CONSTANT_WEIGHT)
        self.assertEqual(sim.days[-1], date(2026, 10, 2))
        return performance(sim.rets, sim.days, sim.turnover, sim.exposure_sum)

    def test_every_robust_cell_matches(self):
        checked = 0
        for symbol in ("BTCUSDT", "ETHUSDT"):
            for label, start in ROBUST_SLICES:
                cells = {"B&H": (S.BuyAndHold(symbol), 0.0)}
                for fee in FEES:
                    cells[f"ENS@{fee}"] = (S.Ens(symbol), fee)
                    cells[f"ENS_VT@{fee}"] = (S.Ens(symbol, S.ENS_VOL_TARGET), fee)
                expected = self.expected[f"{symbol}_{label}"]
                self.assertEqual(set(expected), set(cells))
                for cell, (rule, fee) in cells.items():
                    ours = self._cell(rule, symbol, start, fee)
                    for metric in ROBUST_METRICS:
                        if cell == "B&H" and metric in ("turnover_yr", "avg_exposure"):
                            self.assertNotIn(metric, expected[cell])
                            continue
                        theirs = expected[cell][metric]
                        with self.subTest(symbol=symbol, slice=label, cell=cell, metric=metric):
                            self.assertTrue(close_enough(getattr(ours, metric), theirs), (getattr(ours, metric), theirs))
                        checked += 1
        self.assertEqual(checked, 2 * 3 * (4 + 4 * 6))
        self.assertTrue(math.isnan(self.expected["BTCUSDT_last12m"]["B&H"]["pos12m"]))


class HarnessParity(unittest.TestCase):
    """Units model on rows up to 2026-10-02; DSR from the registry prefix in force at each event."""

    @classmethod
    def setUpClass(cls):
        rows = [r for r in load_rows("BTCUSDT") if E.utc_day(r[0]) <= HARNESS_DATA_END]
        cls.panel = E.Panel({"BTCUSDT": E.bars_from_rows(rows)}, ["BTCUSDT"])
        cls.registry = store.load_imported_records(AUDIT_DIR).registry

    def test_every_logged_summary_of_a_ported_rule_is_reproduced(self):
        logged = {
            ev.seq for ev in self.registry.events
            if ev.kind in ("run", "holdout_result") and ev.name in S.REGISTERED
        }
        self.assertEqual(logged, set(HARNESS_EVENTS))
        for seq, (name, split, slippage) in sorted(HARNESS_EVENTS.items()):
            event = self.registry.events[seq]
            self.assertEqual((event.name, event.payload["split"], event.payload["slippage_bps"]), (name, split, slippage))
            rule = S.ported_rule(name)
            self.assertEqual(event.sha256, S.registration_of(rule).sha256)
            prefix = self.registry.events[:seq]
            self.assertEqual(R.trial_count(prefix), event.payload["n_trials"])
            for fee in FEES:
                evaluation = E.evaluate(rule, self.panel, E.SPLITS[split], fee, slippage)
                ours = evaluation.summary()
                dsr = R.deflated_sharpe_at(prefix, name, split, fee, evaluation.pre_tax, floor=seq not in PRE_FLOOR_SEQS)
                ours["dsr"] = dsr.dsr
                theirs = event.payload["summary"][R.fee_key(fee)]
                self.assertEqual(set(theirs), set(ours))
                for metric, value in theirs.items():
                    with self.subTest(seq=seq, name=name, fee=fee, metric=metric):
                        self.assertTrue(close_enough(ours[metric], value), (ours[metric], value))
                logged_sharpe = event.payload["daily_sharpe"][R.fee_key(fee)]
                self.assertTrue(close_enough(evaluation.pre_tax.daily_sharpe, logged_sharpe))

    def test_holdout_pos12m_is_nan_and_windows_end_before_the_partial_day(self):
        holdout = E.evaluate(S.BtcTrend5(), self.panel, E.HOLDOUT, 0.001, 5.0)
        self.assertTrue(math.isnan(holdout.pre_tax.pos12m))
        self.assertEqual((holdout.pre_tax.start, holdout.pre_tax.end), (date(2025, 10, 3), date(2026, 10, 1)))
        dev = E.evaluate(S.BtcTrend5(), self.panel, E.DEV, 0.001, 5.0)
        self.assertEqual((dev.pre_tax.start, dev.pre_tax.end), (date(2018, 3, 6), date(2025, 10, 2)))
        self.assertTrue(all(E.utc_day(b.open_time_ms) <= HARNESS_DATA_END for b in self.panel.rows["BTCUSDT"]))

    def test_ported_rules_pass_the_lookahead_audit_on_the_real_series(self):
        for name in S.REGISTERED:
            rule = S.ported_rule(name)
            for split in ("dev", "holdout"):
                E.audit_lookahead(rule, self.panel, self.panel.window_days(E.SPLITS[split], rule.warmup_days))


class SourceBinding(unittest.TestCase):
    def test_vendored_sources_hash_to_the_registered_sha256(self):
        records = store.load_imported_records(AUDIT_DIR)
        expected = {
            "ens_btc": "d56a80fd", "ens_vt_btc": "094bb5b9", "btc_trend5": "a96640ff", "btc_trend5_vt": "28b1082d",
            "bh_btc": "d9a2ebc2",
        }
        self.assertEqual(set(S.REGISTERED), set(expected))
        for name, prefix in expected.items():
            source = (AUDIT_DIR / "strategy_sources" / f"{name}.py.txt").read_bytes()
            digest = hashlib.sha256(source).hexdigest()
            registration = records.registry.registration_for(name)
            self.assertTrue(digest.startswith(prefix), name)
            self.assertEqual(digest, registration.sha256, name)
            self.assertEqual(digest, S.REGISTERED[name].sha256, name)
            self.assertEqual(S.registration_of(S.ported_rule(name)), S.REGISTERED[name])
            self.assertEqual(records.sources[name], source)
            self.assertIn(f'NAME = "{name}"'.encode(), source)


if __name__ == "__main__":
    unittest.main()
