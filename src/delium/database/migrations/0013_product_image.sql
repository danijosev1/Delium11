-- Main product image URL (Daily Scan Command Center, Part 3B).
--
-- Captured during Keepa normalization from `imagesCSV` (the first image is the
-- listing's main image) so the Scan Inbox can render product cards without an
-- extra fetch. Additive only; NULL when Keepa returned no image.

ALTER TABLE products ADD COLUMN image_url TEXT;
