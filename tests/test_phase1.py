"""Phase 1 — Research Profile, Keepa evidence, profit-with-real-fees, shortlist +
snapshots, and Product Workspace assembly. No network (fixtures + fake transport).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import discovery_support as seed
from delium.analysis.fees import compute_fees, load_fee_table
from delium.analysis.models import Dimensions, Marketplace
from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.profile import store
from delium.profile.models import CogsMode, ResearchProfile, RiskTolerance
from delium.providers.keepa import KeepaClient, normalize_product
from keepa_support import FakeTransport, keepa_product_body, ok

CFG = DeliumConfig()
US = Marketplace.US


# ---------------------------------------------------------------------------
# Research Profile — application (pure)
# ---------------------------------------------------------------------------
def test_profile_finder_overrides_map_to_finder_keys() -> None:
    p = ResearchProfile(name="x", price_min_cents=1800, price_max_cents=4500, max_reviews=300)
    ov = p.finder_overrides()
    assert ov == {"price_min_cents": 1800, "price_max_cents": 4500, "reviews_max": 300}


def test_profile_profit_overrides_pct_and_unit() -> None:
    pct = ResearchProfile(name="p", cogs_mode=CogsMode.PCT, cogs_value=0.30, freight_per_kg_usd=6.0)
    ov = pct.profit_overrides(price_cents=2000, weight_g=500)
    assert ov.cogs_cents == 600  # 30% of $20.00
    assert ov.freight_cents == 300  # $6/kg × 0.5kg = $3.00

    unit = ResearchProfile(name="u", cogs_mode=CogsMode.UNIT, cogs_value=4.25)
    ov2 = unit.profit_overrides(price_cents=2000, weight_g=None)
    assert ov2.cogs_cents == 425  # $4.25/unit regardless of price
    assert ov2.freight_cents is None  # unknown weight → fall back to config


def test_profile_targets_and_budget_helpers() -> None:
    p = ResearchProfile(name="t", target_net_margin=0.30, target_roi=1.5, budget_usd=5000)
    assert p.meets_targets(net_margin=0.35, roi=2.0) is True
    assert p.meets_targets(net_margin=0.20, roi=2.0) is False
    assert p.meets_targets(net_margin=None, roi=None) is False
    assert p.units_affordable(landed_cost_cents=1000) == 500  # $5000 / $10.00
    # No preference → always "met".
    assert ResearchProfile(name="n").meets_targets(net_margin=None, roi=None) is True


def test_profile_risk_tolerance_is_sort_only() -> None:
    assert ResearchProfile(name="c", risk_tolerance=RiskTolerance.CONSERVATIVE).risk_rank == 0
    assert ResearchProfile(name="a", risk_tolerance=RiskTolerance.AGGRESSIVE).risk_rank == 2


# ---------------------------------------------------------------------------
# Research Profile — persistence + presets
# ---------------------------------------------------------------------------
def test_profile_seeds_two_presets_with_one_active(initialized_db: Path) -> None:
    with get_connection() as conn:
        active = store.load_active(conn)
        profiles = store.list_profiles(conn)
    assert {p.name for p in profiles} == {"Conservative $5k", "Growth $20k"}
    assert active.name == "Conservative $5k" and active.is_active
    assert sum(p.is_active for p in profiles) == 1  # exactly one active


def test_profile_save_roundtrip_and_switch_active(initialized_db: Path) -> None:
    with get_connection() as conn:
        store.ensure_seeded(conn)
        profiles = store.list_profiles(conn)
        growth = next(p for p in profiles if p.name == "Growth $20k")
        store.set_active(conn, growth.id or "")
        active = store.load_active(conn)
    assert active.name == "Growth $20k"
    # Round-trip preserves the typed fields (JSON list + enums).
    assert active.marketplaces == ("US",)
    assert active.cogs_mode is CogsMode.PCT
    assert active.risk_tolerance is RiskTolerance.AGGRESSIVE


def test_profile_edit_persists(initialized_db: Path) -> None:
    with get_connection() as conn:
        active = store.load_active(conn)
        edited = ResearchProfile(
            **{**active.__dict__, "budget_usd": 12345.0, "excluded_categories": ("glass",)}
        )
        store.save(conn, edited)
        reloaded = store.get(conn, active.id or "")
    assert reloaded is not None
    assert reloaded.budget_usd == 12345.0
    assert reloaded.excluded_categories == ("glass",)
    assert reloaded.excludes_category("Kitchen > Glass Jars") is True


# ---------------------------------------------------------------------------
# Keepa evidence capture (monthlySold, fbaFees.pickAndPackFee, referral%)
# ---------------------------------------------------------------------------
def test_normalize_captures_evidence_fields() -> None:
    body = keepa_product_body("B0EVID00001")
    prod = body["products"][0]
    prod["monthlySold"] = 850
    prod["fbaFees"] = {"pickAndPackFee": 537}
    prod["referralFeePercentage"] = 15
    n = normalize_product(prod, "US")
    assert n.monthly_sold == 850
    assert n.fba_pick_pack_cents == 537
    assert n.referral_fee_percent == 0.15


def test_evidence_persisted_and_in_product_view(initialized_db: Path) -> None:
    from delium.ingestion import hydrate_products

    body = keepa_product_body("B0EVID00002")
    body["products"][0]["monthlySold"] = 420
    body["products"][0]["fbaFees"] = {"pickAndPackFee": 512}
    client = KeepaClient("k", transport=FakeTransport([ok(body)]), sleep=lambda _s: None)
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="emerging", input_="x")
    views = hydrate_products(["B0EVID00002"], run_id=run_id, client=client, config=CFG)
    view = views["B0EVID00002"]
    assert view.monthly_sold == 420 and view.fba_pick_pack_cents == 512
    with get_connection() as conn:
        row = repository.get_product(conn, "B0EVID00002", "US")
    assert row["monthly_sold"] == 420 and row["fba_pick_pack_cents"] == 512


# ---------------------------------------------------------------------------
# Profit with the real Keepa FBA fee
# ---------------------------------------------------------------------------
def test_compute_fees_uses_real_fulfillment_when_present() -> None:
    table = load_fee_table()
    dims = Dimensions(200, 150, 50)
    table_fee = compute_fees(table, category="Baby", price_cents=2200, dims=dims, weight_g=300)
    real = compute_fees(
        table,
        category="Baby",
        price_cents=2200,
        dims=dims,
        weight_g=300,
        real_fulfillment_cents=999,
    )
    assert table_fee.fulfillment_source == "table"
    assert real.fulfillment_source == "keepa" and real.fulfillment_cents == 999
    # A zero/absent real fee falls back to the table estimate.
    zero = compute_fees(
        table, category="Baby", price_cents=2200, dims=dims, weight_g=300, real_fulfillment_cents=0
    )
    assert zero.fulfillment_source == "table"


def test_build_profit_threads_real_fee_from_stored_product(initialized_db: Path) -> None:
    from delium.discovery.assembly import build_profit

    with get_connection() as conn:
        run_id = seed.new_run(conn)
        fid = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:US:B0FEE000001",
            payload={"asin": "B0FEE000001"},
        )
        repository.upsert_product(
            conn,
            asin="B0FEE000001",
            fetch_id=fid,
            marketplace="US",
            title="Tray",
            category_path="Baby > Feeding",
            dims={"length_mm": 200, "width_mm": 150, "height_mm": 50},
            weight_g=300,
            fba_pick_pack_cents=642,
        )
        row = repository.get_product(conn, "B0FEE000001", "US")
    scenarios = build_profit(row, 2200, 300, CFG)
    assert scenarios is not None
    assert scenarios.expected.fees.fulfillment_source == "keepa"
    assert scenarios.expected.fees.fulfillment_cents == 642


# ---------------------------------------------------------------------------
# Shortlist + snapshots
# ---------------------------------------------------------------------------
def test_shortlist_crud(initialized_db: Path) -> None:
    with get_connection() as conn:
        repository.upsert_shortlist(conn, asin="B0SHORT0001", status="sampling", notes="looks good")
        entry = repository.get_shortlist_entry(conn, "B0SHORT0001")
        assert entry["status"] == "sampling" and entry["notes"] == "looks good"
        repository.upsert_shortlist(conn, asin="B0SHORT0001", status="rejected", notes="too heavy")
        assert repository.get_shortlist_entry(conn, "B0SHORT0001")["status"] == "rejected"
        assert len(repository.list_shortlist(conn)) == 1
        repository.remove_from_shortlist(conn, "B0SHORT0001")
        assert repository.get_shortlist_entry(conn, "B0SHORT0001") is None


def test_snapshots_series_records_over_time(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = seed.new_run(conn)
        repository.insert_product_snapshot(
            conn, asin="B0SNAP0001", run_id=run_id, bsr=1500, monthly_sold=300, verdict="test"
        )
        repository.insert_product_snapshot(
            conn, asin="B0SNAP0001", run_id=run_id, bsr=1200, monthly_sold=420, verdict="test"
        )
        snaps = repository.get_product_snapshots(conn, "B0SNAP0001")
    assert len(snaps) == 2
    assert [s["bsr"] for s in snaps] == [1500, 1200]  # oldest→newest, momentum visible


# ---------------------------------------------------------------------------
# Product Workspace assembly (read-only)
# ---------------------------------------------------------------------------
def test_workspace_assembles_all_tabs(initialized_db: Path) -> None:
    from delium.discovery.workspace import assemble_workspace

    with get_connection() as conn:
        run_id = seed.new_run(conn)
        # A full niche: target + two competitors on one keyword.
        seed.seed_keyword_market(
            conn, run_id, "US", asins=("B0TARGET001", "B0COMP00001", "B0COMP00002")
        )
        profile = store.load_active(conn)
        ws = assemble_workspace(conn, "B0TARGET001", US, CFG, profile, as_of=date(2025, 8, 1))

    assert ws.found and ws.profile_name == "Conservative $5k"
    assert ws.diagnosis is not None and ws.card is not None
    assert ws.momentum is not None and ws.momentum.dates  # history charted
    assert any(k.is_primary for k in ws.keywords.keywords)  # main keyword resolved
    # Competitors: the other two SERP ASINs (target excluded).
    comp_asins = {c.asin for c in ws.competitors.competitors}
    assert comp_asins == {"B0COMP00001", "B0COMP00002"}
    assert ws.profit.available and ws.profit.scenarios is not None
    assert ws.reviews.available is False  # no reviews seeded → "add keys" path
    assert ws.shortlist.on_shortlist is False


def test_workspace_missing_product_is_not_found(initialized_db: Path) -> None:
    from delium.discovery.workspace import assemble_workspace

    with get_connection() as conn:
        profile = store.load_active(conn)
        ws = assemble_workspace(conn, "B0GHOST0001", US, CFG, profile)
    assert ws.found is False
    assert ws.diagnosis is None  # nothing to score
    assert ws.profit.available is False


def test_deep_dive_plan_lists_missing_evidence_with_cost(initialized_db: Path) -> None:
    from delium.discovery.workspace import assemble_workspace, deep_dive_plan

    with get_connection() as conn:
        run_id = seed.new_run(conn)
        # Only the product exists — no SERP/keywords, no reviews.
        seed.seed_product(conn, run_id, "B0LONE00001", "US")
        profile = store.load_active(conn)
        ws = assemble_workspace(conn, "B0LONE00001", US, CFG, profile)
    plan = deep_dive_plan(
        ws, CFG, reviews_configured=True, dataforseo_configured=True, keepa_configured=True
    )
    keys = {s.key for s in plan.steps}
    assert "competitors" in keys  # no competitor set yet
    assert "keywords" in keys  # reverse-ASIN not fetched
    assert "reviews" in keys  # no reviews mined
    assert plan.has_work and plan.total_cost_usd > 0
    # Nothing is offered when the providers are not configured.
    none_plan = deep_dive_plan(
        ws, CFG, reviews_configured=False, dataforseo_configured=False, keepa_configured=False
    )
    assert not none_plan.has_work
