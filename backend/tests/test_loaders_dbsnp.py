"""Tests for :mod:`genome.annotate.loaders.dbsnp`.

Covers the source pre-flight (libcurl + ``##contig`` accession validation),
the record projection (rsid from the ID column — never ``INFO/RS``;
multi-allelic kept as an array; ``VC`` -> ``variant_class``; ``GENEINFO`` ->
``gene_symbols``; ``is_clinical`` from ``CLNSIG``; ``functional_class`` /
``pos_grch37`` NULL; RefSeq-accession -> chrom incl. Y + MT), the ``user_only``
filter, rsid dedup across an htslib reopen, the version-pointer lifecycle
(short-circuit / force / version override / partial-failure / resume /
``--chromosomes``), orphan version-row cleanup (finding-015), the
external-calls-disabled refusal, and the CLI surface.

Fixtures' INFO keys are the real dbSNP build-157 keys ratified by the
finding-013 gate (``RS``, ``VC``, ``GENEINFO``, ``CLNSIG``, ``dbSNPBuildID``,
``FREQ``, ``GNO``, ``SSR``); only the values are synthesised. cyvcf2 is mocked
so the tests touch neither the network nor the 29 GB source.
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

from genome.annotate.loaders import dbsnp as dbsnp_loader
from genome.annotate.loaders.dbsnp import (
    DBSNP_VCF_URL,
    DBSNP_VERSION,
    SUPPORTED_CHROMS,
    DbsnpSourceContigError,
    _check_source_available,
    _record_to_dbsnp_row,
    load,
)
from genome.annotate.remote_tabix import RemoteTabixLibcurlMissingError
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

_CHROM_TO_ACC: dict[str, str] = dict(dbsnp_loader._CHROM_TO_ACCESSION)  # noqa: SLF001
_ACCESSIONS: tuple[str, ...] = tuple(_CHROM_TO_ACC.values())
_FULL_HEADER: str = "##fileformat=VCFv4.2\n##dbSNP_BUILD_ID=157\n" + "\n".join(
    f"##contig=<ID={acc}>" for acc in _ACCESSIONS
)


# ---------------------------------------------------------------------------
# Fixtures: fake cyvcf2.VCF + record shapes.
# ---------------------------------------------------------------------------


@dataclass
class FakeVariant:
    """Stand-in for a cyvcf2 dbSNP record.

    ``CHROM`` is the RefSeq accession (what dbSNP emits); ``ID`` is the
    ``rs<n>`` string the loader reads as ``rsid``. ``INFO`` is a real ``dict``,
    matching cyvcf2's KeyError-on-missing behaviour.
    """

    ID: str
    CHROM: str
    POS: int
    REF: str
    ALT: tuple[str, ...]
    INFO: dict[str, object] = field(default_factory=dict)
    FILTER: str | None = None


@dataclass
class _FakeVCF:
    """Stand-in for cyvcf2.VCF: yields records inside an accession region."""

    records: list[FakeVariant]
    raw_header: str = _FULL_HEADER
    closed: bool = False

    def __call__(self, region: str) -> Iterable[FakeVariant]:
        match = re.match(r"(NC_\d+\.\d+):(\d+)-(\d+)", region)
        if match is None:
            return iter(())
        accession = match.group(1)
        start, end = int(match.group(2)), int(match.group(3))
        return (r for r in self.records if accession == r.CHROM and start <= r.POS <= end)

    def close(self) -> None:
        self.closed = True


class _VCFFactory:
    """Builds a fresh :class:`_FakeVCF` per open (dbSNP has one URL)."""

    def __init__(self, records: list[FakeVariant], *, raw_header: str = _FULL_HEADER) -> None:
        self.records = records
        self.raw_header = raw_header
        self.openings: list[str] = []

    def __call__(self, url: str) -> _FakeVCF:
        self.openings.append(url)
        return _FakeVCF(records=list(self.records), raw_header=self.raw_header)


@pytest.fixture(autouse=True)
def _isolated(
    isolated_settings: dict[str, str],  # noqa: ARG001 — required by the fixture chain
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Per-test isolation: fresh env + reset structlog."""
    yield
    structlog.reset_defaults()
    monkeypatch.undo()


