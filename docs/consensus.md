# Consensus and discrepancy rules

This document defines the `consensus_v1` rule used by `genome merge`. The
implementation lives in `backend/src/genome/merge/`; the version label is
stamped onto every `consensus_genotypes.resolution_rule` value so older
consensus rows can be regenerated from history when the rule changes.

## Inputs

- All active rows in `genotype_calls` (one active call per `(variant_id, source)`).
- The corresponding rows in `variants_master` (chrom, pos_grch38, ref, alt, rsid).

Three sources feed the merge: the two chip platforms (`23andme`, `ancestry`)
and the Phase 4 imputed source (`beagle_imputed`). The chip platforms are
resolved exactly as in Phase 3; the imputed source is treated as confirming
evidence when at least one chip call produces a *real* genotype at the same
variant, or as the sole evidence (`imputed_only`) when no real chip call is
present — either no chip call at all, or only chip *no-calls* (a chip no-call
carries no genotype and must not clobber a real imputed call; finding-028).
`topmed_imputed` is still in the enum but no longer ingested — the local
Beagle workflow superseded it (see `finding-006`).

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
     `contributing_calls = [call_a, call_b]`, and a `strand_flip_resolved`
     discrepancy with `resolution = 'flipped_strand_match'` is recorded
     on each (the discrepancy row exists for audit — it captures which two
     source calls were reconciled, *not* a real disagreement). The consensus
     alleles stay in that row's own ref/alt frame, so `dosage` remains
     self-consistent with `variants_master.alt_allele`.
   - **Non-palindromic, complement does not match:** the two rows stay
     as separate `platform_unique` rows; this could be a multi-allelic
     split or a true allele difference that the cross-source data
     cannot distinguish from genotype alone.

## `consensus_v1` rule

For each `variants_master` row, given the (possibly absent) `23andme`,
`ancestry`, and `beagle_imputed` active calls:

| `23andme` | `ancestry` | Resolution                                                                                                    |
| --------- | ---------- | ------------------------------------------------------------------------------------------------------------- |
| absent    | absent     | If `beagle_imputed` is also absent: `consensus_method = 'unresolvable'`, no-call (defensive — should not occur in well-formed state). If `beagle_imputed` is present, see the imputed-only rows below. |
| present   | absent     | `single_source` using 23andme's call. `platform_unique` discrepancy at `info` severity (or `no_call_diff` at `minor` if 23andme's call itself is a no-call). |
| absent    | present    | Symmetric.                                                                                                    |
| no-call   | no-call    | `both_concordant` with `is_no_call = true`. No discrepancy — both sides agree on the absence of a call.       |
| no-call   | called     | `single_source` using ancestry's call. `no_call_diff` discrepancy at `minor` severity.                        |
| called    | no-call    | Symmetric.                                                                                                    |
| called    | called, alleles match (after alphabetical sort) | `both_concordant`. No discrepancy.                                                       |
| called    | called, alleles differ, palindromic site (A/T or C/G) | `unresolvable` no-call. `strand_ambiguous` discrepancy at `minor` severity.        |
| called    | called, alleles differ, non-palindromic, complement matches | `disagreement_resolved` using the alleles in this row's frame. `strand_flip_resolved` discrepancy with `resolution = 'flipped_strand_match'` at `info` severity. The discrepancy row is an audit trail of a successful reconciliation, not a disagreement. |
| called    | called, alleles differ, non-palindromic, complement does not match | `unresolvable` no-call. `genotype_mismatch` discrepancy at `major` severity. |

Phase 4 extension — imputed source:

| `23andme` | `ancestry` | `beagle_imputed` | Resolution |
| --------- | ---------- | ---------------- | ---------- |
| absent    | absent     | called           | `imputed_only` using the imputed call's alleles; `is_imputed = true`; `consensus_r2` carries the imputed call's `imputation_r2`. No discrepancy. |
| absent    | absent     | no-call          | `imputed_only` with `is_no_call = true`; `is_imputed = true`; `consensus_r2` carries the imputed call's `imputation_r2`. No discrepancy. |
| no-call (and/or absent) | no-call (and/or absent) | called | **finding-028:** a real imputed call with *no real chip call* — `imputed_only` using the imputed alleles (`is_imputed = true`, `consensus_r2` carried). Each present chip no-call is appended to `contributing_calls` and surfaced as a `no_call_diff` discrepancy (`source_a = beagle_imputed`, the chip no-call as `source_b`, `resolution = 'taken_from_imputed'`). A chip no-call must not clobber a real imputed genotype. |
| any *real* chip call present | (any) | present          | The chip-only resolution above prevails byte-for-byte (method, alleles, dosage, `is_imputed = false`). The imputed call's `call_id` is appended to `contributing_calls` as confirming evidence. No new discrepancy is emitted. |

After the per-row resolve, a tier-3 pass detects strand-flip partners across
`variants_master` rows at the same `(chrom, pos_grch38)`. Matched pairs have
their two `single_source` consensus rows rewritten to
`disagreement_resolved`, with `strand_flip_resolved` discrepancies whose
`resolution = 'flipped_strand_match'`. Rows that also carry an active
`beagle_imputed` call are excluded from tier-3 candidacy: the tier-3 rewrite
replaces `contributing_calls` with just the two paired chip call_ids, which
would drop the imputed call from the audit trail. Preserving the imputed
call as confirming evidence on the per-row consensus is the higher-priority
invariant. This is a rare case in practice — imputed-only variants land at
different `(chrom, pos_grch38)` than the chip data by construction, and a
chip+imputed variant whose strand-flip partner is a separate chip-only
`variants_master` row is the only shape the exclusion affects.

