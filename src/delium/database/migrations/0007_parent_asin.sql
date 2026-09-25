-- Variation parent ASIN (Keepa `parentAsin`).
--
-- Amazon lists each colour/size/pack variation as its own child ASIN under one
-- parent listing. The Product Finder returns children, so a single parent can
-- flood a run with near-identical rows (same age, reviews, BSR band). Storing
-- the parent lets discovery/emerging/cross-market collapse variations to ONE
-- representative row + a variation count, instead of showing eight of the same
-- product. Purely additive; NULL when Keepa reports no parent (a standalone
-- product is its own listing).
--
-- No old migration is modified.

ALTER TABLE products ADD COLUMN parent_asin TEXT;

-- Group-by-parent lookups (dedupe within a marketplace).
CREATE INDEX idx_products_parent ON products (parent_asin, marketplace);
