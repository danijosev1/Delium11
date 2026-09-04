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


# ---------------------------------------------------------------------------
# Competition analysis engine
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PricePoint:
    date: date
    price_cents: int


@dataclass(frozen=True)
class CompetitorSnapshot:
    """One organic top-N competitor. All signals optional; absence lowers
    confidence and is never guessed. `listing_quality` is the overall_score from
    listing.py (0-100) — it is NOT recomputed here."""

    asin: str
    brand: str | None = None
    review_count: int | None = None
    rating: float | None = None
    price_cents: int | None = None
    listing_quality: float | None = None
    review_count_90d_ago: int | None = None
    price_history: tuple[PricePoint, ...] | None = None


@dataclass(frozen=True)
class CompetitionConfig:
    # C1 review moat
    review_moat_lo: float = 200
    review_moat_hi: float = 3000
    # C2 beatable slots
    beatable_review_threshold: int = 150
    beatable_cap: int = 4
    beatable_per_slot: float = 25
    weak_listing_threshold: float = 60  # listing quality below this = weak/beatable
    # C3 review velocity of leaders
    velocity_lo: float = 20
    velocity_hi: float = 300
    velocity_low_class: float = 20  # < → 'low'
    velocity_high_class: float = 100  # > → 'high'
    velocity_leaders: int = 3
    # C4 brand dominance
    big_brand_penalty: float = 25
    hhi_flag_threshold: float = 0.30
    # C5 listing quality advantage
    listing_advantage_threshold: float = 80  # avg competitor quality below → advantage
    # C6 price competition
    price_cv_lo: float = 0.05
    price_cv_hi: float = 0.25
    price_war_min_slots: int = 3
    price_war_recent_days: int = 14
    price_window_days: int = 90
    neutral_score: float = 50.0
    # analysis scope
    top_n: int = 10
    # component weights (sum 100) — docs/scoring-model.md §5
    weight_review_moat: float = 30
    weight_beatable_slots: float = 20
    weight_review_velocity: float = 15
    weight_brand_dominance: float = 15
    weight_listing_gap: float = 15
    weight_price_competition: float = 5


@dataclass(frozen=True)
class CompetitionInput:
    competitors: tuple[CompetitorSnapshot, ...]  # organic top-N, SERP order
    as_of: date
    recognized_brands: frozenset[str] = frozenset()  # known dominant brands


@dataclass(frozen=True)
class ReviewMoat:
    median_reviews: float | None
    mean_reviews: float | None
    max_reviews: int | None
    score: float | None
    detail: str


@dataclass(frozen=True)
class BeatableSlots:
    count: int  # top-N with < threshold reviews (doc-exact C2 count)
    weak_listing_slots: int  # of those, how many also have a weak/unknown listing
    score: float | None
    detail: str


@dataclass(frozen=True)
class ReviewVelocity:
    monthly_velocity: float | None  # median of top-3 leaders' new reviews/month
    classification: str  # 'low' | 'moderate' | 'high' | 'unknown'
    leaders_with_history: int
    score: float | None
    detail: str


@dataclass(frozen=True)
class BrandConcentration:
    top_brand: str | None
    top_brand_slot_share: float | None
    hhi: float | None
    recognized_big_brand_present: bool
    concentration_flag: bool  # HHI > threshold
    score: float | None
    detail: str


@dataclass(frozen=True)
class ListingQualityAdvantage:
    avg_competitor_quality: float | None  # None → no listing data (neutral score used)
    advantage_score: float  # C5 (neutral fallback when no data)
    advantage_available: bool
    coverage: int  # competitors with a listing quality score
    detail: str


@dataclass(frozen=True)
class PriceCompetition:
    median_price_cents: int | None
    price_spread_cents: int | None
    clustering_cv: float | None  # cross-sectional CV of current prices
    median_price_cv_90d: float | None  # time-series volatility
    score: float  # C6 (neutral fallback when no history)
    price_war_flag: bool
    competitors_at_recent_low: int
    detail: str


@dataclass(frozen=True)
class CompetitionConfidence:
    level: Confidence
    competitors_analyzed: int
    review_history_coverage: float
    listing_quality_coverage: float
    price_history_coverage: float


