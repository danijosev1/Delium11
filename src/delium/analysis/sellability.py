"""Sellability — "if I launched this tomorrow, would it sell?" (pure).

Sellability RANKS products that already passed the kills and gates; it never
lets a product bypass them. It combines the vetted opportunity score with only
the signals the opportunity score structurally cannot see, so each signal counts
exactly once (see docs/sellability.md):

    sellability = Σ(component × weight) / Σ(weight)   over AVAILABLE components
      components: opportunity          (all 5 pillars — counted once)
                  price_headroom       (room at MY target price — new)
                  incumbent_freshness  (how young/movable page one is — new)

Profitability lives inside `opportunity` (pillar P) and is never re-added. The
overlapping launchability parts (review moat, brand wall, listing quality) are
excluded because the competition pillar already prices them.

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


def load_sellability_data(version: str = DEFAULT_VERSION) -> SellabilityWeights:
    path = SELLABILITY_DIR / f"{version}.toml"
    if not path.exists():
        raise SellabilityError(f"Sellability weights {version!r} not found at {path}.")
    raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    w = raw.get("weights", {})
    return SellabilityWeights(
        version=str(raw["version"]),
        opportunity=float(w["opportunity"]),
        price_headroom=float(w["price_headroom"]),
        incumbent_freshness=float(w["incumbent_freshness"]),
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
    tail = "" if conf is Confidence.HIGH else f" [{conf.value} confidence — some signals missing]"
    return f"Sellability {score:.0f}/100: " + "; ".join(bits) + "." + tail
