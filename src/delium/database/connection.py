"""SQLite connection handling.

This is a connection placeholder only — the schema itself (products,
keywords, reviews, runs, etc. per docs/data-layer.md) is a later build step.
What's established here is how the rest of the codebase is expected to talk
to SQLite, so that schema work has a stable foundation to build on:

* one file, WAL mode, foreign keys on
* `sqlite3.Row` row factory (dict-like access by column name)
* a context-managed connection so callers never forget to close/commit
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from delium.utils.paths import ensure_directories, get_database_path


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a configured SQLite connection to the Delium database.

    Callers own the returned connection (close it themselves) — use
    `get_connection()` instead for the common case of a single request
    scoped by a `with` block.
    """
    db_path = path or get_database_path()
    ensure_directories()
    conn = sqlite3.connect(db_path)
    _configure_connection(conn)
    return conn


@contextmanager
def get_connection(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context-managed connection: commits on clean exit, rolls back on error.

    Usage:
        with get_connection() as conn:
            conn.execute("select 1")
    """
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
