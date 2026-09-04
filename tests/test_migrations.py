from __future__ import annotations

from pathlib import Path

import pytest

from delium.database import (
    connect,
    discover_migrations,
    get_applied_versions,
    initialize_database,
)
from delium.database import migrations as migrations_module

# Every table the schema is expected to create (docs/data-layer.md §2 + the
# supporting `runs` parent + the migration bookkeeping table).
EXPECTED_TABLES = {
    "runs",
    "raw_fetches",
    "products",
    "price_bsr_history",
    "product_derived",
    "keywords",
    "serp_rankings",
    "competitor_sets",
    "reviews",
    "review_themes",
    "product_matches",
    "candidates",
    "validations",
    "feature_requests",
    "bundle_signals",
    "agent_runs",
    "schema_migrations",
}


def _table_names(db_path: Path) -> set[str]:
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {row["name"] for row in rows}


def test_discover_migrations_are_ordered_and_named() -> None:
    migrations = discover_migrations()
    assert migrations, "expected at least one migration file"
    versions = [m.version for m in migrations]
    assert versions == sorted(versions)
    assert versions[0] == 1


def test_initialize_creates_all_tables(isolated_env: Path) -> None:
    applied = initialize_database()
    assert applied == [1, 2, 3, 4]
    assert EXPECTED_TABLES.issubset(_table_names(isolated_env / "data" / "delium.db"))


def test_initialize_enables_wal_and_foreign_keys(isolated_env: Path) -> None:
    initialize_database()
    conn = connect()
    try:
        (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        (foreign_keys,) = conn.execute("PRAGMA foreign_keys").fetchone()
    finally:
        conn.close()
    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1


def test_initialize_is_idempotent(isolated_env: Path) -> None:
    first = initialize_database()
    second = initialize_database()
    assert first == [1, 2, 3, 4]
    assert second == []  # nothing new to apply the second time


def test_applied_versions_recorded(isolated_env: Path) -> None:
    initialize_database()
    conn = connect()
    try:
        applied = get_applied_versions(conn)
        rows = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    finally:
        conn.close()
    assert applied == {1, 2, 3, 4}
    names = {row["name"] for row in rows}
    assert "0001_initial_schema.sql" in names
    assert "0002_cross_market.sql" in names
    assert "0003_discovery.sql" in names
    assert "0004_agent_layer.sql" in names


def test_key_indexes_exist(isolated_env: Path) -> None:
    initialize_database()
    conn = connect()
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    finally:
        conn.close()
    index_names = {row["name"] for row in rows}
    assert "idx_raw_fetches_lookup" in index_names
    assert "idx_reviews_asin" in index_names
    assert "idx_competitor_sets_run" in index_names


def _columns(db_path: Path, table: str) -> set[str]:
    conn = connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {row["name"] for row in rows}


def test_migration_0002_adds_identity_and_matches(isolated_env: Path) -> None:
    initialize_database()
    db_path = isolated_env / "data" / "delium.db"
    product_cols = _columns(db_path, "products")
    assert {"gtin", "manufacturer"} <= product_cols
    assert "marketplace" in _columns(db_path, "serp_rankings")
    match_cols = _columns(db_path, "product_matches")
    assert {
        "source_asin",
        "source_marketplace",
        "target_asin",
        "target_marketplace",
        "match_method",
        "match_confidence",
        "match_score",
        "signals",
        "conflicts",
        "evidence",
    } <= match_cols


def test_bad_filename_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "bad-name.sql").write_text("SELECT 1;")
    monkeypatch.setattr(migrations_module, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(ValueError, match="does not match"):
        discover_migrations()


def test_duplicate_versions_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0001_b.sql").write_text("SELECT 1;")
    monkeypatch.setattr(migrations_module, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(ValueError, match="Duplicate migration version"):
        discover_migrations()


def test_failed_migration_rolls_back(
    initialized_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A second migration whose SQL is invalid must not be recorded, and must
    # leave no half-applied table behind.
    import sqlite3

    from delium.utils.paths import get_database_path

    migration_dir = tmp_path / "m"
    migration_dir.mkdir()
    # Version 5 (past the real 0001–0004 already applied to initialized_db).
    (migration_dir / "0005_broken.sql").write_text(
        "CREATE TABLE ok_table (id INTEGER);\nTHIS IS NOT SQL;"
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS_DIR", migration_dir)

    # apply_migrations requires an autocommit connection (isolation_level=None).
    conn = sqlite3.connect(get_database_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.Error):
            migrations_module.apply_migrations(conn)
        applied = migrations_module.get_applied_versions(conn)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert 5 not in applied  # broken migration not recorded
    assert "ok_table" not in tables  # its partial DDL was rolled back
