"""Risk engine: every documented deduction, thresholds, and gates."""

from __future__ import annotations

import pytest

from delium.analysis.models import (
    Confidence,
    RiskConfig,
    RiskInput,
    RiskSeverity,
    Seasonality,
)
from delium.analysis.risk import RiskError, analyze_risk, load_risk_rules

RULES = load_risk_rules()
CFG = RiskConfig()


def _run(**kw: object):  # type: ignore[no-untyped-def]
    return analyze_risk(RiskInput(**kw), RULES)  # type: ignore[arg-type]


def _flag(report, risk_type: str):  # type: ignore[no-untyped-def]
    return next((f for f in report.flags if f.risk_type == risk_type), None)


# --- loading --------------------------------------------------------------
def test_rules_load() -> None:
    assert RULES.version == "us-2026"
    assert "Toys & Games" in RULES.ip_categories
    assert "glass" in RULES.fragility_materials


def test_missing_rules_file_raises() -> None:
    with pytest.raises(RiskError):
        load_risk_rules("does-not-exist")


# --- 1. IP risk -----------------------------------------------------------
def test_ip_from_category() -> None:
    f = _flag(_run(category="Toys & Games"), "ip_signal")
    assert f is not None and f.deduction == 40 and f.severity is RiskSeverity.CRITICAL
    assert "design-patent watchlist" in f.evidence


def test_ip_from_patent_marked() -> None:
    f = _flag(_run(category="Home & Kitchen", patent_marked_listings=True), "ip_signal")
    assert f is not None and f.deduction == 40


def test_ip_from_brand_likeness_title() -> None:
    f = _flag(_run(category="Home & Kitchen", titles=("Marvel Spider-Man Mug",)), "ip_signal")
    assert f is not None
    assert "marvel" in f.evidence.lower()


def test_ip_clear_when_assessed_and_safe() -> None:
    r = _run(
        category="Home & Kitchen", patent_marked_listings=False, titles=("Plain Silicone Tray",)
    )
    assert _flag(r, "ip_signal") is None
    assert "ip_signal" not in r.unassessed


def test_ip_unassessed_when_no_inputs() -> None:
    r = _run(brand_hhi=0.1)  # nothing IP-related
    assert "ip_signal" in r.unassessed


# --- 2. Compliance risk ---------------------------------------------------
def test_compliance_triggers() -> None:
    f = _flag(_run(category="Baby"), "compliance")
    assert f is not None and f.deduction == 30 and f.severity is RiskSeverity.CRITICAL
    assert "CPSIA" in f.evidence


def test_compliance_clear_and_unassessed() -> None:
    assert _flag(_run(category="Home & Kitchen"), "compliance") is None
    assert "compliance" in _run(brand_hhi=0.1).unassessed


# --- 3. Fad/trend risk ----------------------------------------------------
def test_trend_short_history() -> None:
    f = _flag(_run(volume_history_months=12), "trend_dependency")
    assert f is not None and f.deduction == 25


def test_trend_fad_spike() -> None:
    f = _flag(
        _run(volume_history_months=36, current_volume=5000, volume_24mo_median=2000),
        "trend_dependency",
    )
    assert f is not None  # 5000 > 2× 2000


def test_trend_clear_and_unassessed() -> None:
    assert (
        _flag(
            _run(volume_history_months=36, current_volume=2100, volume_24mo_median=2000),
            "trend_dependency",
        )
        is None
    )
    assert "trend_dependency" in _run(brand_hhi=0.1).unassessed


# --- 4/5. Seasonality (confirmed vs unknown) ------------------------------
def test_seasonality_confirmed() -> None:
    f = _flag(_run(seasonality=Seasonality(True, 0.55, True, 10.0, 52)), "seasonality_confirmed")
    assert f is not None and f.deduction == 20


def test_seasonality_unknown_is_smaller() -> None:
    f = _flag(_run(seasonality=Seasonality(False, None, None, None, 8)), "seasonality_unknown")
    assert f is not None and f.deduction == 10  # strictly less than confirmed 20


def test_seasonality_clear_when_low_concentration() -> None:
    r = _run(seasonality=Seasonality(True, 0.20, False, 90.0, 52))
    assert _flag(r, "seasonality_confirmed") is None
    assert _flag(r, "seasonality_unknown") is None


def test_seasonality_unassessed_when_absent() -> None:
    assert "seasonality" in _run(brand_hhi=0.1).unassessed


# --- 6. Oversized / logistics (informational) ----------------------------
def test_oversized_is_informational_zero_deduction() -> None:
    r = _run(size_tier="large_bulky")
    f = _flag(r, "oversized_logistics")
    assert f is not None
    assert f.deduction == 0.0
    assert f.severity is RiskSeverity.INFO
    assert r.total_deduction == 0.0  # does not affect the score


# --- 7. Brand concentration ----------------------------------------------
def test_market_concentration() -> None:
    f = _flag(_run(brand_hhi=0.40), "market_concentration")
    assert f is not None and f.deduction == 15
    assert "0.40" in f.evidence


def test_market_concentration_clear() -> None:
    assert _flag(_run(brand_hhi=0.20), "market_concentration") is None


# --- 8. Price-war (informational, not double-counted) --------------------
def test_price_war_is_informational() -> None:
    r = _run(price_war_flag=True)
    f = _flag(r, "price_war")
    assert f is not None and f.deduction == 0.0
    assert "not deducted" in f.explanation
    assert r.total_deduction == 0.0


