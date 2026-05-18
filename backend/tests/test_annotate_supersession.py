"""Tests for :mod:`genome.annotate.supersession`.

Exercises the generic deactivation helper against the real
``clinvar_annotations`` table (created by ``init_databases``). That
table carries both ``is_active`` and ``superseded_by``, which lets us
verify both code paths of :func:`deactivate_prior_versions`.

Also covers the observability additions from finding-009 (#9, #11, #14):
the per-phase structlog progress events, the explicit ``CHECKPOINT``
issued by :func:`commit_and_checkpoint`, and the
:func:`maybe_skip_same_version` short-circuit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog
from structlog.testing import capture_logs

from genome.annotate.source_versions import upsert_source_version
from genome.annotate.supersession import (
    commit_and_checkpoint,
    deactivate_prior_versions,
    maybe_skip_same_version,
)
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


# ---------------------------------------------------------------------------
# Per-phase structlog observability (finding-009 #9 + #11).
# ---------------------------------------------------------------------------


def test_deactivate_prior_versions_emits_update_start_and_complete_events(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """The UPDATE phase emits start + complete events with the expected fields."""
    init_databases()
    with duckdb_connection() as conn:
        v1 = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_03_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=3,
        )
        _seed_clinvar_rows(conn, v1, count=3)
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

        with capture_logs() as captured:
            touched = deactivate_prior_versions(
                conn,
                table="clinvar_annotations",
                new_source_version_id=v2,
                has_superseded_by=True,
                source_name="clinvar",
            )

    assert touched == 3
    events = [c["event"] for c in captured]
    # Two events fire, in order.
    assert events == ["supersession_update_start", "supersession_update_complete"]

    start_event = captured[0]
    assert start_event["source_name"] == "clinvar"
    assert start_event["table"] == "clinvar_annotations"
    assert start_event["new_source_version_id"] == v2
    # The pre-UPDATE count names rows about to be deactivated.
    assert start_event["prior_active_rows"] == 3

    complete_event = captured[1]
    assert complete_event["source_name"] == "clinvar"
    assert complete_event["table"] == "clinvar_annotations"
    assert complete_event["new_source_version_id"] == v2
    assert complete_event["rows_deactivated"] == 3
    # duration_ms is a non-negative integer the operator can use to attribute
    # time without correlating timestamps across events.
    assert isinstance(complete_event["duration_ms"], int)
    assert complete_event["duration_ms"] >= 0


def test_commit_and_checkpoint_emits_four_events_and_issues_checkpoint(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """COMMIT + explicit CHECKPOINT both fire start/complete events with duration_ms."""
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
        # Open a transaction so commit() has work to do; insert one row inside
        # it so the supersession-shaped wrapping is exercised.
        conn.begin()
        _seed_clinvar_rows(conn, v1, count=1)
        with capture_logs() as captured:
            commit_and_checkpoint(conn, source_name="clinvar")

    events = [c["event"] for c in captured]
    assert events == [
        "supersession_commit_start",
        "supersession_commit_complete",
        "supersession_checkpoint_start",
        "supersession_checkpoint_complete",
    ]
    # source_name threads through every event.
    for entry in captured:
        assert entry["source_name"] == "clinvar"
    # Both _complete events report duration_ms.
    assert isinstance(captured[1]["duration_ms"], int)
    assert captured[1]["duration_ms"] >= 0
    assert isinstance(captured[3]["duration_ms"], int)
    assert captured[3]["duration_ms"] >= 0


def test_commit_and_checkpoint_issues_explicit_commit_then_checkpoint() -> None:
    """The helper must call ``conn.commit()`` and then run ``CHECKPOINT``.

    Uses a :class:`MagicMock` connection rather than a real DuckDB
    handle so the test asserts precisely on call order without depending
    on DuckDB's internals. Regression guard: if a future refactor drops
    the explicit CHECKPOINT (finding-009 #11 is the measurement step
    that makes the post-commit flush legible), this assertion fails.
    """
    from unittest.mock import MagicMock, call  # noqa: PLC0415 — test-local

    conn = MagicMock()
    commit_and_checkpoint(conn, source_name="clinvar")

    # commit() runs exactly once; execute("CHECKPOINT") runs exactly once.
    conn.commit.assert_called_once_with()
    conn.execute.assert_called_once_with("CHECKPOINT")

    # Call order: commit() comes before execute("CHECKPOINT"). The mock's
    # parent ``method_calls`` list captures cross-method ordering.
    assert conn.method_calls == [call.commit(), call.execute("CHECKPOINT")]


# ---------------------------------------------------------------------------
# --skip-if-same-version short-circuit (finding-009 #14).
# ---------------------------------------------------------------------------


def test_maybe_skip_same_version_returns_none_when_flag_disabled(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """The flag is opt-in -- with it off the helper never short-circuits."""
    init_databases()
    with duckdb_connection() as conn:
        upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=1,
        )
    result = maybe_skip_same_version(
        source_db="clinvar",
        version="2026_05_10",
        source_file_hash="a" * 64,
        skip_if_same_version=False,
    )
    assert result is None


def test_maybe_skip_same_version_short_circuits_when_active_row_matches(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Matching active (version + hash) → returns RefreshResult, emits event."""
    init_databases()
    with duckdb_connection() as conn:
        v1 = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=42,
        )

    with capture_logs() as captured:
        result = maybe_skip_same_version(
            source_db="clinvar",
            version="2026_05_10",
            source_file_hash="a" * 64,
            skip_if_same_version=True,
        )

    assert result is not None
    assert result.source_db == "clinvar"
    assert result.source_version_id == v1
    assert result.version == "2026_05_10"
    assert result.record_count == 42
    assert result.was_already_current is True

    events = [c["event"] for c in captured]
    assert "supersession_skipped_same_version" in events
    skip_event = next(c for c in captured if c["event"] == "supersession_skipped_same_version")
    assert skip_event["source_db"] == "clinvar"
    assert skip_event["source_version_id"] == v1
    assert skip_event["version"] == "2026_05_10"


