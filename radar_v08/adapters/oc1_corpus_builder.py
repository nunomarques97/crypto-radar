"""Deterministic builder of the OC-1 section 6 corpus from recorded Kraken OHLCVT zips (T051a).

Decisions D59, D60 and D64 (docs/forja/DECISIONS.md). The builder reads the quarterly
``Kraken_OHLCVT_Q<n>_<year>.zip`` archives of a Sextant ``data`` directory and returns a
corpus in schema v2 of ``adapters.benchmark_corpus`` (300 cases: 100 development, 20 per
category, and 200 holdout, 40 per category), which ``write_corpus`` writes to a directory the
caller names. Seed, universe and every selection rule are constants of this module; there is
no environment read, no configuration import and no network.

Reading the source (external input, fail closed with a typed ``BuildError``)
------------------------------------------------------------------------------

* Only regular files named ``Kraken_OHLCVT_Q[1-4]_20YY.zip`` in the data directory itself
  are considered, and only when ``kraken-archive/manifest.json`` lists them; any other zip is
  ignored and reported. From ``kraken-archive`` only that JSON is read (never a ``.parquet``).
* Every file is opened ``"rb"``. A zip's size and sha256 are checked against the manifest
  BEFORE any member is read; a difference aborts the build (``ZIP_HASH_MISMATCH``), nothing is
  ever "corrected". The second pass re-hashes each zip it reads again (``ZIP_CHANGED``).
* Members are read in memory with ``ZipFile.open`` only: nothing is extracted, no temporary
  file is created, nothing is written anywhere near the source. Only members named exactly
  ``<PAIR>_5.csv`` for a pair of ``PAIRS`` are opened; a zip holding such a name twice, more
  than ``MAX_ZIP_MEMBERS`` entries, or a member over ``MAX_MEMBER_BYTES`` uncompressed (the
  header is checked, then the bytes actually read) is refused. Rows must be strict ASCII
  ``epoch,open,high,low,close,volume,trades`` on the 5-minute grid, strictly increasing.
* No intermediate files are needed; if a later step ever needs one, D59 designates
  ``<t051-workdir>/work`` only.

Episodes and separation
-----------------------

Time is cut into 3-hour slots on the UTC epoch grid (``SLOT_BARS`` 5-minute bars; a slot never
crosses a day or a quarter). An episode is the first ``WINDOW_BARS`` (24) bars of a slot and its
decision instant (the close of the last bar shown, or later for a stale mutation, always inside
the slot). At most ONE case uses a slot, whatever the pair, so episodes never overlap and no
pair + window is reused. The covered timeline, minus ``MIN_PARTITION_GAP_SECONDS`` (7 days), is
split one third development, two thirds holdout: every development case ends before every
holdout case starts, and the actual gap (at least 7 days) is written in the lock.

Categories (D60), each built by a fixed rule; ``PASS`` below means ``integrity.evaluate_ohlc``
passes on the 24 bars with 24 bars required at the decision instant (a "clean" window):

* ``invalid_or_stale`` (gold: abstain / reject input): a clean window with ONE recorded
  deterministic mutation, in rotation: last bar older than the OC-1 freshness at the decision
  instant, a missing field, a mixed-up price currency, an impossible (zero) price, a duplicated
  bar timestamp. The gold is ``integrity.evaluate_ohlc`` on the mutated input returning FAIL.
  (A plain gap is UNKNOWN in integrity.py, not FAIL, so gaps belong to the next category.)
* ``insufficient_evidence`` (gold: abstain): coverage below the 24 contiguous bars required,
  from real gaps in the recorded bars (at least 12 present, the last one present), then, if
  the real gaps are not enough, from a short window (the last 12 bars of a clean window). The
  gold is ``integrity.evaluate_ohlc`` returning UNKNOWN.
* ``admissible_no_edge`` (gold: answer, no edge): a clean window whose visible gross edge -
  ``(max high - min low) / last close`` over the bars shown, all closed at or before the
  decision instant - is strictly below the known minimum round-trip cost, the lower bound of
  ``costs.round_trip_cost_lower_bound`` for two spot taker legs at the fee below. Exact
  Decimal comparison; nothing after the decision instant is read.
* ``admissible_positive`` and ``conflicting_evidence``: clean windows chosen by the documented
  rules in ``POSITIVE_RULE`` and ``CONFLICT_RULE`` (price moves visible in the window, compared
  with the same cost bound). They carry NO gold (``gold_status = gold_unavailable``) and no
  field that reads as a label: their gold needs two independent human reviews (OC-1 sec. 6).

Nothing is labelled from future returns, an opportunity score or a model.

Fee: ``FEE_BPS_PER_LEG`` is a copy of the literal default of ``UNCALIBRATED_FEES["spot_taker_bps"]``
in ``radar_v08/config.py`` (26.0), declared an uncalibrated assumption; a test proves the two
are equal without importing config.py. Its value and origin are written in every case.

Selection: per partition, categories in ``CATEGORY_ORDER``; each pool is ordered by
``sha256(SEED | RULES_VERSION | category | pair | slot)`` and filled with the first candidates
whose slot is free. A pool that cannot reach its count aborts the build
(``COUNT_NOT_REACHED``): there is no synthetic filler.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import BinaryIO

from ..domain.costs import (
    CostBoundError,
    CostInputError,
    CostInstrument,
    CostLowerBound,
    CostScenarioInput,
    FeeBasis,
    FeeInput,
    Missing,
    MissingReason,
    ScenarioSize,
    Side,
    SizeProvenance,
    SpreadConvention,
    round_trip_cost_lower_bound,
)
from ..domain.integrity import (
    OC1_POLICY,
    Bar,
    CapabilityResult,
    CheckStatus,
    InstrumentId,
    InstrumentKind,
    OhlcSeries,
    SourceTiming,
    evaluate_ohlc,
)
from ..workflow.benchmark import (
    BenchmarkCase,
    CaseCategory,
    CorpusPartition,
    GoldSource,
    gold_unavailable,
    prompt_token_bound,
)
from ..workflow.scheduler import OC1_ROLE_PROFILES, Role
from .benchmark_corpus import (
    ABSTAIN_BY_CATEGORY,
    CORPUS_SCHEMA_VERSION_V2,
    GOLD_STATUS_DETERMINISTIC,
    GOLD_STATUS_UNAVAILABLE,
    LOCK_FILE_NAME,
    MIN_PARTITION_GAP_SECONDS,
    canonical_sha256,
    prompt_fingerprint,
)

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
SYNTHETIC_FIXTURE_CORPUS = REPOSITORY_ROOT / "tests" / "fixtures" / "benchmark_corpus"
SEXTANT_ROOT = Path("<sextant-repo>")
WORK_DIR = Path("<t051-workdir>/work")  # D59: the only place for intermediate files

CORPUS_ID = "oc1-sec6-kraken-ohlcvt-v1"
RULES_VERSION = "oc1-corpus-rules-1"
SEED = 20260919

VENUE = "kraken"
QUOTE = "USD"
MUTATED_CURRENCY = "EUR"
# Fixed universe: USD spot pairs, majors and mid caps, by their Kraken archive names.
PAIRS: tuple[str, ...] = (
    "XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD", "ADAUSD", "XDGUSD", "LTCUSD", "DOTUSD", "LINKUSD", "AVAXUSD",
    "ATOMUSD", "XLMUSD", "TRXUSD", "BCHUSD", "UNIUSD", "ALGOUSD", "FILUSD", "NEARUSD", "AAVEUSD", "ETCUSD",
    "XMRUSD", "ZECUSD", "KSMUSD", "MANAUSD", "SANDUSD", "GRTUSD", "CRVUSD", "COMPUSD", "SNXUSD", "XTZUSD",
    "DASHUSD", "MINAUSD", "KAVAUSD", "FLOWUSD", "ENJUSD", "BATUSD", "ANKRUSD", "STORJUSD", "LRCUSD", "SUSHIUSD",
)

BAR_SECONDS = 300
WINDOW_BARS = 24
SLOT_BARS = 36
SLOT_SECONDS = SLOT_BARS * BAR_SECONDS
SHORT_WINDOW_BARS = 12
MIN_GAP_WINDOW_BARS = 12
STALE_DELAY_SECONDS = 45 * 60
DEVELOPMENT_PER_CATEGORY = 20
HOLDOUT_PER_CATEGORY = 40
DEVELOPMENT_SHARE = (1, 3)  # of the covered timeline after the gap

# Copy of the literal default of UNCALIBRATED_FEES["spot_taker_bps"] in radar_v08/config.py.
FEE_BPS_PER_LEG: Decimal | None = Decimal("26.0")
FEE_ORIGIN_FILE = "radar_v08/config.py"
FEE_ORIGIN_KEY = "UNCALIBRATED_FEES['spot_taker_bps']"
FEE_SOURCE = f"{FEE_ORIGIN_FILE} {FEE_ORIGIN_KEY} literal default"

POSITIVE_MULTIPLE = 3
POSITIVE_RULE = (
    "clean window; |last close / first open - 1| >= 3 x the cost lower bound; the last-hour move "
    "(close of bar 24 / close of bar 12 - 1) has the same sign and is at least the cost lower bound"
)
CONFLICT_RULE = (
    "clean window; the whole-window move (last close / first open - 1) and the last-hour move "
    "(close of bar 24 / close of bar 12 - 1) have opposite signs and each is at least the cost lower bound"
)
VISIBLE_EDGE_DEFINITION = (
    "(max high - min low) / last close over the bars shown, all closed at or before the decision instant"
)

CATEGORY_ORDER: tuple[CaseCategory, ...] = (
    CaseCategory.ADMISSIBLE_NO_EDGE,
    CaseCategory.CONFLICTING_EVIDENCE,
    CaseCategory.ADMISSIBLE_POSITIVE,
    CaseCategory.INSUFFICIENT_EVIDENCE,
    CaseCategory.INVALID_OR_STALE,
)
CASE_SLUGS: Mapping[CaseCategory, str] = {
    CaseCategory.INVALID_OR_STALE: "invalid-stale",
    CaseCategory.INSUFFICIENT_EVIDENCE: "insufficient",
    CaseCategory.ADMISSIBLE_NO_EDGE: "no-edge",
    CaseCategory.ADMISSIBLE_POSITIVE: "positive",
    CaseCategory.CONFLICTING_EVIDENCE: "conflict",
}
PARTITION_PREFIX: Mapping[CorpusPartition, str] = {CorpusPartition.DEVELOPMENT: "dev", CorpusPartition.HOLDOUT: "hold"}


class Mutation(Enum):
    STALE_TIMESTAMP = "stale_timestamp"
    MISSING_FIELD = "missing_field"
    MIXED_CURRENCY = "mixed_currency"
    IMPOSSIBLE_VALUE = "impossible_value"
    TIMESTAMP_OUT_OF_ORDER = "timestamp_out_of_order"


MUTATION_ROTATION: tuple[Mutation, ...] = tuple(Mutation)

MAX_MANIFEST_BYTES = 1_048_576
MAX_ZIP_MEMBERS = 50_000
MAX_MEMBER_BYTES = 64 * 1_048_576
HASH_CHUNK_BYTES = 1_048_576

EVIDENCE_BARS = "ev-bars"
EVIDENCE_COST = "ev-cost"
SYSTEM_PROMPT = (
    "You are the Screener role in an offline evaluation of recorded public Kraken spot data (5-minute "
    "OHLC bars, UTC; no order book was observed). An admissible claim needs 24 contiguous closed bars "
    "ending at the decision instant, the last closed at most 6 minutes before it, with coherent prices "
    "in the pair's quote currency. "
    "Classify the evidence into exactly one category: invalid_or_stale, conflicting_evidence, "
    "admissible_positive, admissible_no_edge or insufficient_evidence. Set abstain to true when the "
    "evidence is invalid, stale or insufficient. Cite only the evidence ids supplied. Answer only with "
    "the JSON object of the response schema. Risk values are owned by deterministic code and are never "
    "part of the answer."
)

_ZIP_NAME = re.compile(r"Kraken_OHLCVT_Q([1-4])_(20[0-9]{2})\.zip")
_MEMBER_NAME = re.compile(r"[A-Z0-9]{2,16}_5\.csv")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EPOCH_TEXT = re.compile(r"[0-9]{1,12}")
_DECIMAL_TEXT = re.compile(r"[0-9]{1,15}(\.[0-9]{1,20})?([eE][-+]?[0-9]{1,3})?")
_COUNT_TEXT = re.compile(r"[0-9]{1,12}")


class BuildErrorCode(Enum):
    SOURCE_DIR_INVALID = "source_dir_invalid"
    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_INVALID = "manifest_invalid"
    NO_LISTED_ZIP = "no_listed_zip"
    ZIP_SIZE_MISMATCH = "zip_size_mismatch"
    ZIP_HASH_MISMATCH = "zip_hash_mismatch"
    ZIP_CHANGED = "zip_changed"
    ZIP_INVALID = "zip_invalid"
    TOO_MANY_MEMBERS = "too_many_members"
    MEMBER_DUPLICATE = "member_duplicate"
    MEMBER_TOO_LARGE = "member_too_large"
    MEMBER_MALFORMED = "member_malformed"
    FEE_UNAVAILABLE = "fee_unavailable"
    COUNT_NOT_REACHED = "count_not_reached"
    SEPARATION_TOO_SHORT = "separation_too_short"
    PROMPT_OVER_BUDGET = "prompt_over_budget"
    LABEL_INCONSISTENT = "label_inconsistent"
    OUTPUT_REFUSED = "output_refused"
    OUTPUT_EXISTS = "output_exists"


class BuildError(ValueError):
    """The corpus cannot be built (or written) as specified. Nothing is returned or written."""

    def __init__(self, code: BuildErrorCode, where: str, detail: str = "") -> None:
        message = f"OC-1 corpus build refused: {code.value} at {where}"
        super().__init__(f"{message}: {detail}" if detail else message)
        self.code = code
        self.where = where


# -- source reading ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _Row:
    epoch: int
    text: tuple[str, str, str, str, str]  # open, high, low, close, volume as recorded
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-finite JSON constant {name}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_manifest(path: Path) -> dict[str, _ManifestEntry]:
    where = os.fspath(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise BuildError(BuildErrorCode.MANIFEST_INVALID, where, "must be a regular file")
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError:
        raise BuildError(BuildErrorCode.MANIFEST_MISSING, where) from None
    except OSError as error:
        raise BuildError(BuildErrorCode.MANIFEST_INVALID, where, type(error).__name__) from None
    if len(data) > MAX_MANIFEST_BYTES:
        raise BuildError(BuildErrorCode.MANIFEST_INVALID, where, f"over {MAX_MANIFEST_BYTES} bytes")
    try:
        document = json.loads(data.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise BuildError(BuildErrorCode.MANIFEST_INVALID, where, "not strict UTF-8 JSON") from None
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(files, list):
        raise BuildError(BuildErrorCode.MANIFEST_INVALID, where, "expected a 'files' list")
    entries: dict[str, _ManifestEntry] = {}
    for index, item in enumerate(files):
        at = f"{where}.files[{index}]"
        if not isinstance(item, dict):
            raise BuildError(BuildErrorCode.MANIFEST_INVALID, at, "expected an object")
        name, sha, size = item.get("name"), item.get("sha256"), item.get("size_bytes")
        if type(name) is not str or _ZIP_NAME.fullmatch(name) is None:
            raise BuildError(BuildErrorCode.MANIFEST_INVALID, at, "name is not a Kraken OHLCVT quarter zip")
        if type(sha) is not str or _SHA256.fullmatch(sha) is None:
            raise BuildError(BuildErrorCode.MANIFEST_INVALID, at, "sha256 must be 64 lowercase hex characters")
        if type(size) is not int or size < 0:
            raise BuildError(BuildErrorCode.MANIFEST_INVALID, at, "size_bytes must be an integer >= 0")
        if name in entries:
            raise BuildError(BuildErrorCode.MANIFEST_INVALID, at, "duplicate zip name")
        entries[name] = _ManifestEntry(sha256=sha, size_bytes=size)
    return entries


def _zip_order(name: str) -> tuple[int, int]:
    match = _ZIP_NAME.fullmatch(name)
    assert match is not None
    return int(match.group(2)), int(match.group(1))


def _candidate_zips(root: Path) -> list[Path]:
    """Regular ``Kraken_OHLCVT_Q[1-4]_20YY.zip`` files directly in ``root`` (no symlink)."""
    found: list[Path] = []
    try:
        with os.scandir(root) as iterator:
            for entry in iterator:
                if _ZIP_NAME.fullmatch(entry.name) and not entry.is_symlink() and entry.is_file(follow_symlinks=False):
                    found.append(Path(root, entry.name))
    except OSError as error:
        raise BuildError(BuildErrorCode.SOURCE_DIR_INVALID, os.fspath(root), type(error).__name__) from None
    return sorted(found, key=lambda path: _zip_order(path.name))


def _sha256_of(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    while chunk := handle.read(HASH_CHUNK_BYTES):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


def _verified(handle: BinaryIO, path: Path, expected: _ManifestEntry) -> str:
    """Size and sha256 of an open zip against the manifest, before any member is read."""
    where = os.fspath(path)
    size = os.fstat(handle.fileno()).st_size
    if size != expected.size_bytes:
        raise BuildError(BuildErrorCode.ZIP_SIZE_MISMATCH, where, f"{size} bytes, manifest {expected.size_bytes}")
    actual = _sha256_of(handle)
    if actual != expected.sha256:
        raise BuildError(BuildErrorCode.ZIP_HASH_MISMATCH, where, f"sha256 {actual}, manifest {expected.sha256}")
    return actual


def _pair_members(archive: zipfile.ZipFile, where: str) -> dict[str, zipfile.ZipInfo]:
    """The 5-minute CSV member of each universe pair; names are matched exactly, never as paths."""
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise BuildError(BuildErrorCode.TOO_MANY_MEMBERS, where, f"{len(infos)} entries")
    wanted = {f"{pair}_5.csv": pair for pair in PAIRS}
    found: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        pair = wanted.get(info.filename)
        if pair is None or _MEMBER_NAME.fullmatch(info.filename) is None:
            continue  # never opened: other pairs, other intervals, directories, odd or path-like names
        if pair in found:
            raise BuildError(BuildErrorCode.MEMBER_DUPLICATE, f"{where}!{info.filename}")
        found[pair] = info
    return found


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, where: str) -> bytes:
    at = f"{where}!{info.filename}"
    if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
        raise BuildError(BuildErrorCode.MEMBER_TOO_LARGE, at, f"declared {info.file_size} bytes")
    try:
        with archive.open(info, "r") as member:
            data = member.read(MAX_MEMBER_BYTES + 1)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as error:
        raise BuildError(BuildErrorCode.ZIP_INVALID, at, type(error).__name__) from None
    if len(data) > MAX_MEMBER_BYTES:
        raise BuildError(BuildErrorCode.MEMBER_TOO_LARGE, at, f"over {MAX_MEMBER_BYTES} bytes uncompressed")
    return data


def _decimal(text: str, at: str) -> Decimal:
    if _DECIMAL_TEXT.fullmatch(text) is None:
        raise BuildError(BuildErrorCode.MEMBER_MALFORMED, at, "not a plain decimal")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise BuildError(BuildErrorCode.MEMBER_MALFORMED, at, "not a decimal") from None


def _parse_rows(data: bytes, at: str) -> dict[int, _Row]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise BuildError(BuildErrorCode.MEMBER_MALFORMED, at, "not ASCII") from None
    rows: dict[int, _Row] = {}
    previous = -1
    for number, fields in enumerate(csv.reader(io.StringIO(text, newline="")), start=1):
        where = f"{at}:{number}"
        if len(fields) != 7:
            raise BuildError(BuildErrorCode.MEMBER_MALFORMED, where, "expected 7 fields")
        epoch_text, open_text, high_text, low_text, close_text, volume_text, trades_text = fields
        if _EPOCH_TEXT.fullmatch(epoch_text) is None or _COUNT_TEXT.fullmatch(trades_text) is None:
            raise BuildError(BuildErrorCode.MEMBER_MALFORMED, where, "timestamp and trade count must be integers")
        epoch = int(epoch_text)
        if epoch % BAR_SECONDS or epoch <= previous:
            raise BuildError(BuildErrorCode.MEMBER_MALFORMED, where, "off the 5-minute grid or not increasing")
        previous = epoch
        rows[epoch] = _Row(
            epoch=epoch,
            text=(open_text, high_text, low_text, close_text, volume_text),
            open=_decimal(open_text, where),
            high=_decimal(high_text, where),
            low=_decimal(low_text, where),
            close=_decimal(close_text, where),
            volume=_decimal(volume_text, where),
        )
    return rows


# -- rules ---------------------------------------------------------------------------------------


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def _iso(epoch: int) -> str:
    return _utc(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")


def _base(pair: str) -> str:
    return pair[: -len(QUOTE)]


def _instrument(pair: str) -> InstrumentId:
    base = _base(pair)
    return InstrumentId(venue=VENUE, symbol=pair, kind=InstrumentKind.SPOT, base=base, quote=QUOTE, size_unit=base)


def _bar(row: _Row, *, epoch: int | None = None, high: Decimal | None = None, close: Decimal | None = None) -> Bar:
    return Bar(
        open_time=_utc(row.epoch if epoch is None else epoch),
        open=row.open,
        high=row.high if high is None else high,
        low=row.low,
        close=row.close if close is None else close,
        volume=row.volume,
    )


def _integrity(pair: str, bars: Sequence[Bar], decision: int, price_unit: str = QUOTE) -> CapabilityResult:
    """``integrity.evaluate_ohlc`` at the decision instant with the full window required."""
    instrument = _instrument(pair)
    series = OhlcSeries(
        instrument=instrument,
        interval=timedelta(seconds=BAR_SECONDS),
        bars=tuple(bars),
        price_unit=price_unit,
        volume_unit=instrument.size_unit,
        timing=SourceTiming(received_at=_utc(decision)),
    )
    return evaluate_ohlc(series, instrument, _utc(decision), OC1_POLICY, required_bars=WINDOW_BARS)


def fee_input() -> FeeInput:
    """The fee of each leg: the repository's uncalibrated spot taker default, never env or network."""
    value = FEE_BPS_PER_LEG
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise BuildError(BuildErrorCode.FEE_UNAVAILABLE, FEE_ORIGIN_KEY, "no fee rate in the repository")
    return FeeInput(bps=value, basis=FeeBasis.UNCALIBRATED_ASSUMPTION, source=FEE_SOURCE)


