"""Exposure-change alerts of the trend paper books. Paper only.

Pure: no I/O, clock or configuration. :mod:`radar_v08.trend_paper_hook` reads the dedupe file
(:mod:`radar_v08.adapters.trend_alert_store`) and shows the local toast.

* Rules: the four registered rules (:data:`ALERT_RULES`); the buy-and-hold comparators never alert.
* Reference book: each rule's USDT book at the lowest fee (``FEES[0]``). Signals come from the USDT
  closes, so every book of a rule rebalances on the same days; one book stands for the rule.
* Exposure change: the reference book records ``traded`` on at least one sleeve of a booked day.
  Old -> new exposure is ``held_before`` -> ``held_after`` of each traded sleeve.
* Decision day: the booked paper day (decided on the previous close, filled at its open). The
  dedupe key is (rule, decision day). When one catch-up books several days that change the same
  rule, only the latest change is shown; the earlier ones are superseded, so one catch-up shows at
  most one alert per rule.
* Skipped days book no record and never alert: only booked days are
  passed in, and a skip entry found among the records is never read as a book record.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from .domain.trend_paper import FEES, Rule, book_id

ALERT_RULES: tuple[Rule, ...] = (Rule.ENS, Rule.ENS_VT, Rule.BTC_TREND5, Rule.BTC_TREND5_VT)
REFERENCE_QUOTE = "USDT"
REFERENCE_FEE = FEES[0]
PAPER_ONLY = "paper only — no order placed"


class TrendAlertError(Exception):
    """The ledger records needed to detect a change are missing or malformed."""


@dataclass(frozen=True)
class SleeveChange:
    asset: str
    held_before: float
    held_after: float
    price: float


@dataclass(frozen=True)
class ExposureChange:
    rule: Rule
    day: date
    quote: str
    sleeves: tuple[SleeveChange, ...]  # traded sleeves only, in the book's sleeve order

    @property
    def key(self) -> tuple[str, str]:
        return (self.rule.value, self.day.isoformat())


@dataclass(frozen=True)
class AlertPlan:
    shown: tuple[ExposureChange, ...]  # the latest change of each rule, in ALERT_RULES order
    superseded: tuple[ExposureChange, ...]  # earlier changes of the same rules, handled silently


def reference_book(rule: Rule) -> str:
    return book_id(REFERENCE_QUOTE, rule, REFERENCE_FEE)


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrendAlertError(f"{what} is not a number")
    return float(value)


def _change(record: Mapping[str, Any], rule: Rule, day: date) -> ExposureChange | None:
    where = f"{day} {reference_book(rule)}"
    assets = record.get("assets")
    quote = record.get("quote")
    if not isinstance(assets, Mapping) or not isinstance(quote, str):
        raise TrendAlertError(f"{where}: malformed record")
    sleeves: list[SleeveChange] = []
    for asset, sleeve in assets.items():
        if not isinstance(sleeve, Mapping):
            raise TrendAlertError(f"{where}: malformed sleeve {asset}")
        if sleeve.get("traded") is True:
            sleeves.append(
                SleeveChange(
                    str(asset),
                    _number(sleeve.get("held_before"), f"{where} {asset} held_before"),
                    _number(sleeve.get("held_after"), f"{where} {asset} held_after"),
                    _number(sleeve.get("fill_price"), f"{where} {asset} fill_price"),
                )
            )
    return ExposureChange(rule, day, quote, tuple(sleeves)) if sleeves else None


def exposure_changes(records: Sequence[Mapping[str, Any]], days: Iterable[date]) -> list[ExposureChange]:
    """The exposure changes of the alert rules on ``days``, ordered by day then rule. Each day's
    reference records must be in ``records`` (a verified ledger); booked days are its tail, so the
    scan walks backwards and stops once every wanted record is found."""
    wanted = {(day.isoformat(), reference_book(rule)): (rule, day) for day in days for rule in ALERT_RULES}
    found: dict[tuple[str, str], Mapping[str, Any]] = {}
    for record in reversed(records):
        if len(found) == len(wanted):
            break
        if "kind" in record:  # a skip entry, not a book record
            continue
        key = (str(record.get("date")), str(record.get("book")))
        if key in wanted and key not in found:
            found[key] = record
    missing = sorted(set(wanted) - set(found))
    if missing:
        raise TrendAlertError(f"no ledger record for {missing[0][1]} on {missing[0][0]}")
    changes = [_change(found[key], *wanted[key]) for key in wanted]
    order = {rule: k for k, rule in enumerate(ALERT_RULES)}
    return sorted((c for c in changes if c is not None), key=lambda c: (c.day, order[c.rule]))


def plan_alerts(changes: Iterable[ExposureChange]) -> AlertPlan:
    latest: dict[Rule, ExposureChange] = {}
    earlier: list[ExposureChange] = []
    for change in sorted(changes, key=lambda c: c.day):
        previous = latest.get(change.rule)
        if previous is not None:
            earlier.append(previous)
        latest[change.rule] = change
    shown = tuple(latest[rule] for rule in ALERT_RULES if rule in latest)
    return AlertPlan(shown, tuple(sorted(earlier, key=lambda c: (c.day, ALERT_RULES.index(c.rule)))))


def percent(fraction: float) -> str:
    """``0.703`` -> ``70.3%``, ``1.0`` -> ``100%``, never ``-0%``."""
    text = f"{round(fraction * 100, 1) + 0.0:.1f}"
    return (text[:-2] if text.endswith(".0") else text) + "%"


def format_alert(change: ExposureChange) -> tuple[str, str]:
    """The toast title and body: one line per traded sleeve, then the decision day and
    :data:`PAPER_ONLY`."""
    title = f"Trend paper - {change.rule.value} exposure change"
    lines = [
        f"{s.asset} {percent(s.held_before)} -> {percent(s.held_after)} @ {s.price:.2f} {change.quote}"
        for s in change.sleeves
    ]
    lines.append(f"Paper day {change.day.isoformat()} (open) - {PAPER_ONLY}")
    return title, "\n".join(lines)
