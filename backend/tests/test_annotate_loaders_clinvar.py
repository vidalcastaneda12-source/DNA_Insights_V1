"""Tests for :mod:`genome.annotate.loaders.clinvar`.

Covers the per-field coercions (rsID -1 → NULL, empty / dash / na →
NULL, phenotype list / IDs split, HGVS c./p. extraction, Mon DD, YYYY
date parse, review_status → star_rating mapping), the chunk-boundary
contract (1,000,001 rows → 5 chunks of 250K + 250K + 250K + 250K + 1),
the version-resolution path (Last-Modified header → YYYY_MM_DD,
fallback paths), the end-to-end ``refresh`` flow with
``download_to_cache`` + ``_resolve_version_via_head`` monkey-patched
to return a programmatically-built gzipped TSV, the supersession
transaction (new version vs same-version --force vs modified-row
diff), the audited refusal path with ``external_calls_enabled=false``,
and the CLI smoke against ``genome annotate refresh --source clinvar``.
"""

from __future__ import annotations

import gzip
import io
import json
import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from genome.annotate import downloads as annotate_downloads
from genome.annotate.loaders import clinvar as clinvar_loader
from genome.annotate.loaders.clinvar import (
    _CHUNK_SIZE,
    _clean_rsid,
    _empty_to_none,
    _extract_hgvs_c_p,
    _insert_chunk,
    _iter_chunks,
    _parse_clinvar_date,
    _parse_phenotype_ids,
    _parse_phenotype_list,
    _parse_submitter_categories,
    _parse_variant_summary,
    _ParsedRow,
    _resolve_version_via_head,
    _review_status_to_star,
    _stream_bulk_insert,
)
from genome.annotate.registry import get_loader
from genome.cli import app
from genome.db import duckdb_connection, init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.privacy.external_client import ExternalCallsDisabledError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Header + fixture rows. The ClinVar TSV has 44+ columns; we only need
# the 16 the loader reads (per ``_REQUIRED_HEADERS``) plus a handful of
# "skipped" columns to exercise the dictionary lookup robustness.
# ---------------------------------------------------------------------------

_FIXTURE_HEADER = (
    "#AlleleID\t"  # 1 — present but unused by the loader
    "Type\t"  # 2
    "Name\t"  # 3
    "GeneID\t"  # 4
    "GeneSymbol\t"  # 5
    "HGNC_ID\t"  # 6
    "ClinicalSignificance\t"  # 7
    "ClinSigSimple\t"  # 8
    "LastEvaluated\t"  # 9
    "RS# (dbSNP)\t"  # 10
    "nsv/esv (dbVar)\t"  # 11
    "RCVaccession\t"  # 12
    "PhenotypeIDS\t"  # 13
    "PhenotypeList\t"  # 14
    "Origin\t"  # 15
    "OriginSimple\t"  # 16
    "Assembly\t"  # 17
    "ChromosomeAccession\t"  # 18
    "Chromosome\t"  # 19
    "Start\t"  # 20
    "Stop\t"  # 21
    "ReferenceAllele\t"  # 22
    "AlternateAllele\t"  # 23
    "Cytogenetic\t"  # 24
    "ReviewStatus\t"  # 25
    "NumberSubmitters\t"  # 26
    "Guidelines\t"  # 27
    "TestedInGTR\t"  # 28
    "OtherIDs\t"  # 29
    "SubmitterCategories\t"  # 30
    "VariationID\t"  # 31
    "PositionVCF\t"  # 32
    "ReferenceAlleleVCF\t"  # 33
    "AlternateAlleleVCF\t"  # 34
    "SomaticClinicalImpact\t"  # 35
    "SomaticClinicalImpactLastEvaluated\t"  # 36
    "ReviewStatusClinicalImpact\t"  # 37
    "Oncogenicity\t"  # 38
    "OncogenicityLastEvaluated\t"  # 39
    "ReviewStatusOncogenicity\t"  # 40
    "SCVsForAggregateGermlineClassification\t"  # 41
    "SCVsForAggregateSomaticClinicalImpact\t"  # 42
    "SCVsForAggregateOncogenicityClassification"  # 43
)


def _row(  # noqa: PLR0913 — every kwarg corresponds to one ClinVar TSV column
    *,
    allele_id: str = "15041",
    name: str = "NM_014855.3(AP5Z1):c.80A>T (p.Lys27Ter)",
    gene_symbol: str = "AP5Z1",
    clin_sig: str = "Pathogenic",
    last_eval: str = "Dec 17, 2024",
    rs: str = "397704705",
    rcv: str = "RCV000000012",
    phenotype_ids: str = "MONDO:MONDO:0013342,MedGen:C3150901,OMIM:613647||MedGen:C3661900",
    phenotype_list: str = (
        "Hereditary spastic paraplegia 48|Macular dystrophy with or without extraocular features"
    ),
    assembly: str = "GRCh38",
    chrom: str = "7",
    review_status: str = "criteria provided, multiple submitters, no conflicts",
    number_submitters: str = "4",
    other_ids: str = "ClinGen:CA215070,OMIM:613653.0001",
    submitter_categories: str = "3",
    variation_id: str = "14",
    position_vcf: str = "4781213",
    ref_allele_vcf: str = "G",
    alt_allele_vcf: str = "T",
) -> str:
    cells = (
        allele_id,
        "single nucleotide variant",
        name,
        "9907",
        gene_symbol,
        "HGNC:22197",
        clin_sig,
        "1",
        last_eval,
        rs,
        "-",
        rcv,
        phenotype_ids,
        phenotype_list,
        "germline;unknown",
        "germline",
        assembly,
        "NC_000007.14",
        chrom,
        "4781213",
        "4781213",
        "G",
        "T",
        "7p22.1",
        review_status,
        number_submitters,
        "-",
        "N",
        other_ids,
        submitter_categories,
        variation_id,
        position_vcf,
        ref_allele_vcf,
        alt_allele_vcf,
        "-",  # SomaticClinicalImpact
        "-",  # SomaticClinicalImpactLastEvaluated
        "-",  # ReviewStatusClinicalImpact
        "-",  # Oncogenicity
        "-",  # OncogenicityLastEvaluated
        "-",  # ReviewStatusOncogenicity
        "SCV001451119",  # SCVsForAggregateGermlineClassification
        "-",  # SCVsForAggregateSomaticClinicalImpact
        "-",  # SCVsForAggregateOncogenicityClassification
    )
    return "\t".join(cells)


