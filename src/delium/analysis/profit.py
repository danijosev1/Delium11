"""Deterministic profit engine: unit economics, scenarios, and confidence.

Pure computation over `ProfitInputs` + a `FeeBreakdown`. No LLM, no I/O. All
money is integer cents; margins/ROI/break-even are floats. Definitions are
documented on each computed field below. See docs/analysis-engine.md §4.2.
"""

from __future__ import annotations

from dataclasses import replace

from delium.analysis.fees import compute_fees
from delium.analysis.models import (
    ASSUMPTION_FIELDS,
    Confidence,
    Dimensions,
    FeeBreakdown,
    FeeTable,
    LaunchAssumptions,
    ProfitInputs,
    ProfitResult,
    ScenarioAdjustment,
    ScenarioAssumptions,
    ScenarioSet,
)

# Estimated-field name → the input it maps to (for confidence + flags).
_CORE_ASSUMPTIONS = frozenset({"product_cost", "freight"})
_SOFT_ASSUMPTIONS = ASSUMPTION_FIELDS - _CORE_ASSUMPTIONS


def confidence_for(estimated_fields: frozenset[str]) -> Confidence:
    """Confidence from how many assumptions are estimated vs. known.

    HIGH  — the cost drivers (product_cost, freight) are known and at most one
            soft assumption is estimated (essentially quote-backed).
    LOW   — a cost driver is estimated AND almost everything is a guess.
    MEDIUM — everything in between (the "all-assumption but plausible" band).
    """
    core_estimated = len(estimated_fields & _CORE_ASSUMPTIONS)
    soft_estimated = len(estimated_fields & _SOFT_ASSUMPTIONS)

    if core_estimated == 0 and soft_estimated <= 1:
        return Confidence.HIGH
    if core_estimated <= 1 and (core_estimated + soft_estimated) <= 4:
        return Confidence.MEDIUM
    return Confidence.LOW


def compute_profit(
    inputs: ProfitInputs,
    fees: FeeBreakdown,
    launch: LaunchAssumptions | None = None,
) -> ProfitResult:
    launch = launch or LaunchAssumptions()
    price = inputs.selling_price_cents

    # Landed cost: what it takes to get one sellable unit into FBA.
    landed = (
        inputs.product_cost_cents
        + inputs.freight_cents
        + inputs.customs_cents
        + inputs.prep_cost_cents
    )
    amazon = fees.amazon_fees_cents

    # Variable selling costs.
    ppc = round(inputs.ppc_percent * price)
    # Expected return loss per unit sold: refund exposure + lost fulfillment.
    returns = round(inputs.return_rate * (price * 0.5 + fees.fulfillment_cents))

    revenue = price
    gross_profit = revenue - landed  # classic gross profit (before platform costs)
    contribution = revenue - landed - amazon - returns  # before PPC
    net = contribution - ppc

    gross_margin = gross_profit / revenue if revenue > 0 else 0.0
    net_margin = net / revenue if revenue > 0 else 0.0
    roi = net / landed if landed > 0 else 0.0
    # Max PPC fraction of revenue at which net profit hits zero. Clamp to [0, 1].
    break_even_ppc = min(1.0, max(0.0, contribution / revenue)) if revenue > 0 else 0.0

    units = inputs.monthly_sales_units
    monthly_revenue = revenue * units
    monthly_net = net * units
    # Ongoing monthly cash to run: one month of restock + one month of ad spend.
    monthly_cash = (landed + ppc) * units
    # Upfront launch capital: inventory depth + PPC ramp + fixed launch costs.
    launch_capital = round(landed * units * launch.inventory_months) + (
        launch.ppc_ramp_cents + launch.fixed_launch_cents
    )
    payback = (launch_capital / monthly_net) if monthly_net > 0 else None

    return ProfitResult(
        revenue_cents=revenue,
        landed_cost_cents=landed,
        amazon_fees_cents=amazon,
        ppc_cost_cents=ppc,
        returns_cost_cents=returns,
        gross_profit_cents=gross_profit,
        contribution_margin_cents=contribution,
        net_profit_cents=net,
        gross_margin=gross_margin,
        net_margin=net_margin,
        roi=roi,
        break_even_ppc=break_even_ppc,
        monthly_revenue_cents=monthly_revenue,
        monthly_net_profit_cents=monthly_net,
        monthly_cash_requirement_cents=monthly_cash,
        launch_capital_cents=launch_capital,
        payback_months=payback,
        confidence=confidence_for(inputs.estimated_fields),
        assumption_flags=tuple(sorted(inputs.estimated_fields & ASSUMPTION_FIELDS)),
        fees=fees,
        inputs=inputs,
    )


# ---------------------------------------------------------------------------
# Scenario engine
# ---------------------------------------------------------------------------
def _adjust_inputs(base: ProfitInputs, adj: ScenarioAdjustment) -> ProfitInputs:
    return replace(
        base,
        selling_price_cents=round(base.selling_price_cents * adj.price_mult),
        product_cost_cents=round(base.product_cost_cents * adj.cost_mult),
        ppc_percent=max(0.0, base.ppc_percent + adj.ppc_delta),
        return_rate=min(1.0, max(0.0, base.return_rate * adj.return_mult)),
    )


def compute_scenarios(
    table: FeeTable,
    *,
    category: str | None,
    dims: Dimensions | None,
    weight_g: int | None,
    base_inputs: ProfitInputs,
    launch: LaunchAssumptions | None = None,
    assumptions: ScenarioAssumptions | None = None,
) -> ScenarioSet:
    """Compute optimistic / expected / stressed / worst-case profit.

    Fees are recomputed per scenario because the referral fee scales with the
    adjusted price (fulfillment/storage depend on size/weight, which are fixed).
    """
    launch = launch or LaunchAssumptions()
    assumptions = assumptions or ScenarioAssumptions.default()

    def run(adj: ScenarioAdjustment) -> ProfitResult:
        inputs = _adjust_inputs(base_inputs, adj)
        fees = compute_fees(
            table,
            category=category,
            price_cents=inputs.selling_price_cents,
            dims=dims,
            weight_g=weight_g,
            prep_cost_cents=inputs.prep_cost_cents,
        )
        return compute_profit(inputs, fees, launch)

    return ScenarioSet(
        optimistic=run(assumptions.optimistic),
        expected=run(assumptions.expected),
        stressed=run(assumptions.stressed),
        worst_case=run(assumptions.worst_case),
        confidence=confidence_for(base_inputs.estimated_fields),
    )
