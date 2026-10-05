# Zombie listings

**Purpose:** find listings that are **out of stock long-term** but still carry
**strong reviews** and **proven past demand**, and *verify* they are truly dead
(not a temporary stockout and not likely to be restocked). A verified zombie is
**demand proof** for launching your own improved product on a **new** listing.

> ⚠️ All thresholds are **illustrative** until calibrated against real outcomes.
> The verdict is a research signal, not legal or policy clearance — see
> **Compliance** below.

Keepa only; no scraping.

## Data (Keepa Product Finder + history)

The sweep uses the Keepa **Product Finder** (`/query`). Field names, types and
units were verified against the official backend request struct
(`github.com/keepacom/api_backend`, `ProductFinderRequest`) and the
`akaszynski/keepa` client — the same source used for the finder fix:

| Field | Type | Meaning in the zombie sweep |
|---|---|---|
| `current_COUNT_NEW_lte` | Integer | `0` ⇒ **no current new offers** (primary dead signal) |
| `current_RATING_gte` | Integer (0–50) | rating floor — 4.0★ ⇒ `40` |
| `current_COUNT_REVIEWS_gte` | Integer | review floor (social proof) |
| `outOfStockPercentage90_NEW_gte` | Integer | % of the last 90 days with no NEW offer — **optional** |
| `productType` | Byte[] | `[0]` standard physical only |
| `page` / `perPage` | int | paging |
| `sort` | String[][] | `[["current_COUNT_REVIEWS","desc"]]` — strongest social proof first |

**We do NOT send `buyBoxIsAmazon=false`.** That Boolean matches only listings
that *have* a (non-Amazon) buy box, so it excludes the very listings we want — a
dead listing has **no buy box at all**. Amazon-sold listings are excluded
**after hydration** instead, from the Amazon offer history (the Amazon price
series currently carrying an offer ⇒ not a zombie). The
`outOfStockPercentage90_NEW_gte` filter is **optional**: if the full selection
returns 0, the sweep **retries once without it** (core-only) so one over-strict
filter can't zero out the result. There is **no price or sales-rank band**: a
dead listing has no current NEW price.

### Empty-result diagnostics

Every finder call records a diagnostic (shown in the CLI and under the UI result
line, logged as a WARNING on failure/0): the Keepa **HTTP status** and **error
body** (never the key), Keepa's **totalResults** for the query, the **exact
filters sent**, whether the **core-only fallback** was used, and the **skip
reason** when a call was not made (over cap, missing key, marketplace not
covered). Finder tokens are counted in the reported total even when 0 candidates
are swept, so an empty run no longer misleadingly shows "0 tokens".


Candidates are then **hydrated** (batched, cache-first Keepa `/product`). The
availability history is read from the **raw** `csv` series — the normalized
history drops Keepa's `-1` sentinels, but the zombie detector needs them
(`raw_csv_series` keeps them):

- **NEW price** (csv index 1): `-1` ⇒ no new offer at that time.
- **offer count** (csv index 11, `COUNT_NEW`): `0`/`-1` ⇒ no offers (fallback
  when the NEW series is empty).
- **sales rank** (csv index 3): BSR, read **only over in-stock intervals** =
  proven past demand.

### How "continuously out of stock" is computed

The change-points are collapsed into runs of `(start, in_stock)`. The listing is
out of stock during any run whose value is `-1` (NEW) or `0` (offer count).

- **`days_out_of_stock`** = length of the **current, still-open** out-of-stock
  run, measured from its start up to `as_of`. It is only counted when the series
  **ends** out of stock; a series ending in stock yields `0` (not a zombie).
- **`restock_gaps`** = number of historical **out→in** transitions (the
  resurrection pattern). More gaps ⇒ higher resurrection risk.
- **`last_offer_date`** = when the final offer disappeared.
- **in-stock BSR** (median + best) = BSR observed only while in stock.

## Verification score (deterministic, per marketplace)

Thresholds live in `src/delium/analysis/zombies_data/<marketplace>.toml`
(`us`, `uk`, `ca`; others fall back to `us`). Each component is 0–100; the score
is the weighted mean over the **available** components (a missing component is
dropped and the weights renormalize — *missing = unknown*, never assumed dead).

