"""CPIC clinical guidelines loader.

Downloads CPIC's four PostgREST endpoints (/guideline, /pair,
/recommendation, /drug), joins them client-side into
(gene x drug x phenotype) tuples, and bulk-loads them into
``cpic_guidelines`` via PyArrow Table registration + ``INSERT ...
SELECT`` (the project's locked bulk-load convention).

This is the second loader in Phase 5 and mirrors
:mod:`genome.annotate.loaders.pharmgkb` line-for-line in shape:

* Module-level URL constants with a sibling ``URL_VERIFIED_DATE`` so a
  future reader can tell at a glance how stale the link is.
* A ``_resolve_version`` step that probes a small CPIC-side metadata
  endpoint (``/change_log?order=date.desc&limit=1&select=date``) so the
  version label is keyed to the upstream data and the loader is
  idempotent across re-runs against the same snapshot.
* A ``refresh(force)`` function that downloads (skip-if-exists),
  short-circuits when ``annotation_source_versions`` already names the
  resolved version, and otherwise upserts + deactivates + bulk-inserts
  inside one DuckDB transaction.
* ``register_loader(SOURCE_DB, refresh)`` at module-import time so the
  CLI registry is populated by importing this module.

CPIC's tables are small (~thousands of rows), but the join is what
matters: a recommendation's ``lookupkey`` is a multi-gene dict, and we
split one recommendation into one row per gene. All rows split from a
single recommendation share a ``cpic_id`` (the recommendation's primary
key) and differ only in ``gene_symbol`` and ``phenotype``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final

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

if TYPE_CHECKING:
    from pathlib import Path

    from duckdb import DuckDBPyConnection

    from genome.annotate.downloads import DownloadResult

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Upstream URLs (verified 2026-05-15).
#
# CPIC publishes its data via a PostgREST API at ``api.cpicpgx.org``.
# Every endpoint returns the full resource as JSON in a single response
# (no pagination needed at the table sizes shipped today -- ``/pair`` is
# ~hundreds of rows, ``/recommendation`` is ~thousands). The scaffold's
# ``download_to_cache`` injects an ``httpx.Client(follow_redirects=True)``
# so any 303s the upstream issues land transparently and the loader can
# rely on the canonical URLs verbatim.
# ---------------------------------------------------------------------------

URL_VERIFIED_DATE: Final[str] = "2026-05-15"
GUIDELINE_URL: Final[str] = "https://api.cpicpgx.org/v1/guideline"
PAIR_URL: Final[str] = "https://api.cpicpgx.org/v1/pair"
RECOMMENDATION_URL: Final[str] = "https://api.cpicpgx.org/v1/recommendation"
DRUG_URL: Final[str] = "https://api.cpicpgx.org/v1/drug"

# Metadata canary used by :func:`_resolve_version` to label the snapshot
# with CPIC's own last-update date. The query is tiny (one row, one
# column), so even with ``force=True`` the cost is negligible and the
# returned date stays a deterministic function of CPIC's current data
# release.
CHANGE_LOG_LATEST_URL: Final[str] = (
    "https://api.cpicpgx.org/v1/change_log?order=date.desc&limit=1&select=date"
)

SOURCE_DB: Final[str] = "cpic"
_TARGET_TABLE: Final[str] = "cpic_guidelines"

# Endpoint name -> (URL, cached filename). The mapping is captured at
# module scope so the four downloads, their cache filenames, and their
# audit-log ``resource_id`` labels stay reviewable in one place.
_ENDPOINTS: Final[dict[str, tuple[str, str]]] = {
    "guideline": (GUIDELINE_URL, "guideline.json"),
    "pair": (PAIR_URL, "pair.json"),
    "recommendation": (RECOMMENDATION_URL, "recommendation.json"),
    "drug": (DRUG_URL, "drug.json"),
}

# Pediatric mapping. CPIC's recommendation rows carry a ``population``
# string that overloads two axes -- age (e.g. ``'pediatrics'``,
# ``'adults'``) and condition (e.g. ``'PHT naive'``, ``'CVI ACS PCI'``).
# We map the age axis strictly: only an exact ``'pediatrics'`` value
# sets ``pediatric=True``; everything else (``'general'``, condition
# strings, the explicitly ``'adults'`` row) lands as ``None`` so the
# field stays a true positive flag and downstream filters can rely on
# ``pediatric IS TRUE`` semantics.
_PEDIATRIC_POPULATION: Final[str] = "pediatrics"

# Arrow schema used by ``_bulk_insert``. Column order matches the INSERT
# column list constructed below; keeping the schema at module scope means
# the structure is reviewable next to the SQL it feeds.
_ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("guideline_id", pa.int64(), nullable=False),
        pa.field("cpic_id", pa.string()),
        pa.field("gene_symbol", pa.string(), nullable=False),
        pa.field("drug_name", pa.string(), nullable=False),
        pa.field("drug_rxnorm_id", pa.string()),
        pa.field("phenotype", pa.string()),
        pa.field("recommendation", pa.string()),
        pa.field("classification_strength", pa.string()),
        pa.field("cpic_level", pa.string()),
        pa.field("pediatric", pa.bool_()),
        pa.field("guideline_url", pa.string()),
        pa.field("publication_pmid", pa.string()),
        pa.field("last_updated", pa.date32()),
        pa.field("source_version_id", pa.int64(), nullable=False),
        pa.field("retrieval_date", pa.timestamp("us"), nullable=False),
        pa.field("is_active", pa.bool_(), nullable=False),
    ],
)


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    """One row destined for ``cpic_guidelines``.

    Mirrors the destination schema's variable columns. ``guideline_id``,
    ``source_version_id``, ``retrieval_date``, and ``is_active`` are
    assigned at bulk-insert time after parsing completes.

    ``gene_symbol`` and ``drug_name`` are required by the schema (NOT
    NULL); every other column is nullable. ``phenotype`` is technically
    nullable in the schema but the loader drops recommendations without
    a parseable lookupkey at parse time, so the column is always a
    non-empty string in practice.
    """

    cpic_id: str | None
    gene_symbol: str
    drug_name: str
    drug_rxnorm_id: str | None
    phenotype: str
    recommendation: str | None
    classification_strength: str | None
    cpic_level: str | None
    pediatric: bool | None
    guideline_url: str | None
    publication_pmid: str | None
    last_updated: date | None


def _download_all_endpoints(*, force: bool) -> dict[str, DownloadResult]:
    """Download the four CPIC data endpoints to the on-disk cache.

    One :func:`download_to_cache` call per endpoint, each audited under
    its own ``resource_id`` so audit-log queries can drill into a single
    endpoint. ``force`` is passed verbatim so ``--force`` re-downloads
    every file (and the cache-hit fast path applies independently per
    endpoint when not forced).
    """
    out: dict[str, DownloadResult] = {}
    for endpoint, (url, filename) in _ENDPOINTS.items():
        out[endpoint] = download_to_cache(
            SOURCE_DB,
            url,
            filename,
            resource_id=endpoint,
            force=force,
        )
    return out


def _resolve_version(*, force: bool) -> str:
    """Determine the CPIC version label.

    Strategy:

    1. Pull the single most-recent row from ``/change_log`` via
       :func:`download_to_cache` (``?order=date.desc&limit=1&select=date``).
       CPIC writes an entry into ``change_log`` on every data update,
       so the latest entry's date is the closest thing CPIC exposes to
       a "release date".
    2. If the response is the expected one-element list with a ``date``
       field, reformat the date as ``YYYY_MM_DD`` to match the schema
       doc's suggested label shape.
    3. Otherwise fall back to today's UTC date in the same format and
       log loudly so the fallback path is visible at INFO. ``force`` is
       passed to the download helper so a forced refresh re-fetches the
       canary too.

    The canary is downloaded into ``change_log_latest.json`` in the
    source cache directory; it persists across runs so an idempotent
    re-run hits the local file instead of the network.
    """
    try:
        canary = download_to_cache(
            SOURCE_DB,
            CHANGE_LOG_LATEST_URL,
            "change_log_latest.json",
            resource_id="change_log_latest",
            force=force,
        )
        text = canary.path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, ValueError) as exc:
        logger.warning(
            "cpic.version.canary_fetch_failed",
            error=str(exc),
        )
        payload = None

    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        date_field = payload[0].get("date")
        if isinstance(date_field, str):
            # CPIC dates are ISO-8601 ``YYYY-MM-DD`` strings. Slice the first
            # 10 chars defensively in case the upstream ever returns a full
            # datetime (it doesn't today, but the slice is cheap insurance).
            return date_field[:10].replace("-", "_")

    today = datetime.now(UTC).strftime("%Y_%m_%d")
    logger.info(
        "cpic.version.no_metadata_fallback",
        fallback=today,
    )
    return today


def _parse_lookupkey(lookupkey: object) -> list[tuple[str, str]]:
    """Parse CPIC's ``lookupkey`` field into ``[(gene, phenotype), ...]``.

    Real CPIC data ships ``lookupkey`` as a JSON object mapping gene
    symbol to a phenotype string (e.g.
    ``{"CYP2C9": "Poor Metabolizer", "VKORC1": "rs9923231 variant"}``).
    Multi-gene rows split into one ``(gene, phenotype)`` tuple per key;
    single-gene rows return a one-element list.

    The function is intentionally defensive:

    * Non-dict input (``None``, ``str``, ``list``) returns ``[]``.
    * Empty dict returns ``[]``.
    * Items with empty/missing phenotype string are dropped silently;
      the caller's row-level skip-and-log handles the all-empty case.

    Returned tuples preserve insertion order so the split-row sequence
    is stable across runs (Python 3.7+ dict iteration order is
    insertion order).
    """
    if not isinstance(lookupkey, dict) or not lookupkey:
        return []
    parsed: list[tuple[str, str]] = []
    for gene, phenotype in lookupkey.items():
        if not isinstance(gene, str) or not gene:
            continue
        if not isinstance(phenotype, str) or not phenotype:
            continue
        parsed.append((gene, phenotype))
    return parsed


def _first_pmid(citations: object) -> str | None:
    """Return the first PMID string from a ``pair.citations`` list.

    CPIC's pair rows carry an array of PMID strings (e.g.
    ``["32189324"]``, sometimes empty). We surface only the first
    PMID into ``publication_pmid`` because the destination schema has
    one VARCHAR column; the typical case is one PMID per pair, and
    the few multi-citation rows treat the first PMID as the canonical
    guideline publication.

    Non-list / empty / non-string-element inputs return ``None``.
    """
    if not isinstance(citations, list) or not citations:
        return None
    head = citations[0]
    if not isinstance(head, str) or not head:
        return None
    return head


def _pediatric_flag(population: object) -> bool | None:
    """Map a ``recommendation.population`` value to the ``pediatric`` flag.

    CPIC's ``population`` column is overloaded: it carries both age
    cohorts (``'pediatrics'``, ``'adults'``) and condition labels
    (``'PHT naive'``, ``'CVI ACS PCI'``). To keep ``pediatric=True``
    a reliable positive flag, we set it strictly: True iff the value
    is exactly ``'pediatrics'``; otherwise None.

    This deliberately maps the ``'adults'`` rows to None rather than
    False -- "not pediatric" is ambiguous when the same guideline has
    both pediatric and general-population rows, and the None semantics
    let downstream queries ask ``pediatric IS TRUE`` without false
    negatives leaking in.
    """
    if isinstance(population, str) and population == _PEDIATRIC_POPULATION:
        return True
    return None


@dataclass(frozen=True, slots=True)
class _RecBase:
    """Per-recommendation shared fields, post drug/guideline lookup.

    Captures the fields a recommendation contributes to every row split
    out of its ``lookupkey``: the cpic_id, drug, guideline url, and the
    recommendation text + classification + pediatric flag. Splitting
    these out keeps :func:`_build_rows` short enough to stay under the
    ``too-many-statements`` lint and makes the per-gene loop trivially
    a ``map`` over the resolved base.
    """

    cpic_id: str | None
    drug_name: str
    drug_rxnorm_id: str | None
    recommendation: str | None
    classification: str | None
    pediatric: bool | None
    guideline_url: str | None


def _str_or_none(value: object) -> str | None:
    """Return ``value`` if it's a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


