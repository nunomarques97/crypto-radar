"""T032b: local Ollama adapter against a fake server on 127.0.0.1 (ephemeral port).

D21: no request ever reaches a real Ollama. Every test talks to an ``http.server`` bound
to 127.0.0.1 on a port the OS picks, or to a fake session that records calls. Covered:
fake latency and load, timeout, malformed replies (rejected without free-text parsing),
oversized bodies, HTTP errors, redirects (refused, never followed), non-loopback hosts
(refused before any request), environment proxies (ignored) and no cloud fallback.
"""

import ast
import json
import os
import re
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from radar_v08.adapters import local_inference as li
from radar_v08.adapters.local_inference import (
    EndpointRefusal,
    LocalEndpointRefused,
    OllamaLocalInference,
    loopback_base_url,
    parse_chat_reply,
)
from radar_v08.workflow.worker import (
    InferenceCall,
    InferenceFailed,
    InferenceFailure,
    InferenceReply,
)

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["KEEP", "VETO"]}}, "required": ["verdict"]}
MODEL = "fixture-profile-model:1"


def make_call(timeout=5.0, cap=768, context=4096, model=MODEL):
    return InferenceCall(
        model=model,
        system="system text",
        user="user text",
        response_schema=SCHEMA,
        output_cap_tokens=cap,
        context_tokens=context,
        think=False,
        timeout_seconds=timeout,
    )


def envelope(content, **extra):
    body = {"model": MODEL, "message": {"role": "assistant", "content": content}, "done": True, "done_reason": "stop",
            "prompt_eval_count": 120, "eval_count": 9}
    body.update(extra)
    return json.dumps(body).encode()


class Never:
    def is_set(self):
        return False


class Always:
    def is_set(self):
        return True


class FakeOllama:
    """A loopback HTTP server whose behaviour each test sets; it records every request."""

    def __init__(self):
        self.requests = []
        self.release = threading.Event()
        self.behaviour = lambda handler: handler.reply(200, envelope('{"verdict": "KEEP"}'))
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", headers=None, length=True):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                if length:
                    self.send_header("Content-Length", str(len(body)))
                else:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                if not length:
                    self.close_connection = True

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(size)
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "json": json.loads(raw or b"null")})
                fake.behaviour(self)

            def do_GET(self):
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "json": None})
                self.reply(200, b"{}")

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # a client that gave up (timeout test) resets the connection; expected

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.addCleanup(self.fake.close)
        self.adapter = OllamaLocalInference(self.fake.url)
        self.addCleanup(self.adapter.close)


class TestRequestShape(ServerCase):
    def test_valid_reply_is_parsed_json_object_with_token_counts(self):
        result = self.adapter.infer(make_call(), Never())
        self.assertIsInstance(result, InferenceReply)
        self.assertEqual(dict(result.payload), {"verdict": "KEEP"})
        self.assertEqual((result.prompt_tokens, result.output_tokens), (120, 9))

    def test_request_uses_profile_model_and_bounded_options(self):
        self.adapter.infer(make_call(cap=512, context=3000, model="another-profile:7b"), Never())
        self.assertEqual(len(self.fake.requests), 1)
        sent = self.fake.requests[0]
        self.assertEqual(sent["path"], "/api/chat")
        body = sent["json"]
        self.assertEqual(body["model"], "another-profile:7b")
        self.assertIs(body["stream"], False)
        self.assertIs(body["think"], False)
        self.assertEqual(body["format"], SCHEMA)
        self.assertEqual(body["options"], {"temperature": 0, "num_predict": 512, "num_ctx": 3000})
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(sent["headers"].get("Accept-Encoding"), "identity")

    def test_localhost_endpoint_also_reaches_the_loopback_server(self):
        # Windows resolves localhost to ::1 first; the fake (like Ollama by default) listens
        # on 127.0.0.1 only, so the ::1 attempt costs up to one connect timeout.
        adapter = OllamaLocalInference(f"http://localhost:{self.fake.port}", connect_timeout_seconds=0.5)
        self.addCleanup(adapter.close)
        self.assertIsInstance(adapter.infer(make_call(), Never()), InferenceReply)


