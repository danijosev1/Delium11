"""Data pipeline orchestration: fetch → normalize → cache (docs/data-layer.md §3).

The only caller of `providers/`. Owns cache freshness, persistence, and the
compact views handed to the analysis/agent layers.

Implemented: product fetch (Keepa), keyword fetch (DataForSEO). Not yet: review
ingestion, dataset assembly for validation runs.
"""

from delium.ingestion.cross_market import (
    CrossMarketCandidate,
    CrossMarketProvenance,
    build_source,
    build_target,
    discover_cross_market,
    generate_candidates,
)
from delium.ingestion.keywords import KeywordFetchResult, fetch_keywords
from delium.ingestion.products import ProductView, fetch_product
from delium.ingestion.reviews import ReviewFetchResult, fetch_reviews

__all__ = [
    "CrossMarketCandidate",
    "CrossMarketProvenance",
    "KeywordFetchResult",
    "ProductView",
    "ReviewFetchResult",
    "build_source",
    "build_target",
    "discover_cross_market",
    "fetch_keywords",
    "fetch_product",
    "fetch_reviews",
    "generate_candidates",
]
