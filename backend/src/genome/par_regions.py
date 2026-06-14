"""GRCh38 X-chromosome pseudoautosomal region (PAR) boundaries.

PAR1 and PAR2 are the two telomeric segments of chrX that recombine with chrY
and are therefore diploid in both sexes; the non-PAR core between them is
hemizygous in males. The chrX dosage view (``consensus_chrx_dosage_v``) and the
M1 panel diploidizer both gate on this: a male non-PAR position is stored as a
homozygous-diploid R1 call whose dosage the view halves, while PAR positions
pass through as ordinary diploid.

Coordinates are 1-based GRCh38 (the project's primary build):

* PAR1: 10,001 - 2,781,479
* PAR2: 155,701,383 - 156,030,895

:func:`is_nonpar` is the strict complement ``not is_par``, so the two telomeric
slivers outside the PAR windows — ``1 - 10,000`` below PAR1 and
``156,030,896 - 156,040,895`` above PAR2 — count as non-PAR, which is
biologically correct (they are hemizygous in males). The view's non-PAR
predicate must be this same complement (``NOT (… BETWEEN PAR1 … OR … BETWEEN
PAR2 …)``), not a ``BETWEEN`` over the non-PAR core, so the two stay in lock-step
— a parity test pins them together.
"""

from __future__ import annotations

from typing import Final

PAR1_START: Final[int] = 10_001
PAR1_END: Final[int] = 2_781_479
PAR2_START: Final[int] = 155_701_383
PAR2_END: Final[int] = 156_030_895


def is_par(pos_grch38: int) -> bool:
    """Return True if a 1-based GRCh38 chrX position lies in PAR1 or PAR2."""
    return PAR1_START <= pos_grch38 <= PAR1_END or PAR2_START <= pos_grch38 <= PAR2_END


def is_nonpar(pos_grch38: int) -> bool:
    """Return True for a non-PAR chrX position — the strict complement of :func:`is_par`.

    Deliberately includes the two telomeric slivers outside the PAR windows
    (``pos < PAR1_START`` and ``pos > PAR2_END``): those are hemizygous in
    males, so treating them as non-PAR is correct, and the view's predicate
    uses the identical complement form.
    """
    return not is_par(pos_grch38)


__all__ = [
    "PAR1_END",
    "PAR1_START",
    "PAR2_END",
    "PAR2_START",
    "is_nonpar",
    "is_par",
]
