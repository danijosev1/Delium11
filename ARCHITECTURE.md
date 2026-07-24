# Delium — AI-Native Amazon Product Research Platform

**System Architecture v1.0**

Delium is not "Helium 10 with a chat box." Helium 10 gives sellers 30 tools and makes *them* do the work. Delium inverts the model: the seller states an intent ("find me a product in Home & Kitchen with >40% margin and weak incumbent listings"), and a fleet of AI agents does the research, validates it, and delivers a decision-ready report. The tools are internal; the agents use them.

---

## 1. Product Thesis & Guiding Principles

| Principle | Consequence |
|---|---|
| **Agents do the work, not the user** | The primary UI surface is a mission console + report feed, not 30 tool tabs. |
| **Every answer is auditable** | Agents cite the data they used. Reports link to raw evidence (keyword data, BSR history, review excerpts). |
| **Fast is a feature** | Perceived latency < 100ms for UI, streaming for AI output, aggressive caching of Amazon data. |
| **Data is the moat** | Every agent run enriches a shared, tenant-anonymized market-data warehouse. |
| **Boring infrastructure** | Managed services only (Vercel, Supabase, Upstash). Zero self-hosted servers at launch. |
| **Multi-tenant from day one** | Row-Level Security everywhere. No tenant-unaware code path exists. |

---

## 2. High-Level System Diagram

```
                          ┌──────────────────────────────────────────┐
                          │                 VERCEL                   │
                          │  Next.js App (RSC + Client Components)   │
                          │  /app (UI)   /api (Route Handlers)       │
                          └───────┬──────────────────┬───────────────┘
                                  │                  │
                 ┌────────────────┘                  └───────────────┐
                 ▼                                                   ▼
  ┌────────────────────────────┐                     ┌────────────────────────────┐
  │         SUPABASE           │                     │      UPSTASH REDIS         │
  │  Postgres (RLS, pgvector)  │◄───────────────────►│  Cache · Rate limits ·     │
  │  Auth (JWT)                │                     │  QStash job queue          │
  │  Realtime (agent progress) │                     └─────────────┬──────────────┘
  │  Storage (reports, exports)│                                   │
  │  Edge Functions (webhooks) │                                   ▼
  └────────────┬───────────────┘                     ┌────────────────────────────┐
               │                                     │      AGENT WORKERS         │
               │                                     │  (Vercel Functions,        │
               ▼                                     │   fluid compute / long-run)│
  ┌────────────────────────────┐                     │  Orchestrator + Sub-agents │
  │          STRIPE            │                     └─────────────┬──────────────┘
  │  Subscriptions · Metering  │                                   │
  └────────────────────────────┘                                   ▼
                                                     ┌────────────────────────────┐
                                                     │   EXTERNAL DATA & LLMs     │
                                                     │  Amazon data providers     │
                                                     │  (SP-API, Keepa, Rainforest│
                                                     │   /Oxylabs, DataForSEO)    │
                                                     │  OpenAI · Claude · Gemini  │
                                                     └────────────────────────────┘
```

---

## 3. Folder Structure

Monorepo (Turborepo + pnpm). One deployable web app, shared packages, Supabase as its own workspace.

