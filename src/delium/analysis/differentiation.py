"""Deterministic differentiation analysis engine (docs/analysis-engine.md §3,
docs/scoring-model.md §6).

Pure computation over structured Review Miner output. It does NOT import an LLM,
call a provider/ingestion, or read the database, and it never trusts an
LLM-supplied percentage or severity.

Integrity rule: every complaint frequency is RECOMPUTED from the cited review
ids against the supplied review sample — unknown/duplicate/nonexistent ids
cannot inflate it, and a theme's frequency can never exceed 100%. Severity is
derived from the cited reviews' star ratings, not from the Miner's number.

Higher score = stronger, evidence-backed opportunity to build a meaningfully
better product.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import fmean

from delium.analysis import curves
from delium.analysis.models import (
    Addressability,
    Confidence,
    DifferentiationConfidence,
    DifferentiationConfig,
    DifferentiationInput,
    DifferentiationReport,
    DifferentiationTheme,
    FeatureRequest,
    RawTheme,
    Subscore,
    ThemeKind,
)


# ---------------------------------------------------------------------------
# Evidence resolution (the integrity core)
# ---------------------------------------------------------------------------
def _verified_ids(supporting: Sequence[str], eligible: frozenset[str]) -> frozenset[str]:
    """Unique cited ids that actually exist in the review sample.

    - duplicates collapse (set)
    - unknown / nonexistent ids are dropped (intersection)
    So |verified| <= |eligible|, guaranteeing frequency <= 1.0."""
    return frozenset(supporting) & eligible


def _frequency(verified: frozenset[str], sample_size: int) -> float:
    if sample_size <= 0:
        return 0.0
    return len(verified) / sample_size  # <= 1.0 by construction


def _severity(
    verified: frozenset[str], stars_by_id: dict[str, int], cfg: DifferentiationConfig
) -> int | None:
    """Severity 1-3 from the MEAN star rating of the cited reviews. The Miner's
    claimed severity is ignored entirely."""
    stars = [stars_by_id[i] for i in verified if i in stars_by_id]
    if not stars:
        return None
    mean_stars = fmean(stars)
    if mean_stars <= cfg.severity_high_stars:
        return 3
    if mean_stars <= cfg.severity_mid_stars:
        return 2
    return 1


def _addressability_weight(theme: RawTheme, cfg: DifferentiationConfig) -> float:
    if theme.addressability is Addressability.FIXABLE:
        if theme.cogs_delta is None or theme.cogs_delta <= cfg.addressable_cogs_max:
            return cfg.weight_fixable
        return cfg.weight_partial  # fixable but too expensive → only partial credit
    if theme.addressability is Addressability.PARTIAL:
        return cfg.weight_partial
    return 0.0  # HARD or UNKNOWN → never an optimistic score


# ---------------------------------------------------------------------------
# Theme evaluation
# ---------------------------------------------------------------------------
def _evaluate_themes(
    themes: Sequence[RawTheme],
    eligible: frozenset[str],
    stars_by_id: dict[str, int],
    sample_size: int,
    cfg: DifferentiationConfig,
) -> list[DifferentiationTheme]:
    out: list[DifferentiationTheme] = []
    for theme in themes:
        verified = _verified_ids(theme.supporting_review_ids, eligible)
        freq = _frequency(verified, sample_size)
        severity = _severity(verified, stars_by_id, cfg)
        counted = len(verified) >= cfg.min_quotes and severity is not None
        intensity = (freq * 100.0) * severity if (counted and severity is not None) else 0.0
        out.append(
            DifferentiationTheme(
                theme_id=theme.theme_id,
                label=theme.label,
                supporting_count_claimed=len(theme.supporting_review_ids),
                verified_count=len(verified),
                frequency=freq,
                severity=severity,
                intensity=intensity,
                addressability=theme.addressability,
                counted=counted,
                detail=(
                    f"{len(verified)}/{len(theme.supporting_review_ids)} ids verified, "
                    f"freq {freq:.1%}, severity {severity}"
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# F1 — complaint intensity
# ---------------------------------------------------------------------------
def _f1_complaint_intensity(
    evaluated: Sequence[DifferentiationTheme],
    raw_by_id: dict[str, RawTheme],
    bias_adjustment: float,
    cfg: DifferentiationConfig,
) -> float | None:
    complaints = [t for t in evaluated if raw_by_id[t.theme_id].kind is ThemeKind.COMPLAINT]
    if not complaints:
        return None
    counted = [t for t in complaints if t.counted]
    intensity_sum = sum(t.intensity for t in counted)
    score = curves.norm(intensity_sum, cfg.intensity_lo, cfg.intensity_hi)
    return curves.clamp(score + bias_adjustment)


# ---------------------------------------------------------------------------
# F2 — missing features
# ---------------------------------------------------------------------------
def _f2_missing_features(
    features: Sequence[FeatureRequest], eligible: frozenset[str], cfg: DifferentiationConfig
) -> tuple[float | None, int]:
    if not features:
        return None, 0
    gaps = 0
    for f in features:
        verified = _verified_ids(f.supporting_review_ids, eligible)
        # Counts only when backed by ≥ min_quotes AND confirmed absent from top-10.
        if len(verified) >= cfg.min_quotes and f.absent_from_competitors is True:
            gaps += 1
    score = min(gaps, cfg.feature_cap) * cfg.feature_per
    return score, gaps


# ---------------------------------------------------------------------------
# F3 — addressability
# ---------------------------------------------------------------------------
def _f3_addressability(
    evaluated: Sequence[DifferentiationTheme],
    raw_by_id: dict[str, RawTheme],
    cfg: DifferentiationConfig,
) -> float | None:
    counted = [
        t for t in evaluated if t.counted and raw_by_id[t.theme_id].kind is ThemeKind.COMPLAINT
    ]
    total = sum(t.intensity for t in counted)
    if total <= 0:
        return None  # no complaint mass to assess → not an optimistic default
    addressable = sum(
        t.intensity * _addressability_weight(raw_by_id[t.theme_id], cfg) for t in counted
    )
    return curves.clamp(addressable / total * 100.0)


# ---------------------------------------------------------------------------
# F4 — bundle & packaging rubric
# ---------------------------------------------------------------------------
def _theme_freq(theme: RawTheme, eligible: frozenset[str], sample_size: int) -> float:
    return _frequency(_verified_ids(theme.supporting_review_ids, eligible), sample_size)


def _f4_bundle_packaging(
    data: DifferentiationInput,
    eligible: frozenset[str],
    sample_size: int,
    cfg: DifferentiationConfig,
) -> float | None:
    if sample_size <= 0:
        return None
    points = 0.0
    # (a) a bundle complement is mentioned in ≥ 3% of reviews
    if any(
        _frequency(_verified_ids(b.supporting_review_ids, eligible), sample_size)
        >= cfg.bundle_freq_threshold
        for b in data.bundle_signals
    ):
        points += cfg.rubric_points
    # (b) the top-10 don't already bundle it (must be KNOWN false, never assumed)
    if data.competitors_bundle_complement is False:
        points += cfg.rubric_points
    # (c) a packaging-damage complaint ≥ 5%
    if any(
        (t.category or "").lower().startswith("packag")
        and _theme_freq(t, eligible, sample_size) >= cfg.packaging_freq_threshold
        for t in data.themes
        if t.kind is ThemeKind.COMPLAINT
    ):
        points += cfg.rubric_points
    # (d) a usage-confusion complaint ≥ 5%
    if any(
        (t.category or "").lower().startswith("usage")
        and _theme_freq(t, eligible, sample_size) >= cfg.usage_freq_threshold
        for t in data.themes
        if t.kind is ThemeKind.COMPLAINT
    ):
        points += cfg.rubric_points
    return min(points, 100.0)


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------
def _confidence(
    sample_size: int,
    verified_ratio: float,
    counted_themes: int,
    feature_evidence: bool,
    bias_flagged: bool,
    cfg: DifferentiationConfig,
    has_claims: bool,
) -> DifferentiationConfidence:
    if sample_size >= cfg.sample_high:
        base = 2
    elif sample_size >= cfg.sample_medium:
        base = 1
    else:
        base = 0
    if bias_flagged:
        base = min(base, 1)  # positive-skewed sample can't be high confidence
    if has_claims and verified_ratio < cfg.unresolved_ratio_floor:
        base = max(0, base - 1)  # many references cannot be resolved
    if counted_themes == 0 and not feature_evidence:
        base = 0  # no usable evidence
    level = (Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH)[base]
    return DifferentiationConfidence(
        level=level,
        sample_size=sample_size,
        verified_theme_ratio=verified_ratio,
        themes_with_evidence=counted_themes,
        feature_evidence=feature_evidence,
        sample_bias_flagged=bias_flagged,
    )


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


def _data_gaps(data: DifferentiationInput, listing_checkable: bool) -> tuple[str, ...]:
    gaps: list[str] = []
    if not data.reviews:
        gaps.append("reviews")
    if not any(t.kind is ThemeKind.COMPLAINT for t in data.themes):
        gaps.append("complaint_themes")
    if not data.feature_requests:
        gaps.append("feature_evidence")
    if not listing_checkable:
        gaps.append("listing_rating")
    if data.themes and all(t.addressability is Addressability.UNKNOWN for t in data.themes):
        gaps.append("addressability")
    return tuple(gaps)


def analyze_differentiation(
    data: DifferentiationInput, config: DifferentiationConfig | None = None
) -> DifferentiationReport:
    """Evidence-backed differentiation pillar (0-100) from Review Miner output."""
    cfg = config or DifferentiationConfig()
    eligible = frozenset(r.review_id for r in data.reviews)
    stars_by_id = {r.review_id: r.stars for r in data.reviews}
    sample_size = len(eligible)  # de-duplicated eligible reviews
    raw_by_id = {t.theme_id: t for t in data.themes}

    evaluated = _evaluate_themes(data.themes, eligible, stars_by_id, sample_size, cfg)

    # Sample-bias check (never silently corrects — flags + bounded F1 boost).
    sample_rating_avg = fmean([r.stars for r in data.reviews]) if data.reviews else None
    bias_delta: float | None = None
    bias_flag = False
    f1_adjustment = 0.0
    listing_checkable = data.listing_rating_avg is not None
    if sample_rating_avg is not None and listing_checkable and data.listing_rating_avg is not None:
        bias_delta = sample_rating_avg - data.listing_rating_avg
        if bias_delta > cfg.bias_threshold:
            bias_flag = True
            f1_adjustment = cfg.bias_adjustment  # complaints under-represented

    f1 = _f1_complaint_intensity(evaluated, raw_by_id, f1_adjustment, cfg)
    f2, feature_gaps = _f2_missing_features(data.feature_requests, eligible, cfg)
    f3 = _f3_addressability(evaluated, raw_by_id, cfg)
    f4 = _f4_bundle_packaging(data, eligible, sample_size, cfg)

    components = (
        Subscore(
            "complaint_intensity", f1, cfg.weight_complaint_intensity, "F1 recomputed intensity"
        ),
        Subscore(
            "missing_features", f2, cfg.weight_missing_features, f"{feature_gaps} confirmed gaps"
        ),
        Subscore(
            "addressability", f3, cfg.weight_addressability, "F3 fixable share of complaint mass"
        ),
        Subscore("bundle_packaging", f4, cfg.weight_bundle_packaging, "F4 rubric"),
    )

    # Confidence evidence metrics.
    total_claimed = sum(len(t.supporting_review_ids) for t in data.themes)
    total_verified = sum(t.verified_count for t in evaluated)
    verified_ratio = (total_verified / total_claimed) if total_claimed > 0 else 0.0
    counted_themes = sum(1 for t in evaluated if t.counted)
    feature_evidence = any(
        len(_verified_ids(f.supporting_review_ids, eligible)) >= cfg.min_quotes
        for f in data.feature_requests
    )
    confidence = _confidence(
        sample_size,
        verified_ratio,
        counted_themes,
        feature_evidence,
        bias_flag,
        cfg,
        has_claims=total_claimed > 0,
    )

    verified_supporting = sum(t.verified_count for t in evaluated if t.counted)

    return DifferentiationReport(
        pillar_score=_pillar(components),
        confidence=confidence,
        complaint_intensity_score=f1,
        missing_features_score=f2,
        addressability_score=f3,
        bundle_packaging_score=f4,
        themes=tuple(evaluated),
        feature_gap_count=feature_gaps,
        verified_supporting_reviews=verified_supporting,
        sample_rating_avg=sample_rating_avg,
        sample_bias_delta=bias_delta,
        sample_bias_flag=bias_flag,
        f1_bias_adjustment=f1_adjustment,
        components=components,
        data_gaps=_data_gaps(data, listing_checkable),
    )
