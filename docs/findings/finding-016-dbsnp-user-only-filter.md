---
type: decision
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-05-26
supersedes: []
superseded_by: []
---
# Finding 016 — dbSNP filters to user variants only (not gnomAD's three/four-way overlap)

> **Status note (2026-06-21):** the gnomAD *contrast* in this finding — the
> §2 rationale ("a ClinVar/GWAS variant the user does not carry still wants its
> gnomAD AF"), §4's "gnomAD passes `three_way`", and the title's "(not gnomAD's
> three/four-way overlap)" — is **superseded by
> [finding-035](finding-035-gnomad-filter-set-consumer-audit.md)**: the consumer
> audit found nothing reads those rows, so gnomAD adopted `user_only` too
> (2026-06-21). gnomAD now uses the **same** filter as dbSNP; the three-way
> strategy is retained only as gnomAD's revert path. **finding-016's own
> conclusion — that dbSNP filters `user_only` — is unchanged and correct.**

## Context

1. The schema coverage-strategy table (`schema_group_2_reference_annotations.md`)
   listed dbSNP as **"Filtered to overlap — Same reasoning"**, i.e. the same
   coverage rule as gnomAD. CLAUDE.md "Things never to do" #3 spells that rule
   out *for gnomAD specifically*: "Never bulk-load gnomAD without filtering to
   the `(user ∪ ClinVar ∪ GWAS ∪ PGS)` intersection — full gnomAD is too
   large." The "Same reasoning" cell implied dbSNP should inherit the identical
   multi-source intersection.

2. dbSNP's role in this app differs from gnomAD's. gnomAD supplies *population
   allele frequencies*, consumed across the annotation layer — a ClinVar or
   GWAS variant the user does not carry still wants its gnomAD AF. dbSNP
   supplies *canonical variant identity*: rsID, canonical REF/ALT, gene
   symbols, and variant class. Its three concrete consumers all operate on the
   user's own variants in `variants_master`:

   * **rsID canonicalisation** — normalising `variants_master.rsid` against the
     current dbSNP build.
   * **hom-only REF/ALT recovery** (finding-005 #6) — recovering the alternate
     allele for homozygous-reference chip positions, which are user variants.
   * **tier-2 rsID merge matching** (finding-005 #4) — matching user variants
     by merged/withdrawn rsID via `variant_aliases`.

   None of these read dbSNP records at ClinVar/GWAS/PGS positions the user does
   not carry. (ClinVar and GWAS already carry their own rsIDs in their own
   tables.)

## Observation

3. Filtering dbSNP to `(user ∪ ClinVar ∪ GWAS ∪ PGS)` would load dbSNP rows at
   non-user positions that nothing in the current or planned design reads,
   inflating `dbsnp_annotations` with dead rows. The precise coverage for
   dbSNP's consumers is the **distinct `variants_master` positions** — the
   `user_only` leg alone.

4. The filter-set builder extracted at finding-012 #11
   (`genome.annotate.filter_set.build_filter_set`) is parameterised on
   `strategy`: gnomAD passed `"three_way"` (now `"user_only"`, finding-035),
   dbSNP passes `"user_only"`. The two
   strategies share the same `pos_grch38 > 0` sentinel guard and the same
   per-chrom bucketing; only the SQL legs differ. Switching dbSNP to a broader
   coverage later is a one-argument change, not a rewrite.

5. dbSNP's `SUPPORTED_CHROMS` is wider than gnomAD's: 1-22, X, **Y, and MT**.
   gnomAD v4 ships no high-confidence Y/MT AFs (so it skips them); dbSNP ships
   rsIDs for every canonical chromosome, and the user's 23andMe export carries
   Y + MT positions worth annotating. The allow-list is passed into
   `build_filter_set`, so each loader keeps its own.

## Implication

6. **PR B filters dbSNP `user_only`.** The schema coverage-table row is updated
   to "Filtered to user variants" with a cross-reference here. That prose edit
   is the deliberate, documented schema change CLAUDE.md "Things never to do"
   permits; it touches only the coverage-strategy table row, no `CREATE TABLE`
   block, so `ddl/group_2_annotations.sql` re-extracts byte-identical and no
   `rm -rf data/ && genome init` rebuild is implied.

7. **The ClinVar/GWAS/PGS legs are deferred, not forbidden.** If a future
   consumer needs dbSNP annotations at non-user positions, a `"three_way"` (or
   four-way) dbSNP refresh is the same one-argument change. The PGS leg
   specifically is deferred to **a Phase 6 follow-up gated on
   `pgs_score_weights`**, mirroring the gnomAD PGS extension (finding-011) —
   the two extensions are symmetric: same gate, same append-not-refresh shape.

8. **`variant_aliases` is not populated in PR B.** dbSNP governs two tables
   under one `annotation_sources` pointer (`dbsnp_annotations` and
   `variant_aliases`); PR B loads only the former. `variant_aliases` pairs with
   the tier-2 rsID backfill (finding-005 #4), Phase 6+. The `_next_alias_id`
   allocator (PR A) ships unused until then.

## Build-157 divergence (finding-013 gate)

9. The finding-013 verification gate ratified the real source as dbSNP
   **build 157** (`##dbSNP_BUILD_ID=157`, `##reference=GRCh38.p14`), not the
   build 156 the implementation brief assumed. Build 157 carries data the brief
   expected to be absent, forcing two projection decisions (confirmed with the
   user — "Option A"):

   * **`functional_class` → NULL in PR B.** Build 157 exposes only *legacy
     function-class flags* (`NSM`/`SYN`/`NSN`/`U3`/`U5`/`INT`/`ASS`/`DSS`/…),
     not a single VEP-grade consequence value. Mapping 11 sparse flags onto the
     4-value schema vocabulary (`missense`/`synonymous`/`intron`/`utr`) is
     lossy and is superseded by VEP's per-transcript consequences (Phase 6), so
     `functional_class` is left NULL pending VEP. The flags remain in the
     source if a coarse fallback is ever wanted.
   * **`is_clinical` ← presence of `CLNSIG`.** Build 157 carries the clinical
     INFO family (`CLNSIG`/`CLNDN`/`CLNREVSTAT`/…). `is_clinical` is `True` when
     a record carries `CLNSIG`, else `False` — a clean per-record boolean from
     one key, with no ClinVar join (the brief's "clinical key if the gate finds
     one" rule, and it did).

10. **`rsid` reads from `record.ID`, never `INFO/RS`.** Build 156+ emits `RS`
    INFO values exceeding 2³¹; htslib sets them to missing
    (`[W::vcf_parse_info] Extreme INFO/RS value encountered and set to
    missing`). The gate confirmed this empirically at `chr22:10510027`
    (`INFO/RS` missing, `ID = rs2517033109`). The VCF ID column is the
    canonical rsID.

## Follow-up

11. **The PGS leg** (a Phase 6 follow-up gated on `pgs_score_weights`) and the
    finding-005 dbSNP-dependent backfills (#1 canonical REF/ALT strand-flip
    dedupe and #6 hom-only recovery, now in the post-5.7 backfills slot; #4
    tier-2 rsID matching, which populates `variant_aliases`) are the consumers
    that may justify broader dbSNP coverage. When they land, revisit whether
    `user_only` still suffices.

12. **VEP supersedes `functional_class`.** When the Phase 6 VEP runner lands,
    `functional_class` is populated from VEP's per-transcript consequence, not
    from dbSNP's legacy flags. This finding can be marked historical once both
    the PGS leg and VEP have landed.