# ---------------------------------------------------------------------------
# A 50-row fixture set covering: GRCh37/GRCh38 mix, semicolon-list
# fields, empty strings vs `-` vs `na`, RS = -1, every clinical-
# significance bucket the schema mentions, every review-status star
# rating, and one variant that appears on both assemblies.
# ---------------------------------------------------------------------------


def _build_50_row_fixture() -> tuple[list[dict[str, object]], str]:
    """Build a 50-row fixture. Returns (expected_records, tsv_text)."""
    rows: list[str] = []
    expected: list[dict[str, object]] = []

    # Variant 14: appears on BOTH GRCh37 and GRCh38 (the documented
    # cross-assembly case).
    rows.append(
        _row(
            variation_id="14",
            assembly="GRCh37",
            position_vcf="4820844",
            chrom="7",
            rs="397704705",
            clin_sig="Pathogenic",
            review_status="criteria provided, multiple submitters, no conflicts",
        ),
    )
    expected.append(
        {
            "variation_id": "14",
            "rsid": "rs397704705",
            "chrom": "7",
            "pos_grch38": None,  # GRCh37 row → NULL position
            "ref_allele": None,
            "alt_allele": None,
            "clinical_significance": "Pathogenic",
            "star_rating": 2,
        },
    )
    rows.append(
        _row(
            variation_id="14",
            assembly="GRCh38",
            position_vcf="4781213",
            chrom="7",
            rs="397704705",
            clin_sig="Pathogenic",
            review_status="criteria provided, multiple submitters, no conflicts",
        ),
    )
    expected.append(
        {
            "variation_id": "14",
            "rsid": "rs397704705",
            "chrom": "7",
            "pos_grch38": 4781213,
            "ref_allele": "G",
            "alt_allele": "T",
            "clinical_significance": "Pathogenic",
            "star_rating": 2,
        },
    )

    # Two more "both assembly" pairs to test bulk semantics.
    for v_id, sig, status, star in [
        ("21", "Likely pathogenic", "criteria provided, single submitter", 1),
        ("31", "Benign", "reviewed by expert panel", 3),
    ]:
        rows.append(
            _row(
                variation_id=v_id,
                assembly="GRCh37",
                position_vcf="100000",
                rs="123",
                clin_sig=sig,
                review_status=status,
            ),
        )
        expected.append(
            {
                "variation_id": v_id,
                "rsid": "rs123",
                "pos_grch38": None,
                "clinical_significance": sig,
                "star_rating": star,
            },
        )
        rows.append(
            _row(
                variation_id=v_id,
                assembly="GRCh38",
                position_vcf="200000",
                rs="123",
                clin_sig=sig,
                review_status=status,
            ),
        )
        expected.append(
            {
                "variation_id": v_id,
                "rsid": "rs123",
                "pos_grch38": 200000,
                "clinical_significance": sig,
                "star_rating": star,
            },
        )

    # GRCh38-only single rows -- 44 more to reach 50 total.
    significance_rotation = [
        "Pathogenic",
        "Likely pathogenic",
        "Uncertain significance",
        "Likely benign",
        "Benign",
        "Conflicting interpretations of pathogenicity",
        "drug response",
    ]
    review_rotation = [
        ("practice guideline", 4),
        ("reviewed by expert panel", 3),
        ("criteria provided, multiple submitters, no conflicts", 2),
        ("criteria provided, single submitter", 1),
        ("criteria provided, conflicting classifications", 1),
        ("no assertion criteria provided", 0),
        ("no classification provided", 0),
    ]
    rs_options = ["-1", "12345", "99999", "67890", ""]

    needed = 50 - len(rows)
    for i in range(needed):
        v_id = str(1000 + i)
        sig = significance_rotation[i % len(significance_rotation)]
        status, star = review_rotation[i % len(review_rotation)]
        rs = rs_options[i % len(rs_options)]
        # Vary chrom across the canonical set; row 5 sneaks in `M` to
        # exercise the alias remap in normalize_chrom.
        chrom_value = "M" if i == 5 else str((i % 22) + 1)
        rows.append(
            _row(
                variation_id=v_id,
                assembly="GRCh38",
                position_vcf=str(500_000 + i),
                rs=rs,
                clin_sig=sig,
                review_status=status,
                chrom=chrom_value,
                phenotype_list="X|Y" if i % 4 == 0 else "Z",
                phenotype_ids="MONDO:1,OMIM:2||MedGen:3" if i % 4 == 0 else "OMIM:9",
                last_eval="Jan 1, 2025" if i % 3 == 0 else "-",
                ref_allele_vcf="A" if i % 5 != 4 else "-",
                alt_allele_vcf="C" if i % 5 != 4 else "na",
            ),
        )
        expected_rsid = None if rs in {"-1", ""} else f"rs{rs}"
        expected.append(
            {
                "variation_id": v_id,
                "rsid": expected_rsid,
                "chrom": "MT" if i == 5 else str((i % 22) + 1),
                "pos_grch38": 500_000 + i,
                "ref_allele": None if i % 5 == 4 else "A",
                "alt_allele": None if i % 5 == 4 else "C",
                "clinical_significance": sig,
                "review_status": status,
                "star_rating": star,
            },
        )

    assert len(rows) == 50, f"fixture should be 50 rows, got {len(rows)}"
    tsv_text = _FIXTURE_HEADER + "\n" + "\n".join(rows) + "\n"
    return expected, tsv_text


# ---------------------------------------------------------------------------
# Helper: write a gzipped TSV from row strings.
# ---------------------------------------------------------------------------


