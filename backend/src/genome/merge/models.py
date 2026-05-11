"""Data classes used between the merge stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

MERGE_VERSION: Final[str] = "consensus_v1"

ConsensusMethod = Literal[
    "both_concordant",
    "single_source",
    "imputed_only",
    "disagreement_resolved",
    "unresolvable",
]
DiscrepancyType = Literal[
    "genotype_mismatch",
    "strand_ambiguous",
    "build_mismatch",
    "no_call_diff",
    "platform_unique",
    "multi_allelic_split",
]
Severity = Literal["critical", "major", "minor", "info"]
Source = Literal["23andme", "ancestry", "topmed_imputed"]


@dataclass(frozen=True, slots=True)
class CallView:
    """A single source's active call as seen by the merge step.

    All allele fields are post-ingest normalized (alphabetically ordered,
    upper-case single tokens or empty when no-call). ``call_id`` identifies
    the underlying ``genotype_calls`` row so the discrepancy / consensus
    rows can reference it.
    """

    call_id: int
    source: Source
    allele_1: str | None
    allele_2: str | None
    is_no_call: bool


@dataclass(frozen=True, slots=True)
class VariantPair:
    """Everything the consensus rule needs about one variant.

    Built by :mod:`genome.merge.pipeline` from a ``variants_master`` row plus
    its active ``genotype_calls`` rows. ``twentythree`` and ``ancestry`` are
    ``None`` when that source did not contribute an active call.
    """

    variant_id: int
    chrom: str
    pos_grch38: int
    ref_allele: str
    alt_allele: str
    twentythree: CallView | None
    ancestry: CallView | None


@dataclass(frozen=True, slots=True)
class ConsensusRow:
    """One row destined for ``consensus_genotypes``."""

    variant_id: int
    consensus_allele_1: str | None
    consensus_allele_2: str | None
    is_no_call: bool
    dosage: int | None
    consensus_method: ConsensusMethod
    is_imputed: bool
    consensus_r2: float | None
    contributing_calls: tuple[int, ...]
    resolution_rule: str
    confidence: float | None


@dataclass(frozen=True, slots=True)
class DiscrepancyRow:
    """One row destined for ``discrepancies``.

    ``call_b_id`` / ``source_b`` are optional because ``platform_unique`` has
    only one source involved. ``genotype_a`` / ``genotype_b`` are pre-rendered
    ``allele_1/allele_2`` strings (or ``'--'`` for no-call) so the dashboard
    view does not have to re-join.
    """

    variant_id: int
    discrepancy_type: DiscrepancyType
    severity: Severity
    source_a: Source
    call_a_id: int | None
    genotype_a: str | None
    source_b: Source | None
    call_b_id: int | None
    genotype_b: str | None
    resolution: str | None
    resolution_reason: str | None


@dataclass(slots=True)
class MergeResult:
    """Summary returned by :func:`pipeline.merge_all`."""

    consensus_rows_written: int = 0
    discrepancy_rows_written: int = 0
    method_counts: dict[str, int] = field(default_factory=dict)
    discrepancy_type_counts: dict[str, int] = field(default_factory=dict)
    severity_counts: dict[str, int] = field(default_factory=dict)
    strand_flip_resolutions: int = 0
    concordance_rate: float | None = None
    resolution_rule: str = MERGE_VERSION
