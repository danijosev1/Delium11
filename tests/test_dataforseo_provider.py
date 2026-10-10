from __future__ import annotations

import pytest

from dataforseo_support import (
    FakePostTransport,
    http,
    ok,
    related_body,
    serp_body,
    task_error_body,
    volume_body,
)
from delium.providers.base import (
    ProviderAuthError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnsupportedLocationError,
)
from delium.providers.dataforseo import (
    DataForSeoClient,
    labs_amazon_supported,
    merchant_amazon_supported,
    normalize_keyword_data,
    normalize_phrase,
    normalize_serp,
    normalize_volume,
)


def _client(results: list[object], sleeps: list[float] | None = None) -> DataForSeoClient:
    sink = sleeps if sleeps is not None else []
    return DataForSeoClient(
        "login", "pass", transport=FakePostTransport(results), sleep=sink.append
    )


# --- normalization --------------------------------------------------------
def test_normalize_phrase() -> None:
    assert normalize_phrase("  Silicone   Baby  TRAY ") == "silicone baby tray"


def test_normalize_volume() -> None:
    kws = normalize_volume(volume_body(9400))
    assert len(kws) == 1
    assert kws[0].phrase == "silicone baby food tray"
    assert kws[0].volume == 9400


def test_normalize_keyword_data() -> None:
    kws = normalize_keyword_data(related_body([("Freezer Tray", 3200), ("no volume kw", None)]))  # type: ignore[list-item]
    assert kws[0].phrase == "freezer tray"
    assert kws[0].volume == 3200
    assert kws[1].volume is None  # missing search_volume tolerated


def test_normalize_serp_filters_and_flags() -> None:
    items = normalize_serp(serp_body())
    # Only amazon_serp + amazon_paid with an ASIN survive.
    assert [i.asin for i in items] == ["B0AAA00001", "B0BBB00002", "B0CCC00003"]
    assert items[0].sponsored is False
    assert items[1].sponsored is True
    assert items[0].price_cents == 2199
    assert items[2].price_cents is None  # no price_from


# --- client parsing + cost -----------------------------------------------
def test_search_volume_parses_and_tracks_cost() -> None:
    result = _client([ok(volume_body(9400, cost=0.024))]).search_volume(["Silicone Baby Food Tray"])
    assert result.http_status == 200
    assert result.cost_usd == 0.024
    assert result.keywords[0].volume == 9400


def test_related_keywords_parses() -> None:
    result = _client([ok(related_body())]).related_keywords("silicone baby food tray")
    assert len(result.keywords) == 3
    assert result.cost_usd == 0.02


def test_ranked_keywords_reverse_asin_parses() -> None:
    result = _client([ok(related_body())]).ranked_keywords("B0AAA00001")
    assert len(result.keywords) == 3


def test_serp_parses() -> None:
    result = _client([ok(serp_body(cost=0.006))]).serp("silicone baby food tray")
    assert result.cost_usd == 0.006
    assert len(result.serp) == 3


# --- errors / retries -----------------------------------------------------
def test_auth_error_not_retried() -> None:
    sleeps: list[float] = []
    with pytest.raises(ProviderAuthError):
        _client([http(401)], sleeps).search_volume(["x"])
    assert sleeps == []


def test_server_error_retries_then_raises() -> None:
    sleeps: list[float] = []
    transport = FakePostTransport([http(500)])
    client = DataForSeoClient("l", "p", transport=transport, sleep=sleeps.append)
    with pytest.raises(ProviderResponseError):
        client.search_volume(["x"])
    assert transport.call_count == 3
    assert len(sleeps) == 2


def test_network_error_then_success() -> None:
    transport = FakePostTransport([ProviderNetworkError("boom"), ok(volume_body())])
    client = DataForSeoClient("l", "p", transport=transport, sleep=lambda _: None)
    result = client.search_volume(["x"])
    assert transport.call_count == 2
    assert result.keywords[0].volume == 9400


def test_rate_limit_retries_then_raises() -> None:
    transport = FakePostTransport([http(429)])
    client = DataForSeoClient("l", "p", transport=transport, sleep=lambda _: None)
    with pytest.raises(ProviderRateLimitError):
        client.search_volume(["x"])
    assert transport.call_count == 3


def test_task_level_error_raises() -> None:
    with pytest.raises(ProviderResponseError):
        _client([ok(task_error_body())]).search_volume(["x"])


def test_missing_tasks_raises() -> None:
    with pytest.raises(ProviderResponseError):
        _client([ok({"status_code": 20000, "cost": 0})]).search_volume(["x"])


def test_empty_credentials_rejected() -> None:
    from delium.providers.base import ProviderConfigError

    with pytest.raises(ProviderConfigError):
        DataForSeoClient("", "pass")


def test_search_volume_requires_keywords() -> None:
    with pytest.raises(ValueError, match="at least one keyword"):
        _client([ok(volume_body())]).search_volume([])


# --- marketplace routing (cross-market) -----------------------------------
def test_dataforseo_location_mapping() -> None:
    from delium.providers.dataforseo import dataforseo_location

    # (location_code, labs_language_code, serp_language_code). Labs Amazon
    # endpoints require the short ISO "en"; the Merchant SERP wants the locale.
    assert dataforseo_location("US") == (2840, "en", "en_US")
    assert dataforseo_location("UK") == (2826, "en", "en_GB")
    assert dataforseo_location("CA") == (2124, "en", "en_CA")
    assert dataforseo_location("AU") == (2036, "en", "en_AU")
    assert dataforseo_location("IN") == (2356, "en", "en_IN")


