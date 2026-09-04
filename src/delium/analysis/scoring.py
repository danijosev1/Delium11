"""Deterministic opportunity-scoring engine (docs/scoring-model.md §2-§10).

Pure assembly of the five pillar reports into the composite score, hard kills,
gates, and Buy/Test/Avoid verdict. No judgment lives here — the LLM Strategist
is a separate, downstream layer (G5), never called from this module.

Contract (enforced by tests):
- No LLM, provider, database, filesystem, network, clock, or random access.
- All thresholds/weights come from the passed-in `DeliumConfig`; none are
  hardcoded here. Every score is frozen with a `ConfigSnapshot` so it stays
  reproducible after config drift (scoring-model §11.3).
- Same inputs + same config = identical output, forever.

The four-stage funnel is run in order (scoring-model §1): hard kills →
data sufficiency → five pillar scores → gates + verdict. A hard kill or a
failed hard gate is never averaged away by good pillars, and missing data is
never turned into an optimistic assumption.
"""

from __future__ import annotations

from delium.analysis.curves import clamp, norm
from delium.analysis.models import (
    Confidence,
    ConfigSnapshot,
    GateResult,
    KillResult,
    PillarScore,
    ProfitResult,
    ScenarioSet,
    ScoreConfidence,
    ScoredOpportunity,
    ScoringInput,
    StrategistConcurrence,
    Subscore,
    Verdict,
)
from delium.config.models import DeliumConfig

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
_CONF_RANK = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}


def _cents_to_usd(cents: int | None) -> float | None:
    return None if cents is None else cents / 100.0


def _within_band(actual: float, threshold: float, band: float) -> bool:
    """True when `actual` is within `band` (fractional) of `threshold` — a
    borderline case that demotes a kill to Test rather than killing (§2)."""
    if threshold == 0:
        return abs(actual) <= band
    return abs(actual - threshold) <= abs(threshold) * band


# ---------------------------------------------------------------------------
# Stage 0 — Hard kills (scoring-model §2)
# ---------------------------------------------------------------------------
def _kill(
    rule_id: str,
    name: str,
    category: str,
    *,
    triggered: bool,
    demoted: bool = False,
    actual: str | None = None,
    threshold: str | None = None,
    evidence: str = "",
    reason: str = "",
    assessed: bool = True,
) -> KillResult:
    return KillResult(
        rule_id=rule_id,
        name=name,
        triggered=triggered,
        demoted=demoted,
        category=category,
        actual=actual,
        threshold=threshold,
        evidence=evidence,
        reason=reason,
        assessed=assessed,
    )


def _unassessed_kill(rule_id: str, name: str, category: str, reason: str) -> KillResult:
    return _kill(
        rule_id,
        name,
        category,
        triggered=False,
        assessed=False,
        reason=reason,
        evidence="input unavailable",
    )


def _kill_price(inp: ScoringInput, cfg: DeliumConfig) -> tuple[KillResult, KillResult]:
    """K1 (median price < min) and K2 (median price > max)."""
    kr = cfg.kill_rules
    band = kr.borderline_band
    price = _cents_to_usd(inp.market_median_price_cents)
    if price is None:
        return (
            _unassessed_kill("K1", "Price floor", "price", "market median price unknown"),
            _unassessed_kill("K2", "Price ceiling", "price", "market median price unknown"),
        )
    low_trig = price < kr.price_min
    low = _kill(
        "K1",
        "Price floor",
        "price",
        triggered=low_trig,
        demoted=low_trig and _within_band(price, kr.price_min, band),
        actual=f"${price:.2f}",
        threshold=f"${kr.price_min:.0f}",
        evidence=f"market median price ${price:.2f}",
        reason="No room for fees + PPC below the price floor (race-to-bottom).",
    )
    high_trig = price > kr.price_max
    high = _kill(
        "K2",
        "Price ceiling",
        "price",
        triggered=high_trig,
        demoted=high_trig and _within_band(price, kr.price_max, band),
        actual=f"${price:.2f}",
        threshold=f"${kr.price_max:.0f}",
        evidence=f"market median price ${price:.2f}",
        reason="Capital per unit too high for a $5-20k launch (inventory depth suffers).",
    )
    return low, high


def _kill_oversized(inp: ScoringInput) -> KillResult:
    """K3 oversized / heavy (FBA tier above large-standard)."""
    if inp.oversized is None:
        return _unassessed_kill("K3", "Oversized logistics", "logistics", "size tier unknown")
    return _kill(
        "K3",
        "Oversized logistics",
        "logistics",
        triggered=inp.oversized,
        actual="oversized" if inp.oversized else "standard",
        threshold="≤ large standard",
        evidence="FBA size tier above large-standard" if inp.oversized else "within large-standard",
        reason="Fees + freight eat the model; storage risk.",
    )


