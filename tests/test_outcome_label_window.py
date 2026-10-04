"""The symmetric nearest-quote window of outcome labels.

``RADAR_OUTCOME_LABEL_WINDOW`` selects the window: ``symmetric`` (default) takes the valid
quote of the subject's own pair nearest to the target within +/- tolerance, strictly after
the decision; ``forward`` keeps the original rule (first valid quote in [target, target +
tolerance]). Every database is a fixture in a fresh temporary directory (the outcome-label and label-wiring
harnesses); nothing reads or writes ``radar_state.sqlite``, ``runs.jsonl``,
``events.jsonl`` or ``radar.log``. No network, model or radar loop. Expected values are
worked out by hand in the comments.
"""

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, TESTS_DIR)

import test_integrity_wiring as wiring  # noqa: E402  (fake Kraken heartbeat harness)
import test_outcome_label_wiring as lwiring  # noqa: E402  (label wiring harness)
import test_outcome_labels as t041  # noqa: E402  (outcome-label fixtures: subject, quote, TempCase)

from radar_v08 import config, heartbeat  # noqa: E402
from radar_v08.adapters import outcome_store as ost  # noqa: E402
from radar_v08.adapters.outcome_store import (  # noqa: E402
    OutcomeStoreError,
    OutcomeStoreFailure,
)
from radar_v08.domain.outcomes import (  # noqa: E402
    Availability,
    Horizon,
    LabelWindow,
    MissingReason,
    NotMature,
    OutcomeInputError,
    label_horizon,
)

D = Decimal
T0 = t041.T0  # decision_as_of of t041.subject(); its entry quote is at T0 - 5 s
TOL = t041.TOL  # 120 s
TARGET = T0 + timedelta(minutes=15)  # the 15m horizon's target
SYM = LabelWindow.SYMMETRIC
FWD = LabelWindow.FORWARD
subject = t041.subject
quote = t041.quote


def at(seconds):
    """The 15m target shifted by ``seconds`` (negative: before the target)."""
    return TARGET + timedelta(seconds=seconds)


def label(quotes, now, window_kind=SYM, tolerance=TOL, horizon=Horizon.M15, **subject_changes):
    return label_horizon(
        subject(**subject_changes), horizon, quotes, now=now, tolerance=tolerance, window_kind=window_kind
    )


def not_mature(not_before, horizon=Horizon.M15):
    return NotMature(subject().subject_id, horizon, not_before)


# --- pure domain: symmetric -----------------------------------------------------------------------


