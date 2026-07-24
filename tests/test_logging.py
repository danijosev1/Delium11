from __future__ import annotations

import logging

from delium.utils.logging import get_logger


def test_get_logger_is_namespaced_under_delium() -> None:
    log = get_logger("some.module")
    assert log.name == "delium.some.module"


def test_get_logger_does_not_double_prefix() -> None:
    log = get_logger("delium.already.prefixed")
    assert log.name == "delium.already.prefixed"


def test_root_delium_logger_has_a_handler() -> None:
    get_logger(__name__)
    root = logging.getLogger("delium")
    assert len(root.handlers) >= 1
    assert root.propagate is False
