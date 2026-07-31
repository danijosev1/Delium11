"""Cache-freshness check shared across ingestion flows.

Freshness is judged from a raw_fetch's `fetched_at` timestamp (SQLite writes
these as 'YYYY-MM-DD HH:MM:SS' in UTC) against a per-class TTL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def is_fresh(fetched_at: str, ttl: timedelta, *, now: datetime | None = None) -> bool:
    """True if `fetched_at` is within `ttl` of now (UTC)."""
    current = now or datetime.now(UTC)
    try:
        stamped = datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return False
    return (current - stamped) < ttl
