"""Explicit SQL repository helpers.

Thin, typed functions over the schema — no ORM, no query builder. Each takes an
open `sqlite3.Connection`; the caller owns the transaction (use
`delium.database.get_connection`, which commits on clean exit). JSON columns
accept Python objects and are serialized here.

These cover insert/upsert and simple reads for the core tables. The pipeline
(`ingestion/`) and analysis layers build on top of them; they intentionally
contain no business logic.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any


def _new_id() -> str:
    return uuid.uuid4().hex


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _one(cur: sqlite3.Cursor) -> sqlite3.Row | None:
    row: sqlite3.Row | None = cur.fetchone()
    return row


def _all(cur: sqlite3.Cursor) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = cur.fetchall()
    return rows


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------
def insert_run(
    conn: sqlite3.Connection,
    *,
    command: str,
    input_: str | None = None,
    config_snapshot: Any = None,
    run_id: str | None = None,
) -> str:
    rid = run_id or _new_id()
    conn.execute(
        """
        INSERT INTO runs (id, command, input, config_snapshot)
        VALUES (?, ?, ?, ?)
        """,
        (rid, command, input_, None if config_snapshot is None else _dumps(config_snapshot)),
    )
    return rid


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    data_cost_usd: float = 0.0,
    llm_cost_usd: float = 0.0,
) -> None:
    conn.execute(
        """
        UPDATE runs
           SET status = ?, data_cost_usd = ?, llm_cost_usd = ?,
               finished_at = datetime('now')
         WHERE id = ?
        """,
        (status, data_cost_usd, llm_cost_usd, run_id),
    )


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return _one(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)))


# ---------------------------------------------------------------------------
# raw_fetches (cache / fetch log / spend ledger)
# ---------------------------------------------------------------------------
def insert_raw_fetch(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    provider: str,
    endpoint: str,
    request_key: str,
    payload: Any,
    cost_usd: float = 0.0,
    tokens_used: int = 0,
    http_status: int | None = None,
) -> str:
    fetch_id = _new_id()
    conn.execute(
        """
        INSERT INTO raw_fetches
            (id, provider, endpoint, request_key, payload, cost_usd,
             tokens_used, http_status, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fetch_id,
            provider,
            endpoint,
            request_key,
            _dumps(payload),
            cost_usd,
            tokens_used,
            http_status,
            run_id,
        ),
    )
    return fetch_id


def latest_raw_fetch(
    conn: sqlite3.Connection, provider: str, request_key: str
) -> sqlite3.Row | None:
    """Newest cached fetch for a (provider, request_key) — the cache lookup."""
    return _one(
        conn.execute(
            """
            SELECT * FROM raw_fetches
             WHERE provider = ? AND request_key = ?
             ORDER BY fetched_at DESC, rowid DESC
             LIMIT 1
            """,
            (provider, request_key),
        )
    )


