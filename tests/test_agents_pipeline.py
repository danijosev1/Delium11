"""Agent-integrated validation pipeline tests + G5 verdict-ownership regressions.

The security-critical guarantees: the LLM never creates a Buy, never overrides a
hard kill or AVOID, its failure never fabricates evidence or raises confidence,
and it never runs on a hard-killed candidate.
"""

from __future__ import annotations

from pathlib import Path

import agents_support as fake
import discovery_support as seed
import validation_support as vs
from agents_support import RoutingLlmTransport, build_llm, http, json_body
from delium.analysis.models import ScoringInput, Verdict
from delium.analysis.models import StrategistConcurrence as Concur
from delium.analysis.scoring import score_opportunity
from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.validation import Clients, ValidationRequest, ValidationStatus, run_validation
from delium.validation.models import Marketplace

CFG = DeliumConfig()
US, AU = Marketplace.US, Marketplace.AU
TGT = "B0TARGET01"
C1, C2, C3 = "B0COMPET01", "B0COMPET02", "B0COMPET03"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _seed(conn, *, price=2200, reviews=180) -> str:  # type: ignore[no-untyped-def]
    rid = seed.new_run(conn)
    seed.seed_keyword_market(conn, rid, "US", asins=(TGT, C1, C2, C3), price_cents=price)
    for a in (TGT, C1, C2, C3):
        vs.seed_reviews(conn, rid, a, n=reviews)
    return rid


def _routing_llm(*, miner=None, strategist=None):  # type: ignore[no-untyped-def]
    ids = [f"{TGT}-R{i}" for i in range(8)]
    miner = (
        miner
        if miner is not None
        else json_body(
            fake.miner_payload(complaint_ids=ids, feature_ids=ids[:4], bundle_ids=ids[:3])
        )
    )
    strategist = (
        strategist if strategist is not None else json_body(fake.strategist_payload(verdict="buy"))
    )
    transport = RoutingLlmTransport(miner=miner, strategist=strategist)
    return build_llm(transport, CFG.agents), transport


def _validate(clients: Clients, *, marketplace=US, run_id=None) -> object:  # type: ignore[no-untyped-def]
    with get_connection() as conn:
        rid = run_id or repository.insert_run(conn, command="validate", input_=TGT)
    request = ValidationRequest(target=TGT, marketplace=marketplace, run_id=rid)
    with get_connection() as conn:
        return run_validation(conn, request, CFG, clients)


# ===========================================================================
# Full agent-integrated run
# ===========================================================================
def test_full_validation_runs_and_persists_agents(initialized_db: Path) -> None:
    with get_connection() as conn:
        _seed(conn)
    llm, transport = _routing_llm()
    report = _validate(Clients(llm=llm))
    assert report.status is ValidationStatus.SCORED
    assert report.review_evidence.miner_pending is False  # Miner ran
    assert report.miner_report is not None
    assert report.strategist_verdict is not None and report.strategist_verdict.verdict == "buy"
    assert {r.agent for r in report.agent_runs} == {"review_miner", "strategist"}
    assert report.llm_cost_usd > 0
    with get_connection() as conn:
        assert len(repository.get_agent_runs(conn, TGT, "US")) == 2
        assert repository.get_review_themes(conn, TGT)  # evidence persisted
        assert repository.get_feature_requests(conn, TGT)
        assert repository.get_bundle_signals(conn, TGT)


def test_hard_killed_candidate_never_calls_llm(initialized_db: Path) -> None:
    with get_connection() as conn:
        _seed(conn, price=800)  # $8 median < floor → K1
    llm, transport = _routing_llm()
    report = _validate(Clients(llm=llm))
    assert report.status is ValidationStatus.HARD_KILLED
    assert transport.calls == []  # LLM never touched on a hard kill (no spend)
    assert report.miner_report is None and report.strategist_verdict is None


# ===========================================================================
# Degradation — a failed agent never fabricates or raises confidence
# ===========================================================================
def test_miner_failure_still_produces_deterministic_validation(initialized_db: Path) -> None:
    with get_connection() as conn:
        _seed(conn)
    llm, _ = _routing_llm(miner=http(500))  # miner provider error
    report = _validate(Clients(llm=llm))
    assert report.status is ValidationStatus.SCORED  # deterministic result stands
    assert report.review_evidence.miner_pending is True  # miner did not produce evidence
    assert report.scored.verdict is not Verdict.BUY
    miner_run = next(r for r in report.agent_runs if r.agent == "review_miner")
    assert miner_run.status == "failed"
    with get_connection() as conn:
        assert repository.get_review_themes(conn, TGT) == []  # no fabricated evidence


def test_malformed_miner_output_creates_no_evidence(initialized_db: Path) -> None:
    with get_connection() as conn:
        _seed(conn)
    from agents_support import llm_body

    llm, _ = _routing_llm(miner=llm_body("total garbage, no json"))
    report = _validate(Clients(llm=llm))
    assert report.status is ValidationStatus.SCORED
    assert report.scored.verdict is not Verdict.BUY
    with get_connection() as conn:
        assert repository.get_review_themes(conn, TGT) == []


def test_strategist_failure_is_unavailable_and_preserves_result(initialized_db: Path) -> None:
    with get_connection() as conn:
        _seed(conn)
    llm, _ = _routing_llm(strategist=http(500))
    report = _validate(Clients(llm=llm))
    assert report.status is ValidationStatus.SCORED
    assert report.strategist_verdict is None
    assert report.scored.strategist_pending is True  # unavailable → provisional / no buy
    strat_run = next(r for r in report.agent_runs if r.agent == "strategist")
    assert strat_run.status == "failed"


