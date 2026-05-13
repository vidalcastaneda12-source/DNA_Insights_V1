"""Internal data classes used between the parse → normalize → write stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from pathlib import Path

Source = Literal["23andme", "ancestry", "beagle_imputed", "topmed_imputed"]
StrandStatus = Literal[
    "resolved_plus",
    "resolved_minus",
    "flipped_to_match",
    "ambiguous_palindrome",
    "unknown",
]
LiftoverStatus = Literal[
    "native_grch38",
    "lifted_ok",
    "lifted_with_warning",
    "lift_failed",
]
VariantType = Literal["SNV", "INDEL", "MNV"]

# Schema's chromosome_enum.
VALID_CHROMS: frozenset[str] = frozenset(
    {*(str(i) for i in range(1, 23)), "X", "Y", "MT"},
)

# Numeric / alias chromosome translations seen in real exports.
_CHROM_ALIASES: Final[dict[str, str]] = {
    "23": "X",
    "24": "Y",
    "25": "X",  # PAR — collapse into X
    "26": "MT",
    "M": "MT",
}


def normalize_chrom(value: str) -> str | None:
    """Map a raw chromosome label to the schema's ``chromosome_enum``.

    Positive-rule filter: returns the canonical label only when it lands in
    ``VALID_CHROMS`` (``1..22, X, Y, MT``) after the alias remap. Anything
    else — ``'0'``, GRCh38 alt contigs (``'8_KI270821v1_alt'``), unlocalized
    contigs (``'4_GL000008v2_random'``), unplaced contigs (``'Un_GL000226v1'``,
    ``'chrUn_GL000226v1'``), decoy sequences (``'*_decoy'``), arbitrary
    scaffolds — returns ``None`` so the caller can drop or quality-flag the row.

    Used at parse time on raw chromosome labels from the export, and at
    normalize time on post-lift chromosomes (pyliftover can land a canonical
    GRCh37 coordinate on a non-canonical GRCh38 contig).
    """
    raw = value.strip().upper().removeprefix("CHR")
    raw = _CHROM_ALIASES.get(raw, raw)
    if raw in VALID_CHROMS:
        return raw
    return None


@dataclass(frozen=True, slots=True)
class RawFileMeta:
    """Metadata derived from the raw export header."""

    source: Source
    native_build: str  # 'GRCh37' | 'GRCh38'
    chip_version: str | None
    raw_header: tuple[str, ...]


@dataclass(slots=True)
class ParseStats:
    """Mutable counters populated as a raw export streams through the pipeline.

    The parser returns this alongside the row iterator; the normalize stage
    receives the same instance and can mutate ``lifted_to_non_canonical`` when
    a lift-over lands on a non-canonical GRCh38 contig. The caller reads it
    after iteration completes to record per-run drop counts on
    ``ingestion_runs``.
    """

    dropped_non_canonical: int = 0
    lifted_to_non_canonical: int = 0


@dataclass(frozen=True, slots=True)
class RawCall:
    """A single SNP call as it appeared in the raw export, before normalization.

    ``allele_1`` / ``allele_2`` are upper-case single characters for SNVs ('A',
    'C', 'G', 'T'), 'I' / 'D' for 23andMe-style indels, or empty when
    ``is_no_call`` is true. ``pos`` is 1-based and in the file's native build.
    """

    rsid: str | None
    chrom: str
    pos: int
    allele_1: str
    allele_2: str
    is_no_call: bool


@dataclass(frozen=True, slots=True)
class NormalizedCall:
    """A call after lift-over, allele ordering, and palindrome flagging.

    One ``NormalizedCall`` corresponds to one biallelic ``variants_master`` row
    plus one ``genotype_calls`` row. Multi-allelic raw inputs are exploded into
    multiple ``NormalizedCall`` instances upstream.
    """

    rsid: str | None
    chrom: str
    pos_grch38: int
    pos_grch37: int | None
    ref_allele: str
    alt_allele: str
    variant_type: VariantType
    allele_1: str
    allele_2: str
    is_no_call: bool
    strand_status: StrandStatus
    liftover_chain: str | None
    liftover_status: LiftoverStatus
    quality_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Summary returned by :func:`pipeline.ingest_file`."""

    run_id: int
    qc_id: int
    source: Source
    file_path: Path
    archived_path: Path
    file_hash_sha256: str
    file_size_bytes: int
    file_native_build: str
    variants_total: int
    variants_called: int
    variants_no_call: int
    variants_imputed: int
    variants_dropped_non_canonical: int
    variants_dropped_lift_to_non_canonical: int
    new_variants_master_rows: int
    deactivated_prior_calls: int
    qc_status: Literal["pass", "warn", "fail"]
    qc_notes: str
    sex_inferred: str
    call_rate: float
    heterozygosity_rate: float
    chr_x_het_rate: float | None
    quality_flag_counts: dict[str, int] = field(default_factory=dict)
