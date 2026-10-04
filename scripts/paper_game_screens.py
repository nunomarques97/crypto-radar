"""Capture the Game tab screenshots of the EX-1 exit rule with headless Microsoft Edge.

Builds the sample-data preview of ``scripts/paper_game_preview.py`` (temporary databases
written through the real paper store, read by the real reader) in a temporary directory,
serves it on a free 127.0.0.1 port and has headless Edge photograph:

* the office with the wall board of an open play (stop, target, countdown to the 24 h limit);
* the wallet and the current-play panel with its stop, target and time left;
* the history with the recorded close reasons next to older legacy closes;
* the legacy scenario, where a play opened before the policy shows no levels;
* the wallet's "Value now" and the current play (``paper-game-value-*``) with every open play
  priced, with nothing open, and with a USD-priced play whose last price is too old (its
  dependent totals unknown, the FX-excluded label), and that scenario's history with each
  close's assumed fee;
* the Pilot shadow panel (``pilot-shadow-*``) with an open position and no lock, the same
  position without a fresh price, with the daily-loss lock and the kill switch engaged and
  the entries refused since, and before the pilot has started, each in a window tall
  enough for the whole panel;
* the Trend paper panel (``trend-paper-*``) with 14 synthetic paper days booked in both
  currencies, the same with two of them skipped for a missing EUR price, before the first
  paper day, and with a refused (edited) ledger.

each at 1440 and 960 wide (the Game tab is desktop only). Every PNG is written to
``docs/design/screens/`` and checked: it exists, has the requested width and height, and
is not blank. Edge and the server are stopped on every exit path, errors included.

    python -B scripts/paper_game_screens.py
    python -B scripts/paper_game_screens.py --out-dir <folder>
    python -B scripts/paper_game_screens.py --only pilot-shadow   # only the shots named so
    python -B scripts/paper_game_screens.py --only trend-paper
"""

from __future__ import annotations

import argparse
import functools
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import paper_game_preview as preview  # noqa: E402

EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
OUT_DIR = REPOSITORY_ROOT / "docs" / "design" / "screens"
WIDTHS = (1440, 960)
HEIGHT = 900
EDGE_TIMEOUT_SECONDS = 90
# Virtual time Edge lets pass before the shot: the first polls render and the stub's
# scroll settles (it re-applies the scroll for about 3 s).
VIRTUAL_TIME_MS = 6000
# A real screen has many colours; a blank or failed page has one or two.
MIN_DISTINCT_COLOURS = 64
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ScreenError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Shot:
    name: str
    scenario: str
    scroll: str
    widths: tuple[int, ...] = WIDTHS
    pilot: str = "open"
    trend: str = "populated"
    #: Window height per width (the default ``HEIGHT`` when a width is not listed).
    heights: tuple[tuple[int, int], ...] = ()

    def query(self) -> str:
        return f"index.html?scenario={self.scenario}&pilot={self.pilot}&trend={self.trend}&scroll={self.scroll}"

    def height(self, width: int) -> int:
        return dict(self.heights).get(width, HEIGHT)


SHOTS: tuple[Shot, ...] = (
    Shot("paper-game-ex1-board", "open", "pg-office-h"),
    Shot("paper-game-ex1-play", "open", "pg-play-h"),
    Shot("paper-game-ex1-history", "open", "pg-history-h"),
    Shot("paper-game-ex1-legacy-play", "legacy", "pg-play-h", (1440,)),
    Shot("paper-game-value-open", "open", "pg-wallet-h", heights=((1440, 1000), (960, 1900))),
    Shot("paper-game-value-empty", "empty", "pg-wallet-h", heights=((1440, 900), (960, 1300))),
    Shot("paper-game-value-stale", "stale", "pg-wallet-h", heights=((1440, 1000), (960, 1900))),
    Shot("paper-game-value-history", "stale", "pg-history-h"),
    Shot("pilot-shadow-open", "open", "pilot-shadow", pilot="open", heights=((1440, 2000), (960, 3400))),
    Shot("pilot-shadow-stale", "open", "pilot-shadow", pilot="stale", heights=((1440, 2000), (960, 3400))),
    Shot("pilot-shadow-locked", "open", "pilot-shadow", pilot="locked", heights=((1440, 2000), (960, 3400))),
    Shot("pilot-shadow-empty", "open", "pilot-shadow", pilot="empty", heights=((1440, 520), (960, 560))),
    Shot("trend-paper-populated", "open", "trend-paper", trend="populated", heights=((1440, 1760), (960, 2700))),
    Shot("trend-paper-empty", "open", "trend-paper", trend="empty", heights=((1440, 560), (960, 640))),
    Shot("trend-paper-skipped", "open", "trend-paper", trend="skipped", heights=((1440, 1800), (960, 2760))),
    Shot("trend-paper-refused", "open", "trend-paper", trend="refused", heights=((1440, 600), (960, 680))),
)


# --- PNG checks (standard library only) --------------------------------------------------------


