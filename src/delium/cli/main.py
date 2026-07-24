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
from delium.providers import ProviderError
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


@fetch_app.command("product")
def fetch_product_cmd(
    asin: Annotated[str, typer.Argument(help="Amazon ASIN, e.g. B08XXXXXXX.")],
    force: Annotated[bool, typer.Option("--force", help="Bypass the cache and refetch.")] = False,
) -> None:
    """Fetch a product from Keepa (cache-first), store it, and show a summary."""
    from delium.ingestion import fetch_product

    initialize_database()  # idempotent — ensures the schema exists
    config = load_config()

    try:
        client = KeepaClient.from_env()
    except ProviderError as exc:
        console.print(f"[bold red]Provider error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="fetch.product", input_=asin)

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


@app.command()
def portfolio() -> None:
    """Show all validated candidates ranked by score, capital, and payback."""
    log.info("portfolio requested")
    _not_implemented("portfolio")


if __name__ == "__main__":
    app()
