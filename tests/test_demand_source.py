"""Fix A — Keepa monthlySold is the PRIMARY monthly-units source; the BSR curve
is the fallback and no longer has an artificial ceiling."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import discovery_support as seed
from delium.analysis.demand import (
    _curve_units,
    _sales_estimate,
    load_velocity_curves,
)
from delium.analysis.models import AsinHistory, BsrPoint, Confidence, DemandConfig

CURVES = load_velocity_curves()
HK = CURVES.categories["Home & Kitchen"]
CFG = DemandConfig()
AS_OF = date(2026, 8, 1)


def _history(
    asin: str, series: list[tuple[int, int]], monthly_sold: int | None = None
) -> AsinHistory:
    return AsinHistory(
        asin,
        tuple(BsrPoint(AS_OF - timedelta(days=d), b) for d, b in series),
        monthly_sold=monthly_sold,
    )


# ---------------------------------------------------------------------------
# No ceiling — the curve extrapolates log-log past the anchors
# ---------------------------------------------------------------------------
def test_curve_has_no_low_bsr_ceiling() -> None:
    anchors = HK.anchors
    ceiling = anchors[0].units  # the old hard cap
    # A best-seller far below the first anchor must read WAY above the old ceiling.
    hot = _curve_units(anchors, 5)
    assert hot > ceiling * 3, f"expected extrapolation above the old ceiling, got {hot}"
    # Strictly monotonic: lower BSR → more units.
    assert (
        _curve_units(anchors, 5) > _curve_units(anchors, 50) > _curve_units(anchors, anchors[0].bsr)
    )


def test_curve_extrapolates_below_floor_without_zero() -> None:
    anchors = HK.anchors
    huge_bsr = anchors[-1].bsr * 10
    tail = _curve_units(anchors, huge_bsr)
    assert 0 < tail <= anchors[-1].units  # extends down, never zero/negative


# ---------------------------------------------------------------------------
# monthlySold present → amazon bucketed, HIGH confidence
# ---------------------------------------------------------------------------
def test_monthly_sold_is_primary_and_bucketed() -> None:
    # Keepa says 30,000/mo. Bucket (30000, 40000) → repr ~34,641.
    h = _history("B0AMZ00001", [(30, 1500), (0, 1400)], monthly_sold=30000)
    est = _sales_estimate(h, AS_OF, HK, category_known=True, cfg=CFG)
    assert est is not None
    assert est.method == "amazon_bucketed"
    assert est.confidence is Confidence.HIGH
    assert est.units_source == "amazon (bucketed)"
    assert est.low_units == 30000
    assert est.high_units >= 40000 or est.high_units >= est.expected_units
    assert 30000 <= est.expected_units <= 40000  # inside the bucket range


def test_monthly_sold_low_bucket() -> None:
    h = _history("B0AMZ00002", [(30, 5000), (0, 4800)], monthly_sold=100)
    est = _sales_estimate(h, AS_OF, HK, category_known=True, cfg=CFG)
    assert est is not None and est.method == "amazon_bucketed"
    assert est.low_units == 100 and est.high_units >= 200
    assert 100 <= est.expected_units < 200  # geometric repr ~141


def test_monthly_sold_beats_curve_when_curve_would_underestimate() -> None:
    # A high-BSR product the curve would read low, but Amazon says 300/mo.
    h = _history("B0AMZ00003", [(30, 40000), (0, 40000)], monthly_sold=300)
    est = _sales_estimate(h, AS_OF, HK, category_known=True, cfg=CFG)
    assert est is not None and est.method == "amazon_bucketed"
    assert est.expected_units >= 300  # not the tiny curve/rank-drop figure


# ---------------------------------------------------------------------------
# monthlySold absent → estimated fallback, lower confidence, "estimated" source
# ---------------------------------------------------------------------------
def test_absent_monthly_sold_falls_back_to_estimate() -> None:
    h = _history("B0EST00001", [(60, 1600), (30, 1500), (0, 1400)])  # improving history
    est = _sales_estimate(h, AS_OF, HK, category_known=True, cfg=CFG)
    assert est is not None
    assert est.method in {"rank_drop", "curve_fallback"}
    assert est.units_source.startswith("estimated")
    assert not est.is_amazon_source


def test_thin_history_no_monthly_sold_is_low_confidence_curve() -> None:
    h = _history("B0EST00002", [(0, 1500)])  # single point → curve fallback
    est = _sales_estimate(h, AS_OF, HK, category_known=True, cfg=CFG)
    assert est is not None
    assert est.method == "curve_fallback"
    assert est.confidence is Confidence.LOW
    assert est.units_source == "estimated (illustrative curve)"


# ---------------------------------------------------------------------------
# Variation handling: monthlySold read per stored product; family counted once
# via parent dedup (assembly build_demand path)
# ---------------------------------------------------------------------------
def test_build_demand_reads_monthly_sold_from_product(initialized_db: Path) -> None:
    from delium.database import get_connection, repository
    from delium.discovery.assembly import build_demand

    with get_connection() as conn:
        run_id = seed.new_run(conn)
        fid = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:US:B0FAM00001",
            payload={"asin": "B0FAM00001"},
        )
        repository.upsert_product(
            conn,
            asin="B0FAM00001",
            fetch_id=fid,
            marketplace="US",
            title="Tray",
            category_path="Home & Kitchen > Storage",
            parent_asin="B0PARENTAA",
            monthly_sold=5000,
        )
        for day, b in (("2025-06-01", 1600), ("2025-08-01", 1500)):
            repository.upsert_price_bsr_history(
                conn, asin="B0FAM00001", captured_on=day, price_cents=2999, bsr=b
            )
        demand = build_demand(conn, "B0FAM00001", "US", "Home & Kitchen > Storage", [])
    assert demand is not None
    est = next(e for e in demand.sales_estimates if e.asin == "B0FAM00001")
    assert est.method == "amazon_bucketed"
    assert est.low_units == 5000  # the family's Amazon figure, used directly
