"""Strategist agent (docs/agent-layer.md §4) + G5 concurrence derivation.

The Strategist is the capital-allocation argument. It reads the *computed, final*
deterministic numbers (it may challenge the assumptions behind them but never
restate different numbers) plus the validated Review Miner evidence, and returns
a structured recommendation. It does NOT own the verdict: its concurrence becomes
the G5 gate input, which scoring.py evaluates. G5 can only BLOCK a would-be Buy —
a dissent caps a Buy at Test, it never manufactures a Buy or an AVOID.
"""

from __future__ import annotations

import json

from delium.agents.llm import LlmClient, Tier
from delium.agents.runner import AgentResult, run_structured
from delium.agents.schemas import AnalystReport, MinerReport, StrategistVerdict
from delium.analysis.models import (
    ScoredOpportunity,
    ScoringInput,
    StrategistConcurrence,
)
from delium.config.models import DeliumConfig

STRATEGIST_SYSTEM = (
    "You are a private-label Amazon operator who has built 7-figure brands and "
    "lost money learning what the numbers don't say. You are deciding whether to "
    "invest this owner's real capital ($5-20k) in this product. The scores and "
    "financial figures provided are computed and FINAL — you may challenge the "
    "assumptions behind them (see assumption_flags) but never restate different "
    "numbers, and never invent data or review evidence. Argue both sides before "
    "concluding.\n\n"
    "A `buy` with fewer than two named risks, or any verdict without concrete "
    "verdict_changers, is incomplete and will be rejected. You may disagree with "
    "the composite score in either direction; when you do, set agrees_with_score "
    "false and say precisely which pillar it misjudges. Your differentiation_plan "
    "must address cited customer pain, not generic upgrades. `test` is a real "
    "recommendation (spend a little to learn a lot), not a hedge.\n\n"
    "Your recommendation does NOT set the Buy/Test/Avoid decision — deterministic "
    "scoring owns that; your role is the argument and the concurrence. Return ONLY "
    "one JSON object with keys: verdict (buy|test|avoid), conviction (1-5), "
    "agrees_with_score (bool), rationale (list of {point, evidence}), "
    "differentiation_plan, launch_shape (or null), risk_register (>=2 items of "
    "{risk, likelihood, impact, mitigation, evidence}), verdict_changers (>=2 "
    "items of {fact_that_would_flip, how_to_obtain_it}), assumption_challenges, "
    "and one_paragraph (<=120 words)."
)


# ---------------------------------------------------------------------------
# Context assembly (typed → compact named-metric JSON)
# ---------------------------------------------------------------------------
def _scored_brief(scored: ScoredOpportunity) -> dict[str, object]:
    return {
        "verdict_provisional": scored.verdict.value,
        "opportunity_score": round(scored.score, 1),
        "confidence": scored.confidence.level.value,
        "insufficient_data": scored.insufficient_data,
        "pillars": [
            {
                "pillar": p.pillar,
                "raw": None if p.raw_score is None else round(p.raw_score, 1),
                "normalized": None if p.capped_score is None else round(p.capped_score, 1),
                "weight": p.weight,
                "contribution": round(p.weighted_contribution, 1),
                "partial": p.partial,
                "available": p.available,
            }
            for p in scored.pillars
        ],
        "kills_triggered": [k.rule_id for k in scored.kills if k.kills],
        "kills_borderline": [
            k.rule_id for k in scored.kills if k.assessed and k.triggered and k.demoted
        ],
        "gates": [
            {"gate": g.gate_id, "passed": g.passed, "actual": g.actual} for g in scored.gates
        ],
    }


def _profit_brief(inp: ScoringInput) -> dict[str, object] | None:
    if inp.profit is None:
        return None
    exp, stressed = inp.profit.expected, inp.profit.stressed
    return {
        "expected": {
            "net_margin": round(exp.net_margin, 3),
            "roi": round(exp.roi, 3),
            "payback_months": exp.payback_months,
        },
        "stressed": {
            "net_margin": round(stressed.net_margin, 3),
            "roi": round(stressed.roi, 3),
            "payback_months": stressed.payback_months,
            "launch_capital_usd": round(stressed.launch_capital_cents / 100, 0),
        },
        "assumption_flags": list(stressed.assumption_flags),
    }


def _risk_brief(inp: ScoringInput) -> dict[str, object] | None:
    if inp.risk is None:
        return None
    return {
        "risk_score": round(inp.risk.risk_score, 1),
        "flags": [
            {"risk_type": f.risk_type, "deduction": f.deduction, "evidence": f.evidence}
            for f in inp.risk.flags
            if f.deduction > 0
        ],
        "unassessed": list(inp.risk.unassessed),
    }


