"""ClinVar clinical-significance annotations loader.

Downloads ClinVar's ``variant_summary.txt.gz`` (the canonical tab-delimited
per-variant release, ~3M rows), parses it line-by-line with a streaming
reader, and chunk-loads into ``clinvar_annotations`` via PyArrow Table
registration + ``INSERT ... SELECT`` (the project's locked bulk-load
convention).

Sub-phase 5.2 — third loader after PharmGKB (5.1a) and CPIC (5.1b);
mirrors the locked 5.1 template in shape:

* Module-level URL constants with a sibling ``URL_VERIFIED_DATE`` so a
  future reader can tell at a glance how stale the link is.
* A ``_resolve_version_via_head`` step that reads the upstream's
  ``Last-Modified`` HTTP header so the version label is keyed to the
  ClinVar release (idempotent across re-runs against the same release).
* A ``refresh(force)`` function that resolves version, downloads
  (skip-if-exists), short-circuits when ``annotation_source_versions``
  already names the resolved version, and otherwise upserts +
  deactivates + chunk-bulk-inserts inside one DuckDB transaction.
* ``register_loader(SOURCE_DB, refresh)`` at module-import time so the
  CLI registry is populated by importing this module.

What sets ClinVar apart from PharmGKB / CPIC in 5.1:

* **Three orders of magnitude bigger.** PharmGKB ships ~7K rows and
  CPIC ~3.5K; ClinVar is ~3M rows in a single 400+ MB gzipped TSV.
  Holding every row in memory before insert is not viable, so the
  parser is a generator and the bulk insert is chunked at
  :data:`_CHUNK_SIZE` rows per chunk.
* **All chunks land inside one transaction.** The supersession-over-
  update invariant requires that the deactivation of prior active rows
  and the insertion of every new row land or roll back together. We
  never commit between chunks; a mid-stream failure rolls the entire
  load back via ``conn.rollback()``.
* **Two assembly rows per variant.** ClinVar publishes one row per
  ``(VariationID, Assembly)`` pair, so a variant carrying both GRCh37
  and GRCh38 positions appears twice. We persist every row (the load
  contract is "no clinical-significance / variant-type filtering"), but
  only populate the GRCh38-specific columns (``pos_grch38``,
  ``ref_allele``, ``alt_allele``) for ``Assembly == 'GRCh38'`` rows --
  the schema's ``pos_grch38`` column name is constraining and storing
  GRCh37 coordinates under it would mislead position-based joins.

Architectural choice: ``variant_summary.txt`` vs ``ClinVarVariationRelease.xml``.

The TSV is the canonical per-variant release: it carries every column
the schema needs (clinical_significance / review_status / phenotype IDs
and names / RCV accession / submission counts), updates weekly on
Mondays, and parses ~10x faster than the XML alternative. The XML
release is the authoritative source for per-submitter SCV detail; if a
future sub-phase needs that granularity, it lives in a separate
evidence table, not in ``clinvar_annotations``. Out of scope here.
"""

from __future__ import annotations

import csv
import email.utils
import gzip
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final

import httpx
import pyarrow as pa
import structlog

