"""Profit engine unit tests with a fully worked fixture."""

from __future__ import annotations

import pytest

from delium.analysis.fees import compute_fees, load_fee_table
from delium.analysis.models import (
    Confidence,
    Dimensions,
    LaunchAssumptions,
    ProfitInputs,
)
from delium.analysis.profit import compute_profit, confidence_for

TABLE = load_fee_table()
DIMS = Dimensions(200, 150, 15)


def _fees(price_cents: int = 2499):
    return compute_fees(
        TABLE, category="Home & Kitchen", price_cents=price_cents, dims=DIMS, weight_g=300
    )


def test_worked_example_matches_hand_calculation() -> None:
    inputs = ProfitInputs(
        selling_price_cents=2499,
        product_cost_cents=420,
        freight_cents=90,
        customs_cents=30,
        prep_cost_cents=50,
        ppc_percent=0.15,
        return_rate=0.05,
        monthly_sales_units=400,
    )
    fees = _fees()
    # Locks the fee inputs the profit math builds on.
    assert fees.amazon_fees_cents == 711  # 375 + 334 + 0 + 2 storage

    r = compute_profit(inputs, fees, LaunchAssumptions())

    assert r.revenue_cents == 2499
    assert r.landed_cost_cents == 590
    assert r.amazon_fees_cents == 711
    assert r.ppc_cost_cents == 375
    assert r.returns_cost_cents == 79
    assert r.gross_profit_cents == 1909
    assert r.contribution_margin_cents == 1119
    assert r.net_profit_cents == 744
    assert r.gross_margin == pytest.approx(1909 / 2499)
    assert r.net_margin == pytest.approx(744 / 2499)
    assert r.roi == pytest.approx(744 / 590)
    assert r.break_even_ppc == pytest.approx(1119 / 2499)
    assert r.monthly_revenue_cents == 999_600
    assert r.monthly_net_profit_cents == 297_600
    assert r.monthly_cash_requirement_cents == 386_000
    assert r.launch_capital_cents == 940_000
    assert r.payback_months == pytest.approx(940_000 / 297_600)
    assert r.confidence == Confidence.HIGH
    assert r.assumption_flags == ()


def test_negative_profit_is_handled() -> None:
    inputs = ProfitInputs(
        selling_price_cents=1500,
        product_cost_cents=1200,
        freight_cents=200,
        customs_cents=50,
        prep_cost_cents=50,
        ppc_percent=0.20,
        return_rate=0.10,
        monthly_sales_units=100,
    )
    r = compute_profit(inputs, _fees(1500))

    assert r.net_profit_cents < 0
    assert r.net_margin < 0
    assert r.roi < 0
    assert r.payback_months is None  # never recouped
    assert r.break_even_ppc == 0.0  # contribution negative → clamp
    assert r.gross_margin <= 1.0


def test_break_even_ppc_zeroes_net_profit() -> None:
    inputs = ProfitInputs(
        selling_price_cents=3000,
        product_cost_cents=500,
        freight_cents=100,
        customs_cents=0,
        prep_cost_cents=50,
        ppc_percent=0.0,  # will substitute break-even below
        return_rate=0.05,
        monthly_sales_units=300,
    )
    fees = _fees(3000)
    base = compute_profit(inputs, fees)
    # Re-run with PPC set to the reported break-even → net profit ≈ 0.
    at_be = compute_profit(
        ProfitInputs(**{**inputs.__dict__, "ppc_percent": base.break_even_ppc}), fees
    )
    assert at_be.net_profit_cents == pytest.approx(0, abs=2)


def test_assumption_flags_and_confidence() -> None:
    inputs = ProfitInputs(
        selling_price_cents=2499,
        product_cost_cents=420,
        freight_cents=90,
        customs_cents=30,
        prep_cost_cents=50,
        ppc_percent=0.15,
        return_rate=0.05,
        monthly_sales_units=400,
        estimated_fields=frozenset({"product_cost", "ppc"}),
    )
    r = compute_profit(inputs, _fees())
    assert r.assumption_flags == ("ppc", "product_cost")
    assert r.confidence == Confidence.MEDIUM


# --- confidence_for mapping ----------------------------------------------
def test_confidence_high_when_all_known() -> None:
    assert confidence_for(frozenset()) == Confidence.HIGH


def test_confidence_high_with_one_soft_estimate() -> None:
    assert confidence_for(frozenset({"ppc"})) == Confidence.HIGH


def test_confidence_medium_when_core_estimated() -> None:
    assert confidence_for(frozenset({"product_cost"})) == Confidence.MEDIUM
    assert confidence_for(frozenset({"freight", "ppc", "return_rate"})) == Confidence.MEDIUM


def test_confidence_low_when_mostly_estimated() -> None:
    all_fields = frozenset({"product_cost", "freight", "customs", "prep", "ppc", "return_rate"})
    assert confidence_for(all_fields) == Confidence.LOW
