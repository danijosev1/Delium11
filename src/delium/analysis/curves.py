"""Pure scoring-curve math shared by the analysis engines.

No I/O, no config, no time — just deterministic functions. See
docs/analysis-engine.md §7 (curves) and §1 (demand formulas).
"""

from __future__ import annotations

from collections.abc import Sequence
from math import log10
from statistics import median


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def norm(x: float, lo: float, hi: float) -> float:
    """Linear map of x in [lo, hi] to 0-100, clamped."""
    if hi == lo:
        return 0.0
    return clamp((x - lo) / (hi - lo) * 100.0)


def log_norm(x: float, lo: float, hi: float) -> float:
    """Linear map in log10 space; x <= 0 returns 0."""
    if x <= 0:
        return 0.0
    return clamp((log10(x) - log10(lo)) / (log10(hi) - log10(lo)) * 100.0)


def plateau(
    x: float, rise_lo: float, rise_hi: float, fall_lo: float, fall_hi: float, floor: float = 0.0
) -> float:
    """Trapezoid sweet-spot: 0 below rise_lo, ramp to 100 by rise_hi, flat 100
    until fall_lo, decay to `floor` by fall_hi, floor beyond."""
    if x <= rise_lo:
        return 0.0
    if x < rise_hi:
        return (x - rise_lo) / (rise_hi - rise_lo) * 100.0
    if x <= fall_lo:
        return 100.0
    if x < fall_hi:
        return 100.0 - (x - fall_lo) / (fall_hi - fall_lo) * (100.0 - floor)
    return floor


def theil_sen(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Theil–Sen slope estimate: the median of all pairwise slopes. Robust to
    outliers (unlike OLS). Returns None if no valid pair (all x equal, < 2 pts)."""
    n = len(xs)
    slopes: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[j] - xs[i]
            if dx == 0:
                continue
            slopes.append((ys[j] - ys[i]) / dx)
    if not slopes:
        return None
    return median(slopes)
