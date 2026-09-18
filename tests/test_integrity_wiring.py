"""T023b - OC-1 integrity validator wired before L1/L2/L3/router/Qwen consumption.

Integration tests: `run_heartbeat(full=True)` runs for real over a
GuardedSession whose transport is a fake Kraken (no socket, no network), a
deterministic aware clock, a disposable SQLite store and temporary log
paths. Only the scoring internals that are not under test (L1 anomaly
score, L2 setup/opportunity, L3 tradeability) and the model (a fake Qwen
that counts calls) are replaced, so a valid snapshot can reach the router
and every poisoned source is proven to stop before it.
"""

import functools
import json
import logging
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import urlsplit

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config, heartbeat, l2, l3
from radar_v08.anomaly import AnomalyResult
from radar_v08.domain.integrity import ClockSample, SourceTiming
from radar_v08.http_client import GuardedSession
from radar_v08.opportunity import OpportunityResult
from radar_v08.qwen import QwenBatchResult
from radar_v08.router import (
    RouterContext,
    count_confirmations,
    route,
    valid_taker_buy_ratio,
)
from radar_v08.security import RedirectRefused
from radar_v08.setups import SetupResult
from radar_v08.store import SnapshotStore
from radar_v08.tradeability import TradeabilityResult

UTC = timezone.utc
T0 = datetime(2026, 9, 18, 12, 0, 30, tzinfo=UTC)  # run start; the 12:00 bar is still forming
BAR = timedelta(minutes=5)
FORMING_OPEN = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)

SPOT = "https://api.kraken.com/0/public"

ASSETS = {
    # asset: (spot pair key, wsname, perpetual symbol, futures pair field, price)
    "BTC": ("XXBTZUSD", "XBT/USD", "PF_XBTUSD", "XBT:USD", 60000.0),
    "ETH": ("XETHZUSD", "ETH/USD", "PF_ETHUSD", "ETH:USD", 3000.0),
}


