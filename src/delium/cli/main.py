"""Delium CLI entry point.

Five commands, matching ARCHITECTURE.md §4 — `discover`, `validate`, `pains`,
`watch`, `portfolio`. This build step wires up the command surface, argument
parsing, config loading, and logging; the actual pipelines (data fetch →
deterministic analysis → agents → report) are later build steps and are
deliberately left as stubs here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

from delium import __version__
from delium.config import ConfigError, load_config
from delium.database import initialize_database, repository
from delium.database.connection import get_connection
from delium.ingestion import CrossMarketCandidate
from delium.providers import ProviderError, ReviewProviderChain, build_review_provider
from delium.providers.dataforseo import DataForSeoClient
from delium.providers.keepa import KeepaClient
from delium.utils.logging import configure_logging, get_logger
from delium.utils.paths import ensure_directories, get_database_path

if TYPE_CHECKING:
    from delium.discovery.daily_scan import ScanClients
    from delium.reports.cards import CardFacts

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


def _build_provider_factories() -> tuple[
    Callable[[str], object] | None, Callable[[str], object] | None
]:
    """Marketplace-scoped provider factories, or None each if credentials are
    absent — discovery then runs on already-cached data only."""
    keepa: Callable[[str], object] | None
    dfs: Callable[[str], object] | None
    try:
        KeepaClient.from_env()  # probe credentials
        keepa = lambda mp: KeepaClient.from_env(marketplace=mp)  # noqa: E731
    except ProviderError:
        keepa = None
    try:
        DataForSeoClient.from_env()  # probe credentials
        dfs = lambda mp: DataForSeoClient.from_env(marketplace=mp)  # noqa: E731
    except ProviderError:
        dfs = None
    return keepa, dfs


@app.command()
def discover(
    seed: Annotated[
        str | None,
        typer.Argument(help="Seed keyword (shorthand for --keyword). Optional."),
    ] = None,
    keyword: Annotated[
        list[str] | None,
        typer.Option("--keyword", "-k", help="Seed keyword (repeatable)."),
    ] = None,
    asin: Annotated[
        list[str] | None,
        typer.Option("--asin", help="Explicit ASIN to evaluate (repeatable)."),
    ] = None,
    marketplace: Annotated[
        str, typer.Option("--marketplace", "-m", help="Marketplace: US, CA, UK, AU, IN.")
    ] = "US",
    source_marketplace: Annotated[
        str | None,
        typer.Option(
            "--source-marketplace", help="Cross-market source (defaults to --marketplace)."
        ),
    ] = None,
    target_marketplace: Annotated[
        list[str] | None,
        typer.Option("--target-marketplace", help="Cross-market target(s), repeatable."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Max ranked candidates to show.")
    ] = None,
) -> None:
    """Discover candidate products worth validating (cheap & wide triage).

    Modes (combinable): keyword/SERP, explicit ASIN, and cross-market. Output is
    a deterministic triage ranking — a research queue, NOT a Buy recommendation.
    """
    from delium.analysis.models import Marketplace
    from delium.discovery import run_discovery

    initialize_database()
    config = load_config()

    # --source-marketplace, when given, is the marketplace we discover FROM.
    mp_code = _validate_marketplace(source_marketplace or marketplace)
    marketplace_enum = Marketplace(mp_code)
    keywords = [*(keyword or [])]
    if seed:
        keywords.append(seed)
    asins = [*(asin or [])]
    targets = tuple(Marketplace(_validate_marketplace(t)) for t in (target_marketplace or []))

    if not keywords and not asins and not targets:
        console.print(
            "[bold red]Nothing to discover.[/bold red] Provide a seed keyword, "
            "--keyword, --asin, or --target-marketplace."
        )
        raise typer.Exit(code=1)

    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="discover", input_=f"{mp_code}:{','.join(keywords) or asins or targets}"
        )

    keepa_factory, dfs_factory = _build_provider_factories()

    # Keyword expansion: fetch each seed's SERP up front so candidates exist,
    # cache-first, in the requested marketplace (CLI → ingestion → provider).
    if keywords and dfs_factory is not None:
        from delium.ingestion import fetch_keywords

        for kw in keywords:
            try:
                fetch_keywords(
                    kw,
                    run_id=run_id,
                    client=dfs_factory(mp_code),  # type: ignore[arg-type]
                    config=config,
                )
            except ProviderError as exc:
                console.print(f"[yellow]keyword fetch failed for {kw!r}:[/yellow] {exc}")

    with get_connection() as conn:
        report = run_discovery(
            conn,
            marketplace=marketplace_enum,
            config=config,
            run_id=run_id,
            keywords=keywords,
            asins=asins,
            cross_market_targets=targets,
            keepa_factory=keepa_factory,
            dfs_factory=dfs_factory,
        )
        repository.finish_run(conn, run_id, status="complete")
        tokens = repository.run_token_total(conn, run_id)

    _render_discovery(report, limit or config.discovery.max_ranked)
    console.print(f"\n[dim]Keepa tokens used this run: {tokens}[/dim]")


def _render_discovery(report: object, limit: int) -> None:
    from delium.analysis.models import Verdict
    from delium.discovery.models import DiscoveryReport

    assert isinstance(report, DiscoveryReport)
    console.print(
        "[bold]Discovery[/bold] — a deterministic triage ranking (research queue), "
        "[bold]not[/bold] a Buy/Test/Avoid recommendation."
    )
    console.print(
        f"Marketplace: [cyan]{report.marketplace.value}[/cyan]  ·  "
        f"discovered {report.discovered_count}  ·  "
        f"ranked {len(report.ranked)}  ·  killed {len(report.killed)}  ·  "
        f"unresolved {len(report.unresolved)}"
    )

    if report.ranked:
        console.print("\n[bold]Ranked candidates[/bold] (opportunity score DESC):")
        for i, ec in enumerate(report.ranked[:limit], start=1):
            s = ec.scored
            assert s is not None
            flag = " [yellow](needs more data)[/yellow]" if s.insufficient_data else ""
            sources = ",".join(src.value for src in ec.candidate.sources)
            console.print(
                f"  {i:>2}. [green]{ec.asin}[/green] [{ec.marketplace.value}]  "
                f"score {s.score:.0f}  ·  {s.verdict.value.upper()}  ·  "
                f"{s.confidence.level.value} conf  ·  via {sources}{flag}"
            )
    else:
        console.print("\n[yellow]No candidates survived to scoring.[/yellow]")

    if report.killed:
        console.print("\n[bold]Eliminated by hard kills[/bold]:")
        for ec in report.killed:
            reason = ec.notes[0] if ec.notes else ""
            console.print(
                f"  [red]{ec.asin}[/red] [{ec.marketplace.value}]  {ec.kill_rule}: {reason}"
            )

    needs_data = [
        ec for ec in report.ranked if ec.scored is not None and ec.scored.insufficient_data
    ]
    if report.unresolved or needs_data:
        console.print("\n[bold]Needs more data[/bold] (revisit at validate tier):")
        for ec in report.unresolved:
            console.print(f"  [yellow]{ec.asin}[/yellow] [{ec.marketplace.value}]  not fetched")
        for ec in needs_data:
            console.print(
                f"  [yellow]{ec.asin}[/yellow] [{ec.marketplace.value}]  "
                f"partial: {', '.join(ec.scored.confidence.partial_pillars)}"  # type: ignore[union-attr]
            )

    if any(
        ec.scored is not None and ec.scored.verdict is not Verdict.AVOID for ec in report.ranked
    ):
        pass  # discovery never emits BUY; scoring caps at TEST without validate-tier data


def _build_review_provider_probe() -> ReviewProviderChain | None:
    """A review provider chain, or None if no review credentials are configured —
    validation then uses only already-cached reviews."""
    try:
        return build_review_provider()
    except ProviderError:
        return None


def _parse_dims(raw: str | None) -> tuple[int, int, int] | None:
    """Parse a `--dims` override like '160x120x30' (millimetres, L×W×H)."""
    if raw is None:
        return None
    parts = [p for p in raw.replace(",", "x").lower().split("x") if p.strip()]
    if len(parts) != 3:
        console.print("[bold red]--dims must be L×W×H in mm, e.g. 160x120x30.[/bold red]")
        raise typer.Exit(code=1)
    try:
        length, width, height = (int(float(p)) for p in parts)
    except ValueError as exc:
        console.print("[bold red]--dims values must be numbers (mm).[/bold red]")
        raise typer.Exit(code=1) from exc
    return length, width, height


@app.command()
def validate(
    target: Annotated[str, typer.Argument(help="Amazon URL, ASIN, or keyword.")],
    cogs: Annotated[
        float | None, typer.Option("--cogs", help="Override assumed unit COGS in USD.")
    ] = None,
    freight: Annotated[
        float | None, typer.Option("--freight", help="Override assumed freight/unit in USD.")
    ] = None,
    marketplace: Annotated[
        str, typer.Option("--marketplace", "-m", help="Marketplace: US, CA, UK, AU, IN.")
    ] = "US",
    dims: Annotated[
        str | None,
        typer.Option("--dims", help="Override dims (mm, L×W×H), e.g. 160x120x30 — unblocks fees."),
    ] = None,
    weight: Annotated[
        int | None, typer.Option("--weight", help="Override unit weight in grams (unblocks fees).")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Bypass caches and refetch (per ingestion policy).")
    ] = False,
) -> None:
    """Run the full validation on a target ASIN/URL/keyword.

    Fetches cache-first (Keepa + DataForSEO + reviews), runs the LLM Review Miner
    and Strategist when credentials are present (degrading cleanly to the
    deterministic result otherwise), and lets scoring.py issue the final
    Buy/Test/Avoid verdict — the Strategist's concurrence is only the G5 gate and
    can never manufacture a Buy.
    """
    from delium.agents import build_llm_client
    from delium.analysis.models import Marketplace
    from delium.validation import Clients, ValidationRequest, ValidationStatus, run_validation

    initialize_database()
    config = load_config()
    mp_code = _validate_marketplace(marketplace)
    dims_mm = _parse_dims(dims)

    keepa_factory, dfs_factory = _build_provider_factories()
    clients = Clients(
        keepa=keepa_factory,
        dfs=dfs_factory,
        reviews=_build_review_provider_probe(),
        llm=build_llm_client(config.agents),
    )

    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate", input_=f"{mp_code}:{target}")

    request = ValidationRequest(
        target=target,
        marketplace=Marketplace(mp_code),
        run_id=run_id,
        force=force,
        cogs_usd=cogs,
        freight_usd=freight,
        dims_mm=dims_mm,
        weight_g=weight,
    )

    with get_connection() as conn:
        report = run_validation(conn, request, config, clients)
        terminal = report.status in (ValidationStatus.SCORED, ValidationStatus.HARD_KILLED)
        if terminal:
            run_status = "degraded" if report.hydration.degraded else "complete"
        else:
            run_status = "failed"
        repository.finish_run(
            conn,
            run_id,
            status=run_status,
            data_cost_usd=report.data_cost_usd,
            llm_cost_usd=report.llm_cost_usd,
        )
        tokens = repository.run_token_total(conn, run_id)

    _render_validation(report)
    console.print(f"\n[dim]Keepa tokens used this run: {tokens}[/dim]")
    if not terminal:
        raise typer.Exit(code=1)


def _render_validation(report: object) -> None:
    """Render the validation report (deterministic numbers + advisory Strategist
    narrative, clearly separated) and save a Markdown copy. Model/review text is
    printed literally (markup disabled) so it can never inject terminal markup."""
    from datetime import date

    from delium.reports.render import quote_ids, render_validation
    from delium.utils.paths import get_reports_dir
    from delium.validation import ValidationReport

    assert isinstance(report, ValidationReport)
    title = _product_title(report.asin, report.marketplace.value) if report.asin else None
    quotes = _review_quotes(report.asin, quote_ids(report)) if report.asin else {}

    text = render_validation(report, product_title=title, quotes=quotes)
    console.print(text, markup=False)

    if report.asin is not None:
        reports_dir = get_reports_dir()
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / f"validate-{report.asin}-{date.today().isoformat()}.md"
        path.write_text(text, encoding="utf-8")
        console.print(f"\n[dim]Report written to {path}[/dim]")


def _product_title(asin: str, marketplace: str) -> str | None:
    with get_connection() as conn:
        row = repository.get_product(conn, asin, marketplace)
    return row["title"] if row is not None else None


def _review_quotes(asin: str | None, ids: list[str]) -> dict[str, tuple[int, str]]:
    """Fetch (stars, text) for the wanted review ids from the DB — quotes shown in
    the report come from stored reviews by id, never from model output."""
    if not asin or not ids:
        return {}
    wanted = set(ids)
    with get_connection() as conn:
        rows = repository.get_reviews_for_asin(conn, asin)
    out: dict[str, tuple[int, str]] = {}
    for row in rows:
        rid = row["review_id"]
        if rid in wanted:
            out[rid] = (int(row["stars"]), row["body"] or row["title"] or "")
    return out


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
    from delium.ingestion import fetch_keywords, hydrate_products

    try:
        keepa = KeepaClient.from_env(marketplace=source_mp)
        dfs_source = DataForSeoClient.from_env(marketplace=source_mp)
        dfs_targets = {mp: DataForSeoClient.from_env(marketplace=mp) for mp in target_mps}
    except ProviderError as exc:
        console.print(f"[yellow]--force refresh skipped:[/yellow] {exc}")
        return

    # One batched Keepa call for all source candidates (≤100 ASINs/call).
    try:
        hydrate_products(asins, run_id=run_id, client=keepa, config=config, force=True)  # type: ignore[arg-type]
    except ProviderError as exc:
        console.print(f"[yellow]source product refresh failed:[/yellow] {exc}")
    for asin in asins:
        try:
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
def emerging(
    category: Annotated[
        list[int] | None,
        typer.Option("--category", "-c", help="Keepa root category id (repeatable)."),
    ] = None,
    marketplace: Annotated[
        str, typer.Option("--marketplace", "-m", help="Marketplace: US, CA, UK, AU, IN.")
    ] = "US",
    max_reviews: Annotated[
        int | None, typer.Option("--max-reviews", help="Override the review-count ceiling.")
    ] = None,
    max_age_days: Annotated[
        int | None, typer.Option("--max-age-days", help="Override the 'recently listed' window.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the cost confirmation prompt.")
    ] = False,
) -> None:
    """Find recently-launched products already gaining traction (Keepa Product
    Finder), score the top ones through the EXISTING pipeline, and show
    emerging-but-killed products separately with their kill reasons."""
    from delium.analysis.models import Marketplace
    from delium.discovery.emerging import estimate_tokens, run_emerging

    initialize_database()
    config = load_config()
    mp_code = _validate_marketplace(marketplace)
    keepa_factory, dfs_factory = _build_provider_factories()
    if keepa_factory is None:
        console.print(
            "[bold red]Emerging search needs Keepa.[/bold red] Set DELIUM_KEEPA_API_KEY "
            "(the Product Finder is Keepa-only)."
        )
        raise typer.Exit(code=1)

    overrides: dict[str, int] = {}
    if max_reviews is not None:
        overrides["reviews_max"] = max_reviews
    if max_age_days is not None:
        overrides["age_max_days"] = max_age_days

    est = estimate_tokens(config)
    console.print(
        f"[bold]Emerging search[/bold] — {mp_code}, "
        f"categories {category or 'all'}, page size {config.emerging.page_size}."
    )
    console.print(
        f"[yellow]Estimated Keepa tokens:[/yellow] ~{est.finder_tokens} (Product Finder) + up to "
        f"{est.product_tokens_worst_case} (product hydration, worst case; cache reused)."
    )
    if dfs_factory is not None and config.emerging.enrich_top_n > 0:
        console.print(
            f"[dim]Top {config.emerging.enrich_top_n} may also make DataForSEO keyword calls.[/dim]"
        )
    if not yes and not typer.confirm("Proceed and spend Keepa tokens?"):
        console.print("Aborted — no API calls made.")
        raise typer.Exit(code=0)

    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="emerging", input_=f"{mp_code}:{category or ''}"
        )
    with get_connection() as conn:
        report = run_emerging(
            conn,
            marketplace=Marketplace(mp_code),
            category_ids=category or [],
            config=config,
            run_id=run_id,
            keepa_factory=keepa_factory,
            dfs_factory=dfs_factory,
            overrides=overrides,
        )
        repository.finish_run(conn, run_id, status="complete")
    _render_emerging(report)


def _render_emerging(report: object) -> None:
    from delium.discovery.emerging import EmergingReport

    assert isinstance(report, EmergingReport)
    for note in report.notes:
        console.print(f"[yellow]{note}[/yellow]")
    console.print(
        f"\nProduct Finder matched [cyan]{report.finder_total_results or 0}[/cyan] total  ·  "
        f"tokens used: {report.finder_tokens} finder + {report.product_tokens} product."
    )

    if report.ranked:
        console.print("\n[bold]Emerging candidates[/bold] (emergence signal DESC):")
        for c in report.ranked:
            s = c.evaluated.scored
            assert s is not None
            em = (
                "—" if c.emergence.emergence_score is None else f"{c.emergence.emergence_score:.0f}"
            )
            age = "—" if c.emergence.age_days is None else f"{c.emergence.age_days}d"
            flag = (
                "  [magenta](established brand — likely ad-driven)[/magenta]"
                if (c.established_brand)
                else ""
            )
            console.print(
                f"  [green]{c.asin}[/green]  emergence {em}/100 ({age})  ·  "
                f"opportunity {s.score:.0f} · [bold]{s.verdict.value.upper()}[/bold] · "
                f"{s.confidence.level.value} conf{flag}"
            )
            console.print(f"    [dim]{', '.join(c.emergence.reasons)}[/dim]")
            console.print(
                "    [dim]run `delium diagnose` for the per-pillar breakdown and next action.[/dim]"
            )
    else:
        console.print("\n[yellow]No emerging candidates survived scoring.[/yellow]")

    if report.killed:
        console.print("\n[bold]Emerging but hard-killed[/bold] (excluded — reason shown):")
        for c in report.killed:
            em = (
                "—" if c.emergence.emergence_score is None else f"{c.emergence.emergence_score:.0f}"
            )
            reason = c.evaluated.notes[0] if c.evaluated.notes else ""
            console.print(
                f"  [red]{c.asin}[/red]  emergence {em}/100  ·  {c.evaluated.kill_rule}: {reason}"
            )


def _diag_facts(conn: object, asin: str, marketplace: str, emerging_row: object) -> CardFacts:
    """Assemble CardFacts for one product from persisted data (read-only)."""
    import sqlite3

    from delium.reports.cards import CardFacts

    assert isinstance(conn, sqlite3.Connection)
    row = repository.get_product(conn, asin, marketplace)
    history = repository.get_price_bsr_history(conn, asin)

    def _latest(col: str) -> int | None:
        for r in reversed(history):
            if r[col] is not None:
                return int(r[col])
        return None

    emergence = age = None
    if isinstance(emerging_row, sqlite3.Row):
        emergence = emerging_row["emergence_score"]
        age = emerging_row["age_days"]
    return CardFacts(
        title=row["title"] if row is not None else None,
        brand=row["brand"] if row is not None else None,
        category=row["category_path"] if row is not None else None,
        price_cents=_latest("price_cents"),
        bsr=_latest("bsr"),
        reviews=_latest("review_count"),
        age_days=age,
        emergence=emergence,
    )


@app.command()
def diagnose(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Emerging run id to diagnose (default: the latest)."),
    ] = None,
    asin: Annotated[
        str | None,
        typer.Option("--asin", help="Diagnose a single stored ASIN instead of a run."),
    ] = None,
    marketplace: Annotated[
        str, typer.Option("--marketplace", "-m", help="Marketplace for --asin.")
    ] = "US",
) -> None:
    """Explain WHY stored candidates score as they do — per-pillar scores,
    confidence, the input that drove each pillar, and which evidence fix would
    raise confidence. Read-only: rebuilds scoring from the local DB, no API calls.
    """
    from delium.analysis.models import Marketplace
    from delium.discovery import diagnostics
    from delium.reports.cards import build_card

    initialize_database()
    config = load_config()

    with get_connection() as conn:
        if asin is not None:
            mp = Marketplace(_validate_marketplace(marketplace))
            diag = diagnostics.diagnose_candidate(conn, asin.strip().upper(), mp, config)
            diags = [diag] if diag is not None else []
            facts_by_asin = {}
            if diag is not None:
                facts_by_asin[diag.asin] = _diag_facts(conn, diag.asin, mp.value, None)
        else:
            rid = run_id
            if rid is None:
                runs = repository.list_emerging_runs(conn, limit=1)
                if not runs:
                    console.print("[yellow]No emerging runs found in the database.[/yellow]")
                    raise typer.Exit(code=0)
                rid = runs[0]["run_id"]
            diags = diagnostics.diagnose_run(conn, rid, config)
            em_rows = {r["asin"]: r for r in repository.get_emerging_candidates(conn, rid)}
            facts_by_asin = {
                d.asin: _diag_facts(conn, d.asin, d.marketplace, em_rows.get(d.asin)) for d in diags
            }

    if not diags:
        console.print("[yellow]Nothing to diagnose (no stored candidates matched).[/yellow]")
        raise typer.Exit(code=0)

    for d in diags:
        console.print(
            f"\n[bold green]{d.asin}[/bold green] [{d.marketplace}]  "
            f"opportunity {d.score:.0f} · [bold]{d.verdict.upper()}[/bold] · "
            f"{d.confidence} confidence"
        )
        console.print("  [bold]Pillars[/bold] (missing = unknown, excluded from the score):")
        for p in d.pillars:
            if not p.available:
                state = "[magenta]ABSENT (unknown — not scored as 0)[/magenta]"
            elif p.partial:
                state = f"[yellow]partial: {p.cap_reason}[/yellow]"
            else:
                state = "ok"
            capped = "—" if p.capped is None else f"{p.capped:.0f}"
            console.print(
                f"    {p.pillar:<15} score {capped:>4}/100  w{p.weight:.2f}  "
                f"contrib {p.contribution:5.1f}  {p.confidence:<6} {state}"
            )
            console.print(f"      [dim]{p.driver}[/dim]")
        card = build_card(d, facts_by_asin.get(d.asin))
        console.print("  [bold]Plain English[/bold]:")
        for line in card.lines():
            console.print(f"    {line}", markup=False)

    if len(diags) > 1:
        console.print("\n[bold]Confidence-cause summary[/bold] (which fix would lift how many):")
        for impact in diagnostics.summarize_fixes(diags):
            console.print(
                f"  {impact.label}: blocks {impact.blocks_count}, "
                f"sole blocker for {impact.sole_blocker_count} "
                f"(would reach ≥MEDIUM on this fix alone)"
            )


@app.command()
def calibrate(
    asin: Annotated[
        list[str] | None,
        typer.Option("--asin", help="ASIN to calibrate (repeatable). Omit to use the whole DB."),
    ] = None,
    file: Annotated[
        str | None,
        typer.Option("--file", help="Path to a newline-separated list of ASINs."),
    ] = None,
    marketplace: Annotated[
        str, typer.Option("--marketplace", "-m", help="Marketplace: US, CA, UK, AU, IN.")
    ] = "US",
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Re-fetch Keepa first (PAID — confirms the token cost)."),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the refresh confirmation prompt.")
    ] = False,
) -> None:
    """Compare Delium's estimates against Keepa's ground truth (stored data).

    Per product and per category: Delium's BSR-curve monthly-unit estimate vs
    Keepa `monthlySold` (bucketed — compared against the bucket RANGE), and
    Delium's fee-table FBA fee vs Keepa `fbaFees.pickAndPackFee`. Reports MAPE and
    SUGGESTED (never applied) per-category curve/fee adjustments. Stored data only
    unless --refresh.
    """
    from pathlib import Path

    from delium.discovery import calibrate as calib

    initialize_database()
    config = load_config()
    mp_code = _validate_marketplace(marketplace)

    asins = [*(asin or [])]
    if file:
        asins.extend(
            line.strip()
            for line in Path(file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    if refresh:
        keepa_factory, _dfs = _build_provider_factories()
        if keepa_factory is None:
            console.print("[bold red]--refresh needs Keepa.[/bold red] Set DELIUM_KEEPA_API_KEY.")
            raise typer.Exit(code=1)
        with get_connection() as conn:
            targets = asins or calib.all_stored_asins(conn, mp_code)
        if not targets:
            console.print("[yellow]Nothing to refresh — no ASINs given and DB is empty.[/yellow]")
            raise typer.Exit(code=0)
        est_tokens = len(targets) * 2  # ~2 Keepa tokens per product (1 base + 1 rating)
        console.print(
            f"[yellow]Refresh will re-fetch {len(targets)} product(s) — "
            f"~{est_tokens} Keepa tokens (batched, cache bypassed).[/yellow]"
        )
        if not yes and not typer.confirm("Proceed and spend Keepa tokens?"):
            console.print("Aborted — no API calls made.")
            raise typer.Exit(code=0)
        with get_connection() as conn:
            run_id = repository.insert_run(conn, command="calibrate", input_=f"{mp_code}:refresh")
        used = calib.refresh_products(
            targets, mp_code, config, keepa_client_factory=keepa_factory, run_id=run_id
        )
        with get_connection() as conn:
            repository.finish_run(conn, run_id, status="complete")
        console.print(f"[dim]Refreshed — {used} Keepa tokens used.[/dim]")

    with get_connection() as conn:
        report = calib.run(conn, asins=asins or None, marketplace=mp_code)
    _render_calibration(report)


def _render_calibration(report: object) -> None:
    from delium.analysis.calibration import CalibrationReport

    assert isinstance(report, CalibrationReport)
    if report.sample_size == 0:
        console.print(
            "[yellow]No products with stored Keepa monthlySold/fee to calibrate against.[/yellow] "
            "Fetch some products first, or run with --refresh."
        )
        return
    console.print(
        f"[bold]Calibration[/bold] — {report.sample_size} product(s).  "
        f"Units MAPE: {_pct(report.units_mape)}  ·  Fee MAPE: {_pct(report.fee_mape)}  ·  "
        f"Bucket hit-rate: {_rate(report.unit_bucket_hit_rate)}"
    )
    console.print(
        "[dim]monthlySold is bucketed (e.g. '100+'); an estimate inside the bucket counts as a "
        "hit (0 error). All thresholds are illustrative until calibrated.[/dim]"
    )
    console.print("\n[bold]Per product[/bold]:")
    for p in report.products[:50]:
        u = p.units
        contained = "—" if u.contained is None else ("✓in-bucket" if u.contained else "✗out")
        bucket = (
            "—"
            if u.bucket_low is None
            else f"{u.bucket_low}+{'' if u.bucket_high is None else f'..{u.bucket_high}'}"
        )
        du = "—" if u.delium_units is None else f"{u.delium_units:.0f}"
        f = p.fee
        fee = (
            "—"
            if f.keepa_fee_cents is None or f.delium_fee_cents is None
            else f"D ${f.delium_fee_cents / 100:.2f} vs K ${f.keepa_fee_cents / 100:.2f}"
        )
        console.print(
            f"  {p.asin}  units: Delium {du} vs Keepa {bucket} [{contained}]  ·  fee: {fee}"
        )
    console.print("\n[bold]Suggested adjustments[/bold] (NOT applied):")
    for s in report.suggestions:
        console.print(f"  [cyan]{s.category}[/cyan] — {s.note}")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


# ---------------------------------------------------------------------------
# Daily Scan
# ---------------------------------------------------------------------------
scan_app = typer.Typer(help="Daily Scan pipeline — resumable, budget-gated funnel.")
app.add_typer(scan_app, name="scan")

_CONF_CHOICES = {"low", "medium", "high"}


@scan_app.callback(invoke_without_command=True)
def scan_main(
    ctx: typer.Context,
    marketplaces: Annotated[
        str, typer.Option("--marketplaces", help="Comma-separated, e.g. US,CA,UK.")
    ] = "US",
    top: Annotated[int, typer.Option("--top", help="How many finalists to keep.")] = 10,
    sweep_size: Annotated[
        int | None,
        typer.Option("--sweep-size", help="Raw ASINs to bring back from the sweep."),
    ] = None,
    competitor_sets: Annotated[
        int | None,
        typer.Option("--competitor-sets", help="How many top products get a competitor set."),
    ] = None,
    budget_cap: Annotated[
        int | None,
        typer.Option("--budget-cap", help="Abort if projected Keepa tokens exceed this."),
    ] = None,
    max_spend: Annotated[
        float | None, typer.Option("--max-spend", help="Abort if projected USD exceed this.")
    ] = None,
    min_confidence: Annotated[
        str, typer.Option("--min-confidence", help="low | medium | high.")
    ] = "low",
    scheduled: Annotated[
        bool,
        typer.Option("--scheduled", help="Non-interactive; abort (never prompt) if over caps."),
    ] = False,
    light: Annotated[
        bool | None,
        typer.Option(
            "--light/--full",
            help="Light finalist mode: finalist-only reviews + one Claude call, top "
            "--enrich-limit only. Default: light for --scheduled, full otherwise.",
        ),
    ] = None,
    enrich_limit: Annotated[
        int,
        typer.Option("--enrich-limit", help="How many finalists to enrich (light mode)."),
    ] = 5,
    resume: Annotated[
        str | None, typer.Option("--resume", help="Resume a scan by id (from the last stage).")
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the cost confirmation (non-interactive, no TTY)."),
    ] = False,
    include_zombies: Annotated[
        bool,
        typer.Option("--zombies", help="Also run a small zombie-listing pass (off by default)."),
    ] = False,
) -> None:
    """Run a daily scan (interactive: shows the projected cost and confirms)."""
    if ctx.invoked_subcommand is not None:
        return  # `delium scan report …` handled by the subcommand
    from delium.analysis.models import Confidence
    from delium.discovery import daily_scan
    from delium.discovery.daily_scan import ScanParams

    initialize_database()
    config = load_config()
    if min_confidence.lower() not in _CONF_CHOICES:
        console.print(
            f"[bold red]--min-confidence must be one of {sorted(_CONF_CHOICES)}.[/bold red]"
        )
        raise typer.Exit(code=1)
    mps = tuple(_validate_marketplace(m) for m in marketplaces.split(",") if m.strip())

    from delium.profile import store as profile_store

    with get_connection() as conn:
        profile = profile_store.load_active(conn)
    # Light mode is the default for unattended (--scheduled) runs; interactive
    # runs default to the full validate path unless --light is passed.
    light_mode = scheduled if light is None else light
    # Caps default to the active Research Profile's per-scan caps when the flags
    # are omitted, so an unattended run is always bounded (Fix B).
    budget_cap_tokens = budget_cap if budget_cap is not None else profile.keepa_token_cap
    max_spend_usd = max_spend if max_spend is not None else profile.max_scan_usd
    # Sweep size + competitor-set count default to the active profile's sizing.
    sweep_target = sweep_size if sweep_size is not None else profile.scan_sweep_size
    competitor_pool = (
        competitor_sets if competitor_sets is not None else profile.scan_competitor_sets
    )
    params = ScanParams(
        marketplaces=mps,
        top_n=top,
        sweep_target=sweep_target,
        competitor_pool=competitor_pool,
        budget_cap_tokens=budget_cap_tokens,
        max_spend_usd=max_spend_usd,
        min_confidence=Confidence(min_confidence.lower()),
        scheduled=scheduled,
        light_finalists=light_mode,
        enrich_limit=enrich_limit,
        include_zombies=include_zombies,
    )
    clients = _build_scan_clients(config, light=light_mode)

    def _confirm(projection: daily_scan.CostProjection) -> bool:
        mode = (
            "light (finalist-only reviews + 1 Claude call each)" if light_mode else "full validate"
        )
        console.print(
            f"[bold]Finalist mode[/bold]: {mode}"
            + (f"; enriching top {enrich_limit}." if light_mode else ".")
        )
        console.print(
            f"[bold]Sizing[/bold]: sweep {sweep_target} ASINs · {competitor_pool} competitor "
            f"sets · top {top} finalists."
        )
        console.print(
            f"[bold]Projected cost[/bold]: ~{projection.total_tokens} Keepa tokens + "
            f"${projection.total_usd:.2f} (caps: {budget_cap_tokens} tokens / "
            f"${max_spend_usd:.2f})."
        )
        for s in projection.stages:
            console.print(f"  {s.name}: ~{s.keepa_tokens} tokens, ${s.data_usd + s.llm_usd:.2f}")
        for note in projection.notes:
            console.print(f"  [yellow]{note}[/yellow]")
        return bool(typer.confirm("Proceed and spend?"))

    # A scheduled run that fires after a missed launchd interval (Mac was asleep)
    # must not double-spend: skip if a scan already completed today.
    if scheduled and resume is None:
        from datetime import UTC, datetime

        today = datetime.now(UTC).date().isoformat()
        with get_connection() as conn:
            done_today = repository.scan_completed_on(conn, today)
        if done_today is not None:
            console.print(f"[yellow]A scan already completed today ({today}); skipping.[/yellow]")
            return

    from delium.scheduler import notify

    try:
        with get_connection() as conn:
            report = daily_scan.run_scan(
                conn,
                params=params,
                profile=profile,
                config=config,
                clients=clients,
                confirm=None if (scheduled or yes) else _confirm,
                resume_scan_id=resume,
            )
    except daily_scan.ScanAbortedError as exc:
        console.print(f"[bold red]Scan aborted:[/bold red] {exc}")
        if scheduled:
            notify("Delium scan aborted", str(exc))
        raise typer.Exit(code=1) from exc
    except daily_scan.ScanError as exc:
        console.print(f"[bold red]Scan failed:[/bold red] {exc}")
        if scheduled:
            notify("Delium scan failed", str(exc))
        raise typer.Exit(code=1) from exc
    if scheduled:
        n = report.funnel.get("finalists", 0)
        notify(
            "Delium scan complete",
            f"{n} finalists · {report.keepa_tokens} tokens · "
            f"${report.data_usd + report.llm_usd:.2f}",
        )
    _render_scan(report)


@scan_app.command("report")
def scan_report(
    scan_id: Annotated[
        str | None, typer.Argument(help="Scan id to print. Omit for the latest.")
    ] = None,
) -> None:
    """Print a past scan report (funnel, per-stage cost, finalists, categories)."""
    from delium.discovery.daily_scan import build_report

    initialize_database()
    with get_connection() as conn:
        if scan_id is None:
            latest = repository.latest_scan(conn)
            if latest is None:
                console.print("[yellow]No scans yet.[/yellow]")
                raise typer.Exit(code=0)
            scan_id = latest["id"]
        report = build_report(conn, scan_id)
    _render_scan(report)


# ---------------------------------------------------------------------------
# Scheduler (launchd) — `delium schedule …`
# ---------------------------------------------------------------------------
schedule_app = typer.Typer(help="Schedule the Daily Scan via macOS launchd.")
app.add_typer(schedule_app, name="schedule")


def _fmt_times(times: tuple[tuple[int, int], ...]) -> str:
    return ", ".join(f"{h:02d}:{m:02d}" for h, m in times) or "—"


@schedule_app.command("install")
def schedule_install(
    times: Annotated[
        str,
        typer.Option("--times", help="Daily run time(s), e.g. 06:00 or 06:00,18:00."),
    ] = "06:00",
) -> None:
    """Generate + load a launchd agent that runs `delium scan --scheduled` daily."""
    from delium.scheduler import Scheduler, parse_times

    try:
        parsed = parse_times(times)
    except ValueError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from exc
    sched = Scheduler()
    path = sched.install(parsed)
    console.print(f"[green]Installed[/green] launchd agent → {path}")
    console.print(f"  Runs daily at: [bold]{_fmt_times(parsed)}[/bold]")
    console.print(f"  Command: uv run delium scan --scheduled  (cwd {sched.repo_dir()})")
    console.print(f"  Log: {sched.log_path}")
    if not sched.is_macos():
        console.print(
            "[yellow]Not macOS — the plist was written but not loaded into launchd.[/yellow]"
        )


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Unload + remove the launchd agent."""
    from delium.scheduler import Scheduler

    sched = Scheduler()
    if sched.uninstall():
        console.print("[green]Uninstalled[/green] the Daily Scan launchd agent.")
    else:
        console.print("[yellow]No scheduled scan was installed.[/yellow]")


