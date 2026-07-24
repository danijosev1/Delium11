# Delium — Data Economics Report (Pre-Development)

**Purpose:** Before writing code, determine whether a product-validation mission can be produced at a cost that supports a $99/month, 10–20-mission subscription. This document defines the V1 data stack, calculates per-mission COGS, and states the go/no-go conditions.

**Pricing verified July 2026** from provider public pricing pages (sources in §9). All figures USD unless noted; €1 ≈ $1.10.

---

## 1. Data Requirements → Source Mapping

| Data need | What the mission uses it for | Realistic source |
|---|---|---|
| Product info (title, brand, category, images, dimensions) | Analyst context, fee calculation | Keepa, Rainforest, DataForSEO Merchant |
| BSR / rank + history | Demand trend, seasonality | **Keepa** (only provider with deep rank history) |
| Estimated sales | Demand analysis headline number | Derived from **Keepa rank-drop counts** (their "sales" proxy); Jungle Scout API as accuracy benchmark |
| Keywords / search volume | Demand + keyword landscape | **DataForSEO Labs Amazon** (bulk Amazon search volume) |
| Reviews (text) | Review Miner themes, differentiation angles | Scraper (Apify actor or Unwrangle) — see §4, this is the fragile one |
| Pricing + price history | Profit model, price-war detection | **Keepa** |
| Competition signals (top ASINs for a keyword, review counts, ratings, listing quality) | Competition analysis | DataForSEO Amazon SERP or Rainforest search endpoint + Keepa per-ASIN data |

Two structural facts shape everything below:

1. **No single provider covers all seven needs.** Keepa has no search volume and no review text; DataForSEO has no rank history; Rainforest is a page-scrape with no history and no volume. V1 needs 2–3 providers behind the one adapter.
2. **Amazon locked reviews behind login (Nov 2024 → hardened Feb 2025).** Anonymous access yields roughly the top ~100 reviews per ASIN at best; "recent reviews" require account cookies (a ToS/fragility risk we will not take in V1). **The original assumption of 500–1,000 reviews per mission is not realistically purchasable at sane cost — and not necessary** (theme extraction saturates well before 300 reviews).

---

## 2. Provider Evaluation

### Keepa API — history + sales-proxy backbone ✅ core

- **Availability:** product details, price/BSR/review-count history (years deep), category context, rank-drop-based sales estimation. No search volume, no review text.
- **Accuracy:** the de-facto industry standard for price/rank history; rank-drop sales proxy is the same signal most "estimators" are built on.
- **Limits:** token bucket. €49/mo (~$54) = 20 tokens/min (~28.8k/day). A product query with history ≈ 2–4 tokens → a 21-ASIN mission ≈ 50–90 tokens ≈ **4 minutes of token budget**. Fine for dozens of missions/day; the €19 (1 token/min) tier is too slow for our 3-minute mission target.
- **Pricing model:** flat subscription → **marginal cost per mission ≈ $0**; amortized cost falls with volume.

### DataForSEO — Amazon search volume + SERP ✅ core

- **Availability:** Labs "Amazon bulk search volume" (up to 1,000 keywords/request), Amazon related-keywords, Amazon SERP (keyword → ranked ASINs) via Merchant API.
- **Accuracy:** clickstream-modeled volumes — adequate for relative demand ranking, which is what the report needs; we present volumes as ranges anyway.
- **Limits:** pay-as-you-go, no monthly minimum, live endpoints fast enough for mission latency.
- **Pricing:** bulk volume ≈ $0.012/task + $0.00012/keyword → **100 keywords ≈ $0.024**. SERP pages ~$0.002–0.008 → 3–5 pages ≈ **$0.01–0.04**.

### Rainforest API — all-rounder ❌ not for V1 core, kept as fallback

- **Availability:** product pages, search results, some review data. No history, no search volume.
- **Pricing:** Starter $66/mo = 10k credits (~$0.0118/request, before 2–3× multipliers on some params); $0.003/req requires $300/mo commit.
- **Verdict:** at Starter pricing a 21-ASIN + search + reviews mission ≈ 55+ requests ≈ **$0.65/mission** for shallower data than Keepa+DataForSEO deliver for ~$0.05 marginal. The $300/mo tier only makes sense at a scale we don't have. Fallback if Keepa or DataForSEO fails us.

### Jungle Scout API — benchmark, not dependency

- $29–199/mo, but **gated behind their seller-platform plans**, low call quotas, and ToS aimed at sellers, not resellers of their data. Claimed ~84% sales-estimate accuracy — the best public number in the category. **Use:** one month, cheapest tier, as ground truth to calibrate our Keepa-derived estimates in week 1. Not a runtime dependency.

### Review scrapers (Apify actors / Unwrangle) — necessary evil ⚠️

