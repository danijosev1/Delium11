"""Category momentum — which categories are heating up? (pure).

Aggregates an already-scored product set by Keepa category/subcategory and
scores each category on an ABSOLUTE 0-100 scale from fixed thresholds in
`momentum_data/<version>.toml`. Categories may be ranked against each other
within a scan, but the score itself is not batch-relative — "72/100" means the
same thing on any day.

Pure: no I/O beyond its versioned data file, no network, no clock.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from delium.analysis.curves import clamp, log_norm, norm

MOMENTUM_DIR = Path(__file__).parent / "momentum_data"
DEFAULT_VERSION = "us"


class MomentumError(Exception):
    """Raised when the momentum thresholds file is missing or malformed."""


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MomentumProduct:
    """One scored product's momentum inputs. All optional except category."""

    category: str | None
    emergence_score: float | None = None  # 0-100; >= emerging_min counts as emerging
    bsr_change_90d: float | None = None  # signed fraction, negative = rank improving
    age_days: int | None = None
    review_count: int | None = None
    monthly_sold: int | None = None  # Keepa "bought past month" (units)
    price_cents: int | None = None
    profit_net_cents: int | None = None  # net profit per unit at the profile target price


@dataclass(frozen=True)
class CategoryMetrics:
    emerging_count: int
    median_bsr_change_90d: float | None
    new_entrants_90d: int
    median_reviews_top10: float | None
    aggregate_monthly_revenue_usd: float | None
    avg_profit_at_target_usd: float | None
    sample_size: int


@dataclass(frozen=True)
class CategoryMomentum:
    category: str
    score: float | None
    metrics: CategoryMetrics
    reason: str


@dataclass(frozen=True)
class MomentumThresholds:
    version: str
    weights: dict[str, float]
    raw: dict[str, Any]
    emerging_min: float

    def w(self, name: str) -> float:
        return float(self.weights.get(name, 0.0))

    def sect(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get(name, {}))


def load_momentum_data(version: str = DEFAULT_VERSION) -> MomentumThresholds:
    path = MOMENTUM_DIR / f"{version}.toml"
    if not path.exists():
        raise MomentumError(f"Momentum thresholds {version!r} not found at {path}.")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    weights = {k: float(v) for k, v in raw.get("weights", {}).items()}
    emerging_min = float(raw.get("emerging_count", {}).get("emerging_min", 60.0))
    return MomentumThresholds(
        version=str(raw["version"]), weights=weights, raw=raw, emerging_min=emerging_min
    )


# ---------------------------------------------------------------------------
# Aggregation + scoring
# ---------------------------------------------------------------------------
def _metrics(products: list[MomentumProduct], emerging_min: float) -> CategoryMetrics:
    emerging = sum(
        1 for p in products if p.emergence_score is not None and p.emergence_score >= emerging_min
    )
    changes = [p.bsr_change_90d for p in products if p.bsr_change_90d is not None]
    new_entrants = sum(1 for p in products if p.age_days is not None and p.age_days <= 90)
    reviews = [p.review_count for p in products if p.review_count is not None]
    revenue = sum(
        p.monthly_sold * p.price_cents
        for p in products
        if p.monthly_sold is not None and p.price_cents is not None
    )
    has_revenue = any(p.monthly_sold is not None and p.price_cents is not None for p in products)
    profits = [p.profit_net_cents for p in products if p.profit_net_cents is not None]
    return CategoryMetrics(
        emerging_count=emerging,
        median_bsr_change_90d=(median(changes) if changes else None),
        new_entrants_90d=new_entrants,
        median_reviews_top10=(median(reviews) if reviews else None),
        aggregate_monthly_revenue_usd=(revenue / 100.0 if has_revenue else None),
        avg_profit_at_target_usd=(sum(profits) / len(profits) / 100.0 if profits else None),
        sample_size=len(products),
    )


def _score(metrics: CategoryMetrics, t: MomentumThresholds) -> float | None:
    subs: list[tuple[float, float]] = []  # (score, weight)

    ec = t.sect("emerging_count")
    subs.append(
        (
            clamp(
                log_norm(max(metrics.emerging_count, 0) + 1, float(ec["lo"]) + 1, float(ec["hi"]))
            ),
            t.w("emerging_count"),
        )
    )
    if metrics.median_bsr_change_90d is not None:
        bi = t.sect("bsr_improvement")
        subs.append(
            (
                norm(-metrics.median_bsr_change_90d, 0.0, float(bi["improve_hi"])),
                t.w("bsr_improvement"),
            )
        )
    ne = t.sect("new_entrants")
    subs.append((norm(metrics.new_entrants_90d, 0.0, float(ne["cap"])), t.w("new_entrants")))
    if metrics.aggregate_monthly_revenue_usd is not None:
        rv = t.sect("revenue")
        subs.append(
            (
                log_norm(
                    max(metrics.aggregate_monthly_revenue_usd, 1.0),
                    float(rv["lo"]),
                    float(rv["hi"]),
                ),
                t.w("revenue"),
            )
        )

    total_w = sum(w for _, w in subs)
    if total_w <= 0:
        return None
    return round(sum(s * w for s, w in subs) / total_w, 1)


def _reason(category: str, metrics: CategoryMetrics, score: float | None) -> str:
    if score is None:
        return f"{category}: not enough data to score momentum."
    heat = "hot" if score >= 65 else ("warming" if score >= 45 else "quiet")
    bits = [f"{metrics.emerging_count} emerging", f"{metrics.new_entrants_90d} new in 90d"]
    if metrics.median_bsr_change_90d is not None:
        bits.append(f"median 90d BSR {metrics.median_bsr_change_90d:+.0%}")
    if metrics.aggregate_monthly_revenue_usd is not None:
        bits.append(f"~${metrics.aggregate_monthly_revenue_usd:,.0f}/mo est. revenue")
    return f"{category} looks {heat} ({score:.0f}/100): " + ", ".join(bits) + "."


def compute_category_momentum(
    products: list[MomentumProduct],
    thresholds: MomentumThresholds,
    *,
    top_n: int = 8,
    min_sample: int = 1,
) -> list[CategoryMomentum]:
    """Group by category, score each on absolute thresholds, return the top N by
    score (ties broken by sample size then name for determinism). Products with no
    category are ignored."""
    buckets: dict[str, list[MomentumProduct]] = defaultdict(list)
    for p in products:
        if p.category:
            buckets[_top_segment(p.category)].append(p)

    out: list[CategoryMomentum] = []
    for category, group in buckets.items():
        if len(group) < min_sample:
            continue
        metrics = _metrics(group, thresholds.emerging_min)
        score = _score(metrics, thresholds)
        out.append(CategoryMomentum(category, score, metrics, _reason(category, metrics, score)))

    out.sort(key=lambda c: (-(c.score or -1.0), -c.metrics.sample_size, c.category))
    return out[:top_n]


def _top_segment(category_path: str) -> str:
    """The top-level department from a Keepa breadcrumb path (e.g. 'Home &
    Kitchen > Storage' → 'Home & Kitchen')."""
    return category_path.split(">")[0].strip() or category_path.strip()