```
delium/
├── turbo.json
├── pnpm-workspace.yaml
├── .github/workflows/           # CI: typecheck, lint, test, migration-check, deploy
│
├── apps/
│   └── web/                     # Next.js 15 (App Router) — the only deployable
│       ├── app/
│       │   ├── (marketing)/     # Public: landing, pricing, blog — static/ISR
│       │   ├── (auth)/          # sign-in, sign-up, callback, invite acceptance
│       │   ├── (app)/           # Authenticated shell (org-scoped)
│       │   │   ├── dashboard/
│       │   │   ├── missions/            # Agent mission console (create, monitor, review)
│       │   │   │   └── [missionId]/     # Live progress + final report
│       │   │   ├── products/            # Tracked ASINs / watchlists
│       │   │   ├── markets/             # Niche & keyword intelligence views
│       │   │   ├── reports/             # Report library + exports
│       │   │   ├── copilot/             # Free-form AI chat over your data
│       │   │   └── settings/            # org, members, billing, API keys, integrations
│       │   └── api/
│       │       ├── v1/                  # Public REST API (also used by our own UI)
│       │       │   ├── missions/
│       │       │   ├── products/
│       │       │   ├── markets/
│       │       │   └── reports/
│       │       ├── ai/                  # Streaming endpoints (copilot chat, report Q&A)
│       │       ├── jobs/                # QStash-invoked worker endpoints (signed)
│       │       ├── webhooks/            # stripe/, supabase/, providers/
│       │       └── cron/                # Vercel Cron entrypoints (signed)
│       ├── components/          # App-specific components (feature-foldered)
│       ├── lib/                 # App glue: supabase clients, auth helpers, api client
│       └── middleware.ts        # Session refresh, org resolution, rate-limit headers
│
├── packages/
│   ├── ui/                      # shadcn/ui design system + Delium theme + charts
│   ├── db/                      # Drizzle schema (source of truth), typed queries, zod schemas
│   ├── agents/                  # ★ The core IP — framework-agnostic agent runtime
│   │   ├── core/                # AgentRunner, step loop, tool-call protocol, tracing
│   │   ├── orchestrator/        # Mission planner: decomposes intent → agent DAG
│   │   ├── registry/            # Agent definitions (see §7)
│   │   ├── tools/               # Typed tools agents can call (data fetchers, calculators)
│   │   ├── llm/                 # Model router: OpenAI/Claude/Gemini behind one interface
│   │   ├── memory/              # pgvector retrieval, mission scratchpad, org knowledge
│   │   └── eval/                # Golden datasets, regression evals for agent quality
│   ├── amazon-data/             # Provider abstraction: Keepa/Rainforest/SP-API/DataForSEO
│   │   ├── providers/           # One adapter per vendor, normalized output types
│   │   ├── cache.ts             # Read-through Redis + Postgres warm store
│   │   └── quotas.ts            # Per-provider budget & failover logic
│   ├── billing/                 # Stripe: plans, entitlements, usage metering, webhooks logic
│   ├── jobs/                    # Queue contracts: job names, payload schemas, enqueue helpers
│   └── config/                  # eslint, tsconfig, tailwind presets, env validation (zod)
│
├── supabase/
│   ├── migrations/              # SQL migrations (generated from Drizzle, reviewed by hand)
│   ├── functions/               # Edge Functions: light webhook receivers, auth hooks
│   └── seed.sql
│
└── docs/                        # ADRs, runbooks, this document
```

**Key decisions**

- **One deployable.** Agent workers are Vercel Functions with fluid compute / extended duration, not a separate service. When agent workloads outgrow serverless (see §10), `packages/agents` lifts out unchanged into a container worker — that's why it is framework-agnostic and imports nothing from Next.js.
- **Drizzle as schema source of truth**, compiled to SQL migrations applied via Supabase CLI. Full type-safety from DB → API → UI with zod at every boundary.
- **Edge Functions used narrowly**: webhook receipt/verification and Supabase Auth hooks (things that must live near the DB or be independent of Vercel deploys). Heavy logic stays in Vercel functions where the TypeScript monorepo tooling is first-class.

---

## 4. Database Design (PostgreSQL / Supabase)

Three logical schemas:

- **`app`** — tenant data, RLS-protected, per-org.
- **`market`** — shared Amazon market data warehouse. **Not tenant-scoped** — this is the cache/moat. No RLS; accessed only via service role from server code.
- **`ops`** — jobs, agent traces, usage metering, audit.

### 4.1 Tenancy & identity (`app`)

```
organizations      id, name, slug, plan, stripe_customer_id, settings jsonb, created_at
org_members        org_id, user_id, role (owner|admin|member|viewer), invited_by, created_at
users              (Supabase auth.users; mirrored profile row: users_profiles)
api_keys           id, org_id, name, hashed_key, scopes[], last_used_at, expires_at
audit_log          id, org_id, actor_id, action, target, metadata jsonb, created_at
```

Every tenant table carries `org_id uuid not null references organizations`. RLS policy pattern (single, consistent):

```sql
USING (org_id IN (SELECT org_id FROM org_members WHERE user_id = auth.uid()))
```

Role-gated writes via a `has_org_role(org_id, 'admin')` security-definer function. The active org travels in the JWT (`app_metadata.org_id`, set by an auth hook) so policies can use `auth.jwt()->>'org_id'` on hot paths without a join.

### 4.2 Missions & agents (`app` + `ops`)

