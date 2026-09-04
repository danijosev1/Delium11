"""Delium CLI entry point.

Five commands, matching ARCHITECTURE.md §4 — `discover`, `validate`, `pains`,
`watch`, `portfolio`. This build step wires up the command surface, argument
parsing, config loading, and logging; the actual pipelines (data fetch →
deterministic analysis → agents → report) are later build steps and are
deliberately left as stubs here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

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

    _render_discovery(report, limit or config.discovery.max_ranked)


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

    _render_validation(report)
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
