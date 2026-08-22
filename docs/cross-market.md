# Delium — Cross-Market Product Discovery

**Purpose.** Find products that are *already proven* in one Amazon marketplace but
look *underpenetrated yet credibly demanded* in another, so I can prioritize the
handful worth deeper validation for a market-expansion play (US → CA/UK/AU/IN,
UK → US, AU → CA, …). Direction is arbitrary; US is not assumed to be the source.

This is a **discovery signal**, implemented deterministically in
`analysis/cross_market.py`. It is **not** a sixth opportunity pillar and **not** a
second Buy/Test/Avoid system — `analysis/scoring.py` still owns the product
verdict. A later discovery pipeline can combine the opportunity score with the
cross-market score to rank candidates.

The central principle, encoded conservatively (false positives are worse than
missed matches):

```
success elsewhere + target-market evidence + competition/maturity gap
    + transferability + data confidence  =  cross-market opportunity signal
```

and, deliberately:

```
no target demand  ≠  opportunity
no listings       ≠  opportunity      (absence of evidence is not evidence)
```

The engine never says "this will sell in Australia." At most it says "strong
cross-market opportunity signal — worth validating," and it is built to be quiet
unless the evidence genuinely lines up.

---

## 1. Contract (purity)

`cross_market.py` is pure computation: **no LLM, no providers, no ingestion, no
database, no network, no clock, no random**. It consumes already-normalized
inputs (built by the caller from the existing engines) and every threshold/weight
comes from `config.toml [cross_market]`. Same inputs + same config → identical
output, forever. Provider calls live in `providers/`; persistence in
`ingestion/`+`database/`; cross-market math here.

---

## 2. Marketplace model

`Marketplace` (enum) values reuse the `marketplace` strings already stored across
ingestion/DB (`US`, `CA`, `UK`, `AU`, `IN`). Static reference facts live in a
central registry, `analysis/marketplaces.py::MARKETPLACES` (`MarketplaceInfo`:
country, currency, locale, Amazon domain, marketplace id, unit system, language).
Adding a marketplace means one enum entry + one registry row — the engine reads
the registry and hardcodes no marketplace logic.

Currency is **not** converted into the score. V1 compares normalized 0–100 signals
only; it never pretends to know a live exchange rate. FX would enter later, only
if the architecture provides a deterministic FX input.

---

## 3. Product matching

ASINs are marketplace-specific, so identity is resolved on observable signals,
conservatively. `match_products` returns a `ProductMatch` with a
`MatchConfidence`: `EXACT` / `STRONG` / `PROBABLE` / `WEAK` / `UNMATCHED`.

- **GTIN/UPC/EAN agreement → `EXACT`** (the only exact key; normalized by
  stripping non-digits and leading zeros). A GTIN *conflict* caps confidence at
  `WEAK` (different barcodes ⇒ almost certainly different products).
- Otherwise a **weighted fuzzy score** over title (token Jaccard), brand,
  dimensions, and category; fuzzy tops out at `STRONG` (never `EXACT`).
- **Brand mismatch** on non-generic products is a conflict; on
  generic/private-label products it is tolerated at reduced weight, not fatal.
- **Conflicting physical signals** (dimensions or weight outside tolerance) cap
  confidence at `PROBABLE` — a superficial title match with different dimensions
  is not the same product.

Every match exposes the signals used, the conflicting signals, and both
marketplace products. Match confidence is a hard ceiling on the final report
confidence.

---

## 4. Source-market qualification

The source market is where the product shows *durable* success — not merely "has
sales." `assess_source` scores five signals (weights in config):

| Signal | Normalization |
|---|---|
| velocity (median top-10 units/mo) | `log_norm(units, src_velocity_lo, hi)` |
| keyword demand (cluster volume)   | `log_norm(vol, src_keyword_lo, hi)` |
| growth (YoY keyword volume)       | `norm(g, src_growth_lo, hi)` |
| history length (months)           | `norm(months, src_history_lo, hi)` |
| established (median reviews)       | `log_norm(reviews, src_reviews_lo, hi)` |