def _build_indexes(
    *,
    drugs: list[dict[str, object]],
    guidelines: list[dict[str, object]],
    pairs: list[dict[str, object]],
) -> tuple[
    dict[str, dict[str, object]],
    dict[int, dict[str, object]],
    dict[tuple[str, str], dict[str, object]],
]:
    """Build the three join-side indexes :func:`_build_rows` consults.

    Pulled out of ``_build_rows`` so the join loop's cyclomatic
    complexity stays under the linter's threshold. Each index is keyed
    only by entries whose primary-key fields are present and of the
    expected type -- defensive against an upstream PostgREST schema
    change that introduces a NULL or non-string key.
    """
    drug_by_id: dict[str, dict[str, object]] = {}
    for d in drugs:
        drug_id = d.get("drugid")
        if isinstance(drug_id, str):
            drug_by_id[drug_id] = d

    guideline_by_id: dict[int, dict[str, object]] = {}
    for g in guidelines:
        gid = g.get("id")
        if isinstance(gid, int):
            guideline_by_id[gid] = g

    pair_by_drug_gene: dict[tuple[str, str], dict[str, object]] = {}
    for p in pairs:
        pair_drug = p.get("drugid")
        pair_gene = p.get("genesymbol")
        if isinstance(pair_drug, str) and isinstance(pair_gene, str):
            pair_by_drug_gene[(pair_drug, pair_gene)] = p

    return drug_by_id, guideline_by_id, pair_by_drug_gene