def _write_gz(path: Path, tsv_text: str) -> None:
    with gzip.open(path, mode="wt", encoding="utf-8") as gz:
        gz.write(tsv_text)


@pytest.fixture(autouse=True)
def _ensure_clinvar_registered() -> Iterator[None]:
    """Re-register the loader at test start.

    Other annotate test files install autouse fixtures that wipe the
    registry via ``_clear_loaders_for_testing()`` to keep their cases
    hermetic. When their tests run before ours in a full-suite
    invocation, the side-effect registration from
    ``genome.annotate.loaders.clinvar`` is gone. Re-registering here
    makes our tests order-independent. We pop again on teardown so we
    don't leak the registration into the next test file.
    """
    from genome.annotate.registry import _LOADERS, register_loader  # noqa: PLC0415

    _LOADERS.pop("clinvar", None)
    register_loader("clinvar", clinvar_loader.refresh)
    try:
        yield
    finally:
        _LOADERS.pop("clinvar", None)


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


def _patch_download_to_cache(
    monkeypatch: pytest.MonkeyPatch,
    gz_path: Path,
) -> dict[str, int]:
    """Replace ``download_to_cache`` with a stub that returns ``gz_path``.

    Records call count and (optionally) the most recently-seen ``force``
    value so tests can assert "force flag passed through" semantics.
    """
    import hashlib  # noqa: PLC0415

    counter: dict[str, int] = {"calls": 0}
    digest = hashlib.sha256(gz_path.read_bytes()).hexdigest()
    size = gz_path.stat().st_size

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
            path=gz_path,
            sha256=digest,
            size_bytes=size,
        )

    monkeypatch.setattr(clinvar_loader, "download_to_cache", _stub)
    return counter


