"""Read-only scoring diagnostics (no API calls).

Rebuilds the deterministic `ScoredOpportunity` for products already in the
database — from persisted Keepa/SERP/review data via the SAME assembler and
scoring.py the pipelines use — and explains, per pillar, what drove the score
and why confidence is what it is. It changes nothing: no provider, no writes, no
new thresholds. It answers "why is opportunity low / confidence LOW, and which
evidence fix would raise it" for an existing emerging/discover run.

Key distinction it surfaces (scoring-model §3): an ABSENT pillar is *unknown*
(excluded from the composite, lowers confidence), not *bad* (scored 0). A
present-but-thin pillar keeps its sufficiency-capped score. So a low opportunity
number on a finder candidate reflects genuinely low pillars, not missing data.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass

from delium.analysis.models import Confidence, Marketplace, ScoredOpportunity
from delium.analysis.scoring import score_opportunity
from delium.config.models import DeliumConfig
from delium.database import repository
from delium.discovery.assembly import build_scoring_input

# Canonical evidence-fix keys → the human label used in the report. Each low
# pillar / partial cap maps to exactly one fix so counts are addable.
FIX_LABELS: dict[str, str] = {
    "serp_competitors": "keyword→SERP competitor set for finder candidates",
    "reviews_llm": "review provider + LLM keys (Review Miner / Analyst)",
    "keepa_sales": "Keepa monthly-sales cross-check (thin BSR history)",
    "fba_fees": "Keepa FBA fees — dimensions + weight",
    "re_observation": "re-observation over time (accumulate history)",
    "calibration": "calibration / post-mortem weights",
}


@dataclass(frozen=True)
class PillarDiagnosis:
    pillar: str
    available: bool
    partial: bool
    raw: float | None
    capped: float | None
    weight: float
    contribution: float
    confidence: str
    cap_reason: str | None
    driver: str  # the exact evidence string the pillar reported


@dataclass(frozen=True)
class ConfidenceCause:
    pillar: str
    cause: str  # why this pillar lowers confidence
    fix_key: str  # canonical fix that would remove it (see FIX_LABELS)

    @property
    def fix(self) -> str:
        return FIX_LABELS.get(self.fix_key, self.fix_key)


@dataclass(frozen=True)
class CandidateDiagnosis:
    asin: str
    marketplace: str
    verdict: str
    score: float
    confidence: str
    pillars: tuple[PillarDiagnosis, ...]
    causes: tuple[ConfidenceCause, ...]
    kill_reasons: tuple[str, ...]
    gate_reasons: tuple[str, ...]

    @property
    def blocking_fixes(self) -> frozenset[str]:
        """The distinct evidence fixes that are holding confidence below HIGH."""
        return frozenset(c.fix_key for c in self.causes)

    @property
    def sole_blocker(self) -> str | None:
        """The one fix that, applied alone, would clear every confidence cause
        (i.e. would lift this candidate toward MEDIUM/HIGH) — else None."""
        fixes = self.blocking_fixes
        return next(iter(fixes)) if len(fixes) == 1 else None


def _pillar_confidence_cause(pillar: str, conf: str, partial: bool, cap_reason: str | None) -> str:
    if not partial and conf == Confidence.LOW.value:
        return f"{pillar} report is low-confidence"
    if cap_reason:
        return cap_reason
    return f"{pillar} pillar absent"


def _fix_for(pillar: str, available: bool, cap_reason: str | None) -> str:
    """Map a missing/thin pillar to the single evidence fix that would remove it."""
    reason = (cap_reason or "").lower()
    if pillar == "competition":
        return "serp_competitors"
    if pillar == "differentiation":
        return "reviews_llm"
    if pillar == "profitability":
        return "fba_fees"
    if pillar == "demand":
        if "keyword" in reason:
            return "serp_competitors"
        if "keepa" in reason or "history" in reason:
            return "keepa_sales"
        return "keepa_sales"
    if pillar == "risk":
        return "re_observation"
    return "calibration"


def _causes(scored: ScoredOpportunity) -> tuple[ConfidenceCause, ...]:
    """Every reason this candidate's confidence is below HIGH, each tied to the
    one evidence fix that would remove it. HIGH-confidence pillars add nothing."""
    out: list[ConfidenceCause] = []
    for p in scored.pillars:
        blocks = (not p.available) or p.partial or p.confidence is Confidence.LOW
        if not blocks:
            continue
        cause = _pillar_confidence_cause(p.pillar, p.confidence.value, p.partial, p.cap_reason)
        out.append(ConfidenceCause(p.pillar, cause, _fix_for(p.pillar, p.available, p.cap_reason)))
    return tuple(out)


def diagnose_scored(asin: str, marketplace: str, scored: ScoredOpportunity) -> CandidateDiagnosis:
    """Turn a ScoredOpportunity into a per-pillar diagnosis + confidence causes."""
    pillars = tuple(
        PillarDiagnosis(
            pillar=p.pillar,
            available=p.available,
            partial=p.partial,
            raw=p.raw_score,
            capped=p.capped_score,
            weight=p.weight,
            contribution=p.weighted_contribution,
            confidence=p.confidence.value,
            cap_reason=p.cap_reason,
            driver=p.evidence,
        )
        for p in scored.pillars
    )
    kill_reasons = tuple(f"{k.rule_id}: {k.reason}" for k in scored.kills if k.kills)
    gate_reasons = tuple(
        f"{g.gate_id} {g.name}: {g.actual}" for g in scored.gates if g.passed is False
    )
    return CandidateDiagnosis(
        asin=asin,
        marketplace=marketplace,
        verdict=scored.verdict.value,
        score=scored.score,
        confidence=scored.confidence.level.value,
        pillars=pillars,
        causes=_causes(scored),
        kill_reasons=kill_reasons,
        gate_reasons=gate_reasons,
    )


def diagnose_candidate(
    conn: sqlite3.Connection, asin: str, marketplace: Marketplace, config: DeliumConfig
) -> CandidateDiagnosis | None:
    """Rebuild scoring for one stored product (read-only) and diagnose it. Returns
    None when the product was never fetched in this marketplace."""
    inp, _prov = build_scoring_input(conn, asin, marketplace, config)
    if inp is None:
        return None
    scored = score_opportunity(inp, config)
    return diagnose_scored(asin, marketplace.value, scored)


def diagnose_run(
    conn: sqlite3.Connection, run_id: str, config: DeliumConfig
) -> list[CandidateDiagnosis]:
    """Diagnose every candidate persisted for one emerging run (read-only)."""
    out: list[CandidateDiagnosis] = []
    for row in repository.get_emerging_candidates(conn, run_id):
        mp = Marketplace(row["marketplace"])
        diag = diagnose_candidate(conn, row["asin"], mp, config)
        if diag is not None:
            out.append(diag)
    return out


@dataclass(frozen=True)
class FixImpact:
    fix_key: str
    label: str
    blocks_count: int  # candidates whose confidence this fix is (part of) holding down
    sole_blocker_count: int  # candidates it would lift toward MEDIUM/HIGH on its own


def summarize_fixes(diagnoses: list[CandidateDiagnosis]) -> list[FixImpact]:
    """Aggregate the confidence-cause table: per evidence fix, how many candidates
    it blocks, and for how many it is the SOLE blocker (so fixing it alone lifts
    the candidate's confidence). Never changes a rule — a pure projection over the
    already-computed scores."""
    blocks: Counter[str] = Counter()
    sole: Counter[str] = Counter()
    for d in diagnoses:
        for key in d.blocking_fixes:
            blocks[key] += 1
        only = d.sole_blocker
        if only is not None:
            sole[only] += 1
    keys = sorted(blocks, key=lambda k: (-blocks[k], k))
    return [FixImpact(k, FIX_LABELS.get(k, k), blocks[k], sole[k]) for k in keys]
