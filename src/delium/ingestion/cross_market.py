"""Cross-market discovery pipeline (docs/cross-market.md §13, §15).

Bridges persisted, marketplace-scoped provider data to the *pure* cross-market
analysis engine. Responsibilities:

- assemble ``SourceMarketInput`` / ``TargetMarketInput`` / ``TransferabilityInput``
  from stored rows (never from live model guesses),
- generate deterministic source candidates from already-fetched data,
- resolve source→target product identity (GTIN, else fuzzy) and persist the
  match, and
- run ``analysis.cross_market.analyze_cross_market`` and attach provenance.

Crucially it distinguishes three target states — **not looked up** (UNKNOWN),
**looked up and absent** (NOT_PRESENT), and **present** — because
``cross_market.py`` treats "no listings" and "no evidence" very differently, and
neither is an opportunity on its own.

This module is ingestion, so it may touch the database and the pure analysis
layer; it performs no provider/network calls itself (the CLI fetches first) and
imports no LLM/agent code. ``analysis/`` stays pure.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from delium.analysis.cross_market import analyze_cross_market, assess_source, match_products
from delium.analysis.marketplaces import get_marketplace
from delium.analysis.models import (
    CompetitionConfig,
    CrossMarketReport,
    Dimensions,
    Marketplace,
    MarketplaceProduct,
    MatchConfidence,
    ProductMatch,
    SourceMarketInput,
    SourceMaturity,
    TargetMarketInput,
    TransferabilityInput,
)
from delium.config.models import DeliumConfig
from delium.database import repository
from delium.ingestion.keywords import keyword_request_key
from delium.providers.dataforseo import normalize_serp, normalize_volume
from delium.utils.logging import get_logger

log = get_logger(__name__)

# Rough oversized heuristic for the transferability logistics factor (mm / g).
# Deterministic and conservative; the fee/size-tier engine remains the authority
# for fees — this only feeds the cross-market logistics flag.
_OVERSIZED_LONGEST_MM = 450
_OVERSIZED_WEIGHT_G = 20_000

_MATURITY_RANK = {
    SourceMaturity.INSUFFICIENT: 0,
    SourceMaturity.EMERGING: 1,
    SourceMaturity.VALIDATED: 2,
    SourceMaturity.STRONG: 3,
    SourceMaturity.EXCEPTIONAL: 4,
}


@dataclass(frozen=True)
class CrossMarketProvenance:
    """Every input traced to a stored provider fetch (raw_fetches / products)."""

    entries: tuple[tuple[str, str], ...] = ()  # (label, fetch_id)

    def with_entry(self, label: str, fetch_id: str | None) -> CrossMarketProvenance:
        if not fetch_id:
            return self
        return CrossMarketProvenance(self.entries + ((label, fetch_id),))


@dataclass(frozen=True)
class CrossMarketCandidate:
    """One assembled source→target assessment plus its data provenance."""

    source_asin: str
    source_marketplace: Marketplace
    target_marketplace: Marketplace
    seed: str | None
    report: CrossMarketReport
    provenance: CrossMarketProvenance
    match_persisted: bool = False


# ---------------------------------------------------------------------------
# Small persisted-row helpers
# ---------------------------------------------------------------------------
def _latest_history_value(rows: list[sqlite3.Row], column: str) -> int | None:
    for row in reversed(rows):
        value = row[column]
        if value is not None:
            return int(value)
    return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _dims_from_row(row: sqlite3.Row) -> Dimensions | None:
    dims_json = row["dims_json"]
    if not dims_json:
        return None
    parsed = json.loads(dims_json)
    keys = ("length_mm", "width_mm", "height_mm")
    if not all(k in parsed for k in keys):
        return None
    return Dimensions(*(int(parsed[k]) for k in keys))


def _product_from_row(row: sqlite3.Row, marketplace: Marketplace) -> MarketplaceProduct:
    return MarketplaceProduct(
        marketplace=marketplace,
        asin=row["asin"],
        title=row["title"],
        brand=row["brand"],
        manufacturer=row["manufacturer"],
        gtin=row["gtin"],
        dims=_dims_from_row(row),
        weight_g=row["weight_g"],
        category_path=row["category_path"],
    )


def _oversized(row: sqlite3.Row) -> bool | None:
    weight = row["weight_g"]
    dims = _dims_from_row(row)
    if weight is None and dims is None:
        return None
    if weight is not None and weight > _OVERSIZED_WEIGHT_G:
        return True
    return dims is not None and max(dims.sorted_desc()) > _OVERSIZED_LONGEST_MM


# ---------------------------------------------------------------------------
# Source assembly
# ---------------------------------------------------------------------------
def build_source(
    conn: sqlite3.Connection, asin: str, source_mp: Marketplace, config: DeliumConfig
) -> tuple[MarketplaceProduct, SourceMarketInput, str | None, CrossMarketProvenance] | None:
    """Build the source product + evidence input from persisted source-market
    data. Returns None if the product was never fetched in this marketplace."""
    product_row = repository.get_product(conn, asin, source_mp.value)
    if product_row is None:
        return None

    provenance = CrossMarketProvenance().with_entry("source_product", product_row["fetch_id"])
    history = repository.get_price_bsr_history(conn, asin)
    review_count = _latest_history_value(history, "review_count")

    derived = repository.get_product_derived(conn, asin)
    monthly_units: int | None = None
    if derived is not None:
        low, high = derived["est_units_low"], derived["est_units_high"]
        if low is not None and high is not None:
            monthly_units = round((int(low) + int(high)) / 2)
        elif high is not None:
            monthly_units = int(high)
        provenance = provenance.with_entry("source_derived", derived["fetch_id"])

    # Product → keyword cluster via SERP presence in the source marketplace.
    # The seed comes from serp_rankings (its own marketplace column); its volume
    # comes from the marketplace-scoped volume raw_fetch (collision-free and
    # provenance-linked), never the phrase-keyed keywords table.
    phrases = repository.get_serp_keyword_phrases(conn, asin, source_mp.value)
    seed = phrases[0] if phrases else None
    keyword_volume: int | None = None
    if seed is not None:
        keyword_volume, _growth, vol_fetch_id = _keyword_volume_from_fetch(conn, source_mp, seed)
        provenance = provenance.with_entry("source_keyword", vol_fetch_id)

    source_input = SourceMarketInput(
        monthly_units=monthly_units,
        keyword_volume=keyword_volume,
        keyword_growth=None,  # requires a multi-year source series (not in V1 storage)
        history_months=_history_months(history),
        review_count=float(review_count) if review_count is not None else None,
        review_velocity=None,
        competition_score=None,
    )
    return _product_from_row(product_row, source_mp), source_input, seed, provenance


def _keyword_volume_from_fetch(
    conn: sqlite3.Connection, marketplace: Marketplace, phrase: str
) -> tuple[int | None, float | None, str | None]:
    """Search volume for `phrase` in `marketplace`, read from the marketplace-
    scoped bulk_search_volume raw_fetch (the source of truth). Returns
    (volume, growth, fetch_id). Collision-free across marketplaces because the
    request key carries the marketplace."""
    fetch = repository.latest_raw_fetch(
        conn, "dataforseo", keyword_request_key(marketplace.value, "volume", phrase)
    )
    if fetch is None:
        return None, None, None
    try:
        payload = json.loads(fetch["payload"])
    except (ValueError, TypeError):
        return None, None, fetch["id"]
    for kw in normalize_volume(payload):
        if kw.phrase == phrase:
            return kw.volume, None, fetch["id"]
    return None, None, fetch["id"]


def _history_months(history: list[sqlite3.Row]) -> int | None:
    dates = [r["captured_on"] for r in history if r["captured_on"]]
    if len(dates) < 2:
        return None
    first, last = min(dates), max(dates)
    # ISO dates → approximate month span from the year/month components.
    fy, fm = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    return max(1, (ly - fy) * 12 + (lm - fm))


# ---------------------------------------------------------------------------
# Target assembly (the not-present / unknown / present distinction)
# ---------------------------------------------------------------------------
def _target_serp_asins(
    conn: sqlite3.Connection, seed: str, target_mp: Marketplace
) -> tuple[list[str], str | None, bool]:
    """Return (serp_asins, serp_fetch_id, looked_up).

    `looked_up` is True iff a target SERP raw_fetch exists for the seed — the
    boundary between NOT_PRESENT (looked up, none matched) and UNKNOWN (never
    looked up). SERP asins come from stored rows; the raw_fetch is authoritative
    for whether we searched at all (an empty SERP leaves no rows)."""
    fetch = repository.latest_raw_fetch(
        conn, "dataforseo", keyword_request_key(target_mp.value, "serp", seed)
    )
    if fetch is None:
        return [], None, False
    rows = repository.get_serp_rankings(conn, seed, target_mp.value)
    if rows:
        asins = [r["asin"] for r in rows]
    else:
        # Re-normalize from the raw payload (source of truth) in case rows were
        # not extracted; still counts as "looked up".
        asins = [item.asin for item in normalize_serp(json.loads(fetch["payload"]))]
    return asins, fetch["id"], True


def _match_target_product(
    conn: sqlite3.Connection,
    source_product: MarketplaceProduct,
    target_mp: Marketplace,
    serp_asins: list[str],
    config: DeliumConfig,
) -> tuple[MarketplaceProduct, ProductMatch, str, str | None]:
    """Resolve the target listing for the source product. Returns
    (target_product, match, method, target_fetch_id).

    GTIN first, then fuzzy over the target SERP asins' stored products. If none
    matches, the source identity is *projected* into the target marketplace
    (method 'projected'): the product we would launch there, so the match is
    EXACT/STRONG by construction and target presence — not identity — carries
    the 'not here yet' signal."""
    # 1. GTIN match against stored target products.
    if source_product.gtin:
        gtin_row = repository.find_product_by_gtin(conn, source_product.gtin, target_mp.value)
        if gtin_row is not None:
            target = _product_from_row(gtin_row, target_mp)
            gtin_match = match_products(source_product, target, config)
            return target, gtin_match, "gtin", gtin_row["fetch_id"]

    # 2. Fuzzy match against target SERP asins' stored products.
    best: tuple[MarketplaceProduct, ProductMatch, str | None] | None = None
    for serp_asin in serp_asins:
        row = repository.get_product(conn, serp_asin, target_mp.value)
        if row is None:
            continue
        candidate = _product_from_row(row, target_mp)
        match = match_products(source_product, candidate, config)
        if match.confidence is MatchConfidence.UNMATCHED:
            continue
        if best is None or match.score > best[1].score:
            best = (candidate, match, row["fetch_id"])
    if best is not None:
        return best[0], best[1], "fuzzy", best[2]

    # 3. Project the source identity into the target marketplace.
    projected = MarketplaceProduct(
        marketplace=target_mp,
        asin=source_product.asin,  # placeholder: same concept, not the real target ASIN
        title=source_product.title,
        brand=source_product.brand,
        manufacturer=source_product.manufacturer,
        gtin=source_product.gtin,
        dims=source_product.dims,
        weight_g=source_product.weight_g,
        category_path=source_product.category_path,
        generic=source_product.generic,
    )
    return projected, match_products(source_product, projected, config), "projected", None


def build_target(
    conn: sqlite3.Connection,
    source_product: MarketplaceProduct,
    seed: str | None,
    target_mp: Marketplace,
    config: DeliumConfig,
) -> tuple[MarketplaceProduct, TargetMarketInput, ProductMatch, str, CrossMarketProvenance]:
    """Assemble the target product, target evidence, and identity match."""
    provenance = CrossMarketProvenance()

    serp_asins: list[str] = []
    looked_up = False
    if seed is not None:
        serp_asins, serp_fetch_id, looked_up = _target_serp_asins(conn, seed, target_mp)
        provenance = provenance.with_entry("target_serp", serp_fetch_id)

    target_product, match, method, target_fetch_id = _match_target_product(
        conn, source_product, target_mp, serp_asins, config
    )
    provenance = provenance.with_entry("target_product", target_fetch_id)

    # Target demand: the SAME seed looked up in the TARGET marketplace, read
    # from the target's marketplace-scoped volume raw_fetch.
    keyword_volume: int | None = None
    keyword_growth: float | None = None
    serp_presence: bool | None = None
    if seed is not None:
        keyword_volume, keyword_growth, kw_fetch_id = _keyword_volume_from_fetch(
            conn, target_mp, seed
        )
        provenance = provenance.with_entry("target_keyword", kw_fetch_id)
        if looked_up:
            serp_presence = len(serp_asins) > 0

    # Target competition from stored target competitor products.
    review_counts: list[float] = []
    beatable = 0
    # The "beatable" review ceiling is the competition engine's documented C2
    # threshold — reused here, not re-invented.
    threshold = CompetitionConfig().beatable_review_threshold
    competitor_products = 0
    for serp_asin in serp_asins:
        row = repository.get_product(conn, serp_asin, target_mp.value)
        if row is None:
            continue
        competitor_products += 1
        latest = _latest_history_value(
            repository.get_price_bsr_history(conn, serp_asin), "review_count"
        )
        if latest is not None:
            review_counts.append(float(latest))
            if latest < threshold:
                beatable += 1

    # listings_found: None (never looked up → UNKNOWN) vs 0 (looked up, empty →
    # NOT_PRESENT) vs N. This is the core absence-of-evidence safeguard.
    listings_found = len(serp_asins) if looked_up else None

    target_input = TargetMarketInput(
        listings_found=listings_found,
        median_reviews=_median(review_counts),
        avg_listing_quality=None,  # needs the listing engine — out of V1 ingestion scope
        beatable_slots=beatable if competitor_products else None,
        brand_hhi=None,
        keyword_volume=keyword_volume,
        keyword_growth=keyword_growth,
        serp_presence=serp_presence,
        review_velocity=None,
    )
    return target_product, target_input, match, method, provenance


def _transfer_input(
    conn: sqlite3.Connection,
    source_asin: str,
    source_product: MarketplaceProduct,
    source_mp: Marketplace,
    target_mp: Marketplace,
    config: DeliumConfig,
) -> TransferabilityInput:
    """Deterministic transferability signals from stored data + the marketplace
    registry. Unknown signals stay None (→ UNCERTAIN), never optimistic."""
    product_row = repository.get_product(conn, source_asin, source_mp.value)
    oversized = _oversized(product_row) if product_row is not None else None

    price_ok: bool | None = None
    latest_price = _latest_history_value(
        repository.get_price_bsr_history(conn, source_asin), "price_cents"
    )
    if latest_price is not None:
        price_usd = latest_price / 100.0
        price_ok = config.preferences.min_price <= price_usd <= config.preferences.max_price

    seasonality: float | None = None
    derived = repository.get_product_derived(conn, source_asin)
    if derived is not None and derived["seasonality_peak_pct"] is not None:
        seasonality = float(derived["seasonality_peak_pct"])

    src_info = get_marketplace(source_mp)
    tgt_info = get_marketplace(target_mp)
    return TransferabilityInput(
        category_compatible=True if source_product.category_path else None,
        oversized=oversized,
        price_positioning_ok=price_ok,
        compliance_risk=None,  # not deterministically known — stays UNCERTAIN
        seasonality_concentration=seasonality,
        electrical_or_plug_dependent=None,
        unit_system_differs=src_info.unit_system != tgt_info.unit_system,
        language_differs=src_info.language != tgt_info.language,
        keyword_localization_needed=None,
    )


# ---------------------------------------------------------------------------
# Candidate generation & discovery
# ---------------------------------------------------------------------------
def generate_candidates(
    conn: sqlite3.Connection,
    source_mp: Marketplace,
    config: DeliumConfig,
    *,
    limit: int = 25,
    min_monthly_units: int | None = None,
) -> list[str]:
    """Deterministic source candidates: products already fetched in the source
    marketplace, newest first, optionally filtered by a minimum estimated
    velocity. Does NOT scan the catalog (docs/cross-market.md §13)."""
    rows = repository.get_products_by_marketplace(conn, source_mp.value)
    candidates: list[str] = []
    for row in rows:
        if min_monthly_units is not None:
            derived = repository.get_product_derived(conn, row["asin"])
            high = derived["est_units_high"] if derived is not None else None
            if high is None or int(high) < min_monthly_units:
                continue
        candidates.append(row["asin"])
        if len(candidates) >= limit:
            break
    return candidates


def discover_cross_market(
    conn: sqlite3.Connection,
    *,
    source_mp: Marketplace,
    target_mps: tuple[Marketplace, ...],
    config: DeliumConfig,
    run_id: str | None = None,
    limit: int = 25,
    min_source_maturity: SourceMaturity = SourceMaturity.EMERGING,
    min_monthly_units: int | None = None,
    persist_matches: bool = True,
) -> list[CrossMarketCandidate]:
    """Assemble and score every (source candidate → target) pair from persisted
    data. Directional: `source_mp → each target_mp` is evaluated independently.
    Candidates below `min_source_maturity` are skipped (source not proven)."""
    results: list[CrossMarketCandidate] = []
    min_rank = _MATURITY_RANK[min_source_maturity]

    for asin in generate_candidates(
        conn, source_mp, config, limit=limit, min_monthly_units=min_monthly_units
    ):
        built = build_source(conn, asin, source_mp, config)
        if built is None:
            continue
        source_product, source_input, seed, src_prov = built

        # Gate on source maturity before spending effort on targets.
        maturity = assess_source(source_mp, source_input, config).maturity
        if _MATURITY_RANK[maturity] < min_rank:
            continue

        for target_mp in target_mps:
            if target_mp == source_mp:
                continue
            target_product, target_input, _match, method, tgt_prov = build_target(
                conn, source_product, seed, target_mp, config
            )
            transfer_input = _transfer_input(
                conn, asin, source_product, source_mp, target_mp, config
            )
            report = analyze_cross_market(
                source_product=source_product,
                target_product=target_product,
                source_input=source_input,
                target_input=target_input,
                transfer_input=transfer_input,
                config=config,
            )
            persisted = False
            if persist_matches:
                _persist_match(conn, asin, source_mp, target_product, report, method, run_id)
                persisted = True
            provenance = CrossMarketProvenance(src_prov.entries + tgt_prov.entries)
            results.append(
                CrossMarketCandidate(
                    source_asin=asin,
                    source_marketplace=source_mp,
                    target_marketplace=target_mp,
                    seed=seed,
                    report=report,
                    provenance=provenance,
                    match_persisted=persisted,
                )
            )
    return results


def _persist_match(
    conn: sqlite3.Connection,
    source_asin: str,
    source_mp: Marketplace,
    target_product: MarketplaceProduct,
    report: CrossMarketReport,
    method: str,
    run_id: str | None,
) -> None:
    match = report.match
    repository.upsert_product_match(
        conn,
        source_asin=source_asin,
        source_marketplace=source_mp.value,
        target_asin=target_product.asin,
        target_marketplace=target_product.marketplace.value,
        match_method=method,
        match_confidence=match.confidence.value,
        match_score=match.score,
        signals=list(match.signals_used),
        conflicts=list(match.conflicting_signals),
        evidence=match.detail,
        run_id=run_id,
    )
