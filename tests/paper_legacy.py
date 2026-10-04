"""Paper game rows as the code before the EX-1 exit policy wrote them (test fixture only).

``LEGACY_DDL`` is a frozen copy of the paper schema before the EX-1 columns were added
(the ``paper_store.SCHEMA_STATEMENTS`` of commit daf328b). ``insert_legacy_play`` and
``insert_legacy_close`` write rows with only the columns that code wrote: a play with a
fixed hold and no exit levels. Temporary databases only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

LEGACY_DDL = """CREATE TABLE IF NOT EXISTS paper_wallet (
    wallet_id INTEGER PRIMARY KEY CHECK (wallet_id = 1),
    start_balance_cents INTEGER NOT NULL
        CHECK (typeof(start_balance_cents) = 'integer' AND start_balance_cents > 0),
    currency TEXT NOT NULL CHECK (length(currency) > 0),
    recorded_at TEXT NOT NULL CHECK (length(recorded_at) > 0)
);
CREATE TRIGGER IF NOT EXISTS paper_wallet_no_update BEFORE UPDATE ON paper_wallet
BEGIN
    SELECT RAISE(ABORT, 'paper_wallet rows are append-only');
END;
CREATE TRIGGER IF NOT EXISTS paper_wallet_no_delete BEFORE DELETE ON paper_wallet
BEGIN
    SELECT RAISE(ABORT, 'paper_wallet rows are never deleted');
END;
CREATE TABLE IF NOT EXISTS paper_plays (
    play_id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE CHECK (length(event_id) > 0),
    run_id TEXT NOT NULL CHECK (length(run_id) > 0),
    asset TEXT NOT NULL CHECK (length(asset) > 0),
    pair TEXT NOT NULL CHECK (length(pair) > 0),
    quote TEXT NOT NULL CHECK (length(quote) > 0),
    direction TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    stake_cents INTEGER NOT NULL CHECK (typeof(stake_cents) = 'integer' AND stake_cents > 0),
    fee_bps TEXT NOT NULL CHECK (length(fee_bps) > 0),
    hold_minutes INTEGER NOT NULL CHECK (typeof(hold_minutes) = 'integer' AND hold_minutes > 0),
    entry_bid TEXT NOT NULL CHECK (length(entry_bid) > 0),
    entry_ask TEXT NOT NULL CHECK (length(entry_ask) > 0),
    entry_ts TEXT NOT NULL CHECK (length(entry_ts) > 0),
    due_at TEXT NOT NULL CHECK (due_at > entry_ts),
    why_json TEXT NOT NULL CHECK (json_valid(why_json) AND json_type(why_json) = 'object'),
    opened_at TEXT NOT NULL CHECK (length(opened_at) > 0)
);
CREATE TRIGGER IF NOT EXISTS paper_plays_no_update BEFORE UPDATE ON paper_plays
BEGIN
    SELECT RAISE(ABORT, 'paper_plays rows are append-only');
END;
CREATE TRIGGER IF NOT EXISTS paper_plays_no_delete BEFORE DELETE ON paper_plays
BEGIN
    SELECT RAISE(ABORT, 'paper_plays rows are never deleted');
