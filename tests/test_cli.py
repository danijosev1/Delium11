from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from delium import __version__
from delium.cli.main import app

runner = CliRunner()


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    # Click's no_args_is_help exits 0 on older versions, 2 on newer ones —
    # either way it must print usage, not crash.
    assert result.exit_code in (0, 2)
    assert "discover" in result.output
    assert "validate" in result.output


def test_help_flag() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Delium" in result.output or "delium" in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_discover_requires_some_input(initialized_db: Path) -> None:
    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 1
    assert "Nothing to discover" in result.output


def test_discover_unknown_marketplace(initialized_db: Path) -> None:
    result = runner.invoke(app, ["discover", "tray", "-m", "ZZ"])
    assert result.exit_code == 1
    assert "Unknown marketplace" in result.output


def test_discover_keyword_ranks_over_seeded_data(initialized_db: Path) -> None:
    import discovery_support as seed
    from delium.database import get_connection

    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("A1", "A2", "A3"))
    result = runner.invoke(app, ["discover", seed.SEED, "-m", "US"])
    assert result.exit_code == 0
    assert "Discovery" in result.output
    assert "Buy/Test/Avoid" in result.output  # discovery-signal disclaimer
    assert "discovered 3" in result.output
    # Discovery must never present a BUY at this tier.
    assert "BUY" not in result.output


def test_discover_reports_hard_kills(initialized_db: Path) -> None:
    import discovery_support as seed
    from delium.database import get_connection

    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("C1", "C2"), price_cents=800)
    result = runner.invoke(app, ["discover", seed.SEED, "-m", "US"])
    assert result.exit_code == 0
    assert "Eliminated by hard kills" in result.output
    assert "K1" in result.output


def test_validate_command_runs_pipeline(isolated_env: Path) -> None:
    # Implemented: with no provider and nothing cached, it runs the pipeline and
    # reports a clean status rather than the old "not implemented" stub.
    result = runner.invoke(app, ["validate", "B0EXAMPLE1"])
    assert result.exit_code == 1
    assert "not implemented" not in result.output
    assert "Validation" in result.output


def test_validate_accepts_optional_overrides(isolated_env: Path) -> None:
    result = runner.invoke(app, ["validate", "B0EXAMPLE1", "--cogs", "4.20", "--freight", "1.10"])
    assert result.exit_code == 1  # missing product (no provider/cache), but overrides parsed
    assert "not implemented" not in result.output


