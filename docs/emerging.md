# Emerging-products discovery (`delium emerging`)

Find products that **launched recently and are already gaining real traction
while competition is still weak**, then run the promising ones through the
**existing** hard kills + scoring. This is a discovery *source*, not a new
scoring pillar — `scoring.py` remains the sole owner of Buy/Test/Avoid.

## Pipeline

```
Keepa Product Finder (recent + selling + low-review candidates)
  → batched, cache-first product hydration
  → deterministic emergence signal (WHY it's emerging)
  → rank by emergence, take top N
  → EXISTING cheap hard kills → full scoring.py   (reuses discovery.pipeline._evaluate)
  → emerging-but-killed shown separately, with the exact kill reason
  → persist run + candidates (SQLite)
```

Deterministic Python only; no LLM. The only wall-clock use is the Product
Finder's "recently listed" cutoff — a *query input*, never a scoring input
(scoring's `as_of` stays data-derived).

## Keepa endpoint and filters

**Endpoint:** `GET/POST https://api.keepa.com/query` (the Product Finder). The
filter is a URL-encoded `selection` JSON; the API key is a separate parameter and
never part of the selection. Response: `{asinList, totalResults, tokensLeft,
tokensConsumed}`.
Doc source: <https://keepa.com/api-docs/product-finder.html> (checked 2026-09).

Selection built by `analysis.emerging.build_finder_selection` from the external
thresholds (`analysis/emerging_data/<version>.toml`):

| Intent | Keepa selection field | Default |
|---|---|---|
| recently tracked (≤ N days) | `trackingSince_gte` (Keepa-minute cutoff from `age_max_days`) | 180 days |
| meaningful sales | `current_SALES_gte` / `current_SALES_lte` | BSR 200–40,000 |
| private-label price band | `current_NEW_gte` / `current_NEW_lte` | $15–$60 |
| still beatable | `current_COUNT_REVIEWS_lte` | ≤ 100 |
| not sold by Amazon | `buyBoxIsAmazon = false` | on |
| category | `rootCategory` (array of ids) | all (optional) |
| physical products | `productType = [0, 1]`, sort `current_SALES` asc | — |

> Field names follow the Keepa Product Finder schema; verify them against a live
> `/query` response for your account before trusting a real run (Keepa
> occasionally renames finder fields). "Oversized" and BSR-momentum are **not**
> filtered at query time — oversized is caught by the existing hard kill `K3`
> after hydration, and momentum is computed from history (below), both more
> reliable than the finder's delta filters.

**Token cost (Keepa docs):** the Product Finder costs **10 tokens per request +
1 per 100 ASINs returned**. Product hydration is batched (≤100 ASINs/`/product`
call) and cache-first, so it costs roughly **~4 tokens per *cache-missed*
product**. Worst case for the default `page_size = 50`: ~11 finder + ~200
product ≈ **~210 Keepa tokens per run**; a warm cache costs far less. Keepa is a
flat subscription, so the marginal dollar cost is $0 — you spend tokens. The CLI
and UI show this estimate and require confirmation before any call.

## Emergence signal

`analysis.emerging.compute_emergence` scores **why** a candidate reads as
emerging, from its observable, already-fetched facts. It is **0–100 and separate
from the opportunity score** — it never feeds `scoring.py`. Each sub-signal is
0–100 (or `None` when unmeasurable); the score is the weighted mean over the
*available* sub-signals, so missing data lowers coverage rather than inventing a
value.

| Sub-signal | Meaning | From | Default weight |
|---|---|---|---|
| `recency` | younger = higher (100 at ≤ `young_days`, 0 at ≥ `old_days`) | earliest observed history date vs `as_of` | 25 |
| `traction` | more sales (lower BSR, log scale) = higher | latest BSR | 25 |
| `momentum` | 90-day Theil–Sen slope of log10(BSR); negative = rank improving | price/BSR history | 25 |
| `competition` | fewer reviews = higher | latest review count | 15 |
| `review_gap` | selling well **and** still few reviews (geo-mean of traction × competition) | derived | 10 |

Default thresholds (`emerging_data/us.toml`, **ILLUSTRATIVE** — tune to your
categories): `young_days=90`, `old_days=365`, `bsr_strong=500`, `bsr_weak=40000`,
`slope_improving=-0.004`, `reviews_low=5`, `reviews_high=150`.

## Confidence honesty

A recently-launched product has a **short sales history**, so demand is
genuinely less certain. This path changes nothing about confidence: the existing
demand engine already caps confidence on thin history and an unknown category
curve, and `scoring.py`'s gates/verdict rules are unchanged. The emerging path
**cannot produce a BUY on data the existing confidence rules wouldn't allow** —
it only chooses *which* ASINs to score, never *how* they score.

## Persistence

Migration `0006_emerging.sql` adds `emerging_runs` and `emerging_candidates`
(emergence score, age, outcome, opportunity score/verdict, kill rule, and the
sub-signal breakdown). The UI History page lists past emerging runs and their
candidates so runs can be compared over time. The opportunity score/verdict
continue to live in the shared scoring path.

## Limitations

- **Keepa-only search.** The Product Finder is a Keepa endpoint; without a Keepa
  key the command returns a clear note and does nothing (no DataForSEO fallback).
- **Age is a proxy.** `trackingSince` (when Keepa began tracking) and the
  earliest observed history approximate the launch date; a product Keepa started
  tracking late can look younger or older than it is.
- **Finder field names need verification** against a live account (see above).
- **All threshold/curve values are illustrative** and must be calibrated to real
  category norms before the numbers are trustworthy (same caveat as the fee/curve
  /risk tables — see `docs/category-data-audit.md`).
- **DataForSEO enrichment is best-effort**: the seed is derived from the product
  title's first words, so its keyword volume is indicative, not authoritative.
- **Competition/differentiation run thin** for a finder candidate (no SERP
  competitor set is fetched), so those pillars are low-confidence — as they
  should be for a product with little market history.

## Sampling, variation dedupe, brand flags, and diagnosis (2026 update)

- **BSR sub-band sampling.** A single `sort: current_SALES asc` over the whole
  `[bsr_min, bsr_max]` band only ever returns its *top edge* (the lowest-BSR
  mega-sellers). The finder now splits the band into `sub_bands` log-spaced
  sub-ranges (external `[finder].sub_bands`, default 4) and issues one call per
  sub-band, merging the ASIN lists; the top N are then chosen locally by
  emergence. `sub_bands = 1` restores the single-call behavior. `sort_field` is
  configurable in the data file so a verified momentum/rank-drop sort can be
  swapped in without code changes. Token cost scales with the number of
  sub-bands (reported per run).
- **Variation dedupe.** Keepa `parentAsin` is captured and stored
  (migration 0007). `discover`, `emerging`, and `cross-market` collapse
  variations to one row per parent listing (the best-performing child is the
  representative) with a variation count, so eight colours of one product no
  longer fill the table.
- **Established-brand flag.** Brands in `[brands].established` are FLAGGED
  ("likely ad-driven line extension"), never silently scored or dropped — a real
  emerging underdog should never be on that list.
- **`delium diagnose`.** A read-only command (no API calls) that rebuilds the
  deterministic score for stored candidates and shows, per pillar, the score,
  confidence, the input that drove it, and which evidence fix would raise
  confidence — plus a per-product plain-English card. An **absent** pillar is
  reported as *unknown* (excluded from the composite; lowers confidence), never
  as a 0 that would drag the opportunity score down (see
  `docs/scoring-model.md` §3).
