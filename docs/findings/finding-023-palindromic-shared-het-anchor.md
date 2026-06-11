# Finding 023 — Palindromic-shared anchor is het-defined; hom-only recovery reveals hom palindromic sites

## Context

PR-3 canonicalize ([`finding-020`](finding-020-canonical-refalt-backfill.md)) does
canonical REF/ALT backfill + hom-only recovery. Verifying the post-canon
palindromic-shared anchor (the `palindromic shared variants = 31` identifier from
finding-007, mirrored in CLAUDE.md "Real-data observations" #3) surfaced a side
effect worth recording.

## Observation

Pre-canon, exactly **31** palindromic (A/T, C/G) both-called sites were visible —
all **het** (dosage 1), all `both_concordant`. Post-canon, the site-level count of
**all** palindromic both-called rows is **6,681**:

| class | dosage | count | note |
|---|---|---|---|
| het | 1 | 31 | unchanged — the anchor |
| hom-alt | 2 | 18 | post-canon reveal |
| hom-ref | 0 | 6,605 | post-canon reveal |
| unresolvable (no-call) | — | 27 | population-A collisions (finding-020) |
| **total** | | **6,681** | |

The 6,623 hom rows (18 + 6,605) were **invisible pre-canon** because the unobserved
allele was NULL (hom-ref → the alt is unobserved; hom-alt → the ref is unobserved).
Hom-only recovery backfills the missing allele from dbSNP v157, so the site now
presents a complete allele pair and reads palindromic. **`dosage` discriminates**
"allele observed in the genotype" (het — stable) from "allele backfilled" (the hom
tiers — post-canon-only).

## Implication

The palindromic-shared **verification anchor** is defined as **het**
(both-alleles-observed) palindromic = **31, held**. That subset is strand-invariant
(an A/T site reads A/T on either strand), trivially concordant, and is the stable
fingerprint — unmoved by canonicalize. The **6,681** site-level figure is real but
moves with the dbSNP version and the recovery logic, so it is **not** an anchor. The
**27 unresolvable** are already counted under finding-020's `unresolvable` bucket
(population A) — **not** double-counted here.

**Biology note.** Hom palindromic is strand-*sensitive* (a hom-ref `A/A` on the `+`
strand is hom-alt `T/T` on the `−` strand). Of the post-canon hom palindromic rows,
6,623 are cross-platform concordant and 27 collapse to `unresolvable`.

## Follow-up

None (documentation).