def _chunks(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        kind = data[offset + 4:offset + 8]
        yield kind, data[offset + 8:offset + 8 + length]
        offset += 12 + length


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def png_summary(path: Path) -> tuple[int, int, int]:
    """(width, height, distinct colours) of an 8-bit RGB or RGBA PNG; ScreenError otherwise."""
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ScreenError(f"{path.name} is not a PNG")
    header: bytes | None = None
    compressed = bytearray()
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            header = body
        elif kind == b"IDAT":
            compressed += body
    if header is None or len(header) != 13:
        raise ScreenError(f"{path.name} has no valid IHDR")
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", header)
    channels = {2: 3, 6: 4}.get(colour)
    if depth != 8 or channels is None or interlace != 0:
        raise ScreenError(f"{path.name}: unsupported PNG format (depth {depth}, colour type {colour})")
    raw = zlib.decompress(bytes(compressed))
    stride = width * channels
    if len(raw) != height * (stride + 1):
        raise ScreenError(f"{path.name}: pixel data does not match its size")
    previous = bytearray(stride)
    colours: set[bytes] = set()
    for row in range(height):
        start = row * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                line[i] = (line[i] + _paeth(left, up, corner)) & 0xFF
            elif kind != 0:
                raise ScreenError(f"{path.name}: unknown row filter {kind}")
        if row % 4 == 0 and len(colours) < MIN_DISTINCT_COLOURS:
            colours.update(bytes(line[i:i + 3]) for i in range(0, stride, channels * 4))
        previous = line
    return width, height, len(colours)


def check_png(path: Path, width: int, height: int) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ScreenError(f"{path.name} was not written")
    got_width, got_height, colours = png_summary(path)
    if (got_width, got_height) != (width, height):
        raise ScreenError(f"{path.name} is {got_width}x{got_height}, expected {width}x{height}")
    if colours < MIN_DISTINCT_COLOURS:
        raise ScreenError(f"{path.name} looks blank ({colours} colours)")


# --- server and browser -----------------------------------------------------------------------


@contextmanager
def served(directory: Path) -> Iterator[int]:
    handler = functools.partial(preview.QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="paper-screens", daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False, timeout=30
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _stray_edges(profile: Path) -> list[int]:
    """Edge processes still using ``profile`` (only ours: the profile is private to this run)."""
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile}*' }} | ForEach-Object {{ $_.ProcessId }}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=False, timeout=60,
    )
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]


def stop_strays(profile: Path) -> int:
    strays = _stray_edges(profile)
    for pid in strays:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, timeout=30)
    return len(strays)


def capture(url: str, target: Path, width: int, profile: Path, height: int = HEIGHT) -> None:
    """One headless Edge screenshot of ``url`` at ``width`` x ``height``; Edge is always stopped."""
    command: Sequence[str] = (
        str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--mute-audio",
        f"--user-data-dir={profile}", f"--window-size={width},{height}", f"--screenshot={target}",
        f"--virtual-time-budget={VIRTUAL_TIME_MS}", url,
    )
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=EDGE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise ScreenError(f"Edge did not finish {target.name} in {EDGE_TIMEOUT_SECONDS} s") from error
    finally:
        _kill_tree(process)


def build(workdir: Path) -> Path:
    real_database = preview.real_database_path()
    if real_database.is_relative_to(workdir.resolve()):
        raise ScreenError("the real database path must not be inside the preview directory")
    now = datetime.now(UTC).replace(microsecond=0)
    (workdir / "db").mkdir()
    payloads = preview.build_payloads(workdir / "db", now)
    preview.validate_payloads(payloads)
    replay = preview.build_replay(workdir / "db", now)
    preview.validate_replay(replay)
    pilot = preview.build_pilot_payloads(workdir / "db", now)
    preview.validate_pilot_payloads(pilot)
    preview.validate_valuation(payloads, pilot)
    trend = preview.build_trend_payloads(workdir / "db")
    preview.validate_trend_payloads(trend, workdir / "db")
    target = preview.write_preview(workdir / "web", payloads, replay, pilot, trend)
    preview.validate_preview(target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR, help="where the PNGs are written")
    parser.add_argument("--only", default="", help="capture only the shots whose name starts with this")
    args = parser.parse_args(argv)
    shots = [shot for shot in SHOTS if shot.name.startswith(args.only)]
    if not shots:
        print(f"No shot is named {args.only!r}...", file=sys.stderr)
        return 1
    if not EDGE.is_file():
        print(f"Microsoft Edge not found at {EDGE}", file=sys.stderr)
        return 1
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="paper-game-screens-", ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        profile = workdir / "edge-profile"
        try:
            site = build(workdir)
            with served(site) as port:
                for shot in shots:
                    for width in shot.widths:
                        target = (out_dir / f"{shot.name}-{width}.png").resolve()
                        if target.exists():
                            target.unlink()
                        height = shot.height(width)
                        capture(f"http://127.0.0.1:{port}/{shot.query()}", target, width, profile, height)
                        check_png(target, width, height)
                        written.append(target)
                        print(f"OK {target.name}", flush=True)
        except (ScreenError, preview.PreviewError, preview.ps.PaperStoreError, preview.pilot_store.PilotStoreError,
                sqlite3.Error, OSError, subprocess.SubprocessError, preview.trend_paper_store.TrendPaperStoreError,
                preview.trend_p.PaperError, preview.trend_e.EngineError) as error:
            print(f"Screens failed: {error}", file=sys.stderr)
            return 1
        finally:
            strays = stop_strays(profile)
            if strays:
                print(f"Stopped {strays} leftover Edge process(es).", file=sys.stderr)
    print(f"{len(written)} screenshots written and checked; Edge and the server are stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
