"""T051c runner tests: fake Ollama on 127.0.0.1 (ephemeral port) and a fake subprocess only.

No test contacts the real Ollama (port 11434): every ``OllamaControl`` built while these tests
run goes through ``_guarded_base_url``, which fails the test on port 11434. The locked corpus
in ``benchmarks/oc1_screener_v1`` is read (never written); holdout case text is only compared
by sha256 inside the test and never printed. Raw results go to temporary directories.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(REPOSITORY_ROOT))

from radar_v08.adapters import t051_ollama  # noqa: E402
from radar_v08.adapters.local_inference import LocalEndpointRefused  # noqa: E402
from radar_v08.adapters.t051_ollama import (  # noqa: E402
    NVIDIA_SMI_ARGS,
    TASKLIST_ARGS,
    NetworkRefusal,
    OllamaControl,
    ResourceProbe,
    T051NetworkRefused,
    parse_nvidia_smi,
    parse_tasklist,
    resource_report,
)
from radar_v08.workflow import t051_sequence as seq  # noqa: E402
from radar_v08.workflow.t051_sequence import (  # noqa: E402
    Block,
    CaseRef,
    ProbeObservation,
    ResultsCorrupt,
    SealedCaseError,
    SequenceState,
    parse_results,
    plan,
    probe_stats,
    select_candidates,
)

_SPEC = importlib.util.spec_from_file_location("run_t051_block", REPOSITORY_ROOT / "scripts" / "run_t051_block.py")
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
sys.modules["run_t051_block"] = runner
_SPEC.loader.exec_module(runner)

CORPUS = REPOSITORY_ROOT / "benchmarks" / "oc1_screener_v1"
GIB = 1024**3
REAL_OLLAMA_PORT = 11434
FAKE_TOOLS = {"nvidia-smi": r"C:\FakeSystem32\nvidia-smi.exe", "tasklist": r"C:\FakeSystem32\tasklist.exe"}
DIGESTS = {
    "qwen3:14b": "a" * 64,
    "gpt-oss:20b": "b" * 64,
    "llama3.2:latest": "c" * 64,
    "qwen3-coder:30b": "d" * 64,
}
CAPABILITIES = {
    "qwen3:14b": ["completion", "thinking", "tools"],
    "gpt-oss:20b": ["completion", "thinking", "tools"],
    "llama3.2:latest": ["completion", "tools"],
    "qwen3-coder:30b": ["completion", "tools"],
}
_original_base_url = t051_ollama.t051_base_url


def _guarded_base_url(url: object) -> str:
    base = _original_base_url(url)
    if urlsplit(base).port == REAL_OLLAMA_PORT:
        raise AssertionError("a test tried to reach the real Ollama port 11434")
    return base


_GUARD = mock.patch.object(t051_ollama, "t051_base_url", _guarded_base_url)


def setUpModule() -> None:
    _GUARD.start()


def tearDownModule() -> None:
    _GUARD.stop()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_CORPUS_CACHE: dict[str, object] = {}


def corpus():
    if "corpus" not in _CORPUS_CACHE:
        _CORPUS_CACHE["corpus"] = runner.load_corpus(CORPUS, runner.EXPECTED_LOCK_SHA256)
    return _CORPUS_CACHE["corpus"]


def prompt_index() -> dict[str, str]:
    """sha256 of (system, user) -> case id, to identify what the fake server received."""
    data = corpus()
    return {_sha(case.system + "\x00" + case.user): case_id for case_id, case in data.cases.items()}


def valid_answer(abstain: bool = True) -> str:
    return json.dumps(
        {
            "abstain": abstain,
            "classification": "insufficient_evidence" if abstain else "admissible_no_edge",
            "cited_evidence_ids": ["ev-bars"],
            "rationale": "",
        }
    )


class FakeOllama:
    """A loopback server that speaks the five allowed routes and records every request."""

    def __init__(self, clock: FakeClock | None = None, seconds_per_chat: float = 0.0) -> None:
        self.requests: list[dict[str, object]] = []
        self.installed = dict(DIGESTS)
        self.loaded: dict[str, str] = {}
        self.foreign: list[str] = []
        self.ps_digest_override: dict[str, str] = {}
        self.chat_status = 200
        self.chat_body: bytes | None = None
        self.clock = clock
        self.seconds_per_chat = seconds_per_chat
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status: int, document: object) -> None:
                body = document if isinstance(document, bytes) else json.dumps(document).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> object:
                size = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(size)
                return json.loads(raw) if raw else None

            def do_GET(self):
                with fake.lock:
                    fake.requests.append({"method": "GET", "path": self.path, "json": None})
                    if self.path == "/api/tags":
                        models = [{"name": n, "model": n, "digest": d} for n, d in fake.installed.items()]
                        return self.reply(200, {"models": models})
                    if self.path == "/api/ps":
                        models = [
                            {
                                "name": n,
                                "model": n,
                                "digest": fake.ps_digest_override.get(n, d),
                                "size": 10 * GIB,
                                "size_vram": 9 * GIB,
                            }
                            for n, d in fake.loaded.items()
                        ]
                        models += [{"name": n, "model": n, "digest": "e" * 64, "size": GIB, "size_vram": GIB} for n in fake.foreign]
                        return self.reply(200, {"models": models})
                return self.reply(404, {"error": "not found"})

            def do_POST(self):
                body = self._body()
                with fake.lock:
                    fake.requests.append({"method": "POST", "path": self.path, "json": body})
                    if self.path == "/api/show":
                        model = body["model"]
                        if model not in fake.installed:
                            return self.reply(404, {"error": "model not found"})
                        return self.reply(
                            200,
                            {
                                "details": {"quantization_level": "Q4_K_M", "parameter_size": "14B", "family": "fam", "format": "gguf"},
                                "model_info": {"fam.context_length": 40960},
                                "parameters": "stop <end>",
                                "capabilities": CAPABILITIES[model],
                            },
                        )
                    if self.path == "/api/generate":
                        if set(body) == {"model", "keep_alive"} and body["keep_alive"] == 0:
                            fake.loaded.pop(body["model"], None)
                            return self.reply(200, {"model": body["model"], "done": True, "done_reason": "unload"})
                        return self.reply(400, {"error": "unexpected generate"})
                    if self.path == "/api/chat":
                        model = body["model"]
                        fake.loaded[model] = fake.installed[model]
                        if fake.clock is not None:
                            fake.clock.advance(fake.seconds_per_chat)
                        if fake.chat_body is not None:
                            return self.reply(fake.chat_status, fake.chat_body)
                        envelope = {
                            "model": model,
                            "message": {"role": "assistant", "content": valid_answer()},
                            "done": True,
                            "done_reason": "stop",
                            "total_duration": 900_000_000,
                            "load_duration": 100_000_000,
                            "prompt_eval_count": 600,
                            "prompt_eval_duration": 200_000_000,
                            "eval_count": 40,
                            "eval_duration": 500_000_000,
                        }
                        return self.reply(200, envelope)
                return self.reply(404, {"error": "not found"})

            def do_DELETE(self):
                with fake.lock:
                    fake.requests.append({"method": "DELETE", "path": self.path, "json": None})
                return self.reply(404, {"error": "not found"})

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def paths(self) -> list[tuple[str, str]]:
        return [(item["method"], item["path"]) for item in self.requests]

    def chats(self) -> list[dict[str, object]]:
        return [item["json"] for item in self.requests if item["path"] == "/api/chat"]


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0
        self.lock = threading.Lock()

    def monotonic(self) -> float:
        with self.lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.value += seconds


class FakeProcesses:
    """Stands in for subprocess.run; records every argument list and keyword."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.gpu_output: str | Exception = "3205, 16311\n"
        self.tasklist_output: str | Exception = (
            '"Image Name","PID","Session Name","Session#","Mem Usage"\r\n'
            '"ollama.exe","1111","Console","1","45,000 K"\r\n'
            '"ollama.exe","2222","Console","1","1,000,000 K"\r\n'
        )
        self.returncode = 0

    def __call__(self, args, *, capture_output, text, timeout, check, shell):
        self.calls.append((list(args), {"capture_output": capture_output, "text": text, "timeout": timeout, "check": check, "shell": shell}))
        output = self.gpu_output if args[0] == FAKE_TOOLS["nvidia-smi"] else self.tasklist_output
        if isinstance(output, Exception):
            raise output
        return subprocess.CompletedProcess(list(args), self.returncode, stdout=output, stderr="")