from genome.annotate.downloads import download_to_cache
from genome.annotate.registry import RefreshResult, register_loader
from genome.annotate.source_versions import (
    get_current_version,
    upsert_source_version,
)
from genome.annotate.supersession import deactivate_prior_versions
from genome.db.duckdb_conn import duckdb_connection
from genome.ingest.models import normalize_chrom
from genome.privacy.external_client import (
    _DEFAULT_TIMEOUT_S,
    ExternalCallError,
    ExternalCallsDisabledError,
    ExternalClient,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from typing import TextIO

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Upstream URL (verified 2026-05-15).
#
# ``variant_summary.txt.gz`` is the canonical tab-delimited per-variant
# ClinVar release, hosted on NCBI's FTP server and updated weekly
# (Monday cadence). Carries every column ``clinvar_annotations`` needs
# in one file, including both GRCh37 and GRCh38 assembly rows for each
# variant. Distribution endpoint is plain HTTP(S); the scaffold's
# ``download_to_cache`` injects an ``httpx.Client(follow_redirects=True)``
# so any future redirect lands transparently.
# ---------------------------------------------------------------------------

URL_VERIFIED_DATE: Final[str] = "2026-05-15"
VARIANT_SUMMARY_URL: Final[str] = (
    "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz"
)

SOURCE_DB: Final[str] = "clinvar"
_TARGET_TABLE: Final[str] = "clinvar_annotations"
_CACHE_FILENAME: Final[str] = "variant_summary.txt.gz"
_HEAD_RESOURCE_ID: Final[str] = "clinvar_release_metadata"
_DOWNLOAD_RESOURCE_ID: Final[str] = "clinvar_variant_summary"

# Chunk size for the streaming bulk insert. ClinVar ships ~3M rows; at
# 250K rows per chunk the working set stays ~125 MB (Python list of
# parsed rows + the corresponding PyArrow Table) -- comfortable on a
# laptop and large enough to amortize the per-INSERT overhead. Tuned
# once here so a future calibration only edits one constant.
_CHUNK_SIZE: Final[int] = 250_000

# ClinVar's "RS# (dbSNP)" column encodes a missing rsID as the literal
# string "-1" (an integer sentinel, not the empty string). The dash
# variant ("-") shows up in some other columns too; treat both as None.
_MISSING_RSID_TOKENS: Final[frozenset[str]] = frozenset({"-1", "-", ""})

# Columns that ClinVar fills with "-" or "na" instead of an empty
# string when the value is absent. Coerced to None at parse time.
_MISSING_VALUE_TOKENS: Final[frozenset[str]] = frozenset({"-", "na", ""})

# Strict: ``review_status -> star_rating`` mapping per ClinVar's
# documentation (https://www.ncbi.nlm.nih.gov/clinvar/docs/review_status/).
# Stored as a 0-4 SMALLINT in the schema. Unknown / unmapped review
# statuses fall through to ``None`` so a future ClinVar wording change
# is loud (NULL star_rating) rather than silently mis-mapping.
_REVIEW_STATUS_TO_STAR: Final[dict[str, int]] = {
    "practice guideline": 4,
    "reviewed by expert panel": 3,
    "criteria provided, multiple submitters, no conflicts": 2,
    "criteria provided, single submitter": 1,
    "criteria provided, conflicting classifications": 1,
    "criteria provided, conflicting interpretations": 1,
    "no assertion criteria provided": 0,
    "no assertion provided": 0,
    "no classification provided": 0,
    "no classifications from unflagged records": 0,
    "no assertion for the individual variant": 0,
    "no classification for the individual variant": 0,
}

# Trailing ``(p.<hgvs>)`` block in ClinVar's ``Name`` column. Matched
# at end-of-string with optional whitespace so we can split the column
# into hgvs_c (everything before) and hgvs_p (the ``p.…`` body).
_HGVS_PROTEIN_RE: Final[re.Pattern[str]] = re.compile(r"\s*\((p\.[^)]+)\)\s*$")

# Mapping from the ClinVar TSV header to ``_ParsedRow`` field names.
# Captured at module scope so the mapping is reviewable at a glance and
# the parser's header-name lookup stays declarative. The header line
# starts with ``#AlleleID`` (NCBI convention); ``csv.DictReader`` keeps
# the leading ``#`` as part of the field name.
_REQUIRED_HEADERS: Final[tuple[str, ...]] = (
    "ClinicalSignificance",
    "LastEvaluated",
    "RS# (dbSNP)",
    "PhenotypeIDS",
    "PhenotypeList",
    "Assembly",
    "Chromosome",
    "ReviewStatus",
    "NumberSubmitters",
    "OtherIDs",
    "SubmitterCategories",
    "VariationID",
    "PositionVCF",
    "ReferenceAlleleVCF",
    "AlternateAlleleVCF",
    "Name",
)

# Arrow schema used by ``_insert_chunk``. Column order matches the
# INSERT column list constructed below; keeping the schema at module
# scope means the structure is reviewable next to the SQL it feeds.
_ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("clinvar_id", pa.int64(), nullable=False),
        pa.field("variation_id", pa.string()),
        pa.field("rsid", pa.string()),
        pa.field("chrom", pa.string()),
        pa.field("pos_grch38", pa.int64()),
        pa.field("ref_allele", pa.string()),
        pa.field("alt_allele", pa.string()),
        pa.field("clinical_significance", pa.string()),
        pa.field("review_status", pa.string()),
        pa.field("star_rating", pa.int16()),
        pa.field("last_evaluated", pa.date32()),
        pa.field("conditions", pa.list_(pa.string())),
        pa.field("condition_ids", pa.list_(pa.string())),
        pa.field("submission_count", pa.int32()),
        pa.field("submitter_categories", pa.list_(pa.string())),
        pa.field("hgvs_c", pa.string()),
        pa.field("hgvs_p", pa.string()),
        pa.field("inheritance", pa.string()),
        pa.field("source_version_id", pa.int64(), nullable=False),
        pa.field("retrieval_date", pa.timestamp("us"), nullable=False),
        pa.field("is_active", pa.bool_(), nullable=False),
        pa.field("superseded_by", pa.int64()),
    ],
)


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    """One row destined for ``clinvar_annotations``.

    Mirrors the destination schema's variable columns. ``clinvar_id``,
    ``source_version_id``, ``retrieval_date``, ``is_active``, and
    ``superseded_by`` are assigned at bulk-insert time after parsing
    completes.
    """

    variation_id: str | None
    rsid: str | None
    chrom: str | None
    pos_grch38: int | None
    ref_allele: str | None
    alt_allele: str | None
    clinical_significance: str | None
    review_status: str | None
    star_rating: int | None
    last_evaluated: date | None
    conditions: list[str] | None
    condition_ids: list[str] | None
    submission_count: int | None
    submitter_categories: list[str] | None
    hgvs_c: str | None
    hgvs_p: str | None
    inheritance: str | None


