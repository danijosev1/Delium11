"""Seeding helpers for discovery pipeline/CLI tests.

Writes marketplace-scoped raw_fetches + extracted rows directly (as the
marketplace-aware ingestion would), so discovery/orchestration tests run over
realistic stored state without any network.
"""

from __future__ import annotations

import sqlite3

from delium.database import repository

SEED = "baby food tray"


def new_run(conn: sqlite3.Connection, note: str = "discover-test") -> str:
    return repository.insert_run(conn, command="discover", input_=note)


def seed_keyword(
    conn: sqlite3.Connection,
    run_id: str,
    marketplace: str,
    phrase: str,
    volume: int,
) -> str:
    fid = repository.insert_raw_fetch(
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
        conn, phrase=phrase, fetch_id=fid, marketplace=marketplace, volume=volume
    )
    return fid


def seed_serp(
    conn: sqlite3.Connection,
    run_id: str,
    marketplace: str,
    phrase: str,
    asins: list[str],
) -> str:
    items = [
        {"type": "amazon_serp", "rank_absolute": i + 1, "data_asin": a} for i, a in enumerate(asins)
    ]
    fid = repository.insert_raw_fetch(
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
    return fid


def seed_product(
    conn: sqlite3.Connection,
    run_id: str,
    asin: str,
    marketplace: str,
    *,
    price_cents: int = 2200,
    reviews: int = 200,
    bsr: int = 1500,
    brand: str = "Acme",
    title: str = "silicone baby food freezer tray",
    gtin: str | None = None,
    dims: tuple[int, int, int] = (200, 150, 50),
    weight_g: int = 300,
) -> str:
    fid = repository.insert_raw_fetch(
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
        fetch_id=fid,
        marketplace=marketplace,
        title=title,
        brand=brand,
        gtin=gtin,
        category_path="Baby > Feeding",
        dims={"length_mm": dims[0], "width_mm": dims[1], "height_mm": dims[2]},
        weight_g=weight_g,
    )
    for day, b in (("2025-04-01", bsr + 100), ("2025-07-01", bsr)):
        repository.upsert_price_bsr_history(
            conn,
            asin=asin,
            captured_on=day,
            price_cents=price_cents,
            bsr=b,
            review_count=reviews,
        )
    return fid


def seed_keyword_market(
    conn: sqlite3.Connection,
    run_id: str,
    marketplace: str,
    *,
    seed: str = SEED,
    volume: int = 9000,
    asins: tuple[str, ...] = ("A1", "A2", "A3"),
    price_cents: int = 2200,
    reviews: int = 200,
) -> None:
    """A full keyword niche: volume + SERP + one product per SERP ASIN."""
    seed_keyword(conn, run_id, marketplace, seed, volume)
    seed_serp(conn, run_id, marketplace, seed, list(asins))
    for asin in asins:
        seed_product(conn, run_id, asin, marketplace, price_cents=price_cents, reviews=reviews)
