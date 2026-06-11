# Finding 020 — Canonical REF/ALT backfill + hom-only recovery

## Context

`variants_master` was populated by Phase 2's alphabetical-ordering normalize
(`backend/src/genome/ingest/normalize.py` `order_alleles`), which stores the
observed allele pair in alphabetical order. Two consequences, both quantified on
the user's real corpus by [`finding-018`](finding-018-variant-index-allele-match-rate.md):

- **78.3% of rows (738,424 / 942,620) are hom-only `ref==alt`** — Phase 2's
  honest "we don't know the reference" encoding for positions where every
  observation is homozygous. These rows match nothing on the 4-tuple coordinate
  join used by `variant_annotations_index`, and they were dropped from
  imputation per [`finding-005`](finding-005-deferred-improvements.md) #6.
- **~50% of genuine `ref≠alt` rows match gnomAD only when `(ref,alt)` is
  swapped** (101,918 of 204,196 — finding-018 §2) — pure alphabetical-order
  artifact relative to dbSNP's reference orientation.

This is the second of the post-5.7 backfills (the first was
[`finding-019`](finding-019-variant-aliases-backfill.md) — `refresh-aliases`).
It closes [`finding-005`](finding-005-deferred-improvements.md) #1 (the ordering
aspect — strand-flip `variants_master` collapse is deferred to PR 5; see "Out of
scope" below) and #6 (hom-only recovery), and is the deliberate re-lock event
finding-018 anticipated.

## Concordance re-lock — correction, not regression

**The merge's shared-call concordance rate WILL drop from 1.0000. This is the
backfill working as designed, not a regression.**

The merge (`backend/src/genome/merge/pipeline.py`) computes:
```
shared      = both_concordant + disagreement_resolved
discordant  = genotype_mismatch + strand_ambiguous
concordance = shared / (shared + discordant)
```

