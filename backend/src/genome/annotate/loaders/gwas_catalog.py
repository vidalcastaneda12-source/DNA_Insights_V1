"""GWAS Catalog associations loader.

Downloads GWAS Catalog's "all associations" ZIP archive (~600-700K
active rows at the current EBI release, distributed as a zipped TSV),
parses the contained TSV line-by-line with a streaming reader, and
chunk-loads into ``gwas_catalog_associations`` via PyArrow Table
registration + ``INSERT ... SELECT`` (the project's locked bulk-load
convention).

Sub-phase 5.3 — fourth loader after PharmGKB (5.1a), CPIC (5.1b), and
ClinVar (5.2). Mirrors the locked 5.1/5.2 template in shape:

* Module-level URL constants with a sibling ``URL_VERIFIED_DATE`` so a
  future reader can tell at a glance how stale the link is.
* A ``_resolve_version_via_stats`` step that calls the GWAS Catalog
  REST stats endpoint (``/api/search/stats``), parses the JSON
  ``"date"`` field, and renders it as ``YYYY_MM_DD`` -- matching the
  ClinVar loader's version-string convention.
* A ``refresh(force)`` function that resolves version, downloads
  (skip-if-exists), short-circuits when ``annotation_source_versions``
  already names the resolved version, and otherwise upserts +
  deactivates + chunk-bulk-inserts inside one DuckDB transaction.
* ``register_loader(SOURCE_DB, refresh)`` at module-import time so the
  CLI registry is populated by importing this module.

What sets GWAS Catalog apart from the prior three loaders:

* **Scale between CPIC and ClinVar.** CPIC ships ~3.5K rows, PharmGKB
  ~7K, ClinVar ~9M; GWAS Catalog is ~600-700K active associations at
  the current EBI release (one to two ClinVar chunks worth). The
  streaming parser + 250K-row chunked insert pattern still applies,
  even though the smaller corpus would fit in memory comfortably --
  consistency with the ClinVar template costs little here and keeps
  the chunked-insert code path exercised across releases.
* **Multi-SNP expansion at parse time.** A row whose ``SNPS`` column
  carries multiple ``;``-separated rsIDs (haplotype-style entries like
  ``rs123; rs456``) splits into one DB row per rsID, each sharing the
  same study accession, PMID, trait, statistics, and sample-size
  context. The schema's ``rsid VARCHAR NOT NULL`` reflects that the
  loader's atomic unit is (study, SNP), not (study, association entry).
* **Coordinate-less rows are dropped at parse time.** A row whose
  ``CHR_ID`` or ``CHR_POS`` is empty (or one of the GWAS Catalog
  "missing" tokens ``NA`` / ``NR`` / ``-``) cannot satisfy the
  schema's position-based join contract, so the row is dropped and
  counted in the parser stats. Real GWAS Catalog releases ship a few
  hundred such rows -- typically associations the curators have not
  yet positionally mapped.
* **Single-value mapped_trait_uri.** GWAS Catalog's ``MAPPED_TRAIT_URI``
  cell can carry multiple comma-separated EFO URIs when an
  association has been mapped to several EFO terms. The schema's
  ``mapped_trait_uri VARCHAR`` is single-valued, so the loader keeps
  the first URI (the canonical / primary mapping) and counts the
  truncations for the end-of-load summary. ``trait_id`` is derived
  from the same first URI.

Supersession is via the ``annotation_sources`` pointer table (PR 1's
version-pointer refactor): the loader inserts the new active set under
a fresh ``source_version_id`` and then flips the pointer for
``gwas_catalog`` to that id in one statement. Readers that want
current-version rows join through ``annotation_sources``. The prior
set stays in ``gwas_catalog_associations`` indefinitely under its
older ``source_version_id``. The schema lives in
``docs/schemas/schema_group_2_reference_annotations.md`` and is locked
for this sub-phase.

The loader does **not** touch ``variant_annotations_index`` -- that
refresh is a separate downstream concern in sub-phase 5.8.
"""

from __future__ import annotations

import csv
import io
import re
import time
import zipfile
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
from genome.ingest.models import normalize_chrom
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
# The legacy ``api/search/downloads/full`` endpoint that returned the
# canonical "all associations" TSV directly has been retired (404 since
# at least 2026 Q2). The current pattern is a two-step:
#
# 1. GET ``/api/search/stats`` -- returns JSON of the form
#    ``{"date": "YYYY-MM-DD", "ensemblbuild": "...", ...}``. The
#    ``date`` field is the release-snapshot date and is what we use as
#    the version label (``YYYY_MM_DD`` form, matching the ClinVar
#    loader's convention).
# 2. GET the dated release ZIP from the EBI FTP
#    (``gwas-catalog-associations_ontology-annotated-full.zip``).
#
# The stats-endpoint ``date`` is the release freeze date; the matching
# FTP directory is typically published 1-2 days later, so the two
# dates do NOT line up day-for-day. To stay robust against that
# offset (and against future shifts in the FTP layout), the download
# always goes through the ``latest/`` symlink directory -- EBI keeps
# it pointed at the current release. The version label remains the
# stats date so the supersession short-circuit still keys to "what
# release is loaded" rather than "what file did we cache". The race
# window between the stats call and the download is bounded by the
# weekly release cadence and is acceptable for personal-use refreshes.
#
# The downloaded ZIP contains a single TSV
# (``gwas-catalog-download-associations-alt-full.tsv``) carrying the
# same 38 columns the prior ``.tsv`` endpoint shipped.
# ``download_to_cache`` injects an ``httpx.Client(follow_redirects=
# True)`` so any FTP/CDN redirect lands transparently on disk.
# ---------------------------------------------------------------------------

URL_VERIFIED_DATE: Final[str] = "2026-05-17"
GWAS_STATS_URL: Final[str] = "https://www.ebi.ac.uk/gwas/api/search/stats"
GWAS_ASSOCIATIONS_ZIP_URL: Final[str] = (
    "https://ftp.ebi.ac.uk/pub/databases/gwas/releases/latest/"
    "gwas-catalog-associations_ontology-annotated-full.zip"
)