@dataclass(frozen=True)
class CompetitionReport:
    pillar_score: float  # 0-100 competition pillar (higher = more beatable)
    confidence: CompetitionConfidence
    review_moat: ReviewMoat
    beatable_slots: BeatableSlots
    review_velocity: ReviewVelocity
    brand_concentration: BrandConcentration
    listing_quality_advantage: ListingQualityAdvantage
    price_competition: PriceCompetition
    components: tuple[Subscore, ...]
    data_gaps: tuple[str, ...]  # absent input categories

    @property
    def missing_components(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.components if not s.available)

    @property
    def hhi(self) -> float | None:
        return self.brand_concentration.hhi

    @property
    def price_war_flag(self) -> bool:
        return self.price_competition.price_war_flag


# ---------------------------------------------------------------------------
# Differentiation analysis engine
# ---------------------------------------------------------------------------
class ThemeKind(StrEnum):
    COMPLAINT = "complaint"
    PRAISE = "praise"
    MISSING_FEATURE = "missing_feature"
    IMPROVEMENT = "improvement"
    BUNDLE = "bundle"


class Addressability(StrEnum):
    """Structured Strategist/Miner tag — an enum, never a number the engine
    trusts. The engine computes the numeric share itself."""

    FIXABLE = "fixable"  # addressable at low COGS delta
    PARTIAL = "partial"  # partially fixable
    HARD = "hard"  # not realistically fixable
    UNKNOWN = "unknown"  # unassessed — never scored optimistically


@dataclass(frozen=True)
class DiffReview:
    """One review in the eligible sample. Only id + stars are needed here."""

    review_id: str
    stars: int  # 1-5


@dataclass(frozen=True)
class RawTheme:
    """A Review Miner theme. `claimed_*` are LLM-supplied and ADVISORY ONLY —
    the engine recomputes frequency from cited ids and severity from cited stars."""

    theme_id: str
    kind: ThemeKind
    label: str
    supporting_review_ids: tuple[str, ...]
    addressability: Addressability = Addressability.UNKNOWN
    cogs_delta: float | None = None  # fraction; ≤ 0.15 required for FIXABLE
    category: str | None = None  # e.g. 'packaging', 'usage' — for F4 rubric
    claimed_frequency_pct: float | None = None  # IGNORED for arithmetic
    claimed_severity: int | None = None  # IGNORED for arithmetic


@dataclass(frozen=True)
class FeatureRequest:
    feature: str
    supporting_review_ids: tuple[str, ...]
    absent_from_competitors: bool | None  # True = confirmed gap, None = unknown


@dataclass(frozen=True)
class BundleSignal:
    complement: str
    supporting_review_ids: tuple[str, ...]


@dataclass(frozen=True)
class DifferentiationConfig:
    min_quotes: int = 3  # honesty guard: theme needs ≥ this many verified quotes
    # F1 complaint intensity
    intensity_lo: float = 10
    intensity_hi: float = 60
    severity_high_stars: float = 2.0  # mean cited stars ≤ → severity 3
    severity_mid_stars: float = 3.0  # mean cited stars ≤ → severity 2
    # F2 missing features
    feature_cap: int = 4
    feature_per: float = 25
    # F3 addressability weights
    addressable_cogs_max: float = 0.15
    weight_fixable: float = 1.0
    weight_partial: float = 0.5
    # F4 bundle & packaging rubric (25 pts each)
    rubric_points: float = 25
    bundle_freq_threshold: float = 0.03
    packaging_freq_threshold: float = 0.05
    usage_freq_threshold: float = 0.05
    # sample bias (data-layer §1.3)
    bias_threshold: float = 0.4
    bias_adjustment: float = 5.0
    # confidence sample-size bands (analysis-engine §3)
    sample_high: int = 150
    sample_medium: int = 30
    unresolved_ratio_floor: float = 0.5
    # component weights (docs/scoring-model.md §6) — sum 100
    weight_complaint_intensity: float = 40
    weight_missing_features: float = 25
    weight_addressability: float = 20
    weight_bundle_packaging: float = 15


@dataclass(frozen=True)
class DifferentiationTheme:
    theme_id: str
    label: str
    supporting_count_claimed: int  # ids supplied
    verified_count: int  # unique ids present in the sample
    frequency: float  # recomputed, 0-1
    severity: int | None  # 1-3 from cited stars; None if unverifiable
    intensity: float  # frequency_pct × severity
    addressability: Addressability
    counted: bool  # passed the ≥ min_quotes guard
    detail: str


