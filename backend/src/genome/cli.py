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
from genome.imputation import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_R2_THRESHOLD,
    PANEL_CHROMOSOMES,
    DryRunResult,
    ReferencePanel,
    import_result,
    install_panel,
    list_runs,
    parse_chromosomes_filter,
    prepare_run,
    validate_panel,
)
from genome.ingest import Source, ingest_file
from genome.ingest.liftover import LiftoverEngine
from genome.merge import merge_all
from genome.privacy.external_client import is_external_enabled, write_config_change_audit

_VALID_INGEST_SOURCES: tuple[str, ...] = tuple(s for s in get_args(Source) if s != "topmed_imputed")
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


@imputation_app.command("prepare")
def imputation_prepare(
    sample_id: Annotated[
        str,
        typer.Option(
            "--sample-id",
            help="Sample name for the VCF sample column. Used by TopMed as a label.",
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
) -> None:
    """Export the merged consensus genotype set as per-chromosome VCFs.

    Writes files under archive/imputation/run_<id>/upload/. After this completes,
    the user uploads the files to TopMed's web UI (the runbook describes the form
    fields). Polling and download are subsequent commands.
    """
    result = prepare_run(sample_id=sample_id, force_new=force_new)
    typer.echo(
        f"imputation_id={result.imputation_id} "
        f"variants={result.variants_total} "
        f"chroms_exported={sorted(result.variants_per_chrom)} "
        f"upload_dir={result.archive.upload_dir}",
    )
    typer.echo(
        "Next step: upload the per-chromosome VCFs to the TopMed Imputation Server.",
    )
    typer.echo(
        "See docs/runbooks/imputation.md for the exact web-UI form fields.",
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
    decrypted TopMed output) and writes ``genotype_calls`` rows with
    ``source='topmed_imputed'``, plus a fresh ``ingestion_runs`` row and
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