class TestLatencyAndTimeout(ServerCase):
    def delayed(self, seconds, body=None):
        def behaviour(handler):
            self.fake.release.wait(seconds)
            handler.reply(200, body or envelope('{"verdict": "VETO"}'))
        self.fake.behaviour = behaviour

    def test_fake_load_latency_within_limit_succeeds(self):
        self.delayed(0.3)
        started = time.monotonic()
        result = self.adapter.infer(make_call(timeout=5.0), Never())
        self.assertIsInstance(result, InferenceReply)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)

    def test_latency_beyond_limit_is_timeout_within_bound(self):
        self.delayed(30.0)
        started = time.monotonic()
        result = self.adapter.infer(make_call(timeout=0.4), Never())
        elapsed = time.monotonic() - started
        self.assertEqual(result, InferenceFailed(InferenceFailure.TIMEOUT))
        self.assertLess(elapsed, 2.0)

    def test_limit_covers_the_whole_call_on_the_monotonic_clock(self):
        # Fake load: the monotonic clock says 31 s passed while the model loaded, even
        # though the server answered at once. The reply is refused, not returned.
        ticks = iter([100.0, 131.0, 131.0, 131.0])
        adapter = OllamaLocalInference(self.fake.url, monotonic=lambda: next(ticks, 131.0))
        self.addCleanup(adapter.close)
        self.assertEqual(adapter.infer(make_call(timeout=30.0), Never()), InferenceFailed(InferenceFailure.TIMEOUT))
        self.assertEqual(len(self.fake.requests), 1)

    def test_invalid_timeout_is_refused_by_the_call_type(self):
        for bad in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make_call(timeout=bad)


class TestMalformedReplies(ServerCase):
    CASES = {
        "not json": b"Sure! here is the answer",
        "envelope is a list": b"[1, 2]",
        "done false": envelope('{"verdict": "KEEP"}', done=False),
        "done missing": json.dumps({"message": {"role": "assistant", "content": '{"verdict": "KEEP"}'}}).encode(),
        "truncated by length": envelope('{"verdict": "KEEP"}', done_reason="length"),
        "message missing": json.dumps({"done": True}).encode(),
        "wrong role": json.dumps({"done": True, "message": {"role": "user", "content": "{}"}}).encode(),
        "content not text": json.dumps({"done": True, "message": {"role": "assistant", "content": {"verdict": "KEEP"}}}).encode(),
        "prose around json": envelope('The verdict is {"verdict": "KEEP"} as requested'),
        "fenced json": envelope('```json\n{"verdict": "KEEP"}\n```'),
        "content is a list": envelope('[{"verdict": "KEEP"}]'),
        "content is a string": envelope('"KEEP"'),
        "non-finite number": envelope('{"verdict": "KEEP", "score": NaN}'),
        "duplicate key": envelope('{"verdict": "KEEP", "verdict": "VETO"}'),
        "envelope duplicate key": b'{"done": true, "done": true, "message": {"role": "assistant", "content": "{}"}}',
        "eval count over cap": envelope('{"verdict": "KEEP"}', eval_count=769),
        "eval count not int": envelope('{"verdict": "KEEP"}', eval_count="9"),
        "eval count bool": envelope('{"verdict": "KEEP"}', prompt_eval_count=True),
        "invalid utf8": b'\xff\xfe{"done": true}',
    }

    def test_every_malformed_reply_is_rejected_without_text_parsing(self):
        for name, body in self.CASES.items():
            with self.subTest(case=name):
                self.fake.behaviour = lambda handler, body=body: handler.reply(200, body)
                self.assertEqual(self.adapter.infer(make_call(), Never()), InferenceFailed(InferenceFailure.MALFORMED))

    def test_parser_accepts_only_the_exact_shape(self):
        reply = parse_chat_reply(envelope('{"verdict": "KEEP"}'), 768)
        self.assertEqual(dict(reply.payload), {"verdict": "KEEP"})
        without_counts = json.dumps({"done": True, "message": {"role": "assistant", "content": "{}"}}).encode()
        self.assertEqual(parse_chat_reply(without_counts, 768), InferenceReply({}, None, None))

    def test_http_error_status_is_typed(self):
        for status in (400, 404, 500, 503):
            with self.subTest(status=status):
                self.fake.behaviour = lambda handler, status=status: handler.reply(status, b'{"error": "model not found"}')
                self.assertEqual(self.adapter.infer(make_call(), Never()), InferenceFailed(InferenceFailure.HTTP_STATUS))


