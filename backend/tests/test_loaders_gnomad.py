"""Tests for :mod:`genome.annotate.loaders.gnomad`.

Covers the pure helpers (``_coalesce_positions``, ``_record_to_row``),
the three-way intersection SQL (``_build_filter_set``), the
per-chromosome iteration with dedup across exomes/genomes, the
pre-flight libcurl check, the audited refusal path, the version
short-circuit, ``--force`` re-allocation, ``--resume`` continuation,
partial-failure isolation, the partial-chromosomes-no-flip semantic,
audit-event accounting, and AF-bucket / per-population counters.

cyvcf2 is mocked at module load time so tests don't touch the network.
The ExternalClient passes through a real httpx Client wired to a
MockTransport handler that returns 200 on every audited HEAD.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pyarrow.parquet as pq
import pytest
import structlog
import typer
from typer.testing import CliRunner

from genome.annotate.loaders import gnomad as gnomad_loader
from genome.annotate.loaders.gnomad import (
    _POP_TO_VCF_INFO_SUFFIX,
    DEFAULT_COALESCE_DISTANCE_BP,
    GNOMAD_POPULATIONS,
    GNOMAD_URL_TEMPLATE,
    GNOMAD_VERSION,
    MAX_REMOTE_REGION_ATTEMPTS,
    SOURCE_DB,
    SUPPORTED_CHROMS,
    GnomadLibcurlMissingError,
    GnomadRemoteIterationError,
    _build_filter_set,
    _ChromResult,
    _ChromTask,
    _coalesce_positions,
    _merge_chromosome_parquet,
    _record_to_row,
    _scan_for_htslib_errors,
    _stream_chromosome_to_parquet,
    load,
)
from genome.annotate.source_versions import insert_source_version
from genome.annotate.supersession import flip_to_new_version
from genome.cli import app
from genome.db import duckdb_connection, init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.privacy.external_client import (
    _DEFAULT_TIMEOUT_S,
    ExternalCallsDisabledError,
    ExternalClient,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures: fake cyvcf2.VCF + record shapes.
# ---------------------------------------------------------------------------


@dataclass
class FakeVariant:
    """Stand-in for a cyvcf2 record.

    Mirrors the attributes :func:`_record_to_row` reads:
    ``CHROM``, ``POS``, ``REF``, ``ALT``, ``FILTER`` (``None`` means
    PASS), and ``INFO`` (a dict; cyvcf2's ``INFO`` raises KeyError
    on missing keys, so the fake mimics that by being a real ``dict``
    subclass).
    """

    CHROM: str
    POS: int
    REF: str
    ALT: tuple[str, ...]
    INFO: dict[str, object] = field(default_factory=dict)
    FILTER: str | None = None


@dataclass
class _FakeVCF:
    """Stand-in for a cyvcf2.VCF.

    The ``__call__`` method returns the configured records whose
    position is inside the queried region.
    """

    records: list[FakeVariant]
    closed: bool = False
    opens: list[str] = field(default_factory=list)

    def __call__(self, region: str) -> Iterable[FakeVariant]:
        self.opens.append(region)
        match = re.match(r"chr([^:]+):(\d+)-(\d+)", region)
        if match is None:
            return iter(())
        chrom = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3))
        return (
            r
            for r in self.records
            if (r.CHROM.removeprefix("chr") == chrom) and start <= r.POS <= end
        )

    def close(self) -> None:
        self.closed = True


class _VCFFactory:
    """Callable that builds :class:`_FakeVCF` per URL.

    Maps URL → list[FakeVariant]; the same factory instance is shared
    across exomes + genomes URLs so tests can verify dedup semantics.
    """

    def __init__(self, by_url: dict[str, list[FakeVariant]]) -> None:
        self.by_url = by_url
        self.openings: list[str] = []

    def __call__(self, url: str) -> _FakeVCF:
        self.openings.append(url)
        return _FakeVCF(records=list(self.by_url.get(url, [])))


@pytest.fixture(autouse=True)
def _isolated(
    isolated_settings: dict[str, str],  # noqa: ARG001 — required by the fixture chain
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Per-test isolation: fresh env + reset structlog so capture_logs is clean."""
    yield
    structlog.reset_defaults()
    monkeypatch.undo()


@pytest.fixture(autouse=True)
def _ensure_gnomad_registered() -> Iterator[None]:
    """Re-register the loader at test start so the registry survives any test order."""
    from genome.annotate.registry import _LOADERS, register_loader  # noqa: PLC0415

    _LOADERS.pop("gnomad", None)
    register_loader("gnomad", gnomad_loader.refresh)
    try:
        yield
    finally:
        _LOADERS.pop("gnomad", None)


def _patch_cyvcf2(
    monkeypatch: pytest.MonkeyPatch,
    factory: _VCFFactory,
) -> None:
    """Replace ``cyvcf2.VCF`` with ``factory``.

    The loader's two cyvcf2 entry points (``_check_libcurl_available``
    and ``_load_chromosome``) both use local imports of ``from cyvcf2
    import VCF``, so monkeypatching ``cyvcf2.VCF`` reaches both.
    """
    import cyvcf2  # noqa: PLC0415 — test fixture local import

    monkeypatch.setattr(cyvcf2, "VCF", factory)


def _enable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='true' WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _mock_audited_client() -> tuple[ExternalClient, httpx.Client]:
    """Build an audited client wired to a MockTransport returning 200.

    Returns the client + the underlying httpx so callers can close it
    after use. The transport handles GET / HEAD on any URL with an
    empty 200 body.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "0"})

    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=_DEFAULT_TIMEOUT_S)
    return ExternalClient(f"annotations_{SOURCE_DB}", client=http), http


def _audit_rows() -> list[tuple[object, ...]]:
    with sqlcipher_connection() as conn:
        return conn.execute(
            "SELECT action_type, resource_type, resource_id, operation_details,"
            " external_call, external_endpoint, external_payload_hash"
            " FROM audit_log ORDER BY log_id",
        ).fetchall()


def _seed_clinvar_active(
    conn,  # type: ignore[no-untyped-def]
    *,
    rows: list[tuple[str, int]],
    version: str = "2026_05_10",
) -> int:
    """Seed clinvar_annotations + active source-version pointer."""
    sv_id = insert_source_version(
        conn,
        source_db="clinvar",
        version=version,
        source_url=None,
        source_file_hash="c" * 64,
        source_file_size=1,
        record_count=len(rows),
    )
    base_row = conn.execute(
        "SELECT COALESCE(MAX(clinvar_id), 0) FROM clinvar_annotations",
    ).fetchone()
    base = (int(base_row[0]) if base_row is not None else 0) + 1
    for i, (chrom, pos) in enumerate(rows):
        conn.execute(
            """
            INSERT INTO clinvar_annotations (
                clinvar_id, variation_id, chrom, pos_grch38,
                source_version_id, retrieval_date
            )
            VALUES (?, ?, ?::chromosome_enum, ?, ?, CURRENT_TIMESTAMP)
            """,
            [base + i, f"CV{base + i}", chrom, pos, sv_id],
        )
    flip_to_new_version(
        conn,
        source="clinvar",
        table="clinvar_annotations",
        new_source_version_id=sv_id,
    )
    return sv_id


def _seed_gwas_active(
    conn,  # type: ignore[no-untyped-def]
    *,
    rows: list[tuple[str, int]],
    version: str = "2026_05_16",
) -> int:
    """Seed gwas_catalog_associations + active source-version pointer."""
    sv_id = insert_source_version(
        conn,
        source_db="gwas_catalog",
        version=version,
        source_url=None,
        source_file_hash="g" * 64,
        source_file_size=1,
        record_count=len(rows),
    )
    base_row = conn.execute(
        "SELECT COALESCE(MAX(association_id), 0) FROM gwas_catalog_associations",
    ).fetchone()
    base = (int(base_row[0]) if base_row is not None else 0) + 1
    for i, (chrom, pos) in enumerate(rows):
        conn.execute(
            """
            INSERT INTO gwas_catalog_associations (
                association_id, study_accession, rsid, chrom, pos_grch38,
                source_version_id, retrieval_date
            )
            VALUES (?, ?, ?, ?::chromosome_enum, ?, ?, CURRENT_TIMESTAMP)
            """,
            [base + i, f"GCST{base + i}", f"rs{1000 + base + i}", chrom, pos, sv_id],
        )
    flip_to_new_version(
        conn,
        source="gwas_catalog",
        table="gwas_catalog_associations",
        new_source_version_id=sv_id,
    )
    return sv_id


def _seed_user_variants(
    conn,  # type: ignore[no-untyped-def]
    rows: list[tuple[str, int]],
) -> None:
    """Insert rows into variants_master."""
    for chrom, pos in rows:
        conn.execute(
            """
            INSERT INTO variants_master (
                chrom, pos_grch38, ref_allele, alt_allele
            )
            VALUES (?::chromosome_enum, ?, 'A', 'C')
            """,
            [chrom, pos],
        )


# ---------------------------------------------------------------------------
# Pre-flight tests
# ---------------------------------------------------------------------------


def test_preflight_libcurl_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mocked VCF open + iteration succeeds → no exception."""
    factory = _VCFFactory(by_url={})
    _patch_cyvcf2(monkeypatch, factory)
    # Should not raise.
    gnomad_loader._check_libcurl_available()  # noqa: SLF001


