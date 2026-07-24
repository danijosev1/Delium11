"""Schema integrity: foreign keys, CHECK constraints, and primary keys are
actually enforced by the database (not just declared)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from delium.database import get_connection, repository


def _make_run_and_fetch(conn: sqlite3.Connection) -> tuple[str, str]:
    run_id = repository.insert_run(conn, command="validate", input_="B0TEST00001")
    fetch_id = repository.insert_raw_fetch(
        conn,
        run_id=run_id,
        provider="keepa",
        endpoint="product",
        request_key="keepa:product:B0TEST00001",
        payload={"asin": "B0TEST00001"},
    )
    return run_id, fetch_id


def test_foreign_key_violation_is_rejected(initialized_db: Path) -> None:
    # A product referencing a non-existent fetch_id must fail.
    with pytest.raises(sqlite3.IntegrityError):  # noqa: SIM117
        with get_connection() as conn:
            repository.upsert_product(conn, asin="B0TEST00001", fetch_id="does-not-exist")


def test_review_requires_existing_product(initialized_db: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):  # noqa: SIM117
        with get_connection() as conn:
            _, fetch_id = _make_run_and_fetch(conn)
            repository.insert_review(
                conn,
                review_id="r1",
                asin="B0NOEXIST99",  # no such product row
                fetch_id=fetch_id,
                stars=5,
            )


def test_stars_check_constraint(initialized_db: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):  # noqa: SIM117
        with get_connection() as conn:
            _, fetch_id = _make_run_and_fetch(conn)
            repository.upsert_product(conn, asin="B0TEST00001", fetch_id=fetch_id)
            repository.insert_review(
                conn,
                review_id="r1",
                asin="B0TEST00001",
                fetch_id=fetch_id,
                stars=7,  # out of 1..5
            )


def test_selection_method_check_constraint(initialized_db: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):  # noqa: SIM117
        with get_connection() as conn:
            run_id, _ = _make_run_and_fetch(conn)
            conn.execute(
                """
                INSERT INTO competitor_sets
                    (id, run_id, target_asin, member_asins, selection_method)
                VALUES ('cs1', ?, 'B0TEST00001', '[]', 'not_a_method')
                """,
                (run_id,),
            )


def test_review_theme_kind_check_constraint(initialized_db: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):  # noqa: SIM117
        with get_connection() as conn:
            run_id, fetch_id = _make_run_and_fetch(conn)
            repository.upsert_product(conn, asin="B0TEST00001", fetch_id=fetch_id)
            conn.execute(
                """
                INSERT INTO review_themes (id, run_id, asin, kind, theme, quote_review_ids)
                VALUES ('t1', ?, 'B0TEST00001', 'invalid_kind', 'x', '[]')
                """,
                (run_id,),
            )


def test_cascade_delete_from_run(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id, fetch_id = _make_run_and_fetch(conn)
        repository.insert_competitor_set(
            conn,
            run_id=run_id,
            target_asin="B0TEST00001",
            member_asins=["B0AAA", "B0BBB"],
            selection_method="serp_top",
        )

    with get_connection() as conn:
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    with get_connection() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM competitor_sets WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    assert remaining == 0
