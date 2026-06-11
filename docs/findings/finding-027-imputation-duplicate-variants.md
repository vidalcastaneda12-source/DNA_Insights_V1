# Finding 027 — how imputation + chip no-calls create duplicate `variants_master` rows

## Context

PR 5b (finding-026) collapses ≈684 same-SNP duplicate `variants_master` rows. This
finding records **why those duplicates exist** — the upstream mechanisms — and why
the fix is downstream cleanup now with the source fix deferred. Measured read-only
against the user's corpus (dbSNP 157; `variants_master` 3,088,917).

The genuine duplicates split into three populations by origin:

## 1. Chip+imputed strand / representation gap (15: 10 swap + 5 strandflip)

`backend/src/genome/imputation/vcf_export.py` exports the chip consensus to the
per-chromosome VCF Beagle consumes by reading `variants_master.(ref_allele,
alt_allele)` **as-stored** — the Phase-2 alphabetical-ordering normalize
(`ingest/normalize.py:order_alleles`), which at imputation time (Phase 4, before
PR-3 canonicalize) was *not* reconciled to the 1000G Phase 3 panel's strand
convention. There is no panel-strand lookup in the export path.

When the chip's alphabetical `(ref, alt)` differs from the panel's orientation,
Beagle emits the **panel-strand** representation, and the imputed-import upsert
(`imputation/ingest.py:_upsert_variants_master`) matches existing rows on the full
4-tuple `(chrom, pos, ref, alt)` — which a swapped or complemented imputed row does
**not** satisfy. So a *new* `variants_master` row is inserted beside the chip row:

- **swap (10):** chip `(C,T)` + imputed `(T,C)` — same allele set, REF/ALT order
  reversed. Both non-canonical (these sit at positions absent from the user-filtered
  dbSNP, so PR-3 canonicalize had no 4-tuple to re-orient against and left them).
- **strandflip (5):** chip `(A,G)` + imputed `(C,T)`/`(T,C)` — reverse-complement;
  the imputed side matches dbSNP (canonical), the chip side does not. PR-3
  canonicalize is Scope-A (ordering + hom-recovery only, no complement) so it cannot
  fix these either.

These never merge-pair: the chip row resolves `single_source` and the imputed row
`imputed_only`, two separate consensus rows at one position — a Phase-6 double-count.

## 2. Chip no-call meets imputed (≈523, the dominant population)

The chip reported a **no-call** at a position (`ingest/normalize.py` writes it as a
`(N,N)` `variants_master` row with a no-call `genotype_calls`). The same position was
imputed, so a separate imputed `variants_master` row carries the real genotype. The
`(N,N)` row retained the position's **real rsID** (the chip carried it), while the
imputed survivor's rsID is **NULL** — Beagle's synthetic `chr:pos:ref:alt` rsID was
stripped by #66 (finding-021). So the locus's real rsID and its rsID-keyed
annotations (97 `variant_annotations_index` rows across all 661 no-call dups) sit on
the no-genotype `(N,N)` row, divorced from the genotype.

These are **not inert placeholders**: collapsing them re-points the chip no-call onto
the imputed survivor and **relocates the real rsID + its annotations onto the imputed
genotype** — the collapse is enriching, not just dedup. (The re-merge keeps the
imputed genotype only because of the PR-5b-pre `consensus_v1` chip-no-call fix,
finding-028.)

## 3. Chip+chip keying (≈137, related but distinct)

The remaining no-call dups (71 chip+chip + 66 both+chip) are a **Phase-2 chip-keying**
phenomenon, not an imputation one: one chip reported a no-call (`(N,N)`) and the other
chip a real genotype at the same position, keyed to separate rows. PR-3 hom-recovery
gave the real side a canonical `(ref, alt)`; the `(N,N)` row remained. The collapse
repoints the no-call onto the chip-real survivor (genotype already safe, no PR-5b-pre
dependency for these).

## Why downstream cleanup now, source fix deferred

PR 5b collapses **all** existing duplicates regardless of origin. The upstream
`vcf_export.py` panel-strand reconciliation is **deferred**:

- It crosses into the imputation (5a) layer that 5b otherwise does not touch.
- Post-PR-3, `variants_master` REF/ALT is canonicalized to dbSNP (GRCh38 + strand),
  which is the panel's convention — so a *future* re-impute already aligns the
  dbSNP-present positions to the panel, shrinking population 1 to the residual of
  positions absent from the user-filtered dbSNP.
- Re-imputation is a rare, operator-gated ~30-min op, so recurrence is low-frequency.

**Recommended future fix point:** a pre-export canonicalize/panel-reconcile step
folded into a future `imputation prepare` / re-impute PR — write the chip alleles to
the export VCF in the panel's orientation (or canonicalize `variants_master` REF/ALT
before export, recovering the hom-only rows too), and dedup the imputed import against
opposite-strand/swap chip rows in `_upsert_variants_master`.

## Provenance

No schema change. The collapse's operation record is the finding-026 snapshot +
`strand_collapse.complete` structlog event; this finding records the upstream
mechanism for the deferred follow-up.
