"""Preview the Game tab on sample data, for visual checks in a normal browser.

Builds fixture databases in a temporary directory with the real paper store
(``radar_v08.adapters.paper_store``), turns them into ``get_paper_state`` payloads
with the real reader (``ui.paper_reader``), writes a copy of ``ui/web`` with a stub
``window.pywebview.api`` and a visible "sample data" banner, and serves that copy on
127.0.0.1 on a free port.

It never opens the real radar database: every path it writes is inside its own
temporary directory, which is removed on exit together with the server.

    python -B scripts/paper_game_preview.py              # serve until Ctrl+C
    python -B scripts/paper_game_preview.py --seconds 600
    python -B scripts/paper_game_preview.py --self-test  # build and check, no server

Open ``http://127.0.0.1:<port>/index.html?scenario=open`` (also ``legacy``, ``empty``,
``stale``, ``unavailable`` and ``disabled``; ``&radar=STOPPED`` changes the radar pill and
``&scroll=<element id>`` scrolls that element to the top). ``open`` has EX-1 plays with
their stop, target and 24 h limit, closes on each reason and older legacy closes;
``legacy`` has only plays opened before the EX-1 policy; ``stale`` has a play on a
USD-priced pair whose last recorded price is older than the reporting freshness bound, so
the wallet's total value is unknown (and USD-priced closes that carry the FX-excluded
label).

``&pilot=`` picks the Pilot shadow panel's sample (``get_pilot_state``, written through
the real pilot store and read by ``ui.pilot_reader``): ``open`` (one open position, no
lock, a few NO_TRADE decisions; the default), ``stale`` (the same, but the position's last
recorded price is too old to value it), ``locked`` (a gap closed at a loss that
tripped the daily-loss and drawdown locks, the drawdown one cleared by a review, the kill
switch engaged and the entries refused since) or ``empty`` (the pilot has not started:
no ``pilot_*`` tables).

``&trend=`` picks the Trend paper panel's sample (``get_trend_paper_state``, a temporary ledger
written through the real catch-up and trend paper store from a synthetic offline market and
read by ``ui.trend_reader``): ``populated`` (14 paper days booked; the default), ``empty``
(before the first paper day), ``skipped`` (the same 14 days with two of them recorded as skipped
because the synthetic market lacks their EUR prices for good) or ``refused`` (an edited ledger).
The preview never fetches: its
``trend_paper_catch_up`` answers ``UP_TO_DATE`` after a short pause and writes nothing.

``?scenario=replay`` is a labelled replay for the animated office: one fixture
database read after each of a few steps (a radar cycle, an alert that opens a play
and deserves Sonnet, a Qwen review, new quotes, an alert for Fable), served one step
further every few polls (``&every=3`` by default), so the office reacts to activity
that appears between polls as it would with the radar running. The banner names the
step being shown.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from radar_v08 import trend_paper_hook  # noqa: E402
from radar_v08.adapters import paper_store as ps  # noqa: E402
from radar_v08.adapters import pilot_store, trend_paper_store  # noqa: E402
from radar_v08.adapters import qwen_review_store as qrs  # noqa: E402
from radar_v08.domain import paper, risk  # noqa: E402
from radar_v08.domain import trend_engine as trend_e  # noqa: E402
from radar_v08.domain import trend_paper as trend_p  # noqa: E402
from radar_v08.paper_monitor import TICKER_SOURCE  # noqa: E402
from radar_v08.store import SnapshotStore  # noqa: E402
from ui import paper_reader, pilot_reader, trend_reader  # noqa: E402
from ui.paper_reader import PaperReader  # noqa: E402
from ui.pilot_reader import PilotReader  # noqa: E402
from ui.trend_reader import TrendReader  # noqa: E402

WEB_DIR = REPOSITORY_ROOT / "ui" / "web"
STUB_SCRIPT = "preview_stub.js"
BANNER_TEXT = "Sample data — preview"
SCENARIOS = ("open", "legacy", "empty", "stale", "unavailable", "disabled")
DEFAULTS: dict[str, object] = {
    "start_balance": Decimal("1000"), "stake": Decimal("100"), "max_open": 3,
    "hold_minutes": paper.EX1_MAX_HOLD_MINUTES, "fee_bps": Decimal("26"),
}
TERMS = ps.PlayTerms(stake=Decimal("100"), fee_bps=Decimal("26"), max_open=3)
# Legacy plays (opened before the EX-1 exit policy): a fixed hold, no stop or target,
# closed on the first valid quote after the hold.
LEGACY_HOLD_MINUTES = 60
# The fixture ATR of an EX-1 play as a fraction of its entry mid: stop 0.5 %, target 1 % away.
ATR_FRACTION = 0.0025
# How far past a level the quote that touches it lands, as a fraction of the level.
TOUCH_FRACTION = 0.0003

# Legacy closes: (asset, direction, setup, minutes before now it opened, entry mid, exit move %,
# exit delay minutes)
LEGACY_CLOSES: tuple[tuple[str, str, str, int, float, float, int], ...] = (
    ("BTC", "LONG", "BREAKOUT", 2100, 58220.0, 0.74, 0),
    ("ETH", "SHORT", "EXHAUSTION", 2020, 2410.8, 0.31, 0),
    ("XRP", "LONG", "CONTINUATION", 1950, 0.5232, -0.42, 7),
)
# EX-1 closes: (asset, direction, setup, minutes before now it opened, entry mid, reason,
# minutes from entry to the quote that touches the level, source of that quote). A time
# close is observed 2 minutes after its 24 h limit, on a small move that touches nothing.
EX1_CLOSES: tuple[tuple[str, str, str, int, float, str, int, str], ...] = (
    ("ADA", "LONG", "SQUEEZE_RELEASE", 1700, 0.3503, "time", 0, ps.SPOT_SNAPSHOT_SOURCE),
    ("DOGE", "SHORT", "REVERSAL", 400, 0.11022, "stop", 70, ps.SPOT_SNAPSHOT_SOURCE),
    ("LINK", "LONG", "BREAKOUT", 300, 11.842, "target", 120, TICKER_SOURCE),
)
TIME_EXIT_MOVE = 0.12
CLOSED_COUNT = len(LEGACY_CLOSES) + len(EX1_CLOSES)
# Open EX-1 plays: (asset, direction, setup, minutes before now it opened, entry mid, move % since)
OPEN_PLAYS: tuple[tuple[str, str, str, int, float, float], ...] = (
    ("SOL", "LONG", "BREAKOUT", 24, 142.31, 0.58),
    ("AVAX", "SHORT", "REVERSAL", 9, 24.118, 0.21),
)
# The legacy scenario's open play, opened before the EX-1 policy on its fixed hold.
LEGACY_OPEN = ("AVAX", "SHORT", "REVERSAL", 20, 24.118, 0.21)
# The stale scenario: pairs priced in USD, two closes (same layout as EX1_CLOSES), then a
# SOL/EUR play priced until now and a newer ETH/USD play whose last price is
# STALE_QUOTE_MINUTES old, past the reporting freshness bound (paper_reader.NOW_PRICE_MAX_AGE).
STALE_QUOTES = {"BTC": "USD", "ETH": "USD"}
STALE_CLOSES: tuple[tuple[str, str, str, int, float, str, int, str], ...] = (
    ("BTC", "LONG", "BREAKOUT", 420, 64210.5, "target", 95, ps.SPOT_SNAPSHOT_SOURCE),
    ("DOGE", "SHORT", "REVERSAL", 400, 0.11022, "stop", 70, ps.SPOT_SNAPSHOT_SOURCE),
)
STALE_FRESH = ("SOL", "LONG", "BREAKOUT", 40, 142.31, 0.44)
STALE_OLD = ("ETH", "LONG", "CONTINUATION", 30, 2381.45, 0.3)
STALE_QUOTE_MINUTES = 22


class PreviewError(RuntimeError):
    pass


def _why(direction: str, setup: str, rng: random.Random) -> dict[str, object]:
    sign = 1 if direction == "LONG" else -1
    l2: dict[str, object] = {}
    if setup == "BREAKOUT":
        l2["breakout_state"] = "BREAKOUT_UP" if sign > 0 else "BREAKOUT_DOWN"
    return {
        "setup_type": setup,
        "direction": direction,
        "scores": {"opportunity_score": round(rng.uniform(70, 90), 1), "tradeability_score": round(rng.uniform(75, 92), 1),
                   "confidence": "HIGH"},
        "features": {
            "l1": {"return_15m": round(sign * rng.uniform(1.2, 3.4), 2),
                   "volume_intensity_15m": round(rng.uniform(2.0, 4.5), 1)},
            "l2": l2,
        },
    }


class Fixture:
    """One temporary radar database written only through the real store API and plain inserts."""

    def __init__(self, path: Path, now: datetime) -> None:
        self.path = path
        self.now = now
        SnapshotStore(str(path)).close()  # every radar table, as the radar creates them
        self.conn = sqlite3.connect(str(path))
        self.rng = random.Random(7)
        self._runs = 0
        #: The quote currency of an asset's pair (EUR unless listed).
        self.quotes: dict[str, str] = {}

    def close(self) -> None:
        self.conn.close()

    def at(self, minutes_ago: float) -> datetime:
        return self.now - timedelta(minutes=minutes_ago)

    def quote(self, asset: str) -> str:
        return self.quotes.get(asset, "EUR")

    def spot(self, asset: str, at: datetime, mid: float) -> tuple[float, float]:
        half = mid * 0.00008
        bid, ask = round(mid - half, 6), round(mid + half, 6)
        quote = self.quote(asset)
        self.conn.execute(
            "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES (?, ?, ?, ?, ?, ?, 'online')",
            (asset, f"{asset}/{quote}", quote, at.isoformat(), bid, ask),
        )
        self.conn.commit()
        return bid, ask

    def run(self, at: datetime) -> str:
        self._runs += 1
        run_id = f"preview-run-{self._runs}"
        self.conn.execute(
            "INSERT INTO radar_runs (run_id, ts, mode, assets_eligible, shortlist_count, warmup, api_failures) "
            "VALUES (?, ?, 'FULL', ?, ?, 0, 0)",
            (run_id, at.isoformat(), self.rng.randint(590, 630), self.rng.randint(1, 4)),
        )
        self.conn.commit()
        return run_id

    def event(self, event_id: str, at: datetime, asset: str, direction: str, demand: str) -> None:
        self.conn.execute(
            "INSERT INTO events (event_id, dedup_key, ts, type, asset, direction, model_demand, status) "
            "VALUES (?, ?, ?, 'RADAR_ALERT', ?, ?, ?, 'PENDING')",
            (event_id, f"preview-{event_id}", at.isoformat(), asset, direction, demand),
        )
        self.conn.commit()

    def wallet(self) -> None:
        ps.ensure_schema(self.conn)
        ps.ensure_wallet(self.conn, start_balance=Decimal("1000"), currency="EUR", now=self.at(480))

    def open(
        self, asset: str, direction: str, setup: str, at: datetime, mid: float, demand: str = "IGNORE"
    ) -> ps.StoredPlay:
        """An EX-1 play opened through the real store, with a fixture ATR of its own pair."""
        run_id = self.run(at)
        event_id = f"preview-{asset.lower()}-{int(at.timestamp())}"
        self.event(event_id, at, asset, direction, demand)
        bid, ask = self.spot(asset, at, mid)
        pair = f"{asset}/{self.quote(asset)}"
        candidate = ps.PaperCandidate(
            event_id=event_id, run_id=run_id, asset=asset, pair=pair, quote=self.quote(asset), direction=direction,
            bid=bid, ask=ask, snapshot_ts=at.isoformat(), status="online", why=_why(direction, setup, self.rng),
            atr=mid * ATR_FRACTION, atr_pair=pair,  # a float, like the L2 feature it comes from
        )
        report = ps.open_candidates(self.conn, [candidate], terms=TERMS, now=at)
        if len(report.opened) != 1:
            raise PreviewError(f"fixture play {asset} was not opened: {report.skipped}")
        return report.opened[0]

    def open_legacy(self, asset: str, direction: str, setup: str, at: datetime, mid: float) -> None:
        run_id = self.run(at)
        event_id = f"preview-{asset.lower()}-{int(at.timestamp())}"
        self.event(event_id, at, asset, direction, "IGNORE")
        bid, ask = self.spot(asset, at, mid)
        # A legacy row (no EX-1 columns), as the pre-EX-1 store wrote it.
        self.conn.execute(
            "INSERT INTO paper_plays (event_id, run_id, asset, pair, quote, direction, stake_cents, fee_bps, "
            "hold_minutes, entry_bid, entry_ask, entry_ts, due_at, why_json, opened_at) "
            "VALUES (?, ?, ?, ?, 'EUR', ?, 10000, '26', ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id, run_id, asset, f"{asset}/EUR", direction, LEGACY_HOLD_MINUTES,
                str(Decimal(repr(bid))), str(Decimal(repr(ask))), ps.utc_text(at),
                ps.utc_text(at + timedelta(minutes=LEGACY_HOLD_MINUTES)),
                json.dumps(_why(direction, setup, self.rng), sort_keys=True), ps.utc_text(at),
            ),
        )
        self.conn.commit()

    def legacy_closes(self) -> None:
        for asset, direction, setup, ago, mid, move, delay in LEGACY_CLOSES:
            opened = self.at(ago)
            self.open_legacy(asset, direction, setup, opened, mid)
            due = opened + timedelta(minutes=LEGACY_HOLD_MINUTES)
            if delay:
                ps.settle_due(self.conn, now=due + timedelta(minutes=1))  # no quote yet: it stays pending
            exit_at = due + timedelta(minutes=delay)
            self.spot(asset, exit_at, mid * (1 + move / 100))
            report = ps.settle_due(self.conn, now=exit_at + timedelta(minutes=1))
            if len(report.closed) != 1:
                raise PreviewError(f"fixture play {asset} did not close")

    def ex1_closes(self, rows: Sequence[tuple[str, str, str, int, float, str, int, str]] = EX1_CLOSES) -> None:
        """The EX-1 plays, opened in time order and then closed by the real store in the order
        their quotes reach the stop, the target or the time limit; a ticker quote is offered
        the way the monitor offers it."""
        exits: list[tuple[datetime, float, tuple[str, str, str, int, float, str, int, str], ps.StoredPlay]] = []
        for row in sorted(rows, key=lambda item: -item[3]):
            asset, direction, setup, ago, mid, reason, touch_after, _ = row
            play = self.open(asset, direction, setup, self.at(ago), mid)
            if play.stop is None or play.target is None:
                raise PreviewError(f"fixture play {asset} has no exit levels")
            if reason == "time":
                exit_at = play.due_at + timedelta(minutes=2)
                exit_mid = mid * (1 + TIME_EXIT_MOVE / 100)
            else:
                level = float(play.stop if reason == "stop" else play.target)
                exit_at = play.entry_ts + timedelta(minutes=touch_after)
                exit_mid = level * (1 + TOUCH_FRACTION if level > mid else 1 - TOUCH_FRACTION)
            exits.append((exit_at, exit_mid, row, play))
        for exit_at, exit_mid, row, play in sorted(exits, key=lambda item: item[0]):
            self.ex1_close(row, play, exit_at, exit_mid)

    def ex1_close(
        self, row: tuple[str, str, str, int, float, str, int, str], play: ps.StoredPlay, exit_at: datetime,
        exit_mid: float,
    ) -> None:
        asset, reason, source = row[0], row[5], row[7]
        if source == TICKER_SOURCE:
            half = exit_mid * 0.00008
            quote = ps.ObservedQuote(
                play.pair, Decimal(repr(round(exit_mid - half, 6))), Decimal(repr(round(exit_mid + half, 6))), exit_at,
                TICKER_SOURCE,
            )
            report = ps.close_plays(self.conn, now=exit_at + timedelta(seconds=4), extra_quotes=[quote])
        else:
            self.spot(asset, exit_at, exit_mid)
            report = ps.settle_due(self.conn, now=exit_at + timedelta(minutes=1))
        closed = report.closed[0] if len(report.closed) == 1 else None
        if closed is None or closed.exit_reason is None or closed.exit_reason.value != reason:
            raise PreviewError(f"fixture play {asset} did not close on its {reason}")

    def drift(self, asset: str, opened: datetime, ago: int, mid: float, move: float) -> None:
        for step in range(1, ago // 2 + 1):  # a quote every two minutes since entry
            drift = move * step / (ago // 2) + self.rng.uniform(-0.12, 0.12)
            self.spot(asset, opened + timedelta(minutes=2 * step), mid * (1 + drift / 100))

    def build_game(self) -> None:
        self.wallet()
        self.legacy_closes()
        self.ex1_closes()
        for asset, direction, setup, ago, mid, move in OPEN_PLAYS:
            opened = self.at(ago)
            self.open(asset, direction, setup, opened, mid, demand="SONNET" if asset == "SOL" else "IGNORE")
            self.drift(asset, opened, ago, mid, move)
        cycle = self.run(self.at(1))
        qrs.record_batch(self.conn, [qrs.QwenReviewRow(
            run_id=cycle, cycle_ts=self.at(1).isoformat(), mode="shadow", asset="SOL", setup_type="BREAKOUT",
            direction="LONG", anomaly_score=None, opportunity_score=None, tradeability_score=None, router_decision=None,
            batch_status="OK", veto=False, confidence="HIGH", review_direction="LONG", call_sonnet=True,
            call_fable=False, attempts=1,
        )], now=self.at(0.5))

    def build_legacy(self) -> None:
        """Only plays opened before the EX-1 policy: no stop, target or close reason anywhere."""
        self.wallet()
        self.legacy_closes()
        asset, direction, setup, ago, mid, move = LEGACY_OPEN
        opened = self.at(ago)
        self.open_legacy(asset, direction, setup, opened, mid)
        self.drift(asset, opened, ago, mid, move)
        self.run(self.at(1))

    def build_empty(self) -> None:
        self.wallet()
        self.run(self.at(6))
        self.run(self.at(1))

    def build_stale(self) -> None:
        """USD-priced closes, a SOL/EUR play priced until now and a newer ETH/USD play whose
        last price is ``STALE_QUOTE_MINUTES`` old: the wallet's total value is unknown."""
        self.quotes.update(STALE_QUOTES)
        self.wallet()
        self.ex1_closes(STALE_CLOSES)
        plays = (STALE_FRESH, STALE_OLD)
        for asset, direction, setup, ago, mid, _ in plays:
            self.open(asset, direction, setup, self.at(ago), mid)
        for asset, _, _, ago, mid, move in plays:
            priced = ago if asset == STALE_FRESH[0] else ago - STALE_QUOTE_MINUTES
            self.drift(asset, self.at(ago), priced, mid, move)
        self.run(self.at(1))

    def qwen(self, run_id: str, at: datetime, asset: str, direction: str) -> None:
        qrs.record_batch(self.conn, [qrs.QwenReviewRow(
            run_id=run_id, cycle_ts=at.isoformat(), mode="shadow", asset=asset, setup_type="BREAKOUT",
            direction=direction, anomaly_score=None, opportunity_score=None, tradeability_score=None,
            router_decision=None, batch_status="OK", veto=False, confidence="HIGH", review_direction=direction,
            call_sonnet=True, call_fable=False, attempts=1,
        )], now=at)


