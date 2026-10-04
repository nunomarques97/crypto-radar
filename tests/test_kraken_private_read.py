"""Kraken private READ-ONLY adapter (radar_v08/adapters/kraken_private_read.py).

Only fake transports and synthetic fixtures: no socket, no real credential, no real state
file (every state directory is a fresh temporary directory). The key and secret below are
synthetic values made for these tests.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
import io
import json
import logging
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import requests
from requests.adapters import BaseAdapter

from radar_v08.adapters import kraken_private_read as kpr
from radar_v08.adapters.kraken_private_read import (
    AUTH_ERROR_CODES,
    Balances,
    ClosedOrders,
    FeeTier,
    KrakenCredentials,
    KrakenPrivateReader,
    KrakenReadError,
    ModeRefused,
    NonceStore,
    OpenOrders,
    QueriedOrders,
    ReadEndpoint,
    ReadErrorCode,
    RequestsTransport,
    TradeBalance,
    TransportFailure,
    TransportFailureKind,
    TransportReply,
    Unavailable,
    UnavailableReason,
    api_sign,
    load_credentials,
    open_private_reader,
)
from radar_v08.domain.trading_mode import TradingMode

# Synthetic credentials (never a real key).
SYNTHETIC_KEY = "SyntheticTestKeyAAAAbbbbCCCCddddEEEEffff0123456789+/="
SYNTHETIC_SECRET_BYTES = bytes(range(7, 71))
SYNTHETIC_SECRET = base64.b64encode(SYNTHETIC_SECRET_BYTES).decode("ascii")
ENV = {"KRAKEN_API_KEY": SYNTHETIC_KEY, "KRAKEN_API_SECRET": SYNTHETIC_SECRET}

# Kraken's documented spot REST authentication example.
DOC_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
DOC_NONCE = "1616492376594"
DOC_PATH = "/0/private/AddOrder"
DOC_POSTDATA = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
DOC_SIGNATURE = "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="

T0 = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
T0_MS = int(T0.timestamp()) * 1000

REFUSED_ENDPOINTS = [
    "AddOrder",
    "AddOrderBatch",
    "EditOrder",
    "AmendOrder",
    "CancelOrder",
    "CancelAll",
    "CancelAllOrdersAfter",
    "CancelOrderBatch",
    "Withdraw",
    "WithdrawCancel",
    "WalletTransfer",
    "DepositAddresses",
    "DepositMethods",
    "GetWebSocketsToken",
    "CreateSubaccount",
    "AccountTransfer",
    "Stake",
    "Unstake",
    "balance",
    "BALANCE",
    "bALANCE",
    "openorders",
    " Balance",
    "Balance ",
    "Balance\n",
    "Bal ance",
    "\tBalance",
    "Balance/../AddOrder",
    "Balance/%2e%2e/AddOrder",
    "%2e%2e/AddOrder",
    "../AddOrder",
    "/0/private/Balance",
    "private/Balance",
    "Balance?x=1",
    "Balance#frag",
    "Balance/",
    "Balance.",
    "Balance;AddOrder",
    "Balance\x00",
    "https://api.kraken.com/0/private/Balance",
    "",
    "BALANCE_",
    "ReadEndpoint.BALANCE",
    "TradeVolume,AddOrder",
    None,
    1,
    b"Balance",
    ["Balance"],
]

WRITE_STYLE_PARAMETERS = [
    "ordertype",
    "type",
    "volume",
    "price",
    "price2",
    "leverage",
    "key",
    "amount",
    "address",
    "asset_class",
    "nonce",
    "otp",
    "oflags",
    "validate",
    "cancel",
    "Pair",
    "",
]


class FixedClock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def current(self) -> datetime:
        return self.now


def ok(result: object) -> TransportReply:
    return TransportReply(200, json.dumps({"error": [], "result": result}).encode("utf-8"))


def kraken_error(*messages: str) -> TransportReply:
    return TransportReply(200, json.dumps({"error": list(messages)}).encode("utf-8"))


class FakeTransport:
    """Records every post and the nonce file content at that moment; replays scripted replies."""

    def __init__(self, replies=(), nonce_path: Path | None = None):
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []
        self.nonce_path = nonce_path
        self.closed = False

    def post(self, url, body, headers, timeout_seconds):
        on_disk = None
        if self.nonce_path is not None and self.nonce_path.exists():
            on_disk = self.nonce_path.read_text(encoding="ascii").strip()
        self.calls.append(
            {"url": url, "body": body, "headers": dict(headers), "timeout": timeout_seconds, "nonce_on_disk": on_disk}
        )
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

    def keys(self):
        self.accessed.append("keys")
        return super().keys()

    def __iter__(self):
        self.accessed.append("iter")
        return super().__iter__()


class StateDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="kraken-read-test-")
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        self.kraken_dir = self.state / ".kraken"
        self.nonce_path = self.kraken_dir / "nonce"
        self.clock = FixedClock()

    def write_credentials_file(self, content: object) -> None:
        self.kraken_dir.mkdir(exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content)
        (self.kraken_dir / "credentials.json").write_text(text, encoding="utf-8")

    def reader(self, replies=(), environ=None, mode=TradingMode.SHADOW_LIVE):
        transport = FakeTransport(replies, self.nonce_path)
        result = open_private_reader(
            mode,
            environ=ENV if environ is None else environ,
            state_dir=self.state,
            clock=self.clock,
            transport_factory=lambda: transport,
        )
        self.assertIsInstance(result, KrakenPrivateReader)
        return result, transport


# --------------------------------------------------------------------------- signing


class TestSigning(unittest.TestCase):
    def test_documented_vector(self):
        signature = api_sign(DOC_PATH, DOC_NONCE, DOC_POSTDATA, base64.b64decode(DOC_SECRET))
        self.assertEqual(signature, DOC_SIGNATURE)

    def test_signature_changes_with_every_input(self):
        secret = base64.b64decode(DOC_SECRET)
        base = api_sign(DOC_PATH, DOC_NONCE, DOC_POSTDATA, secret)
        self.assertNotEqual(api_sign("/0/private/Balance", DOC_NONCE, DOC_POSTDATA, secret), base)
        self.assertNotEqual(api_sign(DOC_PATH, "1616492376595", DOC_POSTDATA, secret), base)
        self.assertNotEqual(api_sign(DOC_PATH, DOC_NONCE, DOC_POSTDATA + "&x=1", secret), base)
        self.assertNotEqual(api_sign(DOC_PATH, DOC_NONCE, DOC_POSTDATA, secret[:-1]), base)


class TestRequestShape(StateDirCase):
    def test_signed_post_to_the_exact_private_url(self):
        reader, transport = self.reader([ok({"ZEUR": "10.0000"})])
        reader.balance()
        (call,) = transport.calls
        self.assertEqual(call["url"], "https://api.kraken.com/0/private/Balance")
        self.assertEqual(call["body"], f"nonce={T0_MS}".encode("ascii"))
        self.assertEqual(call["timeout"], kpr.REQUEST_TIMEOUT_SECONDS)
        self.assertLessEqual(call["timeout"], 15.0)
        headers = call["headers"]
        self.assertEqual(headers["API-Key"], SYNTHETIC_KEY)
        self.assertEqual(
            headers["API-Sign"],
            api_sign("/0/private/Balance", str(T0_MS), f"nonce={T0_MS}", SYNTHETIC_SECRET_BYTES),
        )
        self.assertTrue(headers["Content-Type"].startswith("application/x-www-form-urlencoded"))

    def test_body_starts_with_nonce_then_sorted_allowlisted_parameters(self):
        reader, transport = self.reader([ok({"open": {}})])
        reader.read("OpenOrders", {"userref": 7, "trades": True, "cl_ord_id": "abc-1"})
        body = transport.calls[0]["body"].decode("ascii")
        self.assertEqual(body, f"nonce={T0_MS}&cl_ord_id=abc-1&trades=true&userref=7")
        self.assertEqual(
            transport.calls[0]["headers"]["API-Sign"],
            api_sign("/0/private/OpenOrders", str(T0_MS), body, SYNTHETIC_SECRET_BYTES),
        )

    def test_each_allowlisted_endpoint_hits_its_own_url(self):
        fixtures = {
            ReadEndpoint.BALANCE: ({}, {"ZEUR": "1.0"}),
            ReadEndpoint.TRADE_BALANCE: ({"asset": "ZEUR"}, {"eb": "1.0", "tb": "1.0"}),
            ReadEndpoint.TRADE_VOLUME: ({"pair": "XBTEUR"}, {"currency": "ZEUR", "volume": "0.0"}),
            ReadEndpoint.OPEN_ORDERS: ({}, {"open": {}}),
            ReadEndpoint.CLOSED_ORDERS: ({"ofs": 0}, {"closed": {}, "count": 0}),
            ReadEndpoint.QUERY_ORDERS: ({"txid": ["OQCLML-BW3P3-BUCMWZ"]}, {}),
        }
        self.assertEqual(set(fixtures), set(ReadEndpoint))
        reader, transport = self.reader([ok(result) for _, result in fixtures.values()])
        for endpoint, (parameters, _) in fixtures.items():
            reader.read(endpoint.value, parameters)
        self.assertEqual(
            [call["url"] for call in transport.calls],
            [f"https://api.kraken.com/0/private/{endpoint.value}" for endpoint in fixtures],
        )


# --------------------------------------------------------------------------- allowlist


class TestEndpointAllowlist(StateDirCase):
    def test_allowlist_is_exactly_the_six_reads(self):
        self.assertEqual(
            {endpoint.value for endpoint in ReadEndpoint},
            {"Balance", "TradeBalance", "TradeVolume", "OpenOrders", "ClosedOrders", "QueryOrders"},
        )
        self.assertEqual(set(kpr.PARAMETER_ALLOWLIST), set(ReadEndpoint))

    def test_write_and_trick_names_are_refused_before_nonce_signature_or_transport(self):
        reader, transport = self.reader()
        with mock.patch.object(kpr, "api_sign", wraps=kpr.api_sign) as signer:
            for name in REFUSED_ENDPOINTS:
                with self.subTest(name=name):
                    with self.assertRaises(KrakenReadError) as caught:
                        reader.read(name)
                    self.assertIs(caught.exception.code, ReadErrorCode.ENDPOINT_REFUSED)
        self.assertEqual(signer.call_count, 0)
        self.assertEqual(transport.calls, [])
        self.assertFalse(self.nonce_path.exists())
        self.assertFalse(self.kraken_dir.exists())

    def test_string_subclass_cannot_impersonate_an_endpoint(self):
        class Sneaky(str):
            def __eq__(self, other):
                return True

            __hash__ = str.__hash__

        reader, transport = self.reader()
        with self.assertRaises(KrakenReadError) as caught:
            reader.read(Sneaky("AddOrder"))
        self.assertIs(caught.exception.code, ReadErrorCode.ENDPOINT_REFUSED)
        self.assertEqual(transport.calls, [])

    def test_no_public_method_takes_an_arbitrary_endpoint(self):
        public = {
            name: member
            for name, member in inspect.getmembers(KrakenPrivateReader, callable)
            if not name.startswith("_")
        }
        self.assertEqual(
            set(public),
            {"read", "balance", "trade_balance", "trade_volume", "open_orders", "closed_orders", "query_orders", "close"},
        )
        for name, member in public.items():
            parameters = set(inspect.signature(member).parameters) - {"self"}
            if name == "read":
                self.assertEqual(parameters, {"endpoint", "parameters"})
            else:
                self.assertFalse({"endpoint", "path", "url", "method", "name"} & parameters, name)
        for forbidden in ("add_order", "cancel_order", "cancel_all", "withdraw", "edit_order", "amend_order"):
            self.assertFalse(hasattr(KrakenPrivateReader, forbidden))

    def test_transport_url_comes_from_the_enum_not_caller_text(self):
        reader, transport = self.reader([ok({"ZEUR": "1.0"})])
        reader.read(ReadEndpoint.BALANCE)
        self.assertEqual(transport.calls[0]["url"], ReadEndpoint.BALANCE.url)


class TestParameterAllowlist(StateDirCase):
    def test_write_style_and_unknown_parameters_are_refused_before_any_call(self):
        reader, transport = self.reader()
        for endpoint in ReadEndpoint:
            for parameter in WRITE_STYLE_PARAMETERS:
                with self.subTest(endpoint=endpoint, parameter=parameter):
                    with self.assertRaises(KrakenReadError) as caught:
                        reader.read(endpoint, {parameter: "1"})
                    self.assertIs(caught.exception.code, ReadErrorCode.PARAMETER_REFUSED)
        self.assertEqual(transport.calls, [])
        self.assertFalse(self.nonce_path.exists())

    def test_parameter_names_are_per_endpoint(self):
        reader, transport = self.reader()
        misplaced = [
            (ReadEndpoint.BALANCE, {"asset": "ZEUR"}),
            (ReadEndpoint.TRADE_BALANCE, {"pair": "XBTEUR"}),
            (ReadEndpoint.TRADE_VOLUME, {"asset": "ZEUR"}),
            (ReadEndpoint.OPEN_ORDERS, {"start": 1}),
            (ReadEndpoint.OPEN_ORDERS, {"txid": ["OQCLML-BW3P3-BUCMWZ"]}),
            (ReadEndpoint.QUERY_ORDERS, {"txid": ["OQCLML-BW3P3-BUCMWZ"], "cl_ord_id": "x"}),
            (ReadEndpoint.QUERY_ORDERS, {"txid": ["OQCLML-BW3P3-BUCMWZ"], "ofs": 0}),
        ]
        for endpoint, parameters in misplaced:
            with self.subTest(endpoint=endpoint, parameters=parameters):
                with self.assertRaises(KrakenReadError) as caught:
                    reader.read(endpoint, parameters)
                self.assertEqual(caught.exception.reason, "parameter_not_allowlisted")
        self.assertEqual(transport.calls, [])

    def test_invalid_values_are_refused(self):
        reader, transport = self.reader()
        txid = "OQCLML-BW3P3-BUCMWZ"
        invalid = [
            (ReadEndpoint.TRADE_BALANCE, {"asset": "ZEUR&type=sell"}),
            (ReadEndpoint.TRADE_BALANCE, {"asset": ""}),
            (ReadEndpoint.TRADE_BALANCE, {"asset": 5}),
            (ReadEndpoint.TRADE_VOLUME, {"pair": "XBT EUR"}),
            (ReadEndpoint.TRADE_VOLUME, {"pair": "XBTEUR&volume=1"}),
            (ReadEndpoint.OPEN_ORDERS, {"trades": "true"}),
            (ReadEndpoint.OPEN_ORDERS, {"trades": 1}),
            (ReadEndpoint.OPEN_ORDERS, {"userref": True}),
            (ReadEndpoint.OPEN_ORDERS, {"userref": "7"}),
            (ReadEndpoint.OPEN_ORDERS, {"userref": 2**31}),
            (ReadEndpoint.OPEN_ORDERS, {"cl_ord_id": "a b"}),
            (ReadEndpoint.CLOSED_ORDERS, {"closetime": "never"}),
            (ReadEndpoint.CLOSED_ORDERS, {"ofs": -1}),
            (ReadEndpoint.CLOSED_ORDERS, {"start": -5}),
            (ReadEndpoint.CLOSED_ORDERS, {"start": 1.5}),
            (ReadEndpoint.QUERY_ORDERS, {}),
            (ReadEndpoint.QUERY_ORDERS, {"txid": txid}),
            (ReadEndpoint.QUERY_ORDERS, {"txid": []}),
            (ReadEndpoint.QUERY_ORDERS, {"txid": [txid, txid]}),
            (ReadEndpoint.QUERY_ORDERS, {"txid": ["not-a-txid"]}),
            (ReadEndpoint.QUERY_ORDERS, {"txid": [f"OQCLML-BW3P3-{n:06d}" for n in range(51)]}),
            (ReadEndpoint.BALANCE, "ordertype=limit"),
            (ReadEndpoint.BALANCE, [("volume", "1")]),
        ]
        for endpoint, parameters in invalid:
            with self.subTest(endpoint=endpoint, parameters=parameters):
                with self.assertRaises(KrakenReadError) as caught:
                    reader.read(endpoint, parameters)
                self.assertIs(caught.exception.code, ReadErrorCode.PARAMETER_REFUSED)
        self.assertEqual(transport.calls, [])
        self.assertFalse(self.nonce_path.exists())

    def test_fifty_txids_are_accepted(self):
        txids = [f"OQCLML-BW3P3-{n:06d}" for n in range(50)]
        reader, transport = self.reader([ok({})])
        reader.query_orders(txids)
        self.assertIn("txid=" + "%2C".join(txids), transport.calls[0]["body"].decode("ascii"))


# --------------------------------------------------------------------------- nonce


class TestNonce(StateDirCase):
    def test_strictly_increasing_across_calls_with_a_frozen_clock(self):
        reader, transport = self.reader([ok({})] * 3)
        for _ in range(3):
            reader.balance()
        nonces = [int(call["body"].decode().split("=")[1]) for call in transport.calls]
        self.assertEqual(nonces, [T0_MS, T0_MS + 1, T0_MS + 2])

    def test_follows_the_clock_when_it_moves_forward(self):
        reader, transport = self.reader([ok({})] * 2)
        reader.balance()
        self.clock.now = T0 + timedelta(seconds=5)
        reader.balance()
        self.assertEqual(transport.calls[1]["body"], f"nonce={T0_MS + 5000}".encode())

    def test_clock_moving_backward_never_lowers_the_nonce(self):
        reader, transport = self.reader([ok({})] * 2)
        reader.balance()
        self.clock.now = T0 - timedelta(hours=3)
        reader.balance()
        self.assertEqual(transport.calls[1]["body"], f"nonce={T0_MS + 1}".encode())

    def test_restart_continues_above_the_persisted_value(self):
        reader, _ = self.reader([ok({})] * 2)
        reader.balance()
        reader.balance()
        self.clock.now = T0 - timedelta(days=1)
        restarted, transport = self.reader([ok({})])
        restarted.balance()
        self.assertEqual(transport.calls[0]["body"], f"nonce={T0_MS + 2}".encode())
        self.assertEqual(NonceStore(self.state, self.clock).last(), T0_MS + 2)

    def test_value_is_persisted_before_the_request_is_sent(self):
        reader, transport = self.reader([ok({})] * 2)
        reader.balance()
        reader.balance()
        for call in transport.calls:
            self.assertEqual(call["nonce_on_disk"], call["body"].decode().split("=")[1])

    def test_nonce_is_consumed_even_when_the_request_fails(self):
        reader, transport = self.reader([TransportFailure(TransportFailureKind.TIMEOUT), ok({})])
        with self.assertRaises(KrakenReadError):
            reader.balance()
        reader.balance()
        self.assertEqual(transport.calls[1]["body"], f"nonce={T0_MS + 1}".encode())

    def test_corrupt_nonce_file_is_unavailable_at_construction_and_never_reset(self):
        for content in (b"", b"abc\n", b"-5\n", b"12.5\n", b"1 2\n", b"\xff\xfe", b"9" * 25 + b"\n"):
            with self.subTest(content=content):
                self.kraken_dir.mkdir(exist_ok=True)
                self.nonce_path.write_bytes(content)
                calls = []
                result = open_private_reader(
                    TradingMode.SHADOW_LIVE,
                    environ=ENV,
                    state_dir=self.state,
                    clock=self.clock,
                    transport_factory=lambda: calls.append(1) or FakeTransport(),
                )
                self.assertEqual(result, Unavailable(UnavailableReason.NONCE_STORE_CORRUPT))
                self.assertEqual(calls, [])
                self.assertEqual(self.nonce_path.read_bytes(), content)

    def test_corruption_after_construction_is_unavailable_without_sending(self):
        reader, transport = self.reader([ok({})])
        reader.balance()
        self.nonce_path.write_bytes(b"garbage")
        with self.assertRaises(KrakenReadError) as caught:
            reader.balance()
        self.assertIs(caught.exception.code, ReadErrorCode.UNAVAILABLE)
        self.assertEqual(caught.exception.reason, "nonce_store_corrupt")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.nonce_path.read_bytes(), b"garbage")

    def test_unreadable_nonce_path_is_unavailable(self):
        self.nonce_path.mkdir(parents=True)  # a directory where the file should be
        result = open_private_reader(
            TradingMode.SHADOW_LIVE, environ=ENV, state_dir=self.state, clock=self.clock,
            transport_factory=FakeTransport,
        )
        self.assertIsInstance(result, Unavailable)
        self.assertIn(
            result.reason, {UnavailableReason.NONCE_STORE_UNREADABLE, UnavailableReason.NONCE_STORE_CORRUPT}
        )

    def test_unwritable_store_sends_nothing(self):
        reader, transport = self.reader([ok({})])
        with mock.patch.object(kpr.os, "replace", side_effect=PermissionError("denied")):
            with self.assertRaises(KrakenReadError) as caught:
                reader.balance()
        self.assertEqual(caught.exception.reason, "nonce_store_unwritable")
        self.assertEqual(transport.calls, [])
        self.assertEqual([p.name for p in self.kraken_dir.iterdir() if p.name != "nonce"], [])

    def test_naive_clock_is_refused(self):
        self.clock.now = datetime(2026, 9, 30, 12, 0, 0)
        reader, transport = self.reader()
        with self.assertRaises(KrakenReadError) as caught:
            reader.balance()
        self.assertEqual(caught.exception.reason, "clock_invalid")
        self.assertEqual(transport.calls, [])

    def test_concurrent_reservations_are_unique_and_increasing(self):
        store = NonceStore(self.state, self.clock)
        seen: list[int] = []
        lock = threading.Lock()

        def work():
            for _ in range(20):
                value = store.reserve()
                with lock:
                    seen.append(value)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(seen)), 80)
        self.assertEqual(store.last(), max(seen))


# --------------------------------------------------------------------------- credentials


class TestCredentials(StateDirCase):
    def assert_unavailable_and_untouched(self, environ, reason):
        constructed = []
        result = open_private_reader(
            TradingMode.SHADOW_LIVE,
            environ=environ,
            state_dir=self.state,
            clock=self.clock,
            transport_factory=lambda: constructed.append(1) or FakeTransport(),
        )
        self.assertEqual(result, Unavailable(reason))
        self.assertEqual(constructed, [])
        self.assertFalse(self.nonce_path.exists())

    def test_environment_pair(self):
        credentials = load_credentials(ENV, self.state)
        self.assertIsInstance(credentials, KrakenCredentials)
        self.assertEqual(credentials.api_key, SYNTHETIC_KEY)
        self.assertEqual(credentials.secret, SYNTHETIC_SECRET_BYTES)

    def test_file_when_no_environment_variable_is_set(self):
        self.write_credentials_file({"api_key": SYNTHETIC_KEY, "api_secret": SYNTHETIC_SECRET})
        credentials = load_credentials({"PATH": "x"}, self.state)
        self.assertIsInstance(credentials, KrakenCredentials)
        self.assertEqual(credentials.secret, SYNTHETIC_SECRET_BYTES)

    def test_environment_wins_and_sources_never_mix(self):
        other_secret = base64.b64encode(b"other-synthetic").decode()
        self.write_credentials_file({"api_key": "FileKey", "api_secret": other_secret})
        credentials = load_credentials(ENV, self.state)
        self.assertEqual((credentials.api_key, credentials.secret), (SYNTHETIC_KEY, SYNTHETIC_SECRET_BYTES))
        # One variable alone never borrows the other half from the file.
        self.assertEqual(
            load_credentials({"KRAKEN_API_KEY": SYNTHETIC_KEY}, self.state),
            Unavailable(UnavailableReason.CREDENTIALS_INCOMPLETE),
        )
        self.assertEqual(
            load_credentials({"KRAKEN_API_SECRET": SYNTHETIC_SECRET}, self.state),
            Unavailable(UnavailableReason.CREDENTIALS_INCOMPLETE),
        )

    def test_missing(self):
        self.assert_unavailable_and_untouched({}, UnavailableReason.CREDENTIALS_MISSING)

    def test_only_one_environment_variable(self):
        self.assert_unavailable_and_untouched({"KRAKEN_API_KEY": SYNTHETIC_KEY}, UnavailableReason.CREDENTIALS_INCOMPLETE)
        self.assert_unavailable_and_untouched(
            {"KRAKEN_API_SECRET": SYNTHETIC_SECRET}, UnavailableReason.CREDENTIALS_INCOMPLETE
        )

    def test_empty_values(self):
        self.assert_unavailable_and_untouched(
            {"KRAKEN_API_KEY": "", "KRAKEN_API_SECRET": SYNTHETIC_SECRET}, UnavailableReason.CREDENTIALS_EMPTY
        )
        self.assert_unavailable_and_untouched(
            {"KRAKEN_API_KEY": SYNTHETIC_KEY, "KRAKEN_API_SECRET": ""}, UnavailableReason.CREDENTIALS_EMPTY
        )

    def test_secret_that_is_not_strict_base64(self):
        for secret in ("not base64!", "abc", SYNTHETIC_SECRET + "\n", " " + SYNTHETIC_SECRET, "====", "é"):
            with self.subTest(secret=secret):
                self.assert_unavailable_and_untouched(
                    {"KRAKEN_API_KEY": SYNTHETIC_KEY, "KRAKEN_API_SECRET": secret}, UnavailableReason.SECRET_NOT_BASE64
                )

    def test_key_with_header_breaking_characters(self):
        for key in (SYNTHETIC_KEY + "\r\nX-Evil: 1", "key with space", "k\x00", "é"):
            with self.subTest(key=key):
                self.assert_unavailable_and_untouched(
                    {"KRAKEN_API_KEY": key, "KRAKEN_API_SECRET": SYNTHETIC_SECRET}, UnavailableReason.API_KEY_MALFORMED
                )

    def test_malformed_files(self):
        cases = [
            ("{not json", UnavailableReason.CREDENTIALS_MALFORMED),
            ("[]", UnavailableReason.CREDENTIALS_MALFORMED),
            ('"text"', UnavailableReason.CREDENTIALS_MALFORMED),
            (json.dumps({"api_key": SYNTHETIC_KEY}), UnavailableReason.CREDENTIALS_INCOMPLETE),
            (json.dumps({"api_key": SYNTHETIC_KEY, "api_secret": SYNTHETIC_SECRET, "extra": 1}),
             UnavailableReason.CREDENTIALS_MALFORMED),
            (json.dumps({"api_key": 5, "api_secret": SYNTHETIC_SECRET}), UnavailableReason.CREDENTIALS_MALFORMED),
            (json.dumps({"api_key": "", "api_secret": ""}), UnavailableReason.CREDENTIALS_EMPTY),
            (json.dumps({"api_key": SYNTHETIC_KEY, "api_secret": "!!"}), UnavailableReason.SECRET_NOT_BASE64),
            ('{"api_key": "a", "api_key": "b", "api_secret": "c"}', UnavailableReason.CREDENTIALS_MALFORMED),
            ('{"api_key": NaN, "api_secret": "c"}', UnavailableReason.CREDENTIALS_MALFORMED),
            ("x" * 5000, UnavailableReason.CREDENTIALS_MALFORMED),
        ]
        for content, reason in cases:
            with self.subTest(content=content[:40]):
                self.write_credentials_file(content)
                self.assert_unavailable_and_untouched({}, reason)

    def test_non_utf8_file(self):
        self.kraken_dir.mkdir()
        (self.kraken_dir / "credentials.json").write_bytes(b"\xff\xfe{}")
        self.assert_unavailable_and_untouched({}, UnavailableReason.CREDENTIALS_MALFORMED)

    def test_unreadable_file(self):
        (self.kraken_dir / "credentials.json").mkdir(parents=True)
        self.assert_unavailable_and_untouched({}, UnavailableReason.CREDENTIALS_UNREADABLE)

    def test_holder_never_prints_its_values(self):
        credentials = KrakenCredentials(SYNTHETIC_KEY, SYNTHETIC_SECRET_BYTES)
        for text in (repr(credentials), str(credentials), f"{credentials}", repr([credentials])):
            self.assertNotIn(SYNTHETIC_KEY, text)
            self.assertNotIn(SYNTHETIC_SECRET, text)
            self.assertNotIn(repr(SYNTHETIC_SECRET_BYTES), text)
        with self.assertRaises(TypeError):
            import pickle

            pickle.dumps(credentials)

    def test_default_state_dir_comes_from_the_injected_environment(self):
        self.assertEqual(kpr.default_state_dir({"RADAR_STATE_DIR": str(self.state)}), self.state)
        self.write_credentials_file({"api_key": SYNTHETIC_KEY, "api_secret": SYNTHETIC_SECRET})
        transport = FakeTransport([ok({})], self.nonce_path)
        reader = open_private_reader(
            TradingMode.SHADOW_LIVE, environ={"RADAR_STATE_DIR": str(self.state)}, clock=self.clock,
            transport_factory=lambda: transport,
        )
        self.assertIsInstance(reader, KrakenPrivateReader)
        reader.balance()
        self.assertTrue(self.nonce_path.exists())


# --------------------------------------------------------------------------- mode gate


class TestModeGate(StateDirCase):
    def test_modes_below_shadow_live_touch_nothing(self):
        self.write_credentials_file({"api_key": SYNTHETIC_KEY, "api_secret": SYNTHETIC_SECRET})
        for mode in (TradingMode.ANALYSIS_ONLY, TradingMode.RETROSPECTIVE, TradingMode.PAPER):
            with self.subTest(mode=mode):
                environ = SpyEnviron(ENV)
                constructed = []
                with mock.patch.object(kpr, "load_credentials", wraps=kpr.load_credentials) as loader:
                    result = open_private_reader(
                        mode,
                        environ=environ,
                        state_dir=self.state,
                        clock=self.clock,
                        transport_factory=lambda: constructed.append(1) or FakeTransport(),
                    )
                self.assertEqual(result, ModeRefused(mode))
                self.assertEqual(environ.accessed, [])
                self.assertEqual(loader.call_count, 0)
                self.assertEqual(constructed, [])
                self.assertFalse(self.nonce_path.exists())

    def test_non_mode_values_are_refused(self):
        for value in ("SHADOW_LIVE", "APPROVED_ENVELOPE", 3, None, True):
            with self.subTest(value=value):
                environ = SpyEnviron(ENV)
                result = open_private_reader(
                    value, environ=environ, state_dir=self.state, transport_factory=FakeTransport  # type: ignore[arg-type]
                )
                self.assertEqual(result, ModeRefused(None))
                self.assertEqual(environ.accessed, [])

    def test_shadow_live_and_above_construct(self):
        for mode in (
            TradingMode.SHADOW_LIVE,
            TradingMode.MICRO_LIVE,
            TradingMode.CONSTRAINED_LIVE,
            TradingMode.APPROVED_ENVELOPE,
        ):
            with self.subTest(mode=mode):
                reader, transport = self.reader(mode=mode)
                self.assertIs(reader.mode, mode)
                self.assertEqual(transport.calls, [])

    def test_reader_class_refuses_a_low_mode(self):
        credentials = KrakenCredentials(SYNTHETIC_KEY, SYNTHETIC_SECRET_BYTES)
        for mode in (TradingMode.ANALYSIS_ONLY, TradingMode.RETROSPECTIVE, TradingMode.PAPER, "SHADOW_LIVE"):
            with self.subTest(mode=mode):
                with self.assertRaises(KrakenReadError) as caught:
                    KrakenPrivateReader(mode, credentials, NonceStore(self.state, self.clock), FakeTransport())
                self.assertIs(caught.exception.code, ReadErrorCode.MODE_REFUSED)

    def test_transport_factory_failure_is_unavailable(self):
        def broken():
            raise RuntimeError(f"boom {SYNTHETIC_SECRET}")

        result = open_private_reader(
            TradingMode.SHADOW_LIVE, environ=ENV, state_dir=self.state, clock=self.clock, transport_factory=broken
        )
        self.assertEqual(result, Unavailable(UnavailableReason.TRANSPORT_UNAVAILABLE))


# --------------------------------------------------------------------------- errors


class TestKrakenErrors(StateDirCase):
    def test_auth_errors_lock_the_reader(self):
        cases = {
            "EAPI:Invalid key": ReadErrorCode.INVALID_KEY,
            "EAPI:Invalid signature": ReadErrorCode.INVALID_SIGNATURE,
            "EAPI:Invalid nonce": ReadErrorCode.INVALID_NONCE,
            "EGeneral:Permission denied": ReadErrorCode.PERMISSION_DENIED,
        }
        self.assertEqual(set(cases.values()), set(AUTH_ERROR_CODES))
        for message, code in cases.items():
            with self.subTest(message=message):
                self.nonce_path.unlink(missing_ok=True)
                reader, transport = self.reader([kraken_error(message), ok({})])
                with self.assertRaises(KrakenReadError) as caught:
                    reader.balance()
                self.assertIs(caught.exception.code, code)
                self.assertIs(reader.locked_by, code)
                nonce_after_error = self.nonce_path.read_bytes()
                for call in (reader.balance, reader.trade_volume, lambda: reader.read("OpenOrders")):
                    with self.assertRaises(KrakenReadError) as locked:
                        call()
                    self.assertIs(locked.exception.code, ReadErrorCode.LOCKED)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(self.nonce_path.read_bytes(), nonce_after_error)

    def test_rate_limit_is_typed_and_not_retried(self):
        for message in ("EAPI:Rate limit exceeded", "EGeneral:Too many requests"):
            with self.subTest(message=message):
                reader, transport = self.reader([kraken_error(message), ok({"ZEUR": "1.0"})])
                with self.assertRaises(KrakenReadError) as caught:
                    reader.balance()
                self.assertIs(caught.exception.code, ReadErrorCode.RATE_LIMITED)
                self.assertEqual(len(transport.calls), 1)
                self.assertIsNone(reader.locked_by)
                self.assertIsInstance(reader.balance(), Balances)

    def test_service_unavailable(self):
        for message in ("EService:Unavailable", "EService:Busy"):
            with self.subTest(message=message):
                reader, transport = self.reader([kraken_error(message)])
                with self.assertRaises(KrakenReadError) as caught:
                    reader.balance()
                self.assertIs(caught.exception.code, ReadErrorCode.SERVICE_UNAVAILABLE)
                self.assertIsNone(reader.locked_by)

    def test_other_and_mixed_errors(self):
        reader, _ = self.reader(
            [
                kraken_error("EGeneral:Invalid arguments"),
                kraken_error("EService:Busy", "EAPI:Invalid key"),
            ]
        )
        with self.assertRaises(KrakenReadError) as caught:
            reader.balance()
        self.assertIs(caught.exception.code, ReadErrorCode.EXCHANGE_ERROR)
        with self.assertRaises(KrakenReadError) as caught:
            reader.balance()
        self.assertIs(caught.exception.code, ReadErrorCode.INVALID_KEY)
        self.assertIs(reader.locked_by, ReadErrorCode.INVALID_KEY)

    def test_warnings_alone_do_not_fail(self):
        body = json.dumps({"error": ["WGeneral:Something"], "result": {"ZEUR": "1.5"}}).encode()
        reader, _ = self.reader([TransportReply(200, body)])
        self.assertEqual(reader.balance().amount("ZEUR"), Decimal("1.5"))

    def test_http_and_body_failures(self):
        cases = [
            (TransportReply(500, b'{"error":[]}'), ReadErrorCode.HTTP_STATUS),
            (TransportReply(404, b""), ReadErrorCode.HTTP_STATUS),
            (TransportReply(429, b""), ReadErrorCode.HTTP_STATUS),
            (TransportReply(302, b""), ReadErrorCode.REDIRECT_REFUSED),
            (TransportReply(200, b"", redirected=True), ReadErrorCode.REDIRECT_REFUSED),
            (TransportReply(200, b"<html>maintenance</html>"), ReadErrorCode.NOT_JSON),
            (TransportReply(200, b"\xff\xfe"), ReadErrorCode.NOT_JSON),
            (TransportReply(200, b""), ReadErrorCode.NOT_JSON),
            (TransportReply(200, b"[]"), ReadErrorCode.MALFORMED_RESPONSE),
            (TransportReply(200, b'{"result": {}}'), ReadErrorCode.MALFORMED_RESPONSE),
            (TransportReply(200, b'{"error": "EAPI:Invalid key"}'), ReadErrorCode.MALFORMED_RESPONSE),
        ]
        for reply, code in cases:
            with self.subTest(reply=reply):
                reader, transport = self.reader([reply])
                with self.assertRaises(KrakenReadError) as caught:
                    reader.balance()
                self.assertIs(caught.exception.code, code)
                self.assertEqual(caught.exception.endpoint, "Balance")
                self.assertEqual(len(transport.calls), 1)
                self.assertIsNone(reader.locked_by)

    def test_transport_exceptions_are_typed_and_carry_no_text(self):
        cases = [
            (TransportFailure(TransportFailureKind.TIMEOUT), ReadErrorCode.TIMEOUT),
            (TransportFailure(TransportFailureKind.CONNECTION), ReadErrorCode.TRANSPORT_FAILED),
            (TransportFailure(TransportFailureKind.TOO_LARGE), ReadErrorCode.TOO_LARGE),
            (TransportFailure(TransportFailureKind.REFUSED), ReadErrorCode.TRANSPORT_FAILED),
            (requests.ConnectionError(f"leak {SYNTHETIC_SECRET}"), ReadErrorCode.TRANSPORT_FAILED),
            (RuntimeError(f"leak {SYNTHETIC_KEY}"), ReadErrorCode.TRANSPORT_FAILED),
        ]
        for failure, code in cases:
            with self.subTest(failure=type(failure).__name__):
                reader, _ = self.reader([failure])
                with self.assertRaises(KrakenReadError) as caught:
                    reader.balance()
                error = caught.exception
                self.assertIs(error.code, code)
                self.assertEqual(str(error), f"{code.value} Balance")
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)


# --------------------------------------------------------------------------- parsing

ORDER_OPEN = {
    "refid": None,
    "userref": 0,
    "cl_ord_id": "synthetic-1",
    "status": "open",
    "opentm": 1759233600.1234,
    "starttm": 0,
    "expiretm": 0,
    "descr": {
        "pair": "XBTEUR",
        "type": "buy",
        "ordertype": "limit",
        "price": "50000.0",
        "price2": "0",
        "leverage": "none",
        "order": "buy 0.01000000 XBTEUR @ limit 50000.0",
        "close": "",
    },
    "vol": "0.01000000",
    "vol_exec": "0.00250000",
    "cost": "125.00000",
    "fee": "0.32500",
    "price": "50000.0",
    "stopprice": "0.00000",
    "limitprice": "0.00000",
    "misc": "",
    "oflags": "fciq",
}
ORDER_CLOSED = {
    **ORDER_OPEN,
    "status": "closed",
    "userref": None,
    "vol_exec": "0.01000000",
    "cost": "500.00000",
    "fee": "1.30000",
    "closetm": 1759237200,
    "reason": None,
    "trades": ["TSYNTH-AAAAA-BBBBBB"],
}
ORDER_CLOSED.pop("cl_ord_id")

FIXTURES = {
    "Balance": {"ZEUR": "240.1234", "XXBT": "0.0000000000", "USDC": "10.00000000"},
    "TradeBalance": {
        "eb": "240.1234",
        "tb": "240.1234",
        "m": "0.0000",
        "n": "0.0000",
        "c": "0.0000",
        "v": "0.0000",
        "e": "240.1234",
        "mf": "240.1234",
        "uv": "0.0000",
    },
    "TradeVolume": {
        "currency": "ZUSD",
        "volume": "1234.5678",
        "fees": {
            "XXBTZEUR": {
                "fee": "0.4000",
                "minfee": "0.1000",
                "maxfee": "0.4000",
                "nextfee": "0.3500",
                "nextvolume": "10000.0000",
                "tiervolume": "0.0000",
            }
        },
        "fees_maker": {
            "XXBTZEUR": {
                "fee": "0.2500",
                "minfee": "0.0000",
                "maxfee": "0.2500",
                "nextfee": "0.2000",
                "nextvolume": "10000.0000",
                "tiervolume": "0.0000",
            }
        },
    },
    "OpenOrders": {"open": {"OSYNTH-AAAAA-BBBBBB": ORDER_OPEN}},
    "ClosedOrders": {"closed": {"OSYNTH-CCCCC-DDDDDD": ORDER_CLOSED}, "count": 1},
    "QueryOrders": {"OSYNTH-AAAAA-BBBBBB": ORDER_OPEN, "OSYNTH-CCCCC-DDDDDD": ORDER_CLOSED},
}


class TestParsing(StateDirCase):
    def test_balance(self):
        reader, _ = self.reader([ok(FIXTURES["Balance"])])
        balances = reader.balance()
        self.assertIsInstance(balances, Balances)
        self.assertEqual(balances.amount("ZEUR"), Decimal("240.1234"))
        self.assertEqual(balances.amount("XXBT"), Decimal("0E-10"))
        self.assertIsNone(balances.amount("ZUSD"))
        self.assertTrue(all(isinstance(item.amount, Decimal) for item in balances.balances))

    def test_trade_balance(self):
        reader, transport = self.reader([ok(FIXTURES["TradeBalance"])])
        result = reader.trade_balance("ZEUR")
        self.assertIsInstance(result, TradeBalance)
        self.assertEqual(result.equivalent_balance, Decimal("240.1234"))
        self.assertEqual(result.equity, Decimal("240.1234"))
        self.assertIsNone(result.margin_level)  # absent is None, never zero
        self.assertIn(b"asset=ZEUR", transport.calls[0]["body"])

    def test_trade_volume_is_the_real_fee_tier(self):
        reader, transport = self.reader([ok(FIXTURES["TradeVolume"])])
        tier = reader.trade_volume("XXBTZEUR")
        self.assertIsInstance(tier, FeeTier)
        self.assertEqual(tier.currency, "ZUSD")
        self.assertEqual(tier.volume_30d, Decimal("1234.5678"))
        fee = tier.pair_fee("XXBTZEUR")
        self.assertEqual((fee.taker_fee, fee.maker_fee), (Decimal("0.4000"), Decimal("0.2500")))
        self.assertEqual(fee.taker_next_fee, Decimal("0.3500"))
        self.assertIn(b"pair=XXBTZEUR", transport.calls[0]["body"])

    def test_trade_volume_without_pair_has_no_fees_and_maker_is_optional(self):
        document = {**FIXTURES["TradeVolume"]}
        document.pop("fees_maker")
        reader, _ = self.reader([ok({"currency": "ZUSD", "volume": "0.0000"}), ok(document)])
        self.assertEqual(reader.trade_volume().pairs, ())
        self.assertIsNone(reader.trade_volume("XXBTZEUR").pair_fee("XXBTZEUR").maker_fee)

    def test_open_orders(self):
        reader, _ = self.reader([ok(FIXTURES["OpenOrders"])])
        (order,) = reader.open_orders().orders
        self.assertEqual(order.txid, "OSYNTH-AAAAA-BBBBBB")
        self.assertEqual(
            (order.status, order.pair, order.side, order.order_type), ("open", "XBTEUR", "buy", "limit")
        )
        self.assertEqual(order.price, Decimal("50000.0"))
        self.assertEqual(order.volume, Decimal("0.01000000"))
        self.assertEqual(order.executed_volume, Decimal("0.00250000"))
        self.assertEqual(order.cost, Decimal("125.00000"))
        self.assertEqual(order.fee, Decimal("0.32500"))
        self.assertEqual(order.opened_at, datetime(2025, 9, 30, 12, 0, 0, 123400, tzinfo=UTC))
        self.assertIs(order.opened_at.tzinfo, UTC)
        self.assertIsNone(order.closed_at)
        self.assertEqual(order.cl_ord_id, "synthetic-1")

    def test_closed_orders(self):
        reader, transport = self.reader([ok(FIXTURES["ClosedOrders"])])
        page = reader.closed_orders(start=1759000000, closetime="close", ofs=0)
        self.assertIsInstance(page, ClosedOrders)
        self.assertEqual(page.count, 1)
        (order,) = page.orders
        self.assertEqual(order.closed_at, datetime(2025, 9, 30, 13, 0, 0, tzinfo=UTC))
        self.assertEqual(order.trade_ids, ("TSYNTH-AAAAA-BBBBBB",))
        self.assertIsNone(order.userref)
        self.assertEqual(
            transport.calls[0]["body"].decode(),
            f"nonce={T0_MS}&closetime=close&ofs=0&start=1759000000&trades=false",
        )

    def test_query_orders(self):
        reader, transport = self.reader([ok(FIXTURES["QueryOrders"])])
        result = reader.query_orders(["OQCLML-BW3P3-BUCMWZ"], trades=True)
        self.assertIsInstance(result, QueriedOrders)
        self.assertEqual([order.status for order in result.orders], ["open", "closed"])
        self.assertIn(b"txid=OQCLML-BW3P3-BUCMWZ", transport.calls[0]["body"])

    def test_results_are_immutable(self):
        reader, _ = self.reader([ok(FIXTURES["OpenOrders"])])
        orders = reader.open_orders()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            orders.orders[0].price = Decimal(1)  # type: ignore[misc]
        self.assertIsInstance(orders.orders, tuple)
        self.assertIsInstance(orders, OpenOrders)

    def assert_malformed(self, endpoint: str, raw_result: str, parameters=None):
        body = ('{"error": [], "result": ' + raw_result + "}").encode()
        reader, _ = self.reader([TransportReply(200, body)])
        with self.assertRaises(KrakenReadError) as caught:
            reader.read(endpoint, parameters)
        self.assertIs(caught.exception.code, ReadErrorCode.MALFORMED_RESPONSE)
        self.assertEqual(str(caught.exception), f"MALFORMED_RESPONSE {endpoint}")

    def test_money_fields_must_be_decimal_strings(self):
        for value in ("240.5", "true", "null", "NaN", "Infinity", "-Infinity", '"NaN"', '"Infinity"', '"1e5"',
                      '"1,5"', '""', '" 1.0"', "[]", "{}"):
            with self.subTest(value=value):
                self.assert_malformed("Balance", '{"ZEUR": ' + value + "}")

    def test_order_fields_are_strictly_parsed(self):
        def order_with(**changes):
            document = json.loads(json.dumps(ORDER_OPEN))
            for key, value in changes.items():
                if key.startswith("descr_"):
                    document["descr"][key[6:]] = value
                else:
                    document[key] = value
            return json.dumps({"open": {"OSYNTH-AAAAA-BBBBBB": document}})

        cases = {
            "float volume": order_with(vol=0.01),
            "bool fee": order_with(fee=True),
            "unknown side": order_with(descr_type="short"),
            "unhashable status": order_with(status=[]),
            "unknown status": order_with(status="filled"),
            "no opentm": order_with(opentm=None),
            "string opentm": order_with(opentm="1759233600"),
            "negative opentm": order_with(opentm=-1),
            "bool userref": order_with(userref=True),
            "bool closetm": order_with(closetm=False),
            "no descr": order_with(descr=None),
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                self.assert_malformed("OpenOrders", raw)
        self.assert_malformed("OpenOrders", '{"open": {"OSYNTH-AAAAA-BBBBBB": {"opentm": NaN}}}')

    def test_other_shapes(self):
        self.assert_malformed("TradeBalance", '{"tb": "1.0"}', {"asset": "ZEUR"})
        self.assert_malformed("TradeVolume", '{"currency": "ZUSD", "volume": 5.5}')
        self.assert_malformed("TradeVolume", '{"currency": "ZUSD", "volume": "1", "fees_maker": {"X": {"fee": "1"}}}')
        self.assert_malformed("ClosedOrders", '{"closed": {}, "count": true}')
        self.assert_malformed("ClosedOrders", '{"closed": {}}')
        self.assert_malformed("OpenOrders", '{"open": []}')
        self.assert_malformed("Balance", '{"ZEUR": "1.0", "ZEUR": "2.0"}')
        self.assert_malformed("Balance", "null")


# --------------------------------------------------------------------------- real requests transport


class RecordingAdapter(BaseAdapter):
    """requests transport adapter fake: never opens a socket."""

    def __init__(self, results):
        super().__init__()
        self.results = list(results)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append({"method": request.method, "url": request.url, "body": request.body,
                          "headers": dict(request.headers), **kwargs})
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        status, headers, body = result
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response.raw = io.BytesIO(body)
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


def session_with(results):
    adapter = RecordingAdapter(results)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session, adapter


class TestRequestsTransport(unittest.TestCase):
    def test_posts_once_without_redirects_proxies_or_environment(self):
        session, adapter = session_with([(200, {}, b'{"error":[],"result":{}}')])
        transport = RequestsTransport(session)
        self.assertFalse(session.trust_env)
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:8080"}):
            reply = transport.post(ReadEndpoint.BALANCE.url, b"nonce=1", {"API-Key": "k"}, 15.0)
        self.assertEqual(reply.status, 200)
        (sent,) = adapter.sent
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["url"], "https://api.kraken.com/0/private/Balance")
        self.assertEqual(sent["body"], b"nonce=1")
        self.assertEqual(sent["timeout"], (5.0, 15.0))
        self.assertFalse(sent["proxies"])

    def test_redirect_is_reported_and_never_followed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                session, adapter = session_with([(status, {"Location": "https://evil.test/0/private/AddOrder"}, b"")])
                reply = RequestsTransport(session).post(ReadEndpoint.BALANCE.url, b"nonce=1", {}, 15.0)
                self.assertTrue(reply.redirected)
                self.assertEqual(len(adapter.sent), 1)

    def test_urls_outside_the_private_read_prefix_are_refused_before_sending(self):
        refused = [
            "https://api.kraken.com/0/private/AddOrder",
            "https://api.kraken.com/0/private/balance",
            "https://api.kraken.com/0/private/Balance/../AddOrder",
            "https://api.kraken.com/0/private/Balance?x=1",
            "https://api.kraken.com/0/public/Time",
            "http://api.kraken.com/0/private/Balance",
            "https://api.kraken.com.evil.test/0/private/Balance",
            "https://api.kraken.com:443/0/private/Balance",
            "https://user@api.kraken.com/0/private/Balance",
            "https://futures.kraken.com/0/private/Balance",
        ]
        session, adapter = session_with([])
        transport = RequestsTransport(session)
        for url in refused:
            with self.subTest(url=url):
                with self.assertRaises(TransportFailure) as caught:
                    transport.post(url, b"nonce=1", {}, 15.0)
                self.assertIs(caught.exception.kind, TransportFailureKind.REFUSED)
        for timeout in (0, -1, 60.0):
            with self.assertRaises(TransportFailure):
                transport.post(ReadEndpoint.BALANCE.url, b"nonce=1", {}, timeout)
        self.assertEqual(adapter.sent, [])

    def test_failures_are_typed_without_context(self):
        cases = [
            (requests.ConnectTimeout(f"x {SYNTHETIC_SECRET}"), TransportFailureKind.TIMEOUT),
            (requests.ReadTimeout("slow"), TransportFailureKind.TIMEOUT),
            (requests.ConnectionError("down"), TransportFailureKind.CONNECTION),
            ((200, {}, b"x" * (kpr.MAX_RESPONSE_BYTES + 10)), TransportFailureKind.TOO_LARGE),
        ]
        for result, kind in cases:
            with self.subTest(kind=kind):
                session, _ = session_with([result])
                with self.assertRaises(TransportFailure) as caught:
                    RequestsTransport(session).post(ReadEndpoint.BALANCE.url, b"nonce=1", {}, 15.0)
                self.assertIs(caught.exception.kind, kind)
                self.assertIsNone(caught.exception.__context__)

    def test_default_factory_builds_a_requests_transport_without_sending(self):
        transport = RequestsTransport()
        self.assertFalse(transport._session.trust_env)
        transport.close()


# --------------------------------------------------------------------------- redaction


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class TestRedaction(StateDirCase):
    SCENARIOS = {
        "success": [ok(FIXTURES["Balance"])],
        "invalid_key": [kraken_error("EAPI:Invalid key")],
        "invalid_signature": [kraken_error("EAPI:Invalid signature")],
        "invalid_nonce": [kraken_error("EAPI:Invalid nonce")],
        "permission": [kraken_error("EGeneral:Permission denied")],
        "rate_limit": [kraken_error("EAPI:Rate limit exceeded")],
        "service": [kraken_error("EService:Unavailable")],
        "other": [kraken_error("EGeneral:Invalid arguments")],
        "http": [TransportReply(503, b"")],
        "redirect": [TransportReply(302, b"")],
        "not_json": [TransportReply(200, b"<html>")],
        "malformed": [ok({"ZEUR": 1.5})],
        "timeout": [TransportFailure(TransportFailureKind.TIMEOUT)],
        "leaky_exception": [RuntimeError(f"{SYNTHETIC_KEY} {SYNTHETIC_SECRET}")],
    }

    def forbidden_texts(self, transport: FakeTransport) -> list[str]:
        texts = [
            SYNTHETIC_KEY,
            SYNTHETIC_SECRET,
            repr(SYNTHETIC_SECRET_BYTES),
            SYNTHETIC_SECRET_BYTES.hex(),
            SYNTHETIC_SECRET_BYTES.decode("latin-1"),
        ]
        for call in transport.calls:
            texts.append(call["headers"]["API-Sign"])
            texts.append(call["body"].decode("ascii"))
        return texts

    def test_nothing_sensitive_reaches_errors_results_or_logs(self):
        handler = ListHandler()
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, previous_level)
        for label, replies in self.SCENARIOS.items():
            with self.subTest(label=label):
                self.nonce_path.unlink(missing_ok=True)
                reader, transport = self.reader(list(replies) + [ok({})])
                outputs = [repr(reader), str(reader)]
                try:
                    outputs.append(repr(reader.balance()))
                except KrakenReadError as error:
                    outputs.extend([str(error), repr(error), repr(error.args), str(error.reason)])
                    outputs.append(repr(error.__context__))
                    outputs.append(repr(error.__cause__))
                try:
                    reader.balance()
                except KrakenReadError as error:
                    outputs.extend([str(error), repr(error)])
                outputs.extend(handler.messages)
                outputs.append(repr(reader))
                blob = "\n".join(outputs)
                for text in self.forbidden_texts(transport):
                    self.assertNotIn(text, blob)
                self.assertGreaterEqual(len(transport.calls), 1)

    def test_unavailable_results_carry_only_a_reason(self):
        for environ in (
            {"KRAKEN_API_KEY": SYNTHETIC_KEY},
            {"KRAKEN_API_KEY": SYNTHETIC_KEY, "KRAKEN_API_SECRET": "!" + SYNTHETIC_SECRET},
            {"KRAKEN_API_KEY": SYNTHETIC_KEY + "\n", "KRAKEN_API_SECRET": SYNTHETIC_SECRET},
        ):
            result = open_private_reader(
                TradingMode.SHADOW_LIVE, environ=environ, state_dir=self.state, transport_factory=FakeTransport
            )
            self.assertIsInstance(result, Unavailable)
            self.assertNotIn(SYNTHETIC_KEY, repr(result))
            self.assertNotIn(SYNTHETIC_SECRET, repr(result))

    def test_transport_reply_repr_hides_the_body(self):
        reply = TransportReply(200, SYNTHETIC_SECRET.encode())
        self.assertNotIn(SYNTHETIC_SECRET, repr(reply))


class TestFileNames(unittest.TestCase):
    def test_new_files_avoid_gitignored_words(self):
        root = Path(__file__).resolve().parent.parent
        for relative in (
            "radar_v08/adapters/kraken_private_read.py",
            "radar_v08/domain/trading_mode.py",
            "tests/test_kraken_private_read.py",
            "tests/test_trading_mode.py",
        ):
            with self.subTest(relative=relative):
                self.assertTrue((root / relative).is_file())
                self.assertNotIn("secret", relative.lower())
                self.assertNotIn("token", relative.lower())


if __name__ == "__main__":
    unittest.main()
