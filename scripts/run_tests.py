"""Run the test suite with disposable, child-only radar configuration."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SENSITIVE_ENVIRONMENT_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CRYPTO_RADAR_NTFY_TOPIC",
    "KRAKEN_API_KEY",
    "KRAKEN_SECRET",
}


def build_test_environment(state_dir: str | os.PathLike[str], base_environment: dict[str, str] | None = None) -> dict[str, str]:
    """Return a child environment without inherited radar state or credentials."""
    environment = dict(os.environ if base_environment is None else base_environment)
    for name in list(environment):
        if name.startswith("RADAR_"):
            environment.pop(name)
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    environment["RADAR_STATE_DIR"] = os.fspath(state_dir)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_unittest_suite(state_dir: str | os.PathLike[str], environment: dict[str, str]) -> int:
    """Stream unittest output and return its exact exit status."""
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=REPOSITORY_ROOT,
        env=environment,
    )
    return result.returncode


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="crypto-radar-tests-") as state_dir:
        environment = build_test_environment(state_dir)
        if shutil.which("node", path=environment.get("PATH")) is None:
            print("Test setup error: Node.js is required for the embedded UI test suites.", file=sys.stderr)
            return 2
        return run_unittest_suite(state_dir, environment)


if __name__ == "__main__":
    raise SystemExit(main())
