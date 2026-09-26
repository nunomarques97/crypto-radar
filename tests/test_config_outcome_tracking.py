"""RADAR_OUTCOME_TRACKING_ENABLED: default on, "0"/"false"/"False" turn it off.

These tests prove the switch's three cases only.

Each case imports radar_v08.config in a fresh child process (same idiom as
tests/test_qwen_profile_runtime.py's run_child): config.py is a module many
other test modules already hold a live reference to, and reloading it
in-process replaces its classes with new objects that compare unequal to
instances other already-imported modules built from the pre-reload classes
(a plain assignment would also skip proving the real os.getenv parsing).
A subprocess avoids all of that cross-test contamination.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _read_flag(extra_env: dict[str, str] | None = None) -> bool:
    with tempfile.TemporaryDirectory(prefix="t2-outcome-tracking-state-") as state_dir:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("RADAR_")}
        environment["RADAR_STATE_DIR"] = state_dir
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.update(extra_env or {})
        completed = subprocess.run(
            [sys.executable, "-c", "import json; from radar_v08 import config; "
                                    "print(json.dumps(config.RADAR_OUTCOME_TRACKING_ENABLED))"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
    if completed.returncode != 0:
        raise AssertionError(f"child failed: {completed.stderr}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


class TestOutcomeTrackingEnabledFlag(unittest.TestCase):
    def test_default_is_true_when_unset(self):
        self.assertTrue(_read_flag())

    def test_zero_string_disables(self):
        self.assertFalse(_read_flag({"RADAR_OUTCOME_TRACKING_ENABLED": "0"}))

    def test_lowercase_false_string_disables(self):
        self.assertFalse(_read_flag({"RADAR_OUTCOME_TRACKING_ENABLED": "false"}))

    def test_capitalized_False_string_disables(self):
        self.assertFalse(_read_flag({"RADAR_OUTCOME_TRACKING_ENABLED": "False"}))

    def test_other_value_leaves_it_enabled(self):
        self.assertTrue(_read_flag({"RADAR_OUTCOME_TRACKING_ENABLED": "yes"}))


if __name__ == "__main__":
    unittest.main()
