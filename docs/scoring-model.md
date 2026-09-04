# Delium — Product Opportunity Scoring Model

**Purpose:** Given thousands of candidates, rank the handful that deserve my attention and capital. The score is a **triage instrument, not a decision** — it filters and ranks; the Strategist argues; I decide. Implemented deterministically in `analysis/scoring.py`; every input and formula lands in the report's methodology section so any score is checkable by hand.

**Optimized for my situation:** private label, $5k–$20k launch capital, ability to improve existing products/listings, "reasonable competition" over virgin markets (a market with flawed winners beats an empty one), and margin discipline over revenue vanity.

---

## 1. The Funnel

```
thousands of candidates
   │
   ├─ STAGE 0  HARD KILLS (§2)          — binary, no score computed, reason logged
   │
   ├─ STAGE 1  DATA SUFFICIENCY (§3)    — enough evidence to score honestly?
   │
   ├─ STAGE 2  PILLAR SCORES (§4–§8)    — five sub-scores, 0–100 each
   │
   ├─ STAGE 3  WEIGHTED SCORE (§9)      — 0–100 composite
   │
   └─ STAGE 4  GATES + THRESHOLDS (§10) — Buy / Test / Avoid
```

A product can score 80 and still be killed by a gate. A gate failure is never silently averaged away by good pillars — that's how people end up launching a beautiful-margin product into a patent lawsuit.

**Normalization convention** used throughout: `norm(x, lo, hi)` maps x linearly to 0–100, clipped; `log_norm` does the same in log-space (used where the underlying quantity spans orders of magnitude, like review counts and volumes). Piecewise "sweet-spot" curves are written out explicitly.

---

## 2. Stage 0 — Hard Rejection Rules

Applied at triage (during `discover`) using cheap data only. Each kill is logged to `candidates.status = rejected` with the rule name — the rejected list is reviewable, because kill rules encode my constraints, not truths.

| # | Rule | Rationale |
|---|---|---|
| K1 | Market median price < $15 | No room for fees + PPC; race-to-bottom territory |
| K2 | Market median price > $70 | Capital per unit too high for a $5–20k launch (inventory depth suffers) |
| K3 | Oversized / heavy (Keepa dims → FBA tier above "large standard") | Fees + freight eat the model; storage risk |
| K4 | Amazon (AmazonBasics/private brands) in top 5 organic | You don't out-margin the referee |
| K5 | Single brand holds ≥ 5 of top 10 slots | Brand-dominated; PPC will be a knife fight with someone richer |
| K6 | Median review count of top 10 > 3,000 | Moat too deep to climb on $5–20k |
| K7 | Every top-10 listing already excellent (Analyst listing-quality avg ≥ 8/10) AND complaint rate < 5% | Nothing to improve = no wedge for a differentiator |
| K8 | Gated category / restricted (topicals, supplements-adjacent, medical claims, batteries where flagged) | Compliance overhead I've excluded in config `avoid` list |
| K9 | Obvious IP signature: character/brand-likeness products, "as seen on TV" clones, design-patent-lookalike categories | Patent risk is the one that zeroes accounts |
| K10 | Trend spike: search volume > 3× its 12-month median AND < 12 months of volume history | Fad. By the time inventory lands, the wave broke |
| K11 | Estimated launch capital > $20k or < $2k at required inventory depth | Outside my band; sub-$2k markets are usually commodity churn |
| K12 | Config `avoid` list match (glass, fragile, hazmat, etc.) | My operating preferences |

K-rules use conservative inputs (e.g., K11 uses the *low* end of the demand range). Borderline cases (within 10% of a threshold) are demoted to the `Test` track rather than killed — thresholds are fences, not cliffs.

---

## 3. Stage 1 — Data Sufficiency Requirements

A score is only as honest as its inputs. Minimum evidence before a full score is valid:

| Requirement | Minimum | If unmet |
|---|---|---|
| Keepa history on top-10 competitors | ≥ 60 days for ≥ 7 of 10 | Demand pillar capped at 60; flag `data_quality: partial` |
| Review sample | ≥ 150 reviews across target + top 3 | Differentiation pillar capped at 50; complaints marked low-confidence |
| Keyword volume data | Primary cluster resolved, ≥ 5 keywords with volume | Demand capped at 50 |
| Price history | ≥ 90 days on ≥ 5 of top 10 | Price-war component defaults to neutral (50) |
| Fee inputs | Dimensions + weight + category known | **Blocking** — no profitability score without real fees; fetch or hand-enter |

