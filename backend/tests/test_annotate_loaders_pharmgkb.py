"""Tests for :mod:`genome.annotate.loaders.pharmgkb`.

Covers the parsing helpers, the version-resolution path (both with
and without PharmGKB's ``CREATED_*.txt`` metadata), and the end-to-end
``refresh`` flow with ``download_to_cache`` monkey-patched to point at
a programmatically-built ZIP. The fixture set exercises rsID, star
allele, HLA allele, descriptive haplotype, multi-drug expansion,
single-drug-name-with-embedded-comma, every evidence level, and an
empty-gene row — i.e. every quirk the real PharmGKB TSV throws at the
loader.
"""

from __future__ import annotations

import re
import zipfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from genome.annotate import downloads as annotate_downloads
from genome.annotate.loaders import pharmgkb as pharmgkb_loader
from genome.annotate.loaders.pharmgkb import (
    _parse_clinical_annotations_tsv,
    _parse_variant_field,
    _resolve_version,
    _split_drugs,
)
from genome.annotate.registry import get_loader
from genome.annotate.source_versions import get_current_version
from genome.cli import app
from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixture TSV — 7 source rows that expand to 12 DB rows post-multi-drug split.
# ---------------------------------------------------------------------------

_FIXTURE_HEADER = (
    "Clinical Annotation ID\tVariant/Haplotypes\tGene\tLevel of Evidence\t"
    "Level Override\tLevel Modifiers\tScore\tPhenotype Category\tPMID Count\t"
    "Evidence Count\tDrug(s)\tPhenotype(s)\tLatest History Date (YYYY-MM-DD)\t"
    "URL\tSpecialty Population"
)

# Each tuple builds one TSV row. Columns match _FIXTURE_HEADER above.
_FIXTURE_ROWS: list[tuple[str, ...]] = [
    # (annotation_id, variant, gene, evidence, override, modifiers, score,
    #  phenotype_cat, pmid_count, evidence_count, drugs, phenotypes,
    #  latest_date, url, specialty)
    (
        "1001",
        "rs951439",
        "RGS4",
        "3",
        "",
        "",
        "1.75",
        "Efficacy",
        "1",
        "1",
        "risperidone",
        "Schizophrenia",
        "2021-03-24",
        "https://www.pharmgkb.org/clinicalAnnotation/1001",
        "",
    ),
    (
        "1002",
        "rs951439",
        "RGS4",
        "1A",
        "",
        "",
        "5.0",
        "Efficacy",
        "1",
        "3",
        "antipsychotics;olanzapine;perphenazine;quetiapine;ziprasidone",
        "Schizophrenia",
        "2021-03-24",
        "https://www.pharmgkb.org/clinicalAnnotation/1002",
        "",
    ),
    (
        "1003",
        "CYP2D6*4",
        "CYP2D6",
        "1B",
        "",
        "Tier 1 VIP",
        "0.0",
        "Toxicity",
        "2",
        "2",
        "codeine",
        "",
        "2023-01-15",
        "https://www.pharmgkb.org/clinicalAnnotation/1003",
        "",
    ),
    (
        "1004",
        "HLA-B*57:01",
        "HLA-B",
        "1A",
        "",
        "",
        "4.0",
        "Toxicity",
        "5",
        "10",
        "abacavir",
        "Hypersensitivity",
        "2024-06-10",
        "https://www.pharmgkb.org/clinicalAnnotation/1004",
        "",
    ),
    (
        "1005",
        "G6PD A- 202A_376G, G6PD B (reference)",
        "G6PD",
        "2A",
        "",
        "",
        "2.0",
        "Toxicity",
        "1",
        "1",
        "primaquine",
        "G6PD deficiency",
        "2024-02-01",
        "https://www.pharmgkb.org/clinicalAnnotation/1005",
        "",
    ),
    (
        "1006",
        "rs1801133",
        "",
        "2B",
        "",
        "",
        "1.0",
        "Metabolism/PK",
        "1",
        "1",
        "methotrexate",
        "",
        "2023-08-15",
        "https://www.pharmgkb.org/clinicalAnnotation/1006",
        "",
    ),
    (
        "1007",
        "rs4149015",
        "SLCO1B1",
        "4",
        "",
        "",
        "0.5",
        "Dosage",
        "1",
        "1",
        "Ace Inhibitors, Plain;Beta blocking agents, selective",
        "Hypertension",
        "2024-03-01",
        "https://www.pharmgkb.org/clinicalAnnotation/1007",
        "",
    ),
]

