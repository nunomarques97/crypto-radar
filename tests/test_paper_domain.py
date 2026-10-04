"""Paper game sums (radar_v08/domain/paper.py): hand-computed LONG/SHORT cases in exact cents.

Every expected value below is worked out by hand in the comment next to it, with the
stake 100 and the fee 26 bps per leg (f = 0.0026) unless said otherwise.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from radar_v08.domain import paper
from radar_v08.domain.paper import Direction, Outcome, QuoteProblem, SkipReason

D = Decimal
STAKE = D("100")
FEE = D("26")


def quote(bid: str, ask: str) -> paper.Quote:
    checked = paper.validate_quote(D(bid), D(ask))
    assert isinstance(checked, paper.Quote)
    return checked


class TestSettle(unittest.TestCase):
    def assert_result(self, result: paper.PlayResult, gross: str, spread: str, fees: str, net: str, outcome: Outcome):
        self.assertEqual((result.gross_mid, result.spread_cost, result.fees, result.net), (D(gross), D(spread), D(fees), D(net)))
        self.assertEqual(result.outcome, outcome)
        # The displayed parts always add up to the net, exactly.
        self.assertEqual(result.gross_mid - result.spread_cost - result.fees, result.net)
        for amount in (result.gross_mid, result.spread_cost, result.fees, result.net):
            self.assertEqual(amount, amount.quantize(D("0.01")))

    def test_long_win(self):
        # entry 99/101 (mid 100), exit 109/111 (mid 110). Buy at 101, sell at 109.
        # gross_mid = 100*(110/100-1) = 10.00
        # price     = 100*(109/101-1) = 7.9207920792...  -> spread = 2.0792... = 2.08
        # fees      = 0.26 + 100*109/101*0.0026 = 0.26 + 0.2805940594 = 0.5405940594 -> 0.54
        # net       = 10.00 - 2.08 - 0.54 = 7.38
        result = paper.settle(Direction.LONG, STAKE, FEE, quote("99", "101"), quote("109", "111"))
        self.assertEqual((result.entry_price, result.exit_price), (D("101"), D("109")))
        self.assert_result(result, "10.00", "2.08", "0.54", "7.38", Outcome.WIN)

    def test_long_loss(self):
        # entry 99.9/100.1 (mid 100), exit 94.9/95.1 (mid 95).
        # gross_mid = -5.00; price = 100*(94.9/100.1-1) = -5.1948051948 -> spread 0.1948... = 0.19
        # fees = 0.26 + 100*94.9/100.1*0.0026 = 0.26 + 0.2464935065 = 0.5064935065 -> 0.51
        # net = -5.00 - 0.19 - 0.51 = -5.70
        result = paper.settle(Direction.LONG, STAKE, FEE, quote("99.9", "100.1"), quote("94.9", "95.1"))
        self.assert_result(result, "-5.00", "0.19", "0.51", "-5.70", Outcome.LOSS)

    def test_short_win(self):
        # entry 99/101 (mid 100), exit 89/91 (mid 90). Sell at 99, buy back at 91.
        # gross_mid = 100*(1-90/100) = 10.00; price = 100*(99-91)/99 = 8.0808... -> spread 1.9191... = 1.92
        # fees = 0.26 + 100*91/99*0.0026 = 0.26 + 0.2389898990 = 0.4989898990 -> 0.50
        # net = 10.00 - 1.92 - 0.50 = 7.58
        result = paper.settle(Direction.SHORT, STAKE, FEE, quote("99", "101"), quote("89", "91"))
        self.assertEqual((result.entry_price, result.exit_price), (D("99"), D("91")))
        self.assert_result(result, "10.00", "1.92", "0.50", "7.58", Outcome.WIN)

    def test_short_loss(self):
        # entry 99/101 (mid 100), exit 104/106 (mid 105).
        # gross_mid = 100*(1-1.05) = -5.00; price = 100*(99-106)/99 = -7.0707... -> spread 2.0707... = 2.07
        # fees = 0.26 + 100*106/99*0.0026 = 0.26 + 0.2783838384 = 0.5383838384 -> 0.54
        # net = -5.00 - 2.07 - 0.54 = -7.61
        result = paper.settle(Direction.SHORT, STAKE, FEE, quote("99", "101"), quote("104", "106"))
        self.assert_result(result, "-5.00", "2.07", "0.54", "-7.61", Outcome.LOSS)

    def test_flat_without_spread_or_fee(self):
        for direction in Direction:
            result = paper.settle(direction, STAKE, D("0"), quote("100", "100"), quote("100", "100"))
            self.assert_result(result, "0.00", "0.00", "0.00", "0.00", Outcome.FLAT)

    def test_unchanged_mid_loses_the_costs(self):
        # entry and exit 99/101: gross 0; LONG price = 100*(99/101-1) = -1.9801... -> spread 1.98
        # fees = 0.26 + 100*99/101*0.0026 = 0.26 + 0.2548514851 -> 0.51; net = -2.49
        result = paper.settle(Direction.LONG, STAKE, FEE, quote("99", "101"), quote("99", "101"))
        self.assert_result(result, "0.00", "1.98", "0.51", "-2.49", Outcome.LOSS)

    def test_cent_rounding_is_half_even(self):
        self.assertEqual(paper.cents(D("0.125")), D("0.12"))
        self.assertEqual(paper.cents(D("0.135")), D("0.14"))
        self.assertEqual(paper.cents(D("-0.125")), D("-0.12"))

    def test_rejects_unvalidated_input(self):
        good = quote("99", "101")
        with self.assertRaises(paper.PaperInputError):
            paper.settle("LONG", STAKE, FEE, good, good)  # type: ignore[arg-type]
        with self.assertRaises(paper.PaperInputError):
            paper.settle(Direction.LONG, 100.0, FEE, good, good)  # type: ignore[arg-type]
        with self.assertRaises(paper.PaperInputError):
            paper.settle(Direction.LONG, D("0"), FEE, good, good)
        with self.assertRaises(paper.PaperInputError):
            paper.settle(Direction.LONG, STAKE, D("-1"), good, good)
        with self.assertRaises(paper.PaperInputError):
            paper.settle(Direction.LONG, STAKE, FEE, (D(99), D(101)), good)  # type: ignore[arg-type]


class TestValidateQuote(unittest.TestCase):
    def test_valid_quotes(self):
        self.assertEqual(paper.validate_quote(D("1.5"), D("1.6")), paper.Quote(D("1.5"), D("1.6")))
        self.assertEqual(paper.validate_quote(1.5, 1.5, "online"), paper.Quote(D("1.5"), D("1.5")))
        self.assertEqual(paper.validate_quote(0.1, 0.3, None), paper.Quote(D("0.1"), D("0.3")))
        self.assertEqual(paper.validate_quote(2, 3, " Online "), paper.Quote(D(2), D(3)))
        self.assertEqual(paper.validate_quote(2, 3, ""), paper.Quote(D(2), D(3)))  # blank: not recorded

    def test_invalid_quotes_are_typed_reasons_never_zero(self):
        cases = [
            ((None, 1.0), QuoteProblem.MISSING),
            ((1.0, None), QuoteProblem.MISSING),
            (("1.0", 2.0), QuoteProblem.NOT_A_NUMBER),
            ((True, 2.0), QuoteProblem.NOT_A_NUMBER),
            ((float("nan"), 2.0), QuoteProblem.NOT_FINITE),
            ((1.0, float("inf")), QuoteProblem.NOT_FINITE),
            ((D("NaN"), D(2)), QuoteProblem.NOT_FINITE),
            ((0.0, 1.0), QuoteProblem.NOT_POSITIVE),
            ((1.0, -1.0), QuoteProblem.NOT_POSITIVE),
            ((2.0, 1.0), QuoteProblem.CROSSED),
        ]
        for (bid, ask), problem in cases:
            with self.subTest(bid=bid, ask=ask):
                self.assertIs(paper.validate_quote(bid, ask), problem)
        self.assertIs(paper.validate_quote(1.0, 2.0, "offline"), QuoteProblem.NOT_ONLINE)
        self.assertIs(paper.validate_quote(1.0, 2.0, "cancel_only"), QuoteProblem.NOT_ONLINE)
        self.assertIs(paper.validate_quote(1.0, 2.0, 1), QuoteProblem.NOT_ONLINE)


class TestAdmission(unittest.TestCase):
    def open_(self, *assets: str) -> list[paper.OpenPosition]:
        return [paper.OpenPosition(asset, STAKE) for asset in assets]

    def test_admits_a_new_play(self):
        self.assertIsNone(paper.admit("LONG", "BTC", self.open_("ETH"), D("1000"), STAKE, 3))
        self.assertIsNone(paper.admit("SHORT", "BTC", [], D("100"), STAKE, 3))

    def test_direction_must_be_long_or_short(self):
        for direction in ("NONE", "long", "", None, 1):
            with self.subTest(direction=direction):
                self.assertIs(paper.admit(direction, "BTC", [], D("1000"), STAKE, 3), SkipReason.NO_DIRECTION)

    def test_one_play_per_asset(self):
        self.assertIs(paper.admit("LONG", "BTC", self.open_("BTC"), D("1000"), STAKE, 3), SkipReason.ASSET_ALREADY_OPEN)

    def test_max_open(self):
        self.assertIs(paper.admit("LONG", "SOL", self.open_("BTC", "ETH", "XRP"), D("1000"), STAKE, 3), SkipReason.MAX_OPEN)
        self.assertIsNone(paper.admit("LONG", "SOL", self.open_("BTC", "ETH"), D("1000"), STAKE, 3))

    def test_available_cash_counts_open_stakes(self):
        # balance 250 - one open stake 100 = 150 available: a 100 play fits, then 50 does not.
        self.assertEqual(paper.available_cash(D("250"), self.open_("BTC")), D("150"))
        self.assertIsNone(paper.admit("LONG", "ETH", self.open_("BTC"), D("250"), STAKE, 3))
        self.assertIs(
            paper.admit("LONG", "SOL", self.open_("BTC", "ETH"), D("250"), STAKE, 3), SkipReason.INSUFFICIENT_CASH
        )
        self.assertIs(paper.admit("LONG", "SOL", [], D("99.99"), STAKE, 3), SkipReason.INSUFFICIENT_CASH)

    def test_bad_rules_raise(self):
        with self.assertRaises(paper.PaperInputError):
            paper.admit("LONG", "BTC", [], D("1000"), STAKE, 0)
        with self.assertRaises(paper.PaperInputError):
            paper.admit("LONG", "BTC", [], D("1000"), STAKE, True)  # type: ignore[arg-type]


class TestBalanceAndTime(unittest.TestCase):
    def test_balance_is_start_plus_nets_only(self):
        self.assertEqual(paper.balance(D("1000"), []), D("1000"))
        self.assertEqual(paper.balance(D("1000"), [D("7.38"), D("-5.70"), D("0.00")]), D("1001.68"))
        with self.assertRaises(paper.PaperInputError):
            paper.balance(D("1000"), [1.5])  # type: ignore[list-item]
        with self.assertRaises(paper.PaperInputError):
            paper.balance(D("0"), [])

    def test_due_at(self):
        start = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
        self.assertEqual(paper.due_at(start, 60), start + timedelta(hours=1))
        other = datetime(2026, 9, 29, 11, 0, tzinfo=timezone(timedelta(hours=1)))
        self.assertEqual(paper.due_at(other, 30), start + timedelta(minutes=30))
        with self.assertRaises(paper.PaperInputError):
            paper.due_at(datetime(2026, 9, 29, 10, 0), 60)
        with self.assertRaises(paper.PaperInputError):
            paper.due_at(start, 0)

    def test_parse_direction(self):
        self.assertIs(paper.parse_direction("LONG"), Direction.LONG)
        self.assertIs(paper.parse_direction("SHORT"), Direction.SHORT)
        self.assertIsNone(paper.parse_direction("NONE"))
        self.assertIsNone(paper.parse_direction(None))



T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
DUE = T0 + timedelta(hours=24)


class TestExitLevels(unittest.TestCase):
    def test_policy_constants_are_fixed(self):
        self.assertEqual(paper.EX1_POLICY_ID, "ex1_initial_paper_v1")
        self.assertEqual((paper.EX1_STOP_ATR_MULTIPLE, paper.EX1_TARGET_R_MULTIPLE), (D(2), D(2)))
        self.assertEqual(paper.EX1_MAX_HOLD_MINUTES, 1440)

    def test_long_levels_from_the_ask(self):
        # entry = ask 101; stop = 101 - 2*1.5 = 98; R = 3; target = 101 + 2*3 = 107
        levels = paper.exit_levels(Direction.LONG, quote("99", "101"), D("1.5"))
        assert levels is not None
        self.assertEqual(
            (levels.policy_id, levels.atr, levels.entry_price, levels.stop, levels.target, levels.hold_minutes),
            ("ex1_initial_paper_v1", D("1.5"), D("101"), D("98.0"), D("107.0"), 1440),
        )

    def test_short_levels_mirrored_on_the_bid(self):
        # entry = bid 99; stop = 99 + 2*1.5 = 102; R = 3; target = 99 - 2*3 = 93
        levels = paper.exit_levels(Direction.SHORT, quote("99", "101"), D("1.5"))
        assert levels is not None
        self.assertEqual((levels.entry_price, levels.stop, levels.target), (D("99"), D("102.0"), D("93.0")))

    def test_non_positive_levels_are_refused(self):
        self.assertIsNone(paper.exit_levels(Direction.LONG, quote("99", "101"), D("50.5")))  # stop 0
        self.assertIsNone(paper.exit_levels(Direction.SHORT, quote("99", "101"), D("24.75")))  # target 0
        self.assertIsNotNone(paper.exit_levels(Direction.SHORT, quote("99", "101"), D("24.7")))

    def test_validate_atr_never_reads_zero(self):
        self.assertEqual(paper.validate_atr(1.25), D("1.25"))
        self.assertEqual(paper.validate_atr(D("0.001")), D("0.001"))
        for value, problem in (
            (None, QuoteProblem.MISSING), ("1.0", QuoteProblem.NOT_A_NUMBER), (True, QuoteProblem.NOT_A_NUMBER),
            (float("nan"), QuoteProblem.NOT_FINITE), (float("inf"), QuoteProblem.NOT_FINITE),
            (D("NaN"), QuoteProblem.NOT_FINITE), (0.0, QuoteProblem.NOT_POSITIVE), (-1, QuoteProblem.NOT_POSITIVE),
        ):
            with self.subTest(value=value):
                self.assertIs(paper.validate_atr(value), problem)

    def test_bad_input_raises(self):
        with self.assertRaises(paper.PaperInputError):
            paper.exit_levels("LONG", quote("99", "101"), D(1))  # type: ignore[arg-type]
        with self.assertRaises(paper.PaperInputError):
            paper.exit_levels(Direction.LONG, quote("99", "101"), D(0))
        with self.assertRaises(paper.PaperInputError):
            paper.exit_levels(Direction.LONG, (D(99), D(101)), D(1))  # type: ignore[arg-type]


class TestExitDecision(unittest.TestCase):
    def decide(self, direction: Direction, bid: str, ask: str, at: datetime = T0 + timedelta(minutes=5)):
        stop, target = (D("98"), D("107")) if direction is Direction.LONG else (D("102"), D("93"))
        return paper.exit_decision(direction, stop, target, DUE, at, quote(bid, ask))

    def test_long_watches_the_bid(self):
        self.assertIsNone(self.decide(Direction.LONG, "98.01", "106"))
        self.assertIs(self.decide(Direction.LONG, "98", "99"), paper.ExitReason.STOP)
        self.assertIs(self.decide(Direction.LONG, "90", "91"), paper.ExitReason.STOP)  # gap through the stop
        self.assertIs(self.decide(Direction.LONG, "107", "108"), paper.ExitReason.TARGET)
        # The ask alone touching a level is not an exit for a LONG.
        self.assertIsNone(self.decide(Direction.LONG, "99", "107.5"))

    def test_short_watches_the_ask(self):
        self.assertIsNone(self.decide(Direction.SHORT, "93", "101.99"))
        self.assertIs(self.decide(Direction.SHORT, "101", "102"), paper.ExitReason.STOP)
        self.assertIs(self.decide(Direction.SHORT, "92", "93"), paper.ExitReason.TARGET)
        self.assertIsNone(self.decide(Direction.SHORT, "92.5", "94"))  # the bid alone is not an exit

    def test_time_on_the_first_quote_at_or_after_due(self):
        self.assertIsNone(self.decide(Direction.LONG, "100", "101", DUE - timedelta(microseconds=1)))
        self.assertIs(self.decide(Direction.LONG, "100", "101", DUE), paper.ExitReason.TIME)
        self.assertIs(self.decide(Direction.SHORT, "100", "101", DUE + timedelta(hours=1)), paper.ExitReason.TIME)

    def test_precedence_stop_then_target_then_time(self):
        # stop >= target can only happen with levels given by hand: both touched on one quote.
        crossed = paper.exit_decision(Direction.LONG, D("100"), D("99"), DUE, DUE, quote("99.5", "100"))
        self.assertIs(crossed, paper.ExitReason.STOP)
        self.assertIs(self.decide(Direction.LONG, "97", "98", DUE), paper.ExitReason.STOP)
        self.assertIs(self.decide(Direction.LONG, "108", "109", DUE), paper.ExitReason.TARGET)

    def test_bad_input_raises(self):
        for args in (
            ("LONG", D(98), D(107), DUE, T0, quote("99", "101")),
            (Direction.LONG, D(98), D(107), DUE, datetime(2026, 9, 29), quote("99", "101")),
            (Direction.LONG, D(0), D(107), DUE, T0, quote("99", "101")),
            (Direction.LONG, D(98), D(107), DUE, T0, (D(99), D(101))),
        ):
            with self.subTest(args=args), self.assertRaises(paper.PaperInputError):
                paper.exit_decision(*args)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
