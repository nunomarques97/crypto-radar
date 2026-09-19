"""T050a: versioned local model profiles loader (radar_v08/adapters/model_profiles.py).

D31/D33: every rejection is a TOML file written in a temporary directory; the only
repository file read is the versioned ``radar_v08/model_profiles.toml``. No model is
run or downloaded, no request reaches Ollama, and the loader is shown to open its file
once in ``"rb"`` mode, write nothing and open no socket. The default-profile test reads
``radar_v08.config`` in a child interpreter with every ``RADAR_QWEN_*`` and
``RADAR_OLLAMA_URL`` variable removed and the state directory pointed at a temporary
directory, so the values compared are the code defaults the radar uses today.
"""

import builtins
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(REPOSITORY_ROOT))

from radar_v08.adapters import model_profiles as mp  # noqa: E402
from radar_v08.adapters.model_profiles import (  # noqa: E402
    DEFAULT_PROFILES_PATH,
    OC1_ROLE_CONTEXT_TOKENS,
    ModelProfileError,
    OutputFormat,
    ProfileErrorCode,
    load_inference_profile,
    load_model_profiles,
)
from radar_v08.workflow.scheduler import OC1_ROLE_PROFILES, Role  # noqa: E402
from radar_v08.workflow.worker import InferenceProfile  # noqa: E402

REAL_FILE = REPOSITORY_ROOT / "radar_v08" / "model_profiles.toml"

VALID_SCREENER = {
    "id": "screener-a",
    "role": "screener",
    "enabled": True,
    "model": "qwen3:14b",
    "endpoint": "http://localhost:11434",
    "output_format": "json_schema",
    "hard_timeout_seconds": 30,
    "context_tokens": 4096,
    "output_cap_tokens": 768,
    "temperature": 0.0,
    "think": False,
    "reserve_vram_gib": 1.5,
    "reserve_ram_gib": 4.0,
}

VALID_CHALLENGER_DISABLED = {
    **VALID_SCREENER,
    "id": "challenger-a",
    "role": "challenger",
    "enabled": False,
    "model": "gpt-oss:20b",
    "hard_timeout_seconds": 45,
    "context_tokens": 8192,
    "output_cap_tokens": 1536,
}

_DROP = object()


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, int):
        return str(value)
    raise TypeError(value)


def render(profiles, schema_version=1, default="screener-a", extra_top=""):
    lines = []
    if schema_version is not _DROP:
        lines.append(f"schema_version = {_toml_value(schema_version)}")
    if default is not _DROP:
        lines.append(f"default_profile = {_toml_value(default)}")
    if extra_top:
        lines.append(extra_top)
    for profile in profiles:
        lines.append("")
        lines.append("# Rationale: test fixture.")
        lines.append("[[profiles]]")
        for key, value in profile.items():
            if value is _DROP:
                continue
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def screener(**overrides):
    return {**VALID_SCREENER, **overrides}


class _TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, text, name="profiles.toml"):
        path = self.tmp / name
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def assert_refused(self, text, code):
        path = self.write(text)
        with self.assertRaises(ModelProfileError) as caught:
            load_model_profiles(path)
        self.assertIs(caught.exception.code, code, str(caught.exception))
        with self.assertRaises(ModelProfileError):
            load_inference_profile(path)
        return caught.exception

    def assert_profile_refused(self, code, **overrides):
        return self.assert_refused(render([screener(**overrides)]), code)


