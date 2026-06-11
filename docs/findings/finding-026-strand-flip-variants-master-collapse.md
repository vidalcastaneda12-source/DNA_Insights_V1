# Same-SNP duplicate `variants_master` collapse (PR 5b — closes finding-005 #1)

## Context

PR-3's Scope-A canonicalize (`genome annotate canonicalize-variants`,
finding-020) deliberately left same-SNP duplicates un-collapsed: one physical
biallelic SNP stored as ≥2 `variants_master` rows at the same `(chrom, pos)`. The
interim `align-tier3-consensus` patch only deleted the non-canonical side's
`consensus_genotypes` row; the duplicate row — with its own `genotype_calls` and
annotation joins — survived and would duplicate Phase-6 insights.

This PR was first written (the original finding-026, superseded by this file)
against **finding-020 recon B's premise that exactly one such pair survives**
(`strand_flip_resolutions = 2` = one pair). That premise was wrong. Read-only
real-data measurement (dbSNP 157; `variants_master` 3,088,917) re-scoped the work.

## Measured reality (the regression anchor for scope)

At the **10,700** `(chrom, pos)` positions with ≥2 `variants_master` SNV rows:

| Class | Count | In scope |
|---|---|---|
| Legit multi-allelic (two different alts sharing one allele, both dbSNP-canonical) | 10,014 | **No — protected** |
| `hom_nocall` — a no-call `(N,N)` placeholder + a real biallelic sibling | 661 | yes |
| `swap` — same allele set, REF/ALT reversed; both non-canonical | 10 | yes |
| `strandflip` — reverse-complement biallelic pair, exactly one canonical | 5 | yes |
| `hom_opp` — real-hom on the opposite strand (incl. chr4:185229100, the single `disagreement_resolved`) | 5 | yes |
| `hom_same` — real-hom, same strand | 3 | yes |

**≈684 actionable genuine-duplicate edges** (660 no-call repoint + 1 no-call DROP
at the size-3 multi-allelic position + 10 swap + 5 strandflip + 5 hom_opp + 3
hom_same), plus ≈1 degenerate size-2 bucket (`(N,N)` + a real-hom, no biallelic
row) skipped. There are **zero** chip+chip strand-flips/swaps; the original
"clean biallelic strand-flip pair" population was dissolved by PR-3 canonicalize +
hom-recovery. The single merge-resolved pair (chr4) is a **hom-opposite-strand**
case (ancestry `G/G` non-canonical + 23andme `(C,T)` canonical, genotype `C/C`),
not a complementary-allele-set pair — see finding-020 recon B (corrected).

