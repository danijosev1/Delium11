"""Differentiation engine: unit, boundary, and integrity tests."""

from __future__ import annotations

import pytest

from delium.analysis.differentiation import analyze_differentiation
from delium.analysis.models import (
    Addressability,
    BundleSignal,
    Confidence,
    DifferentiationConfig,
    DifferentiationInput,
    DiffReview,
    FeatureRequest,
    RawTheme,
    ThemeKind,
)

CFG = DifferentiationConfig()


def _reviews(
    n: int, low_star: int = 0, star_low: int = 1, star_high: int = 5
) -> tuple[DiffReview, ...]:
    """n reviews; the first `low_star` are `star_low`, the rest `star_high`."""
    return tuple(
        DiffReview(f"r{i}", stars=star_low if i < low_star else star_high) for i in range(n)
    )


def _ids(*ranges: range) -> tuple[str, ...]:
    out: list[str] = []
    for r in ranges:
        out.extend(f"r{i}" for i in r)
    return tuple(out)


def _complaint(theme_id: str, ids: tuple[str, ...], **kw: object) -> RawTheme:
    return RawTheme(theme_id, ThemeKind.COMPLAINT, theme_id, ids, **kw)  # type: ignore[arg-type]


# --- 1. frequency recomputed from ids ------------------------------------
def test_frequency_recomputed_from_ids() -> None:
    reviews = _reviews(100, low_star=100)  # all 1-star
    theme = _complaint("t", _ids(range(20)))  # 20 real ids
    r = analyze_differentiation(DifferentiationInput("B0", reviews, (theme,)))
    t = r.themes[0]
    assert t.verified_count == 20
    assert t.frequency == pytest.approx(0.20)  # 20 / 100


# --- 2. duplicate ids count once -----------------------------------------
def test_duplicate_ids_count_once() -> None:
    reviews = _reviews(100, low_star=100)
    ids = _ids(range(10)) + ("r0", "r1", "r2", "r5")  # 4 duplicates
    theme = _complaint("t", ids)
    t = analyze_differentiation(DifferentiationInput("B0", reviews, (theme,))).themes[0]
    assert t.supporting_count_claimed == 14
    assert t.verified_count == 10  # duplicates collapse
    assert t.frequency == pytest.approx(0.10)


# --- 3. unknown/nonexistent ids excluded ---------------------------------
def test_unknown_ids_cannot_inflate_frequency() -> None:
    reviews = _reviews(100, low_star=100)
    ids = _ids(range(10)) + ("ghost1", "ghost2", "ghost3", "ghost4", "ghost5")
    theme = _complaint("t", ids)
    t = analyze_differentiation(DifferentiationInput("B0", reviews, (theme,))).themes[0]
    assert t.verified_count == 10  # ghosts dropped
    assert t.frequency <= 1.0
    assert t.frequency == pytest.approx(0.10)


# --- 4. LLM percentage/severity ignored ----------------------------------
def test_llm_claims_are_ignored() -> None:
    reviews = _reviews(100, low_star=100)  # all 1-star → severity 3
    theme = _complaint("t", _ids(range(5)), claimed_frequency_pct=99.0, claimed_severity=1)
    t = analyze_differentiation(DifferentiationInput("B0", reviews, (theme,))).themes[0]
    assert t.frequency == pytest.approx(0.05)  # not 0.99
    assert t.severity == 3  # from stars, not the claimed 1


# --- 5. severity from cited stars ----------------------------------------
@pytest.mark.parametrize(
    "star, expected_severity",
    [(1, 3), (2, 3), (3, 2), (4, 1), (5, 1)],
)
def test_severity_from_cited_stars(star: int, expected_severity: int) -> None:
    reviews = tuple(DiffReview(f"r{i}", stars=star) for i in range(20))
    theme = _complaint("t", _ids(range(5)))
    t = analyze_differentiation(DifferentiationInput("B0", reviews, (theme,))).themes[0]
    assert t.severity == expected_severity


def test_severity_none_without_verified() -> None:
    reviews = _reviews(50, low_star=50)
    theme = _complaint("t", ("ghost1", "ghost2"))  # no real ids
    t = analyze_differentiation(DifferentiationInput("B0", reviews, (theme,))).themes[0]
    assert t.severity is None
    assert t.counted is False


# --- 6. fixable vs non-fixable -------------------------------------------
def test_addressability_fixable_vs_hard() -> None:
    reviews = _reviews(100, low_star=100)
    fixable = analyze_differentiation(
        DifferentiationInput(
            "B0",
            reviews,
            (
                _complaint(
                    "t", _ids(range(20)), addressability=Addressability.FIXABLE, cogs_delta=0.1
                ),
            ),
        )
    )
    hard = analyze_differentiation(
        DifferentiationInput(
            "B0",
            reviews,
            (_complaint("t", _ids(range(20)), addressability=Addressability.HARD),),
        )
    )
    assert fixable.addressability_score == pytest.approx(100.0)
    assert hard.addressability_score == pytest.approx(0.0)  # not fixable → no credit


