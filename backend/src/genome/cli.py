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
from genome.annotate import annotate_app
from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.db.init_schema import init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.imputation import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_R2_THRESHOLD,
    PANEL_CHROMOSOMES,
    DryRunResult,
    ReferencePanel,
    import_result,
    install_panel,
    list_runs,
    normalize_imputed_rsids,
    parse_chromosomes_filter,
    prepare_run,
    run_imputation,
    validate_panel,
)
from genome.imputation.archive import ImputationArchive
from genome.imputation.beagle_runner import DEFAULT_MEMORY_GB, DEFAULT_NE
from genome.imputation.chrx_panel import ChrxToolingError, prepare_chrx_panel
from genome.ingest import Source, ingest_file
from genome.ingest.liftover import LiftoverEngine
from genome.merge import merge_all
from genome.privacy.external_client import is_external_enabled, write_config_change_audit

_VALID_INGEST_SOURCES: tuple[str, ...] = tuple(
    s for s in get_args(Source) if s not in {"topmed_imputed", "beagle_imputed"}
)
_VALID_LIFTOVER_ENGINES: tuple[str, ...] = tuple(get_args(LiftoverEngine))

# Allowed value_types for `genome config set` — must stay in sync with the
# CHECK constraint on user_preferences.value_type.
_VALID_PREF_VALUE_TYPES: tuple[str, ...] = ("string", "number", "boolean", "json")

app = typer.Typer(no_args_is_help=True, add_completion=False, help="DNA insights CLI")
imputation_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Run the merged genotype set through imputation. The workflow is "
        "prepare → run → import; see docs/runbooks/imputation.md."
    ),
)
config_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Read and write user_preferences from the CLI.",
)
panel_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Manage the local Beagle reference panel (Beagle JAR, PLINK GRCh38 "
        "genetic map, and per-chromosome 1000 Genomes Phase 3 panel VCFs)."
    ),
)
app.add_typer(imputation_app, name="imputation")
app.add_typer(config_app, name="config")
app.add_typer(annotate_app, name="annotate")
imputation_app.add_typer(panel_app, name="panel")


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


# -----------------------------------------------------------------------------
# `genome config` — read / write user_preferences
# -----------------------------------------------------------------------------


@config_app.command("get")
def config_get(
    key: Annotated[str, typer.Argument(help="Preference key, e.g. 'external_calls_enabled'.")],
) -> None:
    """Print the current value of one user_preferences key.

    Exit code is 0 even when the key is missing; the output line distinguishes
    "missing" from a present-but-empty value. Use this to script preference
    inspection.
    """
    with sqlcipher_connection() as conn:
        row = conn.execute(
            "SELECT pref_value, value_type FROM user_preferences WHERE pref_key = ?",
            (key,),
        ).fetchone()
    if row is None:
        typer.echo(f"{key}: <not set>")
        return
    typer.echo(f"{key}: {row[0]} (value_type={row[1]})")


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Preference key.")],
    value: Annotated[str, typer.Argument(help="New value, as a string. See --value-type.")],
    value_type: Annotated[
        str,
        typer.Option(
            "--value-type",
            help=(
                "Pref's value_type. Required only when creating a new key. "
                "One of: 'string', 'number', 'boolean', 'json'."
            ),
        ),
    ] = "",
) -> None:
    """Insert or update a user_preferences row.

    Every change writes a ``config_change`` row to ``audit_log`` so the
    history of preference changes is auditable.

    The most common use of this command is enabling external calls before
    Phase 4 imputation::

        genome config set external_calls_enabled true
    """
    with sqlcipher_connection() as conn:
        existing = conn.execute(
            "SELECT pref_value, value_type FROM user_preferences WHERE pref_key = ?",
            (key,),
        ).fetchone()
        if existing is None:
            if not value_type:
                msg = (
                    f"key {key!r} does not exist; pass --value-type to create it "
                    f"(one of: {list(_VALID_PREF_VALUE_TYPES)})"
                )
                raise typer.BadParameter(msg)
            if value_type not in _VALID_PREF_VALUE_TYPES:
                msg = (
                    f"invalid --value-type {value_type!r}; "
                    f"expected one of {list(_VALID_PREF_VALUE_TYPES)}"
                )
                raise typer.BadParameter(msg)
            conn.execute(
                "INSERT INTO user_preferences (pref_key, pref_value, value_type) VALUES (?, ?, ?)",
                (key, value, value_type),
            )
            old_value: str | None = None
        else:
            old_value = str(existing[0])
            conn.execute(
                "UPDATE user_preferences SET pref_value = ? WHERE pref_key = ?",
                (value, key),
            )
        conn.commit()

    write_config_change_audit(pref_key=key, old_value=old_value, new_value=value)
    typer.echo(f"{key}: {value} (was: {old_value if old_value is not None else '<not set>'})")