```
missions           id, org_id, created_by, type (niche_discovery|product_validation|
                   competitor_teardown|listing_audit|keyword_map|custom),
                   intent text, params jsonb, status (queued|planning|running|
                   needs_review|complete|failed|cancelled), plan jsonb,
                   result_report_id, cost_credits int, created_at, completed_at
mission_steps      id, mission_id, agent_name, status, input jsonb, output jsonb,
                   depends_on uuid[], started_at, finished_at, tokens_in, tokens_out,
                   model, cost_usd numeric
reports            id, org_id, mission_id, title, summary text, body jsonb (structured
                   blocks), verdict (pursue|caution|avoid|n/a), confidence numeric,
                   citations jsonb, created_at
watchlists         id, org_id, name; watchlist_items: watchlist_id, asin, notes
alerts             id, org_id, rule jsonb (e.g. BSR drop, price change, new competitor),
                   channel (email|slack|in_app), last_fired_at
org_memory         id, org_id, kind (preference|fact|decision), content text,
                   embedding vector(1536), source_mission_id   -- pgvector, agent long-term memory
```

### 4.3 Market data warehouse (`market`) — the shared moat

```
products           asin PK, marketplace, title, brand, category_path, images jsonb,
                   attributes jsonb, first_seen_at, last_refreshed_at
product_snapshots  asin, captured_at, price, bsr, rating, review_count, buybox_seller,
                   est_monthly_units, est_monthly_revenue   -- append-only time series
keywords           id, marketplace, phrase, search_volume, volume_trend jsonb,
                   cpc_estimate, last_refreshed_at
keyword_rankings   keyword_id, asin, position, captured_at   -- append-only
niches             id, definition jsonb, opportunity_score, computed_at
reviews_digest     asin, period, themes jsonb (LLM-extracted complaints/praise),
                   sample_quotes jsonb, embedding vector(1536)
provider_calls     provider, endpoint, cost_units, cached bool, called_at  -- spend telemetry
```

- Time-series tables are append-only, partitioned by month (`pg_partman`), old partitions rolled up into daily aggregates.
- `last_refreshed_at` + per-entity TTL drives the read-through cache (§8): a request for an ASIN first hits Redis, then `market.products`, then a provider — and the provider response backfills both.
- **This is the moat**: every mission run by any tenant warms the warehouse; marginal data cost per mission falls as the customer base grows.

### 4.4 Billing & metering (`app` + `ops`)

```
subscriptions      org_id, stripe_subscription_id, plan (starter|pro|scale), status,
                   current_period_end, seats
entitlements       plan, feature, limit_value       -- static table: missions/mo, seats,
                                                    -- tracked ASINs, API rate, model tier
usage_events       id, org_id, kind (mission_run|ai_tokens|tracked_asin|api_call),
                   quantity, metadata jsonb, created_at   -- append-only
usage_rollups      org_id, period, kind, total      -- materialized hourly for fast checks
```

Credits model: each mission type has a credit price; plans include monthly credits; overage is metered to Stripe (usage-based line item). Entitlement checks read `usage_rollups` (fast) with a Redis counter as the hot-path guard.

---

## 5. Authentication & Authorization

- **Supabase Auth**: email magic link + Google OAuth at launch. JWTs verified in Next.js middleware via `@supabase/ssr` (cookie-based sessions, auto-refresh).
- **Org model**: users belong to N orgs; active org stored in JWT app_metadata (switched via a re-mint endpoint). All server code resolves `{ userId, orgId, role }` from one `getSession()` helper — no ad-hoc auth parsing anywhere.
- **Three access planes:**
  1. **Browser → RSC/Route Handlers**: cookie session, RLS enforced (anon-key Supabase client bound to the user JWT).
  2. **Public API (`/api/v1`)**: `Authorization: Bearer <api_key>` — hashed lookup in `api_keys`, scoped (read, write, missions:run), rate-limited per key.
  3. **Internal (workers, webhooks, cron)**: service-role client, but *every* internal function takes an explicit `orgId` parameter and uses query helpers from `packages/db` that require it — service role never means "tenant-blind."
