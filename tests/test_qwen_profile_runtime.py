"""T050c2b: the Qwen runtime (radar_v08/qwen.py + config.QWEN_*) is resolved by the T050a loader.

What is proven here:

* ``config.resolve_qwen_runtime`` builds what ``qwen.py`` sends from the DEFAULT profile of a
  profiles file: model, endpoint, timeout, temperature and think. A copy of the versioned file
  with another model/timeout changes what is sent, so the values really come from the file.
* The four overrides (``RADAR_QWEN_MODEL``, ``RADAR_QWEN_TIMEOUT_SECONDS``,
  ``RADAR_QWEN_TEMPERATURE``, ``RADAR_OLLAMA_URL``) still win, but each passes the loader's own
  rules, and the endpoint must also be an exact ``OLLAMA_ALLOWED_HOSTS`` member (stricter rule
  wins). Every refusal is a typed ``ModelProfileError`` code, never a default.
* With a missing/invalid file, a disabled default or an invalid override, importing
  ``radar_v08.config``, ``radar_v08.qwen``, ``radar_v08.heartbeat`` and ``ui.agents`` does not
  raise (fresh child process, every ``RADAR_*`` stripped, state in a temporary directory, D31);
  ``review_finalists`` answers ``UNAVAILABLE`` with the typed code and a warning log, and
  ``requests.post`` / an injected ``post_fn`` is called zero times; ``config.QWEN_MODEL`` and the
  UI's Qwen agent show no model.
* With the default profile, ``config.QWEN_MODEL`` == ``qwen3:14b`` == the model posted == the
  model the UI's Qwen agent shows.
* A real heartbeat cycle over fake Kraken data (``tests/test_integrity_wiring.py`` fixtures) with
  a refused profile still routes every sealed L3 finalist deterministically, posts nothing and
  records ``data_quality.qwen == "UNAVAILABLE"``.

Every expected value is a literal written here. No real Ollama, no network, no production file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The shared fixtures below live in sibling test modules (tests/ is not a package).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from test_integrity_wiring import FakeKraken, IntegrityWiringBase
from test_qwen_profile_equivalence import _radar_path_env_vars

from radar_v08 import config, heartbeat, qwen
from radar_v08.adapters import model_profiles as mp
from radar_v08.adapters.model_profiles import ModelProfileError, ProfileErrorCode
from radar_v08.config import QwenRuntime, resolve_qwen_runtime

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

REAL_PROFILES_TEXT = (REPOSITORY_ROOT / "radar_v08" / "model_profiles.toml").read_text(encoding="utf-8")

DEFAULT_RUNTIME = QwenRuntime(
    profile_id="screener-qwen3-14b",
    model="qwen3:14b",
    endpoint="http://localhost:11434",
    timeout_seconds=30.0,
    temperature=0.0,
    think=False,
    context_tokens=4096,
    output_cap_tokens=768,
)


def _variant(old: str, new: str) -> str:
    assert REAL_PROFILES_TEXT.count(old) == 1, old
    return REAL_PROFILES_TEXT.replace(old, new)


PROFILE_VARIANTS = {
    "real": REAL_PROFILES_TEXT,
    "invalid_toml": "schema_version = = 1\n",
    "default_disabled": _variant("enabled = true", "enabled = false"),
    "unknown_key": _variant("think = false", "think = false\nnum_ctx = 4096"),
    "port_not_allowlisted": _variant('endpoint = "http://localhost:11434"', 'endpoint = "http://127.0.0.1:11500"'),
    "other_model": _variant('model = "qwen3:14b"', 'model = "qwen3:8b"').replace(
        "hard_timeout_seconds = 30", "hard_timeout_seconds = 20"
    ),
}


class _ProfilesDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="t050c2b-profiles-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def profiles(self, variant: str) -> Path:
        path = self.tmp / f"{variant}.toml"
        path.write_text(PROFILE_VARIANTS[variant], encoding="utf-8")
        return path

    def refused(self, environ: dict[str, str], variant: str = "real") -> ModelProfileError:
        path = self.tmp / "absent.toml" if variant == "absent" else self.profiles(variant)
        with self.assertRaises(ModelProfileError) as caught:
            resolve_qwen_runtime(environ, path)
        return caught.exception


class TestResolveFromTheDefaultProfile(_ProfilesDir):
    def test_versioned_file_resolves_to_todays_runtime(self) -> None:
        self.assertEqual(resolve_qwen_runtime({}, mp.DEFAULT_PROFILES_PATH), DEFAULT_RUNTIME)

    def test_values_come_from_the_file_not_from_code(self) -> None:
        runtime = resolve_qwen_runtime({}, self.profiles("other_model"))
        self.assertEqual((runtime.model, runtime.timeout_seconds), ("qwen3:8b", 20.0))
        self.assertIs(type(runtime.timeout_seconds), float)

    def test_unrelated_environment_is_ignored(self) -> None:
        runtime = resolve_qwen_runtime({"RADAR_QWEN_MAX_RETRIES": "5", "OLLAMA_URL": "http://x:1"}, self.profiles("real"))
        self.assertEqual(runtime, DEFAULT_RUNTIME)

    def test_module_constants_are_the_resolved_default(self) -> None:
        # This process may carry RADAR_* from the caller; compare against its own resolution.
        runtime = config.QWEN_RUNTIME
        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertIsNone(config.QWEN_PROFILE_ERROR)
        self.assertEqual(
            (config.QWEN_MODEL, config.OLLAMA_URL, config.QWEN_TIMEOUT_SECONDS, config.QWEN_TEMPERATURE, config.QWEN_THINK),
            (runtime.model, runtime.endpoint, runtime.timeout_seconds, runtime.temperature, runtime.think),
        )


class TestValidOverrides(_ProfilesDir):
    def test_todays_valid_overrides_keep_precedence(self) -> None:
        runtime = resolve_qwen_runtime(
            {
                "RADAR_QWEN_MODEL": "qwen3:8b",
                "RADAR_QWEN_TIMEOUT_SECONDS": "20",
                "RADAR_OLLAMA_URL": "http://127.0.0.1:11434",
                "RADAR_QWEN_TEMPERATURE": "0",
            },
            self.profiles("real"),
        )
        self.assertEqual(
            runtime,
            QwenRuntime("screener-qwen3-14b", "qwen3:8b", "http://127.0.0.1:11434", 20.0, 0.0, False, 4096, 768),
        )

    def test_whole_second_decimal_and_boundaries_are_accepted(self) -> None:
        path = self.profiles("real")
        self.assertEqual(resolve_qwen_runtime({"RADAR_QWEN_TIMEOUT_SECONDS": "30.0"}, path).timeout_seconds, 30.0)
        self.assertEqual(resolve_qwen_runtime({"RADAR_QWEN_TIMEOUT_SECONDS": "1"}, path).timeout_seconds, 1.0)
        self.assertEqual(resolve_qwen_runtime({"RADAR_QWEN_TEMPERATURE": "0.0"}, path).temperature, 0.0)
        self.assertEqual(resolve_qwen_runtime({"RADAR_QWEN_MODEL": "library/qwen3:14b"}, path).model, "library/qwen3:14b")


class TestInvalidOverridesFailClosed(_ProfilesDir):
    CASES = [
        ("RADAR_QWEN_MODEL", "qwen3", ProfileErrorCode.INVALID_MODEL),
        ("RADAR_QWEN_MODEL", "", ProfileErrorCode.INVALID_MODEL),
        ("RADAR_QWEN_MODEL", "Qwen3:14B", ProfileErrorCode.INVALID_MODEL),
        ("RADAR_QWEN_MODEL", "registry.example.com/qwen3:14b", ProfileErrorCode.INVALID_MODEL),
        ("RADAR_QWEN_MODEL", "gpt-oss:120b-cloud", ProfileErrorCode.CLOUD_MODEL),
        ("RADAR_QWEN_MODEL", "qwen3:cloud", ProfileErrorCode.CLOUD_MODEL),
        ("RADAR_QWEN_TIMEOUT_SECONDS", "31", ProfileErrorCode.TIMEOUT_OUT_OF_LIMITS),
        ("RADAR_QWEN_TIMEOUT_SECONDS", "0", ProfileErrorCode.TIMEOUT_OUT_OF_LIMITS),
        ("RADAR_QWEN_TIMEOUT_SECONDS", "-5", ProfileErrorCode.TIMEOUT_OUT_OF_LIMITS),
        ("RADAR_QWEN_TIMEOUT_SECONDS", "12.5", ProfileErrorCode.WRONG_TYPE),
        ("RADAR_QWEN_TIMEOUT_SECONDS", "abc", ProfileErrorCode.WRONG_TYPE),
        ("RADAR_QWEN_TIMEOUT_SECONDS", "inf", ProfileErrorCode.WRONG_TYPE),
        ("RADAR_QWEN_TIMEOUT_SECONDS", "nan", ProfileErrorCode.WRONG_TYPE),
        ("RADAR_QWEN_TIMEOUT_SECONDS", "", ProfileErrorCode.WRONG_TYPE),
        ("RADAR_QWEN_TEMPERATURE", "0.7", ProfileErrorCode.TEMPERATURE_NOT_ZERO),
        ("RADAR_QWEN_TEMPERATURE", "-0.1", ProfileErrorCode.TEMPERATURE_NOT_ZERO),
        ("RADAR_QWEN_TEMPERATURE", "warm", ProfileErrorCode.WRONG_TYPE),
        ("RADAR_QWEN_TEMPERATURE", "nan", ProfileErrorCode.WRONG_TYPE),
        ("RADAR_OLLAMA_URL", "http://evil.example.com:11434", ProfileErrorCode.ENDPOINT_REFUSED),
        ("RADAR_OLLAMA_URL", "http://192.168.1.10:11434", ProfileErrorCode.ENDPOINT_REFUSED),
        ("RADAR_OLLAMA_URL", "https://localhost:11434", ProfileErrorCode.ENDPOINT_REFUSED),
        ("RADAR_OLLAMA_URL", "http://localhost", ProfileErrorCode.ENDPOINT_REFUSED),
        ("RADAR_OLLAMA_URL", "", ProfileErrorCode.ENDPOINT_REFUSED),
        # Loopback with an explicit port passes the loader, but not the exact allowlist.
        ("RADAR_OLLAMA_URL", "http://127.0.0.1:9999", ProfileErrorCode.ENDPOINT_REFUSED),
        ("RADAR_OLLAMA_URL", "http://localhost:11434/", ProfileErrorCode.ENDPOINT_REFUSED),
    ]

    def test_each_invalid_override_is_refused_with_its_code(self) -> None:
        for name, value, code in self.CASES:
            with self.subTest(name=name, value=value):
                error = self.refused({name: value})
                self.assertIs(error.code, code)
                self.assertEqual(error.where, name)

    def test_a_valid_override_never_rescues_a_broken_file(self) -> None:
        valid = {"RADAR_QWEN_MODEL": "qwen3:14b", "RADAR_OLLAMA_URL": "http://localhost:11434"}
        self.assertIs(self.refused(valid, "absent").code, ProfileErrorCode.FILE_MISSING)
        self.assertIs(self.refused(valid, "invalid_toml").code, ProfileErrorCode.INVALID_TOML)
        self.assertIs(self.refused(valid, "default_disabled").code, ProfileErrorCode.PROFILE_DISABLED)

    def test_profile_endpoint_outside_the_allowlist_is_refused(self) -> None:
        error = self.refused({}, "port_not_allowlisted")
        self.assertIs(error.code, ProfileErrorCode.ENDPOINT_REFUSED)
        self.assertEqual(error.where, "profile 'screener-qwen3-14b'.endpoint")
        # The allowlisted override is the only way past a refused profile endpoint.
        runtime = resolve_qwen_runtime({"RADAR_OLLAMA_URL": "http://127.0.0.1:11434"}, self.profiles("port_not_allowlisted"))
        self.assertEqual(runtime.endpoint, "http://127.0.0.1:11434")

    def test_allowlist_is_stricter_than_the_loader_rule(self) -> None:
        for host in sorted(config.OLLAMA_ALLOWED_HOSTS):
            with self.subTest(host=host):
                self.assertEqual(resolve_qwen_runtime({"RADAR_OLLAMA_URL": host}, self.profiles("real")).endpoint, host)


class TestApplyOverridesKeepsTheProfile(_ProfilesDir):
    def test_other_fields_are_untouched_and_inference_follows(self) -> None:
        profile = mp.load_model_profiles(self.profiles("real")).get("screener-qwen3-14b")
        changed = mp.apply_overrides(profile, model="qwen3:8b", hard_timeout_seconds="20")
        self.assertEqual((changed.model, changed.hard_timeout_seconds), ("qwen3:8b", 20))
        assert changed.inference is not None
        self.assertEqual((changed.inference.model, changed.inference.hard_timeout_seconds), ("qwen3:8b", 20))
        self.assertEqual(
            (changed.context_tokens, changed.output_cap_tokens, changed.reserve_vram_gib, changed.reserve_ram_gib),
            (4096, 768, 1.5, 4.0),
        )
        self.assertEqual(mp.apply_overrides(profile), profile)  # no override, no change

    def test_endpoint_override_is_canonicalised_like_the_file(self) -> None:
        profile = mp.load_model_profiles(self.profiles("real")).get("screener-qwen3-14b")
        self.assertEqual(mp.apply_overrides(profile, endpoint="http://127.0.0.1:11434/").endpoint, "http://127.0.0.1:11434")


# --------------------------------------------------------------------------------------------
# Child processes: import-time behaviour with state in a temporary directory (D31).
# --------------------------------------------------------------------------------------------

CHILD_SCRIPT = r'''
import json
import logging
import sys
from pathlib import Path
from unittest import mock

repo_root, profiles_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo_root)

import radar_v08.adapters.model_profiles as mp
if profiles_path != "-":
    mp.DEFAULT_PROFILES_PATH = Path(profiles_path)

records = []


class Capture(logging.Handler):
    def emit(self, record):
        records.append([record.name, record.levelname, record.getMessage()])


qwen_logger = logging.getLogger("radar_v08.qwen")
qwen_logger.addHandler(Capture())
qwen_logger.setLevel(logging.WARNING)
qwen_logger.propagate = False

imported = []
import radar_v08.config as config
imported.append("radar_v08.config")
import radar_v08.qwen as qwen
imported.append("radar_v08.qwen")
import radar_v08.heartbeat
imported.append("radar_v08.heartbeat")
import ui.agents as agents
imported.append("ui.agents")

FINALISTS = [{"asset": "BTC", "setup_type": "BREAKOUT", "direction": "LONG"}]
VALID = {"reviews": [{
    "asset": "BTC", "setup_type": "BREAKOUT", "direction": "LONG", "market": "SPOT", "veto": False,
    "call_sonnet": False, "call_fable": False, "confidence": "LOW", "reason": "ok", "data_quality_notes": [],
}]}


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": json.dumps(VALID)}}


posted = []
injected = []


def fake_post(url, **kwargs):
    posted.append({"url": url, "model": kwargs["json"]["model"], "timeout": kwargs["timeout"],
                   "options": kwargs["json"]["options"], "keys": sorted(kwargs["json"])})
    return FakeResponse()


def fake_post_fn(payload):
    injected.append(payload["model"])
    return FakeResponse().json()


def run(**kwargs):
    try:
        # Freeze elapsed time here: shared-deadline behavior has dedicated tests.
        result = qwen.review_finalists(**kwargs, monotonic=lambda: 0.0)
    except RuntimeError as exc:
        return {"exception": str(exc)}
    return {"status": result.status, "error": result.error, "error_code": result.error_code,
            "reviews": sorted(result.reviews)}


with mock.patch("requests.post", fake_post):
    default_transport = run(finalists=FINALISTS)
    injected_transport = run(finalists=FINALISTS, post_fn=fake_post_fn)
    empty = run(finalists=[])

error = config.QWEN_PROFILE_ERROR
print(json.dumps({
    "imported": imported,
    "profile_error_code": None if error is None else error.code.value,
    "config_model": config.QWEN_MODEL,
    "config_timeout": config.QWEN_TIMEOUT_SECONDS,
    "config_temperature": config.QWEN_TEMPERATURE,
    "config_think": config.QWEN_THINK,
    "config_url": config.OLLAMA_URL,
    "retries": config.QWEN_MAX_RETRIES_ON_INVALID,
    "pregate": config.QWEN_PREGATE_MIN_OPPORTUNITY,
    "ui_model": agents._build_qwen_screener(agents.AGENT_REGISTRY[0], None).model,
    "default_transport": default_transport,
    "injected_transport": injected_transport,
    "empty": empty,
    "posted": posted,
    "injected": injected,
    "warnings": [r for r in records if r[1] == "WARNING"],
}))
'''


def run_child(profiles_path: str | os.PathLike[str] | None, extra_env: dict[str, str] | None = None) -> dict:
    path_vars = _radar_path_env_vars()
    with tempfile.TemporaryDirectory(prefix="t050c2b-state-") as state_dir, \
            tempfile.TemporaryDirectory(prefix="t050c2b-script-") as script_dir:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("RADAR_")}
        environment["RADAR_STATE_DIR"] = state_dir
        for name in sorted(path_vars):
            environment[name] = os.path.join(state_dir, name)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.update(extra_env or {})
        script = os.path.join(script_dir, "runtime_probe.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(CHILD_SCRIPT)
        completed = subprocess.run(
            [sys.executable, script, str(REPOSITORY_ROOT), "-" if profiles_path is None else os.fspath(profiles_path)],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
    if completed.returncode != 0:
        raise AssertionError(f"child failed: {completed.stderr}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


EXPECTED_IMPORTED = ["radar_v08.config", "radar_v08.qwen", "radar_v08.heartbeat", "ui.agents"]


class TestOneSourceOfTruthInAChild(_ProfilesDir):
    def test_default_profile_model_is_config_payload_and_ui(self) -> None:
        out = run_child(None)
        self.assertEqual(out["imported"], EXPECTED_IMPORTED)
        self.assertIsNone(out["profile_error_code"])
        self.assertEqual(out["config_model"], "qwen3:14b")
        self.assertEqual(out["ui_model"], "qwen3:14b")
        self.assertEqual(out["posted"], [{
            "url": "http://localhost:11434/api/chat", "model": "qwen3:14b", "timeout": 30.0,
            "options": {"temperature": 0.0, "num_ctx": 4096, "num_predict": 768}, "keys": ["format", "messages", "model", "options", "stream", "think"],
        }])
        self.assertEqual(out["injected"], ["qwen3:14b"])
        self.assertEqual(out["default_transport"]["status"], "OK")
        self.assertEqual((out["config_timeout"], out["config_temperature"], out["config_think"]), (30.0, 0.0, False))
        self.assertEqual((out["retries"], out["pregate"]), (1, 50.0))  # unchanged, still in config
        self.assertEqual(out["warnings"], [])

    def test_a_different_file_changes_what_is_sent_and_shown(self) -> None:
        out = run_child(self.profiles("other_model"))
        self.assertEqual((out["config_model"], out["ui_model"]), ("qwen3:8b", "qwen3:8b"))
        self.assertEqual([(p["model"], p["timeout"]) for p in out["posted"]], [("qwen3:8b", 20.0)])

    def test_valid_override_is_what_the_ui_shows(self) -> None:
        out = run_child(None, {"RADAR_QWEN_MODEL": "qwen3:8b"})
        self.assertEqual((out["config_model"], out["ui_model"], out["posted"][0]["model"]), ("qwen3:8b",) * 3)


class TestFailClosedInAChild(_ProfilesDir):
    def assert_unavailable(self, out: dict, code: str) -> None:
        self.assertEqual(out["imported"], EXPECTED_IMPORTED)  # import never raised
        self.assertEqual(out["profile_error_code"], code)
        expected = {"status": "UNAVAILABLE", "error_code": code, "reviews": []}
        for key in ("default_transport", "injected_transport", "empty"):
            with self.subTest(call=key):
                result = out[key]
                self.assertEqual({k: result[k] for k in expected}, expected)
                self.assertTrue(result["error"].startswith(f"model profile refused: model profiles refused: {code} at "))
        self.assertEqual(out["posted"], [])
        self.assertEqual(out["injected"], [])
        # Nothing the runtime does not use is exposed: no model, no timeout, no temperature.
        self.assertEqual(
            (out["config_model"], out["ui_model"], out["config_timeout"], out["config_temperature"], out["config_think"]),
            (None, None, None, None, None),
        )
        self.assertEqual((out["retries"], out["pregate"]), (1, 50.0))
        self.assertEqual(len(out["warnings"]), 3)
        for name, level, message in out["warnings"]:
            self.assertEqual((name, level), ("radar_v08.qwen", "WARNING"))
            self.assertIn(f"Qwen UNAVAILABLE: model profile refused, Ollama not called: model profiles refused: {code}", message)

    def test_missing_file(self) -> None:
        out = run_child(self.tmp / "absent.toml")
        self.assert_unavailable(out, "file_missing")
        self.assertIsNone(out["config_url"])  # no invented URL

    def test_invalid_file(self) -> None:
        self.assert_unavailable(run_child(self.profiles("invalid_toml")), "invalid_toml")

    def test_unknown_key_in_file(self) -> None:
        self.assert_unavailable(run_child(self.profiles("unknown_key")), "unknown_key")

    def test_default_profile_disabled(self) -> None:
        self.assert_unavailable(run_child(self.profiles("default_disabled")), "profile_disabled")

    def test_profile_endpoint_not_allowlisted(self) -> None:
        out = run_child(self.profiles("port_not_allowlisted"))
        self.assert_unavailable(out, "endpoint_refused")
        self.assertIsNone(out["config_url"])

    def test_invalid_overrides(self) -> None:
        cases = {
            "invalid_model": {"RADAR_QWEN_MODEL": "qwen3"},
            "cloud_model": {"RADAR_QWEN_MODEL": "gpt-oss:120b-cloud"},
            "timeout_out_of_limits": {"RADAR_QWEN_TIMEOUT_SECONDS": "31"},
            "wrong_type": {"RADAR_QWEN_TIMEOUT_SECONDS": "abc"},
            "temperature_not_zero": {"RADAR_QWEN_TEMPERATURE": "0.7"},
        }
        for code, env in cases.items():
            with self.subTest(code=code):
                self.assert_unavailable(run_child(None, env), code)

    def test_broken_file_with_valid_url_override(self) -> None:
        out = run_child(self.tmp / "absent.toml", {"RADAR_OLLAMA_URL": "http://127.0.0.1:11434"})
        self.assert_unavailable(out, "file_missing")
        self.assertEqual(out["config_url"], "http://127.0.0.1:11434")

    def test_refused_url_override_keeps_todays_runtime_error(self) -> None:
        # Frozen in tests/test_qwen_profile_equivalence.py (T050c2a): the guard still raises
        # before any call; here the import side is checked too.
        out = run_child(None, {"RADAR_OLLAMA_URL": "http://evil.example.com:11434"})
        self.assertEqual(out["imported"], EXPECTED_IMPORTED)
        self.assertEqual(out["profile_error_code"], "endpoint_refused")
        self.assertEqual(
            out["default_transport"],
            {"exception": "Refusing to call non-local Ollama host: http://evil.example.com:11434"},
        )
        self.assertEqual(out["injected_transport"]["status"], "UNAVAILABLE")
        self.assertEqual(out["posted"], [])
        self.assertEqual(out["injected"], [])
        self.assertIsNone(out["ui_model"])


# --------------------------------------------------------------------------------------------
# Heartbeat: the deterministic L3 path continues without Qwen when the profile is refused.
# --------------------------------------------------------------------------------------------


class TestHeartbeatContinuesWithoutQwen(IntegrityWiringBase):
    def setUp(self) -> None:
        super().setUp()
        self.posted: list[object] = []

        def refuse_post(*args: object, **kwargs: object) -> None:
            self.posted.append((args, kwargs))
            raise AssertionError("Ollama must not be called without a valid profile")

        error = ModelProfileError(ProfileErrorCode.FILE_MISSING, "model_profiles.toml")
        for patcher in (
            mock.patch.object(heartbeat, "review_finalists", qwen.review_finalists),  # the real one
            mock.patch.object(config, "QWEN_RUNTIME", None),
            mock.patch.object(config, "QWEN_PROFILE_ERROR", error),
            mock.patch.object(config, "OLLAMA_URL", None),
            mock.patch.object(requests, "post", refuse_post),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_finalists_are_routed_deterministically_and_nothing_is_posted(self) -> None:
        with self.assertLogs("radar_v08.qwen", "WARNING") as logs:
            output = self.run_cycle(FakeKraken(assets=("BTC", "ETH")))

        self.assertEqual(self.posted, [])
        self.assertEqual(output["data_quality"]["qwen"], "UNAVAILABLE")
        self.assertEqual(sorted(self.routed_assets()), ["BTC", "ETH"])
        for ctx in self.routed:
            self.assertEqual(ctx.qwen_status, "SKIPPED")  # no review exists for any asset
            self.assertFalse(ctx.qwen_veto)
        self.assertEqual(
            logs.output,
            ["WARNING:radar_v08.qwen:Qwen UNAVAILABLE: model profile refused, Ollama not called: "
             "model profiles refused: file_missing at model_profiles.toml"],
        )
        self.assertEqual(output["data_quality"]["integrity"]["seal_blocked"], 0)


if __name__ == "__main__":
    unittest.main()
