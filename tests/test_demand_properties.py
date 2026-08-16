"""Property-based tests for the demand engine (hypothesis)."""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given
from hypothesis import strategies as st

from delium.analysis import curves
from delium.analysis.demand import (
    _keyword_demand,
    _sales_estimate,
    analyze_demand,
    load_velocity_curves,
)
from delium.analysis.models import (
    AsinHistory,
    BsrPoint,
    Confidence,
    DemandConfig,
    KeywordDatum,
)

CURVES = load_velocity_curves()
HK = CURVES.categories["Home & Kitchen"]
CFG = DemandConfig()
AS_OF = date(2026, 8, 1)
_ORDER = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}

# A BSR history: list of (days_ago, bsr).
history_points = st.lists(
    st.tuples(st.integers(min_value=0, max_value=120), st.integers(min_value=1, max_value=500_000)),
    min_size=0,
    max_size=40,
)


def _history(points: list[tuple[int, int]]) -> AsinHistory:
    return AsinHistory("B0X", tuple(BsrPoint(AS_OF - timedelta(days=d), b) for d, b in points))


@given(points=history_points, known=st.booleans())
def test_sales_range_is_ordered(points: list[tuple[int, int]], known: bool) -> None:
    est = _sales_estimate(_history(points), AS_OF, HK, known, CFG)
    if est is not None:
        assert 0 <= est.low_units <= est.expected_units <= est.high_units


@given(points=history_points)
def test_all_component_scores_bounded(points: list[tuple[int, int]]) -> None:
    report = analyze_demand(
        as_of=AS_OF,
        histories=[_history(points)],
        keywords=[KeywordDatum("a b c", 5000)],
        category="Home & Kitchen",
        curves_table=CURVES,
    )
    assert 0.0 <= report.pillar_score <= 100.0
    for comp in report.components:
        assert comp.value is None or 0.0 <= comp.value <= 100.0


@given(
    a=st.integers(min_value=150, max_value=1200),
    b=st.integers(min_value=150, max_value=1200),
)
def test_velocity_non_decreasing_in_sweet_spot(a: int, b: int) -> None:
    # Within the rising/plateau region, more units never lowers the score.
    lo, hi = sorted((a, b))
    assert curves.plateau(hi, *CFG.velocity_curve) >= curves.plateau(lo, *CFG.velocity_curve)


@given(
    base=st.lists(
        st.tuples(st.text(min_size=1, max_size=10), st.integers(min_value=1, max_value=50_000)),
        max_size=6,
    ),
    extra_vol=st.integers(min_value=1, max_value=50_000),
)
def test_adding_keyword_does_not_reduce_volume_component(
    base: list[tuple[str, int]], extra_vol: int
) -> None:
    kws = [KeywordDatum(p, v) for p, v in base]
    before = _keyword_demand(kws, None, None, CFG).search_volume_score or 0.0
    after = _keyword_demand(
        [*kws, KeywordDatum("zzuniquephrase", extra_vol)], None, None, CFG
    ).search_volume_score
    assert after is not None and after >= before - 1e-9


@given(points=history_points)
def test_insufficient_data_cannot_increase_confidence(points: list[tuple[int, int]]) -> None:
    keywords = [KeywordDatum(f"kw{i} phrase", 3000 + i) for i in range(5)]
    histories = [_history(points) for _ in range(9)]
    full = analyze_demand(
        as_of=AS_OF,
        histories=histories,
        keywords=keywords,
        category="Home & Kitchen",
        curves_table=CURVES,
    ).confidence
    # Strictly less data (fewer ASINs, fewer keywords, unknown category).
    reduced = analyze_demand(
        as_of=AS_OF,
        histories=histories[:1],
        keywords=keywords[:1],
        category=None,
        curves_table=CURVES,
    ).confidence
    assert _ORDER[reduced] <= _ORDER[full]


@given(points=history_points)
def test_identical_inputs_identical_outputs(points: list[tuple[int, int]]) -> None:
    kwargs = dict(
        as_of=AS_OF,
        histories=[_history(points)],
        keywords=[KeywordDatum("a b", 4000)],
        category="Home & Kitchen",
        curves_table=CURVES,
    )
    assert analyze_demand(**kwargs) == analyze_demand(**kwargs)  # type: ignore[arg-type]
