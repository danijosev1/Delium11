"""Deterministic validation pipeline tests.

Covers target resolution, cache-first hydration + cost control, review →
differentiation integration (with the integrity rules), listing quality,
hard-kill-first (no wasted review spend), scoring.py as the sole verdict owner,
G5 kept pending, persistence + discovery-provenance upgrade, marketplace
isolation, error taxonomy, and determinism.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import discovery_support as seed
import validation_support as vs
from delium.analysis.models import Marketplace, Verdict
from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.providers import ProviderError
from delium.validation import (
    Clients,
    ValidationRequest,
    ValidationStatus,
    run_validation,
    validation_snapshot,
)

CFG = DeliumConfig()
US, AU = Marketplace.US, Marketplace.AU
SEED = seed.SEED

# Real ASINs are 10 alphanumeric chars; the target resolver requires that shape
# (shorter tokens are treated as keywords), so tests use realistic ASINs.
TGT = "B0TARGET01"
C1, C2, C3 = "B0COMPET01", "B0COMPET02", "B0COMPET03"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _seed_niche(
    conn, run_id, *, target=TGT, comps=(C1, C2, C3), price=2200, marketplace="US"
) -> None:
    asins = (target, *comps)
    seed.seed_keyword_market(conn, run_id, marketplace, asins=asins, price_cents=price)


def _pillar(scored, name):  # type: ignore[no-untyped-def]
    return next(p for p in scored.pillars if p.pillar == name)


def _validate(target, *, marketplace=US, clients=None, run_id=None, **kw):  # type: ignore[no-untyped-def]
    # insert_run commits on its own connection first (as the CLI does), so the
    # hydration connection never contends with an open write transaction.
    with get_connection() as conn:
        rid = run_id or repository.insert_run(conn, command="validate", input_=target)
    request = ValidationRequest(target=target, marketplace=marketplace, run_id=rid, **kw)
    with get_connection() as conn:
        return run_validation(conn, request, CFG, clients or Clients())


# ===========================================================================
# Target resolution (ASIN | URL | keyword)
# ===========================================================================
def test_resolves_bare_asin(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
    report = _validate(TGT)
    assert report.asin == TGT
    assert report.status is ValidationStatus.SCORED


def test_resolves_amazon_url(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid, target="B0ABCDEFGH")
        vs.seed_reviews(conn, rid, "B0ABCDEFGH", n=20)
    report = _validate("https://www.amazon.com/dp/B0ABCDEFGH?th=1")
    assert report.asin == "B0ABCDEFGH"


def test_resolves_keyword_to_top_organic(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid, target="TOP1", comps=("O2", "O3"))
    report = _validate(SEED)  # keyword → SERP → top organic
    assert report.asin == "TOP1"


def test_blank_target_is_invalid(initialized_db: Path) -> None:
    report = _validate("   ")
    assert report.status is ValidationStatus.INVALID_TARGET
    assert report.scored is None


def test_keyword_without_serp_is_insufficient(initialized_db: Path) -> None:
    report = _validate("no such niche here")  # no cached SERP, no dfs client
    assert report.status is ValidationStatus.INSUFFICIENT_DATA


# ===========================================================================
# Product hydration + error taxonomy
# ===========================================================================
def test_missing_product_no_provider_is_missing_credentials(initialized_db: Path) -> None:
    report = _validate("B0GHOST001")  # never fetched, no Keepa client
    assert report.status is ValidationStatus.MISSING_CREDENTIALS
    assert report.scored is None


def test_product_not_found_via_provider(initialized_db: Path) -> None:
    from keepa_support import keepa_product_body, ok

    factory = vs.SpyKeepaFactory([ok(keepa_product_body("B0MISSING1", found=False))])
    report = _validate("B0MISSING1", clients=Clients(keepa=factory))
    assert report.status is ValidationStatus.PRODUCT_NOT_FOUND


def test_provider_error_is_distinct(initialized_db: Path) -> None:
    class _RaisingKeepa:
        marketplace = "US"

        def fetch_products(self, asins: list[str]) -> object:
            raise ProviderError("boom")

    report = _validate("B0RAISE001", clients=Clients(keepa=lambda _mp: _RaisingKeepa()))
    assert report.status is ValidationStatus.PROVIDER_ERROR


# ===========================================================================
# Cache / API-cost control (docs §16)
# ===========================================================================
def test_fresh_review_cache_makes_no_provider_call(initialized_db: Path) -> None:
    provider = vs.FakeReviewProvider()
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        for asin in (TGT, C1, C2, C3):
            vs.seed_reviews(conn, rid, asin, n=30)  # all fresh
    report = _validate(TGT, clients=Clients(reviews=provider))
    assert report.status is ValidationStatus.SCORED
    assert provider.calls == []  # every review cache is fresh → provider untouched


def test_stale_reviews_trigger_provider_fetch(initialized_db: Path) -> None:
    provider = vs.FakeReviewProvider()
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        vs.seed_reviews(conn, rid, TGT, n=30, fresh=False)  # aged out
    _validate(TGT, clients=Clients(reviews=provider))
    assert TGT in provider.calls  # stale cache refetched


def test_fresh_product_cache_makes_no_keepa_call(initialized_db: Path) -> None:
    factory = vs.SpyKeepaFactory([])
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)  # products seeded fresh
    _validate(TGT, clients=Clients(keepa=factory))
    assert factory.call_count == 0  # everything fresh in cache


def test_marketplace_a_cache_never_satisfies_b(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid, marketplace="US")
    report = _validate(TGT, marketplace=AU)  # only US is cached, no provider
    assert report.status is ValidationStatus.MISSING_CREDENTIALS


# ===========================================================================
# Review → differentiation integration (integrity rules, docs §4)
# ===========================================================================
def test_differentiation_uses_real_review_sample(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        vs.seed_reviews(conn, rid, TGT, n=48)
    report = _validate(TGT)
    assert report.review_evidence is not None
    assert report.review_evidence.sample_size == 48
    assert report.review_evidence.miner_pending is True
    diff = _pillar(report.scored, "differentiation")
    assert diff.available is True  # a real report, not absent


def test_no_reviews_leaves_differentiation_absent(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)  # no reviews seeded
    report = _validate(TGT)
    assert report.review_evidence is None
    diff = _pillar(report.scored, "differentiation")
    assert diff.available is False  # absent, never faked


def test_persisted_themes_feed_verified_frequency(initialized_db: Path) -> None:
    from delium.validation.evidence import build_differentiation

    with get_connection() as conn:
        rid = seed.new_run(conn)
        ids = vs.seed_reviews(conn, rid, TGT, n=40, stars=(1, 2, 1, 2, 1))
        # Cite 8 real ids + duplicates + a nonexistent id.
        cited = ids[:8] + ids[:8] + ["GHOST-1"]
        vs.seed_review_theme(conn, rid, TGT, quote_review_ids=cited, frequency_pct=99.0, severity=3)
    with get_connection() as conn:
        report, _evidence = build_differentiation(conn, TGT, CFG)
    theme = report.themes[0]
    # Duplicates collapse and the ghost id is dropped → 8 verified, not 17.
    assert theme.verified_count == 8
    # Recomputed frequency ignores the claimed 99% → 8/40 = 20%.
    assert theme.frequency == pytest.approx(0.20)


def test_claimed_percent_cannot_inflate_frequency(initialized_db: Path) -> None:
    from delium.validation.evidence import build_differentiation

    with get_connection() as conn:
        rid = seed.new_run(conn)
        ids = vs.seed_reviews(conn, rid, TGT, n=50, stars=(1, 2, 3, 4, 5))
        vs.seed_review_theme(
            conn, rid, TGT, quote_review_ids=ids[:5], frequency_pct=100.0, severity=3
        )
    with get_connection() as conn:
        report, _ = build_differentiation(conn, TGT, CFG)
    assert report.themes[0].frequency <= 5 / 50 + 1e-9  # never the claimed 100%


def test_missing_review_evidence_lowers_confidence(initialized_db: Path) -> None:
    from delium.validation.evidence import build_differentiation

    with get_connection() as conn:
        rid = seed.new_run(conn)
        vs.seed_reviews(conn, rid, "SMALL", n=10)  # below every sample band
    with get_connection() as conn:
        report, evidence = build_differentiation(conn, "SMALL", CFG)
    assert report.confidence.level.value == "low"
    assert evidence.sample_size == 10


# ===========================================================================
# Listing quality → competition
# ===========================================================================
def test_competitor_listing_quality_computed(initialized_db: Path) -> None:
    from delium.validation.evidence import build_competitor_listing_quality

    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
    with get_connection() as conn:
        quality = build_competitor_listing_quality(conn, [C1, C2, C3], "US")
    assert set(quality) == {C1, C2, C3}
    assert all(0.0 <= v <= 100.0 for v in quality.values())


# ===========================================================================
# Hard-kill-first (no wasted review spend, docs §6)
# ===========================================================================
def test_cheap_hard_kill_skips_review_spend(initialized_db: Path) -> None:
    provider = vs.FakeReviewProvider()
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid, price=800)  # $8 median < price floor → K1
    report = _validate(TGT, clients=Clients(reviews=provider))
    assert report.status is ValidationStatus.HARD_KILLED
    assert provider.calls == []  # killed before any review fetch
    assert report.scored.hard_kill_triggered is True


# ===========================================================================
# Scoring ownership + G5 pending (docs §9, §10)
# ===========================================================================
def test_scoring_is_sole_verdict_owner(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        vs.seed_reviews(conn, rid, TGT, n=40)
    report = _validate(TGT)
    assert report.scored.verdict in (Verdict.BUY, Verdict.TEST, Verdict.AVOID)
    # No theme evidence + thin sample → differentiation floor blocks BUY.
    assert report.scored.verdict is not Verdict.BUY


def test_g5_strategist_stays_pending(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        vs.seed_reviews(conn, rid, TGT, n=40)
    report = _validate(TGT)
    assert report.strategist_pending is True
    assert report.scored.strategist_pending is True
    g5 = next(g for g in report.scored.gates if g.gate_id == "G5")
    assert g5.passed is None  # pending — never resolved deterministically


# ===========================================================================
# Persistence + discovery-provenance upgrade (docs §11, §14)
# ===========================================================================
def test_validation_is_persisted(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        vs.seed_reviews(conn, rid, TGT, n=40)
    report = _validate(TGT, run_id=rid)
    with get_connection() as conn:
        row = repository.get_validation(conn, run_id=rid, asin=TGT, marketplace="US")
    assert row is not None
    assert row["verdict"] == report.scored.verdict.value
    assert "config_snapshot" in row["scored"]  # complete reproducible snapshot


def test_existing_candidate_is_upgraded_not_replaced(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        vs.seed_reviews(conn, rid, TGT, n=40)
        repository.upsert_candidate(
            conn,
            asin=TGT,
            marketplace="US",
            source="keyword",
            source_ref=SEED,
            evidence=[{"source": "keyword", "reference": SEED}],
            source_run_id=rid,
            status="shortlist",
        )
    report = _validate(TGT, run_id=rid)
    assert report.from_candidate is True
    with get_connection() as conn:
        cand = repository.get_candidate(conn, TGT, "US")
    assert cand["source"] == "keyword"  # discovery provenance preserved
    assert SEED in cand["evidence"]
    assert cand["status"] in ("validated", "rejected")
    assert cand["verdict"] == report.scored.verdict.value


def test_explicit_asin_creates_candidate(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        vs.seed_reviews(conn, rid, TGT, n=40)
    _validate(TGT, run_id=rid)
    with get_connection() as conn:
        cand = repository.get_candidate(conn, TGT, "US")
    assert cand is not None
    assert cand["source"] == "explicit"


# ===========================================================================
# Determinism (docs §18)
# ===========================================================================
def test_validation_is_deterministic(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        vs.seed_reviews(conn, rid, TGT, n=40)
    first = _validate(TGT)
    second = _validate(TGT)
    assert first.scored.score == second.scored.score
    assert first.scored.verdict == second.scored.verdict
    assert validation_snapshot(first.scored) == validation_snapshot(second.scored)


# ===========================================================================
# Purity guards (docs §22)
# ===========================================================================
def test_analysis_layer_does_not_import_validation() -> None:
    import delium.analysis as analysis_pkg

    root = Path(analysis_pkg.__file__).parent
    forbidden = (
        "delium.providers",
        "delium.ingestion",
        "delium.database",
        "delium.discovery",
        "delium.validation",
        "delium.agents",
        "delium.cli",
    )
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            if mod:
                assert not any(mod.startswith(f) for f in forbidden), f"{path.name} imports {mod}"


def test_validation_has_no_llm_random_or_clock() -> None:
    root = Path(__file__).parent.parent / "src" / "delium" / "validation"
    for path in root.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        code = src.replace(ast.get_docstring(tree) or "", "")
        for banned in (
            "import random",
            "random.",
            "datetime.now",
            "time.time",
            "openai",
            "anthropic",
        ):
            assert banned not in code, f"{path.name} contains {banned}"


# ===========================================================================
# Evidence + hydration helper coverage (error/degradation branches)
# ===========================================================================
def test_evidence_parse_helpers() -> None:
    from delium.analysis.models import ThemeKind
    from delium.validation.evidence import _parse_ids, _theme_kind

    assert _theme_kind("complaint") is ThemeKind.COMPLAINT
    assert _theme_kind("weird") is None
    assert _theme_kind(None) is None
    assert _parse_ids('["a", "b", 1]') == ("a", "b")  # non-str dropped
    assert _parse_ids("not json") == ()
    assert _parse_ids(None) == ()


def test_persisted_themes_are_mapped_from_db(initialized_db: Path) -> None:
    from delium.validation.evidence import build_differentiation

    with get_connection() as conn:
        rid = seed.new_run(conn)
        ids = vs.seed_reviews(conn, rid, TGT, n=24)
        vs.seed_review_theme(conn, rid, TGT, kind="praise", quote_review_ids=ids[:4])
        vs.seed_review_theme(
            conn, rid, TGT, kind="complaint", theme="leak", quote_review_ids=ids[:6]
        )
    with get_connection() as conn:
        report, evidence = build_differentiation(conn, TGT, CFG)
    assert evidence.themes_available == 2  # both persisted Miner themes mapped
    assert {t.theme_id for t in report.themes}  # engine evaluated them


def test_listing_quality_skips_unknown_competitor(initialized_db: Path) -> None:
    from delium.validation.evidence import build_competitor_listing_quality

    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
    with get_connection() as conn:
        quality = build_competitor_listing_quality(conn, [C1, "B0GHOSTXXX"], "US")
    assert set(quality) == {C1}  # the unknown ASIN has no product row → skipped


def test_hydrate_reviews_without_provider_uses_cache_only(initialized_db: Path) -> None:
    from delium.validation.hydration import Clients as _Clients
    from delium.validation.hydration import hydrate_reviews

    tally = hydrate_reviews(["B0X"], CFG, "run", _Clients(), budget_usd=3.0)
    assert tally.count == 0 and tally.cost_usd == 0.0
    assert tally.notes  # explains it used cached reviews only


def test_hydrate_reviews_budget_exhausted_is_degraded(initialized_db: Path) -> None:
    from delium.validation.hydration import Clients as _Clients
    from delium.validation.hydration import hydrate_reviews

    provider = vs.FakeReviewProvider()
    tally = hydrate_reviews(["B0A", "B0B"], CFG, "run", _Clients(reviews=provider), budget_usd=0.0)
    assert tally.degraded is True
    assert provider.calls == []  # zero budget → nothing fetched


def test_hydrate_cluster_without_seed_is_noop(initialized_db: Path) -> None:
    from delium.validation.hydration import Clients as _Clients
    from delium.validation.hydration import hydrate_cluster

    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_product(conn, rid, "B0NOSEED01", "US")
    with get_connection() as conn:
        tally = hydrate_cluster(conn, "B0NOSEED01", None, US, CFG, rid, _Clients())
    assert tally.count == 0
    assert "no keyword cluster seed" in tally.notes[0]


def test_provider_error_falls_back_to_cached_product(initialized_db: Path) -> None:
    class _RaisingKeepa:
        marketplace = "US"

        def fetch_products(self, asins: list[str]) -> object:
            raise ProviderError("network")

    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        vs.seed_reviews(conn, rid, TGT, n=40)
    # force=True bypasses the cache → provider raises → falls back to the cached row.
    report = _validate(TGT, clients=Clients(keepa=lambda _mp: _RaisingKeepa()), force=True)
    assert report.status is ValidationStatus.SCORED


def test_competitor_kill_after_hydration_skips_review_spend(initialized_db: Path) -> None:
    provider = vs.FakeReviewProvider()
    with get_connection() as conn:
        rid = seed.new_run(conn)
        # Median top-10 review count far above the moat ceiling → K6 (needs the
        # competition pillar, so it only fires after competitor hydration).
        seed.seed_keyword_market(conn, rid, "US", asins=(TGT, C1, C2, C3), reviews=4000)
    report = _validate(TGT, clients=Clients(reviews=provider))
    assert report.status is ValidationStatus.HARD_KILLED
    assert provider.calls == []  # killed after competitor hydration, before reviews
    assert any(k.rule_id == "K6" and k.kills for k in report.scored.kills)


def test_validate_lone_product_without_serp(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_product(conn, rid, "B0LONE0001", "US")  # no SERP / no competitors
        vs.seed_reviews(conn, rid, "B0LONE0001", n=35)
    report = _validate("B0LONE0001")
    assert report.status is ValidationStatus.SCORED
    assert report.review_evidence.sample_size == 35
    comp = _pillar(report.scored, "competition")
    assert comp.available is False  # no competitors → competition pillar absent


def test_hydrate_cluster_fetches_competitor_products(initialized_db: Path) -> None:
    # The validation-specific part of cluster hydration: fetch each SERP
    # competitor's Keepa stats through ingestion (cache-first). fetch_keywords is
    # covered separately (test_ingestion_keywords), so no DataForSEO client here.
    from delium.providers.keepa import KeepaClient
    from delium.validation.hydration import Clients as _Clients
    from delium.validation.hydration import hydrate_cluster
    from keepa_support import FakeTransport, keepa_product_body
    from keepa_support import ok as kok

    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword(conn, rid, "US", SEED, 9000)  # FK parent for the SERP rows
        seed.seed_serp(conn, rid, "US", SEED, ["B0AAA00001"])  # a cluster competitor

    def keepa_factory(mp: str) -> object:
        return KeepaClient(
            "k",
            transport=FakeTransport([kok(keepa_product_body("B0AAA00001"))]),
            sleep=lambda _: None,
            marketplace=mp,
        )

    with get_connection() as conn:
        tally = hydrate_cluster(conn, TGT, SEED, US, CFG, rid, _Clients(keepa=keepa_factory))
    assert tally.count == 1  # the SERP competitor's Keepa stats were hydrated
    with get_connection() as conn:
        assert repository.get_product(conn, "B0AAA00001", "US") is not None


def test_hydrate_reviews_provider_error_is_degraded(initialized_db: Path) -> None:
    from delium.validation.hydration import Clients as _Clients
    from delium.validation.hydration import hydrate_reviews

    class _RaisingReviews:
        def fetch_reviews(self, asin: str) -> object:
            raise ProviderError("blocked")

    with get_connection() as conn:
        rid = seed.new_run(conn)
    tally = hydrate_reviews(
        ["B0Z0000001"], CFG, rid, _Clients(reviews=_RaisingReviews()), budget_usd=3.0
    )
    assert tally.degraded is True
    assert tally.count == 0
    assert any("review fetch failed" in n for n in tally.notes)


def test_malformed_candidate_evidence_is_handled(initialized_db: Path) -> None:
    # Requirement 17: malformed persisted evidence must not crash a validation.
    with get_connection() as conn:
        rid = seed.new_run(conn)
        _seed_niche(conn, rid)
        vs.seed_reviews(conn, rid, TGT, n=40)
        repository.upsert_candidate(
            conn,
            asin=TGT,
            marketplace="US",
            source="keyword",
            source_ref=SEED,
            evidence=[
                "not-a-dict",
                {"source": "bogus_source"},
                {"source": "keyword", "reference": SEED},
            ],
            source_run_id=rid,
            status="new",
        )
    report = _validate(TGT, run_id=rid)
    assert report.from_candidate is True
    # Only the well-formed keyword evidence survives parsing; junk is dropped.
    assert [e.source.value for e in report.discovery_evidence] == ["keyword"]

    # Now corrupt the JSON entirely — still no crash, evidence simply empty.
    with get_connection() as conn:
        conn.execute(
            "UPDATE candidates SET evidence = '{bad json' WHERE asin = ? AND marketplace = ?",
            (TGT, "US"),
        )
    report2 = _validate(TGT, run_id=rid)
    assert report2.status is ValidationStatus.SCORED
    assert report2.discovery_evidence == ()
