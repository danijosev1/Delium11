-- Research profiles (Phase 1 UI): one shared, named set of seller preferences
-- that drives every finder/search default and every profit calculation. This is
-- PREFERENCE state, layered on top of the engine — it never relaxes a hard kill,
-- gate, or scoring weight (those live in config + scoring.py). Exactly one row is
-- active at a time (is_active = 1); the UI edits these and switches presets.
--
-- Money is stored in integer cents; percentages as 0-1 fractions. Nullable fields
-- mean "no preference" and fall back to the engine defaults. Additive only.

CREATE TABLE research_profiles (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL UNIQUE,
    is_active             INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    -- Budget + return targets.
    budget_usd            REAL,                 -- total capital per product
    target_net_margin     REAL,                 -- 0-1; highlight/sort only (not G1)
    target_roi            REAL,                 -- e.g. 1.5 = 150%; highlight/sort only
    -- Sell-price band + demand/competition preferences (finder defaults).
    price_min_cents       INTEGER,
    price_max_cents       INTEGER,
    min_monthly_sales     INTEGER,
    max_reviews           INTEGER,
    max_weight_g          INTEGER,
    max_size_tier         TEXT,                 -- e.g. 'large_standard'
    -- Categories + marketplaces (JSON arrays).
    preferred_categories  TEXT,                 -- JSON: [str, ...]
    excluded_categories   TEXT,                 -- JSON: [str, ...] (soft filter, not K12)
    marketplaces          TEXT,                 -- JSON: ['US', ...]
    -- Profit assumptions used everywhere unless overridden per product.
    cogs_mode             TEXT NOT NULL DEFAULT 'pct'
                              CHECK (cogs_mode IN ('pct', 'unit')),
    cogs_value            REAL NOT NULL DEFAULT 0.25,  -- pct → 0-1; unit → USD/unit
    freight_per_kg_usd    REAL NOT NULL DEFAULT 6.0,
    -- Risk appetite: sorting/highlighting ONLY (never the hard rules).
    risk_tolerance        TEXT NOT NULL DEFAULT 'balanced'
                              CHECK (risk_tolerance IN ('conservative', 'balanced', 'aggressive')),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- At most one active profile (partial unique index).
CREATE UNIQUE INDEX idx_research_profiles_active
    ON research_profiles (is_active) WHERE is_active = 1;
