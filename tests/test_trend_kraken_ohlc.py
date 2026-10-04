"""Kraken public daily OHLC adapter: the boundary and row validation.

* boundary: only pairs XBTEUR/ETHEUR, interval 1440 and the parameters pair/interval/since are sent,
  always through ``GuardedSession`` to ``https://api.kraken.com/0/public/OHLC``; anything else is
  refused before a request; no environment, credential or private endpoint;
* failures: Kraken error envelopes (rate limits stop the client), network/5xx/429 after the
  session's bounded retries, other HTTP errors, redirects, invalid JSON and every malformed row
  raise one typed ``KrakenOhlcError`` and return nothing; the request budget is bounded;
* success: closed bars from ``since`` on plus the still-open candle's open only (its high, low and
  close are never read), the real result keys, and a visible truncated history.

No network: socket connections are refused for the whole module; HTTP goes to a fake inner session
(or a real ``requests.Session`` with a recording transport adapter), sleeps and timers are injected.
"""

from __future__ import annotations

import ast
import json
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

import requests
from requests.adapters import BaseAdapter

from radar_v08 import config, security
from radar_v08.adapters import kraken_public_ohlc as K
from radar_v08.domain import trend_engine as E
from radar_v08.domain.trend_paper import DailySeries
from radar_v08.http_client import GuardedSession

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPOSITORY_ROOT / "radar_v08" / "adapters" / "kraken_public_ohlc.py"
DAY = 86_400
SINCE = date(2026, 10, 4)
NOW = datetime(2026, 10, 7, 8, 30, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)

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


def secs(day: date) -> int:
    return E.day_open_ms(day) // 1000


def row(day: date, o="100.0", h="110.0", low="90.0", c="105.0", vwap="101.5", volume="12.5", count=42) -> list:
    return [secs(day), o, h, low, c, vwap, volume, count]


def good_rows(first: date = date(2026, 10, 3), live: date = NOW.date()) -> list[list]:
    out, day, price = [], first, 50_000.0
    while day <= live:
        out.append(row(day, f"{price:.1f}", f"{price + 900:.1f}", f"{price - 700:.1f}", f"{price + 300:.1f}"))
        day += timedelta(days=1)
        price += 300
    return out


def envelope(rows, key="XXBTZEUR", last=None, error=None) -> dict:
    result = {key: rows}
    result["last"] = last if last is not None else (rows[-2][0] if len(rows) > 1 else 0)
    return {"error": error or [], "result": result}


