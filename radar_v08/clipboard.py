"""Windows clipboard writer - stdlib only (ctypes), no new dependency.

Used by the "COPIAR PROMPT" notification extension: puts the full ready-to-
paste analysis prompt for one event directly on the Windows clipboard, so it
never depends on manually selecting text from a terminal/log. Never raises -
a clipboard failure must never take the radar down (same best-effort contract
as notifications.py/ntfy.py).

Uses the raw Win32 clipboard API (GlobalAlloc/SetClipboardData with
CF_UNICODETEXT) rather than a GUI toolkit, so it works from the radar's own
process without creating a window.
"""

from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger("radar_v08.clipboard")

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _default_windll():
    return ctypes.windll  # AttributeError on non-Windows - caught by the caller


def copy_text_to_clipboard(text: str, windll_factory=_default_windll) -> bool:
    """Best-effort: True if `text` is now on the Windows clipboard.

    `windll_factory` is injectable purely for testing (so unit tests never
    touch the real OS clipboard); production code should never pass it.
    """
    try:
        windll = windll_factory()
        kernel32 = windll.kernel32
        user32 = windll.user32
        # ctypes defaults to a 32-bit int return type, which truncates real
        # pointers on 64-bit Windows - handles must be declared as void*.
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p

        data = text.encode("utf-16-le") + b"\x00\x00"
        h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_global:
            return False

        ptr = kernel32.GlobalLock(h_global)
        if not ptr:
            kernel32.GlobalFree(h_global)
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h_global)

        if not user32.OpenClipboard(0):
            kernel32.GlobalFree(h_global)
            return False
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, h_global):
                kernel32.GlobalFree(h_global)
                return False
        finally:
            user32.CloseClipboard()

        return True  # ownership of h_global now belongs to the OS - never free it
    except Exception as exc:  # noqa: BLE001 - clipboard is best-effort, never fatal
        logger.warning("Clipboard write failed: %s", exc)
        return False
