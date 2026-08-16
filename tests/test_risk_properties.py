"""Property-based tests for the risk engine (hypothesis)."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from delium.analysis.models import RiskInput, Seasonality
from delium.analysis.risk import analyze_risk, load_risk_rules

RULES = load_risk_rules()

_CATEGORIES = st.one_of(
    st.none(),
    st.sampled_from(["Home & Kitchen", "Toys & Games", "Baby", "Electronics", "Unlisted"]),
)
_freq = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))
_share = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))
_hhi = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))
_optbool = st.one_of(st.none(), st.booleans())
_months = st.one_of(st.none(), st.integers(min_value=0, max_value=60))


@st.composite
def risk_inputs(draw: st.DrawFn) -> RiskInput:  # type: ignore[type-arg]
    season_choice = draw(st.integers(0, 2))
    if season_choice == 0:
        season = None
    elif season_choice == 1:
        season = Seasonality(False, None, None, None, draw(st.integers(0, 10)))
    else:
        peak = draw(st.floats(0.0, 1.0))
        season = Seasonality(True, peak, peak > 0.4, 50.0, 52)
    return RiskInput(
        category=draw(_CATEGORIES),
        materials=tuple(
            draw(st.lists(st.sampled_from(["glass", "silicone", "steel"]), max_size=3))
        ),
        has_firmware=draw(_optbool),
        multi_part=draw(_optbool),
        patent_marked_listings=draw(_optbool),
        sizing_complaint_frequency=draw(_freq),
        damage_complaint_frequency=draw(_freq),
        keyword_top_share=draw(_share),
        brand_hhi=draw(_hhi),
        volume_history_months=draw(_months),
        seasonality=season,
        price_war_flag=draw(_optbool),
    )


@given(data=risk_inputs())
def test_score_within_bounds(data: RiskInput) -> None:
    r = analyze_risk(data, RULES)
    assert 0.0 <= r.risk_score <= 100.0


@given(data=risk_inputs())
def test_deductions_never_negative(data: RiskInput) -> None:
    r = analyze_risk(data, RULES)
    assert r.total_deduction >= 0.0
    for f in r.flags:
        assert f.deduction >= 0.0


@given(data=risk_inputs())
def test_identical_inputs_identical_outputs(data: RiskInput) -> None:
    assert analyze_risk(data, RULES) == analyze_risk(data, RULES)


@given(data=risk_inputs())
def test_every_nonzero_deduction_has_evidence(data: RiskInput) -> None:
    r = analyze_risk(data, RULES)
    for f in r.flags:
        if f.deduction > 0:
            assert f.evidence.strip() and f.source.strip()


@given(data=risk_inputs())
def test_removing_a_condition_cannot_increase_risk(data: RiskInput) -> None:
    # Clearing the HHI condition (set to a safe value / None) cannot raise the
    # total deduction relative to a high-HHI version.
    risky = analyze_risk(RiskInput(**{**data.__dict__, "brand_hhi": 0.9}), RULES).total_deduction
    safe = analyze_risk(RiskInput(**{**data.__dict__, "brand_hhi": 0.0}), RULES).total_deduction
    assert safe <= risky


@given(data=risk_inputs())
def test_unknown_seasonality_never_exceeds_confirmed(data: RiskInput) -> None:
    unknown = analyze_risk(
        RiskInput(**{**data.__dict__, "seasonality": Seasonality(False, None, None, None, 5)}),
        RULES,
    )
    confirmed = analyze_risk(
        RiskInput(**{**data.__dict__, "seasonality": Seasonality(True, 0.9, True, 5.0, 52)}),
        RULES,
    )
    unknown_ded = next(
        (f.deduction for f in unknown.flags if f.risk_type == "seasonality_unknown"), 0.0
    )
    confirmed_ded = next(
        (f.deduction for f in confirmed.flags if f.risk_type == "seasonality_confirmed"), 0.0
    )
    assert unknown_ded <= confirmed_ded


@given(data=risk_inputs())
def test_price_war_and_oversized_never_deduct(data: RiskInput) -> None:
    r = analyze_risk(
        RiskInput(**{**data.__dict__, "price_war_flag": True, "size_tier": "large_bulky"}), RULES
    )
    for f in r.flags:
        if f.risk_type in ("price_war", "oversized_logistics"):
            assert f.deduction == 0.0