def _kill_amazon(inp: ScoringInput) -> KillResult:
    """K4 Amazon / AmazonBasics private brand in top 5 organic."""
    if inp.amazon_in_top5 is None:
        return _unassessed_kill("K4", "Amazon present", "brand", "top-5 brands unknown")
    return _kill(
        "K4",
        "Amazon present",
        "brand",
        triggered=inp.amazon_in_top5,
        actual="present" if inp.amazon_in_top5 else "absent",
        threshold="not in top 5",
        evidence="Amazon/AmazonBasics holds a top-5 organic slot"
        if inp.amazon_in_top5
        else "no Amazon private brand in top 5",
        reason="You don't out-margin the referee.",
    )


def _kill_brand_slots(inp: ScoringInput, cfg: DeliumConfig) -> KillResult:
    """K5 single brand holds ≥ max_brand_slots of top-10 slots."""
    comp = inp.competition
    kr = cfg.kill_rules
    if comp is None or comp.brand_concentration.top_brand_slot_share is None:
        return _unassessed_kill("K5", "Brand dominance", "brand", "brand slot share unknown")
    analyzed = comp.confidence.competitors_analyzed or inp.top_n
    share = comp.brand_concentration.top_brand_slot_share
    slots = round(share * analyzed)
    trig = slots >= kr.max_brand_slots
    return _kill(
        "K5",
        "Brand dominance",
        "brand",
        triggered=trig,
        demoted=trig and slots == kr.max_brand_slots and share < 0.5,
        actual=f"{slots} slots ({share:.0%})",
        threshold=f"< {kr.max_brand_slots} slots",
        evidence=f"top brand {comp.brand_concentration.top_brand!r} holds {slots} of "
        f"{analyzed} slots",
        reason="Brand-dominated; PPC will be a knife fight with someone richer.",
    )


def _kill_moat(inp: ScoringInput, cfg: DeliumConfig) -> KillResult:
    """K6 median review count of top-10 > max_median_reviews."""
    comp = inp.competition
    kr = cfg.kill_rules
    if comp is None or comp.review_moat.median_reviews is None:
        return _unassessed_kill("K6", "Review moat", "moat", "median review count unknown")
    median = comp.review_moat.median_reviews
    trig = median > kr.max_median_reviews
    return _kill(
        "K6",
        "Review moat",
        "moat",
        triggered=trig,
        demoted=trig and _within_band(median, kr.max_median_reviews, kr.borderline_band),
        actual=f"{median:.0f} reviews",
        threshold=f"≤ {kr.max_median_reviews}",
        evidence=f"median top-10 review count {median:.0f}",
        reason="Moat too deep to climb on $5-20k.",
    )


def _kill_no_wedge(inp: ScoringInput, cfg: DeliumConfig) -> KillResult:
    """K7 every top-10 already excellent (avg listing quality ≥ threshold) AND
    complaint rate < threshold → nothing to improve, no differentiation wedge."""
    comp = inp.competition
    kr = cfg.kill_rules
    avg_quality = comp.listing_quality_advantage.avg_competitor_quality if comp else None
    complaint = inp.market_complaint_rate
    if avg_quality is None or complaint is None:
        return _unassessed_kill(
            "K7", "No wedge", "differentiation", "listing quality or complaint rate unknown"
        )
    excellent = avg_quality >= kr.excellent_listing_quality
    low_complaints = complaint < kr.excellent_complaint_rate
    trig = excellent and low_complaints
    return _kill(
        "K7",
        "No wedge",
        "differentiation",
        triggered=trig,
        actual=f"avg quality {avg_quality:.0f}/100, complaints {complaint:.0%}",
        threshold=f"quality < {kr.excellent_listing_quality:.0f} or complaints ≥ "
        f"{kr.excellent_complaint_rate:.0%}",
        evidence=f"top-10 avg listing quality {avg_quality:.0f}/100 and complaint rate "
        f"{complaint:.0%}",
        reason="Nothing to improve = no wedge for a differentiator.",
    )


def _kill_restricted(inp: ScoringInput) -> KillResult:
    """K8 gated / restricted category."""
    if inp.restricted_category is None:
        return _unassessed_kill("K8", "Restricted category", "compliance", "gating status unknown")
    return _kill(
        "K8",
        "Restricted category",
        "compliance",
        triggered=inp.restricted_category,
        actual="restricted" if inp.restricted_category else "open",
        threshold="not gated/restricted",
        evidence="category is gated/restricted (topicals, supplements, medical, flagged batteries)"
        if inp.restricted_category
        else "category is open",
        reason="Compliance overhead excluded in config `avoid` list.",
    )


