# Delium — Personal AI Amazon Product Research System

**Private tool. One user: me. No SaaS, no auth, no billing.**

The system's job: behave like an experienced 7-figure Amazon private-label seller with unlimited patience — scan markets, validate opportunities rigorously, mine customer pain, model profit honestly, and tell me **buy or avoid, why, and how I'd differentiate** — while I stay the decision-maker. It runs on my machine, writes reports to files, and never acts on its own.

Prior SaaS-era design docs are superseded (archived in `docs/archive/`). The provider research in `docs/data-economics.md` still applies — the same data stack, now at hobby cost.

---

## 1. Design Principles

1. **Simplest thing that produces expert-grade judgment.** A CLI + a pipeline + files. No web app, no queue, no workers, no Docker. If a feature doesn't change a buy/avoid decision, it doesn't exist.
2. **AI interprets; code calculates.** Every number that goes into a decision — fees, margins, ROI, opportunity score — is deterministic Python with my assumptions in a config file. LLMs never do arithmetic and never invent data.
3. **Human in the loop by construction.** The system's only outputs are reports and a candidate database. It never orders samples, never messages suppliers, never touches money.
4. **Evidence or it didn't happen.** Every qualitative claim cites the data behind it (review quotes, BSR history, keyword volumes, fetched-at timestamps). A claim with no citation is discarded before it reaches a report.
5. **Cheap enough to run daily.** Target running cost: **~$75–110/month total** (Keepa €49 + DataForSEO ~$10 + review scraping ~$10–20 + LLM ~$10–30 at heavy personal use).

---

## 2. Stack Decision

**Python.** Next.js earns its keep when there's a UI and users; there's neither. Python wins on data wrangling, quick iteration, and running from cron.

| Component | Choice | Why |
|---|---|---|
| Runtime | Python 3.12, single repo, `uv` for deps | One-command setup |
| CLI | Typer | `delium validate B0XXXXXXX` is the whole UX |
| Schemas | Pydantic | Every agent output validated; every data payload typed |
| Storage | **SQLite** (one file, WAL mode) | Postgres/Supabase is unnecessary for one user; SQLite handles caching, candidates, history, full-text search over reviews. Revisit only if a second machine needs concurrent writes |
| LLM | One vendor, two tiers (fast: Haiku-class / frontier: Sonnet-class), one thin `llm.py` | Same two-tier discipline as before; vendor swap is one line |
| Data | Keepa API + DataForSEO (Amazon volume + SERP) + Apify/Unwrangle reviews | Verified stack from `docs/data-economics.md`; ~$1–1.50 per deep validation, near-zero for discovery scans |
| Reports | Markdown files (+ optional self-contained HTML render) in `reports/`, git-ignored | Readable anywhere, diffable, permanent |
| Scheduling | cron (optional) for watchlist refresh | No queue system |

---

## 3. Repository Layout

```
delium/
├── delium/
│   ├── cli.py                 # typer app: discover · validate · pains · watch · portfolio
│   ├── config.py              # loads config.toml, validates with pydantic
│   ├── db.py                  # SQLite schema + migrations (executescript on version bump)
│   ├── data/                  # ALL external data access lives here
│   │   ├── keepa.py           # products, BSR/price history, rank-drop sales proxy
│   │   ├── dataforseo.py      # Amazon search volume, related keywords, SERP
│   │   ├── reviews.py         # review sampling (~100/ASIN cap), graceful degradation
│   │   └── cache.py           # read-through SQLite cache, TTLs, spend ledger
│   ├── analysis/              # DETERMINISTIC — no LLM imports allowed here
│   │   ├── fees.py            # FBA size tiers, fulfillment + referral fees, storage
│   │   ├── profit.py          # landed cost, PPC drag, margin, ROI, payback
│   │   ├── demand.py          # BSR-history → monthly-units range, trend, seasonality
│   │   └── scoring.py         # opportunity score: weighted sub-scores, weights from config
│   ├── agents/
│   │   ├── runner.py          # prompt → LLM → pydantic-validated JSON, retry-once, budget caps
│   │   ├── scout.py           # discovery: niche expansion + candidate triage
│   │   ├── analyst.py         # market interpretation over fetched data
│   │   ├── review_miner.py    # pain/praise themes, missing features, quotes
│   │   ├── strategist.py      # verdict, differentiation plan, risks (frontier)
│   │   └── prompts/           # versioned prompt text files
│   └── report/
│       └── render.py          # verdict → markdown/HTML from typed blocks
├── config.toml                # my assumptions & preferences (§8)
├── data/delium.db             # SQLite (git-ignored)
├── reports/                   # generated reports (git-ignored)
└── evals/golden/              # ~10 frozen runs for prompt-change sanity checks
```

