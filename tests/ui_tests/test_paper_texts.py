"""ui/paper_texts.py: plain-English sentences built only from recorded facts.

The central oracle is traceability: every number in a sentence must be one of the input's
numbers in a display form (absolute value, rounded to the shown places, basis points as a
percentage, seconds as minutes, or the average of recorded spread costs). A template called
with no numbers in its input must print no digit at all.
"""

import ast
import itertools
import os
import re
import sys
import unittest
from decimal import ROUND_HALF_EVEN, Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ui import paper_texts as texts

SETUPS = ("BREAKOUT", "CONTINUATION", "REVERSAL", "SQUEEZE_RELEASE", "EXHAUSTION")
DIRECTIONS = ("LONG", "SHORT")
OUTCOMES = ("WIN", "LOSS", "FLAT")
JARGON = ("LONG", "SHORT", "bps", "BREAKOUT", "CONTINUATION", "REVERSAL", "SQUEEZE", "EXHAUSTION",
          "None", "null", "nan", "NaN", "Infinity", "OK", "TIMEOUT", "SUCCESS", "FAILED")
# English numbers: comma thousands groups, point decimals ("2,001.40", "0.26").
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
UPPER_WORD = re.compile(r"\b[A-Z][A-Z0-9]+\b")
ANALYSIS_WORDS = ("stud", "opinion", "decision", "analy", "review", "gave", "result", "think", "answer",
                  "success", "fail", "timeout", "pending")
UP_TEXT = "betting it goes up"
DOWN_TEXT = "betting it goes down"

FULL_L1 = {
    "return_1m": 0.05,
    "return_5m": 0.31,
    "return_15m": 1.2345,
    "return_1h": -2.137,
    "return_4h": 5.9,
    "volume_intensity_15m": 3.44,
    "relative_return_vs_btc_15m": 0.87,
    "spread_bps": 12.5,
}
FULL_L2 = {
    "breakout_state": "BREAKOUT_UP",
    "rejection_state": "NONE",
    "range_compression": True,
    "range_expansion": True,
    "exhaustion": False,
    "return_15m_atr": 1.8,
    "return_1h_atr": 2.2,
}
SCORES = {"anomaly_score": 7.1, "opportunity_score": 64.0, "tradeability_score": 55.5, "confidence": "MEDIUM"}


def why(setup, direction, l1=FULL_L1, l2=FULL_L2, scores=SCORES):
    return {"setup_type": setup, "direction": direction, "scores": scores, "features": {"l1": l1, "l2": l2}}


def leaves(value):
    """Every number anywhere in ``value`` (mappings, sequences, scalars), as Decimal."""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from leaves(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from leaves(item)
    elif isinstance(value, float):
        yield Decimal(repr(value))
    elif isinstance(value, (int, Decimal)):
        yield Decimal(value)
    elif isinstance(value, str):
        try:
            yield Decimal(value)
        except ArithmeticError:
            return


def display_forms(number):
    size = abs(number)
    forms = {size}
    for exponent in ("1", "0.1", "0.01"):
        forms.add(size.quantize(Decimal(exponent), rounding=ROUND_HALF_EVEN))
    forms.add(size / 100)  # basis points as a percentage
    forms.add((size / 60).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))  # seconds (or minutes) as minutes (or hours)
    return forms


def numbers_in(text):
    return [Decimal(token.replace(",", "")) for token in NUMBER.findall(text)]


class TextCase(unittest.TestCase):
    def assert_traceable(self, text, *inputs, extra=()):
        allowed = set(extra)
        for number in leaves(list(inputs)):
            allowed |= display_forms(number)
        for number in numbers_in(text):
            self.assertIn(number, allowed, f"{number} in {text!r} is not traceable to the input")

    def assert_no_digits(self, text):
        self.assertIsNone(re.search(r"\d", text), f"digit printed without a numeric input: {text!r}")

    def assert_plain(self, text, asset=None):
        for word in JARGON:
            self.assertNotIn(word, text.replace(asset or "\0", ""), f"jargon {word!r} in {text!r}")
        coins = set(UPPER_WORD.findall(text)) - {"AI"}
        self.assertLessEqual(coins, {asset} if asset else set(), f"a coin not in the input: {text!r}")
        self.assertNotIn("<", text)
        self.assertTrue(text.strip())


