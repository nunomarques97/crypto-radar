"""Pilot shadow configuration: the flag and the envelope settings.

``build_pilot_envelope`` is checked directly on plain mappings; the import-time behaviour
(the flag, and a looser-than-EX-1 value refusing to load) is checked in a child process
with a temporary state dir, as for the paper game's settings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal

REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPOSITORY_ROOT)

from radar_v08 import config, pilot_shadow  # noqa: E402
from radar_v08.adapters import pilot_store  # noqa: E402
from radar_v08.domain import risk  # noqa: E402

D = Decimal

_READ_CONFIG = """
import json
from radar_v08 import config
e = config.PILOT_ENVELOPE
print(json.dumps([config.RADAR_PILOT_ENABLED, str(e.equity), e.currency, e.max_positions,
                  str(e.per_entry_loss_pct), str(e.drawdown_pct)]))
"""


def _child(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="pilot-config-state-") as state_dir:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("RADAR_")}
        environment["RADAR_STATE_DIR"] = state_dir
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.update(extra_env)
        return subprocess.run(
            [sys.executable, "-B", "-c", _READ_CONFIG], cwd=REPOSITORY_ROOT, env=environment,
            capture_output=True, text=True, timeout=60,
        )


class TestEnvelopeFromEnvironment(unittest.TestCase):
    def test_defaults_are_the_ex1_envelope_of_240_eur(self):
        envelope = config.build_pilot_envelope({})
        self.assertEqual(
            (envelope.equity, envelope.currency, envelope.per_entry_loss_pct, envelope.aggregate_loss_pct,
             envelope.gross_notional_pct, envelope.cash_buffer_pct, envelope.daily_loss_pct,
             envelope.drawdown_pct, envelope.per_entry_abs_cap, envelope.max_positions, envelope.leverage),
            (D("240.00"), "EUR", D("0.25"), D("0.50"), D("10"), D("10"), D("1"), D("3"), None, 1, 0),
        )
        # The same recorded text as the service's own default: no spurious envelope_changed.
        self.assertEqual(pilot_store.envelope_sha256(envelope), pilot_store.envelope_sha256(pilot_shadow.default_envelope()))

    def test_how_a_value_is_written_does_not_change_the_recorded_envelope(self):
        default = pilot_store.envelope_sha256(config.build_pilot_envelope({}))
        same = config.build_pilot_envelope({
            "RADAR_PILOT_EQUITY_EUR": " 240 ", "RADAR_PILOT_PER_ENTRY_LOSS_PCT": "0.250",
            "RADAR_PILOT_GROSS_NOTIONAL_PCT": "10.0", "RADAR_PILOT_DRAWDOWN_PCT": "3.00",
        })
        self.assertEqual(same.equity, D("240.00"))
        self.assertEqual(pilot_store.envelope_sha256(same), default)
        stricter = config.build_pilot_envelope({"RADAR_PILOT_PER_ENTRY_LOSS_PCT": "0.200"})
        self.assertEqual(str(stricter.per_entry_loss_pct), "0.2")
        self.assertEqual(stricter.per_entry_loss_pct, D("0.2"))

    def test_stricter_values_and_an_absolute_cap_are_accepted(self):
        envelope = config.build_pilot_envelope({
            "RADAR_PILOT_EQUITY_EUR": "100.50", "RADAR_PILOT_PER_ENTRY_LOSS_PCT": "0.1",
            "RADAR_PILOT_AGGREGATE_LOSS_PCT": "0.4", "RADAR_PILOT_GROSS_NOTIONAL_PCT": "5",
            "RADAR_PILOT_CASH_BUFFER_PCT": "20", "RADAR_PILOT_DAILY_LOSS_PCT": "0.5",
            "RADAR_PILOT_DRAWDOWN_PCT": "2", "RADAR_PILOT_PER_ENTRY_CAP_EUR": "0.30",
        })
        self.assertEqual(
            (envelope.equity, envelope.per_entry_loss_pct, envelope.aggregate_loss_pct, envelope.gross_notional_pct,
             envelope.cash_buffer_pct, envelope.daily_loss_pct, envelope.drawdown_pct, envelope.per_entry_abs_cap),
            (D("100.50"), D("0.1"), D("0.4"), D("5"), D("20"), D("0.5"), D("2"), D("0.30")),
        )
        self.assertIsNone(config.build_pilot_envelope({"RADAR_PILOT_PER_ENTRY_CAP_EUR": "  "}).per_entry_abs_cap)

    def test_a_value_looser_than_ex1_names_its_variable_and_refuses(self):
        for name, value in (
            ("RADAR_PILOT_PER_ENTRY_LOSS_PCT", "0.26"), ("RADAR_PILOT_AGGREGATE_LOSS_PCT", "0.51"),
            ("RADAR_PILOT_GROSS_NOTIONAL_PCT", "10.01"), ("RADAR_PILOT_CASH_BUFFER_PCT", "9.99"),
            ("RADAR_PILOT_DAILY_LOSS_PCT", "1.5"), ("RADAR_PILOT_DRAWDOWN_PCT", "3.1"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(risk.EnvelopeError) as caught:
                    config.build_pilot_envelope({name: value})
                self.assertEqual(caught.exception.field, name)

    def test_malformed_values_refuse_instead_of_falling_back(self):
        for name, value in (
            ("RADAR_PILOT_EQUITY_EUR", "240.005"), ("RADAR_PILOT_EQUITY_EUR", "0"), ("RADAR_PILOT_EQUITY_EUR", "abc"),
            ("RADAR_PILOT_EQUITY_EUR", "-240"), ("RADAR_PILOT_PER_ENTRY_LOSS_PCT", "NaN"),
            ("RADAR_PILOT_PER_ENTRY_LOSS_PCT", "0"), ("RADAR_PILOT_DRAWDOWN_PCT", "-1"),
            ("RADAR_PILOT_DAILY_LOSS_PCT", "Infinity"), ("RADAR_PILOT_CASH_BUFFER_PCT", "100"),
            ("RADAR_PILOT_PER_ENTRY_CAP_EUR", "0.001"), ("RADAR_PILOT_PER_ENTRY_CAP_EUR", "x"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError):
                    config.build_pilot_envelope({name: value})


class TestImportTime(unittest.TestCase):
    def _read(self, extra_env: dict[str, str]) -> list[object]:
        completed = _child(extra_env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_defaults_and_the_flag(self):
        self.assertEqual(self._read({}), [True, "240.00", "EUR", 1, "0.25", "3"])
        for value in ("0", "false", "False"):
            with self.subTest(value=value):
                self.assertFalse(self._read({"RADAR_PILOT_ENABLED": value})[0])
        self.assertTrue(self._read({"RADAR_PILOT_ENABLED": "yes"})[0])

    def test_a_looser_value_refuses_to_load(self):
        completed = _child({"RADAR_PILOT_DRAWDOWN_PCT": "5"})
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("RADAR_PILOT_DRAWDOWN_PCT", completed.stderr)
        self.assertIn("looser than the EX-1 ceiling", completed.stderr)


if __name__ == "__main__":
    unittest.main()