**Rule: missing data never defaults to optimistic.** Unknown = neutral-to-pessimistic, visibly flagged. A product cannot reach `Buy` with any pillar in `partial` state (§10).

---

## 4. Pillar 1 — Demand (weight 25)

What I'm actually asking: *is there durable, reachable money here — enough to matter, not so much that it's a war zone?*

| Component | Wt | Formula |
|---|---|---|
| D1 Search volume | 30% | Aggregate exact+variant monthly volume of primary keyword cluster: `log_norm(vol, 2000, 40000)`. Below 2k there's no market; above 40k adds competition faster than opportunity, so it flatlines (not penalized here — competition pillar handles it) |
| D2 Sales velocity | 30% | Median est. units/mo of top-10 (Keepa rank-drop method): **sweet-spot curve** — 0 at <150, rises to 100 across 300–1,200, decays to 70 above 2,500. I want markets where page-2 sellers still eat; hyper-velocity markets are capital games |
| D3 BSR trend | 20% | Median 90-day BSR slope across top-10 (log scale): improving ranks → `norm(-slope)`. A cluster whose incumbents are all *rising* is a growing tide; all sinking = leaking market |
| D4 Market growth | 10% | Keyword volume 12-mo trend: `norm(yoy_growth, -10%, +40%)`. Modest growth scores well; hypergrowth is already captured (and distrusted) by K10 |
| D5 Seasonality | 10% | `100 − norm(peak_concentration, 20%, 60%)` where peak_concentration = share of annual demand in the biggest 8 weeks (from Keepa BSR annual pattern). Even seasonality scores 100; a Q4-only product scores ~0 here **and** takes a risk deduction |

`demand = Σ(component × wt)`. Also emitted: `market_size_range` (units/mo, low–high) — used by K11 and payback math, never shown as a point estimate.

---

## 5. Pillar 2 — Competition (weight 25)

What I'm asking: *can a well-executed new entrant with $5–20k realistically take a top-10 slot within ~6 months?* High score = beatable market, not empty market.

| Component | Wt | Formula |
|---|---|---|
| C1 Review moat | 30% | Median reviews of top-10: `100 − log_norm(median_reviews, 200, 3000)`. Under ~200 median is climbable with a good launch; 3,000+ was killed at K6 anyway |
| C2 Beatable slots | 20% | Count of top-10 with < 150 reviews: `min(count, 4) × 25`. Every weak incumbent on page one is proof the algorithm will seat newcomers |
| C3 Review velocity of leaders | 15% | Median new-reviews/month of top-3 (review count deltas from Keepa history): `100 − log_norm(velocity, 20, 300)`. Fast-compounding leaders rebuild any gap I close |
| C4 Brand dominance | 15% | `100 − (top_brand_slot_share × 100) − (recognized_big_brand_present ? 25 : 0)`, floor 0. (≥50% share already killed at K5) |
| C5 Listing quality gap | 15% | From Analyst (structured rubric: image count/quality, A+ presence, title keyword coverage, bullet quality — each observable, not vibes): `100 − avg_top10_quality × 10`. Mediocre incumbent listings are my favorite signal — improvement is my entire edge |
| C6 Price stability | 5% | Coefficient of variation of top-10 prices over 90 days (Keepa): `100 − norm(cv, 5%, 25%)`. High variance = active price war = margin mirage |

---

## 6. Pillar 3 — Differentiation Opportunity (weight 20)

The pillar my whole strategy leans on: *is there a documented, addressable reason customers would switch to a better version?* Inputs come from the Review Miner (frequencies over the actual sample, quotes attached) — never from model imagination.

| Component | Wt | Formula |
|---|---|---|
| F1 Complaint intensity | 40% | `Σ over complaint themes: freq_pct × severity(1–3)`, normalized `norm(x, 10, 60)`. A market where 25% of reviews complain about the same fixable flaw is a gift |
| F2 Missing features | 25% | Count of distinct features requested in reviews but absent across top-10 (Review Miner cross-check): `min(count,4) × 25` |
| F3 Addressability | 20% | Of the complaint/feature mass above, share fixable at ≤ 15% COGS delta (Strategist assessment against complaint list, conservative): `share × 100`. "Battery dies fast" is addressable; "physics of the product category" is not |
| F4 Bundle & packaging | 15% | Rubric points, 25 each (cap 100): natural bundle complement exists · top-10 don't bundle it · packaging is a stated complaint (damage/unboxing) · usage-instruction complaints (insert opportunity) |

