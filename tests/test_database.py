from __future__ import annotations

from pathlib import Path

from delium.database import connect, get_connection


def test_connect_creates_database_file(isolated_env: Path) -> None:
    db_path = isolated_env / "data" / "delium.db"
    assert not db_path.exists()

    conn = connect(db_path)
    try:
        assert db_path.exists()
    finally:
        conn.close()


def test_connection_uses_wal_and_foreign_keys(isolated_env: Path) -> None:
    with get_connection() as conn:
        (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        (foreign_keys,) = conn.execute("PRAGMA foreign_keys").fetchone()

    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1


def test_row_factory_allows_dict_like_access(isolated_env: Path) -> None:
    with get_connection() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO t (name) VALUES ('widget')")
        row = conn.execute("SELECT * FROM t").fetchone()

    assert row["name"] == "widget"


def test_context_manager_commits_on_success(isolated_env: Path) -> None:
    with get_connection() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t DEFAULT VALUES")

    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]

    assert count == 1


def test_context_manager_rolls_back_on_exception(isolated_env: Path) -> None:
    with get_connection() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    try:
        with get_connection() as conn:
            conn.execute("INSERT INTO t DEFAULT VALUES")
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]

    assert count == 0
