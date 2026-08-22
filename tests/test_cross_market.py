"""Cross-market discovery engine: matching, source/target qualification, gap,
transferability, score, verdicts, directionality, and false-positive safeguards.
"""

from __future__ import annotations

import ast
from pathlib import Path

from delium.analysis.cross_market import (
    analyze_cross_market,
    analyze_cross_markets,
    assess_source,
    assess_target,
    assess_transferability,
    match_products,
)
from delium.analysis.models import (
    Confidence,
    CrossMarketVerdict,
    Dimensions,
    Marketplace,
    MarketplaceProduct,
    MatchConfidence,
    SourceMarketInput,
    SourceMaturity,
    TargetMarketInput,
    TargetPresence,
    TransferabilityInput,
    TransferabilityLevel,
)
from delium.config.models import DeliumConfig

CFG = DeliumConfig()
US, CA, UK, AU, IN = (
    Marketplace.US,
    Marketplace.CA,
    Marketplace.UK,
    Marketplace.AU,
    Marketplace.IN,
)
_TITLE = "silicone baby food freezer tray with lid"


def _prod(mp: Marketplace, **kw: object) -> MarketplaceProduct:
    base: dict[str, object] = {"asin": f"ASIN-{mp.value}", "title": _TITLE}
    base.update(kw)
    return MarketplaceProduct(marketplace=mp, **base)  # type: ignore[arg-type]


def _strong_source() -> SourceMarketInput:
    return SourceMarketInput(
        monthly_units=1500,
        keyword_volume=30000,
        keyword_growth=0.20,
        history_months=30,
        review_count=1200,
        competition_score=55,
        opportunity_score=72,
    )


def _good_target() -> TargetMarketInput:
    return TargetMarketInput(
        listings_found=2,
        median_reviews=80,
        avg_listing_quality=45,
        beatable_slots=3,
        brand_hhi=0.15,
        keyword_volume=6000,
        keyword_growth=0.25,
        serp_presence=True,
    )


def _favorable_transfer() -> TransferabilityInput:
    return TransferabilityInput(
        category_compatible=True,
        oversized=False,
        price_positioning_ok=True,
        compliance_risk=False,
        seasonality_concentration=0.20,
    )


def _run(
    *,
    source_mp: Marketplace = US,
    target_mp: Marketplace = AU,
    source: SourceMarketInput | None = None,
    target: TargetMarketInput | None = None,
    transfer: TransferabilityInput | None = None,
    source_product: MarketplaceProduct | None = None,
    target_product: MarketplaceProduct | None = None,
):  # type: ignore[no-untyped-def]
    return analyze_cross_market(
        source_product=source_product or _prod(source_mp, gtin="0012345678905"),
        target_product=target_product or _prod(target_mp, gtin="0012345678905"),
        source_input=source or _strong_source(),
        target_input=target or _good_target(),
        transfer_input=transfer or _favorable_transfer(),
        config=CFG,
    )


# =========================================================================
# Product matching
# =========================================================================
def test_match_exact_gtin() -> None:
    m = match_products(_prod(US, gtin="0012345678905"), _prod(AU, gtin="12345678905"), CFG)
    assert m.confidence is MatchConfidence.EXACT
    assert m.score == 1.0
    assert "gtin" in m.signals_used


def test_match_gtin_conflict_caps_weak() -> None:
    m = match_products(_prod(US, gtin="0012345678905"), _prod(AU, gtin="9990000000001"), CFG)
    assert m.confidence is MatchConfidence.WEAK
    assert "gtin" in m.conflicting_signals


def test_match_strong_title_and_brand() -> None:
    s = _prod(US, brand="Acme", dims=Dimensions(200, 150, 50), category_path="Baby > Feeding")
    t = _prod(AU, brand="Acme", dims=Dimensions(205, 148, 52), category_path="Baby > Feeding")
    m = match_products(s, t, CFG)
    assert m.confidence in (MatchConfidence.STRONG, MatchConfidence.PROBABLE)
    assert "title" in m.signals_used and "brand" in m.signals_used


def test_match_brand_mismatch_is_conflict() -> None:
    s = _prod(US, brand="Acme")
    t = _prod(AU, brand="Zenith")
    m = match_products(s, t, CFG)
    assert "brand" in m.conflicting_signals


