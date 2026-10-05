"""Product-link builder — every marketplace, both link types, and edge cases."""

from __future__ import annotations

import pytest

from delium.links import amazon_url, keepa_url, product_links


@pytest.mark.parametrize(
    ("marketplace", "expected"),
    [
        ("US", "https://www.amazon.com/dp/B0ABCDEF12"),
        ("UK", "https://www.amazon.co.uk/dp/B0ABCDEF12"),
        ("CA", "https://www.amazon.ca/dp/B0ABCDEF12"),
        ("AU", "https://www.amazon.com.au/dp/B0ABCDEF12"),
        ("IN", "https://www.amazon.in/dp/B0ABCDEF12"),
    ],
)
def test_amazon_url_every_marketplace(marketplace: str, expected: str) -> None:
    assert amazon_url("B0ABCDEF12", marketplace) == expected


@pytest.mark.parametrize(
    ("marketplace", "expected"),
    [
        ("US", "https://keepa.com/#!product/1-B0ABCDEF12"),
        ("UK", "https://keepa.com/#!product/2-B0ABCDEF12"),
        ("CA", "https://keepa.com/#!product/6-B0ABCDEF12"),
        ("IN", "https://keepa.com/#!product/10-B0ABCDEF12"),
    ],
)
def test_keepa_url_every_supported_marketplace(marketplace: str, expected: str) -> None:
    assert keepa_url("B0ABCDEF12", marketplace) == expected


def test_no_keepa_link_for_australia() -> None:
    assert keepa_url("B0ABCDEF12", "AU") is None
    # Amazon AU still works.
    assert amazon_url("B0ABCDEF12", "AU") == "https://www.amazon.com.au/dp/B0ABCDEF12"


def test_lowercase_marketplace_and_asin_are_normalised() -> None:
    assert amazon_url("b0abcdef12", "uk") == "https://www.amazon.co.uk/dp/B0ABCDEF12"
    assert keepa_url("b0abcdef12", "us") == "https://keepa.com/#!product/1-B0ABCDEF12"


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_missing_asin_returns_none(bad: str | None) -> None:
    assert amazon_url(bad, "US") is None
    assert keepa_url(bad, "US") is None


def test_unknown_marketplace_returns_none() -> None:
    assert amazon_url("B0ABCDEF12", "ZZ") is None
    assert keepa_url("B0ABCDEF12", "ZZ") is None
    assert amazon_url("B0ABCDEF12", None) is None


def test_product_links_bundle() -> None:
    links = product_links("b0abcdef12", "UK")
    assert links.asin == "B0ABCDEF12"
    assert links.marketplace == "UK"
    assert links.amazon_url == "https://www.amazon.co.uk/dp/B0ABCDEF12"
    assert links.keepa_url == "https://keepa.com/#!product/2-B0ABCDEF12"
    au = product_links("B0ABCDEF12", "AU")
    assert au.amazon_url is not None and au.keepa_url is None
