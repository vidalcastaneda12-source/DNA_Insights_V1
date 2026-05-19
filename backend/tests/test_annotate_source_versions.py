"""Tests for :mod:`genome.annotate.source_versions`.

Covers ``insert_source_version`` (always allocates a new row) and
``get_current_version`` (reads the row named by ``annotation_sources``).
"""

from __future__ import annotations

import pytest

from genome.annotate.source_versions import (
    KNOWN_SOURCE_DBS,
    get_current_version,
    insert_source_version,
)
from genome.annotate.supersession import flip_to_new_version
from genome.db import duckdb_connection, init_databases


def _seed_clinvar_row(conn: object, source_version_id: int) -> None:
    """Plant one ``clinvar_annotations`` row so flip_to_new_version's COUNT(*) is happy."""
    conn.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO clinvar_annotations (
            clinvar_id, variation_id, source_version_id, retrieval_date
        )
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [source_version_id * 1000, f"VCV{source_version_id:06d}", source_version_id],
    )


def test_insert_writes_a_row(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """A fresh insert returns a new id and the row is queryable."""
    init_databases()
    with duckdb_connection() as conn:
        new_id = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url="https://example.invalid/clinvar.vcf.gz",
            source_file_hash="a" * 64,
            source_file_size=12_345,
            record_count=10,
            notes="initial load",
        )
        row = conn.execute(
            """
            SELECT source_version_id, source_db, version, source_url,
                   source_file_hash, source_file_size, record_count, notes
              FROM annotation_source_versions
             WHERE source_version_id = ?
            """,
            [new_id],
        ).fetchone()
    assert new_id >= 1
    assert row is not None
    assert int(row[0]) == new_id
    assert row[1] == "clinvar"
    assert row[2] == "2026_04_15"
    assert row[3] == "https://example.invalid/clinvar.vcf.gz"
    assert row[4] == "a" * 64
    assert int(row[5]) == 12_345
    assert int(row[6]) == 10
    assert row[7] == "initial load"


def test_insert_always_allocates_new_row_on_same_source_db_and_version(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Calling insert twice with the same (source_db, version) writes two rows.

    Under the version-pointer model the version registry is the audit
    trail. The idempotence belongs upstream (the loader's pre-refresh
    check + ``--skip-if-same-version``). Once ``insert_source_version``
    is called, it always writes a new row -- multiple rows may share
    the same upstream version label.
    """
    init_databases()
    with duckdb_connection() as conn:
        first = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=None,
        )
        second = insert_source_version(
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
    assert second > first
    assert rows is not None
    expected_row_count = 2
    assert rows[0] == expected_row_count


def test_insert_does_not_touch_annotation_sources_pointer(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """``insert_source_version`` writes only to ``annotation_source_versions``.

    The pointer in ``annotation_sources`` is owned by
    :func:`flip_to_new_version`. The insert helper must not pre-flip
    the pointer; that's a separate step in the loader's transaction.
    """
    init_databases()
    with duckdb_connection() as conn:
        insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=None,
        )
        pointer = conn.execute(
            "SELECT COUNT(*) FROM annotation_sources WHERE source_db = 'clinvar'",
        ).fetchone()
    assert pointer is not None
    assert pointer[0] == 0


def test_get_current_version_reads_via_pointer(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """``get_current_version`` returns the row the pointer names, not the latest insert."""
    init_databases()
    with duckdb_connection() as conn:
        v1 = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_03_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=None,
        )
        _seed_clinvar_row(conn, v1)
        flip_to_new_version(
            conn,
            source="clinvar",
            table="clinvar_annotations",
            new_source_version_id=v1,
        )
        # Insert a second row but don't flip yet.
        v2 = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="b" * 64,
            source_file_size=2,
            record_count=None,
        )

        current_before_flip = get_current_version(conn, "clinvar")
        assert current_before_flip is not None
        assert current_before_flip.source_version_id == v1

        # Now flip; current should change to v2.
        _seed_clinvar_row(conn, v2)
        flip_to_new_version(
            conn,
            source="clinvar",
            table="clinvar_annotations",
            new_source_version_id=v2,
        )
        current_after_flip = get_current_version(conn, "clinvar")
    assert current_after_flip is not None
    assert current_after_flip.source_version_id == v2
    assert current_after_flip.version == "2026_04_15"


def test_get_current_version_returns_none_for_unloaded_source(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        assert get_current_version(conn, "clinvar") is None


def test_get_current_version_returns_none_when_pointer_missing_even_if_row_exists(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Inserting without flipping leaves ``get_current_version`` returning None.

    Important under the new model: the pointer in ``annotation_sources``
    is the source of truth for "current". A row inserted but never
    pointed at is not current.
    """
    init_databases()
    with duckdb_connection() as conn:
        insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=None,
        )
        assert get_current_version(conn, "clinvar") is None


def test_insert_rejects_unknown_source_db(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with (
        duckdb_connection() as conn,
        pytest.raises(ValueError, match="unknown source_db") as exc_info,
    ):
        insert_source_version(
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