class RunnerCase(unittest.TestCase):
    seconds_per_chat = 0.0

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.fake = FakeOllama(self.clock, self.seconds_per_chat)
        self.addCleanup(self.fake.close)
        self.processes = FakeProcesses()
        self.free_ram: int | None = 20 * GIB
        temp = tempfile.mkdtemp(prefix="crypto-radar-t051-test-")
        self.addCleanup(shutil.rmtree, temp, True)
        self.out = Path(temp) / "out"

    def runtime(self, **overrides) -> object:
        values = {
            "base_url": self.fake.url,
            "probe": ResourceProbe(run=self.processes, free_ram=lambda: self.free_ram, locate=FAKE_TOOLS.get),
            "clock": runner.Clock(monotonic=self.clock.monotonic),
            "sleep": lambda seconds: None,
        }
        values.update(overrides)
        return runner.Runtime(**values)

    def invoke(self, budget: int = 540, **kwargs):
        runtime = kwargs.pop("runtime", None) or self.runtime()
        return runner.run_invocation(CORPUS, self.out, budget, runtime=runtime, **kwargs)

    def results_path(self) -> Path:
        return self.out / "results" / "t051_results.jsonl"

    def records(self, record_type: str = "call") -> list[dict[str, object]]:
        text = self.results_path().read_text(encoding="utf-8")
        return [record for record in parse_results(text).records if record.get("record_type") == record_type]


# -- network guard ---------------------------------------------------------------------------------


class TestNetworkGuard(RunnerCase):
    def test_routes_outside_the_allowlist_are_refused_before_any_request(self):
        control = OllamaControl(self.fake.url)
        self.addCleanup(control.close)
        for method, path in (
            ("POST", "/api/pull"),
            ("POST", "/api/push"),
            ("POST", "/api/create"),
            ("DELETE", "/api/delete"),
            ("POST", "/api/copy"),
            ("GET", "/api/version"),
            ("GET", "/api/chat"),
            ("POST", "/api/tags"),
            ("POST", "/api/chat/../pull"),
        ):
            with self.subTest(method=method, path=path):
                with self.assertRaises(T051NetworkRefused) as caught:
                    control._request(method, path, {"model": "qwen3:14b"}, 5.0, 1024)
                self.assertIs(caught.exception.code, NetworkRefusal.ROUTE_NOT_ALLOWED)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(control.sent, [])

    def test_allowed_routes_reach_the_fake_server(self):
        control = OllamaControl(self.fake.url)
        self.addCleanup(control.close)
        self.assertEqual(set(control.tags()), set(DIGESTS))
        self.assertEqual(control.ps(), ())
        self.assertEqual(control.show("qwen3:14b").quantization, "Q4_K_M")
        self.assertEqual(self.fake.paths(), [("GET", "/api/tags"), ("GET", "/api/ps"), ("POST", "/api/show")])

    def test_non_loopback_and_non_127_hosts_are_refused(self):
        for url, error in (
            ("http://10.0.0.5:11434", LocalEndpointRefused),
            ("http://example.com:11434", LocalEndpointRefused),
            ("https://127.0.0.1:11434", LocalEndpointRefused),
            ("http://127.0.0.1:11434/api", LocalEndpointRefused),
            ("http://user@127.0.0.1:11434", LocalEndpointRefused),
            ("http://127.0.0.1", LocalEndpointRefused),
            ("http://localhost:" + self.fake.url.rsplit(":", 1)[1], T051NetworkRefused),
        ):
            with self.subTest(url=url):
                with self.assertRaises(error):
                    OllamaControl(url)
        self.assertEqual(self.fake.requests, [])

    def test_the_cli_only_ever_targets_127_0_0_1_11434(self):
        self.assertEqual(t051_ollama.OLLAMA_BASE_URL, "http://127.0.0.1:11434")
        self.assertEqual(runner.Runtime.__dataclass_fields__["base_url"].default, "http://127.0.0.1:11434")
        options = set(vars(runner.parse_arguments(["--corpus", "c", "--out", "o", "--budget-seconds", "1"])))
        self.assertEqual(options, {"corpus", "out", "budget_seconds", "include_optional", "write_freeze"})

    def test_the_radar_runtime_never_imports_the_t051_modules(self):
        own = {"radar_v08/adapters/t051_ollama.py", "radar_v08/workflow/t051_sequence.py"}
        sources = [REPOSITORY_ROOT / "radar.py", *(REPOSITORY_ROOT / "radar_v08").rglob("*.py"), *(REPOSITORY_ROOT / "ui").rglob("*.py")]
        for path in sources:
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            if relative in own:
                continue
            with self.subTest(relative):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("t051_ollama", text)
                self.assertNotIn("t051_sequence", text)

    def test_the_guard_of_this_test_module_refuses_port_11434(self):
        with self.assertRaises(AssertionError):
            OllamaControl("http://127.0.0.1:11434")

    def test_no_subprocess_other_than_the_two_measurement_tools(self):
        self.assertEqual(NVIDIA_SMI_ARGS, ("nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"))
        self.assertEqual(TASKLIST_ARGS, ("tasklist", "/FI", "IMAGENAME eq ollama.exe", "/FO", "CSV"))
        for relative in ("radar_v08/adapters/t051_ollama.py", "radar_v08/workflow/t051_sequence.py", "scripts/run_t051_block.py"):
            source = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("shell=True", source, relative)
            self.assertNotIn("os.system", source, relative)
            self.assertNotIn("Popen", source, relative)
            self.assertNotIn('"ollama"', source, relative)
        self.assertNotIn("subprocess", (REPOSITORY_ROOT / "scripts/run_t051_block.py").read_text(encoding="utf-8"))