# -----------------------------------------------------------------------------
# `genome imputation`
# -----------------------------------------------------------------------------


_SEX_FLAG_HELP = (
    "Profile sex for the chrX path: 'M', 'F', or 'auto' (default — resolve from "
    "the chip sample_qc rows). Drives only the transient prepare/run/import-QC "
    "path; no DB column is written (PR 5a)."
)


def _normalize_sex_flag(raw: str) -> str | None:
    """Validate a ``--sex`` value; return ``'M'`` / ``'F'``, or ``None`` for ``auto``."""
    value = raw.strip()
    if value.lower() == "auto":
        return None
    if value.upper() in {"M", "F"}:
        return value.upper()
    msg = "--sex must be one of: M, F, auto"
    raise typer.BadParameter(msg, param_hint="--sex")


def _gate_chrx_sex(chromosomes_set: frozenset[str] | None, sex: str) -> None:
    """Require a determinate profile sex when chrX is in the run scope (PR 5a).

    chrX imputation needs a known sex to correct male non-PAR dosage downstream.
    ``auto`` resolves it from chip QC; an ambiguous result there aborts with a
    request for an explicit ``--sex``. Autosome-only runs never consult it.
    """
    includes_x = chromosomes_set is None or "X" in chromosomes_set
    if not includes_x:
        return
    from genome.imputation.sex import AmbiguousSexError, resolve_sex  # noqa: PLC0415

    explicit = _normalize_sex_flag(sex)
    try:
        with duckdb_connection() as conn:
            resolved = resolve_sex(conn, explicit)
    except AmbiguousSexError as exc:
        raise typer.BadParameter(str(exc), param_hint="--sex") from exc
    typer.echo(f"chrX imputation profile sex: {resolved}")


def _require_chrx_region_panels(chromosomes_set: frozenset[str] | None) -> None:
    """Abort a chrX run if the chrX region panel subsets haven't been prepared (PR 5a).

    M3-physical needs the three native panel subsets (PAR1 / non-PAR / PAR2) from
    ``panel prepare-chrx``; without them the chrX region runs have no ``ref=``.
    Surface that as a friendly pre-flight when chrX is in scope (finding-029).
    """
    includes_x = chromosomes_set is None or "X" in chromosomes_set
    if not includes_x:
        return
    panel = ReferencePanel.resolve()
    region_panels = (panel.chrx_par1_panel, panel.chrx_nonpar_panel, panel.chrx_par2_panel)
    if not all(p.is_file() for p in region_panels):
        msg = (
            "chrX is in scope but the chrX region reference panels are missing. "
            "Run `genome imputation panel prepare-chrx` first (M3-physical — finding-029)."
        )
        typer.echo(msg, err=True)
        raise typer.Exit(code=1)


def _chrx_in_run_scope(imputation_id: int, chromosomes_set: frozenset[str] | None) -> bool:
    """True iff chrX will actually be imputed in this run (PR 5a).

    chrX runs only when it is in the chromosome scope AND a chrX upload VCF was
    prepared for this run. An autosome-only corpus (no chrX upload) never trips
    the chrX preconditions, even on a full run, so the chrX gates fire exactly
    when chrX is genuinely about to be imputed.
    """
    if chromosomes_set is not None and "X" not in chromosomes_set:
        return False
    archive = ImputationArchive.for_run(get_settings().archive_path, imputation_id)
    # M3-physical: chrX is prepared as region targets under chrX_regions/, not a
    # top-level upload/chrX.vcf.gz. The non-PAR region is always present when chrX
    # has any exportable rows, so it is the presence signal (PR 5a).
    return archive.chrx_region_upload_path("nonpar").is_file()


