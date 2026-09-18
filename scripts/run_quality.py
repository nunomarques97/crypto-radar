"""Run the repository static quality gate without touching runtime state.

Ruff and mypy inspect the real package layout (the required roots below). A
required root that is missing or holds no Python file, or a tool that would
inspect zero files, is a setup error: the gate never passes on nothing. Legacy
findings are covered only by the versioned baseline ``scripts/quality_baseline.json``;
any finding outside it fails. The future critical packages under ``radar_v08``
are optional until they exist; when they do, they keep the strict mypy override
and the import-boundary contract, and the baseline may never cover them.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
CRITICAL_PACKAGE_ROOTS = {
    "domain": Path("radar_v08/domain"),
    "workflow": Path("radar_v08/workflow"),
    "adapters": Path("radar_v08/adapters"),
    "execution": Path("radar_v08/execution"),
}

DOMAIN_FORBIDDEN_PREFIXES = (
    "ui",
    "radar_v08.adapters",
    "radar_v08.execution",
    "radar_v08.config",
    "config",
    "socket",
    "http",
    "urllib",
    "requests",
    "httpx",
    "aiohttp",
    "websockets",
    "os",
    "pathlib",
    "shutil",
    "tempfile",
    "glob",
    "sqlite3",
    "anthropic",
    "ollama",
    "openai",
    "transformers",
    "langchain",
    "llama_cpp",
    "radar_v08.qwen",
    "radar_v08.claude_bridge",
    "radar_v08.http_client",
    "radar_v08.kraken_spot",
    "radar_v08.kraken_futures",
)
WORKFLOW_FORBIDDEN_PREFIXES = ("ui", "radar_v08.adapters")
EXECUTION_FORBIDDEN_PREFIXES = (
    "ui",
    "anthropic",
    "ollama",
    "openai",
    "transformers",
    "langchain",
    "llama_cpp",
    "radar_v08.qwen",
    "radar_v08.claude_bridge",
    "radar_v08.kraken_spot",
    "radar_v08.kraken_futures",
    "radar_v08.http_client",
)
EXECUTION_ADAPTER_IMPLEMENTATION_MARKERS = (
    "kraken",
    "exchange",
    "private",
    "spot",
    "futures",
)


@dataclass(frozen=True)
class BoundaryViolation:
    """A prohibited import found in one future critical package."""

    path: Path
    line: int
    package: str
    imported: str
    reason: str

    def format(self, repository_root: Path) -> str:
        try:
            display_path = self.path.relative_to(repository_root)
        except ValueError:
            display_path = self.path
        return f"{display_path}:{self.line}: {self.package} may not import {self.imported} ({self.reason})"


def discover_critical_package_roots(repository_root: Path = REPOSITORY_ROOT) -> dict[str, Path]:
    """Return only future critical package directories that currently exist."""
    return {
        package: repository_root / relative_root
        for package, relative_root in CRITICAL_PACKAGE_ROOTS.items()
        if (repository_root / relative_root).is_dir()
    }


def module_name_for(path: Path, repository_root: Path) -> str:
    """Return a dotted module name for a repository-relative Python source file."""
    parts = list(path.relative_to(repository_root).with_suffix("").parts)
    return ".".join(parts)


def _is_prefix(imported: str, prefix: str) -> bool:
    return imported == prefix or imported.startswith(f"{prefix}.")


def _relative_base(module_name: str, level: int, imported_module: str | None) -> str:
    """Resolve the base module in a ``from ... import ...`` statement."""
    if not level:
        return imported_module or ""
    package_parts = module_name.split(".")
    if not module_name.endswith(".__init__"):
        package_parts.pop()
    package_parts = package_parts[: len(package_parts) - (level - 1)]
    if imported_module:
        package_parts.extend(imported_module.split("."))
    return ".".join(part for part in package_parts if part)


def import_targets(node: ast.Import | ast.ImportFrom, module_name: str) -> Iterable[str]:
    """Yield canonical targets including names imported from a package."""
    if isinstance(node, ast.Import):
        yield from (alias.name for alias in node.names)
        return

    base = _relative_base(module_name, node.level, node.module)
    if base:
        yield base
    for alias in node.names:
        if alias.name != "*":
            yield f"{base}.{alias.name}" if base else alias.name


def forbidden_reason(package: str, imported: str) -> str | None:
    """Return the violated import rule for an import target, if any."""
    if package == "domain" and any(_is_prefix(imported, prefix) for prefix in DOMAIN_FORBIDDEN_PREFIXES):
        return "domain must remain independent of I/O, UI, adapters, execution, models, and runtime configuration"
    if package == "workflow" and any(_is_prefix(imported, prefix) for prefix in WORKFLOW_FORBIDDEN_PREFIXES):
        return "workflow must use injected ports instead of UI or adapters"
    if package == "execution":
        if any(_is_prefix(imported, prefix) for prefix in EXECUTION_FORBIDDEN_PREFIXES):
            return "execution must not import UI, model SDKs, or exchange implementations"
        if _is_prefix(imported, "radar_v08.adapters"):
            suffix = imported.removeprefix("radar_v08.adapters.")
            if any(marker in suffix for marker in EXECUTION_ADAPTER_IMPLEMENTATION_MARKERS):
                return "execution must not import private or exchange adapter implementations"
    return None


def check_import_boundaries(repository_root: Path = REPOSITORY_ROOT) -> list[BoundaryViolation]:
    """Inspect existing future critical roots for imports that cross their contracts."""
    violations: list[BoundaryViolation] = []
    for package, root in discover_critical_package_roots(repository_root).items():
        for path in sorted(root.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError) as error:
                violations.append(
                    BoundaryViolation(path, getattr(error, "lineno", 0) or 0, package, "<source>", str(error))
                )
                continue
            module_name = module_name_for(path, repository_root)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                for imported in import_targets(node, module_name):
                    reason = forbidden_reason(package, imported)
                    if reason:
                        violations.append(BoundaryViolation(path, node.lineno, package, imported, reason))
    return violations




REQUIRED_TOOL_ROOTS: dict[str, tuple[str, ...]] = {
    "ruff": ("radar_v08", "scripts", "ui", "tests", "radar.py"),
    "mypy": ("radar_v08", "scripts", "ui", "radar.py"),
}
TOOLS = tuple(REQUIRED_TOOL_ROOTS)
BASELINE_RELATIVE_PATH = Path("scripts/quality_baseline.json")
BASELINE_FORMAT_VERSION = 1
SKIPPED_DIRECTORY_NAMES = frozenset({"__pycache__"})
STRICT_MYPY_FLAGS = (
    "check_untyped_defs",
    "disallow_any_generics",
    "disallow_incomplete_defs",
    "disallow_subclassing_any",
    "disallow_untyped_calls",
    "disallow_untyped_decorators",
    "disallow_untyped_defs",
    "extra_checks",
    "no_implicit_reexport",
    "strict_equality",
    "warn_return_any",
    "warn_unused_ignores",
)
_LINE_REFERENCE = re.compile(r"\bline \d+\b")
_WHITESPACE = re.compile(r"\s+")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
# Configuration notes (e.g. unused override sections for absent critical packages) are not findings.
_MYPY_NOTE_LINE = re.compile(r"^[^\s:][^:]*: note: ")

EXIT_PASSED = 0
EXIT_FINDINGS = 1
EXIT_SETUP_ERROR = 2

Fingerprint = tuple[str, str, str, str]


class QualitySetupError(Exception):
    """The gate cannot produce a trustworthy verdict; it must not pass."""


@dataclass(frozen=True)
class CommandResult:
    """Exit status and captured output of one static tool invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Path], CommandResult]


