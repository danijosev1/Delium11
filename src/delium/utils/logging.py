"""Logging setup for Delium.

A single call to `configure_logging()` sets up console logging for the whole
application. Level and format are controlled via environment variables so
behavior can change without editing code:

    DELIUM_LOG_LEVEL=DEBUG delium validate B0EXAMPLE1

No file handler in V1 — this is a personal CLI tool, and terminal output
(optionally redirected by the shell) is enough.
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False


def configure_logging(level: str | None = None) -> None:
    """Configure the root `delium` logger. Safe to call more than once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved_level = (level or os.environ.get("DELIUM_LOG_LEVEL") or "INFO").upper()

    logger = logging.getLogger("delium")
    logger.setLevel(resolved_level)

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.propagate = False

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger under the `delium` hierarchy.

    Usage: `log = get_logger(__name__)`
    """
    configure_logging()
    return logging.getLogger(f"delium.{name}" if not name.startswith("delium") else name)
