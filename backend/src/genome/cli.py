"""Typer CLI entry point. Exposed as the ``genome`` console script."""

from __future__ import annotations

import logging
from pathlib import (
    Path,  # noqa: TC003 — typer needs Path at runtime to resolve Annotated[Path, ...]
)
from typing import Annotated, get_args

import structlog
import typer

from genome import __version__
from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.db.init_schema import init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.ingest import Source, ingest_file
from genome.ingest.liftover import LiftoverEngine
from genome.merge import merge_all

_VALID_INGEST_SOURCES: tuple[str, ...] = tuple(s for s in get_args(Source) if s != "topmed_imputed")
_VALID_LIFTOVER_ENGINES: tuple[str, ...] = tuple(get_args(LiftoverEngine))

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
def ingest(
    file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Raw export file to ingest.",
        ),
    ],
    source: Annotated[
        str,
        typer.Option(
            "--source",
            "-s",
            help="Raw export source: '23andme' or 'ancestry'.",
            case_sensitive=False,
        ),
    ],
    chain_file: Annotated[
        Path | None,
        typer.Option(
            "--chain-file",
            help=(
                "Local UCSC chain file (e.g. hg19ToHg38.over.chain.gz). "
                "Required when the input file is GRCh37."
            ),
            exists=False,
            file_okay=True,
            dir_okay=False,
        ),
    ] = None,
    liftover_engine: Annotated[
        str,
        typer.Option(
            "--liftover-engine",
            help=(
                "Lift-over engine for GRCh37 inputs. 'auto' (default) picks the "
                "`liftover` PyPI package and falls back to `pyliftover` with an "
                "INFO log. 'liftover' / 'pyliftover' force one engine and raise "
                "if it isn't installed."
            ),
            case_sensitive=False,
        ),
    ] = "auto",
) -> None:
    """Parse, normalize, lift over, and persist a raw export.

    Re-ingesting the same source replaces prior calls (the prior rows are
    deactivated, not deleted; supersession is preserved for audit).
    """
    src = source.lower()
    if src not in _VALID_INGEST_SOURCES:
        msg = f"unsupported --source {source!r}; expected one of {sorted(_VALID_INGEST_SOURCES)}"
        raise typer.BadParameter(msg)

    engine = liftover_engine.lower()
    if engine not in _VALID_LIFTOVER_ENGINES:
        msg = (
            f"unsupported --liftover-engine {liftover_engine!r}; "
            f"expected one of {sorted(_VALID_LIFTOVER_ENGINES)}"
        )
        raise typer.BadParameter(msg)

    result = ingest_file(
        source=src,  # type: ignore[arg-type]
        path=file,
        chain_file=chain_file,
        liftover_engine=engine,  # type: ignore[arg-type]
    )
    typer.echo(
        f"run_id={result.run_id} qc_id={result.qc_id} "
        f"variants={result.variants_total} called={result.variants_called} "
        f"no_call={result.variants_no_call} "
        f"dropped_non_canonical={result.variants_dropped_non_canonical} "
        f"dropped_lift_to_non_canonical={result.variants_dropped_lift_to_non_canonical} "
        f"new_master_rows={result.new_variants_master_rows} "
        f"deactivated_prior={result.deactivated_prior_calls} "
        f"call_rate={result.call_rate:.4f} sex={result.sex_inferred} "
        f"qc={result.qc_status}",
    )


@app.command()
def merge() -> None:
    """Compute the consensus across all active genotype calls.

    Rebuilds ``consensus_genotypes`` and ``discrepancies`` from the current
    state of ``genotype_calls`` using the ``consensus_v1`` rule (documented
    in ``docs/consensus.md``). Idempotent: re-running after a re-ingest is
    the supported way to refresh the merged view.
    """
    result = merge_all()
    typer.echo(
        f"rule={result.resolution_rule} "
        f"consensus_rows={result.consensus_rows_written} "
        f"discrepancy_rows={result.discrepancy_rows_written} "
        f"strand_flips={result.strand_flip_resolutions}",
    )
    if result.method_counts:
        methods = " ".join(f"{k}={v}" for k, v in sorted(result.method_counts.items()))
        typer.echo(f"consensus_methods: {methods}")
    if result.discrepancy_type_counts:
        types = " ".join(f"{k}={v}" for k, v in sorted(result.discrepancy_type_counts.items()))
        typer.echo(f"discrepancy_types: {types}")
    if result.severity_counts:
        sevs = " ".join(f"{k}={v}" for k, v in sorted(result.severity_counts.items()))
        typer.echo(f"severity: {sevs}")
    if result.concordance_rate is not None:
        typer.echo(f"shared-call concordance rate: {result.concordance_rate:.4f}")


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