@dataclass(frozen=True)
class DifferentiationConfidence:
    level: Confidence
    sample_size: int
    verified_theme_ratio: float  # resolved ids / claimed ids
    themes_with_evidence: int
    feature_evidence: bool
    sample_bias_flagged: bool


@dataclass(frozen=True)
class DifferentiationInput:
    target_asin: str
    reviews: tuple[DiffReview, ...]  # eligible review sample (denominator)
    themes: tuple[RawTheme, ...] = ()
    feature_requests: tuple[FeatureRequest, ...] = ()
    bundle_signals: tuple[BundleSignal, ...] = ()
    competitors_bundle_complement: bool | None = None  # do top-10 bundle it?
    listing_rating_avg: float | None = None  # listing's displayed rating


@dataclass(frozen=True)
class DifferentiationReport:
    pillar_score: float  # 0-100 differentiation pillar
    confidence: DifferentiationConfidence
    complaint_intensity_score: float | None  # F1
    missing_features_score: float | None  # F2
    addressability_score: float | None  # F3
    bundle_packaging_score: float | None  # F4
    themes: tuple[DifferentiationTheme, ...]
    feature_gap_count: int
    verified_supporting_reviews: int
    sample_rating_avg: float | None
    sample_bias_delta: float | None
    sample_bias_flag: bool
    f1_bias_adjustment: float
    components: tuple[Subscore, ...]
    data_gaps: tuple[str, ...]

    @property
    def missing_components(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.components if not s.available)

    @property
    def has_buy_quality_evidence(self) -> bool:
        """True only when there is verified complaint evidence and confidence is
        not LOW — otherwise the report signals insufficient evidence."""
        return self.confidence.level != Confidence.LOW and self.confidence.themes_with_evidence >= 1


# ---------------------------------------------------------------------------
# Risk analysis engine (deduction-based; docs/scoring-model.md §8,
# docs/analysis-engine.md §5)
# ---------------------------------------------------------------------------
class RiskSeverity(StrEnum):
    CRITICAL = "critical"  # deduction ≥ 30
    HIGH = "high"  # deduction ≥ 20
    MODERATE = "moderate"  # deduction ≥ 10
    INFO = "info"  # 0-deduction informational / unassessed


@dataclass(frozen=True)
class RiskRules:
    """External, versioned risk indicators (loaded from risk_data/*.toml)."""

    version: str
    ip_categories: frozenset[str]  # design-patent-heavy categories
    brand_likeness_lexicon: tuple[str, ...]  # trademark-signature terms
    compliance_map: dict[str, str]  # category → certification requirement
    high_return_categories: frozenset[str]
    fragility_materials: tuple[str, ...]  # glass/ceramic/etc.
    oversized_size_tiers: frozenset[str]  # informational logistics note


@dataclass(frozen=True)
class RiskConfig:
    # Deductions — EXACTLY as documented (docs/scoring-model.md §8 / analysis §5).
    deduct_ip: float = 40
    deduct_compliance: float = 30
    deduct_trend: float = 25
    deduct_seasonality_confirmed: float = 20
    deduct_seasonality_unknown: float = 10
    deduct_high_returns: float = 20
    deduct_fragility: float = 20
    deduct_keyword_concentration: float = 15
    deduct_market_concentration: float = 15
    deduct_supplier: float = 10
    # Documented thresholds.
    seasonality_peak_threshold: float = 0.40
    sizing_freq_threshold: float = 0.10
    damage_freq_threshold: float = 0.08
    keyword_share_threshold: float = 0.60
    hhi_threshold: float = 0.30
    trend_history_months: int = 24
    trend_fad_ratio: float = 2.0
    # Severity bands by deduction magnitude.
    severity_critical: float = 30
    severity_high: float = 20
    severity_moderate: float = 10
    # Confidence: high when ≤ 1 documented rule is unassessed (analysis §5).
    max_unassessed_high: int = 1
    max_unassessed_medium: int = 4


