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


# ---------------------------------------------------------------------------
# Calibration-audit contract tests (docs/category-data-audit.md)
#
# These lock in the CONSERVATIVE guarantees and document the known key/taxonomy
# gaps as intended behavior — a table key that is not an exact Keepa segment
# silently does NOT fire, which is safe (falls back to default) but is a
# coverage gap that requires the operator to reconcile the key against a live
# Keepa categoryTree. They must never start passing by accident.
# ---------------------------------------------------------------------------
def test_singular_key_does_not_match_pluralized_keepa_root() -> None:
    # The shipped key is "Baby"; Keepa's US root is believed to be "Baby
    # Products". Segment equality means "Baby" does NOT match — no false positive,
    # but the CPSIA compliance deduction is MISSED until the key is reconciled.
    # This asserts the safe (conservative) half; the report flags the gap.
    assert category_matches("Baby", "Baby > Feeding")  # exact segment present → fires
    assert not category_matches("Baby", "Baby Products > Feeding > Bowls")
    assert "compliance" not in _risk_types("Baby Products > Feeding > Bowls")


def test_media_closing_keys_reachability() -> None:
    # "Books" is a real Keepa root → reachable. "Music"/"DVD"/"Video, DVD &
    # Blu-ray" do not appear as segments of Keepa's "CDs & Vinyl" / "Movies & TV"
    # roots, so those closing-fee keys are unreachable (fall back to default).
    assert closing_fee(FEES, "Books > Nonfiction > Business") == 180
    assert closing_fee(FEES, "CDs & Vinyl > Pop") == FEES.closing_default_cents
    assert closing_fee(FEES, "Movies & TV > Action") == FEES.closing_default_cents


def test_ambiguous_multi_department_resolution_is_deterministic() -> None:
    # If a path somehow matches two keys, resolution is the FIRST key in the
    # table's iteration order — deterministic and repeatable. (Real Amazon
    # breadcrumbs have a single department root, so this is a defensive contract.)
    path = "Electronics > Accessories > Home & Kitchen"
    assert resolve_category_key(["Electronics", "Home & Kitchen"], path) == "Electronics"
    assert resolve_category_key(["Home & Kitchen", "Electronics"], path) == "Home & Kitchen"
    # Repeatable across calls.
    assert resolve_category_key(FEES.referral_categories, path) == resolve_category_key(
        FEES.referral_categories, path
    )


def test_unknown_category_falls_to_default_in_every_engine() -> None:
    unknown = "Musical Instruments > Guitars > Electric"  # no key in any table
    assert referral_fee(FEES, unknown, 10_000) == round(10_000 * FEES.referral_default_percent)
    assert closing_fee(FEES, unknown) == FEES.closing_default_cents
    _curve, known = CURVES.resolve(unknown)
    assert known is False
    assert _risk_types(unknown) == set()


def test_no_unrelated_breadcrumb_silently_fires_a_risk_rule() -> None:
    # Segment equality guarantees a rule keyed by "Toys & Games" cannot fire for
    # an unrelated department whose path merely contains the word "toys".
    assert "ip_signal" not in _risk_types("Pet Supplies > Dog Toys")
    assert "compliance" not in _risk_types("Office Products > Baby Wipes Dispenser")
