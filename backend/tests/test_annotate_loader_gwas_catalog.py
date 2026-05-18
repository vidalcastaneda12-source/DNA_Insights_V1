"""Tests for :mod:`genome.annotate.loaders.gwas_catalog`.

Covers the per-field coercions (empty / NA / NR / dash → NULL,
multi-SNP SNPS split into separate emits, empty CHR_POS drop,
scientific-notation p-value parse, multi-valued MAPPED_TRAIT_URI
truncation, EFO trait-ID extraction, sample-size leading-integer
extraction, strongest-SNP-risk-allele extraction with unknown
``?`` sentinel, 95% CI bracket parse), the version-resolution path
(stats endpoint ``{"date": "YYYY-MM-DD"}`` → ``YYYY_MM_DD``,
``releasedate`` defensive alias, malformed-JSON loud-fail,
HTTP-5xx propagation, ZIP archive shape checks), the end-to-end
``refresh`` flow against the checked-in 50-row fixture (wrapped in
a ZIP to match the upstream distribution shape), the supersession
transaction (new version vs same-version ``--force``), the audited
refusal path with ``external_calls_enabled=false``, a 100K-row
benchmark guard against the locked < 30 s ceiling, and the CLI smoke
against ``genome annotate refresh --source gwas_catalog``.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from genome.annotate import downloads as annotate_downloads
from genome.annotate.loaders import gwas_catalog as gwas_loader
from genome.annotate.loaders.gwas_catalog import (
    _CHUNK_SIZE,
    _ZIP_TSV_MEMBER,
    _derive_is_replicated,
    _empty_to_none,
    _extract_trait_id,
    _format_version,
    _insert_chunk,
    _iter_chunks,
    _open_tsv_from_zip,
    _parse_ci,
    _parse_effect_allele,
    _parse_first_uri,
    _parse_float,
    _parse_gwas_catalog,
    _parse_int,
    _parse_p_value,
    _parse_sample_size,
    _parse_stats_release_date,
    _ParsedRow,
    _ParseStats,
    _resolve_version_via_stats,
    _split_snps,
    _stream_bulk_insert,
)
from genome.annotate.registry import get_loader
from genome.cli import app
from genome.db import duckdb_connection, init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.privacy.external_client import (
    ExternalCallError,
    ExternalCallsDisabledError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gwas_catalog_sample.tsv"


# ---------------------------------------------------------------------------
# Per-test isolation (mirror the ClinVar / PharmGKB pattern).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_gwas_registered() -> Iterator[None]:
    """Re-register the loader at test start.

    Other annotate test files install autouse fixtures that wipe the
    registry via ``_clear_loaders_for_testing()`` to keep their cases
    hermetic. When their tests run before ours in a full-suite
    invocation, the side-effect registration from
    ``genome.annotate.loaders.gwas_catalog`` is gone. Re-registering
    here makes our tests order-independent. We pop again on teardown
    so we don't leak the registration into the next test file.
    """
    from genome.annotate.registry import _LOADERS, register_loader  # noqa: PLC0415

    _LOADERS.pop("gwas_catalog", None)
    register_loader("gwas_catalog", gwas_loader.refresh)
    try:
        yield
    finally:
        _LOADERS.pop("gwas_catalog", None)


@pytest.fixture(autouse=True)
def _isolated(
    isolated_settings: dict[str, str],  # noqa: ARG001 — required by the fixture chain
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Per-test isolation: tmp dirs, annotations root, fresh DBs."""
    annotations_root = tmp_path / "annotations-root"
    monkeypatch.setenv("ANNOTATIONS_DOWNLOAD_ROOT", str(annotations_root))
    from genome.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        yield annotations_root
    finally:
        get_settings.cache_clear()


