"""Property-based invariants for the scoring engine (hypothesis).

These test mathematical properties (bounds, monotonicity, determinism), not the
implementation line-by-line — per docs/analysis-engine.md §6 test guidance.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from delium.analysis.models import ScoringInput, Verdict
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
BUY_MIN = CFG.verdicts.buy_min
TEST_MIN = CFG.verdicts.test_min
DIFF_FLOOR = CFG.gates.differentiation_floor

_PROFIT_STRONG = dict(
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

_pillar = st.floats(min_value=0.0, max_value=100.0, allow_nan=False)


def _clean_input(
    demand: float = 80,
    competition: float = 70,
    differentiation: float = 70,
    risk: float = 100,
    profit_kwargs: dict[str, object] | None = None,
    **facts: object,
) -> ScoringInput:
    return ScoringInput(
        demand=demand_report(demand),
        competition=competition_report(competition),
        differentiation=differentiation_report(differentiation),
        profit=scenario_set(**(profit_kwargs or _PROFIT_STRONG)),  # type: ignore[arg-type]
        risk=risk_report(risk),
        **{**_CLEAN, **facts},  # type: ignore[arg-type]
    )


# 1 / 12 — bounds
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_score_always_within_bounds(d: float, c: float, f: float, r: float) -> None:
    result = score_opportunity(_clean_input(d, c, f, r), CFG)
    assert 0.0 <= result.score <= 100.0


# 9 — contributions sum to the composite
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_contributions_sum_to_score(d: float, c: float, f: float, r: float) -> None:
    result = score_opportunity(_clean_input(d, c, f, r), CFG)
    total = sum(p.weighted_contribution for p in result.pillars)
    assert abs(total - result.score) < 1e-9


# 2 — increasing a positive pillar cannot decrease the final score (gates/kills fixed)
@given(base=_pillar, delta=st.floats(min_value=0.0, max_value=100.0, allow_nan=False))
def test_raising_demand_never_lowers_score(base: float, delta: float) -> None:
    higher = min(100.0, base + delta)
    lo = score_opportunity(_clean_input(demand=base), CFG).score
    hi = score_opportunity(_clean_input(demand=higher), CFG).score
    assert hi >= lo - 1e-9


# 3 — more risk deductions (lower risk score) cannot raise the opportunity score
@given(risk=_pillar, drop=st.floats(min_value=0.0, max_value=100.0, allow_nan=False))
def test_more_risk_deductions_never_raise_score(risk: float, drop: float) -> None:
    lower = max(0.0, risk - drop)
    safer = score_opportunity(_clean_input(risk=risk), CFG).score
    riskier = score_opportunity(_clean_input(risk=lower), CFG).score
    assert riskier <= safer + 1e-9


# 4 — a triggered hard kill cannot BUY
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_hard_kill_never_buys(d: float, c: float, f: float, r: float) -> None:
    result = score_opportunity(_clean_input(d, c, f, r, ip_signature=True), CFG)
    assert result.verdict is not Verdict.BUY


# 5 — a failed mandatory (hard) gate cannot BUY
@given(d=_pillar, c=_pillar, f=_pillar)
def test_failed_risk_gate_never_buys(d: float, c: float, f: float) -> None:
    # risk 0 → G3 (hard) fails.
    result = score_opportunity(_clean_input(d, c, f, risk=0.0), CFG)
    assert result.verdict is not Verdict.BUY


# 6 — differentiation below the floor cannot BUY
@given(d=_pillar, c=_pillar, r=_pillar, f=st.floats(min_value=0.0, max_value=DIFF_FLOOR - 0.01))
def test_below_differentiation_floor_never_buys(d: float, c: float, r: float, f: float) -> None:
    result = score_opportunity(_clean_input(d, c, f, r), CFG)
    assert result.verdict is not Verdict.BUY


# 7 — missing/insufficient data cannot raise confidence
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_insufficient_data_never_raises_confidence(d: float, c: float, f: float, r: float) -> None:
    full = score_opportunity(_clean_input(d, c, f, r), CFG)
    thin = ScoringInput(**{**_clean_input(d, c, f, r).__dict__, "risk": None})
    thin_result = score_opportunity(thin, CFG)
    rank = {"high": 2, "medium": 1, "low": 0}
    assert rank[thin_result.confidence.level] <= rank[full.confidence.level]


# 8 — determinism
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_deterministic(d: float, c: float, f: float, r: float) -> None:
    inp = _clean_input(d, c, f, r)
    assert score_opportunity(inp, CFG) == score_opportunity(inp, CFG)


# 10 — BUY impossible below buy_min
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_buy_requires_threshold(d: float, c: float, f: float, r: float) -> None:
    result = score_opportunity(_clean_input(d, c, f, r), CFG)
    if result.verdict is Verdict.BUY:
        assert result.score >= BUY_MIN


# 11 — below test_min is always AVOID, never TEST/BUY
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_below_test_min_is_avoid(d: float, c: float, f: float, r: float) -> None:
    result = score_opportunity(_clean_input(d, c, f, r), CFG)
    if result.score < TEST_MIN:
        assert result.verdict is Verdict.AVOID
    if result.verdict is Verdict.TEST:
        assert result.score >= TEST_MIN