# ---------------------------------------------------------------------------
# Field-level coercions.
# ---------------------------------------------------------------------------


def _empty_to_none(value: str) -> str | None:
    """Return ``None`` for empty / dash / ``na`` / whitespace-only values."""
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    return trimmed


def _clean_rsid(value: str) -> str | None:
    """Coerce ClinVar's ``RS# (dbSNP)`` column.

    ClinVar encodes a missing rsID as the literal string ``"-1"`` (an
    integer sentinel from the dbSNP era) and not as empty, so the loader
    has to special-case the value alongside the standard empty / dash
    coercion. Non-missing values are bare digit strings; we prefix them
    with ``"rs"`` to match the project-wide rsID format used by
    ``variants_master``, ``pharmgkb_annotations``, and the dbSNP loader
    that lands in 5.4.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_RSID_TOKENS:
        return None
    if not trimmed.isdigit():
        # Defensive: ClinVar shouldn't ship non-digit rsIDs, but if a
        # future release does, drop the value rather than mangling it.
        return None
    return f"rs{trimmed}"


def _parse_int(value: str) -> int | None:
    """Coerce a TSV cell to an integer; empty / dash / non-numeric → None."""
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    try:
        return int(trimmed)
    except ValueError:
        return None


def _parse_clinvar_date(value: str) -> date | None:
    """Parse ClinVar's ``Mon DD, YYYY`` last-evaluated date.

    Examples in the wild: ``"Dec 17, 2024"``, ``"Jul 3, 2023"``. Empty
    or dash values map to ``None``. Unparseable values also map to
    ``None`` (logged at debug scope; not loud) so a single malformed
    row doesn't poison a multi-million-row load.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    try:
        return datetime.strptime(trimmed, "%b %d, %Y").replace(tzinfo=UTC).date()
    except ValueError:
        return None


def _parse_phenotype_list(value: str) -> list[str] | None:
    """Split ``PhenotypeList`` into a list.

    ClinVar joins phenotype names with the single pipe ``|``. Empty /
    dash values map to ``None`` (not ``[]``) so a downstream
    ``conditions IS NULL`` filter behaves intuitively.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    items = [p.strip() for p in trimmed.split("|") if p.strip()]
    return items or None


def _parse_phenotype_ids(value: str) -> list[str] | None:
    """Flatten ``PhenotypeIDS`` into one list of ID strings.

    ClinVar's encoding is two-level: ``||`` separates phenotypes,
    and within one phenotype's group, ``,`` separates the individual
    IDs. We flatten the two levels into one list because the schema's
    ``condition_ids VARCHAR[]`` is a flat array and consumers querying
    "is OMIM:613647 in condition_ids?" don't care which phenotype the
    ID belonged to. Empty / dash values map to ``None``.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    ids: list[str] = []
    for group in trimmed.split("||"):
        for item in group.split(","):
            cleaned = item.strip()
            if cleaned:
                ids.append(cleaned)
    return ids or None