def _wrap_tsv_in_zip(tsv_path: Path, zip_path: Path) -> Path:
    """Pack the fixture TSV into a one-entry ZIP at the EBI member name.

    The upstream distribution shape is a ZIP archive carrying a single
    entry named ``gwas-catalog-download-associations-alt-full.tsv``.
    Test fixtures are kept as plain TSVs for legibility; integration
    tests use this helper to materialize the same on-disk shape the
    loader sees in production.
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(tsv_path, arcname=_ZIP_TSV_MEMBER)
    return zip_path


def _patch_download_to_cache(
    monkeypatch: pytest.MonkeyPatch,
    fixture_path: Path,
    *,
    tmp_path: Path | None = None,
) -> dict[str, int]:
    """Replace ``download_to_cache`` with a stub returning a ZIP of the fixture.

    The stub wraps the plain-TSV fixture in a ZIP archive matching the
    upstream EBI shape (single entry named :data:`_ZIP_TSV_MEMBER`)
    and writes it to ``tmp_path`` (or a fresh ``tempfile.mkdtemp()``
    directory when ``tmp_path`` is None — fixtures live in the
    checked-in tests tree and must not be written to). Returns a call
    counter so tests can assert the cache was hit / skipped as
    expected.
    """
    import hashlib  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    counter: dict[str, int] = {"calls": 0}
    zip_dir = tmp_path if tmp_path is not None else Path(tempfile.mkdtemp())
    zip_path = zip_dir / f"{fixture_path.stem}.zip"
    _wrap_tsv_in_zip(fixture_path, zip_path)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    size = zip_path.stat().st_size

    def _stub(
        source_db: str,  # noqa: ARG001
        url: str,  # noqa: ARG001
        filename: str,  # noqa: ARG001
        *,
        resource_id: str,  # noqa: ARG001
        force: bool = False,  # noqa: ARG001
    ) -> annotate_downloads.DownloadResult:
        counter["calls"] += 1
        return annotate_downloads.DownloadResult(
            path=zip_path,
            sha256=digest,
            size_bytes=size,
        )

    monkeypatch.setattr(gwas_loader, "download_to_cache", _stub)
    return counter


def _patch_resolve_version(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> dict[str, int]:
    """Replace ``_resolve_version_via_stats`` with a stub returning ``version``."""
    counter: dict[str, int] = {"calls": 0}

    def _stub() -> str:
        counter["calls"] += 1
        return version

    monkeypatch.setattr(gwas_loader, "_resolve_version_via_stats", _stub)
    return counter


def _audit_rows() -> list[tuple[object, ...]]:
    with sqlcipher_connection() as conn:
        return conn.execute(
            "SELECT action_type, resource_type, resource_id, operation_details,"
            " external_call, external_endpoint, external_payload_hash"
            " FROM audit_log ORDER BY log_id",
        ).fetchall()


# ---------------------------------------------------------------------------
# Field-level coercions.
# ---------------------------------------------------------------------------


def test_empty_to_none_covers_gwas_missing_tokens() -> None:
    """GWAS Catalog uses empty / NA / NR / dash interchangeably for missing."""
    assert _empty_to_none("") is None
    assert _empty_to_none("  ") is None
    assert _empty_to_none("NA") is None
    assert _empty_to_none("NR") is None
    assert _empty_to_none("-") is None
    assert _empty_to_none("0.42") == "0.42"
    assert _empty_to_none(" Body mass index ") == "Body mass index"


def test_parse_int_handles_missing_and_valid() -> None:
    assert _parse_int("") is None
    assert _parse_int("NA") is None
    assert _parse_int("not-a-number") is None
    assert _parse_int("4781213") == 4781213


def test_parse_float_handles_scientific_notation() -> None:
    """RISK ALLELE FREQUENCY and effect-size columns ship sci notation."""
    assert _parse_float("0.42") == pytest.approx(0.42)
    assert _parse_float("3.4e-2") == pytest.approx(3.4e-2)
    assert _parse_float("1.23E+02") == pytest.approx(123.0)
    assert _parse_float("NR") is None
    assert _parse_float("") is None


def test_parse_p_value_handles_uppercase_and_lowercase_sci() -> None:
    """P-VALUE column ships both ``E`` and ``e`` forms."""
    assert _parse_p_value("2.0E-9") == pytest.approx(2.0e-9)
    assert _parse_p_value("4.5e-12") == pytest.approx(4.5e-12)
    assert _parse_p_value("1E-100") == pytest.approx(1e-100)
    assert _parse_p_value("NA") is None


def test_parse_ci_extracts_brackets() -> None:
    """Numeric bracketed CI parses cleanly."""
    lower, upper = _parse_ci("[1.23-1.56]")
    assert lower == pytest.approx(1.23)
    assert upper == pytest.approx(1.56)


def test_parse_ci_with_trailing_unit_text() -> None:
    """The free-text annotation after the bracket doesn't break the parse."""
    lower, upper = _parse_ci("[2.5-3.1] unit decrease")
    assert lower == pytest.approx(2.5)
    assert upper == pytest.approx(3.1)


def test_parse_ci_pure_text_returns_none_pair() -> None:
    assert _parse_ci("[NR] unit decrease") == (None, None)
    assert _parse_ci("") == (None, None)
    assert _parse_ci("NR") == (None, None)


def test_parse_sample_size_extracts_leading_integer() -> None:
    """Free-form sample-size text → leading comma-grouped integer."""
    assert _parse_sample_size("4,512 European ancestry individuals") == 4512
    assert _parse_sample_size("100 cases, 200 controls") == 100
    assert _parse_sample_size("NR") is None
    assert _parse_sample_size("") is None


def test_derive_is_replicated_true_on_positive_count() -> None:
    assert _derive_is_replicated("2,000 European ancestry individuals") is True
    assert _derive_is_replicated("") is None
    assert _derive_is_replicated("NR") is None
    assert _derive_is_replicated("0") is None


def test_split_snps_single_rsid() -> None:
    """The common case: one rsID per source row."""
    assert _split_snps("rs397704705") == ["rs397704705"]


def test_split_snps_multi_rsid_semicolon() -> None:
    """``;``-separated multi-SNP entries split into N rsIDs."""
    assert _split_snps("rs200200; rs200201") == ["rs200200", "rs200201"]
    assert _split_snps("rs1;rs2;rs3") == ["rs1", "rs2", "rs3"]


def test_split_snps_bare_digits_get_rs_prefix() -> None:
    """Older releases ship bare-digit rsIDs; the loader normalizes them."""
    assert _split_snps("12345; 67890") == ["rs12345", "rs67890"]


def test_split_snps_rejects_non_rsid_tokens() -> None:
    """Star alleles, haplotype text, etc. are rejected (schema requires NOT NULL rsid)."""
    assert _split_snps("CYP2D6*4") == []
    assert _split_snps("HLA-B*57:01") == []
    assert _split_snps("rs1234 x rs5678") == []  # haplotype-intersection marker


def test_split_snps_empty_input() -> None:
    assert _split_snps("") == []
    assert _split_snps("NR") == []
    assert _split_snps("NA") == []


def test_parse_effect_allele_extracts_trailing_letter() -> None:
    """``rsID-allele`` shape: allele is the trailing token."""
    assert _parse_effect_allele("rs397704705-A") == "A"
    assert _parse_effect_allele("rs200200-T") == "T"
    assert _parse_effect_allele("rs900900-G") == "G"


def test_parse_effect_allele_question_mark_returns_none() -> None:
    """``rsID-?`` → effect allele unknown → NULL."""
    assert _parse_effect_allele("rs700700-?") is None


def test_parse_effect_allele_missing_token_returns_none() -> None:
    assert _parse_effect_allele("") is None
    assert _parse_effect_allele("NR") is None


def test_parse_first_uri_single_value() -> None:
    """The common case: one URI, no truncation."""
    uri, truncated = _parse_first_uri("http://www.ebi.ac.uk/efo/EFO_0001065")
    assert uri == "http://www.ebi.ac.uk/efo/EFO_0001065"
    assert truncated is False


