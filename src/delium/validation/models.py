"""Typed models for the deterministic validation pipeline (ARCHITECTURE.md §4.2).

Pure data structures only — no I/O, no LLM, no clock, no randomness. The
validation *package* orchestrates ingestion + analysis + scoring, but these
models are inert types shared across it. scoring.py remains the sole owner of the
final Buy/Test/Avoid verdict; nothing here re-derives one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from delium.agents.schemas import MinerReport, StrategistVerdict
from delium.analysis.models import DifferentiationReport, Marketplace, ScoredOpportunity
from delium.discovery.assembly import AssemblyProvenance
from delium.discovery.models import DiscoveryEvidence


class ValidationStatus(StrEnum):
    """The terminal state of a validation run. These are deliberately distinct so
    the CLI (and a caller) can tell a retryable data gap from a real rejection
    (docs/data-layer.md §4 reject-for-insufficient-data)."""

    SCORED = "scored"  # full deterministic scoring produced a verdict
    HARD_KILLED = "hard_killed"  # eliminated by a cheap hard kill (no review spend)
    INSUFFICIENT_DATA = "insufficient_data"  # target dead / no cluster volume / fee-blocking
    INVALID_TARGET = "invalid_target"  # could not parse an ASIN/URL/keyword
    PRODUCT_NOT_FOUND = "product_not_found"  # provider resolved, ASIN does not exist
    PROVIDER_ERROR = "provider_error"  # live provider failure (retryable)
    MISSING_CREDENTIALS = "missing_credentials"  # no provider AND nothing cached to work from


@dataclass(frozen=True)
class ValidationRequest:
    """The identity + knobs of one validation, preserved for reproducibility.

    `target` is the raw user input (ASIN, Amazon URL, or keyword); `asin` is the
    resolved product once target resolution runs. `run_id` ties every fetch and
    the persisted validation together (docs §12 run management)."""

    target: str
    marketplace: Marketplace
    run_id: str
    tier: str = "validate"
    force: bool = False
    cogs_usd: float | None = None
    freight_usd: float | None = None
    dims_mm: tuple[int, int, int] | None = None
    weight_g: int | None = None


@dataclass(frozen=True)
class ReviewEvidence:
    """What the review layer supplied to the differentiation engine. The real
    review *sample* (the differentiation denominator) is always assembled from
    persisted reviews. `miner_pending` is True only when the LLM Review Miner did
    NOT run for this evidence (agents off / unavailable) — the themes/features/
    bundles then come from whatever was already persisted, never fabricated."""

    asin: str
    sample_size: int  # eligible (de-duplicated) reviews in the differentiation sample
    themes_available: int  # persisted review_themes rows fed as RawThemes
    listing_rating_avg: float | None  # target's displayed rating (sample-bias check)
    feature_requests: int = 0  # persisted feature_requests fed to F2
    bundle_signals: int = 0  # persisted bundle_signals fed to F4
    miner_pending: bool = True


@dataclass(frozen=True)
class AgentRunInfo:
    """Audit record for one LLM agent invocation — provenance for the report and
    the persisted `agent_runs` row. Carries no evidence itself; the validated
    output lives in the typed report objects and the DB."""

    agent: str  # 'review_miner' | 'strategist'
    status: str  # 'ok' | 'degraded' | 'failed'
    model: str | None = None
    provider: str | None = None
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    dropped: int = 0
    total: int = 0
    error: str | None = None


@dataclass(frozen=True)
class HydrationOutcome:
    """What hydration fetched and what it cost — an auditable spend ledger for the
    run (docs §16 cache/API-cost control). `degraded` marks a run that stopped
    fetching early (budget or provider-failure ceiling)."""

    data_cost_usd: float = 0.0
    llm_cost_usd: float = 0.0
    product_from_cache: bool | None = None
    reviews_from_cache: bool | None = None
    review_provider: str | None = None
    competitors_hydrated: int = 0
    reviews_fetched: int = 0
    degraded: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationReport:
    """The full, self-contained result of a validation run.

    `scored` is the deterministic `ScoredOpportunity` from scoring.py — the sole
    source of the verdict. `strategist_pending` is always True: G5 (the LLM
    Strategist) is a downstream, unimplemented stage, and a BUY here is
    provisional (docs §10). Nothing in this report is an "AI recommendation"."""

    request: ValidationRequest
    asin: str | None
    marketplace: Marketplace
    status: ValidationStatus
    scored: ScoredOpportunity | None = None
    review_evidence: ReviewEvidence | None = None
    hydration: HydrationOutcome = field(default_factory=HydrationOutcome)
    provenance: AssemblyProvenance = field(default_factory=AssemblyProvenance)
    from_candidate: bool = False  # a persisted discovery candidate was upgraded
    discovery_evidence: tuple[DiscoveryEvidence, ...] = ()  # provenance incl. cross-market signal
    # Agent layer (validated outputs only; never freeform model text):
    miner_report: MinerReport | None = None
    strategist_verdict: StrategistVerdict | None = None
    differentiation: DifferentiationReport | None = None  # recomputed themes for the report
    agent_runs: tuple[AgentRunInfo, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def strategist_pending(self) -> bool:
        """A Buy is provisional while G5 is unresolved (Strategist off or degraded);
        the deterministic scored result is the authority."""
        return self.scored.strategist_pending if self.scored is not None else True

    @property
    def data_cost_usd(self) -> float:
        return self.hydration.data_cost_usd

    @property
    def llm_cost_usd(self) -> float:
        return self.hydration.llm_cost_usd
