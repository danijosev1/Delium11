"""External data provider adapters.

Nothing outside this package calls a provider API directly — `ingestion/` is the
only consumer. Adapters contain provider logic and normalization only; caching
and analysis live elsewhere.

Implemented: Keepa. Not yet: DataForSEO, review provider (docs/data-layer.md §1).
"""

from delium.providers.base import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from delium.providers.dataforseo import (
    DataForSeoClient,
    DataForSeoFetch,
    KeywordVolume,
    SerpItem,
)
from delium.providers.keepa import KeepaClient, KeepaFetch, NormalizedProduct, PriceBsrPoint
from delium.providers.reviews import (
    ApifyClient,
    NormalizedReview,
    ReviewFetch,
    ReviewProviderChain,
    UnwrangleClient,
    build_review_provider,
)

__all__ = [
    "ApifyClient",
    "DataForSeoClient",
    "DataForSeoFetch",
    "KeepaClient",
    "KeepaFetch",
    "KeywordVolume",
    "NormalizedProduct",
    "NormalizedReview",
    "PriceBsrPoint",
    "ProviderAuthError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderNetworkError",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "ReviewFetch",
    "ReviewProviderChain",
    "SerpItem",
    "UnwrangleClient",
    "build_review_provider",
]