class TestOllamaDownAndBusy(RunnerCase):
    def test_ollama_down_is_blocked_with_its_own_exit_code(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        status, summary = self.invoke(runtime=self.runtime(base_url=f"http://127.0.0.1:{closed_port}"))
        self.assertIs(status, runner.Status.BLOCKED)
        self.assertEqual(status.value, 4)
        self.assertEqual(summary["exit_code"], 4)
        self.assertIn("not answering", summary["reason"])
        self.assertEqual(self.records(), [])

    def test_busy_ps_blocks_without_unloading_anything(self):
        self.fake.foreign = ["someone-else:7b"]
        self.fake.loaded = {"qwen3:14b": DIGESTS["qwen3:14b"]}  # even a benchmark model loaded by someone else
        status, summary = self.invoke()
        self.assertIs(status, runner.Status.BLOCKED)
        self.assertIn("/api/ps is not empty", summary["reason"])
        self.assertEqual(self.fake.paths(), [("GET", "/api/tags"), ("GET", "/api/ps")])
        self.assertEqual(self.fake.chats(), [])
        self.assertEqual(self.fake.loaded, {"qwen3:14b": DIGESTS["qwen3:14b"]})

    def test_a_foreign_model_appearing_mid_run_blocks_and_is_not_unloaded(self):
        control_ps = OllamaControl.ps
        seen = {"count": 0}

        def ps_with_intruder(control):
            models = control_ps(control)
            seen["count"] += 1
            if seen["count"] == 4:  # after the check before the first probe call
                self.fake.foreign = ["radar-other:1b"]
            return models

        with mock.patch.object(OllamaControl, "ps", ps_with_intruder):
            status, summary = self.invoke()
        self.assertIs(status, runner.Status.BLOCKED)
        self.assertIn("radar-other:1b", summary["reason"])
        self.assertIn("it was not unloaded", summary["reason"])
        unloads = [item["json"]["model"] for item in self.fake.requests if item["path"] == "/api/generate"]
        self.assertEqual(unloads, ["qwen3:14b"])  # only the benchmark model this invocation loaded
        self.assertEqual(self.fake.foreign, ["radar-other:1b"])


# -- full sequence, resume, sealing ---------------------------------------------------------------


class TestFullSequenceAndResume(RunnerCase):
    seconds_per_chat = 5.0

    def test_resume_after_cuts_is_idempotent_and_never_sends_a_sealed_case(self):
        index = prompt_index()
        data = corpus()
        sealed = set(data.lists.holdout_sealed)
        self.assertEqual(len(sealed), 80)
        invocations = []
        for attempt in range(40):
            status, summary = self.invoke()
            invocations.append(status)
            if status is runner.Status.COMPLETE:
                break
            self.assertIs(status, runner.Status.PARTIAL, summary.get("reason"))
            if attempt == 1:
                with open(self.results_path(), "ab") as handle:  # a write cut in the middle of a line
                    handle.write(b'{"record_type":"call","key":{"block":"probe"')
        self.assertIs(invocations[-1], runner.Status.COMPLETE)
        self.assertGreater(len(invocations), 3)
        summaries = self.records("invocation")
        self.assertEqual([item["truncated_line_reported"] is not None for item in summaries[:4]], [False, False, True, False])
        self.assertEqual(len(self.records("truncated_tail")), 1)

        calls = self.records()
        keys = [tuple(record["key"].values()) for record in calls]
        self.assertEqual(len(keys), len(set(keys)))
        measured = [record for record in calls if not record["warmup"]]
        by_group: dict[tuple[str, str], int] = {}
        for record in measured:
            group = (record["key"]["block"], record["key"]["model"])
            by_group[group] = by_group.get(group, 0) + 1
        # All three probe models pass; gpt-oss:20b wins the tie on validity/p95/memory by name.
        expected = {
            ("probe", "qwen3:14b"): 20, ("probe", "gpt-oss:20b"): 20, ("probe", "llama3.2:latest"): 20,
            ("cold", "qwen3:14b"): 10, ("warm", "qwen3:14b"): 20, ("holdout", "qwen3:14b"): 120, ("stability", "qwen3:14b"): 60,
            ("cold", "gpt-oss:20b"): 10, ("warm", "gpt-oss:20b"): 20, ("holdout", "gpt-oss:20b"): 120, ("stability", "gpt-oss:20b"): 60,
        }
        self.assertEqual(by_group, expected)

        received = [index[_sha(chat["messages"][0]["content"] + "\x00" + chat["messages"][1]["content"])] for chat in self.fake.chats()]
        self.assertEqual(len(received), len(calls))  # one request per recorded call: nothing sent twice
        self.assertFalse(sealed & set(received), "a sealed holdout case reached the model")
        holdout_sent = {case_id for case_id in received if data.cases[case_id].partition.value == "holdout"}
        self.assertEqual(holdout_sent, set(data.lists.holdout_labelled))
        probe_sent = {record["key"]["case_id"] for record in calls if record["key"]["block"] == "probe"}
        self.assertEqual(probe_sent, set(data.lists.probe))
        self.assertTrue(all(data.refs[case_id].labelled and data.refs[case_id].partition == "development" for case_id in probe_sent))
        self.assertNotIn(("POST", "/api/pull"), self.fake.paths())
        self.assertTrue({path for _, path in self.fake.paths()} <= {"/api/tags", "/api/ps", "/api/show", "/api/generate", "/api/chat"})

        final = summaries[-1]
        self.assertEqual(final["status"], "COMPLETE")
        self.assertEqual(final["pending"], [])
        self.assertEqual(final["selection"]["candidates"], ["qwen3:14b", "gpt-oss:20b"])
        self.assertEqual(self.fake.loaded, {})  # unloaded at the end
        # A further invocation does nothing: every key is done.
        before = len(self.fake.chats())
        status, summary = self.invoke()
        self.assertIs(status, runner.Status.COMPLETE)
        self.assertEqual(len(self.fake.chats()), before)
        self.assertEqual(summary["model_calls_this_invocation"], 0)

        # Every record carries the freeze hashes; warm-ups follow each (re)load; cold loads start unloaded.
        freeze = json.loads((CORPUS / "t051_freeze.json").read_text(encoding="utf-8"))
        for record in calls:
            self.assertEqual(record["freeze_sha256"], seq.canonical_sha256(freeze))
            self.assertEqual(record["freeze_hashes"], freeze["hashes"])
        cold = [record for record in calls if record["key"]["block"] == "cold"]
        self.assertTrue(all(record["resources_before_load"] is not None for record in cold))
        self.assertTrue(all(record["key"]["case_id"] == data.lists.warmup[0] for record in calls if record["warmup"]))

    def test_optional_model_only_with_the_flag_and_after_every_mandatory_step(self):
        state = SequenceState(
            lists=corpus().lists,
            digests=DIGESTS,
            done=frozenset(),
            probe_observations={},
            include_optional=True,
        )
        progress = plan(state)
        self.assertEqual(progress.next_step.model, "qwen3:14b")
        self.assertIn("qwen3-coder:30b waits until every mandatory step is complete", progress.notes)
        without_flag = plan(SequenceState(corpus().lists, DIGESTS, frozenset(), {}, False))
        self.assertNotIn("qwen3-coder:30b", {group.model for group in without_flag.groups})


class TestBudget(RunnerCase):
    seconds_per_chat = 20.0

    def test_no_call_starts_when_the_remaining_time_cannot_cover_a_30_s_call(self):
        status, summary = self.invoke(budget=100)
        self.assertIs(status, runner.Status.PARTIAL)
        self.assertEqual(status.value, 3)
        # 100 s: warm-up + probe call (needs 75 s), then one more probe call (needs 45 s of the
        # 60 s left); 40 s left < 45 s, so no third call starts.
        self.assertEqual(len(self.fake.chats()), 3)
        blocks = [(record["key"]["block"], record["key"]["model"]) for record in self.records()]
        self.assertEqual(blocks, [("warmup", "qwen3:14b"), ("probe", "qwen3:14b"), ("probe", "qwen3:14b")])
        self.assertEqual(summary["status"], "PARTIAL")
        self.assertIn({"block": "probe", "model": "qwen3:14b", "done": 2, "total": 20}, summary["pending"])
        self.assertEqual(self.fake.loaded, {})  # unloaded at the end of the invocation

    def test_budget_outside_1_to_540_is_refused(self):
        for budget in (0, 541, 10_000, -1):
            with self.subTest(budget=budget):
                status, _ = self.invoke(budget=budget)
                self.assertIs(status, runner.Status.REFUSED)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(runner.main(["--corpus", os.fspath(CORPUS), "--out", os.fspath(self.out), "--budget-seconds", "600"]), 2)
        self.assertEqual(self.fake.requests, [])


# -- request parameters, unloading, measurements ----------------------------------------------------


class TestRequestsAndResources(RunnerCase):
    seconds_per_chat = 20.0

    def test_parameters_sent_are_the_profile_ones_and_are_recorded(self):
        self.invoke(budget=100)
        chats = self.fake.chats()
        for chat in chats:
            self.assertEqual(chat["options"], {"temperature": 0, "num_predict": 768, "num_ctx": 4096})
            self.assertIs(chat["think"], False)  # qwen3:14b has the thinking capability
            self.assertEqual(chat["keep_alive"], 120)
            self.assertIs(chat["stream"], False)
        record = self.records()[1]
        self.assertEqual(record["request"]["options"], chats[1]["options"])
        self.assertEqual(record["request"]["think"], False)
        self.assertEqual(record["request"]["timeout_seconds"], 30.0)
        self.assertEqual(record["request"]["messages_sha256"], hashlib.sha256(json.dumps(chats[1]["messages"], sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        self.assertEqual(record["timing"]["eval_count"], 40)
        self.assertEqual(record["harness"]["outcome"], "accepted")
        self.assertEqual(record["repair"], {"policy": "none", "attempted": False})

    def test_think_is_left_out_for_a_model_without_the_thinking_capability(self):
        control = OllamaControl(self.fake.url)
        self.addCleanup(control.close)
        inference = t051_ollama.T051Inference(control, {"llama3.2:latest": ("completion",), "qwen3:14b": ("thinking",)})
        from radar_v08.workflow.benchmark import SCREENER_OUTPUT_SCHEMA
        from radar_v08.workflow.worker import InferenceCall

        call = InferenceCall("llama3.2:latest", "s", "u", SCREENER_OUTPUT_SCHEMA, 768, 4096, False, 30.0)
        self.assertNotIn("think", inference.request_for(call))
        call = InferenceCall("qwen3:14b", "s", "u", SCREENER_OUTPUT_SCHEMA, 768, 4096, False, 30.0)
        self.assertIs(inference.request_for(call)["think"], False)

    def test_resources_are_measured_with_fixed_argument_lists_without_a_shell(self):
        self.invoke(budget=100)
        self.assertTrue(self.processes.calls)
        for args, kwargs in self.processes.calls:
            # Run by absolute path, never by bare name (no current-directory / PATH search).
            self.assertIn(tuple(args), ((FAKE_TOOLS["nvidia-smi"], *NVIDIA_SMI_ARGS[1:]), (FAKE_TOOLS["tasklist"], *TASKLIST_ARGS[1:])))
            self.assertEqual(kwargs, {"capture_output": True, "text": True, "timeout": 5, "check": False, "shell": False})
        resources = self.records()[1]["resources"]
        self.assertEqual(resources["gpu_used_mib"], 3205)
        self.assertEqual(resources["gpu_total_mib"], 16311)
        self.assertAlmostEqual(resources["free_vram_gib"], (16311 - 3205) / 1024)
        self.assertEqual(resources["ollama_process_ram_bytes"], (45_000 + 1_000_000) * 1024)
        self.assertEqual(resources["ollama_ps_size_bytes"], 10 * GIB)
        self.assertEqual(resources["ollama_ps_size_vram_bytes"], 9 * GIB)
        self.assertEqual(resources["not_measured"], [])

    def test_failed_tools_are_not_measured_never_zero_nor_a_previous_value(self):
        self.invoke(budget=100)  # first invocation: everything measured
        self.processes.gpu_output = FileNotFoundError("nvidia-smi")
        self.processes.tasklist_output = subprocess.TimeoutExpired(["tasklist"], 5)
        self.free_ram = None
        self.clock.advance(1)
        self.invoke(budget=100)
        records = self.records()
        first, last = records[1]["resources"], records[-1]["resources"]
        self.assertEqual(first["gpu_used_mib"], 3205)
        for name in ("gpu_used_mib", "gpu_total_mib", "free_vram_gib", "free_ram_bytes", "free_ram_gib", "ollama_process_ram_bytes"):
            self.assertIsNone(last[name], name)
            self.assertIn(name, last["not_measured"])
        self.assertEqual(records[-1]["harness"]["outcome"], "resources_unreported")
        self.assertIsNone(records[-1]["harness"]["min_free_vram_gib"])

    def test_unreadable_tool_output_is_not_measured(self):
        self.assertIsNone(parse_nvidia_smi(""))
        self.assertIsNone(parse_nvidia_smi("[N/A], [N/A]"))
        self.assertIsNone(parse_nvidia_smi("20000, 16311"))
        self.assertEqual(parse_nvidia_smi("3205, 16311\n100, 8000\n").used_mib, 3205)
        self.assertIsNone(parse_tasklist("INFO: No tasks are running which match the specified criteria.\r\n"))
        self.assertIsNone(parse_tasklist('"ollama.exe","1","Console","1","N/A"\r\n'))
        self.assertEqual(parse_tasklist('"ollama.exe","1","Consola","1","1.234.567 K"\r\n'), 1_234_567 * 1024)
        self.processes.returncode = 1
        probe = ResourceProbe(run=self.processes, free_ram=lambda: None, locate=FAKE_TOOLS.get)
        snapshot = probe.snapshot(None, False)
        self.assertIsNone(snapshot.gpu_used_mib)
        self.assertIsNone(snapshot.ollama_process_ram_bytes)
        self.assertIsNone(resource_report(snapshot))
        self.assertIs(resource_report(probe.snapshot(None, True)).oom, True)

    def test_tools_are_located_by_absolute_path_in_the_system_directory_only(self):
        with tempfile.TemporaryDirectory(prefix="crypto-radar-t051-tools-") as temp:
            system = Path(temp) / "System32"
            system.mkdir()
            (system / "tasklist.exe").write_bytes(b"")
            self.assertEqual(t051_ollama.locate_tool("tasklist", lambda: system), str(system / "tasklist.exe"))
            self.assertIsNone(t051_ollama.locate_tool("nvidia-smi", lambda: system))  # not there: not measured
            self.assertIsNone(t051_ollama.locate_tool("ollama", lambda: system))  # not a measurement tool
            self.assertIsNone(t051_ollama.locate_tool("tasklist", lambda: None))
            self.assertIsNone(t051_ollama.locate_tool("tasklist", lambda: Path("System32")))  # relative: refused
            # A tool planted in the current directory is never picked up.
            planted = Path(temp) / "cwd"
            planted.mkdir()
            (planted / "tasklist.exe").write_bytes(b"")
            with mock.patch("os.getcwd", return_value=str(planted)):
                self.assertEqual(t051_ollama.locate_tool("tasklist", lambda: system), str(system / "tasklist.exe"))
        if os.name == "nt":
            located = t051_ollama.locate_tool("tasklist")
            self.assertIsNotNone(located)
            self.assertTrue(Path(located).is_absolute())
            self.assertEqual(Path(located).parent, t051_ollama.windows_system_directory())

    def test_a_tool_that_cannot_be_located_is_not_measured_and_never_run(self):
        probe = ResourceProbe(run=self.processes, free_ram=lambda: None, locate=lambda name: None)
        self.assertIsNone(probe.gpu())
        self.assertIsNone(probe.ollama_process_ram())
        probe = ResourceProbe(run=self.processes, free_ram=lambda: None, locate=lambda name: name)  # bare name
        self.assertEqual(probe.executables, {"nvidia-smi": None, "tasklist": None})
        self.assertIsNone(probe.gpu())
        self.assertEqual(self.processes.calls, [])

    def test_model_switch_and_end_unload_only_benchmark_models(self):
        # Probe of qwen3:14b is done in a first invocation; the second one switches to gpt-oss.
        self.clock.advance(0)
        for _ in range(8):
            status, _ = self.invoke(budget=540)
        unloads = [item["json"]["model"] for item in self.fake.requests if item["path"] == "/api/generate"]
        self.assertTrue(unloads)
        self.assertTrue(all(model in DIGESTS for model in unloads))
        sequence = [(item["path"], (item["json"] or {}).get("model")) for item in self.fake.requests if item["path"] in ("/api/generate", "/api/chat")]
        loaded = None
        for path, model in sequence:
            if path == "/api/chat":
                self.assertIn(loaded, (None, model), "a second model was loaded before the first was unloaded")
                loaded = model
            else:
                self.assertEqual(model, loaded)
                loaded = None
        self.assertEqual(self.fake.loaded, {})

    def test_oom_reply_is_recorded_as_oom(self):
        self.fake.chat_status = 500
        self.fake.chat_body = json.dumps({"error": "model requires more system memory (20.1 GiB) than is available"}).encode()
        self.invoke(budget=100)
        record = self.records()[1]
        self.assertTrue(record["resources"]["oom"])
        self.assertEqual(record["harness"]["outcome"], "oom")


# -- freeze and digest -----------------------------------------------------------------------------


class TestFreezeAndDigest(RunnerCase):
    seconds_per_chat = 20.0

    def test_committed_freeze_matches_the_code_and_the_corpus(self):
        expected = runner.build_freeze(corpus())
        committed = json.loads((CORPUS / "t051_freeze.json").read_text(encoding="utf-8"))
        self.assertEqual(seq.freeze_differences(expected, committed), ())
        self.assertEqual(len(committed["lists"]["probe_development_labelled"]), 20)
        self.assertEqual(len(committed["lists"]["cold_development"]), 10)
        self.assertEqual(len(committed["lists"]["warm_development"]), 20)
        self.assertEqual(len(committed["lists"]["holdout_labelled"]), 120)
        self.assertEqual(len(committed["lists"]["stability_holdout_labelled"]), 20)
        self.assertEqual(len(committed["lists"]["holdout_sealed_never_sent"]), 80)
        self.assertEqual(committed["corpus_lock_sha256"], runner.EXPECTED_LOCK_SHA256)
        for name in ("model_profiles_toml", "harness_and_parser_benchmark_py", "screener_system_prompt", "screener_output_schema", "runner_cli_run_t051_block_py"):
            self.assertRegex(committed["hashes"][name], r"^[0-9a-f]{64}$")

    def test_divergent_freeze_aborts_before_any_request(self):
        committed = json.loads((CORPUS / "t051_freeze.json").read_text(encoding="utf-8"))
        committed["hashes"]["model_profiles_toml"] = "0" * 64
        freeze = self.out.parent / "freeze.json"
        freeze.write_text(json.dumps(committed), encoding="utf-8")
        status, summary = self.invoke(runtime=self.runtime(freeze_path=freeze))
        self.assertIs(status, runner.Status.ABORTED)
        self.assertEqual(status.value, 5)
        self.assertIn("hashes.model_profiles_toml", summary["reason"])
        self.assertEqual(self.fake.requests, [])

    def test_changed_code_changes_the_freeze(self):
        with tempfile.TemporaryDirectory() as root:
            for _, relative in runner.FROZEN_FILES:
                target = Path(root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPOSITORY_ROOT / relative, target)
            same = runner.build_freeze(corpus(), Path(root))
            self.assertEqual(seq.freeze_differences(same, runner.build_freeze(corpus())), ())
            crlf = Path(root, "radar_v08/model_profiles.toml")
            crlf.write_bytes(crlf.read_bytes().replace(b"\n", b"\r\n"))
            self.assertEqual(seq.freeze_differences(runner.build_freeze(corpus(), Path(root)), same), ())
            with open(Path(root, "radar_v08/workflow/benchmark.py"), "ab") as handle:
                handle.write(b"# edited\n")
            self.assertEqual(
                seq.freeze_differences(runner.build_freeze(corpus(), Path(root)), same), ("hashes.harness_and_parser_benchmark_py",)
            )

    def test_records_written_under_another_freeze_abort(self):
        self.invoke(budget=100)
        path = self.results_path()
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[-2])
        record["freeze_sha256"] = "f" * 64
        record["key"]["case_id"] = "dev-other"
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record) + "\n")
        before = len(self.fake.requests)
        status, summary = self.invoke(budget=100)
        self.assertIs(status, runner.Status.ABORTED)
        self.assertIn("another freeze", summary["reason"])
        self.assertEqual(len(self.fake.requests), before)

    def test_digest_different_from_the_preflight_aborts(self):
        self.invoke(budget=100)
        preflight = self.records("preflight")
        self.assertEqual({record["model"]: record["digest"] for record in preflight}, DIGESTS)
        self.assertEqual(preflight[0]["details"]["quantization"], "Q4_K_M")
        self.fake.installed["qwen3:14b"] = "9" * 64
        chats = len(self.fake.chats())
        status, summary = self.invoke(budget=100)
        self.assertIs(status, runner.Status.ABORTED)
        self.assertIn("digest of qwen3:14b", summary["reason"])
        self.assertEqual(len(self.fake.chats()), chats)

    def test_loaded_digest_different_from_the_preflight_aborts(self):
        self.fake.ps_digest_override["qwen3:14b"] = "8" * 64
        status, summary = self.invoke(budget=540)
        self.assertIs(status, runner.Status.ABORTED)
        self.assertIn("loaded qwen3:14b", summary["reason"])
        self.assertEqual(len(self.fake.chats()), 1)
        self.assertEqual(self.fake.loaded, {})  # still unloaded at the end

    def test_an_empty_loaded_digest_is_not_a_match(self):
        # "" is a prefix of every digest; it must not pass the preflight comparison.
        self.fake.ps_digest_override["qwen3:14b"] = ""
        status, summary = self.invoke(budget=540)
        self.assertIs(status, runner.Status.ABORTED)
        self.assertIn("loaded qwen3:14b", summary["reason"])
        self.assertEqual(len(self.fake.chats()), 1)
        self.assertEqual(self.fake.loaded, {})

    def test_a_complete_last_record_without_its_newline_is_closed_before_the_next_append(self):
        self.invoke(budget=100)
        path = self.results_path()
        data = path.read_bytes()
        self.assertTrue(data.endswith(b"\n"))
        path.write_bytes(data[:-1])  # the write was cut exactly before the newline
        calls_before = len(self.records())
        status, summary = self.invoke(budget=100)
        self.assertIs(status, runner.Status.PARTIAL, summary.get("reason"))
        self.assertIsNone(summary["truncated_line_reported"])
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(data[:-1].decode("utf-8") + "\n"))
        self.assertGreater(len(self.records()), calls_before)  # every line still parses on its own
        status, summary = self.invoke(budget=100)
        self.assertIs(status, runner.Status.PARTIAL, summary.get("reason"))

    def test_missing_model_is_not_installed_and_never_downloaded(self):
        del self.fake.installed["llama3.2:latest"]
        self.invoke(budget=100)
        preflight = {record["model"]: record for record in self.records("preflight")}
        self.assertFalse(preflight["llama3.2:latest"]["installed"])
        self.assertEqual(preflight["llama3.2:latest"]["digest"], "not_installed")
        self.assertNotIn(("POST", "/api/pull"), self.fake.paths())
        self.assertNotIn({"model": "llama3.2:latest"}, [item["json"] for item in self.fake.requests if item["path"] == "/api/show"])

    def test_write_freeze_never_replaces_a_different_freeze(self):
        freeze = self.out.parent / "freeze.json"
        status, _ = runner.write_freeze(CORPUS, self.runtime(freeze_path=freeze))
        self.assertIs(status, runner.Status.COMPLETE)
        self.assertEqual(freeze.read_text(encoding="utf-8"), (CORPUS / "t051_freeze.json").read_text(encoding="utf-8"))
        freeze.write_text("{}", encoding="utf-8")
        status, _ = runner.write_freeze(CORPUS, self.runtime(freeze_path=freeze))
        self.assertIs(status, runner.Status.REFUSED)
        self.assertEqual(freeze.read_text(encoding="utf-8"), "{}")


