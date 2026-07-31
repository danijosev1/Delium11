"""Data pipeline orchestration: fetch → normalize → cache (docs/data-layer.md §3).

The only caller of `providers/`. Owns cache freshness, persistence, and the
compact views handed to the analysis/agent layers.

Implemented: product fetch (Keepa), keyword fetch (DataForSEO). Not yet: review
ingestion, dataset assembly for validation runs.
"""

from delium.ingestion.keywords import KeywordFetchResult, fetch_keywords
from delium.ingestion.products import ProductView, fetch_product

__all__ = [
    "KeywordFetchResult",
    "ProductView",
    "fetch_keywords",
    "fetch_product",
]
