"""gnomAD v4.1.1 filtered allele-frequencies loader.

Streams the gnomAD v4.1.1 per-chromosome sites-only VCFs (exomes + genomes,
GRCh38, GCS-hosted, bgzipped + tabix-indexed) via cyvcf2 remote tabix
queries, filters every site against the three-way intersection of
distinct ``(chrom, pos)`` positions present in the user's variants
plus the active ClinVar release plus the active GWAS Catalog release,
and chunk-loads the resulting per-variant population-AF rows into
``gnomad_frequencies`` via PyArrow Table registration +
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

The filter set is the three-way union ``(user U ClinVar U GWAS)``;
CLAUDE.md "Things never to do" #3 mandates the broader
``(user U ClinVar U GWAS U PGS)`` intersection but PGS per-variant
weights do not yet exist in the database at PR-B time (they land in
Phase 6 as ``pgs_score_weights``). Sub-phase 5.5b will extend the
active gnomAD source-version's coverage to PGS-component variants
without a version bump; see finding-011.

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
import fcntl
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Self

import httpx
import pyarrow as pa
import structlog

from genome.annotate.registry import RefreshResult, register_loader
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


# ---------------------------------------------------------------------------
# Custom errors.
# ---------------------------------------------------------------------------


class GnomadLibcurlMissingError(RuntimeError):
    """Raised when the cyvcf2-bundled htslib cannot reach the gnomAD URL.

    The pre-flight check opens a known-tiny tabix range against the
    chr22 exomes VCF. If the open fails the most likely cause is that
    the installed htslib was built without libcurl support, which means
    remote-tabix reads (the loader's whole filtering strategy) cannot
    work. The message points the operator at the rebuild instructions
    rather than swallowing the failure and falling back to a full
    download.
    """


class GnomadRemoteIterationError(RuntimeError):
    """Raised when a single tabix region exhausts its retry budget.

    The loader detects htslib BGZF / libcurl transient errors by
    capturing C-level stderr writes and reopening the VCF on the next
    region. When the *same* region keeps tripping the corruption
    detector across many reopens, something more durable than a
    transient HTTP/2 framing blip is at play (e.g. gnomAD has rotated
    the URL, or the local network is degraded enough that no region
    completes). The chromosome is failed in that case so the operator
    sees a real error rather than a silent low-row-count run.
    """


# ---------------------------------------------------------------------------
# htslib transient-error recovery.
# ---------------------------------------------------------------------------
#
# gnomAD's GCS bucket responds over HTTPS with HTTP/2 multiplexed
# connections. htslib 1.19's hfile_libcurl plugin opens one libcurl
# easy handle per cyvcf2.VCF. When the loader fires many small tabix
# range requests in rapid succession on the same handle, libcurl
# eventually returns CURLE_HTTP2 (16) on one of the BGZF block reads.
# htslib's iterator can't recover from that — subsequent ``vcf(region)``
# calls silently return zero records, and stderr fills with
# "[E::hts_itr_next] Failed to seek to offset NNN: Illegal seek" lines
# whose offsets are garbage memory. The connection-level state is
# unsalvageable; the only recovery is to close + reopen the VCF.
#
# Detection: htslib writes those error lines directly to fd 2 via the
# hts_log_* helpers, not as Python exceptions. The Python iterator
# protocol returns ``StopIteration`` (an empty for-loop) regardless.
# So the loader captures fd 2 into a pipe during iteration, scans the
# captured bytes for known htslib error tokens after each region, and
# forwards every captured byte through to the operator's real stderr
# so structlog output and other warnings remain visible.

_HTSLIB_ERROR_TOKENS: Final[tuple[bytes, ...]] = (
    b"easy_errno",
    b"bgzf_read_block",
    b"hts_itr_next",
)

MAX_REMOTE_REGION_ATTEMPTS: Final[int] = 5
"""Maximum VCF reopens per region before the chromosome is failed.

Each attempt closes the corrupted handle, reopens against the same
URL, and re-iterates the region. The seen-keys dedup already in
:func:`_load_chromosome` makes record re-yields idempotent (same
``(chrom, pos, ref, alt)`` key → skipped on retry), so a successful
attempt produces no duplicates regardless of how many partial
yields the prior attempts emitted before tripping the detector.

5 is generous: in practice corruption recovers on the first reopen
because the transient libcurl HTTP/2 framing error is a connection-
level event, not a server-side condition. A region that fails 5
times in a row signals something more durable (network outage, URL
rotation), which is properly surfaced as a chromosome failure.
"""


class _StderrTap:
    """Capture and forward htslib's C-level stderr writes.

    Used to detect cyvcf2/htslib BGZF + libcurl errors that don't
    surface as Python exceptions. On enter the tap replaces fd 2 with
    a non-blocking pipe; :meth:`check` drains the pipe, forwards the
    bytes through to the saved real-stderr fd (so structlog output
    and other warnings remain visible to the operator), and returns
    True when one of :data:`_HTSLIB_ERROR_TOKENS` appears in the
    drained bytes. On exit the tap restores the original fd 2 after
    a final drain.

    The pipe is sized to 1 MiB on Linux (``F_SETPIPE_SZ``) so a few
    seconds of unread output cannot back up htslib's writer thread.
    The drain loop is bounded by the pipe contents, not by a timer,
    so :meth:`check` returns promptly between regions.

    The tap is a thin context manager; constructing one outside a
    ``with`` block does nothing and consuming :meth:`check` outside
    the block silently returns ``False``. This makes it safe to keep
    a reference in test fixtures that mock the loader's iteration.
    """

    _PIPE_SIZE: Final[int] = 1024 * 1024
    _READ_CHUNK: Final[int] = 65536

    def __init__(self) -> None:
        self._original_fd: int | None = None
        self._pipe_read: int | None = None

    def __enter__(self) -> Self:
        sys.stderr.flush()
        self._original_fd = os.dup(2)
        read_fd, write_fd = os.pipe()
        fcntl.fcntl(read_fd, fcntl.F_SETFL, os.O_NONBLOCK)
        # Best-effort enlargement of the pipe buffer so a slow consumer
        # doesn't block htslib. F_SETPIPE_SZ is Linux-only; absence is
        # not fatal (the default 64 KiB still works, just with more
        # frequent drain calls).
        f_setpipe_sz = getattr(fcntl, "F_SETPIPE_SZ", None)
        if f_setpipe_sz is not None:
            with contextlib.suppress(OSError):
                fcntl.fcntl(read_fd, f_setpipe_sz, self._PIPE_SIZE)
        os.dup2(write_fd, 2)
        os.close(write_fd)
        self._pipe_read = read_fd
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._original_fd is None or self._pipe_read is None:
            return
        # Final drain so anything written between the last check() and
        # close reaches the operator's stderr.
        self._drain_and_forward()
        sys.stderr.flush()
        os.dup2(self._original_fd, 2)
        try:
            os.close(self._pipe_read)
        finally:
            os.close(self._original_fd)
            self._pipe_read = None
            self._original_fd = None

    def check(self) -> bool:
        """Return True when an htslib error token appeared since the last call.

        Drains the captured stderr buffer (forwarding every byte to
        the operator's real stderr) and scans the drained bytes for
        any of :data:`_HTSLIB_ERROR_TOKENS`. Subsequent calls only
        see new output; the scan is monotonic per-call, not
        cumulative.
        """
        return _scan_for_htslib_errors(self._drain_and_forward())

    def _drain_and_forward(self) -> bytes:
        if self._pipe_read is None or self._original_fd is None:
            return b""
        chunks: list[bytes] = []
        while True:
            try:
                data = os.read(self._pipe_read, self._READ_CHUNK)
            except BlockingIOError:
                break
            if not data:
                break
            chunks.append(data)
        buf = b"".join(chunks)
        if buf:
            os.write(self._original_fd, buf)
        return buf


def _scan_for_htslib_errors(buf: bytes) -> bool:
    """Return True when ``buf`` contains one of the htslib error tokens.

    Module-level helper so tests can exercise the scan logic without
    standing up an fd-redirecting tap. The token list intentionally
    covers libcurl's ``easy_errno`` line (the original-cause signal),
    BGZF's ``bgzf_read_block`` line (htslib's framing-layer signal),
    and the iterator's ``hts_itr_next`` line (the post-corruption
    seek-failure signal that the verification-2 run was flooded with).
    Any one is sufficient evidence that the VCF handle is in a state
    where subsequent region queries will silently return no records.
    """
    return any(token in buf for token in _HTSLIB_ERROR_TOKENS)


# ---------------------------------------------------------------------------
# Pre-flight.
# ---------------------------------------------------------------------------


def _check_libcurl_available() -> None:
    """Confirm cyvcf2 can open a remote gnomAD VCF.

    Opens a known-tiny tabix range against the gnomAD chr22 exomes
    VCF. The query fetches at most one record from a tight position
    window (no I/O cost on the gnomAD server side). A success means
    htslib was built with libcurl support and the gnomAD GCS bucket
    is reachable.

    Raises :class:`GnomadLibcurlMissingError` with an actionable
    message when the open or initial fetch fails for any reason. The
    error preserves the underlying exception via ``__cause__`` so a
    log reader can see the root cause (typically a libcurl-missing
    htslib build).
    """
    from cyvcf2 import VCF  # noqa: PLC0415 — local import keeps the module-level surface narrow

    url = GNOMAD_URL_TEMPLATE.format(data_type="exomes", chrom="22")
    # chr22:10500000-10500100 is in a low-coverage region near the
    # short-arm acrocentric boundary; the range is small enough that
    # the open + iter is essentially free server-side.
    probe_region = "chr22:10500000-10500100"
    try:
        vcf = VCF(url)
        try:
            # Pull one record from a tight tabix range; the open path
            # is what exercises libcurl, but fetching at least one row
            # makes the failure mode unambiguous if the bucket changes
            # shape.
            iter(vcf(probe_region))
        finally:
            vcf.close()
    except Exception as exc:
        msg = (
            "gnomAD remote VCF open failed. "
            "htslib must be built with libcurl support; this environment "
            "doesn't have it (or the gnomAD GCS bucket is unreachable). "
            "Rebuild htslib (and cyvcf2 against it) with libcurl enabled. "
            f"Probe URL: {url}"
        )
        raise GnomadLibcurlMissingError(msg) from exc


# ---------------------------------------------------------------------------
# Filter-set build.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FilterSet:
    """Result of :func:`_build_filter_set`.

    Carries the per-chromosome sorted-unique position lists plus the
    composition counts the end-of-load summary surfaces.
    """

    positions: dict[str, list[int]]
    composition: dict[str, int]


def _build_filter_set(conn: DuckDBPyConnection) -> _FilterSet:
    """Compute the three-way ``(user U ClinVar U GWAS)`` filter set.

    Three components:

    * ``user`` — distinct ``(chrom, pos_grch38)`` from
      ``variants_master``. No per-row activity filter — every row in
      ``variants_master`` is considered current (CLAUDE.md #7
      version-pointer pattern applies to evolving annotation sources
      only; user variants supersede at the row grain via
      ``genotype_calls``).
    * ``clinvar`` — distinct ``(chrom, pos_grch38)`` from
      ``clinvar_annotations`` joined through
      ``annotation_sources`` so only rows under the
      currently-active ClinVar source-version contribute.
    * ``gwas`` — same shape, joined through
      ``annotation_sources`` against ``gwas_catalog_associations``.

    PGS is intentionally excluded at PR B: ``pgs_catalog_scores`` is
    metadata only and does not carry per-variant positions; the
    per-variant weights table (``pgs_score_weights``) lands in
    Phase 6, and 5.5b will then extend gnomAD coverage to those
    additional positions without a version bump. See finding-011.

    Returns a :class:`_FilterSet` whose ``positions`` dict maps each
    chrom in :data:`SUPPORTED_CHROMS` to a sorted list of unique
    positions. ``composition`` carries the per-source distinct counts
    (``user``, ``clinvar``, ``gwas``) plus the ``union_total`` so the
    end-of-load summary can name the filter set's shape.

    Every subquery guards ``pos_grch38 > 0``. Upstream annotation
    loaders (notably ClinVar) emit a ``-1`` sentinel for variants
    whose GRCh38 coordinate could not be resolved; an ``IS NOT NULL``
    guard would still admit those rows, and any negative value
    flowing through :func:`_coalesce_positions` would produce an
    invalid ``chr<N>:-1--1`` tabix region that htslib rejects with
    "Coordinates must be > 0" and may corrupt the BGZF read offset
    state. ``variants_master`` enforces ``pos_grch38 BIGINT NOT
    NULL`` at the schema level, but the same guard is applied
    uniformly for defense in depth.
    """
    chrom_list = ",".join(f"'{c}'" for c in SUPPORTED_CHROMS)

    user_row = conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT chrom, pos_grch38
              FROM variants_master
             WHERE chrom::VARCHAR IN ({chrom_list})
               AND pos_grch38 > 0
        )
        """,  # noqa: S608 — chrom_list is built from the module constant SUPPORTED_CHROMS
    ).fetchone()
    user_count = int(user_row[0]) if user_row is not None else 0

    clinvar_row = conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT c.chrom, c.pos_grch38
              FROM clinvar_annotations c
              JOIN annotation_sources s
                ON s.source_db = 'clinvar'
               AND s.current_source_version_id = c.source_version_id
             WHERE c.chrom::VARCHAR IN ({chrom_list})
               AND c.pos_grch38 > 0
        )
        """,  # noqa: S608
    ).fetchone()
    clinvar_count = int(clinvar_row[0]) if clinvar_row is not None else 0

    gwas_row = conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT g.chrom, g.pos_grch38
              FROM gwas_catalog_associations g
              JOIN annotation_sources s
                ON s.source_db = 'gwas_catalog'
               AND s.current_source_version_id = g.source_version_id
             WHERE g.chrom::VARCHAR IN ({chrom_list})
               AND g.pos_grch38 > 0
        )
        """,  # noqa: S608
    ).fetchone()
    gwas_count = int(gwas_row[0]) if gwas_row is not None else 0

    union_rows = conn.execute(
        f"""
        WITH all_positions AS (
            SELECT chrom::VARCHAR AS chrom, pos_grch38 AS pos
              FROM variants_master
             WHERE chrom::VARCHAR IN ({chrom_list})
               AND pos_grch38 > 0
            UNION
            SELECT c.chrom::VARCHAR AS chrom, c.pos_grch38 AS pos
              FROM clinvar_annotations c
              JOIN annotation_sources s
                ON s.source_db = 'clinvar'
               AND s.current_source_version_id = c.source_version_id
             WHERE c.chrom::VARCHAR IN ({chrom_list})
               AND c.pos_grch38 > 0
            UNION
            SELECT g.chrom::VARCHAR AS chrom, g.pos_grch38 AS pos
              FROM gwas_catalog_associations g
              JOIN annotation_sources s
                ON s.source_db = 'gwas_catalog'
               AND s.current_source_version_id = g.source_version_id
             WHERE g.chrom::VARCHAR IN ({chrom_list})
               AND g.pos_grch38 > 0
        )
        SELECT chrom, pos
          FROM all_positions
         ORDER BY chrom, pos
        """,  # noqa: S608
    ).fetchall()

    by_chrom: dict[str, list[int]] = {chrom: [] for chrom in SUPPORTED_CHROMS}
    union_total = 0
    for chrom_value, pos_value in union_rows:
        chrom_str = str(chrom_value)
        if chrom_str not in by_chrom:
            continue
        by_chrom[chrom_str].append(int(pos_value))
        union_total += 1

    composition = {
        "user": user_count,
        "clinvar": clinvar_count,
        "gwas": gwas_count,
        "union_total": union_total,
    }
    return _FilterSet(positions=by_chrom, composition=composition)


