"""Tests for :mod:`genome.annotate.downloads`.

Covers the on-disk cache resolver and the audited-download wrapper.
The audited path is exercised against a mocked ``httpx`` transport so
no real network call is made; the disabled-master-switch path is
exercised against the real :class:`ExternalClient` so the audit-row
intent/blocked pair is verified for the annotations workflow.
"""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import httpx
import pytest

from genome.annotate.downloads import (
    DownloadResult,
    default_annotations_root,
    download_to_cache,
    source_download_dir,
)
from genome.db import init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.privacy.external_client import ExternalCallsDisabledError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def annotations_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> Iterator[Path]:
    """Point ``settings.annotations_download_root`` at a tmp directory.

    Note that no directory is created up-front — tests that assert
    "the cache root does not exist until a download happens" rely on
    this.
    """
    root = tmp_path / "annotations-root"
    monkeypatch.setenv("ANNOTATIONS_DOWNLOAD_ROOT", str(root))
    from genome.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        yield root
    finally:
        get_settings.cache_clear()


@pytest.fixture
def mock_transport(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[httpx.Request]]:
    """Patch ``httpx.Client.__init__`` to always inject a MockTransport."""
    captured: dict[str, list[httpx.Request]] = {"requests": []}
    payload = b"CLINVAR_VCF_BYTES_v1"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["requests"].append(request)
        return httpx.Response(200, content=payload)

    transport = httpx.MockTransport(handler)
    real_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    return captured


def _enable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='true' WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _audit_rows() -> list[tuple[object, ...]]:
    with sqlcipher_connection() as conn:
        return conn.execute(
            "SELECT action_type, resource_type, resource_id, operation_details,"
            " external_call, external_endpoint, external_payload_hash"
            " FROM audit_log ORDER BY log_id",
        ).fetchall()


# -----------------------------------------------------------------------------
# default_annotations_root / source_download_dir
# -----------------------------------------------------------------------------


def test_default_annotations_root_falls_back_to_home_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("APP_DB_PASSPHRASE", "x")
    monkeypatch.delenv("ANNOTATIONS_DOWNLOAD_ROOT", raising=False)
    from genome.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        root = default_annotations_root()
    finally:
        get_settings.cache_clear()
    assert root == fake_home / ".cache" / "genome" / "annotations"


def test_default_annotations_root_respects_settings_override(
    annotations_root: Path,
) -> None:
    assert default_annotations_root() == annotations_root


def test_default_annotations_root_does_not_create_the_directory(
    annotations_root: Path,
) -> None:
    """Resolving the root path must not create the on-disk cache.

    This is the contract that lets ``genome annotate status`` run on a
    fresh checkout without materializing ``~/.cache/genome/annotations/``.
    """
    _ = default_annotations_root()
    assert not annotations_root.exists()


def test_source_download_dir_creates_the_directory(
    annotations_root: Path,
) -> None:
    target = source_download_dir("clinvar")
    assert target == annotations_root / "clinvar"
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    # Root dir itself was also created with 0700.
    assert annotations_root.is_dir()
    assert stat.S_IMODE(annotations_root.stat().st_mode) == 0o700


# -----------------------------------------------------------------------------
# download_to_cache — happy path / idempotence / force
# -----------------------------------------------------------------------------


def test_download_to_cache_first_call_downloads_and_returns_result(
    annotations_root: Path,
    mock_transport: dict[str, list[httpx.Request]],
) -> None:
    init_databases()
    _enable_external_calls()
    result = download_to_cache(
        "clinvar",
        "https://example.invalid/clinvar.vcf.gz",
        "clinvar.vcf.gz",
        resource_id="clinvar_full",
    )
    assert isinstance(result, DownloadResult)
    assert result.path == annotations_root / "clinvar" / "clinvar.vcf.gz"
    assert result.path.is_file()
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert result.size_bytes == len(b"CLINVAR_VCF_BYTES_v1")
    # Exactly one request went through the mocked transport.
    assert len(mock_transport["requests"]) == 1


