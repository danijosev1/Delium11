-- Agent layer persistence (docs/agent-layer.md §3, §4, §6).
--
-- Additive and backwards compatible. The deterministic engines already consume
-- structured Review Miner evidence (differentiation.py: themes + feature_requests
-- + bundle_signals); this migration gives that evidence a durable home so a
-- validation is fully reproducible and a report is regenerable from the DB with
-- NO further LLM call (agent-layer §6).
--
-- Notes on scope (deliberate, not convenience):
--   * review_themes.kind is left unchanged — the differentiation engine reads
--     bundle signals from a SEPARATE input (DifferentiationInput.bundle_signals),
--     never as a BUNDLE theme kind, so bundle evidence goes in its own table
--     rather than widening the kind CHECK.
--   * review_themes only gains the structured fields the engine already reads
--     (addressability / cogs_delta / category) via additive ALTERs.

-- Structured fields the Review Miner supplies and differentiation.py consumes
-- (F3 addressability, F4 packaging/usage rubric). Nullable — absence is "unknown".
ALTER TABLE review_themes ADD COLUMN addressability TEXT;   -- fixable|partial|hard|unknown
ALTER TABLE review_themes ADD COLUMN cogs_delta REAL;       -- fraction; ≤0.15 → fixable
ALTER TABLE review_themes ADD COLUMN category TEXT;         -- e.g. 'packaging', 'usage'

-- Missing-feature requests (differentiation F2). Distinct from themes because the
-- engine needs `absent_from_competitors`, which review_themes has no column for.
CREATE TABLE feature_requests (
    id                      TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    asin                    TEXT NOT NULL REFERENCES products (asin) ON DELETE CASCADE,
    feature                 TEXT NOT NULL,
    supporting_review_ids   TEXT NOT NULL,   -- JSON: review_id array (>=3 upstream)
    absent_from_competitors INTEGER CHECK (absent_from_competitors IN (0, 1)),  -- null = unknown
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_feature_requests_asin ON feature_requests (asin);
CREATE INDEX idx_feature_requests_run ON feature_requests (run_id);

-- Bundle/complement signals (differentiation F4a). Kept separate from themes.
CREATE TABLE bundle_signals (
    id                    TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    asin                  TEXT NOT NULL REFERENCES products (asin) ON DELETE CASCADE,
    complement            TEXT NOT NULL,
    supporting_review_ids TEXT NOT NULL,     -- JSON: review_id array (>=3 upstream)
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_bundle_signals_asin ON bundle_signals (asin);
CREATE INDEX idx_bundle_signals_run ON bundle_signals (run_id);

-- Audit + reproducibility for every LLM agent invocation: which model/provider
-- ran, what it cost, and the VALIDATED structured output (JSON) — so the report
-- regenerates from data and a run is fully auditable (agent-layer §6, docs §11).
CREATE TABLE agent_runs (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    asin         TEXT NOT NULL,
    marketplace  TEXT NOT NULL,
    agent        TEXT NOT NULL
                     CHECK (agent IN ('scout', 'analyst', 'review_miner', 'strategist')),
    model        TEXT,
    provider     TEXT,
    status       TEXT NOT NULL DEFAULT 'ok'
                     CHECK (status IN ('ok', 'degraded', 'failed')),
    cost_usd     REAL NOT NULL DEFAULT 0,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    output       TEXT,          -- JSON: validated agent output (MinerReport / StrategistVerdict)
    error        TEXT,          -- validation/provider error summary when degraded/failed
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_agent_runs_asin ON agent_runs (asin, marketplace);
CREATE INDEX idx_agent_runs_run ON agent_runs (run_id);
