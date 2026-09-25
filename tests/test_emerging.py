"""Tests for the emerging-products feature: Product Finder selection + client,
the deterministic emergence signal, and end-to-end kill/score routing through
the EXISTING scoring pipeline. No network — Keepa is a fake transport.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from delium.analysis.emerging import (
    EmergenceInput,
    build_finder_selection,
    compute_emergence,
    load_emerging_data,
)
from delium.analysis.models import Marketplace
from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.discovery.emerging import estimate_tokens, run_emerging
from delium.providers.keepa import KeepaClient, finder_token_estimate
from keepa_support import FakeTransport, ok

DATA = load_emerging_data("us")
CFG = DeliumConfig()
AS_OF = date(2026, 6, 1)
_EPOCH = 21564000


def _km(d: date) -> int:
    """Keepa minute for a date at UTC midnight (matches providers/keepa math)."""
    return (d - date(1970, 1, 1)).days * 1440 - _EPOCH


# ---------------------------------------------------------------------------
# Product Finder selection (pure)
# ---------------------------------------------------------------------------
def test_build_finder_selection_maps_thresholds() -> None:
    sel = build_finder_selection(DATA, as_of=AS_OF, category_ids=[3760901], per_page=50)
    assert sel["current_SALES_gte"] == DATA.finder.bsr_min
    assert sel["current_SALES_lte"] == DATA.finder.bsr_max
    assert sel["current_NEW_gte"] == DATA.finder.price_min_cents
    assert sel["current_NEW_lte"] == DATA.finder.price_max_cents
    assert sel["current_COUNT_REVIEWS_lte"] == DATA.finder.reviews_max
    assert sel["buyBoxIsAmazon"] is False  # exclude Amazon
    assert sel["rootCategory"] == [3760901]
    assert sel["perPage"] == 50
    # trackingSince cutoff == age_max_days before as_of, in Keepa minutes.
    assert sel["trackingSince_gte"] == _km(AS_OF - timedelta(days=DATA.finder.age_max_days))


def test_build_finder_selection_overrides() -> None:
    sel = build_finder_selection(
        DATA, as_of=AS_OF, category_ids=[], overrides={"reviews_max": 30, "bsr_max": 9000}
    )
    assert sel["current_COUNT_REVIEWS_lte"] == 30
    assert sel["current_SALES_lte"] == 9000
    assert "rootCategory" not in sel  # no category → omitted


def test_finder_token_estimate() -> None:
    assert finder_token_estimate(50) == 11  # 10 + ceil(50/100)
    assert finder_token_estimate(250) == 13  # 10 + 3
    assert estimate_tokens(CFG).finder_tokens == finder_token_estimate(CFG.emerging.page_size)


# ---------------------------------------------------------------------------
# Product Finder client (fake transport)
# ---------------------------------------------------------------------------
def test_product_finder_parses_asin_list() -> None:
    body = {
        "asinList": ["B0AAA00001", "B0BBB00002"],
        "totalResults": 512,
        "tokensConsumed": 11,
        "tokensLeft": 1200,
    }
    client = KeepaClient("k", transport=FakeTransport([ok(body)]), sleep=lambda _: None)
    result = client.product_finder({"perPage": 50})
    assert result.asins == ("B0AAA00001", "B0BBB00002")
    assert result.total_results == 512
    assert result.tokens_consumed == 11


# ---------------------------------------------------------------------------
# Emergence signal (pure)
# ---------------------------------------------------------------------------
def test_emergence_high_for_young_selling_low_review_improving() -> None:
    sig = compute_emergence(
        EmergenceInput(
            asin="B0X",
            as_of=AS_OF,
            earliest_history=AS_OF - timedelta(days=60),  # young
            current_bsr=800,  # selling
            bsr_slope_90d=-0.01,  # improving fast
            review_count=8,  # low competition
            history_days=60,
        ),
        DATA.emergence,
    )
    assert sig.emergence_score is not None and sig.emergence_score >= 70
    assert sig.age_days == 60
    assert sig.missing == ()


def test_emergence_low_for_old_saturated() -> None:
    sig = compute_emergence(
        EmergenceInput(
            asin="B0Y",
            as_of=AS_OF,
            earliest_history=AS_OF - timedelta(days=400),  # old
            current_bsr=90000,  # weak sales
            bsr_slope_90d=0.02,  # declining
            review_count=900,  # crowded
            history_days=400,
        ),
        DATA.emergence,
    )
    assert sig.emergence_score is not None and sig.emergence_score <= 25


def test_emergence_drops_and_renormalizes_missing_signals() -> None:
    sig = compute_emergence(
        EmergenceInput(
            asin="B0Z",
            as_of=AS_OF,
            earliest_history=None,  # age unknown
            current_bsr=None,  # traction unknown
            bsr_slope_90d=None,  # momentum unknown
            review_count=5,  # only competition measurable
            history_days=None,
        ),
        DATA.emergence,
    )
    # Only competition remains → score equals it, others reported missing.
    assert set(sig.missing) == {"recency", "traction", "momentum", "review_gap"}
    assert sig.emergence_score is not None  # never fabricated, but competition alone stands


# ---------------------------------------------------------------------------
# End-to-end: kill vs score routing through the EXISTING pipeline
# ---------------------------------------------------------------------------
def _product_entry(asin: str, *, price_cents: int, bsr: int, reviews: int, age_days: int) -> Any:
    start, end = AS_OF - timedelta(days=age_days), AS_OF
    csv: list[Any] = [None] * 18
    csv[0] = [_km(start), price_cents, _km(end), price_cents]  # Amazon price
    csv[3] = [_km(start), bsr + 200, _km(end), bsr]  # BSR improving
    csv[16] = [_km(end), 44]  # rating 4.4
    csv[17] = [_km(end), reviews]
    return {
        "asin": asin,
        "title": f"Silicone Gadget {asin}",
        "brand": "Acme",
        "categoryTree": [{"catId": 1, "name": "Home & Kitchen"}],
        "packageLength": 180,
        "packageWidth": 120,
        "packageHeight": 40,
        "packageWeight": 250,
        "imagesCSV": "a.jpg,b.jpg",
        "csv": csv,
    }


def test_run_emerging_routes_killed_and_scored(initialized_db: Path) -> None:
    finder = {
        "asinList": ["B0GOOD0001", "B0CHEAP002"],
        "totalResults": 2,
        "tokensConsumed": 11,
        "tokensLeft": 1200,
    }
    products = {
        "tokensConsumed": 8,
        "products": [
            _product_entry("B0GOOD0001", price_cents=2500, bsr=1200, reviews=20, age_days=70),
            _product_entry("B0CHEAP002", price_cents=800, bsr=1500, reviews=15, age_days=50),
        ],
    }
    transport = FakeTransport([ok(finder), ok(products)])
    client = KeepaClient("k", transport=transport, marketplace="US", sleep=lambda _: None)

    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="emerging", input_="US")
    with get_connection() as conn:
        report = run_emerging(
            conn,
            marketplace=Marketplace.US,
            category_ids=[],
            config=CFG,
            run_id=run_id,
            keepa_factory=lambda _mp: client,
            as_of=AS_OF,
        )

    scored_asins = {c.asin for c in report.ranked}
    killed_asins = {c.asin for c in report.killed}
    # The $8 product is below the price floor → hard-killed (K1); the $25 one scores.
    assert "B0CHEAP002" in killed_asins
    assert "B0GOOD0001" in scored_asins
    killed = next(c for c in report.killed if c.asin == "B0CHEAP002")
    assert killed.evaluated.kill_rule is not None  # exact kill reason preserved
    assert killed.emergence.emergence_score is not None  # emergence computed even when killed

    # Persisted for the History page.
    with get_connection() as conn:
        rows = repository.get_emerging_candidates(conn, run_id)
        runs = repository.list_emerging_runs(conn, limit=5)
    assert {r["asin"] for r in rows} == {"B0GOOD0001", "B0CHEAP002"}
    assert runs and runs[0]["finder_total_results"] == 2


def test_title_seed_from_product(initialized_db: Path) -> None:
    from delium.discovery.emerging import _title_seed

    with get_connection() as conn:
        fid = repository.insert_raw_fetch(
            conn,
            run_id=repository.insert_run(conn, command="t", input_="x"),
            provider="keepa",
            endpoint="product",
            request_key="k",
            payload={},
        )
        repository.upsert_product(
            conn,
            asin="B0AAA00001",
            fetch_id=fid,
            marketplace="US",
            title="Silicone Baby Food Freezer Tray Set",
        )
        seed = _title_seed(conn, "B0AAA00001", "US")
    assert seed == "Silicone Baby Food Freezer"  # first 4 words


def test_emerging_format_rows_shapes() -> None:
    from types import SimpleNamespace

    from delium.ui import format as fmt

    scored = SimpleNamespace(
        score=64.0,
        verdict=SimpleNamespace(value="test"),
        confidence=SimpleNamespace(level=SimpleNamespace(value="low")),
    )
    good = SimpleNamespace(
        asin="B1",
        evaluated=SimpleNamespace(scored=scored, kill_rule=None, notes=()),
        emergence=SimpleNamespace(emergence_score=72.0, age_days=60, reasons=("recency: 60d",)),
    )
    killed = SimpleNamespace(
        asin="B2",
        evaluated=SimpleNamespace(scored=None, kill_rule="K1", notes=("$8 < floor",)),
        emergence=SimpleNamespace(emergence_score=55.0, age_days=40, reasons=()),
    )
    assert fmt.emerging_rows([good])[0]["opportunity"] == 64
    assert fmt.emerging_rows([good])[0]["emergence"] == 72
    krow = fmt.emerging_killed_rows([killed])[0]
    assert krow["kill_rule"] == "K1" and krow["reason"] == "$8 < floor"


def test_emerging_cost_estimate() -> None:
    from delium.ui import costs

    est, finder_tok, product_tok = costs.emerging_estimate(CFG, dataforseo=False)
    assert "Keepa" in est.providers and "DataForSEO" not in est.providers
    assert finder_tok > 0 and product_tok > 0
    est2, _f, _p = costs.emerging_estimate(CFG, dataforseo=True)
    assert ("DataForSEO" in est2.providers) == (CFG.emerging.enrich_top_n > 0)


def test_run_emerging_without_keepa_returns_note() -> None:
    with get_connection() as conn:  # any connection; no keepa factory
        report = run_emerging(
            conn,
            marketplace=Marketplace.US,
            category_ids=[],
            config=CFG,
            run_id="r1",
            keepa_factory=None,
            as_of=AS_OF,
            persist=False,
        )
    assert report.ranked == () and report.notes and "Keepa" in report.notes[0]
