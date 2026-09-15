"""Verify a selected dependency group without installing or launching Crypto Radar."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
GROUP_REQUIREMENTS = {
    "core": "requirements.txt",
    "ui": "requirements-ui.txt",
    "build": "requirements-build.txt",
    "dev": "requirements-dev.txt",
}
GROUP_IMPORTS = {
    "core": ("radar_v08.http_client", "radar_v08.heartbeat"),
    "ui": ("ui.app",),
    "build": ("PyInstaller",),
    "dev": ("mypy", "ruff"),
}
SENSITIVE_ENVIRONMENT_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CRYPTO_RADAR_NTFY_TOPIC",
    "KRAKEN_API_KEY",
    "KRAKEN_SECRET",
}


def read_pinned_requirements(path: Path) -> dict[str, str]:
    """Read exact pins and recursive ``-r`` references from a requirements file."""
    requirements: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            requirements.update(read_pinned_requirements(path.parent / line[3:].strip()))
            continue
        name, separator, version = line.partition("==")
        if not separator or not name or not version:
            raise ValueError(f"{path}: expected an exact pin, found {raw_line!r}")
        requirements[name.lower().replace("_", "-")] = version
    return requirements


def installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def sanitized_environment(state_dir: str) -> dict[str, str]:
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith("RADAR_"):
            environment.pop(name)
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    environment["RADAR_STATE_DIR"] = state_dir
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def check_tools(group: str, which: Callable[[str], str | None] = shutil.which) -> list[str]:
    errors: list[str] = []
    if sys.version_info[:2] != (3, 12):
        errors.append(f"Python 3.12 is required; found {platform.python_version()}")
    if platform.system() != "Windows":
        errors.append(f"Windows is required; found {platform.system()}")
    if group == "core" and which("node") is None:
        errors.append("Node.js is required for the embedded UI test suites")
    return errors


def check_requirements(requirements: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for name, expected in requirements.items():
        actual = installed_version(name)
        if actual is None:
            errors.append(f"Missing required package: {name}=={expected}")
        elif actual != expected:
            errors.append(f"Incompatible package: {name}=={actual}; expected {expected}")
    return errors


def check_imports(modules: Iterable[str], repository_root: Path) -> list[str]:
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="crypto-radar-environment-") as state_dir:
        for module in modules:
            result = subprocess.run(
                [sys.executable, "-c", f"import {module}"],
                cwd=repository_root,
                env=sanitized_environment(state_dir),
                capture_output=True,
                check=False,
                text=True,
                timeout=20,
            )
            if result.returncode:
                detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
                errors.append(f"Import failed: {module}: {' '.join(detail)}")
    return errors


def verify(group: str, repository_root: Path = REPOSITORY_ROOT) -> list[str]:
    if group not in GROUP_REQUIREMENTS:
        raise ValueError(f"Unknown group: {group}")
    requirements = read_pinned_requirements(repository_root / GROUP_REQUIREMENTS[group])
    return [
        *check_tools(group),
        *check_requirements(requirements),
        *check_imports(GROUP_IMPORTS[group], repository_root),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=tuple(GROUP_REQUIREMENTS), required=True)
    args = parser.parse_args(argv)
    print(f"Python: {platform.python_version()} ({sys.executable})")
    print(f"Platform: {platform.platform()}")
    print(f"Group: {args.group}")
    errors = verify(args.group)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Environment verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
