"""T051a: deterministic OC-1 section 6 corpus builder (radar_v08/adapters/oc1_corpus_builder.py).

Every source here is SYNTHETIC: Kraken-shaped OHLCVT zips and a manifest written into a
temporary directory by ``make_sextant``. The real Sextant is never read, and nothing is ever
written into the repository: corpora go to temporary directories only (D31).

The synthetic 5-minute bars are shaped per 3-hour slot (see ``slot_rows``) so that every
category has more candidates than it needs; the tests check the builder's output against
values computed here by hand or recomputed independently from the prompt text, never by
re-running the builder's own helpers.
"""

import ast
import builtins
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
import warnings
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(REPOSITORY_ROOT))

from radar_v08.adapters import oc1_corpus_builder as ob  # noqa: E402
from radar_v08.adapters.benchmark_corpus import (  # noqa: E402
    CorpusError,
    CorpusMode,
    benchmark_profile,
    canonical_sha256,
    load_partition,
)
from radar_v08.adapters.model_profiles import load_model_profiles  # noqa: E402
from radar_v08.adapters.oc1_corpus_builder import (  # noqa: E402
    BuildError,
    BuildErrorCode,
)
from radar_v08.domain.integrity import (  # noqa: E402
    Bar,
    CheckStatus,
    InstrumentId,
    InstrumentKind,
    OhlcSeries,
    SourceTiming,
    evaluate_ohlc,
)
from radar_v08.workflow.benchmark import (  # noqa: E402
    CaseCategory,
    CorpusPartition,
    GoldSource,
    input_budget_tokens,
    prompt_token_bound,
)

sys.path.insert(0, os.fspath(REPOSITORY_ROOT / "scripts"))
import build_oc1_corpus as cli  # noqa: E402

SLOT = 3 * 3600
BAR = 300
LISTED = {
    "Kraken_OHLCVT_Q4_2023.zip": (datetime(2023, 11, 1, tzinfo=UTC), 61),
    "Kraken_OHLCVT_Q1_2024.zip": (datetime(2024, 1, 1, tzinfo=UTC), 31),
}
UNLISTED = "Kraken_OHLCVT_Q2_2024.zip"
ABSENT = "Kraken_OHLCVT_Q2_2023.zip"
BASE_PRICE = {"XBTUSD": 100.0, "ETHUSD": 20.0}
PATTERN_OFFSET = {"XBTUSD": 0, "ETHUSD": 2}
FIVE_MINUTES = timedelta(minutes=5)
ALL_CATEGORIES = {category.value for category in CaseCategory}
GOLD_CATEGORIES = {"invalid_or_stale", "insufficient_evidence", "admissible_no_edge"}


def price(value: float) -> str:
    return f"{value:.6f}"


def slot_rows(slot: int, pattern: int, base: float, *, tail_factor: float = 1.0, gaps: bool = True) -> list[str]:
    """36 bars of one 3-hour slot. The first 24 bars are the episode window:

    0 quiet: closes within 1 bp of the base, range about 3 bps (below the 52 bps bound)
    1 trend: +3.00 % over the window, last hour +1.48 % (same sign)
    2 conflict: +2.5 % in the first hour, -1 % in the last hour, window +1.475 %
    3 gaps: like 4, bars 5..8 missing (20 present, last present)
    4 neutral: alternating +/-0.4 % (range about 80 bps, window move -0.4 %)
    The last 12 bars (after the decision instant) never belong to a window.
    """
    lines: list[str] = []
    previous = base
    for index in range(36):
        epoch = slot + index * BAR
        if index < 24:
            if pattern == 0:
                close = base * (1 + 0.0001 * ((index % 3) - 1))
            elif pattern == 1:
                close = base * (1 + 0.03 * (index + 1) / 24)
            elif pattern == 2:
                close = base * (1 + 0.025 * (index + 1) / 12) if index < 12 else base * 1.025 * (1 - 0.01 * (index - 11) / 12)
            else:
                close = base * (1 + (0.004 if index % 2 == 0 else -0.004))
            if pattern == 3 and gaps and 5 <= index <= 8:
                previous = close
                continue
        else:
            close = base * (1 + 0.001 * (index % 2)) * tail_factor
        open_ = base if index == 0 else previous
        high = max(open_, close) * 1.00005
        low = min(open_, close) * 0.99995
        lines.append(f"{epoch},{price(open_)},{price(high)},{price(low)},{price(close)},1.5,10")
        previous = close
    return lines