def test_match_generic_brand_mismatch_tolerated() -> None:
    s = _prod(US, brand="Acme", generic=True)
    t = _prod(AU, brand="Zenith", generic=True)
    m = match_products(s, t, CFG)
    assert "brand" not in m.conflicting_signals


def test_match_conflicting_dimensions_not_exact() -> None:
    # Superficial title match but very different dimensions → capped at PROBABLE.
    s = _prod(US, brand="Acme", dims=Dimensions(200, 150, 50))
    t = _prod(AU, brand="Acme", dims=Dimensions(600, 400, 300))
    m = match_products(s, t, CFG)
    assert m.confidence is not MatchConfidence.EXACT
    assert m.confidence in (
        MatchConfidence.PROBABLE,
        MatchConfidence.WEAK,
        MatchConfidence.UNMATCHED,
    )
    assert "dims" in m.conflicting_signals


def test_match_conflicting_weight_caps_confidence() -> None:
    s = _prod(US, brand="Acme", weight_g=300)
    t = _prod(AU, brand="Acme", weight_g=3000)
    m = match_products(s, t, CFG)
    assert "weight" in m.conflicting_signals


def test_match_no_signals_unmatched() -> None:
    s = MarketplaceProduct(marketplace=US, asin="A")
    t = MarketplaceProduct(marketplace=AU, asin="B")
    m = match_products(s, t, CFG)
    assert m.confidence is MatchConfidence.UNMATCHED
    assert not m.matched


def test_match_deterministic() -> None:
    s, t = _prod(US, brand="Acme"), _prod(AU, brand="Acme")
    assert match_products(s, t, CFG) == match_products(s, t, CFG)


# =========================================================================
# Source market
# =========================================================================
def test_source_strong() -> None:
    e = assess_source(US, _strong_source(), CFG)
    assert e.maturity in (SourceMaturity.STRONG, SourceMaturity.EXCEPTIONAL)
    assert e.confidence is Confidence.HIGH


def test_source_emerging() -> None:
    e = assess_source(
        US,
        SourceMarketInput(monthly_units=160, keyword_volume=2200, keyword_growth=-0.05),
        CFG,
    )
    assert e.maturity is SourceMaturity.EMERGING


def test_source_validated_band() -> None:
    e = assess_source(
        US,
        SourceMarketInput(
            monthly_units=500, keyword_volume=9000, keyword_growth=0.05, history_months=18
        ),
        CFG,
    )
    assert e.maturity in (SourceMaturity.VALIDATED, SourceMaturity.STRONG)


def test_source_insufficient_when_too_few_signals() -> None:
    e = assess_source(US, SourceMarketInput(monthly_units=1500), CFG)
    assert e.maturity is SourceMaturity.INSUFFICIENT
    assert e.confidence is Confidence.LOW


def test_source_exceptional() -> None:
    e = assess_source(
        US,
        SourceMarketInput(
            monthly_units=5000,
            keyword_volume=40000,
            keyword_growth=0.4,
            history_months=36,
            review_count=3000,
        ),
        CFG,
    )
    assert e.maturity is SourceMaturity.EXCEPTIONAL


# =========================================================================
# Target market
# =========================================================================
def test_target_not_present_no_demand() -> None:
    e = assess_target(
        IN, TargetMarketInput(listings_found=0, keyword_volume=0, serp_presence=False), CFG
    )
    assert e.presence is TargetPresence.NOT_PRESENT
    assert not e.demand_credible


def test_target_not_present_with_strong_demand() -> None:
    e = assess_target(
        AU,
        TargetMarketInput(
            listings_found=0, keyword_volume=8000, keyword_growth=0.3, serp_presence=True
        ),
        CFG,
    )
    assert e.presence is TargetPresence.NOT_PRESENT
    assert e.demand_credible  # demand exists even though nobody is selling


def test_target_underpenetrated() -> None:
    e = assess_target(
        AU,
        TargetMarketInput(
            listings_found=3,
            median_reviews=60,
            avg_listing_quality=40,
            beatable_slots=4,
            brand_hhi=0.1,
            keyword_volume=7000,
            serp_presence=True,
        ),
        CFG,
    )
    assert e.presence is TargetPresence.UNDERPENETRATED
    assert e.competition_weakness_score >= 55


def test_target_mature() -> None:
    e = assess_target(
        UK,
        TargetMarketInput(
            listings_found=10,
            median_reviews=900,
            avg_listing_quality=80,
            keyword_volume=9000,
            serp_presence=True,
        ),
        CFG,
    )
    assert e.presence is TargetPresence.MATURE


