"""Product Workspace assembly (Phase 1) — everything Delium knows about ONE
product, gathered from persisted data (read-only, cache-first, no network).

`assemble_workspace` reads the DB and returns a `Workspace` with the seven tab
payloads the UI renders (overview card + diagnosis, sales/momentum, keywords,
competitors, reviews, profit, risk/verdict) plus shortlist state and the
re-check snapshot series. It applies the active `ResearchProfile` as the profit
assumptions and highlight targets — never changing a kill, gate, or weight.

`deep_dive_plan` reports what evidence is missing for a product and a combined
cost/token estimate, so the UI can offer one confirm-then-fetch button. It makes
no calls itself; the UI service performs the (cache-first, batched) fetches.

Designed with the Phase-2 launch simulator in mind: the momentum series and the
`product_snapshots` re-check history are the evidence base a simulator will draw
on, so they are first-class here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from delium.analysis.models import Marketplace, ScenarioSet
from delium.analysis.scoring import score_opportunity
from delium.config.models import DeliumConfig
from delium.database import repository
from delium.discovery.assembly import build_scoring_input
from delium.discovery.dedupe import group_by_parent
from delium.discovery.diagnostics import CandidateDiagnosis, diagnose_scored
from delium.profile.models import ResearchProfile
from delium.reports.cards import CardFacts, ProductCard, build_card

_GOOD_SALES_DEFAULT = 100  # monthly units that count as "reached good sales"
_RECENT_LAUNCH_DAYS = 365


# ---------------------------------------------------------------------------
# Tab payloads
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MomentumView:
    dates: tuple[str, ...]
    price_usd: tuple[float | None, ...]
    bsr: tuple[int | None, ...]
    reviews: tuple[int | None, ...]
    keepa_monthly_sold: int | None  # Keepa's real "bought past month"
    delium_units_estimate: int | None  # our BSR-based estimate
    emergence: float | None
    age_days: int | None


@dataclass(frozen=True)
class KeywordRow:
    phrase: str
    volume: int | None
    is_primary: bool


@dataclass(frozen=True)
class KeywordsView:
    keywords: tuple[KeywordRow, ...]
    reverse_asin_available: bool  # DataForSEO Labs ranked_keywords fetched?
    note: str


@dataclass(frozen=True)
class CompetitorRow:
    asin: str
    brand: str | None
    price_cents: int | None
    reviews: int | None
    rating: float | None
    bsr: int | None
    monthly_sold: int | None
    age_days: int | None
    variation_count: int


@dataclass(frozen=True)
class CompetitorSet:
    seed: str | None
    competitors: tuple[CompetitorRow, ...]
    launchability_pct: float | None  # % of <12mo launches that reached good sales
    recent_launches: int
    note: str


@dataclass(frozen=True)
class ReviewsView:
    available: bool
    review_count: int
    note: str


@dataclass(frozen=True)
class ProfitView:
    available: bool
    scenarios: ScenarioSet | None
    net_margin: float | None
    roi: float | None
    fee_source: str | None  # 'keepa' | 'table' | None
    units_affordable: int | None
    break_even_units: int | None
    meets_targets: bool
    note: str


@dataclass(frozen=True)
class VariationGroup:
    parent_asin: str | None
    variation_count: int
    sibling_asins: tuple[str, ...]


@dataclass(frozen=True)
class ShortlistState:
    on_shortlist: bool
    status: str | None
    notes: str | None


@dataclass(frozen=True)
class Workspace:
    asin: str
    marketplace: str
    found: bool
    profile_name: str
    facts: CardFacts
    diagnosis: CandidateDiagnosis | None
    card: ProductCard | None
    momentum: MomentumView | None
    keywords: KeywordsView
    competitors: CompetitorSet
    reviews: ReviewsView
    profit: ProfitView
    variation: VariationGroup
    shortlist: ShortlistState
    snapshots: tuple[sqlite3.Row, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Small persisted-data helpers
# ---------------------------------------------------------------------------
def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _latest(rows: list[sqlite3.Row], column: str) -> int | None:
    for r in reversed(rows):
        if r[column] is not None:
            return int(r[column])
    return None


def _age_days(rows: list[sqlite3.Row], as_of: date) -> int | None:
    dates = [d for r in rows if (d := _parse_date(r["captured_on"])) is not None]
    return (as_of - min(dates)).days if dates else None


def _row_get(row: sqlite3.Row, column: str) -> Any:
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


def _parent_of(row: sqlite3.Row) -> str | None:
    value = _row_get(row, "parent_asin")
    return str(value) if value else None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def assemble_workspace(
    conn: sqlite3.Connection,
    asin: str,
    marketplace: Marketplace,
    config: DeliumConfig,
    profile: ResearchProfile,
    *,
    as_of: date | None = None,
) -> Workspace:
    """Gather every tab for one product from the DB (read-only). The active
    `profile` supplies the profit COGS/freight and highlight targets."""
    as_of = as_of or date.today()
    mp = marketplace.value
    row = repository.get_product(conn, asin, mp)
    history = repository.get_price_bsr_history(conn, asin)
    price_cents = _latest(history, "price_cents")
    weight_g = int(row["weight_g"]) if row is not None and row["weight_g"] is not None else None

    # Score with the profile's profit assumptions applied (real Keepa FBA fee is
    # picked up inside build_profit). Verdict rules are unchanged.
    overrides = profile.profit_overrides(price_cents=price_cents, weight_g=weight_g)
    inp, _prov = build_scoring_input(conn, asin, marketplace, config, profit_overrides=overrides)
    diagnosis: CandidateDiagnosis | None = None
    card: ProductCard | None = None
    facts = _facts(row, history, profile)
    if inp is not None:
        scored = score_opportunity(inp, config)
        diagnosis = diagnose_scored(asin, mp, scored)
        card = build_card(diagnosis, facts)

    momentum = _momentum(conn, asin, row, history, inp, as_of) if row is not None else None
    keywords = _keywords(conn, asin, mp)
    competitors = _competitors(conn, asin, mp, keywords, profile, as_of)
    reviews = _reviews(conn, asin)
    profit = _profit(inp.profit if inp is not None else None, profile, price_cents)
    variation = _variation(conn, asin, row, mp)
    shortlist = _shortlist(conn, asin, mp)
    snapshots = tuple(repository.get_product_snapshots(conn, asin, mp))

    return Workspace(
        asin=asin,
        marketplace=mp,
        found=row is not None,
        profile_name=profile.name,
        facts=facts,
        diagnosis=diagnosis,
        card=card,
        momentum=momentum,
        keywords=keywords,
        competitors=competitors,
        reviews=reviews,
        profit=profit,
        variation=variation,
        shortlist=shortlist,
        snapshots=snapshots,
    )


def _facts(
    row: sqlite3.Row | None, history: list[sqlite3.Row], profile: ResearchProfile
) -> CardFacts:
    if row is None:
        return CardFacts()
    return CardFacts(
        title=row["title"],
        brand=row["brand"],
        category=row["category_path"],
        price_cents=_latest(history, "price_cents"),
        bsr=_latest(history, "bsr"),
        reviews=_latest(history, "review_count"),
        monthly_units=_int_or_none(_row_get(row, "monthly_sold")),
    )


def _int_or_none(value: object | None) -> int | None:
    return int(value) if isinstance(value, int) else None


def _momentum(
    conn: sqlite3.Connection,
    asin: str,
    row: sqlite3.Row,
    history: list[sqlite3.Row],
    inp: object,
    as_of: date,
) -> MomentumView:
    from delium.discovery.assembly import _candidate_units  # BSR-based estimate

    derived = repository.get_product_derived(conn, asin)
    delium_units = None
    if derived is not None and derived["est_units_high"] is not None:
        low = derived["est_units_low"] or 0
        high = int(derived["est_units_high"])
        delium_units = round((low + high) / 2)
    elif inp is not None:
        demand = getattr(inp, "demand", None)
        delium_units = _candidate_units(demand, asin) if demand is not None else None

    emergence = _emergence(conn, asin, history, as_of)
    return MomentumView(
        dates=tuple(r["captured_on"] for r in history),
        price_usd=tuple(
            None if r["price_cents"] is None else round(int(r["price_cents"]) / 100, 2)
            for r in history
        ),
        bsr=tuple(None if r["bsr"] is None else int(r["bsr"]) for r in history),
        reviews=tuple(
            None if r["review_count"] is None else int(r["review_count"]) for r in history
        ),
        keepa_monthly_sold=_int_or_none(_row_get(row, "monthly_sold")),
        delium_units_estimate=delium_units,
        emergence=emergence,
        age_days=_age_days(history, as_of),
    )


def _emergence(
    conn: sqlite3.Connection, asin: str, history: list[sqlite3.Row], as_of: date
) -> float | None:
    """Best-effort emergence score from stored history (loads the US emergence
    table; None on any shortfall so the tab simply omits it)."""
    try:
        from delium.analysis.emerging import compute_emergence, load_emerging_data
        from delium.discovery.emerging import _emergence_input

        data = load_emerging_data()
        signal = compute_emergence(_emergence_input(conn, asin, as_of), data.emergence)
        return signal.emergence_score
    except Exception:  # noqa: BLE001 - emergence is advisory; never break the workspace
        return None


def _keywords(conn: sqlite3.Connection, asin: str, marketplace: str) -> KeywordsView:
    phrases = repository.get_serp_keyword_phrases(conn, asin, marketplace)
    rows: list[KeywordRow] = []
    for i, phrase in enumerate(phrases):
        kw = repository.get_keyword(conn, phrase, marketplace)
        volume = None if kw is None or kw["volume"] is None else int(kw["volume"])
        rows.append(KeywordRow(phrase=phrase, volume=volume, is_primary=i == 0))
    note = (
        "Reverse-ASIN keywords (DataForSEO Labs `ranked_keywords`, US only) are not "
        "fetched yet — run a Deep dive to add them."
    )
    return KeywordsView(keywords=tuple(rows), reverse_asin_available=False, note=note)


def _competitors(
    conn: sqlite3.Connection,
    asin: str,
    marketplace: str,
    keywords: KeywordsView,
    profile: ResearchProfile,
    as_of: date,
) -> CompetitorSet:
    seed = next((k.phrase for k in keywords.keywords if k.is_primary), None)
    if seed is None:
        return CompetitorSet(None, (), None, 0, "No main keyword yet — run a Deep dive.")
    ranking = repository.get_serp_rankings(conn, seed, marketplace)
    comp_asins = [r["asin"] for r in ranking if r["asin"] != asin]
    rows = [
        p for a in comp_asins if (p := repository.get_product(conn, a, marketplace)) is not None
    ]
    parents: dict[str, str | None] = {r["asin"]: _parent_of(r) for r in rows}

    built: list[CompetitorRow] = []
    good_threshold = profile.min_monthly_sales or _GOOD_SALES_DEFAULT
    recent = 0
    recent_good = 0
    for r in rows:
        hist = repository.get_price_bsr_history(conn, r["asin"])
        age = _age_days(hist, as_of)
        monthly = _int_or_none(_row_get(r, "monthly_sold"))
        built.append(
            CompetitorRow(
                asin=r["asin"],
                brand=r["brand"],
                price_cents=_latest(hist, "price_cents"),
                reviews=_latest(hist, "review_count"),
                rating=_latest_rating(hist),
                bsr=_latest(hist, "bsr"),
                monthly_sold=monthly,
                age_days=age,
                variation_count=1,
            )
        )
        if age is not None and age <= _RECENT_LAUNCH_DAYS:
            recent += 1
            if monthly is not None and monthly >= good_threshold:
                recent_good += 1

    groups = group_by_parent(
        built,
        asin_of=lambda c: c.asin,
        parents=parents,
        rank=lambda c: float(c.monthly_sold or 0),
    )
    deduped = tuple(
        CompetitorRow(**{**g.representative.__dict__, "variation_count": g.variation_count})
        for g in groups
    )
    launchability = (100.0 * recent_good / recent) if recent > 0 else None
    note = (
        f"Launchability = {recent_good}/{recent} competitors launched in the last 12 months "
        f"reached ≥{good_threshold} units/mo."
        if recent > 0
        else "No competitors with a known launch date under 12 months."
    )
    return CompetitorSet(seed, deduped, launchability, recent, note)


def _latest_rating(rows: list[sqlite3.Row]) -> float | None:
    for r in reversed(rows):
        if r["rating"] is not None:
            return float(r["rating"])
    return None


def _reviews(conn: sqlite3.Connection, asin: str) -> ReviewsView:
    rows = repository.get_reviews_for_asin(conn, asin)
    if rows:
        return ReviewsView(True, len(rows), f"{len(rows)} reviews mined.")
    return ReviewsView(
        False,
        0,
        "No reviews mined yet. Add a review provider + LLM key, then run a Deep dive to "
        "unlock pains, feature gaps, and improvement ideas.",
    )


def _profit(
    scenarios: ScenarioSet | None, profile: ResearchProfile, price_cents: int | None
) -> ProfitView:
    if scenarios is None:
        return ProfitView(
            False,
            None,
            None,
            None,
            None,
            None,
            None,
            False,
            "Profit needs dimensions + weight + price. Run a Deep dive to fetch them.",
        )
    expected = scenarios.expected
    landed = expected.landed_cost_cents
    net = expected.net_profit_cents
    units_affordable = profile.units_affordable(landed)
    break_even = None
    if net > 0:
        break_even = -(-expected.launch_capital_cents // net)  # ceil
    fee_source = expected.fees.fulfillment_source
    note = (
        "FBA fulfilment fee from Keepa (real)."
        if fee_source == "keepa"
        else "FBA fulfilment fee estimated from our fee table (no Keepa fee captured)."
    )
    return ProfitView(
        available=True,
        scenarios=scenarios,
        net_margin=expected.net_margin,
        roi=expected.roi,
        fee_source=fee_source,
        units_affordable=units_affordable,
        break_even_units=break_even,
        meets_targets=profile.meets_targets(net_margin=expected.net_margin, roi=expected.roi),
        note=note,
    )


def _variation(
    conn: sqlite3.Connection, asin: str, row: sqlite3.Row | None, marketplace: str
) -> VariationGroup:
    parent = _parent_of(row) if row is not None else None
    if not parent:
        return VariationGroup(None, 1, ())
    siblings = repository.get_products_by_parent(conn, parent, marketplace)
    others = tuple(s["asin"] for s in siblings if s["asin"] != asin)
    return VariationGroup(str(parent), len(siblings) or 1, others)


def _shortlist(conn: sqlite3.Connection, asin: str, marketplace: str) -> ShortlistState:
    entry = repository.get_shortlist_entry(conn, asin, marketplace)
    if entry is None:
        return ShortlistState(False, None, None)
    return ShortlistState(True, entry["status"], entry["notes"])


# ---------------------------------------------------------------------------
# Deep dive planner
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeepDiveStep:
    key: str  # 'product' | 'competitors' | 'keywords' | 'reviews'
    label: str
    provider: str
    est_cost_usd: float
    est_keepa_tokens: int
    reason: str


@dataclass(frozen=True)
class DeepDivePlan:
    asin: str
    steps: tuple[DeepDiveStep, ...]

    @property
    def total_cost_usd(self) -> float:
        return round(sum(s.est_cost_usd for s in self.steps), 2)

    @property
    def total_keepa_tokens(self) -> int:
        return sum(s.est_keepa_tokens for s in self.steps)

    @property
    def has_work(self) -> bool:
        return bool(self.steps)


def deep_dive_plan(
    workspace: Workspace,
    config: DeliumConfig,
    *,
    reviews_configured: bool,
    dataforseo_configured: bool,
    keepa_configured: bool,
    serp_depth: int = 10,
) -> DeepDivePlan:
    """What this product is missing and the combined cost/token estimate to fetch
    it (cache-first — a step is listed only when the data is actually absent).
    Estimates use the same per-call figures as the pre-flight cost panels."""
    steps: list[DeepDiveStep] = []
    dfs_per_call = 0.01  # DataForSEO keyword/SERP call (docs/data-economics.md)
    review_per_asin = 0.30

    if not workspace.found and keepa_configured:
        steps.append(
            DeepDiveStep(
                "product",
                "Fetch product (Keepa)",
                "Keepa",
                0.0,
                2,
                "Product not fetched in this marketplace yet.",
            )
        )
    if not workspace.competitors.competitors and dataforseo_configured:
        # One keyword bundle to resolve the SERP + Keepa hydration of the page-1 set.
        steps.append(
            DeepDiveStep(
                "competitors",
                "Fetch competitor set (DataForSEO SERP → Keepa)",
                "DataForSEO+Keepa",
                round(3 * dfs_per_call, 2),
                2 * serp_depth,
                "No page-one competitor set for the main keyword.",
            )
        )
    if not workspace.keywords.reverse_asin_available and dataforseo_configured:
        steps.append(
            DeepDiveStep(
                "keywords",
                "Reverse-ASIN keywords (DataForSEO Labs, US only)",
                "DataForSEO",
                round(1 * dfs_per_call, 2),
                0,
                "Ranked keywords the product itself ranks for are not fetched.",
            )
        )
    if not workspace.reviews.available and reviews_configured:
        cap = config.budgets.max_data_usd_per_validate
        steps.append(
            DeepDiveStep(
                "reviews",
                "Mine reviews + run agents",
                "Reviews+LLM",
                round(min(review_per_asin * 4, cap), 2),
                0,
                "No reviews mined; pains/feature-gaps unavailable.",
            )
        )
    return DeepDivePlan(asin=workspace.asin, steps=tuple(steps))
