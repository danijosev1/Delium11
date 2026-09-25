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
from typing import Any

from delium.analysis.models import Marketplace, SourceMaturity
from delium.config.models import DeliumConfig
from delium.database import initialize_database, repository
from delium.database.connection import get_connection
from delium.ingestion import (
    CrossMarketCandidate,
    discover_cross_market,
    fetch_keywords,
    fetch_product,
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
    return ValidateResult(report=report, markdown=render_report(report))


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
def discover(keywords: list[str], marketplace: str, config: DeliumConfig, *, force: bool) -> Any:
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
    return report


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
    "ProductLookup",
    "ValidateResult",
    "cross_market",
    "discover",
    "keyword_research",
    "product_lookup",
    "read_report",
    "recent_runs",
    "recent_validations",
    "render_report",
    "report_files",
    "validate",
]
