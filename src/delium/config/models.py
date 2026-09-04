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


class CrossMarketConfig(StrictModel):
    """Cross-market discovery thresholds/weights (docs/cross-market.md). A
    discovery signal, independent of the five opportunity pillars."""

    # --- product matching ---
    match_title_weight: float = Field(0.45, ge=0)
    match_brand_weight: float = Field(0.20, ge=0)
    match_dims_weight: float = Field(0.20, ge=0)
    match_category_weight: float = Field(0.15, ge=0)
    match_exact_min: float = Field(0.90, ge=0, le=1)
    match_strong_min: float = Field(0.72, ge=0, le=1)
    match_probable_min: float = Field(0.55, ge=0, le=1)
    match_weak_min: float = Field(0.35, ge=0, le=1)
    match_dims_tolerance: float = Field(0.15, ge=0)  # fractional agreement band
    match_weight_tolerance: float = Field(0.15, ge=0)
    match_brand_mismatch_penalty: float = Field(0.15, ge=0)  # non-generic brand clash

    # --- source-market success ---
    src_velocity_lo: float = 150
    src_velocity_hi: float = 2000
    src_keyword_lo: float = 2000
    src_keyword_hi: float = 40000
    src_growth_lo: float = -0.10
    src_growth_hi: float = 0.40
    src_history_lo: float = 6
    src_history_hi: float = 24
    src_reviews_lo: float = 50
    src_reviews_hi: float = 3000
    w_src_velocity: float = 30
    w_src_keyword: float = 25
    w_src_growth: float = 15
    w_src_history: float = 15
    w_src_reviews: float = 15
    src_emerging_max: float = Field(40, ge=0, le=100)
    src_validated_max: float = Field(60, ge=0, le=100)
    src_strong_max: float = Field(80, ge=0, le=100)
    src_min_signals: int = Field(2, ge=1)

    # --- target-market demand ---
    tgt_keyword_lo: float = 500
    tgt_keyword_hi: float = 20000
    tgt_growth_lo: float = -0.10
    tgt_growth_hi: float = 0.50
    w_tgt_keyword: float = 70
    w_tgt_growth: float = 20
    w_tgt_serp: float = 10
    tgt_demand_credible_min: float = Field(35, ge=0, le=100)
    tgt_min_signals: int = Field(2, ge=1)

    # --- target-market competition weakness (higher = weaker/easier) ---
    tgt_reviews_lo: float = 50
    tgt_reviews_hi: float = 2000
    w_cw_reviews: float = 40
    w_cw_listing: float = 25
    w_cw_beatable: float = 20
    w_cw_hhi: float = 15
    empty_market_weakness: float = Field(75, ge=0, le=100)  # 0 listings, inferred

    # --- presence classification ---
    mature_reviews: float = 800
    saturated_reviews: float = 1500
    mature_min_listings: int = 8
    demand_high: float = Field(60, ge=0, le=100)

    # --- transferability ---
    transfer_uncertain_penalty: float = 15
    transfer_unfavorable_penalty: float = 35
    transfer_favorable_min: float = Field(80, ge=0, le=100)
    transfer_uncertain_min: float = Field(45, ge=0, le=100)
    seasonality_uncertain_threshold: float = Field(0.40, ge=0, le=1)

    # --- cross-market composite weights (sum 100) ---
    w_source_success: float = 25
    w_target_demand: float = 25
    w_competition_gap: float = 20
    w_maturity_gap: float = 15
    w_transferability: float = 15

    # --- risk penalties (surfaced, not recomputed) ---
    compliance_risk_penalty: float = Field(25, ge=0)
    unfavorable_transfer_penalty: float = Field(15, ge=0)

    # --- verdict thresholds ---
    strong_opportunity_min: float = Field(70, ge=0, le=100)
    validate_min: float = Field(45, ge=0, le=100)
    strong_competition_gap_min: float = Field(55, ge=0, le=100)
    low_confidence_score_cap: float = Field(55, ge=0, le=100)


class DiscoveryConfig(StrictModel):
    """Discovery / scout orchestration limits (ARCHITECTURE.md §4.1, docs/data-layer
    §3.2). Cheap-and-wide triage: no review fetching, kill-first funnel."""

    max_candidates: int = Field(150, gt=0)  # total unique candidates per run
    max_candidates_per_seed: int = Field(20, gt=0)  # SERP asins kept per keyword
    serp_depth: int = Field(10, gt=0)  # SERP page-1 top-N at discovery
    max_ranked: int = Field(25, gt=0)  # ranked shortlist size
    cross_market_enabled: bool = True
    accept_explicit_asins: bool = True
    # Cross-market discovery source gating (mirrors [cross_market] semantics).
    cross_market_min_source_maturity: str = Field(
        "validated", pattern="^(emerging|validated|strong|exceptional)$"
    )
    cross_market_min_source_units: int = Field(0, ge=0)


class AgentsConfig(StrictModel):
    """LLM agent layer (docs/agent-layer.md). Model *tier* is config, not code:
    `fast` (Haiku-class) for Scout/Analyst/Review Miner, `frontier` (Sonnet-class)
    for the Strategist only. Agents run only when an LLM client is available;
    missing credentials or a provider/validation failure degrades to the
    deterministic result and is never fabricated. Pricing lives here (external),
    never hardcoded in the client, so cost accounting stays auditable."""

    enabled: bool = True  # master switch; False → agents never run (deterministic-only)
    review_miner_enabled: bool = True
    strategist_enabled: bool = True
    fast_model: str = "claude-haiku-4-5"  # Scout / Analyst / Review Miner
    frontier_model: str = "claude-sonnet-5"  # Strategist only
    max_output_tokens_fast: int = Field(4096, gt=0)
    max_output_tokens_frontier: int = Field(5120, gt=0)
    temperature: float = Field(0.0, ge=0.0, le=1.0)  # 0 → reproducible agent output
    max_retries: int = Field(1, ge=0)  # one retry on validation failure (agent-layer §5.2)
    # Per-1M-token USD pricing for cost accounting (Haiku 4.5 / Sonnet 5 defaults).
    fast_input_usd_per_mtok: float = Field(1.0, ge=0)
    fast_output_usd_per_mtok: float = Field(5.0, ge=0)
    frontier_input_usd_per_mtok: float = Field(2.0, ge=0)
    frontier_output_usd_per_mtok: float = Field(10.0, ge=0)
    evidence_drop_threshold: float = Field(0.20, ge=0, le=1)  # >this dropped → fail (§5.3)
    min_quote_ids: int = Field(3, ge=1)  # a theme needs ≥ this many supporting reviews


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
    cross_market: CrossMarketConfig = Field(default_factory=CrossMarketConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
