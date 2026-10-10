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
    _age_out(f"dataforseo:serp:US:{SEED}")  # volume+related stay fresh
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
        volume_fetch = repository.latest_raw_fetch(
            conn, "dataforseo", f"dataforseo:volume:US:{SEED}"
        )
        serp_fetch = repository.latest_raw_fetch(conn, "dataforseo", f"dataforseo:serp:US:{SEED}")

    assert volume_fetch is not None and volume_fetch["cost_usd"] == pytest.approx(0.024)
    assert serp_fetch is not None and serp_fetch["cost_usd"] == pytest.approx(0.006)


def test_api_failure_propagates(initialized_db: Path) -> None:
    client, _ = _client([http(500)])
    with pytest.raises(ProviderResponseError):
        fetch_keywords(SEED, run_id=_new_run(), client=client, config=DeliumConfig())


def _uk_client(results: list[object]) -> tuple[DataForSeoClient, FakePostTransport]:
    transport = FakePostTransport(results)
    client = DataForSeoClient("l", "p", transport=transport, sleep=lambda _: None, marketplace="UK")
    return client, transport


def test_non_us_skips_labs_but_runs_merchant_serp(initialized_db: Path) -> None:
    """UK: the Labs volume/related calls are skipped entirely (US-only — no
    request, no cost), but the Merchant Amazon SERP still runs. This is the fix
    for the 40501 'Invalid Field: location_code' failure on UK scans."""
    # Only the SERP body is queued — if fetch_keywords tried a Labs call it would
    # consume this and the assertions below would break.
    client, transport = _uk_client([ok(serp_body())])
    result = fetch_keywords(SEED, run_id=_new_run(), client=client, config=DeliumConfig())

    assert transport.call_count == 1  # SERP only — the two Labs calls were skipped
    _url, body = transport.calls[0]
    assert body[0]["location_code"] == 2826  # UK merchant SERP
    assert result.marketplace == "UK"
    assert result.seed_volume is None  # Labs volume skipped → unknown, not fabricated
    assert result.related == []
    assert [i.asin for i in result.serp] == ["B0AAA00001", "B0BBB00002", "B0CCC00003"]
    assert result.cost_usd == pytest.approx(0.006)  # only the SERP was billed

    with get_connection() as conn:
        # The seed keyword row exists (FK target for serp_rankings) even though the
        # Labs volume call that normally creates it was skipped — it is attributed
        # to the SERP fetch, which IS a logged raw_fetch.
        seed_kw = repository.get_keyword(conn, SEED, "UK")
        assert seed_kw is not None and seed_kw["volume"] is None
        serps = repository.get_serp_rankings(conn, SEED, "UK")
    assert len(serps) == 3


def test_fully_unsupported_marketplace_is_a_quiet_noop(initialized_db: Path) -> None:
    """AU: neither Labs nor the Merchant SERP cover it — nothing is called, nothing
    is billed, and no rows are written (no crash)."""
    transport = FakePostTransport([ok(serp_body())])
    client = DataForSeoClient("l", "p", transport=transport, sleep=lambda _: None, marketplace="AU")
    result = fetch_keywords(SEED, run_id=_new_run(), client=client, config=DeliumConfig())

    assert transport.call_count == 0
    assert result.serp == [] and result.related == [] and result.cost_usd == 0.0
    with get_connection() as conn:
        assert repository.get_keyword(conn, SEED, "AU") is None
