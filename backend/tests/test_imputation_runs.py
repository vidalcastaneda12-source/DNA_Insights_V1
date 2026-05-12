"""Tests for :mod:`genome.imputation.runs` — the ``imputation_runs`` CRUD helpers."""

from __future__ import annotations

from genome.db import duckdb_connection, init_databases
from genome.imputation.runs import (
    fetch_run,
    insert_run,
    list_all,
    record_download,
    record_import_volumes,
    update_status,
)


def test_insert_then_fetch_round_trips_every_field(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1, 2),
            imputation_server="topmed",
            reference_panel="topmed_r3",
            pipeline_version="imputation_prepare_v0.1.0",
            variants_input=12_345,
        )
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.imputation_id == imp_id
    assert run.input_run_ids == (1, 2)
    assert run.imputation_server == "topmed"
    assert run.reference_panel == "topmed_r3"
    assert run.pipeline_version == "imputation_prepare_v0.1.0"
    assert run.variants_input == 12_345
    assert run.status == "pending"
    # Optional fields are None until later steps populate them.
    assert run.variants_output is None
    assert run.mean_r2 is None
    assert run.output_file_path is None


def test_fetch_run_returns_none_for_missing_id(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        assert fetch_run(conn, 999) is None


def test_update_status_transitions_pending_to_processing_to_completed(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1,),
            imputation_server="topmed",
            reference_panel="topmed_r3",
            pipeline_version="imputation_prepare_v0.1.0",
            variants_input=100,
        )
        update_status(conn, imp_id, status="processing", set_submitted=True)
        first = fetch_run(conn, imp_id)
        update_status(conn, imp_id, status="completed", set_completed=True)
        second = fetch_run(conn, imp_id)

    assert first is not None
    assert second is not None
    assert first.status == "processing"
    assert first.submitted_at is not None
    assert first.completed_at is None
    assert second.status == "completed"
    assert second.submitted_at == first.submitted_at  # idempotent stamp
    assert second.completed_at is not None


def test_update_status_does_not_re_stamp_existing_timestamps(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1,),
            imputation_server="topmed",
            reference_panel="topmed_r3",
            pipeline_version="imputation_prepare_v0.1.0",
            variants_input=10,
        )
        update_status(conn, imp_id, status="processing", set_submitted=True)
        first_submitted = fetch_run(conn, imp_id).submitted_at  # type: ignore[union-attr]
        update_status(conn, imp_id, status="processing", set_submitted=True)
        second_submitted = fetch_run(conn, imp_id).submitted_at  # type: ignore[union-attr]
    assert first_submitted == second_submitted


def test_record_download_and_record_import_volumes(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1,),
            imputation_server="topmed",
            reference_panel="topmed_r3",
            pipeline_version="imputation_prepare_v0.1.0",
            variants_input=100,
        )
        update_status(conn, imp_id, status="completed", set_submitted=True, set_completed=True)
        record_download(
            conn,
            imp_id,
            output_file_path="/tmp/archive.zip",  # noqa: S108 — string in test data, not a real path
            output_file_hash_sha256="a" * 64,
        )
        record_import_volumes(
            conn,
            imp_id,
            variants_output=29_000_000,
            mean_r2=0.82,
            variants_above_r2_0_3=27_000_000,
            variants_above_r2_0_8=15_000_000,
        )
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.output_file_path == "/tmp/archive.zip"  # noqa: S108 — string in test data, not a real path
    assert run.output_file_hash_sha256 == "a" * 64
    assert run.variants_output == 29_000_000
    assert run.mean_r2 is not None
    assert abs(run.mean_r2 - 0.82) < 1e-9
    assert run.variants_above_r2_0_3 == 27_000_000
    assert run.variants_above_r2_0_8 == 15_000_000


def test_list_all_returns_newest_first(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        ids = [
            insert_run(
                conn,
                input_run_ids=(i,),
                imputation_server="topmed",
                reference_panel="topmed_r3",
                pipeline_version="v0.1",
                variants_input=10,
            )
            for i in range(3)
        ]
        rows = list_all(conn)
    assert [r.imputation_id for r in rows] == sorted(ids, reverse=True)
