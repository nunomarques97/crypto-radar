"""Typed ports of the trend rules of the 2026-10-03 feasibility study.

* ``Ens(asset)`` and ``Ens(asset, vol_target=0.5)`` (ENS and ENS_VT): robust.py ``run``.
* ``BtcTrend5`` and ``BtcTrend5Vt``: the pre-registered harness rules ``btc_trend5`` and
  ``btc_trend5_vt``.
* ``BuyAndHold(asset)``: the benchmark (``bh_btc`` for BTCUSDT).

No strategy file is executed. Each rule that was registered in the imported registry declares its
registered name and the sha256 of the registered source; the verbatim sources are kept under
non-importable names in ``docs/audit/2026-10-03-trend-feasibility/strategy_sources/`` and the tests
prove both the hash binding and the numerical equivalence. ENS and ENS_VT on any asset other than
BTCUSDT were exploratory prior trials, never registered, so they carry no registration.

Each rule is pure: its weights depend only on the history it is given (no hidden state). The
arithmetic keeps the reference order of float operations so parity holds to 1e-9.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .trend_engine import Bar, History, Strategy, day_open_ms

BTCUSDT = "BTCUSDT"
ENS_LOOKBACKS: tuple[int, ...] = (20, 50, 100, 150, 200)
ENS_BAND = 0.2
ENS_VOL_TARGET = 0.5
ENS_VOL_DAYS = 30
WARMUP_DAYS = 201
TREND5_SMA: tuple[int, ...] = (100, 200)
TREND5_DONCHIAN: tuple[tuple[int, int], ...] = ((20, 10), (50, 25), (100, 50))
TREND5_BAND = 0.15
TREND5_VT_BAND = 0.10
TREND5_VT_TARGET = 0.30
TREND5_VT_EWMA_SPAN = 20
TREND5_VT_RETURNS = 200
BH_BAND = 0.01
ANNUALIZATION_DAYS = 365


class StrategyError(ValueError):
    """A rule was asked for weights it cannot compute from the given history."""


@dataclass(frozen=True)
class RegisteredRule:
    """Identity of a rule in the imported pre-registration registry."""

    name: str
    sha256: str


REGISTERED: Mapping[str, RegisteredRule] = {
    rule.name: rule
    for rule in (
        RegisteredRule("bh_btc", "d9a2ebc28eef702a026e002242486fe8709119c4775f18f247171ed540e6e089"),
        RegisteredRule("ens_btc", "d56a80fd8c486fcbf960109229834423b9c4364ee54ca7016b68165fdba0a670"),
        RegisteredRule("ens_vt_btc", "094bb5b97af67f22d8c80d4413691af3396f9bd4ece603441a369a999af70126"),
        RegisteredRule("btc_trend5", "a96640fff254d088fe733ff0b01a7a3bb5a72158c3c8cf56ed4e933bf4e92253"),
        RegisteredRule("btc_trend5_vt", "28b1082d695b6662817360444e2f07569fa5a85daa187d51bc154c4ef848c412"),
    )
}


def _require(closes: Sequence[float], n: int, rule: str) -> None:
    if len(closes) < n:
        raise StrategyError(f"{rule} needs {n} closes, got {len(closes)}")


# ---------------------------------------------------------------------------
# Signal arithmetic (shared with the paper trader)
# ---------------------------------------------------------------------------


def ens_fraction(closes: Sequence[float]) -> float:
    """Share of SMA lookbacks {20, 50, 100, 150, 200} with the last close above the SMA."""
    _require(closes, max(ENS_LOOKBACKS), "ENS")
    c = closes[-1]
    return sum(c > sum(closes[-n:]) / n for n in ENS_LOOKBACKS) / len(ENS_LOOKBACKS)


def realized_vol(closes: Sequence[float], days: int = ENS_VOL_DAYS) -> float:
    """Annualized sample volatility of the last ``days`` daily log returns (x sqrt(365))."""
    _require(closes, days + 1, "realized volatility")
    lr = [math.log(closes[x] / closes[x - 1]) for x in range(len(closes) - days, len(closes))]
    m = sum(lr) / days
    return math.sqrt(sum((x - m) ** 2 for x in lr) / (days - 1) * ANNUALIZATION_DAYS)


def ens_target(closes: Sequence[float], vol_target: float | None = None) -> float:
    """ENS, or ENS_VT = ENS x min(1, vol_target / 30-day realized vol) when ``vol_target`` is set."""
    tgt = ens_fraction(closes)
    if vol_target is None:
        return tgt
    vol = realized_vol(closes)
    if vol <= 0:
        raise StrategyError("ENS_VT: zero realized volatility")
    return tgt * min(1.0, vol_target / vol)


def donchian_long(closes: Sequence[float], entry: int, exit_: int) -> bool:
    """Long after a close above the previous ``entry`` closes, flat after a close below the previous
    ``exit_`` closes. Rebuilt from history: the most recent of the two events decides (flat if none)."""
    for t in range(len(closes) - 1, max(entry, exit_) - 1, -1):
        c = closes[t]
        if c > max(closes[t - entry : t]):
            return True
        if c < min(closes[t - exit_ : t]):
            return False
    return False


def trend_votes(closes: Sequence[float]) -> float:
    """btc_trend5: share of five long votes (SMA100, SMA200, Donchian 20/10, 50/25, 100/50)."""
    _require(closes, max(TREND5_SMA), "btc_trend5")
    votes = [closes[-1] > sum(closes[-n:]) / n for n in TREND5_SMA]
    votes += [donchian_long(closes, e, x) for e, x in TREND5_DONCHIAN]
    return sum(votes) / len(votes)


def weekly_vol_scale(bars: Sequence[Bar], today: date) -> float:
    """btc_trend5_vt overlay: min(1, 0.30 / sigma), sigma = EWMA (alpha 2/21) of squared daily log
    returns over the last 200 returns, x sqrt(365), from closes of bars that opened before the open
    of the most recent Monday (UTC) on or before ``today``, so it changes weekly."""
    monday = today - timedelta(days=today.weekday())
    monday_ms = day_open_ms(monday)
    closes = [b.close for b in bars if b.open_time_ms < monday_ms]
    closes = closes[-(TREND5_VT_RETURNS + 1) :]
    _require(closes, 2, "btc_trend5_vt")
    lr = [math.log(closes[k] / closes[k - 1]) for k in range(1, len(closes))]
    a = 2.0 / (TREND5_VT_EWMA_SPAN + 1)
    var = lr[0] ** 2
    for r in lr[1:]:
        var = (1 - a) * var + a * r * r
    sigma = math.sqrt(var * ANNUALIZATION_DAYS)
    return min(1.0, TREND5_VT_TARGET / sigma) if sigma > 0 else 1.0


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ens:
    """ENS (``vol_target`` None) or ENS_VT on one spot asset; long/flat against cash."""

    asset: str
    vol_target: float | None = None

    def __post_init__(self) -> None:
        if self.vol_target is not None and not (math.isfinite(self.vol_target) and self.vol_target > 0):
            raise StrategyError("vol_target must be finite and > 0")

    @property
    def name(self) -> str:
        return f"{'ENS' if self.vol_target is None else 'ENS_VT'}({self.asset})"

    @property
    def universe(self) -> tuple[str, ...]:
        return (self.asset,)

    @property
    def warmup_days(self) -> int:
        return WARMUP_DAYS

    @property
    def band(self) -> float:
        return ENS_BAND

    @property
    def max_gross(self) -> float:
        return 1.0

    @property
    def registration(self) -> RegisteredRule | None:
        if self.asset != BTCUSDT:
            return None
        if self.vol_target is None:
            return REGISTERED["ens_btc"]
        return REGISTERED["ens_vt_btc"] if self.vol_target == ENS_VOL_TARGET else None

    def weights(self, h: History) -> Mapping[str, float]:
        closes = h.closes(self.asset, max(max(ENS_LOOKBACKS), ENS_VOL_DAYS + 1))
        return {self.asset: ens_target(closes, self.vol_target)}


@dataclass(frozen=True)
class BtcTrend5:
    """btc_trend5: five-vote BTC trend ensemble, long/flat against cash, band 0.15."""

    @property
    def name(self) -> str:
        return "btc_trend5"

    @property
    def universe(self) -> tuple[str, ...]:
        return (BTCUSDT,)

    @property
    def warmup_days(self) -> int:
        return WARMUP_DAYS

    @property
    def band(self) -> float:
        return TREND5_BAND

    @property
    def max_gross(self) -> float:
        return 1.0

    @property
    def registration(self) -> RegisteredRule:
        return REGISTERED["btc_trend5"]

    def weights(self, h: History) -> Mapping[str, float]:
        return {BTCUSDT: trend_votes(h.closes(BTCUSDT))}


@dataclass(frozen=True)
class BtcTrend5Vt:
    """btc_trend5_vt: btc_trend5 votes times the weekly 30% volatility-target overlay, band 0.10."""

    @property
    def name(self) -> str:
        return "btc_trend5_vt"

    @property
    def universe(self) -> tuple[str, ...]:
        return (BTCUSDT,)

    @property
    def warmup_days(self) -> int:
        return WARMUP_DAYS

    @property
    def band(self) -> float:
        return TREND5_VT_BAND

    @property
    def max_gross(self) -> float:
        return 1.0

    @property
    def registration(self) -> RegisteredRule:
        return REGISTERED["btc_trend5_vt"]

    def weights(self, h: History) -> Mapping[str, float]:
        scale = weekly_vol_scale(h.bars(BTCUSDT, TREND5_VT_RETURNS + 8), h.today)
        return {BTCUSDT: trend_votes(h.closes(BTCUSDT)) * scale}


@dataclass(frozen=True)
class BuyAndHold:
    """Hold 100% of one spot asset from the first evaluated day (benchmark)."""

    asset: str

    @property
    def name(self) -> str:
        return f"BH({self.asset})"

    @property
    def universe(self) -> tuple[str, ...]:
        return (self.asset,)

    @property
    def warmup_days(self) -> int:
        return WARMUP_DAYS

    @property
    def band(self) -> float:
        return BH_BAND

    @property
    def max_gross(self) -> float:
        return 1.0

    @property
    def registration(self) -> RegisteredRule | None:
        return REGISTERED["bh_btc"] if self.asset == BTCUSDT else None

    def weights(self, h: History) -> Mapping[str, float]:
        return {self.asset: 1.0}


def ported_rule(registered_name: str) -> Strategy:
    """The typed port of a registered rule, by its registered name."""
    rules: dict[str, Strategy] = {
        "bh_btc": BuyAndHold(BTCUSDT),
        "ens_btc": Ens(BTCUSDT),
        "ens_vt_btc": Ens(BTCUSDT, ENS_VOL_TARGET),
        "btc_trend5": BtcTrend5(),
        "btc_trend5_vt": BtcTrend5Vt(),
    }
    if registered_name not in rules:
        raise StrategyError(f"{registered_name!r} is not a ported rule")
    return rules[registered_name]


def registration_of(rule: Strategy) -> RegisteredRule | None:
    registration = getattr(rule, "registration", None)
    return registration if isinstance(registration, RegisteredRule) else None
