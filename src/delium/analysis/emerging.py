"""Emerging-products signal — deterministic, table-driven, pure.

Two responsibilities, both deterministic and free of I/O:

1. `build_finder_selection` — turn the external thresholds + a category set into
   the Keepa Product Finder `selection` JSON (endpoint `GET/POST
   https://api.keepa.com/query`). What to search for.

2. `compute_emergence` — score WHY a candidate reads as emerging (recency, sales
   traction, rank momentum, low competition, sales-vs-reviews gap) from its
   observable, already-fetched facts. This `emergence_score` is SEPARATE from the
   opportunity score and never feeds scoring.py — scoring stays the sole verdict
   owner. Missing inputs drop their sub-signal and the weights renormalize (an
   emergence score is never fabricated from absent data).

Thresholds/weights live in `emerging_data/<version>.toml` (versioned external
data, like the fee/curve/risk tables); this module only reads them.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from delium.analysis.curves import clamp, log_norm, norm

EMERGING_DIR = Path(__file__).parent / "emerging_data"
DEFAULT_VERSION = "us"

# Keepa epoch: unix_seconds = (keepa_minutes + 21564000) * 60 (see providers/keepa).
_KEEPA_EPOCH_MINUTES = 21564000


class EmergingError(Exception):
    """Raised when the emerging thresholds file is missing or malformed."""


# ---------------------------------------------------------------------------
# Config (loaded from the external data file)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FinderThresholds:
    age_max_days: int
    bsr_min: int
    bsr_max: int
    price_min_cents: int
    price_max_cents: int
    reviews_max: int
    exclude_amazon: bool
    sub_bands: int = 1  # split the BSR band into N log-spaced sub-ranges
    sort_field: str = "current_SALES"  # Keepa finder sort key within each sub-band


@dataclass(frozen=True)
class EmergenceThresholds:
    young_days: float
    old_days: float
    bsr_strong: float
    bsr_weak: float
    slope_improving: float
    reviews_low: float
    reviews_high: float
    w_recency: float
    w_traction: float
    w_momentum: float
    w_competition: float
    w_review_gap: float


@dataclass(frozen=True)
class EmergingData:
    version: str
    finder: FinderThresholds
    emergence: EmergenceThresholds
    established_brands: tuple[str, ...] = ()  # large brands to FLAG (never score)


def load_emerging_data(version: str = DEFAULT_VERSION) -> EmergingData:
    path = EMERGING_DIR / f"{version}.toml"
    if not path.exists():
        raise EmergingError(f"Emerging thresholds {version!r} not found at {path}.")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    f, e = raw["finder"], raw["emergence"]
    brands = raw.get("brands", {}).get("established", [])
    return EmergingData(
        version=str(raw["version"]),
        finder=FinderThresholds(
            age_max_days=int(f["age_max_days"]),
            bsr_min=int(f["bsr_min"]),
            bsr_max=int(f["bsr_max"]),
            price_min_cents=int(f["price_min_cents"]),
            price_max_cents=int(f["price_max_cents"]),
            reviews_max=int(f["reviews_max"]),
            exclude_amazon=bool(f["exclude_amazon"]),
            sub_bands=max(1, int(f.get("sub_bands", 1))),
            sort_field=str(f.get("sort_field", "current_SALES")),
        ),
        established_brands=tuple(str(b).strip().lower() for b in brands if str(b).strip()),
        emergence=EmergenceThresholds(
            young_days=float(e["young_days"]),
            old_days=float(e["old_days"]),
            bsr_strong=float(e["bsr_strong"]),
            bsr_weak=float(e["bsr_weak"]),
            slope_improving=float(e["slope_improving"]),
            reviews_low=float(e["reviews_low"]),
            reviews_high=float(e["reviews_high"]),
            w_recency=float(e["w_recency"]),
            w_traction=float(e["w_traction"]),
            w_momentum=float(e["w_momentum"]),
            w_competition=float(e["w_competition"]),
            w_review_gap=float(e["w_review_gap"]),
        ),
    )


# ---------------------------------------------------------------------------
# Keepa Product Finder selection (pure)
# ---------------------------------------------------------------------------
def _days_ago_to_keepa_minute(as_of: date, days: int) -> int:
    cutoff = as_of - timedelta(days=days)
    unix_seconds = int((cutoff - date(1970, 1, 1)).days * 86400)  # midnight UTC of the cutoff date
    return unix_seconds // 60 - _KEEPA_EPOCH_MINUTES


def build_finder_selection(
    data: EmergingData,
    *,
    as_of: date,
    category_ids: list[int],
    page: int = 0,
    per_page: int = 50,
    overrides: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Assemble the Keepa `/query` selection JSON from the thresholds. Field names
    follow the Keepa Product Finder schema; ranges are inclusive. `overrides`
    (e.g. {'reviews_max': 50}) replace the corresponding threshold for one run."""
    f = data.finder
    ov = overrides or {}
    age_days = int(ov.get("age_max_days", f.age_max_days))
    selection: dict[str, Any] = {
        "current_SALES_gte": int(ov.get("bsr_min", f.bsr_min)),
        "current_SALES_lte": int(ov.get("bsr_max", f.bsr_max)),
        "current_NEW_gte": int(ov.get("price_min_cents", f.price_min_cents)),
        "current_NEW_lte": int(ov.get("price_max_cents", f.price_max_cents)),
        "current_COUNT_REVIEWS_lte": int(ov.get("reviews_max", f.reviews_max)),
        # Recently tracked = first appeared in Keepa within the age window.
        "trackingSince_gte": _days_ago_to_keepa_minute(as_of, age_days),
        "productType": [0, 1],  # physical products only
        "page": page,
        "perPage": per_page,
        "sort": [[f.sort_field, "asc"]],  # best-selling (lowest BSR) first within the band
    }
    if f.exclude_amazon:
        selection["buyBoxIsAmazon"] = False
    if category_ids:
        selection["rootCategory"] = list(category_ids)
    return selection


