# Delium

Private, AI-powered Amazon product research system — a personal tool for finding,
validating, and scoring private-label opportunities. Not a SaaS: one user, no
auth, no billing. See `ARCHITECTURE.md` for the full system design and `docs/`
for the data layer, deterministic analysis engine, scoring model, and agent
layer specs.

## Status

**Project foundation only.** Package layout, CLI wiring, configuration
loading, database connection, and logging are in place. Data providers,
the analysis engine, agents, and report rendering are not implemented yet —
see the build order in `ARCHITECTURE.md` §12.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management

## Setup

```bash
./scripts/setup_dev.sh
```

This syncs dependencies, creates `.env` from `.env.example` if missing, and
runs the test suite. Or do it by hand:

```bash
uv sync --all-groups
cp .env.example .env   # then fill in real API keys
uv run pytest
```

## Usage

```bash
uv run delium --help
uv run delium --version

uv run delium discover "silicone baby food tray"
uv run delium validate B0EXAMPLE1
uv run delium pains B0EXAMPLE1
uv run delium watch
uv run delium portfolio
```

### Web UI (local browser front end)

A local Streamlit app wraps the same internal functions as the CLI (no shelling
out, no duplicated logic). It runs only on your machine (localhost, single user,
no login).

```bash
uv sync --group ui        # one-time: install the optional UI dependencies
uv run delium ui          # launches http://localhost:8501 (use --port to change)
```

The UI is organised into four sections (sidebar):

- **Home** — your shortlist, top opportunities matching the active profile,
  recent runs, and Keepa tokens left.
- **Find** — one page with modes: **Emerging** (Keepa Product Finder, profile-
  driven filters + variation dedupe), **Keyword** research, **Black Box
  (Discover)**, **Cross-market**, and **History**. Every result links to the
  workspace ("Open →").
