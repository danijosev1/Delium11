"""Launchability (pure) — absolute scale, missing = unknown, batch-invariant."""

from __future__ import annotations

from delium.analysis.launchability import (
    Competitor,
    compute_launchability,
    load_launchability_data,
)

DATA = load_launchability_data("us")
ESTABLISHED = frozenset({"anker", "sony"})


def _weak_page() -> list[Competitor]:
    # Few reviews, no big brands, fresh listings, thin listings → very launchable.
    return [
        Competitor(
            f"B{i}",
            reviews=40,
            rating=3.9,
            price_cents=5499,
            images_count=2,
            title="basic widget",
            brand="Acme",
            age_days=120,
        )
        for i in range(10)
    ]


def _entrenched_page() -> list[Competitor]:
    return [
        Competitor(
            f"C{i}",
            reviews=5000,
            rating=4.7,
            price_cents=7999,
            images_count=7,
            title="x" * 180,
            brand="Anker",
            age_days=2000,
        )
        for i in range(10)
    ]


def test_missing_competitor_set_is_none_not_zero() -> None:
    r = compute_launchability([], target_price_cents=5499, thresholds=DATA)
    assert r.score is None
    assert "competitor_set" in r.missing


def test_weak_page_is_more_launchable_than_entrenched() -> None:
    weak = compute_launchability(
        _weak_page(), target_price_cents=1999, thresholds=DATA, established_brands=ESTABLISHED
    )
    hard = compute_launchability(
        _entrenched_page(), target_price_cents=7999, thresholds=DATA, established_brands=ESTABLISHED
    )
    assert weak.score is not None and hard.score is not None
    assert weak.score > hard.score
    assert 0 <= hard.score <= 100 and 0 <= weak.score <= 100


def test_absolute_scale_fixed_input_fixed_score() -> None:
    # Same competitor set → identical score no matter what else is scored (not
    # z-scored against a batch).
    page = _weak_page()
    first = compute_launchability(page, target_price_cents=1999, thresholds=DATA)
    # Score an unrelated entrenched page in between…
    compute_launchability(_entrenched_page(), target_price_cents=7999, thresholds=DATA)
    second = compute_launchability(page, target_price_cents=1999, thresholds=DATA)
    assert first.score == second.score


def test_missing_component_renormalizes() -> None:
    # Competitors with no ages / no images → those components drop, score still set.
    comps = [Competitor(f"B{i}", reviews=50, price_cents=2000, brand="Acme") for i in range(5)]
    r = compute_launchability(comps, target_price_cents=2000, thresholds=DATA)
    assert r.score is not None
    assert "listing_age" in r.missing
    assert "listing_quality_gap" in r.missing
    # price_crowding + median_reviews + heavy + brand still present.
    assert r.price_headroom is not None


def test_named_component_accessors() -> None:
    r = compute_launchability(_weak_page(), target_price_cents=1999, thresholds=DATA)
    assert r.price_headroom is not None  # price_crowding
    assert r.incumbent_freshness is not None  # listing_age (young → high)
    assert r.incumbent_freshness >= 60  # 120d << young/old band → very fresh


def test_amazon_presence_penalizes_brand_component() -> None:
    base = [Competitor(f"B{i}", reviews=50, brand="Acme", price_cents=2000) for i in range(5)]
    with_amazon = [*base[:-1], Competitor("BX", reviews=50, brand="AmazonBasics", price_cents=2000)]
    b = compute_launchability(base, target_price_cents=2000, thresholds=DATA)
    a = compute_launchability(with_amazon, target_price_cents=2000, thresholds=DATA)
    assert a.score is not None and b.score is not None and a.score < b.score
