-- Delium initial schema (docs/data-layer.md §2).
--
-- Two zones:
--   raw cache  — whole provider payloads, replayable, doubles as fetch log + spend ledger
--   extracted  — typed rows the deterministic formulas query
--
-- Every extracted row carries the fetch_id (or run_id) it came from, so every
-- number in every report traces back to a logged API call.
--
-- Timestamps/dates are stored as ISO-8601 TEXT (SQLite has no native date type).
-- Booleans are INTEGER 0/1 with CHECK constraints.

-- ---------------------------------------------------------------------------
-- Supporting parent table (ARCHITECTURE.md §9). Present here because it is the
-- FK parent for run_id columns; the full runs/candidates/validations surface is
-- built in a later step.
-- ---------------------------------------------------------------------------
CREATE TABLE runs (
    id              TEXT PRIMARY KEY,
    command         TEXT NOT NULL,
    input           TEXT,
    status          TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'complete', 'failed', 'degraded')),
    data_cost_usd   REAL NOT NULL DEFAULT 0,
    llm_cost_usd    REAL NOT NULL DEFAULT 0,
    config_snapshot TEXT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT
);

-- ---------------------------------------------------------------------------
-- Raw cache / fetch log / spend ledger
-- ---------------------------------------------------------------------------
CREATE TABLE raw_fetches (
    id           TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    request_key  TEXT NOT NULL,          -- e.g. 'keepa:product:B0...'
    payload      TEXT NOT NULL,          -- JSON
    cost_usd     REAL NOT NULL DEFAULT 0,
    tokens_used  INTEGER NOT NULL DEFAULT 0,
    http_status  INTEGER,
    run_id       TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cache lookup: newest row per (provider, request_key).
CREATE INDEX idx_raw_fetches_lookup ON raw_fetches (provider, request_key, fetched_at DESC);
-- Run replay / audit.
CREATE INDEX idx_raw_fetches_run ON raw_fetches (run_id);

-- ---------------------------------------------------------------------------
-- Extracted: products
-- ---------------------------------------------------------------------------
CREATE TABLE products (
    asin              TEXT PRIMARY KEY,
    marketplace       TEXT NOT NULL DEFAULT 'US',
    title             TEXT,
    brand             TEXT,
    category_path     TEXT,
    listing_date      TEXT,
    dims_json         TEXT,              -- JSON: {length_mm, width_mm, height_mm}
    weight_g          INTEGER,
    size_tier         TEXT,
    images_count      INTEGER,
    amazon_on_listing INTEGER NOT NULL DEFAULT 0 CHECK (amazon_on_listing IN (0, 1)),
    fetch_id          TEXT NOT NULL REFERENCES raw_fetches (id),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only longitudinal record; accumulates our own history beyond any
-- provider's window. Upserted from each Keepa history payload.
CREATE TABLE price_bsr_history (
    asin         TEXT NOT NULL REFERENCES products (asin) ON DELETE CASCADE,
    captured_on  TEXT NOT NULL,          -- date 'YYYY-MM-DD'
    price_cents  INTEGER,
    bsr          INTEGER,
    offer_count  INTEGER,
    review_count INTEGER,
    rating       REAL,
    PRIMARY KEY (asin, captured_on)
);

-- Recomputed on every refresh; the deterministic formulas read THIS, not raw JSON.
CREATE TABLE product_derived (
    asin                 TEXT PRIMARY KEY REFERENCES products (asin) ON DELETE CASCADE,
    est_units_low        INTEGER,
    est_units_high       INTEGER,
    bsr_slope_90d        REAL,
    price_cv_90d         REAL,
    review_velocity_mo   REAL,
    seasonality_peak_pct REAL,
    history_days         INTEGER,
    computed_at          TEXT NOT NULL DEFAULT (datetime('now')),
    fetch_id             TEXT NOT NULL REFERENCES raw_fetches (id)
);

-- ---------------------------------------------------------------------------
-- Extracted: keywords & rankings
-- ---------------------------------------------------------------------------
CREATE TABLE keywords (
    phrase        TEXT PRIMARY KEY,      -- lowercased/trimmed by the adapter
    marketplace   TEXT NOT NULL DEFAULT 'US',
    volume        INTEGER,
    volume_series TEXT,                  -- JSON: 12-month series
    cpc_cents     INTEGER,
    fetch_id      TEXT NOT NULL REFERENCES raw_fetches (id),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- SERP asins are intentionally NOT FK'd to products: a SERP may list ASINs we
-- have not (yet) fetched product data for.
CREATE TABLE serp_rankings (
    keyword_phrase TEXT NOT NULL REFERENCES keywords (phrase) ON DELETE CASCADE,
    asin           TEXT NOT NULL,
    position       INTEGER NOT NULL,
    sponsored      INTEGER NOT NULL DEFAULT 0 CHECK (sponsored IN (0, 1)),
    captured_on    TEXT NOT NULL,        -- date 'YYYY-MM-DD'
    PRIMARY KEY (keyword_phrase, asin, captured_on)
);

CREATE INDEX idx_serp_rankings_asin ON serp_rankings (asin);

-- ---------------------------------------------------------------------------
-- Extracted: competitive sets
-- ---------------------------------------------------------------------------
-- Freezes WHICH ASINs a validation compared against, so re-runs build a new
-- set and old reports stay interpretable.
CREATE TABLE competitor_sets (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    target_asin      TEXT NOT NULL,
    member_asins     TEXT NOT NULL,      -- JSON: ordered ASIN array
    selection_method TEXT NOT NULL
                         CHECK (selection_method IN ('serp_top', 'keepa_category', 'manual')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_competitor_sets_run ON competitor_sets (run_id);
CREATE INDEX idx_competitor_sets_target ON competitor_sets (target_asin);

-- ---------------------------------------------------------------------------
-- Extracted: reviews & mined themes
-- ---------------------------------------------------------------------------
CREATE TABLE reviews (
    review_id     TEXT PRIMARY KEY,
    asin          TEXT NOT NULL REFERENCES products (asin) ON DELETE CASCADE,
    stars         INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    review_date   TEXT,
    title         TEXT,
    body          TEXT,
    verified      INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    helpful_votes INTEGER NOT NULL DEFAULT 0,
    fetch_id      TEXT NOT NULL REFERENCES raw_fetches (id)
);

CREATE INDEX idx_reviews_asin ON reviews (asin);

-- Review Miner OUTPUT stored as data: citations (quote_review_ids) resolve to
-- real review rows; themes are queryable across markets over time.
CREATE TABLE review_themes (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    asin             TEXT NOT NULL REFERENCES products (asin) ON DELETE CASCADE,
    kind             TEXT NOT NULL
                         CHECK (kind IN ('complaint', 'praise', 'missing_feature', 'improvement')),
    theme            TEXT NOT NULL,
    frequency_pct    REAL,
    severity         INTEGER CHECK (severity BETWEEN 1 AND 3),
    quote_review_ids TEXT NOT NULL       -- JSON: review_id array (>=3 upstream)
);

CREATE INDEX idx_review_themes_asin ON review_themes (asin);
CREATE INDEX idx_review_themes_run ON review_themes (run_id);
