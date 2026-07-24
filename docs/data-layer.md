# Delium — V1 Data Acquisition Layer

The data layer's contract with the rest of the system: **every number the scoring model (docs/scoring-model.md) consumes is either collected here or deterministically derived from what's collected here.** Agents and analysis code never call providers — they read normalized datasets assembled by this layer (`delium/data/`). That makes cache discipline, spend caps, and reproducibility structural rather than aspirational.

Design rule inherited from ARCHITECTURE.md: raw provider payloads are cached whole (replayability), and the handful of fields the formulas need are extracted into typed tables (queryability). Nothing else is normalized.

---

## 1. Provider Adapters

Three adapters, one interface shape: `fetch_x(key) → NormalizedX` — cache-first, budget-checked, spend-logged. Provider quirks stop at the adapter boundary; everything downstream sees only normalized types.

### 1.1 Keepa (`data/keepa.py`) — products, history, sales proxy

Flat €49/mo plan, 20 tokens/min bucket. Marginal cost ≈ $0; the real constraint is **token pacing**, not money — the adapter maintains a token budget clock and paces batches (21-ASIN fetch ≈ 70–150 tokens ≈ 4–8 min of bucket; fetched in parallel batches while reviews/keywords fetch concurrently, so wall-clock stays inside the run).

| Endpoint | Used for | Params | Token cost |
|---|---|---|---|
| `GET /product` (batched, up to 100 ASINs) | Everything per-ASIN | `asin` (csv), `domain=1` (US), `history=1`, `stats=90`, `rating=1`, `offers=20` only when seller-count needed | ~2 base +1 offers +1 rating per ASIN |
| `GET /query` (Product Finder) | Discovery pre-filter (optional path): category + price band + BSR range + review-count ceiling → ASIN list | filter json, page | ~10/page |
| `GET /category` | Category tree + fee-relevant classification | category id | 1 |

**Collected → normalized `ProductRecord`:**

- Identity: ASIN, title, brand, category path, parent/variation ASINs, images count, listing date
- Physical: dimensions, weight → FBA size tier input (**blocking field for profitability** — see §4)
- Time series (90d min, 365d when available): BSR, buybox price, new-offer count, review count, rating
- Derived at normalize time (deterministic, in adapter): `est_units_range` (rank-drop count method, low/high), `bsr_slope_90d`, `price_cv_90d`, `review_velocity_per_month`, `seasonality_peak_concentration` (needs 365d; else null)
- Seller signals: buybox seller id, Amazon-on-listing flag, FBA/FBM mix

Feeds: D1–D5, C1, C3, C6, K1–K6, K10–K11, profit inputs.

**Failure handling:** HTTP 429/`tokensLeft=0` → wait for bucket refill (pacing clock prevents this in practice). 5xx → 2 retries, backoff 5s/25s. Missing ASIN → `NotFound` recorded in fetch log (dead listing is a *signal*, not an error). Partial history → record with `history_days` actual value; downstream sufficiency rules decide (§4).

### 1.2 DataForSEO (`data/dataforseo.py`) — search volume, keyword expansion, SERP

Pay-as-you-go. All endpoints "live" mode (seconds, slight price premium over queued — worth it for an interactive tool).

| Endpoint | Used for | Params | Cost |
|---|---|---|---|
| `dataforseo_labs/amazon/bulk_search_volume/live` | Volumes for a keyword list (≤1,000/call) | keywords[], location=US | $0.012/task + $0.00012/kw → 100 kw ≈ **$0.024** |
| `dataforseo_labs/amazon/related_keywords/live` | Seed → expansion (discovery) | seed keyword, depth 2–3 | ≈ $0.01–0.02/call, hundreds of terms |
| `dataforseo_labs/amazon/ranked_keywords/live` | ASIN → which keywords it ranks for (reverse-ASIN, validate path) | asin | ≈ $0.02/call |
| `merchant/amazon/products` (SERP) | Keyword → ranked ASIN list (organic + sponsored positions) | keyword, location, page | ≈ $0.002–0.008/page |

**Collected → normalized:**

