"""Deterministic cross-market discovery engine (docs/cross-market.md).

Detects products proven in one Amazon marketplace that look underpenetrated but
credibly demanded in another. This is a DISCOVERY SIGNAL, not one of the five
opportunity pillars and not a second Buy/Test/Avoid system — the existing
scoring engine still owns the product verdict.

Central principle, encoded conservatively (false positives are worse than
missed matches):

    success elsewhere + target-market evidence + competition/maturity gap
    + transferability + data confidence = cross-market opportunity signal

and, deliberately:

    no target demand  ≠  opportunity
    no listings       ≠  opportunity   (absence of evidence is not evidence)

Contract (tested): pure and deterministic — no LLM, provider, ingestion,
database, network, clock, or random access. It consumes already-normalized
inputs only; every threshold/weight comes from the passed-in DeliumConfig.
Same inputs + same config → identical output.
"""

from __future__ import annotations

import re

from delium.analysis.curves import clamp, log_norm, norm
from delium.analysis.marketplaces import language_differs, unit_system_differs
from delium.analysis.models import (
    Confidence,
    CrossMarketComponent,
    CrossMarketConfidence,
    CrossMarketReport,
    CrossMarketVerdict,
    LocalizationFlag,
    MarketGap,
    Marketplace,
    MarketplaceProduct,
    MatchConfidence,
    ProductMatch,
    SourceMarketEvidence,
    SourceMarketInput,
    SourceMaturity,
    Subscore,
    TargetMarketEvidence,
    TargetMarketInput,
    TargetPresence,
    Transferability,
    TransferabilityFactor,
    TransferabilityInput,
    TransferabilityLevel,
)
from delium.config.models import CrossMarketConfig, DeliumConfig

# ---------------------------------------------------------------------------
# Ranking helpers (pure)
# ---------------------------------------------------------------------------
_CONF_RANK = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}
_CONF_BY_RANK = {2: Confidence.HIGH, 1: Confidence.MEDIUM, 0: Confidence.LOW}

_MATCH_RANK = {
    MatchConfidence.EXACT: 4,
    MatchConfidence.STRONG: 3,
    MatchConfidence.PROBABLE: 2,
    MatchConfidence.WEAK: 1,
    MatchConfidence.UNMATCHED: 0,
}
_MATCH_BY_RANK = {v: k for k, v in _MATCH_RANK.items()}
# Match confidence → the highest overall confidence it can support.
_MATCH_TO_CONF = {
    MatchConfidence.EXACT: Confidence.HIGH,
    MatchConfidence.STRONG: Confidence.HIGH,
    MatchConfidence.PROBABLE: Confidence.MEDIUM,
    MatchConfidence.WEAK: Confidence.LOW,
    MatchConfidence.UNMATCHED: Confidence.LOW,
}

_STOPWORDS = frozenset(
    {"the", "a", "an", "for", "and", "or", "of", "with", "to", "in", "on", "by", "pack", "set"}
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _confidence_from_count(present: int, high_at: int, medium_at: int) -> Confidence:
    if present >= high_at:
        return Confidence.HIGH
    if present >= medium_at:
        return Confidence.MEDIUM
    return Confidence.LOW


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2 and t not in _STOPWORDS
    )


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _within(a: float, b: float, tol: float) -> bool:
    hi = max(abs(a), abs(b))
    if hi == 0:
        return True
    return abs(a - b) <= hi * tol


# ---------------------------------------------------------------------------
# Product matching (docs/cross-market.md §3) — conservative identity resolution
# ---------------------------------------------------------------------------
def _norm_gtin(g: str | None) -> str | None:
    if g is None:
        return None
    digits = re.sub(r"\D", "", g)
    return digits.lstrip("0") or "0" if digits else None


