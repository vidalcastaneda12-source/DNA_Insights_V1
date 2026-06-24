---
type: both
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-06-13
supersedes: []
superseded_by: []
---
# Finding 028 — `consensus_v1` chip-no-call clobbers a real imputed genotype

## Context

The PR-5b duplicate collapse (finding-026) re-points the chip no-call sitting on
a duplicate `(N,N)` `variants_master` row onto its surviving variant. For the
≈523 cases where the survivor is `beagle_imputed`-only, this **materializes a
single variant carrying `{imputed-real call + chip-no-call call}`** — a
configuration that has **zero rows in the pre-collapse corpus**, because the
duplication is exactly what keeps the chip no-call (on the `(N,N)` row) and the
imputed genotype (on the resolved row) on *separate* variants. The collapse then
re-runs `genome merge`, which processes the new configuration for the first time.

Tracing `consensus.resolve()` (pre-fix) on `{imputed-real + chip-no-call}` shows
the merge **silently wipes the real imputed genotype**:

- A chip no-call is still `pair.twentythree is not None` (a no-call is a *call*,
  with a `call_id` and `is_no_call=True`), so it routes into the **chip** branch,
  not the imputed branch.
- **1 chip no-call + imputed:** `_resolve_single_source(no-call, other=None)` →
  `present.is_no_call` is True → consensus is `is_no_call=True,
  method='single_source'`. `_append_imputed_call` then only appends the imputed
  `call_id` to `contributing_calls` and **preserves `is_no_call=True`, NULL
  alleles, `is_imputed=False`** — the imputed genotype is demoted to a
  contributing id.
- **2 chip no-calls + imputed:** `_resolve_both_no_call` → `both_concordant,
  is_no_call=True`; imputed appended. Same wipe.

So without a fix, the collapse + re-merge would **zero a real imputed genotype at
≈523 positions** — a regression the collapse would *introduce*, not surface. This
is the gating reason the merge fix lands as its **own PR, before** the collapse
(the collapse re-merges through merge, so the corrected merge must exist first).

This is also a **latent** correctness bug independent of the collapse: any future
path that puts a real imputed call beside a chip no-call on one variant (e.g. a
re-impute over chip-no-call positions) would hit it.

## The fix

`merge/consensus.py:resolve()` gains a guard, evaluated **before** the chip
branches:

```python
a_real = a is not None and not a.is_no_call
b_real = b is not None and not b.is_no_call
if imputed is not None and not imputed.is_no_call and not a_real and not b_real:
    chip_no_calls = tuple(c for c in (a, b) if c is not None)
    return _resolve_imputed_over_chip_nocalls(pair, imputed, chip_no_calls)
```

`_resolve_imputed_over_chip_nocalls` returns the `imputed_only` consensus (real
imputed genotype, `is_imputed=True`, `consensus_r2` carried). Each present chip
no-call is appended to `contributing_calls` and surfaced as a `no_call_diff`
discrepancy (`source_a=beagle_imputed`, the chip no-call as `source_b`,
`resolution='taken_from_imputed'`) — imputation called the site; the chip
reported no-call.

The guard fires **only** for `{imputed-real + chip-no-call(s)-only}`. Every other
configuration is unchanged:

- A *real* chip call present (`a_real` or `b_real`) → the chip resolution prevails
  as before, imputed appended as confirming evidence.
- An imputed *no-call* (`imputed.is_no_call`) → the guard does not fire; the
  existing `imputed_only` no-call branch handles it.
- **The pure imputed-only case (no chip call present at all):** the guard *does*
  fire, but `_resolve_imputed_over_chip_nocalls` with an empty `chip_no_calls`
  returns exactly `(_resolve_imputed_only(pair, imputed), [])` — the same function
  the pre-fix path called — so the output is **byte-identical** in both
  `consensus_genotypes` and `discrepancies`.

## No-op verification (why it is safe to land first)

The only configuration whose output changes — `{imputed-real + chip-no-call}` —
has **0 rows pre-collapse**. So re-merging the current corpus produces a
byte-identical `consensus_genotypes` and `discrepancies`, and an unchanged
`MergeResult` summary (`method_counts`, `type_counts`, `concordance_rate`,
`strand_flip_resolutions`). The PR's gate is: re-merge → assert no change.

Unit coverage (`backend/tests/test_merge_consensus.py`):
`test_imputed_real_survives_one_chip_nocall`,
`test_imputed_real_survives_two_chip_nocall`,
`test_imputed_real_no_chip_call_is_byte_identical_imputed_only` (the no-op proof),
`test_real_chip_with_chip_nocall_and_imputed_stays_chip_dominated`.

## Versioning

`resolution_rule` stays **`consensus_v1`**. No realized consensus row in the
corpus changes (zero affected rows), so this is the same "unchanged byte-for-byte"
criterion under which Phase 4 extended `consensus_v1` in place. The change
*completes* the Phase-4 imputed-evidence handling — which left the chip-no-call
gap — rather than redefining any chip resolution. `docs/consensus.md` is updated
in lock-step.

## Follow-up

- PR 5b's collapse re-points the ≈523 chip no-calls onto imputed survivors; with
  this fix in place those survivors re-merge to `imputed_only` (genotype
  preserved), which the collapse delta table (finding-026) is conditioned on.
- The `no_call_diff` rows this branch emits are the dashboard signal that
  imputation filled a chip no-call; they are not gate-anchored (only
  `genotype_mismatch` among discrepancy types is — see `docs/runbooks/verification.md`).