@pytest.fixture(autouse=True)
def _ensure_dbsnp_registered() -> Iterator[None]:
    """Re-register the loader so the registry survives any test order."""
    from genome.annotate.registry import _LOADERS, register_loader  # noqa: PLC0415

    _LOADERS.pop("dbsnp", None)
    register_loader("dbsnp", dbsnp_loader.refresh)
    try:
        yield
    finally:
        _LOADERS.pop("dbsnp", None)


def _patch_cyvcf2(monkeypatch: pytest.MonkeyPatch, factory: _VCFFactory) -> None:
    import cyvcf2  # noqa: PLC0415 — test fixture local import

    monkeypatch.setattr(cyvcf2, "VCF", factory)


def _patch_check_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the source pre-flight (exercised separately) for load() tests."""
    monkeypatch.setattr(dbsnp_loader, "_check_source_available", lambda: None)


def _enable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='true' WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _mock_audited_client() -> tuple[ExternalClient, httpx.Client]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "0"})

    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=_DEFAULT_TIMEOUT_S)
    return ExternalClient(f"annotations_{dbsnp_loader.SOURCE_DB}", client=http), http


def _audit_rows() -> list[tuple[object, ...]]:
    with sqlcipher_connection() as conn:
        return conn.execute(
            "SELECT action_type, resource_type, resource_id, operation_details,"
            " external_call, external_endpoint, external_payload_hash"
            " FROM audit_log ORDER BY log_id",
        ).fetchall()


def _seed_user_variants(conn, rows: list[tuple[str, int]]) -> None:  # type: ignore[no-untyped-def]
    for chrom, pos in rows:
        conn.execute(
            """
            INSERT INTO variants_master (chrom, pos_grch38, ref_allele, alt_allele)
            VALUES (?::chromosome_enum, ?, 'A', 'C')
            """,
            [chrom, pos],
        )


def _make_record(  # noqa: PLR0913 — wrapper over a structural constructor
    rsid: str,
    chrom: str,
    pos: int,
    ref: str,
    alts: list[str],
    info: dict[str, object] | None = None,
    filter_value: str | None = None,
) -> FakeVariant:
    return FakeVariant(
        ID=rsid,
        CHROM=_CHROM_TO_ACC[chrom],
        POS=pos,
        REF=ref,
        ALT=tuple(alts),
        INFO=dict(info or {}),
        FILTER=filter_value,
    )


def _snv(rsid: str, chrom: str, pos: int, ref: str, alt: str) -> FakeVariant:
    """A typical single-allele SNV record with the real dbSNP INFO key set."""
    return _make_record(
        rsid,
        chrom,
        pos,
        ref,
        [alt],
        {"RS": int(rsid.removeprefix("rs")), "SSR": 0, "VC": "SNV", "GNO": True},
    )


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def test_preflight_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open succeeds + every canonical accession present -> no exception."""
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=[]))
    _check_source_available()


def test_preflight_libcurl_fail_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cyvcf2 open failure -> RemoteTabixLibcurlMissingError mentioning libcurl."""
    import cyvcf2  # noqa: PLC0415

    def _boom(_url: str) -> _FakeVCF:
        msg = "tabix index failed (htslib without libcurl)"
        raise RuntimeError(msg)

    monkeypatch.setattr(cyvcf2, "VCF", _boom)
    with pytest.raises(RemoteTabixLibcurlMissingError, match="libcurl"):
        _check_source_available()


def test_preflight_missing_contig_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A header missing a canonical RefSeq accession -> DbsnpSourceContigError."""
    header_no_y = "\n".join(f"##contig=<ID={a}>" for a in _ACCESSIONS if a != "NC_000024.10")
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=[], raw_header=header_no_y))
    with pytest.raises(DbsnpSourceContigError, match="NC_000024"):
        _check_source_available()


# ---------------------------------------------------------------------------
# _record_to_dbsnp_row
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 25, tzinfo=UTC)


