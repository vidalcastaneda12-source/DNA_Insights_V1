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

Pre-PR-3, `concordance = 1.0000` because zero genuine `genotype_mismatch`
calls survived in the corpus (CLAUDE.md "Real-data observations" #3). That
number is **misleadingly clean**: positions where the two chips actually
disagreed (e.g. 23andMe hom A/A keyed `(A,A)` vs Ancestry het A/G keyed
`(A,G)` at the same `(chrom, pos)`) were split into separate `variants_master`
rows by alphabetical keying. The merge's `_fetch_variant_pairs` pivots calls
per `variant_id`, so those two calls landed in two separate single-source
consensus rows and **were never compared**. The denominator silently excluded
the disagreements. The 1.0000 reflected "rate of agreement among pairs the
keying happened to put together," not "rate of agreement at shared positions."

After hom-only recovery + collision-collapse, those previously-split rows share
one `variant_id` and the merge compares them. Genuine disagreements (which
were always present in the raw data) surface as `genotype_mismatch`, the
denominator grows, and **concordance drops below 1.0000**. The drop is exposure
of biology the alphabetical keying had hidden.

The exact post-backfill rate is data-dependent; it is captured on the first
authoritative run and locked alongside the rest of the bedrock anchors below.
Magnitude bound: with `new_mismatches ≤ rows_collapsed` and the denominator
starting at `120,516`, the new rate lands in the high-`0.99x` range. If the
next independent verification run sees `concordance < 1.0000`, **see this
finding** — it is the post-PR-3 re-lock value, not a regression.

## Bedrock anchor re-lock (every long-standing real-data number)

Every project-wide anchor in CLAUDE.md "Real-data observations" #3 and #4 shifts
with this PR. The first authoritative real-data run against the user's loaded
corpus (dbSNP `157`, ClinVar `2026_05_17`, gnomAD `4.1.1`, GWAS `2026_05_16`,
PharmGKB `2025_07_05`) captures the new values; CLAUDE.md mirrors them in
lockstep. Drift on a re-run against the same corpus + same source versions is
a regression signal.

| Anchor | Pre-PR-3 (locked at finding-018 / CLAUDE.md obs #3-#4) | Post-PR-3 (capture & re-lock on first authoritative run) | Framing |
|---|---|---|---|
| Total chip-derived consensus rows | 942,620 | ↓ by `rows_collapsed` | Collapses remove `variants_master` rows; `consensus_genotypes` is 1:1. Correction — duplicates removed. |
| `both_concordant` | 120,516 | ~stable, possibly slight ↑ | Concordant pairs were mostly already keyed together; collapses that bring two concordant calls onto one variant add a small ↑. Largely unchanged. |
| `single_source` | 821,998 | ↓ materially | Previously-split same-position cross-chip pairs now collapse and get compared. Correction — visibility, not regression. |
| `disagreement_resolved` (consensus method count) | 106 | ~106 then 53 after `align-tier3` | Pre-`align`: merge tier-3 still writes consensus on both sides of each pair, so the count stays. Post-`align`: the non-canonical-side row is deleted, so one consensus row per pair (53). State both. |
| `strand_flip_resolutions` (merge counter) | 106 | ~106 | Scope-A leaves the strand-flipped `variants_master` duplicates as two rows; merge tier-3 keeps pairing them at the genotype level. The deferred PR-5 collapse will drive this toward 0 — see "Out of scope". |
| Palindromic shared variants | 31 | possibly ↑ slightly | If hom-only recovery creates new palindromic shared sites (23andMe hom A/A + Ancestry hom T/T at same pos both → `(A,T)`, both-called, disagree → `strand_ambiguous`). Capture; small. |
| `genotype_mismatch` | ~0 (1.0000 concordance implies negligible) | ↑ materially | The newly-compared previously-split pairs that genuinely disagree. Correction — exposes biology. |
| Concordance rate | 1.0000 | ↓ (high-0.99x; lock actual) | See "Concordance re-lock" above. Correction — denominator now reflects shared positions, not the subset alphabetical keying co-located. |
| Shared-call concordance (obs #3) | 1.0000 | ↓ (same number, same mechanism) | Identical row; same framing. |
| Phase 4 Beagle imputed-only consensus | 2,267,751 | likely stable | Imputed calls are upserted by variant key; canonicalize doesn't re-run imputation. Verify the count is stable as a *negative* anchor. |
| Phase 4 chip+imputed overlap | 101,420 | may shift slightly | Some chip-keyed variants change orientation; the overlap join is keyed by `variant_id` post-collapse. Verify and re-lock if changed. |
| `gnomad_matches` (index) | 101,501 | ↑ dramatically (hundreds of thousands) | Reorient doubles genuine matches; hom-only recovery makes most remaining hom-ref positions coord-matchable. Correction — the finding-018 re-lock. |
| `clinvar_matches` (index) | 2,559 | ↑ dramatically | Same mechanism, smaller absolute (ClinVar is sparser at these positions). |
| `gwas_matches` (index) | 66,726 | unchanged | rsid-keyed, orientation-independent. |
| `pharmgkb_matches` (index) | 1,737 | unchanged | rsid-keyed. |
| Index `row_count`, `is_rare`, `is_ultrarare` | 159,658 / 848 / 421 | all rise | More variants become allele-matchable, including rarer ones. |

`variant_annotations_index` `gnomad_matches` and `clinvar_matches` are the
headline numbers; the merge anchors are the most-likely-to-alarm. The
deferred PR-5 strand-flip `variants_master` collapse will move
`strand_flip_resolutions` and the post-`align` `disagreement_resolved` count
toward 0; this PR holds them at ~106 / ~53 respectively and tracks the
collapse as a known deferred sub-item (see finding-005 #1).

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

Under Scope A the ~106 strand-flipped `variants_master` duplicates remain as
two rows: the side whose allele set matches dbSNP gets canonicalized; the
complement-only sibling stays as-is and matches nothing on the index. But
`merge._apply_strand_flip` writes `consensus_genotypes` for **both**
`variant_id`s in the pair (the inner loop runs twice per pair, so
`strand_flip_resolutions=106` = 53 pairs × 2 row-rewrites). Result: consensus
lives on both variant_ids, annotations only on the canonical one — Phase 6
reads would see 106 variant_ids with `consensus_genotypes` but no
`variant_annotations_index` row.

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