def _differentiation_brief(inp: ScoringInput) -> dict[str, object] | None:
    diff = inp.differentiation
    if diff is None:
        return None
    return {
        "pillar_score": round(diff.pillar_score, 1),
        "confidence": diff.confidence.level.value,
        "sample_size": diff.confidence.sample_size,
        "sample_bias_flag": diff.sample_bias_flag,
        # RECOMPUTED numbers (from differentiation.py), never the model's claims.
        "themes": [
            {
                "label": t.label,
                "frequency_pct": round(t.frequency * 100, 1),
                "severity": t.severity,
                "addressability": t.addressability.value,
                "counted": t.counted,
            }
            for t in diff.themes
        ],
        "feature_gap_count": diff.feature_gap_count,
    }


def _analyst_brief(analyst_report: AnalystReport | None) -> dict[str, object] | None:
    """Compact, INTERPRETIVE Analyst context — market read + observed feature
    matrix. The confirmed feature gaps that actually move the score are in
    `differentiation_recomputed`; this is the competitive narrative only, never a
    number the Strategist may restate."""
    if analyst_report is None:
        return None
    return {
        "market_structure": analyst_report.market_structure.type,
        "attractiveness": analyst_report.attractiveness.rating,
        "openings": [o.description for o in analyst_report.openings],
        "concerns": [c.description for c in analyst_report.concerns],
        "feature_matrix": [
            {"asin": e.asin, "claimed_features": list(e.claimed_features)}
            for e in analyst_report.feature_matrix
        ],
        "data_gaps_acknowledged": list(analyst_report.data_gaps_acknowledged),
    }


def build_strategist_context(
    scored: ScoredOpportunity,
    inp: ScoringInput,
    miner_report: MinerReport | None,
    config: DeliumConfig,
    *,
    cross_market: list[dict[str, object]] | None = None,
    analyst_report: AnalystReport | None = None,
) -> str:
    """Assemble the Strategist prompt from typed deterministic objects + the
    validated Miner/Analyst evidence. The agent numbers are advisory; the
    differentiation block carries the CORRECTED frequencies (and Analyst-confirmed
    feature gaps) the engine recomputed."""
    context: dict[str, object] = {
        "scored": _scored_brief(scored),
        "profit": _profit_brief(inp),
        "risk_ledger": _risk_brief(inp),
        "differentiation_recomputed": _differentiation_brief(inp),
        "miner_report": None if miner_report is None else miner_report.model_dump(),
        "analyst_report": _analyst_brief(analyst_report),
        "preferences": {
            "min_price": config.preferences.min_price,
            "max_price": config.preferences.max_price,
            "avoid": list(config.preferences.avoid),
            "max_launch_budget": config.capital.max_launch_budget,
            "gates": {
                "min_margin": config.gates.min_margin,
                "min_roi": config.gates.min_roi,
                "max_payback_months": config.gates.max_payback_months,
            },
        },
        "cross_market_signals": cross_market or [],
    }
    return "Deterministic evidence package (numbers are final):\n" + json.dumps(
        context, ensure_ascii=False, default=str
    )


# ---------------------------------------------------------------------------
# Run + concurrence
# ---------------------------------------------------------------------------
def run_strategist(
    client: LlmClient,
    *,
    scored: ScoredOpportunity,
    inp: ScoringInput,
    miner_report: MinerReport | None,
    config: DeliumConfig,
    cross_market: list[dict[str, object]] | None = None,
    analyst_report: AnalystReport | None = None,
) -> AgentResult[StrategistVerdict]:
    """Run the frontier-tier Strategist and return its validated verdict."""
    user = build_strategist_context(
        scored,
        inp,
        miner_report,
        config,
        cross_market=cross_market,
        analyst_report=analyst_report,
    )
    return run_structured(
        client,
        tier=Tier.FRONTIER,
        system=STRATEGIST_SYSTEM,
        user=user,
        schema=StrategistVerdict,
        config=config.agents,
    )


def derive_concurrence(result: AgentResult[StrategistVerdict]) -> StrategistConcurrence:
    """Map a Strategist outcome to the G5 gate input. A failed/degraded-out run is
    UNAVAILABLE (no Buy without a review); a `buy` verdict is CONCUR; anything else
    is DISSENT. The Strategist can only ever BLOCK a would-be Buy this way."""
    if not result.ok or result.output is None:
        return StrategistConcurrence.UNAVAILABLE
    if result.output.verdict == "buy":
        return StrategistConcurrence.CONCUR
    return StrategistConcurrence.DISSENT
