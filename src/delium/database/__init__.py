"""SQLite storage: connection handling, SQL migrations, and repository helpers.

Schema is defined as explicit SQL in `migrations/` (docs/data-layer.md §2), not
an ORM. `initialize_database()` creates the file, sets WAL + foreign keys, and
applies pending migrations; `repository` provides thin typed insert/read helpers.
"""

from delium.database import repository
from delium.database.connection import connect, get_connection
from delium.database.migrations import (
    apply_migrations,
    discover_migrations,
    get_applied_versions,
    initialize_database,
)

__all__ = [
    "apply_migrations",
    "connect",
    "discover_migrations",
    "get_applied_versions",
    "get_connection",
    "initialize_database",
    "repository",
]