def test_preflight_libcurl_fail_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cyvcf2 open failure → GnomadLibcurlMissingError with libcurl note."""
    import cyvcf2  # noqa: PLC0415

    def _explode(_url: str) -> _FakeVCF:
        msg = "tabix index failed (htslib without libcurl)"
        raise RuntimeError(msg)

    monkeypatch.setattr(cyvcf2, "VCF", _explode)
    with pytest.raises(GnomadLibcurlMissingError, match="libcurl"):
        gnomad_loader._check_libcurl_available()  # noqa: SLF001


# ---------------------------------------------------------------------------
# _build_filter_set
# ---------------------------------------------------------------------------


def test_build_filter_set_three_way_intersection() -> None:
    """Union across user + ClinVar + GWAS produces sorted unique positions."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("1", 100), ("1", 200), ("2", 300)])
        _seed_clinvar_active(conn, rows=[("1", 200), ("1", 400)])
        _seed_gwas_active(conn, rows=[("2", 300), ("X", 500)])
        result = _build_filter_set(conn)
    assert result.positions["1"] == [100, 200, 400]
    assert result.positions["2"] == [300]
    assert result.positions["X"] == [500]
    # Y / MT not in SUPPORTED_CHROMS — keys absent (chrom not in dict).
    assert "Y" not in result.positions
    assert "MT" not in result.positions
    expected_total = 5
    assert result.composition == {
        "user": 3,
        "clinvar": 2,
        "gwas": 2,
        "union_total": expected_total,
    }


def test_build_filter_set_excludes_inactive_variants_master() -> None:
    """Every variants_master row counts — no per-row activity filter exists."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("1", 1000)])
        result = _build_filter_set(conn)
    # No-active flag on variants_master — every row contributes.
    assert result.positions["1"] == [1000]
    assert result.composition["user"] == 1


def test_build_filter_set_excludes_non_active_clinvar_version() -> None:
    """ClinVar rows under a non-current source_version_id do not contribute."""
    init_databases()
    with duckdb_connection() as conn:
        old_id = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_01",
            source_url=None,
            source_file_hash="x" * 64,
            source_file_size=1,
            record_count=1,
        )
        conn.execute(
            """
            INSERT INTO clinvar_annotations (
                clinvar_id, variation_id, chrom, pos_grch38,
                source_version_id, retrieval_date
            )
            VALUES (1, 'CV1', '1'::chromosome_enum, 999, ?, CURRENT_TIMESTAMP)
            """,
            [old_id],
        )
        # Flip pointer to a DIFFERENT version with no rows under it.
        new_id = _seed_clinvar_active(conn, rows=[("1", 111)])
        assert new_id != old_id
        result = _build_filter_set(conn)
    assert result.positions["1"] == [111]
    assert 999 not in result.positions["1"]


def test_build_filter_set_excludes_non_active_gwas_version() -> None:
    """GWAS rows under a non-current source_version_id do not contribute."""
    init_databases()
    with duckdb_connection() as conn:
        old_id = insert_source_version(
            conn,
            source_db="gwas_catalog",
            version="2026_04_01",
            source_url=None,
            source_file_hash="y" * 64,
            source_file_size=1,
            record_count=1,
        )
        conn.execute(
            """
            INSERT INTO gwas_catalog_associations (
                association_id, study_accession, rsid, chrom, pos_grch38,
                source_version_id, retrieval_date
            )
            VALUES (1, 'GCST0', 'rs0', '1'::chromosome_enum, 888, ?, CURRENT_TIMESTAMP)
            """,
            [old_id],
        )
        new_id = _seed_gwas_active(conn, rows=[("1", 222)])
        assert new_id != old_id
        result = _build_filter_set(conn)
    assert result.positions["1"] == [222]
    assert 888 not in result.positions["1"]


def test_build_filter_set_restricts_to_supported_chroms() -> None:
    """Y / MT positions are filtered out even if present in source tables."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("Y", 100), ("MT", 200), ("X", 300)])
        result = _build_filter_set(conn)
    assert "Y" not in result.positions
    assert "MT" not in result.positions
    assert result.positions["X"] == [300]


# ---------------------------------------------------------------------------
# _coalesce_positions
# ---------------------------------------------------------------------------


def test_coalesce_positions_default_1kb() -> None:
    """Adjacent positions within 1 kb merge; positions >1 kb apart split."""
    out = _coalesce_positions([100, 500, 1000, 1500, 5000], 1000)
    assert out == [(100, 1500), (5000, 5000)]


def test_coalesce_positions_custom_threshold() -> None:
    """Same input, different gap threshold → different range shape."""
    out = _coalesce_positions([100, 500, 1000, 1500, 5000], 100)
    assert out == [(100, 100), (500, 500), (1000, 1000), (1500, 1500), (5000, 5000)]


def test_coalesce_positions_empty_input() -> None:
    assert _coalesce_positions([], DEFAULT_COALESCE_DISTANCE_BP) == []


def test_coalesce_positions_single_position() -> None:
    assert _coalesce_positions([42], DEFAULT_COALESCE_DISTANCE_BP) == [(42, 42)]


# ---------------------------------------------------------------------------
# _record_to_row
# ---------------------------------------------------------------------------


def _make_record(  # noqa: PLR0913 — wrapper over a structural constructor
    chrom: str,
    pos: int,
    ref: str,
    alt: str,
    info: dict[str, object],
    filter_value: str | None = None,
) -> FakeVariant:
    return FakeVariant(
        CHROM=chrom,
        POS=pos,
        REF=ref,
        ALT=(alt,),
        INFO=dict(info),
        FILTER=filter_value,
    )


def test_record_to_row_full_pops() -> None:
    """All 10 AF_<pop> keys → all af_<pop> columns populated, af_mid included.

    Mirrors the gnomAD v4.1 per-chrom VCF INFO contract: global AF/AC/AN
    plus per-population ``AF_<vcf_suffix>`` where the "oth" schema
    column reads from the renamed ``AF_remaining`` INFO key.
    """
    info: dict[str, object] = {
        "AF": 0.05,
        "AC": 10,
        "AN": 200,
    }
    for pop in GNOMAD_POPULATIONS:
        info[f"AF_{_POP_TO_VCF_INFO_SUFFIX[pop]}"] = 0.1
    record = _make_record("chr1", 100, "A", "C", info)
    retrieval = datetime(2026, 5, 19, tzinfo=UTC)
    row = _record_to_row(record, source_version_id=1, retrieval_datetime=retrieval)
    assert row is not None
    assert row["chrom"] == "1"
    assert row["pos_grch38"] == 100
    assert row["ref_allele"] == "A"
    assert row["alt_allele"] == "C"
    assert row["af_global"] == 0.05
    assert row["ac_global"] == 10
    assert row["an_global"] == 200
    for pop in GNOMAD_POPULATIONS:
        assert row[f"af_{pop}"] == 0.1
    assert row["af_mid"] == 0.1
    assert row["filter_status"] == "PASS"


def test_record_to_row_partial_pops() -> None:
    """Missing AF_<pop> keys → NULL in those columns; other cols populated."""
    info = {
        "AF": 0.02,
        "AC": 4,
        "AN": 200,
        "AF_nfe": 0.025,
        "AF_afr": 0.015,
    }
    record = _make_record("chr2", 250, "G", "T", info)
    row = _record_to_row(record, source_version_id=1, retrieval_datetime=datetime.now(UTC))
    assert row is not None
    assert row["af_nfe"] == 0.025
    assert row["af_afr"] == 0.015
    assert row["af_mid"] is None
    assert row["af_ami"] is None


def test_record_to_row_uses_filter_status_from_record() -> None:
    """Non-None FILTER values are preserved verbatim; None → 'PASS'."""
    info: dict[str, object] = {"AF": 0.01}
    record_pass = _make_record("chr1", 100, "A", "C", info)
    record_filter = _make_record("chr1", 200, "A", "C", info, filter_value="AC0")
    now = datetime.now(UTC)
    row_pass = _record_to_row(record_pass, source_version_id=1, retrieval_datetime=now)
    row_filter = _record_to_row(record_filter, source_version_id=1, retrieval_datetime=now)
    assert row_pass is not None
    assert row_filter is not None
    assert row_pass["filter_status"] == "PASS"
    assert row_filter["filter_status"] == "AC0"


# ---------------------------------------------------------------------------
# Dedup across exomes + genomes
# ---------------------------------------------------------------------------


def _build_overlap_factory() -> _VCFFactory:
    """Exomes + genomes URLs for chr22 both ship a record at (22, 1000, A, C).

    Exomes reports AF=0.10, genomes reports AF=0.20. Dedup should
    keep the exomes row (first-write-wins).
    """
    by_url: dict[str, list[FakeVariant]] = {}
    exomes_url = GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22")
    genomes_url = GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22")
    by_url[exomes_url] = [
        _make_record("chr22", 1000, "A", "C", {"AF": 0.10, "AC": 5, "AN": 50}),
    ]
    by_url[genomes_url] = [
        _make_record("chr22", 1000, "A", "C", {"AF": 0.20, "AC": 10, "AN": 50}),
    ]
    return _VCFFactory(by_url=by_url)


def test_dedup_first_write_wins_exomes_genomes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same (chrom, pos, ref, alt) seen in both data_types → one row, exomes wins."""
    init_databases()
    _enable_external_calls()
    factory = _build_overlap_factory()
    _patch_cyvcf2(monkeypatch, factory)

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 1000)])
        _seed_clinvar_active(conn, rows=[("22", 1000)])
        _seed_gwas_active(conn, rows=[("22", 1000)])
        audited, http = _mock_audited_client()
        try:
            result = load(
                conn,
                audited,
                force=True,
                chromosomes=["22"],
            )
        finally:
            audited.close()
            http.close()
        sql = (
            "SELECT chrom::VARCHAR, pos_grch38, ref_allele, alt_allele, af_global"  # noqa: S608
            f" FROM {gnomad_loader._TARGET_TABLE}"  # noqa: SLF001
            " WHERE source_version_id = ?"
        )
        rows = conn.execute(sql, [result.source_version_id]).fetchall()
    assert len(rows) == 1
    chrom, pos, ref, alt, af = rows[0]
    assert chrom == "22"
    assert pos == 1000
    assert ref == "A"
    assert alt == "C"
    # Exomes wins (0.10), not genomes (0.20).
    assert af == 0.10


# ---------------------------------------------------------------------------
# Top-level load() — short-circuit / force / version override
# ---------------------------------------------------------------------------


def _patch_check_libcurl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gnomad_loader, "_check_libcurl_available", lambda: None)


def _patch_serial_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``_run_workers`` with an in-process serial shim.

    Real ``ProcessPoolExecutor`` workers run in spawned subprocesses that don't
    see the test's ``cyvcf2.VCF`` monkeypatch (and would hang on real network).
    The shim runs each worker in-process — where the monkeypatch applies —
    while preserving ``_run_workers``' exact contract: a worker that raises
    becomes a ``"failed"`` :class:`_ChromResult` carrying the exception. Used by
    the parallel-path tests and by the CLI tests, whose ``--jobs`` default
    (:data:`DEFAULT_PARALLEL_JOBS`) now routes through the parallel orchestrator.
    """

    def _serial(tasks: list[_ChromTask], jobs: int) -> list[_ChromResult]:  # noqa: ARG001 — jobs unused in-process
        out: list[_ChromResult] = []
        for task in tasks:
            try:
                out.append(_stream_chromosome_to_parquet(task))
            except Exception as exc:  # noqa: BLE001 — mirror _run_workers' normalisation
                out.append(
                    _ChromResult(
                        chrom=task.chrom,
                        parquet_path=None,
                        row_count=0,
                        status="failed",
                        error=exc,
                    ),
                )
        return out

    monkeypatch.setattr(gnomad_loader, "_run_workers", _serial)


