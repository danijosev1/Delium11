"""Command Center service layer (Part 3B) — background-scan command + progress
reads, inbox card assembly + filters, and inbox actions.

These exercise the tested service functions in `delium.ui.services` (the
Streamlit `app.py` is a thin, untested presentation layer over them). No network:
data is inserted straight into SQLite, so the reads run against fixtures.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.ui import services

CFG = DeliumConfig()


def _seed_scan(conn, *, image: str | None = "https://m.media-amazon.com/images/I/a.jpg"):  # type: ignore[no-untyped-def]
    """A minimal completed scan with one finalist + product + a category."""
    run_id = repository.insert_run(conn, command="scan", input_="US")
    scan_id = repository.create_scan(
        conn, run_id=run_id, marketplaces=["US"], profile_id=None, profile_name="p", params={}
    )
    fetch_id = repository.insert_raw_fetch(
        conn, run_id=run_id, provider="keepa", endpoint="product", request_key="k", payload={}
    )
    repository.upsert_product(
        conn,
        asin="B0CC000001",
        fetch_id=fetch_id,
        marketplace="US",
        title="Silicone Widget",
        brand="Acme",
        image_url=image,
        monthly_sold=300,
    )
    repository.upsert_scan_candidate(
        conn,
        scan_id=scan_id,
        asin="B0CC000001",
        marketplace="US",
        outcome="finalist",
        stage_reached=7,
        sellability=71.0,
        confidence="high",
        differentiation_status="done",
        rank=1,
        reason="strong demand, thin competition",
        data={"category": "Home & Kitchen", "price_cents": 4999, "monthly_sold": 300},
    )
    repository.upsert_scan_category(
        conn,
        scan_id=scan_id,
        category="Home & Kitchen",
        momentum_score=88.0,
        metrics={},
        reason="rising units, few incumbents",
    )
    repository.update_scan(conn, scan_id, status="complete")
    conn.commit()
    return scan_id


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_background_scan_command_is_the_scan_cli() -> None:
    cmd = services.background_scan_command(marketplaces=("US", "CA"), light=True, top=8)
    # Same `delium scan` CLI, non-interactive, light, with the marketplaces + top.
    assert cmd[1:] == [
        "run",
        "delium",
        "scan",
        "--yes",
        "--marketplaces",
        "US,CA",
        "--top",
        "8",
        "--light",
    ]
    full = services.background_scan_command(light=False)
    assert full[-1] == "--full"


def test_next_scheduled_run_picks_soonest_future() -> None:
    now = datetime(2025, 8, 1, 7, 0)
    # 06:00 already passed today → tomorrow; 18:00 still ahead → today 18:00 wins.
    nxt = services._next_scheduled_run(((6, 0), (18, 0)), now=now)
    assert nxt == datetime(2025, 8, 1, 18, 0)
    # Before both → the earliest today.
    early = services._next_scheduled_run(((6, 0), (18, 0)), now=datetime(2025, 8, 1, 5, 0))
    assert early == datetime(2025, 8, 1, 6, 0)
    assert services._next_scheduled_run(()) is None


# ---------------------------------------------------------------------------
# Progress reads
# ---------------------------------------------------------------------------
def test_scan_progress_none_when_no_scans(initialized_db: Path) -> None:
    assert services.scan_progress() is None
    assert services.command_center()["last_scan"] is None
    assert services.emerging_categories() == []
    assert services.inbox_cards(CFG) == []


def test_scan_progress_reads_status_and_funnel(initialized_db: Path) -> None:
    with get_connection() as conn:
        scan_id = _seed_scan(conn)
    prog = services.scan_progress(scan_id)
    assert prog is not None
    assert prog["status"] == "complete"
    assert prog["running"] is False
    assert prog["funnel"]["finalists"] == 1


# ---------------------------------------------------------------------------
# Inbox cards
# ---------------------------------------------------------------------------
def test_inbox_cards_resolve_image_title_and_amazon_source(initialized_db: Path) -> None:
    with get_connection() as conn:
        scan_id = _seed_scan(conn)
    cards = services.inbox_cards(CFG, scan_id)
    assert len(cards) == 1
    card = cards[0]
    assert card.asin == "B0CC000001"
    assert card.title == "Silicone Widget"
    assert card.image_url == "https://m.media-amazon.com/images/I/a.jpg"
    assert card.price_usd == 49.99
    # monthly_sold present in facts → an Amazon (not estimated) source.
    assert card.monthly_sales == 300
    assert card.sales_source == "amazon"
    assert card.confidence == "high"
    assert card.why == "strong demand, thin competition"


def test_inbox_cards_unknown_source_without_monthly_sold_or_estimate(initialized_db: Path) -> None:
    """No Keepa monthly_sold in the facts and no demand estimate to fall back on →
    the source stays 'unknown' (missing = unknown, never assumed)."""
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="scan", input_="US")
        scan_id = repository.create_scan(
            conn, run_id=run_id, marketplaces=["US"], profile_id=None, profile_name="p", params={}
        )
        fetch_id = repository.insert_raw_fetch(
            conn, run_id=run_id, provider="keepa", endpoint="product", request_key="k", payload={}
        )
        repository.upsert_product(conn, asin="B0CC000009", fetch_id=fetch_id, title="No sold")
        repository.upsert_scan_candidate(
            conn,
            scan_id=scan_id,
            asin="B0CC000009",
            marketplace="US",
            outcome="finalist",
            stage_reached=7,
            sellability=40.0,
            confidence="low",
            rank=1,
            data={"category": "Toys", "price_cents": 2500},  # no monthly_sold
        )
        conn.commit()
    cards = services.inbox_cards(CFG, scan_id)
    assert len(cards) == 1
    assert cards[0].sales_source == "unknown"


def test_inbox_filters_apply(initialized_db: Path) -> None:
    with get_connection() as conn:
        scan_id = _seed_scan(conn)
    # High-confidence, amazon-source, in-band → kept.
    assert services.inbox_cards(CFG, scan_id, min_confidence="high", sales_source="amazon")
    # Medium floor keeps a high card; a price cap below the price drops it.
    assert services.inbox_cards(CFG, scan_id, price_max_usd=10.0) == []
    # An estimated-only filter drops the amazon-source card.
    assert services.inbox_cards(CFG, scan_id, sales_source="estimated") == []
    # A different marketplace filter drops it.
    assert services.inbox_cards(CFG, scan_id, marketplace="UK") == []


# ---------------------------------------------------------------------------
# Inbox actions
# ---------------------------------------------------------------------------
def test_shortlist_from_inbox_moves_out_of_inbox(initialized_db: Path) -> None:
    with get_connection() as conn:
        scan_id = _seed_scan(conn)
    services.shortlist_from_inbox(scan_id, "B0CC000001", "US")
    assert services.inbox_cards(CFG, scan_id) == []  # left the inbox
    with get_connection() as conn:
        short = repository.list_shortlist(conn)
    assert any(r["asin"] == "B0CC000001" for r in short)


def test_reject_from_inbox_records_reason_and_removes(initialized_db: Path) -> None:
    with get_connection() as conn:
        scan_id = _seed_scan(conn)
    services.reject_from_inbox(scan_id, "B0CC000001", "US", reason="too seasonal")
    assert services.inbox_cards(CFG, scan_id) == []
    with get_connection() as conn:
        rows = repository.get_scan_candidates(conn, scan_id)
    rejected = [r for r in rows if r["outcome"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "too seasonal"
