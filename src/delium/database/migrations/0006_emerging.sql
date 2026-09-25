-- Emerging-products discovery persistence (docs/emerging.md).
--
-- Additive and backwards compatible. `emerging` finds recently-launched products
-- gaining traction (Keepa Product Finder), computes a deterministic emergence
-- signal, and routes the top N through the EXISTING scoring pipeline. These
-- tables record each run and its candidates so the UI History page can show
-- them and runs can be compared over time. They store the emergence signal and
-- the run's provenance only — the opportunity score/verdict continue to live in
-- the shared `validations` table (scoring.py remains the sole verdict owner).
--
-- No old migration is modified.

CREATE TABLE emerging_runs (
    id                   TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    marketplace          TEXT NOT NULL,
    categories           TEXT,               -- JSON: category ids searched
    page_size            INTEGER NOT NULL,
    top_n                INTEGER NOT NULL,
    finder_total_results INTEGER,            -- Keepa Product Finder totalResults
    finder_tokens        INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE emerging_candidates (
    id                TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    asin              TEXT NOT NULL,
    marketplace       TEXT NOT NULL,
    emergence_score   REAL,                  -- 0-100 emergence signal (NOT the opportunity score)
    age_days          INTEGER,
    outcome           TEXT NOT NULL,          -- 'scored' | 'killed' | 'unresolved'
    opportunity_score REAL,                   -- from scoring.py (null if killed/unresolved)
    verdict           TEXT,
    confidence        TEXT,
    kill_rule         TEXT,                   -- triggering hard-kill id when killed
    signals           TEXT,                   -- JSON: emergence sub-signal breakdown
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_emerging_candidates_run ON emerging_candidates (run_id);
CREATE INDEX idx_emerging_candidates_asin ON emerging_candidates (asin, marketplace);
CREATE INDEX idx_emerging_runs_run ON emerging_runs (run_id);