# --- 9. Data-quality / uncertainty ---------------------------------------
def test_unknown_inputs_do_not_deduct_except_seasonality() -> None:
    # Everything missing → all documented rules unassessed, no deductions.
    r = analyze_risk(RiskInput(), RULES)
    assert r.total_deduction == 0.0
    assert r.risk_score == 100.0
    assert len(r.unassessed) == 9
    assert r.confidence.level == Confidence.LOW


def test_high_returns_and_fragility() -> None:
    r = _run(sizing_complaint_frequency=0.15, damage_complaint_frequency=0.10)
    assert _flag(r, "high_returns").deduction == 20  # type: ignore[union-attr]
    assert _flag(r, "fragility").deduction == 20  # type: ignore[union-attr]


def test_high_returns_from_category() -> None:
    f = _flag(_run(category="Watches"), "high_returns")
    assert f is not None and f.deduction == 20
    assert "high-return list" in f.evidence


def test_fragility_from_material() -> None:
    f = _flag(_run(materials=("Glass",)), "fragility")
    assert f is not None and "glass" in f.evidence.lower()


def test_keyword_concentration() -> None:
    f = _flag(_run(keyword_top_share=0.70), "keyword_concentration")
    assert f is not None and f.deduction == 15


def test_supplier_complexity() -> None:
    assert _flag(_run(has_firmware=True), "supplier_complexity").deduction == 10  # type: ignore[union-attr]
    assert _flag(_run(multi_part=True), "supplier_complexity").deduction == 10  # type: ignore[union-attr]


# --- 10. Multiple simultaneous risks -------------------------------------
def test_multiple_risks_sum_and_floor() -> None:
    r = _run(
        category="Toys & Games",  # ip 40 + compliance 30
        materials=("glass",),  # fragility 20
        has_firmware=True,  # supplier 10
        keyword_top_share=0.70,  # keyword 15
        brand_hhi=0.40,  # market 15
        seasonality=Seasonality(True, 0.55, True, 10.0, 52),  # 20
        volume_history_months=12,  # trend 25
        sizing_complaint_frequency=0.15,  # high_returns 20
    )
    assert r.total_deduction == 195
    assert r.risk_score == 0.0  # floored
    assert r.has_critical_risk is True


# --- 11. No-risk case -----------------------------------------------------
def test_no_risk_full_score() -> None:
    r = _run(
        category="Home & Kitchen",
        patent_marked_listings=False,
        materials=("silicone",),
        has_firmware=False,
        multi_part=False,
        sizing_complaint_frequency=0.02,
        damage_complaint_frequency=0.01,
        keyword_top_share=0.30,
        brand_hhi=0.15,
        seasonality=Seasonality(True, 0.20, False, 90.0, 40),
        volume_history_months=36,
        current_volume=9000,
        volume_24mo_median=8000,
    )
    assert r.risk_score == 100.0
    assert r.total_deduction == 0.0
    assert r.confidence.level == Confidence.HIGH
    assert r.unassessed == ()


# --- 13. evidence on every deduction -------------------------------------
def test_every_deduction_has_evidence_and_source() -> None:
    r = _run(
        category="Baby",
        materials=("ceramic",),
        keyword_top_share=0.80,
        seasonality=Seasonality(True, 0.60, True, 5.0, 52),
    )
    for f in r.flags:
        if f.deduction > 0:
            assert f.evidence.strip()
            assert f.source.strip()
            assert f.explanation.strip()


# --- 14. score / deduction boundaries ------------------------------------
def test_score_bounded_and_deductions_nonnegative() -> None:
    r = _run(category="Toys & Games", brand_hhi=0.9, keyword_top_share=0.99)
    assert 0.0 <= r.risk_score <= 100.0
    assert r.total_deduction >= 0.0
    for f in r.flags:
        assert f.deduction >= 0.0


# --- 15. severity mapping -------------------------------------------------
def test_severity_bands() -> None:
    assert _flag(_run(category="Toys & Games"), "ip_signal").severity is RiskSeverity.CRITICAL  # type: ignore[union-attr]
    assert _flag(_run(category="Baby"), "compliance").severity is RiskSeverity.CRITICAL  # type: ignore[union-attr]
    assert _flag(_run(volume_history_months=6), "trend_dependency").severity is RiskSeverity.HIGH  # type: ignore[union-attr]
    assert _flag(_run(brand_hhi=0.5), "market_concentration").severity is RiskSeverity.MODERATE  # type: ignore[union-attr]


# --- confidence -----------------------------------------------------------
def test_confidence_degrades_with_unassessed() -> None:
    # One unassessed rule (supplier) → still HIGH (≤1).
    high = _run(
        category="Home & Kitchen",
        materials=("silicone",),
        keyword_top_share=0.3,
        brand_hhi=0.1,
        seasonality=Seasonality(True, 0.2, False, 90.0, 40),
        volume_history_months=36,
        sizing_complaint_frequency=0.02,
        patent_marked_listings=False,
    )
    assert high.confidence.level == Confidence.HIGH
    assert high.confidence.unassessed_rules <= 1


def test_ordering_by_deduction() -> None:
    r = _run(category="Toys & Games", brand_hhi=0.4)  # ip 40, compliance 30, market 15
    deducted = [f for f in r.flags if f.deduction > 0]
    assert [f.deduction for f in deducted] == sorted((f.deduction for f in deducted), reverse=True)
