"""Service layer for the UI — calls the SAME internal functions as the CLI.

Every action here goes through `delium.ingestion.*`, `delium.validation`,
`delium.discovery`, and `delium.reports.render` exactly as `cli/main.py` does:
build a provider from env, open a run, call the pipeline, close the run, return
the typed result. No engine, scoring, or verdict logic is re-implemented, and it
never shells out to the CLI. Network access happens only when a provider is
built and the underlying (cache-first) ingestion decides to fetch.

Missing-credential cases raise `ProviderError`; the Streamlit layer catches it
and shows a friendly message rather than a stack trace.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from delium.analysis.models import Marketplace, SourceMaturity
from delium.config.models import DeliumConfig
from delium.database import initialize_database, repository
from delium.database.connection import get_connection
from delium.discovery.daily_scan import STAGE_NAMES
from delium.ingestion import (
    CrossMarketCandidate,
    discover_cross_market,
    fetch_keywords,
    fetch_product,
    fetch_reviews,
)
from delium.ingestion.products import ProductView
from delium.providers import ReviewProviderChain, build_review_provider
from delium.providers.base import ProviderError
from delium.providers.dataforseo import DataForSeoClient
from delium.providers.keepa import KeepaClient


# ---------------------------------------------------------------------------
# Provider construction (mirrors cli/main.py; never raises past the caller)
# ---------------------------------------------------------------------------
def _provider_factories() -> tuple[Callable[[str], Any] | None, Callable[[str], Any] | None]:
    keepa: Callable[[str], Any] | None
    dfs: Callable[[str], Any] | None
    try:
        KeepaClient.from_env()
        keepa = lambda mp: KeepaClient.from_env(marketplace=mp)  # noqa: E731
    except ProviderError:
        keepa = None
    try:
        DataForSeoClient.from_env()
        dfs = lambda mp: DataForSeoClient.from_env(marketplace=mp)  # noqa: E731
    except ProviderError:
        dfs = None
    return keepa, dfs


def _review_probe() -> ReviewProviderChain | None:
    try:
        return build_review_provider()
    except ProviderError:
        return None


# ---------------------------------------------------------------------------
# Keyword research  (== `fetch keywords`)
# ---------------------------------------------------------------------------
def keyword_research(seed: str, marketplace: str, config: DeliumConfig, *, force: bool) -> Any:
    initialize_database()
    client = DataForSeoClient.from_env(marketplace=marketplace)
    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="fetch.keywords", input_=f"{marketplace}:{seed}"
        )
    try:
        result = fetch_keywords(seed, run_id=run_id, client=client, config=config, force=force)
    except ProviderError:
        with get_connection() as conn:
            repository.finish_run(conn, run_id, status="failed")
        raise
    with get_connection() as conn:
        repository.finish_run(conn, run_id, status="complete", data_cost_usd=result.cost_usd)
    return result


# ---------------------------------------------------------------------------
# Product lookup  (== `fetch product`)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProductLookup:
    view: ProductView
    history: list[sqlite3.Row]  # price_bsr_history rows, oldest→newest


def product_lookup(
    asin: str, marketplace: str, config: DeliumConfig, *, force: bool
) -> ProductLookup:
    initialize_database()
    client = KeepaClient.from_env(marketplace=marketplace)
    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="fetch.product", input_=f"{marketplace}:{asin}"
        )
    try:
        view = fetch_product(asin, run_id=run_id, client=client, config=config, force=force)
    except ProviderError:
        with get_connection() as conn:
            repository.finish_run(conn, run_id, status="failed")
        raise
    with get_connection() as conn:
        repository.finish_run(
            conn, run_id, status="complete", data_cost_usd=view.cost_usd if view else 0.0
        )
        history = repository.get_price_bsr_history(conn, asin) if view and view.found else []
    if view is None:
        view = ProductView(asin=asin, found=False, from_cache=False, marketplace=marketplace)
    return ProductLookup(view=view, history=history)


# ---------------------------------------------------------------------------
# Validate  (== `validate`)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ValidateResult:
    report: Any  # ValidationReport
    markdown: str
    tokens_used: int = 0


@dataclass(frozen=True)
class DiscoverResult:
    report: Any  # DiscoveryReport
    tokens_used: int = 0


def validate(
    target: str,
    marketplace: str,
    config: DeliumConfig,
    *,
    cogs: float | None = None,
    freight: float | None = None,
    dims_mm: tuple[int, int, int] | None = None,
    weight_g: int | None = None,
    force: bool = False,
) -> ValidateResult:
    from delium.agents import build_llm_client
    from delium.validation import Clients, ValidationRequest, ValidationStatus, run_validation

    initialize_database()
    keepa_factory, dfs_factory = _provider_factories()
    clients = Clients(
        keepa=keepa_factory,
        dfs=dfs_factory,
        reviews=_review_probe(),
        llm=build_llm_client(config.agents),
    )
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate", input_=f"{marketplace}:{target}")
    request = ValidationRequest(
        target=target,
        marketplace=Marketplace(marketplace),
        run_id=run_id,
        force=force,
        cogs_usd=cogs,
        freight_usd=freight,
        dims_mm=dims_mm,
        weight_g=weight_g,
    )
    with get_connection() as conn:
        report = run_validation(conn, request, config, clients)
        terminal = report.status in (ValidationStatus.SCORED, ValidationStatus.HARD_KILLED)
        status = ("degraded" if report.hydration.degraded else "complete") if terminal else "failed"
        repository.finish_run(
            conn,
            run_id,
            status=status,
            data_cost_usd=report.data_cost_usd,
            llm_cost_usd=report.llm_cost_usd,
        )
        tokens = repository.run_token_total(conn, run_id)
    return ValidateResult(report=report, markdown=render_report(report), tokens_used=tokens)


def render_report(report: Any) -> str:
    """Render a ValidationReport to Markdown using the SAME renderer as the CLI,
    with quotes resolved from stored reviews by id (never model text)."""
    from delium.reports.render import quote_ids, render_validation

    title = _product_title(report.asin, report.marketplace.value) if report.asin else None
    quotes = _review_quotes(report.asin, quote_ids(report)) if report.asin else {}
    return render_validation(report, product_title=title, quotes=quotes)


def _product_title(asin: str, marketplace: str) -> str | None:
    with get_connection() as conn:
        row = repository.get_product(conn, asin, marketplace)
    return row["title"] if row is not None else None


def _review_quotes(asin: str | None, ids: list[str]) -> dict[str, tuple[int, str]]:
    if not asin or not ids:
        return {}
    wanted = set(ids)
    with get_connection() as conn:
        rows = repository.get_reviews_for_asin(conn, asin)
    return {
        row["review_id"]: (int(row["stars"]), row["body"] or row["title"] or "")
        for row in rows
        if row["review_id"] in wanted
    }


# ---------------------------------------------------------------------------
# Discover  (== `discover`)
# ---------------------------------------------------------------------------
def discover(
    keywords: list[str], marketplace: str, config: DeliumConfig, *, force: bool
) -> DiscoverResult:
    from delium.discovery import run_discovery

    initialize_database()
    keepa_factory, dfs_factory = _provider_factories()
    mp = Marketplace(marketplace)
    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="discover", input_=f"{marketplace}:{','.join(keywords)}"
        )
    if dfs_factory is not None:
        for kw in keywords:
            # A failed seed is skipped; discovery still runs on whatever resolved.
            with contextlib.suppress(ProviderError):
                fetch_keywords(
                    kw, run_id=run_id, client=dfs_factory(marketplace), config=config, force=force
                )
    with get_connection() as conn:
        report = run_discovery(
            conn,
            marketplace=mp,
            config=config,
            run_id=run_id,
            keywords=keywords,
            asins=[],
            cross_market_targets=(),
            keepa_factory=keepa_factory,
            dfs_factory=dfs_factory,
        )
        repository.finish_run(conn, run_id, status="complete")
        tokens = repository.run_token_total(conn, run_id)
    return DiscoverResult(report=report, tokens_used=tokens)


# ---------------------------------------------------------------------------
# Cross-market  (== `cross-market`)
# ---------------------------------------------------------------------------
def cross_market(
    source: str,
    targets: list[str],
    config: DeliumConfig,
    *,
    limit: int = 25,
    min_source_maturity: str = "emerging",
    min_monthly_units: int | None = None,
) -> list[CrossMarketCandidate]:
    initialize_database()
    target_mps = [t for t in targets if t != source]
    maturity = SourceMaturity(min_source_maturity.strip().lower())
    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="cross-market", input_=f"{source}->{','.join(target_mps)}"
        )
        candidates = discover_cross_market(
            conn,
            source_mp=Marketplace(source),
            target_mps=tuple(Marketplace(mp) for mp in target_mps),
            config=config,
            run_id=run_id,
            limit=limit,
            min_source_maturity=maturity,
            min_monthly_units=min_monthly_units,
        )
        repository.finish_run(conn, run_id, status="complete")
    return candidates


# ---------------------------------------------------------------------------
# Emerging  (== `emerging`)
# ---------------------------------------------------------------------------
def emerging(
    marketplace: str,
    category_ids: list[int],
    config: DeliumConfig,
    *,
    overrides: dict[str, int] | None = None,
) -> Any:
    from delium.discovery.emerging import run_emerging

    initialize_database()
    keepa_factory, dfs_factory = _provider_factories()
    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="emerging", input_=f"{marketplace}:{category_ids}"
        )
    with get_connection() as conn:
        report = run_emerging(
            conn,
            marketplace=Marketplace(marketplace),
            category_ids=category_ids,
            config=config,
            run_id=run_id,
            keepa_factory=keepa_factory,
            dfs_factory=dfs_factory,
            overrides=overrides,
        )
        repository.finish_run(conn, run_id, status="complete")
    return report


def discovery_product_facts(report: Any) -> dict[str, dict[str, Any]]:
    """Title + latest price/BSR for a discovery report's killed candidates (DB
    reads only). Lets the UI show a full kill table — the price/BSR come from the
    hydrated product row + its newest history point, since the SERP table stores
    neither."""
    facts: dict[str, dict[str, Any]] = {}
    with get_connection() as conn:
        for ec in report.killed:
            row = repository.get_product(conn, ec.asin, ec.marketplace.value)
            history = repository.get_price_bsr_history(conn, ec.asin)
            latest = history[-1] if history else None
            facts[ec.asin] = {
                "title": row["title"] if row is not None else None,
                "price_cents": latest["price_cents"] if latest is not None else None,
                "bsr": latest["bsr"] if latest is not None else None,
            }
    return facts


# ---------------------------------------------------------------------------
# Usage page (token/spend reporting — DB reads + a free Keepa /token call)
# ---------------------------------------------------------------------------
def keepa_token_status() -> Any | None:
    """Live Keepa token balance + refill rate (a free `/token` call, 0 tokens).
    Returns None when Keepa is not configured or the call fails — never the key."""
    try:
        client = KeepaClient.from_env()
    except ProviderError:
        return None
    try:
        return client.token_status()
    except ProviderError:
        return None


def spend_by_provider_day(days: int = 30) -> list[sqlite3.Row]:
    initialize_database()
    with get_connection() as conn:
        return repository.spend_by_provider_day(conn, days=days)


def run_type_summary() -> list[sqlite3.Row]:
    initialize_database()
    with get_connection() as conn:
        return repository.run_type_summary(conn)


def parent_map(asins: list[str], marketplace: str) -> dict[str, str | None]:
    """ASIN → Keepa parent ASIN for a marketplace (variation dedupe). Read-only."""
    if not asins:
        return {}
    initialize_database()
    with get_connection() as conn:
        return repository.get_parent_map(conn, asins, marketplace)


def emerging_facts(report: Any) -> tuple[dict[str, dict[str, Any]], dict[str, str | None]]:
    """(facts_by_asin, parents_by_asin) for a report's ranked candidates — the
    readable-table columns that live in the DB, not on the report (title, brand,
    category, latest price/BSR/reviews, Keepa monthly-units estimate, parent ASIN).
    Read-only."""
    asins = [c.asin for c in report.ranked]
    facts: dict[str, dict[str, Any]] = {}
    with get_connection() as conn:
        parents = repository.get_parent_map(conn, asins, report.marketplace.value)
        for asin in asins:
            row = repository.get_product(conn, asin, report.marketplace.value)
            history = repository.get_price_bsr_history(conn, asin)
            derived = repository.get_product_derived(conn, asin)

            def _latest(col: str, hist: list[sqlite3.Row] = history) -> int | None:
                for r in reversed(hist):
                    if r[col] is not None:
                        return int(r[col])
                return None

            facts[asin] = {
                "title": row["title"] if row is not None else None,
                "brand": row["brand"] if row is not None else None,
                "category": row["category_path"] if row is not None else None,
                "price_cents": _latest("price_cents"),
                "bsr": _latest("bsr"),
                "reviews": _latest("review_count"),
                "monthly_units": (derived["est_units_high"] if derived is not None else None),
            }
    return facts, parents


def recent_emerging_runs(limit: int = 25) -> list[sqlite3.Row]:
    initialize_database()
    with get_connection() as conn:
        return repository.list_emerging_runs(conn, limit=limit)


def emerging_candidates(run_id: str) -> list[sqlite3.Row]:
    initialize_database()
    with get_connection() as conn:
        return repository.get_emerging_candidates(conn, run_id)


# ---------------------------------------------------------------------------
# History (DB reads only — no providers)
# ---------------------------------------------------------------------------
def recent_runs(limit: int = 50) -> list[sqlite3.Row]:
    initialize_database()
    with get_connection() as conn:
        return repository.list_runs(conn, limit=limit)


def recent_validations(limit: int = 50) -> list[sqlite3.Row]:
    initialize_database()
    with get_connection() as conn:
        return repository.list_validations(conn, limit=limit)


def report_files() -> list[dict[str, Any]]:
    """Saved Markdown validation reports on disk, newest first."""
    from delium.utils.paths import get_reports_dir

    reports_dir = get_reports_dir()
    if not reports_dir.exists():
        return []
    files = sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "path": str(p), "modified": _mtime(p)} for p in files]


def read_report(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


def _mtime(path: Any) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


# Re-export for the history page date helper / typing convenience.
__all__ = [
    "DiscoverResult",
    "ProductLookup",
    "ValidateResult",
    "cross_market",
    "discover",
    "discovery_product_facts",
    "emerging_facts",
    "keepa_token_status",
    "keyword_research",
    "parent_map",
    "product_lookup",
    "read_report",
    "recent_runs",
    "recent_validations",
    "render_report",
    "report_files",
    "run_type_summary",
    "spend_by_provider_day",
    "validate",
]


# ---------------------------------------------------------------------------
# Research Profile (Phase 1) — the shared preferences behind every page
# ---------------------------------------------------------------------------
def active_profile() -> Any:
    """The active ResearchProfile (seeded on first use)."""
    from delium.profile import store

    initialize_database()
    with get_connection() as conn:
        return store.load_active(conn)


def list_profiles() -> list[Any]:
    from delium.profile import store

    initialize_database()
    with get_connection() as conn:
        return store.list_profiles(conn)


def save_profile(profile: Any) -> str:
    from delium.profile import store

    initialize_database()
    with get_connection() as conn:
        return store.save(conn, profile)


def activate_profile(profile_id: str) -> None:
    from delium.profile import store

    with get_connection() as conn:
        store.set_active(conn, profile_id)


def delete_profile(profile_id: str) -> None:
    from delium.profile import store

    with get_connection() as conn:
        store.delete(conn, profile_id)


# ---------------------------------------------------------------------------
# Product Workspace (Phase 1)
# ---------------------------------------------------------------------------
def workspace(asin: str, marketplace: str, config: DeliumConfig) -> Any:
    """Assemble the full workspace for one product (read-only, cache-first)."""
    from delium.discovery.workspace import assemble_workspace
    from delium.profile import store

    initialize_database()
    with get_connection() as conn:
        profile = store.load_active(conn)
        return assemble_workspace(
            conn, asin.strip().upper(), Marketplace(marketplace), config, profile
        )


def deep_dive_plan(ws: Any, config: DeliumConfig) -> Any:
    """What the workspace is missing + a combined cost/token estimate."""
    from delium.discovery.workspace import deep_dive_plan as _plan
    from delium.ui import credentials

    return _plan(
        ws,
        config,
        reviews_configured=credentials.is_configured("reviews"),
        dataforseo_configured=credentials.is_configured("dataforseo"),
        keepa_configured=credentials.is_configured("keepa"),
        serp_depth=config.discovery.serp_depth,
    )


def run_deep_dive(asin: str, marketplace: str, config: DeliumConfig, plan: Any) -> tuple[Any, int]:
    """Fetch the missing evidence for a product (cache-first, batched Keepa) and
    return (refreshed workspace, keepa tokens used). PAID — the UI confirms first.
    Reverse-ASIN keywords (DataForSEO Labs) are not wired yet and are skipped."""
    from delium.ingestion import hydrate_products

    initialize_database()
    asin = asin.strip().upper()
    keepa_factory, dfs_factory = _provider_factories()
    step_keys = {s.key for s in plan.steps}
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="deep-dive", input_=f"{marketplace}:{asin}")

    if "product" in step_keys and keepa_factory is not None:
        with contextlib.suppress(ProviderError):
            fetch_product(
                asin, run_id=run_id, client=keepa_factory(marketplace), config=config, force=False
            )
    if "competitors" in step_keys and dfs_factory is not None:
        seed = _main_keyword(asin, marketplace)
        if seed is not None:
            with contextlib.suppress(ProviderError):
                fetch_keywords(seed, run_id=run_id, client=dfs_factory(marketplace), config=config)
            comp_asins = _serp_asins(seed, marketplace)
            if comp_asins and keepa_factory is not None:
                with contextlib.suppress(ProviderError):
                    hydrate_products(
                        comp_asins, run_id=run_id, client=keepa_factory(marketplace), config=config
                    )
    if "reviews" in step_keys:
        provider = _review_probe()
        if provider is not None:
            with contextlib.suppress(ProviderError):
                fetch_reviews(asin, run_id=run_id, provider=provider, config=config)

    with get_connection() as conn:
        repository.finish_run(conn, run_id, status="complete")
        tokens = repository.run_token_total(conn, run_id)
    ws = workspace(asin, marketplace, config)
    _write_snapshot(ws, run_id=run_id)
    return ws, tokens


def _main_keyword(asin: str, marketplace: str) -> str | None:
    with get_connection() as conn:
        phrases = repository.get_serp_keyword_phrases(conn, asin, marketplace)
    return phrases[0] if phrases else None


def _serp_asins(seed: str, marketplace: str) -> list[str]:
    with get_connection() as conn:
        return [r["asin"] for r in repository.get_serp_rankings(conn, seed, marketplace)]


# ---------------------------------------------------------------------------
# Shortlist + re-check snapshots (Phase 1)
# ---------------------------------------------------------------------------
def set_shortlist(
    asin: str, marketplace: str, *, status: str = "researching", notes: str | None = None
) -> None:
    initialize_database()
    with get_connection() as conn:
        repository.upsert_shortlist(
            conn, asin=asin.strip().upper(), marketplace=marketplace, status=status, notes=notes
        )


def remove_from_shortlist(asin: str, marketplace: str) -> None:
    with get_connection() as conn:
        repository.remove_from_shortlist(conn, asin.strip().upper(), marketplace)


def shortlist_rows(status: str | None = None) -> list[sqlite3.Row]:
    initialize_database()
    with get_connection() as conn:
        return repository.list_shortlist(conn, status=status)


def _write_snapshot(ws: Any, *, run_id: str | None = None) -> None:
    """Record a point-in-time snapshot of a workspace's key metrics."""
    m = ws.momentum
    d = ws.diagnosis
    latest_price = m.price_usd[-1] if (m and m.price_usd) else None
    with get_connection() as conn:
        repository.insert_product_snapshot(
            conn,
            asin=ws.asin,
            marketplace=ws.marketplace,
            run_id=run_id,
            price_cents=None if latest_price is None else round(latest_price * 100),
            bsr=(m.bsr[-1] if (m and m.bsr) else None),
            review_count=(m.reviews[-1] if (m and m.reviews) else None),
            monthly_sold=(m.keepa_monthly_sold if m else None),
            emergence_score=(m.emergence if m else None),
            opportunity_score=(d.score if d else None),
            verdict=(d.verdict if d else None),
            confidence=(d.confidence if d else None),
        )