def test_short_circuit_already_current_no_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """Active version matches → exit immediately, no DB writes."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)
    factory = _VCFFactory(by_url={})
    _patch_cyvcf2(monkeypatch, factory)

    with duckdb_connection() as conn:
        sv_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=GNOMAD_VERSION,
            source_url=GNOMAD_URL_TEMPLATE,
            source_file_hash="hash",
            source_file_size=0,
            record_count=0,
        )
        flip_to_new_version(
            conn,
            source=SOURCE_DB,
            table=gnomad_loader._TARGET_TABLE,  # noqa: SLF001
            new_source_version_id=sv_id,
        )
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited)
        finally:
            audited.close()
            http.close()
    assert result.source_version_id == sv_id
    assert result.pointer_flipped is False
    assert result.rows_loaded == 0
    # Factory was never opened (no chrom iteration ran).
    assert factory.openings == []


def test_force_allocates_new_source_version_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """--force allocates a new source_version_id even when version matches."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)
    # Minimal: one filter position on chr22, factory returns one record there.
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record(
                "chr22",
                1000,
                "A",
                "C",
                {"AF": 0.05, "AC": 5, "AN": 100},
            ),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 1000)])
        # Seed a current active gnomad source-version.
        old_sv_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=GNOMAD_VERSION,
            source_url=GNOMAD_URL_TEMPLATE,
            source_file_hash="hash",
            source_file_size=0,
            record_count=0,
        )
        flip_to_new_version(
            conn,
            source=SOURCE_DB,
            table=gnomad_loader._TARGET_TABLE,  # noqa: SLF001
            new_source_version_id=old_sv_id,
        )
        audited, http = _mock_audited_client()
        try:
            # --force with --chromosomes 22 should allocate a new sv_id and
            # land chr22 under it; pointer does NOT flip (partial run).
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()
    assert result.source_version_id is not None
    assert result.source_version_id != old_sv_id
    assert result.pointer_flipped is False
    assert result.rows_loaded >= 1


def test_explicit_version_override_different_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different `version` argument forces a new source-version row + flip."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    # Mock cyvcf2 to return one record on chr22; chr1..21 + X are filtered out
    # because we only seed filter positions on chr22, so the pointer flip
    # logic still has to recognize the partial coverage. Use a single-chrom
    # restriction so we test the "different label" branch without needing
    # a full-genome run.
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record(
                "chr22",
                100,
                "A",
                "C",
                {"AF": 0.5, "AC": 50, "AN": 100},
            ),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])
        old_sv_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=GNOMAD_VERSION,
            source_url=GNOMAD_URL_TEMPLATE,
            source_file_hash="old",
            source_file_size=0,
            record_count=0,
        )
        flip_to_new_version(
            conn,
            source=SOURCE_DB,
            table=gnomad_loader._TARGET_TABLE,  # noqa: SLF001
            new_source_version_id=old_sv_id,
        )
        audited, http = _mock_audited_client()
        try:
            result = load(
                conn,
                audited,
                version="4.1.2",
                chromosomes=["22"],
            )
        finally:
            audited.close()
            http.close()
    assert result.version_label == "4.1.2"
    assert result.source_version_id != old_sv_id
    # Partial-chromosomes does not flip the pointer.
    assert result.pointer_flipped is False
    with duckdb_connection() as conn:
        version_rows = conn.execute(
            "SELECT version FROM annotation_source_versions WHERE source_db='gnomad'"
            " ORDER BY source_version_id",
        ).fetchall()
    assert version_rows == [(GNOMAD_VERSION,), ("4.1.2",)]


# ---------------------------------------------------------------------------
# Partial failure + resume
# ---------------------------------------------------------------------------


def test_partial_failure_leaves_pointer_unflipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a chrom raises mid-iteration, the pointer doesn't flip."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    chrom_records: dict[str, list[FakeVariant]] = {
        c: [
            _make_record(
                f"chr{c}",
                100,
                "A",
                "C",
                {"AF": 0.1, "AC": 1, "AN": 10},
            ),
        ]
        for c in SUPPORTED_CHROMS
    }
    boom_chrom = "7"

    class _BoomFactory:
        openings: list[str] = []  # noqa: RUF012 — test-local mutable attribute

        def __call__(self, url: str) -> _FakeVCF:
            self.openings.append(url)
            # Identify the chrom from the URL.
            for c in SUPPORTED_CHROMS:
                if f"chr{c}.vcf.bgz" in url:
                    if c == boom_chrom:
                        msg = "boom on chr7"
                        raise RuntimeError(msg)
                    return _FakeVCF(records=list(chrom_records[c]))
            return _FakeVCF(records=[])

    monkeypatch.setattr("cyvcf2.VCF", _BoomFactory())
    with duckdb_connection() as conn:
        for c in SUPPORTED_CHROMS:
            _seed_user_variants(conn, [(c, 100)])
        audited, http = _mock_audited_client()
        try:
            with pytest.raises(RuntimeError, match="boom on chr7"):
                load(conn, audited, force=True)
        finally:
            audited.close()
            http.close()
        # Pointer not set — no active gnomad source-version yet.
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='gnomad'",
        ).fetchone()
        # Chrom 1-6 landed under the new source-version-id even though chr7 failed.
        rows_landed = conn.execute(
            f"SELECT COUNT(*) FROM {gnomad_loader._TARGET_TABLE}",  # noqa: SLF001 S608
        ).fetchone()
        # The version row is preserved -- chr1-6 rows reference it, so the
        # orphan-cleanup helper (finding-015) must NOT delete it. The row
        # is "orphan-of-pointer" (not active) but not "orphan-of-data".
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db='gnomad'",
        ).fetchone()
    assert pointer is None
    assert rows_landed is not None
    # chr1..6 each landed at least one row.
    expected_min = 6
    assert rows_landed[0] >= expected_min
    assert version_rows is not None
    assert version_rows[0] == 1


def test_resume_picks_up_remaining_chroms(monkeypatch: pytest.MonkeyPatch) -> None:
    """--resume with an in-flight source-version: skip already-populated chroms."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    by_url: dict[str, list[FakeVariant]] = {}
    for c in SUPPORTED_CHROMS:
        by_url[GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom=c)] = [
            _make_record(
                f"chr{c}",
                100,
                "A",
                "C",
                {"AF": 0.1, "AC": 1, "AN": 10},
            ),
        ]
        by_url[GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom=c)] = []
    factory = _VCFFactory(by_url=by_url)
    _patch_cyvcf2(monkeypatch, factory)

    with duckdb_connection() as conn:
        for c in SUPPORTED_CHROMS:
            _seed_user_variants(conn, [(c, 100)])
        # Pre-seed: simulate a partial run that landed chr22 under a fresh sv_id.
        sv_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=GNOMAD_VERSION,
            source_url=GNOMAD_URL_TEMPLATE,
            source_file_hash="partial",
            source_file_size=0,
            record_count=None,
        )
        conn.execute(
            """
            INSERT INTO gnomad_frequencies (
                freq_id, chrom, pos_grch38, ref_allele, alt_allele,
                af_global, ac_global, an_global,
                af_afr, af_ami, af_amr, af_asj, af_eas, af_fin,
                af_mid, af_nfe, af_sas, af_oth,
                filter_status, source_version_id, retrieval_date
            )
            VALUES (
                1, '22'::chromosome_enum, 100, 'A', 'C',
                0.1, 1, 10,
                NULL, NULL, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL,
                'PASS', ?, CURRENT_TIMESTAMP
            )
            """,
            [sv_id],
        )
        conn.commit()
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, resume=True)
        finally:
            audited.close()
            http.close()
        # Pointer was flipped at the end since the full chrom set is covered.
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='gnomad'",
        ).fetchone()
    assert result.source_version_id == sv_id
    assert result.pointer_flipped is True
    assert pointer is not None
    assert pointer[0] == sv_id


def test_partial_chromosomes_filter_does_not_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--force --chromosomes 22 → content lands, but pointer stays on prior."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record(
                "chr22",
                100,
                "A",
                "C",
                {"AF": 0.1, "AC": 1, "AN": 10},
            ),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])
        # Prior active version under a different chrom.
        prior_sv_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version="4.0.0",
            source_url=GNOMAD_URL_TEMPLATE,
            source_file_hash="old",
            source_file_size=0,
            record_count=0,
        )
        flip_to_new_version(
            conn,
            source=SOURCE_DB,
            table=gnomad_loader._TARGET_TABLE,  # noqa: SLF001
            new_source_version_id=prior_sv_id,
        )
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='gnomad'",
        ).fetchone()
        # Both version rows are preserved: the prior (active) v4.0.0 row and
        # the new (in-flight) chr22-only row. The chr22 row is "orphan-of-
        # pointer" (not active) but data references it, so the cleanup
        # helper (finding-015) must NOT delete it.
        version_ids = conn.execute(
            "SELECT source_version_id FROM annotation_source_versions"
            " WHERE source_db='gnomad' ORDER BY source_version_id",
        ).fetchall()
    assert result.source_version_id is not None
    assert result.source_version_id != prior_sv_id
    assert result.pointer_flipped is False
    # Pointer remains on the prior version.
    assert pointer is not None
    assert pointer[0] == prior_sv_id
    assert version_ids == [(prior_sv_id,), (result.source_version_id,)]


# ---------------------------------------------------------------------------
# Orphan version-row cleanup (finding-015)
#
# Sibling Phase-5 loaders (clinvar, gwas_catalog, pgs_catalog, pharmgkb,
# cpic) each ship a ``_cleanup_orphan_version_row`` helper that deletes
# the ``annotation_source_versions`` row when the load transaction
# rolls back without inserting any data rows. gnomad allocates its
# version row before the per-chromosome loop runs, so a failure mid-
# chromosome or a ``--chromosomes`` partial run whose requested chrom
# yields zero rows can leave the version row dangling. The cleanup
# helper, wired into the post-loop guard, deletes such orphan rows so a
# future run gets a clean sv_id allocation. The four tests below pin
# down the contract: cleanup fires only when (1) the version row was
# freshly allocated in this invocation (not via ``--resume``), and (2)
# zero data rows landed under it, and (3) the pointer was not flipped.
# ---------------------------------------------------------------------------


def test_cleanup_orphan_when_chromosomes_run_yields_zero_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A --chromosomes partial run that lands zero rows triggers cleanup.

    The loader allocates a fresh source_version_id before iterating
    chromosomes. When the requested chromosome completes without
    inserting any rows -- e.g. the upstream factory returned no
    records under the configured filter positions -- the version
    row is an orphan: it exists in ``annotation_source_versions``
    but no ``gnomad_frequencies`` row references it. Per
    finding-015 #11 the loader's post-loop cleanup deletes the
    orphan so a future run gets a clean sv_id allocation.
    """
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    # chr22 has a filter position but the factory returns zero
    # records on both data_types. The loop "succeeds" (chr22 lands
    # in chromosomes_succeeded) but rows_count stays at 0.
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()
        version_count_row = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db='gnomad'",
        ).fetchone()
        freq_rows = conn.execute(
            f"SELECT COUNT(*) FROM {gnomad_loader._TARGET_TABLE}",  # noqa: SLF001 S608
        ).fetchone()
    # Cleanup ran: no annotation_source_versions row for gnomad remains.
    assert version_count_row is not None
    assert version_count_row[0] == 0
    assert freq_rows is not None
    assert freq_rows[0] == 0
    # Result honestly reports no version was retained.
    assert result.source_version_id is None
    assert result.rows_loaded == 0
    assert result.pointer_flipped is False


