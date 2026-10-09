"""Zombie-listing detection — pure engine, finder selection, compliance routing,
and the Keepa-fixture orchestration (including a gzip round-trip).

No network: a routing fake transport drives the real KeepaClient over product
bodies whose `csv` carries long -1 (out-of-stock) gaps and restock patterns.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from delium.analysis.zombies import (
    ZombieEvidence,
    ZombieFinderConfig,
    ZombieVerdict,
    build_stock_timeline,
    build_zombie_finder_selection,
    compliance_flags,
    compute_zombie,
    load_zombie_data,
)
from delium.database import get_connection
from delium.providers.base import HttpResult
from delium.providers.keepa import KeepaClient

AS_OF = date(2026, 10, 1)
_EPOCH = 21564000


def _km(d: date) -> int:
    return (d - date(1970, 1, 1)).days * 1440 - _EPOCH


def _changes(*pairs: tuple[date, int]) -> list[tuple[int, int]]:
    return [(_km(d), v) for d, v in pairs]


# ---------------------------------------------------------------------------
# Stock timeline (pure)
# ---------------------------------------------------------------------------
def test_continuously_out_of_stock_duration() -> None:
    # In stock through 2023-01, then no offer (-1) ever since.
    new = _changes((date(2020, 1, 1), 2999), (date(2023, 1, 1), -1))
    tl = build_stock_timeline(new, [], [], as_of=AS_OF)
    assert tl.observed is True
    assert tl.currently_out_of_stock is True
    assert tl.last_offer_date == date(2023, 1, 1)
    # ~45 months between 2023-01-01 and 2026-10-01 (1369 days / 30).
    assert tl.months_out_of_stock is not None and 44 <= tl.months_out_of_stock <= 46
    assert tl.restock_gaps == 0


def test_no_gap_when_currently_in_stock() -> None:
    new = _changes((date(2020, 1, 1), -1), (date(2024, 1, 1), 2999))
    tl = build_stock_timeline(new, [], [], as_of=AS_OF)
    assert tl.currently_out_of_stock is False
    assert tl.days_out_of_stock == 0
    assert tl.last_offer_date is None
    assert tl.restock_gaps == 1  # one out→in resurrection historically


def test_resurrection_gaps_counted() -> None:
    new = _changes(
        (date(2019, 1, 1), 2999),
        (date(2020, 1, 1), -1),
        (date(2020, 6, 1), 2999),  # restock 1
        (date(2021, 1, 1), -1),
        (date(2021, 6, 1), 2999),  # restock 2
        (date(2022, 1, 1), -1),  # final, still dead
    )
    tl = build_stock_timeline(new, [], [], as_of=AS_OF)
    assert tl.restock_gaps == 2
    assert tl.currently_out_of_stock is True


def test_offer_count_fallback_when_no_new_series() -> None:
    # No NEW price series; offer-count (0 == none) drives availability.
    count_new = _changes((date(2021, 1, 1), 3), (date(2023, 1, 1), 0))
    tl = build_stock_timeline([], count_new, [], as_of=AS_OF)
    assert tl.currently_out_of_stock is True
    assert tl.last_offer_date == date(2023, 1, 1)


def test_in_stock_bsr_uses_only_in_stock_intervals() -> None:
    new = _changes((date(2020, 1, 1), 2999), (date(2022, 1, 1), -1))
    sales = _changes(
        (date(2020, 6, 1), 4000),  # in stock
        (date(2021, 6, 1), 6000),  # in stock
        (date(2023, 1, 1), 400000),  # AFTER it died — must be ignored
    )
    tl = build_stock_timeline(new, [], sales, as_of=AS_OF)
    assert tl.in_stock_bsr_best == 4000
    assert tl.in_stock_bsr_median == 5000


def test_no_history_is_unobserved() -> None:
    tl = build_stock_timeline([], [], [], as_of=AS_OF)
    assert tl.observed is False
    assert tl.days_out_of_stock is None


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------
def _dead_timeline(years_dead: int = 3) -> Any:
    new = _changes((date(2020 - years_dead, 1, 1), 2999), (date(2026 - years_dead, 1, 1), -1))
    sales = _changes((date(2020 - years_dead, 6, 1), 4000))
    return build_stock_timeline(new, [], sales, as_of=AS_OF)


def test_verified_zombie() -> None:
    t = load_zombie_data("uk")
    ev = ZombieEvidence(
        asin="B0DEAD001",
        marketplace="UK",
        timeline=_dead_timeline(3),
        rating=4.6,
        reviews=900,
        brand="Generic",
        amazon_on_listing=False,
    )
    r = compute_zombie(ev, t)
    assert r.verdict is ZombieVerdict.VERIFIED
    assert r.confidence.value == "high"
    assert r.resurrection_risk == 0.0


def test_possibly_temporary_when_too_fresh() -> None:
    t = load_zombie_data("uk")
    new = _changes((date(2024, 1, 1), 2999), (date(2026, 7, 1), -1))  # ~3 months dead
    tl = build_stock_timeline(new, [], _changes((date(2025, 1, 1), 4000)), as_of=AS_OF)
    ev = ZombieEvidence(asin="B0FRESH01", marketplace="UK", timeline=tl, rating=4.5, reviews=500)
    r = compute_zombie(ev, t)
    assert r.verdict is ZombieVerdict.POSSIBLY_TEMPORARY


def test_possibly_temporary_on_high_resurrection_risk() -> None:
    t = load_zombie_data("uk")
    # Three restocks → risk 75 (>= high_risk 60) → capped at possibly temporary.
    new = _changes(
        (date(2017, 1, 1), 2999),
        (date(2018, 1, 1), -1),
        (date(2018, 6, 1), 2999),
        (date(2019, 1, 1), -1),
        (date(2019, 6, 1), 2999),
        (date(2020, 1, 1), -1),
        (date(2020, 6, 1), 2999),
        (date(2021, 1, 1), -1),  # final, dead 5+ years
    )
    tl = build_stock_timeline(new, [], _changes((date(2017, 6, 1), 4000)), as_of=AS_OF)
    ev = ZombieEvidence(asin="B0RESUR01", marketplace="UK", timeline=tl, rating=4.6, reviews=900)
    r = compute_zombie(ev, t)
    assert tl.restock_gaps == 3
    assert r.resurrection_risk is not None and r.resurrection_risk >= 60
    assert r.verdict is ZombieVerdict.POSSIBLY_TEMPORARY


def test_not_a_zombie_when_in_stock() -> None:
    t = load_zombie_data("uk")
    new = _changes((date(2020, 1, 1), -1), (date(2025, 1, 1), 2999))  # back in stock
    tl = build_stock_timeline(new, [], _changes((date(2025, 6, 1), 4000)), as_of=AS_OF)
    ev = ZombieEvidence(
        asin="B0LIVE001",
        marketplace="UK",
        timeline=tl,
        rating=4.6,
        reviews=900,
        has_current_new_offer=True,
    )
    r = compute_zombie(ev, t)
    assert r.verdict is ZombieVerdict.NOT_A_ZOMBIE


def test_not_a_zombie_on_weak_social_proof() -> None:
    t = load_zombie_data("uk")
    ev = ZombieEvidence(
        asin="B0WEAK001",
        marketplace="UK",
        timeline=_dead_timeline(3),
        rating=3.2,
        reviews=900,  # rating below the 4.0 floor
    )
    r = compute_zombie(ev, t)
    assert r.verdict is ZombieVerdict.NOT_A_ZOMBIE


def test_missing_data_caps_at_possibly_temporary_never_verified() -> None:
    t = load_zombie_data("uk")
    # Long dead, but NO rating/reviews and NO in-stock BSR → unknowns.
    new = _changes((date(2019, 1, 1), 2999), (date(2021, 1, 1), -1))
    tl = build_stock_timeline(new, [], [], as_of=AS_OF)
    ev = ZombieEvidence(asin="B0UNK0001", marketplace="UK", timeline=tl)
    r = compute_zombie(ev, t)
    assert r.verdict is ZombieVerdict.POSSIBLY_TEMPORARY  # never VERIFIED on unknowns
    assert r.confidence.value == "low"
    assert set(r.missing) >= {"social_proof", "past_demand"}


# ---------------------------------------------------------------------------
# Compliance routing
# ---------------------------------------------------------------------------
def test_generic_brand_offers_revival_route() -> None:
    cf = compliance_flags(None, "UK")
    assert cf.generic is True
    assert cf.brand_label == "generic/unbranded"
    assert any("Launch your own" in r for r in cf.routes)
    assert any("Possible revival" in r for r in cf.routes)
    assert "UK IPO" in cf.manual_check


def test_named_brand_has_no_revival_route() -> None:
    cf = compliance_flags("Acme", "CA")
    assert cf.generic is False
    assert cf.brand_label == "Acme"
    assert any("Launch your own" in r for r in cf.routes)
    assert all("Possible revival" not in r for r in cf.routes)
    assert any("No revival" in r for r in cf.routes)
    assert "CIPO" in cf.manual_check


def test_manual_check_names_us_office() -> None:
    assert "USPTO" in compliance_flags("x", "US").manual_check


# ---------------------------------------------------------------------------
# Finder selection (fields verified against keepacom/api_backend)
# ---------------------------------------------------------------------------
def test_zombie_finder_selection_fields() -> None:
    sel = build_zombie_finder_selection(
        ZombieFinderConfig(min_rating=4.0, min_reviews=40, out_of_stock_pct_90=100),
        category_ids=[3760901],
    )
    assert sel["current_RATING_gte"] == 40  # 4.0★ on Keepa's 0-50 scale
    assert sel["current_COUNT_REVIEWS_gte"] == 40
    assert sel["outOfStockPercentage90_NEW_gte"] == 100  # the dead prefilter
    assert sel["outOfStockPercentage90_gte"] == 90  # AMAZON OOS — excludes Amazon-sold
    assert sel["categories_include"] == [3760901]  # per-marketplace category slice
    assert sel["productType"] == [0]
    assert sel["sort"] == [["current_COUNT_REVIEWS", "desc"]]
    # current_COUNT_NEW_lte removed: live Keepa /query returned 0 for dead listings.
    assert "current_COUNT_NEW_lte" not in sel
    # buyBoxIsAmazon=false would exclude listings with NO buy box (every dead one).
    assert "buyBoxIsAmazon" not in sel
    assert "current_NEW_gte" not in sel  # no price band — dead listings have no price


def test_zombie_finder_core_only_keeps_dead_prefilter_drops_amazon() -> None:
    sel = build_zombie_finder_selection(
        ZombieFinderConfig(min_rating=4.0, min_reviews=40, out_of_stock_pct_90=100),
        core_only=True,
    )
    # The NEW dead-prefilter is CORE (it works); the Amazon-exclusion is dropped.
    assert sel["outOfStockPercentage90_NEW_gte"] == 100
    assert "outOfStockPercentage90_gte" not in sel
    assert "current_COUNT_NEW_lte" not in sel
    assert "buyBoxIsAmazon" not in sel


def test_zombie_finder_amazon_exclusion_field_is_unprefixed_oos() -> None:
    # The Amazon-exclusion prefilter is the UNPREFIXED outOfStockPercentage90_gte
    # (the Amazon series; outOfStockPercentage90_AMAZON_* does not exist in Keepa).
    on = build_zombie_finder_selection(ZombieFinderConfig(amazon_oos_pct_90=90))
    assert on["outOfStockPercentage90_gte"] == 90
    off = build_zombie_finder_selection(ZombieFinderConfig(amazon_oos_pct_90=0))
    assert "outOfStockPercentage90_gte" not in off


def test_zombie_finder_defaults_come_from_zombie_data() -> None:
    from delium.analysis.zombies import load_zombie_data

    t = load_zombie_data("uk")
    assert t.finder_oos_pct_90 == 100
    assert t.finder_amazon_oos_pct_90 == 90


# ---------------------------------------------------------------------------
# Orchestration over Keepa fixtures (fake transport)
# ---------------------------------------------------------------------------
def _zombie_csv(
    new: list[tuple[int, int]], sales: list[tuple[int, int]], rating: int, reviews: int
) -> list[Any]:
    csv: list[Any] = [None] * 18
    csv[0] = [new[-1][0], -1]  # Amazon: no offer
    csv[1] = [x for pair in new for x in pair]  # NEW price series (with -1 gaps)
    if sales:
        csv[3] = [x for pair in sales for x in pair]
    csv[16] = [new[0][0], rating]
    csv[17] = [new[0][0], reviews]
    return csv


def _zombie_body(asin: str, *, brand: str | None, dead_since: date) -> dict[str, Any]:
    new = _changes((date(2019, 1, 1), 2999), (dead_since, -1))
    sales = _changes((date(2019, 6, 1), 4000), (date(2020, 6, 1), 5000))
    prod: dict[str, Any] = {
        "asin": asin,
        "title": "Reusable Silicone Baking Mat",
        "csv": _zombie_csv(new, sales, rating=46, reviews=820),
    }
    if brand is not None:
        prod["brand"] = brand
    return prod


class FakeZombieKeepa:
    """Routes /query → finder asinList, /token → balance, /product → zombie bodies."""

    def __init__(self, bodies: dict[str, dict[str, Any]], *, tokens_left: int = 9000) -> None:
        self.bodies = bodies
        self.tokens_left = tokens_left
        self.finder_params: list[dict[str, str]] = []

    def request_json(self, url: str, params: Any) -> HttpResult:
        if "/query" in url:
            self.finder_params.append(dict(params))
            return HttpResult(
                200,
                {
                    "asinList": list(self.bodies),
                    "totalResults": len(self.bodies),
                    "tokensConsumed": 11,
                    "tokensLeft": self.tokens_left,
                },
            )
        if "/token" in url:
            return HttpResult(200, {"tokensLeft": self.tokens_left, "refillRate": 20})
        asins = str(params.get("asin", "")).split(",")
        return HttpResult(
            200,
            {
                "tokensConsumed": len(asins),
                "tokensLeft": self.tokens_left,
                "products": [self.bodies[a] for a in asins if a in self.bodies],
            },
        )


def _factory(transport: FakeZombieKeepa):  # type: ignore[no-untyped-def]
    return lambda mp: KeepaClient("k", transport=transport, sleep=lambda _s: None, marketplace=mp)


def test_run_zombies_end_to_end_verifies_and_flags(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    bodies = {
        "B0GEN00001": _zombie_body("B0GEN00001", brand=None, dead_since=date(2023, 1, 1)),
        "B0BRND0002": _zombie_body("B0BRND0002", brand="Acme", dead_since=date(2023, 1, 1)),
    }
    transport = FakeZombieKeepa(bodies)
    with get_connection() as conn:
        report = zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(marketplaces=("UK",), sweep_target=10, top_n=10),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(keepa_factory=_factory(transport)),
            as_of=AS_OF,
        )
    assert report.swept == 2
    assert report.hydrated == 2
    assert report.keepa_tokens > 0
    verdicts = {r.asin: r.verdict for r in report.results}
    assert verdicts["B0GEN00001"] is ZombieVerdict.VERIFIED
    # The finder query carried the verified zombie fields.
    sel = transport.finder_params[0]
    import json as _json

    decoded = _json.loads(sel["selection"])
    assert decoded["outOfStockPercentage90_NEW_gte"] == 100  # the dead prefilter
    assert "current_COUNT_NEW_lte" not in decoded  # removed (matched 0 dead listings)
    # Compliance: the generic listing offers a revival route; the branded one does not.
    gen = next(r for r in report.results if r.asin == "B0GEN00001")
    brand = next(r for r in report.results if r.asin == "B0BRND0002")
    assert gen.compliance.generic is True
    assert brand.compliance.generic is False
    assert any("Possible revival" in x for x in gen.compliance.routes)
    assert all("Possible revival" not in x for x in brand.compliance.routes)


def test_run_zombies_aborts_over_token_cap(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    transport = FakeZombieKeepa(
        {"B0X": _zombie_body("B0X", brand=None, dead_since=date(2023, 1, 1))}
    )
    with get_connection() as conn, pytest.raises(zmod.ZombieAbortedError):
        zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(marketplaces=("UK",), budget_cap_tokens=1),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(keepa_factory=_factory(transport)),
            as_of=AS_OF,
        )


def test_run_zombies_skips_australia(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    transport = FakeZombieKeepa(
        {"B0GEN00001": _zombie_body("B0GEN00001", brand=None, dead_since=date(2023, 1, 1))}
    )
    with get_connection() as conn:
        report = zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(marketplaces=("UK", "AU"), sweep_target=10),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(keepa_factory=_factory(transport)),
            as_of=AS_OF,
        )
    assert any("AU pending" in n for n in report.notes)


def test_zombie_body_decodes_from_gzip_and_computes_oos(monkeypatch: pytest.MonkeyPatch) -> None:
    # Realistic gzip Keepa fixture end-to-end: gzip the body, decode via the real
    # transport, build the timeline, confirm the long -1 gap is detected.
    import email.message
    import gzip
    import json
    import urllib.request

    from delium.discovery.zombies import zombie_evidence_from_raw
    from delium.providers.base import UrllibTransport

    body_dict = {
        "tokensLeft": 500,
        "refillRate": 20,
        "tokensConsumed": 2,
        "products": [_zombie_body("B0GZIP001", brand=None, dead_since=date(2023, 1, 1))],
    }
    body = gzip.compress(json.dumps(body_dict).encode("utf-8"))

    class _Resp:
        status = 200

        def __init__(self) -> None:
            self.headers = email.message.Message()
            self.headers["Content-Encoding"] = "gzip"

        def read(self) -> bytes:
            return body

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    client = KeepaClient("test-key", transport=UrllibTransport(), sleep=lambda _: None)
    fetch = client.fetch_product("B0GZIP001")
    raw = fetch.raw_products["B0GZIP001"]
    ev = zombie_evidence_from_raw(raw, asin="B0GZIP001", marketplace="UK", as_of=AS_OF)
    assert ev.timeline.currently_out_of_stock is True
    assert ev.timeline.last_offer_date == date(2023, 1, 1)
    assert ev.rating == 4.6 and ev.reviews == 820
    assert compute_zombie(ev, load_zombie_data("uk")).verdict is ZombieVerdict.VERIFIED


# ---------------------------------------------------------------------------
# BUGFIX: no buyBoxIsAmazon; post-hydration Amazon exclusion; empty diagnostics
# ---------------------------------------------------------------------------
def _amazon_sold_body(asin: str) -> dict[str, Any]:
    """A reviewed listing with NO new offers but Amazon currently selling (the
    Amazon price series ends with a live price) — must be excluded as a zombie."""
    new = _changes((date(2019, 1, 1), 2999), (date(2023, 1, 1), -1))
    csv = _zombie_csv(new, _changes((date(2019, 6, 1), 4000)), rating=46, reviews=820)
    csv[0] = [_km(date(2024, 1, 1)), 2500]  # Amazon in stock NOW
    return {"asin": asin, "title": "Silicone Mat", "brand": None, "csv": csv}


class SelectionRoutedKeepa:
    """Routes /query by what the selection contains, so a test can make the full
    selection return 0 and the core-only fallback return results (or vice-versa)."""

    def __init__(
        self,
        *,
        full: dict[str, dict[str, Any]] | None = None,
        core: dict[str, dict[str, Any]] | None = None,
        total_results_full: int = 0,
        error_status: int | None = None,
    ) -> None:
        self.full = full or {}
        self.core = core or {}
        self.total_results_full = total_results_full
        self.error_status = error_status
        self.queries: list[dict[str, Any]] = []
        self.tokens_left = 9000

    def request_json(self, url: str, params: Any) -> HttpResult:
        if "/query" in url:
            import json as _json

            sel = _json.loads(params["selection"])
            self.queries.append(sel)
            if self.error_status is not None:
                return HttpResult(
                    self.error_status,
                    {
                        "error": {"type": "invalidParameter", "message": "bad filter"},
                        "tokensLeft": self.tokens_left,
                    },
                )
            # The optional (droppable) filter is the Amazon-exclusion OOS%; the
            # core-only fallback omits it.
            has_amazon = "outOfStockPercentage90_gte" in sel
            bodies = self.full if has_amazon else self.core
            total = self.total_results_full if has_amazon else len(bodies)
            return HttpResult(
                200,
                {
                    "asinList": list(bodies),
                    "totalResults": total,
                    "tokensConsumed": 11,
                    "tokensLeft": self.tokens_left,
                },
            )
        if "/token" in url:
            return HttpResult(200, {"tokensLeft": self.tokens_left, "refillRate": 20})
        asins = str(params.get("asin", "")).split(",")
        pool = {**self.full, **self.core}
        return HttpResult(
            200,
            {
                "tokensConsumed": len(asins),
                "tokensLeft": self.tokens_left,
                "products": [pool[a] for a in asins if a in pool],
            },
        )


def _routed_factory(transport: SelectionRoutedKeepa):  # type: ignore[no-untyped-def]
    return lambda mp: KeepaClient("k", transport=transport, sleep=lambda _s: None, marketplace=mp)


def test_amazon_sold_listing_excluded_after_hydration(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    bodies = {"B0AMZ0001": _amazon_sold_body("B0AMZ0001")}
    transport = FakeZombieKeepa(bodies)
    with get_connection() as conn:
        report = zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(marketplaces=("UK",), sweep_target=10, top_n=10),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(keepa_factory=_factory(transport)),
            as_of=AS_OF,
        )
    # It is swept + hydrated (finder can't filter Amazon-with-no-buybox), but the
    # verdict excludes it because Amazon is selling now.
    assert report.hydrated == 1
    r = report.results[0]
    assert r.evidence.amazon_on_listing is True
    assert r.verdict is ZombieVerdict.NOT_A_ZOMBIE


def test_empty_result_records_finder_diagnostics_and_counts_tokens(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    transport = SelectionRoutedKeepa(full={}, core={}, total_results_full=0)
    with get_connection() as conn:
        report = zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(marketplaces=("CA", "US"), sweep_target=10, top_n=10),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(keepa_factory=_routed_factory(transport)),
            as_of=AS_OF,
        )
    assert report.results == []
    # Finder tokens are counted even though 0 were swept (the "0 tokens" bug).
    assert report.keepa_tokens > 0
    # One diagnostic per active marketplace, each with the filters + total_results.
    diags = {d.marketplace: d for d in report.diagnostics}
    assert set(diags) == {"CA", "US"}
    ca = diags["CA"]
    assert ca.returned == 0
    assert ca.total_results is None  # Keepa's 0 maps to None; the summary says "unknown"
    assert ca.used_fallback is True  # retried core-only after the full selection was empty
    # The fallback keeps the NEW dead-prefilter and drops the Amazon-exclusion filter.
    assert "outOfStockPercentage90_NEW_gte" in ca.filters
    assert "outOfStockPercentage90_gte" not in ca.filters
    assert "current_COUNT_NEW_lte" not in ca.filters
    assert "Filters sent" in ca.summary()


def test_fallback_without_oos_filter_recovers_results(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    # Full selection (with the Amazon-exclusion outOfStockPercentage90_gte) returns
    # nothing; the core-only fallback (Amazon filter dropped) finds the zombie.
    body = {"B0CORE001": _zombie_body("B0CORE001", brand=None, dead_since=date(2023, 1, 1))}
    transport = SelectionRoutedKeepa(full={}, core=body, total_results_full=0)
    with get_connection() as conn:
        report = zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(marketplaces=("UK",), sweep_target=10, top_n=10),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(keepa_factory=_routed_factory(transport)),
            as_of=AS_OF,
        )
    assert report.hydrated == 1
    assert report.diagnostics[0].used_fallback is True
    assert report.results[0].asin == "B0CORE001"


def test_finder_http_error_surfaced_in_diagnostics(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    transport = SelectionRoutedKeepa(error_status=400)
    with get_connection() as conn:
        report = zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(marketplaces=("UK",), sweep_target=10, top_n=10),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(keepa_factory=_routed_factory(transport)),
            as_of=AS_OF,
        )
    assert report.results == []
    diag = report.diagnostics[0]
    assert diag.error is not None
    assert "HTTP 400" in diag.error  # Keepa status surfaced
    assert "k" not in diag.error.split()  # the API key is never in the message
    assert "failed" in diag.summary().lower()


def test_missing_keepa_key_records_skip_reason(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    with get_connection() as conn:
        report = zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(marketplaces=("UK", "CA"), sweep_target=10),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(keepa_factory=None),
            as_of=AS_OF,
        )
    assert report.results == []
    assert {d.marketplace for d in report.diagnostics} == {"UK", "CA"}
    assert all(
        d.skipped_reason and "not configured" in d.skipped_reason for d in report.diagnostics
    )


class PagedKeepa:
    """A finder that returns a different ASIN per page, so --pages can be tested."""

    def __init__(self, per_page_asins: list[str]) -> None:
        self.per_page_asins = per_page_asins
        self.pages_seen: list[int] = []

    def request_json(self, url: str, params: Any) -> HttpResult:
        if "/query" in url:
            import json as _json

            sel = _json.loads(params["selection"])
            page = int(sel.get("page", 0))
            self.pages_seen.append(page)
            asins = [self.per_page_asins[page]] if page < len(self.per_page_asins) else []
            return HttpResult(
                200,
                {
                    "asinList": asins,
                    "totalResults": len(self.per_page_asins),
                    "tokensConsumed": 11,
                    "tokensLeft": 9000,
                },
            )
        if "/token" in url:
            return HttpResult(200, {"tokensLeft": 9000, "refillRate": 20})
        asins = str(params.get("asin", "")).split(",")
        return HttpResult(
            200,
            {
                "tokensConsumed": len(asins),
                "tokensLeft": 9000,
                "products": [
                    _zombie_body(a, brand=None, dead_since=date(2023, 1, 1)) for a in asins if a
                ],
            },
        )


def test_pages_option_pulls_multiple_finder_pages(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    transport = PagedKeepa(["B0PAGE0001", "B0PAGE0002"])
    with get_connection() as conn:
        report = zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(marketplaces=("UK",), pages=2, sweep_target=10, top_n=10),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(
                keepa_factory=lambda mp: KeepaClient(
                    "k", transport=transport, sleep=lambda _s: None, marketplace=mp
                )
            ),
            as_of=AS_OF,
        )
    assert transport.pages_seen == [0, 1]  # both pages fetched
    assert report.swept == 2
    assert {r.asin for r in report.results} == {"B0PAGE0001", "B0PAGE0002"}


def test_zombie_paging_capped_by_depth_limit(initialized_db: Path) -> None:
    # perPage 10000 ⇒ only 1 page fits the 10,000-result depth cap, so a request
    # for 5 pages is clamped to 1 (never 400s on an over-depth page).
    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    transport = PagedKeepa(["B0DEPTH001", "B0DEPTH002", "B0DEPTH003"])
    with get_connection() as conn:
        zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(
                marketplaces=("UK",), pages=5, per_page=10000, sweep_target=10, top_n=10
            ),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(
                keepa_factory=lambda mp: KeepaClient(
                    "k", transport=transport, sleep=lambda _s: None, marketplace=mp
                )
            ),
            as_of=AS_OF,
        )
    assert transport.pages_seen == [0]  # capped to a single page by the depth limit


def test_category_ids_sliced_into_finder_selection(initialized_db: Path) -> None:
    import json as _json

    from delium.config.models import DeliumConfig
    from delium.discovery import zombies as zmod

    bodies = {"B0CAT0001": _zombie_body("B0CAT0001", brand=None, dead_since=date(2023, 1, 1))}
    transport = FakeZombieKeepa(bodies)
    with get_connection() as conn:
        zmod.run_zombies(
            conn,
            params=zmod.ZombieParams(
                marketplaces=("UK",), category_ids={"UK": [3760911]}, sweep_target=10
            ),
            config=DeliumConfig(),
            clients=zmod.ZombieClients(keepa_factory=_factory(transport)),
            as_of=AS_OF,
        )
    sel = _json.loads(transport.finder_params[0]["selection"])
    assert sel["categories_include"] == [3760911]  # per-marketplace category slice
