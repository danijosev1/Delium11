"""Review ingestion: the cache-first `fetch_reviews` flow (docs/data-layer.md §3).

    check cache → if fresh, return DB reviews
                → else fetch (chain: Unwrangle → Apify) → store raw_fetch
                  → normalize → store reviews → return review objects

`reviews.asin` has a foreign key to `products`, so a minimal product row is
ensured before reviews are written (the standalone `fetch reviews` command may
run before a product has been fetched). No AI, no theme extraction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.ingestion.freshness import is_fresh
from delium.providers.reviews import NormalizedReview, ReviewFetch
from delium.utils.logging import get_logger

log = get_logger(__name__)

_PROVIDER = "reviews"


class ReviewSource(Protocol):
    """Anything that can fetch reviews for an ASIN (a client or a chain)."""

    def fetch_reviews(self, asin: str) -> ReviewFetch: ...


@dataclass(frozen=True)
class ReviewFetchResult:
    asin: str
    from_cache: bool
    provider: str | None  # which provider produced the data; None on a cache hit
    cost_usd: float
    reviews: list[NormalizedReview] = field(default_factory=list)


def _request_key(asin: str) -> str:
    return f"{_PROVIDER}:asin:{asin}"


def _reviews_from_db(conn: sqlite3.Connection, asin: str) -> list[NormalizedReview]:
    rows = repository.get_reviews_for_asin(conn, asin)
    return [
        NormalizedReview(
            review_id=row["review_id"],
            asin=row["asin"],
            stars=int(row["stars"]),
            title=row["title"],
            body=row["body"],
            verified_purchase=bool(row["verified"]),
            review_date=row["review_date"],
            helpful_votes=int(row["helpful_votes"]),
            reviewer_name=None,  # not persisted (no column); lives in the raw payload
        )
        for row in rows
    ]


def _store(
    conn: sqlite3.Connection, asin: str, fetch_id: str, reviews: Sequence[NormalizedReview]
) -> None:
    # Ensure the FK parent exists (a bare stub if the product hasn't been fetched).
    if repository.get_product(conn, asin) is None:
        repository.upsert_product(conn, asin=asin, fetch_id=fetch_id)
    for review in reviews:
        repository.insert_review(
            conn,
            review_id=review.review_id,
            asin=asin,
            fetch_id=fetch_id,
            stars=review.stars,
            review_date=review.review_date,
            title=review.title,
            body=review.body,
            verified=review.verified_purchase,
            helpful_votes=review.helpful_votes,
        )


def fetch_reviews(
    asin: str,
    *,
    run_id: str,
    provider: ReviewSource,
    config: DeliumConfig,
    force: bool = False,
) -> ReviewFetchResult:
    """Fetch a review sample for an ASIN, cache-first."""
    request_key = _request_key(asin)
    ttl = timedelta(days=config.cache.review_ttl_days)

    if not force:
        with get_connection() as conn:
            latest = repository.latest_raw_fetch(conn, _PROVIDER, request_key)
            if latest is not None and is_fresh(latest["fetched_at"], ttl):
                reviews = _reviews_from_db(conn, asin)
                log.info("Cache hit for reviews of %s (%d reviews).", asin, len(reviews))
                return ReviewFetchResult(
                    asin=asin, from_cache=True, provider=None, cost_usd=0.0, reviews=reviews
                )

    log.info("Cache miss/stale for reviews of %s — fetching.", asin)
    fetch = provider.fetch_reviews(asin)

    with get_connection() as conn:
        fetch_id = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider=_PROVIDER,
            endpoint=fetch.provider,  # which provider actually served it
            request_key=request_key,
            payload=fetch.raw,
            cost_usd=fetch.cost_usd,
            http_status=fetch.http_status,
        )
        _store(conn, asin, fetch_id, fetch.reviews)

    return ReviewFetchResult(
        asin=asin,
        from_cache=False,
        provider=fetch.provider,
        cost_usd=fetch.cost_usd,
        reviews=list(fetch.reviews),
    )