Honesty guard: F1/F2 compute only from themes with ≥ 3 supporting quotes in the sample. One angry review is an anecdote, not a wedge.

---

## 7. Pillar 4 — Profitability (weight 20)

All inputs from `analysis/profit.py` (deterministic; assumptions from `config.toml`, conservative defaults: COGS 25% of price, TACOS 15%, returns 4%). The pillar scores the *robust* case, not the best case: margin/ROI evaluated at price −10% and COGS +15%.

| Component | Wt | Formula |
|---|---|---|
| P1 Net margin (stressed) | 35% | `norm(margin, 25%, 45%)`. Below 25% net (after PPC drag) a private label can't absorb surprises; 45%+ is excellent |
| P2 ROI (stressed) | 25% | `norm(roi, 100%, 300%)` per inventory turn |
| P3 Capital fit | 20% | Launch capital (2.5 months inventory at low-end demand estimate + PPC ramp + samples/photography ≈ $1.5k fixed): 100 inside $5–14k · linear to 60 at $17k · linear to 0 at $20k · 70 in $3–5k (thin markets, but capital-efficient) |
| P4 Payback | 20% | Months to recoup launch capital at low-end demand × stressed margin: 100 ≤ 4mo · linear to 0 at 9mo |

Gate linkage (§10): failing config gates (min margin 30% *unstressed*, min ROI 100%, payback ≤ 6mo) caps this pillar at 40 regardless of components.

---

## 8. Pillar 5 — Risk (weight 10, deduction-based)

Starts at 100, subtracts per confirmed flag (flags come from Analyst/Strategist with citations, or deterministic checks). Floor 0.

| Flag | Deduction | Detection |
|---|---|---|
| IP/patent signals (design-heavy category, litigious brand present, patent-marked listings) | −40 | Analyst flag + K9 didn't trigger but adjacent |
| Compliance surface (certifications: FDA-adjacent, CPSIA/children, electrical) | −30 | Category + listing analysis |
| Trend dependency (volume history < 24mo or strongly correlated with a fad signal) | −25 | DataForSEO trend history |
| Seasonality concentration > 40% | −20 | Same input as D5 (double-counted deliberately — seasonality hits both demand quality and risk) |
| High-return category (apparel-like fit issues, sizing complaints > 10% of reviews) | −20 | Review Miner theme |
| Fragility (breakage/damage complaints > 8% of reviews, or glass/ceramic materials) | −20 | Review Miner + attributes |
| Single-keyword dependence (> 60% of cluster volume in one keyword) | −15 | Keyword cluster distribution |
| Supplier complexity (electronics with firmware, multi-part assemblies) | −10 | Attributes |

Reported as the flag list with evidence, not just the number — the risk section of the report is this table with citations.

---

## 9. Composite Score

```
score = 0.25×Demand + 0.25×Competition + 0.20×Differentiation
      + 0.20×Profitability + 0.10×Risk
```

Weights live in `config.toml [score_weights]` — they encode my risk appetite and are expected to drift as verdicts get post-mortemed (§11). Two structural notes:

- **Differentiation at 20 is the strategy-defining weight.** A generic me-too product in a good market should *not* score well here — and that's correct behavior, because me-too is not my playbook.
- **Risk at 10 looks low but isn't** — catastrophic risk lives in the kill rules and gates, not the weight. The pillar only prices *survivable* risk into the ranking.

---

## 10. Gates & Verdict Thresholds

Gates are pass/fail checks applied **after** scoring — a high score cannot buy its way past one:

| Gate | Requirement |
|---|---|
| G1 Profit gates | Margin ≥ 30%, ROI ≥ 100%, payback ≤ 6mo (unstressed, config-driven) |
| G2 Data quality | No pillar in `partial` state |
| G3 Risk floor | Risk pillar ≥ 40 (i.e., no near-catastrophic flag combination) |
| G4 Differentiation floor | Differentiation ≥ 45 — I don't launch products I can't make meaningfully better |
| G5 Strategist concurrence | Frontier agent verdict is `buy`, with risk register and verdict-changers populated |