END;
CREATE TABLE IF NOT EXISTS paper_closes (
    close_id INTEGER PRIMARY KEY,
    play_id INTEGER NOT NULL UNIQUE REFERENCES paper_plays (play_id),
    exit_bid TEXT NOT NULL CHECK (length(exit_bid) > 0),
    exit_ask TEXT NOT NULL CHECK (length(exit_ask) > 0),
    exit_ts TEXT NOT NULL CHECK (exit_ts >= due_at),
    exit_snapshot_id INTEGER NOT NULL,
    due_at TEXT NOT NULL CHECK (length(due_at) > 0),
    delay_seconds REAL NOT NULL CHECK (delay_seconds >= 0),
    gross_mid_cents INTEGER NOT NULL CHECK (typeof(gross_mid_cents) = 'integer'),
    spread_cost_cents INTEGER NOT NULL CHECK (typeof(spread_cost_cents) = 'integer' AND spread_cost_cents >= 0),
    fees_cents INTEGER NOT NULL CHECK (typeof(fees_cents) = 'integer' AND fees_cents >= 0),
    net_cents INTEGER NOT NULL CHECK (typeof(net_cents) = 'integer'),
    outcome TEXT NOT NULL CHECK (outcome IN ('WIN', 'LOSS', 'FLAT')),
    closed_at TEXT NOT NULL CHECK (length(closed_at) > 0),
    CHECK (net_cents = gross_mid_cents - spread_cost_cents - fees_cents),
    CHECK ((outcome = 'WIN' AND net_cents > 0) OR (outcome = 'LOSS' AND net_cents < 0)
        OR (outcome = 'FLAT' AND net_cents = 0))
);
CREATE TRIGGER IF NOT EXISTS paper_closes_no_update BEFORE UPDATE ON paper_closes
BEGIN
    SELECT RAISE(ABORT, 'paper_closes rows are append-only');
END;
CREATE TRIGGER IF NOT EXISTS paper_closes_no_delete BEFORE DELETE ON paper_closes
BEGIN
    SELECT RAISE(ABORT, 'paper_closes rows are never deleted');
END;
CREATE TRIGGER IF NOT EXISTS paper_closes_require_play BEFORE INSERT ON paper_closes
BEGIN
    SELECT RAISE(ABORT, 'paper_closes row has no play')
    WHERE NOT EXISTS (SELECT 1 FROM paper_plays WHERE play_id = NEW.play_id);
END;

"""


def utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def create_legacy_schema(conn: sqlite3.Connection, *, start_balance_cents: int = 100000, at: datetime) -> None:
    """The pre-EX-1 paper tables and a recorded wallet."""
    conn.executescript(LEGACY_DDL)
    conn.execute("INSERT INTO paper_wallet VALUES (1, ?, 'EUR', ?)", (start_balance_cents, utc(at)))
    conn.commit()


def insert_legacy_play(
    conn: sqlite3.Connection,
    event_id: str,
    asset: str,
    direction: str,
    bid: object,
    ask: object,
    at: datetime,
    *,
    hold_minutes: int = 60,
    stake_cents: int = 10000,
    fee_bps: str = "26",
    why: dict[str, object] | None = None,
    pair: str | None = None,
    quote: str = "EUR",
    run_id: str = "run-legacy",
) -> int:
    """One play as the pre-EX-1 code opened it: frozen hold, no levels. Returns its id."""
    cursor = conn.execute(
        "INSERT INTO paper_plays (event_id, run_id, asset, pair, quote, direction, stake_cents, fee_bps, "
        "hold_minutes, entry_bid, entry_ask, entry_ts, due_at, why_json, opened_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id, run_id, asset, pair or f"{asset}/{quote}", quote, direction, stake_cents, fee_bps, hold_minutes,
            str(Decimal(repr(bid)) if isinstance(bid, float) else bid),
            str(Decimal(repr(ask)) if isinstance(ask, float) else ask),
            utc(at), utc(at + timedelta(minutes=hold_minutes)),
            json.dumps(why if why is not None else {"setup_type": "BREAKOUT", "direction": direction}),
            utc(at),
        ),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def insert_legacy_close(
    conn: sqlite3.Connection, play_id: int, exit_bid: str, exit_ask: str, exit_at: datetime, due: datetime,
    *, snapshot_id: int, gross: int, spread: int, fees: int, outcome: str, closed_at: datetime,
) -> int:
    """One close row with only the pre-EX-1 columns."""
    cursor = conn.execute(
        "INSERT INTO paper_closes (play_id, exit_bid, exit_ask, exit_ts, exit_snapshot_id, due_at, delay_seconds, "
        "gross_mid_cents, spread_cost_cents, fees_cents, net_cents, outcome, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            play_id, exit_bid, exit_ask, utc(exit_at), snapshot_id, utc(due), (exit_at - due).total_seconds(),
            gross, spread, fees, gross - spread - fees, outcome, utc(closed_at),
        ),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid
