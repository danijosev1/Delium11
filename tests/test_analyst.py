"""Analyst agent + competitor-absence derivation tests.

The load-bearing guarantees mirror the Miner's: the LLM extracts observable
listing facts only, its output is evidence-checked (a claimed feature that is not
in its own listing text is dropped, an unknown ASIN is discarded), and a
competitor is declared to LACK a feature only when the Analyst matrix confirms it
across a sufficient sample — never from silence. The Analyst never sets a score.
"""

from __future__ import annotations

from pathlib import Path

import agents_support as fake
import discovery_support as seed
import validation_support as vs
from agents_support import FakeLlmTransport, build_llm, http, json_body
from delium.agents.analyst import (
    ANALYST_SYSTEM,
    build_analyst_context,
    make_analyst_evidence_check,
    persist_analyst_output,
    run_analyst,
)
from delium.agents.schemas import AnalystReport
from delium.config.models import AgentsConfig, DeliumConfig
from delium.database import get_connection, repository
from delium.utils.text import feature_present, normalize
from delium.validation.evidence import (
    _competitor_haystacks,
    _derive_absence,
    _derive_bundle_complement,
    build_differentiation,
)

CFG = DeliumConfig()
TGT = "B0TARGET01"
C1, C2, C3 = "B0COMPET01", "B0COMPET02", "B0COMPET03"


def _report(**overrides: object) -> AnalystReport:
    payload = fake.analyst_payload()
    payload.update(overrides)
    return AnalystReport.model_validate(payload)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def test_feature_present_substring_and_token_coverage() -> None:
    assert feature_present("silicone lid", "premium silicone lid set", 0.85)
    # both tokens present but not contiguous → token-coverage path
    assert feature_present("silicone lid", "lid moulded from silicone", 0.85)
    assert feature_present("SILICONE", "food-grade silicone tray", 0.85)  # case-insensitive
    # only half the tokens present → below threshold, not present
    assert not feature_present("bamboo handle", "bamboo spoon set", 0.85)
    assert not feature_present("", "anything", 0.85)
    assert not feature_present("x", "", 0.85)


def test_normalize() -> None:
    assert normalize("  Hello,  World!! ") == "hello world"
    assert normalize("Stainless-Steel/Lid") == "stainless steel lid"
    assert normalize(None) == ""


# ---------------------------------------------------------------------------
# Evidence check (the extraction guard)
# ---------------------------------------------------------------------------
def test_evidence_check_drops_unsupported_features_and_unknown_asins() -> None:
    known = frozenset({"A", "B"})
    texts = {"A": "silicone lid tray", "B": "bamboo spoon set"}
    report = _report(
        feature_matrix=[
            {"asin": "A", "claimed_features": ["silicone lid", "titanium blade"]},
            {"asin": "GHOST", "claimed_features": ["magic"]},  # unknown ASIN → all dropped
        ]
    )
    cleaned, dropped, total = make_analyst_evidence_check(known, texts, 0.85)(report)
    assert total == 3  # 2 for A + 1 for GHOST
    assert dropped == 2  # titanium blade (not in A's text) + GHOST's magic
    kept = {e.asin: list(e.claimed_features) for e in cleaned.feature_matrix}
    assert kept == {"A": ["silicone lid"]}  # GHOST entry removed entirely