class TestFormatting(TextCase):
    def test_money_is_english_with_point_decimals_and_euro_prefix(self):
        cases = {
            Decimal("1000"): "€1,000.00",
            Decimal("100"): "€100.00",
            Decimal("999.999"): "€1,000.00",
            Decimal("12345.675"): "€12,345.68",
            Decimal("1234567.1"): "€1,234,567.10",
            Decimal("0.005"): "€0.00",
            Decimal("0.015"): "€0.02",
            Decimal("-1.5"): "-€1.50",
            Decimal("-1234.5"): "-€1,234.50",
            Decimal("-0.001"): "€0.00",
            "12.30": "€12.30",
            7: "€7.00",
            0.1: "€0.10",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(texts.format_money(value), expected)

    def test_signed_money(self):
        self.assertEqual(texts.format_money(Decimal("2.1"), signed=True), "+€2.10")
        self.assertEqual(texts.format_money(Decimal("-2.1"), signed=True), "-€2.10")
        self.assertEqual(texts.format_money(Decimal("0"), signed=True), "€0.00")

    def test_missing_or_unusable_money_is_none_never_zero(self):
        for value in (None, True, False, float("nan"), float("inf"), "abc", Decimal("NaN"), [], {}):
            with self.subTest(value=value):
                self.assertIsNone(texts.format_money(value))

    def test_prices_keep_recorded_digits(self):
        self.assertEqual(texts.format_number(Decimal("0.00001234")), "0.00001234")
        self.assertEqual(texts.format_number(Decimal("1E-8")), "0.00000001")
        self.assertEqual(texts.format_number(101.25), "101.25")
        self.assertEqual(texts.format_number(Decimal("62031.5")), "62,031.5")
        self.assertEqual(texts.format_number(Decimal("-0.0")), "0.0")
        self.assertIsNone(texts.format_number(None))

    def test_percent_uses_one_place_or_two_when_one_shows_zero(self):
        self.assertEqual(texts.format_percent(-2.137), "2.1%")
        self.assertEqual(texts.format_percent(0.04), "0.04%")
        self.assertEqual(texts.format_percent(0.55), "0.6%")
        self.assertEqual(texts.format_percent(1234.56), "1,234.6%")
        self.assertIsNone(texts.format_percent(None))

    def test_direction_text(self):
        self.assertEqual(texts.direction_text("LONG"), UP_TEXT)
        self.assertEqual(texts.direction_text("SHORT"), DOWN_TEXT)
        for value in ("NONE", None, "", "long", 1):
            self.assertIsNone(texts.direction_text(value))


class TestWhySentence(TextCase):
    def test_every_setup_and_direction_with_full_features(self):
        for setup, direction in itertools.product(SETUPS, DIRECTIONS):
            facts = why(setup, direction)
            with self.subTest(setup=setup, direction=direction):
                text = texts.why_sentence(facts, asset="SOL")
                self.assertIn("SOL", text)
                self.assertIn(UP_TEXT if direction == "LONG" else DOWN_TEXT, text)
                self.assertRegex(text, r"\d")  # a recorded feature value is quoted
                self.assert_traceable(text, facts)
                self.assert_plain(text, "SOL")

    def test_setup_sentences_differ(self):
        sentences = {texts.why_sentence(why(setup, "LONG"), asset="SOL") for setup in SETUPS}
        self.assertEqual(len(sentences), len(SETUPS))

    def test_quoted_values_match_the_features(self):
        text = texts.why_sentence(why("REVERSAL", "LONG"), asset="SOL")
        self.assertIn("fell 2.1% in the last hour", text)
        self.assertIn("rose 1.2% in the last quarter hour", text)
        breakout = texts.why_sentence(why("BREAKOUT", "LONG"), asset="SOL")
        self.assertIn("above its highest price", breakout)
        self.assertIn("3.4 times its usual volume", breakout)
        down = texts.why_sentence(why("BREAKOUT", "SHORT", l2={"breakout_state": "BREAKOUT_DOWN"}), asset="SOL")
        self.assertIn("below its lowest price", down)
        continuation = texts.why_sentence(why("CONTINUATION", "LONG"), asset="SOL")
        self.assertIn("did 0.9% better than Bitcoin", continuation)
        self.assertEqual(
            continuation,
            "SOL has kept moving the same way: fell 2.1% in the last hour and did 0.9% better than "
            "Bitcoin in the last quarter hour. The AI is betting it goes up.",
        )

    def test_tiny_moves_read_as_barely_moved_not_zero(self):
        tiny = {"return_1h": 0.004, "return_15m": -0.0049, "relative_return_vs_btc_15m": 0.003,
                "volume_intensity_15m": 0.001}
        continuation = texts.why_sentence(why("CONTINUATION", "LONG", l1=tiny), asset="SOL")
        self.assertEqual(
            continuation,
            "SOL has kept moving the same way: barely moved in the last hour and did about as well as "
            "Bitcoin in the last quarter hour. The AI is betting it goes up.",
        )
        reversal = texts.why_sentence(why("REVERSAL", "SHORT", l1=tiny), asset="SOL")
        self.assertIn("barely moved in the last quarter hour", reversal)
        breakout = texts.why_sentence(why("BREAKOUT", "LONG", l1=tiny), asset="SOL")
        self.assertIn("hardly traded compared with its usual volume", breakout)
        for text in (continuation, reversal, breakout):
            self.assert_no_digits(text)
        exact = {"return_1h": 0, "relative_return_vs_btc_15m": 0}
        flat = texts.why_sentence(why("CONTINUATION", "LONG", l1=exact), asset="SOL")
        self.assertIn("did not move in the last hour", flat)
        self.assertIn("did about as well as Bitcoin", flat)
        # 0.005 still shows two places, as recorded.
        self.assertIn("rose 0.01%", texts.why_sentence(why("CONTINUATION", "LONG", l1={"return_1h": 0.0051}), asset="SOL"))

    def test_null_or_missing_features_omit_their_clauses(self):
        empty_l1 = {key: None for key in FULL_L1}
        empty_l2 = {key: None for key in FULL_L2}
        for setup, direction in itertools.product((*SETUPS, "SOMETHING_NEW", None), (*DIRECTIONS, "NONE", None)):
            for l1, l2 in ((empty_l1, empty_l2), ({}, {}), (None, None)):
                facts = why(setup, direction, l1=l1, l2=l2, scores={})
                with self.subTest(setup=setup, direction=direction, l1=l1):
                    text = texts.why_sentence(facts, asset="SOL")
                    self.assert_no_digits(text)
                    self.assert_plain(text, "SOL")
                    if direction not in DIRECTIONS:
                        self.assertNotIn("betting", text)

    def test_non_finite_and_wrongly_typed_features_are_not_printed(self):
        l1 = {"return_1h": float("nan"), "return_15m": "fast", "volume_intensity_15m": True,
              "relative_return_vs_btc_15m": float("-inf")}
        for setup in SETUPS:
            text = texts.why_sentence(why(setup, "LONG", l1=l1, l2={}), asset="SOL")
            self.assert_no_digits(text)

    def test_unknown_setup_gets_the_generic_sentence(self):
        text = texts.why_sentence(why("SOMETHING_NEW", "SHORT"), asset="ADA")
        self.assertTrue(text.startswith("The radar saw an unusual move in ADA"))
        self.assertIn(DOWN_TEXT, text)
        self.assert_traceable(text, why("SOMETHING_NEW", "SHORT"))
        self.assert_plain(text, "ADA")

    def test_missing_why_and_asset(self):
        self.assertEqual(texts.why_sentence(None), "The radar saw an unusual move.")
        self.assertEqual(texts.why_sentence("not a mapping"), "The radar saw an unusual move.")
        text = texts.why_sentence(why("CONTINUATION", "LONG", l1={}, l2={}))
        self.assertTrue(text.startswith("The coin"))
        self.assert_plain(text)

    def test_direction_argument_overrides_the_recorded_one(self):
        text = texts.why_sentence(why("CONTINUATION", "LONG"), asset="SOL", direction="SHORT")
        self.assertIn(DOWN_TEXT, text)


def closed(direction="LONG", outcome="WIN", **overrides):
    facts = {
        "asset": "ETH",
        "direction": direction,
        "outcome": outcome,
        "net": {"WIN": Decimal("1.23"), "LOSS": Decimal("-0.45"), "FLAT": Decimal("0.00")}[outcome],
        "gross_mid": {"WIN": Decimal("2.10"), "LOSS": Decimal("-0.12"), "FLAT": Decimal("0.87")}[outcome],
        "spread_cost": Decimal("0.35"),
        "fees": Decimal("0.52"),
        "entry_bid": Decimal("2001.10"),
        "entry_ask": Decimal("2001.40"),
        "exit_bid": Decimal("2043.55"),
        "exit_ask": Decimal("2043.90"),
        "delay_seconds": 0.0,
    }
    facts.update(overrides)
    return facts


class TestResultSentence(TextCase):
    def test_every_outcome_and_direction(self):
        verdicts = {"WIN": "won €1.23", "LOSS": "lost €0.45", "FLAT": "broke even (€0.00)"}
        for direction, outcome in itertools.product(DIRECTIONS, OUTCOMES):
            facts = closed(direction, outcome)
            with self.subTest(direction=direction, outcome=outcome):
                text = texts.result_sentence(**facts)
                self.assertTrue(text.startswith(f"ETH {verdicts[outcome]}."))
                self.assertIn("selling price cost €0.35", text)
                self.assertIn("the commissions cost €0.52", text)
                self.assertIn("before costs", text)
                self.assertNotIn("after the planned time", text)
                self.assert_traceable(text, facts)
                self.assert_plain(text, "ETH")

    def test_full_sentence(self):
        self.assertEqual(
            texts.result_sentence(**closed("LONG", "WIN", delay_seconds=312.5)),
            "ETH won €1.23. The price moved the right way: +€2.10 before costs (bought at 2,001.40 and "
            "sold at 2,043.55). The gap between the buying and selling price cost €0.35 and the "
            "commissions cost €0.52. It closed 5 minutes after the planned time, because there was "
            "no valid price before then.",
        )

    def test_fills_follow_the_direction(self):
        rise = texts.result_sentence(**closed("LONG"))
        self.assertIn("bought at 2,001.40 and sold at 2,043.55", rise)
        fall = texts.result_sentence(**closed("SHORT"))
        self.assertIn("sold at 2,001.10 and bought back at 2,043.90", fall)

    def test_price_move_wording(self):
        self.assertIn("moved the right way: +€2.10", texts.result_sentence(**closed(outcome="WIN")))
        self.assertIn("moved the wrong way: -€0.12", texts.result_sentence(**closed(outcome="LOSS")))
        flat_move = texts.result_sentence(**closed(outcome="FLAT", gross_mid=Decimal("0")))
        self.assertIn("barely moved (€0.00 before costs)", flat_move)

    def test_delay_is_stated_only_when_positive(self):
        for seconds, expected in ((312.5, "It closed 5 minutes after"), (45, "It closed 45 seconds after"),
                                  (0.4, "It closed 1 second after"), (60 * 61, "It closed 61 minutes after"),
                                  (95, "It closed 2 minutes after")):
            with self.subTest(seconds=seconds):
                facts = closed(delay_seconds=seconds)
                text = texts.result_sentence(**facts)
                self.assertIn(expected, text)
                self.assert_traceable(text, facts, extra={Decimal(1)} if seconds < 1 else ())
        for seconds in (0, 0.0, None, -3, float("nan")):
            self.assertNotIn("planned time", texts.result_sentence(**closed(delay_seconds=seconds)))

    def test_each_null_fact_drops_only_its_clause(self):
        base = closed()
        for key in base:
            facts = dict(base, **{key: None})
            with self.subTest(null=key):
                text = texts.result_sentence(**facts)
                self.assert_traceable(text, facts)
                self.assert_plain(text, "ETH" if key != "asset" else None)
                if key in ("spread_cost", "fees"):
                    self.assertNotIn("selling price cost" if key == "spread_cost" else "commissions", text)
                if key == "gross_mid":
                    self.assertNotIn("before costs", text)
                    self.assertIn("Bought at", text)

    def test_all_null_prints_no_number(self):
        text = texts.result_sentence()
        self.assertEqual(text, "The play closed.")
        nulls = {key: None for key in closed()}
        self.assert_no_digits(texts.result_sentence(**nulls))

    def test_outcome_falls_back_to_the_sign_of_the_net(self):
        self.assertIn("won", texts.result_sentence(asset="ETH", net=Decimal("0.01")))
        self.assertIn("lost", texts.result_sentence(asset="ETH", net="-3.2"))
        self.assertIn("broke even", texts.result_sentence(asset="ETH", net=Decimal("0.004")))
        self.assertEqual(texts.result_sentence(asset="ETH", outcome="WIN"), "ETH won.")


LEVELS = {"stop": Decimal("1951.00"), "target": Decimal("2102.20"), "hold_minutes": 1440}


class TestExitReasons(TextCase):
    """Closes under the EX-1 exit rule (stop, target, time) and legacy closes (no reason)."""

    def test_each_reason_names_itself_with_the_observed_price_and_the_costs(self):
        expected = {
            ("LONG", "stop"): "It hit the stop: the price fell to the loss limit of 1,951.00 set when it opened",
            ("SHORT", "stop"): "It hit the stop: the price rose to the loss limit of 1,951.00 set when it opened",
            ("LONG", "target"): "It hit the target: the price rose to the profit goal of 2,102.20 set when it opened",
            ("SHORT", "target"): "It hit the target: the price fell to the profit goal of 2,102.20 set when it opened",
            ("LONG", "time"): "It reached the 24-hour limit without hitting the stop or the target",
            ("SHORT", "time"): "It reached the 24-hour limit without hitting the stop or the target",
        }
        observed = {"LONG": "sold at 2,043.55", "SHORT": "bought back at 2,043.90"}
        for (direction, reason), clause in expected.items():
            facts = closed(direction, "LOSS", exit_reason=reason, **LEVELS)
            with self.subTest(direction=direction, reason=reason):
                text = texts.result_sentence(**facts)
                self.assertIn(clause, text)
                self.assertIn(observed[direction], text)  # the observed exit price, never the level
                self.assertIn("selling price cost €0.35", text)
                self.assertIn("the commissions cost €0.52", text)
                self.assertTrue(text.startswith("ETH lost €0.45. It "))
                self.assert_traceable(text, facts)
                self.assert_plain(text, "ETH")

    def test_full_sentence_for_a_stop(self):
        self.assertEqual(
            texts.result_sentence(**closed("LONG", "LOSS", exit_reason="stop", exit_bid=Decimal("1950.20"), **LEVELS)),
            "ETH lost €0.45. It hit the stop: the price fell to the loss limit of 1,951.00 set when it opened, and "
            "it closed on the first price seen there. The price moved the wrong way: -€0.12 before costs (bought at "
            "2,001.40 and sold at 1,950.20). The gap between the buying and selling price cost €0.35 and the "
            "commissions cost €0.52.",
        )

    def test_time_close_keeps_its_recorded_delay(self):
        text = texts.result_sentence(**closed(exit_reason="time", delay_seconds=40, **LEVELS))
        self.assertIn("It reached the 24-hour limit", text)
        self.assertIn("It closed 40 seconds after the planned time", text)

    def test_missing_levels_or_hold_drop_their_numbers(self):
        for reason, clause in (("stop", "the price fell to the loss limit set when it opened"),
                               ("target", "the price rose to the profit goal set when it opened"),
                               ("time", "It reached the time limit without hitting")):
            text = texts.result_sentence(**closed(exit_reason=reason, entry_bid=None, entry_ask=None,
                                                  exit_bid=None, exit_ask=None, gross_mid=None,
                                                  net=None, spread_cost=None, fees=None))
            with self.subTest(reason=reason):
                self.assertIn(clause, text)
                self.assert_no_digits(text)
        self.assertIn("the price reached the loss limit",
                      texts.result_sentence(exit_reason="stop", stop=Decimal("5")).replace(" of 5", ""))

    def test_legacy_close_keeps_the_old_wording(self):
        for reason in (None, "", "gossip"):
            with self.subTest(reason=reason):
                self.assertEqual(texts.result_sentence(**closed(exit_reason=reason, **LEVELS)),
                                 texts.result_sentence(**closed()))
        self.assertNotIn("limit", texts.result_sentence(**closed()))

    def test_labels(self):
        self.assertEqual(texts.exit_reason_label("stop"), "Hit the stop")
        self.assertEqual(texts.exit_reason_label("target"), "Hit the target")
        self.assertEqual(texts.exit_reason_label("time", hold_minutes=1440), "Closed at the 24-hour limit")
        self.assertEqual(texts.exit_reason_label("time", hold_minutes=90), "Closed at the 90-minute limit")
        self.assertEqual(texts.exit_reason_label("time"), "Closed at the time limit")
        for reason in (None, "", "TIME", 3):
            self.assertIsNone(texts.exit_reason_label(reason, hold_minutes=1440))

    def test_close_line_names_the_reason(self):
        net = Decimal("-0.45")
        self.assertEqual(texts.play_close_line(asset="ETH", outcome="LOSS", net=net, exit_reason="stop"),
                         "I closed the ETH play at the stop: lost €0.45.")
        self.assertEqual(texts.play_close_line(asset="ETH", outcome="WIN", net=Decimal("1.23"), exit_reason="target"),
                         "I closed the ETH play at the target: won €1.23.")
        self.assertEqual(texts.play_close_line(asset="ETH", outcome="LOSS", net=net, exit_reason="time",
                                               hold_minutes=1440),
                         "I closed the ETH play at the 24-hour limit: lost €0.45.")
        self.assertEqual(texts.play_close_line(exit_reason="time"), "I closed a play at the time limit.")
        self.assertEqual(texts.play_close_line(asset="ETH", outcome="LOSS", net=net, exit_reason=None),
                         "I closed the ETH play: lost €0.45.")
        recorded = {"asset": "ETH", "outcome": "WIN", "net": "1.23", "exit_reason": "time", "hold_minutes": 1440}
        text = texts.activity_line("play_close", recorded)
        self.assertEqual(text, "I closed the ETH play at the 24-hour limit: won €1.23.")
        self.assert_traceable(text, recorded)

    def test_steps_describe_the_stop_target_and_time_rule(self):
        facts = {"asset": "SOL", "direction": "LONG", "hold_minutes": 1440,
                 "stop": Decimal("96.5"), "target": Decimal("110.25")}
        third = texts.decision_steps(**facts)[2]
        self.assertEqual(third, {
            "title": "Now it waits",
            "text": "It closes by itself on the first price that reaches the loss limit of 96.5 or the profit goal "
                    "of 110.25, or after 24 hours at the latest.",
            "state": "now",
        })
        self.assert_traceable(third["text"], facts)
        self.assertNotIn("minutes", third["text"])
        no_hold = texts.decision_steps(stop=Decimal("1"), target=Decimal("2"))[2]["text"]
        self.assertIn("or at the time limit at the latest", no_hold)
        pending = texts.decision_steps(pending=True, **facts)[2]
        self.assertEqual(pending["title"], "Waiting for a price")
        legacy = texts.decision_steps(hold_minutes=60)[2]["text"]
        self.assertEqual(legacy, "It closes by itself after 1 hour, win or lose.")


class TestDecisionSteps(TextCase):
    def test_full_steps(self):
        facts = {"asset": "SOL", "direction": "LONG", "assets_eligible": 600, "scores": SCORES, "hold_minutes": 60}
        steps = texts.decision_steps(**facts)
        self.assertEqual([step["state"] for step in steps], ["done", "done", "now"])
        self.assertEqual([step["text"] for step in steps], [
            "Looked at 600 coins; SOL was moving unusually.",
            "Scored the alert: opportunity 64 out of a hundred, ease of trading 56 out of a hundred and "
            "medium confidence. Decision: betting it goes up.",
            "It closes by itself after 1 hour, win or lose.",
        ])
        for step in steps:
            self.assert_traceable(step["title"] + " " + step["text"], facts)
            self.assert_plain(step["title"] + " " + step["text"], "SOL")

    def test_missing_facts_drop_their_clause(self):
        steps = texts.decision_steps()
        self.assertEqual([step["text"] for step in steps], [
            "A coin was moving unusually.", "Decided to open the play.", "It closes by itself at the planned time, win or lose.",
        ])
        for step in steps:
            self.assert_no_digits(step["title"] + step["text"])
        self.assertEqual(texts.decision_steps(direction="SHORT", scores={"confidence": None})[1]["text"],
                         "Decision: betting it goes down.")

    def test_pending_and_hold_wording(self):
        pending = texts.decision_steps(pending=True, hold_minutes=60)[2]
        self.assertEqual(pending["title"], "Waiting for a price")
        self.assert_no_digits(pending["text"])
        self.assertIn("after 45 minutes", texts.decision_steps(hold_minutes=45)[2]["text"])
        self.assertIn("after 2 hours", texts.decision_steps(hold_minutes=120)[2]["text"])
        for hold in (0, -5, None, True, 1.5):
            self.assertIn("planned time", texts.decision_steps(hold_minutes=hold)[2]["text"])


class TestCostSentence(TextCase):
    def test_no_plays_no_sentence(self):
        self.assertIsNone(texts.cost_sentence([]))
        self.assertIsNone(texts.cost_sentence([None, float("nan")], [None]))

    def test_commission_only_without_closed_plays(self):
        text = texts.cost_sentence([Decimal("26"), Decimal("26")])
        self.assertEqual(text, "Every buy and every sell pays a commission of 0.26% of the amount at stake.")
        self.assertNotIn("average", text)
        self.assert_traceable(text, [Decimal("26")])
        self.assert_plain(text)

    def test_average_spread_of_closed_plays(self):
        spreads = [Decimal("0.35"), Decimal("0.40")]
        text = texts.cost_sentence([Decimal("26")], spreads)
        self.assertIn("cost €0.38 per play on average", text)  # 0.375, half to even
        self.assert_traceable(text, [Decimal("26")], extra={Decimal("0.38")})
        self.assert_plain(text)

    def test_different_frozen_fees_are_shown_as_a_range(self):
        text = texts.cost_sentence(["40", Decimal("26"), 26.0])
        self.assertIn("between 0.26% and 0.4%", text)
        self.assert_traceable(text, [Decimal("40"), Decimal("26")])


class TestValuationLabels(TextCase):
    def test_fee_provenance_is_the_stored_fee_labelled_as_assumed(self):
        text = texts.fee_provenance_text(Decimal("26"))
        self.assertEqual(text, "Assumed commission of 0.26% on the buy and again on the sell "
                               "(ASSUMED, account tier unverified).")
        self.assert_traceable(text, [Decimal("26")])
        self.assertEqual(texts.fee_provenance_text("40"), texts.fee_provenance_text(Decimal("40")))
        self.assertIn("0.4%", texts.fee_provenance_text("40"))

    def test_fee_provenance_without_a_usable_fee_prints_no_number(self):
        for value in (None, "abc", float("nan"), -1, True):
            with self.subTest(value=value):
                text = texts.fee_provenance_text(value)
                self.assert_no_digits(text)
                self.assertIn("ASSUMED, account tier unverified", text)

    def test_fx_label_says_hypothetical_and_not_inventory(self):
        text = texts.fx_excluded_text("USD", "EUR")
        self.assertEqual(text, "Hypothetical simulation: this pair is priced in USD; its price moves are scaled onto "
                               "the EUR stake with no exchange rate (FX excluded). This is not EUR inventory.")
        self.assert_no_digits(text)
        hostile = "<b>USD</b>"
        self.assertIn(hostile, texts.fx_excluded_text(hostile, "EUR"))  # data, inserted as text by the UI
        self.assert_no_digits(texts.fx_excluded_text(None, None))
        self.assertIn("another currency", texts.fx_excluded_text("", "EUR"))

    def test_freshness_rule_numbers_come_from_the_input(self):
        text = texts.freshness_text(10)
        self.assertIn("at most 10 minutes old", text)
        self.assert_traceable(text, [10])
        for value in (None, -1, 1.5, True):
            with self.subTest(value=value):
                self.assert_no_digits(texts.freshness_text(value))

    def test_fixed_labels_are_plain_and_print_no_number(self):
        for text in (*texts.MARK_REASONS.values(), texts.VALUATION_BASIS_TEXT, texts.PILOT_ACCOUNT_TEXT):
            with self.subTest(text=text):
                self.assert_no_digits(text)
                for word in ("None", "null", "nan", "bps", "zero"):
                    self.assertNotIn(word, text)
        self.assertEqual(set(texts.MARK_REASONS), {"missing_quote", "stale_quote", "invalid_quote"})
        self.assertIn("PAPER", texts.PILOT_ACCOUNT_TEXT)
        self.assertIn("not SHADOW_LIVE", texts.PILOT_ACCOUNT_TEXT)


class TestAgentLines(TextCase):
    def test_cycle(self):
        facts = {"assets_eligible": 42, "shortlist_count": 3, "warmup": 1, "api_failures": 2}
        text = texts.cycle_line(**facts)
        self.assertEqual(
            text,
            "Market sweep done: I looked at 42 coins and 3 made my watch list. "
            "I am still gathering data to see clearly. 2 requests to the exchange failed.",
        )
        self.assert_traceable(text, facts)
        self.assertIn("none made my watch list", texts.cycle_line(assets_eligible=1, shortlist_count=0))
        self.assertIn("I looked at 1 coin ", texts.cycle_line(assets_eligible=1, shortlist_count=0))
        self.assertIn("1 request to the exchange failed.", texts.cycle_line(api_failures=1))
        quiet = texts.cycle_line(warmup=0, api_failures=0)
        self.assertEqual(quiet, "Market sweep done.")
        for nulls in ({}, {"assets_eligible": None, "shortlist_count": None, "warmup": None, "api_failures": None},
                      {"assets_eligible": -1, "shortlist_count": True, "api_failures": 1.5}):
            self.assert_no_digits(texts.cycle_line(**nulls))

    def test_alert(self):
        self.assertEqual(texts.alert_line(asset="ETH", direction="LONG"), "Alert on ETH: it might go up.")
        self.assertEqual(texts.alert_line(asset="ETH", direction="SHORT"), "Alert on ETH: it might go down.")
        self.assertEqual(texts.alert_line(asset="ETH", direction="NONE"), "Alert on ETH: it is moving unusually.")
        self.assertEqual(texts.alert_line(), "New alert on the radar.")
        self.assertEqual(texts.alert_line(direction="LONG"), "New alert: a coin might go up.")
        for direction in ("LONG", "SHORT", None):
            text = texts.alert_line(asset="ETH", direction=direction)
            for certain in ("will", "!", "sure"):
                self.assertNotIn(certain, text)

    def test_qwen_reflects_status_and_only_recorded_verdicts(self):
        self.assertEqual(texts.qwen_line(asset="ETH", batch_status="OK", veto=0, confidence="MEDIUM"),
                         "I reviewed ETH: it looks good to me, with medium confidence.")
        self.assertEqual(texts.qwen_line(asset="ETH", batch_status="OK", veto=True, confidence="HIGH"),
                         "I reviewed ETH: I would stay out of this one, with high confidence.")
        self.assertEqual(texts.qwen_line(asset="ETH", batch_status="OK"), "I reviewed ETH.")
        for status in ("TIMEOUT", "UNAVAILABLE", "INVALID_JSON", "ERROR", None, "SOMETHING"):
            with self.subTest(status=status):
                text = texts.qwen_line(asset="ETH", batch_status=status, veto=True, confidence="HIGH")
                self.assertNotIn("confidence", text)
                self.assertNotIn("stay out", text)
                self.assert_plain(text, "ETH")
                self.assert_no_digits(text)
        lines = {texts.qwen_line(asset="ETH", batch_status=s) for s in ("TIMEOUT", "UNAVAILABLE", "INVALID_JSON", "ERROR")}
        self.assertEqual(len(lines), 4)

    def test_router_decision_says_only_that_the_alert_deserves_a_closer_look(self):
        self.assertEqual(texts.router_line(decision="SONNET", asset="ETH"),
                         "The router says the ETH alert deserves a closer look from me.")
        self.assertEqual(texts.router_line(decision="FABLE", asset="ETH"),
                         "The router woke me up: the ETH alert deserves a closer look from me.")
        self.assertEqual(texts.router_line(decision="SONNET"), "The router says this alert deserves a closer look from me.")
        self.assertEqual(texts.router_line(decision="FABLE", asset="  "),
                         "The router woke me up: this alert deserves a closer look from me.")
        for decision in ("SONNET", "FABLE"):
            for asset in ("ETH", None):
                with self.subTest(decision=decision, asset=asset):
                    text = texts.router_line(decision=decision, asset=asset)
                    self.assert_plain(text, asset)
                    self.assert_no_digits(text)
                    self.assertEqual("ETH" in text, asset == "ETH")
                    for word in ANALYSIS_WORDS:
                        self.assertNotIn(word, text.lower())
        self.assertIn("woke me", texts.router_line(decision="FABLE"))
        self.assertNotIn("woke", texts.router_line(decision="SONNET"))
        for decision in ("IGNORE", None, "", "OPUS", "sonnet", 2):
            with self.subTest(decision=decision):
                self.assertIsNone(texts.router_line(decision=decision, asset="ETH"))

    def test_no_analysis_template_exists(self):
        self.assertFalse(hasattr(texts, "model_line"))
        self.assertNotIn("model_line", texts.__all__)

    def test_router_activity_matches_its_kind(self):
        self.assertEqual(texts.activity_line("sonnet", {"asset": "ETH", "decision": "SONNET"}),
                         texts.router_line(decision="SONNET", asset="ETH"))
        self.assertEqual(texts.activity_line("fable", {"asset": "ETH"}),
                         texts.router_line(decision="FABLE", asset="ETH"))
        # A recorded model status never changes the line: there is no analysis to report.
        self.assertEqual(texts.activity_line("fable", {"asset": "ETH", "status": "SUCCESS"}),
                         texts.router_line(decision="FABLE", asset="ETH"))
        for kind, decision in (("sonnet", "FABLE"), ("fable", "SONNET"), ("sonnet", "IGNORE"), ("fable", "OPUS")):
            with self.subTest(kind=kind, decision=decision), self.assertRaises(ValueError):
                texts.activity_line(kind, {"asset": "ETH", "decision": decision})

    def test_play_open(self):
        facts = {"asset": "ETH", "direction": "SHORT", "stake": Decimal("100")}
        text = texts.play_open_line(**facts)
        self.assertEqual(text, "I put €100.00 of pretend money on ETH: betting it goes down.")
        self.assert_traceable(text, facts)
        self.assertEqual(texts.play_open_line(asset="ETH", direction="LONG"),
                         "I opened a pretend play on ETH: betting it goes up.")
        self.assertEqual(texts.play_open_line(), "I opened a pretend play.")
        self.assertEqual(texts.play_open_line(stake=Decimal("100")), "I put €100.00 of pretend money on a play.")

    def test_play_close(self):
        self.assertEqual(texts.play_close_line(asset="ETH", outcome="LOSS", net=Decimal("-0.45")),
                         "I closed the ETH play: lost €0.45.")
        self.assertEqual(texts.play_close_line(asset="ETH", outcome="WIN", net=Decimal("1.23")),
                         "I closed the ETH play: won €1.23.")
        self.assertEqual(texts.play_close_line(asset="ETH", outcome="FLAT", net=Decimal("0")),
                         "I closed the ETH play: broke even (€0.00).")
        self.assertEqual(texts.play_close_line(asset="ETH", outcome="WIN"), "I closed the ETH play: won.")
        self.assertEqual(texts.play_close_line(), "I closed a play.")

    def test_activity_line_covers_every_kind(self):
        facts = {
            "cycle": {"assets_eligible": 12, "shortlist_count": 2, "warmup": 0, "api_failures": 0},
            "alert": {"asset": "ETH", "direction": "LONG"},
            "qwen": {"asset": "ETH", "batch_status": "OK", "veto": False, "confidence": "LOW"},
            "sonnet": {"asset": "ETH", "decision": "SONNET"},
            "fable": {"asset": "ETH", "decision": "FABLE"},
            "play_open": {"asset": "ETH", "direction": "LONG", "stake": "100.00"},
            "play_close": {"asset": "ETH", "outcome": "LOSS", "net": "-0.45"},
        }
        self.assertEqual(set(facts), set(texts.ACTIVITY_KINDS))
        self.assertEqual(texts.AGENT_FOR_KIND, {
            "cycle": "scout", "alert": "scout", "qwen": "analyst", "sonnet": "strategist",
            "fable": "boss", "play_open": "treasurer", "play_close": "treasurer",
        })
        for kind, recorded in facts.items():
            with self.subTest(kind=kind):
                text = texts.activity_line(kind, recorded)
                self.assert_traceable(text, recorded)
                self.assert_plain(text, "ETH" if "asset" in recorded else None)
                empty = texts.activity_line(kind, {})
                self.assert_no_digits(empty)
                self.assert_plain(empty)
        with self.assertRaises(ValueError):
            texts.activity_line("gossip", {})


class TestBoundaries(unittest.TestCase):
    def test_stored_strings_are_returned_as_plain_text(self):
        hostile = '<img src=x onerror="alert(1)">'
        text = texts.alert_line(asset=hostile, direction="LONG")
        self.assertIn(hostile, text)  # passed as data; the UI inserts it with textContent
        self.assertEqual(text.count("<"), 1)

    def test_no_portuguese_left_in_the_templates(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "ui", "paper_texts.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIsNone(re.search("[ãáàâçéêíóõôú]", source))
        for word in (" aposta ", "jogada", "ganhou", "perdeu", "moeda", "comiss"):
            self.assertNotIn(word, source)

    def test_module_is_pure(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "ui", "paper_texts.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, ("open", "print", "input", "eval", "exec"))
        self.assertLessEqual(imported, {"__future__", "math", "collections.abc", "decimal"})


if __name__ == "__main__":
    unittest.main()
