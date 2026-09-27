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
├── discovery/    # discover/emerging orchestration, assembly, diagnostics, workspace
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
