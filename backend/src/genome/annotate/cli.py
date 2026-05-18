"""Typer subcommands for ``genome annotate``.

5.0 ships two commands:

* ``annotate status`` — read-only summary of what's loaded across every
  source in :data:`KNOWN_SOURCE_DBS`. Does not touch the on-disk cache.
* ``annotate refresh --source <db>`` — dispatches to the registered
  loader. In 5.0 no loaders are registered; the command exits 2 with a
  helpful stub message that points the user at 5.1+. The implementation
  goes through :func:`genome.annotate.registry.get_loader` so once
  loaders ship, the command's CLI surface does not change.
"""

from __future__ import annotations

from typing import Annotated

import typer

from genome.annotate.registry import RefreshResult, get_loader, known_loaders
from genome.annotate.source_versions import KNOWN_SOURCE_DBS, get_current_version
from genome.db.duckdb_conn import duckdb_connection

annotate_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Load and refresh reference annotations (ClinVar, GWAS Catalog, "
        "PharmGKB, CPIC, PGS Catalog, gnomAD, dbSNP, VEP, gene/trait/pathway "
        "dictionaries). Sub-phase 5.0 ships the scaffold only; per-source "
        "loaders land in 5.1+."
    ),
)


def _format_status_line(name: str, width: int, value: str) -> str:
    """Render one row of ``annotate status`` output with a padded source label."""
    return f"{name:<{width}}  {value}"


@annotate_app.command("status")
def annotate_status() -> None:
    """Print loaded version + ingested_at + record_count per known source.

    Reads ``annotation_source_versions`` only. Sources with no
    ``is_current = TRUE`` row print as ``not loaded``. The on-disk
    cache directory is *not* touched — running ``annotate status``
    against a fresh checkout must not create
    ``~/.cache/genome/annotations/``.
    """
    sources = sorted(KNOWN_SOURCE_DBS)
    width = max(len(s) for s in sources)
    with duckdb_connection(read_only=True) as conn:
        for source in sources:
            current = get_current_version(conn, source)
            if current is None:
                typer.echo(_format_status_line(f"{source}:", width + 1, "not loaded"))
                continue
            suffix = ""
            if current.record_count is not None:
                suffix = f", {current.record_count} records"
            value = f"{current.version} (ingested {current.ingested_at}{suffix})"
            typer.echo(_format_status_line(f"{source}:", width + 1, value))


def _stub_message(source: str) -> str:
    """Format the no-loader-registered stub message.

    Lists currently-registered sources when available (5.1+); falls
    back to the 5.0-scaffold-only note when the registry is empty.
    """
    available = sorted(known_loaders())
    if available:
        listing = ", ".join(available)
    else:
        listing = (
            "(none registered yet — sub-phase 5.0 is the scaffold only; "
            "per-source loaders land in 5.1+)"
        )
    return f"no loader registered for source {source!r}.\nAvailable sources: {listing}"


@annotate_app.command("refresh")
def annotate_refresh(
    source: Annotated[
        str,
        typer.Option(
            "--source",
            help=(
                "Source DB label to refresh (e.g. 'clinvar', 'gwas_catalog'). "
                "In sub-phase 5.0 no loaders are registered — every value "
                "exits with a stub message."
            ),
        ),
    ],
    force: Annotated[  # noqa: FBT002 — typer boolean flag, --force is opt-in
        bool,
        typer.Option(
            "--force",
            help=(
                "Force re-download + reload regardless of cached state. "
                "Honoured at the loader's discretion; passed through verbatim."
            ),
        ),
    ] = False,
    skip_if_same_version: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--skip-if-same-version",
            help=(
                "Safety net for --force re-runs: skip the supersession path "
                "when the resolved upstream version + file hash already match "
                "the currently-active row in annotation_source_versions. The "
                "loader emits 'supersession_skipped_same_version' and exits "
                "cleanly (treated as success). Off by default; existing "
                "--force invocations behave identically when this flag is "
                "not set. See finding-009 #14."
            ),
        ),
    ] = False,
) -> None:
    """Refresh one annotation source.

    Looks the source up in the loader registry. In 5.0 the registry is
    empty, so the command exits with code 2 and a stub message that
    points the user at the 5.1+ PRs that add per-source loaders.
    """
    loader = get_loader(source)
    if loader is None:
        typer.echo(_stub_message(source), err=True)
        raise typer.Exit(code=2)
    result: RefreshResult = loader(force, skip_if_same_version)
    typer.echo(
        f"source_db={result.source_db} "
        f"source_version_id={result.source_version_id} "
        f"version={result.version} "
        f"records={result.record_count} "
        f"already_current={result.was_already_current}",
    )


__all__ = [
    "annotate_app",
]
