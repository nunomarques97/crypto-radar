import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ui import paths


class TestRepoRoot(unittest.TestCase):
    def test_unfrozen_resolves_to_actual_repo_root(self):
        # ui/paths.py lives at <repo_root>/ui/paths.py - the real repo root
        # must contain radar.py.
        with patch.object(sys, "frozen", False, create=True):
            root = paths.repo_root()
        self.assertTrue(os.path.isfile(os.path.join(root, "radar.py")))

    def test_frozen_walks_up_from_executable_dir_to_find_radar_py(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Simulate dist/CryptoRadarControlRoom/CryptoRadar.exe two levels
            # under a project root that has radar.py.
            project_root = os.path.join(tmp, "crypto-radar")
            dist_dir = os.path.join(project_root, "dist", "CryptoRadarControlRoom")
            os.makedirs(dist_dir)
            open(os.path.join(project_root, "radar.py"), "w").close()
            fake_exe = os.path.join(dist_dir, "CryptoRadar.exe")

            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", fake_exe):
                root = paths.repo_root()
            self.assertEqual(os.path.normcase(root), os.path.normcase(project_root))

    def test_frozen_falls_back_when_radar_py_not_found_within_walk_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            deeply_nested = os.path.join(tmp, "a", "b", "c", "d", "e", "f")
            os.makedirs(deeply_nested)
            fake_exe = os.path.join(deeply_nested, "CryptoRadar.exe")
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", fake_exe):
                root = paths.repo_root()
            # No exception, falls back to the executable's own directory.
            self.assertEqual(os.path.normcase(root), os.path.normcase(deeply_nested))

    def test_ensure_importable_adds_repo_root_to_sys_path(self):
        root = paths.repo_root()
        with patch.object(sys, "path", list(sys.path)):
            if root in sys.path:
                sys.path.remove(root)
            paths.ensure_importable()
            self.assertIn(root, sys.path)

    def test_ui_state_dir_lives_under_ui_never_touches_sqlite(self):
        state_dir = paths.ui_state_dir()
        self.assertTrue(os.path.isdir(state_dir))
        self.assertIn("ui", os.path.normpath(state_dir).split(os.sep))
        self.assertNotIn("radar_state.sqlite", state_dir)


if __name__ == "__main__":
    unittest.main()
