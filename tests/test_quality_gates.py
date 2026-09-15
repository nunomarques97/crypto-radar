"""Focused tests for the future-critical static quality gates."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


QUALITY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_quality.py"
QUALITY_SPEC = importlib.util.spec_from_file_location("run_quality", QUALITY_SCRIPT)
assert QUALITY_SPEC is not None and QUALITY_SPEC.loader is not None
quality = importlib.util.module_from_spec(QUALITY_SPEC)
sys.modules[QUALITY_SPEC.name] = quality
QUALITY_SPEC.loader.exec_module(quality)


class ImportBoundaryTests(unittest.TestCase):
    def write_fixture(self, root: Path, relative_path: str, source: str) -> None:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def assert_rejected(self, relative_path: str, source: str, expected_import: str) -> None:
        with tempfile.TemporaryDirectory(prefix="crypto-radar-quality-") as temporary_directory:
            root = Path(temporary_directory)
            self.write_fixture(root, relative_path, source)
            violations = quality.check_import_boundaries(root)
        self.assertTrue(any(violation.imported == expected_import for violation in violations), violations)

    def test_domain_adapter_import_is_rejected(self) -> None:
        self.assert_rejected("radar_v08/domain/value.py", "import radar_v08.adapters.clock\n", "radar_v08.adapters.clock")

    def test_workflow_adapter_import_is_rejected(self) -> None:
        self.assert_rejected("radar_v08/workflow/service.py", "import radar_v08.adapters.clock\n", "radar_v08.adapters.clock")

    def test_execution_exchange_adapter_import_is_rejected(self) -> None:
        self.assert_rejected(
            "radar_v08/execution/planner.py",
            "from radar_v08.adapters import kraken_private\n",
            "radar_v08.adapters.kraken_private",
        )

    def test_absent_critical_packages_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="crypto-radar-quality-") as temporary_directory:
            self.assertEqual([], quality.check_import_boundaries(Path(temporary_directory)))


class QualityRunnerTests(unittest.TestCase):
    def test_success_path_checks_tools_and_accepts_empty_roots(self) -> None:
        calls: list[tuple[str, ...]] = []

        def successful_runner(command: tuple[str, ...], repository_root: Path) -> int:
            calls.append(command)
            self.assertTrue(repository_root.is_dir())
            return 0

        with tempfile.TemporaryDirectory(prefix="crypto-radar-quality-") as temporary_directory:
            self.assertEqual(0, quality.run_quality_checks(Path(temporary_directory), successful_runner))
        self.assertEqual(["ruff", "mypy"], [call[2] for call in calls])

    def test_missing_or_failing_child_status_propagates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="crypto-radar-quality-") as temporary_directory:
            root = Path(temporary_directory)
            (root / "radar_v08/domain").mkdir(parents=True)

            def failing_runner(command: tuple[str, ...], repository_root: Path) -> int:
                return 7 if command[2] == "ruff" and "check" in command else 0

            self.assertEqual(7, quality.run_quality_checks(root, failing_runner))

            def missing_runner(command: tuple[str, ...], repository_root: Path) -> int:
                return 2 if command[2] == "ruff" and "--version" in command else 0

            self.assertEqual(2, quality.run_quality_checks(root, missing_runner))

    def test_main_uses_script_relative_root_from_another_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="crypto-radar-quality-") as temporary_directory:
            original_cwd = Path.cwd()
            observed: list[Path] = []
            try:
                os.chdir(temporary_directory)
                with mock.patch.object(quality, "run_quality_checks", side_effect=lambda root=quality.REPOSITORY_ROOT: observed.append(root) or 0):
                    self.assertEqual(0, quality.main([]))
            finally:
                os.chdir(original_cwd)
        self.assertEqual([quality.REPOSITORY_ROOT], observed)


if __name__ == "__main__":
    unittest.main()
