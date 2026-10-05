"""Zombie-listing discovery — orchestration (Keepa only, cache-first).

Pipeline (mirrors the other discovery flows; the engines stay pure):
  0. preflight — project Keepa tokens + USD, abort before spending over caps.
  1. sweep    — Keepa Product Finder for out-of-stock-but-reviewed listings.
  2. hydrate  — batched, cache-first Keepa /product for the candidates.
  3. verify   — rebuild each listing's availability timeline from the RAW csv
                (the -1 gaps the normalized history drops) and run the pure
                `analysis.zombies` verdict.
  4. demand   — OPTIONAL, top candidates only, with confirm: a DataForSEO SERP
                read of the main keyword ("are similar products selling now?").

No provider clients are constructed here — the caller injects factories, so
tests use fakes. scoring stays untouched; this is a separate, additive verdict.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast

from delium.analysis.zombies import (
    ZombieEvidence,
    ZombieFinderConfig,
    ZombieResult,
    ZombieVerdict,
    build_stock_timeline,
    build_zombie_finder_selection,
    compute_zombie,
    load_zombie_data,
)
from delium.config.models import DeliumConfig
from delium.database import repository
from delium.providers.keepa import (
    _CSV_AMAZON,
    _CSV_COUNT_NEW,
    _CSV_COUNT_REVIEWS,
    _CSV_NEW,
    _CSV_RATING,
    _CSV_SALES,
    KeepaClient,
    finder_token_estimate,
    raw_csv_series,
)
from delium.utils.logging import get_logger

log = get_logger(__name__)

_NO_KEEPA = frozenset({"AU"})  # Keepa has no Australia coverage
_DFS_CALL_USD = 0.01

KeepaFactory = Callable[[str], Any]
DfsFactory = Callable[[str], Any]


class ZombieAbortedError(Exception):
    """Raised in preflight when the projection exceeds a cap (before any spend)."""


@dataclass(frozen=True)
class ZombieParams:
    marketplaces: tuple[str, ...] = ("US",)
    min_dead_months: float = 6.0
    min_reviews: int = 50
    min_rating: float = 4.0
    out_of_stock_pct_90: int = 90
    sweep_target: int = 100  # max ASINs to bring back from the finder
    per_page: int = 100
    top_n: int = 20
    budget_cap_tokens: int | None = None
    max_spend_usd: float | None = None
    check_demand: bool = False  # DataForSEO SERP for the top candidates
    scheduled: bool = False


@dataclass(frozen=True)
class ZombieClients:
    keepa_factory: KeepaFactory | None = None
    dfs_factory: DfsFactory | None = None


@dataclass(frozen=True)
class ZombieCostProjection:
    finder_tokens: int
    hydrate_tokens: int
    dfs_usd: float
    notes: tuple[str, ...] = ()

    @property
    def total_tokens(self) -> int:
        return self.finder_tokens + self.hydrate_tokens

    @property
    def total_usd(self) -> float:
        return round(self.dfs_usd, 2)

    def over_caps(self, params: ZombieParams) -> str | None:
        if params.budget_cap_tokens is not None and self.total_tokens > params.budget_cap_tokens:
            return (
                f"projected {self.total_tokens} Keepa tokens exceeds --budget-cap "
                f"{params.budget_cap_tokens}"
            )
        if params.max_spend_usd is not None and self.total_usd > params.max_spend_usd:
            return (
                f"projected ${self.total_usd:.2f} exceeds --max-spend ${params.max_spend_usd:.2f}"
            )
        return None


@dataclass(frozen=True)
class FinderDiagnostic:
    """Why a marketplace's finder call produced what it did — surfaced in the CLI
    and UI so an empty result is never silent. Never carries the API key."""

    marketplace: str
    skipped_reason: str | None = None  # set when the call was NOT made
    http_status: int | None = None
    error: str | None = None  # Keepa's error body (status/tokensLeft/message)
    total_results: int | None = None  # Keepa's totalResults for the query
    returned: int = 0  # ASINs the query returned
    used_fallback: bool = False  # retried without the optional OOS filter
    filters: dict[str, Any] = field(default_factory=dict)  # the exact selection sent

    def summary(self) -> str:
        if self.skipped_reason is not None:
            return f"{self.marketplace}: skipped — {self.skipped_reason}"
        if self.error is not None:
            return f"{self.marketplace}: Keepa finder failed — {self.error}"
        tot = "unknown" if self.total_results is None else f"{self.total_results}"
        note = (
            f"{self.marketplace}: finder returned {self.returned} of {tot} matching"
            f"{' (core-only fallback)' if self.used_fallback else ''}"
        )
        if self.returned == 0:
            keys = ", ".join(f"{k}={self.filters[k]}" for k in sorted(self.filters))
            note += f". Filters sent: {keys}"
        return note


@dataclass
class ZombieReport:
    run_id: str
    marketplaces: tuple[str, ...]
    results: list[ZombieResult] = field(default_factory=list)
    swept: int = 0
    hydrated: int = 0
    keepa_tokens: int = 0
    data_usd: float = 0.0
    notes: list[str] = field(default_factory=list)
    diagnostics: list[FinderDiagnostic] = field(default_factory=list)

    @property
    def verified(self) -> list[ZombieResult]:
        return [r for r in self.results if r.verdict is ZombieVerdict.VERIFIED]


def project_costs(params: ZombieParams) -> ZombieCostProjection:
    """Worst-case Keepa tokens + USD, so the caps bind on the high side."""
    active = [m for m in params.marketplaces if m not in _NO_KEEPA]
    notes = tuple(
        f"{m} pending — Keepa has no {m} data; it will be skipped."
        for m in params.marketplaces
        if m in _NO_KEEPA
    )
    finder_tokens = len(active) * finder_token_estimate(params.per_page)
    hydrate_tokens = params.sweep_target * 2  # ~2 tokens/product, worst case all missed
    dfs_usd = round(params.top_n * 3 * _DFS_CALL_USD, 2) if params.check_demand else 0.0
    return ZombieCostProjection(finder_tokens, hydrate_tokens, dfs_usd, notes)


# ---------------------------------------------------------------------------
# Evidence extraction from the RAW Keepa product (keeps the -1 gaps)
# ---------------------------------------------------------------------------
def _last_value(series: list[tuple[int, int]]) -> int | None:
    return series[-1][1] if series else None


def zombie_evidence_from_raw(
    raw: dict[str, Any], *, asin: str, marketplace: str, as_of: date
) -> ZombieEvidence:
    """Build `ZombieEvidence` from a raw Keepa product dict. The availability
    timeline comes from the NEW/offer-count csv series WITH their -1 gaps;
    rating/reviews are the series' current values; Amazon-on-listing is whether
    the Amazon price series currently carries an offer."""
    csv = raw.get("csv") or []
    new = raw_csv_series(csv, _CSV_NEW)
    count_new = raw_csv_series(csv, _CSV_COUNT_NEW)
    sales = raw_csv_series(csv, _CSV_SALES)
    timeline = build_stock_timeline(new, count_new, sales, as_of=as_of)

    rating_raw = _last_value(raw_csv_series(csv, _CSV_RATING))
    rating = round(rating_raw / 10.0, 1) if rating_raw is not None and rating_raw >= 0 else None
    reviews_raw = _last_value(raw_csv_series(csv, _CSV_COUNT_REVIEWS))
    reviews = reviews_raw if (reviews_raw is not None and reviews_raw >= 0) else None
    amazon_last = _last_value(raw_csv_series(csv, _CSV_AMAZON))
    amazon_on_listing = None if amazon_last is None else amazon_last >= 0
    monthly_sold = raw.get("monthlySold")
    monthly_sold = int(monthly_sold) if isinstance(monthly_sold, int) and monthly_sold > 0 else None

    return ZombieEvidence(
        asin=asin,
        marketplace=marketplace,
        timeline=timeline,
        rating=rating,
        reviews=reviews,
        monthly_sold=monthly_sold,
        brand=raw.get("brand"),
        amazon_on_listing=amazon_on_listing,
        has_current_new_offer=(
            None if timeline.currently_out_of_stock is None else not timeline.currently_out_of_stock
        ),
    )


def _main_keyword(raw: dict[str, Any]) -> str | None:
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    words = [w for w in title.split() if w.isalnum() or "-" in w][:4]
    return " ".join(words) or None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_zombies(
    conn: Any,
    *,
    params: ZombieParams,
    config: DeliumConfig,
    clients: ZombieClients,
    as_of: date | None = None,
    confirm: Callable[[ZombieCostProjection], bool] | None = None,
) -> ZombieReport:
    """Find and verify zombie listings across the requested marketplaces."""
    as_of = as_of or date.today()
    run_id = repository.insert_run(conn, command="zombies", input_=",".join(params.marketplaces))
    report = ZombieReport(run_id=run_id, marketplaces=params.marketplaces)

    active_mps = [m for m in params.marketplaces if m not in _NO_KEEPA]
    projection = project_costs(params)
    report.notes.extend(projection.notes)
    for m in params.marketplaces:
        if m in _NO_KEEPA:
            report.diagnostics.append(
                FinderDiagnostic(marketplace=m, skipped_reason=f"Keepa has no {m} data")
            )
    over = projection.over_caps(params)
    if over is not None:
        for m in active_mps:
            report.diagnostics.append(FinderDiagnostic(marketplace=m, skipped_reason=over))
        repository.finish_run(conn, run_id, status="failed")
        raise ZombieAbortedError(over)
    if not active_mps:
        repository.finish_run(conn, run_id, status="failed")
        raise ZombieAbortedError("no Keepa-covered marketplaces requested")
    if clients.keepa_factory is None:
        reason = "Keepa is not configured — the zombie search needs the Product Finder."
        report.notes.append(reason)
        for m in active_mps:
            report.diagnostics.append(FinderDiagnostic(marketplace=m, skipped_reason=reason))
        repository.finish_run(conn, run_id, status="complete")
        return report
    if not params.scheduled and confirm is not None and not confirm(projection):
        repository.finish_run(conn, run_id, status="failed")
        raise ZombieAbortedError("declined at confirmation")

    results: list[ZombieResult] = []
    finder_tokens = 0
    for mp in active_mps:
        mp_results, mp_tokens = _sweep_marketplace(
            conn, mp, params, config, clients, run_id, as_of, report
        )
        results.extend(mp_results)
        finder_tokens += mp_tokens

    results.sort(key=lambda r: r.score if r.score is not None else -1.0, reverse=True)
    top = results[: params.top_n]

    # Optional current-demand enrichment (top candidates only).
    if params.check_demand and clients.dfs_factory is not None:
        top = _enrich_current_demand(conn, top, params, config, clients, run_id)

    report.results = top
    # Finder calls are not persisted to raw_fetches, so count their tokens here on
    # top of the hydrate tokens the run ledger records (otherwise a sweep that
    # returns 0 misleadingly shows "0 tokens").
    report.keepa_tokens = finder_tokens + repository.run_token_total(conn, run_id)
    report.data_usd = round(repository.run_cost_total(conn, run_id), 4)
    repository.finish_run(conn, run_id, status="complete")
    return report


def _sweep_marketplace(
    conn: Any,
    mp: str,
    params: ZombieParams,
    config: DeliumConfig,
    clients: ZombieClients,
    run_id: str,
    as_of: date,
    report: ZombieReport,
) -> tuple[list[ZombieResult], int]:
    """Finder → cache-first hydrate → verify for one marketplace. Returns
    (results, finder_tokens_consumed) and records a FinderDiagnostic."""
    from delium.ingestion import cached_raw_product, hydrate_products
    from delium.providers.base import ProviderError

    assert clients.keepa_factory is not None
    client = cast(KeepaClient, clients.keepa_factory(mp))
    cfg = ZombieFinderConfig(
        min_rating=params.min_rating,
        min_reviews=params.min_reviews,
        out_of_stock_pct_90=params.out_of_stock_pct_90,
    )
    selection = build_zombie_finder_selection(cfg, per_page=params.per_page)
    finder_tokens = 0
    try:
        finder = client.product_finder(selection)
    except ProviderError as exc:
        log.warning("zombie finder failed (%s): %s | filters=%s", mp, exc, selection)
        report.diagnostics.append(
            FinderDiagnostic(marketplace=mp, error=str(exc), filters=selection)
        )
        return [], finder_tokens
    finder_tokens += finder.tokens_consumed
    used_fallback = False

    # If the full selection returns nothing, retry once WITHOUT the optional
    # out-of-stock-percentage filter (it can over-restrict), so one strict filter
    # can't zero out the sweep. The fallback attempt becomes the reported result.
    if not finder.asins and "outOfStockPercentage90_NEW_gte" in selection:
        core = build_zombie_finder_selection(cfg, per_page=params.per_page, core_only=True)
        try:
            retry = client.product_finder(core)
            finder_tokens += retry.tokens_consumed
            finder, selection, used_fallback = retry, core, True
        except ProviderError as exc:
            log.warning("zombie finder fallback failed (%s): %s", mp, exc)

    asins = list(finder.asins)[: params.sweep_target]
    report.swept += len(asins)
    report.diagnostics.append(
        FinderDiagnostic(
            marketplace=mp,
            http_status=finder.http_status,
            total_results=finder.total_results,
            returned=len(asins),
            used_fallback=used_fallback,
            filters=selection,
        )
    )
    if not asins:
        log.warning(
            "zombie finder returned 0 (%s): total_results=%s filters=%s",
            mp,
            finder.total_results,
            selection,
        )
        return [], finder_tokens

    conn.commit()  # release the write lock before ingestion opens its own connection
    hydrate_products(asins, run_id=run_id, client=client, config=config)

    thresholds = load_zombie_data(mp)
    out: list[ZombieResult] = []
    for asin in asins:
        raw = cached_raw_product(conn, asin, mp)
        if raw is None:
            continue
        report.hydrated += 1
        ev = zombie_evidence_from_raw(raw, asin=asin, marketplace=mp, as_of=as_of)
        # Honour an explicit, stricter min-dead-months from the caller.
        thresholds_eff = thresholds
        if params.min_dead_months and params.min_dead_months > thresholds.min_months:
            from dataclasses import replace

            thresholds_eff = replace(thresholds, min_months=params.min_dead_months)
        out.append(compute_zombie(ev, thresholds_eff))
    return out, finder_tokens


def _enrich_current_demand(
    conn: Any,
    results: list[ZombieResult],
    params: ZombieParams,
    config: DeliumConfig,
    clients: ZombieClients,
    run_id: str,
) -> list[ZombieResult]:
    """For the top candidates: read the main-keyword SERP and set a 0-100
    current-demand signal ("are similar products selling now?"). Best-effort;
    a failure leaves the result unchanged."""
    from dataclasses import replace

    from delium.ingestion import cached_raw_product, fetch_keywords
    from delium.providers.base import ProviderError

    assert clients.dfs_factory is not None
    enriched: list[ZombieResult] = []
    for r in results:
        raw = cached_raw_product(conn, r.asin, r.marketplace)
        seed = _main_keyword(raw) if raw else None
        demand: float | None = None
        if seed is not None:
            try:
                fetch_keywords(
                    seed, run_id=run_id, client=clients.dfs_factory(r.marketplace), config=config
                )
                rankings = repository.get_serp_rankings(conn, seed, r.marketplace)
                # More live, ranked competitors on the seed keyword ⇒ the niche is
                # demonstrably selling now. Capped, monotonic, deterministic.
                demand = min(100.0, len([x for x in rankings if x["asin"]]) * 10.0)
            except ProviderError as exc:
                log.warning("zombie SERP failed (%s): %s", r.asin, exc)
        if demand is not None:
            ev = replace(r.evidence, current_demand=demand)
            thresholds = load_zombie_data(r.marketplace)
            if params.min_dead_months and params.min_dead_months > thresholds.min_months:
                thresholds = replace(thresholds, min_months=params.min_dead_months)
            enriched.append(compute_zombie(ev, thresholds))
        else:
            enriched.append(r)
    return enriched
