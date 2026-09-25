"""Tasks 2-4 — variation dedupe, better finder sampling + brand flag, and the
readable output (rich columns + deterministic plain-English card).

No network: Keepa is a fake transport / fixture bodies, everything else is pure.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from delium.analysis.emerging import (
    build_finder_selections,
    is_established_brand,
    load_emerging_data,
)
from delium.analysis.models import ScoringInput
from delium.analysis.scoring import score_opportunity
from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.discovery.dedupe import group_by_parent
from delium.discovery.diagnostics import diagnose_scored, summarize_fixes
from delium.providers.keepa import KeepaClient
from delium.reports.cards import CardFacts, build_card
from delium.ui import format
from keepa_support import FakeTransport, keepa_product_body, ok
from scoring_support import demand_report, risk_report

CFG = DeliumConfig()
DATA = load_emerging_data("us")


# ---------------------------------------------------------------------------
# Task 2 — parent ASIN capture + dedupe
# ---------------------------------------------------------------------------
def test_keepa_captures_parent_asin_and_stores_it(initialized_db: Path) -> None:
    from delium.ingestion import hydrate_products

    body = keepa_product_body("B0CHILD0001")
    body["products"][0]["parentAsin"] = "B0PARENT001"
    transport = FakeTransport([ok(body)])
    client = KeepaClient("k", transport=transport, sleep=lambda _s: None, marketplace="US")
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="emerging", input_="x")
    hydrate_products(["B0CHILD0001"], run_id=run_id, client=client, config=CFG)

    with get_connection() as conn:
        row = repository.get_product(conn, "B0CHILD0001", "US")
        parents = repository.get_parent_map(conn, ["B0CHILD0001"], "US")
    assert row["parent_asin"] == "B0PARENT001"
    assert parents == {"B0CHILD0001": "B0PARENT001"}


def test_group_by_parent_collapses_variations() -> None:
    items = [
        SimpleNamespace(asin="A1", score=40.0),
        SimpleNamespace(asin="A2", score=70.0),  # best child of the parent
        SimpleNamespace(asin="A3", score=55.0),
        SimpleNamespace(asin="B1", score=65.0),  # standalone
    ]
    parents = {"A1": "P", "A2": "P", "A3": "P", "B1": None}
    groups = group_by_parent(
        items, asin_of=lambda i: i.asin, parents=parents, rank=lambda i: i.score
    )
    assert len(groups) == 2
    top = groups[0]  # ranked best-representative first
    assert top.parent_asin == "P"
    assert top.representative.asin == "A2"  # highest-score child represents
    assert top.variation_count == 3
    assert set(top.child_asins) == {"A1", "A2", "A3"}
    assert groups[1].parent_asin == "B1" and groups[1].variation_count == 1


# ---------------------------------------------------------------------------
# Task 3 — sub-band sampling + brand flag
# ---------------------------------------------------------------------------
def test_build_finder_selections_splits_the_bsr_band() -> None:
    sels = build_finder_selections(DATA, as_of=date(2026, 6, 1), category_ids=[], per_page=40)
    assert len(sels) == DATA.finder.sub_bands >= 2
    # Sub-bands are contiguous and span the whole configured band.
    assert sels[0]["current_SALES_gte"] == DATA.finder.bsr_min
    assert sels[-1]["current_SALES_lte"] == DATA.finder.bsr_max
    for a, b in zip(sels, sels[1:], strict=False):
        assert a["current_SALES_lte"] == b["current_SALES_gte"]  # contiguous edges
        assert a["current_SALES_lte"] > a["current_SALES_gte"]  # non-empty band
    # Each sub-band asks for a share of the page, not the whole page.
    assert all(s["perPage"] <= 40 for s in sels)


def test_single_sub_band_restores_one_selection() -> None:
    import dataclasses

    data = dataclasses.replace(DATA, finder=dataclasses.replace(DATA.finder, sub_bands=1))
    sels = build_finder_selections(data, as_of=date(2026, 6, 1), category_ids=[], per_page=50)
    assert len(sels) == 1
    assert sels[0]["current_SALES_gte"] == data.finder.bsr_min
    assert sels[0]["current_SALES_lte"] == data.finder.bsr_max


def test_is_established_brand_flags_large_brands() -> None:
    assert is_established_brand("Anker", DATA.established_brands) is True
    assert is_established_brand("ANKER", DATA.established_brands) is True
    assert is_established_brand("Amazon Basics", DATA.established_brands) is True
    assert is_established_brand("Acme Widgets", DATA.established_brands) is False
    assert is_established_brand(None, DATA.established_brands) is False


# ---------------------------------------------------------------------------
# Task 1 diagnostics — confidence causes + fix summary
# ---------------------------------------------------------------------------
def _finder_scored() -> object:
    inp = ScoringInput(
        demand=demand_report(80),
        competition=None,
        differentiation=None,
        profit=None,
        risk=risk_report(100),
        market_median_price_cents=2200,
        oversized=False,
        amazon_in_top5=False,
        restricted_category=False,
        ip_signature=False,
        avoid_matches=(),
        fad_search_volume=9400,
        fad_volume_12mo_median=9000,
        volume_history_months=36,
    )
    return score_opportunity(inp, CFG)


def test_diagnosis_maps_absent_pillars_to_evidence_fixes() -> None:
    diag = diagnose_scored("B0DIAG0001", "US", _finder_scored())  # type: ignore[arg-type]
    fixes = {c.pillar: c.fix_key for c in diag.causes}
    assert fixes["competition"] == "serp_competitors"
    assert fixes["differentiation"] == "reviews_llm"
    assert fixes["profitability"] == "fba_fees"
    # A present, sufficient pillar contributes no confidence cause.
    assert "demand" not in fixes


def test_diagnose_candidate_rebuilds_from_db_read_only(initialized_db: Path) -> None:
    from delium.analysis.models import Marketplace
    from delium.discovery.diagnostics import diagnose_candidate
    from delium.ingestion import hydrate_products

    body = keepa_product_body("B0FINDER001")
    transport = FakeTransport([ok(body)])
    client = KeepaClient("k", transport=transport, sleep=lambda _s: None, marketplace="US")
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="emerging", input_="x")
    hydrate_products(["B0FINDER001"], run_id=run_id, client=client, config=CFG)

    calls_before = transport.call_count
    with get_connection() as conn:
        diag = diagnose_candidate(conn, "B0FINDER001", Marketplace.US, CFG)
    assert diag is not None
    assert transport.call_count == calls_before  # rebuilt from the DB, no new fetch
    # Competition/differentiation are absent for a lone finder ASIN → their fixes appear.
    assert "serp_competitors" in diag.blocking_fixes
    assert "reviews_llm" in diag.blocking_fixes


def test_summarize_fixes_counts_blockers() -> None:
    diags = [diagnose_scored(f"B{i}", "US", _finder_scored()) for i in range(3)]  # type: ignore[arg-type]
    impacts = {i.fix_key: i for i in summarize_fixes(diags)}
    # All three finder candidates are blocked by the same three fixes.
    assert impacts["serp_competitors"].blocks_count == 3
    assert impacts["reviews_llm"].blocks_count == 3
    # None has a single sole blocker (three fixes each), so none lifts on one fix.
    assert all(i.sole_blocker_count == 0 for i in impacts.values())


# ---------------------------------------------------------------------------
# Task 4 — rich rows + plain-English card
# ---------------------------------------------------------------------------
def _emerging_candidate(asin: str, emergence: float, *, established: bool = False) -> object:
    scored = _finder_scored()
    return SimpleNamespace(
        asin=asin,
        emergence=SimpleNamespace(emergence_score=emergence, age_days=120),
        evaluated=SimpleNamespace(scored=scored),
        established_brand=established,
    )


def test_emerging_rich_rows_dedupe_and_pillar_columns() -> None:
    cands = [
        _emerging_candidate("A1", 90.0),
        _emerging_candidate("A2", 95.0),  # best child of parent P
        _emerging_candidate("B1", 80.0, established=True),
    ]
    facts = {
        "A2": {"title": "Tray", "brand": "Acme", "price_cents": 2200, "bsr": 210, "reviews": 55},
        "B1": {"title": "Widget", "brand": "Anker", "price_cents": 3000, "bsr": 900, "reviews": 12},
    }
    parents = {"A1": "P", "A2": "P", "B1": None}
    rows = format.emerging_rich_rows(cands, facts, parents)
    assert len(rows) == 2  # A1/A2 collapsed to one parent row
    top = rows[0]
    assert top["asin"] == "A2" and top["parent_asin"] == "P" and top["variations"] == 2
    assert top["price_usd"] == 22.0 and top["bsr"] == 210
    # Absent pillars render as a blank cell (None), not 0.
    assert top["competition"] is None and top["competition_conf"] == "absent"
    assert top["demand"] is not None
    b_row = next(r for r in rows if r["asin"] == "B1")
    assert b_row["flag"] == "established brand"


def test_build_card_is_deterministic_and_explains() -> None:
    diag = diagnose_scored("B0CARD0001", "US", _finder_scored())  # type: ignore[arg-type]
    facts = CardFacts(
        title="Silicone Tray",
        brand="Anker",
        price_cents=2200,
        bsr=210,
        reviews=55,
        age_days=120,
        emergence=95.0,
        established_brand=True,
    )
    card = build_card(diag, facts)
    assert "AVOID" in card.headline and "LOW" in card.headline
    text = "\n".join(card.lines())
    assert "price $22.00" in text and "BSR 210" in text  # promising facts with numbers
    # Confidence explanation names the missing evidence + its fix.
    assert any("competition" in c for c in card.low_confidence)
    assert any("SERP" in f for f in card.to_raise)
    assert any("Anker" in flag for flag in card.flags)  # brand flag surfaced
    assert card.next_action  # a concrete next step is always given
    # Determinism.
    assert build_card(diag, facts).lines() == card.lines()
