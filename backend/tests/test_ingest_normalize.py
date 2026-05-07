"""Strand resolution, palindrome flagging, and lift-over handling."""

from __future__ import annotations

from dataclasses import replace

import pytest

from genome.ingest.liftover import IdentityLiftover, make_liftover
from genome.ingest.models import RawCall
from genome.ingest.normalize import (
    classify_palindrome,
    classify_variant_type,
    normalize_calls,
    order_alleles,
)


def _raw(**overrides) -> RawCall:
    base = RawCall(
        rsid="rs1",
        chrom="1",
        pos=100,
        allele_1="A",
        allele_2="G",
        is_no_call=False,
    )
    return replace(base, **overrides)


def test_classify_palindrome():
    assert classify_palindrome("A", "T") is True
    assert classify_palindrome("T", "A") is True
    assert classify_palindrome("C", "G") is True
    assert classify_palindrome("G", "C") is True
    assert classify_palindrome("A", "G") is False
    assert classify_palindrome("A", "A") is False  # homozygous, not a palindromic pair
    assert classify_palindrome("C", "T") is False


def test_classify_variant_type():
    assert classify_variant_type("A", "G") == "SNV"
    assert classify_variant_type("A", "A") == "SNV"
    assert classify_variant_type("I", "I") == "INDEL"
    assert classify_variant_type("D", "I") == "INDEL"


def test_order_alleles_alphabetical():
    assert order_alleles("G", "A") == ("A", "G")
    assert order_alleles("A", "G") == ("A", "G")
    assert order_alleles("T", "T") == ("T", "T")


def test_normalize_native_grch38_passthrough():
    out = list(
        normalize_calls(
            [_raw()],
            native_build="GRCh38",
            liftover=IdentityLiftover(chain_label="native_grch38"),
        ),
    )
    assert len(out) == 1
    n = out[0]
    assert n.pos_grch38 == 100
    assert n.pos_grch37 is None
    assert n.liftover_status == "native_grch38"
    assert n.strand_status == "resolved_plus"
    # Alleles ordered: A then G.
    assert (n.ref_allele, n.alt_allele) == ("A", "G")


def test_normalize_grch37_uses_identity_liftover_and_keeps_old_pos():
    out = list(
        normalize_calls(
            [_raw(chrom="1", pos=12345)],
            native_build="GRCh37",
            liftover=IdentityLiftover(chain_label="hg19_to_hg38"),
        ),
    )
    n = out[0]
    assert n.pos_grch38 == 12345
    assert n.pos_grch37 == 12345
    assert n.liftover_chain == "hg19_to_hg38"
    assert n.liftover_status == "lifted_ok"


def test_normalize_no_call_yields_n_alleles_and_unknown_strand():
    raw = _raw(allele_1="", allele_2="", is_no_call=True)
    out = list(
        normalize_calls([raw], native_build="GRCh38", liftover=IdentityLiftover()),
    )
    n = out[0]
    assert n.is_no_call is True
    assert (n.ref_allele, n.alt_allele) == ("N", "N")
    assert n.strand_status == "unknown"


def test_normalize_palindrome_flagged():
    raw = _raw(allele_1="A", allele_2="T")
    out = list(
        normalize_calls([raw], native_build="GRCh38", liftover=IdentityLiftover()),
    )
    assert out[0].strand_status == "ambiguous_palindrome"


def test_normalize_indel_strand_unknown():
    raw = _raw(allele_1="I", allele_2="I")
    out = list(
        normalize_calls([raw], native_build="GRCh38", liftover=IdentityLiftover()),
    )
    n = out[0]
    assert n.variant_type == "INDEL"
    assert n.strand_status == "unknown"


class _FailingLiftover:
    chain_label = "fail_chain"

    def lift(self, chrom: str, pos: int) -> tuple[str, int] | None:  # noqa: ARG002
        return None


def test_normalize_drops_lift_failures():
    out = list(
        normalize_calls(
            [_raw(chrom="1", pos=999)],
            native_build="GRCh37",
            liftover=_FailingLiftover(),
        ),
    )
    assert out == []


class _CrossChromLiftover:
    chain_label = "weird"

    def lift(self, chrom: str, pos: int) -> tuple[str, int]:  # noqa: ARG002
        return ("2", pos + 1000)


def test_normalize_lift_to_other_chrom_marked_with_warning():
    out = list(
        normalize_calls(
            [_raw(chrom="1", pos=500)],
            native_build="GRCh37",
            liftover=_CrossChromLiftover(),
        ),
    )
    n = out[0]
    assert n.chrom == "2"
    assert n.pos_grch38 == 1500
    assert n.pos_grch37 == 500
    assert n.liftover_status == "lifted_with_warning"


def test_make_liftover_grch37_requires_chain_file():
    with pytest.raises(ValueError, match="chain file"):
        make_liftover("GRCh37")


def test_make_liftover_grch38_returns_identity():
    lo = make_liftover("GRCh38")
    assert lo.chain_label == "native_grch38"
    assert lo.lift("1", 42) == ("1", 42)
