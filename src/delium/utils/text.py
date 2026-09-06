"""Deterministic text-matching helpers shared by the agent layer and the
evidence-assembly layer.

Kept dependency-free (no agents, no analysis, no I/O) so both the Analyst
feature-extraction guard and the differentiation-evidence derivation can share
one definition of "is this feature observably present in this text" without one
layer importing the other.
"""

from __future__ import annotations


def normalize(text: str | None) -> str:
    """Lowercase, replace non-alphanumerics with spaces, collapse whitespace."""
    if not text:
        return ""
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


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