def _patch_resolve_version(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> dict[str, int]:
    """Replace ``_resolve_version_via_head`` with a stub returning ``version``."""
    counter: dict[str, int] = {"calls": 0}

    def _stub() -> str:
        counter["calls"] += 1
        return version

    monkeypatch.setattr(clinvar_loader, "_resolve_version_via_head", _stub)
    return counter


def _audit_rows() -> list[tuple[object, ...]]:
    with sqlcipher_connection() as conn:
        return conn.execute(
            "SELECT action_type, resource_type, resource_id, operation_details,"
            " external_call, external_endpoint, external_payload_hash"
            " FROM audit_log ORDER BY log_id",
        ).fetchall()


# ---------------------------------------------------------------------------
# _empty_to_none / _clean_rsid coercions
# ---------------------------------------------------------------------------


def test_empty_to_none_handles_dash_and_na() -> None:
    assert _empty_to_none("") is None
    assert _empty_to_none(" ") is None
    assert _empty_to_none("-") is None
    assert _empty_to_none("na") is None
    assert _empty_to_none("Pathogenic") == "Pathogenic"
    assert _empty_to_none("  Likely benign ") == "Likely benign"


def test_clean_rsid_minus_one_returns_none() -> None:
    """ClinVar's missing-rsID sentinel is ``-1`` (integer), not empty."""
    assert _clean_rsid("-1") is None


def test_clean_rsid_empty_returns_none() -> None:
    assert _clean_rsid("") is None
    assert _clean_rsid("   ") is None
    assert _clean_rsid("-") is None


def test_clean_rsid_digits_get_rs_prefix() -> None:
    assert _clean_rsid("397704705") == "rs397704705"
    assert _clean_rsid("12345") == "rs12345"


def test_clean_rsid_non_digit_returns_none() -> None:
    """A defensive guard against future ClinVar shape changes."""
    assert _clean_rsid("abc") is None
    assert _clean_rsid("rs123") is None  # already-prefixed input is rejected


# ---------------------------------------------------------------------------
# _parse_clinvar_date
# ---------------------------------------------------------------------------


def test_parse_clinvar_date_short_month() -> None:
    assert _parse_clinvar_date("Dec 17, 2024") == date(2024, 12, 17)
    assert _parse_clinvar_date("Jan 1, 2025") == date(2025, 1, 1)


def test_parse_clinvar_date_empty_or_dash_returns_none() -> None:
    assert _parse_clinvar_date("") is None
    assert _parse_clinvar_date("-") is None


def test_parse_clinvar_date_unparseable_returns_none() -> None:
    """Loud-but-not-fatal: a mangled date is one row, not an abort."""
    assert _parse_clinvar_date("2024-12-17") is None
    assert _parse_clinvar_date("garbage") is None


# ---------------------------------------------------------------------------
# _parse_phenotype_list / _parse_phenotype_ids
# ---------------------------------------------------------------------------


def test_parse_phenotype_list_pipe_separated() -> None:
    assert _parse_phenotype_list("X|Y|Z") == ["X", "Y", "Z"]


def test_parse_phenotype_list_empty_or_dash_returns_none() -> None:
    assert _parse_phenotype_list("") is None
    assert _parse_phenotype_list("-") is None


def test_parse_phenotype_list_strips_whitespace_and_drops_empty() -> None:
    assert _parse_phenotype_list(" X | |  Y ") == ["X", "Y"]


def test_parse_phenotype_ids_two_level_flatten() -> None:
    """``||`` between phenotypes; ``,`` within one phenotype's IDs."""
    raw = "MONDO:1,OMIM:2||MedGen:3"
    assert _parse_phenotype_ids(raw) == ["MONDO:1", "OMIM:2", "MedGen:3"]


def test_parse_phenotype_ids_single_phenotype() -> None:
    assert _parse_phenotype_ids("OMIM:9") == ["OMIM:9"]


def test_parse_phenotype_ids_empty_or_dash_returns_none() -> None:
    assert _parse_phenotype_ids("") is None
    assert _parse_phenotype_ids("-") is None


# ---------------------------------------------------------------------------
# _parse_submitter_categories
# ---------------------------------------------------------------------------


def test_parse_submitter_categories_wraps_integer() -> None:
    assert _parse_submitter_categories("3") == ["3"]
    assert _parse_submitter_categories("1") == ["1"]


def test_parse_submitter_categories_empty_or_dash_returns_none() -> None:
    assert _parse_submitter_categories("") is None
    assert _parse_submitter_categories("-") is None


# ---------------------------------------------------------------------------
# _extract_hgvs_c_p
# ---------------------------------------------------------------------------


def test_extract_hgvs_c_p_with_protein_block() -> None:
    raw = "NM_014855.3(AP5Z1):c.80A>T (p.Lys27Ter)"
    assert _extract_hgvs_c_p(raw) == ("NM_014855.3(AP5Z1):c.80A>T", "p.Lys27Ter")


def test_extract_hgvs_c_p_without_protein_block() -> None:
    raw = "NM_014855.3(AP5Z1):c.80A>T"
    assert _extract_hgvs_c_p(raw) == ("NM_014855.3(AP5Z1):c.80A>T", None)


def test_extract_hgvs_c_p_empty_returns_none_pair() -> None:
    assert _extract_hgvs_c_p("") == (None, None)
    assert _extract_hgvs_c_p("-") == (None, None)


def test_extract_hgvs_c_p_preserves_p_dot_body() -> None:
    raw = "NM_014855.3(AP5Z1):c.80_83delinsTGCT (p.Arg27_Ile28delinsLeuLeuTer)"
    hgvs_c, hgvs_p = _extract_hgvs_c_p(raw)
    assert hgvs_p == "p.Arg27_Ile28delinsLeuLeuTer"
    assert hgvs_c == "NM_014855.3(AP5Z1):c.80_83delinsTGCT"


# ---------------------------------------------------------------------------
# _review_status_to_star
# ---------------------------------------------------------------------------


def test_review_status_to_star_full_mapping() -> None:
    cases = [
        ("practice guideline", 4),
        ("reviewed by expert panel", 3),
        ("criteria provided, multiple submitters, no conflicts", 2),
        ("criteria provided, single submitter", 1),
        ("criteria provided, conflicting classifications", 1),
        ("criteria provided, conflicting interpretations", 1),
        ("no assertion criteria provided", 0),
        ("no assertion provided", 0),
    ]
    for status, star in cases:
        assert _review_status_to_star(status) == star, status


def test_review_status_to_star_unknown_returns_none() -> None:
    """A new ClinVar wording → NULL, not a wrong star count."""
    assert _review_status_to_star("brand new review status") is None


def test_review_status_to_star_none_input_returns_none() -> None:
    assert _review_status_to_star(None) is None


# ---------------------------------------------------------------------------
# _parse_variant_summary — 50-row fixture
# ---------------------------------------------------------------------------


def test_parse_variant_summary_emits_exact_50_rows() -> None:
    expected, tsv_text = _build_50_row_fixture()
    rows = list(_parse_variant_summary(io.StringIO(tsv_text)))
    assert len(rows) == 50
    assert len(expected) == 50


def test_parse_variant_summary_field_mapping_matches_expected() -> None:
    expected, tsv_text = _build_50_row_fixture()
    rows = list(_parse_variant_summary(io.StringIO(tsv_text)))
    for actual, want in zip(rows, expected, strict=True):
        # variation_id is always populated.
        assert actual.variation_id == want["variation_id"], want["variation_id"]
        # rsid coercion: -1 / "" → None, digits → "rs<digits>".
        assert actual.rsid == want["rsid"], want["variation_id"]
        # GRCh37 rows have NULL position; GRCh38 rows have the parsed int.
        assert actual.pos_grch38 == want["pos_grch38"], want["variation_id"]
        if "ref_allele" in want:
            assert actual.ref_allele == want["ref_allele"], want["variation_id"]
        if "alt_allele" in want:
            assert actual.alt_allele == want["alt_allele"], want["variation_id"]
        # Clinical significance preserved verbatim.
        assert actual.clinical_significance == want["clinical_significance"]
        # star_rating derived from review_status via the locked mapping.
        assert actual.star_rating == want["star_rating"], want["variation_id"]
        if "chrom" in want:
            assert actual.chrom == want["chrom"], want["variation_id"]


def test_parse_variant_summary_grch37_row_nulls_grch38_columns() -> None:
    """GRCh37 row stores chrom + identifiers but NULLs out the GRCh38 cols."""
    _, tsv_text = _build_50_row_fixture()
    rows = list(_parse_variant_summary(io.StringIO(tsv_text)))
    grch37_row = next(r for r in rows if r.variation_id == "14" and r.pos_grch38 is None)
    # Position-specific columns NULL.
    assert grch37_row.pos_grch38 is None
    assert grch37_row.ref_allele is None
    assert grch37_row.alt_allele is None
    # But the row is otherwise populated.
    assert grch37_row.chrom == "7"
    assert grch37_row.rsid == "rs397704705"
    assert grch37_row.clinical_significance == "Pathogenic"


def test_parse_variant_summary_grch38_row_populates_all_columns() -> None:
    _, tsv_text = _build_50_row_fixture()
    rows = list(_parse_variant_summary(io.StringIO(tsv_text)))
    grch38_row = next(r for r in rows if r.variation_id == "14" and r.pos_grch38 == 4781213)
    assert grch38_row.chrom == "7"
    assert grch38_row.pos_grch38 == 4781213
    assert grch38_row.ref_allele == "G"
    assert grch38_row.alt_allele == "T"
    assert grch38_row.rsid == "rs397704705"


def test_parse_variant_summary_missing_required_header_raises() -> None:
    """Loud-fail when ClinVar's header changes -- the loader can't guess."""
    bad_header = "#AlleleID\tType\tName\n1\tx\ty\n"
    with pytest.raises(ValueError, match="missing expected columns"):
        list(_parse_variant_summary(io.StringIO(bad_header)))


def test_parse_variant_summary_no_header_raises() -> None:
    """An empty file is a contract violation, not a zero-row load."""
    with pytest.raises(ValueError, match="no header row"):
        list(_parse_variant_summary(io.StringIO("")))


# ---------------------------------------------------------------------------
# _iter_chunks (the chunk-boundary contract)
# ---------------------------------------------------------------------------


class _CountingRow:
    """Cheap stand-in for ``_ParsedRow`` for the chunk-boundary test.

    The chunk-iteration helper is fully agnostic about the row type
    (it's just ``Iterator[_ParsedRow]`` at the type-checker level), so
    a lightweight sentinel keeps the 1M-row test under a second.
    """

    __slots__ = ()


def _gen_dummy_rows(n: int) -> Iterator[_ParsedRow]:
    """Yield ``n`` cheap sentinels typed as ``_ParsedRow`` for the iter test."""
    for _ in range(n):
        yield _CountingRow()  # type: ignore[misc]


def test_iter_chunks_exact_boundary_at_default_chunk_size() -> None:
    """1,000,001 rows → 4 full chunks of 250K + 1 tail chunk of 1.

    This is the locked "the chunk emission shape stays stable" check.
    A future calibration that flips ``_CHUNK_SIZE`` to a different
    value will need to update both the constant and this test.
    """
    expected_total = 4 * _CHUNK_SIZE + 1
    chunks = list(_iter_chunks(_gen_dummy_rows(expected_total), _CHUNK_SIZE))
    chunk_sizes = [len(c) for c in chunks]
    full_chunks = 4
    assert chunk_sizes == [_CHUNK_SIZE] * full_chunks + [1]
    assert sum(chunk_sizes) == expected_total


def test_iter_chunks_handles_zero_rows() -> None:
    chunks = list(_iter_chunks(_gen_dummy_rows(0), _CHUNK_SIZE))
    assert chunks == []


def test_iter_chunks_single_partial_chunk() -> None:
    chunks = list(_iter_chunks(_gen_dummy_rows(7), _CHUNK_SIZE))
    chunk_sizes = [len(c) for c in chunks]
    assert chunk_sizes == [7]


def test_iter_chunks_smaller_chunk_size() -> None:
    """The helper accepts any chunk size, not just the module default."""
    chunks = list(_iter_chunks(_gen_dummy_rows(10), 3))
    chunk_sizes = [len(c) for c in chunks]
    assert chunk_sizes == [3, 3, 3, 1]


# ---------------------------------------------------------------------------
# _stream_bulk_insert chunk-counter test (no DB)
# ---------------------------------------------------------------------------


def test_stream_bulk_insert_emits_one_insert_chunk_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_stream_bulk_insert`` must call ``_insert_chunk`` once per chunk.

    Verifies the contract that every chunk's PyArrow Table makes it
    to ``_insert_chunk`` -- and that the chunk sizes match the
    ``_iter_chunks`` shape. Mocks both ``_next_clinvar_id`` and
    ``_insert_chunk`` so the test runs without a real DB.
    """
    monkeypatch.setattr(clinvar_loader, "_next_clinvar_id", lambda _conn: 1)

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

    monkeypatch.setattr(clinvar_loader, "_insert_chunk", _stub_insert_chunk)

    expected_total = 4 * _CHUNK_SIZE + 1
    total = _stream_bulk_insert(
        conn=None,  # type: ignore[arg-type] — _next_clinvar_id and _insert_chunk are stubbed
        rows_iter=_gen_dummy_rows(expected_total),
        source_version_id=1,
        retrieval_date=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert total == expected_total
    full_chunks = 4
    assert seen == [_CHUNK_SIZE] * full_chunks + [1]


# ---------------------------------------------------------------------------
# _resolve_version_via_head
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
    """Force ``_resolve_version_via_head`` to use a MockTransport-backed httpx.

    Patches ``httpx.Client`` inside the loader module so the HEAD path
    doesn't touch the real network. The handler receives the
    ``httpx.Request`` and returns an ``httpx.Response`` -- the standard
    httpx-mocking pattern from ``test_privacy_external_client.py``.
    """
    real_client_cls = httpx.Client

    def _factory(
        *_args: object,
        timeout: float = 30.0,  # noqa: ARG001
        follow_redirects: bool = False,  # noqa: ARG001
        **_kwargs: object,
    ) -> httpx.Client:
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(clinvar_loader.httpx, "Client", _factory)


def test_resolve_version_reads_last_modified_header() -> None:
    """A normal Last-Modified header → YYYY_MM_DD."""
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Last-Modified": "Sun, 10 May 2026 15:15:44 GMT"},
        )

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        version = _resolve_version_via_head()
    finally:
        monkeypatch.undo()
    assert version == "2026_05_10"


def test_resolve_version_falls_back_when_header_missing() -> None:
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={})

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        version = _resolve_version_via_head()
    finally:
        monkeypatch.undo()
    assert version == datetime.now(UTC).strftime("%Y_%m_%d")


def test_resolve_version_falls_back_when_header_unparseable() -> None:
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Last-Modified": "garbage-not-a-date"})

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        version = _resolve_version_via_head()
    finally:
        monkeypatch.undo()
    # parsedate_to_datetime returns None for fully-unparseable input
    # in some Python versions; the loader treats both that and any
    # raised TypeError/ValueError as the fallback path.
    assert re.match(r"^\d{4}_\d{2}_\d{2}$", version)


def test_resolve_version_falls_back_when_http_error() -> None:
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        version = _resolve_version_via_head()
    finally:
        monkeypatch.undo()
    assert version == datetime.now(UTC).strftime("%Y_%m_%d")


# ---------------------------------------------------------------------------
# Integration: full transaction (1,000-row fixture)
# ---------------------------------------------------------------------------


def _build_n_row_tsv(n: int, *, base_clin_sig: str = "Benign") -> str:
    """Build a TSV with ``n`` GRCh38 single-allele rows."""
    rows = [
        _row(
            variation_id=str(i),
            assembly="GRCh38",
            position_vcf=str(100_000 + i),
            rs=str(1000 + i),
            clin_sig=base_clin_sig,
            review_status="criteria provided, single submitter",
        )
        for i in range(1, n + 1)
    ]
    return _FIXTURE_HEADER + "\n" + "\n".join(rows) + "\n"


def test_refresh_full_transaction_inserts_1000_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1,000-row fixture → 1 source_version row, 1,000 active rows."""
    init_databases()
    gz_path = tmp_path / "variant_summary.txt.gz"
    _write_gz(gz_path, _build_n_row_tsv(1000))
    _patch_download_to_cache(monkeypatch, gz_path)
    _patch_resolve_version(monkeypatch, "2026_05_10")

    result = clinvar_loader.refresh(force=False)

    expected_n = 1000
    assert result.source_db == "clinvar"
    assert result.version == "2026_05_10"
    assert result.record_count == expected_n
    assert result.was_already_current is False

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM clinvar_annotations WHERE is_active = TRUE",
        ).fetchone()
        inactive = conn.execute(
            "SELECT COUNT(*) FROM clinvar_annotations WHERE is_active = FALSE",
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version, record_count, is_current FROM annotation_source_versions"
            " WHERE source_db = 'clinvar'",
        ).fetchall()
        # Every row is tagged with the new source_version_id.
        sv_count = conn.execute(
            "SELECT COUNT(DISTINCT source_version_id) FROM clinvar_annotations"
            " WHERE is_active = TRUE",
        ).fetchone()
    assert active is not None
    assert active[0] == expected_n
    assert inactive is not None
    assert inactive[0] == 0
    assert version_rows == [("2026_05_10", expected_n, True)]
    assert sv_count is not None
    assert sv_count[0] == 1