def test_target_saturated() -> None:
    e = assess_target(
        UK,
        TargetMarketInput(
            listings_found=12,
            median_reviews=2000,
            avg_listing_quality=88,
            keyword_volume=25000,
            serp_presence=True,
        ),
        CFG,
    )
    assert e.presence is TargetPresence.SATURATED


def test_target_unknown_when_not_looked_up() -> None:
    e = assess_target(AU, TargetMarketInput(), CFG)
    assert e.presence is TargetPresence.UNKNOWN
    assert e.confidence is Confidence.LOW


# =========================================================================
# Market gap
# =========================================================================
def test_large_maturity_gap() -> None:
    r = _run(target=TargetMarketInput(listings_found=0, keyword_volume=6000, serp_presence=True))
    assert r.market_gap.maturity_gap > 50


def test_small_gap_when_target_established() -> None:
    r = _run(
        target=TargetMarketInput(
            listings_found=12,
            median_reviews=1600,
            avg_listing_quality=85,
            keyword_volume=20000,
            serp_presence=True,
        )
    )
    assert r.market_gap.maturity_gap < 40


def test_competition_gap_reflects_relative_weakness() -> None:
    weak_target = _run(
        target=TargetMarketInput(
            listings_found=2,
            median_reviews=50,
            avg_listing_quality=30,
            beatable_slots=4,
            brand_hhi=0.1,
            keyword_volume=6000,
            serp_presence=True,
        )
    )
    strong_target = _run(
        target=TargetMarketInput(
            listings_found=8,
            median_reviews=1500,
            avg_listing_quality=90,
            beatable_slots=0,
            brand_hhi=0.6,
            keyword_volume=6000,
            serp_presence=True,
        )
    )
    assert weak_target.market_gap.competition_gap > strong_target.market_gap.competition_gap


# =========================================================================
# Transferability
# =========================================================================
def test_transfer_favorable() -> None:
    t = assess_transferability(US, AU, _favorable_transfer(), CFG)
    assert t.level is TransferabilityLevel.FAVORABLE


def test_transfer_unfavorable_on_compliance() -> None:
    t = assess_transferability(
        US,
        AU,
        TransferabilityInput(
            category_compatible=True,
            oversized=False,
            price_positioning_ok=True,
            compliance_risk=True,
            seasonality_concentration=0.2,
        ),
        CFG,
    )
    assert t.level is TransferabilityLevel.UNFAVORABLE


def test_transfer_uncertain_on_missing_data() -> None:
    t = assess_transferability(US, AU, TransferabilityInput(), CFG)
    assert t.level in (TransferabilityLevel.UNCERTAIN, TransferabilityLevel.UNFAVORABLE)


def test_transfer_localization_flags_units() -> None:
    # US (imperial) → AU (metric) raises a units localization flag.
    t = assess_transferability(US, AU, _favorable_transfer(), CFG)
    assert any(f.kind == "units" for f in t.localization_flags)


def test_transfer_surfaces_risk_flags_not_recomputed() -> None:
    t = assess_transferability(
        US,
        AU,
        TransferabilityInput(
            category_compatible=True,
            oversized=False,
            price_positioning_ok=True,
            compliance_risk=False,
            seasonality_concentration=0.2,
            surfaced_risk_flags=("compliance: CPSIA", "fragility"),
        ),
        CFG,
    )
    assert t.surfaced_risk_flags == ("compliance: CPSIA", "fragility")


# =========================================================================
# Cross-market score & verdicts
# =========================================================================
def test_strong_opportunity() -> None:
    r = _run()
    assert r.verdict is CrossMarketVerdict.STRONG_OPPORTUNITY
    assert 0.0 <= r.score <= 100.0
    assert r.confidence.level is not Confidence.LOW


def test_underpenetrated_empty_market_with_demand_is_strong() -> None:
    r = _run(
        target=TargetMarketInput(
            listings_found=0, keyword_volume=7000, keyword_growth=0.3, serp_presence=True
        )
    )
    assert r.verdict is CrossMarketVerdict.STRONG_OPPORTUNITY


