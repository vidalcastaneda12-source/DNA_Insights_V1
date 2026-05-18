"""PharmGKB clinical annotations loader.

Downloads PharmGKB's Clinical Annotations ZIP, parses
``clinical_annotations.tsv``, and bulk-loads
(clinical-annotation x drug) rows into ``pharmgkb_annotations`` via
PyArrow Table registration + ``INSERT ... SELECT`` (the project's locked
bulk-load convention).

Sub-phase 5.1a establishes the template every subsequent Phase 5 source
loader follows (CPIC in 5.1b, ClinVar in 5.2, GWAS in 5.3, etc.):

* Module-level URL constants with a sibling ``URL_VERIFIED_DATE`` so a
  future reader can tell at a glance how stale the link is.
* A ``_resolve_version`` step that prefers the upstream's own date
  metadata over the retrieval timestamp so refreshes are idempotent on
  the same archive.
* A ``refresh(force)`` function that downloads (skip-if-exists),
  short-circuits when ``annotation_source_versions`` already names the
  resolved version, and otherwise upserts + deactivates + bulk-inserts
  inside one DuckDB transaction.
* ``register_loader(SOURCE_DB, refresh)`` at module-import time so the
  CLI registry is populated by importing this module.

The loader is small (~thousands of rows) but the conventions matter: the
next loaders inherit the shape verbatim.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pyarrow as pa
import structlog

from genome.annotate.downloads import download_to_cache
from genome.annotate.registry import RefreshResult, register_loader
from genome.annotate.source_versions import (
    get_current_version,
    upsert_source_version,
)
from genome.annotate.supersession import (
    VersionFlipResult,
    commit_and_checkpoint,
    flip_to_new_version,
    maybe_skip_same_version,
)
from genome.db.duckdb_conn import duckdb_connection

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Upstream URLs (verified 2026-05-15).
#
# PharmGKB's canonical ``api.pharmgkb.org`` URL 303-redirects to its
# S3-hosted ``clinicalAnnotations.zip``. The scaffold's
# ``download_to_cache`` injects an ``httpx.Client(follow_redirects=True)``
# so the redirect is followed transparently and the loader can rely on
# the canonical URL — no per-loader workaround needed.
# Last upstream release at verification time: 2025-07-05 (encoded in the
# ZIP's ``CREATED_YYYY-MM-DD.txt`` marker file — see ``_resolve_version``).
# ---------------------------------------------------------------------------

URL_VERIFIED_DATE: Final[str] = "2026-05-15"
CLINICAL_ANN_ZIP_URL: Final[str] = (
    "https://api.pharmgkb.org/v1/download/file/data/clinicalAnnotations.zip"
)

SOURCE_DB: Final[str] = "pharmgkb"
_TARGET_TABLE: Final[str] = "pharmgkb_annotations"
_CLINICAL_ANN_TSV_NAME: Final[str] = "clinical_annotations.tsv"
_CREATED_FILE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^CREATED_(?P<date>\d{4}-\d{2}-\d{2})\.txt$",
)

# Detection rule for the "Variant/Haplotypes" column: a value that matches
# ``rs<digits>`` end-to-end is an rsID; anything else (star alleles like
# ``CYP2D6*4``, HLA alleles like ``HLA-B*57:01``, descriptive haplotypes
# like ``G6PD A- 202A_376G, G6PD B (reference)``) is captured in
# ``star_allele``. This matches the schema's split between ``rsid`` and
# ``star_allele`` and keeps the bucketing trivially testable.
_RSID_RE: Final[re.Pattern[str]] = re.compile(r"^rs\d+$")

# Drug separator. PharmGKB clinical annotations TSV uses ``;`` to separate
# drugs within one ``Drug(s)`` cell; embedded commas inside a single drug
# name (e.g. ``"Ace Inhibitors, Plain"``) are intentional. Splitting on
# ``,`` would shred those single-drug names — verified against the real
# 2025-07-05 release (919 multi-drug rows used ``;``; 49 single-drug rows
# carried embedded commas with no ``;``).
_DRUG_SEPARATOR: Final[str] = ";"

# Mapping from the PharmGKB header to ``_ParsedRow`` fields. Captured at
# module scope so the mapping is reviewable at a glance and the parser's
# header-name lookup stays declarative.
_HEADER_TO_FIELD: Final[dict[str, str]] = {
    "Clinical Annotation ID": "pgkb_accession",
    "Variant/Haplotypes": "variant_haplotypes",
    "Gene": "gene_symbol",
    "Level of Evidence": "evidence_level",
    "Phenotype Category": "phenotype_category",
    "Drug(s)": "drugs",
    "Phenotype(s)": "phenotypes",
    "URL": "guideline_url",
}

# Arrow schema used by ``_bulk_insert``. Column order matches the INSERT
# column list constructed below; keeping the schema at module scope means
# the structure is reviewable next to the SQL it feeds.
_ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("pharmgkb_id", pa.int64(), nullable=False),
        pa.field("pgkb_accession", pa.string()),
        pa.field("rsid", pa.string()),
        pa.field("chrom", pa.string()),
        pa.field("pos_grch38", pa.int64()),
        pa.field("gene_symbol", pa.string()),
        pa.field("star_allele", pa.string()),
        pa.field("haplotype", pa.string()),
        pa.field("drug_name", pa.string(), nullable=False),
        pa.field("drug_rxnorm_id", pa.string()),
        pa.field("drug_atc_code", pa.string()),
        pa.field("phenotype_category", pa.string()),
        pa.field("functional_status", pa.string()),
        pa.field("evidence_level", pa.string()),
        pa.field("guideline_summary", pa.string()),
        pa.field("guideline_url", pa.string()),
        pa.field("source_version_id", pa.int64(), nullable=False),
        pa.field("retrieval_date", pa.timestamp("us"), nullable=False),
    ],
)


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    """One row destined for ``pharmgkb_annotations``.

    Mirrors the destination schema's variable columns. ``pharmgkb_id``,
    ``source_version_id``, and ``retrieval_date`` are assigned at
    bulk-insert time after parsing completes.
    """

    pgkb_accession: str | None
    rsid: str | None
    chrom: str | None
    pos_grch38: int | None
    gene_symbol: str | None
    star_allele: str | None
    haplotype: str | None
    drug_name: str
    drug_rxnorm_id: str | None
    drug_atc_code: str | None
    phenotype_category: str | None
    functional_status: str | None
    evidence_level: str | None
    guideline_summary: str | None
    guideline_url: str | None


def _resolve_version(zip_path: Path) -> str:
    """Determine the PharmGKB version label for ``zip_path``.

    Strategy:

    1. Look inside the ZIP for a member whose name matches
       ``CREATED_YYYY-MM-DD.txt`` (PharmGKB ships one such file at the
       archive root on every release).
    2. If present, return the date reformatted as ``YYYY_MM_DD`` to match
       the schema doc's suggested label shape (``'2026_04_15'``).
    3. Otherwise fall back to today's UTC date, also formatted as
       ``YYYY_MM_DD``.

    Returning a stable string for the same archive contents is critical:
    it is what makes ``refresh()`` idempotent on re-runs against the
    same cached ZIP.
    """
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            match = _CREATED_FILE_PATTERN.match(name)
            if match is not None:
                date_str = match.group("date")
                return date_str.replace("-", "_")
    today = datetime.now(UTC).strftime("%Y_%m_%d")
    logger.info(
        "pharmgkb.version.no_metadata_fallback",
        zip_path=str(zip_path),
        fallback=today,
    )
    return today


def _parse_variant_field(variant_text: str) -> tuple[str | None, str | None]:
    """Split the ``Variant/Haplotypes`` column into ``(rsid, star_allele)``.

    Detection rule: a value matching ``^rs\\d+$`` is an rsID; anything
    else is captured verbatim in ``star_allele``. The PharmGKB TSV's
    third bucket (descriptive haplotypes like
    ``G6PD A- 202A_376G, G6PD B (reference)``) is intentionally folded
    into ``star_allele`` so the field name stays "non-rsID variant
    identifier"; the schema doesn't carry a separate haplotype-text
    column and the descriptive strings are still queryable via LIKE.

    Empty / whitespace input returns ``(None, None)``.
    """
    text = variant_text.strip()
    if not text:
        return None, None
    if _RSID_RE.match(text):
        return text, None
    return None, text


def _split_drugs(drug_field: str) -> list[str]:
    """Split the ``Drug(s)`` column into individual drug names.

    PharmGKB separates drugs within one cell with ``;`` (semicolon, no
    surrounding whitespace). Some drug names contain commas within
    themselves (e.g. ``"Ace Inhibitors, Plain"``, ``"sulfonamides, urea
    derivatives"``); splitting on ``,`` would corrupt those names.
    Empirically verified against the 2025-07-05 release: 919 rows use
    ``;`` to separate drugs; 49 single-drug rows have embedded commas
    with no ``;``.

    Whitespace around each token is trimmed; empty tokens are dropped.
    """
    return [d.strip() for d in drug_field.split(_DRUG_SEPARATOR) if d.strip()]


def _phenotype_summary(phenotypes: str) -> str | None:
    """Render the ``Phenotype(s)`` cell into a ``guideline_summary`` string.

    PharmGKB clinical annotations carry the disease/condition under the
    ``Phenotype(s)`` header; the schema doesn't have a dedicated column
    for that text. Storing it in ``guideline_summary`` preserves the
    information for query-time use (LIKE matches) without losing it to
    the next loader's pass.

    Empty values map to ``None`` so we don't write empty strings.
    """
    text = phenotypes.strip()
    if not text:
        return None
    return f"Phenotype(s): {text}"


def _empty_to_none(value: str) -> str | None:
    """Return ``None`` for empty / whitespace-only strings, else the trimmed value."""
    trimmed = value.strip()
    return trimmed or None


def _parse_clinical_annotations_tsv(tsv_text: str) -> Iterator[_ParsedRow]:
    """Stream rows from ``clinical_annotations.tsv``.

    Multi-drug rows are expanded inline: an annotation that lists
    drugs ``['warfarin', 'acenocoumarol']`` yields two ``_ParsedRow``
    instances sharing every field except ``drug_name``. Field-name
    mapping from PharmGKB's column headers is captured at module
    scope in :data:`_HEADER_TO_FIELD`.

    Empty TSV cells are normalized to ``None``. The schema's
    ``chrom`` and ``pos_grch38`` columns are emitted as ``None`` for
    every row — PharmGKB's clinical-annotations TSV is rsID/haplotype
    keyed and does not carry genomic coordinates; the dbSNP loader in
    5.4 will cross-reference those positions.
    """
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    if reader.fieldnames is None:
        msg = "PharmGKB clinical_annotations.tsv has no header row"
        raise ValueError(msg)
    missing = [h for h in _HEADER_TO_FIELD if h not in reader.fieldnames]
    if missing:
        msg = (
            f"PharmGKB clinical_annotations.tsv is missing expected columns "
            f"{missing!r}; got {list(reader.fieldnames)!r}"
        )
        raise ValueError(msg)
    for raw in reader:
        pgkb_accession = _empty_to_none(raw["Clinical Annotation ID"])
        rsid, star_allele = _parse_variant_field(raw["Variant/Haplotypes"])
        gene_symbol = _empty_to_none(raw["Gene"])
        evidence_level = _empty_to_none(raw["Level of Evidence"])
        phenotype_category = _empty_to_none(raw["Phenotype Category"])
        guideline_summary = _phenotype_summary(raw["Phenotype(s)"])
        guideline_url = _empty_to_none(raw["URL"])
        drugs = _split_drugs(raw["Drug(s)"])
        for drug_name in drugs:
            yield _ParsedRow(
                pgkb_accession=pgkb_accession,
                rsid=rsid,
                chrom=None,
                pos_grch38=None,
                gene_symbol=gene_symbol,
                star_allele=star_allele,
                haplotype=None,
                drug_name=drug_name,
                drug_rxnorm_id=None,
                drug_atc_code=None,
                phenotype_category=phenotype_category,
                functional_status=None,
                evidence_level=evidence_level,
                guideline_summary=guideline_summary,
                guideline_url=guideline_url,
            )


def _next_pharmgkb_id(conn: DuckDBPyConnection) -> int:
    """``COALESCE(MAX(pharmgkb_id), 0) + 1``.

    Mirrors the project's app-allocated BIGINT PK pattern (see
    :func:`genome.imputation.runs._next_imputation_id` and
    :func:`genome.annotate.source_versions._next_source_version_id`).
    Allocates a fresh ID range starting from the next unused integer so
    the bulk insert can compute every row's ``pharmgkb_id`` from a
    monotonically increasing offset.
    """
    row = conn.execute(
        f"SELECT COALESCE(MAX(pharmgkb_id), 0) FROM {_TARGET_TABLE}",  # noqa: S608
    ).fetchone()
    return int(row[0]) + 1 if row is not None else 1


def _bulk_insert(
    conn: DuckDBPyConnection,
    rows: list[_ParsedRow],
    *,
    source_version_id: int,
    retrieval_date: datetime,
) -> int:
    """Bulk-load ``rows`` into ``pharmgkb_annotations``.

    Builds a PyArrow Table with one column per destination column
    (including ``pharmgkb_id``, ``source_version_id``, and
    ``retrieval_date``), registers it under a temp name, then issues
    ``INSERT INTO pharmgkb_annotations (...) SELECT ... FROM <temp>``
    and unregisters. ``chrom`` is cast through ``chromosome_enum`` in
    the SELECT so the NULLs that are correct for PharmGKB (no
    per-variant positions in the source file) reach the enum-typed
    column cleanly.

    Returns the number of rows inserted. A zero-row call inserts
    nothing and returns 0.
    """
    if not rows:
        return 0

    base_id = _next_pharmgkb_id(conn)
    n = len(rows)
    # Naive UTC datetime: pa.timestamp("us") (no tz) lines up with DuckDB's
    # TIMESTAMP (no tz). Keeping retrieval_date naive avoids the tz-aware
    # cast surprise the prior imputation runs deliberately sidestepped too.
    naive_retrieval = retrieval_date.astimezone(UTC).replace(tzinfo=None)
    table = pa.table(
        {
            "pharmgkb_id": pa.array(range(base_id, base_id + n), type=pa.int64()),
            "pgkb_accession": pa.array([r.pgkb_accession for r in rows], type=pa.string()),
            "rsid": pa.array([r.rsid for r in rows], type=pa.string()),
            "chrom": pa.array([r.chrom for r in rows], type=pa.string()),
            "pos_grch38": pa.array([r.pos_grch38 for r in rows], type=pa.int64()),
            "gene_symbol": pa.array([r.gene_symbol for r in rows], type=pa.string()),
            "star_allele": pa.array([r.star_allele for r in rows], type=pa.string()),
            "haplotype": pa.array([r.haplotype for r in rows], type=pa.string()),
            "drug_name": pa.array([r.drug_name for r in rows], type=pa.string()),
            "drug_rxnorm_id": pa.array([r.drug_rxnorm_id for r in rows], type=pa.string()),
            "drug_atc_code": pa.array([r.drug_atc_code for r in rows], type=pa.string()),
            "phenotype_category": pa.array(
                [r.phenotype_category for r in rows],
                type=pa.string(),
            ),
            "functional_status": pa.array(
                [r.functional_status for r in rows],
                type=pa.string(),
            ),
            "evidence_level": pa.array([r.evidence_level for r in rows], type=pa.string()),
            "guideline_summary": pa.array(
                [r.guideline_summary for r in rows],
                type=pa.string(),
            ),
            "guideline_url": pa.array([r.guideline_url for r in rows], type=pa.string()),
            "source_version_id": pa.array([source_version_id] * n, type=pa.int64()),
            "retrieval_date": pa.array([naive_retrieval] * n, type=pa.timestamp("us")),
        },
        schema=_ARROW_SCHEMA,
    )
    try:
        conn.register("_pharmgkb_stage_arrow", table)
        conn.execute(
            f"""
            INSERT INTO {_TARGET_TABLE} (
                pharmgkb_id, pgkb_accession, rsid, chrom, pos_grch38,
                gene_symbol, star_allele, haplotype,
                drug_name, drug_rxnorm_id, drug_atc_code,
                phenotype_category, functional_status, evidence_level,
                guideline_summary, guideline_url,
                source_version_id, retrieval_date
            )
            SELECT
                pharmgkb_id, pgkb_accession, rsid,
                chrom::chromosome_enum, pos_grch38,
                gene_symbol, star_allele, haplotype,
                drug_name, drug_rxnorm_id, drug_atc_code,
                phenotype_category, functional_status, evidence_level,
                guideline_summary, guideline_url,
                source_version_id, retrieval_date
              FROM _pharmgkb_stage_arrow
            """,  # noqa: S608 — table name is a module constant, not user input
        )
    finally:
        conn.unregister("_pharmgkb_stage_arrow")
    return n


def _cleanup_orphan_version_row(
    conn: DuckDBPyConnection,
    source_version_id: int,
) -> None:
    """Best-effort delete of an orphan ``annotation_source_versions`` row.

    Called when the supersede+insert transaction rolls back. The row was
    inserted by ``upsert_source_version`` in its own (already-committed)
    transaction, so a normal rollback won't remove it. The DELETE is
    FK-safe because no ``pharmgkb_annotations`` rows reference the new
    ``source_version_id`` yet (the bulk_insert never committed).

    Failures during cleanup are swallowed and logged; the caller is
    already raising the original exception and the orphan can be
    cleaned up manually in the worst case.
    """
    try:
        conn.execute(
            "DELETE FROM annotation_source_versions WHERE source_version_id = ?",
            [source_version_id],
        )
    except Exception:  # noqa: BLE001 — best-effort cleanup; original exc re-raised by caller
        logger.warning(
            "pharmgkb.cleanup.orphan_version_row_delete_failed",
            source_version_id=source_version_id,
            exc_info=True,
        )


def refresh(
    force: bool,  # noqa: FBT001 — positional matches registry's RefreshFn signature
    skip_if_same_version: bool = False,  # noqa: FBT001, FBT002 — opt-in default for the new flag
) -> RefreshResult:
    """Refresh PharmGKB clinical annotations.

    Pipeline:

    1. Download ``clinicalAnnotations.zip`` via the audited
       :func:`genome.annotate.downloads.download_to_cache`
       (skip-if-exists by default; ``force=True`` re-downloads).
    2. Resolve the version label from the ZIP's ``CREATED_*.txt``
       member (falls back to retrieval date in ``YYYY_MM_DD`` form when
       absent).
    3. Short-circuit and return ``was_already_current=True`` if a row
       in ``annotation_source_versions`` already names the resolved
       ``(source_db='pharmgkb', version)`` and ``force`` is ``False``.
    3a. If ``skip_if_same_version`` is ``True`` and the downloaded
        archive's (version, sha256) match the currently-active row,
        short-circuit via :func:`maybe_skip_same_version` (finding-009
        #14). Off by default.
    4. Extract ``clinical_annotations.tsv`` and parse it. Multi-drug
       rows expand into one ``_ParsedRow`` per drug.
    5. Inside one DuckDB transaction: upsert
       ``annotation_source_versions``, bulk-insert the freshly parsed
       rows under the new ``source_version_id``, and flip the
       ``annotation_sources`` pointer for ``pharmgkb`` to that id via
       :func:`flip_to_new_version`. The pointer flip is the
       supersession event; the prior set stays in
       ``pharmgkb_annotations`` indefinitely under its older
       ``source_version_id``. The supersession transaction is closed
       via :func:`commit_and_checkpoint` so the COMMIT + explicit
       CHECKPOINT phases are observable in the structlog stream
       (finding-009 #9 and #11).
    6. Return a :class:`RefreshResult` describing what landed.
    """
    log = logger.bind(source=SOURCE_DB)

    # 1. Download (skip-if-exists; ``force`` re-downloads). The scaffold's
    # ``download_to_cache`` handles the 303 redirect from PharmGKB's
    # canonical URL to its S3-hosted bucket via its injected
    # ``httpx.Client(follow_redirects=True)`` — the loader does not need
    # to know about it.
    download_result = download_to_cache(
        SOURCE_DB,
        CLINICAL_ANN_ZIP_URL,
        "clinicalAnnotations.zip",
        resource_id="clinical_annotations",
        force=force,
    )
    log.info("pharmgkb.download.audited", sha256=download_result.sha256[:12])

    # 2. Resolve version label.
    version = _resolve_version(download_result.path)
    log.info("pharmgkb.version.resolved", version=version)

    # 3. Idempotence check.
    with duckdb_connection() as conn:
        current = get_current_version(conn, SOURCE_DB)
        if current is not None and current.version == version and not force:
            log.info("pharmgkb.skip_already_current", version=version)
            return RefreshResult(
                source_db=SOURCE_DB,
                source_version_id=current.source_version_id,
                version=version,
                record_count=current.record_count or 0,
                was_already_current=True,
            )

    # 3a. --skip-if-same-version short-circuit (finding-009 #14). Off by
    # default; when on, an active matching (version, hash) row makes the
    # refresh a no-op even under --force.
    skip = maybe_skip_same_version(
        source_db=SOURCE_DB,
        version=version,
        source_file_hash=download_result.sha256,
        skip_if_same_version=skip_if_same_version,
    )
    if skip is not None:
        return skip

    # 4. Extract and parse.
    with zipfile.ZipFile(download_result.path) as zf, zf.open(_CLINICAL_ANN_TSV_NAME) as fh:
        tsv_text = io.TextIOWrapper(fh, encoding="utf-8").read()
    rows = list(_parse_clinical_annotations_tsv(tsv_text))
    log.info("pharmgkb.parse.complete", row_count=len(rows))

    # 5. Single-transaction load.
    #
    # ``upsert_source_version`` manages its own transaction internally
    # (DuckDB's FK+index quirk forces the index drop/recreate to bracket
    # an inner BEGIN/COMMIT pair; see the function's module docstring).
    # DuckDB does not support nested transactions, so we cannot wrap
    # ``upsert_source_version`` in an outer ``conn.begin()`` — doing so
    # raises ``TransactionContext Error: Current transaction is aborted``
    # from the inner BEGIN. The locked scaffold forbids modifying the
    # helper, so the workable shape is:
    #
    #  5a. ``upsert_source_version`` runs (its own transaction).
    #  5b. A separate transaction wraps ``_bulk_insert`` +
    #      ``flip_to_new_version`` — the two writes that the
    #      supersession invariant requires to land atomically (the
    #      insertion of the new active set and the pointer flip that
    #      makes it "current"). The flip runs *after* the insert so
    #      ``flip_to_new_version`` can count the just-inserted rows for
    #      the event payload.
    #
    # If 5b fails, the version row from 5a is orphaned: it claims
    # ``record_count > 0`` but no annotation rows exist for it. The
    # except block below deletes the orphan so a subsequent refresh
    # starts clean. The cleanup is best-effort — if it also fails, the
    # original exception is re-raised verbatim and the orphan is left
    # for manual cleanup. There are no child rows referencing the
    # orphan source_version_id at this point, so the DELETE is FK-safe.
    retrieval_date = datetime.now(UTC)
    flip: VersionFlipResult | None = None
    with duckdb_connection() as conn:
        source_version_id = upsert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=version,
            source_url=CLINICAL_ANN_ZIP_URL,
            source_file_hash=download_result.sha256,
            source_file_size=download_result.size_bytes,
            record_count=len(rows),
        )
        conn.begin()
        try:
            inserted = _bulk_insert(
                conn,
                rows,
                source_version_id=source_version_id,
                retrieval_date=retrieval_date,
            )
            flip = flip_to_new_version(
                conn,
                source=SOURCE_DB,
                table=_TARGET_TABLE,
                new_source_version_id=source_version_id,
            )
            commit_and_checkpoint(conn, source_name=SOURCE_DB)
        except Exception:
            conn.rollback()
            _cleanup_orphan_version_row(conn, source_version_id)
            raise

    assert flip is not None  # noqa: S101 — guaranteed by the try block returning normally
    log.info(
        "pharmgkb.refresh.complete",
        version=version,
        inserted=inserted,
        prior_version_id=flip.prior_version_id,
        prior_row_count=flip.prior_row_count,
        source_version_id=source_version_id,
    )

    return RefreshResult(
        source_db=SOURCE_DB,
        source_version_id=source_version_id,
        version=version,
        record_count=inserted,
        was_already_current=False,
    )


# Register at module-import time. The loaders subpackage __init__.py
# imports this module so the registration happens before any CLI
# dispatch runs.
register_loader(SOURCE_DB, refresh)


__all__ = [
    "CLINICAL_ANN_ZIP_URL",
    "SOURCE_DB",
    "URL_VERIFIED_DATE",
    "refresh",
]