class TestSizeCap(ServerCase):
    def test_declared_oversized_body_is_refused(self):
        adapter = OllamaLocalInference(self.fake.url, max_response_bytes=1024)
        self.addCleanup(adapter.close)
        big = envelope(json.dumps({"verdict": "KEEP", "pad": "x" * 5000}))
        self.fake.behaviour = lambda handler: handler.reply(200, big)
        self.assertEqual(adapter.infer(make_call(), Never()), InferenceFailed(InferenceFailure.TOO_LARGE))

    def test_undeclared_oversized_body_is_refused_while_reading(self):
        adapter = OllamaLocalInference(self.fake.url, max_response_bytes=1024)
        self.addCleanup(adapter.close)
        big = envelope(json.dumps({"verdict": "KEEP", "pad": "x" * 50000}))
        self.fake.behaviour = lambda handler: handler.reply(200, big, length=False)
        self.assertEqual(adapter.infer(make_call(), Never()), InferenceFailed(InferenceFailure.TOO_LARGE))


class TestRedirects(ServerCase):
    def test_redirect_is_refused_and_never_followed(self):
        target = FakeOllama()
        self.addCleanup(target.close)
        for status in (301, 302, 303, 307, 308):
            for location in (f"{target.url}/api/chat", "https://ollama.com/api/chat", "http://10.0.0.5:11434/api/chat"):
                with self.subTest(status=status, location=location):
                    self.fake.behaviour = lambda handler, s=status, loc=location: handler.reply(s, b"", {"Location": loc})
                    result = self.adapter.infer(make_call(), Never())
                    self.assertEqual(result, InferenceFailed(InferenceFailure.REDIRECT_REFUSED))
        self.assertEqual(target.requests, [])

    def test_post_is_sent_with_redirects_disabled(self):
        session = RecordingSession()
        adapter = OllamaLocalInference("http://127.0.0.1:11434", session=session)
        adapter.infer(make_call(), Never())
        self.assertEqual(len(session.posts), 1)
        url, kwargs = session.posts[0]
        self.assertEqual(url, "http://127.0.0.1:11434/api/chat")
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"], (2.0, 5.0))


class RecordingSession(requests.Session):
    """No network: every post raises a connection error after being recorded."""

    def __init__(self):
        super().__init__()
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        raise requests.ConnectionError("fake: connection refused")


