"""5-fold leave-one-out validation of the male non-PAR chrX dosage gate (PR 5a).

Beagle's ``INFO/DR2`` is structurally ``0.00`` for a single male sample in the
hemizygous non-PAR region, so the import gate uses the live dosage signal there:
typed anchors are kept unconditionally and imputed sites are kept iff confident
ALT-bearing (``DS >= threshold``, finding-031). DR2-death means the usual "the
metric says it's good" PASS criterion is unavailable, so this module replaces it
with a falsifiable, **accuracy-grounded** one: hold out the user's own typed
non-PAR anchors in 5 disjoint folds, re-impute each fold, and measure how often
the gate-kept call matches the held-out truth.

Design:

* **Validate, don't search.** The threshold is fixed a priori (the importer's
  default, 0.9). LOO only *measures* the precision achieved at it — below the bar
  is falsification (escalate), not a hunt for a looser bar.
* **5 disjoint folds**, each typed non-PAR anchor held out exactly once
  (:func:`partition_folds`). Per fold: write a masked haploid non-PAR target (the
  fold's anchors set to ``.``), run **one** non-PAR Beagle region against the
  native non-PAR panel subset, read the imputed ``FORMAT/DS`` at the masked
  positions, and compare ``round(DS)`` to the held-out truth.
* **Precision of the gate-kept set.** A masked anchor is re-imputed (so it is an
  imputed call); the gate keeps it iff ``DS >= threshold``. The headline number is
  the precision of that kept set — among re-imputed anchors with ``DS >= 0.9``,
  how often the call matches truth. Stratified by (MAF bin x dosage-confidence
  bin) over the kept set so rare-tail / low-precision cells are visible.
* **Long-op discipline.** Per-fold structlog progress; all scratch under the run
  archive (the big disk), never ``/tmp`` (CLAUDE.md perf convention / PR 5a).

The pure scoring core (:func:`compute_loo_report`, :func:`partition_folds`, the
binning) is unit-tested on a synthetic fixture; the Beagle orchestration
(:func:`run_chrx_loo`) is the named long-op the real-data gate runs.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.imputation.archive import ImputationArchive, restrict_file
from genome.imputation.beagle_runner import (
    DEFAULT_MEMORY_GB,
    DEFAULT_NE,
    check_java_available,
    default_threads,
)
from genome.imputation.ingest import DEFAULT_DCONF_THRESHOLD
from genome.imputation.reference_panel import ReferencePanel, validate_panel

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    import cyvcf2 as _cyvcf2_typing
    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

LOO_PIPELINE_VERSION: Final[str] = "chrx_loo_v0.1.0"
DEFAULT_N_FOLDS: Final[int] = 5
_MIN_FOLDS: Final[int] = 2

# Stratification bin edges. Both are read as right-open ``[lo, hi)`` except the
# final bin, which is closed so the top value (MAF 0.5, dconf 1.0) lands in it.
DEFAULT_MAF_EDGES: Final[tuple[float, ...]] = (0.0, 0.01, 0.05, 0.5)
DEFAULT_CONF_EDGES: Final[tuple[float, ...]] = (0.5, 0.9, 0.95, 0.99, 1.0)
_MAF_NA_BIN: Final[str] = "na"

# Idempotent marker stamped on the imputed sample_qc.qc_notes — same convention
# as the het guard's ``[chrx_male_nonpar_het=N]`` (chrx_qc.py).
_LOO_NOTE_RE: Final[re.Pattern[str]] = re.compile(r"\s*\[chrx_loo_concordance=[^\]]*\]")


class ChrxLooError(RuntimeError):
    """The chrX LOO harness could not run (missing inputs / panel / Java, or a fold failed)."""


# ---------------------------------------------------------------------------
# Pure scoring core (unit-tested without Beagle)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LooAnchorResult:
    """One held-out typed anchor: its truth vs the re-imputed call."""

    pos: int
    truth: int
    """Held-out observed haploid allele — 0 (ref) or 1 (alt)."""
    imputed_dosage: float
    """Beagle ``FORMAT/DS`` at the masked position (haploid, 0..1)."""
    maf: float | None
    """Minor allele frequency ``min(AF, 1-AF)`` from the fold's ``INFO/AF`` (``None`` if absent)."""

    @property
    def dosage_confidence(self) -> float:
        """``max(DS, 1-DS)`` — the gate metric; equals Beagle's max posterior here."""
        return max(self.imputed_dosage, 1.0 - self.imputed_dosage)

    @property
    def imputed_call(self) -> int:
        """Rounded haploid call (``DS >= 0.5`` → 1). Unambiguous once dconf >= 0.9."""
        return 1 if self.imputed_dosage >= 0.5 else 0  # noqa: PLR2004 — haploid midpoint

    @property
    def concordant(self) -> bool:
        """True iff the rounded imputed call equals the held-out truth."""
        return self.imputed_call == self.truth


