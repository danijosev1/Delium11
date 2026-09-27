"""Calibration harness (pure + orchestration) — bucketed monthlySold, fee compare,
MAPE, suggestions, and a stored-data run over a seeded DB."""

from __future__ import annotations

from pathlib import Path

import discovery_support as seed
from delium.analysis.calibration import (
    ProductCalibration,
    build_report,
    compare_fee,
    compare_units,
    monthly_sold_bucket,
)
from delium.analysis.demand import load_velocity_curves
from delium.database import get_connection, repository


# ---------------------------------------------------------------------------
# Bucketed monthlySold
# ---------------------------------------------------------------------------
def test_bucket_ranges() -> None:
    assert monthly_sold_bucket(100) == (100, 200)
    assert monthly_sold_bucket(1000) == (1000, 2000)
    assert monthly_sold_bucket(50) == (50, 100)
    assert monthly_sold_bucket(100000) == (100000, None)  # open-ended top
    assert monthly_sold_bucket(30) == (30, 50)  # below smallest tracked floor


def test_compare_units_contained_is_zero_error() -> None:
    # Keepa says "100+" (bucket 100..200). Delium estimates 150 → inside → 0 error.
    c = compare_units(150, 100)
    assert c.contained is True
    assert c.error_pct == 0.0
    assert c.bucket_low == 100 and c.bucket_high == 200


def test_compare_units_out_of_bucket_uses_nearest_edge() -> None:
    below = compare_units(60, 100)  # bucket 100..200; 60 < 100
    assert below.contained is False
    assert below.error_pct == (100 - 60) / 100  # distance to low edge
    above = compare_units(260, 100)  # 260 >= 200
    assert above.contained is False
    assert above.error_pct == (260 - 200) / 200


def test_compare_units_missing() -> None:
    assert compare_units(None, 100).contained is None
    assert compare_units(150, None).keepa_monthly_sold is None


def test_compare_fee() -> None:
    f = compare_fee(500, 400)
    assert f.ratio == 1.25
    assert abs(f.error_pct - 0.25) < 1e-9
    assert compare_fee(None, 400).ratio is None
    assert compare_fee(500, 0).ratio is None


def test_build_report_mape_and_suggestions() -> None:
    rows = [
        ProductCalibration("B1", "Home", compare_units(150, 100), compare_fee(500, 400)),
        ProductCalibration("B2", "Home", compare_units(60, 100), compare_fee(300, 400)),
    ]
    report = build_report(rows)
    assert report.sample_size == 2
    # One in-bucket (0), one out (0.4) → MAPE 20%.
    assert report.units_mape == 20.0
    assert report.unit_bucket_hit_rate == 0.5
    home = next(s for s in report.suggestions if s.category == "Home")
    assert home.sample_size == 2
    assert home.suggested_curve_scale is not None
    assert home.suggested_fee_scale is not None


def test_build_report_empty() -> None:
    report = build_report([])
    assert report.sample_size == 0
    assert report.units_mape is None and report.fee_mape is None


# ---------------------------------------------------------------------------
# Orchestration over a seeded DB (stored data only, no network)
# ---------------------------------------------------------------------------
def test_run_over_stored_products(initialized_db: Path) -> None:
    from delium.discovery import calibrate as calib

    with get_connection() as conn:
        run_id = seed.new_run(conn)
        # Seed a product with dims/weight + Keepa ground truth (monthlySold + fee).
        fid = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:US:B0CAL00001",
            payload={"asin": "B0CAL00001"},
        )
        repository.upsert_product(
            conn,
            asin="B0CAL00001",
            fetch_id=fid,
            marketplace="US",
            title="Tray",
            category_path="Home & Kitchen > Storage",
            dims={"length_mm": 200, "width_mm": 150, "height_mm": 50},
            weight_g=300,
            monthly_sold=100,
            fba_pick_pack_cents=450,
        )
        repository.upsert_price_bsr_history(
            conn, asin="B0CAL00001", captured_on="2025-07-01", price_cents=2999, bsr=5000
        )
        # A product with no Keepa ground truth is skipped.
        seed.seed_product(conn, run_id, "B0NOGT0001", "US")

    with get_connection() as conn:
        report = calib.run(conn, asins=None, marketplace="US")
    asins = {p.asin for p in report.products}
    assert "B0CAL00001" in asins
    assert "B0NOGT0001" not in asins  # no monthlySold/fee → skipped
    row = next(p for p in report.products if p.asin == "B0CAL00001")
    assert row.units.keepa_monthly_sold == 100
    assert row.units.delium_units is not None  # BSR 5000 → curve estimate
    assert row.fee.keepa_fee_cents == 450
    assert row.fee.delium_fee_cents is not None


def test_curve_units_for_bsr_is_deterministic() -> None:
    curves = load_velocity_curves()
    a = calib_units(curves, "Home & Kitchen", 5000)
    b = calib_units(curves, "Home & Kitchen", 5000)
    assert a == b and a > 0


def calib_units(curves: object, category: str, bsr: int) -> float:
    from delium.analysis.demand import curve_units_for_bsr
    from delium.analysis.models import VelocityCurves

    assert isinstance(curves, VelocityCurves)
    return curve_units_for_bsr(curves, category, bsr)
