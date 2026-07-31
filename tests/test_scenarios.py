"""Scenario engine tests."""

from __future__ import annotations

from delium.analysis.fees import load_fee_table
from delium.analysis.models import (
    Confidence,
    Dimensions,
    ProfitInputs,
    ScenarioAssumptions,
)
from delium.analysis.profit import compute_scenarios

TABLE = load_fee_table()
DIMS = Dimensions(200, 150, 15)

BASE = ProfitInputs(
    selling_price_cents=2499,
    product_cost_cents=500,
    freight_cents=90,
    customs_cents=30,
    prep_cost_cents=50,
    ppc_percent=0.15,
    return_rate=0.05,
    monthly_sales_units=400,
)


def _scenarios(**kwargs: object):
    return compute_scenarios(
        TABLE, category="Home & Kitchen", dims=DIMS, weight_g=300, base_inputs=BASE, **kwargs
    )


def test_four_scenarios_ordered_by_profit() -> None:
    s = _scenarios()
    assert (
        s.optimistic.net_profit_cents
        > s.expected.net_profit_cents
        > s.stressed.net_profit_cents
        > s.worst_case.net_profit_cents
    )


def test_as_dict_exposes_all_scenarios() -> None:
    mapping = _scenarios().as_dict()
    assert set(mapping) == {"optimistic", "expected", "stressed", "worst_case"}
    assert mapping["expected"].inputs.selling_price_cents == 2499


def test_expected_matches_base_inputs() -> None:
    s = _scenarios()
    assert s.expected.inputs.selling_price_cents == 2499
    assert s.expected.inputs.product_cost_cents == 500


def test_optimistic_lowers_cost_and_ppc() -> None:
    s = _scenarios()
    assert s.optimistic.inputs.product_cost_cents < 500  # cost_mult 0.85
    assert s.optimistic.inputs.ppc_percent < 0.15  # ppc_delta -0.03


def test_stressed_raises_cost_and_drops_price() -> None:
    s = _scenarios()
    assert s.stressed.inputs.selling_price_cents < 2499  # price_mult 0.90
    assert s.stressed.inputs.product_cost_cents > 500  # cost_mult 1.15


def test_referral_recomputed_per_scenario_price() -> None:
    # Referral is 15% of price, so a lower stressed price → lower referral.
    s = _scenarios()
    assert s.stressed.fees.referral_cents < s.expected.fees.referral_cents


def test_confidence_propagates_from_base() -> None:
    s = compute_scenarios(
        TABLE,
        category="Home & Kitchen",
        dims=DIMS,
        weight_g=300,
        base_inputs=ProfitInputs(
            **{**BASE.__dict__, "estimated_fields": frozenset({"product_cost"})}
        ),
    )
    assert s.confidence == Confidence.MEDIUM


def test_custom_assumptions_respected() -> None:
    flat = ScenarioAssumptions(
        optimistic=ScenarioAssumptions.default().expected,
        expected=ScenarioAssumptions.default().expected,
        stressed=ScenarioAssumptions.default().expected,
        worst_case=ScenarioAssumptions.default().expected,
    )
    s = _scenarios(assumptions=flat)
    # All identical adjustments → identical net profit.
    assert (
        s.optimistic.net_profit_cents
        == s.expected.net_profit_cents
        == s.stressed.net_profit_cents
        == s.worst_case.net_profit_cents
    )
