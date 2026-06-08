"""Forensic diagnostic for PR-3 canonicalize-variants (read-only).

Answers three questions against the pre-canonicalize snapshot ("snap") and the
post-canonicalize live DB ("post"):
  Q2 (PIVOT): do imputed variants carry NULL, '.', or real rsIDs?
  Q1: what are the 115,700 rsid_conflicts (sentinel vs genuine vs merged-pair)?
  Q3: gwas/pharmgkb drop = dedup (re-lock) vs genuine loss (regression)?
  WHAT-IF: if confirmed sentinel, what survivors_enriched/conflicts a '.'->NULL
           fix would yield, and how much gwas/pharmgkb a fix recovers.

Nothing here writes to snap or post. TEMP tables live in an in-memory DB or in
the read-only snapshot's separate temp catalog.

Run:
  uv run python scripts/diag_canonicalize.py [SNAPSHOT.bak] [LIVE.duckdb]
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)  # quiet genome-import structlog chatter

import duckdb  # noqa: E402
from genome.annotate.canonicalize import (  # noqa: E402
    _BUILD_CANON_BEST_SQL,
    _BUILD_CANON_MAP_SQL,
    _BUILD_REMAP_SQL,
    _BUILD_RESOLVE_SQL,
    _count_rsid_metadata,
    _create_temp_tables,
)

ROOT = Path(__file__).resolve().parents[1]


def _newest_snapshot() -> Path:
    cands = sorted(
        (ROOT / "archive" / "canonicalize").glob("*.bak"),
        key=lambda p: p.stat().st_mtime,
    )
    if not cands:
        sys.exit("no snapshot found under archive/canonicalize/*.bak")
    return cands[-1]


SNAP = Path(sys.argv[1]) if len(sys.argv) > 1 else _newest_snapshot()
POST = Path(sys.argv[2]) if len(sys.argv) > 2 else (ROOT / "data" / "genome.duckdb")


def hr(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def table(rows, headers) -> None:
    rows = [tuple(str(c) for c in r) for r in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
        for i, h in enumerate(headers)
    ]
    print("  " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  " + "-+-".join("-" * w for w in widths))
    for r in rows:
        print("  " + " | ".join(r[i].ljust(widths[i]) for i in range(len(headers))))


# ---------------------------------------------------------------------------
# Cross-DB connection for Q0/Q2/Q3 (in-memory, both files attached READ-ONLY).
# ---------------------------------------------------------------------------
con = duckdb.connect()
con.execute("PRAGMA memory_limit='8GB'")
con.execute(f"ATTACH '{SNAP}' AS snap (READ_ONLY)")
con.execute(f"ATTACH '{POST}' AS post (READ_ONLY)")

print(f"snap = {SNAP}")
print(f"post = {POST}")

# ---- Q0: orientation / sanity --------------------------------------------
hr("Q0  Orientation  (snap must show MORE hom-only ref==alt than post)")
o_rows = []
for db in ("snap", "post"):
    r = con.execute(f"""
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE ref_allele = alt_allele),
            COUNT(*) FILTER (WHERE ref_allele <> alt_allele),
            COUNT(DISTINCT rsid) FILTER (WHERE rsid IS NOT NULL),
            (SELECT current_source_version_id FROM {db}.annotation_sources
              WHERE source_db='dbsnp'),
            (SELECT COUNT(*) FROM {db}.variant_aliases)
        FROM {db}.variants_master
    """).fetchone()
    o_rows.append((db, *r))
table(o_rows, ["db", "vm_rows", "hom_only(ref==alt)", "genuine(ref<>alt)",
               "distinct_rsid", "dbsnp_ptr", "alias_rows"])
print("  NOTE: if snap.hom_only is NOT greater than post.hom_only, you paired the"
      "\n        wrong snapshot — re-run with the other .bak as argv[1].")

# ---------------------------------------------------------------------------
# Q2 (PIVOT): imputed-variant rsid representation
# ---------------------------------------------------------------------------
KIND = ("CASE WHEN rsid IS NULL THEN 'NULL' "
        "WHEN rsid='.' THEN 'dot(.)' WHEN rsid='' THEN 'empty' "
        "WHEN rsid LIKE 'rs%' THEN 'rs#' "
        "WHEN rsid LIKE 'i%' THEN 'i#(chip-internal)' ELSE 'other' END")

hr("Q2  PIVOT — rsid kind among IMPUTED-ONLY variants "
   "(has_imputed_call AND NOT has_genotyped_call)")
for db in ("snap", "post"):
    print(f"\n-- {db} --")
    rows = con.execute(f"""
        SELECT {KIND} AS kind, COUNT(*) n
        FROM {db}.variants_master
        WHERE has_imputed_call AND NOT has_genotyped_call
        GROUP BY 1 ORDER BY n DESC
    """).fetchall()
    table(rows, ["rsid_kind", "n"])
print("\n  DECISION: 'dot(.)' dominant  -> SENTINEL world  (survivors_enriched=0 is a BUG)")
print("            'rs#'   dominant  -> MERGE-SYNONYM world (=0 is correct)")

hr("Q2b  Unfilled-survivor signature: variants WITH a chip call, by rsid kind")
for db in ("snap", "post"):
    print(f"\n-- {db}  (has_genotyped_call = TRUE) --")
    rows = con.execute(f"""
        SELECT has_imputed_call AS also_imputed, {KIND} AS kind, COUNT(*) n
        FROM {db}.variants_master
        WHERE has_genotyped_call
        GROUP BY 1,2 ORDER BY n DESC
    """).fetchall()
    table(rows, ["also_imputed", "rsid_kind", "n"])
print("\n  A chip variant should carry its chip rsid. In SNAP, chip variants with"
      "\n  rsid '.'/NULL ~ 0. In POST, a large (also_imputed=TRUE, kind=dot/NULL)"
      "\n  bucket = reuse survivors left UNFILLED (the chip rsid was swallowed).")


# ---------------------------------------------------------------------------
# Q3: gwas / pharmgkb  drop  =  dedup  vs  genuine loss
# ---------------------------------------------------------------------------
def decompose(label: str, match_rsids_sql: str, baseline: int) -> None:
    hr(f"Q3  {label}: dedup vs genuine-loss decomposition  "
       f"(finding-018 baseline {baseline:,})")
    con.execute(f"CREATE OR REPLACE TEMP TABLE m_rsids AS {match_rsids_sql}")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE jj AS
        WITH pre AS (
            SELECT vm.rsid, COUNT(*) n_pre
            FROM snap.variants_master vm JOIN m_rsids g ON g.rsid = vm.rsid
            GROUP BY vm.rsid),
        pst AS (
            SELECT vm.rsid, COUNT(*) n_post
            FROM post.variants_master vm JOIN m_rsids g ON g.rsid = vm.rsid
            GROUP BY vm.rsid)
        SELECT g.rsid,
               COALESCE(pre.n_pre,0)  AS n_pre,
               COALESCE(pst.n_post,0) AS n_post
        FROM m_rsids g
        LEFT JOIN pre ON pre.rsid = g.rsid
        LEFT JOIN pst ON pst.rsid = g.rsid
    """)
    r = con.execute("""
        SELECT
            SUM(n_pre)  AS matches_pre,
            SUM(n_post) AS matches_post,
            SUM(n_pre - n_post) AS total_drop,
            SUM(CASE WHEN n_post=0 AND n_pre>0 THEN n_pre ELSE 0 END) AS genuine_loss,
            COUNT(*) FILTER (WHERE n_post=0 AND n_pre>0)              AS rsids_vanished,
            SUM(CASE WHEN n_post>0 AND n_post<n_pre THEN n_pre-n_post ELSE 0 END) AS dedup
        FROM jj
    """).fetchone()
    table([r], ["matches_pre", "matches_post", "total_drop",
                "genuine_loss", "rsids_vanished", "dedup"])
    print(f"  (matches_pre should reproduce ~{baseline:,})")
    # Layer 3: of the vanished rsIDs, how many survive POST under a merge-synonym?
    a = con.execute("""
        WITH vanished AS (SELECT rsid FROM jj WHERE n_post=0 AND n_pre>0),
        mapped AS (
            SELECT v.rsid AS old_rsid,
                   COALESCE(va.current_rsid, v.rsid) AS current_rsid
            FROM vanished v
            LEFT JOIN snap.variant_aliases va
              ON va.alias_rsid = v.rsid
             AND va.source_version_id = (SELECT current_source_version_id
                                          FROM snap.annotation_sources
                                          WHERE source_db='dbsnp')),
        post_rsids AS (SELECT DISTINCT rsid FROM post.variants_master
                        WHERE rsid IS NOT NULL)
        SELECT
            COUNT(*) AS vanished_total,
            COUNT(*) FILTER (WHERE m.current_rsid <> m.old_rsid) AS had_alias,
            COUNT(*) FILTER (WHERE p.rsid IS NOT NULL)           AS synonym_present_post
        FROM mapped m LEFT JOIN post_rsids p ON p.rsid = m.current_rsid
    """).fetchone()
    table([a], ["vanished_total", "had_alias_mapping", "synonym_present_post"])
    print("  genuine_loss with synonym_present_post  -> benign RENAME (recoverable)")
    print("  genuine_loss without it                 -> irreducible biology loss")