class TestOutDirectory(RunnerCase):
    def test_out_inside_the_repository_is_refused(self):
        for out in (REPOSITORY_ROOT, REPOSITORY_ROOT / "t051-out", REPOSITORY_ROOT / "benchmarks" / "x", Path(os.fspath(REPOSITORY_ROOT).upper()) / "y"):
            with self.subTest(out=out):
                status, summary = runner.run_invocation(CORPUS, out, 100, runtime=self.runtime())
                self.assertIs(status, runner.Status.REFUSED)
                self.assertIn("inside the repository", summary["reason"])
        # Other spellings of the same directory, found in security review: device path,
        # admin shares by name and by IP, the UNC device form, a forward-slash UNC path, and
        # the other device form.
        root = os.fspath(REPOSITORY_ROOT)
        drive, tail = root[0], root[3:]
        aliases = (
            "\\\\?\\" + root + "\\x",
            f"\\\\localhost\\{drive}$\\{tail}\\x",
            f"\\\\127.0.0.1\\{drive.lower()}$\\{tail}\\x",
            f"\\\\?\\UNC\\localhost\\{drive}$\\{tail}\\x",
            f"//localhost/{drive}$/" + tail.replace("\\", "/") + "/x",
            "\\\\.\\" + root + "\\x",
        )
        for out in aliases:
            with self.subTest(out=out):
                status, summary = runner.run_invocation(CORPUS, out, 100, runtime=self.runtime())
                self.assertIs(status, runner.Status.REFUSED)
                self.assertEqual(summary["exit_code"], 2)
                self.assertTrue(
                    "plain drive-letter path" in summary["reason"] or "inside the repository" in summary["reason"],
                    summary["reason"],
                )
                self.assertFalse((REPOSITORY_ROOT / "x").exists())
        self.assertFalse((REPOSITORY_ROOT / "t051-out").exists())
        self.assertFalse((REPOSITORY_ROOT / "results").exists())
        self.assertEqual(self.fake.requests, [])
        code = runner.main(["--corpus", os.fspath(CORPUS), "--out", os.fspath(REPOSITORY_ROOT / "t051-out"), "--budget-seconds", "10"])
        self.assertEqual(code, 2)
        self.assertFalse((REPOSITORY_ROOT / "t051-out").exists())

    def test_an_alias_of_the_repository_is_refused_by_file_identity(self):
        # An existing directory that is the repository under another name (as a subst drive or
        # another share would be): the text check cannot see it, the samefile walk refuses it.
        alias = self.out.parent / "alias-of-repo"
        alias.mkdir()
        real_samefile = os.path.samefile
        repository = REPOSITORY_ROOT.resolve()

        def samefile(a, b):
            if Path(a) == alias.resolve() and Path(b) == repository:
                return True
            return real_samefile(a, b)

        with mock.patch.object(runner.os.path, "samefile", samefile):
            status, summary = runner.run_invocation(CORPUS, alias / "x", 100, runtime=self.runtime())
        self.assertIs(status, runner.Status.REFUSED)
        self.assertIn("inside the repository", summary["reason"])
        self.assertFalse((alias / "x").exists())
        self.assertEqual(self.fake.requests, [])

    def test_an_ancestor_that_cannot_be_compared_is_refused(self):
        def broken(a, b):
            raise PermissionError("denied")

        with mock.patch.object(runner.os.path, "samefile", broken):
            status, summary = runner.run_invocation(CORPUS, self.out, 100, runtime=self.runtime())
        self.assertIs(status, runner.Status.REFUSED)
        self.assertIn("cannot be compared", summary["reason"])
        self.assertFalse(self.out.exists())

    def test_an_out_that_cannot_be_created_is_refused_not_a_traceback(self):
        blocker = self.out.parent / "a-file"
        blocker.write_text("not a directory", encoding="utf-8")
        status, summary = runner.run_invocation(CORPUS, blocker / "sub", 100, runtime=self.runtime())
        self.assertIs(status, runner.Status.REFUSED)
        self.assertEqual(summary["exit_code"], 2)
        self.assertIn("cannot be created", summary["reason"])
        self.assertEqual(self.fake.requests, [])