def test_download_to_cache_skip_when_file_exists(
    annotations_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],
) -> None:
    init_databases()
    _enable_external_calls()
    first = download_to_cache(
        "clinvar",
        "https://example.invalid/clinvar.vcf.gz",
        "clinvar.vcf.gz",
        resource_id="clinvar_full",
    )
    requests_after_first = len(mock_transport["requests"])
    second = download_to_cache(
        "clinvar",
        "https://example.invalid/clinvar.vcf.gz",
        "clinvar.vcf.gz",
        resource_id="clinvar_full",
    )
    # No new external call.
    assert len(mock_transport["requests"]) == requests_after_first
    # Same hash because the cached file is re-hashed locally.
    assert first.sha256 == second.sha256
    assert first.path == second.path


def test_download_to_cache_force_redownloads(
    annotations_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],
) -> None:
    init_databases()
    _enable_external_calls()
    download_to_cache(
        "clinvar",
        "https://example.invalid/clinvar.vcf.gz",
        "clinvar.vcf.gz",
        resource_id="clinvar_full",
    )
    first_count = len(mock_transport["requests"])
    download_to_cache(
        "clinvar",
        "https://example.invalid/clinvar.vcf.gz",
        "clinvar.vcf.gz",
        resource_id="clinvar_full",
        force=True,
    )
    assert len(mock_transport["requests"]) == first_count + 1


def test_download_to_cache_uses_annotations_endpoint_label(
    annotations_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],  # noqa: ARG001 — keep the patch active
) -> None:
    """Each annotation download labels its audit row ``annotations_<source>``.

    This is what lets an operator query the audit log per source DB
    (`SELECT * FROM audit_log WHERE external_endpoint = 'annotations_clinvar'`).
    """
    init_databases()
    _enable_external_calls()
    download_to_cache(
        "clinvar",
        "https://example.invalid/clinvar.vcf.gz",
        "clinvar.vcf.gz",
        resource_id="clinvar_full",
    )
    rows = _audit_rows()
    annotations_rows = [r for r in rows if str(r[5] or "").startswith("annotations_")]
    assert annotations_rows, "no audit row used the annotations_<source> endpoint label"
    endpoints = {r[5] for r in annotations_rows}
    assert endpoints == {"annotations_clinvar"}
    resource_types = {r[1] for r in annotations_rows}
    assert resource_types == {"annotation_source"}
    resource_ids = {r[2] for r in annotations_rows}
    assert resource_ids == {"clinvar_full"}


# -----------------------------------------------------------------------------
# Disabled master switch path (regression test for PR #29)
# -----------------------------------------------------------------------------


def test_download_to_cache_blocked_when_external_calls_disabled(
    annotations_root: Path,
    mock_transport: dict[str, list[httpx.Request]],  # noqa: ARG001 — keep the patch active
) -> None:
    """External-calls master switch is fail-closed for annotations too.

    Mirrors the behaviour asserted in PR #29 for the imputation panel:
    a disabled switch raises :class:`ExternalCallsDisabledError` *and*
    leaves an intent + blocked audit pair so the privacy-relevant
    blocked attempts are still durably recorded.
    """
    init_databases()
    # ``init_databases`` seeds external_calls_enabled=false; do not flip it.

    with pytest.raises(ExternalCallsDisabledError):
        download_to_cache(
            "clinvar",
            "https://example.invalid/clinvar.vcf.gz",
            "clinvar.vcf.gz",
            resource_id="clinvar_full",
        )
    # File was not written to disk.
    assert not (annotations_root / "clinvar" / "clinvar.vcf.gz").exists()

    rows = _audit_rows()
    assert len(rows) == 2
    intent, blocked = rows
    intent_details = json.loads(str(intent[3]))
    blocked_details = json.loads(str(blocked[3]))
    assert intent_details["phase"] == "intent"
    assert blocked_details["status"] == "blocked"
    assert intent[1] == blocked[1] == "annotation_source"
    assert intent[2] == blocked[2] == "clinvar_full"
    assert intent[5] == blocked[5] == "annotations_clinvar"
