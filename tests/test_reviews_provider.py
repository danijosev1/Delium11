from __future__ import annotations

import pytest

from delium.providers.base import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from delium.providers.reviews import (
    ApifyClient,
    ReviewProviderChain,
    UnwrangleClient,
    normalize_apify,
    normalize_unwrangle,
)
from reviews_support import (
    ASIN,
    FakeGetTransport,
    FakePostTransport,
    apify_items,
    get_resp,
    http,
    unwrangle_body,
)


def _unwrangle(results: list[object], sleeps: list[float] | None = None) -> UnwrangleClient:
    sink = sleeps if sleeps is not None else []
    return UnwrangleClient("key", transport=FakeGetTransport(results), sleep=sink.append)


def _apify(results: list[object]) -> ApifyClient:
    return ApifyClient("token", transport=FakePostTransport(results), sleep=lambda _: None)


# --- normalization --------------------------------------------------------
def test_normalize_unwrangle_maps_fields() -> None:
    reviews = normalize_unwrangle(unwrangle_body(3), ASIN)
    assert len(reviews) == 3
    first = reviews[0]
    assert first.review_id == "R0"
    assert first.asin == ASIN
    assert first.stars == 5
    assert first.title == "Review 0"
    assert first.body == "body text 0"
    assert first.verified_purchase is True
    assert first.review_date == "2026-07-01"
    assert first.reviewer_name == "user0"


def test_normalize_apify_maps_fields_and_helpful_string() -> None:
    reviews = normalize_apify(apify_items(2), ASIN)
    assert len(reviews) == 2
    assert reviews[0].review_id == "A0"
    assert reviews[0].stars == 4
    assert reviews[0].body == "apify body 0"
    assert reviews[1].helpful_votes == 1  # parsed from "1 people found this helpful"


def test_normalize_skips_invalid_stars() -> None:
    body = {
        "reviews": [{"id": "x", "review": "no rating"}, {"id": "y", "rating": 9, "review": "bad"}]
    }
    assert normalize_unwrangle(body, ASIN) == []


def test_normalize_synthesizes_missing_id() -> None:
    body = {"reviews": [{"rating": 5, "review": "great", "author": "a", "date": "2026-01-01"}]}
    reviews = normalize_unwrangle(body, ASIN)
    assert len(reviews) == 1
    assert reviews[0].review_id.startswith(f"{ASIN}-")


# --- clients + cost -------------------------------------------------------
def test_unwrangle_fetch_parses_and_costs() -> None:
    result = _unwrangle([get_resp(unwrangle_body(5))]).fetch_reviews(ASIN)
    assert result.provider == "unwrangle"
    assert len(result.reviews) == 5
    assert result.cost_usd == pytest.approx(5 * 0.003)


def test_apify_fetch_parses_and_costs() -> None:
    result = _apify([get_resp(apify_items(4))]).fetch_reviews(ASIN)
    assert result.provider == "apify"
    assert len(result.reviews) == 4
    assert result.cost_usd == pytest.approx(4 * 0.003)


def test_unwrangle_success_false_raises() -> None:
    with pytest.raises(ProviderResponseError):
        _unwrangle([get_resp(unwrangle_body(0, success=False))]).fetch_reviews(ASIN)


# --- errors / retries -----------------------------------------------------
def test_auth_error_not_retried() -> None:
    sleeps: list[float] = []
    with pytest.raises(ProviderAuthError):
        _unwrangle([http(401)], sleeps).fetch_reviews(ASIN)
    assert sleeps == []


def test_server_error_retries_then_raises() -> None:
    sleeps: list[float] = []
    transport = FakeGetTransport([http(500)])
    client = UnwrangleClient("k", transport=transport, sleep=sleeps.append)
    with pytest.raises(ProviderResponseError):
        client.fetch_reviews(ASIN)
    assert transport.call_count == 3
    assert len(sleeps) == 2