decompose(
    "GWAS",
    """SELECT DISTINCT gw.rsid AS rsid
       FROM snap.gwas_catalog_associations gw
       JOIN snap.annotation_sources s
         ON s.source_db='gwas_catalog'
        AND s.current_source_version_id = gw.source_version_id
       WHERE gw.rsid IS NOT NULL AND gw.trait_name IS NOT NULL""",
    66_726,
)
decompose(
    "PharmGKB",
    """SELECT DISTINCT pg.rsid AS rsid
       FROM snap.pharmgkb_annotations pg
       JOIN snap.annotation_sources s
         ON s.source_db='pharmgkb'
        AND s.current_source_version_id = pg.source_version_id
       WHERE pg.rsid IS NOT NULL""",
    1_737,
)
print("\n  DECISION: genuine_loss ~ 0            -> pure dedup, RE-LOCK LOWER and move on")
print("            genuine_loss large (~drop)  -> REGRESSION, PR-4 owns the '.'->NULL fix")

con.close()

# ---------------------------------------------------------------------------
# Q1: REPLAY the real canonicalize mapping against the read-only snapshot.
#     Must reproduce survivors_enriched=0 and rsid_conflicts=115,700.
# ---------------------------------------------------------------------------
hr("Q1  REPLAY canonicalize mapping on snapshot (read-only)")
try:
    sc = duckdb.connect(str(SNAP), read_only=True)
    sc.execute("PRAGMA memory_limit='8GB'")
    _create_temp_tables(sc)
    sc.execute(_BUILD_CANON_MAP_SQL)
    sc.execute(_BUILD_RESOLVE_SQL)
    sc.execute(_BUILD_REMAP_SQL)
    sc.execute(_BUILD_CANON_BEST_SQL)
