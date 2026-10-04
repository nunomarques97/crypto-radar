"""Runs the Node-based regression tests for the Game tab's Trend paper panel
(ui/web/trend_paper.js) as part of the normal `python -m unittest discover` run -
same pattern as test_pilot_shadow_js.py, except that a missing Node fails instead of
skipping: the panel's behaviour is only checked there.

It also checks that the honesty label and the pre-tax note fixed in ui/web/index.html
are the reader's own texts (ui/trend_reader.py), so the page always shows them, with
or without a reading, and that no trend paper text (page, panel script, reader, CLI
report, Kraken EUR report, alert toast, operator guide docs/guides/TREND-PAPER.md) implies profit,
qualification, live trading or a real order.
"""

from __future__ import annotations

import ast
import html
import os
import re
import shutil
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JS_TEST_PATH = os.path.join(REPO_ROOT, "tests", "ui_tests", "js", "test_trend_paper.mjs")
INDEX_PATH = os.path.join(REPO_ROOT, "ui", "web", "index.html")

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from radar_v08.domain.trend_paper import (  # noqa: E402
    EMPTY_LEDGER,
    PAPER_START,
    DayGap,
    Gap,
    RegistryStatus,
    Rule,
    SkipReason,
    encode_skip,
    parse_ledger,
    render_report,
)
from radar_v08.domain.trend_paper_kraken import (  # noqa: E402
    EMPTY_KRAKEN_LEDGER,
    KrakenDayGap,
    KrakenSkipReason,
    encode_kraken_skip,
    parse_kraken_ledger,
    render_kraken_report,
)
from radar_v08.trend_paper_alerts import (  # noqa: E402
    ExposureChange,
    SleeveChange,
    format_alert,
)
from ui import trend_reader  # noqa: E402


class TrendPaperJsTestCase(unittest.TestCase):
    def test_trend_paper_suite_passes(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is not on PATH - Node.js is required for the trend paper panel suite")
        result = subprocess.run(
            [node, "--test", JS_TEST_PATH],
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"\bfail 0\b")
        self.assertRegex(result.stdout, r"\bskipped 0\b")
        self.assertNotRegex(result.stdout, r"\btests 0\b")


class TrendPaperPageTestCase(unittest.TestCase):
    def panel(self) -> str:
        with open(INDEX_PATH, encoding="utf-8") as handle:
            html = handle.read()
        start = html.index('id="trend-paper"')
        return html[start:html.index("</section>", start)]

    def test_the_fixed_texts_are_the_readers(self):
        panel = self.panel()
        self.assertIn(trend_reader.HONESTY_LABEL, panel)
        self.assertIn(trend_reader.PRE_TAX_NOTE, panel)

    def test_the_only_control_is_catch_up_now(self):
        buttons = re.findall(r"<button\b[^>]*>(.*?)</button>", self.panel(), flags=re.S)
        self.assertEqual(buttons, ["Catch up now"])


#: Words that would imply profit, a promise or live trading. None may appear in a trend paper text.
PROMISE = re.compile(
    r"\b(?:profit\w*|earn(?:s|ed|ing|ings)?|guarantee\w*|wins?|winning|outperform\w*|beats?|"
    r"live trading|go(?:es)? live|real money)\b",
    re.IGNORECASE,
)
#: Every "qualified" must be negated ("not qualified", "not a qualified").
QUALIFIED = re.compile(r"(\bnot (?:a )?)?\bqualif\w*", re.IGNORECASE)
#: Every "order" must be negated ("no order", "no real orders", "places no order").
ORDER = re.compile(r"(\bno (?:real )?)?\borders?\b", re.IGNORECASE)


def honesty_problems(text: str) -> list[str]:
    """The phrases in ``text`` that imply profit, qualification, live trading or real orders."""
    found = [m.group(0) for m in PROMISE.finditer(text)]
    found += [m.group(0) for m in QUALIFIED.finditer(text) if not m.group(1)]
    found += [m.group(0) for m in ORDER.finditer(text) if not m.group(1)]
    return found


def python_texts(path: str, function: str | None = None) -> list[str]:
    """The string literals of a module (or of one function), docstrings and comments excluded."""
    tree = ast.parse(Path(REPO_ROOT, path).read_text(encoding="utf-8"))
    if function is not None:
        tree = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(node) is not None
    }
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def js_texts(path: str) -> list[str]:
    """The double-quoted string literals of a browser script, comments removed."""
    source = Path(REPO_ROOT, path).read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", source)
    return re.findall(r'"((?:[^"\\\n]|\\.)*)"', source)


