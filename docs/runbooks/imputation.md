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