except duckdb.Error as e:  # read-only temp-table write refused (rare)
    sys.exit(f"\nReplay could not build temp tables on a read-only snapshot:\n  {e}\n"
             f"Fallback: cp the .bak to a scratch path and re-run with that path as\n"
             f"argv[1] after editing the read_only=True flag in this block.")

enriched, conflicts = _count_rsid_metadata(sc)
print(f"  replay survivors_enriched = {enriched}   (run reported 0)")
print(f"  replay rsid_conflicts     = {conflicts:,}   (run reported 115,700)")
if conflicts != 115_700:
    print("  !! does NOT match 115,700 — wrong snapshot for this live DB; "
          "try the other .bak as argv[1].")

hr("Q1a  Conflict classification (sentinel artifact vs genuine vs other)")
rows = sc.execute("""
    WITH mover_agg AS (
        SELECT rm.new_variant_id AS survivor_id,
               COUNT(DISTINCT vm.rsid) FILTER
                   (WHERE vm.rsid IS NOT NULL AND vm.rsid<>'.') AS n_nondot,
               BOOL_OR(vm.rsid='.') AS mover_has_dot
        FROM _canon_remap rm JOIN variants_master vm ON vm.variant_id=rm.old_variant_id
        GROUP BY rm.new_variant_id),
    conf AS (
        SELECT r.survivor_id, r.survivor_is_new, b.distinct_rsids, b.best_rsid,
               vm.rsid AS surv_rsid
        FROM _canon_resolve r JOIN _canon_best b ON b.survivor_id=r.survivor_id
        LEFT JOIN variants_master vm
               ON vm.variant_id=r.survivor_id AND NOT r.survivor_is_new
        WHERE b.distinct_rsids>1
           OR (NOT r.survivor_is_new AND vm.rsid IS NOT NULL
               AND b.best_rsid IS NOT NULL AND vm.rsid<>b.best_rsid))
    SELECT
      CASE
        WHEN m.n_nondot>=2 THEN '2+ genuine rsIDs collide (REAL)'
        WHEN c.surv_rsid='.' THEN 'reuse survivor=. vs real mover rsID (SENTINEL)'
        WHEN m.mover_has_dot AND COALESCE(m.n_nondot,0)<=1
             THEN 'mover set {., one real rsID} (SENTINEL)'
        WHEN c.surv_rsid IS NOT NULL AND c.surv_rsid NOT LIKE 'rs%' AND c.surv_rsid<>'.'
             THEN 'reuse survivor non-rs id (i#/other)'
        ELSE 'other' END AS conflict_class,
      COUNT(*) n
    FROM conf c LEFT JOIN mover_agg m ON m.survivor_id=c.survivor_id
    GROUP BY 1 ORDER BY n DESC
""").fetchall()
table(rows, ["conflict_class", "n"])