# After multi-drug expansion: 1+5+1+1+1+1+2 = 12 DB rows.
_EXPECTED_DB_ROW_COUNT = 12

# Distribution of evidence levels after multi-drug expansion.
_EXPECTED_EVIDENCE_DISTRIBUTION = {
    "1A": 6,  # 1002 (x5) + 1004
    "1B": 1,  # 1003
    "2A": 1,  # 1005
    "2B": 1,  # 1006
    "3": 1,  # 1001
    "4": 2,  # 1007
}


def _build_fixture_tsv(rows: list[tuple[str, ...]] | None = None) -> str:
    """Render a TSV string from row tuples (header + data)."""
    actual_rows = rows if rows is not None else _FIXTURE_ROWS
    body = "\n".join("\t".join(row) for row in actual_rows)
    return f"{_FIXTURE_HEADER}\n{body}\n"


def _write_fixture_zip(
    path: Path,
    *,
    created_date: str | None = "2026-04-15",
    rows: list[tuple[str, ...]] | None = None,
) -> None:
    """Write a PharmGKB-shaped ZIP at ``path``.

    When ``created_date`` is None, the ``CREATED_*.txt`` marker is
    omitted (exercising the retrieval-date fallback path).
    """
    with zipfile.ZipFile(path, "w") as zf:
        if created_date is not None:
            zf.writestr(
                f"CREATED_{created_date}.txt",
                f"Created on {created_date.replace('-', '/')} at 00:00:00 UTC.\n",
            )
        zf.writestr("clinical_annotations.tsv", _build_fixture_tsv(rows))
        zf.writestr("LICENSE.txt", "(c) PharmGKB. Fixture only — see real release.\n")


@pytest.fixture(autouse=True)
def _ensure_pharmgkb_registered() -> Iterator[None]:
    """Re-register the loader at test start.

    Other annotate test files (``test_annotate_cli.py``,
    ``test_annotate_registry.py``) install autouse fixtures that wipe
    the registry via ``_clear_loaders_for_testing()`` to keep their own
    cases hermetic. When their tests run before ours in a full-suite
    invocation, the side-effect registration that
    ``genome.annotate.loaders.pharmgkb`` performed at import time is
    gone. Re-registering here makes our tests order-independent.
    """
    from genome.annotate.registry import _LOADERS, register_loader  # noqa: PLC0415

    _LOADERS.pop("pharmgkb", None)
    register_loader("pharmgkb", pharmgkb_loader.refresh)
    try:
        yield
    finally:
        _LOADERS.pop("pharmgkb", None)


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


@pytest.fixture
def fixture_zip(tmp_path: Path) -> Path:
    """Write the default fixture ZIP to disk and return its path."""
    path = tmp_path / "clinicalAnnotations.zip"
    _write_fixture_zip(path)
    return path


def _patch_download_to_cache(
    monkeypatch: pytest.MonkeyPatch,
    zip_path: Path,
) -> dict[str, int]:
    """Replace ``download_to_cache`` with a stub that returns ``zip_path``.

    Patches the symbol where the loader looks it up (``pharmgkb_loader``)
    rather than the scaffold module, mirroring pytest's "patch at the
    call site" convention. The stub avoids any real HTTP and records
    the call count so tests can assert "download was / was not invoked"
    semantics. The returned :class:`DownloadResult` uses the actual
    fixture file's size and SHA-256 so the loader's downstream
    ``annotation_source_versions`` row carries realistic provenance.
    """
    import hashlib  # noqa: PLC0415

    counter = {"calls": 0}
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

    monkeypatch.setattr(pharmgkb_loader, "download_to_cache", _stub)
    return counter


