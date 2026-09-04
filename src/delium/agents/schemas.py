"""Pydantic schemas for validated LLM agent output (docs/agent-layer.md §3, §4).

These are the *only* shape an agent's raw text is allowed to become. Structured
minimums (a Strategist `risk_register` of ≥2, `verdict_changers` of ≥2, etc.)
make hedged or one-sided output structurally invalid — the runner rejects it and
retries once, then degrades. `extra="ignore"` tolerates spurious fields without
failing the whole parse. Nothing here computes a score or a frequency; these are
inert data holders the deterministic engines and the persistence layer consume.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CogsImpact = Literal["none", "low", "moderate", "high"]
Likelihood = Literal["L", "M", "H"]
Impact = Literal["L", "M", "H"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# ---------------------------------------------------------------------------
# Review Miner (agent-layer §3)
# ---------------------------------------------------------------------------
class Complaint(_Model):
    theme: str
    severity: int = Field(default=2, ge=1, le=3)  # advisory — engine recomputes from stars
    quote_review_ids: list[str] = Field(default_factory=list)
    representative_quote_ids: list[str] = Field(default_factory=list)
    affects_asins: list[str] = Field(default_factory=list)
    category: str | None = None  # optional Miner tag, e.g. 'packaging' | 'usage' (F4)


class Praise(_Model):
    theme: str
    quote_review_ids: list[str] = Field(default_factory=list)


class MissingFeature(_Model):
    feature: str
    requested_in_review_ids: list[str] = Field(default_factory=list)
    # The Miner never asserts competitor absence — the code's job (Analyst matrix).
    present_in_competitors: str | bool | None = "unknown"


class ImprovementIdea(_Model):
    idea: str
    addresses_theme: str | None = None  # references a complaint theme label
    manufacturing_note: str | None = None
    cogs_impact_guess: CogsImpact = "moderate"  # categorical hunch, never a $ number


class BundleSignalOut(_Model):
    complement: str
    mentioned_in_review_ids: list[str] = Field(default_factory=list)


class MinerReport(_Model):
    complaints: list[Complaint] = Field(default_factory=list, max_length=10)
    praise: list[Praise] = Field(default_factory=list, max_length=6)
    missing_features: list[MissingFeature] = Field(default_factory=list, max_length=6)
    improvement_ideas: list[ImprovementIdea] = Field(default_factory=list, max_length=6)
    bundle_signals: list[BundleSignalOut] = Field(default_factory=list, max_length=4)
    sample_caveats: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Strategist (agent-layer §4)
# ---------------------------------------------------------------------------
class RationalePoint(_Model):
    point: str
    evidence: list[str] = Field(min_length=1)  # every claim carries citation(s)


class DifferentiationPlanItem(_Model):
    change: str
    addresses: str | None = None  # theme ref
    cogs_impact: CogsImpact = "moderate"
    defensibility_note: str | None = None


class LaunchShape(_Model):
    suggested_price_ref: str | None = None
    inventory_posture: Literal["lean", "standard"] = "standard"
    primary_keyword_ref: str | None = None


class RiskRegisterItem(_Model):
    risk: str
    likelihood: Likelihood = "M"
    impact: Impact = "M"
    mitigation: str
    evidence: list[str] = Field(default_factory=list)


class VerdictChanger(_Model):
    fact_that_would_flip: str
    how_to_obtain_it: str


class AssumptionChallenge(_Model):
    assumption_flag_ref: str
    why_questionable: str


class StrategistVerdict(_Model):
    verdict: Literal["buy", "test", "avoid"]
    conviction: int = Field(ge=1, le=5)
    agrees_with_score: bool
    # Structural minimums (agent-layer §4 guards): hedged/one-sided output is invalid.
    rationale: list[RationalePoint] = Field(min_length=2, max_length=8)
    differentiation_plan: list[DifferentiationPlanItem] = Field(default_factory=list, max_length=5)
    launch_shape: LaunchShape | None = None
    risk_register: list[RiskRegisterItem] = Field(min_length=2)  # ≥2 even on buy
    verdict_changers: list[VerdictChanger] = Field(min_length=2)
    assumption_challenges: list[AssumptionChallenge] = Field(default_factory=list)
    one_paragraph: str
