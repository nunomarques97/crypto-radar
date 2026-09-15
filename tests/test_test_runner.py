from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import run_tests


class TestTestRunnerEnvironment(unittest.TestCase):
    def test_child_uses_disposable_paths_and_cannot_see_sensitive_values(self):
        with tempfile.TemporaryDirectory() as original_dir, tempfile.TemporaryDirectory() as state_dir:
            original = Path(original_dir)
            sentinel = original / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            base_environment = {
                "PATH": "C:\\Windows\\System32",
                "RADAR_STATE_DIR": str(original),
                "RADAR_SQLITE_PATH": str(original / "production.sqlite"),
                "RADAR_EVENTS_LOG_PATH": str(original / "events.jsonl"),
                "RADAR_OUTPUT_V08_PATH": str(original / "output.json"),
                "RADAR_ASSET_PAIRS_CACHE_PATH": str(original / "cache.json"),
                "ANTHROPIC_API_KEY": "seeded-cloud-secret",
                "ANTHROPIC_AUTH_TOKEN": "seeded-cloud-token",
                "KRAKEN_API_KEY": "seeded-kraken-key",
                "KRAKEN_SECRET": "seeded-kraken-secret",
                "CRYPTO_RADAR_NTFY_TOPIC": "seeded-destination",
            }
            environment = run_tests.build_test_environment(state_dir, base_environment)
            child = """
import json
import os
from pathlib import Path
from radar_v08 import config

state = Path(os.environ[\"RADAR_STATE_DIR\"]).resolve()
paths = [
    config.STATE_DIR, config.SQLITE_PATH, config.ASSET_PAIRS_CACHE_PATH,
    config.RUN_LOG_PATH, config.TEXT_LOG_PATH, config.OUTPUT_V08_PATH,
    config.OUTPUT_V07_PATH, config.SHADOW_COMPARISON_PATH, config.EVENTS_LOG_PATH,
]
assert all(Path(path).resolve().is_relative_to(state) for path in paths)
blocked = [\"ANTHROPIC_API_KEY\", \"ANTHROPIC_AUTH_TOKEN\", \"KRAKEN_API_KEY\", \"KRAKEN_SECRET\", \"CRYPTO_RADAR_NTFY_TOPIC\"]
assert not any(os.environ.get(name) for name in blocked)
print(json.dumps({\"state\": str(state), \"paths_ok\": True, \"credentials_absent\": True}))
"""
            result = subprocess.run(
                [sys.executable, "-c", child],
                cwd=run_tests.REPOSITORY_ROOT,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["state"], str(Path(state_dir).resolve()))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(list(original.iterdir()), [sentinel])

    def test_unittest_subprocess_exit_code_is_propagated(self):
        result = mock.Mock(returncode=23)
        with mock.patch.object(run_tests.subprocess, "run", return_value=result) as run:
            exit_code = run_tests.run_unittest_suite("unused-state", {"PATH": "node-path"})

        self.assertEqual(exit_code, 23)
        self.assertEqual(run.call_args.kwargs["cwd"], run_tests.REPOSITORY_ROOT)
        self.assertEqual(run.call_args.args[0], [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])

    def test_missing_node_is_a_setup_failure(self):
        with mock.patch.object(run_tests.shutil, "which", return_value=None), \
             mock.patch.object(run_tests, "run_unittest_suite") as run:
            exit_code = run_tests.main()

        self.assertEqual(exit_code, 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
