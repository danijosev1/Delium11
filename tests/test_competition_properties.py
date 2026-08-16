"""Property-based tests for the competition engine (hypothesis)."""

from __future__ import annotations

from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from delium.analysis.competition import analyze_competition
from delium.analysis.models import (
    CompetitionInput,
    CompetitorSnapshot,
    Confidence,
)

AS_OF = date(2026, 8, 1)
_ORDER = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


@st.composite
def competitors(draw: st.DrawFn, min_size: int = 0, max_size: int = 12):  # type: ignore[no-untyped-def]
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    out = []
    for i in range(n):
        out.append(
            CompetitorSnapshot(
                asin=f"B{i:02d}",
                brand=draw(st.one_of(st.none(), st.sampled_from(["A", "B", "C", "D"]))),
                review_count=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=50_000))),
                review_count_90d_ago=draw(st.one_of(st.none(), st.integers(0, 50_000))),
                listing_quality=draw(st.one_of(st.none(), st.floats(0, 100))),
                price_cents=draw(st.one_of(st.none(), st.integers(100, 50_000))),
            )
        )
    return tuple(out)


@given(comps=competitors())
def test_all_scores_in_range(comps: tuple[CompetitorSnapshot, ...]) -> None:
    r = analyze_competition(CompetitionInput(comps, AS_OF))
    assert 0.0 <= r.pillar_score <= 100.0
    for comp in r.components:
        assert comp.value is None or 0.0 <= comp.value <= 100.0


@given(comps=competitors())
def test_hhi_between_0_and_1(comps: tuple[CompetitorSnapshot, ...]) -> None:
    r = analyze_competition(CompetitionInput(comps, AS_OF))
    if r.hhi is not None:
        assert 0.0 < r.hhi <= 1.0


@given(comps=competitors())
def test_identical_inputs_identical_outputs(comps: tuple[CompetitorSnapshot, ...]) -> None:
    a = analyze_competition(CompetitionInput(comps, AS_OF))
    b = analyze_competition(CompetitionInput(comps, AS_OF))
    assert a == b


@given(
    comps=competitors(min_size=1),
    bump=st.integers(min_value=1, max_value=10_000),
)
def test_more_reviews_never_easier_moat(comps: tuple[CompetitorSnapshot, ...], bump: int) -> None:
    base = analyze_competition(CompetitionInput(comps, AS_OF)).review_moat.score
    heavier = tuple(
        CompetitorSnapshot(
            **{
                **c.__dict__,
                "review_count": (c.review_count + bump) if c.review_count is not None else None,
            }
        )
        for c in comps
    )
    bumped = analyze_competition(CompetitionInput(heavier, AS_OF)).review_moat.score
    if base is not None and bumped is not None:
        assert bumped <= base + 1e-9  # more reviews → moat only gets harder (lower score)


@given(comps=competitors(min_size=8, max_size=12))
def test_missing_data_cannot_increase_confidence(comps: tuple[CompetitorSnapshot, ...]) -> None:
    full = analyze_competition(CompetitionInput(comps, AS_OF)).confidence.level
    # Strip every optional signal → strictly less data.
    stripped = tuple(CompetitorSnapshot(asin=c.asin) for c in comps)
    reduced = analyze_competition(CompetitionInput(stripped, AS_OF)).confidence.level
    assert _ORDER[reduced] <= _ORDER[full]


@given(comps=competitors())
def test_price_war_detection_deterministic(comps: tuple[CompetitorSnapshot, ...]) -> None:
    a = analyze_competition(CompetitionInput(comps, AS_OF)).price_war_flag
    b = analyze_competition(CompetitionInput(comps, AS_OF)).price_war_flag
    assert a == b
    assert isinstance(a, bool)