def _kill_ip(inp: ScoringInput) -> KillResult:
    """K9 obvious IP signature (character/brand-likeness, design-patent lookalike)."""
    if inp.ip_signature is None:
        return _unassessed_kill("K9", "IP signature", "ip", "IP signature unassessed")
    return _kill(
        "K9",
        "IP signature",
        "ip",
        triggered=inp.ip_signature,
        actual="signature present" if inp.ip_signature else "none",
        threshold="no IP signature",
        evidence="character/brand-likeness or design-patent-lookalike signature"
        if inp.ip_signature
        else "no obvious IP signature",
        reason="Patent risk is the one that zeroes accounts.",
    )


def _kill_fad(inp: ScoringInput, cfg: DeliumConfig) -> KillResult:
    """K10 trend spike: volume > ratio × 12-mo median AND < min history months."""
    kr = cfg.kill_rules
    vol = inp.fad_search_volume
    median = inp.fad_volume_12mo_median
    months = inp.volume_history_months
    if vol is None or median is None or months is None:
        return _unassessed_kill("K10", "Fad spike", "trend", "trend history unavailable")
    spike = median > 0 and vol > kr.fad_spike_ratio * median
    thin = months < kr.fad_min_history_months
    trig = spike and thin
    ratio = (vol / median) if median > 0 else 0.0
    return _kill(
        "K10",
        "Fad spike",
        "trend",
        triggered=trig,
        demoted=trig and _within_band(ratio, kr.fad_spike_ratio, kr.borderline_band),
        actual=f"{ratio:.1f}× median, {months}mo history",
        threshold=f"≤ {kr.fad_spike_ratio:.0f}× or ≥ {kr.fad_min_history_months}mo",
        evidence=f"search volume {vol} is {ratio:.1f}× the 12-mo median with only "
        f"{months}mo of history",
        reason="Fad. By the time inventory lands, the wave broke.",
    )


def _kill_capital(inp: ScoringInput, cfg: DeliumConfig) -> KillResult:
    """K11 estimated launch capital outside the $2k-$20k band (conservative)."""
    kr = cfg.kill_rules
    if inp.profit is None:
        return _unassessed_kill("K11", "Capital band", "capital", "no profit model (fees blocking)")
    capital = _cents_to_usd(inp.profit.stressed.launch_capital_cents)
    assert capital is not None
    over = capital > kr.capital_max
    under = capital < kr.capital_min
    trig = over or under
    demoted = (over and _within_band(capital, kr.capital_max, kr.borderline_band)) or (
        under and _within_band(capital, kr.capital_min, kr.borderline_band)
    )
    return _kill(
        "K11",
        "Capital band",
        "capital",
        triggered=trig,
        demoted=trig and demoted,
        actual=f"${capital:,.0f}",
        threshold=f"${kr.capital_min:,.0f}-${kr.capital_max:,.0f}",
        evidence=f"stressed launch capital ${capital:,.0f}",
        reason="Outside my capital band; sub-$2k markets are usually commodity churn.",
    )


def _kill_avoid_list(inp: ScoringInput) -> KillResult:
    """K12 config `avoid` list match (glass, fragile, hazmat, etc.)."""
    trig = len(inp.avoid_matches) > 0
    return _kill(
        "K12",
        "Avoid list",
        "preference",
        triggered=trig,
        actual=", ".join(inp.avoid_matches) if trig else "no matches",
        threshold="no config `avoid` match",
        evidence=f"matched config avoid terms: {', '.join(inp.avoid_matches)}"
        if trig
        else "no config avoid match",
        reason="Operating preference exclusion.",
    )


def _run_kills(inp: ScoringInput, cfg: DeliumConfig) -> tuple[KillResult, ...]:
    k1, k2 = _kill_price(inp, cfg)
    return (
        k1,
        k2,
        _kill_oversized(inp),
        _kill_amazon(inp),
        _kill_brand_slots(inp, cfg),
        _kill_moat(inp, cfg),
        _kill_no_wedge(inp, cfg),
        _kill_restricted(inp),
        _kill_ip(inp),
        _kill_fad(inp, cfg),
        _kill_capital(inp, cfg),
        _kill_avoid_list(inp),
    )


# ---------------------------------------------------------------------------
# Stage 1 — Data sufficiency (scoring-model §3)
# ---------------------------------------------------------------------------
def _keepa_sufficient(inp: ScoringInput, cfg: DeliumConfig) -> bool:
    demand = inp.demand
    if demand is None:
        return False
    suf = cfg.sufficiency
    enough = sum(1 for e in demand.sales_estimates if e.observed_days >= suf.keepa_history_days)
    return enough >= suf.keepa_min_asins


def _keywords_sufficient(inp: ScoringInput, cfg: DeliumConfig) -> bool:
    demand = inp.demand
    if demand is None:
        return False
    return demand.keyword_demand.volumed_phrase_count >= cfg.sufficiency.keyword_min_phrases


