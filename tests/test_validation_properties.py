"""Property-based invariants for the validation layer (hypothesis).

These assert the guarantees the validation pipeline must never violate:
- same data + config → identical score and identical persisted snapshot;
- duplicate review evidence can never change the differentiation result;
- removing review evidence can never raise confidence;
- the persisted snapshot faithfully preserves the scoring result;
- G5 (Strategist) is always pending in the snapshot.
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from delium.analysis.differentiation import analyze_differentiation
from delium.analysis.models import (
    Confidence,
    DifferentiationInput,
    DiffReview,
    RawTheme,
    ScoringInput,
    ThemeKind,
)
from delium.analysis.scoring import score_opportunity
from delium.config.models import DeliumConfig
from delium.validation import validation_snapshot
from scoring_support import (
    competition_report,
    demand_report,
    differentiation_report,
    risk_report,
    scenario_set,
)

CFG = DeliumConfig()
_CONF_RANK = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}
_PROFIT = dict(
    stressed_margin=0.50, stressed_roi=3.5, stressed_payback=3.0, stressed_capital_cents=800_000
)
_CLEAN = dict(
    market_median_price_cents=2200,
    oversized=False,
    amazon_in_top5=False,
    market_complaint_rate=0.22,
    restricted_category=False,
    ip_signature=False,
    avoid_matches=(),
    fad_search_volume=9400,
    fad_volume_12mo_median=9000,
    volume_history_months=36,
)
_pillar = st.floats(min_value=0.0, max_value=100.0, allow_nan=False)


def _input(d: float = 80, c: float = 70, f: float = 70, r: float = 100) -> ScoringInput:
    return ScoringInput(
        demand=demand_report(d),
        competition=competition_report(c),
        differentiation=differentiation_report(f),
        profit=scenario_set(**_PROFIT),  # type: ignore[arg-type]
        risk=risk_report(r),
        **_CLEAN,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 1 — determinism: same data twice → same score AND same persisted snapshot
# ---------------------------------------------------------------------------
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_same_data_same_score_and_snapshot(d: float, c: float, f: float, r: float) -> None:
    inp = _input(d, c, f, r)
    a = score_opportunity(inp, CFG)
    b = score_opportunity(inp, CFG)
    assert a.score == b.score and a.verdict == b.verdict
    assert validation_snapshot(a) == validation_snapshot(b)


# ---------------------------------------------------------------------------
# 2 — the persisted snapshot is JSON-serializable and preserves the result
# ---------------------------------------------------------------------------
@given(d=_pillar, c=_pillar, f=_pillar, r=_pillar)
def test_snapshot_roundtrips_scoring_result(d: float, c: float, f: float, r: float) -> None:
    scored = score_opportunity(_input(d, c, f, r), CFG)
    snap = validation_snapshot(scored)
    text = json.dumps(snap)  # must be serializable for persistence
    reloaded = json.loads(text)
    assert reloaded["verdict"] == scored.verdict.value
    assert reloaded["score"] == scored.score
    assert [p["pillar"] for p in reloaded["pillars"]] == [p.pillar for p in scored.pillars]
    assert len(reloaded["kills"]) == len(scored.kills)
    # G5 is always pending in the snapshot — never resolved deterministically.
    g5 = next(g for g in reloaded["gates"] if g["gate_id"] == "G5")
    assert g5["passed"] is None
    assert reloaded["strategist_pending"] is True


# ---------------------------------------------------------------------------
# 3 — duplicate review evidence can never change the differentiation result
# ---------------------------------------------------------------------------
@given(
    n=st.integers(min_value=5, max_value=80),
    k=st.integers(min_value=1, max_value=5),
    dup=st.integers(min_value=2, max_value=5),
)
def test_duplicate_cited_ids_do_not_change_differentiation(n: int, k: int, dup: int) -> None:
    reviews = tuple(DiffReview(review_id=f"r{i}", stars=(i % 5) + 1) for i in range(n))
    ids = tuple(f"r{i}" for i in range(min(k, n)))
    base = DifferentiationInput(
        target_asin="X",
        reviews=reviews,
        themes=(RawTheme("t1", ThemeKind.COMPLAINT, "leak", ids),),
    )
    duplicated = DifferentiationInput(
        target_asin="X",
        reviews=reviews,
        themes=(RawTheme("t1", ThemeKind.COMPLAINT, "leak", ids * dup),),
    )
    assert (
        analyze_differentiation(base).pillar_score
        == analyze_differentiation(duplicated).pillar_score
    )


# ---------------------------------------------------------------------------
# 4 — removing review evidence can never RAISE differentiation confidence
# ---------------------------------------------------------------------------
@given(n=st.integers(min_value=3, max_value=200), drop=st.integers(min_value=0, max_value=200))
def test_removing_reviews_never_raises_confidence(n: int, drop: int) -> None:
    reviews = tuple(DiffReview(review_id=f"r{i}", stars=(i % 5) + 1) for i in range(n))
    ids = tuple(f"r{i}" for i in range(min(3, n)))  # cited ids present in both samples
    theme = RawTheme("t1", ThemeKind.COMPLAINT, "leak", ids)
    m = max(3, n - drop)  # thinner (or equal) sample, still holding the cited ids
    full = analyze_differentiation(
        DifferentiationInput(target_asin="X", reviews=reviews, themes=(theme,))
    )
    thin = analyze_differentiation(
        DifferentiationInput(target_asin="X", reviews=reviews[:m], themes=(theme,))
    )
    assert _CONF_RANK[thin.confidence.level] <= _CONF_RANK[full.confidence.level]


# ---------------------------------------------------------------------------
# 5 — a claimed percentage can never inflate a theme's recomputed frequency
# ---------------------------------------------------------------------------
@given(
    n=st.integers(min_value=5, max_value=100),
    k=st.integers(min_value=1, max_value=5),
    claimed=st.floats(min_value=0.0, max_value=100.0, allow_nan=False),
)
def test_claimed_percent_never_inflates_frequency(n: int, k: int, claimed: float) -> None:
    reviews = tuple(DiffReview(review_id=f"r{i}", stars=(i % 5) + 1) for i in range(n))
    cited = min(k, n)
    theme = RawTheme(
        "t1",
        ThemeKind.COMPLAINT,
        "leak",
        tuple(f"r{i}" for i in range(cited)),
        claimed_frequency_pct=claimed,
    )
    report = analyze_differentiation(
        DifferentiationInput(target_asin="X", reviews=reviews, themes=(theme,))
    )
    # Frequency is verified-ids / sample — never the claimed percentage.
    assert report.themes[0].frequency <= cited / n + 1e-9
