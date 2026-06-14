# chrX imputation via M1 panel diploidization

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

## Verification (first-authoritative-run anchors — capture at the gate)

The gated chrX reload is VSC-User's independent verification, run with the
**5b collapse chain** folded in:

```
panel prepare-chrx → prepare --sex auto(→M) → run --chromosomes X
  → import --chromosomes X → collapse-duplicate-variants → merge
  → align-tier3-consensus → refresh-index
```

Anchors to **measure and then lock** on the first authoritative run (drift on a
later re-run against the same corpus is then a regression signal):

- **chrX imputed variants > 0** — the original failure was exactly 0. ~25,751
  non-PAR targets seed the input.
- **chrX duplicates collapsed > 0** — 5b's `collapse-duplicate-variants` ran when
  chrX had ~0 imputed variants, so the chip+imputed duplicate classes (REF/ALT
  swap, strand-flip) and the chip-no-call-meets-imputed surfacing (`finding-028`)
  collapse on chrX for the **first time** here. Zero ⇒ investigate.
- **`variants_master` / `consensus_total` / `imputed_only`** rise from baseline by
  the chrX contribution (order ~10⁵).
- **Index anchors re-locked**: `row_count`, `gnomad_matches`, `clinvar_matches`,
  `gwas_matches`, `pharmgkb_matches`, `is_rare`, `is_ultrarare` to their new
  post-chrX values. Confirm the current post-5b baselines (CLAUDE.md obs #4)
  before re-locking so chrX rows are not later flagged as drift.
- **Negative control (must stay unchanged)**: the autosomal/consensus anchors —
  `consensus_total` and shared-call concordance `~0.999776`. **Confirm the exact
  baseline against the repo before trusting** (the plan and CLAUDE.md obs #3
  differ on the `consensus_total` figure; reconcile the live value at the gate).
- **M1 falsifiability**: mean **DR² for non-PAR vs PAR vs autosomes**, and the
  **`male_nonpar_het_anomaly`** count from the view (expect small/near-zero). A
  non-PAR DR² materially below PAR/autosomal, or a het-anomaly count more than a
  handful, **triggers M3-physical** (above).
- **Full-chromosome boundary**: `panel prepare-chrx` asserts zero haploid GTs
  across all of chrX before any Beagle run; a residual failure on the full
  diploidized run would implicate Beagle's boundary handling and gets an entry
  here.

## Follow-up

- Capture the anchors above at the first authoritative run and lock them here.
- If the M3-physical trigger fires, build it (region-split subsets + `bcftools
  concat`) per the option-space record above.
- Persist `--sex` to `sample_qc.sex_expected` (the sex-edge remedy) when an
  all-ambiguous profile actually needs it — not required for this user.
- chrY stays skipped (the panel has no Y); `pos_grch37` recoalesce
  (`finding-005` #9) and `register-existing-result` (PR 11) remain out of scope.
