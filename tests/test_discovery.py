"""Discovery / scout orchestration tests.

Determinism, dedup + merged provenance, marketplace isolation, cross-market
directionality, cache-hit avoidance, kill-first (no wasted enrichment), scoring
as the sole verdict owner, deterministic non-mutating ranking, persistence, and
analysis-layer purity.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import discovery_support as seed
from delium.analysis.models import Marketplace, Verdict
from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.discovery import (
    Candidate,
    CandidateOutcome,
    DiscoveryEvidence,
    DiscoverySource,
    candidates_from_explicit,
    candidates_from_keyword,
    deduplicate,
    rank,
    run_discovery,
)
from discovery_support import SEED

CFG = DeliumConfig()
US, AU, IN = Marketplace.US, Marketplace.AU, Marketplace.IN


def _run(**kw: object):  # type: ignore[no-untyped-def]
    with get_connection() as conn:
        rid = kw.pop("run_id", None) or repository.insert_run(conn, command="discover", input_="t")
        return run_discovery(conn, run_id=rid, config=CFG, **kw)  # type: ignore[arg-type]


# =========================================================================
# Deduplication + merged provenance
# =========================================================================
def _ev(source: DiscoverySource, ref: str, pos: int | None = None) -> DiscoveryEvidence:
    return DiscoveryEvidence(source=source, reference=ref, serp_position=pos)


def test_dedup_merges_same_identity() -> None:
    c1 = Candidate("A1", US, (_ev(DiscoverySource.KEYWORD, "tray", 1),))
    c2 = Candidate("A1", US, (_ev(DiscoverySource.KEYWORD, "freezer", 2),))
    merged = deduplicate([c1, c2])
    assert len(merged) == 1
    assert len(merged[0].evidence) == 2  # provenance merged, not duplicated


def test_dedup_is_idempotent() -> None:
    c = Candidate("A1", US, (_ev(DiscoverySource.KEYWORD, "tray", 1),))
    once = deduplicate([c, c])
    twice = deduplicate(once + once)
    assert once == twice
    assert len(once) == 1
    assert len(once[0].evidence) == 1  # identical evidence deduplicated


def test_dedup_preserves_marketplace_separation() -> None:
    us = Candidate("A1", US, (_ev(DiscoverySource.KEYWORD, "tray", 1),))
    au = Candidate("A1", AU, (_ev(DiscoverySource.KEYWORD, "tray", 1),))
    merged = deduplicate([us, au])
    assert len(merged) == 2  # same ASIN, different marketplace → distinct candidates


def test_dedup_merges_across_sources() -> None:
    kw = Candidate("A1", US, (_ev(DiscoverySource.KEYWORD, "tray", 1),))
    xm = Candidate("A1", US, (_ev(DiscoverySource.CROSS_MARKET, "AU"),))
    merged = deduplicate([kw, xm])
    assert len(merged) == 1
    assert set(merged[0].sources) == {DiscoverySource.KEYWORD, DiscoverySource.CROSS_MARKET}


def test_merge_different_identity_raises() -> None:
    a = Candidate("A1", US)
    b = Candidate("A2", US)
    with pytest.raises(ValueError, match="different identities"):
        a.merged_with(b)


# =========================================================================
# Sources
# =========================================================================
def test_keyword_source_reads_serp(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1", "A2"))
    with get_connection() as conn:
        cands = candidates_from_keyword(conn, SEED, US, cap=10)
    assert [c.asin for c in cands] == ["A1", "A2"]
    assert all(c.marketplace is US for c in cands)
    assert cands[0].evidence[0].serp_position == 1


def test_keyword_source_respects_cap(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1", "A2", "A3"))
    with get_connection() as conn:
        cands = candidates_from_keyword(conn, SEED, US, cap=2)
    assert len(cands) == 2


def test_explicit_source_dedups_input() -> None:
    cands = candidates_from_explicit(["A1", "A1", " A2 ", ""], US)
    assert [c.asin for c in cands] == ["A1", "A2"]
    assert cands[0].evidence[0].source is DiscoverySource.EXPLICIT


# =========================================================================
# Marketplace isolation
# =========================================================================
def test_us_niche_never_discovers_in_candidates(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("USA1", "USA2"))
    # A discovery run scoped to IN sees no US SERP.
    report = _run(marketplace=IN, keywords=[SEED])
    assert report.discovered_count == 0


def test_same_seed_isolated_per_marketplace(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("USA1",))
        seed.seed_keyword_market(conn, rid, "AU", asins=("AUA1",))
    us = _run(marketplace=US, keywords=[SEED])
    au = _run(marketplace=AU, keywords=[SEED])
    assert [c.asin for c in us.discovered] == ["USA1"]
    assert [c.asin for c in au.discovered] == ["AUA1"]


# =========================================================================
# Full pipeline: determinism, kill-first, scoring ownership, ranking
# =========================================================================
def test_discovery_is_deterministic(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1", "A2", "A3"))
    first = _run(marketplace=US, keywords=[SEED], persist=False)
    second = _run(marketplace=US, keywords=[SEED], persist=False)
    assert [e.asin for e in first.ranked] == [e.asin for e in second.ranked]
    assert [e.scored.score for e in first.ranked] == [  # type: ignore[union-attr]
        e.scored.score
        for e in second.ranked  # type: ignore[union-attr]
    ]


def test_ranking_orders_by_score_then_confidence_then_asin(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1", "A2", "A3"))
    report = _run(marketplace=US, keywords=[SEED], persist=False)
    scores = [e.scored.score for e in report.ranked]  # type: ignore[union-attr]
    assert scores == sorted(scores, reverse=True)  # opportunity score DESC


def test_ranking_does_not_mutate_scores(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1", "A2"))
    report = _run(marketplace=US, keywords=[SEED], persist=False)
    evaluated = [*report.ranked]
    before = {e.asin: e.scored.score for e in evaluated}  # type: ignore[union-attr]
    reranked = rank(evaluated)
    after = {e.asin: e.scored.score for e in reranked}  # type: ignore[union-attr]
    assert before == after  # ranking only orders; it never changes a score


def test_hard_kill_first_skips_enrichment(initialized_db: Path) -> None:
    # A market whose median price is below the floor → K1 kills cheaply.
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("C1", "C2"), price_cents=800)
    report = _run(marketplace=US, keywords=[SEED], persist=False)
    assert report.ranked == ()
    assert {e.asin for e in report.killed} == {"C1", "C2"}
    for ec in report.killed:
        assert ec.kill_rule == "K1"
        # Killed cheaply: no pillar was ever assembled (enrichment skipped).
        assert all(not p.available for p in ec.scored.pillars)  # type: ignore[union-attr]


def test_scoring_is_sole_verdict_owner_never_buy_at_discovery(initialized_db: Path) -> None:
    # Discovery tier lacks reviews (differentiation) → scoring never issues BUY.
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1", "A2", "A3"))
    report = _run(marketplace=US, keywords=[SEED], persist=False)
    for ec in report.ranked:
        assert ec.scored is not None
        assert ec.scored.verdict is not Verdict.BUY


def test_unresolved_when_product_not_fetched(initialized_db: Path) -> None:
    # Explicit ASIN with no persisted product → cannot assess.
    report = _run(marketplace=US, asins=["B0GHOST001"], persist=False)
    assert [e.asin for e in report.unresolved] == ["B0GHOST001"]
    assert report.unresolved[0].outcome is CandidateOutcome.UNRESOLVED


# =========================================================================
# Persistence + provenance
# =========================================================================
def test_run_persists_candidates_and_validations(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1", "A2"))
    report = _run(marketplace=US, keywords=[SEED], run_id=rid)
    with get_connection() as conn:
        cands = repository.get_candidates_for_run(conn, report.run_id)
        assert {c["asin"] for c in cands} == {"A1", "A2"}
        assert all(c["source_run_id"] == report.run_id for c in cands)
        val = repository.get_validation(conn, run_id=report.run_id, asin="A1", marketplace="US")
        assert val is not None
        assert val["verdict"] in ("buy", "test", "avoid")


def test_candidate_evidence_is_persisted(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1",))
    _run(marketplace=US, keywords=[SEED], run_id=rid)
    with get_connection() as conn:
        cand = repository.get_candidate(conn, "A1", "US")
    assert cand is not None
    assert cand["source"] == "keyword"
    assert SEED in cand["evidence"]


# =========================================================================
# Cross-market directionality (via discovery)
# =========================================================================
def test_cross_market_discovery_directionality(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        # Strong US source niche + credible-but-empty AU demand.
        seed.seed_product(conn, rid, "USASIN1", "US", gtin="0012345678905", reviews=1200)
        repository.upsert_product_derived(
            conn,
            asin="USASIN1",
            fetch_id=repository.latest_raw_fetch(conn, "keepa", "keepa:product:US:USASIN1")["id"],
            est_units_low=1000,
            est_units_high=2000,
            seasonality_peak_pct=0.2,
        )
        seed.seed_keyword(conn, rid, "US", SEED, 30000)
        seed.seed_serp(conn, rid, "US", SEED, ["USASIN1"])
        seed.seed_keyword(conn, rid, "AU", SEED, 8000)
        seed.seed_serp(conn, rid, "AU", SEED, ["AUWEAK"])
        seed.seed_product(conn, rid, "AUWEAK", "AU", reviews=40)
    forward = _run(marketplace=US, cross_market_targets=(AU,))
    # AU has no source product of its own → reverse yields nothing.
    reverse = _run(marketplace=AU, cross_market_targets=(US,))
    assert any(c.marketplace is AU for c in forward.discovered)
    assert reverse.discovered_count == 0


# =========================================================================
# Cache behavior (hydration goes through ingestion, cache-first)
# =========================================================================
def test_hydration_uses_cache_no_extra_provider_call(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig as _Cfg
    from delium.providers.keepa import KeepaClient
    from keepa_support import FakeTransport, keepa_product_body, ok

    # Pre-seed the keyword niche pointing at one ASIN; pre-cache that product.
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword(conn, rid, "US", SEED, 9000)
        seed.seed_serp(conn, rid, "US", SEED, ["B0AU00001"])
    transport = FakeTransport([ok(keepa_product_body("B0AU00001"))])
    client = KeepaClient("k", transport=transport, sleep=lambda _: None, marketplace="US")
    from delium.ingestion import fetch_product

    with get_connection() as conn:
        fetch_product("B0AU00001", run_id=rid, client=client, config=_Cfg())
    calls_before = transport.call_count

    def keepa_factory(_mp: str) -> object:
        return client

    _run(marketplace=US, keywords=[SEED], run_id=rid, keepa_factory=keepa_factory)
    # The product is fresh in cache → hydration must not call the provider again.
    assert transport.call_count == calls_before


# =========================================================================
# Purity guards
# =========================================================================
def test_analysis_layer_stays_pure() -> None:
    import delium.analysis as analysis_pkg

    root = Path(analysis_pkg.__file__).parent
    forbidden = ("delium.providers", "delium.ingestion", "delium.database", "delium.discovery")
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


def test_discovery_has_no_llm_random_or_clock() -> None:
    root = Path(__file__).parent.parent / "src" / "delium" / "discovery"
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


# =========================================================================
# Assembly coverage (build_scoring_input / pillar assemblers)
# =========================================================================
def test_assembly_returns_none_for_missing_product(initialized_db: Path) -> None:
    from delium.discovery.assembly import build_scoring_input

    with get_connection() as conn:
        inp, prov = build_scoring_input(conn, "GHOST", US, CFG)
    assert inp is None
    assert prov.entries == ()


def test_assembly_cheap_only_populates_kill_facts(initialized_db: Path) -> None:
    from delium.discovery.assembly import build_scoring_input

    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1", "A2"), price_cents=2200)
    with get_connection() as conn:
        inp, _ = build_scoring_input(conn, "A1", US, CFG, cheap_only=True)
    assert inp is not None
    assert inp.market_median_price_cents == 2200
    assert inp.oversized is False
    assert inp.demand is None and inp.competition is None  # no pillar assembly in cheap mode


def test_assembly_full_builds_pillars(initialized_db: Path) -> None:
    from delium.discovery.assembly import build_scoring_input

    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1", "A2", "A3"))
    with get_connection() as conn:
        inp, prov = build_scoring_input(conn, "A1", US, CFG)
    assert inp is not None
    assert inp.demand is not None  # BSR history present
    assert inp.competition is not None  # SERP competitors present
    assert inp.profit is not None  # dims + price present
    assert inp.risk is not None
    assert inp.differentiation is None  # validate-tier (reviews) → deliberately absent
    assert any(label == "product" for label, _ in prov.entries)


def test_assembly_demand_none_without_history(initialized_db: Path) -> None:
    from delium.discovery.assembly import build_scoring_input

    with get_connection() as conn:
        rid = seed.new_run(conn)
        # Product row with no price_bsr_history rows.
        fid = repository.insert_raw_fetch(
            conn,
            run_id=rid,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:US:NOHIST",
            payload={},
        )
        repository.upsert_product(
            conn,
            asin="NOHIST",
            fetch_id=fid,
            marketplace="US",
            title="t",
            category_path="Baby",
            dims={"length_mm": 200, "width_mm": 150, "height_mm": 50},
            weight_g=300,
        )
    with get_connection() as conn:
        inp, _ = build_scoring_input(conn, "NOHIST", US, CFG)
    assert inp is not None
    assert inp.demand is None  # no BSR history
    assert inp.competition is None  # no seed/SERP → no competitors


def test_assembly_profit_none_without_dims(initialized_db: Path) -> None:
    from delium.discovery.assembly import build_scoring_input

    with get_connection() as conn:
        rid = seed.new_run(conn)
        fid = repository.insert_raw_fetch(
            conn,
            run_id=rid,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:US:NODIMS",
            payload={},
        )
        repository.upsert_product(
            conn,
            asin="NODIMS",
            fetch_id=fid,
            marketplace="US",
            title="t",
            category_path="Baby",
            weight_g=None,
        )
        repository.upsert_price_bsr_history(
            conn,
            asin="NODIMS",
            captured_on="2025-07-01",
            price_cents=2200,
            bsr=1500,
            review_count=100,
        )
    with get_connection() as conn:
        inp, _ = build_scoring_input(conn, "NODIMS", US, CFG)
    assert inp is not None
    assert inp.profit is None  # fee-blocking: no dims/weight


def test_assembly_fad_inputs_from_volume_series(initialized_db: Path) -> None:
    from delium.discovery.assembly import build_scoring_input

    with get_connection() as conn:
        rid = seed.new_run(conn)
        # Keyword row (with a 13-point rising series) must exist before its SERP.
        repository.insert_raw_fetch(
            conn,
            run_id=rid,
            provider="dataforseo",
            endpoint="bulk_search_volume",
            request_key=f"dataforseo:volume:US:{SEED}",
            payload={
                "tasks": [
                    {
                        "status_code": 20000,
                        "result": [{"items": [{"keyword": SEED, "search_volume": 12000}]}],
                    }
                ]
            },
        )
        repository.upsert_keyword(
            conn,
            phrase=SEED,
            fetch_id=repository.latest_raw_fetch(
                conn, "dataforseo", f"dataforseo:volume:US:{SEED}"
            )["id"],
            marketplace="US",
            volume=12000,
            volume_series=[1000] * 12 + [12000],
        )
        seed.seed_serp(conn, rid, "US", SEED, ["A1"])
        seed.seed_product(conn, rid, "A1", "US")
    with get_connection() as conn:
        inp, _ = build_scoring_input(conn, "A1", US, CFG, cheap_only=True)
    assert inp is not None
    assert inp.fad_search_volume == 12000
    assert inp.volume_history_months == 13
    assert inp.fad_volume_12mo_median == 1000


def test_assembly_avoid_list_match(initialized_db: Path) -> None:
    from delium.discovery.assembly import build_scoring_input

    cfg = CFG.model_copy(
        update={"preferences": CFG.preferences.model_copy(update={"avoid": ["glass"]})}
    )
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_product(conn, rid, "GLASSY", "US", title="glass storage jar")
        seed.seed_keyword(conn, rid, "US", SEED, 9000)
        seed.seed_serp(conn, rid, "US", SEED, ["GLASSY"])
    with get_connection() as conn:
        inp, _ = build_scoring_input(conn, "GLASSY", US, cfg, cheap_only=True)
    assert inp is not None
    assert "glass" in inp.avoid_matches


# =========================================================================
# Pure helper coverage (assembly + pipeline internals)
# =========================================================================
def test_parse_date_and_series_helpers() -> None:
    from delium.discovery.assembly import _parse_date, _parse_series

    assert _parse_date(None) is None
    assert _parse_date("not-a-date") is None
    assert _parse_date("2025-07-01") is not None
    assert _parse_series(None) is None
    assert _parse_series("{bad json") is None
    assert _parse_series("[1, 2, 3]") == (1, 2, 3)
    assert _parse_series("[]") is None


def test_candidate_units_helper() -> None:
    from delium.discovery.assembly import _candidate_units

    assert _candidate_units(None, "A1") is None


def test_provenance_ignores_missing_fetch_id() -> None:
    from delium.discovery.assembly import AssemblyProvenance

    prov = AssemblyProvenance().add("x", None)
    assert prov.entries == ()


def test_cross_market_source_skips_non_opportunities(initialized_db: Path) -> None:
    # A saturated target must not become a discovery candidate.
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_product(conn, rid, "USASIN1", "US", gtin="0012345678905", reviews=1200)
        repository.upsert_product_derived(
            conn,
            asin="USASIN1",
            fetch_id=repository.latest_raw_fetch(conn, "keepa", "keepa:product:US:USASIN1")["id"],
            est_units_low=1000,
            est_units_high=2000,
        )
        seed.seed_keyword(conn, rid, "US", SEED, 30000)
        seed.seed_serp(conn, rid, "US", SEED, ["USASIN1"])
        # AU: strong demand AND strong incumbents → MATURE/SATURATED, not a gap.
        seed.seed_keyword(conn, rid, "AU", SEED, 25000)
        listings = [f"AUM{i}" for i in range(10)]
        seed.seed_serp(conn, rid, "AU", SEED, listings)
        for a in listings:
            seed.seed_product(conn, rid, a, "AU", reviews=3000)
    report = _run(marketplace=US, cross_market_targets=(AU,))
    assert all(c.marketplace is not AU for c in report.discovered)
