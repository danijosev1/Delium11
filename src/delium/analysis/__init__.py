"""Deterministic analysis engine (docs/analysis-engine.md).

Pure, reproducible computation — no LLM involvement, ever. This package must
never import `delium.agents`.

Implemented: the profit engine (fees + profit + scenarios). Not yet: demand,
competition, differentiation, risk, and scoring.
"""

from delium.analysis.fees import FeeError, compute_fees, load_fee_table
from delium.analysis.listing import compute_listing_quality
from delium.analysis.models import (
    Confidence,
    Dimensions,
    FeeBreakdown,
    FeeTable,
    LaunchAssumptions,
    ListingInput,
    ListingQualityReport,
    ProfitInputs,
    ProfitResult,
    ScenarioAdjustment,
    ScenarioAssumptions,
    ScenarioSet,
    Subscore,
)
from delium.analysis.profit import compute_profit, compute_scenarios, confidence_for

__all__ = [
    "Confidence",
    "Dimensions",
    "FeeBreakdown",
    "FeeError",
    "FeeTable",
    "LaunchAssumptions",
    "ListingInput",
    "ListingQualityReport",
    "ProfitInputs",
    "ProfitResult",
    "ScenarioAdjustment",
    "ScenarioAssumptions",
    "ScenarioSet",
    "Subscore",
    "compute_fees",
    "compute_listing_quality",
    "compute_profit",
    "compute_scenarios",
    "confidence_for",
    "load_fee_table",
]
