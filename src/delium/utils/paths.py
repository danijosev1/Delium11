"""Filesystem locations used by Delium.

Every path can be overridden with an environment variable so the tool can be
pointed at a different config/data/reports location without touching code
(e.g. when running tests, or keeping the DB on a different disk).
"""

from __future__ import annotations

import os
from pathlib import Path


def _project_root() -> Path:
    """Best-effort repository root: three levels up from this file.

    src/delium/utils/paths.py -> src/delium -> src -> <root>
    """
    return Path(__file__).resolve().parents[3]


def get_config_path() -> Path:
    """Location of the user's config.toml.

    Override with DELIUM_CONFIG_PATH. Defaults to <root>/config/config.toml.
    """
    override = os.environ.get("DELIUM_CONFIG_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return _project_root() / "config" / "config.toml"


def get_data_dir() -> Path:
    """Directory holding the SQLite database and cached artifacts.

    Override with DELIUM_DATA_DIR. Defaults to <root>/data.
    """
    override = os.environ.get("DELIUM_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return _project_root() / "data"


def get_database_path() -> Path:
    """Location of the delium.db SQLite file. Override with DELIUM_DB_PATH."""
    override = os.environ.get("DELIUM_DB_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return get_data_dir() / "delium.db"


def get_reports_dir() -> Path:
    """Directory generated markdown/HTML reports are written to.

    Override with DELIUM_REPORTS_DIR. Defaults to <root>/reports.
    """
    override = os.environ.get("DELIUM_REPORTS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return _project_root() / "reports"


def ensure_directories() -> None:
    """Create data/reports directories if they don't exist yet."""
    get_data_dir().mkdir(parents=True, exist_ok=True)
    get_reports_dir().mkdir(parents=True, exist_ok=True)
