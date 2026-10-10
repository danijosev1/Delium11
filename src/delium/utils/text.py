"""Deterministic text-matching helpers shared by the agent layer and the
evidence-assembly layer.

Kept dependency-free (no agents, no analysis, no I/O) so both the Analyst
feature-extraction guard and the differentiation-evidence derivation can share
one definition of "is this feature observably present in this text" without one
layer importing the other.
"""

from __future__ import annotations

import re


def normalize(text: str | None) -> str:
    """Lowercase, replace non-alphanumerics with spaces, collapse whitespace."""
    if not text:
        return ""
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


# Colours, bare measurement units, and pack/count connector words that a product
# title carries as noise around its core noun phrase. Deterministic and
# marketplace-agnostic — used to derive a search keyword from a title when a
# reverse-ASIN / SERP phrase is unavailable (e.g. non-US, where DataForSEO Labs
# does not cover the marketplace). See `title_to_keyword`.
_COLOURS = frozenset(
    {
        "black", "white", "red", "blue", "green", "yellow", "orange", "purple",
        "pink", "grey", "gray", "brown", "beige", "silver", "gold", "golden",
        "navy", "teal", "maroon", "cream", "tan", "ivory", "clear", "transparent",
        "multicolor", "multicolour", "rose", "charcoal", "khaki", "turquoise",
        "burgundy", "lavender", "mint", "coral", "bronze", "copper",
    }
)  # fmt: skip
_UNITS = frozenset(
    {
        "oz", "ml", "l", "g", "kg", "lb", "lbs", "mm", "cm", "m", "in", "inch",
        "inches", "ft", "pcs", "pc", "pk", "pack", "packs", "ct", "count", "counts",
        "mah", "wh", "w", "v", "gb", "tb", "mb", "qt", "gal", "pt", "fl",
    }
)  # fmt: skip
# Connector / count words that are never the product noun themselves.
_CONNECTORS = frozenset(
    {
        "of", "with", "for", "and", "the", "a", "an", "plus", "piece", "pieces",
        "size", "sized", "value", "assorted", "x",
    }
)  # fmt: skip
# A single number, or a number glued to a unit (10000mah, 20oz, 12x16, 1.5l).
_MEASUREMENT_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)*"
    r"(?:oz|ml|l|g|kg|lb|lbs|mm|cm|m|in|inch|inches|ft|pcs|pc|pk|pack|ct|count|"
    r"mah|wh|w|v|gb|tb|mb|qt|gal|pt)?$"
)


def title_to_keyword(
    title: str | None, brand: str | None = None, *, max_words: int = 4
) -> str | None:
    """Derive a deterministic search keyword from a product title — the core noun
    phrase with brand, sizes, colours and pack counts stripped.

    Pure (stdlib only) so both discovery and ingestion can reuse one definition.
    Used to seed the Merchant Amazon SERP for a competitor set when a reverse-ASIN
    / prior-SERP phrase is unavailable (DataForSEO Labs is US-only). Returns None
    only when the title is empty; otherwise it always yields a non-empty phrase
    (falling back to the first words of the normalized title if every token looked
    like noise)."""
    norm = normalize(title)
    if not norm:
        return None
    brand_tokens = set(normalize(brand).split()) if brand else set()
    kept: list[str] = []
    for tok in norm.split():
        if tok in brand_tokens or tok in _COLOURS or tok in _UNITS or tok in _CONNECTORS:
            continue
        if _MEASUREMENT_RE.match(tok):  # pure number or number+unit (20oz, 12x16)
            continue
        if len(tok) < 2:  # stray single letters left after splitting (e.g. "x")
            continue
        kept.append(tok)
        if len(kept) >= max_words:
            break
    if kept:
        return " ".join(kept)
    # Everything looked like noise — fall back to the title minus the brand so a
    # seed still exists (better a weak keyword than no competitor set at all).
    fallback = [t for t in norm.split() if t not in brand_tokens][:max_words]
    return " ".join(fallback) or norm.split()[0]


def feature_present(feature: str, text: str, threshold: float) -> bool:
    """True when `feature` is observably present in `text`. A normalized-substring
    match wins outright; otherwise the share of the feature's word tokens present
    in the text must reach `threshold`.

    The threshold tunes strictness: a high value (extraction guard) keeps
    hallucinated features out of the matrix; a lower value (present-matching a
    requested feature against a competitor listing) is deliberately generous so a
    plausible match is read as PRESENT — never as a manufactured competitor gap."""
    nf = normalize(feature)
    nt = normalize(text)
    if not nf or not nt:
        return False
    if nf in nt:
        return True
    ftokens = nf.split()  # a non-empty normalized string always yields ≥1 token
    haystack = set(nt.split())
    hits = sum(1 for tok in ftokens if tok in haystack)
    return hits / len(ftokens) >= threshold
