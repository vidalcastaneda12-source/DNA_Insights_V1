"""Typer CLI entry point. Exposed as the ``genome`` console script."""

from __future__ import annotations

import logging
from typing import Annotated

import structlog
import typer

from genome import __version__
from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.db.init_schema import init_databases
from genome.db.sqlite_conn import sqlcipher_connection

app = typer.Typer(no_args_is_help=True, add_completion=False, help="DNA insights CLI")


def _configure_logging() -> None:
    settings = get_settings()
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


@app.callback()
def _main() -> None:
    _configure_logging()


@app.command()
def init() -> None:
    """Create both databases (idempotent)."""
    result = init_databases()
    typer.echo(
        f"DuckDB ({result.duckdb_path}): "
        f"{'created' if result.duckdb_created else 'present (skipped)'}",
    )
    typer.echo(
        f"app.db ({result.sqlite_path}): "
        f"{'created' if result.sqlite_created else 'present (skipped)'}",
    )


@app.command()
def status() -> None:
    """Print a summary of database state."""
    settings = get_settings()
    typer.echo(f"DuckDB path:   {settings.genome_duckdb_path}")
    typer.echo(f"  exists:      {settings.genome_duckdb_path.exists()}")

    if settings.genome_duckdb_path.exists():
        with duckdb_connection(read_only=True) as conn:
            tables = conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'main'"
                " AND table_type = 'BASE TABLE'",
            ).fetchone()
            views = conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'main'"
                " AND table_type = 'VIEW'",
            ).fetchone()
            typer.echo(f"  tables:      {tables[0] if tables else 0}")
            typer.echo(f"  views:       {views[0] if views else 0}")

    typer.echo(f"app.db path:   {settings.app_db_path}")
    typer.echo(f"  exists:      {settings.app_db_path.exists()}")

    if settings.app_db_path.exists():
        with sqlcipher_connection() as conn:
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'",
            ).fetchone()
            profiles = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()
            prefs = conn.execute("SELECT COUNT(*) FROM user_preferences").fetchone()
            typer.echo(f"  tables:      {tables[0] if tables else 0}")
            typer.echo(f"  profiles:    {profiles[0] if profiles else 0}")
            typer.echo(f"  preferences: {prefs[0] if prefs else 0}")

    typer.echo(f"External calls enabled: {settings.external_calls_enabled}")


@app.command()
def version() -> None:
    """Print the genome package version."""
    typer.echo(__version__)


_VersionFlag = Annotated[
    bool, typer.Option("--version", help="Print version and exit", is_eager=True)
]


def _print_version_and_exit(value: bool) -> None:  # noqa: FBT001
    if value:
        typer.echo(__version__)
        raise typer.Exit


if __name__ == "__main__":
    app()