def test_cleanup_orphan_when_first_chrom_fails_before_any_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure on the first chrom open (zero rows landed) triggers cleanup.

    Mirrors the per-chrom-loop failure path. ``chr1`` is the first
    URL the loader opens (with ``--chromosomes 1``); the factory
    raises on every open so the loader never commits a single row
    under the freshly-allocated source_version_id. Per
    finding-015 #11 the orphan version row is deleted post-loop
    before the captured exception re-raises.
    """
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    class _BoomOnFirstOpen:
        def __call__(self, _url: str) -> _FakeVCF:
            msg = "boom on first open"
            raise RuntimeError(msg)

    import cyvcf2  # noqa: PLC0415 — test-local import keeps module surface narrow

    monkeypatch.setattr(cyvcf2, "VCF", _BoomOnFirstOpen())

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("1", 100)])
        audited, http = _mock_audited_client()
        try:
            with pytest.raises(RuntimeError, match="boom on first open"):
                load(conn, audited, force=True, chromosomes=["1"])
        finally:
            audited.close()
            http.close()
        version_count_row = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db='gnomad'",
        ).fetchone()
        freq_rows = conn.execute(
            f"SELECT COUNT(*) FROM {gnomad_loader._TARGET_TABLE}",  # noqa: SLF001 S608
        ).fetchone()
    # Cleanup ran: no orphan annotation_source_versions row remains.
    assert version_count_row is not None
    assert version_count_row[0] == 0
    # And no data rows landed.
    assert freq_rows is not None
    assert freq_rows[0] == 0


def test_resume_does_not_cleanup_preexisting_in_flight_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--resume against a pre-existing in-flight sv_id never cleans it up.

    The cleanup helper only fires when the version row was
    allocated in the *current* invocation. ``--resume`` reuses a
    pre-existing sv_id (e.g. the v10 orphan from finding-015);
    cleaning it up would contradict the operator's explicit "resume
    against this" intent and would also remove the row a future
    ``--resume`` would look for. Here the resumed run fails on
    every chrom, lands zero new rows, and the preexisting orphan
    sv_id survives.
    """
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    class _BoomOnResume:
        def __call__(self, _url: str) -> _FakeVCF:
            msg = "boom on resume"
            raise RuntimeError(msg)

    import cyvcf2  # noqa: PLC0415 — test-local import keeps module surface narrow

    monkeypatch.setattr(cyvcf2, "VCF", _BoomOnResume())

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("1", 100)])
        # Pre-seed an orphan-of-data in-flight version row, mirroring the
        # v10 case from finding-015: no gnomad_frequencies row references it.
        preexisting_sv_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=GNOMAD_VERSION,
            source_url=GNOMAD_URL_TEMPLATE,
            source_file_hash="orphan",
            source_file_size=0,
            record_count=None,
        )
        conn.commit()
        audited, http = _mock_audited_client()
        try:
            with pytest.raises(RuntimeError, match="boom on resume"):
                load(conn, audited, resume=True, chromosomes=["1"])
        finally:
            audited.close()
            http.close()
        # The preexisting sv_id is preserved -- cleanup did NOT delete it.
        version_ids = conn.execute(
            "SELECT source_version_id FROM annotation_source_versions WHERE source_db='gnomad'",
        ).fetchall()
    assert version_ids == [(preexisting_sv_id,)]


