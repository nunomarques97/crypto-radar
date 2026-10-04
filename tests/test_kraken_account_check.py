"""scripts/kraken_account_check.py: the redacted, read-only Kraken account check.

Only fake transports, synthetic fixtures and temporary state directories: no socket, no
real credential, no real nonce file and no radar state file. The key and secret below are
synthetic values made for these tests.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPO_DIR = TESTS_DIR.parent
SCRIPT = REPO_DIR / "scripts" / "kraken_account_check.py"
ADAPTER_MODULE = "radar_v08.adapters.kraken_private_read"
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from radar_v08.adapters.kraken_private_read import (  # noqa: E402
    TransportFailure,
    TransportFailureKind,
    TransportReply,
)


def _load():
    spec = importlib.util.spec_from_file_location("kraken_account_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


account_check = _load()

# Synthetic credentials (never a real key).
SYNTHETIC_KEY = "SyntheticCheckKeyQQQQrrrrSSSStttt9876543210+/="
SYNTHETIC_SECRET_BYTES = bytes(range(101, 165))
SYNTHETIC_SECRET = base64.b64encode(SYNTHETIC_SECRET_BYTES).decode("ascii")
ENV = {"KRAKEN_API_KEY": SYNTHETIC_KEY, "KRAKEN_API_SECRET": SYNTHETIC_SECRET}

T0 = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)

USERREF = 918273645
CL_ORD_ID = "synthetic-client-7f3a"
FULL_TXIDS = ("OQSYNT-KKKKK-LLLLLL", "OZSYNT-MMMMM-NNNNNN")


def _order(pair: str, side: str, order_type: str, price: str, volume: str, executed: str, **extra: object) -> dict:
    return {
        "refid": None,
        "userref": USERREF,
        "status": "open",
        "opentm": 1759233600.1234,
        "starttm": 0,
        "expiretm": 0,
        "descr": {"pair": pair, "type": side, "ordertype": order_type, "price": price, "price2": "0"},
        "vol": volume,
        "vol_exec": executed,
        "cost": "0.00000",
        "fee": "0.00000",
        "price": "0.00000",
        **extra,
    }


FIXTURES = {
    "Balance": {"ZEUR": "240.1234", "XXBT": "0.0000000000", "USDC": "10.00000000"},
    "TradeBalance": {"eb": "250.5678", "tb": "240.1234", "e": "240.1234", "mf": "240.1234"},
    "TradeVolume": {
        "currency": "ZUSD",
        "volume": "1234.5678",
        "fees": {"XXBTZEUR": {"fee": "0.4000", "minfee": "0.1000", "maxfee": "0.4000"}},
        "fees_maker": {"XXBTZEUR": {"fee": "0.2500", "minfee": "0.0000", "maxfee": "0.2500"}},
    },
    "OpenOrders": {
        "open": {
            FULL_TXIDS[0]: _order("XBTEUR", "buy", "limit", "50000.0", "0.01000000", "0.00250000", cl_ord_id=CL_ORD_ID),
            FULL_TXIDS[1]: _order("ETHEUR", "sell", "stop-loss", "2100.5", "0.50000000", "0.00000000"),
        }
    },
}
ENDPOINTS = ("Balance", "TradeBalance", "TradeVolume", "OpenOrders")


def ok(result: object) -> TransportReply:
    return TransportReply(200, json.dumps({"error": [], "result": result}).encode("utf-8"))


def kraken_error(*messages: str) -> TransportReply:
    return TransportReply(200, json.dumps({"error": list(messages)}).encode("utf-8"))


def success_replies() -> list[TransportReply]:
    return [ok(FIXTURES[name]) for name in ENDPOINTS]


class FixedClock:
    def current(self) -> datetime:
        return T0


class FakeTransport:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def post(self, url, body, headers, timeout_seconds):
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        if not self.replies:
            raise AssertionError("unexpected extra request")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self):
        self.closed = True


class SpyEnviron(dict):
    """An environ mapping that records every access."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accessed: list[str] = []

    def __getitem__(self, key):
        self.accessed.append(f"get:{key}")
        return super().__getitem__(key)

    def __contains__(self, key):
        self.accessed.append(f"contains:{key}")
        return super().__contains__(key)

    def get(self, key, default=None):
        self.accessed.append(f"get:{key}")
        return super().get(key, default)

    def __iter__(self):
        self.accessed.append("iter")
        return super().__iter__()