def test_record_rsid_from_id_not_info_rs() -> None:
    """rsid comes from record.ID; an overflowed INFO/RS (None) is ignored.

    Mirrors the gate's chr22:10510027 record: ID='rs2517033109' but
    ``INFO/RS`` was set to missing by htslib (value > 2^31).
    """
    record = _make_record(
        "rs2517033109",
        "22",
        10510027,
        "G",
        ["T"],
        {"RS": None, "SSR": 0, "VC": "SNV", "GNO": True},
    )
    row = _record_to_dbsnp_row(record, source_version_id=1, retrieval_datetime=_NOW)
    assert row is not None
    assert row["rsid"] == "rs2517033109"
    assert row["chrom"] == "22"
    assert row["pos_grch38"] == 10510027


def test_record_multiallelic_kept_as_array() -> None:
    """Multi-allelic ALT is kept as a list (not split, not rejected)."""
    record = _make_record(
        "rs2100528659",
        "1",
        1000001,
        "G",
        ["C", "T"],
        {"VC": "SNV", "GENEINFO": "HES4:57801"},
    )
    row = _record_to_dbsnp_row(record, source_version_id=1, retrieval_datetime=_NOW)
    assert row is not None
    assert row["alt_alleles"] == ["C", "T"]


@pytest.mark.parametrize(
    ("vc", "expected"),
    [("SNV", "snv"), ("MNV", "mnv"), ("INS", "in-del"), ("DEL", "in-del"), ("INDEL", "in-del")],
)
def test_record_vc_to_variant_class(vc: str, expected: str) -> None:
    record = _make_record("rs1", "1", 100, "A", ["C"], {"VC": vc})
    row = _record_to_dbsnp_row(record, source_version_id=1, retrieval_datetime=_NOW)
    assert row is not None
    assert row["variant_class"] == expected


def test_record_unknown_vc_maps_to_null() -> None:
    record = _make_record("rs1", "1", 100, "A", ["C"], {"VC": "WEIRD"})
    row = _record_to_dbsnp_row(record, source_version_id=1, retrieval_datetime=_NOW)
    assert row is not None
    assert row["variant_class"] is None


def test_record_geneinfo_to_gene_symbols() -> None:
    single = _make_record("rs1", "1", 100, "A", ["C"], {"VC": "SNV", "GENEINFO": "HES4:57801"})
    multi = _make_record("rs2", "1", 200, "A", ["C"], {"VC": "SNV", "GENEINFO": "A:1|B:2"})
    absent = _make_record("rs3", "1", 300, "A", ["C"], {"VC": "SNV"})
    row_single = _record_to_dbsnp_row(single, source_version_id=1, retrieval_datetime=_NOW)
    row_multi = _record_to_dbsnp_row(multi, source_version_id=1, retrieval_datetime=_NOW)
    row_absent = _record_to_dbsnp_row(absent, source_version_id=1, retrieval_datetime=_NOW)
    assert row_single is not None
    assert row_multi is not None
    assert row_absent is not None
    assert row_single["gene_symbols"] == ["HES4"]
    assert row_multi["gene_symbols"] == ["A", "B"]
    assert row_absent["gene_symbols"] is None


def test_record_is_clinical_from_clnsig() -> None:
    """is_clinical is True iff the record carries CLNSIG."""
    clinical = _make_record("rs1", "1", 100, "A", ["C"], {"VC": "SNV", "CLNSIG": "5"})
    plain = _make_record("rs2", "1", 200, "A", ["C"], {"VC": "SNV"})
    row_clin = _record_to_dbsnp_row(clinical, source_version_id=1, retrieval_datetime=_NOW)
    row_plain = _record_to_dbsnp_row(plain, source_version_id=1, retrieval_datetime=_NOW)
    assert row_clin is not None
    assert row_plain is not None
    assert row_clin["is_clinical"] is True
    assert row_plain["is_clinical"] is False


def test_record_functional_class_and_pos37_null() -> None:
    """functional_class and pos_grch37 are NULL in PR B (deferred to VEP)."""
    record = _make_record("rs1", "1", 100, "A", ["C"], {"VC": "SNV", "U5": True, "NSM": True})
    row = _record_to_dbsnp_row(record, source_version_id=1, retrieval_datetime=_NOW)
    assert row is not None
    assert row["functional_class"] is None
    assert row["pos_grch37"] is None