def _reviews_sufficient(inp: ScoringInput, cfg: DeliumConfig) -> bool:
    diff = inp.differentiation
    if diff is None:
        return False
    return diff.confidence.sample_size >= cfg.sufficiency.review_sample_min


def _price_history_sufficient(inp: ScoringInput, cfg: DeliumConfig) -> bool:
    comp = inp.competition
    if comp is None:
        return False
    return comp.confidence.price_history_coverage >= cfg.sufficiency.price_history_min_coverage


# ---------------------------------------------------------------------------
# Pillar assembly (scoring-model §4-§8)
# ---------------------------------------------------------------------------
def _profit_pillar_components(
    stressed: ProfitResult, cfg: DeliumConfig
) -> tuple[tuple[Subscore, ...], float]:
    """P1-P4 from the stressed profit case (scoring-model §7). Returns the four
    component subscores and the weighted raw pillar (pre gate-cap)."""
    pp = cfg.profit_pillar
    p1 = norm(stressed.net_margin, pp.margin_lo, pp.margin_hi)
    p2 = norm(stressed.roi, pp.roi_lo, pp.roi_hi)

    capital = stressed.launch_capital_cents / 100.0
    if capital <= 0:
        p3 = 0.0
    elif capital < pp.capital_ideal_lo:
        p3 = pp.capital_thin_score
    elif capital <= pp.capital_ideal_hi:
        p3 = 100.0
    elif capital <= pp.capital_soft_hi:
        p3 = 100.0 - (capital - pp.capital_ideal_hi) / (
            pp.capital_soft_hi - pp.capital_ideal_hi
        ) * (100.0 - 60.0)
    elif capital < pp.capital_hard_hi:
        p3 = (
            60.0 - (capital - pp.capital_soft_hi) / (pp.capital_hard_hi - pp.capital_soft_hi) * 60.0
        )
    else:
        p3 = 0.0

    payback = stressed.payback_months
    if payback is None:
        p4 = 0.0
    elif payback <= pp.payback_ideal_months:
        p4 = 100.0
    elif payback < pp.payback_zero_months:
        p4 = (
            100.0
            - (payback - pp.payback_ideal_months)
            / (pp.payback_zero_months - pp.payback_ideal_months)
            * 100.0
        )
    else:
        p4 = 0.0

    weights = (pp.weight_margin, pp.weight_roi, pp.weight_capital, pp.weight_payback)
    total_w = sum(weights)
    values = (p1, p2, p3, p4)
    raw = sum(v * w for v, w in zip(values, weights, strict=True)) / total_w if total_w else 0.0

    payback_txt = "never" if payback is None else f"{payback:.1f}mo"
    components = (
        Subscore(
            "net_margin_stressed",
            p1,
            pp.weight_margin,
            f"stressed net margin {stressed.net_margin:.0%} → {p1:.0f}",
        ),
        Subscore("roi_stressed", p2, pp.weight_roi, f"stressed ROI {stressed.roi:.0%} → {p2:.0f}"),
        Subscore(
            "capital_fit", p3, pp.weight_capital, f"launch capital ${capital:,.0f} → {p3:.0f}"
        ),
        Subscore("payback", p4, pp.weight_payback, f"payback {payback_txt} → {p4:.0f}"),
    )
    return components, clamp(raw)


def _profit_confidence(profit: ScenarioSet | None) -> Confidence:
    return profit.confidence if profit is not None else Confidence.LOW


def _demand_pillar(inp: ScoringInput, cfg: DeliumConfig) -> PillarScore:
    weight = cfg.score_weights.demand
    demand = inp.demand
    if demand is None:
        return PillarScore(
            "demand",
            None,
            None,
            weight,
            0.0,
            Confidence.LOW,
            False,
            True,
            "demand report absent",
            "demand.py",
            (),
            "no demand report",
        )
    raw = demand.pillar_score
    caps: list[tuple[float, str]] = []
    if not _keywords_sufficient(inp, cfg):
        caps.append((cfg.sufficiency.demand_cap_missing_keywords, "keyword volume insufficient"))
    if not _keepa_sufficient(inp, cfg):
        caps.append((cfg.sufficiency.demand_cap_thin_keepa, "thin Keepa history"))
    capped, partial, reason = _apply_caps(raw, caps)
    return PillarScore(
        "demand",
        raw,
        capped,
        weight,
        0.0,
        demand.confidence,
        True,
        partial,
        reason,
        "demand.py",
        demand.components,
        f"demand pillar {raw:.0f}" + (f" (capped {capped:.0f}: {reason})" if partial else ""),
    )


