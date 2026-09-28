"""Amazon "bought in past month" buckets (pure).

Keepa's `monthlySold` mirrors Amazon's displayed "bought in past month" badge,
which is **bucketed** (50+, 100+, 1K+, …): the number Keepa returns is the bucket
FLOOR, so it means a RANGE, not a point. This module maps a floor to its range
and to a single representative value. Shared by the demand pillar (primary units
source) and the calibration harness (bucket-aware error).
"""

from __future__ import annotations

# Amazon's displayed "bought in past month" bucket floors. Keepa's `monthlySold`
# returns one of these (or a value that rounds down to one). ILLUSTRATIVE — the
# buckets widen as they climb; verify against live data.
MONTHLY_SOLD_LADDER: tuple[int, ...] = (
    50,
    100,
    200,
    300,
    400,
    500,
    600,
    700,
    800,
    900,
    1000,
    2000,
    3000,
    4000,
    5000,
    6000,
    7000,
    8000,
    9000,
    10000,
    20000,
    30000,
    40000,
    50000,
    100000,
)


def monthly_sold_bucket(value: int) -> tuple[int, int | None]:
    """(low, high) for a Keepa `monthlySold` floor. `high` is the next bucket
    floor (exclusive), or None for the open-ended top bucket. A value below the
    smallest tracked floor is treated as [value, first_floor)."""
    ladder = MONTHLY_SOLD_LADDER
    if value < ladder[0]:
        return value, ladder[0]
    for lo, hi in zip(ladder, ladder[1:], strict=False):
        if lo <= value < hi:
            return lo, hi
    return ladder[-1], None  # top open-ended bucket


def bucket_representative(low: int, high: int | None) -> float:
    """A single representative value for a bucket: the geometric mean of the
    edges, or ~1.5× the floor for the open-ended top. Geometric (not arithmetic)
    because the buckets span a wide multiplicative range."""
    if high is None:
        return low * 1.5
    return float((low * high) ** 0.5)
