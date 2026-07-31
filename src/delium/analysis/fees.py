"""Amazon fee engine — deterministic, table-driven.

Every fee schedule lives in an external data file (`fee_tables/*.toml`); this
module contains only the lookup/interpolation logic. Nothing here is hardcoded
that Amazon can change (rates, bands, category percentages, storage). No LLM,
no I/O beyond loading the named fee table. See docs/analysis-engine.md §4.1.
"""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import Any

from delium.analysis.models import (
    Dimensions,
    FeeBreakdown,
    FeeTable,
    FulfillmentBand,
    SizeTier,
)

FEE_TABLE_DIR = Path(__file__).parent / "fee_tables"
DEFAULT_FEE_TABLE = "us-2026"

_INCH_MM = 25.4
_CUBIC_INCHES_PER_FOOT = 1728.0


class FeeError(Exception):
    """Raised when fees cannot be computed (e.g. missing/oversized dimensions).

    This is the intentional *blocking* behavior: profitability cannot be
    computed without real physical dimensions (docs/analysis-engine.md §4)."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_fee_table(version: str = DEFAULT_FEE_TABLE) -> FeeTable:
    path = FEE_TABLE_DIR / f"{version}.toml"
    if not path.exists():
        raise FeeError(f"Fee table {version!r} not found at {path}.")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return _parse_fee_table(raw)


def _parse_fee_table(raw: dict[str, Any]) -> FeeTable:
    referral = raw["referral"]
    closing = raw.get("closing", {})
    storage = raw["storage"]
    prep = raw["prep"]

    tiers: list[SizeTier] = []
    for tier in raw["size_tiers"]:
        bands = tuple(
            FulfillmentBand(max_weight_g=int(b["max_weight_g"]), fee_cents=int(b["fee_cents"]))
            for b in tier["bands"]
        )
        overflow = tier.get("overflow")
        tiers.append(
            SizeTier(
                name=str(tier["name"]),
                max_weight_g=int(tier["max_weight_g"]),
                max_longest_mm=int(tier["max_longest_mm"]),
                max_median_mm=int(tier["max_median_mm"]),
                max_shortest_mm=int(tier["max_shortest_mm"]),
                bands=bands,
                overflow_base_cents=int(overflow["base_cents"]) if overflow else None,
                overflow_per_kg_cents=int(overflow["per_kg_cents"]) if overflow else None,
            )
        )

    return FeeTable(
        version=str(raw["version"]),
        effective_date=str(raw["effective_date"]),
        referral_default_percent=float(referral["default_percent"]),
        referral_min_fee_cents=int(referral["min_fee_cents"]),
        referral_categories={str(k): float(v) for k, v in referral.get("categories", {}).items()},
        closing_default_cents=int(closing.get("default_cents", 0)),
        closing_categories={str(k): int(v) for k, v in closing.get("categories", {}).items()},
        storage_standard_per_cf_cents=int(storage["standard_per_cf_cents"]),
        storage_peak_per_cf_cents=int(storage["peak_per_cf_cents"]),
        prep_default_cents=int(prep["default_per_unit_cents"]),
        size_tiers=tuple(tiers),
    )


# ---------------------------------------------------------------------------
# Individual fees
# ---------------------------------------------------------------------------
def select_size_tier(table: FeeTable, dims: Dimensions, weight_g: int) -> SizeTier:
    longest, median, shortest = dims.sorted_desc()
    for tier in table.size_tiers:
        if (
            weight_g <= tier.max_weight_g
            and longest <= tier.max_longest_mm
            and median <= tier.max_median_mm
            and shortest <= tier.max_shortest_mm
        ):
            return tier
    raise FeeError(
        f"Product exceeds all size tiers (dims={dims}, weight={weight_g}g) — "
        "oversized/special handling, not modeled."
    )


def fulfillment_fee(tier: SizeTier, weight_g: int) -> int:
    for band in tier.bands:
        if weight_g <= band.max_weight_g:
            return band.fee_cents
    # Beyond the last band: overflow = base + per_kg over the last band's ceiling.
    if tier.overflow_base_cents is not None and tier.overflow_per_kg_cents is not None:
        last_max = tier.bands[-1].max_weight_g
        extra_kg = math.ceil(max(0, weight_g - last_max) / 1000)
        return tier.overflow_base_cents + tier.overflow_per_kg_cents * extra_kg
    raise FeeError(f"No fulfillment band for weight {weight_g}g in tier {tier.name!r}.")


def referral_fee(table: FeeTable, category: str | None, price_cents: int) -> int:
    percent = table.referral_categories.get(category or "", table.referral_default_percent)
    fee = round(price_cents * percent)
    return max(fee, table.referral_min_fee_cents)


def closing_fee(table: FeeTable, category: str | None) -> int:
    return table.closing_categories.get(category or "", table.closing_default_cents)


def cubic_feet(dims: Dimensions) -> float:
    cubic_inches = (
        (dims.length_mm / _INCH_MM) * (dims.width_mm / _INCH_MM) * (dims.height_mm / _INCH_MM)
    )
    return cubic_inches / _CUBIC_INCHES_PER_FOOT


def storage_fee_monthly(table: FeeTable, dims: Dimensions, *, peak: bool = False) -> int:
    rate = table.storage_peak_per_cf_cents if peak else table.storage_standard_per_cf_cents
    return round(cubic_feet(dims) * rate)


def blended_storage_monthly(table: FeeTable, dims: Dimensions) -> int:
    """9 standard months + 3 peak months, averaged to one monthly figure."""
    standard = storage_fee_monthly(table, dims, peak=False)
    peak = storage_fee_monthly(table, dims, peak=True)
    return round((9 * standard + 3 * peak) / 12)


# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------
def compute_fees(
    table: FeeTable,
    *,
    category: str | None,
    price_cents: int,
    dims: Dimensions | None,
    weight_g: int | None,
    prep_cost_cents: int | None = None,
) -> FeeBreakdown:
    """Full per-unit Amazon fee breakdown. Missing dims/weight is blocking."""
    if dims is None or weight_g is None or weight_g <= 0:
        raise FeeError("Dimensions and a positive weight are required to compute fees.")

    tier = select_size_tier(table, dims, weight_g)
    return FeeBreakdown(
        referral_cents=referral_fee(table, category, price_cents),
        fulfillment_cents=fulfillment_fee(tier, weight_g),
        closing_cents=closing_fee(table, category),
        storage_monthly_cents=blended_storage_monthly(table, dims),
        storage_peak_cents=storage_fee_monthly(table, dims, peak=True),
        prep_cents=prep_cost_cents if prep_cost_cents is not None else table.prep_default_cents,
        size_tier=tier.name,
        fee_table_version=table.version,
    )