def test_refresh_writes_expected_column_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-row sanity: every parsed column lands in the right DB column."""
    init_databases()
    gz_path = tmp_path / "variant_summary.txt.gz"
    tsv = _FIXTURE_HEADER + "\n" + _row() + "\n"
    _write_gz(gz_path, tsv)
    _patch_download_to_cache(monkeypatch, gz_path)
    _patch_resolve_version(monkeypatch, "2026_05_10")

    clinvar_loader.refresh(force=False)

    with duckdb_connection() as conn:
        row = conn.execute(
            """
            SELECT variation_id, rsid, chrom, pos_grch38, ref_allele,
                   alt_allele, clinical_significance, review_status,
                   star_rating, last_evaluated, conditions, condition_ids,
                   submission_count, submitter_categories, hgvs_c, hgvs_p,
                   inheritance, is_active, superseded_by
              FROM clinvar_annotations
            """,
        ).fetchone()
    assert row is not None
    (
        variation_id,
        rsid,
        chrom,
        pos,
        ref,
        alt,
        sig,
        review,
        star,
        last_eval,
        conditions,
        condition_ids,
        sub_count,
        submitter_cats,
        hgvs_c,
        hgvs_p,
        inheritance,
        active,
        superseded,
    ) = row
    assert variation_id == "14"
    assert rsid == "rs397704705"
    assert str(chrom) == "7"
    assert pos == 4781213
    assert ref == "G"
    assert alt == "T"
    assert sig == "Pathogenic"
    assert review == "criteria provided, multiple submitters, no conflicts"
    star_rating_for_two_star_review = 2
    assert star == star_rating_for_two_star_review
    assert last_eval == date(2024, 12, 17)
    assert conditions == [
        "Hereditary spastic paraplegia 48",
        "Macular dystrophy with or without extraocular features",
    ]
    assert condition_ids == [
        "MONDO:MONDO:0013342",
        "MedGen:C3150901",
        "OMIM:613647",
        "MedGen:C3661900",
    ]
    expected_submission_count = 4
    assert sub_count == expected_submission_count
    assert submitter_cats == ["3"]
    assert hgvs_c == "NM_014855.3(AP5Z1):c.80A>T"
    assert hgvs_p == "p.Lys27Ter"
    assert inheritance is None
    assert active is True
    assert superseded is None


# ---------------------------------------------------------------------------
# Integration: supersession (re-run on same fixture twice)
# ---------------------------------------------------------------------------


def test_refresh_supersedes_prior_rows_on_new_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two refreshes of the same 1,000 rows under different versions.

    Asserts: 2 source_version rows, 2,000 total rows (1K active under
    new version, 1K inactive under old), all inactive rows
    superseded_by the new version.
    """
    init_databases()
    gz_path = tmp_path / "variant_summary.txt.gz"
    tsv_text = _build_n_row_tsv(1000)
    _write_gz(gz_path, tsv_text)
    _patch_download_to_cache(monkeypatch, gz_path)

    _patch_resolve_version(monkeypatch, "2026_05_10")
    first = clinvar_loader.refresh(force=False)
    _patch_resolve_version(monkeypatch, "2026_05_17")
    second = clinvar_loader.refresh(force=False)

    expected_n = 1000
    assert first.version == "2026_05_10"
    assert second.version == "2026_05_17"
    assert second.was_already_current is False
    assert second.source_version_id > first.source_version_id

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM clinvar_annotations"
            " WHERE is_active = TRUE AND source_version_id = ?",
            [second.source_version_id],
        ).fetchone()
        inactive = conn.execute(
            "SELECT COUNT(*) FROM clinvar_annotations"
            " WHERE is_active = FALSE AND source_version_id = ?",
            [first.source_version_id],
        ).fetchone()
        # Every inactive row points at the new version.
        superseded = conn.execute(
            "SELECT COUNT(*) FROM clinvar_annotations"
            " WHERE is_active = FALSE AND superseded_by = ?",
            [second.source_version_id],
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version, is_current FROM annotation_source_versions"
            " WHERE source_db = 'clinvar' ORDER BY source_version_id",
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM clinvar_annotations").fetchone()
    assert active is not None
    assert active[0] == expected_n
    assert inactive is not None
    assert inactive[0] == expected_n
    assert superseded is not None
    assert superseded[0] == expected_n
    assert total is not None
    assert total[0] == 2 * expected_n
    assert version_rows == [("2026_05_10", False), ("2026_05_17", True)]


def test_refresh_modified_row_supersession(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per the prompt's modified-row supersession spec.

    Run on a 100-row fixture, then re-run on the same fixture with one
    row's ClinicalSignificance changed from "Benign" to "Likely benign".
    Assert the changed variant has both an inactive Benign row and an
    active Likely benign row, both linked to the right source_version_ids.
    """
    init_databases()

    # v1: 100 Benign rows.
    v1_path = tmp_path / "v1.txt.gz"
    _write_gz(v1_path, _build_n_row_tsv(100, base_clin_sig="Benign"))
    _patch_download_to_cache(monkeypatch, v1_path)
    _patch_resolve_version(monkeypatch, "2026_05_10")
    first = clinvar_loader.refresh(force=False)

    # v2: same 100 rows but variation_id=42's ClinicalSignificance flipped.
    rows: list[str] = []
    flipped_variant_id = 42
    for i in range(1, 101):
        sig = "Likely benign" if i == flipped_variant_id else "Benign"
        rows.append(
            _row(
                variation_id=str(i),
                assembly="GRCh38",
                position_vcf=str(100_000 + i),
                rs=str(1000 + i),
                clin_sig=sig,
                review_status="criteria provided, single submitter",
            ),
        )
    v2_path = tmp_path / "v2.txt.gz"
    _write_gz(v2_path, _FIXTURE_HEADER + "\n" + "\n".join(rows) + "\n")
    _patch_download_to_cache(monkeypatch, v2_path)
    _patch_resolve_version(monkeypatch, "2026_05_17")
    second = clinvar_loader.refresh(force=False)

    with duckdb_connection() as conn:
        # Variant 42's history: one inactive "Benign" + one active "Likely benign".
        history = conn.execute(
            "SELECT clinical_significance, is_active, source_version_id, superseded_by"
            " FROM clinvar_annotations WHERE variation_id = '42'"
            " ORDER BY clinvar_id",
        ).fetchall()
    assert len(history) == 2
    inactive, active = history[0], history[1]
    assert inactive[0] == "Benign"
    assert inactive[1] is False
    assert inactive[2] == first.source_version_id
    assert inactive[3] == second.source_version_id
    assert active[0] == "Likely benign"
    assert active[1] is True
    assert active[2] == second.source_version_id
    assert active[3] is None


def test_refresh_idempotent_short_circuit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same version on second call → was_already_current=True, no new rows."""
    init_databases()
    gz_path = tmp_path / "variant_summary.txt.gz"
    _write_gz(gz_path, _build_n_row_tsv(50))
    _patch_download_to_cache(monkeypatch, gz_path)
    _patch_resolve_version(monkeypatch, "2026_05_10")

    first = clinvar_loader.refresh(force=False)
    second = clinvar_loader.refresh(force=False)

    expected_n = 50
    assert first.was_already_current is False
    assert second.was_already_current is True
    assert second.source_version_id == first.source_version_id
    assert second.record_count == expected_n

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM clinvar_annotations WHERE is_active = TRUE",
        ).fetchone()
        n_versions = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'clinvar'",
        ).fetchone()
    assert active is not None
    assert active[0] == expected_n
    assert n_versions is not None
    assert n_versions[0] == 1


