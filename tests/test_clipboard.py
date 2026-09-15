import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08 import clipboard


def _fake_windll(*, alloc_ok=True, lock_ok=True, open_ok=True, set_ok=True):
    windll = mock.Mock()
    kernel32 = windll.kernel32
    user32 = windll.user32

    kernel32.GlobalAlloc.return_value = 12345 if alloc_ok else 0
    kernel32.GlobalLock.return_value = 6789 if lock_ok else 0
    user32.OpenClipboard.return_value = 1 if open_ok else 0
    user32.SetClipboardData.return_value = 1 if set_ok else 0
    return windll


class TestCopyTextToClipboard(unittest.TestCase):
    def test_success_path(self):
        windll = _fake_windll()
        with mock.patch("ctypes.memmove"):
            result = clipboard.copy_text_to_clipboard("hello", windll_factory=lambda: windll)
        self.assertTrue(result)
        windll.user32.OpenClipboard.assert_called_once()
        windll.user32.EmptyClipboard.assert_called_once()
        windll.user32.SetClipboardData.assert_called_once()
        windll.user32.CloseClipboard.assert_called_once()
        windll.kernel32.GlobalFree.assert_not_called()  # ownership transferred to the OS

    def test_alloc_failure_returns_false(self):
        windll = _fake_windll(alloc_ok=False)
        result = clipboard.copy_text_to_clipboard("hello", windll_factory=lambda: windll)
        self.assertFalse(result)
        windll.user32.OpenClipboard.assert_not_called()

    def test_lock_failure_frees_memory_and_returns_false(self):
        windll = _fake_windll(lock_ok=False)
        result = clipboard.copy_text_to_clipboard("hello", windll_factory=lambda: windll)
        self.assertFalse(result)
        windll.kernel32.GlobalFree.assert_called_once()

    def test_open_clipboard_failure_returns_false(self):
        windll = _fake_windll(open_ok=False)
        with mock.patch("ctypes.memmove"):
            result = clipboard.copy_text_to_clipboard("hello", windll_factory=lambda: windll)
        self.assertFalse(result)
        windll.kernel32.GlobalFree.assert_called_once()

    def test_set_clipboard_data_failure_returns_false(self):
        windll = _fake_windll(set_ok=False)
        with mock.patch("ctypes.memmove"):
            result = clipboard.copy_text_to_clipboard("hello", windll_factory=lambda: windll)
        self.assertFalse(result)
        windll.user32.CloseClipboard.assert_called_once()  # still closed even on failure

    def test_exception_in_backend_never_raises(self):
        def broken_factory():
            raise AttributeError("no windll on this platform")

        try:
            result = clipboard.copy_text_to_clipboard("hello", windll_factory=broken_factory)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"copy_text_to_clipboard raised {exc!r} instead of returning False")
        self.assertFalse(result)

    def test_unicode_text_is_utf16_encoded(self):
        windll = _fake_windll()
        captured = {}

        def fake_memmove(dst, src, count):
            captured["src"] = bytes(src)

        with mock.patch("ctypes.memmove", side_effect=fake_memmove):
            clipboard.copy_text_to_clipboard("PEPE \U0001F680 ção", windll_factory=lambda: windll)
        expected = "PEPE \U0001F680 ção".encode("utf-16-le") + b"\x00\x00"
        self.assertEqual(captured["src"], expected)


if __name__ == "__main__":
    unittest.main()
