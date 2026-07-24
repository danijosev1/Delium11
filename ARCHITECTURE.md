# Delium — V1 Architecture

**AI Amazon Product Validation Report · MVP spec**

One product, one mission type, one deliverable: a seller pastes an Amazon URL, ASIN, or keyword, and gets back a decision-ready validation report — opportunity score, demand, competition, review insights, profit estimate, risks, and a verdict. Built and maintained by one person. Target: **paying customers in 4–6 weeks.**

Everything in this document exists to answer two questions as cheaply as possible:

1. Can we produce a report a seller trusts for under ~$3 in data + AI cost?
2. Will a seller pay a monthly subscription for it?

Anything that doesn't serve those questions is out of scope (see §12).

---

## 1. System Overview

```
┌─────────────────────────────────────────────┐
│              VERCEL (Next.js app)           │
│  UI (App Router, RSC)  ·  Route handlers    │
│  Inngest functions (agent workflow)         │
└──────────┬───────────────────┬──────────────┘
           │                   │
           ▼                   ▼
┌────────────────────┐   ┌──────────────────────────────┐
│      SUPABASE      │   │          EXTERNAL            │
│  Postgres + RLS    │   │  Amazon data provider (one)  │
│  Auth (magic link, │   │  LLM provider (one, 2 tiers) │
│  Google OAuth)     │   │  Stripe Checkout + webhooks  │
└────────────────────┘   └──────────────────────────────┘
```

- **One deployable**: a single Next.js app on Vercel. No monorepo, no packages, no separate workers.
- **Inngest** runs the multi-step agent workflow durably (retries, step memoization, concurrency limits) — no queue infrastructure to own. Inngest functions are served from a route handler inside the same app.
- **Supabase** is the entire backend: Postgres, Auth, RLS, file storage if needed later.
- **No Redis.** Caching lives in Postgres (§8). Add Redis only if a measured problem demands it.

Monthly infra at zero scale: Vercel Hobby/Pro + Supabase Pro + Inngest free tier ≈ **$45–70/mo**, before data/AI usage.

---

## 2. Repository Structure

Single Next.js app. One deliberate boundary: `src/agents/` imports nothing from Next.js, so it can lift into a dedicated worker later without a rewrite. Everything else is ordinary app code.

```
delium/
├── src/
│   ├── app/
│   │   ├── (marketing)/          # Landing + pricing (static)
│   │   ├── (auth)/               # sign-in, callback
│   │   ├── (app)/
│   │   │   ├── dashboard/        # Mission list + "New validation" input
│   │   │   ├── missions/[id]/    # Progress view → final report
│   │   │   └── settings/         # Plan, billing portal link, account
│   │   └── api/
│   │       ├── inngest/          # Inngest serve endpoint (all agent workflow)
│   │       └── webhooks/stripe/  # Checkout + subscription lifecycle
│   ├── agents/                   # ★ Framework-agnostic (no Next.js imports)
│   │   ├── run.ts                # Step runner: prompt → LLM → tools → zod-validated JSON
│   │   ├── llm.ts                # One provider, two tiers: fast | frontier
│   │   ├── orchestrator.ts       # Input parsing + scope decisions
│   │   ├── product-analyst.ts    # Demand + competition + profit (one combined pass)
│   │   ├── review-miner.ts       # Review theme extraction
│   │   ├── report-writer.ts      # Final report composition + verdict
│   │   ├── tools/                # fetchProduct, fetchKeyword, fetchReviews, feeCalculator
│   │   └── prompts/              # Versioned prompt files
│   ├── data/                     # Amazon data access
│   │   ├── provider.ts           # Single provider adapter, normalized types
│   │   └── cache.ts              # Read-through: Postgres cache → provider (§8)
│   ├── lib/                      # supabase clients, auth helper, stripe, env (zod-validated)
│   └── components/               # shadcn/ui + report blocks + progress UI
├── supabase/migrations/          # Plain SQL migrations (Supabase CLI)
├── evals/golden/                 # ~10 saved missions + expected verdicts (manual re-run)
└── docs/decisions.md             # Running ADR log, one file
```

---

## 3. The Product Flow

