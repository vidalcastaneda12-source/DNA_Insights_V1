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

6. **Imputation input misses hom-only positions until canonical REF/ALT is
   loaded.** Phase 4's `genome imputation prepare` filters out variants
   where `ref_allele == alt_allele`, because Phase 2's alphabetical-ordering
   normalize sets both fields to the same base for positions where every
   observation is homozygous. Imputation engines reject `ref=A alt=A`
   rows on input — they carry no allele to impute against — so they are
   dropped before the per-chromosome VCFs are emitted. In practice this
   excludes a large fraction of the chip — typical individuals are
   hom-ref at most common SNPs. Imputation still works against the
   polymorphic subset. (The observation was originally surfaced against
   the TopMed upload contract, but the same input requirement applies
   identically to the local Beagle 5.5 workflow that replaced it per
   `finding-006`.) *Recommended fix point:* Phase 5 dbSNP load populates
   `variant_aliases` with canonical REF/ALT; a follow-on prepare step
   can rewrite the filtered positions and recover the dropped rows.

7. **ClinVar supersession UPDATE+checkpoint dominates same-version
   `--force` re-runs (~28 min for ~9M rows).** Sub-phase 5.2 verification
   surfaced that the locked supersession pattern (CLAUDE.md #7) hits real
   friction at ClinVar scale: the chunked INSERT runs in ~5 min, but the
   subsequent UPDATE of the prior active set plus DuckDB's post-commit
   MVCC checkpoint takes ~23 min and emits no progress until COMMIT
   returns. See `finding-009-clinvar-supersession-checkpoint-cost.md` for
   the full breakdown, mechanism, rejected alternatives, and open
   questions. *Recommended fix point:* the action items in
   finding-009 #14 (explicit `CHECKPOINT`, progress logging, chunked-
   UPDATE design decision, `--skip-if-same-version` short-circuit) must
   be resolved before sub-phase 5.5 (gnomAD filtered) begins, since 5.5
   will exercise the same path at a larger row count.

## Implication

Each item is a known limitation with an identified fix point. They are not
blockers for the current phase but should be revisited as their respective
fix points arrive.

## Follow-up

Track each item against the recommended fix point. New deferred items
discovered in later phases should be appended here (or split into a
successor finding if this one grows unwieldy).
