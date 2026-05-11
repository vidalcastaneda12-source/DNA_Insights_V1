"""Strand resolution helpers for the Phase 3 merge step.

Two questions arise during merge:

1. **Is the site palindromic?** A SNV whose two alleles are an A/T or C/G pair
   reads identically on the plus and minus strands — there is no way to tell
   from genotype alone whether two sources reporting different alleles
   disagree biologically or simply differ in strand convention. Palindromic
   disagreements are flagged ``strand_ambiguous`` and held as no-calls.

2. **Does the complement of one source's call match the other?** For a
   non-palindromic site where the raw allele pairs differ, taking the
   complement of one side often resolves them — the underlying genotype is
   the same, only the strand convention differed. The consensus is recorded
   with ``consensus_method = 'disagreement_resolved'`` and the discrepancy
   carries ``resolution = 'flipped_strand_match'``.

Both helpers operate on the canonical single-base DNA tokens we produce in
``normalize.order_alleles``; ``N`` / indel tokens are treated as their own
complement (the merge step does not attempt strand resolution for them).
"""

from __future__ import annotations

from typing import Final

_COMPLEMENT: Final[dict[str, str]] = {
    "A": "T",
    "T": "A",
    "C": "G",
    "G": "C",
}


def complement(allele: str) -> str:
    """Return the Watson-Crick complement of a single-base allele.

    Non-DNA tokens (``N``, ``I``, ``D``, empty) are returned unchanged so the
    caller can fall back to "no flip possible" without a special case.
    """
    return _COMPLEMENT.get(allele, allele)


def is_palindromic_site(ref_allele: str, alt_allele: str) -> bool:
    """Return ``True`` when ``{ref, alt}`` is ``{A, T}`` or ``{C, G}``.

    These sites read identically on the plus and minus strands, so a
    disagreement between sources cannot be resolved from genotype alone.
    """
    if ref_allele == alt_allele:
        return False
    pair = frozenset({ref_allele, alt_allele})
    return pair in (frozenset({"A", "T"}), frozenset({"C", "G"}))


def sorted_pair(allele_1: str, allele_2: str) -> tuple[str, str]:
    """Return the alphabetically-ordered ``(a, b)`` allele pair.

    Mirrors :func:`genome.ingest.normalize.order_alleles` so post-flip
    comparisons line up with the order the writer used at ingest time.
    """
    if allele_1 <= allele_2:
        return (allele_1, allele_2)
    return (allele_2, allele_1)


def complement_pair(allele_1: str, allele_2: str) -> tuple[str, str]:
    """Return the complement of ``(allele_1, allele_2)`` re-sorted alphabetically.

    Used to check whether one source's call matches the other after a strand
    flip: ``complement_pair(a, b) == sorted_pair(x, y)`` ⇒ the flip resolves
    the disagreement.
    """
    return sorted_pair(complement(allele_1), complement(allele_2))
