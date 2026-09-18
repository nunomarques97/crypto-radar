"""Offline tests for the repository static quality gate (scripts/run_quality.py)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
QUALITY_SCRIPT = REPOSITORY_ROOT / "scripts" / "run_quality.py"
QUALITY_SPEC = importlib.util.spec_from_file_location("run_quality", QUALITY_SCRIPT)
assert QUALITY_SPEC is not None and QUALITY_SPEC.loader is not None
quality = importlib.util.module_from_spec(QUALITY_SPEC)
sys.modules[QUALITY_SPEC.name] = quality
QUALITY_SPEC.loader.exec_module(quality)

FIXTURE_FILES = {
    "radar_v08/__init__.py": "",
    "radar_v08/core.py": "VALUE = 1\n",
    "scripts/tool.py": "print('tool')\n",
    "ui/__init__.py": "",
    "tests/test_fixture.py": "import unittest\n",
    "radar.py": "print('radar')\n",
}
MYPY_FIXTURE_FILES = [path for path in FIXTURE_FILES if not path.startswith("tests/")]


def write_file(root: Path, relative_path: str, source: str = "") -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def make_repository(root: Path) -> None:
    """Create the minimal required layout plus the real pyproject.toml."""
    for relative_path, source in FIXTURE_FILES.items():
        write_file(root, relative_path, source)
    shutil.copyfile(REPOSITORY_ROOT / "pyproject.toml", root / "pyproject.toml")


class FakeRunner:
    """Stand-in for the tool subprocesses: returns canned versions and findings, records calls."""

    def __init__(
        self,
        repository_root: Path,
        ruff: Sequence[tuple[str, int, str, str]] = (),
        mypy: Sequence[tuple[str, int, str, str]] = (),
        versions: dict[str, str] | None = None,
    ) -> None:
        self.repository_root = repository_root
        self.ruff = list(ruff)
        self.mypy = list(mypy)
        self.versions = versions or {"ruff": "0.16.7", "mypy": "2.3.1"}
        self.calls: list[tuple[str, ...]] = []
        self.overrides: dict[tuple[str, str], object] = {}

    def __call__(self, command: Sequence[str], repository_root: Path) -> object:
        command = tuple(command)
        self.calls.append(command)
        tool = command[2]
        kind = "version" if "--version" in command else "check"
        if (tool, kind) in self.overrides:
            return self.overrides[(tool, kind)]
        if kind == "version":
            suffix = " (compiled: yes)" if tool == "mypy" else ""
            return quality.CommandResult(0, f"{tool} {self.versions[tool]}{suffix}\n", "")
        if tool == "ruff":
            entries = [
                {
                    "filename": str(self.repository_root / path),
                    "location": {"row": line, "column": 1},
                    "code": code,
                    "message": message,
                }
                for path, line, code, message in self.ruff
            ]
            return quality.CommandResult(1 if entries else 0, json.dumps(entries), "")
        lines = [
            json.dumps(
                {
                    "file": path.replace("/", "\\"),
                    "line": line,
                    "column": 0,
                    "message": message,
                    "hint": None,
                    "code": code,
                    "severity": "error",
                }
            )
            for path, line, code, message in self.mypy
        ]
        return quality.CommandResult(1 if lines else 0, "\n".join(lines) + ("\n" if lines else ""), "")

    def check_command(self, tool: str) -> tuple[str, ...]:
        matches = [call for call in self.calls if call[2] == tool and "--version" not in call]
        assert len(matches) == 1, self.calls
        return matches[0]


def run_gate(root: Path, runner: FakeRunner, write: bool = False) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        if write:
            status = quality.write_baseline(root, runner)
        else:
            status = quality.run_quality_checks(root, runner)
    return status, stdout.getvalue(), stderr.getvalue()


class ImportBoundaryTests(unittest.TestCase):
    def assert_rejected(self, relative_path: str, source: str, expected_import: str) -> None:
        with tempfile.TemporaryDirectory(prefix="crypto-radar-quality-") as temporary_directory:
            root = Path(temporary_directory)
            write_file(root, relative_path, source)
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


class GateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="crypto-radar-quality-")
        self.root = Path(self._directory.name)
        make_repository(self.root)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_absent_critical_packages_are_accepted(self) -> None:
        runner = FakeRunner(self.root)
        status, stdout, stderr = run_gate(self.root, runner)
        self.assertEqual(0, status, stderr)
        for relative_root in ("radar_v08/domain", "radar_v08/workflow", "radar_v08/adapters", "radar_v08/execution"):
            self.assertIn(f"Critical package {relative_root}: absent, 0 Python files inspected.", stdout)
        self.assertIn(
            "ruff 0.16.7: inspecting 6 Python files (radar_v08: 2, scripts: 1, ui: 1, tests: 1, radar.py: 1).", stdout
        )
        self.assertIn("mypy 2.3.1: inspecting 5 Python files (radar_v08: 2, scripts: 1, ui: 1, radar.py: 1).", stdout)
        self.assertIn("Import-boundary check passed.", stdout)
        self.assertIn("Quality gate passed.", stdout)
        self.assertEqual([], quality.check_import_boundaries(self.root))

    def test_tools_receive_the_explicit_real_layout_files(self) -> None:
        runner = FakeRunner(self.root)
        self.assertEqual(0, run_gate(self.root, runner)[0])
        self.assertEqual(["ruff", "ruff", "mypy", "mypy"], [call[2] for call in runner.calls])
        self.assertEqual(sorted(FIXTURE_FILES), sorted(runner.check_command("ruff")[-len(FIXTURE_FILES):]))
        mypy_command = runner.check_command("mypy")
        self.assertEqual(sorted(MYPY_FIXTURE_FILES), sorted(mypy_command[-len(MYPY_FIXTURE_FILES):]))
        self.assertNotIn("tests/test_fixture.py", mypy_command)

    def test_missing_required_root_fails(self) -> None:
        for relative_root in ("radar_v08", "scripts", "ui", "tests", "radar.py"):
            with self.subTest(root=relative_root), tempfile.TemporaryDirectory(prefix="crypto-radar-quality-") as other:
                root = Path(other)
                make_repository(root)
                target = root / relative_root
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                runner = FakeRunner(root)
                status, stdout, stderr = run_gate(root, runner)
                self.assertEqual(2, status)
                self.assertIn(f"required root {relative_root}", stderr)
                self.assertIn("is missing", stderr)
                self.assertNotIn("Quality gate passed.", stdout)
                self.assertEqual([], runner.calls)

    def test_zero_python_files_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="crypto-radar-quality-") as other:
            root = Path(other)
            for directory in ("radar_v08", "scripts", "ui", "tests"):
                write_file(root, f"{directory}/README.txt", "not python\n")
                write_file(root, f"{directory}/__pycache__/stale.py", "")
            shutil.copyfile(REPOSITORY_ROOT / "pyproject.toml", root / "pyproject.toml")
            runner = FakeRunner(root)
            status, stdout, stderr = run_gate(root, runner)
            self.assertEqual(2, status)
            self.assertIn("required root radar_v08/ contains 0 Python files", stderr)
            self.assertNotIn("Quality gate passed.", stdout)
            self.assertEqual([], runner.calls)
            with self.assertRaisesRegex(quality.QualitySetupError, "0 Python files"):
                quality.collect_tool_files(root, ())

    def test_new_finding_outside_baseline_fails_and_is_listed(self) -> None:
        self.assertEqual(0, run_gate(self.root, FakeRunner(self.root), write=True)[0])
        runner = FakeRunner(self.root, ruff=[("radar_v08/core.py", 3, "F401", "`os` imported but unused")])
        status, stdout, stderr = run_gate(self.root, runner)
        self.assertEqual(1, status)
        self.assertIn("NEW ruff: radar_v08/core.py:3: [F401] `os` imported but unused", stderr)
        self.assertIn("ruff: 1 findings, 0 covered by baseline, 1 new", stdout)
        self.assertIn("Quality gate FAILED.", stdout)

    def test_baselined_finding_passes(self) -> None:
        findings = [("ui/__init__.py", 7, "arg-type", 'Argument 1 has incompatible type "str | None"')]
        self.assertEqual(0, run_gate(self.root, FakeRunner(self.root, mypy=findings), write=True)[0])
        status, stdout, stderr = run_gate(self.root, FakeRunner(self.root, mypy=findings))
        self.assertEqual(0, status, stderr)
        self.assertIn("mypy: 1 findings, 1 covered by baseline, 0 new", stdout)
        self.assertIn("Quality gate passed.", stdout)

    def test_line_only_change_is_still_covered(self) -> None:
        before = [("radar.py", 10, "assignment", "Name defined on line 10 is reassigned")]
        after = [("radar.py", 42, "assignment", "Name defined on line 42 is reassigned")]
        self.assertEqual(0, run_gate(self.root, FakeRunner(self.root, mypy=before), write=True)[0])
        document = json.loads((self.root / "scripts/quality_baseline.json").read_text(encoding="utf-8"))
        expected_entry = {
            "tool": "mypy",
            "path": "radar.py",
            "code": "assignment",
            "message": "Name defined on line N is reassigned",
            "count": 1,
        }
        self.assertEqual([expected_entry], document["findings"])
        status, _, stderr = run_gate(self.root, FakeRunner(self.root, mypy=after))
        self.assertEqual(0, status, stderr)

    def test_extra_occurrence_of_a_baselined_finding_fails(self) -> None:
        finding = ("scripts/tool.py", 1, "F401", "`sys` imported but unused")
        extra = ("scripts/tool.py", 9, "F401", "`sys` imported but unused")
        self.assertEqual(0, run_gate(self.root, FakeRunner(self.root, ruff=[finding]), write=True)[0])
        status, _, stderr = run_gate(self.root, FakeRunner(self.root, ruff=[finding, extra]))
        self.assertEqual(1, status)
        self.assertIn("NEW ruff: scripts/tool.py:9: [F401]", stderr)

    def test_finding_in_critical_package_fails_and_is_never_baselined(self) -> None:
        write_file(self.root, "radar_v08/domain/__init__.py")
        write_file(self.root, "radar_v08/domain/value.py", "def f(x): return x\n")
        path, code, message = "radar_v08/domain/value.py", "no-untyped-def", "Function is missing a type annotation"
        finding = [(path, 1, code, message)]

        status, _, stderr = run_gate(self.root, FakeRunner(self.root, mypy=finding), write=True)
        self.assertEqual(1, status)
        self.assertIn("critical packages may never be baselined", stderr)
        self.assertFalse((self.root / "scripts/quality_baseline.json").exists())

        status, _, stderr = run_gate(self.root, FakeRunner(self.root, mypy=finding))
        self.assertEqual(1, status)
        self.assertIn("NEW mypy: radar_v08/domain/value.py:1: [no-untyped-def]", stderr)
        self.assertIn("(critical package domain: never baselined)", stderr)

        hand_written = {
            "format_version": 1,
            "tools": {"ruff": "0.16.7", "mypy": "2.3.1"},
            "findings": [{"tool": "mypy", "path": path, "code": code, "message": message, "count": 1}],
        }
        (self.root / "scripts/quality_baseline.json").write_text(json.dumps(hand_written), encoding="utf-8")
        status, _, stderr = run_gate(self.root, FakeRunner(self.root, mypy=finding))
        self.assertEqual(2, status)
        self.assertIn("critical packages may never be baselined", stderr)

    def test_present_critical_package_is_inspected_under_the_strict_contract(self) -> None:
        write_file(self.root, "radar_v08/domain/__init__.py")
        write_file(self.root, "radar_v08/domain/value.py", "VALUE: int = 1\n")
        runner = FakeRunner(self.root)
        status, stdout, stderr = run_gate(self.root, runner)
        self.assertEqual(0, status, stderr)
        self.assertIn("Critical package radar_v08/domain: 2 Python files", stdout)
        self.assertIn("radar_v08/domain/value.py", runner.check_command("mypy"))
        self.assertIn("radar_v08/domain/value.py", runner.check_command("ruff"))

        write_file(self.root, "radar_v08/domain/value.py", "import sqlite3\n")
        status, _, stderr = run_gate(self.root, FakeRunner(self.root))
        self.assertEqual(1, status)
        self.assertIn("domain may not import sqlite3", stderr)

    def test_critical_package_without_init_fails(self) -> None:
        write_file(self.root, "radar_v08/workflow/service.py", "VALUE = 1\n")
        status, _, stderr = run_gate(self.root, FakeRunner(self.root))
        self.assertEqual(2, status)
        self.assertIn("radar_v08/workflow/ has no __init__.py", stderr)

    def test_removed_strict_mypy_override_fails(self) -> None:
        pyproject = self.root / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text(encoding="utf-8").replace("disallow_untyped_defs = true\n", ""), encoding="utf-8"
        )
        status, _, stderr = run_gate(self.root, FakeRunner(self.root))
        self.assertEqual(2, status)
        self.assertIn("lost the strict mypy override for radar_v08.domain", stderr)

    def test_tool_version_other_than_the_baseline_fails(self) -> None:
        self.assertEqual(0, run_gate(self.root, FakeRunner(self.root), write=True)[0])
        status, _, stderr = run_gate(self.root, FakeRunner(self.root, versions={"ruff": "0.16.7", "mypy": "2.4.0"}))
        self.assertEqual(2, status)
        self.assertIn("baseline was written with mypy 2.3.1 but the gate is running mypy 2.4.0", stderr)

    def test_missing_or_failing_tool_fails(self) -> None:
        cases = {
            ("ruff", "version"): quality.CommandResult(2, "", "No module named ruff"),
            ("mypy", "check"): quality.CommandResult(2, "", "mypy: error: Duplicate module"),
            ("ruff", "check"): quality.CommandResult(1, "[]", ""),
            ("mypy", "version"): quality.CommandResult(0, "", ""),
        }
        for key, result in cases.items():
            with self.subTest(case=key):
                runner = FakeRunner(self.root)
                runner.overrides[key] = result
                status, stdout, stderr = run_gate(self.root, runner)
                self.assertEqual(2, status)
                self.assertIn("Quality setup error", stderr)
                self.assertNotIn("Quality gate passed.", stdout)

    def test_mypy_configuration_note_is_not_a_finding_but_other_text_fails(self) -> None:
        note = "pyproject.toml: \x1b[94mnote:\x1b[0m unused section(s): module = ['radar_v08.domain']\x1b[0m\n"
        runner = FakeRunner(self.root)
        runner.overrides[("mypy", "check")] = quality.CommandResult(0, note, "")
        status, stdout, stderr = run_gate(self.root, runner)
        self.assertEqual(0, status, stderr)
        self.assertIn("mypy pyproject.toml: note: unused section(s)", stdout)

        runner = FakeRunner(self.root)
        runner.overrides[("mypy", "check")] = quality.CommandResult(1, "radar.py:1: error: broken [misc]\n", "")
        status, _, stderr = run_gate(self.root, runner)
        self.assertEqual(2, status)
        self.assertIn("non-JSON output line", stderr)


class EntryPointTests(unittest.TestCase):
    def test_write_baseline_never_runs_by_default(self) -> None:
        with (
            mock.patch.object(quality, "run_quality_checks", return_value=0) as gate,
            mock.patch.object(quality, "write_baseline", return_value=0) as writer,
        ):
            self.assertEqual(0, quality.main([]))
            gate.assert_called_once_with()
            writer.assert_not_called()
            self.assertEqual(0, quality.main(["--write-baseline"]))
            writer.assert_called_once_with()
            gate.assert_called_once_with()

    def test_main_uses_script_relative_root_from_another_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="crypto-radar-quality-") as temporary_directory:
            original_cwd = Path.cwd()
            observed: list[Path] = []

            def record_root(root: Path = quality.REPOSITORY_ROOT) -> int:
                observed.append(root)
                return 0

            try:
                os.chdir(temporary_directory)
                with mock.patch.object(quality, "run_quality_checks", side_effect=record_root):
                    self.assertEqual(0, quality.main([]))
            finally:
                os.chdir(original_cwd)
        self.assertEqual([REPOSITORY_ROOT], observed)


class VersionedBaselineTests(unittest.TestCase):
    def test_repository_baseline_is_valid_pinned_and_excludes_critical_packages(self) -> None:
        baseline = quality.load_baseline(REPOSITORY_ROOT / "scripts" / "quality_baseline.json")
        self.assertIsNotNone(baseline)
        assert baseline is not None
        requirements = (REPOSITORY_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
        pins = dict(line.strip().split("==", 1) for line in requirements if "==" in line)
        self.assertEqual({"ruff": pins["ruff"], "mypy": pins["mypy"]}, dict(baseline.tools))
        self.assertTrue(baseline.counts)
        self.assertTrue(all(quality.critical_package_for(path) is None for _, path, _, _ in baseline.counts))


if __name__ == "__main__":
    unittest.main()
