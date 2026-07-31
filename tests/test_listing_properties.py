"""Property-based tests for the listing quality engine (hypothesis)."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from delium.analysis.listing import compute_listing_quality
from delium.analysis.models import Confidence, ListingInput

_CONF_ORDER = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}

titles = st.one_of(st.none(), st.text(max_size=400))
counts = st.one_of(st.none(), st.integers(min_value=0, max_value=50))
bools = st.one_of(st.none(), st.booleans())
bullets = st.one_of(st.none(), st.lists(st.text(min_size=1, max_size=20), max_size=10).map(tuple))
keywords = st.one_of(st.none(), st.lists(st.text(min_size=1, max_size=15), max_size=8).map(tuple))
prices = st.one_of(st.none(), st.integers(min_value=100, max_value=50_000))
comp_prices = st.one_of(
    st.none(),
    st.lists(st.integers(min_value=100, max_value=50_000), min_size=1, max_size=10).map(tuple),
)


def _listing(**kwargs: object) -> ListingInput:
    return ListingInput(**kwargs)  # type: ignore[arg-type]


@given(
    title=titles,
    images_count=counts,
    variation_count=counts,
    review_count=counts,
    has_aplus=bools,
    has_brand_store=bools,
    has_video=bools,
    bullets=bullets,
    keywords=keywords,
    price_cents=prices,
    competitor_prices_cents=comp_prices,
)
def test_overall_always_in_range(**kwargs: object) -> None:
    report = compute_listing_quality(_listing(**kwargs))
    assert 0.0 <= report.overall_score <= 100.0


@given(
    title=titles,
    images_count=counts,
    variation_count=counts,
    review_count=counts,
    has_aplus=bools,
    bullets=bullets,
    keywords=keywords,
    price_cents=prices,
    competitor_prices_cents=comp_prices,
)
def test_every_subscore_in_range_or_missing(**kwargs: object) -> None:
    report = compute_listing_quality(_listing(**kwargs))
    for sub in report.subscores:
        assert sub.value is None or 0.0 <= sub.value <= 100.0


@given(count=st.integers(min_value=0, max_value=6), extra=st.integers(min_value=1, max_value=20))
def test_image_subscore_monotonic_in_count(count: int, extra: int) -> None:
    def img_value(n: int) -> float:
        report = compute_listing_quality(ListingInput(images_count=n))
        return next(s.value for s in report.subscores if s.name == "image_count")  # type: ignore[misc]

    assert img_value(count + extra) >= img_value(count)


@given(kw=st.lists(st.text(min_size=1, max_size=12), min_size=1, max_size=8).map(tuple))
def test_keyword_coverage_bounded(kw: tuple[str, ...]) -> None:
    report = compute_listing_quality(ListingInput(title="a b c generic title", keywords=kw))
    value = next(s.value for s in report.subscores if s.name == "keyword_coverage")
    assert value is not None and 0.0 <= value <= 100.0


@given(
    title=st.text(min_size=50, max_size=150),
    images_count=st.integers(min_value=1, max_value=9),
)
def test_richer_listing_never_less_confident(title: str, images_count: int) -> None:
    base = ListingInput(title=title, images_count=images_count)
    richer = ListingInput(
        **{**base.__dict__, "has_aplus": True, "bullets": ("a", "b", "c"), "review_count": 100}
    )
    base_conf = compute_listing_quality(base).confidence
    richer_conf = compute_listing_quality(richer).confidence
    assert _CONF_ORDER[richer_conf] >= _CONF_ORDER[base_conf]
