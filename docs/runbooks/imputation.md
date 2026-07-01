# Imputation Roundtrip Runbook

Phase 4 imputes the merged genotype set against the 1000 Genomes Phase 3
reference panel using Beagle 5.5. The workflow is fully local — no genome
data leaves the machine. The one external call is the one-time reference
panel download (Step 0), which is audited.

## Why we impute

The merged 23andMe + Ancestry call set covers ~940K variants. Imputation
expands this to several million variants on the 1000 Genomes Phase 3 panel.
After this roundtrip, downstream analyses (PGS, fine-scale ancestry,
rare-variant lookups) operate on the imputed set instead of just the chip
variants.

## Privacy posture

Local imputation does not transmit your genome to any third party. Beagle
runs as a local Java process against panel files on disk; nothing about
your sample is sent anywhere.

The reference panel download (Step 0) is the only external call in this
workflow. It hits the Browning Lab's public hosting and downloads panel
files — generic reference data that is the same for every user.
`user_preferences.external_calls_enabled` must be `true` for the panel
download to run; it can be set back to `false` afterward, since no
subsequent step makes external calls.

## Prerequisites

- Java 8 or later. `java -version` should print a working version.
- ~50 GB free disk space for the reference panel + genetic map + Beagle JAR.
- The merged consensus set must be populated. If you haven't already, run
  `genome merge` after ingesting your 23andMe and Ancestry files.

## Workflow overview

| Step | Command                              | Notes                  |
|------|--------------------------------------|------------------------|
| 0    | `genome imputation panel install`    | local, one-time        |
| 1    | `genome imputation prepare`          | local                  |
| 2    | `genome imputation run <id>`         | local (1–4 hours)      |
| 3    | `genome imputation import <id>`      | local                  |
| 4    | `genome merge`                       | local                  |

Every step records audit rows in `app.db.audit_log` so the entire roundtrip
is reviewable after the fact.

## Step 0: install the reference panel (one-time)

```
genome imputation panel install
```

What this does:

- Downloads the per-chromosome 1000 Genomes Phase 3 reference panel
  (`bref3` format), the PLINK genetic map, and the Beagle 5.5 JAR.
- Stores them under `~/.cache/genome/imputation/` (configurable via
  `settings.imputation_panel_root`). The cache directory lives outside
  `data/` deliberately so it survives a database rebuild.
- Resumable: any component already on disk is left alone. Pass `--force`
  to re-download everything, or `--chromosomes 1,22,X` to grab a subset.
- Rewrites each extracted PLINK `.map` so column 1 carries the `chr`
  prefix. The Browning Lab archive ships bare numeric labels (`22`,
  `23`), but Beagle 5.5 does exact-string chromosome matching against
  its reference panel and refuses to run with mismatched labels. The
  rewrite is idempotent — files whose column 1 already starts with
  `chr` are left byte-identical.

Approximate size on disk: 30–50 GB total. The download requires
`user_preferences.external_calls_enabled = true`:

```
genome config set external_calls_enabled true
genome imputation panel install
genome config set external_calls_enabled false   # if you want to lock external calls back off
```

Verify with:

```
genome imputation panel status
```

Expected output: `panel_root: <path>` followed by `all components present`.
If any artifact is missing, the command lists what to re-install.

## Step 1: prepare

```
genome imputation prepare --sample-id <name>
```

What this does:

- Reads `consensus_genotypes` joined to `variants_master`.
- Filters to biallelic SNVs whose REF and ALT are single bases and
  different — INDELs are not in the panel, and positions where Phase 2's
  alphabetical normalize set `ref == alt` cannot be imputed against.
  The downstream impact is that homozygous-only positions are dropped
  from the export.
- Writes one gzipped VCF per chromosome to
  `archive/imputation/run_<id>/upload/`. Files are `chr1.vcf.gz`,
  `chr2.vcf.gz`, …, `chrX.vcf.gz`, `chrY.vcf.gz`. (Y is new in the Beagle
  era; the TopMed r3 panel did not impute Y, so the prepare step
  previously skipped it.)