- **Availability:** ~top 100 reviews per ASIN anonymously; more requires cookies (declined for V1).
- **Pricing:** ≈ **$3 per 1,000 reviews** (Apify PAYG); Unwrangle similar order of magnitude (1 credit/request, ~10 reviews/request).
- **Fragility:** this category breaks whenever Amazon changes markup or policy. Isolate behind the adapter, cache for 7 days, and design the Review Miner to degrade gracefully (report flags "limited review sample" instead of failing the mission).

### Others considered and rejected for V1

- **Amazon SP-API:** not accessible to non-sellers; doesn't carry research data (volume, estimates). Out.
- **Oxylabs / Bright Data / Smartproxy:** raw scraping infra — more control, more maintenance than a solo founder should own in V1.
- **Helium 10 / similar:** no public API; ToS prohibits it.

---

## 3. Mission Cost Model

Mission profile (revised): **1 target ASIN + 20 competitors (Keepa), 100 keywords + 4 SERP pages (DataForSEO), reviews capped at ~300** (target + top 2 competitors × ~100) — see §4 for why 300, not 1,000.

### Variable data cost per mission (uncached)

| Component | Calc | Cost |
|---|---|---|
| Keepa: 21 ASINs w/ history | flat plan, ~70 tokens | $0.00 marginal |
| DataForSEO: 100 keyword volumes | $0.012 + 100×$0.00012 | $0.024 |
| DataForSEO: 4 SERP pages | 4 × ~$0.005 | $0.02 |
| Reviews: ~300 via scraper | 0.3 × $3.00 | $0.90 |
| **Variable data total** | | **≈ $0.95** |

### Fixed data cost per month

| Item | Cost |
|---|---|
| Keepa API (20 tokens/min) | ~$54/mo |
| Jungle Scout (calibration month only, weeks 1–4) | ~$49 one-time |
| **Ongoing fixed** | **~$54/mo** (covered by the first customer) |

### AI cost per mission (one vendor, two tiers)

Assumptions: fast tier ≈ $1/$5 per M tokens in/out; frontier ≈ $3/$15.

| Step | Tier | Tokens (in/out) | Cost |
|---|---|---|---|
| Orchestrator | fast | 5k / 1k | $0.010 |
| Product Analyst | fast | 35k / 3k | $0.050 |
| Review Miner (300 reviews ≈ 45k tok) | fast | 50k / 3k | $0.065 |
| Report Writer | frontier | 20k / 4k | $0.120 |
| Retries/overhead (+25%) | | | $0.061 |
| **AI total** | | | **≈ $0.31** |

### Total COGS per mission

| Scenario | Data | AI | **Total** |
|---|---|---|---|
| Uncached (worst case) | $0.95 | $0.31 | **$1.26** |
| 40% cache hit on ASINs/keywords/reviews | ~$0.60 | $0.31 | **~$0.91** |
| Deep review mission (1,000 reviews — NOT default) | $3.05 | $0.45 | $3.50 |

Sensitivity: the model is **dominated by review scraping** (71% of uncached variable cost). Every other line item is noise. This is where engineering attention on caching and sample-size discipline pays off.

---

## 4. Why 300 Reviews, Not 500–1,000

1. **Access:** anonymous scraping tops out around ~100 reviews/ASIN post-login-wall. 1,000 reviews means cookie pools or keyword-permutation tricks — fragile, slow, and a ToS posture we don't want at launch.
2. **Cost:** 1,000 reviews ≈ $3.00 — that single line item would exceed the entire target COGS.
3. **Quality:** complaint/praise theme extraction saturates: the top ~100 reviews of the target plus ~100 each from the two strongest competitors reliably surface the recurring themes. More reviews sharpen percentages, not insights. The report's methodology block states the sample size honestly.

If a customer segment later demands deep review mining, it becomes a priced add-on (its cost is real), not a default.

---

## 5. Business Model Check

**Price point: $99/mo. Included missions: 10–20.**

| Metric | 10 missions/mo | 15 | 20 |
|---|---|---|---|
| Revenue per mission | $9.90 | $6.60 | $4.95 |
| COGS/mission (uncached $1.26) | 13% | 19% | 25% |
| **Gross margin** | **87%** | **81%** | **75%** |

- Fixed data cost ($54/mo Keepa) is covered by **the first customer**; it amortizes to <$1/mission at 60 missions/mo across the base.
- Realistic behavior: most subscribers use well under their cap (industry norm), and cache hits grow with usage — real margins land **above** the table.
- Absolute worst case (every customer maxes 20 missions, zero cache): 75% gross margin. **Still a healthy SaaS.**

### Maximum acceptable cost per mission

- **Target:** ≤ $2.00 fully loaded (data + AI) → ≥80% margin at 10 missions/$99.
- **Hard ceiling:** $3.00/mission — beyond this at the 20-mission cap, margin drops below 40% and the plan structure must change (raise price, lower cap, or add overage).
- Current model sits at **$1.26**, 37% headroom under target. Enforced in product by the per-mission budget caps already specified in ARCHITECTURE.md §6/§8, and watched via `mission_steps.cost_usd`.

