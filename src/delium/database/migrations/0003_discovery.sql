-- Discovery / scout orchestration persistence (ARCHITECTURE.md §9).
--
-- Additive and backwards compatible. Two tables the architecture specified but
-- that had not yet been built:
--   candidates  — the discovered research queue (dedup identity + provenance)
--   validations — a scored opportunity for a candidate (the ScoredOpportunity)
--
-- ASINs are marketplace-specific, so both tables key on (asin, marketplace),
-- consistent with the marketplace-scoping introduced for cross-market. Every
-- row carries the discovery run that produced it (runs.id) for full provenance.

CREATE TABLE candidates (
    asin           TEXT NOT NULL,
    marketplace    TEXT NOT NULL,
    source         TEXT NOT NULL,          -- 'keyword' | 'cross_market' | 'explicit'
    source_ref     TEXT,                   -- seed keyword / source marketplace / 'user'
    evidence       TEXT,                   -- JSON: merged discovery evidence
    source_run_id  TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    triage_score   REAL,                   -- opportunity score from scoring.py (nullable)
    verdict        TEXT,                   -- scoring verdict (buy|test|avoid) or null
    status         TEXT NOT NULL DEFAULT 'new'
                       CHECK (status IN ('new', 'shortlist', 'validated', 'rejected', 'watching')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (asin, marketplace)
);

CREATE INDEX idx_candidates_run ON candidates (source_run_id);
CREATE INDEX idx_candidates_status ON candidates (status);

-- One scored opportunity per (run, asin, marketplace): the full deterministic
-- ScoredOpportunity is stored as JSON so any ranking/verdict is reproducible
-- without re-running the engines.
CREATE TABLE validations (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    asin             TEXT NOT NULL,
    marketplace      TEXT NOT NULL,
    opportunity_score REAL,
    verdict          TEXT,
    confidence       TEXT,
    insufficient_data INTEGER NOT NULL DEFAULT 0 CHECK (insufficient_data IN (0, 1)),
    scored           TEXT,                 -- JSON: full ScoredOpportunity snapshot
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (run_id, asin, marketplace)
);

CREATE INDEX idx_validations_asin ON validations (asin, marketplace);