# -- pure sequence --------------------------------------------------------------------------------


def _observations(n=20, seconds=2.0, outcome="accepted", valid=True, memory=10 * GIB):
    return tuple(ProbeObservation(f"dev-{i:03d}", outcome, valid, seconds, memory) for i in range(n))


class TestSelectionRule(unittest.TestCase):
    def test_baseline_always_plus_at_most_one_by_validity_then_p95_then_memory(self):
        qwen = probe_stats("qwen3:14b", _observations(outcome="timeout", valid=False, seconds=31.0))
        gpt = probe_stats("gpt-oss:20b", _observations(seconds=5.0))
        llama = probe_stats("llama3.2:latest", _observations(seconds=1.0, valid=False) [:1] + _observations(n=19, seconds=1.0))
        self.assertEqual(select_candidates([qwen, gpt, llama]).candidates, ("qwen3:14b", "gpt-oss:20b"))  # 20 valid beats 19
        llama = probe_stats("llama3.2:latest", _observations(seconds=1.0))
        self.assertEqual(select_candidates([qwen, gpt, llama]).candidates, ("qwen3:14b", "llama3.2:latest"))  # same validity, lower p95
        gpt = probe_stats("gpt-oss:20b", _observations(seconds=1.0, memory=12 * GIB))
        llama = probe_stats("llama3.2:latest", _observations(seconds=1.0, memory=3 * GIB))
        self.assertEqual(select_candidates([qwen, gpt, llama]).candidates, ("qwen3:14b", "llama3.2:latest"))  # lower memory
        llama = probe_stats("llama3.2:latest", _observations(seconds=1.0, memory=None))
        self.assertEqual(select_candidates([qwen, gpt, llama]).candidates, ("qwen3:14b", "gpt-oss:20b"))  # unmeasured memory last

    def test_limits_exclude_oom_reserve_and_p95_and_none_leaves_only_the_baseline(self):
        qwen = probe_stats("qwen3:14b", _observations())
        oom = probe_stats("gpt-oss:20b", _observations(n=19) + (ProbeObservation("x", "oom", False, 3.0, None),))
        reserve = probe_stats("llama3.2:latest", _observations(n=19) + (ProbeObservation("x", "resource_reserve_breached", False, 3.0, 1),))
        self.assertEqual(oom.disqualified, ("oom",))
        self.assertEqual(reserve.disqualified, ("reserve_not_respected",))
        self.assertEqual(select_candidates([qwen, oom, reserve]).candidates, ("qwen3:14b",))
        slow = probe_stats("gpt-oss:20b", _observations(n=18, seconds=2.0) + _observations(n=2, seconds=31.0))
        self.assertAlmostEqual(slow.p95_seconds, 31.0)
        self.assertIn("p95_above_limit", slow.disqualified)
        unreported = probe_stats("llama3.2:latest", _observations(outcome="resources_unreported"))
        self.assertIn("reserve_not_respected", unreported.disqualified)
        self.assertEqual(select_candidates([qwen, slow, unreported]).candidates, ("qwen3:14b",))
        at_limit = probe_stats("gpt-oss:20b", _observations(seconds=30.0))
        self.assertTrue(at_limit.passes_limits)

    def test_selection_is_recomputed_from_the_results_records(self):
        record = {
            "record_type": "call",
            "key": {"block": "probe", "model": "gpt-oss:20b", "digest": "b" * 64, "case_id": "dev-1", "repetition": 1},
            "harness": {"outcome": "accepted", "first_pass_schema_valid": True},
            "timing": {"wall_seconds": 2.5},
            "resources": {"ollama_ps_size_bytes": 5},
        }
        other_digest = dict(record, key=dict(record["key"], digest="z" * 64, case_id="dev-2"))
        found = seq.probe_observations([record, other_digest], {"gpt-oss:20b": "b" * 64})
        self.assertEqual(found, {"gpt-oss:20b": (ProbeObservation("dev-1", "accepted", True, 2.5, 5),)})

    def test_percentile_interpolates(self):
        self.assertEqual(seq.percentile([float(i) for i in range(1, 21)], 0.95), 19.05)


