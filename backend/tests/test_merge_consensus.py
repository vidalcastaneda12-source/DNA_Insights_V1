"""Unit tests for the ``consensus_v1`` resolve rule.

These tests exercise :func:`genome.merge.consensus.resolve` directly with
hand-built :class:`VariantPair` objects, avoiding the DuckDB round-trip. The
pipeline-level test in ``test_merge_pipeline.py`` covers the SQL plumbing
and the tier-3 strand-flip cross-row rewrite.
"""

from __future__ import annotations

from genome.merge.consensus import resolve
from genome.merge.models import CallView, VariantPair


def _pair(  # noqa: PLR0913 — schema-aligned positional fields
    *,
    variant_id: int = 1,
    chrom: str = "1",
    pos: int = 100,
    ref: str = "A",
    alt: str = "G",
    twentythree: CallView | None,
    ancestry: CallView | None,
) -> VariantPair:
    return VariantPair(
        variant_id=variant_id,
        chrom=chrom,
        pos_grch38=pos,
        ref_allele=ref,
        alt_allele=alt,
        twentythree=twentythree,
        ancestry=ancestry,
    )


def _call(
    *,
    call_id: int,
    source: str,
    a1: str | None,
    a2: str | None,
    is_no_call: bool = False,
) -> CallView:
    return CallView(
        call_id=call_id,
        source=source,  # type: ignore[arg-type]
        allele_1=a1,
        allele_2=a2,
        is_no_call=is_no_call,
    )


def test_both_concordant_yields_no_discrepancy() -> None:
    pair = _pair(
        twentythree=_call(call_id=1, source="23andme", a1="A", a2="G"),
        ancestry=_call(call_id=2, source="ancestry", a1="A", a2="G"),
    )
    consensus, discrepancies = resolve(pair)
    assert consensus.consensus_method == "both_concordant"
    assert consensus.is_no_call is False
    assert (consensus.consensus_allele_1, consensus.consensus_allele_2) == ("A", "G")
    assert consensus.dosage == 1  # heterozygous A/G with alt='G'
    assert consensus.contributing_calls == (1, 2)
    assert consensus.confidence == 0.99
    assert discrepancies == []


def test_both_concordant_homozygous_alt_dosage_two() -> None:
    pair = _pair(
        twentythree=_call(call_id=1, source="23andme", a1="G", a2="G"),
        ancestry=_call(call_id=2, source="ancestry", a1="G", a2="G"),
    )
    consensus, _ = resolve(pair)
    assert consensus.dosage == 2


def test_both_concordant_homozygous_ref_dosage_zero() -> None:
    pair = _pair(
        twentythree=_call(call_id=1, source="23andme", a1="A", a2="A"),
        ancestry=_call(call_id=2, source="ancestry", a1="A", a2="A"),
    )
    consensus, _ = resolve(pair)
    assert consensus.dosage == 0


def test_genotype_mismatch_non_palindromic_unresolvable() -> None:
    """A/G vs C/G at a non-palindromic A/G site: complement of C/G = G/C → sorted (C,G) ≠ (A,G)."""
    pair = _pair(
        twentythree=_call(call_id=1, source="23andme", a1="A", a2="G"),
        ancestry=_call(call_id=2, source="ancestry", a1="C", a2="G"),
    )
    consensus, discrepancies = resolve(pair)
    assert consensus.consensus_method == "unresolvable"
    assert consensus.is_no_call is True
    assert consensus.dosage is None
    assert consensus.confidence is None
    assert len(discrepancies) == 1
    disc = discrepancies[0]
    assert disc.discrepancy_type == "genotype_mismatch"
    assert disc.severity == "major"
    assert disc.resolution == "unresolved"
    assert disc.genotype_a == "A/G"
    assert disc.genotype_b == "C/G"


def test_strand_flip_resolution_non_palindromic() -> None:
    """``A/G`` on plus strand vs ``T/C`` on minus strand — complement of T/C is A/G.

    The complement flip succeeds, so the consensus is clean
    (``disagreement_resolved``) and the discrepancy row is the audit-only
    ``strand_flip_resolved`` type at ``info`` severity — not a real mismatch.
    """
    pair = _pair(
        twentythree=_call(call_id=1, source="23andme", a1="A", a2="G"),
        ancestry=_call(call_id=2, source="ancestry", a1="C", a2="T"),  # sorted C/T
    )
    consensus, discrepancies = resolve(pair)
    assert consensus.consensus_method == "disagreement_resolved"
    assert consensus.is_no_call is False
    assert (consensus.consensus_allele_1, consensus.consensus_allele_2) == ("A", "G")
    assert consensus.dosage == 1
    assert consensus.confidence == 0.90
    assert consensus.contributing_calls == (1, 2)
    assert len(discrepancies) == 1
    disc = discrepancies[0]
    assert disc.discrepancy_type == "strand_flip_resolved"
    assert disc.severity == "info"
    assert disc.resolution == "flipped_strand_match"