def test_successful_full_run_does_not_trigger_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete, successful run keeps the version row and flips the pointer.

    Positive control for the cleanup contract: cleanup fires only
    on orphan rows (freshly allocated, zero data, no pointer flip).
    A run where every chrom lands at least one row produces a real
    active version that the helper must NOT delete.
    """
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    by_url: dict[str, list[FakeVariant]] = {}
    for c in SUPPORTED_CHROMS:
        by_url[GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom=c)] = [
            _make_record(
                f"chr{c}",
                100,
                "A",
                "C",
                {"AF": 0.1, "AC": 1, "AN": 10},
            ),
        ]
        by_url[GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom=c)] = []
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))

    with duckdb_connection() as conn:
        for c in SUPPORTED_CHROMS:
            _seed_user_variants(conn, [(c, 100)])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True)
        finally:
            audited.close()
            http.close()
        version_ids = conn.execute(
            "SELECT source_version_id FROM annotation_source_versions WHERE source_db='gnomad'",
        ).fetchall()
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='gnomad'",
        ).fetchone()
    assert result.source_version_id is not None
    assert result.pointer_flipped is True
    assert result.rows_loaded == len(SUPPORTED_CHROMS)
    # The freshly-allocated version row is preserved and named by the pointer.
    assert version_ids == [(result.source_version_id,)]
    assert pointer is not None
    assert pointer[0] == result.source_version_id


# ---------------------------------------------------------------------------
# External-calls-disabled + audit accounting
# ---------------------------------------------------------------------------


def test_external_calls_disabled_blocks_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """external_calls_enabled=false → raise after audit-log intent + blocked rows."""
    init_databases()
    _patch_check_libcurl(monkeypatch)
    factory = _VCFFactory(by_url={})
    _patch_cyvcf2(monkeypatch, factory)
    # init_databases seeds external_calls_enabled=false; do not flip it.
    with duckdb_connection() as conn:
        audited, http = _mock_audited_client()
        try:
            with pytest.raises(ExternalCallsDisabledError):
                load(conn, audited)
        finally:
            audited.close()
            http.close()
    rows = _audit_rows()
    # Exactly one intent + one blocked row from the pre-flight HEAD.
    expected_pair = 2
    assert len(rows) >= expected_pair
    # No gnomad_frequencies content landed.
    with duckdb_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM gnomad_frequencies",
        ).fetchone()
    assert count is not None
    assert count[0] == 0


def test_audit_logs_one_event_per_chrom_per_data_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full run on autosomes + X → 46 audit intent rows and 46 result rows."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    by_url: dict[str, list[FakeVariant]] = {}
    for c in SUPPORTED_CHROMS:
        for data_type in ("exomes", "genomes"):
            by_url[GNOMAD_URL_TEMPLATE.format(data_type=data_type, chrom=c)] = []
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))

    with duckdb_connection() as conn:
        # Seed at least one user-variant position per chrom so the loader
        # actually issues HEAD requests for each (chrom, data_type).
        for c in SUPPORTED_CHROMS:
            _seed_user_variants(conn, [(c, 100)])
        audited, http = _mock_audited_client()
        try:
            load(conn, audited)
        finally:
            audited.close()
            http.close()
    rows = _audit_rows()
    # 23 chromosomes (1-22 + X) by 2 data_types (exomes + genomes) = 46 HEAD requests.
    # Each HEAD writes one intent + one result audit row.
    head_rows = [r for r in rows if r[2] == "gnomad_remote_vcf_open"]
    expected_total = 23 * 2 * 2  # 46 intent + 46 result.
    assert len(head_rows) == expected_total


# ---------------------------------------------------------------------------
# AF buckets + pop presence
# ---------------------------------------------------------------------------


def test_af_bucket_counters_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Variants at exact bucket boundaries land in the documented bucket."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    positions = [
        (100, 0.0005),  # < 0.001
        (200, 0.001),  # 0.001_to_0.01
        (300, 0.01),  # 0.01_to_0.05
        (400, 0.05),  # 0.05_to_0.5
        (500, 0.5),  # 0.05_to_0.5
        (600, 0.6),  # > 0.5
    ]
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record(
                "chr22",
                pos,
                "A",
                "C",
                {"AF": af, "AC": 1, "AN": 100},
            )
            for pos, af in positions
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", pos) for pos, _af in positions])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()
    buckets = result.af_buckets_user_overlap
    assert buckets["lt_0.001"] == 1
    assert buckets["0.001_to_0.01"] == 1
    assert buckets["0.01_to_0.05"] == 1
    assert buckets["0.05_to_0.5"] == 2  # 0.05 and 0.5 both land here.
    assert buckets["gt_0.5"] == 1


def test_pop_af_presence_counters_mixed_zero_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-population counts reflect non-NULL af_<pop> presence."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record(
                "chr22",
                100,
                "A",
                "C",
                {
                    "AF": 0.05,
                    "AC": 1,
                    "AN": 100,
                    "AF_nfe": 0.06,
                    "AF_afr": 0.0,
                },
            ),
            _make_record(
                "chr22",
                200,
                "A",
                "C",
                {
                    "AF": 0.10,
                    "AC": 5,
                    "AN": 100,
                    "AF_nfe": 0.11,
                },
            ),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100), ("22", 200)])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()
    assert result.pop_af_presence["nfe"] == 2
    # af_afr is present on the first record (value 0.0) but absent on the
    # second. The schema's IS NOT NULL counts 0.0 as present.
    assert result.pop_af_presence["afr"] == 1
    assert result.pop_af_presence["mid"] == 0


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_genome_annotate_refresh_gnomad_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI short-circuit path: gnomad already current → loader returns immediately."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url={}))

    with duckdb_connection() as conn:
        sv_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=GNOMAD_VERSION,
            source_url=GNOMAD_URL_TEMPLATE,
            source_file_hash="hash",
            source_file_size=0,
            record_count=0,
        )
        flip_to_new_version(
            conn,
            source=SOURCE_DB,
            table=gnomad_loader._TARGET_TABLE,  # noqa: SLF001
            new_source_version_id=sv_id,
        )

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "refresh", "--source", "gnomad"])
    assert result.exit_code == 0, result.output
    assert "source_db=gnomad" in result.output
    assert f"version={GNOMAD_VERSION}" in result.output


def test_genome_annotate_refresh_gnomad_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI --force flow: at least one chrom landed under a new source-version."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record(
                "chr22",
                100,
                "A",
                "C",
                {"AF": 0.05, "AC": 1, "AN": 100},
            ),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))
    _patch_serial_workers(monkeypatch)  # CLI default --jobs > 1 routes through the parallel path

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "annotate",
            "refresh",
            "--source",
            "gnomad",
            "--force",
            "--chromosomes",
            "22",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "source_db=gnomad" in result.output
    with duckdb_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM gnomad_frequencies",
        ).fetchone()
    assert row is not None
    assert row[0] >= 1


def test_genome_annotate_refresh_gnomad_chromosomes_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--chromosomes restricts to one chrom; pointer stays unflipped."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record(
                "chr22",
                100,
                "A",
                "C",
                {"AF": 0.05, "AC": 1, "AN": 100},
            ),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))
    _patch_serial_workers(monkeypatch)  # CLI default --jobs > 1 routes through the parallel path

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "annotate",
            "refresh",
            "--source",
            "gnomad",
            "--chromosomes",
            "22",
        ],
    )
    assert result.exit_code == 0, result.output
    with duckdb_connection() as conn:
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='gnomad'",
        ).fetchone()
    # Pointer never flipped because the chrom set was restricted.
    assert pointer is None


def test_genome_annotate_refresh_gnomad_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--resume continues an in-flight load and flips when the full set is covered."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    by_url: dict[str, list[FakeVariant]] = {}
    for c in SUPPORTED_CHROMS:
        by_url[GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom=c)] = [
            _make_record(
                f"chr{c}",
                100,
                "A",
                "C",
                {"AF": 0.1, "AC": 1, "AN": 10},
            ),
        ]
        by_url[GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom=c)] = []
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))
    _patch_serial_workers(monkeypatch)  # CLI default --jobs > 1 routes through the parallel path

    with duckdb_connection() as conn:
        for c in SUPPORTED_CHROMS:
            _seed_user_variants(conn, [(c, 100)])
        # Pre-seed in-flight source-version with chr22 already populated.
        sv_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=GNOMAD_VERSION,
            source_url=GNOMAD_URL_TEMPLATE,
            source_file_hash="partial",
            source_file_size=0,
            record_count=None,
        )
        conn.execute(
            """
            INSERT INTO gnomad_frequencies (
                freq_id, chrom, pos_grch38, ref_allele, alt_allele,
                af_global, ac_global, an_global,
                af_afr, af_ami, af_amr, af_asj, af_eas, af_fin,
                af_mid, af_nfe, af_sas, af_oth,
                filter_status, source_version_id, retrieval_date
            )
            VALUES (
                1, '22'::chromosome_enum, 100, 'A', 'C',
                0.1, 1, 10,
                NULL, NULL, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL,
                'PASS', ?, CURRENT_TIMESTAMP
            )
            """,
            [sv_id],
        )
        conn.commit()

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["annotate", "refresh", "--source", "gnomad", "--resume"],
    )
    assert result.exit_code == 0, result.output
    with duckdb_connection() as conn:
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='gnomad'",
        ).fetchone()
    assert pointer is not None
    assert pointer[0] == sv_id


# ---------------------------------------------------------------------------
# Parallel per-chromosome load (process pool + staged-Parquet merge)
#
# The worker (_stream_chromosome_to_parquet) is exercised directly in-process
# (it's a plain function); the load(jobs > 1) integration tests use the
# _patch_serial_workers shim so the orchestration runs in-process where the
# cyvcf2.VCF monkeypatch applies (spawned subprocesses would not see it).
# ---------------------------------------------------------------------------


def _chrom_task(
    chrom: str,
    positions: set[int],
    staging_dir: Path,
    *,
    source_version_id: int = 7,
    region: str | None = None,
) -> _ChromTask:
    """Build a ``_ChromTask`` for the worker tests."""
    return _ChromTask(
        chrom=chrom,
        region_strings=[region or f"chr{chrom}:1-100000"],
        filter_positions=frozenset(positions),
        source_version_id=source_version_id,
        retrieval_datetime=datetime(2026, 5, 19, tzinfo=UTC),
        batch_size=50_000,
        staging_dir=str(staging_dir),
    )


def test_stream_chromosome_to_parquet_writes_deduped_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker stages deduped rows (exomes-win) to Parquet without freq_id."""
    _patch_cyvcf2(monkeypatch, _build_overlap_factory())
    result = _stream_chromosome_to_parquet(_chrom_task("22", {1000}, tmp_path))
    assert result.status == "ok"
    assert result.row_count == 1
    assert result.parquet_path is not None
    table = pq.read_table(result.parquet_path)
    assert "freq_id" not in table.column_names
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["chrom"] == "22"
    assert row["pos_grch38"] == 1000
    assert row["ref_allele"] == "A"
    assert row["alt_allele"] == "C"
    assert row["af_global"] == 0.10  # exomes (0.10) wins over genomes (0.20)
    assert row["source_version_id"] == 7


def test_stream_chromosome_to_parquet_filters_positions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A record whose position is outside filter_positions is dropped → 0 rows."""
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record("chr22", 1000, "A", "C", {"AF": 0.1, "AC": 1, "AN": 10}),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))
    # filter_positions excludes 1000.
    result = _stream_chromosome_to_parquet(_chrom_task("22", {2000}, tmp_path))
    assert result.status == "ok"
    assert result.row_count == 0
    assert result.parquet_path is None


def test_stream_chromosome_to_parquet_propagates_open_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cyvcf2 open failure propagates out of the worker (not swallowed).

    _run_workers normalises the propagated exception into a failed result; the
    worker itself must re-raise so the cause reaches the parent intact.
    """
    import cyvcf2  # noqa: PLC0415

    def _explode(_url: str) -> _FakeVCF:
        msg = "boom open chr22"
        raise RuntimeError(msg)

    monkeypatch.setattr(cyvcf2, "VCF", _explode)
    with pytest.raises(RuntimeError, match="boom open chr22"):
        _stream_chromosome_to_parquet(_chrom_task("22", {1000}, tmp_path))