class TestFixedListsAndSealing(unittest.TestCase):
    def test_lists_are_deterministic_and_the_sealed_cases_are_never_a_step(self):
        data = corpus()
        again = runner.load_corpus(CORPUS, runner.EXPECTED_LOCK_SHA256)
        self.assertEqual(again.lists, data.lists)
        lists = data.lists
        self.assertEqual(len(set(lists.warmup + lists.cold + lists.warm)), 31)
        self.assertEqual(len(lists.holdout_labelled), 120)
        self.assertTrue(set(lists.stability) <= set(lists.holdout_labelled))
        self.assertFalse(set(lists.holdout_sealed) & set(lists.holdout_labelled))
        self.assertTrue(all(not data.refs[case_id].labelled for case_id in lists.holdout_sealed))
        steps = seq._probe_steps("qwen3:14b", lists) + seq._candidate_steps("qwen3:14b", lists)
        self.assertFalse({step.case_id for step in steps} & set(lists.holdout_sealed))
        for case_id in lists.holdout_sealed[:3]:
            with self.assertRaises(SealedCaseError):
                seq.check_not_sealed(case_id, lists)
        dev_unlabelled = {case_id for case_id, ref in data.refs.items() if ref.partition == "development" and not ref.labelled}
        self.assertFalse(dev_unlabelled & set(lists.probe))
        categories = {data.refs[case_id].category for case_id in lists.probe}
        self.assertEqual(categories, {"admissible_no_edge", "insufficient_evidence", "invalid_or_stale"})

    def test_runner_refuses_a_sealed_case_even_if_asked(self):
        invocation = runner.Invocation(CORPUS, Path(tempfile.gettempdir()), 100, False, runner.Runtime())
        step = seq.Step(Block.HOLDOUT, "qwen3:14b", corpus().lists.holdout_sealed[0], 1)
        with self.assertRaises(SealedCaseError):
            invocation.call(None, corpus(), {}, step)

    def test_short_corpus_fails_closed(self):
        refs = [CaseRef(f"d{i}", "0" * 64, "development", "invalid_or_stale", True) for i in range(5)]
        with self.assertRaises(seq.SequenceError):
            seq.build_fixed_lists(refs, [])


