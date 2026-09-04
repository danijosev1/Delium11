"""Seeding helpers + a fake review provider for validation pipeline/CLI tests.

Writes marketplace-scoped raw_fetches + extracted rows directly (as the
marketplace-aware ingestion would) so validation tests run over realistic stored
state without any network. Review rows carry a fresh raw_fetch so `fetch_reviews`
cache-hits by default (proving no provider call is made).
"""

from __future__ import annotations

import sqlite3

from delium.database import repository
from delium.providers.reviews import NormalizedReview, ReviewFetch


def seed_reviews(
    conn: sqlite3.Connection,
    run_id: str,
    asin: str,
    *,
    n: int = 40,
    stars: tuple[int, ...] = (5, 4, 3, 2, 1),
    fresh: bool = True,
) -> list[str]:
    """Seed `n` persisted reviews for `asin` behind a fresh reviews raw_fetch.
    Returns the review ids (so themes can cite real ones)."""
    fid = repository.insert_raw_fetch(
        conn,
        run_id=run_id,
        provider="reviews",
        endpoint="unwrangle",
        request_key=f"reviews:asin:{asin}",
        payload={"asin": asin},
        cost_usd=0.01,
    )
    if repository.get_product(conn, asin) is None:
        repository.upsert_product(conn, asin=asin, fetch_id=fid)
    ids: list[str] = []
    for i in range(n):
        rid = f"{asin}-R{i}"
        ids.append(rid)
        repository.insert_review(
            conn,
            review_id=rid,
            asin=asin,
            fetch_id=fid,
            stars=stars[i % len(stars)],
            review_date=f"2026-06-{(i % 27) + 1:02d}",
        )
    if not fresh:
        conn.execute(
            "UPDATE raw_fetches SET fetched_at = '2000-01-01 00:00:00' WHERE request_key = ?",
            (f"reviews:asin:{asin}",),
        )
    return ids


def seed_review_theme(
    conn: sqlite3.Connection,
    run_id: str,
    asin: str,
    *,
    kind: str = "complaint",
    theme: str = "leaks when frozen",
    quote_review_ids: list[str],
    frequency_pct: float | None = None,
    severity: int | None = None,
) -> str:
    return repository.insert_review_theme(
        conn,
        run_id=run_id,
        asin=asin,
        kind=kind,
        theme=theme,
        quote_review_ids=quote_review_ids,
        frequency_pct=frequency_pct,
        severity=severity,
    )


class FakeReviewProvider:
    """A ReviewSource that records the ASINs it was asked for — so tests can
    assert a fresh cache made NO provider call (calls stays empty)."""

    def __init__(self, n: int = 5, *, cost_per_review: float = 0.003) -> None:
        self.n = n
        self.cost_per_review = cost_per_review
        self.calls: list[str] = []

    def fetch_reviews(self, asin: str) -> ReviewFetch:
        self.calls.append(asin)
        reviews = [
            NormalizedReview(
                review_id=f"{asin}-F{i}",
                asin=asin,
                stars=(i % 5) + 1,
                title=None,
                body=None,
                verified_purchase=True,
                review_date=f"2026-05-{(i % 27) + 1:02d}",
                helpful_votes=0,
            )
            for i in range(self.n)
        ]
        return ReviewFetch(
            provider="unwrangle",
            http_status=200,
            cost_usd=self.cost_per_review * self.n,
            reviews=reviews,
            raw={"asin": asin},
        )


class SpyKeepaFactory:
    """A keepa factory returning a KeepaClient over a FakeTransport, recording
    every provider call so a fresh cache can be shown to skip the provider."""

    def __init__(self, bodies: list[object]) -> None:
        from delium.providers.keepa import KeepaClient
        from keepa_support import FakeTransport

        self._transport = FakeTransport(bodies)
        self._client = KeepaClient(
            "k", transport=self._transport, sleep=lambda _: None, marketplace="US"
        )

    @property
    def call_count(self) -> int:
        return self._transport.call_count

    def __call__(self, marketplace: str) -> object:
        from delium.providers.keepa import KeepaClient

        return KeepaClient(
            "k", transport=self._transport, sleep=lambda _: None, marketplace=marketplace
        )
