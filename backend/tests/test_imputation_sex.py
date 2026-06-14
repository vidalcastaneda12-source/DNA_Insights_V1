"""Tests for :mod:`genome.imputation.sex` and the CLI ``--sex`` gate (PR 5a).

The strict :func:`resolve_sex` underpins the chrX run gate; the soft
:func:`profile_sex_label` underpins the prepare manifest and shares its rule
with the corrected-dosage view's ``profile_sex`` CTE.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import typer

from genome.cli import _chrx_in_run_scope, _gate_chrx_sex, _normalize_sex_flag
from genome.config import get_settings
from genome.db import duckdb_connection, init_databases
from genome.imputation.archive import ImputationArchive
from genome.imputation.sex import (
    AmbiguousSexError,
    profile_sex_label,
    resolve_sex,
)

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def _insert_chip_qc(
    conn: DuckDBPyConnection,
    *,
    run_id: int,
    source: str,
    sex_inferred: str,
) -> None:
    """Seed one chip ingestion_run + its sample_qc row carrying ``sex_inferred``."""
    conn.execute(
        """
        INSERT INTO ingestion_runs (
            run_id, source, file_path, file_hash_sha256, status, pipeline_version
        ) VALUES (?, ?::source_enum, ?, ?, 'completed', 'test')
        """,
        [run_id, source, f"/t/run_{run_id}", "0" * 64],
    )
    conn.execute(
        "INSERT INTO sample_qc (qc_id, run_id, sex_inferred, qc_status) VALUES (?, ?, ?, 'pass')",
        [run_id, run_id, sex_inferred],
    )


def _seed(qc: list[tuple[str, str]], isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Initialize the DB and seed ``(source, sex_inferred)`` chip-QC rows."""
    init_databases()
    with duckdb_connection() as conn:
        for i, (source, sex) in enumerate(qc, start=1):
            _insert_chip_qc(conn, run_id=i, source=source, sex_inferred=sex)


@pytest.mark.parametrize(
    ("qc", "expected"),
    [
        ([("23andme", "M")], "M"),
        ([("23andme", "M"), ("ancestry", "ambiguous")], "M"),  # this user's corpus
        ([("23andme", "ambiguous"), ("ancestry", "F")], "F"),
        ([("23andme", "F"), ("ancestry", "F")], "F"),
    ],
)
def test_resolve_sex_confident_aggregate(
    isolated_settings: dict[str, str],
    qc: list[tuple[str, str]],
    expected: str,
) -> None:
    _seed(qc, isolated_settings)
    with duckdb_connection() as conn:
        assert resolve_sex(conn) == expected
        assert profile_sex_label(conn) == expected


@pytest.mark.parametrize(
    "qc",
    [
        [("23andme", "M"), ("ancestry", "F")],  # conflict
        [("23andme", "ambiguous"), ("ancestry", "ambiguous")],  # all ambiguous
        [],  # no chip QC at all
    ],
)
def test_resolve_sex_ambiguous_raises(
    isolated_settings: dict[str, str],
    qc: list[tuple[str, str]],
) -> None:
    _seed(qc, isolated_settings)
    with duckdb_connection() as conn:
        assert profile_sex_label(conn) == "ambiguous"
        with pytest.raises(AmbiguousSexError):
            resolve_sex(conn)


def test_explicit_sex_overrides_chip_inference(isolated_settings: dict[str, str]) -> None:
    """An explicit ``--sex`` wins even when the chip aggregate would disagree."""
    _seed([("23andme", "M")], isolated_settings)
    with duckdb_connection() as conn:
        assert resolve_sex(conn, "F") == "F"
        assert profile_sex_label(conn, "M") == "M"


def test_imputed_source_does_not_vote_on_profile_sex(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths via fixture
) -> None:
    """Only chip sources count; a beagle_imputed QC row must not break the tie."""
    init_databases()
    with duckdb_connection() as conn:
        _insert_chip_qc(conn, run_id=1, source="23andme", sex_inferred="ambiguous")
        _insert_chip_qc(conn, run_id=2, source="beagle_imputed", sex_inferred="M")
        assert profile_sex_label(conn) == "ambiguous"
        with pytest.raises(AmbiguousSexError):
            resolve_sex(conn)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("auto", None), ("AUTO", None), ("m", "M"), ("M", "M"), ("f", "F"), (" F ", "F")],
)
def test_normalize_sex_flag(raw: str, expected: str | None) -> None:
    assert _normalize_sex_flag(raw) == expected


def test_normalize_sex_flag_rejects_junk() -> None:
    with pytest.raises(typer.BadParameter):
        _normalize_sex_flag("male")


def test_gate_chrx_sex_blocks_ambiguous_when_chrx_in_scope(
    isolated_settings: dict[str, str],
) -> None:
    _seed([("23andme", "ambiguous"), ("ancestry", "ambiguous")], isolated_settings)
    with pytest.raises(typer.BadParameter):
        _gate_chrx_sex(frozenset({"X"}), "auto")
    # A full run (chromosomes=None ⇒ includes X) is gated too.
    with pytest.raises(typer.BadParameter):
        _gate_chrx_sex(None, "auto")


def test_gate_chrx_sex_skips_autosome_only_runs(
    isolated_settings: dict[str, str],
) -> None:
    """An ambiguous-sex profile can still run autosomes — the gate doesn't fire."""
    _seed([("23andme", "ambiguous")], isolated_settings)
    _gate_chrx_sex(frozenset({"1", "2"}), "auto")  # no raise


def test_gate_chrx_sex_accepts_explicit_override(
    isolated_settings: dict[str, str],
) -> None:
    _seed([("23andme", "ambiguous"), ("ancestry", "ambiguous")], isolated_settings)
    _gate_chrx_sex(frozenset({"X"}), "M")  # explicit --sex satisfies the gate


def test_chrx_in_run_scope_requires_upload_and_scope(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects archive path
) -> None:
    """chrX gates fire only when chrX is in scope AND a chrX upload exists."""
    archive = ImputationArchive.for_run(get_settings().archive_path, 1)
    archive.ensure_layout()
    archive.upload_vcf_path("X").write_bytes(b"x")

    # Autosome-only scope is never in chrX scope, even with a chrX upload present.
    assert _chrx_in_run_scope(1, frozenset({"1", "2"})) is False
    # Full / explicit-X scope with the upload present → True.
    assert _chrx_in_run_scope(1, None) is True
    assert _chrx_in_run_scope(1, frozenset({"X"})) is True
    # A run with no chrX upload (id 2) is not in scope even on a full run.
    assert _chrx_in_run_scope(2, None) is False