## Imputation as confirming evidence

The Phase 4 rule extension is anchored on one design choice: imputation
adds confidence, it does not override. When `23andme` and `ancestry` agree,
the consensus method stays `both_concordant`; when they disagree on a
non-palindromic site whose complement flip succeeds, the consensus method
stays `disagreement_resolved`; when only one chip platform reports the
site, the consensus method stays `single_source`. In all three cases an
active `beagle_imputed` call at the same variant is appended to
`contributing_calls` so the dashboard can show that imputation independently
re-derived the same genotype, but the consensus's method, alleles, dosage,
and `is_imputed` flag are not touched. Imputation becomes the consensus when
no chip call is active — the `imputed_only` branch, by far the most common
shape in real-data merges (~2.3M of ~3.2M consensus rows on the user's
corpus) — **and** when the only chip calls present are *no-calls*
(finding-028): a chip no-call carries no genotype, so it must not hold the
consensus as a no-call and demote a real imputed call to evidence. That
configuration has zero rows in the pre-collapse corpus — it is materialized by
the PR-5b duplicate collapse, which re-points a chip no-call onto an imputed
survivor — so the rule is an in-place completion of the Phase-4 extension and
is a no-op on existing data.

The rule label remains `consensus_v1` because the chip-only branches are
unchanged byte-for-byte. The extension was anticipated by Phase 3's
forward-pointing language — `imputed_only` was already an enum member of
`consensus_method_enum` and `beagle_imputed` already an enum member of
`source_enum`. No schema migration was required. The finding-028 chip-no-call
completion likewise keeps `consensus_v1`: it changes no realized consensus row
on the corpus (zero `{imputed-real + chip-no-call}` rows pre-collapse), exactly
the same "unchanged byte-for-byte" criterion.

## Discrepancy type catalog

| Discrepancy type | Meaning |
| ---------------- | ------- |
| `genotype_mismatch`   | Both platforms produced a call, the alleles do not match, and the complement flip does not reconcile them. A true biological disagreement; consensus held as no-call. |
| `strand_flip_resolved` | Both platforms produced a call on what looked like opposite strands; the complement flip reconciled them. The resolution was *successful* — this row exists for audit so the dashboard can show which two source calls were merged. It is **not** a disagreement. Emitted both by the per-row resolve (same `variants_master` row, both sources called it) and by the tier-3 cross-row pass (two `variants_master` rows at the same `(chrom, pos)` with complementary alleles). |
| `strand_ambiguous`    | Site is palindromic (A/T or C/G) and the platforms reported different alleles; strand cannot be inferred from genotype alone. Consensus held as no-call. |
| `no_call_diff`        | One platform produced a call, the other reported `--` (no-call). Consensus takes the called platform. |
| `platform_unique`     | The variant only has an active call from one platform (the other platform did not report this site at all). |
| `build_mismatch`      | Lift-over disagreement between platforms. Not emitted in Phase 3. |
| `multi_allelic_split` | One platform reports the site biallelic, the other multi-allelic. Not emitted in Phase 3. |

## What triggers a discrepancy row

Discrepancy rows are produced in two distinct situations:

- **Successful reconciliations recorded for audit.** `strand_flip_resolved` is
  emitted alongside a `disagreement_resolved` consensus when the two platforms
  reported the same SNP on opposite strands and the complement flip reconciled
  them. The consensus call is clean; the discrepancy row exists so the
  dashboard can show which two source calls were unified and so the merge is
  fully auditable. Severity is always `info`.
- **Real disagreements.** `genotype_mismatch` (alleles differ even after a
  complement flip), `strand_ambiguous` (palindromic A/T or C/G with
  disagreement), and `no_call_diff` (one side called, one side did not) all
  represent actual mismatches between sources. `genotype_mismatch` and
  `build_mismatch` are `major`; `strand_ambiguous`, `no_call_diff`, and
  `multi_allelic_split` are `minor`. `platform_unique` is `info`.

## Severity assignment

| Discrepancy type      | Phase 3 severity |
| --------------------- | ---------------- |
| `genotype_mismatch`   | `major`          |
| `strand_flip_resolved` | `info` (always — successful reconciliation, never escalates) |
| `strand_ambiguous`    | `minor`          |
| `no_call_diff`        | `minor`          |
| `platform_unique`     | `info`           |
| `build_mismatch`      | `major` (not yet emitted in Phase 3) |
| `multi_allelic_split` | `minor` (not yet emitted in Phase 3) |

Severity escalation to `critical` for variants in ACMG SF genes (per the
schema's documented rule) is **out of scope for Phase 3**. The
`variants_master.is_acmg_sf` flag is not populated until Phase 6's ACMG SF
detection pipeline runs (its first task); that pipeline then retroactively
bumps severity on the affected discrepancy rows.

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
  real reference-panel ALT — that reconciliation is a post-5.7 backfill on
  dbSNP canonical REF/ALT (loaded in 5.6), with Phase 6's VEP runner refining
  functional calls.
- `confidence` is a placeholder in Phase 3. The current values are:
  `both_concordant` ⇒ 0.99, `disagreement_resolved` (strand-flip) ⇒ 0.90,
  `single_source` (true `platform_unique`) ⇒ 0.85, `single_source` (with a
  `no_call_diff` against the other platform) ⇒ 0.75, `unresolvable` ⇒
  `NULL`, `imputed_only` ⇒ `NULL` (the per-variant `consensus_r2` carries
  the imputation quality signal until the Phase 7 rollup folds it in). The
  evidence-weighted rollup arrives in Phase 7.

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