@dataclass(frozen=True)
class RiskInput:
    """Facts consumed from upstream engines/product data — never recomputed here.

    seasonality comes from the demand engine, brand_hhi and price_war_flag from
    competition, complaint frequencies from differentiation/review themes, size
    tier from ProductView, keyword_top_share from the keyword cluster."""

    category: str | None = None
    titles: tuple[str, ...] = ()
    patent_marked_listings: bool | None = None  # Analyst flag
    materials: tuple[str, ...] = ()
    has_firmware: bool | None = None
    multi_part: bool | None = None
    sizing_complaint_frequency: float | None = None  # from review themes
    damage_complaint_frequency: float | None = None
    keyword_top_share: float | None = None  # from keyword cluster
    brand_hhi: float | None = None  # from competition
    volume_history_months: int | None = None
    current_volume: int | None = None
    volume_24mo_median: int | None = None
    seasonality: Seasonality | None = None  # from demand engine
    price_war_flag: bool | None = None  # from competition (informational)
    size_tier: str | None = None  # from ProductView (informational)
    oversized: bool | None = None  # informational logistics note


@dataclass(frozen=True)
class RiskFlag:
    risk_type: str
    deduction: float  # points subtracted (≥ 0)
    severity: RiskSeverity
    evidence: str  # concrete citation
    source: str  # the input/rule that triggered it
    explanation: str
    assessed: bool = True  # False = could not be evaluated (missing input)


@dataclass(frozen=True)
class RiskConfidence:
    level: Confidence
    assessed_rules: int
    unassessed_rules: int
    total_rules: int


@dataclass(frozen=True)
class RiskReport:
    risk_score: float  # 0-100, higher = safer (100 − total deductions, floor 0)
    total_deduction: float
    confidence: RiskConfidence
    flags: tuple[RiskFlag, ...]  # triggered + informational, ordered by deduction
    unassessed: tuple[str, ...]  # documented rules that could not be evaluated
    data_gaps: tuple[str, ...]

    @property
    def risk_adjusted_score(self) -> float:
        return self.risk_score

    @property
    def has_critical_risk(self) -> bool:
        return any(f.severity is RiskSeverity.CRITICAL and f.deduction > 0 for f in self.flags)


# ---------------------------------------------------------------------------
# Final opportunity scoring engine (docs/scoring-model.md §2-§10,
# docs/analysis-engine.md §6). Pure assembly of the five pillar reports into
# the composite score, gates, kills, and Buy/Test/Avoid verdict.
# ---------------------------------------------------------------------------
class Verdict(StrEnum):
    BUY = "buy"
    TEST = "test"
    AVOID = "avoid"


class StrategistConcurrence(StrEnum):
    """The resolved G5 (Strategist concurrence) input to scoring (scoring-model
    §10). Deterministic scoring stays the sole verdict owner; this is a *validated*
    concurrence signal (like risk flags or mined themes are LLM-sourced inputs),
    not the LLM setting a verdict. G5 is a BUY gate: it can only BLOCK a would-be
    Buy, never manufacture one or change a non-Buy verdict.

    - PENDING: not evaluated (default) — a provisional Buy is allowed, marked
      `strategist_pending` (the agents-off / pre-Strategist baseline).
    - CONCUR: Strategist ran and returned `buy` with the required register →
      G5 passes, a qualifying Buy is confirmed.
    - DISSENT: Strategist ran and did NOT concur with `buy` → G5 not met, a
      would-be Buy is capped at Test and the disagreement is surfaced.
    - UNAVAILABLE: Strategist could not run (error/degraded) → G5 unmet, a
      would-be Buy is capped at Test (no Buy without a Strategist review)."""

    PENDING = "pending"
    CONCUR = "concur"
    DISSENT = "dissent"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class KillResult:
    """One Stage-0 hard-rejection rule (scoring-model §2). `triggered` and not
    `demoted` forces AVOID; `demoted` (within the borderline band) caps at TEST;
    `assessed=False` means the inputs were absent so the rule could not run."""

    rule_id: str  # 'K1'..'K12'
    name: str
    triggered: bool
    demoted: bool  # borderline (within band of threshold) → Test, not kill
    category: str  # 'price' | 'logistics' | 'brand' | 'moat' | 'ip' | ...
    actual: str | None  # human-readable observed value
    threshold: str | None  # human-readable limit
    evidence: str
    reason: str
    assessed: bool = True

    @property
    def kills(self) -> bool:
        """True only for an assessed, triggered, non-demoted rule (forces AVOID)."""
        return self.assessed and self.triggered and not self.demoted


