"""PGS Catalog score-level metadata loader.

Downloads PGS Catalog's metadata bundle (a single ``.tar.gz`` carrying
the per-resource CSVs), parses the four CSVs relevant to score-level
metadata, joins them client-side into one row per PGS, applies a
max-across-cohorts reduction to collapse multi-row performance metrics
into the schema's two scalar performance columns, and chunk-loads the
joined rows into ``pgs_catalog_scores`` via PyArrow Table registration
+ ``INSERT ... SELECT`` (the project's locked bulk-load convention).

Sub-phase 5.4 — fifth loader after PharmGKB (5.1a), CPIC (5.1b),
ClinVar (5.2), and GWAS Catalog (5.3). Mirrors the locked
5.1/5.2/5.3 template in shape:

* Module-level URL constants with a sibling ``URL_VERIFIED_DATE`` so a
  future reader can tell at a glance how stale the link is.
* A ``_resolve_version_via_release_latest`` step that calls the PGS
  Catalog REST release-current endpoint, parses the JSON ``"date"``
  field, and renders it as ``YYYY_MM_DD`` -- matching the ClinVar
  and GWAS Catalog loaders' version-string convention.
* A ``refresh(force)`` function that resolves version, downloads
  (skip-if-exists), short-circuits when ``annotation_source_versions``
  already names the resolved version, and otherwise upserts +
  deactivates + chunk-bulk-inserts inside one DuckDB transaction.
* ``register_loader(SOURCE_DB, refresh)`` at module-import time so the
  CLI registry is populated by importing this module.

What sets PGS Catalog apart from the prior four loaders:

* **Surrogate score_record_id primary key.** The schema uses a
  surrogate ``score_record_id BIGINT PRIMARY KEY`` rather than
  ``pgs_id`` directly so the same ``pgs_id`` can appear under
  multiple ``source_version_id`` values (one row per release) without
  violating the PK. ``pgs_score_weights.pgs_id`` is therefore
  application-validated rather than DB-FK-enforced (DuckDB requires
  the FK target to carry a ``PRIMARY KEY`` or ``UNIQUE`` constraint,
  which the surrogate-PK shape no longer satisfies for ``pgs_id``).
* **Four-CSV in-memory join + REST trait_category lookup.** The
  bundle ships four metadata CSVs relevant to score-level state:
  scores (one row per PGS), publications (one row per PGP ID),
  efo_traits (one row per EFO term -- used for the
  ``orphan_trait_refs`` counter only; the bundle does not carry
  a category column), and performance_metrics (multiple rows per
  PGS, one per evaluation cohort). The loader parses all four
  into memory (each is a few thousand rows; the bundle
  decompresses to ~10-15 MB), then joins on the natural keys to
  produce one ``_ParsedRow`` per PGS. The schema's
  ``trait_category`` column is populated from a fifth audited
  download: the ``/rest/trait_category/all`` REST endpoint,
  which returns ~11 categories keyed by EFO ID. The
  trait_category lookup is independent of the bundle's EFO
  traits CSV -- a score whose EFO is missing from the bundle
  (counted as orphan) may still pick up a category from the
  REST payload, and vice versa.
* **Performance-metric max reduction.** A single PGS typically has
  multiple performance_metrics entries -- one per evaluation cohort
  / sample set. The schema's ``performance_auc`` and
  ``performance_or_per_sd`` columns are scalars, so the loader
  collapses the per-cohort entries via ``max(non-NULL values)``.
  The "max" rule is the simplest auditable reduction at this scale,
  not the most statistically honest one -- honest per-cohort
  reporting would require a separate ``pgs_catalog_performance``
  table, which is a future schema change, not 5.4 work. The runbook
  and docstring call out the auditability trade-off explicitly.
* **Gzipped TAR bundle, not TAR-in-ZIP.** The upstream bundle is a
  single-layer ``.tar.gz``; the loader opens it with
  :func:`tarfile.open` ``mode="r:gz"`` (random-access read). The
  streaming variant (``"r|gz"``) returns file objects backed by a
  non-seekable inner stream, which breaks :class:`io.TextIOWrapper`
  -- the bundle is small (~4 MB compressed) so non-streaming open
  is acceptable. One layer of decompression rather than the two
  layers a TAR-in-ZIP would require. The helper stays inside this
  module per the GWAS Catalog precedent of not promoting
  source-specific archive shapes to ``downloads.py``.

Supersession is via the ``annotation_sources`` pointer table (PR 1's
version-pointer refactor): the loader inserts the new active set under
a fresh ``source_version_id`` and then flips the pointer for
``pgs_catalog`` to that id in one statement. Readers that want
current-version rows join through ``annotation_sources``. The prior
set stays in ``pgs_catalog_scores`` indefinitely under its older
``source_version_id``.

The loader does **not** touch ``variant_annotations_index`` (refresh
is a separate downstream concern in sub-phase 5.8), and it does
**not** load per-PGS variant weights (``pgs_score_weights`` is Phase
6 work; this PR ships score-level metadata only).
"""

from __future__ import annotations

import csv
import io
import json
import re
import tarfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final

import httpx
import pyarrow as pa
import structlog

