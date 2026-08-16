"""Typed schema for config.toml.

This module only *models and validates* configuration — it contains no
calculation logic. The values here are consumed later by `analysis/` and
`agents/`, per docs/analysis-engine.md §9 and docs/scoring-model.md.

Every section mirrors a `[section]` table in config.toml. Unknown keys are
rejected (`extra="forbid"`) so a typo in config.toml fails loudly at startup
instead of being silently ignored.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base for all config sections: no silent typos, no surprise types."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketplaceConfig(StrictModel):
    country: str = "US"


class CacheConfig(StrictModel):
    """Per-class cache TTLs (docs/data-layer.md §2). A cached row older than its
    TTL is refetched; freshness is judged from the newest raw_fetch."""

    product_ttl_hours: float = Field(24, gt=0)
    keyword_ttl_days: float = Field(7, gt=0)
    serp_ttl_days: float = Field(3, gt=0)
    review_ttl_days: float = Field(14, gt=0)
    category_ttl_days: float = Field(30, gt=0)


class CapitalConfig(StrictModel):
    max_launch_budget: float = Field(15000, gt=0)


class PreferencesConfig(StrictModel):
    min_price: float = Field(18, ge=0)
    max_price: float = Field(60, gt=0)
    avoid: list[str] = Field(
        default_factory=lambda: ["oversized", "glass", "batteries", "topicals", "gated"]
    )


class AssumptionsConfig(StrictModel):
    default_cogs_pct: float = Field(0.25, gt=0, lt=1)
    cogs_optimistic_pct: float = Field(0.20, gt=0, lt=1)
    cogs_pessimistic_pct: float = Field(0.32, gt=0, lt=1)
    freight_per_unit: float = Field(0.90, ge=0)
    duty_pct: float = Field(0.05, ge=0, lt=1)
    tacos_pct: float = Field(0.15, ge=0, lt=1)
    return_rate_default: float = Field(0.04, ge=0, lt=1)
    inventory_months: float = Field(2.5, gt=0)
    ppc_ramp_usd: float = Field(2000, ge=0)
    fixed_launch_usd: float = Field(1500, ge=0)
    packaging_dims_allowance: float = Field(0.10, ge=0)


class StressConfig(StrictModel):
    price_delta: float = -0.10
    cogs_delta: float = 0.15
    tacos_delta_pts: float = 0.05


class GatesConfig(StrictModel):
    min_margin: float = Field(0.30, ge=0, lt=1)
    min_roi: float = Field(1.00, ge=0)
    max_payback_months: float = Field(6, gt=0)
    risk_floor: float = Field(40, ge=0, le=100)
    differentiation_floor: float = Field(45, ge=0, le=100)


class ScoreWeightsConfig(StrictModel):
    demand: float = Field(25, ge=0)
    competition: float = Field(25, ge=0)
    differentiation: float = Field(20, ge=0)
    profitability: float = Field(20, ge=0)
    risk: float = Field(10, ge=0)


class BudgetsConfig(StrictModel):
    max_data_usd_per_validate: float = Field(3.0, gt=0)
    max_data_usd_per_discover: float = Field(0.5, gt=0)
    max_llm_usd_per_validate: float = Field(1.0, gt=0)
    monthly_spend_alarm_usd: float = Field(120, gt=0)


class KillRulesConfig(StrictModel):
    price_min: float = 15
    price_max: float = 70
    max_median_reviews: int = 3000
    max_brand_slots: int = 5
    fad_spike_ratio: float = 3.0
    fad_min_history_months: int = 12  # K10: fad only if < this much volume history
    excellent_listing_quality: float = Field(80, ge=0, le=100)  # K7: avg ≥ 8/10
    excellent_complaint_rate: float = Field(0.05, ge=0, le=1)  # K7: complaints < 5%
    capital_max: float = 20000
    capital_min: float = 2000
    borderline_band: float = Field(0.10, ge=0, lt=1)


class SufficiencyConfig(StrictModel):
    """Stage-1 data-sufficiency thresholds and caps (scoring-model §3)."""

    keepa_history_days: int = Field(60, gt=0)  # per competitor
    keepa_min_asins: int = Field(7, ge=0)  # ≥ this many of top-10 with history
    review_sample_min: int = Field(150, ge=0)  # across target + top 3
    keyword_min_phrases: int = Field(5, ge=0)  # keywords with resolved volume
    price_history_min_coverage: float = Field(0.5, ge=0, le=1)  # ≥ 5 of 10
    demand_cap_thin_keepa: float = Field(60, ge=0, le=100)
    demand_cap_missing_keywords: float = Field(50, ge=0, le=100)
    differentiation_cap_thin_reviews: float = Field(50, ge=0, le=100)


class ProfitPillarConfig(StrictModel):
    """Pillar-4 normalization bounds (scoring-model §7). The profit engine emits
    raw unit economics; this maps them to the 0-100 pillar."""

    margin_lo: float = 0.25
    margin_hi: float = 0.45
    roi_lo: float = 1.00
    roi_hi: float = 3.00
    capital_ideal_lo: float = 5000
    capital_ideal_hi: float = 14000
    capital_soft_hi: float = 17000  # score decays 100→60 across ideal_hi..soft_hi
    capital_hard_hi: float = 20000  # score decays 60→0 across soft_hi..hard_hi
    capital_thin_score: float = 70  # positive capital below ideal_lo
    payback_ideal_months: float = 4  # ≤ → 100
    payback_zero_months: float = 9  # ≥ → 0
    gate_fail_cap: float = 40  # failing G1 caps the pillar here
    weight_margin: float = 35
    weight_roi: float = 25
    weight_capital: float = 20
    weight_payback: float = 20


class RiskDeductionsConfig(StrictModel):
    ip: float = 40
    compliance: float = 30
    trend: float = 25
    seasonal: float = 20
    seasonal_unknown: float = 10
    returns: float = 20
    fragility: float = 20
    kw_concentration: float = 15
    market_hhi: float = 15
    supplier: float = 10


class VerdictsConfig(StrictModel):
    buy_min: float = Field(75, ge=0, le=100)
    test_min: float = Field(60, ge=0, le=100)


class DeliumConfig(StrictModel):
    """Root configuration object loaded from config.toml."""

    marketplace: MarketplaceConfig = Field(default_factory=MarketplaceConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    capital: CapitalConfig = Field(default_factory=CapitalConfig)
    preferences: PreferencesConfig = Field(default_factory=PreferencesConfig)
    assumptions: AssumptionsConfig = Field(default_factory=AssumptionsConfig)
    stress: StressConfig = Field(default_factory=StressConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)
    score_weights: ScoreWeightsConfig = Field(default_factory=ScoreWeightsConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    kill_rules: KillRulesConfig = Field(default_factory=KillRulesConfig)
    sufficiency: SufficiencyConfig = Field(default_factory=SufficiencyConfig)
    profit_pillar: ProfitPillarConfig = Field(default_factory=ProfitPillarConfig)
    risk_deductions: RiskDeductionsConfig = Field(default_factory=RiskDeductionsConfig)
    verdicts: VerdictsConfig = Field(default_factory=VerdictsConfig)
