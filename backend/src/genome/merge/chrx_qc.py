"""Post-merge chrX QC: the male non-PAR het-anomaly guard (PR 5a, finding-029).

Under M1 + R1 a male non-PAR chrX position is imputed as a diploid genotype and
stored homozygous-diploid; a *heterozygous* call there is biologically
impossible (males are hemizygous in non-PAR chrX). The corrected-dosage view
(``consensus_chrx_dosage_v``) flags those as ``male_nonpar_het_anomaly``. This
guard runs right after :func:`genome.merge.pipeline.merge_all` rebuilds the
consensus: it counts the anomalies, warns if any exist, and records the count on
the imputed ``sample_qc.qc_notes`` (an existing TEXT column) — keep + warn +
count, never a silent drop, and no new column.

A high count is the §7 falsifiability signal that the diploid-target imputation
is producing impossible male hets; the expected steady state is a small count
(near zero).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import structlog

from genome.db.init_schema import materialize_view

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

CHRX_DOSAGE_VIEW: Final[str] = "consensus_chrx_dosage_v"

# Idempotent marker appended to the imputed QC notes; re-merges replace it rather
# than stacking duplicates, and a count that drops back to zero clears it.
_CHRX_NOTE_RE: Final[re.Pattern[str]] = re.compile(r"\s*\[chrx_male_nonpar_het=\d+\]")


def apply_chrx_het_guard(conn: DuckDBPyConnection) -> int:
    """Count male non-PAR chrX het anomalies and record them on the imputed QC row.

    Materializes the chrX dosage view (idempotent — the view-only no-rebuild
    path, so this also self-heals a legacy DB that predates the view), counts
    ``male_nonpar_het_anomaly``, warns when any are present, and updates the
    imputed ``sample_qc.qc_notes`` idempotently. Returns the anomaly count, which
    is ``0`` whenever there is no male chrX imputed data — the steady state until
    chrX imputation lands.

    Designed to run inside the merge transaction so the recorded count is atomic
    with the consensus it describes.
    """
    materialize_view(conn, CHRX_DOSAGE_VIEW)
    row = conn.execute(
        f"SELECT COUNT(*) FROM {CHRX_DOSAGE_VIEW} WHERE male_nonpar_het_anomaly",  # noqa: S608 — constant identifier
    ).fetchone()
    count = int(row[0]) if row is not None and row[0] is not None else 0
    if count > 0:
        logger.warning("merge.chrx_male_nonpar_het_anomaly", count=count)
    _annotate_imputed_qc_notes(conn, count)
    return count


def _annotate_imputed_qc_notes(conn: DuckDBPyConnection, count: int) -> None:
    """Idempotently stamp ``[chrx_male_nonpar_het=N]`` on the imputed QC row.

    Targets the most recent ``beagle_imputed`` ``sample_qc`` row. A stale marker
    from a prior merge is stripped first, then the current marker is appended
    when ``count > 0`` (so a count that falls back to zero clears the note). A
    no-op when no imputed run exists yet.
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
    cleaned = _CHRX_NOTE_RE.sub("", existing).strip()
    if count > 0:
        marker = f"[chrx_male_nonpar_het={count}]"
        new_notes = f"{cleaned} {marker}".strip() if cleaned else marker
    else:
        new_notes = cleaned
    if new_notes != existing:
        conn.execute(
            "UPDATE sample_qc SET qc_notes = ? WHERE qc_id = ?",
            [new_notes or None, qc_id],
        )


__all__ = ["CHRX_DOSAGE_VIEW", "apply_chrx_het_guard"]
