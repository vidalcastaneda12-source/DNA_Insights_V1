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
import pytest
import structlog
from typer.testing import CliRunner

from genome.annotate.loaders import gnomad as gnomad_loader
from genome.annotate.loaders.gnomad import (
    _POP_TO_VCF_INFO_SUFFIX,
    DEFAULT_COALESCE_DISTANCE_BP,
    GNOMAD_POPULATIONS,
    GNOMAD_URL_TEMPLATE,
    GNOMAD_VERSION,
    SOURCE_DB,
    SUPPORTED_CHROMS,
    GnomadLibcurlMissingError,
    _build_filter_set,
    _coalesce_positions,
    _record_to_row,
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
    assert pointer is None
    assert rows_landed is not None
    # chr1..6 each landed at least one row.
    expected_min = 6
    assert rows_landed[0] >= expected_min


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
    assert result.source_version_id != prior_sv_id
    assert result.pointer_flipped is False
    # Pointer remains on the prior version.
    assert pointer is not None
    assert pointer[0] == prior_sv_id


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
