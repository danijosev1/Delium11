from __future__ import annotations

from pathlib import Path

import pytest

from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.ingestion import fetch_reviews
from delium.providers.base import ProviderResponseError
from delium.providers.reviews import ApifyClient, ReviewProviderChain, UnwrangleClient
from reviews_support import (
    ASIN,
    FakeGetTransport,
    FakePostTransport,
    apify_items,
    get_resp,
    http,
    unwrangle_body,
)


def _unwrangle(results: list[object]) -> tuple[UnwrangleClient, FakeGetTransport]:
    transport = FakeGetTransport(results)
    return UnwrangleClient("k", transport=transport, sleep=lambda _: None), transport


def _new_run() -> str:
    with get_connection() as conn:
        return repository.insert_run(conn, command="fetch.reviews", input_=ASIN)


def _age_out() -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE raw_fetches SET fetched_at = '2000-01-01 00:00:00' WHERE request_key = ?",
            (f"reviews:asin:{ASIN}",),
        )


def test_miss_fetches_normalizes_and_stores(initialized_db: Path) -> None:
    client, transport = _unwrangle([get_resp(unwrangle_body(3))])
    result = fetch_reviews(ASIN, run_id=_new_run(), provider=client, config=DeliumConfig())

    assert result.from_cache is False
    assert result.provider == "unwrangle"
    assert len(result.reviews) == 3
    assert result.cost_usd == pytest.approx(3 * 0.003)
    assert transport.call_count == 1

    with get_connection() as conn:
        # A product stub is created so the reviews FK is satisfied.
        assert repository.get_product(conn, ASIN) is not None
        assert len(repository.get_reviews_for_asin(conn, ASIN)) == 3


def test_cache_hit_returns_db_reviews_without_provider(initialized_db: Path) -> None:
    client, transport = _unwrangle([get_resp(unwrangle_body(3))])
    config = DeliumConfig()

    first = fetch_reviews(ASIN, run_id=_new_run(), provider=client, config=config)
    second = fetch_reviews(ASIN, run_id=_new_run(), provider=client, config=config)

    assert first.from_cache is False
    assert second.from_cache is True
    assert second.provider is None
    assert len(second.reviews) == 3
    assert transport.call_count == 1  # provider untouched on the hit


def test_stale_cache_refetches(initialized_db: Path) -> None:
    client, transport = _unwrangle([get_resp(unwrangle_body(3)), get_resp(unwrangle_body(3))])
    config = DeliumConfig()

    fetch_reviews(ASIN, run_id=_new_run(), provider=client, config=config)
    _age_out()
    second = fetch_reviews(ASIN, run_id=_new_run(), provider=client, config=config)

    assert second.from_cache is False
    assert transport.call_count == 2


def test_force_bypasses_cache(initialized_db: Path) -> None:
    client, transport = _unwrangle([get_resp(unwrangle_body(3)), get_resp(unwrangle_body(3))])
    config = DeliumConfig()

    fetch_reviews(ASIN, run_id=_new_run(), provider=client, config=config)
    fetch_reviews(ASIN, run_id=_new_run(), provider=client, config=config, force=True)

    assert transport.call_count == 2


def test_cost_tracked_in_raw_fetch(initialized_db: Path) -> None:
    client, _ = _unwrangle([get_resp(unwrangle_body(10))])
    fetch_reviews(ASIN, run_id=_new_run(), provider=client, config=DeliumConfig())

    with get_connection() as conn:
        fetch = repository.latest_raw_fetch(conn, "reviews", f"reviews:asin:{ASIN}")
    assert fetch is not None
    assert fetch["cost_usd"] == pytest.approx(10 * 0.003)
    assert fetch["endpoint"] == "unwrangle"


def test_fallback_provider_records_apify(initialized_db: Path) -> None:
    primary = UnwrangleClient("k", transport=FakeGetTransport([http(500)]), sleep=lambda _: None)
    fallback = ApifyClient(
        "t", transport=FakePostTransport([get_resp(apify_items(2))]), sleep=lambda _: None
    )
    chain = ReviewProviderChain([primary, fallback])

    result = fetch_reviews(ASIN, run_id=_new_run(), provider=chain, config=DeliumConfig())

    assert result.provider == "apify"
    assert len(result.reviews) == 2
    with get_connection() as conn:
        fetch = repository.latest_raw_fetch(conn, "reviews", f"reviews:asin:{ASIN}")
    assert fetch is not None and fetch["endpoint"] == "apify"


def test_zero_reviews_still_logs_and_returns_empty(initialized_db: Path) -> None:
    client, transport = _unwrangle([get_resp(unwrangle_body(0))])
    result = fetch_reviews(ASIN, run_id=_new_run(), provider=client, config=DeliumConfig())

    assert result.from_cache is False
    assert result.reviews == []
    with get_connection() as conn:
        # Fetch still logged; product stub created; no review rows.
        assert repository.latest_raw_fetch(conn, "reviews", f"reviews:asin:{ASIN}") is not None
        assert repository.get_product(conn, ASIN) is not None
        assert repository.get_reviews_for_asin(conn, ASIN) == []


def test_api_failure_propagates(initialized_db: Path) -> None:
    client, _ = _unwrangle([http(500)])
    with pytest.raises(ProviderResponseError):
        fetch_reviews(ASIN, run_id=_new_run(), provider=client, config=DeliumConfig())
