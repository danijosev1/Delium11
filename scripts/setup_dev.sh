#!/usr/bin/env bash
# One-command dev environment setup.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Syncing dependencies with uv..."
uv sync --all-groups

if [ ! -f .env ]; then
  echo "Creating .env from .env.example (fill in real API keys before running providers)..."
  cp .env.example .env
fi

echo "Running test suite..."
uv run pytest

echo "Done. Try: uv run delium --help"
