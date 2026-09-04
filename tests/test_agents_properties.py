"""Property-based invariants for the agent layer (hypothesis).

The load-bearing guarantees, over randomized inputs:
- the Strategist concurrence can never CREATE a Buy, override a hard kill, or make
  the verdict more confident than the deterministic pipeline already allows;
- the Review Miner's evidence resolution can never keep a theme that lacks enough
  real cited ids, so fabricated/duplicate ids can never enter the scored evidence.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from delium.agents.miner import make_evidence_check
from delium.agents.schemas import MinerReport
from delium.analysis.models import ScoringInput, Verdict
from delium.analysis.models import StrategistConcurrence as Concur
from delium.analysis.scoring import score_opportunity
from delium.config.models import DeliumConfig
from scoring_support import (
    competition_report,
    demand_report,
    differentiation_report,
    risk_report,
    scenario_set,
)

CFG = DeliumConfig()
_RANK = {Verdict.AVOID: 0, Verdict.TEST: 1, Verdict.BUY: 2}
_pillar = st.floats(min_value=0.0, max_value=100.0, allow_nan=False)
_PROFIT = dict(
    stressed_margin=0.50, stressed_roi=3.5, stressed_payback=3.0, stressed_capital_cents=800_000
)
_CLEAN = dict(
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


def _input(d: float, c: float, f: float, r: float, **facts: object) -> ScoringInput:
    return ScoringInput(
        demand=demand_report(d),
        competition=competition_report(c),
        differentiation=differentiation_report(f),
        profit=scenario_set(**_PROFIT),  # type: ignore[arg-type]
        risk=risk_report(r),
        **{**_CLEAN, **facts},  # type: ignore[arg-type]
    )


# 1 — DISSENT and UNAVAILABLE can never produce a BUY, for any input.
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_dissent_and_unavailable_never_buy(d: float, c: float, f: float, r: float) -> None:
    inp = _input(d, c, f, r)
    assert score_opportunity(inp, CFG, strategist=Concur.DISSENT).verdict is not Verdict.BUY
    assert score_opportunity(inp, CFG, strategist=Concur.UNAVAILABLE).verdict is not Verdict.BUY


# 2 — concurrence only matters at the Buy tier: if the provisional (pending)
# verdict is not BUY, no concurrence value changes it.
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_concurrence_cannot_change_a_non_buy(d: float, c: float, f: float, r: float) -> None:
    inp = _input(d, c, f, r)
    pending = score_opportunity(inp, CFG, strategist=Concur.PENDING).verdict
    if pending is not Verdict.BUY:
        for conc in (Concur.CONCUR, Concur.DISSENT, Concur.UNAVAILABLE):
            assert score_opportunity(inp, CFG, strategist=conc).verdict is pending


# 3 — the LLM can only reduce confidence: a dissent/unavailable verdict is never
# ranked higher than the concur/pending one on the same input.
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_llm_never_raises_verdict(d: float, c: float, f: float, r: float) -> None:
    inp = _input(d, c, f, r)
    concur = _RANK[score_opportunity(inp, CFG, strategist=Concur.CONCUR).verdict]
    dissent = _RANK[score_opportunity(inp, CFG, strategist=Concur.DISSENT).verdict]
    unavailable = _RANK[score_opportunity(inp, CFG, strategist=Concur.UNAVAILABLE).verdict]
    assert dissent <= concur
    assert unavailable <= concur


# 4 — a hard kill forces AVOID under every concurrence value (LLM can't rescue).
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_hard_kill_avoids_under_all_concurrence(d: float, c: float, f: float, r: float) -> None:
    killed = _input(d, c, f, r, ip_signature=True)  # K9 hard kill
    for conc in (Concur.PENDING, Concur.CONCUR, Concur.DISSENT, Concur.UNAVAILABLE):
        assert score_opportunity(killed, CFG, strategist=conc).verdict is Verdict.AVOID


# 5 — evidence resolution keeps a theme iff it has ≥ min real cited ids; fabricated
# or duplicated ids can never sneak a theme into the scored evidence.
@given(
    real=st.integers(min_value=0, max_value=5),
    dupes=st.integers(min_value=0, max_value=4),
    fakes=st.integers(min_value=0, max_value=4),
)
def test_evidence_check_keeps_only_well_supported_themes(real: int, dupes: int, fakes: int) -> None:
    eligible = frozenset(f"r{i}" for i in range(5))
    ids = [f"r{i}" for i in range(min(real, 5))]
    ids += ids[:dupes]  # duplicates collapse — must not count
    ids += [f"ghost{i}" for i in range(fakes)]  # fabricated — must not count
    report = MinerReport.model_validate({"complaints": [{"theme": "t", "quote_review_ids": ids}]})
    cleaned, dropped, total = make_evidence_check(eligible, 3)(report)
    kept = len(cleaned.complaints)
    unique_real = len(frozenset(ids) & eligible)
    assert kept == (1 if unique_real >= 3 else 0)
    assert dropped == (0 if unique_real >= 3 else 1)
    assert total == 1
