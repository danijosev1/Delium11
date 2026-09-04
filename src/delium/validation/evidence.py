"""Assemble validate-tier evidence for the analysis engines from persisted data.

Two boundaries live here, both deterministic:

1. Review evidence → the differentiation engine. The eligible review *sample*
   (id + stars) is read straight from the `reviews` table and is the real
   denominator the engine recomputes every frequency against. Review Miner
   themes (an LLM/INTERPRET-stage output) are read from `review_themes` when
   already persisted and mapped to `RawTheme`s — with `addressability=UNKNOWN`
   (never scored optimistically) and the persisted `claimed_*` numbers passed
   through only as ADVISORY fields the engine ignores. The Review Miner LLM is
   NOT run here: no themes are invented, and `miner_pending` stays True.

2. Listing quality → the competition engine. `compute_listing_quality` runs on
   the observable listing facts we persist (title, images, reviews, rating,
   price vs. competitors, keyword coverage). Facts we don't persist (A+, video,
   bullets, brand store, variations) are left absent, which the listing engine
   treats as lower confidence — never guessed.

This module assembles inputs and calls the engines; it never computes a pillar
score itself, and it never trusts an LLM-supplied number.
"""

from __future__ import annotations

import json
import sqlite3

from delium.analysis.differentiation import analyze_differentiation
from delium.analysis.listing import compute_listing_quality
from delium.analysis.models import (
    Addressability,
    DifferentiationReport,
    DiffReview,
    ListingInput,
    RawTheme,
    ThemeKind,
)
from delium.database import repository
from delium.validation.models import ReviewEvidence


# ---------------------------------------------------------------------------
# Review evidence → differentiation
# ---------------------------------------------------------------------------
def _theme_kind(raw: str | None) -> ThemeKind | None:
    if not raw:
        return None
    try:
        return ThemeKind(raw)
    except ValueError:
        return None


def _parse_ids(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    return tuple(str(v) for v in parsed if isinstance(v, str))


def _diff_reviews(conn: sqlite3.Connection, asin: str) -> tuple[DiffReview, ...]:
    """The eligible review sample — every persisted review for the target ASIN.
    Only id + stars are needed; the engine dedups ids itself."""
    rows = repository.get_reviews_for_asin(conn, asin)
    return tuple(DiffReview(review_id=r["review_id"], stars=int(r["stars"])) for r in rows)


def _raw_themes(conn: sqlite3.Connection, asin: str) -> tuple[RawTheme, ...]:
    """Persisted Review Miner themes → engine `RawTheme`s. Addressability is not
    persisted, so it is UNKNOWN (never counted as fixable); the persisted
    frequency/severity are attached as advisory-only fields the engine ignores in
    favor of recomputing from the cited review ids."""
    out: list[RawTheme] = []
    for row in repository.get_review_themes(conn, asin):
        kind = _theme_kind(row["kind"])
        if kind is None:
            continue  # unknown kind — cannot be scored, drop rather than guess
        out.append(
            RawTheme(
                theme_id=row["id"],
                kind=kind,
                label=row["theme"],
                supporting_review_ids=_parse_ids(row["quote_review_ids"]),
                addressability=Addressability.UNKNOWN,
                claimed_frequency_pct=row["frequency_pct"],  # advisory only (ignored)
                claimed_severity=row["severity"],  # advisory only (ignored)
            )
        )
    return tuple(out)


def _latest_rating(conn: sqlite3.Connection, asin: str) -> float | None:
    for row in reversed(repository.get_price_bsr_history(conn, asin)):
        if row["rating"] is not None:
            return float(row["rating"])
    return None


def build_differentiation(
    conn: sqlite3.Connection, asin: str, config: object
) -> tuple[DifferentiationReport | None, ReviewEvidence | None]:
    """Assemble the differentiation input from persisted reviews + themes and run
    the engine. Returns (None, None) when no reviews were fetched — then the
    differentiation pillar is genuinely absent (blocks Buy via G4), not faked.

    `config` is accepted for symmetry with the other assemblers (the engine uses
    its own `DifferentiationConfig` defaults); it is intentionally unused here."""
    from delium.analysis.models import DifferentiationInput

    del config  # differentiation engine owns its thresholds
    reviews = _diff_reviews(conn, asin)
    if not reviews:
        return None, None

    themes = _raw_themes(conn, asin)
    listing_rating = _latest_rating(conn, asin)
    data = DifferentiationInput(
        target_asin=asin,
        reviews=reviews,
        themes=themes,
        # feature_requests / bundle_signals are Review Miner (LLM) outputs; they
        # are not persisted deterministically, so they stay empty until the Miner
        # runs. Left empty, never invented.
        feature_requests=(),
        bundle_signals=(),
        competitors_bundle_complement=None,
        listing_rating_avg=listing_rating,
    )
    report = analyze_differentiation(data)
    evidence = ReviewEvidence(
        asin=asin,
        sample_size=report.confidence.sample_size,
        themes_available=len(themes),
        listing_rating_avg=listing_rating,
        miner_pending=True,
    )
    return report, evidence


# ---------------------------------------------------------------------------
# Listing quality → competition (C2/C5)
# ---------------------------------------------------------------------------
def _latest(rows: list[sqlite3.Row], column: str) -> int | None:
    for row in reversed(rows):
        value = row[column]
        if value is not None:
            return int(value)
    return None


def build_competitor_listing_quality(
    conn: sqlite3.Connection, competitor_asins: list[str], marketplace: str
) -> dict[str, float]:
    """Compute a 0-100 listing-quality score per competitor from observable,
    persisted listing facts. Missing facts lower each listing's confidence inside
    the engine; they are never guessed. Returns {asin: overall_score}."""
    rows = {
        asin: row
        for asin in competitor_asins
        if (row := repository.get_product(conn, asin, marketplace)) is not None
    }
    histories = {asin: repository.get_price_bsr_history(conn, asin) for asin in rows}
    market_prices = tuple(
        p for asin in rows if (p := _latest(histories[asin], "price_cents")) is not None
    )

    quality: dict[str, float] = {}
    for asin, row in rows.items():
        history = histories[asin]
        price = _latest(history, "price_cents")
        # A listing's own price is excluded from the market context it's compared to.
        others = tuple(
            p
            for a, h in histories.items()
            if a != asin and (p := _latest(h, "price_cents")) is not None
        )
        keywords = tuple(repository.get_serp_keyword_phrases(conn, asin, marketplace))
        listing = ListingInput(
            title=row["title"],
            images_count=row["images_count"],
            review_count=_latest(history, "review_count"),
            rating=_latest_rating(conn, asin),
            price_cents=price,
            competitor_prices_cents=others or (market_prices or None),
            keywords=keywords or None,
            brand=row["brand"],
        )
        quality[asin] = compute_listing_quality(listing).overall_score
    return quality