def match_products(
    source: MarketplaceProduct, target: MarketplaceProduct, config: DeliumConfig
) -> ProductMatch:
    """Resolve whether two marketplace listings are the same underlying product.
    GTIN agreement is the only exact key; otherwise a weighted fuzzy score over
    title/brand/dims/category, capped when physical signals conflict."""
    cfg = config.cross_market
    signals: list[str] = []
    conflicts: list[str] = []

    sg, tg = _norm_gtin(source.gtin), _norm_gtin(target.gtin)
    if sg is not None and tg is not None:
        if sg == tg:
            return ProductMatch(
                source,
                target,
                MatchConfidence.EXACT,
                1.0,
                ("gtin",),
                (),
                "GTIN/UPC/EAN match — exact identity",
            )
        conflicts.append("gtin")  # different GTIN → almost certainly different products

    parts: list[tuple[float, float]] = []  # (weight, similarity)

    if source.title and target.title:
        sim = _jaccard(source.title, target.title)
        parts.append((cfg.match_title_weight, sim))
        if sim >= 0.5:
            signals.append("title")

    if source.brand and target.brand:
        if source.brand.strip().lower() == target.brand.strip().lower():
            parts.append((cfg.match_brand_weight, 1.0))
            signals.append("brand")
        elif source.generic or target.generic:
            parts.append((cfg.match_brand_weight, 0.5))  # generic/private-label: tolerated
            signals.append("brand~generic")
        else:
            parts.append((cfg.match_brand_weight, 0.0))
            conflicts.append("brand")

    if source.dims is not None and target.dims is not None:
        s, t = source.dims.sorted_desc(), target.dims.sorted_desc()
        agree = all(_within(a, b, cfg.match_dims_tolerance) for a, b in zip(s, t, strict=True))
        parts.append((cfg.match_dims_weight, 1.0 if agree else 0.0))
        (signals if agree else conflicts).append("dims")

    if source.category_path and target.category_path:
        sim = _jaccard(source.category_path, target.category_path)
        parts.append((cfg.match_category_weight, sim))
        if sim >= 0.5:
            signals.append("category")

    if (
        source.weight_g is not None
        and target.weight_g is not None
        and not _within(source.weight_g, target.weight_g, cfg.match_weight_tolerance)
    ):
        conflicts.append("weight")

    total_w = sum(w for w, _ in parts)
    score = sum(w * s for w, s in parts) / total_w if total_w > 0 else 0.0

    if score >= cfg.match_strong_min:
        confidence = MatchConfidence.STRONG  # fuzzy tops out at STRONG; EXACT needs GTIN
    elif score >= cfg.match_probable_min:
        confidence = MatchConfidence.PROBABLE
    elif score >= cfg.match_weak_min:
        confidence = MatchConfidence.WEAK
    else:
        confidence = MatchConfidence.UNMATCHED

    # Conflicting physical signals cap confidence — a superficial title match with
    # different dimensions/weight is not the same product.
    if "dims" in conflicts or "weight" in conflicts:
        confidence = _MATCH_BY_RANK[
            min(_MATCH_RANK[confidence], _MATCH_RANK[MatchConfidence.PROBABLE])
        ]
    if "gtin" in conflicts:
        confidence = _MATCH_BY_RANK[min(_MATCH_RANK[confidence], _MATCH_RANK[MatchConfidence.WEAK])]

    detail = f"fuzzy identity score {score:.2f}; signals={signals or ['none']}"
    if conflicts:
        detail += f"; conflicts={conflicts}"
    return ProductMatch(source, target, confidence, score, tuple(signals), tuple(conflicts), detail)


