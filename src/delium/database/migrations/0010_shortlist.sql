-- Shortlist + product snapshots (Phase 1 workspace).
--
-- `shortlist` is the seller's working set of products under research, with a
-- status and free-text notes. `product_snapshots` records a point-in-time copy
-- of a product's key Keepa metrics each time it is fetched or re-checked, so the
-- workspace can show whether momentum held over time — this snapshot series is
-- also the evidence base for the Phase-2 launch simulator. Additive only.

CREATE TABLE shortlist (
    asin         TEXT NOT NULL,
    marketplace  TEXT NOT NULL DEFAULT 'US',
    status       TEXT NOT NULL DEFAULT 'researching'
                     CHECK (status IN ('researching', 'sampling', 'rejected', 'launched')),
    notes        TEXT,
    added_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (asin, marketplace)
);

CREATE TABLE product_snapshots (
    id             TEXT PRIMARY KEY,
    asin           TEXT NOT NULL,
    marketplace    TEXT NOT NULL DEFAULT 'US',
    captured_at    TEXT NOT NULL DEFAULT (datetime('now')),
    run_id         TEXT REFERENCES runs (id) ON DELETE SET NULL,
    price_cents    INTEGER,
    bsr            INTEGER,
    review_count   INTEGER,
    rating         REAL,
    monthly_sold   INTEGER,
    emergence_score REAL,
    opportunity_score REAL,
    verdict        TEXT,
    confidence     TEXT
);

CREATE INDEX idx_product_snapshots_asin ON product_snapshots (asin, marketplace, captured_at);
