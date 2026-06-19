"""Tests for the region/sex-aware import gate (PR 5a / finding-031).

Two layers:

* the pure :func:`genome.imputation.ingest._variant_quality` decision — the
  dosage-confidence math, its edges, the scale guard, the fail-closed-on-missing-DS
  behavior, and the region/sex branching (incl. an ``is_nonpar``↔importer parity
  pin that mirrors the dosage-view parity test);
* the end-to-end import of a male non-PAR chrX result — counter purity (the dead
  non-PAR DR2 stays out of ``mean_r2``), the ``imputation_r2`` overload, the
  ``nonpar_dosage_conf`` quality flag, anchor retention, the shared stream/dry-run
  parity, and the fail-closed sex guard.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from genome.db import duckdb_connection, init_databases
from genome.imputation.archive import ImputationArchive
from genome.imputation.ingest import (
    DEFAULT_DCONF_THRESHOLD,
    DryRunResult,
    ImportResult,
    _variant_quality,
    import_result,
)
from genome.imputation.runs import insert_run, record_download, update_status
from genome.par_regions import PAR1_END, PAR1_START, PAR2_END, PAR2_START, is_nonpar

_NONPAR_CORE = 50_000_000


def _q(  # noqa: PLR0913 — mirrors the _variant_quality keyword surface
    *,
    chrom: str,
    pos: int,
    profile_sex: str | None,
    r2: float | None = None,
    ds: float | None = None,
    is_imputed: bool = False,
    r2_threshold: float = 0.3,
    dconf_threshold: float = DEFAULT_DCONF_THRESHOLD,
) -> tuple[bool, float | None, bool]:
    """Thin wrapper returning ``(keep, quality, is_dconf)`` for terse assertions."""
    v = _variant_quality(
        chrom=chrom,
        pos=pos,
        profile_sex=profile_sex,
        r2=r2,
        ds=ds,
        is_imputed=is_imputed,
        r2_threshold=r2_threshold,
        dconf_threshold=dconf_threshold,
    )
    return v.keep, v.quality, v.is_dconf


# ---------------------------------------------------------------------------
# Pure _variant_quality decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ds", "expected_dconf", "expected_keep"),
    [
        (1.0, 1.0, True),  # confident ALT
        (0.97, 0.97, True),  # confident ALT
        (0.9, 0.9, True),  # exactly at the 0.9 bar — kept (>=)
        (0.89, 0.89, False),  # just below the bar
        (0.6, 0.6, False),  # uncertain middle
        (0.1, 0.9, False),  # confident hom-REF imputed — dropped (high dconf, low DS)
        (0.0, 1.0, False),  # confident hom-REF imputed — dropped
    ],
)
def test_male_nonpar_imputed_keeps_only_confident_alt(
    ds: float,
    expected_dconf: float,
    expected_keep: bool,  # noqa: FBT001
) -> None:
    """An *imputed* male non-PAR site is kept iff DS >= 0.9 (informative yield)."""
    keep, quality, is_dconf = _q(
        chrom="X", pos=_NONPAR_CORE, profile_sex="M", ds=ds, is_imputed=True
    )
    assert is_dconf is True
    assert quality == pytest.approx(expected_dconf)  # stored quality is always dconf
    assert keep is expected_keep


@pytest.mark.parametrize("ds", [0.0, 1.0, 0.5, 0.97])
def test_male_nonpar_typed_anchor_always_kept(ds: float) -> None:
    """A *typed* (observed) male non-PAR anchor is kept regardless of DS direction."""
    keep, quality, is_dconf = _q(
        chrom="X", pos=_NONPAR_CORE, profile_sex="M", ds=ds, is_imputed=False
    )
    assert (keep, is_dconf) == (True, True)
    assert quality == pytest.approx(max(ds, 1.0 - ds))


def test_male_nonpar_missing_ds_fails_closed() -> None:
    """No DS on a male non-PAR row must raise, never fall back to the dead DR2 gate."""
    with pytest.raises(RuntimeError, match="no FORMAT/DS"):
        _q(chrom="X", pos=_NONPAR_CORE, profile_sex="M", r2=0.0, ds=None, is_imputed=True)


def test_male_nonpar_ds_scale_guard_raises_on_diploid_scale() -> None:
    """A DS above the 0..1 haploid scale (a re-dip seam change) must fail loudly."""
    with pytest.raises(RuntimeError, match="haploid scale"):
        _q(chrom="X", pos=_NONPAR_CORE, profile_sex="M", ds=1.5, is_imputed=True)


def test_male_nonpar_ds_at_scale_boundary_is_allowed() -> None:
    """DS exactly 1.0 (and a hair over, within epsilon) is on-scale, not a violation."""
    keep, quality, is_dconf = _q(
        chrom="X", pos=_NONPAR_CORE, profile_sex="M", ds=1.0 + 1e-6, is_imputed=True
    )
    assert is_dconf is True
    assert keep is True
    assert quality is not None


@pytest.mark.parametrize(
    ("chrom", "pos", "profile_sex"),
    [
        ("X", PAR1_START + 10, "M"),  # male PAR1 — diploid, DR2-valid
        ("X", PAR2_START + 10, "M"),  # male PAR2 — diploid, DR2-valid
        ("X", _NONPAR_CORE, "F"),  # female X — genuinely diploid
        ("X", _NONPAR_CORE, "ambiguous"),  # not a determinate male
        ("X", _NONPAR_CORE, None),  # no manifest sex
        ("1", _NONPAR_CORE, "M"),  # autosome
    ],
)
def test_non_male_nonpar_uses_dr2_gate(chrom: str, pos: int, profile_sex: str | None) -> None:
    """Everything outside male non-PAR keeps the DR2 gate; DS is ignored there."""
    # DS would *fail* the dconf gate (0.6) but is irrelevant on the DR2 branch.
    keep, quality, is_dconf = _q(chrom=chrom, pos=pos, profile_sex=profile_sex, r2=0.5, ds=0.6)
    assert is_dconf is False
    assert quality == pytest.approx(0.5)  # DR2 stored verbatim
    assert keep is True  # 0.5 >= 0.3
    # And a low DR2 drops on this same branch.
    assert _q(chrom=chrom, pos=pos, profile_sex=profile_sex, r2=0.2, ds=0.6)[0] is False


def test_dr2_gate_keeps_missing_r2() -> None:
    """A DR2-path variant with no R² value passes through (pre-existing behavior)."""
    keep, quality, is_dconf = _q(chrom="1", pos=123, profile_sex="M", r2=None, ds=None)
    assert (keep, quality, is_dconf) == (True, None, False)


@pytest.mark.parametrize(
    "pos",
    [
        1,
        10_000,
        PAR1_START,  # 10_001
        PAR1_END,  # 2_781_479
        PAR1_END + 1,
        _NONPAR_CORE,
        PAR2_START - 1,
        PAR2_START,  # 155_701_383
        PAR2_END,  # 156_030_895
        PAR2_END + 1,
        156_040_895,
    ],
)
def test_importer_gate_branch_matches_is_nonpar(pos: int) -> None:
    """The importer takes the dconf branch for a male chrX position iff ``is_nonpar``.

    The dosage view pins its non-PAR predicate to ``is_nonpar`` (test_views_chrx_dosage);
    this is the same pin on the import side, so the boundary can't drift between
    the gate that writes the call and the view that corrects its dosage.
    """
    # ds=1.0 keeps on the dconf branch; r2=0.5 keeps on the DR2 branch — so `keep`
    # is True either way and the assertion isolates the *branch* (is_dconf).
    _keep, _quality, is_dconf = _q(chrom="X", pos=pos, profile_sex="M", r2=0.5, ds=1.0)
    assert is_dconf is is_nonpar(pos)


# ---------------------------------------------------------------------------
# End-to-end import of a male non-PAR chrX result
# ---------------------------------------------------------------------------


def _seed_completed_run(archive_root: Path) -> int:
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1,),
            imputation_server="beagle",
            reference_panel="1000g_phase3_grch38",
            pipeline_version="imputation_prepare_v0.1.0",
            variants_input=100,
        )
        update_status(conn, imp_id, status="completed", set_submitted=True, set_completed=True)
        record_download(
            conn,
            imp_id,
            output_file_path="/tmp/x.zip",  # noqa: S108 — dummy path string in test data
            output_file_hash_sha256="a" * 64,
        )
    ImputationArchive.for_run(archive_root, imp_id).ensure_layout()
    return imp_id


# (pos, gt, ds, dr2, imp) — gt is the re-diploidized diploid GT the concat carries;
# ds is on the haploid 0..1 scale for non-PAR; imp marks an INFO/IMP (Beagle-imputed)
# site — typed anchors lack it; dr2 is the structurally-dead 0 on non-PAR.
_CHRX_RECORDS: tuple[tuple[int, str, float, float, bool], ...] = (
    (_NONPAR_CORE + 0, "0|0", 0.0, 0.0, False),  # typed anchor ref      — KEEP
    (_NONPAR_CORE + 1, "1|1", 1.0, 0.0, False),  # typed anchor alt      — KEEP
    (_NONPAR_CORE + 2, "1|1", 0.97, 0.0, True),  # imputed confident ALT — KEEP
    (_NONPAR_CORE + 3, "0|0", 0.02, 0.0, True),  # imputed confident REF — DROP
    (_NONPAR_CORE + 4, "0|0", 0.6, 0.0, True),  # imputed uncertain     — DROP
    (PAR1_START + 100, "0|1", 0.9, 0.7, True),  # PAR het — DR2 0.7>=0.3 — KEEP
    (PAR1_START + 101, "0|0", 0.0, 0.1, True),  # PAR — DR2 0.1<0.3      — DROP
)


def _write_chrx_ds_vcf(dest: Path) -> None:
    """Write a chrX result VCF mimicking the M3 concat: GT:DS, INFO DR2;AF;IMP."""
    header = (
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=chrX,length=156040895,assembly=GRCh38>\n"
        '##INFO=<ID=DR2,Number=A,Type=Float,Description="Dosage R-squared">\n'
        '##INFO=<ID=AF,Number=A,Type=Float,Description="Alt frequency">\n'
        '##INFO=<ID=IMP,Number=0,Type=Flag,Description="Imputed marker">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        '##FORMAT=<ID=DS,Number=A,Type=Float,Description="Dosage">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
    )
    lines = [header]
    for i, (pos, gt, ds, dr2, imp) in enumerate(_CHRX_RECORDS):
        info = f"DR2={dr2};AF=0.2" + (";IMP" if imp else "")
        lines.append(f"chrX\t{pos}\trs{1000 + i}\tA\tG\t.\tPASS\t{info}\tGT:DS\t{gt}:{ds}\n")
    with gzip.open(dest, "wt", encoding="ascii") as out:
        out.writelines(lines)


def _write_chrx_manifest(
    archive: ImputationArchive, *, profile_sex: str | None, chrx_ploidy: str | None
) -> None:
    payload: dict[str, object] = {"variants_per_chrom": {"X": len(_CHRX_RECORDS)}}
    if profile_sex is not None:
        payload["profile_sex"] = profile_sex
    if chrx_ploidy is not None:
        payload["chrx_ploidy"] = chrx_ploidy
    archive.upload_dir.mkdir(parents=True, exist_ok=True)
    archive.upload_manifest.write_text(json.dumps(payload), encoding="utf-8")


def _import_male_chrx(isolated_settings: dict[str, str]) -> ImportResult:
    init_databases()
    archive_root = Path(isolated_settings["ARCHIVE_PATH"])
    imp_id = _seed_completed_run(archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_chrx_ds_vcf(archive.result_dir / "chrX.vcf.gz")
    _write_chrx_manifest(archive, profile_sex="M", chrx_ploidy="male_nonpar_haploid")
    result = import_result(imp_id, archive_root=archive_root, chromosomes=frozenset({"X"}))
    assert isinstance(result, ImportResult)
    return result


def test_male_nonpar_import_keeps_anchors_and_confident_drops_lowconf(
    isolated_settings: dict[str, str],
) -> None:
    result = _import_male_chrx(isolated_settings)
    # Kept: 2 typed anchors + 1 confident-ALT imputed (non-PAR) + 1 PAR (DR2 0.7).
    # Dropped: confident-hom-REF imputed + uncertain imputed (non-PAR) + 1 PAR (DR2 0.1).
    assert result.variants_total == 4
    assert result.variants_below_threshold == 3
    assert result.nonpar_confident == 3
    assert result.profile_sex == "M"
    assert result.dconf_threshold == DEFAULT_DCONF_THRESHOLD


def test_male_nonpar_counter_purity_and_overload(
    isolated_settings: dict[str, str],
) -> None:
    """Dead non-PAR DR2 stays out of mean_r2; dconf lands in imputation_r2 + flag."""
    result = _import_male_chrx(isolated_settings)
    # Only the kept PAR row's DR2 (0.7) feeds the DR2 stats — the non-PAR DR2=0.0
    # never enters, so the mean is 0.7 (not dragged toward 0).
    assert result.mean_r2 == pytest.approx(0.7)
    assert result.variants_above_r2_0_3 == 1
    assert result.variants_above_r2_0_8 == 0

    with duckdb_connection() as conn:
        nonpar = conn.execute(
            """
            SELECT vm.pos_grch38, gc.imputation_r2, gc.quality_flags
              FROM genotype_calls gc
              JOIN variants_master vm ON vm.variant_id = gc.variant_id
             WHERE gc.is_active AND vm.pos_grch38 >= ? AND vm.pos_grch38 <= ?
             ORDER BY vm.pos_grch38
            """,
            [_NONPAR_CORE, _NONPAR_CORE + 2],
        ).fetchall()
        par = conn.execute(
            "SELECT imputation_r2, quality_flags FROM genotype_calls gc "
            "JOIN variants_master vm ON vm.variant_id = gc.variant_id "
            "WHERE gc.is_active AND vm.pos_grch38 = ?",
            [PAR1_START + 100],
        ).fetchone()

    # Non-PAR kept rows: imputation_r2 carries the dosage-confidence, flagged.
    # Two typed anchors (dconf 1.0) + one confident-ALT imputed (dconf 0.97).
    by_pos = {pos: (r2, flags) for pos, r2, flags in nonpar}
    assert set(by_pos) == {_NONPAR_CORE, _NONPAR_CORE + 1, _NONPAR_CORE + 2}
    assert by_pos[_NONPAR_CORE][0] == pytest.approx(1.0)  # typed ref anchor
    assert by_pos[_NONPAR_CORE + 1][0] == pytest.approx(1.0)  # typed alt anchor
    assert by_pos[_NONPAR_CORE + 2][0] == pytest.approx(0.97, abs=1e-4)  # imputed ALT
    assert all(flags == ["nonpar_dosage_conf"] for _r2, flags in by_pos.values())
    # PAR row: ordinary DR2, no dconf flag.
    assert par[0] == pytest.approx(0.7)
    assert par[1] is None


def test_male_nonpar_dry_run_matches_real_import(
    isolated_settings: dict[str, str],
) -> None:
    """The shared helper makes the dry-run kept-count equal the real import total."""
    init_databases()
    archive_root = Path(isolated_settings["ARCHIVE_PATH"])
    imp_id = _seed_completed_run(archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_chrx_ds_vcf(archive.result_dir / "chrX.vcf.gz")
    _write_chrx_manifest(archive, profile_sex="M", chrx_ploidy="male_nonpar_haploid")

    dry = import_result(
        imp_id, archive_root=archive_root, chromosomes=frozenset({"X"}), dry_run=True
    )
    assert isinstance(dry, DryRunResult)
    assert dry.profile_sex == "M"
    real = import_result(imp_id, archive_root=archive_root, chromosomes=frozenset({"X"}))
    assert isinstance(real, ImportResult)
    assert dry.variants_total == real.variants_total == 4
    assert dry.variants_below_threshold == real.variants_below_threshold == 3


def test_male_haploid_manifest_fails_closed_when_sex_not_m(
    isolated_settings: dict[str, str],
) -> None:
    """A male-haploid chrX output imported as non-male must raise, not zero non-PAR."""
    init_databases()
    archive_root = Path(isolated_settings["ARCHIVE_PATH"])
    imp_id = _seed_completed_run(archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_chrx_ds_vcf(archive.result_dir / "chrX.vcf.gz")
    # chrx_ploidy says male-haploid but profile_sex is absent → resolves to None.
    _write_chrx_manifest(archive, profile_sex=None, chrx_ploidy="male_nonpar_haploid")

    with pytest.raises(RuntimeError, match="male_nonpar_haploid"):
        import_result(imp_id, archive_root=archive_root, chromosomes=frozenset({"X"}))

    # An explicit --sex M override unblocks it.
    result = import_result(imp_id, archive_root=archive_root, chromosomes=frozenset({"X"}), sex="M")
    assert isinstance(result, ImportResult)
    assert result.nonpar_confident == 3


def test_male_haploid_fail_closed_skipped_when_chrx_excluded(
    isolated_settings: dict[str, str],
) -> None:
    """The sex guard only fires when chrX is actually in scope."""
    init_databases()
    archive_root = Path(isolated_settings["ARCHIVE_PATH"])
    imp_id = _seed_completed_run(archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    # A non-chrX result with the (irrelevant here) male-haploid manifest marker.
    chr1 = archive.result_dir / "chr1.vcf.gz"
    with gzip.open(chr1, "wt", encoding="ascii") as out:
        out.write(
            "##fileformat=VCFv4.2\n"
            "##contig=<ID=chr1,length=248956422>\n"
            '##INFO=<ID=DR2,Number=A,Type=Float,Description="r2">\n'
            '##FORMAT=<ID=GT,Number=1,Type=String,Description="GT">\n'
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
            "chr1\t1000\trs1\tA\tG\t.\tPASS\tDR2=0.9\tGT\t0/1\n"
        )
    _write_chrx_manifest(archive, profile_sex=None, chrx_ploidy="male_nonpar_haploid")
    # variants_per_chrom only mentions X; add chr1 so the empty guard is satisfied.
    archive.upload_manifest.write_text(
        json.dumps(
            {
                "variants_per_chrom": {"1": 1},
                "chrx_ploidy": "male_nonpar_haploid",
            }
        ),
        encoding="utf-8",
    )
    result = import_result(imp_id, archive_root=archive_root, chromosomes=frozenset({"1"}))
    assert isinstance(result, ImportResult)
    assert result.variants_total == 1
