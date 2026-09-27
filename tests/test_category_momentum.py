"""Category momentum (pure) — absolute score, batch-invariant, missing data."""

from __future__ import annotations

from delium.analysis.category_momentum import (
    MomentumProduct,
    compute_category_momentum,
    load_momentum_data,
)

DATA = load_momentum_data("us")


def _hot(cat: str, n: int = 6) -> list[MomentumProduct]:
    return [
        MomentumProduct(
            category=cat,
            emergence_score=85,
            bsr_change_90d=-0.25,
            age_days=45,
            review_count=40,
            monthly_sold=500,
            price_cents=3000,
            profit_net_cents=900,
        )
        for _ in range(n)
    ]


def _quiet(cat: str, n: int = 6) -> list[MomentumProduct]:
    return [
        MomentumProduct(
            category=cat,
            emergence_score=10,
            bsr_change_90d=0.05,
            age_days=1500,
            review_count=4000,
            monthly_sold=None,
            price_cents=3000,
        )
        for _ in range(n)
    ]


def test_hot_category_outscores_quiet() -> None:
    results = compute_category_momentum(_hot("Home & Kitchen") + _quiet("Office"), DATA)
    by_cat = {r.category: r for r in results}
    assert by_cat["Home & Kitchen"].score is not None
    assert by_cat["Office"].score is not None
    assert by_cat["Home & Kitchen"].score > by_cat["Office"].score


def test_absolute_score_is_batch_invariant() -> None:
    # A category's score must be identical whether or not other categories are in
    # the batch (absolute thresholds, not ranked-relative).
    alone = compute_category_momentum(_hot("Home & Kitchen"), DATA)
    with_others = compute_category_momentum(
        _hot("Home & Kitchen") + _quiet("Office") + _hot("Toys"), DATA
    )
    a = next(r for r in alone if r.category == "Home & Kitchen").score
    b = next(r for r in with_others if r.category == "Home & Kitchen").score
    assert a == b


def test_top_n_and_ordering() -> None:
    products = _hot("A") + _quiet("B") + _hot("C") + _quiet("D")
    results = compute_category_momentum(products, DATA, top_n=2)
    assert len(results) == 2
    assert results[0].score is not None and results[1].score is not None
    assert results[0].score >= results[1].score  # sorted desc


def test_uses_top_level_segment_and_ignores_uncategorized() -> None:
    products = [
        *_hot("Home & Kitchen > Storage"),
        MomentumProduct(category=None, emergence_score=90),
    ]
    results = compute_category_momentum(products, DATA)
    cats = {r.category for r in results}
    assert cats == {"Home & Kitchen"}  # subcategory folded to department; None dropped


def test_metrics_and_missing_revenue() -> None:
    # No monthly_sold anywhere → revenue metric None, score still computed from the
    # other components (renormalized).
    results = compute_category_momentum(_quiet("Office"), DATA)
    office = next(r for r in results if r.category == "Office")
    assert office.metrics.aggregate_monthly_revenue_usd is None
    assert office.score is not None
    assert office.metrics.sample_size == 6
