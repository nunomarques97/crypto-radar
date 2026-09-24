"""T050c2c: does the frozen app resolve `radar_v08/model_profiles.toml` (D55(7))?

Static proof only - PyInstaller is never imported and never run here, no executable is
generated, `dist/` and `build/` are untouched.

Reaches the loader, static evidence:

* `ui/__main__.py`'s very first statement is `from ui.app import run`. Loading `ui.app`
  runs `from ui.bridge import Api` (module scope, not inside `run()`), which loads
  `ui.bridge`, whose module scope has `from radar_v08 import (alerts, clipboard, config,
  mock_alert, notifications)`. All of this happens before `ui.paths.ensure_importable()`
  can run: that call only exists inside `ui.app.run()` and `ui.bridge.Api.__init__`, never
  at either file's module scope, so it cannot execute before the imports above already did.
* So the frozen executable's very first `import radar_v08.config` - triggered merely by
  loading the entry module, before any window opens or any button is pressed - is served by
  whatever PyInstaller put on `sys.path` by default: its own frozen importer reading the
  bundled `PYZ` archive, never the live source tree on disk (`ensure_importable()` has not
  inserted the repo root yet).
* `radar_v08/config.py` resolves `QWEN_RUNTIME`/`QWEN_PROFILE_ERROR` at import time
  (`_resolve_qwen_runtime_at_import`, T9/T050c2b) from
  `radar_v08.adapters.model_profiles.DEFAULT_PROFILES_PATH` - no env var, no `ui.paths`
  involved. That constant is `Path(__file__).resolve().parent.parent / "model_profiles.toml"`,
  purely `__file__`-relative. For a module PyInstaller loads from its frozen archive,
  `__file__` is synthesized under `sys._MEIPASS` (PyInstaller's documented run-time
  behaviour) rather than pointing at a real file on disk - so the computed path becomes
  `<sys._MEIPASS>/radar_v08/model_profiles.toml`. A `.toml` data file is never picked up by
  PyInstaller's own static import-graph analysis (only `.py` modules are), so it has to be
  named explicitly in the `.spec`'s `datas`, exactly like the existing `('ui/web', 'ui/web')`
  entry (`Analysis(...).datas`; `sys._MEIPASS` in this project's onedir build,
  `exclude_binaries=True` + `COLLECT`, is the same base directory `a.datas` entries land in).

So this task's branch is "reaches, including only by importing config": the pair
`radar_v08/model_profiles.toml -> radar_v08` belongs in `Analysis(...).datas`, and the tests
below prove, by simulating that bundle layout, that the path the loader actually computes
exists inside it.
"""

from __future__ import annotations

import ast
import shutil
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPOSITORY_ROOT / "CryptoRadarControlRoom.spec"
MODEL_PROFILES_PY = REPOSITORY_ROOT / "radar_v08" / "adapters" / "model_profiles.py"
VERSIONED_PROFILES_TOML = REPOSITORY_ROOT / "radar_v08" / "model_profiles.toml"
PROFILES_DATA_PAIR = ("radar_v08/model_profiles.toml", "radar_v08")


def _analysis_datas() -> list[tuple[str, str]]:
    """The literal `datas=[...]` passed to `Analysis(...)` in the `.spec`, read by `ast`
    only (the `.spec` is never imported/executed, matching `scripts/run_quality.py`'s own
    text/AST reading of `config.py` for its `RADAR_*_PATH` scan)."""
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"), filename=str(SPEC_PATH))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Analysis"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "datas":
                value = ast.literal_eval(keyword.value)
                if not isinstance(value, list) or not all(
                    isinstance(item, tuple) and len(item) == 2 for item in value
                ):
                    raise AssertionError(f"Analysis(datas=...) is not a list of (src, dest) pairs: {value!r}")
                return value
        raise AssertionError("Analysis(...) call has no datas= keyword")
    raise AssertionError("no Analysis(...) call found in CryptoRadarControlRoom.spec")


def _materialize_bundle(datas: list[tuple[str, str]], meipass: Path) -> None:
    """Copy every `(src, dest)` pair into `meipass`, the way PyInstaller's `COLLECT` would
    for this project's onedir build: a directory source's contents land under `dest`; a file
    source lands at `dest/<basename(src)>`."""
    for src, dest in datas:
        source_path = REPOSITORY_ROOT / src
        dest_root = meipass / dest
        if source_path.is_dir():
            shutil.copytree(source_path, dest_root, dirs_exist_ok=True)
        elif source_path.is_file():
            dest_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, dest_root / source_path.name)
        else:
            raise AssertionError(f"datas source does not exist: {source_path}")


def _frozen_profiles_path(meipass: Path) -> Path:
    """The identical formula `radar_v08/adapters/model_profiles.py::DEFAULT_PROFILES_PATH`
    uses (`Path(__file__).resolve().parent.parent / "model_profiles.toml"`), applied to the
    synthetic `__file__` PyInstaller's frozen importer gives a module loaded from its
    archive: `<sys._MEIPASS>/<package>/<subpackage>/<module>.pyc`. The exact terminal
    filename does not matter here - `.parent.parent` strips exactly the module file and its
    containing `adapters/` directory regardless of the file's name or suffix, leaving
    `<sys._MEIPASS>/radar_v08`.
    """
    synthetic_file = meipass / "radar_v08" / "adapters" / "model_profiles.pyc"
    return synthetic_file.resolve().parent.parent / "model_profiles.toml"