# ---------------------------------------------------------------------------
# Source-market qualification (docs/cross-market.md §4)
# ---------------------------------------------------------------------------
def assess_source(
    marketplace: Marketplace, data: SourceMarketInput, config: DeliumConfig
) -> SourceMarketEvidence:
    """Classify how convincingly the product has succeeded in the source market.
    'Success' is durable demand/velocity/growth/history — not merely 'has sales'."""
    cfg = config.cross_market
    components: list[Subscore] = []
    reasons: list[str] = []

    def add(name: str, value: float | None, weight: float, detail: str) -> None:
        components.append(Subscore(name, value, weight, detail))

    if data.monthly_units is not None:
        v = log_norm(data.monthly_units, cfg.src_velocity_lo, cfg.src_velocity_hi)
        add("velocity", v, cfg.w_src_velocity, f"{data.monthly_units} units/mo → {v:.0f}")
    else:
        add("velocity", None, cfg.w_src_velocity, "no velocity estimate")

    if data.keyword_volume is not None:
        v = log_norm(data.keyword_volume, cfg.src_keyword_lo, cfg.src_keyword_hi)
        add("keyword_demand", v, cfg.w_src_keyword, f"{data.keyword_volume} vol → {v:.0f}")
    else:
        add("keyword_demand", None, cfg.w_src_keyword, "no keyword volume")

    if data.keyword_growth is not None:
        v = norm(data.keyword_growth, cfg.src_growth_lo, cfg.src_growth_hi)
        add("growth", v, cfg.w_src_growth, f"YoY {data.keyword_growth:.0%} → {v:.0f}")
    else:
        add("growth", None, cfg.w_src_growth, "no growth data")

    if data.history_months is not None:
        v = norm(data.history_months, cfg.src_history_lo, cfg.src_history_hi)
        add("history", v, cfg.w_src_history, f"{data.history_months}mo history → {v:.0f}")
    else:
        add("history", None, cfg.w_src_history, "no history length")

    if data.review_count is not None:
        v = log_norm(data.review_count, cfg.src_reviews_lo, cfg.src_reviews_hi)
        add("established", v, cfg.w_src_reviews, f"{data.review_count:.0f} reviews → {v:.0f}")
    else:
        add("established", None, cfg.w_src_reviews, "no review count")

    present = sum(1 for c in components if c.available)
    avail_w = sum(c.weight for c in components if c.available)
    score = (
        sum(c.weight * (c.value or 0.0) for c in components if c.available) / avail_w
        if avail_w > 0
        else 0.0
    )
    score = clamp(score)

    if present < cfg.src_min_signals:
        maturity = SourceMaturity.INSUFFICIENT
        confidence = Confidence.LOW
        reasons.append(f"only {present} source signal(s) — insufficient to qualify success")
    else:
        if score >= cfg.src_strong_max:
            maturity = SourceMaturity.EXCEPTIONAL
        elif score >= cfg.src_validated_max:
            maturity = SourceMaturity.STRONG
        elif score >= cfg.src_emerging_max:
            maturity = SourceMaturity.VALIDATED
        else:
            maturity = SourceMaturity.EMERGING
        confidence = _confidence_from_count(present, high_at=4, medium_at=3)
        reasons.append(f"source success {score:.0f}/100 from {present} signals → {maturity.value}")
        if data.opportunity_score is not None:
            reasons.append(f"source opportunity score {data.opportunity_score:.0f} (context)")

    return SourceMarketEvidence(
        marketplace=marketplace,
        source_success_score=score,
        maturity=maturity,
        confidence=confidence,
        components=tuple(components),
        reasons=tuple(reasons),
        signals_present=present,
    )


# ---------------------------------------------------------------------------
# Target-market qualification (docs/cross-market.md §5-§7)
# ---------------------------------------------------------------------------
def _target_demand(
    data: TargetMarketInput, cfg: CrossMarketConfig
) -> tuple[float, int, list[Subscore]]:
    comps: list[Subscore] = []
    present = 0
    if data.keyword_volume is not None:
        present += 1
        v = log_norm(data.keyword_volume, cfg.tgt_keyword_lo, cfg.tgt_keyword_hi)
        comps.append(
            Subscore("kw_volume", v, cfg.w_tgt_keyword, f"{data.keyword_volume} vol → {v:.0f}")
        )
    else:
        comps.append(Subscore("kw_volume", None, cfg.w_tgt_keyword, "no target keyword volume"))
    if data.keyword_growth is not None:
        present += 1
        v = norm(data.keyword_growth, cfg.tgt_growth_lo, cfg.tgt_growth_hi)
        comps.append(
            Subscore("kw_growth", v, cfg.w_tgt_growth, f"YoY {data.keyword_growth:.0%} → {v:.0f}")
        )
    else:
        comps.append(Subscore("kw_growth", None, cfg.w_tgt_growth, "no target growth"))
    if data.serp_presence is not None:
        present += 1
        v = 100.0 if data.serp_presence else 0.0
        comps.append(Subscore("serp", v, cfg.w_tgt_serp, f"SERP presence={data.serp_presence}"))
    else:
        comps.append(Subscore("serp", None, cfg.w_tgt_serp, "no SERP lookup"))

    avail_w = sum(c.weight for c in comps if c.available)
    score = (
        sum(c.weight * (c.value or 0.0) for c in comps if c.available) / avail_w
        if avail_w > 0
        else 0.0
    )
    return clamp(score), present, comps


