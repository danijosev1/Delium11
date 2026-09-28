-- Per-scan spending caps on the Research Profile (Daily Scan Part 3).
--
-- The scheduler and `delium scan` read these as the default Keepa-token and USD
-- caps: a scan aborts in preflight before spending if the projection exceeds
-- them. Conservative defaults suit an unattended daily run. Additive only.

ALTER TABLE research_profiles ADD COLUMN max_scan_usd REAL NOT NULL DEFAULT 5.0;
ALTER TABLE research_profiles ADD COLUMN keepa_token_cap INTEGER NOT NULL DEFAULT 1500;
