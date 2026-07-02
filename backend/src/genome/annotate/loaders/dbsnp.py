"""dbSNP build 157 loader — canonical rsIDs + variant metadata for user variants.

Streams NCBI's single multi-chromosome dbSNP GRCh38 VCF
(``GCF_000001405.40.gz``, bgzipped + tabix-indexed, ~29.5 GB) via cyvcf2
remote-tabix queries, filters every site to the **user's own variant
positions** (the ``user_only`` filter strategy — see finding-016), and
chunk-loads the per-variant rows into ``dbsnp_annotations`` via PyArrow Table
registration + ``INSERT ... SELECT`` (the project's locked bulk-load
convention).

Sub-phase 5.6 — the seventh and last Phase-5 reference-annotation source, and
the **second remote-tabix source** after gnomAD (5.5). The generic
htslib/HTTP-2 machinery (open → iterate → detect → reopen → retry, the
``_StderrTap`` corruption detector, position coalescing, the audited HEAD,
the libcurl pre-flight) lives in :mod:`genome.annotate.remote_tabix`;
this loader supplies the dbSNP-specific projection, the single-file
per-chromosome iteration, and the version-pointer supersession lifecycle.

dbSNP's value here is annotating the user's variants: canonical rsIDs,
canonical REF/ALT, gene symbols, and variant class. Those consumers all
operate on ``variants_master``, so the filter is ``user_only`` (distinct user
positions) — the same filter gnomAD adopted in finding-035; the three-way
``(user U ClinVar U GWAS)`` strategy is retained in
:mod:`genome.annotate.filter_set` only as gnomAD's revert path. dbSNP's
ClinVar/GWAS/PGS legs are deferred — see finding-016.

Three dbSNP-specific contracts, all ratified against the real source before
this loader was written (finding-013 gate, recorded in the PR description):

* **rsid comes from the VCF ID column (``record.ID``), never ``INFO/RS``.**
  dbSNP build 156+ emits ``RS`` INFO values exceeding 2^31; htslib sets them
  to missing (``[W::vcf_parse_info] Extreme INFO/RS value ...``) so ``INFO/RS``
  is unreliable. ``record.ID`` carries the canonical ``rs<n>`` string.
* **#CHROM uses RefSeq accessions** (``NC_000001.11`` ... ``NC_012920.1``),
  not ``chrN``. The accession <-> canonical-chrom map is the stable RefSeq
  GRCh38.p14 (GCF_000001405.40) assembly definition, hard-coded here and
  validated against the VCF header's ``##contig`` set at pre-flight (a loud
  error if NCBI ever bumps the patch and renames an accession).
* **multi-allelic sites are kept as an array.** ``alt_alleles`` is
  ``VARCHAR[]``; the loader does NOT split or reject multi-allelic records
  (the opposite of gnomAD, whose public per-chrom VCFs are pre-split).

``functional_class`` is left NULL in PR B: build 157 carries only coarse
legacy function-class *flags* (NSM/SYN/INT/U3/U5/...), not a single VEP-grade
consequence value; populating it is deferred to VEP (Phase 6). ``is_clinical``
is populated from the presence of the ``CLNSIG`` INFO key (no ClinVar join).
``variant_aliases`` is NOT populated in PR B (it pairs with the tier-2 rsID
backfill, finding-005 #4 — Phase 6+).

Supersession is via the ``annotation_sources`` pointer table (finding-010
version-pointer pattern), identical to gnomAD: new content lands under a fresh
``source_version_id`` for the duration of the run, and the ``dbsnp`` pointer
flips to that id in one statement once the full chrom set lands. Partial
``--chromosomes`` runs do not flip; ``--resume`` continues an in-flight load.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import httpx
import pyarrow as pa
import structlog

from genome.annotate.filter_set import build_filter_set
from genome.annotate.registry import RefreshResult, register_loader
from genome.annotate.remote_tabix import (
    RemoteReadStats,
    RemoteTabixLibcurlMissingError,
    audited_head,
    coalesce_positions,
    iter_remote_vcf_regions,
)
from genome.annotate.source_versions import (
    get_current_version,
    insert_source_version,
)
from genome.annotate.supersession import (
    _next_dbsnp_id,
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
# Upstream source (ratified 2026-05-25; finding-013 gate).
# ---------------------------------------------------------------------------

URL_VERIFIED_DATE: Final[str] = "2026-05-25"

DBSNP_VCF_URL: Final[str] = (
    "https://ftp.ncbi.nlm.nih.gov/snp/latest_release/VCF/GCF_000001405.40.gz"
)
"""Single multi-chromosome dbSNP GRCh38 VCF (bgzipped + tabix-indexed)."""

DBSNP_VERSION: Final[str] = "157"
"""Locked source build (``##dbSNP_BUILD_ID=157``, ``##reference=GRCh38.p14``).

CLI ``--version VERSION`` overrides at refresh time.
"""

DEFAULT_BATCH_SIZE: Final[int] = 50_000
"""Bulk-insert chunk size; see gnomAD's note — 50K rows amortises INSERT cost."""

DEFAULT_COALESCE_DISTANCE_BP: Final[int] = 50_000
"""Default tabix-range coalescing gap (bp).

50 kb is the value finding-012 settled on for the gnomAD/GCS bucket; NCBI's
FTP host serves the same HTTP/2 reality, and >= 50 kb keeps reopens rare
(finding-012 #10). Tunable per refresh via the CLI ``--coalesce-distance N``.
"""

# RefSeq GRCh38.p14 (GCF_000001405.40) canonical chromosome accessions. This is
# the stable assembly definition, validated against the VCF header ``##contig``
# set at pre-flight (the header carries only accession IDs, not the chrom
# label, so the map cannot be *derived* from it — only ratified against it).
_CHROM_TO_ACCESSION: Final[dict[str, str]] = {
    "1": "NC_000001.11",
    "2": "NC_000002.12",
    "3": "NC_000003.12",
    "4": "NC_000004.12",
    "5": "NC_000005.10",
    "6": "NC_000006.12",
    "7": "NC_000007.14",
    "8": "NC_000008.11",
    "9": "NC_000009.12",
    "10": "NC_000010.11",
    "11": "NC_000011.10",
    "12": "NC_000012.12",
    "13": "NC_000013.11",
    "14": "NC_000014.9",
    "15": "NC_000015.10",
    "16": "NC_000016.10",
    "17": "NC_000017.11",
    "18": "NC_000018.10",
    "19": "NC_000019.10",
    "20": "NC_000020.11",
    "21": "NC_000021.9",
    "22": "NC_000022.11",
    "X": "NC_000023.11",
    "Y": "NC_000024.10",
    "MT": "NC_012920.1",
}
_ACCESSION_TO_CHROM: Final[dict[str, str]] = {
    accession: chrom for chrom, accession in _CHROM_TO_ACCESSION.items()
}

SUPPORTED_CHROMS: Final[tuple[str, ...]] = (
    *(str(n) for n in range(1, 23)),
    "X",
    "Y",
    "MT",
)
"""Canonical chromosomes loaded from dbSNP: 1-22, X, Y, MT.

Unlike gnomAD (which excludes Y/MT), dbSNP ships high-confidence rsIDs for
every canonical chromosome, and the user's 23andMe export carries Y + MT
positions worth annotating. The CLI ``--chromosomes LIST`` flag filters within
this set.
"""

# dbSNP ``INFO/VC`` (variation class) -> the schema's ``variant_class``
# vocabulary {snv, in-del, mnv}. Gate-confirmed value seen: ``SNV``. The rest
# are mapped defensively against dbSNP's documented VC vocabulary; an
# unrecognised VC maps to NULL (logged at debug, not dropped).
_VC_TO_VARIANT_CLASS: Final[dict[str, str]] = {
    "SNV": "snv",
    "MNV": "mnv",
    "INS": "in-del",
    "DEL": "in-del",
    "INDEL": "in-del",
    "DIV": "in-del",
}

SOURCE_DB: Final[str] = "dbsnp"
_TARGET_TABLE: Final[str] = "dbsnp_annotations"
_PREFLIGHT_RESOURCE_ID: Final[str] = "dbsnp_libcurl_preflight"
_REMOTE_OPEN_RESOURCE_ID: Final[str] = "dbsnp_remote_vcf_open"


# Arrow schema used by :func:`_insert_batch`. Column order matches the INSERT
# column list. ``alt_alleles`` and ``gene_symbols`` are list columns
# (``VARCHAR[]`` in the DDL) — the multi-allelic-as-array and gene-symbol-list
# projections depend on this.
_ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("dbsnp_id", pa.int64(), nullable=False),
        pa.field("rsid", pa.string(), nullable=False),
        pa.field("chrom", pa.string()),
        pa.field("pos_grch38", pa.int64()),
        pa.field("pos_grch37", pa.int64()),
        pa.field("ref_allele", pa.string()),
        pa.field("alt_alleles", pa.list_(pa.string())),
        pa.field("variant_class", pa.string()),
        pa.field("gene_symbols", pa.list_(pa.string())),
        pa.field("functional_class", pa.string()),
        pa.field("is_clinical", pa.bool_()),
        pa.field("source_version_id", pa.int64(), nullable=False),
        pa.field("retrieval_date", pa.timestamp("us"), nullable=False),
    ],
)