# The replay: each step writes at its own time, oldest first.
REPLAY_STEP_MINUTES = 2
REPLAY_SOL = ("SOL", "LONG", "BREAKOUT", 142.31)
REPLAY_AVAX = ("AVAX", "SHORT", "REVERSAL", 24.118)


def _replay_steps() -> tuple[tuple[str, Any], ...]:
    sol, sol_dir, sol_setup, sol_mid = REPLAY_SOL
    avax, avax_dir, avax_setup, avax_mid = REPLAY_AVAX

    def quiet(f: Fixture, t: datetime) -> None:
        f.wallet()
        f.run(t - timedelta(minutes=3))

    def cycle(f: Fixture, t: datetime) -> None:
        f.run(t)

    def open_sol(f: Fixture, t: datetime) -> None:
        f.open(sol, sol_dir, sol_setup, t, sol_mid, demand="SONNET")

    def review(f: Fixture, t: datetime) -> None:
        f.qwen(f.run(t), t, sol, sol_dir)
        f.spot(sol, t, sol_mid * 1.0021)

    def quotes(f: Fixture, t: datetime) -> None:
        f.run(t)
        f.spot(sol, t - timedelta(minutes=1), sol_mid * 1.0034)
        f.spot(sol, t, sol_mid * 1.0029)

    def open_avax(f: Fixture, t: datetime) -> None:
        f.open(avax, avax_dir, avax_setup, t, avax_mid, demand="FABLE")
        f.spot(sol, t, sol_mid * 1.0046)

    def later(f: Fixture, t: datetime) -> None:
        f.run(t)
        f.spot(sol, t, sol_mid * 1.0052)
        f.spot(avax, t, avax_mid * 0.9991)

    return (
        ("quiet office", quiet),
        ("a radar cycle", cycle),
        ("an alert opens a SOL play; the router says Sonnet", open_sol),
        ("a Qwen review of SOL", review),
        ("another cycle and new prices", quotes),
        ("an alert opens an AVAX play; the router says Fable", open_avax),
        ("another cycle", later),
    )