from genome.annotate.downloads import download_to_cache
from genome.annotate.registry import RefreshResult, register_loader
from genome.annotate.source_versions import (
    get_current_version,
    insert_source_version,
)
from genome.annotate.supersession import (
    VersionFlipResult,
    commit_and_checkpoint,
    flip_to_new_version,
    maybe_skip_same_version,
)
from genome.db.duckdb_conn import duckdb_connection
from genome.privacy.external_client import (
    _DEFAULT_TIMEOUT_S,
    ExternalClient,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path
    from typing import TextIO

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Upstream URLs (verified 2026-05-17).
#
# The PGS Catalog publishes two surfaces we care about:
#
# 1. ``https://www.pgscatalog.org/rest/release/current/`` -- REST
#    endpoint returning JSON of the form
#    ``{"date": "YYYY-MM-DD", "score_count": N, ...}``. The ``date``
#    field is the release-snapshot date and is what we use as the
#    version label (``YYYY_MM_DD`` form, matching the ClinVar and
#    GWAS Catalog loaders' convention). Note the endpoint is
#    ``/release/current/`` (not ``/release/latest/`` -- the latter
#    returns HTTP 500 at the verification date).
#
# 2. ``https://ftp.ebi.ac.uk/pub/databases/spot/pgs/metadata/
#    pgs_all_metadata.tar.gz`` -- the canonical "latest" bundle.
#    A single ``.tar.gz`` archive (~4 MB compressed; ~15 MB after
#    decompress) containing the per-resource CSVs (scores,
#    publications, EFO traits, performance metrics, cohorts,
#    evaluation sample sets, score development samples, plus a
#    sibling ``.xlsx`` we ignore). The TAR is opened with
#    ``tarfile.open(..., mode="r:gz")`` (random-access read of a
#    gzipped TAR -- non-streaming, because streaming mode's
#    file objects don't support ``seekable()`` and that breaks
#    ``io.TextIOWrapper``); the bundle is small (~4 MB compressed)
#    so non-streaming open is acceptable. One layer of decompression
#    rather than the two layers a TAR-in-ZIP would require.
#
# ``download_to_cache`` injects an ``httpx.Client(follow_redirects=
# True)`` so any FTP/CDN redirect lands transparently on disk.
# ---------------------------------------------------------------------------

URL_VERIFIED_DATE: Final[str] = "2026-05-17"
PGS_RELEASE_LATEST_URL: Final[str] = "https://www.pgscatalog.org/rest/release/current/"
PGS_METADATA_BUNDLE_URL: Final[str] = (
    "https://ftp.ebi.ac.uk/pub/databases/spot/pgs/metadata/pgs_all_metadata.tar.gz"
)
# Trait-category REST endpoint. The metadata bundle's
# ``pgs_all_metadata_efo_traits.csv`` does not carry a category column,
# so the loader downloads this REST payload as a sibling JSON file and
# uses it to populate ``trait_category`` on the joined rows. Payload
# shape: ``{"count": N, "results": [{"label": "Cancer", "efotraits":
# [{"id": "EFO_xxx", ...}, ...]}, ...]}``. Pagination is bounded by
# the ~tens of categories PGS Catalog publishes; the response fits
# comfortably in one page today. If a future release grows past the
# REST endpoint's default page size, ``_parse_trait_categories``
# raises a clear error pointing at pagination as the cause.
PGS_TRAIT_CATEGORY_URL: Final[str] = "https://www.pgscatalog.org/rest/trait_category/all"

# Canonical filenames inside the bundle. The TAR member names include
# a leading ``/`` (the upstream packaging used absolute paths), so the
# helper matches members via ``endswith`` rather than exact equality.
_SCORES_MEMBER: Final[str] = "pgs_all_metadata_scores.csv"
_PUBLICATIONS_MEMBER: Final[str] = "pgs_all_metadata_publications.csv"
_EFO_TRAITS_MEMBER: Final[str] = "pgs_all_metadata_efo_traits.csv"
_PERFORMANCE_MEMBER: Final[str] = "pgs_all_metadata_performance_metrics.csv"

SOURCE_DB: Final[str] = "pgs_catalog"
_TARGET_TABLE: Final[str] = "pgs_catalog_scores"
_CACHE_FILENAME: Final[str] = "pgs_all_metadata.tar.gz"
_TRAIT_CATEGORY_CACHE_FILENAME: Final[str] = "trait_categories.json"
_RELEASE_RESOURCE_ID: Final[str] = "pgs_catalog_release_current"
_DOWNLOAD_RESOURCE_ID: Final[str] = "pgs_catalog_all_metadata"
_TRAIT_CATEGORY_RESOURCE_ID: Final[str] = "pgs_catalog_trait_categories"

# Chunk size for the bulk insert. Matches the ClinVar / GWAS Catalog
# loaders. PGS Catalog ships ~5-7K rows at the current release so a
# refresh fits in a single chunk; keeping the same constant means the
# chunked-insert code path is exercised identically across loaders and
# a future calibration touches one value in one place.
_CHUNK_SIZE: Final[int] = 250_000

# Tokens PGS Catalog uses to signal a missing cell. Empty,
# whitespace-only, ``NA``, ``NR`` ("not reported"), and the standard
# dash all coerce to ``None``.
_MISSING_VALUE_TOKENS: Final[frozenset[str]] = frozenset(
    {"", "NA", "NR", "-"},
)

# Leading-number extractor for the performance-metric cells, which
# ship as ``"<estimate> [<lower>,<upper>]"`` (e.g. ``"0.622
# [0.619,0.627]"`` for AUROC or ``"1.55 [1.52,1.58]"`` for OR). We
# only persist the point estimate -- the CI is dropped at this layer.
_LEADING_NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)",
)

# Matches the date strings used by both the release-current REST
# payload (``YYYY-MM-DD``) and the publications CSV's
# ``Publication Date`` column. A four-digit year is enough for the
# schema's ``publication_year INTEGER`` cell.
_ISO_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<year>\d{4})[-/](?P<month>\d{2})[-/](?P<day>\d{2})$",
)

# Required header columns per CSV. Captured at module scope so the
# header-drift error message lists every expected column at one place.
_SCORES_REQUIRED_HEADERS: Final[tuple[str, ...]] = (
    "Polygenic Score (PGS) ID",
    "PGS Name",
    "Reported Trait",
    "Mapped Trait(s) (EFO ID)",
    "Number of Variants",
    "PGS Publication (PGP) ID",
    "Ancestry Distribution (%) - Source of Variant Associations (GWAS)",
    "Ancestry Distribution (%) - Score Development/Training",
)
_PUBLICATIONS_REQUIRED_HEADERS: Final[tuple[str, ...]] = (
    "PGS Publication/Study (PGP) ID",
    "Publication Date",
    "digital object identifier (doi)",
    "PubMed ID (PMID)",
)
_EFO_TRAITS_REQUIRED_HEADERS: Final[tuple[str, ...]] = (
    "Ontology Trait ID",
    "Ontology Trait Label",
)
_PERFORMANCE_REQUIRED_HEADERS: Final[tuple[str, ...]] = (
    "Evaluated Score",
    "Odds Ratio (OR)",
    "Area Under the Receiver-Operating Characteristic Curve (AUROC)",
)

# Arrow schema used by ``_insert_chunk``. Column order matches the
# INSERT column list constructed below.
_ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("score_record_id", pa.int64(), nullable=False),
        pa.field("pgs_id", pa.string(), nullable=False),
        pa.field("pgs_name", pa.string()),
        pa.field("trait_efo", pa.string()),
        pa.field("trait_reported", pa.string()),
        pa.field("trait_category", pa.string()),
        pa.field("publication_pmid", pa.string()),
        pa.field("publication_doi", pa.string()),
        pa.field("publication_year", pa.int32()),
        pa.field("variants_total", pa.int32()),
        pa.field("reference_population", pa.string()),
        pa.field("ancestry_distribution", pa.string()),
        pa.field("performance_auc", pa.float64()),
        pa.field("performance_or_per_sd", pa.float64()),
        pa.field("source_version_id", pa.int64(), nullable=False),
        pa.field("retrieval_date", pa.timestamp("us"), nullable=False),
    ],
)


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    """One row destined for ``pgs_catalog_scores``.

    Mirrors the destination schema's variable columns.
    ``score_record_id``, ``source_version_id``, and ``retrieval_date``
    are assigned at bulk-insert time after parsing completes.
    ``weights_storage`` is left to the schema default
    (``'overlapping_only'``).
    """

    pgs_id: str
    pgs_name: str | None
    trait_efo: str | None
    trait_reported: str | None
    trait_category: str | None
    publication_pmid: str | None
    publication_doi: str | None
    publication_year: int | None
    variants_total: int | None
    reference_population: str | None
    ancestry_distribution: str | None
    performance_auc: float | None
    performance_or_per_sd: float | None