def _iso_z(moment):
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _epoch(moment):
    return (moment - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()


class StepClock:
    """Aware UTC clock that advances 1 ms on every read (thread-safe)."""

    def __init__(self, start=T0, step=timedelta(milliseconds=1)):
        self._now = start
        self._step = step
        self._lock = threading.Lock()
        self.reads = 0

    def __call__(self):
        with self._lock:
            value = self._now
            self._now += self._step
            self.reads += 1
            return value


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = dict(headers or {})
        self.history = []

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _ticker_row(price):
    bid, ask = price - 0.5, price + 0.5
    return {
        "a": [str(ask), "1", "1.000"],
        "b": [str(bid), "1", "1.000"],
        "c": [str(price), "0.1"],
        "v": ["100.0", "1000.0"],
        "p": [str(price), str(price)],
        "t": [500, 5000],
        "l": [str(price * 0.97), str(price * 0.97)],
        "h": [str(price * 1.03), str(price * 1.03)],
        "o": str(price),
    }


def _ohlc_rows(price, closed_bars=60):
    rows = []
    first_open = FORMING_OPEN - closed_bars * BAR
    for index in range(closed_bars + 1):  # + the bar still forming at receipt
        opened = first_open + index * BAR
        rows.append(
            [int(_epoch(opened)), str(price), str(price * 1.001), str(price * 0.999), str(price), str(price), "10.0", 5]
        )
    return rows


def _levels(price, side, count=10, with_time=True):
    sign = -1 if side == "bids" else 1
    levels = []
    for index in range(count):
        level = [str(price + sign * (0.5 + index)), "5.0"]
        if with_time:
            level.append(_epoch(T0 - timedelta(seconds=2)))
        levels.append(level)
    return levels


def _trades(price, buys=16, sells=4):
    rows = []
    for index in range(buys + sells):
        side = "b" if index < buys else "s"
        moment = T0 - timedelta(seconds=3 + index)
        rows.append([str(price), "0.5", _epoch(moment), side, "m", ""])
    return rows


def _futures_row(asset, last_time):
    _pair, _ws, symbol, fut_pair, price = ASSETS[asset]
    return {
        "symbol": symbol,
        "pair": fut_pair,
        "last": price,
        "markPrice": price,
        "indexPrice": price,
        "bid": price - 1.0,
        "ask": price + 1.0,
        "bidSize": 3.0,
        "askSize": 3.0,
        "volumeQuote": 50_000_000.0,
        "openInterest": 1000.0,
        "fundingRate": 0.0001,
        "fundingRatePrediction": 0.0001,
        "open24h": price,
        "lastTime": _iso_z(last_time),
        "suspended": False,
        "postOnly": False,
        "tag": "perpetual",
    }


class FakeKraken:
    """Duck-typed inner requests.Session for GuardedSession: routes each
    allowlisted endpoint to a scripted payload (or response). Records calls."""

    def __init__(self, assets=("BTC",)):
        self.assets = tuple(assets)
        self.calls = []
        self._lock = threading.Lock()
        self.asset_pairs = {
            "error": [],
            "result": {
                ASSETS[a][0]: {"altname": ASSETS[a][0], "wsname": ASSETS[a][1], "status": "online", "aclass_base": "currency"}
                for a in self.assets
            },
        }
        self.ticker = {"error": [], "result": {ASSETS[a][0]: _ticker_row(ASSETS[a][4]) for a in self.assets}}
        self.futures_tickers = {
            "result": "success",
            "serverTime": _iso_z(T0 + timedelta(milliseconds=3)),
            "tickers": [_futures_row(a, T0 - timedelta(seconds=5)) for a in self.assets],
        }
        self.ohlc = {
            ASSETS[a][0]: {"error": [], "result": {ASSETS[a][0]: _ohlc_rows(ASSETS[a][4]), "last": int(_epoch(FORMING_OPEN - BAR))}}
            for a in self.assets
        }
        self.depth = {
            ASSETS[a][0]: {
                "error": [],
                "result": {ASSETS[a][0]: {"bids": _levels(ASSETS[a][4], "bids"), "asks": _levels(ASSETS[a][4], "asks")}},
            }
            for a in self.assets
        }
        self.trades = {
            ASSETS[a][0]: {"error": [], "result": {ASSETS[a][0]: _trades(ASSETS[a][4]), "last": "1"}} for a in self.assets
        }
        self.orderbook = {
            ASSETS[a][2]: {
                "result": "success",
                "serverTime": _iso_z(T0),
                "orderBook": {
                    "bids": [[ASSETS[a][4] - 1.0 - i, 3.0] for i in range(10)],
                    "asks": [[ASSETS[a][4] + 1.0 + i, 3.0] for i in range(10)],
                },
            }
            for a in self.assets
        }
        self.overrides = {}  # (path, key) -> FakeResponse

    def request(self, method, url, params=None, timeout=None, allow_redirects=True):
        params = dict(params or {})
        with self._lock:
            self.calls.append({"method": method, "url": url, "params": params, "allow_redirects": allow_redirects})
        path = urlsplit(url).path
        key = params.get("pair") or params.get("symbol")
        override = self.overrides.get((path, key)) or self.overrides.get((path, None))
        if override is not None:
            return override
        if path == "/0/public/AssetPairs":
            return FakeResponse(self.asset_pairs)
        if path == "/0/public/Ticker":
            return FakeResponse(self.ticker)
        if path == "/0/public/OHLC":
            return FakeResponse(self.ohlc[key])
        if path == "/0/public/Depth":
            return FakeResponse(self.depth[key])
        if path == "/0/public/Trades":
            return FakeResponse(self.trades[key])
        if path == "/derivatives/api/v3/tickers":
            return FakeResponse(self.futures_tickers)
        if path == "/derivatives/api/v3/orderbook":
            return FakeResponse(self.orderbook[key])
        raise AssertionError(f"unexpected request {url}")

    def close(self):
        pass

    def paths(self):
        return [urlsplit(call["url"]).path for call in self.calls]


class FakeQwen:
    """Counts every model call; never touches Ollama."""

    def __init__(self):
        self.calls = []

    def __call__(self, payloads):
        self.calls.append(payloads)
        return QwenBatchResult(status="OK", reviews={})

    @property
    def assets(self):
        return [payload["asset"] for batch in self.calls for payload in batch]


def _fake_anomaly(store, asset, pair, now, features, btc_pair):
    return AnomalyResult(
        asset=asset, warmup=False, sample_count=100, history_minutes=600.0, anomaly_score=5.0,
        price_z=None, volume_z=None, trades_z=None, oi_z=None, relative_btc_z=None, features=features,
    )


def _fake_setup(_l1, _l2f):
    return SetupResult("BREAKOUT", "LONG", [])


def _fake_opportunity(_l1, _l2f, _setup, _market, _spread):
    return OpportunityResult(
        score=80.0, breakdown={"momentum_coherence": 1.0, "derivatives_coherence": 1.0}, derivatives_coherence="COHERENT"
    )


def _fake_tradeability(**_kwargs):
    return TradeabilityResult(score=80.0, state="TRADEABLE")


class IntegrityWiringBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crypto-radar-t023b-")
        self.db_path = os.path.join(self.tmp, "state.sqlite")
        self.store = SnapshotStore(self.db_path)
        self.qwen = FakeQwen()
        self.routed = []
        self.run_records = []

        def spy_route(ctx, data_quality_ok=True):
            self.routed.append(ctx)
            return route(ctx, data_quality_ok=data_quality_ok)

        logger = logging.getLogger("radar_v08.test_integrity_wiring")
        patches = [
            mock.patch.object(heartbeat, "configure_logging", return_value=logger),
            mock.patch.object(heartbeat, "append_run_record", side_effect=self.run_records.append),
            mock.patch.object(
                heartbeat,
                "get_asset_pairs",
                functools.partial(heartbeat.get_asset_pairs, cache_path=os.path.join(self.tmp, "pairs.json")),
            ),
            mock.patch.object(config, "EVENTS_LOG_PATH", os.path.join(self.tmp, "events.jsonl")),
            mock.patch.object(heartbeat, "review_finalists", self.qwen),
            mock.patch.object(heartbeat, "route", spy_route),
            mock.patch.object(heartbeat, "compute_anomaly", _fake_anomaly),
            mock.patch.object(l2, "classify_setup", _fake_setup),
            mock.patch.object(l2, "compute_opportunity_score", _fake_opportunity),
            mock.patch.object(l3, "compute_tradeability", _fake_tradeability),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cycle(self, kraken, clock=None):
        session = GuardedSession(1.0, 0, 0.0, http_session=kraken)
        return heartbeat.run_heartbeat(
            mode="TEST", store=self.store, full=True, session=session, clock=clock or StepClock()
        )

    def routed_assets(self):
        return [ctx.asset for ctx in self.routed]

    def events(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY asset")]

    def count(self, sql, *args):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(sql, args).fetchone()[0]

    def assert_blocked_before_model(self, output):
        self.assertEqual(self.qwen.calls, [], "the fake Qwen must not be called")
        self.assertEqual(self.routed, [], "nothing may reach the router")
        self.assertEqual(self.events(), [])
        self.assertEqual(output["funnel"]["events_created"], 0)


class TestValidSnapshotReachesTheModel(IntegrityWiringBase):
    def test_valid_case_reaches_qwen_and_router_with_real_taker_buy_and_integrity_record(self):
        kraken = FakeKraken(assets=("BTC", "ETH"))

        output = self.run_cycle(kraken)

        self.assertEqual(len(self.qwen.calls), 1)
        self.assertEqual(sorted(self.qwen.assets), ["BTC", "ETH"])
        self.assertEqual(sorted(self.routed_assets()), ["BTC", "ETH"])
        by_asset = {ctx.asset: ctx for ctx in self.routed}
        # 16 buys / 4 sells of equal size: the real, validated taker-buy ratio.
        self.assertAlmostEqual(by_asset["BTC"].taker_buy_ratio, 0.8)
        self.assertEqual(by_asset["BTC"].derivatives_coherence_credit, 1.0)
        payload = {p["asset"]: p for p in self.qwen.calls[0]}
        self.assertEqual(payload["BTC"]["derivatives_coherence"], "COHERENT")

        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["clock"], "PASS")
        self.assertEqual(integrity["policy_version"], "OC-1")
        self.assertEqual(integrity["futures_eligible"], 2)
        self.assertEqual(
            (integrity["spot_rows_rejected"], integrity["l1_blocked"], integrity["l2_blocked"], integrity["seal_blocked"]),
            (0, 0, 0, 0),
        )

        events = self.events()
        self.assertEqual([e["asset"] for e in events], ["BTC", "ETH"])
        context = json.loads(events[0]["context_json"])
        record = context["integrity"]
        self.assertFalse(record["opportunity_blocked"])
        self.assertEqual(record["policy_version"], "OC-1")
        self.assertEqual(record["eligible_futures"], ["PF_XBTUSD"])
        statuses = {(r["capability"], r["subject"]): r["status"] for r in record["results"]}
        self.assertEqual(statuses[("spot_ticker", "XBT/USD")], "PASS")
        self.assertEqual(statuses[("pair_ohlc", "XBT/USD")], "PASS")
        self.assertEqual(statuses[("book", "XBT/USD")], "PASS")
        self.assertEqual(statuses[("trades", "XBT/USD")], "PASS")
        self.assertEqual(statuses[("futures", "PF_XBTUSD")], "PASS")
        self.assertEqual(statuses[("clock", "utc")], "PASS")
        ticker = next(r for r in record["results"] if r["capability"] == "spot_ticker")
        self.assertEqual(ticker["time_basis"], "RECEIPT_ONLY")
        self.assertEqual(ticker["source_time"], "UNAVAILABLE")  # never a fabricated exchange time
        self.assertEqual(record["futures_book"]["status"], "PASS")
        self.assertIsNotNone(record["clock_reference"]["offset_bound_ms"])
        self.assertEqual(events[0]["market"], "FUTURES")

    def test_forming_bar_is_neither_validated_as_closed_nor_stored(self):
        kraken = FakeKraken()

        self.run_cycle(kraken)

        stored = self.count("SELECT COUNT(*) FROM ohlc_bars WHERE pair = ?", "XXBTZUSD")
        self.assertEqual(stored, 60)  # 60 closed bars; the 12:00 forming bar is not stored
        newest = self.count("SELECT MAX(bar_time) FROM ohlc_bars WHERE pair = ?", "XXBTZUSD")
        self.assertEqual(newest, (FORMING_OPEN - BAR).isoformat())
        self.assertEqual(len(self.qwen.calls), 1)


class TestPoisonedSourcesNeverReachTheModel(IntegrityWiringBase):
    def test_nan_spot_price_is_rejected_before_persistence_and_l1(self):
        kraken = FakeKraken()
        kraken.ticker["result"]["XXBTZUSD"]["b"][0] = "nan"

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        self.assertEqual(output["data_quality"]["integrity"]["spot_rows_rejected"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM spot_snapshots WHERE pair = ?", "XXBTZUSD"), 0)
        self.assertNotIn("/0/public/OHLC", kraken.paths())  # never shortlisted, nothing consumed
        self.assertEqual(output["candidates"], [])

    def test_crossed_spot_ticker_is_rejected(self):
        kraken = FakeKraken()
        row = kraken.ticker["result"]["XXBTZUSD"]
        row["b"][0], row["a"][0] = "60001.0", "60000.0"

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        self.assertEqual(output["data_quality"]["integrity"]["spot_rows_rejected"], 1)

    def test_swapped_pair_in_ohlc_response_blocks_l2_and_stores_nothing(self):
        kraken = FakeKraken()
        payload = kraken.ohlc["XXBTZUSD"]["result"]
        payload["XETHZUSD"] = payload.pop("XXBTZUSD")  # Kraken answered with another market

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        self.assertEqual(output["data_quality"]["integrity"]["l2_blocked"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM ohlc_bars"), 0)
        self.assertIsNone(self.store.get_ohlc_cursor("XXBTZUSD", config.OHLC_INTERVAL_MINUTES))
        candidate = output["candidates"][0]
        self.assertIn("integrity_blocked:l2", candidate["flags"])
        self.assertIsNone(candidate["opportunity_score"])  # unknown, not zero
        self.assertNotIn("/0/public/Depth", kraken.paths())

    def test_swapped_pair_in_depth_response_blocks_at_the_seal(self):
        kraken = FakeKraken()
        book = kraken.depth["XXBTZUSD"]["result"]
        book["XETHZUSD"] = book.pop("XXBTZUSD")

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        self.assertEqual(output["data_quality"]["integrity"]["seal_blocked"], 1)

    def test_open_bar_after_the_forming_bar_blocks_l2(self):
        kraken = FakeKraken()
        rows = kraken.ohlc["XXBTZUSD"]["result"]["XXBTZUSD"]
        rows.append([int(_epoch(FORMING_OPEN + BAR)), "60000", "60060", "59940", "60000", "60000", "10.0", 5])

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        self.assertEqual(output["data_quality"]["integrity"]["l2_blocked"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM ohlc_bars"), 0)

    def test_nan_ohlc_bar_blocks_l2(self):
        kraken = FakeKraken()
        kraken.ohlc["XXBTZUSD"]["result"]["XXBTZUSD"][10][4] = "nan"

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        self.assertEqual(self.count("SELECT COUNT(*) FROM ohlc_bars"), 0)

    def test_stale_ohlc_window_blocks_l2(self):
        kraken = FakeKraken()
        rows = kraken.ohlc["XXBTZUSD"]["result"]["XXBTZUSD"]
        del rows[-3:]  # last close 11:50: 10.5 min old > 6 min

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        self.assertEqual(output["data_quality"]["integrity"]["l2_blocked"], 1)

    def test_crossed_book_blocks_at_the_seal(self):
        kraken = FakeKraken()
        book = kraken.depth["XXBTZUSD"]["result"]["XXBTZUSD"]
        book["bids"][0][0] = "60010.0"  # best bid above best ask 60000.5

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        self.assertEqual(output["data_quality"]["integrity"]["seal_blocked"], 1)
        self.assertIn("integrity_blocked:seal", output["candidates"][0]["flags"])

    def test_nan_trade_price_blocks_at_the_seal(self):
        kraken = FakeKraken()
        kraken.trades["XXBTZUSD"]["result"]["XXBTZUSD"][0][0] = "nan"

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)

    def test_redirect_outside_the_allowlist_is_refused_loudly_before_the_model(self):
        kraken = FakeKraken()
        kraken.overrides[("/0/public/Depth", "XXBTZUSD")] = FakeResponse(
            status_code=302, headers={"Location": "https://evil.test/0/public/Depth"}
        )

        with self.assertRaises(RedirectRefused):
            self.run_cycle(kraken)

        self.assertEqual(self.qwen.calls, [])
        self.assertEqual(self.routed, [])
        self.assertTrue(all(call["allow_redirects"] is False for call in kraken.calls))

    def test_redirect_inside_the_allowlist_is_not_followed_and_blocks(self):
        kraken = FakeKraken()
        kraken.overrides[("/0/public/Depth", "XXBTZUSD")] = FakeResponse(
            status_code=301, headers={"Location": f"{SPOT}/Depth"}
        )

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        self.assertEqual(kraken.paths().count("/0/public/Depth"), 1)  # never followed

    def test_future_dated_depth_level_blocks_at_the_seal(self):
        kraken = FakeKraken()
        kraken.depth["XXBTZUSD"]["result"]["XXBTZUSD"]["asks"][3][2] = _epoch(T0 + timedelta(minutes=10))

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)


class TestFuturesAreRejectedOneByOne(IntegrityWiringBase):
    def test_five_minute_old_future_is_removed_alone_and_valid_spot_still_arrives(self):
        kraken = FakeKraken(assets=("BTC", "ETH"))
        kraken.futures_tickers["tickers"][0]["lastTime"] = _iso_z(T0 - timedelta(minutes=5))

        output = self.run_cycle(kraken)

        integrity = output["data_quality"]["integrity"]
        self.assertEqual((integrity["futures_eligible"], integrity["futures_rejected"]), (1, 1))
        self.assertEqual(output["data_quality"]["futures_ticker"], "OK")
        # Valid spot BTC still reaches the model, without any futures claim.
        self.assertEqual(sorted(self.qwen.assets), ["BTC", "ETH"])
        payload = {p["asset"]: p for p in self.qwen.calls[0]}
        self.assertEqual(payload["BTC"]["derivatives_coherence"], "UNAVAILABLE")
        self.assertEqual(payload["ETH"]["derivatives_coherence"], "COHERENT")
        by_asset = {ctx.asset: ctx for ctx in self.routed}
        self.assertEqual(by_asset["BTC"].derivatives_coherence_credit, 0.0)
        self.assertEqual(by_asset["ETH"].derivatives_coherence_credit, 1.0)
        # The stale perpetual is never matched, fetched or persisted as current data.
        self.assertNotIn(
            "PF_XBTUSD", [c["params"].get("symbol") for c in kraken.calls if c["url"].endswith("/orderbook")]
        )
        events = {e["asset"]: e for e in self.events()}
        self.assertEqual(events["BTC"]["market"], "SPOT")
        self.assertEqual(events["ETH"]["market"], "FUTURES")
        btc_context = json.loads(events["BTC"]["context_json"])
        self.assertEqual(btc_context["futures"], "UNAVAILABLE")
        self.assertEqual(btc_context["futures_symbol"], "UNAVAILABLE")
        self.assertEqual(btc_context["integrity"]["eligible_futures"], [])
        btc = next(c for c in output["candidates"] if c["asset"] == "BTC")
        self.assertIsNone(btc["futures_symbol"])

    def test_future_with_swapped_pair_field_is_hard_rejected_and_not_persisted(self):
        kraken = FakeKraken(assets=("BTC", "ETH"))
        kraken.futures_tickers["tickers"][0]["pair"] = "ETH:USD"  # PF_XBTUSD claiming to be ETH

        output = self.run_cycle(kraken)

        self.assertEqual(self.count("SELECT COUNT(*) FROM futures_snapshots WHERE symbol = ?", "PF_XBTUSD"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM futures_snapshots WHERE symbol = ?", "PF_ETHUSD"), 1)
        self.assertEqual(output["data_quality"]["integrity"]["futures_eligible"], 1)
        self.assertEqual(sorted(self.qwen.assets), ["BTC", "ETH"])

    def test_future_with_nan_price_is_rejected_without_touching_spot(self):
        kraken = FakeKraken()
        kraken.futures_tickers["tickers"][0]["bid"] = "nan"

        output = self.run_cycle(kraken)

        self.assertEqual(self.count("SELECT COUNT(*) FROM futures_snapshots"), 0)
        self.assertEqual(output["data_quality"]["futures_ticker"], "STALE")
        self.assertEqual(self.qwen.assets, ["BTC"])
        self.assertEqual(self.routed[0].derivatives_coherence_credit, 0.0)

    def test_future_without_a_bid_is_unknown_kept_as_observation_but_never_consumed(self):
        kraken = FakeKraken()
        del kraken.futures_tickers["tickers"][0]["bid"]

        output = self.run_cycle(kraken)

        self.assertEqual(self.count("SELECT COUNT(*) FROM futures_snapshots WHERE symbol = ?", "PF_XBTUSD"), 1)
        self.assertEqual(output["data_quality"]["integrity"]["futures_eligible"], 0)
        self.assertEqual(self.qwen.assets, ["BTC"])


class TestClockIsNeverAssumedSynchronised(IntegrityWiringBase):
    def test_no_venue_time_reference_suspends_every_consumption(self):
        kraken = FakeKraken()
        kraken.overrides[("/derivatives/api/v3/tickers", None)] = FakeResponse(status_code=503)

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        integrity = output["data_quality"]["integrity"]
        self.assertEqual(integrity["clock"], "UNKNOWN")
        self.assertEqual(integrity["clock_reasons"], ["clock_sync_unknown", "clock_uncertainty_unknown"])
        self.assertEqual(integrity["l1_blocked"], 1)
        # L0 persistence of the valid observation still happens.
        self.assertEqual(self.count("SELECT COUNT(*) FROM spot_snapshots WHERE pair = ?", "XXBTZUSD"), 1)

    def test_skewed_local_clock_fails_and_blocks(self):
        kraken = FakeKraken()
        kraken.futures_tickers["serverTime"] = _iso_z(T0 + timedelta(seconds=2))

        output = self.run_cycle(kraken)

        self.assert_blocked_before_model(output)
        self.assertEqual(output["data_quality"]["integrity"]["clock"], "FAIL")
        self.assertEqual(output["data_quality"]["integrity"]["clock_reasons"], ["clock_uncertainty_exceeded"])

    def test_venue_clock_sample_bounds_the_offset_by_the_round_trip(self):
        sent = T0
        timing = SourceTiming(received_at=T0 + timedelta(milliseconds=200), source_time=T0 + timedelta(milliseconds=50))
        sample, record = heartbeat._venue_clock_sample(sent, timing, sent)
        self.assertEqual(
            sample, ClockSample(synchronized=True, offset_uncertainty=timedelta(milliseconds=151), previous_wall_time=sent)
        )
        self.assertEqual(record["offset_bound_ms"], 151.0)
        unknown, _ = heartbeat._venue_clock_sample(sent, SourceTiming(received_at=T0, source_time=None), sent)
        self.assertEqual(unknown, ClockSample(None, None, sent))


class TestTakerBuyIsUnavailableNotZero(IntegrityWiringBase):
    def test_no_trades_reaches_router_with_taker_buy_unavailable(self):
        kraken = FakeKraken()
        kraken.trades["XXBTZUSD"]["result"]["XXBTZUSD"] = []

        self.run_cycle(kraken)

        self.assertEqual(self.qwen.assets, ["BTC"])
        self.assertIsNone(self.routed[0].taker_buy_ratio)
        record = json.loads(self.events()[0]["context_json"])["integrity"]
        trades = next(r for r in record["results"] if r["capability"] == "trades")
        self.assertEqual(trades["status"], "UNKNOWN")
        self.assertEqual([reason["code"] for reason in trades["reasons"]], ["no_trades"])

    def test_unknown_aggressor_side_makes_taker_buy_unavailable(self):
        kraken = FakeKraken()
        kraken.trades["XXBTZUSD"]["result"]["XXBTZUSD"][0][3] = "x"

        self.run_cycle(kraken)

        self.assertEqual(self.qwen.assets, ["BTC"])
        self.assertIsNone(self.routed[0].taker_buy_ratio)

    def test_stale_last_trade_makes_taker_buy_unavailable(self):
        kraken = FakeKraken()
        for row in kraken.trades["XXBTZUSD"]["result"]["XXBTZUSD"]:
            row[2] = _epoch(T0 - timedelta(minutes=3))

        self.run_cycle(kraken)

        self.assertEqual(self.qwen.assets, ["BTC"])
        self.assertIsNone(self.routed[0].taker_buy_ratio)

    def test_all_sells_pass_a_real_zero(self):
        kraken = FakeKraken()
        kraken.trades["XXBTZUSD"]["result"]["XXBTZUSD"] = _trades(60000.0, buys=0, sells=10)

        self.run_cycle(kraken)

        self.assertEqual(self.routed[0].taker_buy_ratio, 0.0)  # measured zero, validated


class TestRouterSeam(unittest.TestCase):
    def test_valid_taker_buy_ratio_accepts_only_finite_values_in_unit_interval(self):
        self.assertEqual(valid_taker_buy_ratio(0.0), 0.0)
        self.assertEqual(valid_taker_buy_ratio(1.0), 1.0)
        self.assertEqual(valid_taker_buy_ratio(0.8), 0.8)
        for bad in (None, math.nan, math.inf, -math.inf, -0.01, 1.01, True, "0.8"):
            self.assertIsNone(valid_taker_buy_ratio(bad), bad)

    def _ctx(self, ratio, direction="LONG"):
        return RouterContext(
            asset="BTC", anomaly_score=5.0, opportunity_score=60.0, tradeability_score=80.0,
            tradeability_state="TRADEABLE", setup_type="BREAKOUT", direction=direction,
            momentum_1h_atr=None, momentum_coherence=0.0, volume_intensity_15m=None,
            range_expansion=None, breakout_state="NONE", derivatives_coherence_credit=0.0,
            taker_buy_ratio=ratio, qwen_status="SKIPPED",
        )

    def test_invalid_ratio_never_counts_as_a_confirmation(self):
        self.assertEqual(count_confirmations(self._ctx(0.9))[1], ["taker_imbalance"])
        for bad in (math.nan, 5.0, -3.0, math.inf):
            self.assertEqual(count_confirmations(self._ctx(bad))[1], [])
            self.assertEqual(count_confirmations(self._ctx(bad, direction="SHORT"))[1], [])
        self.assertEqual(count_confirmations(self._ctx(0.1, direction="SHORT"))[1], ["taker_imbalance"])


class TestLegacyPathsUnchanged(unittest.TestCase):
    def test_run_l2_and_run_l3_without_clock_keep_their_signature_and_defaults(self):
        self.assertEqual(l2.run_l2(None, None, [], T0, "run"), ({}, 0, 0))
        self.assertEqual(l3.run_l3(None, []), ({}, 0, 0))


if __name__ == "__main__":
    unittest.main()
