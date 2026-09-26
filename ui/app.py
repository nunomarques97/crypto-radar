"""Builds and starts the pywebview window. Kept separate from `bridge.py`
(the Api surface) and `__main__.py` (the CLI entry point) so each piece is
independently importable/testable.
"""

from __future__ import annotations

import os

import webview

from ui import paths, ui_state
from ui.bridge import Api


def _index_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")


def run() -> None:
    paths.ensure_importable()
    state = ui_state.load()
    api = Api()

    window = webview.create_window(
        "Crypto Radar — Control Room",
        _index_path(),
        js_api=api,
        width=state["window"]["width"],
        height=state["window"]["height"],
        x=state["window"].get("x"),
        y=state["window"].get("y"),
        background_color="#0B0E13",
        min_size=(1024, 720),
    )

    if window is None:
        api.close()
        return

    def _on_closed():
        try:
            api.close()
        except Exception:  # noqa: BLE001 - shutdown must never raise into pywebview
            pass

    window.events.closed += _on_closed
    webview.start()


if __name__ == "__main__":
    run()
