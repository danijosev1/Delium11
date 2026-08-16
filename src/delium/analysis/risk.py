"""Deterministic risk analysis engine (docs/scoring-model.md §8,
docs/analysis-engine.md §5).

Pure computation over facts already produced by the other engines and product
data. No LLM, no providers, no ingestion, no database, no network. The engine
invents no categories or weights: it applies EXACTLY the documented deduction
ledger (start 100, subtract per confirmed flag, floor 0).

Rules:
- Every non-zero deduction carries concrete evidence and a source.
- Confirmed risks and unknown data are distinct: a missing input becomes an
  `unassessed` entry (no deduction) — except seasonality, whose unknown case has
  its own documented −10, strictly smaller than the confirmed −20.
- Signals owned by another pillar (price war → competition, oversized → kill
  rule K3 / fees) are surfaced as informational flags with 0 deduction, never
  double-counted here.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from delium.analysis.models import (
    Confidence,
    RiskConfidence,
    RiskConfig,
    RiskFlag,
    RiskInput,
    RiskReport,
    RiskRules,
    RiskSeverity,
)

RISK_DATA_DIR = Path(__file__).parent / "risk_data"
DEFAULT_RISK_RULES = "us"


class RiskError(Exception):
    """Raised for unrecoverable configuration problems (e.g. missing rules file)."""


# ---------------------------------------------------------------------------
# Loading versioned risk rules
# ---------------------------------------------------------------------------
def load_risk_rules(version: str = DEFAULT_RISK_RULES) -> RiskRules:
    path = RISK_DATA_DIR / f"{version}.toml"
    if not path.exists():
        raise RiskError(f"Risk rules {version!r} not found at {path}.")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return _parse_rules(raw)


def _parse_rules(raw: dict[str, Any]) -> RiskRules:
    ip = raw.get("ip", {})
    compliance = raw.get("compliance", {})
    high_returns = raw.get("high_returns", {})
    fragility = raw.get("fragility", {})
    logistics = raw.get("logistics", {})
    return RiskRules(
        version=str(raw["version"]),
        ip_categories=frozenset(str(c) for c in ip.get("design_patent_categories", [])),
        brand_likeness_lexicon=tuple(str(t).lower() for t in ip.get("brand_likeness_lexicon", [])),
        compliance_map={str(k): str(v) for k, v in compliance.get("categories", {}).items()},
        high_return_categories=frozenset(str(c) for c in high_returns.get("categories", [])),
        fragility_materials=tuple(str(m).lower() for m in fragility.get("materials", [])),
        oversized_size_tiers=frozenset(str(t) for t in logistics.get("oversized_size_tiers", [])),
    )


# ---------------------------------------------------------------------------
# Rule result: a triggered flag, a clear (assessed, no risk), or unassessed
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _RuleResult:
    flag: RiskFlag | None
    assessed: bool


def _severity(deduction: float, cfg: RiskConfig) -> RiskSeverity:
    if deduction >= cfg.severity_critical:
        return RiskSeverity.CRITICAL
    if deduction >= cfg.severity_high:
        return RiskSeverity.HIGH
    if deduction >= cfg.severity_moderate:
        return RiskSeverity.MODERATE
    return RiskSeverity.INFO


def _flag(
    risk_type: str, deduction: float, evidence: str, source: str, explanation: str, cfg: RiskConfig
) -> _RuleResult:
    return _RuleResult(
        RiskFlag(risk_type, deduction, _severity(deduction, cfg), evidence, source, explanation),
        True,
    )


_CLEAR = _RuleResult(None, True)
_UNASSESSED = _RuleResult(None, False)


# ---------------------------------------------------------------------------
# Documented rules
# ---------------------------------------------------------------------------
def _rule_ip(data: RiskInput, rules: RiskRules, cfg: RiskConfig) -> _RuleResult:
    if data.category is None and data.patent_marked_listings is None and not data.titles:
        return _UNASSESSED
    if data.category is not None and data.category in rules.ip_categories:
        return _flag(
            "ip_signal",
            cfg.deduct_ip,
            f"category {data.category!r} on the design-patent watchlist",
            "risk_rules.ip_categories",
            "Design-heavy category with elevated IP/patent risk.",
            cfg,
        )
    if data.patent_marked_listings is True:
        return _flag(
            "ip_signal",
            cfg.deduct_ip,
            "Analyst flagged patent-marked listings",
            "patent_marked_listings",
            "Incumbent listings carry patent markings.",
            cfg,
        )
    for title in data.titles:
        low = title.lower()
        for term in rules.brand_likeness_lexicon:
            if term in low:
                return _flag(
                    "ip_signal",
                    cfg.deduct_ip,
                    f"brand-likeness term {term!r} in title {title[:60]!r}",
                    "risk_rules.brand_likeness_lexicon",
                    "Trademark-signature term present.",
                    cfg,
                )
    return _CLEAR


def _rule_compliance(data: RiskInput, rules: RiskRules, cfg: RiskConfig) -> _RuleResult:
    if data.category is None:
        return _UNASSESSED
    requirement = rules.compliance_map.get(data.category)
    if requirement is not None:
        return _flag(
            "compliance",
            cfg.deduct_compliance,
            f"{data.category!r} → {requirement}",
            "risk_rules.compliance_map",
            "Category triggers a certification/compliance surface.",
            cfg,
        )
    return _CLEAR


def _rule_trend(data: RiskInput, cfg: RiskConfig) -> _RuleResult:
    has_history = data.volume_history_months is not None
    has_ratio = data.current_volume is not None and data.volume_24mo_median is not None
    if not has_history and not has_ratio:
        return _UNASSESSED
    if (
        has_history
        and data.volume_history_months is not None
        and (data.volume_history_months < cfg.trend_history_months)
    ):
        return _flag(
            "trend_dependency",
            cfg.deduct_trend,
            f"only {data.volume_history_months}mo volume history (< {cfg.trend_history_months})",
            "volume_history_months",
            "Too little history to rule out a fad.",
            cfg,
        )
    if (
        has_ratio
        and data.current_volume is not None
        and data.volume_24mo_median is not None
        and data.volume_24mo_median > 0
        and data.current_volume > cfg.trend_fad_ratio * data.volume_24mo_median
    ):
        return _flag(
            "trend_dependency",
            cfg.deduct_trend,
            f"current volume {data.current_volume} > {cfg.trend_fad_ratio}× 24mo median "
            f"{data.volume_24mo_median}",
            "current_volume/volume_24mo_median",
            "Volume spike consistent with a fad.",
            cfg,
        )
    return _CLEAR


def _rule_seasonality(data: RiskInput, cfg: RiskConfig) -> _RuleResult:
    season = data.seasonality
    if season is None:
        return _UNASSESSED
    if not season.assessable:
        return _flag(
            "seasonality_unknown",
            cfg.deduct_seasonality_unknown,
            "peak concentration unassessable (< 12 months of history)",
            "demand.seasonality",
            "Seasonality could not be confirmed — smaller uncertainty penalty.",
            cfg,
        )
    if (
        season.peak_concentration is not None
        and season.peak_concentration > cfg.seasonality_peak_threshold
    ):
        return _flag(
            "seasonality_confirmed",
            cfg.deduct_seasonality_confirmed,
            f"peak-8-week concentration {season.peak_concentration:.0%} > "
            f"{cfg.seasonality_peak_threshold:.0%}",
            "demand.seasonality",
            "Demand is concentrated in a short season.",
            cfg,
        )
    return _CLEAR


def _rule_high_returns(data: RiskInput, rules: RiskRules, cfg: RiskConfig) -> _RuleResult:
    if data.sizing_complaint_frequency is None and data.category is None:
        return _UNASSESSED
    if (
        data.sizing_complaint_frequency is not None
        and data.sizing_complaint_frequency > cfg.sizing_freq_threshold
    ):
        return _flag(
            "high_returns",
            cfg.deduct_high_returns,
            f"sizing/fit complaints {data.sizing_complaint_frequency:.0%} > "
            f"{cfg.sizing_freq_threshold:.0%}",
            "review_themes.sizing",
            "Sizing/fit issues drive returns.",
            cfg,
        )
    if data.category is not None and data.category in rules.high_return_categories:
        return _flag(
            "high_returns",
            cfg.deduct_high_returns,
            f"category {data.category!r} on the high-return list",
            "risk_rules.high_return_categories",
            "Structurally high-return category.",
            cfg,
        )
    return _CLEAR


def _rule_fragility(data: RiskInput, rules: RiskRules, cfg: RiskConfig) -> _RuleResult:
    if data.damage_complaint_frequency is None and not data.materials:
        return _UNASSESSED
    if (
        data.damage_complaint_frequency is not None
        and data.damage_complaint_frequency > cfg.damage_freq_threshold
    ):
        return _flag(
            "fragility",
            cfg.deduct_fragility,
            f"damage complaints {data.damage_complaint_frequency:.0%} > "
            f"{cfg.damage_freq_threshold:.0%}",
            "review_themes.damage",
            "Breakage/damage complaints indicate fragility.",
            cfg,
        )
    for material in data.materials:
        if material.lower() in rules.fragility_materials:
            return _flag(
                "fragility",
                cfg.deduct_fragility,
                f"fragile material {material!r}",
                "risk_rules.fragility_materials",
                "Fragile material raises breakage risk.",
                cfg,
            )
    return _CLEAR


def _rule_keyword_concentration(data: RiskInput, cfg: RiskConfig) -> _RuleResult:
    if data.keyword_top_share is None:
        return _UNASSESSED
    if data.keyword_top_share > cfg.keyword_share_threshold:
        return _flag(
            "keyword_concentration",
            cfg.deduct_keyword_concentration,
            f"top keyword holds {data.keyword_top_share:.0%} of cluster volume > "
            f"{cfg.keyword_share_threshold:.0%}",
            "keyword_cluster.top_share",
            "Demand depends on a single keyword.",
            cfg,
        )
    return _CLEAR


def _rule_market_concentration(data: RiskInput, cfg: RiskConfig) -> _RuleResult:
    if data.brand_hhi is None:
        return _UNASSESSED
    if data.brand_hhi > cfg.hhi_threshold:
        return _flag(
            "market_concentration",
            cfg.deduct_market_concentration,
            f"brand HHI {data.brand_hhi:.2f} > {cfg.hhi_threshold:.2f}",
            "competition.hhi",
            "A few brands dominate the market.",
            cfg,
        )
    return _CLEAR


def _rule_supplier(data: RiskInput, cfg: RiskConfig) -> _RuleResult:
    if data.has_firmware is None and data.multi_part is None:
        return _UNASSESSED
    if data.has_firmware is True:
        return _flag(
            "supplier_complexity",
            cfg.deduct_supplier,
            "product has firmware",
            "attributes.has_firmware",
            "Firmware raises supplier/QA complexity.",
            cfg,
        )
    if data.multi_part is True:
        return _flag(
            "supplier_complexity",
            cfg.deduct_supplier,
            "multi-part assembly",
            "attributes.multi_part",
            "Multi-part assembly raises supplier complexity.",
            cfg,
        )
    return _CLEAR


# ---------------------------------------------------------------------------
# Informational flags (0 deduction — owned by other pillars/kill rules)
# ---------------------------------------------------------------------------
def _informational(data: RiskInput, rules: RiskRules) -> list[RiskFlag]:
    out: list[RiskFlag] = []
    if data.price_war_flag is True:
        out.append(
            RiskFlag(
                "price_war",
                0.0,
                RiskSeverity.INFO,
                "competition.price_war_flag = true",
                "competition.price_war_flag",
                "Price war already priced into the competition pillar — not deducted here.",
            )
        )
    oversized = data.oversized is True or (
        data.size_tier is not None and data.size_tier in rules.oversized_size_tiers
    )
    if oversized:
        out.append(
            RiskFlag(
                "oversized_logistics",
                0.0,
                RiskSeverity.INFO,
                f"size tier {data.size_tier!r}" if data.size_tier else "flagged oversized",
                "product.size_tier",
                "Oversized handling is governed by kill rule K3 and the fee engine — "
                "not a risk deduction.",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Top-level engine
# ---------------------------------------------------------------------------
# Documented rule names, in ledger order (seasonality is one rule, two outcomes).
_RULE_NAMES = (
    "ip_signal",
    "compliance",
    "trend_dependency",
    "seasonality",
    "high_returns",
    "fragility",
    "keyword_concentration",
    "market_concentration",
    "supplier_complexity",
)


def analyze_risk(data: RiskInput, rules: RiskRules, config: RiskConfig | None = None) -> RiskReport:
    """Apply the documented deduction ledger and return the 0-100 risk score."""
    cfg = config or RiskConfig()

    results: dict[str, _RuleResult] = {
        "ip_signal": _rule_ip(data, rules, cfg),
        "compliance": _rule_compliance(data, rules, cfg),
        "trend_dependency": _rule_trend(data, cfg),
        "seasonality": _rule_seasonality(data, cfg),
        "high_returns": _rule_high_returns(data, rules, cfg),
        "fragility": _rule_fragility(data, rules, cfg),
        "keyword_concentration": _rule_keyword_concentration(data, cfg),
        "market_concentration": _rule_market_concentration(data, cfg),
        "supplier_complexity": _rule_supplier(data, cfg),
    }

    triggered = [r.flag for r in results.values() if r.flag is not None]
    unassessed = tuple(name for name in _RULE_NAMES if not results[name].assessed)
    informational = _informational(data, rules)

    total_deduction = sum(f.deduction for f in triggered)
    risk_score = max(0.0, 100.0 - total_deduction)

    # Order flags by deduction (largest risk first), informational last.
    flags = tuple(sorted(triggered, key=lambda f: -f.deduction)) + tuple(informational)

    unassessed_count = len(unassessed)
    if unassessed_count <= cfg.max_unassessed_high:
        level = Confidence.HIGH
    elif unassessed_count <= cfg.max_unassessed_medium:
        level = Confidence.MEDIUM
    else:
        level = Confidence.LOW

    return RiskReport(
        risk_score=risk_score,
        total_deduction=total_deduction,
        confidence=RiskConfidence(
            level=level,
            assessed_rules=len(_RULE_NAMES) - unassessed_count,
            unassessed_rules=unassessed_count,
            total_rules=len(_RULE_NAMES),
        ),
        flags=flags,
        unassessed=unassessed,
        data_gaps=unassessed,
    )
