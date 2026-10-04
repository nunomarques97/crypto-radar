"""Read-only audit of the paper game and pilot shadow records in one SQLite file.

Usage::

    python scripts/audit_paper.py --db PATH [--format json|text]

``--db`` is required: there is no default and no live path. The script checks that the
path is an existing regular file, then opens it with a ``file:`` URI in ``mode=ro`` plus
``PRAGMA query_only = ON``, reads everything it needs, closes the connection and only
then prints to stdout. It never creates, migrates or writes a file, never calls the
network, a model or a private API, and imports only the standard library and pure
``radar_v08.domain`` modules (no configuration, runtime, adapter or UI module).

What it prints:

* ``as_of`` (when the audit read the file) and the units of every number;
* the paper cashflow: start balance, realized gross at the mid, spread cost, assumed
  fees, realized net, the stakes still reserved by open plays and the resulting realized
  balance and free cash. Open plays are counted at their stake; the audit takes no mark;
* play counts (open and closed, wins, losses, flats) and the same totals grouped by exit
  policy, direction, setup, entry spread bucket and entry UTC hour;
* hold durations (``exit_ts - entry_ts``) of closed plays by exit reason;
* pilot decisions and ``NO_TRADE`` refusals by reason, and pilot position counts.

Paper money is the stake-scaled pretend wallet currency. A play quoted in another
currency is a hypothetical simulation with FX excluded, not inventory in the wallet
currency. Fees are the ASSUMED rate stored on each play (account tier unverified).

A table or column that is absent (an empty file, a database from before the EX-1 exit
policy, no pilot tables) is reported as a typed unavailable entry such as
``{"available": false, "reason": "missing_column", "table": "paper_closes",
"column": "exit_reason"}``, never as a zero total. The recorded ``why`` object, the
pilot envelope and any other raw context are not printed: only the setup label is
extracted from the ``why`` object.

Exit codes: 0 audit printed, 2 refused (usage, missing file, unreadable database).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from radar_v08.domain import paper  # noqa: E402

AUDIT_FORMAT = "paper_audit_v1"
#: How long a read waits for another process's short write transaction.
BUSY_TIMEOUT_SECONDS = 2.0
#: Upper bounds (inclusive, bps of ask/bid - 1) of the entry spread buckets.
SPREAD_BUCKETS: tuple[tuple[str, Decimal], ...] = (
    ("le_5bps", Decimal(5)),
    ("gt_5_le_20bps", Decimal(20)),
)
SPREAD_TOP_BUCKET = "gt_20bps"
INVALID_QUOTE = "invalid_quote"
INVALID_TIME = "invalid_time"
NONE_RECORDED = "none_recorded"
#: A group key read from the database is clipped to this length.
MAX_KEY_LENGTH = 64

WALLET_TABLE = "paper_wallet"
PLAY_TABLE = "paper_plays"
CLOSE_TABLE = "paper_closes"
PILOT_DECISION_TABLE = "pilot_decisions"
PILOT_POSITION_TABLE = "pilot_positions"
PILOT_CLOSE_TABLE = "pilot_closes"
PILOT_ACCOUNT_TABLE = "pilot_account"

WALLET_COLUMNS = ("wallet_id", "start_balance_cents", "currency")
PLAY_COLUMNS = ("play_id", "direction", "quote", "stake_cents", "fee_bps", "entry_bid", "entry_ask", "entry_ts")
CLOSE_COLUMNS = ("play_id", "exit_ts", "gross_mid_cents", "spread_cost_cents", "fees_cents", "net_cents", "outcome")

UNITS = {
    "money": "integer cents of the paper wallet currency (paper.currency)",
    "paper_money_basis": (
        "stake-scaled pretend money; a play quoted in another currency is a hypothetical "
        "simulation with FX excluded, not inventory in the wallet currency"
    ),
    "fees": "the ASSUMED fee per leg stored on each play (account tier unverified), both legs charged at close",
    "open_plays": "counted at their reserved stake; the audit takes no mark and values no open play",
    "groups": "gross, spread, fees and net of a group cover its closed plays only; open plays are counted, not valued",
    "spread": "entry spread in bps of entry_ask / entry_bid - 1",
    "entry_hour": "UTC hour (00-23) of entry_ts",
    "duration": "seconds from entry_ts to exit_ts",
}


class Refused(Exception):
    """The audit was refused before or while reading; nothing was printed on stdout."""


class Malformed(Exception):
    """A stored value does not have the type the schema requires."""

    def __init__(self, table: str, column: str) -> None:
        super().__init__(f"{table}.{column}")
        self.table = table
        self.column = column


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of the paper game and pilot shadow records (pretend money only). "
            "Opens the given SQLite file read-only and prints to stdout; it never writes."
        )
    )
    parser.add_argument("--db", required=True, help="SQLite file to audit (required; no default)")
    parser.add_argument("--format", choices=("json", "text"), default="text", help="output format (default: text)")
    return parser


def _readonly_uri(path: Path) -> str:
    # ``as_uri`` percent-encodes ``?`` and ``#``: a path can never inject URI parameters.
    return path.as_uri() + "?mode=ro"


def connect(db: str) -> sqlite3.Connection:
    """A read-only connection to an existing regular file; a missing path is refused."""
    try:
        path = Path(db).resolve()
        found = path.is_file()
    except (OSError, ValueError):
        found = False
    if not found:
        raise Refused(f"database not found or not a regular file: {db}")
    conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=BUSY_TIMEOUT_SECONDS)
    conn.execute("PRAGMA query_only = ON")
    return conn


def unavailable(reason: str, table: str, column: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"available": False, "reason": reason, "table": table}
    if column is not None:
        entry["column"] = column
    return entry


def read_schema(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {table: {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')} for table in tables}


def missing(schema: Mapping[str, set[str]], table: str, columns: Iterable[str] = ()) -> dict[str, Any] | None:
    """The typed unavailable entry for the first absent table or column, else ``None``."""
    if table not in schema:
        return unavailable("missing_table", table)
    for column in columns:
        if column not in schema[table]:
            return unavailable("missing_column", table, column)
    return None


def _cents(table: str, column: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Malformed(table, column)
    return value


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def _key(value: object) -> str:
    if value is None:
        return NONE_RECORDED
    text = str(value)[:MAX_KEY_LENGTH]
    return "".join(char if char.isprintable() else "?" for char in text) or NONE_RECORDED


def spread_bucket(entry_bid: object, entry_ask: object) -> str:
    quote = paper.validate_quote(_decimal(entry_bid), _decimal(entry_ask))
    if isinstance(quote, paper.QuoteProblem):
        return INVALID_QUOTE
    with localcontext(paper.PAPER_CONTEXT):
        spread_bps = (quote.ask / quote.bid - 1) * paper.BPS
    for name, upper in SPREAD_BUCKETS:
        if spread_bps <= upper:
            return name
    return SPREAD_TOP_BUCKET


def entry_hour(entry_ts: object) -> str:
    moment = _time(entry_ts)
    return INVALID_TIME if moment is None else f"{moment.hour:02d}"


def _empty_group() -> dict[str, int]:
    return {
        "plays": 0,
        "open": 0,
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "flats": 0,
        "gross_mid_cents": 0,
        "spread_cost_cents": 0,
        "fees_cents": 0,
        "net_cents": 0,
    }


def _add(group: dict[str, int], play: Mapping[str, Any]) -> None:
    group["plays"] += 1
    close = play["close"]
    if close is None:
        group["open"] += 1
        return
    group["closed"] += 1
    outcome = close["outcome"]
    if outcome == paper.Outcome.WIN.value:
        group["wins"] += 1
    elif outcome == paper.Outcome.LOSS.value:
        group["losses"] += 1
    elif outcome == paper.Outcome.FLAT.value:
        group["flats"] += 1
    for column in ("gross_mid_cents", "spread_cost_cents", "fees_cents", "net_cents"):
        group[column] += close[column]


def group_by(plays: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, dict[str, int]] = {}
    for play in plays:
        _add(groups.setdefault(play[key], _empty_group()), play)
    return {"available": True, "groups": dict(sorted(groups.items()))}


def duration_stats(seconds: Sequence[float]) -> dict[str, Any]:
    return {
        "closed": len(seconds),
        "mean_seconds": round(statistics.fmean(seconds), 3),
        "median_seconds": round(statistics.median(seconds), 3),
        "min_seconds": round(min(seconds), 3),
        "max_seconds": round(max(seconds), 3),
    }


def durations_by(plays: Sequence[Mapping[str, Any]], key: str | None) -> dict[str, Any]:
    buckets: dict[str, list[float]] = {}
    invalid = 0
    for play in plays:
        close = play["close"]
        if close is None:
            continue
        start, end = _time(play["entry_ts"]), _time(close["exit_ts"])
        if start is None or end is None or end < start:
            invalid += 1
            continue
        name = "all_closed" if key is None else close[key]
        buckets.setdefault(name, []).append((end - start).total_seconds())
    return {
        "available": True,
        "groups": {name: duration_stats(values) for name, values in sorted(buckets.items())},
        "invalid_times": invalid,
    }


def _load_plays(conn: sqlite3.Connection, schema: Mapping[str, set[str]]) -> list[dict[str, Any]]:
    play_columns = schema[PLAY_TABLE]
    close_columns = schema[CLOSE_TABLE]
    policy = "p.exit_policy" if "exit_policy" in play_columns else "NULL"
    # Only the setup label leaves the ``why`` object, and only when it is a JSON string.
    setup = (
        "CASE WHEN json_valid(p.why_json) AND json_type(p.why_json, '$.setup_type') = 'text' "
        "THEN json_extract(p.why_json, '$.setup_type') END"
        if "why_json" in play_columns
        else "NULL"
    )
    reason = "c.exit_reason" if "exit_reason" in close_columns else "NULL"
    rows = conn.execute(
        f"""SELECT p.play_id, p.direction, p.quote, p.stake_cents, p.fee_bps, p.entry_bid, p.entry_ask,
               p.entry_ts, {policy}, {setup}, c.play_id, c.exit_ts, c.gross_mid_cents, c.spread_cost_cents,
               c.fees_cents, c.net_cents, c.outcome, {reason}
        FROM {PLAY_TABLE} p LEFT JOIN {CLOSE_TABLE} c ON c.play_id = p.play_id
        ORDER BY p.play_id"""
    ).fetchall()
    plays: list[dict[str, Any]] = []
    for row in rows:
        (play_id, direction, quote, stake, fee_bps, bid, ask, entry_ts, exit_policy, setup_type,
         close_play, exit_ts, gross, spread, fees, net, outcome, exit_reason) = row
        close = None
        if close_play is not None:
            close = {
                "exit_ts": exit_ts,
                "gross_mid_cents": _cents(CLOSE_TABLE, "gross_mid_cents", gross),
                "spread_cost_cents": _cents(CLOSE_TABLE, "spread_cost_cents", spread),
                "fees_cents": _cents(CLOSE_TABLE, "fees_cents", fees),
                "net_cents": _cents(CLOSE_TABLE, "net_cents", net),
                "outcome": outcome,
                "exit_reason": _key(exit_reason),
            }
        plays.append(
            {
                "play_id": play_id,
                "stake_cents": _cents(PLAY_TABLE, "stake_cents", stake),
                "direction": _key(direction),
                "quote": _key(quote),
                "fee_bps": _key(fee_bps),
                "exit_policy": _key(exit_policy),
                "setup": _key(setup_type),
                "spread_bucket": spread_bucket(bid, ask),
                "entry_hour_utc": entry_hour(entry_ts),
                "entry_ts": entry_ts,
                "close": close,
            }
        )
    return plays


def _count_by(plays: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for play in plays:
        counts[play[key]] = counts.get(play[key], 0) + 1
    return dict(sorted(counts.items()))


def audit_paper(conn: sqlite3.Connection, schema: Mapping[str, set[str]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The paper section and its coverage entries."""
    coverage: list[dict[str, Any]] = []
    gap = (
        missing(schema, WALLET_TABLE, WALLET_COLUMNS)
        or missing(schema, PLAY_TABLE, PLAY_COLUMNS)
        or missing(schema, CLOSE_TABLE, CLOSE_COLUMNS)
    )
    if gap is not None:
        coverage.append({"section": "paper", **gap})
        return {"available": False, "unavailable": gap}, coverage
    coverage.append({"section": "paper", "available": True})
    wallet = conn.execute(f"SELECT start_balance_cents, currency FROM {WALLET_TABLE} WHERE wallet_id = 1").fetchone()
    try:
        plays = _load_plays(conn, schema)
        start = None if wallet is None else _cents(WALLET_TABLE, "start_balance_cents", wallet[0])
    except Malformed as error:
        gap = unavailable("malformed_value", error.table, error.column)
        coverage[-1] = {"section": "paper", **gap}
        return {"available": False, "unavailable": gap}, coverage

    closed = [play for play in plays if play["close"] is not None]
    open_plays = [play for play in plays if play["close"] is None]
    totals = _empty_group()
    for play in plays:
        _add(totals, play)
    open_stakes = sum(play["stake_cents"] for play in open_plays)
    identity = all(
        c["gross_mid_cents"] - c["spread_cost_cents"] - c["fees_cents"] == c["net_cents"]
        for c in (play["close"] for play in closed)
    )
    if start is None:
        wallet_entry: dict[str, Any] = unavailable("missing_row", WALLET_TABLE)
        coverage.append({"section": "paper.wallet", **wallet_entry})
        balance: dict[str, Any] = {"realized_balance_cents": None, "free_cash_cents": None, "unavailable": wallet_entry}
        currency = None
    else:
        currency = _key(wallet[1])
        balance = {
            "realized_balance_cents": start + totals["net_cents"],
            "free_cash_cents": start + totals["net_cents"] - open_stakes,
        }
    cashflow = {
        "start_balance_cents": start,
        "realized_gross_mid_cents": totals["gross_mid_cents"],
        "realized_spread_cost_cents": totals["spread_cost_cents"],
        "realized_fees_cents": totals["fees_cents"],
        "realized_net_cents": totals["net_cents"],
        "open_stake_cents": open_stakes,
        **balance,
        "identity_gross_minus_spread_minus_fees_equals_net": identity,
    }
    groups: dict[str, Any] = {}
    for name, column in (
        ("exit_policy", "exit_policy"),
        ("direction", None),
        ("setup", "why_json"),
        ("spread_bucket", None),
        ("entry_hour_utc", None),
    ):
        gap = None if column is None else missing(schema, PLAY_TABLE, (column,))
        groups[name] = gap if gap is not None else group_by(plays, name)
        if gap is not None:
            coverage.append({"section": f"paper.groups.{name}", **gap})
    reason_gap = missing(schema, CLOSE_TABLE, ("exit_reason",))
    if reason_gap is not None:
        coverage.append({"section": "paper.hold_durations_by_exit_reason", **reason_gap})
    section = {
        "available": True,
        "currency": currency,
        "cashflow": cashflow,
        "counts": {
            "plays": len(plays),
            "open": len(open_plays),
            "closed": len(closed),
            "wins": totals["wins"],
            "losses": totals["losses"],
            "flats": totals["flats"],
        },
        "quote_currencies": _count_by(plays, "quote"),
        "assumed_fee_bps_per_leg": _count_by(plays, "fee_bps"),
        "groups": groups,
        "hold_durations_all_closed": durations_by(plays, None),
        "hold_durations_by_exit_reason": reason_gap if reason_gap is not None else durations_by(plays, "exit_reason"),
    }
    return section, coverage