def test_evidence_check_prunes_unknown_asins_from_rubric_and_who_wins() -> None:
    known = frozenset({"A"})
    texts = {"A": "silicone lid"}
    report = _report(
        who_wins_and_why=[
            {"asin": "A", "advantage": "cheapest", "evidence": []},
            {"asin": "GHOST", "advantage": "fabricated", "evidence": []},
        ],
        listing_rubric=[{"asin": "A"}, {"asin": "GHOST"}],
    )
    cleaned, _dropped, _total = make_analyst_evidence_check(known, texts, 0.85)(report)
    assert [w.asin for w in cleaned.who_wins_and_why] == ["A"]
    assert [r.asin for r in cleaned.listing_rubric] == ["A"]


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------
def test_build_context_orders_target_first_and_labels_roles(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_product(conn, rid, TGT, "US", title="target silicone lid tray")
        seed.seed_product(conn, rid, C1, "US", title="rival bamboo spoon set")
        listings = {a: r for a in (TGT, C1) if (r := repository.get_product(conn, a)) is not None}
    ctx = build_analyst_context(listings, target_asin=TGT, competitor_asins=[C1])
    assert f'asin="{TGT}" role="target"' in ctx
    assert f'asin="{C1}" role="competitor"' in ctx
    assert "silicone lid" in ctx and "bamboo spoon" in ctx
    assert ctx.index(f'asin="{TGT}"') < ctx.index(f'asin="{C1}"')  # target first


# ---------------------------------------------------------------------------
# run_analyst + persistence
# ---------------------------------------------------------------------------
def test_run_analyst_persists_only_competitor_features(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_product(conn, rid, TGT, "US", title="silicone lid freezer tray")
        seed.seed_product(conn, rid, C1, "US", title="stainless steel divided tray with lid")
        seed.seed_product(conn, rid, C2, "US", title="bamboo spoon feeding set")
    payload = fake.analyst_payload(
        feature_matrix=[
            {"asin": TGT, "claimed_features": ["silicone lid"]},
            {"asin": C1, "claimed_features": ["stainless steel", "lid", "made-up widget"]},
            {"asin": C2, "claimed_features": ["bamboo spoon"]},
        ]
    )
    llm = build_llm(FakeLlmTransport([json_body(payload)]), AgentsConfig())
    with get_connection() as conn:
        run = run_analyst(
            conn, llm, target_asin=TGT, competitor_asins=[C1, C2], config=AgentsConfig()
        )
        assert run.result.ok and run.result.output is not None
        persist_analyst_output(
            conn,
            run_id=rid,
            target_asin=TGT,
            competitor_asins=run.competitor_asins,
            report=run.result.output,
        )
        target_feats = repository.get_competitor_features(conn, TGT)
        c1 = {r["feature"] for r in repository.get_competitor_features(conn, C1)}
        c2 = {r["feature"] for r in repository.get_competitor_features(conn, C2)}
    assert target_feats == []  # target features are NEVER persisted to competitor_features
    assert {"stainless steel", "lid"} <= c1
    assert "made-up widget" not in c1  # not in C1's title → dropped by the evidence check
    assert c2 == {"bamboo spoon"}


def test_run_analyst_persist_is_replace_not_append(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_product(conn, rid, C1, "US", title="stainless steel tray")
        repository.insert_competitor_feature(conn, run_id=rid, asin=C1, feature="stale feature")
    report = _report(feature_matrix=[{"asin": C1, "claimed_features": ["stainless steel"]}])
    with get_connection() as conn:
        persist_analyst_output(
            conn, run_id=rid, target_asin=TGT, competitor_asins=(C1,), report=report
        )
        feats = {r["feature"] for r in repository.get_competitor_features(conn, C1)}
    assert feats == {"stainless steel"}  # stale row replaced, not appended


def test_persist_dedupes_and_skips_blank_features(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_product(conn, rid, C1, "US", title="tray with lid")
    # Duplicate "lid" collapses; the blank entry is skipped. persist trusts the
    # report as-is (the evidence check already ran in run_analyst).
    matrix = [{"asin": C1, "claimed_features": ["lid", "lid", "  ", "tray"]}]
    report = _report(feature_matrix=matrix)
    with get_connection() as conn:
        persist_analyst_output(
            conn, run_id=rid, target_asin=TGT, competitor_asins=(C1,), report=report
        )
        feats = [r["feature"] for r in repository.get_competitor_features(conn, C1)]
    assert sorted(feats) == ["lid", "tray"]


def test_build_context_tolerates_empty_listing_text(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_product(conn, rid, TGT, "US", title="target tray", brand="", gtin=None)
        seed.seed_product(conn, rid, C1, "US", title="", brand="")  # no observable text
        listings = {a: r for a in (TGT, C1) if (r := repository.get_product(conn, a)) is not None}
    ctx = build_analyst_context(listings, target_asin=TGT, competitor_asins=[C1])
    assert f'asin="{C1}" role="competitor"' in ctx  # block emitted even when empty


def test_run_analyst_failure_degrades_without_persisting(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_product(conn, rid, C1, "US", title="stainless steel tray")
    llm = build_llm(FakeLlmTransport([http(500)]), AgentsConfig())  # provider error
    with get_connection() as conn:
        run = run_analyst(conn, llm, target_asin=TGT, competitor_asins=[C1], config=AgentsConfig())
        assert run.result.status == "failed" and run.result.output is None
        assert repository.get_competitor_features(conn, C1) == []
    assert ANALYST_SYSTEM  # sanity: prompt is a non-empty constant


# ---------------------------------------------------------------------------
# Absence derivation (evidence.py) — the conservative, coverage-gated core
# ---------------------------------------------------------------------------
def test_derive_absence_true_only_when_no_competitor_has_it() -> None:
    from delium.analysis.models import FeatureRequest

    haystacks = {C1: "silicone lid", C2: "bamboo spoon", C3: "plastic cup"}
    reqs = (
        FeatureRequest(
            feature="titanium blade", supporting_review_ids=("r1",), absent_from_competitors=None
        ),
        FeatureRequest(
            feature="silicone lid", supporting_review_ids=("r1",), absent_from_competitors=None
        ),
    )
    derived = {f.feature: f.absent_from_competitors for f in _derive_absence(reqs, haystacks)}
    assert derived["titanium blade"] is True  # nobody claims it → confirmed gap
    assert derived["silicone lid"] is False  # C1 claims it → present, not a gap


def test_derive_bundle_complement_true_false_none() -> None:
    from delium.analysis.models import BundleSignal

    haystacks = {C1: "storage bag included", C2: "spoon"}
    present = (BundleSignal(complement="storage bag", supporting_review_ids=("r1",)),)
    absent = (BundleSignal(complement="warming plate", supporting_review_ids=("r1",)),)
    assert _derive_bundle_complement(present, haystacks) is True  # already bundled → no opening
    assert _derive_bundle_complement(absent, haystacks) is False  # opening exists
    assert _derive_bundle_complement((), haystacks) is None  # nothing to judge


def _seed_market_with_reviews(conn, rid: str) -> list[str]:  # type: ignore[no-untyped-def]
    seed.seed_keyword_market(conn, rid, "US", asins=(TGT, C1, C2, C3))
    return vs.seed_reviews(conn, rid, TGT, n=20)


def test_build_differentiation_confirms_gap_when_coverage_met(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        ids = _seed_market_with_reviews(conn, rid)
        repository.insert_feature_request(
            conn,
            run_id=rid,
            asin=TGT,
            feature="titanium blade",
            supporting_review_ids=ids[:5],
            absent_from_competitors=None,
        )
        for comp, feat in ((C1, "silicone lid"), (C2, "bamboo spoon"), (C3, "plastic cup")):
            repository.insert_competitor_feature(conn, run_id=rid, asin=comp, feature=feat)
        report, ev = build_differentiation(
            conn, TGT, CFG, miner_ran=True, competitor_asins=[C1, C2, C3], marketplace="US"
        )
    assert ev is not None and ev.competitor_matrix_confirmed is True
    gap = next(g for g in ev.feature_gaps if g.feature == "titanium blade")
    assert gap.status == "absent"
    assert report is not None and report.feature_gap_count >= 1


def test_build_differentiation_marks_present_when_a_competitor_claims_it(
    initialized_db: Path,
) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        ids = _seed_market_with_reviews(conn, rid)
        repository.insert_feature_request(
            conn,
            run_id=rid,
            asin=TGT,
            feature="titanium blade",
            supporting_review_ids=ids[:5],
            absent_from_competitors=None,
        )
        # C1 actually claims the requested feature → present, not a gap.
        for comp, feat in ((C1, "titanium blade"), (C2, "bamboo spoon"), (C3, "plastic cup")):
            repository.insert_competitor_feature(conn, run_id=rid, asin=comp, feature=feat)
        _report_out, ev = build_differentiation(
            conn, TGT, CFG, miner_ran=True, competitor_asins=[C1, C2, C3], marketplace="US"
        )
    gap = next(g for g in ev.feature_gaps if g.feature == "titanium blade")
    assert gap.status == "present"


def test_build_differentiation_unknown_below_coverage_gate(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        ids = _seed_market_with_reviews(conn, rid)
        repository.insert_feature_request(
            conn,
            run_id=rid,
            asin=TGT,
            feature="titanium blade",
            supporting_review_ids=ids[:5],
            absent_from_competitors=None,
        )
        # Only ONE competitor analyzed (< default min coverage of 3): cannot confirm
        # absence — the feature must stay UNKNOWN, never assumed absent from silence.
        repository.insert_competitor_feature(conn, run_id=rid, asin=C1, feature="silicone lid")
        report, ev = build_differentiation(
            conn, TGT, CFG, miner_ran=True, competitor_asins=[C1, C2, C3], marketplace="US"
        )
    assert ev.competitor_matrix_confirmed is False
    assert all(g.status == "unknown" for g in ev.feature_gaps)
    assert report.feature_gap_count == 0  # UNKNOWN never counts as a confirmed gap


def test_no_competitor_features_leaves_everything_unknown(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        ids = _seed_market_with_reviews(conn, rid)
        repository.insert_feature_request(
            conn,
            run_id=rid,
            asin=TGT,
            feature="titanium blade",
            supporting_review_ids=ids[:5],
            absent_from_competitors=None,
        )
        # Competitors exist as listings but the Analyst persisted no features.
        haystacks = _competitor_haystacks(conn, [C1, C2, C3], "US")
        report, ev = build_differentiation(
            conn, TGT, CFG, miner_ran=True, competitor_asins=[C1, C2, C3], marketplace="US"
        )
    assert haystacks == {}  # no persisted features → not analyzed → no coverage
    assert ev.competitor_matrix_confirmed is False
    assert all(g.status == "unknown" for g in ev.feature_gaps)
    assert report.feature_gap_count == 0
