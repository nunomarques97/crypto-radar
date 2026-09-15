import os
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
        state["last_tab"] = "alertas"
        state["window"]["x"] = 120
        ui_state.save(state, self.tmp_dir)

        reloaded = ui_state.load(self.tmp_dir)
        self.assertEqual(reloaded["last_tab"], "alertas")
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


if __name__ == "__main__":
    unittest.main()