class TestValidFiles(_TempDirTest):
    def test_valid_file_resolves_to_an_inference_profile(self):
        path = self.write(render([screener(), VALID_CHALLENGER_DISABLED]))
        profiles = load_model_profiles(path)
        self.assertEqual(profiles.schema_version, 1)
        self.assertEqual(profiles.default_profile_id, "screener-a")
        self.assertEqual(sorted(profiles.profiles), ["challenger-a", "screener-a"])
        resolved = load_inference_profile(path)
        self.assertIs(type(resolved), InferenceProfile)
        self.assertEqual(
            resolved,
            InferenceProfile(
                profile_id="screener-a",
                model="qwen3:14b",
                role=Role.SCREENER,
                hard_timeout_seconds=30,
                context_tokens=4096,
                output_cap_tokens=768,
                think=False,
            ),
        )
        self.assertEqual(profiles.resolve("screener-a"), resolved)
        screener_profile = profiles.get("screener-a")
        self.assertIs(screener_profile.output_format, OutputFormat.JSON_SCHEMA)
        self.assertEqual(screener_profile.endpoint, "http://localhost:11434")
        self.assertEqual(screener_profile.temperature, 0.0)
        self.assertEqual((screener_profile.reserve_vram_gib, screener_profile.reserve_ram_gib), (1.5, 4.0))

    def test_profiles_mapping_is_read_only(self):
        profiles = load_model_profiles(self.write(render([screener()])))
        with self.assertRaises(TypeError):
            profiles.profiles["x"] = profiles.get("screener-a")

    def test_disabled_challenger_loads_but_never_resolves(self):
        profiles = load_model_profiles(self.write(render([screener(), VALID_CHALLENGER_DISABLED])))
        challenger = profiles.get("challenger-a")
        self.assertFalse(challenger.enabled)
        self.assertIsNone(challenger.inference)
        with self.assertRaises(ModelProfileError) as caught:
            profiles.resolve("challenger-a")
        self.assertIs(caught.exception.code, ProfileErrorCode.PROFILE_DISABLED)

    def test_disabled_deep_analyst_within_its_limits_loads(self):
        deep = {
            **VALID_CHALLENGER_DISABLED,
            "id": "deep-a",
            "role": "deep_analyst",
            "hard_timeout_seconds": 90,
            "output_cap_tokens": 2048,
        }
        profiles = load_model_profiles(self.write(render([screener(), deep])))
        self.assertIs(profiles.get("deep-a").role, Role.DEEP_ANALYST)

    def test_boundaries_accepted(self):
        # Smallest context that still fits the Screener input budget: 2800 + 768.
        load_model_profiles(self.write(render([screener(context_tokens=3568)])))
        load_model_profiles(self.write(render([screener(hard_timeout_seconds=1, output_cap_tokens=1)])))
        load_model_profiles(self.write(render([screener(reserve_vram_gib=8, reserve_ram_gib=16)])))
        load_model_profiles(self.write(render([screener(temperature=0)])))

    def test_trailing_slash_endpoint_is_canonicalised(self):
        profiles = load_model_profiles(self.write(render([screener(endpoint="http://127.0.0.1:11434/")])))
        self.assertEqual(profiles.get("screener-a").endpoint, "http://127.0.0.1:11434")

    def test_unknown_profile_id_is_refused(self):
        profiles = load_model_profiles(self.write(render([screener()])))
        with self.assertRaises(ModelProfileError) as caught:
            profiles.resolve("nope")
        self.assertIs(caught.exception.code, ProfileErrorCode.PROFILE_NOT_FOUND)

    def test_environment_does_not_change_the_loaded_profile(self):
        path = self.write(render([screener()]))
        with mock.patch.dict(
            os.environ,
            {"RADAR_QWEN_MODEL": "llama3.2:latest", "RADAR_OLLAMA_URL": "http://example.com:80"},
        ):
            resolved = load_inference_profile(path)
            endpoint = load_model_profiles(path).get("screener-a").endpoint
        self.assertEqual(resolved.model, "qwen3:14b")
        self.assertEqual(endpoint, "http://localhost:11434")


class TestFileRejections(_TempDirTest):
    def test_missing_file(self):
        with self.assertRaises(ModelProfileError) as caught:
            load_model_profiles(self.tmp / "absent.toml")
        self.assertIs(caught.exception.code, ProfileErrorCode.FILE_MISSING)

    def test_directory_is_unreadable(self):
        with self.assertRaises(ModelProfileError) as caught:
            load_model_profiles(self.tmp)
        self.assertIs(caught.exception.code, ProfileErrorCode.FILE_UNREADABLE)

    def test_invalid_toml(self):
        self.assert_refused("schema_version = = 1\n", ProfileErrorCode.INVALID_TOML)

    def test_toml_duplicate_key_is_invalid_toml(self):
        text = render([screener()]).replace('role = "screener"', 'role = "screener"\nrole = "screener"')
        self.assert_refused(text, ProfileErrorCode.INVALID_TOML)

    def test_not_utf8(self):
        self.assert_refused(b'schema_version = 1\n# \xff\xfe\n', ProfileErrorCode.INVALID_TOML)

    def test_too_large(self):
        text = render([screener()]) + "#" + "x" * mp.MAX_FILE_BYTES + "\n"
        self.assert_refused(text, ProfileErrorCode.FILE_TOO_LARGE)

    def test_empty_file_is_missing_keys(self):
        self.assert_refused("", ProfileErrorCode.MISSING_KEY)