class TestResultsFile(unittest.TestCase):
    def test_truncated_last_line_is_tolerated_and_reported(self):
        text = '{"record_type":"call","key":{}}\n{"record_type":"ca'
        parsed = parse_results(text)
        self.assertEqual(parsed.truncated_line, 2)
        self.assertEqual(len(parsed.records), 1)

    def test_marked_truncated_line_is_tolerated_and_an_unmarked_middle_line_is_corrupt(self):
        marked = '{"a":1}\n{"broken\n{"record_type":"truncated_tail","line":2}\n{"b":2}\n'
        parsed = parse_results(marked)
        self.assertIsNone(parsed.truncated_line)
        self.assertEqual(parsed.marked_truncated_lines, (2,))
        with self.assertRaises(ResultsCorrupt):
            parse_results('{"a":1}\n{"broken\n{"b":2}\n')

    def test_empty_file_has_no_record_and_no_truncated_line(self):
        parsed = parse_results("")
        self.assertEqual((parsed.records, parsed.truncated_line, parsed.marked_truncated_lines), ((), None, ()))

    def test_duplicate_keys_are_refused(self):
        record = {"record_type": "call", "key": {"block": "probe", "model": "m", "digest": "d", "case_id": "c", "repetition": 1}}
        with self.assertRaises(ResultsCorrupt):
            seq.done_keys([record, record])
        self.assertEqual(seq.done_keys([record]), frozenset({("probe", "m", "d", "c", 1)}))

    def test_warmup_repetitions_never_collide(self):
        done = frozenset({("warmup", "m", "d", "c", 1), ("warmup", "m", "d", "c", 2), ("warmup", "x", "d", "c", 7)})
        self.assertEqual(seq.next_warmup_repetition(done, "m", "d"), 3)
        self.assertEqual(seq.next_warmup_repetition(done, "n", "d"), 1)


if __name__ == "__main__":
    unittest.main()
