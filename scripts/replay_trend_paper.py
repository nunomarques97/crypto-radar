"""Deterministic offline replay of the trend paper end-to-end scenario.

Test tooling for the research-only trend paper books: no network, no order, no account, no
credential, no private endpoint. It runs the radar starts of ``tests/trend_paper_fakes.E2E_STARTS``
over the vendored synthetic candles of ``tests/fixtures/trend/e2e/`` through the real radar start
path (``radar_v08.cli._start_trend_paper_catch_up``, joined on its thread) with a fake clock, the
real Binance and Kraken adapters over fake HTTP sessions and a toast recorder, exactly as
``tests/test_trend_paper_e2e.py`` does. ``config.STATE_DIR`` points at the replay's own state dir for
the whole run, so the real state dir and ``radar_state.sqlite`` are never read or written, and every
socket connection is refused.

Usage:

    python -B scripts/replay_trend_paper.py                 replay into a fresh temporary state dir
    python -B scripts/replay_trend_paper.py --out DIR       replay into DIR (new or empty; kept)
    python -B scripts/replay_trend_paper.py --check         also compare every produced file with its golden
    python -B scripts/replay_trend_paper.py --write-golden  regenerate tests/fixtures/trend/e2e/ (candles,
                                                            goldens and MANIFEST.json) from the scenario

Each start prints one line: its fake-clock moment, the Binance and Kraken outcome, the days booked
and skipped, and the toasts it showed (their titles follow, indented).

Exit codes: 0 done (``--check``: every file matches); 1 ``--check`` mismatch, the first differing
file is named; 2 usage error (``--out`` not new or empty, or the radar state dir).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import socket
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TextIO
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPOSITORY_ROOT / "tests"
for _path in (REPOSITORY_ROOT, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from radar_v08 import config  # noqa: E402

F = importlib.import_module("trend_paper_fakes")

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2


def _refuse_network(*args: object, **kwargs: object) -> None:
    raise OSError("network access refused: the trend paper replay is offline")


@contextlib.contextmanager
def offline() -> Iterator[None]:
    """Refuse every socket connection and name lookup for the duration."""
    with contextlib.ExitStack() as stack:
        for name in ("connect", "connect_ex"):
            stack.enter_context(mock.patch.object(socket.socket, name, _refuse_network))
        stack.enter_context(mock.patch.object(socket, "create_connection", _refuse_network))
        stack.enter_context(mock.patch.object(socket, "getaddrinfo", _refuse_network))
        yield


def _out_problem(out: Path) -> str | None:
    resolved = out.resolve()
    if resolved == Path(config.STATE_DIR).resolve():
        return f"{out} is the radar state dir; use a new or empty directory"
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        return f"{out} is not a new or empty directory"
    return None


def replay(state_dir: Path, candles_dir: Path, stdout: TextIO) -> dict[str, bytes]:
    """Run the scenario into ``state_dir``, print the per-start summary and return the produced
    files by golden name."""
    with offline():
        results = F.run_e2e_scenario(state_dir, F.load_e2e_candles(candles_dir))
        produced: dict[str, bytes] = F.e2e_outputs(state_dir, results)
    for result in results:
        print(result.line(), file=stdout)
        for title, _ in result.toasts:
            print(f"      toast: {title}", file=stdout)
    booked = sum(len(r.booked) for r in results)
    kraken = sum(len(r.kraken_booked) for r in results)
    toasts = sum(len(r.toasts) for r in results)
    print(
        f"{len(results)} starts: {booked} Binance days and {kraken} Kraken days booked, {toasts} toasts.",
        file=stdout,
    )
    return produced


def check(produced: dict[str, bytes], stdout: TextIO) -> int:
    """Compare the vendored candles, the produced files and MANIFEST.json with the fixture directory."""
    directory: Path = F.E2E_DIR
    candles: dict[str, bytes] = F.e2e_candle_files()
    first = F.e2e_first_difference(directory, candles) or F.e2e_first_difference(directory, produced)
    if first is None:
        on_disk = {name: (directory / name).read_bytes() for name in (*candles, *produced)}
        if F.e2e_first_difference(directory, {F.E2E_MANIFEST: F.e2e_manifest(on_disk)}) is not None:
            first = F.E2E_MANIFEST
    if first is not None:
        print(f"MISMATCH: {first} differs from tests/fixtures/trend/e2e/{first}", file=stdout)
        return EXIT_MISMATCH
    print(f"Match: every candle file, golden and {F.E2E_MANIFEST} is identical.", file=stdout)
    return EXIT_OK


def write_golden(state_dir: Path, stdout: TextIO) -> int:
    """Regenerate the candles, then replay over them and write the goldens and MANIFEST.json, only
    under tests/fixtures/trend/e2e/."""
    directory: Path = F.E2E_DIR
    directory.mkdir(parents=True, exist_ok=True)
    candles: dict[str, bytes] = F.e2e_candle_files()
    for name, data in candles.items():
        (directory / name).write_bytes(data)
    produced = replay(state_dir, directory, stdout)
    for name, data in produced.items():
        (directory / name).write_bytes(data)
    (directory / F.E2E_MANIFEST).write_bytes(F.e2e_manifest({**candles, **produced}))
    print(f"Wrote {len(candles) + len(produced) + 1} files under tests/fixtures/trend/e2e/.", file=stdout)
    return EXIT_OK


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    out_stream = stdout if stdout is not None else sys.stdout
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, help="state dir to replay into (new or empty); default a temporary one")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare the produced files with the goldens")
    mode.add_argument("--write-golden", action="store_true", help="regenerate tests/fixtures/trend/e2e/")
    args = parser.parse_args(argv)
    if args.out is not None:
        problem = _out_problem(args.out)
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return EXIT_USAGE
    with contextlib.ExitStack() as stack:
        if args.out is not None:
            state_dir = args.out.resolve()
            state_dir.mkdir(parents=True, exist_ok=True)
        else:
            state_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="trend-paper-replay-")))
        print(f"Trend paper replay (offline, synthetic test data) into {state_dir}", file=out_stream)
        if args.write_golden:
            return write_golden(state_dir, out_stream)
        produced = replay(state_dir, F.E2E_DIR, out_stream)
        return check(produced, out_stream) if args.check else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