class TestTopLevelRejections(_TempDirTest):
    def test_unknown_top_level_key(self):
        self.assert_refused(render([screener()], extra_top='fallback = "qwen3:8b"'), ProfileErrorCode.UNKNOWN_KEY)

    def test_missing_schema_version(self):
        self.assert_refused(render([screener()], schema_version=_DROP), ProfileErrorCode.MISSING_KEY)

    def test_missing_default_profile(self):
        self.assert_refused(render([screener()], default=_DROP), ProfileErrorCode.MISSING_KEY)

    def test_unsupported_schema_version(self):
        self.assert_refused(render([screener()], schema_version=2), ProfileErrorCode.UNSUPPORTED_SCHEMA_VERSION)

    def test_schema_version_wrong_types(self):
        for value in ("1", True, 1.0):
            with self.subTest(value=value):
                self.assert_refused(render([screener()], schema_version=value), ProfileErrorCode.WRONG_TYPE)

    def test_default_profile_wrong_type(self):
        self.assert_refused(render([screener()], default=1), ProfileErrorCode.WRONG_TYPE)

    def test_no_profiles(self):
        self.assert_refused('schema_version = 1\ndefault_profile = "a"\nprofiles = []\n', ProfileErrorCode.NO_PROFILES)

    def test_profiles_must_be_an_array_of_tables(self):
        self.assert_refused(
            'schema_version = 1\ndefault_profile = "a"\n[profiles]\nid = "a"\n', ProfileErrorCode.WRONG_TYPE
        )
        self.assert_refused('schema_version = 1\ndefault_profile = "a"\nprofiles = [1]\n', ProfileErrorCode.WRONG_TYPE)

    def test_unknown_default(self):
        self.assert_refused(render([screener()], default="screener-b"), ProfileErrorCode.DEFAULT_NOT_FOUND)

    def test_disabled_default(self):
        self.assert_refused(render([screener(enabled=False)]), ProfileErrorCode.PROFILE_DISABLED)
        self.assert_refused(
            render([screener(), VALID_CHALLENGER_DISABLED], default="challenger-a"),
            ProfileErrorCode.PROFILE_DISABLED,
        )

    def test_duplicate_ids(self):
        self.assert_refused(render([screener(), screener()]), ProfileErrorCode.DUPLICATE_ID)

    def test_one_invalid_profile_rejects_the_whole_file(self):
        bad = {**VALID_CHALLENGER_DISABLED, "hard_timeout_seconds": 46}
        self.assert_refused(render([screener(), bad]), ProfileErrorCode.TIMEOUT_OUT_OF_LIMITS)


class TestProfileKeyAndTypeRejections(_TempDirTest):
    def test_unknown_profile_key(self):
        self.assert_profile_refused(ProfileErrorCode.UNKNOWN_KEY, num_gpu=1)
        self.assert_profile_refused(ProfileErrorCode.UNKNOWN_KEY, fallback_endpoint="http://localhost:11434")

    def test_each_missing_profile_key(self):
        for key in VALID_SCREENER:
            with self.subTest(key=key):
                self.assert_profile_refused(ProfileErrorCode.MISSING_KEY, **{key: _DROP})

    def test_wrong_types(self):
        cases = {
            "id": 7,
            "role": 1,
            "enabled": "true",
            "model": 14,
            "endpoint": 11434,
            "output_format": True,
            "hard_timeout_seconds": 30.0,
            "context_tokens": "4096",
            "output_cap_tokens": True,
            "temperature": "0",
            "think": 0,
            "reserve_vram_gib": "1.5",
            "reserve_ram_gib": False,
        }
        self.assertEqual(set(cases), set(VALID_SCREENER))
        for key, value in cases.items():
            with self.subTest(key=key):
                self.assert_profile_refused(ProfileErrorCode.WRONG_TYPE, **{key: value})

    def test_non_finite_numbers(self):
        for key in ("temperature", "reserve_vram_gib", "reserve_ram_gib"):
            for value in (math.nan, math.inf):
                with self.subTest(key=key, value=value):
                    self.assert_profile_refused(ProfileErrorCode.WRONG_TYPE, **{key: value})

    def test_invalid_ids(self):
        for value in ("", "Screener", "a b", "-a", "a" * 65, "a/b"):
            with self.subTest(value=value):
                self.assert_profile_refused(ProfileErrorCode.INVALID_ID, id=value)


