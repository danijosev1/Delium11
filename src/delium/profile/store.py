"""Persistence orchestration for research profiles.

Thin layer between the `ResearchProfile` model and `repository`: seeds the two
shipped presets on first use, and loads/saves/activates profiles as models.
Every function takes an open connection (the UI service opens/closes it), so
this module never touches the DB path directly.
"""

from __future__ import annotations

import sqlite3

from delium.database import repository
from delium.profile.models import DEFAULT_PRESETS, ResearchProfile


def ensure_seeded(conn: sqlite3.Connection) -> None:
    """Insert the shipped presets the first time the profile table is empty, so
    there is always an active profile to drive the UI."""
    if repository.count_research_profiles(conn) > 0:
        return
    for preset in DEFAULT_PRESETS:
        pid = repository.save_research_profile(conn, **preset.to_values())
        if preset.is_active:
            repository.set_active_research_profile(conn, pid)


def load_active(conn: sqlite3.Connection) -> ResearchProfile:
    """The active profile (seeding + activating a default if none exists)."""
    ensure_seeded(conn)
    row = repository.get_active_research_profile(conn)
    if row is None:
        profiles = repository.list_research_profiles(conn)
        if not profiles:  # pragma: no cover - ensure_seeded guarantees ≥1
            raise RuntimeError("no research profile available after seeding")
        repository.set_active_research_profile(conn, profiles[0]["id"])
        row = repository.get_active_research_profile(conn)
        assert row is not None
    return ResearchProfile.from_row(row)


def list_profiles(conn: sqlite3.Connection) -> list[ResearchProfile]:
    ensure_seeded(conn)
    return [ResearchProfile.from_row(r) for r in repository.list_research_profiles(conn)]


def get(conn: sqlite3.Connection, profile_id: str) -> ResearchProfile | None:
    row = repository.get_research_profile(conn, profile_id)
    return ResearchProfile.from_row(row) if row is not None else None


def save(conn: sqlite3.Connection, profile: ResearchProfile) -> str:
    """Insert or update a profile; returns its id."""
    return repository.save_research_profile(conn, profile_id=profile.id, **profile.to_values())


def set_active(conn: sqlite3.Connection, profile_id: str) -> None:
    repository.set_active_research_profile(conn, profile_id)


def delete(conn: sqlite3.Connection, profile_id: str) -> None:
    """Delete a profile. If it was the active one, activate another so the UI
    always has an active profile."""
    was_active = repository.get_active_research_profile(conn)
    repository.delete_research_profile(conn, profile_id)
    if was_active is not None and was_active["id"] == profile_id:
        remaining = repository.list_research_profiles(conn)
        if remaining:
            repository.set_active_research_profile(conn, remaining[0]["id"])
