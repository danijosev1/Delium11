"""SQL migration system.

Migrations are plain `.sql` files in `migrations/`, named `NNNN_name.sql`.
They are applied in numeric order exactly once; applied versions are tracked in
a `schema_migrations` table. No ORM — the schema is explicit SQL.

Atomicity: each migration's schema statements and its `schema_migrations`
bookkeeping row are wrapped in a single `BEGIN … COMMIT` and executed together,
so a migration either fully applies and is recorded, or neither.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from delium.utils.logging import get_logger
from delium.utils.paths import ensure_directories, get_database_path

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Controlled filename shape: NNNN_lowercase_words.sql — no quotes, so embedding
# the filename in the bookkeeping INSERT below is injection-safe.
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def discover_migrations() -> list[Migration]:
    """Return all migration files sorted by version, validating their names."""
    migrations: list[Migration] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_RE.match(path.name)
        if not match:
            raise ValueError(f"Migration file {path.name!r} does not match NNNN_name.sql naming.")
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=path.name,
                sql=path.read_text(encoding="utf-8"),
            )
        )
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("Duplicate migration version numbers found.")
    return migrations


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def get_applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Versions already applied, per the schema_migrations table."""
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row[0]) for row in rows}


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending migrations in order. Returns versions newly applied.

    `conn` must be in autocommit mode (isolation_level=None) so the explicit
    BEGIN/COMMIT in each migration controls the transaction.
    """
    applied = get_applied_versions(conn)
    newly_applied: list[int] = []

    for migration in discover_migrations():
        if migration.version in applied:
            continue

        script = (
            "BEGIN;\n"
            f"{migration.sql}\n"
            "INSERT INTO schema_migrations (version, name) VALUES "
            f"({migration.version}, '{migration.name}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error:
            # Roll back the half-applied migration. Use conn.rollback() rather
            # than executescript("ROLLBACK") — the latter would COMMIT the
            # pending transaction first, defeating the point.
            conn.rollback()
            log.error("Migration %s failed to apply.", migration.name)
            raise

        log.info("Applied migration %s", migration.name)
        newly_applied.append(migration.version)

    return newly_applied


def initialize_database(path: Path | None = None) -> list[int]:
    """Create the database file (if needed), set pragmas, and apply migrations.

    Returns the list of migration versions applied by this call (empty if the
    database was already up to date). Idempotent.
    """
    db_path = path or get_database_path()
    ensure_directories()

    # Autocommit mode: migrations manage their own transactions.
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return apply_migrations(conn)
    finally:
        conn.close()
