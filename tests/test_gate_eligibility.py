"""`gate_eligibility` — the rank-stage distinction between a hard gate that
failed on PRESENT evidence (a real disqualification → exclude) and one that
failed only because its input was MISSING ("missing = unknown" → still rankable,
labelled pending). Pure: builds a real ScoredOpportunity from fixture pillars.
"""

from __future__ import annotations

from delium.analysis.models import ScoringInput
from delium.analysis.scoring import gate_eligibility, score_opportunity
from delium.config.models import DeliumConfig
from scoring_support import demand_report, risk_report, scenario_set

CFG = DeliumConfig()

# Clean Stage-0 facts so no hard KILL fires — this isolates the GATE half.
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


def _input(*, profit: object = None, risk_score: float | None = 90.0) -> ScoringInput:
    return ScoringInput(
        demand=demand_report(80),
        competition=None,
        differentiation=None,
        profit=profit,  # type: ignore[arg-type]
        risk=None if risk_score is None else risk_report(risk_score),
        **_CLEAN,  # type: ignore[arg-type]
    )


def _scored(**kw: object):  # type: ignore[no-untyped-def]
    return score_opportunity(_input(**kw), CFG)  # type: ignore[arg-type]


# --- G1 profit: missing input vs present-but-failing ----------------------
def test_missing_profit_is_pending_not_excluded() -> None:
    elig = gate_eligibility(_scored(profit=None))
    assert elig.excluded is False
    assert elig.blocked_gate is None
    assert "G1" in elig.pending_gates  # no profit model → unknown, awaited


def test_present_profit_below_threshold_is_excluded_with_numbers() -> None:
    failing = scenario_set(expected_margin=0.02, expected_roi=0.05, expected_payback=99.0)
    elig = gate_eligibility(_scored(profit=failing))
    assert elig.excluded is True
    assert elig.blocked_gate == "G1"
    assert elig.blocked_reason is not None
    # The reason carries the observed numbers and the required threshold.
    assert "G1" in elig.blocked_reason
    assert "2%" in elig.blocked_reason  # observed net margin
    assert "30%" in elig.blocked_reason  # required min margin
    assert "G1" not in elig.pending_gates  # a real fail, not pending


def test_present_profit_passing_is_not_excluded() -> None:
    passing = scenario_set(expected_margin=0.45, expected_roi=2.5, expected_payback=3.0)
    elig = gate_eligibility(_scored(profit=passing))
    assert elig.excluded is False
    assert "G1" not in elig.pending_gates


# --- G3 risk floor: same missing vs present distinction -------------------
def test_missing_risk_is_pending_not_excluded() -> None:
    # Profit passes (present) so G1 doesn't mask G3; risk report is absent.
    passing = scenario_set(expected_margin=0.45, expected_roi=2.5, expected_payback=3.0)
    elig = gate_eligibility(_scored(profit=passing, risk_score=None))
    assert elig.excluded is False
    assert "G3" in elig.pending_gates


def test_present_risk_below_floor_is_excluded() -> None:
    passing = scenario_set(expected_margin=0.45, expected_roi=2.5, expected_payback=3.0)
    elig = gate_eligibility(_scored(profit=passing, risk_score=10.0))  # below floor 40
    assert elig.excluded is True
    assert elig.blocked_gate == "G3"
    assert elig.blocked_reason is not None and "G3" in elig.blocked_reason
