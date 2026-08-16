"""Typed data structures for the deterministic profit engine.

Pure types only — no calculation, no I/O, no config or LLM imports. Money is
carried as integer **cents** to avoid floating-point drift; ratios (margins,
ROI, break-even PPC) are floats. See docs/analysis-engine.md §4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Fee-table structures (loaded from external data files; never hardcoded)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Dimensions:
    length_mm: int
    width_mm: int
    height_mm: int

    def sorted_desc(self) -> tuple[int, int, int]:
        a, b, c = sorted((self.length_mm, self.width_mm, self.height_mm), reverse=True)
        return a, b, c


@dataclass(frozen=True)
class FulfillmentBand:
    max_weight_g: int
    fee_cents: int


@dataclass(frozen=True)
class SizeTier:
    name: str
    max_weight_g: int
    max_longest_mm: int
    max_median_mm: int
    max_shortest_mm: int
    bands: tuple[FulfillmentBand, ...]
    overflow_base_cents: int | None = None
    overflow_per_kg_cents: int | None = None


@dataclass(frozen=True)
class FeeTable:
    version: str
    effective_date: str
    referral_default_percent: float
    referral_min_fee_cents: int
    referral_categories: dict[str, float]
    closing_default_cents: int
    closing_categories: dict[str, int]
    storage_standard_per_cf_cents: int
    storage_peak_per_cf_cents: int
    prep_default_cents: int
    size_tiers: tuple[SizeTier, ...]


@dataclass(frozen=True)
class FeeBreakdown:
    referral_cents: int
    fulfillment_cents: int
    closing_cents: int
    storage_monthly_cents: int  # blended standard/peak monthly, per unit
    storage_peak_cents: int
    prep_cents: int
    size_tier: str
    fee_table_version: str

    @property
    def amazon_fees_cents(self) -> int:
        """Amazon's cut per unit (referral + fulfillment + closing + storage)."""
        return (
            self.referral_cents
            + self.fulfillment_cents
            + self.closing_cents
            + self.storage_monthly_cents
        )


# ---------------------------------------------------------------------------
# Profit engine inputs / outputs
# ---------------------------------------------------------------------------
# Field names that may be marked "estimated" (assumption) vs "known" (quote).
ASSUMPTION_FIELDS = frozenset({"product_cost", "freight", "customs", "prep", "ppc", "return_rate"})


@dataclass(frozen=True)
class ProfitInputs:
    selling_price_cents: int
    product_cost_cents: int
    freight_cents: int
    customs_cents: int
    prep_cost_cents: int
    ppc_percent: float  # TACOS, fraction of revenue
    return_rate: float  # fraction of units
    monthly_sales_units: int
    # Which of ASSUMPTION_FIELDS are estimated rather than known-from-quote.
    estimated_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LaunchAssumptions:
    inventory_months: float = 2.5
    ppc_ramp_cents: int = 200_000  # $2,000
    fixed_launch_cents: int = 150_000  # $1,500


@dataclass(frozen=True)
class ProfitResult:
    # --- per unit ---
    revenue_cents: int
    landed_cost_cents: int
    amazon_fees_cents: int
    ppc_cost_cents: int
    returns_cost_cents: int
    gross_profit_cents: int
    contribution_margin_cents: int
    net_profit_cents: int
    gross_margin: float
    net_margin: float
    roi: float
    break_even_ppc: float
    # --- monthly / aggregate ---
    monthly_revenue_cents: int
    monthly_net_profit_cents: int
    monthly_cash_requirement_cents: int
    launch_capital_cents: int
    payback_months: float | None  # None = never recouped (non-positive net)
    # --- provenance ---
    confidence: Confidence
    assumption_flags: tuple[str, ...]
    fees: FeeBreakdown
    inputs: ProfitInputs


# ---------------------------------------------------------------------------
# Scenario engine
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScenarioAdjustment:
    price_mult: float = 1.0
    cost_mult: float = 1.0
    ppc_delta: float = 0.0  # additive to ppc_percent
    return_mult: float = 1.0


@dataclass(frozen=True)
class ScenarioAssumptions:
    optimistic: ScenarioAdjustment
    expected: ScenarioAdjustment
    stressed: ScenarioAdjustment
    worst_case: ScenarioAdjustment

    @classmethod
    def default(cls) -> ScenarioAssumptions:
        return cls(
            optimistic=ScenarioAdjustment(
                price_mult=1.0, cost_mult=0.85, ppc_delta=-0.03, return_mult=0.8
            ),
            expected=ScenarioAdjustment(),
            stressed=ScenarioAdjustment(
                price_mult=0.90, cost_mult=1.15, ppc_delta=0.05, return_mult=1.25
            ),
            worst_case=ScenarioAdjustment(
                price_mult=0.85, cost_mult=1.30, ppc_delta=0.10, return_mult=1.5
            ),
        )


@dataclass(frozen=True)
class ScenarioSet:
    optimistic: ProfitResult
    expected: ProfitResult
    stressed: ProfitResult
    worst_case: ProfitResult
    confidence: Confidence

    def as_dict(self) -> dict[str, ProfitResult]:
        return {
            "optimistic": self.optimistic,
            "expected": self.expected,
            "stressed": self.stressed,
            "worst_case": self.worst_case,
        }