hr("Q1b  Source breakdown of conflicted survivors (chip / imputed)")
rows = sc.execute("""
    WITH conf AS (
        SELECT r.survivor_id, r.survivor_is_new, b.best_rsid, vm.rsid AS surv_rsid,
               b.distinct_rsids
        FROM _canon_resolve r JOIN _canon_best b ON b.survivor_id=r.survivor_id
        LEFT JOIN variants_master vm
               ON vm.variant_id=r.survivor_id AND NOT r.survivor_is_new
        WHERE b.distinct_rsids>1
           OR (NOT r.survivor_is_new AND vm.rsid IS NOT NULL
               AND b.best_rsid IS NOT NULL AND vm.rsid<>b.best_rsid)),
    mover_src AS (
        SELECT rm.new_variant_id AS survivor_id,
               BOOL_OR(gc.source='23andme')  m23,
               BOOL_OR(gc.source='ancestry') manc,
               BOOL_OR(gc.source IN ('beagle_imputed','topmed_imputed')) mimp
        FROM _canon_remap rm JOIN genotype_calls gc ON gc.variant_id=rm.old_variant_id
        GROUP BY rm.new_variant_id),
    surv_src AS (
        SELECT gc.variant_id AS survivor_id,
               BOOL_OR(gc.source IN ('23andme','ancestry')) schip,
               BOOL_OR(gc.source IN ('beagle_imputed','topmed_imputed')) simp
        FROM genotype_calls gc GROUP BY gc.variant_id)
    SELECT
      CASE WHEN c.survivor_is_new THEN 'new-allocated survivor'
           WHEN ss.simp AND NOT ss.schip THEN 'reuse: imputed-only survivor'
           WHEN ss.schip AND ss.simp THEN 'reuse: chip+imputed survivor'
           WHEN ss.schip THEN 'reuse: chip-only survivor'
           ELSE 'reuse: (no calls)' END AS survivor_kind,
      COALESCE(ms.m23 AND ms.manc, FALSE) AS movers_chip_vs_chip,
      COALESCE(ms.mimp, FALSE)            AS movers_include_imputed,
      COUNT(*) n
    FROM conf c
    LEFT JOIN surv_src  ss ON ss.survivor_id=c.survivor_id
    LEFT JOIN mover_src ms ON ms.survivor_id=c.survivor_id
    GROUP BY 1,2,3 ORDER BY n DESC LIMIT 30
""").fetchall()
table(rows, ["survivor_kind", "movers_chip_vs_chip", "movers_incl_imputed", "n"])

