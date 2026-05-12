# Deferred improvements identified during Phase 2 and Phase 3

## Context

Several improvements were identified but deferred to keep phase scope
manageable. This document tracks them so they aren't forgotten.

## Observation

1. **Duplicate `variants_master` rows for the same SNP on opposite
   strands.** Phase 2's tier-1 matching `(chrom, pos, ref, alt)` doesn't
   unify strand-flipped representations. Phase 3's tier-3 cross-row matching
   handles this at merge time (106 cases in real data), but the underlying
   duplication remains in `variants_master`. Each row joins independently to
   annotation sources, potentially duplicating insights at Phase 5.
   *Recommended fix point:* when Phase 5 annotation joins surface duplicate
   findings visibly, a normalization step at ingest can consolidate these.

2. **Profile-level QC rollup.** The current `sample_qc` table is
   per-ingestion-run. A profile-level rollup that combines per-source
   inferences (e.g., resolving "23andMe says M, Ancestry says ambiguous" to
   a single profile-level "M") doesn't exist. *Recommended fix point:*
   Phase 5 or whenever the user-facing summary view is built.

3. **`het_outlier` threshold calibration across sources.** Currently QC
   passes without a strict outlier check. When introduced, the threshold
   must handle 23andMe v5 (~0.17), Ancestry v2 (~0.34), and post-imputation
   values (likely different again). *Recommended fix point:* When the
   threshold is first needed.

4. **Tier-2 rsID matching in merge.** Phase 3 implemented tier-1
   (chrom+pos+ref+alt) and tier-3 (fuzzy strand) but deferred tier-2 (rsID
   matching with merge resolution via `variant_aliases`). Currently the
   `variant_aliases` table isn't populated, so tier-2 has nothing to match
   against. *Recommended fix point:* Phase 5 annotation loaders will
   populate `variant_aliases` from dbSNP merge records; tier-2 merge logic
   can be added then.

5. **ACMG SF severity escalation.** Phase 3's discrepancy detection writes
   base severity correctly but doesn't escalate to `critical` for variants
   in ACMG SF genes (the `is_acmg_sf` flag on `variants_master` isn't
   populated until Phase 5). *Recommended fix point:* Phase 5 enrichment
   job re-walks discrepancies and bumps severity once ACMG SF flags are in
   place.

## Implication

Each item is a known limitation with an identified fix point. They are not
blockers for the current phase but should be revisited as their respective
fix points arrive.

## Follow-up

Track each item against the recommended fix point. New deferred items
discovered in later phases should be appended here (or split into a
successor finding if this one grows unwieldy).
