-- Cross-market discovery support (docs/cross-market.md §15).
--
-- Additive and backwards compatible: new nullable columns on `products`, a
-- marketplace provenance column on `serp_rankings`, a marketplace index, and a
-- new `product_matches` table linking one underlying product across two
-- marketplaces. Nothing existing is dropped or re-typed.

-- Product identity fields (Keepa eanList/upcList → gtin; manufacturer). These
-- let cross-market identity matching reach an EXACT (GTIN) match from stored
-- data instead of relying on caller-supplied barcodes.
ALTER TABLE products ADD COLUMN gtin TEXT;
ALTER TABLE products ADD COLUMN manufacturer TEXT;

-- Cross-market lookups filter products by marketplace; index it.
CREATE INDEX idx_products_marketplace ON products (marketplace);
CREATE INDEX idx_products_gtin ON products (gtin);

-- SERP rows carry their marketplace so a US SERP never satisfies an IN read.
-- Constant DEFAULT keeps the ALTER additive over existing rows.
ALTER TABLE serp_rankings ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'US';

-- ---------------------------------------------------------------------------
-- Cross-marketplace product-family link. A deterministic match between a source
-- listing and a target-marketplace listing, regenerable and auditable: the
-- method, confidence, score, and the signals/conflicts that produced it are all
-- persisted with run provenance.
-- ---------------------------------------------------------------------------
CREATE TABLE product_matches (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT REFERENCES runs (id) ON DELETE SET NULL,
    source_asin        TEXT NOT NULL,
    source_marketplace TEXT NOT NULL,
    target_asin        TEXT NOT NULL,
    target_marketplace TEXT NOT NULL,
    match_method       TEXT NOT NULL,      -- 'gtin' | 'fuzzy' | 'projected' | 'manual'
    match_confidence   TEXT NOT NULL,      -- exact | strong | probable | weak | unmatched
    match_score        REAL NOT NULL,
    signals            TEXT,               -- JSON: signals used
    conflicts          TEXT,               -- JSON: conflicting signals
    evidence           TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source_asin, source_marketplace, target_asin, target_marketplace)
);

CREATE INDEX idx_product_matches_source
    ON product_matches (source_asin, source_marketplace);
CREATE INDEX idx_product_matches_target
    ON product_matches (target_asin, target_marketplace);