def _parse_submitter_categories(value: str) -> list[str] | None:
    """Wrap ClinVar's ``SubmitterCategories`` integer in a single-element list.

    The source value is a single integer (1-4 in observed releases)
    that encodes which submitter classes contributed to the variant
    (see ClinVar docs: 1 = literature only, 2 = at least one clinical
    lab, 3 = at least one expert panel / practice guideline, 4 =
    practice guideline). The destination column is a ``VARCHAR[]`` with
    a comment naming label-form values like ``'expert_panel'`` /
    ``'clinical_lab'`` / ``'lit_only'`` -- but the source data is
    integer-encoded. We preserve the integer code as a single-element
    list rather than guessing at label mapping; consumers can map to
    canonical labels via a versioned function once the label set is
    formally agreed.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    return [trimmed]


def _extract_hgvs_c_p(name: str) -> tuple[str | None, str | None]:
    """Split ClinVar's ``Name`` column into ``(hgvs_c, hgvs_p)``.

    The ``Name`` column carries the full HGVS expression for the
    variant, optionally followed by a parenthesized protein expression
    (e.g. ``NM_014855.3(AP5Z1):c.80_83delinsTGCT…
    (p.Arg27_Ile28delinsLeuLeuTer)``). We split on the trailing
    ``(p.…)`` block: everything before goes into ``hgvs_c``, the
    ``p.…`` body itself goes into ``hgvs_p``. When no protein block is
    present, ``hgvs_p`` is ``None`` and ``hgvs_c`` is the full ``Name``
    value.

    Empty / dash names map to ``(None, None)``.
    """
    trimmed = name.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None, None
    match = _HGVS_PROTEIN_RE.search(trimmed)
    if match is None:
        return trimmed, None
    return trimmed[: match.start()].strip() or None, match.group(1)


def _review_status_to_star(review_status: str | None) -> int | None:
    """Map ClinVar's text review-status to the schema's 0-4 ``star_rating``.

    Lookup is case-insensitive against :data:`_REVIEW_STATUS_TO_STAR`;
    unknown statuses return ``None``. The intent is loud rather than
    quiet -- if ClinVar ships a new review-status label and we haven't
    mapped it yet, the column reads NULL in queries (and is visible in
    the post-load ``review_status_distribution`` summary) instead of
    getting a wrong star count.
    """
    if review_status is None:
        return None
    return _REVIEW_STATUS_TO_STAR.get(review_status.strip().lower())


# ---------------------------------------------------------------------------
# Streaming parser.
# ---------------------------------------------------------------------------


def _row_to_parsed(raw: dict[str, str]) -> _ParsedRow:
    """Map one ``csv.DictReader`` row to a ``_ParsedRow``.

    Per-row coercions happen inline so the function stays readable next
    to the column-mapping it implements. The GRCh37/GRCh38 split:
    every row carries assembly-independent identifiers (variation_id,
    rsid, chrom, the clinical-interpretation columns, phenotype lists,
    HGVS), but ``pos_grch38`` / ``ref_allele`` / ``alt_allele`` are only
    populated for ``Assembly == 'GRCh38'`` rows -- the schema's
    ``pos_grch38`` column name is constraining and storing GRCh37
    coordinates under it would mislead position-based joins. GRCh37
    rows still land in the table (with the position-specific columns
    NULL) so the row count and distinct VariationID drift identifiers
    stay stable.
    """
    assembly = (raw.get("Assembly") or "").strip()
    is_grch38 = assembly == "GRCh38"

    review_status = _empty_to_none(raw.get("ReviewStatus", ""))
    hgvs_c, hgvs_p = _extract_hgvs_c_p(raw.get("Name", ""))

    return _ParsedRow(
        variation_id=_empty_to_none(raw.get("VariationID", "")),
        rsid=_clean_rsid(raw.get("RS# (dbSNP)", "")),
        chrom=normalize_chrom(raw.get("Chromosome", "")),
        pos_grch38=_parse_int(raw.get("PositionVCF", "")) if is_grch38 else None,
        ref_allele=_empty_to_none(raw.get("ReferenceAlleleVCF", "")) if is_grch38 else None,
        alt_allele=_empty_to_none(raw.get("AlternateAlleleVCF", "")) if is_grch38 else None,
        clinical_significance=_empty_to_none(raw.get("ClinicalSignificance", "")),
        review_status=review_status,
        star_rating=_review_status_to_star(review_status),
        last_evaluated=_parse_clinvar_date(raw.get("LastEvaluated", "")),
        conditions=_parse_phenotype_list(raw.get("PhenotypeList", "")),
        condition_ids=_parse_phenotype_ids(raw.get("PhenotypeIDS", "")),
        submission_count=_parse_int(raw.get("NumberSubmitters", "")),
        submitter_categories=_parse_submitter_categories(raw.get("SubmitterCategories", "")),
        hgvs_c=hgvs_c,
        hgvs_p=hgvs_p,
        inheritance=None,
    )


def _parse_variant_summary(text_io: TextIO) -> Iterator[_ParsedRow]:
    """Stream rows from ``variant_summary.txt``.

    Yields one ``_ParsedRow`` per TSV row. No filtering at parse time
    -- the load contract is "no clinical-significance filter, no
    variant-type filter; schema-level filtering is a query concern, not
    a load concern". Parser-level coercions (empty → None, ``-1`` →
    None on rsID, list-field splits) happen in :func:`_row_to_parsed`.

    Raises :class:`ValueError` if any column in
    :data:`_REQUIRED_HEADERS` is missing -- the upstream contract has
    shifted and the loader can't produce a correct mapping; loud-fail
    is preferable to a silent column drop.
    """
    reader = csv.DictReader(text_io, delimiter="\t")
    if reader.fieldnames is None:
        msg = "ClinVar variant_summary.txt has no header row"
        raise ValueError(msg)
    missing = [h for h in _REQUIRED_HEADERS if h not in reader.fieldnames]
    if missing:
        msg = (
            f"ClinVar variant_summary.txt is missing expected columns "
            f"{missing!r}; got {list(reader.fieldnames)!r}"
        )
        raise ValueError(msg)
    for raw in reader:
        yield _row_to_parsed(raw)


# ---------------------------------------------------------------------------
# Version resolution via HEAD.
# ---------------------------------------------------------------------------


def _resolve_version_via_head() -> str:
    """Resolve the ClinVar version label from the upstream ``Last-Modified``.

    Issues a HEAD request to :data:`VARIANT_SUMMARY_URL` via the
    audited :class:`ExternalClient`. The HTTP ``Last-Modified`` header
    (RFC 822 form) is parsed via :func:`email.utils.parsedate_to_datetime`
    and rendered as ``YYYY_MM_DD`` to match the schema's
    ``annotation_source_versions.version`` shape.

    Failure modes:

    * :class:`ExternalCallsDisabledError` propagates -- callers see the
      audited refusal directly. The privacy gate is fail-closed; we do
      not paper over it with a fallback.
    * Any other :class:`ExternalCallError` (network, HTTP 4xx/5xx,
      malformed Last-Modified) → fall back to today's UTC date in the
      same format. Logged at INFO so the fallback is visible in the
      structlog output.

    The HEAD request is the loader's first audited call; placing it
    before the download means a fresh refresh against an unchanged
    release short-circuits before re-downloading the 400+ MB body.
    """
    try:
        with (
            httpx.Client(
                follow_redirects=True,
                timeout=_DEFAULT_TIMEOUT_S,
            ) as http_client,
            ExternalClient(
                f"annotations_{SOURCE_DB}",
                client=http_client,
            ) as client,
        ):
            response = client.request(
                "HEAD",
                VARIANT_SUMMARY_URL,
                resource_type="annotation_source",
                resource_id=_HEAD_RESOURCE_ID,
            )
        last_modified = response.headers.get("Last-Modified")
    except ExternalCallsDisabledError:
        # Privacy gate is fail-closed; surface immediately.
        raise
    except ExternalCallError as exc:
        logger.warning("clinvar.version.head_failed", error=str(exc))
        return _today_label()

    if last_modified:
        try:
            parsed = email.utils.parsedate_to_datetime(last_modified)
        except (TypeError, ValueError) as exc:
            logger.info(
                "clinvar.version.last_modified_unparseable",
                last_modified=last_modified,
                error=str(exc),
            )
            return _today_label()
        return parsed.astimezone(UTC).strftime("%Y_%m_%d")

    logger.info("clinvar.version.last_modified_missing")
    return _today_label()


def _today_label() -> str:
    """Today's UTC date as ``YYYY_MM_DD`` -- the version-resolution fallback."""
    return datetime.now(UTC).strftime("%Y_%m_%d")