def tree(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


class NoSocket:
    """Any socket use during the test is a failure."""

    def __enter__(self):
        self._patches = [
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used")),
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")),
            mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("network used")),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False


class CheckCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="kraken-check-test-")
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)

    def run_check(self, argv, *, environ=None, replies=(), transport=None):
        transport = transport if transport is not None else FakeTransport(replies)
        built: list[FakeTransport] = []

        def factory():
            built.append(transport)
            return transport

        out, err = io.StringIO(), io.StringIO()
        with NoSocket():
            code = account_check.main(
                argv,
                environ=ENV if environ is None else environ,
                state_dir=self.state,
                clock=FixedClock(),
                transport_factory=factory,
                stdout=out,
                stderr=err,
            )
        return code, out.getvalue(), err.getvalue(), transport, built

    def forbidden_texts(self, transport: FakeTransport) -> list[str]:
        texts = [
            SYNTHETIC_KEY,
            SYNTHETIC_SECRET,
            SYNTHETIC_SECRET_BYTES.hex(),
            str(USERREF),
            CL_ORD_ID,
            *FULL_TXIDS,
            "API-Sign",
            "API-Key",
            "nonce=",
        ]
        for call in transport.calls:
            body = call["body"].decode("ascii")
            texts.append(body)
            texts.append(call["headers"]["API-Sign"])
            texts.append(body.split("&", 1)[0].removeprefix("nonce="))
        return texts

    def assert_redacted(self, transport: FakeTransport, *outputs: str) -> None:
        blob = "\n".join(outputs)
        for text in self.forbidden_texts(transport):
            self.assertNotIn(text, blob)


