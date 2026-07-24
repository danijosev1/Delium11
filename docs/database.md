# Delium — V1 Database Design (Supabase PostgreSQL)

Scope: exactly the nine concerns in the V1 spec — auth/profile, subscriptions, missions, agent execution tracking, reports, three cache stores, and cost tracking. Nine tables total. No orgs, no teams, no roles, no credits, no warehouse.

Design rules applied throughout:

- **UUID PKs** (`gen_random_uuid()`), `created_at`/`updated_at` on every table, one shared trigger for `updated_at`.
- **Status/enum-ish columns are `text` + `CHECK`**, not Postgres `ENUM` types — same integrity, painless to evolve during a 6-week MVP.
- **JSONB only where the shape is genuinely fluid**: report bodies, citations, cached provider payloads, step I/O. Everything queryable/aggregatable (costs, scores, statuses, tokens) is a typed column.
- **Two access planes**: user-facing tables have RLS with the single pattern `user_id = auth.uid()`; operational/cache tables have RLS **enabled with no policies** — a deliberate deny-all that makes them service-role-only by construction.

---

## 1. ERD

```
auth.users (Supabase-managed)
    │ 1:1                                   ┌────────────────────────────┐
    ▼                                       │  SERVICE-ROLE-ONLY PLANE   │
profiles ──1:1── subscriptions              │                            │
    │                                       │  stripe_events             │
    │ 1:N                                   │  cached_products           │
    ▼                                       │  cached_keywords           │
missions ──1:N── mission_steps ─────────────│  cached_reviews            │
    │                                       └────────────────────────────┘
    │ 1:1
    ▼
reports
```