def test_parse_first_uri_multi_value_keeps_first_logs_truncation() -> None:
    """Multi-valued URI → keep first; truncation flag is True."""
    uri, truncated = _parse_first_uri(
        "http://www.ebi.ac.uk/efo/EFO_0000384,http://www.ebi.ac.uk/efo/EFO_0000729",
    )
    assert uri == "http://www.ebi.ac.uk/efo/EFO_0000384"
    assert truncated is True


def test_parse_first_uri_empty_returns_none() -> None:
    uri, truncated = _parse_first_uri("")
    assert uri is None
    assert truncated is False


def test_extract_trait_id_efo() -> None:
    assert _extract_trait_id("http://www.ebi.ac.uk/efo/EFO_0001065") == "EFO_0001065"
    assert _extract_trait_id("http://www.ebi.ac.uk/efo/EFO_0004340") == "EFO_0004340"


def test_extract_trait_id_mondo() -> None:
    assert _extract_trait_id("http://purl.obolibrary.org/obo/MONDO_0007254") == "MONDO_0007254"


def test_extract_trait_id_none_input_returns_none() -> None:
    assert _extract_trait_id(None) is None
    assert _extract_trait_id("not-a-uri") is None


# ---------------------------------------------------------------------------
# Version-string parse (stats-endpoint payload).
# ---------------------------------------------------------------------------


def test_parse_stats_release_date_canonical_shape() -> None:
    """The live EBI shape: ``{"date": "YYYY-MM-DD", ...}``."""
    assert _parse_stats_release_date(
        {"date": "2026-04-27", "ensemblbuild": "115"},
    ) == date(2026, 4, 27)


def test_parse_stats_release_date_releasedate_alias() -> None:
    """Defensive accept of the documented ``releasedate`` alias."""
    assert _parse_stats_release_date(
        {"releasedate": "2025-08-12"},
    ) == date(2025, 8, 12)


def test_parse_stats_release_date_prefers_date_over_releasedate() -> None:
    """When both are present, ``date`` wins (it's the canonical key)."""
    assert _parse_stats_release_date(
        {"date": "2026-04-27", "releasedate": "1999-12-31"},
    ) == date(2026, 4, 27)


def test_parse_stats_release_date_missing_field_raises() -> None:
    with pytest.raises(ValueError, match="missing a 'date'"):
        _parse_stats_release_date({"ensemblbuild": "115"})


def test_parse_stats_release_date_non_string_value_raises() -> None:
    with pytest.raises(ValueError, match="missing a 'date'"):
        _parse_stats_release_date({"date": 20260427})


def test_parse_stats_release_date_bad_format_raises() -> None:
    with pytest.raises(ValueError, match="does not match YYYY-MM-DD"):
        _parse_stats_release_date({"date": "April 27, 2026"})


def test_parse_stats_release_date_non_object_raises() -> None:
    with pytest.raises(ValueError, match="expected a JSON object"):
        _parse_stats_release_date("2026-04-27")


def test_format_version_renders_yyyy_mm_dd_with_underscores() -> None:
    """Matches the ClinVar loader convention."""
    assert _format_version(date(2026, 4, 27)) == "2026_04_27"
    assert _format_version(date(2024, 1, 5)) == "2024_01_05"


# ---------------------------------------------------------------------------
# Parser end-to-end against the 50-row fixture.
# ---------------------------------------------------------------------------

_EXPECTED_EMITTED = 51
_EXPECTED_DROPPED_EMPTY_POS = 2
_EXPECTED_MULTI_SNP_EXPANSIONS = 2
_EXPECTED_TRUNCATED_TRAIT_URI = 1


def _load_fixture_rows() -> tuple[list[_ParsedRow], _ParseStats]:
    stats = _ParseStats()
    with _FIXTURE_PATH.open(encoding="utf-8", newline="") as fh:
        rows = list(_parse_gwas_catalog(fh, stats))
    return rows, stats


def test_parse_gwas_catalog_emits_expected_total() -> None:
    """50 source rows → 51 emits after 2 drops + 1 + 2 multi-SNP fanouts."""
    rows, stats = _load_fixture_rows()
    assert len(rows) == _EXPECTED_EMITTED
    assert stats.rows_emitted == _EXPECTED_EMITTED
    expected_source_rows = 50
    assert stats.rows_read == expected_source_rows


def test_parse_gwas_catalog_drops_empty_chr_pos_rows() -> None:
    """Rows with empty CHR_POS land in ``dropped_empty_pos``."""
    _, stats = _load_fixture_rows()
    assert stats.dropped_empty_pos == _EXPECTED_DROPPED_EMPTY_POS


def test_parse_gwas_catalog_counts_multi_snp_expansions() -> None:
    """Rows with multiple SNPs increment the expansion counter once per row."""
    _, stats = _load_fixture_rows()
    assert stats.multi_snp_expansions == _EXPECTED_MULTI_SNP_EXPANSIONS


def test_parse_gwas_catalog_counts_truncated_mapped_trait_uri() -> None:
    """Multi-valued MAPPED_TRAIT_URI rows are counted as truncated."""
    _, stats = _load_fixture_rows()
    assert stats.truncated_mapped_trait_uri == _EXPECTED_TRUNCATED_TRAIT_URI


def test_parse_gwas_catalog_multi_snp_row_emits_n_rsids() -> None:
    """Row with ``rs200200; rs200201`` → two emits sharing the study."""
    rows, _ = _load_fixture_rows()
    matched = [r for r in rows if r.study_accession == "GCST003333"]
    expected_matched = 2
    assert len(matched) == expected_matched
    assert {r.rsid for r in matched} == {"rs200200", "rs200201"}
    # Trait / coordinates / pmid shared across the emits.
    assert {r.pmid for r in matched} == {"33333333"}
    assert {r.trait_name for r in matched} == {"body height"}


