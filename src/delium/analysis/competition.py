"""Deterministic competition analysis engine (docs/analysis-engine.md §2,
docs/scoring-model.md §5).

Pure computation: no provider/DB/agent/ingestion/report imports, no network, no
LLM, no current-time calls (the caller passes `as_of`). Same inputs + same
config → same output. Missing data lowers confidence and drops components out of
the renormalized pillar (C5/C6 use the documented neutral fallback); it is never
guessed with optimistic assumptions.

Higher scores mean a MORE beatable market (per the scoring model's framing —
flawed incumbents are the opportunity). Listing quality is consumed from
listing.py's output and never recomputed here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from statistics import fmean, median, pstdev

from delium.analysis import curves
from delium.analysis.models import (
    BeatableSlots,
    BrandConcentration,
    CompetitionConfidence,
    CompetitionConfig,
    CompetitionInput,
    CompetitionReport,
    CompetitorSnapshot,
    Confidence,
    ListingQualityAdvantage,
    PriceCompetition,
    PricePoint,
    ReviewMoat,
    ReviewVelocity,
    Subscore,
)


# ---------------------------------------------------------------------------
# C1 — review moat
# ---------------------------------------------------------------------------
def _review_moat(top: Sequence[CompetitorSnapshot], cfg: CompetitionConfig) -> ReviewMoat:
    counts = [c.review_count for c in top if c.review_count is not None]
    if not counts:
        return ReviewMoat(None, None, None, None, "no review counts available")
    med = median(counts)
    score = 100.0 - curves.log_norm(med, cfg.review_moat_lo, cfg.review_moat_hi)
    return ReviewMoat(
        median_reviews=med,
        mean_reviews=fmean(counts),
        max_reviews=max(counts),
        score=score,
        detail=f"median {med:.0f} reviews across {len(counts)} competitors",
    )


# ---------------------------------------------------------------------------
# C2 — beatable slots
# ---------------------------------------------------------------------------
def _beatable_slots(top: Sequence[CompetitorSnapshot], cfg: CompetitionConfig) -> BeatableSlots:
    known = [c for c in top if c.review_count is not None]
    if not known:
        return BeatableSlots(0, 0, None, "no review counts to assess beatable slots")
    low_review = [
        c
        for c in known
        if c.review_count is not None and c.review_count < cfg.beatable_review_threshold
    ]
    weak = sum(
        1
        for c in low_review
        if c.listing_quality is None or c.listing_quality < cfg.weak_listing_threshold
    )
    count = len(low_review)
    score = min(count, cfg.beatable_cap) * cfg.beatable_per_slot
    return BeatableSlots(
        count=count,
        weak_listing_slots=weak,
        score=score,
        detail=f"{count} of {len(known)} competitors under {cfg.beatable_review_threshold} reviews",
    )


# ---------------------------------------------------------------------------
# C3 — review velocity of leaders
# ---------------------------------------------------------------------------
def _review_velocity(top: Sequence[CompetitorSnapshot], cfg: CompetitionConfig) -> ReviewVelocity:
    leaders = sorted(
        (c for c in top if c.review_count is not None),
        key=lambda c: c.review_count or 0,
        reverse=True,
    )[: cfg.velocity_leaders]
    velocities: list[float] = []
    for c in leaders:
        if c.review_count is not None and c.review_count_90d_ago is not None:
            delta = c.review_count - c.review_count_90d_ago
            velocities.append(max(0.0, delta) / 3.0)  # 90d → per month
    if not velocities:
        return ReviewVelocity(None, "unknown", 0, None, "no review history for leaders")
    med = median(velocities)
    score = 100.0 - curves.log_norm(med, cfg.velocity_lo, cfg.velocity_hi)
    if med < cfg.velocity_low_class:
        classification = "low"
    elif med > cfg.velocity_high_class:
        classification = "high"
    else:
        classification = "moderate"
    return ReviewVelocity(
        monthly_velocity=med,
        classification=classification,
        leaders_with_history=len(velocities),
        score=score,
        detail=f"median {med:.1f} new reviews/mo across {len(velocities)} leaders",
    )


# ---------------------------------------------------------------------------
# C4 — brand concentration / dominance
# ---------------------------------------------------------------------------
def _brand_concentration(
    top: Sequence[CompetitorSnapshot],
    recognized_brands: frozenset[str],
    cfg: CompetitionConfig,
) -> BrandConcentration:
    brands = [c.brand for c in top if c.brand]
    if not brands:
        return BrandConcentration(None, None, None, False, False, None, "no brand data")
    total = len(brands)
    counts: dict[str, int] = {}
    for b in brands:
        counts[b] = counts.get(b, 0) + 1
    top_brand = max(counts, key=lambda b: counts[b])
    top_share = counts[top_brand] / total
    hhi = sum((n / total) ** 2 for n in counts.values())
    big_present = any(b in recognized_brands for b in brands)
    score = max(0.0, 100.0 - top_share * 100.0 - (cfg.big_brand_penalty if big_present else 0.0))
    flag = hhi > cfg.hhi_flag_threshold
    return BrandConcentration(
        top_brand=top_brand,
        top_brand_slot_share=top_share,
        hhi=hhi,
        recognized_big_brand_present=big_present,
        concentration_flag=flag,
        score=score,
        detail=f"top brand {top_brand!r} holds {top_share:.0%} of slots, HHI {hhi:.2f}",
    )


# ---------------------------------------------------------------------------
# C5 — listing quality advantage (consumes listing.py output; never recomputes)
# ---------------------------------------------------------------------------
def _listing_advantage(
    top: Sequence[CompetitorSnapshot], cfg: CompetitionConfig
) -> ListingQualityAdvantage:
    qualities = [c.listing_quality for c in top if c.listing_quality is not None]
    if not qualities:
        return ListingQualityAdvantage(
            None, cfg.neutral_score, False, 0, "no listing quality data — neutral score"
        )
    avg = fmean(qualities)
    score = curves.clamp(100.0 - avg)  # worse incumbent listings → more opportunity
    available = avg < cfg.listing_advantage_threshold
    return ListingQualityAdvantage(
        avg_competitor_quality=avg,
        advantage_score=score,
        advantage_available=available,
        coverage=len(qualities),
        detail=f"avg competitor listing quality {avg:.1f}/100 across {len(qualities)}",
    )


# ---------------------------------------------------------------------------
# C6 — price competition + price-war detection
# ---------------------------------------------------------------------------
def _window(history: tuple[PricePoint, ...], as_of: date, days: int) -> list[PricePoint]:
    return [p for p in history if 0 <= (as_of - p.date).days <= days and p.price_cents > 0]


def _price_competition(
    top: Sequence[CompetitorSnapshot], as_of: date, cfg: CompetitionConfig
) -> PriceCompetition:
    current = [c.price_cents for c in top if c.price_cents is not None and c.price_cents > 0]
    median_price = round(median(current)) if current else None
    spread = (max(current) - min(current)) if current else None
    clustering = (pstdev(current) / fmean(current)) if len(current) >= 2 else None

    cvs: list[float] = []
    recent_low = 0
    for c in top:
        if not c.price_history:
            continue
        pts = _window(c.price_history, as_of, cfg.price_window_days)
        if len(pts) < 2:
            continue
        prices = [p.price_cents for p in pts]
        mean_price = fmean(prices)
        if mean_price > 0:
            cvs.append(pstdev(prices) / mean_price)
        low = min(prices)
        low_date = max(p.date for p in pts if p.price_cents == low)
        if (as_of - low_date).days <= cfg.price_war_recent_days:
            recent_low += 1

    median_cv = median(cvs) if cvs else None
    score = (
        100.0 - curves.norm(median_cv, cfg.price_cv_lo, cfg.price_cv_hi)
        if median_cv is not None
        else cfg.neutral_score
    )
    war = recent_low >= cfg.price_war_min_slots
    cv_txt = f"{median_cv:.2f}" if median_cv is not None else "n/a (no history)"
    return PriceCompetition(
        median_price_cents=median_price,
        price_spread_cents=spread,
        clustering_cv=clustering,
        median_price_cv_90d=median_cv,
        score=score,
        price_war_flag=war,
        competitors_at_recent_low=recent_low,
        detail=f"median 90d price CV {cv_txt}; {recent_low} at a recent low",
    )


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------
def _confidence(top: Sequence[CompetitorSnapshot], cfg: CompetitionConfig) -> CompetitionConfidence:
    n = len(top)
    if n == 0:
        return CompetitionConfidence(Confidence.LOW, 0, 0.0, 0.0, 0.0)
    review_hist_cov = sum(1 for c in top if c.review_count_90d_ago is not None) / n
    listing_cov = sum(1 for c in top if c.listing_quality is not None) / n
    price_hist_cov = sum(1 for c in top if c.price_history) / n
    review_count_frac = sum(1 for c in top if c.review_count is not None) / n

    points = 0.0
    points += 2 if n >= 8 else 1 if n >= 4 else 0
    points += 1 if review_count_frac >= 0.8 else 0
    points += 1 if review_hist_cov >= 0.3 else 0
    points += 1 if listing_cov >= 0.5 else 0
    points += 1 if price_hist_cov >= 0.5 else 0

    if points >= 5 and n >= 8 and review_count_frac >= 0.8:
        level = Confidence.HIGH
    elif points <= 1:
        level = Confidence.LOW
    else:
        level = Confidence.MEDIUM
    return CompetitionConfidence(level, n, review_hist_cov, listing_cov, price_hist_cov)


# ---------------------------------------------------------------------------
# Pillar assembly
# ---------------------------------------------------------------------------
def _pillar(components: Sequence[Subscore]) -> float:
    available = [c for c in components if c.value is not None]
    total_weight = sum(c.weight for c in available)
    if total_weight <= 0:
        return 0.0
    weighted = sum((c.value or 0.0) * c.weight for c in available)
    return curves.clamp(weighted / total_weight)


def _data_gaps(top: Sequence[CompetitorSnapshot]) -> tuple[str, ...]:
    gaps: list[str] = []
    if not any(c.review_count is not None for c in top):
        gaps.append("review_counts")
    if not any(c.review_count_90d_ago is not None for c in top):
        gaps.append("review_history")
    if not any(c.brand for c in top):
        gaps.append("brands")
    if not any(c.listing_quality is not None for c in top):
        gaps.append("listing_quality")
    if not any(c.price_cents is not None for c in top):
        gaps.append("prices")
    if not any(c.price_history for c in top):
        gaps.append("price_history")
    return tuple(gaps)


def analyze_competition(
    data: CompetitionInput, config: CompetitionConfig | None = None
) -> CompetitionReport:
    """Analyze the competitive top-N and return the 0-100 competition pillar."""
    cfg = config or CompetitionConfig()
    top = data.competitors[: cfg.top_n]

    moat = _review_moat(top, cfg)
    beatable = _beatable_slots(top, cfg)
    velocity = _review_velocity(top, cfg)
    brand = _brand_concentration(top, data.recognized_brands, cfg)
    listing = _listing_advantage(top, cfg)
    price = _price_competition(top, data.as_of, cfg)

    components = (
        Subscore("review_moat", moat.score, cfg.weight_review_moat, moat.detail),
        Subscore("beatable_slots", beatable.score, cfg.weight_beatable_slots, beatable.detail),
        Subscore("review_velocity", velocity.score, cfg.weight_review_velocity, velocity.detail),
        Subscore("brand_dominance", brand.score, cfg.weight_brand_dominance, brand.detail),
        Subscore("listing_gap", listing.advantage_score, cfg.weight_listing_gap, listing.detail),
        Subscore("price_competition", price.score, cfg.weight_price_competition, price.detail),
    )

    return CompetitionReport(
        pillar_score=_pillar(components),
        confidence=_confidence(top, cfg),
        review_moat=moat,
        beatable_slots=beatable,
        review_velocity=velocity,
        brand_concentration=brand,
        listing_quality_advantage=listing,
        price_competition=price,
        components=components,
        data_gaps=_data_gaps(top),
    )
