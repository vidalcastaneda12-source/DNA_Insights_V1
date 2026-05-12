"""Tests for :mod:`genome.privacy.external_client`.

Covers the contract that every Phase 4+ external call depends on: the master
switch is honored, every attempt writes one intent + one outcome audit row,
payload hashes are correct, and failures don't bypass auditing.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import httpx
import pytest

from genome.db import init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.privacy.external_client import (
    ExternalCallError,
    ExternalCallsDisabledError,
    ExternalClient,
    is_external_enabled,
    write_config_change_audit,
)

if TYPE_CHECKING:
    from pathlib import Path


def _enable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='true' WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _disable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='false'"
            " WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _audit_rows() -> list[tuple[object, ...]]:
    with sqlcipher_connection() as conn:
        return conn.execute(
            "SELECT action_type, resource_type, resource_id, operation_details,"
            " external_call, external_endpoint, external_payload_hash"
            " FROM audit_log ORDER BY log_id",
        ).fetchall()


def _mock_client(handler):  # type: ignore[no-untyped-def]
    """Build an httpx.Client backed by a MockTransport."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_disabled_master_switch_blocks_call_and_raises(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _disable_external_calls()
    # No request reaches the transport — the disabled check raises first, but
    # only after the intent + blocked audit rows are written.
    with _mock_client(lambda _r: httpx.Response(200, text="never seen")) as transport_client:
        client = ExternalClient("topmed", client=transport_client)
        with pytest.raises(ExternalCallsDisabledError, match="genome config set"):
            client.request("GET", "https://example/x", resource_type="t")
    # Two rows: the intent precedes the disabled-check, and a blocked result
    # row is written before the exception escapes. Blocked attempts must leave
    # a database trace per the "every external-facing operation is audited"
    # guarantee in CLAUDE.md decision #9.
    rows = _audit_rows()
    assert len(rows) == 2
    intent, blocked = rows
    intent_details = json.loads(str(intent[3]))
    blocked_details = json.loads(str(blocked[3]))
    assert intent_details == {"method": "GET", "phase": "intent"}
    assert blocked_details["phase"] == "result"
    assert blocked_details["status"] == "blocked"
    assert blocked_details["blocked"] is True
    assert blocked_details["method"] == "GET"
    assert "duration_ms" in blocked_details
    # Both rows tag the call as external and share endpoint + payload hash.
    assert intent[4] == 1
    assert blocked[4] == 1
    assert intent[5] == blocked[5] == "topmed"
    assert intent[6] == blocked[6]


