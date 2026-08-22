"""Typed models for the deterministic discovery / scout orchestration layer.

Pure data structures only — no I/O, no LLM, no clock, no randomness. The
discovery *package* may orchestrate providers/ingestion/analysis/scoring, but
these models are inert types shared across it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from delium.analysis.models import Marketplace, ScoredOpportunity


class DiscoverySource(StrEnum):
    """Where a candidate came from. Sources are pluggable; these are the V1 set
    (ARCHITECTURE.md §4.1 keyword/SERP, docs/cross-market.md, explicit ASIN)."""

    KEYWORD = "keyword"
    CROSS_MARKET = "cross_market"
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class DiscoveryEvidence:
    """One provenance record for how a candidate was discovered. Multiple pieces
    of evidence merge onto a single candidate (dedup), never duplicate it."""

    source: DiscoverySource
    reference: str  # seed keyword / source marketplace / 'user'
    detail: str = ""
    serp_position: int | None = None  # keyword/SERP rank, if applicable
    cross_market_score: float | None = None  # cross-market signal, if applicable

    def key(self) -> tuple[str, str, str, int | None]:
        """Identity of this evidence for dedup within a candidate."""
        return (self.source.value, self.reference, self.detail, self.serp_position)

    def sort_key(self) -> tuple[str, str, str, int]:
        """Total-order key for deterministic sorting (None → -1, always first)."""
        pos = -1 if self.serp_position is None else self.serp_position
        return (self.source.value, self.reference, self.detail, pos)


@dataclass(frozen=True)
class Candidate:
    """A discovered product to consider for research. Identity is
    (asin, marketplace): the same ASIN found via many keywords / SERPs /
    cross-market paths is ONE candidate with merged evidence, not many."""

    asin: str
    marketplace: Marketplace
    evidence: tuple[DiscoveryEvidence, ...] = ()

    @property
    def identity(self) -> tuple[str, str]:
        return (self.asin, self.marketplace.value)

    @property
    def sources(self) -> tuple[DiscoverySource, ...]:
        seen: dict[DiscoverySource, None] = {}
        for e in self.evidence:
            seen.setdefault(e.source, None)
        return tuple(seen)

    def merged_with(self, other: Candidate) -> Candidate:
        """Merge another candidate for the same identity: union of evidence,
        deterministically de-duplicated and ordered. Never mutates either input."""
        if self.identity != other.identity:
            raise ValueError(
                f"cannot merge different identities: {self.identity} vs {other.identity}"
            )
        combined: dict[tuple[str, str, str, int | None], DiscoveryEvidence] = {}
        for e in (*self.evidence, *other.evidence):
            combined.setdefault(e.key(), e)
        ordered = tuple(sorted(combined.values(), key=lambda e: e.sort_key()))
        return replace(self, evidence=ordered)

    def with_sorted_evidence(self) -> Candidate:
        return replace(self, evidence=tuple(sorted(self.evidence, key=lambda e: e.sort_key())))


class CandidateOutcome(StrEnum):
    """What the orchestration did with a candidate."""

    KILLED = "killed"  # eliminated by a cheap hard kill (no enrichment spent)
    SCORED = "scored"  # ran full scoring.py
    UNRESOLVED = "unresolved"  # could not hydrate the minimum data to assess


@dataclass(frozen=True)
class EvaluatedCandidate:
    """A candidate after orchestration: its outcome and (if scored) the full
    deterministic ScoredOpportunity — scoring.py stays the sole verdict owner."""

    candidate: Candidate
    outcome: CandidateOutcome
    scored: ScoredOpportunity | None = None
    kill_rule: str | None = None  # the triggering kill id, when KILLED
    notes: tuple[str, ...] = ()

    @property
    def asin(self) -> str:
        return self.candidate.asin

    @property
    def marketplace(self) -> Marketplace:
        return self.candidate.marketplace


@dataclass(frozen=True)
class DiscoveryReport:
    """The full deterministic result of a discovery run."""

    run_id: str
    marketplace: Marketplace
    discovered: tuple[Candidate, ...]  # deduped candidate set
    ranked: tuple[EvaluatedCandidate, ...]  # scored survivors, ranked
    killed: tuple[EvaluatedCandidate, ...]  # cheap-killed
    unresolved: tuple[EvaluatedCandidate, ...]  # insufficient data to assess
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def discovered_count(self) -> int:
        return len(self.discovered)
