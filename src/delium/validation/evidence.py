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
    BundleSignal,
    DifferentiationReport,
    DiffReview,
    FeatureRequest,
    ListingInput,
    RawTheme,
    ThemeKind,
)
from delium.config.models import DeliumConfig
from delium.database import repository
from delium.utils.text import feature_present
from delium.validation.models import FeatureGap, ReviewEvidence

# Present-matching a customer-requested feature against a competitor's listing is
# deliberately generous (lower than the Analyst's 0.85 extraction guard): a
# plausible match must read as PRESENT so we never manufacture a competitor gap.
_PRESENT_MATCH_THRESHOLD = 0.6


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


def _addressability(raw: str | None) -> Addressability:
    """Persisted addressability string → enum. Absent/unknown → UNKNOWN, which the
    engine never scores optimistically (F3 weight 0)."""
    if not raw:
        return Addressability.UNKNOWN
    try:
        return Addressability(raw)
    except ValueError:
        return Addressability.UNKNOWN


def _raw_themes(conn: sqlite3.Connection, asin: str) -> tuple[RawTheme, ...]:
    """Persisted Review Miner themes → engine `RawTheme`s. Addressability / cogs
    delta / category are read from the persisted structured fields (UNKNOWN when
    absent — never optimistic); the persisted frequency/severity are attached as
    advisory-only fields the engine ignores in favor of recomputing from the cited
    review ids."""
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
                addressability=_addressability(row["addressability"]),
                cogs_delta=row["cogs_delta"],
                category=row["category"],
                claimed_frequency_pct=row["frequency_pct"],  # advisory only (ignored)
                claimed_severity=row["severity"],  # advisory only (ignored)
            )
        )
    return tuple(out)


def _bool_or_none(value: object) -> bool | None:
    return None if value is None else bool(value)


def _feature_requests(conn: sqlite3.Connection, asin: str) -> tuple[FeatureRequest, ...]:
    """Persisted Review Miner missing-feature requests → engine FeatureRequests
    (differentiation F2). `absent_from_competitors` stays None (unknown) until an
    Analyst feature matrix confirms it — unknown never counts as a confirmed gap."""
    return tuple(
        FeatureRequest(
            feature=row["feature"],
            supporting_review_ids=_parse_ids(row["supporting_review_ids"]),
            absent_from_competitors=_bool_or_none(row["absent_from_competitors"]),
        )
        for row in repository.get_feature_requests(conn, asin)
    )


def _bundle_signals(conn: sqlite3.Connection, asin: str) -> tuple[BundleSignal, ...]:
    """Persisted Review Miner bundle/complement signals → engine BundleSignals (F4a)."""
    return tuple(
        BundleSignal(
            complement=row["complement"],
            supporting_review_ids=_parse_ids(row["supporting_review_ids"]),
        )
        for row in repository.get_bundle_signals(conn, asin)
    )


def _latest_rating(conn: sqlite3.Connection, asin: str) -> float | None:
    for row in reversed(repository.get_price_bsr_history(conn, asin)):
        if row["rating"] is not None:
            return float(row["rating"])
    return None


# ---------------------------------------------------------------------------
# Analyst feature matrix → confirmed competitor absence (differentiation F2/F4b)
# ---------------------------------------------------------------------------
def _competitor_haystacks(
    conn: sqlite3.Connection, competitor_asins: list[str], marketplace: str | None
) -> dict[str, str]:
    """Per-competitor present-match text: the features the Analyst persisted for
    that listing PLUS its observable listing text (title/brand/category). Only
    competitors with ≥1 persisted feature are included — an ASIN with no persisted
    features was not analyzed (or yielded nothing observable), so it can neither
    confirm nor deny a gap and must not count toward coverage."""
    haystacks: dict[str, str] = {}
    for asin in competitor_asins:
        features = [row["feature"] for row in repository.get_competitor_features(conn, asin)]
        if not features:
            continue
        product = repository.get_product(conn, asin, marketplace)
        listing_bits = (
            [product["title"] or "", product["brand"] or "", product["category_path"] or ""]
            if product is not None
            else []
        )
        haystacks[asin] = " ".join([*features, *listing_bits])
    return haystacks


def _present_in_any(term: str, haystacks: dict[str, str]) -> bool:
    return any(feature_present(term, text, _PRESENT_MATCH_THRESHOLD) for text in haystacks.values())