def build_replay(workdir: Path, now: datetime) -> list[dict[str, Any]]:
    """The replay payloads: one fixture database read right after each step, oldest first."""
    steps = _replay_steps()
    first = now - timedelta(minutes=REPLAY_STEP_MINUTES * (len(steps) - 1))
    path = workdir / "replay.sqlite"
    fixture = Fixture(path, now)
    reader = PaperReader(str(path), enabled=True, qwen_enabled=True, default_params=DEFAULTS)
    payloads: list[dict[str, Any]] = []
    try:
        for index, (label, write) in enumerate(steps):
            moment = first + timedelta(minutes=REPLAY_STEP_MINUTES * index)
            write(fixture, moment)
            payload = reader.read(moment + timedelta(seconds=30))
            payload["replay_step"] = {"index": index + 1, "total": len(steps), "label": label}
            payloads.append(payload)
    finally:
        fixture.close()
    return payloads


def validate_replay(replay: Sequence[Mapping[str, Any]]) -> None:
    ids = [{entry["id"] for entry in payload["activity"]} for payload in replay]
    kinds = {entry["kind"] for entry in replay[-1]["activity"]} if replay else set()
    checks = {
        "replay has several steps": len(replay) >= 5,
        "every replay step is available": all(p["available"] is True for p in replay),
        "every replay step adds activity and keeps the old": all(
            before < after for before, after in zip(ids, ids[1:], strict=False)
        ),
        "replay starts with no open play": bool(replay) and not replay[0]["open_plays"],
        "replay ends with two open plays": bool(replay) and len(replay[-1]["open_plays"]) == 2,
        "replay reaches every routed agent": {"cycle", "alert", "qwen", "sonnet", "fable", "play_open"} <= kinds,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise PreviewError("replay check failed: " + "; ".join(failed))


# --- the Pilot shadow panel's samples -----------------------------------------------------------

PILOT_SCENARIOS = ("open", "stale", "locked", "empty")
PILOT_FEE_BPS = Decimal("26")
#: AssetPairs-like rules for the sample pairs (sample values, never fetched).
PILOT_RULES: dict[str, dict[str, object]] = {
    "SOL/EUR": {"lot_decimals": 8, "ordermin": "0.02", "costmin": "0.5", "tick_size": "0.01"},
    "XRP/EUR": {"lot_decimals": 8, "ordermin": "2", "costmin": "0.5", "tick_size": "0.00001"},
    "ADA/EUR": {"lot_decimals": 8, "ordermin": "5", "costmin": "0.5", "tick_size": "0.000001"},
}


def _pilot_candidate(
    event_id: str, asset: str, quote: str, direction: str, touch: tuple[str, str], atr: str, at: datetime
) -> pilot_store.PilotCandidate:
    pair = f"{asset}/{quote}"
    return pilot_store.PilotCandidate(
        event_id=event_id, run_id=f"preview-{event_id}", asset=asset, pair=pair, quote=quote, direction=direction,
        bid=Decimal(touch[0]), ask=Decimal(touch[1]), snapshot_ts=at.isoformat(), status="online", atr=Decimal(atr),
        atr_pair=pair, pair_entry=PILOT_RULES.get(pair),
    )


def _pilot_spot(conn: sqlite3.Connection, pair: str, at: datetime, bid: float, ask: float) -> None:
    asset, quote = pair.split("/")
    conn.execute(
        "INSERT INTO spot_snapshots (asset, pair, quote, ts, bid, ask, status) VALUES (?, ?, ?, ?, ?, ?, 'online')",
        (asset, pair, quote, at.isoformat(), bid, ask),
    )
    conn.commit()


def _pilot_offer(conn: sqlite3.Connection, envelope: risk.Envelope, at: datetime, *offers: tuple[Any, ...]) -> None:
    """Offer ``(event_id, asset, quote, direction, (bid, ask), atr)`` candidates seen at ``at``."""
    candidates = [
        _pilot_candidate(event_id, asset, quote, direction, touch, atr, at)
        for event_id, asset, quote, direction, touch, atr in offers
    ]
    pilot_store.open_candidates(conn, candidates, envelope=envelope, fee_bps=PILOT_FEE_BPS, now=at)


SOL = ("SOL", "EUR", "LONG", ("121.34", "121.40"), "0.62")
XRP = ("XRP", "EUR", "LONG", ("0.48211", "0.48220"), "0.00150")
ADA = ("ADA", "EUR", "LONG", ("0.35012", "0.35020"), "0.00092")
ETH_USD = ("ETH", "USD", "LONG", ("2381.4", "2381.5"), "4.1")


#: How old the open pilot position's last price is: fresh in ``open``, past the reporting
#: freshness bound in ``stale``.
PILOT_MARK_AGE = timedelta(minutes=3)
PILOT_STALE_MARK_AGE = timedelta(minutes=25)


def _pilot_open(
    conn: sqlite3.Connection, envelope: risk.Envelope, now: datetime, mark_age: timedelta = PILOT_MARK_AGE
) -> None:
    """Earlier refusals, then one SOL/EUR entry (the notional limit sets its size) marked a little up
    ``mark_age`` ago."""
    start = now - timedelta(hours=5)
    _pilot_offer(conn, envelope, start + timedelta(minutes=10),
                 ("pv-btc", "BTC", "USD", "LONG", ("64210.1", "64210.2"), "88.4"))
    _pilot_offer(conn, envelope, start + timedelta(minutes=40),
                 ("pv-doge", "DOGE", "EUR", "SHORT", ("0.10212", "0.10215"), "0.00041"))
    _pilot_offer(conn, envelope, now - timedelta(hours=2, minutes=14), ("pv-sol", *SOL))
    _pilot_spot(conn, "SOL/EUR", now - mark_age, 121.71, 121.77)
    _pilot_offer(conn, envelope, now - timedelta(minutes=48), ("pv-xrp", *XRP), ("pv-eth", *ETH_USD))
    pilot_store.evaluate_locks(conn, now=now - timedelta(minutes=1))  # the radar marks the account each cycle


def _pilot_locked(conn: sqlite3.Connection, envelope: risk.Envelope, now: datetime) -> None:
    """A gap far through the stop trips both locks, a review clears the drawdown one, the kill switch goes on."""
    opened = now - timedelta(hours=4)
    _pilot_offer(conn, envelope, opened, ("pv-sol", *SOL))
    gap = opened + timedelta(minutes=37)
    # The radar marks the account each cycle, so the gap's UTC day has its day start before the gap.
    pilot_store.evaluate_locks(conn, now=gap)
    _pilot_spot(conn, "SOL/EUR", gap, 82.10, 82.30)
    pilot_store.close_positions(conn, now=gap + timedelta(seconds=5))
    refused = gap + timedelta(minutes=20)
    _pilot_offer(conn, envelope, refused, ("pv-xrp", *XRP))
    drawdown = next(lock for lock in pilot_store.active_locks(conn) if lock.kind is risk.LockKind.DRAWDOWN)
    pilot_store.review_lock(conn, lock_id=drawdown.lock_id, reviewer="operator", cause="SOL gap checked; the stop worked",
                            now=refused + timedelta(minutes=25))
    pilot_store.engage_kill_switch(conn, reason="pause until the daily lock is reviewed", actor="operator",
                                   now=refused + timedelta(minutes=30))
    _pilot_offer(conn, envelope, refused + timedelta(minutes=70), ("pv-ada", *ADA))
    _pilot_offer(conn, envelope, refused + timedelta(minutes=95), ("pv-eth", *ETH_USD))
    _pilot_offer(conn, envelope, refused + timedelta(minutes=130),
                 ("pv-sol2", "SOL", "EUR", "LONG", ("83.02", "83.08"), "0.71"))
    pilot_store.evaluate_locks(conn, now=now - timedelta(minutes=1))


def build_pilot_payloads(workdir: Path, now: datetime) -> dict[str, dict[str, Any]]:
    """One ``get_pilot_state`` payload per pilot scenario, from temporary databases under ``workdir``."""
    envelope = risk.Envelope(Decimal("240.00"), "EUR")
    builders: dict[str, Callable[[sqlite3.Connection, risk.Envelope, datetime], None] | None] = {
        "open": _pilot_open,
        "stale": functools.partial(_pilot_open, mark_age=PILOT_STALE_MARK_AGE),
        "locked": _pilot_locked,
        "empty": None,
    }
    payloads: dict[str, dict[str, Any]] = {}
    for name, build in builders.items():
        path = workdir / f"pilot-{name}.sqlite"
        SnapshotStore(str(path)).close()
        if build is not None:
            conn = sqlite3.connect(path)
            try:
                pilot_store.ensure_schema(conn)
                pilot_store.ensure_account(conn, envelope=envelope, now=now - timedelta(hours=6))
                build(conn, envelope, now)
            finally:
                conn.close()
        payloads[name] = PilotReader(str(path), enabled=True).read(now)
    return payloads


def validate_pilot_payloads(payloads: Mapping[str, Mapping[str, Any]]) -> None:
    opened, stale, locked, empty = payloads["open"], payloads["stale"], payloads["locked"], payloads["empty"]
    counts = {name: {c["reason"]: c["count"] for c in p["no_trade"]["counts"]} for name, p in payloads.items()}
    checks = {
        "open pilot has one open position": opened["available"] is True and opened["open_count"] == 1
        and opened["open_position"]["pair"] == "SOL/EUR",
        "open pilot has no lock and the kill switch off": not opened["locks"]["active"]
        and opened["kill_switch"]["engaged"] is False,
        "open pilot's last sizing opened the position": opened["last_sizing"]["event_id"] == "pv-sol"
        and opened["last_sizing"]["outcome"] == pilot_store.OPENED,
        "open pilot has NO_TRADE reasons": counts["open"]["quote_currency_mismatch"] == 1
        and counts["open"]["unsupported_direction"] == 1 and counts["open"]["position_already_open"] == 2,
        "locked pilot has an active daily lock": [lock["kind"] for lock in locked["locks"]["active"]] == ["daily_loss"],
        "locked pilot has a reviewed drawdown lock": any(
            lock["kind"] == "drawdown" and lock["review"] is not None for lock in locked["locks"]["recent"]
        ),
        "locked pilot has the kill switch engaged": locked["kill_switch"]["engaged"] is True,
        "locked pilot refused on the lock and the switch": counts["locked"]["daily_loss_lock"] == 1
        and counts["locked"]["kill_switch_engaged"] == 3,
        "empty pilot has not started": empty["available"] is False and empty["reason"] == pilot_reader.REASON_NO_TABLES,
        "every pilot payload is pretend money": all(p["pretend_money"] is True for p in payloads.values()),
        "every pilot payload is a PAPER account": all(
            p["account_mode"] == pilot_reader.ACCOUNT_MODE for p in payloads.values()
        ),
        "open pilot is valued on a fresh price": opened["valuation"]["mark_status"] == paper_reader.ALL_MARKED
        and opened["valuation"]["total_equity"] is not None,
        "stale pilot's position has no fresh price": stale["valuation"]["mark_status"] == paper_reader.UNAVAILABLE
        and [u["status"] for u in stale["valuation"]["unmarked"]] == [paper_reader.STALE_QUOTE]
        and all(stale["valuation"][key] is None for key in ("total_equity", "liquidation_value", "open_net_pnl"))
        and stale["valuation"]["free_cash"] is not None,
        "the open position's fee is ASSUMED": all(
            p["open_position"]["fee_source"] == paper_reader.FEE_SOURCE
            and p["open_position"]["account_tier_verified"] is False
            for p in (opened, stale)
        ),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise PreviewError("pilot payload check failed: " + "; ".join(failed))
    json.dumps(payloads)  # must cross to JS as plain JSON


# --- the Trend paper panel's samples ------------------------------------------------------------

TREND_SCENARIOS = ("populated", "empty", "skipped", "refused")
#: The synthetic clock of the trend samples: 14 paper days (2026-10-04 to 2026-10-17) are due at
#: 08:00 UTC on 2026-10-17; the empty sample is read the evening before the first paper day.
TREND_NOW = datetime(2026, 10, 17, 8, tzinfo=UTC)
TREND_BEFORE = datetime(2026, 10, 3, 21, tzinfo=UTC)
TREND_FX = 1.08  # synthetic USDT per EUR, around which the daily rate moves a little
#: Synthetic daily moves of the last weeks (fractions), so the books both gain and lose.
#: The ``skipped`` sample's synthetic market lacks these candles for good (a later candle follows each),
#: so its catch-up confirms them with a second request and records both days as skipped
#: (no EUR pair and no EURUSDT open on the day: no EUR price).
TREND_GAPS: Mapping[str, frozenset[date]] = {
    "BTCEUR": frozenset({date(2026, 10, 8)}),
    "ETHEUR": frozenset({date(2026, 10, 12)}),
    "EURUSDT": frozenset({date(2026, 10, 8), date(2026, 10, 12)}),
}
TREND_TAIL = (0.021, 0.013, -0.006, 0.018, -0.031, -0.024, 0.009, 0.015, -0.012, -0.027, 0.006, -0.019, 0.011,
              -0.008, 0.004, 0.017, -0.014, 0.003)


def _fx(day: date) -> float:
    """The synthetic USDT per EUR of ``day``: within 0.35% of ``TREND_FX``."""
    return round(TREND_FX * (1 + 0.0035 * ((day.toordinal() % 9) - 4) / 4), 5)


class SyntheticMarket:
    """A deterministic offline daily market for the trend paper samples (never fetched, not market
    history): a seeded walk from the listing day for each USDT pair with its last ``TREND_TAIL``
    days fixed, EUR pairs at a daily rate near ``TREND_FX`` and a still-open candle on ``now``'s day.
    ``gaps`` names closed candles the market never returns (per symbol, by day)."""

    def __init__(self, now: datetime, gaps: Mapping[str, frozenset[date]] | None = None) -> None:
        self.series: dict[str, trend_p.DailySeries] = {}
        rng = random.Random(11)
        last = now.date() - timedelta(days=1)
        tail_from = last - timedelta(days=len(TREND_TAIL) - 1)
        for asset, usdt in trend_p.USDT_SYMBOL.items():
            bars: list[trend_e.Bar] = []
            price = {"BTC": 4300.0, "ETH": 300.0}[asset]
            day = trend_p.BTCUSDT_LISTING_DAY
            while day <= last:
                if day >= tail_from:
                    move = TREND_TAIL[(day - tail_from).days] * (1.0 if asset == "BTC" else 1.3)
                else:
                    move = rng.gauss(0.0012, 0.035)
                close = round(max(price * (1 + move), 1.0), 2)
                bars.append(trend_e.Bar(trend_e.day_open_ms(day), price, max(price, close), min(price, close), close))
                price = close
                day += timedelta(days=1)
            self.series[usdt] = trend_p.DailySeries(usdt, tuple(bars), now.date(), price)
            eur_symbol = trend_p.EUR_SYMBOL[asset]
            eur = tuple(
                trend_e.Bar(b.open_time_ms, b.open / rate, b.high / rate, b.low / rate, b.close / rate)
                for b in bars
                for rate in (_fx(trend_e.utc_day(b.open_time_ms)),)
            )
            self.series[eur_symbol] = trend_p.DailySeries(eur_symbol, eur, now.date(), price / _fx(now.date()))
        fx = tuple(
            trend_e.Bar(b.open_time_ms, rate, rate, rate, rate)
            for b in self.series["BTCUSDT"].closed
            for rate in (_fx(trend_e.utc_day(b.open_time_ms)),)
        )
        self.series[trend_p.EURUSDT] = trend_p.DailySeries(trend_p.EURUSDT, fx, now.date(), _fx(now.date()))
        for symbol, days in (gaps or {}).items():
            s = self.series[symbol]
            kept = tuple(b for b in s.closed if trend_e.utc_day(b.open_time_ms) not in days)
            self.series[symbol] = trend_p.DailySeries(symbol, kept, s.live_day, s.live_open)

    def fetch_daily(self, symbol: str, since: date, now_ms: int) -> trend_p.DailySeries:
        s = self.series[symbol]
        bars = tuple(b for b in s.closed if b.open_time_ms >= trend_e.day_open_ms(since))
        return trend_p.DailySeries(symbol, bars, s.live_day, s.live_open)

    def close(self) -> None:
        pass


def _fixed(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def build_trend_payloads(workdir: Path) -> dict[str, dict[str, Any]]:
    """One ``get_trend_paper_state`` payload per trend scenario, each from its own state dir under
    ``workdir``; the real ledger is never opened."""
    payloads: dict[str, dict[str, Any]] = {}
    for name in TREND_SCENARIOS:
        state_dir = workdir / f"trend-{name}"
        state_dir.mkdir()
        moment = TREND_BEFORE if name == "empty" else TREND_NOW
        if name != "empty":
            gaps = TREND_GAPS if name == "skipped" else None
            trend_paper_hook.catch_up(state_dir, SyntheticMarket(moment, gaps), clock=_fixed(moment))
        if name == "refused":
            path = trend_paper_store.ledger_path(state_dir)
            data = path.read_bytes()
            path.write_bytes(data.replace(b'"fee_rate":0.001', b'"fee_rate":0.0001', 1))  # one edited record
        payloads[name] = TrendReader(state_dir, clock=_fixed(moment)).read()
    return payloads


def validate_trend_payloads(payloads: Mapping[str, Mapping[str, Any]], workdir: Path) -> None:
    populated, empty, refused = payloads["populated"], payloads["empty"], payloads["refused"]
    skipped = payloads["skipped"]
    rows = [row for group in populated["quotes"] for row in group["rows"]]
    figures = ("equity", "return_pct", "max_drawdown_pct", "fees", "vs_buy_hold_pp", "last_date")
    checks = {
        "populated trend books 14 days": populated["state"] == trend_reader.STATE_OK
        and populated["days_booked"] == 14 and populated["last_day"] == "2026-10-17",
        "populated trend shows EUR then USDT": [g["quote"] for g in populated["quotes"]] == ["EUR", "USDT"],
        "populated trend has every strategy at both fees": len(rows) == 16
        and {r["label"] for r in rows} == {label for _, label in trend_reader.STRATEGIES}
        and {r["fee_pct"] for r in rows} == {"0.1", "0.4"},
        "populated trend has every figure": all(r[key] is not None for r in rows for key in figures)
        and all(r["assets"] for r in rows),
        "populated trend has gains and losses": any(r["return_pct"].startswith("-") for r in rows)
        or any(r["vs_buy_hold_pp"].startswith("-") for r in rows),
        "skipped trend books 12 days and lists the 2 skipped ones": skipped["state"] == trend_reader.STATE_OK
        and skipped["days_booked"] == 12 and skipped["days_skipped"] == 2 and skipped["last_day"] == "2026-10-17"
        and [d["day"] for d in skipped["skipped_days"]] == ["2026-10-08", "2026-10-12"]
        and all("no EUR price" in d["reason"] for d in skipped["skipped_days"]),
        "empty trend has no book": empty["state"] == trend_reader.STATE_EMPTY and empty["quotes"] == [],
        "refused trend shows no figure": refused["state"] == trend_reader.STATE_REFUSED and refused["quotes"] == []
        and refused["error"] is not None,
        "every trend payload carries the honesty label": all(
            p["honesty_label"] == trend_reader.HONESTY_LABEL for p in payloads.values()
        ),
        "every trend payload carries the skipped days": all(
            isinstance(p["days_skipped"], int) and p["days_skipped"] == len(p["skipped_days"]) for p in payloads.values()
        )
        and populated["days_skipped"] == 0,
        "trend samples stay in the preview directory": all(
            trend_paper_store.ledger_path(workdir / f"trend-{name}").resolve().is_relative_to(workdir.resolve())
            for name in TREND_SCENARIOS
        ),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise PreviewError("trend payload check failed: " + "; ".join(failed))
    json.dumps(payloads)  # must cross to JS as plain JSON


def build_payloads(workdir: Path, now: datetime) -> dict[str, dict[str, Any]]:
    """One ``get_paper_state`` payload per scenario, all from temporary databases under ``workdir``."""
    builders = {
        "open": Fixture.build_game, "legacy": Fixture.build_legacy, "empty": Fixture.build_empty,
        "stale": Fixture.build_stale, "unavailable": None, "disabled": Fixture.build_game,
    }
    payloads: dict[str, dict[str, Any]] = {}
    for name, build in builders.items():
        path = workdir / f"{name}.sqlite"
        fixture = Fixture(path, now)
        try:
            if build is not None:
                build(fixture)
        finally:
            fixture.close()
        reader = PaperReader(str(path), enabled=name != "disabled", qwen_enabled=True, default_params=DEFAULTS)
        payloads[name] = reader.read(now)
    return payloads


def validate_payloads(payloads: Mapping[str, Mapping[str, Any]]) -> None:
    game, legacy, empty, stale = payloads["open"], payloads["legacy"], payloads["empty"], payloads["stale"]
    valued = stale["wallet"]["valuation"]
    plan = ("exit_policy", "stop", "target", "max_hold_minutes", "exit_due_at")
    checks = {
        "open scenario is available": game["available"] is True,
        "open scenario has open plays": len(game["open_plays"]) == len(OPEN_PLAYS),
        "open scenario has every closed play": game["history_total"] == CLOSED_COUNT,
        "open scenario has a balance series": len(game["wallet"]["series"]) == CLOSED_COUNT + 1,
        "open scenario has activity": len(game["activity"]) > 0,
        "open plays carry their recorded exit plan": all(
            all(play[key] is not None for key in plan) for play in game["open_plays"]
        ),
        "open scenario has a close for each reason": sorted(
            h["exit_reason"] for h in game["history"] if h["exit_reason"] is not None
        ) == sorted(row[5] for row in EX1_CLOSES),
        "legacy closes have no reason": sum(
            h["exit_reason"] is None and h["exit_reason_text"] is None for h in game["history"]
        ) == len(LEGACY_CLOSES),
        "a close carries the ticker source": any(h["exit_source"] == TICKER_SOURCE for h in game["history"]),
        "default hold is the 24 h policy": game["params"]["hold_minutes"] == paper.EX1_MAX_HOLD_MINUTES
        and legacy["params"]["hold_minutes"] == paper.EX1_MAX_HOLD_MINUTES,
        "legacy open play has no exit plan": len(legacy["open_plays"]) == 1
        and all(legacy["open_plays"][0][key] is None for key in plan),
        "legacy history has no reason": bool(legacy["history"])
        and all(h["exit_reason"] is None for h in legacy["history"]),
        "empty scenario has a wallet and no plays": empty["available"] is True and not empty["open_plays"]
        and not empty["history"],
        "unavailable scenario is not available": payloads["unavailable"]["available"] is False,
        "disabled scenario says so": payloads["disabled"]["reason"] == paper_reader.REASON_DISABLED,
        "every payload is pretend money": all(p["pretend_money"] is True for p in payloads.values()),
        "open scenario is valued on fresh prices": game["wallet"]["valuation"]["mark_status"] == paper_reader.ALL_MARKED,
        "empty scenario has nothing open to value": empty["wallet"]["valuation"]["mark_status"]
        == paper_reader.NO_OPEN_POSITIONS,
        "stale scenario's ETH/USD play has no fresh price": [(u["pair"], u["status"]) for u in valued["unmarked"]]
        == [(STALE_OLD[0] + "/USD", paper_reader.STALE_QUOTE)],
        "stale scenario's dependent totals are unknown, not zero": all(
            valued[key] is None for key in ("total_equity", "liquidation_value", "open_net_pnl")
        ) and all(valued[key] is not None for key in ("free_cash", "open_cost_basis", "realized_pnl")),
        "stale scenario labels its USD-priced plays FX excluded": any(p["fx_excluded"] for p in stale["open_plays"])
        and any(h["fx_excluded"] for h in stale["history"]) and not any(p["fx_excluded"] for p in game["open_plays"]),
        "every stored fee is ASSUMED, the tier unverified": all(
            item["fee_source"] == paper_reader.FEE_SOURCE and item["account_tier_verified"] is False
            for p in payloads.values() for item in [*p["open_plays"], *p["history"]]
        ),
    }
    wallet = game["wallet"]
    total = Decimal(wallet["start_balance"]) + sum(Decimal(h["net"]) for h in game["history"])
    checks["balance equals start plus every net result"] = Decimal(wallet["balance"]) == total
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise PreviewError("payload check failed: " + "; ".join(failed))
    json.dumps(payloads)  # must cross to JS as plain JSON


def _cents(value: object) -> Decimal:
    if not isinstance(value, str):
        raise PreviewError(f"expected a decimal string, got {value!r}")
    return Decimal(value)


def validate_valuation(payloads: Mapping[str, Mapping[str, Any]], pilot: Mapping[str, Mapping[str, Any]]) -> None:
    """The valuation identity on every preview payload that has one: ``total_equity = free_cash +
    liquidation_value = realized_balance + open_net_pnl``, the open plays add up to the totals,
    and a total that depends on an unpriced position is ``None`` (never zero)."""
    failed: list[str] = []
    dependent = ("total_equity", "liquidation_value", "open_net_pnl")
    for name, payload in payloads.items():
        wallet = payload.get("wallet")
        if not isinstance(wallet, Mapping) or not isinstance(wallet.get("valuation"), Mapping):
            continue
        v = wallet["valuation"]
        plays = payload["open_plays"]
        if _cents(v["free_cash"]) != _cents(v["realized_balance"]) - _cents(v["open_cost_basis"]):
            failed.append(f"{name}: free cash is not the realized balance minus the cost basis")
        if _cents(v["realized_pnl"]) != _cents(v["realized_balance"]) - _cents(wallet["start_balance"]):
            failed.append(f"{name}: realized P&L is not the realized balance minus the start")
        if _cents(v["open_cost_basis"]) != sum((_cents(p["cost_basis"]) for p in plays), Decimal(0)):
            failed.append(f"{name}: the cost basis is not the open plays' stakes")
        if v["unmarked"]:
            if any(v[key] is not None for key in dependent):
                failed.append(f"{name}: a valuation with an unpriced play has a total")
            continue
        total, liquidation, open_net = (_cents(v[key]) for key in dependent)
        if not total == _cents(v["free_cash"]) + liquidation == _cents(v["realized_balance"]) + open_net:
            failed.append(f"{name}: the total equity identity does not hold")
        if open_net != sum((_cents(p["open_net_pnl"]) for p in plays), Decimal(0)):
            failed.append(f"{name}: the open net is not the sum of the open plays")
        if liquidation != sum((_cents(p["liquidation_value"]) for p in plays), Decimal(0)):
            failed.append(f"{name}: the liquidation value is not the sum of the open plays")
    for name, payload in pilot.items():
        v = payload.get("valuation")
        if not isinstance(v, Mapping):
            continue
        exact = v["exact"]
        if v["unmarked"]:
            if any(exact[key] is not None for key in dependent):
                failed.append(f"pilot {name}: a valuation with an unpriced position has a total")
            continue
        total = Decimal(exact["total_equity"])
        if total != Decimal(exact["free_cash"]) + Decimal(exact["liquidation_value"]):
            failed.append(f"pilot {name}: total equity is not free cash plus the liquidation value")
        if total != _cents(v["realized_balance"]) + Decimal(exact["open_net_pnl"]):
            failed.append(f"pilot {name}: total equity is not the realized balance plus the open net")
        if Decimal(exact["open_net_pnl"]) != Decimal(exact["liquidation_value"]) - Decimal(exact["open_cost_basis"]):
            failed.append(f"pilot {name}: the open net is not the liquidation value minus the cost basis")
    if failed:
        raise PreviewError("valuation check failed: " + "; ".join(failed))


def stub_script(
    payloads: Mapping[str, Any],
    replay: Sequence[Mapping[str, Any]],
    pilot: Mapping[str, Any] | None = None,
    trend: Mapping[str, Any] | None = None,
) -> str:
    return (
        "/* Preview only: a stub of window.pywebview.api serving sample payloads. */\n"
        "(function () {\n"
        "  var PAYLOADS = " + json.dumps(payloads, sort_keys=True) + ";\n"
        "  var REPLAY = " + json.dumps(replay, sort_keys=True) + ";\n"
        "  var PILOT = " + json.dumps(pilot or {}, sort_keys=True) + ";\n"
        "  var TREND = " + json.dumps(trend or {}, sort_keys=True) + ";\n"
        "  var BANNER = " + json.dumps(BANNER_TEXT) + ";\n"
        "  var query = new URLSearchParams(window.location.search);\n"
        "  var replaying = query.get('scenario') === 'replay';\n"
        "  var scenario = PAYLOADS[query.get('scenario')] ? query.get('scenario') : 'open';\n"
        "  var pilotScenario = PILOT[query.get('pilot')] ? query.get('pilot') : 'open';\n"
        "  var trendScenario = TREND[query.get('trend')] ? query.get('trend') : 'populated';\n"
        "  var every = Math.max(1, parseInt(query.get('every') || '3', 10) || 3);\n"
        "  var polls = 0;\n"
        "  // The replay moves one step further every `every` polls and names the step in the banner.\n"
        "  function replayed() {\n"
        "    var payload = REPLAY[Math.min(REPLAY.length - 1, Math.floor(polls / every))];\n"
        "    polls += 1;\n"
        "    var step = payload.replay_step;\n"
        "    var banner = document.getElementById('preview-banner');\n"
        "    if (banner) banner.textContent = BANNER + ' \\u00b7 replay step ' + step.index + '/' + step.total + ': ' + step.label;\n"
        "    return payload;\n"
        "  }\n"
        "  var radar = query.get('radar') || 'RUNNING';\n"
        "  var state = {\n"
        "    process: { state: radar, pid: null, uptime_seconds: null, last_exit_code: null, last_error: null },\n"
        "    system_status: { kraken: 'UNKNOWN', kraken_futures: 'UNKNOWN', sqlite: 'UNKNOWN', qwen: 'UNKNOWN',\n"
        "      ntfy_enabled: false, claude_bridge: 'UNKNOWN' },\n"
        "    funnel: {}, next_full_cycle_eta_seconds: null, agents: [], agent_connections: [],\n"
        "    agent_communications: [], alerts_preview: [], latest_event: null\n"
        "  };\n"
        "  var api = {\n"
        "    get_paper_state: function () { return Promise.resolve(replaying ? replayed() : PAYLOADS[scenario]); },\n"
        "    get_pilot_state: function () {\n"
        "      var sample = PILOT[pilotScenario];\n"
        "      return sample ? Promise.resolve(sample) : Promise.reject(new Error('no pilot sample'));\n"
        "    },\n"
        "    get_trend_paper_state: function () {\n"
        "      var sample = TREND[trendScenario];\n"
        "      return sample ? Promise.resolve(sample) : Promise.reject(new Error('no trend sample'));\n"
        "    },\n"
        "    // The preview never fetches or writes: its catch-up answers after a short pause.\n"
        "    trend_paper_catch_up: function () {\n"
        "      return new Promise(function (resolve) { setTimeout(function () { resolve({ ok: true,\n"
        "        status: 'UP_TO_DATE', days_booked: 0, detail: 'Every due paper day is already booked.' }); }, 1200); });\n"
        "    },\n"
        "    get_state: function () { return Promise.resolve(state); },\n"
        "    get_ui_state: function () { return Promise.resolve({ last_tab: 'game' }); },\n"
        "    save_ui_state: function () { return Promise.resolve(null); },\n"
        "    list_alerts: function () { return Promise.resolve([]); },\n"
        "    list_history: function () { return Promise.resolve([]); },\n"
        "    list_operational_mock_alerts: function () { return Promise.resolve([]); },\n"
        "    system_info: function () { return Promise.resolve({}); }\n"
        "  };\n"
        "  window.pywebview = { api: new Proxy(api, { get: function (target, name) {\n"
        "    return name in target ? target[name] : function () {\n"
        "      return Promise.resolve({ ok: false, error: 'not available in the preview' }); };\n"
        "  } }) };\n"
        "  window.addEventListener('DOMContentLoaded', function () {\n"
        "    window.dispatchEvent(new Event('pywebviewready'));\n"
        "  });\n"
        "  // `&scroll=<element id>` keeps that element at the top while the first renders settle.\n"
        "  var scrollId = query.get('scroll');\n"
        "  if (scrollId) {\n"
        "    var tries = 0;\n"
        "    var timer = setInterval(function () {\n"
        "      var target = document.getElementById(scrollId);\n"
        "      if (target) target.scrollIntoView({ block: 'start' });\n"
        "      if (++tries >= 8) clearInterval(timer);\n"
        "    }, 400);\n"
        "  }\n"
        "})();\n"
    )


BANNER_HTML = (
    '<div id="preview-banner" role="note" style="position:fixed;z-index:9999;left:50%;bottom:10px;'
    'transform:translateX(-50%);padding:5px 12px;border-radius:999px;background:#3A2E12;color:#F5B94D;'
    'border:1px solid #6B5320;font:600 12px Inter,system-ui,sans-serif;pointer-events:none">'
    + BANNER_TEXT + "</div>"
)


def write_preview(
    target: Path,
    payloads: Mapping[str, Any],
    replay: Sequence[Mapping[str, Any]],
    pilot: Mapping[str, Any] | None = None,
    trend: Mapping[str, Any] | None = None,
) -> Path:
    """A copy of ui/web with the stub API loaded first and the sample-data banner."""
    shutil.copytree(WEB_DIR, target)
    index = target / "index.html"
    html = index.read_text(encoding="utf-8")
    first_script = '<script src="test_mode.js"></script>'
    if first_script not in html or "<body>" not in html:
        raise PreviewError("ui/web/index.html no longer has the expected <body> and first script")
    html = html.replace(first_script, f'<script src="{STUB_SCRIPT}"></script>\n{first_script}', 1)
    html = html.replace("<body>", "<body>\n" + BANNER_HTML, 1)
    index.write_text(html, encoding="utf-8")
    (target / STUB_SCRIPT).write_text(stub_script(payloads, replay, pilot, trend), encoding="utf-8")
    return target


def validate_preview(target: Path) -> None:
    html = (target / "index.html").read_text(encoding="utf-8")
    stub = (target / STUB_SCRIPT).read_text(encoding="utf-8")
    checks = {
        "banner present": BANNER_TEXT in html,
        "stub loads before the app": html.index(STUB_SCRIPT) < html.index('src="app.js"'),
        "game script present": (target / "paper_game.js").is_file() and 'src="paper_game.js"' in html,
        "office scripts present": all(
            (target / name).is_file() and f'src="{name}"' in html for name in ("paper_office_engine.js", "paper_office.js")
        ),
        "stub serves the replay": "REPLAY" in stub and "replay_step" in stub,
        "stub serves get_paper_state": "get_paper_state" in stub,
        "pilot shadow script and panel present": (target / "pilot_shadow.js").is_file()
        and 'src="pilot_shadow.js"' in html and 'id="pilot-shadow"' in html,
        "stub serves get_pilot_state": "get_pilot_state" in stub,
        "trend paper script and panel present": (target / "trend_paper.js").is_file()
        and 'src="trend_paper.js"' in html and 'id="trend-paper"' in html,
        "stub serves the trend paper and its catch-up": "get_trend_paper_state" in stub
        and "trend_paper_catch_up" in stub,
        "stub opens the Game tab": "last_tab: 'game'" in stub and 'id="tab-game"' in html,
        "no external resources": all(marker not in html for marker in ("http://", "https://", "//fonts.")),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise PreviewError("preview check failed: " + "; ".join(failed))


def real_database_path() -> Path:
    """Where the real radar database lives; only compared as a path, never opened."""
    from radar_v08 import config

    return Path(os.path.realpath(config.SQLITE_PATH))


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        pass


def serve(directory: Path, seconds: float | None) -> None:
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="paper-preview", daemon=True)
    try:
        thread.start()
        port = server.server_address[1]
        print(f"Serving the Game tab preview on http://127.0.0.1:{port}/index.html?scenario=open", flush=True)
        print("Scenarios: " + ", ".join(SCENARIOS) + ", replay. Ctrl+C stops it.", flush=True)
        deadline = None if seconds is None else time.monotonic() + seconds
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        print("Preview server stopped.", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--self-test", action="store_true", help="build and check the payloads and files, then exit")
    parser.add_argument("--seconds", type=float, default=None, help="stop the server after this many seconds")
    args = parser.parse_args(argv)

    real_database = real_database_path()
    now = datetime.now(UTC).replace(microsecond=0)
    with tempfile.TemporaryDirectory(prefix="paper-game-preview-") as tmp:
        workdir = Path(tmp)
        if real_database.is_relative_to(workdir.resolve()):
            raise PreviewError("the real database path must not be inside the preview directory")
        try:
            (workdir / "db").mkdir()
            payloads = build_payloads(workdir / "db", now)
            validate_payloads(payloads)
            replay = build_replay(workdir / "db", now)
            validate_replay(replay)
            pilot = build_pilot_payloads(workdir / "db", now)
            validate_pilot_payloads(pilot)
            validate_valuation(payloads, pilot)
            trend = build_trend_payloads(workdir / "db")
            validate_trend_payloads(trend, workdir / "db")
            target = write_preview(workdir / "web", payloads, replay, pilot, trend)
            validate_preview(target)
        except (PreviewError, ps.PaperStoreError, pilot_store.PilotStoreError, sqlite3.Error,
                trend_paper_store.TrendPaperStoreError, trend_p.PaperError, trend_e.EngineError) as error:
            print(f"Preview build failed: {error}", file=sys.stderr)
            return 1
        if args.self_test:
            print(
                f"Self-test OK: {len(payloads)} scenarios, {len(pilot)} pilot samples, {len(trend)} trend paper "
                f"samples and a {len(replay)}-step "
                "replay built and checked, and the valuation identity holds on every payload, "
                "in a temporary directory."
            )
        else:
            serve(target, args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
