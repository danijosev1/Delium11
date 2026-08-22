"""Keepa provider adapter.

Responsibilities (and only these): talk to the Keepa `/product` endpoint,
respect the token bucket, retry transient failures, and normalize the raw
response into our database-shaped types. No caching, no analysis, no scoring —
those live in `ingestion/` and `analysis/`.

Keepa pricing is a flat monthly subscription with a per-minute *token* bucket,
so the meaningful constraint is token pacing, not dollars (docs/data-economics.md
§2). Each response reports `tokensLeft`/`refillRate`; the client throttles
against those reactively and paces before large batches.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import ceil
from typing import Any

from delium.config.secrets import get_secrets
from delium.providers.base import (
    HttpResult,
    ProviderAuthError,
    ProviderConfigError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderResponseError,
    Transport,
    UrllibTransport,
)
from delium.utils.logging import get_logger

log = get_logger(__name__)

KEEPA_PRODUCT_URL = "https://api.keepa.com/product"

# Keepa timestamps are "Keepa minutes": minutes since the Keepa epoch.
# unix_seconds = (keepa_minutes + KEEPA_EPOCH_MINUTES) * 60
KEEPA_EPOCH_MINUTES = 21564000

# Keepa domain ids per marketplace (Keepa's fixed numbering). Keyed by the
# marketplace code strings used across ingestion/DB (== analysis Marketplace enum
# values). Providers stay decoupled from the analysis layer; this table is the
# provider-side half of the centralized marketplace model.
_KEEPA_DOMAINS: dict[str, int] = {
    "US": 1,
    "UK": 2,
    "CA": 6,
    "IN": 10,
    "AU": 13,
}
# Domain id for amazon.com (US). Keepa uses 1 for the US marketplace.
_DOMAIN_US = _KEEPA_DOMAINS["US"]


def keepa_domain(marketplace: str) -> int:
    """Keepa domain id for a marketplace code, or raise for an unsupported one."""
    try:
        return _KEEPA_DOMAINS[marketplace]
    except KeyError as exc:
        raise ProviderConfigError(
            f"Keepa: unsupported marketplace {marketplace!r} "
            f"(known: {', '.join(sorted(_KEEPA_DOMAINS))})."
        ) from exc


# csv[] positional indices we consume (Keepa's fixed layout).
_CSV_AMAZON = 0  # Amazon price, cents
_CSV_NEW = 1  # marketplace New price, cents
_CSV_SALES = 3  # sales rank (BSR)
_CSV_RATING = 16  # rating, 0-50 (i.e. 45 == 4.5 stars)
_CSV_COUNT_REVIEWS = 17  # review count

# Reactive throttling / retries.
_TOKENS_PER_PRODUCT_ESTIMATE = 4  # pre-flight estimate; corrected from responses
_MAX_TOKEN_WAIT_S = 300.0
_MAX_RETRIES = 2  # → 3 attempts total (docs/data-layer.md §1.1: "5xx → 2 retries")
_BACKOFF_SECONDS = (5.0, 25.0)


# ---------------------------------------------------------------------------
# Normalized output types (database-shaped; see docs/data-layer.md §2)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PriceBsrPoint:
    captured_on: str  # 'YYYY-MM-DD'
    price_cents: int | None = None
    bsr: int | None = None
    offer_count: int | None = None
    review_count: int | None = None
    rating: float | None = None


@dataclass(frozen=True)
class NormalizedProduct:
    asin: str
    marketplace: str
    title: str | None
    brand: str | None
    category_path: str | None
    dims: dict[str, int] | None
    weight_g: int | None
    images_count: int | None
    amazon_on_listing: bool
    gtin: str | None = None  # best barcode (EAN preferred, else UPC) — for identity
    manufacturer: str | None = None
    history: list[PriceBsrPoint] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KeepaFetch:
    """One `/product` call's result, split per requested ASIN."""

    http_status: int
    tokens_consumed: int
    tokens_left: int | None
    normalized: dict[str, NormalizedProduct]  # only ASINs actually found
    raw_products: dict[str, dict[str, Any]]  # every requested ASIN (for the fetch log)

    def per_asin_tokens(self, asin_count: int) -> int:
        if asin_count <= 0:
            return 0
        return max(1, self.tokens_consumed // asin_count)


# ---------------------------------------------------------------------------
# Time + csv parsing helpers (pure)
# ---------------------------------------------------------------------------
def keepa_minutes_to_datetime(km: int) -> datetime:
    return datetime.fromtimestamp((km + KEEPA_EPOCH_MINUTES) * 60, tz=UTC)


def keepa_minutes_to_date(km: int) -> str:
    return keepa_minutes_to_datetime(km).strftime("%Y-%m-%d")


def _csv_series(csv: Sequence[Any], index: int) -> list[tuple[str, int]]:
    """Return [(date, value)] for one csv metric, dropping Keepa's -1 sentinels."""
    if index >= len(csv):
        return []
    arr = csv[index]
    if not arr:
        return []
    out: list[tuple[str, int]] = []
    for i in range(0, len(arr) - 1, 2):
        km = arr[i]
        value = arr[i + 1]
        if value is None or value < 0:
            continue
        out.append((keepa_minutes_to_date(int(km)), int(value)))
    return out


def _positive_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return None
    return ivalue if ivalue > 0 else None


def _extract_dims(raw: dict[str, Any]) -> dict[str, int] | None:
    dims = {
        "length_mm": _positive_or_none(raw.get("packageLength")),
        "width_mm": _positive_or_none(raw.get("packageWidth")),
        "height_mm": _positive_or_none(raw.get("packageHeight")),
    }
    if all(v is None for v in dims.values()):
        return None
    return {k: v for k, v in dims.items() if v is not None}


def _extract_images_count(raw: dict[str, Any]) -> int | None:
    images_csv = raw.get("imagesCSV")
    if isinstance(images_csv, str) and images_csv:
        return len(images_csv.split(","))
    images = raw.get("images")
    if isinstance(images, list):
        return len(images)
    return None


def _extract_category_path(raw: dict[str, Any]) -> str | None:
    tree = raw.get("categoryTree")
    if isinstance(tree, list) and tree:
        names = [
            str(node.get("name")) for node in tree if isinstance(node, dict) and node.get("name")
        ]
        if names:
            return " > ".join(names)
    return None


def _first_barcode(raw: dict[str, Any], key: str) -> str | None:
    """First non-empty barcode string from a Keepa list field (eanList/upcList)."""
    values = raw.get(key)
    if isinstance(values, list):
        for value in values:
            text = str(value).strip() if value is not None else ""
            if text:
                return text
    elif isinstance(values, str) and values.strip():
        return values.strip()
    return None


def _extract_gtin(raw: dict[str, Any]) -> str | None:
    """Best single barcode for identity matching: EAN (13-digit superset) is
    preferred over UPC (12-digit). We do NOT invent identifiers — only what
    Keepa returns. The cross-market matcher normalizes UPC/EAN to digits, so a
    UPC stored here still matches its EAN-13 counterpart elsewhere."""
    return _first_barcode(raw, "eanList") or _first_barcode(raw, "upcList")


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_found(raw: dict[str, Any]) -> bool:
    """A Keepa product entry with no title and no history is a dead/absent ASIN."""
    if raw.get("title"):
        return True
    csv = raw.get("csv")
    return bool(isinstance(csv, list) and any(csv))


def normalize_product(raw: dict[str, Any], marketplace: str = "US") -> NormalizedProduct:
    """Map one raw Keepa product dict into our `NormalizedProduct`."""
    csv = raw.get("csv") or []

    amazon = _csv_series(csv, _CSV_AMAZON)
    new = _csv_series(csv, _CSV_NEW)
    sales = _csv_series(csv, _CSV_SALES)
    rating = _csv_series(csv, _CSV_RATING)
    reviews = _csv_series(csv, _CSV_COUNT_REVIEWS)

    points: dict[str, dict[str, Any]] = {}
    # Prefer Amazon price; fall back to marketplace New only where Amazon absent.
    for date, value in new:
        points.setdefault(date, {})["price_cents"] = value
    for date, value in amazon:
        points.setdefault(date, {})["price_cents"] = value
    for date, value in sales:
        points.setdefault(date, {})["bsr"] = value
    for date, value in rating:
        points.setdefault(date, {})["rating"] = value / 10.0
    for date, value in reviews:
        points.setdefault(date, {})["review_count"] = value

    history = [
        PriceBsrPoint(
            captured_on=date,
            price_cents=fields.get("price_cents"),
            bsr=fields.get("bsr"),
            review_count=fields.get("review_count"),
            rating=fields.get("rating"),
        )
        for date, fields in sorted(points.items())
    ]

    return NormalizedProduct(
        asin=str(raw.get("asin")),
        marketplace=marketplace,
        title=raw.get("title"),
        brand=raw.get("brand"),
        category_path=_extract_category_path(raw),
        dims=_extract_dims(raw),
        weight_g=_positive_or_none(raw.get("packageWeight")),
        images_count=_extract_images_count(raw),
        amazon_on_listing=bool(amazon),
        gtin=_extract_gtin(raw),
        manufacturer=_clean_str(raw.get("manufacturer")),
        history=history,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class KeepaClient:
    """Throttled, retrying client for the Keepa `/product` endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        marketplace: str = "US",
        domain: int | None = None,
    ) -> None:
        if not api_key:
            raise ProviderConfigError("Keepa API key is required.")
        self._api_key = api_key
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        self._marketplace = marketplace
        # `domain` overrides the marketplace mapping when given (back-compat).
        self._domain = domain if domain is not None else keepa_domain(marketplace)
        self._tokens_left: int | None = None
        self._refill_rate: int | None = None

    @property
    def marketplace(self) -> str:
        return self._marketplace

    @classmethod
    def from_env(
        cls, *, transport: Transport | None = None, marketplace: str = "US"
    ) -> KeepaClient:
        key = get_secrets().keepa_api_key
        if key is None:
            raise ProviderConfigError(
                "DELIUM_KEEPA_API_KEY is not set — add it to your environment or .env."
            )
        return cls(key.get_secret_value(), transport=transport, marketplace=marketplace)

    def fetch_product(self, asin: str) -> KeepaFetch:
        return self.fetch_products([asin])

    def fetch_products(self, asins: Sequence[str]) -> KeepaFetch:
        if not asins:
            raise ValueError("fetch_products requires at least one ASIN.")

        params = {
            "key": self._api_key,
            "domain": str(self._domain),
            "asin": ",".join(asins),
            "history": "1",
            "stats": "90",
            "rating": "1",
        }
        self._await_tokens(len(asins) * _TOKENS_PER_PRODUCT_ESTIMATE)
        result = self._request_with_retries(params)
        return self._parse(result, asins)

    # -- throttling & retries ------------------------------------------------
    def _await_tokens(self, estimated: int) -> None:
        if self._tokens_left is None or self._tokens_left >= estimated:
            return
        rate = self._refill_rate or 1
        deficit = estimated - self._tokens_left
        wait_s = min(ceil(deficit / rate * 60), _MAX_TOKEN_WAIT_S)
        log.info("Keepa token pacing: waiting %ss for refill.", wait_s)
        self._sleep(wait_s)
        # Assume the wait refilled enough; the next response corrects the count.
        self._tokens_left = estimated

    def _request_with_retries(self, params: dict[str, str]) -> HttpResult:
        last_status = 0
        for attempt in range(_MAX_RETRIES + 1):
            try:
                result = self._transport.request_json(KEEPA_PRODUCT_URL, params)
            except ProviderNetworkError:
                if attempt < _MAX_RETRIES:
                    self._sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
                    continue
                raise
            last_status = result.status

            if result.status == 200:
                return result
            if result.status in (401, 403):
                raise ProviderAuthError(f"Keepa rejected the API key (HTTP {result.status}).")
            if result.status == 429:
                self._absorb_token_state(result.body)
                if attempt < _MAX_RETRIES:
                    self._wait_for_refill()
                    continue
                raise ProviderRateLimitError("Keepa token bucket exhausted (HTTP 429).")
            if result.status >= 500:
                if attempt < _MAX_RETRIES:
                    self._sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
                    continue
                break
            # Other 4xx: not retryable.
            raise ProviderResponseError(f"Keepa returned HTTP {result.status}.")

        raise ProviderResponseError(
            f"Keepa request failed after retries (last status {last_status})."
        )

    def _wait_for_refill(self) -> None:
        rate = self._refill_rate or 1
        wait_s = min(ceil(_TOKENS_PER_PRODUCT_ESTIMATE / rate * 60), _MAX_TOKEN_WAIT_S)
        self._sleep(wait_s)

    def _absorb_token_state(self, body: dict[str, Any]) -> None:
        if "tokensLeft" in body:
            self._tokens_left = _positive_or_none(body.get("tokensLeft")) or 0
        if "refillRate" in body:
            self._refill_rate = _positive_or_none(body.get("refillRate"))

    # -- parsing -------------------------------------------------------------
    def _parse(self, result: HttpResult, asins: Sequence[str]) -> KeepaFetch:
        body = result.body
        self._absorb_token_state(body)

        products = body.get("products")
        if not isinstance(products, list):
            raise ProviderResponseError("Keepa response missing 'products' array.")

        by_asin: dict[str, dict[str, Any]] = {}
        for entry in products:
            if isinstance(entry, dict) and entry.get("asin"):
                by_asin[str(entry["asin"])] = entry

        raw_products: dict[str, dict[str, Any]] = {}
        normalized: dict[str, NormalizedProduct] = {}
        for asin in asins:
            entry = by_asin.get(asin)
            if entry is None:
                raw_products[asin] = {"asin": asin, "found": False}
                continue
            raw_products[asin] = entry
            if _is_found(entry):
                normalized[asin] = normalize_product(entry, marketplace=self._marketplace)

        tokens_consumed = _positive_or_none(body.get("tokensConsumed")) or 0
        return KeepaFetch(
            http_status=result.status,
            tokens_consumed=tokens_consumed,
            tokens_left=self._tokens_left,
            normalized=normalized,
            raw_products=raw_products,
        )
