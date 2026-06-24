---
type: observation
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-06-19
supersedes: []
superseded_by: []
---
# Imputed-call re-import crashes on the `discrepancies` -> `genotype_calls` FK

## Status

Fixed in PR 5a (pre-Phase-6), as the blocker that stopped the first real-data
chrX M1->M3 re-import. The fix is a gated pre-import `discrepancies` clear in
`genome.imputation.ingest`, unit-tested with a regression that reproduces the
crash. The real-data chrX gate (`import -> collapse-duplicate-variants -> merge
-> align-tier3-consensus -> refresh-index -> chrx-loo`) is re-run at the
merge-gate verification.

## Context

PR 5a re-imputes chrX (M1 -> M3-physical, [`finding-029`](finding-029-chrx-imputation-m1.md))
and re-imports it over a corpus that has already been merged once. That baseline
merge wrote `discrepancies` rows referencing the prior (M1) chrX imputed calls.
Re-importing the M3 result supersedes those M1 calls — and that is where it
crashed.

The QC layer that motivated the re-import
([`finding-031`](finding-031-chrx-nonpar-dosage-confidence-qc.md), the
dosage-confidence gate) is verified-correct (run_0003: 93,606 chrX kept; the
authoritative run_0002 lock is 92,832 — see [`finding-033`](finding-033-chrx-loo-allele-aware-matching.md)).
This is a **separate, pre-existing** bug in the supersession path, *exposed* — not
caused — by the re-import. It is also **general**: any future autosomal imputed re-import
over a merged corpus hits it.

## Problem: an indexed-column UPDATE delete+reinserts the parent row

`_deactivate_prior_imputed_calls` supersedes prior imputed calls with

```sql
UPDATE genotype_calls SET is_active = FALSE, superseded_reason = ?
 WHERE is_active AND source = 'beagle_imputed' AND variant_id IN (...re-imported...)
```

`is_active` is part of `idx_gc_active (variant_id, source, is_active)`
(`ddl/group_1_genotype.sql`). DuckDB implements an UPDATE that touches an
**indexed** column as a **delete + reinsert** of the row on that index — even
though no key column's value changes. The reinsert of a `genotype_calls` row
fires the **parent-side** foreign key from

```
discrepancies.call_a_id / call_b_id  ->  genotype_calls(call_id)
```

(`ddl/group_1_genotype.sql:198,201`). Because the prior merge wrote discrepancy
rows pointing at the M1 imputed calls being superseded, the reinsert is rejected:

```
Violates foreign key constraint because key call_a_id: <id> is still referenced
by a foreign key in a different table.
```

The whole import runs in one transaction (`_execute_import`), and the
supersession runs per batch inside it. DuckDB has no `SAVEPOINT`, so the fix
cannot be a mid-import commit.

`discrepancies` is the **only** table that FK-references `genotype_calls(call_id)`
(grep `REFERENCES genotype_calls`). `consensus_genotypes` is keyed by
`variant_id` (not a call FK) and `contributing_calls` is a `BIGINT[]`, not an FK —
so neither is affected.

This is the identical DuckDB quirk already handled in
`genome.annotate.canonicalize` and `genome.annotate.strand_collapse`: an UPDATE
that delete+reinserts an FK-referenced row fires the parent-side check, and
DuckDB's FK enforcement reads **pre-transaction** state, so an in-transaction
delete of the referencing rows is invisible to it
([`finding-020`](finding-020-canonical-refalt-backfill.md)).

## Fix: a gated TX0 `discrepancies` pre-clear

`import` clears the referencing `discrepancies` in a **committed** transaction
*before* the import transaction opens (`_preclear_discrepancies_for_supersession`):

- **Gate**: count `discrepancies` rows whose `call_a_id` / `call_b_id` reference
  an active `beagle_imputed` call. Every call the import will supersede is an
  active imputed call at gate time, so this count is a superset of the FK
  blockers. Zero -> no-op (first import / chip-only state — untouched, so the
  additive-ingest path and the existing tests are unchanged).
- **Clear**: `DELETE FROM discrepancies` (wholesale), committed before
  `BEGIN TRANSACTION`. With the referencing rows committed-away, DuckDB's
  pre-transaction FK check sees nothing to violate and every per-batch
  `is_active` flip succeeds.

Full clear (not a targeted `WHERE call_*_id IN (...)`) because it matches both
existing TX0 precedents verbatim *and* the next two reload steps —
`collapse-duplicate-variants` (its own TX0 `DELETE FROM discrepancies`) and
`merge` (`DELETE` + rebuild) — clear `discrepancies` wholesale anyway. A targeted
delete that preserved chip-vs-chip rows would have them overwritten by `merge`
moments later, so it buys nothing while diverging from precedent.

## Why this is safe

`discrepancies` is merge-derived: `merge_all` does `DELETE FROM discrepancies`
then rebuilds it from the active calls (`merge/pipeline.py`). The reload runbook
always runs `merge` after `import` (and `import`'s own next-step message says so),
and a re-import that supersedes imputed calls **requires** a re-merge regardless —
`consensus_genotypes` would otherwise reference the now-inactive calls. So
clearing `discrepancies` adds no work that `merge` was not already going to do.

Crash windows mirror canonicalize's TX0 analysis: a crash after the pre-clear
commit but before / within the import leaves `discrepancies` empty with
`genotype_calls` otherwise intact — a re-mergeable state, fixed by re-running the
post-import `merge`. The supersession-over-update guarantee (CLAUDE.md decision
\#7) is untouched: the pre-clear only deletes merge-derived rows; the
INSERT-new-active + deactivate-old of `genotype_calls` stays atomic inside the
import transaction.

## Tests

- `test_reimport_supersedes_calls_referenced_by_discrepancies` — import, insert a
  `discrepancies` row whose `call_a_id` references an imputed call (the shape
  `merge` produces), re-import; asserts no crash, supersession completes,
  `discrepancies` cleared. Red on the pre-fix code (the `ConstraintException`),
  green after.
- `test_reimport_keeps_discrepancies_not_referencing_imputed_calls` — pins the
  gate: a discrepancy that references no genotype call survives a re-import, so
  the clear is scoped to the FK hazard, not an unconditional wipe.

## Links / out of scope

- [`finding-020`](finding-020-canonical-refalt-backfill.md) — the canonicalize
  TX0/TX1/TX2 FK pattern this reuses.
- [`finding-031`](finding-031-chrx-nonpar-dosage-confidence-qc.md) — the chrX QC
  gate whose re-import surfaced this.
- A schema change to make `discrepancies.call_*_id` `ON DELETE SET NULL` or
  application-validated would dissolve the quirk but needs a DDL re-extraction +
  database rebuild; rejected here as far heavier than a merge-derived pre-clear.
