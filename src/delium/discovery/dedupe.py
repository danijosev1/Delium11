"""Variation de-duplication by Keepa parent ASIN (pure).

Amazon models each colour/size/pack option as its own child ASIN under one
parent listing. The Product Finder returns children, so a single product can
appear as many near-identical rows (same age, reviews, BSR band). This groups
children under their parent and keeps the best-performing child as the
representative, plus a variation count — one row per real product.

Pure and deterministic: the parent lookup (a DB read) is done by the caller and
passed in as a mapping. No I/O, no scoring, no network here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ParentGroup[T]:
    """One real product: its parent listing key, the best child as representative,
    the number of variations collapsed into it, and every child ASIN."""

    parent_asin: str  # the grouping key: the Keepa parent, or the ASIN itself
    representative: T
    variation_count: int
    child_asins: tuple[str, ...]


def group_by_parent[T](
    items: Iterable[T],
    *,
    asin_of: Callable[[T], str],
    parents: Mapping[str, str | None],
    rank: Callable[[T], float],
    prefer_high: bool = True,
) -> list[ParentGroup[T]]:
    """Collapse `items` sharing a parent ASIN into one group each.

    `parents` maps a child ASIN to its parent (None / absent → the product is its
    own listing and groups under its own ASIN). Within a group the representative
    is the child with the best `rank` (highest when `prefer_high`, else lowest).
    Groups are returned best-representative first; ties break on the parent key so
    the ordering is deterministic. Never mutates the inputs.
    """
    buckets: dict[str, list[T]] = {}
    order: list[str] = []
    for item in items:
        asin = asin_of(item)
        parent = parents.get(asin) or asin
        if parent not in buckets:
            buckets[parent] = []
            order.append(parent)
        buckets[parent].append(item)

    groups: list[ParentGroup[T]] = []
    for parent in order:
        members = buckets[parent]
        representative = (max if prefer_high else min)(members, key=rank)
        groups.append(
            ParentGroup(
                parent_asin=parent,
                representative=representative,
                variation_count=len(members),
                child_asins=tuple(asin_of(m) for m in members),
            )
        )
    sign = -1.0 if prefer_high else 1.0
    groups.sort(key=lambda g: (sign * rank(g.representative), g.parent_asin))
    return groups
