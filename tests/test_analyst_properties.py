"""Property-based invariants for the Analyst layer (hypothesis).

Over randomized inputs:
- the evidence check NEVER keeps a claimed feature that is not observably in its
  own listing text, and NEVER keeps an entry for an unknown ASIN — so a
  hallucinated feature or ASIN can never enter the persisted matrix;
- `normalize` is idempotent and `feature_present` is monotonic in its threshold;
- competitor absence is derived conservatively: a feature is marked absent iff no
  competitor haystack contains it — silence for a feature that IS present can
  never flip it to 'absent'.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

import agents_support as fake
from delium.agents.analyst import make_analyst_evidence_check
from delium.agents.schemas import AnalystReport
from delium.analysis.models import BundleSignal, FeatureRequest
from delium.utils.text import feature_present, normalize
from delium.validation.evidence import _derive_absence, _derive_bundle_complement

_asins = st.sampled_from(["A", "B", "C", "GHOST", "Z"])
_words = st.sampled_from(["silicone", "lid", "bamboo", "steel", "blade", "cup", "bag", "spoon"])
_phrases = st.lists(_words, min_size=1, max_size=3).map(lambda ws: " ".join(ws))
_texts = st.lists(_words, min_size=0, max_size=6).map(lambda ws: " ".join(ws))


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
@given(s=st.text())
def test_normalize_is_idempotent(s: str) -> None:
    once = normalize(s)
    assert normalize(once) == once


@given(feature=_phrases, text=_texts, lo=st.floats(0.0, 1.0), hi=st.floats(0.0, 1.0))
def test_feature_present_monotonic_in_threshold(
    feature: str, text: str, lo: float, hi: float
) -> None:
    if lo > hi:
        lo, hi = hi, lo
    # A stricter (higher) threshold implies the looser one also matches.
    if feature_present(feature, text, hi):
        assert feature_present(feature, text, lo)


# ---------------------------------------------------------------------------
# Evidence check — the extraction guard
# ---------------------------------------------------------------------------
@given(
    entries=st.lists(
        st.tuples(_asins, st.lists(_phrases, min_size=0, max_size=4)), min_size=0, max_size=5
    )
)
def test_evidence_check_keeps_only_observable_features(
    entries: list[tuple[str, list[str]]],
) -> None:
    known = frozenset({"A", "B", "C"})
    texts = {"A": "silicone lid", "B": "bamboo spoon set", "C": "stainless steel blade"}
    matrix = [{"asin": a, "claimed_features": feats} for a, feats in entries]
    report = AnalystReport.model_validate({**fake.analyst_payload(), "feature_matrix": matrix})

    cleaned, dropped, total = make_analyst_evidence_check(known, texts, 0.85)(report)

    # Every surviving entry is a known ASIN, and every surviving feature is present.
    for entry in cleaned.feature_matrix:
        assert entry.asin in known
        for feat in entry.claimed_features:
            assert feature_present(feat, texts[entry.asin], 0.85)
    # Accounting is exact: total counts every claimed feature; dropped is the rest.
    assert total == sum(len(feats) for _a, feats in entries)
    kept = sum(len(e.claimed_features) for e in cleaned.feature_matrix)
    assert kept + dropped == total


# ---------------------------------------------------------------------------
# Absence derivation — never invents a gap
# ---------------------------------------------------------------------------
@given(features=st.lists(_phrases, min_size=1, max_size=4), texts=st.lists(_texts, max_size=4))
def test_derived_absence_matches_presence(features: list[str], texts: list[str]) -> None:
    haystacks = {f"C{i}": t for i, t in enumerate(texts)}
    reqs = tuple(
        FeatureRequest(feature=f, supporting_review_ids=("r1",), absent_from_competitors=None)
        for f in features
    )
    for derived in _derive_absence(reqs, haystacks):
        present = any(feature_present(derived.feature, h, 0.6) for h in haystacks.values())
        # 'absent' (True) iff no competitor haystack contains it; present → never absent.
        assert derived.absent_from_competitors is (not present)


@given(complements=st.lists(_phrases, max_size=4), texts=st.lists(_texts, min_size=1, max_size=4))
def test_bundle_complement_is_conservative(complements: list[str], texts: list[str]) -> None:
    haystacks = {f"C{i}": t for i, t in enumerate(texts)}
    signals = tuple(BundleSignal(complement=c, supporting_review_ids=("r1",)) for c in complements)
    result = _derive_bundle_complement(signals, haystacks)
    if not signals:
        assert result is None  # nothing to judge
    else:
        any_present = any(
            feature_present(s.complement, h, 0.6) for s in signals for h in haystacks.values()
        )
        # True (already bundled) iff some complement is present; else False (opening).
        assert result is any_present
