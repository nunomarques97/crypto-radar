"""SQLite snapshot store: radar_state.sqlite.

Never touches TRADING_STATE.md / TRADING_HISTORY.md - this is the radar's own
technical state, separate from the trading journal.

Deltas are reset-aware: Kraken's `v[0]`/`t[0]` accumulate since 00:00 UTC, so
a naive `current - previous` produces a huge negative "spike" at the daily
reset. When current < previous we treat it as a session reset and record the
delta as the current cumulative value itself (the activity since the reset),
never as a negative number, and flag it as such.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .adapters import evidence_store
from .domain.evidence import SealedEvidence
from .domain.integrity import InstrumentId

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    asset TEXT PRIMARY KEY,
    first_seen_ts TEXT NOT NULL,
    last_seen_ts TEXT NOT NULL,
    is_stable INTEGER NOT NULL DEFAULT 0,
    is_fiat INTEGER NOT NULL DEFAULT 0,
    excluded_reason TEXT
);

CREATE TABLE IF NOT EXISTS spot_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    pair TEXT NOT NULL,
    quote TEXT NOT NULL,
    ts TEXT NOT NULL,
    last REAL,
    bid REAL,
    ask REAL,
    bid_size REAL,
    ask_size REAL,
    volume_today REAL,
    volume_24h REAL,
    vwap_today REAL,
    vwap_24h REAL,
    trades_today INTEGER,
    trades_24h INTEGER,
    high_today REAL,
    low_today REAL,
    high_24h REAL,
    low_24h REAL,
    open_today REAL,
    status TEXT,
    delta_volume REAL,
    delta_trades INTEGER,
    session_reset INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_spot_asset_ts ON spot_snapshots(asset, ts);
CREATE INDEX IF NOT EXISTS idx_spot_pair_ts ON spot_snapshots(pair, ts);

CREATE TABLE IF NOT EXISTS futures_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    asset TEXT NOT NULL,
    ts TEXT NOT NULL,
    last REAL,
    mark_price REAL,
    index_price REAL,
    bid REAL,
    ask REAL,
    bid_size REAL,
    ask_size REAL,
    volume_quote REAL,
    open_interest REAL,
    funding_rate_raw REAL,
    funding_prediction_raw REAL,
    open_24h REAL,
    last_time TEXT,
    suspended INTEGER,
    post_only INTEGER,
    tag TEXT,
    oi_delta REAL,
    session_reset INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fut_symbol_ts ON futures_snapshots(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_fut_asset_ts ON futures_snapshots(asset, ts);

CREATE TABLE IF NOT EXISTS radar_runs (
    run_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    markets_seen INTEGER,
    assets_eligible INTEGER,
    assets_tradeable INTEGER,
    futures_perpetuals INTEGER,
    snapshot_count INTEGER,
    shortlist_count INTEGER,
    warmup INTEGER,
    latency_ms REAL,
    api_failures INTEGER,
    data_quality_json TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    ts TEXT NOT NULL,
    anomaly_score REAL,
    warmup INTEGER,
    flags_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_asset_ts ON alerts(asset, ts);

CREATE TABLE IF NOT EXISTS forward_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    ts TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL,
    return_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_fwd_asset_ts ON forward_returns(asset, ts);

CREATE TABLE IF NOT EXISTS ohlc_bars (
    pair TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    bar_time TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    vwap REAL,
    volume REAL,
    trades INTEGER,
    PRIMARY KEY (pair, interval_minutes, bar_time)
);
CREATE INDEX IF NOT EXISTS idx_ohlc_pair_interval_time ON ohlc_bars(pair, interval_minutes, bar_time);

CREATE TABLE IF NOT EXISTS ohlc_cursor (
    pair TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    last_since INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (pair, interval_minutes)
);

CREATE TABLE IF NOT EXISTS l2_feature_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    ts TEXT NOT NULL,
    entry_price REAL,
    anomaly_score REAL,
    opportunity_score REAL,
    setup_type TEXT,
    direction TEXT,
    features_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_l2_asset_ts ON l2_feature_snapshots(asset, ts);

CREATE TABLE IF NOT EXISTS model_cooldowns (
    asset TEXT NOT NULL,
    model TEXT NOT NULL,
    last_sent_ts TEXT NOT NULL,
    setup_type TEXT,
    direction TEXT,
    opportunity_score REAL,
    PRIMARY KEY (asset, model)
);

CREATE TABLE IF NOT EXISTS model_budget_usage (
    model TEXT NOT NULL,
    window_kind TEXT NOT NULL,
    window_start TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (model, window_kind, window_start)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    asset TEXT NOT NULL,
    setup_type TEXT,
    direction TEXT,
    market TEXT,
    anomaly_score REAL,
    opportunity_score REAL,
    tradeability_score REAL,
    confidence TEXT,
    model_demand TEXT,
    reason TEXT,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_dedup ON events(dedup_key);
CREATE INDEX IF NOT EXISTS idx_events_asset_ts ON events(asset, ts);

-- Phase 4: one row per Claude Bridge call attempt, for audit/idempotency.
-- Never lost on terminal close - a run's model output survives the process.
CREATE TABLE IF NOT EXISTS model_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    response TEXT,
    parsed_output_json TEXT,
    latency_ms REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_analyses_event ON model_analyses(event_id);

-- Single-row table: the Claude Bridge's last-known health state, so the
-- terminal can show it even across process restarts (task section 15).
CREATE TABLE IF NOT EXISTS bridge_health (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
    detail TEXT,
    updated_ts TEXT NOT NULL
);
"""

