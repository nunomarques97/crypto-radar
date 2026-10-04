"""Plain-English sentences for the paper game screen (DESIGN.md "AI Game").

Every sentence is built by a template from facts the backend recorded: the frozen ``why``
of a play, its recorded close, a radar_runs row, a qwen_reviews row, the router decision
recorded on a real alert. Nothing is estimated. A missing or unusable fact drops its clause; the fixed wording never
contains a digit, so every number in a sentence comes from the input (rounded only for
display). The words shown to the user avoid trading jargon: no "LONG"/"SHORT", no "bps",
no setup names.

The Strategist and the Boss only ever repeat the router's decision that an alert deserves
Sonnet or Fable: the Claude API stays off for paper trading (DESIGN.md, decision of
2026-09-29), so their lines never claim or imply that an analysis happened.

Pure: no I/O, no clock, no configuration. Numbers use English formatting (comma thousands
separators, point decimals: ``€1,000.00``, ``0.55%``), ready for a monospaced font. The
returned strings are plain text; the caller inserts them as text, never as HTML.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

__all__ = [
    "AGENT_FOR_KIND",
    "ACTIVITY_KINDS",
    "activity_line",
    "alert_line",
    "cost_sentence",
    "cycle_line",
    "decision_steps",
    "direction_text",
    "exit_reason_label",
    "fee_provenance_text",
    "freshness_text",
    "fx_excluded_text",
    "format_money",
    "format_number",
    "format_percent",
    "MARK_REASONS",
    "play_close_line",
    "PILOT_ACCOUNT_TEXT",
    "PILOT_REASONS",
    "play_open_line",
    "qwen_line",
    "REASON_DISABLED",
    "REASON_NO_DATABASE",
    "REASON_NO_PLAYS",
    "REASON_NO_TABLES",
    "REASON_NO_WALLET",
    "REASON_UNREADABLE",
    "result_sentence",
    "router_line",
    "VALUATION_BASIS_TEXT",
    "why_sentence",
]

CENT = Decimal("0.01")
_ONE_PLACE = Decimal("0.1")

UP = "LONG"
DOWN = "SHORT"

#: Which agent speaks for each activity kind (DESIGN.md "AI Game"). ``sonnet``/``fable``
#: are router decisions recorded on a real alert, not model calls.
AGENT_FOR_KIND: dict[str, str] = {
    "cycle": "scout",
    "alert": "scout",
    "qwen": "analyst",
    "sonnet": "strategist",
    "fable": "boss",
    "play_open": "treasurer",
    "play_close": "treasurer",
}
ACTIVITY_KINDS = tuple(AGENT_FOR_KIND)

#: Why the game screen has nothing (or nothing new) to show; shown as plain text.
REASON_NO_DATABASE = "No radar data yet."
REASON_NO_TABLES = "No plays yet: the radar has not started the game."
REASON_NO_WALLET = "No plays yet: the pretend wallet has not been created."
REASON_NO_PLAYS = "No plays yet."
REASON_UNREADABLE = "The game data could not be read."
REASON_DISABLED = "The game is switched off: the radar opens no new plays."

#: Why the Pilot shadow panel has nothing (or nothing new) to show, by reason code
#: (``ui.pilot_reader``); shown as plain text.
PILOT_REASONS: dict[str, str] = {
    "no_database": "No radar data yet.",
    "no_tables": "The pilot shadow has not started yet: the radar has not created its records.",
    "no_account": "The pilot shadow has not started yet: its pretend account is not recorded.",
    "unreadable": "The pilot shadow data could not be read.",
    "disabled": "The pilot shadow is switched off: the radar opens no new pilot entries.",
}

#: Why an open position has no valuation mark, by the typed status of ``ui.paper_reader``.
MARK_REASONS: dict[str, str] = {
    "missing_quote": "No price of this pair was recorded since it opened, so its value now is unknown.",
    "stale_quote": "The last price of this pair is too old to value it now.",
    "invalid_quote": "The recent prices of this pair were not usable (crossed, not a finite positive "
    "number, or the pair was not trading), so its value now is unknown.",
}
#: How the open positions are valued (``valuation_basis`` = ``conservative_liquidation``).
VALUATION_BASIS_TEXT = (
    "Open positions are valued as if closed now at the price you could actually sell at "
    "(or buy back at, for a bet on a fall), after the assumed commission on both the buy and the sell."
)
#: What the pilot shadow account is, and is not.
PILOT_ACCOUNT_TEXT = (
    "A separate pretend EUR account (PAPER). It is not SHADOW_LIVE and not a real exchange "
    "account: no real balance is read or shown."
)

_CONFIDENCE_WORDS = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}


# --- values ---------------------------------------------------------------------------


def _code(value: object) -> str | None:
    """A recorded code as a string: an enum's ``value`` or a plain non-blank string."""
    value = getattr(value, "value", value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _decimal(value: object) -> Decimal | None:
    """A finite number as a Decimal (a float at its shortest repr), else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = repr(value)
    if isinstance(value, (Decimal, int, str)):
        try:
            number = Decimal(value)
        except (InvalidOperation, ValueError):
            return None
        return number if number.is_finite() else None
    return None


def _count(value: object) -> int | None:
    """A recorded non-negative whole count, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _flag(value: object) -> bool | None:
    """A recorded boolean (SQLite 0/1 accepted), else ``None``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _asset(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _group(integer_digits: str) -> str:
    """English grouping: a comma every three integer digits (``1,000``)."""
    groups = []
    while len(integer_digits) > 3:
        groups.append(integer_digits[-3:])
        integer_digits = integer_digits[:-3]
    groups.append(integer_digits)
    return ",".join(reversed(groups))


def _plain(number: Decimal) -> str:
    """``number`` in positional notation, English style; a negative zero loses its sign."""
    text = format(number, "f")
    sign = ""
    if text.startswith("-"):
        text = text[1:]
        sign = "" if number == 0 else "-"
    integer_digits, dot, fraction = text.partition(".")
    return f"{sign}{_group(integer_digits)}{dot}{fraction}"


def format_money(value: object, *, signed: bool = False) -> str | None:
    """``value`` as euros in English format, e.g. ``€1.23`` or ``€10,000.00``; ``None``
    when missing.

    Rounded to cents half to even. ``signed`` prefixes ``+`` to a positive amount; a
    negative amount always carries ``-`` (``-€1.50``); zero never has a sign.
    """
    number = _decimal(value)
    if number is None:
        return None
    rounded = number.quantize(CENT, rounding=ROUND_HALF_EVEN)
    sign = "-" if rounded < 0 else "+" if signed and rounded > 0 else ""
    return f"{sign}€{_plain(abs(rounded))}"


def format_number(value: object) -> str | None:
    """A recorded price or ratio with all its recorded digits, English style."""
    number = _decimal(value)
    if number is None:
        return None
    return _plain(number)


def _magnitude(number: Decimal) -> str:
    """``abs(number)`` to one decimal place, or two when one place would show zero."""
    size = abs(number)
    rounded = size.quantize(_ONE_PLACE, rounding=ROUND_HALF_EVEN)
    if rounded == 0:
        rounded = size.quantize(CENT, rounding=ROUND_HALF_EVEN)
    return _plain(rounded)


def _negligible(number: Decimal) -> bool:
    """True for a non-zero number that would show as zero at two decimal places."""
    return number != 0 and abs(number).quantize(CENT, rounding=ROUND_HALF_EVEN) == 0


def format_percent(value: object) -> str | None:
    """A recorded percentage (already in percent units) as ``2.3%``, unsigned."""
    number = _decimal(value)
    if number is None:
        return None
    return f"{_magnitude(number)}%"


def direction_text(direction: object) -> str | None:
    """The play direction in plain words; ``None`` when there is none."""
    code = _code(direction)
    if code == UP:
        return "betting it goes up"
    if code == DOWN:
        return "betting it goes down"
    return None


def _sentence(head: str, clauses: Iterable[str | None]) -> str:
    """``head: a, b and c.`` with the missing clauses left out; ``head.`` when none remain."""
    kept = [clause for clause in clauses if clause]
    if not kept:
        return head + "."
    listed = kept[0] if len(kept) == 1 else ", ".join(kept[:-1]) + " and " + kept[-1]
    return f"{head}: {listed}."


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- the why of a play ------------------------------------------------------------------

_LAST_HOUR = "in the last hour"
_LAST_QUARTER = "in the last quarter hour"


def _features(why: Mapping[str, object] | None, layer: str) -> Mapping[str, object]:
    if not isinstance(why, Mapping):
        return {}
    features = why.get("features")
    if not isinstance(features, Mapping):
        return {}
    values = features.get(layer)
    return values if isinstance(values, Mapping) else {}


def _move(value: object, window: str) -> str | None:
    """``rose 2.3% in the last hour`` from a recorded return in percent."""
    number = _decimal(value)
    if number is None:
        return None
    if number == 0:
        return f"did not move {window}"
    if _negligible(number):
        return f"barely moved {window}"
    verb = "rose" if number > 0 else "fell"
    return f"{verb} {_magnitude(number)}% {window}"


def _volume(value: object) -> str | None:
    """``traded 3.4 times its usual volume`` from a recorded volume intensity (quarter hour)."""
    number = _decimal(value)
    if number is None or number < 0:
        return None
    if number == 0 or _negligible(number):
        return "hardly traded compared with its usual volume"
    return f"traded {_magnitude(number)} times its usual volume"


def _versus_btc(value: object) -> str | None:
    number = _decimal(value)
    if number is None:
        return None
    if number == 0 or _negligible(number):
        return f"did about as well as Bitcoin {_LAST_QUARTER}"
    side = "better" if number > 0 else "worse"
    return f"did {_magnitude(number)}% {side} than Bitcoin {_LAST_QUARTER}"


def _setup_head(setup: str | None, asset: str | None, l2: Mapping[str, object]) -> str:
    name = asset or "the coin"
    if setup == "BREAKOUT":
        state = _code(l2.get("breakout_state"))
        if state == "BREAKOUT_UP":
            return f"{name} climbed above its highest price of the last few hours"
        if state == "BREAKOUT_DOWN":
            return f"{name} dropped below its lowest price of the last few hours"
        return f"{name} broke out of the price range it had been in"
    if setup == "CONTINUATION":
        return f"{name} has kept moving the same way"
    if setup == "REVERSAL":
        return f"{name} showed signs of turning around"
    if setup == "SQUEEZE_RELEASE":
        if _flag(l2.get("range_compression")) is False:
            return f"{name} started moving strongly"
        return f"{name} sat in a tight price range and then started moving strongly"
    if setup == "EXHAUSTION":
        return f"{name} made a very strong move that seems to be running out of steam"
    return f"The radar saw an unusual move in {asset}" if asset else "The radar saw an unusual move"


def why_sentence(why: Mapping[str, object] | None, *, asset: object = None, direction: object = None) -> str:
    """Why a play was opened, from its frozen ``why`` JSON (setup, direction, features).

    ``direction`` defaults to the direction recorded in ``why``. Only feature values present
    in ``why`` are quoted; the setup names never reach the text.
    """
    facts: Mapping[str, object] = why if isinstance(why, Mapping) else {}
    name = _asset(asset)
    setup = _code(facts.get("setup_type"))
    side = _code(direction) or _code(facts.get("direction"))
    l1 = _features(facts, "l1")
    l2 = _features(facts, "l2")

    if setup == "REVERSAL":
        clauses = [_move(l1.get("return_1h"), _LAST_HOUR), _move(l1.get("return_15m"), _LAST_QUARTER)]
    elif setup in ("BREAKOUT", "SQUEEZE_RELEASE"):
        clauses = [_move(l1.get("return_15m"), _LAST_QUARTER), _volume(l1.get("volume_intensity_15m"))]
    elif setup == "CONTINUATION":
        clauses = [_move(l1.get("return_1h"), _LAST_HOUR), _versus_btc(l1.get("relative_return_vs_btc_15m"))]
    elif setup == "EXHAUSTION":
        clauses = [_move(l1.get("return_1h"), _LAST_HOUR), _volume(l1.get("volume_intensity_15m"))]
    else:
        clauses = [_move(l1.get("return_15m"), _LAST_QUARTER), _volume(l1.get("volume_intensity_15m"))]

    head = _setup_head(setup, name, l2)
    text = _capital(_sentence(head, clauses))
    bet = direction_text(side)
    if bet:
        text += f" The AI is {bet}."
    return text


# --- the exit rule (EX-1 initial paper policy) -----------------------------------------------
# A play with a stop and a target closes on the first recorded price that reaches one of
# them, or at its time limit; a legacy play (no levels, no exit reason) after a fixed hold.

STOP = "stop"
TARGET = "target"
TIME = "time"


def _hold_limit(minutes: object) -> str:
    """``the 24-hour limit`` from a recorded hold; ``the time limit`` when there is none."""
    count = _count(minutes)
    if count is None or count <= 0:
        return "the time limit"
    if count % 60 == 0:
        return f"the {count // 60}-hour limit"
    return f"the {count}-minute limit"


def exit_reason_label(reason: object, *, hold_minutes: object = None) -> str | None:
    """A short label for a recorded exit reason (``Hit the stop``); ``None`` for a legacy
    close, which has no recorded reason."""
    code = _code(reason)
    if code == STOP:
        return "Hit the stop"
    if code == TARGET:
        return "Hit the target"
    if code == TIME:
        return _capital(f"closed at {_hold_limit(hold_minutes)}")
    return None


def _toward(direction: str | None, falls: bool) -> str:
    """How the price went to a level: ``fell to`` / ``rose to``, ``reached`` when unknown."""
    if direction not in (UP, DOWN):
        return "reached"
    return "fell to" if falls else "rose to"


def _reason_sentence(reason: str | None, direction: str | None, stop: object, target: object, hold: object) -> str | None:
    """Why a play under the EX-1 exit rule closed, from its recorded reason and frozen levels."""
    if reason == STOP:
        level = format_number(stop)
        limit = f"the loss limit of {level}" if level else "the loss limit"
        verb = _toward(direction, falls=direction == UP)
        return f"It hit the stop: the price {verb} {limit} set when it opened, and it closed on the first price seen there."
    if reason == TARGET:
        level = format_number(target)
        goal = f"the profit goal of {level}" if level else "the profit goal"
        verb = _toward(direction, falls=direction == DOWN)
        return f"It hit the target: the price {verb} {goal} set when it opened, and it closed on the first price seen there."
    if reason == TIME:
        return (
            f"It reached {_hold_limit(hold)} without hitting the stop or the target, "
            "so it closed on the first price seen after that."
        )
    return None


# --- the result of a closed play ----------------------------------------------------------


def _outcome(outcome: object, net: Decimal | None) -> str | None:
    code = _code(outcome)
    if code in ("WIN", "LOSS", "FLAT"):
        return code
    if net is None:
        return None
    rounded = net.quantize(CENT, rounding=ROUND_HALF_EVEN)
    return "WIN" if rounded > 0 else "LOSS" if rounded < 0 else "FLAT"


def _verdict(outcome: str | None, net: Decimal | None) -> str | None:
    """``won €1.23`` / ``lost €0.45`` / ``broke even (€0.00)``."""
    amount = format_money(abs(net)) if net is not None else None
    if outcome == "WIN":
        return f"won {amount}" if amount else "won"
    if outcome == "LOSS":
        return f"lost {amount}" if amount else "lost"
    if outcome == "FLAT":
        return f"broke even ({amount})" if amount else "broke even"
    return None


def _fills(direction: str | None, entry_bid: object, entry_ask: object, exit_bid: object, exit_ask: object) -> str | None:
    """The recorded fill prices: a rise is bought at the ask and sold at the bid."""
    if direction == UP:
        bought, sold = format_number(entry_ask), format_number(exit_bid)
        if bought and sold:
            return f"bought at {bought} and sold at {sold}"
    elif direction == DOWN:
        sold, bought = format_number(entry_bid), format_number(exit_ask)
        if sold and bought:
            return f"sold at {sold} and bought back at {bought}"
    return None


def _price_move(gross_mid: object) -> str | None:
    number = _decimal(gross_mid)
    if number is None:
        return None
    rounded = number.quantize(CENT, rounding=ROUND_HALF_EVEN)
    if rounded == 0:
        return f"The price barely moved ({format_money(rounded)} before costs)"
    side = "the right way" if rounded > 0 else "the wrong way"
    return f"The price moved {side}: {format_money(rounded, signed=True)} before costs"


def _delay(seconds: object) -> str | None:
    number = _decimal(seconds)
    if number is None or number <= 0:
        return None
    if number < 90:
        whole = max(int(number.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)), 1)
        unit = "second" if whole == 1 else "seconds"
    else:
        whole = int((number / 60).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
        unit = "minute" if whole == 1 else "minutes"
    return f"It closed {whole} {unit} after the planned time, because there was no valid price before then."


def result_sentence(
    *,
    asset: object = None,
    direction: object = None,
    outcome: object = None,
    net: object = None,
    gross_mid: object = None,
    spread_cost: object = None,
    fees: object = None,
    entry_bid: object = None,
    entry_ask: object = None,
    exit_bid: object = None,
    exit_ask: object = None,
    delay_seconds: object = None,
    exit_reason: object = None,
    stop: object = None,
    target: object = None,
    hold_minutes: object = None,
) -> str:
    """How a closed play ended, from its recorded close: the net in €, why it closed (its
    recorded exit reason with the play's frozen stop, target or hold; nothing for a legacy
    close), the price move before costs with the observed fill prices, the spread, the
    commissions and, when there was one, the exit delay."""
    name = _asset(asset) or "The play"
    side = _code(direction)
    net_value = _decimal(net)
    verdict = _verdict(_outcome(outcome, net_value), net_value)
    first = f"{name} {verdict}." if verdict else f"{name} closed."

    fills = _fills(side, entry_bid, entry_ask, exit_bid, exit_ask)
    move = _price_move(gross_mid)
    if move and fills:
        move = f"{move} ({fills})"
    elif fills:
        move = _capital(fills)

    spread = format_money(spread_cost)
    fee = format_money(fees)
    costs = [
        f"the gap between the buying and selling price cost {spread}" if spread else None,
        f"the commissions cost {fee}" if fee else None,
    ]
    cost_text = " and ".join(item for item in costs if item)

    parts = [first]
    reason = _reason_sentence(_code(exit_reason), side, stop, target, hold_minutes)
    if reason:
        parts.append(reason)
    if move:
        parts.append(move + ".")
    if cost_text:
        parts.append(_capital(cost_text) + ".")
    delay = _delay(delay_seconds)
    if delay:
        parts.append(delay)
    return " ".join(parts)


# --- the decision steps of an open play ------------------------------------------------


def _whole(value: object) -> str | None:
    number = _decimal(value)
    if number is None:
        return None
    return _plain(number.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _hold(minutes: object) -> str | None:
    count = _count(minutes)
    if count is None or count <= 0:
        return None
    if count % 60 == 0:
        hours = count // 60
        return f"{hours} {'hour' if hours == 1 else 'hours'}"
    return f"{count} {'minute' if count == 1 else 'minutes'}"


def decision_steps(
    *,
    asset: object = None,
    direction: object = None,
    assets_eligible: object = None,
    scores: Mapping[str, object] | None = None,
    hold_minutes: object = None,
    pending: bool = False,
    stop: object = None,
    target: object = None,
) -> list[dict[str, str]]:
    """The three steps behind an open play, from its recorded facts: what the radar saw
    (its cycle's ``assets_eligible``), how the alert was scored (the ``scores`` frozen in
    the play's why) and what happens now: the exit rule of its frozen stop, target and
    hold, the fixed hold of a legacy play (no levels), or the wait for a valid price.
    Each step is ``{"title", "text", "state"}`` with ``state`` ``done`` or ``now``."""
    name = _asset(asset) or "a coin"
    seen = _count(assets_eligible)
    moved = f"{name} was moving unusually."
    first = f"Looked at {seen} {'coin' if seen == 1 else 'coins'}; {moved}" if seen is not None else _capital(moved)

    facts: Mapping[str, object] = scores if isinstance(scores, Mapping) else {}
    opportunity = _whole(facts.get("opportunity_score"))
    tradeable = _whole(facts.get("tradeability_score"))
    level = _CONFIDENCE_WORDS.get(_code(facts.get("confidence")) or "")
    clauses = [
        f"opportunity {opportunity} out of a hundred" if opportunity else None,
        f"ease of trading {tradeable} out of a hundred" if tradeable else None,
        f"{level} confidence" if level else None,
    ]
    bet = direction_text(direction)
    if any(clauses):
        second = _sentence("Scored the alert", clauses)
        if bet:
            second += f" Decision: {bet}."
    else:
        second = f"Decision: {bet}." if bet else "Decided to open the play."

    if pending:
        third = {
            "title": "Waiting for a price",
            "text": "The closing time has passed, but there is no valid price yet; it closes at the first one.",
            "state": "now",
        }
    elif stop is not None and target is not None:
        loss, goal = format_number(stop), format_number(target)
        hold = _hold(hold_minutes)
        limit = f"the loss limit of {loss}" if loss else "the loss limit"
        aim = f"the profit goal of {goal}" if goal else "the profit goal"
        latest = f"or after {hold} at the latest" if hold else "or at the time limit at the latest"
        third = {
            "title": "Now it waits",
            "text": f"It closes by itself on the first price that reaches {limit} or {aim}, {latest}.",
            "state": "now",
        }
    else:
        hold = _hold(hold_minutes)
        third = {
            "title": "Now it waits",
            "text": f"It closes by itself after {hold}, win or lose."
            if hold
            else "It closes by itself at the planned time, win or lose.",
            "state": "now",
        }
    return [
        {"title": "The radar noticed", "text": first, "state": "done"},
        {"title": "The AI checked", "text": second, "state": "done"},
        third,
    ]


# --- the cost sentence ------------------------------------------------------------------


def _fee_percent(bps: Decimal) -> str:
    return f"{_plain((bps / 100).normalize())}%"


def cost_sentence(fee_bps: Iterable[object], closed_spread_costs: Iterable[object] = ()) -> str | None:
    """What each play pays, from the ``fee_bps`` frozen on the plays and, when closed plays
    exist, their recorded average spread cost. ``None`` when there is nothing recorded."""
    fees = sorted({number for number in map(_decimal, fee_bps) if number is not None and number >= 0})
    spreads = [number for number in map(_decimal, closed_spread_costs) if number is not None]
    parts = []
    if fees:
        low, high = _fee_percent(fees[0]), _fee_percent(fees[-1])
        rate = low if low == high else f"between {low} and {high}"
        parts.append(f"Every buy and every sell pays a commission of {rate} of the amount at stake.")
    if spreads:
        average = sum(spreads, Decimal(0)) / len(spreads)
        parts.append(
            "On the plays already closed, the gap between the buying and selling price "
            f"cost {format_money(average)} per play on average."
        )
    return " ".join(parts) or None


# --- valuation labels -------------------------------------------------------------------


def fee_provenance_text(fee_bps: object) -> str:
    """The commission stored on one play, per buy and per sell, labelled as an assumption."""
    number = _decimal(fee_bps)
    head = (
        f"Assumed commission of {_fee_percent(number)} on the buy and again on the sell"
        if number is not None and number >= 0
        else "Assumed commission on the buy and on the sell"
    )
    return f"{head} (ASSUMED, account tier unverified)."


def fx_excluded_text(quote: object, currency: object) -> str:
    """Why the wallet amounts of a play priced in another currency are hypothetical."""
    priced = f"priced in {quote}" if isinstance(quote, str) and quote.strip() else "priced in another currency"
    wallet = currency if isinstance(currency, str) and currency.strip() else "wallet"
    return (
        f"Hypothetical simulation: this pair is {priced}; its price moves are scaled onto the {wallet} "
        f"stake with no exchange rate (FX excluded). This is not {wallet} inventory."
    )


def freshness_text(max_age_minutes: object) -> str:
    """The reporting freshness rule a valuation mark must meet."""
    minutes = _count(max_age_minutes)
    age = f"at most {minutes} minutes old" if minutes is not None else "recent"
    return f"A position is valued only on a valid price of its own pair, recorded since it opened and {age}."


# --- the agents' lines --------------------------------------------------------------------


def cycle_line(
    *,
    assets_eligible: object = None,
    shortlist_count: object = None,
    warmup: object = None,
    api_failures: object = None,
) -> str:
    """Scout, after a finished radar cycle (a radar_runs row)."""
    eligible = _count(assets_eligible)
    shortlist = _count(shortlist_count)
    failures = _count(api_failures)
    clauses = [
        f"I looked at {eligible} {'coin' if eligible == 1 else 'coins'}" if eligible is not None else None,
        None
        if shortlist is None
        else "none made my watch list"
        if shortlist == 0
        else f"{shortlist} made my watch list",
    ]
    text = _sentence("Market sweep done", clauses)
    if _flag(warmup):
        text += " I am still gathering data to see clearly."
    if failures:
        noun = "request to the exchange failed" if failures == 1 else "requests to the exchange failed"
        text += f" {failures} {noun}."
    return text


def alert_line(*, asset: object = None, direction: object = None) -> str:
    """Scout, on a new real radar alert."""
    name = _asset(asset)
    side = _code(direction)
    subject = f"Alert on {name}: it" if name else "New alert: a coin"
    if side == UP:
        return f"{subject} might go up."
    if side == DOWN:
        return f"{subject} might go down."
    if name:
        return f"{subject} is moving unusually."
    return "New alert on the radar."


def qwen_line(
    *,
    asset: object = None,
    batch_status: object = None,
    veto: object = None,
    confidence: object = None,
) -> str:
    """Analyst, on a qwen_reviews row; veto and confidence only when recorded."""
    name = _asset(asset) or "this coin"
    status = _code(batch_status)
    if status == "OK":
        verdict = _flag(veto)
        head = (
            f"I reviewed {name}: I would stay out of this one"
            if verdict is True
            else f"I reviewed {name}: it looks good to me"
            if verdict is False
            else f"I reviewed {name}"
        )
        level = _CONFIDENCE_WORDS.get(_code(confidence) or "")
        return f"{head}, with {level} confidence." if level else f"{head}."
    if status == "TIMEOUT":
        return f"I could not review {name} in time."
    if status == "UNAVAILABLE":
        return f"I could not review {name}: my model was not available."
    if status == "INVALID_JSON":
        return f"My review of {name} came out garbled and does not count."
    if status == "ERROR":
        return f"Something went wrong while I was reviewing {name}."
    return f"I took a look at {name}."


_ROUTED = {"SONNET": "sonnet", "FABLE": "fable"}


def router_line(*, decision: object = None, asset: object = None) -> str | None:
    """Strategist (``SONNET``) or Boss (``FABLE``) on the router decision recorded on a real
    alert (its ``model_demand``). The line says only that the alert deserves a closer look;
    ``None`` for ``IGNORE``, a missing or an unknown decision."""
    code = _code(decision)
    name = _asset(asset)
    subject = f"the {name} alert" if name else "this alert"
    if code == "SONNET":
        return f"The router says {subject} deserves a closer look from me."
    if code == "FABLE":
        return f"The router woke me up: {subject} deserves a closer look from me."
    return None


def play_open_line(*, asset: object = None, direction: object = None, stake: object = None) -> str:
    """Treasurer, when a pretend play is opened."""
    name = _asset(asset)
    amount = format_money(stake)
    if amount:
        head = f"I put {amount} of pretend money on {name or 'a play'}"
    else:
        head = f"I opened a pretend play on {name}" if name else "I opened a pretend play"
    return _sentence(head, [direction_text(direction)])


_CLOSED_AT = {STOP: "at the stop", TARGET: "at the target"}


def play_close_line(
    *,
    asset: object = None,
    outcome: object = None,
    net: object = None,
    exit_reason: object = None,
    hold_minutes: object = None,
) -> str:
    """Treasurer, when a pretend play is closed; names its recorded exit reason (none for
    a legacy close)."""
    name = _asset(asset)
    net_value = _decimal(net)
    verdict = _verdict(_outcome(outcome, net_value), net_value)
    head = f"I closed the {name} play" if name else "I closed a play"
    reason = _code(exit_reason)
    if reason == TIME:
        head += f" at {_hold_limit(hold_minutes)}"
    elif reason in _CLOSED_AT:
        head += f" {_CLOSED_AT[reason]}"
    return _sentence(head, [verdict])


def activity_line(kind: str, facts: Mapping[str, object]) -> str:
    """The speech line for one activity ``kind`` from its recorded ``facts``."""
    def get(key: str) -> object:
        return facts.get(key)

    if kind == "cycle":
        return cycle_line(
            assets_eligible=get("assets_eligible"),
            shortlist_count=get("shortlist_count"),
            warmup=get("warmup"),
            api_failures=get("api_failures"),
        )
    if kind == "alert":
        return alert_line(asset=get("asset"), direction=get("direction"))
    if kind == "qwen":
        return qwen_line(
            asset=get("asset"),
            batch_status=get("batch_status"),
            veto=get("veto"),
            confidence=get("confidence"),
        )
    if kind in ("sonnet", "fable"):
        decision = _code(get("decision")) or kind.upper()
        line = router_line(decision=decision, asset=get("asset"))
        if line is None or _ROUTED.get(decision) != kind:
            raise ValueError(f"activity kind {kind!r} needs the router decision {kind.upper()!r}, got {decision!r}")
        return line
    if kind == "play_open":
        return play_open_line(asset=get("asset"), direction=get("direction"), stake=get("stake"))
    if kind == "play_close":
        return play_close_line(
            asset=get("asset"),
            outcome=get("outcome"),
            net=get("net"),
            exit_reason=get("exit_reason"),
            hold_minutes=get("hold_minutes"),
        )
    raise ValueError(f"unknown activity kind {kind!r}")