# ---------------------------------------------------------------------------
# Chunked bulk insert.
# ---------------------------------------------------------------------------


def _next_clinvar_id(conn: DuckDBPyConnection) -> int:
    """``COALESCE(MAX(clinvar_id), 0) + 1``.

    Mirrors :func:`genome.annotate.loaders.pharmgkb._next_pharmgkb_id`
    and the wider project pattern of app-allocated BIGINT primary keys
    via ``MAX + 1``. Called once at the start of streaming; per-chunk
    base IDs are advanced from the previous chunk's actual size.
    """
    row = conn.execute(
        f"SELECT COALESCE(MAX(clinvar_id), 0) FROM {_TARGET_TABLE}",  # noqa: S608
    ).fetchone()
    return int(row[0]) + 1 if row is not None else 1


def _iter_chunks(
    rows_iter: Iterable[_ParsedRow],
    chunk_size: int,
) -> Iterator[list[_ParsedRow]]:
    """Yield ``chunk_size``-sized slices from ``rows_iter``.

    The last chunk may be smaller than ``chunk_size`` if the iterator
    doesn't divide evenly. Chunks are returned as fresh lists so the
    caller can mutate or drop them without affecting subsequent
    iterations. Pulled out of :func:`_stream_bulk_insert` so the
    chunking logic is unit-testable without a DuckDB connection in
    hand.
    """
    chunk: list[_ParsedRow] = []
    for row in rows_iter:
        chunk.append(row)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _insert_chunk(
    conn: DuckDBPyConnection,
    rows: list[_ParsedRow],
    *,
    base_id: int,
    source_version_id: int,
    retrieval_date: datetime,
) -> int:
    """Insert one chunk of ``_ParsedRow`` into ``clinvar_annotations``.

    Builds a PyArrow Table with one column per destination column
    (including ``clinvar_id``, ``source_version_id``, ``retrieval_date``,
    ``is_active=True``, and ``superseded_by=NULL``), registers it under
    a temp name, then issues
    ``INSERT INTO clinvar_annotations (...) SELECT ... FROM <temp>``
    and unregisters. ``chrom`` is cast through ``chromosome_enum`` in
    the SELECT so the NULLs that are correct for non-canonical or
    missing chromosomes reach the enum-typed column cleanly.

    Returns the number of rows inserted (== ``len(rows)``). A zero-row
    call inserts nothing and returns 0.
    """
    if not rows:
        return 0

    n = len(rows)
    # Naive UTC datetime: pa.timestamp("us") (no tz) lines up with
    # DuckDB's TIMESTAMP (no tz). Same convention as the PharmGKB and
    # CPIC loaders and the imputation runs writer.
    naive_retrieval = retrieval_date.astimezone(UTC).replace(tzinfo=None)
    table = pa.table(
        {
            "clinvar_id": pa.array(range(base_id, base_id + n), type=pa.int64()),
            "variation_id": pa.array([r.variation_id for r in rows], type=pa.string()),
            "rsid": pa.array([r.rsid for r in rows], type=pa.string()),
            "chrom": pa.array([r.chrom for r in rows], type=pa.string()),
            "pos_grch38": pa.array([r.pos_grch38 for r in rows], type=pa.int64()),
            "ref_allele": pa.array([r.ref_allele for r in rows], type=pa.string()),
            "alt_allele": pa.array([r.alt_allele for r in rows], type=pa.string()),
            "clinical_significance": pa.array(
                [r.clinical_significance for r in rows],
                type=pa.string(),
            ),
            "review_status": pa.array([r.review_status for r in rows], type=pa.string()),
            "star_rating": pa.array([r.star_rating for r in rows], type=pa.int16()),
            "last_evaluated": pa.array([r.last_evaluated for r in rows], type=pa.date32()),
            "conditions": pa.array([r.conditions for r in rows], type=pa.list_(pa.string())),
            "condition_ids": pa.array(
                [r.condition_ids for r in rows],
                type=pa.list_(pa.string()),
            ),
            "submission_count": pa.array(
                [r.submission_count for r in rows],
                type=pa.int32(),
            ),
            "submitter_categories": pa.array(
                [r.submitter_categories for r in rows],
                type=pa.list_(pa.string()),
            ),
            "hgvs_c": pa.array([r.hgvs_c for r in rows], type=pa.string()),
            "hgvs_p": pa.array([r.hgvs_p for r in rows], type=pa.string()),
            "inheritance": pa.array([r.inheritance for r in rows], type=pa.string()),
            "source_version_id": pa.array([source_version_id] * n, type=pa.int64()),
            "retrieval_date": pa.array([naive_retrieval] * n, type=pa.timestamp("us")),
            "is_active": pa.array([True] * n, type=pa.bool_()),
            "superseded_by": pa.array([None] * n, type=pa.int64()),
        },
        schema=_ARROW_SCHEMA,
    )
    try:
        conn.register("_clinvar_stage_arrow", table)
        conn.execute(
            f"""
            INSERT INTO {_TARGET_TABLE} (
                clinvar_id, variation_id, rsid,
                chrom, pos_grch38, ref_allele, alt_allele,
                clinical_significance, review_status, star_rating, last_evaluated,
                conditions, condition_ids,
                submission_count, submitter_categories,
                hgvs_c, hgvs_p, inheritance,
                source_version_id, retrieval_date, is_active, superseded_by
            )
            SELECT
                clinvar_id, variation_id, rsid,
                chrom::chromosome_enum, pos_grch38, ref_allele, alt_allele,
                clinical_significance, review_status, star_rating, last_evaluated,
                conditions, condition_ids,
                submission_count, submitter_categories,
                hgvs_c, hgvs_p, inheritance,
                source_version_id, retrieval_date, is_active, superseded_by
              FROM _clinvar_stage_arrow
            """,  # noqa: S608 — table name is a module constant, not user input
        )
    finally:
        conn.unregister("_clinvar_stage_arrow")
    return n


