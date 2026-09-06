"""Category matching between Keepa breadcrumb paths and department-keyed tables.

Ingestion stores the FULL Keepa category breadcrumb (e.g.
``"Home & Kitchen > Kitchen & Dining > Storage"``), but the fee, velocity-curve,
and risk-rule tables are keyed by a department NAME (``"Home & Kitchen"``). A
naive ``category in table`` therefore never fires on real provider data — every
product silently falls back to the default rate/curve/no-risk, which misstates
profit, caps demand confidence, and skips category risk deductions.

These helpers match a table key against any *segment* of the breadcrumb, so a
department key resolves whether it is passed alone (as tests do) or embedded in a
full path (as real Keepa data supplies). Matching is segment-EQUALITY (after
case/whitespace normalization), never substring, so ``"Books"`` cannot match
``"Cookbooks"``. Pure: no I/O, no config, no dependency on the analysis models.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Keepa joins breadcrumbs with " > "; tolerate a few common separators.
_SPLIT = re.compile(r"[>/›»|]")


def _norm(text: str) -> str:
    return " ".join(text.strip().casefold().split())


def category_segments(category: str | None) -> tuple[str, ...]:
    """Normalized segments of a breadcrumb path (empty tuple for None/empty)."""
    if not category:
        return ()
    return tuple(seg for part in _SPLIT.split(category) if (seg := _norm(part)))


def category_matches(key: str, category: str | None) -> bool:
    """True when department `key` equals any segment of the breadcrumb `category`.
    An exact single-segment `category` (what the engine tests pass) still matches."""
    return _norm(key) in category_segments(category)


def resolve_category_key(keys: Iterable[str], category: str | None) -> str | None:
    """The first `key` (in iteration order) whose department matches `category`,
    else None. Iteration order is the table's own order, so a well-formed table
    keyed by distinct departments resolves deterministically."""
    segments = set(category_segments(category))
    for key in keys:
        if _norm(key) in segments:
            return key
    return None
