from __future__ import annotations

from pathlib import Path

import pytest

from delium.config import ConfigError, DeliumConfig, load_config


def test_missing_config_file_falls_back_to_defaults(isolated_env: Path) -> None:
    config = load_config()
    assert isinstance(config, DeliumConfig)
    assert config.marketplace.country == "US"
    assert config.score_weights.demand == 25


def test_valid_config_overrides_defaults(isolated_env: Path) -> None:
    config_path = isolated_env / "config.toml"
    config_path.write_text(
        """
        [preferences]
        min_price = 25
        max_price = 80
        avoid = ["glass"]

        [score_weights]
        demand = 30
        competition = 20
        differentiation = 20
        profitability = 20
        risk = 10
        """
    )

    config = load_config(config_path)

    assert config.preferences.min_price == 25
    assert config.preferences.max_price == 80
    assert config.preferences.avoid == ["glass"]
    assert config.score_weights.demand == 30
    # untouched sections keep their documented defaults
    assert config.gates.min_margin == 0.30


def test_malformed_toml_raises_config_error(isolated_env: Path) -> None:
    config_path = isolated_env / "config.toml"
    config_path.write_text("this is not [valid toml")

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_unknown_key_raises_config_error(isolated_env: Path) -> None:
    config_path = isolated_env / "config.toml"
    config_path.write_text(
        """
        [preferences]
        min_price = 25
        typo_field = "oops"
        """
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_out_of_range_value_raises_config_error(isolated_env: Path) -> None:
    config_path = isolated_env / "config.toml"
    config_path.write_text(
        """
        [gates]
        min_margin = 1.5
        """
    )

    with pytest.raises(ConfigError):
        load_config(config_path)
