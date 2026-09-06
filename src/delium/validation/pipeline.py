"""Deterministic validation orchestration (ARCHITECTURE.md §4.2, docs/data-layer
§3.1). Turns a candidate (explicit ASIN, Amazon URL, keyword, or a persisted
discovery candidate) into a fully-scored, persisted opportunity.

Funnel (hard-kill-first, so the expensive review + LLM spend is never wasted):
  resolve target → target Keepa (cheap) → cheap hard kills → stop if killed
  → cluster + competitors (Keepa, cheap) → competitor-aware hard kills → stop if killed
  → reviews (target + top-3) → Review Miner (LLM) → persist structured evidence
  → differentiation (recomputed) → scoring.py (provisional) → Strategist (LLM) → G5
  → scoring.py (final) → persist validation + agent runs + upgrade candidate

Boundaries this module keeps:
- scoring.py is the SOLE owner of the Buy/Test/Avoid verdict; nothing here
  re-implements a score, kill, or gate. The Strategist only supplies the G5
  concurrence, which scoring.py evaluates and which can only block a would-be Buy.
- providers/LLM are reached only through ingestion/the agent layer; the LLM never
  runs on a hard-killed candidate (those return above the agent phase) and never
  runs when its required inputs are missing.
- LLM output is validated, evidence-resolved, and its numbers recomputed
  deterministically — a Miner/Strategist failure degrades to the deterministic
  result and never fabricates evidence or raises confidence.
- no randomness, no wall-clock feeds a scoring decision (`as_of` is data-derived).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel

from delium.agents.analyst import persist_analyst_output, run_analyst
from delium.agents.miner import persist_miner_output, run_review_miner
from delium.agents.runner import AgentResult
from delium.agents.schemas import AnalystReport, MinerReport, StrategistVerdict
from delium.agents.strategist import derive_concurrence, run_strategist
from delium.analysis.models import (
    DifferentiationReport,
    Marketplace,
    ScoredOpportunity,
    ScoringInput,
    StrategistConcurrence,
)
from delium.analysis.scoring import score_opportunity
from delium.config.models import DeliumConfig
from delium.database import repository
from delium.discovery.assembly import AssemblyProvenance, ProfitOverrides, build_scoring_input
from delium.discovery.models import DiscoveryEvidence, DiscoverySource
from delium.utils.logging import get_logger
from delium.validation.evidence import build_competitor_listing_quality, build_differentiation
from delium.validation.hydration import (
    Clients,
    ensure_product,
    hydrate_cluster,
    hydrate_reviews,
    resolve_target,
)
from delium.validation.models import (
    AgentRunInfo,
    HydrationOutcome,
    ReviewEvidence,
    ValidationReport,
    ValidationRequest,
    ValidationStatus,
)

log = get_logger(__name__)

_REVIEW_COMPETITORS = 3  # target + top-3 competitors (~400 reviews; data-layer §3.1)
_ANALYST_COMPETITORS = 10  # feature matrix over the top-10 organic (analysis-engine §F2)


@dataclass(frozen=True)
class _AgentsOutcome:
    """Result of the LLM agent phase — validated outputs, audit records, and cost.
    scoring.py still owns the verdict; `concurrence` is only the G5 gate input."""

    miner_report: MinerReport | None = None
    analyst_report: AnalystReport | None = None
    strategist_verdict: StrategistVerdict | None = None
    concurrence: StrategistConcurrence = StrategistConcurrence.PENDING
    runs: tuple[AgentRunInfo, ...] = ()
    llm_cost_usd: float = 0.0
    miner_ran: bool = False


def run_validation(
    conn: sqlite3.Connection,
    request: ValidationRequest,
    config: DeliumConfig,
    clients: Clients | None = None,
) -> ValidationReport:
    """Run the full deterministic validation and return a self-contained report.

    Reads happen on `conn`; hydration writes go through ingestion's own
    connections, and the only writes on `conn` (persistence) happen last — so a
    single writer never contends with hydration (SQLite is single-writer)."""
    clients = clients or Clients()
    mp = request.marketplace
    budget = config.budgets.max_data_usd_per_validate
    cost = 0.0
    notes: list[str] = []

    def _report(
        status: ValidationStatus,
        *,
        asin: str | None = None,
        scored: ScoredOpportunity | None = None,
        review_evidence: ReviewEvidence | None = None,
        provenance: AssemblyProvenance | None = None,
        hydration: HydrationOutcome | None = None,
        from_candidate: bool = False,
        discovery_evidence: tuple[DiscoveryEvidence, ...] = (),
        agents: _AgentsOutcome | None = None,
        differentiation: DifferentiationReport | None = None,
    ) -> ValidationReport:
        agents = agents or _AgentsOutcome()
        return ValidationReport(
            request=request,
            asin=asin,
            marketplace=mp,
            status=status,
            scored=scored,
            review_evidence=review_evidence,
            provenance=provenance or AssemblyProvenance(),
            hydration=hydration or HydrationOutcome(data_cost_usd=cost, notes=tuple(notes)),
            from_candidate=from_candidate,
            discovery_evidence=discovery_evidence,
            miner_report=agents.miner_report,
            analyst_report=agents.analyst_report,
            strategist_verdict=agents.strategist_verdict,
            differentiation=differentiation,
            agent_runs=agents.runs,
            notes=tuple(notes),
        )

    # 1. Resolve target ASIN (ASIN | Amazon URL | keyword → SERP top organic).
    resolution = resolve_target(
        conn, request.target, mp, config, request.run_id, clients, force=request.force
    )
    cost += resolution.cost_usd
    if resolution.error == "invalid":
        notes.append("could not parse an ASIN, Amazon URL, or keyword from the target")
        return _report(ValidationStatus.INVALID_TARGET)
    if resolution.asin is None:
        notes.append("could not resolve a target ASIN from the keyword's SERP")
        return _report(ValidationStatus.INSUFFICIENT_DATA)
    asin = resolution.asin

    # Preserve discovery provenance: is this an already-known candidate?
    candidate_row = repository.get_candidate(conn, asin, mp.value)
    discovery_evidence = _discovery_evidence(candidate_row)
    from_candidate = candidate_row is not None

    # 2. Ensure the target product (cheap, cache-first).
    product = ensure_product(conn, asin, mp, config, request.run_id, clients, force=request.force)
    cost += product.cost_usd
    if not product.found:
        status = {
            "not_found": ValidationStatus.PRODUCT_NOT_FOUND,
            "provider_error": ValidationStatus.PROVIDER_ERROR,
            "no_provider_no_cache": ValidationStatus.MISSING_CREDENTIALS,
        }.get(product.error or "", ValidationStatus.INSUFFICIENT_DATA)
        notes.append(f"target product unavailable ({product.error})")
        return _report(
            status, asin=asin, from_candidate=from_candidate, discovery_evidence=discovery_evidence
        )

    overrides = _profit_overrides(request)

    # 3. Cheap hard-kill gate (target-only facts) — stop before ANY enrichment.
    cheap_input, _ = build_scoring_input(conn, asin, mp, config, cheap_only=True)
    if cheap_input is None:
        return _report(
            ValidationStatus.INSUFFICIENT_DATA,
            asin=asin,
            from_candidate=from_candidate,
            discovery_evidence=discovery_evidence,
        )
    cheap_scored = score_opportunity(cheap_input, config)
    if cheap_scored.hard_kill_triggered:
        _persist(conn, request, asin, cheap_scored, candidate_row)
        notes.append("eliminated by a cheap hard kill before enrichment")
        return _report(
            ValidationStatus.HARD_KILLED,
            asin=asin,
            scored=cheap_scored,
            from_candidate=from_candidate,
            discovery_evidence=discovery_evidence,
        )

    # 4. Cluster + competitor hydration (Keepa/DataForSEO, cache-first, cheap).
    cluster = hydrate_cluster(
        conn, asin, resolution.seed, mp, config, request.run_id, clients, force=request.force
    )
    cost += cluster.cost_usd
    notes.extend(cluster.notes)

    listing_quality = _competitor_listing_quality(conn, asin, resolution.seed, mp, config)

    # 4b. Competitor-aware hard-kill gate — still before the review spend.
    pre_input, _ = build_scoring_input(
        conn, asin, mp, config, listing_quality=listing_quality, profit_overrides=overrides
    )
    if pre_input is not None:
        pre_scored = score_opportunity(pre_input, config)
        if pre_scored.hard_kill_triggered:
            _persist(conn, request, asin, pre_scored, candidate_row)
            notes.append("eliminated by a hard kill after competitor hydration (no reviews spent)")
            return _report(
                ValidationStatus.HARD_KILLED,
                asin=asin,
                scored=pre_scored,
                hydration=HydrationOutcome(
                    data_cost_usd=cost,
                    product_from_cache=product.from_cache,
                    competitors_hydrated=cluster.count,
                    notes=tuple(notes),
                ),
                from_candidate=from_candidate,
                discovery_evidence=discovery_evidence,
            )

    # 5. Reviews — the validate-tier spend — only for survivors.
    competitor_review_asins = _top_competitors(conn, asin, resolution.seed, mp, _REVIEW_COMPETITORS)
    review_asins = [asin, *competitor_review_asins]
    reviews = hydrate_reviews(
        review_asins,
        config,
        request.run_id,
        clients,
        budget_usd=max(0.0, budget - cost),
        force=request.force,
    )
    cost += reviews.cost_usd
    notes.extend(reviews.notes)

    # 6. Review Miner (LLM) — persists structured evidence the engine recomputes.
    agents = _run_miner(conn, request, config, clients, asin, competitor_review_asins, notes)

    # 6b. Analyst (LLM) — persists the competitor feature matrix (top-10 listings)
    #     the differentiation engine reads to confirm F2 gaps / F4 bundle openings.
    analyst_competitor_asins = _top_competitors(
        conn, asin, resolution.seed, mp, _ANALYST_COMPETITORS
    )
    agents = _run_analyst(
        conn, request, config, clients, asin, analyst_competitor_asins, agents, notes
    )

    # 7. Differentiation from the real review sample + (Miner-persisted) evidence.
    #    Competitor absence is derived from the Analyst matrix, coverage-gated.
    differentiation, review_evidence = build_differentiation(
        conn,
        asin,
        config,
        miner_ran=agents.miner_ran,
        competitor_asins=analyst_competitor_asins,
        marketplace=mp.value,
    )

    # 8. Full ScoringInput → scoring.py (provisional, G5 pending).
    full_input, provenance = build_scoring_input(
        conn,
        asin,
        mp,
        config,
        differentiation=differentiation,
        listing_quality=listing_quality,
        profit_overrides=overrides,
    )
    if full_input is None:
        return _report(
            ValidationStatus.INSUFFICIENT_DATA,
            asin=asin,
            from_candidate=from_candidate,
            discovery_evidence=discovery_evidence,
        )
    provisional = score_opportunity(full_input, config)

    # 9. Strategist (LLM, frontier) → G5 concurrence → FINAL deterministic score.
    agents = _run_strategist(
        conn,
        request,
        config,
        clients,
        asin,
        full_input,
        provisional,
        agents,
        discovery_evidence,
        notes,
    )
    scored = score_opportunity(full_input, config, strategist=agents.concurrence)

    # 10. Persist last (single writer): validation snapshot + upgraded candidate.
    _persist(conn, request, asin, scored, candidate_row)

    hydration = HydrationOutcome(
        data_cost_usd=cost,
        llm_cost_usd=agents.llm_cost_usd,
        product_from_cache=product.from_cache,
        reviews_from_cache=reviews.from_cache,
        review_provider=reviews.provider,
        competitors_hydrated=cluster.count,
        reviews_fetched=reviews.count,
        degraded=reviews.degraded or any(r.status == "failed" for r in agents.runs),
        notes=tuple(notes),
    )
    return _report(
        ValidationStatus.SCORED,
        asin=asin,
        scored=scored,
        review_evidence=review_evidence,
        provenance=provenance,
        hydration=hydration,
        from_candidate=from_candidate,
        discovery_evidence=discovery_evidence,
        agents=agents,
        differentiation=differentiation,
    )


# ---------------------------------------------------------------------------
# Agent phase (LLM) — never runs on hard-killed candidates (they returned above)
# ---------------------------------------------------------------------------
def _run_miner(
    conn: sqlite3.Connection,
    request: ValidationRequest,
    config: DeliumConfig,
    clients: Clients,
    asin: str,
    competitor_asins: list[str],
    notes: list[str],
) -> _AgentsOutcome:
    """Run the Review Miner when enabled + affordable + there are reviews. Persists
    its validated evidence (replacing stale) and an audit row. On any failure the
    deterministic pipeline continues on whatever evidence is already persisted —
    no fabrication, and a missing Miner never raises confidence."""
    ac = config.agents
    if clients.llm is None or not ac.enabled or not ac.review_miner_enabled:
        return _AgentsOutcome()
    if not repository.get_reviews_for_asin(conn, asin):
        return _AgentsOutcome()  # nothing to mine → differentiation stays evidence-thin
    if config.budgets.max_llm_usd_per_validate <= 0:
        notes.append("review miner skipped — LLM budget is zero")
        return _AgentsOutcome()

    miner_run = run_review_miner(
        conn,
        clients.llm,
        target_asin=asin,
        competitor_asins=competitor_asins,
        config=ac,
        listing_rating_avg=_target_rating(conn, asin),
    )
    result = miner_run.result
    info = _agent_run_info("review_miner", result)
    _persist_agent_run(conn, request, asin, "review_miner", result)

    miner_report: MinerReport | None = None
    miner_ran = False
    if result.ok and result.output is not None:
        persist_miner_output(conn, run_id=request.run_id, asin=asin, report=result.output)
        miner_report = result.output
        miner_ran = True
        if result.status == "degraded":
            notes.append(f"review miner degraded — {result.dropped}/{result.total} items dropped")
    else:
        notes.append(f"review miner unavailable — {result.error}")

    return _AgentsOutcome(
        miner_report=miner_report,
        runs=(info,),
        llm_cost_usd=result.cost_usd,
        miner_ran=miner_ran,
    )


def _run_analyst(
    conn: sqlite3.Connection,
    request: ValidationRequest,
    config: DeliumConfig,
    clients: Clients,
    asin: str,
    competitor_asins: list[str],
    agents: _AgentsOutcome,
    notes: list[str],
) -> _AgentsOutcome:
    """Run the Analyst when enabled + affordable + there are competitors to read.
    Persists the competitor feature matrix (replacing stale) and an audit row. On
    any failure the pipeline continues: `absent_from_competitors` / bundle openings
    simply stay UNKNOWN (never assumed present or absent), and a missing Analyst
    never raises confidence or manufactures a competitor deficiency."""
    ac = config.agents
    if clients.llm is None or not ac.enabled or not ac.analyst_enabled:
        return agents
    if not competitor_asins:
        return agents  # no competitor listings → matrix stays empty, gaps stay UNKNOWN
    remaining = config.budgets.max_llm_usd_per_validate - agents.llm_cost_usd
    if remaining <= 0:
        notes.append("analyst skipped — LLM budget exhausted; competitor gaps stay unknown")
        return agents

    analyst_run = run_analyst(
        conn,
        clients.llm,
        target_asin=asin,
        competitor_asins=competitor_asins,
        config=ac,
    )
    result = analyst_run.result
    info = _agent_run_info("analyst", result)
    _persist_agent_run(conn, request, asin, "analyst", result)

    analyst_report: AnalystReport | None = None
    if result.ok and result.output is not None:
        persist_analyst_output(
            conn,
            run_id=request.run_id,
            target_asin=asin,
            competitor_asins=analyst_run.competitor_asins,
            report=result.output,
        )
        analyst_report = result.output
        if result.status == "degraded":
            notes.append(f"analyst degraded — {result.dropped}/{result.total} features dropped")
    else:
        notes.append(f"analyst unavailable — {result.error}; competitor gaps stay unknown")

    return _AgentsOutcome(
        miner_report=agents.miner_report,
        analyst_report=analyst_report,
        strategist_verdict=agents.strategist_verdict,
        concurrence=agents.concurrence,
        runs=(*agents.runs, info),
        llm_cost_usd=agents.llm_cost_usd + result.cost_usd,
        miner_ran=agents.miner_ran,
    )


def _run_strategist(
    conn: sqlite3.Connection,
    request: ValidationRequest,
    config: DeliumConfig,
    clients: Clients,
    asin: str,
    inp: ScoringInput,
    provisional: ScoredOpportunity,
    agents: _AgentsOutcome,
    discovery_evidence: tuple[DiscoveryEvidence, ...],
    notes: list[str],
) -> _AgentsOutcome:
    """Run the Strategist and derive the G5 concurrence. It never sets the verdict:
    concurrence can only block a would-be Buy (scoring.py applies the gate).
    Agents-off leaves PENDING (provisional Buy allowed); an enabled-but-unavailable
    Strategist yields UNAVAILABLE (no Buy without a review)."""
    ac = config.agents
    if clients.llm is None or not ac.enabled or not ac.strategist_enabled:
        return agents  # PENDING — the agents-off baseline
    remaining = config.budgets.max_llm_usd_per_validate - agents.llm_cost_usd
    if remaining <= 0:
        notes.append("strategist skipped — LLM budget exhausted; a Buy cannot be confirmed")
        return _replace_concurrence(agents, StrategistConcurrence.UNAVAILABLE)

    result = run_strategist(
        clients.llm,
        scored=provisional,
        inp=inp,
        miner_report=agents.miner_report,
        config=config,
        cross_market=_cross_market_signals(discovery_evidence),
        analyst_report=agents.analyst_report,
    )
    info = _agent_run_info("strategist", result)
    _persist_agent_run(conn, request, asin, "strategist", result)
    concurrence = derive_concurrence(result)
    if not result.ok:
        notes.append(f"strategist unavailable — {result.error}; a Buy cannot be confirmed")

    return _AgentsOutcome(
        miner_report=agents.miner_report,
        analyst_report=agents.analyst_report,
        strategist_verdict=result.output,
        concurrence=concurrence,
        runs=(*agents.runs, info),
        llm_cost_usd=agents.llm_cost_usd + result.cost_usd,
        miner_ran=agents.miner_ran,
    )


def _replace_concurrence(
    agents: _AgentsOutcome, concurrence: StrategistConcurrence
) -> _AgentsOutcome:
    return _AgentsOutcome(
        miner_report=agents.miner_report,
        analyst_report=agents.analyst_report,
        strategist_verdict=agents.strategist_verdict,
        concurrence=concurrence,
        runs=agents.runs,
        llm_cost_usd=agents.llm_cost_usd,
        miner_ran=agents.miner_ran,
    )


def _agent_run_info[R: BaseModel](agent: str, result: AgentResult[R]) -> AgentRunInfo:
    return AgentRunInfo(
        agent=agent,
        status=result.status,
        model=result.model,
        provider=result.provider,
        cost_usd=result.cost_usd,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        dropped=result.dropped,
        total=result.total,
        error=result.error,
    )


def _persist_agent_run[R: BaseModel](
    conn: sqlite3.Connection,
    request: ValidationRequest,
    asin: str,
    agent: str,
    result: AgentResult[R],
) -> None:
    """Persist the audit + reproducibility row for one agent invocation, including
    the VALIDATED structured output (never raw model text)."""
    output = result.output.model_dump() if result.output is not None else None
    repository.insert_agent_run(
        conn,
        run_id=request.run_id,
        asin=asin,
        marketplace=request.marketplace.value,
        agent=agent,
        status=result.status,
        model=result.model,
        provider=result.provider,
        cost_usd=result.cost_usd,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        output=output,
        error=result.error,
    )


def _cross_market_signals(
    discovery_evidence: tuple[DiscoveryEvidence, ...],
) -> list[dict[str, object]]:
    """Compact cross-market context for the Strategist — a discovery signal only,
    never a scoring input."""
    return [
        {
            "source": e.source.value,
            "reference": e.reference,
            "cross_market_score": e.cross_market_score,
        }
        for e in discovery_evidence
        if e.cross_market_score is not None
    ]


def _target_rating(conn: sqlite3.Connection, asin: str) -> float | None:
    for row in reversed(repository.get_price_bsr_history(conn, asin)):
        if row["rating"] is not None:
            return float(row["rating"])
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _profit_overrides(request: ValidationRequest) -> ProfitOverrides:
    from delium.analysis.models import Dimensions

    return ProfitOverrides(
        cogs_cents=None if request.cogs_usd is None else round(request.cogs_usd * 100),
        freight_cents=None if request.freight_usd is None else round(request.freight_usd * 100),
        dims=None if request.dims_mm is None else Dimensions(*request.dims_mm),
        weight_g=request.weight_g,
    )


def _competitor_listing_quality(
    conn: sqlite3.Connection,
    asin: str,
    seed: str | None,
    marketplace: Marketplace,
    config: DeliumConfig,
) -> dict[str, float]:
    competitor_asins = _competitor_asins(conn, asin, seed, marketplace, config.discovery.serp_depth)
    if not competitor_asins:
        return {}
    return build_competitor_listing_quality(conn, competitor_asins, marketplace.value)


def _competitor_asins(
    conn: sqlite3.Connection, asin: str, seed: str | None, marketplace: Marketplace, cap: int
) -> list[str]:
    if seed is None:
        phrases = repository.get_serp_keyword_phrases(conn, asin, marketplace.value)
        seed = phrases[0] if phrases else None
    if seed is None:
        return []
    rows = repository.get_serp_rankings(conn, seed, marketplace.value)
    return [r["asin"] for r in rows[:cap]]


def _top_competitors(
    conn: sqlite3.Connection, asin: str, seed: str | None, marketplace: Marketplace, n: int
) -> list[str]:
    """Top-N organic competitor ASINs (excluding the target) for review sampling."""
    if seed is None:
        phrases = repository.get_serp_keyword_phrases(conn, asin, marketplace.value)
        seed = phrases[0] if phrases else None
    if seed is None:
        return []
    out: list[str] = []
    for row in repository.get_serp_rankings(conn, seed, marketplace.value):
        if row["sponsored"] or row["asin"] == asin:
            continue
        out.append(row["asin"])
        if len(out) >= n:
            break
    return out


def _discovery_evidence(candidate_row: sqlite3.Row | None) -> tuple[DiscoveryEvidence, ...]:
    """Surface a persisted candidate's discovery provenance (including any
    cross-market signal) — read-only context, never a scoring input."""
    if candidate_row is None or not candidate_row["evidence"]:
        return ()
    try:
        parsed = json.loads(candidate_row["evidence"])
    except (ValueError, TypeError):
        return ()
    out: list[DiscoveryEvidence] = []
    for e in parsed if isinstance(parsed, list) else ():
        if not isinstance(e, dict):
            continue
        try:
            source = DiscoverySource(e.get("source", ""))
        except ValueError:
            continue
        out.append(
            DiscoveryEvidence(
                source=source,
                reference=str(e.get("reference", "")),
                detail=str(e.get("detail", "")),
                serp_position=e.get("serp_position"),
                cross_market_score=e.get("cross_market_score"),
            )
        )
    return tuple(out)


def _load_evidence(raw: str | None) -> object | None:
    """Parse a candidate's persisted evidence JSON, tolerating a corrupt column
    (malformed persisted data must never crash a re-validation — docs §17)."""
    if not raw:
        return None
    try:
        parsed: object = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed


def _candidate_status(scored: ScoredOpportunity) -> str:
    """Upgrade the research-queue status from a validation outcome (candidates
    CHECK: new|shortlist|validated|rejected|watching)."""
    if scored.hard_kill_triggered or scored.bad_opportunity:
        return "rejected"
    return "validated"


def _persist(
    conn: sqlite3.Connection,
    request: ValidationRequest,
    asin: str,
    scored: ScoredOpportunity,
    candidate_row: sqlite3.Row | None,
) -> None:
    """Persist the validation snapshot and upgrade the candidate record. The
    existing discovery provenance is preserved (an upgrade, not a new record);
    a never-discovered ASIN gets a minimal explicit candidate."""
    repository.upsert_validation(
        conn,
        run_id=request.run_id,
        asin=asin,
        marketplace=request.marketplace.value,
        opportunity_score=scored.score,
        verdict=scored.verdict.value,
        confidence=scored.confidence.level.value,
        insufficient_data=scored.insufficient_data,
        scored=validation_snapshot(scored),
    )
    if candidate_row is not None:
        source = candidate_row["source"]
        source_ref = candidate_row["source_ref"]
        evidence = _load_evidence(candidate_row["evidence"])  # tolerates corrupt JSON
        source_run_id = candidate_row["source_run_id"]
    else:
        source = DiscoverySource.EXPLICIT.value
        source_ref = "validate"
        evidence = [
            {
                "source": DiscoverySource.EXPLICIT.value,
                "reference": "validate",
                "detail": "validate command",
                "serp_position": None,
                "cross_market_score": None,
            }
        ]
        source_run_id = request.run_id
    repository.upsert_candidate(
        conn,
        asin=asin,
        marketplace=request.marketplace.value,
        source=source,
        source_ref=source_ref,
        evidence=evidence,
        source_run_id=source_run_id,
        triage_score=scored.score,
        verdict=scored.verdict.value,
        status=_candidate_status(scored),
    )


def validation_snapshot(scored: ScoredOpportunity) -> dict[str, object]:
    """Complete, reproducible JSON snapshot of the ScoredOpportunity — every kill,
    gate, pillar (raw + capped + weighted), confidence, verdict basis, and the
    config snapshot that decided it (docs §9/§11). Enough to regenerate the
    report and re-derive the verdict without re-running the engines."""
    cs = scored.config_snapshot
    return {
        "verdict": scored.verdict.value,
        "score": scored.score,
        "base_weighted_score": scored.base_weighted_score,
        "confidence": scored.confidence.level.value,
        "insufficient_data": scored.insufficient_data,
        "strategist_pending": scored.strategist_pending,  # G5 always pending here
        "verdict_basis": list(scored.verdict_basis),
        "pillars": [
            {
                "pillar": p.pillar,
                "raw_score": p.raw_score,
                "capped_score": p.capped_score,
                "weight": p.weight,
                "weighted_contribution": p.weighted_contribution,
                "confidence": p.confidence.value,
                "available": p.available,
                "partial": p.partial,
                "cap_reason": p.cap_reason,
                "source": p.source,
            }
            for p in scored.pillars
        ],
        "kills": [
            {
                "rule_id": k.rule_id,
                "name": k.name,
                "triggered": k.triggered,
                "demoted": k.demoted,
                "kills": k.kills,
                "assessed": k.assessed,
                "actual": k.actual,
                "threshold": k.threshold,
            }
            for k in scored.kills
        ],
        "gates": [
            {
                "gate_id": g.gate_id,
                "name": g.name,
                "passed": g.passed,  # G5 = null (pending)
                "hard": g.hard,
                "actual": g.actual,
                "threshold": g.threshold,
            }
            for g in scored.gates
        ],
        "config_snapshot": {
            "weights": [list(x) for x in cs.weights],
            "gate_thresholds": [list(x) for x in cs.gate_thresholds],
            "kill_thresholds": [list(x) for x in cs.kill_thresholds],
            "verdict_thresholds": [list(x) for x in cs.verdict_thresholds],
            "sufficiency": [list(x) for x in cs.sufficiency],
            "profit_pillar": [list(x) for x in cs.profit_pillar],
        },
    }