| Verdict | Criteria | Meaning |
|---|---|---|
| **BUY** | Score ≥ 75 **and** all gates pass | Request supplier quotes + samples this week. Expected hit-rate at calibration: ~1–3% of validated candidates |
| **TEST** | Score 60–74 with gates passing, **or** score ≥ 75 with exactly one soft-gate failure (G2/G4), **or** borderline kill-rule demotions | Worth money-limited exploration: order competitor samples, get real supplier quotes to replace COGS assumptions, put on `watch`. Re-score with real numbers |
| **AVOID** | Score < 60, or any hard gate failure (G1/G3/G5) | Logged with reasons; `watch` may keep an eye on genuinely interesting AVOIDs whose blocker is temporal (e.g., moat rising but market growing faster) |

**Score/Strategist disagreement is surfaced, never averaged**: "scores 78 — Strategist says avoid: category shows design-patent enforcement pattern" appears verbatim at the top of the report. When they disagree, the pessimist wins the default and I make the final call.

**G5 implementation note (`analysis/scoring.py`).** `score_opportunity` takes the *resolved* Strategist concurrence as a validated input (`StrategistConcurrence`: `pending | concur | dissent | unavailable`) — the LLM never sets the verdict; scoring.py evaluates the gate. G5 is a **Buy gate only**: it can block a would-be Buy but can never manufacture a Buy nor change a non-Buy verdict. `concur` confirms a qualifying Buy; `dissent` or `unavailable` caps a would-be Buy at **Test** (not Avoid) with the disagreement surfaced — this reconciles the "hard gate → Avoid" phrasing above with the agent-layer rule that a missing/withheld Strategist means "no Buy can be issued," and guarantees the agent layer can only ever make the system *more* cautious. `pending` (the default / agents-off) leaves a provisional Buy, matching the pre-agent behavior. A hard kill or a failed G1/G3 still forces Avoid regardless of concurrence.

---

## 11. Calibration & Honesty Rules

1. **The $10k test.** After each `validate`, the report footer asks the only question that matters: *would I personally wire $10k against this analysis?* If my gut says no while the score says Buy, the gap gets written down — that log is the calibration dataset.
2. **Post-mortems move weights.** After ~20 validations (and especially after any real launch), review: did high-Differentiation picks outperform? Are Demand estimates biased vs. observed reality? Adjust `[score_weights]` and normalization bounds in config — never hand-adjust an individual score.
3. **No retroactive fitting.** A score is frozen with its `config_snapshot` (runs table). Recalibration changes future scores; it never rewrites old ones.
4. **Ranges beat points everywhere.** Any component whose input is a range (demand, margin) is computed at the conservative end for gates and the midpoint for ranking. Optimism is allowed in ranking; never in gates.
5. **The score cannot see sunk cost.** Re-validating a product I already like uses fresh data and the same pipeline. The tool exists precisely to argue with my enthusiasm.

---

## 12. Worked Example (illustrative)

Silicone baby-food freezer tray, median price $22, top-10 median 480 reviews, 2 weak slots, 22% of reviews complain about lids cracking, volume cluster 9,400/mo, stable trend, stressed margin 33%, capital $8.2k, payback 4.5mo, one risk flag (CPSIA children's product −30):

```
Demand:          D1 log_norm(9400)≈52 ·30% + D2 (median 410 units)≈78 ·30% + D3 flat 55 ·20%
                 + D4 +8% 45 ·10% + D5 even 88 ·10%  →  63
Competition:     C1 (480)≈66·30% + C2 (2 slots) 50·20% + C3 40·15% + C4 85·15% + C5 60·15% + C6 80·5%  →  62
Differentiation: F1 (22×2 sev=44)→57·40% + F2 (2 feats) 50·25% + F3 0.7→70·20% + F4 50·15%  →  57
Profitability:   P1 (33%)→40·35% + P2 (140%)→20·25% + P3 (8.2k) 100·20% + P4 (4.5mo) 90·20%  →  57
Risk:            100 − 30 (CPSIA)  →  70

Score = .25(63) + .25(62) + .20(57) + .20(57) + .10(70) = 61 → TEST
Gate note: G4 differentiation 57 ✓ · G1 ✓ · compliance flag → real answer is
"order samples + confirm CPSIA testing cost, then re-score with actual COGS."
```

Which is exactly the right answer for that product — promising, not proven. The model's job is to say *that*, thousands of times, so I only spend evenings on the sixty-ones and up.