def test_parse_gwas_catalog_three_snp_row_emits_three_rsids() -> None:
    """Row with 3 SNPs emits exactly 3."""
    rows, _ = _load_fixture_rows()
    matched = [r for r in rows if r.study_accession == "GCST009999"]
    expected_matched = 3
    assert len(matched) == expected_matched
    assert {r.rsid for r in matched} == {"rs900900", "rs900901", "rs900902"}


def test_parse_gwas_catalog_happy_path_row_column_mapping() -> None:
    """Single-SNP row 1 maps every column the loader reads."""
    rows, _ = _load_fixture_rows()
    happy = next(r for r in rows if r.study_accession == "GCST001234")
    assert happy.rsid == "rs397704705"
    assert happy.chrom == "7"
    assert happy.pos_grch38 == 4781213
    assert happy.pmid == "12345678"
    assert happy.p_value == pytest.approx(2.0e-9)
    assert happy.effect_size == pytest.approx(1.34)
    assert happy.ci_95_lower == pytest.approx(1.23)
    assert happy.ci_95_upper == pytest.approx(1.56)
    assert happy.effect_allele == "A"
    assert happy.effect_allele_freq == pytest.approx(0.42)
    assert happy.trait_id == "EFO_0004340"
    assert happy.trait_name == "body mass index"
    assert happy.mapped_trait_uri == "http://www.ebi.ac.uk/efo/EFO_0004340"
    expected_initial = 4512
    expected_replication = 2000
    assert happy.sample_size_initial == expected_initial
    assert happy.sample_size_replication == expected_replication
    assert happy.is_replicated is True
    # NULL columns we intentionally don't populate in 5.3.
    assert happy.effect_size_unit is None
    assert happy.ancestry is None
    assert happy.other_allele is None


def test_parse_gwas_catalog_sci_notation_p_value_row() -> None:
    """The explicit sci-notation case: ``4.5e-12``."""
    rows, _ = _load_fixture_rows()
    sci = next(r for r in rows if r.study_accession == "GCST005555")
    assert sci.p_value == pytest.approx(4.5e-12)
    assert sci.rsid == "rs500500"


def test_parse_gwas_catalog_unknown_effect_allele_row() -> None:
    """``rs700700-?`` → effect_allele=NULL."""
    rows, _ = _load_fixture_rows()
    unknown = next(r for r in rows if r.study_accession == "GCST007777")
    assert unknown.effect_allele is None
    assert unknown.rsid == "rs700700"


def test_parse_gwas_catalog_nr_risk_freq_row() -> None:
    """``RISK ALLELE FREQUENCY = NR`` → NULL."""
    rows, _ = _load_fixture_rows()
    nr_row = next(r for r in rows if r.study_accession == "GCST008888")
    assert nr_row.effect_allele_freq is None


def test_parse_gwas_catalog_multi_valued_uri_row_keeps_first() -> None:
    """Row with two comma-separated URIs → keeps the first."""
    rows, _ = _load_fixture_rows()
    multi = next(r for r in rows if r.study_accession == "GCST006666")
    assert multi.mapped_trait_uri == "http://www.ebi.ac.uk/efo/EFO_0000384"
    assert multi.trait_id == "EFO_0000384"


def test_parse_gwas_catalog_missing_required_header_raises() -> None:
    """Loud-fail when the GWAS Catalog header drifts."""
    bad_header = "PUBMEDID\tSNPS\nfoo\trs1\n"
    with pytest.raises(ValueError, match="missing expected columns"):
        list(_parse_gwas_catalog(io.StringIO(bad_header), _ParseStats()))


def test_parse_gwas_catalog_no_header_raises() -> None:
    with pytest.raises(ValueError, match="no header row"):
        list(_parse_gwas_catalog(io.StringIO(""), _ParseStats()))


# ---------------------------------------------------------------------------
# _open_tsv_from_zip — upstream ZIP shape sanity checks.
# ---------------------------------------------------------------------------


def test_open_tsv_from_zip_reads_canonical_member(tmp_path: Path) -> None:
    """The ZIP wrapper yields the canonical TSV entry."""
    zip_path = tmp_path / "ok.zip"
    _wrap_tsv_in_zip(_FIXTURE_PATH, zip_path)
    with _open_tsv_from_zip(zip_path) as fh:
        first_line = fh.readline()
    assert first_line.startswith("DATE ADDED TO CATALOG")


def test_open_tsv_from_zip_rejects_non_zip_file(tmp_path: Path) -> None:
    """A plain TSV (not a ZIP) on disk surfaces a clear error."""
    not_a_zip = tmp_path / "plain.tsv"
    not_a_zip.write_text("ABC\tDEF\n1\t2\n", encoding="utf-8")
    with (
        pytest.raises(ValueError, match="not a ZIP archive"),
        _open_tsv_from_zip(not_a_zip),
    ):
        pass


def test_open_tsv_from_zip_rejects_unexpected_member(tmp_path: Path) -> None:
    """A ZIP without the expected TSV entry surfaces a clear error."""
    zip_path = tmp_path / "wrong-name.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("some-other-file.tsv", "ignored\n")
    with (
        pytest.raises(ValueError, match="missing expected entry"),
        _open_tsv_from_zip(zip_path),
    ):
        pass


# ---------------------------------------------------------------------------
# _iter_chunks contract.
# ---------------------------------------------------------------------------


class _CountingRow:
    """Cheap stand-in for ``_ParsedRow`` for the chunk-boundary test."""

    __slots__ = ()


def _gen_dummy_rows(n: int) -> Iterator[_ParsedRow]:
    for _ in range(n):
        yield _CountingRow()  # type: ignore[misc]


def test_iter_chunks_exact_boundary_at_default_chunk_size() -> None:
    """3 x 250K + 1 → three full chunks + tail of 1."""
    full_chunks = 3
    expected_total = full_chunks * _CHUNK_SIZE + 1
    chunks = list(_iter_chunks(_gen_dummy_rows(expected_total), _CHUNK_SIZE))
    chunk_sizes = [len(c) for c in chunks]
    assert chunk_sizes == [_CHUNK_SIZE] * full_chunks + [1]