@dataclass(slots=True)
class _ParseStats:
    """Mutable parser-level counters surfaced at end of load."""

    rows_read_scores: int = 0
    rows_read_publications: int = 0
    rows_read_traits: int = 0
    rows_read_performance: int = 0
    rows_read_trait_categories: int = 0
    orphan_publication_refs: int = 0
    orphan_trait_refs: int = 0
    scores_without_performance: int = 0
    multi_cohort_performance: int = 0
    truncated_trait_efo: int = 0
    extra: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _RawScoreRow:
    """Captured columns from one ``pgs_all_metadata_scores.csv`` row."""

    pgs_id: str
    pgs_name: str | None
    trait_reported: str | None
    trait_efo: str | None
    variants_total: int | None
    publication_id: str | None
    ancestry_distribution: str | None
    reference_population: str | None


@dataclass(frozen=True, slots=True)
class _RawPublicationRow:
    """Captured columns from one ``pgs_all_metadata_publications.csv`` row."""

    publication_pmid: str | None
    publication_doi: str | None
    publication_year: int | None


@dataclass(frozen=True, slots=True)
class _RawTraitRow:
    """Captured columns from one ``pgs_all_metadata_efo_traits.csv`` row."""

    trait_category: str | None


@dataclass(frozen=True, slots=True)
class _RawPerformanceRow:
    """Captured columns from one ``pgs_all_metadata_performance_metrics.csv`` row."""

    auc: float | None
    or_per_sd: float | None


# ---------------------------------------------------------------------------
# Field-level coercions.
# ---------------------------------------------------------------------------


def _empty_to_none(value: str) -> str | None:
    """Return ``None`` for empty / standard-missing / whitespace-only values."""
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    return trimmed


def _parse_int(value: str) -> int | None:
    """Coerce a CSV cell to an integer; missing / non-numeric → None."""
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    try:
        return int(trimmed)
    except ValueError:
        return None


