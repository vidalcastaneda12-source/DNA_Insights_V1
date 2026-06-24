---
type: observation
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-05-12
supersedes: []
superseded_by: []
---
# 23andMe v5 and Ancestry v2: chip overlap and concordance

## Context

Phase 3's merge logic depends on the overlap between the two consumer chips
and the rate at which they agree on shared variants. These numbers were
unknown until real data ingestion completed.

## Observation

From real data — the user's own 23andMe v5 and Ancestry v2 exports:

- 23andMe v5 contributes 628,525 variants to `variants_master`.
- Ancestry v2 contributes 314,095 platform-unique variants (total 434,613 in
  the source file, but most overlap with 23andMe).
- **Shared overlap: 120,516 variants** (~28% of Ancestry's chip, ~19% of
  23andMe's).
- **Concordance rate on shared variants: 1.0000.** Every shared SNP that both
  platforms called produced the same genotype after strand resolution.
- 106 of the shared variants required tier-3 cross-row strand-flip resolution
  (same SNP on opposite strands recorded as two `variants_master` rows; merge
  unifies them).
- 31 palindromic shared variants (A/T or C/G); all 31 are concordant on real
  data.

## Implication

The two consumer chips agree extremely well on the SNPs they both genotype,
but they target meaningfully different SNP populations. Phase 5+ analyses
that depend on either platform alone will have access to roughly 600-700K
variants; analyses that need imputed data will move to ~30M after Phase 4.
Cross-platform discrepancy analyses are well-supported by the 120K shared
pool.

## Follow-up

None. The result validates the merge approach designed in Phase 3.
