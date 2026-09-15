"""Runs the Node-based regression tests for the TEST MODE toggle's pure
logic (ui/web/test_mode.js) as part of the normal `python -m unittest
discover` run, so this repo keeps a single command that proves everything.

The toggle itself is UI-only JavaScript (no Python involved - it never
changes mock_alert, notifications, or event persistence), so its logic is
tested in Node directly (tests/ui_tests/js/test_test_mode.mjs) rather than
reimplemented or faked in Python. This wrapper just shells out to `node
--test` and surfaces a clear pass/fail plus full output on failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JS_TEST_PATH = os.path.join(REPO_ROOT, "tests", "ui_tests", "js", "test_test_mode.mjs")


class TestModeJsTestCase(unittest.TestCase):
    def test_test_mode_pure_logic_suite_passes(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not on PATH - install Node.js to run the JS TEST MODE suite")

        result = subprocess.run(
            [node, "--test", JS_TEST_PATH],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("fail 0", result.stdout)


if __name__ == "__main__":
    unittest.main()
