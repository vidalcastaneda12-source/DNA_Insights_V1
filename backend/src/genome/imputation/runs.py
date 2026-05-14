"""CRUD helpers for ``imputation_runs``.

This is the authoritative record of a roundtrip's state. Every Phase 4 CLI
command reads it before acting, and every state transition writes a single
row update. The DuckDB schema's ``ingestion_status_enum`` is
``pending | processing | completed | failed``. Phase 4 maps the workflow's
finer-grained stages onto these four (plus a few application-only labels
stored in ``status`` as plain strings, but only after we expand the enum in
a later release — for now we stay strictly within the enum and use
``parameters``-style metadata sparingly).

Schema fields we own (locked by ``ddl/group_1_genotype.sql``):

* ``imputation_id`` — surrogate key.
* ``input_run_ids`` — array of ``ingestion_runs.run_id`` that fed the upload.
* ``imputation_server``, ``reference_panel`` — provenance.
* ``submitted_at``, ``completed_at`` — timing.
* ``status`` — enum (see above).
* ``variants_input``, ``variants_output``, ``mean_r2``,
  ``variants_above_r2_0_3``, ``variants_above_r2_0_8`` — volumes.
* ``output_file_path``, ``output_file_hash_sha256`` — result archive.
* ``pipeline_version`` — the code version that produced the row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog

from genome.db.duckdb_conn import duckdb_connection

if TYPE_CHECKING:
    from pathlib import Path

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

ImputationStatus = Literal["pending", "processing", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class ImputationRun:
    """One row in ``imputation_runs``, presented to callers in a typed shape."""

    imputation_id: int
    input_run_ids: tuple[int, ...]
    imputation_server: str
    reference_panel: str | None
    submitted_at: str | None
    completed_at: str | None
    status: ImputationStatus
    variants_input: int | None
    variants_output: int | None
    mean_r2: float | None
    variants_above_r2_0_3: int | None
    variants_above_r2_0_8: int | None
    r2_threshold: float | None
    output_file_path: str | None
    output_file_hash_sha256: str | None
    pipeline_version: str


def _next_imputation_id(conn: DuckDBPyConnection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(imputation_id), 0) FROM imputation_runs").fetchone()
    return int(row[0]) + 1 if row is not None else 1


def insert_run(  # noqa: PLR0913 — schema fields are not collapsible
    conn: DuckDBPyConnection,
    *,
    input_run_ids: tuple[int, ...],
    imputation_server: str,
    reference_panel: str | None,
    pipeline_version: str,
    variants_input: int,
) -> int:
    """Insert a fresh ``imputation_runs`` row in ``status='pending'``.

    Returns the new ``imputation_id``. ``submitted_at`` is left NULL — it's
    set when :func:`mark_submitted` is called after the user has actually
    uploaded the file to TopMed. ``status='pending'`` therefore means
    "VCFs prepared locally; user has not yet uploaded".
    """
    imputation_id = _next_imputation_id(conn)
    conn.execute(
        """
        INSERT INTO imputation_runs (
            imputation_id, input_run_ids, imputation_server, reference_panel,
            status, variants_input, pipeline_version
        )
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        [
            imputation_id,
            list(input_run_ids),
            imputation_server,
            reference_panel,
            variants_input,
            pipeline_version,
        ],
    )
    return imputation_id


def fetch_run(conn: DuckDBPyConnection, imputation_id: int) -> ImputationRun | None:
    """Return the row for ``imputation_id`` or ``None`` if it does not exist."""
    row = conn.execute(
        """
        SELECT
            imputation_id, input_run_ids, imputation_server, reference_panel,
            CAST(submitted_at AS VARCHAR), CAST(completed_at AS VARCHAR),
            CAST(status AS VARCHAR),
            variants_input, variants_output, mean_r2,
            variants_above_r2_0_3, variants_above_r2_0_8, r2_threshold,
            output_file_path, output_file_hash_sha256, pipeline_version
        FROM imputation_runs
        WHERE imputation_id = ?
        """,
        [imputation_id],
    ).fetchone()
    if row is None:
        return None
    return _row_to_dataclass(row)


