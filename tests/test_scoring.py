"""Opportunity scoring engine: funnel, kills, gates, sufficiency, verdicts.

The four-stage funnel (scoring-model §1-§10) is exercised end-to-end with the
factories in `scoring_support`, which default to sufficient, no-kill data so a
test can flip exactly one dimension.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from delium.analysis.models import Confidence, ScoringInput, Verdict
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

# A profit scenario whose stressed case pins the profitability pillar to 100.
_PROFIT_100 = dict(
    stressed_margin=0.50, stressed_roi=3.5, stressed_payback=3.0, stressed_capital_cents=800_000
)

# Clean Stage-0 kill facts: nothing trips.
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


def _input(
    *,
    demand: float = 80,
    competition: float = 70,
    differentiation: float = 70,
    risk: float = 100,
    profit_kwargs: dict[str, object] | None = None,
    **facts: object,
) -> ScoringInput:
    merged = {**_CLEAN_FACTS, **facts}
    return ScoringInput(
        demand=demand_report(demand),
        competition=competition_report(competition),
        differentiation=differentiation_report(differentiation),
        profit=scenario_set(**(profit_kwargs or _PROFIT_100)),  # type: ignore[arg-type]
        risk=risk_report(risk),
        **merged,  # type: ignore[arg-type]
    )


def _score(**kw: object) -> Verdict:
    return score_opportunity(_input(**kw), CFG).verdict  # type: ignore[arg-type]


# --- hand-worked composite ------------------------------------------------
def test_hand_worked_composite() -> None:
    # d=72 c=60 df=60 profit=100 risk=100 →
    # .25·72 + .25·60 + .20·60 + .20·100 + .10·100 = 18+15+12+20+10 = 75.0
    r = score_opportunity(_input(demand=72, competition=60, differentiation=60, risk=100), CFG)
    assert r.score == pytest.approx(75.0)
    contribs = {p.pillar: p.weighted_contribution for p in r.pillars}
    assert contribs["demand"] == pytest.approx(18.0)
    assert contribs["competition"] == pytest.approx(15.0)
    assert contribs["differentiation"] == pytest.approx(12.0)
    assert contribs["profitability"] == pytest.approx(20.0)
    assert contribs["risk"] == pytest.approx(10.0)
    # Contributions sum exactly to the composite (property 9).
    assert sum(contribs.values()) == pytest.approx(r.score)


def test_pillar_weights_come_from_config() -> None:
    r = score_opportunity(_input(), CFG)
    weights = {p.pillar: p.weight for p in r.pillars}
    assert weights == {
        "demand": 25,
        "competition": 25,
        "differentiation": 20,
        "profitability": 20,
        "risk": 10,
    }


# --- verdict boundaries ---------------------------------------------------
def test_buy_boundary_exact_75() -> None:
    r = score_opportunity(_input(demand=72, competition=60, differentiation=60), CFG)
    assert r.score == pytest.approx(75.0)
    assert r.verdict is Verdict.BUY


def test_just_below_buy_is_test() -> None:
    r = score_opportunity(_input(demand=71.96, competition=60, differentiation=60), CFG)
    assert r.score < 75.0
    assert r.verdict is Verdict.TEST


def test_test_lower_boundary_exact_60() -> None:
    # d=30 c=50 df=50 profit=100 risk=100 → 7.5+12.5+10+20+10 = 60.0
    r = score_opportunity(_input(demand=30, competition=50, differentiation=50), CFG)
    assert r.score == pytest.approx(60.0)
    assert r.verdict is Verdict.TEST


def test_just_below_test_is_avoid() -> None:
    r = score_opportunity(_input(demand=29.9, competition=50, differentiation=50), CFG)
    assert r.score < 60.0
    assert r.verdict is Verdict.AVOID


def test_score_75_all_gates_passing_is_buy() -> None:
    assert _score(demand=72, competition=60, differentiation=60) is Verdict.BUY


def test_score_75_one_gate_failing_is_test() -> None:
    # Same 75 composite but differentiation below the floor → G4 fails → TEST.
    # .25·72.4 + .25·72.4 + .20·44 + 20 + 10 = 18.1+18.1+8.8+30 = 75.0
    r = score_opportunity(_input(demand=72.4, competition=72.4, differentiation=44), CFG)
    assert r.score == pytest.approx(75.0)
    assert r.verdict is Verdict.TEST
    assert "G4" in r.failed_gates


# --- differentiation floor (G4) -------------------------------------------
def test_differentiation_floor_exactly_45_passes() -> None:
    r = score_opportunity(_input(differentiation=45), CFG)
    g4 = next(g for g in r.gates if g.gate_id == "G4")
    assert g4.passed is True


def test_differentiation_floor_4499_fails_and_blocks_buy() -> None:
    r = score_opportunity(_input(demand=100, competition=100, differentiation=44.99), CFG)
    g4 = next(g for g in r.gates if g.gate_id == "G4")
    assert g4.passed is False
    assert r.verdict is not Verdict.BUY


# --- risk floor (G3, hard) ------------------------------------------------
def test_risk_floor_39_hard_fails_to_avoid() -> None:
    r = score_opportunity(_input(demand=95, competition=95, differentiation=95, risk=39), CFG)
    assert r.verdict is Verdict.AVOID
    assert "G3" in r.failed_gates


def test_risk_floor_exactly_40_passes() -> None:
    r = score_opportunity(_input(risk=40), CFG)
    g3 = next(g for g in r.gates if g.gate_id == "G3")
    assert g3.passed is True


def test_risk_0_and_risk_100() -> None:
    lo = score_opportunity(_input(risk=0), CFG)
    hi = score_opportunity(_input(risk=100), CFG)
    assert next(g for g in lo.gates if g.gate_id == "G3").passed is False
    assert next(g for g in hi.gates if g.gate_id == "G3").passed is True
    assert hi.score > lo.score  # risk pillar contributes positively


# --- all-zero / all-100 ---------------------------------------------------
def test_all_pillars_zero() -> None:
    zero_profit = dict(
        stressed_margin=0.0,
        stressed_roi=0.0,
        stressed_payback=None,
        stressed_capital_cents=0,
        expected_margin=0.0,
        expected_roi=0.0,
        expected_payback=None,
    )
    r = score_opportunity(
        _input(demand=0, competition=0, differentiation=0, risk=0, profit_kwargs=zero_profit),
        CFG,
    )
    assert r.score == pytest.approx(0.0)
    assert r.verdict is Verdict.AVOID


def test_all_pillars_hundred() -> None:
    r = score_opportunity(_input(demand=100, competition=100, differentiation=100, risk=100), CFG)
    assert r.score == pytest.approx(100.0)
    assert r.verdict is Verdict.BUY


# --- hard kills -----------------------------------------------------------
def test_excellent_score_but_hard_kill_is_avoid() -> None:
    r = score_opportunity(
        _input(demand=100, competition=100, differentiation=100, ip_signature=True), CFG
    )
    assert r.score > 75
    assert r.verdict is Verdict.AVOID
    assert r.hard_kill_triggered
    assert any(k.rule_id == "K9" and k.kills for k in r.kills)


def test_k1_price_below_floor() -> None:
    r = score_opportunity(_input(market_median_price_cents=1000), CFG)  # $10 < $15
    k1 = next(k for k in r.kills if k.rule_id == "K1")
    assert k1.triggered and k1.kills
    assert r.verdict is Verdict.AVOID


def test_k2_price_above_ceiling() -> None:
    r = score_opportunity(_input(market_median_price_cents=9000), CFG)  # $90 > $70
    k2 = next(k for k in r.kills if k.rule_id == "K2")
    assert k2.triggered and k2.kills


def test_k3_oversized() -> None:
    r = score_opportunity(_input(oversized=True), CFG)
    assert next(k for k in r.kills if k.rule_id == "K3").kills


def test_k4_amazon_in_top5() -> None:
    r = score_opportunity(_input(amazon_in_top5=True), CFG)
    assert next(k for k in r.kills if k.rule_id == "K4").kills


def test_k5_brand_dominance() -> None:
    inp = _input()
    inp = ScoringInput(
        **{**inp.__dict__, "competition": competition_report(70, top_brand_slot_share=0.6)}
    )
    r = score_opportunity(inp, CFG)
    k5 = next(k for k in r.kills if k.rule_id == "K5")
    assert k5.triggered and k5.kills  # 6 of 10 slots ≥ 5


def test_k6_review_moat() -> None:
    inp = _input()
    inp = ScoringInput(
        **{**inp.__dict__, "competition": competition_report(70, median_reviews=3500)}
    )
    r = score_opportunity(inp, CFG)
    assert next(k for k in r.kills if k.rule_id == "K6").kills


def test_k7_no_wedge() -> None:
    inp = _input(market_complaint_rate=0.02)
    inp = ScoringInput(
        **{**inp.__dict__, "competition": competition_report(70, avg_competitor_quality=85.0)}
    )
    r = score_opportunity(inp, CFG)
    k7 = next(k for k in r.kills if k.rule_id == "K7")
    assert k7.triggered and k7.kills


def test_k8_restricted_category() -> None:
    r = score_opportunity(_input(restricted_category=True), CFG)
    assert next(k for k in r.kills if k.rule_id == "K8").kills


def test_k10_fad_spike() -> None:
    r = score_opportunity(
        _input(fad_search_volume=40000, fad_volume_12mo_median=10000, volume_history_months=6), CFG
    )
    k10 = next(k for k in r.kills if k.rule_id == "K10")
    assert k10.triggered and k10.kills  # 4× median AND < 12mo


def test_k10_not_fad_when_enough_history() -> None:
    r = score_opportunity(
        _input(fad_search_volume=40000, fad_volume_12mo_median=10000, volume_history_months=24), CFG
    )
    assert not next(k for k in r.kills if k.rule_id == "K10").triggered


def test_k11_capital_over_band() -> None:
    r = score_opportunity(
        _input(profit_kwargs={**_PROFIT_100, "stressed_capital_cents": 3_000_000}), CFG
    )  # $30k > $20k
    assert next(k for k in r.kills if k.rule_id == "K11").kills


def test_k11_capital_under_band() -> None:
    r = score_opportunity(
        _input(profit_kwargs={**_PROFIT_100, "stressed_capital_cents": 100_000}), CFG
    )  # $1k < $2k
    assert next(k for k in r.kills if k.rule_id == "K11").kills


def test_k12_avoid_list_match() -> None:
    r = score_opportunity(_input(avoid_matches=("glass",)), CFG)
    assert next(k for k in r.kills if k.rule_id == "K12").kills
    assert r.verdict is Verdict.AVOID


# --- borderline demotion --------------------------------------------------
def test_borderline_price_demotes_to_test_not_kill() -> None:
    # $14 is < $15 but within the 10% borderline band (13.5-16.5) → demoted.
    r = score_opportunity(_input(market_median_price_cents=1400), CFG)
    k1 = next(k for k in r.kills if k.rule_id == "K1")
    assert k1.triggered and k1.demoted and not k1.kills
    assert r.verdict is Verdict.TEST


# --- unassessed kills -----------------------------------------------------
def test_missing_kill_facts_do_not_trigger() -> None:
    inp = ScoringInput(
        demand=demand_report(80),
        competition=competition_report(70),
        differentiation=differentiation_report(70),
        profit=scenario_set(**_PROFIT_100),  # type: ignore[arg-type]
        risk=risk_report(100),
        # every optional kill fact left as its default (None / empty)
    )
    r = score_opportunity(inp, CFG)
    unassessed = {k.rule_id for k in r.kills if not k.assessed}
    assert {"K1", "K2", "K3", "K4", "K7", "K8", "K9", "K10"} <= unassessed
    assert not r.hard_kill_triggered  # missing facts never kill


# --- profit gate G1 -------------------------------------------------------
def test_g1_failure_caps_profit_pillar_and_avoids() -> None:
    bad = dict(
        stressed_margin=0.50,
        stressed_roi=3.5,
        stressed_payback=3.0,
        stressed_capital_cents=800_000,
        expected_margin=0.10,
        expected_roi=0.5,
        expected_payback=8.0,
    )
    r = score_opportunity(_input(profit_kwargs=bad), CFG)
    profit = next(p for p in r.pillars if p.pillar == "profitability")
    assert profit.raw_score is not None and profit.raw_score > 40
    assert profit.capped_score == pytest.approx(40.0)  # §7 gate-linkage cap
    assert "G1" in r.failed_gates
    assert r.verdict is Verdict.AVOID  # G1 is a hard gate


def test_g1_passes_when_unstressed_gates_met() -> None:
    r = score_opportunity(_input(), CFG)
    assert next(g for g in r.gates if g.gate_id == "G1").passed is True


# --- data sufficiency -----------------------------------------------------
def test_thin_reviews_caps_differentiation_and_blocks_buy() -> None:
    inp = _input(demand=100, competition=100)
    inp = ScoringInput(
        **{**inp.__dict__, "differentiation": differentiation_report(90, sample_size=50)}
    )
    r = score_opportunity(inp, CFG)
    diff = next(p for p in r.pillars if p.pillar == "differentiation")
    assert diff.capped_score == pytest.approx(50.0)  # capped from 90
    assert diff.partial
    assert r.insufficient_data
    assert "G2" in r.failed_gates
    assert r.verdict is not Verdict.BUY


def test_thin_keepa_caps_demand_at_60() -> None:
    inp = ScoringInput(
        **{
            **_input().__dict__,
            "demand": demand_report(90, keepa_asins_with_history=3),
        }
    )
    r = score_opportunity(inp, CFG)
    demand = next(p for p in r.pillars if p.pillar == "demand")
    assert demand.capped_score == pytest.approx(60.0)
    assert demand.partial


def test_missing_keywords_caps_demand_at_50() -> None:
    inp = ScoringInput(
        **{
            **_input().__dict__,
            "demand": demand_report(90, volumed_phrases=2),
        }
    )
    r = score_opportunity(inp, CFG)
    demand = next(p for p in r.pillars if p.pillar == "demand")
    assert demand.capped_score == pytest.approx(50.0)  # tighter of the two caps


def test_insufficient_data_is_not_bad_opportunity() -> None:
    inp = ScoringInput(
        **{
            **_input(demand=100, competition=100).__dict__,
            "differentiation": differentiation_report(90, sample_size=10),
        }
    )
    r = score_opportunity(inp, CFG)
    assert r.insufficient_data
    assert not r.bad_opportunity  # thin evidence, not a bad market
    assert r.verdict is Verdict.TEST  # research-later


def test_bad_opportunity_is_distinct_from_insufficient() -> None:
    r = score_opportunity(_input(ip_signature=True), CFG)
    assert r.verdict is Verdict.AVOID
    assert r.bad_opportunity
    assert not r.insufficient_data


# --- missing pillars ------------------------------------------------------
def test_missing_one_pillar_blocks_buy() -> None:
    inp = ScoringInput(**{**_input(demand=100, competition=100).__dict__, "risk": None})
    r = score_opportunity(inp, CFG)
    risk = next(p for p in r.pillars if p.pillar == "risk")
    assert not risk.available
    assert risk.weighted_contribution == pytest.approx(0.0)  # missing = 0, not optimistic
    assert r.verdict is not Verdict.BUY


def test_missing_profit_is_blocking_insufficient() -> None:
    inp = ScoringInput(**{**_input().__dict__, "profit": None})
    r = score_opportunity(inp, CFG)
    assert r.insufficient_data
    assert not r.bad_opportunity
    assert "G1" in r.failed_gates
    assert r.verdict is Verdict.AVOID  # fees blocking (§3)


# --- confidence -----------------------------------------------------------
def test_confidence_high_when_all_sufficient() -> None:
    r = score_opportunity(_input(), CFG)
    assert r.confidence.level is Confidence.HIGH


def test_partial_pillar_forces_low_confidence() -> None:
    inp = ScoringInput(**{**_input().__dict__, "demand": demand_report(90, volumed_phrases=1)})
    r = score_opportunity(inp, CFG)
    assert r.confidence.level is Confidence.LOW
    assert "demand" in r.confidence.partial_pillars


def test_low_confidence_report_downgrades_score_confidence() -> None:
    inp = ScoringInput(
        **{**_input().__dict__, "competition": competition_report(70, confidence=Confidence.MEDIUM)}
    )
    r = score_opportunity(inp, CFG)
    assert r.confidence.level is Confidence.MEDIUM


# --- verdict ordering / kills ordering ------------------------------------
def test_kills_all_twelve_present() -> None:
    r = score_opportunity(_input(), CFG)
    assert {k.rule_id for k in r.kills} == {f"K{i}" for i in range(1, 13)}


def test_gates_all_five_present() -> None:
    r = score_opportunity(_input(), CFG)
    assert [g.gate_id for g in r.gates] == ["G1", "G2", "G3", "G4", "G5"]


def test_strategist_gate_is_pending() -> None:
    r = score_opportunity(_input(), CFG)
    g5 = next(g for g in r.gates if g.gate_id == "G5")
    assert g5.passed is None
    assert r.strategist_pending


# --- config snapshot / reproducibility ------------------------------------
def test_identical_inputs_identical_outputs() -> None:
    inp = _input()
    assert score_opportunity(inp, CFG) == score_opportunity(inp, CFG)


def test_config_snapshot_captures_thresholds() -> None:
    r = score_opportunity(_input(), CFG)
    snap = dict(r.config_snapshot.weights)
    assert snap["demand"] == 25
    gates = dict(r.config_snapshot.gate_thresholds)
    assert gates["differentiation_floor"] == 45
    verdicts = dict(r.config_snapshot.verdict_thresholds)
    assert verdicts["buy_min"] == 75 and verdicts["test_min"] == 60


def test_config_snapshot_reproduces_after_drift() -> None:
    # Old score keeps its snapshot even when config weights change (§11.3).
    old = score_opportunity(_input(), CFG)
    drifted = DeliumConfig.model_validate({"score_weights": {"demand": 40}})
    new = score_opportunity(_input(), drifted)
    assert dict(old.config_snapshot.weights)["demand"] == 25
    assert dict(new.config_snapshot.weights)["demand"] == 40
    assert old.score != new.score  # future scores move; the snapshot pins the old one


def test_custom_config_thresholds_are_honored() -> None:
    cfg = DeliumConfig.model_validate({"verdicts": {"buy_min": 90, "test_min": 70}})
    # Composite ~75 would be BUY by default but only TEST at buy_min=90.
    r = score_opportunity(_input(demand=72, competition=60, differentiation=60), cfg)
    assert r.score == pytest.approx(75.0)
    assert r.verdict is Verdict.TEST


# --- purity guards --------------------------------------------------------
def test_scoring_module_has_no_forbidden_imports() -> None:
    src = Path("src/delium/analysis/scoring.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = (
        "datetime",
        "time",
        "random",
        "secrets",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "sqlite3",
    )
    for mod in imported:
        assert not mod.startswith(forbidden), f"forbidden import: {mod}"
        assert "provider" not in mod and "ingestion" not in mod and "database" not in mod
        assert "agent" not in mod and "llm" not in mod


# --- helper / defensive-branch coverage -----------------------------------
def test_within_band_zero_threshold() -> None:
    from delium.analysis.scoring import _within_band

    assert _within_band(0.05, 0.0, 0.1) is True
    assert _within_band(0.2, 0.0, 0.1) is False


def test_sufficiency_helpers_treat_missing_reports_pessimistically() -> None:
    from delium.analysis.scoring import (
        _keepa_sufficient,
        _keywords_sufficient,
        _price_history_sufficient,
        _profit_confidence,
        _reviews_sufficient,
    )

    empty = ScoringInput()
    assert _keepa_sufficient(empty, CFG) is False
    assert _keywords_sufficient(empty, CFG) is False
    assert _reviews_sufficient(empty, CFG) is False
    assert _price_history_sufficient(empty, CFG) is False
    assert _profit_confidence(None) is Confidence.LOW


def test_k5_k6_unassessed_when_subfields_missing() -> None:
    inp = ScoringInput(
        **{
            **_input().__dict__,
            "competition": competition_report(70, top_brand_slot_share=None, median_reviews=None),  # type: ignore[arg-type]
        }
    )
    r = score_opportunity(inp, CFG)
    assert not next(k for k in r.kills if k.rule_id == "K5").assessed
    assert not next(k for k in r.kills if k.rule_id == "K6").assessed


def test_all_pillars_absent() -> None:
    r = score_opportunity(ScoringInput(), CFG)
    assert r.score == pytest.approx(0.0)
    assert all(not p.available for p in r.pillars)
    assert r.verdict is Verdict.AVOID
    g4 = next(g for g in r.gates if g.gate_id == "G4")
    assert g4.passed is False  # differentiation report absent


def test_capital_fit_decay_bands() -> None:
    # Profit pillar raw strictly decreases as capital climbs through the bands.
    def profit_raw(capital: int) -> float:
        r = score_opportunity(
            _input(profit_kwargs={**_PROFIT_100, "stressed_capital_cents": capital}), CFG
        )
        p = next(p for p in r.pillars if p.pillar == "profitability")
        assert p.raw_score is not None
        return p.raw_score

    ideal = profit_raw(800_000)  # $8k → P3 100
    soft = profit_raw(1_550_000)  # $15.5k → P3 80
    hard = profit_raw(1_850_000)  # $18.5k → P3 30
    assert ideal > soft > hard


def test_capital_fit_thin_market() -> None:
    r = score_opportunity(
        _input(profit_kwargs={**_PROFIT_100, "stressed_capital_cents": 400_000}), CFG
    )  # $4k → thin-market P3 = 70
    p = next(p for p in r.pillars if p.pillar == "profitability")
    comp = {s.name: s.value for s in p.components}
    assert comp["capital_fit"] == pytest.approx(70.0)


def test_payback_middle_band() -> None:
    mid = dict(
        stressed_margin=0.50, stressed_roi=3.5, stressed_payback=6.0, stressed_capital_cents=800_000
    )
    r = score_opportunity(_input(profit_kwargs=mid), CFG)
    p = next(p for p in r.pillars if p.pillar == "profitability")
    comp = {s.name: s.value for s in p.components}
    assert comp["payback"] == pytest.approx(60.0)  # 100 − (6−4)/5·100


def test_never_recouped_payback_scores_zero() -> None:
    never = dict(
        stressed_margin=0.50,
        stressed_roi=3.5,
        stressed_payback=None,
        stressed_capital_cents=800_000,
    )
    r = score_opportunity(_input(profit_kwargs=never), CFG)
    p = next(p for p in r.pillars if p.pillar == "profitability")
    comp = {s.name: s.value for s in p.components}
    assert comp["payback"] == pytest.approx(0.0)
    # A payback beyond the zero-months horizon also scores 0.
    slow = dict(
        stressed_margin=0.50,
        stressed_roi=3.5,
        stressed_payback=10.0,
        stressed_capital_cents=800_000,
    )
    r2 = score_opportunity(_input(profit_kwargs=slow), CFG)
    p2 = next(p for p in r2.pillars if p.pillar == "profitability")
    assert {s.name: s.value for s in p2.components}["payback"] == pytest.approx(0.0)


def test_low_confidence_report_without_partial_yields_low() -> None:
    inp = ScoringInput(**{**_input().__dict__, "risk": risk_report(90, confidence=Confidence.LOW)})
    r = score_opportunity(inp, CFG)
    assert r.confidence.level is Confidence.LOW
    assert not r.confidence.partial_pillars  # low came from report confidence, not a cap


def test_cap_flags_partial_even_when_raw_already_below_cap() -> None:
    # Demand raw 40 (< 50 keyword cap): score unchanged but pillar still partial.
    inp = ScoringInput(**{**_input().__dict__, "demand": demand_report(40, volumed_phrases=1)})
    r = score_opportunity(inp, CFG)
    demand = next(p for p in r.pillars if p.pillar == "demand")
    assert demand.capped_score == pytest.approx(40.0)
    assert demand.partial


def test_scoring_module_reads_no_clock_or_random() -> None:
    # Scan code only — strip the module docstring (which legitimately names the
    # forbidden concepts when describing the purity contract).
    src = Path("src/delium/analysis/scoring.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    docstring = ast.get_docstring(tree) or ""
    code = src.replace(docstring, "")
    for banned in ("now(", "today(", "random.", "random(", "open(", "utcnow", "time("):
        assert banned not in code, f"banned call present: {banned}"
