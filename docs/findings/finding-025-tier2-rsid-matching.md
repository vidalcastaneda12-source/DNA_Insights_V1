---
type: both
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-06-11
supersedes: []
superseded_by: []
---
# finding-025 — Tier-2 rsID matching in `refresh-index` (PR 4)

## Summary

PR 4 (RM-34cb101, Phase 6 → Prerequisites) makes the two rsID-keyed legs of the
`variant_annotations_index` build (GWAS Catalog, PharmGKB) resolve merged-away
("stale") rsIDs through the `variant_aliases` map that PR 2 populated
(finding-019). Both the user side (`variants_master.rsid`) and the source side
(`gwas_catalog_associations.rsid` / `pharmgkb_annotations.rsid`) are
canonicalized to their dbSNP merge survivor before the join, so a variant
carrying a stale rsID still matches an annotation keyed on the current rsID — and
vice-versa. This closes finding-005 #4 (the one merge tier Phase 3 left
unimplemented because `variant_aliases` was empty). Coordinate-keyed sources
(ClinVar, gnomAD) are untouched — rsID merges are irrelevant to a
`(chrom,pos,ref,alt)` join.

## Design

Two leading CTEs added to `index_refresh._BUILD_SQL`; only `gwas_roll` and
`pharmgkb_roll` change.

- **`alias_map`** — the active-dbSNP-epoch map `(alias_rsid → current_rsid)`,
  filtered via the `annotation_sources` pointer and **`GROUP BY alias_rsid`**.
  The GROUP BY makes the map provably 1:1 on the join key even though
  `variant_aliases.alias_rsid` carries no UNIQUE constraint (the loader dedups at
  write time, but that is a runtime invariant, not a DB guarantee). The gate's
  "one survivor per alias" check (below) confirms `ANY_VALUE` is deterministic on
  the real map.
- **`vm_canon`** — `variants_master` with each rsID canonicalized to its
  survivor: `COALESCE(alias_map.current_rsid, vm.rsid)`. Non-`rs`/synthetic/NULL
  rsIDs find no alias and pass through unchanged.

Each rsID leg `LEFT JOIN`s `alias_map` on the *source* rsID and joins `vm_canon`
on `canon_rsid = COALESCE(alias_map.current_rsid, source.rsid)` — both sides
canonicalized. The aggregates are unchanged (`COUNT(DISTINCT)`, `MIN`, `arg_min`,
`list_distinct(array_agg)`), all fan-out-safe.

**Monotonic / graceful degradation.** `canon(x) = COALESCE(map[x], x)` is the
identity when `x` is not an alias, so every prior tier-1 equality survives (no
match lost); the only new equalities are stale↔survivor collapses. When dbSNP is
unloaded `alias_map` is empty and both legs reduce *exactly* to the prior
`vm.rsid = source.rsid` join — every pre-existing index test passes unchanged,
with no dbSNP fixture.

**Single-hop.** Resolution is one hop because dbSNP's `current_rsid` (RsMergeArch
`rsCurrent`) is the pre-collapsed transitive survivor (finding-019 §1) — proven
on the real dbSNP-157 map by the gate check below.

## Provenance

The index's rsID matching now depends on the dbSNP alias epoch, so dbSNP's
version is recorded in the per-row `refresh_versions` JSON (a durable column,
`ddl/group_2_annotations.sql:498`) via a new `_PROVENANCE_SOURCES` constant.
`_VARIANT_LINKABLE_SOURCES` (the column contributors) is unchanged — dbSNP
contributes *matching*, not a column.

## `tier2_rsid_lifts` — a sentinel, not a count

`IndexRefreshResult` gains `tier2_rsid_lifts`: distinct indexed variants whose
own rsID is a merged-away alias and that carry a GWAS/PharmGKB annotation. It is
a **direction-1 path-fired sentinel** — `> 0` proves the tier-2 path fired. It is
**not** the recovered-variant count and is not a clean bound on it: it excludes
direction-2 lifts (where the *source* carried the stale rsID) and includes any
direction-1 variant that already matched under tier-1. The recovered count is the
per-leg `*_matches` delta. Reported this way so the docs do not repeat
finding-020's walked-back-magnitude error.