def test_maybe_skip_same_version_returns_none_when_no_active_row(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """No matching active row → proceed normally (returns None)."""
    init_databases()
    # No rows seeded — the source has never been loaded.
    result = maybe_skip_same_version(
        source_db="clinvar",
        version="2026_05_10",
        source_file_hash="a" * 64,
        skip_if_same_version=True,
    )
    assert result is None


def test_maybe_skip_same_version_returns_none_when_version_differs(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Active row has a different version label → proceed normally."""
    init_databases()
    with duckdb_connection() as conn:
        upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=1,
        )

    result = maybe_skip_same_version(
        source_db="clinvar",
        version="2026_05_17",  # newer label
        source_file_hash="a" * 64,
        skip_if_same_version=True,
    )
    assert result is None


def test_maybe_skip_same_version_returns_none_when_hash_differs(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Active row matches version but the file hash drifted → proceed normally.

    This is the safety net: upstream silently regenerated the file
    under the same version label. We don't want to skip a re-load in
    that case (the bytes changed; the load is meaningful).
    """
    init_databases()
    with duckdb_connection() as conn:
        upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=1,
        )

    result = maybe_skip_same_version(
        source_db="clinvar",
        version="2026_05_10",
        source_file_hash="b" * 64,  # different bytes
        skip_if_same_version=True,
    )
    assert result is None


def test_maybe_skip_same_version_ignores_superseded_match(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """A matching but superseded (older) row does NOT short-circuit.

    The match has to be against the *currently-active* row. Earlier
    versions in the supersession chain are not eligible: we are looking
    for "is what's about to land already current?", not "have we ever
    seen this version?".
    """
    init_databases()
    with duckdb_connection() as conn:
        # Insert v1, then v2. v1 becomes superseded (is_current=FALSE).
        upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_03_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=1,
        )
        upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="b" * 64,
            source_file_size=1,
            record_count=1,
        )

    # Asking about v1 (now superseded) should NOT short-circuit, even
    # though the (source_db, version, hash) tuple existed historically.
    result = maybe_skip_same_version(
        source_db="clinvar",
        version="2026_03_15",
        source_file_hash="a" * 64,
        skip_if_same_version=True,
    )
    assert result is None


@pytest.fixture(autouse=True)
def _reset_structlog_after_each_test() -> object:
    """Restore structlog defaults so capture_logs doesn't leak between tests."""
    try:
        yield
    finally:
        structlog.reset_defaults()