- **RLS is the last line of defense, not the only one**: application-layer authorization (role checks, entitlement checks) runs first; RLS catches bugs.
- **Auth hooks (Supabase Edge Function)**: on signup → create org, seed trial entitlements, stamp org_id claim; on invite acceptance → add membership, restamp claim.
- MFA (TOTP) and SAML/SSO deferred to the Team/Enterprise tier (roadmap).

---

## 6. API Architecture

### 6.1 Surfaces

| Surface | Transport | Consumers | Notes |
|---|---|---|---|
| `/api/v1/*` | REST + JSON, OpenAPI-documented | Our UI **and** customers | Versioned, stable. Zod-validated in/out; spec generated from the same zod schemas. |
| `/api/ai/*` | POST + SSE streaming | UI only | Copilot chat, report Q&A. Vercel AI SDK streaming protocol. |
| Supabase Realtime | WebSocket | UI only | Mission/step status changes (`postgres_changes` on `missions`, `mission_steps`) → live agent progress UI. No polling. |
| `/api/jobs/*` | POST, QStash-signature verified | QStash only | Worker entrypoints. Idempotent by `job_id`. |
| `/api/webhooks/*` | POST, signature verified | Stripe, providers | Verify → persist event → enqueue → 200 fast. |
| `/api/cron/*` | GET, `CRON_SECRET` | Vercel Cron | Thin: enqueue work, never do it inline. |

### 6.2 Conventions

- **Dogfooding**: the UI consumes `/api/v1` through a typed client (generated from the OpenAPI spec). If the public API can't do it, our UI can't either — keeps the API honest and complete.
- Errors: RFC 7807 problem+json; every response carries `x-request-id` (propagated into logs and agent traces).
- Rate limiting: Upstash sliding window — per-user for the UI, per-key for the API, per-org for missions. Limits are entitlement-driven.
- Pagination: cursor-based everywhere (`created_at, id` keyset).
- Idempotency: mutating endpoints accept `Idempotency-Key`; job handlers dedupe on `job_id` stored in `ops.jobs`.

### 6.3 Example: mission lifecycle

```
POST /api/v1/missions          { type: "product_validation", intent: "...", params: {...} }
  → entitlement check (credits, concurrency) → insert mission (status=queued)
  → enqueue plan job → 202 { mission_id }

QStash → POST /api/jobs/mission.plan     → Orchestrator plans DAG, writes mission_steps
QStash → POST /api/jobs/mission.step     → one invocation per ready step (fan-out)
                                            each step streams trace + status to DB
UI     ← Supabase Realtime               → live step progress, token-level streaming
                                            for the writer step via /api/ai
GET  /api/v1/reports/{id}                → final structured report + citations
```

---

## 7. Agent Architecture (the core)

### 7.1 Design stance

- **Orchestrator–worker DAG, not free-form autonomy.** A planner produces an explicit, inspectable plan (a DAG of typed steps). Users can see, and later edit, the plan. Predictable cost, parallelism, and retries fall out of this.
- **Agents are data, not code**: each agent is a declarative definition (system prompt, tool allowlist, model tier, output zod schema, budget) in `packages/agents/registry`. Adding an agent = adding a definition + evals, not new plumbing.
- **Structured outputs only.** Every agent emits schema-validated JSON. Prose exists only inside the final report writer.
- **Every step is resumable**: steps are idempotent QStash jobs keyed by `mission_step.id`; a crashed run resumes from the last completed step.

### 7.2 The fleet

| Agent | Role | Typical model tier |
|---|---|---|
| **Orchestrator** | Parse intent → choose mission template → emit step DAG with budgets | Frontier (Claude) |
| **Market Scout** | Expand seed niche/keyword → candidate ASIN set via keyword & category tools | Mid (Gemini Flash / GPT-mini) |
| **Demand Analyst** | Volume, seasonality, trend; estimates units/revenue from snapshots | Mid |
| **Competition Analyst** | Review moats, listing quality of incumbents, brand dominance, ad intensity | Mid |
| **Review Miner** | LLM extraction over reviews → complaint/praise themes = differentiation angles | Mid, high volume |
| **Margin Modeler** | Landed cost, FBA fees, referral fees, breakeven ACOS; sensitivity table | Deterministic tools + small model |
| **Risk Auditor** | IP/gating/seasonality/fragility/compliance red flags | Frontier |
| **Critic** | Adversarial pass: challenges other agents' conclusions, flags weak evidence, forces re-runs | Frontier (different vendor than the analysts — cross-model checking) |
| **Report Writer** | Composes the final structured report with citations and a verdict + confidence | Frontier |
| **Copilot** | Interactive chat over org data + warehouse; can launch missions | Frontier, streaming |

