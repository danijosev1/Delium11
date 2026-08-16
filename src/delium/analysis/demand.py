"""Deterministic demand analysis engine (docs/analysis-engine.md §1,
docs/scoring-model.md §4).

Pure computation: no provider/DB/LLM imports, no network, no current-time calls
(the caller passes `as_of`). Same inputs + same config → same output. Missing
data lowers confidence and drops components out of the pillar; it is never
guessed or filled with optimistic defaults.

Produces the five demand components (search volume, sales velocity, BSR trend,
market growth, seasonality), a sales-estimate *range* per ASIN (never a single
falsely precise number), and the 0-100 demand pillar score for scoring.py.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from datetime import date
from math import log10
from pathlib import Path
from statistics import median
from typing import Any

from delium.analysis import curves
from delium.analysis.models import (
    AsinHistory,
    BsrTrend,
    CategoryCurve,
    Confidence,
    DemandConfig,
    DemandReport,
    KeywordDatum,
    KeywordDemand,
    SalesEstimate,
    Seasonality,
    Subscore,
    VelocityAnchor,
    VelocityCurves,
)

CURVES_DIR = Path(__file__).parent / "curves_data" / "bsr_velocity"
DEFAULT_CURVES = "us"


class DemandError(Exception):
    """Raised for unrecoverable configuration problems (e.g. missing curve file)."""


# ---------------------------------------------------------------------------
# Loading the versioned BSR → velocity curves
# ---------------------------------------------------------------------------
def load_velocity_curves(version: str = DEFAULT_CURVES) -> VelocityCurves:
    path = CURVES_DIR / f"{version}.toml"
    if not path.exists():
        raise DemandError(f"BSR velocity curves {version!r} not found at {path}.")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return _parse_curves(raw)


def _parse_category(name: str, raw: dict[str, Any]) -> CategoryCurve:
    anchors = tuple(
        VelocityAnchor(bsr=int(a["bsr"]), units=int(a["units"]))
        for a in sorted(raw["anchors"], key=lambda a: int(a["bsr"]))
    )
    return CategoryCurve(
        name=name,
        base_factor=float(raw["base_factor"]),
        multi_unit_factor=float(raw["multi_unit_factor"]),
        anchors=anchors,
    )


def _parse_curves(raw: dict[str, Any]) -> VelocityCurves:
    categories = {
        name: _parse_category(name, body) for name, body in raw.get("categories", {}).items()
    }
    return VelocityCurves(
        version=str(raw["version"]),
        default=_parse_category("default", raw["default"]),
        categories=categories,
    )


def _curve_units(anchors: tuple[VelocityAnchor, ...], bsr: int) -> float:
    """Log-log interpolate monthly units at a BSR; clamp beyond the anchor ends."""
    if bsr <= anchors[0].bsr:
        return float(anchors[0].units)
    if bsr >= anchors[-1].bsr:
        return float(anchors[-1].units)
    for lo, hi in zip(anchors, anchors[1:], strict=False):
        if lo.bsr <= bsr <= hi.bsr:
            t = (log10(bsr) - log10(lo.bsr)) / (log10(hi.bsr) - log10(lo.bsr))
            log_units = log10(lo.units) + t * (log10(hi.units) - log10(lo.units))
            return float(10.0**log_units)
    return float(anchors[-1].units)  # pragma: no cover - anchors are sorted


# ---------------------------------------------------------------------------
# Sales estimate (rank-drop method with curve fallback)
# ---------------------------------------------------------------------------
def _offsets(history: AsinHistory, as_of: date) -> list[tuple[int, int]]:
    """(days_before_as_of, bsr) for positive-BSR observations, past only."""
    out: list[tuple[int, int]] = []
    for point in history.observations:
        off = (as_of - point.date).days
        if off >= 0 and point.bsr > 0:
            out.append((off, point.bsr))
    return out


def _sales_estimate(
    history: AsinHistory,
    as_of: date,
    curve: CategoryCurve,
    category_known: bool,
    cfg: DemandConfig,
) -> SalesEstimate | None:
    points = _offsets(history, as_of)
    if not points:
        return None

    current_bsr = min(points, key=lambda p: p[0])[1]  # smallest offset = newest
    rank_ref = round(_curve_units(curve.anchors, current_bsr))

    window = sorted((p for p in points if p[0] <= 90), key=lambda p: -p[0])  # oldest→newest
    n_obs = len(window)
    drops = sum(1 for k in range(len(window) - 1) if window[k + 1][1] < window[k][1])
    observed_days = window[0][0] - window[-1][0] if len(window) >= 2 else 0

    if observed_days >= cfg.partial_history_days:
        monthly = drops / observed_days * 30
        width = 1.0 if observed_days >= cfg.full_history_days else 1.5
        expected = round(monthly * curve.base_factor)
        low = round(monthly * cfg.low_mult / width)
        high = round(monthly * cfg.high_mult * curve.multi_unit_factor * width)
        method = "rank_drop"
        # HIGH requires both full history AND a known category curve — an unknown
        # category caps a per-ASIN estimate at MEDIUM (doc §1).
        confidence = (
            Confidence.HIGH
            if (observed_days >= cfg.full_history_days and category_known)
            else Confidence.MEDIUM
        )
    else:
        # Thin history: fall back to the category rank curve with wide bounds.
        expected = rank_ref
        low = round(rank_ref * 0.5)
        high = round(rank_ref * 2.0 * curve.multi_unit_factor)
        method = "curve_fallback"
        confidence = Confidence.LOW

    # Guarantee low <= expected <= high, non-negative.
    low = max(0, min(low, expected))
    high = max(high, expected)
    return SalesEstimate(
        asin=history.asin,
        low_units=low,
        expected_units=expected,
        high_units=high,
        confidence=confidence,
        method=method,
        observed_days=observed_days,
        n_observations=n_obs,
        drops=drops,
        current_bsr=current_bsr,
        rank_reference_units=rank_ref,
    )


# ---------------------------------------------------------------------------
# BSR trend (Theil–Sen)
# ---------------------------------------------------------------------------
def _asin_improvement(history: AsinHistory, as_of: date) -> float | None:
    """Annualized log10-BSR improvement (positive = rank getting better)."""
    window = [p for p in _offsets(history, as_of) if p[0] <= 90]
    if len(window) < 2:
        return None
    xs = [-off for off, _ in window]  # time increases with x
    ys = [log10(bsr) for _, bsr in window]
    slope = curves.theil_sen(xs, ys)  # d log10(bsr) / d day
    if slope is None:
        return None
    return -slope * 365.0  # improving rank = decreasing BSR = negative slope


def _bsr_trend(histories: Sequence[AsinHistory], as_of: date, cfg: DemandConfig) -> BsrTrend:
    improvements = [imp for h in histories if (imp := _asin_improvement(h, as_of)) is not None]
    if not improvements:
        return BsrTrend("unknown", None, None, None, 0)
    med = median(improvements)
    score = curves.norm(med, cfg.trend_lo, cfg.trend_hi)
    direction = "improving" if med > 0.02 else "declining" if med < -0.02 else "flat"
    return BsrTrend(direction, abs(med), med, score, len(improvements))


# ---------------------------------------------------------------------------
# Seasonality (target ASIN, needs >= 12 months)
# ---------------------------------------------------------------------------
def _seasonality(history: AsinHistory, as_of: date, cfg: DemandConfig) -> Seasonality:
    points = [p for p in _offsets(history, as_of) if p[0] <= cfg.seasonality_min_days]
    max_off = max((off for off, _ in points), default=0)
    if max_off < cfg.seasonality_min_days:
        return Seasonality(False, None, None, None, 0)

    # Weekly bins of the inverse-BSR demand proxy.
    bins: dict[int, list[float]] = {}
    for off, bsr in points:
        bins.setdefault(off // 7, []).append(1.0 / bsr)
    weekly = [sum(v) / len(v) for v in bins.values()]
    if len(weekly) < cfg.seasonality_min_weeks:
        return Seasonality(False, None, None, None, len(weekly))

    total = sum(weekly)
    if total <= 0:
        return Seasonality(False, None, None, None, len(weekly))
    peak = sum(sorted(weekly, reverse=True)[:8]) / total
    score = 100.0 - curves.norm(peak, cfg.seasonality_lo, cfg.seasonality_hi)
    return Seasonality(True, peak, peak > cfg.seasonal_flag_threshold, score, len(weekly))


# ---------------------------------------------------------------------------
# Keyword demand (D1 volume + D4 growth)
# ---------------------------------------------------------------------------
def _keyword_demand(
    keywords: Sequence[KeywordDatum],
    primary_phrase: str | None,
    volume_series: Sequence[int] | None,
    cfg: DemandConfig,
) -> KeywordDemand:
    volumed = [(k.phrase, k.volume) for k in keywords if k.volume is not None and k.volume > 0]
    total = sum(v for _, v in volumed)

    # Dedup: a phrase that is a substring of a higher-volume member counts at 30%.
    deduped = 0.0
    for phrase, vol in volumed:
        is_sub = any(
            phrase != other and phrase in other and other_vol > vol for other, other_vol in volumed
        )
        deduped += vol * (cfg.dedup_substring_weight if is_sub else 1.0)
    deduplicated = round(deduped)

    primary = primary_phrase
    if primary is None and volumed:
        primary = max(volumed, key=lambda kv: kv[1])[0]
    primary_volume = next((v for p, v in volumed if p == primary), 0)
    concentration = primary_volume / total if total > 0 else 0.0

    yoy: float | None = None
    growth_score: float | None = None
    if volume_series is not None and len(volume_series) >= 12 and volume_series[0] > 0:
        yoy = (volume_series[-1] - volume_series[0]) / volume_series[0]
        growth_score = curves.norm(yoy, cfg.growth_lo, cfg.growth_hi)

    volume_score = (
        curves.log_norm(deduplicated, cfg.volume_lo, cfg.volume_hi) if deduplicated > 0 else None
    )
    return KeywordDemand(
        total_volume=total,
        deduplicated_volume=deduplicated,
        primary_phrase=primary,
        primary_volume=primary_volume,
        demand_concentration=concentration,
        volumed_phrase_count=len(volumed),
        yoy_growth=yoy,
        search_volume_score=volume_score,
        market_growth_score=growth_score,
    )


# ---------------------------------------------------------------------------
# Confidence (deterministic, monotone in data availability)
# ---------------------------------------------------------------------------
def _confidence(
    histories: Sequence[AsinHistory],
    estimates: Sequence[SalesEstimate],
    keyword_demand: KeywordDemand,
    seasonality: Seasonality,
    category_known: bool,
    cfg: DemandConfig,
) -> Confidence:
    n = len(histories)
    if n == 0:
        return Confidence.LOW
    full_history = sum(1 for e in estimates if e.observed_days >= cfg.full_history_days)
    frac_full = full_history / n
    median_obs = median([e.n_observations for e in estimates]) if estimates else 0
    volumed = keyword_demand.volumed_phrase_count

    points = 0.0
    points += 2 if frac_full >= 0.8 else 1 if frac_full >= 0.5 else 0
    points += 1 if median_obs >= 8 else 0
    points += 2 if volumed >= cfg.full_volumed_phrases else 1 if volumed >= 2 else 0
    points += 1 if (category_known and full_history >= 1) else 0
    points += 1 if seasonality.assessable else 0

    if points >= 6 and category_known and volumed >= cfg.full_volumed_phrases and frac_full >= 0.8:
        return Confidence.HIGH
    if points <= 2:
        return Confidence.LOW
    return Confidence.MEDIUM


# ---------------------------------------------------------------------------
# Top-level engine
# ---------------------------------------------------------------------------
def _pillar(components: Sequence[Subscore]) -> float:
    available = [c for c in components if c.value is not None]
    total_weight = sum(c.weight for c in available)
    if total_weight <= 0:
        return 0.0
    weighted = sum((c.value or 0.0) * c.weight for c in available)
    return curves.clamp(weighted / total_weight)


def analyze_demand(
    *,
    as_of: date,
    histories: Sequence[AsinHistory],
    keywords: Sequence[KeywordDatum],
    category: str | None,
    curves_table: VelocityCurves,
    primary_phrase: str | None = None,
    volume_series: Sequence[int] | None = None,
    config: DemandConfig | None = None,
) -> DemandReport:
    """Analyze demand for a market (target ASIN first in `histories`)."""
    cfg = config or DemandConfig()
    curve, category_known = curves_table.resolve(category)

    estimates = tuple(
        est
        for h in histories
        if (est := _sales_estimate(h, as_of, curve, category_known, cfg)) is not None
    )
    market_low = round(sum(e.low_units for e in estimates) * cfg.market_share_factor)
    market_expected = round(sum(e.expected_units for e in estimates) * cfg.market_share_factor)
    market_high = round(sum(e.high_units for e in estimates) * cfg.market_share_factor)

    velocity_score: float | None = None
    median_expected_units: float | None = None
    if estimates:
        median_expected_units = median([e.expected_units for e in estimates])
        velocity_score = curves.plateau(median_expected_units, *cfg.velocity_curve)

    keyword_demand = _keyword_demand(keywords, primary_phrase, volume_series, cfg)
    trend = _bsr_trend(histories, as_of, cfg)
    seasonality = (
        _seasonality(histories[0], as_of, cfg)
        if histories
        else Seasonality(False, None, None, None, 0)
    )

    components = (
        Subscore(
            "search_volume",
            keyword_demand.search_volume_score,
            cfg.weight_search_volume,
            f"deduped cluster volume {keyword_demand.deduplicated_volume}",
        ),
        Subscore(
            "sales_velocity",
            velocity_score,
            cfg.weight_sales_velocity,
            f"median expected {median_expected_units} units/mo",
        ),
        Subscore(
            "bsr_trend",
            trend.score,
            cfg.weight_bsr_trend,
            f"median annual log10 improvement {trend.median_annual_change}",
        ),
        Subscore(
            "market_growth",
            keyword_demand.market_growth_score,
            cfg.weight_market_growth,
            f"keyword YoY {keyword_demand.yoy_growth}",
        ),
        Subscore(
            "seasonality",
            seasonality.score,
            cfg.weight_seasonality,
            f"peak-8-week concentration {seasonality.peak_concentration}",
        ),
    )

    confidence = _confidence(histories, estimates, keyword_demand, seasonality, category_known, cfg)

    return DemandReport(
        pillar_score=_pillar(components),
        confidence=confidence,
        components=components,
        sales_estimates=estimates,
        market_units_low=market_low,
        market_units_expected=market_expected,
        market_units_high=market_high,
        keyword_demand=keyword_demand,
        bsr_trend=trend,
        seasonality=seasonality,
    )
