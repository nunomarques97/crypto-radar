"""Runs the Node-based regression tests for the Agent Control Room's pure
logic (ui/web/agent_room.js) as part of the normal `python -m unittest
discover` run - same pattern as test_test_mode_js.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JS_TEST_PATH = os.path.join(REPO_ROOT, "tests", "ui_tests", "js", "test_agent_room.mjs")


class AgentRoomJsTestCase(unittest.TestCase):
    def test_agent_room_pure_logic_suite_passes(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not on PATH - install Node.js to run the JS Agent Room suite")

        result = subprocess.run(
            [node, "--test", JS_TEST_PATH],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("fail 0", result.stdout)


if __name__ == "__main__":
    unittest.main()
