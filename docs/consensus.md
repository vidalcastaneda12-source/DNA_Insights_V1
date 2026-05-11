# Consensus and discrepancy rules

This document defines the `consensus_v1` rule used by `genome merge`. The
implementation lives in `backend/src/genome/merge/`; the version label is
stamped onto every `consensus_genotypes.resolution_rule` value so older
consensus rows can be regenerated from history when the rule changes.

## Inputs

- All active rows in `genotype_calls` (one active call per `(variant_id, source)`).
- The corresponding rows in `variants_master` (chrom, pos_grch38, ref, alt, rsid).

Phase 3 only resolves the two raw-export sources, `23andme` and `ancestry`.
`topmed_imputed` is reserved for Phase 4; an `imputed_only` consensus method
already exists in the schema and will be exercised then.

## Variant matching strategy

Three lookup keys, applied in order:

1. **Primary — `(chrom, pos_grch38, ref_allele, alt_allele)`.** After
   lift-over and alphabetical-order normalization at ingest time, both
   sources' calls land on the same `variants_master` row when this key
   matches. Tier-1 matches are the common case — about 120K shared
   variants in the user's real-data corpus.
2. **Secondary — `rsid`.** Useful when one source's lift-over puts a
   variant at a slightly different position than the other's. This tier
   depends on `variant_aliases` (Group 2) to resolve dbSNP merges and
   withdrawals, so it is **deferred to Phase 5**. Phase 3 leaves the
   matching to tier 1 + tier 3 only.
3. **Tertiary fuzzy — `(chrom, pos_grch38)` with strand resolution.**
   When both sources have a call at the same position but on different
   `variants_master` rows (different `(ref, alt)`), the merge step looks
   for a complement match across rows:
   - **A/T or C/G site (palindromic):** strand cannot be inferred from
     genotype. Each row stays as a separate `platform_unique` discrepancy;
     no cross-row consensus.
   - **Non-palindromic, complement matches:** the two rows are the same
     biological variant on different strand conventions. Both rows are
     rewritten with `consensus_method = 'disagreement_resolved'`,
     `contributing_calls = [call_a, call_b]`, and a `genotype_mismatch`
     discrepancy with `resolution = 'flipped_strand_match'` is recorded
     on each. The consensus alleles stay in that row's own ref/alt
     frame, so `dosage` remains self-consistent with
     `variants_master.alt_allele`.
   - **Non-palindromic, complement does not match:** the two rows stay
     as separate `platform_unique` rows; this could be a multi-allelic
     split or a true allele difference that the cross-source data
     cannot distinguish from genotype alone.

## `consensus_v1` rule

For each `variants_master` row, given the (possibly absent) `23andme` and
`ancestry` active calls:

| `23andme` | `ancestry` | Resolution                                                                                                    |
| --------- | ---------- | ------------------------------------------------------------------------------------------------------------- |
| absent    | absent     | `consensus_method = 'unresolvable'`, no-call. Defensive — should not occur in well-formed state.              |
| present   | absent     | `single_source` using 23andme's call. `platform_unique` discrepancy at `info` severity (or `no_call_diff` at `minor` if 23andme's call itself is a no-call). |
| absent    | present    | Symmetric.                                                                                                    |
| no-call   | no-call    | `both_concordant` with `is_no_call = true`. No discrepancy — both sides agree on the absence of a call.       |
| no-call   | called     | `single_source` using ancestry's call. `no_call_diff` discrepancy at `minor` severity.                        |
| called    | no-call    | Symmetric.                                                                                                    |
| called    | called, alleles match (after alphabetical sort) | `both_concordant`. No discrepancy.                                                       |
| called    | called, alleles differ, palindromic site (A/T or C/G) | `unresolvable` no-call. `strand_ambiguous` discrepancy at `minor` severity.        |
| called    | called, alleles differ, non-palindromic, complement matches | `disagreement_resolved` using the alleles in this row's frame. `genotype_mismatch` discrepancy with `resolution = 'flipped_strand_match'` at `info` severity. |
| called    | called, alleles differ, non-palindromic, complement does not match | `unresolvable` no-call. `genotype_mismatch` discrepancy at `major` severity. |

After the per-row resolve, a tier-3 pass detects strand-flip partners across
`variants_master` rows at the same `(chrom, pos_grch38)`. Matched pairs have
their two `single_source` consensus rows rewritten to
`disagreement_resolved`, with `genotype_mismatch` discrepancies whose
`resolution = 'flipped_strand_match'`.

## Severity assignment

| Discrepancy type     | Phase 3 severity |
| -------------------- | ---------------- |
| `genotype_mismatch` (raw, non-resolvable)   | `major`         |
| `genotype_mismatch` (resolved by strand flip) | `info`        |
| `strand_ambiguous`   | `minor`          |
| `no_call_diff`       | `minor`          |
| `platform_unique`    | `info`           |
| `build_mismatch`     | `major` (not yet emitted in Phase 3) |
| `multi_allelic_split` | `minor` (not yet emitted in Phase 3) |

Severity escalation to `critical` for variants in ACMG SF genes (per the
schema's documented rule) is **out of scope for Phase 3**. The
`variants_master.is_acmg_sf` flag is not populated until the Phase 5
reference-annotation loaders run; a later enrichment job in Phase 5+ will
retroactively bump severity on the affected discrepancy rows.

## Idempotence

`genome merge` is idempotent: each invocation `DELETE`s every row from
`consensus_genotypes` and `discrepancies` first, then rebuilds both tables
from the current set of active `genotype_calls`. Re-running after a
re-ingest is the supported way to refresh the merged view. The whole merge
runs inside one DuckDB transaction, so a mid-merge failure leaves both
tables in their previous consistent state.

## Dosage and confidence

- `dosage` counts ALT-matching alleles in the consensus (`0`, `1`, or `2`),
  using `variants_master.alt_allele` as the ALT label. The ALT label
  assigned at ingest is the alphabetically-larger observed allele, not the
  real reference-panel ALT — that reconciliation lands in Phase 5 once VEP
  and dbSNP annotations are loaded.
- `confidence` is a placeholder in Phase 3. The current values are:
  `both_concordant` ⇒ 0.99, `disagreement_resolved` (strand-flip) ⇒ 0.90,
  `single_source` (true `platform_unique`) ⇒ 0.85, `single_source` (with a
  `no_call_diff` against the other platform) ⇒ 0.75, `unresolvable` ⇒
  `NULL`. The evidence-weighted rollup arrives in Phase 7.

## Versioning

The rule label `'consensus_v1'` is the constant `MERGE_VERSION` in
`backend/src/genome/merge/models.py`. When the rule changes:

1. Bump the constant (e.g. `'consensus_v2'`).
2. Update this document in lock-step with the code change.
3. The supersession workflow on `genotype_calls` already preserves history,
   so a new merge under the new rule rebuilds `consensus_genotypes` and
   `discrepancies` against the latest active calls. Older rule versions can
   be reconstructed by checking out the prior rule's code against the same
   `genotype_calls` snapshot.
