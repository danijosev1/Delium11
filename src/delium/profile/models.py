"""Research Profile model + the pure derivations that apply it.

`ResearchProfile` mirrors the `research_profiles` table. Its methods turn the
seller's preferences into concrete inputs the rest of the system already
understands:

  * `finder_overrides()`  → the Keepa Product Finder override keys the finder
     already honors (price band, review ceiling), so no engine change is needed.
  * `profit_overrides()`  → per-unit COGS + freight in cents for the profit
     engine (via discovery.assembly.ProfitOverrides), used everywhere unless a
     per-product override is supplied.
  * `meets_targets()` / `risk_rank()` → highlight/sort hints ONLY.

Nothing here changes a kill, gate, or weight; unset (None) fields mean "no
preference" and fall back to the engine defaults.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from delium.discovery.assembly import ProfitOverrides


def _json_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        raw = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return ()
    return tuple(str(v) for v in raw) if isinstance(raw, list) else ()


class RiskTolerance(StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class CogsMode(StrEnum):
    PCT = "pct"  # cogs_value is a 0-1 fraction of selling price
    UNIT = "unit"  # cogs_value is USD per unit


@dataclass(frozen=True)
class ProfitProfile:
    """The profit-relevant slice of a profile (COGS + freight assumptions)."""

    cogs_mode: CogsMode = CogsMode.PCT
    cogs_value: float = 0.25
    freight_per_kg_usd: float = 6.0

    def cogs_cents(self, price_cents: int) -> int:
        if self.cogs_mode is CogsMode.UNIT:
            return max(0, round(self.cogs_value * 100))
        return max(0, round(self.cogs_value * price_cents))

    def freight_cents(self, weight_g: int | None) -> int | None:
        """Per-unit freight from a per-kg rate + the unit weight. None when the
        weight is unknown, so the caller falls back to the config assumption."""
        if weight_g is None or weight_g <= 0:
            return None
        return max(0, round(self.freight_per_kg_usd * (weight_g / 1000.0) * 100))


@dataclass(frozen=True)
class ResearchProfile:
    """One named preference set. Money is in cents; percentages are 0-1 fractions.
    `None` means 'no preference' (fall back to the engine default)."""

    name: str
    id: str | None = None
    is_active: bool = False
    budget_usd: float | None = None
    target_net_margin: float | None = None
    target_roi: float | None = None
    price_min_cents: int | None = None
    price_max_cents: int | None = None
    min_monthly_sales: int | None = None
    max_reviews: int | None = None
    max_weight_g: int | None = None
    max_size_tier: str | None = None
    preferred_categories: tuple[str, ...] = ()
    excluded_categories: tuple[str, ...] = ()
    marketplaces: tuple[str, ...] = ("US",)
    cogs_mode: CogsMode = CogsMode.PCT
    cogs_value: float = 0.25
    freight_per_kg_usd: float = 6.0
    risk_tolerance: RiskTolerance = RiskTolerance.BALANCED
    # -- daily-scan spending caps (Part 3) ---------------------------------
    # A scan aborts in preflight before spending if its projection exceeds
    # either cap. Conservative defaults suit an unattended daily run; the CLI
    # uses these when --max-spend / --budget-cap are not passed.
    max_scan_usd: float = 5.0
    keepa_token_cap: int = 1500
    # -- daily-scan sizing (drives the projected token/USD cost) -----------
    # sweep_size: raw ASINs the sweep brings back; scan_competitor_sets: how many
    # top products get a page-one competitor set. The CLI uses these when
    # --sweep-size / --competitor-sets are omitted.
    scan_sweep_size: int = 300
    scan_competitor_sets: int = 40

    # -- profit ------------------------------------------------------------
    @property
    def profit_profile(self) -> ProfitProfile:
        return ProfitProfile(self.cogs_mode, self.cogs_value, self.freight_per_kg_usd)

    def profit_overrides(self, *, price_cents: int | None, weight_g: int | None) -> ProfitOverrides:
        """A `ProfitOverrides` carrying the profile's COGS + freight for one
        product. COGS needs a price (pct mode) — with no price and pct mode it is
        left unset so the engine keeps its assumption. Freight needs a weight."""
        from delium.discovery.assembly import ProfitOverrides

        pp = self.profit_profile
        cogs = None
        if pp.cogs_mode is CogsMode.UNIT:
            cogs = pp.cogs_cents(0)
        elif price_cents is not None:
            cogs = pp.cogs_cents(price_cents)
        return ProfitOverrides(cogs_cents=cogs, freight_cents=pp.freight_cents(weight_g))

    # -- finder ------------------------------------------------------------
    def finder_overrides(self) -> dict[str, int]:
        """Override keys the Keepa Product Finder selection already honors. Only
        the price band + review ceiling map to existing finder fields; category
        and monthly-sales preferences are applied as result filters/highlights
        (see `matches_*`), so no finder/engine change is required."""
        ov: dict[str, int] = {}
        if self.price_min_cents is not None:
            ov["price_min_cents"] = self.price_min_cents
        if self.price_max_cents is not None:
            ov["price_max_cents"] = self.price_max_cents
        if self.max_reviews is not None:
            ov["reviews_max"] = self.max_reviews
        return ov

    def category_ids(self) -> list[int]:
        """Preferred categories that are numeric Keepa root-category ids."""
        return [int(c) for c in self.preferred_categories if str(c).strip().isdigit()]

    # -- highlight / sort (never the hard rules) ---------------------------
    def meets_margin(self, net_margin: float | None) -> bool:
        return self.target_net_margin is None or (
            net_margin is not None and net_margin >= self.target_net_margin
        )

    def meets_roi(self, roi: float | None) -> bool:
        return self.target_roi is None or (roi is not None and roi >= self.target_roi)

    def meets_targets(self, *, net_margin: float | None, roi: float | None) -> bool:
        return self.meets_margin(net_margin) and self.meets_roi(roi)

    def units_affordable(self, landed_cost_cents: int | None) -> int | None:
        """How many units the budget buys at the given per-unit landed cost."""
        if self.budget_usd is None or not landed_cost_cents or landed_cost_cents <= 0:
            return None
        return int((self.budget_usd * 100) // landed_cost_cents)

    def excludes_category(self, category_path: str | None) -> bool:
        """Soft exclusion (sort/highlight only — NOT a hard kill). True when the
        product's category path matches any excluded-category term."""
        if not category_path or not self.excluded_categories:
            return False
        hay = category_path.lower()
        return any(term.strip().lower() in hay for term in self.excluded_categories if term.strip())

    @property
    def risk_rank(self) -> int:
        """0 conservative … 2 aggressive — a sort/highlight lever only."""
        return {
            RiskTolerance.CONSERVATIVE: 0,
            RiskTolerance.BALANCED: 1,
            RiskTolerance.AGGRESSIVE: 2,
        }[self.risk_tolerance]

    # -- persistence conversion -------------------------------------------
    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ResearchProfile:
        return cls(
            id=row["id"],
            name=row["name"],
            is_active=bool(row["is_active"]),
            budget_usd=row["budget_usd"],
            target_net_margin=row["target_net_margin"],
            target_roi=row["target_roi"],
            price_min_cents=row["price_min_cents"],
            price_max_cents=row["price_max_cents"],
            min_monthly_sales=row["min_monthly_sales"],
            max_reviews=row["max_reviews"],
            max_weight_g=row["max_weight_g"],
            max_size_tier=row["max_size_tier"],
            preferred_categories=_json_list(row["preferred_categories"]),
            excluded_categories=_json_list(row["excluded_categories"]),
            marketplaces=_json_list(row["marketplaces"]) or ("US",),
            cogs_mode=CogsMode(row["cogs_mode"]),
            cogs_value=row["cogs_value"],
            freight_per_kg_usd=row["freight_per_kg_usd"],
            risk_tolerance=RiskTolerance(row["risk_tolerance"]),
            max_scan_usd=row["max_scan_usd"],
            keepa_token_cap=row["keepa_token_cap"],
            scan_sweep_size=row["scan_sweep_size"],
            scan_competitor_sets=row["scan_competitor_sets"],
        )

    def to_values(self) -> dict[str, Any]:
        """Column values for `repository.save_research_profile` (JSON-serialized
        list fields, enums as their string value, is_active as 0/1)."""
        return {
            "name": self.name,
            "is_active": int(self.is_active),
            "budget_usd": self.budget_usd,
            "target_net_margin": self.target_net_margin,
            "target_roi": self.target_roi,
            "price_min_cents": self.price_min_cents,
            "price_max_cents": self.price_max_cents,
            "min_monthly_sales": self.min_monthly_sales,
            "max_reviews": self.max_reviews,
            "max_weight_g": self.max_weight_g,
            "max_size_tier": self.max_size_tier,
            "preferred_categories": json.dumps(list(self.preferred_categories)),
            "excluded_categories": json.dumps(list(self.excluded_categories)),
            "marketplaces": json.dumps(list(self.marketplaces)),
            "cogs_mode": self.cogs_mode.value,
            "cogs_value": self.cogs_value,
            "freight_per_kg_usd": self.freight_per_kg_usd,
            "risk_tolerance": self.risk_tolerance.value,
            "max_scan_usd": self.max_scan_usd,
            "keepa_token_cap": self.keepa_token_cap,
            "scan_sweep_size": self.scan_sweep_size,
            "scan_competitor_sets": self.scan_competitor_sets,
        }