@imputation_app.command("prepare")
def imputation_prepare(
    sample_id: Annotated[
        str,
        typer.Option(
            "--sample-id",
            help="Sample name for the VCF sample column.",
        ),
    ] = "sample",
    force_new: Annotated[  # noqa: FBT002 — typer boolean flag, --force-new is opt-in
        bool,
        typer.Option(
            "--force-new",
            help=(
                "Create a new imputation run even if one is already pending/processing. "
                "Use this only when intentionally starting over (e.g. the previous run "
                "was abandoned)."
            ),
        ),
    ] = False,
    sex: Annotated[
        str,
        typer.Option("--sex", help=_SEX_FLAG_HELP),
    ] = "auto",
) -> None:
    """Export the merged consensus genotype set as per-chromosome VCFs.

    Writes files under archive/imputation/run_<id>/upload/. After this
    completes, run ``genome imputation run <id>`` to pipe those VCFs
    through local Beagle 5.5 against the 1000 Genomes Phase 3 reference
    panel, then ``genome imputation import <id>`` to ingest the imputed
    output. See ``docs/runbooks/imputation.md`` for the end-to-end flow.
    """
    result = prepare_run(sample_id=sample_id, force_new=force_new, sex=_normalize_sex_flag(sex))
    typer.echo(
        f"imputation_id={result.imputation_id} "
        f"variants={result.variants_total} "
        f"profile_sex={result.profile_sex} "
        f"chroms_exported={sorted(result.variants_per_chrom)} "
        f"upload_dir={result.archive.upload_dir}",
    )
    typer.echo(
        f"Next step: run `genome imputation run {result.imputation_id}` "
        f"to pipe these VCFs through local Beagle 5.5.",
    )
    typer.echo(
        "See docs/runbooks/imputation.md for the prepare → run → import flow.",
    )


@imputation_app.command("import")
def imputation_import(  # noqa: PLR0913 — one CLI flag per operational control
    imputation_id: Annotated[int, typer.Argument(help="Run ID.")],
    r2_threshold: Annotated[
        float,
        typer.Option(
            "--r2-threshold",
            min=0.0,
            max=1.0,
            help=(
                "Variants with INFO/R2 below this threshold are skipped at import "
                "time and never written to genotype_calls. Recorded on the run's "
                "imputation_runs.r2_threshold column."
            ),
        ),
    ] = DEFAULT_R2_THRESHOLD,
    chromosomes: Annotated[
        str | None,
        typer.Option(
            "--chromosomes",
            help=(
                "Comma-separated chromosome list (e.g. '1,2,X'). When set, only "
                "matching per-chromosome VCFs are processed. Useful for partial "
                "recovery or testing."
            ),
        ),
    ] = None,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            min=1,
            help=(
                "Rows per Arrow Table bulk-insert batch. Lower this on memory-constrained machines."
            ),
        ),
    ] = DEFAULT_BATCH_SIZE,
    dry_run: Annotated[  # noqa: FBT002 — typer boolean flag, --dry-run is opt-in
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Parse each VCF and report expected variant counts plus an "
                "estimated wall-clock time, without writing anything to the "
                "database."
            ),
        ),
    ] = False,
    force_reimport: Annotated[  # noqa: FBT002 — typer boolean flag, --force-reimport is opt-in
        bool,
        typer.Option(
            "--force-reimport",
            help=(
                "Required to re-run import on a run whose variants_output is "
                "already populated. The prior imputed calls are deactivated via "
                "the existing supersession pattern."
            ),
        ),
    ] = False,
) -> None:
    """Stream the imputed VCFs into the database.

    Reads archive/imputation/run_<id>/result/chr*.dose.vcf.gz files (the
    Beagle 5.5 output) and writes ``genotype_calls`` rows with
    ``source='beagle_imputed'``, plus a fresh ``ingestion_runs`` row and
    ``sample_qc`` row.

    Idempotent: re-importing supersedes prior imputed calls for the same
    positions rather than duplicating.
    """
    try:
        chromosomes_set = parse_chromosomes_filter(chromosomes)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--chromosomes") from exc

    result = import_result(
        imputation_id,
        r2_threshold=r2_threshold,
        chromosomes=chromosomes_set,
        batch_size=batch_size,
        dry_run=dry_run,
        force_reimport=force_reimport,
    )
    if isinstance(result, DryRunResult):
        est = result.estimated_seconds
        typer.echo(
            f"[dry-run] imputation_id={result.imputation_id} "
            f"r2_threshold={result.r2_threshold:.4f} "
            f"chromosomes={list(result.chromosomes_planned)} "
            f"variants_total={result.variants_total} "
            f"variants_below_threshold={result.variants_below_threshold} "
            f"estimated_seconds={est:.1f}",
        )
        if result.per_chrom:
            per_chrom = " ".join(f"{k}={v}" for k, v in sorted(result.per_chrom.items()))
            typer.echo(f"[dry-run] per_chrom: {per_chrom}")
        typer.echo(
            "[dry-run] No database writes happened. Re-run without --dry-run to import.",
        )
        return

    mean_r2 = f"{result.mean_r2:.4f}" if result.mean_r2 is not None else "-"
    typer.echo(
        f"imputation_id={result.imputation_id} ingestion_run_id={result.ingestion_run_id} "
        f"qc_id={result.qc_id} variants={result.variants_total} "
        f"called={result.variants_called} no_call={result.variants_no_call} "
        f"below_threshold={result.variants_below_threshold} "
        f"new_master_rows={result.new_variants_master_rows} "
        f"deactivated_prior={result.deactivated_prior_calls} "
        f"r2_threshold={result.r2_threshold:.4f} "
        f"chromosomes={list(result.chromosomes_imported)} "
        f"mean_r2={mean_r2} "
        f"r2_above_0.3={result.variants_above_r2_0_3} "
        f"r2_above_0.8={result.variants_above_r2_0_8}",
    )
    typer.echo(
        "Next step: re-run `genome merge` to refresh consensus across all three sources.",
    )