def body(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


class FakeResponse:
    def __init__(self, status=200, content=b"{}", headers=None, history=()):
        self.status_code = status
        self.content = content
        self.headers = dict(headers or {})
        self.history = list(history)
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class FakeHttpSession:
    """Stand-in for the inner requests.Session: records calls and replays scripted results."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.trust_env = True
        self.auth = ("user", "secret")
        self.closed = False

    def request(self, method, url, params=None, timeout=None, allow_redirects=True, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "params": params, "timeout": timeout,
             "allow_redirects": allow_redirects, "extra": kwargs}
        )
        if not self.results:
            raise AssertionError(f"unexpected extra request to {url}")
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def ok(payload) -> FakeResponse:
    return FakeResponse(200, body(payload))


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def client(*results, **kwargs):
    fake = FakeHttpSession(*results)
    sleeps = Sleeps()
    kwargs.setdefault("timer", lambda: 0.0)
    made = K.KrakenPublicOhlc(http_session=fake, sleep=sleeps, **kwargs)
    return made, fake, sleeps


class TestSuccess(unittest.TestCase):
    def test_closed_bars_and_the_live_open_with_the_real_xbt_key(self):
        rows = good_rows()
        rows[-1][2:5] = ["not a number", "NaN", "-1"]  # the live candle's high, low and close are never read
        c, fake, _ = client(ok(envelope(rows, "XXBTZEUR")))
        series = c.fetch_daily("XBTEUR", SINCE, NOW_MS)
        self.assertIsInstance(series, DailySeries)
        self.assertIsInstance(series, K.KrakenDailySeries)
        self.assertEqual(series.symbol, "XBTEUR")
        self.assertEqual([E.utc_day(b.open_time_ms) for b in series.closed],
                         [date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)])  # 2026-10-03 dropped
        first = series.closed[0]
        self.assertEqual((first.open, first.high, first.low, first.close), (50_300.0, 51_200.0, 49_600.0, 50_600.0))
        self.assertEqual(series.live_day, date(2026, 10, 7))
        self.assertEqual(series.live_open, float(rows[-1][1]))
        self.assertIsNone(series.close_on(date(2026, 10, 7)))
        self.assertEqual(series.open_on(date(2026, 10, 7)), float(rows[-1][1]))
        self.assertIsNone(series.history_start)
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://api.kraken.com/0/public/OHLC")
        self.assertEqual(call["params"], {"pair": "XBTEUR", "interval": 1440, "since": secs(SINCE) - DAY})
        self.assertIs(call["allow_redirects"], False)
        self.assertEqual(call["timeout"], K.DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(c.requests_sent, 1)

    def test_eth_uses_the_xeth_key_and_returns_the_requested_pair(self):
        c, fake, _ = client(ok(envelope(good_rows(), "XETHZEUR")))
        series = c.fetch_daily("ETHEUR", SINCE, NOW_MS)
        self.assertEqual(series.symbol, "ETHEUR")
        self.assertEqual(len(series.closed), 3)
        self.assertEqual(fake.calls[0]["params"]["pair"], "ETHEUR")

    def test_the_altname_key_is_accepted_too(self):
        c, _, _ = client(ok(envelope(good_rows(), "ETHEUR")))
        self.assertEqual(c.fetch_daily("ETHEUR", SINCE, NOW_MS).symbol, "ETHEUR")

    def test_a_day_that_just_ended_is_closed(self):
        now_ms = E.day_open_ms(NOW.date())  # exactly 00:00 UTC: the previous day has ended
        rows = good_rows()
        c, _, _ = client(ok(envelope(rows)))
        series = c.fetch_daily("XBTEUR", SINCE, now_ms)
        self.assertEqual(series.live_day, NOW.date())
        self.assertEqual(E.utc_day(series.closed[-1].open_time_ms), date(2026, 10, 6))

    def test_no_live_candle_returned(self):
        rows = good_rows()[:-1]
        c, _, _ = client(ok(envelope(rows)))
        series = c.fetch_daily("XBTEUR", SINCE, NOW_MS)
        self.assertIsNone(series.live_day)
        self.assertIsNone(series.live_open)
        self.assertEqual(len(series.closed), 3)

    def test_kraken_since_read_as_exclusive_does_not_look_truncated(self):
        c, _, _ = client(ok(envelope(good_rows(first=SINCE))))
        series = c.fetch_daily("XBTEUR", SINCE, NOW_MS)
        self.assertIsNone(series.history_start)
        self.assertEqual(E.utc_day(series.closed[0].open_time_ms), SINCE)

    def test_a_truncated_history_is_visible(self):
        since = date(2024, 1, 1)
        rows = good_rows(first=date(2026, 9, 1))
        c, fake, _ = client(ok(envelope(rows)))
        series = c.fetch_daily("XBTEUR", since, NOW_MS)
        self.assertEqual(series.history_start, date(2026, 9, 1))
        self.assertEqual(E.utc_day(series.closed[0].open_time_ms), date(2026, 9, 1))
        self.assertEqual(fake.calls[0]["params"]["since"], secs(since) - DAY)

    def test_a_missing_candle_inside_the_history_is_a_gap_not_a_truncation(self):
        rows = [r for r in good_rows() if r[0] != secs(date(2026, 10, 5))]
        c, _, _ = client(ok(envelope(rows)))
        series = c.fetch_daily("XBTEUR", SINCE, NOW_MS)
        self.assertIsNone(series.history_start)
        self.assertIsNone(series.open_on(date(2026, 10, 5)))
        self.assertEqual(len(series.closed), 2)

    def test_since_after_today_keeps_nothing(self):
        c, _, _ = client(ok(envelope(good_rows())))
        series = c.fetch_daily("XBTEUR", date(2026, 10, 9), NOW_MS)
        self.assertEqual(series.closed, ())
        self.assertIsNone(series.live_day)
        self.assertIsNone(series.history_start)

    def test_zero_volume_day_is_accepted(self):
        rows = good_rows()
        rows[2] = row(date(2026, 10, 5), "100", "100", "100", "100", "0.0", "0.00000000", 0)
        c, _, _ = client(ok(envelope(rows)))
        self.assertEqual(c.fetch_daily("XBTEUR", SINCE, NOW_MS).close_on(date(2026, 10, 5)), 100.0)

    def test_close_closes_the_inner_session(self):
        c, fake, _ = client()
        c.close()
        self.assertTrue(fake.closed)


class TestBoundary(unittest.TestCase):
    def test_refused_pairs_send_nothing(self):
        for pair in ("BTCEUR", "XXBTZEUR", "XETHZEUR", "xbteur", "XBT/EUR", "XBTUSD", "ETHUSDT", "", None, 1):
            with self.subTest(pair=pair):
                c, fake, _ = client(ok(envelope(good_rows())))
                with self.assertRaises(K.KrakenOhlcError) as caught:
                    c.fetch_daily(pair, SINCE, NOW_MS)
                self.assertIs(caught.exception.code, K.KrakenOhlcErrorCode.REFUSED)
                self.assertEqual(fake.calls, [])
                self.assertEqual(c.requests_sent, 0)

    def test_refused_since_or_clock_sends_nothing(self):
        cases = [
            (datetime(2026, 10, 4, tzinfo=UTC), NOW_MS),
            ("2026-10-04", NOW_MS),
            (None, NOW_MS),
            (SINCE, -1),
            (SINCE, True),
            (SINCE, float(NOW_MS)),
            (SINCE, str(NOW_MS)),
        ]
        for since, now_ms in cases:
            with self.subTest(since=since, now_ms=now_ms):
                c, fake, _ = client(ok(envelope(good_rows())))
                with self.assertRaises(K.KrakenOhlcError) as caught:
                    c.fetch_daily("XBTEUR", since, now_ms)
                self.assertIs(caught.exception.code, K.KrakenOhlcErrorCode.REFUSED)
                self.assertEqual(fake.calls, [])

    def test_refused_parameters_send_nothing(self):
        good = {"pair": "XBTEUR", "interval": 1440, "since": secs(SINCE)}
        refused = [
            {**good, "interval": 60},
            {**good, "interval": 1},
            {**good, "interval": 10080},
            {**good, "interval": "1440"},
            {**good, "interval": 1440.0},
            {**good, "interval": True},
            {**good, "pair": "XBTUSD"},
            {**good, "pair": "XXBTZEUR"},
            {**good, "pair": ["XBTEUR"]},
            {**good, "since": secs(SINCE) + 3600},
            {**good, "since": -DAY},
            {**good, "since": str(secs(SINCE))},
            {**good, "since": float(secs(SINCE))},
            {**good, "since": False},
            {**good, "count": 720},
            {**good, "nonce": 1},
            {**good, "otp": "x"},
            {"pair": "XBTEUR"},
            {"interval": 1440},
            {},
        ]
        for params in refused:
            with self.subTest(params=params):
                c, fake, _ = client(ok(envelope(good_rows())))
                with self.assertRaises(K.KrakenOhlcError) as caught:
                    c._get(params)
                self.assertIs(caught.exception.code, K.KrakenOhlcErrorCode.REFUSED)
                self.assertEqual(fake.calls, [])
                self.assertEqual(c.requests_sent, 0)
        K.assert_allowed_params(good)
        K.assert_allowed_params({"pair": "ETHEUR", "interval": 1440})
        with self.assertRaises(K.KrakenOhlcError):
            K.assert_allowed_params([("pair", "XBTEUR")])  # type: ignore[arg-type]

    def test_every_request_goes_through_the_guarded_session_and_the_allowlist(self):
        c, fake, _ = client(ok(envelope(good_rows())))
        self.assertIsInstance(c._session, GuardedSession)
        with mock.patch.object(security, "assert_allowed_request", wraps=security.assert_allowed_request) as guard:
            c.fetch_daily("XBTEUR", SINCE, NOW_MS)
        guard.assert_called_once_with("GET", K.OHLC_URL)
        self.assertIn("/0/public/OHLC", config.HTTP_PUBLIC_ALLOWLIST["api.kraken.com"])
        self.assertEqual(len(fake.calls), 1)

    def test_the_injected_session_ignores_the_environment_and_sends_no_auth(self):
        c, fake, _ = client()
        self.assertIs(fake.trust_env, False)
        self.assertIsNone(fake.auth)

    def test_the_default_session_is_a_requests_session_that_ignores_the_environment(self):
        c = K.KrakenPublicOhlc()
        try:
            inner = c._session._session
            self.assertIsInstance(inner, requests.Session)
            self.assertIs(inner.trust_env, False)
            self.assertIsNone(inner.auth)
        finally:
            c.close()

    def test_a_real_session_sends_only_the_allowed_query(self):
        sent = []

        class Recording(BaseAdapter):
            def send(self, request, **kwargs):
                sent.append((request.method, request.url, dict(request.headers)))
                response = requests.Response()
                response.status_code = 200
                response._content = body(envelope(good_rows(), "XETHZEUR"))
                response.url = request.url
                response.request = request
                return response

            def close(self):
                pass

        http = requests.Session()
        http.mount("https://", Recording())
        http.mount("http://", Recording())
        env = {"HTTPS_PROXY": "http://127.0.0.1:9", "NETRC": str(REPOSITORY_ROOT / "missing-netrc")}
        with mock.patch.dict("os.environ", env):
            c = K.KrakenPublicOhlc(http_session=http, sleep=Sleeps(), timer=lambda: 0.0)
            series = c.fetch_daily("ETHEUR", SINCE, NOW_MS)
        self.assertEqual(series.symbol, "ETHEUR")
        self.assertEqual(len(sent), 1)
        method, url, headers = sent[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, f"https://api.kraken.com/0/public/OHLC?pair=ETHEUR&interval=1440&since={secs(SINCE) - DAY}")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("API-Key", headers)

    def test_module_reads_no_environment_credentials_or_private_code(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add("." * node.level + (node.module or ""))
                modules.update(f"{node.module}.{alias.name}" for alias in node.names)
        for forbidden in ("requests", "os", "dotenv", "socket", "urllib.request", "http.client"):
            self.assertNotIn(forbidden, modules)
        for name in modules:
            self.assertNotIn("kraken_private", name)
            self.assertNotIn("kraken_futures", name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"environ", "getenv", "getenvb", "netrc"})
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, {"environ", "getenv", "open", "netrc"})
        for needle in ("/private/", "API-Key", "API-Sign", "dotenv", "secret"):
            self.assertNotIn(needle, source)

    def test_the_kraken_allowlist_is_not_widened(self):
        self.assertEqual(set(config.HTTP_PUBLIC_ALLOWLIST), {"api.kraken.com", "futures.kraken.com"})
        self.assertEqual(
            config.HTTP_PUBLIC_ALLOWLIST["api.kraken.com"],
            frozenset({"/0/public/AssetPairs", "/0/public/Ticker", "/0/public/OHLC", "/0/public/Depth", "/0/public/Trades"}),
        )


class TestTransportFailures(unittest.TestCase):
    def assertCode(self, code, call):
        with self.assertRaises(K.KrakenOhlcError) as caught:
            call()
        self.assertIs(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def test_timeouts_are_retried_with_backoff_then_fail(self):
        c, fake, sleeps = client(requests.Timeout("t1"), requests.Timeout("t2"), requests.Timeout("t3"))
        self.assertCode(K.KrakenOhlcErrorCode.NETWORK, lambda: c.fetch_daily("XBTEUR", SINCE, NOW_MS))
        self.assertEqual(len(fake.calls), K.DEFAULT_MAX_RETRIES + 1)
        self.assertEqual(sleeps.calls, [1.0, 2.0])

    def test_connection_errors_and_5xx_fail_after_the_bounded_retries(self):
        for results in (
            [requests.ConnectionError("down")] * 3,
            [FakeResponse(503)] * 3,
            [FakeResponse(502), requests.ConnectionError("x"), FakeResponse(500)],
        ):
            with self.subTest(results=results):
                c, fake, _ = client(*results)
                self.assertCode(K.KrakenOhlcErrorCode.NETWORK, lambda: c.fetch_daily("XBTEUR", SINCE, NOW_MS))
                self.assertEqual(len(fake.calls), 3)

    def test_a_retry_that_succeeds_returns_the_series(self):
        c, fake, sleeps = client(FakeResponse(503), ok(envelope(good_rows())))
        self.assertEqual(len(c.fetch_daily("XBTEUR", SINCE, NOW_MS).closed), 3)
        self.assertEqual(sleeps.calls, [1.0])

    def test_http_429_after_retries_stops_the_client(self):
        c, fake, _ = client(FakeResponse(429), FakeResponse(429), FakeResponse(429))
        self.assertCode(K.KrakenOhlcErrorCode.RATE_LIMITED, lambda: c.fetch_daily("XBTEUR", SINCE, NOW_MS))
        self.assertEqual(len(fake.calls), 3)
        self.assertCode(K.KrakenOhlcErrorCode.RATE_LIMITED, lambda: c.fetch_daily("ETHEUR", SINCE, NOW_MS))
        self.assertEqual(len(fake.calls), 3)  # nothing more was sent

    def test_other_http_errors_are_not_retried(self):
        for status in (400, 403, 404):
            with self.subTest(status=status):
                c, fake, _ = client(FakeResponse(status))
                self.assertCode(K.KrakenOhlcErrorCode.HTTP_ERROR, lambda: c.fetch_daily("XBTEUR", SINCE, NOW_MS))
                self.assertEqual(len(fake.calls), 1)

    def test_redirects_are_never_followed(self):
        for location in ("https://evil.example/0/public/OHLC", "https://api.kraken.com/0/public/OHLC", None):
            with self.subTest(location=location):
                headers = {"Location": location} if location else {}
                c, fake, _ = client(FakeResponse(302, b"", headers))
                self.assertCode(K.KrakenOhlcErrorCode.REDIRECT, lambda: c.fetch_daily("XBTEUR", SINCE, NOW_MS))
                self.assertEqual(len(fake.calls), 1)
        c, fake, _ = client(FakeResponse(200, body(envelope(good_rows())), history=[FakeResponse(301)]))
        self.assertCode(K.KrakenOhlcErrorCode.REDIRECT, lambda: c.fetch_daily("XBTEUR", SINCE, NOW_MS))

    def test_the_request_budget_bounds_one_client(self):
        results = [ok(envelope(good_rows())) for _ in range(3)]
        c, fake, _ = client(*results, max_requests=2)
        c.fetch_daily("XBTEUR", SINCE, NOW_MS)
        c.fetch_daily("XBTEUR", SINCE, NOW_MS)
        self.assertCode(K.KrakenOhlcErrorCode.BUDGET, lambda: c.fetch_daily("ETHEUR", SINCE, NOW_MS))
        self.assertEqual(len(fake.calls), 2)

    def test_an_oversized_body_is_refused(self):
        c, _, _ = client(FakeResponse(200, b" " * (K.MAX_RESPONSE_BYTES + 1)))
        self.assertCode(K.KrakenOhlcErrorCode.TOO_LARGE, lambda: c.fetch_daily("XBTEUR", SINCE, NOW_MS))


class TestEnvelopeFailures(unittest.TestCase):
    def fails(self, content: bytes, code, pair="XBTEUR"):
        c, fake, _ = client(FakeResponse(200, content))
        with self.assertRaises(K.KrakenOhlcError) as caught:
            c.fetch_daily(pair, SINCE, NOW_MS)
        self.assertIs(caught.exception.code, code, str(caught.exception))
        self.assertEqual(len(fake.calls), 1)
        return c, fake

    def test_rate_limit_envelopes_stop_the_client(self):
        for message in ("EAPI:Rate limit exceeded", "EGeneral:Too many requests", "EService:Throttled:1700000000"):
            with self.subTest(message=message):
                c, fake = self.fails(body({"error": [message]}), K.KrakenOhlcErrorCode.RATE_LIMITED)
                with self.assertRaises(K.KrakenOhlcError) as caught:
                    c.fetch_daily("ETHEUR", SINCE, NOW_MS)
                self.assertIs(caught.exception.code, K.KrakenOhlcErrorCode.RATE_LIMITED)
                self.assertEqual(len(fake.calls), 1)  # not sent

    def test_other_error_envelopes(self):
        for errors in (["EQuery:Unknown asset pair"], ["EService:Unavailable"], ["EGeneral:Invalid arguments"]):
            with self.subTest(errors=errors):
                self.fails(body({"error": errors, "result": {}}), K.KrakenOhlcErrorCode.API_ERROR)

    def test_invalid_json(self):
        for content in (b"", b"<html>", b'{"error": [], "result": {', b"\xff\xfe"):
            with self.subTest(content=content):
                self.fails(content, K.KrakenOhlcErrorCode.INVALID_JSON)

    def test_json_constants_are_non_finite(self):
        text = body(envelope(good_rows())).replace(b'"50300.0"', b"NaN")
        self.fails(text, K.KrakenOhlcErrorCode.NON_FINITE)

    def test_bad_envelopes(self):
        for payload in ([], "x", {"result": {}}, {"error": "none", "result": {}}, {"error": [1], "result": {}},
                        {"error": [], "result": {}, "extra": 1}, {"error": []}, {"error": [], "result": []}):
            with self.subTest(payload=payload):
                self.fails(body(payload), K.KrakenOhlcErrorCode.MALFORMED)

    def test_missing_or_ambiguous_result_keys(self):
        rows = good_rows()
        cases = [
            {"error": [], "result": {"last": 1}},
            {"error": [], "result": {}},
            {"error": [], "result": {"XXBTZEUR": rows, "XBTEUR": rows, "last": 1}},
            {"error": [], "result": {"XETHZEUR": rows, "last": 1}},
            {"error": [], "result": {"XXBTZUSD": rows, "last": 1}},
        ]
        for payload in cases:
            with self.subTest(keys=sorted(payload["result"])):
                self.fails(body(payload), K.KrakenOhlcErrorCode.RESULT_KEY)
        duplicated = b'{"error": [], "result": {"XXBTZEUR": [], "XXBTZEUR": ' + json.dumps(rows).encode() + b"}}"
        self.fails(duplicated, K.KrakenOhlcErrorCode.RESULT_KEY)
        self.fails(body(envelope(rows, "XXBTZEUR")), K.KrakenOhlcErrorCode.RESULT_KEY, pair="ETHEUR")

    def test_bad_last_or_rows_container(self):
        self.fails(body({"error": [], "result": {"XXBTZEUR": good_rows(), "last": "1"}}), K.KrakenOhlcErrorCode.MALFORMED)
        self.fails(body({"error": [], "result": {"XXBTZEUR": {"0": 1}, "last": 1}}), K.KrakenOhlcErrorCode.MALFORMED)
        self.fails(body({"error": [], "result": {"XXBTZEUR": [], "last": 1}}), K.KrakenOhlcErrorCode.EMPTY)


class TestRowFailures(unittest.TestCase):
    def fails(self, rows, code, now_ms=NOW_MS):
        c, fake, _ = client(ok(envelope(rows)))
        with self.assertRaises(K.KrakenOhlcError) as caught:
            c.fetch_daily("XBTEUR", SINCE, now_ms)
        self.assertIs(caught.exception.code, code, str(caught.exception))
        self.assertEqual(len(fake.calls), 1)

    def replaced(self, index, column, value):
        rows = good_rows()
        rows[index][column] = value
        return rows

    def test_malformed_rows(self):
        rows = good_rows()
        cases = [
            rows[:1] + ["not a row"] + rows[2:],
            rows[:1] + [rows[1][:7]] + rows[2:],
            rows[:1] + [rows[1] + ["extra"]] + rows[2:],
            rows[:1] + [{"time": rows[1][0]}] + rows[2:],
            self.replaced(1, 0, str(rows[1][0])),
            self.replaced(1, 0, float(rows[1][0])),
            self.replaced(1, 0, True),
            self.replaced(0, 0, -DAY),
            self.replaced(1, 7, "42"),
            self.replaced(1, 7, -1),
            self.replaced(1, 7, False),
            self.replaced(1, 2, "40000.0"),  # high below the open/close
            self.replaced(1, 3, "60000.0"),  # low above the open/close
        ]
        for n, case in enumerate(cases):
            with self.subTest(n=n):
                self.fails(case, K.KrakenOhlcErrorCode.MALFORMED)

    def test_non_numeric_values(self):
        for column in (1, 2, 3, 4, 5, 6):
            for value in ("abc", "", None, [], True, "1,5"):
                with self.subTest(column=column, value=value):
                    self.fails(self.replaced(1, column, value), K.KrakenOhlcErrorCode.MALFORMED)

    def test_non_finite_values(self):
        for column in (1, 2, 3, 4, 5, 6):
            for value in ("NaN", "nan", "inf", "-Infinity"):
                with self.subTest(column=column, value=value):
                    self.fails(self.replaced(1, column, value), K.KrakenOhlcErrorCode.NON_FINITE)

    def test_non_positive_values(self):
        for column in (1, 2, 3, 4):
            for value in ("0", "0.0", "-1", -5):
                with self.subTest(column=column, value=value):
                    self.fails(self.replaced(1, column, value), K.KrakenOhlcErrorCode.NON_POSITIVE)
        for column in (5, 6):
            with self.subTest(column=column):
                self.fails(self.replaced(1, column, "-0.5"), K.KrakenOhlcErrorCode.NON_POSITIVE)
        self.fails(self.replaced(-1, 1, "0"), K.KrakenOhlcErrorCode.NON_POSITIVE)  # the live open too

    def test_not_midnight_aligned(self):
        for offset in (1, 3600, DAY // 2):
            with self.subTest(offset=offset):
                self.fails(self.replaced(1, 0, secs(SINCE) + offset), K.KrakenOhlcErrorCode.NOT_MIDNIGHT)

    def test_duplicate_rows(self):
        rows = good_rows()
        self.fails(rows[:2] + [rows[1]] + rows[2:], K.KrakenOhlcErrorCode.DUPLICATE)

    def test_non_ascending_rows(self):
        rows = good_rows()
        self.fails([rows[0], rows[2], rows[1]] + rows[3:], K.KrakenOhlcErrorCode.NON_ASCENDING)

    def test_a_row_after_the_live_candle(self):
        rows = good_rows()
        live = rows[-1]
        self.fails(rows[:-2] + [live, rows[-2]], K.KrakenOhlcErrorCode.MALFORMED)
        # A second still-open-looking row cannot exist without opening after the clock.
        now_ms = E.day_open_ms(NOW.date()) - 1  # 2026-10-06 is live, 2026-10-07 opens after the clock
        self.fails(rows, K.KrakenOhlcErrorCode.CLOCK_SKEW, now_ms=now_ms)

    def test_a_candle_opening_after_the_clock_is_clock_skew(self):
        rows = good_rows() + [row(NOW.date() + timedelta(days=1))]
        self.fails(rows, K.KrakenOhlcErrorCode.CLOCK_SKEW)

    def test_invalid_rows_before_since_still_fail(self):
        # A bad row is a bad response even when its day would have been dropped.
        self.fails(self.replaced(0, 1, "NaN"), K.KrakenOhlcErrorCode.NON_FINITE)


if __name__ == "__main__":
    unittest.main()
