# Calibration

**Purpose:** measure how well Delium's own estimates match Keepa's ground truth,
so the illustrative curve/fee tables can be corrected against reality. Calibration
**only measures** — it never changes a curve, fee, weight, kill, or gate. Every
suggested adjustment is printed with its sample size for you to apply by hand
(and re-version the data file).

> ⚠️ All curve/fee/threshold values in Delium are **illustrative** until this
> harness (plus real launch outcomes) has calibrated them. The CLI and UI say so.

## What it compares

For each product (`delium calibrate`, or Settings → Calibration):

1. **Monthly units** — Delium's BSR-curve estimate
   (`demand.curve_units_for_bsr`, the category velocity curve interpolated at the
   product's current BSR) vs Keepa **`monthlySold`**.
2. **FBA fee** — Delium's fee-table fulfilment fee (`fees.fulfillment_fee` for the
   product's size tier + weight) vs Keepa **`fbaFees.pickAndPackFee`**.

Both Keepa fields are captured on the existing `stats` product call (migration
0009) at no extra token cost.

## How the bucketed `monthlySold` is handled

Keepa's `monthlySold` mirrors Amazon's displayed **"bought in past month"** badge,
which is **bucketed** (50+, 100+, 200+ … 1K+, 2K+ …). The number Keepa returns is
the bucket **floor**, so it means a **range**, not a point. Comparing a point
estimate against a point would manufacture error that isn't real.

So calibration compares Delium's estimate against the **bucket range**:

- `monthly_sold_bucket(value)` maps the floor to `(low, high)` using Amazon's
  displayed ladder (`MONTHLY_SOLD_LADDER`); the top bucket is open-ended
  (`high = None`).
- **In-bucket is a hit:** if `low ≤ delium < high`, error = **0%**.
- **Out of bucket:** error = the distance to the **nearest edge**
  (`(low − delium)/low` below, `(delium − high)/high` above) — never penalised for
  landing anywhere inside the range.
- **Ratio** (for bias direction) uses a single representative per bucket — the
  geometric mean of the edges (`√(low·high)`, or `1.5×` the floor for the
  open-ended top) — so `ratio > 1` means Delium over-estimates.

The report shows, per product, whether Delium's estimate is **inside** Keepa's
bucket, and aggregates a **bucket hit-rate** alongside the MAPE.

## Aggregates & suggestions

- **Units MAPE / Fee MAPE** — mean absolute % error across products (units are
  bucket-aware: in-bucket contributes 0).
- **Per category** (top-level Keepa department): median unit ratio, a **suggested
  curve scale** = `1 / median_ratio` (what would centre the bias on the bucket),
  the bucket hit-rate, median fee ratio, and a **suggested fee scale**
  = `1 / median_fee_ratio` — each with its **sample size**. Suggestions are
  advisory; apply them by editing `curves_data/…` / `fee_tables/…` and bumping the
  version.

## Data sources & `--refresh`

Stored data only by default (no network): products already in SQLite with a
Keepa `monthlySold` and/or `fba_pick_pack_cents`. Products lacking both are
skipped (nothing to compare).

`delium calibrate --refresh` re-fetches the target ASINs from Keepa first
(**batched, cache bypassed**). It prints the estimated token cost (~2 tokens per
product) and **requires a confirm** (`--yes` to skip) before spending. Without
`--refresh`, no paid call is ever made.

```bash
delium calibrate                       # every stored product, stored data only
delium calibrate --asin B0… --asin B0… # specific ASINs
delium calibrate --file asins.txt      # newline-separated list
delium calibrate --refresh             # re-fetch first (confirms the token cost)
```
