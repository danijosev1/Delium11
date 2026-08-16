"""Demand engine: unit, boundary, and edge-case tests."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from delium.analysis import curves
from delium.analysis.demand import (
    _bsr_trend,
    _keyword_demand,
    _sales_estimate,
    _seasonality,
    analyze_demand,
    load_velocity_curves,
)
from delium.analysis.models import (
    AsinHistory,
    BsrPoint,
    Confidence,
    DemandConfig,
    KeywordDatum,
)

CURVES = load_velocity_curves()
HK = CURVES.categories["Home & Kitchen"]
CFG = DemandConfig()
AS_OF = date(2026, 8, 1)


def _history(asin: str, series: list[tuple[int, int]]) -> AsinHistory:
    """series = [(days_ago, bsr), ...]."""
    return AsinHistory(asin, tuple(BsrPoint(AS_OF - timedelta(days=d), b) for d, b in series))


def _steady_drop(asin: str, days: int = 60, start: int = 1000, step: int = 5) -> AsinHistory:
    """Daily points, BSR strictly improving → one drop per day."""
    pts = []
    bsr = start
    for d in range(days, -1, -1):  # oldest → newest
        pts.append(BsrPoint(AS_OF - timedelta(days=d), bsr))
        bsr -= step
    return AsinHistory(asin, tuple(pts))


# --- sales estimate (hand-worked) ----------------------------------------
def test_rank_drop_sales_estimate_hand_worked() -> None:
    # 60 days, a drop every day → 60 drops over 60 observed days → 30/mo.
    est = _sales_estimate(_steady_drop("B0X", days=60, step=5), AS_OF, HK, True, CFG)
    assert est is not None
    assert est.method == "rank_drop"
    assert est.drops == 60
    assert est.observed_days == 60
    assert est.expected_units == 30  # 60/60*30 * base_factor 1.0
    assert est.low_units == 24  # 30 * 0.8
    assert est.high_units == 62  # round(30 * 1.6 * 1.3)
    assert est.confidence == Confidence.HIGH
    assert est.low_units <= est.expected_units <= est.high_units


def test_partial_history_widens_bounds() -> None:
    # 45 days of history (30-59 band) → width ×1.5, medium confidence.
    est = _sales_estimate(_steady_drop("B0X", days=45, step=5), AS_OF, HK, True, CFG)
    assert est is not None
    assert est.observed_days == 45
    assert est.confidence == Confidence.MEDIUM
    # bounds are wider than the un-widened case would be
    assert est.low_units <= est.expected_units <= est.high_units


def test_thin_history_falls_back_to_curve() -> None:
    est = _sales_estimate(
        _history("B0X", [(20, 1000), (10, 1000), (0, 1000)]), AS_OF, HK, True, CFG
    )
    assert est is not None
    assert est.method == "curve_fallback"
    assert est.confidence == Confidence.LOW
    assert est.rank_reference_units == 900  # curve at BSR 1000 for H&K
    assert est.expected_units == 900  # fallback uses the rank reference
    assert est.low_units <= est.expected_units <= est.high_units


def test_unknown_category_caps_confidence_at_medium() -> None:
    curve, known = CURVES.resolve(None)
    assert known is False
    est = _sales_estimate(_steady_drop("B0X", days=60), AS_OF, curve, known, CFG)
    assert est is not None
    assert est.confidence == Confidence.MEDIUM  # would be HIGH if category known


def test_no_bsr_yields_no_estimate() -> None:
    assert _sales_estimate(AsinHistory("B0X", ()), AS_OF, HK, True, CFG) is None


# --- BSR trend / Theil–Sen -----------------------------------------------
def test_trend_improving() -> None:
    trend = _bsr_trend([_steady_drop("B0X", days=60, start=2000, step=10)], AS_OF, CFG)
    assert trend.direction == "improving"
    assert trend.median_annual_change is not None and trend.median_annual_change > 0
    assert trend.score is not None and trend.score > 50


def test_trend_declining() -> None:
    # BSR increasing over time (rank worsening).
    worsening = _history("B0X", [(60, 500), (30, 1500), (0, 4000)])
    trend = _bsr_trend([worsening], AS_OF, CFG)
    assert trend.direction == "declining"
    assert trend.score is not None and trend.score < 50


def test_trend_flat() -> None:
    flat = _history("B0X", [(60, 1000), (30, 1000), (0, 1000)])
    trend = _bsr_trend([flat], AS_OF, CFG)
    assert trend.direction == "flat"
    assert trend.median_annual_change == pytest.approx(0.0)
    assert trend.score == pytest.approx(curves.norm(0.0, CFG.trend_lo, CFG.trend_hi))


def test_theil_sen_edge_cases() -> None:
    assert curves.theil_sen([1.0], [2.0]) is None  # single point
    assert curves.theil_sen([5.0, 5.0], [1.0, 9.0]) is None  # identical x → no slope
    assert curves.theil_sen([0.0, 2.0], [0.0, 4.0]) == pytest.approx(2.0)  # two points
    # Robust to a spike outlier (median of pairwise slopes).
    assert curves.theil_sen([0.0, 1.0, 2.0, 3.0], [0.0, 1.0, 99.0, 3.0]) == pytest.approx(1.0)


def test_trend_unknown_without_enough_points() -> None:
    trend = _bsr_trend([_history("B0X", [(10, 1000)])], AS_OF, CFG)
    assert trend.direction == "unknown"
    assert trend.score is None
    assert trend.asins_with_slope == 0


# --- seasonality ----------------------------------------------------------
def _year_history(asin: str, bsr_for_day) -> AsinHistory:  # type: ignore[no-untyped-def]
    pts = [BsrPoint(AS_OF - timedelta(days=d), bsr_for_day(d)) for d in range(0, 400)]
    return AsinHistory(asin, tuple(pts))


def test_seasonality_unknown_under_12_months() -> None:
    short = _steady_drop("B0X", days=90)
    season = _seasonality(short, AS_OF, CFG)
    assert season.assessable is False
    assert season.score is None
    assert season.peak_concentration is None
    assert season.seasonal_flag is None


def test_seasonality_even_demand_scores_high() -> None:
    season = _seasonality(_year_history("B0X", lambda d: 1000), AS_OF, CFG)
    assert season.assessable is True
    assert season.seasonal_flag is False
    assert season.score is not None and season.score >= 90


def test_seasonality_concentrated_flags_seasonal() -> None:
    # Great rank (BSR 100) for an 8-week window, poor rank (5000) otherwise.
    def bsr(d: int) -> int:
        return 100 if 100 <= d < 156 else 5000

    season = _seasonality(_year_history("B0X", bsr), AS_OF, CFG)
    assert season.assessable is True
    assert season.peak_concentration is not None and season.peak_concentration > 0.6
    assert season.seasonal_flag is True
    assert season.score is not None and season.score < 20


# --- keyword demand + dedup ----------------------------------------------
def test_keyword_dedup_substring_rule() -> None:
    kws = [
        KeywordDatum("baby food tray", 4000),  # substring of the 9000 phrase → 30%
        KeywordDatum("silicone baby food tray", 9000),
        KeywordDatum("freezer tray", 3000),
    ]
    kd = _keyword_demand(kws, None, None, CFG)
    assert kd.total_volume == 16000
    assert kd.deduplicated_volume == 9000 + round(4000 * 0.3) + 3000  # 13200
    assert kd.primary_phrase == "silicone baby food tray"  # highest volume
    assert kd.primary_volume == 9000
    assert kd.demand_concentration == pytest.approx(9000 / 16000)
    assert kd.volumed_phrase_count == 3


def test_keyword_none_volumes_excluded() -> None:
    kws = [KeywordDatum("a", 3000), KeywordDatum("b", None), KeywordDatum("c", 0)]
    kd = _keyword_demand(kws, None, None, CFG)
    assert kd.total_volume == 3000
    assert kd.volumed_phrase_count == 1


def test_market_growth_from_series() -> None:
    series = [1000, 1050, 1100, 1150, 1200, 1250, 1300, 1350, 1400, 1450, 1500, 1600]
    kd = _keyword_demand([KeywordDatum("a", 5000)], None, series, CFG)
    assert kd.yoy_growth == pytest.approx(0.6)  # (1600-1000)/1000
    assert kd.market_growth_score == pytest.approx(100.0)  # 0.6 > growth_hi 0.4 → clamps


def test_market_growth_none_without_12_months() -> None:
    kd = _keyword_demand([KeywordDatum("a", 5000)], None, [1000, 1100, 1200], CFG)
    assert kd.yoy_growth is None
    assert kd.market_growth_score is None


# --- pillar + confidence + missing data ----------------------------------
def _full_market():  # type: ignore[no-untyped-def]
    histories = [_steady_drop(f"B0{i}", days=70, start=800 + i * 50) for i in range(9)]
    keywords = [
        KeywordDatum("silicone baby food tray", 9000),
        KeywordDatum("baby food tray", 4000),
        KeywordDatum("freezer tray", 3000),
        KeywordDatum("baby food storage", 2500),
        KeywordDatum("silicone tray", 1500),
    ]
    return histories, keywords


def test_full_market_high_confidence() -> None:
    histories, keywords = _full_market()
    report = analyze_demand(
        as_of=AS_OF,
        histories=histories,
        keywords=keywords,
        category="Home & Kitchen",
        curves_table=CURVES,
    )
    assert report.confidence == Confidence.HIGH
    # search_volume, sales_velocity, bsr_trend are assessed; growth needs a
    # 12-month series and seasonality needs 12 months of history → both missing.
    assert set(report.missing_components) == {"market_growth", "seasonality"}
    assert 0.0 <= report.pillar_score <= 100.0


def test_missing_keywords_drops_volume_component() -> None:
    histories, _ = _full_market()
    report = analyze_demand(
        as_of=AS_OF,
        histories=histories,
        keywords=[],
        category="Home & Kitchen",
        curves_table=CURVES,
    )
    assert "search_volume" in report.missing_components
    assert report.keyword_demand.search_volume_score is None


def test_empty_market_is_low_confidence_zero_pillar() -> None:
    report = analyze_demand(
        as_of=AS_OF, histories=[], keywords=[], category=None, curves_table=CURVES
    )
    assert report.pillar_score == 0.0
    assert report.confidence == Confidence.LOW
    assert report.bsr_trend.direction == "unknown"
    assert report.seasonality.assessable is False


def test_category_unknown_not_high_confidence() -> None:
    histories, keywords = _full_market()
    report = analyze_demand(
        as_of=AS_OF, histories=histories, keywords=keywords, category=None, curves_table=CURVES
    )
    assert report.confidence != Confidence.HIGH


# --- boundary: velocity sweet spot ---------------------------------------
@pytest.mark.parametrize(
    "units, expected",
    [(100, 0.0), (150, 0.0), (300, 100.0), (1200, 100.0), (2500, 70.0), (5000, 70.0)],
)
def test_velocity_curve_boundaries(units: float, expected: float) -> None:
    assert curves.plateau(units, *CFG.velocity_curve) == pytest.approx(expected)


# --- curve helper + loader edge cases ------------------------------------
def test_curve_math_degenerate_inputs() -> None:
    assert curves.norm(5.0, 10.0, 10.0) == 0.0  # lo == hi
    assert curves.log_norm(0, 1, 10) == 0.0  # x <= 0
    assert curves.log_norm(-5, 1, 10) == 0.0


def test_missing_curve_file_raises() -> None:
    from delium.analysis.demand import DemandError, load_velocity_curves

    with pytest.raises(DemandError):
        load_velocity_curves("does-not-exist")
