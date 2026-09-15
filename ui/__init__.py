"""Local desktop control room for the Crypto Radar backend (radar_v08).

Observability + process control only: this package never reimplements a
radar_v08 mechanism (events, alerts, prompts, notifications, budgets) - it
only reads radar_v08's own store/config/output and starts/stops radar.py as
a subprocess. See DESIGN.md for the visual system and
<home>\\.claude\\plans\\deep-jingling-avalanche.md for the plan this
package implements.

`radar_v08/config.py` derives its default STATE_DIR from its own `__file__`
(the directory one level above radar_v08/). That is correct when running
from source, but when this UI is frozen with PyInstaller, `config.py`'s
`__file__` resolves inside PyInstaller's bundled/extracted temp directory,
not the real project folder - so radar_v08 would silently open/create an
empty SQLite file there instead of the real `radar_state.sqlite`. Setting
RADAR_STATE_DIR here, before radar_v08.config is imported by any submodule,
is the one correct fix: config.py already honors this env var as an
override, so no radar_v08 code needs to change.

`notifications.copy_prompt_for_event` also opens an auxiliary "COPIAR
PROMPT" popup window (radar_v08/prompt_popup.py) as a fallback for headless
CLI usage, spawned via `sys.executable`. In a source run that resolves to a
real `python.exe`/`pythonw.exe`, so the popup is a small Tk window. In this
frozen .exe, `sys.executable` IS the CryptoRadarControlRoom.exe itself - so
that spawn launches a second full instance of this same UI (its entrypoint
starts the Control Room unconditionally, ignoring argv), which looks like a
duplicate Control Room window. The Control Room already has its own COPIAR
PROMPT affordance for every alert/mock row, so this popup is redundant here
regardless of frozen/source - disabling it via the existing
RADAR_COPY_PROMPT_POPUP_ENABLED override (same mechanism as RADAR_STATE_DIR
above) removes the duplicate-window risk without touching radar_v08 or
affecting the CLI/real loop, which never sets this env var.
"""

import os

if not os.environ.get("RADAR_STATE_DIR"):
    from ui import paths as _paths
    os.environ["RADAR_STATE_DIR"] = _paths.repo_root()

os.environ.setdefault("RADAR_COPY_PROMPT_POPUP_ENABLED", "0")