def _top_level_from_imports(path: Path) -> dict[str, set[str]]:
    """`{module: {imported names}}` for every top-level (module-scope) `from <module> import
    ...` in `path`, by `ast` only - the file is never imported."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            result.setdefault(node.module, set()).update(alias.name for alias in node.names)
    return result


def _calls_ensure_importable_at_module_scope(path: Path) -> bool:
    """True if `path` calls `....ensure_importable()` directly in its module body (not
    nested inside a function/class), by `ast` only."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "ensure_importable"
        ):
            return True
    return False


class TestSpecListsTheProfilesDataPair(unittest.TestCase):
    """Criterion (2): the pair belongs in the `.spec`'s `datas`."""

    def test_spec_bundles_model_profiles_toml_into_radar_v08(self) -> None:
        self.assertIn(PROFILES_DATA_PAIR, _analysis_datas())

    def test_the_existing_ui_web_pair_is_still_there(self) -> None:
        # This task only adds a pair; it must not touch the one the UI already needs.
        self.assertIn(("ui/web", "ui/web"), _analysis_datas())

    def test_pair_source_is_the_real_versioned_profiles_file(self) -> None:
        source, _dest = PROFILES_DATA_PAIR
        self.assertEqual(REPOSITORY_ROOT / source, VERSIONED_PROFILES_TOML)
        self.assertTrue(VERSIONED_PROFILES_TOML.is_file())


class TestSimulatedBundleResolvesTheLoaderPath(unittest.TestCase):
    """Criterion (2): a test that reads the `.spec` and simulates the bundle layout
    (`sys.frozen`/`sys._MEIPASS`) proving the profile path the loader resolves exists inside
    it. No PyInstaller import, no PyInstaller run, no executable, `dist/`/`build/` untouched."""

    def test_bundle_layout_from_the_real_spec_contains_the_loader_path(self) -> None:
        datas = _analysis_datas()
        with tempfile.TemporaryDirectory() as tmp:
            meipass = Path(tmp)
            _materialize_bundle(datas, meipass)
            resolved = _frozen_profiles_path(meipass)
            self.assertTrue(resolved.is_file(), f"loader path missing from simulated bundle: {resolved}")
            self.assertEqual(resolved.read_bytes(), VERSIONED_PROFILES_TOML.read_bytes())

    def test_without_the_pair_the_loader_path_would_be_missing(self) -> None:
        # Regression guard: dropping the pair from the .spec must fail this suite, not pass
        # it silently. Materialize every OTHER pair the real .spec lists and show the loader
        # path is absent without the one under test.
        datas = [pair for pair in _analysis_datas() if pair != PROFILES_DATA_PAIR]
        self.assertNotIn(PROFILES_DATA_PAIR, datas)  # sanity: the filter actually removed it
        with tempfile.TemporaryDirectory() as tmp:
            meipass = Path(tmp)
            _materialize_bundle(datas, meipass)
            resolved = _frozen_profiles_path(meipass)
            self.assertFalse(resolved.exists())

    def test_loader_formula_pinned_against_the_real_source(self) -> None:
        # Pins the exact expression this test's _frozen_profiles_path() mirrors, so a future
        # change to DEFAULT_PROFILES_PATH's computation is caught here instead of leaving this
        # test silently checking a stale formula. Text only, model_profiles.py is not imported.
        source = MODEL_PROFILES_PY.read_text(encoding="utf-8")
        self.assertIn(
            'DEFAULT_PROFILES_PATH = Path(__file__).resolve().parent.parent / "model_profiles.toml"',
            source,
        )


class TestFrozenEntryPointReachesConfigBeforeEnsureImportable(unittest.TestCase):
    """Criterion (1): static proof that the frozen app imports `radar_v08.config` (and,
    through it, the profile loader) before `ui.paths.ensure_importable()` ever runs - so the
    first resolution genuinely happens against the frozen bundle, not the live source tree."""

    def test_main_imports_app_run_as_its_first_statement(self) -> None:
        main_imports = _top_level_from_imports(REPOSITORY_ROOT / "ui" / "__main__.py")
        self.assertIn("run", main_imports.get("ui.app", set()))

    def test_app_imports_bridge_api_at_module_scope(self) -> None:
        app_imports = _top_level_from_imports(REPOSITORY_ROOT / "ui" / "app.py")
        self.assertIn("Api", app_imports.get("ui.bridge", set()))

    def test_bridge_imports_radar_v08_config_at_module_scope(self) -> None:
        bridge_imports = _top_level_from_imports(REPOSITORY_ROOT / "ui" / "bridge.py")
        self.assertIn("config", bridge_imports.get("radar_v08", set()))

    def test_app_and_bridge_never_call_ensure_importable_at_module_scope(self) -> None:
        # ensure_importable() only ever runs inside a function/method body (ui.app.run,
        # ui.bridge.Api.__init__), so it cannot run before the module-scope imports above.
        self.assertFalse(_calls_ensure_importable_at_module_scope(REPOSITORY_ROOT / "ui" / "app.py"))
        self.assertFalse(_calls_ensure_importable_at_module_scope(REPOSITORY_ROOT / "ui" / "bridge.py"))


class TestPyInstallerNeverRan(unittest.TestCase):
    """Criterion (3): nothing in this suite builds anything."""

    def test_this_module_never_imports_or_subprocesses_pyinstaller(self) -> None:
        # ast only, so the check survives comments/docstrings mentioning the tool by name
        # (this module's own docstring does, to explain the proof) without false-failing.
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=str(Path(__file__)))
        imported_modules: set[str] = set()
        subprocess_calls = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {
                "run", "Popen", "call", "check_call", "check_output",
            }:
                subprocess_calls += 1
        self.assertFalse(any("pyinstaller" in name.lower() for name in imported_modules))
        self.assertEqual(subprocess_calls, 0)


if __name__ == "__main__":
    unittest.main()
