"""Tests for :mod:`genome.annotate.supersession`.

Exercises the version-pointer supersession workflow against the real
``annotation_sources`` + ``clinvar_annotations`` tables (created by
``init_databases``):

* :func:`flip_to_new_version` upserts the per-source pointer in
  ``annotation_sources`` and emits the ``supersession_version_flip``
  event with prior + new version ids and row counts.
* :func:`commit_and_checkpoint` brackets ``conn.commit()`` and the
  explicit ``CHECKPOINT`` with per-phase structlog events.
* :func:`maybe_skip_same_version` short-circuits the refresh when the
  incoming (version, hash) matches the currently-active row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog
from structlog.testing import capture_logs

from genome.annotate.source_versions import insert_source_version
from genome.annotate.supersession import (
    VersionFlipResult,
    commit_and_checkpoint,
    flip_to_new_version,
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

    The schema requires a non-null ``retrieval_date``; everything else has a
    sensible default that lets us focus on which ``source_version_id``
    each row belongs to.
    """
    for i in range(count):
        conn.execute(
            """
            INSERT INTO clinvar_annotations (
                clinvar_id, variation_id, source_version_id, retrieval_date
            )
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [source_version_id * 1000 + i, f"VCV{i:06d}", source_version_id],
        )


# ---------------------------------------------------------------------------
# flip_to_new_version — first load + refresh.
# ---------------------------------------------------------------------------


def test_flip_to_new_version_first_load_inserts_pointer_row(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """First load for a source: pointer row doesn't exist yet → INSERT it."""
    init_databases()
    with duckdb_connection() as conn:
        v1 = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=3,
        )
        _seed_clinvar_rows(conn, v1, count=3)

        result = flip_to_new_version(
            conn,
            source="clinvar",
            table="clinvar_annotations",
            new_source_version_id=v1,
        )
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db = ?",
            ["clinvar"],
        ).fetchone()

    assert pointer is not None
    assert int(pointer[0]) == v1
    assert isinstance(result, VersionFlipResult)
    assert result.source == "clinvar"
    assert result.prior_version_id is None  # first load → no prior pointer
    assert result.new_version_id == v1
    assert result.prior_row_count == 0
    assert result.new_row_count == 3
    assert isinstance(result.elapsed_ms, int)
    assert result.elapsed_ms >= 0


def test_flip_to_new_version_refresh_updates_existing_pointer(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Refresh path: pointer row exists → UPDATE its current_source_version_id."""
    init_databases()
    with duckdb_connection() as conn:
        v1 = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=2,
        )
        _seed_clinvar_rows(conn, v1, count=2)
        # First flip puts the pointer at v1.
        flip_to_new_version(
            conn,
            source="clinvar",
            table="clinvar_annotations",
            new_source_version_id=v1,
        )

        # Now load v2 and flip again — UPDATE path.
        v2 = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
            source_url=None,
            source_file_hash="b" * 64,
            source_file_size=1,
            record_count=3,
        )
        _seed_clinvar_rows(conn, v2, count=3)

        result = flip_to_new_version(
            conn,
            source="clinvar",
            table="clinvar_annotations",
            new_source_version_id=v2,
        )
        pointer_rows = conn.execute(
            "SELECT source_db, current_source_version_id FROM annotation_sources",
        ).fetchall()

    # Pointer flipped from v1 to v2; only one row per source.
    assert len(pointer_rows) == 1
    assert pointer_rows[0][0] == "clinvar"
    assert int(pointer_rows[0][1]) == v2
    assert result.prior_version_id == v1
    assert result.new_version_id == v2
    assert result.prior_row_count == 2
    assert result.new_row_count == 3


def test_flip_to_new_version_same_upstream_version_allocates_new_id(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Same upstream version label, two refreshes → two distinct ids, pointer flips.

    This is the regression case the PR fixes. Under the prior model the
    second refresh re-used the first refresh's source_version_id and
    the loader inserted new rows under it (duplicates). Under the new
    model every call to ``insert_source_version`` allocates a fresh
    id; the pointer flips to it; ``supersession_version_flip``'s
    ``prior_version_id`` and ``new_version_id`` are distinct.
    """
    init_databases()
    with duckdb_connection() as conn:
        v1 = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=4,
        )
        _seed_clinvar_rows(conn, v1, count=4)
        flip_to_new_version(
            conn,
            source="clinvar",
            table="clinvar_annotations",
            new_source_version_id=v1,
        )
        # Same upstream version label, fresh refresh: a NEW row in
        # annotation_source_versions with a distinct source_version_id.
        v2 = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=4,
        )
        _seed_clinvar_rows(conn, v2, count=4)
        result = flip_to_new_version(
            conn,
            source="clinvar",
            table="clinvar_annotations",
            new_source_version_id=v2,
        )

        version_rows = conn.execute(
            "SELECT source_version_id, version FROM annotation_source_versions"
            " WHERE source_db = 'clinvar' ORDER BY source_version_id",
        ).fetchall()
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db = 'clinvar'",
        ).fetchone()

    assert v2 != v1
    assert [(int(r[0]), r[1]) for r in version_rows] == [
        (v1, "2026_05_10"),
        (v2, "2026_05_10"),
    ]
    assert pointer is not None
    assert int(pointer[0]) == v2
    assert result.prior_version_id == v1
    assert result.new_version_id == v2
    assert result.prior_row_count == 4
    assert result.new_row_count == 4