The **661 `(N,N)` deads are materialized and enriching**, not inert: 657 carry a
`single_source` consensus, 4 `both_concordant`, all 661 an rsID, 97 a
`variant_annotations_index` row. The survivors are mostly `beagle_imputed`-only
with NULL rsid (Beagle's synthetic rsID stripped by #66), so collapsing relocates
the real rsID + its annotations onto the imputed genotype (finding-027).

## Two root causes the original code had (both fixed)

- **RC1 — predicate too narrow.** The original `_identify` required
  `len(canonical)==1` AND `complement_pair(N)==sorted_pair(C)`, matching only the
  reverse-complement biallelic pair with one canonical = the 5 `strandflip`. Swaps
  (`ncanon=0`, not a reverse-complement) and every `hom_*`/`hom_nocall` failed it.
- **RC2 — candidate-set SQL hid the 661 no-call dups (dominant).** `_CLASSIFY_SQL`'s
  `bucket` CTE applied `ref/alt IN ('A','C','G','T')` **before** the partitioned
  `COUNT`, so an `(N,N)` row was dropped and its biallelic sibling saw
  `bucket_size = 1` → discarded. The original SQL saw 10,038 buckets vs 10,699.

## What shipped

`genome annotate collapse-duplicate-variants` (module
`backend/src/genome/annotate/strand_collapse.py`,
`collapse_duplicate_variants(conn=None, *, dry_run=False, force=False, no_backup=False)`),
lazy-imported from the CLI like `canonicalize-variants` / `align-tier3-consensus`.

### Identification — per EDGE, not per bucket

`_CLASSIFY_SQL` (RC2-fixed: no ACGT filter before the partitioned `COUNT`)
classifies every SNV row in a ≥2 bucket canonical/non-canonical against the active
dbSNP. Per position:

1. **Protect the legit multi-allelic alts** — the canonical biallelic rows with
   *different* allele sets are never collapsed onto each other (the 10,014 guard,
   stated as edge-protection).
2. **Pick a single survivor** for the duplicates: exactly-one-canonical → that row;
   zero-canonical → a biallelic-distinct row preferring a chip call over
   imputed-only, total tiebreak lowest `variant_id`. Never a hom/`(N,N)` row.
3. **Reconcile each duplicate** via the call-content router (below), or **DROP** an
   `(N,N)` no-call at a position with ≥2 protected alts (no single survivor).
4. No biallelic-distinct row anywhere → **skip + warn** (degenerate).

### Reconciliation — routed by call content

The structural mechanism gives an expected strand (same for swap/hom-same,
opposite for strandflip/hom-opposite); each active call's observed alleles must
agree:

* **repoint as-is** (no-call / swap / hom-same): observed alleles already lie on
  the survivor's strand — the call rides `_REPOINT_ALL_CALLS_SQL` verbatim (no new
  call, no supersession). This is the 660 no-call + 10 swap + 3 hom-same.
