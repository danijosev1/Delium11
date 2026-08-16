"""Factories that build minimal-but-valid pillar reports for scoring tests.

Every factory defaults to *sufficient, no-kill, no-gate-failure* data so a test
can flip exactly one dimension and observe the effect in isolation.
"""

from __future__ import annotations

from datetime import date

from delium.analysis.models import (
    BeatableSlots,
    BrandConcentration,
    BsrTrend,
    CompetitionConfidence,
    CompetitionReport,
    Confidence,
    DemandReport,
    DifferentiationConfidence,
    DifferentiationReport,
    FeeBreakdown,
    KeywordDemand,
    ListingQualityAdvantage,
    PriceCompetition,
    ProfitInputs,
    ProfitResult,
    ReviewMoat,
    ReviewVelocity,
    RiskConfidence,
    RiskReport,
    SalesEstimate,
    ScenarioSet,
    Seasonality,
    Subscore,
)


def demand_report(
    pillar_score: float = 70.0,
    *,
    confidence: Confidence = Confidence.HIGH,
    keepa_asins_with_history: int = 8,
    volumed_phrases: int = 6,
) -> DemandReport:
    estimates = tuple(
        SalesEstimate(
            asin=f"A{i}",
            low_units=100,
            expected_units=200,
            high_units=300,
            confidence=Confidence.HIGH,
            method="rank_drop",
            observed_days=90,
            n_observations=90,
            drops=40,
            current_bsr=5000,
            rank_reference_units=200,
        )
        for i in range(keepa_asins_with_history)
    )
    return DemandReport(
        pillar_score=pillar_score,
        confidence=confidence,
        components=(Subscore("search_volume", pillar_score, 30, "vol"),),
        sales_estimates=estimates,
        market_units_low=1000,
        market_units_expected=2000,
        market_units_high=3000,
        keyword_demand=KeywordDemand(
            total_volume=9000,
            deduplicated_volume=8000,
            primary_phrase="thing",
            primary_volume=4000,
            demand_concentration=0.44,
            volumed_phrase_count=volumed_phrases,
            yoy_growth=0.08,
            search_volume_score=pillar_score,
            market_growth_score=50.0,
        ),
        bsr_trend=BsrTrend("flat", 0.0, 0.0, 55.0, 8),
        seasonality=Seasonality(True, 0.2, False, 90.0, 52),
    )


def competition_report(
    pillar_score: float = 65.0,
    *,
    confidence: Confidence = Confidence.HIGH,
    median_reviews: float = 400.0,
    top_brand_slot_share: float = 0.2,
    top_brand: str = "Acme",
    competitors_analyzed: int = 10,
    avg_competitor_quality: float | None = 55.0,
    price_history_coverage: float = 0.8,
    price_war_flag: bool = False,
) -> CompetitionReport:
    return CompetitionReport(
        pillar_score=pillar_score,
        confidence=CompetitionConfidence(
            level=confidence,
            competitors_analyzed=competitors_analyzed,
            review_history_coverage=0.9,
            listing_quality_coverage=0.9,
            price_history_coverage=price_history_coverage,
        ),
        review_moat=ReviewMoat(median_reviews, median_reviews, 900, 60.0, "moat"),
        beatable_slots=BeatableSlots(2, 1, 50.0, "slots"),
        review_velocity=ReviewVelocity(40.0, "moderate", 3, 60.0, "vel"),
        brand_concentration=BrandConcentration(
            top_brand, top_brand_slot_share, 0.15, False, False, 70.0, "brand"
        ),
        listing_quality_advantage=ListingQualityAdvantage(
            avg_competitor_quality, 60.0, avg_competitor_quality is not None, 10, "adv"
        ),
        price_competition=PriceCompetition(2200, 500, 0.1, 0.1, 70.0, price_war_flag, 0, "price"),
        components=(Subscore("review_moat", pillar_score, 30, "moat"),),
        data_gaps=(),
    )


