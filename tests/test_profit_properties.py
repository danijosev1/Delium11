"""Property-based tests for the profit engine (hypothesis)."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from delium.analysis.fees import compute_fees, load_fee_table
from delium.analysis.models import Dimensions, ProfitInputs
from delium.analysis.profit import compute_profit

TABLE = load_fee_table()
DIMS = Dimensions(200, 150, 15)


def _fees(price_cents: int):
    return compute_fees(
        TABLE, category="Home & Kitchen", price_cents=price_cents, dims=DIMS, weight_g=300
    )


def _inputs(price: int, cost: int, ppc: float, ret: float) -> ProfitInputs:
    return ProfitInputs(
        selling_price_cents=price,
        product_cost_cents=cost,
        freight_cents=90,
        customs_cents=30,
        prep_cost_cents=50,
        ppc_percent=ppc,
        return_rate=ret,
        monthly_sales_units=300,
    )


prices = st.integers(min_value=500, max_value=20_000)
costs = st.integers(min_value=50, max_value=4_000)
ppcs = st.floats(min_value=0.0, max_value=0.4)
rets = st.floats(min_value=0.0, max_value=0.3)


@given(price=prices, cost=costs, ppc=ppcs, ret=rets)
def test_margins_never_exceed_100_percent(price: int, cost: int, ppc: float, ret: float) -> None:
    r = compute_profit(_inputs(price, cost, ppc, ret), _fees(price))
    assert r.gross_margin <= 1.0
    assert r.net_margin <= 1.0


@given(price=prices, cost=costs, ppc=ppcs, ret=rets)
def test_break_even_ppc_is_a_sensible_fraction(
    price: int, cost: int, ppc: float, ret: float
) -> None:
    r = compute_profit(_inputs(price, cost, ppc, ret), _fees(price))
    assert 0.0 <= r.break_even_ppc <= 1.0


@given(price=prices, cost=costs, ppc=ppcs, ret=rets)
def test_negative_profit_has_no_payback(price: int, cost: int, ppc: float, ret: float) -> None:
    r = compute_profit(_inputs(price, cost, ppc, ret), _fees(price))
    if r.net_profit_cents <= 0:
        assert r.payback_months is None
    else:
        assert r.payback_months is not None and r.payback_months > 0


@given(
    price=st.integers(min_value=2_000, max_value=20_000),
    cost=st.integers(min_value=200, max_value=3_000),
    ppc=st.floats(min_value=0.0, max_value=0.2),
    ret=st.floats(min_value=0.0, max_value=0.1),
)
def test_roi_increases_as_cogs_decreases(price: int, cost: int, ppc: float, ret: float) -> None:
    higher = compute_profit(_inputs(price, cost, ppc, ret), _fees(price))
    lower = compute_profit(_inputs(price, cost - 100, ppc, ret), _fees(price))
    # Cheaper COGS → strictly better ROI (net up, capital down).
    assert lower.roi > higher.roi