def test_opportunity_to_validate_when_demand_unproven() -> None:
    r = _run(
        target=TargetMarketInput(
            listings_found=1, median_reviews=20, keyword_volume=0, serp_presence=False
        )
    )
    assert r.verdict is CrossMarketVerdict.OPPORTUNITY_TO_VALIDATE
    assert not r.target_evidence.demand_credible


def test_mature_market_verdict() -> None:
    r = _run(
        target=TargetMarketInput(
            listings_found=12,
            median_reviews=2000,
            avg_listing_quality=88,
            keyword_volume=25000,
            serp_presence=True,
        )
    )
    assert r.verdict is CrossMarketVerdict.MATURE_MARKET


def test_weak_transfer_on_unfavorable() -> None:
    r = _run(
        transfer=TransferabilityInput(
            category_compatible=False,
            oversized=True,
            price_positioning_ok=False,
            compliance_risk=False,
            seasonality_concentration=0.2,
        )
    )
    assert r.verdict is CrossMarketVerdict.WEAK_TRANSFER


def test_insufficient_when_unmatched() -> None:
    r = _run(
        source_product=MarketplaceProduct(marketplace=US, asin="A"),
        target_product=MarketplaceProduct(marketplace=AU, asin="B"),
    )
    assert r.verdict is CrossMarketVerdict.INSUFFICIENT_DATA


def test_insufficient_when_source_thin() -> None:
    r = _run(source=SourceMarketInput(monthly_units=1500))
    assert r.verdict is CrossMarketVerdict.INSUFFICIENT_DATA


def test_score_within_bounds_extreme() -> None:
    r = _run(
        source=SourceMarketInput(
            monthly_units=99999,
            keyword_volume=999999,
            keyword_growth=5.0,
            history_months=120,
            review_count=99999,
            competition_score=100,
        ),
        target=TargetMarketInput(
            listings_found=0, keyword_volume=999999, keyword_growth=5.0, serp_presence=True
        ),
    )
    assert 0.0 <= r.score <= 100.0


def test_components_sum_to_base_score() -> None:
    r = _run()
    total = sum(c.weighted_contribution for c in r.components)
    assert abs(total - r.base_score) < 1e-9


def test_components_expose_full_provenance() -> None:
    r = _run()
    names = {c.name for c in r.components}
    assert names == {
        "source_success",
        "target_demand",
        "competition_gap",
        "maturity_gap",
        "transferability",
    }
    for c in r.components:
        assert 0.0 <= c.normalized <= 100.0
        assert c.raw and c.evidence


# =========================================================================
# False-positive safeguards (brief §"cross-market false positive tests")
# =========================================================================
def test_fp_us_success_no_india_demand_not_strong() -> None:
    r = _run(
        target_mp=IN,
        target=TargetMarketInput(
            listings_found=2, median_reviews=30, keyword_volume=0, serp_presence=False
        ),
    )
    assert r.verdict is not CrossMarketVerdict.STRONG_OPPORTUNITY


def test_fp_no_listings_no_demand_not_strong() -> None:
    r = _run(
        target_mp=IN,
        target=TargetMarketInput(listings_found=0, keyword_volume=0, serp_presence=False),
    )
    assert r.verdict is CrossMarketVerdict.INSUFFICIENT_DATA
    assert r.verdict is not CrossMarketVerdict.STRONG_OPPORTUNITY


def test_fp_huge_demand_strong_incumbents_not_gap() -> None:
    r = _run(
        target=TargetMarketInput(
            listings_found=15,
            median_reviews=3000,
            avg_listing_quality=92,
            brand_hhi=0.5,
            keyword_volume=50000,
            serp_presence=True,
        )
    )
    assert r.verdict is CrossMarketVerdict.MATURE_MARKET
    assert r.verdict is not CrossMarketVerdict.STRONG_OPPORTUNITY


def test_fp_title_match_conflicting_dims_not_exact() -> None:
    r = _run(
        source_product=_prod(US, brand="Acme", dims=Dimensions(200, 150, 50)),
        target_product=_prod(AU, brand="Acme", dims=Dimensions(700, 500, 400)),
    )
    assert r.match.confidence is not MatchConfidence.EXACT


def test_fp_compliance_risk_downgrades() -> None:
    clean = _run()
    risky = _run(
        transfer=TransferabilityInput(
            category_compatible=True,
            oversized=False,
            price_positioning_ok=True,
            compliance_risk=True,
            seasonality_concentration=0.2,
        )
    )
    assert clean.verdict is CrossMarketVerdict.STRONG_OPPORTUNITY
    assert risky.verdict is not CrossMarketVerdict.STRONG_OPPORTUNITY
    assert risky.score < clean.score