# ---------------------------------------------------------------------------
# _parse_variant_field
# ---------------------------------------------------------------------------


def test_parse_variant_field_rsid() -> None:
    assert _parse_variant_field("rs1234567") == ("rs1234567", None)


def test_parse_variant_field_star_allele() -> None:
    assert _parse_variant_field("CYP2D6*4") == (None, "CYP2D6*4")


def test_parse_variant_field_hla_allele() -> None:
    assert _parse_variant_field("HLA-B*57:01") == (None, "HLA-B*57:01")


def test_parse_variant_field_descriptive_haplotype() -> None:
    raw = "G6PD A- 202A_376G, G6PD B (reference)"
    assert _parse_variant_field(raw) == (None, raw)


def test_parse_variant_field_empty() -> None:
    assert _parse_variant_field("") == (None, None)


def test_parse_variant_field_whitespace() -> None:
    assert _parse_variant_field("   \t  ") == (None, None)


def test_parse_variant_field_rs_with_trailing_text_is_not_rsid() -> None:
    """Strict ``^rs\\d+$`` rule: a non-rsID with rs-prefix goes to star_allele.

    Real PharmGKB rsIDs never have trailing tokens; this guards the
    detection rule from broadening to a permissive ``startswith('rs')``.
    """
    assert _parse_variant_field("rs1234abc") == (None, "rs1234abc")


# ---------------------------------------------------------------------------
# _split_drugs
# ---------------------------------------------------------------------------


def test_split_drugs_single() -> None:
    assert _split_drugs("warfarin") == ["warfarin"]


def test_split_drugs_multi_semicolon() -> None:
    raw = "antipsychotics;olanzapine;perphenazine;quetiapine;ziprasidone"
    assert _split_drugs(raw) == [
        "antipsychotics",
        "olanzapine",
        "perphenazine",
        "quetiapine",
        "ziprasidone",
    ]


def test_split_drugs_does_not_split_on_comma_inside_single_name() -> None:
    """Embedded commas in drug names (e.g. ``"Ace Inhibitors, Plain"``) must survive."""
    raw = "Ace Inhibitors, Plain"
    assert _split_drugs(raw) == ["Ace Inhibitors, Plain"]


def test_split_drugs_mixed_semicolons_and_embedded_commas() -> None:
    """A real-world cell: semicolons separate drugs, commas inside names stay put."""
    raw = "Ace Inhibitors, Plain;Beta blocking agents, selective"
    assert _split_drugs(raw) == [
        "Ace Inhibitors, Plain",
        "Beta blocking agents, selective",
    ]


def test_split_drugs_strips_whitespace_and_drops_empty_tokens() -> None:
    assert _split_drugs("warfarin ;  ; aspirin ") == ["warfarin", "aspirin"]


# ---------------------------------------------------------------------------
# _parse_clinical_annotations_tsv
# ---------------------------------------------------------------------------


def test_parse_tsv_yields_expected_post_expansion_count() -> None:
    rows = list(
        _parse_clinical_annotations_tsv(_build_fixture_tsv()),
    )
    assert len(rows) == _EXPECTED_DB_ROW_COUNT


def test_parse_tsv_multi_drug_row_shares_pgkb_accession() -> None:
    rows = list(
        _parse_clinical_annotations_tsv(_build_fixture_tsv()),
    )
    multi = [r for r in rows if r.pgkb_accession == "1002"]
    assert len(multi) == 5
    drugs = {r.drug_name for r in multi}
    assert drugs == {
        "antipsychotics",
        "olanzapine",
        "perphenazine",
        "quetiapine",
        "ziprasidone",
    }
    # Other fields stay identical across the expanded rows.
    assert {r.rsid for r in multi} == {"rs951439"}
    assert {r.evidence_level for r in multi} == {"1A"}
    assert {r.gene_symbol for r in multi} == {"RGS4"}