def recheck(asin: str, marketplace: str, config: DeliumConfig) -> tuple[Any, int]:
    """Refetch this product's Keepa data (cache-first) and store a fresh snapshot
    so momentum can be compared over time. Returns (workspace, keepa tokens)."""
    from delium.ingestion import hydrate_products

    initialize_database()
    asin = asin.strip().upper()
    keepa_factory, _dfs = _provider_factories()
    tokens = 0
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="recheck", input_=f"{marketplace}:{asin}")
    if keepa_factory is not None:
        with contextlib.suppress(ProviderError):
            hydrate_products(
                [asin], run_id=run_id, client=keepa_factory(marketplace), config=config, force=True
            )
    with get_connection() as conn:
        repository.finish_run(conn, run_id, status="complete")
        tokens = repository.run_token_total(conn, run_id)
    ws = workspace(asin, marketplace, config)
    _write_snapshot(ws, run_id=run_id)
    return ws, tokens


# ---------------------------------------------------------------------------
# Home summary (Phase 1)
# ---------------------------------------------------------------------------
def home_summary(config: DeliumConfig, *, top_n: int = 10) -> dict[str, Any]:
    """Shortlist, recent runs, top profile-matching opportunities, Keepa tokens.
    DB-only except the free Keepa /token check (0 tokens)."""
    from delium.profile import store

    initialize_database()
    with get_connection() as conn:
        profile = store.load_active(conn)
        shortlist = repository.list_shortlist(conn)
        runs = repository.list_runs(conn, limit=10)
        validations = repository.list_validations(conn, limit=200)
    top = _top_opportunities(validations, profile, top_n)
    return {
        "profile": profile,
        "shortlist": shortlist,
        "runs": runs,
        "top_opportunities": top,
        "keepa_tokens": keepa_token_status(),
    }


