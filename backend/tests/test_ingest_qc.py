"""Sample-QC computation."""

from __future__ import annotations

from decimal import Decimal

from genome.ingest.models import NormalizedCall
from genome.ingest.qc import compute_sample_qc


def _make_call(
    *,
    chrom: str = "1",
    pos: int = 1,
    a1: str = "A",
    a2: str = "G",
    is_no_call: bool = False,
) -> NormalizedCall:
    return NormalizedCall(
        rsid=None,
        chrom=chrom,
        pos_grch38=pos,
        pos_grch37=None,
        ref_allele="A",
        alt_allele="G",
        variant_type="SNV",
        allele_1=a1,
        allele_2=a2,
        is_no_call=is_no_call,
        strand_status="resolved_plus",
        liftover_chain="native_grch38",
        liftover_status="native_grch38",
        quality_flags=(),
    )


def test_compute_sample_qc_call_rate_and_het():
    # 8 autosomal calls, 1 no-call, half het.
    calls = (
        [
            _make_call(pos=i, a1="A", a2="G")
            for i in range(4)  # het
        ]
        + [
            _make_call(pos=10 + i, a1="A", a2="A")
            for i in range(4)  # hom
        ]
        + [_make_call(pos=99, is_no_call=True)]
    )

    qc = compute_sample_qc(calls)
    assert qc.variants_total == 9
    assert qc.variants_called == 8
    assert qc.variants_no_call == 1
    assert qc.call_rate == Decimal("0.8889")
    assert qc.heterozygosity_rate == Decimal("0.5000")
    assert qc.qc_status == "fail"  # under 0.90 warn threshold


def test_compute_sample_qc_pass_status():
    calls = [_make_call(pos=i, a1="A", a2="A") for i in range(100)]
    qc = compute_sample_qc(calls)
    assert qc.call_rate == Decimal("1.0000")
    assert qc.qc_status == "pass"


def test_compute_sample_qc_male_inferred():
    calls = (
        # lots of autosomal het + hom for a baseline call_rate
        [_make_call(pos=i, a1="A", a2="G") for i in range(40)]
        + [_make_call(pos=200 + i, a1="A", a2="A") for i in range(60)]
        # X mostly homozygous, Y has called variants
        + [_make_call(chrom="X", pos=300 + i, a1="A", a2="A") for i in range(20)]
        + [_make_call(chrom="Y", pos=400 + i, a1="A", a2="A") for i in range(10)]
    )
    qc = compute_sample_qc(calls)
    assert qc.sex_inferred == "M"


def test_compute_sample_qc_female_inferred():
    calls = (
        [_make_call(pos=i, a1="A", a2="A") for i in range(100)]
        # X with high het rate, no Y calls
        + [_make_call(chrom="X", pos=300 + i, a1="A", a2="G") for i in range(15)]
        + [_make_call(chrom="X", pos=400 + i, a1="A", a2="A") for i in range(5)]
    )
    qc = compute_sample_qc(calls)
    assert qc.sex_inferred == "F"


def test_compute_sample_qc_ambiguous_default():
    calls = [_make_call(pos=i, a1="A", a2="A") for i in range(10)]
    qc = compute_sample_qc(calls)
    # No X / Y data → cannot decide → ambiguous, status downgrades to warn for the note.
    assert qc.sex_inferred == "ambiguous"


def test_compute_sample_qc_handles_empty_input():
    qc = compute_sample_qc([])
    assert qc.variants_total == 0
    assert qc.call_rate == Decimal("0.0000")
    assert qc.qc_status == "fail"