def _target_competition_weakness(
    data: TargetMarketInput, cfg: CrossMarketConfig
) -> tuple[float, int, str]:
    """0-100, higher = weaker/easier target. `listings_found == 0` is inferred
    (empty market), flagged as low-confidence — never treated as proven-weak."""
    if data.listings_found == 0:
        return (
            cfg.empty_market_weakness,
            0,
            "no listings — competition weakness inferred (low confidence)",
        )
    parts: list[tuple[float, float]] = []
    present = 0
    if data.median_reviews is not None:
        present += 1
        parts.append(
            (
                cfg.w_cw_reviews,
                100.0 - log_norm(data.median_reviews, cfg.tgt_reviews_lo, cfg.tgt_reviews_hi),
            )
        )
    if data.avg_listing_quality is not None:
        present += 1
        parts.append((cfg.w_cw_listing, clamp(100.0 - data.avg_listing_quality)))
    if data.beatable_slots is not None:
        present += 1
        parts.append((cfg.w_cw_beatable, clamp(min(data.beatable_slots, 4) * 25.0)))
    if data.brand_hhi is not None:
        present += 1
        parts.append((cfg.w_cw_hhi, clamp(100.0 - data.brand_hhi * 100.0)))
    if not parts:
        return 50.0, 0, "no competition data — neutral"
    total_w = sum(w for w, _ in parts)
    score = sum(w * v for w, v in parts) / total_w
    return clamp(score), present, f"competition weakness {score:.0f}/100 from {present} signals"


def _target_maturity(data: TargetMarketInput, cfg: CrossMarketConfig) -> float:
    vals: list[float] = []
    if data.listings_found is not None:
        vals.append(norm(data.listings_found, 0, cfg.mature_min_listings))
    if data.median_reviews is not None:
        vals.append(log_norm(data.median_reviews, cfg.tgt_reviews_lo, cfg.tgt_reviews_hi))
    if not vals:
        return 0.0
    return clamp(sum(vals) / len(vals))


def assess_target(
    marketplace: Marketplace, data: TargetMarketInput, config: DeliumConfig
) -> TargetMarketEvidence:
    cfg = config.cross_market
    demand_score, demand_signals, demand_comps = _target_demand(data, cfg)
    weakness, comp_signals, comp_detail = _target_competition_weakness(data, cfg)
    maturity_score = _target_maturity(data, cfg)

    # Demand is credible only with real volume evidence — a lone SERP boolean or
    # growth figure is not enough (absence of evidence ≠ opportunity).
    demand_credible = (
        demand_score >= cfg.tgt_demand_credible_min
        and data.keyword_volume is not None
        and data.keyword_volume > 0
    )
    demand_high = demand_score >= cfg.demand_high

    reviews = data.median_reviews or 0.0
    if data.listings_found is None:
        presence = TargetPresence.UNKNOWN
    elif data.listings_found == 0:
        presence = TargetPresence.NOT_PRESENT
    elif reviews >= cfg.saturated_reviews:
        presence = TargetPresence.SATURATED if demand_high else TargetPresence.MATURE
    elif data.listings_found >= cfg.mature_min_listings and reviews >= cfg.mature_reviews:
        presence = TargetPresence.MATURE
    elif weakness >= cfg.strong_competition_gap_min and demand_credible:
        presence = TargetPresence.UNDERPENETRATED
    else:
        presence = TargetPresence.EARLY

    components = tuple(demand_comps)
    reasons = [
        f"presence={presence.value}; demand {demand_score:.0f}/100 "
        f"({'credible' if demand_credible else 'not credible'}); {comp_detail}"
    ]

    # Confidence: needs both demand and competition evidence to be trustworthy.
    if presence is TargetPresence.UNKNOWN:
        confidence = Confidence.LOW
    else:
        signal_conf = _confidence_from_count(demand_signals, high_at=2, medium_at=1)
        if comp_signals == 0 and data.listings_found != 0:
            signal_conf = Confidence.LOW
        confidence = signal_conf

    return TargetMarketEvidence(
        marketplace=marketplace,
        presence=presence,
        target_demand_score=demand_score,
        demand_credible=demand_credible,
        competition_weakness_score=weakness,
        target_maturity_score=maturity_score,
        confidence=confidence,
        components=components,
        reasons=tuple(reasons),
        signals_present=demand_signals + comp_signals,
    )


