"""Deterministic setup classification. No LLM anywhere in this module.

`setup_type` in {BREAKOUT, CONTINUATION, REVERSAL, SQUEEZE_RELEASE,
EXHAUSTION, NONE}; `direction` in {LONG, SHORT, NONE}. Every condition below
is spelled out and every threshold comes from `config.SETUP_THRESHOLDS` - no
hidden constants.

Evaluation order (first match wins) and why:
1. Exhaustion + reversal confirmation -> REVERSAL. An extreme, fading move
   that's ALSO rejecting at a range extreme is a reversal candidate, not
   just a warning.
2. Squeeze release -> SQUEEZE_RELEASE. A volatility expansion out of a prior
   compression is a distinct, higher-quality event than a plain breakout.
3. Breakout -> BREAKOUT. Price cleared the 24h/4h range with volume.
4. Reversal (without exhaustion) -> REVERSAL. Divergence from the higher
   timeframe trend at a range extreme, with a rejection wick.
5. Continuation -> CONTINUATION. Momentum coherent across 5m/15m/1h, no
   exhaustion, no fresh breakout (already trending).
6. Exhaustion alone -> EXHAUSTION, direction NONE. A flag, not a trade idea.
7. Otherwise -> NONE / NONE.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .anomaly import Features as L1Features
from .l2_features import L2Features
from .l2_features import sign as _sign

SETUP_TYPES = ("BREAKOUT", "CONTINUATION", "REVERSAL", "SQUEEZE_RELEASE", "EXHAUSTION", "NONE")
DIRECTIONS = ("LONG", "SHORT", "NONE")


@dataclass
class SetupResult:
    setup_type: str
    direction: str
    notes: list[str]


def _direction_from_sign(sign: int) -> str:
    if sign > 0:
        return "LONG"
    if sign < 0:
        return "SHORT"
    return "NONE"


def _volume_confirmed(l1: L1Features) -> bool:
    intensity = l1.volume_intensity_15m
    return intensity is not None and intensity >= config.SETUP_THRESHOLDS["volume_confirm_intensity"]


def _reversal_confirmed(l1: L1Features, l2: L2Features) -> bool:
    """Divergence between the fresh 15m move and the 1h trend, at a range
    extreme, with a rejection wick and volume.
    """
    if l2.return_15m_atr is None or l2.return_1h_atr is None:
        return False
    thresh = config.SETUP_THRESHOLDS["reversal_min_atr_15m"]
    if abs(l2.return_15m_atr) < thresh:
        return False

    diverges = _sign(l2.return_15m_atr) != 0 and _sign(l2.return_15m_atr) != _sign(l2.return_1h_atr)
    at_extreme = l2.rejection_state in ("REJECTION_AT_HIGH", "REJECTION_AT_LOW")
    return diverges and at_extreme and _volume_confirmed(l1)


def _squeeze_release_confirmed(l1: L1Features, l2: L2Features) -> bool:
    """Compressed a moment ago (`range_compression`, evaluated on the PRIOR
    bar - see l2_features.compute_l2_features), expanding right now, with
    volume backing the release. Compression alone is never enough (doc
    explicit: "não considerar o simples estado de compressão como
    oportunidade por si só").
    """
    return bool(l2.range_compression and l2.range_expansion and _volume_confirmed(l1))


def _breakout_confirmed(l2: L2Features) -> bool:
    return l2.breakout_state in ("BREAKOUT_UP", "BREAKOUT_DOWN")


def _continuation_confirmed(l1: L1Features, l2: L2Features) -> bool:
    """Momentum must be coherent (same sign) across 5m/15m/1h and clear the
    configured ATR minimums - a single noisy bar doesn't count.
    """
    signs = [_sign(l2.return_5m_atr), _sign(l2.return_15m_atr), _sign(l2.return_1h_atr)]
    if 0 in signs:
        return False
    if len(set(signs)) != 1:
        return False
    if l2.return_15m_atr is None or l2.return_1h_atr is None:
        return False
    if abs(l2.return_15m_atr) < config.SETUP_THRESHOLDS["momentum_min_atr_15m"]:
        return False
    if abs(l2.return_1h_atr) < config.SETUP_THRESHOLDS["momentum_min_atr_1h"]:
        return False
    return True


def classify_setup(l1: L1Features, l2: L2Features) -> SetupResult:
    if l2.l2_warmup:
        return SetupResult("NONE", "NONE", ["l2_warmup"])

    notes: list[str] = []

    if l2.exhaustion and _reversal_confirmed(l1, l2):
        notes.append("exhaustion_with_reversal_confirmation")
        direction = _direction_from_sign(_sign(l2.return_15m_atr))
        return SetupResult("REVERSAL", direction, notes)

    if _squeeze_release_confirmed(l1, l2):
        notes.append("squeeze_release: compression then expansion, volume-confirmed")
        direction = _direction_from_sign(_sign(l2.return_15m_atr))
        return SetupResult("SQUEEZE_RELEASE", direction, notes)

    if _breakout_confirmed(l2) and _volume_confirmed(l1) and l2.range_expansion:
        notes.append("breakout: range clear + volume + range expansion")
        direction = "LONG" if l2.breakout_state == "BREAKOUT_UP" else "SHORT"
        return SetupResult("BREAKOUT", direction, notes)

    if not l2.exhaustion and _reversal_confirmed(l1, l2):
        notes.append("reversal: divergence + rejection + volume")
        direction = _direction_from_sign(_sign(l2.return_15m_atr))
        return SetupResult("REVERSAL", direction, notes)

    if not l2.exhaustion and _continuation_confirmed(l1, l2):
        notes.append("continuation: coherent momentum across 5m/15m/1h")
        direction = _direction_from_sign(_sign(l2.return_1h_atr))
        return SetupResult("CONTINUATION", direction, notes)

    if l2.exhaustion:
        notes.append("exhaustion: extreme move + fading volume, no reversal confirmation yet")
        return SetupResult("EXHAUSTION", "NONE", notes)

    return SetupResult("NONE", "NONE", notes)