def _parse_year(value: str) -> int | None:
    """Pull the four-digit year out of an ISO-form date string.

    PGS Catalog publications ship ``Publication Date`` as
    ``YYYY-MM-DD`` (occasionally ``YYYY/MM/DD``). The schema's
    ``publication_year`` is just the integer year; we extract it via
    the ISO-date regex so non-conforming text returns ``None`` instead
    of silently truncating.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    match = _ISO_DATE_RE.match(trimmed)
    if match is None:
        return None
    try:
        return int(match.group("year"))
    except ValueError:
        return None


def _parse_leading_number(value: str) -> float | None:
    """Extract the leading floating-point estimate from a metric cell.

    PGS Catalog encodes per-cohort performance metrics as
    ``"<estimate> [<lower>,<upper>]"`` (e.g. ``"0.622 [0.619,0.627]"``
    for AUROC or ``"1.55 [1.52,1.58]"`` for OR). The schema's
    ``performance_auc`` / ``performance_or_per_sd`` columns are
    scalars, so the loader keeps only the point estimate. Missing /
    pure-text / unparseable returns ``None``.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    match = _LEADING_NUMBER_RE.match(trimmed)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _first_efo_id(value: str) -> tuple[str | None, bool]:
    """Return ``(first_efo, was_truncated)`` from a comma-list cell.

    ``Mapped Trait(s) (EFO ID)`` ships as a single EFO/MONDO/HP ID
    in the common case (e.g. ``"MONDO_0004989"``) but as a
    comma-separated list when a score is mapped to multiple
    ontology terms. The schema's ``trait_efo VARCHAR`` is
    single-valued; the loader keeps the first ID (the curators'
    primary mapping) and reports the truncation count via the
    returned flag so the loader can sum across the stream and
    surface the total in the end-of-load summary.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None, False
    parts = [p.strip() for p in trimmed.split(",") if p.strip()]
    if not parts:
        return None, False
    return parts[0], len(parts) > 1


# ---------------------------------------------------------------------------
# Bundle streaming.
# ---------------------------------------------------------------------------


@contextmanager
def _open_csv_from_bundle(
    bundle_path: Path,
    member_name: str,
) -> Iterator[TextIO]:
    """Yield a UTF-8 text handle over the named CSV inside the metadata bundle.

    The PGS Catalog metadata bundle is a single-layer ``.tar.gz``.
    :mod:`tarfile` opens it with ``mode="r:gz"`` (random-access read
    of a gzipped TAR). The streaming-mode alternative (``"r|gz"``)
    returns members backed by a non-seekable inner stream, which
    breaks :class:`io.TextIOWrapper`'s seek-probe. The bundle is
    small (~4 MB compressed; ~15 MB after decompress) so the memory
    cost of non-streaming open is acceptable -- the trade-off the
    upstream archive shape forces on us.

    The upstream packaging used absolute paths so TAR member names
    typically include a leading ``/`` (e.g.
    ``"/pgs_all_metadata_scores.csv"``); the loader matches via
    ``endswith`` rather than exact equality. Directory entries and
    members that ``extractfile`` cannot open (links, devices) are
    skipped silently and the iteration continues.

    Raises :class:`ValueError` when the bundle does not carry the
    expected member (shape drift / future archive layout change).
    The same exception class is used by the GWAS Catalog ZIP helper
    for consistency at the runbook level.
    """
    with tarfile.open(bundle_path, mode="r:gz") as tf:
        for member in tf.getmembers():
            if member.isdir():
                continue
            if not member.name.endswith(member_name):
                continue
            fh = tf.extractfile(member)
            if fh is None:
                # Symlink / device / hardlink members can return None
                # from extractfile -- skip and continue scanning.
                continue
            yield io.TextIOWrapper(fh, encoding="utf-8", newline="")
            return
    msg = f"PGS Catalog bundle {bundle_path.name} missing expected entry {member_name!r}"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Per-file parsers.
# ---------------------------------------------------------------------------


def _require_headers(
    reader: csv.DictReader[str],
    required: tuple[str, ...],
    *,
    member_name: str,
) -> None:
    """Loud-fail when a CSV is missing any column we depend on."""
    if reader.fieldnames is None:
        msg = f"PGS Catalog CSV {member_name} has no header row"
        raise ValueError(msg)
    missing = [h for h in required if h not in reader.fieldnames]
    if missing:
        msg = (
            f"PGS Catalog CSV {member_name} is missing expected columns "
            f"{missing!r}; got {list(reader.fieldnames)!r}"
        )
        raise ValueError(msg)


def _parse_scores(
    fh: TextIO,
    stats: _ParseStats,
) -> dict[str, _RawScoreRow]:
    """Stream the scores CSV into a ``pgs_id`` → ``_RawScoreRow`` dict.

    Increments :attr:`_ParseStats.rows_read_scores` per row and
    :attr:`_ParseStats.truncated_trait_efo` on every multi-EFO
    ``Mapped Trait(s) (EFO ID)`` cell. Rows without a parseable
    ``pgs_id`` are skipped silently -- the schema's ``pgs_id NOT
    NULL`` would reject them at insert time anyway, and the upstream
    contract guarantees one per row.
    """
    reader = csv.DictReader(fh)
    _require_headers(reader, _SCORES_REQUIRED_HEADERS, member_name=_SCORES_MEMBER)
    out: dict[str, _RawScoreRow] = {}
    for raw in reader:
        stats.rows_read_scores += 1
        pgs_id = _empty_to_none(raw.get("Polygenic Score (PGS) ID", ""))
        if pgs_id is None:
            continue
        trait_efo, truncated = _first_efo_id(raw.get("Mapped Trait(s) (EFO ID)", ""))
        if truncated:
            stats.truncated_trait_efo += 1
        out[pgs_id] = _RawScoreRow(
            pgs_id=pgs_id,
            pgs_name=_empty_to_none(raw.get("PGS Name", "")),
            trait_reported=_empty_to_none(raw.get("Reported Trait", "")),
            trait_efo=trait_efo,
            variants_total=_parse_int(raw.get("Number of Variants", "")),
            publication_id=_empty_to_none(raw.get("PGS Publication (PGP) ID", "")),
            ancestry_distribution=_empty_to_none(
                raw.get(
                    "Ancestry Distribution (%) - Source of Variant Associations (GWAS)",
                    "",
                ),
            ),
            reference_population=_empty_to_none(
                raw.get("Ancestry Distribution (%) - Score Development/Training", ""),
            ),
        )
    return out


def _parse_publications(
    fh: TextIO,
    stats: _ParseStats,
) -> dict[str, _RawPublicationRow]:
    """Stream the publications CSV into a ``PGP ID`` → row dict.

    Increments :attr:`_ParseStats.rows_read_publications` per row.
    Rows without a parseable PGP ID are skipped silently.
    """
    reader = csv.DictReader(fh)
    _require_headers(
        reader,
        _PUBLICATIONS_REQUIRED_HEADERS,
        member_name=_PUBLICATIONS_MEMBER,
    )
    out: dict[str, _RawPublicationRow] = {}
    for raw in reader:
        stats.rows_read_publications += 1
        pgp_id = _empty_to_none(raw.get("PGS Publication/Study (PGP) ID", ""))
        if pgp_id is None:
            continue
        pmid_raw = raw.get("PubMed ID (PMID)", "")
        pmid = _empty_to_none(pmid_raw)
        # PMID ships as an integer string in the CSV; preserve as string
        # to match the schema's VARCHAR column shape.
        out[pgp_id] = _RawPublicationRow(
            publication_pmid=pmid,
            publication_doi=_empty_to_none(raw.get("digital object identifier (doi)", "")),
            publication_year=_parse_year(raw.get("Publication Date", "")),
        )
    return out


def _parse_traits(
    fh: TextIO,
    stats: _ParseStats,
) -> dict[str, _RawTraitRow]:
    """Stream the EFO traits CSV into an ``EFO ID`` → row dict.

    Increments :attr:`_ParseStats.rows_read_traits` per row.

    The upstream EFO traits CSV does not ship a ``Trait Category``
    column at the verified date -- the closest signal is the
    ``Ontology Trait Description``, which is a free-text definition
    rather than a category label. The category lookup for the
    schema's ``trait_category`` column flows through the separate
    ``/rest/trait_category/all`` REST endpoint instead (see
    :func:`_parse_trait_categories`). The bundle's EFO traits CSV
    is still parsed at this stage: the dict's keys drive the
    ``orphan_trait_refs`` counter on the join (a score whose
    ``trait_efo`` is missing from the EFO file is the "orphan"
    signal), keeping the counter's semantics unchanged from the
    original 5.4 contract.
    """
    reader = csv.DictReader(fh)
    _require_headers(reader, _EFO_TRAITS_REQUIRED_HEADERS, member_name=_EFO_TRAITS_MEMBER)
    out: dict[str, _RawTraitRow] = {}
    for raw in reader:
        stats.rows_read_traits += 1
        efo_id = _empty_to_none(raw.get("Ontology Trait ID", ""))
        if efo_id is None:
            continue
        out[efo_id] = _RawTraitRow(trait_category=None)
    return out


def _validate_trait_category_payload(payload: object) -> list[object]:
    """Validate the trait_category JSON envelope and return the ``results`` list.

    Three loud-fail shape checks:

    * Top level must be a JSON object (not a list, string, etc.).
    * ``next`` must be ``None`` -- the loader assumes the response
      fits in a single page, which is true at the verified date
      (~11 categories vs the REST default page size). A non-null
      ``next`` means the upstream has grown past that page size
      and the parser needs updating to follow pagination.
    * ``results`` must be a list.
    """
    if not isinstance(payload, dict):
        msg = (
            f"PGS Catalog trait_category payload was "
            f"{type(payload).__name__}, expected a JSON object"
        )
        raise ValueError(msg)  # noqa: TRY004 — shape error, not type error
    if payload.get("next") is not None:
        msg = (
            "PGS Catalog trait_category endpoint returned a paginated "
            f"response (next={payload.get('next')!r}); the loader assumes "
            "a single-page response and needs updating to follow pagination"
        )
        raise ValueError(msg)
    results = payload.get("results")
    if not isinstance(results, list):
        msg = (
            "PGS Catalog trait_category payload missing 'results' list; "
            f"got keys {sorted(payload)!r}"
        )
        raise ValueError(msg)  # noqa: TRY004 — shape error, not type error
    return results


def _emit_trait_category_entries(
    label: str,
    efotraits: list[object],
    out: dict[str, str],
) -> int:
    """Emit ``(efo_id, label)`` pairs into ``out``; return the duplicate count.

    Last-write-wins on duplicate EFOs across categories; the
    returned count is how many EFOs were already present under a
    *different* label when this category's traits were emitted.
    """
    duplicates = 0
    for trait in efotraits:
        if not isinstance(trait, dict):
            continue
        efo_id = trait.get("id")
        if not isinstance(efo_id, str) or not efo_id:
            continue
        if efo_id in out and out[efo_id] != label:
            duplicates += 1
        out[efo_id] = label
    return duplicates


def _parse_trait_categories(
    path: Path,
    stats: _ParseStats,
) -> dict[str, str]:
    """Load PGS Catalog's trait-category JSON into a ``efo_id`` → ``category`` dict.

    The REST endpoint returns paginated JSON of the shape::

        {
          "count": 11,
          "next": null,
          "results": [
            {"label": "Cardiovascular disease",
             "efotraits": [
                 {"id": "EFO_0001645", "label": "...", ...},
                 ...
             ]},
            ...
          ]
        }

    The function reads the cached JSON payload, walks every category,
    and emits one ``(efo_id, category_label)`` pair per EFO trait in
    the category. If the same EFO appears in multiple categories
    (uncommon but possible), the last one wins; the counter
    :attr:`_ParseStats.extra` tracks the duplicate count under the
    key ``efo_in_multiple_categories``.

    Bumps :attr:`_ParseStats.rows_read_trait_categories` per category
    row. See :func:`_validate_trait_category_payload` for the
    loud-fail shape checks.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = _validate_trait_category_payload(payload)
    out: dict[str, str] = {}
    duplicate_efos = 0
    for entry in results:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        efotraits = entry.get("efotraits")
        if not isinstance(label, str) or not label:
            continue
        if not isinstance(efotraits, list):
            continue
        stats.rows_read_trait_categories += 1
        duplicate_efos += _emit_trait_category_entries(label, efotraits, out)
    if duplicate_efos:
        stats.extra["efo_in_multiple_categories"] = duplicate_efos
    return out