def cost_lower_bound(pair: str) -> CostLowerBound:
    """Known minimum round-trip cost of a spot pair whose book was not observed (costs.py, D64)."""
    fee = fee_input()
    not_observed = Missing(MissingReason.NOT_OBSERVED, "OHLCVT bars carry no order book")
    scenario = CostScenarioInput(
        instrument=CostInstrument(kind=InstrumentKind.SPOT, symbol=pair, quote_currency=QUOTE),
        side=Side.LONG,
        size=ScenarioSize(notional=Decimal(1), provenance=SizeProvenance.HYPOTHETICAL_MANUAL),
        spread_convention=SpreadConvention.HALF_SPREAD_PLUS_TOUCH_SLIPPAGE,
        spread=not_observed,
        buy_slippage=not_observed,
        sell_slippage=not_observed,
        entry_fee=fee,
        exit_fee=fee,
        funding_intervals=0,
        funding=None,
    )
    try:
        return round_trip_cost_lower_bound(scenario)
    except (CostBoundError, CostInputError) as error:
        raise BuildError(BuildErrorCode.FEE_UNAVAILABLE, FEE_ORIGIN_KEY, str(error)) from None


def visible_edge_below_bound(rows: Sequence[_Row], bound: CostLowerBound) -> bool:
    """No edge: ``(max high - min low) / last close < bound``, exact (both sides times last close)."""
    spread = max(row.high for row in rows) - min(row.low for row in rows)
    return spread < bound.lower_bound_fraction * rows[-1].close


