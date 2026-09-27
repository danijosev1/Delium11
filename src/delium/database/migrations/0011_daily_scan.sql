-- Daily Scan pipeline persistence (docs: README "Daily Scan").
--
-- A scan is a resumable, multi-stage funnel. Each stage's progress is persisted
-- so a crash resumes from the last completed stage; per-stage token/dollar cost
-- and funnel counts are recorded for the scan report. Finalists land in a "scan
-- inbox" (scan_candidates) that is SEPARATE from the shortlist — the seller
-- promotes to the shortlist manually. Additive only.

CREATE TABLE scans (
    id                TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    marketplaces      TEXT NOT NULL,        -- JSON: ['US', ...] (requested)
    profile_id        TEXT,
    profile_name      TEXT,
    status            TEXT NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'complete', 'failed', 'aborted')),
    stage             INTEGER NOT NULL DEFAULT 0,   -- last COMPLETED stage index
    params            TEXT,                 -- JSON: top_n, budget_cap, max_spend, min_confidence…
    keepa_tokens      INTEGER NOT NULL DEFAULT 0,
    data_usd          REAL NOT NULL DEFAULT 0,
    llm_usd           REAL NOT NULL DEFAULT 0,
    notes             TEXT,                 -- JSON: messages (e.g. "AU pending")
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE scan_stages (
    id            TEXT PRIMARY KEY,
    scan_id       TEXT NOT NULL REFERENCES scans (id) ON DELETE CASCADE,
    stage         INTEGER NOT NULL,         -- 0..8
    name          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'running', 'complete', 'skipped', 'failed')),
    input_count   INTEGER NOT NULL DEFAULT 0,
    output_count  INTEGER NOT NULL DEFAULT 0,
    killed_count  INTEGER NOT NULL DEFAULT 0,
    keepa_tokens  INTEGER NOT NULL DEFAULT 0,
    data_usd      REAL NOT NULL DEFAULT 0,
    llm_usd       REAL NOT NULL DEFAULT 0,
    detail        TEXT,                     -- JSON: funnel detail, kill reasons summary…
    started_at    TEXT,
    finished_at   TEXT,
    UNIQUE (scan_id, stage)
);

CREATE TABLE scan_candidates (
    id                   TEXT PRIMARY KEY,
    scan_id              TEXT NOT NULL REFERENCES scans (id) ON DELETE CASCADE,
    asin                 TEXT NOT NULL,
    marketplace          TEXT NOT NULL,
    parent_asin          TEXT,
    outcome              TEXT NOT NULL DEFAULT 'swept',
                         -- swept | hydrated | killed | scored | competitor_set | ranked | finalist
    stage_reached        INTEGER NOT NULL DEFAULT 1,
    source               TEXT,             -- 'finder' | 'cross_market'
    kill_rule            TEXT,
    established_brand    INTEGER NOT NULL DEFAULT 0 CHECK (established_brand IN (0, 1)),
    cheap_score          REAL,
    opportunity_score    REAL,
    launchability        REAL,
    sellability          REAL,
    confidence           TEXT,
    verdict              TEXT,
    rank                 INTEGER,
    differentiation_status TEXT,           -- 'pending' | 'done' | 'n/a'
    reason               TEXT,
    data                 TEXT,             -- JSON snapshot (facts + component breakdown)
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (scan_id, asin, marketplace)
);

CREATE TABLE scan_categories (
    id             TEXT PRIMARY KEY,
    scan_id        TEXT NOT NULL REFERENCES scans (id) ON DELETE CASCADE,
    category       TEXT NOT NULL,
    momentum_score REAL,
    metrics        TEXT,                   -- JSON: CategoryMetrics
    reason         TEXT
);

CREATE INDEX idx_scan_candidates_scan ON scan_candidates (scan_id, outcome);
CREATE INDEX idx_scan_stages_scan ON scan_stages (scan_id, stage);
CREATE INDEX idx_scans_status ON scans (status, created_at);