class TestLoopbackOnly(unittest.TestCase):
    REFUSED = {
        "https://127.0.0.1:11434": EndpointRefusal.NOT_HTTP,
        "ftp://127.0.0.1:11434": EndpointRefusal.NOT_HTTP,
        "HTTP://127.0.0.1:11434": EndpointRefusal.NOT_HTTP,
        "http://ollama.com:11434": EndpointRefusal.NOT_LOOPBACK,
        "http://api.ollama.com": EndpointRefusal.NOT_LOOPBACK,
        "http://10.0.0.5:11434": EndpointRefusal.NOT_LOOPBACK,
        "http://192.168.1.20:11434": EndpointRefusal.NOT_LOOPBACK,
        "http://0.0.0.0:11434": EndpointRefusal.NOT_LOOPBACK,
        "http://127.0.0.2:11434": EndpointRefusal.NOT_LOOPBACK,
        "http://[::1]:11434": EndpointRefusal.NOT_LOOPBACK,
        "http://localhost.evil.example:11434": EndpointRefusal.NOT_LOOPBACK,
        "http://127.0.0.1.nip.io:11434": EndpointRefusal.NOT_LOOPBACK,
        "http://LOCALHOST:11434": EndpointRefusal.NOT_LOOPBACK,
        "http://localhost.:11434": EndpointRefusal.NOT_LOOPBACK,
        "http://127.0.0.1:011434": EndpointRefusal.NOT_LOOPBACK,
        "http://user@127.0.0.1:11434": EndpointRefusal.USERINFO,
        "http://evil.example@127.0.0.1:11434": EndpointRefusal.USERINFO,
        "http://127.0.0.1": EndpointRefusal.PORT_REQUIRED,
        "http://localhost": EndpointRefusal.PORT_REQUIRED,
        "http://127.0.0.1:0": EndpointRefusal.PORT_REQUIRED,
        "http://127.0.0.1:11434/api": EndpointRefusal.EXTRA_PARTS,
        "http://127.0.0.1:11434/?next=http://ollama.com": EndpointRefusal.EXTRA_PARTS,
        "http://127.0.0.1:11434#x": EndpointRefusal.EXTRA_PARTS,
        "http://127.0.0.1:99999": EndpointRefusal.MALFORMED_URL,
        "http://127.0.0.1:11434\n": EndpointRefusal.MALFORMED_URL,
        "http://127.0.0.1\\@ollama.com:11434": EndpointRefusal.MALFORMED_URL,
        "": EndpointRefusal.MALFORMED_URL,
    }

    def test_non_loopback_hosts_are_refused_before_any_request(self):
        for url, code in self.REFUSED.items():
            with self.subTest(url=url):
                session = RecordingSession()
                with self.assertRaises(LocalEndpointRefused) as caught:
                    OllamaLocalInference(url, session=session)
                self.assertIs(caught.exception.code, code)
                self.assertEqual(session.posts, [])

    def test_non_text_endpoint_is_refused(self):
        for bad in (None, 11434, b"http://127.0.0.1:11434"):
            with self.subTest(bad=bad), self.assertRaises(LocalEndpointRefused):
                loopback_base_url(bad)

    def test_only_the_two_loopback_names_are_accepted(self):
        self.assertEqual(loopback_base_url("http://127.0.0.1:11434"), "http://127.0.0.1:11434")
        self.assertEqual(loopback_base_url("http://localhost:11434/"), "http://localhost:11434")
        self.assertEqual(li.LOOPBACK_HOSTS, frozenset({"127.0.0.1", "localhost"}))

    def test_unavailable_server_has_no_fallback(self):
        session = RecordingSession()
        adapter = OllamaLocalInference("http://127.0.0.1:11434", session=session)
        self.assertEqual(adapter.infer(make_call(), Never()), InferenceFailed(InferenceFailure.UNAVAILABLE))
        self.assertEqual([url for url, _ in session.posts], ["http://127.0.0.1:11434/api/chat"])

    def test_module_names_no_other_endpoint(self):
        with open(li.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        literals = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        hosts = {host for text in literals for host in re.findall(r"://([^/:<{\s`]+)", text)}
        self.assertLessEqual(hosts, {"127.0.0.1", "localhost"})
        self.assertIn("127.0.0.1", hosts)


class TestConnectionAndEnvironment(ServerCase):
    def test_closed_port_is_unavailable(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        adapter = OllamaLocalInference(f"http://127.0.0.1:{port}", connect_timeout_seconds=0.5)
        self.addCleanup(adapter.close)
        # Refused (POSIX) or unanswered within the connect timeout (Windows retries a
        # refused loopback SYN): either way the server is not there.
        self.assertEqual(adapter.infer(make_call(timeout=5.0), Never()), InferenceFailed(InferenceFailure.UNAVAILABLE))

    def test_environment_proxies_are_ignored(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
        probe.close()
        proxies = {"HTTP_PROXY": f"http://127.0.0.1:{dead}", "http_proxy": f"http://127.0.0.1:{dead}",
                   "ALL_PROXY": f"http://127.0.0.1:{dead}", "NO_PROXY": "", "no_proxy": ""}
        with mock.patch.dict(os.environ, proxies):
            adapter = OllamaLocalInference(self.fake.url)
            self.addCleanup(adapter.close)
            self.assertIsInstance(adapter.infer(make_call(), Never()), InferenceReply)
        self.assertFalse(adapter._session.trust_env)
        self.assertEqual(len(self.fake.requests), 1)

    def test_cancel_before_the_call_sends_nothing(self):
        self.assertEqual(self.adapter.infer(make_call(), Always()), InferenceFailed(InferenceFailure.CANCELLED))
        self.assertEqual(self.fake.requests, [])

    def test_cancel_seen_while_reading_discards_the_reply(self):
        class AfterHeaders:
            def __init__(self):
                self.checks = 0

            def is_set(self):
                self.checks += 1
                return self.checks > 1

        self.assertEqual(self.adapter.infer(make_call(), AfterHeaders()), InferenceFailed(InferenceFailure.CANCELLED))

    def test_unencodable_call_is_refused_before_sending(self):
        call = InferenceCall(MODEL, "s", "u", {"bad": float("nan")}, 10, 100, False, 5.0)
        self.assertEqual(self.adapter.infer(call, Never()), InferenceFailed(InferenceFailure.INVALID_REQUEST))
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