def _stream_bulk_insert(
    conn: DuckDBPyConnection,
    rows_iter: Iterable[_ParsedRow],
    *,
    source_version_id: int,
    retrieval_date: datetime,
    chunk_size: int = _CHUNK_SIZE,
) -> int:
    """Drain ``rows_iter`` into ``clinvar_annotations`` in chunks.

    Each chunk is a separate :func:`_insert_chunk` call (PyArrow Table
    registration + ``INSERT ... SELECT``). All chunks must run inside
    the same DuckDB transaction -- the caller bracket-controls
    ``conn.begin()`` / ``conn.commit()``. Chunks are deliberately *not*
    committed individually: a mid-stream failure must roll back the
    deactivation of prior active rows along with the partial insert,
    or the supersession-over-update invariant is broken.

    Per-chunk progress is logged at INFO with the chunk index, the
    row count, and the cumulative total. Returns the total number of
    rows inserted.
    """
    base_id = _next_clinvar_id(conn)
    next_id = base_id
    total = 0
    for chunk_index, chunk in enumerate(_iter_chunks(rows_iter, chunk_size), start=1):
        inserted = _insert_chunk(
            conn,
            chunk,
            base_id=next_id,
            source_version_id=source_version_id,
            retrieval_date=retrieval_date,
        )
        total += inserted
        next_id += inserted
        logger.info(
            "clinvar.bulk_insert.chunk",
            chunk_index=chunk_index,
            rows=inserted,
            cumulative=total,
        )
    return total


# ---------------------------------------------------------------------------
# Supersession + rollback helpers (mirror PharmGKB / CPIC).
# ---------------------------------------------------------------------------