* **complement + supersede** (strandflip / hom-opposite): INSERT a new active call
  with `complement_pair` alleles (`strand_status='flipped_to_match'`), deactivate +
  supersede the old (decision #7). This is the 10 (5 strandflip + 5 hom_opp).
* **DROP** (the 1 size-3 `(N,N)`): delete its no-call call + row, **no repoint** onto
  an arbitrary alt; coalesce its locus rsID onto a canonical sibling whose `rsid` is
  NULL.

A call that resolves under **neither** strand (an internally inconsistent row) skips
the edge — counted `genotype_mismatch_skipped`, not guessed.

### Guards

* **Source-collision** — a reconciliation that would give the survivor two *active*
  calls of one `source` skips the edge (`source_collision_skipped`). **Measured 0**
  corpus-wide (incl. the 4 `both_concordant` deads — imputed-only survivors).
* **Palindromic survivors** (A/T, C/G) are skipped (swap vs flip undecidable).
* There is **no** no-imputed-call guard (the original code's): imputed calls
  relocate to the survivor exactly like chip calls.

### Dependency — PR 5b-pre (finding-028)

The no-call repoints re-merge to `imputed_only` (genotype preserved) only because
the `consensus_v1` chip-no-call fix (finding-028) landed first; without it `merge`
would clobber ≈523 imputed genotypes. **PR 5b-pre must merge before this PR.**

### Transaction scaffold

Reuses canonicalize's TX0 (`DELETE discrepancies`) / TX1 (clear the two
`variants_master`-keyed rollups; INSERT complemented calls; re-point every call on
each reconciled dead; deactivate + supersede; DELETE the dropped `(N,N)` calls) /
TX2 (rsID coalesce; DELETE orphan reconciled + dropped rows; recompute `has_*_call`)
+ the `idx_vm_rsid` drop dance. Pre-mutation snapshot to `archive/strand-collapse/`.
No schema change; no `variant_id_seq` resync (allocates no new `variant_id`s).

## Predicted deltas (regression anchor; GATE-FILL; conditioned on PR 5b-pre)

Author the model/sign; **VSC-User fills the exact gate numbers** post-run.

| Quantity | Baseline | Model → predicted |
|---|---|---|
| `variants_master` rows | 3,088,917 | one dead per edge → **−≈684** (GATE-FILL) |
| `genotype_calls` total | — | **+≈10** (complemented inserts; repoints add 0) |
| `consensus_total` | 3,088,916 | **−≈684** (one per deleted dead; chr4 dead had none) |
| `single_source` | 822,048 | **−≈657** (no-call 1-chip deads; strandflip/hom flips cancel) |
| `imputed_only` | 2,146,324 | **−≈22** (10 swap deads + 12 survivors flipping to single_source); the ≈523 no-call survivors **stay** imputed_only (PR 5b-pre) |
| `both_concordant` | 120,516 | **−≈3** = −4 (no-call 2-chip deads) +1 (chr4) |
| `disagreement_resolved` (post-align) | 1 | **0** (chr4 → both_concordant) |
| `strand_flip_resolutions` (merge counter) | 2 | **0** |
| `align-tier3 rows_deleted` | 1 | **0** |
| `genotype_mismatch` (gate-anchored) | 0 | **0** (measured: all edges reconcile) |
| `discrepancies` (only `genotype_mismatch` anchored) | — | reshuffle: `platform_unique` ↓≈657, `no_call_diff` ↑≈660, `strand_flip_resolved` −1 (GATE-FILL) |
| concordance (gate-anchored) | 0.999776 | **0.999776 (held)** — discordant side (0 mismatch + 27 strand_ambiguous) untouched; the shared side shifts by a handful against ~120,540, so the 6-figure rate does not move. A drop ⇒ a spurious `genotype_mismatch` ⇒ stop-and-investigate. |
| index `row_count` | finding-025 | **−≈97** (the 97 dead index rows deleted; rsID-keyed annotations relocate onto survivors) |
| index coord-keyed (`gnomad`/`clinvar`/`is_rare`/`is_ultrarare`) | finding-025 | **≈ unchanged** (deads non-canonical/`(N,N)` — never coord-matched) |
| index rsID-keyed (`gwas`/`pharmgkb`) | finding-025 | relocated, ≈held or small rise (GATE-FILL) |

**Dry-run gate:** ≈684 actionable = `no_call_repointed ≈660` + `no_call_dropped = 1`
+ 10 swap + 5 strandflip + 5 hom_opp + 3 hom_same; `legit_multiallelic_skipped ≈
10,014`; `genotype_mismatch_skipped = 0`; `source_collision_skipped = 0`;
`degenerate_skipped ≈ 1`. `variants_master_deleted` equals the sum.

## Verification

Automated (synthetic): `backend/tests/test_strand_collapse.py` — one fixture per
mechanism, the size-3 DROP (rule-3 tripwire: `(N,N)` dropped, both alts survive),
legit-multiallelic protection, genotype-mismatch + source-collision + degenerate
skips, the inverted imputed-relocation test, `--dry-run` mutates nothing, rsID
coalesce/conflict, downstream clears, idempotence, two-pair generality, and **two
integration tests** that run real `merge_all()` after collapse: a strandflip →
single `both_concordant`, and an imputed survivor + repointed chip no-call →
`imputed_only` (the finding-028 dependency).

Real-data (VSC-User gate, `docs/runbooks/verification.md`):
`collapse-duplicate-variants --dry-run` (confirm the per-mechanism counts + zero
mismatch/collision) → `collapse-duplicate-variants` → `merge` →
`align-tier3-consensus` (expect `rows_deleted=0`) → `refresh-index`; confirm every
predicted delta; lock the captured numbers as the post-collapse regression anchor.

## Follow-up

- finding-005 #1 is **closed** by this PR (ordering aspect closed in PR-3 /
  finding-020; the duplicate-collapse aspect closed here).
- The chip+imputed duplication mechanism (the source-strand gap that created the 15
  swap/strandflip dups + the no-call-meets-imputed duplication) is **finding-027**.
- The `pos_grch37` re-coalesce (finding-005 #9) is **not** folded in — still deferred.
- PR 5a (chrX resolution, Option B) lands separately.