def test_parse_tsv_row_missing_gene_emits_none() -> None:
    rows = list(
        _parse_clinical_annotations_tsv(_build_fixture_tsv()),
    )
    matching = [r for r in rows if r.pgkb_accession == "1006"]
    assert len(matching) == 1
    assert matching[0].gene_symbol is None


def test_parse_tsv_chrom_and_pos_grch38_are_none() -> None:
    """PharmGKB TSV is rsID/haplotype-keyed; chrom/pos fill in via dbSNP later."""
    rows = list(
        _parse_clinical_annotations_tsv(_build_fixture_tsv()),
    )
    assert all(r.chrom is None for r in rows)
    assert all(r.pos_grch38 is None for r in rows)


def test_parse_tsv_star_allele_row_has_no_rsid() -> None:
    rows = list(
        _parse_clinical_annotations_tsv(_build_fixture_tsv()),
    )
    star_row = next(r for r in rows if r.pgkb_accession == "1003")
    assert star_row.rsid is None
    assert star_row.star_allele == "CYP2D6*4"


def test_parse_tsv_hla_row_routes_to_star_allele_bucket() -> None:
    rows = list(
        _parse_clinical_annotations_tsv(_build_fixture_tsv()),
    )
    hla_row = next(r for r in rows if r.pgkb_accession == "1004")
    assert hla_row.rsid is None
    assert hla_row.star_allele == "HLA-B*57:01"


def test_parse_tsv_evidence_levels_preserved() -> None:
    rows = list(
        _parse_clinical_annotations_tsv(_build_fixture_tsv()),
    )
    counts: dict[str, int] = {}
    for r in rows:
        if r.evidence_level is not None:
            counts[r.evidence_level] = counts.get(r.evidence_level, 0) + 1
    assert counts == _EXPECTED_EVIDENCE_DISTRIBUTION


def test_parse_tsv_phenotype_text_lands_in_guideline_summary() -> None:
    rows = list(
        _parse_clinical_annotations_tsv(_build_fixture_tsv()),
    )
    risperidone = next(
        r for r in rows if r.pgkb_accession == "1001" and r.drug_name == "risperidone"
    )
    assert risperidone.guideline_summary == "Phenotype(s): Schizophrenia"


def test_parse_tsv_empty_phenotype_is_none() -> None:
    rows = list(
        _parse_clinical_annotations_tsv(_build_fixture_tsv()),
    )
    codeine = next(r for r in rows if r.drug_name == "codeine")
    assert codeine.guideline_summary is None


def test_parse_tsv_guideline_url_preserved() -> None:
    rows = list(
        _parse_clinical_annotations_tsv(_build_fixture_tsv()),
    )
    one = next(r for r in rows if r.pgkb_accession == "1001")
    assert one.guideline_url == "https://www.pharmgkb.org/clinicalAnnotation/1001"


def test_parse_tsv_missing_required_header_raises() -> None:
    bad = "Clinical Annotation ID\tNotARealColumn\n1001\tx\n"
    with pytest.raises(ValueError, match="missing expected columns"):
        list(_parse_clinical_annotations_tsv(bad))


# ---------------------------------------------------------------------------
# _resolve_version
# ---------------------------------------------------------------------------


def test_resolve_version_reads_created_marker(tmp_path: Path) -> None:
    path = tmp_path / "z.zip"
    _write_fixture_zip(path, created_date="2026-04-15")
    assert _resolve_version(path) == "2026_04_15"


def test_resolve_version_falls_back_to_today_when_marker_absent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "z.zip"
    _write_fixture_zip(path, created_date=None)
    version = _resolve_version(path)
    assert re.match(r"^\d{4}_\d{2}_\d{2}$", version)
    today = datetime.now(UTC).strftime("%Y_%m_%d")
    assert version == today


