"""Task 1 — missing data is UNKNOWN, not BAD (scoring-model §3).

An absent pillar must be excluded from the composite (renormalized away) so it
lowers *confidence*, not the *score* — while the gates + confidence still stop a
missing-data candidate from ever reading as a Buy. A present-but-thin pillar
keeps its sufficiency cap. These lock the fix in place and guard the invariant.
"""

from __future__ import annotations

import pytest

from delium.analysis.models import Confidence, ScoringInput, Verdict
from delium.analysis.scoring import score_opportunity
from delium.config.models import DeliumConfig
from scoring_support import demand_report, risk_report

CFG = DeliumConfig()

_CLEAN_FACTS = dict(
    market_median_price_cents=2200,
    oversized=False,
    amazon_in_top5=False,
    market_complaint_rate=0.22,
    restricted_category=False,
    ip_signature=False,
    avoid_matches=(),
    fad_search_volume=9400,
    fad_volume_12mo_median=9000,
    volume_history_months=36,
)


def _finder_like() -> ScoringInput:
    """A finder candidate: demand + risk assessable, but competition (no SERP set),
    differentiation (no reviews/LLM) and profit (no fees) absent."""
    return ScoringInput(
        demand=demand_report(80),
        competition=None,
        differentiation=None,
        profit=None,
        risk=risk_report(100),
        **_CLEAN_FACTS,  # type: ignore[arg-type]
    )


def test_absent_pillars_renormalize_instead_of_zero_filling() -> None:
    r = score_opportunity(_finder_like(), CFG)
    demand_p = next(p for p in r.pillars if p.pillar == "demand")
    risk_p = next(p for p in r.pillars if p.pillar == "risk")
    assert demand_p.capped_score is not None and risk_p.capped_score is not None

    # Composite renormalizes over the AVAILABLE pillars only (demand 0.25 + risk
    # 0.10), NOT over the full weight total. Old zero-fill gave ~30; the honest
    # "what we can see" score is ~86.
    expected = (demand_p.capped_score * 0.25 + risk_p.capped_score * 0.10) / 0.35
    assert r.score == pytest.approx(expected, abs=0.5)
    assert r.score > 60  # would have been ~30 under the old missing=0 behavior

    # Absent pillars contribute nothing themselves…
    for name in ("competition", "differentiation", "profitability"):
        p = next(pp for pp in r.pillars if pp.pillar == name)
        assert not p.available
        assert p.weighted_contribution == pytest.approx(0.0)
    # …and the present pillars' contributions sum to the composite.
    assert sum(p.weighted_contribution for p in r.pillars) == pytest.approx(r.score, abs=0.1)


def test_missing_data_lowers_confidence_and_blocks_buy_not_score() -> None:
    r = score_opportunity(_finder_like(), CFG)
    # Unknown → LOW confidence, insufficient_data flagged, and a hard gate (G1,
    # fees blocking) forces AVOID. The high score never becomes a Buy.
    assert r.confidence.level is Confidence.LOW
    assert r.insufficient_data
    assert r.verdict is Verdict.AVOID
    assert "competition" in r.confidence.missing_pillars
    assert "profitability" in r.confidence.missing_pillars


def test_present_but_thin_pillar_still_capped() -> None:
    # A thin-but-present demand pillar keeps its §3 sufficiency cap (not renormalized
    # away) — only genuinely absent pillars are excluded.
    inp = ScoringInput(
        demand=demand_report(95, volumed_phrases=1),  # missing-keyword cap applies
        competition=None,
        differentiation=None,
        profit=None,
        risk=risk_report(100),
        **_CLEAN_FACTS,  # type: ignore[arg-type]
    )
    r = score_opportunity(inp, CFG)
    demand_p = next(p for p in r.pillars if p.pillar == "demand")
    assert demand_p.partial  # capped, still present
    assert demand_p.capped_score is not None and demand_p.capped_score <= 95
    assert "demand" in r.confidence.partial_pillars