def _row_to_dataclass(row: tuple[object, ...]) -> ImputationRun:
    """Convert a DuckDB row tuple to a typed :class:`ImputationRun`.

    Centralized so :func:`fetch_run` and :func:`list_all` stay in sync if the
    column list ever changes.
    """
    (
        imputation_id,
        input_run_ids,
        server,
        panel,
        submitted_at,
        completed_at,
        status,
        v_in,
        v_out,
        mean_r2,
        above_03,
        above_08,
        r2_threshold,
        out_path,
        out_hash,
        pipeline_v,
    ) = row
    raw_ids: object = input_run_ids if input_run_ids is not None else []
    if not isinstance(raw_ids, list):
        msg = f"input_run_ids column must be a list, got {type(raw_ids).__name__}"
        raise TypeError(msg)
    input_ids = tuple(int(i) for i in raw_ids)
    return ImputationRun(
        imputation_id=int(imputation_id),  # type: ignore[call-overload]
        input_run_ids=input_ids,
        imputation_server=str(server),
        reference_panel=None if panel is None else str(panel),
        submitted_at=None if submitted_at is None else str(submitted_at),
        completed_at=None if completed_at is None else str(completed_at),
        status=_coerce_status(status),
        variants_input=None if v_in is None else int(v_in),  # type: ignore[call-overload]
        variants_output=None if v_out is None else int(v_out),  # type: ignore[call-overload]
        mean_r2=None if mean_r2 is None else float(mean_r2),  # type: ignore[arg-type]
        variants_above_r2_0_3=None if above_03 is None else int(above_03),  # type: ignore[call-overload]
        variants_above_r2_0_8=None if above_08 is None else int(above_08),  # type: ignore[call-overload]
        r2_threshold=None if r2_threshold is None else float(r2_threshold),  # type: ignore[arg-type]
        output_file_path=None if out_path is None else str(out_path),
        output_file_hash_sha256=None if out_hash is None else str(out_hash),
        pipeline_version=str(pipeline_v),
    )


def _coerce_status(value: object) -> ImputationStatus:
    """Coerce DuckDB's enum value to a Literal-friendly Python string."""
    s = str(value)
    if s in {"pending", "processing", "completed", "failed"}:
        return s  # type: ignore[return-value]
    msg = f"unexpected imputation_runs.status value: {value!r}"
    raise ValueError(msg)


def list_all(conn: DuckDBPyConnection) -> list[ImputationRun]:
    """Return every ``imputation_runs`` row, ordered newest-first by id."""
    rows = conn.execute(
        """
        SELECT
            imputation_id, input_run_ids, imputation_server, reference_panel,
            CAST(submitted_at AS VARCHAR), CAST(completed_at AS VARCHAR),
            CAST(status AS VARCHAR),
            variants_input, variants_output, mean_r2,
            variants_above_r2_0_3, variants_above_r2_0_8, r2_threshold,
            output_file_path, output_file_hash_sha256, pipeline_version
        FROM imputation_runs
        ORDER BY imputation_id DESC
        """,
    ).fetchall()
    return [_row_to_dataclass(r) for r in rows]


def update_status(
    conn: DuckDBPyConnection,
    imputation_id: int,
    *,
    status: ImputationStatus,
    set_submitted: bool = False,
    set_completed: bool = False,
) -> None:
    """Move a run to ``status``.

    ``set_submitted`` and ``set_completed`` stamp the corresponding timestamp
    columns to ``CURRENT_TIMESTAMP`` only if they were previously NULL. This
    preserves idempotence — re-running a status check that already advanced
    a run does not bump the timestamp.

    Invariant the callers must honour: every transition out of ``pending``
    passes ``set_submitted=True`` and every transition to ``completed``
    passes ``set_completed=True``. ``COALESCE`` semantics here are what
    make re-entry safe; the helper itself does not infer the flags from
    the status.
    """
    parts = ["status = ?"]
    params: list[object] = [status]
    if set_submitted:
        parts.append("submitted_at = COALESCE(submitted_at, CURRENT_TIMESTAMP)")
    if set_completed:
        parts.append("completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)")
    sql = (
        "UPDATE imputation_runs SET "  # noqa: S608 — column list is internal
        + ", ".join(parts)
        + " WHERE imputation_id = ?"
    )
    params.append(imputation_id)
    conn.execute(sql, params)


def record_download(
    conn: DuckDBPyConnection,
    imputation_id: int,
    *,
    output_file_path: str,
    output_file_hash_sha256: str,
) -> None:
    """Record the downloaded archive's path and SHA-256 on the run."""
    conn.execute(
        """
        UPDATE imputation_runs
           SET output_file_path = ?,
               output_file_hash_sha256 = ?
         WHERE imputation_id = ?
        """,
        [output_file_path, output_file_hash_sha256, imputation_id],
    )


def record_import_volumes(  # noqa: PLR0913 — schema fields are not collapsible
    conn: DuckDBPyConnection,
    imputation_id: int,
    *,
    variants_output: int,
    mean_r2: float | None,
    variants_above_r2_0_3: int,
    variants_above_r2_0_8: int,
    r2_threshold: float | None,
) -> None:
    """Record the per-variant volume / quality summaries on the run.

    Called by :func:`import_result` after the imputed VCFs are ingested.
    ``r2_threshold`` captures the import-time filter used; ``None`` means no
    threshold was applied.
    """
    conn.execute(
        """
        UPDATE imputation_runs
           SET variants_output = ?,
               mean_r2 = ?,
               variants_above_r2_0_3 = ?,
               variants_above_r2_0_8 = ?,
               r2_threshold = ?
         WHERE imputation_id = ?
        """,
        [
            variants_output,
            mean_r2,
            variants_above_r2_0_3,
            variants_above_r2_0_8,
            r2_threshold,
            imputation_id,
        ],
    )


def list_runs(*, duckdb_path: Path | None = None) -> list[ImputationRun]:
    """Read-only convenience wrapper around :func:`list_all`."""
    with duckdb_connection(duckdb_path, read_only=True) as conn:
        return list_all(conn)
