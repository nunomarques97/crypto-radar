"""Tiny local, UI-only state: window position/size, last active tab. Never
radar domain state - never written into radar_state.sqlite. Lives under
`ui/.state/` (see paths.ui_state_dir()), best-effort (a corrupt/missing file
just means defaults are used, never a crash).
"""

from __future__ import annotations

import json
import os
from typing import Any

from ui import paths

_STATE_FILENAME = "ui_state.json"

DEFAULTS: dict[str, Any] = {
    "window": {"x": None, "y": None, "width": 1440, "height": 960},
    "last_tab": "dashboard",
}


def _state_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or paths.ui_state_dir(), _STATE_FILENAME)


def load(state_dir: str | None = None) -> dict[str, Any]:
    path = _state_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save(state: dict[str, Any], state_dir: str | None = None) -> None:
    path = _state_path(state_dir)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError:
        pass  # best-effort - losing UI convenience state is never fatal