- `profiles` mirrors `auth.users` (Supabase Auth owns identity; we never touch `auth.*` directly). Created by a trigger on signup.
- `subscriptions` is **1:1 with the user** and is also the billing meter (`missions_used_this_period`). A row is created at signup in `trial` state — so the entitlement check is one indexed lookup with no special-casing for trials.
- `missions` is the unit of work; `mission_steps` is its execution + **cost ledger** (the table the business's unit economics are read from — see docs/data-economics.md §5).
- `reports` is 1:1 with a completed mission but a separate table: it's the user-facing product with different lifecycle and read patterns than the operational mission row.
- Cache tables hold **shared, public Amazon data** — they belong to no user, contain no PII, and are keyed by marketplace + natural key with a UUID surrogate PK for consistency.

---

## 2. Migration 0001 — extensions & shared helpers

```sql
-- pgcrypto ships enabled on Supabase; gen_random_uuid() is available.

create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;
```

---

## 3. Migration 0002 — profiles (auth mirror)

```sql
create table public.profiles (
  id          uuid primary key references auth.users (id) on delete cascade,
  email       text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

alter table public.profiles enable row level security;

create policy "read own profile"   on public.profiles for select using (id = auth.uid());
create policy "update own profile" on public.profiles for update using (id = auth.uid());
-- no insert/delete policies: rows are created by the signup trigger, removed by auth cascade

create trigger profiles_updated_at before update on public.profiles
  for each row execute function public.set_updated_at();

-- Signup hook: create profile + trial subscription atomically
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, email) values (new.id, new.email);
  insert into public.subscriptions (user_id, plan, status, mission_limit)
  values (new.id, 'trial', 'active', 2);          -- 2 free missions, no card
  return new;
end $$;

create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();
```

| Column | Purpose |
|---|---|
| `id` | Same UUID as `auth.users.id` — the one join key for the whole schema |
| `email` | Denormalized for app queries without touching `auth` schema |

*(Ordering note: 0002 and 0003 ship in one deploy — `handle_new_user` references `subscriptions`. The function is created here but the trigger only fires on real signups, which occur after both migrations apply.)*

---

## 4. Migration 0003 — subscriptions & stripe_events

```sql
create table public.subscriptions (
  id                         uuid primary key default gen_random_uuid(),
  user_id                    uuid not null unique references public.profiles (id) on delete cascade,
  plan                       text not null default 'trial'
                             check (plan in ('trial','starter','pro')),
  status                     text not null default 'active'
                             check (status in ('active','past_due','canceled','incomplete')),
  stripe_customer_id         text unique,          -- null until first checkout
  stripe_subscription_id     text unique,          -- null for trial
  mission_limit              int  not null default 2 check (mission_limit >= 0),
  missions_used_this_period  int  not null default 0 check (missions_used_this_period >= 0),
  current_period_end         timestamptz,          -- null for trial (trial never resets)
  created_at                 timestamptz not null default now(),
  updated_at                 timestamptz not null default now()
);

alter table public.subscriptions enable row level security;
create policy "read own subscription" on public.subscriptions
  for select using (user_id = auth.uid());
-- writes: service role only (Stripe webhooks + mission completion) — no user policies

create trigger subscriptions_updated_at before update on public.subscriptions
  for each row execute function public.set_updated_at();

create index subscriptions_stripe_customer_idx
  on public.subscriptions (stripe_customer_id) where stripe_customer_id is not null;

-- Webhook idempotency ledger (raw event persisted before processing)
create table public.stripe_events (
  id           uuid primary key default gen_random_uuid(),
  event_id     text not null unique,               -- Stripe evt_... id; the dedupe key
  type         text not null,
  payload      jsonb not null,
  processed_at timestamptz,                        -- null = received, not yet applied
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

alter table public.stripe_events enable row level security;   -- no policies: service-role only

create trigger stripe_events_updated_at before update on public.stripe_events
  for each row execute function public.set_updated_at();
```

| Column | Purpose |
|---|---|
| `mission_limit` | The plan's hard cap, denormalized onto the row (2 / 10 / 40). One lookup answers "can this user run a mission" — no plan-config join, and per-user overrides (support gestures) are free |
| `missions_used_this_period` | **The entire billing meter.** Incremented in the same transaction that marks a mission `complete`; reset to 0 by the `invoice.paid` webhook. Failed missions never increment it |
| `current_period_end` | From Stripe; drives period display and sanity-checks webhook resets |
| `stripe_events.event_id` | Unique — replayed webhook deliveries no-op on conflict |

**Entitlement check + meter, as one atomic service-role call:**

```sql
create or replace function public.increment_mission_usage(p_user_id uuid)
returns boolean language plpgsql security definer set search_path = public as $$
declare ok boolean;
begin
  update public.subscriptions
     set missions_used_this_period = missions_used_this_period + 1
   where user_id = p_user_id
     and status = 'active'
     and missions_used_this_period < mission_limit
  returning true into ok;
  return coalesce(ok, false);        -- false = at limit or not active → upgrade prompt
end $$;

revoke execute on function public.increment_mission_usage from anon, authenticated;
```

*(Called at mission creation, not completion, to prevent racing N parallel missions past the cap; a mission that later fails gets its increment refunded by the workflow's failure handler.)*

---

## 5. Migration 0004 — missions & mission_steps

```sql
create table public.missions (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references public.profiles (id) on delete cascade,
  input_type     text not null check (input_type in ('url','asin','keyword')),
  input_raw      text not null,                    -- exactly what the user typed
  marketplace    text not null default 'US',
  resolved_asin  text,                             -- set by Orchestrator (null for keyword missions until resolved)
  status         text not null default 'queued'
                 check (status in ('queued','running','complete','failed')),
  current_step   text,                             -- e.g. 'review_miner'; drives the progress UI via polling
  failure_reason text,                             -- user-readable; set only when status='failed'
  report_id      uuid,                             -- convenience back-ref, FK added in 0005
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  completed_at   timestamptz
);

alter table public.missions enable row level security;
create policy "read own missions" on public.missions
  for select using (user_id = auth.uid());
-- NO insert policy for users: missions are created by a server action (service role)
-- AFTER increment_mission_usage() succeeds. A client-side insert would bypass the cap.

create trigger missions_updated_at before update on public.missions
  for each row execute function public.set_updated_at();

create index missions_user_created_idx on public.missions (user_id, created_at desc);
create index missions_active_idx on public.missions (status)
  where status in ('queued','running');            -- tiny hot set: workflow + alarm queries

create table public.mission_steps (
  id            uuid primary key default gen_random_uuid(),
  mission_id    uuid not null references public.missions (id) on delete cascade,
  name          text not null check (name in
                ('orchestrator','data_fetch','product_analyst','review_miner','report_writer')),
  status        text not null default 'running'
                check (status in ('running','complete','failed')),
  model         text,                              -- e.g. 'claude-haiku-4-5'; null for data_fetch
  tokens_in     int  not null default 0,
  tokens_out    int  not null default 0,
  llm_cost_usd  numeric(8,4) not null default 0,   -- typed, not jsonb: this is the unit-economics ledger
  data_cost_usd numeric(8,4) not null default 0,   -- provider spend attributed to this step
  input         jsonb,                             -- step input summary (debugging/replay)
  output        jsonb,                             -- validated step output (debugging/replay)
  error         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  finished_at   timestamptz
);

alter table public.mission_steps enable row level security;   -- no policies: service-role only
-- (UI progress reads missions.current_step; step internals are ops data)

create trigger mission_steps_updated_at before update on public.mission_steps
  for each row execute function public.set_updated_at();

create index mission_steps_mission_idx on public.mission_steps (mission_id);
create index mission_steps_cost_idx on public.mission_steps (created_at);
  -- supports the weekly COGS query: avg cost/mission over a date range
```

The COGS dashboard query this schema exists to answer (docs/data-economics.md §5):

```sql
select date_trunc('day', m.created_at) as day,
       count(distinct m.id)                                as missions,
       round(avg(s.llm + s.data), 2)                       as avg_cost_usd
from missions m
join (select mission_id, sum(llm_cost_usd) llm, sum(data_cost_usd) data
      from mission_steps group by mission_id) s on s.mission_id = m.id
where m.status = 'complete'
group by 1 order by 1;
```

---

## 6. Migration 0005 — reports

```sql
create table public.reports (
  id                 uuid primary key default gen_random_uuid(),
  user_id            uuid not null references public.profiles (id) on delete cascade,
  mission_id         uuid not null unique references public.missions (id) on delete cascade,
  title              text not null,
  opportunity_score  int  not null check (opportunity_score between 0 and 100),
  verdict            text not null check (verdict in ('pursue','caution','avoid')),
  confidence         numeric(3,2) not null check (confidence between 0 and 1),
  body               jsonb not null,   -- ordered typed blocks (ARCHITECTURE.md §7); shape owned by zod, not SQL
  citations          jsonb not null default '[]'::jsonb,  -- citation id → raw data point + fetched_at
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

alter table public.reports enable row level security;
create policy "read own reports" on public.reports
  for select using (user_id = auth.uid());
-- writes: service role only (Report Writer step)

create trigger reports_updated_at before update on public.reports
  for each row execute function public.set_updated_at();

create index reports_user_created_idx on public.reports (user_id, created_at desc);

-- close the loop: missions.report_id back-reference
alter table public.missions
  add constraint missions_report_fk
  foreign key (report_id) references public.reports (id) on delete set null;
```

`opportunity_score`, `verdict`, `confidence` are **typed columns, not buried in `body`**: the dashboard lists and sorts by them, and future features (filtering, digest emails) query them. `user_id` is denormalized (derivable via `mission_id`) so the RLS policy stays the single standard pattern with no join.

---

## 7. Migration 0006 — cache tables (shared, service-role only)

One shape, three tables — kept separate (not one generic `cache` table) because TTLs, payload shapes, and purge policies differ per class, and "avoid unnecessary abstraction" cuts both ways: a generic entity-type column is the abstraction we don't need.

```sql
create table public.cached_products (
  id          uuid primary key default gen_random_uuid(),
  marketplace text not null default 'US',
  asin        text not null,
  payload     jsonb not null,          -- normalized provider output (Keepa-sourced)
  source      text not null,           -- 'keepa' | 'rainforest' — provenance for debugging
  fetched_at  timestamptz not null default now(),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (marketplace, asin)
);

create table public.cached_keywords (
  id          uuid primary key default gen_random_uuid(),
  marketplace text not null default 'US',
  phrase      text not null,           -- lowercased/trimmed before write (adapter's job)
  payload     jsonb not null,          -- search volume, trend, related terms (DataForSEO)
  source      text not null,
  fetched_at  timestamptz not null default now(),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (marketplace, phrase)
);

create table public.cached_reviews (
  id           uuid primary key default gen_random_uuid(),
  marketplace  text not null default 'US',
  asin         text not null,
  payload      jsonb not null,         -- review sample array (~100/ASIN cap, see data-economics §4)
  review_count int not null default 0, -- sample size actually retrieved — queryable health metric
  source       text not null,
  fetched_at   timestamptz not null default now(),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (marketplace, asin)
);

-- RLS enabled + zero policies = deny-all to anon/authenticated; service role bypasses RLS.
alter table public.cached_products enable row level security;
alter table public.cached_keywords enable row level security;
alter table public.cached_reviews  enable row level security;

create trigger cached_products_updated_at before update on public.cached_products
  for each row execute function public.set_updated_at();
create trigger cached_keywords_updated_at before update on public.cached_keywords
  for each row execute function public.set_updated_at();
create trigger cached_reviews_updated_at  before update on public.cached_reviews
  for each row execute function public.set_updated_at();

-- TTL purge support (weekly cron deletes rows older than ~30d; freshness checks
-- are per-row via the unique key, so fetched_at needs no index for reads)
create index cached_products_fetched_idx on public.cached_products (fetched_at);
create index cached_keywords_fetched_idx on public.cached_keywords (fetched_at);
create index cached_reviews_fetched_idx  on public.cached_reviews  (fetched_at);
```

Read path (adapter, service role): `select ... where marketplace = $1 and asin = $2` → if `fetched_at` within TTL (products 24h / keywords 7d / reviews 7d) return `payload`, else call provider and `insert ... on conflict (marketplace, asin) do update set payload, source, fetched_at = now()`.

---

## 8. RLS Summary

| Table | anon/authenticated access | Writes |
|---|---|---|
| `profiles` | select/update own | signup trigger |
| `subscriptions` | select own | service role (webhooks, `increment_mission_usage`) |
| `missions` | select own | service role (server action + Inngest workflow) |
| `mission_steps` | none (deny-all) | service role (workflow) |
| `reports` | select own | service role (Report Writer) |
| `stripe_events` | none (deny-all) | service role (webhook handler) |
| `cached_*` (×3) | none (deny-all) | service role (data adapter) |

Principles: RLS is enabled on **every** table — there is no unprotected table to misuse. Exactly one user-facing policy pattern (`user_id = auth.uid()` / `id = auth.uid()`), so there is nothing subtle to audit. All mutations that carry business rules (mission caps, meter increments, Stripe state) go through service-role code paths where the rules live; RLS is the backstop, not the business logic. `increment_mission_usage` is `security definer` with execute revoked from client roles.

---

## 9. Index Strategy

Only indexes with a named query behind them — every index taxes the write path:

| Index | Serves |
|---|---|
| `missions (user_id, created_at desc)` | Dashboard mission list (the hottest query) |
| `missions (status) where queued/running` | Workflow pickup + "stuck missions" alarm; partial → stays tiny |
| `reports (user_id, created_at desc)` | Report library list |
| `mission_steps (mission_id)` | Step lookup per mission (workflow + debugging) |
| `mission_steps (created_at)` | Weekly COGS aggregation window |
| `subscriptions (user_id) unique` | Entitlement check (implicit via unique constraint) |
| `subscriptions (stripe_customer_id) partial` | Webhook → user resolution |
| `stripe_events (event_id) unique` | Webhook idempotency (implicit) |
| `cached_* (marketplace, natural key) unique` | Cache point-reads (implicit) |
| `cached_* (fetched_at)` | Purge cron only |

Deliberately absent: no index on `missions.resolved_asin` (no lookup-by-ASIN feature in V1), no GIN indexes on any JSONB (nothing queries inside payloads/bodies — they are read whole), no composite status+user indexes (per-user row counts are tiny). Add indexes when a slow query exists, not before.

---

## 10. Migration Order & Rationale

| # | File | Contents | Depends on |
|---|---|---|---|
| 0001 | `extensions_helpers.sql` | `set_updated_at()` | — |
| 0002 | `profiles.sql` | profiles, RLS, signup trigger fn | 0001 |
| 0003 | `subscriptions.sql` | subscriptions, stripe_events, `increment_mission_usage` | 0002 (FK to profiles) |
| 0004 | `missions.sql` | missions, mission_steps, indexes | 0002 |
| 0005 | `reports.sql` | reports + `missions.report_id` FK back-ref | 0004 |
| 0006 | `cache.sql` | cached_products / keywords / reviews | 0001 only |

Applied via Supabase CLI in CI before Vercel promote (ARCHITECTURE.md §10). 0002+0003 deploy together (signup trigger references subscriptions). The `missions ↔ reports` circular reference is resolved by adding the back-ref FK in 0005, after both tables exist. All migrations are forward-only; schema changes post-launch follow expand-then-contract.

---

## 11. What Was Deliberately Left Out

- **No `plans` config table** — two plans' limits are a constant in code and a denormalized `mission_limit` column. A table adds a join to every entitlement check to serve plans that don't change monthly.
- **No history/time-series tables** — Keepa payloads (which include history arrays) live whole in `cached_products.payload`. When a feature needs queryable history (trend charts, alerts), that's the trigger to extract it (ARCHITECTURE.md §12).
- **No soft deletes, no audit log, no `deleted_at`** — cascade deletes from `auth.users` satisfy account deletion; nothing in V1 needs undo.
- **No Postgres queues/job tables** — Inngest owns workflow state; `mission_steps` is our ledger of what happened, not a coordination mechanism.