@dataclass(frozen=True, slots=True)
class LooCell:
    """One (MAF bin x dconf bin) cell of the stratified concordance table."""

    maf_bin: str
    conf_bin: str
    n: int
    concordant: int

    @property
    def concordance(self) -> float | None:
        """Concordance in this cell, or ``None`` when empty."""
        return self.concordant / self.n if self.n else None


@dataclass(frozen=True, slots=True)
class LooReport:
    """Aggregate LOO result across all folds."""

    n_anchors: int
    n_folds: int
    threshold: float
    n_at_or_above_threshold: int
    n_concordant_at_threshold: int
    concordance: float | None
    """Precision of the gate-kept set: concordance among re-imputed anchors with
    ``imputed_dosage >= threshold`` (the confident-ALT calls the importer keeps —
    finding-031). ``None`` when no anchor re-imputed to a confident-ALT call."""
    cells: tuple[LooCell, ...]
    maf_edges: tuple[float, ...]
    conf_edges: tuple[float, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the report artifact + log lines."""
        return {
            "pipeline_version": LOO_PIPELINE_VERSION,
            "n_anchors": self.n_anchors,
            "n_folds": self.n_folds,
            "dconf_threshold": self.threshold,
            "n_at_or_above_threshold": self.n_at_or_above_threshold,
            "n_concordant_at_threshold": self.n_concordant_at_threshold,
            "concordance_at_threshold": self.concordance,
            "maf_edges": list(self.maf_edges),
            "conf_edges": list(self.conf_edges),
            "cells": [
                {
                    "maf_bin": c.maf_bin,
                    "conf_bin": c.conf_bin,
                    "n": c.n,
                    "concordant": c.concordant,
                    "concordance": c.concordance,
                }
                for c in self.cells
            ],
        }


def partition_folds(positions: Iterable[int], n_folds: int) -> list[frozenset[int]]:
    """Partition ``positions`` into ``n_folds`` disjoint folds, each held out once.

    Positions are sorted ascending and dealt round-robin (sorted index modulo
    ``n_folds``), so each fold is an evenly-spaced "comb" across the region rather
    than a contiguous block — a held-out site always has typed neighbours, which
    is the realistic LOO condition. Deterministic (no RNG), so re-runs reproduce.
    """
    if n_folds < _MIN_FOLDS:
        msg = f"n_folds must be >= {_MIN_FOLDS}, got {n_folds}"
        raise ValueError(msg)
    folds: list[set[int]] = [set() for _ in range(n_folds)]
    for i, pos in enumerate(sorted(set(positions))):
        folds[i % n_folds].add(pos)
    return [frozenset(f) for f in folds]


def _bin_label(value: float, edges: Sequence[float]) -> str:
    """Return the ``"lo-hi"`` label of the bin ``value`` falls in.

    Bins are right-open ``[edges[i], edges[i+1])`` except the last, which is
    closed so a value equal to the top edge lands in it. Values below ``edges[0]``
    fall in the first bin; values at/above the top edge fall in the last bin.
    """
    n = len(edges)
    for i in range(n - 1):
        lo, hi = edges[i], edges[i + 1]
        last = i == n - 2  # penultimate edge → the closed top bin
        in_bin = lo <= value <= hi if last else lo <= value < hi
        if in_bin or (i == 0 and value < lo):
            return f"{lo:.2f}-{hi:.2f}"
    return f"{edges[-2]:.2f}-{edges[-1]:.2f}"


def compute_loo_report(
    results: Sequence[LooAnchorResult],
    *,
    threshold: float = DEFAULT_DCONF_THRESHOLD,
    n_folds: int = DEFAULT_N_FOLDS,
    maf_edges: tuple[float, ...] = DEFAULT_MAF_EDGES,
    conf_edges: tuple[float, ...] = DEFAULT_CONF_EDGES,
) -> LooReport:
    """Aggregate per-anchor LOO results into the overall + stratified report.

    LOO masks typed anchors and re-imputes them, so every result is an *imputed*
    call. The importer keeps an imputed male non-PAR call iff it is a
    confident-ALT call (``imputed_dosage >= threshold``, finding-031), so the
    headline ``concordance`` is the **precision of that gate-kept set**: among the
    re-imputed anchors with ``imputed_dosage >= threshold``, how often the call
    matches the held-out truth. The stratified ``cells`` cover the **gate-kept
    set** (binned by MAF x dconf — every kept call has ``dconf = DS >= threshold``),
    so each cell is a per-(MAF, confidence) precision the PASS criterion checks for
    collapse. Anchors that re-impute below the bar are dropped by the gate and so
    excluded here — they are a sensitivity (recall) question, not a kept-call
    accuracy one.
    """
    at_threshold = [r for r in results if r.imputed_dosage >= threshold]
    n_at = len(at_threshold)
    n_conc = sum(1 for r in at_threshold if r.concordant)
    overall = n_conc / n_at if n_at else None

    buckets: dict[tuple[str, str], list[int]] = {}
    for r in at_threshold:
        maf_bin = _MAF_NA_BIN if r.maf is None else _bin_label(r.maf, maf_edges)
        conf_bin = _bin_label(r.dosage_confidence, conf_edges)
        buckets.setdefault((maf_bin, conf_bin), []).append(int(r.concordant))
    cells = tuple(
        LooCell(maf_bin=mb, conf_bin=cb, n=len(flags), concordant=sum(flags))
        for (mb, cb), flags in sorted(buckets.items())
    )
    return LooReport(
        n_anchors=len(results),
        n_folds=n_folds,
        threshold=threshold,
        n_at_or_above_threshold=n_at,
        n_concordant_at_threshold=n_conc,
        concordance=overall,
        cells=cells,
        maf_edges=maf_edges,
        conf_edges=conf_edges,
    )


# ---------------------------------------------------------------------------
# VCF helpers (unit-tested on tiny fixtures)
# ---------------------------------------------------------------------------


def read_haploid_anchors(target_vcf: Path) -> dict[int, int]:
    """Read ``pos -> truth haploid allele`` from a male non-PAR target VCF.

    The prepare step writes the non-PAR target as plain-gzip text with a single
    ``GT`` FORMAT field and a haploid sample token (``0`` / ``1`` / ``.``). Only
    biallelic-SNV rows with a determinate haploid call (``0`` or ``1``) are
    anchors; no-calls and anything malformed are skipped.
    """
    anchors: dict[int, int] = {}
    with gzip.open(target_vcf, "rt", encoding="ascii") as fh:
        for line in fh:
            if not line or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 10:  # noqa: PLR2004 — CHROM..SAMPLE is 10 columns
                continue
            ref, alt, sample = cols[3], cols[4], cols[9]
            if len(ref) != 1 or len(alt) != 1:
                continue
            gt = sample.split(":", 1)[0]
            if gt in {"0", "1"}:
                anchors[int(cols[1])] = int(gt)
    return anchors


def write_masked_target(src_vcf: Path, dst_vcf: Path, mask_positions: frozenset[int]) -> int:
    """Copy ``src_vcf`` to ``dst_vcf`` with ``mask_positions`` set to missing (``.``).

    A line-level transform that preserves the header verbatim (so Beagle sees the
    identical contig / FORMAT it would in production) and replaces only the sample
    GT token of the masked records with ``.``. Returns the number of records
    masked. Both files are plain-gzip text, matching the prepare-step writer.
    """
    masked = 0
    with (
        gzip.open(src_vcf, "rt", encoding="ascii") as src,
        gzip.open(dst_vcf, "wt", encoding="ascii", compresslevel=6) as dst,
    ):
        for line in src:
            if not line or line.startswith("#"):
                dst.write(line)
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 10 and int(cols[1]) in mask_positions:  # noqa: PLR2004 — 10 VCF columns
                cols[9] = "."
                dst.write("\t".join(cols) + "\n")
                masked += 1
            else:
                dst.write(line)
    return masked


def read_imputed_calls(
    result_vcf: Path,
    anchors_truth: dict[int, int],
    mask_positions: frozenset[int],
) -> list[LooAnchorResult]:
    """Read the re-imputed DS + AF at ``mask_positions`` and pair with truth.

    Returns one :class:`LooAnchorResult` per masked position present in
    ``result_vcf`` with a readable haploid ``FORMAT/DS``. ``INFO/AF`` (Beagle's
    panel alt frequency) drives the MAF stratification; a missing AF yields
    ``maf=None`` (the ``na`` bin) without dropping the anchor from the overall
    concordance.
    """
    import cyvcf2  # noqa: PLC0415 — deferred so the module loads without cyvcf2 at type-check time

    out: list[LooAnchorResult] = []
    reader = cyvcf2.VCF(str(result_vcf))
    try:
        for v in reader:
            pos = int(v.POS)
            if pos not in mask_positions or pos not in anchors_truth:
                continue
            ds = _read_sample_ds(v)
            if ds is None:
                continue
            af_raw = v.INFO.get("AF")
            maf: float | None = None
            if af_raw is not None:
                af = float(af_raw)
                maf = min(af, 1.0 - af)
            out.append(
                LooAnchorResult(pos=pos, truth=anchors_truth[pos], imputed_dosage=ds, maf=maf),
            )
    finally:
        reader.close()
    return out


def _read_sample_ds(variant: _cyvcf2_typing.Variant) -> float | None:
    """Sample-0 ``FORMAT/DS`` as ``float``, or ``None`` (no DS tag / NaN).

    cyvcf2 raises ``KeyError`` when the DS tag is undeclared and returns a
    ``(n_samples, 1)`` float32 array otherwise. ``variant`` is untyped (cyvcf2 has
    no stubs), so indexing the array is allowed without a ``type: ignore``.
    """
    try:
        arr = variant.format("DS")
    except KeyError:
        return None
    if arr is None:
        return None
    value = float(arr[0][0])
    return None if math.isnan(value) else value


# ---------------------------------------------------------------------------
# Beagle orchestration (the named long-op; not CI-tested)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LooInputs:
    """Resolved on-disk inputs for the chrX LOO run."""

    archive: ImputationArchive
    panel: ReferencePanel
    nonpar_target: Path


def _resolve_loo_inputs(
    imputation_id: int,
    *,
    archive_root: Path,
    panel_root: Path | None,
) -> _LooInputs:
    """Resolve + validate the run archive, the non-PAR target, and the panel."""
    archive = ImputationArchive.for_run(archive_root, imputation_id)
    nonpar_target = archive.chrx_region_upload_path("nonpar")
    if not nonpar_target.is_file():
        msg = (
            f"no male non-PAR chrX target at {nonpar_target}. Run `genome imputation "
            f"prepare --sex M` (then it lands under upload/chrX_regions/) before chrx-loo."
        )
        raise ChrxLooError(msg)
    panel = ReferencePanel.resolve(panel_root)
    problems = validate_panel(panel)
    if problems:
        detail = "\n  - ".join(problems)
        msg = f"reference panel is incomplete:\n  - {detail}"
        raise ChrxLooError(msg)
    if not panel.chrx_nonpar_panel.is_file():
        msg = (
            f"non-PAR chrX panel subset missing at {panel.chrx_nonpar_panel}. "
            "Run `genome imputation panel prepare-chrx` first."
        )
        raise ChrxLooError(msg)
    return _LooInputs(archive=archive, panel=panel, nonpar_target=nonpar_target)


def _run_fold_beagle(  # noqa: PLR0913 — Beagle's CLI is a flat keyword list
    *,
    panel: ReferencePanel,
    target: Path,
    out_prefix: Path,
    threads: int,
    memory_gb: int,
    ne: int,
    fold: int,
    imputation_id: int,
) -> Path:
    """Run one non-PAR Beagle imputation for a masked fold; return the output VCF.

    Mirrors the production non-PAR region invocation (same ``ref=`` non-PAR panel
    subset, same ``map=``) so LOO measures the production mechanic. Streams
    Beagle's stderr into structlog. Raises :class:`ChrxLooError` on a non-zero
    exit or a missing output.
    """
    cmd = [
        "java",
        f"-Xmx{memory_gb}g",
        "-jar",
        str(panel.beagle_jar),
        f"ref={panel.chrx_nonpar_panel}",
        f"map={panel.map_for_chrom('X')}",
        f"gt={target}",
        f"out={out_prefix}",
        f"nthreads={threads}",
        f"ne={ne}",
        "impute=true",
    ]
    log = logger.bind(imputation_id=imputation_id, fold=fold)
    log.info("imputation.chrx_loo.fold.beagle.start", cmd=cmd)
    proc = subprocess.Popen(  # noqa: S603 — argv composed here, no shell
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stderr is not None  # noqa: S101 — stderr=PIPE guarantees non-None
    try:
        for line in proc.stderr:
            stripped = line.rstrip()
            if stripped:
                log.info("imputation.chrx_loo.fold.beagle.stderr", line=stripped)
    finally:
        rc = proc.wait()
    out_vcf = out_prefix.with_name(out_prefix.name + ".vcf.gz")
    if rc != 0 or not out_vcf.is_file():
        msg = f"Beagle failed for LOO fold {fold} (rc={rc}); output {out_vcf} missing"
        raise ChrxLooError(msg)
    restrict_file(out_vcf)
    return out_vcf


def _impute_fold(  # noqa: PLR0913 — per-fold Beagle config + the fold's held-out set
    inputs: _LooInputs,
    *,
    fold: int,
    fold_positions: frozenset[int],
    anchors: dict[int, int],
    threads: int,
    memory_gb: int,
    ne: int,
    imputation_id: int,
) -> list[LooAnchorResult]:
    """Mask one fold, re-impute it, and return the held-out anchor comparisons."""
    loo_dir = inputs.archive.chrx_loo_dir
    masked_target = loo_dir / f"fold_{fold}.target.vcf.gz"
    out_prefix = loo_dir / f"fold_{fold}"
    n_masked = write_masked_target(inputs.nonpar_target, masked_target, fold_positions)
    restrict_file(masked_target)
    logger.info(
        "imputation.chrx_loo.fold.start",
        imputation_id=imputation_id,
        fold=fold,
        masked=n_masked,
    )
    out_vcf = _run_fold_beagle(
        panel=inputs.panel,
        target=masked_target,
        out_prefix=out_prefix,
        threads=threads,
        memory_gb=memory_gb,
        ne=ne,
        fold=fold,
        imputation_id=imputation_id,
    )
    results = read_imputed_calls(out_vcf, anchors, fold_positions)
    logger.info(
        "imputation.chrx_loo.fold.complete",
        imputation_id=imputation_id,
        fold=fold,
        evaluated=len(results),
    )
    return results


def _write_report_artifact(archive: ImputationArchive, report: LooReport) -> Path:
    """Write the LOO report JSON under the run's ``loo/`` dir; return its path."""
    path = archive.chrx_loo_dir / "REPORT.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    restrict_file(path)
    return path


def _stamp_loo_qc_notes(conn: DuckDBPyConnection, report: LooReport) -> None:
    """Idempotently stamp ``[chrx_loo_concordance=…]`` on the imputed QC row.

    Mirrors the het guard's marker convention (chrx_qc.py): strip any prior LOO
    marker, then append the current one. A no-op when no imputed run exists yet
    (LOO run before import) — the report artifact + return value still carry it.
    """
    row = conn.execute(
        """
        SELECT sq.qc_id, sq.qc_notes
          FROM sample_qc sq
          JOIN ingestion_runs ir ON ir.run_id = sq.run_id
         WHERE CAST(ir.source AS VARCHAR) = 'beagle_imputed'
         ORDER BY sq.qc_id DESC
         LIMIT 1
        """,
    ).fetchone()
    if row is None:
        return
    qc_id = int(row[0])
    existing = str(row[1]) if row[1] is not None else ""
    cleaned = _LOO_NOTE_RE.sub("", existing).strip()
    conc = "na" if report.concordance is None else f"{report.concordance:.4f}"
    marker = (
        f"[chrx_loo_concordance={conc}@dconf{report.threshold:.2f}"
        f"/n{report.n_at_or_above_threshold}]"
    )
    new_notes = f"{cleaned} {marker}".strip() if cleaned else marker
    if new_notes != existing:
        conn.execute(
            "UPDATE sample_qc SET qc_notes = ? WHERE qc_id = ?",
            [new_notes, qc_id],
        )


def run_chrx_loo(  # noqa: PLR0913 — operational controls map 1:1 to the CLI surface
    imputation_id: int,
    *,
    n_folds: int = DEFAULT_N_FOLDS,
    dconf_threshold: float = DEFAULT_DCONF_THRESHOLD,
    threads: int | None = None,
    memory_gb: int = DEFAULT_MEMORY_GB,
    ne: int = DEFAULT_NE,
    duckdb_path: Path | None = None,
    archive_root: Path | None = None,
    panel_root: Path | None = None,
) -> LooReport:
    """Run 5-fold LOO validation of the male non-PAR chrX dosage gate (PR 5a).

    Reads the run's prepared male non-PAR target, partitions its typed anchors
    into ``n_folds`` disjoint folds, re-imputes each masked fold against the
    native non-PAR panel subset, and scores ``round(DS)`` against the held-out
    truth. Writes a JSON report under ``archive/imputation/run_<id>/loo/`` and
    idempotently stamps the headline concordance on the imputed ``sample_qc``
    row. Returns the :class:`LooReport`.

    A named long-op (per-fold structlog progress; scratch on the big disk, never
    ``/tmp``). The threshold is fixed, not searched — LOO measures the concordance
    achieved at ``dconf_threshold``; below the gate's bar is falsification.
    """
    settings = get_settings()
    duckdb_path = duckdb_path or settings.genome_duckdb_path
    archive_root = archive_root or settings.archive_path
    if not 0.5 <= dconf_threshold <= 1.0:  # noqa: PLR2004 — dconf ∈ [0.5, 1]
        msg = f"dconf_threshold must be between 0.5 and 1.0, got {dconf_threshold!r}"
        raise ValueError(msg)
    if threads is None:
        threads = default_threads()

    log = logger.bind(imputation_id=imputation_id, pipeline_version=LOO_PIPELINE_VERSION)
    log.info("imputation.chrx_loo.start", n_folds=n_folds, dconf_threshold=dconf_threshold)

    check_java_available()
    inputs = _resolve_loo_inputs(imputation_id, archive_root=archive_root, panel_root=panel_root)
    inputs.archive.chrx_loo_dir.mkdir(parents=True, exist_ok=True)

    anchors = read_haploid_anchors(inputs.nonpar_target)
    if not anchors:
        msg = f"non-PAR target {inputs.nonpar_target} has no usable haploid anchors"
        raise ChrxLooError(msg)
    folds = partition_folds(anchors.keys(), n_folds)
    log.info("imputation.chrx_loo.anchors", n_anchors=len(anchors), n_folds=n_folds)

    all_results: list[LooAnchorResult] = []
    for fold, fold_positions in enumerate(folds):
        if not fold_positions:
            continue
        all_results.extend(
            _impute_fold(
                inputs,
                fold=fold,
                fold_positions=fold_positions,
                anchors=anchors,
                threads=threads,
                memory_gb=memory_gb,
                ne=ne,
                imputation_id=imputation_id,
            ),
        )

    report = compute_loo_report(all_results, threshold=dconf_threshold, n_folds=n_folds)
    report_path = _write_report_artifact(inputs.archive, report)
    with duckdb_connection(duckdb_path) as conn:
        _stamp_loo_qc_notes(conn, report)
    log.info(
        "imputation.chrx_loo.complete",
        n_anchors=report.n_anchors,
        n_at_threshold=report.n_at_or_above_threshold,
        concordance=report.concordance,
        report=str(report_path),
    )
    return report


__all__ = [
    "DEFAULT_N_FOLDS",
    "LOO_PIPELINE_VERSION",
    "ChrxLooError",
    "LooAnchorResult",
    "LooCell",
    "LooReport",
    "compute_loo_report",
    "partition_folds",
    "read_haploid_anchors",
    "read_imputed_calls",
    "run_chrx_loo",
    "write_masked_target",
]
