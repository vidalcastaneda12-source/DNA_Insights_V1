---
type: both
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-05-12
supersedes: []
superseded_by: []
---
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
   *Recommended fix point:* the post-5.7 backfills slot — with dbSNP canonical
   REF/ALT loaded (5.6), a normalization step can consolidate the
   strand-flipped duplicates.
   *Status:* **closed — ordering aspect in PR 3 (finding-020), duplicate
   collapse in PR 5b (finding-026/027).** PR 3's `canonicalize-variants`
   canonicalized the ordering aspect (the ~101,918 alphabetical-swap victims
   dominant in finding-018). The residual duplicate aspect was **mis-scoped here
   as "the 106 tier-3 cases where the two chips stored complementary allele
   sets"** — that population was dissolved by PR-3 canonicalize + hom-recovery.
   Read-only measurement (finding-026) found the real post-canon residual is
   ≈684 duplicates across **five** mechanisms, dominated by **no-call `(N,N)`
   placeholders (≈661)**, not clean chip+chip strand-flips (of which there are
   **zero**): 660 no-call repoints + 1 no-call DROP + 10 chip+imputed REF/ALT
   swaps + 5 chip+imputed strand-flips + 5 hom-opposite-strand (incl. the single
   chr4 `disagreement_resolved`) + 3 hom-same-strand. PR 5b's
   `collapse-duplicate-variants` collapses all of them via per-edge
   reconciliation (repoint / complement via supersession / drop), leaving legit
   multi-allelics protected; the chip+imputed origin is finding-027. (PR 3 had
   left these as duplicate rows with `align-tier3-consensus` deleting the
   non-canonical-side consensus as an interim patch — now a no-op backstop.)

2. **Profile-level QC rollup.** The current `sample_qc` table is
   per-ingestion-run. A profile-level rollup that combines per-source
   inferences (e.g., resolving "23andMe says M, Ancestry says ambiguous" to
   a single profile-level "M") doesn't exist. *Recommended fix point:*
   Phase 6's genome-QC pipeline (consolidated there per finding-017).

3. **`het_outlier` threshold calibration across sources.** Currently QC
   passes without a strict outlier check. When introduced, the threshold
   must handle 23andMe v5 (~0.17), Ancestry v2 (~0.34), and post-imputation
   values (likely different again). *Recommended fix point:* When the
   threshold is first needed.