# ---------------------------------------------------------------------------
# Position coalescing.
# ---------------------------------------------------------------------------


def _coalesce_positions(
    positions: list[int],
    max_gap: int,
) -> list[tuple[int, int]]:
    """Merge sorted positions into inclusive ranges when within ``max_gap``.

    Adjacent positions whose gap (``next - current``) is less than or
    equal to ``max_gap`` merge into one ``(start, end)`` tuple.
    Positions farther apart than ``max_gap`` produce separate ranges.

    Returns ``[]`` for an empty input. A single position ``p`` returns
    ``[(p, p)]``.

    Pure function — exercised independently by unit tests without a
    DuckDB connection or network. Tabix accepts ``start:end`` ranges
    inclusively on both ends, so the merged range covers every
    original position; gnomAD records inside the gaps (sites we don't
    care about) are still seen by the iterator and filtered in
    :func:`_load_chromosome`.
    """
    if not positions:
        return []
    out: list[tuple[int, int]] = []
    start = positions[0]
    end = positions[0]
    for pos in positions[1:]:
        if pos - end <= max_gap:
            end = pos
        else:
            out.append((start, end))
            start = pos
            end = pos
    out.append((start, end))
    return out


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
        table_data[col] = pa.array([r[col] for r in rows], type=pa.float64())
    table_data["filter_status"] = pa.array(
        [r["filter_status"] for r in rows],
        type=pa.string(),
    )
    table_data["source_version_id"] = pa.array(
        [r["source_version_id"] for r in rows],
        type=pa.int64(),
    )
    table_data["retrieval_date"] = pa.array(
        [r["retrieval_date"] for r in rows],
        type=pa.timestamp("us"),
    )
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

    The remote-tabix VCF open is the actual data fetch, but it does
    not flow through the audited HTTP client (cyvcf2 talks to htslib's
    libcurl directly). To keep the audit-log invariant — every external
    call is logged — the loader issues an audited HEAD against the
    same URL once per chromosome / data_type. The HEAD's response
    headers carry no genome content; the audit row records that the
    URL was opened.

    A non-200 HEAD does not abort the chromosome — gnomAD's GCS bucket
    occasionally rejects HEAD against the canonical URL even when GET
    works fine. The loader logs the failure and proceeds; the real
    error path is the cyvcf2 open inside :func:`_load_chromosome`.
    """
    try:
        client.request(
            "HEAD",
            url,
            resource_type="annotation_source",
            resource_id=_REMOTE_OPEN_RESOURCE_ID,
        )
    except ExternalCallError as exc:
        logger.info("gnomad.audited_head_non_fatal", url=url, error=str(exc))


def _load_chromosome(  # noqa: C901, PLR0913, PLR0915 — irreducible per-chrom configuration + retry loop
    conn: DuckDBPyConnection,
    audited_client: ExternalClient,
    chrom: str,
    regions: list[tuple[int, int]],
    filter_positions: frozenset[int],
    source_version_id: int,
    retrieval_datetime: datetime,
    batch_size: int,
) -> int:
    """Iterate gnomAD's remote VCFs for ``chrom`` and insert filtered rows.

    Steps for each ``data_type`` in ``("exomes", "genomes")``:

    1. Issue an audited HEAD against the URL (paper trail).
    2. Open the remote VCF via cyvcf2 ``VCF(url)`` inside a stderr-tap
       context that captures htslib's C-level error writes.
    3. For each ``(start, end)`` tabix range, iterate records. After
       the inner iterator exhausts, the tap's :meth:`_StderrTap.check`
       reports whether libcurl tripped a transient HTTP/2 framing
       error during the read (the gnomAD-on-GCS failure mode that
       silently corrupts htslib's iterator state and turns every
       subsequent ``vcf(region)`` call into an empty yield). When
       detected, the VCF is closed + reopened and the region is
       re-iterated. Bounded by :data:`MAX_REMOTE_REGION_ATTEMPTS`.
    4. Reject records whose position is not in ``filter_positions``
       (the coalesced ranges cover gaps between actual filter
       positions; the membership check is the precise filter).
    5. Build per-row dicts via :func:`_record_to_row`; dedup by
       ``(chrom, pos, ref, alt)`` with first-write-wins (exomes
       iterates first → exomes-derived AF wins on overlapping sites).
       The dedup set is shared across retry attempts, so any records
       yielded before a mid-region corruption event are simply re-
       skipped on the retry pass — record re-yields are idempotent.
    6. Flush every ``batch_size`` rows via :func:`_insert_batch`.

    Returns the number of rows inserted for the chromosome.
    """
    from cyvcf2 import VCF  # noqa: PLC0415 — local import keeps module surface narrow

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

    for data_type in ("exomes", "genomes"):
        url = GNOMAD_URL_TEMPLATE.format(data_type=data_type, chrom=chrom)
        _audited_head(audited_client, url)
        logger.info("gnomad.remote_open", chrom=chrom, data_type=data_type, url=url)
        reopens = 0
        with _StderrTap() as detector:
            # Discard any stderr written during VCF construction; tests
            # mock VCF and write nothing, the real cyvcf2 may emit
            # benign warnings (e.g. about index recency) that should
            # not look like a corruption event to the first region's
            # post-iteration check.
            vcf = VCF(url)
            detector.check()
            try:
                for start, end in regions:
                    region = f"chr{chrom}:{start}-{end}"
                    recovered = False
                    for attempt in range(MAX_REMOTE_REGION_ATTEMPTS):
                        for record in vcf(region):
                            _consume_record(record)
                        if not detector.check():
                            recovered = True
                            break
                        # Transient htslib error during read. Close the
                        # corrupted handle and reopen against the same
                        # URL only if there is another attempt left —
                        # the final attempt's failure leads straight to
                        # raise, no point spending a reopen on a handle
                        # that will never be used. The seen_keys set
                        # makes record re-yields on the next attempt
                        # idempotent.
                        if attempt + 1 == MAX_REMOTE_REGION_ATTEMPTS:
                            break
                        # Corrupt-handle close may raise; the open
                        # call that follows discards the handle anyway,
                        # so a close failure here is not actionable.
                        with contextlib.suppress(Exception):
                            vcf.close()
                        vcf = VCF(url)
                        # Drain stderr produced during reopen so the
                        # next region's check starts clean.
                        detector.check()
                        reopens += 1
                        logger.warning(
                            "gnomad.chrom.htslib_recover",
                            chrom=chrom,
                            data_type=data_type,
                            region=region,
                            attempt=attempt + 1,
                            reopens=reopens,
                        )
                    if not recovered:
                        msg = (
                            f"gnomAD region {region!r} for chrom {chrom} "
                            f"({data_type}) failed after "
                            f"{MAX_REMOTE_REGION_ATTEMPTS} attempts — "
                            "persistent htslib transient errors"
                        )
                        raise GnomadRemoteIterationError(msg)
            finally:
                # Final close of whichever VCF handle is current. The
                # last handle may be corrupt; suppress close errors so
                # the outer summary log still runs.
                with contextlib.suppress(Exception):
                    vcf.close()
        if reopens:
            logger.info(
                "gnomad.chrom.htslib_recover_summary",
                chrom=chrom,
                data_type=data_type,
                reopens=reopens,
            )

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
    4. Build the three-way filter set and record composition counts.
    5. Restrict the chromosome list to the intersection with
       :data:`SUPPORTED_CHROMS`; skip chroms already populated when
       resuming.
    6. Per chromosome: coalesce filter positions into tabix ranges,
       open the remote VCFs, filter records, dedup, chunk-insert.
       Partial failures stop the run but leave already-loaded
       chromosomes under the new ``source_version_id``.
    7. When the full chrom set landed (and the caller didn't restrict
       via ``chromosomes``), flip the ``annotation_sources`` pointer
       and commit.
    8. Compute the drift-identifier summary and return.

    See finding-011 for the three-way-vs-four-way design discussion.
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
    succeeded: list[str] = []
    failed: list[str] = []
    capture_failure: BaseException | None = None

    # 6. Per-chromosome load.
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
            import contextlib  # noqa: PLC0415 — local import keeps module-level surface narrow

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
) -> RefreshResult:
    """Refresh gnomAD filtered allele frequencies.

    Registry adapter around :func:`load`. The registry's RefreshFn
    signature is ``(force, skip_if_same_version) -> RefreshResult``;
    the gnomad CLI threads the rich kwargs through. The bare-form
    ``refresh(force, skip_if_same_version)`` from the registry path
    runs against the full SUPPORTED_CHROMS set with default coalesce.

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