def visible_edge_bps(rows: Sequence[_Row]) -> Decimal:
    """The visible gross edge in bps, rounded up to 4 decimals (presentation only)."""
    spread = max(row.high for row in rows) - min(row.low for row in rows)
    return (spread * Decimal(10_000) / rows[-1].close).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)


def _move(start: Decimal, end: Decimal) -> Decimal:
    return end / start - 1


def _positive(rows: Sequence[_Row], bound: Decimal) -> bool:
    whole = _move(rows[0].open, rows[-1].close)
    hour = _move(rows[11].close, rows[-1].close)
    return abs(whole) >= POSITIVE_MULTIPLE * bound and abs(hour) >= bound and (whole > 0) == (hour > 0)


def _conflicting(rows: Sequence[_Row], bound: Decimal) -> bool:
    whole = _move(rows[0].open, rows[-1].close)
    hour = _move(rows[11].close, rows[-1].close)
    return abs(whole) >= bound and abs(hour) >= bound and (whole > 0) != (hour > 0)


# -- candidates (pass 1) -------------------------------------------------------------------------

_CLEAN = 1
_NO_EDGE = 2
_POSITIVE = 4
_CONFLICT = 8
_REAL_GAP = 16


@dataclass(frozen=True, slots=True)
class _Candidate:
    slot: int  # epoch of the slot start
    pair: str
    zip_name: str
    flags: int