def test_resolve_version_picks_only_matching_filename(tmp_path: Path) -> None:
    """Non-conforming filenames must not be mistaken for the date marker."""
    path = tmp_path / "z.zip"
    with zipfile.ZipFile(path, "w") as zf:
        # Plausible distractor filenames.
        zf.writestr("CREATED_DATE.txt", "ignored")
        zf.writestr("Created_2025-01-01.txt", "wrong case prefix")
        zf.writestr("clinical_annotations.tsv", _build_fixture_tsv())
    today = datetime.now(UTC).strftime("%Y_%m_%d")
    assert _resolve_version(path) == today


# ---------------------------------------------------------------------------
# refresh() end-to-end (using monkeypatched download_to_cache)
# ---------------------------------------------------------------------------


def test_refresh_inserts_rows_and_records_source_version(
    fixture_zip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_zip)

    result = pharmgkb_loader.refresh(force=False)

    assert result.source_db == "pharmgkb"
    assert result.version == "2026_04_15"
    assert result.record_count == _EXPECTED_DB_ROW_COUNT
    assert result.was_already_current is False

    with duckdb_connection() as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) FROM pharmgkb_annotations WHERE is_active = TRUE",
        ).fetchone()
        all_active = conn.execute(
            "SELECT COUNT(*) FROM pharmgkb_annotations WHERE is_active = FALSE",
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version, record_count, is_current FROM annotation_source_versions"
            " WHERE source_db = 'pharmgkb'",
        ).fetchall()

    assert active_count is not None
    assert active_count[0] == _EXPECTED_DB_ROW_COUNT
    assert all_active is not None
    assert all_active[0] == 0
    assert len(version_rows) == 1
    assert version_rows[0] == ("2026_04_15", _EXPECTED_DB_ROW_COUNT, True)


def test_refresh_second_call_is_short_circuit(
    fixture_zip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    counter = _patch_download_to_cache(monkeypatch, fixture_zip)
    first = pharmgkb_loader.refresh(force=False)
    second = pharmgkb_loader.refresh(force=False)

    assert first.was_already_current is False
    assert second.was_already_current is True
    assert second.version == first.version
    assert second.source_version_id == first.source_version_id
    # Download was attempted twice (once per call) — skip-if-exists is
    # the cache layer's job, not refresh()'s — but the second call must
    # not insert new rows.
    assert counter["calls"] == 2

    with duckdb_connection() as conn:
        n_active = conn.execute(
            "SELECT COUNT(*) FROM pharmgkb_annotations WHERE is_active = TRUE",
        ).fetchone()
        n_versions = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'pharmgkb'",
        ).fetchone()
    assert n_active is not None
    assert n_active[0] == _EXPECTED_DB_ROW_COUNT
    assert n_versions is not None
    assert n_versions[0] == 1


def test_refresh_new_version_supersedes_prior_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    v1_path = tmp_path / "v1.zip"
    v2_path = tmp_path / "v2.zip"
    _write_fixture_zip(v1_path, created_date="2026-04-15")
    # A trimmed fixture for v2 so we can verify both row counts move.
    trimmed_rows = _FIXTURE_ROWS[:3]
    _write_fixture_zip(v2_path, created_date="2026-05-15", rows=trimmed_rows)

    _patch_download_to_cache(monkeypatch, v1_path)
    first = pharmgkb_loader.refresh(force=False)
    _patch_download_to_cache(monkeypatch, v2_path)
    second = pharmgkb_loader.refresh(force=False)

    assert first.version == "2026_04_15"
    assert second.version == "2026_05_15"
    assert second.was_already_current is False
    assert second.source_version_id > first.source_version_id

    with duckdb_connection() as conn:
        inactive = conn.execute(
            "SELECT COUNT(*) FROM pharmgkb_annotations"
            " WHERE is_active = FALSE AND source_version_id = ?",
            [first.source_version_id],
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM pharmgkb_annotations"
            " WHERE is_active = TRUE AND source_version_id = ?",
            [second.source_version_id],
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version, is_current FROM annotation_source_versions"
            " WHERE source_db = 'pharmgkb' ORDER BY source_version_id",
        ).fetchall()

    assert inactive is not None
    assert inactive[0] == _EXPECTED_DB_ROW_COUNT
    # Trimmed v2 expansion: 1+5+1 = 7 rows.
    assert active is not None
    assert active[0] == 7
    assert version_rows == [("2026_04_15", False), ("2026_05_15", True)]


