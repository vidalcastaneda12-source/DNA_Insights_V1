"""Tests for the chrX non-PAR LOO harness (PR 5a / finding-031).

Covers the unit-testable core that the named long-op composes — the fold
partition, the per-anchor scoring properties, the stratified report aggregation,
and the gzip-VCF mask/read helpers — on synthetic fixtures with known truth. The
Beagle orchestration (:func:`genome.imputation.chrx_loo.run_chrx_loo`) needs the
real panel and is exercised by the real-data gate, not here.
"""

from __future__ import annotations

import gzip
from typing import TYPE_CHECKING

import pytest

from genome.imputation.chrx_loo import (
    LooAnchorResult,
    compute_loo_report,
    partition_folds,
    read_haploid_anchors,
    read_imputed_calls,
    write_masked_target,
)

if TYPE_CHECKING:
    from pathlib import Path

_NONPAR = 50_000_000


# ---------------------------------------------------------------------------
# partition_folds
# ---------------------------------------------------------------------------


def test_partition_folds_is_disjoint_and_total() -> None:
    positions = [10, 20, 30, 40, 50, 60, 70]
    folds = partition_folds(positions, 5)
    assert len(folds) == 5
    # Each position appears exactly once across the folds.
    union: set[int] = set()
    for f in folds:
        assert union.isdisjoint(f)
        union |= f
    assert union == set(positions)


def test_partition_folds_is_deterministic() -> None:
    positions = [70, 10, 50, 30, 90, 20]
    assert partition_folds(positions, 3) == partition_folds(positions, 3)


def test_partition_folds_rejects_too_few_folds() -> None:
    with pytest.raises(ValueError, match="n_folds must be"):
        partition_folds([1, 2, 3], 1)


# ---------------------------------------------------------------------------
# LooAnchorResult scoring properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ds", "dconf", "call"),
    [
        (0.97, 0.97, 1),
        (0.03, 0.97, 0),
        (1.0, 1.0, 1),
        (0.0, 1.0, 0),
        (0.5, 0.5, 1),  # midpoint rounds up
        (0.49, 0.51, 0),
    ],
)
def test_anchor_dconf_and_call(ds: float, dconf: float, call: int) -> None:
    r = LooAnchorResult(pos=1, truth=call, imputed_dosage=ds, maf=0.2)
    assert r.dosage_confidence == pytest.approx(dconf)
    assert r.imputed_call == call
    assert r.concordant is True  # truth set to the rounded call above


def test_anchor_discordant() -> None:
    r = LooAnchorResult(pos=1, truth=0, imputed_dosage=0.95, maf=0.2)
    assert r.imputed_call == 1
    assert r.concordant is False


# ---------------------------------------------------------------------------
# compute_loo_report
# ---------------------------------------------------------------------------


def _sample_results() -> list[LooAnchorResult]:
    # The gate keeps an imputed call iff imputed_dosage >= threshold (confident
    # ALT); LOO measures the precision of that kept set.
    return [
        LooAnchorResult(pos=1, truth=1, imputed_dosage=0.97, maf=0.30),  # kept, true ALT
        LooAnchorResult(pos=2, truth=1, imputed_dosage=0.92, maf=None),  # kept, true ALT (maf na)
        LooAnchorResult(pos=3, truth=0, imputed_dosage=0.95, maf=0.20),  # kept, FALSE ALT
        LooAnchorResult(pos=4, truth=0, imputed_dosage=0.02, maf=0.40),  # dropped: hom-REF
        LooAnchorResult(pos=5, truth=1, imputed_dosage=0.60, maf=0.05),  # dropped: uncertain
    ]


def test_report_overall_concordance_at_threshold() -> None:
    report = compute_loo_report(_sample_results(), threshold=0.9)
    # Gate-kept (ds >= 0.9): pos1/pos2 true ALT (conc), pos3 false ALT (disc).
    assert report.n_anchors == 5
    assert report.n_at_or_above_threshold == 3
    assert report.n_concordant_at_threshold == 2
    assert report.concordance == pytest.approx(2 / 3)


def test_report_threshold_is_a_strict_filter() -> None:
    # At 0.96 only pos1 (0.97) survives → 1/1 precision; the false-ALT pos3 (0.95)
    # and pos2 (0.92) drop out.
    report = compute_loo_report(_sample_results(), threshold=0.96)
    assert report.n_at_or_above_threshold == 1
    assert report.concordance == pytest.approx(1.0)


