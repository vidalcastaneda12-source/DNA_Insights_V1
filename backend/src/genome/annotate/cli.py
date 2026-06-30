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
    jobs: int | None,
) -> RefreshResult:
    """Dispatch ``annotate refresh`` to a remote-tabix loader (gnomad / dbsnp).

    Each loader's ``refresh`` shares the same signature; the only per-source
    differences are the locked default version and default coalesce distance,
    which the loader module exposes as constants. The user's ``--version`` /
    ``--coalesce-distance`` (when given) override those defaults.

    ``--jobs`` parallelizes the per-chromosome stream. Only gnomad implements
    it (the ~14.6 h full-genome load); it resolves to
    :data:`gnomad.DEFAULT_PARALLEL_JOBS` when omitted. dbsnp does not yet
    parallelize, so an explicit ``--jobs`` for dbsnp is rejected rather than
    silently ignored.
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
            jobs=jobs if jobs is not None else gnomad.DEFAULT_PARALLEL_JOBS,
        )

    if jobs is not None:
        msg = "--jobs is only implemented for gnomad; dbsnp streams sequentially — omit --jobs"
        raise typer.BadParameter(msg)

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
    jobs: Annotated[
        int | None,
        typer.Option(
            "--jobs",
            help=(
                "[gnomad only] Number of chromosomes to stream concurrently "
                "(worker processes). The full-genome gnomad load is "
                "network-latency-bound; higher values keep more tabix requests "
                "in flight. Defaults to 8. Set 1 for the sequential path. "
                "Tune to your connection (try 4/8/16). Rejected for dbsnp."
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
            jobs=jobs,
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
        if jobs is not None:
            _reject_remote_tabix_only_flag("jobs", source)
        result = loader(force, skip_if_same_version)
    if result.was_already_current:
        # Short-circuit: nothing was loaded. Printing ``records=<N>`` here is
        # misleading (it is the *current* count, not an insert). Report the
        # already-current state explicitly instead. Semantics are unchanged —
        # this is presentation only (finding-010 #12).
        typer.echo(
            f"source_db={result.source_db} "
            f"source_version_id={result.source_version_id} "
            f"version={result.version} "
            f"already_current=True "
            f"(no rows loaded; current record_count={result.record_count})",
        )
    else:
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
        f"curated={result.curated_count} tier2_rsid_lifts={result.tier2_rsid_lifts} "
        f"versions={result.refresh_versions} "
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


@annotate_app.command("canonicalize-variants")
def annotate_canonicalize_variants(
    force: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--force",
            help=(
                "Bypass the already-canonical fast-path and run the full walk. "
                "On idempotent re-runs this still writes nothing new (all deltas "
                "zero); use it as a belt-and-suspenders verification step."
            ),
        ),
    ] = False,
    no_backup: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--no-backup",
            help=(
                "Skip the pre-mutation genome.duckdb snapshot. The snapshot is "
                "the rollback path for a successful-but-wrong backfill; only "
                "skip it when re-running with an existing snapshot already in "
                "archive/canonicalize/."
            ),
        ),
    ] = False,
) -> None:
    """Canonicalize ``variants_master`` REF/ALT against dbSNP (post-5.7 backfill).

    Re-orients the alphabetical-ordering swap victims and recovers hom-only
    (``ref==alt``) rows by assigning a real ALT from the currently-active
    dbSNP source-version. Collapses any rows whose new canonical key collides
    with a sibling at the same position (re-pointing
    ``genotype_calls.variant_id`` FKs to the survivor). Closes finding-005 #1
    (ordering aspect) and #6 (hom-only recovery).

    The downstream rebuilds (``genome merge`` → ``genome annotate
    align-tier3-consensus`` → ``genome annotate refresh-index``) must run
    next to bring the database to a fully coherent state. See finding-020.
    """
    from genome.annotate.canonicalize import (  # noqa: PLC0415
        DbsnpNotLoadedError,
        DerivedTablesNotEmptyError,
        canonicalize_variants,
    )

    try:
        result = canonicalize_variants(force=force, no_backup=no_backup)
    except DbsnpNotLoadedError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except DerivedTablesNotEmptyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(
        f"variants_master canonicalized: "
        f"source_version_id={result.dbsnp_source_version_id} "
        f"already_canonical={result.already_canonical} "
        f"rows_reoriented={result.rows_reoriented} "
        f"rows_recovered_hom_ref={result.rows_recovered_hom_ref} "
        f"rows_recovered_hom_ref_multialt={result.rows_recovered_hom_ref_multialt} "
        f"rows_recovered_hom_alt={result.rows_recovered_hom_alt} "
        f"rows_collapsed={result.rows_collapsed} "
        f"calls_repointed={result.calls_repointed} "
        f"new_variant_ids_allocated={result.new_variant_ids_allocated} "
        f"survivors_flag_updated={result.survivors_flag_updated} "
        f"survivors_enriched={result.survivors_enriched} "
        f"rsid_conflicts={result.rsid_conflicts} "
        f"genuine_variants_after={result.genuine_variants_after} "
        f"hom_ref_remaining={result.hom_ref_remaining} "
        f"backup={result.backup_path or 'skipped'}",
    )


@annotate_app.command("align-tier3-consensus")
def annotate_align_tier3_consensus() -> None:
    """Delete non-canonical-side consensus rows for tier-3 strand-flip pairs.

    Companion to ``canonicalize-variants``: after ``genome merge`` rebuilds
    ``consensus_genotypes``, this command identifies pairs of
    ``variants_master`` rows at the same ``(chrom, pos_grch38)`` whose
    consensus is ``disagreement_resolved`` (the merge-tier-3 shape) and
    determines which side matches a dbSNP 4-tuple — the canonical side. The
    non-canonical-side ``consensus_genotypes`` row is DELETEd so Phase 6
    pipelines see exactly one ``variant_id`` per real biallelic site, with
    annotations aligned. Run between ``genome merge`` and ``genome annotate
    refresh-index``. See PR-3 Q1 alignment refinement.
    """
    from genome.annotate.align_tier3 import (  # noqa: PLC0415
        DbsnpNotLoadedError,
        align_tier3_consensus,
    )

    try:
        result = align_tier3_consensus()
    except DbsnpNotLoadedError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(
        f"tier-3 consensus aligned: "
        f"source_version_id={result.dbsnp_source_version_id} "
        f"pairs_examined={result.pairs_examined} "
        f"rows_deleted={result.rows_deleted}",
    )


@annotate_app.command("collapse-duplicate-variants")
def annotate_collapse_duplicate_variants(
    dry_run: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Identify the actionable duplicate edges and print the per-mechanism "
                "breakdown WITHOUT mutating. Run this first and confirm it reports the "
                "expected counts (and zero genotype_mismatch / source_collision) "
                "before the real collapse."
            ),
        ),
    ] = False,
    force: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--force",
            help=(
                "Proceed (snapshot + clear the regenerated downstream rollups) "
                "even when nothing is actionable. Mirrors canonicalize-variants' "
                "--force; the collapse itself only runs when an edge is found."
            ),
        ),
    ] = False,
    no_backup: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--no-backup",
            help=(
                "Skip the pre-mutation genome.duckdb snapshot. The snapshot is the "
                "rollback path; only skip it when an existing snapshot is already "
                "in archive/strand-collapse/."
            ),
        ),
    ] = False,
) -> None:
    """Collapse same-SNP duplicate ``variants_master`` rows (closes finding-005 #1).

    Each physical SNP stored as ≥2 rows at one ``(chrom, pos)`` — a no-call ``(N,N)``
    placeholder, a REF/ALT swap, a strand-flip, or a hom opposite/same-strand row —
    is collapsed onto one survivor (repoint / complement via row-grain supersession /
    drop), while legit multi-allelic alts are protected. dbSNP must be loaded. Depends
    on the PR-5b-pre consensus_v1 chip-no-call fix. Run in the reload sequence:
    ``canonicalize-variants`` → ``collapse-duplicate-variants`` → ``genome merge`` →
    ``align-tier3-consensus`` (now a no-op) → ``refresh-index``. See finding-005 #1 +
    finding-026/027.
    """
    from genome.annotate.strand_collapse import (  # noqa: PLC0415
        DbsnpNotLoadedError,
        collapse_duplicate_variants,
    )

    try:
        result = collapse_duplicate_variants(dry_run=dry_run, force=force, no_backup=no_backup)
    except DbsnpNotLoadedError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    prefix = "[dry-run] " if result.dry_run else ""
    typer.echo(
        f"{prefix}duplicate collapse: "
        f"source_version_id={result.dbsnp_source_version_id} "
        f"actionable_edges={result.actionable_edges} "
        f"no_call_repointed={result.no_call_repointed} "
        f"no_call_dropped={result.no_call_dropped} "
        f"swaps={result.swaps_collapsed} "
        f"strandflips={result.strandflips_collapsed} "
        f"hom_opp={result.hom_opp_collapsed} "
        f"hom_same={result.hom_same_collapsed} "
        f"legit_multiallelic_skipped={result.legit_multiallelic_skipped} "
        f"genotype_mismatch_skipped={result.genotype_mismatch_skipped} "
        f"source_collision_skipped={result.source_collision_skipped} "
        f"palindromic_skipped={result.palindromic_skipped} "
        f"degenerate_skipped={result.degenerate_skipped}",
    )
    if result.dry_run:
        for edge in result.edges:
            typer.echo(
                f"  survivor={edge.survivor_id} ({edge.survivor_ref}/{edge.survivor_alt}) "
                f"mechanism={edge.mechanism} dead={list(edge.dead_variant_ids)} "
                f"dead_rsids={list(edge.dead_rsids)} "
                f"calls_to_complement={edge.calls_complemented}",
            )
        return

    typer.echo(
        f"  calls_complemented={result.calls_complemented} "
        f"calls_repointed={result.calls_repointed} "
        f"variants_master_deleted={result.variants_master_deleted} "
        f"rsid_coalesced={result.rsid_coalesced} "
        f"rsid_conflicts={result.rsid_conflicts} "
        f"backup={result.backup_path or 'skipped'}",
    )


@annotate_app.command("seed-genes")
def annotate_seed_genes(
    force: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--force",
            help=(
                "Re-seed even when genes is already populated: DELETE FROM genes "
                "and re-INSERT under a fresh source_version_id, in one transaction. "
                "Refuses (raises) if any of the five FK dependents (the four "
                "derived_* tables or pathway_genes) still reference genes."
            ),
        ),
    ] = False,
) -> None:
    """Seed ``genes`` with the FK-satisfying gene-symbol subset (PR 6).

    Writes the set-union of the ACMG SF v3.3 secondary-findings panel (84 genes)
    and the gene symbols the currently-active CPIC + PharmGKB tables carry, under
    a freshly-allocated ``hgnc`` ``annotation_source_versions`` row (decision #8).
    This satisfies the ``REFERENCES genes(gene_symbol)`` FK that four Phase-6
    ``derived_*`` tables plus ``pathway_genes`` carry, unblocking Phase 6. The
    ``annotation_sources`` pointer is NOT flipped (genes is a one-time static
    seed, not an evolving source). Full genes/traits/pathways dictionaries remain
    deferred to Phase 7.
    """
    from genome.annotate.seed_genes import seed_genes  # noqa: PLC0415

    result = seed_genes(force=force)
    typer.echo(
        f"genes seeded: source_version_id={result.source_version_id} "
        f"already_populated={result.already_populated} "
        f"genes_rows={result.genes_rows} "
        f"acmg_sf_genes={result.acmg_sf_genes} "
        f"pgx_genes={result.pgx_genes} "
        f"cpic_covered={result.cpic_covered} "
        f"pharmgkb_covered={result.pharmgkb_covered}",
    )


@annotate_app.command("purge-superseded")
def annotate_purge_superseded(
    execute: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--execute",
            help=(
                "Actually delete the superseded rows. WITHOUT this flag the command "
                "is a read-only dry-run that prints the per-source partition and "
                "mutates nothing. --execute proceeds when the mandatory probe surfaces "
                "real work — a deletable version OR a zero-data unreferenced registry "
                "orphan to self-heal. Under the default --keep 1 it is a no-op on the "
                "current corpus (the single prior is protected) — corpus-conditional, "
                "not structural: it will still snapshot and sweep an orphan if one exists."
            ),
        ),
    ] = False,
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            help=(
                "Narrow the purge to one pointer-bearing source (clinvar, "
                "gwas_catalog, pharmgkb, cpic, gnomad, dbsnp, pgs_catalog). Default: "
                "all seven. dbsnp covers dbsnp_annotations + variant_aliases as a unit."
            ),
        ),
    ] = None,
    keep: Annotated[
        int,
        typer.Option(
            "--keep",
            help=(
                "How many NON-active prior versions to retain per source (most-recent "
                "first). 1 (default) keeps the single prior; 0 reclaims every "
                "superseded version. The active build (the annotation_sources pointer) "
                "is always kept regardless of --keep."
            ),
        ),
    ] = 1,
    no_backup: Annotated[  # noqa: FBT002 — typer boolean flag, opt-in
        bool,
        typer.Option(
            "--no-backup",
            help=(
                "Skip the pre-mutation genome.duckdb snapshot (only taken on --execute "
                "with a non-empty deletable set). The snapshot is the sole hard-recovery "
                "path; only skip it when one already exists in archive/purge/."
            ),
        ),
    ] = False,
) -> None:
    """Purge superseded annotation rows, FK-safe, with the active build protected (PR 9).

    Per supersedable source (the seven with an ``annotation_sources`` pointer) the
    ``(active, prior, deletable)`` partition is re-derived at runtime from the
    pointer — never a hardcoded ``source_version_id`` (the PR-7 trap, finding-015).
    Dry-run by default; ``--execute`` deletes the deletable rows in two
    FK-safe transactions (data, then the guarded registry row) after a pre-mutation
    snapshot. The active build is structurally undeletable. See finding-010 #14.
    """
    from genome.annotate.purge import PurgeError, purge_superseded  # noqa: PLC0415

    try:
        result = purge_superseded(
            execute=execute,
            source=source,
            keep=keep,
            no_backup=no_backup,
        )
    except (ValueError, PurgeError) as exc:
        # PurgeError covers every fail-closed abort (RegistryStillReferenced, Dangling/Ambiguous
        # Partition, ActiveBuildAtRisk, NegativeControl) — surface as clean stderr + exit 2, not a
        # raw traceback. ValueError is an unknown --source.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    if result.executed:
        mode = "executed"
    elif execute:
        mode = "nothing-to-delete"
    else:
        mode = "dry-run"
    typer.echo(
        f"purge-superseded [{mode}]: "
        f"data_rows_deleted={result.data_rows_deleted} "
        f"registry_rows_deleted={result.registry_rows_deleted} "
        f"orphan_rows_swept={result.orphan_rows_swept}",
    )
    for plan in result.plans:
        typer.echo(
            f"  {plan.source_db}: active={plan.active_id} prior={plan.prior_id} "
            f"deletable={list(plan.deletable_ids)}",
        )
    if result.executed:
        typer.echo(
            f"  negative_control_ok={result.negative_control_ok} "
            f"active_rows_unchanged={result.active_rows_unchanged} "
            f"pointer_unchanged={result.pointer_unchanged} "
            f"backup={result.backup_path or 'skipped'}",
        )


__all__ = [
    "annotate_app",
]