def test_strand_ambiguous_palindromic_at_site() -> None:
    """A/T site with disagreement: A/A vs T/T can be either strand — unresolvable."""
    pair = _pair(
        ref="A",
        alt="T",
        twentythree=_call(call_id=1, source="23andme", a1="A", a2="A"),
        ancestry=_call(call_id=2, source="ancestry", a1="T", a2="T"),
    )
    consensus, discrepancies = resolve(pair)
    assert consensus.consensus_method == "unresolvable"
    assert consensus.is_no_call is True
    assert len(discrepancies) == 1
    disc = discrepancies[0]
    assert disc.discrepancy_type == "strand_ambiguous"
    assert disc.severity == "minor"


def test_strand_ambiguous_palindromic_cg_site() -> None:
    pair = _pair(
        ref="C",
        alt="G",
        twentythree=_call(call_id=1, source="23andme", a1="C", a2="C"),
        ancestry=_call(call_id=2, source="ancestry", a1="G", a2="G"),
    )
    _, discrepancies = resolve(pair)
    assert len(discrepancies) == 1
    assert discrepancies[0].discrepancy_type == "strand_ambiguous"


def test_no_call_diff_when_one_source_called() -> None:
    pair = _pair(
        twentythree=_call(call_id=1, source="23andme", a1="A", a2="G"),
        ancestry=_call(call_id=2, source="ancestry", a1=None, a2=None, is_no_call=True),
    )
    consensus, discrepancies = resolve(pair)
    assert consensus.consensus_method == "single_source"
    assert (consensus.consensus_allele_1, consensus.consensus_allele_2) == ("A", "G")
    assert consensus.contributing_calls == (1,)
    assert consensus.confidence == 0.75
    assert len(discrepancies) == 1
    disc = discrepancies[0]
    assert disc.discrepancy_type == "no_call_diff"
    assert disc.severity == "minor"
    assert disc.source_a == "23andme"
    assert disc.source_b == "ancestry"
    assert disc.call_b_id == 2  # the no-call's call_id is preserved for audit
    assert disc.genotype_b == "--"


def test_no_call_diff_symmetric_when_ancestry_called() -> None:
    pair = _pair(
        twentythree=_call(call_id=1, source="23andme", a1=None, a2=None, is_no_call=True),
        ancestry=_call(call_id=2, source="ancestry", a1="A", a2="G"),
    )
    consensus, discrepancies = resolve(pair)
    assert consensus.consensus_method == "single_source"
    assert consensus.contributing_calls == (2,)
    assert discrepancies[0].source_a == "ancestry"
    assert discrepancies[0].source_b == "23andme"


def test_platform_unique_only_23andme() -> None:
    pair = _pair(
        twentythree=_call(call_id=1, source="23andme", a1="A", a2="G"),
        ancestry=None,
    )
    consensus, discrepancies = resolve(pair)
    assert consensus.consensus_method == "single_source"
    assert (consensus.consensus_allele_1, consensus.consensus_allele_2) == ("A", "G")
    assert consensus.confidence == 0.85
    assert consensus.contributing_calls == (1,)
    assert len(discrepancies) == 1
    disc = discrepancies[0]
    assert disc.discrepancy_type == "platform_unique"
    assert disc.severity == "info"
    assert disc.source_a == "23andme"
    assert disc.source_b is None
    assert disc.call_b_id is None


def test_platform_unique_only_ancestry() -> None:
    pair = _pair(
        twentythree=None,
        ancestry=_call(call_id=2, source="ancestry", a1="A", a2="G"),
    )
    consensus, discrepancies = resolve(pair)
    assert consensus.consensus_method == "single_source"
    assert consensus.contributing_calls == (2,)
    assert discrepancies[0].source_a == "ancestry"


def test_both_no_call_is_concordant_no_discrepancy() -> None:
    pair = _pair(
        twentythree=_call(call_id=1, source="23andme", a1=None, a2=None, is_no_call=True),
        ancestry=_call(call_id=2, source="ancestry", a1=None, a2=None, is_no_call=True),
    )
    consensus, discrepancies = resolve(pair)
    assert consensus.consensus_method == "both_concordant"
    assert consensus.is_no_call is True
    assert consensus.dosage is None
    assert consensus.contributing_calls == (1, 2)
    assert discrepancies == []


def test_genotype_a_renders_no_call_dashes() -> None:
    """The rendered genotype on a no-call discrepancy is the schema's '--' token."""
    pair = _pair(
        twentythree=_call(call_id=1, source="23andme", a1=None, a2=None, is_no_call=True),
        ancestry=None,
    )
    _, discrepancies = resolve(pair)
    assert discrepancies[0].genotype_a == "--"


def test_no_active_calls_anywhere_is_defensive_unresolvable() -> None:
    """A variants_master row with no active calls should not crash the merge."""
    pair = _pair(twentythree=None, ancestry=None)
    consensus, discrepancies = resolve(pair)
    assert consensus.consensus_method == "unresolvable"
    assert consensus.is_no_call is True
    assert consensus.contributing_calls == ()
    assert discrepancies == []