def _competition_pillar(inp: ScoringInput, cfg: DeliumConfig) -> PillarScore:
    weight = cfg.score_weights.competition
    comp = inp.competition
    if comp is None:
        return PillarScore(
            "competition",
            None,
            None,
            weight,
            0.0,
            Confidence.LOW,
            False,
            True,
            "competition report absent",
            "competition.py",
            (),
            "no competition report",
        )
    raw = comp.pillar_score
    # Price history thinness only neutralizes the C6 component (handled inside
    # competition.py) — it does not make the whole pillar `partial` (§3 table).
    note = "" if _price_history_sufficient(inp, cfg) else " [price-war component neutralized]"
    return PillarScore(
        "competition",
        raw,
        raw,
        weight,
        0.0,
        comp.confidence.level,
        True,
        False,
        None,
        "competition.py",
        comp.components,
        f"competition pillar {raw:.0f}{note}",
    )


def _differentiation_pillar(inp: ScoringInput, cfg: DeliumConfig) -> PillarScore:
    weight = cfg.score_weights.differentiation
    diff = inp.differentiation
    if diff is None:
        return PillarScore(
            "differentiation",
            None,
            None,
            weight,
            0.0,
            Confidence.LOW,
            False,
            True,
            "differentiation report absent",
            "differentiation.py",
            (),
            "no differentiation report",
        )
    raw = diff.pillar_score
    caps: list[tuple[float, str]] = []
    if not _reviews_sufficient(inp, cfg):
        caps.append((cfg.sufficiency.differentiation_cap_thin_reviews, "review sample < minimum"))
    capped, partial, reason = _apply_caps(raw, caps)
    return PillarScore(
        "differentiation",
        raw,
        capped,
        weight,
        0.0,
        diff.confidence.level,
        True,
        partial,
        reason,
        "differentiation.py",
        diff.components,
        f"differentiation pillar {raw:.0f}"
        + (f" (capped {capped:.0f}: {reason})" if partial else ""),
    )


def _profit_pillar(inp: ScoringInput, cfg: DeliumConfig, g1_passed: bool) -> PillarScore:
    weight = cfg.score_weights.profitability
    profit = inp.profit
    if profit is None:
        # Fee inputs are blocking (§3): no real fees → no profitability score.
        return PillarScore(
            "profitability",
            None,
            None,
            weight,
            0.0,
            Confidence.LOW,
            False,
            True,
            "fee inputs blocking (dims/weight/category required)",
            "profit.py",
            (),
            "no profit model — fee inputs required",
        )
    components, raw = _profit_pillar_components(profit.stressed, cfg)
    capped = raw
    reason: str | None = None
    partial = False
    if not g1_passed:
        # §7 gate linkage: failing the unstressed profit gates caps the pillar.
        cap = cfg.profit_pillar.gate_fail_cap
        if raw > cap:
            capped = cap
            reason = f"G1 profit gate failed → capped at {cap:.0f}"
    return PillarScore(
        "profitability",
        raw,
        capped,
        weight,
        0.0,
        profit.confidence,
        True,
        partial,
        reason,
        "profit.py",
        components,
        f"profitability pillar {raw:.0f}" + (f" ({reason})" if reason else ""),
    )


def _risk_pillar(inp: ScoringInput, cfg: DeliumConfig) -> PillarScore:
    weight = cfg.score_weights.risk
    risk = inp.risk
    if risk is None:
        return PillarScore(
            "risk",
            None,
            None,
            weight,
            0.0,
            Confidence.LOW,
            False,
            True,
            "risk report absent",
            "risk.py",
            (),
            "no risk report",
        )
    raw = risk.risk_score
    components = tuple(
        Subscore(f.risk_type, f.deduction, 0.0, f"{f.evidence} (−{f.deduction:.0f})")
        for f in risk.flags
    )
    return PillarScore(
        "risk",
        raw,
        raw,
        weight,
        0.0,
        risk.confidence.level,
        True,
        False,
        None,
        "risk.py",
        components,
        f"risk pillar {raw:.0f} (100 − deductions)",
    )


def _apply_caps(raw: float, caps: list[tuple[float, str]]) -> tuple[float, bool, str | None]:
    """Apply the tightest sufficiency cap. Returns (capped, partial, reason)."""
    if not caps:
        return raw, False, None
    cap_value, reason = min(caps, key=lambda c: c[0])
    if raw <= cap_value:
        # Data is thin, but the raw score is already at/under the cap: the score
        # is unchanged, yet the pillar is still flagged `partial` (blocks Buy).
        return raw, True, reason
    return cap_value, True, reason