# Name of the TSV inside the downloaded ZIP. EBI ships exactly one
# entry; if the archive layout changes the parser raises a clear
# error rather than silently picking a different file.
_ZIP_TSV_MEMBER: Final[str] = "gwas-catalog-download-associations-alt-full.tsv"

SOURCE_DB: Final[str] = "gwas_catalog"
_TARGET_TABLE: Final[str] = "gwas_catalog_associations"
_CACHE_FILENAME: Final[str] = "gwas-catalog-associations_ontology-annotated-full.zip"
_STATS_RESOURCE_ID: Final[str] = "gwas_catalog_release_stats"
_DOWNLOAD_RESOURCE_ID: Final[str] = "gwas_catalog_all_associations"

# Chunk size for the streaming bulk insert. Matches the ClinVar loader
# (250K rows per chunk → ~125 MB working set). GWAS Catalog ships
# ~600-700K rows so a full release fits into 2-3 chunks; keeping the
# same constant means the chunked-insert code path is exercised
# identically across loaders and a future calibration touches one
# value in one place.
_CHUNK_SIZE: Final[int] = 250_000

# Tokens GWAS Catalog uses to signal a missing cell. The TSV is
# inconsistent about which sentinel a given column uses -- some
# columns emit empty, others ``NA``, others ``NR`` ("not reported"),
# others the standard dash. We treat all four as missing.
_MISSING_VALUE_TOKENS: Final[frozenset[str]] = frozenset(
    {"", "NA", "NR", "-"},
)

# Tokens that map to an unknown effect allele in the
# ``STRONGEST SNP-RISK ALLELE`` column (typical: ``rs1234-?``).
_UNKNOWN_ALLELE_TOKENS: Final[frozenset[str]] = frozenset({"?", "NR"})

# Detection rule for individual SNPS column entries. GWAS Catalog
# typically encodes one rsID per token; the loader accepts the bare
# digit form (``12345``) and the prefixed form (``rs12345``) for
# robustness across older releases and emits the prefixed form
# downstream.
_RSID_RE: Final[re.Pattern[str]] = re.compile(r"^rs\d+$")
_BARE_RSID_RE: Final[re.Pattern[str]] = re.compile(r"^\d+$")

# 95% CI column shape: ``[lower-upper]`` optionally followed by free
# text (e.g. ``[1.23-1.56] unit decrease``). Captures the two
# floating-point bounds via a non-greedy match; anything that doesn't
# fit returns ``(None, None)``.
_CI_RE: Final[re.Pattern[str]] = re.compile(
    r"\[\s*(?P<lower>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*-\s*"
    r"(?P<upper>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*\]",
)

# Trailing ``-<allele>`` block on the ``STRONGEST SNP-RISK ALLELE``
# column (e.g. ``rs1234-A``). Captures the allele body; ``?`` and
# other unknown sentinels are coerced to NULL downstream.
_STRONGEST_SNP_RE: Final[re.Pattern[str]] = re.compile(
    r"-(?P<allele>[A-Za-z0-9?]+)\s*$",
)

# Leading integer extractor for the ``INITIAL SAMPLE SIZE`` and
# ``REPLICATION SAMPLE SIZE`` columns. GWAS Catalog ships these as
# free-form text (``"4,512 European ancestry individuals"``); we
# pull the first comma-stripped integer and use it as the rough
# numeric size. NULL when no leading integer is present.
_LEADING_INTEGER_RE: Final[re.Pattern[str]] = re.compile(r"^[^\d]*([\d,]+)")

# EFO / MONDO / HP trait-ID extractor. Pulls the terminal
# ``<PREFIX>_<digits>`` token out of an EFO URI (e.g.
# ``http://www.ebi.ac.uk/efo/EFO_0001065`` → ``EFO_0001065``).
_TRAIT_ID_RE: Final[re.Pattern[str]] = re.compile(r"([A-Za-z]+_\d+)\s*$")

# Mapping from the GWAS Catalog TSV header to the columns the loader
# reads. Captured at module scope so the lookup stays declarative and
# a header drift is one place to fix.
_REQUIRED_HEADERS: Final[tuple[str, ...]] = (
    "PUBMEDID",
    "STUDY ACCESSION",
    "SNPS",
    "CHR_ID",
    "CHR_POS",
    "STRONGEST SNP-RISK ALLELE",
    "RISK ALLELE FREQUENCY",
    "P-VALUE",
    "OR or BETA",
    "95% CI (TEXT)",
    "DISEASE/TRAIT",
    "MAPPED_TRAIT",
    "MAPPED_TRAIT_URI",
    "INITIAL SAMPLE SIZE",
    "REPLICATION SAMPLE SIZE",
)

# Arrow schema used by ``_insert_chunk``. Column order matches the
# INSERT column list constructed below; keeping the schema at module
# scope means the structure is reviewable next to the SQL it feeds.
_ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("association_id", pa.int64(), nullable=False),
        pa.field("study_accession", pa.string()),
        pa.field("pmid", pa.string()),
        pa.field("rsid", pa.string(), nullable=False),
        pa.field("chrom", pa.string()),
        pa.field("pos_grch38", pa.int64()),
        pa.field("trait_id", pa.string()),
        pa.field("trait_name", pa.string()),
        pa.field("mapped_trait_uri", pa.string()),
        pa.field("effect_size", pa.float64()),
        pa.field("effect_size_unit", pa.string()),
        pa.field("effect_allele", pa.string()),
        pa.field("other_allele", pa.string()),
        pa.field("effect_allele_freq", pa.float64()),
        pa.field("ci_95_lower", pa.float64()),
        pa.field("ci_95_upper", pa.float64()),
        pa.field("p_value", pa.float64()),
        pa.field("sample_size_initial", pa.int32()),
        pa.field("sample_size_replication", pa.int32()),
        pa.field("ancestry", pa.string()),
        pa.field("is_replicated", pa.bool_()),
        pa.field("source_version_id", pa.int64(), nullable=False),
        pa.field("retrieval_date", pa.timestamp("us"), nullable=False),
    ],
)


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    """One row destined for ``gwas_catalog_associations``.

    Mirrors the destination schema's variable columns.
    ``association_id``, ``source_version_id``, and ``retrieval_date``
    are assigned at bulk-insert time after parsing completes.
    """

    study_accession: str | None
    pmid: str | None
    rsid: str
    chrom: str | None
    pos_grch38: int | None
    trait_id: str | None
    trait_name: str | None
    mapped_trait_uri: str | None
    effect_size: float | None
    effect_size_unit: str | None
    effect_allele: str | None
    other_allele: str | None
    effect_allele_freq: float | None
    ci_95_lower: float | None
    ci_95_upper: float | None
    p_value: float | None
    sample_size_initial: int | None
    sample_size_replication: int | None
    ancestry: str | None
    is_replicated: bool | None


