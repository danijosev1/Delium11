-- Analyst agent persistence (docs/agent-layer.md §2).
--
-- Additive and backwards compatible. The Analyst extracts a competitor feature
-- matrix — the *claimed* (observable) features per listing — which the
-- deterministic differentiation engine consumes to decide, conservatively,
-- whether a review-requested feature is absent from the top-10 (F2) and whether
-- competitors already offer a bundle complement (F4). Kept in its own table so
-- the derivation is reproducible from the DB with no further LLM call, and clean
-- of the Review Miner's customer-evidence tables.
--
-- No old migration is modified.

CREATE TABLE competitor_features (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    asin       TEXT NOT NULL REFERENCES products (asin) ON DELETE CASCADE,
    feature    TEXT NOT NULL,     -- a feature CLAIMED in this listing's observable text
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_competitor_features_asin ON competitor_features (asin);
CREATE INDEX idx_competitor_features_run ON competitor_features (run_id);
