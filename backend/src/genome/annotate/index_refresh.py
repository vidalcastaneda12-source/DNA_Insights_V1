"""Build the ``variant_annotations_index`` rollup (sub-phase 5.7).

Phase 5 shipped seven per-source annotation loaders, each landing its own
table under the version-pointer supersession pattern (finding-010). This
module owns the denormalized rollup that joins the variant-linkable ones
into one sparse row per variant — the table ``variant_full_v`` and the SNP
detail page read from.

Four sources contribute a column to the index:

* **ClinVar** — joined on full GRCh38 coords ``(chrom, pos, ref, alt)``;
  clinical significance is allele-specific, so the coordinate key (the
  ``variants_master`` UNIQUE key) keeps the join ≤1:1.
* **gnomAD** — joined on full coords for the same reason (allele frequency
  is allele-specific).
* **GWAS Catalog** — joined on ``rsid``; the catalog carries no ref/alt, and
  a GWAS association is locus-level evidence. When two ``variants_master``
  rows share an rsid (a multi-allelic split), both carry the trait.
* **PharmGKB** — joined on ``rsid``; PGx associations are locus-level.

Every read filters to the *currently-active* version via the
``annotation_sources`` pointer (the same join shape as ``user_pgx_variants_v``),
so a superseded release never leaks into the index. The four VEP columns
(``most_severe_consequence``, ``impact``, ``cadd_phred``,
``alphamissense_class``) and ``is_acmg_sf`` ship NULL — Phase 6 backfills them
via a later rollup refresh (finding-017). ``is_curated`` is computed from
ClinVar or PharmGKB only; CPIC is gene+drug grain with no variant linkage, so
it cannot contribute at the variant level until a gene→variant mapping lands
(Phase 6/7) — the DDL comment's "ClinVar, PharmGKB, CPIC" overstates current
coverage.

The index is **not** a registered source: it has no ``annotation_sources``
row of its own and does not route through ``register_loader`` /
``flip_to_new_version`` / ``insert_source_version``. It is a derived
materialization. ``variant_id`` is its PRIMARY KEY, so supersession is a
wholesale replace — ``DELETE`` then ``INSERT … SELECT`` inside one
transaction. DuckDB snapshot isolation means a concurrent reader sees either
the whole old index or the whole new one, never a mix (CLAUDE.md decision #7).
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from genome.annotate.source_versions import get_current_version
from genome.annotate.supersession import commit_and_checkpoint
from genome.db.duckdb_conn import duckdb_connection

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

_VARIANT_LINKABLE_SOURCES: Final[tuple[str, ...]] = (
    "clinvar",
    "gwas_catalog",
    "gnomad",
    "pharmgkb",
)
"""The four sources that contribute a column to ``variant_annotations_index``.

