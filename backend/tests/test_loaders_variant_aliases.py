"""Tests for :mod:`genome.annotate.loaders.variant_aliases`.

Covers the RsMergeArch column projection (``rsHigh`` -> ``alias_rsid``,
``rsCurrent`` -> ``current_rsid``, bare-int -> ``rs`` prefix,
``alias_type='merged'``), the both-sided user filter (kept when the user carries
either the merged-away or the surviving rsID), dedup on ``alias_rsid``,
self-merge and malformed-row skips, the same-epoch attach (rows land under the
current dbSNP ``source_version_id`` with no pointer flip and no
``annotation_source_versions`` mutation), the no-dbSNP-loaded error, the re-run
short-circuit / ``--force`` DELETE+re-INSERT, the external-calls-disabled audited
refusal, and the CLI surface.

The synthetic ``RsMergeArch.bcp.gz`` is built with real column layout; the
network download is monkeypatched so the tests touch neither NCBI nor
``~/.cache``.
"""

from __future__ import annotations

import gzip
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

import genome.annotate.loaders.variant_aliases as va_loader
from genome.annotate import downloads
from genome.annotate.downloads import DownloadResult
from genome.annotate.loaders.variant_aliases import (
    DbsnpNotLoadedError,
    refresh_aliases,
)
from genome.annotate.source_versions import get_current_version, insert_source_version
from genome.annotate.supersession import flip_to_new_version
from genome.cli import app
from genome.db import duckdb_connection, init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.privacy.external_client import ExternalCallsDisabledError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(
    isolated_settings: dict[str, str],  # noqa: ARG001 — activates the tmp-dir settings
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    yield
    structlog.reset_defaults()
    monkeypatch.undo()


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


def _seed_dbsnp_version(conn, *, record_count: int = 42) -> int:  # type: ignore[no-untyped-def]
    """Establish a current dbSNP epoch (as the VCF load would) and return its id."""
    svid = insert_source_version(
        conn,
        source_db="dbsnp",
        version="157",
        source_url="https://example/dbsnp.vcf.gz",
        source_file_hash="dbsnp_157",
        source_file_size=0,
        record_count=record_count,
    )
    flip_to_new_version(
        conn,
        source="dbsnp",
        table="dbsnp_annotations",
        new_source_version_id=svid,
    )
    return svid


def _seed_user_rsids(conn, rsids: list[str | None]) -> None:  # type: ignore[no-untyped-def]
    for i, rsid in enumerate(rsids):
        conn.execute(
            """
            INSERT INTO variants_master (chrom, pos_grch38, ref_allele, alt_allele, rsid)
            VALUES ('1'::chromosome_enum, ?, 'A', 'C', ?)
            """,
            [1000 + i, rsid],
        )


def _merge_line(rs_high: object, rs_current: object) -> str:
    """One RsMergeArch.bcp row: 9 tab-separated columns, rsCurrent at index 6."""
    return "\t".join(
        [
            str(rs_high),  # 0 rsHigh
            str(rs_high),  # 1 rsLow
            "151",  # 2 build_id
            "0",  # 3 orien
            "2018-01-01 00:00:00",  # 4 create_time
            "2018-01-01 00:00:00",  # 5 last_updated_time
            str(rs_current),  # 6 rsCurrent
            "0",  # 7 orien2Current
            "",  # 8 comment
        ],
    )


def _write_gz(path: Path, lines: list[str]) -> Path:
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    path.write_bytes(gzip.compress(payload))
    return path


class _DownloadSpy:
    """Stand-in for ``download_to_cache`` returning a pre-written gz; counts calls."""

    def __init__(self, gz_path: Path) -> None:
        self.gz_path = gz_path
        self.calls = 0

    def __call__(self, source_db, url, filename, *, resource_id, force=False):  # type: ignore[no-untyped-def]  # noqa: ARG002
        self.calls += 1
        return DownloadResult(
            path=self.gz_path,
            sha256="a" * 64,
            size_bytes=self.gz_path.stat().st_size,
        )


def _patch_download(monkeypatch: pytest.MonkeyPatch, spy: _DownloadSpy) -> None:
    monkeypatch.setattr(va_loader, "download_to_cache", spy)


def _alias_rows(conn, svid: int) -> dict[str, tuple[str, str]]:  # type: ignore[no-untyped-def]
    rows = conn.execute(
        "SELECT alias_rsid, current_rsid, alias_type FROM variant_aliases"
        " WHERE source_version_id = ?",
        [svid],
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


# ---------------------------------------------------------------------------
# Filter + projection
# ---------------------------------------------------------------------------


def test_keeps_merge_when_user_carries_old_rsid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User carries the merged-away rsID -> the merge row lands (primary lift)."""
    init_databases()
    gz = _write_gz(tmp_path / "rma.gz", [_merge_line(100, 200)])
    _patch_download(monkeypatch, _DownloadSpy(gz))
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_user_rsids(conn, ["rs100"])
        result = refresh_aliases(conn)
        aliases = _alias_rows(conn, svid)
    assert result.rows_loaded == 1
    assert aliases == {"rs100": ("rs200", "merged")}
    assert result.user_old_rsid_hits == 1
    assert result.user_current_rsid_hits == 0


def test_keeps_merge_when_user_carries_current_rsid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User carries the surviving rsID -> the merge row still lands (other side)."""
    init_databases()
    gz = _write_gz(tmp_path / "rma.gz", [_merge_line(100, 200)])
    _patch_download(monkeypatch, _DownloadSpy(gz))
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_user_rsids(conn, ["rs200"])
        result = refresh_aliases(conn)
        aliases = _alias_rows(conn, svid)
    assert aliases == {"rs100": ("rs200", "merged")}
    assert result.user_old_rsid_hits == 0
    assert result.user_current_rsid_hits == 1


def test_drops_merge_unrelated_to_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    gz = _write_gz(tmp_path / "rma.gz", [_merge_line(100, 200)])
    _patch_download(monkeypatch, _DownloadSpy(gz))
    with duckdb_connection() as conn:
        _seed_dbsnp_version(conn)
        _seed_user_rsids(conn, ["rs999"])
        result = refresh_aliases(conn)
    assert result.rows_loaded == 0


def test_self_merge_and_malformed_rows_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rsHigh==rsCurrent, short rows, and non-numeric rows are dropped defensively."""
    init_databases()
    gz = _write_gz(
        tmp_path / "rma.gz",
        [
            _merge_line(100, 100),  # self-merge -> skip
            "100\t100\t151",  # too few columns -> skip
            _merge_line("abc", 200),  # non-numeric rsHigh -> skip
            _merge_line(300, 400),  # valid, user carries rs300
        ],
    )
    _patch_download(monkeypatch, _DownloadSpy(gz))
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_user_rsids(conn, ["rs100", "rs300"])
        result = refresh_aliases(conn)
        aliases = _alias_rows(conn, svid)
    assert result.rows_loaded == 1
    assert aliases == {"rs300": ("rs400", "merged")}


def test_dedup_on_alias_rsid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated rsHigh keeps the first survivor only (one row per merged-away rsID)."""
    init_databases()
    gz = _write_gz(tmp_path / "rma.gz", [_merge_line(100, 200), _merge_line(100, 300)])
    _patch_download(monkeypatch, _DownloadSpy(gz))
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_user_rsids(conn, ["rs100"])
        result = refresh_aliases(conn)
        aliases = _alias_rows(conn, svid)
    assert result.rows_loaded == 1
    assert aliases == {"rs100": ("rs200", "merged")}


def test_drift_identifiers_both_sides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """user_old / user_current hits count distinct user rsIDs on each side."""
    init_databases()
    gz = _write_gz(tmp_path / "rma.gz", [_merge_line(100, 200), _merge_line(400, 500)])
    _patch_download(monkeypatch, _DownloadSpy(gz))
    with duckdb_connection() as conn:
        _seed_dbsnp_version(conn)
        _seed_user_rsids(conn, ["rs100", "rs500"])  # rs100 = old side; rs500 = current side
        result = refresh_aliases(conn)
    assert result.rows_loaded == 2
    assert result.distinct_alias_rsid == 2
    assert result.distinct_current_rsid == 2
    assert result.user_old_rsid_hits == 1
    assert result.user_current_rsid_hits == 1


# ---------------------------------------------------------------------------
# Same-epoch attach (no pointer flip, no version-row mutation)
# ---------------------------------------------------------------------------


def test_rows_attach_to_current_dbsnp_svid_without_flip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    gz = _write_gz(tmp_path / "rma.gz", [_merge_line(100, 200)])
    _patch_download(monkeypatch, _DownloadSpy(gz))
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn, record_count=42)
        _seed_user_rsids(conn, ["rs100"])
        result = refresh_aliases(conn)

        # Aliases live under the dbSNP epoch the pointer already names.
        assert result.target_source_version_id == svid
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='dbsnp'",
        ).fetchone()
        assert pointer is not None
        assert pointer[0] == svid

        # The shared annotation_source_versions row is untouched (record_count
        # belongs to dbsnp_annotations, not the alias backfill).
        rc = conn.execute(
            "SELECT record_count FROM annotation_source_versions WHERE source_version_id = ?",
            [svid],
        ).fetchone()
        assert rc is not None
        assert rc[0] == 42

        # No new dbsnp source-version was allocated.
        n_versions = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db='dbsnp'",
        ).fetchone()
        assert n_versions is not None
        assert n_versions[0] == 1

        current = get_current_version(conn, "dbsnp")
        assert current is not None
        assert current.source_version_id == svid


