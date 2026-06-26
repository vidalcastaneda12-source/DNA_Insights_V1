"""CLI smoke tests for the Phase 4 surface.

Verifies that ``genome config`` and ``genome imputation`` are wired correctly
into the Typer app and that their ``--help`` output exists. Functional
behavior is covered by the module-specific test files; this file only
asserts the CLI plumbing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.cli import app
from genome.db import duckdb_connection, init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.privacy.external_client import is_external_enabled

if TYPE_CHECKING:
    from collections.abc import Iterator

    from duckdb import DuckDBPyConnection


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Restore structlog defaults after each test.

    The CLI's ``_configure_logging`` mutates structlog's global state. Without
    this fixture a subsequent test (e.g. ``test_ingest_liftover``) sees a
    configured logger and the WARNING level filter swallows the INFO message
    those tests assert on.
    """
    try:
        yield
    finally:
        structlog.reset_defaults()


def test_config_get_returns_seeded_value(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["config", "get", "external_calls_enabled"])
    assert result.exit_code == 0
    # Seed default is 'false' (per init_schema.py USER_PREFERENCES_SEED). The
    # privacy master switch is fail-closed per CLAUDE.md decision #9.
    assert "false" in result.output
    assert "value_type=boolean" in result.output


def test_config_get_handles_missing_key(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["config", "get", "nope_not_there"])
    assert result.exit_code == 0
    assert "<not set>" in result.output


def test_config_set_updates_existing_key_and_writes_audit_row(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["config", "set", "external_calls_enabled", "true"],
    )
    assert result.exit_code == 0
    # The new value is reflected in the DB.
    with sqlcipher_connection() as conn:
        value = conn.execute(
            "SELECT pref_value FROM user_preferences WHERE pref_key=?",
            ("external_calls_enabled",),
        ).fetchone()[0]
        audit_rows = conn.execute(
            "SELECT action_type, resource_id, operation_details "
            "FROM audit_log WHERE action_type='config_change'",
        ).fetchall()
    assert value == "true"
    assert len(audit_rows) == 1
    assert audit_rows[0][1] == "external_calls_enabled"
    # Operation details JSON should reflect old → new transition.
    assert "false" in audit_rows[0][2]
    assert "true" in audit_rows[0][2]


