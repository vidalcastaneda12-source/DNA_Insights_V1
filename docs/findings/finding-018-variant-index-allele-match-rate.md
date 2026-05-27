# Finding 018 — variant_annotations_index allele-match rate is gated on canonical REF/ALT

## Context

Sub-phase 5.7 (PR #62) shipped the `variant_annotations_index` rollup builder
(`genome annotate refresh-index`), the terminal Phase-5 step. It joins the four
variant-linkable sources into one sparse row per variant: ClinVar and gnomAD on
full GRCh38 coords `(chrom, pos, ref, alt)`, GWAS Catalog and PharmGKB on rsid.
First-run real-data verification against the user's loaded corpus (ClinVar
`2026_05_17`, gnomAD `4.1.1`, GWAS `2026_05_16`, PharmGKB `2025_07_05`) produced
coord-join match counts an order of magnitude below the planning estimate. This
finding records why the gap is expected, not a regression, and what lifts it.

## Observation

First-run numbers (locked as drift identifiers; mirrored in CLAUDE.md
"Real-data observations"):

| Metric | Value |
|---|---|
| `row_count` | 159,658 |
| `gnomad_matches` (`af_global` present) | 101,501 |
| `clinvar_matches` (`clinvar_count > 0`) | 2,559 |
| `gwas_matches` (`gwas_trait_count > 0`) | 66,726 |
| `pharmgkb_matches` (`has_pgx`) | 1,737 |
| `curated_count` (`is_curated`) | 4,198 |
| `is_rare` TRUE | 848 |
| `is_ultrarare` TRUE | 421 |
| wall-clock | ~2.2 s |

The planning estimate put `gnomad_matches` "modestly below" the 1,272,116
position-level gnomAD↔user overlap (`annotations.md`) and `is_ultrarare` near
399,321. The actual `gnomad_matches` is 101,501 — 12.5× lower — and
`is_ultrarare` is 421, ~950× lower. The 4-tuple coord join behaves exactly as
the locked plan specified (allele-specific AF requires an allele-level match);
the *estimate* was wrong, for two compounding reasons confirmed against the
data:

1. **78.3% of `variants_master` rows (738,424 of 942,620) are stored
   `ref_allele == alt_allele`.** These are hom-ref calls — Phase 2's
   alphabetical-ordering normalize sets both fields to the same base where every
   observation is homozygous (finding-005 #6). A `ref==alt` row carries no
   polymorphic ALT, so it cannot match gnomAD's or ClinVar's ALT on the 4-tuple.
   Only the 204,196 genuine (`ref≠alt`) variants are eligible.

2. **Of those 204,196 genuine variants, ~50% match gnomAD only with REF/ALT
   swapped.** 204,115 overlap a gnomAD position; 102,019 match in the same
   orientation, 101,918 match only when `(ref, alt)` is swapped. `variants_master`
   REF/ALT is not yet canonicalized to the genomic reference — Phase 2's tier-1
   matching does not unify strand-flipped representations (finding-005 #1), so
   roughly half the genuine variants carry the opposite reference designation
   from gnomAD/ClinVar. ClinVar shows the same shape: 2,492 of the genuine
   variants match its 4-tuple.

The rarity estimate compounded a third confusion: the planning ~399,321 came
from gnomAD's *position-level* AF buckets (every gnomAD allele at a user
position), not *allele-matched user variants*. The variants that do match are
overwhelmingly common chip SNPs, so only 848 / 421 are rare / ultra-rare.

## Implication

The 5.7 index is correct as specified and as built — every structural invariant
holds (PK-unique, VEP + `is_acmg_sf` 100% NULL, counts/arrays/`has_pgx`/
`is_curated` never NULL, zero FK orphans, single `refresh_versions`,
`variant_full_v` returns joined annotations). The low coord-join match rate
reflects the *current* `variants_master` REF/ALT representation, not the builder.

Both compounding causes trace to the same un-done work: `variants_master` REF/ALT
is not yet canonicalized to the genomic reference. That canonicalization is the
**post-5.7 backfills slot**, gated on the dbSNP build loaded in 5.6:

- **finding-005 #1** (strand-flip dedupe via canonical REF/ALT) addresses the
  ~50% orientation mismatch among genuine variants — re-orienting to the
  dbSNP-supplied reference is expected to roughly double genuine-variant coord
  matches.
- **finding-005 #6** (hom-ref `ref==alt` positions from Phase 2's normalize)
  governs the 78% hom-ref rows; whether and how those rows acquire a real ALT
  (and thus become coord-matchable, with the genotype layer recording that the
  user is hom-ref) is a backfill design question, not settled here.

Because the rollup is a wholesale recompute (`refresh-index` = DELETE +
`INSERT … SELECT`), the backfill simply re-runs the command after
re-canonicalizing `variants_master` — no migration, no schema change. The match
rate is expected to rise materially; the new numbers get captured and re-locked
then.

The rsid-keyed sources (GWAS 66,726, PharmGKB 1,737) are unaffected by the
REF/ALT issue — they match on rsid regardless of allele orientation, which is
why GWAS out-matches gnomAD here despite gnomAD's far larger table. They do,
however, attach locus-level evidence to every biallelic split sharing an rsid;
see the runbook join-model note.

A separate non-issue worth recording: `gene_variant_summary_v.pathogenic_count`
is 0 on real data because the `genes` dictionary table is empty (deferred to
Phase 7). The view's `genes ⨝ variants_master` join has no left side; this is
expected at 5.7 and unrelated to the index build.

## Follow-up tracking

- The first-run numbers above are the locked drift identifiers. A re-run against
  the same corpus that deviates is a regression signal.
- Re-running `refresh-index` after the finding-005 #1 canonical-REF/ALT backfill
  is expected to materially raise `gnomad_matches` / `clinvar_matches`; capture
  the new numbers and re-lock at that point.
- VEP columns + `is_acmg_sf` are filled by Phase 6 (finding-017); `is_curated`
  gains CPIC coverage only when a gene→variant mapping lands (Phase 6/7).
