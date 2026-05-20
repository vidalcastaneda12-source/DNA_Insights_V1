"""Typer subcommands for ``genome annotate``.

5.0 ships two commands:

* ``annotate status`` — read-only summary of what's loaded across every
  source in :data:`KNOWN_SOURCE_DBS`. Does not touch the on-disk cache.
* ``annotate refresh --source <db>`` — dispatches to the registered
  loader. In 5.0 no loaders are registered; the command exits 2 with a
  helpful stub message that points the user at 5.1+. The implementation
  goes through :func:`genome.annotate.registry.get_loader` so once
  loaders ship, the command's CLI surface does not change.

Sub-phase 5.5 adds three gnomad-specific flags
(``--chromosomes``, ``--resume``, ``--coalesce-distance``) plus a
``--version`` override. These are only honoured for ``--source gnomad``;
passing them on another source raises ``BadParameter`` so a misroute is
loud rather than silent.
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

    Reads ``annotation_source_versions`` via the ``annotation_sources``
    pointer. Sources with no pointer row print as ``not loaded``. The
    on-disk cache directory is *not* touched — running ``annotate
    status`` against a fresh checkout must not create
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


def _parse_chromosomes(value: str | None) -> tuple[str, ...] | None:
    """Parse the ``--chromosomes`` flag into a tuple of chrom labels.

    Accepts comma- or whitespace-separated values; trims whitespace.
    ``None`` returns ``None`` (the "no filter" sentinel). Empty string
    or a list of only-empty tokens also returns ``None``.
    """
    if value is None:
        return None
    parts = [p.strip() for p in value.replace(",", " ").split() if p.strip()]
    if not parts:
        return None
    return tuple(parts)


def _reject_gnomad_only_flag(name: str, source: str) -> None:
    """Raise BadParameter when a gnomad-only flag is passed for another source."""
    msg = f"--{name} is gnomad-specific and not applicable to source {source!r}"
    raise typer.BadParameter(msg)


@annotate_app.command("refresh")
def annotate_refresh(  # noqa: PLR0913 — irreducible CLI surface; gnomad-specific flags are isolated
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
    version: Annotated[
        str | None,
        typer.Option(
            "--version",
            help=(
                "[gnomad-only] Override the locked GNOMAD_VERSION ('4.1.1'). "
                "Used to test a future release label or to re-load against an "
                "explicit prior gnomAD release. Ignored by other loaders."
            ),
        ),
    ] = None,
    chromosomes: Annotated[
        str | None,
        typer.Option(
            "--chromosomes",
            help=(
                "[gnomad-only] Comma- or whitespace-separated list of "
                "chromosomes to refresh (e.g. '22' or '1,2,3,X'). When "
                "restricted, the version-pointer flip is deferred — run "
                "--resume against the full chrom set to finalize."
            ),
        ),
    ] = None,
    resume: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--resume",
            help=(
                "[gnomad-only] Continue a previously-interrupted load. "
                "Locates the in-flight (un-flipped) source_version_id for the "
                "resolved version and runs only the chromosomes that haven't "
                "yet been populated under it. Flips the pointer at the end "
                "when every supported chrom is present."
            ),
        ),
    ] = False,
    coalesce_distance: Annotated[
        int | None,
        typer.Option(
            "--coalesce-distance",
            help=(
                "[gnomad-only] Maximum gap (bp) between adjacent filter "
                "positions before they're split into separate tabix ranges. "
                "Defaults to 1000. Larger values fetch more contiguous data; "
                "smaller values issue more tabix queries."
            ),
        ),
    ] = None,
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

    chrom_filter = _parse_chromosomes(chromosomes)

    if source == "gnomad":
        from genome.annotate.loaders.gnomad import (  # noqa: PLC0415 — local import keeps the module side-effect-free
            DEFAULT_COALESCE_DISTANCE_BP,
            GNOMAD_VERSION,
        )
        from genome.annotate.loaders.gnomad import (  # noqa: PLC0415 — local import keeps the module side-effect-free
            refresh as gnomad_refresh,
        )

        result: RefreshResult = gnomad_refresh(
            force,
            skip_if_same_version,
            version=version or GNOMAD_VERSION,
            chromosomes=chrom_filter,
            resume=resume,
            coalesce_distance=(
                coalesce_distance if coalesce_distance is not None else DEFAULT_COALESCE_DISTANCE_BP
            ),
        )
    else:
        # Reject gnomad-only flags on other sources — passing them is a
        # misroute, and silent acceptance would mask the bug.
        if version is not None:
            _reject_gnomad_only_flag("version", source)
        if chrom_filter is not None:
            _reject_gnomad_only_flag("chromosomes", source)
        if resume:
            _reject_gnomad_only_flag("resume", source)
        if coalesce_distance is not None:
            _reject_gnomad_only_flag("coalesce-distance", source)
        result = loader(force, skip_if_same_version)
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