Pre-PR-3, `concordance = 1.0000` because zero discordant calls survived in the
corpus (CLAUDE.md "Real-data observations" #3). That number is **misleadingly
clean**: positions where the two chips' hom-only calls were recorded under
different alphabetical keys (e.g. 23andMe hom `A/A` keyed `(A,A)` vs Ancestry hom
`T/T` keyed `(T,T)` at the same `(chrom, pos)`) were split into separate
`variants_master` rows. The merge's `_fetch_variant_pairs` pivots calls per
`variant_id`, so those two calls landed in two separate single-source consensus
rows and **were never compared**. The denominator silently excluded them. The
1.0000 reflected "rate of agreement among pairs the keying happened to put
together," not "rate of agreement at shared positions."

After hom-only recovery + collision-collapse, those previously-split rows share
one `variant_id` and the merge compares them. The gate measured the result:
**concordance = 0.999776** (locked — recon A confirmed correct unification,
below). The drop is driven **entirely** by the **27 palindromic
`strand_ambiguous` no-calls** of population A in the "Post-canon classification
model" below — two hom-only rows at one `(chrom, pos)` whose recovered alleles
form an A/T or C/G pair, both-called, that disagree on a strand convention
genotype alone cannot resolve (`consensus.py:_resolve_both_called` →
`is_palindromic_site` → `unresolvable`). They enter the discordant side
(`shared` 120,516 / `shared+discordant` 120,543 = 0.999776). The feared
`genotype_mismatch` flood did **not** appear — `genotype_mismatch = 0`. The drop
is narrow and entirely strand-ambiguity, **not** a wave of genuine biological
disagreement.

This supersedes the earlier "high-0.99x" hand-wave — for the right reason: the
magnitude was never bounded a priori (the only bound, `new_mismatches ≤
rows_collapsed`, is the same order as the denominator), so the value is whatever
the gate measures — here 0.999776, set by 27 palindromic no-calls, not by a
`rows_collapsed`-scaled mismatch count. The authoritative post-PR-3 rate is
**0.999776**. **Recon A verdict: correct unification** — VSC-User's inspection
confirmed the 27 sit at 27 *distinct* palindromic sites (two genuinely-same-site
hom-only rows the alphabetical keying had split, now honestly held as a no-call —
an improvement), not *over-collapse* (canonicalize merging two distinct variants,
which would manufacture a false disagreement). If an independent run sees
`concordance < 1.0000`, **see this finding** — it is the post-PR-3 re-lock value,
not a regression.

## Bedrock anchor re-lock (every long-standing real-data number)

Every project-wide anchor in CLAUDE.md "Real-data observations" #3 and #4 shifts
with this PR. The first authoritative real-data run against the user's loaded
corpus (dbSNP `157`, ClinVar `2026_05_17`, gnomAD `4.1.1`, GWAS `2026_05_19`,
PharmGKB `2025_07_05`) captures the new values; CLAUDE.md mirrors them in
lockstep. Drift on a re-run against the same corpus + same source versions is
a regression signal. (The GWAS cache that the gate actually loaded is the
`2026_05_19` release — one epoch newer than the `2026_05_16` finding-018 locked
against, hence pre-canon `gwas_matches` 66,724 vs the 66,726 in finding-018. The
loader stamped the in-DB version row with a *June* label against this May cache;
that label↔data decoupling is [`finding-022`](finding-022-loader-version-label-decoupling.md),
distinct from this corpus-date correction.)

| Anchor | Pre-PR-3 (locked at finding-018 / CLAUDE.md obs #3-#4) | Post-PR-3 (capture & re-lock on first authoritative run) | Framing |
|---|---|---|---|
| Total chip-derived consensus rows | 942,620 | **942,592** (gate-measured; ↓ by 28 net) | Net chip Δ = −28 = −27 (population A collapse) − 1 (`align-tier3` deletes the non-canonical side of its 1 examined pair). Of `rows_collapsed`=121,454, 121,427 fall on the imputed-only side (population C) and only 27 on the chip side; the extra −1 is the post-merge `align-tier3` deletion, not a collapse. |
| `both_concordant` | 120,516 | **120,516** (gate-measured; held) | Held exactly — the collapse moved no row into or out of this bucket (classification model; partition-confirmed). |
| `single_source` | 821,998 | **822,048** (gate-measured; partition-confirmed) | Net +50 = −54 (27 palindromic duplicate-pairs collapse to a no-call) + ~104 (the reorient-moved ex-`disagreement_resolved` rows re-classify here on the post-canon merge — inferred from the disagreement-resolved drop + this measured net, since canonicalize re-keyed them so they are not `variant_id`-traceable; recon B). **Not** the predicted "↓ materially". |
| `disagreement_resolved` (consensus method count) | 106 | **1** (gate-measured, post-`align-tier3`) | The 106 reorient-movers re-classified on the post-canon merge — ~104 to `single_source`, leaving **2 post-merge**; `align-tier3` then deleted the non-canonical side of its 1 examined pair → **1 final**. The earlier "stays mid-double-digits" prediction is superseded. |
| `strand_flip_resolutions` (merge counter) | 106 | **2** (gate-measured) | The **merge counter** (rewrites during merge) — distinct from the final `disagreement_resolved` consensus-row count of **1** above, because `align-tier3` runs *after* merge and the counter does not see its deletion. Canonicalize reoriented the swap-victims upstream, so merge tier-3 finds almost no single-source complement pairs left to resolve (recon B). The deferred PR-5 collapse drives the residual toward 0 — see "Out of scope". |
| Palindromic shared variants | 31 | **31** (gate-measured; held — het def) | Defined as **het** palindromic both-called sites (both alleles observed) — strand-invariant, hence trivially concordant and unchanged by canonicalize: exactly 31 pre and post. The site-level count of *all* palindromic both-called rows is 6,681 post-canon (31 het + 18 hom-alt + 6,605 hom-ref + 27 unresolvable); the 6,623 hom rows are REF/ALT-backfill reveals (the unobserved allele was NULL until hom-only recovery) and the 27 are population-A collisions already counted under `unresolvable` — neither belongs in this anchor. See finding-023. |
| `genotype_mismatch` | ~0 (1.0000 concordance implies negligible) | **0** (gate-measured) | The feared flood did **not** materialise — zero genuine non-palindromic disagreements surfaced. The concordance drop is entirely the 27 palindromic `strand_ambiguous` no-calls (recon A), not `genotype_mismatch`. |
| Concordance rate | 1.0000 | **0.999776** (gate-measured) | See "Concordance re-lock" + recon A. Driven by 27 `strand_ambiguous` entering the denominator (120,516 / 120,543). Recon A **confirmed correct unification** — 27 distinct palindromic sites, not over-collapse. |
| Shared-call concordance (obs #3) | 1.0000 | **0.999776** (gate-measured) | Identical row; same framing. |
| Phase 4 Beagle imputed-only consensus | 2,267,751 | **2,146,324** (gate-measured; Δ −121,427) | **Not stable.** Δ −121,427 == `survivors_enriched`: imputed-only survivors absorbed colliding chip movers and flipped to chip-derived; the mover is removed in the same collapse, so chip-derived stays ~flat (classification model, population C). |
| Phase 4 chip+imputed overlap | 101,420 | **222,847** (gate-measured; validated at 101,420 pre-canon) | Canonicalize reoriented and hom-only-recovered chip variants so ~2.2× more coordinate-match the imputation panel and gain an appended imputed call — same mechanism as the `gnomad_matches` rise. Definition: chip-derived consensus rows whose `contributing_calls` include an imputed `genotype_calls` row. |
| `gnomad_matches` (index) | 101,501 | **2,796,952** (gate-measured) | Reorient + hom-only recovery made nearly the whole corpus coord-matchable — the finding-018 re-lock (~27× the pre-canon count). |
| `clinvar_matches` (index) | 2,559 | **61,458** (gate-measured) | Same mechanism, smaller absolute (ClinVar is sparser at these positions). |
| `gwas_matches` (index) | 66,726 (finding-018) | **66,701** (gate-measured; 66,724 pre-canon swept → 66,701 post-canon, Δ −23) | **Not unchanged:** collapse-dedup. When two rows both GWAS-matched on the same rsID collapse to one survivor, the index loses one match-bearing row (rsid-keyed ≠ collapse-immune). See recon C. |
| `pharmgkb_matches` (index) | 1,737 | **1,737** (gate-confirmed, unchanged) | rsid-keyed; the rsID-preservation invariant held — no same-rsID collapse reduced it. |
| `survivors_enriched` (`CanonicalizeResult`) | N/A (new identifier) | **121,427** (gate-measured) | Reused imputed-only survivors (NULL rsID) whose rsID was filled from a colliding chip mover — the dominant rsID-rescue path, and population C of the classification model. |
| `rsid_conflicts` (`CanonicalizeResult`) | N/A (new identifier) | **1** (gate-measured) | One genuine real-rs#-vs-real-rs# collision on a canonicalized key (lowest-`variant_id` wins, loser warned). #66's sweep made coalescing almost redundant, but this 1 genuine case justifies retaining it — see finding-021 amendment. |
| Index `row_count`, `is_rare`, `is_ultrarare` | 159,658 / 848 / 421 | **row_count 2,824,229 / is_rare 163,160 / is_ultrarare 103,261** (gate-measured) | row_count rose ~17.7×; `is_rare` / `is_ultrarare` rose far more (~192× / 245×) because the pre-canon index was chip-only (common-biased, 0.8% rare) while the now-matchable imputed corpus carries the rare tail arrays can't capture — rare-fraction-of-matched rose 0.84% → 5.83%. `is_ultrarare` ⊂ `is_rare` ⊂ `gnomad_matches` holds (103,261 < 163,160 < 2,796,952). |

`variant_annotations_index` `gnomad_matches` and `clinvar_matches` are the
headline numbers; the merge anchors are the most-likely-to-alarm. The gate
measured the `strand_flip_resolutions` merge counter at **2** and the final
post-`align-tier3` `disagreement_resolved` consensus-row count at **1** (the
counter is blind to the post-merge align deletion) — the canonicalize
reorientation subsumed the tier-3 strand-flip work upstream (see "Post-canon
classification model", recon B), far below the pre-gate prediction (which expected
these to stay near the pre-canon 106). The deferred PR-5 strand-flip
`variants_master` collapse drives the residual toward 0 and tracks the collapse
as a known deferred sub-item (see finding-005 #1).

## Post-canon classification model (gate-measured, first authoritative run)

**Row convention:** exactly one `consensus_genotypes` row per `variants_master`
row — post-canonicalize, one consensus row per variant. The `consensus_method`
partition below is `COUNT(*) GROUP BY consensus_method`, so `disagreement_resolved`
is a **row count** (106 pre-canon), independent of the `strand_flip_resolutions`
*counter* (`_apply_strand_flip` advances it once per rewritten row); the two are
distinct quantities that merely coincide at 106. The pre-canon partition sums to
the 942,620 chip anchor: 120,516 + 821,998 + 106.

| `consensus_method` (chip-derived, `NOT is_imputed`) | pre | **post-merge** | Δ |
|---|---|---|---|
| `both_concordant` | 120,516 | 120,516 | 0 |
| `single_source` | 821,998 | 822,048 | +50 |
| `disagreement_resolved` | 106 | 2 | −104 |
| `unresolvable` (27 `strand_ambiguous` + 0 `genotype_mismatch`) | ~0 | 27 | +27 |
| **chip-derived total** | 942,620 | 942,593 | −27 |

(This table is the **post-merge** state — before `align-tier3-consensus`. The
align step then removes 1 `disagreement_resolved` row; the final post-align
numbers are in the bedrock anchor table and the "Bridge to final" note below.)

Three independent populations, each closing against the captured totals
(`consensus_total` 3,210,371 → 3,088,917; `rows_collapsed` 121,454;
`imputed_only` 2,267,751 → 2,146,324; `survivors_enriched` 121,427):

- **Population A — palindromic (27 events).** Two *pre-canon* hom-only
  `single_source` variant rows at one `(chrom, pos)` (a duplicate the alphabetical
  keying split) collapse to one post-canon `unresolvable` row via
  `_resolve_both_called`'s palindromic branch. Per event: single_source −2,
  unresolvable +1, chip −1, consensus_total −1. ×27 ⇒ single_source −54,
  unresolvable +27, chip −27. **Newly compared** (never in the pre-canon
  denominator) — this *is* the entire concordance drop (shared 120,516 / 120,543).
  A **separate population from the 106**, not pre-canon disagreements.
- **Population B — strand-flip (106 → 2, post-merge).** The 106 pre-canon
  `disagreement_resolved` rows are **reorient-movers**: canonicalize re-keyed them
  (the `genuine_reorient` class assigns fresh `variant_id`s — VSC-User's snapshot
  confirms 106 such rows in the snapshot, **zero surviving by `variant_id`**). On
  the post-canon merge those re-keyed variants re-classify: **~104 land in
  `single_source`**, leaving 2 as `disagreement_resolved` (post-merge). The +104 is
  **inferred** from the `disagreement_resolved` drop plus the measured
  `single_source` net of +50 — *not* `variant_id`-traced, since the ids changed.
  "No rows removed" still holds (a re-key is a 1→1 row move), so the −104/+104
  cancel and `consensus_total` is unaffected by this population.
- **Population C — imputed-flip (121,427).** An imputed-only survivor (NULL rsID)
  absorbs a colliding chip mover: the survivor flips to chip-derived **and the
  mover is removed in the same collapse**, so the chip-derived count nets 0 while
  `imputed_only` and `consensus_total` each drop 1. ×121,427 == the `imputed_only`
  Δ and the `survivors_enriched` count.

**Closures (post-merge).** single_source Δ = −54 (A) + 104 (B) = **+50** ✓.
chip-derived Δ = **−27** (A only) ✓ (942,620 → 942,593). `imputed_only` Δ =
**−121,427** (C) ✓. `consensus_total` Δ = −121,427 (C) − 27 (A) = **−121,454** =
−`rows_collapsed` ✓ (3,210,371 → 3,088,917). `rows_collapsed` = 121,427 (C) + 27
(A) = **121,454** ✓.

**Bridge to final — `align-tier3-consensus`.** The table and closures above are the
**post-merge** state. `align-tier3` then runs — it examines 1 pair and deletes the
non-canonical-side `consensus_genotypes` row — giving the **final** post-align
numbers the bedrock table locks: `disagreement_resolved` **2 → 1**, chip-derived
total **942,593 → 942,592**, `consensus_total` **3,088,917 → 3,088,916** (=
3,210,371 − 121,454 collapse − 1 align-tier3). `strand_flip_resolutions` stays **2**
— it is a merge counter, blind to the post-merge align step. Every captured anchor
reconciles; no row is unaccounted for.

### Recon A — verdict on the 27 (correct unification vs over-collapse)

The concordance drop is benign **only if** the 27 are genuinely-same-site
palindromic unifications. VSC-User ran this and **confirmed correct unification**
— 27 *distinct* palindromic sites:

```sql
-- each strand_ambiguous no-call should sit at one (chrom,pos) with a single
-- canonical (ref,alt) and two contributing chip calls (one per platform):
SELECT cg.variant_id, vm.chrom, vm.pos_grch38, vm.ref_allele, vm.alt_allele,
       cg.contributing_calls
FROM consensus_genotypes cg
JOIN variants_master vm USING (variant_id)
JOIN discrepancies d ON d.variant_id = cg.variant_id
WHERE d.discrepancy_type = 'strand_ambiguous';
```

Correct unification ⇒ one genuine biallelic palindromic site per row, both
platforms' hom-only calls now compared (an honest no-call — an improvement);
over-collapse would have shown two distinct variants merged (inconsistent alleles /
unrelated calls). The result was the former, so concordance **0.999776 is locked**.

### Recon B — canonicalize subsumes tier-3 strand-flip upstream

The 106 → 2 (post-merge) drop is population B: ~104 of the reorient-moved rows
re-classify to `single_source`, **not** collapsed away (`rows_collapsed` is fully
consumed by C + A, leaving no room for a collapse explanation). VSC-User's partition
**confirmed** the post-align row counts (120,516 / 822,048 / **1** / 27 = 942,592),
and the snapshot **confirmed** the movers (106 `disagreement_resolved` rows in the
snapshot, **zero surviving by `variant_id`**):

```sql
-- partition — confirmed 120,516 / 822,048 / 1 / 27 (post-align):
SELECT consensus_method, COUNT(*)
FROM consensus_genotypes WHERE NOT is_imputed
GROUP BY consensus_method;
```

The per-`variant_id` fate-trace returns **empty by construction** — canonicalize
re-keyed the movers, so the 106 pre-canon `disagreement_resolved` `variant_id`s do
not survive. To row-confirm where the ~104 landed, join the snapshot to the current
state on `(chrom, pos_grch38)`, not `variant_id`.

### Recon C — `gwas_matches` −23 (collapse-dedup)

`gwas_matches` is rsid-keyed and orientation-immune, but **not** collapse-immune.
`66,726` (finding-018, `2026_05_16` GWAS epoch) → `66,724` (pre-canon, swept
`2026_05_19` corpus; the −2 is the epoch difference, not canonicalize) → `66,701`
(post-canon, **Δ −23** through canonicalize). The loader cache-skew (finding-022)
cannot produce a pre→post delta — the same loaded GWAS data sits on both sides —
so the −23 is a canonicalize effect: when two `variants_master` rows that **both**
GWAS-matched on the same rsID collapse onto one survivor, the index (one row per
`variant_id`) loses one match-bearing row. The 1 `rsid_conflict` plus ~22
same-rsID collapses ≈ −23. **Do not** record this as "within 1-2 rows of locked."
VSC-User confirms by counting distinct gwas-matched `variant_id`s pre vs post
against the snapshot (`COUNT(*) … WHERE gwas_trait_count > 0`).

### rsID preservation — an invariant across collapse

The collapse keeps one survivor row per canonical key. The first implementation
inherited only *that one row's* `rsid` and discarded every other collapsed
mover's — losing ~115,662 distinct rsIDs on the first real-data run and dropping
the rsid-keyed match counts (`gwas_matches` 66,726 → 55,047, `pharmgkb_matches`
1,737 → 1,411), the opposite of the "unchanged" re-lock above. Two collapse
paths leaked: the **new-survivor** path copied the `MIN(old_variant_id)`
representative's rsID (rsid-blind), and the **reuse** path adopted an existing
sibling's `variant_id` as survivor — typically a NULL-rsID imputed-only row
(Beagle ID `.`) — so a colliding chip swap-victim's rsID vanished (the dominant
~100K case, matching the chip+imputed overlap of 101,420).

The fix makes rsID-preservation an **invariant**: post-run rsid set ⊇ pre-run
rsid set, except where two genuinely-distinct non-NULL rsIDs collide on one
canonical key (unavoidable in a single-`rsid`-column schema). A connection-scoped
`_canon_best(survivor_id, best_rsid, distinct_rsids)` TEMP table aggregates the
best non-NULL rsID across *all* movers per survivor —
`arg_min(rsid, variant_id) FILTER (WHERE rsid IS NOT NULL)`, lowest-`variant_id`
wins. The new-survivor INSERT sources `COALESCE(best_rsid, rep.rsid)`; the reuse
survivor is filled by a TX2 `UPDATE … SET rsid = COALESCE(vm.rsid, best_rsid)`
(survivor's own non-NULL rsID always wins). That UPDATE is **not** intrinsically
FK-safe: `rsid` carries the plain `idx_vm_rsid` index, and DuckDB delete+reinserts
a row whenever an UPDATE touches an *indexed* column — which fires the parent-side
`genotype_calls.variant_id` FK check on a survivor that has calls (verified against
DuckDB 1.5.3; `_RECOMPUTE_FLAGS_SQL` is exempt only because `has_*_call` are
unindexed). The orchestrator therefore drops `idx_vm_rsid` **committed, before TX2
opens** (an in-TX drop is invisible to DuckDB's pre-transaction FK check — the same
quirk that forces the TX split) and rebuilds it in a `finally`, so a TX2 failure
can't strand the DB without the index. Conflicts (a survivor's movers disagreeing,
or a reuse survivor's own rsID disagreeing with the pick) are counted in
`rsid_conflicts` and emit a `canonicalize.rsid_conflicts` warning — surfaced, never
silently dropped.

## Hom-only multi-alt surfacing caveat

For a hom-ref position with multiple single-base dbSNP alts (e.g.
`alt_alleles=['T','C','G']`), the canonicalize step picks the alphabetically
smallest alt (`MIN(alt_b)`) and assigns it as the row's ALT. The user is hom-ref
so dosage is 0 regardless of which alt we pick — the choice does **not** change
the user's genotype interpretation. But it **does** determine which annotation
rows the row joins to after `refresh-index` (annotations are keyed on the full
4-tuple, allele-specific).

**Consequence to communicate to downstream readers:** a hom-ref multi-alt
`variant_annotations_index` entry reflects **one arbitrary alt's** annotation,
not the full position's clinical context. Example: at a position where dbSNP
has `alt=['G','T']`, ClinVar flags `A>T` as Pathogenic and `A>G` as Benign, and
the user is hom-ref `A/A` (carries neither alt), the index entry surfaces the
`A>G` Benign annotation (alphabetically-first alt) — the Pathogenic `A>T` is
silent at this row. The user doesn't carry the variant either way, so no
clinical call is mis-stated; but a UI that displays "ClinVar significance at
this variant" should not be read as "the clinical significance at this
position." Phase 6/7 may revisit per-alt hom-ref surfacing if a consumer needs
it.

The `mapping_kind='hom_ref_recover_multialt'` count in `CanonicalizeResult` is
the visible signal for how many index rows have this caveat.

## Design decisions

### 1. Mapping: ordering reorient + hom-only recovery; no complement (Scope A)

The mapping (built in `_BUILD_CANON_MAP_SQL`) covers three kinds:

- **`genuine_reorient`** — `ref≠alt`, observed allele set `{X,Y} ==
  {dbSNP.ref, some single-base alt_b}`. Target: `(dbSNP.ref, the-other-base)`.
  Rows whose stored `(ref,alt)` already matches dbSNP orientation are excluded
  by the no-op filter `WHERE (ref_c, alt_c) <> (old_ref, old_alt)`.
- **`hom_ref_recover` / `hom_ref_recover_multialt`** — `ref==alt`, observed
  base `B == dbSNP.ref`. Target: `(B, alt_b)` where `alt_b` is the
  alphabetically-smallest single-base dbSNP alt. The multi-alt suffix flags the
  surfacing caveat above.
- **`hom_alt_recover`** — `ref==alt`, observed base `B != dbSNP.ref` and
  `B ∈ single-base dbSNP alts`. Target: `(dbSNP.ref, B)`; dosage will resolve
  to 2 on re-merge.

Rows that match dbSNP only after reverse-complement (true strand-flipped
duplicates — the ~106 tier-3 cases in real data) are **not** complement-mapped
here. Scope A leaves them as two `variants_master` rows; merge tier-3 keeps
resolving them at the genotype level as today (`strand_flip_resolutions`
stays ~106). The minimal post-merge cleanup is `align-tier3-consensus` (§3
below). Full `variants_master` collapse for those pairs is deferred to PR 5
(strand architecture) and tracked under finding-005 #1.

Per `old_variant_id`, the candidate set is reduced to one target via
`ROW_NUMBER()` with a kind-priority order (`genuine_reorient` > `hom_ref` >
`hom_ref_multialt` > `hom_alt`) and `(ref_c, alt_c)` as the deterministic
tie-break — so re-runs against the same corpus + same dbSNP source-version
produce byte-identical output (drift is a regression signal).

### 2. Why we INSERT new `variant_id`s for movers instead of UPDATEing in place

DuckDB enforces the `uq_variant_position UNIQUE (chrom, pos_grch38,
ref_allele, alt_allele)` constraint via the ART index, and an UPDATE that
touches an indexed column is implemented internally as DELETE + INSERT on the
index. With `genotype_calls.variant_id` declared `REFERENCES
variants_master(variant_id)` (ddl/group_1_genotype.sql:117), even an UPDATE
that leaves `variant_id` unchanged trips DuckDB's FK check (the index sees the
inner DELETE as orphaning a still-referenced PK). DuckDB has no
`DISABLE FOREIGN_KEYS` pragma, no `ALTER TABLE DROP CONSTRAINT`, and no
`SAVEPOINT`.

The only mechanic that works:
1. Allocate a fresh `variant_id` for each canonical target key (or reuse an
   existing unchanged sibling's id when one already sits at the target).
2. INSERT the canonical row.
3. UPDATE `genotype_calls.variant_id` to point to the survivor.
4. DELETE the old mover rows (their FK refs are gone).

Unchanged rows that happen to already sit at a target key are reused as
survivors so we don't introduce avoidable churn (e.g. a hom-only `(A,A)` that
recovers to `(A,G)` and finds an existing genuine `(A,G)` sibling: the genuine
sibling becomes the survivor, no new id allocated, the hom-only call
re-points to it, the hom-only row is deleted).

Consequence: **`variant_id` is NOT preserved for movers** (re-oriented or
recovered rows). This is acceptable because every consumer of `variant_id` is
either (a) downstream-regenerated (`consensus_genotypes`, `discrepancies`,
`variant_annotations_index` — all DELETEd during the canonicalize step and
rebuilt by `merge` / `refresh-index`), or (b) precondition-empty in the PR-3
window (the Phase-6/7 derived/insight tables enumerated in
`_PRECONDITION_TABLES`).

**`variant_id_seq` re-sync (a consequence of the explicit allocator).**
`variants_master.variant_id` is the schema's only sequence-backed PK
(`DEFAULT nextval('variant_id_seq')`), and the ingest paths (`writer.py`,
`imputation.ingest`) omit `variant_id` and rely on that default. The allocator
above assigns survivor ids explicitly as `MAX(variant_id) + ROW_NUMBER()`
without advancing the sequence, so TX2 must re-sync `variant_id_seq` past the
new high-water mark afterward (`_resync_variant_id_sequence`) — otherwise the
next default-`nextval` ingest collides on the PK. DuckDB has no usable sequence
reset under the column-DEFAULT dependency (`CREATE OR REPLACE SEQUENCE` trips a
DependencyException; `ALTER SEQUENCE … RESTART` is unimplemented), so the
re-sync advances by draining `nextval` to `MAX(variant_id)` via
`SELECT max(s) FROM (SELECT nextval('variant_id_seq') FROM range(delta))`; the
volatile `nextval` must be materialized through `max(s)` or DuckDB prunes it
under a `count(*)` wrapper. Dropping `variant_id_seq` in favor of the
`MAX`-based allocator the annotation tables already use is a candidate schema
follow-up that would remove this asymmetry entirely.

### 3. Three-transaction split

DuckDB's FK enforcement on a row delete reads the *pre-transaction* state of the
*referencing* table, so an in-transaction DELETE of the referencing rows is
invisible to the check. Two distinct FKs hit this, forcing a three-way split on
the same connection:

- **TX0**: `DELETE FROM discrepancies` and commit. `discrepancies` is the only
  table whose FK points *onto* `genotype_calls` (`call_a_id` / `call_b_id` →
  `genotype_calls(call_id)`). The TX1 repoint `UPDATE genotype_calls SET
  variant_id` is executed by DuckDB as delete+reinsert of each row (`variant_id`
  carries its own FK's ART index), which fires that parent-side check; it must
  already see `discrepancies` empty as of a committed transaction.
- **TX1**: stage `_canon_map` / `_canon_resolve` / `_canon_remap`, DELETE the two
  `variants_master`-keyed rollups (`consensus_genotypes` /
  `variant_annotations_index`), INSERT new survivor rows, UPDATE
  `genotype_calls.variant_id` to point to them. Commit.
- **TX2**: DELETE the now-orphan old mover rows (keyed off the still-live
  connection-scoped `_canon_map` TEMP, which survives the TX1 commit; the same
  quirk again — the repoint away from the movers must be committed first),
  recompute survivor `has_*_call` flags, then re-sync `variant_id_seq` past the
  explicitly-allocated survivor ids (see §2). `commit_and_checkpoint`.

Crash windows are recoverable within the runbook: a crash after TX0 / before TX1
leaves `discrepancies` empty with `variants_master` unchanged; a crash after TX1
/ before TX2 leaves **harmless** orphan `variants_master` rows (no calls
reference them, downstream tables empty). A re-run of `canonicalize-variants`
DELETEs orphans as a no-new-survivors-needed pass, and `merge` /
`refresh-index` rebuild the downstream tables regardless. The supersession
atomicity guarantee (CLAUDE.md decision #7) is preserved at the *downstream*
boundary — a reader sees either the entire pre-canonicalize state or the entire
post-canonicalize state at the `consensus_genotypes` / `variant_annotations_index`
grain (those are wholesale-cleared here and re-derived by `merge` /
`refresh-index` after the canonicalize finishes).

### 4. Post-merge `align-tier3-consensus`

Under Scope A any strand-flipped `variants_master` duplicate that survives
canonicalize remains as two rows: the side whose allele set matches dbSNP gets
canonicalized; the complement-only sibling stays as-is and matches nothing on the
index. `merge._apply_strand_flip` writes `consensus_genotypes` for **both**
`variant_id`s in such a pair (the inner loop runs twice per pair, so the
`strand_flip_resolutions` counter advances by two per surviving pair). Result:
consensus lives on both variant_ids, annotations only on the canonical one — so
without cleanup, Phase 6 would see those variant_ids with `consensus_genotypes`
but no `variant_annotations_index` row. *(At design time this population was
expected to be the full ~106-row tier-3 set; the gate measured it at
`strand_flip_resolutions`=2 — one surviving pair — because canonicalize
reorientation subsumed the rest upstream. See "Post-canon classification model",
recon B.)*

The small companion command `genome annotate align-tier3-consensus` identifies
pairs of `variants_master` rows at the same `(chrom, pos_grch38)` where both
consensus rows have `consensus_method='disagreement_resolved'`, determines
which side matches a dbSNP 4-tuple (the canonical side), and `DELETE`s the
`consensus_genotypes` row on the non-canonical side. The non-canonical
`variants_master` row stays as a vestigial row with `genotype_calls` but no
`consensus_genotypes`. The surviving canonical consensus's
`contributing_calls` array already references both call_ids, so no information
is lost.

This is the minimal alignment that keeps Phase 6 reading exactly one
`variant_id` per real biallelic site without dragging `genotype_calls`
supersession into this PR. The full `variants_master`-level strand-flip
collapse is deferred to PR 5; see "Out of scope" below.

### 5. Backup / snapshot

The canonicalize CLI auto-snapshots `genome.duckdb` before the mutation
transaction opens (CHECKPOINT → `shutil.copy2` → chmod 0600), to
`archive/canonicalize/genome.duckdb.pre-canonicalize.dbsnp<version>.<UTC>.bak`
under the gitignored `archive/` snapshots dir. `--no-backup` skips it for
re-runs and space-constrained machines. The fast-path detector skips the
snapshot when the table is already canonical (nothing to protect).

**Restore (operator-driven, documented in `docs/runbooks/annotations.md`):**
```
# stop any process holding genome.duckdb, then:
cp archive/canonicalize/genome.duckdb.pre-canonicalize.<…>.bak data/genome.duckdb
chmod 0600 data/genome.duckdb
```

The snapshot is the rollback path for a successful-but-wrong backfill (the
in-transaction ROLLBACK only covers a crash). Auto-cleanup is manual — the
operator deletes the snapshot once the backfill is verified merged, to prevent
silent disk growth across re-runs.

## Provenance — operation-level + snapshot

No schema/DDL change (locked). Provenance for CLAUDE.md decision #8 is captured
at the operation grain by three artifacts that together provide complete
before/after coverage:

1. **The pre-mutation snapshot** (§5) = the literal "before" state. Naming
   includes the dbSNP version + UTC timestamp.
2. **This finding** (the "after" + method) — captures the dbSNP
   `source_version_id` used, the backfill date, the snapshot filename, the
   before/after locked counts (above), and **explicit query patterns** to
   derive "was this row canonicalized / hom-recovered" (below).
3. **structlog `canonicalize.complete`** — the durable in-log operation event
   stamped with `dbsnp_source_version_id`, all delta counts, and
   `wall_clock_seconds`.

### Query patterns for row-level "was this canonicalized?"

These reconstruct row-level provenance from the snapshot + current state when
needed:

- **Hom-recovered rows**: `SELECT vm.* FROM variants_master vm WHERE
  vm.ref_allele != vm.alt_allele AND vm.variant_id IN (SELECT variant_id FROM
  genotype_calls GROUP BY variant_id HAVING BOOL_AND(allele_1 = allele_2))` —
  variants whose genotype is unanimously homozygous across all calls but whose
  ref/alt now differs are by construction the recovered set.
- **Reoriented rows**: compare current `(ref_allele, alt_allele)` against the
  snapshot's same `variant_id` (the snapshot has the pre-canonicalize state;
  any row whose alleles swapped is a reorient). Movers got fresh
  `variant_id`s, so this comparison uses the snapshot's
  `(chrom, pos_grch38, variant_id)` against the current state — joins via
  `(chrom, pos_grch38)` since `variant_id` may not survive.

The structlog event is the authoritative operation record; finding-020 + the
snapshot are the durable artifacts.

## CLI shape

Two new standalone `annotate` subcommands; see
`docs/runbooks/annotations.md` "After a schema rebuild" for the reload
ordering:

```
genome annotate canonicalize-variants    # checkpoint → snapshot → mutate (3 txns)
genome merge                              # rebuild consensus_genotypes + discrepancies
genome annotate align-tier3-consensus     # delete non-canonical-side consensus rows
genome annotate refresh-index             # rebuild variant_annotations_index
```

`canonicalize-variants` flags: `--force` (bypass already-canonical fast-path),
`--no-backup` (skip pre-mutation snapshot). `align-tier3-consensus` takes no
flags. Each command commits independently and prints a one-line summary of
the locked drift identifiers; **the database between commands is transiently
stale** (e.g. between canonicalize and merge, `consensus_genotypes` is empty;
between merge and refresh-index, `variant_annotations_index` is empty) and
must not be read by Phase-6 consumers during the sequence.

## Out of scope (deferred)

- **Full `variants_master`-level strand-flip collapse for the ~106 tier-3
  pairs.** Would require complementing `genotype_calls.allele_1/2` via
  row-grain supersession (INSERT new + deactivate old) to keep dosage
  consistent. Deferred to **PR 5 (chrX/strand architecture)**; tracked in
  finding-005 #1 as an explicit deferred sub-item.
- **Tier-2 rsID matching via `variant_aliases`.** Separate PR 4; finding-005
  #4 / finding-019.
- **`genes` seed.** Phase 7.
- **Re-running Beagle imputation.** Hom-only recovery enables a *future*
  `genome imputation prepare` to include those rows (the `ref!=alt` filter at
  `backend/src/genome/imputation/vcf_export.py:191` is unchanged; recovered
  rows now satisfy it), but `imputation run` is a separate 30-min gated op
  the operator triggers when they want to re-impute.

## Follow-up

- Lock the post-PR-3 numbers (every row of the bedrock anchor table) on the
  first authoritative real-data run.
- Mirror the new numbers in CLAUDE.md "Real-data observations" #3 and #4 with
  parentheticals naming this finding for the framing trail.
- Manual cleanup of `archive/canonicalize/*.bak` once each backfill verifies
  and merges.
- Re-run `canonicalize-variants` after any future `genome annotate refresh
  --source dbsnp` that flips the dbsnp pointer (the canonical REF/ALT source
  has changed; the prior canonicalization may no longer match the new
  dbSNP).