def test_network_error_then_success() -> None:
    transport = FakeGetTransport([ProviderNetworkError("boom"), get_resp(unwrangle_body(1))])
    client = UnwrangleClient("k", transport=transport, sleep=lambda _: None)
    result = client.fetch_reviews(ASIN)
    assert transport.call_count == 2
    assert len(result.reviews) == 1


def test_rate_limit_exhausted_raises() -> None:
    transport = FakeGetTransport([http(429)])
    client = UnwrangleClient("k", transport=transport, sleep=lambda _: None)
    with pytest.raises(ProviderRateLimitError):
        client.fetch_reviews(ASIN)


# --- chain / fallback -----------------------------------------------------
def test_chain_falls_back_to_apify() -> None:
    primary = UnwrangleClient("k", transport=FakeGetTransport([http(500)]), sleep=lambda _: None)
    fallback = _apify([get_resp(apify_items(2))])
    chain = ReviewProviderChain([primary, fallback])
    result = chain.fetch_reviews(ASIN)
    assert result.provider == "apify"
    assert len(result.reviews) == 2


def test_chain_uses_primary_when_it_succeeds() -> None:
    primary = _unwrangle([get_resp(unwrangle_body(3))])
    fallback = _apify([get_resp(apify_items(2))])
    chain = ReviewProviderChain([primary, fallback])
    result = chain.fetch_reviews(ASIN)
    assert result.provider == "unwrangle"
    assert len(result.reviews) == 3


def test_chain_raises_when_all_fail() -> None:
    primary = UnwrangleClient("k", transport=FakeGetTransport([http(500)]), sleep=lambda _: None)
    fallback = ApifyClient("t", transport=FakePostTransport([http(500)]), sleep=lambda _: None)
    chain = ReviewProviderChain([primary, fallback])
    with pytest.raises(ProviderResponseError):
        chain.fetch_reviews(ASIN)


def test_empty_credentials_rejected() -> None:
    with pytest.raises(ProviderConfigError):
        UnwrangleClient("")
    with pytest.raises(ProviderConfigError):
        ApifyClient("")


# --- parsing edge cases ---------------------------------------------------
def test_parses_worded_date_and_votes_string() -> None:
    body = {
        "reviews": [
            {
                "id": "z",
                "rating": 4,
                "review": "ok",
                "date": "Reviewed in the United States on July 2, 2026",
                "helpful_votes": "12 people found this helpful",
            }
        ]
    }
    review = normalize_unwrangle(body, ASIN)[0]
    assert review.review_date == "2026-07-02"
    assert review.helpful_votes == 12


def test_apify_unverified_and_synthesized_id() -> None:
    items = [{"ratingScore": 5, "reviewDescription": "great", "isVerified": False}]
    review = normalize_apify(items, ASIN)[0]
    assert review.verified_purchase is False
    assert review.review_id.startswith(f"{ASIN}-")


# --- build_review_provider from env --------------------------------------
def test_build_provider_prefers_unwrangle_then_apify(monkeypatch: pytest.MonkeyPatch) -> None:
    from delium.providers.reviews import build_review_provider

    monkeypatch.setenv("DELIUM_UNWRANGLE_API_KEY", "u")
    monkeypatch.setenv("DELIUM_APIFY_API_TOKEN", "a")
    chain = build_review_provider()
    assert [p.name for p in chain._providers] == ["unwrangle", "apify"]


def test_build_provider_apify_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from delium.providers.reviews import build_review_provider

    monkeypatch.delenv("DELIUM_UNWRANGLE_API_KEY", raising=False)
    monkeypatch.setenv("DELIUM_APIFY_API_TOKEN", "a")
    chain = build_review_provider()
    assert [p.name for p in chain._providers] == ["apify"]


def test_build_provider_none_configured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from delium.providers.reviews import build_review_provider

    monkeypatch.delenv("DELIUM_UNWRANGLE_API_KEY", raising=False)
    monkeypatch.delenv("DELIUM_APIFY_API_TOKEN", raising=False)
    with pytest.raises(ProviderConfigError):
        build_review_provider()
