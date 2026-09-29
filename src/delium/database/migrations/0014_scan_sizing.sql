-- Per-scan sizing defaults on the Research Profile (Daily Scan pacing fix).
--
-- sweep_size     = how many raw ASINs the sweep brings back (drives hydrate cost)
-- competitor_sets = how many top products get a page-one competitor set
-- The CLI uses these when --sweep-size / --competitor-sets are omitted, so an
-- unattended run is sized by the active profile. Additive only.

ALTER TABLE research_profiles ADD COLUMN scan_sweep_size INTEGER NOT NULL DEFAULT 300;
ALTER TABLE research_profiles ADD COLUMN scan_competitor_sets INTEGER NOT NULL DEFAULT 40;