@dataclass(slots=True)
class _ParseStats:
    """Mutable parser-level counters surfaced at end of load.

    Threaded through :func:`_parse_gwas_catalog` so the caller can
    inspect the drop / split counts after streaming completes. Keeps
    the streaming generator surface side-effect-free w.r.t. the
    structlog logger -- the loader logs the stats once at end of load
    rather than per row.
    """

    rows_read: int = 0
    rows_emitted: int = 0
    dropped_empty_pos: int = 0
    dropped_no_valid_snp: int = 0
    multi_snp_expansions: int = 0
    truncated_mapped_trait_uri: int = 0
    extra_rows: dict[str, int] = field(default_factory=dict)


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
    """Coerce a TSV cell to an integer; empty / missing / non-numeric → None."""
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    try:
        return int(trimmed)
    except ValueError:
        return None


def _parse_float(value: str) -> float | None:
    """Coerce a TSV cell to a float; empty / missing / non-numeric → None.

    Accepts the scientific notation forms GWAS Catalog uses for risk
    allele frequency and odds-ratio columns (``"3.4e-2"``,
    ``"1.23E+02"``) via :func:`float`'s native parser.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    try:
        return float(trimmed)
    except ValueError:
        return None


def _parse_p_value(value: str) -> float | None:
    """Parse the ``P-VALUE`` column's scientific-notation float.

    GWAS Catalog ships p-values as e.g. ``"2.0E-9"`` or
    ``"4.5e-12"``. :func:`float` handles both forms natively; the
    helper exists so the call site is self-documenting and the
    fixture / real-data verification have one place to assert
    against.
    """
    return _parse_float(value)


def _parse_ci(value: str) -> tuple[float | None, float | None]:
    """Parse a ``[lower-upper]`` 95% CI string into bounds.

    GWAS Catalog's ``95% CI (TEXT)`` column ships values like
    ``"[1.23-1.56]"`` (numeric) or ``"[1.23-1.56] unit decrease"``
    (numeric plus free-text annotation) or pure free text
    (``"[NR] unit decrease"``). The regex captures the two
    floating-point bounds; non-matching input returns
    ``(None, None)``.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None, None
    match = _CI_RE.search(trimmed)
    if match is None:
        return None, None
    try:
        return float(match.group("lower")), float(match.group("upper"))
    except ValueError:
        return None, None


def _parse_sample_size(value: str) -> int | None:
    """Extract the leading integer from a sample-size text cell.

    GWAS Catalog ships ``INITIAL SAMPLE SIZE`` and
    ``REPLICATION SAMPLE SIZE`` as free-form text (e.g.
    ``"4,512 European ancestry individuals"``); the leading
    comma-grouped integer is the rough numeric size used by the
    schema's ``INTEGER`` columns. NULL when no leading integer is
    present.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    match = _LEADING_INTEGER_RE.match(trimmed)
    if match is None:
        return None
    digits = match.group(1).replace(",", "")
    try:
        return int(digits)
    except ValueError:
        return None


def _derive_is_replicated(value: str) -> bool | None:
    """Coerce ``REPLICATION SAMPLE SIZE`` text to ``is_replicated``.

    Rule: any cell whose leading integer is a positive number maps
    to ``True``; missing / zero / unparseable maps to ``None``
    (not ``False``) -- the column overloads "no replication
    cohort" with "replication cohort exists but size unknown",
    so NULL keeps ``is_replicated IS TRUE`` semantics clean
    downstream.
    """
    n = _parse_sample_size(value)
    if n is None or n <= 0:
        return None
    return True


def _split_snps(value: str) -> list[str]:
    """Split the ``SNPS`` column into individual rsIDs.

    GWAS Catalog uses ``;`` to separate multiple SNPs in a haplotype-
    style multi-SNP association. Each token is stripped, normalized
    to the ``rs<digits>`` form, and dropped if it doesn't match the
    expected shape. Returns the emitted rsIDs in source order; an
    empty list signals "no valid SNP in this row".

    Splits on ``;`` only; commas and ``x`` (the haplotype-
    intersection marker) are deliberately ignored -- those forms
    represent a single combined association rather than independent
    rsID-per-row entries, and the schema's ``rsid VARCHAR NOT NULL``
    contract is per-row so collapsing them to one row would lose
    information. A future sub-phase that needs haplotype-aware
    matching can re-parse the original SNPS string from the source
    snapshot.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return []
    out: list[str] = []
    for token in trimmed.split(";"):
        cleaned = token.strip()
        if not cleaned:
            continue
        if _RSID_RE.match(cleaned):
            out.append(cleaned)
            continue
        if _BARE_RSID_RE.match(cleaned):
            out.append(f"rs{cleaned}")
            continue
        # Reject anything that isn't recognizably an rsID -- the
        # schema's ``rsid VARCHAR NOT NULL`` won't accept star alleles
        # or haplotype text.
    return out


