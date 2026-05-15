"""Tests for :mod:`genome.annotate.supersession`.

Exercises the generic deactivation helper against the real
``clinvar_annotations`` table (created by ``init_databases``). That
table carries both ``is_active`` and ``superseded_by``, which lets us
verify both code paths of :func:`deactivate_prior_versions`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from genome.annotate.source_versions import upsert_source_version
from genome.annotate.supersession import deactivate_prior_versions
from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def _seed_clinvar_rows(
    conn: DuckDBPyConnection,
    source_version_id: int,
    *,
    count: int,
) -> None:
    """Plant ``count`` ``clinvar_annotations`` rows under one source_version_id.

    The schema requires a non-null ``retrieval_date``; everything else
    has a sensible default that lets us focus on ``source_version_id``,
    ``is_active``, and ``superseded_by``.
    """
    for i in range(count):
        conn.execute(
            """
            INSERT INTO clinvar_annotations (
                clinvar_id, variation_id, source_version_id, retrieval_date,
                is_active, superseded_by
            )
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, TRUE, NULL)
            """,
            [source_version_id * 1000 + i, f"VCV{i:06d}", source_version_id],
        )


def test_deactivate_prior_versions_flips_is_active_and_sets_superseded_by(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        v1 = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_03_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=2,
        )
        _seed_clinvar_rows(conn, v1, count=2)
        v2 = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="b" * 64,
            source_file_size=1,
            record_count=3,
        )
        _seed_clinvar_rows(conn, v2, count=3)

        touched = deactivate_prior_versions(
            conn,
            table="clinvar_annotations",
            new_source_version_id=v2,
            has_superseded_by=True,
        )
        rows = conn.execute(
            """
            SELECT source_version_id, is_active, superseded_by
              FROM clinvar_annotations
             ORDER BY clinvar_id
            """,
        ).fetchall()

    assert touched == 2
    by_version: dict[int, list[tuple[object, ...]]] = {}
    for r in rows:
        by_version.setdefault(int(r[0]), []).append(r)
    # Prior-version rows are deactivated and point at the new version.
    assert len(by_version[v1]) == 2
    for r in by_version[v1]:
        assert r[1] is False
        assert r[2] == v2
    # New-version rows are untouched.
    assert len(by_version[v2]) == 3
    for r in by_version[v2]:
        assert r[1] is True
        assert r[2] is None


def test_deactivate_prior_versions_without_superseded_by_leaves_column_null(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        v1 = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_03_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=1,
        )
        _seed_clinvar_rows(conn, v1, count=1)
        v2 = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="b" * 64,
            source_file_size=1,
            record_count=1,
        )
        _seed_clinvar_rows(conn, v2, count=1)

        touched = deactivate_prior_versions(
            conn,
            table="clinvar_annotations",
            new_source_version_id=v2,
            has_superseded_by=False,
        )
        rows = conn.execute(
            """
            SELECT source_version_id, is_active, superseded_by
              FROM clinvar_annotations
             ORDER BY clinvar_id
            """,
        ).fetchall()

    assert touched == 1
    by_version = {int(r[0]): r for r in rows}
    assert by_version[v1][1] is False
    assert by_version[v1][2] is None
    assert by_version[v2][1] is True
    assert by_version[v2][2] is None


def test_deactivate_prior_versions_returns_zero_when_no_prior_rows(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        v1 = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=1,
        )
        _seed_clinvar_rows(conn, v1, count=2)
        touched = deactivate_prior_versions(
            conn,
            table="clinvar_annotations",
            new_source_version_id=v1,
            has_superseded_by=True,
        )
    assert touched == 0


def test_deactivate_prior_versions_rejects_unknown_table(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with (
        duckdb_connection() as conn,
        pytest.raises(ValueError, match="unknown supersession table") as exc_info,
    ):
        deactivate_prior_versions(
            conn,
            table="users",
            new_source_version_id=1,
            has_superseded_by=True,
        )
    message = str(exc_info.value)
    # The error message names the allowed set so the operator can see
    # which tables are supported without re-reading the schema doc.
    for label in (
        "clinvar_annotations",
        "gwas_catalog_associations",
        "pharmgkb_annotations",
        "cpic_guidelines",
        "pgs_catalog_scores",
    ):
        assert label in message
