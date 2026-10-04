"""Kraken spot private REST adapter, READ ONLY (F3a; docs/FUTURE_TRADING_ROADMAP.md F3).

Six account reads and nothing else: Balance, TradeBalance, TradeVolume, OpenOrders,
ClosedOrders and QueryOrders, each a ``POST https://api.kraken.com/0/private/<name>``.
There is no code path to any write endpoint (AddOrder, EditOrder, AmendOrder, Cancel*,
Withdraw*, WalletTransfer, DepositAddresses, GetWebSocketsToken, ...). The radar loop,
radar.py and the UI never import this module.

Construction gate
-----------------

The only supported way to obtain a reader is ``open_private_reader`` with an explicit
``TradingMode``. ANALYSIS_ONLY, RETROSPECTIVE and PAPER get a ``ModeRefused`` before any
credential is read, any nonce file is touched or any transport is created. SHADOW_LIVE and
above build a ``KrakenPrivateReader``; the reader itself refuses a lower mode too.

Credentials
-----------

From the injected ``environ``: ``KRAKEN_API_KEY`` and ``KRAKEN_API_SECRET``, both. When
neither is present, from ``<state dir>/.kraken/credentials.json`` (exactly
``{"api_key": ..., "api_secret": ...}``, UTF-8 with or without a byte order mark). The
two sources are never mixed: one variable alone is ``credentials_incomplete``. The secret must be strict base64. Every problem is an
``Unavailable`` with a reason code, and then no nonce file is created and no transport is
built. ``KrakenCredentials`` never shows the key or the secret in ``repr``/``str``.

Signing (Kraken spot REST authentication)
-----------------------------------------

``API-Sign = base64(HMAC-SHA512(base64-decoded secret, uri_path + SHA256(nonce + postdata)))``
with headers ``API-Key`` and ``API-Sign`` and a form-urlencoded body that starts with
``nonce=``. ``api_sign`` is pure and reproduces the documented vector.

Nonce
-----

``NonceStore`` keeps the last nonce in ``<state dir>/.kraken/nonce``. The next value is
``max(last + 1, now in epoch milliseconds)`` and it is written atomically (temporary file,
fsync, replace) BEFORE the request is sent, so a crash never reuses a nonce. A corrupt or
unreadable file is ``UNAVAILABLE``, never a reset to a lower value. One process at a time
per state directory: two processes sharing the directory could interleave.

Requests and errors
-------------------

``read`` is the single choke point: the endpoint name must be exactly one of the six and
every parameter must be on that endpoint's allowlist with a well-formed value; otherwise a
``KrakenReadError`` (ENDPOINT_REFUSED / PARAMETER_REFUSED) is raised before a nonce is
reserved, a signature is computed or the transport is called. The URL is built from the
allowlisted endpoint, never from caller text. ``RequestsTransport`` posts only to that
exact host and path prefix, ignores the environment (proxies, .netrc), never follows a
redirect, bounds the time and caps the body.

Kraken errors: ``EAPI:Invalid key``, ``EAPI:Invalid signature``, ``EAPI:Invalid nonce`` and
``EGeneral:Permission denied`` lock the reader instance (every later call raises LOCKED
without any network). Rate limits are RATE_LIMITED, never retried here. ``EService``
Unavailable/Busy is SERVICE_UNAVAILABLE. HTTP non-200, 3xx, non-JSON and transport
failures are typed codes. An error carries only its code, the endpoint name and a fixed
reason code: never the key, the secret, the signature, the request body or response text.
Nothing here logs.

Results are immutable dataclasses. Money, quantity and fee fields are ``Decimal`` parsed
from JSON strings only (a JSON number, bool, NaN or Infinity there is MALFORMED_RESPONSE);
order times are aware UTC datetimes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

import requests

from ..domain.trading_mode import TradingMode, allows_private_reads
from .clock import SystemClock

API_HOST = "api.kraken.com"
PRIVATE_PATH_PREFIX = "/0/private/"
PRIVATE_URL_PREFIX = f"https://{API_HOST}{PRIVATE_PATH_PREFIX}"
CONNECT_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CREDENTIAL_FILE_BYTES = 4096
MAX_QUERY_TXIDS = 50
_CHUNK_BYTES = 64 * 1024

ENV_API_KEY = "KRAKEN_API_KEY"
ENV_API_SECRET = "KRAKEN_API_SECRET"
KRAKEN_DIRECTORY_NAME = ".kraken"
CREDENTIALS_FILE_NAME = "credentials.json"
NONCE_FILE_NAME = "nonce"
_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent


class ReadEndpoint(Enum):
    """The complete allowlist. Adding a member is a reviewed decision, never a parameter."""

    BALANCE = "Balance"
    TRADE_BALANCE = "TradeBalance"
    TRADE_VOLUME = "TradeVolume"
    OPEN_ORDERS = "OpenOrders"
    CLOSED_ORDERS = "ClosedOrders"
    QUERY_ORDERS = "QueryOrders"

    @property
    def uri_path(self) -> str:
        return PRIVATE_PATH_PREFIX + self.value

    @property
    def url(self) -> str:
        return PRIVATE_URL_PREFIX + self.value


_ENDPOINTS_BY_NAME: dict[str, ReadEndpoint] = {endpoint.value: endpoint for endpoint in ReadEndpoint}


class ReadErrorCode(Enum):
    MODE_REFUSED = "MODE_REFUSED"
    UNAVAILABLE = "UNAVAILABLE"
    ENDPOINT_REFUSED = "ENDPOINT_REFUSED"
    PARAMETER_REFUSED = "PARAMETER_REFUSED"
    INVALID_KEY = "INVALID_KEY"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    INVALID_NONCE = "INVALID_NONCE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    LOCKED = "LOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    EXCHANGE_ERROR = "EXCHANGE_ERROR"
    HTTP_STATUS = "HTTP_STATUS"
    REDIRECT_REFUSED = "REDIRECT_REFUSED"
    NOT_JSON = "NOT_JSON"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    TOO_LARGE = "TOO_LARGE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"


#: Errors that prove the credentials, the signature or the nonce are wrong: the reader locks.
AUTH_ERROR_CODES = frozenset(
    {
        ReadErrorCode.INVALID_KEY,
        ReadErrorCode.INVALID_SIGNATURE,
        ReadErrorCode.INVALID_NONCE,
        ReadErrorCode.PERMISSION_DENIED,
    }
)


class UnavailableReason(Enum):
    CREDENTIALS_MISSING = "credentials_missing"
    CREDENTIALS_INCOMPLETE = "credentials_incomplete"
    CREDENTIALS_EMPTY = "credentials_empty"
    CREDENTIALS_MALFORMED = "credentials_malformed"
    CREDENTIALS_UNREADABLE = "credentials_unreadable"
    API_KEY_MALFORMED = "api_key_malformed"
    SECRET_NOT_BASE64 = "api_secret_not_base64"
    NONCE_STORE_CORRUPT = "nonce_store_corrupt"
    NONCE_STORE_UNREADABLE = "nonce_store_unreadable"
    NONCE_STORE_UNWRITABLE = "nonce_store_unwritable"
    CLOCK_INVALID = "clock_invalid"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"


class KrakenReadError(Exception):
    """A typed failure: a code, the endpoint name (when known) and a fixed reason code only."""

    def __init__(
        self,
        code: ReadErrorCode,
        endpoint: str | None = None,
        reason: str | None = None,
        *,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.endpoint = endpoint
        self.reason = reason
        self.http_status = http_status
        super().__init__(" ".join(part for part in (code.value, endpoint, reason) if part))

    def __repr__(self) -> str:
        return f"KrakenReadError({str(self)!r})"


@dataclass(frozen=True)
class ModeRefused:
    """The mode may not construct a private adapter; nothing else was touched."""

    mode: TradingMode | None
    reason: str = "mode_below_shadow_live"


@dataclass(frozen=True)
class Unavailable:
    """The reader cannot run; only a reason code, never a credential value."""

    reason: UnavailableReason


# --------------------------------------------------------------------------- credentials


class KrakenCredentials:
    """API key plus the decoded secret. Never printable: repr/str are fixed text."""

    __slots__ = ("_api_key", "_secret")

    def __init__(self, api_key: str, secret: bytes) -> None:
        self._api_key = api_key
        self._secret = secret

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def secret(self) -> bytes:
        return self._secret

    def __repr__(self) -> str:
        return "KrakenCredentials(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("KrakenCredentials cannot be pickled")


_API_KEY_PATTERN = re.compile(r"[A-Za-z0-9+/=_-]{1,256}")


def _credentials_from_values(api_key: object, api_secret: object) -> KrakenCredentials | Unavailable:
    if not isinstance(api_key, str) or not isinstance(api_secret, str):
        return Unavailable(UnavailableReason.CREDENTIALS_MALFORMED)
    if not api_key or not api_secret:
        return Unavailable(UnavailableReason.CREDENTIALS_EMPTY)
    if _API_KEY_PATTERN.fullmatch(api_key) is None:
        return Unavailable(UnavailableReason.API_KEY_MALFORMED)
    try:
        secret = base64.b64decode(api_secret.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        secret = b""
    if not secret:
        return Unavailable(UnavailableReason.SECRET_NOT_BASE64)
    return KrakenCredentials(api_key, secret)


def kraken_directory(state_dir: str | os.PathLike[str]) -> Path:
    """``<state dir>/.kraken``: credentials file and nonce store."""
    return Path(state_dir) / KRAKEN_DIRECTORY_NAME


def default_state_dir(environ: Mapping[str, str]) -> Path:
    """``RADAR_STATE_DIR`` from the injected environment, else the repository root (as config.py)."""
    configured = environ.get("RADAR_STATE_DIR")
    return Path(configured) if configured else _REPOSITORY_ROOT


def _reject_constant(_name: str) -> object:
    raise ValueError("non-finite JSON constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def load_credentials(
    environ: Mapping[str, str], state_dir: str | os.PathLike[str]
) -> KrakenCredentials | Unavailable:
    """Environment (both variables) or else the credentials file; never a mix of the two."""
    has_key = ENV_API_KEY in environ
    has_secret = ENV_API_SECRET in environ
    if has_key or has_secret:
        if not (has_key and has_secret):
            return Unavailable(UnavailableReason.CREDENTIALS_INCOMPLETE)
        return _credentials_from_values(environ[ENV_API_KEY], environ[ENV_API_SECRET])
    path = kraken_directory(state_dir) / CREDENTIALS_FILE_NAME
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CREDENTIAL_FILE_BYTES + 1)
    except FileNotFoundError:
        return Unavailable(UnavailableReason.CREDENTIALS_MISSING)
    except OSError:
        return Unavailable(UnavailableReason.CREDENTIALS_UNREADABLE)
    if len(raw) > MAX_CREDENTIAL_FILE_BYTES:
        return Unavailable(UnavailableReason.CREDENTIALS_MALFORMED)
    document: object = None
    malformed = False
    try:
        # utf-8-sig: Windows Notepad may save the file with a byte order mark.
        document = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        malformed = True
    if malformed or not isinstance(document, dict) or set(document) != {"api_key", "api_secret"}:
        if isinstance(document, dict) and set(document) < {"api_key", "api_secret"}:
            return Unavailable(UnavailableReason.CREDENTIALS_INCOMPLETE)
        return Unavailable(UnavailableReason.CREDENTIALS_MALFORMED)
    return _credentials_from_values(document["api_key"], document["api_secret"])


# --------------------------------------------------------------------------- signing


def api_sign(uri_path: str, nonce: str, postdata: str, secret: bytes) -> str:
    """Kraken's API-Sign: base64(HMAC-SHA512(secret, uri_path + SHA256(nonce + postdata)))."""
    digest = hashlib.sha256((nonce + postdata).encode("utf-8")).digest()
    mac = hmac.new(secret, uri_path.encode("utf-8") + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode("ascii")


# --------------------------------------------------------------------------- nonce store


class WallClock(Protocol):
    def current(self) -> datetime: ...


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NONCE_TEXT = re.compile(r"[0-9]{1,19}\n?")
_MAX_NONCE = 2**63 - 1


class NonceStoreError(Exception):
    def __init__(self, reason: UnavailableReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class NonceStore:
    """Persisted monotonic nonce under ``<state dir>/.kraken/nonce`` (single process)."""

    def __init__(self, state_dir: str | os.PathLike[str], clock: WallClock) -> None:
        self._directory = kraken_directory(state_dir)
        self._path = self._directory / NONCE_FILE_NAME
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def last(self) -> int | None:
        """The persisted value, ``None`` when there is no file; corrupt/unreadable raises."""
        try:
            text = self._path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise NonceStoreError(UnavailableReason.NONCE_STORE_UNREADABLE) from None
        try:
            decoded = text.decode("ascii")
        except UnicodeDecodeError:
            decoded = ""
        if _NONCE_TEXT.fullmatch(decoded) is None:
            raise NonceStoreError(UnavailableReason.NONCE_STORE_CORRUPT)
        value = int(decoded)
        if value > _MAX_NONCE:
            raise NonceStoreError(UnavailableReason.NONCE_STORE_CORRUPT)
        return value

    def _now_ms(self) -> int:
        now = self._clock.current()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise NonceStoreError(UnavailableReason.CLOCK_INVALID)
        return (now - _EPOCH) // timedelta(milliseconds=1)

    def reserve(self) -> int:
        """Persist and return ``max(last + 1, now_ms)``; the file is on disk before returning."""
        with self._lock:
            last = self.last()
            value = max((last if last is not None else 0) + 1, self._now_ms())
            if value > _MAX_NONCE:
                raise NonceStoreError(UnavailableReason.NONCE_STORE_CORRUPT)
            self._write(value)
            return value

    def _write(self, value: int) -> None:
        temporary: str | None = None
        failed = False
        try:
            self._directory.mkdir(exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix="nonce.", suffix=".tmp", dir=self._directory)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(f"{value}\n".encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            temporary = None
        except OSError:
            failed = True
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        if failed:
            raise NonceStoreError(UnavailableReason.NONCE_STORE_UNWRITABLE)


# --------------------------------------------------------------------------- transport


class TransportFailureKind(Enum):
    TIMEOUT = "TIMEOUT"
    CONNECTION = "CONNECTION"
    TOO_LARGE = "TOO_LARGE"
    REFUSED = "REFUSED"


class TransportFailure(Exception):
    def __init__(self, kind: TransportFailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


@dataclass(frozen=True)
class TransportReply:
    status: int
    body: bytes
    redirected: bool = False

    def __repr__(self) -> str:
        return f"TransportReply(status={self.status}, bytes={len(self.body)}, redirected={self.redirected})"


class PrivateTransport(Protocol):
    def post(self, url: str, body: bytes, headers: Mapping[str, str], timeout_seconds: float) -> TransportReply: ...

    def close(self) -> None: ...


class RequestsTransport:
    """POST to the exact Kraken private prefix; no environment, no redirects, bounded."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False

    def close(self) -> None:
        self._session.close()

    def post(self, url: str, body: bytes, headers: Mapping[str, str], timeout_seconds: float) -> TransportReply:
        if (
            type(url) is not str
            or not url.startswith(PRIVATE_URL_PREFIX)
            or url.removeprefix(PRIVATE_URL_PREFIX) not in _ENDPOINTS_BY_NAME
        ):
            raise TransportFailure(TransportFailureKind.REFUSED)
        if not 0 < timeout_seconds <= REQUEST_TIMEOUT_SECONDS:
            raise TransportFailure(TransportFailureKind.REFUSED)
        failure: TransportFailureKind | None = None
        reply: TransportReply | None = None
        try:
            response = self._session.post(
                url,
                data=body,
                headers=dict(headers),
                timeout=(min(CONNECT_TIMEOUT_SECONDS, timeout_seconds), timeout_seconds),
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout:
            failure = TransportFailureKind.TIMEOUT
        except requests.RequestException:
            failure = TransportFailureKind.CONNECTION
        else:
            try:
                reply, failure = self._read(response)
            finally:
                response.close()
        if failure is not None:
            raise TransportFailure(failure)
        assert reply is not None
        return reply

    @staticmethod
    def _read(response: requests.Response) -> tuple[TransportReply | None, TransportFailureKind | None]:
        status = int(response.status_code)
        if 300 <= status < 400 or response.history:
            return TransportReply(status, b"", redirected=True), None
        received = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                received.extend(chunk)
                if len(received) > MAX_RESPONSE_BYTES:
                    return None, TransportFailureKind.TOO_LARGE
        except requests.Timeout:
            return None, TransportFailureKind.TIMEOUT
        except requests.RequestException:
            return None, TransportFailureKind.CONNECTION
        return TransportReply(status, bytes(received)), None


# --------------------------------------------------------------------------- parameters

_ASSET = re.compile(r"[A-Za-z0-9.]{1,16}")
_PAIR_LIST = re.compile(r"[A-Za-z0-9./]{2,32}(,[A-Za-z0-9./]{2,32}){0,19}")
_CL_ORD_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_TXID = re.compile(r"[A-Z0-9]{6}-[A-Z0-9]{5}-[A-Z0-9]{6}")
_CLOSETIME = frozenset({"open", "close", "both"})
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1


def _param_bool(value: object) -> str | None:
    if type(value) is bool:
        return "true" if value else "false"
    return None


def _param_userref(value: object) -> str | None:
    if type(value) is int and _INT32_MIN <= value <= _INT32_MAX:
        return str(value)
    return None


def _param_pattern(pattern: re.Pattern[str]) -> Callable[[object], str | None]:
    def validate(value: object) -> str | None:
        if type(value) is str and pattern.fullmatch(value) is not None:
            return value
        return None

    return validate


def _param_time(value: object) -> str | None:
    if type(value) is int and 0 <= value <= 10**11:
        return str(value)
    if type(value) is str and _TXID.fullmatch(value) is not None:
        return value
    return None


def _param_offset(value: object) -> str | None:
    if type(value) is int and 0 <= value <= 10**9:
        return str(value)
    return None


def _param_closetime(value: object) -> str | None:
    if type(value) is str and value in _CLOSETIME:
        return value
    return None


def _param_txids(value: object) -> str | None:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        return None
    if not 1 <= len(value) <= MAX_QUERY_TXIDS or len(set(value)) != len(value):
        return None
    if not all(type(item) is str and _TXID.fullmatch(item) is not None for item in value):
        return None
    return ",".join(value)


_ORDER_FILTERS: dict[str, Callable[[object], str | None]] = {
    "trades": _param_bool,
    "userref": _param_userref,
    "cl_ord_id": _param_pattern(_CL_ORD_ID),
}

#: Per-endpoint parameter allowlist with a strict validator for each value.
PARAMETER_ALLOWLIST: dict[ReadEndpoint, dict[str, Callable[[object], str | None]]] = {
    ReadEndpoint.BALANCE: {},
    ReadEndpoint.TRADE_BALANCE: {"asset": _param_pattern(_ASSET)},
    ReadEndpoint.TRADE_VOLUME: {"pair": _param_pattern(_PAIR_LIST)},
    ReadEndpoint.OPEN_ORDERS: dict(_ORDER_FILTERS),
    ReadEndpoint.CLOSED_ORDERS: {
        **_ORDER_FILTERS,
        "start": _param_time,
        "end": _param_time,
        "ofs": _param_offset,
        "closetime": _param_closetime,
    },
    ReadEndpoint.QUERY_ORDERS: {
        "txid": _param_txids,
        "trades": _param_bool,
        "userref": _param_userref,
    },
}
_REQUIRED_PARAMETERS: dict[ReadEndpoint, frozenset[str]] = {ReadEndpoint.QUERY_ORDERS: frozenset({"txid"})}


def resolve_endpoint(name: object) -> ReadEndpoint:
    """The allowlisted endpoint named exactly by ``name``; anything else is ENDPOINT_REFUSED."""
    if isinstance(name, ReadEndpoint):
        return name
    if type(name) is str:
        endpoint = _ENDPOINTS_BY_NAME.get(name)
        if endpoint is not None:
            return endpoint
    raise KrakenReadError(ReadErrorCode.ENDPOINT_REFUSED, None, "endpoint_not_allowlisted")


def encode_parameters(endpoint: ReadEndpoint, parameters: Mapping[str, object] | None) -> list[tuple[str, str]]:
    """Validated ``(name, value)`` pairs in sorted order; unknown or invalid is PARAMETER_REFUSED."""
    if parameters is not None and not isinstance(parameters, Mapping):
        raise KrakenReadError(ReadErrorCode.PARAMETER_REFUSED, endpoint.value, "parameters_not_a_mapping")
    given = dict(parameters or {})
    allowed = PARAMETER_ALLOWLIST[endpoint]
    encoded: list[tuple[str, str]] = []
    for name in sorted(given, key=lambda item: str(item)):
        validator = allowed.get(name) if type(name) is str else None
        if validator is None:
            raise KrakenReadError(ReadErrorCode.PARAMETER_REFUSED, endpoint.value, "parameter_not_allowlisted")
        value = validator(given[name])
        if value is None:
            raise KrakenReadError(ReadErrorCode.PARAMETER_REFUSED, endpoint.value, "parameter_value_invalid")
        encoded.append((name, value))
    if not _REQUIRED_PARAMETERS.get(endpoint, frozenset()) <= {name for name, _ in encoded}:
        raise KrakenReadError(ReadErrorCode.PARAMETER_REFUSED, endpoint.value, "parameter_required")
    return encoded


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class AssetBalance:
    asset: str
    amount: Decimal


@dataclass(frozen=True)
class Balances:
    balances: tuple[AssetBalance, ...]

    def amount(self, asset: str) -> Decimal | None:
        for balance in self.balances:
            if balance.asset == asset:
                return balance.amount
        return None


@dataclass(frozen=True)
class TradeBalance:
    """Kraken TradeBalance in the requested asset; an absent optional field is None, never 0."""

    equivalent_balance: Decimal
    trade_balance: Decimal
    margin: Decimal | None
    unrealized_pnl: Decimal | None
    cost_basis: Decimal | None
    valuation: Decimal | None
    equity: Decimal | None
    free_margin: Decimal | None
    margin_level: Decimal | None
    unexecuted_value: Decimal | None


@dataclass(frozen=True)
class PairFee:
    """The account's real fee for one pair, in percent (Kraken ``fees`` / ``fees_maker``)."""

    pair: str
    taker_fee: Decimal
    maker_fee: Decimal | None
    taker_min_fee: Decimal | None
    taker_max_fee: Decimal | None
    taker_next_fee: Decimal | None
    next_volume: Decimal | None
    tier_volume: Decimal | None


@dataclass(frozen=True)
class FeeTier:
    currency: str
    volume_30d: Decimal
    pairs: tuple[PairFee, ...]

    def pair_fee(self, pair: str) -> PairFee | None:
        for fee in self.pairs:
            if fee.pair == pair:
                return fee
        return None


@dataclass(frozen=True)
class OrderRecord:
    txid: str
    status: str
    pair: str
    side: str
    order_type: str
    price: Decimal
    average_price: Decimal | None
    volume: Decimal
    executed_volume: Decimal
    cost: Decimal
    fee: Decimal
    opened_at: datetime
    closed_at: datetime | None
    userref: int | None
    cl_ord_id: str | None
    trade_ids: tuple[str, ...]


@dataclass(frozen=True)
class OpenOrders:
    orders: tuple[OrderRecord, ...]


@dataclass(frozen=True)
class ClosedOrders:
    orders: tuple[OrderRecord, ...]
    count: int


@dataclass(frozen=True)
class QueriedOrders:
    orders: tuple[OrderRecord, ...]


ReadResult = Balances | TradeBalance | FeeTier | OpenOrders | ClosedOrders | QueriedOrders


class _Malformed(Exception):
    pass


_DECIMAL_TEXT = re.compile(r"-?[0-9]{1,30}(\.[0-9]{1,30})?")
_ORDER_STATUSES = frozenset({"pending", "open", "closed", "canceled", "expired"})
_SIDES = frozenset({"buy", "sell"})
_ORDER_TYPE = re.compile(r"[a-z][a-z-]{0,31}")
_NAME = re.compile(r"[A-Za-z0-9./]{1,32}")
_ANY_TXID = re.compile(r"[A-Za-z0-9-]{1,64}")
_MAX_EPOCH_SECONDS = Decimal(10) ** 11


def _decimal(value: object) -> Decimal:
    if type(value) is not str or _DECIMAL_TEXT.fullmatch(value) is None:
        raise _Malformed
    return Decimal(value)


def _optional_decimal(document: Mapping[str, object], key: str) -> Decimal | None:
    if key not in document or document[key] is None:
        return None
    return _decimal(document[key])


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _Malformed
    return value


def _name(value: object, pattern: re.Pattern[str] = _NAME) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise _Malformed
    return value


def _timestamp(value: object) -> datetime:
    if type(value) is int:
        seconds = Decimal(value)
    elif isinstance(value, Decimal) and value.is_finite():
        seconds = value
    else:
        raise _Malformed
    if not 0 < seconds < _MAX_EPOCH_SECONDS:
        raise _Malformed
    microseconds = int((seconds * 1_000_000).to_integral_value(rounding=ROUND_DOWN))
    return _EPOCH + timedelta(microseconds=microseconds)


def _parse_balances(result: object) -> Balances:
    document = _object(result)
    return Balances(tuple(AssetBalance(_name(asset), _decimal(amount)) for asset, amount in document.items()))


def _parse_trade_balance(result: object) -> TradeBalance:
    document = _object(result)
    return TradeBalance(
        equivalent_balance=_decimal(document.get("eb")),
        trade_balance=_decimal(document.get("tb")),
        margin=_optional_decimal(document, "m"),
        unrealized_pnl=_optional_decimal(document, "n"),
        cost_basis=_optional_decimal(document, "c"),
        valuation=_optional_decimal(document, "v"),
        equity=_optional_decimal(document, "e"),
        free_margin=_optional_decimal(document, "mf"),
        margin_level=_optional_decimal(document, "ml"),
        unexecuted_value=_optional_decimal(document, "uv"),
    )


def _parse_fee_tier(result: object) -> FeeTier:
    document = _object(result)
    taker = _object(document.get("fees", {}))
    maker = _object(document.get("fees_maker", {}))
    if not set(maker) <= set(taker):
        raise _Malformed
    pairs: list[PairFee] = []
    for pair, entry in taker.items():
        fee = _object(entry)
        maker_fee = _object(maker[pair]) if pair in maker else None
        pairs.append(
            PairFee(
                pair=_name(pair),
                taker_fee=_decimal(fee.get("fee")),
                maker_fee=_decimal(maker_fee.get("fee")) if maker_fee is not None else None,
                taker_min_fee=_optional_decimal(fee, "minfee"),
                taker_max_fee=_optional_decimal(fee, "maxfee"),
                taker_next_fee=_optional_decimal(fee, "nextfee"),
                next_volume=_optional_decimal(fee, "nextvolume"),
                tier_volume=_optional_decimal(fee, "tiervolume"),
            )
        )
    return FeeTier(currency=_name(document.get("currency")), volume_30d=_decimal(document.get("volume")), pairs=tuple(pairs))


def _parse_order(txid: str, value: object) -> OrderRecord:
    order = _object(value)
    description = _object(order.get("descr"))
    status = order.get("status")
    side = description.get("type")
    if status not in _ORDER_STATUSES or side not in _SIDES:
        raise _Malformed
    assert isinstance(status, str) and isinstance(side, str)
    closed_raw = order.get("closetm")
    userref = order.get("userref")
    if userref is not None and type(userref) is not int:
        raise _Malformed
    cl_ord_id = order.get("cl_ord_id")
    if cl_ord_id is not None:
        cl_ord_id = _name(cl_ord_id, _CL_ORD_ID)
    trades = order.get("trades", [])
    if not isinstance(trades, list):
        raise _Malformed
    average = order.get("price")
    return OrderRecord(
        txid=_name(txid, _ANY_TXID),
        status=status,
        pair=_name(description.get("pair")),
        side=side,
        order_type=_name(description.get("ordertype"), _ORDER_TYPE),
        price=_decimal(description.get("price")),
        average_price=_decimal(average) if average is not None else None,
        volume=_decimal(order.get("vol")),
        executed_volume=_decimal(order.get("vol_exec")),
        cost=_decimal(order.get("cost")),
        fee=_decimal(order.get("fee")),
        opened_at=_timestamp(order.get("opentm")),
        closed_at=None if closed_raw is None or (type(closed_raw) is int and closed_raw == 0) else _timestamp(closed_raw),
        userref=userref,
        cl_ord_id=cl_ord_id,
        trade_ids=tuple(_name(trade, _ANY_TXID) for trade in trades),
    )


def _parse_orders(document: Mapping[str, object]) -> tuple[OrderRecord, ...]:
    return tuple(_parse_order(txid, order) for txid, order in document.items())


def _parse_open_orders(result: object) -> OpenOrders:
    return OpenOrders(_parse_orders(_object(_object(result).get("open"))))


def _parse_closed_orders(result: object) -> ClosedOrders:
    document = _object(result)
    count = document.get("count")
    if type(count) is not int or count < 0:
        raise _Malformed
    return ClosedOrders(_parse_orders(_object(document.get("closed"))), count)


def _parse_queried_orders(result: object) -> QueriedOrders:
    return QueriedOrders(_parse_orders(_object(result)))


_PARSERS: dict[ReadEndpoint, Callable[[object], ReadResult]] = {
    ReadEndpoint.BALANCE: _parse_balances,
    ReadEndpoint.TRADE_BALANCE: _parse_trade_balance,
    ReadEndpoint.TRADE_VOLUME: _parse_fee_tier,
    ReadEndpoint.OPEN_ORDERS: _parse_open_orders,
    ReadEndpoint.CLOSED_ORDERS: _parse_closed_orders,
    ReadEndpoint.QUERY_ORDERS: _parse_queried_orders,
}

# --------------------------------------------------------------------------- errors

_KRAKEN_ERRORS: dict[str, ReadErrorCode] = {
    "EAPI:Invalid key": ReadErrorCode.INVALID_KEY,
    "EAPI:Invalid signature": ReadErrorCode.INVALID_SIGNATURE,
    "EAPI:Invalid nonce": ReadErrorCode.INVALID_NONCE,
    "EGeneral:Permission denied": ReadErrorCode.PERMISSION_DENIED,
    "EAPI:Rate limit exceeded": ReadErrorCode.RATE_LIMITED,
    "EGeneral:Too many requests": ReadErrorCode.RATE_LIMITED,
    "EService:Unavailable": ReadErrorCode.SERVICE_UNAVAILABLE,
    "EService:Busy": ReadErrorCode.SERVICE_UNAVAILABLE,
}
_ERROR_PRECEDENCE = (
    ReadErrorCode.INVALID_KEY,
    ReadErrorCode.INVALID_SIGNATURE,
    ReadErrorCode.INVALID_NONCE,
    ReadErrorCode.PERMISSION_DENIED,
    ReadErrorCode.RATE_LIMITED,
    ReadErrorCode.SERVICE_UNAVAILABLE,
    ReadErrorCode.EXCHANGE_ERROR,
)
_TRANSPORT_CODES: dict[TransportFailureKind, ReadErrorCode] = {
    TransportFailureKind.TIMEOUT: ReadErrorCode.TIMEOUT,
    TransportFailureKind.CONNECTION: ReadErrorCode.TRANSPORT_FAILED,
    TransportFailureKind.TOO_LARGE: ReadErrorCode.TOO_LARGE,
    TransportFailureKind.REFUSED: ReadErrorCode.TRANSPORT_FAILED,
}


def classify_kraken_errors(errors: Sequence[object]) -> ReadErrorCode | None:
    """The most severe typed code in Kraken's ``error`` list; warnings (``W...``) alone are None."""
    found: set[ReadErrorCode] = set()
    for entry in errors:
        if type(entry) is not str:
            found.add(ReadErrorCode.EXCHANGE_ERROR)
        elif entry.startswith("W"):
            continue
        else:
            found.add(_KRAKEN_ERRORS.get(entry, ReadErrorCode.EXCHANGE_ERROR))
    for code in _ERROR_PRECEDENCE:
        if code in found:
            return code
    return None


class _BadJson(Exception):
    pass


def _reject_response_constant(_name: str) -> object:
    raise _Malformed


def _unique_response_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    try:
        return _unique_object(pairs)
    except ValueError:
        raise _Malformed from None


def _decode_body(body: bytes) -> object:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise _BadJson from None
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_response_constant,
            object_pairs_hook=_unique_response_object,
        )
    except _Malformed:
        raise
    except (ValueError, RecursionError):
        raise _BadJson from None


# --------------------------------------------------------------------------- reader


class KrakenPrivateReader:
    """Signed, allowlisted, read-only Kraken spot account access for SHADOW_LIVE and above."""

    def __init__(
        self,
        mode: TradingMode,
        credentials: KrakenCredentials,
        nonce_store: NonceStore,
        transport: PrivateTransport,
    ) -> None:
        if not allows_private_reads(mode):
            raise KrakenReadError(ReadErrorCode.MODE_REFUSED, None, "mode_below_shadow_live")
        if not isinstance(credentials, KrakenCredentials):
            raise KrakenReadError(ReadErrorCode.UNAVAILABLE, None, UnavailableReason.CREDENTIALS_MALFORMED.value)
        self._mode = mode
        self._credentials = credentials
        self._nonces = nonce_store
        self._transport = transport
        self._request_lock = threading.Lock()
        self._locked_by: ReadErrorCode | None = None

    def __repr__(self) -> str:
        locked = self._locked_by.value if self._locked_by is not None else "no"
        return f"KrakenPrivateReader(mode={self._mode.value}, locked={locked})"

    @property
    def mode(self) -> TradingMode:
        return self._mode

    @property
    def locked_by(self) -> ReadErrorCode | None:
        return self._locked_by

    def close(self) -> None:
        self._transport.close()

    def read(self, endpoint: object, parameters: Mapping[str, object] | None = None) -> ReadResult:
        """The single choke point: allowlist, parameters, lock, nonce, sign, send, classify, parse."""
        allowed = resolve_endpoint(endpoint)
        encoded = encode_parameters(allowed, parameters)
        with self._request_lock:
            if self._locked_by is not None:
                raise KrakenReadError(ReadErrorCode.LOCKED, allowed.value, self._locked_by.value)
            document = self._send(allowed, encoded)
        return self._interpret(allowed, document)

    def _send(self, endpoint: ReadEndpoint, encoded: list[tuple[str, str]]) -> object:
        unavailable: UnavailableReason | None = None
        try:
            nonce = str(self._nonces.reserve())
        except NonceStoreError as error:
            unavailable = error.reason
        if unavailable is not None:
            raise KrakenReadError(ReadErrorCode.UNAVAILABLE, endpoint.value, unavailable.value)
        postdata = urlencode([("nonce", nonce), *encoded])
        headers = {
            "API-Key": self._credentials.api_key,
            "API-Sign": api_sign(endpoint.uri_path, nonce, postdata, self._credentials.secret),
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Accept-Encoding": "identity",
        }
        failure: ReadErrorCode | None = None
        reply: TransportReply | None = None
        try:
            reply = self._transport.post(endpoint.url, postdata.encode("ascii"), headers, REQUEST_TIMEOUT_SECONDS)
        except TransportFailure as error:
            failure = _TRANSPORT_CODES.get(error.kind, ReadErrorCode.TRANSPORT_FAILED)
        except Exception:
            failure = ReadErrorCode.TRANSPORT_FAILED
        if failure is not None or not isinstance(reply, TransportReply):
            raise KrakenReadError(failure or ReadErrorCode.TRANSPORT_FAILED, endpoint.value)
        if reply.redirected or 300 <= reply.status < 400:
            raise KrakenReadError(ReadErrorCode.REDIRECT_REFUSED, endpoint.value, http_status=reply.status)
        if reply.status != 200:
            raise KrakenReadError(ReadErrorCode.HTTP_STATUS, endpoint.value, http_status=reply.status)
        decoded: object = None
        problem: ReadErrorCode | None = None
        try:
            decoded = _decode_body(reply.body)
        except _BadJson:
            problem = ReadErrorCode.NOT_JSON
        except _Malformed:
            problem = ReadErrorCode.MALFORMED_RESPONSE
        if problem is not None:
            raise KrakenReadError(problem, endpoint.value)
        if not isinstance(decoded, dict) or not isinstance(decoded.get("error"), list):
            raise KrakenReadError(ReadErrorCode.MALFORMED_RESPONSE, endpoint.value)
        code = classify_kraken_errors(decoded["error"])
        if code is not None:
            if code in AUTH_ERROR_CODES:
                self._locked_by = code
            raise KrakenReadError(code, endpoint.value)
        return decoded.get("result")

    @staticmethod
    def _interpret(endpoint: ReadEndpoint, result: object) -> ReadResult:
        parsed: ReadResult | None = None
        try:
            parsed = _PARSERS[endpoint](result)
        except (_Malformed, ArithmeticError, OverflowError, TypeError, ValueError):
            parsed = None
        if parsed is None:
            raise KrakenReadError(ReadErrorCode.MALFORMED_RESPONSE, endpoint.value)
        return parsed

    def balance(self) -> Balances:
        return _expect(self.read(ReadEndpoint.BALANCE), Balances)

    def trade_balance(self, asset: str | None = None) -> TradeBalance:
        parameters = {} if asset is None else {"asset": asset}
        return _expect(self.read(ReadEndpoint.TRADE_BALANCE, parameters), TradeBalance)

    def trade_volume(self, pair: str | None = None) -> FeeTier:
        parameters = {} if pair is None else {"pair": pair}
        return _expect(self.read(ReadEndpoint.TRADE_VOLUME, parameters), FeeTier)

    def open_orders(self, *, trades: bool = False, userref: int | None = None) -> OpenOrders:
        parameters: dict[str, object] = {"trades": trades}
        if userref is not None:
            parameters["userref"] = userref
        return _expect(self.read(ReadEndpoint.OPEN_ORDERS, parameters), OpenOrders)

    def closed_orders(
        self,
        *,
        trades: bool = False,
        start: int | str | None = None,
        end: int | str | None = None,
        ofs: int | None = None,
        closetime: str | None = None,
    ) -> ClosedOrders:
        parameters: dict[str, object] = {"trades": trades}
        for name, value in (("start", start), ("end", end), ("ofs", ofs), ("closetime", closetime)):
            if value is not None:
                parameters[name] = value
        return _expect(self.read(ReadEndpoint.CLOSED_ORDERS, parameters), ClosedOrders)

    def query_orders(self, txids: Sequence[str], *, trades: bool = False) -> QueriedOrders:
        parameters: dict[str, object] = {"txid": list(txids), "trades": trades}
        return _expect(self.read(ReadEndpoint.QUERY_ORDERS, parameters), QueriedOrders)


def _expect[T](result: ReadResult, kind: type[T]) -> T:
    if not isinstance(result, kind):
        raise KrakenReadError(ReadErrorCode.MALFORMED_RESPONSE)
    return result


def open_private_reader(
    mode: TradingMode,
    *,
    environ: Mapping[str, str],
    state_dir: str | os.PathLike[str] | None = None,
    clock: WallClock | None = None,
    transport_factory: Callable[[], PrivateTransport] = RequestsTransport,
) -> KrakenPrivateReader | ModeRefused | Unavailable:
    """The only factory. Mode first (nothing read below SHADOW_LIVE), then credentials, then
    a read-only check of the nonce store, and only then the transport."""
    if not isinstance(mode, TradingMode) or not allows_private_reads(mode):
        return ModeRefused(mode if isinstance(mode, TradingMode) else None)
    directory = Path(state_dir) if state_dir is not None else default_state_dir(environ)
    credentials = load_credentials(environ, directory)
    if isinstance(credentials, Unavailable):
        return credentials
    store = NonceStore(directory, clock if clock is not None else SystemClock())
    try:
        store.last()
    except NonceStoreError as error:
        return Unavailable(error.reason)
    transport: PrivateTransport | None = None
    try:
        transport = transport_factory()
    except Exception:
        transport = None
    if transport is None:
        return Unavailable(UnavailableReason.TRANSPORT_UNAVAILABLE)
    return KrakenPrivateReader(mode, credentials, store, transport)