| Component | Weight (US) | Source |
|---|---|---|
| `dead_duration` | 30 | months continuously out of stock → `min_months`…`ideal_months` |
| `social_proof` | 20 | rating ≥ `min_rating` and reviews ≥ `min_reviews` |
| `past_demand` | 30 | in-stock BSR vs `strong_bsr`…`weak_bsr` (or Keepa `monthlySold`) |
| `low_resurrection_risk` | 20 | `100 − risk`; risk rises per restock gap and if the original seller is active |
| `current_demand` | 10 | top candidates only — DataForSEO SERP ("are similar products selling now?") |

**Default thresholds** (US baseline; UK/CA use lower review floors and BSR
bands for the smaller catalogues):

- `duration`: `min_months = 6`, `ideal_months = 24`
- `social_proof`: `min_rating = 4.0`, `min_reviews = 50` (UK 40, CA 30)
- `past_demand`: `strong_bsr = 20000`, `weak_bsr = 200000` (UK 10k/120k, CA 8k/100k)
- `resurrection`: `gap_risk_each = 25`, `seller_active_risk = 40`, `high_risk = 60`
- `verdict`: `verified_min = 70`, `possible_min = 45`

### Verdict

Hard gates can only make a candidate **less** of a zombie:

- currently in stock / has a live offer, or **sold by Amazon** → **Not a zombie**
- rating below the floor, or reviews below the floor → **Not a zombie**
- out of stock but **< `min_months`** → **Possibly temporary**
- key evidence missing (no history / no rating / no reviews / no in-stock BSR) →
  capped at **Possibly temporary** (never "Verified" on unknowns)
- resurrection risk ≥ `high_risk` → capped at **Possibly temporary**

Otherwise: score ≥ `verified_min` → **Verified zombie**; score ≥ `possible_min`
→ **Possibly temporary**; below → **Not a zombie**. Every result carries the
evidence numbers, a plain-English reason, and a confidence level driven by how
much evidence was present.

## Compliance flags (shown on every result)

- **Brand**: `generic/unbranded` vs the named brand on the listing.
- **Route** (default first):
  - *Launch your own improved version on a NEW listing* (the zombie is demand
    proof) — always offered.
  - *Possible revival* — **only** offered when the brand looks generic, and only
    if you can source the **identical** product or hold the brand rights. For a
    named brand the tool instead says **No revival**.
  - The tool **never** suggests putting a different/private-label product on an
    existing ASIN.
- **Manual check required**: a trademark search (UK IPO / CIPO / USPTO by
  marketplace) and an Amazon anti-counterfeit / listing-ownership policy review
  before acting. The tool does not clear you legally.

## Interfaces

### CLI

```bash
delium zombies --marketplaces UK,CA                 # defaults: 6 months, 50 reviews, top 20
delium zombies --marketplaces UK --min-dead-months 12 --min-reviews 100 --top 10
delium zombies --marketplaces UK --check-demand     # + DataForSEO SERP (PAID) for the top N
```

A cost/token preflight prints the projected Keepa tokens (finder + hydrate) and
DataForSEO USD, and asks to confirm before spending (`--yes` skips it).

### UI

**Find → Zombies**: run the search, then each result is a card (verdict,
evidence, compliance flags, route) with *Open in workspace*. The **Product
workspace** gains a **Zombie check** tab: an out-of-stock timeline chart plus the
verdict, evidence, compliance flags and route for the open ASIN (cache-first; run
a Deep dive first if nothing is cached).

### Daily Scan (optional)

`delium scan --zombies` runs a small, additive zombie pass for the scan's
marketplaces after the main funnel and attaches a one-line summary to the report
notes. It is **off by default** and never changes the main funnel or verdicts.

## Cost per run

Worst case (all cache-miss), per marketplace:

- **Sweep**: one Product Finder call ≈ `10 + ceil(perPage/100)` Keepa tokens
  (≈ 11 for 100 results).
- **Hydrate**: ≈ `2 × sweep_size` tokens (≈ 200 for 100 ASINs).
- **Current demand** (opt-in, top N only): ≈ `3 × top_n × $0.01` DataForSEO.

So a default UK+CA run (`sweep 100`, no demand check) projects to roughly
**~420 Keepa tokens** and **$0** DataForSEO; adding `--check-demand --top 20`
adds about **$0.60**. Everything is cache-first, so repeats are far cheaper, and
the `--budget-cap` / `--max-spend` caps abort in preflight before any spend.
