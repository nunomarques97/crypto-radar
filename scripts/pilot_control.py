"""Operate the pilot shadow's kill switch and loss-lock reviews, or show its state.

Usage::

    python scripts/pilot_control.py status [--json] [--db PATH]
    python scripts/pilot_control.py kill --reason TEXT [--db PATH]
    python scripts/pilot_control.py release --reason TEXT [--db PATH]
    python scripts/pilot_control.py review-lock --lock-id N --cause TEXT --reviewer NAME [--db PATH]

The pilot shadow is pretend money only: this script never places an
order, never calls a private API, a model or the network. ``--db`` defaults to the
radar's own database (``config.SQLITE_PATH``). A missing database, or one without the
pilot tables, is refused: nothing is created. ``status`` opens the database read-only;
the other commands append one row through ``adapters.pilot_store`` (append-only tables,
one short transaction) and change nothing else:

* ``kill`` engages the kill switch: every new candidate is refused
  (``kill_switch_engaged``) until ``release``; an open position still closes by its exits.
* ``review-lock`` is the only way to clear a daily-loss or drawdown lock: it records the
  reviewer and the cause and rebases the lock's reference to the equity now.

Exit codes: 0 done, 2 refused or failed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from radar_v08 import config  # noqa: E402
from radar_v08.adapters import pilot_store  # noqa: E402

#: The actor recorded on a kill switch row written by this script.
ACTOR = "scripts/pilot_control.py"
#: How long a write waits for the running radar's short transactions.
BUSY_TIMEOUT_SECONDS = 5.0


class Refused(Exception):
    """The command was refused before anything was written."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pilot shadow control (pretend money only): status, kill switch, lock reviews."
    )
    parser.add_argument("--db", default=None, help="radar database (default: config.SQLITE_PATH)")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="show the account, kill switch, locks and open position")
    status.add_argument("--json", action="store_true", help="print one JSON object")
    kill = commands.add_parser("kill", help="engage the kill switch: no new entry")
    kill.add_argument("--reason", required=True)
    release = commands.add_parser("release", help="release the kill switch")
    release.add_argument("--reason", required=True)
    review = commands.add_parser("review-lock", help="clear one lock with an explicit review")
    review.add_argument("--lock-id", required=True, type=int)
    review.add_argument("--cause", required=True)
    review.add_argument("--reviewer", required=True)
    for sub in (status, kill, release, review):
        sub.add_argument("--db", default=argparse.SUPPRESS, help="radar database (default: config.SQLITE_PATH)")
    return parser


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    """Open an existing database with the pilot tables; never create either."""
    if not path.is_file():
        raise Refused(f"no database at {path}; nothing was created")
    mode = "ro" if read_only else "rw"
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        if not pilot_store.schema_present(conn):
            raise Refused(f"{path} has no pilot shadow tables; nothing was created (they appear with the loop)")
    except BaseException:
        conn.close()
        raise
    return conn


def _text(value: Any) -> Any:
    return None if value is None else pilot_store.decimal_text(value)


