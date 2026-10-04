"""Guard: the desktop UI source has no Portuguese left (DESIGN.md: all UI copy in English).

Every visible string is a literal in these files, so a static scan of the source covers the
UI without a browser. It looks for Portuguese accented letters, a list of Portuguese UI words
and the Game's old agent names. Symbols the UI uses (euro sign, dashes, arrows, check marks,
middle dot, ellipsis, emoji) are not Portuguese and stay allowed. No network, no file writes
outside a temporary directory.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The scanned files, relative to the repository root.
SCANNED_GLOBS = ("ui/web/*.html", "ui/web/*.js", "ui/web/*.css", "ui/*.py")
SCANNED_FILES = ("scripts/paper_game_preview.py",)

PORTUGUESE_LETTERS = "ãõçáéíóúâêôàÃÕÇÁÉÍÓÚÂÊÔÀ"
ACCENTED = re.compile(f"[{PORTUGUESE_LETTERS}]")

#: Portuguese UI words, matched case-insensitively as whole words (an identifier such as
#: ``sem_x`` or a longer English word never matches).
PORTUGUESE_WORDS = (
    "Estado", "Agentes", "Alertas", "Historico", "Sistema", "Hora", "Horas", "Sem", "ainda",
    "COPIAR", "COPIADO", "FALHOU", "TESTAR", "TESTE", "ABRIR", "Jogo", "Ultimo", "Proximo",
    "evento", "processados", "atividade", "registados", "linhas", "nenhum", "nao", "COMUNICACAO",
)
#: Portuguese phrases, matched case-insensitively (accented spellings are caught by ACCENTED too).
PORTUGUESE_PHRASES = ("dados de exemplo", "pre-visualizacao", "pré-visualização", "Histórico")
#: The Game's agent names before the rename to Scout, Analyst, Strategist, Boss and Treasurer.
OLD_AGENT_NAMES = ("Olheiro", "Analista", "Estratega", "Chefe", "Tesoureiro")

WORDS = re.compile(r"\b(?:" + "|".join(map(re.escape, PORTUGUESE_WORDS + OLD_AGENT_NAMES)) + r")\b", re.IGNORECASE)
PHRASES = re.compile("|".join(map(re.escape, PORTUGUESE_PHRASES)), re.IGNORECASE)

#: Non-Portuguese symbols the UI shows; they must never trip the guard.
ALLOWED_SYMBOLS = "€ — – ▲ ▼ ✓ · … → ← 🧪 ✔ ✖"


def find_portuguese(text: str) -> list[tuple[int, str]]:
    """(line number, matched text) for every Portuguese letter, word or phrase in ``text``."""
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        for pattern in (ACCENTED, WORDS, PHRASES):
            found.extend((number, match.group(0)) for match in pattern.finditer(line))
    return found


def scanned_paths(root: Path) -> list[Path]:
    paths = [path for pattern in SCANNED_GLOBS for path in sorted(root.glob(pattern))]
    return paths + [root / name for name in SCANNED_FILES]


def scan(root: Path) -> dict[str, list[tuple[int, str]]]:
    """Every scanned file under ``root`` that contains Portuguese, with its findings."""
    report: dict[str, list[tuple[int, str]]] = {}
    for path in scanned_paths(root):
        found = find_portuguese(path.read_text(encoding="utf-8"))
        if found:
            report[path.relative_to(root).as_posix()] = found
    return report


class TestUiIsEnglish(unittest.TestCase):
    def test_the_scan_covers_the_ui_files(self):
        names = {path.relative_to(ROOT).as_posix() for path in scanned_paths(ROOT)}
        for expected in (
            "ui/web/index.html", "ui/web/app.js", "ui/web/style.css", "ui/web/agent_room.js",
            "ui/web/test_mode.js", "ui/web/paper_game.js", "ui/web/paper_office.js",
            "ui/web/paper_office_engine.js", "ui/web/trend_paper.js", "ui/bridge.py", "ui/agents.py", "ui/data_reader.py",
            "ui/process_manager.py", "ui/paper_texts.py", "ui/paper_reader.py",
            "scripts/paper_game_preview.py",
        ):
            self.assertIn(expected, names)
        for path in scanned_paths(ROOT):
            self.assertTrue(path.is_file(), path)

    def test_no_portuguese_is_left_in_the_ui_source(self):
        report = scan(ROOT)
        self.assertEqual(report, {}, "Portuguese found in the UI source: " + repr(report))

    def test_index_html_is_declared_english(self):
        html = (ROOT / "ui/web/index.html").read_text(encoding="utf-8")
        self.assertRegex(html, r'<html lang="en">')
        self.assertNotRegex(html, r'lang="pt')


class TestTheGuardItself(unittest.TestCase):
    def test_symbols_the_ui_uses_are_allowed(self):
        self.assertEqual(find_portuguese(ALLOWED_SYMBOLS), [])
        self.assertEqual(find_portuguese("Pretend money: €1,000.00 — up 0.55% ▲ · COPIED ✓ …"), [])

    def test_english_words_that_contain_a_listed_word_are_not_flagged(self):
        sample = "Semantic system alerts; the historical estate; seminar; sem_tag; Scout, Analyst, Boss"
        self.assertEqual(find_portuguese(sample), [])

    def test_each_kind_of_portuguese_is_flagged(self):
        cases = {
            "Último heartbeat": "Ú",
            "a reação": "ç",
            "<span>Estado <b>": "Estado",
            "Sistema / Logs": "Sistema",
            "Sem alertas reais": "Sem",
            "(sem linhas ainda)": "sem",
            "COPIAR PROMPT": "COPIAR",
            "TESTAR NTFY": "TESTAR",
            "ABRIR LOG": "ABRIR",
            "Historico": "Historico",
            "<th>Hora</th>": "Hora",
            "Sample data — preview · dados de exemplo": "dados de exemplo",
            "pre-visualizacao": "pre-visualizacao",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIn(expected, [match for _, match in find_portuguese(text)])

    def test_the_old_agent_names_are_flagged(self):
        for name in OLD_AGENT_NAMES:
            for spelling in (name, name.lower(), name.upper()):
                with self.subTest(spelling=spelling):
                    self.assertTrue(find_portuguese(f'id: "{spelling}"'))

    def test_an_injected_portuguese_string_makes_the_scan_fail(self):
        # Run the real scan on a temporary copy; the real files are never edited.
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp)
            for path in scanned_paths(ROOT):
                target = copy / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            self.assertEqual(scan(copy), {}, "the clean copy must pass first")

            index = copy / "ui/web/index.html"
            index.write_text(
                index.read_text(encoding="utf-8").replace("<h3>System</h3>", "<h3>Sistema</h3>", 1),
                encoding="utf-8",
            )
            app = copy / "ui/web/app.js"
            app.write_text(app.read_text(encoding="utf-8") + '\n// "Sem alertas até ao momento"\n', encoding="utf-8")

            report = scan(copy)
            self.assertEqual(sorted(report), ["ui/web/app.js", "ui/web/index.html"])
            self.assertIn("Sistema", [match for _, match in report["ui/web/index.html"]])
            self.assertIn("é", [match for _, match in report["ui/web/app.js"]])
            with self.assertRaises(AssertionError):
                self.assertEqual(report, {})


if __name__ == "__main__":
    unittest.main()