def test_refresh_force_reloads_same_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``force=True`` re-runs even when version is unchanged.

    Same-version --force: ``upsert_source_version`` is idempotent on
    (source_db, version), so source_version_id is unchanged. The
    force path blanket-deactivates every prior active row (tagging
    them with superseded_by = same version_id) and re-inserts.
    """
    init_databases()
    gz_path = tmp_path / "variant_summary.txt.gz"
    _write_gz(gz_path, _build_n_row_tsv(100))
    _patch_download_to_cache(monkeypatch, gz_path)
    _patch_resolve_version(monkeypatch, "2026_05_10")

    first = clinvar_loader.refresh(force=False)
    second = clinvar_loader.refresh(force=True)

    expected_n = 100
    assert first.was_already_current is False
    assert second.was_already_current is False
    assert second.source_version_id == first.source_version_id

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM clinvar_annotations WHERE is_active = TRUE",
        ).fetchone()
        deactivated = conn.execute(
            "SELECT COUNT(*) FROM clinvar_annotations"
            " WHERE is_active = FALSE AND superseded_by = ?",
            [first.source_version_id],
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM clinvar_annotations").fetchone()
    assert active is not None
    assert active[0] == expected_n
    assert deactivated is not None
    assert deactivated[0] == expected_n
    assert total is not None
    assert total[0] == 2 * expected_n


def test_refresh_transaction_rolls_back_on_bulk_insert_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A streaming-insert failure rolls back every chunk + the deactivation."""
    init_databases()
    gz_path = tmp_path / "variant_summary.txt.gz"
    _write_gz(gz_path, _build_n_row_tsv(100))
    _patch_download_to_cache(monkeypatch, gz_path)
    _patch_resolve_version(monkeypatch, "2026_05_10")

    boom = RuntimeError("simulated insert failure")

    def _explode(*_args: object, **_kwargs: object) -> int:
        raise boom

    monkeypatch.setattr(clinvar_loader, "_stream_bulk_insert", _explode)

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        clinvar_loader.refresh(force=False)

    with duckdb_connection() as conn:
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'clinvar'",
        ).fetchone()
        annotation_rows = conn.execute(
            "SELECT COUNT(*) FROM clinvar_annotations",
        ).fetchone()
    # Transaction rolled back; orphan version row cleaned up.
    assert version_rows is not None
    assert version_rows[0] == 0
    assert annotation_rows is not None
    assert annotation_rows[0] == 0