@schedule_app.command("status")
def schedule_status() -> None:
    """Show whether the agent is installed, its times, last run, and log tail."""
    from delium.scheduler import Scheduler

    initialize_database()
    sched = Scheduler()
    st = sched.status()
    if not st.installed:
        console.print(
            "[yellow]No scheduled scan installed.[/yellow] Run `delium schedule install`."
        )
    else:
        console.print(f"[green]Installed[/green]: {st.plist_path}")
        console.print(f"  Times: [bold]{_fmt_times(st.times)}[/bold]")
    with get_connection() as conn:
        last = repository.latest_scan(conn)
    if last is not None:
        spent = float(last["data_usd"]) + float(last["llm_usd"])
        console.print(
            f"  Last run: {last['created_at']} → [bold]{last['status']}[/bold] "
            f"({last['keepa_tokens']} tokens, ${spent:.2f})"
        )
    else:
        console.print("  Last run: [dim]none yet[/dim]")
    if st.log_tail:
        console.print(f"  Log tail ({st.log_path}):")
        for line in st.log_tail.splitlines():
            console.print(f"    [dim]{line}[/dim]")


@schedule_app.command("run-now")
def schedule_run_now() -> None:
    """Trigger the scheduled scan immediately (kickstarts the launchd agent when
    installed on macOS; otherwise runs `uv run delium scan --scheduled` here)."""
    import subprocess

    from delium.scheduler import Scheduler

    sched = Scheduler()
    if sched.is_macos() and sched.plist_path.exists():
        sched.run_now()
        console.print("[green]Kicked off[/green] the launchd agent. See `delium schedule status`.")
        return
    console.print("Running the scan inline: uv run delium scan --scheduled …")
    proc = subprocess.run(  # noqa: S603
        [sched.uv_path(), "run", "delium", "scan", "--scheduled"],
        cwd=str(sched.repo_dir()),
        check=False,
    )
    if proc.returncode != 0:
        raise typer.Exit(code=proc.returncode)