# ---------------------------------------------------------------------------
# products
# ---------------------------------------------------------------------------
def upsert_product(
    conn: sqlite3.Connection,
    *,
    asin: str,
    fetch_id: str,
    marketplace: str = "US",
    title: str | None = None,
    brand: str | None = None,
    category_path: str | None = None,
    listing_date: str | None = None,
    dims: Any = None,
    weight_g: int | None = None,
    size_tier: str | None = None,
    images_count: int | None = None,
    amazon_on_listing: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO products
            (asin, marketplace, title, brand, category_path, listing_date,
             dims_json, weight_g, size_tier, images_count, amazon_on_listing,
             fetch_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(asin) DO UPDATE SET
            marketplace = excluded.marketplace,
            title = excluded.title,
            brand = excluded.brand,
            category_path = excluded.category_path,
            listing_date = excluded.listing_date,
            dims_json = excluded.dims_json,
            weight_g = excluded.weight_g,
            size_tier = excluded.size_tier,
            images_count = excluded.images_count,
            amazon_on_listing = excluded.amazon_on_listing,
            fetch_id = excluded.fetch_id,
            updated_at = datetime('now')
        """,
        (
            asin,
            marketplace,
            title,
            brand,
            category_path,
            listing_date,
            None if dims is None else _dumps(dims),
            weight_g,
            size_tier,
            images_count,
            int(amazon_on_listing),
            fetch_id,
        ),
    )


def get_product(conn: sqlite3.Connection, asin: str) -> sqlite3.Row | None:
    return _one(conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)))


# ---------------------------------------------------------------------------
# price_bsr_history
# ---------------------------------------------------------------------------
def upsert_price_bsr_history(
    conn: sqlite3.Connection,
    *,
    asin: str,
    captured_on: str,
    price_cents: int | None = None,
    bsr: int | None = None,
    offer_count: int | None = None,
    review_count: int | None = None,
    rating: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO price_bsr_history
            (asin, captured_on, price_cents, bsr, offer_count, review_count, rating)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asin, captured_on) DO UPDATE SET
            price_cents = excluded.price_cents,
            bsr = excluded.bsr,
            offer_count = excluded.offer_count,
            review_count = excluded.review_count,
            rating = excluded.rating
        """,
        (asin, captured_on, price_cents, bsr, offer_count, review_count, rating),
    )


def get_price_bsr_history(conn: sqlite3.Connection, asin: str) -> list[sqlite3.Row]:
    return _all(
        conn.execute(
            "SELECT * FROM price_bsr_history WHERE asin = ? ORDER BY captured_on",
            (asin,),
        )
    )


# ---------------------------------------------------------------------------
# product_derived
# ---------------------------------------------------------------------------
def upsert_product_derived(
    conn: sqlite3.Connection,
    *,
    asin: str,
    fetch_id: str,
    est_units_low: int | None = None,
    est_units_high: int | None = None,
    bsr_slope_90d: float | None = None,
    price_cv_90d: float | None = None,
    review_velocity_mo: float | None = None,
    seasonality_peak_pct: float | None = None,
    history_days: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO product_derived
            (asin, est_units_low, est_units_high, bsr_slope_90d, price_cv_90d,
             review_velocity_mo, seasonality_peak_pct, history_days,
             computed_at, fetch_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(asin) DO UPDATE SET
            est_units_low = excluded.est_units_low,
            est_units_high = excluded.est_units_high,
            bsr_slope_90d = excluded.bsr_slope_90d,
            price_cv_90d = excluded.price_cv_90d,
            review_velocity_mo = excluded.review_velocity_mo,
            seasonality_peak_pct = excluded.seasonality_peak_pct,
            history_days = excluded.history_days,
            computed_at = datetime('now'),
            fetch_id = excluded.fetch_id
        """,
        (
            asin,
            est_units_low,
            est_units_high,
            bsr_slope_90d,
            price_cv_90d,
            review_velocity_mo,
            seasonality_peak_pct,
            history_days,
            fetch_id,
        ),
    )


def get_product_derived(conn: sqlite3.Connection, asin: str) -> sqlite3.Row | None:
    return _one(conn.execute("SELECT * FROM product_derived WHERE asin = ?", (asin,)))


# ---------------------------------------------------------------------------
# keywords & serp_rankings
# ---------------------------------------------------------------------------
def upsert_keyword(
    conn: sqlite3.Connection,
    *,
    phrase: str,
    fetch_id: str,
    marketplace: str = "US",
    volume: int | None = None,
    volume_series: Any = None,
    cpc_cents: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO keywords
            (phrase, marketplace, volume, volume_series, cpc_cents, fetch_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(phrase) DO UPDATE SET
            marketplace = excluded.marketplace,
            volume = excluded.volume,
            volume_series = excluded.volume_series,
            cpc_cents = excluded.cpc_cents,
            fetch_id = excluded.fetch_id,
            updated_at = datetime('now')
        """,
        (
            phrase,
            marketplace,
            volume,
            None if volume_series is None else _dumps(volume_series),
            cpc_cents,
            fetch_id,
        ),
    )


def get_keyword(conn: sqlite3.Connection, phrase: str) -> sqlite3.Row | None:
    return _one(conn.execute("SELECT * FROM keywords WHERE phrase = ?", (phrase,)))


def upsert_serp_ranking(
    conn: sqlite3.Connection,
    *,
    keyword_phrase: str,
    asin: str,
    position: int,
    captured_on: str,
    sponsored: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO serp_rankings (keyword_phrase, asin, position, sponsored, captured_on)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(keyword_phrase, asin, captured_on) DO UPDATE SET
            position = excluded.position,
            sponsored = excluded.sponsored
        """,
        (keyword_phrase, asin, position, int(sponsored), captured_on),
    )


def get_serp_rankings(conn: sqlite3.Connection, keyword_phrase: str) -> list[sqlite3.Row]:
    return _all(
        conn.execute(
            "SELECT * FROM serp_rankings WHERE keyword_phrase = ? ORDER BY position",
            (keyword_phrase,),
        )
    )


# ---------------------------------------------------------------------------
# competitor_sets
# ---------------------------------------------------------------------------
def insert_competitor_set(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    target_asin: str,
    member_asins: list[str],
    selection_method: str,
) -> str:
    set_id = _new_id()
    conn.execute(
        """
        INSERT INTO competitor_sets
            (id, run_id, target_asin, member_asins, selection_method)
        VALUES (?, ?, ?, ?, ?)
        """,
        (set_id, run_id, target_asin, _dumps(member_asins), selection_method),
    )
    return set_id


def get_competitor_set(conn: sqlite3.Connection, set_id: str) -> sqlite3.Row | None:
    return _one(conn.execute("SELECT * FROM competitor_sets WHERE id = ?", (set_id,)))


# ---------------------------------------------------------------------------
# reviews & review_themes
# ---------------------------------------------------------------------------
def insert_review(
    conn: sqlite3.Connection,
    *,
    review_id: str,
    asin: str,
    fetch_id: str,
    stars: int,
    review_date: str | None = None,
    title: str | None = None,
    body: str | None = None,
    verified: bool = False,
    helpful_votes: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO reviews
            (review_id, asin, stars, review_date, title, body, verified,
             helpful_votes, fetch_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(review_id) DO UPDATE SET
            asin = excluded.asin,
            stars = excluded.stars,
            review_date = excluded.review_date,
            title = excluded.title,
            body = excluded.body,
            verified = excluded.verified,
            helpful_votes = excluded.helpful_votes,
            fetch_id = excluded.fetch_id
        """,
        (
            review_id,
            asin,
            stars,
            review_date,
            title,
            body,
            int(verified),
            helpful_votes,
            fetch_id,
        ),
    )


def get_reviews_for_asin(conn: sqlite3.Connection, asin: str) -> list[sqlite3.Row]:
    return _all(
        conn.execute(
            "SELECT * FROM reviews WHERE asin = ? ORDER BY review_date DESC, review_id",
            (asin,),
        )
    )


def insert_review_theme(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    asin: str,
    kind: str,
    theme: str,
    quote_review_ids: list[str],
    frequency_pct: float | None = None,
    severity: int | None = None,
) -> str:
    theme_id = _new_id()
    conn.execute(
        """
        INSERT INTO review_themes
            (id, run_id, asin, kind, theme, frequency_pct, severity, quote_review_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            theme_id,
            run_id,
            asin,
            kind,
            theme,
            frequency_pct,
            severity,
            _dumps(quote_review_ids),
        ),
    )
    return theme_id


def get_review_themes(conn: sqlite3.Connection, asin: str) -> list[sqlite3.Row]:
    return _all(conn.execute("SELECT * FROM review_themes WHERE asin = ? ORDER BY kind", (asin,)))