def _parse_effect_allele(strongest_snp_text: str) -> str | None:
    """Pull the effect allele off a ``rsID-allele`` string.

    GWAS Catalog encodes the strongest SNP / risk allele as
    ``rs1234-A`` (rsID, hyphen, allele letter). The loader extracts
    the trailing allele body; ``?`` (allele unknown) and the standard
    missing tokens coerce to NULL.
    """
    trimmed = strongest_snp_text.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None
    match = _STRONGEST_SNP_RE.search(trimmed)
    if match is None:
        return None
    allele = match.group("allele")
    if allele in _UNKNOWN_ALLELE_TOKENS:
        return None
    return allele


def _parse_first_uri(value: str) -> tuple[str | None, bool]:
    """Return ``(first_uri, was_truncated)`` from a comma-list cell.

    GWAS Catalog ships ``MAPPED_TRAIT_URI`` as a single URI in the
    common case but as a comma-separated list when an association is
    mapped to multiple EFO terms (e.g.
    ``"http://www.ebi.ac.uk/efo/EFO_0001065,http://www.ebi.ac.uk/efo/EFO_0009909"``).
    The schema's ``mapped_trait_uri VARCHAR`` is single-valued, so the
    loader keeps the first URI (the curators' primary mapping) and
    reports the truncation count via the returned flag so the loader
    can sum across the stream and surface the total in the end-of-load
    summary.
    """
    trimmed = value.strip()
    if trimmed in _MISSING_VALUE_TOKENS:
        return None, False
    parts = [p.strip() for p in trimmed.split(",") if p.strip()]
    if not parts:
        return None, False
    return parts[0], len(parts) > 1


def _extract_trait_id(mapped_trait_uri: str | None) -> str | None:
    """Pull the EFO / MONDO / HP trait ID out of an EFO URI.

    Input shapes:

    * ``"http://www.ebi.ac.uk/efo/EFO_0001065"`` → ``"EFO_0001065"``
    * ``"http://purl.obolibrary.org/obo/MONDO_0007254"``
      → ``"MONDO_0007254"``
    * ``""`` / ``None`` → ``None``
    """
    if mapped_trait_uri is None:
        return None
    match = _TRAIT_ID_RE.search(mapped_trait_uri)
    if match is None:
        return None
    return match.group(1)


# ---------------------------------------------------------------------------
# ZIP → TSV streaming.
# ---------------------------------------------------------------------------


@contextmanager
def _open_tsv_from_zip(zip_path: Path) -> Iterator[TextIO]:
    """Yield a UTF-8 text handle over the TSV inside a GWAS Catalog ZIP.

    EBI distributes the associations release as a ZIP archive
    carrying exactly one entry
    (:data:`_ZIP_TSV_MEMBER`). The wrapping ZIP layer means the
    downloaded artifact is ~60 MB on disk while the contained TSV
    decompresses to ~300 MB; streaming the entry through
    :mod:`zipfile` keeps the memory footprint bounded the same way
    the prior ``.tsv.gz`` style would have. If the archive shape
    drifts (the TSV is renamed, additional entries appear, or the
    file isn't a ZIP at all) the function raises a clear error so
    the upstream change surfaces fast.
    """
    if not zipfile.is_zipfile(zip_path):
        msg = (
            f"GWAS Catalog cached download {zip_path} is not a ZIP "
            "archive; upstream layout may have changed"
        )
        raise ValueError(msg)
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        if _ZIP_TSV_MEMBER not in names:
            msg = (
                f"GWAS Catalog ZIP {zip_path.name} missing expected "
                f"entry {_ZIP_TSV_MEMBER!r}; archive contains {names!r}"
            )
            raise ValueError(msg)
        with archive.open(_ZIP_TSV_MEMBER) as raw:
            yield io.TextIOWrapper(raw, encoding="utf-8", newline="")


# ---------------------------------------------------------------------------
# Streaming parser.
# ---------------------------------------------------------------------------


def _row_to_parsed_rows(
    raw: dict[str, str],
    stats: _ParseStats,
) -> Iterator[_ParsedRow]:
    """Yield one ``_ParsedRow`` per rsID in a single source row.

    Edge cases handled here (and counted in ``stats``):

    * Empty ``CHR_ID`` or ``CHR_POS`` → drop the entire row (counted
      as ``dropped_empty_pos``). The schema's position-based join
      contract has no use for a coordinate-less association, and
      every other column flows from the same source row so dropping
      one rsID emit isn't preferable to dropping all of them.
    * ``SNPS`` cell with no parseable rsID → drop the entire row
      (``dropped_no_valid_snp``). The schema's ``rsid NOT NULL``
      constraint would reject the emit anyway.
    * ``SNPS`` with two or more ``;``-separated rsIDs → emit one
      row per rsID, all sharing the source row's other columns
      (``multi_snp_expansions`` is incremented once per source row,
      so the counter reads as "how many source rows produced
      multiple emits").
    * ``MAPPED_TRAIT_URI`` with multiple comma-separated URIs → keep
      the first URI; increment ``truncated_mapped_trait_uri``.
    """
    chr_id = _empty_to_none(raw.get("CHR_ID", ""))
    chr_pos_int = _parse_int(raw.get("CHR_POS", ""))
    if chr_id is None or chr_pos_int is None:
        stats.dropped_empty_pos += 1
        return

    rsids = _split_snps(raw.get("SNPS", ""))
    if not rsids:
        stats.dropped_no_valid_snp += 1
        return

    if len(rsids) > 1:
        stats.multi_snp_expansions += 1

    first_uri, truncated = _parse_first_uri(raw.get("MAPPED_TRAIT_URI", ""))
    if truncated:
        stats.truncated_mapped_trait_uri += 1

    trait_id = _extract_trait_id(first_uri)
    trait_name = _empty_to_none(raw.get("MAPPED_TRAIT", "")) or _empty_to_none(
        raw.get("DISEASE/TRAIT", ""),
    )
    effect_allele = _parse_effect_allele(raw.get("STRONGEST SNP-RISK ALLELE", ""))
    effect_allele_freq = _parse_float(raw.get("RISK ALLELE FREQUENCY", ""))
    p_value = _parse_p_value(raw.get("P-VALUE", ""))
    effect_size = _parse_float(raw.get("OR or BETA", ""))
    ci_lower, ci_upper = _parse_ci(raw.get("95% CI (TEXT)", ""))
    sample_size_initial = _parse_sample_size(raw.get("INITIAL SAMPLE SIZE", ""))
    sample_size_replication = _parse_sample_size(
        raw.get("REPLICATION SAMPLE SIZE", ""),
    )
    is_replicated = _derive_is_replicated(raw.get("REPLICATION SAMPLE SIZE", ""))
    pmid = _empty_to_none(raw.get("PUBMEDID", ""))
    study_accession = _empty_to_none(raw.get("STUDY ACCESSION", ""))
    chrom = normalize_chrom(chr_id)

    # ``effect_size_unit`` and ``ancestry`` are intentionally NULL
    # for first-version 5.3. The "OR or BETA" column does not
    # disambiguate which it is at the row level (the unit hint
    # sometimes lives in the "95% CI (TEXT)" free text but parsing
    # that is brittle), and ancestry data lives in a separate GWAS
    # Catalog ancestry TSV that this loader does not consume. Both
    # are surfaceable in a follow-up sub-phase without a schema
    # change.
    effect_size_unit: str | None = None
    ancestry: str | None = None

    for rsid in rsids:
        yield _ParsedRow(
            study_accession=study_accession,
            pmid=pmid,
            rsid=rsid,
            chrom=chrom,
            pos_grch38=chr_pos_int,
            trait_id=trait_id,
            trait_name=trait_name,
            mapped_trait_uri=first_uri,
            effect_size=effect_size,
            effect_size_unit=effect_size_unit,
            effect_allele=effect_allele,
            other_allele=None,
            effect_allele_freq=effect_allele_freq,
            ci_95_lower=ci_lower,
            ci_95_upper=ci_upper,
            p_value=p_value,
            sample_size_initial=sample_size_initial,
            sample_size_replication=sample_size_replication,
            ancestry=ancestry,
            is_replicated=is_replicated,
        )