def _build_scan_clients(config: object, *, light: bool = False) -> ScanClients:
    """Provider factories + an optional finalist enricher (reviews + Claude
    differentiation + re-score, via the validation pipeline).

    `light` runs the enricher in the validation pipeline's light mode:
    finalist-only reviews + a single Claude call (no competitor reviews, no
    Analyst), reusing cached reviews/analysis. Full mode runs the full path."""
    from delium.analysis.models import Marketplace
    from delium.config.models import DeliumConfig
    from delium.discovery.daily_scan import EnrichResult, ScanClients

    assert isinstance(config, DeliumConfig)
    keepa_factory, dfs_factory = _build_provider_factories()
    reviews = _build_review_provider_probe()
    from delium.agents import build_llm_client

    llm = build_llm_client(config.agents)
    enrich = None
    if keepa_factory is not None and (reviews is not None or llm is not None):

        def _enrich(asin: str, marketplace: str, run_id: str) -> EnrichResult:
            from delium.validation import (
                Clients,
                ValidationRequest,
                ValidationStatus,
                run_validation,
            )

            val_clients = Clients(keepa=keepa_factory, dfs=dfs_factory, reviews=reviews, llm=llm)
            request = ValidationRequest(
                target=asin,
                marketplace=Marketplace(marketplace),
                run_id=run_id,
                force=False,
                light=light,
            )
            with get_connection() as vconn:
                report = run_validation(vconn, request, config, val_clients)
            scored = report.scored
            diff_available = bool(
                scored
                and any(p.pillar == "differentiation" and p.available for p in scored.pillars)
            )
            terminal = report.status in (ValidationStatus.SCORED, ValidationStatus.HARD_KILLED)
            eligible = terminal and (scored is None or scored.verdict.value != "avoid")
            return EnrichResult(
                opportunity_score=scored.score if scored is not None else None,
                differentiation_available=diff_available,
                data_usd=report.data_cost_usd,
                llm_usd=report.llm_cost_usd,
                eligible=eligible,
            )

        enrich = _enrich
    return ScanClients(keepa_factory=keepa_factory, dfs_factory=dfs_factory, enrich_finalist=enrich)