## Real-data gate

User corpus; dbSNP `157`, ClinVar `2026_06_08`, GWAS `2026_06_01`, gnomAD
`4.1.1`, PharmGKB `2025_07_05`.

**Map-integrity pre-checks** (both **0** on the active dbSNP-157 map):

- single-hop / terminal-survivor (`current_rsid` that is itself an `alias_rsid`) = **0**.
- one survivor per alias (`alias_rsid` with >1 distinct `current_rsid`) = **0**.

**Index anchors** (`genome annotate refresh-index`):

| anchor | pre-PR-4 (post-PR-3 lock) | post-PR-4 | Δ |
|---|---|---|---|
| `gwas_matches` | 66,701 | **66,764** | **+63** |
| `pharmgkb_matches` | 1,737 | **1,738** | **+1** |
| `gnomad_matches` | 2,796,952 | 2,796,952 | 0 (coord-keyed) |
| `clinvar_matches` | 61,458 | 61,458 | 0 (coord-keyed) |
| `is_rare` | 163,160 | 163,160 | 0 |
| `is_ultrarare` | 103,261 | 103,261 | 0 |
| `row_count` | 2,824,229 | **2,824,236** | **+7** |

The per-leg deltas were **predicted exactly** before the run by a
canonical-match-set − raw-match-set query (the `raw` reproduction returned
66,701 / 1,737, equal to the live baseline) and confirmed exactly after.

**Recovered variants (the honest magnitude): 64 distinct variants** newly gain a
GWAS/PharmGKB annotation:

- GWAS +63 = **28 direction-1** (user carried the stale rsID) + **35 direction-2**
  (the GWAS row carried the stale rsID; the user had the current one).
- PharmGKB +1 = direction-1.
- Of the 64, only **7** were not already in the index via another source (hence
  `row_count` +7); the rest already had a gnomAD/ClinVar/other-rsID row.

Direction-2 (35) exceeding direction-1 (28) on the GWAS leg confirms both-sided
canonicalization is load-bearing — GWAS Catalog carries more stale rsIDs mapping
to the user's current rsIDs than the user carries stale rsIDs with GWAS hits.

`tier2_rsid_lifts` (sentinel) = **48** — neither the recovered count (64) nor the
direction-1-genuine count (29), exactly as the sentinel's definition predicts.

These are the post-PR-4 regression anchors; drift on a re-run against the same
corpus + source versions is a regression signal.

## Wall-clock — pre-existing, not a PR-4 regression

`refresh-index` on the full post-PR-3 corpus took **~120 s** (build/INSERT
**7.5 s**; the DuckDB transaction **commit of the 2.8M-row rebuild took 111 s**;
checkpoint 0 s). The build query — including the new `alias_map`/`vm_canon`
joins — is the 7.5 s portion; the 111 s commit is the cost of persisting a full
2.8M-row index replace on the 5.5 GB file and is **independent of PR 4** (the
inserted row count moved by +7). This exceeds the ~30 s routine-refresh target
(CLAUDE.md), but the ~2.2 s figure in observation #4 was the *pre-canonicalization*
159,658-row build; at the post-PR-3 2.8M-row volume the commit dominates.
Flagged as a pre-existing performance follow-up (incremental index update, or
treat `refresh-index` as a gated post-backfill step) — out of scope for PR 4.

## Out of scope

- `merge` / `consensus` tier-2 cross-chip pairing (subsumed by PR 3 canonicalization; residual strand-flip collapse is PR 5).
- Recursive multi-hop alias resolution (single-hop is complete per the gate check; recursion is only warranted if that check ever returns nonzero).
- The ~120 s `refresh-index` commit cost (pre-existing; performance follow-up).
