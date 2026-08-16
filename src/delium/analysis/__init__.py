"""Deterministic analysis engine (docs/analysis-engine.md).

Pure, reproducible computation — no LLM involvement, ever. This package must
never import `delium.agents`.

Implemented: the profit engine (fees + profit + scenarios). Not yet: demand,
competition, differentiation, risk, and scoring.
"""

from delium.analysis.demand import DemandError, analyze_demand, load_velocity_curves
from delium.analysis.fees import FeeError, compute_fees, load_fee_table
from delium.analysis.listing import compute_listing_quality
from delium.analysis.models import (
    AsinHistory,
    BsrPoint,
    BsrTrend,
    Confidence,
    DemandConfig,
    DemandReport,
    Dimensions,
    FeeBreakdown,
    FeeTable,
    KeywordDatum,
    KeywordDemand,
    LaunchAssumptions,
    ListingInput,
    ListingQualityReport,
    ProfitInputs,
    ProfitResult,
    SalesEstimate,
    ScenarioAdjustment,
    ScenarioAssumptions,
    ScenarioSet,
    Seasonality,
    Subscore,
    VelocityCurves,
)
from delium.analysis.profit import compute_profit, compute_scenarios, confidence_for

__all__ = [
    "AsinHistory",
    "BsrPoint",
    "BsrTrend",
    "Confidence",
    "DemandConfig",
    "DemandError",
    "DemandReport",
    "Dimensions",
    "FeeBreakdown",
    "FeeError",
    "FeeTable",
    "KeywordDatum",
    "KeywordDemand",
    "LaunchAssumptions",
    "ListingInput",
    "ListingQualityReport",
    "ProfitInputs",
    "ProfitResult",
    "SalesEstimate",
    "ScenarioAdjustment",
    "ScenarioAssumptions",
    "ScenarioSet",
    "Seasonality",
    "Subscore",
    "VelocityCurves",
    "analyze_demand",
    "compute_fees",
    "compute_listing_quality",
    "compute_profit",
    "compute_scenarios",
    "confidence_for",
    "load_fee_table",
    "load_velocity_curves",
]