4. **Tier-2 rsID matching in merge.** Phase 3 implemented tier-1
   (chrom+pos+ref+alt) and tier-3 (fuzzy strand) but deferred tier-2 (rsID
   matching with merge resolution via `variant_aliases`). Currently the
   `variant_aliases` table isn't populated, so tier-2 has nothing to match
   against. *Recommended fix point:* the post-5.7 backfills slot — populate
   `variant_aliases` from dbSNP merge records (the dbSNP loader shipped in 5.6
   but left `variant_aliases` empty per finding-016 #8), then add tier-2 merge
   logic.

5. **ACMG SF severity escalation.** Phase 3's discrepancy detection writes
   base severity correctly but doesn't escalate to `critical` for variants
   in ACMG SF genes (the `is_acmg_sf` flag on `variants_master` isn't
   populated until Phase 6's ACMG SF detection pipeline). *Recommended fix
   point:* Phase 6 ACMG SF detection populates `is_acmg_sf` as its first task,
   then re-walks discrepancies and bumps severity once the flags are in place.

6. *(Status: shipped in PR 3 / finding-020.)* **Imputation input misses
   hom-only positions until canonical REF/ALT is loaded.** Phase 4's
   `genome imputation prepare` filters out variants
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
   `finding-006`.) *Recommended fix point:* the post-5.7 backfills slot — the
   dbSNP load (5.6) supplies canonical REF/ALT; a follow-on prepare step can
   rewrite the filtered positions and recover the dropped rows.

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

8. **Generalize the gwas_catalog hash-based fallback short-circuit.**
   `finding-014` documents an upstream label drift in EBI's GWAS Catalog
   stats endpoint that produced two different `version` labels
   (`2026_05_16` → `2026_04_27`) for byte-identical release content. The
   gwas_catalog loader gained a post-download hash-match fallback that
   short-circuits when the file SHA-256 matches the active row's recorded
   hash but the resolved label differs. The fallback is gwas_catalog-only
   for now; if a second Phase-5/6 loader exhibits the same drift,
   extract the comparison into a shared
   `maybe_skip_on_hash_match(source_db, version, hash, force)` helper in
   `genome.annotate.supersession` and adopt it across the affected loaders.
   *Recommended fix point:* the second time the pattern shows up.

9. **`pos_grch37` not coalesced across canonicalize collapse.**
   `genome annotate canonicalize-variants`'s new-survivor INSERT
   (`_INSERT_NEW_SURVIVORS_SQL`) inherits only the `MIN(old_variant_id)`
   representative's `pos_grch37`. Where the rows collapsing onto one canonical
   key carry divergent `pos_grch37` values — or where a NULL-GRCh37
   representative (e.g. an imputed-only survivor) absorbs movers that do carry a
   GRCh37 coordinate — the non-representative GRCh37 coordinate is dropped, not
   coalesced. This is a deliberate deferral, not an oversight: unlike the rsID,
   an opaque identifier the collapse can `arg_min` across movers (the retained
   `_canon_best` coalescing), a GRCh37 coordinate is meaningless without the
   liftover chain that produced it — coalescing one would require re-running
   liftover to keep the coordinate bound to its
   `(grch38, grch37, liftover-provenance)` triple. The GRCh38 coordinate (the
   project's primary) is unaffected, and `consensus_genotypes` /
   `variant_annotations_index` key on GRCh38; only the alongside-stored GRCh37
   value is at issue. *Recommended fix point:* a re-liftover pass — fold into
   PR 5 (strand architecture, which already re-derives `genotype_calls` allele
   state via supersession) or a dedicated GRCh37-recoalesce step.

10. **Loader version label decouples from cached data on a rebuild reload
    (finding-022).** ClinVar and GWAS Catalog resolve their `version` label from a
    live network call (`_resolve_version_via_head` / `_resolve_version_via_stats`)
    placed *before* the skip-if-exists `download_to_cache`. On a fresh
    `rm -rf data/` rebuild that reloads from a preserved older cache, the label
    resolves to the *current upstream* (e.g. June) release while the loaded bytes
    are the *cached* (e.g. May) release — and finding-014's hash fallback cannot
    reconcile them because there is no active row yet. The DB version row is
    mislabeled; the data is correct (finding-022 has the full mechanism + the
    DB-vs-docs map). *Recommended fix point:* the next annotation-loader PR — bind
    the persisted label to the loaded bytes, either via a sidecar `<file>.version`
    written on a fresh download and read back on a cache-hit, or by generalizing
    finding-014's `maybe_skip_on_hash_match` to adopt the label of any prior
    `annotation_source_versions` row whose hash matches the cached file.

11. **GWAS `MAPPED_TRAIT_URI` truncated to a single EFO URI.** GWAS Catalog's
    `MAPPED_TRAIT_URI` cell can carry multiple comma-separated EFO URIs when an
    association is mapped to several EFO terms (e.g.
    `"...EFO_0000384,...EFO_0000729"`), but the schema's `mapped_trait_uri
    VARCHAR` is single-valued. The sub-phase 5.3 `gwas_catalog` loader therefore
    keeps the first URI (the curators' primary mapping), derives `trait_id` from
    that same first URI, and increments the `truncated_mapped_trait_uri`
    end-of-load counter on every truncation so the loss is surfaced rather than
    silent. This entry is parity documentation for the behavior already
    described in `docs/runbooks/annotations.md` (the "Single-value
    `mapped_trait_uri`" note, lines 747-756); it was deferred from sub-phase 5.3.
    *Recommended fix point:* a future schema change making `mapped_trait_uri` a
    `VARCHAR[]` array so all mapped EFO URIs are retained — a schema-doc edit +
    `ddl` re-extract + DB rebuild, captured (still deferred, do not execute
    opportunistically) as ROADMAP slot **RM-85121ee** under "Deferred schema
    changes (gated on next DB rebuild)". *Status:* **open** — deferred from 5.3.

## Implication

Each item is a known limitation with an identified fix point. They are not
blockers for the current phase but should be revisited as their respective
fix points arrive.

## Follow-up

Track each item against the recommended fix point. New deferred items
discovered in later phases should be appended here (or split into a
successor finding if this one grows unwieldy).