def differentiation_report(
    pillar_score: float = 55.0,
    *,
    confidence: Confidence = Confidence.HIGH,
    sample_size: int = 200,
) -> DifferentiationReport:
    return DifferentiationReport(
        pillar_score=pillar_score,
        confidence=DifferentiationConfidence(
            level=confidence,
            sample_size=sample_size,
            verified_theme_ratio=0.9,
            themes_with_evidence=3,
            feature_evidence=True,
            sample_bias_flagged=False,
        ),
        complaint_intensity_score=pillar_score,
        missing_features_score=50.0,
        addressability_score=70.0,
        bundle_packaging_score=50.0,
        themes=(),
        feature_gap_count=2,
        verified_supporting_reviews=40,
        sample_rating_avg=4.0,
        sample_bias_delta=0.1,
        sample_bias_flag=False,
        f1_bias_adjustment=0.0,
        components=(Subscore("complaint_intensity", pillar_score, 40, "intensity"),),
        data_gaps=(),
    )


def risk_report(
    risk_score: float = 90.0,
    *,
    confidence: Confidence = Confidence.HIGH,
    deductions: tuple[tuple[str, float], ...] = (),
) -> RiskReport:
    from delium.analysis.models import RiskFlag, RiskSeverity

    flags = tuple(
        RiskFlag(
            risk_type=name,
            deduction=ded,
            severity=RiskSeverity.HIGH,
            evidence=f"{name} evidence",
            source="test",
            explanation="test",
        )
        for name, ded in deductions
    )
    return RiskReport(
        risk_score=risk_score,
        total_deduction=100.0 - risk_score,
        confidence=RiskConfidence(confidence, 9, 0, 9),
        flags=flags,
        unassessed=(),
        data_gaps=(),
    )


def _profit_result(
    *,
    net_margin: float,
    roi: float,
    payback_months: float | None,
    launch_capital_cents: int,
    confidence: Confidence = Confidence.HIGH,
) -> ProfitResult:
    fees = FeeBreakdown(
        referral_cents=300,
        fulfillment_cents=400,
        closing_cents=0,
        storage_monthly_cents=20,
        storage_peak_cents=40,
        prep_cents=30,
        size_tier="large_standard",
        fee_table_version="test",
    )
    inputs = ProfitInputs(
        selling_price_cents=2200,
        product_cost_cents=550,
        freight_cents=90,
        customs_cents=30,
        prep_cost_cents=30,
        ppc_percent=0.15,
        return_rate=0.04,
        monthly_sales_units=200,
        estimated_fields=frozenset(),
    )
    return ProfitResult(
        revenue_cents=2200,
        landed_cost_cents=700,
        amazon_fees_cents=720,
        ppc_cost_cents=330,
        returns_cost_cents=40,
        gross_profit_cents=1500,
        contribution_margin_cents=740,
        net_profit_cents=410,
        gross_margin=0.68,
        net_margin=net_margin,
        roi=roi,
        break_even_ppc=0.3,
        monthly_revenue_cents=440000,
        monthly_net_profit_cents=82000,
        monthly_cash_requirement_cents=200000,
        launch_capital_cents=launch_capital_cents,
        payback_months=payback_months,
        confidence=confidence,
        assumption_flags=(),
        fees=fees,
        inputs=inputs,
    )


def scenario_set(
    *,
    stressed_margin: float = 0.35,
    stressed_roi: float = 2.0,
    stressed_payback: float | None = 3.5,
    stressed_capital_cents: int = 800_000,
    expected_margin: float = 0.40,
    expected_roi: float = 2.5,
    expected_payback: float | None = 3.0,
    confidence: Confidence = Confidence.HIGH,
) -> ScenarioSet:
    expected = _profit_result(
        net_margin=expected_margin,
        roi=expected_roi,
        payback_months=expected_payback,
        launch_capital_cents=stressed_capital_cents,
        confidence=confidence,
    )
    stressed = _profit_result(
        net_margin=stressed_margin,
        roi=stressed_roi,
        payback_months=stressed_payback,
        launch_capital_cents=stressed_capital_cents,
        confidence=confidence,
    )
    return ScenarioSet(
        optimistic=expected,
        expected=expected,
        stressed=stressed,
        worst_case=stressed,
        confidence=confidence,
    )


def _dt() -> date:
    return date(2026, 1, 1)