# ---------------------------------------------------------------------------
# Market gap (docs/cross-market.md §6)
# ---------------------------------------------------------------------------
def compute_market_gap(
    source: SourceMarketEvidence,
    target: TargetMarketEvidence,
    source_input: SourceMarketInput,
    target_input: TargetMarketInput,
    config: DeliumConfig,
) -> MarketGap:
    maturity_gap = clamp(source.source_success_score - target.target_maturity_score)
    source_weakness = (
        source_input.competition_score if source_input.competition_score is not None else 50.0
    )
    competition_gap = clamp(50.0 + (target.competition_weakness_score - source_weakness) * 0.5)

    demand_gap: float | None = None
    if source_input.keyword_volume and target_input.keyword_volume is not None:
        demand_gap = clamp(target_input.keyword_volume / source_input.keyword_volume * 100.0)

    detail = (
        f"maturity gap {maturity_gap:.0f} (source {source.source_success_score:.0f} − "
        f"target {target.target_maturity_score:.0f}); competition gap {competition_gap:.0f}"
    )
    return MarketGap(maturity_gap, competition_gap, demand_gap, detail)


# ---------------------------------------------------------------------------
# Transferability (docs/cross-market.md §8-§9)
# ---------------------------------------------------------------------------
def _factor(
    name: str, ok: bool | None, evidence_ok: str, evidence_bad: str
) -> TransferabilityFactor:
    if ok is None:
        return TransferabilityFactor(name, TransferabilityLevel.UNCERTAIN, f"{name}: unknown")
    if ok:
        return TransferabilityFactor(name, TransferabilityLevel.FAVORABLE, evidence_ok)
    return TransferabilityFactor(name, TransferabilityLevel.UNFAVORABLE, evidence_bad)


