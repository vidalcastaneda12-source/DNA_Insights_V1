"""Typer subcommands for ``genome annotate``.

5.0 ships two commands:

* ``annotate status`` — read-only summary of what's loaded across every
  source in :data:`KNOWN_SOURCE_DBS`. Does not touch the on-disk cache.
* ``annotate refresh --source <db>`` — dispatches to the registered
  loader. In 5.0 no loaders are registered; the command exits 2 with a
  helpful stub message that points the user at 5.1+. The implementation
  goes through :func:`genome.annotate.registry.get_loader` so once
  loaders ship, the command's CLI surface does not change.

Sub-phase 5.5 added the remote-tabix flags (``--chromosomes``, ``--resume``,
``--coalesce-distance``) plus a ``--version`` override; 5.6 generalised them to
every remote-tabix source (:data:`_REMOTE_TABIX_SOURCES` — gnomad, dbsnp).
Passing them on a non-remote-tabix source raises ``BadParameter`` so a
misroute is loud rather than silent.
"""

from __future__ import annotations

from typing import Annotated, Final

import typer

from genome.annotate.registry import RefreshResult, get_loader, known_loaders
from genome.annotate.source_versions import KNOWN_SOURCE_DBS, get_current_version
from genome.db.duckdb_conn import duckdb_connection

_REMOTE_TABIX_SOURCES: Final = frozenset({"gnomad", "dbsnp"})
"""Sources whose loaders stream a remote bgzipped+tabix VCF and accept the rich
``--version`` / ``--chromosomes`` / ``--resume`` / ``--coalesce-distance`` flags.
"""

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


def _reject_remote_tabix_only_flag(name: str, source: str) -> None:
    """Raise BadParameter when a remote-tabix-only flag is passed for another source."""
    sources = ", ".join(sorted(_REMOTE_TABIX_SOURCES))
    msg = f"--{name} is only valid for remote-tabix sources ({sources}), not source {source!r}"
    raise typer.BadParameter(msg)