def _parse_performance(
    fh: TextIO,
    stats: _ParseStats,
) -> dict[str, list[_RawPerformanceRow]]:
    """Stream the performance-metrics CSV into a ``PGS ID`` → list dict.

    Increments :attr:`_ParseStats.rows_read_performance` per row. A
    single PGS appears in multiple rows (one per evaluation cohort /
    sample set); the join layer collapses the list via the
    max-across-cohorts reduction documented on :func:`_join_metadata`.

    Per-row missing handling: the ``Odds Ratio (OR)`` and
    ``Area Under the Receiver-Operating Characteristic Curve
    (AUROC)`` cells often arrive as ``"<estimate> [<lower>,<upper>]"``
    (e.g. ``"0.622 [0.619,0.627]"``). :func:`_parse_leading_number`
    extracts only the point estimate; the CI is dropped at this
    layer. Missing / pure-text cells return ``None`` for that field
    while the rest of the row still contributes.
    """
    reader = csv.DictReader(fh)
    _require_headers(reader, _PERFORMANCE_REQUIRED_HEADERS, member_name=_PERFORMANCE_MEMBER)
    out: dict[str, list[_RawPerformanceRow]] = {}
    for raw in reader:
        stats.rows_read_performance += 1
        pgs_id = _empty_to_none(raw.get("Evaluated Score", ""))
        if pgs_id is None:
            continue
        auc = _parse_leading_number(
            raw.get(
                "Area Under the Receiver-Operating Characteristic Curve (AUROC)",
                "",
            ),
        )
        or_value = _parse_leading_number(raw.get("Odds Ratio (OR)", ""))
        out.setdefault(pgs_id, []).append(
            _RawPerformanceRow(auc=auc, or_per_sd=or_value),
        )
    return out


# ---------------------------------------------------------------------------
# In-memory join + performance reduction.
# ---------------------------------------------------------------------------


def _reduce_performance(
    entries: list[_RawPerformanceRow],
) -> tuple[float | None, float | None]:
    """Collapse per-cohort performance entries into ``(auc, or_per_sd)``.

    Rule: ``max(values for non-NULL entries)`` per column. When every
    entry for that column is ``None``, the column returns ``None``.
    This is the simplest auditable reduction at this scale, not the
    most statistically honest one -- honest per-cohort reporting
    would require a separate ``pgs_catalog_performance`` table, which
    is a future schema change, not 5.4 work.
    """
    auc_values = [e.auc for e in entries if e.auc is not None]
    or_values = [e.or_per_sd for e in entries if e.or_per_sd is not None]
    return (
        max(auc_values) if auc_values else None,
        max(or_values) if or_values else None,
    )


def _join_metadata(  # noqa: PLR0913 — the five per-source dicts + stats are not collapsible
    scores: dict[str, _RawScoreRow],
    publications: dict[str, _RawPublicationRow],
    traits: dict[str, _RawTraitRow],
    performance: dict[str, list[_RawPerformanceRow]],
    trait_categories: dict[str, str],
    stats: _ParseStats,
) -> list[_ParsedRow]:
    """Join the per-file dicts into one ``_ParsedRow`` per PGS.

    Counters bumped on this path:

    * ``orphan_publication_refs`` -- a score references a PGP ID
      missing from the publications dict. The row still emits with
      ``publication_pmid`` / ``publication_doi`` / ``publication_year``
      set to ``None``.
    * ``orphan_trait_refs`` -- a score references an EFO ID missing
      from the bundle's EFO traits dict. The row still emits with
      ``trait_category=None`` (the orphan counter and the category
      lookup are independent; an orphan score may still pick up a
      category if its EFO ID is in the REST trait_category payload).
    * ``scores_without_performance`` -- a score has no entries in
      the performance dict. Both performance columns emit ``None``.
    * ``multi_cohort_performance`` -- a score had two or more
      performance entries; the max reduction collapsed them. The
      counter reads as "how many scores had multi-cohort
      performance data".

    ``trait_categories`` is the ``efo_id`` → ``category_label`` dict
    sourced from PGS Catalog's ``/rest/trait_category/all`` endpoint
    (see :func:`_parse_trait_categories`). The bundle's
    ``pgs_all_metadata_efo_traits.csv`` does not carry a category
    column, so this is the only source for ``trait_category``. A
    score whose ``trait_efo`` is not in ``trait_categories`` leaves
    that column NULL but does not affect any of the counters --
    the REST endpoint's coverage is independent of the orphan
    bookkeeping driven by the bundle's EFO file.

    Output is sorted by ``pgs_id`` ascending so the
    ``score_record_id`` allocation order is deterministic across
    runs and the tests can assert against expected sequences.
    """
    out: list[_ParsedRow] = []
    for pgs_id in sorted(scores):
        score = scores[pgs_id]
        # Publication join.
        publication: _RawPublicationRow | None = None
        if score.publication_id is not None:
            publication = publications.get(score.publication_id)
            if publication is None:
                stats.orphan_publication_refs += 1
        # Trait orphan signal (driven by the bundle's EFO file).
        # The category lookup uses the REST trait_category dict
        # independently; the two sources may have slightly different
        # coverage and that is fine.
        if score.trait_efo is not None and score.trait_efo not in traits:
            stats.orphan_trait_refs += 1
        trait_category: str | None = (
            trait_categories.get(score.trait_efo) if score.trait_efo is not None else None
        )
        # Performance reduction.
        entries = performance.get(pgs_id, [])
        if not entries:
            stats.scores_without_performance += 1
            performance_auc: float | None = None
            performance_or_per_sd: float | None = None
        else:
            if len(entries) > 1:
                stats.multi_cohort_performance += 1
            performance_auc, performance_or_per_sd = _reduce_performance(entries)
        out.append(
            _ParsedRow(
                pgs_id=pgs_id,
                pgs_name=score.pgs_name,
                trait_efo=score.trait_efo,
                trait_reported=score.trait_reported,
                trait_category=trait_category,
                publication_pmid=publication.publication_pmid if publication else None,
                publication_doi=publication.publication_doi if publication else None,
                publication_year=publication.publication_year if publication else None,
                variants_total=score.variants_total,
                reference_population=score.reference_population,
                ancestry_distribution=score.ancestry_distribution,
                performance_auc=performance_auc,
                performance_or_per_sd=performance_or_per_sd,
            ),
        )
    return out


# ---------------------------------------------------------------------------
# Version resolution.
# ---------------------------------------------------------------------------


def _parse_release_payload(payload: object) -> date:
    """Pull the release date out of a parsed release-current payload.

    Strict on shape: the payload must be a mapping that carries a
    ``date`` key (the live shape the PGS Catalog REST endpoint
    returns) or a ``release_date`` / ``releasedate`` key (defensive
    against a documented historical form). The value must match
    :data:`_ISO_DATE_RE`. Any drift raises a clear
    :class:`ValueError` so a future API change surfaces as a loud
    refresh failure rather than a silent stale version label.
    """
    if not isinstance(payload, dict):
        msg = f"PGS Catalog release response was {type(payload).__name__}, expected a JSON object"
        raise ValueError(msg)  # noqa: TRY004 — shape error, not type error
    raw = payload.get("date") or payload.get("release_date") or payload.get("releasedate")
    if not isinstance(raw, str) or not raw:
        msg = (
            "PGS Catalog release response is missing a 'date' / 'release_date' / "
            f"'releasedate' string field; got keys {sorted(payload)!r}"
        )
        raise ValueError(msg)
    match = _ISO_DATE_RE.match(raw.strip())
    if match is None:
        msg = (
            f"PGS Catalog release date {raw!r} does not match YYYY-MM-DD; "
            "upstream shape has drifted"
        )
        raise ValueError(msg)
    return date(
        year=int(match.group("year")),
        month=int(match.group("month")),
        day=int(match.group("day")),
    )


