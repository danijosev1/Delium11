"""Competition engine: unit, boundary, and edge-case tests."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from delium.analysis import curves
from delium.analysis.competition import (
    _beatable_slots,
    _brand_concentration,
    _price_competition,
    _review_moat,
    _review_velocity,
    analyze_competition,
)
from delium.analysis.models import (
    CompetitionConfig,
    CompetitionInput,
    CompetitorSnapshot,
    Confidence,
    PricePoint,
)

CFG = CompetitionConfig()
AS_OF = date(2026, 8, 1)


def _c(asin: str, **kw: object) -> CompetitorSnapshot:
    return CompetitorSnapshot(asin=asin, **kw)  # type: ignore[arg-type]


def _price_history(asin_prices: list[tuple[int, int]]) -> tuple[PricePoint, ...]:
    """[(days_ago, price_cents)] → PricePoint tuple."""
    return tuple(PricePoint(AS_OF - timedelta(days=d), p) for d, p in asin_prices)


# --- hand-worked full report ---------------------------------------------
def test_hand_worked_pillar() -> None:
    comps = (
        _c("B01", brand="Acme", review_count=120, listing_quality=55.0, review_count_90d_ago=90),
        _c("B02", brand="Acme", review_count=80, listing_quality=40.0, review_count_90d_ago=60),
        _c("B03", brand="Beta", review_count=500, listing_quality=70.0, review_count_90d_ago=470),
        _c("B04", brand="Gamma", review_count=200),
        _c("B05", brand="Delta", review_count=140, listing_quality=50.0),
    )
    r = analyze_competition(CompetitionInput(comps, AS_OF))
    assert r.review_moat.median_reviews == 140
    assert r.review_moat.score == 100.0  # median 140 < lo 200 → moat 0 → score 100
    assert r.beatable_slots.count == 3  # 120, 80, 140 under 150
    assert r.beatable_slots.score == 75  # min(3,4)*25
    assert r.review_velocity.classification == "low"
    assert r.brand_concentration.top_brand_slot_share == pytest.approx(0.4)
    assert r.brand_concentration.hhi == pytest.approx(0.28)
    assert r.listing_quality_advantage.advantage_score == pytest.approx(46.25)
    assert r.pillar_score == pytest.approx(78.44, abs=0.05)


# --- C1 review moat boundaries -------------------------------------------
@pytest.mark.parametrize(
    "median_reviews, expected_score",
    [(0, 100.0), (200, 100.0), (3000, 0.0), (10000, 0.0)],
)
def test_review_moat_boundaries(median_reviews: int, expected_score: float) -> None:
    # A single competitor makes the median equal to its review count.
    moat = _review_moat([_c("B0", review_count=median_reviews)], CFG)
    assert moat.score == pytest.approx(expected_score)


def test_review_moat_missing() -> None:
    moat = _review_moat([_c("B0"), _c("B1")], CFG)
    assert moat.score is None
    assert moat.median_reviews is None


# --- C2 beatable slots ----------------------------------------------------
@pytest.mark.parametrize(
    "counts, expected_count, expected_score",
    [
        ([500, 600, 700], 0, 0),
        ([100, 500, 600], 1, 25),
        ([100, 120, 140], 3, 75),
        ([10, 20, 30, 40, 50, 60], 6, 100),  # capped at 4 slots → 100
    ],
)
def test_beatable_slots(counts: list[int], expected_count: int, expected_score: float) -> None:
    b = _beatable_slots([_c(f"B{i}", review_count=c) for i, c in enumerate(counts)], CFG)
    assert b.count == expected_count
    assert b.score == expected_score


def test_beatable_slots_weak_listing_context() -> None:
    comps = [
        _c("B0", review_count=100, listing_quality=30.0),  # low review + weak listing
        _c("B1", review_count=120, listing_quality=90.0),  # low review, strong listing
        _c("B2", review_count=140),  # low review, unknown listing → counted weak
    ]
    b = _beatable_slots(comps, CFG)
    assert b.count == 3
    assert b.weak_listing_slots == 2  # B0 (weak) + B2 (unknown)


# --- C3 review velocity ---------------------------------------------------
def test_review_velocity_high() -> None:
    # top-3 leaders each gained lots of reviews in 90d → high velocity.
    comps = [_c(f"B{i}", review_count=1000, review_count_90d_ago=1000 - 600) for i in range(3)]
    v = _review_velocity(comps, CFG)
    assert v.monthly_velocity == pytest.approx(200.0)  # 600/3
    assert v.classification == "high"
    assert v.score is not None and v.score < 30  # fast-growing leaders → hard


def test_review_velocity_low_and_clamped_negative() -> None:
    comps = [
        _c("B0", review_count=100, review_count_90d_ago=95),  # +5 → 1.67/mo
        _c("B1", review_count=100, review_count_90d_ago=140),  # negative → clamps to 0
        _c("B2", review_count=100, review_count_90d_ago=100),  # 0
    ]
    v = _review_velocity(comps, CFG)
    assert v.classification == "low"
    assert v.monthly_velocity is not None and v.monthly_velocity >= 0


def test_review_velocity_missing_history() -> None:
    v = _review_velocity([_c("B0", review_count=100)], CFG)
    assert v.classification == "unknown"
    assert v.score is None


# --- C4 brand concentration / HHI ----------------------------------------
def test_hhi_all_distinct() -> None:
    comps = [_c(f"B{i}", brand=f"Brand{i}") for i in range(5)]
    bc = _brand_concentration(comps, frozenset(), CFG)
    assert bc.hhi == pytest.approx(0.2)  # 5 * (1/5)^2
    assert bc.top_brand_slot_share == pytest.approx(0.2)
    assert bc.concentration_flag is False


def test_hhi_monopoly() -> None:
    comps = [_c(f"B{i}", brand="OneBrand") for i in range(5)]
    bc = _brand_concentration(comps, frozenset(), CFG)
    assert bc.hhi == pytest.approx(1.0)
    assert bc.concentration_flag is True  # > 0.30
    assert bc.score == pytest.approx(0.0)  # 100 - 100 slot share


def test_brand_dominance_big_brand_penalty() -> None:
    comps = [_c("B0", brand="Acme"), _c("B1", brand="Beta"), _c("B2", brand="Gamma")]
    without = _brand_concentration(comps, frozenset(), CFG)
    with_big = _brand_concentration(comps, frozenset({"Acme"}), CFG)
    assert with_big.recognized_big_brand_present is True
    assert with_big.score == pytest.approx(without.score - CFG.big_brand_penalty)


def test_brand_concentration_missing() -> None:
    bc = _brand_concentration([_c("B0"), _c("B1")], frozenset(), CFG)
    assert bc.hhi is None
    assert bc.score is None


# --- C5 listing quality advantage ----------------------------------------
def test_listing_advantage_available() -> None:
    r = analyze_competition(
        CompetitionInput((_c("B0", listing_quality=40.0), _c("B1", listing_quality=50.0)), AS_OF)
    )
    la = r.listing_quality_advantage
    assert la.avg_competitor_quality == pytest.approx(45.0)
    assert la.advantage_score == pytest.approx(55.0)
    assert la.advantage_available is True


def test_listing_advantage_none_when_incumbents_excellent() -> None:
    r = analyze_competition(
        CompetitionInput((_c("B0", listing_quality=95.0), _c("B1", listing_quality=90.0)), AS_OF)
    )
    assert r.listing_quality_advantage.advantage_available is False


def test_listing_advantage_neutral_without_data() -> None:
    r = analyze_competition(CompetitionInput((_c("B0"), _c("B1")), AS_OF))
    la = r.listing_quality_advantage
    assert la.avg_competitor_quality is None
    assert la.advantage_score == CFG.neutral_score  # neutral fallback
    assert la.advantage_available is False
    assert "listing_quality" in r.data_gaps


# --- C6 price competition + price war ------------------------------------
def test_price_metrics_cross_sectional() -> None:
    comps = [_c("B0", price_cents=1000), _c("B1", price_cents=2000), _c("B2", price_cents=1500)]
    pc = _price_competition(comps, AS_OF, CFG)
    assert pc.median_price_cents == 1500
    assert pc.price_spread_cents == 1000  # 2000 - 1000
    assert pc.clustering_cv is not None


def test_price_war_detected() -> None:
    # 3 competitors hit their 90d low within the last 14 days.
    comps = [
        _c("B0", price_history=_price_history([(80, 2000), (60, 1900), (5, 1500)])),
        _c("B1", price_history=_price_history([(70, 2100), (40, 2000), (3, 1600)])),
        _c("B2", price_history=_price_history([(85, 2200), (30, 2100), (1, 1700)])),
    ]
    pc = _price_competition(comps, AS_OF, CFG)
    assert pc.competitors_at_recent_low == 3
    assert pc.price_war_flag is True


def test_no_price_war_when_lows_are_old() -> None:
    comps = [
        _c("B0", price_history=_price_history([(80, 1500), (5, 2000)])),  # low was 80d ago
        _c("B1", price_history=_price_history([(70, 1600), (3, 2100)])),
    ]
    pc = _price_competition(comps, AS_OF, CFG)
    assert pc.competitors_at_recent_low == 0
    assert pc.price_war_flag is False


def test_price_score_neutral_without_history() -> None:
    pc = _price_competition([_c("B0", price_cents=1000)], AS_OF, CFG)
    assert pc.median_price_cv_90d is None
    assert pc.score == CFG.neutral_score


def test_price_score_from_cv() -> None:
    # Very stable prices (low CV) → high price-competition score.
    stable = _c("B0", price_history=_price_history([(80, 2000), (40, 2000), (5, 2000)]))
    pc = _price_competition([stable], AS_OF, CFG)
    assert pc.median_price_cv_90d == pytest.approx(0.0)
    assert pc.score == 100.0  # 100 - norm(0, 0.05, 0.25) = 100


def test_price_history_too_few_in_window_is_neutral() -> None:
    # Only one point falls inside the 90d window → no CV computable → neutral.
    comps = [_c("B0", price_history=_price_history([(200, 2000), (5, 1800)]))]
    pc = _price_competition(comps, AS_OF, CFG)
    assert pc.median_price_cv_90d is None
    assert pc.score == CFG.neutral_score


# --- confidence + pillar edge cases --------------------------------------
def test_empty_market_low_confidence() -> None:
    r = analyze_competition(CompetitionInput((), AS_OF))
    assert r.confidence.level == Confidence.LOW
    # Only C5/C6 neutral fallbacks contribute → pillar 50.
    assert r.pillar_score == pytest.approx(50.0)
    assert set(r.missing_components) == {
        "review_moat",
        "beatable_slots",
        "review_velocity",
        "brand_dominance",
    }


def test_full_data_high_confidence() -> None:
    comps = tuple(
        _c(
            f"B{i:02d}",
            brand=f"Brand{i % 4}",
            review_count=100 + i * 30,
            review_count_90d_ago=90 + i * 30,
            listing_quality=50.0 + i,
            price_cents=2000 + i * 50,
            price_history=_price_history([(80, 2000 + i * 50), (5, 2000 + i * 50)]),
        )
        for i in range(9)
    )
    r = analyze_competition(CompetitionInput(comps, AS_OF))
    assert r.confidence.level == Confidence.HIGH
    assert r.data_gaps == ()


def test_top_n_truncation() -> None:
    comps = tuple(_c(f"B{i:02d}", review_count=100, brand="X") for i in range(15))
    r = analyze_competition(CompetitionInput(comps, AS_OF))
    assert r.confidence.competitors_analyzed == 10  # top_n default


# --- curve sanity ---------------------------------------------------------
def test_review_moat_uses_log_norm() -> None:
    moat = _review_moat([_c("B0", review_count=775)], CFG)  # geometric mid of 200,3000
    assert moat.score == pytest.approx(100.0 - curves.log_norm(775, 200, 3000))