class TestSymmetricNearestQuote(unittest.TestCase):
    def test_only_a_pre_target_quote_is_available_once_target_plus_its_distance_has_passed(self):
        # One quote 40 s before the target: mid (101.9 + 102.1) / 2 = 102 -> 102/100 - 1 = +0.02.
        quotes = [quote(at(-40), "101.9", "102.1", "pre")]
        self.assertEqual(label(quotes, now=at(0)), not_mature(at(40)))
        self.assertEqual(label(quotes, now=at(39)), not_mature(at(40)))
        result = label(quotes, now=at(40))
        self.assertIs(result.status, Availability.AVAILABLE)
        self.assertEqual((result.exit_source, result.exit_mid, result.market_return), ("pre", D("102"), D("0.02")))
        self.assertEqual(result.exit_observed_at, at(-40))
        self.assertEqual(result.label_available_at, at(40))  # target + |-40 s|
        self.assertEqual(result.exit_offset, timedelta(seconds=-40))
        # LONG net = 0.02 - 0.0077033034 = 0.0122966966.
        self.assertEqual(result.net_markout, D("0.0122966966"))

    def test_pre_and_post_target_quotes_pick_the_nearest(self):
        post_nearer = [quote(at(-30), "90", "90.2", "pre-30"), quote(at(20), "100.9", "101.1", "post+20")]
        result = label(post_nearer, now=at(20))
        self.assertEqual((result.exit_source, result.label_available_at), ("post+20", at(20)))
        self.assertEqual(result.exit_offset, timedelta(seconds=20))

        pre_nearer = [quote(at(-10), "100.9", "101.1", "pre-10"), quote(at(50), "90", "90.2", "post+50")]
        result = label(pre_nearer, now=at(60))
        self.assertEqual((result.exit_source, result.label_available_at), ("pre-10", at(10)))
        self.assertEqual(result.exit_offset, timedelta(seconds=-10))

    def test_equal_distance_picks_the_later_quote_then_the_smaller_source(self):
        tie = [quote(at(30), "100.9", "101.1", "post+30"), quote(at(-30), "90", "90.2", "pre-30")]
        result = label(tie, now=at(30))
        self.assertEqual((result.exit_source, result.exit_observed_at), ("post+30", at(30)))

        same_instant = [quote(at(-5), "90", "90.2", "b"), quote(at(-5), "100.9", "101.1", "a")]
        self.assertEqual(label(same_instant, now=at(5)).exit_source, "a")
        self.assertEqual(label(list(reversed(same_instant)), now=at(5)).exit_source, "a")

    def test_the_entry_quote_and_anything_up_to_the_decision_are_never_used(self):
        # tolerance 20 min > horizon 15 min: the window [T0 - 5 min, T0 + 35 min] reaches back
        # past the decision. The entry-time quote (T0 - 5 s) and a quote at exactly T0 are the
        # only quotes in it, and neither may be used.
        wide = timedelta(minutes=20)
        quotes = [quote(T0 - timedelta(seconds=5), "99.9", "100.1", "entry"), quote(T0, "99.9", "100.1", "decision")]
        self.assertEqual(label(quotes, now=at(60), tolerance=wide), not_mature(TARGET + wide))
        closed = label(quotes, now=TARGET + wide, tolerance=wide)
        self.assertIs(closed.status, Availability.UNAVAILABLE)
        self.assertIs(closed.market_missing, MissingReason.QUOTE_MISSING_AT_HORIZON)
        self.assertEqual(closed.label_available_at, TARGET + wide)
        # One microsecond after the decision is a candidate (15 min - 1 us from the target).
        just_after = T0 + timedelta(microseconds=1)
        picked = label([*quotes, quote(just_after, "99.9", "100.1", "after")], now=TARGET + wide, tolerance=wide)
        self.assertEqual((picked.exit_source, picked.exit_observed_at), ("after", just_after))
        # An invalid quote at the decision is not a candidate either: still "missing", not "invalid".
        invalid_at_decision = [quote(T0, "0", "100.1", "decision-invalid")]
        self.assertIs(
            label(invalid_at_decision, now=TARGET + wide, tolerance=wide).market_missing,
            MissingReason.QUOTE_MISSING_AT_HORIZON,
        )

    def test_no_quote_in_the_window_is_missing_only_after_target_plus_tolerance(self):
        outside = [quote(at(-121), "100", "100.2"), quote(at(121), "100", "100.2")]
        self.assertEqual(label(outside, now=at(119)), not_mature(at(120)))
        closed = label(outside, now=at(120))
        self.assertIs(closed.status, Availability.UNAVAILABLE)
        self.assertIs(closed.market_missing, MissingReason.QUOTE_MISSING_AT_HORIZON)
        self.assertEqual(closed.label_available_at, at(120))
        self.assertIsNone(closed.exit_offset)
        self.assertIsNone(closed.market_return)
        self.assertIs(closed.net_missing, MissingReason.GROSS_UNAVAILABLE)

    def test_a_pre_target_pick_waits_and_a_later_closer_quote_wins(self):
        early = [quote(at(-60), "90", "90.2", "pre-60")]
        self.assertEqual(label(early, now=at(30)), not_mature(at(60)))
        # A quote 45 s after the target arrives before target + 60 s and is nearer.
        arrived = [*early, quote(at(45), "100.9", "101.1", "post+45")]
        self.assertEqual(label(arrived, now=at(44)), not_mature(at(60)))  # not observed yet at 44 s
        result = label(arrived, now=at(45))
        self.assertEqual((result.exit_source, result.label_available_at), ("post+45", at(45)))
        # A later quote farther than 60 s never displaces the pre-target pick.
        farther = [*early, quote(at(61), "100.9", "101.1", "post+61")]
        self.assertEqual(label(farther, now=at(61)).exit_source, "pre-60")

    def test_invalid_only_candidates_are_quote_invalid_at_target_plus_tolerance(self):
        invalid = [
            quote(at(-50), "0", "100.2"),  # non-positive bid
            quote(at(-10), None, "100.2"),  # bid not observed
            quote(at(10), "100.3", "100.2"),  # crossed
        ]
        self.assertEqual(label(invalid, now=at(60)), not_mature(at(120)))
        closed = label(invalid, now=at(120))
        self.assertIs(closed.market_missing, MissingReason.QUOTE_INVALID_AT_HORIZON)
        self.assertEqual(closed.label_available_at, at(120))
        # An invalid nearer quote never beats a valid farther one.
        mixed = [*invalid, quote(at(-100), "100.9", "101.1", "valid-100")]
        self.assertEqual(label(mixed, now=at(100)).exit_source, "valid-100")

    def test_quotes_after_now_are_never_used(self):
        poisoned = [quote(at(-90), "90", "90.2", "pre-90"), quote(at(15), "120", "120.2", "future")]
        self.assertEqual(label(poisoned, now=at(10)), not_mature(at(90)))

    def test_no_price_source_and_before_target_are_unchanged(self):
        self.assertEqual(label([quote(at(-20), "99", "99.2")], now=at(-1)), not_mature(TARGET))
        unsupported = label(None, now=at(0))
        self.assertIs(unsupported.market_missing, MissingReason.PRICE_SOURCE_UNSUPPORTED)
        self.assertEqual(unsupported.label_available_at, TARGET)

    def test_window_kind_must_be_a_label_window(self):
        for bad in ("symmetric", None, 1):
            with self.subTest(bad=bad), self.assertRaises(OutcomeInputError):
                label([], now=at(200), window_kind=bad)


