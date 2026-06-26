---
type: both
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-06-09
supersedes: []
superseded_by: []
---
# Finding 021 — Synthetic chr:pos:ref:alt IDs in variants_master.rsid from imputation ingest

## Context

Phase-4 imputation ingest (`backend/src/genome/imputation/ingest.py`) streams the
per-chromosome Beagle 5.5 output VCFs and bulk-loads them into `variants_master`
and `genotype_calls`. At the variant-write site the VCF `ID` column was copied
verbatim into `variants_master.rsid`.

Beagle 5.5 against the 1000 Genomes Phase 3 panel emits a synthetic
`chrom:pos:ref:alt` identifier (e.g. `14:29619977:C:T`) in the VCF `ID` field for
any variant with no dbSNP rsID in the panel. Copied verbatim, the ~2.26M
imputed-only rows (the `imputed_only = 2,267,751` consensus bucket, finding-007)
carried a coordinate string in `rsid` rather than a real `rs<n>` or NULL.

The defect was latent through Phase 4 and most of Phase 5 — nothing read `rsid` as
a join key — until the PR-3 canonical REF/ALT backfill (finding-020) turned `rsid`
into load-bearing data. (This finding is numbered 021, leaving 020 to the
concurrent PR-3 canonicalize work it depends on.)

## Observation

PR-3's canonicalize collapses variant rows during the canonical REF/ALT backfill.
When a chip variant re-orients onto a pre-existing imputed survivor row,
canonicalize enriches the survivor's `rsid` from the chip call — but only when the
survivor's `rsid IS NULL` (the `_ENRICH_REUSE_RSID_SQL` guard). Because the
imputed survivor held a non-NULL synthetic string, the guard skipped enrichment
and the chip variant's real `rs<n>` was dropped.

The downstream blast radius was annotation-wide and genuine, not noise:

- `gwas_matches`: 66,726 → 55,047
- `pharmgkb_matches`: 1,737 → 1,411

(~99.8% real-rsID loss on the rsid-keyed sources; the coord-keyed gnomAD/ClinVar
sources are unaffected.) These are distinct from the related collapse metrics —
`rows_collapsed` (115,726), `rsid_conflicts` (~115,700), and "~115,662 distinct
rsIDs lost" each measure a different thing and must not be conflated.

A read-only fix preview (synthetic IDs NULLed at the source) recovered essentially
the entire loss. A sweep of every reader of `variants_master.rsid` — seven call
sites (the `index_refresh.py` GWAS and PharmGKB joins, `variant_aliases.py` ×3,
the `canonicalize.py` coalescing, and an ingest docstring) — confirmed none parse
or depend on the synthetic `chr:pos:ref:alt` format, so NULLing is safe.

## Implication

The root cause is at ingest, not canonicalize: dirty input (synthetic IDs
masquerading as rsIDs) defeated an otherwise-correct enrichment guard. The fix is
source-level and ships in two parts, both in this PR:

1. **Recurrence prevention — a strict predicate at the assignment site.** The VCF
   `ID` is stored only when it matches `^rs[0-9]+$` (a real dbSNP `rs<n>`), else
   NULL (`_dbsnp_rsid_or_none`). Future imports never persist a synthetic ID.

2. **One-time remediation — a standalone idempotent sweep** (`genome imputation
   normalize-rsids`) that NULLs the already-persisted synthetic strings. The sweep
   is **positively** scoped to the `chrom:pos:ref:alt` format — never the negation
   of `^rs[0-9]+$` — so real `rs<n>` and chip-internal `i####` IDs (which carry no
   colon) are left untouched. The sweep NULLs exactly the regex-matched coordinate
   rows and logs the non-matched remainder of the non-`rs` / non-`i` / non-`.` /
   non-NULL population (count + a bounded sample) rather than aborting — that
   remainder is legitimate chip-probe IDs, not synthetic (see **Amendment** below).
   Because the bulk UPDATE rewrites the indexed
   (`idx_vm_rsid`), FK-referenced `rsid` column, the sweep drops the index
   (committed) before the UPDATE and rebuilds it in a `finally` — DuckDB
   delete+reinserts a row when an indexed column changes, which would otherwise
   trip the `genotype_calls.variant_id` parent check against pre-transaction state
   (the same quirk the canonicalize backfill handles).

`--force-reimport` is **not** the cleaning mechanism. The import upsert
(`_upsert_variants_master`) inserts only variants not already present
(`WHERE vm.variant_id IS NULL`) and nothing in the re-import path rewrites an
existing row's `rsid`, so a re-import of the same corpus would leave every
persisted synthetic string in place. This is a data cleanup, not a schema rebuild.

NULL is lossless. The synthetic `chr:pos:ref:alt` is fully reconstructable from
the `chrom` / `pos` / `ref` / `alt` columns, and it does not belong in
`variant_aliases`, which records dbSNP `RsMergeArch` merge-history (finding-019),
not coordinate strings.

