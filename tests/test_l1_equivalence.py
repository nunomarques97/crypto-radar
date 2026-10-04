"""L1SPEED: the windowed L1 series and the per-cycle BTC cache return exactly what the
full scans did. The oracle is ``tests/l1_reference.py``, a verbatim copy of the
pre-change functions. Floats are compared through ``float.hex`` (bit-for-bit)."""

import math
import os
import random
import sys
import tempfile
import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import l1_reference as ref  # noqa: E402

from radar_v08 import anomaly, config  # noqa: E402
from radar_v08.store import SnapshotStore, SpotSnapshotInput  # noqa: E402

NOW = datetime(2026, 9, 26, 12, 0, 0, 123456, tzinfo=timezone.utc)
CYCLE_TIMES = (NOW, NOW - timedelta(hours=7, seconds=13), NOW - timedelta(hours=26, microseconds=77))
BTC_PAIRS = ("XXBTZUSD", "XXBTZEUR")


def canon(value):
    """Floats as float.hex, containers recursively, everything else by repr."""
    if isinstance(value, float):
        return "f:" + value.hex()
    if isinstance(value, (list, tuple)):
        return [canon(v) for v in value]
    return repr(value)


def result_record(result):
    record = {f.name: canon(getattr(result, f.name)) for f in fields(result) if f.name != "features"}
    record["features"] = {f.name: canon(getattr(result.features, f.name)) for f in fields(result.features)}
    return record


def iso(dt, keep_micros=True):
    return (dt if keep_micros else dt.replace(microsecond=0)).isoformat()


def snap(asset, pair, ts, last, volume, trades):
    quote = pair[-3:]
    return SpotSnapshotInput(
        asset=asset, pair=pair, quote=quote, ts=ts, last=last,
        bid=None if last is None else last * 0.999, ask=None if last is None else last * 1.001,
        bid_size=2.0, ask_size=3.0, volume_today=volume, volume_24h=None if volume is None else volume * 4,
        vwap_today=last, vwap_24h=last, trades_today=trades, trades_24h=None if trades is None else trades * 4,
        high_today=last, low_today=last, high_24h=last, low_24h=last, open_today=last, status="online",
    )


def build_history(rng, start, end, *, dense=(), gaps=(), resets=(), ties_at=(), duplicates=0, invalid_every=0):
    """Rows for one pair: jittered ~1-minute cadence inside ``dense`` spans and ~10-minute
    cadence elsewhere (keeps the quadratic reference fast), with and without microseconds,
    gaps longer than the tolerance, counter resets, exact ties and invalid prices."""
    rows = []
    t = start
    price = rng.uniform(0.5, 500.0)
    volume = rng.uniform(0, 1e5)
    trades = rng.randrange(0, 5000)
    step = 0
    while t < end:
        if any(g0 <= t < g1 for g0, g1 in gaps):
            t = next(g1 for g0, g1 in gaps if g0 <= t < g1)
            continue
        step += 1
        price *= 1.0 + rng.gauss(0, 0.004)
        volume += rng.uniform(0, 400)
        trades += rng.randrange(0, 40)
        if any(r0 <= t < r0 + timedelta(minutes=1) for r0 in resets):
            volume = rng.uniform(0, 50)
            trades = rng.randrange(0, 5)
        last = price
        if invalid_every and step % invalid_every == 0:
            last = rng.choice([None, 0.0, -price, 0])
        vol = None if invalid_every and step % (invalid_every + 3) == 0 else volume
        trd = None if invalid_every and step % (invalid_every + 5) == 0 else trades
        rows.append((iso(t, keep_micros=step % 4 != 0), last, vol, trd))
        steps = [55, 60, 60, 60, 61, 65, 90] if any(d0 <= t < d1 for d0, d1 in dense) else [540, 600, 600, 601, 660]
        t += timedelta(seconds=rng.choice(steps), microseconds=rng.randrange(0, 1_000_000))
    for anchor in ties_at:
        # Two rows exactly d before and after (anchor - 15m): an exact-distance tie for the anchor row.
        d = timedelta(seconds=rng.choice([10, 30, 120]), microseconds=rng.choice([0, 250_000]))
        base = anchor - timedelta(minutes=15)
        for dt in (base - d, base + d, anchor):
            rows.append((iso(dt), rng.uniform(0.5, 500.0), rng.uniform(0, 1e5), rng.randrange(0, 5000)))
    for _ in range(duplicates):
        ts, last, vol, trd = rows[rng.randrange(len(rows))]
        rows.append((ts, None if last is None else last * rng.uniform(0.9, 1.1), vol, trd))
    return rows


