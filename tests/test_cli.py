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


def test_discover_command_is_wired_but_unimplemented(isolated_env: Path) -> None:
    result = runner.invoke(app, ["discover", "silicone baby food tray"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_validate_command_is_wired_but_unimplemented(isolated_env: Path) -> None:
    result = runner.invoke(app, ["validate", "B0EXAMPLE1"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_validate_accepts_optional_overrides(isolated_env: Path) -> None:
    result = runner.invoke(app, ["validate", "B0EXAMPLE1", "--cogs", "4.20", "--freight", "1.10"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


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