def test_pains_command_is_wired_but_unimplemented(isolated_env: Path) -> None:
    result = runner.invoke(app, ["pains", "B0EXAMPLE1"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_watch_command_is_wired_but_unimplemented(isolated_env: Path) -> None:
    result = runner.invoke(app, ["watch"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_portfolio_command_is_wired_but_unimplemented(isolated_env: Path) -> None:
    result = runner.invoke(app, ["portfolio"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_bad_config_fails_fast(isolated_env: Path) -> None:
    (isolated_env / "config.toml").write_text("not [valid toml")
    result = runner.invoke(app, ["portfolio"])
    assert result.exit_code == 1
    assert "Configuration error" in result.output


def test_db_init_creates_database(isolated_env: Path) -> None:
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0
    assert "Database ready" in result.output
    assert (isolated_env / "data" / "delium.db").exists()


def test_db_init_is_idempotent(isolated_env: Path) -> None:
    first = runner.invoke(app, ["db", "init"])
    second = runner.invoke(app, ["db", "init"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "up to date" in second.output


def test_db_status_before_and_after_init(isolated_env: Path) -> None:
    before = runner.invoke(app, ["db", "status"])
    assert before.exit_code == 1
    assert "No database yet" in before.output

    runner.invoke(app, ["db", "init"])
    after = runner.invoke(app, ["db", "status"])
    assert after.exit_code == 0
    assert "applied" in after.output


def test_fetch_product_without_api_key_errors(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DELIUM_KEEPA_API_KEY", raising=False)
    result = runner.invoke(app, ["fetch", "product", "B08EXAMPLE"])
    assert result.exit_code == 1
    assert "Provider error" in result.output


def test_fetch_product_displays_summary(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import delium.cli.main as cli_main
    from keepa_support import DEFAULT_ASIN, FakeTransport, keepa_product_body, ok

    monkeypatch.setenv("DELIUM_KEEPA_API_KEY", "test-key")
    transport = FakeTransport([ok(keepa_product_body())])

    def fake_from_env(**_kwargs: object) -> object:
        from delium.providers.keepa import KeepaClient

        return KeepaClient("test-key", transport=transport, sleep=lambda _: None)

    monkeypatch.setattr(cli_main.KeepaClient, "from_env", staticmethod(fake_from_env))

    result = runner.invoke(app, ["fetch", "product", DEFAULT_ASIN])
    assert result.exit_code == 0
    assert DEFAULT_ASIN in result.output
    assert "Test Silicone Tray" in result.output
    assert "$20.99" in result.output  # latest price rendered from cents


def test_fetch_keywords_without_creds_errors(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DELIUM_DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DELIUM_DATAFORSEO_PASSWORD", raising=False)
    result = runner.invoke(app, ["fetch", "keywords", "silicone baby food tray"])
    assert result.exit_code == 1
    assert "Provider error" in result.output


def test_fetch_keywords_displays_summary(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import delium.cli.main as cli_main
    from dataforseo_support import SEED, FakePostTransport, ok, related_body, serp_body, volume_body

    monkeypatch.setenv("DELIUM_DATAFORSEO_LOGIN", "l")
    monkeypatch.setenv("DELIUM_DATAFORSEO_PASSWORD", "p")
    transport = FakePostTransport([ok(volume_body(9400)), ok(related_body()), ok(serp_body())])

    def fake_from_env(**_kwargs: object) -> object:
        from delium.providers.dataforseo import DataForSeoClient

        return DataForSeoClient("l", "p", transport=transport, sleep=lambda _: None)

    monkeypatch.setattr(cli_main.DataForSeoClient, "from_env", staticmethod(fake_from_env))

    result = runner.invoke(app, ["fetch", "keywords", SEED])
    assert result.exit_code == 0
    assert "9,400" in result.output  # seed volume
    assert "freezer tray silicone" in result.output  # a related keyword
    assert "B0AAA00001" in result.output  # a SERP ASIN


def test_fetch_reviews_without_creds_errors(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DELIUM_UNWRANGLE_API_KEY", raising=False)
    monkeypatch.delenv("DELIUM_APIFY_API_TOKEN", raising=False)
    result = runner.invoke(app, ["fetch", "reviews", "B08EXAMPLE"])
    assert result.exit_code == 1
    assert "Provider error" in result.output


def test_fetch_reviews_displays_summary(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import delium.cli.main as cli_main
    from reviews_support import ASIN, FakeGetTransport, get_resp, unwrangle_body

    monkeypatch.setenv("DELIUM_UNWRANGLE_API_KEY", "key")
    transport = FakeGetTransport([get_resp(unwrangle_body(3))])

    def fake_build(**_kwargs: object) -> object:
        from delium.providers.reviews import ReviewProviderChain, UnwrangleClient

        return ReviewProviderChain(
            [UnwrangleClient("key", transport=transport, sleep=lambda _: None)]
        )

    monkeypatch.setattr(cli_main, "build_review_provider", fake_build)

    result = runner.invoke(app, ["fetch", "reviews", ASIN])
    assert result.exit_code == 0
    assert "Total reviews: 3" in result.output
    assert "Average rating:" in result.output
    assert "Newest review: 2026-07-03" in result.output


# --- cross-market command -------------------------------------------------
def _seed_cross_market_db() -> None:
    import cross_market_support as seed
    from delium.database import get_connection

    with get_connection() as conn:
        run = seed.new_run(conn)
        seed.seed_strong_source(conn, run, "USASIN1")
        seed.seed_keyword_volume(conn, run, "AU", seed._SEED, 8000)
        seed.seed_serp(conn, run, "AU", seed._SEED, ["AUWEAK"])
        seed.seed_product(conn, run, "AUWEAK", "AU", reviews=40)


def test_cross_market_unknown_marketplace(initialized_db: Path) -> None:
    result = runner.invoke(app, ["cross-market", "US", "ZZ"])
    assert result.exit_code == 1
    assert "Unknown marketplace" in result.output


def test_cross_market_requires_target(initialized_db: Path) -> None:
    result = runner.invoke(app, ["cross-market", "US"])
    assert result.exit_code == 1
    assert "target" in result.output.lower()


def test_cross_market_single_target(initialized_db: Path) -> None:
    _seed_cross_market_db()
    result = runner.invoke(app, ["cross-market", "US", "AU"])
    assert result.exit_code == 0
    assert "Cross-market discovery" in result.output
    assert "Buy/Test/Avoid verdict" in result.output  # discovery-signal disclaimer
    assert "US → AU" in result.output
    assert "USASIN1" in result.output


def test_cross_market_multi_target(initialized_db: Path) -> None:
    _seed_cross_market_db()
    result = runner.invoke(app, ["cross-market", "US", "--targets", "AU,IN"])
    assert result.exit_code == 0
    assert "US → AU" in result.output
    assert "US → IN" in result.output  # IN never looked up → insufficient


def test_cross_market_rejects_both_target_and_targets(initialized_db: Path) -> None:
    result = runner.invoke(app, ["cross-market", "US", "AU", "--targets", "IN"])
    assert result.exit_code == 1
    assert "not both" in result.output


def test_cross_market_no_candidates_message(initialized_db: Path) -> None:
    result = runner.invoke(app, ["cross-market", "US", "AU"])
    assert result.exit_code == 0
    assert "No qualifying candidates" in result.output


def test_cross_market_persists_match(initialized_db: Path) -> None:
    _seed_cross_market_db()
    runner.invoke(app, ["cross-market", "US", "AU"])
    from delium.database import get_connection, repository

    with get_connection() as conn:
        matches = repository.get_matches_for_source(
            conn, source_asin="USASIN1", source_marketplace="US"
        )
    assert len(matches) == 1
    assert matches[0]["target_marketplace"] == "AU"


def test_cross_market_invalid_maturity(initialized_db: Path) -> None:
    result = runner.invoke(app, ["cross-market", "US", "AU", "--min-source-maturity", "bogus"])
    assert result.exit_code == 1
    assert "Unknown maturity" in result.output


def test_cross_market_target_same_as_source(initialized_db: Path) -> None:
    result = runner.invoke(app, ["cross-market", "US", "US"])
    assert result.exit_code == 1
    assert "differs from the source" in result.output


def test_cross_market_force_without_credentials_degrades(
    initialized_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cross_market_db()
    monkeypatch.delenv("DELIUM_KEEPA_API_KEY", raising=False)
    monkeypatch.delenv("DELIUM_DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DELIUM_DATAFORSEO_PASSWORD", raising=False)
    result = runner.invoke(app, ["cross-market", "US", "AU", "--force"])
    assert result.exit_code == 0
    assert "refresh skipped" in result.output  # no creds → degrade, still discovers
    assert "US → AU" in result.output


def test_fetch_product_marketplace_option_validated(initialized_db: Path) -> None:
    result = runner.invoke(app, ["fetch", "product", "B0X", "-m", "ZZ"])
    assert result.exit_code == 1
    assert "Unknown marketplace" in result.output


def test_cross_market_force_refresh_calls_providers(
    initialized_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --force with working (mocked) providers must refetch source + per-market
    # keyword data — the CLI → provider → cache marketplace flow.
    import delium.cli.main as cli_main
    from dataforseo_support import FakePostTransport, ok, related_body, serp_body, volume_body
    from delium.providers.dataforseo import DataForSeoClient
    from delium.providers.keepa import KeepaClient
    from keepa_support import FakeTransport, keepa_product_body
    from keepa_support import ok as kok

    _seed_cross_market_db()  # seeds source product + source seed/serp
    monkeypatch.setenv("DELIUM_KEEPA_API_KEY", "k")
    monkeypatch.setenv("DELIUM_DATAFORSEO_LOGIN", "l")
    monkeypatch.setenv("DELIUM_DATAFORSEO_PASSWORD", "p")

    def fake_keepa(**kwargs: object) -> object:
        mp = str(kwargs.get("marketplace", "US"))
        return KeepaClient(
            "k",
            transport=FakeTransport([kok(keepa_product_body("USASIN1"))]),
            sleep=lambda _: None,
            marketplace=mp,
        )

    def fake_dfs(**kwargs: object) -> object:
        mp = str(kwargs.get("marketplace", "US"))
        transport = FakePostTransport(
            [ok(volume_body(9000)), ok(related_body()), ok(serp_body())] * 2
        )
        return DataForSeoClient("l", "p", transport=transport, sleep=lambda _: None, marketplace=mp)

    monkeypatch.setattr(cli_main.KeepaClient, "from_env", staticmethod(fake_keepa))
    monkeypatch.setattr(cli_main.DataForSeoClient, "from_env", staticmethod(fake_dfs))

    result = runner.invoke(app, ["cross-market", "US", "AU", "--force"])
    assert result.exit_code == 0
    assert "US → AU" in result.output


def test_discover_keyword_hydrates_via_mocked_providers(
    initialized_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercises the CLI keyword-expansion + hydration path (CLI → ingestion →
    # provider), cache-first, with mocked providers.
    import delium.cli.main as cli_main
    from dataforseo_support import FakePostTransport, related_body, serp_body, volume_body
    from dataforseo_support import ok as dfs_ok
    from delium.providers.dataforseo import DataForSeoClient
    from delium.providers.keepa import KeepaClient
    from keepa_support import FakeTransport, keepa_product_body
    from keepa_support import ok as kok

    monkeypatch.setenv("DELIUM_KEEPA_API_KEY", "k")
    monkeypatch.setenv("DELIUM_DATAFORSEO_LOGIN", "l")
    monkeypatch.setenv("DELIUM_DATAFORSEO_PASSWORD", "p")

    def fake_keepa(**kwargs: object) -> object:
        mp = str(kwargs.get("marketplace", "US"))
        return KeepaClient(
            "k",
            transport=FakeTransport([kok(keepa_product_body("B0AAA00001"))]),
            sleep=lambda _: None,
            marketplace=mp,
        )

    def fake_dfs(**kwargs: object) -> object:
        mp = str(kwargs.get("marketplace", "US"))
        transport = FakePostTransport(
            [dfs_ok(volume_body(9000)), dfs_ok(related_body()), dfs_ok(serp_body())] * 3
        )
        return DataForSeoClient("l", "p", transport=transport, sleep=lambda _: None, marketplace=mp)

    monkeypatch.setattr(cli_main.KeepaClient, "from_env", staticmethod(fake_keepa))
    monkeypatch.setattr(cli_main.DataForSeoClient, "from_env", staticmethod(fake_dfs))

    result = runner.invoke(app, ["discover", "silicone baby food tray", "-m", "US"])
    assert result.exit_code == 0
    assert "Discovery" in result.output


# =========================================================================
# validate command (deterministic, over cached data — no providers)
# =========================================================================
def test_validate_scores_over_cached_data(initialized_db: Path) -> None:
    import discovery_support as seed
    import validation_support as vs
    from delium.database import get_connection

    with get_connection() as conn:
        rid = seed.new_run(conn)
        seed.seed_keyword_market(conn, rid, "US", asins=("B0VALID001", "B0VALID002"))
        vs.seed_reviews(conn, rid, "B0VALID001", n=40)
    result = runner.invoke(app, ["validate", "B0VALID001", "-m", "US"])
    assert result.exit_code == 0
    assert "## Verdict" in result.output
    assert "## Pillars" in result.output
    # Review Miner did not run (no LLM client) → shown as such, not faked.
    assert "Review Miner" in result.output and "not run" in result.output
    # G5 (LLM Strategist) must be shown as pending, never resolved here.
    assert "G5" in result.output and "pending" in result.output
    # Discovery/validate-tier data without a Strategist must not present a BUY.
    assert "**BUY**" not in result.output


def test_validate_unknown_marketplace(initialized_db: Path) -> None:
    result = runner.invoke(app, ["validate", "B0VALID001", "-m", "ZZ"])
    assert result.exit_code == 1
    assert "Unknown marketplace" in result.output


def test_validate_missing_product_reports_no_verdict(initialized_db: Path) -> None:
    result = runner.invoke(app, ["validate", "B0GHOST999", "-m", "US"])
    assert result.exit_code == 1
    assert "No verdict produced" in result.output


def test_validate_hard_kill_shows_kill(initialized_db: Path) -> None:
    import discovery_support as seed
    from delium.database import get_connection

    with get_connection() as conn:
        rid = seed.new_run(conn)
        # $8 median price is below the floor → K1 kills cheaply.
        seed.seed_keyword_market(conn, rid, "US", asins=("B0KILL0001",), price_cents=800)
    result = runner.invoke(app, ["validate", "B0KILL0001", "-m", "US"])
    assert result.exit_code == 0
    assert "Hard kill" in result.output


def test_validate_bad_dims_option_is_rejected(initialized_db: Path) -> None:
    result = runner.invoke(app, ["validate", "B0VALID001", "--dims", "10x20"])
    assert result.exit_code == 1
    assert "L×W×H" in result.output