def test_merge_chromosome_parquet_assigns_contiguous_freq_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Merging two staged chromosomes assigns gap-free, ordered freq_ids."""
    init_databases()
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="1"): [
            _make_record("chr1", 100, "A", "C", {"AF": 0.1, "AC": 1, "AN": 10}),
            _make_record("chr1", 200, "A", "G", {"AF": 0.2, "AC": 2, "AN": 10}),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="1"): [],
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="2"): [
            _make_record("chr2", 300, "A", "T", {"AF": 0.3, "AC": 3, "AN": 10}),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="2"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))

    with duckdb_connection() as conn:
        # A real source-version row so the gnomad_frequencies FK is satisfied.
        sv_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=GNOMAD_VERSION,
            source_url=GNOMAD_URL_TEMPLATE,
            source_file_hash="merge-test",
            source_file_size=0,
            record_count=None,
        )
        r1 = _stream_chromosome_to_parquet(
            _chrom_task("1", {100, 200}, tmp_path, source_version_id=sv_id),
        )
        r2 = _stream_chromosome_to_parquet(
            _chrom_task("2", {300}, tmp_path, source_version_id=sv_id),
        )
        assert r1.parquet_path is not None
        assert r2.parquet_path is not None
        _merge_chromosome_parquet(conn, r1.parquet_path)
        _merge_chromosome_parquet(conn, r2.parquet_path)
        rows = conn.execute(
            "SELECT freq_id, chrom::VARCHAR, source_version_id"
            " FROM gnomad_frequencies ORDER BY freq_id",
        ).fetchall()
    assert [r[0] for r in rows] == [1, 2, 3]  # gap-free across both chroms
    assert [r[1] for r in rows] == ["1", "1", "2"]
    assert all(r[2] == sv_id for r in rows)


_PARALLEL_SPECS: dict[str, list[tuple[int, str, str, float]]] = {
    "1": [(100, "A", "C", 0.1), (200, "A", "G", 0.2)],
    "2": [(300, "A", "T", 0.3)],
    "22": [(400, "C", "T", 0.4)],
}


def _build_multichrom_factory() -> _VCFFactory:
    """Factory across chr1/2/22 with one genomes/exomes overlap per chrom."""
    by_url: dict[str, list[FakeVariant]] = {}
    for chrom, recs in _PARALLEL_SPECS.items():
        by_url[GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom=chrom)] = [
            _make_record(f"chr{chrom}", pos, ref, alt, {"AF": af, "AC": 1, "AN": 100})
            for pos, ref, alt, af in recs
        ]
        pos, ref, alt, _af = recs[0]
        # genomes carries the same first site with a different AF → exomes must win.
        by_url[GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom=chrom)] = [
            _make_record(f"chr{chrom}", pos, ref, alt, {"AF": 0.99, "AC": 1, "AN": 100}),
        ]
    return _VCFFactory(by_url=by_url)


def _collect_gnomad_rows(
    conn: object,
    source_version_id: int | None,
) -> list[tuple[object, ...]]:
    """Sorted (chrom, pos, ref, alt, af_global) for one source-version."""
    return conn.execute(  # type: ignore[attr-defined]
        "SELECT chrom::VARCHAR, pos_grch38, ref_allele, alt_allele, af_global"  # noqa: S608
        f" FROM {gnomad_loader._TARGET_TABLE}"  # noqa: SLF001
        " WHERE source_version_id = ?"
        " ORDER BY chrom, pos_grch38, ref_allele, alt_allele",
        [source_version_id],
    ).fetchall()


def test_parallel_load_reproduces_sequential_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """load(jobs=2) produces byte-identical rows + counts to load(jobs=1).

    Both runs execute against one database (force allocates a distinct
    source-version each time; the version-pointer pattern keeps the prior
    version's rows queryable), so the two row sets compare directly.
    """
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)
    with duckdb_connection() as conn:
        for chrom, recs in _PARALLEL_SPECS.items():
            _seed_user_variants(conn, [(chrom, pos) for pos, _r, _a, _af in recs])

    # Sequential baseline.
    _patch_cyvcf2(monkeypatch, _build_multichrom_factory())
    with duckdb_connection() as conn:
        audited, http = _mock_audited_client()
        try:
            seq = load(conn, audited, force=True, jobs=1)
        finally:
            audited.close()
            http.close()
        seq_rows = _collect_gnomad_rows(conn, seq.source_version_id)

    # Parallel run (in-process shim) into a fresh source-version.
    _patch_cyvcf2(monkeypatch, _build_multichrom_factory())
    _patch_serial_workers(monkeypatch)
    with duckdb_connection() as conn:
        audited, http = _mock_audited_client()
        try:
            par = load(conn, audited, force=True, jobs=2)
        finally:
            audited.close()
            http.close()
        par_rows = _collect_gnomad_rows(conn, par.source_version_id)

    assert par.source_version_id != seq.source_version_id
    assert par.rows_loaded == seq.rows_loaded
    assert par_rows == seq_rows
    # The overlapping (1,100,A,C) site kept the exomes AF (0.1), not genomes 0.99.
    assert ("1", 100, "A", "C", 0.1) in par_rows
    # Four distinct sites across chr1/2/22 (one exomes/genomes overlap deduped).
    expected_rows = 4
    assert par.rows_loaded == expected_rows


def test_parallel_load_partial_failure_merges_successes_no_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One chrom's worker fails → successes still merge, pointer unflipped, raises.

    Parallel analogue of test_partial_failure_leaves_pointer_unflipped. Unlike
    the sequential ``break``-on-first-failure, the parallel path is fail-soft:
    every dispatched chromosome except the failed one merges, so all but chr7
    land. The pointer never flips on a failure and the cause re-raises.
    """
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    chrom_records: dict[str, list[FakeVariant]] = {
        c: [_make_record(f"chr{c}", 100, "A", "C", {"AF": 0.1, "AC": 1, "AN": 10})]
        for c in SUPPORTED_CHROMS
    }
    boom_chrom = "7"

    class _BoomFactory:
        def __call__(self, url: str) -> _FakeVCF:
            for c in SUPPORTED_CHROMS:
                if f"chr{c}.vcf.bgz" in url:
                    if c == boom_chrom:
                        msg = "boom on chr7"
                        raise RuntimeError(msg)
                    return _FakeVCF(records=list(chrom_records[c]))
            return _FakeVCF(records=[])

    monkeypatch.setattr("cyvcf2.VCF", _BoomFactory())
    _patch_serial_workers(monkeypatch)
    with duckdb_connection() as conn:
        for c in SUPPORTED_CHROMS:
            _seed_user_variants(conn, [(c, 100)])
        audited, http = _mock_audited_client()
        try:
            with pytest.raises(RuntimeError, match="boom on chr7"):
                load(conn, audited, force=True, jobs=4)
        finally:
            audited.close()
            http.close()
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='gnomad'",
        ).fetchone()
        rows_landed = conn.execute(
            f"SELECT COUNT(*) FROM {gnomad_loader._TARGET_TABLE}",  # noqa: SLF001 S608
        ).fetchone()
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db='gnomad'",
        ).fetchone()
    assert pointer is None
    assert rows_landed is not None
    # Every chrom except chr7 merged (fail-soft, not break-on-first-failure).
    assert rows_landed[0] == len(SUPPORTED_CHROMS) - 1
    assert version_rows is not None
    assert version_rows[0] == 1


def test_parallel_load_audited_heads_count_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallel run still writes 2 HEAD audit events (intent+result) per loaded chrom."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)
    chroms = ["21", "22"]
    by_url: dict[str, list[FakeVariant]] = {}
    for c in chroms:
        by_url[GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom=c)] = [
            _make_record(f"chr{c}", 100, "A", "C", {"AF": 0.1, "AC": 1, "AN": 10}),
        ]
        by_url[GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom=c)] = []
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))
    _patch_serial_workers(monkeypatch)
    with duckdb_connection() as conn:
        for c in chroms:
            _seed_user_variants(conn, [(c, 100)])
        audited, http = _mock_audited_client()
        try:
            load(conn, audited, force=True, chromosomes=chroms, jobs=2)
        finally:
            audited.close()
            http.close()
    head_rows = [r for r in _audit_rows() if r[2] == "gnomad_remote_vcf_open"]
    # 2 chroms by 2 data_types by (intent + result) = 8.
    assert len(head_rows) == 2 * 2 * 2


def test_jobs_one_uses_sequential_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """jobs=1 never reaches the process pool (the sequential fallback runs)."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record("chr22", 100, "A", "C", {"AF": 0.1, "AC": 1, "AN": 10}),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))

    def _boom(_tasks: object, _jobs: object) -> list[_ChromResult]:
        msg = "jobs=1 must not call _run_workers"
        raise AssertionError(msg)

    monkeypatch.setattr(gnomad_loader, "_run_workers", _boom)
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"], jobs=1)
        finally:
            audited.close()
            http.close()
    assert result.rows_loaded == 1


def test_genome_annotate_refresh_gnomad_jobs_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --jobs 2 runs the parallel path (via the in-process shim) and lands rows."""
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)
    by_url: dict[str, list[FakeVariant]] = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record("chr22", 100, "A", "C", {"AF": 0.05, "AC": 1, "AN": 100}),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }
    _patch_cyvcf2(monkeypatch, _VCFFactory(by_url=by_url))
    _patch_serial_workers(monkeypatch)
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "annotate",
            "refresh",
            "--source",
            "gnomad",
            "--force",
            "--chromosomes",
            "22",
            "--jobs",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    with duckdb_connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM gnomad_frequencies").fetchone()
    assert row is not None
    assert row[0] >= 1


def test_refresh_remote_tabix_rejects_jobs_for_dbsnp() -> None:
    """--jobs on dbsnp is rejected before any load (only gnomad parallelizes).

    Unit-tests the dispatch helper directly so the assertion does not depend on
    the global loader registry's order-sensitive state across the suite.
    """
    from genome.annotate.cli import _refresh_remote_tabix  # noqa: PLC0415

    with pytest.raises(typer.BadParameter, match="--jobs"):
        _refresh_remote_tabix(
            "dbsnp",
            force=False,
            skip_if_same_version=False,
            version=None,
            chrom_filter=None,
            resume=False,
            coalesce_distance=None,
            jobs=2,
        )


def test_reject_jobs_flag_for_non_remote_tabix_source() -> None:
    """--jobs on a non-remote-tabix source raises BadParameter (the else-branch guard)."""
    from genome.annotate.cli import _reject_remote_tabix_only_flag  # noqa: PLC0415

    with pytest.raises(typer.BadParameter, match="--jobs"):
        _reject_remote_tabix_only_flag("jobs", "clinvar")


# ---------------------------------------------------------------------------
# Regression tests — PR-B real-data verification failure
#
# Three concrete defects were caught against the real gnomAD v4.1 VCFs
# in the first verification run; the unit tests at the time used a
# synthetic INFO key set (``AF_joint`` / ``AF_joint_<pop>``) and a
# filter-set with non-negative positions, so neither defect was
# observable in CI. These tests pin down the real contracts.
# ---------------------------------------------------------------------------