### 7.3 Runtime (`packages/agents/core`)

- **Step loop**: prompt → model → (tool calls)* → validated output. Tools are zod-typed functions (data fetchers from `packages/amazon-data`, calculators, DB queries). The loop enforces per-step budgets (max tokens, max tool calls, wall clock) — hard caps, since costs are metered to tenants.
- **Model router (`packages/agents/llm`)**: one `complete()/stream()` interface over OpenAI, Anthropic, Gemini. Routing policy per agent = ordered preference list + automatic failover on 5xx/timeouts + tier downgrade under load. Vendor choice is config, never inline code.
- **Memory**: (a) mission scratchpad — shared jsonb blackboard on the mission row that downstream steps read; (b) org memory — pgvector store of preferences/decisions ("we avoid oversized items") injected into planner context; (c) the `market` warehouse itself is the fleet's shared world-model.
- **Tracing & evals**: every step logs prompt hash, tool calls, tokens, cost, latency to `ops` tables (viewable in an internal admin). `packages/agents/eval` holds golden missions replayed in CI against recorded tool outputs — agent quality regressions fail the build like test regressions.
- **Human-in-the-loop**: missions can pause at `needs_review` checkpoints (e.g. "approve spending 40 more credits to deep-scan 200 ASINs"). Realtime pushes the prompt to the UI; the answer resumes the DAG.

### 7.4 Cost control

Per-mission credit budget → allocated across steps by the planner → enforced by the runner. Token spend recorded per step feeds both tenant metering (`usage_events`) and internal unit economics (cost per mission type per model, watched weekly).

---

## 8. Caching Strategy

Layered, with explicit TTLs per data class:

| Layer | What | TTL / policy |
|---|---|---|
| **CDN (Vercel)** | Marketing pages, OG images, public docs | ISR, hours–days |
| **RSC / `unstable_cache`** | Dashboard aggregates, entitlement lookups | 30–300s, tag-invalidated on writes |
| **Redis (Upstash)** | Hot Amazon data (ASIN summaries, keyword volumes), session-ish state, rate limiters, per-org usage counters, LLM response cache (hash of prompt+model for deterministic tool-ish calls) | ASIN: 6–24h · keywords: 24h · counters: rolling |
| **Postgres `market` schema** | Durable warm store for all provider data | Per-entity `last_refreshed_at` + class TTL (price/BSR: 24h; catalog attributes: 7d; reviews digest: 7d) |
| **Provider** | Cache miss only | Budget-guarded (`quotas.ts`), cheapest adequate provider first |

Read-through rule in one place (`packages/amazon-data/cache.ts`): **Redis → Postgres → provider**, each miss backfills the layers above. Agents never call providers directly — only through this module — so cache discipline and spend caps are structurally guaranteed.

