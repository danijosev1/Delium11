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

__all__ = [
    "DataForSeoClient",
    "DataForSeoFetch",
    "KeepaClient",
    "KeepaFetch",
    "KeywordVolume",
    "NormalizedProduct",
    "PriceBsrPoint",
    "ProviderAuthError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderNetworkError",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "SerpItem",
]
