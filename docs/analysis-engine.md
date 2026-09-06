# Delium — V1 Deterministic Analysis Engine

**Contract:** `analysis/` is pure computation — same inputs, same outputs, forever. No LLM imports (lint-enforced), no network, no clock reads except via passed-in `as_of` date. It consumes the normalized tables from docs/data-layer.md and the structured Review Miner output, and produces every metric docs/scoring-model.md references. AI *interprets* these numbers downstream; nothing in this package can hallucinate because nothing in it generates.

Conventions used by all modules:

- **`norm(x, lo, hi)`** → linear map to 0–100, clipped. **`log_norm`** → same in log₁₀ space. **`sweet_spot(x, rise_lo, rise_hi, decay_hi, floor)`** → piecewise: 0 below `rise_lo`, linear rise to 100 across [rise_lo, rise_hi], flat, linear decay to `floor` at `decay_hi`+.
- **Ranges over points**: any estimated quantity is `(low, high)`; gates evaluate `low` (conservative), ranking uses midpoint (scoring-model §11.4).
- **Every output is a frozen dataclass** carrying `value(s)`, `confidence (high|medium|low)`, `inputs_used` (fetch_ids), and `notes[]` (machine-generated caveats that flow into the report's methodology section verbatim).
- **Missing-data behavior is per-field and explicit** — a module never throws on absent optional data; it returns the field as `null` + a note + degraded confidence, and `scoring.py` applies the caps from scoring-model §3. Only fee-blocking fields (dims/weight/category) abort, per data-layer §4.

---

## 1. `demand.py` — Demand Analysis

**Inputs:** `product_derived` + `price_bsr_history` rows for target + top-10 competitors; `keywords`/`keyword_clusters` for the market cluster.

| Output | Formula |
|---|---|
| `est_units_range` (per ASIN) | **Rank-drop method**: count discrete BSR drops over trailing 90d (a drop ≈ ≥1 sale event), scale by drop-size heuristic and category BSR→velocity curve (piecewise table per top-level category, versioned in `curves_data/bsr_velocity/`). Bounds: `low = drops × 0.8`, `high = drops × 1.6 × multi_unit_factor` (category-typical units/order). Requires ≥30d history; 30–59d widens bounds ×1.5 |
| `market_units_range` | Σ over top-10 `est_units_range`, then ×1.25 to account for page-1 share ≈ 80% of cluster demand |
| `sales_velocity_score` (D2) | `sweet_spot(median_top10_units_mid, 150, 300→1200, 2500, floor 70)` |
| `search_volume_score` (D1) | `log_norm(cluster_volume, 2000, 40000)` where `cluster_volume` = Σ member volumes, deduped by phrase containment (a phrase that is a substring of a higher-volume member contributes 30% weight to avoid double count) |
| `bsr_trend_score` (D3) | **Theil–Sen slope** of log(BSR) over 90d per ASIN (robust to spike outliers — OLS is banned here; single lightning-deal spikes wreck it), median across top-10, mapped `norm(−slope_annualized, −20%, +40%)` |
| `market_growth_score` (D4) | `norm(volume_yoy, −10%, +40%)` from 12-mo `volume_series`; if series < 12mo → null + note (K10 handles the fad case separately) |
| `seasonality_score` (D5) & `peak_concentration` | From 365d BSR pattern: inverse-BSR as demand proxy, weekly-binned; `peak_concentration` = best-8-week share of annual proxy demand. Score `100 − norm(peak, 20%, 60%)`. <365d history → null score, `peak = null`, note "seasonality unassessed" (risk engine then applies a −10 *unknown-seasonality* deduction instead of the −20 confirmed one) |

**Confidence:** high = ≥8 of top-10 with ≥60d history and cluster ≥5 volumed phrases; medium = sufficiency "partial" band; low otherwise. `est_units_range` confidence additionally requires the category to exist in the BSR→velocity table (else bounds widen ×2 and confidence caps at medium).

---

## 2. `competition.py` — Competition Analysis

**Inputs:** `products` + `product_derived` for top-10; `serp_rankings`; Analyst listing-rubric output (structured, observable fields only); price history.

| Output | Formula |
|---|---|
| `review_stats` | median / mean / min of top-10 review counts; count with <150 (`beatable_slots`) |
| `review_moat_score` (C1) | `100 − log_norm(median_reviews, 200, 3000)` |
| `beatable_slots_score` (C2) | `min(beatable_slots, 4) × 25` |
| `review_velocity` (C3) | per-leader: Δreview_count over trailing 90d from `price_bsr_history` ÷ 3 → monthly; median of top-3; score `100 − log_norm(v, 20, 300)`. Negative deltas (review purges) clamp to 0 with note |
| `brand_concentration` (C4) | slot share of modal brand in top-10 + **HHI** over brand slot shares (both reported; score per scoring-model C4 formula; HHI > 0.30 adds a risk-engine flag) |
| `listing_quality` (C5) | mean of Analyst rubric (0–10: images≥7, video, A+ present, title keyword coverage, bullets structured, review-responding brand) across top-10 → score `100 − mean × 10`. **Rubric fields are counts/booleans the Analyst extracts, not opinions** — spot-checkable against the listing |
| `price_competition_score` (C6) | median of per-ASIN 90d price CV → `100 − norm(cv, 5%, 25%)`; additionally `price_war_flag` = true if ≥3 of top-10 hit a 90d price low within the last 14d simultaneously |
| `competition_score` | weighted per scoring-model §5 |

**Missing data:** absent price history → C6 = 50 neutral + note; missing listing rubric (Analyst step failed) → C5 = 50 neutral + note + pillar confidence low. Fewer than 10 resolvable competitors → compute over what exists, note "top-N only," confidence per sufficiency table.

**Implementation note (C5 / Analyst rubric).** As built, `listing_quality` (C5) is computed **deterministically** from the observable listing facts we actually persist (title, `images_count`, review count, rating, price vs. competitors, keyword coverage) via `analysis/listing.py` — it is *not* wired to the Analyst's `listing_rubric`. Most rubric fields (video, A+, structured bullets, review-responding brand) are not persisted from the listing, so feeding LLM guesses into a scored pillar would violate the "observable, spot-checkable" rule. The Analyst `listing_rubric` is therefore produced for the **report only** (advisory competitive context); the only Analyst output that reaches a scored pillar is the `feature_matrix` (via F2/F4b below). This keeps the LLM out of competition scoring entirely.

---

## 3. `differentiation.py` — Differentiation Analysis

**Inputs:** `review_themes` rows (Review Miner output — already citation-gated: themes with <3 supporting quotes were discarded upstream); `review_fetch_meta` (sample size + bias delta); top-10 feature matrix (Analyst-extracted: which listed features each competitor claims).

The module is deterministic math **over** LLM-extracted structure — the LLM found the themes; the arithmetic, weighting, and scores happen here and are reproducible from the stored `review_themes` rows.

| Output | Formula |
|---|---|
| `complaint_intensity` (F1) | `Σ complaints: frequency_pct × severity(1–3)` → `norm(x, 10, 60)`. Frequencies are recomputed here from `quote_review_ids` counts ÷ sample size — the Miner's own claimed percentages are cross-checked and the *recomputed* value wins (LLM numbers are never trusted arithmetic) |
| `missing_features_score` (F2) | features requested in themes ∧ absent from all top-10 feature matrices: `min(count, 4) × 25` |
| `addressability` (F3) | share of complaint-intensity mass whose themes carry Strategist `addressable=true, cogs_delta ≤ 15%` tags → `share × 100`; Strategist tags are boolean/enum (structured), the share math is ours |
| `bundle_packaging_score` (F4) | rubric per scoring-model F4: 25 pts each for (complement mentioned in ≥3% of reviews) · (no top-10 bundles it) · (packaging-damage theme ≥5%) · (usage-confusion theme ≥5%) |
| `sample_bias_note` | `sample_rating_avg − listing_rating_avg`; if > +0.4 stars, complaints are likely *under*-represented → note appended and F1 gets a `+5 latent-complaint adjustment` (bounded, documented) |

**Confidence:** high ≥150 reviews & bias delta <0.4; medium 30–149; low <30 (pillar capped 50 by scoring). Zero reviews → all outputs null, pillar `missing`, verdict cap Test (data-layer §1.3).

**Implementation note (F2 / F4b — Analyst feature matrix).** The "absent from all top-10 feature matrices" input to F2, and the "no top-10 bundles it" input to F4b, are derived in `validation/evidence.py` from the persisted `competitor_features` rows (the Analyst's claimed-feature matrix), **conservatively and coverage-gated**:
- A requested feature is marked `absent_from_competitors = True` **only** when at least `agents.min_competitor_feature_coverage` competitors were analyzed (have persisted features) *and* none of them claim the feature (generous fuzzy present-matching against each competitor's claimed features + observable listing text). If the coverage gate is not met, or any competitor plausibly claims it, the flag stays `False`/`None` — **"not mentioned" is never treated as "absent."** F2 additionally requires the feature to carry ≥ `min_quotes` verified review citations before it counts.
- `competitors_bundle_complement` is `False` (an opening → F4b awards) only when the coverage gate is met and no analyzed competitor offers the customer-requested complement; `True` when at least one does; `None` (no award) otherwise.

The persisted matrix holds **competitor** claimed features only; each was verified (fuzzy ≥ `agents.feature_match_threshold`) to appear in that listing's own observable text by the agent runner before persistence, so the derivation is reproducible from the DB with no further LLM call.

---

## 4. `fees.py` + `profit.py` — Profitability Engine (no AI, ever)

### 4.1 `fees.py`

**Inputs:** dims, weight, category, target price. **Fee data lives in versioned YAML** (`analysis/fee_tables/us-2026.yaml`) with `effective_date` — fees change ~yearly; updating fees is a data edit + test-fixture update, not a code change.

- `size_tier(dims, weight)` → Amazon's published decision table (small-standard → large-bulky), computed on packaged-dims assumption (+10% dims allowance, config).
- `fulfillment_fee(tier, weight)` → published FBA rate card lookup + interpolation rules.
- `referral_fee(category, price)` → category % table with minimums.
- `storage_fee_monthly(tier, volume_ft³, month)` → standard vs. Q4 rates; annualized average used in unit economics, Q4 rate reported separately.
- Output includes the exact table rows used (`fee_table_version`, row keys) in `inputs_used`.

### 4.2 `profit.py`

**Inputs:** fees output; market price stats; `est_units_range`; config assumption set; optional CLI overrides (`--cogs 4.20 --freight 1.10` once real supplier quotes exist — overrides recorded in the run's `config_snapshot`).

```
price            = median top-10 buybox price (not target's — I price to market)
landed_cost      = cogs + freight_per_unit + duty_pct × cogs
                   cogs default = default_cogs_pct × price   (assumption, loudly labeled)
per_unit_costs   = fulfillment + referral + storage_avg + returns_cost + ppc_drag
                   returns_cost = return_rate × (price × 0.5 + fulfillment)   # refund loss model
                   ppc_drag     = tacos_pct × price
margin_unit      = price − landed_cost − per_unit_costs
margin_pct       = margin_unit / price
roi_per_turn     = margin_unit / landed_cost
launch_capital   = landed_cost × units_low × inventory_months(2.5)
                   + ppc_ramp (config, default $2,000) + fixed_launch ($1,500)
payback_months   = launch_capital / (margin_unit × units_low)
```

**Three COGS scenarios always computed:** optimistic (20% of price), base (config default 25%), pessimistic (32%) — collapsed to one only when a real quote overrides. **Stressed case** for scoring P1/P2: price −10%, cogs +15%, tacos +5pts simultaneously. **Sensitivity surface**: margin & ROI over price ±15% × cogs ±20% × tacos 10–25% (the report's table).

**Outputs:** full waterfall (every line item), scenarios, stressed values, gate results (min_margin/min_roi/max_payback from config), `assumption_flags[]` marking which inputs are assumptions vs. quotes. **Confidence:** quotes present → high; all-assumption → medium, never high; missing dims → module refuses (blocking, per data-layer).

---

## 5. `risk.py` — Risk Engine

**Inputs:** category path, product attributes, `review_themes`, `keyword_clusters`, brand/HHI stats, volume history, seasonality output.

Deduction ledger, start 100, floor 0 — each entry `(flag, deduction, evidence)` where evidence is a concrete citation (theme id, keyword stat, category rule):

| Flag | Trigger (deterministic) | Deduction |
|---|---|---|
| `ip_signal` | category in design-patent watchlist (config) ∨ Analyst flagged patent-marked listings ∨ brand-likeness lexicon hit in titles | −40 |
| `compliance` | category → certification map hit (CPSIA/children, FDA-adjacent, electrical UL, food-contact) — versioned YAML like fee tables | −30 |
| `trend_dependency` | volume history <24mo ∨ current volume >2× 24-mo median | −25 |
| `seasonality_confirmed` | `peak_concentration > 40%` | −20 |
| `seasonality_unknown` | peak unassessable (<365d history) | −10 |
| `high_returns` | sizing/fit themes >10% frequency ∨ category in high-return list | −20 |
| `fragility` | damage themes >8% ∨ material lexicon hit (glass/ceramic) | −20 |
| `keyword_concentration` | cluster `top_share_pct > 60%` | −15 |
| `market_concentration` | brand HHI > 0.30 (from competition.py) | −15 |
| `supplier_complexity` | attribute rules (firmware/electronics multi-part) | −10 |

Output: `risk_score`, ordered flag ledger with evidence (rendered verbatim as the report's risk table). Missing inputs for a flag → flag `unassessed` (listed, no deduction, except seasonality's explicit −10 unknown case). Confidence: high when ≤1 flag unassessed.

---

## 6. `scoring.py` — Opportunity Score Engine

Pure assembly of scoring-model §2–§10 — no new judgment lives here:

1. **Hard kills** K1–K12: evaluated first, cheapest inputs, conservative range-ends; each kill emits `(rule, threshold, actual_value)`. Borderline (within 10%) → `demoted_to_test` instead of killed.
2. **Sufficiency caps** from data_quality flags (scoring-model §3 table).
3. **Pillar assembly**: reads the five module outputs, applies component weights from config, records every component's `(raw_input, normalized_score, weight)` triple — the methodology section is generated from these triples, which is what makes any score hand-checkable.
4. **Composite** = config weights · pillar scores.
5. **Gates** G1–G5 (G5, Strategist concurrence, is checked by the *pipeline* after the Strategist runs; scoring emits `pending_strategist` until then).
6. **Verdict**: BUY / TEST / AVOID per scoring-model §10, plus `verdict_basis` (which thresholds/gates decided it) and the score-vs-strategist disagreement banner when applicable.

Output object `ScoredOpportunity` is what gets persisted to `validations` and handed to the report renderer — it contains everything needed to regenerate the report without re-running anything.

---

## 7. Package Structure

```
delium/analysis/
├── types.py              # frozen dataclasses: module inputs/outputs, ScoredOpportunity
├── curves.py             # norm, log_norm, sweet_spot, theil_sen, hhi, cv — pure math
├── demand.py
├── competition.py
├── differentiation.py
├── fees.py
├── profit.py
├── risk.py
├── scoring.py
├── fee_tables/           # us-2026.yaml (+ effective_date; old versions kept)
├── rules_data/           # compliance category map, IP watchlist, high-return
│                         #   categories, brand-likeness lexicon — YAML, versioned
└── curves_data/          # bsr_velocity per-category tables, versioned
```

Import law (lint-enforced): `analysis/*` may import `types`, `curves`, stdlib, and data files — never `agents/`, `data/`, or anything that does I/O. All external facts arrive as function arguments.

---

## 8. Unit Testing Strategy

1. **Fee engine vs. Amazon's own calculator** (highest-stakes tests): ~15 fixture products spanning every size tier, each verified by hand against Amazon's Revenue Calculator; asserted to the cent. Fee-table YAML edits must update fixtures — CI fails otherwise. This is the module where a silent bug costs real money.
2. **Golden pipeline fixtures**: the ~10 frozen validation runs (`evals/golden/`) replayed through the full analysis chain; every pillar score asserted within ±1 point. Any intentional formula change regenerates goldens in the same commit with the diff visible in review.
3. **Property tests** (hypothesis): monotonicity (more reviews ⇒ moat score never rises; higher margin ⇒ P1 never falls), bounds (all scores ∈ [0,100] for arbitrary inputs), range sanity (`low ≤ mid ≤ high` always), stressed ≤ unstressed margin.
4. **Missing-data matrix**: for each module, a parametrized test dropping each optional input → asserts null-not-throw, correct note emitted, correct confidence downgrade, correct scoring cap applied.
5. **Kill-rule table tests**: each K-rule gets an at-threshold, just-inside, just-outside triple, including the 10% borderline-demotion band.
6. **Calibration harness** (not CI — a command): `delium calibrate` compares `est_units_range` against any ground truth I enter (my own future sales data, or manually observed inventory-drop tests) and reports bias — feeds §11 of the scoring model.

Coverage bar: `analysis/` at ~100% line coverage — it's pure functions; there is no excuse. (`agents/` and `data/` are tested by goldens and integration smoke, not line coverage.)

---

## 9. config.toml Additions (consolidated)

```toml
[assumptions]        default_cogs_pct=0.25  cogs_optimistic_pct=0.20  cogs_pessimistic_pct=0.32
                     freight_per_unit=0.90  duty_pct=0.05  tacos_pct=0.15
                     return_rate_default=0.04  inventory_months=2.5
                     ppc_ramp_usd=2000  fixed_launch_usd=1500  packaging_dims_allowance=0.10

[stress]             price_delta=-0.10  cogs_delta=+0.15  tacos_delta_pts=+0.05

[gates]              min_margin=0.30  min_roi=1.00  max_payback_months=6
                     risk_floor=40  differentiation_floor=45

[score_weights]      demand=25 competition=25 differentiation=20 profitability=20 risk=10
[demand_curves]      volume_lo=2000 volume_hi=40000  velocity=[150,300,1200,2500,70]
                     bsr_trend=[-0.20,0.40]  seasonality=[0.20,0.60]
[competition_curves] moat=[200,3000]  beatable_reviews_max=150  velocity=[20,300]
                     price_cv=[0.05,0.25]
[kill_rules]         price_min=15 price_max=70 max_median_reviews=3000
                     max_brand_slots=5 fad_spike_ratio=3.0 capital_max=20000 capital_min=2000
                     borderline_band=0.10
[risk_deductions]    ip=40 compliance=30 trend=25 seasonal=20 seasonal_unknown=10
                     returns=20 fragility=20 kw_concentration=15 market_hhi=15 supplier=10
[verdicts]           buy_min=75 test_min=60
```

Everything numeric a formula uses lives here or in the versioned data files — **no magic numbers in code**. `config_snapshot` on each run (already in the runs table) therefore fully determines every score, which is the property that makes calibration honest: change the config, and only future scores move.
