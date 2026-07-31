"""Deterministic listing-quality engine.

Turns observable listing facts (title length, image/bullet counts, A+/brand-store
booleans, keyword coverage, review density, price positioning) into a 0-100
Listing Quality Score with a per-subscore explanation. Higher = better listing.
The competition pillar later inverts this (a strong incumbent listing = less
opportunity), but this module only measures quality.

No AI, no guessing: a missing input drops its subscore out of the weighted
average (weights renormalize over what is known) and lowers confidence. See
docs/analysis-engine.md §2 (listing_quality / C5) and docs/scoring-model.md C5.
"""

from __future__ import annotations

import math
from statistics import median

from delium.analysis.models import (
    Confidence,
    ListingInput,
    ListingQualityReport,
    Subscore,
)

# Subscore weights (sum = 100). Renormalized over available subscores.
_WEIGHTS: dict[str, float] = {
    "title_length": 10,
    "keyword_coverage": 20,
    "bullet_count": 10,
    "image_count": 15,
    "aplus": 15,
    "brand_store": 5,
    "variation": 5,
    "review_density": 10,
    "price_positioning": 5,
    "completeness": 5,
}

# Amazon best-practice targets.
_IDEAL_BULLETS = 5
_IDEAL_IMAGES = 7

# Confidence thresholds on the fraction of total weight actually assessed.
_HIGH_COVERAGE = 0.85
_MEDIUM_COVERAGE = 0.55


# ---------------------------------------------------------------------------
# Pure curve helpers
# ---------------------------------------------------------------------------
def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _plateau(
    x: float, rise_lo: float, rise_hi: float, fall_lo: float, fall_hi: float, floor: float = 0.0
) -> float:
    """Trapezoid: 0 below rise_lo, ramps to 100 by rise_hi, flat 100 until
    fall_lo, decays to `floor` by fall_hi, floor beyond."""
    if x <= rise_lo:
        return 0.0
    if x < rise_hi:
        return (x - rise_lo) / (rise_hi - rise_lo) * 100.0
    if x <= fall_lo:
        return 100.0
    if x < fall_hi:
        return 100.0 - (x - fall_lo) / (fall_hi - fall_lo) * (100.0 - floor)
    return floor