def test_fp_poor_transferability_downgrades() -> None:
    r = _run(
        transfer=TransferabilityInput(
            category_compatible=False,
            oversized=False,
            price_positioning_ok=True,
            compliance_risk=False,
            seasonality_concentration=0.2,
        )
    )
    assert r.verdict is not CrossMarketVerdict.STRONG_OPPORTUNITY


# =========================================================================
# Directionality & multi-market
# =========================================================================
def test_direction_matters() -> None:
    # US strong → AU weak target is a very different question than AU → US.
    us_source = _strong_source()
    au_source = SourceMarketInput(
        monthly_units=180, keyword_volume=2500, keyword_growth=-0.05, history_months=8
    )
    us_as_target = TargetMarketInput(
        listings_found=15,
        median_reviews=2500,
        avg_listing_quality=90,
        keyword_volume=40000,
        serp_presence=True,
    )
    au_as_target = _good_target()

    forward = analyze_cross_market(
        source_product=_prod(US, gtin="0012345678905"),
        target_product=_prod(AU, gtin="0012345678905"),
        source_input=us_source,
        target_input=au_as_target,
        transfer_input=_favorable_transfer(),
        config=CFG,
    )
    reverse = analyze_cross_market(
        source_product=_prod(AU, gtin="0012345678905"),
        target_product=_prod(US, gtin="0012345678905"),
        source_input=au_source,
        target_input=us_as_target,
        transfer_input=_favorable_transfer(),
        config=CFG,
    )
    assert forward.verdict != reverse.verdict
    assert forward.score != reverse.score


def test_multi_market_fan_out() -> None:
    targets = tuple(
        (_prod(mp, gtin="0012345678905"), _good_target(), _favorable_transfer())
        for mp in (CA, UK, AU, IN)
    )
    reports = analyze_cross_markets(
        source_product=_prod(US, gtin="0012345678905"),
        source_input=_strong_source(),
        targets=targets,
        config=CFG,
    )
    assert len(reports) == 4
    assert [r.target_marketplace for r in reports] == [CA, UK, AU, IN]
    assert all(r.source_marketplace is US for r in reports)


def test_deterministic_report() -> None:
    assert _run() == _run()


# =========================================================================
# Config-driven & purity
# =========================================================================
def test_weights_come_from_config_snapshot() -> None:
    r = _run()
    snap = dict(r.weights_snapshot)
    assert snap["source_success"] == CFG.cross_market.w_source_success
    assert snap["target_demand"] == CFG.cross_market.w_target_demand


def test_custom_config_changes_verdict() -> None:
    strict = DeliumConfig.model_validate({"cross_market": {"strong_opportunity_min": 95}})
    r = analyze_cross_market(
        source_product=_prod(US, gtin="0012345678905"),
        target_product=_prod(AU, gtin="0012345678905"),
        source_input=_strong_source(),
        target_input=_good_target(),
        transfer_input=_favorable_transfer(),
        config=strict,
    )
    assert r.verdict is not CrossMarketVerdict.STRONG_OPPORTUNITY


def test_module_has_no_forbidden_imports() -> None:
    src = Path("src/delium/analysis/cross_market.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = (
        "datetime",
        "time",
        "random",
        "secrets",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "sqlite3",
    )
    for mod in imported:
        assert not mod.startswith(forbidden), f"forbidden import: {mod}"
        for banned in ("provider", "ingestion", "database", "agent", "llm", "keepa", "dataforseo"):
            assert banned not in mod, f"forbidden dependency: {mod}"


# =========================================================================
# Branch / defensive coverage
# =========================================================================
def test_match_titles_only_stopwords_score_zero() -> None:
    # Titles present but all tokens are stopwords → jaccard 0.0 branch.
    s = MarketplaceProduct(US, "A", title="the a for")
    t = MarketplaceProduct(AU, "B", title="the a for")
    m = match_products(s, t, CFG)
    assert m.confidence is MatchConfidence.UNMATCHED


def test_match_zero_dimensions_agree() -> None:
    # `_within` hi==0 branch (both zero on a dimension).
    s = _prod(US, brand="Acme", dims=Dimensions(0, 0, 0))
    t = _prod(AU, brand="Acme", dims=Dimensions(0, 0, 0))
    m = match_products(s, t, CFG)
    assert "dims" in m.signals_used


