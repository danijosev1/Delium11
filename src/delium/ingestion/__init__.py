"""Data pipeline orchestration: fetch → normalize → cache (docs/data-layer.md §3).

The only caller of `providers/`. Owns cache freshness, persistence, and the
compact views handed to the analysis/agent layers.

Implemented: product fetch. Not yet: keyword/SERP/review ingestion, dataset
assembly for validation runs.
"""

from delium.ingestion.products import ProductView, fetch_product

__all__ = ["ProductView", "fetch_product"]