def _deactivate_for_refresh(
    conn: DuckDBPyConnection,
    *,
    source_version_id: int,
    force: bool,
) -> int:
    """Deactivate prior ClinVar rows ahead of a refresh insert.

    Mirrors :func:`genome.annotate.loaders.pharmgkb._deactivate_for_refresh`
    and :func:`genome.annotate.loaders.cpic._deactivate_for_refresh`:
    on the normal (non-force) path defer to the schema's standard
    supersession helper; on the force path blanket-deactivate every
    active ClinVar row so a re-run against the same version label
    doesn't leave duplicate active rows.

    ``clinvar_annotations`` carries both ``is_active`` *and*
    ``superseded_by`` (the only Phase-5 source loader so far that
    populates the ``superseded_by`` chain), so we pass
    ``has_superseded_by=True`` to the standard helper -- deactivated
    rows get tagged with the new ``source_version_id`` so the
    supersession history is followable.

    Returns the number of rows flipped to ``is_active=FALSE``.
    """
    if not force:
        return deactivate_prior_versions(
            conn,
            table=_TARGET_TABLE,
            new_source_version_id=source_version_id,
            has_superseded_by=True,
        )
    # _TARGET_TABLE is a module constant, not user input — S608 is a
    # false positive here.
    res = conn.execute(
        f"UPDATE {_TARGET_TABLE} "  # noqa: S608
        "SET is_active = FALSE, superseded_by = ? "
        "WHERE is_active = TRUE",
        [source_version_id],
    )
    row = res.fetchone() if hasattr(res, "fetchone") else None
    return int(row[0]) if row is not None and row[0] is not None else 0