# ---------------------------------------------------------------------------
# Stage 4 — Gates (scoring-model §10)
# ---------------------------------------------------------------------------
def _gate_profit(inp: ScoringInput, cfg: DeliumConfig) -> GateResult:
    """G1 unstressed margin ≥ min, ROI ≥ min, payback ≤ max (hard)."""
    g = cfg.gates
    profit = inp.profit
    threshold = (
        f"margin ≥ {g.min_margin:.0%}, ROI ≥ {g.min_roi:.0%}, "
        f"payback ≤ {g.max_payback_months:.0f}mo"
    )
    if profit is None:
        return GateResult(
            "G1",
            "Profit gate",
            False,
            True,
            "no profit model",
            threshold,
            "fee inputs blocking — profitability cannot be verified",
        )
    r = profit.expected  # unstressed, config-driven (§10 G1)
    margin_ok = r.net_margin >= g.min_margin
    roi_ok = r.roi >= g.min_roi
    payback_ok = r.payback_months is not None and r.payback_months <= g.max_payback_months
    passed = margin_ok and roi_ok and payback_ok
    payback_txt = "never" if r.payback_months is None else f"{r.payback_months:.1f}mo"
    return GateResult(
        "G1",
        "Profit gate",
        passed,
        True,
        f"margin {r.net_margin:.0%}, ROI {r.roi:.0%}, payback {payback_txt}",
        threshold,
        "unstressed profit gates " + ("pass" if passed else "fail"),
    )


def _gate_data_quality(partial_pillars: tuple[str, ...]) -> GateResult:
    """G2 no pillar in `partial` state (soft)."""
    passed = len(partial_pillars) == 0
    return GateResult(
        "G2",
        "Data quality",
        passed,
        False,
        "partial: " + ", ".join(partial_pillars) if partial_pillars else "all pillars sufficient",
        "no pillar in partial state",
        "sufficient evidence" if passed else f"partial pillars: {', '.join(partial_pillars)}",
    )


def _gate_risk_floor(inp: ScoringInput, cfg: DeliumConfig) -> GateResult:
    """G3 risk pillar ≥ floor (hard)."""
    floor = cfg.gates.risk_floor
    risk = inp.risk
    if risk is None:
        return GateResult(
            "G3",
            "Risk floor",
            False,
            True,
            "no risk report",
            f"risk ≥ {floor:.0f}",
            "risk pillar unavailable",
        )
    passed = risk.risk_score >= floor
    return GateResult(
        "G3",
        "Risk floor",
        passed,
        True,
        f"risk {risk.risk_score:.0f}",
        f"risk ≥ {floor:.0f}",
        "risk floor " + ("cleared" if passed else "breached (near-catastrophic flags)"),
    )


def _gate_differentiation(diff_pillar: PillarScore, cfg: DeliumConfig) -> GateResult:
    """G4 differentiation pillar ≥ floor (soft)."""
    floor = cfg.gates.differentiation_floor
    value = diff_pillar.capped_score
    if value is None:
        return GateResult(
            "G4",
            "Differentiation floor",
            False,
            False,
            "no differentiation report",
            f"differentiation ≥ {floor:.0f}",
            "differentiation pillar unavailable",
        )
    passed = value >= floor
    return GateResult(
        "G4",
        "Differentiation floor",
        passed,
        False,
        f"differentiation {value:.0f}",
        f"differentiation ≥ {floor:.0f}",
        "meaningful improvement possible" if passed else "cannot make it meaningfully better",
    )


def _gate_strategist(concurrence: StrategistConcurrence) -> GateResult:
    """G5 Strategist concurrence (scoring-model §10). A *validated* concurrence
    signal resolved deterministically here — the LLM never sets the verdict. G5 is
    a BUY gate: it can only block a would-be Buy, never manufacture one. PENDING
    (default) leaves a provisional Buy allowed; CONCUR passes; DISSENT/UNAVAILABLE
    cap a would-be Buy at Test with the reason surfaced."""
    passed: bool | None
    if concurrence is StrategistConcurrence.CONCUR:
        passed, actual = True, "strategist concurs (buy)"
    elif concurrence is StrategistConcurrence.DISSENT:
        passed, actual = False, "strategist does not concur"
    elif concurrence is StrategistConcurrence.UNAVAILABLE:
        passed, actual = None, "strategist unavailable (degraded)"
    else:  # PENDING
        passed, actual = None, "pending"
    return GateResult(
        "G5",
        "Strategist concurrence",
        passed,
        True,
        actual,
        "frontier verdict = buy",
        "a Buy requires Strategist concurrence; G5 can only block a Buy, never create one",
    )


# ---------------------------------------------------------------------------
# Confidence + verdict
# ---------------------------------------------------------------------------
def _score_confidence(pillars: tuple[PillarScore, ...]) -> ScoreConfidence:
    partial = tuple(p.pillar for p in pillars if p.partial and p.available)
    missing = tuple(p.pillar for p in pillars if not p.available)
    notes: list[str] = []
    level = Confidence.HIGH
    if missing or partial:
        level = Confidence.LOW
        if missing:
            notes.append("missing pillars: " + ", ".join(missing))
        if partial:
            notes.append("partial pillars: " + ", ".join(partial))
    else:
        worst = min(_CONF_RANK[p.confidence] for p in pillars)
        if worst == 0:
            level = Confidence.LOW
            notes.append("a pillar report is low-confidence")
        elif worst == 1:
            level = Confidence.MEDIUM
    return ScoreConfidence(level, partial, missing, tuple(notes))


