"""Synthetic tests of the trend research engine, tax, metrics and strategy purity.

Every series here is generated in the test with hand-derived expectations; no fixture file, no
network (socket connections are refused for the whole module) and no runtime state.
"""

import ast
import math
import unittest
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from unittest import mock

from radar_v08.domain import trend_engine as E
from radar_v08.domain import trend_metrics as M
from radar_v08.domain import trend_strategies as S

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DOMAIN_MODULES = ("trend_engine", "trend_metrics", "trend_strategies", "trend_registry")
OPENS = [100.0, 110.0, 99.0, 120.0, 120.0, 132.0]
START = date(2025, 9, 25)

_patches = []


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
        patcher = mock.patch(target, _refuse_network)
        patcher.start()
        _patches.append(patcher)


def tearDownModule():
    while _patches:
        _patches.pop().stop()


def bars(opens, start=START):
    """Daily bars where close(i) = open(i+1); the last close equals the last open."""
    t0 = E.day_open_ms(start)
    out = []
    for i, o in enumerate(opens):
        c = opens[i + 1] if i + 1 < len(opens) else o
        out.append(E.Bar(t0 + i * E.MS_PER_DAY, o, max(o, c), min(o, c), c))
    return out


def panel(opens_by_inst, start=START):
    rows = {k: bars(v, start) for k, v in opens_by_inst.items()}
    return E.Panel(rows, list(rows))


@dataclass
class Rule:
    fn: object
    universe: tuple = ("X",)
    band: float = 0.0
    warmup_days: int = 0
    max_gross: float = 1.0
    name: str = "t"

    def weights(self, h):
        return self.fn(h)


def growth(rets):
    final = 1.0
    for r in rets:
        final *= 1 + r
    return final


class EngineTiming(unittest.TestCase):
    def test_history_holds_only_bars_closed_before_today(self):
        h = E.History(panel({"X": OPENS}), 3)
        self.assertEqual(h.today, date(2025, 9, 28))
        self.assertEqual(h.closes("X"), [110.0, 99.0, 120.0])
        self.assertEqual(h.opens("X", 1), [99.0])
        self.assertEqual(h.nbars("X"), 3)

    def test_strategy_sees_previous_close_and_trades_at_today_open(self):
        seen = []

        def rule(h):
            seen.append((h.today, h.closes("X")[-1] if h.nbars("X") else None))
            return {"X": 1.0}

        r = E.simulate(Rule(rule), panel({"X": OPENS}), E.ALL, fee=0.0)
        self.assertEqual(seen[0], (date(2025, 9, 25), None))
        self.assertEqual(seen[1], (date(2025, 9, 26), 110.0))  # close of 09-25 = open of 09-26
        # bought at the 09-25 open, so day 0 earns open(09-26) / open(09-25) - 1
        self.assertAlmostEqual(r.rets[0], 110.0 / 100.0 - 1, places=12)

    def test_full_position_returns_run_open_to_open(self):
        r = E.simulate(Rule(lambda h: {"X": 1.0}), panel({"X": OPENS}), E.ALL, fee=0.0)
        expect = [OPENS[i + 1] / OPENS[i] - 1 for i in range(len(OPENS) - 1)]
        self.assertEqual(len(r.rets), len(expect))
        for a, b in zip(r.rets, expect):
            self.assertAlmostEqual(a, b, places=12)
        self.assertEqual(r.days[-1], date(2025, 9, 29))  # the last bar only supplies the final open

    def test_cash_earns_zero(self):
        r = E.simulate(Rule(lambda h: {}), panel({"X": OPENS}), E.ALL, fee=0.004)
        self.assertTrue(all(x == 0 for x in r.rets))
        self.assertEqual(r.trades, 0)

    def test_window_days(self):
        p = panel({"X": [100.0] * 10}, start=date(2025, 9, 28))
        self.assertEqual([p.dates[i] for i in p.window_days(E.DEV, 0)], [date(2025, 9, d) for d in (28, 29, 30)] + [date(2025, 10, 1), date(2025, 10, 2)])
        self.assertEqual(p.dates[p.window_days(E.HOLDOUT, 0)[0]], date(2025, 10, 3))
        self.assertEqual(p.window_days(E.ALL, 3)[0], 3)
        with self.assertRaises(E.EngineError) as caught:
            E.simulate(Rule(lambda h: {}), p, E.Window(date(2025, 10, 6), None), 0.0)
        self.assertIs(caught.exception.code, E.EngineErrorCode.WINDOW_TOO_SHORT)


