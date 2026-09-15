"""Subprocess coverage for the side-effect-free radar.py entry parser."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RADAR_PATH = REPOSITORY_ROOT / "radar.py"


class CliModeParsingTestCase(unittest.TestCase):
    def _environment_with_import_traps(self, trap_directory: Path) -> dict[str, str]:
        (trap_directory / "sitecustomize.py").write_text(
            textwrap.dedent(
                """
                import sys
                import types

                def blocked(*args, **kwargs):
                    raise AssertionError("parser path attempted a blocked side effect")

                requests = types.ModuleType("requests")
                requests.get = blocked
                requests.post = blocked
                requests.RequestException = RuntimeError
                sys.modules["requests"] = requests

                class BlockV08Runtime:
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname == "radar_v08" or fullname.startswith("radar_v08."):
                            raise AssertionError("parser path imported the v0.8 runtime")
                        return None

                sys.meta_path.insert(0, BlockV08Runtime())
                """
            ),
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(trap_directory), environment.get("PYTHONPATH")))
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment

    def _subprocess(self, arguments: list[str], environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RADAR_PATH), *arguments],
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def _run_routing_probe(self, arguments: list[str]) -> dict[str, object]:
        probe = textwrap.dedent(
            f"""
            import json
            import runpy
            import sys
            import types

            module = runpy.run_path({str(RADAR_PATH)!r}, run_name="radar_cli_probe")
            calls = []

            def legacy_main():
                calls.append(["v07"])
                return 71

            def run_mode(mode, argv=None):
                calls.append(["v08", mode, argv])
                return 72

            package = types.ModuleType("radar_v08")
            package.__path__ = []
            cli = types.ModuleType("radar_v08.cli")
            cli.run_mode = run_mode
            sys.modules["radar_v08"] = package
            sys.modules["radar_v08.cli"] = cli
            module["entry_main"].__globals__["main"] = legacy_main
            exit_code = module["entry_main"]({arguments!r})
            print(json.dumps({{"calls": calls, "exit_code": exit_code}}))
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_no_arguments_select_v08_heartbeat_without_runtime_imports(self):
        result = self._run_routing_probe([])
        self.assertEqual(result, {"calls": [["v08", "heartbeat", []]], "exit_code": 72})

    def test_help_prints_usage_without_runtime_or_legacy_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._subprocess(["--help"], self._environment_with_import_traps(Path(directory)))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())
        self.assertNotIn("KRAKEN QWEN RADAR", result.stdout)
        self.assertNotIn("blocked side effect", result.stderr)

    def test_unknown_flag_exits_two_with_usage_and_no_runtime_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._subprocess(["--unknown"], self._environment_with_import_traps(Path(directory)))

        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr.lower())
        self.assertNotIn("KRAKEN QWEN RADAR", result.stdout)

    def test_unsupported_mode_exits_two_with_usage_and_no_runtime_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._subprocess(["--mode", "legacy"], self._environment_with_import_traps(Path(directory)))

        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr.lower())
        self.assertIn("invalid choice", result.stderr)

    def test_malformed_mode_event_combinations_exit_two_without_runtime_imports(self):
        for arguments in (
            ["--mode", "prompt"],
            ["--mode", "heartbeat", "--event", "event-1"],
            ["--mode", "heartbeat", "--mode", "shadow"],
            ["--mode", "prompt", "--event", "one", "--event", "two"],
        ):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as directory:
                result = self._subprocess(arguments, self._environment_with_import_traps(Path(directory)))
            self.assertEqual(result.returncode, 2)
            self.assertIn("usage:", result.stderr.lower())

    def test_explicit_v07_and_shadow_routes_remain_distinct(self):
        self.assertEqual(self._run_routing_probe(["--mode", "v07"]), {"calls": [["v07"]], "exit_code": 71})
        self.assertEqual(
            self._run_routing_probe(["--mode", "shadow"]),
            {"calls": [["v08", "shadow", []]], "exit_code": 72},
        )

    def test_mode_aliases_are_not_accepted(self):
        for arguments in (["--mode", "HEARTBEAT"], ["-h"]):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as directory:
                result = self._subprocess(arguments, self._environment_with_import_traps(Path(directory)))

            self.assertEqual(result.returncode, 2)
            self.assertIn("usage:", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
