"""Deterministic discovery / scout orchestration (ARCHITECTURE.md §4.1).

Turns discovery sources into a ranked research queue by orchestrating the
existing deterministic components — sources → dedup → cheap hydration → hard
kills (scoring.py) → enrichment → full scoring (scoring.py) → ranking → persist.

Boundaries this module keeps:
- scoring.py is the SOLE owner of the opportunity verdict; nothing here
  re-implements a score, kill, or gate.
- ranking never mutates a score — it only orders.
- providers are reached only through ingestion (cache-first), never directly.
- no LLM, no randomness, no wall-clock for scoring decisions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from delium.analysis.models import (
    Confidence,
    Marketplace,
    ScoredOpportunity,
    SourceMaturity,
    Verdict,
)
from delium.analysis.scoring import score_opportunity
from delium.config.models import DeliumConfig
from delium.database import repository
from delium.discovery.assembly import AssemblyProvenance, build_scoring_input
from delium.discovery.models import (
    Candidate,
    CandidateOutcome,
    DiscoveryEvidence,
    DiscoveryReport,
    DiscoverySource,
    EvaluatedCandidate,
)
from delium.utils.logging import get_logger

log = get_logger(__name__)

_CONF_RANK = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}

# Optional hydration hooks: marketplace code → provider client. Kept generic so
# the CLI can inject real clients and tests can inject fakes (or omit them for
# persisted-only, fully deterministic runs).
KeepaFactory = Callable[[str], object]
DfsFactory = Callable[[str], object]


# ---------------------------------------------------------------------------
# Discovery sources (read persisted, marketplace-scoped data)
# ---------------------------------------------------------------------------
def candidates_from_keyword(
    conn: sqlite3.Connection, seed: str, marketplace: Marketplace, *, cap: int
) -> list[Candidate]:
    """SERP top-N ASINs for a seed keyword in one marketplace (ARCHITECTURE
    §4.1). Preserves the seed + SERP position as provenance."""
    from delium.providers.dataforseo import normalize_phrase

    phrase = normalize_phrase(seed)
    rows = repository.get_serp_rankings(conn, phrase, marketplace.value)
    out: list[Candidate] = []
    for row in rows[:cap]:
        evidence = DiscoveryEvidence(
            source=DiscoverySource.KEYWORD,
            reference=phrase,
            detail=f"serp:{marketplace.value}",
            serp_position=int(row["position"]),
        )
        out.append(Candidate(asin=row["asin"], marketplace=marketplace, evidence=(evidence,)))
    return out


def candidates_from_explicit(asins: list[str], marketplace: Marketplace) -> list[Candidate]:
    """User-supplied ASINs — validate known products through the full pipeline."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for asin in asins:
        clean = asin.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        evidence = DiscoveryEvidence(
            source=DiscoverySource.EXPLICIT, reference="user", detail="explicit"
        )
        out.append(Candidate(asin=clean, marketplace=marketplace, evidence=(evidence,)))
    return out


