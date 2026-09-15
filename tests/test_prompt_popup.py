import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import config, prompt_popup


class TestPopupScriptOnDisk(unittest.TestCase):
    def setUp(self):
        self._original_state_dir = config.STATE_DIR
        self.tmp_dir = tempfile.mkdtemp()
        config.STATE_DIR = self.tmp_dir
        import importlib
        importlib.reload(prompt_popup)

    def tearDown(self):
        config.STATE_DIR = self._original_state_dir
        import importlib
        importlib.reload(prompt_popup)

    def test_script_is_written_and_contains_the_button_and_clipboard_logic(self):
        path = prompt_popup._ensure_script_on_disk()
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("COPIAR PROMPT", content)
        self.assertIn("SetClipboardData", content)
        self.assertIn("tkinter", content)

    def test_script_is_only_written_once(self):
        path = prompt_popup._ensure_script_on_disk()
        mtime_1 = os.path.getmtime(path)
        prompt_popup._ensure_script_on_disk()
        mtime_2 = os.path.getmtime(path)
        self.assertEqual(mtime_1, mtime_2)


class TestLaunchCopyPromptPopup(unittest.TestCase):
    def setUp(self):
        self._original_state_dir = config.STATE_DIR
        self.tmp_dir = tempfile.mkdtemp()
        config.STATE_DIR = self.tmp_dir
        import importlib
        importlib.reload(prompt_popup)

    def tearDown(self):
        config.STATE_DIR = self._original_state_dir
        import importlib
        importlib.reload(prompt_popup)

    def test_launch_spawns_a_detached_process_with_prompt_in_a_temp_file(self):
        fake_popen = mock.Mock()
        result = prompt_popup.launch_copy_prompt_popup("evt_001", "PROMPT BODY TEXT", popen_fn=fake_popen)
        self.assertTrue(result)
        fake_popen.assert_called_once()
        args = fake_popen.call_args[0][0]
        self.assertEqual(args[2], "evt_001")
        prompt_file_path = args[3]
        with open(prompt_file_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "PROMPT BODY TEXT")

    def test_launch_never_raises_when_popen_fails(self):
        def broken_popen(*args, **kwargs):
            raise OSError("no python launcher found")

        try:
            result = prompt_popup.launch_copy_prompt_popup("evt_002", "text", popen_fn=broken_popen)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"launch_copy_prompt_popup raised {exc!r} instead of returning False")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
