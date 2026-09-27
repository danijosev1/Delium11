"""Daily Scan pipeline (Part 2) — stage ordering, resume-after-crash, budget
abort, funnel counts, finder-level kill filters, inbox-vs-shortlist, AU message.

No network: Keepa + DataForSEO are routing fake transports over the real client
classes, so the whole 8-stage funnel runs against fixtures.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from delium.analysis.models import Confidence
from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.discovery import daily_scan
from delium.discovery.daily_scan import EnrichResult, ScanAbortedError, ScanClients, ScanParams
from delium.profile import store as profile_store
from delium.providers.base import HttpResult
from delium.providers.keepa import KeepaClient
from keepa_support import KM_DAY1, KM_DAY2, KM_DAY3

CFG = DeliumConfig()
AS_OF = date(2025, 8, 1)
FINDER_ASINS = ["B0SCAN00001", "B0SCAN00002", "B0SCAN00003"]


def _product(asin: str) -> dict[str, Any]:
    csv: list[Any] = [None] * 18
    csv[0] = [KM_DAY1, 4999, KM_DAY2, 5099, KM_DAY3, 4999]  # Amazon price ($49.99)
    csv[3] = [KM_DAY1, 1600, KM_DAY2, 1500, KM_DAY3, 1400]  # BSR improving
    csv[16] = [KM_DAY3, 44]  # rating 4.4
    csv[17] = [KM_DAY1, 60, KM_DAY3, 120]  # reviews accruing
    return {
        "asin": asin,
        "title": f"Silicone Gadget {asin}",
        "brand": "Acme",
        "categoryTree": [{"catId": 1, "name": "Home & Kitchen"}],
        "packageLength": 200,
        "packageWidth": 150,
        "packageHeight": 40,
        "packageWeight": 300,
        "imagesCSV": "a.jpg,b.jpg",
        "monthlySold": 300,
        "fbaFees": {"pickAndPackFee": 520},
        "csv": csv,
    }


class RoutingKeepa:
    """Fake Keepa transport: routes by URL. /query → finder asinList, /token →
    balance, /product → a synthesized product per requested ASIN."""

    def __init__(self, finder_asins: list[str]) -> None:
        self.finder_asins = finder_asins
        self.finder_params: list[dict[str, str]] = []
        self.product_calls = 0

    def request_json(self, url: str, params: Any) -> HttpResult:
        if "/query" in url:
            self.finder_params.append(dict(params))
            return HttpResult(
                200,
                {
                    "asinList": self.finder_asins,
                    "totalResults": len(self.finder_asins),
                    "tokensConsumed": 11,
                    "tokensLeft": 9000,
                },
            )
        if "/token" in url:
            return HttpResult(200, {"tokensLeft": 9000, "refillRate": 20})
        # /product — synthesize a product for each requested ASIN.
        self.product_calls += 1
        asins = str(params.get("asin", "")).split(",")
        return HttpResult(
            200,
            {
                "tokensConsumed": len(asins),
                "tokensLeft": 9000,
                "products": [_product(a) for a in asins if a],
            },
        )


def _keepa_factory(transport: RoutingKeepa):  # type: ignore[no-untyped-def]
    return lambda mp: KeepaClient("k", transport=transport, sleep=lambda _s: None, marketplace=mp)


def _profile(conn: Any):  # type: ignore[no-untyped-def]
    """A favorable profile so the fixture products clear the profit gate (G1) and
    reach TEST → become eligible finalists (kept in-band: price $30-60, ≤4kg)."""
    from delium.profile.models import CogsMode, ResearchProfile

    profile_store.ensure_seeded(conn)
    active = profile_store.load_active(conn)
    fav = ResearchProfile(
        id=active.id,
        name=active.name,
        is_active=True,
        budget_usd=15000,
        price_min_cents=3000,
        price_max_cents=6000,
        max_reviews=1000,
        max_weight_g=4000,
        cogs_mode=CogsMode.UNIT,
        cogs_value=3.0,  # $3/unit → healthy margin at $49.99
        freight_per_kg_usd=2.0,
    )
    profile_store.save(conn, fav)
    conn.commit()
    return profile_store.load_active(conn)


def _run(params: ScanParams, clients: ScanClients, resume: str | None = None):  # type: ignore[no-untyped-def]
    with get_connection() as conn:
        profile = _profile(conn)
        return daily_scan.run_scan(
            conn,
            params=params,
            profile=profile,
            config=CFG,
            clients=clients,
            confirm=None,
            as_of=AS_OF,
            resume_scan_id=resume,
        )


# ---------------------------------------------------------------------------
# Full funnel + stage ordering
# ---------------------------------------------------------------------------
def test_full_funnel_runs_all_stages_in_order(initialized_db: Path) -> None:
    transport = RoutingKeepa(FINDER_ASINS)
    clients = ScanClients(keepa_factory=_keepa_factory(transport))
    report = _run(ScanParams(marketplaces=("US",), sweep_target=50, top_n=3), clients)

    assert report.status == "complete"
    # Stages 0..8 all present, complete, and in order.
    stage_ids = [s["stage"] for s in report.stages]
    assert stage_ids == list(range(9))
    assert all(s["status"] in {"complete", "skipped"} for s in report.stages)
    # Funnel: swept → hydrated → scored → finalists.
    assert report.funnel["swept"] == 3
    assert report.funnel["hydrated"] == 3
    assert report.funnel["scored"] == 3
    assert report.funnel["finalists"] == 3
    assert report.keepa_tokens > 0  # tokens logged


def test_finalists_land_in_inbox_not_shortlist(initialized_db: Path) -> None:
    transport = RoutingKeepa(FINDER_ASINS)
    report = _run(
        ScanParams(marketplaces=("US",), top_n=3),
        ScanClients(keepa_factory=_keepa_factory(transport)),
    )
    assert len(report.finalists) == 3
    # No enricher → differentiation pending, never a fabricated score.
    assert all(f["differentiation"] == "pending" for f in report.finalists)
    with get_connection() as conn:
        inbox = repository.scan_inbox(conn, report.scan_id)
        shortlist = repository.list_shortlist(conn)
    assert {c["asin"] for c in inbox} == set(FINDER_ASINS)
    assert shortlist == []  # inbox is SEPARATE from the shortlist


# ---------------------------------------------------------------------------
# Finder-level kill filters
# ---------------------------------------------------------------------------
def test_finder_selection_carries_kill_filters(initialized_db: Path) -> None:
    transport = RoutingKeepa(FINDER_ASINS)
    _run(
        ScanParams(marketplaces=("US",)),
        ScanClients(keepa_factory=_keepa_factory(transport)),
    )
    assert transport.finder_params, "the sweep should call the finder"
    # Keepa encodes the finder filter as a JSON string under the "selection" param.
    import json

    sel = json.loads(transport.finder_params[0]["selection"])
    # Price band (K1/K2), review cap (K6), Amazon exclusion (K4), weight (K3).
    assert "current_NEW_gte" in sel and "current_NEW_lte" in sel
    assert "current_COUNT_REVIEWS_lte" in sel
    assert sel.get("buyBoxIsAmazon") is False
    assert "packageWeight_lte" in sel  # profile max weight pushed into the finder


# ---------------------------------------------------------------------------
# Budget abort (before any spend)
# ---------------------------------------------------------------------------
def test_budget_cap_aborts_before_spending(initialized_db: Path) -> None:
    transport = RoutingKeepa(FINDER_ASINS)
    clients = ScanClients(keepa_factory=_keepa_factory(transport))
    with pytest.raises(ScanAbortedError):
        _run(ScanParams(marketplaces=("US",), budget_cap_tokens=1), clients)
    # Nothing was swept (aborted in preflight).
    with get_connection() as conn:
        scans = repository.list_scans(conn)
        assert scans and scans[0]["status"] == "aborted"
        assert repository.get_scan_candidates(conn, scans[0]["id"]) == []
    assert transport.product_calls == 0  # never hydrated


def test_max_spend_aborts(initialized_db: Path) -> None:
    transport = RoutingKeepa(FINDER_ASINS)
    with pytest.raises(ScanAbortedError):
        _run(
            ScanParams(marketplaces=("US",), max_spend_usd=0.001),
            ScanClients(keepa_factory=_keepa_factory(transport)),
        )


# ---------------------------------------------------------------------------
# AU message
# ---------------------------------------------------------------------------
def test_au_is_skipped_with_message(initialized_db: Path) -> None:
    transport = RoutingKeepa(FINDER_ASINS)
    report = _run(
        ScanParams(marketplaces=("US", "AU"), top_n=3),
        ScanClients(keepa_factory=_keepa_factory(transport)),
    )
    assert any("AU pending" in n for n in report.notes)
    with get_connection() as conn:
        cands = repository.get_scan_candidates(conn, report.scan_id)
    assert all(c["marketplace"] != "AU" for c in cands)  # AU never scanned


# ---------------------------------------------------------------------------
# Resume after a crash
# ---------------------------------------------------------------------------
def test_resume_after_crash_completes(initialized_db: Path) -> None:
    transport = RoutingKeepa(FINDER_ASINS)

    calls = {"n": 0}

    def _boom(asin: str, mp: str, run_id: str) -> EnrichResult:
        calls["n"] += 1
        raise RuntimeError("simulated crash in finalists")

    crashing = ScanClients(keepa_factory=_keepa_factory(transport), enrich_finalist=_boom)
    with get_connection() as conn:
        profile = _profile(conn)
        with pytest.raises(RuntimeError):
            daily_scan.run_scan(
                conn,
                params=ScanParams(marketplaces=("US",), top_n=3),
                profile=profile,
                config=CFG,
                clients=crashing,
                confirm=None,
                as_of=AS_OF,
            )
        scans = repository.list_scans(conn)
    scan_id = scans[0]["id"]
    with get_connection() as conn:
        row = repository.get_scan(conn, scan_id)
        assert row["status"] == "failed"
        assert row["stage"] == 6  # stages 0-6 completed before the stage-7 crash

    # Resume with a working (no-op) enricher → completes stages 7-8.
    healthy = ScanClients(keepa_factory=_keepa_factory(transport))
    report = _run(ScanParams(marketplaces=("US",), top_n=3), healthy, resume=scan_id)
    assert report.status == "complete"
    assert report.funnel["finalists"] == 3
    # The competitor/scoring stages were NOT re-run from scratch (resume skipped them).


def test_resume_finished_scan_is_noop(initialized_db: Path) -> None:
    transport = RoutingKeepa(FINDER_ASINS)
    clients = ScanClients(keepa_factory=_keepa_factory(transport))
    report = _run(ScanParams(marketplaces=("US",), top_n=3), clients)
    product_calls_after_first = transport.product_calls
    again = _run(ScanParams(marketplaces=("US",), top_n=3), clients, resume=report.scan_id)
    assert again.status == "complete"
    assert transport.product_calls == product_calls_after_first  # no re-hydration


# ---------------------------------------------------------------------------
# min-confidence gate
# ---------------------------------------------------------------------------
def test_min_confidence_high_filters_finalists(initialized_db: Path) -> None:
    # With no competitor set + no differentiation, sellability confidence is low,
    # so a HIGH floor should admit no finalists.
    transport = RoutingKeepa(FINDER_ASINS)
    report = _run(
        ScanParams(marketplaces=("US",), top_n=3, min_confidence=Confidence.HIGH),
        ScanClients(keepa_factory=_keepa_factory(transport)),
    )
    assert report.funnel["finalists"] == 0
    assert report.status == "complete"