class EngineCosts(unittest.TestCase):
    def test_entry_fee_never_borrows(self):
        r = E.simulate(Rule(lambda h: {"X": 1.0}, band=0.05), panel({"X": OPENS}), E.ALL, fee=0.004)
        self.assertAlmostEqual(growth(r.rets), OPENS[-1] / OPENS[0] / 1.004, places=12)
        self.assertEqual(r.trades, 1)

    def test_fee_plus_slippage_per_leg_on_traded_notional(self):
        flat = [100.0] * 5
        rule = Rule(lambda h: {"X": 1.0} if h.today < date(2025, 9, 27) else {})
        r = E.simulate(rule, panel({"X": flat}), E.ALL, fee=0.001, slippage_bps=9)
        cost = 0.001 + 0.0009
        bought = 1 / (1 + cost)
        self.assertAlmostEqual(growth(r.rets), bought * (1 - cost), places=12)
        self.assertAlmostEqual(r.turnover, bought + 1.0, places=9)
        self.assertEqual(r.trades, 2)
        self.assertAlmostEqual(r.equity[-1], bought * (1 - cost), places=12)

    def test_band_compares_target_with_drifted_weight(self):
        # 50% target, price doubles: drifted weight 2/3; band 0.2 holds, band 0.1 rebalances
        opens = [100.0, 100.0, 200.0, 200.0]
        loose = E.simulate(Rule(lambda h: {"X": 0.5}, band=0.2), panel({"X": opens}), E.ALL, fee=0.0)
        tight = E.simulate(Rule(lambda h: {"X": 0.5}, band=0.1), panel({"X": opens}), E.ALL, fee=0.0)
        self.assertEqual(loose.trades, 1)
        self.assertEqual(tight.trades, 2)
        self.assertAlmostEqual(tight.turnover, 0.5 + (2 / 3 - 0.5), places=12)

    def test_zero_target_always_exits_inside_the_band(self):
        # 0.6 bought at 100; the price falls to 20 so the drifted weight is 0.12 / 0.52 = 0.23, inside
        # the 0.5 band. A small target keeps the position; a zero target exits anyway.
        opens = [100.0, 20.0, 20.0, 20.0]
        exit_rule = Rule(lambda h: {"X": 0.6} if h.today == START else {}, band=0.5)
        hold_rule = Rule(lambda h: {"X": 0.6} if h.today == START else {"X": 0.01}, band=0.5)
        exited = E.simulate(exit_rule, panel({"X": opens}), E.ALL, fee=0.0)
        held = E.simulate(hold_rule, panel({"X": opens}), E.ALL, fee=0.0)
        self.assertEqual(exited.trades, 2)
        self.assertEqual(exited.targets[1], {})
        self.assertEqual(exited.rets[1], 0.0)
        self.assertEqual(held.trades, 1)

    def test_sells_run_before_buys_and_fund_them(self):
        # day 0 all in X; day 1 switch to Y at flat prices. Selling X first leaves (1-c)/(1+c) in
        # cash, and the Y buy is scaled to that cash so its fee never borrows: (1-c)/(1+c)^2.
        c = 0.01
        p = panel({"X": [100.0] * 3, "Y": [50.0] * 3})
        rule = Rule(lambda h: {"X": 1.0} if h.today == START else {"Y": 1.0}, universe=("X", "Y"))
        r = E.simulate(rule, p, E.ALL, fee=c)
        self.assertEqual(r.trades, 3)
        self.assertAlmostEqual(r.equity[-1], (1 - c) / (1 + c) ** 2, places=12)
        self.assertTrue(all(x > 0 for x in r.equity))

    def test_constant_weight_mode_rebalances_for_free(self):
        opens = [100.0, 100.0, 200.0, 100.0]
        r = E.simulate(Rule(lambda h: {"X": 0.5}, band=0.2), panel({"X": opens}), E.ALL, fee=0.0,
                       accounting=E.Accounting.CONSTANT_WEIGHT)
        self.assertEqual([round(x, 12) for x in r.rets], [0.0, 0.5, -0.25])
        units = E.simulate(Rule(lambda h: {"X": 0.5}, band=0.2), panel({"X": opens}), E.ALL, fee=0.0)
        self.assertAlmostEqual(units.rets[1], 0.5 * 1.0, places=12)
        self.assertAlmostEqual(units.rets[2], (0.5 + 0.5 * 2 * 0.5) / 1.5 - 1, places=12)  # drifted to 2/3
        with self.assertRaises(E.EngineError):
            E.simulate(Rule(lambda h: {"X": 0.5}), panel({"X": opens}), E.ALL, 0.0,
                       accounting=E.Accounting.CONSTANT_WEIGHT, tax=True)