def _window_epochs(slot: int) -> list[int]:
    return [slot + index * BAR_SECONDS for index in range(WINDOW_BARS)]


def _classify(pair: str, zip_name: str, rows: Mapping[int, _Row], bound: CostLowerBound) -> list[_Candidate]:
    if not rows:
        return []
    first, last = min(rows), max(rows)
    fraction = bound.lower_bound_fraction
    candidates: list[_Candidate] = []
    for slot in range(first - first % SLOT_SECONDS, last + 1, SLOT_SECONDS):
        epochs = _window_epochs(slot)
        present = [rows[epoch] for epoch in epochs if epoch in rows]
        decision = epochs[-1] + BAR_SECONDS
        if len(present) == WINDOW_BARS:
            result = _integrity(pair, [_bar(row) for row in present], decision)
            if result.status is not CheckStatus.PASS or not _fits(pair, present, decision, WINDOW_BARS, bound):
                continue
            flags = _CLEAN
            if visible_edge_below_bound(present, bound):
                flags |= _NO_EDGE
            elif _positive(present, fraction):
                flags |= _POSITIVE
            elif _conflicting(present, fraction):
                flags |= _CONFLICT
            candidates.append(_Candidate(slot, pair, zip_name, flags))
        elif len(present) >= MIN_GAP_WINDOW_BARS and epochs[-1] in rows:
            result = _integrity(pair, [_bar(row) for row in present], decision)
            if result.status is CheckStatus.UNKNOWN and _fits(pair, present, decision, len(present), bound):
                candidates.append(_Candidate(slot, pair, zip_name, _REAL_GAP))
    return candidates