PR-3's canonicalize-side coalescing (`arg_min(rsid, variant_id) FILTER (WHERE rsid
IS NOT NULL)` plus the `rsid_conflicts` counter, commit 9deb08c) is **retained** —
it is the correct handler for genuine multi-rsID collapses, a separate real case
from the synthetic-ID class. On clean data the `IS NULL` guard now passes for
ex-synthetic survivors, so the real `rs<n>` is adopted.

The structural anchors (942,620 consensus / 120,516 shared / 1.0000 concordance /
106 strand flips / 31 palindromic) are a fixed negative control. The fix touches
imputation rsid hygiene only — nothing chip-merge-derived — so they must not move.

## Follow-up

- This PR (ingest hygiene, branched from `main`; not PR-4) is gated only on
  data-cleanliness: after the sweep, synthetic-format rsids = 0; the distinct
  `rs<n>` count is unchanged (927,964); imputed-only `rsid` is NULL or a real
  `rs<n>`; the structural anchors are unmoved.
- PR-3 re-verification, after PR-3 rebases onto this merged fix: `gwas_matches` →
  66,726, `pharmgkb_matches` → 1,737, `rsid_conflicts` → 0, rsID invariant
  0-lost. **Superseded by the second Amendment below** — the gate measured
  `gwas_matches` 66,701, `rsid_conflicts` 1 (one genuine collision survives the
  #66 sweep); `pharmgkb_matches` 1,737 and the rsID invariant held.
- Deferred (no V1 action): surfacing synthetic-ID provenance is unnecessary — it
  is reconstructable on demand and unused.

## Amendment — guard relaxed to positive-match-and-log (PR #66 real-data gate)

The sweep originally used a pre-flight *equality* check: it aborted unless the
coordinate-regex match count equaled the non-`rs` / non-`i` / non-`.` / non-NULL
complement. Real-data verification showed that invariant is too strict. Against the
user's corpus the regex matched 2,267,751 synthetic IDs while the complement was
2,267,767 — a 16-row gap.

Those 16 are **not** synthetic. They are legitimate chip probe IDs carried in by
23andMe/Ancestry ingest — Illumina 1000G-Project probe names (`kgp1851883`,
`kgp9989353`), a vendor probe ID (`VGXS34713`), and Ancestry.com-internal IDs
(`acom_1kg_6_30078939`, `acom_rs201205097`, the last embedding a valid rsID). They
are real variant identifiers, not `chrom:pos:ref:alt` strings. The coordinate regex
correctly excludes them; the bug was the guard's invariant, not the regex.

The sweep is therefore coordinate-scoped, not "all non-`rs`." The equality check is
removed; the sweep NULLs exactly the coordinate-matched rows and **logs** the
leftover (count + a bounded distinct sample) on the `imputation.normalize_rsids.preflight`
event for visibility instead of aborting. The leftover is logged on every run —
including the idempotent no-op re-run where `matched == 0` — so the chip-probe
residue stays observable in steady state. Recovering these probe IDs to canonical
rsIDs (`kgp`→`rs`, unwrapping `acom_rs…`) is alias-normalization, deferred to
`variant_aliases` (the pre-Phase-6 **PR 14** slot, ROADMAP — PR 4's merged-rsID
resolution, finding-025, did not cover this alias-format normalization); the ingest predicate `_dbsnp_rsid_or_none` is unchanged
(it is correct and real-data-confirmed).

## Amendment — one genuine `rsid_conflicts` survives the #66 sweep (PR-3 canonicalize gate)

The Follow-up above predicted PR-3 re-verification would land `rsid_conflicts → 0`
— reasoning that once #66 NULLed the synthetic `chrom:pos:ref:alt` strings, the
dominant collision class (a chip swap-victim's real `rs<n>` colliding with an
imputed survivor's synthetic string) would vanish and leave the coalescing with
nothing to do. The canonicalize gate confirmed the *dominant* half of that
prediction and corrected the rest:

- **`rsid_conflicts` = 1, not 0.** #66 removed ~115,700 *synthetic* false-collisions
  (there the `arg_min` coalescing was choosing between a real `rs<n>` and a
  coordinate string — never a genuine conflict). But **one genuine collision
  remains**: a canonicalized key onto which two *distinct real* `rs<n>` values
  collapse (the `genuine_reorient` class). The coalescing resolves it
  deterministically (lowest-`variant_id` wins) and the loser is **warned**, never
  silently dropped (`canonicalize.rsid_conflicts`). This is exactly the case the
  coalescing exists for — so #66 made it *almost* redundant, not redundant.
  **Coalescing is retained-and-justified**, answering the open question of whether
  the #66 sweep obsoleted it.
- **`gwas_matches` = 66,701, not 66,726.** The rsID-preservation invariant held
  (post-run rsid set ⊇ pre-run set; zero net rsID loss), but `gwas_matches` still
  moved −23 vs the pre-canon swept count (66,724) — a *collapse-dedup* effect
  unrelated to rsID loss: two rows GWAS-matching on the same rsID collapse to one
  survivor, and the index keeps one row per `variant_id`. Reconciled in
  finding-020 "recon C", not here. `pharmgkb_matches` held at 1,737.

Net: the #66 fix did its job (synthetic-ID class eliminated, no real rsID lost to
the synthetic guard), and the residual `rsid_conflicts`=1 is a real, correctly
handled, genuinely-distinct-rsID collapse — not a regression and not synthetic
residue.