@dataclass(frozen=True)
class GateResult:
    """One Stage-4 gate (scoring-model §10). A high score cannot buy past a gate.
    Hard gates (G1/G3) failing → AVOID; soft gates (G2/G4) failing → block Buy
    (demote to Test). G5 (Strategist) is `passed=None` — pending, checked by the
    pipeline, never by scoring."""

    gate_id: str  # 'G1'..'G5'
    name: str
    passed: bool | None  # None = pending (G5 strategist concurrence)
    hard: bool  # hard gate failure forces AVOID; soft only blocks Buy
    actual: str | None
    threshold: str | None
    reason: str


@dataclass(frozen=True)
class PillarScore:
    """One of the five weighted pillars, with full provenance so the composite
    is hand-auditable: raw report score → sufficiency cap → weighted share."""

    pillar: str  # 'demand' | 'competition' | 'differentiation' | 'profitability' | 'risk'
    raw_score: float | None  # 0-100 from the underlying report (pre-cap)
    capped_score: float | None  # after any sufficiency/gate cap
    weight: float  # configured pillar weight
    weighted_contribution: float  # capped_score × weight / Σ weights
    confidence: Confidence
    available: bool  # False = report absent
    partial: bool  # True = a sufficiency cap applied (blocks Buy via G2)
    cap_reason: str | None
    source: str  # which report produced it
    components: tuple[Subscore, ...]  # pass-through component provenance
    evidence: str


@dataclass(frozen=True)
class ScoreConfidence:
    level: Confidence
    partial_pillars: tuple[str, ...]
    missing_pillars: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ConfigSnapshot:
    """Frozen copy of every threshold/weight that decided a score, so an old
    score stays reproducible after config drift (scoring-model §11.3). Plain
    numbers only — no config import, no DB dependency; the persistence layer
    serializes this verbatim."""

    weights: tuple[tuple[str, float], ...]
    gate_thresholds: tuple[tuple[str, float], ...]
    kill_thresholds: tuple[tuple[str, float], ...]
    verdict_thresholds: tuple[tuple[str, float], ...]
    sufficiency: tuple[tuple[str, float], ...]
    profit_pillar: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class ScoringInput:
    """Everything scoring assembles: the five pillar reports plus the cheap kill
    facts that aren't owned by any pillar. Absent fields are treated
    pessimistically (never optimistically) — a missing kill fact means the rule
    is `unassessed`, a missing pillar makes the candidate insufficient."""

    demand: DemandReport | None = None
    competition: CompetitionReport | None = None
    differentiation: DifferentiationReport | None = None
    profit: ScenarioSet | None = None
    risk: RiskReport | None = None
    # Cheap Stage-0 kill facts (scoring-model §2) not derivable from a pillar.
    market_median_price_cents: int | None = None  # K1/K2
    oversized: bool | None = None  # K3
    amazon_in_top5: bool | None = None  # K4
    market_complaint_rate: float | None = None  # K7 (with listing quality)
    restricted_category: bool | None = None  # K8
    ip_signature: bool | None = None  # K9
    avoid_matches: tuple[str, ...] = ()  # K12 config `avoid` list matches
    fad_search_volume: int | None = None  # K10
    fad_volume_12mo_median: int | None = None  # K10
    volume_history_months: int | None = None  # K10
    top_n: int = 10


@dataclass(frozen=True)
class ScoredOpportunity:
    """The persisted, self-contained result — everything needed to regenerate
    the report and re-derive the verdict without re-running the engines."""

    verdict: Verdict
    score: float  # composite 0-100 (provisional pending G5 when verdict == BUY)
    base_weighted_score: float  # composite before any documented top-level caps
    pillars: tuple[PillarScore, ...]
    kills: tuple[KillResult, ...]
    gates: tuple[GateResult, ...]
    confidence: ScoreConfidence
    insufficient_data: bool  # any pillar partial/absent → research-later, not bad
    strategist_pending: bool  # G5 unresolved → a BUY here is provisional
    verdict_basis: tuple[str, ...]  # which thresholds/gates/kills decided it
    config_snapshot: ConfigSnapshot

    @property
    def hard_kill_triggered(self) -> bool:
        return any(k.kills for k in self.kills)

    @property
    def failed_gates(self) -> tuple[str, ...]:
        return tuple(g.gate_id for g in self.gates if g.passed is False)

    @property
    def bad_opportunity(self) -> bool:
        """AVOID for a substantive reason (kill / hard gate / low score) rather
        than merely thin evidence — distinct from `insufficient_data`."""
        return self.verdict is Verdict.AVOID and not self.insufficient_data