# -- prompts -------------------------------------------------------------------------------------

# Every mutation changes the user text by at most this many UTF-8 bytes (a price currency of the
# same length, a timestamp of the same length, a shorter value): pass 1 keeps this margin.
_PROMPT_MARGIN_BYTES = 32


def _render_user(
    pair: str,
    first_epoch: int,
    decision: int,
    lines: Sequence[tuple[str, str, str, str, str]],
    price_unit: str,
    bound: CostLowerBound,
) -> str:
    base = _base(pair)
    fee = bound.fees[0]
    header = (
        f"Pair: {base}/{QUOTE} (Kraken spot). Decision instant: {_iso(decision)}.\n"
        f"{EVIDENCE_BARS}: 5-minute bars of {_utc(first_epoch).strftime('%Y-%m-%d')} UTC, prices in "
        f"{price_unit}; columns open_time,open,high,low,close:\n"
    )
    body = "\n".join(",".join(line) for line in lines)
    cost = (
        f"\n{EVIDENCE_COST}: known minimum round-trip cost {_plain(bound.lower_bound_bps)} bps "
        f"(taker fee {fee.bps} bps per leg, uncalibrated assumption); spread and slippage not observed."
    )
    return header + body + cost


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def _lines(rows: Sequence[_Row]) -> list[tuple[str, str, str, str, str]]:
    return [(_utc(row.epoch).strftime("%H:%M"), *row.text[:4]) for row in rows]  # volume is not shown


def _token_bound(system: str, user: str) -> int:
    probe = BenchmarkCase(
        case_id="probe",
        partition=CorpusPartition.DEVELOPMENT,
        category=CaseCategory.INSUFFICIENT_EVIDENCE,
        role=Role.SCREENER,
        system=system,
        user=user,
        evidence_ids=(EVIDENCE_BARS, EVIDENCE_COST),
        gold=gold_unavailable(),
        case_sha256="0" * 64,
    )
    return prompt_token_bound(probe)


def input_budget_tokens() -> int:
    """The OC-1 Screener input cap (2,800 tokens by the conservative UTF-8 byte bound)."""
    return OC1_ROLE_PROFILES[Role.SCREENER].max_input_tokens


def _fits(pair: str, rows: Sequence[_Row], decision: int, shown: int, bound: CostLowerBound) -> bool:
    shown_rows = rows[-shown:]
    user = _render_user(pair, shown_rows[0].epoch, decision, _lines(shown_rows), QUOTE, bound)
    return _token_bound(SYSTEM_PROMPT, user) + _PROMPT_MARGIN_BYTES <= input_budget_tokens()


# -- selection -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Pick:
    candidate: _Candidate
    category: CaseCategory
    short_window: bool = False


def _key(category: CaseCategory, candidate: _Candidate, variant: str = "") -> str:
    text = f"{SEED}|{RULES_VERSION}|{category.value}{variant}|{candidate.pair}|{candidate.slot}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pools(candidates: Iterable[_Candidate], category: CaseCategory) -> list[tuple[_Candidate, bool]]:
    """Candidates for ``category`` in selection order: (candidate, short_window)."""
    items = list(candidates)

    def ordered(flag: int, variant: str = "") -> list[_Candidate]:
        chosen = [item for item in items if item.flags & flag]
        return sorted(chosen, key=lambda item: _key(category, item, variant))

    if category is CaseCategory.ADMISSIBLE_NO_EDGE:
        return [(item, False) for item in ordered(_NO_EDGE)]
    if category is CaseCategory.ADMISSIBLE_POSITIVE:
        return [(item, False) for item in ordered(_POSITIVE)]
    if category is CaseCategory.CONFLICTING_EVIDENCE:
        return [(item, False) for item in ordered(_CONFLICT)]
    if category is CaseCategory.INSUFFICIENT_EVIDENCE:
        real = [(item, False) for item in ordered(_REAL_GAP)]
        return real + [(item, True) for item in ordered(_CLEAN, ":short")]
    return [(item, False) for item in ordered(_CLEAN)]


def _select(
    candidates: Sequence[_Candidate], partition: CorpusPartition, per_category: int, used: set[int]
) -> list[_Pick]:
    picks: list[_Pick] = []
    for category in CATEGORY_ORDER:
        taken = 0
        for candidate, short in _pools(candidates, category):
            if taken == per_category:
                break
            if candidate.slot in used:
                continue
            used.add(candidate.slot)
            picks.append(_Pick(candidate, category, short))
            taken += 1
        if taken < per_category:
            raise BuildError(
                BuildErrorCode.COUNT_NOT_REACHED,
                f"{partition.value}/{category.value}",
                f"{taken} of {per_category} cases from the recorded data",
            )
    return picks


