from __future__ import annotations

from pathlib import Path

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
