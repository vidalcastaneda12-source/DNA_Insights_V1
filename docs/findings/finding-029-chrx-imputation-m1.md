# chrX imputation via M1 panel diploidization

> **Note:** the title names M1 (the option first explored). The **shipped**
> mechanic is **M3-physical region split** (PR #74) — M1 was built, failed its
> falsifiability gate, and was deleted; the M1 narrative below is kept as the
> audit trail. See "M3-physical built (PR 5a)" below and finding-031. The filename
> is retained to preserve inbound links.

## Context

chrX imputation yielded **zero** variants for the male user. `finding-008`
established the proximate cause: Beagle 5.5's reference loader requires uniform
ploidy per sample across the whole chromosome and aborts on the 1000 Genomes
panel's male non-PAR haploid representation at the first position past PAR1
(`HG00096 … chrX:2785078 is haploid`). `finding-008` left two fix options open
((a) fake-diploid the panel, (b) sex-aware region split) plus a temporary (c)
skip-chrX.

PR 5a resolves this. The mechanic and representation were decided after a
planning-time probe against the on-disk panel (VSC-User hit terminal issues, so
the probe ran during planning). This finding records the probe, the decision,
the conditional fallback, and the first-run anchor shapes.

## The probe

| Probe | Result | Consequence |
|---|---|---|
| (e) ploidy coding | PAR1: all 3202 diploid. Non-PAR: 1598 haploid males (single-token GT) + 1604 diploid females. | The failure is a **within-sample** PAR1→non-PAR ploidy transition, not cross-sample mixed ploidy. |
| (a) as-is non-PAR window | runs clean (exit 0). | Beagle **accepts** cross-sample mixed ploidy (haploid males + diploid females at one site). |
| (a′) `chrom=<region>` on the **full** panel | fails (`HG00096 … chrX:2785078 is haploid`). | `chrom=` is a restrictor that does **not** dodge the load-time ploidy check → region-split needs **physical** panel subsets. **M3-via-`chrom=` is dead.** |
| (c) native Beagle haploid/X mode | none — usage exposes only `chrom=` | **M2 (a native haploid mode) is dead.** |
| (b) diploidized window | runs clean (exit 0). | **M1 validated** — diploidize male non-PAR → homozygous-diploid and the panel loads. |
| output ploidy | a diploid target yields **diploid output** (all records `\|`). | **R1 is seam-free** — no import re-haploidization step is needed. |
| (d) map label | the runner's `plink.chrX.GRCh38.map` col1 is already `chrX`; 23 stray `plink.chrchr*.GRCh38.map` files exist. | The genetic map is **install-hardening, not a blocker** (recon correction to the original concern). |
| target data (read-only) | 26,270 exportable chrX consensus rows (25,751 non-PAR + 519 PAR) post-canon hom-only recovery; ~0 pre-canon. | Real chrX to impute, recovered by PR-3 canonicalization (`finding-005` #6). |

## The decision: M1 (diploidize male non-PAR), R1 (diploid storage)

The probe **killed the alternatives** — M2 does not exist, and `chrom=` cannot
dodge the within-sample transition — leaving M1 (diploidize the full 3202-sample
panel, one Beagle run) versus M3-physical (region-split panel subsets + concat).

**M1 was chosen.** It is field-standard for diploid-only imputers. The user's own
genotype is preserved **losslessly** under R1: male non-PAR is stored as a
homozygous-diploid call (dosage 0/2), and the new `consensus_chrx_dosage_v` view
corrects the dosage back to hemizygous (2→1, 0→0) for a male profile. The only
distortion M1 introduces is **reference-side allele-frequency weighting** —
non-PAR male haplotypes are counted twice in the panel — which is bounded: it
does not reorder rarity, remove haplotypes, or touch annotation allele
frequencies (those come from gnomAD, not the panel).

M3-physical's only gain over M1 is faithful reference AF. It does **not** fix
biologically-impossible male hets (those arise from the R1 diploid *target* under
either mechanic and are caught by the het guard below), and it adds a
`bcftools concat` boundary seam plus a permanent chrX exception to the
one-run-per-chromosome runner. M1's single weak point — reference AF weighting —
is made **falsifiable at the gate** (see Verification), not assumed away.

### M3-physical — documented conditional fallback (NOT built)

M3-physical is the recorded fallback if the gate measurements (below) show M1 is
inadequate: split the panel into PAR1 / non-PAR / PAR2 physical subsets, impute
each with the appropriate ploidy for the user's sex, and `bcftools concat` the
per-region outputs. **Trigger:** non-PAR DR² materially below PAR/autosomal
DR² (beyond the modest reduction expected from lower non-PAR marker density),
**or** a `male_nonpar_het_anomaly` count more than a handful. It is not built;
this is the option-space record so a future session does not re-derive it.

## M1 failed the gate → M3-physical Task 0 probe (PASS, 2026-06-16)

M1's first authoritative real-data run **failed its own falsifiability gate**,
firing the M3-physical trigger above:

- 25,751 non-PAR male targets → **31** out at DR²>0.3, **29** of them
  biologically-impossible male hets → **net ~1 usable non-PAR variant**.
- Raw Beagle non-PAR output: **2,731,121** variants, mean DR² ≈ **0.0000**, only
  **55** > 0.3 (0.002%). `male_nonpar_het_anomaly = 30`.
- Internal control: PAR (un-diploidized) imputed normally — 519 anchors → 1,958
  calls. Non-PAR has 50× the anchors and yields 63× fewer. The genetic map is
  fine; whole-panel diploidization destroys non-PAR information content.

### Task 0 probe — does native-haploid (M3) recover non-PAR DR²?

PR 5a Task 0 (the hard build gate) probed whether imputing the **native**
(un-diploidized) panel — males left haploid in non-PAR — with a haploid male
target restores DR². Method: leave 12 panel males out of the reference, thin
those 12 to chip-like density (~190 typed markers/Mb, close to the real ~169/Mb
non-PAR chip density) as the target, `beagle ref= gt= map= impute=true` on a
2 Mb window, mean DR² over imputed (IMP) sites. All arms `bcftools`-built
against the on-disk panel; Beagle 5.5, **exit 0 everywhere**:

| Arm | Region | Target | Panel | imputed n | meanDR² | DR²>0.3 | DR²>0.8 | out ploidy |
|---|---|---|---|---|---|---|---|---|
| **Primary** | non-PAR chrX:49–51M | haploid | native | 22,172 | 0.026 | 503 (2.3%) | 306 (1.4%) | **haploid** `GT:DS` |
| PAR control | PAR1 chrX:0.5–2.5M | diploid | native | 69,277 | 0.070 | 7.7% | 2.1% | diploid |
| chr20 control | chr20:30–32M | diploid | native | 48,743 | 0.088 | 10.0% | 6.2% | diploid |
| non-PAR diploid-target | non-PAR chrX:49–51M (same) | diploid (re-dip) | native | 22,172 | 0.026 | 503 (2.3%) | 306 (1.4%) | diploid |

Findings:

1. **M3 is viable — decisively better than M1.** Native-haploid non-PAR yields
   **2.3% > 0.3** vs M1's **0.002%** (~1,100× more imputable sites), mean DR²
   0.026 vs 0.0000. Projected over ~152 Mb non-PAR this is order **10⁴**
   (tens of thousands of) usable variants — ≫ M1's net ~1, ≫ the PAR yield.
2. **Non-PAR sits ~3–4× below the diploid baselines** (2.3% vs PAR 7.7% /
   autosomal 10%). This is the *expected* chrX penalty — the non-PAR window has
   ~half the marker density of the autosomal window and a smaller effective Ne
   (1,598 male haplotypes are hemizygous, not 2×) — **not** a catastrophic
   failure. The low mean DR² in *every* arm (incl. diploid PAR/chr20) is the
   rare-variant tail of *raw* Beagle output; the production "mean 0.82" is the
   post-`DR²>0.3` figure.
3. **Haploid-dosage DR² deflation refuted.** Re-diploidizing the target on the
   identical window/ref gives byte-identical DR² stats (0.026 / 503 / 306) — the
   target's input ploidy does not change imputation quality. The plan's
   haploid-target choice is correct; there is no better target-diploidization
   variant to chase.
4. **Beagle emits HAPLOID output for a haploid target** (`GT:DS`, GT = `0`/`1`)
   → the R1 re-diploidization seam (plan Task 4) is **load-bearing, not a
   no-op**, and (since haploid→hom-diploid is deterministic) M3+R1 cannot
   introduce a male non-PAR het → `male_nonpar_het_anomaly` ≈ 0 by construction.

Probe limitations (do not change the verdict): one mid-core 2 Mb non-PAR window
(not whole non-PAR); 12-sample leave-out study (vs production's 1 sample) — DR²
noise affects all arms equally; DR² is Beagle's internal estimate, not measured
concordance. The real-data gate (Verification, below) measures the full
distribution.

**Verdict: GO** — the Task 0 hard gate passes. Build M3-physical (tasks 1–9).

## What landed in PR 5a

- **finding-008 safety fixes** (mechanic-independent): shared `imputation/bgzf.py`;
  the runner unlinks a partial output on failure and treats an EOF-less BGZF as
  *not* clean (so a truncated output is re-imputed, never skipped); the import
  step refuses a cleanly-closed-yet-empty chromosome whose prepare-manifest
  upload count was non-trivial (manifest-robust — skipped when no manifest).
- **Genetic-map hardening** (downgraded, not unblocking): a total, idempotent
  `normalize_map_chrom` (strips every `chr`, maps `23→X` / `24→Y`, re-emits one
  `chr`) applied on every install including the no-download path; stray
  `plink.chrchr*.GRCh38.map` cleanup; `validate_panel` positively asserts the
  chrX map's column 1 is `chrX`.
- **Minimal sex mechanism** (`imputation/sex.py`): `resolve_sex` / `profile_sex_label`
  resolve a determinate profile sex from an explicit `--sex` or the confident
  chip `sample_qc.sex_inferred` aggregate. `--sex {M,F,auto}` on `prepare` (manifest
  provenance) and `run` (a chrX-scope gate). **No DB column is written.**
- **`genome/par_regions.py`** + the **`consensus_chrx_dosage_v`** view (the sole
  DDL/schema touch — see "schema carve-out") + a post-merge **male-non-PAR-het
  guard** that counts `male_nonpar_het_anomaly` and records it idempotently on the
  imputed `sample_qc.qc_notes`.
- **M1 mechanic**: `genome imputation panel prepare-chrx` diploidizes the chrX
  panel via the probe-validated `bgzip | awk | bgzip` stream (non-PAR-gated;
  **not** `bcftools +fixploidy`), asserts the whole chromosome is haploid-free,
  and writes `chrX.diploidized.vcf.gz`; the runner points the chrX `ref=` at it
  and fails chrX clearly (rather than crashing) when it is absent.

### Schema carve-out

PR 5a is schema-free **except** the one `consensus_chrx_dosage_v` view, added to
both `docs/schemas/schema_group_1_genotype_data.md` and `ddl/group_1_genotype.sql`
and materialized on an existing DB via a targeted idempotent
`CREATE OR REPLACE VIEW` (`genome.db.init_schema.materialize_view`, read from the
canonical DDL). **View-only ⇒ no `rm -rf data/` rebuild** (PR #68 precedent); the
view is nonetheless compiled-and-tested through the real `genome init` path,
which raises on a view-compile failure.

### Sex-edge limitation + deferred remedy

`--sex` is **not persisted**, so the view derives profile sex in-SQL from chip
`sample_qc`. If a profile is **all-`ambiguous`** and no `--sex` is supplied, the
view's `profile_sex` resolves to `ambiguous` and the corrected-dosage view
**passes male non-PAR dosage through uncorrected** (it cannot know the sex). This
**cannot occur for this user** — 23andMe infers `M`, so the chip aggregate is a
determinate `M`. Deferred remedy: persist `--sex` to the existing
`sample_qc.sex_expected` column and `COALESCE` it into the view's `profile_sex`
CTE. (The chrX *run* additionally hard-gates on a determinate sex via
`resolve_sex`, so an ambiguous profile is told to pass `--sex` before a chrX run
rather than silently mis-correcting.)

## Verification (first-authoritative-run anchors — LOCKED, run_0002)

The gated chrX reload is VSC-User's independent verification, run with the
**5b collapse chain** folded in:

```
panel prepare-chrx → prepare --sex auto(→M) → run --chromosomes X
  → import --chromosomes X → collapse-duplicate-variants → merge
  → align-tier3-consensus → refresh-index
```

Anchors **measured and locked** on the first authoritative run (**run_0002**;
drift on a re-run against the same corpus + run_0002 build is a regression signal;
re-derive read-only against `data/genome.duckdb`):

- **chrX imputed variants > 0** (the original failure was exactly 0): **92,832**
  total kept (non-PAR **90,999** + PAR **1,833**). *Tolerance-banded — Beagle is
  multi-threaded / not bit-reproducible; run_0003 saw non-PAR ~91,085.* Re-derive:
  `genotype_calls` source `'beagle_imputed'` (active) ⋈ `variants_master`, split by
  the PAR predicate (`pos_grch38 BETWEEN 10001 AND 2781479 OR BETWEEN 155701383 AND
  156030895`).
- **non-PAR dosage-confidence (the live quality signal — `INFO/DR2` is dead, see
  below):** **87,578** ≥0.99 + **3,421** in [0.9,0.99) = 90,999 (rows carrying
  `quality_flags` `nonpar_dosage_conf`; the typed-anchor subset is 84,657 at
  `imputation_r2`=1.0). *Tolerance-banded.*
- **chrX duplicates collapsed > 0** — 5b's `collapse-duplicate-variants` surfaced
  the chip+imputed duplicate classes (REF/ALT swap, strand-flip) and the
  chip-no-call-meets-imputed surfacing (`finding-028`) on chrX for the **first
  time** here (it had run when chrX was ~0). The result is folded into the
  consensus deltas below.
- **`variants_master` / `consensus_total` / `imputed_only` rose** (exact/
  deterministic): `consensus_total` = `variants_master` total **3,160,364**;
  `imputed_only` **2,218,539**; `single_source` **821,285**; `both_concordant`
  **120,513**; `disagreement_resolved` **0**; `unresolvable` **27**. The chrX
  `imputed_only` contribution is **72,237**.
- **Index anchors — DEFERRED to PR C.** `row_count`, `gnomad_matches`,
  `clinvar_matches`, `gwas_matches`, `pharmgkb_matches`, `is_rare`, `is_ultrarare`
  are **not** locked here: the live gate DB is a `user_only` gnomAD build
  (`finding-035`) and gnomAD was loaded before the chrX import, so the new chrX
  positions mostly lack gnomAD annotations. They re-lock at the post-chrX gnomAD
  reload + `refresh-index` (plan Items 2/4 / PR C); CLAUDE.md obs #4 is unchanged
  until then.
- **Negative control (must stay unchanged — exact):** autosomal `both_concordant`
  **115,509** / `single_source` **793,917** / `imputed_only` **2,146,302** /
  `unresolvable` **26**; shared-call concordance **0.9997760079641613**
  (= 120,513/120,540, **UNCHANGED by chrX** — `genotype_mismatch`=0, 27
  `strand_ambiguous`). The pre-chrX `consensus_total` baseline is 3,088,916
  (CLAUDE.md obs #3 / obs #6); the post-chrX 3,160,364 above is the chrX-inclusive
  boundary (the two are distinct boundaries, both correct).
- **Quality (M1 falsifiability), reframed per `## Correction` / finding-031:** on
  single-sample male non-PAR the Beagle `INFO/DR2` is **structurally 0** for every
  marker (a dead metric, not lost information), so quality is **not** gated on a
  non-PAR-vs-PAR DR² comparison. It is established by the dosage-confidence split
  (above) and the 5-fold LOO: concordance **0.985550** (6957/7059 @ dconf 0.9;
  n_anchors 20,472; not-in-panel 5,276; run_0002 `REPORT.json`), comfortably above
  the finding-031 ≥95% bar; run-to-run band ≈0.985–0.986. `male_nonpar_het_anomaly`
  = **1** (one residual chip miscall; ≈0 by construction under R1) — far below the
  "more than a handful" M3-physical re-trigger.
- **Full-chromosome boundary**: `panel prepare-chrx` asserts the region subsets are
  haploid-correct (PAR haploid-free, non-PAR retains male haplotypes) before any
  Beagle run; no residual full-run boundary failure was observed.

## Side-effect surfaced: the canonicalize variant_id_seq off-by-one

The first real chrX `import` is the first default-`nextval` `variants_master`
insert *after* a canonicalize (the autosomal import ran before it; 5b-collapse /
merge / refresh-index don't `nextval`-insert), and it hit a duplicate-PK at
exactly `MAX(variant_id)`. Root cause: `canonicalize`'s
`_resync_variant_id_sequence` read `duckdb_sequences().last_value` to size its
drain, but DuckDB 1.5.x reports `last_value` as the *last returned* value on a
connection that called `nextval` in-session and the *next to return* on a fresh
connection. canonicalize allocates survivor ids explicitly and never calls
`nextval`, so it always runs on a fresh-position connection — the resync read the
"next" value as "consumed", under-drained by one, and stranded the sequence at
exactly `MAX(variant_id)`. The same-connection regression test could not catch
it (its seed `nextval`s flip `last_value` to the last-returned meaning). Fixed in
this PR by peeking one `nextval` and draining the remaining gap (no reliance on
the catalog view), with a fresh-connection regression test.

### Repairing an already-stranded DB — and why the code fix persists but a
### standalone script doesn't

The code fix only prevents *future* canonicalize runs from stranding the
sequence; a DB canonicalized under the old code is already stranded and needs a
one-time repair. The non-obvious part: **DuckDB 1.5.x does not persist a
pure-`nextval` advance across connection close — not even with an explicit
`CHECKPOINT`.** The sequence counter is flushed to disk only alongside a *data*
modification. So a naive repair script that just drains `nextval` advances the
counter in memory and then silently loses it on `close()`, leaving the next
reopen stranded at the same id.

Two consequences:

- The in-pipeline fix is safe: `_resync_variant_id_sequence` runs inside
  canonicalize's TX2, which is full of `DELETE`/`UPDATE` writes and ends in
  `commit_and_checkpoint`, so the sequence advance is anchored and persists.
- A standalone repair must anchor the advance with a real write. The verified
  recipe: drain `nextval` past `MAX(variant_id)`, then `INSERT` one throwaway
  `variants_master` row (which consumes a `nextval` at a now-safe id and dirties
  the table) and `DELETE` it, then `CHECKPOINT`. Net zero rows; the sequence
  position now survives a reopen. Verify on a *fresh* connection that
  `nextval('variant_id_seq') > MAX(variant_id)`.

## M3-physical built (PR 5a)

The Task 0 probe passed, so M3-physical was built and the M1 *code* deleted
(the M1 narrative above is kept as the audit trail). What shipped:

- **Panel split** (`genome imputation panel prepare-chrx`,
  `imputation/chrx_panel.py`): ensures the panel `.tbi`, then `bcftools view -r`
  emits three **native** subsets `chrX.{par1,nonpar,par2}.vcf.gz` beside the
  panel; prep-time assertions pin PAR-haploid-free + non-PAR-retains-males. The
  region boundaries derive from `par_regions` (the non-PAR region uses an
  open-ended upper range so it reaches the contig end regardless of assembly
  length). `ReferencePanel.diploidized_chrx_panel` → `chrx_{par1,nonpar,par2}_panel`.
- **Region-aware target export** (`imputation/vcf_export.py`): chrX rows bucket
  into the three regions in Python (no SQL predicate); a male profile renders
  non-PAR **haploid** (`_haploid_genotype_for_dosage`: 0→`0`, 2→`1`, impossible
  het 1→`.`), PAR + female/ambiguous stay diploid. Region targets land under
  `archive/.../upload/chrX_regions/`; the manifest records `chrx_regions` counts
  + the `chrx_ploidy` decision.
- **Three-region run + concat** (`imputation/beagle_runner._impute_chrx_regions`):
  one Beagle invocation per region against its native subset, the non-PAR output
  re-diploidized (R1, `rediploidize_vcf` — un-gated, idempotent), then `bcftools
  index -t` ×3 + `bcftools concat -a` → one `result/chrX.vcf.gz`. `concat -a`
  re-sorts across files (the non-PAR sliver < PAR1). Per-region empty guard
  (target present but 0 imputed records → fail), region-level resumability, and
  per-step structlog progress. Autosomal path byte-identical; chrX stays one
  accounting entry.
- **R1 storage unchanged**: `consensus_chrx_dosage_v`, `apply_chrx_het_guard`,
  `chrx_qc.py`, and the importer (`ingest.py`) are untouched — the runner's
  re-diploidization makes the male non-PAR output diploid hom before import.
- **Tooling**: `bcftools` is now a hard prerequisite for the chrX path
  (README + runbook updated).

## Follow-up

- **First-authoritative-run M3 anchors captured (run_0002) and locked** in the
  Verification section above and CLAUDE.md obs #3 (these replace the M1 failure
  numbers as the regression signal): non-PAR usable yield **90,999** (≫ M1's net
  ~1; tolerance-banded); chrX total kept **92,832**; quality via dosage-confidence
  + LOO (concordance **0.985550**) — **not** DR², which is structurally 0 for
  single-sample male non-PAR (see `## Correction` / finding-031);
  `male_nonpar_het_anomaly` = **1**; `consensus_total` **3,160,364**; negative
  controls (autosomal anchors, PAR, shared-call concordance **0.9997760079641613**)
  unchanged. Re-locked **index** match counts are **DEFERRED to PR C** (gnomAD-
  gated). Done in PR A: CLAUDE.md obs #3 updated and ROADMAP PR 5 / 5a flipped to
  `[x]`.
- Persist `--sex` to `sample_qc.sex_expected` (the sex-edge remedy) when an
  all-ambiguous profile actually needs it — not required for this user.
- chrY stays skipped (the panel has no Y); `pos_grch37` recoalesce
  (`finding-005` #9) and `register-existing-result` (PR 11) remain out of scope.

## Correction (PR 5a QC layer — see finding-031)

The verdict above that "whole-panel diploidization destroys non-PAR information
content, yielding mean DR² ≈ 0" **conflated a structurally dead metric with
information loss**, and is corrected by
[`finding-031`](finding-031-chrx-nonpar-dosage-confidence-qc.md):

- `INFO/DR2` is a **cross-sample** dosage-r² estimator. For a **single** male
  sample in the **hemizygous non-PAR** region there is no within-sample
  heterozygosity and no across-sample dosage variance, so DR² is `0.00` for
  **every** non-PAR marker — **imputed and typed alike**, and **regardless of M1
  vs M3, target ploidy, or panel** (three single-sample probes confirmed this).
  M1's first-run "31 out at DR²>0.3" and "mean DR² ≈ 0.0000" were reading a
  *dead* metric, not measuring lost information.
- The Task-0 GO probe's "2.3% > DR²0.3" for native-haploid non-PAR came from a
  **12-sample leave-out** study — the multi-sample setup supplies exactly the
  cross-sample variance the estimator needs, which single-sample production does
  not have. So that probe could not, and did not, establish that M3 beats M1 on
  single-sample *accuracy*; it established that the mechanic *runs* and that DR²
  is recoverable *when a cohort is present*.
- The M1-vs-M3 **accuracy** question is therefore **not settled by DR²** and is
  re-evaluable only via dosage-confidence (`max(DS, 1−DS)`) and the 5-fold LOO
  harness (finding-031). **M3-physical is kept** — built, sound, PAR DR² works,
  non-PAR calls are real and validate under LOO — and the import gate now uses
  the live dosage signal instead of the dead DR² for male non-PAR.