Three hard boundaries, enforced by convention and a lint rule:
- `analysis/` never imports `agents/` (numbers stay deterministic).
- `agents/` never calls providers directly — only via `data/` (caching + spend caps are structural).
- `report/` renders typed blocks only — LLM prose never becomes HTML unescaped.

---

## 4. The Five Commands (workflows, not services)

### 4.1 `delium discover "<seed niche or keyword>"` — Product Discovery

Find candidates worth validating. Cheap and wide.

```
seed → DataForSEO: related keywords + volumes (hundreds, ~$0.05)
     → filter by config thresholds (volume range, trend ≥ flat)
     → SERP top-10 per surviving keyword → candidate ASIN pool
     → Keepa quick stats per ASIN (price, BSR, reviews, seller count — flat-rate)
     → deterministic triage score (demand/competition heuristics)
     → Scout agent (fast tier): clusters candidates into niches, flags
       underserved patterns (high volume + weak listings + low review moats),
       kills obvious traps (brand-dominated, race-to-bottom pricing)
     → writes candidates to DB + reports/discover-<slug>-<date>.md
       (ranked shortlist with one-line theses)
```

Growing-niche detection: volume trend from DataForSEO + Keepa BSR trajectories of incumbents (improving BSR across a cluster = rising tide). Underserved: volume high, top-10 average review count low, listing quality gaps flagged by Scout.

### 4.2 `delium validate <ASIN|keyword>` — Full Market Validation

The deep dive. This is the old "mission," richer now that COGS pressure is personal (~$1.50 data + ~$0.50 LLM per run is fine).

```
Stage 1  FETCH (parallel, all cached):
         target + top ~20 competitors (Keepa, with 90-day+ history)
         keyword set + volumes (DataForSEO), SERP structure
         reviews: target + top 3 competitors × ~100 (~400 total)

Stage 2  COMPUTE (deterministic):
         demand.py    → monthly-units range per competitor, market size range,
                        trend, seasonality flags (from BSR history, not vibes)
         fees.py      → size tier, FBA + referral fees at market price
         profit.py    → full unit economics (§6)
         scoring.py   → sub-scores (§7) — provisional, pre-agent

Stage 3  INTERPRET (agents, in order):
         Analyst (fast)       → market structure: who wins and why, price bands,
                                review moats, listing quality gaps, brand dominance
         Review Miner (fast)  → §5 pain analysis on the fetched review sample
         Strategist (frontier)→ reads ALL of the above → verdict + rationale +
                                differentiation plan + risk register + what would
                                change the verdict

Stage 4  SCORE & RENDER:
         scoring.py finalizes (differentiation sub-score uses Review Miner output)
         report/render.py → reports/validate-<asin>-<date>.md
         run + costs + verdict stored in DB
```

### 4.3 `delium pains <ASIN>` — Customer Pain Deep-Dive

Standalone review mining when I already like a market: fetches up to ~100 reviews each for target + up to 5 competitors, runs Review Miner with a larger budget, outputs a product-improvement brief (complaint frequency table, missing features, quote bank for supplier conversations).

### 4.4 `delium watch` — Watchlist Refresh (cron, optional)

For shortlisted ASINs/niches: re-pull Keepa + volumes weekly, recompute scores, append to history, and flag deltas worth attention ("competitor stockout 3 weeks," "review velocity doubled," "price war started") into `reports/watch-<date>.md`. Read-only; it never re-runs frontier analysis unless a delta trips a threshold.

### 4.5 `delium portfolio` — Cross-Candidate View

Ranks everything validated to date by score, capital required, and payback; surfaces the current top-5 with verdict summaries. Pure DB query + render, zero API cost.

---

## 5. The Agents (four, and why not more)

