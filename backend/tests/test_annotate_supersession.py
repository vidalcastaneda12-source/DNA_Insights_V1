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


# ---------------------------------------------------------------------------
# force_all_active mode (finding-009 #16 — unified --force path).
# ---------------------------------------------------------------------------


def test_deactivate_prior_versions_force_all_active_deactivates_every_active_row(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """``force_all_active=True`` sweeps active rows regardless of version id.

    Seeds three versions, all active, then calls the helper with
    ``new_source_version_id=v3`` and ``force_all_active=True``. The
    default predicate would only touch v1 and v2 (versions strictly
    less than v3); force mode must touch v3 too, since the
    same-version ``--force`` case is the whole reason the parameter
    exists.
    """
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
        v3 = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_15",
            source_url=None,
            source_file_hash="c" * 64,
            source_file_size=1,
            record_count=1,
        )
        _seed_clinvar_rows(conn, v3, count=1)

        touched = deactivate_prior_versions(
            conn,
            table="clinvar_annotations",
            new_source_version_id=v3,
            has_superseded_by=True,
            force_all_active=True,
        )
        rows = conn.execute(
            """
            SELECT source_version_id, is_active, superseded_by
              FROM clinvar_annotations
             ORDER BY clinvar_id
            """,
        ).fetchall()

    # 2 (v1) + 3 (v2) + 1 (v3) = 6 active rows; force mode touches them all.
    assert touched == 6
    by_version: dict[int, list[tuple[object, ...]]] = {}
    for r in rows:
        by_version.setdefault(int(r[0]), []).append(r)
    for sv_id in (v1, v2, v3):
        assert len(by_version[sv_id]) == ({v1: 2, v2: 3, v3: 1}[sv_id])
        for r in by_version[sv_id]:
            assert r[1] is False
            # Every deactivated row points at the new (== v3) version id,
            # including v3's own rows -- the "self supersession" shape
            # that lets us identify which sweep deactivated them.
            assert r[2] == v3


def test_deactivate_prior_versions_default_mode_byte_identical_behavior(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """``force_all_active=False`` (default) preserves existing behavior.

    Regression guard for the unification refactor: the default path
    must touch exactly the rows whose ``source_version_id`` is
    strictly less than the new id AND ``is_active`` is true. v3's own
    rows (sharing the new id) stay active.
    """
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
        v3 = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_15",
            source_url=None,
            source_file_hash="c" * 64,
            source_file_size=1,
            record_count=1,
        )
        _seed_clinvar_rows(conn, v3, count=1)

        touched = deactivate_prior_versions(
            conn,
            table="clinvar_annotations",
            new_source_version_id=v3,
            has_superseded_by=True,
            # force_all_active defaults to False; assert the documented
            # default behavior is preserved.
        )
        rows = conn.execute(
            """
            SELECT source_version_id, is_active, superseded_by
              FROM clinvar_annotations
             ORDER BY clinvar_id
            """,
        ).fetchall()

    # v1 (2) + v2 (3) = 5 rows; v3's own rows are NOT touched.
    assert touched == 5
    by_version: dict[int, list[tuple[object, ...]]] = {}
    for r in rows:
        by_version.setdefault(int(r[0]), []).append(r)
    for sv_id in (v1, v2):
        for r in by_version[sv_id]:
            assert r[1] is False
            assert r[2] == v3
    # v3's own row is untouched.
    assert len(by_version[v3]) == 1
    assert by_version[v3][0][1] is True
    assert by_version[v3][0][2] is None