def _resolve_rec_base(
    rec: dict[str, object],
    *,
    drug_by_id: dict[str, dict[str, object]],
    guideline_by_id: dict[int, dict[str, object]],
) -> _RecBase | None:
    """Resolve the cross-table joins for one recommendation.

    Returns ``None`` (and logs a debug line) when the recommendation
    can't be turned into a valid row: no drugid, drugid not in the
    /drug payload, or drug entry missing a name. The schema's NOT NULL
    ``drug_name`` would reject the row at insert time anyway, so we
    drop it deliberately at parse time and surface the recommendation
    id for forensic traceability.
    """
    rec_id = rec.get("id")
    rec_drug = rec.get("drugid")
    if not isinstance(rec_drug, str):
        logger.debug("cpic.recommendation.skipped_no_drugid", recommendation_id=rec_id)
        return None

    drug = drug_by_id.get(rec_drug, {})
    drug_name = _str_or_none(drug.get("name"))
    if drug_name is None:
        logger.debug(
            "cpic.recommendation.skipped_unknown_drug",
            recommendation_id=rec_id,
            drugid=rec_drug,
        )
        return None

    guideline_id_raw = rec.get("guidelineid")
    guideline = (
        guideline_by_id.get(guideline_id_raw, {}) if isinstance(guideline_id_raw, int) else {}
    )
    return _RecBase(
        cpic_id=str(rec_id) if rec_id is not None else None,
        drug_name=drug_name,
        drug_rxnorm_id=_str_or_none(drug.get("rxnormid")),
        recommendation=_str_or_none(rec.get("drugrecommendation")),
        classification=_str_or_none(rec.get("classification")),
        pediatric=_pediatric_flag(rec.get("population")),
        guideline_url=_str_or_none(guideline.get("url")),
    )


