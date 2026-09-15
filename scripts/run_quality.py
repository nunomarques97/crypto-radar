"""Run static checks for future critical modules without touching runtime state."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import tempfile


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


def run_command(command: Sequence[str], repository_root: Path) -> int:
    """Run one static tool from the discovered repository root."""
    try:
        return subprocess.run(command, cwd=repository_root, check=False).returncode
    except OSError as error:
        print(f"Quality setup error: could not start {' '.join(command)}: {error}", file=sys.stderr)
        return 2


CommandRunner = Callable[[Sequence[str], Path], int]


def run_quality_checks(
    repository_root: Path = REPOSITORY_ROOT,
    command_runner: CommandRunner = run_command,
) -> int:
    """Run Ruff, mypy, and import-boundary checks without creating packages."""
    roots = list(discover_critical_package_roots(repository_root).values())
    for tool, check_arguments in (("ruff", ("check",)), ("mypy", ())):
        status = command_runner((sys.executable, "-m", tool, "--version"), repository_root)
        if status:
            print(
                f"Quality setup error: {tool} is unavailable or broken; install requirements-dev.txt.",
                file=sys.stderr,
            )
            return status
        if roots:
            if tool == "ruff":
                command = (sys.executable, "-m", tool, *check_arguments, "--no-cache", *map(str, roots))
                status = command_runner(command, repository_root)
            else:
                with tempfile.TemporaryDirectory(prefix="crypto-radar-mypy-") as cache_directory:
                    command = (
                        sys.executable,
                        "-m",
                        tool,
                        "--cache-dir",
                        cache_directory,
                        *map(str, roots),
                    )
                    status = command_runner(command, repository_root)
            if status:
                return status
        else:
            print(f"No future critical package roots found; {tool} check has no files to inspect.")

    violations = check_import_boundaries(repository_root)
    if violations:
        for violation in violations:
            print(violation.format(repository_root), file=sys.stderr)
        return 1
    print("Import-boundary check passed.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-boundaries", action="store_true", help="run only the import-boundary check")
    arguments = parser.parse_args(argv)
    if arguments.check_boundaries:
        violations = check_import_boundaries()
        for violation in violations:
            print(violation.format(REPOSITORY_ROOT), file=sys.stderr)
        return 1 if violations else 0
    return run_quality_checks()


if __name__ == "__main__":
    raise SystemExit(main())