def test_deactivate_prior_versions_force_mode_without_superseded_by(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Force mode with ``has_superseded_by=False`` only flips ``is_active``.

    Mirrors the PharmGKB / CPIC / GWAS / PGS schema shape: those
    tables carry ``is_active`` but not ``superseded_by``. The helper
    must respect the flag even in force mode -- it doesn't try to
    write the (non-existent) ``superseded_by`` column.
    """
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

        touched = deactivate_prior_versions(
            conn,
            table="clinvar_annotations",
            new_source_version_id=v1,
            has_superseded_by=False,
            force_all_active=True,
        )
        rows = conn.execute(
            """
            SELECT is_active, superseded_by
              FROM clinvar_annotations
             ORDER BY clinvar_id
            """,
        ).fetchall()

    assert touched == 2
    for r in rows:
        assert r[0] is False
        # No has_superseded_by → superseded_by stays NULL even in force mode.
        assert r[1] is None


def test_deactivate_prior_versions_events_include_force_all_active_flag(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Both ``_start`` and ``_complete`` events surface the mode flag.

    Force mode must be distinguishable from default mode in the log
    stream so an operator reading a `--force` re-run can confirm the
    sweep semantics. Real-data verification of finding-009 #16 found
    the previous inline UPDATE emitted no events at all; this
    assertion is the regression guard for the new unified path.
    """
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

        # Default mode emits force_all_active=False.
        with capture_logs() as default_log:
            deactivate_prior_versions(
                conn,
                table="clinvar_annotations",
                new_source_version_id=v1 + 1,  # sentinel new id (no rows under it)
                has_superseded_by=True,
                source_name="clinvar",
            )

        # Re-seed since the above deactivated v1's rows.
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

        # Force mode emits force_all_active=True.
        with capture_logs() as force_log:
            deactivate_prior_versions(
                conn,
                table="clinvar_annotations",
                new_source_version_id=v2,
                has_superseded_by=True,
                source_name="clinvar",
                force_all_active=True,
            )

    for entry in default_log:
        if entry["event"] in {"supersession_update_start", "supersession_update_complete"}:
            assert entry["force_all_active"] is False
    for entry in force_log:
        if entry["event"] in {"supersession_update_start", "supersession_update_complete"}:
            assert entry["force_all_active"] is True


# ---------------------------------------------------------------------------
# Loader-level — each Phase-5 loader's _deactivate_for_refresh routes through
# the shared helper (finding-009 #16).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("loader_module", "table"),
    [
        ("genome.annotate.loaders.clinvar", "clinvar_annotations"),
        ("genome.annotate.loaders.pharmgkb", "pharmgkb_annotations"),
        ("genome.annotate.loaders.cpic", "cpic_guidelines"),
        ("genome.annotate.loaders.gwas_catalog", "gwas_catalog_associations"),
        ("genome.annotate.loaders.pgs_catalog", "pgs_catalog_scores"),
    ],
)
def test_loader_deactivate_for_refresh_routes_through_shared_helper(
    loader_module: str,
    table: str,
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Each loader's ``_deactivate_for_refresh`` wrapper calls the shared helper.

    Regression guard for finding-009 #16: the prior inline UPDATE on
    the ``force=True`` path bypassed
    :func:`deactivate_prior_versions` and its observability events.
    Patching the helper module-side and asserting it gets called with
    ``force_all_active=force`` confirms the unification holds across
    every Phase-5 loader and that no loader silently keeps an inline
    UPDATE behind the wrapper.
    """
    from importlib import import_module  # noqa: PLC0415 — test-local
    from unittest.mock import patch  # noqa: PLC0415 — test-local

    loader = import_module(loader_module)
    sentinel_version_id = 12345

    for force in (False, True):
        with patch.object(loader, "deactivate_prior_versions", return_value=0) as mock_helper:
            loader._deactivate_for_refresh(  # noqa: SLF001 — test asserts the wrapper's contract
                conn=None,  # the mock doesn't use it
                source_version_id=sentinel_version_id,
                force=force,
            )
            mock_helper.assert_called_once()
            kwargs = mock_helper.call_args.kwargs
            assert kwargs["table"] == table
            assert kwargs["new_source_version_id"] == sentinel_version_id
            assert kwargs["force_all_active"] is force
            assert kwargs["source_name"] == loader.SOURCE_DB
            # ClinVar is the only Phase-5 loader that populates
            # superseded_by; the others must keep has_superseded_by=False.
            expected_has_superseded_by = loader_module.endswith(".clinvar")
            assert kwargs["has_superseded_by"] is expected_has_superseded_by


@pytest.fixture(autouse=True)
def _reset_structlog_after_each_test() -> object:
    """Restore structlog defaults so capture_logs doesn't leak between tests."""
    try:
        yield
    finally:
        structlog.reset_defaults()
