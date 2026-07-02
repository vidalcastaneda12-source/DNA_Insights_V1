"""gnomAD v4.1.1 filtered allele-frequencies loader.

Streams the gnomAD v4.1.1 per-chromosome sites-only VCFs (exomes + genomes,
GRCh38, GCS-hosted, bgzipped + tabix-indexed) via cyvcf2 remote tabix
queries, filters every site against the user's own distinct
``(chrom, pos)`` positions in ``variants_master`` (the ``user_only`` filter,
adopted per finding-035), and chunk-loads the resulting per-variant
population-AF rows into ``gnomad_frequencies`` via PyArrow Table registration +
``INSERT ... SELECT`` (the project's locked bulk-load convention).

Sub-phase 5.5 — sixth loader after PharmGKB (5.1a), CPIC (5.1b),
ClinVar (5.2), GWAS Catalog (5.3), and PGS Catalog (5.4). Mirrors the
locked 5.1+ template where it can but diverges in three structural
ways the upstream distribution forces:

* **Two URLs per chromosome.** gnomAD v4 ships ``exomes`` and
  ``genomes`` as separate per-chromosome VCFs at distinct GCS paths;
  the loader reads both per chromosome and dedupes the joined row set
  by ``(chrom, pos, ref, alt)`` with first-write-wins (exomes wins
  because they iterate first).
* **Streamed remote, not downloaded.** Each per-chromosome VCF is
  several GB; the loader uses cyvcf2's remote-tabix mode (``VCF(url)``)
  so only the bytes inside the requested tabix ranges hit the wire.
  An audited HEAD request per ``(chrom, data_type)`` records the
  remote VCF open in ``audit_log`` even though no file is written to
  ``~/.cache/genome/annotations/``.
* **Per-chromosome resumability.** A full-genome run is wall-clock-
  hours; a single chromosome's network blip should not abort the
  whole job. Each chromosome's content lands under a freshly-allocated
  new ``source_version_id`` as it completes; the version-pointer flip
  in ``annotation_sources`` is deferred until every requested
  chromosome succeeds. A ``--resume`` invocation picks up the
  in-flight new source_version_id and runs the remaining chromosomes.

The filter set is the user's own distinct ``variants_master`` positions
(the ``user_only`` strategy, adopted per finding-035: VSC-User ruled
2026-06-21 after the consumer audit found every ``gnomad_frequencies``
reader inner-joins ``variants_master``, so the ClinVar/GWAS-only legs were
loaded but never read). CLAUDE.md "Things never to do" #3's
``(user U ClinVar U GWAS U PGS)`` union remains the documented upper bound
and the one-argument revert path — ``user_only`` is a strict subset of it.
The three-way ``(user U ClinVar U GWAS)`` strategy stays first-class in
:mod:`genome.annotate.filter_set`; the Phase 6 PGS extension gated on
``pgs_score_weights`` (finding-011) is moot while the filter is narrowed.

Supersession is via the ``annotation_sources`` pointer table
(finding-010 version-pointer pattern): the loader inserts new content
under a fresh ``source_version_id`` for the duration of the run, and
flips the pointer for ``gnomad`` to that id in one statement once the
full chrom set lands successfully. Partial-chromosome runs (the
``--chromosomes`` filter) intentionally do **not** flip the pointer —
the operator must run ``--resume`` to land the remaining chromosomes
before the new version becomes user-visible.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import structlog

from genome.annotate.filter_set import FilterSet, build_filter_set
from genome.annotate.registry import RefreshResult, register_loader
from genome.annotate.remote_tabix import (
    MAX_REMOTE_REGION_ATTEMPTS,
    RemoteReadStats,
    RemoteTabixIterationError,
    RemoteTabixLibcurlMissingError,
    _scan_for_htslib_errors,  # noqa: F401 — re-export for the gnomad public surface (tests)
    _StderrTap,  # noqa: F401 — re-export for the gnomad public surface (tests reference it)
    audited_head,
    check_libcurl_available,
    coalesce_positions,
    iter_remote_vcf_regions,
)
from genome.annotate.source_versions import (
    get_current_version,
    insert_source_version,
)
from genome.annotate.supersession import (
    commit_and_checkpoint,
    flip_to_new_version,
)
from genome.db.duckdb_conn import duckdb_connection
from genome.privacy.external_client import (
    _DEFAULT_TIMEOUT_S,
    ExternalCallError,
    ExternalCallsDisabledError,
    ExternalClient,
    is_external_enabled,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Upstream URLs (verified 2026-05-19).
#
# gnomAD v4.1.1 ships per-chromosome sites-only VCFs (bgzipped + tabix-
# indexed) at the GCS public bucket. Two data types per chromosome:
# ``exomes`` and ``genomes``. The remote VCFs are opened by cyvcf2 via
# htslib's libcurl plugin; only the bytes inside the requested tabix
# ranges hit the wire.
# ---------------------------------------------------------------------------

URL_VERIFIED_DATE: Final[str] = "2026-05-19"

GNOMAD_VERSION: Final[str] = "4.1.1"
"""Locked source release. CLI ``--version VERSION`` overrides at refresh time."""

GNOMAD_URL_TEMPLATE: Final[str] = (
    "https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/vcf/"
    "{data_type}/gnomad.{data_type}.v4.1.sites.chr{chrom}.vcf.bgz"
)
"""Per-chromosome remote VCF URL.

Two substitutions: ``data_type`` (``exomes`` or ``genomes``) and
``chrom`` (``1`` ... ``22`` or ``X``). gnomAD's GCS paths use the
unprefixed chromosome label (``chr1`` not ``chrom_1``).
"""

GNOMAD_POPULATIONS: Final[tuple[str, ...]] = (
    "afr",
    "ami",
    "amr",
    "asj",
    "eas",
    "fin",
    "mid",
    "nfe",
    "sas",
    "oth",
)
"""gnomAD v4 inferred-ancestry-group labels, in schema column order.

