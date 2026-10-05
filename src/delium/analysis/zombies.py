"""Zombie-listing detection — pure.

A *zombie* is a listing that has been out of stock long-term yet still carries
strong social proof (rating + reviews) and proved real demand while it was alive.
This module decides, deterministically and from observable Keepa history only,
whether a candidate is a **Verified zombie**, **Possibly temporary**, or **Not a
zombie**, and emits the compliance flags that gate how (or whether) a seller may
act on it.

Design rules (same spirit as the other engines):
  * Pure: no network, no clock (the caller passes `as_of`), no randomness; the
    only I/O is loading the versioned `zombies_data/<marketplace>.toml` file.
  * Missing data LOWERS confidence, it never asserts "dead". A candidate is only
    "Verified" when the dead-duration, no-offer and social-proof facts are all
    present and pass; anything unknown caps the verdict at "Possibly temporary".
  * Compliance first: every result carries a brand flag, a suggested route that
    never puts a different product on an existing ASIN, and a manual-check line.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Any

from delium.analysis.curves import clamp, log_norm, norm
from delium.analysis.models import Confidence

ZOMBIES_DIR = Path(__file__).parent / "zombies_data"
DEFAULT_VERSION = "us"

# Keepa epoch for the minute→date math (mirrors providers/keepa.py).
_KEEPA_EPOCH_MINUTES = 21564000
_MIN_PER_DAY = 1440
_DAYS_PER_MONTH = 30.0

# Brand strings that mean "no real brand on the listing".
_GENERIC_BRANDS = frozenset(
    {"generic", "unbranded", "no brand", "nobrand", "oem", "n/a", "na", "none", "-"}
)

# Marketplace → the trademark office a seller must check before reviving.
_TRADEMARK_OFFICE = {
    "US": "USPTO (uspto.gov)",
    "UK": "the UK IPO (gov.uk/search-for-trademark)",
    "CA": "CIPO (ised-isde.canada.ca)",
    "DE": "the DPMA / EUIPO",
    "FR": "the INPI / EUIPO",
}


class ZombieError(Exception):
    """Raised when the zombie thresholds file is missing or malformed."""


class ZombieVerdict(StrEnum):
    VERIFIED = "Verified zombie"
    POSSIBLY_TEMPORARY = "Possibly temporary"
    NOT_A_ZOMBIE = "Not a zombie"


# ---------------------------------------------------------------------------
# Thresholds (external data file)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ZombieThresholds:
    version: str
    min_months: float
    ideal_months: float
    min_rating: float
    min_reviews: int
    strong_bsr: int
    weak_bsr: int
    gap_risk_each: float
    seller_active_risk: float
    high_risk: float
    w_dead_duration: float
    w_social_proof: float
    w_past_demand: float
    w_low_resurrection_risk: float
    w_current_demand: float
    verified_min: float
    possible_min: float


def load_zombie_data(marketplace: str = DEFAULT_VERSION) -> ZombieThresholds:
    """Load per-marketplace zombie thresholds (e.g. 'uk' → zombies_data/uk.toml).
    Falls back to the US file when a marketplace has no dedicated file yet."""
    name = marketplace.strip().lower() or DEFAULT_VERSION
    path = ZOMBIES_DIR / f"{name}.toml"
    if not path.exists():
        path = ZOMBIES_DIR / f"{DEFAULT_VERSION}.toml"
    if not path.exists():
        raise ZombieError(f"Zombie thresholds for {marketplace!r} not found at {path}.")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    d, s, p = raw["duration"], raw["social_proof"], raw["past_demand"]
    r, w, v = raw["resurrection"], raw["weights"], raw["verdict"]
    return ZombieThresholds(
        version=str(raw["version"]),
        min_months=float(d["min_months"]),
        ideal_months=float(d["ideal_months"]),
        min_rating=float(s["min_rating"]),
        min_reviews=int(s["min_reviews"]),
        strong_bsr=int(p["strong_bsr"]),
        weak_bsr=int(p["weak_bsr"]),
        gap_risk_each=float(r["gap_risk_each"]),
        seller_active_risk=float(r["seller_active_risk"]),
        high_risk=float(r["high_risk"]),
        w_dead_duration=float(w["dead_duration"]),
        w_social_proof=float(w["social_proof"]),
        w_past_demand=float(w["past_demand"]),
        w_low_resurrection_risk=float(w["low_resurrection_risk"]),
        w_current_demand=float(w.get("current_demand", 0.0)),
        verified_min=float(v["verified_min"]),
        possible_min=float(v["possible_min"]),
    )


# ---------------------------------------------------------------------------
# Stock timeline (pure, from raw Keepa change-points incl. -1 gaps)
# ---------------------------------------------------------------------------
def _date_to_keepa_minute(d: date) -> int:
    return (d - date(1970, 1, 1)).days * _MIN_PER_DAY - _KEEPA_EPOCH_MINUTES


def _keepa_minute_to_date(km: int) -> date:
    return date(1970, 1, 1).fromordinal(
        date(1970, 1, 1).toordinal() + (km + _KEEPA_EPOCH_MINUTES) // _MIN_PER_DAY
    )


@dataclass(frozen=True)
class StockTimeline:
    """What the NEW/offer-count history says about availability over time."""

    observed: bool  # any usable history at all
    currently_out_of_stock: bool | None
    days_out_of_stock: int | None  # continuous, up to `as_of`; 0 when in stock
    restock_gaps: int  # historical out→in transitions (resurrections)
    last_offer_date: date | None  # when the last offer disappeared
    in_stock_bsr_median: int | None  # BSR median over in-stock intervals
    in_stock_bsr_best: int | None  # best (lowest) BSR while in stock

    @property
    def months_out_of_stock(self) -> float | None:
        if self.days_out_of_stock is None:
            return None
        return round(self.days_out_of_stock / _DAYS_PER_MONTH, 1)


def build_stock_timeline(
    new_changes: list[tuple[int, int]],
    count_new_changes: list[tuple[int, int]],
    sales_changes: list[tuple[int, int]],
    *,
    as_of: date,
) -> StockTimeline:
    """Reconstruct availability from raw Keepa change-points (keepa-minute, value),
    where a -1 in the NEW series (or 0/-1 in the offer-count series) marks a
    no-offer / out-of-stock transition.

    `days_out_of_stock` is the length of the CURRENT, still-open out-of-stock run
    measured up to `as_of` — i.e. only counted when the series ends out of stock.
    A series ending in stock yields 0 (not a zombie by duration). `restock_gaps`
    counts every historical out→in transition (the resurrection pattern)."""
    # Prefer the NEW price series (−1 == no offer); fall back to offer-count.
    use_count = not new_changes
    source = count_new_changes if use_count else new_changes
    if not source:
        return StockTimeline(False, None, None, 0, None, None, None)

    def in_stock(value: int) -> bool:
        return value > 0 if use_count else value >= 0

    # Collapse consecutive equal states into runs of (start_km, in_stock).
    runs: list[tuple[int, bool]] = []
    for km, value in sorted(source):
        st = in_stock(value)
        if not runs or runs[-1][1] != st:
            runs.append((km, st))

    restock_gaps = sum(1 for i in range(1, len(runs)) if runs[i][1] and not runs[i - 1][1])
    last_km, last_state = runs[-1]
    as_of_km = _date_to_keepa_minute(as_of)

    if last_state:
        currently_oos = False
        days_oos = 0
        last_offer_date: date | None = None
    else:
        currently_oos = True
        days_oos = max(0, (as_of_km - last_km) // _MIN_PER_DAY)
        last_offer_date = _keepa_minute_to_date(last_km)

    # BSR observed during in-stock intervals = proven past demand.
    intervals = _in_stock_intervals(runs, as_of_km)
    in_stock_bsr = [
        value for km, value in sorted(sales_changes) if value > 0 and _within(km, intervals)
    ]
    bsr_median = int(median(in_stock_bsr)) if in_stock_bsr else None
    bsr_best = min(in_stock_bsr) if in_stock_bsr else None

    return StockTimeline(
        observed=True,
        currently_out_of_stock=currently_oos,
        days_out_of_stock=days_oos,
        restock_gaps=restock_gaps,
        last_offer_date=last_offer_date,
        in_stock_bsr_median=bsr_median,
        in_stock_bsr_best=bsr_best,
    )


def _in_stock_intervals(runs: list[tuple[int, bool]], as_of_km: int) -> list[tuple[int, int]]:
    """[(start_km, end_km)] intervals during which the listing was in stock."""
    intervals: list[tuple[int, int]] = []
    for i, (km, st) in enumerate(runs):
        if not st:
            continue
        end = runs[i + 1][0] if i + 1 < len(runs) else as_of_km
        intervals.append((km, end))
    return intervals


def _within(km: int, intervals: list[tuple[int, int]]) -> bool:
    return any(start <= km < end for start, end in intervals)


# ---------------------------------------------------------------------------
# Compliance flags (shown on every result)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ComplianceFlags:
    brand_label: str  # "generic/unbranded" or the named brand
    generic: bool
    routes: tuple[str, ...]  # suggested, compliant routes (default first)
    manual_check: str  # trademark + policy reminder


def _is_generic_brand(brand: str | None) -> bool:
    if brand is None:
        return True
    b = brand.strip().lower()
    return (not b) or b in _GENERIC_BRANDS


def compliance_flags(brand: str | None, marketplace: str = "US") -> ComplianceFlags:
    """Brand flag + compliant route(s) + the manual-check reminder. The DEFAULT
    route is always to launch your own improved product on a NEW listing (the
    zombie is demand proof). A revival route is offered ONLY when the brand looks
    generic — and never by placing a different product on the existing ASIN."""
    generic = _is_generic_brand(brand)
    label = "generic/unbranded" if generic else (brand or "").strip()
    office = _TRADEMARK_OFFICE.get(marketplace.strip().upper(), "your local trademark office")
    routes = [
        "Launch your own improved version on a NEW listing (the zombie is demand proof).",
    ]
    if generic:
        routes.append(
            "Possible revival — ONLY if you can source the IDENTICAL product AND the brand is "
            "generic, or you hold the brand rights. Never list a different product on this ASIN."
        )
    else:
        routes.append(
            f"No revival: '{label}' is a named brand — selling on its ASIN without authorisation "
            "risks IP/counterfeit action. Compete with your own NEW listing instead."
        )
    manual_check = (
        f"Manual check required before acting: trademark search at {office}, and review Amazon's "
        "anti-counterfeit / listing-ownership policies. This tool does not clear you legally."
    )
    return ComplianceFlags(label, generic, tuple(routes), manual_check)


# ---------------------------------------------------------------------------
# Verification (deterministic)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ZombieEvidence:
    """Everything the verdict needs. Absent fields drop their component (missing =
    unknown) rather than being treated as dead/alive."""

    asin: str
    marketplace: str
    timeline: StockTimeline
    rating: float | None = None  # stars, 0-5
    reviews: int | None = None
    monthly_sold: int | None = None  # Keepa "bought past month" (usually 0 for a dead one)
    brand: str | None = None
    amazon_on_listing: bool | None = None  # K4 — sold by Amazon
    has_current_new_offer: bool | None = None  # True = NOT out of stock now
    seller_active_elsewhere: bool | None = None  # original seller still selling → risk
    current_demand: float | None = None  # 0-100 SERP signal (top candidates only)


@dataclass(frozen=True)
class SubScore:
    name: str
    score: float | None  # 0-100, or None when its inputs are absent
    weight: float
    detail: str


@dataclass(frozen=True)
class ZombieResult:
    asin: str
    marketplace: str
    verdict: ZombieVerdict
    score: float | None  # 0-100 weighted over available components
    confidence: Confidence
    resurrection_risk: float | None  # 0-100 (higher = more likely to come back)
    components: tuple[SubScore, ...]
    reasons: tuple[str, ...]
    missing: tuple[str, ...]
    evidence: ZombieEvidence
    compliance: ComplianceFlags


def _social_proof_score(ev: ZombieEvidence, t: ZombieThresholds) -> float | None:
    if ev.rating is None or ev.reviews is None:
        return None
    rating_s = norm(ev.rating, t.min_rating, 5.0)
    reviews_s = log_norm(float(ev.reviews), float(max(1, t.min_reviews)), float(t.min_reviews) * 20)
    return round((rating_s + reviews_s) / 2, 1)


def _past_demand_score(ev: ZombieEvidence, t: ZombieThresholds) -> float | None:
    bsr = ev.timeline.in_stock_bsr_median
    if bsr is not None and bsr > 0:
        # Lower BSR while in stock = more demand. 100 at strong_bsr, 0 at weak_bsr.
        return round(100.0 - log_norm(float(bsr), float(t.strong_bsr), float(t.weak_bsr)), 1)
    if ev.monthly_sold:  # fallback: Keepa units (rarely present on a dead listing)
        return round(log_norm(float(ev.monthly_sold), 50.0, 5000.0), 1)
    return None


def _resurrection_risk(ev: ZombieEvidence, t: ZombieThresholds) -> float | None:
    if not ev.timeline.observed:
        return None
    risk = ev.timeline.restock_gaps * t.gap_risk_each
    if ev.seller_active_elsewhere:
        risk += t.seller_active_risk
    return clamp(risk)


def compute_zombie(ev: ZombieEvidence, thresholds: ZombieThresholds) -> ZombieResult:
    """Deterministic verdict from observable facts. Hard gates can only make a
    candidate LESS of a zombie; unknowns cap the verdict at 'Possibly temporary'."""
    t = thresholds
    tl = ev.timeline
    reasons: list[str] = []
    compliance = compliance_flags(ev.brand, ev.marketplace)

    # --- components (0-100 each; None when unmeasurable) ---
    months = tl.months_out_of_stock
    dead_score: float | None
    if not tl.observed or months is None:
        dead_score = None
    elif not tl.currently_out_of_stock:
        dead_score = 0.0
    else:
        dead_score = round(norm(months, t.min_months, t.ideal_months), 1)

    social = _social_proof_score(ev, t)
    past = _past_demand_score(ev, t)
    risk = _resurrection_risk(ev, t)
    low_risk_score = None if risk is None else round(100.0 - risk, 1)

    components = [
        SubScore(
            "dead_duration",
            dead_score,
            t.w_dead_duration,
            "—" if months is None else f"{months} months out of stock",
        ),
        SubScore(
            "social_proof",
            social,
            t.w_social_proof,
            "—"
            if (ev.rating is None or ev.reviews is None)
            else f"{ev.rating:.1f}★ / {ev.reviews} reviews",
        ),
        SubScore(
            "past_demand",
            past,
            t.w_past_demand,
            "—"
            if tl.in_stock_bsr_median is None
            else f"in-stock BSR median ~{tl.in_stock_bsr_median:,}",
        ),
        SubScore(
            "low_resurrection_risk",
            low_risk_score,
            t.w_low_resurrection_risk,
            "—" if risk is None else f"{tl.restock_gaps} restock gap(s), risk {risk:.0f}",
        ),
    ]
    if ev.current_demand is not None:
        components.append(
            SubScore(
                "current_demand",
                round(ev.current_demand, 1),
                t.w_current_demand,
                f"SERP demand {ev.current_demand:.0f}/100",
            )
        )

    available = [c for c in components if c.score is not None]
    missing = tuple(c.name for c in components if c.score is None)
    if available:
        wsum = sum(c.weight for c in available) or 1.0
        score: float | None = round(sum((c.score or 0.0) * c.weight for c in available) / wsum, 1)
    else:
        score = None

    # --- hard gates (can only lower the verdict) ---
    not_a_zombie = False
    if ev.has_current_new_offer is True or tl.currently_out_of_stock is False:
        not_a_zombie = True
        reasons.append("currently in stock / has a live offer — not dead.")
    if ev.amazon_on_listing is True:
        not_a_zombie = True
        reasons.append("sold by Amazon — not an openable gap.")
    if ev.rating is not None and ev.rating < t.min_rating:
        not_a_zombie = True
        reasons.append(f"rating {ev.rating:.1f}★ below the {t.min_rating:.1f}★ floor.")
    if ev.reviews is not None and ev.reviews < t.min_reviews:
        not_a_zombie = True
        reasons.append(f"only {ev.reviews} reviews (< {t.min_reviews}) — weak social proof.")

    too_fresh = tl.currently_out_of_stock is True and months is not None and months < t.min_months
    risk_high = risk is not None and risk >= t.high_risk
    # Unknowns that forbid a 'Verified' claim.
    unknown_blockers = (
        not tl.observed or months is None or ev.rating is None or ev.reviews is None or past is None
    )

    # --- confidence from coverage ---
    coverage = len(available) / max(1, len(components))
    if unknown_blockers or coverage < 0.5:
        confidence = Confidence.LOW
    elif missing:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.HIGH

    # --- verdict ---
    if not_a_zombie:
        verdict = ZombieVerdict.NOT_A_ZOMBIE
    elif too_fresh:
        verdict = ZombieVerdict.POSSIBLY_TEMPORARY
        reasons.append(
            f"out of stock {months} months (< {t.min_months:.0f}-month floor) — may be temporary."
        )
    elif unknown_blockers:
        verdict = ZombieVerdict.POSSIBLY_TEMPORARY
        reasons.append("key evidence missing — cannot verify as dead (treated as unknown).")
    elif risk_high:
        verdict = ZombieVerdict.POSSIBLY_TEMPORARY
        reasons.append(
            f"resurrection risk {risk:.0f}/100 (repeated restocks or active seller) — may return."
        )
    elif score is not None and score >= t.verified_min:
        verdict = ZombieVerdict.VERIFIED
        reasons.append(
            f"out of stock {months} months, {ev.rating:.1f}★/{ev.reviews} reviews, "
            f"proven in-stock demand — a verified long-dead listing."
        )
    elif score is not None and score >= t.possible_min:
        verdict = ZombieVerdict.POSSIBLY_TEMPORARY
        reasons.append("some zombie signals present but not decisive.")
    else:
        verdict = ZombieVerdict.NOT_A_ZOMBIE
        reasons.append("signals too weak to treat as a zombie.")

    return ZombieResult(
        asin=ev.asin,
        marketplace=ev.marketplace,
        verdict=verdict,
        score=score,
        confidence=confidence,
        resurrection_risk=risk,
        components=tuple(components),
        reasons=tuple(reasons),
        missing=missing,
        evidence=ev,
        compliance=compliance,
    )


@dataclass(frozen=True)
class ZombieFinderConfig:
    """Finder-side filters for the zombie sweep (verified against the official
    Keepa backend ProductFinderRequest — see docs/zombies.md)."""

    min_rating: float = 4.0
    min_reviews: int = 50
    out_of_stock_pct_90: int = 90  # % of the last 90 days with no NEW offer
    sort_field: str = "current_COUNT_REVIEWS"


def build_zombie_finder_selection(
    cfg: ZombieFinderConfig, *, page: int = 0, per_page: int = 50, core_only: bool = False
) -> dict[str, Any]:
    """Keepa `/query` selection for out-of-stock-but-reviewed listings. Field
    names/types verified against github.com/keepacom/api_backend
    ProductFinderRequest:
      current_COUNT_NEW_lte (Integer)   — 0 ⇒ no current new offers (primary
                                          dead-listing signal)
      current_RATING_gte (Integer, 0-50)— rating floor (4.0★ ⇒ 40)
      current_COUNT_REVIEWS_gte (Integer)
      outOfStockPercentage90_NEW_gte (Integer) — OOS share of the last 90 days;
                                          OPTIONAL (dropped on the fallback retry)
      productType (Byte[]) / page / perPage / sort (String[][])

    We do NOT send `buyBoxIsAmazon=false`: that Boolean matches only listings
    that HAVE a (non-Amazon) buy box, so it excludes the very listings we want —
    dead ones have no buy box at all. Amazon-sold listings are excluded AFTER
    hydration from the Amazon offer history instead (see `zombie_evidence_from_raw`).
    No price/sales band either: a dead listing has no NEW price.

    `core_only=True` drops the optional OOS-percentage filter, used as a fallback
    retry when the full selection returns 0 (so one over-strict optional filter
    can't zero out the sweep)."""
    selection: dict[str, Any] = {
        "current_COUNT_NEW_lte": 0,
        "current_RATING_gte": int(round(cfg.min_rating * 10)),
        "current_COUNT_REVIEWS_gte": int(cfg.min_reviews),
        "productType": [0],
        "page": page,
        "perPage": per_page,
        "sort": [[cfg.sort_field, "desc"]],  # strongest social proof first
    }
    if not core_only and cfg.out_of_stock_pct_90 > 0:
        selection["outOfStockPercentage90_NEW_gte"] = int(cfg.out_of_stock_pct_90)
    return selection