def _build_rows(
    guidelines: list[dict[str, object]],
    pairs: list[dict[str, object]],
    recommendations: list[dict[str, object]],
    drugs: list[dict[str, object]],
) -> list[_ParsedRow]:
    """Join the four endpoint payloads into ``_ParsedRow`` instances.

    The join shape:

    * Recommendation -> drug via ``rec.drugid == drug.drugid``. A
      recommendation that references a drug not in ``/drug``, or a
      drug entry without a ``name``, is skipped (the schema's NOT NULL
      ``drug_name`` would reject it anyway). The skip is logged with
      the recommendation id for forensic traceability.
    * Recommendation -> guideline via ``rec.guidelineid == guideline.id``.
      Missing guideline rows leave ``guideline_url`` as None.
    * (Recommendation, gene) -> pair via
      ``(rec.drugid, gene_symbol) == (pair.drugid, pair.genesymbol)``.
      Pair carries ``cpic_level`` and the citation list that supplies
      ``publication_pmid``. Missing pair rows leave both as None.

    Recommendations with an unparseable / empty ``lookupkey`` are
    skipped (and debug-logged) because we can't produce a row with a
    NULL phenotype under the loader's "(gene x drug x phenotype)
    granularity" contract. Real-data verification shows zero such rows
    today but the skip is structural, not data-dependent.

    ``cpic_id`` is the recommendation's primary key (``rec.id``)
    coerced to ``str``. Rows split from one recommendation share the
    same ``cpic_id`` and differ only in ``gene_symbol`` and
    ``phenotype``.
    """
    drug_by_id, guideline_by_id, pair_by_drug_gene = _build_indexes(
        drugs=drugs,
        guidelines=guidelines,
        pairs=pairs,
    )

    rows: list[_ParsedRow] = []
    skipped = 0
    for rec in recommendations:
        gene_pheno = _parse_lookupkey(rec.get("lookupkey"))
        if not gene_pheno:
            skipped += 1
            logger.debug(
                "cpic.recommendation.skipped_no_lookupkey",
                recommendation_id=rec.get("id"),
            )
            continue

        base = _resolve_rec_base(
            rec,
            drug_by_id=drug_by_id,
            guideline_by_id=guideline_by_id,
        )
        if base is None:
            skipped += 1
            continue

        # ``_resolve_rec_base`` already validated ``rec["drugid"]`` as
        # ``str``; re-check inside the loop to narrow the static type.
        rec_drug = rec["drugid"]
        if not isinstance(rec_drug, str):
            continue
        for gene_symbol, phenotype in gene_pheno:
            pair = pair_by_drug_gene.get((rec_drug, gene_symbol), {})
            rows.append(
                _ParsedRow(
                    cpic_id=base.cpic_id,
                    gene_symbol=gene_symbol,
                    drug_name=base.drug_name,
                    drug_rxnorm_id=base.drug_rxnorm_id,
                    phenotype=phenotype,
                    recommendation=base.recommendation,
                    classification_strength=base.classification,
                    cpic_level=_str_or_none(pair.get("cpiclevel")),
                    pediatric=base.pediatric,
                    guideline_url=base.guideline_url,
                    publication_pmid=_first_pmid(pair.get("citations")),
                    last_updated=None,
                ),
            )

    if skipped:
        logger.info(
            "cpic.build_rows.skipped_total",
            skipped=skipped,
            emitted=len(rows),
        )
    return rows