Mirrors the ``af_<pop>`` columns on ``gnomad_frequencies`` exactly
(after PR #46 which added ``af_mid``). Order matters for the row tuple
construction in :func:`_insert_chunk`.
"""

# gnomAD v4 renamed the "Other / unspecified" inferred-ancestry-group
# from ``oth`` to ``remaining`` in the public VCF INFO keys but the
# schema column ``af_oth`` (PR #46-era convention) is unchanged. The
# loader keeps the schema label and reads the new VCF INFO key when
# projecting a record. All other populations map identity.
_POP_TO_VCF_INFO_SUFFIX: Final[dict[str, str]] = {
    "afr": "afr",
    "ami": "ami",
    "amr": "amr",
    "asj": "asj",
    "eas": "eas",
    "fin": "fin",
    "mid": "mid",
    "nfe": "nfe",
    "sas": "sas",
    "oth": "remaining",
}

DEFAULT_BATCH_SIZE: Final[int] = 50_000
"""Bulk-insert chunk size.

50K rows is comfortable as a PyArrow Table working set (~25 MB
across the 20-column schema) and large enough to amortize the
per-INSERT overhead across many millions of variants.
"""

DEFAULT_COALESCE_DISTANCE_BP: Final[int] = 50000
"""Default tabix-range coalescing gap in base pairs.

Adjacent filter positions within 50 kb merge into one tabix range so
the remote VCF is queried in larger contiguous spans rather than a
flood of single-position queries. Tunable per refresh via the CLI
``--coalesce-distance N`` flag.

50000 was selected after real-data verification at 1000 bp produced
630+ HTTP/2 framing reopens on chromosome 1 alone within one hour;
50 kb dropped that to ~2 reopens per chromosome across the full
genome and completed the full run in 14.6 h. See
``docs/findings/finding-012-coalesce-distance-and-http2-reliability.md``.
"""

DEFAULT_PARALLEL_JOBS: Final[int] = 8
"""Default number of concurrent per-chromosome worker processes.

The full-genome load is network-latency-bound: each worker streams one
chromosome's remote VCFs (exomes + genomes) from GCS, so N workers keep
N tabix range requests in flight at once instead of one. 8 is a
conservative default that delivers a large speedup over the ~14.6 h
single-stream baseline while staying gentle on the uplink and on gnomAD's
HTTP/2 framing (finding-012); the CLI ``--jobs N`` flag tunes it. The
library default in :func:`load` / :func:`refresh` stays ``1`` (sequential)
so existing callers and the registry bare-form are unchanged.
"""

SUPPORTED_CHROMS: Final[tuple[str, ...]] = (*(str(n) for n in range(1, 23)), "X")
"""Canonical autosomal + X chromosomes loaded from gnomAD.

Y and MT are intentionally excluded: gnomAD v4 does not ship
high-confidence allele frequencies for those chromosomes in the
public per-chromosome VCFs used here. The CLI ``--chromosomes LIST``
flag filters within this set.
"""

SOURCE_DB: Final[str] = "gnomad"
_TARGET_TABLE: Final[str] = "gnomad_frequencies"
_PREFLIGHT_RESOURCE_ID: Final[str] = "gnomad_libcurl_preflight"
_REMOTE_OPEN_RESOURCE_ID: Final[str] = "gnomad_remote_vcf_open"


# Arrow schema used by ``_insert_chunk``. Column order matches the
# INSERT column list constructed below. Population-AF columns appear
# in :data:`GNOMAD_POPULATIONS` order between ``af_global``/``ac/an``
# and the ``filter_status`` / source-tag pair.
_ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("freq_id", pa.int64(), nullable=False),
        pa.field("rsid", pa.string()),
        pa.field("chrom", pa.string()),
        pa.field("pos_grch38", pa.int64()),
        pa.field("ref_allele", pa.string()),
        pa.field("alt_allele", pa.string()),
        pa.field("af_global", pa.float64()),
        pa.field("ac_global", pa.int32()),
        pa.field("an_global", pa.int32()),
        *(pa.field(f"af_{pop}", pa.float64()) for pop in GNOMAD_POPULATIONS),
        pa.field("filter_status", pa.string()),
        pa.field("source_version_id", pa.int64(), nullable=False),
        pa.field("retrieval_date", pa.timestamp("us"), nullable=False),
    ],
)


# ``_ARROW_SCHEMA`` without the leading ``freq_id`` field (index 0). The
# parallel worker stages filtered rows to Parquet without ``freq_id``; the
# parent assigns ``freq_id`` at merge time (:func:`_merge_chromosome_parquet`)
# so the surrogate key stays gap-free under the single DuckDB writer.
_STAGING_SCHEMA: Final[pa.Schema] = _ARROW_SCHEMA.remove(0)


# ---------------------------------------------------------------------------
# Result dataclass.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GnomadLoadResult:
    """Outcome of one :func:`load` invocation.

    Carries every drift identifier the runbook will compare across
    releases (per-chrom row counts, filter-set composition, AF buckets
    on the user-variant overlap, per-population AF presence) plus the
    operational status (which chromosomes succeeded vs failed, whether
    the pointer flipped, wall-clock wall).

    ``reopens_total`` is the success-path htslib-reopen run total
    (finding-012 #12) — a tolerance-banded network signal, NOT a
    byte-exact drift anchor (see ``docs/runbooks/annotations.md``). It is
    authoritative only on a clean run (``chromosomes_failed`` empty); on a
    failed run it under-reports, because a dead spawn worker's partial
    reopen count cannot be recovered (the parallel path rebuilds a failed
    ``_ChromResult`` with ``reopens=0`` — see :func:`_run_workers`). The
    sequential path does surface a failed chromosome's partial (its
    run-level accumulator is mutated at the reopen site before the raise).
    Do not build a threshold-gate on this field without first carrying the
    dead worker's partial.
    """

    version_label: str
    source_version_id: int | None
    pointer_flipped: bool
    rows_loaded: int
    distinct_variants_per_chrom: dict[str, int]
    filter_set_composition: dict[str, int]
    match_rate: float
    af_buckets_user_overlap: dict[str, int]
    mean_af_user_overlap: float
    pop_af_presence: dict[str, int]
    chromosomes_succeeded: tuple[str, ...]
    chromosomes_failed: tuple[str, ...]
    wall_clock_seconds: float
    reopens_total: int


# ---------------------------------------------------------------------------
# Custom errors.
# ---------------------------------------------------------------------------


# finding-012 #11 moved the error types to ``remote_tabix``. These aliases
# preserve the gnomad public surface — callers and tests still raise/catch
# ``GnomadLibcurlMissingError`` / ``GnomadRemoteIterationError``. They are the
# *same* classes the shared machinery raises, so the catches keep working.
GnomadLibcurlMissingError = RemoteTabixLibcurlMissingError
GnomadRemoteIterationError = RemoteTabixIterationError


# (htslib transient-error recovery moved to genome.annotate.remote_tabix —
#  finding-012 #11. _StderrTap, _scan_for_htslib_errors, _HTSLIB_ERROR_TOKENS,
#  and MAX_REMOTE_REGION_ATTEMPTS are imported + re-exported at module top.)


# ---------------------------------------------------------------------------
# Pre-flight.
# ---------------------------------------------------------------------------


def _check_libcurl_available() -> None:
    """Confirm cyvcf2 can open the gnomAD chr22 exomes VCF (libcurl pre-flight).

    Thin wrapper over
    :func:`genome.annotate.remote_tabix.check_libcurl_available` pinned to
    the gnomAD chr22 exomes URL and a known-tiny probe range
    (``chr22:10500000-10500100`` — a low-coverage window near the short-arm
    acrocentric boundary, free server-side). Raises
    :class:`GnomadLibcurlMissingError` (the remote_tabix error type,
    re-exported) on failure.
    """
    url = GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22")
    check_libcurl_available(url, "chr22:10500000-10500100")


# ---------------------------------------------------------------------------
# Filter-set build.
# ---------------------------------------------------------------------------


# finding-012 #11 moved the filter-set builder to genome.annotate.filter_set
# and parameterised it on ``strategy``. gnomAD's active filter is the
# ``"user_only"`` strategy — the user's own distinct ``variants_master``
# ``(chrom, pos_grch38)`` positions — adopted per finding-035 (VSC-User ruled
# 2026-06-21) because every ``gnomad_frequencies`` consumer inner-joins
# ``variants_master``, so the ClinVar/GWAS-only legs were loaded but never read.
# The ``"three_way"`` ``(user U ClinVar U GWAS)`` strategy remains first-class in
# ``genome.annotate.filter_set`` as the one-argument revert path + the future PGS
# extension (finding-011). The alias + wrapper preserve the names the tests
# import (``_FilterSet``, ``_build_filter_set``) and that ``load`` calls.
_FilterSet = FilterSet


def _build_filter_set(conn: DuckDBPyConnection) -> FilterSet:
    """Compute gnomAD's ``user_only`` filter set.

    Thin wrapper over :func:`genome.annotate.filter_set.build_filter_set`
    pinned to the ``"user_only"`` strategy and gnomAD's
    :data:`SUPPORTED_CHROMS` (1-22, X). The filter is the user's own distinct
    ``variants_master`` ``(chrom, pos_grch38)`` positions — adopted per
    finding-035 (VSC-User ruled 2026-06-21) after the consumer audit found that
    every ``gnomad_frequencies`` reader inner-joins ``variants_master``, so the
    ClinVar/GWAS-only positions were loaded but never read.

    The ``"three_way"`` ``(user U ClinVar U GWAS)`` strategy stays first-class in
    :mod:`genome.annotate.filter_set` as the revert path and the eventual PGS
    extension (a Phase 6 follow-up gated on ``pgs_score_weights`` would extend
    coverage without a version bump). See finding-035 for the consumer audit and
    finding-011 for the three-way-vs-four-way design discussion.
    """
    return build_filter_set(conn, strategy="user_only", supported_chroms=SUPPORTED_CHROMS)


# ---------------------------------------------------------------------------
# Position coalescing — re-export.
# ---------------------------------------------------------------------------

# finding-012 #11 moved the implementation to remote_tabix. The gnomad alias
# preserves the name the tests import and that ``load`` calls.
_coalesce_positions = coalesce_positions


# ---------------------------------------------------------------------------
# Per-record extraction.
# ---------------------------------------------------------------------------


def _info_get_float(info: object, key: str) -> float | None:
    """Return ``info[key]`` as a float when present, else ``None``.

    cyvcf2's ``record.INFO`` raises ``KeyError`` on missing keys
    (vs returning ``None``), so the helper wraps the lookup in a
    try/except. Empty / NaN values that float() would tolerate map
    to None too — a "missing INFO field" semantic is what the
    schema expects.
    """
    try:
        value = info[key]  # type: ignore[index]
    except (KeyError, TypeError):
        return None
    if value is None:
        return None
    import math  # noqa: PLC0415 — local import keeps module-level surface narrow

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result


def _info_get_int(info: object, key: str) -> int | None:
    """Return ``info[key]`` as an int when present, else ``None``."""
    try:
        value = info[key]  # type: ignore[index]
    except (KeyError, TypeError):
        return None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_to_row(
    record: object,
    source_version_id: int,
    retrieval_datetime: datetime,
) -> dict[str, object] | None:
    """Project one cyvcf2 record into a row destined for ``gnomad_frequencies``.

    Reads the global trio (``AF`` / ``AC`` / ``AN``) plus per-population
    AFs (``AF_<pop>`` per :data:`_POP_TO_VCF_INFO_SUFFIX`) out of the
    record's INFO dict. The per-chromosome v4.1 sites VCFs (both
    ``exomes`` and ``genomes`` variants) expose these plain-suffix keys
    directly — the ``_joint`` prefix exists only on a separate combined
    release and is not present here. ``af_oth`` reads from the renamed
    ``AF_remaining`` key (gnomAD v4 retired the ``oth`` label). The
    Amish population (``ami``) is absent from the exomes VCF in v4.1
    and so resolves to ``None`` on exomes records; genomes records
    carry it. Missing INFO keys map to ``None``. The ``filter_status``
    column carries the FILTER token (``"PASS"`` when cyvcf2 reports
    ``None``); other columns come from ``CHROM`` / ``POS`` / ``REF`` /
    ``ALT``.

    Returns ``None`` defensively for multi-allelic records — gnomAD's
    public per-chromosome VCFs are pre-split, so this is invariant
    insurance rather than a real code path. Records with empty or
    multi-element ``ALT`` arrays are dropped.
    """
    alts: tuple[object, ...] = tuple(getattr(record, "ALT", ()) or ())
    if len(alts) != 1:
        return None
    alt = alts[0]
    if not isinstance(alt, str) or not alt:
        return None

    chrom_raw = getattr(record, "CHROM", None)
    if not isinstance(chrom_raw, str) or not chrom_raw:
        return None
    chrom = chrom_raw.removeprefix("chr")

    pos = getattr(record, "POS", None)
    if not isinstance(pos, int):
        try:
            pos = int(pos)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    ref = getattr(record, "REF", None)
    if not isinstance(ref, str) or not ref:
        return None

    info = getattr(record, "INFO", {})
    filter_value = getattr(record, "FILTER", None)
    filter_status = "PASS" if filter_value is None else str(filter_value)

    row: dict[str, object] = {
        "rsid": None,
        "chrom": chrom,
        "pos_grch38": int(pos),
        "ref_allele": ref,
        "alt_allele": alt,
        "af_global": _info_get_float(info, "AF"),
        "ac_global": _info_get_int(info, "AC"),
        "an_global": _info_get_int(info, "AN"),
        "filter_status": filter_status,
        "source_version_id": source_version_id,
        "retrieval_date": retrieval_datetime.astimezone(UTC).replace(tzinfo=None),
    }
    for pop in GNOMAD_POPULATIONS:
        row[f"af_{pop}"] = _info_get_float(info, f"AF_{_POP_TO_VCF_INFO_SUFFIX[pop]}")
    return row


# ---------------------------------------------------------------------------
# Bulk-insert helpers.
# ---------------------------------------------------------------------------


def _next_freq_id(conn: DuckDBPyConnection) -> int:
    """``COALESCE(MAX(freq_id), 0) + 1``."""
    row = conn.execute(
        f"SELECT COALESCE(MAX(freq_id), 0) FROM {_TARGET_TABLE}",  # noqa: S608 — module constant
    ).fetchone()
    return int(row[0]) + 1 if row is not None else 1


def _rows_to_arrow_columns(rows: list[dict[str, object]]) -> dict[str, pa.Array]:
    """Build the per-column PyArrow arrays for every column except ``freq_id``.

    Shared by :func:`_insert_batch` (which prepends the ``freq_id`` column for
    the sequential in-process insert) and the parallel worker's Parquet writer
    (which stages rows without ``freq_id``, the parent assigning it at merge
    time). Column set + dtypes mirror :data:`_ARROW_SCHEMA` minus ``freq_id``.
    """
    cols: dict[str, pa.Array] = {
        "rsid": pa.array([r["rsid"] for r in rows], type=pa.string()),
        "chrom": pa.array([r["chrom"] for r in rows], type=pa.string()),
        "pos_grch38": pa.array([r["pos_grch38"] for r in rows], type=pa.int64()),
        "ref_allele": pa.array([r["ref_allele"] for r in rows], type=pa.string()),
        "alt_allele": pa.array([r["alt_allele"] for r in rows], type=pa.string()),
        "af_global": pa.array([r["af_global"] for r in rows], type=pa.float64()),
        "ac_global": pa.array([r["ac_global"] for r in rows], type=pa.int32()),
        "an_global": pa.array([r["an_global"] for r in rows], type=pa.int32()),
    }
    for pop in GNOMAD_POPULATIONS:
        col = f"af_{pop}"
        cols[col] = pa.array([r[col] for r in rows], type=pa.float64())
    cols["filter_status"] = pa.array([r["filter_status"] for r in rows], type=pa.string())
    cols["source_version_id"] = pa.array(
        [r["source_version_id"] for r in rows],
        type=pa.int64(),
    )
    cols["retrieval_date"] = pa.array(
        [r["retrieval_date"] for r in rows],
        type=pa.timestamp("us"),
    )
    return cols


def _insert_batch(
    conn: DuckDBPyConnection,
    rows: list[dict[str, object]],
    *,
    base_id: int,
) -> int:
    """Bulk-insert one batch into ``gnomad_frequencies``.

    Builds a PyArrow Table with one column per destination column,
    registers it under a temp name, and runs
    ``INSERT INTO gnomad_frequencies (...) SELECT ... FROM <temp>``.
    ``freq_id`` is allocated as ``range(base_id, base_id + n)``.
    """
    if not rows:
        return 0
    n = len(rows)
    table_data: dict[str, pa.Array] = {
        "freq_id": pa.array(range(base_id, base_id + n), type=pa.int64()),
        **_rows_to_arrow_columns(rows),
    }
    table = pa.table(table_data, schema=_ARROW_SCHEMA)

    pop_cols = ", ".join(f"af_{pop}" for pop in GNOMAD_POPULATIONS)
    try:
        conn.register("_gnomad_stage_arrow", table)
        conn.execute(
            f"""
            INSERT INTO {_TARGET_TABLE} (
                freq_id, rsid, chrom, pos_grch38, ref_allele, alt_allele,
                af_global, ac_global, an_global,
                {pop_cols},
                filter_status, source_version_id, retrieval_date
            )
            SELECT
                freq_id, rsid, chrom::chromosome_enum, pos_grch38,
                ref_allele, alt_allele,
                af_global, ac_global, an_global,
                {pop_cols},
                filter_status, source_version_id, retrieval_date
              FROM _gnomad_stage_arrow
            """,  # noqa: S608 — table + column lists are module-controlled
        )
    finally:
        conn.unregister("_gnomad_stage_arrow")
    return n


# ---------------------------------------------------------------------------
# Per-chromosome iteration.
# ---------------------------------------------------------------------------


def _audited_head(client: ExternalClient, url: str) -> None:
    """Issue an audited HEAD against ``url`` for the audit-log paper trail.

    Thin wrapper over :func:`genome.annotate.remote_tabix.audited_head`
    pinned to gnomAD's resource id + event prefix, so the audit row's
    ``resource_id`` stays ``gnomad_remote_vcf_open`` and the non-fatal log
    event stays ``gnomad.audited_head_non_fatal``.
    """
    audited_head(
        client,
        url,
        resource_id=_REMOTE_OPEN_RESOURCE_ID,
        event_prefix=SOURCE_DB,
        log=logger,
    )


def _load_chromosome(  # noqa: C901, PLR0913 — irreducible per-chrom configuration
    conn: DuckDBPyConnection,
    audited_client: ExternalClient,
    chrom: str,
    regions: list[tuple[int, int]],
    filter_positions: frozenset[int],
    source_version_id: int,
    retrieval_datetime: datetime,
    batch_size: int,
    *,
    reopen_stats: RemoteReadStats | None = None,
) -> int:
    """Iterate gnomAD's remote VCFs for ``chrom`` and insert filtered rows.

    For each ``data_type`` in ``("exomes", "genomes")`` the loader issues
    an audited HEAD (paper trail) and streams the remote VCF via
    :func:`genome.annotate.remote_tabix.iter_remote_vcf_regions`, which
    owns the open → iterate → detect-corruption → reopen → retry loop (the
    gnomAD-on-GCS HTTP/2 framing failure mode) and emits the
    ``gnomad.remote_open`` / ``gnomad.chrom.htslib_recover`` events. Each
    yielded record is consumed here:

    * Reject records whose position is not in ``filter_positions`` (the
      coalesced ranges cover gaps between actual filter positions; the
      membership check is the precise filter).
    * Build per-row dicts via :func:`_record_to_row`; dedup by
      ``(chrom, pos, ref, alt)`` with first-write-wins (exomes iterates
      first → exomes-derived AF wins on overlapping sites). The dedup set
      is shared across the two data types and across retry attempts, so a
      record re-yielded after a mid-region reopen lands at most once.
    * Flush every ``batch_size`` rows via :func:`_insert_batch`.

    Returns the number of rows inserted for the chromosome.
    """
    seen_keys: set[tuple[str, int, str, str]] = set()
    pending: list[dict[str, object]] = []
    inserted = 0
    base_id = _next_freq_id(conn)

    def _flush() -> None:
        nonlocal inserted, base_id, pending
        if not pending:
            return
        n = _insert_batch(conn, pending, base_id=base_id)
        inserted += n
        base_id += n
        logger.info(
            "gnomad.bulk_insert.chunk",
            chrom=chrom,
            rows=n,
            cumulative=inserted,
        )
        pending = []

    def _consume_record(record: object) -> None:
        row = _record_to_row(record, source_version_id, retrieval_datetime)
        if row is None:
            return
        pos_obj = row["pos_grch38"]
        if not isinstance(pos_obj, int):
            return
        pos = pos_obj
        if pos not in filter_positions:
            return
        key = (
            str(row["chrom"]),
            pos,
            str(row["ref_allele"]),
            str(row["alt_allele"]),
        )
        if key in seen_keys:
            return
        seen_keys.add(key)
        pending.append(row)
        if len(pending) >= batch_size:
            _flush()

    region_strings = [f"chr{chrom}:{start}-{end}" for start, end in regions]
    # exomes iterates first so its AF wins on (chrom, pos, ref, alt)
    # overlaps via the seen_keys first-write-wins dedup.
    for data_type in ("exomes", "genomes"):
        url = GNOMAD_URL_TEMPLATE.format(data_type=data_type, chrom=chrom)
        _audited_head(audited_client, url)
        for record in iter_remote_vcf_regions(
            url,
            region_strings,
            event_prefix=SOURCE_DB,
            log_context={"chrom": chrom, "data_type": data_type},
            log=logger,
            reopen_stats=reopen_stats,
        ):
            _consume_record(record)

    _flush()
    return inserted


# ---------------------------------------------------------------------------
# Resume helpers.
# ---------------------------------------------------------------------------


def _find_in_flight_source_version_id(
    conn: DuckDBPyConnection,
    version: str,
) -> int | None:
    """Return a partially-loaded new ``source_version_id`` for ``version`` if any.

    "In-flight" means an ``annotation_source_versions`` row exists for
    ``(source_db='gnomad', version=<version>)`` but the
    ``annotation_sources`` pointer doesn't name it yet — i.e. a prior
    run inserted some chromosomes' content and exited without flipping
    the pointer (the partial-chromosome semantic). Returns the
    largest such id so a ``--resume`` continues against the most
    recent attempt; returns ``None`` when no in-flight row exists.
    """
    row = conn.execute(
        """
        SELECT asv.source_version_id
          FROM annotation_source_versions asv
          LEFT JOIN annotation_sources a
            ON a.source_db = 'gnomad'
           AND a.current_source_version_id = asv.source_version_id
         WHERE asv.source_db = 'gnomad'
           AND asv.version = ?
           AND a.source_db IS NULL
         ORDER BY asv.source_version_id DESC
         LIMIT 1
        """,
        [version],
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _populated_chroms(conn: DuckDBPyConnection, source_version_id: int) -> set[str]:
    """Chromosomes that already have rows under ``source_version_id``."""
    rows = conn.execute(
        f"""
        SELECT DISTINCT chrom::VARCHAR
          FROM {_TARGET_TABLE}
         WHERE source_version_id = ?
        """,  # noqa: S608 — module constant
        [source_version_id],
    ).fetchall()
    return {str(r[0]) for r in rows}


# ---------------------------------------------------------------------------
# Rollback / cleanup helper.
# ---------------------------------------------------------------------------


def _cleanup_orphan_version_row(
    conn: DuckDBPyConnection,
    source_version_id: int,
) -> None:
    """Best-effort delete of an orphan ``annotation_source_versions`` row.

    Same shape as the PharmGKB / CPIC / ClinVar / GWAS Catalog / PGS
    Catalog helpers — called when a freshly-allocated gnomad version
    row has nothing referencing it after the per-chromosome load loop
    exits (a chrom-grain partial run that landed zero rows, or a
    failure path that never reached a successful per-chrom commit).
    The row was inserted by :func:`insert_source_version` in its own
    (already-committed) transaction, so the loop's per-chromosome
    ``conn.rollback()`` cannot undo it; finding-015 documents the
    v6/v7/v8/v10 audit-trail anomaly this helper prevents going
    forward.

    The DELETE is FK-safe because the trigger condition is "zero
    ``gnomad_frequencies`` rows under this ``source_version_id``" —
    a freshly-allocated id whose per-chrom loop never reached a
    successful commit. The caller also guards on ``not
    pointer_flipped`` so the active version is never removed.
    Failures during cleanup are swallowed and logged; the caller is
    already raising or returning, and the orphan can be cleaned up
    manually in the worst case.
    """
    try:
        conn.execute(
            "DELETE FROM annotation_source_versions WHERE source_version_id = ?",
            [source_version_id],
        )
    except Exception:  # noqa: BLE001 — best-effort cleanup; caller has already raised/returned
        logger.warning(
            "gnomad.cleanup.orphan_version_row_delete_failed",
            source_version_id=source_version_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Post-load summary helpers.
# ---------------------------------------------------------------------------


def _summarize_run(
    conn: DuckDBPyConnection,
    source_version_id: int,
) -> tuple[
    int,
    dict[str, int],
    float,
    dict[str, int],
    float,
    dict[str, int],
]:
    """Compute the locked drift identifiers for the just-loaded source version.

    Returns ``(rows_loaded, distinct_variants_per_chrom, match_rate,
    af_buckets_user_overlap, mean_af_user_overlap, pop_af_presence)``.

    * ``rows_loaded`` — total rows under ``source_version_id``.
    * ``distinct_variants_per_chrom`` — ``COUNT(*) GROUP BY chrom``;
      for the gnomAD table this is also the per-chrom row count
      (one row per (chrom, pos, ref, alt) by construction).
    * ``match_rate`` — fraction of distinct ``variants_master``
      positions that have at least one gnomAD AF row.
    * ``af_buckets_user_overlap`` — AF distribution of gnomAD rows
      that share a ``(chrom, pos_grch38)`` with at least one
      ``variants_master`` row, bucketed by the documented breakpoints
      (``< 0.001`` / ``[0.001, 0.01)`` / ``[0.01, 0.05)`` /
      ``[0.05, 0.5]`` / ``> 0.5``).
    * ``mean_af_user_overlap`` — mean ``af_global`` over the same
      overlap subset (NULLs dropped).
    * ``pop_af_presence`` — per-population count of rows where
      ``af_<pop> IS NOT NULL``.
    """
    rows_row = conn.execute(
        f"SELECT COUNT(*) FROM {_TARGET_TABLE} WHERE source_version_id = ?",  # noqa: S608
        [source_version_id],
    ).fetchone()
    rows_loaded = int(rows_row[0]) if rows_row is not None else 0

    per_chrom_rows = conn.execute(
        f"""
        SELECT chrom::VARCHAR, COUNT(*)
          FROM {_TARGET_TABLE}
         WHERE source_version_id = ?
         GROUP BY 1
        """,  # noqa: S608
        [source_version_id],
    ).fetchall()
    distinct_per_chrom = {str(c): int(n) for c, n in per_chrom_rows}

    user_total_row = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT chrom, pos_grch38 FROM variants_master
        )
        """,
    ).fetchone()
    user_total = int(user_total_row[0]) if user_total_row is not None else 0

    if user_total > 0:
        overlap_count_row = conn.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT DISTINCT vm.chrom, vm.pos_grch38
                  FROM variants_master vm
                  JOIN {_TARGET_TABLE} g
                    ON g.chrom = vm.chrom
                   AND g.pos_grch38 = vm.pos_grch38
                 WHERE g.source_version_id = ?
            )
            """,  # noqa: S608
            [source_version_id],
        ).fetchone()
        overlap_count = int(overlap_count_row[0]) if overlap_count_row is not None else 0
        match_rate = overlap_count / user_total
    else:
        match_rate = 0.0

    buckets_query = f"""
        SELECT
            COUNT(*) FILTER (WHERE g.af_global < 0.001) AS lt_0001,
            COUNT(*) FILTER (
                WHERE g.af_global >= 0.001 AND g.af_global < 0.01
            ) AS bucket_a,
            COUNT(*) FILTER (
                WHERE g.af_global >= 0.01 AND g.af_global < 0.05
            ) AS bucket_b,
            COUNT(*) FILTER (
                WHERE g.af_global >= 0.05 AND g.af_global <= 0.5
            ) AS bucket_c,
            COUNT(*) FILTER (WHERE g.af_global > 0.5) AS gt_05,
            AVG(g.af_global) AS mean_af
          FROM {_TARGET_TABLE} g
          JOIN variants_master vm
            ON g.chrom = vm.chrom
           AND g.pos_grch38 = vm.pos_grch38
         WHERE g.source_version_id = ?
           AND g.af_global IS NOT NULL
    """  # noqa: S608
    bucket_row = conn.execute(buckets_query, [source_version_id]).fetchone()
    if bucket_row is None:
        af_buckets = {
            "lt_0.001": 0,
            "0.001_to_0.01": 0,
            "0.01_to_0.05": 0,
            "0.05_to_0.5": 0,
            "gt_0.5": 0,
        }
        mean_af = 0.0
    else:
        af_buckets = {
            "lt_0.001": int(bucket_row[0] or 0),
            "0.001_to_0.01": int(bucket_row[1] or 0),
            "0.01_to_0.05": int(bucket_row[2] or 0),
            "0.05_to_0.5": int(bucket_row[3] or 0),
            "gt_0.5": int(bucket_row[4] or 0),
        }
        mean_af = float(bucket_row[5]) if bucket_row[5] is not None else 0.0

    pop_presence: dict[str, int] = {}
    for pop in GNOMAD_POPULATIONS:
        pop_row = conn.execute(
            f"""
            SELECT COUNT(*) FROM {_TARGET_TABLE}
             WHERE source_version_id = ?
               AND af_{pop} IS NOT NULL
            """,  # noqa: S608 — pop value comes from the locked module constant
            [source_version_id],
        ).fetchone()
        pop_presence[pop] = int(pop_row[0]) if pop_row is not None else 0

    return (
        rows_loaded,
        distinct_per_chrom,
        match_rate,
        af_buckets,
        mean_af,
        pop_presence,
    )


# ---------------------------------------------------------------------------
# Parallel per-chromosome load (process pool + staged-Parquet merge).
#
# The full-genome load is network-latency-bound: the sequential path streams
# one chromosome's exomes-then-genomes VCFs at a time, so exactly one tabix
# range request is ever in flight. The parallel path runs N chromosomes
# concurrently in worker *processes* (not threads — iter_remote_vcf_regions'
# _StderrTap redirects process-global fd 2 to detect HTTP/2 corruption, which
# is unsafe to share across threads; spawn, not fork, because the parent holds
# an open DuckDB connection + httpx/ExternalClient that fork would leak).
# Workers stage filtered rows (no freq_id) to per-chromosome Parquet files;
# the parent — the single DuckDB writer — merges them serially in canonical
# order, so every output row, dedup decision, and the gap-free freq_id
# allocation match the sequential path exactly.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ChromTask:
    """One chromosome's parallel-load work unit (picklable for spawn workers).

    Carries everything a worker process needs to stream + filter + dedup one
    chromosome without touching the database or the audited client: the
    pre-formatted tabix region strings, the exact filter positions, the
    staging directory to write its Parquet file into, and the row-tagging
    metadata (``source_version_id`` / ``retrieval_datetime``) the parent
    allocated before dispatch. All fields are plain picklable types.
    """

    chrom: str
    region_strings: list[str]
    filter_positions: frozenset[int]
    source_version_id: int
    retrieval_datetime: datetime
    batch_size: int
    staging_dir: str


@dataclass(frozen=True, slots=True)
class _ChromResult:
    """Outcome of one :func:`_stream_chromosome_to_parquet` worker call.

    ``status`` is ``"ok"`` or ``"failed"``. On success ``parquet_path`` names
    the staged file (or ``None`` when the chromosome produced zero rows) and
    ``row_count`` is the deduped row count. On failure ``error`` carries the
    original exception — constructed parent-side in :func:`_run_workers` from
    the worker's propagated (pickled-and-re-raised) exception — so the
    orchestrator can re-raise it faithfully, preserving the sequential
    loader's "log the drift summary, then raise the cause" contract.
    """

    chrom: str
    parquet_path: str | None
    row_count: int
    status: str
    error: BaseException | None = None
    reopens: int = 0


def _stream_chromosome_to_parquet(task: _ChromTask) -> _ChromResult:  # noqa: C901 — irreducible filter+dedup+stream loop (mirrors _load_chromosome)
    """Worker: stream one chromosome's gnomAD VCFs into a staged Parquet file.

    Runs in a separate (spawned) process — opens NO DuckDB connection and NO
    audited client (the parent issues the audited HEADs and owns every DB
    write). For each ``data_type`` in ``("exomes", "genomes")`` it streams via
    :func:`iter_remote_vcf_regions` (whose per-process ``_StderrTap`` HTTP/2
    detector is safe here because each process has its own fd 2), applies the
    exact filter + ``(chrom, pos, ref, alt)`` exomes-win dedup as the
    sequential :func:`_load_chromosome`, and appends ``freq_id``-less rows to a
    :class:`pyarrow.parquet.ParquetWriter`.

    Returns an ``"ok"`` :class:`_ChromResult` (``parquet_path=None`` when zero
    rows landed). On any streaming error it closes a partial writer and
    re-raises, letting the executor propagate the exception to the parent,
    where :func:`_run_workers` records it as a ``"failed"`` result.
    """
    seen_keys: set[tuple[str, int, str, str]] = set()
    pending: list[dict[str, object]] = []
    row_count = 0
    # One accumulator for the whole worker — allocated OUTSIDE the
    # ``for data_type`` loop so exomes and genomes reopens both sum into it
    # (finding-012 #12; the parent aggregates each worker's ``reopens``).
    stats = RemoteReadStats()
    parquet_path = str(Path(task.staging_dir) / f"gnomad_chr{task.chrom}.parquet")
    writer: pq.ParquetWriter | None = None

    def _flush() -> None:
        nonlocal pending, writer, row_count
        if not pending:
            return
        table = pa.table(_rows_to_arrow_columns(pending), schema=_STAGING_SCHEMA)
        if writer is None:
            writer = pq.ParquetWriter(parquet_path, _STAGING_SCHEMA)  # type: ignore[no-untyped-call]  # pyarrow.parquet untyped
        writer.write_table(table)  # type: ignore[no-untyped-call]  # pyarrow.parquet untyped
        row_count += len(pending)
        pending = []

    try:
        for data_type in ("exomes", "genomes"):
            url = GNOMAD_URL_TEMPLATE.format(data_type=data_type, chrom=task.chrom)
            for record in iter_remote_vcf_regions(
                url,
                task.region_strings,
                event_prefix=SOURCE_DB,
                log_context={"chrom": task.chrom, "data_type": data_type},
                log=logger,
                reopen_stats=stats,
            ):
                row = _record_to_row(record, task.source_version_id, task.retrieval_datetime)
                if row is None:
                    continue
                pos_obj = row["pos_grch38"]
                if not isinstance(pos_obj, int):
                    continue
                if pos_obj not in task.filter_positions:
                    continue
                key = (
                    str(row["chrom"]),
                    pos_obj,
                    str(row["ref_allele"]),
                    str(row["alt_allele"]),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                pending.append(row)
                if len(pending) >= task.batch_size:
                    _flush()
        _flush()
        if writer is None:
            return _ChromResult(
                chrom=task.chrom,
                parquet_path=None,
                row_count=0,
                status="ok",
                reopens=stats.reopens,
            )
        writer.close()  # type: ignore[no-untyped-call]  # pyarrow.parquet untyped
        writer = None
        return _ChromResult(
            chrom=task.chrom,
            parquet_path=parquet_path,
            row_count=row_count,
            status="ok",
            reopens=stats.reopens,
        )
    finally:
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()  # type: ignore[no-untyped-call]  # pyarrow.parquet untyped


def _run_workers(tasks: list[_ChromTask], jobs: int) -> list[_ChromResult]:
    """Run chromosome workers in a spawned process pool; collect results.

    The ONLY function that touches :class:`ProcessPoolExecutor`, so tests
    monkeypatch it with a synchronous in-process shim (spawn workers would not
    see a test's ``cyvcf2.VCF`` monkeypatch). ``spawn`` is mandatory: the
    parent holds an open DuckDB connection + httpx client + ExternalClient that
    ``fork`` would leak into children. A worker that raises is normalised here
    into a ``"failed"`` :class:`_ChromResult` carrying the exception, so one
    chromosome's failure never aborts the pool or loses the cause.
    """
    if not tasks:
        return []
    results: list[_ChromResult] = []
    max_workers = max(1, min(jobs, len(tasks)))
    ctx = get_context("spawn")
    with ProcessPoolExecutor(max_workers=max_workers, mp_context=ctx) as executor:
        future_to_chrom = {
            executor.submit(_stream_chromosome_to_parquet, task): task.chrom for task in tasks
        }
        for future in as_completed(future_to_chrom):
            chrom = future_to_chrom[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 — normalise any worker failure into a result
                # ``reopens`` defaults to 0 here: a dead worker's partial
                # reopen count died with its process (its _ChromResult never
                # reached an ok-return), so GnomadLoadResult.reopens_total
                # under-reports on a parallel failure. 0 is the honest
                # unrecoverable value — see the reopens_total docstring caveat.
                results.append(
                    _ChromResult(
                        chrom=chrom,
                        parquet_path=None,
                        row_count=0,
                        status="failed",
                        error=exc,
                    ),
                )
    return results


def _merge_chromosome_parquet(conn: DuckDBPyConnection, parquet_path: str) -> None:
    """Insert one chromosome's staged Parquet rows into ``gnomad_frequencies``.

    Allocates a contiguous ``freq_id`` block starting at :func:`_next_freq_id`
    and streams the staged rows straight from Parquet via DuckDB's
    ``read_parquet`` (no Python row round-trip). Called serially by the parent
    in canonical chromosome order, so ``freq_id`` allocation is gap-free and
    deterministic under the single writer. ``source_version_id`` and
    ``retrieval_date`` are already present in the staged rows (the worker wrote
    them), so they flow through the ``SELECT`` unchanged.
    """
    base_id = _next_freq_id(conn)
    pop_cols = ", ".join(f"af_{pop}" for pop in GNOMAD_POPULATIONS)
    conn.execute(
        f"""
        INSERT INTO {_TARGET_TABLE} (
            freq_id, rsid, chrom, pos_grch38, ref_allele, alt_allele,
            af_global, ac_global, an_global,
            {pop_cols},
            filter_status, source_version_id, retrieval_date
        )
        SELECT
            (CAST(? AS BIGINT) - 1) + row_number() OVER () AS freq_id,
            rsid, chrom::chromosome_enum, pos_grch38, ref_allele, alt_allele,
            af_global, ac_global, an_global,
            {pop_cols},
            filter_status, source_version_id, retrieval_date
          FROM read_parquet(?)
        """,  # noqa: S608 — table + column lists are module-controlled
        [base_id, parquet_path],
    )


def _run_parallel_load(  # noqa: PLR0913 — orchestration config is irreducible
    conn: DuckDBPyConnection,
    audited_client: ExternalClient,
    *,
    requested: list[str],
    filter_set: FilterSet,
    source_version_id: int,
    retrieval_datetime: datetime,
    coalesce_distance: int,
    batch_size: int,
    jobs: int,
) -> tuple[list[str], list[str], BaseException | None, int]:
    """Parallel analogue of ``load``'s per-chromosome loop.

    Streams chromosomes concurrently (one worker process per chromosome, both
    data types) and merges each staged Parquet file serially into
    ``gnomad_frequencies`` under the single DuckDB writer. Returns the same
    ``(succeeded, failed, capture_failure, reopens_total)`` tuple the
    sequential loop produces (``reopens_total`` summed over the workers'
    per-chromosome ``_ChromResult.reopens``), so ``load``'s downstream
    pointer-flip / orphan-cleanup / summary steps are shared and unchanged.

    Mirrors the sequential semantics: chromosomes with no filter positions
    succeed without a worker; the parent issues the two audited HEADs per
    loaded chromosome up front (workers can't write ``app.db``); rows land
    under ``source_version_id`` as each chromosome merges; a worker failure
    records that chromosome as failed, captures the first cause, and is
    fail-soft (already-succeeded chromosomes still merge, the pointer never
    flips on a failure). ``succeeded`` / ``failed`` are returned in canonical
    ``requested`` order, the same order ``freq_id`` is allocated in.
    """
    succeeded: list[str] = []
    failed: list[str] = []
    capture_failure: BaseException | None = None
    no_position: set[str] = set()
    tasks: list[_ChromTask] = []
    staging_dir = tempfile.mkdtemp(prefix="gnomad_stage_")
    try:
        for chrom in requested:
            positions = filter_set.positions.get(chrom, [])
            if not positions:
                logger.info("gnomad.chrom.no_filter_positions", chrom=chrom)
                no_position.add(chrom)
                continue
            regions = _coalesce_positions(positions, coalesce_distance)
            region_strings = [f"chr{chrom}:{start}-{end}" for start, end in regions]
            # Audited HEADs up front — the parent owns app.db; workers can't.
            for data_type in ("exomes", "genomes"):
                url = GNOMAD_URL_TEMPLATE.format(data_type=data_type, chrom=chrom)
                _audited_head(audited_client, url)
            tasks.append(
                _ChromTask(
                    chrom=chrom,
                    region_strings=region_strings,
                    filter_positions=frozenset(positions),
                    source_version_id=source_version_id,
                    retrieval_datetime=retrieval_datetime,
                    batch_size=batch_size,
                    staging_dir=staging_dir,
                ),
            )

        logger.info(
            "gnomad.parallel.dispatch",
            chroms=[task.chrom for task in tasks],
            jobs=jobs,
            staging_dir=staging_dir,
        )
        results = {result.chrom: result for result in _run_workers(tasks, jobs)}
        # Sum over every worker result present in ``results`` (not re-derived
        # from ``requested``, not ``max``/last); correct under the loader's
        # unique-chrom invariant — the same one the merge loop below assumes. A
        # failed/dead worker contributes reopens=0 (documented accepted
        # limitation on GnomadLoadResult.reopens_total).
        total_reopens = sum(r.reopens for r in results.values())

        # Merge in canonical (requested) order: deterministic, gap-free freq_id.
        for chrom in requested:
            if chrom in no_position:
                succeeded.append(chrom)
                continue
            result = results.get(chrom)
            if result is None:
                continue
            if result.status == "failed":
                failed.append(chrom)
                if capture_failure is None:
                    capture_failure = result.error
                logger.error("gnomad.chrom.failed", chrom=chrom, error=repr(result.error))
                continue
            chrom_started = time.monotonic()
            if result.parquet_path is not None and result.row_count > 0:
                _merge_chromosome_parquet(conn, result.parquet_path)
            conn.commit()
            logger.info(
                "gnomad.chrom.complete",
                chrom=chrom,
                rows=result.row_count,
                elapsed_seconds=round(time.monotonic() - chrom_started, 1),
                parallel=True,
            )
            succeeded.append(chrom)
        return succeeded, failed, capture_failure, total_reopens
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _run_sequential_load(  # noqa: PLR0913 — orchestration config is irreducible
    conn: DuckDBPyConnection,
    audited_client: ExternalClient,
    *,
    requested: list[str],
    filter_set: FilterSet,
    source_version_id: int,
    retrieval_datetime: datetime,
    coalesce_distance: int,
    batch_size: int,
    log: structlog.stdlib.BoundLogger,
) -> tuple[list[str], list[str], BaseException | None, int]:
    """Sequential per-chromosome load — the original ``load`` loop, extracted.

    One chromosome at a time, one data type at a time; each chromosome commits
    independently and a failure stops the run (``break``) leaving the
    already-loaded chromosomes under ``source_version_id``. Returns the
    ``(succeeded, failed, capture_failure, reopens_total)`` tuple ``load``
    threads into the shared pointer-flip / summary tail; ``reopens_total`` is
    the run-level htslib-reopen count (one accumulator threaded through every
    chromosome, so a failed chromosome's partial is included — it is mutated at
    the reopen site before the raise). ``jobs <= 1`` routes here, so every
    existing direct-``load`` caller and the registry bare-form run this path
    unchanged.
    """
    succeeded: list[str] = []
    failed: list[str] = []
    capture_failure: BaseException | None = None
    run_stats = RemoteReadStats()
    for chrom in requested:
        positions = filter_set.positions.get(chrom, [])
        if not positions:
            log.info("gnomad.chrom.no_filter_positions", chrom=chrom)
            succeeded.append(chrom)
            continue
        regions = _coalesce_positions(positions, coalesce_distance)
        filter_set_pos = frozenset(positions)
        chrom_started = time.monotonic()
        try:
            n = _load_chromosome(
                conn,
                audited_client,
                chrom,
                regions,
                filter_set_pos,
                source_version_id,
                retrieval_datetime,
                batch_size,
                reopen_stats=run_stats,
            )
            conn.commit()
            elapsed = time.monotonic() - chrom_started
            log.info(
                "gnomad.chrom.complete",
                chrom=chrom,
                rows=n,
                regions=len(regions),
                elapsed_seconds=round(elapsed, 1),
            )
            succeeded.append(chrom)
        except Exception as exc:
            # Best-effort rollback — when the failure happens before
            # any INSERT executed against this chrom (e.g. the remote
            # VCF open raises), DuckDB has not auto-started a
            # transaction yet and rollback() raises. The chrom-failed
            # path swallows that nested error and re-raises the
            # original cause below.
            with contextlib.suppress(Exception):
                conn.rollback()
            elapsed = time.monotonic() - chrom_started
            log.exception(
                "gnomad.chrom.failed",
                chrom=chrom,
                elapsed_seconds=round(elapsed, 1),
            )
            failed.append(chrom)
            capture_failure = exc
            break
    return succeeded, failed, capture_failure, run_stats.reopens


# ---------------------------------------------------------------------------
# Top-level entrypoint — load.
# ---------------------------------------------------------------------------


def load(  # noqa: C901, PLR0912, PLR0913, PLR0915 — single entry point; the per-step branching is explicit
    conn: DuckDBPyConnection,
    audited_client: ExternalClient,
    *,
    force: bool = False,
    version: str = GNOMAD_VERSION,
    chromosomes: Sequence[str] | None = None,
    resume: bool = False,
    coalesce_distance: int = DEFAULT_COALESCE_DISTANCE_BP,
    batch_size: int = DEFAULT_BATCH_SIZE,
    jobs: int = 1,
) -> GnomadLoadResult:
    """Load gnomAD v4.1.1 filtered allele frequencies.

    Pipeline:

    1. Pre-flight ``libcurl`` check and ``external_calls_enabled``
       gate. Both fail fast with actionable errors.
    2. Resolve the current active gnomAD source-version (may be
       ``None``). If it already names ``version`` and neither
       ``force`` nor ``resume`` is set, short-circuit.
    3. Decide the working ``source_version_id``: re-use an in-flight
       one when ``resume`` is set; otherwise allocate a fresh row.
    4. Build the ``user_only`` filter set (the user's own distinct
       ``variants_master`` positions, finding-035) and record composition
       counts.
    5. Restrict the chromosome list to the intersection with
       :data:`SUPPORTED_CHROMS`; skip chroms already populated when
       resuming.
    6. Per chromosome: coalesce filter positions into tabix ranges,
       open the remote VCFs, filter records, dedup, chunk-insert.
       Partial failures stop the run but leave already-loaded
       chromosomes under the new ``source_version_id``. With ``jobs >
       1`` the chromosomes stream concurrently in worker processes
       (:func:`_run_parallel_load`); the parent merges each staged
       Parquet file serially, so the output rows + ``freq_id`` match
       the sequential path exactly. ``jobs <= 1`` runs the original
       sequential loop (:func:`_run_sequential_load`).
    7. When the full chrom set landed (and the caller didn't restrict
       via ``chromosomes``), flip the ``annotation_sources`` pointer
       and commit.
    8. Compute the drift-identifier summary and return.

    ``jobs`` defaults to ``1`` (sequential) so direct callers and the
    registry bare-form are unchanged; the CLI resolves a higher default
    (:data:`DEFAULT_PARALLEL_JOBS`). See finding-035 for the ``user_only``
    filter adoption and finding-011 for the three-way-vs-four-way design
    discussion (retained as the revert / PGS path).
    """
    started = time.monotonic()
    log = logger.bind(source=SOURCE_DB, version=version)

    # 1. Pre-flight.
    if not is_external_enabled():
        # Mirror the audited refusal pattern: open one audited HEAD so
        # the blocked attempt is durably recorded in audit_log before
        # raising. The _audited_attempt helper writes both intent and
        # blocked rows when the master switch is off.
        url = GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22")
        try:
            audited_client.request(
                "HEAD",
                url,
                resource_type="annotation_source",
                resource_id=_PREFLIGHT_RESOURCE_ID,
            )
        except ExternalCallsDisabledError:
            raise
        except ExternalCallError as exc:
            log.info("gnomad.preflight_audit_error", error=str(exc))
        # Defensive: if the HEAD did not raise (master switch came up
        # between checks), still refuse — the policy is fail-closed.
        raise ExternalCallsDisabledError
    _check_libcurl_available()

    # 2. Current version.
    current = get_current_version(conn, SOURCE_DB)
    if current is not None and current.version == version and not force and not resume:
        log.info("gnomad.skip_already_current", version=version)
        wall = time.monotonic() - started
        return GnomadLoadResult(
            version_label=version,
            source_version_id=current.source_version_id,
            pointer_flipped=False,
            rows_loaded=0,
            distinct_variants_per_chrom={},
            filter_set_composition={},
            match_rate=0.0,
            af_buckets_user_overlap={},
            mean_af_user_overlap=0.0,
            pop_af_presence={},
            chromosomes_succeeded=(),
            chromosomes_failed=(),
            wall_clock_seconds=wall,
            reopens_total=0,
        )

    # 3. Source-version id (fresh or in-flight resume).
    source_version_id: int | None = None
    version_row_freshly_allocated = False
    if resume:
        source_version_id = _find_in_flight_source_version_id(conn, version)
        if source_version_id is not None:
            log.info(
                "gnomad.resume_existing",
                source_version_id=source_version_id,
            )
    if source_version_id is None:
        source_version_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=version,
            source_url=GNOMAD_URL_TEMPLATE,
            source_file_hash=f"gnomad_{version}",
            source_file_size=0,
            record_count=None,
        )
        version_row_freshly_allocated = True
        log.info("gnomad.allocated_new_version", source_version_id=source_version_id)

    # 4. Filter set.
    filter_set = _build_filter_set(conn)
    log.info(
        "gnomad.filter_set_composition",
        **filter_set.composition,
    )

    # 5. Chrom list.
    if chromosomes is None:
        requested = list(SUPPORTED_CHROMS)
        partial_run = False
    else:
        requested = [c for c in chromosomes if c in SUPPORTED_CHROMS]
        partial_run = set(requested) != set(SUPPORTED_CHROMS)
    if resume:
        already = _populated_chroms(conn, source_version_id)
        requested = [c for c in requested if c not in already]
        if already:
            log.info("gnomad.resume_skip_chroms", skip=sorted(already))

    retrieval_datetime = datetime.now(UTC)

    # 6. Per-chromosome load — parallel across chromosomes when jobs > 1,
    #    else the original sequential loop. Both return the same
    #    (succeeded, failed, capture_failure, reopens_total) 4-tuple the
    #    shared tail consumes.
    if jobs > 1:
        succeeded, failed, capture_failure, reopens_total = _run_parallel_load(
            conn,
            audited_client,
            requested=requested,
            filter_set=filter_set,
            source_version_id=source_version_id,
            retrieval_datetime=retrieval_datetime,
            coalesce_distance=coalesce_distance,
            batch_size=batch_size,
            jobs=jobs,
        )
    else:
        succeeded, failed, capture_failure, reopens_total = _run_sequential_load(
            conn,
            audited_client,
            requested=requested,
            filter_set=filter_set,
            source_version_id=source_version_id,
            retrieval_datetime=retrieval_datetime,
            coalesce_distance=coalesce_distance,
            batch_size=batch_size,
            log=log,
        )

    # 7. Pointer flip (only on a full successful run that wasn't
    # restricted by --chromosomes).
    pointer_flipped = False
    if not failed and not partial_run and succeeded:
        # Re-check populated chroms in case resume started from a
        # partial state. Pointer flips only when every SUPPORTED_CHROMS
        # entry has at least one populated chrom OR resumed-already +
        # newly-succeeded covers the full set.
        populated = _populated_chroms(conn, source_version_id)
        if set(populated) >= set(SUPPORTED_CHROMS) - {
            c for c in SUPPORTED_CHROMS if not filter_set.positions.get(c)
        }:
            flip_to_new_version(
                conn,
                source=SOURCE_DB,
                table=_TARGET_TABLE,
                new_source_version_id=source_version_id,
            )
            commit_and_checkpoint(conn, source_name=SOURCE_DB)
            pointer_flipped = True
            log.info("gnomad.pointer_flipped", source_version_id=source_version_id)
        else:
            log.info(
                "gnomad.pointer_not_flipped_incomplete_coverage",
                populated=sorted(populated),
                expected=list(SUPPORTED_CHROMS),
            )

    if partial_run and not failed:
        log.info(
            "gnomad.partial_run_pointer_not_flipped",
            requested=requested,
            note="run --resume against the full chrom set to flip the pointer",
        )

    # Backfill record_count on the version row using the cumulative
    # post-flush total. If the version row was freshly allocated in
    # this invocation and nothing landed under it (failure path before
    # any chrom committed, or a --chromosomes partial run whose
    # requested chrom yielded zero rows), delete the orphan row so a
    # future run gets a clean sv_id allocation. Per finding-015 #11
    # this is the post-loop guard wired to "no chrom committed any
    # rows"; the resume path is excluded so in-flight state is
    # preserved for the next resume invocation.
    rows_count_row = conn.execute(
        f"SELECT COUNT(*) FROM {_TARGET_TABLE} WHERE source_version_id = ?",  # noqa: S608
        [source_version_id],
    ).fetchone()
    rows_count = int(rows_count_row[0]) if rows_count_row is not None else 0

    if version_row_freshly_allocated and rows_count == 0 and not pointer_flipped:
        _cleanup_orphan_version_row(conn, source_version_id)
        conn.commit()
        log.info(
            "gnomad.orphan_version_row_cleaned_up",
            source_version_id=source_version_id,
        )
        source_version_id = None
        rows_loaded = 0
        distinct_per_chrom: dict[str, int] = {}
        match_rate = 0.0
        af_buckets: dict[str, int] = {
            "lt_0.001": 0,
            "0.001_to_0.01": 0,
            "0.01_to_0.05": 0,
            "0.05_to_0.5": 0,
            "gt_0.5": 0,
        }
        mean_af = 0.0
        pop_presence: dict[str, int] = dict.fromkeys(GNOMAD_POPULATIONS, 0)
    else:
        conn.execute(
            "UPDATE annotation_source_versions SET record_count = ? WHERE source_version_id = ?",
            [rows_count, source_version_id],
        )
        conn.commit()

        # 8. Summary.
        (
            rows_loaded,
            distinct_per_chrom,
            match_rate,
            af_buckets,
            mean_af,
            pop_presence,
        ) = _summarize_run(conn, source_version_id)

    wall = time.monotonic() - started
    log.info(
        "gnomad.refresh.complete",
        version=version,
        source_version_id=source_version_id,
        pointer_flipped=pointer_flipped,
        rows_loaded=rows_loaded,
        distinct_variants_per_chrom=distinct_per_chrom,
        filter_set_composition=filter_set.composition,
        match_rate=round(match_rate, 4),
        af_buckets_user_overlap=af_buckets,
        mean_af_user_overlap=round(mean_af, 4),
        pop_af_presence=pop_presence,
        chromosomes_succeeded=tuple(succeeded),
        chromosomes_failed=tuple(failed),
        wall_clock_seconds=round(wall, 1),
        reopens_total=reopens_total,
    )

    if capture_failure is not None:
        # Partial failures: log the summary first so the operator has
        # the drift identifiers, then re-raise so the CLI surfaces the
        # underlying cause.
        raise capture_failure

    return GnomadLoadResult(
        version_label=version,
        source_version_id=source_version_id,
        pointer_flipped=pointer_flipped,
        rows_loaded=rows_loaded,
        distinct_variants_per_chrom=distinct_per_chrom,
        filter_set_composition=filter_set.composition,
        match_rate=match_rate,
        af_buckets_user_overlap=af_buckets,
        mean_af_user_overlap=mean_af,
        pop_af_presence=pop_presence,
        chromosomes_succeeded=tuple(succeeded),
        chromosomes_failed=tuple(failed),
        wall_clock_seconds=wall,
        reopens_total=reopens_total,
    )


# ---------------------------------------------------------------------------
# Registry adapter — refresh.
# ---------------------------------------------------------------------------


def refresh(  # noqa: PLR0913 — registry signature + gnomad-specific kwargs
    force: bool,  # noqa: FBT001 — positional matches registry's RefreshFn signature
    skip_if_same_version: bool = False,  # noqa: FBT001, FBT002 — opt-in default for the shared flag
    *,
    version: str = GNOMAD_VERSION,
    chromosomes: Sequence[str] | None = None,
    resume: bool = False,
    coalesce_distance: int = DEFAULT_COALESCE_DISTANCE_BP,
    jobs: int = 1,
) -> RefreshResult:
    """Refresh gnomAD filtered allele frequencies.

    Registry adapter around :func:`load`. The registry's RefreshFn
    signature is ``(force, skip_if_same_version) -> RefreshResult``;
    the gnomad CLI threads the rich kwargs through. The bare-form
    ``refresh(force, skip_if_same_version)`` from the registry path
    runs against the full SUPPORTED_CHROMS set with default coalesce
    and ``jobs=1`` (sequential); the CLI passes ``--jobs`` explicitly.

    ``skip_if_same_version`` is accepted for signature parity but
    unused: the gnomad pre-flight already short-circuits on
    ``current.version == version and not force``, which gives the
    same idempotence guarantee without consulting a file hash (there
    is no single downloaded artifact whose SHA-256 anchors the match).
    """
    del skip_if_same_version  # see docstring; gnomad pre-flight covers this.

    with (
        httpx.Client(
            follow_redirects=True,
            timeout=_DEFAULT_TIMEOUT_S,
        ) as http_client,
        ExternalClient(
            f"annotations_{SOURCE_DB}",
            client=http_client,
        ) as audited_client,
        duckdb_connection() as conn,
    ):
        result = load(
            conn,
            audited_client,
            force=force,
            version=version,
            chromosomes=chromosomes,
            resume=resume,
            coalesce_distance=coalesce_distance,
            jobs=jobs,
        )

    return RefreshResult(
        source_db=SOURCE_DB,
        source_version_id=result.source_version_id or 0,
        version=result.version_label,
        record_count=result.rows_loaded,
        was_already_current=(
            result.source_version_id is not None
            and not result.pointer_flipped
            and result.rows_loaded == 0
        ),
    )


# Register at module-import time. The loaders subpackage __init__.py
# imports this module so the registration happens before any CLI
# dispatch runs.
register_loader(SOURCE_DB, refresh)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_COALESCE_DISTANCE_BP",
    "GNOMAD_POPULATIONS",
    "GNOMAD_URL_TEMPLATE",
    "GNOMAD_VERSION",
    "MAX_REMOTE_REGION_ATTEMPTS",
    "SOURCE_DB",
    "SUPPORTED_CHROMS",
    "URL_VERIFIED_DATE",
    "GnomadLibcurlMissingError",
    "GnomadLoadResult",
    "GnomadRemoteIterationError",
    "load",
    "refresh",
]