def _cleanup_orphan_version_row(
    conn: DuckDBPyConnection,
    source_version_id: int,
) -> None:
    """Best-effort delete of an orphan ``annotation_source_versions`` row.

    Same shape as the PharmGKB / CPIC helpers -- called when the
    supersede + chunked-insert transaction rolls back so the version
    row that :func:`upsert_source_version` committed in its own
    transaction doesn't leave a dangling "version exists but zero rows
    referenced" state. The DELETE is FK-safe because no
    ``clinvar_annotations`` rows reference the new ``source_version_id``
    yet (the stream insert never committed). Failures are swallowed
    and logged; the caller is already raising the original exception.
    """
    try:
        conn.execute(
            "DELETE FROM annotation_source_versions WHERE source_version_id = ?",
            [source_version_id],
        )
    except Exception:  # noqa: BLE001 — best-effort cleanup; original exc re-raised by caller
        logger.warning(
            "clinvar.cleanup.orphan_version_row_delete_failed",
            source_version_id=source_version_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Post-load summary (drift identifiers).
# ---------------------------------------------------------------------------


def _summarize_active(conn: DuckDBPyConnection) -> dict[str, object]:
    """Compute the drift identifiers logged at end-of-load.

    Returns the same five quantities the runbook documents as locked
    drift signals for ClinVar: total active row count, distinct
    variation_id count, distinct non-NULL rsid count, the
    clinical_significance distribution, and the review_status
    distribution. Run after the supersession transaction commits so
    only ``is_active = TRUE`` rows count.
    """
    total_row = conn.execute(
        f"SELECT COUNT(*) FROM {_TARGET_TABLE} WHERE is_active",  # noqa: S608
    ).fetchone()
    distinct_variation_row = conn.execute(
        f"SELECT COUNT(DISTINCT variation_id) FROM {_TARGET_TABLE} WHERE is_active",  # noqa: S608
    ).fetchone()
    distinct_rsid_row = conn.execute(
        f"SELECT COUNT(DISTINCT rsid) FROM {_TARGET_TABLE} "  # noqa: S608
        "WHERE is_active AND rsid IS NOT NULL",
    ).fetchone()
    significance_rows = conn.execute(
        f"SELECT clinical_significance, COUNT(*) FROM {_TARGET_TABLE} "  # noqa: S608
        "WHERE is_active GROUP BY 1 ORDER BY 2 DESC",
    ).fetchall()
    review_rows = conn.execute(
        f"SELECT review_status, COUNT(*) FROM {_TARGET_TABLE} "  # noqa: S608
        "WHERE is_active GROUP BY 1 ORDER BY 2 DESC",
    ).fetchall()
    return {
        "active_total": int(total_row[0]) if total_row is not None else 0,
        "distinct_variation_id": int(distinct_variation_row[0])
        if distinct_variation_row is not None
        else 0,
        "distinct_rsid_non_null": int(distinct_rsid_row[0]) if distinct_rsid_row is not None else 0,
        "clinical_significance_distribution": {
            ("__null__" if k is None else str(k)): int(v) for k, v in significance_rows
        },
        "review_status_distribution": {
            ("__null__" if k is None else str(k)): int(v) for k, v in review_rows
        },
    }


# ---------------------------------------------------------------------------
# Module entry point — refresh
# ---------------------------------------------------------------------------


def refresh(force: bool) -> RefreshResult:  # noqa: FBT001 — registry RefreshFn signature
    """Refresh ClinVar clinical-significance annotations.

    Pipeline:

    1. Resolve version via HEAD against
       :data:`VARIANT_SUMMARY_URL` (audited; falls back to today's UTC
       date when the ``Last-Modified`` header is absent or
       unparseable).
    2. Short-circuit and return ``was_already_current=True`` if a row
       in ``annotation_source_versions`` already names the resolved
       ``(source_db='clinvar', version)`` and ``force`` is ``False``.
       This is what makes a re-run against an unchanged ClinVar release
       cheap: no download, no parse, no insert.
    3. Download ``variant_summary.txt.gz`` via the audited
       :func:`genome.annotate.downloads.download_to_cache`
       (skip-if-exists by default; ``force=True`` re-downloads).
    4. Inside one DuckDB transaction: upsert
       ``annotation_source_versions``, deactivate prior ClinVar rows
       via :func:`_deactivate_for_refresh`, stream-parse the gzipped
       TSV, chunk-insert at :data:`_CHUNK_SIZE` rows per chunk via
       :func:`_stream_bulk_insert`, and update the version row's
       ``record_count`` once the streaming completes.
    5. Open a fresh read-only connection and emit a structlog summary
       line with the locked drift identifiers (active row total,
       distinct variation_id, distinct non-NULL rsID, clinical
       significance distribution, review status distribution).
    6. Return a :class:`RefreshResult` describing what landed.
    """
    log = logger.bind(source=SOURCE_DB)

    # 1. Resolve version via HEAD. ExternalCallsDisabledError propagates.
    version = _resolve_version_via_head()
    log.info("clinvar.version.resolved", version=version)

    # 2. Idempotence check -- short-circuit before downloading the body.
    with duckdb_connection() as conn:
        current = get_current_version(conn, SOURCE_DB)
        if current is not None and current.version == version and not force:
            log.info("clinvar.skip_already_current", version=version)
            return RefreshResult(
                source_db=SOURCE_DB,
                source_version_id=current.source_version_id,
                version=version,
                record_count=current.record_count or 0,
                was_already_current=True,
            )

    # 3. Download (skip-if-exists; force re-downloads).
    download_result = download_to_cache(
        SOURCE_DB,
        VARIANT_SUMMARY_URL,
        _CACHE_FILENAME,
        resource_id=_DOWNLOAD_RESOURCE_ID,
        force=force,
    )
    log.info(
        "clinvar.download.audited",
        sha256=download_result.sha256[:16],
        size_bytes=download_result.size_bytes,
    )

    # 4. Single-transaction load. The PharmGKB loader's "version row in
    # its own transaction, supersede + insert in the wrapping
    # transaction" shape applies verbatim here -- DuckDB does not
    # support nested transactions and ``upsert_source_version`` manages
    # its own due to the FK + index quirk documented in that module.
    # The only difference for ClinVar is the bulk insert is *streamed*
    # in chunks, all of which sit inside the same transaction so a
    # mid-stream failure rolls every chunk back together.
    started = time.monotonic()
    retrieval_date = datetime.now(UTC)
    with duckdb_connection() as conn:
        source_version_id = upsert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=version,
            source_url=VARIANT_SUMMARY_URL,
            source_file_hash=download_result.sha256,
            source_file_size=download_result.size_bytes,
            record_count=None,
        )
        conn.begin()
        try:
            deactivated = _deactivate_for_refresh(
                conn,
                source_version_id=source_version_id,
                force=force,
            )
            with gzip.open(
                download_result.path,
                mode="rt",
                encoding="utf-8",
                newline="",
            ) as fh:
                inserted = _stream_bulk_insert(
                    conn,
                    _parse_variant_summary(fh),
                    source_version_id=source_version_id,
                    retrieval_date=retrieval_date,
                )
            # Backfill record_count now that we know the streaming total.
            # The column isn't indexed, so this UPDATE is FK-safe and
            # doesn't trigger the ``idx_asv_current`` quirk.
            conn.execute(
                "UPDATE annotation_source_versions "
                "SET record_count = ? "
                "WHERE source_version_id = ?",
                [inserted, source_version_id],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            _cleanup_orphan_version_row(conn, source_version_id)
            raise

    elapsed = time.monotonic() - started

    # 5. Post-load summary (drift identifiers). Read-only; runs against
    # the just-committed state.
    with duckdb_connection(read_only=True) as conn:
        summary = _summarize_active(conn)

    log.info(
        "clinvar.refresh.complete",
        version=version,
        sha256=download_result.sha256[:16],
        size_bytes=download_result.size_bytes,
        inserted=inserted,
        deactivated=deactivated,
        source_version_id=source_version_id,
        elapsed_seconds=round(elapsed, 1),
        active_total=summary["active_total"],
        distinct_variation_id=summary["distinct_variation_id"],
        distinct_rsid_non_null=summary["distinct_rsid_non_null"],
        clinical_significance_distribution=summary["clinical_significance_distribution"],
        review_status_distribution=summary["review_status_distribution"],
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
    "SOURCE_DB",
    "URL_VERIFIED_DATE",
    "VARIANT_SUMMARY_URL",
    "refresh",
]
