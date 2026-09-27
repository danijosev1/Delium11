"""Launchability — how beatable is page one for a new entrant? (pure).

Given a product's already-hydrated page-one competitor set and the seller's
target price, score how launchable the market is on an ABSOLUTE 0-100 scale
(100 = most beatable). Every component maps through fixed thresholds in
`launchability_data/<version>.toml`, never z-scored against the current batch,
so a score means the same thing across days.

Rules honoured:
  * No competitor set → launchability is None (unknown), never 0.
  * A present-but-incomputable component (e.g. no ages) is dropped and the
    weights renormalize over the available ones (missing = unknown).
  * Listing-quality uses ONLY fields we store: images_count, rating, and title
    length (characters). It is a cheap proxy, not the Analyst's rubric.

This module is pure: no I/O beyond loading its own versioned data file, no
network, no clock, no randomness.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from delium.analysis.curves import clamp, log_norm, norm

LAUNCHABILITY_DIR = Path(__file__).parent / "launchability_data"
DEFAULT_VERSION = "us"

_AMAZON_BRANDS = frozenset({"amazon", "amazonbasics", "amazon basics"})


class LaunchabilityError(Exception):
    """Raised when the launchability thresholds file is missing or malformed."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Competitor:
    """One already-hydrated page-one competitor. All fields optional — an absent
    field simply drops the components that need it."""

    asin: str
    reviews: int | None = None
    rating: float | None = None
    price_cents: int | None = None
    images_count: int | None = None
    title: str | None = None
    brand: str | None = None
    age_days: int | None = None


@dataclass(frozen=True)
class SubScore:
    name: str
    score: float | None  # 0-100, or None when its inputs are absent
    weight: float
    detail: str


@dataclass(frozen=True)
class LaunchabilityScore:
    score: float | None  # weighted over AVAILABLE components; None if no set
    components: tuple[SubScore, ...]
    missing: tuple[str, ...]
    reason: str

    @property
    def price_headroom(self) -> float | None:
        """The price-crowding component as a standalone 0-100 signal (used by
        sellability as a non-overlapping input)."""
        return _named(self.components, "price_crowding")

    @property
    def incumbent_freshness(self) -> float | None:
        """The listing-age component as a standalone 0-100 signal (young page →
        high). Used by sellability as a non-overlapping input."""
        return _named(self.components, "listing_age")


def _named(components: tuple[SubScore, ...], name: str) -> float | None:
    for c in components:
        if c.name == name:
            return c.score
    return None


# ---------------------------------------------------------------------------
# Thresholds (external data file)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LaunchabilityThresholds:
    version: str
    weights: dict[str, float]
    raw: dict[str, Any]

    def w(self, name: str) -> float:
        return float(self.weights.get(name, 0.0))

    def sect(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get(name, {}))


def load_launchability_data(version: str = DEFAULT_VERSION) -> LaunchabilityThresholds:
    path = LAUNCHABILITY_DIR / f"{version}.toml"
    if not path.exists():
        raise LaunchabilityError(f"Launchability thresholds {version!r} not found at {path}.")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    weights = {k: float(v) for k, v in raw.get("weights", {}).items()}
    return LaunchabilityThresholds(version=str(raw["version"]), weights=weights, raw=raw)


# ---------------------------------------------------------------------------
# Components (each pure; None when its inputs are absent)
# ---------------------------------------------------------------------------
def _median_reviews(comps: list[Competitor], t: LaunchabilityThresholds) -> SubScore:
    vals = [c.reviews for c in comps if c.reviews is not None]
    w = t.w("median_reviews")
    if not vals:
        return SubScore("median_reviews", None, w, "no review counts")
    med = median(vals)
    cfg = t.sect("median_reviews")
    score = clamp(100.0 - log_norm(max(med, 1.0), float(cfg["lo"]), float(cfg["hi"])))
    return SubScore("median_reviews", score, w, f"median top-10 reviews {med:.0f}")


def _heavy_review_count(comps: list[Competitor], t: LaunchabilityThresholds) -> SubScore:
    vals = [c.reviews for c in comps if c.reviews is not None]
    w = t.w("heavy_review_count")
    if not vals:
        return SubScore("heavy_review_count", None, w, "no review counts")
    cfg = t.sect("heavy_review_count")
    heavy = sum(1 for v in vals if v > int(cfg["threshold"]))
    score = clamp(100.0 - norm(heavy, 0.0, float(cfg["cap"])))
    return SubScore(
        "heavy_review_count", score, w, f"{heavy} of {len(vals)} > {cfg['threshold']} reviews"
    )


def _brand_presence(
    comps: list[Competitor], t: LaunchabilityThresholds, established: frozenset[str]
) -> SubScore:
    w = t.w("brand_presence")
    brands = [(c.brand or "").strip().lower() for c in comps if c.brand]
    if not brands:
        return SubScore("brand_presence", None, w, "no brands")
    cfg = t.sect("brand_presence")
    amazon = any(b in _AMAZON_BRANDS for b in brands)
    big = any(_is_established(b, established) for b in brands)
    score = 100.0
    detail = "no Amazon/established brand on page one"
    if amazon:
        score -= float(cfg["amazon_penalty"])
        detail = "Amazon present on page one"
    if big:
        score -= float(cfg["established_brand_penalty"])
        detail = "established brand present" if not amazon else detail + " + established brand"
    return SubScore("brand_presence", clamp(score), w, detail)