def test_record_accession_translation_incl_y_mt() -> None:
    """RefSeq accessions translate to canonical chroms, including Y and MT."""
    y_record = _make_record("rs10", "Y", 2800017, "A", ["C"], {"VC": "SNV"})
    mt_record = _make_record("rs11", "MT", 73, "A", ["G"], {"VC": "SNV"})
    y_row = _record_to_dbsnp_row(y_record, source_version_id=1, retrieval_datetime=_NOW)
    mt_row = _record_to_dbsnp_row(mt_record, source_version_id=1, retrieval_datetime=_NOW)
    assert y_row is not None
    assert mt_row is not None
    assert y_record.CHROM == "NC_000024.10"
    assert mt_record.CHROM == "NC_012920.1"
    assert y_row["chrom"] == "Y"
    assert mt_row["chrom"] == "MT"


def test_record_skips_missing_rsid() -> None:
    """A record with no usable ID column is dropped (rsid is NOT NULL)."""
    record = FakeVariant(ID=".", CHROM="NC_000001.11", POS=100, REF="A", ALT=("C",))
    assert _record_to_dbsnp_row(record, source_version_id=1, retrieval_datetime=_NOW) is None


# ---------------------------------------------------------------------------
# user_only filter + end-to-end load
# ---------------------------------------------------------------------------


def test_load_user_only_filter_and_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """user_only ignores ClinVar/GWAS; only user positions land; arrays persist."""
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)

    records = [
        _make_record(
            "rs2100528659",
            "22",
            1000,
            "G",
            ["C", "T"],
            {"VC": "SNV", "GENEINFO": "HES4:57801", "CLNSIG": "5"},
        ),
        # A dbSNP record at a NON-user position inside the coalesced range:
        # must be dropped by the precise per-position membership check.
        _snv("rs999", "22", 4000, "A", "C"),
        _snv("rs2061858963", "22", 4500, "T", "C"),
    ]
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=records))

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 1000), ("22", 4500)])
        # Seed a ClinVar row at a different position — user_only must ignore it.
        cv_id = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
            source_url=None,
            source_file_hash="c" * 64,
            source_file_size=1,
            record_count=1,
        )
        conn.execute(
            """
            INSERT INTO clinvar_annotations (
                clinvar_id, variation_id, chrom, pos_grch38, source_version_id, retrieval_date
            )
            VALUES (1, 'CV1', '22'::chromosome_enum, 2000, ?, CURRENT_TIMESTAMP)
            """,
            [cv_id],
        )
        flip_to_new_version(
            conn,
            source="clinvar",
            table="clinvar_annotations",
            new_source_version_id=cv_id,
        )
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()
        rows = conn.execute(
            "SELECT rsid, pos_grch38, alt_alleles, gene_symbols, variant_class, is_clinical"  # noqa: S608
            f" FROM {dbsnp_loader._TARGET_TABLE}"  # noqa: SLF001
            " WHERE source_version_id = ?",
            [result.source_version_id],
        ).fetchall()
    # Composition has only user + union_total (no clinvar/gwas keys).
    assert result.filter_set_composition == {"user": 2, "union_total": 2}
    # User positions 1000 + 4500 landed; the non-user dbSNP site (4000) and the
    # ClinVar-only position (2000) did not.
    assert len(rows) == 2
    by_pos = {r[1]: r for r in rows}
    assert sorted(by_pos) == [1000, 4500]
    _, _, alt_alleles, gene_symbols, variant_class, is_clinical = by_pos[1000]
    assert list(alt_alleles) == ["C", "T"]
    assert list(gene_symbols) == ["HES4"]
    assert variant_class == "snv"
    assert bool(is_clinical) is True
    assert result.multiallelic_rows == 1
    assert result.is_clinical_rows == 1
    assert result.gene_symbols_present == 1