| | **Scout** | **Analyst** | **Review Miner** | **Strategist** |
|---|---|---|---|---|
| Persona | Sourcing-savvy niche hunter | Competitive market analyst | Voice-of-customer researcher | 7-figure seller making a capital allocation call |
| Tier | fast | fast | fast | **frontier (the only one)** |
| Input | Keyword/volume/SERP tables + Keepa quick stats (as compact JSON) | Full competitor dataset + computed demand/price stats | Review sample (~400 texts) + product context | Every prior output + computed economics + scores |
| Output (pydantic) | `niche_clusters[]`, `top_candidates[]{asin, thesis, flags}`, `rejected[]{asin, reason}` | `market_structure`, `price_bands`, `review_moat`, `listing_gaps[]`, `brand_dominance`, `citations[]` | `complaints[]{theme, frequency_pct, severity, quotes[]}`, `praise[]`, `missing_features[]`, `improvement_ideas[]`, `citations[]` | `verdict (buy\|avoid\|watch)`, `conviction (1-5)`, `rationale[]`, `differentiation_plan[]`, `risk_register[]{risk, likelihood, impact, mitigation}`, `verdict_changers[]` |
| Token budget (in/out) | 30k / 3k | 40k / 4k | 60k / 4k | 30k / 5k |
| ~Cost per run | $0.05 | $0.06 | $0.08 | $0.17 |
| Tools | none — data pre-fetched into context | none | none | none |

Deliberate choices:

- **No tool-calling.** The pipeline pre-fetches everything; agents receive compact, normalized JSON in context and return JSON. This removes the entire class of agent-loop failures, makes runs reproducible, and makes budgets exact. (If a stage needs more data, the *pipeline* fetches it, not the agent.)
- **No Orchestrator agent.** The CLI command *is* the orchestration — a fixed pipeline needs no LLM planner. Input parsing (URL→ASIN, keyword detection) is a regex, not a model.
- **Profit and scoring are not agents** (§6, §7). The Strategist receives their outputs and may *question* assumptions ("your $4.50 landed-cost guess looks optimistic for glass") but cannot alter the numbers.
- **The Strategist must argue against itself**: its schema requires `verdict_changers` — the specific facts that would flip the verdict — and at least two entries in `risk_register` even on a `buy`. A buy with no named risks fails validation and retries.

Anti-hallucination, systemwide: agents see only fetched data (no open-ended knowledge questions); every claim needs a `citation_id` referencing a stored data point; numeric fields in agent outputs are cross-checked against source data where possible (a quoted price must exist in the dataset within 1%); one retry with the validation error appended, then the run fails loudly. Missing data degrades explicitly: every report section carries `data_quality: full | partial | missing`, and thin evidence (e.g., 12 reviews retrieved) is stated in the section header, never papered over.

---

## 6. Profit Analysis (deterministic — `analysis/profit.py`)

All assumptions live in `config.toml`, versioned per run so every report is auditable against the assumptions that produced it.

```
Inputs:  market price (Keepa median of top-10), product dimensions/weight (Keepa),
         category → referral fee %, my assumption set (below)

Model:   landed_cost   = est. unit cost (config: default % of price, overridable
                         per-run: --cost 4.20) + freight/unit + duty
         fba_fees      = size-tier fulfillment fee + monthly storage/unit
         referral      = category % × price
         ppc_drag      = assumed TACOS % × price   (config, default conservative 15%)
         returns       = category return-rate % × price
         margin/unit   = price − all of the above
         ROI           = margin ÷ landed_cost
         payback       = launch capital (inventory + PPC ramp) ÷ monthly profit @ 
                         conservative unit estimate (low end of demand range)

Output:  sensitivity table — margin/ROI across price ±15%, cost ±20%, TACOS 10–25% —
         because the honest answer is a surface, not a point.
```

Hard gates (config): min margin 30%, min ROI 100%, max payback 6 months at the low demand estimate. Failing a gate doesn't hide the product — it caps the profitability sub-score and the Strategist must address it explicitly.

---

## 7. Opportunity Score (deterministic — `analysis/scoring.py`)

`score = Σ weightᵢ × sub_scoreᵢ` → 0–100. Weights in config (my risk appetite, not hardcoded):

| Sub-score | Default weight | Computed from |
|---|---|---|
| Demand | 25 | Market size range, volume trend, seasonality penalty |
| Competition | 25 | Review moat (median reviews top-10), seller count, brand dominance, listing quality gaps |
| Differentiation | 20 | Review Miner: complaint frequency × severity × addressability of missing features |
| Profitability | 20 | Margin/ROI/payback vs. gates, sensitivity robustness |
| Risk | 10 (inverted) | Seasonality, fragility, gating, IP signals, price-war evidence, single-keyword dependence |