def _format_version(release_date: date) -> str:
    """Render a release date as the canonical ``YYYY_MM_DD`` version label."""
    return release_date.strftime("%Y_%m_%d")


def _resolve_version_via_release_latest() -> str:
    """Resolve the PGS Catalog version label via the release-current endpoint.

    Issues an audited GET to :data:`PGS_RELEASE_LATEST_URL`, parses
    the JSON body, extracts the release date via
    :func:`_parse_release_payload`, and renders it as ``YYYY_MM_DD``.

    Failure modes (mirror the GWAS Catalog stats resolver):

    * :class:`ExternalCallsDisabledError` propagates -- the privacy
      gate is fail-closed.
    * Any other :class:`ExternalCallError` (network, HTTP 4xx/5xx)
      raises through. No silent fallback to "today" -- that would
      either cause a duplicate load against the previously-current
      release or paint a misleading version label onto a release
      that's actually identical.
    * Malformed JSON or a missing ``date`` field raises
      :class:`ValueError` with the live payload shape, so a future
      upstream API change surfaces as a fast diagnostic.

    The release GET is the loader's first audited call. Placing it
    before the download means a fresh refresh against an unchanged
    release short-circuits before re-fetching the ~4 MB bundle.
    """
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
            "GET",
            PGS_RELEASE_LATEST_URL,
            resource_type="annotation_source",
            resource_id=_RELEASE_RESOURCE_ID,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        msg = f"PGS Catalog release response is not valid JSON: {response.text[:200]}"
        raise ValueError(msg) from exc

    release_date = _parse_release_payload(payload)
    return _format_version(release_date)


# ---------------------------------------------------------------------------
# Chunked bulk insert.
# ---------------------------------------------------------------------------


def _next_score_record_id(conn: DuckDBPyConnection) -> int:
    """``COALESCE(MAX(score_record_id), 0) + 1``.

    Mirrors the project-wide app-allocated BIGINT PK pattern. Called
    once at the start of streaming; per-chunk base IDs are advanced
    from the previous chunk's actual size.
    """
    row = conn.execute(
        f"SELECT COALESCE(MAX(score_record_id), 0) FROM {_TARGET_TABLE}",  # noqa: S608
    ).fetchone()
    return int(row[0]) + 1 if row is not None else 1


