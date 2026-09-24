"""T050c2a: frozen-value equivalence test for radar_v08/qwen.py + config.py (D53/D55/D56).

This test is the prerequisite of the T050c wiring (linking `qwen.py`/`config.py` to the
T050a profile loader, D55): it captures TODAY's runtime behaviour as LITERAL constants
(payload shape, URL, timeout, retry count, the local-host guard and the OK/TIMEOUT/UNAVAILABLE
states) and asserts the current code against them. It changes nothing in `radar_v08/qwen.py`,
`radar_v08/config.py`, `radar_v08/adapters/` or `radar_v08/model_profiles.toml` (D53 scope: only
tests are touched). The follow-up wiring task must keep this file green without changing any
of the frozen literals below (D55(2)).

**Why this is not tautological.** Every expected value below (`EXPECTED_*`) is a constant
written by hand from one offline run of the real code, inspected and copied in before this
test existed — never
derived from `radar_v08.config` or `radar_v08.qwen` at test time. The test only ever compares
what the CURRENT code actually sends/returns (captured through `unittest.mock` on
`requests.post`, real network never touched) against those pre-written literals. If a future
change to `qwen.py` or `config.py` alters the payload, the URL, the timeout, the retry count,
the guard or a state's error text, this test fails — it is not `assertEqual(actual, actual)`
in disguise; the constants exist independently of the code under test.

**Isolation (D31).** Every scenario runs the real `radar_v08.qwen.review_finalists` in a
FRESH CHILD PROCESS (never in this test process), with every inherited `RADAR_*` variable
removed and `RADAR_STATE_DIR` plus every `RADAR_*_PATH` found by scanning `radar_v08/config.py`
(same AST-scan technique as `tests/test_benchmark_harness.py::test_benchmark_import_loads_no_network_module`)
pointed at a fresh `tempfile.TemporaryDirectory`. `requests.post` is replaced with a Python
function (`unittest.mock.patch`) inside the child before any call: no request ever reaches a
socket, and no real Ollama server needs to run for this test to pass or fail correctly.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------------------------
# Frozen literals (obtained once, offline, from the code as it stood before this test existed).
# --------------------------------------------------------------------------------------------

# The synthetic finalist the child process feeds to review_finalists() in every scenario.
FINALISTS_FIXTURE = [{"asset": "BTC", "setup_type": "BREAKOUT", "direction": "LONG"}]

# json.dumps(payload["messages"], sort_keys=True) hashed with sha256, for FINALISTS_FIXTURE.
# Frozen because the literal system prompt string is long; the hash is exact and was captured
# from the real _build_payload() output before this test existed. Identical for the default
# and the "valid overrides" scenario, because RADAR_QWEN_MODEL/RADAR_QWEN_TIMEOUT_SECONDS/
# RADAR_OLLAMA_URL do not change SYSTEM_PROMPT or the user message content.
EXPECTED_MESSAGES_SHA256 = "75a4eefd8239de66078b20f31653471724216237c40535448217203ed9e1247a"

# The exact "format" (Ollama structured-output JSON schema) qwen.py sends today.
EXPECTED_FORMAT = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asset": {"type": "string"},
                    "setup_type": {
                        "type": "string",
                        "enum": ["BREAKOUT", "CONTINUATION", "REVERSAL", "SQUEEZE_RELEASE", "EXHAUSTION", "NONE"],
                    },
                    "direction": {"type": "string", "enum": ["LONG", "SHORT", "NONE"]},
                    "market": {"type": "string", "enum": ["SPOT", "FUTURES", "BOTH", "NONE"]},
                    "veto": {"type": "boolean"},
                    "call_sonnet": {"type": "boolean"},
                    "call_fable": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                    "reason": {"type": "string"},
                    "data_quality_notes": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "asset", "setup_type", "direction", "market", "veto",
                    "call_sonnet", "call_fable", "confidence", "reason", "data_quality_notes",
                ],
            },
        },
    },
    "required": ["reviews"],
}

# The payload's top-level keys, EXACTLY (compared as a sorted list, so an extra key such as
# keep_alive or a top-level num_ctx/num_predict fails the test just like a missing one).
EXPECTED_PAYLOAD_KEYS = ["format", "messages", "model", "options", "stream", "think"]

EXPECTED_OPTIONS = {"temperature": 0.0}
EXPECTED_THINK = False
EXPECTED_STREAM = False

# Default environment (no RADAR_QWEN_*/RADAR_OLLAMA_URL set at all).
EXPECTED_MODEL_DEFAULT = "qwen3:14b"
EXPECTED_URL_DEFAULT = "http://localhost:11434/api/chat"
EXPECTED_TIMEOUT_DEFAULT = 30.0
# JSON cannot tell 30 from 30.0, so the child also reports the Python type of the timeout
# argument passed to requests.post (config.QWEN_TIMEOUT_SECONDS is float(...) today).
EXPECTED_TIMEOUT_TYPE = "float"

# QWEN_MAX_RETRIES_ON_INVALID (1) + 1 initial attempt = 2 calls to requests.post per response
# on every failure path below.
EXPECTED_CALLS_ON_RETRY = 2

# Today's valid overrides (D55(4)): RADAR_QWEN_MODEL, RADAR_QWEN_TIMEOUT_SECONDS, RADAR_OLLAMA_URL.
OVERRIDE_ENV = {
    "RADAR_QWEN_MODEL": "qwen3:8b",
    "RADAR_QWEN_TIMEOUT_SECONDS": "20",
    "RADAR_OLLAMA_URL": "http://127.0.0.1:11434",
}
EXPECTED_MODEL_OVERRIDE = "qwen3:8b"
EXPECTED_URL_OVERRIDE = "http://127.0.0.1:11434/api/chat"
EXPECTED_TIMEOUT_OVERRIDE = 20.0

# RADAR_OLLAMA_URL values _assert_local_ollama refuses today, before any requests.post call.
# Today's guard is exact membership in {"http://localhost:11434", "http://127.0.0.1:11434"},
# so it refuses: a different port on loopback, non-loopback hosts on the allowed port (a
# public DNS name, a LAN address, the all-interfaces address), another scheme and a trailing
# slash. Each maps to its exact frozen RuntimeError message (literal, written out in full).
GUARD_REFUSED = {
    "http://127.0.0.1:9999": "Refusing to call non-local Ollama host: http://127.0.0.1:9999",
    "http://localhost:9999": "Refusing to call non-local Ollama host: http://localhost:9999",
    "http://evil.example.com:11434": "Refusing to call non-local Ollama host: http://evil.example.com:11434",
    "http://192.168.1.10:11434": "Refusing to call non-local Ollama host: http://192.168.1.10:11434",
    "http://0.0.0.0:11434": "Refusing to call non-local Ollama host: http://0.0.0.0:11434",
    "https://localhost:11434": "Refusing to call non-local Ollama host: https://localhost:11434",
    "http://localhost:11434/": "Refusing to call non-local Ollama host: http://localhost:11434/",
}

# Exact frozen error text per failing state (captured the same way as the payload above).
EXPECTED_ERROR_INVALID_JSON = "invalid JSON: Expecting value: line 1 column 1 (char 0)"
EXPECTED_ERROR_SCHEMA_INVALID = "schema validation failed: invalid setup_type: 'NOT_A_TYPE'"
EXPECTED_ERROR_TIMEOUT = "timeout: simulated timeout"
EXPECTED_ERROR_REQUEST_EXCEPTION = "request error: ollama not running"
EXPECTED_ERROR_UNKNOWN_SYMBOL = "schema validation failed: unknown symbol in review: 'ETH'"
# Mixed sequence: a timeout on the first attempt, invalid JSON on the retry -> the LAST error
# decides the state, so UNAVAILABLE (not TIMEOUT).
EXPECTED_ERROR_TIMEOUT_THEN_INVALID_JSON = EXPECTED_ERROR_INVALID_JSON


# --------------------------------------------------------------------------------------------
# Child-process probe: exercises the REAL radar_v08.qwen.review_finalists with requests.post
# replaced by a Python function (never a real socket). Never imports radar_v08.qwen in THIS
# (parent) process, so RADAR_QWEN_*/RADAR_OLLAMA_URL removal is airtight per scenario.
# --------------------------------------------------------------------------------------------

CHILD_SCRIPT = '''
import hashlib
import json
import sys
from unittest import mock

repo_root, scenario = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo_root)

import requests
from radar_v08 import qwen

FINALISTS = [{"asset": "BTC", "setup_type": "BREAKOUT", "direction": "LONG"}]


def valid_review(asset):
    return {
        "reviews": [
            {
                "asset": asset, "setup_type": "BREAKOUT", "direction": "LONG", "market": "SPOT",
                "veto": False, "call_sonnet": True, "call_fable": False, "confidence": "MEDIUM",
                "reason": "coherent breakout with volume", "data_quality_notes": [],
            }
        ]
    }


def ollama_content(reviews_obj):
    return {"message": {"content": json.dumps(reviews_obj)}}


class FakeResponse:
    def __init__(self, obj):
        self._obj = obj

    def raise_for_status(self):
        return None

    def json(self):
        return self._obj


BAD_SCHEMA = {
    "reviews": [
        {
            "asset": "BTC", "setup_type": "NOT_A_TYPE", "direction": "LONG", "market": "SPOT",
            "veto": False, "call_sonnet": True, "call_fable": False, "confidence": "MEDIUM",
            "reason": "x", "data_quality_notes": [],
        }
    ]
}

SCENARIO_REPLIES = {
    "valid": lambda: FakeResponse(ollama_content(valid_review("BTC"))),
    "override_valid": lambda: FakeResponse(ollama_content(valid_review("BTC"))),
    "invalid_json_twice": lambda: FakeResponse({"message": {"content": "not json at all"}}),
    "schema_invalid": lambda: FakeResponse(ollama_content(BAD_SCHEMA)),
    "timeout_twice": lambda: requests.Timeout("simulated timeout"),
    "request_exception": lambda: requests.ConnectionError("ollama not running"),
    "unknown_symbol": lambda: FakeResponse(ollama_content(valid_review("ETH"))),
    # If the guard ever let a refused URL through, the fake post answers validly so the test
    # fails on the asserted exception/call count (clean assertion), not on a child crash.
    "guard_bad_host": lambda: FakeResponse(ollama_content(valid_review("BTC"))),
}

SEQUENCE_REPLIES = {
    "timeout_then_invalid_json": [
        lambda: requests.Timeout("simulated timeout"),
        lambda: FakeResponse({"message": {"content": "not json at all"}}),
    ],
}

calls = []


def fake_post(url, **kwargs):
    calls.append({"url": url, "payload": kwargs.get("json"), "timeout": kwargs.get("timeout")})
    if scenario in SEQUENCE_REPLIES:
        outcome = SEQUENCE_REPLIES[scenario][len(calls) - 1]()
    else:
        outcome = SCENARIO_REPLIES[scenario]()
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


output = {}

if scenario == "empty_finalists":
    with mock.patch("requests.post", fake_post):
        result = qwen.review_finalists([])
    output["status"] = result.status
    output["error"] = result.error
    output["reviews"] = sorted(result.reviews)
elif scenario == "guard_bad_host":
    with mock.patch("requests.post", fake_post):
        try:
            qwen.review_finalists(FINALISTS)
            output["exception_type"] = None
            output["exception_message"] = None
        except RuntimeError as exc:
            output["exception_type"] = type(exc).__name__
            output["exception_message"] = str(exc)
else:
    with mock.patch("requests.post", fake_post):
        result = qwen.review_finalists(FINALISTS)
    output["status"] = result.status
    output["error"] = result.error
    output["reviews"] = sorted(result.reviews)

output["calls"] = len(calls)
if calls:
    first_payload = calls[0]["payload"]
    output["url"] = calls[0]["url"]
    output["timeout"] = calls[0]["timeout"]
    output["timeout_type"] = type(calls[0]["timeout"]).__name__
    output["payload_keys"] = sorted(first_payload.keys())
    output["model"] = first_payload.get("model")
    output["options"] = first_payload.get("options")
    output["think"] = first_payload.get("think")
    output["stream"] = first_payload.get("stream")
    output["format"] = first_payload.get("format")
    output["messages_sha256"] = hashlib.sha256(
        json.dumps(first_payload.get("messages"), sort_keys=True).encode("utf-8")
    ).hexdigest()

print(json.dumps(output))
'''


def _radar_path_env_vars() -> set[str]:
    """AST-scan radar_v08/config.py (read as text, never imported) for RADAR_*_PATH getenv
    calls, exactly like tests/test_benchmark_harness.py::test_benchmark_import_loads_no_network_module,
    so the child's environment isolates every state path, not just RADAR_STATE_DIR (D31).
    """
    config_source = (REPOSITORY_ROOT / "radar_v08" / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(config_source, filename="radar_v08/config.py")
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and re.fullmatch(r"RADAR_[A-Z0-9_]*_PATH", node.args[0].value)
        ):
            names.add(node.args[0].value)
    return names


def _run_scenario(scenario: str, extra_env: dict[str, str] | None = None) -> dict[str, object]:
    """Run one scenario of CHILD_SCRIPT in a fresh, RADAR_*-clean child process, RADAR_STATE_DIR
    and every discovered RADAR_*_PATH pointed at a fresh temporary directory (D31). Returns the
    parsed JSON the child printed.
    """
    path_vars = _radar_path_env_vars()
    with tempfile.TemporaryDirectory(prefix="t050c2a-state-") as state_dir, \
            tempfile.TemporaryDirectory(prefix="t050c2a-script-") as script_dir:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("RADAR_")}
        environment["RADAR_STATE_DIR"] = state_dir
        for name in sorted(path_vars):
            environment[name] = os.path.join(state_dir, name)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if extra_env:
            environment.update(extra_env)

        script_path = os.path.join(script_dir, "qwen_probe.py")
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(CHILD_SCRIPT)

        completed = subprocess.run(
            [sys.executable, script_path, str(REPOSITORY_ROOT), scenario],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert completed.returncode == 0, f"child failed (scenario={scenario}): {completed.stderr}"
    return json.loads(completed.stdout.strip())


class TestConfigPathScanFindsTheThreeNamedMinimums(unittest.TestCase):
    def test_scan_covers_the_task_named_variables(self) -> None:
        found = _radar_path_env_vars()
        self.assertTrue(
            {"RADAR_EVENTS_LOG_PATH", "RADAR_OUTPUT_V08_PATH", "RADAR_SQLITE_PATH"} <= found,
            f"scan of radar_v08/config.py missed some of the task's named minimums: {sorted(found)}",
        )


class TestQwenDefaultPayloadEquivalence(unittest.TestCase):
    """No RADAR_QWEN_*/RADAR_OLLAMA_URL set at all: today's default-profile payload."""

    def test_valid_reply_payload_url_timeout_and_call_count(self) -> None:
        output = _run_scenario("valid")
        self.assertEqual(output["status"], "OK")
        self.assertIsNone(output["error"])
        self.assertEqual(output["reviews"], ["BTC"])
        self.assertEqual(output["calls"], 1)  # no retry needed
        self.assertEqual(output["url"], EXPECTED_URL_DEFAULT)
        self.assertEqual(output["timeout"], EXPECTED_TIMEOUT_DEFAULT)
        self.assertEqual(output["timeout_type"], EXPECTED_TIMEOUT_TYPE)
        self.assertEqual(output["payload_keys"], EXPECTED_PAYLOAD_KEYS)
        self.assertEqual(output["model"], EXPECTED_MODEL_DEFAULT)
        self.assertEqual(output["options"], EXPECTED_OPTIONS)
        self.assertIs(output["think"], EXPECTED_THINK)
        self.assertIs(output["stream"], EXPECTED_STREAM)
        self.assertEqual(output["format"], EXPECTED_FORMAT)
        self.assertEqual(output["messages_sha256"], EXPECTED_MESSAGES_SHA256)

    def test_empty_finalists_makes_no_post_call(self) -> None:
        output = _run_scenario("empty_finalists")
        self.assertEqual(output["status"], "OK")
        self.assertIsNone(output["error"])
        self.assertEqual(output["reviews"], [])
        self.assertEqual(output["calls"], 0)
        self.assertNotIn("url", output)  # requests.post was never called