def audit_pilot(conn: sqlite3.Connection, schema: Mapping[str, set[str]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The pilot section (decisions, refusals by reason, positions) and its coverage entries."""
    coverage: list[dict[str, Any]] = []
    gap = missing(schema, PILOT_DECISION_TABLE, ("outcome", "reason"))
    if gap is not None:
        coverage.append({"section": "pilot", **gap})
        return {"available": False, "unavailable": gap}, coverage
    coverage.append({"section": "pilot", "available": True})
    outcomes = {
        _key(outcome): count
        for outcome, count in conn.execute(f"SELECT outcome, count(*) FROM {PILOT_DECISION_TABLE} GROUP BY outcome")
    }
    refusals = {
        _key(reason): count
        for reason, count in conn.execute(
            f"SELECT reason, count(*) FROM {PILOT_DECISION_TABLE} WHERE outcome = 'NO_TRADE' GROUP BY reason"
        )
    }
    section: dict[str, Any] = {
        "available": True,
        "decisions": {"total": sum(outcomes.values()), "by_outcome": dict(sorted(outcomes.items()))},
        "no_trade_by_reason": dict(sorted(refusals.items())),
    }
    currency_gap = missing(schema, PILOT_ACCOUNT_TABLE, ("account_id", "currency"))
    if currency_gap is None:
        row = conn.execute(f"SELECT currency FROM {PILOT_ACCOUNT_TABLE} WHERE account_id = 1").fetchone()
        section["currency"] = None if row is None else _key(row[0])
    positions_gap = missing(schema, PILOT_POSITION_TABLE, ("position_id",)) or missing(
        schema, PILOT_CLOSE_TABLE, ("position_id",)
    )
    if positions_gap is not None:
        section["positions"] = positions_gap
        coverage.append({"section": "pilot.positions", **positions_gap})
    else:
        total, closed_count = conn.execute(
            f"""SELECT count(*), count(c.position_id) FROM {PILOT_POSITION_TABLE} p
            LEFT JOIN {PILOT_CLOSE_TABLE} c ON c.position_id = p.position_id"""
        ).fetchone()
        section["positions"] = {"available": True, "total": total, "open": total - closed_count, "closed": closed_count}
    return section, coverage


def run_audit(db: str, now: datetime | None = None) -> dict[str, Any]:
    """Read the whole audit on one read-only connection, closed before this returns."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        with closing(connect(db)) as conn:
            schema = read_schema(conn)
            paper_section, paper_coverage = audit_paper(conn, schema)
            pilot_section, pilot_coverage = audit_pilot(conn, schema)
    except sqlite3.Error as error:
        raise Refused(f"cannot read the database: {error.__class__.__name__}: {error}") from None
    return {
        "format": AUDIT_FORMAT,
        "as_of": moment.isoformat(timespec="seconds"),
        "units": UNITS,
        "coverage": [*paper_coverage, *pilot_coverage],
        "paper": paper_section,
        "pilot": pilot_section,
    }


def _money(cents: object, currency: object) -> str:
    if not isinstance(cents, int):
        return "unavailable"
    return f"{Decimal(cents) / 100:.2f} {currency or ''}".rstrip()


def _unavailable_text(entry: Mapping[str, Any]) -> str:
    where = entry["table"] + (f".{entry['column']}" if "column" in entry else "")
    return f"unavailable ({entry['reason']}: {where})"


def render_text(report: Mapping[str, Any]) -> str:
    lines = [f"Paper audit ({report['format']}), as of {report['as_of']}"]
    lines += [f"  units.{name}: {text}" for name, text in report["units"].items()]
    lines.append("Coverage:")
    for entry in report["coverage"]:
        state = "available" if entry["available"] else _unavailable_text(entry)
        lines.append(f"  {entry['section']}: {state}")
    section = report["paper"]
    lines.append("Paper game:")
    if not section["available"]:
        lines.append(f"  {_unavailable_text(section['unavailable'])}")
    else:
        currency = section["currency"]
        flow = section["cashflow"]
        for name in (
            "start_balance_cents",
            "realized_gross_mid_cents",
            "realized_spread_cost_cents",
            "realized_fees_cents",
            "realized_net_cents",
            "open_stake_cents",
            "realized_balance_cents",
            "free_cash_cents",
        ):
            lines.append(f"  {name.removesuffix('_cents')}: {_money(flow[name], currency)}")
        lines.append(
            f"  gross - spread - fees = net on every close: {flow['identity_gross_minus_spread_minus_fees_equals_net']}"
        )
        lines.append("  counts: " + ", ".join(f"{name} {count}" for name, count in section["counts"].items()))
        lines.append("  quote currencies: " + json.dumps(section["quote_currencies"]))
        lines.append("  assumed fee bps per leg: " + json.dumps(section["assumed_fee_bps_per_leg"]))
        for name, group in section["groups"].items():
            lines.append(f"  by {name}:")
            if not group["available"]:
                lines.append(f"    {_unavailable_text(group)}")
                continue
            for key, stats in group["groups"].items():
                lines.append(
                    f"    {key}: plays {stats['plays']} (open {stats['open']}, closed {stats['closed']}, "
                    f"wins {stats['wins']}), realized net {_money(stats['net_cents'], currency)}, "
                    f"spread {_money(stats['spread_cost_cents'], currency)}, fees {_money(stats['fees_cents'], currency)}"
                )
        for name in ("hold_durations_all_closed", "hold_durations_by_exit_reason"):
            durations = section[name]
            lines.append(f"  {name}:")
            if not durations["available"]:
                lines.append(f"    {_unavailable_text(durations)}")
                continue
            for key, stats in durations["groups"].items():
                lines.append(
                    f"    {key}: closed {stats['closed']}, mean {stats['mean_seconds']} s, "
                    f"median {stats['median_seconds']} s"
                )
    pilot = report["pilot"]
    lines.append("Pilot shadow (pretend PAPER account):")
    if not pilot["available"]:
        lines.append(f"  {_unavailable_text(pilot['unavailable'])}")
    else:
        decisions = pilot["decisions"]
        lines.append(f"  decisions: {decisions['total']} " + json.dumps(decisions["by_outcome"]))
        lines.append("  NO_TRADE by reason: " + json.dumps(pilot["no_trade_by_reason"]))
        positions = pilot["positions"]
        if positions["available"]:
            lines.append(f"  positions: {positions['total']} (open {positions['open']}, closed {positions['closed']})")
        else:
            lines.append(f"  positions: {_unavailable_text(positions)}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_audit(args.db)
    except Refused as error:
        print(f"audit_paper: {error}", file=sys.stderr)
        return 2
    if args.format == "json":
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=False) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