class TestLabelInvariant(unittest.TestCase):
    def test_available_at_is_target_plus_the_absolute_offset(self):
        pre = label([quote(at(-40), "101.9", "102.1", "pre")], now=at(40))
        with self.assertRaises(OutcomeInputError):
            replace(pre, label_available_at=pre.exit_observed_at)  # before the target
        with self.assertRaises(OutcomeInputError):
            replace(pre, label_available_at=at(41))
        post = label([quote(at(30), "101.9", "102.1", "post")], now=at(30))
        self.assertEqual(post.label_available_at, post.exit_observed_at)
        with self.assertRaises(OutcomeInputError):
            replace(post, label_available_at=at(31))


# --- pure domain: forward is the original rule ----------------------------------------------------


class TestForwardIsUnchanged(unittest.TestCase):
    """The same fixtures under ``FORWARD`` give the original results (worked out from its rule)."""

    def test_the_default_is_forward(self):
        fixtures = [
            ([quote(at(-40), "101.9", "102.1", "pre")], at(200)),
            ([quote(at(-30), "90", "90.2", "pre"), quote(at(20), "100.9", "101.1", "post")], at(20)),
            ([quote(at(-10), "100.9", "101.1", "pre"), quote(at(50), "90", "90.2", "post")], at(60)),
            ([quote(at(10), "0", "100.2")], at(60)),
        ]
        for quotes, now in fixtures:
            with self.subTest(now=now):
                self.assertEqual(
                    label_horizon(subject(), Horizon.M15, quotes, now=now, tolerance=TOL),
                    label(quotes, now=now, window_kind=FWD),
                )

    def test_forward_ignores_pre_target_quotes_and_takes_the_first_valid_one(self):
        only_pre = [quote(at(-40), "101.9", "102.1", "pre")]
        self.assertEqual(label(only_pre, now=at(40), window_kind=FWD), not_mature(at(120)))
        missing = label(only_pre, now=at(120), window_kind=FWD)
        self.assertIs(missing.market_missing, MissingReason.QUOTE_MISSING_AT_HORIZON)
        self.assertEqual(missing.label_available_at, at(120))

        pre_nearer = [quote(at(-10), "100.9", "101.1", "pre-10"), quote(at(50), "90", "90.2", "post+50")]
        first = label(pre_nearer, now=at(60), window_kind=FWD)
        self.assertEqual((first.exit_source, first.label_available_at, first.exit_offset), ("post+50", at(50), timedelta(seconds=50)))

        # First valid, not nearest: +20 s wins over nothing earlier; a later +5 s cannot exist.
        tie = [quote(at(30), "100.9", "101.1", "post+30"), quote(at(-30), "90", "90.2", "pre-30")]
        self.assertEqual(label(tie, now=at(30), window_kind=FWD).exit_source, "post+30")

        invalid_then_valid = [quote(at(10), "0", "100.2"), quote(at(90), "100.9", "101.1", "post+90")]
        self.assertEqual(label(invalid_then_valid, now=at(90), window_kind=FWD).exit_source, "post+90")

        # Inside the window with no quote yet it waits for the whole window, as before.
        self.assertEqual(label([], now=at(60), window_kind=FWD), not_mature(at(120)))


# --- adapter ------------------------------------------------------------------------------------