class TestRoleAndLimitRejections(_TempDirTest):
    def test_unknown_role(self):
        for value in ("fable", "Screener", "routing_adviser", ""):
            with self.subTest(value=value):
                self.assert_profile_refused(ProfileErrorCode.UNKNOWN_ROLE, role=value)

    def test_disabled_roles_cannot_be_enabled(self):
        for role, timeout, cap in (("challenger", 45, 1536), ("deep_analyst", 90, 2048)):
            with self.subTest(role=role):
                enabled = {
                    **VALID_SCREENER,
                    "id": "other",
                    "role": role,
                    "context_tokens": 8192,
                    "hard_timeout_seconds": timeout,
                    "output_cap_tokens": cap,
                }
                self.assert_refused(render([screener(), enabled]), ProfileErrorCode.ROLE_DISABLED_IN_OC1)

    def test_screener_timeout_limits(self):
        for value in (0, -1, 31):
            with self.subTest(value=value):
                self.assert_profile_refused(ProfileErrorCode.TIMEOUT_OUT_OF_LIMITS, hard_timeout_seconds=value)

    def test_screener_output_cap_limits(self):
        for value in (0, 769):
            with self.subTest(value=value):
                self.assert_profile_refused(ProfileErrorCode.OUTPUT_CAP_OUT_OF_LIMITS, output_cap_tokens=value)

    def test_screener_context_limits(self):
        # Above the 4,096 total, or too small to hold the 2,800 input budget plus the output cap.
        for value in (4097, 8192, 3567, 0):
            with self.subTest(value=value):
                self.assert_profile_refused(ProfileErrorCode.CONTEXT_OUT_OF_LIMITS, context_tokens=value)

    def test_disabled_role_limits_are_still_checked(self):
        cases = (
            ("hard_timeout_seconds", 46, ProfileErrorCode.TIMEOUT_OUT_OF_LIMITS),
            ("output_cap_tokens", 1537, ProfileErrorCode.OUTPUT_CAP_OUT_OF_LIMITS),
            ("context_tokens", 8193, ProfileErrorCode.CONTEXT_OUT_OF_LIMITS),
        )
        for key, value, code in cases:
            with self.subTest(key=key):
                bad = {**VALID_CHALLENGER_DISABLED, key: value}
                self.assert_refused(render([screener(), bad]), code)

    def test_output_format_is_a_closed_enum(self):
        for value in ("json", "text", "free_text", "JSON_SCHEMA", ""):
            with self.subTest(value=value):
                self.assert_profile_refused(ProfileErrorCode.UNKNOWN_OUTPUT_FORMAT, output_format=value)

    def test_temperature_must_be_zero(self):
        for value in (0.1, 1e-9, -0.5, 1):
            with self.subTest(value=value):
                self.assert_profile_refused(ProfileErrorCode.TEMPERATURE_NOT_ZERO, temperature=value)

    def test_think_must_be_false(self):
        self.assert_profile_refused(ProfileErrorCode.THINK_ENABLED, think=True)

    def test_resource_reserves(self):
        self.assert_profile_refused(ProfileErrorCode.RESOURCE_RESERVE_TOO_LOW, reserve_vram_gib=1.49)
        self.assert_profile_refused(ProfileErrorCode.RESOURCE_RESERVE_TOO_LOW, reserve_vram_gib=0)
        self.assert_profile_refused(ProfileErrorCode.RESOURCE_RESERVE_TOO_LOW, reserve_ram_gib=3.99)
        self.assert_profile_refused(ProfileErrorCode.RESOURCE_RESERVE_TOO_LOW, reserve_ram_gib=-4)