# -- cases (pass 2) ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuiltCorpus:
    """A built corpus in memory: the lock document and each case document by partition."""

    lock: Mapping[str, object]
    cases: Mapping[CorpusPartition, Sequence[Mapping[str, object]]]
    lock_sha256: str
    ignored_zips: tuple[str, ...]
    listed_zips_absent: tuple[str, ...]
    pool_sizes: Mapping[str, int] = field(default_factory=dict)


def _fee_record(bound: CostLowerBound) -> dict[str, object]:
    fee = bound.fees[0]
    return {
        "bps_per_leg": str(fee.bps),
        "legs": 2,
        "basis": fee.basis.value,
        "origin_file": FEE_ORIGIN_FILE,
        "origin_key": FEE_ORIGIN_KEY,
    }


def _bound_record(bound: CostLowerBound) -> dict[str, object]:
    return {
        "kind": bound.kind.value,
        "status": bound.status.value,
        "bps": _plain(bound.lower_bound_bps),
        "fee_lines": [
            {"leg": line.leg.value if line.leg else None, "bps": _plain(line.bps), "source": line.source}
            for line in bound.fee_lines
        ],
        "missing": [
            {"component": line.component.value, "leg": line.leg.value if line.leg else None, "reason": line.reason.value}
            for line in bound.missing
        ],
        "function": "radar_v08.domain.costs.round_trip_cost_lower_bound",
    }


def _integrity_record(result: CapabilityResult) -> dict[str, object]:
    return {
        "policy": OC1_POLICY.version,
        "function": "radar_v08.domain.integrity.evaluate_ohlc",
        "required_bars": WINDOW_BARS,
        "status": result.status.value,
        "reasons": sorted({reason.code.value for reason in result.reasons}),
    }


def _mutate(
    pick_index: int, candidate: _Candidate, rows: list[_Row]
) -> tuple[list[tuple[str, str, str, str, str]], list[Bar], int, str, dict[str, object]]:
    """Apply the rotation's mutation to a clean window: (prompt lines, bars, decision, unit, record)."""
    mutation = MUTATION_ROTATION[pick_index % len(MUTATION_ROTATION)]
    index = 1 + int(_key(CaseCategory.INVALID_OR_STALE, candidate, ":bar")[:8], 16) % (WINDOW_BARS - 1)
    lines = _lines(rows)
    bars = [_bar(row) for row in rows]
    decision = rows[-1].epoch + BAR_SECONDS
    unit = QUOTE
    row = rows[index]
    record: dict[str, object] = {"kind": mutation.value}
    if mutation is Mutation.STALE_TIMESTAMP:
        record.update(bar_index=None, field="decision_at_utc", original=_iso(decision))
        decision += STALE_DELAY_SECONDS
        record["mutated"] = _iso(decision)
    elif mutation is Mutation.MISSING_FIELD:
        record.update(bar_index=index, field="high", original=row.text[1], mutated="")
        lines[index] = (lines[index][0], lines[index][1], "", lines[index][3], lines[index][4])
        bars[index] = _bar(row, high=Decimal("NaN"))  # an absent value is not a finite number
    elif mutation is Mutation.MIXED_CURRENCY:
        record.update(bar_index=None, field="price_unit", original=QUOTE, mutated=MUTATED_CURRENCY)
        unit = MUTATED_CURRENCY
    elif mutation is Mutation.IMPOSSIBLE_VALUE:
        record.update(bar_index=index, field="close", original=row.text[3], mutated="0")
        lines[index] = (*lines[index][:4], "0")
        bars[index] = _bar(row, close=Decimal(0))
    else:
        previous = rows[index - 1].epoch
        record.update(bar_index=index, field="open_time", original=lines[index][0], mutated=lines[index - 1][0])
        lines[index] = (lines[index - 1][0], *lines[index][1:])
        bars[index] = _bar(row, epoch=previous)
    return lines, bars, decision, unit, record


def _case_document(
    pick: _Pick,
    pick_index: int,
    case_id: str,
    partition: CorpusPartition,
    rows: Mapping[int, _Row],
    zip_sha: str,
) -> dict[str, object]:
    candidate = pick.candidate
    pair = candidate.pair
    bound = cost_lower_bound(pair)
    epochs = _window_epochs(candidate.slot)
    window = [rows[epoch] for epoch in epochs if epoch in rows]
    decision = epochs[-1] + BAR_SECONDS
    category = pick.category
    unit = QUOTE
    lines = _lines(window)
    gold: dict[str, object] | None = None
    gold_status = GOLD_STATUS_UNAVAILABLE
    details: dict[str, object]
    if category is CaseCategory.INVALID_OR_STALE:
        if len(window) != WINDOW_BARS:
            raise BuildError(BuildErrorCode.LABEL_INCONSISTENT, case_id, "invalid cases start from a clean window")
        lines, bars, decision, unit, mutation = _mutate(pick_index, candidate, window)
        result = _integrity(pair, bars, decision, unit)
        if result.status is not CheckStatus.FAIL:
            raise BuildError(BuildErrorCode.LABEL_INCONSISTENT, case_id, f"integrity is {result.status.value}, not FAIL")
        rule = f"mutation:{mutation['kind']}"
        details = {"mutation": mutation, "integrity": _integrity_record(result)}
        gold_rule = "integrity-fail:reject-input"
    elif category is CaseCategory.INSUFFICIENT_EVIDENCE:
        if pick.short_window:
            window = window[-SHORT_WINDOW_BARS:]
            lines = _lines(window)
        result = _integrity(pair, [_bar(row) for row in window], decision)
        if result.status is not CheckStatus.UNKNOWN:
            raise BuildError(BuildErrorCode.LABEL_INCONSISTENT, case_id, f"integrity is {result.status.value}, not UNKNOWN")
        kind = "short_window" if pick.short_window else "real_gap"
        rule = f"coverage:{kind}"
        details = {
            "coverage": {"kind": kind, "bars_shown": len(window), "bars_required": WINDOW_BARS},
            "integrity": _integrity_record(result),
        }
        gold_rule = "integrity-unknown:abstain"
    elif category is CaseCategory.ADMISSIBLE_NO_EDGE:
        if len(window) != WINDOW_BARS or not visible_edge_below_bound(window, bound):
            raise BuildError(BuildErrorCode.LABEL_INCONSISTENT, case_id, "visible edge is not below the cost bound")
        rule = "no-edge:visible-edge-below-cost-lower-bound"
        details = {
            "visible_gross_edge": {"definition": VISIBLE_EDGE_DEFINITION, "bps_rounded_up": str(visible_edge_bps(window))},
            "cost_lower_bound": _bound_record(bound),
            "comparison": "visible gross edge < cost lower bound (exact Decimal)",
        }
        gold_rule = "no-edge:visible-edge-below-cost-lower-bound"
    else:
        positive = category is CaseCategory.ADMISSIBLE_POSITIVE
        rule = "selection:positive-move" if positive else "selection:conflicting-moves"
        details = {
            "selection": POSITIVE_RULE if positive else CONFLICT_RULE,
            "cost_lower_bound_bps": _plain(bound.lower_bound_bps),
            "gold_unavailable_reason": "needs two independent human reviews (OC-1 section 6)",
        }
        gold_rule = ""
    if category in (CaseCategory.INVALID_OR_STALE, CaseCategory.INSUFFICIENT_EVIDENCE, CaseCategory.ADMISSIBLE_NO_EDGE):
        gold = {
            "source": GoldSource.DETERMINISTIC_RULE.value,
            "rule": gold_rule,
            "abstain_expected": ABSTAIN_BY_CATEGORY[category],
            "human_review": None,
        }
        gold_status = GOLD_STATUS_DETERMINISTIC
    user = _render_user(pair, window[0].epoch, decision, lines, unit, bound)
    if _token_bound(SYSTEM_PROMPT, user) > input_budget_tokens():
        raise BuildError(BuildErrorCode.PROMPT_OVER_BUDGET, case_id, "over the Screener input cap")
    return {
        "schema_version": CORPUS_SCHEMA_VERSION_V2,
        "case_id": case_id,
        "partition": partition.value,
        "category": category.value,
        "role": Role.SCREENER.value,
        "synthetic": False,
        "prompt": {"system": SYSTEM_PROMPT, "user": user},
        "evidence_ids": [EVIDENCE_BARS, EVIDENCE_COST],
        "gold": gold,
        "gold_status": gold_status,
        "construction": {"rule": rule, "rules_version": RULES_VERSION, "details": details},
        "provenance": {
            "venue": VENUE,
            "pair": pair,
            "base": _base(pair),
            "quote": QUOTE,
            "bar_seconds": BAR_SECONDS,
            "window_start_utc": _iso(window[0].epoch),
            "window_end_utc": _iso(window[-1].epoch + BAR_SECONDS),
            "decision_at_utc": _iso(decision),
            "source": {"zip": candidate.zip_name, "zip_sha256": zip_sha, "member": f"{pair}_5.csv"},
            "fee": _fee_record(bound),
        },
    }