# Two shipped presets the DB is seeded with on first use (docs mention them).
DEFAULT_PRESETS: tuple[ResearchProfile, ...] = (
    ResearchProfile(
        name="Conservative $5k",
        is_active=True,
        budget_usd=5000,
        target_net_margin=0.30,
        target_roi=1.5,
        price_min_cents=1800,
        price_max_cents=4500,
        min_monthly_sales=300,
        max_reviews=300,
        max_weight_g=2000,
        max_size_tier="large_standard",
        marketplaces=("US",),
        cogs_mode=CogsMode.PCT,
        cogs_value=0.28,
        freight_per_kg_usd=7.0,
        risk_tolerance=RiskTolerance.CONSERVATIVE,
        max_scan_usd=5.0,
        keepa_token_cap=1500,
        scan_sweep_size=200,
        scan_competitor_sets=25,
    ),
    ResearchProfile(
        name="Growth $20k",
        is_active=False,
        budget_usd=20000,
        target_net_margin=0.25,
        target_roi=1.0,
        price_min_cents=1500,
        price_max_cents=7000,
        min_monthly_sales=150,
        max_reviews=800,
        max_weight_g=4000,
        max_size_tier="large_standard",
        marketplaces=("US",),
        cogs_mode=CogsMode.PCT,
        cogs_value=0.25,
        freight_per_kg_usd=6.0,
        risk_tolerance=RiskTolerance.AGGRESSIVE,
        max_scan_usd=8.0,
        keepa_token_cap=2500,
        scan_sweep_size=300,
        scan_competitor_sets=40,
    ),
)