def _log_norm(x: float, lo: float, hi: float) -> float:
    if x <= 0:
        return 0.0
    return _clamp((math.log10(x) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * 100.0)


# ---------------------------------------------------------------------------
# Individual subscores → (value | None, detail)
# ---------------------------------------------------------------------------
def _title_length(listing: ListingInput) -> tuple[float | None, str]:
    if listing.title is None:
        return None, "title not provided"
    length = len(listing.title.strip())
    # Sweet spot 80-200 chars (keyword-rich but under Amazon's ~200 limit).
    value = _plateau(length, 15, 80, 200, 250, floor=70)
    return value, f"title {length} chars"


def _keyword_coverage(listing: ListingInput) -> tuple[float | None, str]:
    if not listing.keywords:
        return None, "no target keywords provided"
    haystack_parts = [listing.title or ""]
    if listing.bullets:
        haystack_parts.extend(listing.bullets)
    if not any(part.strip() for part in haystack_parts):
        return None, "no title/bullet text to search"
    haystack = " ".join(haystack_parts).lower()
    matched = sum(1 for kw in listing.keywords if kw.strip().lower() in haystack)
    value = matched / len(listing.keywords) * 100.0
    return value, f"{matched}/{len(listing.keywords)} keywords present"


def _bullet_count(listing: ListingInput) -> tuple[float | None, str]:
    if listing.bullets is None:
        return None, "bullets not provided"
    count = len(listing.bullets)
    value = min(count, _IDEAL_BULLETS) / _IDEAL_BULLETS * 100.0
    return value, f"{count} bullets (ideal {_IDEAL_BULLETS})"


def _image_count(listing: ListingInput) -> tuple[float | None, str]:
    if listing.images_count is None:
        return None, "image count not provided"
    count = max(0, listing.images_count)
    value = min(count, _IDEAL_IMAGES) / _IDEAL_IMAGES * 100.0
    return value, f"{count} images (ideal {_IDEAL_IMAGES})"


def _aplus(listing: ListingInput) -> tuple[float | None, str]:
    if listing.has_aplus is None:
        return None, "A+ presence unknown"
    return (100.0 if listing.has_aplus else 0.0), f"A+ content: {listing.has_aplus}"


def _brand_store(listing: ListingInput) -> tuple[float | None, str]:
    if listing.has_brand_store is None:
        return None, "brand store presence unknown"
    return (100.0 if listing.has_brand_store else 0.0), f"brand store: {listing.has_brand_store}"


def _variation(listing: ListingInput) -> tuple[float | None, str]:
    if listing.variation_count is None:
        return None, "variation count not provided"
    count = max(0, listing.variation_count)
    # 0 variations is neutral (many good listings are single-variant); more
    # variations indicate a more developed listing, up to a cap.
    value = 50.0 + min(count, 5) / 5 * 50.0
    return value, f"{count} variations"


def _review_density(listing: ListingInput) -> tuple[float | None, str]:
    if listing.review_count is None:
        return None, "review count not provided"
    value = _log_norm(max(0, listing.review_count), 10, 3000)
    return value, f"{listing.review_count} reviews (log-scaled social proof)"


def _price_positioning(listing: ListingInput) -> tuple[float | None, str]:
    if listing.price_cents is None or not listing.competitor_prices_cents:
        return None, "price or competitor prices unavailable"
    market = median(listing.competitor_prices_cents)
    if market <= 0:
        return None, "invalid competitor median"
    ratio = listing.price_cents / market
    # Competitive band 0.8-1.05× the market median scores best; far above =
    # overpriced, far below = race-to-bottom / quality doubt.
    value = _plateau(ratio, 0.5, 0.8, 1.05, 1.5, floor=20)
    return value, f"price {ratio:.2f}× market median"


def _completeness(listing: ListingInput) -> tuple[float | None, str]:
    # Presence of each key listing element, over the elements we actually know.
    elements: list[bool] = []
    if listing.title is not None:
        elements.append(bool(listing.title.strip()))
    if listing.bullets is not None:
        elements.append(len(listing.bullets) >= 1)
    if listing.images_count is not None:
        elements.append(listing.images_count >= 1)
    if listing.has_aplus is not None:
        elements.append(listing.has_aplus)
    if listing.has_video is not None:
        elements.append(listing.has_video)
    if listing.has_brand_store is not None:
        elements.append(listing.has_brand_store)
    if not elements:
        return None, "no completeness signals available"
    present = sum(elements)
    value = present / len(elements) * 100.0
    return value, f"{present}/{len(elements)} key elements present"


_SUBSCORES = {
    "title_length": _title_length,
    "keyword_coverage": _keyword_coverage,
    "bullet_count": _bullet_count,
    "image_count": _image_count,
    "aplus": _aplus,
    "brand_store": _brand_store,
    "variation": _variation,
    "review_density": _review_density,
    "price_positioning": _price_positioning,
    "completeness": _completeness,
}


def _confidence(assessed_weight: float, total_weight: float) -> Confidence:
    ratio = assessed_weight / total_weight if total_weight > 0 else 0.0
    if ratio >= _HIGH_COVERAGE:
        return Confidence.HIGH
    if ratio >= _MEDIUM_COVERAGE:
        return Confidence.MEDIUM
    return Confidence.LOW


def compute_listing_quality(listing: ListingInput) -> ListingQualityReport:
    """Score a single listing's quality (0-100) with per-subscore explanations."""
    subscores: list[Subscore] = []
    weighted_sum = 0.0
    assessed_weight = 0.0

    for name, fn in _SUBSCORES.items():
        weight = _WEIGHTS[name]
        value, detail = fn(listing)
        if value is not None:
            value = _clamp(value)
            weighted_sum += value * weight
            assessed_weight += weight
        subscores.append(Subscore(name=name, value=value, weight=weight, detail=detail))

    overall = weighted_sum / assessed_weight if assessed_weight > 0 else 0.0
    return ListingQualityReport(
        overall_score=_clamp(overall),
        confidence=_confidence(assessed_weight, sum(_WEIGHTS.values())),
        subscores=tuple(subscores),
    )