# ---------------------------------------------------------------------------
# Result dataclass.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DbsnpLoadResult:
    """Outcome of one :func:`load` invocation.

    Carries every drift identifier the runbook compares across builds
    (per-chrom row counts, filter-set composition, match rate vs
    ``variants_master``, the ``variant_class`` distribution, the gene-symbol
    and multi-allelic and clinical counts) plus the operational status.

    ``reopens_total`` is the run-total htslib-reopen count (finding-012 #12) —
    a tolerance-banded network signal, NOT a byte-exact drift anchor. dbSNP is
    sequential-only (one accumulator threaded through the chrom loop), so unlike
    gnomAD there is no parallel-failure under-count: a failed chromosome's
    partial is still surfaced (it is mutated at the reopen site before the
    raise).
    """

    version_label: str
    source_version_id: int | None
    pointer_flipped: bool
    rows_loaded: int
    distinct_variants_per_chrom: dict[str, int]
    filter_set_composition: dict[str, int]
    match_rate: float
    variant_class_distribution: dict[str, int]
    gene_symbols_present: int
    multiallelic_rows: int
    is_clinical_rows: int
    chromosomes_succeeded: tuple[str, ...]
    chromosomes_failed: tuple[str, ...]
    wall_clock_seconds: float
    reopens_total: int


