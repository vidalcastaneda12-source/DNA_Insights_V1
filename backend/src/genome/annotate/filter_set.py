"""Position filter-set builder for remote-tabix annotation loaders.

Extracted from :mod:`genome.annotate.loaders.gnomad` at sub-phase 5.6 and
parameterised on a ``strategy`` so two remote-tabix loaders can share it:

* ``"three_way"`` — the ``(user U ClinVar U GWAS)`` intersection. The
  upstream VCFs are filtered to positions present in the user's
  variants OR the active ClinVar release OR the active GWAS Catalog release.
  CLAUDE.md "Things never to do" #3 mandates the broader
  ``(user U ClinVar U GWAS U PGS)`` intersection but PGS per-variant weights
  do not yet exist at PR-B time; see finding-011. This was gnomAD's original
  filter; it is retained as gnomAD's revert path + the future PGS extension.
* ``"user_only"`` — the filter used by **both gnomAD and dbSNP**. Both
  annotate **the user's own variants**, and every consumer of their tables
  (``gnomad_frequencies``, ``dbsnp_annotations``) inner-joins
  ``variants_master``, so the filter is the distinct user positions alone.
  dbSNP shipped on this filter from sub-phase 5.6 (the ClinVar/GWAS/PGS legs
  deferred — see finding-016); gnomAD adopted it per finding-035 (VSC-User
  ruled ``user_only`` 2026-06-21) after the consumer audit confirmed the
  ClinVar/GWAS-only legs were loaded but never read.

The active-version joins go through ``annotation_sources`` (the version-pointer
pattern, finding-010): only rows under the currently-active ClinVar / GWAS
source-version contribute.

Every subquery guards ``pos_grch38 > 0``. Upstream annotation loaders (notably
ClinVar) emit a ``-1`` sentinel for variants whose GRCh38 coordinate could not
be resolved; an ``IS NOT NULL`` guard would still admit those, and any negative
value flowing through :func:`genome.annotate.remote_tabix.coalesce_positions`
would produce an invalid ``<contig>:-1--1`` tabix region that htslib rejects
with "Coordinates must be > 0" and may corrupt the BGZF read offset state. The
guard lives here, at the filter set, so every consumer inherits it
(finding-013 #11). ``variants_master`` enforces ``pos_grch38 BIGINT NOT NULL``
at the schema level, but the same guard is applied uniformly for defence in
depth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from duckdb import DuckDBPyConnection

FilterStrategy = Literal["user_only", "three_way"]


@dataclass(frozen=True, slots=True)
class FilterSet:
    """Result of :func:`build_filter_set`.

    Carries the per-chromosome sorted-unique position lists plus the
    composition counts the end-of-load summary surfaces. ``positions``
    maps each chrom in the caller's ``supported_chroms`` to a sorted
    list of unique positions. ``composition`` carries the per-source
    distinct counts plus ``union_total``; its key set depends on the
    strategy (``{user, union_total}`` for ``user_only``;
    ``{user, clinvar, gwas, union_total}`` for ``three_way``).
    """

    positions: dict[str, list[int]]
    composition: dict[str, int]


def build_filter_set(
    conn: DuckDBPyConnection,
    *,
    strategy: FilterStrategy,
    supported_chroms: Sequence[str],
) -> FilterSet:
    """Compute the ``(chrom, pos_grch38)`` filter set for a remote-tabix loader.

    ``supported_chroms`` is the chrom allow-list the loader will query
    (gnomAD: 1-22, X; dbSNP: 1-22, X, Y, MT). Positions on chroms outside
    the allow-list are dropped, and only allow-list chroms appear as keys
    in the returned ``positions`` dict.

    ``strategy``:

    * ``"three_way"`` — distinct positions from ``variants_master`` U
      active ``clinvar_annotations`` U active ``gwas_catalog_associations``
      (the ClinVar/GWAS legs joined through ``annotation_sources`` on the
      current pointer). Composition keys
      ``{user, clinvar, gwas, union_total}``.
    * ``"user_only"`` — distinct positions from ``variants_master`` only.
      Composition keys ``{user, union_total}``.
    """
    chrom_list = ",".join(f"'{c}'" for c in supported_chroms)

    user_row = conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT chrom, pos_grch38
              FROM variants_master
             WHERE chrom::VARCHAR IN ({chrom_list})
               AND pos_grch38 > 0
        )
        """,  # noqa: S608 — chrom_list is built from the caller's chrom allow-list
    ).fetchone()
    user_count = int(user_row[0]) if user_row is not None else 0

    if strategy == "user_only":
        # DISTINCT so a multi-allelic position split into biallelic rows in
        # variants_master (architecture decision #3) contributes one position,
        # not one per ALT — keeping union_total == user and the per-chrom
        # position lists free of duplicates (the three_way path dedups via
        # UNION; this leg must dedup too). Real-data verification caught the
        # 196-row over-count on the chip-only corpus (finding-013's lesson).
        union_rows = conn.execute(
            f"""
            SELECT DISTINCT chrom::VARCHAR AS chrom, pos_grch38 AS pos
              FROM variants_master
             WHERE chrom::VARCHAR IN ({chrom_list})
               AND pos_grch38 > 0
             ORDER BY chrom, pos
            """,  # noqa: S608
        ).fetchall()
        by_chrom, union_total = _bucket_positions(union_rows, supported_chroms)
        composition = {"user": user_count, "union_total": union_total}
        return FilterSet(positions=by_chrom, composition=composition)

    # Otherwise: the three-way (user U ClinVar U GWAS) union.
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

    by_chrom, union_total = _bucket_positions(union_rows, supported_chroms)
    composition = {
        "user": user_count,
        "clinvar": clinvar_count,
        "gwas": gwas_count,
        "union_total": union_total,
    }
    return FilterSet(positions=by_chrom, composition=composition)


def _bucket_positions(
    union_rows: list[tuple[object, ...]],
    supported_chroms: Sequence[str],
) -> tuple[dict[str, list[int]], int]:
    """Bucket sorted ``(chrom, pos)`` rows into a per-chrom list dict.

    Returns ``(by_chrom, union_total)``. Chroms outside
    ``supported_chroms`` are skipped (defence in depth; the SQL already
    filters to the allow-list). ``union_total`` counts only positions
    that landed in a supported-chrom bucket.
    """
    by_chrom: dict[str, list[int]] = {chrom: [] for chrom in supported_chroms}
    union_total = 0
    for chrom_value, pos_value in union_rows:
        chrom_str = str(chrom_value)
        if chrom_str not in by_chrom:
            continue
        by_chrom[chrom_str].append(int(pos_value))  # type: ignore[call-overload]
        union_total += 1
    return by_chrom, union_total


__all__ = [
    "FilterSet",
    "FilterStrategy",
    "build_filter_set",
]
