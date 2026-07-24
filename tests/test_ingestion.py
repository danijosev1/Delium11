from __future__ import annotations

from pathlib import Path

import pytest

from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.ingestion import fetch_product
from delium.providers.base import ProviderResponseError
from delium.providers.keepa import KeepaClient
from keepa_support import DEFAULT_ASIN, FakeTransport, http, keepa_product_body, ok


def _make_client(results: list[object]) -> tuple[KeepaClient, FakeTransport]:
    transport = FakeTransport(results)
    return KeepaClient("k", transport=transport, sleep=lambda _: None), transport


def _new_run() -> str:
    with get_connection() as conn:
        return repository.insert_run(conn, command="fetch.product", input_=DEFAULT_ASIN)


def _age_out_cache(asin: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE raw_fetches SET fetched_at = '2000-01-01 00:00:00' WHERE request_key = ?",
            (f"keepa:product:{asin}",),
        )


def test_miss_fetches_stores_and_returns(initialized_db: Path) -> None:
    client, transport = _make_client([ok(keepa_product_body())])
    view = fetch_product(DEFAULT_ASIN, run_id=_new_run(), client=client, config=DeliumConfig())

    assert view is not None
    assert view.found is True
    assert view.from_cache is False
    assert view.title == "Test Silicone Tray"
    assert view.dims == {"length_mm": 200, "width_mm": 150, "height_mm": 40}
    assert view.weight_g == 300
    assert view.latest_price_cents == 2099  # most recent day
    assert view.latest_bsr == 1600
    assert view.history_points == 2
    assert transport.call_count == 1

    # Extracted tables were populated.
    with get_connection() as conn:
        assert repository.get_product(conn, DEFAULT_ASIN) is not None
        assert len(repository.get_price_bsr_history(conn, DEFAULT_ASIN)) == 2


def test_cache_hit_does_not_call_provider(initialized_db: Path) -> None:
    client, transport = _make_client([ok(keepa_product_body())])
    config = DeliumConfig()

    first = fetch_product(DEFAULT_ASIN, run_id=_new_run(), client=client, config=config)
    second = fetch_product(DEFAULT_ASIN, run_id=_new_run(), client=client, config=config)

    assert first is not None and first.from_cache is False
    assert second is not None and second.from_cache is True
    assert transport.call_count == 1  # provider untouched on the cache hit


def test_stale_cache_refetches(initialized_db: Path) -> None:
    client, transport = _make_client([ok(keepa_product_body()), ok(keepa_product_body())])
    config = DeliumConfig()

    fetch_product(DEFAULT_ASIN, run_id=_new_run(), client=client, config=config)
    _age_out_cache(DEFAULT_ASIN)
    second = fetch_product(DEFAULT_ASIN, run_id=_new_run(), client=client, config=config)

    assert second is not None and second.from_cache is False
    assert transport.call_count == 2  # stale entry forced a refetch


def test_force_bypasses_fresh_cache(initialized_db: Path) -> None:
    client, transport = _make_client([ok(keepa_product_body()), ok(keepa_product_body())])
    config = DeliumConfig()

    fetch_product(DEFAULT_ASIN, run_id=_new_run(), client=client, config=config)
    fetch_product(DEFAULT_ASIN, run_id=_new_run(), client=client, config=config, force=True)

    assert transport.call_count == 2


def test_not_found_logs_fetch_and_returns_absent_view(initialized_db: Path) -> None:
    client, transport = _make_client([ok(keepa_product_body(found=False))])
    view = fetch_product("B0MISSING01", run_id=_new_run(), client=client, config=DeliumConfig())

    assert view is not None
    assert view.found is False
    assert transport.call_count == 1

    with get_connection() as conn:
        # The fetch is still logged (audit trail), but no product row exists.
        assert repository.latest_raw_fetch(conn, "keepa", "keepa:product:B0MISSING01") is not None
        assert repository.get_product(conn, "B0MISSING01") is None


def test_api_failure_propagates(initialized_db: Path) -> None:
    client, _ = _make_client([http(500)])
    with pytest.raises(ProviderResponseError):
        fetch_product(DEFAULT_ASIN, run_id=_new_run(), client=client, config=DeliumConfig())