# forward_returns started (Phase 1) as (asset, ts, horizon_minutes, return_pct).
# Phase 2 adds entry_price/mfe/mae/labeled_at via migration so an existing
# radar_state.sqlite from Phase 1 upgrades in place instead of being replaced.
_FORWARD_RETURNS_MIGRATION_COLUMNS = {
    "entry_price": "REAL",
    "mfe_pct": "REAL",
    "mae_pct": "REAL",
    "labeled_at": "TEXT",
    "pair": "TEXT",
}

# Phase 4 adds these to `events` in place, so a Phase 1-3 radar_state.sqlite
# upgrades rather than being replaced (same pattern as forward_returns above).
_EVENTS_MIGRATION_COLUMNS = {
    "context_json": "TEXT",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "last_error": "TEXT",
    "next_attempt_at": "TEXT",
    "processing_started_at": "TEXT",
    "updated_ts": "TEXT",
    "notified": "INTEGER NOT NULL DEFAULT 0",
    # Mobile push (ntfy) delivery state - independent of `notified` (Windows
    # toast) so a failed phone push can be retried across cycles without
    # ever re-sending the Windows toast or re-billing a model call.
    "ntfy_status": "TEXT",
    "ntfy_attempts": "INTEGER NOT NULL DEFAULT 0",
    "ntfy_last_error": "TEXT",
    "ntfy_updated_ts": "TEXT",
}


class StoreError(RuntimeError):
    """Fatal SQLite failure. Never swallowed - a broken store must be loud."""


def reset_aware_delta(current: float | int | None, previous: float | int | None) -> tuple[float | int | None, bool]:
    """Reset-aware delta. Returns (delta, was_reset).

    Kraken's cumulative-since-00:00-UTC counters (v[0], t[0]) drop back to a
    small number at the daily reset. A naive `current - previous` would read
    as a large negative spike; instead we treat `current` itself as the
    delta (activity accrued since the reset) and flag it as a reset.
    """
    if current is None or previous is None:
        return None, False
    if current < previous:
        return current, True
    return current - previous, False


# Backwards-compatible private alias used internally in this module.
_delta = reset_aware_delta


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SpotSnapshotInput:
    asset: str
    pair: str
    quote: str
    ts: str
    last: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    volume_today: float
    volume_24h: float
    vwap_today: float
    vwap_24h: float
    trades_today: int
    trades_24h: int
    high_today: float
    low_today: float
    high_24h: float
    low_24h: float
    open_today: float
    status: str


@dataclass
class FuturesSnapshotInput:
    symbol: str
    asset: str
    ts: str
    last: float | None
    mark_price: float | None
    index_price: float | None
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    volume_quote: float | None
    open_interest: float | None
    funding_rate_raw: float | None
    funding_prediction_raw: float | None
    open_24h: float | None
    last_time: str | None
    suspended: bool
    post_only: bool
    tag: str | None


class SnapshotStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.executescript(SCHEMA)
            self._migrate_forward_returns()
            self._migrate_events()
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"Failed to open/initialize snapshot store at {path}: {exc}") from exc
        # T030b: additive, ledgered migrations (new tables only) in one short
        # transaction. Already current -> no write at all. Any failure rolls back
        # and is raised as a typed SchemaMigrationError; the store is not usable.
        try:
            self.migrate_schema()
        except BaseException:
            self._conn.close()
            raise

    def _migrate_forward_returns(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(forward_returns)").fetchall()}
        for column, sql_type in _FORWARD_RETURNS_MIGRATION_COLUMNS.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE forward_returns ADD COLUMN {column} {sql_type}")

    def _migrate_events(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(events)").fetchall()}
        for column, sql_type in _EVENTS_MIGRATION_COLUMNS.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE events ADD COLUMN {column} {sql_type}")

    def close(self) -> None:
        self._conn.close()

    # -- schema-version ledger and sealed evidence (T030b) ---------------------

    def migrate_schema(self) -> tuple[int, ...]:
        """Apply pending ledgered migrations; returns the versions applied (empty if current)."""
        with self._lock:
            return evidence_store.apply_schema_migrations(self._conn, now=datetime.now(timezone.utc))

    def schema_ledger(self) -> tuple[evidence_store.LedgerEntry, ...]:
        with self._lock:
            return evidence_store.read_ledger(self._conn)

    def save_evidence(self, evidence: SealedEvidence) -> bool:
        with self._lock:
            return evidence_store.save_evidence(self._conn, evidence, now=datetime.now(timezone.utc))

    def load_evidence(self, evidence_id: str) -> SealedEvidence | None:
        with self._lock:
            return evidence_store.load_evidence(self._conn, evidence_id)

    def link_event_evidence(self, event_id: str, evidence_id: str, run_id: str, instrument: InstrumentId) -> bool:
        with self._lock:
            return evidence_store.link_event_evidence(
                self._conn,
                event_id=event_id,
                evidence_id=evidence_id,
                run_id=run_id,
                instrument=instrument,
                now=datetime.now(timezone.utc),
            )

    def load_event_evidence(self, event_id: str) -> evidence_store.EventEvidence:
        with self._lock:
            return evidence_store.load_event_evidence(self._conn, event_id)

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise StoreError(f"SQLite operation failed: {exc}") from exc
            finally:
                cur.close()

    # -- assets -----------------------------------------------------------

    def upsert_asset(self, asset: str, ts: str, is_stable: bool, is_fiat: bool, excluded_reason: str | None) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO assets (asset, first_seen_ts, last_seen_ts, is_stable, is_fiat, excluded_reason)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset) DO UPDATE SET
                    last_seen_ts = excluded.last_seen_ts,
                    is_stable = excluded.is_stable,
                    is_fiat = excluded.is_fiat,
                    excluded_reason = excluded.excluded_reason
                """,
                (asset, ts, ts, int(is_stable), int(is_fiat), excluded_reason),
            )

    # -- spot ---------------------------------------------------------------

    def latest_spot_snapshot(self, pair: str, before_ts: str | None = None) -> sqlite3.Row | None:
        with self._cursor() as cur:
            if before_ts is None:
                cur.execute(
                    "SELECT * FROM spot_snapshots WHERE pair = ? ORDER BY ts DESC LIMIT 1",
                    (pair,),
                )
            else:
                cur.execute(
                    "SELECT * FROM spot_snapshots WHERE pair = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                    (pair, before_ts),
                )
            return cur.fetchone()

    @staticmethod
    def _select_previous_spot(cur: sqlite3.Cursor, pair: str, before_ts: str) -> sqlite3.Row | None:
        cur.execute(
            "SELECT * FROM spot_snapshots WHERE pair = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
            (pair, before_ts),
        )
        return cur.fetchone()

    def _insert_spot_row(self, cur: sqlite3.Cursor, snap: SpotSnapshotInput) -> None:
        previous = self._select_previous_spot(cur, snap.pair, snap.ts)
        delta_volume, reset_v = _delta(snap.volume_today, previous["volume_today"] if previous else None)
        delta_trades, reset_t = _delta(snap.trades_today, previous["trades_today"] if previous else None)
        session_reset = bool(reset_v or reset_t)

        cur.execute(
            """
            INSERT INTO spot_snapshots (
                asset, pair, quote, ts, last, bid, ask, bid_size, ask_size,
                volume_today, volume_24h, vwap_today, vwap_24h,
                trades_today, trades_24h, high_today, low_today, high_24h, low_24h,
                open_today, status, delta_volume, delta_trades, session_reset
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snap.asset, snap.pair, snap.quote, snap.ts, snap.last, snap.bid, snap.ask,
                snap.bid_size, snap.ask_size, snap.volume_today, snap.volume_24h,
                snap.vwap_today, snap.vwap_24h, snap.trades_today, snap.trades_24h,
                snap.high_today, snap.low_today, snap.high_24h, snap.low_24h,
                snap.open_today, snap.status, delta_volume, delta_trades, int(session_reset),
            ),
        )

    def insert_spot_snapshot(self, snap: SpotSnapshotInput) -> None:
        self.insert_spot_snapshots_batch([snap])

    def insert_spot_snapshots_batch(self, snaps: list[SpotSnapshotInput]) -> None:
        """Insert many snapshots in a single locked transaction (one commit),
        not one transaction per row - this is the "batch SQLite inserts"
        performance requirement from the architecture doc.
        """
        if not snaps:
            return
        with self._lock:
            cur = self._conn.cursor()
            try:
                for snap in snaps:
                    self._insert_spot_row(cur, snap)
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise StoreError(f"SQLite batch insert (spot) failed: {exc}") from exc
            finally:
                cur.close()

    # -- futures --------------------------------------------------------------

    def latest_futures_snapshot(self, symbol: str, before_ts: str | None = None) -> sqlite3.Row | None:
        with self._cursor() as cur:
            if before_ts is None:
                cur.execute(
                    "SELECT * FROM futures_snapshots WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                    (symbol,),
                )
            else:
                cur.execute(
                    "SELECT * FROM futures_snapshots WHERE symbol = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                    (symbol, before_ts),
                )
            return cur.fetchone()

    @staticmethod
    def _select_previous_futures(cur: sqlite3.Cursor, symbol: str, before_ts: str) -> sqlite3.Row | None:
        cur.execute(
            "SELECT * FROM futures_snapshots WHERE symbol = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
            (symbol, before_ts),
        )
        return cur.fetchone()

    def _insert_futures_row(self, cur: sqlite3.Cursor, snap: FuturesSnapshotInput) -> None:
        previous = self._select_previous_futures(cur, snap.symbol, snap.ts)
        oi_delta = None
        session_reset = False
        if previous is not None and previous["open_interest"] is not None and snap.open_interest is not None:
            oi_delta = snap.open_interest - previous["open_interest"]

        cur.execute(
            """
            INSERT INTO futures_snapshots (
                symbol, asset, ts, last, mark_price, index_price, bid, ask,
                bid_size, ask_size, volume_quote, open_interest,
                funding_rate_raw, funding_prediction_raw, open_24h, last_time,
                suspended, post_only, tag, oi_delta, session_reset
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snap.symbol, snap.asset, snap.ts, snap.last, snap.mark_price, snap.index_price,
                snap.bid, snap.ask, snap.bid_size, snap.ask_size, snap.volume_quote,
                snap.open_interest, snap.funding_rate_raw, snap.funding_prediction_raw,
                snap.open_24h, snap.last_time, int(snap.suspended), int(snap.post_only),
                snap.tag, oi_delta, int(session_reset),
            ),
        )

    def insert_futures_snapshot(self, snap: FuturesSnapshotInput) -> None:
        self.insert_futures_snapshots_batch([snap])

    def insert_futures_snapshots_batch(self, snaps: list[FuturesSnapshotInput]) -> None:
        if not snaps:
            return
        with self._lock:
            cur = self._conn.cursor()
            try:
                for snap in snaps:
                    self._insert_futures_row(cur, snap)
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise StoreError(f"SQLite batch insert (futures) failed: {exc}") from exc
            finally:
                cur.close()

    # -- lookups ----------------------------------------------------------

    def nearest_spot_snapshot(
        self, asset: str, target_ts: str, tolerance_seconds: float
    ) -> sqlite3.Row | None:
        """Snapshot for `asset` closest to `target_ts`, within tolerance."""
        target_dt = datetime.fromisoformat(target_ts)
        low = (target_dt - timedelta(seconds=tolerance_seconds)).isoformat()
        high = (target_dt + timedelta(seconds=tolerance_seconds)).isoformat()

        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM spot_snapshots WHERE asset = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                (asset, low, high),
            )
            rows = cur.fetchall()

        if not rows:
            return None

        def diff_seconds(row: sqlite3.Row) -> float:
            return abs((datetime.fromisoformat(row["ts"]) - target_dt).total_seconds())

        return min(rows, key=diff_seconds)

    def nearest_futures_snapshot(
        self, asset: str, target_ts: str, tolerance_seconds: float
    ) -> sqlite3.Row | None:
        target_dt = datetime.fromisoformat(target_ts)
        low = (target_dt - timedelta(seconds=tolerance_seconds)).isoformat()
        high = (target_dt + timedelta(seconds=tolerance_seconds)).isoformat()

        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM futures_snapshots WHERE asset = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                (asset, low, high),
            )
            rows = cur.fetchall()

        if not rows:
            return None

        def diff_seconds(row: sqlite3.Row) -> float:
            return abs((datetime.fromisoformat(row["ts"]) - target_dt).total_seconds())

        return min(rows, key=diff_seconds)

    def nearest_spot_snapshot_by_pair(
        self, pair: str, target_ts: str, tolerance_seconds: float
    ) -> sqlite3.Row | None:
        """Like `nearest_spot_snapshot`, but scoped to one market (pair)
        rather than every quote market of an asset. Used by L1 and
        forward-return labeling so BTC/EUR history can never contaminate a
        BTC/USD entry.
        """
        target_dt = datetime.fromisoformat(target_ts)
        low = (target_dt - timedelta(seconds=tolerance_seconds)).isoformat()
        high = (target_dt + timedelta(seconds=tolerance_seconds)).isoformat()

        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM spot_snapshots WHERE pair = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                (pair, low, high),
            )
            rows = cur.fetchall()

        if not rows:
            return None

        def diff_seconds(row: sqlite3.Row) -> float:
            return abs((datetime.fromisoformat(row["ts"]) - target_dt).total_seconds())

        return min(rows, key=diff_seconds)

    def spot_snapshots_between_pair(self, pair: str, start_ts: str, end_ts: str) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM spot_snapshots WHERE pair = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                (pair, start_ts, end_ts),
            )
            return cur.fetchall()

    def spot_history_by_pair(self, pair: str, since_ts: str) -> list[sqlite3.Row]:
        """Ascending spot price history for one exact selected market pair."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM spot_snapshots WHERE pair = ? AND ts >= ? ORDER BY ts",
                (pair, since_ts),
            )
            return cur.fetchall()

    def asset_history(self, asset: str, since_ts: str) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM spot_snapshots WHERE asset = ? AND ts >= ? ORDER BY ts",
                (asset, since_ts),
            )
            return cur.fetchall()

    def futures_history(self, asset: str, since_ts: str) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM futures_snapshots WHERE asset = ? AND ts >= ? ORDER BY ts",
                (asset, since_ts),
            )
            return cur.fetchall()

    # -- runs / alerts / forward returns ------------------------------------

    def insert_run(self, run: dict[str, Any]) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO radar_runs (
                    run_id, ts, mode, markets_seen, assets_eligible, assets_tradeable,
                    futures_perpetuals, snapshot_count, shortlist_count, warmup,
                    latency_ms, api_failures, data_quality_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"], run["ts"], run["mode"], run["markets_seen"],
                    run["assets_eligible"], run["assets_tradeable"], run["futures_perpetuals"],
                    run["snapshot_count"], run["shortlist_count"], int(run["warmup"]),
                    run["latency_ms"], run["api_failures"], run["data_quality_json"],
                ),
            )

    def insert_alert(self, run_id: str, asset: str, ts: str, anomaly_score: float | None, warmup: bool, flags: list[str]) -> None:
        import json as _json

        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO alerts (run_id, asset, ts, anomaly_score, warmup, flags_json) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, asset, ts, anomaly_score, int(warmup), _json.dumps(flags)),
            )

    def insert_forward_return(self, asset: str, ts: str, horizon_minutes: int, return_pct: float | None) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO forward_returns (asset, ts, horizon_minutes, return_pct) VALUES (?, ?, ?, ?)",
                (asset, ts, horizon_minutes, return_pct),
            )

    def create_forward_return_placeholders(
        self, asset: str, pair: str, ts: str, entry_price: float, horizons_minutes: list[int]
    ) -> None:
        """One pending row per horizon, to be filled in later by
        `pending_forward_returns` / a caller's labeling pass once enough time
        (and snapshots) have actually passed. Never fabricated early.
        `pair` is stored so labeling reads price history from the single
        market this candidate was evaluated on, not every quote of the asset.
        """
        if not horizons_minutes:
            return
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.executemany(
                    "INSERT INTO forward_returns (asset, ts, horizon_minutes, return_pct, entry_price, pair) "
                    "VALUES (?, ?, ?, NULL, ?, ?)",
                    [(asset, ts, h, entry_price, pair) for h in horizons_minutes],
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise StoreError(f"SQLite forward-return placeholder insert failed: {exc}") from exc
            finally:
                cur.close()

    def pending_forward_returns(self, now: datetime, limit: int = 500) -> list[sqlite3.Row]:
        """Rows whose horizon is due (ts + horizon_minutes <= now) and that
        have not been labeled yet.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM forward_returns WHERE return_pct IS NULL ORDER BY ts LIMIT ?",
                (limit * 4,),  # over-fetch a bit; horizon-due filtering happens in Python (ts is ISO text)
            )
            rows = cur.fetchall()

        due = []
        for row in rows:
            target = datetime.fromisoformat(row["ts"]) + timedelta(minutes=row["horizon_minutes"])
            if target <= now:
                due.append(row)
            if len(due) >= limit:
                break
        return due

    def label_forward_return(
        self, row_id: int, return_pct: float, mfe_pct: float | None, mae_pct: float | None, labeled_at: str
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE forward_returns SET return_pct = ?, mfe_pct = ?, mae_pct = ?, labeled_at = ? WHERE id = ?",
                (return_pct, mfe_pct, mae_pct, labeled_at, row_id),
            )

    # -- OHLC (L2, candidate-only, incremental) ------------------------------

    def get_ohlc_cursor(self, pair: str, interval_minutes: int) -> int | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT last_since FROM ohlc_cursor WHERE pair = ? AND interval_minutes = ?",
                (pair, interval_minutes),
            )
            row = cur.fetchone()
        return int(row["last_since"]) if row else None

    def set_ohlc_cursor(self, pair: str, interval_minutes: int, last_since: int, ts: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO ohlc_cursor (pair, interval_minutes, last_since, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pair, interval_minutes) DO UPDATE SET
                    last_since = excluded.last_since,
                    updated_at = excluded.updated_at
                """,
                (pair, interval_minutes, last_since, ts),
            )

    def insert_ohlc_bars_batch(self, pair: str, interval_minutes: int, bars: list[Any]) -> None:
        """`bars` are OhlcBar-like objects (bar_time/open/high/low/close/vwap/
        volume/trades attributes). INSERT OR REPLACE so a re-fetched partial
        bar (the still-forming current bar) is refreshed, not duplicated.
        """
        if not bars:
            return
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO ohlc_bars (
                        pair, interval_minutes, bar_time, open, high, low, close, vwap, volume, trades
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (pair, interval_minutes, b.bar_time, b.open, b.high, b.low, b.close, b.vwap, b.volume, b.trades)
                        for b in bars
                    ],
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise StoreError(f"SQLite OHLC batch insert failed: {exc}") from exc
            finally:
                cur.close()

    def get_ohlc_window(self, pair: str, interval_minutes: int, limit: int) -> list[sqlite3.Row]:
        """Most recent `limit` bars, ascending by time (oldest first)."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM (
                    SELECT * FROM ohlc_bars WHERE pair = ? AND interval_minutes = ?
                    ORDER BY bar_time DESC LIMIT ?
                ) ORDER BY bar_time ASC
                """,
                (pair, interval_minutes, limit),
            )
            return cur.fetchall()

    def ohlc_bars_between(self, pair: str, interval_minutes: int, start_ts: str, end_ts: str) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM ohlc_bars WHERE pair = ? AND interval_minutes = ? AND bar_time BETWEEN ? AND ? "
                "ORDER BY bar_time ASC",
                (pair, interval_minutes, start_ts, end_ts),
            )
            return cur.fetchall()

    def insert_l2_feature_snapshot(
        self,
        run_id: str,
        asset: str,
        ts: str,
        entry_price: float | None,
        anomaly_score: float | None,
        opportunity_score: float | None,
        setup_type: str,
        direction: str,
        features_json: str,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO l2_feature_snapshots (
                    run_id, asset, ts, entry_price, anomaly_score, opportunity_score,
                    setup_type, direction, features_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, asset, ts, entry_price, anomaly_score, opportunity_score, setup_type, direction, features_json),
            )

    # -- cooldown (Phase 3, per asset+model) ---------------------------------

    def get_cooldown(self, asset: str, model: str) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM model_cooldowns WHERE asset = ? AND model = ?",
                (asset, model),
            )
            return cur.fetchone()

    def set_cooldown(
        self, asset: str, model: str, ts: str, setup_type: str, direction: str, opportunity_score: float | None
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO model_cooldowns (asset, model, last_sent_ts, setup_type, direction, opportunity_score)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset, model) DO UPDATE SET
                    last_sent_ts = excluded.last_sent_ts,
                    setup_type = excluded.setup_type,
                    direction = excluded.direction,
                    opportunity_score = excluded.opportunity_score
                """,
                (asset, model, ts, setup_type, direction, opportunity_score),
            )

    # -- model budgets (Phase 3, hourly/daily per model) ---------------------

    def get_budget_count(self, model: str, window_kind: str, window_start: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                "SELECT count FROM model_budget_usage WHERE model = ? AND window_kind = ? AND window_start = ?",
                (model, window_kind, window_start),
            )
            row = cur.fetchone()
        return int(row["count"]) if row else 0

    def increment_budget(self, model: str, window_kind: str, window_start: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO model_budget_usage (model, window_kind, window_start, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(model, window_kind, window_start) DO UPDATE SET
                    count = count + 1
                """,
                (model, window_kind, window_start),
            )

    # -- event queue (Phase 3) -----------------------------------------------

    def find_open_event_by_dedup(self, dedup_key: str) -> sqlite3.Row | None:
        """An "open" event is one not yet PROCESSED/FAILED - used to dedupe
        so the same (asset, setup, direction, model_demand) doesn't spawn a
        fresh event every heartbeat.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM events WHERE dedup_key = ? AND status IN ('PENDING', 'PROCESSING', 'DEFERRED')
                ORDER BY ts DESC LIMIT 1
                """,
                (dedup_key,),
            )
            return cur.fetchone()

    def insert_event(self, event: dict[str, Any]) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO events (
                    event_id, dedup_key, ts, type, asset, setup_type, direction, market,
                    anomaly_score, opportunity_score, tradeability_score, confidence,
                    model_demand, reason, status, context_json, attempts, last_error,
                    next_attempt_at, processing_started_at, updated_ts, notified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"], event["dedup_key"], event["ts"], event["type"], event["asset"],
                    event["setup_type"], event["direction"], event["market"], event["anomaly_score"],
                    event["opportunity_score"], event["tradeability_score"], event["confidence"],
                    event["model_demand"], event["reason"], event["status"],
                    event.get("context_json"), int(event.get("attempts", 0)), event.get("last_error"),
                    event.get("next_attempt_at"), event.get("processing_started_at"),
                    event.get("updated_ts", event["ts"]), int(event.get("notified", False)),
                ),
            )

    def get_event(self, event_id: str) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
            return cur.fetchone()

    def update_event_status(self, event_id: str, status: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE events SET status = ? WHERE event_id = ?", (status, event_id))

    # -- event lifecycle (Phase 4: Claude Bridge) ----------------------------

    def find_actionable_events(self, now_iso: str, limit: int) -> list[sqlite3.Row]:
        """PENDING events, plus DEFERRED events whose backoff has elapsed
        (`next_attempt_at` unset or due). Never FAILED/PROCESSED/PROCESSING -
        those are settled or already claimed. Oldest first.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM events
                WHERE status = 'PENDING'
                   OR (status = 'DEFERRED' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                ORDER BY ts ASC
                LIMIT ?
                """,
                (now_iso, limit),
            )
            return cur.fetchall()

    def claim_event_for_processing(self, event_id: str, now_iso: str) -> bool:
        """Atomic claim: only succeeds if the event is still PENDING/DEFERRED.
        Returns False if it was already claimed (or settled) by someone else -
        the caller must never process it in that case (idempotency guard).
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE events SET status = 'PROCESSING', processing_started_at = ?, updated_ts = ?
                WHERE event_id = ? AND status IN ('PENDING', 'DEFERRED')
                """,
                (now_iso, now_iso, event_id),
            )
            return cur.rowcount == 1

    def recover_stale_processing(self, cutoff_iso: str, now_iso: str) -> list[str]:
        """A PROCESSING row whose claim predates `cutoff_iso` means the
        process died mid-call (task section 2: recovery after timeout).
        Goes back to PENDING, not DEFERRED - it never actually failed a call.
        Returns the recovered event_ids so the caller can audit-log each one.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT event_id FROM events WHERE status = 'PROCESSING' AND processing_started_at IS NOT NULL "
                "AND processing_started_at < ?",
                (cutoff_iso,),
            )
            event_ids = [row["event_id"] for row in cur.fetchall()]
            if event_ids:
                cur.executemany(
                    """
                    UPDATE events
                    SET status = 'PENDING', attempts = attempts + 1,
                        last_error = 'stale_processing_recovered', processing_started_at = NULL,
                        updated_ts = ?
                    WHERE event_id = ?
                    """,
                    [(now_iso, event_id) for event_id in event_ids],
                )
            return event_ids

    def mark_event_processed(self, event_id: str, now_iso: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE events SET status = 'PROCESSED', processing_started_at = NULL,
                    last_error = NULL, next_attempt_at = NULL, updated_ts = ?
                WHERE event_id = ?
                """,
                (now_iso, event_id),
            )

    def mark_event_deferred(self, event_id: str, now_iso: str, reason: str, next_attempt_at: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE events SET status = 'DEFERRED', attempts = attempts + 1, last_error = ?,
                    next_attempt_at = ?, processing_started_at = NULL, updated_ts = ?
                WHERE event_id = ?
                """,
                (reason, next_attempt_at, now_iso, event_id),
            )

    def mark_event_failed(self, event_id: str, now_iso: str, reason: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE events SET status = 'FAILED', attempts = attempts + 1, last_error = ?,
                    processing_started_at = NULL, next_attempt_at = NULL, updated_ts = ?
                WHERE event_id = ?
                """,
                (reason, now_iso, event_id),
            )

    def mark_event_notified(self, event_id: str, now_iso: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE events SET notified = 1, updated_ts = ? WHERE event_id = ?",
                (now_iso, event_id),
            )

    def set_ntfy_status(self, event_id: str, status: str, now_iso: str, error: str | None = None) -> None:
        """Mobile push delivery state for one event_id: PENDING while a send
        is in flight, SENT once delivered (terminal - dedup by event_id means
        it is never sent again), FAILED if it errored (bumps `ntfy_attempts`
        so the bounded cross-cycle retry eventually gives up). Never touches
        the event's own analysis `status` column - a push failure must never
        change whether the event itself counts as processed.
        """
        with self._cursor() as cur:
            if status == "FAILED":
                cur.execute(
                    """
                    UPDATE events SET ntfy_status = ?, ntfy_attempts = ntfy_attempts + 1,
                        ntfy_last_error = ?, ntfy_updated_ts = ? WHERE event_id = ?
                    """,
                    (status, error, now_iso, event_id),
                )
            else:
                cur.execute(
                    "UPDATE events SET ntfy_status = ?, ntfy_last_error = ?, ntfy_updated_ts = ? WHERE event_id = ?",
                    (status, error, now_iso, event_id),
                )

    def find_ntfy_retry_candidates(self, cutoff_iso: str, max_attempts: int, limit: int = 20) -> list[sqlite3.Row]:
        """PROCESSED events whose mobile push previously FAILED, still under
        the retry budget, and whose last attempt is old enough to retry again
        - a bounded, spaced-out retry, never a tight loop.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM events
                WHERE status = 'PROCESSED' AND ntfy_status = 'FAILED' AND ntfy_attempts < ?
                  AND (ntfy_updated_ts IS NULL OR ntfy_updated_ts <= ?)
                ORDER BY ntfy_updated_ts ASC
                LIMIT ?
                """,
                (max_attempts, cutoff_iso, limit),
            )
            return cur.fetchall()

    def event_status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ("PENDING", "PROCESSING", "PROCESSED", "DEFERRED", "FAILED")}
        with self._cursor() as cur:
            cur.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status")
            for row in cur.fetchall():
                if row["status"] in counts:
                    counts[row["status"]] = row["n"]
        return counts

    def latest_event(self) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM events ORDER BY updated_ts DESC, ts DESC LIMIT 1")
            return cur.fetchone()

    def list_alert_events(self, limit: int = 20) -> list[sqlite3.Row]:
        """Alerts recoverable via `--mode alerts`/`--mode prompt`: anything
        the Demand Router routed to SONNET/FABLE (IGNORE-demand candidates
        never produce a notification, so they are excluded), most recent
        first. Includes MOCK-* synthetic events - callers tag those
        separately (see alerts.py) rather than filtering them out here.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM events WHERE model_demand IN ('SONNET', 'FABLE') ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            return cur.fetchall()

    # -- model analyses (Phase 4) ---------------------------------------------

    def insert_model_analysis(
        self,
        event_id: str,
        model: str,
        model_version: str,
        requested_at: str,
        completed_at: str | None,
        status: str,
        response: str | None,
        parsed_output_json: str | None,
        latency_ms: float | None,
        input_tokens: int | None,
        output_tokens: int | None,
        error: str | None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO model_analyses (
                    event_id, model, model_version, requested_at, completed_at, status,
                    response, parsed_output_json, latency_ms, input_tokens, output_tokens, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, model, model_version, requested_at, completed_at, status,
                    response, parsed_output_json, latency_ms, input_tokens, output_tokens, error,
                ),
            )

    def get_model_analyses_for_event(self, event_id: str) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM model_analyses WHERE event_id = ? ORDER BY id ASC",
                (event_id,),
            )
            return cur.fetchall()

    def has_successful_analysis(self, event_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM model_analyses WHERE event_id = ? AND status = 'SUCCESS' LIMIT 1",
                (event_id,),
            )
            return cur.fetchone() is not None

    def model_analysis_call_counts(self) -> dict[str, int]:
        """Real API attempts per model - COUNT(*) over `model_analyses`, one
        row per actual dispatch. This is distinct from the Demand Router's
        `sonnet_demand`/`fable_demand` in radar_v08_output.json, which counts
        decisions, not calls (a control-room UI must keep the two separate).
        """
        with self._cursor() as cur:
            cur.execute("SELECT model, COUNT(*) AS n FROM model_analyses GROUP BY model")
            return {row["model"]: row["n"] for row in cur.fetchall()}

    # -- Claude Bridge health (Phase 4) ---------------------------------------

    def set_bridge_health(self, state: str, detail: str | None, now_iso: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bridge_health (id, state, detail, updated_ts) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET state = excluded.state, detail = excluded.detail,
                    updated_ts = excluded.updated_ts
                """,
                (state, detail, now_iso),
            )

    def get_bridge_health(self) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM bridge_health WHERE id = 1")
            return cur.fetchone()

    # -- retention ----------------------------------------------------------

    def prune(self, retention_days: int, now: datetime | None = None) -> tuple[int, int]:
        """Delete spot/futures snapshots (and OHLC bars) older than the
        retention window. Returns (spot_deleted, futures_deleted).
        """
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=retention_days)).isoformat()
        with self._cursor() as cur:
            cur.execute("DELETE FROM spot_snapshots WHERE ts < ?", (cutoff,))
            spot_deleted = cur.rowcount
            cur.execute("DELETE FROM futures_snapshots WHERE ts < ?", (cutoff,))
            futures_deleted = cur.rowcount
            cur.execute("DELETE FROM ohlc_bars WHERE bar_time < ?", (cutoff,))
        return spot_deleted, futures_deleted