hr("Q1c  Merged-pairs: of survivors with 2+ real rsIDs, how many are one locus "
   "(alias-equivalent) vs truly distinct loci")
rows = sc.execute("""
    WITH resolved AS (
        SELECT rm.new_variant_id AS survivor_id, vm.rsid AS raw_rsid,
               COALESCE(va.current_rsid, vm.rsid) AS canon_rsid
        FROM _canon_remap rm
        JOIN variants_master vm ON vm.variant_id=rm.old_variant_id AND vm.rsid LIKE 'rs%'
        LEFT JOIN variant_aliases va
          ON va.alias_rsid = vm.rsid
         AND va.source_version_id = (SELECT current_source_version_id
                                      FROM annotation_sources WHERE source_db='dbsnp')),
    per_surv AS (
        SELECT survivor_id, COUNT(DISTINCT raw_rsid) draw,
               COUNT(DISTINCT canon_rsid) dcanon
        FROM resolved GROUP BY survivor_id HAVING COUNT(DISTINCT raw_rsid)>1)
    SELECT COUNT(*) survivors_with_2plus_real_rsids,
           COUNT(*) FILTER (WHERE dcanon=1) merged_pairs_same_locus,
           COUNT(*) FILTER (WHERE dcanon>1) truly_distinct_loci
    FROM per_surv
""").fetchall()
table(rows, ["2+real_rsid_survivors", "merged_pairs_same_locus", "truly_distinct_loci"])

# ---------------------------------------------------------------------------
# WHAT-IF: treat '.' as NULL (only meaningful if Q2 = sentinel world).
# ---------------------------------------------------------------------------
hr("WHAT-IF  recompute treating '.' as NULL (the PR-4 fix preview)")
sc.execute("""
    CREATE OR REPLACE TEMP TABLE _canon_best_nd AS
    SELECT rm.new_variant_id AS survivor_id,
           arg_min(vm.rsid, vm.variant_id)
               FILTER (WHERE vm.rsid IS NOT NULL AND vm.rsid<>'.') AS best_rsid_nd,
           COUNT(DISTINCT vm.rsid)
               FILTER (WHERE vm.rsid IS NOT NULL AND vm.rsid<>'.') AS distinct_nd
    FROM _canon_remap rm JOIN variants_master vm ON vm.variant_id=rm.old_variant_id
    GROUP BY rm.new_variant_id
""")
ef = sc.execute("""
    SELECT COUNT(*) FROM _canon_resolve r
    JOIN _canon_best_nd b ON b.survivor_id=r.survivor_id
    JOIN variants_master vm ON vm.variant_id=r.survivor_id
    WHERE NOT r.survivor_is_new
      AND (vm.rsid IS NULL OR vm.rsid='.')
      AND b.best_rsid_nd IS NOT NULL
""").fetchone()[0]
cf = sc.execute("""
    SELECT COUNT(*) FROM _canon_resolve r
    JOIN _canon_best_nd b ON b.survivor_id=r.survivor_id
    LEFT JOIN variants_master vm
           ON vm.variant_id=r.survivor_id AND NOT r.survivor_is_new
    WHERE b.distinct_nd>1
       OR (NOT r.survivor_is_new AND vm.rsid IS NOT NULL AND vm.rsid<>'.'
           AND b.best_rsid_nd IS NOT NULL AND vm.rsid<>b.best_rsid_nd)
""").fetchone()[0]
print(f"  survivors_enriched (now=0)        -> with '.'->NULL: {ef:,}")
print(f"  rsid_conflicts     (now=115,700)  -> with '.'->NULL: {cf:,}")
print("  Reading: large enriched_fixed + small conflicts_fixed confirms the 115,700")
print("  are mostly '.' noise and ~that-many rsIDs are rescuable. The gwas/pharmgkb")
print("  'genuine_loss' from Q3 is what the fix recovers (returns toward baseline).")
sc.close()