def _top_opportunities(
    validations: list[sqlite3.Row], profile: Any, top_n: int
) -> list[sqlite3.Row]:
    """Best recent validations, most-recent-first within score, filtered to the
    profile's marketplaces. Sorting/highlighting only — no rule change."""
    mps = set(profile.marketplaces) if profile.marketplaces else None
    rows = [
        r
        for r in validations
        if r["opportunity_score"] is not None and (mps is None or r["marketplace"] in mps)
    ]
    rows.sort(key=lambda r: float(r["opportunity_score"]), reverse=True)
    return rows[:top_n]


# ---------------------------------------------------------------------------
# Command Center (Daily Scan, Part 3B) — top bar, background scan, live progress,
# emerging categories, and the scan inbox as product cards. All reads are DB-only
# (the free Keepa /token check aside); the scan itself runs as a detached
# background process, NOT in Streamlit's thread.
# ---------------------------------------------------------------------------
def background_scan_command(
    *, marketplaces: tuple[str, ...] = ("US",), light: bool = True, top: int = 10
) -> list[str]:
    """The exact argv for a background Daily Scan — the SAME `delium scan` CLI the
    scheduler and terminal use. `--yes` skips the interactive confirm (there is no
    TTY); caps still bind in preflight. Pure, so it is unit-testable."""
    from delium.scheduler import Scheduler

    args = [
        Scheduler.uv_path(),
        "run",
        "delium",
        "scan",
        "--yes",
        "--marketplaces",
        ",".join(marketplaces),
        "--top",
        str(top),
        "--light" if light else "--full",
    ]
    return args


