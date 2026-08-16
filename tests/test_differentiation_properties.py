"""Property-based tests for the differentiation engine (hypothesis)."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from delium.analysis.differentiation import analyze_differentiation
from delium.analysis.models import (
    Addressability,
    Confidence,
    DifferentiationInput,
    DiffReview,
    RawTheme,
    ThemeKind,
)

_ORDER = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}

review_ids = st.lists(st.integers(min_value=0, max_value=199), min_size=0, max_size=60)
addressabilities = st.sampled_from(list(Addressability))


def _reviews(n: int) -> tuple[DiffReview, ...]:
    return tuple(DiffReview(f"r{i}", stars=(i % 5) + 1) for i in range(n))


def _theme(ids: list[int], addr: Addressability = Addressability.UNKNOWN) -> RawTheme:
    return RawTheme(
        "t",
        ThemeKind.COMPLAINT,
        "theme",
        tuple(f"r{i}" for i in ids),
        addressability=addr,
    )


@st.composite
def inputs(draw: st.DrawFn) -> DifferentiationInput:  # type: ignore[type-arg]
    n = draw(st.integers(min_value=0, max_value=200))
    ids = draw(review_ids)
    addr = draw(addressabilities)
    listing = draw(st.one_of(st.none(), st.floats(1.0, 5.0)))
    return DifferentiationInput("B0", _reviews(n), (_theme(ids, addr),), listing_rating_avg=listing)


@given(data=inputs())
def test_all_scores_in_range(data: DifferentiationInput) -> None:
    r = analyze_differentiation(data)
    assert 0.0 <= r.pillar_score <= 100.0
    for comp in r.components:
        assert comp.value is None or 0.0 <= comp.value <= 100.0


@given(data=inputs())
def test_frequency_in_unit_interval(data: DifferentiationInput) -> None:
    r = analyze_differentiation(data)
    for t in r.themes:
        assert 0.0 <= t.frequency <= 1.0


@given(n=st.integers(min_value=1, max_value=200), ids=review_ids)
def test_duplicate_ids_cannot_increase_frequency(n: int, ids: list[int]) -> None:
    reviews = _reviews(n)
    base = analyze_differentiation(DifferentiationInput("B0", reviews, (_theme(ids),))).themes[0]
    doubled = analyze_differentiation(
        DifferentiationInput("B0", reviews, (_theme(ids + ids),))
    ).themes[0]
    assert doubled.frequency == base.frequency  # duplicates never inflate


@given(n=st.integers(min_value=10, max_value=200), base_ids=review_ids, extra=review_ids)
def test_adding_verified_evidence_cannot_reduce_frequency(
    n: int, base_ids: list[int], extra: list[int]
) -> None:
    reviews = _reviews(n)
    base = analyze_differentiation(DifferentiationInput("B0", reviews, (_theme(base_ids),))).themes[
        0
    ]
    more = analyze_differentiation(
        DifferentiationInput("B0", reviews, (_theme(base_ids + extra),))
    ).themes[0]
    assert more.frequency >= base.frequency - 1e-12


@given(n=st.integers(min_value=1, max_value=200), ids=review_ids, fakes=st.integers(0, 50))
def test_fake_ids_cannot_increase_score(n: int, ids: list[int], fakes: int) -> None:
    reviews = _reviews(n)
    clean = analyze_differentiation(DifferentiationInput("B0", reviews, (_theme(ids),)))
    ghost_ids = [f"ghost{k}" for k in range(fakes)]
    dirty = analyze_differentiation(
        DifferentiationInput(
            "B0",
            reviews,
            (
                RawTheme(
                    "t",
                    ThemeKind.COMPLAINT,
                    "theme",
                    tuple(f"r{i}" for i in ids) + tuple(ghost_ids),
                ),
            ),
        )
    )
    assert dirty.pillar_score <= clean.pillar_score + 1e-9


@given(data=inputs())
def test_identical_inputs_identical_outputs(data: DifferentiationInput) -> None:
    assert analyze_differentiation(data) == analyze_differentiation(data)


@given(n=st.integers(min_value=1, max_value=200), ids=review_ids)
def test_missing_evidence_cannot_increase_confidence(n: int, ids: list[int]) -> None:
    reviews = _reviews(n)
    full = analyze_differentiation(
        DifferentiationInput("B0", reviews, (_theme(ids),), listing_rating_avg=1.0)
    ).confidence.level
    # Strip themes and shrink the sample → strictly less evidence.
    reduced = analyze_differentiation(
        DifferentiationInput("B0", reviews[: n // 2], ())
    ).confidence.level
    assert _ORDER[reduced] <= _ORDER[full]