def test_addressability_unknown_is_zero_not_optimistic() -> None:
    reviews = _reviews(100, low_star=100)
    r = analyze_differentiation(
        DifferentiationInput(
            "B0",
            reviews,
            (_complaint("t", _ids(range(20)), addressability=Addressability.UNKNOWN),),
        )
    )
    assert r.addressability_score == pytest.approx(0.0)


def test_addressability_fixable_but_expensive_is_partial() -> None:
    reviews = _reviews(100, low_star=100)
    r = analyze_differentiation(
        DifferentiationInput(
            "B0",
            reviews,
            (
                _complaint(
                    "t", _ids(range(20)), addressability=Addressability.FIXABLE, cogs_delta=0.30
                ),
            ),
        )
    )
    assert r.addressability_score == pytest.approx(50.0)  # weight 0.5


# --- 7. feature gaps ------------------------------------------------------
def test_feature_gaps_require_evidence_and_absence() -> None:
    reviews = _reviews(100, low_star=100)
    feats = (
        FeatureRequest("seal", _ids(range(5)), absent_from_competitors=True),  # counts
        FeatureRequest("handle", _ids(range(5, 10)), absent_from_competitors=None),  # unknown → no
        FeatureRequest("lid", ("r0", "r1"), absent_from_competitors=True),  # too few ids → no
    )
    r = analyze_differentiation(DifferentiationInput("B0", reviews, (), feats))
    assert r.feature_gap_count == 1
    assert r.missing_features_score == 25


def test_feature_gaps_capped_at_four() -> None:
    reviews = _reviews(100, low_star=100)
    feats = tuple(
        FeatureRequest(f"f{k}", _ids(range(k * 3, k * 3 + 3)), absent_from_competitors=True)
        for k in range(6)
    )
    r = analyze_differentiation(DifferentiationInput("B0", reviews, (), feats))
    assert r.feature_gap_count == 6
    assert r.missing_features_score == 100  # min(6,4)*25


def test_feature_none_without_data() -> None:
    r = analyze_differentiation(DifferentiationInput("B0", _reviews(100), ()))
    assert r.missing_features_score is None
    assert "feature_evidence" in r.data_gaps


# --- 8. multiple themes ---------------------------------------------------
def test_multiple_themes_intensity_sum() -> None:
    reviews = _reviews(100, low_star=100)  # all 1-star → severity 3
    themes = (
        _complaint("t1", _ids(range(10)), addressability=Addressability.FIXABLE, cogs_delta=0.1),
        _complaint("t2", _ids(range(10, 20)), addressability=Addressability.HARD),
        _complaint("t3", ("r0",)),  # below min_quotes → excluded
    )
    r = analyze_differentiation(DifferentiationInput("B0", reviews, themes))
    # intensity: t1 = 10*3 = 30, t2 = 10*3 = 30, sum 60 → norm(60,10,60)=100
    assert r.complaint_intensity_score == pytest.approx(100.0)
    # addressability: only t1 fixable (30 of 60) → 50%
    assert r.addressability_score == pytest.approx(50.0)
    assert sum(1 for t in r.themes if t.counted) == 2


# --- 9. sample-bias detection --------------------------------------------
def test_sample_bias_flag_and_f1_boost() -> None:
    # 90 five-star + 10 one-star → sample avg 4.6; listing 3.0 → delta 1.6 > 0.4.
    reviews = _reviews(100, low_star=10)  # 10 one-star, 90 five-star
    theme = _complaint("t", _ids(range(10)))  # cites the 10 one-star reviews
    r = analyze_differentiation(
        DifferentiationInput("B0", reviews, (theme,), listing_rating_avg=3.0)
    )
    assert r.sample_bias_flag is True
    assert r.sample_bias_delta == pytest.approx(4.6 - 3.0)
    assert r.f1_bias_adjustment == 5.0


def test_no_bias_when_sample_matches_listing() -> None:
    reviews = _reviews(100, low_star=50)  # avg 3.0
    theme = _complaint("t", _ids(range(10)))
    r = analyze_differentiation(
        DifferentiationInput("B0", reviews, (theme,), listing_rating_avg=3.0)
    )
    assert r.sample_bias_flag is False
    assert r.f1_bias_adjustment == 0.0


def test_bias_uncheckable_without_listing_rating() -> None:
    r = analyze_differentiation(DifferentiationInput("B0", _reviews(100), ()))
    assert r.sample_bias_delta is None
    assert "listing_rating" in r.data_gaps