def _parse_gwas_catalog(
    text_io: TextIO,
    stats: _ParseStats,
) -> Iterator[_ParsedRow]:
    """Stream rows from the GWAS Catalog "all associations" TSV.

    Yields one ``_ParsedRow`` per emitted (study, rsID) tuple --
    multi-SNP source rows fan out into multiple emits per the
    contract documented on :func:`_row_to_parsed_rows`. The streaming
    pattern matches the ClinVar loader: no row-level filtering beyond
    the schema-driven drops (empty CHR_POS, invalid SNPS) so the
    full active corpus lands and downstream filters (p-value cutoff,
    trait selection) become query concerns.

    Raises :class:`ValueError` if any column in
    :data:`_REQUIRED_HEADERS` is missing -- the upstream contract has
    shifted and the loader can't produce a correct mapping; loud-fail
    is preferable to a silent column drop.
    """
    reader = csv.DictReader(text_io, delimiter="\t")
    if reader.fieldnames is None:
        msg = "GWAS Catalog associations TSV has no header row"
        raise ValueError(msg)
    missing = [h for h in _REQUIRED_HEADERS if h not in reader.fieldnames]
    if missing:
        msg = (
            f"GWAS Catalog associations TSV is missing expected columns "
            f"{missing!r}; got {list(reader.fieldnames)!r}"
        )
        raise ValueError(msg)
    for raw in reader:
        stats.rows_read += 1
        for parsed in _row_to_parsed_rows(raw, stats):
            stats.rows_emitted += 1
            yield parsed


# ---------------------------------------------------------------------------
# Version resolution.
# ---------------------------------------------------------------------------


# Matches a calendar date in either ``YYYY-MM-DD`` (stats-endpoint
# native form) or ``YYYY/MM/DD`` (defensive against a future shape
# tweak). Captured groups expose year / month / day for the
# ``YYYY_MM_DD`` render.
_STATS_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<year>\d{4})[-/](?P<month>\d{2})[-/](?P<day>\d{2})$",
)


