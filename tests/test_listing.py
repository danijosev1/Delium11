"""Listing quality engine: unit, boundary, and property tests."""

from __future__ import annotations

import pytest

from delium.analysis.listing import compute_listing_quality
from delium.analysis.models import Confidence, ListingInput

FULL = ListingInput(
    title="x" * 120,  # in the 80-200 sweet spot → title_length 100
    bullets=("a", "b", "c", "d", "e"),  # 5 → 100
    images_count=7,  # 7 → 100
    has_aplus=True,  # 100
    has_brand_store=True,  # 100
    has_video=None,  # unknown → skipped in completeness
    variation_count=3,  # 50 + 3/5*50 = 80
    review_count=500,  # log_norm(500,10,3000) ≈ 68.59
    keywords=("silicone", "tray"),
    price_cents=2000,
    competitor_prices_cents=(1800, 2000, 2200),  # median 2000 → ratio 1.0 → 100
    brand="Acme",
)


def _by_name(report, name):  # type: ignore[no-untyped-def]
    return next(s for s in report.subscores if s.name == name)


# --- worked example -------------------------------------------------------
def test_full_listing_overall_and_confidence() -> None:
    # keywords "silicone"/"tray" are not in the title ("xxxx..."), so coverage 0.
    listing = ListingInput(**{**FULL.__dict__, "title": "silicone tray " + "x" * 106})
    report = compute_listing_quality(listing)

    assert _by_name(report, "title_length").value == 100.0
    assert _by_name(report, "keyword_coverage").value == 100.0
    assert _by_name(report, "bullet_count").value == 100.0
    assert _by_name(report, "image_count").value == 100.0
    assert _by_name(report, "aplus").value == 100.0
    assert _by_name(report, "variation").value == 80.0
    assert _by_name(report, "review_density").value == pytest.approx(68.59, abs=0.1)
    assert _by_name(report, "price_positioning").value == 100.0
    assert report.confidence == Confidence.HIGH
    assert report.missing == ()
    # Weighted average with review_density ≈ 68.59 pulls it just under 96.
    assert report.overall_score == pytest.approx(95.86, abs=0.1)


def test_every_subscore_has_an_explanation() -> None:
    report = compute_listing_quality(FULL)
    assert len(report.subscores) == 10
    for sub in report.subscores:
        assert sub.detail  # non-empty explanation for every subscore


# --- missing data lowers confidence, does not guess ----------------------
def test_missing_fields_drop_out_and_lower_confidence() -> None:
    sparse = ListingInput(
        title="a decent product title that is reasonably long here",
        images_count=5,
        bullets=("a", "b"),
    )
    report = compute_listing_quality(sparse)

    # Unavailable subscores are listed as missing, not scored as 0.
    assert "aplus" in report.missing
    assert "keyword_coverage" in report.missing
    assert "price_positioning" in report.missing
    assert report.confidence == Confidence.LOW
    # Overall reflects only the assessed subscores.
    assert 0.0 <= report.overall_score <= 100.0


def test_no_data_is_low_confidence_zero_score() -> None:
    report = compute_listing_quality(ListingInput())
    assert report.overall_score == 0.0
    assert report.confidence == Confidence.LOW
    assert len(report.missing) == 10


def test_adding_a_field_never_lowers_confidence() -> None:
    base = ListingInput(title="t" * 100, images_count=6)
    richer = ListingInput(**{**base.__dict__, "has_aplus": True, "bullets": ("a", "b", "c")})
    order = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
    base_conf = compute_listing_quality(base).confidence
    richer_conf = compute_listing_quality(richer).confidence
    assert order[richer_conf] >= order[base_conf]


# --- boundary tests -------------------------------------------------------
@pytest.mark.parametrize(
    "length, expected",
    [(0, 0.0), (15, 0.0), (80, 100.0), (200, 100.0), (250, 70.0), (400, 70.0)],
)
def test_title_length_boundaries(length: int, expected: float) -> None:
    report = compute_listing_quality(ListingInput(title="x" * length))
    assert _by_name(report, "title_length").value == pytest.approx(expected)


@pytest.mark.parametrize("count, expected", [(0, 0.0), (3, 60.0), (5, 100.0), (8, 100.0)])
def test_bullet_count_boundaries(count: int, expected: float) -> None:
    report = compute_listing_quality(ListingInput(bullets=tuple("x" for _ in range(count))))
    assert _by_name(report, "bullet_count").value == pytest.approx(expected)


@pytest.mark.parametrize("count, expected", [(0, 0.0), (7, 100.0), (14, 100.0)])
def test_image_count_boundaries(count: int, expected: float) -> None:
    report = compute_listing_quality(ListingInput(images_count=count))
    assert _by_name(report, "image_count").value == pytest.approx(expected)


@pytest.mark.parametrize(
    "matched_keywords, expected",
    [(("silicone", "tray"), 100.0), (("silicone", "missing"), 50.0), (("nope",), 0.0)],
)
def test_keyword_coverage_boundaries(matched_keywords: tuple[str, ...], expected: float) -> None:
    report = compute_listing_quality(
        ListingInput(title="silicone tray for baby food", keywords=matched_keywords)
    )
    assert _by_name(report, "keyword_coverage").value == pytest.approx(expected)


@pytest.mark.parametrize(
    "price, expected",
    [
        (2000, pytest.approx(100.0)),  # ratio 1.0 → in the competitive plateau
        (1400, pytest.approx(66.67, abs=0.1)),  # ratio 0.7 → on the rising edge
        (2600, pytest.approx(55.56, abs=0.1)),  # ratio 1.3 → decaying (overpriced)
        (600, pytest.approx(0.0)),  # ratio 0.3 → below floor (race-to-bottom)
    ],
)
def test_price_positioning_boundaries(price: int, expected: object) -> None:
    report = compute_listing_quality(
        ListingInput(price_cents=price, competitor_prices_cents=(1800, 2000, 2200))
    )
    assert _by_name(report, "price_positioning").value == expected


def test_aplus_and_brand_store_binary() -> None:
    on = compute_listing_quality(ListingInput(has_aplus=True, has_brand_store=True))
    off = compute_listing_quality(ListingInput(has_aplus=False, has_brand_store=False))
    assert _by_name(on, "aplus").value == 100.0
    assert _by_name(off, "aplus").value == 0.0
    assert _by_name(on, "brand_store").value == 100.0
    assert _by_name(off, "brand_store").value == 0.0
