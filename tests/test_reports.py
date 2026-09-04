"""Validation report rendering tests (docs/agent-layer.md §6).

Renders from typed objects only, cleanly separates deterministic numbers from the
advisory Strategist narrative, escapes untrusted model/review text, and shows
quotes fetched by id — never re-emitted model text.
"""

from __future__ import annotations

from pathlib import Path

import agents_support as fake
import discovery_support as seed
import validation_support as vs
from agents_support import RoutingLlmTransport, build_llm, json_body
from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.reports.render import render_validation
from delium.validation import Clients, ValidationRequest, run_validation
from delium.validation.models import Marketplace

CFG = DeliumConfig()
TGT = "B0TARGET01"
C1, C2, C3 = "B0COMPET01", "B0COMPET02", "B0COMPET03"


def _run_with_agents(*, miner=None, strategist=None):  # type: ignore[no-untyped-def]
    ids = [f"{TGT}-R{i}" for i in range(8)]
    miner = miner if miner is not None else json_body(fake.miner_payload(complaint_ids=ids))
    strategist = (
        strategist if strategist is not None else json_body(fake.strategist_payload(verdict="buy"))
    )
    llm = build_llm(RoutingLlmTransport(miner=miner, strategist=strategist), CFG.agents)
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=(TGT, C1, C2, C3))
        for a in (TGT, C1, C2, C3):
            vs.seed_reviews(conn, rid, a, n=180)
        rid2 = repository.insert_run(conn, command="validate", input_=TGT)
    with get_connection() as conn:
        return run_validation(
            conn,
            ValidationRequest(target=TGT, marketplace=Marketplace.US, run_id=rid2),
            CFG,
            Clients(llm=llm),
        )


def test_render_separates_deterministic_and_strategist(initialized_db: Path) -> None:
    report = _run_with_agents()
    text = render_validation(report, product_title="Silicone tray")
    assert "## Verdict" in text
    assert "## Pillars (deterministic)" in text
    assert "## Strategist recommendation (advisory" in text
    # The deterministic verdict is echoed; the advisory note is explicit.
    assert report.scored.verdict.value.upper() in text
    assert "does not set the verdict" in text


def test_render_escapes_untrusted_model_text(initialized_db: Path) -> None:
    # A malicious theme label with markup/control characters must be neutralized.
    ids = [f"{TGT}-R{i}" for i in range(8)]
    evil = fake.miner_payload(complaint_ids=ids, theme="pwn [bold]red[/bold] `x` | y")
    report = _run_with_agents(miner=json_body(evil))
    text = render_validation(report)
    # The firewall neutralizes backticks (→') and pipes (→/) so model text cannot
    # break the Markdown table or inject markup; the label still renders as data.
    assert "pwn [bold]red[/bold] 'x' / y" in text
    assert "`x`" not in text
    assert " | y" not in text  # raw pipe replaced


def test_render_regenerates_from_typed_objects_only(initialized_db: Path) -> None:
    # Rendering twice from the same report is identical and needs no LLM/DB call.
    report = _run_with_agents()
    assert render_validation(report) == render_validation(report)


def test_render_handles_missing_agents_gracefully(initialized_db: Path) -> None:
    # No LLM client → deterministic-only report still renders every core section.
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=(TGT, C1, C2, C3))
        vs.seed_reviews(conn, rid, TGT, n=40)
        rid2 = repository.insert_run(conn, command="validate", input_=TGT)
    with get_connection() as conn:
        report = run_validation(
            conn,
            ValidationRequest(target=TGT, marketplace=Marketplace.US, run_id=rid2),
            CFG,
            Clients(),
        )
    text = render_validation(report)
    assert "Review Miner: **not run" in text
    assert "Strategist not run" in text
    assert "## Verdict" in text


# ---------------------------------------------------------------------------
# Direct render unit tests (edge cases) — construct typed reports, no pipeline.
# ---------------------------------------------------------------------------
def _scored(**facts):  # type: ignore[no-untyped-def]
    from delium.analysis.models import ScoringInput, StrategistConcurrence
    from delium.analysis.scoring import score_opportunity
    from scoring_support import (
        competition_report,
        demand_report,
        differentiation_report,
        risk_report,
        scenario_set,
    )

    fields = dict(
        demand=demand_report(95),
        competition=competition_report(90),
        differentiation=differentiation_report(85),
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
        market_complaint_rate=0.22,
        restricted_category=False,
        ip_signature=False,
        fad_search_volume=9400,
        fad_volume_12mo_median=9000,
        volume_history_months=36,
    )
    fields.update(facts)
    return score_opportunity(ScoringInput(**fields), CFG, strategist=StrategistConcurrence.CONCUR)


