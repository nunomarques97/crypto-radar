"""Explicit trading modes of the future trading program, in their fixed order.

docs/FUTURE_TRADING_ROADMAP.md "Fixed progression and permissions":
ANALYSIS_ONLY < RETROSPECTIVE < PAPER < SHADOW_LIVE < MICRO_LIVE < CONSTRAINED_LIVE <
APPROVED_ENVELOPE. A mode is always chosen explicitly; it is never inferred from which
credentials happen to be present. ANALYSIS_ONLY, RETROSPECTIVE and PAPER may not construct
any private exchange adapter; SHADOW_LIVE is the first mode that may read the account.

Parsing is strict: the exact upper-case name only. Blank text, surrounding whitespace, a
lower-case or mixed-case spelling and any unknown value are refused, never mapped to a
default. The only default (``DEFAULT_TRADING_MODE``) is the lowest mode. Pure: no I/O.
"""

from __future__ import annotations

from enum import Enum


class TradingModeError(ValueError):
    """A value that is not exactly one TradingMode name."""


class TradingMode(Enum):
    """The fixed progression; comparisons follow the roadmap order, never the names."""

    ANALYSIS_ONLY = "ANALYSIS_ONLY"
    RETROSPECTIVE = "RETROSPECTIVE"
    PAPER = "PAPER"
    SHADOW_LIVE = "SHADOW_LIVE"
    MICRO_LIVE = "MICRO_LIVE"
    CONSTRAINED_LIVE = "CONSTRAINED_LIVE"
    APPROVED_ENVELOPE = "APPROVED_ENVELOPE"

    @property
    def rank(self) -> int:
        return _ORDER.index(self)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, TradingMode):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, TradingMode):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, TradingMode):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, TradingMode):
            return NotImplemented
        return self.rank >= other.rank


_ORDER: tuple[TradingMode, ...] = tuple(TradingMode)

#: The only default: the lowest mode. Nothing defaults above PAPER.
DEFAULT_TRADING_MODE = TradingMode.ANALYSIS_ONLY
#: The first mode allowed to construct an authenticated account-read adapter.
PRIVATE_READ_MINIMUM_MODE = TradingMode.SHADOW_LIVE


def parse_trading_mode(value: object) -> TradingMode:
    """Return the mode named exactly by ``value``; anything else raises TradingModeError."""
    if type(value) is not str:
        raise TradingModeError("trading mode must be a string")
    for mode in _ORDER:
        if value == mode.value:
            return mode
    raise TradingModeError("unknown trading mode; expected one of: " + ", ".join(mode.value for mode in _ORDER))


def allows_private_reads(mode: TradingMode) -> bool:
    """True only for SHADOW_LIVE and above; any non-TradingMode value is refused."""
    if not isinstance(mode, TradingMode):
        return False
    return mode >= PRIVATE_READ_MINIMUM_MODE
