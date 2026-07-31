from __future__ import annotations

from pathlib import Path

import pytest

from dataforseo_support import (
    SEED,
    FakePostTransport,
    http,
    ok,
    related_body,
    serp_body,
    volume_body,
)
from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.ingestion import fetch_keywords
from delium.providers.base import ProviderResponseError
from delium.providers.dataforseo import DataForSeoClient


# The three calls fetch_keywords makes, in order: volume, related, serp.
def _full_run() -> list[object]:
    return [ok(volume_body(9400)), ok(related_body()), ok(serp_body())]


def _client(results: list[object]) -> tuple[DataForSeoClient, FakePostTransport]:
    transport = FakePostTransport(results)
    return DataForSeoClient("l", "p", transport=transport, sleep=lambda _: None), transport


def _new_run() -> str:
    with get_connection() as conn:
        return repository.insert_run(conn, command="fetch.keywords", input_=SEED)


def _age_out(endpoint_key: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE raw_fetches SET fetched_at = '2000-01-01 00:00:00' WHERE request_key = ?",
            (endpoint_key,),
        )


def test_miss_fetches_normalizes_and_stores(initialized_db: Path) -> None:
    client, transport = _client(_full_run())
    result = fetch_keywords(SEED, run_id=_new_run(), client=client, config=DeliumConfig())

    assert result.seed == SEED
    assert result.seed_volume == 9400
    assert result.from_cache is False
    assert len(result.related) == 3
    assert [i.asin for i in result.serp] == ["B0AAA00001", "B0BBB00002", "B0CCC00003"]
    assert result.cost_usd == pytest.approx(0.024 + 0.02 + 0.006)
    assert transport.call_count == 3

    with get_connection() as conn:
        seed_kw = repository.get_keyword(conn, SEED)
        assert seed_kw is not None and seed_kw["volume"] == 9400
        assert repository.get_keyword(conn, "freezer tray silicone") is not None
        serps = repository.get_serp_rankings(conn, SEED)
    assert len(serps) == 3


def test_serp_storage_records_positions_and_sponsored(initialized_db: Path) -> None:
    client, _ = _client(_full_run())
    fetch_keywords(SEED, run_id=_new_run(), client=client, config=DeliumConfig())

    with get_connection() as conn:
        serps = repository.get_serp_rankings(conn, SEED)

    assert [s["asin"] for s in serps] == ["B0AAA00001", "B0BBB00002", "B0CCC00003"]
    assert [s["position"] for s in serps] == [1, 2, 3]
    by_asin = {s["asin"]: s["sponsored"] for s in serps}
    assert by_asin["B0AAA00001"] == 0
    assert by_asin["B0BBB00002"] == 1  # amazon_paid → sponsored


def test_cache_hit_skips_provider(initialized_db: Path) -> None:
    client, transport = _client(_full_run())
    config = DeliumConfig()

    first = fetch_keywords(SEED, run_id=_new_run(), client=client, config=config)
    second = fetch_keywords(SEED, run_id=_new_run(), client=client, config=config)

    assert first.from_cache is False
    assert second.from_cache is True
    assert second.seed_volume == 9400  # re-normalized from cached payload
    assert len(second.related) == 3
    assert transport.call_count == 3  # nothing new fetched


def test_stale_serp_refetches_only_serp(initialized_db: Path) -> None:
    client, transport = _client(
        [ok(volume_body()), ok(related_body()), ok(serp_body()), ok(serp_body())]
    )
    config = DeliumConfig()

    fetch_keywords(SEED, run_id=_new_run(), client=client, config=config)
    _age_out(f"dataforseo:serp:{SEED}")  # volume+related stay fresh
    result = fetch_keywords(SEED, run_id=_new_run(), client=client, config=config)

    assert result.from_cache is False  # serp was refetched
    assert transport.call_count == 4  # 3 + 1 serp refetch


def test_force_refetches_all(initialized_db: Path) -> None:
    client, transport = _client(_full_run() + _full_run())
    config = DeliumConfig()

    fetch_keywords(SEED, run_id=_new_run(), client=client, config=config)
    fetch_keywords(SEED, run_id=_new_run(), client=client, config=config, force=True)

    assert transport.call_count == 6


def test_cost_tracking_recorded_in_raw_fetches(initialized_db: Path) -> None:
    client, _ = _client(_full_run())
    fetch_keywords(SEED, run_id=_new_run(), client=client, config=DeliumConfig())

    with get_connection() as conn:
        volume_fetch = repository.latest_raw_fetch(conn, "dataforseo", f"dataforseo:volume:{SEED}")
        serp_fetch = repository.latest_raw_fetch(conn, "dataforseo", f"dataforseo:serp:{SEED}")

    assert volume_fetch is not None and volume_fetch["cost_usd"] == pytest.approx(0.024)
    assert serp_fetch is not None and serp_fetch["cost_usd"] == pytest.approx(0.006)


def test_api_failure_propagates(initialized_db: Path) -> None:
    client, _ = _client([http(500)])
    with pytest.raises(ProviderResponseError):
        fetch_keywords(SEED, run_id=_new_run(), client=client, config=DeliumConfig())
