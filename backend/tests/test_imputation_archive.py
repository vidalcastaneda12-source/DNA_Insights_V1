"""Tests for :mod:`genome.imputation.archive` — per-run directory layout."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from genome.imputation.archive import ImputationArchive, restrict_file

if TYPE_CHECKING:
    from pathlib import Path


def test_for_run_builds_zero_padded_root(tmp_path: Path) -> None:
    a = ImputationArchive.for_run(tmp_path, 7)
    assert a.root == tmp_path / "imputation" / "run_0007"


def test_for_run_padding_extends_for_large_ids(tmp_path: Path) -> None:
    a = ImputationArchive.for_run(tmp_path, 99_999)
    # 5+ digit ids do not truncate — the format spec is ``:04d``, which pads
    # but does not cap.
    assert a.root.name == "run_99999"


def test_ensure_layout_creates_subdirs_with_0700(tmp_path: Path) -> None:
    a = ImputationArchive.for_run(tmp_path, 1)
    a.ensure_layout()
    for d in (a.root, a.upload_dir, a.result_dir):
        assert d.is_dir()
        mode = stat.S_IMODE(d.stat().st_mode)
        assert mode == 0o700, f"{d} mode is {oct(mode)}, expected 0o700"


def test_ensure_layout_is_idempotent(tmp_path: Path) -> None:
    a = ImputationArchive.for_run(tmp_path, 1)
    a.ensure_layout()
    a.ensure_layout()  # second call is a no-op
    assert a.root.is_dir()


def test_upload_vcf_path_uses_chr_prefix(tmp_path: Path) -> None:
    a = ImputationArchive.for_run(tmp_path, 1)
    assert a.upload_vcf_path("1").name == "chr1.vcf.gz"
    assert a.upload_vcf_path("X").name == "chrX.vcf.gz"
    assert a.upload_vcf_path("MT").name == "chrMT.vcf.gz"


def test_list_upload_vcfs_returns_sorted_files(tmp_path: Path) -> None:
    a = ImputationArchive.for_run(tmp_path, 1)
    a.ensure_layout()
    # Write empty placeholders.
    for c in ("10", "2", "X"):
        a.upload_vcf_path(c).write_bytes(b"placeholder")
    files = a.list_upload_vcfs()
    # Alphabetical sort yields chr10, chr2, chrX (ascii sort).
    assert [f.name for f in files] == ["chr10.vcf.gz", "chr2.vcf.gz", "chrX.vcf.gz"]


def test_list_upload_vcfs_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    a = ImputationArchive.for_run(tmp_path, 999)
    assert a.list_upload_vcfs() == []


def test_list_result_vcfs_only_matches_dose_pattern(tmp_path: Path) -> None:
    a = ImputationArchive.for_run(tmp_path, 1)
    a.ensure_layout()
    (a.result_dir / "chr1.dose.vcf.gz").write_bytes(b"x")
    (a.result_dir / "chr1.info.gz").write_bytes(b"x")  # info file, not dose
    (a.result_dir / "topmed_result.zip").write_bytes(b"x")
    files = a.list_result_vcfs()
    assert [f.name for f in files] == ["chr1.dose.vcf.gz"]


def test_restrict_file_chmods_to_0600(tmp_path: Path) -> None:
    p = tmp_path / "f"
    p.write_text("x")
    p.chmod(0o644)
    restrict_file(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_restrict_file_silently_ignores_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    # Should not raise.
    restrict_file(missing)