def test_match_low_title_and_category_similarity_not_counted() -> None:
    s = _prod(US, title="alpha beta gamma", category_path="Baby > Feeding")
    t = _prod(AU, title="delta epsilon zeta", category_path="Home > Kitchen")
    m = match_products(s, t, CFG)
    assert "title" not in m.signals_used
    assert "category" not in m.signals_used


def test_source_velocity_missing_branch() -> None:
    e = assess_source(
        US, SourceMarketInput(keyword_volume=30000, keyword_growth=0.2, history_months=24), CFG
    )
    assert any(c.name == "velocity" and not c.available for c in e.components)


def test_target_confidence_low_without_competition_signals() -> None:
    # Listings present but no competition fields at all → competition signals 0.
    e = assess_target(
        AU, TargetMarketInput(listings_found=3, keyword_volume=6000, serp_presence=True), CFG
    )
    assert e.confidence is Confidence.LOW


def test_transfer_seasonality_uncertain_branch() -> None:
    t = assess_transferability(
        US,
        AU,
        TransferabilityInput(
            category_compatible=True,
            oversized=False,
            price_positioning_ok=True,
            compliance_risk=False,
            seasonality_concentration=0.6,
        ),
        CFG,
    )
    assert t.level is TransferabilityLevel.UNCERTAIN
    assert any(
        f.name == "seasonality" and f.level is TransferabilityLevel.UNCERTAIN for f in t.factors
    )


def test_transfer_electrical_and_keyword_localization_flags() -> None:
    t = assess_transferability(
        US,
        AU,
        TransferabilityInput(
            category_compatible=True,
            oversized=False,
            price_positioning_ok=True,
            compliance_risk=False,
            seasonality_concentration=0.2,
            electrical_or_plug_dependent=True,
            keyword_localization_needed=True,
        ),
        CFG,
    )
    kinds = {f.kind for f in t.localization_flags}
    assert "electrical" in kinds and "keywords" in kinds


def test_transfer_same_unit_system_no_units_flag() -> None:
    # CA → UK are both metric → no units localization flag (unit_system False branch).
    t = assess_transferability(CA, UK, _favorable_transfer(), CFG)
    assert not any(f.kind == "units" for f in t.localization_flags)


def test_verdict_insufficient_when_target_unknown() -> None:
    r = _run(target=TargetMarketInput())  # listings None → UNKNOWN, no demand
    assert r.target_evidence.presence is TargetPresence.UNKNOWN
    assert r.verdict is CrossMarketVerdict.INSUFFICIENT_DATA


def test_summary_surfaces_risk_flags_and_thin_target_note() -> None:
    r = _run(
        target=TargetMarketInput(listings_found=2, keyword_volume=6000, serp_presence=True),
        transfer=TransferabilityInput(
            category_compatible=True,
            oversized=False,
            price_positioning_ok=True,
            compliance_risk=False,
            seasonality_concentration=0.2,
            surfaced_risk_flags=("compliance: CPSIA",),
        ),
    )
    assert any("risk flags" in line for line in r.summary)
    assert r.confidence.target_confidence is Confidence.LOW  # thin target → note path


def test_match_weight_within_tolerance_no_conflict() -> None:
    s = _prod(US, brand="Acme", weight_g=300)
    t = _prod(AU, brand="Acme", weight_g=310)
    m = match_products(s, t, CFG)
    assert "weight" not in m.conflicting_signals


def test_report_without_localization_flags() -> None:
    # CA → UK share the metric unit system → no units flag, none others set.
    r = analyze_cross_market(
        source_product=_prod(CA, gtin="0012345678905"),
        target_product=_prod(UK, gtin="0012345678905"),
        source_input=_strong_source(),
        target_input=_good_target(),
        transfer_input=_favorable_transfer(),
        config=CFG,
    )
    assert not r.transferability.localization_flags
    assert not any("localization" in line for line in r.summary)


def test_module_reads_no_clock_or_random() -> None:
    src = Path("src/delium/analysis/cross_market.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    code = src.replace(ast.get_docstring(tree) or "", "")
    for banned in ("now(", "today(", "random.", "random(", "utcnow", "open("):
        assert banned not in code, f"banned call present: {banned}"