Invalidation: writes to tenant data bust RSC tags; market data is time-based only (it's telemetry, not truth); "force refresh" is a paid, entitlement-gated action.

---

## 9. Background Jobs & Scheduling

- **Queue: Upstash QStash** → HTTPS delivery to `/api/jobs/*` with signature verification, automatic retries with backoff, DLQ. Fits the serverless model (no long-lived consumers to host).
- **Job catalog** (contracts in `packages/jobs`, payloads zod-validated):
  - `mission.plan`, `mission.step`, `mission.finalize`
  - `data.refresh_asin`, `data.refresh_keyword` (fan-out batches)
  - `alerts.evaluate` (rule engine over fresh snapshots)
  - `billing.sync_usage` (push metered usage to Stripe)
  - `warehouse.rollup`, `warehouse.partition_maintenance`
- **Cron (Vercel Cron → enqueue only):**
  - hourly: tracked-ASIN refresh scheduler (spread across the hour by org hash), usage rollups
  - daily: keyword volume refresh, alert digests, provider spend report
  - weekly: niche opportunity re-scoring, partition maintenance, eval-suite run
- **Idempotency & poison handling**: every handler checks/records `job_id` in `ops.jobs`; max 5 retries then DLQ + Slack alert; mission steps mark the mission `failed` with a user-readable reason rather than silently dying.

---

## 10. Scaling Plan

**Phase 1 (0 → ~1k orgs): ship on managed serverless.** Everything above. Bottlenecks will be provider quotas and LLM rate limits, not our infra. Mitigations already built in: caching, model failover, per-org concurrency caps.

**Phase 2 (~1k → 10k orgs):**
- Postgres: move heavy read paths (dashboards, niche scoring) to Supabase read replicas; `market` time-series partitions aggressively pruned/rolled up. Consider a separate Postgres cluster for `market` (it has no RLS and different scaling shape than tenant data).
- Agent workers: lift `packages/agents` runner into containerized workers (Fly/Railway/ECS) consuming the same QStash topics — the package boundary makes this a deployment change, not a rewrite. Long missions stop being constrained by function duration.
- Redis: split cache vs. rate-limit vs. queue into separate Upstash databases.

**Phase 3 (10k+):**
- `market` warehouse → dedicated analytics store (ClickHouse) for time-series; Postgres keeps serving row lookups.
- Embedding/RAG store evaluated for dedicated vector infra if pgvector p95 degrades.
- Multi-region read path (Vercel is already edge; DB read replicas per region).

**Always-on guardrails**: per-org mission concurrency limits, global LLM token budgets with queuing (missions degrade to "queued" rather than 429s), provider spend circuit breakers.

---

## 11. Deployment & Environments

- **Environments**: `production`, `staging` (own Supabase project + Stripe test mode + capped LLM budgets), ephemeral **preview** per PR (Vercel preview + Supabase branch DB, seeded).
- **CI (GitHub Actions)**: typecheck → lint → unit tests → migration dry-run against a shadow DB → agent eval smoke suite (recorded tool outputs, cheap models) → deploy.
- **Migrations**: applied by CI via Supabase CLI *before* the Vercel promote; expand-migrate-contract pattern for breaking changes (old + new code must both run during rollout).
- **Config**: all env vars zod-validated at boot (`packages/config`); a missing secret fails the build, not the request.
- **Observability**: Sentry (errors, both client and server), Axiom or Vercel OTel drain (structured logs keyed by `request_id`/`mission_id`), agent traces in `ops` with an internal viewer. Four golden alerts: job DLQ depth, mission failure rate, provider spend velocity, LLM error rate per vendor.
- **Rollback**: Vercel instant rollback for app code; migrations designed to be forward-safe so app rollback never requires DB rollback.

---

## 12. Security & Compliance Notes

- RLS on every tenant table; service-role usage confined to `packages/db` helpers that require explicit `orgId`.
- API keys stored hashed (SHA-256 + per-key salt), shown once.
- Webhooks: signature verification + event persistence before processing (replayable).
- PII footprint deliberately tiny (email, name). Market data contains no PII.
- Prompt-injection posture: review text and listing copy fed to agents is untrusted — agents that consume it have no write-tools; the Critic checks for instruction-following anomalies; reports render as structured blocks, never raw model HTML.
- SOC 2 groundwork from day one: audit_log, access reviews, single-provider blast radius (Supabase) — certification itself is a roadmap item.

---

## 13. Roadmap

**v1 (launch, ~3 months)** — Missions: niche discovery, product validation, listing audit. Copilot chat. Watchlists + alerts. Stripe plans + credits. Public read API.

**v1.5** — Competitor teardown & keyword-map missions. Editable mission plans (user tweaks the DAG before run). Slack alerts. CSV/PDF report exports. Team roles polish.

**v2** — **Continuous autonomous mode**: standing agents that watch your niches and proactively file opportunity reports ("a competitor's rating just collapsed — here's the opening"). Seller account integration (SP-API OAuth) → your real sales data joins the analysis. Supplier-sourcing agent (Alibaba data). PPC audit agent.

**v3** — Marketplace expansion (EU/JP marketplaces). Portfolio strategist agent (allocates your capital across opportunities). API + webhooks for agencies; white-label reports. Enterprise: SSO/SAML, SOC 2 report, custom data residency.

**Deliberate non-goals for now**: Chrome extension parity with Helium 10, PPC bid management (execution, not research), repricing, inventory management. We win by owning the *decision* layer, not by rebuilding the operations layer.

---

*Maintained in `docs/` via ADRs; material changes to this document require an ADR.*