# ---------------------------------------------------------------------------
# Integration: external-calls-disabled refusal
# ---------------------------------------------------------------------------


def test_refresh_blocked_when_external_calls_disabled() -> None:
    """A disabled master switch raises and leaves an intent + blocked pair.

    The loader's first audited call is the HEAD that resolves
    version. With external_calls_enabled=false (the
    ``init_databases`` seed default), the disabled check raises
    :class:`ExternalCallsDisabledError` before the body of the HEAD
    request is sent. The pair lands in audit_log under the
    ``annotations_clinvar`` endpoint with resource_id
    ``clinvar_release_metadata`` so an operator can see exactly which
    URL was attempted.
    """
    init_databases()
    # init_databases seeds external_calls_enabled=false; do not flip it.
    # No patching of download_to_cache or _resolve_version_via_head --
    # we want the real audited flow.

    with pytest.raises(ExternalCallsDisabledError, match="genome config set"):
        clinvar_loader.refresh(force=False)

    rows = _audit_rows()
    expected_pair = 2
    assert len(rows) == expected_pair, rows
    intent, blocked = rows
    intent_details = json.loads(str(intent[3]))
    blocked_details = json.loads(str(blocked[3]))
    assert intent_details["phase"] == "intent"
    assert intent_details["method"] == "HEAD"
    assert blocked_details["status"] == "blocked"
    assert blocked_details["method"] == "HEAD"
    assert intent[1] == blocked[1] == "annotation_source"
    assert intent[2] == blocked[2] == "clinvar_release_metadata"
    assert intent[5] == blocked[5] == "annotations_clinvar"