def test_refresh_force_reloads_even_when_version_matches(
    fixture_zip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_zip)
    first = pharmgkb_loader.refresh(force=False)
    second = pharmgkb_loader.refresh(force=True)

    assert first.was_already_current is False
    assert second.was_already_current is False
    # ``force=True`` with the same version label reuses the existing
    # source_version_id (``upsert_source_version`` is idempotent on
    # ``(source_db, version)``). The force path blanket-deactivates
    # every prior active PharmGKB row before re-inserting, so:
    #   * the first refresh's rows flip to is_active=FALSE,
    #   * the second refresh's rows land as is_active=TRUE,
    #   * every row carries the same source_version_id,
    #   * total active row count matches the fixture's expected count.
    assert second.source_version_id == first.source_version_id

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM pharmgkb_annotations WHERE is_active = TRUE",
        ).fetchone()
        deactivated = conn.execute(
            "SELECT COUNT(*) FROM pharmgkb_annotations WHERE is_active = FALSE",
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM pharmgkb_annotations").fetchone()
    assert active is not None
    assert active[0] == _EXPECTED_DB_ROW_COUNT
    assert deactivated is not None
    assert deactivated[0] == _EXPECTED_DB_ROW_COUNT
    assert total is not None
    assert total[0] == 2 * _EXPECTED_DB_ROW_COUNT


def test_refresh_writes_expected_column_values(
    fixture_zip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_zip)
    pharmgkb_loader.refresh(force=False)

    with duckdb_connection() as conn:
        row = conn.execute(
            """
            SELECT pgkb_accession, rsid, star_allele, gene_symbol,
                   drug_name, phenotype_category, evidence_level,
                   guideline_summary, guideline_url, chrom, pos_grch38,
                   is_active
              FROM pharmgkb_annotations
             WHERE pgkb_accession = '1001'
            """,
        ).fetchone()
    assert row is not None
    (
        pgkb,
        rsid,
        star,
        gene,
        drug,
        cat,
        evidence,
        summary,
        url,
        chrom,
        pos,
        active,
    ) = row
    assert pgkb == "1001"
    assert rsid == "rs951439"
    assert star is None
    assert gene == "RGS4"
    assert drug == "risperidone"
    assert cat == "Efficacy"
    assert evidence == "3"
    assert summary == "Phenotype(s): Schizophrenia"
    assert url == "https://www.pharmgkb.org/clinicalAnnotation/1001"
    assert chrom is None
    assert pos is None
    assert active is True


# ---------------------------------------------------------------------------
# Registry / module-import side effects
# ---------------------------------------------------------------------------


def test_get_loader_returns_pharmgkb_refresh() -> None:
    """Side-effect import wires the loader into the registry."""
    assert get_loader("pharmgkb") is pharmgkb_loader.refresh


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_refresh_pharmgkb_runs_and_prints_summary(
    fixture_zip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_zip)

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "refresh", "--source", "pharmgkb"])

    assert result.exit_code == 0, result.output
    assert "source_db=pharmgkb" in result.output
    assert "version=2026_04_15" in result.output
    assert f"records={_EXPECTED_DB_ROW_COUNT}" in result.output
    assert "already_current=False" in result.output


def test_cli_status_after_refresh_reports_pharmgkb_loaded(
    fixture_zip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_zip)
    pharmgkb_loader.refresh(force=False)

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "status"])
    assert result.exit_code == 0, result.output
    pharmgkb_lines = [line for line in result.output.splitlines() if line.startswith("pharmgkb:")]
    assert len(pharmgkb_lines) == 1
    line = pharmgkb_lines[0]
    assert "2026_04_15" in line
    assert "ingested " in line
    assert f"{_EXPECTED_DB_ROW_COUNT} records" in line


