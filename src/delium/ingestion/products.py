"""Product ingestion: the cache-first `fetch_product` flow (docs/data-layer.md §3).

    check cache → if fresh, return DB data
                → else call Keepa → store raw_fetch → normalize
                  → store extracted tables → return product view

The provider is the only thing that touches the network; this module owns the
freshness decision, persistence, and building the view returned to callers. It
performs no analysis.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.ingestion.freshness import is_fresh
from delium.providers.keepa import KeepaClient, NormalizedProduct
from delium.utils.logging import get_logger

log = get_logger(__name__)

_PROVIDER = "keepa"
_ENDPOINT = "product"


@dataclass(frozen=True)
class ProductView:
    """What callers (and the CLI) get back from a product fetch."""

    asin: str
    found: bool
    from_cache: bool
    marketplace: str = "US"
    title: str | None = None
    brand: str | None = None
    category_path: str | None = None
    dims: dict[str, int] | None = None
    weight_g: int | None = None
    images_count: int | None = None
    gtin: str | None = None
    manufacturer: str | None = None
    latest_price_cents: int | None = None
    latest_bsr: int | None = None
    history_points: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0


def _request_key(marketplace: str, asin: str) -> str:
    # Marketplace-scoped so a US fetch and an IN fetch of the same ASIN never
    # share a cache entry (cross-market isolation).
    return f"{_PROVIDER}:{_ENDPOINT}:{marketplace}:{asin}"


def _latest(rows: list[sqlite3.Row], column: str) -> int | None:
    """Most recent non-null value of `column` across ascending-ordered history."""
    for row in reversed(rows):
        value = row[column]
        if value is not None:
            return int(value)
    return None


def _view_from_db(
    conn: sqlite3.Connection,
    asin: str,
    *,
    marketplace: str,
    from_cache: bool,
    tokens_used: int,
    cost_usd: float,
) -> ProductView | None:
    product = repository.get_product(conn, asin, marketplace)
    if product is None:
        return None
    history = repository.get_price_bsr_history(conn, asin)
    dims_json = product["dims_json"]
    dims = None
    if dims_json:
        parsed = json.loads(dims_json)
        dims = {k: int(v) for k, v in parsed.items()}
    return ProductView(
        asin=asin,
        found=True,
        from_cache=from_cache,
        marketplace=product["marketplace"],
        title=product["title"],
        brand=product["brand"],
        category_path=product["category_path"],
        dims=dims,
        weight_g=product["weight_g"],
        images_count=product["images_count"],
        gtin=product["gtin"],
        manufacturer=product["manufacturer"],
        latest_price_cents=_latest(history, "price_cents"),
        latest_bsr=_latest(history, "bsr"),
        history_points=len(history),
        tokens_used=tokens_used,
        cost_usd=cost_usd,
    )


def _store_normalized(conn: sqlite3.Connection, product: NormalizedProduct, fetch_id: str) -> None:
    repository.upsert_product(
        conn,
        asin=product.asin,
        fetch_id=fetch_id,
        marketplace=product.marketplace,
        title=product.title,
        brand=product.brand,
        category_path=product.category_path,
        dims=product.dims,
        weight_g=product.weight_g,
        size_tier=None,  # size-tier classification is analysis/fees, not ingestion
        images_count=product.images_count,
        amazon_on_listing=product.amazon_on_listing,
        gtin=product.gtin,
        manufacturer=product.manufacturer,
    )
    for point in product.history:
        repository.upsert_price_bsr_history(
            conn,
            asin=product.asin,
            captured_on=point.captured_on,
            price_cents=point.price_cents,
            bsr=point.bsr,
            offer_count=point.offer_count,
            review_count=point.review_count,
            rating=point.rating,
        )


def fetch_product(
    asin: str,
    *,
    run_id: str,
    client: KeepaClient,
    config: DeliumConfig,
    force: bool = False,
) -> ProductView | None:
    """Fetch a product cache-first from the client's marketplace. Returns a
    `ProductView`, or None if the ASIN could not be resolved (a not-found is
    still logged to raw_fetches). The cache key and stored rows carry the
    marketplace, so a US fetch never satisfies an IN request."""
    marketplace = client.marketplace
    request_key = _request_key(marketplace, asin)
    ttl = timedelta(hours=config.cache.product_ttl_hours)

    if not force:
        with get_connection() as conn:
            latest = repository.latest_raw_fetch(conn, _PROVIDER, request_key)
            if latest is not None and is_fresh(latest["fetched_at"], ttl):
                view = _view_from_db(
                    conn,
                    asin,
                    marketplace=marketplace,
                    from_cache=True,
                    tokens_used=0,
                    cost_usd=0.0,
                )
                if view is not None:
                    log.info("Cache hit for %s [%s] (fresh within TTL).", asin, marketplace)
                    return view

    log.info("Cache miss/stale for %s [%s] — calling Keepa.", asin, marketplace)
    fetch = client.fetch_products([asin])
    per_asin_tokens = fetch.per_asin_tokens(1)
    normalized = fetch.normalized.get(asin)

    with get_connection() as conn:
        fetch_id = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider=_PROVIDER,
            endpoint=_ENDPOINT,
            request_key=request_key,
            payload=fetch.raw_products.get(asin, {"asin": asin, "found": False}),
            cost_usd=0.0,  # Keepa is a flat subscription — marginal $ per call is 0
            tokens_used=per_asin_tokens,
            http_status=fetch.http_status,
        )
        if normalized is None:
            log.info("ASIN %s [%s] not found on Keepa — recorded in fetch log.", asin, marketplace)
            return ProductView(
                asin=asin,
                found=False,
                from_cache=False,
                marketplace=marketplace,
                tokens_used=per_asin_tokens,
                cost_usd=0.0,
            )
        _store_normalized(conn, normalized, fetch_id)
        return _view_from_db(
            conn,
            asin,
            marketplace=marketplace,
            from_cache=False,
            tokens_used=per_asin_tokens,
            cost_usd=0.0,
        )