class EngineValidation(unittest.TestCase):
    def assertCode(self, code, fn):
        with self.assertRaises(E.EngineError) as caught:
            fn()
        self.assertIs(caught.exception.code, code)

    def test_weights_are_validated(self):
        p = panel({"X": OPENS})
        self.assertCode(E.EngineErrorCode.INVALID_WEIGHT, lambda: E.simulate(Rule(lambda h: {"X": -0.5}), p, E.ALL, 0.0))
        self.assertCode(E.EngineErrorCode.INVALID_WEIGHT, lambda: E.simulate(Rule(lambda h: {"X": math.nan}), p, E.ALL, 0.0))
        self.assertCode(E.EngineErrorCode.GROSS_EXCEEDED, lambda: E.simulate(Rule(lambda h: {"X": 1.5}), p, E.ALL, 0.0))
        self.assertCode(E.EngineErrorCode.UNKNOWN_INSTRUMENT, lambda: E.simulate(Rule(lambda h: {"Y": 0.5}), p, E.ALL, 0.0))
        self.assertCode(E.EngineErrorCode.INVALID_PARAMETER, lambda: E.simulate(Rule(lambda h: {}), p, E.ALL, -0.001))

    def test_only_spot_is_ported(self):
        self.assertCode(E.EngineErrorCode.NOT_SPOT, lambda: E.Panel({"PERP:XUSDT": bars(OPENS)}, ["PERP:XUSDT"]))

    def test_bar_rows_are_validated(self):
        t0 = E.day_open_ms(START)
        good = [[t0, 1.0, 2.0, 0.5, 1.5], [t0 + E.MS_PER_DAY, 1.5, 2.0, 1.0, 1.2, "extra"]]
        self.assertEqual(len(E.bars_from_rows(good)), 2)
        for rows, code in (
            ([[t0, 1.0, 2.0, 0.5, 1.5], [t0, 1.0, 2.0, 0.5, 1.5]], E.EngineErrorCode.NON_MONOTONIC),
            ([[t0 + E.MS_PER_DAY, 1.0, 2.0, 0.5, 1.5], [t0, 1.0, 2.0, 0.5, 1.5]], E.EngineErrorCode.NON_MONOTONIC),
            ([[t0 + 1, 1.0, 2.0, 0.5, 1.5]], E.EngineErrorCode.INVALID_BAR),
            ([[t0, math.nan, 2.0, 0.5, 1.5]], E.EngineErrorCode.INVALID_BAR),
            ([[t0, 1.0, 2.0, 0.5, math.inf]], E.EngineErrorCode.INVALID_BAR),
            ([[t0, 1.0, 2.0, 0.5, 0.0]], E.EngineErrorCode.INVALID_BAR),
            ([[t0, True, 2.0, 0.5, 1.5]], E.EngineErrorCode.INVALID_BAR),
            ([[t0, "1.0", 2.0, 0.5, 1.5]], E.EngineErrorCode.INVALID_BAR),
            ([[t0, 1.0, 2.0, 0.5]], E.EngineErrorCode.INVALID_BAR),
        ):
            self.assertCode(code, lambda rows=rows: E.bars_from_rows(rows))