def _iter_chunks(
    rows_iter: Iterable[_ParsedRow],
    chunk_size: int,
) -> Iterator[list[_ParsedRow]]:
    """Yield ``chunk_size``-sized slices from ``rows_iter``.

    Mirrors the GWAS Catalog helper. PGS Catalog ships ~5-7K rows so
    every refresh fits in a single chunk; the chunked-insert code
    path stays exercised across the loader family.
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
    """Insert one chunk of ``_ParsedRow`` into ``pgs_catalog_scores``.

    Builds a PyArrow Table with one column per destination column
    (including ``score_record_id``, ``source_version_id``, and
    ``retrieval_date``), registers it under a temp name, then issues
    ``INSERT INTO pgs_catalog_scores (...) SELECT ... FROM <temp>``
    and unregisters. ``weights_storage`` is omitted from the INSERT
    column list so the schema's ``DEFAULT 'overlapping_only'`` applies
    to every new row.

    Returns the number of rows inserted (== ``len(rows)``). A
    zero-row call inserts nothing and returns 0.
    """
    if not rows:
        return 0

    n = len(rows)
    naive_retrieval = retrieval_date.astimezone(UTC).replace(tzinfo=None)
    table = pa.table(
        {
            "score_record_id": pa.array(range(base_id, base_id + n), type=pa.int64()),
            "pgs_id": pa.array([r.pgs_id for r in rows], type=pa.string()),
            "pgs_name": pa.array([r.pgs_name for r in rows], type=pa.string()),
            "trait_efo": pa.array([r.trait_efo for r in rows], type=pa.string()),
            "trait_reported": pa.array([r.trait_reported for r in rows], type=pa.string()),
            "trait_category": pa.array([r.trait_category for r in rows], type=pa.string()),
            "publication_pmid": pa.array(
                [r.publication_pmid for r in rows],
                type=pa.string(),
            ),
            "publication_doi": pa.array(
                [r.publication_doi for r in rows],
                type=pa.string(),
            ),
            "publication_year": pa.array(
                [r.publication_year for r in rows],
                type=pa.int32(),
            ),
            "variants_total": pa.array(
                [r.variants_total for r in rows],
                type=pa.int32(),
            ),
            "reference_population": pa.array(
                [r.reference_population for r in rows],
                type=pa.string(),
            ),
            "ancestry_distribution": pa.array(
                [r.ancestry_distribution for r in rows],
                type=pa.string(),
            ),
            "performance_auc": pa.array(
                [r.performance_auc for r in rows],
                type=pa.float64(),
            ),
            "performance_or_per_sd": pa.array(
                [r.performance_or_per_sd for r in rows],
                type=pa.float64(),
            ),
            "source_version_id": pa.array([source_version_id] * n, type=pa.int64()),
            "retrieval_date": pa.array(
                [naive_retrieval] * n,
                type=pa.timestamp("us"),
            ),
        },
        schema=_ARROW_SCHEMA,
    )
    try:
        conn.register("_pgs_stage_arrow", table)
        conn.execute(
            f"""
            INSERT INTO {_TARGET_TABLE} (
                score_record_id, pgs_id, pgs_name,
                trait_efo, trait_reported, trait_category,
                publication_pmid, publication_doi, publication_year,
                variants_total,
                reference_population, ancestry_distribution,
                performance_auc, performance_or_per_sd,
                source_version_id, retrieval_date
            )
            SELECT
                score_record_id, pgs_id, pgs_name,
                trait_efo, trait_reported, trait_category,
                publication_pmid, publication_doi, publication_year,
                variants_total,
                reference_population, ancestry_distribution,
                performance_auc, performance_or_per_sd,
                source_version_id, retrieval_date
              FROM _pgs_stage_arrow
            """,  # noqa: S608 — table name is a module constant, not user input
        )
    finally:
        conn.unregister("_pgs_stage_arrow")
    return n


def _stream_bulk_insert(
    conn: DuckDBPyConnection,
    rows_iter: Iterable[_ParsedRow],
    *,
    source_version_id: int,
    retrieval_date: datetime,
    chunk_size: int = _CHUNK_SIZE,
) -> int:
    """Drain ``rows_iter`` into ``pgs_catalog_scores`` in chunks.

    Each chunk is a separate :func:`_insert_chunk` call (PyArrow
    Table registration + ``INSERT ... SELECT``). All chunks must run
    inside the same DuckDB transaction -- the caller bracket-controls
    ``conn.begin()`` / ``conn.commit()``. Chunks are deliberately
    *not* committed individually so a mid-stream failure rolls back
    the deactivation of prior active rows along with the partial
    insert (the supersession-over-update invariant).

    Per-chunk progress is logged at INFO with the chunk index, the
    row count, and the cumulative total. Returns the total number of
    rows inserted.
    """
    base_id = _next_score_record_id(conn)
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
            "pgs_catalog.bulk_insert.chunk",
            chunk_index=chunk_index,
            rows=inserted,
            cumulative=total,
        )
    return total


# ---------------------------------------------------------------------------
# Rollback helper.
# ---------------------------------------------------------------------------


def _cleanup_orphan_version_row(
    conn: DuckDBPyConnection,
    source_version_id: int,
) -> None:
    """Best-effort delete of an orphan ``annotation_source_versions`` row.

    Same shape as the PharmGKB / CPIC / ClinVar / GWAS Catalog
    helpers -- called when the supersede + chunked-insert
    transaction rolls back so the version row that
    :func:`insert_source_version` committed in its own transaction
    doesn't leave a dangling "version exists but zero rows
    referenced" state. The DELETE is FK-safe because no
    ``pgs_catalog_scores`` rows reference the new
    ``source_version_id`` yet (the stream insert never committed).
    Failures are swallowed and logged; the caller is already
    raising the original exception.
    """
    try:
        conn.execute(
            "DELETE FROM annotation_source_versions WHERE source_version_id = ?",
            [source_version_id],
        )
    except Exception:  # noqa: BLE001 — best-effort cleanup; original exc re-raised by caller
        logger.warning(
            "pgs_catalog.cleanup.orphan_version_row_delete_failed",
            source_version_id=source_version_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Post-load summary (drift identifiers).
# ---------------------------------------------------------------------------


def _summarize_active(conn: DuckDBPyConnection) -> dict[str, object]:
    """Compute the drift identifiers logged at end-of-load.

    Returns the durable signals real-data verification compares
    across releases:

    * ``active_total`` -- count of rows under the currently-active version
    * ``distinct_pgs_id`` -- ``COUNT(DISTINCT pgs_id)``
    * ``distinct_trait_efo`` -- ``COUNT(DISTINCT trait_efo)``
    * ``distinct_publication_pmid`` -- ``COUNT(DISTINCT publication_pmid)``
    * ``distinct_trait_category`` -- ``COUNT(DISTINCT trait_category)``
    * ``with_performance_auc`` -- count where ``performance_auc IS NOT NULL``
    * ``with_performance_or_per_sd`` -- count where
      ``performance_or_per_sd IS NOT NULL``

    Counts rows whose ``source_version_id`` matches the
    ``annotation_sources`` pointer for ``pgs_catalog``. Run after the
    supersession transaction commits so the pointer already names the
    new version.
    """
    total_row = conn.execute(
        f"SELECT COUNT(*) FROM {_TARGET_TABLE} p "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id",
    ).fetchone()
    distinct_pgs_row = conn.execute(
        f"SELECT COUNT(DISTINCT p.pgs_id) FROM {_TARGET_TABLE} p "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id",
    ).fetchone()
    distinct_efo_row = conn.execute(
        f"SELECT COUNT(DISTINCT p.trait_efo) FROM {_TARGET_TABLE} p "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id "
        "WHERE p.trait_efo IS NOT NULL",
    ).fetchone()
    distinct_pmid_row = conn.execute(
        f"SELECT COUNT(DISTINCT p.publication_pmid) FROM {_TARGET_TABLE} p "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id "
        "WHERE p.publication_pmid IS NOT NULL",
    ).fetchone()
    distinct_category_row = conn.execute(
        f"SELECT COUNT(DISTINCT p.trait_category) FROM {_TARGET_TABLE} p "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id "
        "WHERE p.trait_category IS NOT NULL",
    ).fetchone()
    with_auc_row = conn.execute(
        f"SELECT COUNT(*) FROM {_TARGET_TABLE} p "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id "
        "WHERE p.performance_auc IS NOT NULL",
    ).fetchone()
    with_or_row = conn.execute(
        f"SELECT COUNT(*) FROM {_TARGET_TABLE} p "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id "
        "WHERE p.performance_or_per_sd IS NOT NULL",
    ).fetchone()
    return {
        "active_total": int(total_row[0]) if total_row is not None else 0,
        "distinct_pgs_id": int(distinct_pgs_row[0]) if distinct_pgs_row is not None else 0,
        "distinct_trait_efo": int(distinct_efo_row[0]) if distinct_efo_row is not None else 0,
        "distinct_publication_pmid": int(distinct_pmid_row[0])
        if distinct_pmid_row is not None
        else 0,
        "distinct_trait_category": int(distinct_category_row[0])
        if distinct_category_row is not None
        else 0,
        "with_performance_auc": int(with_auc_row[0]) if with_auc_row is not None else 0,
        "with_performance_or_per_sd": int(with_or_row[0]) if with_or_row is not None else 0,
    }


# ---------------------------------------------------------------------------
# Module entry point — refresh
# ---------------------------------------------------------------------------


def refresh(
    force: bool,  # noqa: FBT001 — positional matches registry's RefreshFn signature
    skip_if_same_version: bool = False,  # noqa: FBT001, FBT002 — opt-in default for the new flag
) -> RefreshResult:
    """Refresh PGS Catalog score-level metadata.

    Pipeline:

    1. Resolve version via the PGS Catalog REST release-current
       endpoint (audited). The endpoint returns
       ``{"date": "YYYY-MM-DD", ...}``; the date is rendered as
       ``YYYY_MM_DD`` and is the version label.
    2. Short-circuit and return ``was_already_current=True`` if a
       row in ``annotation_source_versions`` already names the
       resolved ``(source_db='pgs_catalog', version)`` and ``force``
       is ``False``.
    3a. Download the ``pgs_all_metadata.tar.gz`` bundle from the
       EBI FTP via the audited
       :func:`genome.annotate.downloads.download_to_cache`
       (skip-if-exists by default; ``force=True`` re-downloads).
    3b. Download the ``/rest/trait_category/all`` REST payload as
       a sibling JSON file. The bundle's EFO traits CSV does not
       carry a category column, so this second audited download
       supplies the ``efo_id`` → ``category`` dict that populates
       the schema's ``trait_category`` field.
    3c. If ``skip_if_same_version`` is ``True`` and the metadata
        bundle's (version, sha256) match the currently-active row,
        short-circuit via :func:`maybe_skip_same_version` (finding-009
        #14). ``source_file_hash`` is the bundle's SHA-256 -- the same
        value ``insert_source_version`` stores -- so the trait-category
        sibling is not included in the match (matches the
        ``insert_source_version`` call below). Off by default.
    4. Inside one DuckDB transaction: upsert
       ``annotation_source_versions``, open the bundle four times
       (one per metadata file) and parse each into its in-memory dict,
       parse the trait_category JSON into a separate dict, run
       :func:`_join_metadata` to produce the joined
       ``list[_ParsedRow]``, chunk-insert via
       :func:`_stream_bulk_insert`, update the version row's
       ``record_count`` once the streaming completes, and flip the
       ``annotation_sources`` pointer for ``pgs_catalog`` to the new
       ``source_version_id`` via :func:`flip_to_new_version`. The
       pointer flip is the supersession event; the prior set stays in
       ``pgs_catalog_scores`` indefinitely under its older
       ``source_version_id``. The supersession transaction is closed
       via :func:`commit_and_checkpoint` so the COMMIT + explicit
       CHECKPOINT phases are observable in the structlog stream
       (finding-009 #9 and #11).
    5. On exception: ``conn.rollback()``,
       :func:`_cleanup_orphan_version_row`, re-raise.
    6. Open a fresh read-only connection and emit a structlog
       summary line with the locked drift identifiers plus parser
       stats and elapsed wall-clock.
    7. Return a :class:`RefreshResult` describing what landed.
    """
    log = logger.bind(source=SOURCE_DB)

    # 1. Resolve version via the release-current endpoint.
    # ExternalCallsDisabledError propagates.
    version = _resolve_version_via_release_latest()
    log.info("pgs_catalog.version.resolved", version=version)

    # 2. Idempotence check -- short-circuit before downloading the bundle.
    with duckdb_connection() as conn:
        current = get_current_version(conn, SOURCE_DB)
        if current is not None and current.version == version and not force:
            log.info("pgs_catalog.skip_already_current", version=version)
            return RefreshResult(
                source_db=SOURCE_DB,
                source_version_id=current.source_version_id,
                version=version,
                record_count=current.record_count or 0,
                was_already_current=True,
            )

    # 3. Download the metadata bundle (skip-if-exists; force
    # re-downloads).
    download_result = download_to_cache(
        SOURCE_DB,
        PGS_METADATA_BUNDLE_URL,
        _CACHE_FILENAME,
        resource_id=_DOWNLOAD_RESOURCE_ID,
        force=force,
    )
    log.info(
        "pgs_catalog.download.audited",
        sha256=download_result.sha256[:16],
        size_bytes=download_result.size_bytes,
    )

    # 3b. Download the trait_category REST payload. The bundle's
    # EFO traits CSV does not carry a category column, so this
    # second audited download supplies the dictionary that
    # populates the schema's ``trait_category`` field.
    trait_categories_download = download_to_cache(
        SOURCE_DB,
        PGS_TRAIT_CATEGORY_URL,
        _TRAIT_CATEGORY_CACHE_FILENAME,
        resource_id=_TRAIT_CATEGORY_RESOURCE_ID,
        force=force,
    )
    log.info(
        "pgs_catalog.trait_categories.audited",
        sha256=trait_categories_download.sha256[:16],
        size_bytes=trait_categories_download.size_bytes,
    )

    # 3c. --skip-if-same-version short-circuit (finding-009 #14). Off by
    # default. The match key is the bundle's SHA-256 only -- the same
    # value insert_source_version stores -- so the trait-category
    # sibling does not participate in the match.
    skip = maybe_skip_same_version(
        source_db=SOURCE_DB,
        version=version,
        source_file_hash=download_result.sha256,
        skip_if_same_version=skip_if_same_version,
    )
    if skip is not None:
        return skip

    # 4. Single-transaction load. Mirrors the GWAS Catalog shape:
    # the version row insert runs in autocommit, then a second
    # transaction wraps the chunked insert + pointer flip atomically.
    # The flip runs after the INSERT so ``flip_to_new_version`` can
    # count the just-inserted rows for the event payload.
    started = time.monotonic()
    retrieval_date = datetime.now(UTC)
    stats = _ParseStats()
    inserted = 0
    flip: VersionFlipResult | None = None
    with duckdb_connection() as conn:
        source_version_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=version,
            source_url=PGS_METADATA_BUNDLE_URL,
            source_file_hash=download_result.sha256,
            source_file_size=download_result.size_bytes,
            record_count=None,
        )
        conn.begin()
        try:
            # Four bundle scans, one per metadata file, plus one
            # JSON load for the trait_category payload.
            with _open_csv_from_bundle(
                download_result.path,
                _SCORES_MEMBER,
            ) as fh:
                scores = _parse_scores(fh, stats)
            with _open_csv_from_bundle(
                download_result.path,
                _PUBLICATIONS_MEMBER,
            ) as fh:
                publications = _parse_publications(fh, stats)
            with _open_csv_from_bundle(
                download_result.path,
                _EFO_TRAITS_MEMBER,
            ) as fh:
                traits = _parse_traits(fh, stats)
            with _open_csv_from_bundle(
                download_result.path,
                _PERFORMANCE_MEMBER,
            ) as fh:
                performance = _parse_performance(fh, stats)
            trait_categories = _parse_trait_categories(
                trait_categories_download.path,
                stats,
            )
            joined = _join_metadata(
                scores,
                publications,
                traits,
                performance,
                trait_categories,
                stats,
            )
            inserted = _stream_bulk_insert(
                conn,
                joined,
                source_version_id=source_version_id,
                retrieval_date=retrieval_date,
            )
            conn.execute(
                "UPDATE annotation_source_versions "
                "SET record_count = ? "
                "WHERE source_version_id = ?",
                [inserted, source_version_id],
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

    elapsed = time.monotonic() - started
    assert flip is not None  # noqa: S101 — guaranteed by the try block returning normally

    # 5. Post-load summary (drift identifiers). Read-only; runs
    # against the just-committed state.
    with duckdb_connection(read_only=True) as conn:
        summary = _summarize_active(conn)

    log.info(
        "pgs_catalog.refresh.complete",
        version=version,
        sha256=download_result.sha256[:16],
        size_bytes=download_result.size_bytes,
        inserted=inserted,
        prior_version_id=flip.prior_version_id,
        prior_row_count=flip.prior_row_count,
        source_version_id=source_version_id,
        elapsed_seconds=round(elapsed, 1),
        rows_read_scores=stats.rows_read_scores,
        rows_read_publications=stats.rows_read_publications,
        rows_read_traits=stats.rows_read_traits,
        rows_read_performance=stats.rows_read_performance,
        rows_read_trait_categories=stats.rows_read_trait_categories,
        orphan_publication_refs=stats.orphan_publication_refs,
        orphan_trait_refs=stats.orphan_trait_refs,
        scores_without_performance=stats.scores_without_performance,
        multi_cohort_performance=stats.multi_cohort_performance,
        truncated_trait_efo=stats.truncated_trait_efo,
        active_total=summary["active_total"],
        distinct_pgs_id=summary["distinct_pgs_id"],
        distinct_trait_efo=summary["distinct_trait_efo"],
        distinct_publication_pmid=summary["distinct_publication_pmid"],
        distinct_trait_category=summary["distinct_trait_category"],
        with_performance_auc=summary["with_performance_auc"],
        with_performance_or_per_sd=summary["with_performance_or_per_sd"],
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
    "PGS_METADATA_BUNDLE_URL",
    "PGS_RELEASE_LATEST_URL",
    "PGS_TRAIT_CATEGORY_URL",
    "SOURCE_DB",
    "URL_VERIFIED_DATE",
    "refresh",
]