- `KeywordRecord`: phrase (lowercased/trimmed — normalization is the adapter's job, one place only), monthly volume, 12-month volume series (→ trend, K10 fad check, single-keyword-dependence risk), cpc estimate if present
- `SerpRecord`: keyword → ordered ASINs with organic position + sponsored flag + sponsored density (ad-intensity signal for C-pillar context)

Feeds: D1, D4, discovery expansion, keyword cluster construction, K10, risk flag "single-keyword dependence."

**Failure handling:** per-call cost check against run budget *before* dispatch. 4xx (bad keyword) → log + skip, never retry. 5xx/timeout → 2 retries, backoff. Volume returned null for a valid term → store `volume=null` (some long-tails legitimately have no data), excluded from cluster sums, counted in sufficiency.

### 1.3 Review provider (`data/reviews.py`) — the fragile one

Primary: Unwrangle Amazon Reviews API; fallback: Apify actor (bake-off decides which is primary — same normalized output either way, switch is a config line). Anonymous ceiling: **~100 reviews/ASIN** (top reviews). We do not use logged-in/cookie scraping.

| Call | Params | Yield | Cost |
|---|---|---|---|
| reviews-by-ASIN, sorted by "top reviews" | asin, marketplace, max_pages=10 | up to ~100 reviews | ≈ **$0.30/ASIN** ($3/1,000) |

**Collected → normalized `ReviewRecord[]`:** review id, star rating, date, title, body text, verified flag, helpful votes. Plus fetch-level: `retrieved_count`, `requested_count`, rating distribution of sample vs. listing's overall distribution (sample-bias check — "top reviews" skew positive/helpful; the delta is stored and shown in methodology).

Feeds: F1–F4 (via Review Miner), risk flags (fragility, sizing/returns), K7.

**Failure handling — degrade, never die:** this category breaks whenever Amazon changes markup. Per-ASIN: 2 retries → on final failure record `retrieved_count=0` and continue the run. Run-level: if target-ASIN reviews < 30, Differentiation pillar caps per scoring-model §3 and the report says so; if *zero* reviews retrievable across all ASINs, `validate` completes with Differentiation marked `missing` and verdict capped at Test. Failure rate >15% across a week → alarm to switch provider (both adapters stay maintained).

---

## 2. SQLite Storage Model

One file (`data/delium.db`, WAL). Two zones: **raw cache** (whole payloads, replayable) and **extracted** (typed rows the formulas query). Extracted rows always carry the `fetch_id` they came from — every number in every report traces to a logged API call.

```
── raw cache ─────────────────────────────────────────────────────────
raw_fetches        id PK, provider, endpoint, request_key,   -- e.g. 'keepa:product:B0…'
                   payload (json), cost_usd, tokens_used, http_status,
                   run_id FK, fetched_at
                   INDEX (provider, request_key, fetched_at DESC)   -- cache lookup
                   INDEX (run_id)                                    -- run replay/audit
                   -- doubles as the DATA FETCH LOG and the spend ledger

── extracted: products ──────────────────────────────────────────────
products           asin PK, marketplace, title, brand, category_path,
                   listing_date, dims_json, weight_g, size_tier,
                   images_count, amazon_on_listing (bool),
                   fetch_id FK, updated_at
price_bsr_history  asin FK, captured_on (date), price_cents, bsr,
                   offer_count, review_count, rating
                   PK (asin, captured_on)
                   -- upserted from each Keepa history payload; accumulates OUR
                   -- longitudinal record beyond any provider's window
product_derived    asin PK, est_units_low, est_units_high, bsr_slope_90d,
                   price_cv_90d, review_velocity_mo, seasonality_peak_pct,
                   history_days, computed_at, fetch_id FK
                   -- recomputed on every refresh; formulas read THIS, not raw json

── extracted: keywords & rankings ───────────────────────────────────
keywords           phrase PK, marketplace, volume, volume_series (json, 12mo),
                   cpc_cents, fetch_id FK, updated_at
serp_rankings      keyword_phrase FK, asin, position, sponsored (bool),
                   captured_on (date)
                   PK (keyword_phrase, asin, captured_on)
keyword_clusters   cluster_id PK, run_id FK, primary_phrase, member_phrases (json),
                   total_volume, top_share_pct    -- single-keyword-dependence input

── extracted: competitive sets ──────────────────────────────────────
competitor_sets    id PK, run_id FK, target_asin, member_asins (json ordered),
                   selection_method ('serp_top'|'keepa_category'|'manual'),
                   created_at
                   -- freezes WHICH 20 ASINs a validation compared against;
                   -- re-runs build a new set, old reports stay interpretable

── extracted: reviews ───────────────────────────────────────────────
reviews            review_id PK, asin FK, stars, review_date, title, body,
                   verified (bool), helpful_votes, fetch_id FK
                   INDEX (asin)
review_fetch_meta  asin PK, retrieved_count, requested_count,
                   sample_rating_avg, listing_rating_avg,   -- bias check pair
                   fetch_id FK, fetched_at
review_themes      id PK, run_id FK, asin, kind ('complaint'|'praise'|
                   'missing_feature'|'improvement'), theme, frequency_pct,
                   severity (1-3), quote_review_ids (json)   -- ≥3 or theme discarded
                   -- Review Miner OUTPUT stored as data: citations resolve to
                   -- real review rows; themes are queryable across markets over time
```

Cache freshness is determined by querying `raw_fetches` for the newest row per `request_key` — no separate TTL bookkeeping table. `runs` / `candidates` / `validations` tables are unchanged from ARCHITECTURE.md §9.

**TTLs** (config, per class): product+history 24h · keyword volume 7d · SERP 3d (rankings move) · reviews 14d · category/fee metadata 30d.

---

## 3. Pipeline Flows

### 3.1 `validate <ASIN>` — assembly sequence

```
input ASIN (or keyword → SERP → pick target)
   │
   ├─ 1. Keepa: target product (history=1, stats, offers) ──────────── cache-first
   ├─ 2. Reverse-ASIN (DataForSEO ranked_keywords) → keyword cluster
   │       └─ bulk_search_volume for cluster (one call, ≤100 kw)
   ├─ 3. SERP for top-3 cluster keywords → competitor pool
   │       └─ dedupe, rank by frequency×position → TOP 20 competitor ASINs
   │          → competitor_sets row (frozen)
   ├─ 4. Keepa batch: 20 competitors ───────────────── paced, parallel with step 5
   ├─ 5. Reviews: target + top-3 competitors (~400) ── parallel with step 4
   │
   ├─ 6. NORMALIZE: adapters extract → products / price_bsr_history /
   │       product_derived / keywords / serp_rankings / reviews tables
   ├─ 7. SUFFICIENCY CHECK (§4) → data_quality flags per pillar
   │
   └─ 8. ASSEMBLE COMPACT DATASET for agents/analysis:
          ValidationDataset {
            target: ProductSummary,                  # ~40 fields, no raw json
            competitors: ProductSummary[20],         # table-shaped
            keyword_cluster: {phrases, volumes, trend, top_share},
            serp: position matrix + sponsored density,
            reviews: {target: text[], competitors: text[], bias_meta},
            derived: demand/price stats from product_derived,
            data_quality: per-pillar flags,
            citations_index: field → fetch_id
          }
          → analysis/ computes → agents interpret → report cites fetch_ids
```

Token/latency budget: steps 1–5 run in ~4–8 minutes dominated by Keepa pacing and review pages; acceptable for a ~monthly-cadence deep dive.

### 3.2 `discover <seed>` — cheap and wide

```
seed → related_keywords (1–2 calls) → few hundred phrases
     → bulk_search_volume (1 call) → filter by config bands + trend
     → SERP page-1 only for top ~15 surviving keywords
     → candidate ASIN pool (~100–150 unique)
     → Keepa batch quick stats (NO offers param — cheaper tokens; price/BSR/
       reviews/listing-age only)
     → hard kills K1–K6, K10–K12 (all computable from the above)
     → triage score → top ~20 candidates → Scout agent → shortlist report
NO review fetching at discovery — reviews are validate-tier spend.
```

### 3.3 `watch` — refresh only what's stale

Weekly: for watched ASINs, Keepa refresh (append `price_bsr_history`) + volume refresh for watched clusters. Deltas computed against our own accumulated history. No SERP, no reviews unless a delta threshold trips (e.g., review_count jump >20% → one review fetch to see what changed).

---

## 4. Data Quality Rules

**Freshness:** a cached payload past TTL is *stale, not invalid* — the fetch layer refreshes it; if refresh fails, the stale copy is used with `stale: true, age_days: n` carried into `data_quality` and the report methodology. Hard staleness ceiling: data older than 2× TTL is treated as missing.

**Sufficiency (implements scoring-model §3):** computed per pillar at step 7:

| Check | Full | Partial (pillar capped) | Missing (blocks Buy) |
|---|---|---|---|
| Competitor Keepa history | ≥60d on ≥7 of top 10 | ≥30d on ≥5 | less |
| Review sample | ≥150 across target+top 3 | 30–149 | <30 |
| Keyword cluster | ≥5 phrases with volume | 2–4 | <2 |
| Price history | ≥90d on ≥5 of top 10 | 30–89d | C6 → neutral 50 |
| Dims/weight/category | present | — | **blocking**: profitability not computed; prompt for manual entry (`--dims`, `--weight`) |

**Confidence scoring:** each `ValidationDataset` section carries `confidence: high|medium|low` derived mechanically (sample size, history depth, staleness, provider failure count during the run) — not model-judged. Review data additionally carries the sample-bias delta (top-reviews skew), which the Review Miner prompt must acknowledge.

**Retry rules (uniform):** live API errors → 2 retries, exponential backoff (5s/25s); 4xx never retried; per-run failure budget of 5 provider errors → after that, stop fetching, complete with what's cached, mark run `degraded`. Never retry-loop against a provider that's clearly down.

**Reject-for-insufficient-data:** a candidate is auto-demoted (not scored) when: target ASIN unresolvable/dead; no keyword in the cluster has volume data; or fee-blocking fields are missing and not supplied manually. Logged as `rejected: insufficient_data` with the specific gaps — distinguishable from "rejected: bad opportunity," because the former is worth retrying in a week.

---

## 5. Cost Controls

**Budgets (config `[budgets]`, enforced in `data/cache.py` before every dispatch — an over-budget call raises before the request leaves):**

| Run type | Data budget | Typical actual |
|---|---|---|
| `discover` | **$0.50 max** | $0.10–0.30 (volume calls + 15 SERP pages; Keepa flat) |
| `validate` | **$3.00 max** | $1.30–1.60 (reviews $1.20 for 4 ASINs + DataForSEO ~$0.10) |
| `pains` | $2.50 max | ~$1.80 (6 ASINs of reviews) |
| `watch` weekly | $1.00 max | ~$0.10–0.30 |
| Monthly alarm | $120 total (incl. Keepa $54 flat) | expected ~$75–110 |

**Cache-first, always:** every fetch checks `raw_fetches` freshness before dispatch — including *mid-run* (the 20 competitors of two overlapping niches share cache rows). Cache hit rate is a first-class number in every run footer; validating within a niche you've been discovering in should show >50% hits.

**Call-avoidance rules (cheapest data answers first):**

1. Kill rules run on the cheapest tier: K1–K6/K10–K12 need only Keepa quick stats + volume — never spend review money on a candidate that a $0 check can kill.
2. Reviews are the expensive tier ($0.30/ASIN): fetched only at `validate`/`pains`, only for target + top-3, only after kill rules pass.
3. Keepa `offers` param (seller-count detail) requested only at validate, not discovery — token cost discipline.
4. SERP page-1 only at discovery; deeper pages only when validate needs the full top-20.
5. Keyword volumes fetched in single bulk calls (task fee amortized), never per-phrase.
6. `watch` reads its own accumulated `price_bsr_history` before asking any provider for "what changed."
7. Re-validation of a known ASIN within TTL windows re-fetches **nothing** — it recomputes from cache and says so in the methodology block.

**Ledger & visibility:** `raw_fetches.cost_usd` is the ground-truth spend record; `runs` aggregates it per run; every report footer prints `data $X.XX · llm $X.XX · cache hits N/M`. The monthly alarm reads the ledger, not provider dashboards.
