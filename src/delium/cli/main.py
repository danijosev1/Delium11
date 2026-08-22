"""Delium CLI entry point.

Five commands, matching ARCHITECTURE.md §4 — `discover`, `validate`, `pains`,
`watch`, `portfolio`. This build step wires up the command surface, argument
parsing, config loading, and logging; the actual pipelines (data fetch →
deterministic analysis → agents → report) are later build steps and are
deliberately left as stubs here.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from delium import __version__
from delium.config import ConfigError, load_config
from delium.database import initialize_database, repository
from delium.database.connection import get_connection
from delium.ingestion import CrossMarketCandidate
from delium.providers import ProviderError, build_review_provider
from delium.providers.dataforseo import DataForSeoClient
from delium.providers.keepa import KeepaClient
from delium.utils.logging import configure_logging, get_logger
from delium.utils.paths import ensure_directories, get_database_path

app = typer.Typer(
    name="delium",
    help="Private AI-powered Amazon product research system.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database administration: init, status.", no_args_is_help=True)
app.add_typer(db_app, name="db")
fetch_app = typer.Typer(help="Fetch and cache raw data from providers.", no_args_is_help=True)
app.add_typer(fetch_app, name="fetch")
console = Console()
log = get_logger(__name__)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"delium {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    log_level: Annotated[
        str, typer.Option("--log-level", help="DEBUG, INFO, WARNING, ERROR.")
    ] = "INFO",
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed version and exit.",
        ),
    ] = False,
) -> None:
    """Delium — decide what to sell before you spend a dollar sourcing it."""
    configure_logging(log_level)
    ensure_directories()
    try:
        load_config()
    except ConfigError as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


def _not_implemented(command: str) -> None:
    console.print(
        f"[yellow]`{command}` is not implemented yet.[/yellow] "
        "This is project scaffolding — see ARCHITECTURE.md for the build order."
    )
    raise typer.Exit(code=1)


@db_app.command("init")
def db_init() -> None:
    """Create the database, enable WAL + foreign keys, and apply migrations."""
    db_path = get_database_path()
    applied = initialize_database()
    console.print(f"[green]Database ready[/green] at {db_path}")
    if applied:
        console.print(f"Applied {len(applied)} migration(s): {applied}")
    else:
        console.print("Already up to date — no migrations to apply.")


@db_app.command("status")
def db_status() -> None:
    """Show the database location and which migrations have been applied."""
    from delium.database import discover_migrations, get_applied_versions
    from delium.database.connection import connect

    db_path = get_database_path()
    if not db_path.exists():
        console.print(f"[yellow]No database yet[/yellow] at {db_path} — run `delium db init`.")
        raise typer.Exit(code=1)

    all_versions = [m.version for m in discover_migrations()]
    conn = connect()
    try:
        applied = get_applied_versions(conn)
    finally:
        conn.close()

    console.print(f"Database: {db_path}")
    for version in all_versions:
        mark = "[green]applied[/green]" if version in applied else "[yellow]pending[/yellow]"
        console.print(f"  {version:04d}  {mark}")


def _price_str(cents: int | None) -> str:
    return f"${cents / 100:.2f}" if cents is not None else "—"


def _validate_marketplace(code: str) -> str:
    """Normalize and validate a marketplace code against the central registry."""
    from delium.analysis.models import Marketplace

    normalized = code.strip().upper()
    try:
        return Marketplace(normalized).value
    except ValueError as exc:
        supported = ", ".join(m.value for m in Marketplace)
        console.print(f"[bold red]Unknown marketplace {code!r}.[/bold red] Supported: {supported}.")
        raise typer.Exit(code=1) from exc


@fetch_app.command("product")
def fetch_product_cmd(
    asin: Annotated[str, typer.Argument(help="Amazon ASIN, e.g. B08XXXXXXX.")],
    marketplace: Annotated[
        str, typer.Option("--marketplace", "-m", help="Marketplace: US, CA, UK, AU, IN.")
    ] = "US",
    force: Annotated[bool, typer.Option("--force", help="Bypass the cache and refetch.")] = False,
) -> None:
    """Fetch a product from Keepa (cache-first), store it, and show a summary."""
    from delium.ingestion import fetch_product

    initialize_database()  # idempotent — ensures the schema exists
    config = load_config()
    marketplace = _validate_marketplace(marketplace)

    try:
        client = KeepaClient.from_env(marketplace=marketplace)
    except ProviderError as exc:
        console.print(f"[bold red]Provider error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="fetch.product", input_=f"{marketplace}:{asin}"
        )

    status = "complete"
    try:
        view = fetch_product(asin, run_id=run_id, client=client, config=config, force=force)
    except ProviderError as exc:
        with get_connection() as conn:
            repository.finish_run(conn, run_id, status="failed")
        console.print(f"[bold red]Fetch failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    with get_connection() as conn:
        repository.finish_run(
            conn, run_id, status=status, data_cost_usd=view.cost_usd if view else 0.0
        )

    if view is None or not view.found:
        console.print(f"[yellow]ASIN {asin} not found on Keepa.[/yellow]")
        raise typer.Exit(code=1)

    source = "cache" if view.from_cache else f"Keepa ({view.tokens_used} tokens)"
    console.print(f"[dim]marketplace: {view.marketplace}[/dim]")
    dims = (
        f"{view.dims['length_mm']}×{view.dims['width_mm']}×{view.dims['height_mm']} mm"
        if view.dims and {"length_mm", "width_mm", "height_mm"} <= view.dims.keys()
        else "—"
    )
    console.print(f"[bold green]{view.asin}[/bold green]  ({source})")
    console.print(f"  Title:      {view.title or '—'}")
    console.print(f"  Brand:      {view.brand or '—'}")
    console.print(f"  Category:   {view.category_path or '—'}")
    console.print(f"  Dimensions: {dims}")
    console.print(f"  Weight:     {f'{view.weight_g} g' if view.weight_g else '—'}")
    console.print(f"  Images:     {view.images_count if view.images_count is not None else '—'}")
    console.print(f"  Latest price: {_price_str(view.latest_price_cents)}")
    console.print(f"  Latest BSR:   {view.latest_bsr if view.latest_bsr is not None else '—'}")
    console.print(f"  History points: {view.history_points}")


@fetch_app.command("keywords")
def fetch_keywords_cmd(
    keyword: Annotated[str, typer.Argument(help="Seed keyword, e.g. 'silicone baby food tray'.")],
    marketplace: Annotated[
        str, typer.Option("--marketplace", "-m", help="Marketplace: US, CA, UK, AU, IN.")
    ] = "US",
    force: Annotated[bool, typer.Option("--force", help="Bypass the cache and refetch.")] = False,
) -> None:
    """Fetch a keyword's volume, related keywords, and Amazon SERP (cache-first)."""
    from delium.ingestion import fetch_keywords

    initialize_database()  # idempotent
    config = load_config()
    marketplace = _validate_marketplace(marketplace)

    try:
        client = DataForSeoClient.from_env(marketplace=marketplace)
    except ProviderError as exc:
        console.print(f"[bold red]Provider error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="fetch.keywords", input_=f"{marketplace}:{keyword}"
        )

    try:
        result = fetch_keywords(keyword, run_id=run_id, client=client, config=config, force=force)
    except ProviderError as exc:
        with get_connection() as conn:
            repository.finish_run(conn, run_id, status="failed")
        console.print(f"[bold red]Fetch failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    with get_connection() as conn:
        repository.finish_run(conn, run_id, status="complete", data_cost_usd=result.cost_usd)

    source = "cache" if result.from_cache else f"DataForSEO (${result.cost_usd:.4f})"
    volume = f"{result.seed_volume:,}" if result.seed_volume is not None else "—"
    console.print(f"[bold green]{result.seed}[/bold green]  ({source})")
    console.print(f"  Search volume: {volume}")
    console.print("  Competition:   n/a (Amazon volume endpoint returns volume only)")

    console.print("  Top related keywords:")
    ranked = sorted(result.related, key=lambda k: (k.volume is None, -(k.volume or 0)))
    for kw in ranked[:10]:
        vol = f"{kw.volume:,}" if kw.volume is not None else "—"
        console.print(f"    {kw.phrase}  ({vol})")
    if not ranked:
        console.print("    —")

    console.print("  Top Amazon SERP ASINs:")
    for item in sorted(result.serp, key=lambda s: s.position)[:10]:
        tag = " [dim](sponsored)[/dim]" if item.sponsored else ""
        console.print(f"    #{item.position:<3} {item.asin}{tag}")
    if not result.serp:
        console.print("    —")


@fetch_app.command("reviews")
def fetch_reviews_cmd(
    asin: Annotated[str, typer.Argument(help="Amazon ASIN, e.g. B08XXXXXXX.")],
    force: Annotated[bool, typer.Option("--force", help="Bypass the cache and refetch.")] = False,
) -> None:
    """Fetch a review sample for an ASIN (Unwrangle → Apify), store, and summarize."""
    from delium.ingestion import fetch_reviews

    initialize_database()  # idempotent
    config = load_config()

    try:
        provider = build_review_provider()
    except ProviderError as exc:
        console.print(f"[bold red]Provider error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="fetch.reviews", input_=asin)

    try:
        result = fetch_reviews(asin, run_id=run_id, provider=provider, config=config, force=force)
    except ProviderError as exc:
        with get_connection() as conn:
            repository.finish_run(conn, run_id, status="failed")
        console.print(f"[bold red]Fetch failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    with get_connection() as conn:
        repository.finish_run(conn, run_id, status="complete", data_cost_usd=result.cost_usd)

    reviews = result.reviews
    source = "cache" if result.from_cache else f"{result.provider} (${result.cost_usd:.4f})"
    console.print(f"[bold green]{result.asin}[/bold green]  ({source})")

    if not reviews:
        console.print("  [yellow]No reviews retrieved.[/yellow]")
        return

    total = len(reviews)
    avg = sum(r.stars for r in reviews) / total
    console.print(f"  Total reviews: {total}")
    console.print(f"  Average rating: {avg:.2f}")
    console.print("  Rating distribution:")
    for star in range(5, 0, -1):
        count = sum(1 for r in reviews if r.stars == star)
        bar = "█" * count
        console.print(f"    {star}★ {count:>4}  {bar}")
    dates = [r.review_date for r in reviews if r.review_date]
    console.print(f"  Newest review: {max(dates) if dates else '—'}")


@app.command()
def discover(
    seed: Annotated[str, typer.Argument(help="Seed niche or keyword to expand from.")],
) -> None:
    """Scan a niche/keyword for candidate products worth validating."""
    log.info("discover requested: seed=%r", seed)
    _not_implemented("discover")


@app.command()
def validate(
    target: Annotated[str, typer.Argument(help="Amazon URL, ASIN, or keyword.")],
    cogs: Annotated[
        float | None, typer.Option("--cogs", help="Override assumed unit COGS in USD.")
    ] = None,
    freight: Annotated[
        float | None, typer.Option("--freight", help="Override assumed freight/unit in USD.")
    ] = None,
) -> None:
    """Run the full product validation mission on a target ASIN/URL/keyword."""
    log.info("validate requested: target=%r cogs=%r freight=%r", target, cogs, freight)
    _not_implemented("validate")


@app.command()
def pains(
    asin: Annotated[str, typer.Argument(help="Target ASIN to mine reviews for.")],
) -> None:
    """Deep-dive customer pain mining for a single ASIN and its competitors."""
    log.info("pains requested: asin=%r", asin)
    _not_implemented("pains")


@app.command()
def watch() -> None:
    """Refresh the watchlist: re-pull tracked ASINs/niches and flag deltas."""
    log.info("watch requested")
    _not_implemented("watch")


def _refresh_discovery_data(
    source_mp: str, target_mps: list[str], config: object, asins: list[str], run_id: str
) -> None:
    """Best-effort provider refresh for --force: refetch each candidate's source
    product and re-pull its linked seed keyword cluster in the source and every
    target marketplace. Requires provider credentials; degrades to a warning if
    they are absent. This is where the marketplace flows CLI → provider → cache."""
    from delium.ingestion import fetch_keywords, fetch_product

    try:
        keepa = KeepaClient.from_env(marketplace=source_mp)
        dfs_source = DataForSeoClient.from_env(marketplace=source_mp)
        dfs_targets = {mp: DataForSeoClient.from_env(marketplace=mp) for mp in target_mps}
    except ProviderError as exc:
        console.print(f"[yellow]--force refresh skipped:[/yellow] {exc}")
        return

    for asin in asins:
        try:
            fetch_product(asin, run_id=run_id, client=keepa, config=config, force=True)  # type: ignore[arg-type]
            with get_connection() as conn:
                seeds = repository.get_serp_keyword_phrases(conn, asin, source_mp)
            for seed in seeds[:1]:  # primary cluster only
                fetch_keywords(seed, run_id=run_id, client=dfs_source, config=config, force=True)  # type: ignore[arg-type]
                for client in dfs_targets.values():
                    fetch_keywords(seed, run_id=run_id, client=client, config=config, force=True)  # type: ignore[arg-type]
        except ProviderError as exc:
            console.print(f"[yellow]refresh failed for {asin}:[/yellow] {exc}")


@app.command("cross-market")
def cross_market_cmd(
    source: Annotated[str, typer.Argument(help="Source marketplace, e.g. US.")],
    target: Annotated[
        str | None,
        typer.Argument(help="Single target marketplace, e.g. AU. Omit if using --targets."),
    ] = None,
    targets: Annotated[
        str | None,
        typer.Option("--targets", help="Comma-separated targets, e.g. US,UK,CA,AU,IN."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max source candidates to evaluate.")] = 25,
    min_source_demand: Annotated[
        int | None,
        typer.Option("--min-source-demand", help="Min source est. monthly units to qualify."),
    ] = None,
    min_source_maturity: Annotated[
        str,
        typer.Option(
            "--min-source-maturity",
            help="Min source maturity: emerging, validated, strong, exceptional.",
        ),
    ] = "emerging",
    force: Annotated[
        bool, typer.Option("--force", help="Refresh provider data before discovery.")
    ] = False,
) -> None:
    """Discover products proven in SOURCE that look underpenetrated in a target.

    Reads already-fetched, marketplace-scoped data (fetch first with
    `fetch product -m` / `fetch keywords -m`). This is a DISCOVERY SIGNAL — what
    to VALIDATE next — never a Buy/Test/Avoid verdict (scoring owns that).
    """
    from delium.analysis.models import Marketplace, SourceMaturity
    from delium.ingestion import discover_cross_market

    initialize_database()
    config = load_config()

    source_mp = _validate_marketplace(source)
    if target and targets:
        console.print("[bold red]Pass either a TARGET argument or --targets, not both.[/bold red]")
        raise typer.Exit(code=1)
    raw_targets = targets.split(",") if targets else ([target] if target else [])
    if not raw_targets:
        console.print("[bold red]Specify a target marketplace (TARGET or --targets).[/bold red]")
        raise typer.Exit(code=1)
    target_mps = [_validate_marketplace(t) for t in raw_targets if t.strip()]
    target_mps = [mp for mp in target_mps if mp != source_mp]
    if not target_mps:
        console.print("[bold red]No target marketplace differs from the source.[/bold red]")
        raise typer.Exit(code=1)

    try:
        maturity = SourceMaturity(min_source_maturity.strip().lower())
    except ValueError as exc:
        console.print(f"[bold red]Unknown maturity {min_source_maturity!r}.[/bold red]")
        raise typer.Exit(code=1) from exc

    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="cross-market", input_=f"{source_mp}->{','.join(target_mps)}"
        )

    if force:
        with get_connection() as conn:
            candidate_asins = repository.get_products_by_marketplace(conn, source_mp, limit=limit)
        _refresh_discovery_data(
            source_mp, target_mps, config, [r["asin"] for r in candidate_asins], run_id
        )

    with get_connection() as conn:
        candidates = discover_cross_market(
            conn,
            source_mp=Marketplace(source_mp),
            target_mps=tuple(Marketplace(mp) for mp in target_mps),
            config=config,
            run_id=run_id,
            limit=limit,
            min_source_maturity=maturity,
            min_monthly_units=min_source_demand,
        )
        repository.finish_run(conn, run_id, status="complete")

    _render_cross_market(source_mp, target_mps, candidates)


def _render_cross_market(
    source_mp: str, target_mps: list[str], typed: list[CrossMarketCandidate]
) -> None:
    console.print(
        "[bold]Cross-market discovery[/bold] — a signal for what to "
        "[bold]validate[/bold] next, not a Buy/Test/Avoid verdict."
    )
    console.print(f"Source: [cyan]{source_mp}[/cyan]  →  Targets: {', '.join(target_mps)}")

    if not typed:
        console.print(
            "\n[yellow]No qualifying candidates.[/yellow] Fetch source products/keywords "
            "first (`fetch product -m`, `fetch keywords -m`) or relax "
            "--min-source-maturity / --min-source-demand."
        )
        return

    # Best opportunities first, then by score.
    order = {
        "strong_opportunity": 0,
        "opportunity_to_validate": 1,
        "mature_market": 2,
        "weak_transfer": 3,
        "insufficient_data": 4,
    }
    typed.sort(key=lambda c: (order.get(c.report.verdict.value, 9), -c.report.score))

    for c in typed:
        r = c.report
        se, te = r.source_evidence, r.target_evidence
        title = (r.match.source.title or c.source_asin)[:60]
        console.print(
            f"\n[bold green]{c.source_asin}[/bold green]  {title}"
            f"\n  {c.source_marketplace.value} → {c.target_marketplace.value}"
            f"   [bold]{r.verdict.value.replace('_', ' ').upper()}[/bold]"
            f"  (cross-market score {r.score:.0f}/100, {r.confidence.level.value} confidence)"
        )
        console.print(
            f"  match: {r.match.confidence.value}"
            f"  · source: {se.maturity.value} ({se.source_success_score:.0f})"
            f"  · target presence: {te.presence.value}"
        )
        demand = f"{te.target_demand_score:.0f}/100" + ("" if te.demand_credible else " (unproven)")
        console.print(
            f"  target demand: {demand}"
            f"  · competition gap: {r.market_gap.competition_gap:.0f}"
            f"  · transferability: {r.transferability.level.value}"
        )
        for line in r.summary[1:4]:
            console.print(f"    - {line}")


@app.command()
def portfolio() -> None:
    """Show all validated candidates ranked by score, capital, and payback."""
    log.info("portfolio requested")
    _not_implemented("portfolio")


if __name__ == "__main__":
    app()