- Writes a JSON `MANIFEST.json` recording the run parameters.
- Inserts an `imputation_runs` row in `status='pending'` with
  `imputation_server='beagle'`, `reference_panel='1000g_phase3_grch38'`,
  and the input ingestion run IDs.

The output line includes the new `imputation_id` — note it down; you'll use
it for every subsequent command.

Validate before running Beagle:

```
ls -la archive/imputation/run_<id>/upload/
zcat archive/imputation/run_<id>/upload/chr1.vcf.gz | head -20
```

You should see VCFv4.2 header lines (`##fileformat=VCFv4.2`,
`##contig=<ID=chr1,...,assembly=GRCh38>`) followed by variant rows in
`chr<N>\tPOS\tRSID\tREF\tALT\t.\tPASS\t.\tGT\t<0/0|0/1|1/1>` form. The
genotypes should be a mix of `0/0`, `0/1`, and `1/1` — a file with only
`0/0`s indicates a problem (likely a stale or empty consensus table).

If `genome imputation prepare` errors with "no eligible SNV consensus rows",
run `genome merge` first — the consensus table is empty.

## Step 2: run

```
genome imputation run <imputation_id>
```

What this does:

- Spawns one `java -jar beagle.jar` subprocess per chromosome, reading
  the matching `upload/chr<N>.vcf.gz`, the panel `bref3` file, and the
  genetic map.
- Writes one `result/chr<N>.vcf.gz` per chromosome.
- Transitions `imputation_runs.status` to `processing` when the first
  chromosome starts, then to `completed` if every attempted chromosome
  succeeds. Mixed success leaves it at `processing` so partial retries
  preserve the successful chromosomes.

Flags:

- `--chromosomes 1,22,X` — limit the run to specific chromosomes. Useful
  for retrying failures or for a quick chr22-only smoke test before
  committing to a full run.