# ---------------------------------------------------------------------------
# Custom errors.
# ---------------------------------------------------------------------------


class DbsnpSourceContigError(RuntimeError):
    """Raised when the dbSNP VCF header is missing an expected RefSeq accession.

    The accession map (:data:`_CHROM_TO_ACCESSION`) is the stable RefSeq
    GRCh38.p14 assembly definition. If NCBI bumps the assembly patch and
    renames a canonical accession, querying by the stale accession would
    silently return zero records — a regression that looks like success.
    The pre-flight raises this instead, so the operator sees a real error.
    """


# ---------------------------------------------------------------------------
# Pre-flight.
# ---------------------------------------------------------------------------


def _validate_source_contigs(raw_header: str) -> None:
    """Confirm every canonical RefSeq accession appears in the VCF ``##contig``.

    Raises :class:`DbsnpSourceContigError` listing any missing accession.
    """
    present: set[str] = set()
    for line in raw_header.splitlines():
        if line.startswith("##contig"):
            match = re.search(r"ID=([^,>]+)", line)
            if match is not None:
                present.add(match.group(1))
    missing = [acc for acc in _CHROM_TO_ACCESSION.values() if acc not in present]
    if missing:
        msg = (
            f"dbSNP source {DBSNP_VCF_URL} is missing expected canonical RefSeq "
            f"accessions in its ##contig header: {missing}. NCBI may have bumped "
            "the GRCh38 assembly patch — update _CHROM_TO_ACCESSION to match."
        )
        raise DbsnpSourceContigError(msg)


def _check_source_available() -> None:
    """Pre-flight: open the dbSNP VCF and ratify its observable schema.

    A successful remote ``cyvcf2.VCF(url)`` open fetches the header over
    htslib's libcurl, so it doubles as the libcurl-available probe (a
    libcurl-missing htslib build fails the open). The fetched header is then
    checked against :data:`_CHROM_TO_ACCESSION` so a future NCBI patch bump
    fails loudly rather than silently returning zero rows.

    Raises :class:`RemoteTabixLibcurlMissingError` when the open fails (the
    library/network problem) and :class:`DbsnpSourceContigError` when the open
    succeeds but a canonical accession is missing (the schema-drift problem).
    """
    from cyvcf2 import VCF  # noqa: PLC0415

    try:
        vcf = VCF(DBSNP_VCF_URL)
    except Exception as exc:
        msg = (
            f"dbSNP remote VCF open failed for {DBSNP_VCF_URL!r}. "
            "htslib must be built with libcurl support; this environment "
            "doesn't have it (or the NCBI host is unreachable). "
            "Rebuild htslib (and cyvcf2 against it) with libcurl enabled."
        )
        raise RemoteTabixLibcurlMissingError(msg) from exc
    try:
        _validate_source_contigs(vcf.raw_header)
    finally:
        vcf.close()


# ---------------------------------------------------------------------------
# Per-record extraction.
# ---------------------------------------------------------------------------


def _info_get_str(info: object, key: str) -> str | None:
    """Return ``info[key]`` as a str when present, else ``None``.

    cyvcf2's ``record.INFO`` raises ``KeyError`` on missing keys, so the
    lookup is wrapped. (The test fakes use a plain ``dict``, which behaves the
    same.)
    """
    try:
        value = info[key]  # type: ignore[index]
    except (KeyError, TypeError):
        return None
    if value is None:
        return None
    return str(value)


def _info_has(info: object, key: str) -> bool:
    """Return True when ``info[key]`` is present and non-None."""
    try:
        value = info[key]  # type: ignore[index]
    except (KeyError, TypeError):
        return False
    return value is not None


