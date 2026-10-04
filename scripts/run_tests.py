"""Run the test suite with disposable, child-only radar configuration.

By default the suite runs in parallel: a child process discovers the same test
set as ``python -m unittest discover -s tests``, then independent work units
(test modules, or test classes of the modules listed in ``SPLIT_MODULES``) run in
worker subprocesses. Every child gets the sanitized test environment and its own
disposable state directory. Unit output is printed in discovery order, and the
run fails closed unless every discovered test id ran exactly once and passed or
was skipped. ``--serial`` keeps the original single ``unittest discover`` run and
``--list`` prints the discovered test ids without running them.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATH = Path(__file__).resolve()
SENSITIVE_ENVIRONMENT_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CRYPTO_RADAR_NTFY_TOPIC",
    "KRAKEN_API_KEY",
    "KRAKEN_API_SECRET",
    "KRAKEN_SECRET",
}
# Modules whose test classes run as separate work units instead of one module unit.
# Only modules whose classes share no expensive module-level fixture are split: a split
# class repeats its module's setUpModule and caches in its own process.
SPLIT_MODULES: frozenset[str] = frozenset({
    "test_trend_paper",
    "test_trend_paper_e2e",
})
# Units submitted to the pool before the others, slowest measured first, so the long
# units do not start last. A name that no longer matches a unit only loses its priority.
SCHEDULE_FIRST: tuple[str, ...] = (
    "test_l1_equivalence",
    "test_oc1_corpus_builder",
    "test_trend_paper_e2e.Scenario",
    "test_t051_runner",
    "test_qwen_profile_runtime",
)

# Loads this file by path in a fresh interpreter and runs one worker command.
# ``sys.path[0]`` becomes the absolute working directory, as with ``-m unittest``.
_WORKER_BOOTSTRAP = """\
import importlib.util, os, sys
sys.path[0] = os.getcwd()
path = sys.argv.pop(1)
spec = importlib.util.spec_from_file_location("_radar_test_worker", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
raise SystemExit(module.worker_main(sys.argv[1:]))
"""


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


# ---------------------------------------------------------------------------
# Parent side: discovery, scheduling, aggregation.


@dataclass(frozen=True)
class WorkUnit:
    name: str
    test_ids: tuple[str, ...]


@dataclass(frozen=True)
class Discovery:
    top_level_dir: str
    units: tuple[WorkUnit, ...]
    errors: tuple[str, ...]

    @property
    def test_ids(self) -> list[str]:
        return [test_id for unit in self.units for test_id in unit.test_ids]


@dataclass(frozen=True)
class UnitResult:
    unit: WorkUnit
    returncode: int
    output: str
    seconds: float
    report: dict[str, Any] | None
    problems: tuple[str, ...]


def _child_command(*arguments: str) -> list[str]:
    return [sys.executable, "-c", _WORKER_BOOTSTRAP, os.fspath(RUNNER_PATH), *arguments]


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _read_report(path: Path) -> dict[str, Any] | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def discover_units(
    run_dir: Path,
    *,
    start_dir: str = "tests",
    top_level_dir: str | None = None,
    cwd: str | os.PathLike[str] = REPOSITORY_ROOT,
    split_modules: Iterable[str] = SPLIT_MODULES,
    base_environment: dict[str, str] | None = None,
) -> Discovery:
    """Discover tests in a sanitized child process and group them into work units."""
    state_dir = run_dir / "discovery-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "discovery.json"
    command = _child_command(
        "discover",
        "--start-dir", start_dir,
        "--top-level-dir", top_level_dir or "",
        "--split", ",".join(sorted(split_modules)),
        "--output", os.fspath(report_path),
    )
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=build_test_environment(state_dir, base_environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    report = _read_report(report_path)
    if report is None:
        output = _decode(completed.stdout).strip()
        return Discovery("", (), (f"discovery child exited with code {completed.returncode} without a report\n{output}",))
    errors = [str(error) for error in report.get("errors", [])]
    if completed.returncode != 0 and not errors:
        errors.append(f"discovery child exited with code {completed.returncode}")
    units = tuple(WorkUnit(str(unit["name"]), tuple(str(test_id) for test_id in unit["ids"])) for unit in report.get("units", []))
    return Discovery(str(report.get("top_level_dir", "")), units, tuple(errors))


def compare_test_ids(discovered: Sequence[str], ran: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return (missing, extra) ids; a repeated id counts once per extra occurrence."""
    expected = Counter(discovered)
    actual = Counter(ran)
    missing = sorted((expected - actual).elements())
    extra = sorted((actual - expected).elements())
    return missing, extra


def run_unit(
    unit: WorkUnit,
    index: int,
    run_dir: Path,
    *,
    top_level_dir: str,
    cwd: str | os.PathLike[str],
    base_environment: dict[str, str] | None,
) -> UnitResult:
    """Run one work unit in a worker subprocess with its own state directory."""
    unit_dir = run_dir / f"unit-{index:04d}"
    state_dir = unit_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    report_path = unit_dir / "report.json"
    command = _child_command("run", "--unit", unit.name, "--top-level-dir", top_level_dir, "--output", os.fspath(report_path))
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=build_test_environment(state_dir, base_environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    seconds = time.perf_counter() - started
    report = _read_report(report_path)
    problems: list[str] = []
    if report is None:
        problems.append(f"worker exited with code {completed.returncode} without a parsable result")
    else:
        if report.get("unit") != unit.name:
            problems.append(f"worker reported unit {report.get('unit')!r}")
        expected_code = 0 if report.get("successful") is True else 1
        if completed.returncode != expected_code:
            problems.append(f"worker exited with code {completed.returncode}, expected {expected_code}")
        missing, extra = compare_test_ids(unit.test_ids, [str(test_id) for test_id in report.get("ran", [])])
        if missing or extra:
            problems.append(f"ran ids differ from discovery ({len(missing)} missing, {len(extra)} extra or duplicate)")
    return UnitResult(unit, completed.returncode, _decode(completed.stdout), seconds, report, tuple(problems))


def _ordered_units(units: Sequence[WorkUnit], schedule_first: Sequence[str]) -> list[tuple[int, WorkUnit]]:
    rank = {name: position for position, name in enumerate(schedule_first)}
    indexed = list(enumerate(units))
    return sorted(indexed, key=lambda item: (rank.get(item[1].name, len(rank)), item[0]))


def _report_items(results: Iterable[UnitResult], key: str) -> Iterator[Any]:
    for result in results:
        if result.report is not None:
            yield from result.report.get(key, [])


def _write_summary(
    out: TextIO,
    discovery: Discovery,
    results: Sequence[UnitResult],
    jobs: int,
    wall_seconds: float,
) -> bool:
    ran = [str(test_id) for test_id in _report_items(results, "ran")]
    failures = [str(item[0]) for item in _report_items(results, "failures")]
    errors = [str(item[0]) for item in _report_items(results, "errors")]
    skipped = [(str(item[0]), str(item[1])) for item in _report_items(results, "skipped")]
    expected_failures = [str(test_id) for test_id in _report_items(results, "expected_failures")]
    unexpected_successes = [str(test_id) for test_id in _report_items(results, "unexpected_successes")]
    missing, extra = compare_test_ids(discovery.test_ids, ran)

    out.write("\n" + "=" * 70 + "\n")
    out.write("Parallel test summary\n")
    out.write(f"Units: {len(discovery.units)} (jobs {jobs}); discovered tests: {len(discovery.test_ids)}\n")
    out.write(
        f"Ran {len(ran)} tests: failures {len(failures)}, errors {len(errors)}, skipped {len(skipped)}, "
        f"expected failures {len(expected_failures)}, unexpected successes {len(unexpected_successes)}\n"
    )
    for title, items in (
        ("Skipped", [f"{test_id}: {reason}" for test_id, reason in skipped]),
        ("Failures", failures),
        ("Errors", errors),
        ("Expected failures", expected_failures),
        ("Unexpected successes", unexpected_successes),
        ("Missing test ids (discovered but not run)", missing),
        ("Unexpected or duplicate test ids (run but not discovered once)", extra),
    ):
        if items:
            out.write(f"{title}:\n")
            out.writelines(f"  {item}\n" for item in items)
    problems = [f"{result.unit.name}: {problem}" for result in results for problem in result.problems]
    if problems:
        out.write("Worker problems:\n")
        out.writelines(f"  {problem}\n" for problem in problems)

    out.write("Per-unit wall time (slowest first):\n")
    for result in sorted(results, key=lambda item: (-item.seconds, item.unit.name)):
        out.write(f"  {result.seconds:9.2f}s  {len(result.unit.test_ids):5d} tests  {result.unit.name}\n")
    out.write(f"Total wall time: {wall_seconds:.2f}s\n")

    passed = (
        len(results) == len(discovery.units)
        and not problems
        and not failures
        and not errors
        and not unexpected_successes
        and not missing
        and not extra
    )
    out.write(("RESULT: OK" if passed else "RESULT: FAILED") + "\n")
    return passed


def run_parallel(
    run_dir: Path,
    *,
    jobs: int,
    out: TextIO,
    start_dir: str = "tests",
    top_level_dir: str | None = None,
    cwd: str | os.PathLike[str] = REPOSITORY_ROOT,
    split_modules: Iterable[str] = SPLIT_MODULES,
    schedule_first: Sequence[str] = SCHEDULE_FIRST,
    base_environment: dict[str, str] | None = None,
) -> int:
    """Run the discovered work units in parallel and return 0 only for a complete, passing run."""
    started = time.perf_counter()
    discovery = discover_units(
        run_dir,
        start_dir=start_dir,
        top_level_dir=top_level_dir,
        cwd=cwd,
        split_modules=split_modules,
        base_environment=base_environment,
    )
    if discovery.errors:
        out.write("Test discovery failed:\n")
        out.writelines(f"  {error}\n" for error in discovery.errors)
        out.write("RESULT: FAILED\n")
        return 1
    if not discovery.units:
        out.write("Test discovery found no tests.\nRESULT: FAILED\n")
        return 1

    total = len(discovery.units)
    results: dict[int, UnitResult] = {}
    next_to_print = 0

    def flush_ready() -> None:
        nonlocal next_to_print
        while next_to_print in results:
            result = results[next_to_print]
            out.write(f"----- unit {next_to_print + 1}/{total}: {result.unit.name} ({len(result.unit.test_ids)} tests) -----\n")
            out.write(result.output)
            if result.output and not result.output.endswith("\n"):
                out.write("\n")
            out.flush()
            next_to_print += 1

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures: dict[Future[UnitResult], int] = {
            pool.submit(
                run_unit,
                unit,
                index,
                run_dir,
                top_level_dir=discovery.top_level_dir,
                cwd=cwd,
                base_environment=base_environment,
            ): index
            for index, unit in _ordered_units(discovery.units, schedule_first)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as error:  # noqa: BLE001 - a launcher failure must still be reported
                results[index] = UnitResult(discovery.units[index], -1, "", 0.0, None, (f"worker could not run: {error!r}",))
            flush_ready()

    ordered = [results[index] for index in range(total)]
    passed = _write_summary(out, discovery, ordered, jobs, time.perf_counter() - started)
    return 0 if passed else 1


def list_tests(
    run_dir: Path,
    *,
    out: TextIO,
    start_dir: str = "tests",
    top_level_dir: str | None = None,
    cwd: str | os.PathLike[str] = REPOSITORY_ROOT,
    base_environment: dict[str, str] | None = None,
) -> int:
    """Print the discovered test ids, one per line, then a count line."""
    discovery = discover_units(
        run_dir, start_dir=start_dir, top_level_dir=top_level_dir, cwd=cwd, base_environment=base_environment
    )
    if discovery.errors:
        out.write("Test discovery failed:\n")
        out.writelines(f"  {error}\n" for error in discovery.errors)
        return 1
    ids = discovery.test_ids
    modules = {test_id.rsplit(".", 2)[0] for test_id in ids}
    out.writelines(f"{test_id}\n" for test_id in ids)
    out.write(f"{len(ids)} tests discovered in {len(modules)} modules\n")
    return 0


# ---------------------------------------------------------------------------
# Child side: runs inside the discovery and worker subprocesses.


def _flatten(suite: unittest.TestSuite | unittest.TestCase) -> Iterator[unittest.TestCase]:
    if isinstance(suite, unittest.TestSuite):
        for child in suite:
            yield from _flatten(child)
    else:
        yield suite


def _is_failed_test(test: unittest.TestCase) -> bool:
    return type(test).__name__ in {"_FailedTest", "ModuleImportFailure"}


class _ModuleRecordingLoader(unittest.TestLoader):
    """A loader that remembers which module produced each module suite."""

    def __init__(self) -> None:
        super().__init__()
        self.module_of_suite: dict[int, Any] = {}

    def loadTestsFromModule(self, module: Any, *args: Any, pattern: str | None = None) -> unittest.TestSuite:
        suite = super().loadTestsFromModule(module, *args, pattern=pattern)
        self.module_of_suite[id(suite)] = module
        return suite


def _class_units(module: Any, suite: unittest.TestSuite, errors: list[str]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for class_suite in suite:
        ids = [test.id() for test in _flatten(class_suite)]
        if not ids:
            continue
        classes = {type(test) for test in _flatten(class_suite)}
        names = [name for name in dir(module) if any(getattr(module, name, None) is cls for cls in classes)]
        if len(classes) != 1 or len(names) != 1:
            errors.append(f"cannot split {module.__name__}: a class suite is not bound to exactly one module name")
            continue
        units.append({"name": f"{module.__name__}.{names[0]}", "ids": ids})
    return units


def _discover_command(start_dir: str, top_level_dir: str, split: set[str], output: Path) -> int:
    loader = _ModuleRecordingLoader()
    errors: list[str] = []
    units: list[dict[str, Any]] = []
    suite = loader.discover(start_dir, pattern="test*.py", top_level_dir=top_level_dir or None)
    errors.extend(str(error) for error in loader.errors)
    for child in suite:
        tests = list(_flatten(child))
        failed = [test.id() for test in tests if _is_failed_test(test)]
        if failed:
            errors.extend(f"discovery error: {test_id}" for test_id in failed)
            continue
        module = loader.module_of_suite.get(id(child))
        if module is None:
            if tests:
                errors.append(f"discovered tests outside a module suite: {tests[0].id()}")
            continue
        if not tests:
            continue
        if module.__name__ in split and isinstance(child, unittest.TestSuite):
            units.extend(_class_units(module, child, errors))
        else:
            units.append({"name": module.__name__, "ids": [test.id() for test in tests]})
    resolved_top = os.path.abspath(top_level_dir or start_dir)
    output.write_text(json.dumps({"top_level_dir": resolved_top, "units": units, "errors": errors}), encoding="utf-8")
    return 1 if errors else 0


class _RecordingResult(unittest.TextTestResult):
    """A verbose text result that also records every outcome by test id."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ran_ids: list[str] = []
        self.skip_records: list[list[str]] = []

    def startTest(self, test: unittest.TestCase) -> None:
        self.ran_ids.append(test.id())
        super().startTest(test)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        self.skip_records.append([test.id(), reason])
        super().addSkip(test, reason)


def _run_command(unit: str, top_level_dir: str, output: Path) -> int:
    if top_level_dir and top_level_dir not in sys.path:
        sys.path.insert(0, top_level_dir)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(unit)
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=_RecordingResult)
    result = runner.run(suite)
    assert isinstance(result, _RecordingResult)
    successful = result.wasSuccessful() and not loader.errors
    report = {
        "unit": unit,
        "ran": result.ran_ids,
        "tests_run": result.testsRun,
        "failures": [[test.id(), trace] for test, trace in result.failures],
        "errors": [[test.id(), trace] for test, trace in result.errors] + [[unit, str(error)] for error in loader.errors],
        "skipped": result.skip_records,
        "expected_failures": [test.id() for test, _ in result.expectedFailures],
        "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
        "successful": successful,
    }
    sys.stderr.flush()
    sys.stdout.flush()
    output.write_text(json.dumps(report), encoding="utf-8")
    return 0 if successful else 1


def worker_main(argv: Sequence[str]) -> int:
    """Entry point of the discovery and worker child processes."""
    parser = argparse.ArgumentParser(prog="run_tests worker")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover")
    discover.add_argument("--start-dir", required=True)
    discover.add_argument("--top-level-dir", default="")
    discover.add_argument("--split", default="")
    discover.add_argument("--output", required=True, type=Path)
    run = commands.add_parser("run")
    run.add_argument("--unit", required=True)
    run.add_argument("--top-level-dir", default="")
    run.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(list(argv))
    if args.command == "discover":
        split = {name for name in args.split.split(",") if name}
        return _discover_command(args.start_dir, args.top_level_dir, split, args.output)
    return _run_command(args.unit, args.top_level_dir, args.output)


# ---------------------------------------------------------------------------
# Command line.


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the crypto-radar test suite with disposable configuration.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serial", action="store_true", help="run one serial 'unittest discover' process")
    mode.add_argument("--list", action="store_true", help="print the discovered test ids without running them")
    parser.add_argument(
        "--jobs",
        type=_positive_int,
        default=os.cpu_count() or 1,
        help="parallel worker processes (default: os.cpu_count())",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args([] if argv is None else list(argv))
    with tempfile.TemporaryDirectory(prefix="crypto-radar-tests-", ignore_cleanup_errors=True) as state_dir:
        if args.list:
            return list_tests(Path(state_dir), out=sys.stdout)
        environment = build_test_environment(state_dir)
        if shutil.which("node", path=environment.get("PATH")) is None:
            print("Test setup error: Node.js is required for the embedded UI test suites.", file=sys.stderr)
            return 2
        if args.serial:
            return run_unittest_suite(state_dir, environment)
        return run_parallel(Path(state_dir), jobs=args.jobs, out=sys.stdout)


if __name__ == "__main__":
    # Child output is UTF-8; never let a narrow console encoding abort the summary.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")
    raise SystemExit(main(sys.argv[1:]))