class TrendWordingTestCase(unittest.TestCase):
    """No trend paper text, in the page, the panel script, the
    reader, the CLI report or the alert toast, implies profit, qualification, live trading or a
    real order; every place that shows results says they are pre-tax."""

    def corpus(self) -> dict[str, list[str]]:
        panel = TrendPaperPageTestCase().panel()
        page = html.unescape(re.sub(r"<[^>]+>", " ", panel))
        status = RegistryStatus(True, 29, ("btc_trend5",))
        before = datetime(2026, 10, 3, 20, tzinfo=UTC)
        after = datetime(2026, 10, 6, 8, tzinfo=UTC)
        reports = [
            render_report(EMPTY_LEDGER, status, before, None, offline=True),
            render_report(EMPTY_LEDGER, status, after, None, offline=True),
            render_report(EMPTY_LEDGER, RegistryStatus(False, None, (), "x"), after, None, signal_error="x"),
        ]
        gap = DayGap(PAPER_START, SkipReason.NO_FILL_CANDLE, (Gap("BTCUSDT", PAPER_START),))
        skipped = parse_ledger(encode_skip(EMPTY_LEDGER, gap, "2026-10-05T08:00:00+00:00"))
        reports.append(render_report(skipped, status, after, None, offline=True))
        skipped_payload = trend_reader.TrendReader("unused")._payload(skipped, None)
        change = ExposureChange(Rule.ENS, PAPER_START, "USDT", (SleeveChange("BTC", 0.0, 0.703, 61234.0),))
        # The Kraken EUR report, empty, with a skipped day, and with the
        # main ledger refused.
        kraken_gap = KrakenDayGap(PAPER_START, KrakenSkipReason.NO_KRAKEN_OPEN, (Gap("XBTEUR", PAPER_START),))
        kraken_skipped = parse_kraken_ledger(encode_kraken_skip(EMPTY_KRAKEN_LEDGER, kraken_gap, "2026-10-05T08:00:00+00:00"))
        kraken_reports = [
            render_kraken_report(EMPTY_KRAKEN_LEDGER, before, None, binance_note="ledger.jsonl refused (LEDGER_TORN)"),
            render_kraken_report(kraken_skipped, after, skipped),
        ]
        return {
            "ui/trend_reader.py payload with a skipped day": [
                skipped_payload["reason"], *(d["reason"] for d in skipped_payload["skipped_days"])
            ],
            "docs/guides/TREND-PAPER.md": [Path(REPO_ROOT, "docs", "guides", "TREND-PAPER.md").read_text(encoding="utf-8")],
            "ui/web/index.html (trend paper panel)": [page],
            "ui/web/trend_paper.js": js_texts("ui/web/trend_paper.js"),
            "ui/trend_reader.py": python_texts("ui/trend_reader.py"),
            "scripts/run_trend_paper.py": python_texts("scripts/run_trend_paper.py"),
            "radar_v08/domain/trend_paper.py render_report": python_texts(
                "radar_v08/domain/trend_paper.py", "render_report"
            ) + reports,
            "radar_v08/trend_paper_alerts.py": python_texts("radar_v08/trend_paper_alerts.py")
            + list(format_alert(change)),
            "scripts/run_trend_paper_kraken.py": python_texts("scripts/run_trend_paper_kraken.py"),
            "radar_v08/domain/trend_paper_kraken.py": python_texts("radar_v08/domain/trend_paper_kraken.py")
            + kraken_reports,
        }

    def test_no_text_implies_profit_qualification_live_trading_or_real_orders(self):
        corpus = self.corpus()
        # The scan reaches the texts the user actually sees.
        self.assertIn(trend_reader.HONESTY_LABEL, corpus["ui/trend_reader.py"])
        self.assertIn(trend_reader.HONESTY_LABEL, corpus["ui/web/trend_paper.js"])
        self.assertIn("Catch up now", corpus["ui/web/index.html (trend paper panel)"][0])
        self.assertIn(" — no order placed", corpus["radar_v08/trend_paper_alerts.py"][-1])
        # The skipped-days line, the waiting state and the guide.
        js = corpus["ui/web/trend_paper.js"]
        for text in (
            " paper days skipped", " (a public candle is missing for good; no record, the books carry over unchanged)",
            "Waiting for data", "The public candles do not cover every due paper day yet; nothing was written.",
        ):
            self.assertIn(text, js)
        self.assertIn("no BTCUSDT candle on 2026-10-04 (no fill price)",
                      corpus["ui/trend_reader.py payload with a skipped day"])
        self.assertIn("Paper days skipped: 1", corpus["radar_v08/domain/trend_paper.py render_report"][-1])
        self.assertIn("NOT QUALIFIED", corpus["docs/guides/TREND-PAPER.md"][0])
        kraken = corpus["radar_v08/domain/trend_paper_kraken.py"]
        self.assertIn("0.4% maker assumption", kraken)
        self.assertIn("Kraken paper days skipped: 1", kraken[-1])
        self.assertIn("no Kraken XBTEUR candle on 2026-10-04 (no Kraken fill price)", kraken[-1])
        self.assertIn("every difference is n/a", kraken[-2])
        for name, texts in corpus.items():
            self.assertTrue(texts, name)
            with self.subTest(source=name):
                self.assertEqual([p for t in texts for p in honesty_problems(t)], [])

    def test_every_text_that_shows_results_says_pre_tax(self):
        corpus = self.corpus()
        self.assertIn("pre-tax", corpus["ui/web/index.html (trend paper panel)"][0])
        self.assertIn("Results are pre-tax", trend_reader.PRE_TAX_NOTE)
        self.assertIn("pre-tax", corpus["docs/guides/TREND-PAPER.md"][0])
        for report in corpus["radar_v08/domain/trend_paper.py render_report"][-4:]:
            self.assertIn("PAPER ONLY | PRE-TAX | NOT QUALIFIED", report)
            self.assertIn("Results are pre-tax and not qualified", report)
        self.assertIn("paper only — no order placed", corpus["radar_v08/trend_paper_alerts.py"][-1])
        for report in corpus["radar_v08/domain/trend_paper_kraken.py"][-2:]:
            self.assertIn("PAPER ONLY | PRE-TAX | NOT QUALIFIED", report)
            self.assertIn("Results are pre-tax and not qualified", report)

    def test_the_scan_flags_each_kind_of_problem(self):
        for text in (
            "Strategy profit so far", "Guaranteed returns", "ENS beats buy-and-hold", "Qualified strategy",
            "The strategy is qualified", "Orders are sent at the open", "Ready to go live", "live trading",
            "A real order was placed", "It earns 2% a month",
        ):
            with self.subTest(text=text):
                self.assertTrue(honesty_problems(text))
        for text in (
            "Paper only — research, not qualified, no real orders", "not a qualified strategy",
            "no order is sent", "It places no order.", "paper only — no order placed", "NOT QUALIFIED",
            "a polite live region",
        ):
            with self.subTest(text=text):
                self.assertEqual(honesty_problems(text), [])


if __name__ == "__main__":
    unittest.main()
