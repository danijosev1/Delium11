"""Sellability (pure) — no double counting, renormalize on missing, confidence
separate, no bypass, batch-invariant. Includes the product_momentum component
(the candidate's own review velocity — non-overlapping)."""

from __future__ import annotations

import pytest

from delium.analysis.models import Confidence
from delium.analysis.sellability import (
    SellabilityInput,
    compute_sellability,
    load_sellability_data,
    product_momentum_score,
)

W = load_sellability_data("us")


def test_all_components_weighted_and_high_confidence() -> None:
    r = compute_sellability(
        SellabilityInput(
            opportunity_score=60, price_headroom=80, incumbent_freshness=40, product_momentum=50
        ),
        W,
    )
    # (60*55 + 80*22 + 40*13 + 50*10) / 100 = (3300+1760+520+500)/100 = 60.8
    assert r.score == pytest.approx(60.8)
    assert r.confidence is Confidence.HIGH  # all four present
    assert r.missing == ()


def test_opportunity_only_renormalizes_to_opportunity_low_confidence() -> None:
    r = compute_sellability(SellabilityInput(opportunity_score=68), W)
    assert r.score == 68.0  # renormalized over the one available component
    assert r.confidence is Confidence.LOW
    assert set(r.missing) == {"price_headroom", "incumbent_freshness", "product_momentum"}


def test_confidence_is_separate_from_score() -> None:
    two = compute_sellability(SellabilityInput(opportunity_score=70, price_headroom=70), W)
    assert two.score == 70.0  # (70*55 + 70*22)/77
    assert two.confidence is Confidence.MEDIUM  # 2 of 4 present


def test_ineligible_never_scores_no_bypass() -> None:
    r = compute_sellability(
        SellabilityInput(opportunity_score=90, price_headroom=90, eligible=False), W
    )
    assert r.score is None
    assert "did not pass" in r.reason


def test_same_inputs_same_score_regardless_of_batch() -> None:
    inp = SellabilityInput(
        opportunity_score=55, price_headroom=65, incumbent_freshness=45, product_momentum=30
    )
    a = compute_sellability(inp, W)
    compute_sellability(SellabilityInput(opportunity_score=10), W)
    compute_sellability(SellabilityInput(opportunity_score=99, price_headroom=99), W)
    b = compute_sellability(inp, W)
    assert a.score == b.score


def test_profitability_not_double_counted() -> None:
    # Components are opportunity + the three non-overlapping signals. No separate
    # margin/profit term, so profitability (inside opportunity) is counted once.
    r = compute_sellability(SellabilityInput(opportunity_score=60, price_headroom=60), W)
    names = {c.name for c in r.components}
    assert names == {"opportunity", "price_headroom", "incumbent_freshness", "product_momentum"}
    assert "profit" not in names and "margin" not in names


def test_product_momentum_score_from_review_velocity() -> None:
    assert product_momentum_score(None, W) is None
    assert product_momentum_score(0, W) is None
    lo = product_momentum_score(1, W)
    hi = product_momentum_score(120, W)
    assert lo is not None and hi is not None
    assert lo <= 5 and hi >= 95  # log-scaled across the configured band
    # Monotonic: more reviews/month → higher momentum.
    assert product_momentum_score(40, W) > product_momentum_score(10, W)  # type: ignore[operator]


def test_product_momentum_is_own_signal_absolute() -> None:
    # Same review velocity → same momentum score regardless of anything else.
    a = product_momentum_score(30, W)
    b = product_momentum_score(30, W)
    assert a == b