def test_iter_chunks_handles_zero_rows() -> None:
    assert list(_iter_chunks(_gen_dummy_rows(0), _CHUNK_SIZE)) == []


def test_iter_chunks_single_partial_chunk() -> None:
    chunks = list(_iter_chunks(_gen_dummy_rows(7), _CHUNK_SIZE))
    assert [len(c) for c in chunks] == [7]


# ---------------------------------------------------------------------------
# _stream_bulk_insert chunk counter test (no DB).
# ---------------------------------------------------------------------------


def test_stream_bulk_insert_emits_one_insert_chunk_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every chunk must reach ``_insert_chunk`` and the sizes line up."""
    monkeypatch.setattr(gwas_loader, "_next_association_id", lambda _conn: 1)

    seen: list[int] = []

    def _stub_insert_chunk(
        _conn: object,
        rows: list[_ParsedRow],
        *,
        base_id: int,  # noqa: ARG001
        source_version_id: int,  # noqa: ARG001
        retrieval_date: datetime,  # noqa: ARG001
    ) -> int:
        seen.append(len(rows))
        return len(rows)

    monkeypatch.setattr(gwas_loader, "_insert_chunk", _stub_insert_chunk)

    full_chunks = 2
    expected_total = full_chunks * _CHUNK_SIZE + 5
    total = _stream_bulk_insert(
        conn=None,  # type: ignore[arg-type]
        rows_iter=_gen_dummy_rows(expected_total),
        source_version_id=1,
        retrieval_date=datetime(2026, 5, 17, tzinfo=UTC),
    )
    assert total == expected_total
    expected_tail = 5
    assert seen == [_CHUNK_SIZE] * full_chunks + [expected_tail]


# ---------------------------------------------------------------------------
# _resolve_version_via_stats (stats-endpoint variants).
# ---------------------------------------------------------------------------


def _enable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='true' WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _patched_external_client_with_handler(
    monkeypatch: pytest.MonkeyPatch,
    handler,  # type: ignore[no-untyped-def]
) -> None:
    """Force ``_resolve_version_via_stats`` to use a MockTransport-backed httpx."""
    real_client_cls = httpx.Client

    def _factory(
        *_args: object,
        timeout: float = 30.0,  # noqa: ARG001
        follow_redirects: bool = False,  # noqa: ARG001
        **_kwargs: object,
    ) -> httpx.Client:
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(gwas_loader.httpx, "Client", _factory)


def test_resolve_version_extracts_date_from_stats_payload() -> None:
    """The live shape: ``{"date": "YYYY-MM-DD", ...}`` → ``YYYY_MM_DD``."""
    init_databases()
    _enable_external_calls()

    def handler(request: httpx.Request) -> httpx.Response:
        # The stats endpoint is what the loader targets; the URL should
        # be exactly the constant the loader exposes.
        assert str(request.url) == gwas_loader.GWAS_STATS_URL
        return httpx.Response(
            200,
            json={
                "date": "2026-04-27",
                "associations": "1099366",
                "ensemblbuild": "115",
                "dbsnpbuild": "156",
            },
        )

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        version = _resolve_version_via_stats()
    finally:
        monkeypatch.undo()
    assert version == "2026_04_27"


def test_resolve_version_accepts_releasedate_alias() -> None:
    """Defensive: a payload that uses ``releasedate`` still resolves."""
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"releasedate": "2025-08-12"})

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        version = _resolve_version_via_stats()
    finally:
        monkeypatch.undo()
    assert version == "2025_08_12"


def test_resolve_version_raises_on_malformed_payload() -> None:
    """Missing date field raises before any download begins."""
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ensemblbuild": "115"})

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        with pytest.raises(ValueError, match="missing a 'date'"):
            _resolve_version_via_stats()
    finally:
        monkeypatch.undo()


def test_resolve_version_raises_on_non_json_body() -> None:
    """A non-JSON 200 response raises a clear error."""
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>EBI gateway error</html>")

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        with pytest.raises(ValueError, match="not valid JSON"):
            _resolve_version_via_stats()
    finally:
        monkeypatch.undo()


def test_resolve_version_propagates_http_5xx() -> None:
    """A 5xx from the stats endpoint surfaces as ExternalCallError."""
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        with pytest.raises(ExternalCallError, match="HTTP 503"):
            _resolve_version_via_stats()
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# Integration: full transaction against the 50-row fixture.
# ---------------------------------------------------------------------------


def test_refresh_full_transaction_inserts_expected_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture → 1 source_version row, 51 active rows (50 source - 2 dropped + 3 expanded)."""
    init_databases()
    _patch_download_to_cache(monkeypatch, _FIXTURE_PATH)
    _patch_resolve_version(monkeypatch, "2026_05_12")

    result = gwas_loader.refresh(force=False)

    assert result.source_db == "gwas_catalog"
    assert result.version == "2026_05_12"
    assert result.record_count == _EXPECTED_EMITTED
    assert result.was_already_current is False

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM gwas_catalog_associations g "
            "JOIN annotation_sources s "
            "ON s.source = 'gwas_catalog' AND s.current_source_version_id = g.source_version_id",
        ).fetchone()
        non_current = conn.execute(
            "SELECT COUNT(*) FROM gwas_catalog_associations g "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM annotation_sources s "
            "  WHERE s.source = 'gwas_catalog' "
            "    AND s.current_source_version_id = g.source_version_id"
            ")",
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version, record_count, is_current FROM annotation_source_versions"
            " WHERE source_db = 'gwas_catalog'",
        ).fetchall()
    assert active is not None
    assert active[0] == _EXPECTED_EMITTED
    assert non_current is not None
    assert non_current[0] == 0
    assert version_rows == [("2026_05_12", _EXPECTED_EMITTED, True)]


