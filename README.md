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

All five commands are wired up and argument-complete; each currently exits
with "not implemented yet" until its pipeline is built.

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
├── cli/          # Typer app: discover · validate · pains · watch · portfolio
├── config/       # config.toml loading (models.py) + secrets (secrets.py)
├── database/     # SQLite connection handling
├── providers/    # Keepa / DataForSEO / review provider adapters (not yet implemented)
├── ingestion/     # fetch → normalize → cache pipeline (not yet implemented)
├── analysis/     # deterministic demand/competition/profit/risk/scoring (not yet implemented)
├── agents/       # Scout / Analyst / Review Miner / Strategist (not yet implemented)
├── reports/      # typed-block → markdown rendering (not yet implemented)
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