# --- 10. missing review data ---------------------------------------------
def test_zero_reviews_all_missing() -> None:
    r = analyze_differentiation(DifferentiationInput("B0", (), ()))
    assert r.pillar_score == 0.0
    assert r.confidence.level == Confidence.LOW
    assert r.complaint_intensity_score is None
    assert r.bundle_packaging_score is None
    assert "reviews" in r.data_gaps
    assert r.has_buy_quality_evidence is False


# --- 11. missing theme evidence ------------------------------------------
def test_themes_but_no_verified_evidence() -> None:
    reviews = _reviews(100, low_star=100)
    theme = _complaint("t", ("ghost1", "ghost2", "ghost3", "ghost4"))
    r = analyze_differentiation(DifferentiationInput("B0", reviews, (theme,)))
    # complaint exists but zero verified → F1 computed 0 (not optimistic), F3 None.
    assert r.complaint_intensity_score == pytest.approx(0.0)
    assert r.addressability_score is None
    assert r.has_buy_quality_evidence is False


# --- 12. confidence degradation ------------------------------------------
def test_confidence_high_needs_large_clean_sample() -> None:
    reviews = _reviews(200, low_star=200)
    theme = _complaint("t", _ids(range(30)))
    r = analyze_differentiation(
        DifferentiationInput("B0", reviews, (theme,), listing_rating_avg=1.0)
    )
    assert r.confidence.level == Confidence.HIGH


def test_confidence_small_sample_is_low() -> None:
    reviews = _reviews(20, low_star=20)
    theme = _complaint("t", _ids(range(10)))
    r = analyze_differentiation(DifferentiationInput("B0", reviews, (theme,)))
    assert r.confidence.level == Confidence.LOW


def test_confidence_drops_with_many_unresolved_ids() -> None:
    reviews = _reviews(200, low_star=200)
    # Only 4 of 40 cited ids resolve → verified ratio 0.1 < 0.5 → downgrade.
    ids = _ids(range(4)) + tuple(f"ghost{i}" for i in range(36))
    theme = _complaint("t", ids)
    r = analyze_differentiation(
        DifferentiationInput("B0", reviews, (theme,), listing_rating_avg=1.0)
    )
    assert r.confidence.level == Confidence.MEDIUM  # downgraded from HIGH


# --- 13. pillar weighting -------------------------------------------------
def test_pillar_weighting_hand_worked() -> None:
    reviews = _reviews(100, low_star=100)
    themes = (
        _complaint("t", _ids(range(22)), addressability=Addressability.FIXABLE, cogs_delta=0.1),
    )
    feats = (FeatureRequest("seal", _ids(range(5)), absent_from_competitors=True),)
    r = analyze_differentiation(DifferentiationInput("B0", reviews, themes, feats))
    # F1=100 (intensity 22*3=66→clamp), F2=25, F3=100, F4=0
    # pillar = (100*40 + 25*25 + 100*20 + 0*15)/100 = 66.25
    assert r.complaint_intensity_score == pytest.approx(100.0)
    assert r.missing_features_score == 25
    assert r.addressability_score == pytest.approx(100.0)
    assert r.bundle_packaging_score == pytest.approx(0.0)
    assert r.pillar_score == pytest.approx(66.25)


# --- F4 rubric ------------------------------------------------------------
def test_f4_bundle_packaging_rubric() -> None:
    reviews = _reviews(100, low_star=100)
    themes = (
        _complaint("pkg", _ids(range(10)), category="packaging"),  # ≥5% packaging
        _complaint("use", _ids(range(10, 16)), category="usage instructions"),  # ≥5% usage
    )
    bundles = (BundleSignal("liner", _ids(range(20, 25))),)  # 5% ≥ 3%
    r = analyze_differentiation(
        DifferentiationInput(
            "B0", reviews, themes, bundle_signals=bundles, competitors_bundle_complement=False
        )
    )
    assert r.bundle_packaging_score == 100.0  # all 4 criteria met


def test_f4_competitor_bundle_unknown_not_credited() -> None:
    reviews = _reviews(100, low_star=100)
    bundles = (BundleSignal("liner", _ids(range(20, 25))),)
    r = analyze_differentiation(
        DifferentiationInput(
            "B0", reviews, (), bundle_signals=bundles, competitors_bundle_complement=None
        )
    )
    assert r.bundle_packaging_score == 25.0  # only the bundle-mention criterion


# --- 14. score boundaries -------------------------------------------------
def test_scores_bounded() -> None:
    reviews = _reviews(100, low_star=100)
    themes = tuple(
        _complaint(f"t{k}", _ids(range(k * 10, k * 10 + 10)), addressability=Addressability.FIXABLE)
        for k in range(5)
    )
    r = analyze_differentiation(DifferentiationInput("B0", reviews, themes))
    for comp in r.components:
        assert comp.value is None or 0.0 <= comp.value <= 100.0
    assert 0.0 <= r.pillar_score <= 100.0