_VALID_RUN_CHROMS: tuple[str, ...] = (*(str(i) for i in range(1, 23)), "X", "Y")


def _parse_run_chromosomes(raw: str | None) -> frozenset[str] | None:
    """Parse a ``--chromosomes`` value for ``imputation run``.

    Accepts the same shape as the import command's filter (comma-separated,
    optional ``chr`` prefix, case-insensitive) but the valid set is the
    autosomes plus X and Y. chrY may be present in the upload set even
    though the panel doesn't cover it — the runner skips it with a
    warning at run time.
    """
    if raw is None:
        return None
    tokens = [t.strip().upper().removeprefix("CHR") for t in raw.split(",") if t.strip()]
    if not tokens:
        msg = "chromosome filter is empty after parsing; pass at least one chromosome"
        raise ValueError(msg)
    bad = sorted({t for t in tokens if t not in _VALID_RUN_CHROMS})
    if bad:
        msg = (
            f"invalid chromosome(s) {bad!r}; valid chromosomes are "
            f"{sorted(_VALID_RUN_CHROMS, key=lambda c: (0, int(c)) if c.isdigit() else (1, c))}"
        )
        raise ValueError(msg)
    return frozenset(tokens)


@imputation_app.command("run")
def imputation_run(  # noqa: PLR0913 — one CLI flag per operational control
    imputation_id: Annotated[int, typer.Argument(help="Run ID.")],
    chromosomes: Annotated[
        str | None,
        typer.Option(
            "--chromosomes",
            help=(
                "Comma-separated chromosome list (e.g. '1,2,X'). When set, only "
                "matching per-chromosome upload VCFs are run through Beagle. "
                "Useful for partial recovery, retrying failures, or testing."
            ),
        ),
    ] = None,
    threads: Annotated[
        int | None,
        typer.Option(
            "--threads",
            min=1,
            help=(
                "Number of threads Beagle should use (passed as ``nthreads=``). "
                "Defaults to max(1, os.cpu_count() - 1) so one core stays free "
                "for the OS / shell."
            ),
        ),
    ] = None,
    memory_gb: Annotated[
        int,
        typer.Option(
            "--memory-gb",
            min=1,
            help=(
                "Java heap size in GB (Beagle's ``-Xmx``). Default 8 GB suits "
                "most chromosomes; chr1 / chr2 may need 12-16 GB on dense panels."
            ),
        ),
    ] = DEFAULT_MEMORY_GB,
    ne: Annotated[
        int,
        typer.Option(
            "--ne",
            min=1,
            help=(
                "Effective population size (Beagle's ``ne=``). Default 1,000,000 "
                "matches Beagle's documented default for outbred human populations."
            ),
        ),
    ] = DEFAULT_NE,
    force: Annotated[  # noqa: FBT002 — typer boolean flag, --force is opt-in
        bool,
        typer.Option(
            "--force",
            help=(
                "Re-run every chromosome even if its output VCF already exists "
                "and parses cleanly. By default, finished chromosomes are skipped "
                "to make the runner resumable."
            ),
        ),
    ] = False,
    sex: Annotated[
        str,
        typer.Option("--sex", help=_SEX_FLAG_HELP),
    ] = "auto",
) -> None:
    """Run Beagle 5.5 against the prepared upload VCFs for one run.

    Reads ``archive/imputation/run_<id>/upload/chr*.vcf.gz`` and writes
    ``archive/imputation/run_<id>/result/chr<N>.vcf.gz`` per chromosome.
    Imputation runs one chromosome at a time as separate ``java -jar``
    invocations so a failed chromosome can be retried independently —
    pass ``--chromosomes`` to limit the retry set.

    Status transitions: ``pending`` → ``processing`` when the first
    chromosome starts; ``completed`` if every attempted chromosome
    succeeds; ``failed`` if every attempted chromosome fails. Mixed
    success leaves the status at ``processing`` so the user can retry
    the failures without losing the successes.
    """
    try:
        chromosomes_set = _parse_run_chromosomes(chromosomes)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--chromosomes") from exc

    # chrX imputation needs a determinate profile sex and the region panel subsets.
    # Gate both before the long run — but only when chrX will actually run (in
    # scope AND chrX region targets exist), so autosome-only runs are unaffected.
    if _chrx_in_run_scope(imputation_id, chromosomes_set):
        _gate_chrx_sex(chromosomes_set, sex)
        _require_chrx_region_panels(chromosomes_set)

    result = run_imputation(
        imputation_id,
        chromosomes=chromosomes_set,
        threads=threads,
        memory_gb=memory_gb,
        ne=ne,
        force=force,
    )

    per_chrom = " ".join(f"{k}={v:.1f}s" for k, v in sorted(result.per_chrom_seconds.items()))
    typer.echo(
        f"imputation_id={result.imputation_id} "
        f"attempted={list(result.chromosomes_attempted)} "
        f"completed={list(result.chromosomes_completed)} "
        f"failed={list(result.chromosomes_failed)} "
        f"skipped={list(result.chromosomes_skipped)}",
    )
    if per_chrom:
        typer.echo(f"per_chrom_seconds: {per_chrom}")
    if result.chromosomes_failed:
        typer.echo(
            "Some chromosomes failed; review the structlog output above. "
            f"Re-run with `--chromosomes {','.join(result.chromosomes_failed)}` "
            "after addressing the failure.",
        )
    elif result.chromosomes_completed:
        typer.echo(
            "Next step: `genome imputation import <id>` to load the imputed "
            "VCFs into genotype_calls and variants_master.",
        )