def test_cli_refresh_force_flag_passes_through_to_loader(
    fixture_zip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` flag must reach ``refresh`` (covered with a recording stub)."""
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_zip)
    pharmgkb_loader.refresh(force=False)

    received: dict[str, bool] = {}

    def _recording_refresh(force: bool) -> object:  # noqa: FBT001
        received["force"] = force
        # Return a sentinel RefreshResult so the CLI's print path can run.
        from genome.annotate.registry import RefreshResult  # noqa: PLC0415

        return RefreshResult(
            source_db="pharmgkb",
            source_version_id=42,
            version="2026_04_15",
            record_count=0,
            was_already_current=False,
        )

    monkeypatch.setattr(
        "genome.annotate.registry._LOADERS",
        {"pharmgkb": _recording_refresh},
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["annotate", "refresh", "--source", "pharmgkb", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert received == {"force": True}


# ---------------------------------------------------------------------------
# Atomicity — a failure mid-bulk-insert leaves zero rows
# ---------------------------------------------------------------------------


def test_refresh_transaction_rolls_back_on_bulk_insert_failure(
    fixture_zip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bulk-insert failure must leave both DB tables untouched.

    The download → upsert → supersede → bulk-insert sequence runs in
    one DuckDB transaction. The supersession-over-update invariant
    requires atomicity: a half-loaded refresh that records the
    annotation_source_versions row but no pharmgkb_annotations rows
    would corrupt downstream queries that count records via the
    version row.
    """
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_zip)

    boom = RuntimeError("simulated insert failure")

    def _explode(*_args: object, **_kwargs: object) -> int:
        raise boom

    monkeypatch.setattr(pharmgkb_loader, "_bulk_insert", _explode)

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        pharmgkb_loader.refresh(force=False)

    with duckdb_connection() as conn:
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'pharmgkb'",
        ).fetchone()
        annotation_rows = conn.execute(
            "SELECT COUNT(*) FROM pharmgkb_annotations",
        ).fetchone()
    # Transaction rolled back — neither table grew.
    assert version_rows is not None
    assert version_rows[0] == 0
    assert annotation_rows is not None
    assert annotation_rows[0] == 0


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_url_verified_date_is_iso_format() -> None:
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", pharmgkb_loader.URL_VERIFIED_DATE)


def test_clinical_ann_zip_url_points_at_pharmgkb_canonical() -> None:
    assert pharmgkb_loader.CLINICAL_ANN_ZIP_URL == (
        "https://api.pharmgkb.org/v1/download/file/data/clinicalAnnotations.zip"
    )


def test_source_db_label() -> None:
    assert pharmgkb_loader.SOURCE_DB == "pharmgkb"


# ---------------------------------------------------------------------------
# Idempotence check using the real (live-network) helper — guarded by skip
# ---------------------------------------------------------------------------


def test_refresh_get_current_version_lookup_uses_pharmgkb_label(
    fixture_zip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idempotence check reads the active version by source_db='pharmgkb'."""
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_zip)
    pharmgkb_loader.refresh(force=False)
    with duckdb_connection() as conn:
        current = get_current_version(conn, "pharmgkb")
    assert current is not None
    assert current.version == "2026_04_15"
    assert current.source_db == "pharmgkb"
    assert current.is_current is True
    # source_url + file_hash + file_size persisted from the download_to_cache
    # stub's payload.
    assert current.source_url == pharmgkb_loader.CLINICAL_ANN_ZIP_URL
    assert current.source_file_hash is not None
    assert current.source_file_size is not None
    assert current.source_file_size > 0
    # Idempotence path on second invocation returns the same row.
    second = pharmgkb_loader.refresh(force=False)
    assert second.source_version_id == current.source_version_id
