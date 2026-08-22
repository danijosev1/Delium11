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
    gtin: str | None = None,
    manufacturer: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO products
            (asin, marketplace, title, brand, category_path, listing_date,
             dims_json, weight_g, size_tier, images_count, amazon_on_listing,
             gtin, manufacturer, fetch_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
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
            gtin = excluded.gtin,
            manufacturer = excluded.manufacturer,
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
            gtin,
            manufacturer,
            fetch_id,
        ),
    )


def get_product(
    conn: sqlite3.Connection, asin: str, marketplace: str | None = None
) -> sqlite3.Row | None:
    """Fetch a product by ASIN. When `marketplace` is given, the stored row is
    returned only if its marketplace matches — so an IN query never satisfies
    with a US-stored row (cross-market isolation)."""
    if marketplace is None:
        return _one(conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)))
    return _one(
        conn.execute(
            "SELECT * FROM products WHERE asin = ? AND marketplace = ?",
            (asin, marketplace),
        )
    )


def get_products_by_marketplace(
    conn: sqlite3.Connection, marketplace: str, *, limit: int | None = None
) -> list[sqlite3.Row]:
    """All products stored for a marketplace, newest first (candidate pool)."""
    sql = "SELECT * FROM products WHERE marketplace = ? ORDER BY updated_at DESC, asin"
    params: tuple[Any, ...] = (marketplace,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (marketplace, limit)
    return _all(conn.execute(sql, params))


def find_product_by_gtin(
    conn: sqlite3.Connection, gtin: str, marketplace: str
) -> sqlite3.Row | None:
    """A product in `marketplace` whose GTIN matches — the exact identity key."""
    if not gtin:
        return None
    return _one(
        conn.execute(
            """
            SELECT * FROM products
             WHERE gtin = ? AND marketplace = ?
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            (gtin, marketplace),
        )
    )


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


def get_keyword(
    conn: sqlite3.Connection, phrase: str, marketplace: str | None = None
) -> sqlite3.Row | None:
    """Fetch a keyword row. When `marketplace` is given, the row is returned only
    if its marketplace matches (cross-market isolation)."""
    if marketplace is None:
        return _one(conn.execute("SELECT * FROM keywords WHERE phrase = ?", (phrase,)))
    return _one(
        conn.execute(
            "SELECT * FROM keywords WHERE phrase = ? AND marketplace = ?",
            (phrase, marketplace),
        )
    )


def get_serp_keyword_phrases(conn: sqlite3.Connection, asin: str, marketplace: str) -> list[str]:
    """Distinct keyword phrases whose SERP in `marketplace` ranked this ASIN,
    best position first. Reads serp_rankings directly (its own marketplace
    column), independent of the collision-prone keywords PK."""
    rows = _all(
        conn.execute(
            """
            SELECT keyword_phrase, MIN(position) AS best
              FROM serp_rankings
             WHERE asin = ? AND marketplace = ?
             GROUP BY keyword_phrase
             ORDER BY best
            """,
            (asin, marketplace),
        )
    )
    return [r["keyword_phrase"] for r in rows]


def get_keywords_for_asin(
    conn: sqlite3.Connection, asin: str, marketplace: str
) -> list[sqlite3.Row]:
    """Keywords whose SERP in `marketplace` ranked this ASIN — the deterministic
    product→keyword-cluster link. Both the SERP row and the keyword row must be
    in `marketplace`, so a US ranking never links an IN keyword."""
    return _all(
        conn.execute(
            """
            SELECT DISTINCT k.*
              FROM keywords k
              JOIN serp_rankings s ON s.keyword_phrase = k.phrase
             WHERE s.asin = ? AND s.marketplace = ? AND k.marketplace = ?
             ORDER BY k.volume DESC NULLS LAST, k.phrase
            """,
            (asin, marketplace, marketplace),
        )
    )


def upsert_serp_ranking(
    conn: sqlite3.Connection,
    *,
    keyword_phrase: str,
    asin: str,
    position: int,
    captured_on: str,
    sponsored: bool = False,
    marketplace: str = "US",
) -> None:
    conn.execute(
        """
        INSERT INTO serp_rankings
            (keyword_phrase, asin, position, sponsored, captured_on, marketplace)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(keyword_phrase, asin, captured_on) DO UPDATE SET
            position = excluded.position,
            sponsored = excluded.sponsored,
            marketplace = excluded.marketplace
        """,
        (keyword_phrase, asin, position, int(sponsored), captured_on, marketplace),
    )


def get_serp_rankings(
    conn: sqlite3.Connection, keyword_phrase: str, marketplace: str | None = None
) -> list[sqlite3.Row]:
    if marketplace is None:
        return _all(
            conn.execute(
                "SELECT * FROM serp_rankings WHERE keyword_phrase = ? ORDER BY position",
                (keyword_phrase,),
            )
        )
    return _all(
        conn.execute(
            """
            SELECT * FROM serp_rankings
             WHERE keyword_phrase = ? AND marketplace = ?
             ORDER BY position
            """,
            (keyword_phrase, marketplace),
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


# ---------------------------------------------------------------------------
# product_matches (cross-marketplace product-family link)
# ---------------------------------------------------------------------------
def upsert_product_match(
    conn: sqlite3.Connection,
    *,
    source_asin: str,
    source_marketplace: str,
    target_asin: str,
    target_marketplace: str,
    match_method: str,
    match_confidence: str,
    match_score: float,
    signals: list[str] | None = None,
    conflicts: list[str] | None = None,
    evidence: str | None = None,
    run_id: str | None = None,
) -> str:
    """Persist (or refresh) a deterministic cross-market identity match. Keyed by
    the (source, target) marketplace pair so a re-run overwrites rather than
    duplicates. Returns the row id."""
    match_id = _new_id()
    conn.execute(
        """
        INSERT INTO product_matches
            (id, run_id, source_asin, source_marketplace, target_asin,
             target_marketplace, match_method, match_confidence, match_score,
             signals, conflicts, evidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_asin, source_marketplace, target_asin, target_marketplace)
        DO UPDATE SET
            run_id = excluded.run_id,
            match_method = excluded.match_method,
            match_confidence = excluded.match_confidence,
            match_score = excluded.match_score,
            signals = excluded.signals,
            conflicts = excluded.conflicts,
            evidence = excluded.evidence,
            created_at = datetime('now')
        """,
        (
            match_id,
            run_id,
            source_asin,
            source_marketplace,
            target_asin,
            target_marketplace,
            match_method,
            match_confidence,
            match_score,
            None if signals is None else _dumps(signals),
            None if conflicts is None else _dumps(conflicts),
            evidence,
        ),
    )
    return match_id


def get_product_match(
    conn: sqlite3.Connection,
    *,
    source_asin: str,
    source_marketplace: str,
    target_asin: str,
    target_marketplace: str,
) -> sqlite3.Row | None:
    return _one(
        conn.execute(
            """
            SELECT * FROM product_matches
             WHERE source_asin = ? AND source_marketplace = ?
               AND target_asin = ? AND target_marketplace = ?
            """,
            (source_asin, source_marketplace, target_asin, target_marketplace),
        )
    )


def get_matches_for_source(
    conn: sqlite3.Connection,
    *,
    source_asin: str,
    source_marketplace: str,
    target_marketplace: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM product_matches WHERE source_asin = ? AND source_marketplace = ?"
    params: tuple[Any, ...] = (source_asin, source_marketplace)
    if target_marketplace is not None:
        sql += " AND target_marketplace = ?"
        params = (source_asin, source_marketplace, target_marketplace)
    sql += " ORDER BY match_score DESC"
    return _all(conn.execute(sql, params))


# ---------------------------------------------------------------------------
# candidates (discovery research queue)
# ---------------------------------------------------------------------------
def upsert_candidate(
    conn: sqlite3.Connection,
    *,
    asin: str,
    marketplace: str,
    source: str,
    source_run_id: str,
    source_ref: str | None = None,
    evidence: Any = None,
    triage_score: float | None = None,
    verdict: str | None = None,
    status: str = "new",
) -> None:
    conn.execute(
        """
        INSERT INTO candidates
            (asin, marketplace, source, source_ref, evidence, source_run_id,
             triage_score, verdict, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(asin, marketplace) DO UPDATE SET
            source = excluded.source,
            source_ref = excluded.source_ref,
            evidence = excluded.evidence,
            source_run_id = excluded.source_run_id,
            triage_score = excluded.triage_score,
            verdict = excluded.verdict,
            status = excluded.status,
            updated_at = datetime('now')
        """,
        (
            asin,
            marketplace,
            source,
            source_ref,
            None if evidence is None else _dumps(evidence),
            source_run_id,
            triage_score,
            verdict,
            status,
        ),
    )


def get_candidate(conn: sqlite3.Connection, asin: str, marketplace: str) -> sqlite3.Row | None:
    return _one(
        conn.execute(
            "SELECT * FROM candidates WHERE asin = ? AND marketplace = ?",
            (asin, marketplace),
        )
    )


def get_candidates_for_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return _all(
        conn.execute(
            """
            SELECT * FROM candidates
             WHERE source_run_id = ?
             ORDER BY triage_score DESC NULLS LAST, asin
            """,
            (run_id,),
        )
    )


# ---------------------------------------------------------------------------
# validations (scored opportunities)
# ---------------------------------------------------------------------------
def upsert_validation(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    asin: str,
    marketplace: str,
    opportunity_score: float | None = None,
    verdict: str | None = None,
    confidence: str | None = None,
    insufficient_data: bool = False,
    scored: Any = None,
) -> str:
    validation_id = _new_id()
    conn.execute(
        """
        INSERT INTO validations
            (id, run_id, asin, marketplace, opportunity_score, verdict,
             confidence, insufficient_data, scored)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, asin, marketplace) DO UPDATE SET
            opportunity_score = excluded.opportunity_score,
            verdict = excluded.verdict,
            confidence = excluded.confidence,
            insufficient_data = excluded.insufficient_data,
            scored = excluded.scored
        """,
        (
            validation_id,
            run_id,
            asin,
            marketplace,
            opportunity_score,
            verdict,
            confidence,
            int(insufficient_data),
            None if scored is None else _dumps(scored),
        ),
    )
    return validation_id


def get_validation(
    conn: sqlite3.Connection, *, run_id: str, asin: str, marketplace: str
) -> sqlite3.Row | None:
    return _one(
        conn.execute(
            "SELECT * FROM validations WHERE run_id = ? AND asin = ? AND marketplace = ?",
            (run_id, asin, marketplace),
        )
    )
