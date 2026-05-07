"""Compute the per-ingestion ``sample_qc`` row from a stream of normalized calls."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from genome.ingest.models import NormalizedCall

# Sex inference cutoffs. These are deliberate simplifications — Phase 6's
# ``derived_genome_qc`` step will recompute against population baselines once
# annotations are loaded.
_X_HET_FEMALE_MIN: float = 0.10
_X_HET_MALE_MAX: float = 0.05
_Y_CALLS_MIN_FOR_MALE: int = 5

_QC_PASS_CALL_RATE: float = 0.97
_QC_WARN_CALL_RATE: float = 0.90


@dataclass(frozen=True, slots=True)
class SampleQC:
    """Computed per-ingestion QC. Fields map 1:1 onto ``sample_qc``."""

    call_rate: Decimal
    heterozygosity_rate: Decimal
    het_outlier: bool | None
    sex_inferred: Literal["M", "F", "ambiguous"]
    chr_x_het_rate: Decimal | None
    qc_status: Literal["pass", "warn", "fail"]
    qc_notes: str
    variants_total: int
    variants_called: int
    variants_no_call: int


def _quant4(x: float) -> Decimal:
    """Round to four decimal places (DECIMAL(5,4) precision)."""
    return Decimal(f"{x:.4f}")


def _is_het(call: NormalizedCall) -> bool:
    if call.is_no_call:
        return False
    return call.allele_1 != call.allele_2


def compute_sample_qc(calls: Iterable[NormalizedCall]) -> SampleQC:
    """Aggregate a single sample's calls into ``SampleQC``."""
    total = 0
    called = 0
    het = 0
    autosomal_called = 0
    autosomal_het = 0
    x_called = 0
    x_het = 0
    y_called = 0

    for call in calls:
        total += 1
        if call.is_no_call:
            continue
        called += 1
        het_flag = _is_het(call)
        if het_flag:
            het += 1
        if call.chrom == "X":
            x_called += 1
            if het_flag:
                x_het += 1
        elif call.chrom == "Y":
            y_called += 1
        elif call.chrom not in {"MT", "X", "Y"}:
            autosomal_called += 1
            if het_flag:
                autosomal_het += 1

    call_rate = called / total if total else 0.0
    het_rate = autosomal_het / autosomal_called if autosomal_called else 0.0
    chr_x_het_rate = x_het / x_called if x_called else None

    sex = _infer_sex(chr_x_het_rate, y_called)
    status, notes = _rollup_status(call_rate, sex)

    return SampleQC(
        call_rate=_quant4(call_rate),
        heterozygosity_rate=_quant4(het_rate),
        het_outlier=None,  # baseline arrives in phase 6.
        sex_inferred=sex,
        chr_x_het_rate=_quant4(chr_x_het_rate) if chr_x_het_rate is not None else None,
        qc_status=status,
        qc_notes=notes,
        variants_total=total,
        variants_called=called,
        variants_no_call=total - called,
    )


def _infer_sex(
    chr_x_het_rate: float | None,
    y_called: int,
) -> Literal["M", "F", "ambiguous"]:
    if y_called >= _Y_CALLS_MIN_FOR_MALE and (
        chr_x_het_rate is None or chr_x_het_rate <= _X_HET_MALE_MAX
    ):
        return "M"
    if y_called < _Y_CALLS_MIN_FOR_MALE and (
        chr_x_het_rate is not None and chr_x_het_rate >= _X_HET_FEMALE_MIN
    ):
        return "F"
    return "ambiguous"


def _rollup_status(
    call_rate: float,
    sex: str,
) -> tuple[Literal["pass", "warn", "fail"], str]:
    notes: list[str] = []
    if call_rate >= _QC_PASS_CALL_RATE:
        status: Literal["pass", "warn", "fail"] = "pass"
    elif call_rate >= _QC_WARN_CALL_RATE:
        status = "warn"
        notes.append(f"call_rate={call_rate:.4f} below 0.97 pass threshold")
    else:
        status = "fail"
        notes.append(f"call_rate={call_rate:.4f} below 0.90 warn threshold")
    if sex == "ambiguous":
        notes.append("sex inference ambiguous (X het / Y call counts inconclusive)")
    return status, "; ".join(notes)