class TestQwenRetryAndStateEquivalence(unittest.TestCase):
    """Fixed set of fake replies: invalid JSON twice, schema-invalid, timeout twice,
    a persistent RequestException and an unknown symbol -> exact frozen state/error/call count.
    """

    def test_invalid_json_twice_is_unavailable(self) -> None:
        output = _run_scenario("invalid_json_twice")
        self.assertEqual(output["status"], "UNAVAILABLE")
        self.assertEqual(output["error"], EXPECTED_ERROR_INVALID_JSON)
        self.assertEqual(output["calls"], EXPECTED_CALLS_ON_RETRY)

    def test_schema_invalid_is_unavailable(self) -> None:
        output = _run_scenario("schema_invalid")
        self.assertEqual(output["status"], "UNAVAILABLE")
        self.assertEqual(output["error"], EXPECTED_ERROR_SCHEMA_INVALID)
        self.assertEqual(output["calls"], EXPECTED_CALLS_ON_RETRY)

    def test_timeout_twice_is_timeout(self) -> None:
        output = _run_scenario("timeout_twice")
        self.assertEqual(output["status"], "TIMEOUT")
        self.assertEqual(output["error"], EXPECTED_ERROR_TIMEOUT)
        self.assertEqual(output["calls"], EXPECTED_CALLS_ON_RETRY)

    def test_persistent_request_exception_is_unavailable(self) -> None:
        output = _run_scenario("request_exception")
        self.assertEqual(output["status"], "UNAVAILABLE")
        self.assertEqual(output["error"], EXPECTED_ERROR_REQUEST_EXCEPTION)
        self.assertEqual(output["calls"], EXPECTED_CALLS_ON_RETRY)

    def test_unknown_symbol_is_unavailable(self) -> None:
        output = _run_scenario("unknown_symbol")
        self.assertEqual(output["status"], "UNAVAILABLE")
        self.assertEqual(output["error"], EXPECTED_ERROR_UNKNOWN_SYMBOL)
        self.assertEqual(output["calls"], EXPECTED_CALLS_ON_RETRY)

    def test_timeout_then_invalid_json_is_unavailable(self) -> None:
        output = _run_scenario("timeout_then_invalid_json")
        self.assertEqual(output["status"], "UNAVAILABLE")
        self.assertEqual(output["error"], EXPECTED_ERROR_TIMEOUT_THEN_INVALID_JSON)
        self.assertEqual(output["calls"], EXPECTED_CALLS_ON_RETRY)