def test_report_cells_cover_gate_kept_set() -> None:
    report = compute_loo_report(_sample_results(), threshold=0.9)
    cells = {(c.maf_bin, c.conf_bin): c for c in report.cells}
    # Common (MAF >= 0.05), high-confidence cell: pos1 (conc) + pos3 (disc).
    common_hi = cells[("0.05-0.50", "0.95-0.99")]
    assert common_hi.n == 2
    assert common_hi.concordant == 1
    # The maf=None kept anchor lands in the 'na' MAF bin.
    na_cell = cells[("na", "0.90-0.95")]
    assert na_cell.n == 1
    assert na_cell.concordant == 1
    # Cells cover exactly the gate-kept set (ds >= threshold), not all anchors.
    assert sum(c.n for c in report.cells) == report.n_at_or_above_threshold == 3


def test_report_empty_results() -> None:
    report = compute_loo_report([], threshold=0.9)
    assert report.n_anchors == 0
    assert report.n_at_or_above_threshold == 0
    assert report.concordance is None
    assert report.cells == ()


def test_report_to_dict_round_trips_key_fields() -> None:
    report = compute_loo_report(_sample_results(), threshold=0.9)
    d = report.to_dict()
    assert d["n_anchors"] == 5
    assert d["dconf_threshold"] == 0.9
    assert d["concordance_at_threshold"] == pytest.approx(2 / 3)
    assert isinstance(d["cells"], list)


# ---------------------------------------------------------------------------
# gzip-VCF helpers
# ---------------------------------------------------------------------------

_TARGET_HEADER = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=chrX,length=156040895,assembly=GRCh38>\n"
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
)


def _write_target(path: Path) -> None:
    """A male non-PAR haploid target: two real anchors, one no-call, one indel."""
    rows = [
        f"chrX\t{_NONPAR}\trs1\tA\tG\t.\tPASS\t.\tGT\t1\n",
        f"chrX\t{_NONPAR + 1}\trs2\tA\tG\t.\tPASS\t.\tGT\t0\n",
        f"chrX\t{_NONPAR + 2}\trs3\tA\tG\t.\tPASS\t.\tGT\t.\n",  # no-call → not an anchor
        f"chrX\t{_NONPAR + 3}\trs4\tAT\tG\t.\tPASS\t.\tGT\t1\n",  # not a SNV → skipped
    ]
    with gzip.open(path, "wt", encoding="ascii") as out:
        out.write(_TARGET_HEADER)
        out.writelines(rows)


def test_read_haploid_anchors_keeps_only_called_biallelic_snvs(tmp_path: Path) -> None:
    target = tmp_path / "nonpar.vcf.gz"
    _write_target(target)
    anchors = read_haploid_anchors(target)
    assert anchors == {_NONPAR: 1, _NONPAR + 1: 0}


def test_write_masked_target_sets_fold_to_missing(tmp_path: Path) -> None:
    target = tmp_path / "nonpar.vcf.gz"
    masked = tmp_path / "fold.vcf.gz"
    _write_target(target)

    n = write_masked_target(target, masked, frozenset({_NONPAR}))
    assert n == 1

    # The masked anchor is now a no-call (dropped from anchors); the other survives.
    assert read_haploid_anchors(masked) == {_NONPAR + 1: 0}
    # Header is preserved verbatim and the masked record carries a '.' sample.
    with gzip.open(masked, "rt", encoding="ascii") as fh:
        text = fh.read()
    assert "##fileformat=VCFv4.2" in text
    assert f"chrX\t{_NONPAR}\trs1\tA\tG\t.\tPASS\t.\tGT\t.\n" in text


def test_read_imputed_calls_pairs_ds_af_with_truth(tmp_path: Path) -> None:
    result = tmp_path / "fold_out.vcf.gz"
    header = (
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=chrX,length=156040895,assembly=GRCh38>\n"
        '##INFO=<ID=AF,Number=A,Type=Float,Description="Alt frequency">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        '##FORMAT=<ID=DS,Number=A,Type=Float,Description="Dosage">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
    )
    rows = [
        f"chrX\t{_NONPAR}\t.\tA\tG\t.\tPASS\tAF=0.30\tGT:DS\t1:0.97\n",  # masked → read
        f"chrX\t{_NONPAR + 1}\t.\tA\tG\t.\tPASS\tAF=0.10\tGT:DS\t0:0.02\n",  # not masked → skip
    ]
    with gzip.open(result, "wt", encoding="ascii") as out:
        out.write(header)
        out.writelines(rows)

    anchors_truth = {_NONPAR: 1, _NONPAR + 1: 0}
    calls = read_imputed_calls(result, anchors_truth, frozenset({_NONPAR}))
    assert len(calls) == 1
    call = calls[0]
    assert call.pos == _NONPAR
    assert call.truth == 1
    assert call.imputed_dosage == pytest.approx(0.97, abs=1e-4)
    assert call.maf == pytest.approx(0.30, abs=1e-4)
    assert call.dosage_confidence == pytest.approx(0.97, abs=1e-4)
    assert call.concordant is True
