from __future__ import annotations

import pytest

from delium.providers.base import (
    ProviderAuthError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from delium.providers.keepa import (
    KeepaClient,
    keepa_minutes_to_date,
    normalize_product,
)
from keepa_support import (
    DEFAULT_ASIN,
    FakeTransport,
    http,
    keepa_product_body,
    ok,
)


def _client(results: list[object], sleeps: list[float] | None = None) -> KeepaClient:
    sink = sleeps if sleeps is not None else []
    return KeepaClient("test-key", transport=FakeTransport(results), sleep=sink.append)


# --- pure helpers ---------------------------------------------------------
def test_keepa_minutes_to_date_epoch() -> None:
    # 2025-01-01 00:00:00 UTC == unix 1735689600 == keepa minute 7364160.
    assert keepa_minutes_to_date(7364160) == "2025-01-01"


def test_normalize_product_maps_all_fields() -> None:
    body = keepa_product_body()
    product = normalize_product(body["products"][0])

    assert product.asin == DEFAULT_ASIN
    assert product.title == "Test Silicone Tray"
    assert product.brand == "Acme"
    assert product.category_path == "Baby > Feeding"
    assert product.dims == {"length_mm": 200, "width_mm": 150, "height_mm": 40}
    assert product.weight_g == 300
    assert product.images_count == 3
    assert product.amazon_on_listing is True


def test_normalize_product_builds_history() -> None:
    body = keepa_product_body()
    product = normalize_product(body["products"][0])

    # Two dated points; the -1 sentinel on day 3 is dropped.
    assert [p.captured_on for p in product.history] == ["2025-01-01", "2025-01-02"]
    day1, day2 = product.history
    assert day1.price_cents == 2199
    assert day1.bsr == 1500
    assert day1.rating == 4.5
    assert day1.review_count == 480
    assert day2.price_cents == 2099
    assert day2.bsr == 1600
    assert day2.rating is None  # only present on day 1


# --- client parsing -------------------------------------------------------
def test_fetch_product_parses_and_absorbs_tokens() -> None:
    client = _client([ok(keepa_product_body(tokens_left=250, tokens_consumed=3))])
    result = client.fetch_product(DEFAULT_ASIN)

    assert result.http_status == 200
    assert result.tokens_consumed == 3
    assert result.tokens_left == 250
    assert DEFAULT_ASIN in result.normalized
    assert DEFAULT_ASIN in result.raw_products


def test_not_found_asin_is_absent_from_normalized() -> None:
    client = _client([ok(keepa_product_body(found=False))])
    result = client.fetch_product("B0MISSING01")

    assert result.normalized == {}
    assert result.raw_products["B0MISSING01"] == {"asin": "B0MISSING01", "found": False}


def test_batch_fetch_splits_per_asin_tokens() -> None:
    body = keepa_product_body(tokens_consumed=6)
    body["products"].append({**body["products"][0], "asin": "B0SECOND002"})
    client = _client([ok(body)])
    result = client.fetch_products([DEFAULT_ASIN, "B0SECOND002"])

    assert set(result.normalized) == {DEFAULT_ASIN, "B0SECOND002"}
    assert result.per_asin_tokens(2) == 3


# --- errors, retries, throttling -----------------------------------------
def test_auth_error_not_retried() -> None:
    sleeps: list[float] = []
    client = _client([http(401)], sleeps)
    with pytest.raises(ProviderAuthError):
        client.fetch_product(DEFAULT_ASIN)
    assert sleeps == []  # no retry on auth failure


def test_server_error_retries_then_raises() -> None:
    sleeps: list[float] = []
    transport = FakeTransport([http(500)])
    client = KeepaClient("k", transport=transport, sleep=sleeps.append)
    with pytest.raises(ProviderResponseError):
        client.fetch_product(DEFAULT_ASIN)
    assert transport.call_count == 3  # 1 + 2 retries
    assert len(sleeps) == 2  # a backoff before each retry


def test_network_error_then_success() -> None:
    transport = FakeTransport([ProviderNetworkError("boom"), ok(keepa_product_body())])
    client = KeepaClient("k", transport=transport, sleep=lambda _: None)
    result = client.fetch_product(DEFAULT_ASIN)
    assert transport.call_count == 2
    assert DEFAULT_ASIN in result.normalized


def test_rate_limit_waits_then_succeeds() -> None:
    sleeps: list[float] = []
    transport = FakeTransport(
        [http(429, {"tokensLeft": 0, "refillRate": 20}), ok(keepa_product_body())]
    )
    client = KeepaClient("k", transport=transport, sleep=sleeps.append)
    result = client.fetch_product(DEFAULT_ASIN)
    assert transport.call_count == 2
    assert len(sleeps) == 1  # waited for refill once
    assert DEFAULT_ASIN in result.normalized


def test_rate_limit_exhausted_raises() -> None:
    transport = FakeTransport([http(429, {"tokensLeft": 0, "refillRate": 20})])
    client = KeepaClient("k", transport=transport, sleep=lambda _: None)
    with pytest.raises(ProviderRateLimitError):
        client.fetch_product(DEFAULT_ASIN)


def test_throttles_when_tokens_low() -> None:
    sleeps: list[float] = []
    # First call reports only 1 token left; the second must pace before dispatch.
    transport = FakeTransport(
        [
            ok(keepa_product_body(tokens_left=1, refill_rate=20)),
            ok(keepa_product_body(tokens_left=200, refill_rate=20)),
        ]
    )
    client = KeepaClient("k", transport=transport, sleep=sleeps.append)
    client.fetch_product(DEFAULT_ASIN)
    assert sleeps == []  # first call: no prior token state
    client.fetch_product(DEFAULT_ASIN)
    assert len(sleeps) == 1 and sleeps[0] > 0  # paced before the second call


def test_missing_products_array_raises() -> None:
    client = _client([ok({"tokensLeft": 100})])
    with pytest.raises(ProviderResponseError):
        client.fetch_product(DEFAULT_ASIN)


def test_empty_api_key_rejected() -> None:
    from delium.providers.base import ProviderConfigError

    with pytest.raises(ProviderConfigError):
        KeepaClient("")


# --- normalization edge cases --------------------------------------------
def test_normalize_missing_dims_and_images() -> None:
    raw = {
        "asin": "B0BARE00001",
        "title": "Bare Product",
        "images": ["x.jpg", "y.jpg"],  # list form instead of imagesCSV
        "csv": [None] * 4,
    }
    product = normalize_product(raw)
    assert product.dims is None
    assert product.weight_g is None
    assert product.images_count == 2
    assert product.category_path is None
    assert product.history == []
    assert product.amazon_on_listing is False


def test_normalize_falls_back_to_new_price() -> None:
    from keepa_support import KM_DAY1

    csv: list[object] = [None] * 18
    csv[1] = [KM_DAY1, 1899]  # NEW price only, no Amazon series
    raw = {"asin": "B0NEW000001", "title": "New Only", "csv": csv}
    product = normalize_product(raw)
    assert product.amazon_on_listing is False
    assert product.history[0].price_cents == 1899


def test_empty_asin_list_rejected() -> None:
    client = _client([ok(keepa_product_body())])
    with pytest.raises(ValueError, match="at least one ASIN"):
        client.fetch_products([])


# --- marketplace + identity (cross-market) --------------------------------
def test_keepa_domain_mapping() -> None:
    from delium.providers.keepa import keepa_domain

    assert keepa_domain("US") == 1
    assert keepa_domain("UK") == 2
    assert keepa_domain("CA") == 6
    assert keepa_domain("IN") == 10
    assert keepa_domain("AU") == 13


def test_keepa_unknown_marketplace_rejected() -> None:
    from delium.providers.base import ProviderConfigError
    from delium.providers.keepa import keepa_domain

    with pytest.raises(ProviderConfigError, match="unsupported marketplace"):
        keepa_domain("ZZ")


def test_client_marketplace_sets_domain() -> None:
    client = KeepaClient("k", transport=FakeTransport([ok(keepa_product_body())]), marketplace="AU")
    assert client.marketplace == "AU"
    client.fetch_product(DEFAULT_ASIN)
    # The Keepa domain for AU (13) was sent on the request.
    transport = client._transport  # type: ignore[attr-defined]
    assert transport.calls[0]["domain"] == "13"


def test_normalize_extracts_gtin_and_manufacturer() -> None:
    product = normalize_product(keepa_product_body()["products"][0], marketplace="AU")
    assert product.marketplace == "AU"
    assert product.gtin == "0012345678905"  # EAN preferred
    assert product.manufacturer == "Acme Corp"


def test_normalize_gtin_falls_back_to_upc() -> None:
    raw = {"asin": "B0UPC000001", "title": "UPC only", "upcList": ["012345678905"], "csv": []}
    product = normalize_product(raw)
    assert product.gtin == "012345678905"


def test_normalize_gtin_absent_is_none() -> None:
    raw = {"asin": "B0NOGTIN001", "title": "No codes", "csv": []}
    product = normalize_product(raw)
    assert product.gtin is None
    assert product.manufacturer is None