def candidates_from_cross_market(
    conn: sqlite3.Connection,
    *,
    source_mp: Marketplace,
    target_mps: tuple[Marketplace, ...],
    config: DeliumConfig,
    run_id: str,
) -> list[Candidate]:
    """Products proven in `source_mp` that read as opportunities in a target
    marketplace become candidates THERE (docs/cross-market.md). Reuses the
    existing cross-market pipeline; keeps source/target + score provenance."""
    from delium.analysis.models import CrossMarketVerdict
    from delium.ingestion.cross_market import discover_cross_market

    dcfg = config.discovery
    maturity = SourceMaturity(dcfg.cross_market_min_source_maturity)
    results = discover_cross_market(
        conn,
        source_mp=source_mp,
        target_mps=target_mps,
        config=config,
        run_id=run_id,
        min_source_maturity=maturity,
        min_monthly_units=dcfg.cross_market_min_source_units or None,
        # Discovery keeps `conn` write-free during gathering so later hydration
        # (ingestion's own connections) never contends; the dedicated
        # `cross-market` command persists product_matches.
        persist_matches=False,
    )
    out: list[Candidate] = []
    for cand in results:
        report = cand.report
        # Only genuine opportunities become candidates — mature/weak/insufficient
        # cross-market results are not research-queue material.
        if report.verdict not in (
            CrossMarketVerdict.STRONG_OPPORTUNITY,
            CrossMarketVerdict.OPPORTUNITY_TO_VALIDATE,
        ):
            continue
        # The target product may be a real target listing or a projection of the
        # source identity (same ASIN) when the product is not yet present there.
        evidence = DiscoveryEvidence(
            source=DiscoverySource.CROSS_MARKET,
            reference=source_mp.value,
            detail=f"{source_mp.value}->{cand.target_marketplace.value}:{cand.source_asin}",
            cross_market_score=report.score,
        )
        out.append(
            Candidate(
                asin=report.match.target.asin,
                marketplace=cand.target_marketplace,
                evidence=(evidence,),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Deduplication (identity = (asin, marketplace); evidence merged)
# ---------------------------------------------------------------------------
def deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse candidates sharing an identity into one with merged, ordered
    evidence. Deterministic and idempotent; input order of first appearance is
    preserved for stability."""
    order: list[tuple[str, str]] = []
    merged: dict[tuple[str, str], Candidate] = {}
    for cand in candidates:
        key = cand.identity
        if key in merged:
            merged[key] = merged[key].merged_with(cand)
        else:
            merged[key] = cand.with_sorted_evidence()
            order.append(key)
    return [merged[k] for k in order]


# ---------------------------------------------------------------------------
# Hydration (cache-first via ingestion; never calls providers directly)
# ---------------------------------------------------------------------------
def _candidate_seed(candidate: Candidate) -> str | None:
    for e in candidate.evidence:
        if e.source is DiscoverySource.KEYWORD:
            return e.reference
    return None


def _hydrate_cheap(
    candidate: Candidate, config: DeliumConfig, run_id: str, keepa_factory: KeepaFactory | None
) -> None:
    """Fetch only the candidate's own Keepa quick stats (cheap kill tier)."""
    if keepa_factory is None:
        return
    from delium.ingestion import fetch_product

    client = keepa_factory(candidate.marketplace.value)
    fetch_product(candidate.asin, run_id=run_id, client=client, config=config)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Ranking (deterministic; never mutates a score)
# ---------------------------------------------------------------------------
def _rank_key(ec: EvaluatedCandidate) -> tuple[float, int, str]:
    scored = ec.scored
    assert scored is not None
    # opportunity_score DESC → confidence DESC → ASIN ASC (stable tie-breaker).
    return (-scored.score, -_CONF_RANK[scored.confidence.level], ec.asin)


def rank(evaluated: list[EvaluatedCandidate]) -> list[EvaluatedCandidate]:
    """Deterministic ranking of scored candidates. Does not alter any score."""
    scored = [ec for ec in evaluated if ec.outcome is CandidateOutcome.SCORED]
    return sorted(scored, key=_rank_key)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _evaluate(
    conn: sqlite3.Connection,
    candidate: Candidate,
    config: DeliumConfig,
) -> tuple[EvaluatedCandidate, AssemblyProvenance]:
    """Cheap-kill-first evaluation of one candidate. Enrichment (full pillar
    assembly) happens only after the cheap kills pass — expensive analysis is
    never spent on a candidate a $0 check can eliminate."""
    mp = candidate.marketplace
    cheap_input, prov = build_scoring_input(conn, candidate.asin, mp, config, cheap_only=True)
    if cheap_input is None:
        return (
            EvaluatedCandidate(
                candidate=candidate,
                outcome=CandidateOutcome.UNRESOLVED,
                notes=("product not fetched in this marketplace",),
            ),
            prov,
        )

    cheap_scored = score_opportunity(cheap_input, config)
    if cheap_scored.hard_kill_triggered:
        kill = next(k for k in cheap_scored.kills if k.kills)
        return (
            EvaluatedCandidate(
                candidate=candidate,
                outcome=CandidateOutcome.KILLED,
                scored=cheap_scored,  # cheap-only: pillars deliberately unassembled
                kill_rule=kill.rule_id,
                notes=(kill.reason,),
            ),
            prov,
        )

    full_input, full_prov = build_scoring_input(conn, candidate.asin, mp, config, cheap_only=False)
    assert full_input is not None  # product row existed for the cheap pass
    scored = score_opportunity(full_input, config)
    return (
        EvaluatedCandidate(candidate=candidate, outcome=CandidateOutcome.SCORED, scored=scored),
        full_prov,
    )


def _gather(
    conn: sqlite3.Connection,
    *,
    marketplace: Marketplace,
    keywords: list[str],
    asins: list[str],
    cross_market_targets: tuple[Marketplace, ...],
    config: DeliumConfig,
    run_id: str,
) -> list[Candidate]:
    dcfg = config.discovery
    gathered: list[Candidate] = []
    for seed in keywords:
        gathered.extend(
            candidates_from_keyword(conn, seed, marketplace, cap=dcfg.max_candidates_per_seed)
        )
    if asins and dcfg.accept_explicit_asins:
        gathered.extend(candidates_from_explicit(asins, marketplace))
    if cross_market_targets and dcfg.cross_market_enabled:
        gathered.extend(
            candidates_from_cross_market(
                conn,
                source_mp=marketplace,
                target_mps=cross_market_targets,
                config=config,
                run_id=run_id,
            )
        )
    return gathered


def run_discovery(
    conn: sqlite3.Connection,
    *,
    marketplace: Marketplace,
    config: DeliumConfig,
    run_id: str,
    keywords: list[str] | None = None,
    asins: list[str] | None = None,
    cross_market_targets: tuple[Marketplace, ...] = (),
    keepa_factory: KeepaFactory | None = None,
    dfs_factory: DfsFactory | None = None,
    persist: bool = True,
) -> DiscoveryReport:
    """Run the full deterministic discovery funnel and return a ranked report.

    Operates on persisted data; when provider factories are supplied it hydrates
    cache-first (cheap tier for all candidates, full enrichment only for
    survivors of the hard kills). Every candidate/validation is written with the
    run's provenance.
    """
    keywords = keywords or []
    asins = asins or []

    gathered = _gather(
        conn,
        marketplace=marketplace,
        keywords=keywords,
        asins=asins,
        cross_market_targets=cross_market_targets,
        config=config,
        run_id=run_id,
    )
    discovered = deduplicate(gathered)[: config.discovery.max_candidates]

    # Evaluate with reads on `conn` + hydration via ingestion's own connections.
    # No writes on `conn` happen in this loop, so the inner hydration writes
    # never contend with an open outer transaction (SQLite single-writer).
    evaluated: list[EvaluatedCandidate] = []
    for cand in discovered:
        # Cheap hydration → cheap kills → (survivors only) full hydration.
        _hydrate_cheap(cand, config, run_id, keepa_factory)
        cheap_input, _ = build_scoring_input(
            conn, cand.asin, cand.marketplace, config, cheap_only=True
        )
        survives = (
            cheap_input is not None
            and not score_opportunity(cheap_input, config).hard_kill_triggered
        )
        if survives:
            _hydrate_full_serp(conn, cand, config, run_id, keepa_factory, dfs_factory)
        ec, _prov = _evaluate(conn, cand, config)
        evaluated.append(ec)

    ranked = rank(evaluated)[: config.discovery.max_ranked]
    killed = [e for e in evaluated if e.outcome is CandidateOutcome.KILLED]
    unresolved = [e for e in evaluated if e.outcome is CandidateOutcome.UNRESOLVED]

    # Persist last: a single writer on `conn`, after all hydration has finished.
    if persist:
        for ec in evaluated:
            _persist_evaluated(conn, run_id, ec)

    return DiscoveryReport(
        run_id=run_id,
        marketplace=marketplace,
        discovered=tuple(discovered),
        ranked=tuple(ranked),
        killed=tuple(killed),
        unresolved=tuple(unresolved),
    )


def _hydrate_full_serp(
    conn: sqlite3.Connection,
    candidate: Candidate,
    config: DeliumConfig,
    run_id: str,
    keepa_factory: KeepaFactory | None,
    dfs_factory: DfsFactory | None,
) -> None:
    """Full enrichment with a real connection (keyword cluster + competitors)."""
    if keepa_factory is None and dfs_factory is None:
        return
    from delium.ingestion import fetch_keywords, fetch_product

    seed = _candidate_seed(candidate)
    mp = candidate.marketplace.value
    if seed is not None and dfs_factory is not None:
        fetch_keywords(seed, run_id=run_id, client=dfs_factory(mp), config=config)  # type: ignore[arg-type]
    if keepa_factory is not None:
        client = keepa_factory(mp)
        if seed is not None:
            for row in repository.get_serp_rankings(conn, seed, mp):
                fetch_product(row["asin"], run_id=run_id, client=client, config=config)  # type: ignore[arg-type]


def _evidence_json(e: DiscoveryEvidence) -> dict[str, object]:
    return {
        "source": e.source.value,
        "reference": e.reference,
        "detail": e.detail,
        "serp_position": e.serp_position,
        "cross_market_score": e.cross_market_score,
    }


def _persist_evaluated(conn: sqlite3.Connection, run_id: str, ec: EvaluatedCandidate) -> None:
    """Persist one evaluated candidate: the candidate row (with verdict/status)
    and, when scored, its validation snapshot."""
    cand = ec.candidate
    scored = ec.scored
    repository.upsert_candidate(
        conn,
        asin=cand.asin,
        marketplace=cand.marketplace.value,
        source=cand.sources[0].value if cand.sources else DiscoverySource.KEYWORD.value,
        source_ref=cand.evidence[0].reference if cand.evidence else None,
        evidence=[_evidence_json(e) for e in cand.evidence],
        source_run_id=run_id,
        triage_score=scored.score if scored is not None else None,
        verdict=scored.verdict.value if scored is not None else None,
        status=_candidate_status(ec),
    )
    if scored is not None:
        repository.upsert_validation(
            conn,
            run_id=run_id,
            asin=ec.asin,
            marketplace=ec.marketplace.value,
            opportunity_score=scored.score,
            verdict=scored.verdict.value,
            confidence=scored.confidence.level.value,
            insufficient_data=scored.insufficient_data,
            scored=_scored_snapshot(scored),
        )


def _scored_snapshot(scored: ScoredOpportunity) -> dict[str, object]:
    """Compact, reproducible snapshot of the ScoredOpportunity for persistence."""
    return {
        "verdict": scored.verdict.value,
        "score": scored.score,
        "confidence": scored.confidence.level.value,
        "insufficient_data": scored.insufficient_data,
        "strategist_pending": scored.strategist_pending,
        "pillars": [
            {
                "pillar": p.pillar,
                "raw_score": p.raw_score,
                "capped_score": p.capped_score,
                "weight": p.weight,
                "weighted_contribution": p.weighted_contribution,
                "available": p.available,
                "partial": p.partial,
            }
            for p in scored.pillars
        ],
        "kills": [k.rule_id for k in scored.kills if k.kills],
        "failed_gates": list(scored.failed_gates),
        "verdict_basis": list(scored.verdict_basis),
    }


def _candidate_status(ec: EvaluatedCandidate) -> str:
    if ec.outcome is CandidateOutcome.KILLED:
        return "rejected"
    if ec.outcome is CandidateOutcome.UNRESOLVED:
        return "new"
    scored = ec.scored
    assert scored is not None
    if scored.verdict is Verdict.AVOID and not scored.insufficient_data:
        return "rejected"
    if scored.verdict in (Verdict.BUY, Verdict.TEST):
        return "shortlist"
    return "new"  # insufficient data → revisit
