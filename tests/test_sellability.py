"""Sellability (pure) — no double counting, renormalize on missing, confidence
separate, no bypass, batch-invariant."""

from __future__ import annotations

from delium.analysis.models import Confidence
from delium.analysis.sellability import (
    SellabilityInput,
    compute_sellability,
    load_sellability_data,
)

W = load_sellability_data("us")


def test_all_components_weighted_and_high_confidence() -> None:
    r = compute_sellability(
        SellabilityInput(opportunity_score=60, price_headroom=80, incumbent_freshness=40), W
    )
    # (60*60 + 80*25 + 40*15) / 100 = (3600+2000+600)/100 = 62.0
    assert r.score == 62.0
    assert r.confidence is Confidence.HIGH
    assert r.missing == ()


def test_opportunity_only_renormalizes_to_opportunity_low_confidence() -> None:
    r = compute_sellability(SellabilityInput(opportunity_score=68), W)
    assert r.score == 68.0  # renormalized over the one available component
    assert r.confidence is Confidence.LOW
    assert set(r.missing) == {"price_headroom", "incumbent_freshness"}


def test_confidence_is_separate_from_score() -> None:
    # Same score numerator/denominator, different coverage → score reflects only
    # available components; confidence (badge) changes, not folded into score.
    two = compute_sellability(SellabilityInput(opportunity_score=70, price_headroom=70), W)
    assert two.score == 70.0  # (70*60 + 70*25)/85
    assert two.confidence is Confidence.MEDIUM


def test_ineligible_never_scores_no_bypass() -> None:
    r = compute_sellability(
        SellabilityInput(opportunity_score=90, price_headroom=90, eligible=False), W
    )
    assert r.score is None
    assert "did not pass" in r.reason


def test_same_inputs_same_score_regardless_of_batch() -> None:
    inp = SellabilityInput(opportunity_score=55, price_headroom=65, incumbent_freshness=45)
    a = compute_sellability(inp, W)
    # Score other products in between…
    compute_sellability(SellabilityInput(opportunity_score=10), W)
    compute_sellability(SellabilityInput(opportunity_score=99, price_headroom=99), W)
    b = compute_sellability(inp, W)
    assert a.score == b.score


def test_profitability_not_double_counted() -> None:
    # Sellability inputs are opportunity + price_headroom + incumbent_freshness.
    # There is no separate margin/profit term, so profitability (inside
    # opportunity) is counted exactly once. Guard the component set.
    r = compute_sellability(SellabilityInput(opportunity_score=60, price_headroom=60), W)
    names = {c.name for c in r.components}
    assert names == {"opportunity", "price_headroom", "incumbent_freshness"}
    assert "profit" not in names and "margin" not in names