def _parse_geneinfo(value: str | None) -> list[str] | None:
    """Parse a dbSNP ``GENEINFO`` value into a list of gene symbols.

    Format is ``symbol:geneid`` pairs delimited by ``|`` (e.g.
    ``HES4:57801`` or ``A:1|B:2``). Returns the symbols (the part before the
    first ``:`` of each segment), or ``None`` when the value is empty/absent.
    """
    if not value:
        return None
    symbols: list[str] = []
    for segment in value.split("|"):
        token = segment.strip()
        if not token:
            continue
        symbol = token.split(":", 1)[0].strip()
        if symbol:
            symbols.append(symbol)
    return symbols or None


def _record_to_dbsnp_row(  # noqa: PLR0911 — defensive per-field guards (rsid/chrom/pos/ref/alt)
    record: object,
    source_version_id: int,
    retrieval_datetime: datetime,
) -> dict[str, object] | None:
    """Project one cyvcf2 record into a row destined for ``dbsnp_annotations``.

    ``rsid`` reads from ``record.ID`` (the ``rs<n>`` string) — never
    ``INFO/RS`` (int32 overflow on build 156+). ``chrom`` translates the
    RefSeq accession via :data:`_ACCESSION_TO_CHROM`. Multi-allelic ALTs are
    kept as a list (``alt_alleles``). ``variant_class`` maps ``INFO/VC``;
    ``gene_symbols`` parses ``INFO/GENEINFO``; ``is_clinical`` is the presence
    of ``INFO/CLNSIG``; ``functional_class`` and ``pos_grch37`` are NULL in
    PR B. Returns ``None`` for records the loader cannot store (missing rsid,
    non-canonical contig, missing REF/ALT).
    """
    rsid = getattr(record, "ID", None)
    if not isinstance(rsid, str) or not rsid or rsid == ".":
        return None

    chrom_raw = getattr(record, "CHROM", None)
    if not isinstance(chrom_raw, str):
        return None
    chrom = _ACCESSION_TO_CHROM.get(chrom_raw)
    if chrom is None:
        # We only ever query canonical accessions, so this is defensive.
        return None

    pos = getattr(record, "POS", None)
    if not isinstance(pos, int):
        try:
            pos = int(pos)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    ref = getattr(record, "REF", None)
    if not isinstance(ref, str) or not ref:
        return None

    alts_raw: tuple[object, ...] = tuple(getattr(record, "ALT", ()) or ())
    alt_alleles = [a for a in alts_raw if isinstance(a, str) and a]
    if not alt_alleles:
        return None

    info = getattr(record, "INFO", {})
    vc = _info_get_str(info, "VC")
    variant_class = _VC_TO_VARIANT_CLASS.get(vc.upper()) if vc else None

    return {
        "rsid": rsid,
        "chrom": chrom,
        "pos_grch38": int(pos),
        "pos_grch37": None,
        "ref_allele": ref,
        "alt_alleles": alt_alleles,
        "variant_class": variant_class,
        "gene_symbols": _parse_geneinfo(_info_get_str(info, "GENEINFO")),
        "functional_class": None,
        "is_clinical": _info_has(info, "CLNSIG"),
        "source_version_id": source_version_id,
        "retrieval_date": retrieval_datetime.astimezone(UTC).replace(tzinfo=None),
    }


# ---------------------------------------------------------------------------
# Bulk-insert helpers.
# ---------------------------------------------------------------------------


def _insert_batch(
    conn: DuckDBPyConnection,
    rows: list[dict[str, object]],
    *,
    base_id: int,
) -> int:
    """Bulk-insert one batch into ``dbsnp_annotations`` via PyArrow + INSERT...SELECT.

    ``dbsnp_id`` is allocated as ``range(base_id, base_id + n)``. The Arrow
    ``alt_alleles`` / ``gene_symbols`` columns are ``list<string>`` and land
    in the DuckDB ``VARCHAR[]`` columns directly.
    """
    if not rows:
        return 0
    n = len(rows)
    table_data: dict[str, pa.Array] = {
        "dbsnp_id": pa.array(range(base_id, base_id + n), type=pa.int64()),
        "rsid": pa.array([r["rsid"] for r in rows], type=pa.string()),
        "chrom": pa.array([r["chrom"] for r in rows], type=pa.string()),
        "pos_grch38": pa.array([r["pos_grch38"] for r in rows], type=pa.int64()),
        "pos_grch37": pa.array([r["pos_grch37"] for r in rows], type=pa.int64()),
        "ref_allele": pa.array([r["ref_allele"] for r in rows], type=pa.string()),
        "alt_alleles": pa.array(
            [r["alt_alleles"] for r in rows],
            type=pa.list_(pa.string()),
        ),
        "variant_class": pa.array([r["variant_class"] for r in rows], type=pa.string()),
        "gene_symbols": pa.array(
            [r["gene_symbols"] for r in rows],
            type=pa.list_(pa.string()),
        ),
        "functional_class": pa.array(
            [r["functional_class"] for r in rows],
            type=pa.string(),
        ),
        "is_clinical": pa.array([r["is_clinical"] for r in rows], type=pa.bool_()),
        "source_version_id": pa.array(
            [r["source_version_id"] for r in rows],
            type=pa.int64(),
        ),
        "retrieval_date": pa.array(
            [r["retrieval_date"] for r in rows],
            type=pa.timestamp("us"),
        ),
    }
    table = pa.table(table_data, schema=_ARROW_SCHEMA)

    try:
        conn.register("_dbsnp_stage_arrow", table)
        conn.execute(
            f"""
            INSERT INTO {_TARGET_TABLE} (
                dbsnp_id, rsid, chrom, pos_grch38, pos_grch37, ref_allele,
                alt_alleles, variant_class, gene_symbols, functional_class,
                is_clinical, source_version_id, retrieval_date
            )
            SELECT
                dbsnp_id, rsid, chrom::chromosome_enum, pos_grch38, pos_grch37,
                ref_allele, alt_alleles, variant_class, gene_symbols,
                functional_class, is_clinical, source_version_id, retrieval_date
              FROM _dbsnp_stage_arrow
            """,  # noqa: S608 — table + column lists are module-controlled
        )
    finally:
        conn.unregister("_dbsnp_stage_arrow")
    return n


