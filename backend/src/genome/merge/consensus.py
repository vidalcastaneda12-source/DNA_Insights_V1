"""``consensus_v1`` — the rule that resolves one variant into one consensus call.

The rule is intentionally simple and intentionally versioned. Every consensus
row stamps ``resolution_rule = 'consensus_v1'`` so a later session can rebuild
history when the rule changes. See ``docs/consensus.md`` for the prose version.

Phase 3 introduced the chip-only branches (``23andme`` + ``ancestry``). Phase 4
extends the same rule in place to handle ``beagle_imputed`` calls: imputation
is treated as confirming evidence that appends to ``contributing_calls`` when
a chip call is present, and as the sole evidence (``imputed_only``) when no
chip call is present at the variant. The rule label remains ``consensus_v1``
— no version bump — because the chip-only resolutions are unchanged
byte-for-byte.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from genome.merge.models import (
    MERGE_VERSION,
    CallView,
    ConsensusRow,
    DiscrepancyRow,
)
from genome.merge.strand import (
    complement_pair,
    is_palindromic_site,
    sorted_pair,
)

if TYPE_CHECKING:
    from genome.merge.models import (
        ConsensusMethod,
        DiscrepancyType,
        Severity,
        Source,
        VariantPair,
    )

# Phase-3 confidence anchors. These are deliberate placeholders — the
# evidence-weighted rollup arrives in Phase 7. They are surfaced here so the
# discrepancy dashboard has *something* monotonic to sort by today.
_CONF_BOTH_CONCORDANT: Final[float] = 0.99
_CONF_STRAND_FLIP_RESOLVED: Final[float] = 0.90
_CONF_SINGLE_SOURCE: Final[float] = 0.85
_CONF_SINGLE_NO_CALL_DIFF: Final[float] = 0.75


def _render_genotype(call: CallView | None) -> str | None:
    """Return ``'A/G'`` style for a call, ``'--'`` for no-call, ``None`` if absent."""
    if call is None:
        return None
    if call.is_no_call:
        return "--"
    return f"{call.allele_1 or ''}/{call.allele_2 or ''}"


def _dosage(
    allele_1: str | None,
    allele_2: str | None,
    alt_allele: str,
    *,
    is_no_call: bool,
) -> int | None:
    """Count ALT-matching alleles for a biallelic consensus call.

    ``None`` for no-call rows. The ALT label here is the alphabetically-ordered
    pseudo-ALT we assign at ingest, not the real reference panel ALT — that
    reconciliation happens in Phase 5 once VEP / dbSNP land.
    """
    if is_no_call or allele_1 is None or allele_2 is None:
        return None
    count = 0
    if allele_1 == alt_allele:
        count += 1
    if allele_2 == alt_allele:
        count += 1
    return count


def _consensus(  # noqa: PLR0913 — every arg comes straight off the schema
    *,
    variant_id: int,
    a1: str | None,
    a2: str | None,
    is_no_call: bool,
    alt_allele: str,
    method: ConsensusMethod,
    contributing: tuple[int, ...],
    confidence: float | None,
    is_imputed: bool = False,
    consensus_r2: float | None = None,
) -> ConsensusRow:
    return ConsensusRow(
        variant_id=variant_id,
        consensus_allele_1=None if is_no_call else a1,
        consensus_allele_2=None if is_no_call else a2,
        is_no_call=is_no_call,
        dosage=_dosage(a1, a2, alt_allele, is_no_call=is_no_call),
        consensus_method=method,
        is_imputed=is_imputed,
        consensus_r2=consensus_r2,
        contributing_calls=contributing,
        resolution_rule=MERGE_VERSION,
        confidence=confidence,
    )


def _append_imputed_call(
    consensus: ConsensusRow,
    imputed_call_id: int,
) -> ConsensusRow:
    """Return a new ConsensusRow with ``imputed_call_id`` appended to ``contributing_calls``.

    ``ConsensusRow`` is frozen, so this returns a fresh instance. The chip
    consensus's method, alleles, dosage, and ``is_imputed=False`` are
    preserved exactly — imputation adds confidence as confirming evidence
    only, never overrides chip-source resolution.
    """
    return ConsensusRow(
        variant_id=consensus.variant_id,
        consensus_allele_1=consensus.consensus_allele_1,
        consensus_allele_2=consensus.consensus_allele_2,
        is_no_call=consensus.is_no_call,
        dosage=consensus.dosage,
        consensus_method=consensus.consensus_method,
        is_imputed=consensus.is_imputed,
        consensus_r2=consensus.consensus_r2,
        contributing_calls=(*consensus.contributing_calls, imputed_call_id),
        resolution_rule=consensus.resolution_rule,
        confidence=consensus.confidence,
    )


def _discrepancy(  # noqa: PLR0913 — schema fields, not collapsible
    *,
    variant_id: int,
    dtype: DiscrepancyType,
    severity: Severity,
    source_a: Source,
    call_a: CallView | None,
    source_b: Source | None,
    call_b: CallView | None,
    resolution: str | None,
    reason: str | None,
) -> DiscrepancyRow:
    return DiscrepancyRow(
        variant_id=variant_id,
        discrepancy_type=dtype,
        severity=severity,
        source_a=source_a,
        call_a_id=call_a.call_id if call_a is not None else None,
        genotype_a=_render_genotype(call_a),
        source_b=source_b,
        call_b_id=call_b.call_id if call_b is not None else None,
        genotype_b=_render_genotype(call_b),
        resolution=resolution,
        resolution_reason=reason,
    )


def _resolve_both_called(
    pair: VariantPair,
    a: CallView,
    b: CallView,
) -> tuple[ConsensusRow, DiscrepancyRow | None]:
    """Both ``23andme`` and ``ancestry`` produced a call (neither is no-call).

    Compare the alphabetically-ordered allele pairs. Identical ⇒ ``both_concordant``.
    Different ⇒ try a complement flip; if that resolves and the site is
    non-palindromic, record ``flipped_strand_match``. If the site is
    palindromic ⇒ ``strand_ambiguous`` and the consensus is held as no-call.
    Otherwise it is a real ``genotype_mismatch`` and the consensus is held
    as no-call.
    """
    a_alleles = sorted_pair(a.allele_1 or "", a.allele_2 or "")
    b_alleles = sorted_pair(b.allele_1 or "", b.allele_2 or "")

    if a_alleles == b_alleles:
        return (
            _consensus(
                variant_id=pair.variant_id,
                a1=a_alleles[0],
                a2=a_alleles[1],
                is_no_call=False,
                alt_allele=pair.alt_allele,
                method="both_concordant",
                contributing=(a.call_id, b.call_id),
                confidence=_CONF_BOTH_CONCORDANT,
            ),
            None,
        )

    palindromic = is_palindromic_site(pair.ref_allele, pair.alt_allele)
    if palindromic:
        # Strand cannot be inferred from genotype alone at A/T or C/G sites;
        # hold as no-call and surface the ambiguity for the dashboard.
        return (
            _consensus(
                variant_id=pair.variant_id,
                a1=None,
                a2=None,
                is_no_call=True,
                alt_allele=pair.alt_allele,
                method="unresolvable",
                contributing=(a.call_id, b.call_id),
                confidence=None,
            ),
            _discrepancy(
                variant_id=pair.variant_id,
                dtype="strand_ambiguous",
                severity="minor",
                source_a=a.source,
                call_a=a,
                source_b=b.source,
                call_b=b,
                resolution="unresolved",
                reason="palindromic site (A/T or C/G); strand not inferable from genotype",
            ),
        )

    # Non-palindromic mismatch: try a strand flip.
    b_flipped = complement_pair(b.allele_1 or "", b.allele_2 or "")
    if a_alleles == b_flipped:
        return (
            _consensus(
                variant_id=pair.variant_id,
                a1=a_alleles[0],
                a2=a_alleles[1],
                is_no_call=False,
                alt_allele=pair.alt_allele,
                method="disagreement_resolved",
                contributing=(a.call_id, b.call_id),
                confidence=_CONF_STRAND_FLIP_RESOLVED,
            ),
            _discrepancy(
                variant_id=pair.variant_id,
                dtype="strand_flip_resolved",
                severity="info",
                source_a=a.source,
                call_a=a,
                source_b=b.source,
                call_b=b,
                resolution="flipped_strand_match",
                reason=(
                    f"non-palindromic site; complement of {b.source} "
                    f"({b_alleles[0]}/{b_alleles[1]}) matches {a.source} "
                    f"({a_alleles[0]}/{a_alleles[1]})"
                ),
            ),
        )

    return (
        _consensus(
            variant_id=pair.variant_id,
            a1=None,
            a2=None,
            is_no_call=True,
            alt_allele=pair.alt_allele,
            method="unresolvable",
            contributing=(a.call_id, b.call_id),
            confidence=None,
        ),
        _discrepancy(
            variant_id=pair.variant_id,
            dtype="genotype_mismatch",
            severity="major",
            source_a=a.source,
            call_a=a,
            source_b=b.source,
            call_b=b,
            resolution="unresolved",
            reason=("non-palindromic site; alleles disagree even after complement flip"),
        ),
    )


def _resolve_single_source(
    pair: VariantPair,
    present: CallView,
    *,
    other_call: CallView | None,
) -> tuple[ConsensusRow, DiscrepancyRow]:
    """Only one platform produced a call for this variant.

    ``other_call`` is ``None`` when no row from the other platform exists at
    all (true ``platform_unique``). When the other platform did report on the
    site but its call was a no-call, the no-call ``CallView`` is passed so
    the discrepancy is ``no_call_diff`` and the no-call's ``call_id`` is
    captured in the discrepancy row.
    """
    if present.is_no_call:
        # Even the one call we have is a no-call: report platform_unique
        # against no other source. Down-stream tools can spot this via
        # consensus.is_no_call=True with method='single_source'.
        return (
            _consensus(
                variant_id=pair.variant_id,
                a1=None,
                a2=None,
                is_no_call=True,
                alt_allele=pair.alt_allele,
                method="single_source",
                contributing=(present.call_id,),
                confidence=None,
            ),
            _discrepancy(
                variant_id=pair.variant_id,
                dtype="platform_unique",
                severity="info",
                source_a=present.source,
                call_a=present,
                source_b=None,
                call_b=None,
                resolution="taken_from_a",
                reason=(f"only {present.source} reported this site and the call is no-call"),
            ),
        )

    a_alleles = sorted_pair(present.allele_1 or "", present.allele_2 or "")
    if other_call is None:
        return (
            _consensus(
                variant_id=pair.variant_id,
                a1=a_alleles[0],
                a2=a_alleles[1],
                is_no_call=False,
                alt_allele=pair.alt_allele,
                method="single_source",
                contributing=(present.call_id,),
                confidence=_CONF_SINGLE_SOURCE,
            ),
            _discrepancy(
                variant_id=pair.variant_id,
                dtype="platform_unique",
                severity="info",
                source_a=present.source,
                call_a=present,
                source_b=None,
                call_b=None,
                resolution="taken_from_a",
                reason=f"variant present only on {present.source}",
            ),
        )

    return (
        _consensus(
            variant_id=pair.variant_id,
            a1=a_alleles[0],
            a2=a_alleles[1],
            is_no_call=False,
            alt_allele=pair.alt_allele,
            method="single_source",
            contributing=(present.call_id,),
            confidence=_CONF_SINGLE_NO_CALL_DIFF,
        ),
        _discrepancy(
            variant_id=pair.variant_id,
            dtype="no_call_diff",
            severity="minor",
            source_a=present.source,
            call_a=present,
            source_b=other_call.source,
            call_b=other_call,
            resolution="taken_from_a",
            reason=(f"{present.source} called this site; {other_call.source} reported no-call"),
        ),
    )


def _resolve_both_no_call(
    pair: VariantPair,
    a: CallView,
    b: CallView,
) -> ConsensusRow:
    """Both sources reported the site but neither produced a call.

    Both sides agree that this site is no-call, so the consensus method is
    ``both_concordant`` with ``is_no_call = True``. No discrepancy.
    """
    return _consensus(
        variant_id=pair.variant_id,
        a1=None,
        a2=None,
        is_no_call=True,
        alt_allele=pair.alt_allele,
        method="both_concordant",
        contributing=(a.call_id, b.call_id),
        confidence=None,
    )


def _resolve_both_present(
    pair: VariantPair,
    a: CallView,
    b: CallView,
) -> tuple[ConsensusRow, list[DiscrepancyRow]]:
    if a.is_no_call and b.is_no_call:
        return (_resolve_both_no_call(pair, a, b), [])
    if a.is_no_call:
        consensus, disc = _resolve_single_source(pair, b, other_call=a)
        return (consensus, [disc])
    if b.is_no_call:
        consensus, disc = _resolve_single_source(pair, a, other_call=b)
        return (consensus, [disc])
    consensus, maybe_disc = _resolve_both_called(pair, a, b)
    return (consensus, [maybe_disc] if maybe_disc is not None else [])


def _resolve_no_active_calls(pair: VariantPair) -> tuple[ConsensusRow, list[DiscrepancyRow]]:
    """Defensive branch: a ``variants_master`` row with no active call anywhere.

    Should not occur in a well-formed state, but we keep merge idempotent so
    an upstream cleanup that leaves a stray row does not bring the pipeline
    down.
    """
    return (
        _consensus(
            variant_id=pair.variant_id,
            a1=None,
            a2=None,
            is_no_call=True,
            alt_allele=pair.alt_allele,
            method="unresolvable",
            contributing=(),
            confidence=None,
        ),
        [],
    )


def _resolve_imputed_only(pair: VariantPair, imputed: CallView) -> ConsensusRow:
    """Only the ``beagle_imputed`` call is active at this variant.

    The consensus method is ``imputed_only`` and ``consensus_r2`` carries the
    imputed call's per-variant R² so downstream consumers can filter by
    imputation quality. ``confidence`` is left ``None`` as a placeholder for
    the Phase 7 evidence-weighted rollup. No discrepancy is emitted: an
    imputed-only call is not a disagreement with anything, just a thin source.
    """
    if imputed.is_no_call:
        return _consensus(
            variant_id=pair.variant_id,
            a1=None,
            a2=None,
            is_no_call=True,
            alt_allele=pair.alt_allele,
            method="imputed_only",
            contributing=(imputed.call_id,),
            confidence=None,
            is_imputed=True,
            consensus_r2=imputed.imputation_r2,
        )
    a_alleles = sorted_pair(imputed.allele_1 or "", imputed.allele_2 or "")
    return _consensus(
        variant_id=pair.variant_id,
        a1=a_alleles[0],
        a2=a_alleles[1],
        is_no_call=False,
        alt_allele=pair.alt_allele,
        method="imputed_only",
        contributing=(imputed.call_id,),
        confidence=None,
        is_imputed=True,
        consensus_r2=imputed.imputation_r2,
    )


def resolve(pair: VariantPair) -> tuple[ConsensusRow, list[DiscrepancyRow]]:
    """Apply ``consensus_v1`` to one variant pair.

    Returns the consensus row destined for ``consensus_genotypes`` and zero or
    more discrepancy rows destined for ``discrepancies``.

    Branch order:

    1. If any chip call (``23andme`` and/or ``ancestry``) is active, the
       chip-only Phase 3 resolution runs unchanged. If an active
       ``beagle_imputed`` call is also present at the same variant, it is
       appended to the resulting consensus's ``contributing_calls`` as
       confirming evidence — the consensus method, alleles, dosage, and
       ``is_imputed`` flag are not touched.
    2. If only the imputed call is active, produce an ``imputed_only``
       consensus carrying the imputed call's alleles and ``imputation_r2``.
    3. Otherwise defensively produce an ``unresolvable`` no-call.
    """
    a = pair.twentythree
    b = pair.ancestry
    imputed = pair.imputed

    if a is not None and b is not None:
        consensus, discs = _resolve_both_present(pair, a, b)
        if imputed is not None:
            consensus = _append_imputed_call(consensus, imputed.call_id)
        return (consensus, discs)
    if a is not None:
        consensus, disc = _resolve_single_source(pair, a, other_call=None)
        if imputed is not None:
            consensus = _append_imputed_call(consensus, imputed.call_id)
        return (consensus, [disc])
    if b is not None:
        consensus, disc = _resolve_single_source(pair, b, other_call=None)
        if imputed is not None:
            consensus = _append_imputed_call(consensus, imputed.call_id)
        return (consensus, [disc])
    if imputed is not None:
        return (_resolve_imputed_only(pair, imputed), [])
    return _resolve_no_active_calls(pair)