def test_record_to_row_uses_v41_real_info_key_names() -> None:
    """Per-chrom v4.1 INFO contract: AF, AC, AN, AF_<pop>; oth ← AF_remaining.

    Inspecting the real gnomAD v4.1 chr22 exomes VCF header (verified
    2026-05-20) confirms the INFO keys are the plain-suffix variants,
    not the ``_joint`` family. The Amish ``ami`` population is absent
    on the exomes side and must resolve to ``None``; the loader still
    populates ``af_ami`` from genomes records via the
    exomes-first-wins dedup. ``af_oth`` reads from the renamed
    ``AF_remaining`` key (gnomAD v4 retired the ``oth`` label).
    """
    info: dict[str, object] = {
        "AF": 1.8458999875292648e-06,
        "AC": 1,
        "AN": 541740,
        "AF_afr": 0.0,
        "AF_amr": 1.5e-05,
        "AF_asj": 0.0,
        "AF_eas": 0.0,
        "AF_fin": 0.0,
        "AF_mid": 0.0,
        "AF_nfe": 2.8e-06,
        "AF_sas": 0.0,
        "AF_remaining": 0.0,
        # AF_ami intentionally absent — matches v4.1 exomes layout.
    }
    record = _make_record("chr22", 17007792, "T", "A", info)
    row = _record_to_row(record, source_version_id=1, retrieval_datetime=datetime.now(UTC))
    assert row is not None
    assert row["af_global"] == pytest.approx(1.8459e-06, abs=1e-09)
    assert row["ac_global"] == 1
    assert row["an_global"] == 541740
    assert row["af_afr"] == 0.0
    assert row["af_amr"] == pytest.approx(1.5e-05, abs=1e-09)
    assert row["af_nfe"] == pytest.approx(2.8e-06, abs=1e-09)
    assert row["af_oth"] == 0.0  # populated from AF_remaining
    assert row["af_ami"] is None  # absent in exomes


def test_record_to_row_does_not_read_legacy_joint_keys() -> None:
    """Synthetic ``AF_joint``/``AF_joint_<pop>`` INFO is ignored.

    This is the explicit regression for the PR-B failure: the original
    loader read ``AF_joint`` etc., so against the real v4.1 per-chrom
    VCFs (which lack any ``_joint`` keys) every AF value resolved to
    ``None`` while rows still landed. The test asserts the inverse —
    that legacy ``_joint`` keys do *not* populate the schema columns,
    locking the contract in place against any future reversion.
    """
    info: dict[str, object] = {
        "AF_joint": 0.05,
        "AC_joint": 10,
        "AN_joint": 200,
        "AF_joint_afr": 0.1,
        "AF_joint_nfe": 0.2,
        "AF_joint_remaining": 0.3,
    }
    record = _make_record("chr22", 17007792, "T", "A", info)
    row = _record_to_row(record, source_version_id=1, retrieval_datetime=datetime.now(UTC))
    assert row is not None
    assert row["af_global"] is None
    assert row["ac_global"] is None
    assert row["an_global"] is None
    for pop in GNOMAD_POPULATIONS:
        assert row[f"af_{pop}"] is None, f"af_{pop} should be None for legacy keys"


def test_record_to_row_af_oth_reads_from_af_remaining_only() -> None:
    """``af_oth`` must come from ``AF_remaining``, not from ``AF_oth``.

    gnomAD v4 dropped the ``oth`` label entirely; an ``AF_oth`` INFO
    key does not exist in the public VCFs. If a hypothetical record
    carried ``AF_oth``, the loader should still ignore it and read
    ``AF_remaining``.
    """
    # AF_oth present, AF_remaining absent → af_oth is None (we don't
    # read AF_oth).
    info_with_oth: dict[str, object] = {
        "AF": 0.05,
        "AC": 1,
        "AN": 100,
        "AF_oth": 0.42,
    }
    row = _record_to_row(
        _make_record("chr22", 100, "A", "C", info_with_oth),
        source_version_id=1,
        retrieval_datetime=datetime.now(UTC),
    )
    assert row is not None
    assert row["af_oth"] is None
    # AF_remaining present → populates af_oth.
    info_with_remaining: dict[str, object] = {
        "AF": 0.05,
        "AC": 1,
        "AN": 100,
        "AF_remaining": 0.42,
    }
    row = _record_to_row(
        _make_record("chr22", 100, "A", "C", info_with_remaining),
        source_version_id=1,
        retrieval_datetime=datetime.now(UTC),
    )
    assert row is not None
    assert row["af_oth"] == pytest.approx(0.42, abs=1e-09)


def test_build_filter_set_excludes_sentinel_negative_positions() -> None:
    """ClinVar emits ``pos_grch38 = -1`` for unresolved coordinates.

    Real-data observation: the active ClinVar release in the
    project DB contains 20,173 rows with ``pos_grch38 = -1`` under
    the current source-version pointer. The original
    ``IS NOT NULL`` guard in ``_build_filter_set`` admitted these,
    which then flowed through ``_coalesce_positions`` and produced
    ``chr<N>:-1--1`` tabix regions — the htslib "Coordinates must
    be > 0" error in the first PR-B verification run. The
    tightened guard (``pos_grch38 > 0``) must drop every negative
    sentinel from each source-side subquery and from the union.
    """
    init_databases()
    with duckdb_connection() as conn:
        # ClinVar: real positions on chr22 plus the -1 sentinel.
        _seed_clinvar_active(
            conn,
            rows=[("22", 17007792), ("22", -1), ("1", -1), ("1", 500)],
        )
        # GWAS: also seed a -1 row to verify the same guard fires
        # against the GWAS subquery.
        _seed_gwas_active(conn, rows=[("22", 17007800), ("22", -1)])
        # variants_master forbids NULL but accepts any BIGINT; insert
        # a positive position so the user-side subquery has content.
        _seed_user_variants(conn, [("22", 17007792), ("1", 500)])
        result = _build_filter_set(conn)
    # No chromosome's position list may contain a negative value.
    for chrom, positions in result.positions.items():
        assert all(p > 0 for p in positions), (
            f"chrom {chrom} retains non-positive positions: {[p for p in positions if p <= 0]}"
        )
    # Specifically chr22 keeps the real positions, drops the -1.
    assert -1 not in result.positions["22"]
    assert 17007792 in result.positions["22"]
    assert 17007800 in result.positions["22"]
    # And chr1 keeps 500 but not -1.
    assert -1 not in result.positions["1"]
    assert 500 in result.positions["1"]
    # Composition counters reflect only positive rows: ClinVar 2 (22, 17007792) + (1, 500),
    # GWAS 1 (22, 17007800), user 2 (22, 17007792) + (1, 500), union_total 3 distinct positions.
    expected_clinvar = 2
    expected_gwas = 1
    expected_user = 2
    expected_union = 3
    assert result.composition["clinvar"] == expected_clinvar
    assert result.composition["gwas"] == expected_gwas
    assert result.composition["user"] == expected_user
    assert result.composition["union_total"] == expected_union