def _decide_verdict(
    *,
    score: float,
    kills: tuple[KillResult, ...],
    gates: tuple[GateResult, ...],
    concurrence: StrategistConcurrence,
    cfg: DeliumConfig,
) -> tuple[Verdict, tuple[str, ...]]:
    v = cfg.verdicts
    basis: list[str] = []

    hard_kills = [k for k in kills if k.kills]
    demoted = [k for k in kills if k.assessed and k.triggered and k.demoted]
    # G5 is a BUY gate handled separately below — it can only block a would-be Buy,
    # never force AVOID on a Test/Avoid. So it is excluded from the blanket
    # hard-gate-fail rule that turns G1/G3 failures into AVOID.
    hard_gate_fail = [g for g in gates if g.hard and g.passed is False and g.gate_id != "G5"]
    soft_gate_fail = [g for g in gates if not g.hard and g.passed is False]

    if hard_kills:
        basis.append("hard kill: " + ", ".join(k.rule_id for k in hard_kills))
        return Verdict.AVOID, tuple(basis)
    if hard_gate_fail:
        basis.append("hard gate failed: " + ", ".join(g.gate_id for g in hard_gate_fail))
        return Verdict.AVOID, tuple(basis)
    if score < v.test_min:
        basis.append(f"score {score:.1f} < {v.test_min:.0f}")
        return Verdict.AVOID, tuple(basis)

    if score >= v.buy_min and not soft_gate_fail and not demoted:
        # A Buy requires Strategist concurrence (G5). PENDING (not yet evaluated)
        # leaves a provisional Buy; CONCUR confirms it. DISSENT or UNAVAILABLE
        # blocks the Buy → capped at Test (never AVOID), disagreement surfaced.
        if concurrence in (StrategistConcurrence.PENDING, StrategistConcurrence.CONCUR):
            note = (
                "all deterministic gates pass; Strategist concurs"
                if concurrence is StrategistConcurrence.CONCUR
                else "all deterministic gates pass (G5 provisional — Strategist pending)"
            )
            basis.append(f"score {score:.1f} ≥ {v.buy_min:.0f}, {note}")
            return Verdict.BUY, tuple(basis)
        basis.append(
            f"score {score:.1f} ≥ {v.buy_min:.0f} but G5 not met "
            f"({concurrence.value}) → capped at Test"
        )
        return Verdict.TEST, tuple(basis)

    if soft_gate_fail:
        basis.append("soft gate(s) failed: " + ", ".join(g.gate_id for g in soft_gate_fail))
    if demoted:
        basis.append("borderline kill demotion: " + ", ".join(k.rule_id for k in demoted))
    if not basis:
        basis.append(f"score {score:.1f} in Test band [{v.test_min:.0f}, {v.buy_min:.0f})")
    return Verdict.TEST, tuple(basis)