```
User input (URL | ASIN | keyword)
   → POST server action: create mission (status=queued), check monthly limit
   → inngest.send("mission/requested")

Inngest workflow "run-mission":
   step 1  Orchestrator   parse input → resolve target ASIN(s) + keyword set
                          (URL→ASIN extraction, keyword→top-N ASINs via provider)
   step 2  Data fetch     product data, keyword volumes, top-competitor set,
                          review sample — via data/cache.ts (parallel steps)
   step 3  Product Analyst demand + competition + profit model → structured JSON
   step 4  Review Miner   complaint/praise themes + differentiation angles → JSON
   step 5  Report Writer  compose report blocks, opportunity score (0–100),
                          verdict (pursue | caution | avoid) + confidence
   step 6  Persist report, mission status=complete

UI polls mission status every 2s while running (simple SWR refresh — no
websockets in V1). Median mission target: < 3 minutes.
```

Failure handling is Inngest's: automatic per-step retries, then the workflow marks the mission `failed` with a user-readable reason and **does not count against the monthly limit**.

---

## 4. Database (Postgres, V1 tables only)

All tenant tables carry `user_id` and RLS. One user = one account. No orgs, no teams, no roles.

```sql
-- Tenant data (RLS: user_id = auth.uid())
profiles          user_id PK → auth.users, email, created_at
subscriptions     user_id PK, stripe_customer_id, stripe_subscription_id,
                  plan ('starter'|'pro'), status, current_period_end,
                  missions_used_this_period int, period_started_at
missions          id, user_id, input_type ('url'|'asin'|'keyword'), input_raw,
                  resolved_asin, status ('queued'|'running'|'complete'|'failed'),
                  failure_reason, created_at, completed_at
reports           id, user_id, mission_id UNIQUE, title,
                  opportunity_score int, verdict, confidence numeric,
                  body jsonb,          -- structured blocks (see §7)
                  citations jsonb,     -- data points each claim rests on
                  created_at

-- Shared cache, NOT tenant data (no RLS; server-role access only)
cached_products   asin PK, marketplace, payload jsonb, fetched_at
cached_keywords   phrase PK, marketplace, payload jsonb, fetched_at
cached_reviews    asin PK, marketplace, payload jsonb, fetched_at

-- Ops
mission_steps     id, mission_id, name, status, model, tokens_in, tokens_out,
                  cost_usd numeric, started_at, finished_at
                  -- cost visibility per mission; this is how we watch unit economics
```

Notes:

- `missions_used_this_period` is the entire billing meter: incremented in the same transaction that marks a mission `complete`; reset by the Stripe `invoice.paid` webhook at period rollover. No usage_events, no rollups, no credits.
- `mission_steps.cost_usd` is non-negotiable even in V1 — **cost-per-mission is the metric the business lives or dies on**, and it must be queryable from day one (`select avg(sum) ... group by mission`).
- Cache tables are plain rows with `fetched_at` TTLs. No partitions, no warehouse, no time series. Historical snapshots come later if a feature needs them.

---

## 5. Authentication

- **Supabase Auth**: magic link + Google OAuth. Cookie sessions via `@supabase/ssr`, refreshed in middleware.
- One helper — `getUser()` — used by every server action and route handler. No other auth code path exists.
- **RLS on every tenant table** with the single policy pattern `user_id = auth.uid()`. Cache/ops tables are service-role only and contain no PII.
- Authorization is trivial by construction: a user sees their own missions and reports, full stop. No roles, no invites, no API keys.

---

## 6. AI Layer