def _is_established(brand: str, established: frozenset[str]) -> bool:
    if not brand:
        return False
    tokens = set(brand.replace("-", " ").split())
    return any(
        name == brand or name in tokens or (" " in name and name in brand) for name in established
    )


def _listing_age(comps: list[Competitor], t: LaunchabilityThresholds) -> SubScore:
    vals = [c.age_days for c in comps if c.age_days is not None]
    w = t.w("listing_age")
    if not vals:
        return SubScore("listing_age", None, w, "no listing ages")
    med = median(vals)
    cfg = t.sect("listing_age")
    # Young page (fresh listings) = movable market → high; old = entrenched → low.
    score = clamp(100.0 - norm(med, float(cfg["young_days"]), float(cfg["old_days"])))
    return SubScore("listing_age", score, w, f"median listing age {med:.0f}d")


def _listing_quality_gap(comps: list[Competitor], t: LaunchabilityThresholds) -> SubScore:
    w = t.w("listing_quality_gap")
    cfg = t.sect("listing_quality_gap")
    qualities: list[float] = []
    for c in comps:
        parts: list[float] = []
        if c.images_count is not None:
            parts.append(norm(c.images_count, float(cfg["images_lo"]), float(cfg["images_hi"])))
        if c.rating is not None:
            parts.append(norm(c.rating, float(cfg["rating_lo"]), float(cfg["rating_hi"])))
        if c.title is not None:
            parts.append(norm(len(c.title), float(cfg["title_len_lo"]), float(cfg["title_len_hi"])))
        if parts:
            qualities.append(sum(parts) / len(parts))
    if not qualities:
        return SubScore("listing_quality_gap", None, w, "no images/rating/title data")
    avg_quality = sum(qualities) / len(qualities)
    score = clamp(100.0 - avg_quality)  # weak incumbent listings → room to win
    return SubScore(
        "listing_quality_gap", score, w, f"avg incumbent listing quality {avg_quality:.0f}/100"
    )


def _price_crowding(
    comps: list[Competitor], target_price_cents: int | None, t: LaunchabilityThresholds
) -> SubScore:
    w = t.w("price_crowding")
    prices = [c.price_cents for c in comps if c.price_cents is not None and c.price_cents > 0]
    if target_price_cents is None or target_price_cents <= 0 or not prices:
        return SubScore("price_crowding", None, w, "no target price or competitor prices")
    cfg = t.sect("price_crowding")
    band = float(cfg["band_pct"]) * target_price_cents
    lo, hi = target_price_cents - band, target_price_cents + band
    in_band = sum(1 for p in prices if lo <= p <= hi)
    score = clamp(100.0 - norm(in_band, 0.0, float(cfg["cap"])))
    return SubScore(
        "price_crowding",
        score,
        w,
        f"{in_band} competitor(s) within ±{cfg['band_pct']:.0%} of ${target_price_cents / 100:.2f}",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def compute_launchability(
    competitors: list[Competitor],
    *,
    target_price_cents: int | None,
    thresholds: LaunchabilityThresholds,
    established_brands: frozenset[str] = frozenset(),
) -> LaunchabilityScore:
    """Absolute-scale launchability (0-100, higher = more beatable). Empty
    competitor set → None (unknown). Otherwise the weighted mean over available
    components, weights renormalized to skip missing ones."""
    if not competitors:
        return LaunchabilityScore(
            None, (), ("competitor_set",), "No page-one competitor set — launchability unknown."
        )
    comps = list(competitors)
    subs = (
        _median_reviews(comps, thresholds),
        _heavy_review_count(comps, thresholds),
        _brand_presence(comps, thresholds, established_brands),
        _listing_age(comps, thresholds),
        _listing_quality_gap(comps, thresholds),
        _price_crowding(comps, target_price_cents, thresholds),
    )
    present = [s for s in subs if s.score is not None]
    total_w = sum(s.weight for s in present)
    score = (
        round(sum(s.score * s.weight for s in present if s.score is not None) / total_w, 1)
        if total_w > 0
        else None
    )
    missing = tuple(s.name for s in subs if s.score is None)
    return LaunchabilityScore(score, subs, missing, _reason(comps, subs, score))


def _reason(comps: list[Competitor], subs: tuple[SubScore, ...], score: float | None) -> str:
    if score is None:
        return "Not enough competitor data to assess launchability."
    verdict = "beatable" if score >= 60 else ("workable" if score >= 40 else "entrenched")
    drivers = [s.detail for s in subs if s.score is not None][:3]
    return f"Page one looks {verdict} ({score:.0f}/100): " + "; ".join(drivers) + "."
