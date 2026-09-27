"""Sellability — "if I launched this tomorrow, would it sell?" (pure).

Sellability RANKS products that already passed the kills and gates; it never
lets a product bypass them. It combines the vetted opportunity score with only
the signals the opportunity score structurally cannot see, so each signal counts
exactly once (see docs/sellability.md):

    sellability = Σ(component × weight) / Σ(weight)   over AVAILABLE components
      components: opportunity          (all 5 pillars — counted once)
                  price_headroom       (room at MY target price — new)
                  incumbent_freshness  (how young/movable page one is — new)
                  product_momentum     (the candidate's OWN review velocity — new)

Profitability lives inside `opportunity` (pillar P) and is never re-added. The
overlapping launchability parts (review moat, brand wall, listing quality) are
excluded because the competition pillar already prices them. `product_momentum`
is the candidate's own new-reviews/month — no pillar uses it (competition C3 is
the *leaders'* review velocity; demand D2/D3 are cluster medians; the candidate's
own BSR slope is deliberately left out to avoid overlap with D3 and the emergence
signal). See docs/sellability.md.

Rules honoured: absolute scales (weights in `sellability_data/<version>.toml`);
missing component → dropped, weights renormalize (missing = unknown); confidence
is a SEPARATE badge, never folded into the score; not eligible (failed a kill or
gate) → score is None with a reason.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from delium.analysis.curves import clamp, log_norm
from delium.analysis.models import Confidence

SELLABILITY_DIR = Path(__file__).parent / "sellability_data"
DEFAULT_VERSION = "us"


class SellabilityError(Exception):
    """Raised when the sellability weights file is missing or malformed."""


@dataclass(frozen=True)
class SellabilityInput:
    """The three inputs, each optional. `eligible` is False when the product
    failed a kill or gate — sellability then refuses to score it (never a bypass).
    `opportunity_score` is the existing composite; the other two are the
    non-overlapping launchability components."""

    opportunity_score: float | None
    price_headroom: float | None = None
    incumbent_freshness: float | None = None
    product_momentum: float | None = None  # 0-100 from the candidate's own review velocity
    eligible: bool = True


@dataclass(frozen=True)
class SellabilityComponent:
    name: str
    score: float | None
    weight: float


@dataclass(frozen=True)
class SellabilityScore:
    score: float | None
    components: tuple[SellabilityComponent, ...]
    missing: tuple[str, ...]
    confidence: Confidence  # SEPARATE badge — not folded into the score
    reason: str


@dataclass(frozen=True)
class SellabilityWeights:
    version: str
    opportunity: float
    price_headroom: float
    incumbent_freshness: float
    product_momentum: float
    momentum_reviews_lo: float
    momentum_reviews_hi: float


def load_sellability_data(version: str = DEFAULT_VERSION) -> SellabilityWeights:
    path = SELLABILITY_DIR / f"{version}.toml"
    if not path.exists():
        raise SellabilityError(f"Sellability weights {version!r} not found at {path}.")
    raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    w = raw.get("weights", {})
    pm = raw.get("product_momentum", {})
    return SellabilityWeights(
        version=str(raw["version"]),
        opportunity=float(w["opportunity"]),
        price_headroom=float(w["price_headroom"]),
        incumbent_freshness=float(w["incumbent_freshness"]),
        product_momentum=float(w.get("product_momentum", 0.0)),
        momentum_reviews_lo=float(pm.get("reviews_per_month_lo", 1)),
        momentum_reviews_hi=float(pm.get("reviews_per_month_hi", 120)),
    )


def product_momentum_score(
    reviews_per_month: float | None, weights: SellabilityWeights
) -> float | None:
    """Map the candidate's OWN new-reviews/month to an absolute 0-100 momentum
    score (log-scaled by the data-file thresholds). None → unknown."""
    if reviews_per_month is None or reviews_per_month <= 0:
        return None
    return round(
        clamp(
            log_norm(reviews_per_month, weights.momentum_reviews_lo, weights.momentum_reviews_hi)
        ),
        1,
    )


def _confidence(present: int, total: int) -> Confidence:
    """Coverage-based badge (separate from the score). All components present →
    HIGH; the opportunity score alone → LOW."""
    if present >= total:
        return Confidence.HIGH
    if present >= 2:
        return Confidence.MEDIUM
    return Confidence.LOW


def compute_sellability(inp: SellabilityInput, weights: SellabilityWeights) -> SellabilityScore:
    """Absolute-scale sellability (0-100). Same inputs → same score regardless of
    batch. Renormalizes over available components; confidence is separate."""
    if not inp.eligible:
        return SellabilityScore(
            None, (), (), Confidence.LOW, "Excluded — did not pass the kills/gates."
        )
    components = (
        SellabilityComponent("opportunity", inp.opportunity_score, weights.opportunity),
        SellabilityComponent("price_headroom", inp.price_headroom, weights.price_headroom),
        SellabilityComponent(
            "incumbent_freshness", inp.incumbent_freshness, weights.incumbent_freshness
        ),
        SellabilityComponent("product_momentum", inp.product_momentum, weights.product_momentum),
    )
    present = [c for c in components if c.score is not None]
    total_w = sum(c.weight for c in present)
    score = (
        round(sum(c.score * c.weight for c in present if c.score is not None) / total_w, 1)
        if total_w > 0
        else None
    )
    missing = tuple(c.name for c in components if c.score is None)
    conf = _confidence(len(present), len(components))
    return SellabilityScore(score, components, missing, conf, _reason(inp, score, conf))


def _reason(inp: SellabilityInput, score: float | None, conf: Confidence) -> str:
    if score is None:
        return "Not enough data to assess sellability."
    bits: list[str] = [f"opportunity {inp.opportunity_score:.0f}/100"]
    if inp.price_headroom is not None:
        room = "room" if inp.price_headroom >= 60 else "tight"
        bits.append(f"price headroom {inp.price_headroom:.0f}/100 ({room} at your target)")
    if inp.incumbent_freshness is not None:
        fresh = "movable" if inp.incumbent_freshness >= 60 else "entrenched"
        bits.append(f"incumbents {fresh} ({inp.incumbent_freshness:.0f}/100)")
    if inp.product_momentum is not None:
        pull = "gaining reviews" if inp.product_momentum >= 50 else "slow review pull"
        bits.append(f"own momentum {inp.product_momentum:.0f}/100 ({pull})")
    tail = "" if conf is Confidence.HIGH else f" [{conf.value} confidence — some signals missing]"
    return f"Sellability {score:.0f}/100: " + "; ".join(bits) + "." + tail
