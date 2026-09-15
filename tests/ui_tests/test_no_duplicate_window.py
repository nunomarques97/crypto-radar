"""Regression test for the EXECUTAR MOCK ALERT duplicate-window bug.

Root cause: `notifications.copy_prompt_for_event` (called from
`mock_alert.run_mock_alert`, the exact function the Mocks tab's EXECUTAR
MOCK ALERT button invokes via `ui.bridge.Api.run_mock_alert`) opens an
auxiliary "COPIAR PROMPT" popup window by spawning
`[sys.executable, <popup script>, ...]` (radar_v08/prompt_popup.py). In the
frozen .exe, `sys.executable` IS CryptoRadarControlRoom.exe itself, and its
entrypoint starts the Control Room unconditionally regardless of argv - so
that spawn launched a second full Control Room window.

`ui/__init__.py` now sets RADAR_COPY_PROMPT_POPUP_ENABLED=0 (an override
radar_v08/config.py already supported) before radar_v08.config is ever
imported, so the popup - and therefore the extra process - never spawns
while running under this UI, in source or frozen form.

This runs in a fresh subprocess (not the shared test-suite interpreter)
because radar_v08.config bakes the env var into a module-level constant at
import time, and other test modules may have already imported it - a fresh
interpreter is the only way to honestly reproduce "importing `ui` first,
like `python -m ui` / the frozen entrypoint does".
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCRIPT = r"""
import os
import sys
import tempfile

sys.path.insert(0, {repo_root!r})

with tempfile.TemporaryDirectory(prefix="crypto-radar-ui-test-") as state_dir:
    # This child may be run directly, outside scripts/run_tests.py. Clear
    # inherited artifact overrides before ui imports config, then give the
    # real mock-event path a disposable state root.
    for name in list(os.environ):
        if name.startswith("RADAR_"):
            os.environ.pop(name)
    os.environ.pop("CRYPTO_RADAR_NTFY_TOPIC", None)
    os.environ["RADAR_STATE_DIR"] = state_dir
    if {inherited_popup_enabled!r}:
        os.environ["RADAR_COPY_PROMPT_POPUP_ENABLED"] = "1"

    import ui  # noqa: F401 - must run its module-init (RADAR_* env overrides) first, like python -m ui does

    from radar_v08 import config, mock_alert, notifications, ntfy, prompt_popup
    from radar_v08.store import SnapshotStore

    assert config.COPY_PROMPT_POPUP_ENABLED is False, (
        "COPY_PROMPT_POPUP_ENABLED should be disabled once `ui` is imported first"
    )

    # Keep the production mock-alert path, while replacing only delivery
    # boundaries in this child. This avoids desktop toast, ntfy, clipboard,
    # and popup effects without disabling their assertions globally.
    windows_delivery = []
    mobile_delivery = []
    clipboard_writes = []
    spawned = []
    notifications.send_windows_notification = lambda *a, **k: (windows_delivery.append((a, k)), True)[1]
    ntfy.send_ntfy_notification = lambda *a, **k: (mobile_delivery.append((a, k)), "DISABLED")[1]
    notifications.clipboard.copy_text_to_clipboard = lambda text: (clipboard_writes.append(text), True)[1]
    prompt_popup.launch_copy_prompt_popup = lambda *a, **k: (spawned.append((a, k)), True)[1]

    db_path = os.path.join(state_dir, "mock-alert.sqlite")
    config.EVENTS_LOG_PATH = os.path.join(state_dir, "events.jsonl")
    store = SnapshotStore(db_path)
    try:
        mock_alert.run_mock_alert(store)
    finally:
        store.close()

    assert len(windows_delivery) == 1
    assert len(mobile_delivery) == 1
    assert len(clipboard_writes) == 1
    print("SPAWNED_COUNT=%d" % len(spawned))
"""


class NoDuplicateWindowTestCase(unittest.TestCase):
    def test_mock_alert_never_spawns_popup_subprocess_when_run_under_ui(self):
        for inherited_popup_enabled in (False, True):
            with self.subTest(inherited_popup_enabled=inherited_popup_enabled):
                script = _SCRIPT.format(
                    repo_root=REPO_ROOT,
                    inherited_popup_enabled=inherited_popup_enabled,
                )
                with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
                    fh.write(script)
                    script_path = fh.name
                try:
                    result = subprocess.run(
                        [sys.executable, script_path],
                        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
                    )
                finally:
                    os.remove(script_path)

                self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
                self.assertIn("SPAWNED_COUNT=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