- **Product** — the **workspace**: everything about ONE product across seven
  tabs — Overview (plain-English card), Sales & momentum (Keepa history, Keepa
  monthly-sold vs Delium's estimate, emergence, launch age), Keywords,
  Competitors (page-one set, deduped, with a launchability stat), Reviews &
  improvement ideas, Profit (real Keepa FBA fee where available, profile
  COGS/freight, units affordable + break-even), and Risk & verdict. One
  **Deep dive** button fetches whatever is missing (competitors, keywords,
  reviews, fees) after showing a combined cost/token estimate. You can add to
  the **shortlist** (status + notes) and **Re-check** to refresh Keepa data and
  store a snapshot so you can see whether momentum held.
- **Settings** — the **Research Profile** (+ named presets), API credential
  status, and Usage.

Every page shows the active profile at the top ("Profile: Conservative $5k").
A sidebar panel shows which credentials are configured (never their values).
Before any paid provider call the app shows the providers and an estimated cost
and requires a confirm click, then reports the actual cost; cached data is
reused and shown as \$0.00.

### Research Profile

One shared, DB-backed set of seller preferences drives every finder/search
default and every profit calculation, and steers how results are sorted and
highlighted. Fields: budget, target sell-price range, min monthly sales, max
reviews, preferred/excluded categories, marketplaces, max size tier/weight,
default COGS (% of price or $/unit), freight per kg, target net margin/ROI, and
risk tolerance. It is a **preferences layer only** — it never relaxes a hard
kill, gate, or scoring weight (those stay in `config` + `scoring.py`). Ships with
two presets ("Conservative $5k", "Growth $20k"); edit them or add your own in
Settings. Profit everywhere uses the profile's COGS/freight unless you override
per product.

### Keepa evidence

Delium captures three extra fields from the same `stats`-enabled Keepa `/product`
call (no extra tokens), verified against Keepa's official product object
(`github.com/keepacom/api_backend`): `monthlySold` (units bought in the past
month — Keepa's real figure, not an estimate), `fbaFees.pickAndPackFee` (the real
FBA fulfilment fee, cents), and `referralFeePercentage`. The real FBA fee is fed
into the profit engine when present (falling back to the fee table, clearly
labelled), so profit confidence rises through the existing rules.

### Daily Scan (`delium scan`)

A resumable, budget-gated funnel that turns a broad Keepa sweep into a short
ranked "scan inbox". It never loosens a kill or gate and preserves
"missing = unknown"; scoring.py stays the sole verdict owner.

```bash
delium scan                              # interactive: shows projected cost, confirms
delium scan --marketplaces US,CA,UK
delium scan --top 10 --budget-cap 1500 --max-spend 5
delium scan --min-confidence medium
delium scan --scheduled                  # non-interactive; aborts if over caps
delium scan --resume <scan_id>           # continue a crashed scan from its last stage
delium scan report [<scan_id>]           # print a past scan (latest if omitted)
```

**Stages** (each persists progress to SQLite, logs Keepa tokens + $, and records
its funnel counts, so a crash resumes from the last completed stage):

0. **Preflight** — load the active Research Profile, a free Keepa `/token`
   check, project the cost of every later stage, and **abort before spending**
   if it would exceed `--budget-cap` (tokens) or `--max-spend` (USD).
1. **Sweep** — Keepa Product Finder across BSR sub-bands per profile category,
   with as many hard-kill rules as the finder supports pushed **into** the query
   (price band → `current_NEW_gte/lte` for K1/K2; not sold by Amazon →
   `buyBoxIsAmazon=false` for K4; review cap → `current_COUNT_REVIEWS_lte` for
   K6; weight limit → `packageWeight_lte` for K3) so less junk comes back. Also
   runs the existing cross-market pass.
2. **Hydrate** — batched, cache-first Keepa `/product` (≤100/call) for the sweep.
3. **Normalize + hard kill** — merge variations by parent ASIN, flag established
   brands, apply the existing kill rules (every kill logged with its reason).
4. **Cheap scoring** — demand, profitability (with the real Keepa fee), risk,
   emergence — everything needing no further paid call. Missing = unknown.
5. **Competitor sets** — for the top ~40 by cheap score: main keyword →
   DataForSEO SERP → batched Keepa hydration → competition + launchability.
6. **Rank** — sellability on all survivors; keep the top N (respecting
   `--min-confidence`).
7. **Finalists** — reviews + Claude differentiation (the validation pipeline,
   per-product budget caps) for the top N only, then re-score and re-rank.
   Products without differentiation show **"differentiation pending"**, never a
   heuristic score mixed in as if it were real.
8. **Report** — persist the funnel, per-stage cost, top N with one-line reasons,
   and emerging categories. Finalists go to the **scan inbox** — *separate* from
   the shortlist; you promote them manually.

Australia has no Keepa coverage: a requested `AU` marketplace is reported as
"AU pending — Keepa has no Australia data" and skipped; the scan continues.

**Cost model (projected worst case, default scan — US, ~300 swept, 40
competitor sets, top 10):** sweep ≈ 44 Keepa tokens; hydrate ≈ 600 tokens;
competitor sets ≈ 800 tokens + ~$1.20 DataForSEO; finalists ≈ up to
`$3.03 × 10` data + `$1.00 × 10` LLM (bounded by the per-validate budget caps).
Everything is cache-first, so a repeated lookup is free and real costs run far
below these caps. The caps bind on the projection, before any spend.

## Configuration

- `config/config.toml` — assumptions, preferences, score weights, gates (see
  `ARCHITECTURE.md` §8 and `docs/analysis-engine.md` §9). Safe to commit —
  contains no secrets.
- `.env` — API keys (Keepa, DataForSEO, review provider, LLM vendor). Never
  committed; see `.env.example` for the expected variables.

Both support path overrides via environment variables
(`DELIUM_CONFIG_PATH`, `DELIUM_DATA_DIR`, `DELIUM_DB_PATH`,
`DELIUM_REPORTS_DIR`, `DELIUM_LOG_LEVEL`) — see `src/delium/utils/paths.py`.

## Project layout

```
src/delium/
├── cli/          # Typer app: discover · validate · emerging · diagnose · ui · …
├── config/       # config.toml loading (models.py) + secrets (secrets.py)
├── database/     # SQLite connection, numbered migrations, repository
├── providers/    # Keepa / DataForSEO / review provider adapters
├── ingestion/    # fetch → normalize → cache-first pipeline (batched Keepa)
├── analysis/     # deterministic demand/competition/profit/risk/scoring engines
├── discovery/    # discover/emerging/daily-scan orchestration, assembly, diagnostics, workspace, calibrate
├── profile/      # Research Profile model + store (DB-backed preferences)
├── agents/       # Scout / Analyst / Review Miner / Strategist (LLM layer)
├── reports/      # markdown rendering + deterministic plain-English cards
├── ui/           # Streamlit app + tested service/format helpers
└── utils/        # logging, filesystem paths
```

Data lives in `data/delium.db` (SQLite, git-ignored). Generated reports land
in `reports/` (git-ignored). Neither directory is meant to be committed —
they're regenerated from the config + cached provider data.

## Development

```bash
uv run pytest              # tests
uv run pytest --cov        # tests with coverage report
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy                # type check
```

## Design principles

1. AI interprets; code calculates. Every number that drives a decision (fees,
   margins, ROI, opportunity score) is deterministic Python — LLMs never do
   arithmetic and never invent data.
2. Evidence or it didn't happen. Every qualitative claim in a report cites the
   data behind it.
3. Human in the loop, always. The system only ever produces reports and a
   candidate database — it never acts on its own.

Full rationale in `ARCHITECTURE.md`.