class TestLabelerSymmetricWindow(t041.TempCase):
    def test_a_pre_target_snapshot_is_written_once_target_plus_its_distance_has_passed(self):
        conn = self.migrated()
        s = subject()
        self.assertTrue(ost.register_subject(conn, s, now=T0))
        self.add_quote(conn, at(-30), 101.4, 101.6)  # mid 101.5 -> +0.015
        (row_id,) = self.query("SELECT id FROM spot_snapshots WHERE pair = ?", t041.PAIR)[0]

        waiting = ost.label_due_outcomes(conn, now=at(10), tolerance=TOL, window_kind=SYM)
        self.assertEqual((waiting.horizons_considered, waiting.written, waiting.not_mature), (1, 0, 1))
        self.assertEqual(self.query("SELECT COUNT(*) FROM outcome_labels"), [(0,)])

        run = ost.label_due_outcomes(conn, now=at(30), tolerance=TOL, window_kind=SYM)
        self.assertEqual((run.horizons_considered, run.labeled_available, run.labeled_unavailable), (1, 1, 0))
        rows = self.query(
            "SELECT status, exit_source, exit_observed_at, target_at, label_available_at, market_return "
            "FROM outcome_labels WHERE subject_id = ?",
            s.subject_id,
        )
        self.assertEqual(
            rows,
            [
                (
                    "AVAILABLE",
                    f"spot_snapshots:{row_id}",
                    "2026-09-19T10:14:30.000000+00:00",
                    "2026-09-19T10:15:00.000000+00:00",
                    "2026-09-19T10:15:30.000000+00:00",
                    "0.015",
                )
            ],
        )
        self.assertLess(rows[0][2], rows[0][3])  # exit_observed_at < target_at
        (outcome,) = ost.load_outcomes(conn, s.subject_id)  # the stored row loads back
        self.assertEqual(outcome.label.exit_offset, timedelta(seconds=-30))

    def test_the_decision_snapshot_is_never_read_even_with_a_wide_tolerance(self):
        conn = self.migrated()
        s = subject()
        ost.register_subject(conn, s, now=T0)
        self.add_quote(conn, T0 - timedelta(seconds=5), 99.9, 100.1)  # the entry quote
        self.add_quote(conn, T0, 99.9, 100.1)  # stamped exactly at the decision
        wide = timedelta(minutes=20)
        run = ost.label_due_outcomes(conn, now=TARGET + wide, tolerance=wide, window_kind=SYM)
        self.assertEqual((run.labeled_available, run.labeled_unavailable), (0, 1))
        (outcome,) = ost.load_outcomes(conn, s.subject_id)
        self.assertIs(outcome.label.market_missing, MissingReason.QUOTE_MISSING_AT_HORIZON)
        self.assertEqual(outcome.label.label_available_at, TARGET + wide)

    def test_the_same_fixture_in_forward_mode_is_missing_as_before(self):
        conn = self.migrated()
        s = subject()
        ost.register_subject(conn, s, now=T0)
        self.add_quote(conn, at(-30), 101.4, 101.6)
        waiting = ost.label_due_outcomes(conn, now=at(30), tolerance=TOL)  # default: forward
        self.assertEqual((waiting.written, waiting.not_mature), (0, 1))
        run = ost.label_due_outcomes(conn, now=at(120), tolerance=TOL, window_kind=FWD)
        self.assertEqual((run.labeled_available, run.labeled_unavailable), (0, 1))
        (outcome,) = ost.load_outcomes(conn, s.subject_id)
        self.assertIs(outcome.label.market_missing, MissingReason.QUOTE_MISSING_AT_HORIZON)

    def test_window_kind_must_be_a_label_window(self):
        conn = self.migrated()
        with self.assertRaises(OutcomeStoreError) as caught:
            ost.label_due_outcomes(conn, now=at(30), tolerance=TOL, window_kind="symmetric")
        self.assertIs(caught.exception.code, OutcomeStoreFailure.INVALID_ARGUMENT)


# --- config -------------------------------------------------------------------------------------


