from __future__ import annotations

from pathlib import Path

from delium.utils import paths


def test_defaults_resolve_under_project_root() -> None:
    assert paths.get_config_path().name == "config.toml"
    assert paths.get_data_dir().name == "data"
    assert paths.get_reports_dir().name == "reports"
    assert paths.get_database_path().parent == paths.get_data_dir()


def test_env_overrides_are_respected(isolated_env: Path) -> None:
    assert paths.get_config_path() == isolated_env / "config.toml"
    assert paths.get_data_dir() == isolated_env / "data"
    assert paths.get_reports_dir() == isolated_env / "reports"


def test_ensure_directories_creates_data_and_reports(isolated_env: Path) -> None:
    assert not (isolated_env / "data").exists()
    assert not (isolated_env / "reports").exists()

    paths.ensure_directories()

    assert (isolated_env / "data").is_dir()
    assert (isolated_env / "reports").is_dir()
