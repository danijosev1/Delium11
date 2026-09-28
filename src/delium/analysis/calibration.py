"""Calibration harness (pure) — how well do Delium's estimates match Keepa?

Compares, per product and per category:
  * Delium's BSR-curve monthly-unit estimate vs Keepa `monthlySold`
  * Delium's fee-table FBA fulfilment fee vs Keepa `fbaFees.pickAndPackFee`

Keepa's `monthlySold` mirrors Amazon's *bucketed* "bought in past month" badge
(50+, 100+, 1K+, …): the number is the bucket FLOOR, so it means a RANGE, not a
point. We compare Delium's estimate against the bucket **range** — inside the
range is a hit (0 error), and error is only counted as the distance to the
nearest bucket edge. Suggested curve/fee adjustments are computed per category
with sample sizes, but **never applied** — this module only measures.

Pure: no DB, no network. The CLI supplies already-read values and the loaded
velocity curves + fee table.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from delium.analysis.amazon_buckets import (
    MONTHLY_SOLD_LADDER,
    monthly_sold_bucket,
)
from delium.analysis.amazon_buckets import (
    bucket_representative as _bucket_repr,
)
from delium.analysis.demand import curve_units_for_bsr
from delium.analysis.fees import fulfillment_fee, load_fee_table, select_size_tier
from delium.analysis.models import Dimensions, VelocityCurves

__all__ = [  # re-exported for callers/tests that imported them from here
    "MONTHLY_SOLD_LADDER",
    "monthly_sold_bucket",
]


# ---------------------------------------------------------------------------
# Per-product comparison
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class UnitComparison:
    keepa_monthly_sold: int | None
    bucket_low: int | None
    bucket_high: int | None
    delium_units: float | None
    contained: bool | None  # is Delium's estimate inside Keepa's bucket range?
    error_pct: float | None  # 0 inside the bucket; else distance to nearest edge
    ratio: float | None  # delium / bucket representative (>1 = Delium over-estimates)


@dataclass(frozen=True)
class FeeComparison:
    keepa_fee_cents: int | None
    delium_fee_cents: int | None
    error_pct: float | None
    ratio: float | None  # delium / keepa (>1 = Delium over-estimates the fee)


@dataclass(frozen=True)
class ProductCalibration:
    asin: str
    category: str | None
    units: UnitComparison
    fee: FeeComparison


def compare_units(delium_units: float | None, keepa_monthly_sold: int | None) -> UnitComparison:
    if keepa_monthly_sold is None or keepa_monthly_sold <= 0:
        return UnitComparison(keepa_monthly_sold, None, None, delium_units, None, None, None)
    low, high = monthly_sold_bucket(keepa_monthly_sold)
    if delium_units is None:
        return UnitComparison(keepa_monthly_sold, low, high, None, None, None, None)
    contained = delium_units >= low and (high is None or delium_units < high)
    if contained:
        error_pct = 0.0
    elif delium_units < low:
        error_pct = (low - delium_units) / low
    else:  # delium_units >= high (high is not None here)
        assert high is not None
        error_pct = (delium_units - high) / high
    repr_val = _bucket_repr(low, high)
    ratio = delium_units / repr_val if repr_val > 0 else None
    return UnitComparison(keepa_monthly_sold, low, high, delium_units, contained, error_pct, ratio)


def compare_fee(delium_fee_cents: int | None, keepa_fee_cents: int | None) -> FeeComparison:
    if not keepa_fee_cents or keepa_fee_cents <= 0 or delium_fee_cents is None:
        return FeeComparison(keepa_fee_cents, delium_fee_cents, None, None)
    error_pct = abs(delium_fee_cents - keepa_fee_cents) / keepa_fee_cents
    ratio = delium_fee_cents / keepa_fee_cents
    return FeeComparison(keepa_fee_cents, delium_fee_cents, error_pct, ratio)


# ---------------------------------------------------------------------------
# Delium's own estimates from stored facts (pure — inputs supplied by the CLI)
# ---------------------------------------------------------------------------
def delium_unit_estimate(
    curves: VelocityCurves, category: str | None, current_bsr: int | None
) -> float | None:
    if current_bsr is None or current_bsr <= 0:
        return None
    return round(curve_units_for_bsr(curves, category, current_bsr), 1)


def delium_fee_estimate(
    category: str | None, dims: Dimensions | None, weight_g: int | None
) -> int | None:
    if dims is None or weight_g is None or weight_g <= 0:
        return None
    table = load_fee_table()
    tier = select_size_tier(table, dims, weight_g)
    return fulfillment_fee(tier, weight_g)


# ---------------------------------------------------------------------------
# Aggregation + suggestions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CategorySuggestion:
    category: str
    sample_size: int
    unit_median_ratio: float | None  # >1 → Delium over-estimates units
    suggested_curve_scale: float | None  # multiply the category curve by this
    unit_bucket_hit_rate: float | None  # share of products inside Keepa's bucket
    fee_median_ratio: float | None  # delium/keepa
    suggested_fee_scale: float | None  # multiply Delium's fulfilment fee by this
    note: str


@dataclass(frozen=True)
class CalibrationReport:
    products: tuple[ProductCalibration, ...]
    units_mape: float | None  # mean absolute % error (bucket-aware) across products
    fee_mape: float | None
    unit_bucket_hit_rate: float | None
    suggestions: tuple[CategorySuggestion, ...]

    @property
    def sample_size(self) -> int:
        return len(self.products)


def _mape(values: list[float]) -> float | None:
    return round(100.0 * sum(values) / len(values), 1) if values else None


def build_report(products: list[ProductCalibration]) -> CalibrationReport:
    """Aggregate per-product comparisons into MAPE + per-category suggested
    (never applied) adjustments."""
    unit_errs = [p.units.error_pct for p in products if p.units.error_pct is not None]
    fee_errs = [p.fee.error_pct for p in products if p.fee.error_pct is not None]
    contained = [p.units.contained for p in products if p.units.contained is not None]
    hit_rate = (sum(1 for c in contained if c) / len(contained)) if contained else None

    by_cat: dict[str, list[ProductCalibration]] = {}
    for p in products:
        by_cat.setdefault(_seg(p.category), []).append(p)

    suggestions = tuple(_suggest(cat, rows) for cat, rows in sorted(by_cat.items()) if rows)
    return CalibrationReport(
        products=tuple(products),
        units_mape=_mape(unit_errs),
        fee_mape=_mape(fee_errs),
        unit_bucket_hit_rate=(round(hit_rate, 3) if hit_rate is not None else None),
        suggestions=suggestions,
    )


def _suggest(category: str, rows: list[ProductCalibration]) -> CategorySuggestion:
    unit_ratios = [r.units.ratio for r in rows if r.units.ratio is not None]
    fee_ratios = [r.fee.ratio for r in rows if r.fee.ratio is not None]
    contained = [r.units.contained for r in rows if r.units.contained is not None]

    umr = median(unit_ratios) if unit_ratios else None
    fmr = median(fee_ratios) if fee_ratios else None
    hit = (sum(1 for c in contained if c) / len(contained)) if contained else None
    # To remove bias, scale so the median ratio would become ~1.
    curve_scale = round(1.0 / umr, 3) if umr and umr > 0 else None
    fee_scale = round(1.0 / fmr, 3) if fmr and fmr > 0 else None
    note = (
        f"{len(rows)} sample(s); "
        + (f"units median ratio {umr:.2f} (→ ×{curve_scale} curve)" if umr else "no unit data")
        + (f"; fee median ratio {fmr:.2f} (→ ×{fee_scale} fee)" if fmr else "")
        + " — SUGGESTED ONLY, not applied."
    )
    return CategorySuggestion(
        category=category,
        sample_size=len(rows),
        unit_median_ratio=(round(umr, 3) if umr else None),
        suggested_curve_scale=curve_scale,
        unit_bucket_hit_rate=(round(hit, 3) if hit is not None else None),
        fee_median_ratio=(round(fmr, 3) if fmr else None),
        suggested_fee_scale=fee_scale,
        note=note,
    )


def _seg(category: str | None) -> str:
    if not category:
        return "(uncategorized)"
    return category.split(">")[0].strip() or category.strip()