**One LLM provider. Two tiers.** (Recommendation: Anthropic — `claude-haiku-4-5` as **fast**, `claude-sonnet-5` as **frontier**. The choice is one line in `llm.ts`; what's structural is that there is exactly one vendor and two named tiers.)

| Agent | Tier | Job | Output |
|---|---|---|---|
| **Orchestrator** | fast | Parse input, resolve ASIN/keywords, decide fetch scope | scope JSON |
| **Product Analyst** | fast | Demand, competition, and profit in one pass over fetched data; fee math done by a deterministic calculator tool, not the model | analysis JSON |
| **Review Miner** | fast | Extract complaint/praise themes + differentiation angles from a capped review sample (~100 reviews) | themes JSON |
| **Report Writer** | frontier | Compose the report, score the opportunity, issue verdict + confidence, cite the data behind each claim | report blocks JSON |

Rules that keep cost and quality under control:

- **Structured output everywhere**: every agent's output is zod-validated JSON; a validation failure retries once with the error appended, then fails the step.
- **Hard budgets per step**: max tokens and max tool calls enforced in `run.ts`. A mission has a total cost ceiling; exceeding it fails loudly rather than silently overspending.
- **Only the Report Writer uses the frontier tier.** Target LLM cost: **< $0.30/mission**; alarm if the 7-day average exceeds $0.50.
- **Untrusted-input rule**: review text and listing copy are untrusted. Agents that read them (Product Analyst, Review Miner) have read-only tools and their outputs are data, not instructions. Reports render as typed blocks — model output is never rendered as raw HTML.
- **Profit estimates are deterministic**: FBA/referral fee calculation is a plain function with published fee tables. The model interprets; it never arithmetics.

**Evals**: `evals/golden/` holds ~10 recorded missions (frozen tool outputs + expected verdict/score range). Re-run manually before any prompt or model change. No framework, no CI gate — a script and a diff.

---

## 7. The Report (the actual product)

`reports.body` is an ordered list of typed blocks the UI renders natively:

```
verdict_banner    { verdict, opportunity_score, confidence, one_line_rationale }
demand            { est_monthly_units_range, search_volume_summary, trend, seasonality_note }
competition       { top_competitors[], review_moat_assessment, listing_quality_gaps }
review_insights   { complaint_themes[], praise_themes[], differentiation_angles[] }
profit            { price, est_landed_cost_range, fba_fees, referral_fee,
                    margin_range, breakeven_acos }
risks             { flags[] : ip | gated | seasonal | fragile | saturated | compliance }
methodology       { data_sources, fetched_at timestamps, caveats }
```

- **Estimates are ranges with stated caveats, never false precision.** The `methodology` block is mandatory — trust is the product, and Helium 10 refugees will stress-test the numbers.
- Every quantitative claim carries a citation id resolving into `reports.citations` (the raw data point + when it was fetched).
- Export: print-styled page → browser PDF. No PDF pipeline in V1.

---

## 8. Amazon Data & Caching

**This is the existential dependency — treat it as week-1 work, before UI.**

- **One provider** behind `data/provider.ts` with normalized types. Selection criteria, in order: (1) has search-volume + sales-estimate signals, not just page scrapes, (2) per-call price at our volumes, (3) rate limits compatible with a 3-minute mission. Evaluate Keepa + one of Rainforest/DataForSEO in week 1 **with a spreadsheet of real per-mission cost** before committing. SP-API is not part of V1 (not accessible, and doesn't carry research data).
- **Read-through cache in Postgres** (`data/cache.ts`): check `cached_*` row and `fetched_at` TTL → hit returns instantly, miss calls the provider and upserts. TTLs: product data 24h, keyword volumes 7d, reviews 7d. Popular ASINs get cheap fast; cache hit rate is a dashboard number from day one.
- **Sales estimates**: V1 uses the provider's estimates, relabeled as ranges with our caveats. We do not build our own BSR→units model yet — but every fetched data point lands in the cache tables, so the raw material accumulates for a proper model later (§13).
- **Spend guards**: per-mission provider-call cap (Orchestrator sets scope; runner enforces), plus a monthly provider budget env var — at 80% an email alarm, at 100% new missions queue with an honest status message instead of silently failing.
- Target data cost: **< $1.50/mission uncached**, falling with cache hit rate.

---

## 9. Payments & Billing

- **Stripe Checkout** (hosted page) + **Stripe Customer Portal** (self-serve cancel/upgrade). We build no billing UI beyond two buttons.
- **Two plans**: Starter (~$29/mo, 10 missions) · Pro (~$79/mo, 40 missions). Prices are hypotheses; the mechanism is fixed — flat monthly, hard mission limits, no overage, no credits, no metering.
  - Sanity check: at ~$1.80 COGS/mission fully uncached, worst-case Pro gross margin ≈ 9% — real margin depends on cache hit rate and typical usage well under the cap. Watch `mission_steps.cost_usd` weekly and reprice/re-limit as facts arrive.
- **Free trial**: 2 missions on signup, no card. The report is the demo.
- **Enforcement**: mission creation checks `status='active'` and `missions_used_this_period < plan limit` → hard stop with upgrade prompt.
- **Webhooks** (`/api/webhooks/stripe`): verify signature → persist raw event → apply. Handles `checkout.session.completed`, `invoice.paid` (reset counter), `customer.subscription.updated|deleted`. Idempotent by event id.

---

## 10. Deployment, Environments, Observability

- **Two environments.** Production and one staging (separate Supabase project, Stripe test mode, capped budgets). Vercel preview deploys point at staging. No per-PR databases.
- **CI (GitHub Actions)**: typecheck → lint → unit tests (fee calculator, zod schemas, webhook handlers) → deploy. Migrations applied via Supabase CLI before promote; expand-then-contract for breaking changes.
- **Config**: all env vars zod-validated at boot; missing secret fails the build.
- **Observability, minimal but real**: Sentry (client + server) · Inngest dashboard (workflow runs, retries, failures — free) · `mission_steps` as the cost ledger. **Three alarms only**: mission failure rate > 10% (daily), avg mission cost > threshold (daily), provider monthly spend > 80% budget.
- **Rollback**: Vercel instant rollback; migrations forward-safe so app rollback never needs a DB rollback.

---

## 11. Security (V1 scope)

- RLS on every tenant table; service-role confined to `data/` and Inngest functions.
- Stripe webhook signature verification; raw events persisted before processing.
- PII footprint: email only. Cache tables hold public Amazon data, no PII.
- Untrusted-content rule from §6: scraped text never gains tool access or renders as HTML.
- Secrets in Vercel/Supabase env only; zod-validated. No secrets in the repo, ever.
- Deferred deliberately: SOC 2, audit log, MFA, SSO — none blocks a first paying customer.

---

## 12. Explicitly Out of Scope for V1

Cut, with the trigger that brings each back:

| Cut | Comes back when |
|---|---|
| Public API + API keys | An agency asks and offers money |
| Multi-org, teams, roles, invites | A customer asks to add a teammate (≈1 week to add on top of `user_id` scoping) |
| Credits, metering, overage billing | Hard limits demonstrably leave money on the table |
| Multi-LLM routing / second vendor | Primary vendor reliability or cost forces it (one-line tier swap meanwhile) |
| Critic agent, editable plans, HITL checkpoints | Report-quality complaints point at reasoning errors |
| Data warehouse, partitions, time series | A feature needs history (trend charts, alerts) |
| Vector memory / org knowledge | Repeat usage shows personalization demand |
| Redis, QStash, websockets/Realtime | A measured latency or cost problem Postgres+polling can't solve |
| Alerts, watchlists, Slack, niche discovery, listing audit | V1 mission retains paying users |
| Eval framework in CI | Prompt regressions actually bite a customer |

---

## 13. Roadmap After First Revenue

1. **v1.1** — second mission type (niche discovery: keyword → ranked opportunity list), report sharing links, trend history on cached data.
2. **v1.2** — watchlists + weekly re-validation email ("your niche moved"), teams.
3. **v2** — continuous monitoring agents (the original vision's headline), our own BSR→units estimation model trained on accumulated cache data, public API.

Each stage is funded by the previous one working. The V1 bet stays narrow on purpose: **one input box, one report, one price — shipped in weeks, instrumented to prove or kill the unit economics fast.**

---

## 14. Build Plan (4–6 weeks)

| Week | Deliverable |
|---|---|
| 1 | Provider bake-off with real cost spreadsheet · data adapter + Postgres cache · fee calculator |
| 2 | Agent pipeline end-to-end in Inngest (CLI-triggered, no UI) · first full report JSON · golden fixtures started |
| 3 | Auth + app shell + mission input + progress + report renderer |
| 4 | Stripe Checkout + webhooks + limits · staging env · Sentry + alarms |
| 5 | Prompt tuning against goldens · landing page + pricing · polish |
| 6 | Buffer · soft launch to 10–20 sellers · watch cost ledger and verdict quality |

*Changes to this document go in `docs/decisions.md` — one paragraph per decision, dated.*
