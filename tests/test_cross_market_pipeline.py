"""Cross-market discovery pipeline (ingestion → pure engine) tests.

Marketplace isolation, directionality, matching, the not-present/unknown/credible
target distinction, provenance, cache behavior, candidate generation, and match
persistence — all over seeded, marketplace-scoped stored data.
"""

from __future__ import annotations

from pathlib import Path

import cross_market_support as seed
from cross_market_support import _SEED
from delium.analysis.models import (
    Confidence,
    CrossMarketVerdict,
    Marketplace,
    MatchConfidence,
    SourceMaturity,
    TargetPresence,
)
from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.ingestion.cross_market import (
    build_source,
    build_target,
    discover_cross_market,
    generate_candidates,
)

CFG = DeliumConfig()
US, CA, UK, AU, IN = (
    Marketplace.US,
    Marketplace.CA,
    Marketplace.UK,
    Marketplace.AU,
    Marketplace.IN,
)


def _discover(target_mps: tuple[Marketplace, ...], **kw: object):  # type: ignore[no-untyped-def]
    with get_connection() as conn:
        return discover_cross_market(
            conn,
            source_mp=US,
            target_mps=target_mps,
            config=CFG,
            **kw,  # type: ignore[arg-type]
        )


# =========================================================================
# Marketplace isolation
# =========================================================================
def test_us_product_never_satisfies_in_query(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_product(conn, run, "B0SHARED", "US", reviews=500)
    with get_connection() as conn:
        # Same ASIN string queried under IN must not return the US row.
        assert repository.get_product(conn, "B0SHARED", "US") is not None
        assert repository.get_product(conn, "B0SHARED", "IN") is None


def test_marketplace_scoped_cache_keys_are_distinct(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_keyword_volume(conn, run, "US", _SEED, 30000)
        seed.seed_keyword_volume(conn, run, "IN", _SEED, 500)
    with get_connection() as conn:
        us = repository.latest_raw_fetch(conn, "dataforseo", f"dataforseo:volume:US:{_SEED}")
        india = repository.latest_raw_fetch(conn, "dataforseo", f"dataforseo:volume:IN:{_SEED}")
        assert us is not None and india is not None
        assert us["id"] != india["id"]


def test_source_volume_isolated_from_target_volume(initialized_db: Path) -> None:
    # US and IN share the seed phrase (keywords PK collision) — volume must still
    # be read per-marketplace from the scoped raw_fetch, not cross-contaminated.
    with get_connection() as conn:
        run = seed.new_run(conn)
        src_seed = seed.seed_strong_source(conn, run)  # US volume 30000
        seed.seed_keyword_volume(conn, run, "IN", src_seed, 500)  # collides on phrase PK
    with get_connection() as conn:
        built = build_source(conn, "USASIN1", US, CFG)
        assert built is not None
        _product, source_input, seed_phrase, _prov = built
        assert seed_phrase == src_seed
        assert source_input.keyword_volume == 30000  # US, not the IN 500


# =========================================================================
# Directionality
# =========================================================================
def test_us_to_au_differs_from_au_to_us(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")  # strong US source
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)  # credible AU demand
        seed.seed_serp(conn, run, "AU", _SEED, [])  # looked up, empty (not present)

    forward = _discover((AU,), run_id=None)
    reverse = _discover((US,))  # AU as source: no AU source product exists
    assert len(forward) == 1
    assert len(reverse) == 0  # nothing proven in AU → not a source
    assert forward[0].report.verdict is not CrossMarketVerdict.INSUFFICIENT_DATA


# =========================================================================
# Matching (through the pipeline / repository)
# =========================================================================
def test_match_exact_gtin_across_marketplaces(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")  # gtin 0012345678905
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)
        seed.seed_serp(conn, run, "AU", _SEED, ["AUASIN1"])
        # Target listing with the SAME GTIN → exact identity.
        seed.seed_product(conn, run, "AUASIN1", "AU", gtin="0012345678905", reviews=60)
    with get_connection() as conn:
        built = build_source(conn, "USASIN1", US, CFG)
        assert built is not None
        source_product, _si, seed_phrase, _p = built
        _tp, _ti, match, method, _prov = build_target(conn, source_product, seed_phrase, AU, CFG)
    assert match.confidence is MatchConfidence.EXACT
    assert method == "gtin"


def test_match_conflicting_dims_not_exact(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")  # dims 200x150x50, no... has gtin
        # Source without gtin so matching is fuzzy; target has same title, wild dims.
        seed.seed_product(conn, run, "USNOGTIN", "US", reviews=1200, est_units=(1000, 2000))
        seed.seed_serp(conn, run, "US", _SEED, ["USASIN1", "USNOGTIN"])
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)
        seed.seed_serp(conn, run, "AU", _SEED, ["AUBIG"])
        seed.seed_product(conn, run, "AUBIG", "AU", reviews=60, dims=(900, 700, 500))
    with get_connection() as conn:
        built = build_source(conn, "USNOGTIN", US, CFG)
        assert built is not None
        source_product, _si, seed_phrase, _p = built
        _tp, _ti, match, method, _prov = build_target(conn, source_product, seed_phrase, AU, CFG)
    assert match.confidence is not MatchConfidence.EXACT
    assert "dims" in match.conflicting_signals
    assert method == "fuzzy"


def test_match_projected_when_absent(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)
        seed.seed_serp(conn, run, "AU", _SEED, [])  # no target listing
    with get_connection() as conn:
        built = build_source(conn, "USASIN1", US, CFG)
        assert built is not None
        source_product, _si, seed_phrase, _p = built
        target_product, _ti, match, method, _prov = build_target(
            conn, source_product, seed_phrase, AU, CFG
        )
    assert method == "projected"
    assert target_product.marketplace is AU
    assert match.confidence is MatchConfidence.EXACT  # same identity projected


def test_match_persisted_and_regenerable(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)
        seed.seed_serp(conn, run, "AU", _SEED, ["AUASIN1"])
        seed.seed_product(conn, run, "AUASIN1", "AU", gtin="0012345678905", reviews=60)
    _discover((AU,))
    with get_connection() as conn:
        matches = repository.get_matches_for_source(
            conn, source_asin="USASIN1", source_marketplace="US", target_marketplace="AU"
        )
    assert len(matches) == 1
    assert matches[0]["match_method"] == "gtin"
    assert matches[0]["match_confidence"] == "exact"


# =========================================================================
# Target presence: not present / insufficient / credible
# =========================================================================
def test_target_unknown_when_never_looked_up(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")  # no target data at all
    result = _discover((IN,))
    assert len(result) == 1
    r = result[0].report
    assert r.target_evidence.presence is TargetPresence.UNKNOWN
    assert r.verdict is CrossMarketVerdict.INSUFFICIENT_DATA
    assert not r.target_evidence.demand_credible


def test_target_not_present_with_no_demand_is_insufficient(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 0)  # looked up, no demand
        seed.seed_serp(conn, run, "AU", _SEED, [])  # looked up, empty
    r = _discover((AU,))[0].report
    assert r.target_evidence.presence is TargetPresence.NOT_PRESENT
    assert not r.target_evidence.demand_credible
    assert r.verdict is CrossMarketVerdict.INSUFFICIENT_DATA
    assert r.verdict is not CrossMarketVerdict.STRONG_OPPORTUNITY


def test_target_not_present_with_credible_demand_is_opportunity(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)  # credible demand
        seed.seed_serp(conn, run, "AU", _SEED, [])  # empty market
    r = _discover((AU,))[0].report
    assert r.target_evidence.presence is TargetPresence.NOT_PRESENT
    assert r.target_evidence.demand_credible
    assert r.verdict in (
        CrossMarketVerdict.STRONG_OPPORTUNITY,
        CrossMarketVerdict.OPPORTUNITY_TO_VALIDATE,
    )


def test_underpenetrated_with_weak_competition_is_strong(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)  # credible demand
        seed.seed_serp(conn, run, "AU", _SEED, ["AUWEAK"])  # one weak incumbent
        seed.seed_product(conn, run, "AUWEAK", "AU", reviews=40)  # low review moat
    r = _discover((AU,))[0].report
    assert r.target_evidence.presence is TargetPresence.UNDERPENETRATED
    assert r.verdict is CrossMarketVerdict.STRONG_OPPORTUNITY
    assert 0.0 <= r.score <= 100.0


def test_mature_target_is_not_a_gap(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 25000)
        listings = [f"AUM{i}" for i in range(10)]
        seed.seed_serp(conn, run, "AU", _SEED, listings)
        for a in listings:
            seed.seed_product(conn, run, a, "AU", reviews=2500)  # strong incumbents
    r = _discover((AU,))[0].report
    assert r.target_evidence.presence in (TargetPresence.MATURE, TargetPresence.SATURATED)
    assert r.verdict is CrossMarketVerdict.MATURE_MARKET


# =========================================================================
# Provenance
# =========================================================================
def test_every_input_traces_to_a_stored_fetch(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)
        seed.seed_serp(conn, run, "AU", _SEED, ["AUASIN1"])
        seed.seed_product(conn, run, "AUASIN1", "AU", gtin="0012345678905", reviews=60)
    cand = _discover((AU,))[0]
    labels = {label for label, _ in cand.provenance.entries}
    assert {"source_product", "source_keyword", "target_serp", "target_keyword"} <= labels
    # Every recorded fetch id exists in raw_fetches or products.fetch_id.
    with get_connection() as conn:
        valid = {row["id"] for row in conn.execute("SELECT id FROM raw_fetches")}
        for _label, fetch_id in cand.provenance.entries:
            assert fetch_id in valid


# =========================================================================
# Cache behavior
# =========================================================================
def test_rerun_uses_cache_no_provider_call(initialized_db: Path) -> None:
    from delium.config.models import DeliumConfig as _Cfg
    from delium.ingestion import fetch_product
    from delium.providers.keepa import KeepaClient
    from keepa_support import FakeTransport, keepa_product_body, ok

    transport = FakeTransport([ok(keepa_product_body("B0AU00001"))])
    client = KeepaClient("k", transport=transport, sleep=lambda _: None, marketplace="AU")
    with get_connection() as conn:
        run = seed.new_run(conn)
    first = fetch_product("B0AU00001", run_id=run, client=client, config=_Cfg())
    second = fetch_product("B0AU00001", run_id=run, client=client, config=_Cfg())
    assert first is not None and first.marketplace == "AU"
    assert second is not None and second.from_cache is True
    assert transport.call_count == 1  # cache hit — no second provider call


# =========================================================================
# Candidate generation
# =========================================================================
def test_generate_candidates_filters_by_marketplace_and_demand(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_product(conn, run, "USHI", "US", reviews=800, est_units=(1000, 2000))
        seed.seed_product(conn, run, "USLO", "US", reviews=50, est_units=(10, 40))
        seed.seed_product(conn, run, "AUX", "AU", reviews=100, est_units=(1000, 2000))
    with get_connection() as conn:
        all_us = generate_candidates(conn, US, CFG)
        gated = generate_candidates(conn, US, CFG, min_monthly_units=500)
    assert set(all_us) == {"USHI", "USLO"}  # AU product excluded
    assert gated == ["USHI"]  # only the high-velocity US product


def test_discovery_skips_below_min_source_maturity(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        # A thin source: one signal only → INSUFFICIENT/EMERGING maturity.
        seed.seed_product(conn, run, "USTHIN", "US", est_units=(10, 20))
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)
    result = _discover((AU,), min_source_maturity=SourceMaturity.VALIDATED)
    assert result == []  # source not mature enough to bother with targets


# =========================================================================
# Determinism
# =========================================================================
def test_discovery_is_deterministic(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)
        seed.seed_serp(conn, run, "AU", _SEED, ["AUWEAK"])
        seed.seed_product(conn, run, "AUWEAK", "AU", reviews=40)
    first = _discover((AU,), persist_matches=False)
    second = _discover((AU,), persist_matches=False)
    assert first[0].report == second[0].report


def test_source_confidence_not_raised_by_missing_data(initialized_db: Path) -> None:
    # Fewer source signals must not yield higher overall confidence.
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USFULL")
        seed.seed_product(conn, run, "USTHIN", "US", reviews=1200, est_units=(1000, 2000))
        seed.seed_serp(conn, run, "US", _SEED, ["USFULL", "USTHIN"])
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)
        seed.seed_serp(conn, run, "AU", _SEED, ["AUWEAK"])
        seed.seed_product(conn, run, "AUWEAK", "AU", reviews=40)
    rank = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}
    with get_connection() as conn:
        full = discover_cross_market(
            conn, source_mp=US, target_mps=(AU,), config=CFG, persist_matches=False
        )
    by_asin = {c.source_asin: c for c in full}
    assert (
        rank[by_asin["USTHIN"].report.source_evidence.confidence]
        <= rank[by_asin["USFULL"].report.source_evidence.confidence]
    )


# =========================================================================
# Helper / branch coverage
# =========================================================================
def test_keyword_volume_from_fetch_edge_cases(initialized_db: Path) -> None:
    from delium.ingestion.cross_market import _keyword_volume_from_fetch

    with get_connection() as conn:
        run = seed.new_run(conn)
        # No fetch at all → all None.
        assert _keyword_volume_from_fetch(conn, US, "ghost phrase") == (None, None, None)
        # Malformed payload → (None, None, fetch_id).
        repository.insert_raw_fetch(
            conn,
            run_id=run,
            provider="dataforseo",
            endpoint="bulk_search_volume",
            request_key="dataforseo:volume:US:bad",
            payload="not-json-object-but-a-string",
        )
        # A payload whose items don't include the phrase → (None, None, fetch_id).
        seed.seed_keyword_volume(conn, run, "US", "other phrase", 999)
        vol, growth, fid = _keyword_volume_from_fetch(conn, US, "other phrase")
        assert vol == 999 and growth is None and fid is not None


def test_row_helpers_dims_weight_history(initialized_db: Path) -> None:
    from delium.ingestion.cross_market import _dims_from_row, _history_months, _median, _oversized

    assert _median([]) is None
    assert _median([2.0, 4.0]) == 3.0  # even-length branch
    assert _median([5.0]) == 5.0

    with get_connection() as conn:
        run = seed.new_run(conn)
        # No dims + huge weight → oversized True; dims_from_row None.
        fid = repository.insert_raw_fetch(
            conn,
            run_id=run,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:US:NODIMS",
            payload={},
        )
        repository.upsert_product(
            conn, asin="NODIMS", fetch_id=fid, marketplace="US", title="t", weight_g=25000
        )
        row = repository.get_product(conn, "NODIMS", "US")
        assert row is not None
        assert _dims_from_row(row) is None
        assert _oversized(row) is True  # weight over threshold
        assert _history_months(repository.get_price_bsr_history(conn, "NODIMS")) is None


def test_oversized_none_when_no_dims_or_weight(initialized_db: Path) -> None:
    from delium.ingestion.cross_market import _oversized

    with get_connection() as conn:
        run = seed.new_run(conn)
        fid = repository.insert_raw_fetch(
            conn,
            run_id=run,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:US:BARE",
            payload={},
        )
        repository.upsert_product(conn, asin="BARE", fetch_id=fid, marketplace="US", title="t")
        row = repository.get_product(conn, "BARE", "US")
        assert row is not None
        assert _oversized(row) is None


def test_build_source_none_for_missing_product(initialized_db: Path) -> None:
    with get_connection() as conn:
        assert build_source(conn, "DOESNOTEXIST", US, CFG) is None


def test_fuzzy_skips_serp_asins_without_stored_products(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        # Source without GTIN so target matching is fuzzy. Keyword rows must
        # exist before their SERP rows (FK on keyword_phrase).
        seed.seed_product(conn, run, "USNOGTIN", "US", reviews=1200, est_units=(1000, 2000))
        seed.seed_keyword_volume(conn, run, "US", _SEED, 30000)
        seed.seed_serp(conn, run, "US", _SEED, ["USNOGTIN"])
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)
        # Target SERP lists two asins: one has no stored product, one matches.
        seed.seed_serp(conn, run, "AU", _SEED, ["AUGHOST", "AUREAL"])
        seed.seed_product(conn, run, "AUREAL", "AU", reviews=60)
    with get_connection() as conn:
        built = build_source(conn, "USNOGTIN", US, CFG)
        assert built is not None
        source_product, _si, seed_phrase, _p = built
        target_product, _ti, _match, method, _prov = build_target(
            conn, source_product, seed_phrase, AU, CFG
        )
    assert method == "fuzzy"
    assert target_product.asin == "AUREAL"  # ghost skipped


def test_discover_skips_target_equal_to_source(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")
    # US in the target list is skipped; only nothing remains → empty.
    result = _discover((US,))
    assert result == []


def test_source_price_out_of_band_transfer(initialized_db: Path) -> None:
    from delium.ingestion.cross_market import _transfer_input

    with get_connection() as conn:
        run = seed.new_run(conn)
        # Very high price → price_positioning_ok False branch.
        seed.seed_product(
            conn, run, "USPRICE", "US", reviews=1200, est_units=(1000, 2000), price_cents=999900
        )
        built = build_source(conn, "USPRICE", US, CFG)
        assert built is not None
        source_product = built[0]
        ti = _transfer_input(conn, "USPRICE", source_product, US, AU, CFG)
    assert ti.price_positioning_ok is False
    assert ti.unit_system_differs is True  # US imperial → AU metric


def test_est_units_high_only_branch(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        fid = seed.seed_product(conn, run, "USHIGH", "US", reviews=1000)
        repository.upsert_product_derived(
            conn, asin="USHIGH", fetch_id=fid, est_units_low=None, est_units_high=2000
        )
        seed.seed_keyword_volume(conn, run, "US", _SEED, 30000)
        seed.seed_serp(conn, run, "US", _SEED, ["USHIGH"])
        built = build_source(conn, "USHIGH", US, CFG)
    assert built is not None
    assert built[1].monthly_units == 2000  # high-only fallback


def test_keyword_volume_phrase_absent_from_payload(initialized_db: Path) -> None:
    from delium.ingestion.cross_market import _keyword_volume_from_fetch

    with get_connection() as conn:
        run = seed.new_run(conn)
        repository.insert_raw_fetch(
            conn,
            run_id=run,
            provider="dataforseo",
            endpoint="bulk_search_volume",
            request_key="dataforseo:volume:US:wanted",
            payload={
                "tasks": [
                    {
                        "status_code": 20000,
                        "result": [{"items": [{"keyword": "other", "search_volume": 5}]}],
                    }
                ]
            },
        )
        vol, growth, fid = _keyword_volume_from_fetch(conn, US, "wanted")
    assert vol is None and growth is None and fid is not None  # fetch exists, phrase missing


def test_dims_missing_key_is_none(initialized_db: Path) -> None:
    from delium.ingestion.cross_market import _dims_from_row

    with get_connection() as conn:
        run = seed.new_run(conn)
        fid = repository.insert_raw_fetch(
            conn,
            run_id=run,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:US:PARTDIM",
            payload={},
        )
        repository.upsert_product(
            conn,
            asin="PARTDIM",
            fetch_id=fid,
            marketplace="US",
            title="t",
            dims={"length_mm": 100},  # missing width/height
        )
        row = repository.get_product(conn, "PARTDIM", "US")
    assert row is not None
    assert _dims_from_row(row) is None


def test_fuzzy_unmatched_falls_back_to_projected(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_product(conn, run, "USNOGTIN", "US", reviews=1200, est_units=(1000, 2000))
        seed.seed_keyword_volume(conn, run, "US", _SEED, 30000)
        seed.seed_serp(conn, run, "US", _SEED, ["USNOGTIN"])
        seed.seed_keyword_volume(conn, run, "AU", _SEED, 8000)
        seed.seed_serp(conn, run, "AU", _SEED, ["AUUNRELATED"])
        # A completely unrelated target product → no shared signals → UNMATCHED.
        seed.seed_product(
            conn,
            run,
            "AUUNRELATED",
            "AU",
            title="unrelated widget gadget",
            brand="Zenith",
            reviews=60,
            dims=(900, 800, 700),
        )
    with get_connection() as conn:
        built = build_source(conn, "USNOGTIN", US, CFG)
        assert built is not None
        source_product, _si, seed_phrase, _p = built
        target_product, _ti, _match, method, _prov = build_target(
            conn, source_product, seed_phrase, AU, CFG
        )
    assert method == "projected"  # no fuzzy match survived → projected identity
    assert target_product.asin == "USNOGTIN"  # projection carries the source identity


def test_generate_candidates_respects_limit(initialized_db: Path) -> None:
    with get_connection() as conn:
        run = seed.new_run(conn)
        for i in range(3):
            seed.seed_product(conn, run, f"USP{i}", "US", reviews=100, est_units=(1000, 2000))
        got = generate_candidates(conn, US, CFG, limit=2)
    assert len(got) == 2
