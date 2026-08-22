"""Keyword ingestion: the cache-first `fetch_keywords` flow (docs/data-layer.md §3).

For a seed keyword this assembles three DataForSEO calls — the seed's search
volume, related keywords, and the Amazon SERP — each cached independently by its
own request key and TTL (keyword vs SERP). The raw response is the source of
truth: on a cache hit it is re-normalized from the stored payload without a
provider call; on a miss it is fetched, logged, normalized, and the extracted
keyword/serp rows are written.

The provider is the only thing that touches the network. No analysis here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.ingestion.freshness import is_fresh
from delium.providers.dataforseo import (
    DataForSeoClient,
    DataForSeoFetch,
    KeywordVolume,
    SerpItem,
    normalize_keyword_data,
    normalize_phrase,
    normalize_serp,
    normalize_volume,
)
from delium.utils.logging import get_logger

log = get_logger(__name__)

_PROVIDER = "dataforseo"


@dataclass(frozen=True)
class KeywordFetchResult:
    seed: str
    seed_volume: int | None
    from_cache: bool
    marketplace: str = "US"
    related: list[KeywordVolume] = field(default_factory=list)
    serp: list[SerpItem] = field(default_factory=list)
    cost_usd: float = 0.0


def keyword_request_key(marketplace: str, endpoint: str, seed: str) -> str:
    """Marketplace-scoped cache key: a US keyword/SERP lookup never satisfies an
    IN one. `endpoint` is 'volume' | 'related' | 'serp'."""
    return f"{_PROVIDER}:{endpoint}:{marketplace}:{seed}"


# Backwards-compatible private alias used throughout this module.
_request_key = keyword_request_key


@dataclass(frozen=True)
class _StepOutcome:
    body: dict[str, object]
    from_cache: bool
    cost_usd: float
    fetch_id: str | None  # None on a cache hit (already logged previously)


def _cached_or_fetch(
    *,
    run_id: str,
    endpoint: str,
    request_key: str,
    ttl: timedelta,
    force: bool,
    call: Callable[[], DataForSeoFetch],
) -> _StepOutcome:
    """Read-through one endpoint: fresh cache → stored payload, else fetch+log."""
    if not force:
        with get_connection() as conn:
            latest = repository.latest_raw_fetch(conn, _PROVIDER, request_key)
            if latest is not None and is_fresh(latest["fetched_at"], ttl):
                return _StepOutcome(
                    body=json.loads(latest["payload"]),
                    from_cache=True,
                    cost_usd=0.0,
                    fetch_id=None,
                )

    fetch = call()
    with get_connection() as conn:
        fetch_id = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider=_PROVIDER,
            endpoint=endpoint,
            request_key=request_key,
            payload=fetch.raw,
            cost_usd=fetch.cost_usd,
        )
    return _StepOutcome(fetch.raw, from_cache=False, cost_usd=fetch.cost_usd, fetch_id=fetch_id)


def fetch_keywords(
    seed: str,
    *,
    run_id: str,
    client: DataForSeoClient,
    config: DeliumConfig,
    force: bool = False,
) -> KeywordFetchResult:
    """Fetch a seed keyword's volume, related keywords, and SERP — cache-first,
    scoped to the client's marketplace."""
    seed_norm = normalize_phrase(seed)
    marketplace = client.marketplace
    kw_ttl = timedelta(days=config.cache.keyword_ttl_days)
    serp_ttl = timedelta(days=config.cache.serp_ttl_days)
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    # 1. Seed search volume — runs first so the seed keyword row exists before
    #    any serp_ranking (FK) references it.
    volume_step = _cached_or_fetch(
        run_id=run_id,
        endpoint="bulk_search_volume",
        request_key=_request_key(marketplace, "volume", seed_norm),
        ttl=kw_ttl,
        force=force,
        call=lambda: client.search_volume([seed_norm]),
    )
    seed_volume = _seed_volume(normalize_volume(volume_step.body), seed_norm)
    if volume_step.fetch_id is not None:
        with get_connection() as conn:
            repository.upsert_keyword(
                conn,
                phrase=seed_norm,
                fetch_id=volume_step.fetch_id,
                marketplace=marketplace,
                volume=seed_volume,
            )

    # 2. Related keywords.
    related_step = _cached_or_fetch(
        run_id=run_id,
        endpoint="related_keywords",
        request_key=_request_key(marketplace, "related", seed_norm),
        ttl=kw_ttl,
        force=force,
        call=lambda: client.related_keywords(seed_norm),
    )
    related = normalize_keyword_data(related_step.body)
    if related_step.fetch_id is not None:
        with get_connection() as conn:
            for kw in related:
                repository.upsert_keyword(
                    conn,
                    phrase=kw.phrase,
                    fetch_id=related_step.fetch_id,
                    marketplace=marketplace,
                    volume=kw.volume,
                )

    # 3. SERP.
    serp_step = _cached_or_fetch(
        run_id=run_id,
        endpoint="amazon_serp",
        request_key=_request_key(marketplace, "serp", seed_norm),
        ttl=serp_ttl,
        force=force,
        call=lambda: client.serp(seed_norm),
    )
    serp = normalize_serp(serp_step.body)
    if serp_step.fetch_id is not None:
        with get_connection() as conn:
            for item in serp:
                repository.upsert_serp_ranking(
                    conn,
                    keyword_phrase=seed_norm,
                    asin=item.asin,
                    position=item.position,
                    sponsored=item.sponsored,
                    captured_on=today,
                    marketplace=marketplace,
                )

    from_cache = volume_step.from_cache and related_step.from_cache and serp_step.from_cache
    total_cost = volume_step.cost_usd + related_step.cost_usd + serp_step.cost_usd
    log.info(
        "fetch_keywords(%s): %d related, %d serp, cache=%s, cost=$%.4f",
        seed_norm,
        len(related),
        len(serp),
        from_cache,
        total_cost,
    )
    return KeywordFetchResult(
        seed=seed_norm,
        seed_volume=seed_volume,
        from_cache=from_cache,
        marketplace=marketplace,
        related=related,
        serp=serp,
        cost_usd=total_cost,
    )


def _seed_volume(volumes: list[KeywordVolume], seed_norm: str) -> int | None:
    for kw in volumes:
        if kw.phrase == seed_norm:
            return kw.volume
    return volumes[0].volume if volumes else None