- `--threads N` — number of threads per chromosome (Beagle's `nthreads=`).
  Defaults to `max(1, cpu_count() - 1)`.
- `--memory-gb N` — Java heap size in GB (Beagle's `-Xmx`). Default 8 GB
  handles most chromosomes; chr1 and chr2 may need 12–16 GB on dense
  panels.
- `--ne N` — effective population size (Beagle's `ne=`). Default
  1,000,000 matches Beagle's documented default for outbred humans.
- `--force` — re-run every chromosome even if its output VCF already
  exists. Resumability is the default; finished chromosomes are skipped.

Expected wall-clock: 1–4 hours for a full genome on a modern laptop,
depending on CPU count and memory. A chr22-only run is ~5–10 minutes and
is a useful first invocation to verify the pipeline end-to-end.

OOM handling: if Beagle aborts with `OutOfMemoryError`, lower
`--memory-gb` (some chromosomes need less than the default and the JVM
sometimes over-allocates) or run a chromosome subset so each process has
the heap to itself. The runner reports per-chromosome wall-clock at
completion; chromosomes that failed can be re-driven with
`--chromosomes <failed-list>`.

## Step 3: import

```
genome imputation import <imputation_id>
```

What this does:

- Streams each `result/chr<N>.vcf.gz` through cyvcf2.
- Filters to biallelic SNVs (consistent with prepare).
- For each variant: extracts the imputation R² from `INFO/DR2`
  (Beagle 5.5's native dosage-R² field; the importer also accepts
  `INFO/R2` and `INFO/Rsq` as fallbacks), derives `allele_1` / `allele_2`
  from the GT field, classifies as `is_no_call=true` when GT is `./.`.
- Writes to `variants_master` (inserting new rows for any imputed
  positions not yet present, flipping `has_imputed_call=TRUE` on existing
  rows) and `genotype_calls` (source `beagle_imputed`,
  `is_imputed=TRUE`, `imputation_panel='1000g_phase3_grch38'`, with the
  per-variant R²).
- Computes a `sample_qc` row for the imputed result: call rate (~100%),
  het rate, sex inference from imputed X / Y, mean R², low-R² count.
- Updates `imputation_runs` with `variants_output`, `mean_r2`,
  `variants_above_r2_0_3`, `variants_above_r2_0_8`, `r2_threshold`.

Operational flags:

- `--r2-threshold 0.3` (default) — variants whose R² is below the
  threshold are skipped and never written. Set to `0.0` to keep every
  variant; the per-variant R² is still recorded so downstream filters
  can be re-tightened later.
- `--chromosomes 1,X` — limit the import to specific chromosomes.
- `--dry-run` — parse the VCFs and report per-chromosome counts and an
  estimated wall-clock without writing anything.
- `--force-reimport` — required to re-import a run whose
  `variants_output` is already populated. The prior calls are
  deactivated via the existing supersession pattern.

Idempotence: re-running supersedes prior imputed calls at the same
positions rather than duplicating.

Re-importing over an already-merged corpus is FK-safe: the supersession flips
`genotype_calls.is_active`, which DuckDB runs as a delete+reinsert that would
otherwise trip the `discrepancies` -> `genotype_calls(call_id)` foreign key, so
the import first clears the referencing `discrepancies` rows in a committed
pre-step (rebuilt by the following `genome merge`). See `finding-032`.

Expected runtime: a few minutes for a few-million-variant Beagle output on
a laptop with a recent SSD. The pipeline streams per chromosome and
batches 50K rows per Arrow Table for the DuckDB bulk load, so memory
stays bounded.

After import:

```
genome status
```

`beagle_imputed` should appear in the `genotype_calls` source mix and the
master row count should grow by the imputed total.

## Step 4: merge

```
genome merge
```

The consensus needs to be refreshed across all three sources now that
`beagle_imputed` is present. Expected changes:

- Significant growth in `consensus_methods.imputed_only` for variants
  present only in the imputed set (the bulk of the new rows).
- Existing variants that both chip platforms called gain `beagle_imputed`
  as a third contributing call. The `consensus_v1` rule resolves these
  cases unchanged from before — imputation adds confidence but does not
  override two-source concordance.

After this step, the entire downstream pipeline (Phases 5+) operates on
the unified imputed set.

## chrX imputation (M3-physical region split)

chrX needs one extra step. The 1000 Genomes panel stores male non-PAR chrX
**haploid**, and Beagle 5.5's reference loader requires uniform ploidy per
sample across the whole chromosome — it aborts at the first non-PAR position
(`HG00096 … chrX:2785078 is haploid`). PR 5a resolves this with the
**M3-physical** mechanic (`finding-029`): physically split the panel into three
region subsets (PAR1 / non-PAR / PAR2), impute each natively with the
biologically-correct ploidy (male non-PAR stays haploid), then `bcftools concat`
the per-region outputs into one `result/chrX.vcf.gz`. (The earlier M1
whole-panel diploidization failed its falsifiability gate — fake-homozygosing
half the panel destroyed non-PAR information content, yielding mean DR² ≈ 0; see
`finding-029`.)

### Step 0.5: prepare the chrX panel (one-time)

```
genome imputation panel prepare-chrx
```

What this does:

- Ensures the panel `.tbi` index, then splits the installed `chrX.vcf.gz` into
  three **native** (un-diploidized) subsets via `bcftools view -r` —
  `chrX.par1.vcf.gz`, `chrX.nonpar.vcf.gz`, `chrX.par2.vcf.gz` — beside the panel.
  Needs `bcftools` on PATH (plus `bgzip` / `awk` for the composition checks).
- Asserts each subset's ploidy composition: the PAR subsets are haploid-free, and
  the non-PAR subset retains the panel's male hemizygous haplotypes.
- Idempotent — existing subsets are reused; pass `--force` to rebuild. This is a
  gated operation (it scans the whole chromosome).

**Wall-clock expectation (prep-time only).** On the real 1000 Genomes chrX panel,
`prepare-chrx` takes **~80 minutes** — the non-PAR composition assertion
(`count_haploid_gts`, O(variants × samples)) streams the whole ~3,202-sample
non-PAR subset (≈55 CPU-min pegged at 100% CPU) and emits **no progress output**,
so expect a long silent wait on the first run (it is not a hang). The cost is
**prep-time only and idempotent** (the three subsets cache beside the panel; a
re-run hits `skip_existing`), runs on the **panel** rather than your data, and
**does not affect `genome imputation run`** (the runner's only `count_haploid_gts`
use is the single-sample `rediploidize_vcf` post-assertion, O(variants × 1)). See
[`finding-030`](../findings/finding-030-prepare-chrx-haploid-count-perf.md) for the
short-circuit existence-check fix (recommended there; not yet applied — no code
change in this docs-only PR).

`genome imputation run` with chrX in scope points each region's `ref=` at its
matching native subset and refuses to start (with an actionable message) if the
subsets are missing.

### Sex (`--sex`)

`genome imputation prepare` and `genome imputation run` take `--sex {M,F,auto}`
(default `auto`). `auto` resolves the profile sex from the chip `sample_qc`
rows; `prepare` records it in the manifest as provenance, and a chrX `run`
**requires** a determinate sex (it corrects male non-PAR dosage downstream). If
the chip aggregate is ambiguous, pass `--sex M` or `--sex F`. Nothing is
persisted to the database.

### Corrected dosage + het guard

Under M3 the male non-PAR target is exported **haploid**; Beagle imputes it
against the native non-PAR subset and emits haploid calls, which the runner
re-diploidizes back to homozygous-diploid (R1: `0`→`0|0`, `1`→`1|1`) before the
importer — so `variants_master` / `consensus_genotypes` and everything downstream
stay byte-unchanged (lossless). The `consensus_chrx_dosage_v` view still maps the
stored hom-diploid back to the true hemizygous copy number (`corrected_dosage`:
2→1, 0→0) for a male profile and flags any biologically-impossible male non-PAR
het (`male_nonpar_het_anomaly`). After `genome merge`, the male-non-PAR-het guard
counts those anomalies and records the count on the imputed `sample_qc.qc_notes`
(`[chrx_male_nonpar_het=N]`). Under M3 this should read ≈ 0 by construction — a
haploid call re-diploidizes to a homozygote, never a het — so a non-trivial count
is the signal to revisit (see `finding-029`).

### The full chrX reload sequence

When loading chrX for the first time, fold in the PR-5b duplicate-collapse chain
so the chip+imputed duplicates surface on chrX:

```
genome imputation panel prepare-chrx
genome imputation prepare --sex auto
genome imputation run <id> --chromosomes X
genome imputation import <id> --chromosomes X
genome annotate collapse-duplicate-variants
genome merge
genome annotate align-tier3-consensus
genome annotate refresh-index
```

## Rebuilding from a preserved archive

PRs that touch `docs/schemas/` or `ddl/` require the schema-change
rebuild documented in `CLAUDE.md` ("Schema changes require rebuilding
local databases"): `rm -rf data/`, `uv run genome init`, re-ingest both
sources, `genome merge`. When that rebuild happens after a Phase 4
imputed corpus has already been produced, the on-disk Beagle output at
`archive/imputation/run_<id>/result/chr<N>.vcf.gz` survives (the
archive lives outside `data/` deliberately), but the
`imputation_runs` row does not. The fresh `genome imputation prepare`
that follows the rebuild lands a new row in `status='pending'`.

The correct sequence is **prepare → run → import** — or, when the
*full* result tree survived intact, the JVM-free
**prepare → register-existing-result → import** fast path (see
[Fast path](#fast-path-register-existing-result-full-archive-preserved)
below). Neither is the bare prepare → import shortcut one might expect
when the result VCFs are already on disk: `genome imputation import`
guards on `status='completed'` and aborts with:

    RuntimeError: imputation_id <id> is in status 'pending'; download
    the result first (status must be 'completed' before import)

`genome imputation run <id>` is the step that moves the row from
`pending` → `processing` → `completed` (the local Beagle replacement
for the legacy "download the result" phrasing — see
`docs/findings/finding-006-topmed-not-viable-for-personal-genomics.md`).
The runner is resumable: any `result/chr<N>.vcf.gz` that already
exists and parses cleanly with cyvcf2 is skipped (logged at INFO as
`imputation.beagle.chrom.skip_existing`), and anything missing is
re-imputed for real against the on-disk panel.

### Fast path: `register-existing-result` (full archive preserved)

When the **entire** result tree survived the rebuild — every
`result/chr<N>.vcf.gz` the prepare manifest expects is on disk and
intact — `genome imputation register-existing-result <id>` collapses the
bridge to a single JVM-free command, instead of `run` booting Beagle
just to skip every already-imputed chromosome:

```
genome imputation prepare ...                     # re-creates the pending run row
genome imputation register-existing-result <id>   # validate-and-flip, no Beagle
genome imputation import <id>                      # load the validated VCFs
```

`register` never boots Java/Beagle. It validates the preserved tree
against the prepare `upload/MANIFEST.json` and, only if every expected
chromosome passes, flips `imputation_runs.status` straight to
`completed` (stamping `submitted_at` / `completed_at`, finding-007 Fix 3)
so the `status='completed'` guard on `import` clears. It writes no
`genotype_calls` — loading the VCFs stays `import`'s job.

Validation is **fail-closed and truncation-aware** (finding-008 #2):

- The expected set is the manifest's `variants_per_chrom` keys ∩ the
  reference panel, so a manifest `Y` / `MT` key is dropped — chrY is
  intentionally absent from the panel, and the user's own run carries a
  `Y` key with no `result/chrY.vcf.gz`.
- chrX is validated via the **top-level concat** `result/chrX.vcf.gz`
  (finding-029), never the `result/chrX_regions/` per-region subdir.
- Each expected VCF must exist, be a non-truncated BGZF (a missing
  28-byte EOF marker is a mid-write Beagle failure cyvcf2 would otherwise
  read as a silent zero-variant success), and stream ≥1 record when the
  manifest recorded uploaded variants for it.
- Any missing / truncated / silently-empty VCF, an on-disk result VCF the
  manifest does not list, an absent or unparseable manifest, or a run
  already `completed` / `failed` is refused with a non-zero exit and the
  run's status left **byte-unchanged** (no torn state).

**id ↔ `run_<id>` directory alignment (important).** After `rm -rf
data/`, `_next_imputation_id` restarts at **1**, but the preserved tree
is whatever id it was first produced under (e.g. `run_0002`). A DB row
must therefore exist at the *preserved run's* id before `register` — so
re-run `prepare` until the new `pending` row lands on that id, then
`register <that id>`. A wrong id fails **loud**, never silent: an id with
no row → `imputation_id <id> not found`; an id whose `run_<id>/result/`
tree is absent → an empty-expected / missing-VCF refusal. This is the
same id↔dir constraint `run` and `import` already inherit.

For a **partially** preserved archive, use `run` instead — it re-imputes
only the missing chromosomes; `register` is all-or-nothing and refuses a
partial tree rather than registering it.

The wall-clock cost scales with how much of the archive survived:

- **Full archive preserved** — seconds: `register-existing-result`
  (above) validate-and-flips with no Beagle subprocess at all; `run` also
  works (it parse-checks every chromosome and skips them all). Total
  rebuild cost is dominated by the re-ingest and merge.
- **Partial archive preserved** — minutes per missing chromosome.
  Beagle re-imputes only the missing set against the existing panel.
  A real verification session with only chr22 preserved (residue of an
  earlier smoke test) re-imputed chr1–chr21 + chrX in ~21 minutes.
- **No archive preserved** — ~30 minutes for a full-genome re-run on
  the typical 23andMe v5 + Ancestry v2 corpus (per CLAUDE.md
  "Real-data observations" #3).

In every case the import that follows is a pure DuckDB load — no
external work — so it costs whatever the existing import normally
costs (a few minutes for a few-million-variant Beagle output).

See `docs/findings/finding-008-phase4-rebuild-and-chrx-observations.md`
for the durable real-data write-up.

## Expected log output

cyvcf2 emits `[W::vcf_parse] Contig 'chr<N>' is not defined in the header.`
once per file open during the run, import, and dry-run steps. Beagle's
output VCFs do not declare contigs in their headers; cyvcf2 parses the
files correctly regardless. The warning is informational — about 23
lines per full-genome import — and can be ignored.

## Troubleshooting

### Java not found / Java too old

Beagle 5.5 needs Java 8+. Install one of:

- macOS: `brew install openjdk@17`
- Ubuntu / Debian: `sudo apt install openjdk-17-jre`
- Fedora / RHEL: `sudo dnf install java-17-openjdk`

`java -version` should print a version string ≥ 1.8.

### Beagle OOM

Lower `--memory-gb` (sometimes the JVM over-allocates and a smaller heap
finishes), or run fewer chromosomes per invocation so each subprocess has
the heap to itself.

### "panel missing for chr<N>"

A specific chromosome's panel `bref3` file is missing under
`<panel_root>/panel/`. Re-run:

```
genome imputation panel install
```

The installer is resumable; only the missing files are fetched. Use
`--chromosomes <N>` to limit to the missing chromosome.

### chr Y output empty

The 1000 Genomes Phase 3 `bref3` release has limited Y-chromosome
coverage. If the runner emits a `no panel for chrY` warning and skips Y,
that is expected and not a bug. The merged set will continue to carry
the chip-derived Y calls.

### chrX hemizygous-haploid Beagle failure (resolved — PR 5a)

Historically chrX imputation failed in Beagle 5.5 with:

    java.lang.IllegalArgumentException: Reference sample HG00096 has an
    inconsistent number of alleles. The first genotype is diploid, but
    the genotype at position chrX:2785078 is haploid

The 1000G panel represents male non-PAR chrX as haploid, and Beagle's reference
loader requires uniform ploidy per sample across the whole chromosome. PR 5a
resolves this with the **M3-physical region-split** mechanic — see "chrX imputation
(M3-physical region split)" above and `docs/findings/finding-029-chrx-imputation-m1.md`.
Run `genome imputation panel prepare-chrx` once before any chrX run.

If you see the error above, the chrX region subsets
(`chrX.{par1,nonpar,par2}.vcf.gz`) are missing or stale: run
`genome imputation panel prepare-chrx` (or `--force` to rebuild). The runner now
fails chrX cleanly (a `no_region_panel` event with a "run `genome imputation panel
prepare-chrx` first" hint) rather than crashing mid-write, and refuses to import a
truncated or silently-empty chromosome.

### Audit log review

The full audit trail for the workflow is in `app.db.audit_log`. Convenient
view:

```
SELECT timestamp, action_type, resource_id, external_endpoint,
       json_extract(operation_details, '$.phase') AS phase,
       json_extract(operation_details, '$.status') AS status
FROM audit_log
WHERE resource_type IN ('imputation_run', 'user_preference', 'reference_panel')
ORDER BY timestamp DESC;
```

Under the local workflow, the only external call audited is the one-time
reference-panel download in Step 0. Every other step is local and produces
local-action audit rows only. The `external_payload_hash` column lets you
verify the same URL was hit without leaking the URL itself.