# ---------------------------------------------------------------------------
# Listing analysis engine
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ListingInput:
    """Observable, deterministic listing facts (counts/booleans/text lengths).

    Every field is optional: an absent field lowers confidence, it is never
    guessed. Populated from normalized product data + the Analyst's structured
    rubric + SERP competitor prices."""

    title: str | None = None
    bullets: tuple[str, ...] | None = None
    images_count: int | None = None
    has_aplus: bool | None = None
    has_brand_store: bool | None = None
    has_video: bool | None = None
    variation_count: int | None = None
    review_count: int | None = None
    rating: float | None = None
    keywords: tuple[str, ...] | None = None  # target keywords for coverage
    price_cents: int | None = None
    competitor_prices_cents: tuple[int, ...] | None = None  # from SERP
    brand: str | None = None


@dataclass(frozen=True)
class Subscore:
    name: str
    value: float | None  # None = could not be assessed (input missing)
    weight: float
    detail: str  # human-readable explanation of the raw input → value

    @property
    def available(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class ListingQualityReport:
    overall_score: float  # 0-100, higher = better listing
    confidence: Confidence
    subscores: tuple[Subscore, ...]

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.subscores if not s.available)

    @property
    def assessed(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.subscores if s.available)


# ---------------------------------------------------------------------------
# Demand analysis engine
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BsrPoint:
    date: date
    bsr: int


@dataclass(frozen=True)
class AsinHistory:
    """A single ASIN's BSR observations (target first, then competitors)."""

    asin: str
    observations: tuple[BsrPoint, ...]


@dataclass(frozen=True)
class KeywordDatum:
    phrase: str
    volume: int | None  # None = volume unknown (excluded from sums, per data-layer §1.2)


@dataclass(frozen=True)
class VelocityAnchor:
    bsr: int
    units: int


@dataclass(frozen=True)
class CategoryCurve:
    name: str
    base_factor: float
    multi_unit_factor: float
    anchors: tuple[VelocityAnchor, ...]


@dataclass(frozen=True)
class VelocityCurves:
    version: str
    default: CategoryCurve
    categories: dict[str, CategoryCurve]

    def resolve(self, category: str | None) -> tuple[CategoryCurve, bool]:
        """Return (curve, category_known). Falls back to the default curve."""
        if category is not None and category in self.categories:
            return self.categories[category], True
        return self.default, False


@dataclass(frozen=True)
class DemandConfig:
    # D1 search volume (log-normalized cluster volume)
    volume_lo: float = 2000
    volume_hi: float = 40000
    dedup_substring_weight: float = 0.30
    # D2 sales-velocity sweet spot (rise_lo, rise_hi, fall_lo, fall_hi, floor)
    velocity_curve: tuple[float, float, float, float, float] = (150, 300, 1200, 2500, 70)
    # D3 BSR trend (annual log10 improvement mapped to 0-100)
    trend_lo: float = -0.20
    trend_hi: float = 0.40
    # D4 market growth (YoY keyword volume)
    growth_lo: float = -0.10
    growth_hi: float = 0.40
    # D5 seasonality (peak-8-week concentration)
    seasonality_lo: float = 0.20
    seasonality_hi: float = 0.60
    seasonal_flag_threshold: float = 0.40
    # sales-estimate bounds
    low_mult: float = 0.8
    high_mult: float = 1.6
    market_share_factor: float = 1.25  # page-1 ≈ 80% of demand
    # thresholds
    full_history_days: int = 60
    partial_history_days: int = 30
    seasonality_min_days: int = 365
    seasonality_min_weeks: int = 8
    full_volumed_phrases: int = 5
    # component weights (sum 100)
    weight_search_volume: float = 30
    weight_sales_velocity: float = 30
    weight_bsr_trend: float = 20
    weight_market_growth: float = 10
    weight_seasonality: float = 10


@dataclass(frozen=True)
class SalesEstimate:
    asin: str
    low_units: int
    expected_units: int
    high_units: int
    confidence: Confidence
    method: str  # 'rank_drop' | 'curve_fallback'
    observed_days: int
    n_observations: int
    drops: int
    current_bsr: int
    rank_reference_units: int


@dataclass(frozen=True)
class KeywordDemand:
    total_volume: int
    deduplicated_volume: int
    primary_phrase: str | None
    primary_volume: int
    demand_concentration: float  # primary share of total (single-keyword dependence)
    volumed_phrase_count: int
    yoy_growth: float | None
    search_volume_score: float | None
    market_growth_score: float | None


@dataclass(frozen=True)
class BsrTrend:
    direction: str  # 'improving' | 'declining' | 'flat' | 'unknown'
    magnitude: float | None  # abs annual log10 change
    median_annual_change: float | None  # signed; positive = rank improving
    score: float | None
    asins_with_slope: int


@dataclass(frozen=True)
class Seasonality:
    assessable: bool
    peak_concentration: float | None
    seasonal_flag: bool | None
    score: float | None
    weeks_observed: int


@dataclass(frozen=True)
class DemandReport:
    pillar_score: float  # 0-100 demand pillar for scoring.py
    confidence: Confidence
    components: tuple[Subscore, ...]
    sales_estimates: tuple[SalesEstimate, ...]
    market_units_low: int
    market_units_expected: int
    market_units_high: int
    keyword_demand: KeywordDemand
    bsr_trend: BsrTrend
    seasonality: Seasonality

    @property
    def missing_components(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.components if not s.available)
