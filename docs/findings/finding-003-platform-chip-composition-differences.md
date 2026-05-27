# 23andMe v5 and Ancestry v2: chip composition differences

## Context

Real-data ingestion in Phase 2 surfaced two meaningful per-chip differences
that affect QC interpretation and downstream analyses.

## Observation

- **Ancestry v2 does not include Y-chromosome SNPs.** A male sample's
  Ancestry data alone produces `sex_inferred='ambiguous'` (correctly — with
  no Y data, sex cannot be determined). 23andMe v5 includes Y SNPs and infers
  sex unambiguously.
- **Heterozygosity rate is chip-dependent.** Same sample, two different
  platforms:
  - 23andMe v5: ~0.17 (broad SNP panel; many low-MAF variants where the
    typical individual is homozygous-reference).
  - Ancestry v2: ~0.34 (panel curated for ancestry-informative markers with
    higher MAF).
- This 2× gap is chip-design signal, not biological signal.

## Implication

The `het_outlier` QC threshold (when introduced) must accommodate both
ranges or be source-aware. The per-source `sex_inferred` field is correct on
its own terms but may not be a useful single answer at the profile level — a
profile-level QC rollup should combine per-source inferences.

## Follow-up

Phase 6's genome-QC pipeline should introduce a profile-level `sample_qc` rollup that
combines per-run inferences across sources. Until then, treat ambiguous
Ancestry sex inferences as expected behavior, not anomalies.