@imputation_app.command("list")
def imputation_list() -> None:
    """Show all imputation_runs rows with current status, timing, and key stats."""
    runs = list_runs()
    if not runs:
        typer.echo("(no imputation runs yet — start with `genome imputation prepare`)")
        return
    for r in runs:
        typer.echo(
            f"#{r.imputation_id:04d} "
            f"status={r.status} "
            f"server={r.imputation_server} panel={r.reference_panel or '-'} "
            f"submitted={r.submitted_at or '-'} "
            f"completed={r.completed_at or '-'} "
            f"variants_in={r.variants_input if r.variants_input is not None else '-'} "
            f"variants_out={r.variants_output if r.variants_output is not None else '-'} "
            f"mean_r2={r.mean_r2 if r.mean_r2 is not None else '-'} "
            f"r2_threshold={r.r2_threshold if r.r2_threshold is not None else '-'}",
        )


@imputation_app.command("normalize-rsids")
def imputation_normalize_rsids() -> None:
    """NULL synthetic ``chrom:pos:ref:alt`` strings in ``variants_master.rsid``.

    One-time, idempotent remediation (finding-021) of imputed variants whose rsid
    carries a Beagle coordinate string instead of a real dbSNP ``rs#`` or NULL.
    Real ``rs#`` and chip-internal ``i####`` IDs are left untouched; the sweep is
    positively scoped to the synthetic format and logs any leftover non-synthetic
    IDs (e.g. chip-probe ``kgp…`` / ``acom…`` names) for visibility rather than
    aborting.
    """
    with duckdb_connection() as conn:
        cleaned = normalize_imputed_rsids(conn)
    typer.echo(f"normalized {cleaned} synthetic rsid(s) to NULL")


# -----------------------------------------------------------------------------
# `genome imputation panel` — manage the local Beagle reference panel
# -----------------------------------------------------------------------------


def _parse_panel_chromosomes(raw: str | None) -> frozenset[str] | None:
    """Parse a ``--chromosomes`` value against :data:`PANEL_CHROMOSOMES`.

    Mirrors :func:`parse_chromosomes_filter` but accepts only chromosomes
    that exist in the reference panel (no chrY, no chrMT).
    """
    if raw is None:
        return None
    tokens = [t.strip().upper().removeprefix("CHR") for t in raw.split(",") if t.strip()]
    if not tokens:
        msg = "chromosome filter is empty after parsing; pass at least one chromosome"
        raise ValueError(msg)
    bad = sorted({t for t in tokens if t not in PANEL_CHROMOSOMES})
    if bad:
        msg = (
            f"invalid chromosome(s) {bad!r}; valid panel chromosomes are "
            f"{sorted(PANEL_CHROMOSOMES, key=lambda c: (0, int(c)) if c.isdigit() else (1, c))}"
        )
        raise ValueError(msg)
    return frozenset(tokens)


