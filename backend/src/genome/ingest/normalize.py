"""Normalize raw calls into biallelic, GRCh38, strand-flagged ``NormalizedCall`` rows.

Without a reference panel (loaded in phase 5) we cannot truly identify which
observed allele is the reference and which is the alt. We use a deterministic
rule — alphabetical order — so re-ingesting the same file produces identical
``(chrom, pos, ref, alt)`` keys, which is what the dedup constraint needs.
Phase 3's merge step joins by rsID and (chrom, pos) when ref/alt disagree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.ingest.models import (
    LiftoverStatus,
    NormalizedCall,
    RawCall,
    StrandStatus,
    VariantType,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from genome.ingest.liftover import Liftover

_PALINDROME_PAIRS: frozenset[frozenset[str]] = frozenset(
    {frozenset({"A", "T"}), frozenset({"C", "G"})},
)
_INDEL_TOKENS: frozenset[str] = frozenset({"I", "D"})
_DNA_TOKENS: frozenset[str] = frozenset({"A", "C", "G", "T"})


def classify_palindrome(allele_1: str, allele_2: str) -> bool:
    """Return ``True`` when ``{allele_1, allele_2}`` is ``{A, T}`` or ``{C, G}``."""
    if allele_1 == allele_2:
        return False
    return frozenset({allele_1, allele_2}) in _PALINDROME_PAIRS


def classify_variant_type(allele_1: str, allele_2: str) -> VariantType:
    """Coarse variant-type label from the observed allele pair."""
    s = {allele_1, allele_2}
    if s & _INDEL_TOKENS:
        return "INDEL"
    if s <= _DNA_TOKENS:
        return "SNV"
    # Fall-through: mixed N/IUPAC. Treat as SNV — Phase 5 annotation refines it.
    return "SNV"


def order_alleles(allele_1: str, allele_2: str) -> tuple[str, str]:
    """Return ``(ref, alt)`` using alphabetical order on the observed alleles.

    For a homozygous call (``A/A``) ``ref == alt``. The merge step in Phase 3
    re-keys against the loaded reference once we have one.
    """
    if allele_1 <= allele_2:
        return (allele_1, allele_2)
    return (allele_2, allele_1)


def _strand_for(call: RawCall, variant_type: VariantType) -> StrandStatus:
    if call.is_no_call:
        return "unknown"
    if variant_type == "INDEL":
        return "unknown"
    if classify_palindrome(call.allele_1, call.allele_2):
        return "ambiguous_palindrome"
    return "resolved_plus"


def _liftover_status(
    native_build: str,
    lifted: tuple[str, int] | None,
    expected_chrom: str,
) -> LiftoverStatus:
    if native_build == "GRCh38":
        return "native_grch38"
    if lifted is None:
        return "lift_failed"
    if lifted[0] != expected_chrom:
        return "lifted_with_warning"
    return "lifted_ok"


def normalize_calls(
    calls: Iterable[RawCall],
    *,
    native_build: str,
    liftover: Liftover,
) -> Iterator[NormalizedCall]:
    """Stream-normalize raw calls into ``NormalizedCall`` rows.

    Drops rows whose lift-over fails outright (tracked via
    ``quality_flags`` / counts on the writer side); palindromic SNVs are kept
    with ``strand_status='ambiguous_palindrome'`` so the merge step can decide
    what to do.
    """
    for call in calls:
        variant_type = classify_variant_type(call.allele_1, call.allele_2)
        if call.is_no_call:
            # No-call: still emit so we can track call_rate; allele fields blank.
            ref, alt = ("N", "N")
        else:
            ref, alt = order_alleles(call.allele_1, call.allele_2)

        if native_build == "GRCh38":
            pos_grch38 = call.pos
            pos_grch37: int | None = None
            chain = liftover.chain_label
            status: LiftoverStatus = "native_grch38"
        else:
            lifted = liftover.lift(call.chrom, call.pos)
            chain = liftover.chain_label
            status = _liftover_status(native_build, lifted, call.chrom)
            if status == "lift_failed":
                # Skip the row; the writer counts these via quality_flag stats
                # we surface back through the aggregate. Yielding a row with no
                # pos_grch38 would violate the schema (NOT NULL).
                continue
            assert lifted is not None  # noqa: S101 — narrowed by status check
            pos_grch38 = lifted[1]
            pos_grch37 = call.pos
            # If the lift moved us to a different chromosome, follow it; the
            # call.chrom we keep as the post-lift chromosome since variant
            # identity lives in GRCh38 space.
            chrom_post = lifted[0]
            yield NormalizedCall(
                rsid=call.rsid,
                chrom=chrom_post,
                pos_grch38=pos_grch38,
                pos_grch37=pos_grch37,
                ref_allele=ref,
                alt_allele=alt,
                variant_type=variant_type,
                allele_1=call.allele_1,
                allele_2=call.allele_2,
                is_no_call=call.is_no_call,
                strand_status=_strand_for(call, variant_type),
                liftover_chain=chain,
                liftover_status=status,
                quality_flags=(),
            )
            continue

        yield NormalizedCall(
            rsid=call.rsid,
            chrom=call.chrom,
            pos_grch38=pos_grch38,
            pos_grch37=pos_grch37,
            ref_allele=ref,
            alt_allele=alt,
            variant_type=variant_type,
            allele_1=call.allele_1,
            allele_2=call.allele_2,
            is_no_call=call.is_no_call,
            strand_status=_strand_for(call, variant_type),
            liftover_chain=chain,
            liftover_status=status,
            quality_flags=(),
        )
