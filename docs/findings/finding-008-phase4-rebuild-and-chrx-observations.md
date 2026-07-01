---
type: observation
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-05-14
supersedes: []
superseded_by: []
---
# Phase 4 rebuild workflow and chrX hemizygous-haploid Beagle failure

> **Status: closed by PR #74** (chrX resolution via the M3-physical region split).
> See [`finding-029`](finding-029-chrx-imputation-m1.md) for the resolution and the
> locked post-chrX anchors. The historical diagnosis below is retained as-is.

## Context

Real-data verification of the schema view fix in PR #31 required the
standard schema-change rebuild documented in `CLAUDE.md`
(`rm -rf data/` + `genome init` + re-ingest + `genome merge` + new
prepare + import). That exercise surfaced two durable observations
about the Phase 4 imputation pipeline that aren't captured elsewhere
in `docs/findings/` or `docs/runbooks/imputation.md`. Both are worth
pinning so future sessions don't re-discover them: the first is an
operational gap in the documented prepare → import path, and the
second explains the mechanism behind the "chrX imputed variants: 0 for
males" symptom already documented in CLAUDE.md "Real-data observations"
#3.

This finding follows the same multi-observation shape as
`finding-007-beagle-real-data-cleanup.md` — both observations emerged
from the same real-data session against the user's merged 23andMe v5 +
Ancestry v2 corpus, and neither requires code or schema changes in
this PR.

## Observations

### 1 — Rebuilding from a preserved Beagle archive needs an explicit `run` step

