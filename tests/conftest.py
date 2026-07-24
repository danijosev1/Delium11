"""Shared pytest fixtures.

Every test that touches the filesystem (config/db/reports paths) gets an
isolated tmp_path via environment overrides — tests never read or write
the real ./config, ./data, or ./reports directories.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point all Delium filesystem paths at a fresh tmp_path for one test."""
    monkeypatch.setenv("DELIUM_CONFIG_PATH", str(tmp_path / "config.toml"))
    monkeypatch.setenv("DELIUM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DELIUM_DB_PATH", str(tmp_path / "data" / "delium.db"))
    monkeypatch.setenv("DELIUM_REPORTS_DIR", str(tmp_path / "reports"))
    yield tmp_path


@pytest.fixture
def initialized_db(isolated_env: Path) -> Path:
    """An isolated environment with migrations already applied."""
    from delium.database import initialize_database

    initialize_database()
    return isolated_env