def test_dataforseo_unknown_marketplace_rejected() -> None:
    from delium.providers.base import ProviderConfigError
    from delium.providers.dataforseo import dataforseo_location

    with pytest.raises(ProviderConfigError, match="unsupported marketplace"):
        dataforseo_location("ZZ")


def test_client_marketplace_sets_location_in_request() -> None:
    # The realistic non-US path is the Merchant Amazon SERP (Labs is US-only); a
    # UK client must route to UK (2826) with the "en_GB" locale.
    transport = FakePostTransport([ok(serp_body())])
    client = DataForSeoClient(
        "login", "pass", transport=transport, sleep=lambda _: None, marketplace="UK"
    )
    assert client.marketplace == "UK"
    client.serp("baby food tray")
    _url, body = transport.calls[0]
    assert body[0]["location_code"] == 2826
    assert body[0]["language_code"] == "en_GB"


# --- per-endpoint location coverage ---------------------------------------
# DataForSEO Labs Amazon (volume/related/ranked) is US-only; the Merchant Amazon
# SERP covers the Amazon storefronts (US/UK/CA/EU). An unsupported marketplace is
# refused BEFORE any HTTP call, so it never costs anything.
def test_location_predicates_match_endpoint_coverage() -> None:
    assert labs_amazon_supported("US") is True
    for mp in ("UK", "CA", "AU", "IN"):
        assert labs_amazon_supported(mp) is False
    # Merchant SERP: the marketplaces Delium wires a location code for and that
    # the endpoint covers (US/UK/CA). AU/IN are not merchant-served.
    for mp in ("US", "UK", "CA"):
        assert merchant_amazon_supported(mp) is True
    for mp in ("AU", "IN", "ZZ"):
        assert merchant_amazon_supported(mp) is False


@pytest.mark.parametrize("marketplace", ["UK", "CA", "AU", "IN"])
def test_labs_endpoints_refused_for_non_us_without_a_call(marketplace: str) -> None:
    """The three Labs Amazon endpoints must raise (US-only) before any request —
    an unsupported location costs nothing (the 40501 'Invalid Field' bug)."""
    transport = FakePostTransport([ok(volume_body()), ok(related_body()), ok(related_body())])
    client = DataForSeoClient(
        "login", "pass", transport=transport, sleep=lambda _: None, marketplace=marketplace
    )
    for call in (
        lambda c: c.search_volume(["baby food tray"]),
        lambda c: c.related_keywords("baby food tray"),
        lambda c: c.ranked_keywords("B0AAA00001"),
    ):
        with pytest.raises(ProviderUnsupportedLocationError):
            call(client)
    assert transport.call_count == 0  # never hit the wire → no DataForSEO cost


def test_labs_endpoints_send_iso_language_code() -> None:
    """For the supported marketplace (US) the three Labs Amazon endpoints send the
    short ISO language_code ("en"), never the "en_US" locale."""
    for call, response in (
        (lambda c: c.search_volume(["baby food tray"]), volume_body()),
        (lambda c: c.related_keywords("baby food tray"), related_body()),
        (lambda c: c.ranked_keywords("B0AAA00001"), related_body()),
    ):
        transport = FakePostTransport([ok(response)])
        client = DataForSeoClient(
            "login", "pass", transport=transport, sleep=lambda _: None, marketplace="US"
        )
        call(client)
        _url, body = transport.calls[0]
        assert body[0]["location_code"] == 2840
        assert body[0]["language_code"] == "en"


# Merchant SERP locale per supported marketplace (docs-verified 2026-10).
_SERP_LOCALE = {"US": (2840, "en_US"), "UK": (2826, "en_GB"), "CA": (2124, "en_CA")}


@pytest.mark.parametrize("marketplace", ["US", "UK", "CA"])
def test_serp_endpoint_sends_locale_language_code(marketplace: str) -> None:
    """The Merchant Amazon SERP endpoint requires the "en_XX" locale form."""
    loc, serp_lang = _SERP_LOCALE[marketplace]
    transport = FakePostTransport([ok(serp_body())])
    client = DataForSeoClient(
        "login", "pass", transport=transport, sleep=lambda _: None, marketplace=marketplace
    )
    client.serp("baby food tray")
    _url, body = transport.calls[0]
    assert body[0]["location_code"] == loc
    assert body[0]["language_code"] == serp_lang


@pytest.mark.parametrize("marketplace", ["AU", "IN"])
def test_serp_refused_for_unsupported_marketplace_without_a_call(marketplace: str) -> None:
    """A marketplace the Merchant SERP does not cover is refused before any HTTP
    call — no request, no cost."""
    transport = FakePostTransport([ok(serp_body())])
    client = DataForSeoClient(
        "login", "pass", transport=transport, sleep=lambda _: None, marketplace=marketplace
    )
    with pytest.raises(ProviderUnsupportedLocationError):
        client.serp("baby food tray")
    assert transport.call_count == 0