def test_load_does_not_query_negative_region_when_sources_have_sentinels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a -1 sentinel in ClinVar must never reach cyvcf2 as a region.

    Wires up a custom factory that captures every region string the
    loader passes to ``cyvcf2.VCF(...)`` and asserts the loader never
    builds a negative or non-positive range — even when the upstream
    annotation sources carry the sentinel rows that caused the PR-B
    "Coordinates must be > 0" failure.
    """
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    captured_regions: list[str] = []

    @dataclass
    class _TrackingFakeVCF:
        records: list[FakeVariant]

        def __call__(self, region: str) -> Iterable[FakeVariant]:
            captured_regions.append(region)
            match = re.match(r"chr([^:]+):(-?\d+)--?(-?\d+)", region)
            if match is None:
                return iter(())
            chrom = match.group(1)
            start = int(match.group(2))
            end = int(match.group(3))
            return (
                r
                for r in self.records
                if (r.CHROM.removeprefix("chr") == chrom) and start <= r.POS <= end
            )

        def close(self) -> None:
            pass

    by_url = {
        GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22"): [
            _make_record(
                "chr22",
                17007792,
                "T",
                "A",
                {"AF": 1.84e-06, "AC": 1, "AN": 541740, "AF_remaining": 0.0},
            ),
        ],
        GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22"): [],
    }

    def _factory(url: str) -> _TrackingFakeVCF:
        return _TrackingFakeVCF(records=list(by_url.get(url, [])))

    import cyvcf2  # noqa: PLC0415 — test fixture local import

    monkeypatch.setattr(cyvcf2, "VCF", _factory)

    with duckdb_connection() as conn:
        # Mix real positions on chr22 with a -1 ClinVar sentinel.
        _seed_clinvar_active(conn, rows=[("22", 17007792), ("22", -1)])
        _seed_user_variants(conn, [("22", 17007792)])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()

    # The loader must produce only positive ranges.
    for region in captured_regions:
        match = re.match(r"chr[^:]+:(-?\d+)-(-?\d+)", region)
        assert match is not None, f"malformed region: {region!r}"
        start = int(match.group(1))
        end = int(match.group(2))
        assert start > 0, f"non-positive start in region {region!r}"
        assert end > 0, f"non-positive end in region {region!r}"
    # And the legitimate position landed under the new version with a
    # populated af_global — proves the v4.1 INFO key fix is wired
    # through the same flow.
    assert result.source_version_id is not None
    assert result.rows_loaded == 1
    with duckdb_connection() as conn:
        af_global = conn.execute(
            "SELECT af_global FROM gnomad_frequencies WHERE source_version_id = ?",
            [result.source_version_id],
        ).fetchone()
    assert af_global is not None
    assert af_global[0] == pytest.approx(1.84e-06, abs=1e-09)


# ---------------------------------------------------------------------------
# htslib transient-error recovery (HTTP/2 framing → BGZF illegal-seek)
# ---------------------------------------------------------------------------


def test_scan_for_htslib_errors_recognizes_runtime_error_tokens() -> None:
    """``_scan_for_htslib_errors`` flags the three htslib runtime-error tokens.

    The token set was chosen from real-data verification #2 stderr
    samples; the loader treats any one of them as proof the VCF
    handle's iterator state is corrupted and must be reopened. A
    benign log line (no token) must not flag, so the scanner can be
    called after every region's iteration without false positives.
    """
    assert _scan_for_htslib_errors(b"") is False
    assert _scan_for_htslib_errors(b"benign stderr output, nothing alarming") is False
    assert (
        _scan_for_htslib_errors(
            b"[E::easy_errno] Libcurl reported error 16 (Error in the HTTP2 framing layer)",
        )
        is True
    )
    assert (
        _scan_for_htslib_errors(
            b"[E::bgzf_read_block] Failed to read BGZF block data at offset 100",
        )
        is True
    )
    assert (
        _scan_for_htslib_errors(
            b"[E::hts_itr_next] Failed to seek to offset 12345: Illegal seek",
        )
        is True
    )


@dataclass
class _CorruptingFakeVCF:
    """FakeVCF that simulates htslib transient corruption on demand.

    Parameters mirror :class:`_FakeVCF` plus ``corrupt_regions`` —
    the set of region strings on which the VCF will (a) write the
    htslib BGZF / libcurl error tokens to the live fd 2 and (b)
    yield zero records, simulating htslib's silent-after-corruption
    behavior. ``opened_regions`` records every region call so tests
    can verify reopens and retries.
    """

    records: list[FakeVariant]
    corrupt_regions: set[str] = field(default_factory=set)
    opened_regions: list[str] = field(default_factory=list)

    def __call__(self, region: str) -> Iterable[FakeVariant]:
        self.opened_regions.append(region)
        if region in self.corrupt_regions:
            # Simulate htslib's stderr emissions during a libcurl
            # HTTP/2 framing failure. The loader's _StderrTap captures
            # these via fd 2 and flags the iterator as corrupted.
            import os as _os  # noqa: PLC0415 — test-local

            _os.write(
                2,
                b"[E::easy_errno] Libcurl reported error 16 "
                b"(Error in the HTTP2 framing layer)\n"
                b"[E::bgzf_read_block] Failed to read BGZF block "
                b"data at offset 100 expected 1024 bytes; hread returned -1\n",
            )
            return iter(())
        match = re.match(r"chr([^:]+):(\d+)-(\d+)", region)
        if match is None:
            return iter(())
        chrom = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3))
        return (
            r
            for r in self.records
            if (r.CHROM.removeprefix("chr") == chrom) and start <= r.POS <= end
        )

    def close(self) -> None:
        return


class _ReopenTrackingFactory:
    """VCF factory that returns corrupting fakes for the first open per URL.

    The first ``VCF(url)`` call for any URL returns a fake whose
    ``corrupt_regions`` is configured to fail on the named region;
    subsequent opens of the same URL return clean fakes. Tests use
    this to assert that the loader detects corruption on the first
    attempt, reopens the VCF, and recovers the data on the retry.
    """

    def __init__(
        self,
        by_url: dict[str, list[FakeVariant]],
        *,
        corrupt_first_on: dict[str, set[str]] | None = None,
        always_corrupt_on: dict[str, set[str]] | None = None,
    ) -> None:
        self.by_url = by_url
        self.corrupt_first_on = corrupt_first_on or {}
        self.always_corrupt_on = always_corrupt_on or {}
        self.openings: list[str] = []
        self.fakes: list[_CorruptingFakeVCF] = []

    def __call__(self, url: str) -> _CorruptingFakeVCF:
        is_first_open = self.openings.count(url) == 0
        self.openings.append(url)
        always = self.always_corrupt_on.get(url, set())
        first_only = self.corrupt_first_on.get(url, set()) if is_first_open else set()
        fake = _CorruptingFakeVCF(
            records=list(self.by_url.get(url, [])),
            corrupt_regions=set(always) | set(first_only),
        )
        self.fakes.append(fake)
        return fake


def test_load_chromosome_recovers_from_htslib_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient htslib BGZF/HTTP2 error on a region → reopen + retry → records land.

    Mimics the verification-2 failure mode: a libcurl HTTP/2 framing
    error mid-stream silently zeros the VCF iterator, htslib spews
    seek-error lines to stderr, and the loader must detect that
    corruption and reopen against the same URL before the affected
    region can yield records again. The fake here corrupts the first
    open's iteration on the merged exomes region; the loader's retry
    path must reopen, re-iterate, and land both records.
    """
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    records = [
        _make_record("chr22", 1000, "A", "C", {"AF": 0.05, "AC": 5, "AN": 100}),
        _make_record("chr22", 2000, "G", "T", {"AF": 0.10, "AC": 10, "AN": 100}),
    ]
    exomes_url = GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22")
    genomes_url = GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22")
    by_url: dict[str, list[FakeVariant]] = {
        exomes_url: records,
        genomes_url: [],
    }
    # Default coalesce distance is 50 kb, so positions 1000 and 2000
    # merge into a single tabix range "chr22:1000-2000".
    fail_region = "chr22:1000-2000"
    factory = _ReopenTrackingFactory(
        by_url=by_url,
        corrupt_first_on={exomes_url: {fail_region}},
    )

    import cyvcf2  # noqa: PLC0415 — test fixture local import

    monkeypatch.setattr(cyvcf2, "VCF", factory)

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 1000), ("22", 2000)])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()

    # Both records landed despite the first attempt yielding none.
    assert result.rows_loaded == 2
    # The exomes URL was opened more than once: initial open + at least
    # one reopen triggered by the corruption detector.
    assert factory.openings.count(exomes_url) >= 2, factory.openings
    # The genomes URL never tripped a reopen (no corruption configured).
    assert factory.openings.count(genomes_url) == 1, factory.openings


def test_load_chromosome_dedups_partial_yields_across_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry that re-yields the same records → seen-keys dedups → no duplicates.

    Cyvcf2 partial yields before a corruption event are not retracted
    by the retry; the loader's per-chromosome seen-keys set must catch
    the re-yielded records on the second attempt so the same
    ``(chrom, pos, ref, alt)`` cannot land twice. The fake here
    yields one good record on the merged region, then corrupts (the
    second record is "lost" to the corruption event); the retry on
    the reopened handle yields both. The end-state DB must contain
    each record exactly once.
    """
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    # Two records on chr22 in the same coalesced region. The fake's
    # iterator yields the first, then "corrupts" before the second.
    # On retry, the (clean) reopened iterator yields both.
    records = [
        _make_record("chr22", 1000, "A", "C", {"AF": 0.05, "AC": 5, "AN": 100}),
        _make_record("chr22", 2000, "G", "T", {"AF": 0.10, "AC": 10, "AN": 100}),
    ]
    exomes_url = GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22")
    genomes_url = GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22")
    fail_region = "chr22:1000-2000"

    @dataclass
    class _PartialThenCorruptVCF:
        records: list[FakeVariant]
        corrupt_region: str
        is_first_open: bool
        opened_regions: list[str] = field(default_factory=list)

        def __call__(self, region: str) -> Iterable[FakeVariant]:
            self.opened_regions.append(region)
            match = re.match(r"chr([^:]+):(\d+)-(\d+)", region)
            if match is None:
                return iter(())
            chrom = match.group(1)
            start = int(match.group(2))
            end = int(match.group(3))
            matching = [
                r
                for r in self.records
                if (r.CHROM.removeprefix("chr") == chrom) and start <= r.POS <= end
            ]
            if region == self.corrupt_region and self.is_first_open:

                def _gen() -> Iterator[FakeVariant]:
                    # Yield first record successfully, then "corrupt"
                    # the stream — write htslib stderr tokens and stop
                    # yielding. The loader sees one record arrive,
                    # then the iterator exhausts, then the post-region
                    # check finds the error tokens.
                    if matching:
                        yield matching[0]
                    import os as _os  # noqa: PLC0415 — test-local

                    _os.write(
                        2,
                        b"[E::easy_errno] Libcurl reported error 16 "
                        b"(Error in the HTTP2 framing layer)\n"
                        b"[E::hts_itr_next] Failed to seek to offset "
                        b"999999999999: Illegal seek\n",
                    )

                return _gen()
            return iter(matching)

        def close(self) -> None:
            return

    openings: list[str] = []

    def _factory(url: str) -> _PartialThenCorruptVCF:
        is_first = openings.count(url) == 0
        openings.append(url)
        return _PartialThenCorruptVCF(
            records=list({exomes_url: records, genomes_url: []}.get(url, [])),
            corrupt_region=fail_region,
            is_first_open=is_first,
        )

    import cyvcf2  # noqa: PLC0415 — test fixture local import

    monkeypatch.setattr(cyvcf2, "VCF", _factory)

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 1000), ("22", 2000)])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()

    assert result.rows_loaded == 2
    # Exomes reopened at least once; record at pos 1000 was yielded on
    # both attempts but only landed once thanks to seen_keys dedup.
    assert openings.count(exomes_url) >= 2
    with duckdb_connection() as conn:
        rows = conn.execute(
            "SELECT pos_grch38, COUNT(*) FROM gnomad_frequencies"
            " WHERE source_version_id = ?"
            " GROUP BY pos_grch38 ORDER BY pos_grch38",
            [result.source_version_id],
        ).fetchall()
    assert rows == [(1000, 1), (2000, 1)]


def test_load_chromosome_fails_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent corruption on the same region → GnomadRemoteIterationError.

    When every reopen produces another error on the same region —
    suggesting something more durable than a transient HTTP/2 blip
    (network outage, server-side rotation) — the chromosome must
    fail with an explicit error rather than silently producing a
    low-row-count run that looks like success. The error is wrapped
    in the loader's per-chromosome failure path: ``capture_failure``
    re-raises it after the summary log, and the chromosome lands in
    ``chromosomes_failed`` on the result.
    """
    init_databases()
    _enable_external_calls()
    _patch_check_libcurl(monkeypatch)

    exomes_url = GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22")
    genomes_url = GNOMAD_URL_TEMPLATE.format(data_type="genomes", chrom="22")
    fail_region = "chr22:1000-1000"
    by_url: dict[str, list[FakeVariant]] = {
        exomes_url: [
            _make_record("chr22", 1000, "A", "C", {"AF": 0.05, "AC": 5, "AN": 100}),
        ],
        genomes_url: [],
    }
    factory = _ReopenTrackingFactory(
        by_url=by_url,
        always_corrupt_on={exomes_url: {fail_region}},
    )

    import cyvcf2  # noqa: PLC0415 — test fixture local import

    monkeypatch.setattr(cyvcf2, "VCF", factory)

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 1000)])
        audited, http = _mock_audited_client()
        try:
            with pytest.raises(GnomadRemoteIterationError, match="region"):
                load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()

    # Verifies the reopen budget was actually exhausted, not short-
    # circuited: every attempt corresponds to one VCF open on the URL.
    assert factory.openings.count(exomes_url) == MAX_REMOTE_REGION_ATTEMPTS
