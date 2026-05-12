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

6. **TopMed upload misses hom-only positions until canonical REF/ALT is
   loaded.** Phase 4's `genome imputation prepare` filters out variants
   where `ref_allele == alt_allele`, because Phase 2's alphabetical-ordering
   normalize sets both fields to the same base for positions where every
   observation is homozygous. TopMed cannot impute against `ref=A alt=A`
   rows, so they are dropped from the upload. In practice this excludes a
   large fraction of the chip — typical individuals are hom-ref at most
   common SNPs. Imputation still works against the polymorphic subset.
   *Recommended fix point:* Phase 5 dbSNP load populates `variant_aliases`
   with canonical REF/ALT; a follow-on prepare step can rewrite the
   filtered positions and recover the dropped rows. Tracked in the Phase 4
   runbook's "compression note" section.

## Implication

Each item is a known limitation with an identified fix point. They are not
blockers for the current phase but should be revisited as their respective
fix points arrive.

## Follow-up

Track each item against the recommended fix point. New deferred items
discovered in later phases should be appended here (or split into a
successor finding if this one grows unwieldy).