# ---------------------------------------------------------------------------
# Config snapshot
# ---------------------------------------------------------------------------
def _snapshot(cfg: DeliumConfig) -> ConfigSnapshot:
    w, g, k, v = cfg.score_weights, cfg.gates, cfg.kill_rules, cfg.verdicts
    s, pp = cfg.sufficiency, cfg.profit_pillar
    return ConfigSnapshot(
        weights=(
            ("demand", w.demand),
            ("competition", w.competition),
            ("differentiation", w.differentiation),
            ("profitability", w.profitability),
            ("risk", w.risk),
        ),
        gate_thresholds=(
            ("min_margin", g.min_margin),
            ("min_roi", g.min_roi),
            ("max_payback_months", g.max_payback_months),
            ("risk_floor", g.risk_floor),
            ("differentiation_floor", g.differentiation_floor),
        ),
        kill_thresholds=(
            ("price_min", k.price_min),
            ("price_max", k.price_max),
            ("max_median_reviews", float(k.max_median_reviews)),
            ("max_brand_slots", float(k.max_brand_slots)),
            ("fad_spike_ratio", k.fad_spike_ratio),
            ("fad_min_history_months", float(k.fad_min_history_months)),
            ("excellent_listing_quality", k.excellent_listing_quality),
            ("excellent_complaint_rate", k.excellent_complaint_rate),
            ("capital_max", k.capital_max),
            ("capital_min", k.capital_min),
            ("borderline_band", k.borderline_band),
        ),
        verdict_thresholds=(("buy_min", v.buy_min), ("test_min", v.test_min)),
        sufficiency=(
            ("keepa_history_days", float(s.keepa_history_days)),
            ("keepa_min_asins", float(s.keepa_min_asins)),
            ("review_sample_min", float(s.review_sample_min)),
            ("keyword_min_phrases", float(s.keyword_min_phrases)),
            ("price_history_min_coverage", s.price_history_min_coverage),
            ("demand_cap_thin_keepa", s.demand_cap_thin_keepa),
            ("demand_cap_missing_keywords", s.demand_cap_missing_keywords),
            ("differentiation_cap_thin_reviews", s.differentiation_cap_thin_reviews),
        ),
        profit_pillar=(
            ("margin_lo", pp.margin_lo),
            ("margin_hi", pp.margin_hi),
            ("roi_lo", pp.roi_lo),
            ("roi_hi", pp.roi_hi),
            ("capital_ideal_lo", pp.capital_ideal_lo),
            ("capital_ideal_hi", pp.capital_ideal_hi),
            ("capital_soft_hi", pp.capital_soft_hi),
            ("capital_hard_hi", pp.capital_hard_hi),
            ("capital_thin_score", pp.capital_thin_score),
            ("payback_ideal_months", pp.payback_ideal_months),
            ("payback_zero_months", pp.payback_zero_months),
            ("gate_fail_cap", pp.gate_fail_cap),
            ("weight_margin", pp.weight_margin),
            ("weight_roi", pp.weight_roi),
            ("weight_capital", pp.weight_capital),
            ("weight_payback", pp.weight_payback),
        ),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def score_opportunity(
    inp: ScoringInput,
    config: DeliumConfig,
    *,
    strategist: StrategistConcurrence = StrategistConcurrence.PENDING,
) -> ScoredOpportunity:
    """Assemble the five pillar reports into the final opportunity score.

    Runs the four-stage funnel in order (§1) and returns a fully self-contained,
    hand-auditable `ScoredOpportunity`. Pure: same inputs + same config +
    same `strategist` → identical output. `config` supplies every threshold;
    nothing is hardcoded.

    `strategist` is the resolved G5 concurrence (default PENDING = provisional,
    the agents-off behavior). It is a validated boolean-like signal, never the LLM
    setting the verdict: G5 is a BUY gate that can only block a would-be Buy.
    """
    # Stage 0 — hard kills.
    kills = _run_kills(inp, config)

    # Stage 4a — profit gate is needed before the profit pillar (gate linkage §7).
    g1 = _gate_profit(inp, config)

    # Stages 1+2 — sufficiency-capped pillar scores.
    demand_p = _demand_pillar(inp, config)
    competition_p = _competition_pillar(inp, config)
    differentiation_p = _differentiation_pillar(inp, config)
    profit_p = _profit_pillar(inp, config, g1.passed is True)
    risk_p = _risk_pillar(inp, config)
    pillars_raw = (demand_p, competition_p, differentiation_p, profit_p, risk_p)

    # Stage 3 — composite. Normalize by the configured weight total so the score
    # stays on a 0-100 scale; a missing pillar contributes 0 but stays in the
    # denominator (missing data is pessimistic, never optimistic — §3).
    total_w = sum(p.weight for p in pillars_raw)
    pillars: list[PillarScore] = []
    composite = 0.0
    for p in pillars_raw:
        contribution = 0.0
        if total_w > 0 and p.capped_score is not None:
            contribution = p.capped_score * p.weight / total_w
        composite += contribution
        pillars.append(
            PillarScore(
                p.pillar,
                p.raw_score,
                p.capped_score,
                p.weight,
                contribution,
                p.confidence,
                p.available,
                p.partial,
                p.cap_reason,
                p.source,
                p.components,
                p.evidence,
            )
        )
    composite = clamp(composite)
    pillars_t = tuple(pillars)

    # Stage 4b — remaining gates.
    partial_pillars = tuple(p.pillar for p in pillars_t if p.partial)
    gates = (
        g1,
        _gate_data_quality(partial_pillars),
        _gate_risk_floor(inp, config),
        _gate_differentiation(differentiation_p, config),
        _gate_strategist(strategist),
    )

    verdict, basis = _decide_verdict(
        score=composite, kills=kills, gates=gates, concurrence=strategist, cfg=config
    )
    confidence = _score_confidence(pillars_t)
    insufficient = len(partial_pillars) > 0
    # A Buy is provisional only while G5 is unresolved (pending) or the Strategist
    # could not run (unavailable); a concur/dissent verdict is final.
    strategist_pending = strategist in (
        StrategistConcurrence.PENDING,
        StrategistConcurrence.UNAVAILABLE,
    )

    return ScoredOpportunity(
        verdict=verdict,
        score=composite,
        base_weighted_score=composite,
        pillars=pillars_t,
        kills=kills,
        gates=gates,
        confidence=confidence,
        insufficient_data=insufficient,
        strategist_pending=strategist_pending,
        verdict_basis=basis,
        config_snapshot=_snapshot(config),
    )