def _refresh_remote_tabix(  # noqa: PLR0913 — CLI flag passthrough
    source: str,
    *,
    force: bool,
    skip_if_same_version: bool,
    version: str | None,
    chrom_filter: tuple[str, ...] | None,
    resume: bool,
    coalesce_distance: int | None,
) -> RefreshResult:
    """Dispatch ``annotate refresh`` to a remote-tabix loader (gnomad / dbsnp).

    Each loader's ``refresh`` shares the same signature; the only per-source
    differences are the locked default version and default coalesce distance,
    which the loader module exposes as constants. The user's ``--version`` /
    ``--coalesce-distance`` (when given) override those defaults.
    """
    if source == "gnomad":
        from genome.annotate.loaders import gnomad  # noqa: PLC0415

        return gnomad.refresh(
            force,
            skip_if_same_version,
            version=version or gnomad.GNOMAD_VERSION,
            chromosomes=chrom_filter,
            resume=resume,
            coalesce_distance=(
                coalesce_distance
                if coalesce_distance is not None
                else gnomad.DEFAULT_COALESCE_DISTANCE_BP
            ),
        )

    from genome.annotate.loaders import dbsnp  # noqa: PLC0415

    return dbsnp.refresh(
        force,
        skip_if_same_version,
        version=version or dbsnp.DBSNP_VERSION,
        chromosomes=chrom_filter,
        resume=resume,
        coalesce_distance=(
            coalesce_distance
            if coalesce_distance is not None
            else dbsnp.DEFAULT_COALESCE_DISTANCE_BP
        ),
    )


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
                "[gnomad/dbsnp only] Override the loader's locked source version "
                "(gnomad '4.1.1', dbsnp '157'). Used to test a future release "
                "label or re-load against an explicit prior release. Ignored by "
                "non-remote-tabix sources."
            ),
        ),
    ] = None,
    chromosomes: Annotated[
        str | None,
        typer.Option(
            "--chromosomes",
            help=(
                "[gnomad/dbsnp only] Comma- or whitespace-separated list of "
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
                "[gnomad/dbsnp only] Continue a previously-interrupted load. "
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
                "[gnomad/dbsnp only] Maximum gap (bp) between adjacent filter "
                "positions before they're split into separate tabix ranges. "
                "Defaults to 50000. Larger values fetch more contiguous data; "
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

    if source in _REMOTE_TABIX_SOURCES:
        result: RefreshResult = _refresh_remote_tabix(
            source,
            force=force,
            skip_if_same_version=skip_if_same_version,
            version=version,
            chrom_filter=chrom_filter,
            resume=resume,
            coalesce_distance=coalesce_distance,
        )
    else:
        # Reject remote-tabix-only flags on other sources — passing them is a
        # misroute, and silent acceptance would mask the bug.
        if version is not None:
            _reject_remote_tabix_only_flag("version", source)
        if chrom_filter is not None:
            _reject_remote_tabix_only_flag("chromosomes", source)
        if resume:
            _reject_remote_tabix_only_flag("resume", source)
        if coalesce_distance is not None:
            _reject_remote_tabix_only_flag("coalesce-distance", source)
        result = loader(force, skip_if_same_version)
    typer.echo(
        f"source_db={result.source_db} "
        f"source_version_id={result.source_version_id} "
        f"version={result.version} "
        f"records={result.record_count} "
        f"already_current={result.was_already_current}",
    )


@annotate_app.command("refresh-index")
def annotate_refresh_index(
    force: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--force",
            help="Accepted for symmetry; the build is unconditional (no-op).",
        ),
    ] = False,
) -> None:
    """Rebuild the ``variant_annotations_index`` rollup (sub-phase 5.7).

    Joins the currently-active ClinVar / GWAS Catalog / gnomAD / PharmGKB
    releases into one sparse row per variant that carries ≥1 annotation, so
    ``variant_full_v`` returns joined annotations instead of NULLs. VEP columns
    and ``is_acmg_sf`` ship NULL pending Phase 6. Wholesale replace in one
    transaction — readers never see a torn index.
    """
    from genome.annotate.index_refresh import refresh_index  # noqa: PLC0415

    result = refresh_index(force=force)
    typer.echo(
        f"variant_annotations_index rebuilt: rows={result.row_count} "
        f"clinvar={result.clinvar_matches} gwas={result.gwas_matches} "
        f"gnomad={result.gnomad_matches} pharmgkb={result.pharmgkb_matches} "
        f"curated={result.curated_count} versions={result.refresh_versions} "
        f"elapsed_ms={result.elapsed_ms}",
    )


@annotate_app.command("refresh-aliases")
def annotate_refresh_aliases(
    force: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--force",
            help=(
                "Re-download RsMergeArch and rebuild the alias set even when "
                "variant_aliases is already populated for the current dbSNP "
                "version (DELETE + re-INSERT under the same source_version_id, "
                "in one transaction)."
            ),
        ),
    ] = False,
) -> None:
    """Populate ``variant_aliases`` from dbSNP's rs-merge archive (post-5.7 backfill).

    Loads NCBI's ``RsMergeArch.bcp.gz`` (filtered to merges touching the user's
    own rsIDs on either side) into ``variant_aliases`` under the **current**
    dbSNP ``source_version_id`` — the dbSNP VCF must already be loaded
    (``genome annotate refresh --source dbsnp``); the pointer is not flipped and
    the VCF is not re-streamed. Fills the map the deferred tier-2 rsID merge
    matching consumes. Re-run after any future dbSNP refresh to re-attach the
    map to the new epoch.
    """
    from genome.annotate.loaders.variant_aliases import (  # noqa: PLC0415
        DbsnpNotLoadedError,
        refresh_aliases,
    )

    try:
        result = refresh_aliases(force=force)
    except DbsnpNotLoadedError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(
        f"variant_aliases populated: source_version_id={result.target_source_version_id} "
        f"already_populated={result.already_populated} "
        f"rows={result.rows_loaded} "
        f"distinct_alias={result.distinct_alias_rsid} "
        f"distinct_current={result.distinct_current_rsid} "
        f"user_old_rsid_hits={result.user_old_rsid_hits} "
        f"user_current_rsid_hits={result.user_current_rsid_hits} "
        f"scanned={result.source_rows_scanned}",
    )


__all__ = [
    "annotate_app",
]