def _load_endpoint_payload(path: Path) -> list[dict[str, object]]:
    """Read one of CPIC's endpoint JSON files into a list-of-dicts.

    The four CPIC endpoints all return a top-level JSON array. A
    non-array payload (or an array containing non-dict elements) is a
    contract violation; we raise :class:`ValueError` so the failure is
    surfaced before the join runs against partially-typed data.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        msg = f"CPIC endpoint payload {path.name} is not a JSON array"
        raise TypeError(msg)
    rows: list[dict[str, object]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            msg = f"CPIC endpoint payload {path.name} has a non-object element"
            raise TypeError(msg)
        rows.append(entry)
    return rows


def _combined_file_hash(downloads: dict[str, DownloadResult]) -> str:
    """Deterministic SHA-256 over the four data endpoints' SHA-256s.

    ``annotation_source_versions`` stores a single ``source_file_hash``,
    but CPIC's snapshot is spread across four files. Hashing the sorted
    ``(endpoint, sha256)`` tuples gives one fingerprint that changes
    iff any endpoint's data changes -- the schema's intent ("identify
    the on-disk snapshot uniquely") preserved across the multi-file
    shape. Sorted-by-name ordering guarantees the combined hash is
    independent of dict iteration order.
    """
    h = hashlib.sha256()
    for name in sorted(downloads):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(downloads[name].sha256.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _next_guideline_id(conn: DuckDBPyConnection) -> int:
    """``COALESCE(MAX(guideline_id), 0) + 1``.

    Mirrors :func:`genome.annotate.loaders.pharmgkb._next_pharmgkb_id`.
    Allocates a fresh ID range starting from the next unused integer so
    the bulk insert can compute every row's ``guideline_id`` from a
    monotonically increasing offset.
    """
    row = conn.execute(
        f"SELECT COALESCE(MAX(guideline_id), 0) FROM {_TARGET_TABLE}",  # noqa: S608
    ).fetchone()
    return int(row[0]) + 1 if row is not None else 1


def _bulk_insert(
    conn: DuckDBPyConnection,
    rows: list[_ParsedRow],
    *,
    source_version_id: int,
    retrieval_date: datetime,
) -> int:
    """Bulk-load ``rows`` into ``cpic_guidelines``.

    Builds a PyArrow Table with one column per destination column
    (including ``guideline_id``, ``source_version_id``,
    ``retrieval_date``, and ``is_active=True``), registers it under a
    temp name, then issues ``INSERT INTO cpic_guidelines (...)
    SELECT ... FROM <temp>`` and unregisters.

    Returns the number of rows inserted. A zero-row call inserts
    nothing and returns 0.
    """
    if not rows:
        return 0

    base_id = _next_guideline_id(conn)
    n = len(rows)
    # Naive UTC datetime: pa.timestamp("us") (no tz) lines up with DuckDB's
    # TIMESTAMP (no tz). Same convention as the PharmGKB loader and the
    # imputation runs writer.
    naive_retrieval = retrieval_date.astimezone(UTC).replace(tzinfo=None)
    table = pa.table(
        {
            "guideline_id": pa.array(range(base_id, base_id + n), type=pa.int64()),
            "cpic_id": pa.array([r.cpic_id for r in rows], type=pa.string()),
            "gene_symbol": pa.array([r.gene_symbol for r in rows], type=pa.string()),
            "drug_name": pa.array([r.drug_name for r in rows], type=pa.string()),
            "drug_rxnorm_id": pa.array([r.drug_rxnorm_id for r in rows], type=pa.string()),
            "phenotype": pa.array([r.phenotype for r in rows], type=pa.string()),
            "recommendation": pa.array([r.recommendation for r in rows], type=pa.string()),
            "classification_strength": pa.array(
                [r.classification_strength for r in rows],
                type=pa.string(),
            ),
            "cpic_level": pa.array([r.cpic_level for r in rows], type=pa.string()),
            "pediatric": pa.array([r.pediatric for r in rows], type=pa.bool_()),
            "guideline_url": pa.array([r.guideline_url for r in rows], type=pa.string()),
            "publication_pmid": pa.array(
                [r.publication_pmid for r in rows],
                type=pa.string(),
            ),
            "last_updated": pa.array([r.last_updated for r in rows], type=pa.date32()),
            "source_version_id": pa.array([source_version_id] * n, type=pa.int64()),
            "retrieval_date": pa.array([naive_retrieval] * n, type=pa.timestamp("us")),
            "is_active": pa.array([True] * n, type=pa.bool_()),
        },
        schema=_ARROW_SCHEMA,
    )
    try:
        conn.register("_cpic_stage_arrow", table)
        conn.execute(
            f"""
            INSERT INTO {_TARGET_TABLE} (
                guideline_id, cpic_id,
                gene_symbol, drug_name, drug_rxnorm_id,
                phenotype, recommendation, classification_strength,
                cpic_level, pediatric,
                guideline_url, publication_pmid, last_updated,
                source_version_id, retrieval_date, is_active
            )
            SELECT
                guideline_id, cpic_id,
                gene_symbol, drug_name, drug_rxnorm_id,
                phenotype, recommendation, classification_strength,
                cpic_level, pediatric,
                guideline_url, publication_pmid, last_updated,
                source_version_id, retrieval_date, is_active
              FROM _cpic_stage_arrow
            """,  # noqa: S608 — table name is a module constant, not user input
        )
    finally:
        conn.unregister("_cpic_stage_arrow")
    return n


def _deactivate_for_refresh(
    conn: DuckDBPyConnection,
    *,
    source_version_id: int,
    force: bool,
) -> int:
    """Deactivate prior CPIC rows ahead of a refresh insert.

    Mirrors :func:`genome.annotate.loaders.pharmgkb._deactivate_for_refresh`:
    on the normal (non-force) path defer to the schema's standard
    supersession helper; on the force path blanket-deactivate every
    active row so a re-run against the same version label doesn't
    leave duplicate active rows.

    ``cpic_guidelines`` carries ``is_active`` but not ``superseded_by``
    (verified against ``docs/schemas/schema_group_2_reference_annotations.md``
    and ``ddl/group_2_annotations.sql``), so we pass
    ``has_superseded_by=False`` to the standard helper.

    Returns the number of rows flipped to ``is_active=FALSE``.
    """
    if not force:
        return deactivate_prior_versions(
            conn,
            table=_TARGET_TABLE,
            new_source_version_id=source_version_id,
            has_superseded_by=False,
        )
    # _TARGET_TABLE is a module constant, not user input — S608 is a
    # false positive here.
    res = conn.execute(
        f"UPDATE {_TARGET_TABLE} SET is_active = FALSE WHERE is_active = TRUE",  # noqa: S608
    )
    row = res.fetchone() if hasattr(res, "fetchone") else None
    return int(row[0]) if row is not None and row[0] is not None else 0


def _cleanup_orphan_version_row(
    conn: DuckDBPyConnection,
    source_version_id: int,
) -> None:
    """Best-effort delete of an orphan ``annotation_source_versions`` row.

    Same shape as PharmGKB's helper -- called when the supersede+insert
    transaction rolls back so the version row that
    :func:`upsert_source_version` committed in its own transaction
    doesn't leave a dangling "version exists but zero rows referenced"
    state. The DELETE is FK-safe because no ``cpic_guidelines`` rows
    reference the new ``source_version_id`` yet (the bulk_insert never
    committed). Failures are swallowed and logged; the caller is
    already raising the original exception.
    """
    try:
        conn.execute(
            "DELETE FROM annotation_source_versions WHERE source_version_id = ?",
            [source_version_id],
        )
    except Exception:  # noqa: BLE001 — best-effort cleanup; original exc re-raised by caller
        logger.warning(
            "cpic.cleanup.orphan_version_row_delete_failed",
            source_version_id=source_version_id,
            exc_info=True,
        )


def refresh(force: bool) -> RefreshResult:  # noqa: FBT001 — registry RefreshFn signature
    """Refresh CPIC clinical guidelines.

    Pipeline:

    1. Download the four CPIC data endpoints
       (``/guideline``, ``/pair``, ``/recommendation``, ``/drug``) via
       the audited :func:`genome.annotate.downloads.download_to_cache`
       (skip-if-exists by default; ``force=True`` re-downloads).
    2. Resolve the version label from the ``/change_log`` canary
       (falls back to retrieval date in ``YYYY_MM_DD`` form when the
       canary fails or returns nothing parseable).
    3. Short-circuit and return ``was_already_current=True`` if a row
       in ``annotation_source_versions`` already names the resolved
       ``(source_db='cpic', version)`` and ``force`` is ``False``.
    4. Parse the four JSON payloads and join them client-side.
       Multi-gene recommendations split into one ``_ParsedRow`` per gene.
    5. Inside one DuckDB transaction: upsert
       ``annotation_source_versions``, deactivate prior CPIC rows via
       :func:`deactivate_prior_versions`, and bulk-insert the freshly
       joined rows.
    6. Return a :class:`RefreshResult` describing what landed.
    """
    log = logger.bind(source=SOURCE_DB)

    # 1. Download (skip-if-exists; ``force`` re-downloads). The scaffold's
    # ``download_to_cache`` handles redirects via its injected
    # ``httpx.Client(follow_redirects=True)`` -- the loader does not
    # need to know about it.
    downloads = _download_all_endpoints(force=force)
    for endpoint, dr in sorted(downloads.items()):
        log.info(
            "cpic.download.audited",
            endpoint=endpoint,
            sha256=dr.sha256[:12],
            size_bytes=dr.size_bytes,
        )

    # 2. Resolve version label.
    version = _resolve_version(force=force)
    log.info("cpic.version.resolved", version=version)

    # 3. Idempotence check.
    with duckdb_connection() as conn:
        current = get_current_version(conn, SOURCE_DB)
        if current is not None and current.version == version and not force:
            log.info("cpic.skip_already_current", version=version)
            return RefreshResult(
                source_db=SOURCE_DB,
                source_version_id=current.source_version_id,
                version=version,
                record_count=current.record_count or 0,
                was_already_current=True,
            )

    # 4. Parse and join.
    guidelines = _load_endpoint_payload(downloads["guideline"].path)
    pairs = _load_endpoint_payload(downloads["pair"].path)
    recommendations = _load_endpoint_payload(downloads["recommendation"].path)
    drugs = _load_endpoint_payload(downloads["drug"].path)
    rows = _build_rows(guidelines, pairs, recommendations, drugs)
    log.info(
        "cpic.parse.complete",
        row_count=len(rows),
        guideline_count=len(guidelines),
        pair_count=len(pairs),
        recommendation_count=len(recommendations),
        drug_count=len(drugs),
    )

    # 5. Single-transaction load. The PharmGKB loader's "version row
    # in its own transaction, supersede+insert in the wrapping
    # transaction" shape applies verbatim here -- DuckDB does not
    # support nested transactions and ``upsert_source_version`` manages
    # its own due to the FK+index quirk documented in that module.
    combined_hash = _combined_file_hash(downloads)
    total_size = sum(dr.size_bytes for dr in downloads.values())
    retrieval_date = datetime.now(UTC)
    with duckdb_connection() as conn:
        source_version_id = upsert_source_version(
            conn,
            source_db=SOURCE_DB,
            version=version,
            source_url=GUIDELINE_URL,
            source_file_hash=combined_hash,
            source_file_size=total_size,
            record_count=len(rows),
        )
        conn.begin()
        try:
            # ``cpic_guidelines`` carries ``is_active`` but not
            # ``superseded_by`` (schema-verified). Same supersession
            # pattern as PharmGKB: standard helper on non-force,
            # blanket sweep on force to avoid duplicate-active rows
            # when the version label is unchanged.
            deactivated = _deactivate_for_refresh(
                conn,
                source_version_id=source_version_id,
                force=force,
            )
            inserted = _bulk_insert(
                conn,
                rows,
                source_version_id=source_version_id,
                retrieval_date=retrieval_date,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            _cleanup_orphan_version_row(conn, source_version_id)
            raise

    log.info(
        "cpic.refresh.complete",
        version=version,
        inserted=inserted,
        deactivated=deactivated,
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
    "CHANGE_LOG_LATEST_URL",
    "DRUG_URL",
    "GUIDELINE_URL",
    "PAIR_URL",
    "RECOMMENDATION_URL",
    "SOURCE_DB",
    "URL_VERIFIED_DATE",
    "refresh",
]
