"""Property-based invariants for the cross-market engine (hypothesis).

Tests mathematical properties (bounds, monotonicity, determinism, confidence
monotonicity, directionality) — not implementation statements.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from delium.analysis.cross_market import analyze_cross_market, assess_source, match_products
from delium.analysis.models import (
    Confidence,
    Dimensions,
    Marketplace,
    MarketplaceProduct,
    SourceMarketInput,
    TargetMarketInput,
    TransferabilityInput,
)
from delium.config.models import DeliumConfig

CFG = DeliumConfig()
US, AU = Marketplace.US, Marketplace.AU
_TITLE = "silicone baby food freezer tray with lid"
_CONF_RANK = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}

_units = st.integers(min_value=0, max_value=50_000)
_vol = st.integers(min_value=0, max_value=200_000)
_growth = st.floats(min_value=-0.5, max_value=2.0, allow_nan=False)
_reviews = st.floats(min_value=0.0, max_value=5000.0, allow_nan=False)
_quality = st.floats(min_value=0.0, max_value=100.0, allow_nan=False)
_hhi = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
_listings = st.integers(min_value=0, max_value=30)


def _prod(mp: Marketplace, gtin: str = "0012345678905") -> MarketplaceProduct:
    return MarketplaceProduct(marketplace=mp, asin=f"A-{mp.value}", title=_TITLE, gtin=gtin)


def _fav() -> TransferabilityInput:
    return TransferabilityInput(
        category_compatible=True,
        oversized=False,
        price_positioning_ok=True,
        compliance_risk=False,
        seasonality_concentration=0.2,
    )


def _full_source(monthly_units: int = 1500) -> SourceMarketInput:
    return SourceMarketInput(
        monthly_units=monthly_units,
        keyword_volume=30000,
        keyword_growth=0.2,
        history_months=30,
        review_count=1200,
        competition_score=55,
    )


def _full_target(
    *,
    median_reviews: float = 100.0,
    keyword_volume: int = 6000,
    brand_hhi: float = 0.15,
    avg_listing_quality: float = 45.0,
    listings_found: int = 3,
) -> TargetMarketInput:
    return TargetMarketInput(
        listings_found=listings_found,
        median_reviews=median_reviews,
        avg_listing_quality=avg_listing_quality,
        beatable_slots=3,
        brand_hhi=brand_hhi,
        keyword_volume=keyword_volume,
        keyword_growth=0.2,
        serp_presence=True,
    )


def _report(source: SourceMarketInput, target: TargetMarketInput, transfer=None):  # type: ignore[no-untyped-def]
    return analyze_cross_market(
        source_product=_prod(US),
        target_product=_prod(AU),
        source_input=source,
        target_input=target,
        transfer_input=transfer or _fav(),
        config=CFG,
    )


# 1 — score bounds
@given(u=_units, v=_vol, r=_reviews, h=_hhi)
def test_score_within_bounds(u: int, v: int, r: float, h: float) -> None:
    rep = _report(_full_source(u), _full_target(median_reviews=r, keyword_volume=v, brand_hhi=h))
    assert 0.0 <= rep.score <= 100.0
    assert 0.0 <= rep.base_score <= 100.0


# 2 — determinism
@given(u=_units, v=_vol)
def test_deterministic(u: int, v: int) -> None:
    s, t = _full_source(u), _full_target(keyword_volume=v)
    assert _report(s, t) == _report(s, t)


# 3 — stronger target competition cannot increase the score
@given(base=_reviews, extra=st.floats(min_value=0.0, max_value=4000.0, allow_nan=False))
def test_stronger_target_competition_never_raises_score(base: float, extra: float) -> None:
    weak = _report(_full_source(), _full_target(median_reviews=base))
    strong = _report(_full_source(), _full_target(median_reviews=base + extra))
    assert strong.score <= weak.score + 1e-9


# 4 — stronger target demand cannot reduce the score
@given(base=_vol, extra=st.integers(min_value=0, max_value=150_000))
def test_stronger_target_demand_never_lowers_score(base: int, extra: int) -> None:
    lo = _report(_full_source(), _full_target(keyword_volume=base))
    hi = _report(_full_source(), _full_target(keyword_volume=base + extra))
    assert hi.score >= lo.score - 1e-9


# 5 — stronger source evidence cannot reduce the source-success score
@given(base=_units, extra=st.integers(min_value=0, max_value=48_000))
def test_stronger_source_never_lowers_source_score(base: int, extra: int) -> None:
    lo = assess_source(US, _full_source(base), CFG)
    hi = assess_source(US, _full_source(base + extra), CFG)
    assert hi.source_success_score >= lo.source_success_score - 1e-9


# 6 — lower match confidence cannot increase final confidence
@given(u=_units, v=_vol)
def test_lower_match_confidence_never_raises_final(u: int, v: int) -> None:
    s, t = _full_source(u), _full_target(keyword_volume=v)
    exact = _report(s, t)  # GTIN exact
    # Degrade the match: no gtin, conflicting dims → capped match confidence.
    degraded = analyze_cross_market(
        source_product=MarketplaceProduct(US, "A", title=_TITLE, dims=Dimensions(200, 150, 50)),
        target_product=MarketplaceProduct(AU, "B", title=_TITLE, dims=Dimensions(800, 600, 400)),
        source_input=s,
        target_input=t,
        transfer_input=_fav(),
        config=CFG,
    )
    assert _CONF_RANK[degraded.confidence.level] <= _CONF_RANK[exact.confidence.level]


# 7 — missing data cannot increase confidence
@given(v=_vol)
def test_missing_data_never_raises_confidence(v: int) -> None:
    full = _report(_full_source(), _full_target(keyword_volume=v))
    thinner = _report(
        SourceMarketInput(monthly_units=1500, keyword_volume=30000),  # fewer signals
        _full_target(keyword_volume=v),
    )
    assert _CONF_RANK[thinner.confidence.level] <= _CONF_RANK[full.confidence.level]


# 8 — reversing source/target is not silently symmetric for asymmetric evidence
@given(u=st.integers(min_value=800, max_value=5000))
def test_reversal_not_symmetric_for_asymmetric_evidence(u: int) -> None:
    strong_source = _full_source(u)
    weak_source = SourceMarketInput(monthly_units=150, keyword_volume=2100, keyword_growth=-0.05)
    mature_target = _full_target(median_reviews=2500, listings_found=15, avg_listing_quality=90)
    fresh_target = _full_target()

    forward = analyze_cross_market(
        source_product=_prod(US),
        target_product=_prod(AU),
        source_input=strong_source,
        target_input=fresh_target,
        transfer_input=_fav(),
        config=CFG,
    )
    reverse = analyze_cross_market(
        source_product=_prod(AU),
        target_product=_prod(US),
        source_input=weak_source,
        target_input=mature_target,
        transfer_input=_fav(),
        config=CFG,
    )
    assert (forward.verdict, forward.score) != (reverse.verdict, reverse.score)


# 9 — component contributions are bounded and consistent
@given(u=_units, v=_vol, r=_reviews)
def test_component_contributions_bounded_and_sum(u: int, v: int, r: float) -> None:
    rep = _report(_full_source(u), _full_target(median_reviews=r, keyword_volume=v))
    total_w = sum(w for _, w in rep.weights_snapshot)
    for c in rep.components:
        assert 0.0 <= c.normalized <= 100.0
        assert -1e-9 <= c.weighted_contribution <= c.weight + 1e-9
    assert abs(sum(c.weighted_contribution for c in rep.components) - rep.base_score) < 1e-9
    assert total_w > 0


# 10 — match scoring is deterministic and bounded
@given(
    same_brand=st.booleans(),
    dims_ok=st.booleans(),
)
def test_match_score_bounded(same_brand: bool, dims_ok: bool) -> None:
    s = MarketplaceProduct(US, "A", title=_TITLE, brand="Acme", dims=Dimensions(200, 150, 50))
    t = MarketplaceProduct(
        AU,
        "B",
        title=_TITLE,
        brand="Acme" if same_brand else "Zenith",
        dims=Dimensions(200, 150, 50) if dims_ok else Dimensions(900, 700, 500),
    )
    m = match_products(s, t, CFG)
    assert 0.0 <= m.score <= 1.0
    assert m == match_products(s, t, CFG)
