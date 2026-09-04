"""Assemble a scoring.py `ScoringInput` from persisted, marketplace-scoped data.

Discovery is cheap-and-wide: it drives the deterministic pillars computable from
Keepa + DataForSEO data (demand, competition, profit-under-config-assumptions,
risk). Differentiation needs review sampling — a validate-tier spend — so it is
left `None`; scoring.py then reports the differentiation pillar partial and never
issues a BUY on discovery-tier data alone. scoring.py remains the sole owner of
the verdict; this module never re-implements a score, kill, or gate.

Determinism: `as_of` is derived from the stored data (latest observation date),
never the wall clock. No randomness, no network. This is orchestration (it reads
the DB and calls analysis loaders), so it lives outside the pure `analysis/`
package.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from delium.analysis.competition import analyze_competition
from delium.analysis.demand import analyze_demand, load_velocity_curves
from delium.analysis.fees import load_fee_table
from delium.analysis.models import (
    ASSUMPTION_FIELDS,
    AsinHistory,
    BsrPoint,
    CompetitionInput,
    CompetitionReport,
    CompetitorSnapshot,
    DemandReport,
    DifferentiationReport,
    Dimensions,
    KeywordDatum,
    Marketplace,
    PricePoint,
    ProfitInputs,
    RiskInput,
    RiskReport,
    ScenarioSet,
    ScoringInput,
)
from delium.analysis.profit import compute_scenarios
from delium.analysis.risk import analyze_risk, load_risk_rules
from delium.database import repository
from delium.ingestion.cross_market import _oversized  # dims/weight heuristic (pure)
from delium.utils.logging import get_logger

log = get_logger(__name__)

_AMAZON_BRANDS = frozenset({"amazon", "amazonbasics", "amazon basics"})
_EPOCH = date(2000, 1, 1)  # deterministic fallback when no observations exist


@dataclass(frozen=True)
class AssemblyProvenance:
    """Fetch ids backing the assembled input, for run traceability."""

    entries: tuple[tuple[str, str], ...] = ()

    def add(self, label: str, fetch_id: str | None) -> AssemblyProvenance:
        if not fetch_id:
            return self
        return AssemblyProvenance(self.entries + ((label, fetch_id),))


@dataclass(frozen=True)
class ProfitOverrides:
    """User-supplied profit inputs (validate-tier CLI --cogs/--freight/--dims/
    --weight). Each is optional; when present it replaces the config assumption
    and, for cost/freight, is marked *known* (dropped from `estimated_fields`)
    so the profit engine's confidence reflects a real quote instead of a guess.
    Dims/weight overrides unblock the fee model when Keepa lacks them."""

    cogs_cents: int | None = None
    freight_cents: int | None = None
    dims: Dimensions | None = None
    weight_g: int | None = None


# ---------------------------------------------------------------------------
# Row → model helpers
# ---------------------------------------------------------------------------
def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _asin_history(conn: sqlite3.Connection, asin: str) -> AsinHistory:
    rows = repository.get_price_bsr_history(conn, asin)
    points = tuple(
        BsrPoint(date=d, bsr=int(r["bsr"]))
        for r in rows
        if r["bsr"] is not None and (d := _parse_date(r["captured_on"])) is not None
    )
    return AsinHistory(asin=asin, observations=points)


def _price_history(conn: sqlite3.Connection, asin: str) -> tuple[PricePoint, ...]:
    rows = repository.get_price_bsr_history(conn, asin)
    return tuple(
        PricePoint(date=d, price_cents=int(r["price_cents"]))
        for r in rows
        if r["price_cents"] is not None and (d := _parse_date(r["captured_on"])) is not None
    )


def _latest(rows: list[sqlite3.Row], column: str) -> int | None:
    for row in reversed(rows):
        value = row[column]
        if value is not None:
            return int(value)
    return None


def _latest_rating(rows: list[sqlite3.Row]) -> float | None:
    for row in reversed(rows):
        value = row["rating"]
        if value is not None:
            return float(value)
    return None


def _as_of(histories: list[AsinHistory]) -> date:
    """Deterministic reference date: the latest observation across histories.
    Never the wall clock, so a run is reproducible from its data alone."""
    dates = [p.date for h in histories for p in h.observations]
    return max(dates) if dates else _EPOCH


# ---------------------------------------------------------------------------
# Competitor gathering (shared by competition + demand + kills)
# ---------------------------------------------------------------------------
def _seed_phrase(conn: sqlite3.Connection, asin: str, marketplace: str) -> str | None:
    phrases = repository.get_serp_keyword_phrases(conn, asin, marketplace)
    return phrases[0] if phrases else None


def _competitor_asins(
    conn: sqlite3.Connection, seed: str | None, marketplace: str, cap: int
) -> list[str]:
    if seed is None:
        return []
    rows = repository.get_serp_rankings(conn, seed, marketplace)
    return [r["asin"] for r in rows[:cap]]


def _competitor_snapshot(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    marketplace: str,
    listing_quality: Mapping[str, float] | None = None,
) -> CompetitorSnapshot:
    asin = row["asin"]
    history = repository.get_price_bsr_history(conn, asin)
    # Listing quality is computed at validate tier (from observable listing facts)
    # and supplied here; at discovery tier it is absent (analyst-tier spend).
    quality = listing_quality.get(asin) if listing_quality else None
    return CompetitorSnapshot(
        asin=asin,
        brand=row["brand"],
        review_count=_latest(history, "review_count"),
        rating=_latest_rating(history),
        price_cents=_latest(history, "price_cents"),
        listing_quality=quality,
        review_count_90d_ago=None,
        price_history=_price_history(conn, asin) or None,
    )


# ---------------------------------------------------------------------------
# Pillar assemblers (each returns None when its data is genuinely absent)
# ---------------------------------------------------------------------------
def _keyword_data(
    conn: sqlite3.Connection, asin: str, marketplace: str
) -> tuple[tuple[KeywordDatum, ...], str | None, tuple[int, ...] | None]:
    phrases = repository.get_serp_keyword_phrases(conn, asin, marketplace)
    data: list[KeywordDatum] = []
    series: tuple[int, ...] | None = None
    for phrase in phrases:
        kw = repository.get_keyword(conn, phrase, marketplace)
        volume = None if kw is None or kw["volume"] is None else int(kw["volume"])
        data.append(KeywordDatum(phrase=phrase, volume=volume))
        if series is None and kw is not None and kw["volume_series"]:
            parsed = _parse_series(kw["volume_series"])
            if parsed:
                series = parsed
    primary = phrases[0] if phrases else None
    return tuple(data), primary, series


def _parse_series(series_json: str | None) -> tuple[int, ...] | None:
    if not series_json:
        return None
    try:
        raw = json.loads(series_json)
    except (ValueError, TypeError):
        return None
    values = [int(v) for v in raw if isinstance(v, int | float)]
    return tuple(values) or None


def build_demand(
    conn: sqlite3.Connection,
    asin: str,
    marketplace: str,
    category: str | None,
    competitor_asins: list[str],
) -> DemandReport | None:
    target = _asin_history(conn, asin)
    if not target.observations:
        return None  # no BSR history → demand not assessable at discovery
    histories = [target, *(_asin_history(conn, c) for c in competitor_asins if c != asin)]
    keywords, primary, series = _keyword_data(conn, asin, marketplace)
    return analyze_demand(
        as_of=_as_of(histories),
        histories=histories,
        keywords=keywords,
        category=category,
        curves_table=load_velocity_curves(),
        primary_phrase=primary,
        volume_series=list(series) if series else None,
    )


def build_competition(
    conn: sqlite3.Connection,
    marketplace: str,
    competitor_rows: list[sqlite3.Row],
    listing_quality: Mapping[str, float] | None = None,
) -> CompetitionReport | None:
    if not competitor_rows:
        return None
    snapshots = tuple(
        _competitor_snapshot(conn, r, marketplace, listing_quality) for r in competitor_rows
    )
    histories = [h for h in (_asin_history_dates(s) for s in snapshots) if h is not None]
    as_of = max(histories) if histories else _EPOCH
    return analyze_competition(CompetitionInput(competitors=snapshots, as_of=as_of))


def _asin_history_dates(snapshot: CompetitorSnapshot) -> date | None:
    if not snapshot.price_history:
        return None
    return max(p.date for p in snapshot.price_history)


def build_profit(
    row: sqlite3.Row,
    price_cents: int | None,
    monthly_units: int | None,
    config: object,
    overrides: ProfitOverrides | None = None,
) -> ScenarioSet | None:
    """Profit under conservative config assumptions (docs/scoring-model §7). Fee
    inputs (dims/weight/category) are blocking — no fees, no profit pillar. A
    validate-tier `overrides` may supply real COGS/freight (marked known, not
    estimated) and dims/weight to unblock the fee model when Keepa lacks them."""
    from delium.config.models import DeliumConfig

    assert isinstance(config, DeliumConfig)
    ov = overrides or ProfitOverrides()
    dims = ov.dims or _dims_from_row(row)
    weight_g = ov.weight_g if ov.weight_g is not None else row["weight_g"]
    if dims is None or weight_g is None or price_cents is None or price_cents <= 0:
        return None

    a = config.assumptions
    # Overridden fields are real quotes → drop them from the estimated set so the
    # profit engine's confidence isn't held down by an assumption we replaced.
    estimated = set(ASSUMPTION_FIELDS)
    if ov.cogs_cents is not None:
        cogs = ov.cogs_cents
        estimated.discard("product_cost")
    else:
        cogs = round(a.default_cogs_pct * price_cents)
    if ov.freight_cents is not None:
        freight = ov.freight_cents
        estimated.discard("freight")
    else:
        freight = round(a.freight_per_unit * 100)

    base = ProfitInputs(
        selling_price_cents=price_cents,
        product_cost_cents=cogs,
        freight_cents=freight,
        customs_cents=round(a.duty_pct * cogs),
        prep_cost_cents=0,  # fee table supplies the default prep charge
        ppc_percent=a.tacos_pct,
        return_rate=a.return_rate_default,
        monthly_sales_units=monthly_units or 0,
        estimated_fields=frozenset(estimated),
    )
    return compute_scenarios(
        load_fee_table(),
        category=row["category_path"],
        dims=dims,
        weight_g=int(weight_g),
        base_inputs=base,
    )


def _dims_from_row(row: sqlite3.Row) -> Dimensions | None:
    dims_json = row["dims_json"]
    if not dims_json:
        return None
    parsed = json.loads(dims_json)
    keys = ("length_mm", "width_mm", "height_mm")
    if not all(k in parsed for k in keys):
        return None
    return Dimensions(*(int(parsed[k]) for k in keys))


def build_risk(
    row: sqlite3.Row,
    demand: DemandReport | None,
    competition: CompetitionReport | None,
    oversized: bool | None,
) -> RiskReport:
    seasonality = demand.seasonality if demand is not None else None
    keyword_top_share = demand.keyword_demand.demand_concentration if demand is not None else None
    brand_hhi = competition.hhi if competition is not None else None
    price_war = competition.price_war_flag if competition is not None else None
    title = row["title"]
    risk_input = RiskInput(
        category=row["category_path"],
        titles=(title,) if title else (),
        keyword_top_share=keyword_top_share,
        brand_hhi=brand_hhi,
        seasonality=seasonality,
        price_war_flag=price_war,
        oversized=oversized,
    )
    return analyze_risk(risk_input, load_risk_rules())


# ---------------------------------------------------------------------------
# Cheap kill facts (Keepa quick stats + volume only; docs/data-layer §229)
# ---------------------------------------------------------------------------
def _median_int(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _avoid_matches(row: sqlite3.Row, avoid: list[str]) -> tuple[str, ...]:
    haystack = " ".join(str(row[c]) for c in ("title", "brand", "category_path") if row[c]).lower()
    return tuple(term for term in avoid if term.lower() in haystack)


def _fad_inputs(series: tuple[int, ...] | None) -> tuple[int | None, int | None, int | None]:
    """(current_volume, 12mo median, history_months) from a stored volume series."""
    if not series:
        return None, None, None
    current = series[-1]
    window = series[-12:] if len(series) >= 12 else series
    median = _median_int(list(window))
    return current, median, len(series)


# ---------------------------------------------------------------------------
# Top-level assembly
# ---------------------------------------------------------------------------
def build_scoring_input(
    conn: sqlite3.Connection,
    asin: str,
    marketplace: Marketplace,
    config: object,
    *,
    cheap_only: bool = False,
    differentiation: DifferentiationReport | None = None,
    listing_quality: Mapping[str, float] | None = None,
    profit_overrides: ProfitOverrides | None = None,
) -> tuple[ScoringInput | None, AssemblyProvenance]:
    """Assemble a `ScoringInput` for one candidate from persisted data.

    `cheap_only=True` populates just the inputs needed for the cheap hard kills
    (price, oversized, avoid list, fad) — so the kill-first funnel can eliminate
    candidates before any pillar analysis. Returns (None, provenance) if the
    product was never fetched in this marketplace.

    Validate-tier callers additionally supply the real review-evidence
    `differentiation` report, per-competitor `listing_quality` (0-100), and
    profit `overrides`. Discovery passes none of these, so its behavior is
    unchanged — differentiation stays absent, listing quality stays unknown.
    This module never computes a pillar itself; it only assembles the inputs the
    analysis engines consume.
    """
    from delium.config.models import DeliumConfig

    assert isinstance(config, DeliumConfig)
    row = repository.get_product(conn, asin, marketplace.value)
    if row is None:
        return None, AssemblyProvenance()
    prov = AssemblyProvenance().add("product", row["fetch_id"])

    history = repository.get_price_bsr_history(conn, asin)
    price_cents = _latest(history, "price_cents")
    oversized = _oversized(row)

    seed = _seed_phrase(conn, asin, marketplace.value)
    depth = config.discovery.serp_depth
    competitor_asins = _competitor_asins(conn, seed, marketplace.value, depth)
    competitor_rows = [
        r
        for c in competitor_asins
        if (r := repository.get_product(conn, c, marketplace.value)) is not None
    ]

    # Market median price (K1/K2): competitors' latest prices, else the candidate.
    comp_prices = [
        p
        for r in competitor_rows
        if (p := _latest(repository.get_price_bsr_history(conn, r["asin"]), "price_cents"))
        is not None
    ]
    if price_cents is not None:
        comp_prices.append(price_cents)
    market_median_price = _median_int(comp_prices)

    amazon_in_top5 = any(
        (r["brand"] or "").strip().lower() in _AMAZON_BRANDS for r in competitor_rows[:5]
    )
    avoid_matches = _avoid_matches(row, config.preferences.avoid)

    _keywords, _primary, series = _keyword_data(conn, asin, marketplace.value)
    fad_volume, fad_median, history_months = _fad_inputs(series)

    if cheap_only:
        return (
            ScoringInput(
                market_median_price_cents=market_median_price,
                oversized=oversized,
                amazon_in_top5=amazon_in_top5 if competitor_rows else None,
                avoid_matches=avoid_matches,
                fad_search_volume=fad_volume,
                fad_volume_12mo_median=fad_median,
                volume_history_months=history_months,
                top_n=depth,
            ),
            prov,
        )

    # Full pillar assembly. `differentiation` is supplied by validate-tier
    # callers (real review evidence) and stays None at discovery tier.
    demand = build_demand(conn, asin, marketplace.value, row["category_path"], competitor_asins)
    competition = build_competition(conn, marketplace.value, competitor_rows, listing_quality)
    monthly_units = _candidate_units(demand, asin)
    profit = build_profit(row, price_cents, monthly_units, config, profit_overrides)
    risk = build_risk(row, demand, competition, oversized)

    for r in competitor_rows:
        prov = prov.add(f"competitor:{r['asin']}", r["fetch_id"])

    return (
        ScoringInput(
            demand=demand,
            competition=competition,
            differentiation=differentiation,
            profit=profit,
            risk=risk,
            market_median_price_cents=market_median_price,
            oversized=oversized,
            amazon_in_top5=amazon_in_top5 if competitor_rows else None,
            avoid_matches=avoid_matches,
            fad_search_volume=fad_volume,
            fad_volume_12mo_median=fad_median,
            volume_history_months=history_months,
            top_n=depth,
        ),
        prov,
    )


def _candidate_units(demand: DemandReport | None, asin: str) -> int | None:
    if demand is None:
        return None
    for est in demand.sales_estimates:
        if est.asin == asin:
            return est.expected_units
    return demand.market_units_expected or None
