"""SQLite storage. Schema (products/keywords/reviews/runs/...) lands in a later
build step per docs/data-layer.md — this package currently only establishes
the connection convention the rest of the codebase builds on.
"""

from delium.database.connection import connect, get_connection

__all__ = ["connect", "get_connection"]
