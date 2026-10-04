import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ui import ui_state


class TestUiState(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.tmp_dir):
            os.remove(os.path.join(self.tmp_dir, name))
        os.rmdir(self.tmp_dir)

    def test_load_defaults_when_no_file(self):
        state = ui_state.load(self.tmp_dir)
        self.assertEqual(state["last_tab"], "dashboard")
        self.assertEqual(state["window"]["width"], 1440)

    def test_round_trips_to_its_own_file(self):
        state = ui_state.load(self.tmp_dir)
        state["last_tab"] = "alerts"
        state["window"]["x"] = 120
        ui_state.save(state, self.tmp_dir)

        reloaded = ui_state.load(self.tmp_dir)
        self.assertEqual(reloaded["last_tab"], "alerts")
        self.assertEqual(reloaded["window"]["x"], 120)

    def test_never_writes_to_sqlite_path(self):
        state = ui_state.load(self.tmp_dir)
        ui_state.save(state, self.tmp_dir)
        written_files = os.listdir(self.tmp_dir)
        self.assertNotIn("radar_state.sqlite", written_files)
        self.assertEqual(written_files, ["ui_state.json"])

    def test_corrupt_file_falls_back_to_defaults(self):
        path = os.path.join(self.tmp_dir, "ui_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        state = ui_state.load(self.tmp_dir)
        self.assertEqual(state["last_tab"], "dashboard")

    def test_default_tab_is_one_of_the_english_tab_ids(self):
        index_html = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ui", "web", "index.html")
        with open(index_html, encoding="utf-8") as fh:
            html = fh.read()
        nav_tabs = re.findall(r'class="nav-item[^"]*" data-tab="([^"]+)"', html)
        sections = re.findall(r'<section class="tab-panel" id="tab-([^"]+)"', html)
        self.assertEqual(nav_tabs, ["dashboard", "agents", "alerts", "history", "game", "system"])
        self.assertEqual(sections, nav_tabs)
        self.assertIn(ui_state.load(self.tmp_dir)["last_tab"], nav_tabs)

    def test_a_legacy_saved_tab_is_returned_as_saved(self):
        # The file is never rewritten on load; the UI's resolveTab (test_mode.js)
        # sends an id that is no longer a tab, such as an old Portuguese one, to
        # the dashboard. tests/ui_tests/js/test_test_mode.mjs covers that fallback.
        path = os.path.join(self.tmp_dir, "ui_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"last_tab": "agentes"}')
        self.assertEqual(ui_state.load(self.tmp_dir)["last_tab"], "agentes")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"last_tab": "agentes"}')


if __name__ == "__main__":
    unittest.main()