def test_flip_to_new_version_emits_event_with_full_payload(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """The ``supersession_version_flip`` event carries the full payload."""
    init_databases()
    with duckdb_connection() as conn:
        v1 = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=2,
        )
        _seed_clinvar_rows(conn, v1, count=2)
        flip_to_new_version(
            conn,
            source="clinvar",
            table="clinvar_annotations",
            new_source_version_id=v1,
        )
        v2 = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
            source_url=None,
            source_file_hash="b" * 64,
            source_file_size=1,
            record_count=5,
        )
        _seed_clinvar_rows(conn, v2, count=5)

        with capture_logs() as captured:
            flip_to_new_version(
                conn,
                source="clinvar",
                table="clinvar_annotations",
                new_source_version_id=v2,
            )

    events = [c for c in captured if c["event"] == "supersession_version_flip"]
    assert len(events) == 1
    event = events[0]
    assert event["source"] == "clinvar"
    assert event["prior_version_id"] == v1
    assert event["new_version_id"] == v2
    assert event["prior_row_count"] == 2
    assert event["new_row_count"] == 5
    assert isinstance(event["elapsed_ms"], int)
    assert event["elapsed_ms"] >= 0


def test_flip_to_new_version_atomic_single_upsert(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """The pointer flip runs as one UPSERT inside the current transaction.

    A failure between the COUNT and the UPSERT (or between the UPSERT
    and the row-count check) leaves the pointer in a defined state:
    either the prior row (no UPSERT yet, rollback restores it) or the
    new row (UPSERT ran, will be committed when the surrounding
    transaction commits). Regression guard: the helper must NOT call
    ``conn.commit()`` itself (the caller owns transaction boundaries).
    """
    from unittest.mock import MagicMock  # noqa: PLC0415 — test-local

    conn = MagicMock()
    # COUNT(*) on prior, UPSERT, COUNT(*) on new — three execute calls
    # and zero commits.
    conn.execute.return_value.fetchone.return_value = (0,)
    flip_to_new_version(
        conn,
        source="clinvar",
        table="clinvar_annotations",
        new_source_version_id=42,
    )
    # The helper does not commit; the wrapping transaction owns COMMIT.
    conn.commit.assert_not_called()


def test_flip_to_new_version_rejects_unknown_table(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with (
        duckdb_connection() as conn,
        pytest.raises(ValueError, match="unknown supersession table") as exc_info,
    ):
        flip_to_new_version(
            conn,
            source="clinvar",
            table="users",
            new_source_version_id=1,
        )
    message = str(exc_info.value)
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


def test_commit_and_checkpoint_emits_four_events_and_issues_checkpoint(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """COMMIT + explicit CHECKPOINT both fire start/complete events with duration_ms."""
    init_databases()
    with duckdb_connection() as conn:
        v1 = insert_source_version(
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
    for entry in captured:
        assert entry["source_name"] == "clinvar"
    assert isinstance(captured[1]["duration_ms"], int)
    assert captured[1]["duration_ms"] >= 0
    assert isinstance(captured[3]["duration_ms"], int)
    assert captured[3]["duration_ms"] >= 0


def test_commit_and_checkpoint_issues_explicit_commit_then_checkpoint() -> None:
    """The helper must call ``conn.commit()`` and then run ``CHECKPOINT``.

    Uses a :class:`MagicMock` connection rather than a real DuckDB
    handle so the test asserts precisely on call order without depending
    on DuckDB's internals.
    """
    from unittest.mock import MagicMock, call  # noqa: PLC0415 — test-local

    conn = MagicMock()
    commit_and_checkpoint(conn, source_name="clinvar")
    conn.commit.assert_called_once_with()
    conn.execute.assert_called_once_with("CHECKPOINT")
    assert conn.method_calls == [call.commit(), call.execute("CHECKPOINT")]


# ---------------------------------------------------------------------------
# --skip-if-same-version short-circuit (finding-009 #14).
# ---------------------------------------------------------------------------


def _insert_and_flip(  # noqa: PLR0913 — keyword-only audit fields are not collapsible
    conn: DuckDBPyConnection,
    *,
    source_db: str,
    table: str,
    version: str,
    source_file_hash: str,
    record_count: int,
) -> int:
    """Helper: insert a version row, seed one annotation row, flip the pointer."""
    sv_id = insert_source_version(
        conn,
        source_db=source_db,
        version=version,
        source_url=None,
        source_file_hash=source_file_hash,
        source_file_size=1,
        record_count=record_count,
    )
    _seed_clinvar_rows(conn, sv_id, count=1)
    flip_to_new_version(
        conn,
        source=source_db,
        table=table,
        new_source_version_id=sv_id,
    )
    return sv_id


def test_maybe_skip_same_version_returns_none_when_flag_disabled(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """The flag is opt-in -- with it off the helper never short-circuits."""
    init_databases()
    with duckdb_connection() as conn:
        _insert_and_flip(
            conn,
            source_db="clinvar",
            table="clinvar_annotations",
            version="2026_05_10",
            source_file_hash="a" * 64,
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
        v1 = _insert_and_flip(
            conn,
            source_db="clinvar",
            table="clinvar_annotations",
            version="2026_05_10",
            source_file_hash="a" * 64,
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
        _insert_and_flip(
            conn,
            source_db="clinvar",
            table="clinvar_annotations",
            version="2026_05_10",
            source_file_hash="a" * 64,
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
        _insert_and_flip(
            conn,
            source_db="clinvar",
            table="clinvar_annotations",
            version="2026_05_10",
            source_file_hash="a" * 64,
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

    The match has to be against the *currently-active* row -- the one
    the ``annotation_sources`` pointer names. Earlier rows in the
    supersession chain are not eligible.
    """
    init_databases()
    with duckdb_connection() as conn:
        # Insert v1, flip pointer; then insert v2 and flip pointer.
        _insert_and_flip(
            conn,
            source_db="clinvar",
            table="clinvar_annotations",
            version="2026_03_15",
            source_file_hash="a" * 64,
            record_count=1,
        )
        _insert_and_flip(
            conn,
            source_db="clinvar",
            table="clinvar_annotations",
            version="2026_04_15",
            source_file_hash="b" * 64,
            record_count=1,
        )

    # Asking about v1 (now superseded by the pointer) should NOT short-circuit.
    result = maybe_skip_same_version(
        source_db="clinvar",
        version="2026_03_15",
        source_file_hash="a" * 64,
        skip_if_same_version=True,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Loader-level — each Phase-5 loader populates annotation_sources on refresh.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "table"),
    [
        ("clinvar", "clinvar_annotations"),
        ("gwas_catalog", "gwas_catalog_associations"),
        ("pharmgkb", "pharmgkb_annotations"),
        ("cpic", "cpic_guidelines"),
        ("pgs_catalog", "pgs_catalog_scores"),
    ],
)
def test_flip_to_new_version_supports_every_phase_5_source(
    source: str,
    table: str,
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Each Phase-5 loader's target source/table pair flips cleanly.

    Regression guard for the version-pointer refactor: every source the
    refactor touched must work through the shared helper, including the
    first-load path (no prior pointer row) for sources whose loader
    test suites don't exercise that codepath directly.
    """
    init_databases()
    with duckdb_connection() as conn:
        new_id = insert_source_version(
            conn,
            source_db=source,
            version="2026_05_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=1,
            record_count=0,
        )
        result = flip_to_new_version(
            conn,
            source=source,
            table=table,
            new_source_version_id=new_id,
        )
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db = ?",
            [source],
        ).fetchone()

    assert pointer is not None
    assert int(pointer[0]) == new_id
    assert result.prior_version_id is None
    assert result.new_version_id == new_id


@pytest.fixture(autouse=True)
def _reset_structlog_after_each_test() -> object:
    """Restore structlog defaults so capture_logs doesn't leak between tests."""
    try:
        yield
    finally:
        structlog.reset_defaults()
