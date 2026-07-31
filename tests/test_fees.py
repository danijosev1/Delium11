"""Fee engine tests. The fee-table values are the source of truth; these assert
the engine selects the right tier/band/percentage and combines them correctly.

Where a fixture is labeled 'calculator example', the expected number is derived
directly from the illustrative us-2026 table (referral % × price, the weight
band, etc.) — the same arithmetic Amazon's Revenue Calculator performs."""

from __future__ import annotations

import pytest

from delium.analysis.fees import (
    FeeError,
    blended_storage_monthly,
    closing_fee,
    compute_fees,
    cubic_feet,
    fulfillment_fee,
    load_fee_table,
    referral_fee,
    select_size_tier,
)
from delium.analysis.models import Dimensions

TABLE = load_fee_table()


# --- loading --------------------------------------------------------------
def test_table_loads_with_version_and_tiers() -> None:
    assert TABLE.version == "us-2026"
    assert TABLE.effective_date == "2026-01-01"
    assert [t.name for t in TABLE.size_tiers] == [
        "small_standard",
        "large_standard",
        "large_bulky",
    ]


def test_unknown_table_raises() -> None:
    with pytest.raises(FeeError):
        load_fee_table("does-not-exist")


# --- referral -------------------------------------------------------------
def test_referral_uses_category_percent() -> None:
    # Home & Kitchen 15% of $24.99 = $3.75 (rounded from 374.85).
    assert referral_fee(TABLE, "Home & Kitchen", 2499) == 375


def test_referral_electronics_lower_percent() -> None:
    # Electronics 8% of $50.00 = $4.00.
    assert referral_fee(TABLE, "Electronics", 5000) == 400


def test_referral_falls_back_to_default() -> None:
    assert referral_fee(TABLE, "Unlisted Category", 2000) == 300  # 15% default
    assert referral_fee(TABLE, None, 2000) == 300


def test_referral_min_fee_applied() -> None:
    # 15% of $1.00 = 15c, but the $0.30 minimum wins.
    assert referral_fee(TABLE, "Home & Kitchen", 100) == 30


# --- closing --------------------------------------------------------------
def test_closing_fee_media_and_default() -> None:
    assert closing_fee(TABLE, "Books") == 180
    assert closing_fee(TABLE, "Home & Kitchen") == 0
    assert closing_fee(TABLE, None) == 0


# --- size tier + fulfillment ---------------------------------------------
def test_small_standard_selected_and_priced() -> None:
    dims = Dimensions(200, 150, 15)
    tier = select_size_tier(TABLE, dims, 300)  # 300g
    assert tier.name == "small_standard"
    # 300g → first band with max_weight_g >= 300 is the 340g band (334c).
    assert fulfillment_fee(tier, 300) == 334


def test_small_standard_band_boundaries() -> None:
    tier = select_size_tier(TABLE, Dimensions(200, 150, 15), 100)
    assert fulfillment_fee(tier, 113) == 306  # exactly 4oz → first band
    assert fulfillment_fee(tier, 114) == 315  # just over → next band


def test_large_standard_selected_by_dimension() -> None:
    # Light but long → too long for small standard, falls to large standard.
    dims = Dimensions(420, 300, 100)
    tier = select_size_tier(TABLE, dims, 300)
    assert tier.name == "large_standard"
    assert fulfillment_fee(tier, 300) == 398  # 340g band


def test_large_standard_overflow() -> None:
    tier = select_size_tier(TABLE, Dimensions(420, 300, 100), 2000)
    # 2000g is past the last band (1361g): base 640 + 16 * ceil((2000-1361)/1000=1) = 656.
    assert fulfillment_fee(tier, 2000) == 656


def test_oversized_raises() -> None:
    with pytest.raises(FeeError):
        select_size_tier(TABLE, Dimensions(3000, 1000, 1000), 30000)


# --- storage --------------------------------------------------------------
def test_cubic_feet_and_storage() -> None:
    # 300 × 200 × 100 mm ≈ 0.2118 cubic feet.
    dims = Dimensions(300, 200, 100)
    assert cubic_feet(dims) == pytest.approx(0.2118, abs=0.001)
    # Blended = (9*std + 3*peak)/12; std=round(0.2118*87)=18, peak=round(0.2118*240)=51.
    assert blended_storage_monthly(TABLE, dims) == round((9 * 18 + 3 * 51) / 12)


# --- combined + blocking --------------------------------------------------
def test_compute_fees_full_breakdown() -> None:
    fees = compute_fees(
        TABLE,
        category="Home & Kitchen",
        price_cents=2499,
        dims=Dimensions(200, 150, 15),
        weight_g=300,
    )
    assert fees.referral_cents == 375
    assert fees.fulfillment_cents == 334
    assert fees.closing_cents == 0
    assert fees.prep_cents == 50  # table default
    assert fees.size_tier == "small_standard"
    assert fees.fee_table_version == "us-2026"
    # amazon_fees = referral + fulfillment + closing + storage
    assert fees.amazon_fees_cents == 375 + 334 + 0 + fees.storage_monthly_cents


def test_compute_fees_prep_override() -> None:
    fees = compute_fees(
        TABLE,
        category="Home & Kitchen",
        price_cents=2499,
        dims=Dimensions(200, 150, 15),
        weight_g=300,
        prep_cost_cents=120,
    )
    assert fees.prep_cents == 120


def test_missing_dims_is_blocking() -> None:
    with pytest.raises(FeeError):
        compute_fees(TABLE, category="Home & Kitchen", price_cents=2499, dims=None, weight_g=300)
    with pytest.raises(FeeError):
        compute_fees(
            TABLE,
            category="Home & Kitchen",
            price_cents=2499,
            dims=Dimensions(200, 150, 15),
            weight_g=None,
        )
