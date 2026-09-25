"""Tests for the UI's non-UI helper modules (credentials, costs, format).

No Streamlit and no network: these cover credential status, cache-aware cost
estimates (using the same raw_fetches cache the CLI uses), the pure display
formatters, and the two read-only repository list helpers the History page uses.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from delium.config.models import DeliumConfig
from delium.config.secrets import DeliumSecrets
from delium.database import repository
from delium.database.connection import get_connection
from delium.ingestion.keywords import keyword_request_key
from delium.providers.dataforseo import normalize_phrase
from delium.ui import costs, format, services
from delium.ui.credentials import _status_from, is_configured

CFG = DeliumConfig()


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------
def test_status_reflects_configured_providers_without_exposing_values() -> None:
    secrets = DeliumSecrets(
        keepa_api_key=None,
        dataforseo_login="login",
        dataforseo_password="pw",
        unwrangle_api_key="u",
        apify_api_token=None,
        llm_api_key=None,
    )
    status = {p.key: p for p in _status_from(secrets)}
    assert status["keepa"].configured is False
    assert status["dataforseo"].configured is True  # both login+password present
    assert status["reviews"].configured is True  # unwrangle alone is enough
    assert status["llm"].configured is False
    # Never leak a value.
    for p in status.values():
        assert "login" not in p.enables and "pw" not in p.enables


def test_dataforseo_needs_both_login_and_password() -> None:
    secrets = DeliumSecrets(dataforseo_login="login", dataforseo_password=None)
    status = {p.key: p for p in _status_from(secrets)}
    assert status["dataforseo"].configured is False


# ---------------------------------------------------------------------------
# costs — cache-aware estimates
# ---------------------------------------------------------------------------
def _seed_fetch(conn, provider: str, request_key: str) -> None:  # type: ignore[no-untyped-def]
    run_id = repository.insert_run(conn, command="test", input_="x")
    repository.insert_raw_fetch(
        conn, run_id=run_id, provider=provider, endpoint="e", request_key=request_key, payload={}
    )


def test_keyword_estimate_uncached_charges_for_three_calls(initialized_db: Path) -> None:
    with get_connection() as conn:
        est = costs.keyword_estimate(conn, "US", "brand new seed", CFG, force=False)
    assert est.fully_cached is False
    assert est.providers == ("DataForSEO",)
    assert est.est_high_usd > 0


def test_keyword_estimate_fully_cached_is_free(initialized_db: Path) -> None:
    seed = normalize_phrase("silicone baby food tray")
    with get_connection() as conn:
        for endpoint in ("volume", "related", "serp"):
            _seed_fetch(conn, "dataforseo", keyword_request_key("US", endpoint, seed))
        est = costs.keyword_estimate(conn, "US", seed, CFG, force=False)
    assert est.fully_cached is True
    assert est.est_high_usd == 0.0
    assert est.providers == ()


def test_keyword_estimate_force_ignores_cache(initialized_db: Path) -> None:
    seed = normalize_phrase("silicone baby food tray")
    with get_connection() as conn:
        for endpoint in ("volume", "related", "serp"):
            _seed_fetch(conn, "dataforseo", keyword_request_key("US", endpoint, seed))
        est = costs.keyword_estimate(conn, "US", seed, CFG, force=True)
    assert est.fully_cached is False


def test_product_estimate_cache_hit_and_marginal_zero(initialized_db: Path) -> None:
    with get_connection() as conn:
        miss = costs.product_estimate(conn, "US", "B0AAA00001", CFG, force=False)
        _seed_fetch(conn, "keepa", "keepa:product:US:B0AAA00001")
        hit = costs.product_estimate(conn, "US", "B0AAA00001", CFG, force=False)
    assert miss.fully_cached is False and miss.providers == ("Keepa",)
    assert miss.est_high_usd == 0.0  # Keepa is subscription — $0 marginal
    assert hit.fully_cached is True


def test_validate_estimate_lists_only_configured_providers() -> None:
    est = costs.validate_estimate(CFG, keepa=True, dataforseo=True, reviews=False, llm=False)
    assert "Keepa" in est.providers and "DataForSEO" in est.providers
    assert "Review provider" not in est.providers and "LLM" not in est.providers
    with_paid = costs.validate_estimate(CFG, keepa=True, dataforseo=True, reviews=True, llm=True)
    assert with_paid.est_high_usd > 0  # reviews + LLM budget ceilings


# ---------------------------------------------------------------------------
# format — pure display transforms
# ---------------------------------------------------------------------------
def test_verdict_style_colours() -> None:
    assert format.verdict_style("buy") == ("BUY", "#1a7f37")
    assert format.verdict_style("avoid")[0] == "AVOID"
    assert format.verdict_style("strong_opportunity")[0] == "STRONG OPPORTUNITY"
    assert format.verdict_style("mystery") == ("MYSTERY", "#57606a")  # graceful fallback


def test_history_series_aligns_and_converts_units() -> None:
    rows = [
        {"captured_on": "2026-01-01", "price_cents": 2599, "bsr": 1500},
        {"captured_on": "2026-02-01", "price_cents": None, "bsr": 1200},
    ]
    series = format.history_series(rows)
    assert series["date"] == ["2026-01-01", "2026-02-01"]
    assert series["price_usd"] == [25.99, None]
    assert series["bsr"] == [1500, 1200]


def test_related_and_serp_rows_sort_and_shape() -> None:
    result = SimpleNamespace(
        related=[
            SimpleNamespace(phrase="a", volume=10),
            SimpleNamespace(phrase="b", volume=None),
            SimpleNamespace(phrase="c", volume=90),
        ],
        serp=[
            SimpleNamespace(position=2, asin="B2", sponsored=True, price_cents=None, title="t2"),
            SimpleNamespace(position=1, asin="B1", sponsored=False, price_cents=2199, title="t1"),
        ],
    )
    related = format.related_rows(result)
    assert [r["keyword"] for r in related] == ["c", "a", "b"]  # volume desc, None last
    serp = format.serp_rows(result)
    assert [s["position"] for s in serp] == [1, 2]  # position asc
    assert serp[0]["price_usd"] == 21.99


def test_pillar_kill_gate_rows() -> None:
    scored = SimpleNamespace(
        pillars=[
            SimpleNamespace(
                pillar="demand",
                raw_score=80.0,
                capped_score=80.0,
                weight=25,
                weighted_contribution=20.0,
                confidence=SimpleNamespace(value="high"),
                partial=False,
                available=True,
            )
        ],
        kills=[
            SimpleNamespace(
                rule_id="K1",
                name="price floor",
                kills=True,
                assessed=True,
                triggered=True,
                demoted=False,
                actual="$8",
                threshold="$15",
                reason="too cheap",
            ),
            SimpleNamespace(
                rule_id="K2",
                name="ok",
                kills=False,
                assessed=True,
                triggered=False,
                demoted=False,
                actual="",
                threshold="",
            ),
        ],
        gates=[
            SimpleNamespace(
                gate_id="G5", name="strategist", passed=None, hard=False, actual="pending"
            ),
        ],
    )
    pillars = format.pillar_rows(scored)
    assert pillars[0]["status"] == "ok" and pillars[0]["contribution"] == 20.0
    kills = format.kill_rows(scored)
    assert len(kills) == 1 and kills[0]["rule"] == "K1" and kills[0]["effect"] == "KILL"
    gates = format.gate_rows(scored)
    assert gates[0]["state"] == "pending"


def test_run_and_validation_rows(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = repository.insert_run(conn, command="validate", input_="US:B0AAA00001")
        repository.finish_run(conn, rid, status="complete", data_cost_usd=0.03, llm_cost_usd=0.01)
        runs = repository.list_runs(conn, limit=10)
        vals = repository.list_validations(conn, limit=10)
    run_rows = format.run_rows(runs)
    assert run_rows and run_rows[0]["command"] == "validate"
    assert run_rows[0]["data_usd"] == 0.03
    assert format.validation_rows(vals) == []  # none persisted


def test_discovery_and_cross_market_rows() -> None:
    report = SimpleNamespace(
        ranked=[
            SimpleNamespace(
                asin="B1",
                marketplace=SimpleNamespace(value="US"),
                scored=SimpleNamespace(
                    score=72.0,
                    verdict=SimpleNamespace(value="test"),
                    confidence=SimpleNamespace(level=SimpleNamespace(value="medium")),
                    insufficient_data=False,
                ),
                candidate=SimpleNamespace(sources=[SimpleNamespace(value="serp")]),
            ),
            SimpleNamespace(
                asin="B2",
                marketplace=SimpleNamespace(value="US"),
                scored=None,
                candidate=SimpleNamespace(sources=[]),
            ),
        ]
    )
    rows = format.discovery_rows(report)
    assert len(rows) == 1 and rows[0]["asin"] == "B1" and rows[0]["via"] == "serp"

    cand = SimpleNamespace(
        source_asin="B9",
        source_marketplace=SimpleNamespace(value="US"),
        target_marketplace=SimpleNamespace(value="AU"),
        report=SimpleNamespace(
            verdict=SimpleNamespace(value="opportunity_to_validate"),
            score=61.0,
            confidence=SimpleNamespace(level=SimpleNamespace(value="low")),
            match=SimpleNamespace(
                source=SimpleNamespace(title="Widget"),
                confidence=SimpleNamespace(value="fuzzy"),
            ),
            target_evidence=SimpleNamespace(
                presence=SimpleNamespace(value="underpenetrated"),
                target_demand_score=55.0,
                demand_credible=True,
            ),
            market_gap=SimpleNamespace(competition_gap=40.0),
        ),
    )
    cm = format.cross_market_rows([cand])
    assert cm[0]["route"] == "US→AU" and cm[0]["title"] == "Widget"


# ---------------------------------------------------------------------------
# costs / credentials — extra pure branches
# ---------------------------------------------------------------------------
def test_cost_estimate_is_paid_and_discover_estimate() -> None:
    free = costs.CostEstimate((), 0.0, 0.0, fully_cached=True)
    paid = costs.CostEstimate(("DataForSEO",), 0.02, 0.03, fully_cached=False)
    assert free.is_paid is False and paid.is_paid is True
    est = costs.discover_estimate(CFG, keyword_count=2, dataforseo=True, keepa=False)
    assert "DataForSEO" in est.providers and est.est_high_usd > 0


def test_is_configured_uses_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DELIUM_DATAFORSEO_LOGIN", "x")
    monkeypatch.setenv("DELIUM_DATAFORSEO_PASSWORD", "y")
    monkeypatch.delenv("DELIUM_KEEPA_API_KEY", raising=False)
    assert is_configured("dataforseo") is True
    assert is_configured("keepa") is False


# ---------------------------------------------------------------------------
# services — reachable without any network (DB-only + no-credential paths)
# ---------------------------------------------------------------------------
def test_services_history_reads(initialized_db: Path) -> None:
    with get_connection() as conn:
        rid = repository.insert_run(conn, command="validate", input_="US:B0AAA00001")
        repository.finish_run(conn, rid, status="complete", data_cost_usd=0.02)
    assert any(r["command"] == "validate" for r in services.recent_runs())
    assert services.recent_validations() == []
    assert services.report_files() == []  # none written yet


def test_services_cross_market_empty_db_returns_empty(initialized_db: Path) -> None:
    # discover_cross_market reads the DB only — no providers, no network.
    assert services.cross_market("US", ["AU"], CFG) == []


def test_services_keyword_research_without_creds_raises(initialized_db: Path) -> None:
    from delium.providers.base import ProviderError

    with pytest.raises(ProviderError):
        services.keyword_research("silicone baby food tray", "US", CFG, force=False)


def test_services_validate_without_keepa_degrades_no_verdict(initialized_db: Path) -> None:
    # No credentials configured (conftest isolates .env): validate must return a
    # report with no scored verdict, never crash, and make no network call.
    from delium.validation import ValidationStatus

    res = services.validate("B0AAA00001", "US", CFG)
    assert res.report.scored is None
    assert res.report.status is ValidationStatus.MISSING_CREDENTIALS
    assert isinstance(res.markdown, str) and res.markdown