class LookaheadAudit(unittest.TestCase):
    def test_refuses_a_strategy_that_peeks(self):
        p = panel({"X": OPENS})
        cheat = Rule(lambda h: {"X": h._panel.rows["X"][-1].close / 1000})  # reads the last bar of the whole series
        with self.assertRaises(E.EngineError) as caught:
            E.audit_lookahead(cheat, p, list(range(1, len(OPENS) - 1)))
        self.assertIs(caught.exception.code, E.EngineErrorCode.LOOKAHEAD)

    def test_refuses_a_strategy_that_keeps_state(self):
        calls = []

        def stateful(h):
            calls.append(1)
            return {"X": 0.5 if len(calls) % 2 else 1.0}

        with self.assertRaises(E.EngineError) as caught:
            E.audit_lookahead(Rule(stateful), panel({"X": OPENS}), [2, 3])
        self.assertIs(caught.exception.code, E.EngineErrorCode.LOOKAHEAD)

    def test_accepts_an_honest_strategy(self):
        p = panel({"X": OPENS})
        honest = Rule(lambda h: {"X": 1.0 if h.nbars("X") > 1 and h.closes("X")[-1] > h.closes("X")[-2] else 0.0})
        E.audit_lookahead(honest, p, list(range(0, len(OPENS) - 1)))

    def test_ported_rules_are_pure_on_a_synthetic_series(self):
        opens = [100.0 * (1 + 0.03 * math.sin(i / 7.0)) + i * 0.2 for i in range(320)]
        p = E.Panel({"BTCUSDT": bars(opens, date(2024, 1, 1))}, ["BTCUSDT"])
        for rule in (S.Ens("BTCUSDT"), S.Ens("BTCUSDT", 0.5), S.BtcTrend5(), S.BtcTrend5Vt(), S.BuyAndHold("BTCUSDT")):
            days = p.window_days(E.ALL, rule.warmup_days)
            E.audit_lookahead(rule, p, days)
            first = E.History(p, days[0])
            self.assertEqual(rule.weights(first), rule.weights(first), rule.name)

    def test_ported_rules_ignore_the_fill_day_bar_and_later_on_every_day(self):
        # Exhaustive, unlike the sampled audit: for every evaluated day D, rewriting the bars of D
        # and after (the D open is the fill price) leaves the weights for D unchanged.
        opens = [100.0 * (1 + 0.25 * math.sin(i / 11.0)) + i * 0.1 for i in range(300)]
        original = bars(opens, date(2024, 1, 1))
        p = E.Panel({"BTCUSDT": original}, ["BTCUSDT"])
        vt = S.BtcTrend5Vt()
        rules = (S.Ens("BTCUSDT"), S.Ens("BTCUSDT", 0.5), S.BtcTrend5(), vt)
        for i in p.window_days(E.ALL, S.WARMUP_DAYS):
            future = [
                E.Bar(b.open_time_ms, b.open * f, b.high * f, b.low * f, b.close * f)
                for b, f in zip(original[i:], (3.0 if k % 2 else 0.2 for k in range(len(original) - i)))
            ]
            changed = E.Panel({"BTCUSDT": original[:i] + future}, ["BTCUSDT"])
            for rule in rules:
                with self.subTest(rule=rule.name, day=p.dates[i]):
                    self.assertEqual(rule.weights(E.History(p, i)), rule.weights(E.History(changed, i)))
        # the volatility overlay is fixed for a week: every day uses the scale of its UTC Monday
        days = p.window_days(E.ALL, S.WARMUP_DAYS)
        scales = {p.dates[i]: S.weekly_vol_scale(E.History(p, i).bars("BTCUSDT"), p.dates[i]) for i in days}
        for d, scale in scales.items():
            monday = date.fromordinal(d.toordinal() - d.weekday())
            if monday in scales:
                self.assertEqual(scale, scales[monday], d)
        self.assertGreater(len(set(scales.values())), 1)


