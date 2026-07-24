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
from delium.database import initialize_database
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
