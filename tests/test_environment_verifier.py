from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import verify_environment


class EnvironmentVerifierTests(unittest.TestCase):
    def test_reads_recursive_exact_pins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "base.txt").write_text("requests==2.34.2\n", encoding="utf-8")
            (root / "group.txt").write_text("-r base.txt\nruff==0.16.7\n", encoding="utf-8")
            self.assertEqual(
                verify_environment.read_pinned_requirements(root / "group.txt"),
                {"requests": "2.34.2", "ruff": "0.16.7"},
            )

    def test_reports_missing_and_incompatible_packages(self) -> None:
        with mock.patch.object(verify_environment, "installed_version", side_effect=[None, "1.0"]):
            errors = verify_environment.check_requirements({"missing": "2.0", "wrong": "2.0"})
        self.assertEqual(len(errors), 2)
        self.assertIn("Missing required package", errors[0])
        self.assertIn("Incompatible package", errors[1])

    def test_reports_missing_node(self) -> None:
        with mock.patch.object(sys, "version_info", (3, 12, 0)), mock.patch.object(
            verify_environment.platform, "system", return_value="Windows"
        ):
            errors = verify_environment.check_tools("core", which=lambda _: None)
        self.assertEqual(errors, ["Node.js is required for the embedded UI test suites"])

    def test_no_effect_import_uses_temporary_state_and_sanitizes_credentials(self) -> None:
        observed: dict[str, object] = {}

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            observed["command"] = command
            observed["environment"] = kwargs["env"]
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret", "RADAR_STATE_DIR": "live"}, clear=False), mock.patch.object(
            verify_environment.subprocess, "run", side_effect=run
        ):
            self.assertEqual(verify_environment.check_imports(("radar_v08.http_client",), Path.cwd()), [])
        environment = observed["environment"]
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotEqual(environment["RADAR_STATE_DIR"], "live")

    def test_main_propagates_failure(self) -> None:
        with mock.patch.object(verify_environment, "verify", return_value=["failure"]):
            self.assertEqual(verify_environment.main(["--group", "core"]), 1)


if __name__ == "__main__":
    unittest.main()