class FifoTaxRules(unittest.TestCase):
    def test_short_term_gain_is_taxed_at_28_percent_fifo(self):
        t = E.FifoTax()
        t.trade("X", 1, 100, 0, date(2024, 1, 1))
        t.trade("X", 1, 200, 0, date(2024, 1, 10))
        t.trade("X", -1, 300, 0, date(2024, 1, 20))  # FIFO closes the 100 lot
        self.assertAlmostEqual(t.realized[2024], 200)
        self.assertAlmostEqual(t.tax_for(2024), 56)
        self.assertEqual(len(t.lots["X"]), 1)
        self.assertEqual(t.lots["X"][0].unit_cost, 200)

    def test_lot_held_365_days_or_more_is_exempt_including_losses(self):
        t = E.FifoTax()
        t.trade("X", 1, 100, 0, date(2023, 1, 1))
        t.trade("X", -1, 500, 0, date(2024, 1, 1))  # exactly 365 days: exempt gain
        self.assertEqual(t.tax_for(2024), 0)
        t.trade("Y", 1, 100, 0, date(2023, 1, 1))
        t.trade("Y", -1, 10, 0, date(2024, 6, 1))  # exempt loss is not deductible either
        self.assertEqual(t.realized.get(2024, 0.0), 0.0)
        t.trade("Z", 1, 100, 0, date(2023, 1, 2))
        t.trade("Z", -1, 150, 0, date(2024, 1, 1))  # 364 days: taxable
        self.assertAlmostEqual(t.tax_for(2024), 0.28 * 50)

    def test_losses_offset_gains_only_within_the_calendar_year(self):
        t = E.FifoTax()
        t.trade("X", 1, 100, 0, date(2024, 2, 1))
        t.trade("X", -1, 50, 0, date(2024, 3, 1))  # -50 in 2024
        t.trade("X", 1, 100, 0, date(2024, 4, 1))
        t.trade("X", -1, 180, 0, date(2024, 5, 1))  # +80 in 2024
        t.trade("X", 1, 100, 0, date(2024, 12, 1))
        t.trade("X", -1, 60, 0, date(2024, 12, 20))  # -40 in 2024: net -10
        t.trade("X", 1, 100, 0, date(2025, 1, 5))
        t.trade("X", -1, 150, 0, date(2025, 1, 6))  # +50 in 2025, no carry-forward of 2024's loss
        self.assertAlmostEqual(t.realized[2024], -10)
        self.assertEqual(t.tax_for(2024), 0)
        self.assertAlmostEqual(t.tax_for(2025), 14)

    def test_fees_reduce_the_gain_and_lots_split(self):
        t = E.FifoTax()
        t.trade("X", 2, 100, 2, date(2024, 1, 1))  # cost 101 per unit
        t.trade("X", -1, 150, 1.5, date(2024, 2, 1))  # 150 - 101 - 1.5 = 47.5
        self.assertAlmostEqual(t.realized[2024], 47.5)
        self.assertAlmostEqual(t.lots["X"][0].qty, 1)

    def test_tax_is_paid_at_year_end(self):
        opens = [100.0, 100.0, 150.0, 150.0, 150.0]  # 2025-12-29 .. 2026-01-02
        rule = Rule(lambda h: {"X": 1.0} if h.today < date(2025, 12, 31) else {})
        p = panel({"X": opens}, start=date(2025, 12, 29))
        r = E.simulate(rule, p, E.ALL, fee=0.0, tax=True)
        self.assertAlmostEqual(r.tax_paid, 0.28 * 0.5)
        self.assertAlmostEqual(r.realized_by_year[2025], 0.5)
        # paid at the 2026-01-01 open: the 2025-12-31 equity is untaxed, the 2026-01-01 one is not
        self.assertAlmostEqual(r.equity[2], 1.5)
        self.assertAlmostEqual(r.equity[3], 1.5 - 0.14)
        self.assertAlmostEqual(growth(r.rets), 1.5 - 0.14)
        untaxed = E.simulate(rule, p, E.ALL, fee=0.0)
        self.assertAlmostEqual(growth(untaxed.rets), 1.5)

    def test_final_partial_year_is_paid_at_the_end_of_the_run(self):
        opens = [100.0, 100.0, 150.0, 150.0]  # 2026-01-05 .. 2026-01-08, no year change
        rule = Rule(lambda h: {"X": 1.0} if h.today < date(2026, 1, 7) else {})
        r = E.simulate(rule, panel({"X": opens}, start=date(2026, 1, 5)), E.ALL, fee=0.0, tax=True)
        self.assertAlmostEqual(r.tax_paid, 0.14)
        self.assertAlmostEqual(r.equity[-2], 1.5)
        self.assertAlmostEqual(r.equity[-1], 1.36)

    def test_tax_bill_sells_spot_pro_rata_when_cash_is_short(self):
        # buy 0.01 at 100; sell half at 200 (gain 0.5 in 2025); buy back with all cash; at the
        # 2026-01-01 open the 0.14 bill sells 7% of the position. That sale realizes 0.07 in 2026,
        # paid at the end of the run (0.0196), again from a pro-rata sale. As in the reference
        # harness, the gain realized by that last sale (0.0098) is booked but never taxed again.
        opens = [100.0, 200.0, 200.0, 200.0, 200.0]  # 2025-12-29 .. 2026-01-02
        rule = Rule(lambda h: {"X": 0.5} if h.today == date(2025, 12, 30) else {"X": 1.0}, band=0.1)
        r = E.simulate(rule, panel({"X": opens}, start=date(2025, 12, 29)), E.ALL, fee=0.0, tax=True)
        self.assertAlmostEqual(r.realized_by_year[2025], 0.5)
        self.assertAlmostEqual(r.realized_by_year[2026], 0.07 + 0.0098, places=12)
        self.assertAlmostEqual(r.tax_paid, 0.14 + 0.0196, places=12)
        self.assertAlmostEqual(r.equity[3], 2.0 - 0.14, places=12)
        self.assertAlmostEqual(r.equity[-1], 2.0 - 0.14 - 0.0196, places=12)
        self.assertEqual(r.trades, 5)