### Verdict: **the business model works.**

The $99 price carries the cost structure comfortably. The three conditions attached:

1. **Review sample stays capped (~300)** and deep review mining is never silently unlimited.
2. **Keepa-derived sales estimates prove "good enough."** This is the open accuracy question — the calibration exercise in §7 answers it in week 1.
3. **A lower-priced Starter tier (e.g. $29/10 missions) also clears the math** ($2.90/mission revenue vs $1.26 COGS ≈ 57% margin — acceptable for an acquisition tier, thin; keep Starter at 5–8 missions if we want margin symmetry).

---

## 6. Recommended V1 Data Stack

| Role | Provider | Cost shape |
|---|---|---|
| Product data, BSR/price history, sales proxy | **Keepa API** (€49 tier) | Flat ~$54/mo |
| Amazon search volume + SERP/competitor discovery | **DataForSEO** (Labs Amazon + Merchant SERP) | PAYG ≈ $0.05/mission |
| Review text (capped ~300/mission) | **Apify Amazon-reviews actor** (or Unwrangle — pick in bake-off) | PAYG ≈ $0.90/mission |
| Sales-estimate calibration (temporary) | Jungle Scout cheapest tier, month 1 only | ~$49 once |
| Fallback if any core fails bake-off | Rainforest API Starter | $66/mo if needed |

All behind the single `data/provider.ts` adapter with the Postgres read-through cache (TTLs: products 24h, keywords 7d, reviews 7d) — per ARCHITECTURE.md §8.

---

## 7. Week-1 Validation Plan (do this before building UI)

1. **Sign up:** Keepa €49, DataForSEO PAYG ($50 deposit), Apify free tier, Jungle Scout cheapest plan.
2. **Run 10 real missions by hand** (scripted API calls, no product code): 10 ASINs across categories we care about (Home & Kitchen, Sports, Pet, Kitchen electrics, one gated category on purpose).
3. **Measure per mission:** actual API spend per provider, wall-clock latency, failure/empty-response rate, review counts actually retrievable per ASIN.
4. **Calibrate estimates:** compare Keepa rank-drop-derived monthly-unit ranges against Jungle Scout's estimator for the same 30 ASINs. **Pass bar: our range brackets JS's point estimate for ≥70% of ASINs.** If we fail, the Demand block ships with wider ranges and stronger caveats — or we license an estimates source before launch.
5. **Stress the review path:** confirm ~100 reviews/ASIN is actually retrievable today for 20 random ASINs; measure scraper failure rate. If >15% fail, evaluate Unwrangle/second actor before committing.
6. **Output:** one spreadsheet — cost, latency, failure rate per mission — checked into `docs/`. Go/no-go on the stack, and real numbers replace the estimates in §3.

**Kill criteria for this plan:** if week-1 measured COGS exceeds $3/mission with the capped review sample, or sales-estimate calibration fails badly and no affordable estimates source exists, we stop and rethink the product shape (e.g., keyword-first reports without unit estimates) **before** writing application code.

---

## 8. Risks Register (data-specific)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Amazon further locks reviews (even top-100 gone) | Medium | High — Review Insights block degrades | Adapter isolation; graceful "limited sample" reporting; Unwrangle/Apify redundancy; long 7-day cache |
| Keepa ToS/pricing change | Low | High — history backbone | Cache accumulates our own history from day 1; Rainforest fallback for current-state data |
| Sales-estimate accuracy complaints | Medium | High — trust is the product | Ranges + methodology block; week-1 calibration; never show false precision |
| DataForSEO volume quality poor in niches | Medium | Medium | Present volume as relative demand; cross-check top keywords during bake-off |
| Scraper latency blows 3-min mission target | Medium | Low | Parallel fetch; reviews fetched concurrently with Keepa calls; report can complete with partial reviews |

---

## 9. Sources

- Keepa API plans & token rates: fbamultitool.com Keepa subscription guide; saasworthy.com Keepa pricing
- Rainforest API tiers ($66 Starter/10k credits; $300 Pro; credit multipliers): asinspotlight.com and flybyapis.com comparisons
- DataForSEO Amazon bulk search volume ($0.012/task + $0.00012/keyword) & Merchant Amazon pricing: dataforseo.com pricing pages, docs.dataforseo.com
- Amazon review login wall (Nov 2024 / Feb 2025) and 100-review pagination cap: scrape.do blog, Apify actor documentation
- Apify review scraping ≈ $3/1,000 results; free tier: apify.com actor pages
- Jungle Scout API gating, $29–199/mo, ~84% estimate accuracy claim: revenuegeeks.com, demandsage.com

*Numbers current as of July 2026; re-verify at bake-off signup — provider pricing in this category changes quarterly.*
