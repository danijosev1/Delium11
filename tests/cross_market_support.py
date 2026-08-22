"""Seeding helpers for cross-market pipeline/CLI tests.

Writes marketplace-scoped raw_fetches + extracted rows directly, mimicking what
the marketplace-aware ingestion would persist — so pipeline tests exercise the
assembler/discovery over realistic stored state without touching the network.
"""

from __future__ import annotations

import sqlite3

from delium.database import repository

_SEED = "baby food tray"


def new_run(conn: sqlite3.Connection, note: str = "cross-market-test") -> str:
    return repository.insert_run(conn, command="cross-market", input_=note)


def seed_product(
    conn: sqlite3.Connection,
    run_id: str,
    asin: str,
    marketplace: str,
    *,
    gtin: str | None = None,
    title: str = "silicone baby food freezer tray with lid",
    brand: str = "Acme",
    reviews: int | None = None,
    dims: tuple[int, int, int] = (200, 150, 50),
    weight_g: int = 300,
    price_cents: int = 2200,
    est_units: tuple[int, int] | None = None,
    seasonality_peak: float | None = None,
) -> str:
    """Persist a product (+ history + optional derived) in one marketplace."""
    fetch_id = repository.insert_raw_fetch(
        conn,
        run_id=run_id,
        provider="keepa",
        endpoint="product",
        request_key=f"keepa:product:{marketplace}:{asin}",
        payload={"asin": asin},
    )
    repository.upsert_product(
        conn,
        asin=asin,
        fetch_id=fetch_id,
        marketplace=marketplace,
        title=title,
        brand=brand,
        gtin=gtin,
        category_path="Baby > Feeding",
        dims={"length_mm": dims[0], "width_mm": dims[1], "height_mm": dims[2]},
        weight_g=weight_g,
    )
    for day in ("2025-01-01", "2025-07-01"):
        repository.upsert_price_bsr_history(
            conn,
            asin=asin,
            captured_on=day,
            price_cents=price_cents,
            bsr=1500,
            review_count=reviews,
        )
    if est_units is not None or seasonality_peak is not None:
        repository.upsert_product_derived(
            conn,
            asin=asin,
            fetch_id=fetch_id,
            est_units_low=est_units[0] if est_units else None,
            est_units_high=est_units[1] if est_units else None,
            seasonality_peak_pct=seasonality_peak,
        )
    return fetch_id


def seed_keyword_volume(
    conn: sqlite3.Connection,
    run_id: str,
    marketplace: str,
    phrase: str,
    volume: int,
) -> str:
    """Persist a marketplace-scoped bulk_search_volume raw_fetch + keyword row."""
    fetch_id = repository.insert_raw_fetch(
        conn,
        run_id=run_id,
        provider="dataforseo",
        endpoint="bulk_search_volume",
        request_key=f"dataforseo:volume:{marketplace}:{phrase}",
        payload={
            "tasks": [
                {
                    "status_code": 20000,
                    "result": [{"items": [{"keyword": phrase, "search_volume": volume}]}],
                }
            ]
        },
    )
    repository.upsert_keyword(
        conn, phrase=phrase, fetch_id=fetch_id, marketplace=marketplace, volume=volume
    )
    return fetch_id


def seed_serp(
    conn: sqlite3.Connection,
    run_id: str,
    marketplace: str,
    phrase: str,
    asins: list[str],
) -> str:
    """Persist a marketplace-scoped SERP raw_fetch (+ ranking rows). An empty
    `asins` records a 'looked up, nothing found' state (NOT_PRESENT)."""
    items = [
        {"type": "amazon_serp", "rank_absolute": i + 1, "data_asin": a} for i, a in enumerate(asins)
    ]
    fetch_id = repository.insert_raw_fetch(
        conn,
        run_id=run_id,
        provider="dataforseo",
        endpoint="amazon_serp",
        request_key=f"dataforseo:serp:{marketplace}:{phrase}",
        payload={"tasks": [{"status_code": 20000, "result": [{"items": items}]}]},
    )
    for i, asin in enumerate(asins):
        repository.upsert_serp_ranking(
            conn,
            keyword_phrase=phrase,
            asin=asin,
            position=i + 1,
            captured_on="2025-07-01",
            marketplace=marketplace,
        )
    return fetch_id


def seed_strong_source(conn: sqlite3.Connection, run_id: str, asin: str = "USASIN1") -> str:
    """A convincingly-successful US source product linked to the seed keyword."""
    seed_product(
        conn,
        run_id,
        asin,
        "US",
        gtin="0012345678905",
        reviews=1200,
        est_units=(1000, 2000),
        seasonality_peak=0.2,
    )
    seed_keyword_volume(conn, run_id, "US", _SEED, 30000)
    seed_serp(conn, run_id, "US", _SEED, [asin])
    return _SEED
