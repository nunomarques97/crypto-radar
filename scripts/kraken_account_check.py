"""Show the Kraken spot account through the READ-ONLY private adapter, redacted.

Usage::

    python scripts/kraken_account_check.py --mode SHADOW_LIVE [--pair XBTEUR] [--asset ZEUR]

The mode is always explicit. Without ``--mode``, or with ANALYSIS_ONLY,
RETROSPECTIVE or PAPER, the command refuses before it reads any credential, touches the
nonce file or creates a transport. From SHADOW_LIVE up it opens the adapter
(``radar_v08.adapters.kraken_private_read``) and makes exactly four signed reads: Balance,
TradeBalance, TradeVolume and OpenOrders. It never places, changes or cancels an order.

Credentials come from ``KRAKEN_API_KEY`` + ``KRAKEN_API_SECRET`` or else from
``<state dir>/.kraken/credentials.json`` (the state dir is ``RADAR_STATE_DIR``, else this
project folder). The output never contains the key, the secret, the signature, the nonce,
a request body, an order's userref or cl_ord_id, or a full order id (only its first
segment). The only file this command writes is the nonce store ``<state dir>/.kraken/nonce``;
it never opens the radar's database or logs.

Exit codes: 0 done, 2 refused (mode), 3 unavailable (no usable credentials or nonce store),
4 a Kraken read failed.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import TextIO

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from radar_v08.adapters.kraken_private_read import (  # noqa: E402
    Balances,
    FeeTier,
    KrakenPrivateReader,
    KrakenReadError,
    ModeRefused,
    OpenOrders,
    OrderRecord,
    PrivateTransport,
    ReadErrorCode,
    RequestsTransport,
    TradeBalance,
    Unavailable,
    UnavailableReason,
    WallClock,
    open_private_reader,
)
from radar_v08.domain.trading_mode import (  # noqa: E402
    PRIVATE_READ_MINIMUM_MODE,
    TradingMode,
    TradingModeError,
    allows_private_reads,
    parse_trading_mode,
)

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNAVAILABLE = 3
EXIT_READ_FAILED = 4

DEFAULT_PAIR = "XBTEUR"
DEFAULT_ASSET = "ZEUR"
GUIDE = "docs/KRAKEN_READ_ONLY_API_KEY.md"

_UNAVAILABLE_HINTS: dict[UnavailableReason, str] = {
    UnavailableReason.CREDENTIALS_MISSING: f"No Kraken API key found. Follow {GUIDE} to create a read-only key.",
    UnavailableReason.CREDENTIALS_INCOMPLETE: "The Kraken API key and its private key must both be set, in the same place.",
    UnavailableReason.CREDENTIALS_EMPTY: "The Kraken API key or its private key is empty.",
    UnavailableReason.CREDENTIALS_MALFORMED: 'The credentials file must hold exactly {"api_key": "...", "api_secret": "..."}.',
    UnavailableReason.CREDENTIALS_UNREADABLE: "The credentials file could not be read.",
    UnavailableReason.API_KEY_MALFORMED: "The API key has characters Kraken never uses. Copy it again from Kraken.",
    UnavailableReason.SECRET_NOT_BASE64: "The private key is not valid base64. Copy it again from Kraken.",
    UnavailableReason.NONCE_STORE_CORRUPT: "The nonce file .kraken/nonce is damaged; it is never reset automatically.",
    UnavailableReason.NONCE_STORE_UNREADABLE: "The nonce file .kraken/nonce could not be read.",
    UnavailableReason.NONCE_STORE_UNWRITABLE: "The nonce file .kraken/nonce could not be written.",
    UnavailableReason.CLOCK_INVALID: "The local clock did not give a valid time.",
    UnavailableReason.TRANSPORT_UNAVAILABLE: "The HTTPS client could not be created.",
}

_READ_HINTS: dict[ReadErrorCode, str] = {
    ReadErrorCode.INVALID_KEY: f"Kraken rejected the API key. Check it, or create a new one with {GUIDE}.",
    ReadErrorCode.INVALID_SIGNATURE: "Kraken rejected the signature. The private key is probably wrong; copy it again.",
    ReadErrorCode.INVALID_NONCE: "Kraken rejected the nonce. Use this key only for this command, one run at a time.",
    ReadErrorCode.PERMISSION_DENIED: f"The key lacks a query permission. Check it against {GUIDE}.",
    ReadErrorCode.RATE_LIMITED: "Kraken's rate limit was reached. Wait a few minutes and run the check again.",
    ReadErrorCode.SERVICE_UNAVAILABLE: "Kraken is busy or unavailable. Try again later.",
    ReadErrorCode.TIMEOUT: "Kraken did not answer in time. Try again later.",
    ReadErrorCode.TRANSPORT_FAILED: "Kraken could not be reached. Check the internet connection.",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Kraken account check: balances, fee tier and open orders, redacted."
    )
    parser.add_argument(
        "--mode",
        default=None,
        help=f"explicit trading mode; {PRIVATE_READ_MINIMUM_MODE.value} or higher is required",
    )
    parser.add_argument("--pair", default=DEFAULT_PAIR, help=f"pair for the fee tier (default {DEFAULT_PAIR})")
    parser.add_argument("--asset", default=DEFAULT_ASSET, help=f"asset for the trade balance (default {DEFAULT_ASSET})")
    return parser


def short_order_id(txid: str) -> str:
    """Only the first segment of an order id: enough to tell orders apart on screen."""
    return txid.split("-", 1)[0][:6] + "-..."


def plain(value: Decimal) -> str:
    """A Decimal in positional notation (Kraken's "0.0000000000" stays that, never 0E-10)."""
    return format(value, "f")


def _format_order(order: OrderRecord) -> str:
    return (
        f"  {short_order_id(order.txid):<10} {order.pair:<12} {order.side:<4} {order.order_type:<12} "
        f"price {plain(order.price)}  volume {plain(order.volume)}  executed {plain(order.executed_volume)}  "
        f"{order.status}"
    )


def render(
    mode: TradingMode,
    asset: str,
    pair: str,
    balances: Balances,
    trade_balance: TradeBalance,
    fee_tier: FeeTier,
    open_orders: OpenOrders,
) -> list[str]:
    """The redacted report: no credential, nonce, full order id, userref or cl_ord_id."""
    lines = [f"Kraken account check (read only, mode {mode.value})", "", "Balances"]
    if balances.balances:
        width = max(len(balance.asset) for balance in balances.balances)
        lines.extend(f"  {balance.asset:<{width}}  {plain(balance.amount)}" for balance in balances.balances)
    else:
        lines.append("  (no assets)")
    lines += [
        "",
        f"Trade balance ({asset})",
        f"  Equivalent balance: {plain(trade_balance.equivalent_balance)}",
        f"  Trade balance: {plain(trade_balance.trade_balance)}",
        "",
        f"Fee tier (requested pair {pair})",
        f"  30-day volume: {plain(fee_tier.volume_30d)} {fee_tier.currency}",
    ]
    if fee_tier.pairs:
        for fee in fee_tier.pairs:
            maker = f"{plain(fee.maker_fee)} %" if fee.maker_fee is not None else "not returned"
            lines.append(f"  {fee.pair}: taker {plain(fee.taker_fee)} %, maker {maker}")
    else:
        lines.append("  Kraken returned no fee for this pair.")
    lines += ["", f"Open orders ({len(open_orders.orders)})"]
    if open_orders.orders:
        lines.extend(_format_order(order) for order in open_orders.orders)
    else:
        lines.append("  (none)")
    return lines


def _read_account(
    reader: KrakenPrivateReader, asset: str, pair: str
) -> tuple[Balances, TradeBalance, FeeTier, OpenOrders]:
    return (
        reader.balance(),
        reader.trade_balance(asset),
        reader.trade_volume(pair),
        reader.open_orders(),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    clock: WallClock | None = None,
    transport_factory: Callable[[], PrivateTransport] = RequestsTransport,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_request:
        return exit_request.code if isinstance(exit_request.code, int) else EXIT_REFUSED

    # The mode is checked before the environment, the state dir or the network is touched.
    if args.mode is None:
        print(
            f"Refused: --mode is required. The account check runs only with --mode "
            f"{PRIVATE_READ_MINIMUM_MODE.value} or higher. Nothing was read.",
            file=err,
        )
        return EXIT_REFUSED
    try:
        mode = parse_trading_mode(args.mode)
    except TradingModeError:
        valid = ", ".join(item.value for item in TradingMode)
        print(f"Refused: unknown mode. Use one of: {valid}. Nothing was read.", file=err)
        return EXIT_REFUSED
    if not allows_private_reads(mode):
        print(
            f"Refused: mode {mode.value} may not read the Kraken account. "
            f"Use --mode {PRIVATE_READ_MINIMUM_MODE.value} or higher. Nothing was read.",
            file=err,
        )
        return EXIT_REFUSED

    opened = open_private_reader(
        mode,
        environ=os.environ if environ is None else environ,
        state_dir=state_dir,
        clock=clock,
        transport_factory=transport_factory,
    )
    if isinstance(opened, ModeRefused):
        print(f"Refused: mode {mode.value} may not read the Kraken account. Nothing was read.", file=err)
        return EXIT_REFUSED
    if isinstance(opened, Unavailable):
        print(f"UNAVAILABLE: {opened.reason.value}. {_UNAVAILABLE_HINTS.get(opened.reason, '')}".rstrip(), file=err)
        return EXIT_UNAVAILABLE

    try:
        account = _read_account(opened, args.asset, args.pair)
    except KrakenReadError as error:
        endpoint = error.endpoint or "request"
        detail = f" ({error.reason})" if error.reason else ""
        status = f" HTTP {error.http_status}" if error.http_status is not None else ""
        hint = _READ_HINTS.get(error.code, "")
        print(f"FAILED at {endpoint}: {error.code.value}{detail}{status}. {hint}".rstrip(), file=err)
        return EXIT_READ_FAILED
    except Exception as error:  # never print an unexpected message: it could carry request data
        print(f"FAILED: unexpected {type(error).__name__}. Nothing more was read.", file=err)
        return EXIT_READ_FAILED
    finally:
        opened.close()

    for line in render(mode, args.asset, args.pair, *account):
        print(line, file=out)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