def _parse_stats_release_date(payload: object) -> date:
    """Pull the release date out of a parsed stats-endpoint payload.

    Strict on shape: the payload must be a mapping that carries a
    ``date`` key (the live shape the EBI stats endpoint returns) or
    a ``releasedate`` key (defensive against a documented historical
    form). The value must match :data:`_STATS_DATE_RE`. Any drift
    raises a clear :class:`ValueError` so a future EBI schema change
    surfaces as a loud refresh failure rather than a silent stale
    version label.
    """
    if not isinstance(payload, dict):
        msg = f"GWAS Catalog stats response was {type(payload).__name__}, expected a JSON object"
        # ValueError (not TypeError) keeps the per-shape failures
        # under one exception class so callers and tests don't have
        # to discriminate "wrong key" from "wrong type". The runbook
        # references ValueError in the troubleshooting section.
        raise ValueError(msg)  # noqa: TRY004 — shape error, not type error
    raw = payload.get("date") or payload.get("releasedate")
    if not isinstance(raw, str) or not raw:
        msg = (
            "GWAS Catalog stats response is missing a 'date' / "
            f"'releasedate' string field; got keys {sorted(payload)!r}"
        )
        raise ValueError(msg)
    match = _STATS_DATE_RE.match(raw.strip())
    if match is None:
        msg = (
            f"GWAS Catalog stats date {raw!r} does not match YYYY-MM-DD; upstream shape has drifted"
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


def _resolve_version_via_stats() -> str:
    """Resolve the GWAS Catalog version label via the stats endpoint.

    Issues an audited GET to :data:`GWAS_STATS_URL`, parses the JSON
    body, extracts the release date via
    :func:`_parse_stats_release_date`, and renders it as
    ``YYYY_MM_DD``.

    Failure modes:

    * :class:`ExternalCallsDisabledError` propagates -- callers see
      the audited refusal directly. The privacy gate is fail-closed;
      we do not paper over it with a fallback.
    * Any other :class:`ExternalCallError` (network, HTTP 4xx/5xx)
      raises through. A failed version resolution must NOT silently
      fall back to "today" -- that would either cause a duplicate
      load against the previously-current release or paint a
      misleading version label onto a release that's actually
      identical. Better to fail loudly and let the operator retry.
    * Malformed JSON or a missing ``date`` field raises
      :class:`ValueError` with the live payload shape, so a future
      upstream API change surfaces as a fast diagnostic rather than
      a silent bad write.

    The stats GET is the loader's first audited call. Placing it
    before the download means a fresh refresh against an unchanged
    release short-circuits before re-fetching the ~60 MB ZIP body.
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
            GWAS_STATS_URL,
            resource_type="annotation_source",
            resource_id=_STATS_RESOURCE_ID,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        msg = f"GWAS Catalog stats response is not valid JSON: {response.text[:200]}"
        raise ValueError(msg) from exc

    release_date = _parse_stats_release_date(payload)
    return _format_version(release_date)


# ---------------------------------------------------------------------------
# Chunked bulk insert.
# ---------------------------------------------------------------------------


def _next_association_id(conn: DuckDBPyConnection) -> int:
    """``COALESCE(MAX(association_id), 0) + 1``.

    Mirrors the project-wide app-allocated BIGINT PK pattern
    (:func:`genome.annotate.loaders.clinvar._next_clinvar_id`,
    :func:`genome.annotate.loaders.pharmgkb._next_pharmgkb_id`).
    Called once at the start of streaming; per-chunk base IDs are
    advanced from the previous chunk's actual size.
    """
    row = conn.execute(
        f"SELECT COALESCE(MAX(association_id), 0) FROM {_TARGET_TABLE}",  # noqa: S608
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
    iterations. Mirrors the ClinVar helper so the chunking logic
    stays unit-testable without a DuckDB connection in hand.
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
    """Insert one chunk of ``_ParsedRow`` into ``gwas_catalog_associations``.

    Builds a PyArrow Table with one column per destination column
    (including ``association_id``, ``source_version_id``, and
    ``retrieval_date``), registers it under a temp name, then issues
    ``INSERT INTO gwas_catalog_associations (...) SELECT ... FROM
    <temp>`` and unregisters. ``chrom`` is cast through
    ``chromosome_enum`` in the SELECT so the NULLs that are correct
    for non-canonical or missing chromosomes reach the enum-typed
    column cleanly.

    Returns the number of rows inserted (== ``len(rows)``). A
    zero-row call inserts nothing and returns 0.
    """
    if not rows:
        return 0

    n = len(rows)
    # Naive UTC datetime: pa.timestamp("us") (no tz) lines up with
    # DuckDB's TIMESTAMP (no tz). Same convention as the PharmGKB,
    # CPIC, and ClinVar loaders.
    naive_retrieval = retrieval_date.astimezone(UTC).replace(tzinfo=None)
    table = pa.table(
        {
            "association_id": pa.array(range(base_id, base_id + n), type=pa.int64()),
            "study_accession": pa.array(
                [r.study_accession for r in rows],
                type=pa.string(),
            ),
            "pmid": pa.array([r.pmid for r in rows], type=pa.string()),
            "rsid": pa.array([r.rsid for r in rows], type=pa.string()),
            "chrom": pa.array([r.chrom for r in rows], type=pa.string()),
            "pos_grch38": pa.array([r.pos_grch38 for r in rows], type=pa.int64()),
            "trait_id": pa.array([r.trait_id for r in rows], type=pa.string()),
            "trait_name": pa.array([r.trait_name for r in rows], type=pa.string()),
            "mapped_trait_uri": pa.array(
                [r.mapped_trait_uri for r in rows],
                type=pa.string(),
            ),
            "effect_size": pa.array([r.effect_size for r in rows], type=pa.float64()),
            "effect_size_unit": pa.array(
                [r.effect_size_unit for r in rows],
                type=pa.string(),
            ),
            "effect_allele": pa.array(
                [r.effect_allele for r in rows],
                type=pa.string(),
            ),
            "other_allele": pa.array(
                [r.other_allele for r in rows],
                type=pa.string(),
            ),
            "effect_allele_freq": pa.array(
                [r.effect_allele_freq for r in rows],
                type=pa.float64(),
            ),
            "ci_95_lower": pa.array([r.ci_95_lower for r in rows], type=pa.float64()),
            "ci_95_upper": pa.array([r.ci_95_upper for r in rows], type=pa.float64()),
            "p_value": pa.array([r.p_value for r in rows], type=pa.float64()),
            "sample_size_initial": pa.array(
                [r.sample_size_initial for r in rows],
                type=pa.int32(),
            ),
            "sample_size_replication": pa.array(
                [r.sample_size_replication for r in rows],
                type=pa.int32(),
            ),
            "ancestry": pa.array([r.ancestry for r in rows], type=pa.string()),
            "is_replicated": pa.array(
                [r.is_replicated for r in rows],
                type=pa.bool_(),
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
        conn.register("_gwas_stage_arrow", table)
        conn.execute(
            f"""
            INSERT INTO {_TARGET_TABLE} (
                association_id, study_accession, pmid,
                rsid, chrom, pos_grch38,
                trait_id, trait_name, mapped_trait_uri,
                effect_size, effect_size_unit, effect_allele, other_allele,
                effect_allele_freq, ci_95_lower, ci_95_upper, p_value,
                sample_size_initial, sample_size_replication,
                ancestry, is_replicated,
                source_version_id, retrieval_date
            )
            SELECT
                association_id, study_accession, pmid,
                rsid, chrom::chromosome_enum, pos_grch38,
                trait_id, trait_name, mapped_trait_uri,
                effect_size, effect_size_unit, effect_allele, other_allele,
                effect_allele_freq, ci_95_lower, ci_95_upper, p_value,
                sample_size_initial, sample_size_replication,
                ancestry, is_replicated,
                source_version_id, retrieval_date
              FROM _gwas_stage_arrow
            """,  # noqa: S608 — table name is a module constant, not user input
        )
    finally:
        conn.unregister("_gwas_stage_arrow")
    return n


def _stream_bulk_insert(
    conn: DuckDBPyConnection,
    rows_iter: Iterable[_ParsedRow],
    *,
    source_version_id: int,
    retrieval_date: datetime,
    chunk_size: int = _CHUNK_SIZE,
) -> int:
    """Drain ``rows_iter`` into ``gwas_catalog_associations`` in chunks.

    Each chunk is a separate :func:`_insert_chunk` call (PyArrow
    Table registration + ``INSERT ... SELECT``). All chunks must run
    inside the same DuckDB transaction -- the caller bracket-controls
    ``conn.begin()`` / ``conn.commit()``. Chunks are deliberately
    *not* committed individually: a mid-stream failure must roll
    back the deactivation of prior active rows along with the
    partial insert, or the supersession-over-update invariant is
    broken.

    Per-chunk progress is logged at INFO with the chunk index, the
    row count, and the cumulative total. Returns the total number of
    rows inserted.
    """
    base_id = _next_association_id(conn)
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
            "gwas_catalog.bulk_insert.chunk",
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

    Same shape as the PharmGKB / CPIC / ClinVar helpers -- called
    when the supersede + chunked-insert transaction rolls back so
    the version row that :func:`insert_source_version` committed in
    its own transaction doesn't leave a dangling "version exists
    but zero rows referenced" state. The DELETE is FK-safe because
    no ``gwas_catalog_associations`` rows reference the new
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
            "gwas_catalog.cleanup.orphan_version_row_delete_failed",
            source_version_id=source_version_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Post-load summary (drift identifiers).
# ---------------------------------------------------------------------------


def _summarize_active(conn: DuckDBPyConnection) -> dict[str, object]:
    """Compute the drift identifiers logged at end-of-load.

    Returns the durable signals real-data verification will compare
    across releases:

    * ``active_total`` -- count of rows under the currently-active version
    * ``distinct_study_accession`` -- ``COUNT(DISTINCT study_accession)``
    * ``distinct_pmid`` -- ``COUNT(DISTINCT pmid)``
    * ``distinct_rsid`` -- ``COUNT(DISTINCT rsid)``
    * ``distinct_trait_name`` -- ``COUNT(DISTINCT trait_name)``

    Counts rows whose ``source_version_id`` matches the
    ``annotation_sources`` pointer for ``gwas_catalog``. Run after the
    supersession transaction commits so the pointer already names the
    new version.
    """
    total_row = conn.execute(
        f"SELECT COUNT(*) FROM {_TARGET_TABLE} g "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'gwas_catalog' AND s.current_source_version_id = g.source_version_id",
    ).fetchone()
    distinct_study_row = conn.execute(
        f"SELECT COUNT(DISTINCT g.study_accession) FROM {_TARGET_TABLE} g "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'gwas_catalog' AND s.current_source_version_id = g.source_version_id "
        "WHERE g.study_accession IS NOT NULL",
    ).fetchone()
    distinct_pmid_row = conn.execute(
        f"SELECT COUNT(DISTINCT g.pmid) FROM {_TARGET_TABLE} g "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'gwas_catalog' AND s.current_source_version_id = g.source_version_id "
        "WHERE g.pmid IS NOT NULL",
    ).fetchone()
    distinct_rsid_row = conn.execute(
        f"SELECT COUNT(DISTINCT g.rsid) FROM {_TARGET_TABLE} g "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'gwas_catalog' AND s.current_source_version_id = g.source_version_id",
    ).fetchone()
    distinct_trait_row = conn.execute(
        f"SELECT COUNT(DISTINCT g.trait_name) FROM {_TARGET_TABLE} g "  # noqa: S608
        "JOIN annotation_sources s "
        "ON s.source_db = 'gwas_catalog' AND s.current_source_version_id = g.source_version_id "
        "WHERE g.trait_name IS NOT NULL",
    ).fetchone()
    return {
        "active_total": int(total_row[0]) if total_row is not None else 0,
        "distinct_study_accession": int(distinct_study_row[0])
        if distinct_study_row is not None
        else 0,
        "distinct_pmid": int(distinct_pmid_row[0]) if distinct_pmid_row is not None else 0,
        "distinct_rsid": int(distinct_rsid_row[0]) if distinct_rsid_row is not None else 0,
        "distinct_trait_name": int(distinct_trait_row[0]) if distinct_trait_row is not None else 0,
    }


# ---------------------------------------------------------------------------
# Module entry point — refresh
# ---------------------------------------------------------------------------


def refresh(
    force: bool,  # noqa: FBT001 — positional matches registry's RefreshFn signature
    skip_if_same_version: bool = False,  # noqa: FBT001, FBT002 — opt-in default for the new flag
) -> RefreshResult:
    """Refresh GWAS Catalog associations.

    Pipeline:

    1. Resolve version via the GWAS Catalog REST stats endpoint
       (audited). The endpoint returns
       ``{"date": "YYYY-MM-DD", ...}``; the date is rendered as
       ``YYYY_MM_DD`` and is the version label.
    2. Short-circuit and return ``was_already_current=True`` if a row
       in ``annotation_source_versions`` already names the resolved
       ``(source_db='gwas_catalog', version)`` and ``force`` is
       ``False``. This is what makes a re-run against an unchanged
       GWAS Catalog release cheap: no download, no parse, no insert.
    3. Download the dated "all associations" ZIP from the EBI FTP
       ``latest/`` directory via the audited
       :func:`genome.annotate.downloads.download_to_cache`
       (skip-if-exists by default; ``force=True`` re-downloads).
    3a. **Hash-based fallback short-circuit (finding-014).** If
        ``force`` is ``False`` and the downloaded ZIP's SHA-256 matches
        the currently-active version row's recorded hash but the
        resolved upstream label differs, fire
        ``gwas_catalog.skip_content_unchanged`` and return
        ``was_already_current=True`` against the active row. The EBI
        stats endpoint has been observed to ship a different ``date``
        field for byte-identical content (a backwards drift from
        ``2026_05_16`` to ``2026_04_27`` on the same 4717ff06… ZIP),
        which made label-only identity unstable for this source. Hash
        is the stable identity; the fallback prevents an upstream
        label change from minting a no-op supersession. ``--force``
        bypasses this fallback the same way it bypasses the Step-2
        label-based short-circuit.
    3b. If ``skip_if_same_version`` is ``True`` and the downloaded
        ZIP's (version, sha256) match the currently-active row,
        short-circuit via :func:`maybe_skip_same_version` (finding-009
        #14). Off by default. Distinct from 3a because 3a fires on
        hash-match-with-label-drift; 3b fires on both-match and is
        gated on the opt-in flag.
    4. Inside one DuckDB transaction: upsert
       ``annotation_source_versions``, open the ZIP and stream-parse
       the contained TSV, chunk-insert at :data:`_CHUNK_SIZE` rows per
       chunk via :func:`_stream_bulk_insert`, update the version row's
       ``record_count`` once the streaming completes, and flip the
       ``annotation_sources`` pointer for ``gwas_catalog`` to the new
       ``source_version_id`` via :func:`flip_to_new_version`. The
       pointer flip is the supersession event; the prior set stays in
       the table indefinitely under its older ``source_version_id``.
       The supersession transaction is closed via
       :func:`commit_and_checkpoint` so the COMMIT + explicit
       CHECKPOINT phases are observable in the structlog stream
       (finding-009 #9 and #11).
    5. Open a fresh read-only connection and emit a structlog summary
       line with the locked drift identifiers (active row total,
       distinct study_accession, distinct pmid, distinct rsid,
       distinct trait_name) plus the parser stats (rows read /
       emitted / dropped / multi-SNP-expansions / truncated trait
       URIs).
    6. Return a :class:`RefreshResult` describing what landed.
    """
    log = logger.bind(source=SOURCE_DB)

    # 1. Resolve version via the stats endpoint.
    # ExternalCallsDisabledError propagates.
    version = _resolve_version_via_stats()
    log.info("gwas_catalog.version.resolved", version=version)

    # 2. Idempotence check -- short-circuit before downloading the body.
    with duckdb_connection() as conn:
        current = get_current_version(conn, SOURCE_DB)
        if current is not None and current.version == version and not force:
            log.info("gwas_catalog.skip_already_current", version=version)
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
        GWAS_ASSOCIATIONS_ZIP_URL,
        _CACHE_FILENAME,
        resource_id=_DOWNLOAD_RESOURCE_ID,
        force=force,
    )
    log.info(
        "gwas_catalog.download.audited",
        sha256=download_result.sha256[:16],
        size_bytes=download_result.size_bytes,
    )

    # 3a. Hash-based fallback short-circuit (finding-014). EBI's stats
    # endpoint has been observed to return a different `date` field for
    # byte-identical release ZIPs (label `2026_05_16` → `2026_04_27` on
    # the same 4717ff06... hash). When force=False and the downloaded
    # ZIP's SHA-256 matches the active version's recorded hash, treat
    # the refresh as a no-op even though the resolved label differs.
    # `--force` bypasses this fallback so the operator retains an
    # escape hatch when EBI ships genuinely new content under a
    # repeated hash (vanishingly unlikely, but the flag is still
    # respected).
    if not force:
        with duckdb_connection() as conn:
            current_pointer = get_current_version(conn, SOURCE_DB)
        if (
            current_pointer is not None
            and current_pointer.source_file_hash == download_result.sha256
            and current_pointer.version != version
        ):
            log.info(
                "gwas_catalog.skip_content_unchanged",
                resolved_version=version,
                active_source_version_id=current_pointer.source_version_id,
                active_version=current_pointer.version,
                active_source_file_hash=current_pointer.source_file_hash,
                new_source_file_hash=download_result.sha256,
                reason="upstream label drifted, content identical",
            )
            return RefreshResult(
                source_db=SOURCE_DB,
                source_version_id=current_pointer.source_version_id,
                version=current_pointer.version,
                record_count=current_pointer.record_count or 0,
                was_already_current=True,
            )

    # 3b. --skip-if-same-version short-circuit (finding-009 #14). Off by
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

    # 4. Single-transaction load. Same shape as ClinVar: the version
    # row insert runs in autocommit, then a second transaction wraps
    # the chunked insert + pointer flip pair atomically. The flip
    # runs after the INSERT so ``flip_to_new_version`` can count the
    # just-inserted rows for the event payload.
    started = time.monotonic()
    retrieval_date = datetime.now(UTC)
    stats = _ParseStats()
    flip: VersionFlipResult | None = None
    with duckdb_connection() as conn:
        source_version_id = insert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=version,
            source_url=GWAS_ASSOCIATIONS_ZIP_URL,
            source_file_hash=download_result.sha256,
            source_file_size=download_result.size_bytes,
            record_count=None,
        )
        conn.begin()
        try:
            with _open_tsv_from_zip(download_result.path) as fh:
                inserted = _stream_bulk_insert(
                    conn,
                    _parse_gwas_catalog(fh, stats),
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
        "gwas_catalog.refresh.complete",
        version=version,
        sha256=download_result.sha256[:16],
        size_bytes=download_result.size_bytes,
        inserted=inserted,
        prior_version_id=flip.prior_version_id,
        prior_row_count=flip.prior_row_count,
        source_version_id=source_version_id,
        elapsed_seconds=round(elapsed, 1),
        rows_read=stats.rows_read,
        rows_emitted=stats.rows_emitted,
        dropped_empty_pos=stats.dropped_empty_pos,
        dropped_no_valid_snp=stats.dropped_no_valid_snp,
        multi_snp_expansions=stats.multi_snp_expansions,
        truncated_mapped_trait_uri=stats.truncated_mapped_trait_uri,
        active_total=summary["active_total"],
        distinct_study_accession=summary["distinct_study_accession"],
        distinct_pmid=summary["distinct_pmid"],
        distinct_rsid=summary["distinct_rsid"],
        distinct_trait_name=summary["distinct_trait_name"],
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
    "GWAS_ASSOCIATIONS_ZIP_URL",
    "GWAS_STATS_URL",
    "SOURCE_DB",
    "URL_VERIFIED_DATE",
    "refresh",
]