def _status(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    account = pilot_store.read_account(conn)
    state = pilot_store.account_state(conn, now=now)
    switch = pilot_store.kill_switch(conn)
    locks = pilot_store.read_locks(conn)
    return {
        "now": now.isoformat(),
        "account": None
        if account is None
        else {
            "assigned_equity": _text(account.envelope.equity),
            "currency": account.envelope.currency,
            "recorded_at": account.recorded_at,
            "envelope_sha256": account.envelope_sha256,
        },
        "equity": None if state is None else _text(state.equity),
        "realized": None if state is None else _text(state.realized),
        "cash": None if state is None else _text(state.cash),
        "day_start": _text(pilot_store.day_start(conn, pilot_store.utc_day(now))),
        "high_water": _text(pilot_store.high_water(conn)),
        "kill_switch": {
            "state": "engaged" if switch.engaged else "released",
            "reason": switch.reason,
            "actor": switch.actor,
            "recorded_at": switch.recorded_at,
        },
        "locks_active": [
            {
                "lock_id": lock.lock_id,
                "kind": lock.kind.value,
                "equity": _text(lock.equity),
                "reference": _text(lock.reference),
                "limit_pct": _text(lock.limit_pct),
                "tripped_at": lock.tripped_at,
            }
            for lock in locks
            if lock.active
        ],
        "locks_reviewed": sum(1 for lock in locks if not lock.active),
        "open_positions": [
            {
                "position_id": position.position_id,
                "pair": position.pair,
                "quantity": _text(position.quantity),
                "entry_ask": _text(position.entry_ask),
                "stop": _text(position.stop),
                "target": _text(position.target),
                "due_at": position.due_at.isoformat(),
            }
            for position in pilot_store.read_open_positions(conn)
        ],
        "no_trade": {reason: count for reason, count in pilot_store.no_trade_counts(conn).items() if count},
    }


def _print_status(status: dict[str, Any], out: TextIO) -> None:
    account = status["account"]
    print("Pilot shadow - pretend money, no orders, LONG only", file=out)
    if account is None:
        print("  Account: not recorded yet", file=out)
    else:
        print(f"  Assigned equity: {account['assigned_equity']} {account['currency']}", file=out)
    for label, key in (("Equity now", "equity"), ("Realized", "realized"), ("Cash", "cash"),
                       ("Day start (UTC)", "day_start"), ("High-water", "high_water")):
        print(f"  {label}: {status[key] if status[key] is not None else 'not recorded'}", file=out)
    switch = status["kill_switch"]
    detail = f" ({switch['reason']}, {switch['recorded_at']})" if switch["reason"] else ""
    print(f"  Kill switch: {switch['state']}{detail}", file=out)
    if status["locks_active"]:
        for lock in status["locks_active"]:
            print(
                f"  Lock {lock['lock_id']}: {lock['kind']} since {lock['tripped_at']} "
                f"(equity {lock['equity']}, reference {lock['reference']}, limit {lock['limit_pct']}%)",
                file=out,
            )
    else:
        print("  Locks: none active", file=out)
    for position in status["open_positions"]:
        print(
            f"  Open: #{position['position_id']} {position['pair']} qty {position['quantity']} "
            f"entry {position['entry_ask']} stop {position['stop']} target {position['target']} "
            f"until {position['due_at']}",
            file=out,
        )
    refusals = ", ".join(f"{reason} {count}" for reason, count in status["no_trade"].items()) or "none"
    print(f"  NO_TRADE so far: {refusals}", file=out)


def _run(args: argparse.Namespace, now: datetime, out: TextIO) -> None:
    path = Path(args.db if args.db is not None else config.SQLITE_PATH)
    conn = _connect(path, read_only=args.command == "status")
    try:
        if args.command == "status":
            status = _status(conn, now)
            if args.json:
                print(json.dumps(status, indent=2, sort_keys=True), file=out)
            else:
                _print_status(status, out)
        elif args.command == "kill":
            switch = pilot_store.engage_kill_switch(conn, reason=args.reason, actor=ACTOR, now=now)
            print(f"Kill switch engaged at {switch.recorded_at}: no new pilot entry until release.", file=out)
        elif args.command == "release":
            switch = pilot_store.release_kill_switch(conn, reason=args.reason, actor=ACTOR, now=now)
            print(f"Kill switch released at {switch.recorded_at}.", file=out)
        else:
            review = pilot_store.review_lock(
                conn, lock_id=args.lock_id, reviewer=args.reviewer, cause=args.cause, now=now
            )
            print(
                f"Lock {review.lock_id} cleared by review {review.review_id} at {review.reviewed_at}; "
                f"reference rebased to equity {pilot_store.decimal_text(review.rebase_equity)}.",
                file=out,
            )
    finally:
        conn.close()


def _wall_clock() -> datetime:
    return datetime.now(UTC)


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], datetime] = _wall_clock,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr
    try:
        _run(args, clock(), stdout)
    except Refused as error:
        print(f"Refused: {error}", file=stderr)
        return 2
    except pilot_store.PilotStoreError as error:
        print(f"Refused ({error.code.value}): {error.detail}", file=stderr)
        return 2
    except sqlite3.Error as error:
        print(f"Database error: {error}", file=stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
