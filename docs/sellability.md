# Sellability

**Question it answers:** *"If I launched this tomorrow, would it actually sell?"*

Sellability is a **ranking** signal for products that have **already passed** the
hard kills and gates. It never lets a product bypass them: the caller only feeds
eligible products, and `compute_sellability` refuses (`score = None`) when
`eligible=False`. It does **not** change any scoring weight, kill, gate, or
confidence rule — it sits on top.

> ⚠️ All thresholds/weights here are **illustrative** until calibrated against
> real launch outcomes (`delium calibrate` + post-launch review). The UI and
> data files say so.

## What the five existing pillars already use

Read from `docs/scoring-model.md` §4–§8 and `analysis/scoring.py`. The
**opportunity score** already prices:

| Pillar | Signals it already uses |
|---|---|
| Demand (25) | search volume, **sales velocity (BSR-based units of top-10)**, **90d BSR trend**, market growth, seasonality |
| Competition (25) | **median reviews of top-10**, **beatable slots (<150 reviews)**, review velocity, **brand dominance incl. Amazon/big-brand presence**, **listing-quality gap**, price stability |
| Differentiation (20) | complaint intensity, missing features, addressability, bundle/packaging |
| **Profitability (20)** | **net margin, ROI, capital fit, payback** |
| Risk (10) | IP, compliance, trend, seasonality, returns, fragility, etc. |

So anything about **review moat, brand wall, listing quality, price stability,
demand velocity/trend, and all of profitability** is already inside the
opportunity number.

## The double-counting trap

Launchability (`analysis/launchability.py`) is a useful standalone "how beatable
is page one" score, but most of its components **overlap** the competition
pillar:

| Launchability component | Overlaps? |
|---|---|
| median reviews of top-10 | ✅ Competition C1 |
| count of top-10 with >1,000 reviews | ✅ Competition C1/C2 (moat depth) |
| Amazon / established-brand presence | ✅ Competition C4 (+ kill K4) |
| listing-quality gap | ✅ Competition C5 |
| **median listing age** | ❌ not in any pillar |
| **price-band crowding at my target price** | ❌ not in any pillar |

Feeding the whole launchability score into sellability would count reviews,
brand, and listing quality **twice**. Feeding profitability again (via a margin
term) would count it twice too.

## The formula (chosen design)

We use **option (a)**: sellability = a weighted combination of the opportunity
score **plus only the non-overlapping parts of launchability**. This is cleaner
than trying to re-derive a bespoke blend, because the opportunity score is
already the calibrated, gated, confidence-aware verdict — we trust it and add
exactly the two things it cannot see.

```
sellability = Σ(component × weight) / Σ(weight)     over AVAILABLE components

  opportunity          weight 60   # all 5 pillars — counted once
  price_headroom       weight 25   # launchability's price-crowding at MY target
  incumbent_freshness  weight 15   # launchability's median-listing-age signal
```

- **Each signal counts once.** Reviews / brand / listing quality / price
  stability / demand / risk live in `opportunity`. Profitability lives in
  `opportunity` (pillar P) and is **never re-added**. The only additions are the
  two signals the opportunity score is structurally blind to:
  - **price_headroom** — the opportunity score is agnostic to *my specific entry
    price*; whether the band around my target is empty or crowded is new.
  - **incumbent_freshness** — how young/movable the page-one listings are; no
    pillar uses competitor listing age.
- **Absolute scale.** Weights live in `sellability_data/<version>.toml`; each
  input is an absolute 0-100 (opportunity from scoring.py, the other two from
  launchability's fixed-threshold components). Same inputs → same score on any
  day, independent of the batch.
- **Missing = unknown.** An absent component is dropped and the weights
  renormalize over what remains (the same rule as the composite fix). With no
  competitor set, sellability = the opportunity score alone, at LOW confidence.
- **Confidence is a separate badge**, derived from how many components were
  present (all three → HIGH, opportunity-only → LOW). It is never folded into the
  score.
- **No bypass.** `eligible=False` (failed a kill/gate) → `score = None`.

## Output

`SellabilityScore` returns the score, the component breakdown, the list of
missing components, the confidence badge, and a one-sentence plain-English reason
with real numbers, e.g.:

> "Sellability 71/100: opportunity 68/100; price headroom 82/100 (room at your
> target); incumbents movable (74/100)."