def test_status_reports_live_external_calls_value_not_env_snapshot(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """`status` must report the LIVE user_preferences gate value, not the .env snapshot.

    Regression for finding-024: `status` read ``settings.external_calls_enabled`` (a
    load-time ``.env`` snapshot) while the egress gate enforces ``user_preferences``. After
    ``config set external_calls_enabled true`` — which writes ONLY ``user_preferences``; the
    isolated env keeps ``EXTERNAL_CALLS_ENABLED=false`` — ``status`` and the gate must agree.
    The pre-fix code, reading the ``.env``-bound Settings, would print ``False`` here.
    """
    init_databases()
    runner = CliRunner()
    set_result = runner.invoke(app, ["config", "set", "external_calls_enabled", "true"])
    assert set_result.exit_code == 0
    # The egress gate now reads True from the live store...
    assert is_external_enabled() is True
    # ...and status must display the same effective value.
    status_result = runner.invoke(app, ["status"])
    assert status_result.exit_code == 0
    assert "External calls enabled: True" in status_result.output


def test_config_set_requires_value_type_for_new_key(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["config", "set", "brand_new_key", "x"])
    assert result.exit_code != 0
    assert "value-type" in result.output.lower() or "value-type" in str(result.exception).lower()


def test_config_set_creates_new_key_with_value_type(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["config", "set", "new_key", "hello", "--value-type", "string"],
    )
    assert result.exit_code == 0
    with sqlcipher_connection() as conn:
        row = conn.execute(
            "SELECT pref_value, value_type FROM user_preferences WHERE pref_key='new_key'",
        ).fetchone()
    assert row == ("hello", "string")


def test_config_set_rejects_invalid_value_type(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["config", "set", "another_key", "x", "--value-type", "bogus"],
    )
    assert result.exit_code != 0


def test_imputation_help_top_level_and_each_subcommand(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    runner = CliRunner()
    top = runner.invoke(app, ["imputation", "--help"])
    assert top.exit_code == 0
    for cmd in ("prepare", "import", "list"):
        # Each command should appear in the parent help.
        assert cmd in top.output, f"{cmd!r} missing from `imputation --help`"
        sub = runner.invoke(app, ["imputation", cmd, "--help"])
        assert sub.exit_code == 0, f"{cmd} --help failed: {sub.output}"


def test_imputation_list_when_empty(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "list"])
    assert result.exit_code == 0
    assert "no imputation runs yet" in result.output


def test_imputation_import_help_lists_all_operational_flags(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """``genome imputation import --help`` exposes every operational flag."""
    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "import", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--r2-threshold",
        "--chromosomes",
        "--dry-run",
        "--batch-size",
        "--force-reimport",
    ):
        assert flag in result.output, f"{flag!r} missing from `imputation import --help`"


def test_imputation_import_rejects_invalid_chromosomes_filter(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """``--chromosomes`` with an invalid token aborts before any DB work."""
    init_databases()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["imputation", "import", "1", "--chromosomes", "1,NOPE"],
    )
    assert result.exit_code != 0
    assert (
        "invalid chromosome" in result.output.lower()
        or "invalid chromosome"
        in str(
            result.exception or "",
        ).lower()
    )


def _seed_one_consensus_variant(conn: DuckDBPyConnection) -> None:
    """Seed the minimum DB state needed for ``prepare_run`` to succeed.

    Mirrors the helper in ``test_imputation_vcf_export.py`` but inlined so this
    file stays self-contained.
    """
    conn.execute(
        """
        INSERT INTO ingestion_runs (
            run_id, source, source_chip_version, file_path, file_hash_sha256,
            file_size_bytes, file_native_build,
            variants_total, variants_called, variants_no_call, variants_imputed,
            status, pipeline_version, completed_at
        ) VALUES (1, '23andme'::source_enum, 'test', '/test/run_1', ?, 100,
                  'GRCh38', 1, 1, 0, 0, 'completed', 'pipeline_test',
                  CURRENT_TIMESTAMP)
        """,
        ["0" * 64],
    )
    conn.execute(
        """
        INSERT INTO variants_master (
            variant_id, rsid, chrom, pos_grch38, pos_grch37, ref_allele, alt_allele,
            variant_type, has_genotyped_call, has_imputed_call, is_acmg_sf,
            liftover_chain, liftover_status
        ) VALUES (1, 'rs_a', '1'::chromosome_enum, 1000, 1000, 'A', 'G',
                  'SNV'::variant_type_enum, TRUE, FALSE, FALSE,
                  'native_grch38', 'native_grch38')
        """,
    )
    conn.execute(
        """
        INSERT INTO consensus_genotypes (
            variant_id, consensus_allele_1, consensus_allele_2, is_no_call,
            dosage, consensus_method, is_imputed, contributing_calls,
            resolution_rule, confidence
        ) VALUES (1, 'A', 'G', FALSE, 1,
                  'both_concordant'::consensus_method_enum, FALSE,
                  ARRAY[]::BIGINT[], 'consensus_v1', 1.0)
        """,
    )
    conn.execute(
        """
        INSERT INTO genotype_calls (
            call_id, variant_id, source, source_chip_version, ingestion_run_id,
            genotype_raw, allele_1, allele_2, is_no_call,
            is_imputed, raw_strand, strand_status, quality_flags, is_active
        ) VALUES (1, 1, '23andme'::source_enum, 'test', 1,
                  'AG', 'A', 'G', FALSE, FALSE, '+',
                  'resolved_plus'::strand_status_enum,
                  ARRAY[]::VARCHAR[], TRUE)
        """,
    )


def test_imputation_prepare_stdout_has_no_topmed_or_web_ui_text(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Post-pivot check: prepare's stdout points at the local Beagle flow.

    Per finding-006, Phase 4 abandoned the TopMed Imputation Server in favor
    of local Beagle 5.5. The prepare command's "next step" guidance must
    reflect that — no TopMed, no web-UI, no upload language.
    """
    init_databases()
    with duckdb_connection() as conn:
        _seed_one_consensus_variant(conn)

    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "prepare"])
    assert result.exit_code == 0, result.output
    output_lower = result.output.lower()

    assert "topmed" not in output_lower, result.output
    assert "web-ui" not in output_lower, result.output
    assert "web ui" not in output_lower, result.output
    assert "form fields" not in output_lower, result.output

    match = re.search(r"imputation_id=(\d+)", result.output)
    assert match is not None, f"prepare did not echo imputation_id: {result.output}"
    imputation_id = match.group(1)
    assert f"genome imputation run {imputation_id}" in result.output, result.output
    assert "docs/runbooks/imputation.md" in result.output, result.output