def assess_transferability(
    source: Marketplace,
    target: Marketplace,
    data: TransferabilityInput,
    config: DeliumConfig,
) -> Transferability:
    cfg = config.cross_market
    factors: list[TransferabilityFactor] = [
        _factor(
            "category", data.category_compatible, "category compatible", "category incompatible"
        ),
        _factor(
            "logistics",
            None if data.oversized is None else not data.oversized,
            "standard logistics",
            "oversized/heavy — freight risk",
        ),
        _factor(
            "price_positioning",
            data.price_positioning_ok,
            "price fits target band",
            "price mispositioned",
        ),
        _factor(
            "compliance",
            None if data.compliance_risk is None else not data.compliance_risk,
            "no added compliance surface",
            "target compliance/regulatory surface",
        ),
    ]
    if data.seasonality_concentration is None:
        factors.append(
            TransferabilityFactor(
                "seasonality", TransferabilityLevel.UNCERTAIN, "seasonality unknown"
            )
        )
    elif data.seasonality_concentration >= cfg.seasonality_uncertain_threshold:
        factors.append(
            TransferabilityFactor(
                "seasonality",
                TransferabilityLevel.UNCERTAIN,
                f"seasonal concentration {data.seasonality_concentration:.0%}",
            )
        )
    else:
        factors.append(
            TransferabilityFactor(
                "seasonality", TransferabilityLevel.FAVORABLE, "year-round demand"
            )
        )

    score = 100.0
    for f in factors:
        if f.level is TransferabilityLevel.UNCERTAIN:
            score -= cfg.transfer_uncertain_penalty
        elif f.level is TransferabilityLevel.UNFAVORABLE:
            score -= cfg.transfer_unfavorable_penalty
    score = clamp(score)

    has_unfavorable = any(f.level is TransferabilityLevel.UNFAVORABLE for f in factors)
    has_uncertain = any(f.level is TransferabilityLevel.UNCERTAIN for f in factors)
    if has_unfavorable or score < cfg.transfer_uncertain_min:
        level = TransferabilityLevel.UNFAVORABLE
    elif has_uncertain or score < cfg.transfer_favorable_min:
        level = TransferabilityLevel.UNCERTAIN
    else:
        level = TransferabilityLevel.FAVORABLE

    localization: list[LocalizationFlag] = []
    if unit_system_differs(source, target):
        localization.append(LocalizationFlag("units", "measurement/unit conventions differ"))
    if language_differs(source, target):  # pragma: no cover - all registered marketplaces are en
        localization.append(LocalizationFlag("language", "listing language differs"))
    if data.electrical_or_plug_dependent:
        localization.append(LocalizationFlag("electrical", "plug/voltage standards differ"))
    if data.keyword_localization_needed:
        localization.append(
            LocalizationFlag("keywords", "search terminology differs — re-map keywords")
        )

    detail = f"transferability {level.value} ({score:.0f}/100)"
    return Transferability(
        level=level,
        score=score,
        factors=tuple(factors),
        localization_flags=tuple(localization),
        surfaced_risk_flags=data.surfaced_risk_flags,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Assembly, confidence, verdict (docs/cross-market.md §10-§12)
# ---------------------------------------------------------------------------
def _overall_confidence(
    match: ProductMatch, source: SourceMarketEvidence, target: TargetMarketEvidence
) -> CrossMarketConfidence:
    match_conf = _MATCH_TO_CONF[match.confidence]
    worst = min(
        _CONF_RANK[match_conf], _CONF_RANK[source.confidence], _CONF_RANK[target.confidence]
    )
    notes: list[str] = []
    if match.confidence in (MatchConfidence.WEAK, MatchConfidence.UNMATCHED):
        notes.append("identity match is weak — treat comparison cautiously")
    if source.confidence is Confidence.LOW:
        notes.append("source evidence is thin")
    if target.confidence is Confidence.LOW:
        notes.append("target evidence is thin")
    return CrossMarketConfidence(
        level=_CONF_BY_RANK[worst],
        match_confidence=match.confidence,
        source_confidence=source.confidence,
        target_confidence=target.confidence,
        notes=tuple(notes),
    )


def _decide_verdict(
    *,
    score: float,
    match: ProductMatch,
    source: SourceMarketEvidence,
    target: TargetMarketEvidence,
    gap: MarketGap,
    transfer: Transferability,
    confidence: CrossMarketConfidence,
    cfg: CrossMarketConfig,
) -> tuple[CrossMarketVerdict, list[str]]:
    reasons: list[str] = []
    if not match.matched:
        return CrossMarketVerdict.INSUFFICIENT_DATA, ["no credible product identity match"]
    if source.maturity is SourceMaturity.INSUFFICIENT:
        return CrossMarketVerdict.INSUFFICIENT_DATA, ["source-market success not established"]
    if target.presence is TargetPresence.UNKNOWN and not target.demand_credible:
        return CrossMarketVerdict.INSUFFICIENT_DATA, [
            "target market not assessed — no evidence either way"
        ]
    if target.presence is TargetPresence.NOT_PRESENT and not target.demand_credible:
        # Absence of listings AND no demand signal = absence of evidence, not opportunity.
        return CrossMarketVerdict.INSUFFICIENT_DATA, [
            "no target listings and no credible target demand — "
            "absence of evidence, not opportunity"
        ]
    if transfer.level is TransferabilityLevel.UNFAVORABLE:
        return CrossMarketVerdict.WEAK_TRANSFER, [
            "transferability is unfavorable — source success unlikely to carry over"
        ]
    if target.presence in (TargetPresence.MATURE, TargetPresence.SATURATED):
        return CrossMarketVerdict.MATURE_MARKET, [
            f"target already {target.presence.value} — established incumbents, limited gap"
        ]
    if (
        score >= cfg.strong_opportunity_min
        and target.demand_credible
        and gap.competition_gap >= cfg.strong_competition_gap_min
        and confidence.level is not Confidence.LOW
    ):
        reasons.append(
            f"proven source ({source.maturity.value}), credible target demand, "
            f"weak competition (gap {gap.competition_gap:.0f}) — strong signal to validate"
        )
        return CrossMarketVerdict.STRONG_OPPORTUNITY, reasons
    if score >= cfg.validate_min or (
        source.maturity in (SourceMaturity.STRONG, SourceMaturity.EXCEPTIONAL)
        and not target.demand_credible
    ):
        if not target.demand_credible:
            reasons.append("source-market proven, but target demand is not yet validated")
        else:
            reasons.append(
                "promising, but not yet a strong signal — worth money-limited validation"
            )
        return CrossMarketVerdict.OPPORTUNITY_TO_VALIDATE, reasons
    return CrossMarketVerdict.WEAK_TRANSFER, ["signal too weak to justify validation spend"]


def analyze_cross_market(
    *,
    source_product: MarketplaceProduct,
    target_product: MarketplaceProduct,
    source_input: SourceMarketInput,
    target_input: TargetMarketInput,
    transfer_input: TransferabilityInput,
    config: DeliumConfig,
) -> CrossMarketReport:
    """Assess one source → one target cross-market transfer opportunity.

    Directional by construction: swapping source/target (with their own evidence)
    produces an independent assessment. Pure and deterministic.
    """
    cfg = config.cross_market
    src_mp = source_product.marketplace
    tgt_mp = target_product.marketplace

    match = match_products(source_product, target_product, config)
    source = assess_source(src_mp, source_input, config)
    target = assess_target(tgt_mp, target_input, config)
    gap = compute_market_gap(source, target, source_input, target_input, config)
    transfer = assess_transferability(src_mp, tgt_mp, transfer_input, config)

    weights = (
        ("source_success", cfg.w_source_success),
        ("target_demand", cfg.w_target_demand),
        ("competition_gap", cfg.w_competition_gap),
        ("maturity_gap", cfg.w_maturity_gap),
        ("transferability", cfg.w_transferability),
    )
    total_w = sum(w for _, w in weights)
    normalized = {
        "source_success": source.source_success_score,
        "target_demand": target.target_demand_score,
        "competition_gap": gap.competition_gap,
        "maturity_gap": gap.maturity_gap,
        "transferability": transfer.score,
    }
    raws = {
        "source_success": f"{source.maturity.value} ({source.source_success_score:.0f})",
        "target_demand": f"{target.target_demand_score:.0f}/100"
        + ("" if target.demand_credible else " (not credible)"),
        "competition_gap": f"target weakness vs source ({gap.competition_gap:.0f})",
        "maturity_gap": f"{gap.maturity_gap:.0f}",
        "transferability": transfer.level.value,
    }
    comp_conf = {
        "source_success": source.confidence,
        "target_demand": target.confidence,
        "competition_gap": target.confidence,
        "maturity_gap": target.confidence,
        "transferability": Confidence.HIGH,
    }
    components: list[CrossMarketComponent] = []
    base_score = 0.0
    for name, weight in weights:
        value = normalized[name]
        contribution = value * weight / total_w if total_w > 0 else 0.0
        base_score += contribution
        components.append(
            CrossMarketComponent(
                name=name,
                raw=raws[name],
                normalized=value,
                weight=weight,
                weighted_contribution=contribution,
                evidence=raws[name],
                confidence=comp_conf[name],
            )
        )
    base_score = clamp(base_score)

    confidence = _overall_confidence(match, source, target)

    # Surfaced risk penalties (never recomputed here).
    risk_penalty = 0.0
    if transfer_input.compliance_risk:
        risk_penalty += cfg.compliance_risk_penalty
    if transfer.level is TransferabilityLevel.UNFAVORABLE:
        risk_penalty += cfg.unfavorable_transfer_penalty

    score = clamp(base_score - risk_penalty)
    if confidence.level is Confidence.LOW:
        score = min(score, cfg.low_confidence_score_cap)

    verdict, verdict_reasons = _decide_verdict(
        score=score,
        match=match,
        source=source,
        target=target,
        gap=gap,
        transfer=transfer,
        confidence=confidence,
        cfg=cfg,
    )

    summary: list[str] = [f"{src_mp.value} → {tgt_mp.value}: {verdict.value}"]
    summary.extend(verdict_reasons)
    summary.extend(source.reasons)
    summary.extend(target.reasons)
    if transfer.localization_flags:
        summary.append("localization: " + ", ".join(f.kind for f in transfer.localization_flags))
    if transfer.surfaced_risk_flags:
        summary.append("risk flags: " + ", ".join(transfer.surfaced_risk_flags))

    return CrossMarketReport(
        source_marketplace=src_mp,
        target_marketplace=tgt_mp,
        verdict=verdict,
        score=score,
        base_score=base_score,
        match=match,
        source_evidence=source,
        target_evidence=target,
        market_gap=gap,
        transferability=transfer,
        components=tuple(components),
        confidence=confidence,
        risk_penalty=risk_penalty,
        summary=tuple(summary),
        weights_snapshot=weights,
    )


def analyze_cross_markets(
    *,
    source_product: MarketplaceProduct,
    source_input: SourceMarketInput,
    targets: tuple[tuple[MarketplaceProduct, TargetMarketInput, TransferabilityInput], ...],
    config: DeliumConfig,
) -> tuple[CrossMarketReport, ...]:
    """Run one source against many targets, producing an independent report per
    target (US → CA / UK / AU / IN). Ordering follows the input; callers rank by
    `score`, `market_gap`, `confidence`, etc. Same logic, no duplication."""
    return tuple(
        analyze_cross_market(
            source_product=source_product,
            target_product=tp,
            source_input=source_input,
            target_input=ti,
            transfer_input=xi,
            config=config,
        )
        for tp, ti, xi in targets
    )
