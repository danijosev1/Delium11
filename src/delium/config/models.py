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
    capital_max: float = 20000
    capital_min: float = 2000
    borderline_band: float = Field(0.10, ge=0, lt=1)


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
    capital: CapitalConfig = Field(default_factory=CapitalConfig)
    preferences: PreferencesConfig = Field(default_factory=PreferencesConfig)
    assumptions: AssumptionsConfig = Field(default_factory=AssumptionsConfig)
    stress: StressConfig = Field(default_factory=StressConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)
    score_weights: ScoreWeightsConfig = Field(default_factory=ScoreWeightsConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    kill_rules: KillRulesConfig = Field(default_factory=KillRulesConfig)
    risk_deductions: RiskDeductionsConfig = Field(default_factory=RiskDeductionsConfig)
    verdicts: VerdictsConfig = Field(default_factory=VerdictsConfig)