class Metrics(unittest.TestCase):
    def test_performance_basics(self):
        rets = [0.01, -0.02] * 400
        days = [date(2020, 1, 1)] * 800
        m = M.performance(rets, days, turnover=4.0, exposure_sum=400.0)
        self.assertLess(m.mdd, 0)
        self.assertEqual(m.max_dd, -m.mdd)
        self.assertAlmostEqual(m.turnover_yr, 4.0 / (800 / 365))
        self.assertAlmostEqual(m.avg_exposure, 0.5)
        self.assertEqual(set(m.year_returns), {2020})

    def test_pos12m_is_nan_without_a_full_rolling_year(self):
        m = M.performance([0.001] * 365, [date(2025, 1, 1)] * 365)
        self.assertTrue(math.isnan(m.pos12m))
        self.assertEqual(M.performance([0.001] * 366, [date(2025, 1, 1)] * 366).pos12m, 1.0)

    def test_performance_refuses_bad_input(self):
        with self.assertRaises(M.TrendMetricsError):
            M.performance([0.1], [date(2025, 1, 1)])
        with self.assertRaises(M.TrendMetricsError):
            M.performance([0.1, math.nan], [date(2025, 1, 1)] * 2)
        with self.assertRaises(M.TrendMetricsError):
            M.performance([0.1, 0.2], [date(2025, 1, 1)])

    def test_dsr_penalizes_trials(self):
        one, _ = M.deflated_sharpe(0.05, 2000, 0.0, 3.0, 1, 0.0004)
        many, sr0 = M.deflated_sharpe(0.05, 2000, 0.0, 3.0, 100, 0.0004)
        self.assertGreater(one, many)
        self.assertAlmostEqual(sr0, M.expected_max_sharpe(100, 0.0004))
        self.assertGreater(sr0, 0)

    def test_trial_sharpe_variance_is_floored_at_one_over_t(self):
        self.assertEqual(M.trial_sharpe_variance([0.05], 400), 1 / 400)
        self.assertEqual(M.trial_sharpe_variance([0.05, 0.05], 400), 1 / 400)
        wide = [0.0, 1.0]
        self.assertEqual(M.trial_sharpe_variance(wide, 400), 0.5)
        self.assertEqual(M.trial_sharpe_variance([0.05], 400, floor=False), 0.0)


