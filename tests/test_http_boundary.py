"""T023a - public HTTP boundary: exact allowlist, redirects never followed.

No network: every test uses either a duck-typed fake inner session or a real
requests.Session whose transport adapter is replaced by a recording fake, so
nothing ever leaves the process.
"""

import ast
import json
import os
import sys
import unittest
from unittest import mock

import requests
from requests.adapters import BaseAdapter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config, kraken_futures, kraken_spot, qwen
from radar_v08.http_client import ApiError, GuardedSession, RedirectNotFollowed
from radar_v08.security import (
    RedirectRefused,
    SecurityViolation,
    assert_allowed_request,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SPOT = "https://api.kraken.com/0/public"
FUTURES = "https://futures.kraken.com/derivatives/api/v3"

ENDPOINTS_USED_TODAY = (
    f"{SPOT}/AssetPairs",
    f"{SPOT}/Ticker",
    f"{SPOT}/OHLC",
    f"{SPOT}/Depth",
    f"{SPOT}/Trades",
    f"{FUTURES}/tickers",
    f"{FUTURES}/orderbook",
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, history=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = dict(headers or {})
        self.history = list(history or [])

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeHttpSession:
    """Stand-in for requests.Session: records calls, replays scripted results."""

    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []
        self.closed = False

    def request(self, method, url, params=None, timeout=None, allow_redirects=True):
        self.calls.append(
            {"method": method, "url": url, "params": params, "timeout": timeout, "allow_redirects": allow_redirects}
        )
        if not self.results:
            raise AssertionError(f"unexpected extra request to {url}")
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class RecordingAdapter(BaseAdapter):
    """Transport-level fake for a real requests.Session: never opens a socket."""

    def __init__(self, results):
        super().__init__()
        self.results = list(results)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append((request.method, request.url))
        if not self.results:
            raise AssertionError(f"unexpected extra request to {request.url}")
        status, headers, body = self.results.pop(0)
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content = json.dumps(body).encode("utf-8")
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


def real_session_with(results):
    adapter = RecordingAdapter(results)
    http = requests.Session()
    http.trust_env = False
    http.mount("https://", adapter)
    http.mount("http://", adapter)
    return GuardedSession(timeout=5, max_retries=0, backoff_base=0, http_session=http), adapter


def guarded(results=(), max_retries=3, backoff_base=0.5):
    fake = FakeHttpSession(results)
    return GuardedSession(timeout=7, max_retries=max_retries, backoff_base=backoff_base, http_session=fake), fake


class TestAllowlistConstants(unittest.TestCase):
    def test_allowlist_is_exactly_the_endpoints_used_today(self):
        self.assertEqual(config.HTTP_ALLOWED_SCHEME, "https")
        self.assertEqual(config.HTTP_ALLOWED_METHODS, frozenset({"GET"}))
        self.assertEqual(
            dict(config.HTTP_PUBLIC_ALLOWLIST),
            {
                "api.kraken.com": frozenset(
                    {"/0/public/AssetPairs", "/0/public/Ticker", "/0/public/OHLC", "/0/public/Depth", "/0/public/Trades"}
                ),
                "futures.kraken.com": frozenset({"/derivatives/api/v3/tickers", "/derivatives/api/v3/orderbook"}),
            },
        )

    def test_allowlist_is_read_only(self):
        with self.assertRaises(TypeError):
            config.HTTP_PUBLIC_ALLOWLIST["evil.test"] = frozenset({"/"})  # type: ignore[index]
        with self.assertRaises(AttributeError):
            config.HTTP_PUBLIC_ALLOWLIST["api.kraken.com"].add("/0/private/Balance")  # type: ignore[attr-defined]

    def test_every_endpoint_used_today_is_allowed(self):
        for url in ENDPOINTS_USED_TODAY:
            with self.subTest(url=url):
                assert_allowed_request("GET", url)


class TestRefusedBeforeLeavingProcess(unittest.TestCase):
    REFUSED_URLS = (
        "https://api.kraken.com.evil.test/0/public/Ticker",
        "https://evil.test/0/public/Ticker",
        "https://kraken.com/0/public/Ticker",
        "https://api.kraken.com@evil.test/0/public/Ticker",
        "https://kraken.com@evil/0/public/Ticker",
        "https://user:pass@api.kraken.com/0/public/Ticker",
        "https://@api.kraken.com/0/public/Ticker",
        "http://api.kraken.com/0/public/Ticker",
        "HTTP://api.kraken.com/0/public/Ticker",
        "ftp://api.kraken.com/0/public/Ticker",
        "//api.kraken.com/0/public/Ticker",
        "/0/public/Ticker",
        "https://api.kraken.com:443/0/public/Ticker",
        "https://api.kraken.com:8443/0/public/Ticker",
        "https://api.kraken.com:/0/public/Ticker",
        "https://futures.kraken.com:444/derivatives/api/v3/tickers",
        "https://API.KRAKEN.COM/0/public/Ticker",
        "https://api.kraken.com./0/public/Ticker",
        "https://[::1]/0/public/Ticker",
        "https://api.kraken.com/0/private/Balance",
        "https://api.kraken.com/0/private/AddOrder",
        "https://api.kraken.com/0/PRIVATE/Balance",
        "https://api.kraken.com/0/public/../private/Balance",
        "https://api.kraken.com/0/public/%2e%2e/private/Balance",
        "https://api.kraken.com/0/public/%54icker",
        "https://api.kraken.com/0/public/ticker",
        "https://api.kraken.com/0/public/Ticker/",
        "https://api.kraken.com/0/public/Ticker;x=1",
        "https://api.kraken.com/0/public/SystemStatus",
        "https://api.kraken.com/0/public",
        "https://api.kraken.com/",
        "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
        "https://api.kraken.com/0/public/Ticker#frag",
        "https://api.kraken.com/0/public/Ticker\t",
        "https://api.kraken.com/0/public/Tick\ner",
        " https://api.kraken.com/0/public/Ticker",
        "https://api.kraken.com\\@evil.test/0/public/Ticker",
        "https://futures.kraken.com/derivatives/api/v3/sendorder",
        "https://futures.kraken.com/derivatives/api/v3/accounts",
        "https://futures.kraken.com/derivatives/api/v3/openpositions",
        "https://futures.kraken.com/0/public/Ticker",
        "https://api.kraken.com/derivatives/api/v3/tickers",
        "http://localhost:11434/api/chat",
        "http://127.0.0.1:11434/api/chat",
        "https://ntfy.sh/some-topic",
    )

    def test_each_refused_url_raises_and_never_reaches_transport(self):
        for url in self.REFUSED_URLS:
            with self.subTest(url=url):
                session, fake = guarded()
                with self.assertRaises(SecurityViolation):
                    session.get(url)
                self.assertEqual(fake.calls, [])

    def test_refused_url_never_reaches_a_real_requests_transport(self):
        session, adapter = real_session_with([])
        for url in self.REFUSED_URLS:
            with self.subTest(url=url):
                with self.assertRaises(SecurityViolation):
                    session.get(url)
        self.assertEqual(adapter.sent, [])

    def test_non_get_methods_are_refused(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "CONNECT", "TRACE", "", None, b"GET"):
            with self.subTest(method=method):
                with self.assertRaises(SecurityViolation):
                    assert_allowed_request(method, f"{SPOT}/Ticker")

    def test_guarded_session_exposes_no_write_method(self):
        for name in ("post", "put", "delete", "patch", "request", "send", "head"):
            self.assertFalse(hasattr(GuardedSession, name), name)

    def test_non_string_url_is_refused(self):
        for url in (None, b"https://api.kraken.com/0/public/Ticker", 42):
            with self.subTest(url=url):
                with self.assertRaises(SecurityViolation):
                    assert_allowed_request("GET", url)


class TestAcceptedRequests(unittest.TestCase):
    def test_each_endpoint_used_today_is_sent_once_without_redirects(self):
        for url in ENDPOINTS_USED_TODAY:
            with self.subTest(url=url):
                session, fake = guarded([FakeResponse(200, {"ok": True})])
                response = session.get(url, params={"pair": "XBTUSD"})
                self.assertEqual(response.json(), {"ok": True})
                self.assertEqual(
                    fake.calls,
                    [{"method": "GET", "url": url, "params": {"pair": "XBTUSD"}, "timeout": 7, "allow_redirects": False}],
                )

    def test_default_inner_session_is_a_requests_session(self):
        default = GuardedSession(timeout=1, max_retries=0, backoff_base=0)
        try:
            self.assertIsInstance(default._session, requests.Session)
        finally:
            default.close()

    def test_real_call_sites_hit_only_allowlisted_endpoints(self):
        ok_spot = {"error": [], "result": {}}
        ok_futures = {"result": "success", "tickers": [], "orderBook": {"bids": [], "asks": []}}
        session, adapter = real_session_with(
            [(200, {}, ok_spot)] * 7 + [(200, {}, ok_futures)] * 4
        )
        kraken_spot.fetch_asset_pairs(session)
        kraken_spot.fetch_ticker(session)
        kraken_spot.fetch_ohlc(session, "XBTUSD", interval=5, since=1700000000)
        kraken_spot.fetch_depth(session, "XBTUSD", count=25)
        kraken_spot.fetch_depth_with_times(session, "XBTUSD", count=25)
        kraken_spot.fetch_trades(session, "XBTUSD")
        kraken_spot.fetch_trades(session, "XBTUSD", since=1700000000.5)
        kraken_futures.fetch_tickers(session)
        kraken_futures.fetch_tickers_with_server_time(session)
        kraken_futures.fetch_orderbook(session, "PF_XBTUSD")
        kraken_futures.fetch_orderbook_with_server_time(session, "PF_XBTUSD")

        self.assertEqual(
            adapter.sent,
            [
                ("GET", f"{SPOT}/AssetPairs"),
                ("GET", f"{SPOT}/Ticker"),
                ("GET", f"{SPOT}/OHLC?pair=XBTUSD&interval=5&since=1700000000"),
                ("GET", f"{SPOT}/Depth?pair=XBTUSD&count=25"),
                ("GET", f"{SPOT}/Depth?pair=XBTUSD&count=25"),
                ("GET", f"{SPOT}/Trades?pair=XBTUSD"),
                ("GET", f"{SPOT}/Trades?pair=XBTUSD&since=1700000000.5"),
                ("GET", f"{FUTURES}/tickers"),
                ("GET", f"{FUTURES}/tickers"),
                ("GET", f"{FUTURES}/orderbook?symbol=PF_XBTUSD"),
                ("GET", f"{FUTURES}/orderbook?symbol=PF_XBTUSD"),
            ],
        )
        used = {url.split("?", 1)[0] for _method, url in adapter.sent}
        allowed = {
            f"https://{host}{path}" for host, paths in config.HTTP_PUBLIC_ALLOWLIST.items() for path in paths
        }
        self.assertEqual(used, allowed)

    def test_hostile_query_parameter_cannot_change_host_or_path(self):
        session, adapter = real_session_with([(200, {}, {"error": [], "result": {}})])
        kraken_spot.fetch_depth(session, "../../0/private/Balance?x=@evil.test#", count=25)
        self.assertEqual(len(adapter.sent), 1)
        sent = adapter.sent[0][1]
        self.assertTrue(sent.startswith(f"{SPOT}/Depth?pair="), sent)
        self.assertNotIn("/0/private/", sent)
        self.assertNotIn("#", sent)


class TestRedirectsNeverFollowed(unittest.TestCase):
    def test_redirect_outside_allowlist_is_refused_with_typed_error(self):
        targets = (
            "https://evil.test/0/public/Ticker",
            "https://api.kraken.com.evil.test/0/public/Ticker",
            "https://api.kraken.com@evil.test/0/public/Ticker",
            "http://api.kraken.com/0/public/Ticker",
            "https://api.kraken.com:8443/0/public/Ticker",
            "/0/private/Balance",
            "../private/Balance",
            "//evil.test/x",
            "https://api.kraken.com/0/public/Ticker\\@evil.test",
            "http://localhost:11434/api/chat",
            "https://ntfy.sh/topic",
        )
        for status in (301, 302, 303, 307, 308):
            for location in targets:
                with self.subTest(status=status, location=location):
                    session, fake = guarded([FakeResponse(status, headers={"Location": location})] * 4)
                    with self.assertRaises(RedirectRefused):
                        session.get(f"{SPOT}/Ticker")
                    self.assertEqual(len(fake.calls), 1)  # neither followed nor retried

    def test_redirect_refused_is_a_security_violation_not_an_api_error(self):
        self.assertTrue(issubclass(RedirectRefused, SecurityViolation))
        self.assertFalse(issubclass(RedirectRefused, ApiError))

    def test_real_requests_session_does_not_follow_redirect_off_allowlist(self):
        session, adapter = real_session_with(
            [(302, {"Location": "https://evil.test/steal"}, {}), (200, {}, {"stolen": True})]
        )
        with self.assertRaises(RedirectRefused):
            session.get(f"{SPOT}/Ticker")
        self.assertEqual(adapter.sent, [("GET", f"{SPOT}/Ticker")])

    def test_redirect_to_allowlisted_target_is_still_not_followed(self):
        session, fake = guarded([FakeResponse(301, headers={"Location": f"{SPOT}/Ticker?pair=XBTUSD"})])
        with self.assertRaises(RedirectNotFollowed) as ctx:
            session.get(f"{SPOT}/Ticker")
        self.assertIsInstance(ctx.exception, ApiError)
        self.assertEqual(len(fake.calls), 1)

    def test_redirect_without_location_is_not_followed(self):
        session, fake = guarded([FakeResponse(302)])
        with self.assertRaises(RedirectNotFollowed):
            session.get(f"{FUTURES}/tickers")
        self.assertEqual(len(fake.calls), 1)

    def test_response_with_redirect_history_is_refused(self):
        session, fake = guarded([FakeResponse(200, {"ok": True}, history=[FakeResponse(302)])])
        with self.assertRaises(RedirectRefused):
            session.get(f"{SPOT}/Ticker")
        self.assertEqual(len(fake.calls), 1)


class TestRetryBackoffPreserved(unittest.TestCase):
    def test_429_and_5xx_are_retried_with_exponential_backoff(self):
        session, fake = guarded(
            [FakeResponse(429), FakeResponse(503), FakeResponse(500), FakeResponse(200, {"ok": 1})],
            max_retries=3,
            backoff_base=0.5,
        )
        with mock.patch("radar_v08.http_client.time.sleep") as sleep:
            response = session.get(f"{SPOT}/Ticker")
        self.assertEqual(response.json(), {"ok": 1})
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.5, 1.0, 2.0])
        self.assertTrue(all(c["allow_redirects"] is False for c in fake.calls))

    def test_exhausted_retries_raise_api_error(self):
        session, fake = guarded([FakeResponse(502)] * 3, max_retries=2, backoff_base=0.25)
        with mock.patch("radar_v08.http_client.time.sleep") as sleep:
            with self.assertRaises(ApiError) as ctx:
                session.get(f"{FUTURES}/orderbook", params={"symbol": "PF_XBTUSD"})
        self.assertIn("after 3 attempts", str(ctx.exception))
        self.assertIn("HTTP 502", str(ctx.exception))
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.5])

    def test_connection_errors_are_retried(self):
        session, fake = guarded(
            [requests.ConnectionError("boom"), FakeResponse(200, {"ok": 2})], max_retries=1, backoff_base=0.1
        )
        with mock.patch("radar_v08.http_client.time.sleep") as sleep:
            response = session.get(f"{SPOT}/Depth", params={"pair": "XBTUSD", "count": 25})
        self.assertEqual(response.json(), {"ok": 2})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.1])

    def test_other_4xx_is_not_retried(self):
        session, fake = guarded([FakeResponse(404)], max_retries=3)
        with mock.patch("radar_v08.http_client.time.sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                session.get(f"{SPOT}/Trades", params={"pair": "XBTUSD"})
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()


class TestGuardNotWidened(unittest.TestCase):
    def test_ollama_and_ntfy_hosts_are_not_in_the_allowlist(self):
        allow_hosts = set(config.HTTP_PUBLIC_ALLOWLIST)
        for host in ("localhost", "127.0.0.1", "ntfy.sh", "localhost:11434", "127.0.0.1:11434"):
            self.assertNotIn(host, allow_hosts)
        self.assertEqual(config.OLLAMA_ALLOWED_HOSTS, {"http://localhost:11434", "http://127.0.0.1:11434"})

    def test_ollama_guard_unchanged(self):
        qwen._assert_local_ollama("http://localhost:11434")
        with self.assertRaises(RuntimeError):
            qwen._assert_local_ollama("https://api.kraken.com")

    def test_only_http_client_ntfy_and_qwen_import_requests(self):
        importers = set()
        package = os.path.join(REPO_ROOT, "radar_v08")
        for dirpath, _dirs, files in os.walk(package):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=path)
                for node in ast.walk(tree):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        modules = [node.module]
                    if any(m.split(".")[0] in {"requests", "urllib3", "httpx", "aiohttp", "socket"} or m in {"urllib.request", "http.client"} for m in modules):
                        importers.add(os.path.relpath(path, REPO_ROOT).replace(os.sep, "/"))
        # T032b adds the loopback-only Ollama adapter (refuses every non-loopback host,
        # never follows redirects; see tests/test_local_inference_adapter.py).
        self.assertEqual(importers, {"radar_v08/http_client.py", "radar_v08/ntfy.py", "radar_v08/qwen.py",
                                     "radar_v08/adapters/local_inference.py"})


if __name__ == "__main__":
    unittest.main()