def _episode_end(document: Mapping[str, object]) -> str:
    provenance = document["provenance"]
    assert isinstance(provenance, dict)
    value = provenance["decision_at_utc"]
    assert isinstance(value, str)
    return value


def _episode_start(document: Mapping[str, object]) -> str:
    provenance = document["provenance"]
    assert isinstance(provenance, dict)
    value = provenance["window_start_utc"]
    assert isinstance(value, str)
    return value


def _parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


# -- build ---------------------------------------------------------------------------------------


def build_corpus(sextant_data: str | os.PathLike[str]) -> BuiltCorpus:
    """Build the corpus in memory from ``sextant_data`` (read only). Nothing is written."""
    where = os.fspath(sextant_data)
    try:
        root = Path(sextant_data).resolve(strict=True)
    except (OSError, RuntimeError):
        raise BuildError(BuildErrorCode.SOURCE_DIR_INVALID, where, "must be an existing directory") from None
    if not root.is_dir():
        raise BuildError(BuildErrorCode.SOURCE_DIR_INVALID, where, "must be an existing directory")
    bound = cost_lower_bound(PAIRS[0])  # fails first when the fee is unavailable
    manifest = _read_manifest(root / "kraken-archive" / "manifest.json")
    present = _candidate_zips(root)
    listed = [path for path in present if path.name in manifest]
    ignored = tuple(sorted(path.name for path in present if path.name not in manifest))
    absent = tuple(sorted(set(manifest) - {path.name for path in present}))
    if not listed:
        raise BuildError(BuildErrorCode.NO_LISTED_ZIP, where, "no zip of the manifest is present")

    hashes: dict[str, str] = {}
    candidates: list[_Candidate] = []
    for path in listed:
        with open(path, "rb") as handle:
            hashes[path.name] = _verified(handle, path, manifest[path.name])
            try:
                with zipfile.ZipFile(handle) as archive:
                    members = _pair_members(archive, os.fspath(path))
                    for pair in PAIRS:
                        if pair in members:
                            rows = _parse_rows(_read_member(archive, members[pair], os.fspath(path)), f"{path.name}!{pair}")
                            candidates.extend(_classify(pair, path.name, rows, bound))
            except zipfile.BadZipFile as error:
                raise BuildError(BuildErrorCode.ZIP_INVALID, os.fspath(path), str(error)) from None

    candidates.sort(key=lambda item: (item.slot, item.pair))
    if not candidates:
        raise BuildError(BuildErrorCode.COUNT_NOT_REACHED, where, "no candidate window")
    first = candidates[0].slot
    last = candidates[-1].slot + SLOT_SECONDS
    usable = last - first - MIN_PARTITION_GAP_SECONDS
    if usable <= 0:
        raise BuildError(BuildErrorCode.SEPARATION_TOO_SHORT, where, "the data spans less than the 7-day gap")
    share, parts = DEVELOPMENT_SHARE
    development_end = first + (usable * share // parts) // SLOT_SECONDS * SLOT_SECONDS
    holdout_start = development_end + MIN_PARTITION_GAP_SECONDS
    development_pool = [item for item in candidates if item.slot + SLOT_SECONDS <= development_end]
    holdout_pool = [item for item in candidates if item.slot >= holdout_start]
    pool_sizes: dict[str, int] = {}
    for partition, pool in ((CorpusPartition.DEVELOPMENT, development_pool), (CorpusPartition.HOLDOUT, holdout_pool)):
        for category in CATEGORY_ORDER:
            pool_sizes[f"{partition.value}/{category.value}"] = len(_pools(pool, category))
    used: set[int] = set()
    picks = {
        CorpusPartition.DEVELOPMENT: _select(development_pool, CorpusPartition.DEVELOPMENT, DEVELOPMENT_PER_CATEGORY, used),
        CorpusPartition.HOLDOUT: _select(holdout_pool, CorpusPartition.HOLDOUT, HOLDOUT_PER_CATEGORY, used),
    }

    # Pass 2: re-verify each zip, read only the members of the picked windows, build the cases.
    needed: dict[str, set[str]] = {}
    for partition_picks in picks.values():
        for pick in partition_picks:
            needed.setdefault(pick.candidate.zip_name, set()).add(pick.candidate.pair)
    member_rows: dict[tuple[str, str], dict[int, _Row]] = {}
    for path in listed:
        if path.name not in needed:
            continue
        with open(path, "rb") as handle:
            again = _verified(handle, path, manifest[path.name])
            if again != hashes[path.name]:
                raise BuildError(BuildErrorCode.ZIP_CHANGED, os.fspath(path))
            try:
                with zipfile.ZipFile(handle) as archive:
                    members = _pair_members(archive, os.fspath(path))
                    for pair in sorted(needed[path.name]):
                        data = _read_member(archive, members[pair], os.fspath(path))
                        member_rows[(path.name, pair)] = _parse_rows(data, f"{path.name}!{pair}")
            except zipfile.BadZipFile as error:
                raise BuildError(BuildErrorCode.ZIP_INVALID, os.fspath(path), str(error)) from None

    documents: dict[CorpusPartition, list[dict[str, object]]] = {}
    for partition, partition_picks in picks.items():
        built: list[dict[str, object]] = []
        for category in CATEGORY_ORDER:
            chosen = [pick for pick in partition_picks if pick.category is category]
            rotation = list(chosen)  # the mutation rotation follows the selection order
            chosen.sort(key=lambda pick: (pick.candidate.slot, pick.candidate.pair))
            for number, pick in enumerate(chosen, start=1):
                case_id = f"{PARTITION_PREFIX[partition]}-{CASE_SLUGS[category]}-{number:03d}"
                rows = member_rows[(pick.candidate.zip_name, pick.candidate.pair)]
                built.append(
                    _case_document(pick, rotation.index(pick), case_id, partition, rows, hashes[pick.candidate.zip_name])
                )
        documents[partition] = sorted(built, key=lambda document: str(document["case_id"]))

    development_last = max(_parse_iso(_episode_end(item)) for item in documents[CorpusPartition.DEVELOPMENT])
    holdout_first = min(_parse_iso(_episode_start(item)) for item in documents[CorpusPartition.HOLDOUT])
    gap = int((holdout_first - development_last).total_seconds())
    if gap < MIN_PARTITION_GAP_SECONDS:
        raise BuildError(BuildErrorCode.SEPARATION_TOO_SHORT, where, f"{gap} s between the partitions")
    partitions: dict[str, object] = {}
    for partition, items in documents.items():
        table = {
            str(item["case_id"]): {
                "sha256": canonical_sha256(item),
                "prompt_sha256": _fingerprint(item),
            }
            for item in items
        }
        partitions[partition.value] = {"partition_sha256": canonical_sha256(table), "cases": table}
    lock: dict[str, object] = {
        "schema_version": CORPUS_SCHEMA_VERSION_V2,
        "corpus_id": CORPUS_ID,
        "synthetic": False,
        "partitions": partitions,
        "separation": {
            "development_end_utc": development_last.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "holdout_start_utc": holdout_first.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "gap_seconds": gap,
            "min_gap_seconds": MIN_PARTITION_GAP_SECONDS,
        },
        "builder": {
            "rules_version": RULES_VERSION,
            "seed": SEED,
            "source_zips": [{"zip": name, "sha256": hashes[name]} for name in sorted(hashes, key=_zip_order)],
        },
    }
    return BuiltCorpus(
        lock=lock,
        cases={partition: tuple(items) for partition, items in documents.items()},
        lock_sha256=canonical_sha256(lock),
        ignored_zips=ignored,
        listed_zips_absent=absent,
        pool_sizes=pool_sizes,
    )


def _fingerprint(document: Mapping[str, object]) -> str:
    prompt = document["prompt"]
    evidence = document["evidence_ids"]
    assert isinstance(prompt, dict) and isinstance(evidence, list)
    return prompt_fingerprint(str(prompt["system"]), str(prompt["user"]), [str(item) for item in evidence])


# -- output --------------------------------------------------------------------------------------


def _same_or_inside(path: Path, parent: Path) -> bool:
    child = os.path.normcase(os.fspath(path))
    base = os.path.normcase(os.fspath(parent))
    return child == base or child.startswith(base.rstrip("\\/") + os.sep)


def check_output_dir(out: str | os.PathLike[str], sextant_data: str | os.PathLike[str]) -> Path:
    """Refuse an output directory at the repository root (or above it), in the synthetic fixture
    corpus, inside the Sextant (the data directory given or the Sextant root), inside ``.git``, or
    that exists and is not empty. Returns the resolved directory; nothing is created here."""
    where = os.fspath(out)
    target = Path(out).resolve(strict=False)
    data = Path(sextant_data).resolve(strict=False)
    if _same_or_inside(REPOSITORY_ROOT, target):
        raise BuildError(BuildErrorCode.OUTPUT_REFUSED, where, "the repository root or one of its parents")
    for refused, reason in (
        (SYNTHETIC_FIXTURE_CORPUS, "the synthetic fixture corpus"),
        (REPOSITORY_ROOT / ".git", "the git directory"),
        (data, "the Sextant data directory"),
        (SEXTANT_ROOT, "the Sextant"),
    ):
        if _same_or_inside(target, refused):
            raise BuildError(BuildErrorCode.OUTPUT_REFUSED, where, f"inside {reason}")
    if _same_or_inside(data, target):
        raise BuildError(BuildErrorCode.OUTPUT_REFUSED, where, "contains the Sextant data directory")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise BuildError(BuildErrorCode.OUTPUT_EXISTS, where, "must not exist or be an empty directory")
    return target


def _json_bytes(document: Mapping[str, object]) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def write_corpus(corpus: BuiltCorpus, out: str | os.PathLike[str], sextant_data: str | os.PathLike[str]) -> Path:
    """Write ``lock.json``, ``development/`` and ``holdout/`` into ``out`` (checked first)."""
    target = check_output_dir(out, sextant_data)
    target.mkdir(parents=True, exist_ok=True)
    for partition, items in corpus.cases.items():
        directory = target / partition.value
        directory.mkdir()
        for item in items:
            with open(directory / f"{item['case_id']}.json", "xb") as handle:
                handle.write(_json_bytes(item))
    with open(target / LOCK_FILE_NAME, "xb") as handle:
        handle.write(_json_bytes(corpus.lock))
    return target
