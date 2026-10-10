"""`title_to_keyword` — the deterministic title→search-phrase fallback used to
seed a competitor-set SERP when a reverse-ASIN / prior-SERP phrase is unavailable
(e.g. non-US, where DataForSEO Labs is US-only). Pure; no network, no DB."""

from __future__ import annotations

from delium.utils.text import title_to_keyword


def test_strips_brand_sizes_colours_and_keeps_the_noun_phrase() -> None:
    kw = title_to_keyword("Anker Portable Charger 10000mAh Power Bank, Black", "Anker")
    assert kw == "portable charger power bank"


def test_strips_measurements_and_material_stays() -> None:
    kw = title_to_keyword("YETI Rambler 20 oz Tumbler, Stainless Steel, Navy", "YETI")
    assert kw == "rambler tumbler stainless steel"


def test_strips_pack_counts() -> None:
    kw = title_to_keyword("3-Pack Microfiber Cleaning Cloths 12x16 inches")
    assert kw == "microfiber cleaning cloths"


def test_caps_at_four_words() -> None:
    kw = title_to_keyword("Set of 6 Silicone Baking Cups Reusable Muffin Liners Non-Stick")
    assert kw is not None
    assert len(kw.split()) <= 4
    assert "silicone" in kw and "baking" in kw


def test_brand_may_be_multiword() -> None:
    kw = title_to_keyword("Amazon Basics Stainless Steel Water Bottle", "Amazon Basics")
    assert kw == "stainless steel water bottle"


def test_empty_title_returns_none() -> None:
    assert title_to_keyword(None) is None
    assert title_to_keyword("") is None
    assert title_to_keyword("   ") is None


def test_all_noise_falls_back_to_title_minus_brand() -> None:
    # Everything looks like brand/colour/size — still yields a (weak) phrase, never
    # empty, so a competitor set can still be attempted.
    kw = title_to_keyword("Anker Black Red Blue", "Anker")
    assert kw == "black red blue"


def test_deterministic() -> None:
    title = "Contigo AUTOSEAL West Loop Travel Mug 16 oz Stainless"
    assert title_to_keyword(title, "Contigo") == title_to_keyword(title, "Contigo")