@app.command()
def zombies(
    marketplaces: Annotated[
        str, typer.Option("--marketplaces", help="Comma-separated, e.g. UK,CA.")
    ] = "UK,CA",
    min_dead_months: Annotated[
        float, typer.Option("--min-dead-months", help="Minimum continuous out-of-stock months.")
    ] = 6.0,
    min_reviews: Annotated[
        int, typer.Option("--min-reviews", help="Review floor (social proof).")
    ] = 50,
    min_rating: Annotated[float, typer.Option("--min-rating", help="Star rating floor.")] = 4.0,
    top: Annotated[int, typer.Option("--top", help="How many top candidates to keep.")] = 20,
    sweep_size: Annotated[
        int, typer.Option("--sweep-size", help="Raw ASINs to bring back from the finder.")
    ] = 100,
    pages: Annotated[
        int, typer.Option("--pages", help="How many finder pages to pull per marketplace.")
    ] = 1,
    check_demand: Annotated[
        bool,
        typer.Option("--check-demand", help="DataForSEO SERP for the top candidates (PAID)."),
    ] = False,
    budget_cap: Annotated[
        int | None,
        typer.Option("--budget-cap", help="Abort if projected Keepa tokens exceed this."),
    ] = None,
    max_spend: Annotated[
        float | None, typer.Option("--max-spend", help="Abort if projected USD exceed this.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the cost confirmation prompt.")
    ] = False,
) -> None:
    """Find out-of-stock-but-reviewed listings and verify they are truly dead
    (zombies) — with compliance flags and a suggested, policy-safe route."""
    from delium.discovery import zombies as zmod
    from delium.discovery.daily_scan import _category_ids_for
    from delium.profile import store as profile_store

    initialize_database()
    config = load_config()
    mps = tuple(_validate_marketplace(m) for m in marketplaces.split(",") if m.strip())
    keepa_factory, dfs_factory = _build_provider_factories()
    # Slice by the active Research Profile's preferred categories, resolved
    # per-marketplace exactly like the scan finder (US ids never sent to UK/CA).
    with get_connection() as conn:
        profile = profile_store.load_active(conn)
    category_ids: dict[str, list[int]] = {}
    for m in mps:
        ids, _note = _category_ids_for(profile, m)
        if ids:
            category_ids[m] = ids
    params = zmod.ZombieParams(
        marketplaces=mps,
        min_dead_months=min_dead_months,
        min_reviews=min_reviews,
        min_rating=min_rating,
        category_ids=category_ids,
        sweep_target=sweep_size,
        per_page=max(sweep_size, 50),
        pages=pages,
        top_n=top,
        budget_cap_tokens=budget_cap,
        max_spend_usd=max_spend,
        check_demand=check_demand,
    )
    clients = zmod.ZombieClients(keepa_factory=keepa_factory, dfs_factory=dfs_factory)

    def _confirm(p: zmod.ZombieCostProjection) -> bool:
        console.print(
            f"[bold]Projected cost[/bold]: ~{p.total_tokens} Keepa tokens "
            f"({p.finder_tokens} finder + {p.hydrate_tokens} hydrate) + ${p.total_usd:.2f} "
            f"DataForSEO{' (demand check)' if check_demand else ''}."
        )
        for note in p.notes:
            console.print(f"  [yellow]{note}[/yellow]")
        return bool(typer.confirm("Proceed and spend?"))

    try:
        with get_connection() as conn:
            report = zmod.run_zombies(
                conn,
                params=params,
                config=config,
                clients=clients,
                confirm=None if yes else _confirm,
            )
    except zmod.ZombieAbortedError as exc:
        console.print(f"[bold red]Zombies aborted:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_zombies(report)


def _render_zombies(report: object) -> None:
    from delium.discovery.zombies import ZombieReport

    assert isinstance(report, ZombieReport)
    console.print(
        f"[bold]Zombies[/bold]  ·  markets {', '.join(report.marketplaces)}  ·  "
        f"swept {report.swept} → hydrated {report.hydrated} → {len(report.results)} ranked"
    )
    for note in report.notes:
        console.print(f"  [yellow]{note}[/yellow]")
    for diag in report.diagnostics:
        console.print(f"  [dim]finder[/dim] {diag.summary()}", markup=False)
    console.print(f"[bold]Cost[/bold]: {report.keepa_tokens} Keepa tokens · ${report.data_usd:.2f}")
    if not report.results:
        console.print("  [dim]no candidates[/dim]")
        return
    _colour = {
        "Verified zombie": "green",
        "Possibly temporary": "yellow",
        "Not a zombie": "dim",
    }
    for i, r in enumerate(report.results, start=1):
        colour = _colour.get(r.verdict.value, "white")
        score = "—" if r.score is None else f"{r.score:.0f}"
        console.print(
            f"\n  {i:>2}. [{colour}]{r.asin}[/{colour}] [{r.marketplace}]  "
            f"[{colour}]{r.verdict.value}[/{colour}]  score {score} · {r.confidence.value} conf"
        )
        for c in r.components:
            if c.score is not None:
                console.print(f"      {c.name:<22} {c.score:>5.0f}  [dim]{c.detail}[/dim]")
        if r.missing:
            console.print(f"      [dim]unknown: {', '.join(r.missing)}[/dim]")
        if r.reasons:
            console.print(f"      [italic]{r.reasons[-1]}[/italic]", markup=False)
        console.print(f"      [bold]Brand[/bold]: {r.compliance.brand_label}")
        for route in r.compliance.routes:
            console.print(f"      → {route}", markup=False)
        console.print(f"      [yellow]{r.compliance.manual_check}[/yellow]", markup=False)


def _render_scan(report: object) -> None:
    from delium.discovery.daily_scan import ScanReport

    assert isinstance(report, ScanReport)
    console.print(
        f"[bold]Scan[/bold] {report.scan_id}  ·  {report.status}  ·  "
        f"markets {', '.join(report.marketplaces)}"
    )
    for note in report.notes:
        console.print(f"  [yellow]{note}[/yellow]")
    funnel = report.funnel
    console.print(
        "[bold]Funnel[/bold]: "
        + " → ".join(
            f"{k} {funnel.get(k, 0)}"
            for k in ("swept", "hydrated", "killed", "scored", "competitor_set", "finalists")
        )
    )
    console.print(
        f"[bold]Cost[/bold]: {report.keepa_tokens} Keepa tokens · "
        f"${report.data_usd:.2f} data · ${report.llm_usd:.2f} LLM"
    )
    console.print("\n[bold]Per stage[/bold]:")
    for s in report.stages:
        console.print(
            f"  {s['stage']} {s['name']:<16} {s['status']:<9} "
            f"in {s['in']:>4} out {s['out']:>4} killed {s['killed']:>4}  "
            f"{s['tokens']} tok  ${s['data_usd']:.2f}+${s['llm_usd']:.2f}"
        )
    console.print("\n[bold]Scan inbox — finalists[/bold] (promote to shortlist manually):")
    if not report.finalists:
        console.print("  [dim]none[/dim]")
    for f in report.finalists:
        sell = "—" if f["sellability"] is None else f"{f['sellability']:.0f}"
        console.print(
            f"  {f['rank']:>2}. [green]{f['asin']}[/green] [{f['marketplace']}]  "
            f"sellability {sell} · {f['confidence']} conf · diff {f['differentiation']}"
        )
        if f["reason"]:
            console.print(f"      [dim]{f['reason']}[/dim]", markup=False)
    console.print("\n[bold]Emerging categories[/bold]:")
    for c in report.categories:
        score = "—" if c["score"] is None else f"{c['score']:.0f}"
        console.print(f"  [cyan]{c['category']}[/cyan] {score}/100 — {c['reason']}", markup=False)


@app.command()
def portfolio() -> None:
    """Show all validated candidates ranked by score, capital, and payback."""
    log.info("portfolio requested")
    _not_implemented("portfolio")


@app.command()
def ui(
    port: Annotated[int, typer.Option("--port", help="Local port to serve on.")] = 8501,
) -> None:
    """Launch the local Streamlit web UI (browser front end, localhost only)."""
    import importlib.util
    import subprocess
    import sys
    from pathlib import Path

    if importlib.util.find_spec("streamlit") is None:
        console.print(
            "[bold red]Streamlit is not installed.[/bold red] Install the UI extras:\n"
            "  uv sync --group ui        [dim](or: pip install 'streamlit>=1.37')[/dim]"
        )
        raise typer.Exit(code=1)

    app_path = Path(__file__).resolve().parent.parent / "ui" / "app.py"
    console.print(f"[green]Starting Delium UI[/green] at http://localhost:{port}  (Ctrl-C to stop)")
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(port),
        "--server.address",
        "localhost",
    ]
    raise typer.Exit(code=subprocess.call(cmd))


if __name__ == "__main__":
    app()