@panel_app.command("status")
def panel_status() -> None:
    """Report whether all reference-panel artifacts are present on disk.

    Prints the resolved panel root, then either ``all components present``
    or one ``- <problem>`` line per missing artifact. Exit code is 0 either
    way; the user reads the output to decide whether to run ``install``.
    """
    panel = ReferencePanel.resolve()
    typer.echo(f"panel_root: {panel.root}")
    problems = validate_panel(panel)
    if not problems:
        typer.echo("all components present")
        return
    typer.echo(f"missing {len(problems)} component(s):")
    for p in problems:
        typer.echo(f"  - {p}")


@panel_app.command("install")
def panel_install(
    force: Annotated[  # noqa: FBT002 — typer boolean flag, --force is opt-in
        bool,
        typer.Option(
            "--force",
            help=(
                "Re-download every selected artifact even if it is already "
                "on disk. By default, existing files are left alone."
            ),
        ),
    ] = False,
    chromosomes: Annotated[
        str | None,
        typer.Option(
            "--chromosomes",
            help=(
                "Comma-separated chromosome list (e.g. '1,22,X'). When set, "
                "only the matching per-chromosome panel VCFs are downloaded; "
                "the Beagle JAR and the genetic-map archive are left alone."
            ),
        ),
    ] = None,
) -> None:
    """Download missing reference-panel artifacts via the audited HTTP client.

    Requires ``user_preferences.external_calls_enabled = true``. When the
    master switch is off the command aborts immediately with an actionable
    error message — the same one any external call would produce.
    """
    try:
        chromosomes_set = _parse_panel_chromosomes(chromosomes)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--chromosomes") from exc

    if not is_external_enabled():
        msg = (
            "External calls are disabled; cannot download the reference panel. "
            "Enable them with `genome config set external_calls_enabled true` "
            "and re-run."
        )
        typer.echo(msg, err=True)
        raise typer.Exit(code=1)

    panel = ReferencePanel.resolve()
    install_panel(panel, force=force, chromosomes=chromosomes_set)
    typer.echo(f"panel_root: {panel.root}")
    problems = validate_panel(panel)
    if not problems:
        typer.echo("all components present")
    else:
        typer.echo(f"after install, {len(problems)} component(s) still missing:")
        for p in problems:
            typer.echo(f"  - {p}")


@panel_app.command("prepare-chrx")
def panel_prepare_chrx(
    force: Annotated[  # noqa: FBT002 — typer boolean flag, --force is opt-in
        bool,
        typer.Option(
            "--force",
            help="Rebuild the chrX region subsets even if they already exist.",
        ),
    ] = False,
) -> None:
    """Split the chrX reference panel into native region subsets for Beagle (M3 — finding-029).

    Emits three native (un-diploidized) subsets of the installed 1000G chrX panel
    — ``chrX.par1.vcf.gz`` / ``chrX.nonpar.vcf.gz`` / ``chrX.par2.vcf.gz`` — via
    ``bcftools view -r``, so each region loads into Beagle 5.5 with the
    biologically-correct ploidy (male non-PAR stays haploid). Required before
    ``genome imputation run`` with chrX in scope; one-time and idempotent. Ensures
    the panel ``.tbi`` first and asserts each subset's ploidy composition.
    Requires ``bcftools`` on PATH.
    """
    panel = ReferencePanel.resolve()
    try:
        result = prepare_chrx_panel(panel, force=force)
    except ChrxToolingError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if result.skipped:
        typer.echo(
            f"chrX region panels already present: {result.par1_path.name}, "
            f"{result.nonpar_path.name}, {result.par2_path.name} "
            "(pass --force to rebuild)",
        )
        return
    typer.echo(
        f"chrX panel split into region subsets beside {result.par1_path.parent}: "
        f"{result.par1_path.name}, {result.nonpar_path.name}, {result.par2_path.name} "
        f"(non-PAR haploid GTs={result.nonpar_haploid_gts})",
    )
    typer.echo(
        "Next: `genome imputation run <id> --chromosomes X` (or a full run including X).",
    )


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