class StoreEquivalence(unittest.TestCase):
    """compute_features + compute_anomaly + heartbeat's ranking, reference vs live, on real stores."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(self.path)
        self.store = SnapshotStore(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    def _populate(self, seed):
        rng = random.Random(seed)
        start = NOW - timedelta(hours=50)
        pairs = {"BTC": BTC_PAIRS, "ETH": ("XETHZUSD", "XETHZEUR"), "SOL": ("SOLUSD", "SOLEUR"),
                 "DOGE": ("XDGUSD",), "NEW": ("NEWUSD",), "ODD": ("ODDUSD", "ODDEUR")}
        inputs = []
        for asset, asset_pairs in pairs.items():
            for pair in asset_pairs:
                if asset == "NEW":
                    rows = build_history(rng, NOW - timedelta(minutes=8), NOW)  # warm-up
                else:
                    rows = build_history(
                        rng, start, NOW,
                        dense=[(now - timedelta(hours=1, minutes=30), now) for now in CYCLE_TIMES],
                        gaps=[(NOW - timedelta(hours=30), NOW - timedelta(hours=29, minutes=20)),
                              (NOW - timedelta(hours=3, minutes=20), NOW - timedelta(hours=3))],
                        resets=[NOW - timedelta(hours=12), NOW - timedelta(hours=36)],
                        ties_at=[NOW - timedelta(hours=rng.uniform(1, 40)) for _ in range(4)] + [NOW],
                        duplicates=5,
                        invalid_every=0 if asset == "BTC" else rng.choice([7, 11, 13]),
                    )
                for ts, last, vol, trd in rows:
                    inputs.append(snap(asset, pair, ts, last, vol, trd))
        rng.shuffle(inputs)
        self.store.insert_spot_snapshots_batch(inputs)
        return pairs

    def _cycle(self, module, pairs, now, btc_pair, cache):
        btc_15 = btc_1h = None
        if btc_pair is not None:
            btc_last = 30_000.0
            btc_15 = module.compute_return(btc_last, module.lookup_past_spot(self.store, btc_pair, now, 15))
            btc_1h = module.compute_return(btc_last, module.lookup_past_spot(self.store, btc_pair, now, 60))
        extra = {"btc_cache": {}} if cache else {}
        by_asset = {}
        for asset, asset_pairs in pairs.items():
            pair = asset_pairs[0]
            features = module.compute_features(
                store=self.store, asset=asset, pair=pair, now_dt=now, current_last=101.5,
                current_volume_today=2_000.0, current_trades_today=900, current_spread_bps=4.0,
                current_bid=101.0, current_bid_size=1.5, current_ask=102.0, current_ask_size=2.5,
                btc_return_15m=btc_15, btc_return_1h=btc_1h,
            )
            by_asset[asset] = module.compute_anomaly(self.store, asset, pair, now, features, btc_pair, **extra)
        ranked = sorted(by_asset, key=lambda a: (by_asset[a].anomaly_score is None, -(by_asset[a].anomaly_score or 0)))
        return {a: result_record(r) for a, r in by_asset.items()}, ranked[: config.ANOMALY_SHORTLIST_SIZE]

    def test_cycles_match_reference_bit_for_bit(self):
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                self.tearDown()
                self.setUp()
                pairs = self._populate(seed)
                for now in CYCLE_TIMES:
                    for btc_pair in ("XXBTZUSD", "XXBTZEUR", "MISSINGUSD", None):
                        expected = self._cycle(ref, pairs, now, btc_pair, cache=False)
                        self.assertEqual(self._cycle(anomaly, pairs, now, btc_pair, cache=False), expected)
                        self.assertEqual(self._cycle(anomaly, pairs, now, btc_pair, cache=True), expected)
                records, _ = expected
                # the scenarios really reach the relative-BTC and warm-up branches
                self.assertTrue(records["NEW"]["warmup"] == "True")
                self.assertTrue(any(r["relative_btc_z"] != "None" for r in self._cycle(ref, pairs, NOW, "XXBTZUSD", False)[0].values()))

    def test_btc_history_is_fetched_once_per_cache(self):
        pairs = self._populate(9)
        calls = []
        original = self.store.spot_history_by_pair

        def counting(pair, since_ts):
            calls.append(pair)
            return original(pair, since_ts)

        self.store.spot_history_by_pair = counting
        self._cycle(ref, pairs, NOW, "XXBTZUSD", cache=False)
        per_asset = calls.count("XXBTZUSD")
        self.assertGreater(per_asset, 2)  # BTC's own history + one fetch per non-warm-up asset
        calls.clear()
        self._cycle(anomaly, pairs, NOW, "XXBTZUSD", cache=False)
        self.assertEqual(calls.count("XXBTZUSD"), per_asset)
        calls.clear()
        self._cycle(anomaly, pairs, NOW, "XXBTZUSD", cache=True)
        self.assertEqual(calls.count("XXBTZUSD"), 2)  # BTC's own history + one shared fetch

    def test_cache_is_keyed_by_pair_and_lookback(self):
        pairs = self._populate(4)
        cache = {}
        features = anomaly.Features(relative_return_vs_btc_15m=0.5)
        for now in (NOW, NOW - timedelta(hours=5)):
            for btc_pair in BTC_PAIRS:
                shared = anomaly.compute_anomaly(self.store, "ETH", "XETHZUSD", now, features, btc_pair, btc_cache=cache)
                expected = ref.compute_anomaly(self.store, "ETH", "XETHZUSD", now, features, btc_pair)
                self.assertEqual(result_record(shared), result_record(expected))
        self.assertEqual(len(cache), 4)
        self.assertTrue(pairs)


def rows_of(items):
    return [{"ts": ts, "last": last, "volume_today": vol, "trades_today": trd} for ts, last, vol, trd in items]


def series_outputs(module, rows, btc_rows):
    out = {}
    for horizon in (1, 5, 15, 60):
        for name, call in (
            ("return", lambda h: module.historical_return_series(rows, h)),
            ("observations", lambda h: module._historical_return_observations(rows, h)),
            ("volume", lambda h: module.historical_delta_series(rows, "volume_today", h)),
            ("trades", lambda h: module.historical_delta_series(rows, "trades_today", h)),
            ("relative", lambda h: module.historical_relative_btc_series(rows, btc_rows, h)),
        ):
            try:
                out[(name, horizon)] = canon(call(horizon))
            except Exception as exc:  # the same failure is part of the contract
                out[(name, horizon)] = ("raised", type(exc).__name__)
    return out


class SeriesEquivalence(unittest.TestCase):
    """The four series functions on hand-built and fuzzed row lists."""

    def assertSame(self, rows, btc_rows):
        self.assertEqual(series_outputs(anomaly, rows, btc_rows), series_outputs(ref, rows, btc_rows))

    def test_exact_tie_goes_to_earliest_row(self):
        t = NOW
        rows = rows_of([
            (iso(t - timedelta(minutes=15, seconds=20)), 100.0, 10.0, 1),
            (iso(t - timedelta(minutes=14, seconds=40)), 200.0, 30.0, 3),
            (iso(t), 110.0, 50.0, 7),
        ])
        self.assertEqual(anomaly.historical_return_series(rows, 15), [(110.0 / 100.0 - 1.0) * 100.0])
        self.assertEqual(anomaly.historical_delta_series(rows, "volume_today", 15), [40.0])
        self.assertSame(rows, rows)

    def test_tolerance_boundary_at_the_microsecond(self):
        t = NOW
        tolerance = timedelta(seconds=15 * 60 * config.LOOKUP_TOLERANCE_FRACTION)
        for offset in (tolerance, tolerance + timedelta(microseconds=1), tolerance - timedelta(microseconds=1)):
            for sign in (1, -1):
                rows = rows_of([
                    (iso(t - timedelta(minutes=15) + sign * offset), 100.0, 10.0, 1),
                    (iso(t), 101.0, 12.0, 2),
                ])
                with self.subTest(offset=offset, sign=sign):
                    self.assertSame(rows, rows_of([(iso(t - timedelta(minutes=15) + sign * offset), 1.0, 1.0, 1),
                                                   (iso(t + sign * offset), 1.01, 1.0, 1)]))
        inside = rows_of([(iso(t - timedelta(minutes=15) - tolerance), 100.0, 1.0, 1), (iso(t), 101.0, 2.0, 2)])
        outside = rows_of([(iso(t - timedelta(minutes=15) - tolerance - timedelta(microseconds=1)), 100.0, 1.0, 1),
                           (iso(t), 101.0, 2.0, 2)])
        self.assertEqual(len(anomaly.historical_return_series(inside, 15)), 1)
        self.assertEqual(anomaly.historical_return_series(outside, 15), [])

    def test_invalid_nearest_price_is_picked_then_dropped_only_in_return_series(self):
        t = NOW
        rows = rows_of([
            (iso(t - timedelta(minutes=15, seconds=60)), 100.0, 10.0, 1),
            (iso(t - timedelta(minutes=15)), 0.0, None, 2),
            (iso(t), 110.0, 30.0, 3),
        ])
        self.assertEqual(anomaly.historical_return_series(rows, 15), [])
        self.assertEqual(len(anomaly._historical_return_observations(rows, 15)), 1)
        self.assertEqual(anomaly.historical_delta_series(rows, "volume_today", 15), [20.0])
        self.assertSame(rows, rows)

    def test_reset_deltas_are_dropped(self):
        t = NOW
        rows = rows_of([(iso(t - timedelta(minutes=15)), 1.0, 900.0, 90), (iso(t), 1.0, 5.0, 100)])
        self.assertEqual(anomaly.historical_delta_series(rows, "volume_today", 15), [])
        self.assertEqual(anomaly.historical_delta_series(rows, "trades_today", 15), [10])
        self.assertSame(rows, rows)

    def test_fallback_inputs_behave_like_the_full_scan(self):
        t = NOW
        ordered = [(t + timedelta(minutes=m), 100.0 + m, 10.0 * m, m) for m in range(0, 40, 3)]
        unsorted = rows_of([(iso(a), b, c, d) for a, b, c, d in reversed(ordered)])
        naive = rows_of([(iso(a.replace(tzinfo=None)), b, c, d) for a, b, c, d in ordered])
        offset = rows_of([(iso(a.astimezone(timezone(timedelta(hours=1 if i % 2 else -3)))), b, c, d)
                          for i, (a, b, c, d) in enumerate(ordered)])
        mixed = naive[:5] + rows_of([(iso(a), b, c, d) for a, b, c, d in ordered[5:]])
        texty = rows_of([(iso(a), "1.5" if i == 3 else b, "x" if i == 4 else c, d) for i, (a, b, c, d) in enumerate(ordered)])
        with_nan = rows_of([(iso(a), math.nan if i == 6 else b, c, d) for i, (a, b, c, d) in enumerate(ordered)])
        aware = rows_of([(iso(a), b, c, d) for a, b, c, d in ordered])
        for rows in (unsorted, naive, offset, mixed, texty, with_nan, aware, []):
            for btc_rows in (aware, naive, offset, []):
                self.assertSame(rows, btc_rows)
        self.assertEqual(series_outputs(anomaly, mixed, aware)[("return", 15)], ("raised", "TypeError"))

    def test_fuzzed_lists_with_ties_duplicates_and_invalid_values(self):
        rng = random.Random(20260926)
        for case in range(250):
            n = rng.randrange(0, 60)
            grid = rng.choice([30, 60, 150, 300, 450])
            base = NOW - timedelta(hours=3)
            times = sorted(base + timedelta(seconds=grid * rng.randrange(0, 120),
                                            microseconds=rng.choice([0, 0, 1, 500_000, 999_999]))
                           for _ in range(n))
            items = [(iso(ts, keep_micros=rng.random() < 0.7),
                      rng.choice([None, 0.0, -1.0, rng.uniform(1, 10), rng.uniform(1, 10)]),
                      rng.choice([None, rng.uniform(0, 1000), rng.uniform(0, 1000)]),
                      rng.choice([None, rng.randrange(0, 300), rng.randrange(0, 300)])) for ts in times]
            btc_items = [(iso(base + timedelta(seconds=grid * k)), rng.uniform(1, 2), 1.0, 1) for k in range(rng.randrange(0, 80))]
            with self.subTest(case=case):
                self.assertSame(rows_of(items), rows_of(btc_items))


if __name__ == "__main__":
    unittest.main()