# ---------------------------------------------------------------------------
# Per-chromosome iteration.
# ---------------------------------------------------------------------------


def _load_chromosome(  # noqa: PLR0913 — irreducible per-chrom configuration
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
    """Stream the dbSNP VCF for ``chrom`` and insert filtered rows.

    Translates ``chrom`` to its RefSeq accession, builds accession-prefixed
    tabix region strings from the coalesced ``regions``, issues an audited
    HEAD (paper trail), then streams the single dbSNP VCF via
    :func:`genome.annotate.remote_tabix.iter_remote_vcf_regions` (which owns
    the open/reopen/retry loop and emits ``dbsnp.remote_open`` /
    ``dbsnp.chrom.htslib_recover`` events). Each yielded record is consumed:

    * Reject records whose position is not in ``filter_positions`` (the
      coalesced ranges cover gaps between actual user positions; the
      membership check is the precise filter).
    * Project via :func:`_record_to_dbsnp_row`; dedup by ``rsid`` (one rs per
      dbSNP record, so re-yields after a mid-region reopen land at most once).
    * Flush every ``batch_size`` rows via :func:`_insert_batch`.

    Returns the number of rows inserted for the chromosome.
    """
    accession = _CHROM_TO_ACCESSION[chrom]
    seen_rsids: set[str] = set()
    pending: list[dict[str, object]] = []
    inserted = 0
    base_id = _next_dbsnp_id(conn)

    def _flush() -> None:
        nonlocal inserted, base_id, pending
        if not pending:
            return
        flushed = _insert_batch(conn, pending, base_id=base_id)
        inserted += flushed
        base_id += flushed
        logger.info(
            "dbsnp.bulk_insert.chunk",
            chrom=chrom,
            rows=flushed,
            cumulative=inserted,
        )
        pending = []

    def _consume_record(record: object) -> None:
        row = _record_to_dbsnp_row(record, source_version_id, retrieval_datetime)
        if row is None:
            return
        pos_obj = row["pos_grch38"]
        if not isinstance(pos_obj, int) or pos_obj not in filter_positions:
            return
        rsid = str(row["rsid"])
        if rsid in seen_rsids:
            return
        seen_rsids.add(rsid)
        pending.append(row)
        if len(pending) >= batch_size:
            _flush()

    region_strings = [f"{accession}:{start}-{end}" for start, end in regions]
    audited_head(
        audited_client,
        DBSNP_VCF_URL,
        resource_id=_REMOTE_OPEN_RESOURCE_ID,
        event_prefix=SOURCE_DB,
        log=logger,
    )
    for record in iter_remote_vcf_regions(
        DBSNP_VCF_URL,
        region_strings,
        event_prefix=SOURCE_DB,
        log_context={"chrom": chrom},
        log=logger,
        reopen_stats=reopen_stats,
    ):
        _consume_record(record)

    _flush()
    return inserted


# ---------------------------------------------------------------------------
# Resume + cleanup helpers (mirror gnomAD).
# ---------------------------------------------------------------------------


def _find_in_flight_source_version_id(
    conn: DuckDBPyConnection,
    version: str,
) -> int | None:
    """Return a partially-loaded new ``source_version_id`` for ``version`` if any.

    "In-flight" means an ``annotation_source_versions`` row exists for
    ``(source_db='dbsnp', version=<version>)`` but the ``annotation_sources``
    pointer doesn't name it yet — a prior run that inserted some chromosomes
    and exited without flipping. Returns the largest such id so ``--resume``
    continues against the most recent attempt; ``None`` when none exists.
    """
    row = conn.execute(
        """
        SELECT asv.source_version_id
          FROM annotation_source_versions asv
          LEFT JOIN annotation_sources a
            ON a.source_db = 'dbsnp'
           AND a.current_source_version_id = asv.source_version_id
         WHERE asv.source_db = 'dbsnp'
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


def _cleanup_orphan_version_row(
    conn: DuckDBPyConnection,
    source_version_id: int,
) -> None:
    """Best-effort delete of an orphan ``annotation_source_versions`` row.

    Same shape as the sibling Phase-5 loaders (finding-015): called when a
    freshly-allocated dbsnp version row has zero ``dbsnp_annotations`` rows
    referencing it after the per-chromosome loop exits (a partial run that
    landed nothing, or a failure before any per-chrom commit). The caller
    guards on ``not pointer_flipped`` so the active version is never removed.
    """
    try:
        conn.execute(
            "DELETE FROM annotation_source_versions WHERE source_version_id = ?",
            [source_version_id],
        )
    except Exception:  # noqa: BLE001 — best-effort cleanup; caller has already raised/returned
        logger.warning(
            "dbsnp.cleanup.orphan_version_row_delete_failed",
            source_version_id=source_version_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Post-load summary.
# ---------------------------------------------------------------------------


def _summarize_run(
    conn: DuckDBPyConnection,
    source_version_id: int,
) -> tuple[int, dict[str, int], float, dict[str, int], int, int, int]:
    """Compute the locked drift identifiers for the just-loaded source version.

    Returns ``(rows_loaded, distinct_variants_per_chrom, match_rate,
    variant_class_distribution, gene_symbols_present, multiallelic_rows,
    is_clinical_rows)``.
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
        "SELECT COUNT(*) FROM (SELECT DISTINCT chrom, pos_grch38 FROM variants_master)",
    ).fetchone()
    user_total = int(user_total_row[0]) if user_total_row is not None else 0

    if user_total > 0:
        overlap_row = conn.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT DISTINCT vm.chrom, vm.pos_grch38
                  FROM variants_master vm
                  JOIN {_TARGET_TABLE} d
                    ON d.chrom = vm.chrom
                   AND d.pos_grch38 = vm.pos_grch38
                 WHERE d.source_version_id = ?
            )
            """,  # noqa: S608
            [source_version_id],
        ).fetchone()
        overlap = int(overlap_row[0]) if overlap_row is not None else 0
        match_rate = overlap / user_total
    else:
        match_rate = 0.0

    class_rows = conn.execute(
        f"""
        SELECT COALESCE(variant_class, 'NULL'), COUNT(*)
          FROM {_TARGET_TABLE}
         WHERE source_version_id = ?
         GROUP BY 1
        """,  # noqa: S608
        [source_version_id],
    ).fetchall()
    variant_class_distribution = {str(c): int(n) for c, n in class_rows}

    counts_row = conn.execute(
        f"""
        SELECT
            COUNT(*) FILTER (
                WHERE gene_symbols IS NOT NULL AND len(gene_symbols) > 0
            ),
            COUNT(*) FILTER (WHERE len(alt_alleles) > 1),
            COUNT(*) FILTER (WHERE is_clinical)
          FROM {_TARGET_TABLE}
         WHERE source_version_id = ?
        """,  # noqa: S608
        [source_version_id],
    ).fetchone()
    gene_symbols_present = int(counts_row[0]) if counts_row is not None else 0
    multiallelic_rows = int(counts_row[1]) if counts_row is not None else 0
    is_clinical_rows = int(counts_row[2]) if counts_row is not None else 0

    return (
        rows_loaded,
        distinct_per_chrom,
        match_rate,
        variant_class_distribution,
        gene_symbols_present,
        multiallelic_rows,
        is_clinical_rows,
    )


# ---------------------------------------------------------------------------
# Top-level entrypoint — load.
# ---------------------------------------------------------------------------


def load(  # noqa: C901, PLR0912, PLR0913, PLR0915 — single entry point; per-step branching is explicit
    conn: DuckDBPyConnection,
    audited_client: ExternalClient,
    *,
    force: bool = False,
    version: str = DBSNP_VERSION,
    chromosomes: Sequence[str] | None = None,
    resume: bool = False,
    coalesce_distance: int = DEFAULT_COALESCE_DISTANCE_BP,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> DbsnpLoadResult:
    """Load dbSNP build 157 rsID + variant metadata for the user's variants.

    Pipeline mirrors gnomAD's (finding-010 version-pointer supersession):

    1. ``external_calls_enabled`` gate (audited refusal) + source pre-flight
       (libcurl + ``##contig`` accession validation).
    2. Resolve the active dbsnp source-version; short-circuit when it already
       names ``version`` and neither ``force`` nor ``resume`` is set.
    3. Re-use an in-flight ``source_version_id`` when resuming, else allocate.
    4. Build the ``user_only`` filter set.
    5. Restrict to the intersection with :data:`SUPPORTED_CHROMS`; skip
       already-populated chroms when resuming.
    6. Per chromosome: coalesce positions into tabix ranges, stream the VCF,
       filter, dedup, chunk-insert, commit. Partial failures stop the run but
       leave already-loaded chromosomes under the new ``source_version_id``.
    7. Flip the ``annotation_sources`` pointer only on a full successful run
       not restricted by ``--chromosomes``.
    8. Clean up an orphan version row (zero rows landed) and summarise.
    """
    started = time.monotonic()
    log = logger.bind(source=SOURCE_DB, version=version)

    # 1. Pre-flight.
    if not is_external_enabled():
        # Record the blocked attempt in audit_log before raising (fail-closed).
        try:
            audited_client.request(
                "HEAD",
                DBSNP_VCF_URL,
                resource_type="annotation_source",
                resource_id=_PREFLIGHT_RESOURCE_ID,
            )
        except ExternalCallsDisabledError:
            raise
        except ExternalCallError as exc:
            log.info("dbsnp.preflight_audit_error", error=str(exc))
        raise ExternalCallsDisabledError
    _check_source_available()

    # 2. Current version.
    current = get_current_version(conn, SOURCE_DB)
    if current is not None and current.version == version and not force and not resume:
        log.info("dbsnp.skip_already_current", version=version)
        wall = time.monotonic() - started
        return DbsnpLoadResult(
            version_label=version,
            source_version_id=current.source_version_id,
            pointer_flipped=False,
            rows_loaded=0,
            distinct_variants_per_chrom={},
            filter_set_composition={},
            match_rate=0.0,
            variant_class_distribution={},
            gene_symbols_present=0,
            multiallelic_rows=0,
            is_clinical_rows=0,
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
            log.info("dbsnp.resume_existing", source_version_id=source_version_id)
    if source_version_id is None:
        source_version_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=version,
            source_url=DBSNP_VCF_URL,
            source_file_hash=f"dbsnp_{version}",
            source_file_size=0,
            record_count=None,
        )
        version_row_freshly_allocated = True
        log.info("dbsnp.allocated_new_version", source_version_id=source_version_id)

    # 4. Filter set (user_only).
    filter_set = build_filter_set(
        conn,
        strategy="user_only",
        supported_chroms=SUPPORTED_CHROMS,
    )
    log.info("dbsnp.filter_set_composition", **filter_set.composition)

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
            log.info("dbsnp.resume_skip_chroms", skip=sorted(already))

    retrieval_datetime = datetime.now(UTC)
    succeeded: list[str] = []
    failed: list[str] = []
    capture_failure: BaseException | None = None
    # One run-level reopen accumulator threaded through every chromosome
    # (finding-012 #12). dbSNP is sequential-only, so this always surfaces a
    # failed chromosome's partial — it is mutated at the reopen site before
    # any raise, so it survives a mid-loop break.
    run_stats = RemoteReadStats()

    # 6. Per-chromosome load.
    for chrom in requested:
        positions = filter_set.positions.get(chrom, [])
        if not positions:
            log.info("dbsnp.chrom.no_filter_positions", chrom=chrom)
            succeeded.append(chrom)
            continue
        regions = coalesce_positions(positions, coalesce_distance)
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
                "dbsnp.chrom.complete",
                chrom=chrom,
                rows=n,
                regions=len(regions),
                elapsed_seconds=round(elapsed, 1),
            )
            succeeded.append(chrom)
        except Exception as exc:
            import contextlib  # noqa: PLC0415 — local import keeps module-level surface narrow

            with contextlib.suppress(Exception):
                conn.rollback()
            elapsed = time.monotonic() - chrom_started
            log.exception(
                "dbsnp.chrom.failed",
                chrom=chrom,
                elapsed_seconds=round(elapsed, 1),
            )
            failed.append(chrom)
            capture_failure = exc
            break

    # 7. Pointer flip (only on a full successful run not restricted by --chromosomes).
    pointer_flipped = False
    if not failed and not partial_run and succeeded:
        populated = _populated_chroms(conn, source_version_id)
        required = set(SUPPORTED_CHROMS) - {
            c for c in SUPPORTED_CHROMS if not filter_set.positions.get(c)
        }
        if set(populated) >= required:
            flip_to_new_version(
                conn,
                source=SOURCE_DB,
                table=_TARGET_TABLE,
                new_source_version_id=source_version_id,
            )
            commit_and_checkpoint(conn, source_name=SOURCE_DB)
            pointer_flipped = True
            log.info("dbsnp.pointer_flipped", source_version_id=source_version_id)
        else:
            log.info(
                "dbsnp.pointer_not_flipped_incomplete_coverage",
                populated=sorted(populated),
                expected=list(SUPPORTED_CHROMS),
            )

    if partial_run and not failed:
        log.info(
            "dbsnp.partial_run_pointer_not_flipped",
            requested=requested,
            note="run --resume against the full chrom set to flip the pointer",
        )

    # Backfill record_count, or clean up an orphan version row when nothing
    # landed under a freshly-allocated id (finding-015).
    rows_count_row = conn.execute(
        f"SELECT COUNT(*) FROM {_TARGET_TABLE} WHERE source_version_id = ?",  # noqa: S608
        [source_version_id],
    ).fetchone()
    rows_count = int(rows_count_row[0]) if rows_count_row is not None else 0

    if version_row_freshly_allocated and rows_count == 0 and not pointer_flipped:
        _cleanup_orphan_version_row(conn, source_version_id)
        conn.commit()
        log.info("dbsnp.orphan_version_row_cleaned_up", source_version_id=source_version_id)
        source_version_id = None
        rows_loaded = 0
        distinct_per_chrom: dict[str, int] = {}
        match_rate = 0.0
        variant_class_distribution: dict[str, int] = {}
        gene_symbols_present = 0
        multiallelic_rows = 0
        is_clinical_rows = 0
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
            variant_class_distribution,
            gene_symbols_present,
            multiallelic_rows,
            is_clinical_rows,
        ) = _summarize_run(conn, source_version_id)

    wall = time.monotonic() - started
    log.info(
        "dbsnp.refresh.complete",
        version=version,
        source_version_id=source_version_id,
        pointer_flipped=pointer_flipped,
        rows_loaded=rows_loaded,
        distinct_variants_per_chrom=distinct_per_chrom,
        filter_set_composition=filter_set.composition,
        match_rate=round(match_rate, 4),
        variant_class_distribution=variant_class_distribution,
        gene_symbols_present=gene_symbols_present,
        multiallelic_rows=multiallelic_rows,
        is_clinical_rows=is_clinical_rows,
        chromosomes_succeeded=tuple(succeeded),
        chromosomes_failed=tuple(failed),
        wall_clock_seconds=round(wall, 1),
        reopens_total=run_stats.reopens,
    )

    if capture_failure is not None:
        raise capture_failure

    return DbsnpLoadResult(
        version_label=version,
        source_version_id=source_version_id,
        pointer_flipped=pointer_flipped,
        rows_loaded=rows_loaded,
        distinct_variants_per_chrom=distinct_per_chrom,
        filter_set_composition=filter_set.composition,
        match_rate=match_rate,
        variant_class_distribution=variant_class_distribution,
        gene_symbols_present=gene_symbols_present,
        multiallelic_rows=multiallelic_rows,
        is_clinical_rows=is_clinical_rows,
        chromosomes_succeeded=tuple(succeeded),
        chromosomes_failed=tuple(failed),
        wall_clock_seconds=wall,
        reopens_total=run_stats.reopens,
    )


# ---------------------------------------------------------------------------
# Registry adapter — refresh.
# ---------------------------------------------------------------------------


def refresh(  # noqa: PLR0913 — registry signature + remote-tabix-specific kwargs
    force: bool,  # noqa: FBT001 — positional matches registry's RefreshFn signature
    skip_if_same_version: bool = False,  # noqa: FBT001, FBT002 — opt-in default for the shared flag
    *,
    version: str = DBSNP_VERSION,
    chromosomes: Sequence[str] | None = None,
    resume: bool = False,
    coalesce_distance: int = DEFAULT_COALESCE_DISTANCE_BP,
) -> RefreshResult:
    """Refresh dbSNP annotations.

    Registry adapter around :func:`load`, mirroring gnomAD's. The bare-form
    ``refresh(force, skip_if_same_version)`` from the registry path runs the
    full SUPPORTED_CHROMS set with default coalesce. ``skip_if_same_version``
    is accepted for signature parity but unused — the pre-flight already
    short-circuits on ``current.version == version and not force`` (there is no
    single downloaded artifact whose SHA-256 anchors the match).
    """
    del skip_if_same_version  # see docstring; the pre-flight covers this.

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


# Register at module-import time. The loaders subpackage __init__.py imports
# this module so the registration happens before any CLI dispatch runs.
register_loader(SOURCE_DB, refresh)


__all__ = [
    "DBSNP_VCF_URL",
    "DBSNP_VERSION",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_COALESCE_DISTANCE_BP",
    "SOURCE_DB",
    "SUPPORTED_CHROMS",
    "URL_VERIFIED_DATE",
    "DbsnpLoadResult",
    "DbsnpSourceContigError",
    "load",
    "refresh",
]