def test_refresh_writes_expected_column_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify one specific fixture row lands in the right DB columns end-to-end."""
    init_databases()
    _patch_download_to_cache(monkeypatch, _FIXTURE_PATH)
    _patch_resolve_version(monkeypatch, "2026_05_12")

    gwas_loader.refresh(force=False)

    with duckdb_connection() as conn:
        row = conn.execute(
            """
            SELECT study_accession, pmid, rsid, chrom, pos_grch38,
                   trait_id, trait_name, mapped_trait_uri,
                   effect_size, effect_allele, effect_allele_freq,
                   ci_95_lower, ci_95_upper, p_value,
                   sample_size_initial, sample_size_replication,
                   is_replicated
              FROM gwas_catalog_associations
             WHERE study_accession = 'GCST001234'
            """,
        ).fetchone()
    assert row is not None
    (
        study,
        pmid,
        rsid,
        chrom,
        pos,
        trait_id,
        trait_name,
        mapped_uri,
        effect,
        allele,
        freq,
        ci_lower,
        ci_upper,
        p_value,
        ss_initial,
        ss_repl,
        is_repl,
    ) = row
    expected_initial = 4512
    expected_replication = 2000
    assert study == "GCST001234"
    assert pmid == "12345678"
    assert rsid == "rs397704705"
    assert str(chrom) == "7"
    assert pos == 4781213
    assert trait_id == "EFO_0004340"
    assert trait_name == "body mass index"
    assert mapped_uri == "http://www.ebi.ac.uk/efo/EFO_0004340"
    assert effect == pytest.approx(1.34)
    assert allele == "A"
    assert freq == pytest.approx(0.42)
    assert ci_lower == pytest.approx(1.23)
    assert ci_upper == pytest.approx(1.56)
    assert p_value == pytest.approx(2.0e-9)
    assert ss_initial == expected_initial
    assert ss_repl == expected_replication
    assert is_repl is True


# ---------------------------------------------------------------------------
# Integration: supersession (round-trip same-version --force).
# ---------------------------------------------------------------------------


def test_refresh_supersedes_prior_rows_same_version_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-version ``--force`` re-run.

    Per the prompt's round-trip spec: load fixture twice (second run
    forced at the same version). Under the version-pointer pattern
    both refreshes' rows land under the same ``source_version_id`` and
    both are "current" because the pointer matches. Dedup at read time
    is a downstream concern, not a supersession-correctness issue.

    * ``source_version_id`` is unchanged (idempotent upsert on
      ``(source_db, version)``).
    * ``annotation_sources`` pointer still names that id.
    * Active (= current-version) count == 2 x fixture emit count.
    * Total row count == 2 x fixture emit count.
    """
    init_databases()
    _patch_download_to_cache(monkeypatch, _FIXTURE_PATH)
    _patch_resolve_version(monkeypatch, "2026_05_12")

    first = gwas_loader.refresh(force=False)
    second = gwas_loader.refresh(force=True)

    assert first.was_already_current is False
    assert second.was_already_current is False
    # Same version label → same source_version_id (idempotent upsert).
    assert second.source_version_id == first.source_version_id

    with duckdb_connection() as conn:
        current_pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources "
            "WHERE source = 'gwas_catalog'",
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM gwas_catalog_associations g "
            "JOIN annotation_sources s "
            "ON s.source = 'gwas_catalog' AND s.current_source_version_id = g.source_version_id",
        ).fetchone()
        total = conn.execute(
            "SELECT COUNT(*) FROM gwas_catalog_associations",
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version, is_current FROM annotation_source_versions"
            " WHERE source_db = 'gwas_catalog'",
        ).fetchall()
    assert current_pointer is not None
    assert int(current_pointer[0]) == first.source_version_id
    assert active is not None
    assert active[0] == 2 * _EXPECTED_EMITTED
    assert total is not None
    assert total[0] == 2 * _EXPECTED_EMITTED
    # One version row -- same-version refresh did not insert a new
    # ``annotation_source_versions`` entry.
    assert version_rows == [("2026_05_12", True)]


def test_refresh_supersedes_prior_rows_on_new_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two refreshes under different version labels.

    Asserts: 2 source_version rows, the ``annotation_sources`` pointer
    moved from v1 to v2, and both row sets coexist in the table under
    their respective ``source_version_id`` values.
    """
    init_databases()
    _patch_download_to_cache(monkeypatch, _FIXTURE_PATH)

    _patch_resolve_version(monkeypatch, "2026_05_10")
    first = gwas_loader.refresh(force=False)
    _patch_resolve_version(monkeypatch, "2026_05_17")
    second = gwas_loader.refresh(force=False)

    assert first.version == "2026_05_10"
    assert second.version == "2026_05_17"
    assert second.source_version_id > first.source_version_id

    with duckdb_connection() as conn:
        current_pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources "
            "WHERE source = 'gwas_catalog'",
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM gwas_catalog_associations WHERE source_version_id = ?",
            [second.source_version_id],
        ).fetchone()
        prior_rows = conn.execute(
            "SELECT COUNT(*) FROM gwas_catalog_associations WHERE source_version_id = ?",
            [first.source_version_id],
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version, is_current FROM annotation_source_versions"
            " WHERE source_db = 'gwas_catalog' ORDER BY source_version_id",
        ).fetchall()
    assert current_pointer is not None
    assert int(current_pointer[0]) == second.source_version_id
    assert active is not None
    assert active[0] == _EXPECTED_EMITTED
    assert prior_rows is not None
    assert prior_rows[0] == _EXPECTED_EMITTED
    assert version_rows == [
        ("2026_05_10", False),
        ("2026_05_17", True),
    ]


def test_refresh_idempotent_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same version on second call (no force) → was_already_current=True."""
    init_databases()
    _patch_download_to_cache(monkeypatch, _FIXTURE_PATH)
    _patch_resolve_version(monkeypatch, "2026_05_12")

    first = gwas_loader.refresh(force=False)
    second = gwas_loader.refresh(force=False)

    assert first.was_already_current is False
    assert second.was_already_current is True
    assert second.source_version_id == first.source_version_id
    assert second.record_count == _EXPECTED_EMITTED

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM gwas_catalog_associations g "
            "JOIN annotation_sources s "
            "ON s.source = 'gwas_catalog' AND s.current_source_version_id = g.source_version_id",
        ).fetchone()
        n_versions = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'gwas_catalog'",
        ).fetchone()
    assert active is not None
    assert active[0] == _EXPECTED_EMITTED
    assert n_versions is not None
    assert n_versions[0] == 1