def test_no_dbsnp_loaded_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    gz = _write_gz(tmp_path / "rma.gz", [_merge_line(100, 200)])
    spy = _DownloadSpy(gz)
    _patch_download(monkeypatch, spy)
    with duckdb_connection() as conn:
        _seed_user_rsids(conn, ["rs100"])
        with pytest.raises(DbsnpNotLoadedError):
            refresh_aliases(conn)
    # Fail-fast: never reached the download.
    assert spy.calls == 0


# ---------------------------------------------------------------------------
# Re-run semantics
# ---------------------------------------------------------------------------


def test_rerun_without_force_short_circuits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    gz = _write_gz(tmp_path / "rma.gz", [_merge_line(100, 200)])
    spy = _DownloadSpy(gz)
    _patch_download(monkeypatch, spy)
    with duckdb_connection() as conn:
        _seed_dbsnp_version(conn)
        _seed_user_rsids(conn, ["rs100"])
        first = refresh_aliases(conn)
        assert first.rows_loaded == 1
        assert spy.calls == 1

        second = refresh_aliases(conn)
        assert second.already_populated is True
        assert second.rows_loaded == 1
        # Short-circuit: no second download, no re-write.
        assert spy.calls == 1


def test_force_rerun_replaces_under_same_svid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    gz = tmp_path / "rma.gz"
    _write_gz(gz, [_merge_line(100, 200)])
    spy = _DownloadSpy(gz)
    _patch_download(monkeypatch, spy)
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_user_rsids(conn, ["rs100", "rs300"])
        refresh_aliases(conn)

        # Upstream "changes": rs100 now resolves elsewhere and rs300 gains a merge.
        _write_gz(gz, [_merge_line(100, 999), _merge_line(300, 400)])
        result = refresh_aliases(conn, force=True)
        aliases = _alias_rows(conn, svid)

    assert spy.calls == 2
    assert result.already_populated is False
    assert result.rows_loaded == 2
    assert aliases == {"rs100": ("rs999", "merged"), "rs300": ("rs400", "merged")}


