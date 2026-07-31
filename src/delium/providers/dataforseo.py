"""DataForSEO provider adapter.

Talks to the DataForSEO Labs Amazon endpoints (search volume, related keywords,
reverse-ASIN ranked keywords) and the Amazon merchant SERP endpoint, then
normalizes each response into our database-shaped types. Same shape as the Keepa
adapter: an injectable transport, retry/error handling, and pure normalization —
no caching, no analysis.

DataForSEO is pay-as-you-go; each response reports a `cost` (USD) which the
adapter surfaces so ingestion can record spend (docs/data-economics.md §2).
Requests are POST + HTTP Basic auth (login/password).
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from delium.config.secrets import get_secrets
from delium.providers.base import (
    HttpResult,
    PostTransport,
    ProviderAuthError,
    ProviderConfigError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderResponseError,
    UrllibTransport,
)
from delium.utils.logging import get_logger

log = get_logger(__name__)

_BASE_URL = "https://api.dataforseo.com/v3/"

# Endpoint paths (all POST, "live" mode).
_PATH_VOLUME = "dataforseo_labs/amazon/bulk_search_volume/live"
_PATH_RELATED = "dataforseo_labs/amazon/related_keywords/live"
_PATH_RANKED = "dataforseo_labs/amazon/ranked_keywords/live"
_PATH_SERP = "merchant/amazon/products/live/advanced"

# US marketplace defaults.
_LOCATION_CODE = 2840  # United States
_LANGUAGE_CODE = "en_US"

# DataForSEO status codes in [20000, 30000) are success.
_SUCCESS_MIN = 20000
_SUCCESS_MAX = 30000

# Only these SERP item types carry a product ASIN we care about.
_SERP_ORGANIC = "amazon_serp"
_SERP_PAID = "amazon_paid"

_MAX_RETRIES = 2
_BACKOFF_SECONDS = (5.0, 25.0)


# ---------------------------------------------------------------------------
# Normalized output types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class KeywordVolume:
    phrase: str
    volume: int | None


@dataclass(frozen=True)
class SerpItem:
    asin: str
    position: int
    sponsored: bool
    title: str | None = None
    price_cents: int | None = None


@dataclass(frozen=True)
class DataForSeoFetch:
    """One endpoint call's result. Only the relevant list is populated."""

    http_status: int
    cost_usd: float
    raw: dict[str, Any]
    keywords: list[KeywordVolume] = field(default_factory=list)
    serp: list[SerpItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helpers / normalization (operate on a raw response body)
# ---------------------------------------------------------------------------
def normalize_phrase(phrase: str) -> str:
    """Lowercase, trim, and collapse internal whitespace — done in one place."""
    return " ".join(phrase.split()).lower()


def _nonneg_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return None
    return ivalue if ivalue >= 0 else None


def _price_to_cents(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None


def response_cost(body: dict[str, Any]) -> float:
    cost = body.get("cost")
    try:
        return float(cost) if cost is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _items(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull tasks[0].result[0].items[], raising on a task-level error status."""
    tasks = body.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ProviderResponseError("DataForSEO response missing 'tasks'.")
    task = tasks[0]
    status = task.get("status_code")
    if isinstance(status, int) and not (_SUCCESS_MIN <= status < _SUCCESS_MAX):
        raise ProviderResponseError(f"DataForSEO task error {status}: {task.get('status_message')}")
    result = task.get("result")
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        return []
    items = result[0].get("items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def normalize_volume(body: dict[str, Any]) -> list[KeywordVolume]:
    """bulk_search_volume items: {keyword, search_volume}."""
    out: list[KeywordVolume] = []
    for item in _items(body):
        phrase = item.get("keyword")
        if not phrase:
            continue
        volume = _nonneg_int(item.get("search_volume"))
        out.append(KeywordVolume(normalize_phrase(str(phrase)), volume))
    return out


def normalize_keyword_data(body: dict[str, Any]) -> list[KeywordVolume]:
    """related/ranked keywords: keyword_data.{keyword, keyword_info.search_volume}."""
    out: list[KeywordVolume] = []
    for item in _items(body):
        data = item.get("keyword_data")
        if not isinstance(data, dict):
            continue
        phrase = data.get("keyword")
        if not phrase:
            continue
        info = data.get("keyword_info")
        volume = _nonneg_int(info.get("search_volume")) if isinstance(info, dict) else None
        out.append(KeywordVolume(normalize_phrase(str(phrase)), volume))
    return out


def normalize_serp(body: dict[str, Any]) -> list[SerpItem]:
    """merchant SERP items: {type, rank_absolute, data_asin, title, price_from}."""
    out: list[SerpItem] = []
    for item in _items(body):
        item_type = item.get("type")
        if item_type not in (_SERP_ORGANIC, _SERP_PAID):
            continue
        asin = item.get("data_asin") or item.get("asin")
        position = _nonneg_int(item.get("rank_absolute"))
        if not asin or position is None:
            continue
        out.append(
            SerpItem(
                asin=str(asin),
                position=position,
                sponsored=(item_type == _SERP_PAID),
                title=item.get("title"),
                price_cents=_price_to_cents(item.get("price_from")),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class DataForSeoClient:
    """Retrying client for the DataForSEO Amazon endpoints."""

    def __init__(
        self,
        login: str,
        password: str,
        *,
        transport: PostTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not login or not password:
            raise ProviderConfigError("DataForSEO login and password are required.")
        token = base64.b64encode(f"{login}:{password}".encode()).decode()
        self._auth_header = f"Basic {token}"
        self._transport = transport or UrllibTransport()
        self._sleep = sleep

    @classmethod
    def from_env(cls, *, transport: PostTransport | None = None) -> DataForSeoClient:
        secrets = get_secrets()
        if secrets.dataforseo_login is None or secrets.dataforseo_password is None:
            raise ProviderConfigError(
                "DELIUM_DATAFORSEO_LOGIN / DELIUM_DATAFORSEO_PASSWORD are not set."
            )
        return cls(
            secrets.dataforseo_login.get_secret_value(),
            secrets.dataforseo_password.get_secret_value(),
            transport=transport,
        )

    # -- public endpoint methods --------------------------------------------
    def search_volume(self, keywords: Sequence[str]) -> DataForSeoFetch:
        if not keywords:
            raise ValueError("search_volume requires at least one keyword.")
        body = [
            {
                "keywords": [normalize_phrase(k) for k in keywords],
                "location_code": _LOCATION_CODE,
                "language_code": _LANGUAGE_CODE,
            }
        ]
        result = self._post(_PATH_VOLUME, body)
        return DataForSeoFetch(
            http_status=result.status,
            cost_usd=response_cost(result.body),
            raw=result.body,
            keywords=normalize_volume(result.body),
        )

    def related_keywords(self, seed: str, *, depth: int = 2, limit: int = 100) -> DataForSeoFetch:
        body = [
            {
                "keyword": normalize_phrase(seed),
                "location_code": _LOCATION_CODE,
                "language_code": _LANGUAGE_CODE,
                "depth": depth,
                "limit": limit,
            }
        ]
        result = self._post(_PATH_RELATED, body)
        return DataForSeoFetch(
            http_status=result.status,
            cost_usd=response_cost(result.body),
            raw=result.body,
            keywords=normalize_keyword_data(result.body),
        )

    def ranked_keywords(self, asin: str, *, limit: int = 100) -> DataForSeoFetch:
        """Reverse-ASIN: keywords a given ASIN ranks for."""
        body = [
            {
                "asin": asin,
                "location_code": _LOCATION_CODE,
                "language_code": _LANGUAGE_CODE,
                "limit": limit,
            }
        ]
        result = self._post(_PATH_RANKED, body)
        return DataForSeoFetch(
            http_status=result.status,
            cost_usd=response_cost(result.body),
            raw=result.body,
            keywords=normalize_keyword_data(result.body),
        )

    def serp(self, keyword: str) -> DataForSeoFetch:
        body = [
            {
                "keyword": normalize_phrase(keyword),
                "location_code": _LOCATION_CODE,
                "language_code": _LANGUAGE_CODE,
            }
        ]
        result = self._post(_PATH_SERP, body)
        return DataForSeoFetch(
            http_status=result.status,
            cost_usd=response_cost(result.body),
            raw=result.body,
            serp=normalize_serp(result.body),
        )

    # -- transport with retries ---------------------------------------------
    def _post(self, path: str, body: Any) -> HttpResult:
        url = _BASE_URL + path
        headers = {"Authorization": self._auth_header}
        last_status = 0
        for attempt in range(_MAX_RETRIES + 1):
            try:
                result = self._transport.post_json(url, body, headers)
            except ProviderNetworkError:
                if attempt < _MAX_RETRIES:
                    self._sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
                    continue
                raise
            last_status = result.status

            if result.status == 200:
                return result
            if result.status in (401, 403):
                raise ProviderAuthError(f"DataForSEO auth rejected (HTTP {result.status}).")
            if result.status == 429:
                if attempt < _MAX_RETRIES:
                    self._sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
                    continue
                raise ProviderRateLimitError("DataForSEO rate limit (HTTP 429).")
            if result.status >= 500:
                if attempt < _MAX_RETRIES:
                    self._sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
                    continue
                break
            raise ProviderResponseError(f"DataForSEO returned HTTP {result.status}.")

        raise ProviderResponseError(
            f"DataForSEO request failed after retries (last status {last_status})."
        )
