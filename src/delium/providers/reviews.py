"""Review provider adapter.

Fetches a review sample for an ASIN. Primary provider is Unwrangle (GET);
fallback is an Apify actor (POST). Both normalize into a common `NormalizedReview`
shape. A `ReviewProviderChain` tries providers in order so a primary failure
falls back automatically.

Anonymous review access tops out around ~100 reviews/ASIN (docs/data-layer.md
§1.3); this is the fragile data source, so error handling degrades gracefully
rather than throwing. No AI, no theme extraction — that is the Review Miner's job.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from delium.config.secrets import get_secrets
from delium.providers.base import (
    HttpResult,
    PostTransport,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderResponseError,
    Transport,
    UrllibTransport,
)
from delium.utils.logging import get_logger

log = get_logger(__name__)

_UNWRANGLE_URL = "https://data.unwrangle.com/api/getter/"
_APIFY_BASE = "https://api.apify.com/v2"
_APIFY_DEFAULT_ACTOR = "junglee~amazon-reviews-scraper"

# Approx blended scraper cost, USD per review (~$3 / 1,000; docs/data-economics.md §2).
# Real billing is credit/subscription-based; this is a spend estimate for the ledger.
_COST_PER_REVIEW = 0.003
_MAX_REVIEWS = 100  # anonymous ceiling

_MAX_RETRIES = 2
_BACKOFF_SECONDS = (5.0, 25.0)


# ---------------------------------------------------------------------------
# Normalized output
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NormalizedReview:
    review_id: str
    asin: str
    stars: int
    title: str | None
    body: str | None
    verified_purchase: bool
    review_date: str | None
    helpful_votes: int
    reviewer_name: str | None = None


@dataclass(frozen=True)
class ReviewFetch:
    provider: str
    http_status: int
    cost_usd: float
    reviews: list[NormalizedReview] = field(default_factory=list)
    raw: Any = None


# ---------------------------------------------------------------------------
# Parsing helpers (pure)
# ---------------------------------------------------------------------------
def _coerce_stars(value: Any) -> int | None:
    try:
        stars = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return stars if 1 <= stars <= 5 else None


def _parse_votes(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return int(value)
    match = re.search(r"(\d[\d,]*)", str(value))
    return int(match.group(1).replace(",", "")) if match else 0


def _parse_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    iso = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if iso:
        return iso.group(1)
    worded = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", text)
    if worded:
        try:
            parsed = datetime.strptime(
                f"{worded.group(1)} {worded.group(2)} {worded.group(3)}", "%B %d %Y"
            )
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            return None
    return text or None


def _review_id(raw_id: Any, asin: str, author: Any, date: Any, body: Any) -> str:
    if raw_id:
        return str(raw_id)
    digest = hashlib.sha1(f"{asin}|{author}|{date}|{body}".encode()).hexdigest()[:16]
    return f"{asin}-{digest}"


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def normalize_unwrangle(body: Any, asin: str) -> list[NormalizedReview]:
    reviews = body.get("reviews") if isinstance(body, dict) else None
    out: list[NormalizedReview] = []
    for raw in reviews or []:
        if not isinstance(raw, dict):
            continue
        stars = _coerce_stars(_first(raw, "rating", "stars", "review_rating"))
        if stars is None:
            continue
        title = _first(raw, "title", "review_title")
        text = _first(raw, "review", "review_text", "body")
        author = _first(raw, "author", "reviewer_name", "profile_name")
        date = _parse_date(_first(raw, "date", "review_date"))
        out.append(
            NormalizedReview(
                review_id=_review_id(_first(raw, "id", "review_id"), asin, author, date, text),
                asin=asin,
                stars=stars,
                title=title,
                body=text,
                verified_purchase=bool(raw.get("verified_purchase")),
                review_date=date,
                helpful_votes=_parse_votes(_first(raw, "helpful_votes", "helpful")),
                reviewer_name=author,
            )
        )
    return out


def normalize_apify(items: Any, asin: str) -> list[NormalizedReview]:
    out: list[NormalizedReview] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        stars = _coerce_stars(_first(raw, "ratingScore", "rating", "stars"))
        if stars is None:
            continue
        title = _first(raw, "reviewTitle", "title")
        text = _first(raw, "reviewDescription", "reviewText", "text", "body")
        author = _first(raw, "reviewerName", "author", "name")
        date = _parse_date(_first(raw, "date", "reviewDate", "reviewedIn"))
        verified_raw = _first(raw, "isVerified", "verified", "verifiedPurchase")
        out.append(
            NormalizedReview(
                review_id=_review_id(_first(raw, "reviewId", "id"), asin, author, date, text),
                asin=asin,
                stars=stars,
                title=title,
                body=text,
                verified_purchase=bool(verified_raw),
                review_date=date,
                helpful_votes=_parse_votes(
                    _first(raw, "reviewReaction", "helpfulVotes", "helpful_votes")
                ),
                reviewer_name=author,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Shared retry loop
# ---------------------------------------------------------------------------
def _request_with_retries(
    send: Callable[[], HttpResult], sleep: Callable[[float], None], *, provider: str
) -> HttpResult:
    last_status = 0
    for attempt in range(_MAX_RETRIES + 1):
        try:
            result = send()
        except ProviderNetworkError:
            if attempt < _MAX_RETRIES:
                sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
                continue
            raise
        last_status = result.status

        if result.status == 200:
            return result
        if result.status in (401, 403):
            raise ProviderAuthError(f"{provider} auth rejected (HTTP {result.status}).")
        if result.status == 429:
            if attempt < _MAX_RETRIES:
                sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
                continue
            raise ProviderRateLimitError(f"{provider} rate limit (HTTP 429).")
        if result.status >= 500:
            if attempt < _MAX_RETRIES:
                sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
                continue
            break
        raise ProviderResponseError(f"{provider} returned HTTP {result.status}.")

    raise ProviderResponseError(
        f"{provider} request failed after retries (last status {last_status})."
    )


def _estimate_cost(review_count: int) -> float:
    return round(review_count * _COST_PER_REVIEW, 4)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class UnwrangleClient:
    """Primary review provider (GET)."""

    name = "unwrangle"

    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ProviderConfigError("Unwrangle API key is required.")
        self._api_key = api_key
        self._transport = transport or UrllibTransport()
        self._sleep = sleep

    def fetch_reviews(self, asin: str) -> ReviewFetch:
        params = {
            "platform": "amazon_reviews",
            "asin": asin,
            "country_code": "us",
            "page": "1",
            "api_key": self._api_key,
        }
        result = _request_with_retries(
            lambda: self._transport.request_json(_UNWRANGLE_URL, params),
            self._sleep,
            provider=self.name,
        )
        body = result.body
        if isinstance(body, dict) and body.get("success") is False:
            raise ProviderResponseError(f"Unwrangle error: {body.get('error')}")
        reviews = normalize_unwrangle(body, asin)
        return ReviewFetch(
            provider=self.name,
            http_status=result.status,
            cost_usd=_estimate_cost(len(reviews)),
            reviews=reviews,
            raw=body,
        )


class ApifyClient:
    """Fallback review provider (POST to an actor's run-sync endpoint)."""

    name = "apify"

    def __init__(
        self,
        api_token: str,
        *,
        actor: str = _APIFY_DEFAULT_ACTOR,
        transport: PostTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_token:
            raise ProviderConfigError("Apify API token is required.")
        self._api_token = api_token
        self._actor = actor
        self._transport = transport or UrllibTransport()
        self._sleep = sleep

    def fetch_reviews(self, asin: str) -> ReviewFetch:
        url = f"{_APIFY_BASE}/acts/{self._actor}/run-sync-get-dataset-items?token={self._api_token}"
        request_body = {"asins": [asin], "maxReviews": _MAX_REVIEWS}
        result = _request_with_retries(
            lambda: self._transport.post_json(url, request_body, {}),
            self._sleep,
            provider=self.name,
        )
        items = result.body if isinstance(result.body, list) else []
        reviews = normalize_apify(items, asin)
        return ReviewFetch(
            provider=self.name,
            http_status=result.status,
            cost_usd=_estimate_cost(len(reviews)),
            reviews=reviews,
            raw=items,
        )


class ReviewProviderChain:
    """Tries providers in order; a `ProviderError` falls through to the next."""

    name = "review-chain"

    def __init__(self, providers: Sequence[UnwrangleClient | ApifyClient]) -> None:
        self._providers = list(providers)

    def fetch_reviews(self, asin: str) -> ReviewFetch:
        last_error: ProviderError | None = None
        for provider in self._providers:
            try:
                return provider.fetch_reviews(asin)
            except ProviderError as exc:
                log.warning("Review provider %s failed: %s", provider.name, exc)
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ProviderConfigError("No review providers configured.")


def build_review_provider(
    *,
    transport: Any = None,
) -> ReviewProviderChain:
    """Build the review chain from environment secrets (Unwrangle → Apify)."""
    secrets = get_secrets()
    providers: list[UnwrangleClient | ApifyClient] = []
    if secrets.unwrangle_api_key is not None:
        providers.append(
            UnwrangleClient(secrets.unwrangle_api_key.get_secret_value(), transport=transport)
        )
    if secrets.apify_api_token is not None:
        providers.append(
            ApifyClient(secrets.apify_api_token.get_secret_value(), transport=transport)
        )
    if not providers:
        raise ProviderConfigError(
            "No review provider configured — set DELIUM_UNWRANGLE_API_KEY or "
            "DELIUM_APIFY_API_TOKEN."
        )
    return ReviewProviderChain(providers)