class TestParseOutcomeLabelWindow(unittest.TestCase):
    def test_unset_means_symmetric(self):
        self.assertEqual(config.OUTCOME_LABEL_WINDOWS, ("symmetric", "forward"))
        self.assertEqual(config.OUTCOME_LABEL_WINDOW_DEFAULT, "symmetric")
        self.assertEqual(config.parse_outcome_label_window(None), "symmetric")

    def test_both_values_are_trimmed_and_case_folded(self):
        for raw, expected in [
            ("symmetric", "symmetric"), (" Symmetric ", "symmetric"), ("forward", "forward"), ("\tFORWARD\n", "forward"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(config.parse_outcome_label_window(raw), expected)
                self.assertIsInstance(LabelWindow(config.parse_outcome_label_window(raw)), LabelWindow)

    def test_any_other_value_raises_naming_the_allowed_values(self):
        for raw in ["", "   ", "nearest", "both", "0", "symmetric,forward", "for ward"]:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    config.parse_outcome_label_window(raw)
                self.assertIn("symmetric, forward", str(caught.exception))

    def test_config_value_comes_from_the_helper(self):
        self.assertEqual(
            config.RADAR_OUTCOME_LABEL_WINDOW,
            config.parse_outcome_label_window(os.environ.get("RADAR_OUTCOME_LABEL_WINDOW")),
        )

    def test_an_invalid_value_refuses_to_load_the_config(self):
        # A separate interpreter, so this process's config is never reloaded.
        for raw in ["bogus", ""]:
            with self.subTest(raw=raw):
                env = dict(os.environ, RADAR_OUTCOME_LABEL_WINDOW=raw, PYTHONDONTWRITEBYTECODE="1")
                result = subprocess.run(
                    [sys.executable, "-B", "-c", "import radar_v08.config"],
                    cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("RADAR_OUTCOME_LABEL_WINDOW must be one of symmetric, forward", result.stderr)
        env = dict(os.environ, RADAR_OUTCOME_LABEL_WINDOW="forward", PYTHONDONTWRITEBYTECODE="1")
        loaded = subprocess.run(
            [sys.executable, "-B", "-c", "import radar_v08.config as c; print(c.RADAR_OUTCOME_LABEL_WINDOW)"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual((loaded.returncode, loaded.stdout.strip()), (0, "forward"), loaded.stderr)


# --- heartbeat wiring ---------------------------------------------------------------------------


class TestHeartbeatPassesTheConfiguredWindow(lwiring.LabelWiringBase):
    def setUp(self):
        super().setUp()
        # The oracle is "the cycle's now is the injected clock's first reading" (see
        # test_outcome_label_wiring's honesty cases); the corrected clock has its own tests.
        patcher = mock.patch.object(config, "RADAR_CLOCK_CORRECTION_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def label_cycle(self, window):
        kraken = wiring.FakeKraken(assets=("BTC",))
        self.run_full_cycle(kraken, wiring.StepClock(start=lwiring.FROZEN))
        pair = self.subject_rows()[0]["pair"]
        target = datetime.fromisoformat(self.subject_rows()[0]["decision_as_of"]) + timedelta(minutes=15)
        planted = target - timedelta(seconds=30)
        self.insert_future_snapshot(pair, "BTC", planted, 61000.0)
        with self.no_new_subjects(), mock.patch.object(config, "RADAR_OUTCOME_LABEL_WINDOW", window), \
                mock.patch.object(heartbeat.outcome_store, "label_due_outcomes", wraps=ost.label_due_outcomes) as spy:
            output = self.run_full_cycle(kraken, wiring.StepClock(start=target + timedelta(seconds=40)))
        self.assertEqual(spy.call_count, 1)
        (row,) = self.label_rows(horizon="15m")
        return spy.call_args.kwargs["window_kind"], output["funnel"], row, planted

    def test_symmetric_uses_the_nearer_pre_target_snapshot(self):
        window_kind, funnel, row, planted = self.label_cycle("symmetric")
        self.assertIs(window_kind, LabelWindow.SYMMETRIC)
        self.assertEqual(datetime.fromisoformat(row["exit_observed_at"]), planted)
        target = datetime.fromisoformat(row["target_at"])
        self.assertEqual(datetime.fromisoformat(row["label_available_at"]), target + (target - planted))
        self.assertAlmostEqual(float(D(row["exit_mid"])), 61000.0, delta=1.0)
        self.assertEqual(
            (funnel["outcome_labels_considered"], funnel["outcome_labels_available"],
             funnel["outcome_labels_unavailable"], funnel["outcome_labels_not_mature"]),
            (1, 1, 0, 0),
        )

    def test_forward_uses_the_cycles_own_post_target_snapshot(self):
        window_kind, funnel, row, planted = self.label_cycle("forward")
        self.assertIs(window_kind, LabelWindow.FORWARD)
        exit_observed_at = datetime.fromisoformat(row["exit_observed_at"])
        self.assertGreaterEqual(exit_observed_at, datetime.fromisoformat(row["target_at"]))
        self.assertEqual(datetime.fromisoformat(row["label_available_at"]), exit_observed_at)
        self.assertAlmostEqual(float(D(row["exit_mid"])), 60000.0, delta=1.0)
        self.assertEqual((funnel["outcome_labels_available"], funnel["outcome_labels_not_mature"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