class StrategyArithmetic(unittest.TestCase):
    def test_ens_fraction_and_vol_target(self):
        closes = [100.0] * 199 + [101.0]  # last close above every SMA
        self.assertEqual(S.ens_fraction(closes), 1.0)
        self.assertEqual(S.ens_fraction([101.0] + [100.0] * 199), 0.0)
        self.assertEqual(S.realized_vol([100.0] * 31), 0.0)
        with self.assertRaises(S.StrategyError):
            S.ens_target([100.0] * 200, 0.5)  # zero volatility
        with self.assertRaises(S.StrategyError):
            S.ens_fraction([100.0] * 199)

    def test_donchian_most_recent_event_decides(self):
        rising = [float(i) for i in range(1, 30)]
        self.assertTrue(S.donchian_long(rising, 20, 10))
        falling = list(reversed(rising))
        self.assertFalse(S.donchian_long(falling, 20, 10))
        self.assertFalse(S.donchian_long([5.0] * 30, 20, 10))  # neither event: flat

    def test_registrations(self):
        self.assertEqual(S.Ens("BTCUSDT").registration.name, "ens_btc")
        self.assertEqual(S.Ens("BTCUSDT", 0.5).registration.name, "ens_vt_btc")
        self.assertIsNone(S.Ens("ETHUSDT").registration)
        self.assertIsNone(S.Ens("BTCUSDT", 0.4).registration)
        self.assertEqual(S.BuyAndHold("BTCUSDT").registration.name, "bh_btc")
        self.assertIsNone(S.BuyAndHold("ETHUSDT").registration)
        for name in S.REGISTERED:
            self.assertEqual(S.registration_of(S.ported_rule(name)).name, name)
        with self.assertRaises(S.StrategyError):
            S.ported_rule("xs_mom10")


class ModuleBoundary(unittest.TestCase):
    ALLOWED = {
        "__future__", "bisect", "collections", "collections.abc", "dataclasses", "datetime", "enum", "hashlib",
        "json", "math", "re", "statistics", "types", "typing",
        ".trend_engine", ".trend_metrics",
    }

    def test_domain_modules_import_no_io(self):
        for module in DOMAIN_MODULES:
            path = REPOSITORY_ROOT / "radar_v08" / "domain" / f"{module}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add("." * node.level + (node.module or ""))
            self.assertLessEqual(imported, self.ALLOWED, module)


if __name__ == "__main__":
    unittest.main()