def test_load_dedups_reyielded_rsid_across_reopen(monkeypatch: pytest.MonkeyPatch) -> None:
    """A record re-yielded after an htslib reopen lands exactly once (rsid dedup)."""
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)

    accession = _CHROM_TO_ACC["22"]
    fail_region = f"{accession}:1000-2000"
    records = [_snv("rs100", "22", 1000, "A", "C"), _snv("rs200", "22", 2000, "G", "T")]

    @dataclass
    class _PartialThenCorruptVCF:
        records: list[FakeVariant]
        is_first_open: bool

        def __call__(self, region: str) -> Iterable[FakeVariant]:
            match = re.match(r"(NC_\d+\.\d+):(\d+)-(\d+)", region)
            if match is None:
                return iter(())
            acc, start, end = match.group(1), int(match.group(2)), int(match.group(3))
            matching = [r for r in self.records if acc == r.CHROM and start <= r.POS <= end]
            if region == fail_region and self.is_first_open:

                def _gen() -> Iterator[FakeVariant]:
                    import os as _os  # noqa: PLC0415

                    if matching:
                        yield matching[0]
                    _os.write(
                        2,
                        b"[E::easy_errno] Libcurl reported error 16 "
                        b"(Error in the HTTP2 framing layer)\n"
                        b"[E::hts_itr_next] Failed to seek: Illegal seek\n",
                    )

                return _gen()
            return iter(matching)

        def close(self) -> None:
            return

    openings: list[str] = []

    def _factory(_url: str) -> _PartialThenCorruptVCF:
        is_first = len(openings) == 0
        openings.append(_url)
        return _PartialThenCorruptVCF(records=list(records), is_first_open=is_first)

    import cyvcf2  # noqa: PLC0415

    monkeypatch.setattr(cyvcf2, "VCF", _factory)

    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 1000), ("22", 2000)])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()
        per_rsid = conn.execute(
            f"SELECT rsid, COUNT(*) FROM {dbsnp_loader._TARGET_TABLE}"  # noqa: SLF001, S608
            " WHERE source_version_id = ? GROUP BY rsid ORDER BY rsid",
            [result.source_version_id],
        ).fetchall()
    assert result.rows_loaded == 2
    assert per_rsid == [("rs100", 1), ("rs200", 1)]
    assert len(openings) >= 2  # at least one reopen happened


# ---------------------------------------------------------------------------
# Version lifecycle
# ---------------------------------------------------------------------------


def _seed_current_version(conn, version: str = DBSNP_VERSION) -> int:  # type: ignore[no-untyped-def]
    sv_id = insert_source_version(
        conn,
        source_db=dbsnp_loader.SOURCE_DB,
        version=version,
        source_url=DBSNP_VCF_URL,
        source_file_hash=f"dbsnp_{version}",
        source_file_size=0,
        record_count=0,
    )
    flip_to_new_version(
        conn,
        source=dbsnp_loader.SOURCE_DB,
        table=dbsnp_loader._TARGET_TABLE,  # noqa: SLF001
        new_source_version_id=sv_id,
    )
    return sv_id


def test_short_circuit_already_current(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)
    factory = _VCFFactory(records=[])
    _patch_cyvcf2(monkeypatch, factory)
    with duckdb_connection() as conn:
        sv_id = _seed_current_version(conn)
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited)
        finally:
            audited.close()
            http.close()
    assert result.source_version_id == sv_id
    assert result.pointer_flipped is False
    assert result.rows_loaded == 0
    assert factory.openings == []  # no iteration ran


def test_force_reallocates_new_version(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=[_snv("rs1", "22", 1000, "A", "C")]))
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 1000)])
        old_sv_id = _seed_current_version(conn)
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()
    assert result.source_version_id is not None
    assert result.source_version_id != old_sv_id
    assert result.pointer_flipped is False  # partial (--chromosomes) run
    assert result.rows_loaded == 1


def test_version_override_different_label(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=[_snv("rs1", "22", 100, "A", "C")]))
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])
        old_sv_id = _seed_current_version(conn)
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, version="158", chromosomes=["22"])
        finally:
            audited.close()
            http.close()
        version_rows = conn.execute(
            "SELECT version FROM annotation_source_versions WHERE source_db='dbsnp'"
            " ORDER BY source_version_id",
        ).fetchall()
    assert result.version_label == "158"
    assert result.source_version_id != old_sv_id
    assert version_rows == [(DBSNP_VERSION,), ("158",)]