def launch_background_scan(
    *, marketplaces: tuple[str, ...] = ("US",), light: bool = True, top: int = 10
) -> int:
    """Start a Daily Scan as a DETACHED background process and return its PID.
    `start_new_session=True` puts it in its own process group so closing the
    browser (or the Streamlit server) does not kill the scan; progress is read
    back from SQLite by `scan_progress()`."""
    import subprocess

    from delium.scheduler import Scheduler

    cmd = background_scan_command(marketplaces=marketplaces, light=light, top=top)
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(Scheduler.repo_dir()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def scan_progress(scan_id: str | None = None) -> dict[str, Any] | None:
    """Live progress for the latest (or a given) scan, read straight from SQLite so
    the UI can poll it while a background scan runs. Returns None if no scan yet."""
    initialize_database()
    with get_connection() as conn:
        scan = (
            repository.get_scan(conn, scan_id)
            if scan_id is not None
            else repository.latest_scan(conn)
        )
        if scan is None:
            return None
        sid, run_id = scan["id"], scan["run_id"]
        stages = repository.get_scan_stages(conn, sid)
        candidates = repository.get_scan_candidates(conn, sid)
        # Live cost from the run ledger (scans.* cost columns are only final on
        # completion; the run totals update as each stage spends).
        tokens = repository.run_token_total(conn, run_id)
        data_usd = repository.run_cost_total(conn, run_id)
    stage_idx = int(scan["stage"])
    llm_usd = sum(float(s["llm_usd"]) for s in stages)
    funnel = {
        "swept": sum(1 for c in candidates if c["stage_reached"] >= 1),
        "hydrated": sum(
            1 for c in candidates if c["stage_reached"] >= 2 and c["outcome"] != "killed"
        ),
        "killed": sum(1 for c in candidates if c["outcome"] == "killed"),
        "scored": sum(1 for c in candidates if c["cheap_score"] is not None),
        "competitor_set": sum(1 for c in candidates if c["launchability"] is not None),
        "finalists": sum(1 for c in candidates if c["outcome"] == "finalist"),
    }
    running = scan["status"] == "running"
    next_stage = (
        STAGE_NAMES[stage_idx + 1] if running and stage_idx + 1 < len(STAGE_NAMES) else None
    )
    return {
        "scan_id": sid,
        "status": scan["status"],
        "running": running,
        "stage_index": stage_idx,
        "stage_name": STAGE_NAMES[stage_idx] if 0 <= stage_idx < len(STAGE_NAMES) else "—",
        "next_stage": next_stage,
        "total_stages": len(STAGE_NAMES),
        "created_at": scan["created_at"],
        "funnel": funnel,
        "keepa_tokens": tokens,
        "data_usd": round(data_usd, 4),
        "llm_usd": round(llm_usd, 4),
        "stages": [
            {
                "name": s["name"],
                "status": s["status"],
                "in": s["input_count"],
                "out": s["output_count"],
            }
            for s in stages
        ],
    }


def _next_scheduled_run(
    times: tuple[tuple[int, int], ...], *, now: datetime | None = None
) -> datetime | None:
    """The next wall-clock datetime one of `times` (hour, minute) fires, from now.
    Pure (inject `now` in tests)."""
    if not times:
        return None
    now = now or datetime.now()
    candidates: list[datetime] = []
    for h, m in times:
        today = now.replace(hour=h, minute=m, second=0, microsecond=0)
        candidates.append(today if today > now else today + timedelta(days=1))
    return min(candidates)


def command_center() -> dict[str, Any]:
    """Top-bar summary for the Command Center: last scan (time + result), inbox
    count, Keepa tokens left, next scheduled run, active profile, and live
    progress for a running scan. DB-only aside from the free token check."""
    from delium.profile import store
    from delium.scheduler import Scheduler

    initialize_database()
    with get_connection() as conn:
        profile = store.load_active(conn)
        last = repository.latest_scan(conn)
        inbox_count = len(repository.scan_inbox(conn, last["id"])) if last is not None else 0
    times = Scheduler().installed_times()
    return {
        "profile": profile,
        "last_scan": last,
        "inbox_count": inbox_count,
        "keepa_tokens": keepa_token_status(),
        "scheduled_times": times,
        "next_scheduled_run": _next_scheduled_run(times),
        "progress": scan_progress(last["id"]) if last is not None else None,
    }


def emerging_categories(scan_id: str | None = None, *, top_n: int = 5) -> list[dict[str, Any]]:
    """Top emerging categories from the latest (or given) scan — momentum score +
    a one-line reason."""
    initialize_database()
    with get_connection() as conn:
        if scan_id is None:
            latest = repository.latest_scan(conn)
            if latest is None:
                return []
            scan_id = latest["id"]
        cats = repository.get_scan_categories(conn, scan_id)
    return [
        {"category": c["category"], "score": c["momentum_score"], "reason": c["reason"]}
        for c in cats[:top_n]
    ]


@dataclass(frozen=True)
class InboxCard:
    """One Scan Inbox product card — everything the UI renders, already resolved."""

    asin: str
    marketplace: str
    image_url: str | None
    title: str | None
    brand: str | None
    price_usd: float | None
    monthly_sales: int | None
    sales_source: str  # "amazon" | "estimated" | "unknown"
    profit_per_unit_usd: float | None
    net_margin: float | None
    sellability: float | None
    confidence: str | None
    differentiation_status: str | None
    why: str | None
    rank: int | None


def _sales_source_label(is_amazon: bool | None) -> str:
    if is_amazon is None:
        return "unknown"
    return "amazon" if is_amazon else "estimated"


def inbox_cards(
    config: DeliumConfig,
    scan_id: str | None = None,
    *,
    min_confidence: str | None = None,
    category: str | None = None,
    price_min_usd: float | None = None,
    price_max_usd: float | None = None,
    marketplace: str | None = None,
    sales_source: str | None = None,
) -> list[InboxCard]:
    """Build the Scan Inbox cards for the latest (or given) scan, applying the
    filter bar. Profit/unit + margin and the sales source are recomputed from
    persisted data via the SAME assembly the scan used — no re-fetch, no re-score
    of the verdict."""
    from delium.discovery.assembly import build_scoring_input
    from delium.profile import store

    _conf_rank = {"low": 0, "medium": 1, "high": 2}
    initialize_database()
    cards: list[InboxCard] = []
    with get_connection() as conn:
        if scan_id is None:
            latest = repository.latest_scan(conn)
            if latest is None:
                return []
            scan_id = latest["id"]
        profile = store.load_active(conn)
        finalists = repository.scan_inbox(conn, scan_id)
        for c in finalists:
            asin, mp = c["asin"], c["marketplace"]
            if marketplace is not None and mp != marketplace:
                continue
            facts = _loads_json(c["data"])
            prod = repository.get_product(conn, asin, mp)
            price_cents = facts.get("price_cents")
            weight = prod["weight_g"] if prod is not None else None
            overrides = profile.profit_overrides(price_cents=price_cents, weight_g=weight)
            profit_pu: float | None = None
            net_margin: float | None = None
            monthly_sales: int | None = facts.get("monthly_sold")
            is_amazon: bool | None = monthly_sales is not None or None
            try:
                inp, _ = build_scoring_input(
                    conn, asin, Marketplace(mp), config, profit_overrides=overrides
                )
            except Exception:  # noqa: BLE001 - a card must never crash the inbox
                inp = None
            if inp is not None and inp.profit is not None:
                exp = inp.profit.expected
                profit_pu = exp.net_profit_cents / 100.0
                net_margin = exp.net_margin
            if inp is not None and inp.demand is not None:
                est = next((e for e in inp.demand.sales_estimates if e.asin == asin), None)
                if est is not None:
                    monthly_sales = est.expected_units
                    is_amazon = est.is_amazon_source
            source = _sales_source_label(is_amazon)
            # -- filters --
            if (
                min_confidence is not None
                and c["confidence"] is not None
                and _conf_rank.get(c["confidence"], 0) < _conf_rank.get(min_confidence, 0)
            ):
                continue
            if category is not None and (facts.get("category") or "") != category:
                continue
            price_usd = None if price_cents is None else price_cents / 100.0
            if price_min_usd is not None and (price_usd is None or price_usd < price_min_usd):
                continue
            if price_max_usd is not None and (price_usd is None or price_usd > price_max_usd):
                continue
            if sales_source is not None and source != sales_source:
                continue
            cards.append(
                InboxCard(
                    asin=asin,
                    marketplace=mp,
                    image_url=prod["image_url"] if prod is not None else None,
                    title=prod["title"] if prod is not None else None,
                    brand=prod["brand"] if prod is not None else None,
                    price_usd=price_usd,
                    monthly_sales=monthly_sales,
                    sales_source=source,
                    profit_per_unit_usd=profit_pu,
                    net_margin=net_margin,
                    sellability=c["sellability"],
                    confidence=c["confidence"],
                    differentiation_status=c["differentiation_status"],
                    why=c["reason"],
                    rank=c["rank"],
                )
            )
    return cards


def shortlist_from_inbox(
    scan_id: str, asin: str, marketplace: str, *, notes: str | None = None
) -> None:
    """Inbox action: add a finalist to the shortlist and remove it from the inbox."""
    initialize_database()
    with get_connection() as conn:
        repository.upsert_shortlist(
            conn,
            asin=asin.strip().upper(),
            marketplace=marketplace,
            status="researching",
            notes=notes,
        )
        repository.set_scan_candidate_outcome(
            conn, scan_id=scan_id, asin=asin, marketplace=marketplace, outcome="shortlisted"
        )


def reject_from_inbox(
    scan_id: str, asin: str, marketplace: str, *, reason: str | None = None
) -> None:
    """Inbox action: reject a finalist (optionally with a reason) — leaves the
    inbox, never touches its score or verdict."""
    initialize_database()
    with get_connection() as conn:
        repository.set_scan_candidate_outcome(
            conn,
            scan_id=scan_id,
            asin=asin,
            marketplace=marketplace,
            outcome="rejected",
            reason=reason,
        )


def _loads_json(value: str | None) -> dict[str, Any]:
    import json

    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Zombie listings (Find → Zombies + the workspace "Zombie check" panel)
# ---------------------------------------------------------------------------
def find_zombies(
    marketplaces: tuple[str, ...],
    config: DeliumConfig,
    *,
    min_dead_months: float = 6.0,
    min_reviews: int = 50,
    min_rating: float = 4.0,
    top: int = 20,
    check_demand: bool = False,
) -> Any:
    """Run the zombie discovery (Keepa Product Finder → cache-first hydrate →
    verify). Pressing Run in the UI is the spend consent, so no extra confirm."""
    from delium.discovery import zombies as zmod

    initialize_database()
    keepa, dfs = _provider_factories()
    params = zmod.ZombieParams(
        marketplaces=marketplaces,
        min_dead_months=min_dead_months,
        min_reviews=min_reviews,
        min_rating=min_rating,
        top_n=top,
        check_demand=check_demand,
    )
    clients = zmod.ZombieClients(keepa_factory=keepa, dfs_factory=dfs)
    with get_connection() as conn:
        return zmod.run_zombies(conn, params=params, config=config, clients=clients, confirm=None)


def zombie_check(asin: str, marketplace: str, config: DeliumConfig) -> Any:
    """Verify one already-fetched listing as a zombie (cache-first, no network).
    Returns None if the product's history is not cached yet (run a Deep dive)."""
    from datetime import date as _date

    from delium.analysis.zombies import compute_zombie, load_zombie_data
    from delium.discovery.zombies import zombie_evidence_from_raw
    from delium.ingestion import cached_raw_product

    initialize_database()
    with get_connection() as conn:
        raw = cached_raw_product(conn, asin.strip().upper(), marketplace)
    if raw is None:
        return None
    ev = zombie_evidence_from_raw(
        raw, asin=asin.strip().upper(), marketplace=marketplace, as_of=_date.today()
    )
    return compute_zombie(ev, load_zombie_data(marketplace))


def zombie_timeline_series(asin: str, marketplace: str) -> list[dict[str, Any]]:
    """Step points [{date, in_stock}] from the cached NEW/offer-count history, for
    the out-of-stock timeline chart. Empty when nothing is cached."""
    from delium.ingestion import cached_raw_product
    from delium.providers.keepa import (
        _CSV_COUNT_NEW,
        _CSV_NEW,
        keepa_minutes_to_date,
        raw_csv_series,
    )

    initialize_database()
    with get_connection() as conn:
        raw = cached_raw_product(conn, asin.strip().upper(), marketplace)
    if raw is None:
        return []
    csv: Any = raw.get("csv") or []
    new = raw_csv_series(csv, _CSV_NEW)
    use_count = not new
    source = raw_csv_series(csv, _CSV_COUNT_NEW) if use_count else new
    points: list[dict[str, Any]] = []
    for km, value in sorted(source):
        in_stock = value > 0 if use_count else value >= 0
        points.append({"date": keepa_minutes_to_date(km), "in_stock": 1 if in_stock else 0})
    return points


# ---------------------------------------------------------------------------
# Calibration (Daily Scan, Part 1) — stored-data comparison against Keepa
# ---------------------------------------------------------------------------
def calibration_report(
    marketplace: str, config: DeliumConfig, asins: list[str] | None = None
) -> Any:
    """Run the calibration harness over stored data (no network)."""
    from delium.discovery import calibrate as calib

    initialize_database()
    with get_connection() as conn:
        return calib.run(conn, asins=asins or None, marketplace=marketplace)


def calibration_stored_count(marketplace: str) -> int:
    from delium.discovery import calibrate as calib

    initialize_database()
    with get_connection() as conn:
        return len(calib.all_stored_asins(conn, marketplace))
