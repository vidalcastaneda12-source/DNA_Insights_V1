"""Tests for :mod:`genome.imputation.topmed_client`.

These cover the parse-state mapping (Cloudgene API → our ``ImputationStatus``)
and the high-level helpers (``check_status``, ``download_result``). HTTP is
mocked with ``httpx.MockTransport``; we never reach the real TopMed server.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import httpx
import pytest

from genome.db import duckdb_connection, init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.imputation.archive import ImputationArchive
from genome.imputation.runs import (
    fetch_run,
    insert_run,
    update_status,
)
from genome.imputation.topmed_client import (
    TopMedClient,
    _state_to_status,
    check_status,
    download_result,
)
from genome.privacy.external_client import ExternalCallError

if TYPE_CHECKING:
    from pathlib import Path


def _enable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='true' WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _seed_imputation_run(*, status: str = "pending") -> int:
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1,),
            imputation_server="topmed",
            reference_panel="topmed_r3",
            pipeline_version="imputation_prepare_v0.1.0",
            variants_input=10,
        )
        if status != "pending":
            update_status(
                conn,
                imp_id,
                status=status,  # type: ignore[arg-type]
                set_submitted=True,
                set_completed=(status == "completed"),
            )
    return imp_id


# ----------------------------------------------------------------------------
# State mapping unit tests
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("state", [1, 2, 3])
def test_cloudgene_running_states_map_to_processing(state: int) -> None:
    assert _state_to_status(state) == "processing"


def test_cloudgene_state_4_maps_to_completed() -> None:
    assert _state_to_status(4) == "completed"


@pytest.mark.parametrize("state", [5, 6, 7, 100, -1])
def test_unknown_or_failure_states_map_to_failed(state: int) -> None:
    assert _state_to_status(state) == "failed"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("running", "processing"),
        ("queued", "processing"),
        ("pending", "processing"),
        ("success", "completed"),
        ("completed", "completed"),
        ("OK", "completed"),
        ("unknown_label", "failed"),
    ],
)
def test_string_labels_map_through_normalize(label: str, expected: str) -> None:
    assert _state_to_status(label) == expected


def test_none_maps_to_failed() -> None:
    assert _state_to_status(None) == "failed"


# ----------------------------------------------------------------------------
# check_status: end-to-end against a mocked TopMed
# ----------------------------------------------------------------------------


def _client_for(response_factory):  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(response_factory)
    return TopMedClient(http_client=httpx.Client(transport=transport))


def test_check_status_persists_processing_state(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()
    imp_id = _seed_imputation_run()

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "job-1", "state": 2})

    with _client_for(handler) as client:
        status = check_status(
            imp_id,
            status_url="https://example/api/v2/jobs/job-1",
            client=client,
        )

    assert status.status == "processing"
    assert status.raw_state == 2
    assert status.job_id == "job-1"
    # DB reflects the transition + submitted_at.
    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "processing"
    assert run.submitted_at is not None


def test_check_status_marks_completed_and_stamps_completed_at(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()
    imp_id = _seed_imputation_run()

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "job-1", "state": 4, "completedAt": "2026-05-12T12:00:00Z"},
        )

    with _client_for(handler) as client:
        status = check_status(imp_id, status_url="https://e", client=client)

    assert status.status == "completed"
    assert status.completed_at == "2026-05-12T12:00:00Z"
    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "completed"
    assert run.completed_at is not None


def test_check_status_is_idempotent_on_repeat(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()
    imp_id = _seed_imputation_run()

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": 4})

    with _client_for(handler) as client:
        check_status(imp_id, status_url="https://e", client=client)
        first = fetch_run_completed_at(imp_id)
        check_status(imp_id, status_url="https://e", client=client)
        second = fetch_run_completed_at(imp_id)
    assert first == second  # completed_at stamped once


def fetch_run_completed_at(imp_id: int) -> str | None:
    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    return run.completed_at if run else None


def test_check_status_raises_for_unknown_id(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": 4})

    with _client_for(handler) as client, pytest.raises(ValueError, match="not found"):
        check_status(999, status_url="https://e", client=client)


def test_check_status_propagates_external_call_error_on_bad_json(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()
    imp_id = _seed_imputation_run()

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    with _client_for(handler) as client, pytest.raises(ExternalCallError, match="JSON"):
        check_status(imp_id, status_url="https://e", client=client)


def test_check_status_records_one_audit_pair_per_call(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()
    imp_id = _seed_imputation_run()

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": 4})

    with _client_for(handler) as client:
        check_status(imp_id, status_url="https://e", client=client)

    with sqlcipher_connection() as conn:
        rows = conn.execute(
            "SELECT external_endpoint, external_call FROM audit_log",
        ).fetchall()
    assert len(rows) == 2  # intent + result
    assert {r[0] for r in rows} == {"topmed"}
    assert all(r[1] == 1 for r in rows)


# ----------------------------------------------------------------------------
# download_result
# ----------------------------------------------------------------------------


def test_download_result_streams_archive_and_records_hash(
    isolated_settings: dict[str, str],  # noqa: ARG001
    tmp_path: Path,
) -> None:
    init_databases()
    _enable_external_calls()
    imp_id = _seed_imputation_run(status="completed")

    payload = b"FAKE-TOPMED-ARCHIVE" * 100
    expected_hash = hashlib.sha256(payload).hexdigest()

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    with _client_for(handler) as client:
        dest = download_result(
            imp_id,
            download_url="https://example/result.zip",
            password="pw",
            archive_root=tmp_path,
            client=client,
        )

    archive = ImputationArchive.for_run(tmp_path, imp_id)
    assert dest == archive.encrypted_archive
    assert dest.is_file()
    assert dest.read_bytes() == payload
    # Hash is on the run row.
    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.output_file_hash_sha256 == expected_hash


def test_download_result_rejects_run_not_completed(
    isolated_settings: dict[str, str],  # noqa: ARG001
    tmp_path: Path,
) -> None:
    init_databases()
    _enable_external_calls()
    imp_id = _seed_imputation_run(status="processing")

    with (
        _client_for(lambda _r: httpx.Response(200, content=b"x")) as client,
        pytest.raises(
            RuntimeError,
            match="only 'completed' runs",
        ),
    ):
        download_result(
            imp_id,
            download_url="https://example/x",
            password="pw",
            archive_root=tmp_path,
            client=client,
        )


def test_download_result_is_idempotent_on_matching_hash(
    isolated_settings: dict[str, str],  # noqa: ARG001
    tmp_path: Path,
) -> None:
    init_databases()
    _enable_external_calls()
    imp_id = _seed_imputation_run(status="completed")

    payload = b"DATA" * 256
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=payload)

    with _client_for(handler) as client:
        download_result(
            imp_id,
            download_url="https://example/result.zip",
            password="pw",
            archive_root=tmp_path,
            client=client,
        )
        download_result(
            imp_id,
            download_url="https://example/result.zip",
            password="pw",
            archive_root=tmp_path,
            client=client,
        )
    # Second call short-circuits via hash match — only one HTTP request landed.
    assert calls["n"] == 1


def test_download_result_removes_partial_file_on_error(
    isolated_settings: dict[str, str],  # noqa: ARG001
    tmp_path: Path,
) -> None:
    init_databases()
    _enable_external_calls()
    imp_id = _seed_imputation_run(status="completed")

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _client_for(handler) as client, pytest.raises(ExternalCallError):
        download_result(
            imp_id,
            download_url="https://example/x",
            password="pw",
            archive_root=tmp_path,
            client=client,
        )
    archive = ImputationArchive.for_run(tmp_path, imp_id)
    assert not archive.encrypted_archive.exists()
