"""Loads and validates config.toml into a `DeliumConfig`.

Precedence: the on-disk config.toml supplies whatever sections it defines;
any section left out falls back to the documented defaults in
`delium.config.models` (so a brand-new checkout works with an empty or
partial config.toml, and a missing file falls back to all-defaults with a
warning rather than crashing).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import ValidationError

from delium.config.models import DeliumConfig
from delium.utils.logging import get_logger
from delium.utils.paths import get_config_path

log = get_logger(__name__)


class ConfigError(Exception):
    """Raised when config.toml exists but fails to parse or validate."""


def load_config(path: Path | None = None) -> DeliumConfig:
    """Load `DeliumConfig` from `path` (default: the standard config path).

    Raises `ConfigError` for a malformed file so a bad edit is caught at
    startup, not silently ignored mid-run.
    """
    config_path = path or get_config_path()

    if not config_path.exists():
        log.warning(
            "No config.toml found at %s — using built-in defaults for everything.",
            config_path,
        )
        return DeliumConfig()

    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc

    try:
        return DeliumConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {config_path}:\n{exc}") from exc