# ---------------------------------------------------------------------------
# Cross-market discovery engine (docs/cross-market.md). Detects products proven
# in one Amazon marketplace that look underpenetrated but credibly demanded in
# another. A discovery SIGNAL — NOT one of the five opportunity pillars, and
# never a second BUY/TEST/AVOID system. Pure types only.
# ---------------------------------------------------------------------------
class Marketplace(StrEnum):
    """Amazon marketplaces. Values match the `marketplace` strings already used
    across ingestion/DB. Add entries here and in `analysis.marketplaces` — the
    engine never hardcodes a marketplace."""

    US = "US"
    CA = "CA"
    UK = "UK"
    AU = "AU"
    IN = "IN"


@dataclass(frozen=True)
class MarketplaceInfo:
    """Static reference data for one marketplace (centralized in
    `analysis.marketplaces`). Not user-tunable config — reference facts."""

    code: Marketplace
    country: str
    currency: str
    locale: str
    domain: str
    marketplace_id: str
    unit_system: str  # 'imperial' | 'metric'
    language: str


class MatchConfidence(StrEnum):
    EXACT = "exact"  # GTIN/UPC/EAN agree
    STRONG = "strong"
    PROBABLE = "probable"
    WEAK = "weak"
    UNMATCHED = "unmatched"


@dataclass(frozen=True)
class MarketplaceProduct:
    """A product's identity + coarse snapshot in ONE marketplace. ASINs are
    marketplace-specific, so identity is matched on observable signals, never
    assumed equal across marketplaces."""

    marketplace: Marketplace
    asin: str
    title: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    gtin: str | None = None  # UPC/EAN/GTIN when known — the only exact key
    dims: Dimensions | None = None
    weight_g: int | None = None
    category_path: str | None = None
    price_cents: int | None = None
    generic: bool | None = None  # private-label/generic → brand mismatch tolerated


@dataclass(frozen=True)
class ProductMatch:
    source: MarketplaceProduct
    target: MarketplaceProduct
    confidence: MatchConfidence
    score: float  # 0-1 fuzzy identity score (1.0 for GTIN-exact)
    signals_used: tuple[str, ...]
    conflicting_signals: tuple[str, ...]
    detail: str

    @property
    def matched(self) -> bool:
        return self.confidence is not MatchConfidence.UNMATCHED


class SourceMaturity(StrEnum):
    INSUFFICIENT = "insufficient"  # too little evidence to classify
    EMERGING = "emerging"
    VALIDATED = "validated"
    STRONG = "strong"
    EXCEPTIONAL = "exceptional"


@dataclass(frozen=True)
class SourceMarketInput:
    """Normalized source-market facts fed from demand/competition/scoring. Every
    field optional; absence lowers confidence and is never guessed."""

    monthly_units: int | None = None  # median top-10 est. velocity (demand)
    keyword_volume: int | None = None  # primary cluster volume (demand)
    keyword_growth: float | None = None  # YoY (demand D4)
    history_months: int | None = None  # length of demand history
    review_count: float | None = None  # median top-10 reviews (competition C1)
    review_velocity: float | None = None  # leaders' new reviews/mo (competition C3)
    competition_score: float | None = None  # competition pillar (higher = weaker)
    opportunity_score: float | None = None  # scoring.py composite, if available


@dataclass(frozen=True)
class SourceMarketEvidence:
    marketplace: Marketplace
    source_success_score: float  # 0-100
    maturity: SourceMaturity
    confidence: Confidence
    components: tuple[Subscore, ...]
    reasons: tuple[str, ...]
    signals_present: int


class TargetPresence(StrEnum):
    UNKNOWN = "unknown"  # insufficient data
    NOT_PRESENT = "not_present"  # no credible matching listing
    EARLY = "early"  # some presence, immature
    UNDERPENETRATED = "underpenetrated"  # exists but weak vs demand
    MATURE = "mature"  # established
    SATURATED = "saturated"  # strong demand AND strong incumbents