# ---------------------------------------------------------------------------
# Registry / module-import side effects
# ---------------------------------------------------------------------------


def test_get_loader_returns_clinvar_refresh() -> None:
    assert get_loader("clinvar") is clinvar_loader.refresh


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_refresh_clinvar_runs_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    gz_path = tmp_path / "variant_summary.txt.gz"
    _write_gz(gz_path, _build_n_row_tsv(50))
    _patch_download_to_cache(monkeypatch, gz_path)
    _patch_resolve_version(monkeypatch, "2026_05_10")

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "refresh", "--source", "clinvar"])
    assert result.exit_code == 0, result.output
    assert "source_db=clinvar" in result.output
    assert "version=2026_05_10" in result.output
    assert "records=50" in result.output
    assert "already_current=False" in result.output


def test_cli_status_after_refresh_reports_clinvar_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    gz_path = tmp_path / "variant_summary.txt.gz"
    _write_gz(gz_path, _build_n_row_tsv(20))
    _patch_download_to_cache(monkeypatch, gz_path)
    _patch_resolve_version(monkeypatch, "2026_05_10")
    clinvar_loader.refresh(force=False)

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "status"])
    assert result.exit_code == 0, result.output
    clinvar_lines = [line for line in result.output.splitlines() if line.startswith("clinvar:")]
    assert len(clinvar_lines) == 1
    line = clinvar_lines[0]
    assert "2026_05_10" in line
    assert "ingested " in line
    assert "20 records" in line


def test_cli_refresh_force_flag_passes_through_to_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` must reach the loader's force argument verbatim."""
    init_databases()
    gz_path = tmp_path / "variant_summary.txt.gz"
    _write_gz(gz_path, _build_n_row_tsv(10))
    _patch_download_to_cache(monkeypatch, gz_path)
    _patch_resolve_version(monkeypatch, "2026_05_10")
    clinvar_loader.refresh(force=False)

    received: dict[str, bool] = {}

    def _recording_refresh(force: bool) -> object:  # noqa: FBT001
        received["force"] = force
        from genome.annotate.registry import RefreshResult  # noqa: PLC0415

        return RefreshResult(
            source_db="clinvar",
            source_version_id=42,
            version="2026_05_10",
            record_count=0,
            was_already_current=False,
        )

    monkeypatch.setattr(
        "genome.annotate.registry._LOADERS",
        {"clinvar": _recording_refresh},
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["annotate", "refresh", "--source", "clinvar", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert received == {"force": True}


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_url_verified_date_is_iso_format() -> None:
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", clinvar_loader.URL_VERIFIED_DATE)


def test_variant_summary_url_matches_canonical_ncbi_path() -> None:
    assert clinvar_loader.VARIANT_SUMMARY_URL == (
        "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz"
    )


def test_source_db_label() -> None:
    assert clinvar_loader.SOURCE_DB == "clinvar"


def test_chunk_size_locked_at_250k() -> None:
    """The runbook documents 250K; pin it so a casual flip is loud."""
    expected_chunk_size = 250_000
    assert expected_chunk_size == _CHUNK_SIZE


# ---------------------------------------------------------------------------
# _insert_chunk smoke (real DB; verifies the SELECT cast lands cleanly)
# ---------------------------------------------------------------------------


def test_insert_chunk_handles_nulls_for_grch37_columns(tmp_path: Path) -> None:  # noqa: ARG001
    """Direct call exercises the chrom enum cast and list-column shapes."""
    init_databases()
    rows = [
        _ParsedRow(
            variation_id="VID-1",
            rsid=None,
            chrom="X",
            pos_grch38=None,  # GRCh37 row → NULL position
            ref_allele=None,
            alt_allele=None,
            clinical_significance="Pathogenic",
            review_status="practice guideline",
            star_rating=4,
            last_evaluated=date(2025, 1, 1),
            conditions=["foo", "bar"],
            condition_ids=None,
            submission_count=2,
            submitter_categories=["3"],
            hgvs_c="NM_X:c.1A>T",
            hgvs_p="p.Met1Ter",
            inheritance=None,
        ),
    ]
    with duckdb_connection() as conn:
        # We need an annotation_source_versions row to satisfy the FK.
        from genome.annotate.source_versions import (  # noqa: PLC0415
            upsert_source_version,
        )

        sv_id = upsert_source_version(
            conn,
            source_db="clinvar",
            version="2026_05_10",
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
            retrieval_date=datetime(2026, 5, 15, tzinfo=UTC),
        )
    assert n == 1
    with duckdb_connection() as conn:
        row = conn.execute(
            "SELECT chrom, pos_grch38, conditions, condition_ids, star_rating"
            " FROM clinvar_annotations WHERE variation_id = 'VID-1'",
        ).fetchone()
    assert row is not None
    chrom, pos, conditions, condition_ids, star = row
    assert str(chrom) == "X"
    assert pos is None
    assert conditions == ["foo", "bar"]
    assert condition_ids is None
    star_for_practice_guideline = 4
    assert star == star_for_practice_guideline