def test_low_llm_budget_skips_strategist(initialized_db: Path) -> None:
    cfg = CFG.model_copy(
        update={"budgets": CFG.budgets.model_copy(update={"max_llm_usd_per_validate": 1e-9})}
    )
    with get_connection() as conn:
        _seed(conn)
    llm, _ = _routing_llm()
    with get_connection() as conn:
        rid = repository.insert_run(conn, command="validate", input_=TGT)
    with get_connection() as conn:
        report = run_validation(
            conn, ValidationRequest(target=TGT, marketplace=US, run_id=rid), cfg, Clients(llm=llm)
        )
    # Tiny budget: the Miner runs (evidence needed), but the Strategist is skipped;
    # a Buy cannot be confirmed without a Strategist review (concurrence UNAVAILABLE).
    assert report.status is ValidationStatus.SCORED
    assert report.strategist_verdict is None
    assert report.scored.strategist_pending is True


# ===========================================================================
# G5 verdict ownership (authoritative, via scoring.py) — STEP 9 regressions
# ===========================================================================
def _buy_qualifying() -> ScoringInput:
    from scoring_support import (
        competition_report,
        demand_report,
        differentiation_report,
        risk_report,
        scenario_set,
    )

    clean = dict(
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
    profit = dict(
        stressed_margin=0.5, stressed_roi=3.5, stressed_payback=3.0, stressed_capital_cents=800_000
    )
    return ScoringInput(
        demand=demand_report(95),
        competition=competition_report(90),
        differentiation=differentiation_report(85),
        profit=scenario_set(**profit),
        risk=risk_report(100),
        **clean,  # type: ignore[arg-type]
    )


def test_strategist_concur_confirms_buy_but_cannot_create_one() -> None:
    buy = _buy_qualifying()
    assert score_opportunity(buy, CFG, strategist=Concur.CONCUR).verdict is Verdict.BUY
    # A low-score input can never become BUY no matter what the Strategist concurs.
    low = ScoringInput()
    assert score_opportunity(low, CFG, strategist=Concur.CONCUR).verdict is not Verdict.BUY


def test_strategist_dissent_blocks_buy_to_test_never_avoids() -> None:
    buy = _buy_qualifying()
    result = score_opportunity(buy, CFG, strategist=Concur.DISSENT)
    assert result.verdict is Verdict.TEST  # blocked, not AVOID


def test_strategist_cannot_override_a_hard_kill() -> None:
    killed = ScoringInput(**{**_buy_qualifying().__dict__, "ip_signature": True})  # K9
    for c in (Concur.CONCUR, Concur.DISSENT, Concur.PENDING, Concur.UNAVAILABLE):
        assert score_opportunity(killed, CFG, strategist=c).verdict is Verdict.AVOID


def test_strategist_cannot_override_a_deterministic_avoid() -> None:
    # A sub-test_min score is AVOID regardless of a Strategist "buy".
    from scoring_support import (
        competition_report,
        demand_report,
        differentiation_report,
        risk_report,
        scenario_set,
    )

    weak = ScoringInput(
        demand=demand_report(5),
        competition=competition_report(5),
        differentiation=differentiation_report(5),
        profit=scenario_set(
            stressed_margin=0.5,
            stressed_roi=3.5,
            stressed_payback=3.0,
            stressed_capital_cents=800_000,
        ),
        risk=risk_report(100),
        market_median_price_cents=2200,
        oversized=False,
        amazon_in_top5=False,
        restricted_category=False,
        ip_signature=False,
        fad_search_volume=9400,
        fad_volume_12mo_median=9000,
        volume_history_months=36,
    )
    assert score_opportunity(weak, CFG, strategist=Concur.CONCUR).verdict is Verdict.AVOID


def test_unavailable_never_more_confident_than_pending() -> None:
    buy = _buy_qualifying()
    pending = score_opportunity(buy, CFG, strategist=Concur.PENDING)
    unavailable = score_opportunity(buy, CFG, strategist=Concur.UNAVAILABLE)
    # Pending allows a provisional Buy; unavailable can only be as cautious or more.
    rank = {Verdict.AVOID: 0, Verdict.TEST: 1, Verdict.BUY: 2}
    assert rank[unavailable.verdict] <= rank[pending.verdict]


# ===========================================================================
# Marketplace isolation holds with agents present
# ===========================================================================
def test_marketplace_isolation_with_agents(initialized_db: Path) -> None:
    with get_connection() as conn:
        _seed(conn)  # US only
    llm, transport = _routing_llm()
    report = _validate(Clients(llm=llm), marketplace=AU)  # AU has no cached product
    assert report.status is ValidationStatus.MISSING_CREDENTIALS
    assert transport.calls == []  # no product → no reviews → no LLM


# ===========================================================================
# Determinism: same data + same (fake) agent output → same verdict & snapshot
# ===========================================================================
def test_agent_run_is_deterministic(initialized_db: Path) -> None:
    from delium.validation import validation_snapshot

    with get_connection() as conn:
        _seed(conn)
    llm1, _ = _routing_llm()
    first = _validate(Clients(llm=llm1))
    llm2, _ = _routing_llm()
    second = _validate(Clients(llm=llm2))
    assert first.scored.score == second.scored.score
    assert first.scored.verdict == second.scored.verdict
    assert validation_snapshot(first.scored) == validation_snapshot(second.scored)