def _make_report(**kw):  # type: ignore[no-untyped-def]
    from delium.validation.models import ValidationReport, ValidationStatus

    defaults = dict(
        request=ValidationRequest(target=TGT, marketplace=Marketplace.US, run_id="r1"),
        asin=TGT,
        marketplace=Marketplace.US,
        status=ValidationStatus.SCORED,
    )
    defaults.update(kw)
    return ValidationReport(**defaults)


def test_render_non_scored_status() -> None:
    from delium.validation.models import ValidationStatus

    report = _make_report(
        asin=None,
        status=ValidationStatus.INVALID_TARGET,
        scored=None,
        notes=("could not parse target",),
    )
    text = render_validation(report)
    assert "No verdict produced" in text
    assert "could not parse target" in text


def test_render_disagreement_banner() -> None:
    from delium.agents.schemas import StrategistVerdict

    scored = _scored()  # a BUY
    sv = StrategistVerdict.model_validate(fake.strategist_payload(verdict="avoid", agrees=False))
    text = render_validation(_make_report(scored=scored, strategist_verdict=sv))
    assert "Disagreement" in text
    assert "Strategist argues **AVOID**" in text
    # Strategist risk register renders in the Risks section.
    assert "(strategist)" in text


def test_render_hard_kill_banner() -> None:
    from delium.validation.models import ValidationStatus

    killed = _scored(ip_signature=True)  # K9 → AVOID
    report = _make_report(status=ValidationStatus.HARD_KILLED, scored=killed)
    text = render_validation(report)
    assert "Hard kill" in text and "K9" in text


def test_render_quotes_are_shown_by_id(initialized_db: Path) -> None:
    from delium.reports.render import quote_ids

    report = _run_with_agents()
    ids = quote_ids(report)
    assert ids  # counted complaint themes cite representative ids
    quotes = {ids[0]: (1, "the lid cracked on first freeze")}
    text = render_validation(report, quotes=quotes)
    assert "the lid cracked on first freeze" in text


def test_render_deterministic_risk_and_features() -> None:
    from delium.agents.schemas import MinerReport
    from delium.analysis.models import ScoringInput, StrategistConcurrence
    from delium.analysis.scoring import score_opportunity
    from scoring_support import (
        competition_report,
        demand_report,
        differentiation_report,
        risk_report,
        scenario_set,
    )

    scored = score_opportunity(
        ScoringInput(
            demand=demand_report(70),
            competition=competition_report(70),
            differentiation=differentiation_report(60),
            profit=scenario_set(
                stressed_margin=0.4,
                stressed_roi=2.0,
                stressed_payback=4.0,
                stressed_capital_cents=800_000,
            ),
            risk=risk_report(60, deductions=(("compliance", 30.0),)),
            market_median_price_cents=2200,
            oversized=False,
            amazon_in_top5=False,
            restricted_category=False,
            ip_signature=False,
            fad_search_volume=9400,
            fad_volume_12mo_median=9000,
            volume_history_months=36,
        ),
        CFG,
        strategist=StrategistConcurrence.PENDING,
    )
    miner = MinerReport.model_validate(
        {
            "missing_features": [
                {"feature": "silicone lid", "requested_in_review_ids": ["a", "b", "c"]}
            ],
            "bundle_signals": [
                {"complement": "storage bag", "mentioned_in_review_ids": ["a", "b", "c"]}
            ],
        }
    )
    text = render_validation(_make_report(scored=scored, miner_report=miner))
    assert "## Risks" in text and "(deterministic)" in text  # risk flag rendered
    assert "## Product opportunities" in text
    assert "silicone lid" in text and "storage bag" in text


def test_render_methodology_shows_agent_errors() -> None:
    from delium.validation.models import AgentRunInfo, HydrationOutcome

    scored = _scored()
    runs = (
        AgentRunInfo(
            agent="review_miner",
            status="failed",
            model="m",
            cost_usd=0.0,
            error="model refused the request",
        ),
        AgentRunInfo(agent="strategist", status="failed", model="m2", error="provider error"),
    )
    report = _make_report(
        scored=scored,
        agent_runs=runs,
        hydration=HydrationOutcome(data_cost_usd=0.5, llm_cost_usd=0.01, degraded=True),
    )
    text = render_validation(report)
    assert "## Methodology" in text
    assert "review_miner" in text and "model refused the request" in text
    assert "Run degraded" in text
    # Strategist section explains it did not produce a review (no verdict).
    assert "did not produce a review" in text