class TestModeGate(CheckCase):
    def test_missing_or_low_or_unknown_mode_refuses_before_anything_is_read(self):
        for argv in (
            [],
            ["--mode", "ANALYSIS_ONLY"],
            ["--mode", "RETROSPECTIVE"],
            ["--mode", "PAPER"],
            ["--mode", "shadow_live"],
            ["--mode", " SHADOW_LIVE"],
            ["--mode", ""],
            ["--mode", "LIVE"],
        ):
            with self.subTest(argv=argv):
                environ = SpyEnviron(ENV)
                code, out, err, transport, built = self.run_check(argv, environ=environ, replies=success_replies())
                self.assertEqual(code, account_check.EXIT_REFUSED)
                self.assertEqual(out, "")
                self.assertTrue(err.startswith("Refused:"), err)
                self.assertIn("Nothing was read.", err)
                self.assertEqual(environ.accessed, [])
                self.assertEqual(built, [])
                self.assertEqual(transport.calls, [])
                self.assertEqual(tree(self.state), set())
                self.assert_redacted(transport, out, err)

    def test_refusal_does_not_default_the_environment_or_state_dir(self):
        with mock.patch.object(account_check, "open_private_reader") as opener:
            code = account_check.main(["--mode", "PAPER"], stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(code, account_check.EXIT_REFUSED)
        opener.assert_not_called()

    def test_unknown_argument_is_refused(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            code, out, _err, transport, built = self.run_check(["--mode", "SHADOW_LIVE", "--order", "buy"])
        self.assertNotEqual(code, 0)
        self.assertEqual((out, built, transport.calls), ("", [], []))


class TestUnavailable(CheckCase):
    def test_shadow_live_without_credentials_is_unavailable_with_no_network(self):
        code, out, err, transport, built = self.run_check(["--mode", "SHADOW_LIVE"], environ={})
        self.assertEqual(code, account_check.EXIT_UNAVAILABLE)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("UNAVAILABLE: credentials_missing."), err)
        self.assertEqual(built, [])
        self.assertEqual(transport.calls, [])
        self.assertEqual(tree(self.state), set())

    def test_default_transport_is_never_built_without_credentials(self):
        out, err = io.StringIO(), io.StringIO()
        with NoSocket(), mock.patch.object(account_check, "RequestsTransport") as requests_transport:
            code = account_check.main(
                ["--mode", "SHADOW_LIVE"], environ={}, state_dir=self.state, stdout=out, stderr=err
            )
        self.assertEqual(code, account_check.EXIT_UNAVAILABLE)
        requests_transport.assert_not_called()

    def test_every_unavailable_reason_is_only_a_code_and_a_hint(self):
        for label, environ in (
            ("incomplete", {"KRAKEN_API_KEY": SYNTHETIC_KEY}),
            ("not_base64", {"KRAKEN_API_KEY": SYNTHETIC_KEY, "KRAKEN_API_SECRET": "!" + SYNTHETIC_SECRET}),
            ("key_malformed", {"KRAKEN_API_KEY": SYNTHETIC_KEY + "\n", "KRAKEN_API_SECRET": SYNTHETIC_SECRET}),
        ):
            with self.subTest(label=label):
                code, out, err, transport, built = self.run_check(["--mode", "SHADOW_LIVE"], environ=environ)
                self.assertEqual(code, account_check.EXIT_UNAVAILABLE)
                self.assertTrue(err.startswith("UNAVAILABLE: "), err)
                self.assertEqual(built, [])
                self.assert_redacted(transport, out, err)

    def test_corrupt_nonce_store_is_unavailable_and_kept(self):
        (self.state / ".kraken").mkdir()
        (self.state / ".kraken" / "nonce").write_text("not a number", encoding="ascii")
        code, _out, err, transport, built = self.run_check(["--mode", "SHADOW_LIVE"], replies=success_replies())
        self.assertEqual(code, account_check.EXIT_UNAVAILABLE)
        self.assertIn("nonce_store_corrupt", err)
        self.assertEqual((built, transport.calls), ([], []))
        self.assertEqual((self.state / ".kraken" / "nonce").read_text(encoding="ascii"), "not a number")


class TestSuccess(CheckCase):
    def test_prints_balances_fee_tier_and_open_orders_redacted(self):
        code, out, err, transport, _ = self.run_check(["--mode", "SHADOW_LIVE"], replies=success_replies())
        self.assertEqual(code, account_check.EXIT_OK, err)
        self.assertEqual(err, "")
        self.assertTrue(transport.closed)
        self.assertEqual(
            [call["url"] for call in transport.calls],
            [f"https://api.kraken.com/0/private/{name}" for name in ENDPOINTS],
        )
        self.assertIn(b"asset=ZEUR", transport.calls[1]["body"])
        self.assertIn(b"pair=XBTEUR", transport.calls[2]["body"])
        self.assertIn("Kraken account check (read only, mode SHADOW_LIVE)", out)
        for line in (
            "  ZEUR  240.1234",
            "  XXBT  0.0000000000",
            "  USDC  10.00000000",
            "Trade balance (ZEUR)",
            "  Equivalent balance: 250.5678",
            "  Trade balance: 240.1234",
            "Fee tier (requested pair XBTEUR)",
            "  30-day volume: 1234.5678 ZUSD",
            "  XXBTZEUR: taker 0.4000 %, maker 0.2500 %",
            "Open orders (2)",
        ):
            self.assertIn(line, out.splitlines())
        orders = [line for line in out.splitlines() if line.startswith("  O")]
        self.assertEqual(len(orders), 2)
        self.assertTrue(orders[0].startswith("  OQSYNT-... XBTEUR       buy  limit"), orders[0])
        for text in ("price 50000.0", "volume 0.01000000", "executed 0.00250000", "open"):
            self.assertIn(text, orders[0])
        self.assertTrue(orders[1].startswith("  OZSYNT-... ETHEUR       sell stop-loss"), orders[1])
        self.assertTrue(out.isascii())
        self.assert_redacted(transport, out, err)

    def test_pair_and_asset_options_and_credentials_file(self):
        kraken = self.state / ".kraken"
        kraken.mkdir()
        (kraken / "credentials.json").write_text(
            json.dumps({"api_key": SYNTHETIC_KEY, "api_secret": SYNTHETIC_SECRET}), encoding="utf-8"
        )
        code, out, err, transport, _ = self.run_check(
            ["--mode", "MICRO_LIVE", "--pair", "ETHEUR", "--asset", "ZUSD"], environ={}, replies=success_replies()
        )
        self.assertEqual(code, account_check.EXIT_OK, err)
        self.assertIn(b"asset=ZUSD", transport.calls[1]["body"])
        self.assertIn(b"pair=ETHEUR", transport.calls[2]["body"])
        self.assertEqual(transport.calls[0]["headers"]["API-Key"], SYNTHETIC_KEY)
        self.assert_redacted(transport, out, err)

    def test_credentials_file_saved_by_notepad_with_a_byte_order_mark(self):
        kraken = self.state / ".kraken"
        kraken.mkdir()
        document = json.dumps({"api_key": SYNTHETIC_KEY, "api_secret": SYNTHETIC_SECRET}, indent=2)
        (kraken / "credentials.json").write_bytes(b"\xef\xbb\xbf" + document.replace("\n", "\r\n").encode("utf-8"))
        code, _out, err, transport, _ = self.run_check(["--mode", "SHADOW_LIVE"], environ={}, replies=success_replies())
        self.assertEqual(code, account_check.EXIT_OK, err)
        self.assertEqual(transport.calls[0]["headers"]["API-Key"], SYNTHETIC_KEY)

    def test_empty_account(self):
        replies = [ok({}), ok({"eb": "0.0000", "tb": "0.0000"}), ok({"currency": "ZUSD", "volume": "0.0000"}),
                   ok({"open": {}})]
        code, out, err, _transport, _ = self.run_check(["--mode", "SHADOW_LIVE"], replies=replies)
        self.assertEqual(code, account_check.EXIT_OK, err)
        for line in ("  (no assets)", "  Kraken returned no fee for this pair.", "Open orders (0)", "  (none)"):
            self.assertIn(line, out.splitlines())

    def test_invalid_pair_is_refused_before_it_is_sent(self):
        code, out, err, transport, _ = self.run_check(
            ["--mode", "SHADOW_LIVE", "--pair", "XBT EUR&type=buy"], replies=success_replies()
        )
        self.assertEqual(code, account_check.EXIT_READ_FAILED)
        self.assertIn("PARAMETER_REFUSED", err)
        self.assertEqual(len(transport.calls), 2)  # Balance and TradeBalance only
        self.assertTrue(transport.closed)
        self.assertNotIn("type=buy", out + err)


class TestFailures(CheckCase):
    SCENARIOS = {
        "invalid_key": ([kraken_error("EAPI:Invalid key")], "Balance", "INVALID_KEY", 1),
        "invalid_nonce": ([ok(FIXTURES["Balance"]), kraken_error("EAPI:Invalid nonce")], "TradeBalance",
                          "INVALID_NONCE", 2),
        "permission": ([kraken_error("EGeneral:Permission denied")], "Balance", "PERMISSION_DENIED", 1),
        "rate_limit": ([ok(FIXTURES["Balance"]), ok(FIXTURES["TradeBalance"]), kraken_error("EAPI:Rate limit exceeded")],
                       "TradeVolume", "RATE_LIMITED", 3),
        "transport": ([*success_replies()[:3], TransportFailure(TransportFailureKind.CONNECTION)], "OpenOrders",
                      "TRANSPORT_FAILED", 4),
        "timeout": ([TransportFailure(TransportFailureKind.TIMEOUT)], "Balance", "TIMEOUT", 1),
        "http": ([TransportReply(503, b"")], "Balance", "HTTP_STATUS", 1),
        "leaky_exception": ([RuntimeError(f"{SYNTHETIC_KEY} {SYNTHETIC_SECRET} {CL_ORD_ID}")], "Balance",
                            "TRANSPORT_FAILED", 1),
    }

    def test_failures_print_only_a_typed_code_and_nothing_sensitive(self):
        for label, (replies, endpoint, code_text, calls) in self.SCENARIOS.items():
            with self.subTest(label=label):
                for item in (self.state / ".kraken").glob("*"):
                    item.unlink()
                code, out, err, transport, _ = self.run_check(["--mode", "SHADOW_LIVE"], replies=replies)
                self.assertEqual(code, account_check.EXIT_READ_FAILED)
                self.assertEqual(out, "")
                self.assertTrue(err.startswith(f"FAILED at {endpoint}: {code_text}"), err)
                self.assertEqual(len(transport.calls), calls)  # no retry, nothing after the failure
                self.assertTrue(transport.closed)
                self.assert_redacted(transport, out, err)

    def test_auth_error_never_prints_account_data_already_read(self):
        replies = [ok(FIXTURES["Balance"]), ok(FIXTURES["TradeBalance"]), kraken_error("EAPI:Invalid key")]
        code, out, err, _transport, _ = self.run_check(["--mode", "SHADOW_LIVE"], replies=replies)
        self.assertEqual(code, account_check.EXIT_READ_FAILED)
        self.assertEqual(out, "")
        self.assertNotIn("240.1234", err)

    def test_unexpected_exception_prints_only_its_type(self):
        with mock.patch.object(account_check, "_read_account", side_effect=ValueError(SYNTHETIC_SECRET)):
            code, out, err, transport, _ = self.run_check(["--mode", "SHADOW_LIVE"], replies=success_replies())
        self.assertEqual(code, account_check.EXIT_READ_FAILED)
        self.assertEqual(err.strip(), "FAILED: unexpected ValueError. Nothing more was read.")
        self.assertTrue(transport.closed)
        self.assert_redacted(transport, out, err)


class TestWrites(CheckCase):
    RADAR_FILES = ("radar_state.sqlite", "runs.jsonl", "events.jsonl", "radar.log")

    def test_only_write_is_the_nonce_store_under_the_state_dir(self):
        with mock.patch("sqlite3.connect", side_effect=AssertionError("database opened")):
            code, _out, err, _transport, _ = self.run_check(["--mode", "SHADOW_LIVE"], replies=success_replies())
        self.assertEqual(code, account_check.EXIT_OK, err)
        self.assertEqual(tree(self.state), {".kraken", ".kraken/nonce"})
        self.assertRegex((self.state / ".kraken" / "nonce").read_text(encoding="ascii"), r"^[0-9]+\n$")
        for name in self.RADAR_FILES:
            self.assertFalse((self.state / name).exists())

    def test_failure_also_writes_only_the_nonce_store(self):
        code, *_ = self.run_check(["--mode", "SHADOW_LIVE"], replies=[kraken_error("EAPI:Invalid key")])
        self.assertEqual(code, account_check.EXIT_READ_FAILED)
        self.assertEqual(tree(self.state), {".kraken", ".kraken/nonce"})

    def test_script_imports_no_radar_writer(self):
        allowed = {
            "__future__",
            "argparse",
            "collections.abc",
            "decimal",
            "os",
            "pathlib",
            "sys",
            "typing",
            ADAPTER_MODULE,
            "radar_v08.domain.trading_mode",
        }
        tree_ = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree_):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(imported - allowed, set())

    def test_loading_the_script_loads_no_radar_writer(self):
        with tempfile.TemporaryDirectory(prefix="kraken-check-child-") as child_state:
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("RADAR_") and not key.startswith("KRAKEN_")
            }
            environment["RADAR_STATE_DIR"] = child_state
            child = (
                "import importlib.util, json, sys\n"
                f"spec = importlib.util.spec_from_file_location('check', {str(SCRIPT)!r})\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "print(json.dumps(sorted(sys.modules)))\n"
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", child],
                cwd=REPO_DIR,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            loaded = set(json.loads(result.stdout))
            self.assertEqual(os.listdir(child_state), [])
        self.assertIn(ADAPTER_MODULE, loaded)
        for module in (
            "sqlite3",
            "radar_v08.config",
            "radar_v08.heartbeat",
            "radar_v08.store",
            "radar_v08.events",
            "radar_v08.adapters.pilot_store",
            "radar_v08.adapters.paper_store",
            "radar_v08.pilot_shadow",
        ):
            self.assertNotIn(module, loaded)


def _ast_imports_adapter(source: str, package: str) -> bool:
    """True when ``source`` (a module of ``package``) imports the adapter, directly or relatively."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(alias.name == ADAPTER_MODULE or alias.name.startswith(ADAPTER_MODULE + ".") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, base] if base else parts)
            if base == ADAPTER_MODULE or base.startswith(ADAPTER_MODULE + "."):
                return True
            if any(f"{base}.{alias.name}" == ADAPTER_MODULE for alias in node.names):
                return True
    return False


def _imports_adapter(path: Path) -> bool:
    """An import of the adapter in any form, or any mention of its module name (dynamic import)."""
    source = path.read_text(encoding="utf-8")
    package = ".".join(path.relative_to(REPO_DIR).with_suffix("").parts[:-1])
    return _ast_imports_adapter(source, package) or "kraken_private_read" in source


class TestImportBoundary(unittest.TestCase):
    def test_radar_runtime_and_ui_never_import_the_adapter(self):
        adapter = REPO_DIR / "radar_v08" / "adapters" / "kraken_private_read.py"
        files = [REPO_DIR / "radar.py", *sorted((REPO_DIR / "radar_v08").rglob("*.py")),
                 *sorted((REPO_DIR / "ui").rglob("*.py"))]
        files = [path for path in files if path != adapter and "__pycache__" not in path.parts]
        self.assertGreater(len(files), 50)
        importers = [path.relative_to(REPO_DIR).as_posix() for path in files if _imports_adapter(path)]
        self.assertEqual(importers, [])

    def test_the_scanner_resolves_every_static_import_form(self):
        for source, package in (
            ("import radar_v08.adapters.kraken_private_read\n", "radar_v08"),
            ("import radar_v08.adapters.kraken_private_read as reader\n", "ui"),
            ("from radar_v08.adapters import kraken_private_read\n", ""),
            ("from radar_v08.adapters.kraken_private_read import api_sign\n", "radar_v08.workflow"),
            ("from .kraken_private_read import api_sign\n", "radar_v08.adapters"),
            ("from . import kraken_private_read\n", "radar_v08.adapters"),
            ("from ..adapters import kraken_private_read\n", "radar_v08.execution"),
            ("from ..adapters.kraken_private_read import open_private_reader\n", "radar_v08.domain"),
            ("from .adapters import kraken_private_read\n", "radar_v08"),
        ):
            with self.subTest(source=source, package=package):
                self.assertTrue(_ast_imports_adapter(source, package))
        for source, package in (
            ("from radar_v08.adapters import kraken_timestamps\n", ""),
            ("from . import kraken_timestamps\n", "radar_v08.adapters"),
            ("from .kraken_spot import fetch_ticker\n", "radar_v08"),
        ):
            with self.subTest(source=source, package=package):
                self.assertFalse(_ast_imports_adapter(source, package))

    def test_only_the_script_and_tests_import_it(self):
        self.assertTrue(_imports_adapter(SCRIPT))
        self.assertTrue(_imports_adapter(TESTS_DIR / "test_kraken_private_read.py"))
        self.assertTrue(_ast_imports_adapter(SCRIPT.read_text(encoding="utf-8"), "scripts"))


NEW_FILES = (
    "radar_v08/adapters/kraken_private_read.py",
    "radar_v08/domain/trading_mode.py",
    "scripts/kraken_account_check.py",
    "tests/test_kraken_private_read.py",
    "tests/test_kraken_account_check.py",
    "tests/test_trading_mode.py",
    "docs/KRAKEN_READ_ONLY_API_KEY.md",
)


class TestGitignore(unittest.TestCase):
    """Read-only ``git check-ignore --no-index``: the index and the work tree are never touched."""

    def check_ignore(self, paths):
        git = shutil.which("git")
        self.assertIsNotNone(git, "git is required for the .gitignore check")
        environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        return subprocess.run(
            [git, "check-ignore", "--no-index", "--verbose", "--non-matching", "--", *paths],
            cwd=REPO_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_explicit_entry(self):
        lines = (REPO_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/.kraken/", lines)

    def test_kraken_folder_files_are_ignored(self):
        paths = [".kraken/credentials.json", ".kraken/nonce", ".kraken/nonce.12345.tmp"]
        result = self.check_ignore(paths)
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in result.stdout.splitlines():
            source, _tab, path = line.partition("	")
            with self.subTest(path=path):
                self.assertIn(path, paths)
                self.assertTrue(source.startswith(".gitignore:"), line)
                self.assertTrue(source.endswith(":/.kraken/"), line)  # the explicit entry, not a wildcard
        self.assertEqual(len(result.stdout.splitlines()), len(paths))

    def test_no_new_file_of_this_change_is_ignored(self):
        result = self.check_ignore(list(NEW_FILES))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)  # 1: nothing ignored
        for line in result.stdout.splitlines():
            with self.subTest(line=line):
                self.assertTrue(line.startswith("::	"), line)


class TestFileNames(unittest.TestCase):
    def test_new_files_avoid_gitignored_words(self):
        for relative in NEW_FILES:
            with self.subTest(relative=relative):
                self.assertTrue((REPO_DIR / relative).is_file())
                self.assertNotIn("secret", relative.lower())
                self.assertNotIn("token", relative.lower())


if __name__ == "__main__":
    unittest.main()
