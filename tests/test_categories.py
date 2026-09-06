"""Regression tests for breadcrumb-path category resolution (production-audit fix).

Ingestion stores Keepa's FULL category breadcrumb ("Electronics > Headphones"),
but the fee / velocity-curve / risk tables are keyed by a department name
("Electronics"). Before the fix these were matched by exact equality, so real
provider data never matched — every product silently got the default referral
rate, the default demand curve (confidence capped), and skipped category risk
deductions. These tests lock in segment-based resolution end to end.
"""

from __future__ import annotations

from delium.analysis.categories import (
    category_matches,
    category_segments,
    resolve_category_key,
)
from delium.analysis.demand import load_velocity_curves
from delium.analysis.fees import closing_fee, load_fee_table, referral_fee
from delium.analysis.models import RiskInput
from delium.analysis.risk import analyze_risk, load_risk_rules

FEES = load_fee_table()
CURVES = load_velocity_curves()
RULES = load_risk_rules()


# ---------------------------------------------------------------------------
# The pure resolver
# ---------------------------------------------------------------------------
def test_segments_split_and_normalize() -> None:
    assert category_segments("Home & Kitchen > Kitchen & Dining > Storage") == (
        "home & kitchen",
        "kitchen & dining",
        "storage",
    )
    assert category_segments(None) == ()
    assert category_segments("") == ()


def test_matches_exact_and_within_path() -> None:
    assert category_matches("Electronics", "Electronics")  # exact single segment
    assert category_matches("Electronics", "Electronics > Headphones > In-Ear")
    assert category_matches("Kitchen & Dining", "Home & Kitchen > Kitchen & Dining")
    assert not category_matches("Electronics", "Home & Kitchen > Storage")
    assert not category_matches("Electronics", None)


def test_match_is_segment_equality_not_substring() -> None:
    # "Books" must NOT match "Cookbooks" (the substring trap the fix avoids).
    assert not category_matches("Books", "Cookbooks > Baking")
    assert category_matches("Books", "Books > Cookbooks")


def test_resolve_returns_first_matching_key_or_none() -> None:
    keys = ["Electronics", "Home & Kitchen"]
    assert resolve_category_key(keys, "Electronics > Cables") == "Electronics"
    assert resolve_category_key(keys, "Toys & Games > Blocks") is None


# ---------------------------------------------------------------------------
# Profit — referral fee resolves the real (non-default) rate from a breadcrumb
# ---------------------------------------------------------------------------
def test_referral_fee_resolves_category_from_breadcrumb_path() -> None:
    # Electronics referral is 8%; the default is 15%. A real breadcrumb must
    # resolve the 8% rate, not silently fall back to 15%.
    assert referral_fee(FEES, "Electronics > Headphones > In-Ear", 10_000) == 800
    # Exact department name still works (engine-test contract preserved).
    assert referral_fee(FEES, "Electronics", 10_000) == 800
    # Unknown path → documented default.
    assert referral_fee(FEES, "Widgets > Gizmos", 10_000) == 1500


def test_closing_fee_no_false_substring_match() -> None:
    # "Cookbooks" must not trip the media "Books" closing fee.
    assert closing_fee(FEES, "Cookbooks > Baking") == FEES.closing_default_cents
    assert closing_fee(FEES, "Books > Fiction") == 180


# ---------------------------------------------------------------------------
# Demand — the category curve is selected (and category_known becomes True)
# ---------------------------------------------------------------------------
def test_velocity_curve_resolves_from_breadcrumb_path() -> None:
    curve, known = CURVES.resolve("Home & Kitchen > Kitchen & Dining > Storage")
    assert known is True
    assert curve.name == "Home & Kitchen"
    _default, known_unknown = CURVES.resolve("Nonexistent > Path")
    assert known_unknown is False


# ---------------------------------------------------------------------------
# Risk — category deductions fire on real breadcrumb paths (were silently skipped)
# ---------------------------------------------------------------------------
def _risk_types(category: str) -> set[str]:
    report = analyze_risk(RiskInput(category=category), RULES)
    return {f.risk_type for f in report.flags if f.deduction > 0}


def test_risk_category_rules_fire_on_breadcrumb_paths() -> None:
    assert "compliance" in _risk_types("Baby > Feeding > Bowls")
    assert "ip_signal" in _risk_types("Toys & Games > Action Figures")
    assert "high_returns" in _risk_types("Watches > Smart Watches")
    # A clean category triggers none of the category-keyed deductions.
    assert _risk_types("Home & Kitchen > Storage") == set()