Each sub-score's formula is documented in the report's methodology section with the input values, so a score of 71 is checkable by hand. The Strategist's verdict may disagree with the score ("scores 74 but avoid — category is one lawsuit-happy brand away from trouble"); disagreement is surfaced, never averaged away. **Score ranks; Strategist decides; I approve.**

---

## 8. `config.toml` (the system's model of me)

```toml
[marketplace]      country = "US"
[capital]          max_launch_budget = 15000        # inventory + PPC ramp
[preferences]      min_price = 18   max_price = 60  # fee-structure sweet spot
                   avoid = ["oversized", "glass", "batteries", "topicals", "gated"]
[assumptions]      default_cogs_pct = 0.25  freight_per_unit = 0.9  duty_pct = 0.05
                   tacos_pct = 0.15  return_rate_default = 0.04
[gates]            min_margin = 0.30  min_roi = 1.0  max_payback_months = 6
[score_weights]    demand = 25  competition = 25  differentiation = 20
                   profitability = 20  risk = 10
[budgets]          max_data_usd_per_validate = 3.0  max_llm_usd_per_validate = 1.0
                   monthly_spend_alarm_usd = 120
```

Scout and Strategist receive `[preferences]` in context — the system learns my constraints from config, not from vector memory.

---

## 9. SQLite Schema (one file, seven tables)

```
runs            id, command, input, status, started_at, finished_at,
                data_cost_usd, llm_cost_usd, config_snapshot (json)
candidates      asin PK, marketplace, niche, source_run_id, triage_score,
                status (new|shortlist|validated|rejected|watching), updated_at
validations     id, run_id, asin, opportunity_score, sub_scores (json),
                verdict, conviction, report_path, created_at
watch_history   asin, captured_at, price, bsr, review_count, est_units_range
cached_products asin PK, payload (json), fetched_at        # TTL 24h
cached_keywords phrase PK, payload (json), fetched_at      # TTL 7d
cached_reviews  asin PK, payload (json), review_count, fetched_at   # TTL 14d
```

`runs.config_snapshot` + cached payloads means any past verdict can be re-derived. `watch_history` accumulates my own time-series from day one — after a few months, trend analysis runs on my data before touching APIs.

---

## 10. Reports (the actual product)

`validate` output structure (markdown, ~3–5 pages):

```
VERDICT BANNER   buy/avoid/watch · conviction 1–5 · opportunity score · one-line thesis
SNAPSHOT         price band, est. market size range, top-10 table
DEMAND           units ranges (methodology: Keepa rank-drops), trend, seasonality
COMPETITION      moat table, brand dominance, listing gaps, who's beatable and why
CUSTOMER PAIN    complaint table (theme · freq% · severity · sample quotes),
                 missing features, improvement brief
UNIT ECONOMICS   full waterfall + sensitivity table + gate results
DIFFERENTIATION  Strategist's plan: the version of this product I would launch
RISKS            register with likelihood/impact/mitigation + verdict_changers
METHODOLOGY      every data source, fetched-at, sample sizes, config snapshot,
                 sub-score formulas with inputs, all citations resolved
```

Numbers are always **ranges with stated method**. The methodology section is mandatory and generated, not written by the LLM.

---

## 11. Operating Cost & Cadence

| Activity | Cadence | Cost |
|---|---|---|
| Keepa subscription | monthly | ~$54 flat |
| `discover` run | 2–3/week | ~$0.10–0.30 each |
| `validate` run | ~5–10/month | ~$1.50–2.50 each |
| `watch` refresh | weekly cron | ~$0.10 |
| **Total** | | **~$75–110/month** |

Spend guards: per-run budget caps from config enforced in `data/cache.py` and `agents/runner.py`; monthly ledger in `runs`; alarm printed (and emailed if configured) past the threshold.

---

## 12. Build Order (~2–3 weeks of evenings)

1. **Week 1:** `data/` adapters + SQLite cache + `analysis/fees.py`+`profit.py` (pure functions, unit-tested against Amazon's published fee tables). Manual smoke test: full data pull for 5 known ASINs, spend spreadsheet.
2. **Week 2:** `validate` pipeline end-to-end — compute stages, then Analyst → Review Miner → Strategist, then the markdown renderer. Calibrate demand estimates against 2–3 products I know the real numbers for.
3. **Week 3:** `discover` + Scout, `pains`, `watch` cron, `portfolio`, golden fixtures, config polish.

Deferred until the tool earns it: HTML dashboard, supplier-sourcing helpers (Alibaba data), PPC keyword planning, multi-marketplace. Each gets added only when a real sourcing decision demands it.