def test_refresh_transaction_rolls_back_on_bulk_insert_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A streaming-insert failure rolls back every chunk + the deactivation."""
    init_databases()
    _patch_download_to_cache(monkeypatch, _FIXTURE_PATH)
    _patch_resolve_version(monkeypatch, "2026_05_12")

    boom = RuntimeError("simulated insert failure")

    def _explode(*_args: object, **_kwargs: object) -> int:
        raise boom

    monkeypatch.setattr(gwas_loader, "_stream_bulk_insert", _explode)

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        gwas_loader.refresh(force=False)

    with duckdb_connection() as conn:
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'gwas_catalog'",
        ).fetchone()
        annotation_rows = conn.execute(
            "SELECT COUNT(*) FROM gwas_catalog_associations",
        ).fetchone()
    assert version_rows is not None
    assert version_rows[0] == 0
    assert annotation_rows is not None
    assert annotation_rows[0] == 0


# ---------------------------------------------------------------------------
# Integration: external-calls-disabled refusal.
# ---------------------------------------------------------------------------


def test_refresh_blocked_when_external_calls_disabled() -> None:
    """A disabled master switch raises and leaves an intent + blocked pair.

    The loader's first audited call is the GET against the stats
    endpoint that resolves version. With external_calls_enabled=false
    (the ``init_databases`` seed default), the disabled check raises
    :class:`ExternalCallsDisabledError` before the body of the stats
    request is sent.
    """
    init_databases()
    # init_databases seeds external_calls_enabled=false; do not flip.

    with pytest.raises(ExternalCallsDisabledError, match="genome config set"):
        gwas_loader.refresh(force=False)

    rows = _audit_rows()
    expected_pair = 2
    assert len(rows) == expected_pair, rows
    intent, blocked = rows
    intent_details = json.loads(str(intent[3]))
    blocked_details = json.loads(str(blocked[3]))
    assert intent_details["phase"] == "intent"
    assert intent_details["method"] == "GET"
    assert blocked_details["status"] == "blocked"
    assert blocked_details["method"] == "GET"
    assert intent[1] == blocked[1] == "annotation_source"
    assert intent[2] == blocked[2] == "gwas_catalog_release_stats"
    assert intent[5] == blocked[5] == "annotations_gwas_catalog"


# ---------------------------------------------------------------------------
# Registry / module-import side effects.
# ---------------------------------------------------------------------------


def test_get_loader_returns_gwas_refresh() -> None:
    assert get_loader("gwas_catalog") is gwas_loader.refresh


def test_source_db_label() -> None:
    assert gwas_loader.SOURCE_DB == "gwas_catalog"


def test_chunk_size_locked_at_250k() -> None:
    """Runbook documents 250K; pin it so a casual flip is loud."""
    expected_chunk_size = 250_000
    assert expected_chunk_size == _CHUNK_SIZE


def test_url_verified_date_is_iso_format() -> None:
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", gwas_loader.URL_VERIFIED_DATE)


def test_gwas_stats_url_matches_canonical_ebi_path() -> None:
    assert gwas_loader.GWAS_STATS_URL == ("https://www.ebi.ac.uk/gwas/api/search/stats")


def test_gwas_associations_zip_url_matches_canonical_ebi_ftp() -> None:
    """The ``latest/`` symlink avoids the stats-date vs publish-date offset."""
    assert gwas_loader.GWAS_ASSOCIATIONS_ZIP_URL == (
        "https://ftp.ebi.ac.uk/pub/databases/gwas/releases/latest/"
        "gwas-catalog-associations_ontology-annotated-full.zip"
    )


# ---------------------------------------------------------------------------
# CLI integration.
# ---------------------------------------------------------------------------


def test_cli_refresh_gwas_catalog_runs_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, _FIXTURE_PATH)
    _patch_resolve_version(monkeypatch, "2026_05_12")

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "refresh", "--source", "gwas_catalog"])
    assert result.exit_code == 0, result.output
    assert "source_db=gwas_catalog" in result.output
    assert "version=2026_05_12" in result.output
    assert f"records={_EXPECTED_EMITTED}" in result.output
    assert "already_current=False" in result.output


def test_cli_status_after_refresh_reports_gwas_catalog_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, _FIXTURE_PATH)
    _patch_resolve_version(monkeypatch, "2026_05_12")
    gwas_loader.refresh(force=False)

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "status"])
    assert result.exit_code == 0, result.output
    matching = [line for line in result.output.splitlines() if line.startswith("gwas_catalog:")]
    assert len(matching) == 1
    line = matching[0]
    assert "2026_05_12" in line
    assert f"{_EXPECTED_EMITTED} records" in line


# ---------------------------------------------------------------------------
# _insert_chunk smoke (real DB; verifies the SELECT cast lands cleanly).
# ---------------------------------------------------------------------------


def test_insert_chunk_handles_null_columns() -> None:
    """Direct call exercises the chrom enum cast and NULL columns."""
    init_databases()
    rows = [
        _ParsedRow(
            study_accession=None,
            pmid=None,
            rsid="rs1",
            chrom="X",
            pos_grch38=None,
            trait_id=None,
            trait_name=None,
            mapped_trait_uri=None,
            effect_size=None,
            effect_size_unit=None,
            effect_allele=None,
            other_allele=None,
            effect_allele_freq=None,
            ci_95_lower=None,
            ci_95_upper=None,
            p_value=None,
            sample_size_initial=None,
            sample_size_replication=None,
            ancestry=None,
            is_replicated=None,
        ),
    ]
    with duckdb_connection() as conn:
        from genome.annotate.source_versions import (  # noqa: PLC0415
            upsert_source_version,
        )

        sv_id = upsert_source_version(
            conn,
            source_db="gwas_catalog",
            version="2026_05_12",
            source_url="https://example.invalid/x",
            source_file_hash="abc",
            source_file_size=1,
            record_count=None,
        )
        n = _insert_chunk(
            conn,
            rows,
            base_id=1,
            source_version_id=sv_id,
            retrieval_date=datetime(2026, 5, 17, tzinfo=UTC),
        )
    assert n == 1
    with duckdb_connection() as conn:
        row = conn.execute(
            "SELECT rsid, chrom, pos_grch38 FROM gwas_catalog_associations WHERE rsid = 'rs1'",
        ).fetchone()
    assert row is not None
    rsid, chrom, pos = row
    assert rsid == "rs1"
    assert str(chrom) == "X"
    assert pos is None


# ---------------------------------------------------------------------------
# Benchmark guard: 100K-row synthetic fixture under 30 s.
# ---------------------------------------------------------------------------


_BENCHMARK_ROW_COUNT: int = 100_000
_BENCHMARK_CEILING_S: float = 30.0


def _build_synthetic_tsv(n: int) -> str:
    """Build an N-row GWAS Catalog TSV in memory for the benchmark.

    Mirrors the fixture-file shape (same 38 columns), all rows
    happy-path single-SNP so the parser doesn't drop any. Different
    rsIDs per row so the DB writes don't hit any uniqueness constraint
    issues (the schema's primary key is the app-allocated
    ``association_id``; ``rsid`` carries no UNIQUE constraint).
    """
    header_cells = [
        "DATE ADDED TO CATALOG",
        "PUBMEDID",
        "FIRST AUTHOR",
        "DATE",
        "JOURNAL",
        "LINK",
        "STUDY",
        "DISEASE/TRAIT",
        "INITIAL SAMPLE SIZE",
        "REPLICATION SAMPLE SIZE",
        "REGION",
        "CHR_ID",
        "CHR_POS",
        "REPORTED GENE(S)",
        "MAPPED_GENE",
        "UPSTREAM_GENE_ID",
        "DOWNSTREAM_GENE_ID",
        "SNP_GENE_IDS",
        "UPSTREAM_GENE_DISTANCE",
        "DOWNSTREAM_GENE_DISTANCE",
        "STRONGEST SNP-RISK ALLELE",
        "SNPS",
        "MERGED",
        "SNP_ID_CURRENT",
        "CONTEXT",
        "INTERGENIC",
        "RISK ALLELE FREQUENCY",
        "P-VALUE",
        "PVALUE_MLOG",
        "P-VALUE (TEXT)",
        "OR or BETA",
        "95% CI (TEXT)",
        "PLATFORM [SNPS PASSING QC]",
        "CNV",
        "MAPPED_TRAIT",
        "MAPPED_TRAIT_URI",
        "STUDY ACCESSION",
        "GENOTYPING TECHNOLOGY",
    ]
    lines = ["\t".join(header_cells)]
    for i in range(n):
        rsid_int = 1000 + i
        chrom = str((i % 22) + 1)
        cells = [
            "2024-12-01",
            f"{20000000 + i}",
            "Smith J",
            "2024-11-15",
            "Nature Genetics",
            "https://example.invalid/x",
            "Example GWAS study",
            "Body mass index",
            "4,512 European ancestry individuals",
            "2,000 European ancestry individuals",
            f"{chrom}q21.1",
            chrom,
            str(500000 + i),
            "FTO",
            "FTO",
            "",
            "",
            "ENSG00000140718",
            "",
            "",
            f"rs{rsid_int}-A",
            f"rs{rsid_int}",
            "0",
            str(rsid_int),
            "intron_variant",
            "0",
            "0.42",
            "2.0E-9",
            "8.7",
            "",
            "1.34",
            "[1.23-1.56]",
            "Illumina [600000]",
            "N",
            "body mass index",
            "http://www.ebi.ac.uk/efo/EFO_0004340",
            f"GCST0{30000 + i:05d}",
            "Genome-wide genotyping array",
        ]
        lines.append("\t".join(cells))
    return "\n".join(lines) + "\n"


def test_benchmark_parse_and_stage_100k_rows_under_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """100K-row synthetic fixture parses and stages in under 30 s.

    The locked < 30 s ceiling is the project-wide "routine refresh"
    target documented in ``CLAUDE.md``. Parse + chunked insert at
    100K rows extrapolates to the real-release 600-700K row corpus
    landing well inside the 5-minute first-load target the runbook
    sets for GWAS Catalog refreshes.
    """
    init_databases()
    fixture_path = tmp_path / "gwas_catalog_synthetic_100k.tsv"
    fixture_path.write_text(
        _build_synthetic_tsv(_BENCHMARK_ROW_COUNT),
        encoding="utf-8",
    )
    _patch_download_to_cache(monkeypatch, fixture_path)
    _patch_resolve_version(monkeypatch, "2026_05_12")

    started = time.monotonic()
    result = gwas_loader.refresh(force=False)
    elapsed = time.monotonic() - started

    assert result.record_count == _BENCHMARK_ROW_COUNT
    assert elapsed < _BENCHMARK_CEILING_S, (
        f"100K-row parse+stage took {elapsed:.2f}s "
        f"(ceiling {_BENCHMARK_CEILING_S}s); investigate before merging"
    )