class TestQwenLocalHostGuard(unittest.TestCase):
    """RADAR_OLLAMA_URL set to a URL that is not in OLLAMA_ALLOWED_HOSTS (fixed set, not
    itself overridable): _assert_local_ollama must raise RuntimeError before any post call.
    Covers both halves of the criterion: non-loopback host on the allowed port, and a
    different port on loopback (plus scheme / trailing-slash variants of today's exact match).
    """

    def test_refused_urls_raise_before_any_post_call(self) -> None:
        for bad_url, expected_message in GUARD_REFUSED.items():
            with self.subTest(url=bad_url):
                output = _run_scenario("guard_bad_host", extra_env={"RADAR_OLLAMA_URL": bad_url})
                self.assertEqual(output["exception_type"], "RuntimeError")
                self.assertEqual(output["exception_message"], expected_message)
                self.assertEqual(output["calls"], 0)


class TestQwenValidOverridesEquivalence(unittest.TestCase):
    """D55(4): RADAR_QWEN_MODEL, RADAR_QWEN_TIMEOUT_SECONDS, RADAR_OLLAMA_URL (all valid per
    the loader's own rules: explicit tag, no cloud tag, timeout within the role limit, loopback
    endpoint) must give today's exact overridden model/URL/timeout, everything else unchanged.
    """

    def test_valid_overrides_change_only_model_url_and_timeout(self) -> None:
        output = _run_scenario("override_valid", extra_env=OVERRIDE_ENV)
        self.assertEqual(output["status"], "OK")
        self.assertIsNone(output["error"])
        self.assertEqual(output["calls"], 1)
        self.assertEqual(output["model"], EXPECTED_MODEL_OVERRIDE)
        self.assertEqual(output["url"], EXPECTED_URL_OVERRIDE)
        self.assertEqual(output["timeout"], EXPECTED_TIMEOUT_OVERRIDE)
        self.assertEqual(output["timeout_type"], EXPECTED_TIMEOUT_TYPE)
        self.assertEqual(output["payload_keys"], EXPECTED_PAYLOAD_KEYS)
        # Unaffected by these three overrides: options/think/stream/format/messages.
        self.assertEqual(output["options"], EXPECTED_OPTIONS)
        self.assertIs(output["think"], EXPECTED_THINK)
        self.assertIs(output["stream"], EXPECTED_STREAM)
        self.assertEqual(output["format"], EXPECTED_FORMAT)
        self.assertEqual(output["messages_sha256"], EXPECTED_MESSAGES_SHA256)


if __name__ == "__main__":
    unittest.main()