def _log_band_edges(lo: int, hi: int, bands: int) -> list[int]:
    """`bands + 1` log-spaced integer edges spanning [lo, hi] (inclusive). Log
    spacing matches BSR's scale — sales fall off geometrically with rank, so equal
    log steps give sub-bands with comparable product density."""
    from math import log10

    if bands <= 1 or lo <= 0 or hi <= lo:
        return [lo, hi]
    ratio = log10(hi / lo)
    edges = [int(round(lo * 10 ** (ratio * i / bands))) for i in range(bands + 1)]
    edges[0], edges[-1] = lo, hi  # pin the ends against rounding drift
    return edges


def build_finder_selections(
    data: EmergingData,
    *,
    as_of: date,
    category_ids: list[int],
    per_page: int = 50,
    overrides: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """One Keepa `/query` selection per BSR sub-band (docs/emerging.md sampling).

    Splitting [bsr_min, bsr_max] into `sub_bands` log-spaced ranges and sampling
    each fixes the top-edge bias of a single ascending sort (which only ever
    returns the lowest-BSR products). Each sub-band gets an even share of
    `per_page`; the caller merges the returned ASINs and ranks locally by
    emergence. `sub_bands = 1` returns a single full-band selection (old behavior).
    """
    f = data.finder
    ov = overrides or {}
    bsr_min = int(ov.get("bsr_min", f.bsr_min))
    bsr_max = int(ov.get("bsr_max", f.bsr_max))
    bands = max(1, f.sub_bands)
    if bands <= 1:
        return [
            build_finder_selection(
                data,
                as_of=as_of,
                category_ids=category_ids,
                per_page=per_page,
                overrides=overrides,
            )
        ]
    per_band = max(1, -(-per_page // bands))  # ceil division, ≥ 1 per band
    edges = _log_band_edges(bsr_min, bsr_max, bands)
    selections: list[dict[str, Any]] = []
    for i in range(bands):
        band_ov = {**ov, "bsr_min": edges[i], "bsr_max": edges[i + 1]}
        selections.append(
            build_finder_selection(
                data,
                as_of=as_of,
                category_ids=category_ids,
                per_page=per_band,
                overrides=band_ov,
            )
        )
    return selections


def is_established_brand(brand: str | None, established: tuple[str, ...]) -> bool:
    """True when `brand` matches a configured large-brand name (case-insensitive,
    whole-string or word match) — a launch to FLAG as likely ad-driven, never to
    score differently or drop."""
    if not brand:
        return False
    b = brand.strip().lower()
    if not b:
        return False
    tokens = set(b.replace("-", " ").split())
    return any(name == b or name in tokens or (" " in name and name in b) for name in established)


# ---------------------------------------------------------------------------
# Emergence signal (pure)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EmergenceInput:
    asin: str
    as_of: date
    earliest_history: date | None  # oldest observation (age proxy)
    current_bsr: int | None
    bsr_slope_90d: float | None  # Theil–Sen slope of log10(BSR)/day; <0 = improving
    review_count: int | None
    history_days: int | None


@dataclass(frozen=True)
class SubSignal:
    name: str
    score: float | None  # 0-100, or None when its inputs are absent
    weight: float
    detail: str


@dataclass(frozen=True)
class EmergenceSignal:
    asin: str
    emergence_score: float | None  # weighted over AVAILABLE sub-signals; None if none
    age_days: int | None
    subsignals: tuple[SubSignal, ...]
    reasons: tuple[str, ...]

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.subsignals if s.score is None)


def compute_emergence(inp: EmergenceInput, thresholds: EmergenceThresholds) -> EmergenceSignal:
    """Deterministic emergence score from observable facts. Each sub-signal is
    0-100 (or None when unmeasurable); the score is the weighted mean over the
    available ones, so absent data lowers coverage rather than inventing a value."""
    t = thresholds
    age_days = None if inp.earliest_history is None else (inp.as_of - inp.earliest_history).days

    recency = _recency(age_days, t)
    traction = _traction(inp.current_bsr, t)
    momentum = _momentum(inp.bsr_slope_90d, inp.history_days, t)
    competition = _competition(inp.review_count, t)
    review_gap = _review_gap(traction.score, competition.score, t.w_review_gap)

    subs = (recency, traction, momentum, competition, review_gap)
    scored = [s for s in subs if s.score is not None]
    total_w = sum(s.weight for s in scored)
    emergence = (
        round(sum(s.score * s.weight for s in scored if s.score is not None) / total_w, 1)
        if total_w > 0
        else None
    )
    reasons = tuple(f"{s.name}: {s.detail}" for s in subs)
    return EmergenceSignal(inp.asin, emergence, age_days, subs, reasons)


def _recency(age_days: int | None, t: EmergenceThresholds) -> SubSignal:
    if age_days is None:
        return SubSignal("recency", None, t.w_recency, "age unknown")
    score = clamp(100.0 - norm(age_days, t.young_days, t.old_days))
    return SubSignal("recency", score, t.w_recency, f"{age_days}d since first tracked")


def _traction(bsr: int | None, t: EmergenceThresholds) -> SubSignal:
    if bsr is None or bsr <= 0:
        return SubSignal("traction", None, t.w_traction, "no current BSR")
    score = clamp(100.0 - log_norm(bsr, t.bsr_strong, t.bsr_weak))
    return SubSignal("traction", score, t.w_traction, f"current BSR {bsr:,}")


def _momentum(slope: float | None, history_days: int | None, t: EmergenceThresholds) -> SubSignal:
    if slope is None or (history_days is not None and history_days < 14):
        return SubSignal("momentum", None, t.w_momentum, "insufficient history for a trend")
    # slope < 0 = BSR improving. Map [0, slope_improving] → [0, 100].
    score = clamp(norm(-slope, 0.0, -t.slope_improving))
    direction = "improving" if slope < 0 else "flat/declining"
    return SubSignal("momentum", score, t.w_momentum, f"90d rank {direction}")


def _competition(reviews: int | None, t: EmergenceThresholds) -> SubSignal:
    if reviews is None:
        return SubSignal("competition", None, t.w_competition, "review count unknown")
    score = clamp(100.0 - norm(reviews, t.reviews_low, t.reviews_high))
    return SubSignal("competition", score, t.w_competition, f"{reviews} reviews")


def _review_gap(traction: float | None, competition: float | None, weight: float) -> SubSignal:
    if traction is None or competition is None:
        return SubSignal("review_gap", None, weight, "needs BSR + review count")
    score = clamp((traction * competition) ** 0.5)  # sells well AND still few reviews
    return SubSignal("review_gap", score, weight, "sales-vs-reviews gap")