def test_partial_failure_leaves_pointer_unflipped(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)

    boom_acc = _CHROM_TO_ACC["7"]

    @dataclass
    class _BoomVCF:
        records: list[FakeVariant]

        def __call__(self, region: str) -> Iterable[FakeVariant]:
            if region.startswith(boom_acc):
                msg = "boom on chr7"
                raise RuntimeError(msg)
            match = re.match(r"(NC_\d+\.\d+):(\d+)-(\d+)", region)
            if match is None:
                return iter(())
            acc, start, end = match.group(1), int(match.group(2)), int(match.group(3))
            return (r for r in self.records if acc == r.CHROM and start <= r.POS <= end)

        def close(self) -> None:
            return

    all_records = [_snv(f"rs{i}", c, 100, "A", "C") for i, c in enumerate(SUPPORTED_CHROMS, 1)]

    def _factory(_url: str) -> _BoomVCF:
        return _BoomVCF(records=list(all_records))

    import cyvcf2  # noqa: PLC0415

    monkeypatch.setattr(cyvcf2, "VCF", _factory)

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
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='dbsnp'",
        ).fetchone()
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db='dbsnp'",
        ).fetchone()
    assert pointer is None  # never flipped
    # The version row is preserved (chr1-6 reference it) — not orphan-cleaned.
    assert version_rows is not None
    assert version_rows[0] == 1


def test_resume_continues_and_flips(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)
    records = [_snv(f"rs{i}", c, 100, "A", "C") for i, c in enumerate(SUPPORTED_CHROMS, 1)]
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=records))

    with duckdb_connection() as conn:
        for c in SUPPORTED_CHROMS:
            _seed_user_variants(conn, [(c, 100)])
        # Pre-seed an in-flight version with chr22 already populated.
        sv_id = insert_source_version(
            conn,
            source_db=dbsnp_loader.SOURCE_DB,
            version=DBSNP_VERSION,
            source_url=DBSNP_VCF_URL,
            source_file_hash="partial",
            source_file_size=0,
            record_count=None,
        )
        conn.execute(
            f"""
            INSERT INTO {dbsnp_loader._TARGET_TABLE} (
                dbsnp_id, rsid, chrom, pos_grch38, pos_grch37, ref_allele,
                alt_alleles, variant_class, gene_symbols, functional_class,
                is_clinical, source_version_id, retrieval_date
            )
            VALUES (
                1, 'rs22', '22'::chromosome_enum, 100, NULL, 'A',
                ['C'], 'snv', NULL, NULL, FALSE, ?, CURRENT_TIMESTAMP
            )
            """,  # noqa: SLF001, S608
            [sv_id],
        )
        conn.commit()
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, resume=True)
        finally:
            audited.close()
            http.close()
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='dbsnp'",
        ).fetchone()
    assert result.source_version_id == sv_id
    assert result.pointer_flipped is True
    assert pointer is not None
    assert pointer[0] == sv_id


def test_chromosomes_restrict_does_not_flip(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=[_snv("rs1", "22", 100, "A", "C")]))
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])
        prior_sv_id = _seed_current_version(conn, version="156")
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='dbsnp'",
        ).fetchone()
    assert result.pointer_flipped is False
    assert pointer is not None
    assert pointer[0] == prior_sv_id  # still the prior version


# ---------------------------------------------------------------------------
# Orphan version-row cleanup (finding-015)
# ---------------------------------------------------------------------------


def test_cleanup_orphan_when_run_yields_zero_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=[]))  # no records anywhere
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True, chromosomes=["22"])
        finally:
            audited.close()
            http.close()
        version_count = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db='dbsnp'",
        ).fetchone()
    assert result.source_version_id is None
    assert result.rows_loaded == 0
    assert version_count is not None
    assert version_count[0] == 0  # orphan cleaned up


