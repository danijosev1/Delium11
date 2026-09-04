"""LLM agent layer unit tests: client, runner guards, Review Miner, Strategist.

No live LLM calls — every test injects a fake POST transport. Covers the
integrity guards the deterministic layer relies on: structured-output validation,
retry-once, evidence resolution (drop/degrade/fail), and that the LLM never
supplies a number or a verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agents_support as fake
import discovery_support as seed
import validation_support as vs
from agents_support import FakeLlmTransport, build_llm, http, json_body, llm_body, miner_payload
from delium.agents.llm import (
    LlmAuthError,
    LlmError,
    LlmRateLimitError,
    LlmResponseError,
    Tier,
    build_llm_client,
)
from delium.agents.miner import (
    _COGS_MAP,
    make_evidence_check,
    persist_miner_output,
    run_review_miner,
)
from delium.agents.runner import run_structured
from delium.agents.schemas import MinerReport, StrategistVerdict
from delium.agents.strategist import derive_concurrence, run_strategist
from delium.analysis.models import Addressability, StrategistConcurrence
from delium.config.models import AgentsConfig, DeliumConfig
from delium.database import get_connection, repository

CFG = DeliumConfig()
AC = CFG.agents


# ===========================================================================
# LLM client
# ===========================================================================
def test_client_parses_text_and_usage() -> None:
    llm = build_llm(FakeLlmTransport([llm_body("hello", input_tokens=1000, output_tokens=500)]))
    resp = llm.complete(tier=Tier.FAST, system="s", user="u")
    assert resp.text == "hello"
    assert resp.input_tokens == 1000 and resp.output_tokens == 500
    # Haiku default pricing: 1$/MTok in, 5$/MTok out.
    assert resp.cost_usd == pytest.approx(1000 / 1e6 * 1.0 + 500 / 1e6 * 5.0)


def test_client_tier_selects_model_and_pricing() -> None:
    transport = FakeLlmTransport([llm_body("x"), llm_body("y")])
    llm = build_llm(transport)
    llm.complete(tier=Tier.FAST, system="s", user="u")
    llm.complete(tier=Tier.FRONTIER, system="s", user="u")
    assert transport.calls[0]["body"]["model"] == AC.fast_model
    assert transport.calls[1]["body"]["model"] == AC.frontier_model


def test_client_sends_temperature_zero_for_reproducibility() -> None:
    transport = FakeLlmTransport([llm_body("x")])
    build_llm(transport).complete(tier=Tier.FAST, system="s", user="u")
    assert transport.calls[0]["body"]["temperature"] == 0.0
    assert transport.calls[0]["headers"]["x-api-key"] == "test-key"


def test_client_auth_error() -> None:
    llm = build_llm(FakeLlmTransport([http(401)]))
    with pytest.raises(LlmAuthError):
        llm.complete(tier=Tier.FAST, system="s", user="u")


def test_client_rate_limit_error() -> None:
    llm = build_llm(FakeLlmTransport([http(429)]))
    with pytest.raises(LlmRateLimitError):
        llm.complete(tier=Tier.FAST, system="s", user="u")


def test_client_server_error_and_malformed_body() -> None:
    with pytest.raises(LlmResponseError):
        build_llm(FakeLlmTransport([http(503)])).complete(tier=Tier.FAST, system="s", user="u")
    with pytest.raises(LlmResponseError):
        build_llm(FakeLlmTransport([http(200, [])])).complete(tier=Tier.FAST, system="s", user="u")


def test_client_transport_exception_becomes_llm_error() -> None:
    llm = build_llm(FakeLlmTransport([RuntimeError("boom")]))
    with pytest.raises((LlmError, RuntimeError)):
        # transport raises a non-ProviderError → propagates; ProviderError → LlmError.
        llm.complete(tier=Tier.FAST, system="s", user="u")


def test_client_refusal_flag() -> None:
    llm = build_llm(FakeLlmTransport([llm_body("", stop_reason="refusal")]))
    resp = llm.complete(tier=Tier.FAST, system="s", user="u")
    assert resp.refused is True


def test_build_llm_client_none_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELIUM_LLM_API_KEY", raising=False)
    assert build_llm_client(AC) is None


def test_build_llm_client_present_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIUM_LLM_API_KEY", "sk-test")
    assert build_llm_client(AC) is not None


# ===========================================================================
# Runner guards
# ===========================================================================
def test_runner_valid_output_ok() -> None:
    payload = miner_payload(complaint_ids=["a", "b", "c"])
    llm = build_llm(FakeLlmTransport([json_body(payload)]))
    result = run_structured(
        llm, tier=Tier.FAST, system="voice", user="u", schema=MinerReport, config=AC
    )
    assert result.status == "ok"
    assert result.output is not None and result.output.complaints[0].theme


def test_runner_extracts_json_from_prose_and_fences() -> None:
    payload = miner_payload(complaint_ids=["a", "b", "c"])
    text = (
        f"Here is the result:\n```json\n{json_body(payload).body['content'][0]['text']}\n```\nDone."
    )
    llm = build_llm(FakeLlmTransport([llm_body(text)]))
    result = run_structured(
        llm, tier=Tier.FAST, system="voice", user="u", schema=MinerReport, config=AC
    )
    assert result.ok


def test_runner_malformed_json_retries_then_fails() -> None:
    transport = FakeLlmTransport([llm_body("not json at all"), llm_body("still not json")])
    result = run_structured(
        build_llm(transport), tier=Tier.FAST, system="s", user="u", schema=MinerReport, config=AC
    )
    assert result.status == "failed"
    assert transport.call_count == 2  # one retry (max_retries=1)


def test_runner_schema_violation_fails() -> None:
    # StrategistVerdict requires >=2 risk_register / verdict_changers → this is invalid.
    bad = {
        "verdict": "buy",
        "conviction": 4,
        "agrees_with_score": True,
        "rationale": [],
        "risk_register": [],
        "verdict_changers": [],
        "one_paragraph": "x",
    }
    result = run_structured(
        build_llm(FakeLlmTransport([json_body(bad)])),
        tier=Tier.FRONTIER,
        system="s",
        user="u",
        schema=StrategistVerdict,
        config=AC,
    )
    assert result.status == "failed" and result.output is None


def test_runner_llm_error_is_failed_not_fabricated() -> None:
    result = run_structured(
        build_llm(FakeLlmTransport([http(500)])),
        tier=Tier.FAST,
        system="s",
        user="u",
        schema=MinerReport,
        config=AC,
    )
    assert result.status == "failed" and result.output is None


def test_runner_evidence_drop_within_threshold_degrades() -> None:
    eligible = frozenset({"a", "b", "c", "d"})
    # 6 themed items, 1 fabricated → 1/6 ≈ 17% dropped, under the 20% threshold → degraded.
    good = {"quote_review_ids": ["a", "b", "c"]}
    payload = {
        "complaints": [
            {"theme": "t1", "severity": 3, **good},
            {"theme": "t2", "severity": 2, **good},
            {"theme": "t3", "severity": 2, **good},
            {"theme": "t4", "severity": 1, "quote_review_ids": ["ghost1", "ghost2", "ghost3"]},
        ],
        "praise": [{"theme": "p1", **good}],
        "missing_features": [{"feature": "f1", "requested_in_review_ids": ["a", "b", "d"]}],
        "improvement_ideas": [],
        "bundle_signals": [],
        "sample_caveats": [],
    }
    result = run_structured(
        build_llm(FakeLlmTransport([json_body(payload)])),
        tier=Tier.FAST,
        system="s",
        user="u",
        schema=MinerReport,
        config=AC,
        evidence_check=make_evidence_check(eligible, 3),
    )
    assert result.status == "degraded"
    assert result.dropped == 1 and result.total == 6
    # The fabricated theme was mechanically dropped from the cleaned output.
    assert result.output is not None
    assert all(c.theme != "t4" for c in result.output.complaints)


def test_runner_evidence_drop_over_threshold_fails() -> None:
    eligible = frozenset({"a", "b", "c"})
    payload = {
        "complaints": [
            {"theme": "real", "severity": 3, "quote_review_ids": ["a", "b", "c"]},
            {"theme": "fake", "severity": 2, "quote_review_ids": ["x", "y", "z"]},
        ],
        "praise": [],
        "missing_features": [],
        "improvement_ideas": [],
        "bundle_signals": [],
        "sample_caveats": [],
    }
    transport = FakeLlmTransport([json_body(payload), json_body(payload)])
    result = run_structured(
        build_llm(transport),
        tier=Tier.FAST,
        system="s",
        user="u",
        schema=MinerReport,
        config=AC,
        evidence_check=make_evidence_check(eligible, 3),
    )
    assert result.status == "failed"  # 50% dropped > 20%
    assert transport.call_count == 2  # retried once


# ===========================================================================
# Review Miner: evidence resolution + persistence mapping
# ===========================================================================
def test_miner_evidence_drops_fabricated_and_duplicate_ids() -> None:
    eligible = frozenset({"r1", "r2", "r3", "r4", "r5"})
    check = make_evidence_check(eligible, 3)
    report = MinerReport.model_validate(
        {
            "complaints": [
                {"theme": "real", "quote_review_ids": ["r1", "r2", "r3"]},  # keep
                {"theme": "dupes", "quote_review_ids": ["r1", "r1", "r1"]},  # 1 unique → drop
                {"theme": "ghosts", "quote_review_ids": ["x", "y", "z"]},  # 0 real → drop
            ]
        }
    )
    cleaned, dropped, total = check(report)
    assert total == 3 and dropped == 2
    assert [c.theme for c in cleaned.complaints] == ["real"]


def test_miner_cogs_map_is_deterministic_lookup() -> None:
    assert _COGS_MAP["none"][0] is Addressability.FIXABLE
    assert _COGS_MAP["low"][0] is Addressability.FIXABLE
    assert _COGS_MAP["moderate"][0] is Addressability.PARTIAL
    assert _COGS_MAP["high"][0] is Addressability.HARD


def test_miner_persistence_maps_all_evidence(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        ids = vs.seed_reviews(conn, rid, "B0TARGET01", n=10)
        report = MinerReport.model_validate(
            {
                "complaints": [
                    {
                        "theme": "lid cracks",
                        "severity": 3,
                        "quote_review_ids": ids[:5],
                        "category": "usage",
                    }
                ],
                "praise": [{"theme": "easy release", "quote_review_ids": ids[:4]}],
                "missing_features": [
                    {"feature": "silicone lid", "requested_in_review_ids": ids[:6]}
                ],
                "improvement_ideas": [
                    {
                        "idea": "thicker lid",
                        "addresses_theme": "lid cracks",
                        "cogs_impact_guess": "low",
                    }
                ],
                "bundle_signals": [
                    {"complement": "storage bag", "mentioned_in_review_ids": ids[:3]}
                ],
                "sample_caveats": [],
            }
        )
        persist_miner_output(conn, run_id=rid, asin="B0TARGET01", report=report)
    with get_connection() as conn:
        themes = repository.get_review_themes(conn, "B0TARGET01")
        features = repository.get_feature_requests(conn, "B0TARGET01")
        bundles = repository.get_bundle_signals(conn, "B0TARGET01")
    complaint = next(t for t in themes if t["kind"] == "complaint")
    assert complaint["addressability"] == "fixable"  # low cogs impact → fixable
    assert complaint["cogs_delta"] == pytest.approx(0.10)
    assert complaint["category"] == "usage"
    assert {t["kind"] for t in themes} == {"complaint", "praise"}
    assert len(features) == 1 and features[0]["absent_from_competitors"] is None
    assert len(bundles) == 1 and bundles[0]["complement"] == "storage bag"


def test_miner_persistence_replaces_stale_evidence(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        ids = vs.seed_reviews(conn, rid, "B0TARGET01", n=10)
        first = MinerReport.model_validate(
            {"complaints": [{"theme": "old", "quote_review_ids": ids[:3]}]}
        )
        persist_miner_output(conn, run_id=rid, asin="B0TARGET01", report=first)
        second = MinerReport.model_validate(
            {"complaints": [{"theme": "new", "quote_review_ids": ids[:3]}]}
        )
        persist_miner_output(conn, run_id=rid, asin="B0TARGET01", report=second)
    with get_connection() as conn:
        themes = repository.get_review_themes(conn, "B0TARGET01")
    assert [t["theme"] for t in themes] == ["new"]  # stale replaced, not accumulated


def test_run_review_miner_end_to_end(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = seed.new_run(conn)
        ids = vs.seed_reviews(conn, rid, "B0TARGET01", n=40)
    payload = miner_payload(complaint_ids=ids[:6], feature_ids=ids[:4], bundle_ids=ids[:3])
    llm = build_llm(FakeLlmTransport([json_body(payload)]))
    with get_connection() as conn:
        run = run_review_miner(conn, llm, target_asin="B0TARGET01", competitor_asins=[], config=AC)
    assert run.result.ok
    assert run.sample_size == 40
    assert run.result.output is not None


# ===========================================================================
# Strategist
# ===========================================================================
def test_derive_concurrence_maps_outcomes() -> None:
    from delium.agents.runner import AgentResult

    ok_buy = AgentResult(
        "ok",
        StrategistVerdict.model_validate(fake.strategist_payload(verdict="buy")),
        "m",
        "anthropic",
        "frontier",
        0.1,
        1,
        1,
        0,
        3,
        None,
    )
    ok_avoid = AgentResult(
        "ok",
        StrategistVerdict.model_validate(fake.strategist_payload(verdict="avoid")),
        "m",
        "anthropic",
        "frontier",
        0.1,
        1,
        1,
        0,
        3,
        None,
    )
    failed = AgentResult("failed", None, "m", "anthropic", "frontier", 0.0, 0, 0, 0, 0, "err")
    assert derive_concurrence(ok_buy) is StrategistConcurrence.CONCUR
    assert derive_concurrence(ok_avoid) is StrategistConcurrence.DISSENT
    assert derive_concurrence(failed) is StrategistConcurrence.UNAVAILABLE


def test_strategist_provider_failure_is_unavailable() -> None:
    from delium.analysis.scoring import score_opportunity

    scored = score_opportunity(_min_scoring_input(), CFG)
    result = run_strategist(
        build_llm(FakeLlmTransport([http(500)])),
        scored=scored,
        inp=_min_scoring_input(),
        miner_report=None,
        config=CFG,
    )
    assert not result.ok
    assert derive_concurrence(result) is StrategistConcurrence.UNAVAILABLE


def test_strategist_valid_verdict() -> None:
    from delium.analysis.scoring import score_opportunity

    scored = score_opportunity(_min_scoring_input(), CFG)
    result = run_strategist(
        build_llm(FakeLlmTransport([json_body(fake.strategist_payload(verdict="test"))])),
        scored=scored,
        inp=_min_scoring_input(),
        miner_report=None,
        config=CFG,
    )
    assert result.ok and result.output is not None
    assert result.output.verdict == "test"


# ---------------------------------------------------------------------------
def _min_scoring_input():  # type: ignore[no-untyped-def]
    from delium.analysis.models import ScoringInput

    return ScoringInput()


def test_agents_disabled_config_defaults() -> None:
    # Master switch + per-agent switches exist and default on.
    assert AgentsConfig().enabled is True
    assert AgentsConfig().review_miner_enabled is True
    assert AgentsConfig().strategist_enabled is True


def test_agents_layer_does_not_import_validation_or_cli() -> None:
    import ast

    import delium.agents as agents_pkg

    root = Path(agents_pkg.__file__).parent
    forbidden = ("delium.validation", "delium.discovery", "delium.cli", "delium.reports")
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


def test_miner_context_is_deterministic_and_untrusted_wrapped(initialized_db: Path) -> None:
    from delium.agents.miner import build_miner_context

    with get_connection() as conn:
        rid = seed.new_run(conn)
        fid = repository.insert_raw_fetch(
            conn,
            run_id=rid,
            provider="reviews",
            endpoint="unwrangle",
            request_key="reviews:asin:B0TARGET01",
            payload={},
        )
        repository.upsert_product(conn, asin="B0TARGET01", fetch_id=fid)
        # Bodies with repeated bigrams so the deterministic hint scaffolding fires.
        for i in range(6):
            repository.insert_review(
                conn,
                review_id=f"B0TARGET01-R{i}",
                asin="B0TARGET01",
                fetch_id=fid,
                stars=(i % 5) + 1,
                body="the lid cracks when frozen and the lid leaks badly",
            )
        rows = repository.get_reviews_for_asin(conn, "B0TARGET01")
    ctx1 = build_miner_context("B0TARGET01", {"B0TARGET01": rows}, listing_rating_avg=4.5)
    ctx2 = build_miner_context("B0TARGET01", {"B0TARGET01": rows}, listing_rating_avg=4.5)
    assert ctx1 == ctx2  # deterministic
    assert "<customer_text" in ctx1  # untrusted-text firewall wrapping
    assert "rating_bias_delta" in ctx1  # bias meta the model must acknowledge
    assert "the lid" in ctx1  # recurring bigram surfaced in deterministic_hints