The documented Phase 4 flow (`docs/runbooks/imputation.md` Steps 1–3)
is built around a fresh `prepare` followed by a one-time Beagle run.
The schema-change convention in `CLAUDE.md` ("Schema changes require
rebuilding local databases") forces a different path on every PR that
touches `docs/schemas/` or `ddl/`: drop `data/`, `genome init`,
re-ingest both sources, `genome merge`, then a fresh `genome
imputation prepare` to re-create the `imputation_runs` row. That row
lands in `status='pending'` because prepare alone does not run
Beagle. The next step in the documented flow is `genome imputation
import <id>`, which guards on `status='completed'` and refuses to
proceed:

    RuntimeError: imputation_id 1 is in status 'pending'; download the
    result first (status must be 'completed' before import)

The fix is to interpose `genome imputation run <id>` between prepare
and import. The runner is resumable per `finding-007`: a chromosome
whose `result/chr<N>.vcf.gz` already exists on disk and parses cleanly
with cyvcf2 is skipped (`imputation.beagle.chrom.skip_existing`) and
no Beagle subprocess is launched. Anything missing is re-imputed for
real.

The cost of the rebuild therefore scales with how much of the
`archive/imputation/run_<id>/result/` tree survived the rebuild:

- **Full archive preserved** — seconds per chromosome (cyvcf2
  parse-check only); the rebuild's wall-clock cost is dominated by
  the re-ingest and merge, not by Beagle.
- **Partial archive preserved** — minutes; Beagle re-runs the missing
  chromosomes against the on-disk panel. In the verification session
  this finding emerges from, only `chr22.vcf.gz` was present (the
  residue of an earlier chr22-only smoke test referenced in
  `finding-007`). The runner correctly logged the chr22 skip via
  `imputation.beagle.chrom.skip_existing` and re-imputed chr1–chr21 +
  chrX. Total: ~21 minutes of real Beagle work before the chrX
  failure described in observation 2.
- **No archive preserved** — ~30 minutes for a full-genome
  re-imputation against the user's corpus (per the durable runtime in
  CLAUDE.md "Real-data observations" #3).

In all three cases the prepare step's output is byte-identical because
the upstream inputs (the same raw exports, the same merge result) are
deterministic, so the on-disk Beagle output produced against the prior
prepare is still valid input to import — there is no need to
re-impute just because the row's database side was recreated.

### 2 — chrX fails with `IllegalArgumentException` from Beagle's reference loader

Real-data Beagle 5.5 runs against the 1000 Genomes Phase 3 GRCh38
reference panel (EBI's high-coverage phased release) fail on chrX with:

    java.lang.IllegalArgumentException: Reference sample HG00096 has an
    inconsistent number of alleles. The first genotype is diploid, but
    the genotype at position chrX:2785078 is haploid

`HG00096` is a male reference sample in the 1000 Genomes panel.
chrX:2785078 sits 3,599 bp outside the documented PAR1 boundary on
GRCh38 (PAR1 ends at chrX:2,781,479). The 1000G panel correctly
represents non-PAR chrX as haploid for males and diploid for females,
matching the biology of the X chromosome. Beagle 5.5's reference
loader, however, requires uniform ploidy per sample across the entire
chromosome and rejects the mixed-ploidy male representation at the
first position past PAR1.

Operational behavior:

- The chrX Beagle subprocess exits with returncode 1 after writing a
  truncated `result/chrX.vcf.gz` with no BGZF EOF marker.
- The downstream `genome imputation import <id>` reads the truncated
  file via cyvcf2, which emits
  `[W::bgzf_read_block] EOF marker is absent. The input may be
  truncated` and then iterates 0 variants without raising.
- The other chromosomes (1–22) succeed normally, so `imputation_runs`
  records autosomal output. Because chrX failed while the autosomes
  succeeded, the run is a mixed outcome and `imputation_runs.status`
  remains at `processing` per the partial-failure convention
  established in `finding-007` Fix 3.

This is the root cause of the previously-documented symptom in
CLAUDE.md "Real-data observations" #3 ("chrX imputed variants: 0 for
males"). The CLAUDE.md note attributed the zero count to the
prepare-layer drop of `ref==alt` hemizygous rows (`finding-005` #6).
That filter is real and contributes — most male non-PAR chrX positions
never reach the upload VCF — but it is not the whole story. The
upload VCF still contains the PAR variants and any heterozygous chrX
positions present in the user's chip data, and Beagle's reference
loader fails on the panel side before any user variant is imputed.
The end-to-end symptom is the same (zero chrX imputed variants for a
male user), but the mechanism is the Beagle reference-panel failure
above; the prepare-layer filter is a contributing factor, not the
proximate cause.

## Implications

### Operational guidance

`docs/runbooks/imputation.md` now documents the rebuild-from-preserved
-archive workflow as a first-class scenario: invoke
`prepare → run → import` in sequence after a schema-change rebuild,
expect the runner to skip preserved per-chromosome outputs and
re-impute anything missing, and expect wall-clock to scale with the
surviving fraction of the archive. The runbook also documents the
chrX failure under "Troubleshooting" so a user hitting the
`IllegalArgumentException` knows it is expected pending one of the
fixes below.

### chrX fix options (deferred → resolved by PR #74)

Two fixes are known; both are deferred to a later session:

(a) **Pre-process the reference panel** to make male non-PAR X "fake
    diploid" by duplicating each haploid allele into a diploid
    homozygous genotype before passing the panel to Beagle. This is
    the standard workaround applied by other single-sample imputation
    pipelines and only touches the panel side, so the runner and the
    upload-VCF path stay unchanged. The trade-off is a derived panel
    that needs management, revalidation on panel updates, and an
    extra ~5–10 GB of disk for the rewritten chrX file.

(b) **Sex-aware chrX handling** — split chrX into PAR1, PAR2, and the
    non-PAR region and impute each appropriately for the user's
    inferred sex. Correct in principle and avoids modifying the
    reference panel, but requires more pipeline orchestration
    (per-region prepare, per-region Beagle invocation, per-region
    import) than (a) and pulls a sex inference dependency into the
    Beagle path.

A third option worth flagging is **silently skipping chrX entirely**
until (a) or (b) lands, since the current behavior — Beagle writes a
truncated chrX VCF, cyvcf2 reads zero variants, no error surfaces — is
a quiet failure mode rather than an obvious one. Whichever fix is
chosen, the runner should refuse to write a truncated `result/chrX
.vcf.gz` and the import step should refuse to silently treat zero
chrX variants from a non-trivial input as success.

### Future enhancement worth noting

A `genome imputation register-existing-result <id>` command would
bypass the runner entirely when a complete on-disk Beagle output tree
is present, validating each per-chromosome VCF with cyvcf2 and
flipping `imputation_runs.status` directly to `completed` (with
`submitted_at` and `completed_at` stamped to current timestamps).
That would collapse the "full archive preserved" rebuild case to a
single command. Implemented as `RM-7fba363` (PR 11): the runner's
skip-cleanly-parsing-files behavior already handled this case, but
`register-existing-result` collapses it to one JVM-free
validate-and-flip that skips the Beagle boot entirely — truncation-aware
(per #2 above), over the manifest ∩ reference-panel chromosome set
(chrY excluded; chrX via the top-level concat).

## Follow-up

- **Resolved by PR #74** — none of fix options (a), (b), or the temporary
  (c) (silently skip chrX) was taken; the M3-physical region split
  (sex-aware PAR1 / non-PAR / PAR2) superseded the entire (a)/(b)/(c)
  option space. See finding-029 / finding-031 / finding-033. This
  finding's job was to capture the mechanism and the option space.
- The `register-existing-result` command is now tracked as
  `RM-7fba363` (PR 11) and implemented here — the JVM-free
  validate-and-flip described above (truncation-aware, manifest-driven,
  fail-closed, status-only).
- CLAUDE.md "Real-data observations" #3 is intentionally left as-is.
  The symptom it documents is still correct on its own terms; this
  finding now supplies the mechanism behind it.