PGS Catalog (score-level) contributes no rollup column, so it is absent here.
dbSNP contributes no column either, but its ``variant_aliases`` map now drives
tier-2 rsID matching on the GWAS/PharmGKB legs (finding-005 #4 / finding-019),
so its version is captured for provenance via :data:`_PROVENANCE_SOURCES`.
"""

_PROVENANCE_SOURCES: Final[tuple[str, ...]] = (*_VARIANT_LINKABLE_SOURCES, "dbsnp")
"""Sources whose active version the index build depends on (for ``refresh_versions``).

The four column contributors plus dbSNP: tier-2 rsID matching resolves both the
user-side and source-side rsIDs through the dbSNP-epoch ``variant_aliases`` map,
so two index builds under different dbSNP pointers can differ. Recording dbSNP's
version keeps the per-row ``refresh_versions`` snapshot reproducible.
"""

# The build statement. Four per-source rollup CTEs (each unique on
# ``variant_id`` via its GROUP BY), a UNION of their keys, then one LEFT JOIN
# of each CTE back onto that key set so every join is independent and ≤1:1.
# The 25-column INSERT list matches ddl/group_2_annotations.sql 459-499 exactly.
# The lone ``?`` binds the versions-JSON snapshot (identical on every row);
# every other value is computed in-engine. No string interpolation → no S608.
_BUILD_SQL: Final = """
INSERT INTO variant_annotations_index (
    variant_id,
    clinvar_significance, clinvar_star_rating, clinvar_count, clinvar_conditions,
    gwas_trait_count, gwas_min_p_value, gwas_traits, gwas_strongest_trait,
    af_global, af_max_population, af_min_population, is_rare, is_ultrarare,
    most_severe_consequence, impact, cadd_phred, alphamissense_class,
    has_pgx, pgx_drug_count, pgx_drugs,
    is_acmg_sf, is_curated, last_refreshed, refresh_versions
)
WITH alias_map AS (
    -- Tier-2 rsID merge map (finding-005 #4 / finding-019), filtered to the
    -- active dbSNP epoch via the annotation_sources pointer. GROUP BY makes the
    -- map provably 1:1 on alias_rsid even though the table carries no UNIQUE
    -- constraint there (the loader dedups at write time, but that is a runtime
    -- invariant, not a DB guarantee); ANY_VALUE is therefore deterministic.
    -- Single-hop resolution is complete because current_rsid is dbSNP's
    -- pre-collapsed transitive survivor (finding-019: rsCurrent, not rsLow).
    -- When dbSNP is unloaded this CTE is empty and both rsID legs below reduce
    -- exactly to the prior tier-1 join vm.rsid = source.rsid (graceful degrade).
    SELECT alias_rsid, ANY_VALUE(current_rsid) AS current_rsid
    FROM variant_aliases va
    JOIN annotation_sources va_src
        ON va_src.source_db = 'dbsnp'
       AND va_src.current_source_version_id = va.source_version_id
    GROUP BY alias_rsid
),
vm_canon AS (
    -- variants_master with each rsID canonicalized to its merge survivor.
    -- Non-rs / synthetic / NULL rsIDs find no alias and pass through unchanged.
    SELECT vm.variant_id,
           COALESCE(am.current_rsid, vm.rsid) AS canon_rsid
    FROM variants_master vm
    LEFT JOIN alias_map am ON am.alias_rsid = vm.rsid
    WHERE vm.rsid IS NOT NULL
),
clinvar_roll AS (
    SELECT
        vm.variant_id,
        arg_max(
            cv.clinical_significance,
            CASE lower(trim(cv.clinical_significance))
                WHEN 'pathogenic' THEN 6
                WHEN 'pathogenic/likely pathogenic' THEN 6
                WHEN 'likely pathogenic' THEN 5
                WHEN 'conflicting' THEN 4
                WHEN 'uncertain significance' THEN 3
                WHEN 'likely benign' THEN 2
                WHEN 'benign/likely benign' THEN 2
                WHEN 'benign' THEN 1
                ELSE 0
            END
        ) AS clinvar_significance,
        MAX(cv.star_rating) AS clinvar_star_rating,
        COUNT(*) AS clinvar_count,
        list_sort(list_distinct(flatten(
            array_agg(cv.conditions) FILTER (WHERE cv.conditions IS NOT NULL)
        ))) AS clinvar_conditions
    FROM clinvar_annotations cv
    JOIN annotation_sources cv_src
        ON cv_src.source_db = 'clinvar'
       AND cv_src.current_source_version_id = cv.source_version_id
    JOIN variants_master vm
        ON vm.chrom = cv.chrom
       AND vm.pos_grch38 = cv.pos_grch38
       AND vm.ref_allele = cv.ref_allele
       AND vm.alt_allele = cv.alt_allele
    WHERE cv.pos_grch38 IS NOT NULL
    GROUP BY vm.variant_id
),
gwas_roll AS (
    SELECT
        vmc.variant_id,
        COUNT(DISTINCT gw.trait_name) AS gwas_trait_count,
        MIN(gw.p_value) AS gwas_min_p_value,
        list_sort(list_distinct(
            array_agg(gw.trait_name) FILTER (WHERE gw.trait_name IS NOT NULL)
        )) AS gwas_traits,
        arg_min(gw.trait_name, gw.p_value) AS gwas_strongest_trait
    FROM gwas_catalog_associations gw
    JOIN annotation_sources gw_src
        ON gw_src.source_db = 'gwas_catalog'
       AND gw_src.current_source_version_id = gw.source_version_id
    LEFT JOIN alias_map gam ON gam.alias_rsid = gw.rsid
    JOIN vm_canon vmc
        ON vmc.canon_rsid = COALESCE(gam.current_rsid, gw.rsid)
    GROUP BY vmc.variant_id
),
gnomad_roll AS (
    SELECT
        vm.variant_id,
        MAX(gn.af_global) AS af_global,
        GREATEST(
            MAX(gn.af_afr), MAX(gn.af_ami), MAX(gn.af_amr), MAX(gn.af_asj),
            MAX(gn.af_eas), MAX(gn.af_fin), MAX(gn.af_mid), MAX(gn.af_nfe),
            MAX(gn.af_sas), MAX(gn.af_oth)
        ) AS af_max_population,
        LEAST(
            MAX(gn.af_afr), MAX(gn.af_ami), MAX(gn.af_amr), MAX(gn.af_asj),
            MAX(gn.af_eas), MAX(gn.af_fin), MAX(gn.af_mid), MAX(gn.af_nfe),
            MAX(gn.af_sas), MAX(gn.af_oth)
        ) AS af_min_population
    FROM gnomad_frequencies gn
    JOIN annotation_sources gn_src
        ON gn_src.source_db = 'gnomad'
       AND gn_src.current_source_version_id = gn.source_version_id
    JOIN variants_master vm
        ON vm.chrom = gn.chrom
       AND vm.pos_grch38 = gn.pos_grch38
       AND vm.ref_allele = gn.ref_allele
       AND vm.alt_allele = gn.alt_allele
    WHERE gn.pos_grch38 IS NOT NULL
    GROUP BY vm.variant_id
),
pharmgkb_roll AS (
    SELECT
        vmc.variant_id,
        COUNT(DISTINCT pg.drug_name) AS pgx_drug_count,
        list_sort(list_distinct(
            array_agg(pg.drug_name) FILTER (WHERE pg.drug_name IS NOT NULL)
        )) AS pgx_drugs
    FROM pharmgkb_annotations pg
    JOIN annotation_sources pg_src
        ON pg_src.source_db = 'pharmgkb'
       AND pg_src.current_source_version_id = pg.source_version_id
    LEFT JOIN alias_map pam ON pam.alias_rsid = pg.rsid
    JOIN vm_canon vmc
        ON vmc.canon_rsid = COALESCE(pam.current_rsid, pg.rsid)
    WHERE pg.rsid IS NOT NULL
    GROUP BY vmc.variant_id
),
all_variants AS (
    SELECT variant_id FROM clinvar_roll
    UNION
    SELECT variant_id FROM gwas_roll
    UNION
    SELECT variant_id FROM gnomad_roll
    UNION
    SELECT variant_id FROM pharmgkb_roll
)
SELECT
    av.variant_id,
    cv.clinvar_significance,
    cv.clinvar_star_rating,
    COALESCE(cv.clinvar_count, 0),
    COALESCE(cv.clinvar_conditions, []::VARCHAR[]),
    COALESCE(gw.gwas_trait_count, 0),
    gw.gwas_min_p_value,
    COALESCE(gw.gwas_traits, []::VARCHAR[]),
    gw.gwas_strongest_trait,
    gn.af_global,
    gn.af_max_population,
    gn.af_min_population,
    gn.af_global < 0.01,
    gn.af_global < 0.001,
    CAST(NULL AS VARCHAR),
    CAST(NULL AS VARCHAR),
    CAST(NULL AS DOUBLE),
    CAST(NULL AS VARCHAR),
    (pg.variant_id IS NOT NULL),
    COALESCE(pg.pgx_drug_count, 0),
    COALESCE(pg.pgx_drugs, []::VARCHAR[]),
    CAST(NULL AS BOOLEAN),
    (cv.variant_id IS NOT NULL OR pg.variant_id IS NOT NULL),
    CURRENT_TIMESTAMP,
    CAST(? AS JSON)
FROM all_variants av
LEFT JOIN clinvar_roll cv ON cv.variant_id = av.variant_id
LEFT JOIN gwas_roll gw ON gw.variant_id = av.variant_id
LEFT JOIN gnomad_roll gn ON gn.variant_id = av.variant_id
LEFT JOIN pharmgkb_roll pg ON pg.variant_id = av.variant_id
"""

_SUMMARY_SQL: Final = """
SELECT
    COUNT(*),
    COUNT(*) FILTER (WHERE clinvar_count > 0),
    COUNT(*) FILTER (WHERE gwas_trait_count > 0),
    COUNT(*) FILTER (WHERE af_global IS NOT NULL),
    COUNT(*) FILTER (WHERE has_pgx),
    COUNT(*) FILTER (WHERE is_curated)
FROM variant_annotations_index
"""

_TIER2_LIFTS_SQL: Final = """
SELECT COUNT(DISTINCT vai.variant_id)
FROM variant_annotations_index vai
JOIN variants_master vm
    ON vm.variant_id = vai.variant_id
JOIN variant_aliases va
    ON va.alias_rsid = vm.rsid
JOIN annotation_sources va_src
    ON va_src.source_db = 'dbsnp'
   AND va_src.current_source_version_id = va.source_version_id
WHERE vai.gwas_trait_count > 0 OR vai.has_pgx
"""


@dataclass(frozen=True, slots=True)
class IndexRefreshResult:
    """Outcome of one :func:`refresh_index` call.

    ``row_count`` is the materialized row total (== ``COUNT(DISTINCT
    variant_id)`` since ``variant_id`` is the PK). The four ``*_matches``
    counts are per-source contribution tallies derived from the index's own
    columns: ``clinvar``/``gwas`` from their count columns, ``gnomad`` from
    ``af_global IS NOT NULL``, ``pharmgkb`` from ``has_pgx`` (exact).
    ``refresh_versions`` is the pre-serialization ``{source_db: version}``
    snapshot stamped on every row. ``tier2_rsid_lifts`` is a direction-1
    path-fired sentinel — indexed variants whose own rsID is a merged-away dbSNP
    alias and that carry a GWAS/PharmGKB annotation. It proves the tier-2 path
    fired but is **not** the recovered-variant count and is not a clean bound on
    it: it misses direction-2 lifts (where the *source* carried the stale rsID)
    and counts any direction-1 variant that already matched under tier-1. The
    recovered count is the per-leg ``*_matches`` delta vs the prior build.
    """

    row_count: int
    clinvar_matches: int
    gwas_matches: int
    gnomad_matches: int
    pharmgkb_matches: int
    curated_count: int
    tier2_rsid_lifts: int
    refresh_versions: dict[str, str]
    elapsed_ms: int


def _collect_refresh_versions(conn: DuckDBPyConnection) -> dict[str, str]:
    """Resolve the currently-active version label for each contributing source.

    Reads through the ``annotation_sources`` pointer via
    :func:`get_current_version`. Sources with no current pointer (never
    loaded) are omitted, so the resulting map names only the releases that
    actually feed this build.
    """
    versions: dict[str, str] = {}
    for source_db in _PROVENANCE_SOURCES:
        current = get_current_version(conn, source_db)
        if current is not None:
            versions[source_db] = current.version
    return versions


def _summarize_index(conn: DuckDBPyConnection) -> tuple[int, int, int, int, int, int]:
    """Return ``(row_count, clinvar, gwas, gnomad, pharmgkb, curated)`` counts.

    One scan over the freshly-built index. The per-source counts use the
    materialized columns as presence proxies (see :class:`IndexRefreshResult`).
    """
    row = conn.execute(_SUMMARY_SQL).fetchone()
    if row is None:  # pragma: no cover — COUNT(*) always returns one row
        return (0, 0, 0, 0, 0, 0)
    return (
        int(row[0]),
        int(row[1]),
        int(row[2]),
        int(row[3]),
        int(row[4]),
        int(row[5]),
    )


def _count_tier2_lifts(conn: DuckDBPyConnection) -> int:
    """Count indexed variants carrying a merged-away rsID with an rsID annotation.

    Direction-1 path-fired sentinel (see :class:`IndexRefreshResult`): distinct
    index variants whose own rsID is a merged-away alias under the active dbSNP
    epoch and that carry a GWAS or PharmGKB annotation. ``> 0`` proves the
    tier-2 path fired. It is **not** the recovered-variant count: it excludes
    direction-2 lifts (source-side stale rsID) and includes any direction-1
    variant that already matched under tier-1. Returns 0 when
    dbSNP/``variant_aliases`` is absent (the alias join yields no rows).
    """
    row = conn.execute(_TIER2_LIFTS_SQL).fetchone()
    return int(row[0]) if row is not None else 0


def refresh_index(
    conn: DuckDBPyConnection | None = None,
    *,
    force: bool = False,
) -> IndexRefreshResult:
    """Rebuild ``variant_annotations_index`` from the current annotation set.

    Wholesale replace: ``DELETE`` then ``INSERT … SELECT`` inside one
    transaction, so a reader sees either the entire old index or the entire
    new one (CLAUDE.md decision #7). The build is a pure in-engine DuckDB
    scan over the four variant-linkable sources, each filtered to its
    currently-active version via the ``annotation_sources`` pointer.

    ``conn`` defaults to a freshly-opened read-write connection; callers
    (and tests) may pass an already-open connection, which this function will
    not close. ``force`` is accepted for CLI symmetry but the build is
    unconditional, so it is a documented no-op.
    """
    started = time.monotonic()
    log = logger.bind(force=force)

    # Own the connection only when none was supplied; a borrowed conn is wrapped
    # in nullcontext so the `with` block does not close a conn it didn't open.
    # The `conn is None` test narrows in both ternary arms, so nullcontext(conn)
    # sees a non-None conn (an intermediate bool would defeat that narrowing).
    ctx: contextlib.AbstractContextManager[DuckDBPyConnection] = (
        duckdb_connection() if conn is None else contextlib.nullcontext(conn)
    )
    with ctx as active_conn:
        versions = _collect_refresh_versions(active_conn)
        versions_json = json.dumps(versions, sort_keys=True)
        log.info("index_refresh.versions_resolved", versions=versions)

        active_conn.begin()
        try:
            active_conn.execute("DELETE FROM variant_annotations_index")
            log.info("index_refresh.cleared")
            active_conn.execute(_BUILD_SQL, [versions_json])
            log.info("index_refresh.inserted")
            commit_and_checkpoint(active_conn, source_name="variant_annotations_index")
        except Exception:
            active_conn.rollback()
            raise

        (
            row_count,
            clinvar_matches,
            gwas_matches,
            gnomad_matches,
            pharmgkb_matches,
            curated_count,
        ) = _summarize_index(active_conn)
        tier2_rsid_lifts = _count_tier2_lifts(active_conn)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "index_refresh.complete",
        row_count=row_count,
        clinvar_matches=clinvar_matches,
        gwas_matches=gwas_matches,
        gnomad_matches=gnomad_matches,
        pharmgkb_matches=pharmgkb_matches,
        curated_count=curated_count,
        tier2_rsid_lifts=tier2_rsid_lifts,
        versions=versions,
        elapsed_ms=elapsed_ms,
    )
    return IndexRefreshResult(
        row_count=row_count,
        clinvar_matches=clinvar_matches,
        gwas_matches=gwas_matches,
        gnomad_matches=gnomad_matches,
        pharmgkb_matches=pharmgkb_matches,
        curated_count=curated_count,
        tier2_rsid_lifts=tier2_rsid_lifts,
        refresh_versions=versions,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "IndexRefreshResult",
    "refresh_index",
]
