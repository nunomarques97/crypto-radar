"""Auxiliary "COPIAR PROMPT" window - the toast-action fallback.

The existing Windows toast (notifications.py) rides PowerShell's own
registered AUMID with no activation handler registered (no COM server, no
protocol registration) - so a real clickable toast button cannot reliably
call back into this radar without a materially bigger, riskier change
(registering a URI-protocol handler or a COM activator in the registry) than
this feature's single stated goal. This module implements the fallback the
task itself proposes instead: a small, always-reliable auxiliary window with
one COPIAR PROMPT button.

Note the radar's own process already copies the prompt to the clipboard the
moment the event is processed (see notifications.copy_prompt_for_event) - this
window is a visual anchor plus a way to copy again if the clipboard was
overwritten by something else before the user got to paste it. It is spawned
as a short-lived, detached subprocess so it never blocks the radar's loop,
and it never replaces the normal Windows toast.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import uuid

from . import config

logger = logging.getLogger("radar_v08.prompt_popup")

_POPUP_SCRIPT_PATH = os.path.join(config.STATE_DIR, "_radar_copy_prompt_popup.py")

# Self-contained on purpose (own copy of the clipboard write, no `radar_v08`
# import): this runs as a standalone subprocess, so it must not depend on
# package/sys.path resolution to do the one thing that matters (copy text).
_POPUP_SCRIPT = r'''
import ctypes
import sys
import tkinter as tk

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def copy_to_clipboard(text):
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
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
            user32.SetClipboardData(CF_UNICODETEXT, h_global)
        finally:
            user32.CloseClipboard()
        return True
    except Exception:
        return False


def main():
    if len(sys.argv) < 3:
        return
    event_id = sys.argv[1]
    prompt_path = sys.argv[2]
    with open(prompt_path, "r", encoding="utf-8") as fh:
        prompt_text = fh.read()

    root = tk.Tk()
    root.title("CRYPTO RADAR")
    root.attributes("-topmost", True)
    if event_id.startswith("MOCK-"):
        header_text = u"CRYPTO RADAR — MOCK TEST"
    else:
        header_text = "CRYPTO RADAR ALERT - " + event_id
    tk.Label(root, text=header_text, font=("Segoe UI", 10, "bold")).pack(padx=12, pady=(12, 4))
    status = tk.StringVar(value="Prompt ready - click to copy again if needed.")
    tk.Label(root, textvariable=status, wraplength=360, justify="left").pack(padx=12, pady=4)

    def do_copy():
        ok = copy_to_clipboard(prompt_text)
        status.set(u"Prompt copied to clipboard ✅" if ok else "Clipboard copy failed.")

    tk.Button(root, text="COPIAR PROMPT", command=do_copy, width=24).pack(padx=12, pady=8)
    tk.Button(root, text="Fechar", command=root.destroy, width=24).pack(padx=12, pady=(0, 12))
    do_copy()
    root.mainloop()


if __name__ == "__main__":
    main()
'''


def _ensure_script_on_disk() -> str:
    if not os.path.exists(_POPUP_SCRIPT_PATH):
        with open(_POPUP_SCRIPT_PATH, "w", encoding="utf-8") as fh:
            fh.write(_POPUP_SCRIPT)
    return _POPUP_SCRIPT_PATH


def _python_launcher() -> str:
    """Prefers pythonw.exe (no console flash) next to the current
    interpreter, falling back to the current interpreter itself."""
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


def launch_copy_prompt_popup(event_id: str, prompt_text: str, popen_fn=subprocess.Popen) -> bool:
    """Best-effort, non-blocking: never raises, never delays the radar loop.

    `popen_fn` is injectable purely for testing so unit tests never spawn a
    real window; production code should never pass it.
    """
    try:
        script_path = _ensure_script_on_disk()
        fd, prompt_path = tempfile.mkstemp(prefix=f"radar_prompt_{uuid.uuid4().hex[:8]}_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt_text)
        popen_fn(
            [_python_launcher(), script_path, event_id, prompt_path],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - the popup is best-effort, never fatal
        logger.warning("Failed to launch COPIAR PROMPT popup: %s", exc)
        return False