def member_csv(pair: str, start: datetime, days: int, *, tail_factor: float = 1.0, gaps: bool = True) -> bytes:
    first = int(start.timestamp())
    lines: list[str] = []
    for slot in range(first, first + days * 86400, SLOT):
        pattern = (slot // SLOT + PATTERN_OFFSET[pair]) % 5
        lines.extend(slot_rows(slot, pattern, BASE_PRICE[pair], tail_factor=tail_factor, gaps=gaps))
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def write_zip(path: Path, members: dict[str, bytes]) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate names are written on purpose in one test
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))  # byte-stable zips
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_sextant(
    root: Path,
    *,
    tail_factor: float = 1.0,
    gaps: bool = True,
    days_scale: float = 1.0,
    extra_members: dict[str, bytes] | None = None,
) -> Path:
    """A Sextant-shaped ``data`` directory: two listed zips, one unlisted, JSON and parquet."""
    data = root / "data"
    archive_dir = data / "kraken-archive"
    archive_dir.mkdir(parents=True)
    manifest_files = []
    for name, (start, days) in LISTED.items():
        members: dict[str, bytes] = {
            "XBTUSD_1.csv": b"never,opened\r\n",
            "DOGEUSD_5.csv": b"not,in,the,universe\r\n",
            "../XBTUSD_5.csv": b"path,like,name\r\n",
            "sub/ETHUSD_5.csv": b"path,like,name\r\n",
        }
        for pair in ("XBTUSD", "ETHUSD"):
            members[f"{pair}_5.csv"] = member_csv(
                pair, start, max(1, int(days * days_scale)), tail_factor=tail_factor, gaps=gaps
            )
        members.update(extra_members or {})
        write_zip(data / name, members)
        manifest_files.append(
            {"name": name, "quarter": name[14:21], "sha256": sha256_file(data / name), "size_bytes": (data / name).stat().st_size}
        )
    manifest_files.append({"name": ABSENT, "quarter": "Q2_2023", "sha256": "0" * 64, "size_bytes": 1})
    # Not in the manifest: different data (it would move the split) that must never be read.
    write_zip(data / UNLISTED, {"XBTUSD_5.csv": member_csv("XBTUSD", datetime(2024, 4, 1, tzinfo=UTC), 30)})
    (archive_dir / "manifest.json").write_text(json.dumps({"files": manifest_files, "missing": 0}), encoding="utf-8")
    (archive_dir / "listing_calendar.json").write_text("{}", encoding="utf-8")
    (archive_dir / "bars.parquet").write_bytes(b"PAR1 not a real parquet")
    (data / "Kraken_OHLCVT_notes.txt").write_text("ignored", encoding="utf-8")
    return data


def snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """size, mtime_ns and sha256 of every file under ``root``, plus every directory."""
    state: dict[str, tuple[int, int, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            state[os.path.join(dirpath, name)] = (0, 0, "dir")
        for name in filenames:
            path = Path(dirpath, name)
            info = path.stat()
            state[os.fspath(path)] = (info.st_size, info.st_mtime_ns, sha256_file(path))
    return state


_ROW = re.compile(r"([0-9]{2}):([0-9]{2}),([0-9.]*),([0-9.]*),([0-9.]*),([0-9.]*)")
_HEADER = re.compile(
    r"Pair: ([A-Z0-9]+)/USD \(Kraken spot\)\. Decision instant: ([0-9T:-]+)Z\.\n"
    r"ev-bars: 5-minute bars of ([0-9-]+) UTC, prices in ([A-Z]+); columns open_time,open,high,low,close:\n"
)


def parse_prompt(user: str) -> tuple[str, datetime, str, list[tuple[datetime, list[str]]]]:
    """Independent reading of a case prompt: base, decision instant, price unit, bars as text."""
    header = _HEADER.match(user)
    assert header is not None, user[:200]
    base, decision_text, day_text, unit = header.groups()
    decision = datetime.strptime(decision_text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    day = datetime.strptime(day_text, "%Y-%m-%d").replace(tzinfo=UTC)
    bars = []
    for line in user.splitlines():
        match = _ROW.fullmatch(line)
        if match:
            hour, minute, *values = match.groups()
            bars.append((day + timedelta(hours=int(hour), minutes=int(minute)), values))
    return base, decision, unit, bars


def integrity_of(user: str) -> CheckStatus:
    base, decision, unit, rows = parse_prompt(user)
    instrument = InstrumentId("kraken", f"{base}USD", InstrumentKind.SPOT, base, "USD", base)
    # The prompt shows no volume; a zero volume is valid for integrity.py, so only what is shown decides.
    bars = tuple(
        Bar(when, *(Decimal(value) if value else Decimal("NaN") for value in values), Decimal(0)) for when, values in rows
    )
    series = OhlcSeries(instrument, FIVE_MINUTES, bars, unit, base, SourceTiming(received_at=decision))
    return evaluate_ohlc(series, instrument, decision, required_bars=24).status


class Built:
    """One build of the default synthetic source, shared by the read-only tests."""

    temp: tempfile.TemporaryDirectory[str]
    data: Path
    corpus: ob.BuiltCorpus
    out: Path


def setUpModule() -> None:  # noqa: N802
    Built.temp = tempfile.TemporaryDirectory(prefix="t051a-")
    root = Path(Built.temp.name)
    Built.data = make_sextant(root / "sextant")
    Built.corpus = ob.build_corpus(Built.data)
    Built.out = ob.write_corpus(Built.corpus, root / "corpus", Built.data)


def tearDownModule() -> None:  # noqa: N802
    Built.temp.cleanup()


def all_cases() -> list[dict]:
    return [dict(case) for partition in CorpusPartition for case in Built.corpus.cases[partition]]


class TestCountsAndLoader(unittest.TestCase):
    def test_exact_counts_per_partition_and_category(self) -> None:
        for partition, per_category in ((CorpusPartition.DEVELOPMENT, 20), (CorpusPartition.HOLDOUT, 40)):
            cases = Built.corpus.cases[partition]
            self.assertEqual(len(cases), per_category * 5)
            counts: dict[str, int] = {}
            for case in cases:
                counts[str(case["category"])] = counts.get(str(case["category"]), 0) + 1
            self.assertEqual(counts, {category: per_category for category in ALL_CATEGORIES})

    def test_written_corpus_loads_with_the_existing_loader(self) -> None:
        development = load_partition(Built.out, CorpusMode.DEVELOPMENT, expected_lock_sha256=Built.corpus.lock_sha256)
        holdout = load_partition(Built.out, CorpusMode.HOLDOUT, expected_lock_sha256=Built.corpus.lock_sha256)
        self.assertEqual((len(development.cases), len(holdout.cases)), (100, 200))
        self.assertFalse(development.synthetic)
        self.assertEqual(sorted(entry.name for entry in Built.out.iterdir()), ["development", "holdout", "lock.json"])
        lock = json.loads((Built.out / "lock.json").read_text(encoding="utf-8"))
        self.assertEqual(canonical_sha256(lock), Built.corpus.lock_sha256)
        self.assertEqual(lock["schema_version"], 2)

    def test_every_case_passes_the_harness_context_gate(self) -> None:
        profiles = load_model_profiles()
        budget = input_budget_tokens(benchmark_profile(profiles.get(profiles.default_profile_id)).inference)
        self.assertEqual(budget, 2800)
        for mode in CorpusMode:
            partition = load_partition(Built.out, mode, expected_lock_sha256=Built.corpus.lock_sha256)
            for case in partition.cases:
                with self.subTest(case.case_id):
                    self.assertLessEqual(prompt_token_bound(case), budget)

    def test_development_mode_never_reads_the_holdout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t051a-dev-") as temp:
            copy = ob.write_corpus(Built.corpus, Path(temp) / "corpus", Built.data)
            for path in (copy / "holdout").iterdir():
                path.write_bytes(b"\xff not json")
            (copy / "holdout" / "stray.bin").write_bytes(b"x")
            partition = load_partition(copy, CorpusMode.DEVELOPMENT, expected_lock_sha256=Built.corpus.lock_sha256)
            self.assertEqual(len(partition.cases), 100)
            with self.assertRaises(CorpusError):
                load_partition(copy, CorpusMode.HOLDOUT, expected_lock_sha256=Built.corpus.lock_sha256)


class TestDeterminism(unittest.TestCase):
    def test_two_builds_give_the_same_lock_and_bytes(self) -> None:
        again = ob.build_corpus(Built.data)
        self.assertEqual(again.lock_sha256, Built.corpus.lock_sha256)
        with tempfile.TemporaryDirectory(prefix="t051a-det-") as temp:
            second = ob.write_corpus(again, Path(temp) / "corpus", Built.data)
            first_files = {p.relative_to(Built.out): p.read_bytes() for p in Built.out.rglob("*.json")}
            second_files = {p.relative_to(second): p.read_bytes() for p in second.rglob("*.json")}
            self.assertEqual(first_files, second_files)
            self.assertEqual(len(first_files), 301)

    def test_seed_and_rules_are_fixed_in_code(self) -> None:
        self.assertEqual((ob.SEED, ob.RULES_VERSION), (20260919, "oc1-corpus-rules-1"))
        lock = Built.corpus.lock
        self.assertEqual(lock["builder"]["seed"], 20260919)
        self.assertEqual(
            [item["zip"] for item in lock["builder"]["source_zips"]], ["Kraken_OHLCVT_Q4_2023.zip", "Kraken_OHLCVT_Q1_2024.zip"]
        )


class TestSourceHandling(unittest.TestCase):
    def test_divergent_hash_aborts_before_any_member_is_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t051a-hash-") as temp:
            data = make_sextant(Path(temp))
            manifest_path = data / "kraken-archive" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][1]["sha256"] = "a" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            opened: list[str] = []
            real_open = zipfile.ZipFile.open

            def spy(self, name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
                opened.append(getattr(self, "filename", "") or "")
                return real_open(self, name, *args, **kwargs)

            with mock.patch.object(zipfile.ZipFile, "open", spy), self.assertRaises(BuildError) as caught:
                ob.build_corpus(data)
            self.assertIs(caught.exception.code, BuildErrorCode.ZIP_HASH_MISMATCH)
            self.assertIn("Kraken_OHLCVT_Q1_2024.zip", caught.exception.where)
            # Q4_2023 (verified) was read; Q1_2024 never had a member opened.
            self.assertTrue(opened)
            self.assertFalse([name for name in opened if "Q1_2024" in name])

    def test_divergent_size_and_changed_bytes_abort(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t051a-size-") as temp:
            data = make_sextant(Path(temp))
            with open(data / "Kraken_OHLCVT_Q4_2023.zip", "ab") as handle:
                handle.write(b"\0")
            with self.assertRaises(BuildError) as caught:
                ob.build_corpus(data)
            self.assertIs(caught.exception.code, BuildErrorCode.ZIP_SIZE_MISMATCH)
        with tempfile.TemporaryDirectory(prefix="t051a-flip-") as temp:
            data = make_sextant(Path(temp))
            path = data / "Kraken_OHLCVT_Q4_2023.zip"
            content = bytearray(path.read_bytes())
            content[100] ^= 0xFF
            path.write_bytes(bytes(content))
            with self.assertRaises(BuildError) as caught:
                ob.build_corpus(data)
            self.assertIs(caught.exception.code, BuildErrorCode.ZIP_HASH_MISMATCH)

    def test_unlisted_zip_is_ignored_and_never_opened(self) -> None:
        self.assertEqual(Built.corpus.ignored_zips, (UNLISTED,))
        self.assertEqual(Built.corpus.listed_zips_absent, (ABSENT,))
        with tempfile.TemporaryDirectory(prefix="t051a-unlisted-") as temp:
            data = make_sextant(Path(temp))
            (data / UNLISTED).unlink()
            without = ob.build_corpus(data)
        self.assertEqual(without.lock_sha256, Built.corpus.lock_sha256)
        self.assertEqual(without.ignored_zips, ())

    def test_only_manifest_json_and_listed_zips_are_opened_read_only(self) -> None:
        calls: list[tuple[str, str]] = []
        real_open = builtins.open

        def spy(file, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            calls.append((os.fspath(file) if isinstance(file, (str, os.PathLike)) else repr(file), mode))
            return real_open(file, mode, *args, **kwargs)

        with mock.patch.object(builtins, "open", spy):
            ob.build_corpus(Built.data)
        prefix = os.path.normcase(os.fspath(Built.data.resolve()))
        source = [(path, mode) for path, mode in calls if os.path.normcase(os.path.realpath(path)).startswith(prefix)]
        self.assertTrue(source)
        self.assertEqual({mode for _, mode in source}, {"rb"})
        names = sorted({Path(path).name for path, _ in source})
        self.assertEqual(names, ["Kraken_OHLCVT_Q1_2024.zip", "Kraken_OHLCVT_Q4_2023.zip", "manifest.json"])
        self.assertFalse([path for path, _ in calls if path.endswith(".parquet") or "listing_calendar" in path])

    def test_never_extracts_and_writes_nothing_in_the_source(self) -> None:
        before = snapshot(Built.data.parent)

        def refuse(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError("extraction attempted")

        with (
            mock.patch.object(zipfile.ZipFile, "extract", refuse),
            mock.patch.object(zipfile.ZipFile, "extractall", refuse),
            tempfile.TemporaryDirectory(prefix="t051a-out-") as temp,
        ):
            corpus = ob.build_corpus(Built.data)
            ob.write_corpus(corpus, Path(temp) / "corpus", Built.data)
        self.assertEqual(snapshot(Built.data.parent), before)
        source = (REPOSITORY_ROOT / "radar_v08" / "adapters" / "oc1_corpus_builder.py").read_text(encoding="utf-8")
        used = {node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)}
        used |= {node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)}
        for token in ("extract", "extractall", "mkstemp", "mkdtemp", "TemporaryDirectory", "NamedTemporaryFile", "getenv", "environ"):
            self.assertNotIn(token, used)

    def test_malformed_duplicate_and_oversized_members_are_refused(self) -> None:
        cases = {
            "malformed row": ({"XBTUSD_5.csv": b"1698796800,1,1,1,1\r\n"}, BuildErrorCode.MEMBER_MALFORMED),
            "not ascii": ({"XBTUSD_5.csv": "1698796800,1,1,1,1,1,1\u00e9\r\n".encode("utf-8")}, BuildErrorCode.MEMBER_MALFORMED),
            "off grid": ({"XBTUSD_5.csv": b"1698796801,1,1,1,1,1,1\r\n"}, BuildErrorCode.MEMBER_MALFORMED),
            "duplicate member": ({"ETHUSD_5.csv": b"1698796800,1,1,1,1,1,1\r\n"}, BuildErrorCode.MEMBER_DUPLICATE),
        }
        for name, (extra, code) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory(prefix="t051a-bad-") as temp:
                data = make_sextant(Path(temp))
                path = data / "Kraken_OHLCVT_Q4_2023.zip"
                with warnings.catch_warnings(), zipfile.ZipFile(path, "a") as archive:
                    warnings.simplefilter("ignore")
                    for member, content in extra.items():
                        if name == "duplicate member":
                            archive.writestr(member, content)
                if name != "duplicate member":
                    members = {}
                    with zipfile.ZipFile(path) as archive:
                        for info in archive.infolist():
                            members[info.filename] = archive.read(info)
                    members.update(extra)
                    path.unlink()
                    write_zip(path, members)
                manifest_path = data / "kraken-archive" / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["files"][0].update(sha256=sha256_file(path), size_bytes=path.stat().st_size)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(BuildError) as caught:
                    ob.build_corpus(data)
                self.assertIs(caught.exception.code, code)
        with mock.patch.object(ob, "MAX_MEMBER_BYTES", 1000), self.assertRaises(BuildError) as caught:
            ob.build_corpus(Built.data)
        self.assertIs(caught.exception.code, BuildErrorCode.MEMBER_TOO_LARGE)

    def test_missing_manifest_and_bad_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t051a-manifest-") as temp:
            data = make_sextant(Path(temp))
            manifest_path = data / "kraken-archive" / "manifest.json"
            manifest_path.write_text('{"files": [{"name": "x.zip", "sha256": "0", "size_bytes": 1}]}', encoding="utf-8")
            with self.assertRaises(BuildError) as caught:
                ob.build_corpus(data)
            self.assertIs(caught.exception.code, BuildErrorCode.MANIFEST_INVALID)
            manifest_path.unlink()
            with self.assertRaises(BuildError) as caught:
                ob.build_corpus(data)
            self.assertIs(caught.exception.code, BuildErrorCode.MANIFEST_MISSING)

    def test_count_not_reached_is_a_typed_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t051a-short-") as temp:
            data = make_sextant(Path(temp), days_scale=0.3)
            with self.assertRaises(BuildError) as caught:
                ob.build_corpus(data)
            self.assertIs(caught.exception.code, BuildErrorCode.COUNT_NOT_REACHED)
            self.assertRegex(caught.exception.where, r"^(development|holdout)/[a-z_]+$")


class TestSeparation(unittest.TestCase):
    def test_development_strictly_before_holdout_with_seven_days(self) -> None:
        def when(case: dict, key: str) -> datetime:
            return datetime.strptime(case["provenance"][key], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

        development = Built.corpus.cases[CorpusPartition.DEVELOPMENT]
        holdout = Built.corpus.cases[CorpusPartition.HOLDOUT]
        last_development = max(when(case, "decision_at_utc") for case in development)
        first_holdout = min(when(case, "window_start_utc") for case in holdout)
        self.assertGreaterEqual(first_holdout - last_development, timedelta(days=7))
        separation = Built.corpus.lock["separation"]
        self.assertEqual(separation["development_end_utc"], last_development.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(separation["holdout_start_utc"], first_holdout.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(separation["gap_seconds"], int((first_holdout - last_development).total_seconds()))
        self.assertEqual(separation["min_gap_seconds"], 7 * 86400)

    def test_episodes_never_overlap_and_no_pair_window_reused(self) -> None:
        intervals = []
        for case in all_cases():
            provenance = case["provenance"]
            start = datetime.strptime(provenance["window_start_utc"], "%Y-%m-%dT%H:%M:%SZ")
            end = datetime.strptime(provenance["decision_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
            self.assertLess(start, end)
            intervals.append((start, end, provenance["pair"]))
        intervals.sort()
        for (start_a, end_a, _), (start_b, _, _) in zip(intervals, intervals[1:]):
            self.assertLessEqual(end_a, start_b)
        self.assertEqual(len({(pair, start) for start, _, pair in intervals}), 300)

    def test_every_case_records_rule_pair_interval_and_source(self) -> None:
        hashes = {item["zip"]: item["sha256"] for item in Built.corpus.lock["builder"]["source_zips"]}
        for case in all_cases():
            provenance = case["provenance"]
            with self.subTest(case["case_id"]):
                self.assertTrue(case["construction"]["rule"])
                self.assertEqual(case["construction"]["rules_version"], "oc1-corpus-rules-1")
                self.assertIn(provenance["pair"], ("XBTUSD", "ETHUSD"))
                self.assertEqual(provenance["source"]["member"], f"{provenance['pair']}_5.csv")
                self.assertEqual(provenance["source"]["zip_sha256"], sha256_file(Built.data / provenance["source"]["zip"]))
                self.assertEqual(hashes[provenance["source"]["zip"]], provenance["source"]["zip_sha256"])
                self.assertEqual(
                    provenance["fee"],
                    {
                        "bps_per_leg": "26.0",
                        "legs": 2,
                        "basis": "uncalibrated_assumption",
                        "origin_file": "radar_v08/config.py",
                        "origin_key": "UNCALIBRATED_FEES['spot_taker_bps']",
                    },
                )
                self.assertEqual(case["prompt"]["system"], ob.SYSTEM_PROMPT)  # same view for every case


class TestGold(unittest.TestCase):
    def test_gold_only_in_three_categories_and_unavailable_in_two(self) -> None:
        for case in all_cases():
            with self.subTest(case["case_id"]):
                if case["category"] in GOLD_CATEGORIES:
                    self.assertEqual(case["gold_status"], "deterministic")
                    self.assertEqual(case["gold"]["source"], "deterministic_rule")
                    self.assertIsNone(case["gold"]["human_review"])
                    self.assertEqual(case["gold"]["abstain_expected"], case["category"] != "admissible_no_edge")
                else:
                    self.assertIsNone(case["gold"])
                    self.assertEqual(case["gold_status"], "gold_unavailable")
                    details = case["construction"]["details"]
                    self.assertEqual(set(details), {"selection", "cost_lower_bound_bps", "gold_unavailable_reason"})
                    self.assertNotIn("abstain", json.dumps(details))
        holdout = load_partition(Built.out, CorpusMode.HOLDOUT, expected_lock_sha256=Built.corpus.lock_sha256)
        for case in holdout.cases:
            if case.category.value in GOLD_CATEGORIES:
                self.assertIs(case.gold.source, GoldSource.DETERMINISTIC_RULE)
            else:
                self.assertIs(case.gold.source, GoldSource.GOLD_UNAVAILABLE)
                self.assertIsNone(case.gold.abstain_expected)

    def test_invalid_cases_fail_integrity_from_the_prompt_alone(self) -> None:
        kinds: dict[str, int] = {}
        for case in all_cases():
            if case["category"] != "invalid_or_stale":
                continue
            with self.subTest(case["case_id"]):
                self.assertIs(integrity_of(case["prompt"]["user"]), CheckStatus.FAIL)
                mutation = case["construction"]["details"]["mutation"]
                self.assertEqual(case["construction"]["details"]["integrity"]["status"], "FAIL")
                self.assertNotEqual(mutation["original"], mutation["mutated"])
                kinds[mutation["kind"]] = kinds.get(mutation["kind"], 0) + 1
        self.assertEqual(
            kinds,
            {"stale_timestamp": 12, "missing_field": 12, "mixed_currency": 12, "impossible_value": 12, "timestamp_out_of_order": 12},
        )

    def test_insufficient_cases_are_unknown_from_the_prompt_alone(self) -> None:
        for case in all_cases():
            if case["category"] != "insufficient_evidence":
                continue
            with self.subTest(case["case_id"]):
                self.assertIs(integrity_of(case["prompt"]["user"]), CheckStatus.UNKNOWN)
                self.assertEqual(case["construction"]["details"]["coverage"]["kind"], "real_gap")
                self.assertLess(len(parse_prompt(case["prompt"]["user"])[3]), 24)

    def test_short_windows_fill_insufficient_evidence_when_there_are_no_real_gaps(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t051a-nogaps-") as temp:
            corpus = ob.build_corpus(make_sextant(Path(temp), gaps=False))
        insufficient = [
            dict(case)
            for partition in CorpusPartition
            for case in corpus.cases[partition]
            if case["category"] == "insufficient_evidence"
        ]
        self.assertEqual(len(insufficient), 60)
        for case in insufficient:
            self.assertEqual(case["construction"]["details"]["coverage"], {"kind": "short_window", "bars_shown": 12, "bars_required": 24})
            self.assertIs(integrity_of(case["prompt"]["user"]), CheckStatus.UNKNOWN)

    def test_no_edge_label_uses_only_bars_closed_by_the_decision_instant(self) -> None:
        checked = 0
        for case in all_cases():
            if case["category"] != "admissible_no_edge":
                continue
            with self.subTest(case["case_id"]):
                _, decision, unit, rows = parse_prompt(case["prompt"]["user"])
                self.assertEqual(unit, "USD")
                self.assertEqual(len(rows), 24)
                self.assertTrue(all(when + FIVE_MINUTES <= decision for when, _ in rows))
                highs = [Decimal(values[1]) for _, values in rows]
                lows = [Decimal(values[2]) for _, values in rows]
                last_close = Decimal(rows[-1][1][3])
                # 52 bps = 2 x 26.0 bps (config.py literal default), computed by hand.
                self.assertLess((max(highs) - min(lows)) * 10000, Decimal(52) * last_close)
                bound = case["construction"]["details"]["cost_lower_bound"]
                self.assertEqual((bound["kind"], bound["status"], bound["bps"]), ("COST_LOWER_BOUND", "COST_INCOMPLETE", "52"))
                self.assertEqual(
                    sorted((item["component"], item["leg"], item["reason"]) for item in bound["missing"]),
                    [
                        ("slippage", "entry", "not_observed"),
                        ("slippage", "exit", "not_observed"),
                        ("spread", "entry", "not_observed"),
                        ("spread", "exit", "not_observed"),
                    ],
                )
                self.assertNotIn("total", json.dumps(bound))
                checked += 1
        self.assertEqual(checked, 60)

    def test_data_after_the_decision_instant_changes_nothing(self) -> None:
        """Bars 25..36 of every slot (after each decision instant) multiplied by ten."""
        with tempfile.TemporaryDirectory(prefix="t051a-future-") as temp:
            spiked = ob.build_corpus(make_sextant(Path(temp), tail_factor=10.0))

        def without_zip_hash(case: dict) -> dict:
            copy = json.loads(json.dumps(case))
            copy["provenance"]["source"].pop("zip_sha256")
            return copy

        for partition in CorpusPartition:
            before = [without_zip_hash(dict(case)) for case in Built.corpus.cases[partition]]
            after = [without_zip_hash(dict(case)) for case in spiked.cases[partition]]
            self.assertEqual(before, after)

    def test_fee_is_the_config_literal_default_and_never_read_from_env(self) -> None:
        tree = ast.parse((REPOSITORY_ROOT / "radar_v08" / "config.py").read_text(encoding="utf-8"))
        literal = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "UNCALIBRATED_FEES" for t in node.targets):
                assert isinstance(node.value, ast.Dict)
                for key, value in zip(node.value.keys, node.value.values):
                    if isinstance(key, ast.Constant) and key.value == "spot_taker_bps":
                        getenv_call = value.args[0]  # float(os.getenv("RADAR_FEE_SPOT_TAKER_BPS", "26.0"))
                        literal = getenv_call.args[1].value
        self.assertEqual(literal, "26.0")
        self.assertEqual(ob.FEE_BPS_PER_LEG, Decimal(literal))
        source = (REPOSITORY_ROOT / "radar_v08" / "adapters" / "oc1_corpus_builder.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                imported.add("." * node.level + (node.module or ""))
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for forbidden in ("..config", "radar_v08.config", "requests", "socket", "urllib", "http", "subprocess"):
            self.assertNotIn(forbidden, imported)
        with mock.patch.dict(os.environ, {"RADAR_FEE_SPOT_TAKER_BPS": "0"}):
            self.assertEqual(ob.cost_lower_bound("XBTUSD").lower_bound_bps, Decimal("52.0000"))

    def test_missing_or_negative_fee_is_a_typed_error(self) -> None:
        for value in (None, Decimal("-1"), Decimal("NaN")):
            with self.subTest(str(value)), mock.patch.object(ob, "FEE_BPS_PER_LEG", value):
                with self.assertRaises(BuildError) as caught:
                    ob.build_corpus(Built.data)
                self.assertIs(caught.exception.code, BuildErrorCode.FEE_UNAVAILABLE)


class TestOutputDirectory(unittest.TestCase):
    def assertRefused(self, out: Path, data: Path, code: BuildErrorCode = BuildErrorCode.OUTPUT_REFUSED) -> None:  # noqa: N802
        with self.assertRaises(BuildError) as caught:
            ob.check_output_dir(out, data)
        self.assertIs(caught.exception.code, code)

    def test_refused_locations(self) -> None:
        data = Built.data
        self.assertRefused(REPOSITORY_ROOT, data)
        self.assertRefused(REPOSITORY_ROOT.parent, data)
        self.assertRefused(REPOSITORY_ROOT / "tests" / "fixtures" / "benchmark_corpus", data)
        self.assertRefused(REPOSITORY_ROOT / "tests" / "fixtures" / "benchmark_corpus" / "new", data)
        self.assertRefused(REPOSITORY_ROOT / ".git" / "corpus", data)
        self.assertRefused(data, data)
        self.assertRefused(data / "kraken-archive" / "corpus", data)
        self.assertRefused(data.parent, data, BuildErrorCode.OUTPUT_REFUSED)
        self.assertRefused(Path("<sextant-repo>/data/corpus"), data)
        self.assertRefused(Path("c:/users/user/desktop/SEXTANT/elsewhere"), data)
        self.assertRefused(Built.out, data, BuildErrorCode.OUTPUT_EXISTS)
        allowed = ob.check_output_dir(REPOSITORY_ROOT / "docs" / "benchmark_corpus_oc1_not_created", data)
        self.assertFalse(allowed.exists())

    def test_cli_requires_both_arguments_and_refuses_the_repository_root(self) -> None:
        for argv in ([], ["--sextant-data", "x"], ["--out", "y"]):
            with self.subTest(argv), mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                cli.parse_arguments(argv)
        before = sorted(entry.name for entry in REPOSITORY_ROOT.iterdir())
        with mock.patch("sys.stderr", io.StringIO()) as stderr:
            code = cli.main(["--sextant-data", os.fspath(Built.data), "--out", os.fspath(REPOSITORY_ROOT)])
        self.assertEqual(code, 2)
        self.assertIn("output_refused", stderr.getvalue())
        self.assertEqual(sorted(entry.name for entry in REPOSITORY_ROOT.iterdir()), before)

    def test_cli_builds_into_a_new_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t051a-cli-") as temp, mock.patch("sys.stdout", io.StringIO()) as stdout:
            code = cli.main(["--sextant-data", os.fspath(Built.data), "--out", os.fspath(Path(temp) / "corpus")])
            self.assertEqual(code, 0)
            self.assertIn(f"lock sha256 {Built.corpus.lock_sha256}", stdout.getvalue())
            self.assertEqual(canonical_sha256(json.loads((Path(temp) / "corpus" / "lock.json").read_text("utf-8"))), Built.corpus.lock_sha256)

    def test_cli_script_never_names_the_harness(self) -> None:
        # tests/test_benchmark_harness.py forbids that word in every script.
        self.assertNotIn("bench" + "mark", (REPOSITORY_ROOT / "scripts" / "build_oc1_corpus.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