Score = weighted mean over the signals actually present (missing signals lower
confidence, never inflate). Competition and opportunity-score are surfaced as
*context* only — not folded into the source score — to avoid double-counting the
competition/maturity gap computed later. Classification: `INSUFFICIENT` (fewer
than `src_min_signals` present) / `EMERGING` / `VALIDATED` / `STRONG` /
`EXCEPTIONAL`. The output states why.

---

## 5. Target-market qualification

`assess_target` classifies `TargetPresence`:

- **UNKNOWN** — `listings_found is None` (not looked up).
- **NOT_PRESENT** — `listings_found == 0` (looked up, none found).
- **EARLY** — some presence, immature.
- **UNDERPENETRATED** — exists but competition weak *relative to credible demand*.
- **MATURE** — established (enough listings + review depth).
- **SATURATED** — strong demand *and* strong incumbents/review moats.

`UNKNOWN` and `NOT_PRESENT` are distinct and neither implies opportunity. No
listings can mean no demand, wrong keyword, localization gap, data failure, or a
regulatory barrier — so it is never scored as "excellent" on its own.

---

## 6. Target demand, maturity gap, competition gap

**Target demand** (`_target_demand`): weighted `log_norm` of keyword volume +
`norm` of growth + a small SERP-presence bump. Demand is **credible** only with
real volume evidence (`keyword_volume` present and `> 0` *and* score ≥
`tgt_demand_credible_min`) — a lone SERP boolean or growth number is not enough.

**Maturity gap**: `clamp(source_success − target_maturity)`, where target maturity
combines listing count and review depth. Larger credible gap = more interesting.

**Competition gap**: `clamp(50 + (target_weakness − source_weakness)·0.5)` — how
much *easier* the target looks than the source, so a target with far fewer reviews
than the source scores above 50. Raw review counts are not compared directly; each
side is normalized first. When the target has zero listings, competition weakness
is *inferred* at `empty_market_weakness` and flagged low-confidence — it only
counts toward a strong verdict when demand is independently credible.

---

## 7. Demand / competition states

The verdict logic distinguishes the four states the brief calls out:

- **A. proven + credible demand + weak competition** → strongest signal.
- **B. proven + uncertain demand + weak competition** → research opportunity.
- **C. proven + strong demand + strong competition** → mature/saturated market.
- **D. proven + no demand** → likely poor transferability / insufficient evidence.

---

## 8. Transferability

`assess_transferability` grades observable factors (never cultural speculation):
category compatibility, logistics (oversized), price positioning, compliance
surface, and seasonality. Any **unfavorable** hard factor (e.g. compliance
surface, oversized, incompatible category) makes the overall level `UNFAVORABLE`,
which downgrades the verdict to `WEAK_TRANSFER`. Risk flags from the existing risk
engine are **surfaced verbatim**, never recomputed here.

---

## 9. Localization flags

Identified, not solved, in V1: unit-system differences (from the marketplace
registry), language differences, plug/voltage dependence, and keyword/terminology
re-mapping. Flags are informational and do not by themselves kill a signal.

---

## 10. Cross-market score

```
base = Σ(component·weight) / Σweight        components, weights in [cross_market]
  source_success · target_demand · competition_gap · maturity_gap · transferability
score = clamp(base − risk_penalty)          compliance/unfavorable-transfer penalties
score = min(score, low_confidence_score_cap) if overall confidence is LOW
```

Bounded [0, 100]. Every component exposes raw input, normalized score, weight,
weighted contribution, evidence, and confidence, so a score is hand-auditable.
This score answers "how attractive is it to *investigate* taking this proven
product into this target marketplace?" — it is not the product opportunity score.

---

## 11. Confidence

Overall confidence is the **worst** of match confidence, source confidence, and
target confidence (each mapped to high/medium/low). Lower match confidence or
thinner data can only lower it, never raise it. A `LOW` overall confidence caps
the score.

---

## 12. Verdicts and false-positive safeguards

`CrossMarketVerdict`: `STRONG_OPPORTUNITY` / `OPPORTUNITY_TO_VALIDATE` /
`MATURE_MARKET` / `WEAK_TRANSFER` / `INSUFFICIENT_DATA`. Decision order (first
match wins):

1. no identity match → `INSUFFICIENT_DATA`
2. source success not established → `INSUFFICIENT_DATA`
3. target unknown/not-present **and** no credible demand → `INSUFFICIENT_DATA`
   (absence of evidence)
4. transferability unfavorable → `WEAK_TRANSFER`
5. target mature/saturated → `MATURE_MARKET`
6. score ≥ `strong_opportunity_min` **and** demand credible **and** competition
   gap ≥ `strong_competition_gap_min` **and** confidence ≥ medium →
   `STRONG_OPPORTUNITY`
7. score ≥ `validate_min`, or strong source with unproven target demand →
   `OPPORTUNITY_TO_VALIDATE`
8. otherwise → `WEAK_TRANSFER`

Built-in safeguards (each has an explicit test):

- US-proven but **no credible India demand** is not strong (→ validate/insufficient).
- **No listings + no demand** is `INSUFFICIENT_DATA`, never strong.
- **Strong demand + strong incumbents** is `MATURE_MARKET`, not a gap opportunity.
- **Title match + conflicting dimensions** never resolves to `EXACT`.
- **Compliance risk** in the target forces `UNFAVORABLE` transfer → downgrade.
- **Poor transferability** downgrades to `WEAK_TRANSFER` regardless of source
  strength.

---

## 13. Multi-market comparison & ranking

`analyze_cross_market` is one source → one target. `analyze_cross_markets` fans a
single source across many targets (US → CA/UK/AU/IN) with no duplicated logic,
returning an independent `CrossMarketReport` each. Reversing direction (AU → US)
uses the reversed evidence and produces an independent result — the engine is
directional by construction. Reports carry enough structured fields
(`score`, `market_gap`, `transferability`, `confidence`, `target_marketplace`,
`match.confidence`, `source_evidence.maturity`, `target_evidence.demand_credible`)
for a later pipeline to sort/filter the best expansion targets.

---

## 14. Configuration

All thresholds/weights live in `config.toml [cross_market]`
(`CrossMarketConfig`, validated by the existing config system): match thresholds,
source-success thresholds/weights, target-demand thresholds/weights,
competition-weakness weights, presence thresholds, transferability penalties,
composite weights, risk penalties, and verdict thresholds. No second config
system; no magic numbers in the engine.

---

## 15. Known limitations & data to acquire for production

The engine is only as good as its normalized inputs. Gaps found in the current
schema/ingestion that must be filled before production cross-market runs:

- **No GTIN/UPC/EAN column.** The `products` table has no barcode field, so today
  matching cannot reach `EXACT` from stored data — it relies on the caller
  supplying `gtin` on `MarketplaceProduct` where Keepa exposes it. *Recommended:*
  add `gtin TEXT` (and optionally `manufacturer TEXT`) to `products`, populate
  from the Keepa `eanList`/`upcList`/`manufacturer` fields in
  `providers/keepa.py`, and surface them on `ProductView`.
- **No cross-marketplace product-family link.** There is no table mapping one
  underlying product across marketplaces. Persisting `ProductMatch` results (a
  `product_matches` table keyed by source/target ASIN + confidence + signals)
  would let discovery avoid re-matching and let me curate/override matches.
- **Per-marketplace fetches must be requested explicitly.** Keepa/DataForSEO are
  called per marketplace; ingestion currently defaults to `US`. Cross-market
  discovery needs the caller to fetch the same product/keyword cluster in each
  target marketplace and build the `TargetMarketInput` from those normalized rows.
- **Keyword localization is a flag, not a solution.** Target keyword volume must
  be looked up with target-appropriate phrases; the engine flags when that
  re-mapping is needed but cannot perform it.
- **FX is intentionally absent.** Economic comparison is normalized-only until a
  deterministic FX input exists.

These are ingestion/schema tasks, deliberately out of scope for the pure analysis
module; the analysis contract stays clean.
