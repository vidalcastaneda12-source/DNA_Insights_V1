"""Tests for :mod:`genome.annotate.source_versions`.

Covers the ``annotation_source_versions`` upsert + read helpers: the
one-current-row invariant, the ``(source_db, version)`` idempotence
short-circuit, and the source_db whitelist.
"""

from __future__ import annotations

import pytest

from genome.annotate.source_versions import (
    KNOWN_SOURCE_DBS,
    get_current_version,
    upsert_source_version,
)
from genome.db import duckdb_connection, init_databases


def test_upsert_inserts_new_row_as_current(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        new_id = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url="https://example.invalid/clinvar.vcf.gz",
            source_file_hash="a" * 64,
            source_file_size=12_345,
            record_count=10,
            notes="initial load",
        )
        current = get_current_version(conn, "clinvar")
    assert new_id >= 1
    assert current is not None
    assert current.source_version_id == new_id
    assert current.source_db == "clinvar"
    assert current.version == "2026_04_15"
    assert current.source_url == "https://example.invalid/clinvar.vcf.gz"
    assert current.source_file_hash == "a" * 64
    assert current.source_file_size == 12_345
    assert current.record_count == 10
    assert current.is_current is True
    assert current.notes == "initial load"


def test_upsert_same_source_db_and_version_is_idempotent(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        first = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=None,
        )
        second = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url="https://different.invalid/clinvar.vcf.gz",
            source_file_hash="b" * 64,
            source_file_size=999,
            record_count=999,
        )
        rows = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'clinvar'",
        ).fetchone()
    assert first == second
    assert rows is not None
    assert rows[0] == 1


def test_upsert_new_version_deactivates_prior_current(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        first = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_03_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=None,
        )
        second = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="b" * 64,
            source_file_size=2,
            record_count=None,
        )
        rows = conn.execute(
            """
            SELECT source_version_id, version, is_current
              FROM annotation_source_versions
             WHERE source_db = 'clinvar'
             ORDER BY source_version_id
            """,
        ).fetchall()
        current_rows = [r for r in rows if r[2]]

    assert second > first
    assert len(rows) == 2
    assert len(current_rows) == 1
    assert current_rows[0][0] == second
    by_id = {r[0]: r for r in rows}
    assert by_id[first][2] is False
    assert by_id[second][2] is True


def test_get_current_version_returns_latest_after_multiple_upserts(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_03_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=None,
        )
        latest_id = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="b" * 64,
            source_file_size=2,
            record_count=None,
        )
        current = get_current_version(conn, "clinvar")
    assert current is not None
    assert current.source_version_id == latest_id
    assert current.version == "2026_04_15"


def test_get_current_version_returns_none_for_unloaded_source(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        assert get_current_version(conn, "clinvar") is None


def test_upsert_rejects_unknown_source_db(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with (
        duckdb_connection() as conn,
        pytest.raises(ValueError, match="unknown source_db") as exc_info,
    ):
        upsert_source_version(
            conn,
            source_db="not_a_real_source",
            version="2026_04_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=None,
        )
    message = str(exc_info.value)
    # The error message names the sorted set of valid values so the
    # operator can pick the right one without re-reading the schema doc.
    for label in sorted(KNOWN_SOURCE_DBS):
        assert label in message