# ---------------------------------------------------------------------------
# External-calls-disabled
# ---------------------------------------------------------------------------


def test_external_calls_disabled_blocks_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Disabled switch on an un-cached file -> audited refusal, table stays empty."""
    init_databases()
    # Keep the real download_to_cache (to exercise gating) but redirect the cache
    # off ~/.cache and onto tmp.
    monkeypatch.setattr(downloads, "default_annotations_root", lambda: tmp_path / "annotations")
    with duckdb_connection() as conn:
        _seed_dbsnp_version(conn)
        _seed_user_rsids(conn, ["rs100"])
        # init_databases seeds external_calls_enabled=false; do not flip it.
        with pytest.raises(ExternalCallsDisabledError):
            refresh_aliases(conn)
        count = conn.execute("SELECT COUNT(*) FROM variant_aliases").fetchone()
    assert count is not None
    assert count[0] == 0
    rows = _audit_rows()
    # One intent + one blocked row for the dbsnp_rsmergearch download attempt.
    merge_rows = [r for r in rows if r[2] == "dbsnp_rsmergearch"]
    assert len(merge_rows) >= 2  # intent + blocked


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_refresh_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    gz = _write_gz(tmp_path / "rma.gz", [_merge_line(100, 200)])
    _patch_download(monkeypatch, _DownloadSpy(gz))
    with duckdb_connection() as conn:
        _seed_dbsnp_version(conn)
        _seed_user_rsids(conn, ["rs100"])

    result = CliRunner().invoke(app, ["annotate", "refresh-aliases"])
    assert result.exit_code == 0, result.output
    assert "variant_aliases populated" in result.output
    assert "rows=1" in result.output
    assert "user_old_rsid_hits=1" in result.output


def test_cli_refresh_aliases_no_dbsnp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    gz = _write_gz(tmp_path / "rma.gz", [_merge_line(100, 200)])
    _patch_download(monkeypatch, _DownloadSpy(gz))
    with duckdb_connection() as conn:
        _seed_user_rsids(conn, ["rs100"])

    result = CliRunner().invoke(app, ["annotate", "refresh-aliases"])
    assert result.exit_code == 2  # Typer Exit(code=2)
    assert "load the dbSNP VCF first" in result.output