class TestEndpointAndModelRejections(_TempDirTest):
    def test_only_loopback_endpoints(self):
        refused = (
            "https://localhost:11434",
            "https://ollama.com",
            "https://ollama.com:443",
            "http://ollama.com:11434",
            "http://api.openai.com:80",
            "http://192.168.1.10:11434",
            "http://0.0.0.0:11434",
            "http://[::1]:11434",
            "http://localhost.:11434",
            "http://LOCALHOST:11434",
            "http://localhost",
            "http://localhost:0",
            "http://user@localhost:11434",
            "http://localhost:11434/api/chat",
            "http://localhost:11434?x=1",
            "http://127.0.0.1.nip.io:11434",
            "localhost:11434",
            "",
        )
        for value in refused:
            with self.subTest(value=value):
                self.assert_profile_refused(ProfileErrorCode.ENDPOINT_REFUSED, endpoint=value)

    def test_model_names(self):
        invalid = (
            "",
            "qwen3",  # no explicit tag
            "Qwen3:14B",
            "registry.ollama.ai/library/qwen3:14b",
            "hf.co/org/model:q4",
            "a/b/c:1",
            "qwen3:14b ",
            "qwen3::14b",
            "https://ollama.com/qwen3:14b",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assert_profile_refused(ProfileErrorCode.INVALID_MODEL, model=value)

    def test_cloud_model_tags(self):
        for value in ("gpt-oss:120b-cloud", "deepseek-v3.1:671b-cloud", "kimi-k2:cloud", "library/qwen3:cloud"):
            with self.subTest(value=value):
                self.assert_profile_refused(ProfileErrorCode.CLOUD_MODEL, model=value)

    def test_local_model_names_accepted(self):
        for value in ("qwen3:14b", "gpt-oss:20b", "llama3.2:latest", "qwen3-coder:30b", "library/qwen3:14b"):
            with self.subTest(value=value):
                load_model_profiles(self.write(render([screener(model=value)])))


class TestRealVersionedFile(unittest.TestCase):
    def test_default_path_is_the_versioned_file(self):
        self.assertEqual(Path(DEFAULT_PROFILES_PATH).resolve(), REAL_FILE.resolve())
        self.assertTrue(REAL_FILE.is_file())

    def test_real_file_loads(self):
        profiles = load_model_profiles()
        self.assertEqual(profiles.schema_version, 1)
        self.assertEqual(profiles.default_profile_id, "screener-qwen3-14b")
        self.assertIs(type(load_inference_profile()), InferenceProfile)

    def test_every_profile_has_a_rationale_comment(self):
        lines = REAL_FILE.read_text(encoding="utf-8").splitlines()
        headers = [index for index, line in enumerate(lines) if line.strip() == "[[profiles]]"]
        self.assertEqual(len(headers), len(load_model_profiles().profiles))
        for index in headers:
            block = []
            cursor = index - 1
            while cursor >= 0 and lines[cursor].startswith("#"):
                block.append(lines[cursor])
                cursor -= 1
            self.assertTrue(any(line.startswith("# Rationale:") for line in block), f"line {index + 1}")

    def test_default_profile_matches_the_role_limits(self):
        profile = load_model_profiles().get("screener-qwen3-14b")
        limits = OC1_ROLE_PROFILES[Role.SCREENER]
        self.assertIs(profile.role, Role.SCREENER)
        self.assertTrue(profile.enabled)
        self.assertEqual(profile.hard_timeout_seconds, limits.hard_timeout_seconds)
        self.assertEqual(profile.output_cap_tokens, limits.output_cap_tokens)
        self.assertEqual(profile.context_tokens, OC1_ROLE_CONTEXT_TOKENS[Role.SCREENER])
        self.assertEqual((profile.hard_timeout_seconds, profile.context_tokens, profile.output_cap_tokens), (30, 4096, 768))
        self.assertGreaterEqual(profile.context_tokens - profile.output_cap_tokens, limits.max_input_tokens)
        self.assertIs(profile.output_format, OutputFormat.JSON_SCHEMA)
        self.assertEqual((profile.reserve_vram_gib, profile.reserve_ram_gib), (1.5, 4.0))

    def test_default_profile_resolves_exactly_to_todays_config_defaults(self):
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("RADAR_QWEN_") and name != "RADAR_OLLAMA_URL"
        }
        probe = (
            "import json\n"
            "from radar_v08 import config\n"
            "print(json.dumps({'model': config.QWEN_MODEL, 'timeout': config.QWEN_TIMEOUT_SECONDS,"
            " 'temperature': config.QWEN_TEMPERATURE, 'think': config.QWEN_THINK,"
            " 'ollama_url': config.OLLAMA_URL}))\n"
        )
        with tempfile.TemporaryDirectory() as state_dir:
            environment["RADAR_STATE_DIR"] = state_dir
            environment["PYTHONPATH"] = os.fspath(REPOSITORY_ROOT)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=state_dir,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        today = json.loads(completed.stdout)
        # The code defaults are what the radar runs today (D33); pin them so a drift shows here.
        self.assertEqual(today, {
            "model": "qwen3:14b",
            "timeout": 30.0,
            "temperature": 0.0,
            "think": False,
            "ollama_url": "http://localhost:11434",
        })
        profiles = load_model_profiles()
        resolved = profiles.resolve()
        profile = profiles.get(profiles.default_profile_id)
        self.assertEqual(resolved.model, today["model"])
        self.assertEqual(float(resolved.hard_timeout_seconds), today["timeout"])
        self.assertEqual(profile.temperature, today["temperature"])
        self.assertIs(resolved.think, today["think"])
        self.assertEqual(profile.endpoint, today["ollama_url"])
        self.assertIs(resolved.role, Role.SCREENER)