@dataclass(frozen=True)
class TargetMarketInput:
    """Normalized target-market facts. `listings_found is None` = not looked up
    (UNKNOWN); `listings_found == 0` = looked up, none found (NOT_PRESENT).
    These are NOT the same, and neither implies opportunity."""

    listings_found: int | None = None
    median_reviews: float | None = None  # competition C1 input
    avg_listing_quality: float | None = None  # 0-100 (listing engine)
    beatable_slots: int | None = None  # competition C2 input
    brand_hhi: float | None = None  # competition C4 input
    keyword_volume: int | None = None  # target demand (DataForSEO)
    keyword_growth: float | None = None
    serp_presence: bool | None = None  # any organic SERP results for the cluster
    review_velocity: float | None = None


@dataclass(frozen=True)
class TargetMarketEvidence:
    marketplace: Marketplace
    presence: TargetPresence
    target_demand_score: float  # 0-100
    demand_credible: bool  # >= configured credibility floor
    competition_weakness_score: float  # 0-100 (higher = weaker/easier target)
    target_maturity_score: float  # 0-100 (higher = more established)
    confidence: Confidence
    components: tuple[Subscore, ...]
    reasons: tuple[str, ...]
    signals_present: int


@dataclass(frozen=True)
class MarketGap:
    maturity_gap: float  # 0-100 (source_success − target_maturity, floored 0)
    competition_gap: float  # 0-100 (how much easier the target looks vs source)
    demand_gap: float | None  # target demand relative to source demand, if both known
    detail: str


class TransferabilityLevel(StrEnum):
    FAVORABLE = "favorable"
    UNCERTAIN = "uncertain"
    UNFAVORABLE = "unfavorable"


@dataclass(frozen=True)
class TransferabilityInput:
    """Observable/structured transfer signals — never cultural speculation."""

    category_compatible: bool | None = None
    oversized: bool | None = None  # logistics
    price_positioning_ok: bool | None = None  # within target band
    compliance_risk: bool | None = None  # regulatory surface in target
    seasonality_concentration: float | None = None  # peak-8-week share (demand D5)
    electrical_or_plug_dependent: bool | None = None
    unit_system_differs: bool = False
    language_differs: bool = False
    keyword_localization_needed: bool | None = None
    surfaced_risk_flags: tuple[str, ...] = ()  # from risk engine — surfaced, not recomputed


@dataclass(frozen=True)
class TransferabilityFactor:
    name: str
    level: TransferabilityLevel
    evidence: str


@dataclass(frozen=True)
class LocalizationFlag:
    kind: str
    evidence: str


@dataclass(frozen=True)
class Transferability:
    level: TransferabilityLevel
    score: float  # 0-100
    factors: tuple[TransferabilityFactor, ...]
    localization_flags: tuple[LocalizationFlag, ...]
    surfaced_risk_flags: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class CrossMarketComponent:
    name: str
    raw: str  # human-readable raw input
    normalized: float  # 0-100
    weight: float
    weighted_contribution: float
    evidence: str
    confidence: Confidence


class CrossMarketVerdict(StrEnum):
    STRONG_OPPORTUNITY = "strong_opportunity"
    OPPORTUNITY_TO_VALIDATE = "opportunity_to_validate"
    MATURE_MARKET = "mature_market"
    WEAK_TRANSFER = "weak_transfer"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class CrossMarketConfidence:
    level: Confidence
    match_confidence: MatchConfidence
    source_confidence: Confidence
    target_confidence: Confidence
    notes: tuple[str, ...]


@dataclass(frozen=True)
class CrossMarketReport:
    """One source → one target cross-market assessment. A discovery signal for
    later validation — deliberately cautious, never a promise of sales."""

    source_marketplace: Marketplace
    target_marketplace: Marketplace
    verdict: CrossMarketVerdict
    score: float  # 0-100 attractiveness of investigating this transfer
    base_score: float  # weighted component score before risk penalties/caps
    match: ProductMatch
    source_evidence: SourceMarketEvidence
    target_evidence: TargetMarketEvidence
    market_gap: MarketGap
    transferability: Transferability
    components: tuple[CrossMarketComponent, ...]
    confidence: CrossMarketConfidence
    risk_penalty: float
    summary: tuple[str, ...]  # cautious, evidence-based rationale lines
    weights_snapshot: tuple[tuple[str, float], ...]  # component weights used

    @property
    def is_opportunity(self) -> bool:
        return self.verdict in (
            CrossMarketVerdict.STRONG_OPPORTUNITY,
            CrossMarketVerdict.OPPORTUNITY_TO_VALIDATE,
        )