def test_disabled_master_switch_writes_blocked_pair_for_download(
    isolated_settings: dict[str, str],  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """The blocked-attempt audit pair applies to ``download`` as well as ``request``."""
    init_databases()
    _disable_external_calls()
    dest = tmp_path / "never.zip"
    with _mock_client(lambda _r: httpx.Response(200, content=b"x")) as transport_client:
        client = ExternalClient("topmed", client=transport_client)
        with pytest.raises(ExternalCallsDisabledError):
            client.download(
                "https://example/zip",
                str(dest),
                resource_type="imputation_run",
                resource_id="999",
            )
    assert not dest.exists()
    rows = _audit_rows()
    assert len(rows) == 2
    assert json.loads(str(rows[0][3]))["phase"] == "intent"
    assert json.loads(str(rows[1][3]))["status"] == "blocked"


def test_successful_call_writes_two_audit_rows_with_matching_payload_hash(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()
    body = b'{"hello":"world"}'
    expected_hash = hashlib.sha256(body).hexdigest()

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    with _mock_client(handler) as transport_client:
        client = ExternalClient("topmed", client=transport_client)
        resp = client.request(
            "POST",
            "https://example/job",
            body=body,
            resource_type="imputation_run",
            resource_id="42",
            action_type="write",
        )

    assert resp.status_code == 200
    assert seen["url"] == "https://example/job"
    assert seen["body"] == body

    rows = _audit_rows()
    assert len(rows) == 2  # exactly one intent + one result row
    # Both rows share the same endpoint and payload hash.
    endpoints = {r[5] for r in rows}
    hashes = {r[6] for r in rows}
    assert endpoints == {"topmed"}
    assert hashes == {expected_hash}
    # The result row records 'success'.
    intent, result = rows
    intent_details = json.loads(str(intent[3]))
    result_details = json.loads(str(result[3]))
    assert intent_details == {"method": "POST", "phase": "intent"}
    assert result_details["phase"] == "result"
    assert result_details["status"] == "success"
    assert result_details["method"] == "POST"
    assert "duration_ms" in result_details
    # external_call flag is set; resource fields wired through.
    assert intent[4] == 1
    assert intent[1] == "imputation_run"
    assert intent[2] == "42"


def test_network_error_records_failure_audit_row_and_raises_external_call_error(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg)

    with _mock_client(handler) as transport_client:
        client = ExternalClient("topmed", client=transport_client)
        with pytest.raises(ExternalCallError, match="network error"):
            client.request("GET", "https://example", resource_type="t")

    rows = _audit_rows()
    assert len(rows) == 2  # intent + failure result
    result = json.loads(str(rows[-1][3]))
    assert result["phase"] == "result"
    assert result["status"] == "failure"
    assert result["error_type"] == "ExternalCallError"
    assert "connection refused" in result["error"]


def test_http_500_records_failure_audit_row_and_raises(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()

    with _mock_client(lambda _r: httpx.Response(500, text="server boom")) as t:
        client = ExternalClient("topmed", client=t)
        with pytest.raises(ExternalCallError, match="HTTP 500"):
            client.request("GET", "https://example", resource_type="t")

    rows = _audit_rows()
    assert len(rows) == 2
    result = json.loads(str(rows[-1][3]))
    assert result["status"] == "failure"
    assert "HTTP 500" in result["error"]


def test_http_404_also_records_failure(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()

    with _mock_client(lambda _r: httpx.Response(404, text="nope")) as t:
        client = ExternalClient("topmed", client=t)
        with pytest.raises(ExternalCallError, match="HTTP 404"):
            client.request("GET", "https://example", resource_type="t")
    rows = _audit_rows()
    assert len(rows) == 2
    assert json.loads(str(rows[-1][3]))["status"] == "failure"


def test_json_body_is_hashed_canonically(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()
    payload = {"b": 1, "a": 2}
    # canonical encoding: keys sorted, no whitespace
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    expected = hashlib.sha256(canonical).hexdigest()

    with _mock_client(lambda _r: httpx.Response(200, json={})) as t:
        client = ExternalClient("topmed", client=t)
        client.request(
            "POST",
            "https://example",
            json_body=payload,
            resource_type="t",
        )

    rows = _audit_rows()
    assert rows[0][6] == expected
    assert rows[1][6] == expected


def test_empty_body_hashes_to_empty_sha256(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()
    expected = hashlib.sha256(b"").hexdigest()

    with _mock_client(lambda _r: httpx.Response(200, json={})) as t:
        client = ExternalClient("topmed", client=t)
        client.request("GET", "https://example", resource_type="t")

    rows = _audit_rows()
    assert rows[0][6] == expected


def test_each_retry_writes_a_new_audit_pair(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Calling request() twice (e.g. user retries) produces two pairs, never updates."""
    init_databases()
    _enable_external_calls()

    attempts = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            msg = "slow"
            raise httpx.ReadTimeout(msg)
        return httpx.Response(200, json={"ok": True})

    with _mock_client(handler) as t:
        client = ExternalClient("topmed", client=t)
        with pytest.raises(ExternalCallError):
            client.request("GET", "https://example", resource_type="t")
        # The retry succeeds.
        client.request("GET", "https://example", resource_type="t")

    rows = _audit_rows()
    # Two pairs = 4 rows, in order: intent/failure, intent/success.
    assert len(rows) == 4
    statuses = [json.loads(str(r[3])).get("status") for r in rows]
    assert statuses == [None, "failure", None, "success"]
    # All four rows share the same payload hash (empty body).
    assert len({r[6] for r in rows}) == 1


def test_download_streams_to_disk_and_returns_sha256(
    isolated_settings: dict[str, str],  # noqa: ARG001
    tmp_path: Path,
) -> None:
    init_databases()
    _enable_external_calls()
    expected_payload = b"x" * 10_000
    expected_hash = hashlib.sha256(expected_payload).hexdigest()

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=expected_payload)

    dest = tmp_path / "result.zip"
    with _mock_client(handler) as t:
        client = ExternalClient("topmed", client=t)
        digest = client.download(
            "https://example/zip",
            str(dest),
            resource_type="imputation_run",
            resource_id="1",
        )
    assert digest == expected_hash
    assert dest.read_bytes() == expected_payload
    # One intent + one success row.
    rows = _audit_rows()
    assert len(rows) == 2
    assert json.loads(str(rows[-1][3]))["status"] == "success"


def test_download_http_error_raises_and_records(
    isolated_settings: dict[str, str],  # noqa: ARG001
    tmp_path: Path,
) -> None:
    init_databases()
    _enable_external_calls()

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    dest = tmp_path / "result.zip"
    with _mock_client(handler) as t:
        client = ExternalClient("topmed", client=t)
        with pytest.raises(ExternalCallError, match="HTTP 503"):
            client.download(
                "https://example/zip",
                str(dest),
                resource_type="imputation_run",
            )
    rows = _audit_rows()
    assert len(rows) == 2
    assert json.loads(str(rows[-1][3]))["status"] == "failure"


def test_is_external_enabled_reads_user_preferences(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    _disable_external_calls()
    assert is_external_enabled() is False
    _enable_external_calls()
    assert is_external_enabled() is True


def test_write_config_change_audit_records_local_change(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    log_id = write_config_change_audit(
        pref_key="external_calls_enabled",
        old_value="false",
        new_value="true",
    )
    assert log_id > 0
    rows = _audit_rows()
    assert len(rows) == 1
    (action_type, resource_type, resource_id, details, external_call, endpoint, payload_hash) = (
        rows[0]
    )
    assert action_type == "config_change"
    assert resource_type == "user_preference"
    assert resource_id == "external_calls_enabled"
    assert external_call == 0  # local change, not external
    assert endpoint is None
    assert payload_hash is None
    parsed = json.loads(str(details))
    assert parsed == {
        "pref_key": "external_calls_enabled",
        "old_value": "false",
        "new_value": "true",
    }