class TestLoaderHasNoSideEffects(_TempDirTest):
    def _load_guarded(self, path):
        real_open = builtins.open
        opened = []

        def recording_open(file, mode="r", *args, **kwargs):
            opened.append((os.fspath(file), mode))
            return real_open(file, mode, *args, **kwargs)

        def no_socket(*args, **kwargs):
            raise AssertionError("the profile loader must not open a socket")

        with (
            mock.patch("builtins.open", side_effect=recording_open),
            mock.patch.object(socket, "socket", side_effect=no_socket),
            mock.patch.object(socket, "create_connection", side_effect=no_socket),
            mock.patch.object(socket, "getaddrinfo", side_effect=no_socket),
        ):
            result = load_model_profiles(path)
        return result, opened

    def test_reads_once_in_binary_mode_and_writes_nothing(self):
        path = self.write(render([screener(), VALID_CHALLENGER_DISABLED]))
        before = {entry.name: (entry.stat().st_size, entry.stat().st_mtime_ns) for entry in self.tmp.iterdir()}
        _, opened = self._load_guarded(path)
        after = {entry.name: (entry.stat().st_size, entry.stat().st_mtime_ns) for entry in self.tmp.iterdir()}
        self.assertEqual(opened, [(os.fspath(path), "rb")])
        self.assertEqual(before, after)

    def test_real_file_is_only_read(self):
        stat_before = REAL_FILE.stat()
        _, opened = self._load_guarded(DEFAULT_PROFILES_PATH)
        stat_after = REAL_FILE.stat()
        self.assertEqual(opened, [(os.fspath(DEFAULT_PROFILES_PATH), "rb")])
        self.assertEqual((stat_before.st_size, stat_before.st_mtime_ns), (stat_after.st_size, stat_after.st_mtime_ns))

    def test_rejections_also_open_no_socket(self):
        path = self.write(render([screener(endpoint="https://ollama.com")]))
        with self.assertRaises(ModelProfileError):
            self._load_guarded(path)

    def test_source_has_no_write_or_network_calls(self):
        source = Path(mp.__file__).read_text(encoding="utf-8")
        for pattern in (r"\bimport socket\b", r"\bimport requests\b", r"\.write", r"tomllib\.loads", r"getenv", r"environ"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, source))
        # The only open() call in the module is the binary read of the profiles file.
        self.assertEqual(re.findall(r"\bopen\([^)]*\)", source), ['open(path, "rb")'])


if __name__ == "__main__":
    unittest.main()
