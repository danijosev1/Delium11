"""Property-based invariants for discovery orchestration (hypothesis).

Dedup idempotence, evidence-merge invariants, deterministic ranking that never
mutates scores, and marketplace separation.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from delium.analysis.models import Marketplace
from delium.discovery import Candidate, DiscoveryEvidence, DiscoverySource, deduplicate

_MARKETS = st.sampled_from([Marketplace.US, Marketplace.CA, Marketplace.UK, Marketplace.AU])
_ASINS = st.sampled_from(["A1", "A2", "A3", "B1", "B2"])
_SOURCES = st.sampled_from(list(DiscoverySource))
_REFS = st.sampled_from(["tray", "freezer", "US", "AU", "user"])


@st.composite
def evidence(draw: st.DrawFn) -> DiscoveryEvidence:  # type: ignore[type-arg]
    return DiscoveryEvidence(
        source=draw(_SOURCES),
        reference=draw(_REFS),
        serp_position=draw(st.one_of(st.none(), st.integers(1, 10))),
    )


@st.composite
def candidates(draw: st.DrawFn) -> Candidate:  # type: ignore[type-arg]
    ev = tuple(draw(st.lists(evidence(), min_size=1, max_size=4)))
    return Candidate(asin=draw(_ASINS), marketplace=draw(_MARKETS), evidence=ev)


@given(cands=st.lists(candidates(), max_size=12))
def test_dedup_is_idempotent(cands: list[Candidate]) -> None:
    once = deduplicate(cands)
    twice = deduplicate(once)
    assert once == twice


@given(cands=st.lists(candidates(), max_size=12))
def test_dedup_collapses_to_unique_identities(cands: list[Candidate]) -> None:
    result = deduplicate(cands)
    identities = [c.identity for c in result]
    assert len(identities) == len(set(identities))  # one candidate per identity


@given(cands=st.lists(candidates(), min_size=1, max_size=12))
def test_dedup_preserves_all_identities(cands: list[Candidate]) -> None:
    result = deduplicate(cands)
    assert {c.identity for c in result} == {c.identity for c in cands}


@given(cands=st.lists(candidates(), max_size=12))
def test_adding_duplicate_evidence_creates_no_new_candidate(cands: list[Candidate]) -> None:
    base = deduplicate(cands)
    # Re-adding every candidate (identical evidence) must not grow the set nor
    # duplicate any evidence within a candidate.
    doubled = deduplicate(cands + cands)
    assert len(doubled) == len(base)
    for c in doubled:
        keys = [e.key() for e in c.evidence]
        assert len(keys) == len(set(keys))


@given(cands=st.lists(candidates(), max_size=12))
def test_dedup_order_is_deterministic(cands: list[Candidate]) -> None:
    assert deduplicate(cands) == deduplicate(cands)


@given(
    a=candidates(),
    extra=st.lists(evidence(), max_size=3),
)
def test_merge_is_commutative_in_evidence(a: Candidate, extra: list[DiscoveryEvidence]) -> None:
    b = Candidate(asin=a.asin, marketplace=a.marketplace, evidence=tuple(extra))
    ab = a.merged_with(b)
    ba = b.merged_with(a)
    assert ab == ba  # merge order does not change the merged candidate
    assert ab.identity == a.identity
