"""Typed data structures for the deterministic profit engine.

Pure types only — no calculation, no I/O, no config or LLM imports. Money is
carried as integer **cents** to avoid floating-point drift; ratios (margins,
ROI, break-even PPC) are floats. See docs/analysis-engine.md §4.
"""

from __future__ import annotations

from dataclasses import dataclass
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
