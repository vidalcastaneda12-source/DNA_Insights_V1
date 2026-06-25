"""Tests for :mod:`genome.privacy.external_client`.

Covers the contract that every Phase 4+ external call depends on: the master
switch is honored, every attempt writes one intent + one outcome audit row,
payload hashes are correct, and failures don't bypass auditing.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

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
    write_merge_audit,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
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


class _UnReadStream(httpx.SyncByteStream):
    """A streaming body that requires explicit ``response.read()``.

    ``httpx.Response(..., text=...)`` and ``httpx.Response(..., content=...)``
    both pre-buffer the body so ``.text`` works immediately. Real upstream
    HTTP responses streamed via ``Client.stream(...)`` instead defer body
    consumption -- ``.text`` raises ``ResponseNotRead`` until ``.read()``
    runs. This stub reproduces the deferred-read shape so the test exercises
    the same code path that fires against a live EBI / NCBI 404.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __iter__(self) -> Iterator[bytes]:
        yield self._body

    def close(self) -> None:
        return None


def test_download_http_error_includes_streamed_body_snippet(
    isolated_settings: dict[str, str],  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """Regression: the error message must surface the response body snippet
    even when the response is in deferred-read streaming mode.

    Before the fix, ``response.text`` was accessed before ``response.read()``
    ran, which raised ``httpx.ResponseNotRead`` and masked the actual HTTP
    error. The bug fired against every real-world 4xx/5xx download (the
    existing 503 ``text='busy'`` test passed only because MockTransport's
    text= kwarg pre-buffered the body).
    """
    init_databases()
    _enable_external_calls()

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(404, stream=_UnReadStream(b"not found here"))

    dest = tmp_path / "result.zip"
    with _mock_client(handler) as t:
        client = ExternalClient("ebi_gwas", client=t)
        with pytest.raises(ExternalCallError) as exc_info:
            client.download(
                "https://example/zip",
                str(dest),
                resource_type="annotation_source",
            )
    # The message must carry the HTTP status, the endpoint label, AND the
    # body snippet — and must NOT mention ResponseNotRead (the masking bug).
    msg = str(exc_info.value)
    assert "HTTP 404" in msg
    assert "ebi_gwas" in msg
    assert "not found here" in msg
    assert "ResponseNotRead" not in msg

    rows = _audit_rows()
    assert len(rows) == 2
    result_details = json.loads(str(rows[-1][3]))
    assert result_details["status"] == "failure"
    # The audited error_type must be ExternalCallError, not the masked
    # ResponseNotRead — that's what proves the inner exception didn't
    # escape the snippet capture.
    assert result_details["error_type"] == "ExternalCallError"


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


# ===========================================================================
# write_merge_audit — the two-row agentic-merge audit (Sub Project A).
#
# Plan-blind spec source: synthesized-plan §4.4 + GATE-1 decision #2
# (external_call=1; reuse _insert_audit_row(action_type='write',
# resource_type='pull_request', external_endpoint='github',
# external_payload_hash=sha256(stable payload))), §5 test list item 6
# (two-row / intent-survives / never-stores-body / action_type∈enum /
# result-write-failure propagates), and the FROZEN INTERFACE CONTRACT
# (write_merge_audit keyword-only signature). Decision #9 (audited external
# call: external_call=1, hash-not-body) + "never store the body" govern.
#
# These reuse the file's own _audit_rows() helper + the isolated_settings
# fixture + init_databases() (the FTS5 notes_fts DDL runs at init — probed
# OK). RED on NotImplementedError (write_merge_audit is a stub) is correct.
#
# Pre-mortem coupling (premortem-digest skeptic-2 #riskiest-3 + R2): the two-
# row + intent-survives + propagates tests pin the predicted surprise
# "single-row leaves a phantom merge / gating blocks on a transient lock" —
# the contract is records-and-PROCEEDS (a write error is OBSERVABLE, NOT
# gates/un-merges the prior intent).
# ===========================================================================

# A fixed synthetic merge target (no real PR / no secret). Kept as explicitly-typed
# scalars (not a dict splat) so mypy --strict preserves the precise keyword types.
_PR_NUMBER: int = 6
_HEAD_SHA: str = "0123456789abcdef0123456789abcdef01234567"
_BASE_REF: str = "main"
# Strings that must NEVER appear in any stored operation_details (the body /
# gh argv / PR title — only the hash of the payload may be persisted).
_FORBIDDEN_BODY_TOKENS = (
    "gh pr merge",
    "--squash",
    "Sub Project A",  # a plausible PR title
    "gh ",
)


def _is_64_hex(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _merge_audit(
    phase: Literal["intent", "result"],
    status: Literal["success", "failure"] | None = None,
) -> int:
    """Call ``write_merge_audit`` for the fixed synthetic target (typed-keyword wrapper)."""
    return write_merge_audit(
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
        base_ref=_BASE_REF,
        phase=phase,
        status=status,
    )


def test_write_merge_audit_writes_two_rows_sharing_payload_hash(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """from: plan §4.4 + GATE-1 #2 + §5 item 6 (two-row).

    An intent row before the merge and a result row after — exactly two rows, both tagged
    external (external_call=1), action_type='write', resource_type='pull_request',
    external_endpoint='github', SHARING the same payload hash. row0 phase='intent',
    row1 phase='result'. This is the two-row shape mirrored from ``_audited_attempt``.
    """
    init_databases()
    _merge_audit("intent")
    _merge_audit("result", status="success")

    rows = _audit_rows()
    assert len(rows) == 2
    intent, result = rows
    # Both rows: external + write + pull_request + github endpoint.
    for row in (intent, result):
        action_type, resource_type, _resource_id, _details, external_call, endpoint, _hash = row
        assert action_type == "write"
        assert resource_type == "pull_request"
        assert external_call == 1
        assert endpoint == "github"
    # Both rows share one payload hash (the stable {pr, head_sha, base, squash} digest).
    assert intent[6] == result[6]
    assert _is_64_hex(intent[6])
    # The phases are ordered intent → result.
    assert json.loads(str(intent[3]))["phase"] == "intent"
    assert json.loads(str(result[3]))["phase"] == "result"
    assert json.loads(str(result[3]))["status"] == "success"


def test_write_merge_audit_intent_row_survives_without_result(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """from: plan §4.4 ("crashes mid-flight still leaves the intent row") + §5 item 6
    (intent-survives).

    When only the intent row is written (the result never follows — e.g. the process died
    between intent and the post-merge result write), exactly one row survives, and it is the
    intent row. The merge audit must never silently lose the fact that a merge was attempted.
    """
    init_databases()
    _merge_audit("intent")

    rows = _audit_rows()
    assert len(rows) == 1
    assert json.loads(str(rows[0][3]))["phase"] == "intent"
    assert rows[0][4] == 1  # external_call
    assert rows[0][0] == "write"  # action_type


def test_write_merge_audit_never_stores_body(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """from: decision #9 + "never store the body" + §5 item 6 (never-stores-body).

    The payload hash is a 64-hex sha256, and NO row's operation_details contains the merge
    body, the ``gh`` argv, or the PR title — only the hash of the payload may be persisted
    (CLAUDE.md "Never store the body of an external request — only the hash").
    """
    init_databases()
    _merge_audit("intent")
    _merge_audit("result", status="success")

    rows = _audit_rows()
    for row in rows:
        payload_hash = row[6]
        assert _is_64_hex(payload_hash), f"payload hash not 64-hex sha256: {payload_hash!r}"
        details_text = str(row[3])
        for token in _FORBIDDEN_BODY_TOKENS:
            assert token not in details_text, (
                f"operation_details leaked a body/argv/title token {token!r}: {details_text!r}"
            )


def test_write_merge_audit_action_type_is_write_and_passes_check(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """from: plan §4.4 (action_type='write' forced — the enum has no 'merge') + §5 item 6
    (action_type∈enum).

    The audit_log.action_type CHECK enum (ddl) has no 'merge', so the merge audit uses
    'write'. The INSERT must SUCCEED (no CHECK-constraint violation) and the stored
    action_type is 'write'. (If the body had passed action_type='merge', the INSERT would
    raise an IntegrityError — this proves it does not.)
    """
    init_databases()
    log_id = _merge_audit("intent")
    # The helper returns the inserted log_id (mirrors write_config_change_audit's >0 contract).
    assert log_id > 0
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0][0] == "write"


def test_write_merge_audit_result_write_failure_propagates(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: plan §4.4 + §5 item 6 (result-write-failure propagates — OBSERVABLE, NOT gating).

    The contract is "a write error is OBSERVABLE", NOT "it gates/un-merges the prior intent".
    Monkeypatch the named collaborator ``_insert_audit_row`` to raise, then call
    ``write_merge_audit(phase='result', ...)`` — the error must RE-RAISE (propagate), so a
    failed result-row write is never swallowed. (The prior intent row, written by a real
    earlier call, legitimately remains — the result-write failure does NOT roll it back / un-
    merge; this test asserts only that the failure is surfaced, not that anything is gated.)
    """
    init_databases()
    # A genuine intent row first (a real merge that already happened upstream).
    _merge_audit("intent")
    assert len(_audit_rows()) == 1

    sentinel = "result-row INSERT boom"

    def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError(sentinel)

    # Patch the collaborator named in the frozen contract (interface-level, not body logic).
    monkeypatch.setattr(
        "genome.privacy.external_client._insert_audit_row",
        _boom,
    )
    with pytest.raises(RuntimeError, match=sentinel):
        _merge_audit("result", status="success")

    # The intent row is not rolled back by the result-write failure (records-and-proceeds:
    # the failure is observable, the prior merge fact is preserved — NOT gated/un-merged).
    assert len(_audit_rows()) == 1
    assert json.loads(str(_audit_rows()[0][3]))["phase"] == "intent"


def test_write_merge_audit_failure_result_stores_truncated_error(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Stage-3 D-coverage: a ``phase='result'`` row with an ``error=`` stores the error in
    ``operation_details`` (so a failed merge is diagnosable from the log), truncated to a
    bounded length, and still never leaks a body token or stores anything but the hash.
    """
    init_databases()
    long_error = "boom " * 300  # 1500 chars — must be truncated by the helper
    write_merge_audit(
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
        base_ref=_BASE_REF,
        phase="result",
        status="failure",
        error=long_error,
    )
    rows = _audit_rows()
    assert len(rows) == 1
    details = json.loads(str(rows[0][3]))
    assert details["phase"] == "result"
    assert details["status"] == "failure"
    assert "error" in details
    # Truncated (bounded), and a prefix of the supplied error.
    assert len(details["error"]) <= 500
    assert long_error.startswith(details["error"])
    # Still hash-only + no body token leak.
    assert _is_64_hex(rows[0][6])
    for token in _FORBIDDEN_BODY_TOKENS:
        assert token not in str(rows[0][3])
