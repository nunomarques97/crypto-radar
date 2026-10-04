"""Performance metrics and the Deflated Sharpe Ratio of the trend research.

Ported from the 2026-10-03 feasibility harness (``harness/metrics.py``) and ``analyze.stats``, whose
conventions it keeps: daily simple open-to-open returns, 365-day years, sample standard deviation,
``pos12m`` as the share of rolling 365-day windows with a positive return (NaN when the series is
not longer than 365 days) and the Deflated Sharpe Ratio of Bailey & Lopez de Prado (2014).

Pure: no I/O, no clock. Values are floats, not Decimal, on purpose: the parity tests reproduce the
reference implementations to 1e-9, which requires the same float operations in the same order
(Python's ``sum`` included). These are research statistics, never money amounts.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist

EULER_GAMMA = 0.5772156649015329
DAYS_PER_YEAR = 365


class TrendMetricsError(ValueError):
    """A metric was requested on input it cannot be computed from."""


@dataclass(frozen=True)
class Performance:
    """Metrics of one daily return series. ``mdd`` is <= 0 and ``max_dd`` is its magnitude."""

    days: int
    start: date
    end: date
    final_multiple: float
    cagr: float
    mdd: float
    max_dd: float
    sharpe: float
    daily_sharpe: float
    sortino: float
    pos12m: float
    turnover_yr: float
    avg_exposure: float
    worst_year: float
    year_returns: Mapping[int, float]
    skew: float
    kurtosis: float


def _moments(rets: Sequence[float]) -> tuple[float, float, float, float]:
    n = len(rets)
    mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    if sd == 0:
        return mu, 0.0, 0.0, 3.0
    m2 = sum((r - mu) ** 2 for r in rets) / n
    skew = sum((r - mu) ** 3 for r in rets) / n / m2**1.5
    kurt = sum((r - mu) ** 4 for r in rets) / n / m2**2
    return mu, sd, skew, kurt


def performance(
    rets: Sequence[float], days: Sequence[date], turnover: float = 0.0, exposure_sum: float = 0.0
) -> Performance:
    """Daily simple returns (open to open) and their days -> metrics."""
    n = len(rets)
    if n < 2:
        raise TrendMetricsError("need at least two daily returns")
    if len(days) != n:
        raise TrendMetricsError(f"{n} returns but {len(days)} days")
    if not all(math.isfinite(r) for r in rets):
        raise TrendMetricsError("non-finite daily return")
    eq, peak, mdd = 1.0, 1.0, 0.0
    curve: list[float] = []
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        curve.append(eq)
    yrs = n / DAYS_PER_YEAR
    cagr = eq ** (1 / yrs) - 1 if eq > 0 else -1.0
    mu, sd, skew, kurt = _moments(rets)
    sd = sd or 1e-12
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / n) or 1e-12
    pos = tot = 0
    for i in range(DAYS_PER_YEAR, n):
        tot += 1
        pos += curve[i] / curve[i - DAYS_PER_YEAR] > 1
    years: dict[int, float] = {}
    for d, r in zip(days, rets, strict=True):
        years[d.year] = years.get(d.year, 1.0) * (1 + r)
    year_returns = {y: v - 1 for y, v in sorted(years.items())}
    return Performance(
        days=n,
        start=days[0],
        end=days[-1],
        final_multiple=eq,
        cagr=cagr,
        mdd=mdd,
        max_dd=-mdd,
        sharpe=mu / sd * math.sqrt(DAYS_PER_YEAR),
        daily_sharpe=mu / sd,
        sortino=mu / downside * math.sqrt(DAYS_PER_YEAR),
        pos12m=pos / tot if tot else float("nan"),
        turnover_yr=turnover / yrs,
        avg_exposure=exposure_sum / n,
        worst_year=min(year_returns.values()),
        year_returns=year_returns,
        skew=skew,
        kurtosis=kurt,
    )


def expected_max_sharpe(n_trials: int, var_sharpe: float) -> float:
    """E[max SR] of ``n_trials`` zero-skill strategies whose Sharpe estimates have variance ``var_sharpe``."""
    if n_trials <= 1 or var_sharpe <= 0:
        return 0.0
    z = NormalDist()
    return math.sqrt(var_sharpe) * (
        (1 - EULER_GAMMA) * z.inv_cdf(1 - 1 / n_trials) + EULER_GAMMA * z.inv_cdf(1 - 1 / (n_trials * math.e))
    )


def deflated_sharpe(
    daily_sr: float, n_obs: int, skew: float, kurt: float, n_trials: int, var_sharpe: float
) -> tuple[float, float]:
    """(probability that the true daily Sharpe exceeds the best expected from luck, that hurdle)."""
    sr0 = expected_max_sharpe(n_trials, var_sharpe)
    denom = 1 - skew * daily_sr + (kurt - 1) / 4 * daily_sr**2
    if denom <= 0 or n_obs < 2:
        return float("nan"), sr0
    return NormalDist().cdf((daily_sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom)), sr0


def sample_variance(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def trial_sharpe_variance(sharpes: Sequence[float], n_obs: int, *, floor: bool = True) -> float:
    """Variance of the logged daily Sharpes across trials, floored at 1/T.

    1/T is the sampling variance of a zero-skill daily Sharpe over T days; without the floor a
    handful of similar trials would make the hurdle almost 0. ``floor=False`` reproduces only the
    registry events written before the harness adopted the floor (seq 4-6 of the imported registry).
    """
    if n_obs < 1:
        raise TrendMetricsError("the variance floor needs at least one observation")
    observed = sample_variance(sharpes)
    return max(observed, 1.0 / n_obs) if floor else observed