def _derive_absence(
    features: tuple[FeatureRequest, ...], haystacks: dict[str, str]
) -> tuple[FeatureRequest, ...]:
    """Set `absent_from_competitors` per requested feature from the analyzed
    competitor set: True only when NO analyzed competitor claims it (a confirmed
    gap), False when at least one does. Caller guarantees the coverage gate is met;
    a feature never becomes 'absent' on thin evidence."""
    return tuple(
        FeatureRequest(
            feature=f.feature,
            supporting_review_ids=f.supporting_review_ids,
            absent_from_competitors=not _present_in_any(f.feature, haystacks),
        )
        for f in features
    )


def _derive_bundle_complement(
    bundle_signals: tuple[BundleSignal, ...], haystacks: dict[str, str]
) -> bool | None:
    """F4b input. True when at least one analyzed competitor already offers a
    customer-requested complement (no opening); False when none do (a real
    opening); None when there is nothing to judge. Caller guarantees the coverage
    gate is met — the engine only rewards a confirmed False."""
    if not bundle_signals:
        return None
    return any(_present_in_any(b.complement, haystacks) for b in bundle_signals)


def build_differentiation(
    conn: sqlite3.Connection,
    asin: str,
    config: DeliumConfig,
    *,
    miner_ran: bool = False,
    competitor_asins: list[str] | None = None,
    marketplace: str | None = None,
) -> tuple[DifferentiationReport | None, ReviewEvidence | None]:
    """Assemble the differentiation input from persisted reviews + Review Miner
    evidence (themes, feature requests, bundle signals) and run the engine.
    Returns (None, None) when no reviews were fetched — then the differentiation
    pillar is genuinely absent (blocks Buy via G4), not faked.

    `miner_ran` records whether the LLM Review Miner produced this run's evidence,
    so the report can show its status; the deterministic engine is unaffected.

    When `competitor_asins` are given and the Analyst has persisted a feature
    matrix for enough of them (`agents.min_competitor_feature_coverage`), each
    requested feature's `absent_from_competitors` and the
    `competitors_bundle_complement` flag are DERIVED from that matrix — otherwise
    both stay UNKNOWN (None), never assumed. The engine owns every threshold; this
    only supplies confirmed-or-unknown observations."""
    from delium.analysis.models import DifferentiationInput

    reviews = _diff_reviews(conn, asin)
    if not reviews:
        return None, None

    themes = _raw_themes(conn, asin)
    feature_requests = _feature_requests(conn, asin)
    bundle_signals = _bundle_signals(conn, asin)
    listing_rating = _latest_rating(conn, asin)

    # Confirmed competitor absence is derived from the Analyst matrix, coverage-
    # gated so it is only asserted on a sufficient sample; otherwise left unknown.
    competitors_bundle_complement: bool | None = None
    haystacks = _competitor_haystacks(conn, competitor_asins or [], marketplace)
    matrix_confirmed = len(haystacks) >= config.agents.min_competitor_feature_coverage
    if matrix_confirmed:
        feature_requests = _derive_absence(feature_requests, haystacks)
        competitors_bundle_complement = _derive_bundle_complement(bundle_signals, haystacks)

    data = DifferentiationInput(
        target_asin=asin,
        reviews=reviews,
        themes=themes,
        feature_requests=feature_requests,
        bundle_signals=bundle_signals,
        competitors_bundle_complement=competitors_bundle_complement,
        listing_rating_avg=listing_rating,
    )
    report = analyze_differentiation(data)
    evidence = ReviewEvidence(
        asin=asin,
        sample_size=report.confidence.sample_size,
        themes_available=len(themes),
        feature_requests=len(feature_requests),
        bundle_signals=len(bundle_signals),
        listing_rating_avg=listing_rating,
        miner_pending=not miner_ran,
        feature_gaps=_feature_gaps(feature_requests),
        competitor_matrix_confirmed=matrix_confirmed,
    )
    return report, evidence


def _feature_gaps(features: tuple[FeatureRequest, ...]) -> tuple[FeatureGap, ...]:
    """Classify each requested feature by its competitor-absence status for the
    report's feature-gap table — the same tri-state the engine consumed."""
    status = {True: "absent", False: "present", None: "unknown"}
    return tuple(
        FeatureGap(
            feature=f.feature,
            status=status[f.absent_from_competitors],
            request_count=len(f.supporting_review_ids),
        )
        for f in features
    )


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