@dataclass(frozen=True)
class Finding:
    """One tool finding; the line is kept for display only."""

    tool: str
    path: str
    line: int
    code: str
    message: str

    @property
    def fingerprint(self) -> Fingerprint:
        """Line-free identity used to match the baseline."""
        return (self.tool, self.path, self.code, normalize_message(self.message))

    def format(self) -> str:
        return f"{self.tool}: {self.path}:{self.line}: [{self.code}] {self.message}"


@dataclass(frozen=True)
class Baseline:
    """Versioned legacy findings accepted as debt, counted per fingerprint."""

    tools: Mapping[str, str]
    counts: Mapping[Fingerprint, int]


def run_command(command: Sequence[str], repository_root: Path) -> CommandResult:
    """Run one static tool from the repository root and capture its output."""
    try:
        completed = subprocess.run(
            list(command),
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return CommandResult(EXIT_SETUP_ERROR, "", f"could not start {' '.join(command)}: {error}")
    return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def normalize_message(message: str) -> str:
    """Drop line references and collapse whitespace so fingerprints survive line moves."""
    return _WHITESPACE.sub(" ", _LINE_REFERENCE.sub("line N", message)).strip()


def relative_posix(path: str, repository_root: Path) -> str:
    """Return a repository-relative POSIX path for a tool-reported file."""
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(repository_root.resolve())
        except ValueError:
            return candidate.as_posix()
    return candidate.as_posix()


def critical_package_for(path: str) -> str | None:
    """Return the critical package that owns a repository-relative path, if any."""
    for package, root in CRITICAL_PACKAGE_ROOTS.items():
        if path.startswith(root.as_posix() + "/"):
            return package
    return None


def _python_files_under(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if path.is_file() and not any(part in SKIPPED_DIRECTORY_NAMES for part in path.parts)
    )


def collect_tool_files(repository_root: Path, required_roots: Sequence[str]) -> dict[str, list[str]]:
    """Map each required root to the repository-relative Python files it contributes.

    A missing root, a root without Python files, or a total of zero files raises
    :class:`QualitySetupError`: the gate never passes on nothing.
    """
    files_by_root: dict[str, list[str]] = {}
    for relative_root in required_roots:
        root = repository_root / relative_root
        if relative_root.endswith(".py"):
            if not root.is_file():
                raise QualitySetupError(f"required root {relative_root} is missing (expected a Python file)")
            files = [root]
        else:
            if not root.is_dir():
                raise QualitySetupError(f"required root {relative_root}/ is missing (expected a directory)")
            files = _python_files_under(root)
            if not files:
                raise QualitySetupError(f"required root {relative_root}/ contains 0 Python files")
        files_by_root[relative_root] = [path.relative_to(repository_root).as_posix() for path in files]
    if sum(len(files) for files in files_by_root.values()) == 0:
        raise QualitySetupError("the gate would inspect 0 Python files")
    return files_by_root


def _override_modules(override: Mapping[str, object]) -> list[object]:
    modules = override.get("module")
    return list(modules) if isinstance(modules, list) else [modules]


def _check_strict_mypy_override(repository_root: Path) -> None:
    pyproject = repository_root / "pyproject.toml"
    try:
        configuration = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise QualitySetupError(f"cannot read the mypy configuration in pyproject.toml: {error}") from error
    overrides = configuration.get("tool", {}).get("mypy", {}).get("overrides", [])
    if not isinstance(overrides, list):
        overrides = []
    for relative_root in CRITICAL_PACKAGE_ROOTS.values():
        dotted = ".".join(relative_root.parts)
        for module in (dotted, f"{dotted}.*"):
            strict = any(
                isinstance(override, dict)
                and module in _override_modules(override)
                and all(override.get(flag) is True for flag in STRICT_MYPY_FLAGS)
                for override in overrides
            )
            if not strict:
                raise QualitySetupError(
                    f"pyproject.toml lost the strict mypy override for {module}; "
                    f"required flags: {', '.join(STRICT_MYPY_FLAGS)}"
                )


def check_critical_package_contract(repository_root: Path) -> dict[str, int]:
    """Return Python file counts per critical package and enforce the strict contract."""
    counts: dict[str, int] = {}
    present = discover_critical_package_roots(repository_root)
    for package, relative_root in CRITICAL_PACKAGE_ROOTS.items():
        root = present.get(package)
        if root is None:
            counts[package] = 0
            continue
        if not (root / "__init__.py").is_file():
            raise QualitySetupError(
                f"critical package {relative_root.as_posix()}/ has no __init__.py; mypy would not apply "
                "the strict override to its modules"
            )
        counts[package] = len(_python_files_under(root))
    _check_strict_mypy_override(repository_root)
    return counts


def load_baseline(path: Path) -> Baseline | None:
    """Read and validate the versioned baseline; ``None`` when the file does not exist."""
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise QualitySetupError(f"baseline {path} is unreadable: {error}") from error
    if not isinstance(document, dict) or document.get("format_version") != BASELINE_FORMAT_VERSION:
        raise QualitySetupError(
            f"baseline {path} has an unsupported format_version (expected {BASELINE_FORMAT_VERSION})"
        )
    tools = document.get("tools")
    entries = document.get("findings")
    if not isinstance(tools, dict) or set(tools) != set(TOOLS) or not isinstance(entries, list):
        raise QualitySetupError(f"baseline {path} must record the versions of {', '.join(TOOLS)} and a findings list")
    counts: dict[Fingerprint, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise QualitySetupError(f"baseline {path} has a malformed finding entry: {entry!r}")
        tool = entry.get("tool")
        finding_path = entry.get("path")
        code = entry.get("code")
        message = entry.get("message")
        count = entry.get("count")
        if (
            not isinstance(tool, str)
            or tool not in TOOLS
            or not isinstance(finding_path, str)
            or not isinstance(code, str)
            or not isinstance(message, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
        ):
            raise QualitySetupError(f"baseline {path} has a malformed finding entry: {entry!r}")
        package = critical_package_for(finding_path)
        if package is not None:
            raise QualitySetupError(
                f"baseline {path} covers {finding_path} in critical package {package}; "
                "critical packages may never be baselined"
            )
        fingerprint = (tool, finding_path, code, normalize_message(message))
        if fingerprint in counts:
            raise QualitySetupError(f"baseline {path} lists a fingerprint twice: {fingerprint!r}")
        counts[fingerprint] = count
    return Baseline({str(name): str(version) for name, version in tools.items()}, counts)


def render_baseline(tool_versions: Mapping[str, str], findings: Iterable[Finding]) -> str:
    """Return the baseline document for the given findings, sorted and line-free."""
    counts = Counter(finding.fingerprint for finding in findings)
    entries = [
        {"tool": tool, "path": path, "code": code, "message": message, "count": count}
        for (tool, path, code, message), count in sorted(counts.items())
    ]
    document = {
        "format_version": BASELINE_FORMAT_VERSION,
        "description": (
            "Legacy ruff/mypy findings accepted as debt. Written only by "
            "'python scripts/run_quality.py --write-baseline'; fingerprints carry no line number. "
            "Critical packages (radar_v08/domain, workflow, adapters, execution) may never appear here."
        ),
        "tools": {tool: tool_versions[tool] for tool in TOOLS},
        "findings": entries,
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def tool_version(tool: str, repository_root: Path, command_runner: CommandRunner) -> str:
    """Return the installed tool version, or raise when the tool cannot run."""
    result = command_runner((sys.executable, "-m", tool, "--version"), repository_root)
    words = result.stdout.split()
    if result.returncode or len(words) < 2 or words[0] != tool:
        raise QualitySetupError(
            f"{tool} is unavailable or broken (exit {result.returncode}); install requirements-dev.txt. "
            f"{result.stderr.strip()}".strip()
        )
    return words[1]


def _check_status_matches(tool: str, returncode: int, finding_count: int) -> None:
    if (returncode == 1) != (finding_count > 0):
        raise QualitySetupError(
            f"{tool} exited {returncode} but {finding_count} findings were parsed; refusing an unverifiable verdict"
        )


def run_ruff(files: Sequence[str], repository_root: Path, command_runner: CommandRunner) -> list[Finding]:
    """Run Ruff over the explicit file list and parse its JSON findings."""
    command = (sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "json", *files)
    result = command_runner(command, repository_root)
    if result.returncode not in (0, 1):
        raise QualitySetupError(f"ruff failed with exit {result.returncode}: {result.stderr.strip()}")
    try:
        entries = json.loads(result.stdout or "[]")
    except ValueError as error:
        raise QualitySetupError(f"ruff produced unparseable JSON output: {error}") from error
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise QualitySetupError("ruff JSON output is not a list of findings")
    findings = [
        Finding(
            "ruff",
            relative_posix(str(entry.get("filename") or "<unknown>"), repository_root),
            int((entry.get("location") or {}).get("row") or 0),
            str(entry.get("code") or "syntax-error"),
            str(entry.get("message") or ""),
        )
        for entry in entries
    ]
    _check_status_matches("ruff", result.returncode, len(findings))
    return findings


def run_mypy(files: Sequence[str], repository_root: Path, command_runner: CommandRunner) -> list[Finding]:
    """Run mypy over the explicit file list with a disposable cache and parse its JSON findings."""
    with tempfile.TemporaryDirectory(prefix="crypto-radar-mypy-") as cache_directory:
        command = (sys.executable, "-m", "mypy", "--cache-dir", cache_directory, "--output", "json", *files)
        result = command_runner(command, repository_root)
    if result.returncode not in (0, 1):
        raise QualitySetupError(f"mypy failed with exit {result.returncode}: {result.stderr.strip()}")
    findings: list[Finding] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        plain = _ANSI_ESCAPE.sub("", line)
        if _MYPY_NOTE_LINE.match(plain):
            print(f"mypy {plain}")
            continue
        try:
            entry = json.loads(line)
        except ValueError as error:
            raise QualitySetupError(f"mypy produced a non-JSON output line: {plain!r}") from error
        if not isinstance(entry, dict):
            raise QualitySetupError(f"mypy produced an unexpected JSON value: {line!r}")
        if entry.get("severity") != "error":
            continue
        findings.append(
            Finding(
                "mypy",
                relative_posix(str(entry.get("file") or "<unknown>"), repository_root),
                int(entry.get("line") or 0),
                str(entry.get("code") or "error"),
                str(entry.get("message") or ""),
            )
        )
    _check_status_matches("mypy", result.returncode, len(findings))
    return findings


def compare_with_baseline(
    findings: Sequence[Finding], baseline: Baseline | None
) -> tuple[list[Finding], int, list[tuple[Fingerprint, int]]]:
    """Return (new findings, covered count, baseline entries not observed with their unused count)."""
    allowed = dict(baseline.counts) if baseline else {}
    new: list[Finding] = []
    covered = 0
    for finding in sorted(findings, key=lambda item: (item.tool, item.path, item.line, item.code)):
        fingerprint = finding.fingerprint
        if critical_package_for(finding.path) is None and allowed.get(fingerprint, 0) > 0:
            allowed[fingerprint] -= 1
            covered += 1
        else:
            new.append(finding)
    stale = sorted((fingerprint, count) for fingerprint, count in allowed.items() if count > 0)
    return new, covered, stale


def _inspect(
    repository_root: Path, command_runner: CommandRunner
) -> tuple[dict[str, str], dict[str, list[Finding]]]:
    """Validate roots and contracts, run both tools, and print what was inspected."""
    files_by_tool = {tool: collect_tool_files(repository_root, roots) for tool, roots in REQUIRED_TOOL_ROOTS.items()}
    critical_counts = check_critical_package_contract(repository_root)
    for package, count in critical_counts.items():
        relative_root = CRITICAL_PACKAGE_ROOTS[package].as_posix()
        if count:
            print(
                f"Critical package {relative_root}: {count} Python files "
                "(strict mypy override, import boundary, never baselined)."
            )
        else:
            print(f"Critical package {relative_root}: absent, 0 Python files inspected.")

    runners = {"ruff": run_ruff, "mypy": run_mypy}
    versions: dict[str, str] = {}
    findings: dict[str, list[Finding]] = {}
    for tool in TOOLS:
        versions[tool] = tool_version(tool, repository_root, command_runner)
        files = [path for root_files in files_by_tool[tool].values() for path in root_files]
        breakdown = ", ".join(f"{root}: {len(root_files)}" for root, root_files in files_by_tool[tool].items())
        print(f"{tool} {versions[tool]}: inspecting {len(files)} Python files ({breakdown}).")
        findings[tool] = runners[tool](files, repository_root, command_runner)
    return versions, findings


def run_quality_checks(
    repository_root: Path = REPOSITORY_ROOT,
    command_runner: CommandRunner = run_command,
    baseline_path: Path | None = None,
) -> int:
    """Run Ruff, mypy, the baseline comparison, and the import-boundary check."""
    baseline_file = baseline_path or repository_root / BASELINE_RELATIVE_PATH
    try:
        baseline = load_baseline(baseline_file)
        versions, findings = _inspect(repository_root, command_runner)
        if baseline is None:
            print(f"No baseline at {baseline_file}; every finding counts as new.")
        else:
            mismatched = [tool for tool in TOOLS if baseline.tools.get(tool) != versions[tool]]
            if mismatched:
                raise QualitySetupError(
                    "baseline was written with "
                    + ", ".join(f"{tool} {baseline.tools.get(tool)}" for tool in mismatched)
                    + " but the gate is running "
                    + ", ".join(f"{tool} {versions[tool]}" for tool in mismatched)
                    + "; use the pinned requirements-dev.txt tools"
                )
    except QualitySetupError as error:
        print(f"Quality setup error: {error}", file=sys.stderr)
        return EXIT_SETUP_ERROR

    status = EXIT_PASSED
    for tool in TOOLS:
        new, covered, stale = compare_with_baseline(findings[tool], baseline)
        unused = sum(count for fingerprint, count in stale if fingerprint[0] == tool)
        print(
            f"{tool}: {len(findings[tool])} findings, {covered} covered by baseline, "
            f"{len(new)} new, {unused} baseline occurrences no longer observed."
        )
        for finding in new:
            package = critical_package_for(finding.path)
            suffix = f" (critical package {package}: never baselined)" if package else ""
            print(f"NEW {finding.format()}{suffix}", file=sys.stderr)
        if new:
            status = EXIT_FINDINGS

    violations = check_import_boundaries(repository_root)
    for violation in violations:
        print(violation.format(repository_root), file=sys.stderr)
    if violations:
        status = EXIT_FINDINGS
    else:
        print("Import-boundary check passed.")
    print("Quality gate passed." if status == EXIT_PASSED else "Quality gate FAILED.")
    return status


def write_baseline(
    repository_root: Path = REPOSITORY_ROOT,
    command_runner: CommandRunner = run_command,
    baseline_path: Path | None = None,
) -> int:
    """Regenerate the baseline from the current findings; refuses critical-package findings."""
    baseline_file = baseline_path or repository_root / BASELINE_RELATIVE_PATH
    try:
        versions, findings = _inspect(repository_root, command_runner)
    except QualitySetupError as error:
        print(f"Quality setup error: {error}", file=sys.stderr)
        return EXIT_SETUP_ERROR
    everything = [finding for tool in TOOLS for finding in findings[tool]]
    critical = [finding for finding in everything if critical_package_for(finding.path)]
    if critical:
        for finding in critical:
            print(f"CRITICAL {finding.format()}", file=sys.stderr)
        print("Refusing to write a baseline: critical packages may never be baselined.", file=sys.stderr)
        return EXIT_FINDINGS
    baseline_file.write_text(render_baseline(versions, everything), encoding="utf-8", newline="\n")
    summary = ", ".join(f"{tool}: {len(findings[tool])} findings" for tool in TOOLS)
    print(f"Wrote {baseline_file} ({summary}).")
    return EXIT_PASSED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-boundaries", action="store_true", help="run only the import-boundary check")
    mode.add_argument(
        "--write-baseline",
        action="store_true",
        help=f"regenerate {BASELINE_RELATIVE_PATH.as_posix()} from the current findings (never runs by default)",
    )
    arguments = parser.parse_args(argv)
    if arguments.check_boundaries:
        violations = check_import_boundaries()
        for violation in violations:
            print(violation.format(REPOSITORY_ROOT), file=sys.stderr)
        return 1 if violations else 0
    if arguments.write_baseline:
        return write_baseline()
    return run_quality_checks()


if __name__ == "__main__":
    raise SystemExit(main())