def test_cleanup_orphan_when_first_chrom_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)

    class _BoomOnOpen:
        def __call__(self, _url: str) -> _FakeVCF:
            msg = "boom on first open"
            raise RuntimeError(msg)

    import cyvcf2  # noqa: PLC0415

    monkeypatch.setattr(cyvcf2, "VCF", _BoomOnOpen())
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("1", 100)])
        audited, http = _mock_audited_client()
        try:
            with pytest.raises(RuntimeError, match="boom on first open"):
                load(conn, audited, force=True, chromosomes=["1"])
        finally:
            audited.close()
            http.close()
        version_count = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db='dbsnp'",
        ).fetchone()
    assert version_count is not None
    assert version_count[0] == 0


def test_resume_does_not_clean_preexisting_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)

    class _BoomOnOpen:
        def __call__(self, _url: str) -> _FakeVCF:
            msg = "boom on resume"
            raise RuntimeError(msg)

    import cyvcf2  # noqa: PLC0415

    monkeypatch.setattr(cyvcf2, "VCF", _BoomOnOpen())
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("1", 100)])
        preexisting = insert_source_version(
            conn,
            source_db=dbsnp_loader.SOURCE_DB,
            version=DBSNP_VERSION,
            source_url=DBSNP_VCF_URL,
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
        version_ids = conn.execute(
            "SELECT source_version_id FROM annotation_source_versions WHERE source_db='dbsnp'",
        ).fetchall()
    assert version_ids == [(preexisting,)]  # preserved for the next --resume


def test_full_run_flips_and_does_not_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)
    records = [_snv(f"rs{i}", c, 100, "A", "C") for i, c in enumerate(SUPPORTED_CHROMS, 1)]
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=records))
    with duckdb_connection() as conn:
        for c in SUPPORTED_CHROMS:
            _seed_user_variants(conn, [(c, 100)])
        audited, http = _mock_audited_client()
        try:
            result = load(conn, audited, force=True)
        finally:
            audited.close()
            http.close()
        pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db='dbsnp'",
        ).fetchone()
        version_ids = conn.execute(
            "SELECT source_version_id FROM annotation_source_versions WHERE source_db='dbsnp'",
        ).fetchall()
    assert result.source_version_id is not None
    assert result.pointer_flipped is True
    assert result.rows_loaded == len(SUPPORTED_CHROMS)
    assert version_ids == [(result.source_version_id,)]
    assert pointer is not None
    assert pointer[0] == result.source_version_id


# ---------------------------------------------------------------------------
# External-calls-disabled
# ---------------------------------------------------------------------------


def test_external_calls_disabled_blocks_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _patch_check_source(monkeypatch)
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=[]))
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
    expected_pair = 2  # one intent + one blocked row from the pre-flight HEAD
    assert len(rows) >= expected_pair
    with duckdb_connection() as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM {dbsnp_loader._TARGET_TABLE}").fetchone()  # noqa: SLF001, S608
    assert count is not None
    assert count[0] == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_dbsnp_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=[]))
    with duckdb_connection() as conn:
        _seed_current_version(conn)

    result = CliRunner().invoke(app, ["annotate", "refresh", "--source", "dbsnp"])
    assert result.exit_code == 0, result.output
    assert "source_db=dbsnp" in result.output
    assert f"version={DBSNP_VERSION}" in result.output


def test_cli_dbsnp_force_and_chromosomes(monkeypatch: pytest.MonkeyPatch) -> None:
    init_databases()
    _enable_external_calls()
    _patch_check_source(monkeypatch)
    _patch_cyvcf2(monkeypatch, _VCFFactory(records=[_snv("rs1", "22", 100, "A", "C")]))
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 100)])

    result = CliRunner().invoke(
        app,
        ["annotate", "refresh", "--source", "dbsnp", "--force", "--chromosomes", "22"],
    )
    assert result.exit_code == 0, result.output
    assert "source_db=dbsnp" in result.output
    with duckdb_connection() as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM {dbsnp_loader._TARGET_TABLE}").fetchone()  # noqa: SLF001, S608
    assert row is not None
    assert row[0] >= 1
