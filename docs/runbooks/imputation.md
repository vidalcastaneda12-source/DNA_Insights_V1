# Imputation Roundtrip Runbook

Phase 4 of the project. This runbook documents the manual workflow for running
the merged genotype set through the TopMed Imputation Server and ingesting the
imputed result. It is intentionally part manual: TopMed does not provide a
programmatic upload API for free-tier users; the upload step happens through
their web UI.

The local CLI handles everything except the upload — preparation, status
polling, download, and ingest are all automated. The manual step is bounded:
copy a few URLs in, paste the password from TopMed's confirmation email back
out, decrypt the archive with a standard tool. The runbook below walks through
each step in order.

## Why we impute

The merged 23andMe + Ancestry call set covers ~940K variants. The TopMed r3
reference panel is built from ~140K diverse genomes and imputes to ~300M
variants genome-wide. After this roundtrip, downstream analyses (PGS, fine-
scale ancestry, rare-variant lookups) operate on the full imputed set instead
of just the chip variants.

## Privacy posture

External upload to TopMed transmits your genome data to a third party. This is
a deliberate trade-off; impute only when you are comfortable with that. Before
beginning, confirm:

- `external_calls_enabled = true` in user preferences (see "enabling external
  calls" below).
- You understand TopMed's privacy policy and data retention practices.
- You have read their FAQ on data deletion.

After the roundtrip is complete and the imputed data is ingested locally, you
may delete the uploaded job from TopMed's web UI. The local result is
sufficient; TopMed's copy can be removed.

## Workflow overview

| Step | Command | Manual? |
|------|---------|---------|
| 0 | `genome config set external_calls_enabled true` | local |
| 1 | `genome imputation prepare` | local |
| 2 | upload to TopMed via web UI | **manual** |
| 3 | `genome imputation status <id> --status-url <url>` | local (polls) |
| 4 | `genome imputation download <id> --download-url <url> --password <pw>` | local |
| 5 | decrypt archive with TopMed's password | **manual** |
| 6 | `genome imputation import <id>` | local |
| 7 | `genome merge` | local |

Steps 1, 3, 4, 6, and 7 record audit rows in `app.db.audit_log` so the entire
roundtrip is reviewable after the fact.

## Enabling external calls

Phase 4 is the first phase in the project that makes any network call. The
master switch lives in `user_preferences.external_calls_enabled` and defaults
to `false`. Enable it once, then leave it on for the duration of imputation:

```
genome config set external_calls_enabled true
```

The change writes one `config_change` row to `audit_log`. You can disable
again after the roundtrip is complete:

```
genome config set external_calls_enabled false
```

A `genome imputation` command attempted while the switch is `false` raises
with an actionable error message pointing back at this step.

## Detailed steps

### Step 1: Prepare the upload

```
genome imputation prepare --sample-id <name>
```

What this does:

- Reads `consensus_genotypes` joined to `variants_master`.
- Filters to SNVs (TopMed cannot impute INDELs — only the reference panel's
  pre-cataloged variants get imputed, and 23andMe's `I`/`D` indel encoding
  doesn't carry the sequence anyway).
- Filters out positions where `ref_allele == alt_allele`. Phase 2's
  alphabetical-ordering normalize sets both fields to the same base for
  positions where every observation is homozygous (we don't yet have a
  reference panel to identify the true canonical allele). TopMed cannot
  impute against `ref=A alt=A` rows. **The downstream impact is that
  homozygous-only positions are dropped from the upload** — typically the
  majority of chip variants, because most positions on either chip are
  hom-ref in any given individual. Imputation still works against the
  remaining polymorphic positions, and once Phase 5 loads dbSNP, a future
  prepare step can rewrite these with canonical REF/ALT to recover the
  dropped rows.
- Writes one gzipped VCF per chromosome to
  `archive/imputation/run_<id>/upload/`. Files are `chr1.vcf.gz`, `chr2.vcf.gz`,
  …, `chrX.vcf.gz`. (Y and MT are skipped — TopMed r3 does not impute them.)
- Writes a JSON `MANIFEST.json` recording the run parameters.
- Inserts an `imputation_runs` row in `status='pending'` with `pipeline_version`,
  `imputation_server='topmed'`, `reference_panel='topmed_r3'`, and the input
  ingestion run IDs.

The output line includes the new `imputation_id` — write it down; you'll use
it for every subsequent command.

Validate before uploading:

```
ls -la archive/imputation/run_<id>/upload/
zcat archive/imputation/run_<id>/upload/chr1.vcf.gz | head -20
```

You should see VCFv4.2 header lines (`##fileformat=VCFv4.2`,
`##contig=<ID=chr1,...,assembly=GRCh38>`) followed by variant rows with
`chr<N>\tPOS\tRSID\tREF\tALT\t.\tPASS\t.\tGT\t0/0|0/1|1/1`. The genotypes
should be a mix of `0/0` (hom-ref), `0/1` (het), and `1/1` (hom-alt) — a
file with only `0/0`s indicates a problem (likely a stale or empty
consensus table).

If `genome imputation prepare` errors with "no eligible SNV consensus rows",
run `genome merge` first — the consensus table is empty.

#### Compression note

The local prepare uses Python's `gzip` module. TopMed prefers bgzip
(block-gzip) but accepts plain gzip; the server re-compresses internally.
If a future TopMed validator rejects the upload, run `bgzip` over each
file locally and re-upload:

```
for f in archive/imputation/run_<id>/upload/chr*.vcf.gz; do
    gunzip "$f"
    bgzip "${f%.gz}"
done
```

### Step 2: Upload to TopMed (manual)

1. Go to https://imputation.biodatacatalyst.nhlbi.nih.gov and sign in.
2. Click **Run** → **Genotype Imputation (Minimac4)**.
3. Form fields:
   - **Reference panel**: `TOPMed r3`. (Update this if a newer panel is
     released — TopMed r3 is current as of writing.)
   - **Array build**: `GRCh38/hg38`. (Our prepare step produces GRCh38-native
     output.)
   - **rsq filter**: `off`. We do our own filtering downstream — capturing all
     variants gives the analyst the most flexibility.
   - **Phasing**: `Eagle v2.4`. The default and the right choice.
   - **Population**: pick the closest population to your ancestry, or
     `Mixed/Other (skip QC)` if uncertain. The population field is used by
     TopMed for allele-frequency QC of the input panel; mismatched-population
     samples can be flagged and excluded. If your data is multi-population
     ancestry, `Mixed/Other` is the safer choice.
   - **Mode**: `Quality Control & Imputation`. Skipping QC is an option but
     the QC step catches strand-flips and ref-mismatches at low cost; keep
     it on.
   - **AES-256 encryption password**: **Save this.** TopMed encrypts the
     result archive with this password and emails it back to you. You will
     pass it to `genome imputation download`. The TopMed UI does not display
     the password again after submission.
4. Upload the `chr*.vcf.gz` files from `archive/imputation/run_<id>/upload/`.
5. Submit the job. TopMed assigns a job ID and shows the job detail page.

**Save two URLs from the job detail page:**

- The **status URL** (visible in the page's address bar or under "API"):
  `https://imputation.biodatacatalyst.nhlbi.nih.gov/api/v2/jobs/<job_id>`.
- The **download URL** (visible later when the job completes; you'll get an
  email with the link).

### Step 3: Monitor (poll status)

```
genome imputation status <imputation_id> --status-url <status_url>
```

What this does:

- Calls the status URL once (a GET).
- Parses the response: the TopMed (Cloudgene) API returns
  `{"state": 1|2|3|4|5|6, ...}`. States 1/2/3 map to `processing`, 4 to
  `completed`, 5/6 to `failed`.
- Updates `imputation_runs.status` and stamps `submitted_at` (the user has
  obviously submitted, since they have a job URL). If the job is
  `completed`, also stamps `completed_at`.
- Records two `audit_log` rows (intent + result) with `external_endpoint='topmed'`
  and the URL's SHA-256 as `external_payload_hash`.

Idempotence: safe to re-run. Re-running on a completed job produces the same
DB state and a fresh pair of audit rows.

Expected processing time: typically 4-8 hours; longer (up to ~24h) during peak
periods. The TopMed status page shows queue position; this CLI just reflects
what the API reports.

You can poll periodically by hand, or use a cron job / `watch`:

```
watch -n 1800 'genome imputation status <id> --status-url <url>'
```

(The local CLI does *not* run a background polling daemon. Long-running
polling jobs are a job-queue concern that lives in the `jobs` table, slated
for Phase 8 when the API + worker arrive.)

### Step 4: Download

Once `genome imputation status` reports `completed`, TopMed has emailed you a
download link. Run:

```
genome imputation download <imputation_id> --download-url <download_url>
# password is prompted for interactively; you can also pass --password <pw>
```

What this does:

- Streams the encrypted ZIP from TopMed to
  `archive/imputation/run_<id>/result/topmed_result.zip`.
- Computes SHA-256 of the saved file and records it on
  `imputation_runs.output_file_hash_sha256`.
- Writes a small `topmed_result.sha256` bookkeeping file (the hash, no
  password — the password is yours to keep).
- Audit-logs the download as a single intent/result pair.

Idempotence: if the archive is already on disk and its hash matches the
stored one, the download is skipped and the existing path is returned. Safe
to retry after a network hiccup; nothing partial is left behind.

The password is required for **decryption**, not for download. Even though
TopMed sends it in the same email as the download link, the link itself is
unauthenticated — anyone with the link can fetch the archive, but only
someone with the password can decrypt it.

### Step 5: Decrypt the archive (manual)

TopMed encrypts the archive with AES-256. The standard `unzip` tool does not
support AES; use `7z` (from `p7zip` on Linux/macOS):

```
cd archive/imputation/run_<id>/result/
7z x -p<PASSWORD> topmed_result.zip
```

This produces per-chromosome `chr<N>.dose.vcf.gz` files plus per-chromosome
`chr<N>.info.gz` info files (R² and allele frequency tables). The
`chr<N>.dose.vcf.gz` files are what `genome imputation import` consumes.

Tip: do not stash the password in a file in the result directory. Keep it in
your password manager. If the password is lost, TopMed cannot recover it —
the job's encryption key is destroyed when TopMed deletes the archive (30
days after completion by default).

### Step 6: Ingest

```
genome imputation import <imputation_id>
```

What this does:

- Streams each `chr<N>.dose.vcf.gz` through cyvcf2.
- Filters to biallelic SNVs (consistent with prepare).
- For each variant: extracts INFO/R² (TopMed's imputation quality metric),
  derives `allele_1`/`allele_2` from the GT field, classifies as
  `is_no_call=true` when GT is `./.`.
- Writes to `variants_master` (inserting new rows for any imputed positions
  not yet present, flipping `has_imputed_call=TRUE` on existing rows) and
  `genotype_calls` (source `topmed_imputed`, `is_imputed=TRUE`,
  `imputation_panel='topmed_r3'`, with the per-variant R²).
- Computes a `sample_qc` row for the imputed result: call rate (~100%), het
  rate, sex inference from imputed X/Y, mean R², low-R² count.
- Updates `imputation_runs` with `variants_output`, `mean_r2`,
  `variants_above_r2_0_3`, `variants_above_r2_0_8`.

Idempotence: re-running supersedes prior imputed calls at the same positions
rather than duplicating — the same supersession-over-update pattern as the
raw ingest writer.

Expected runtime: ~30 minutes for the full ~30M-variant TopMed output on a
laptop with a recent SSD. The pipeline streams per chromosome and batches
50K rows per Arrow Table for the DuckDB bulk load, so memory stays bounded
at a few hundred MB.

After import, validate:

```
genome status
```

You should see `topmed_imputed` reflected in the genotype_calls source mix,
and the master row count should grow by tens of millions.

### Step 7: Re-run merge

```
genome merge
```

The consensus needs to be refreshed across all three sources now that
`topmed_imputed` is present. Expected changes:

- Significant growth in `consensus_methods.imputed_only` for variants
  present only in the imputed set (the bulk of the new rows).
- Existing variants that both chip platforms called gain `topmed_imputed`
  as a third contributing call. The `consensus_v1` rule resolves these
  cases unchanged from before — imputation adds confidence but does not
  override two-source concordance.

After this step, the entire downstream pipeline (Phases 5+) operates on the
unified ~30M-variant set.

## Troubleshooting

### TopMed rejects the upload as "invalid VCF"

Try the bgzip step under "compression note" above. TopMed's validator is
strict about block boundaries when the file is large; plain gzip works for
small files but can trip the validator on full genome exports.

### TopMed fails the job with "ref-mismatch on chr<N>"

The reference alleles in the upload don't match TopMed's reference. Most
common cause: the consensus table contains rows with `ref` and `alt` derived
from alphabetical ordering rather than from a true reference panel — Phase
2's normalize step picks the alphabetically-earlier allele as `ref`. This
is correct for our internal merge logic but can clash with TopMed when both
chips happened to genotype on the minus strand.

Fix: re-run `genome merge` to make sure the consensus is current. If the
problem persists, the issue is a specific row in `variants_master`; check
`call_comparison_v` for the affected position.

### `genome imputation download` fails with HTTP 404

The TopMed result archive expires (30 days from completion by default). If
the link is dead, you'll need to re-submit the job. Generate a fresh upload
with `genome imputation prepare --force-new`.

### Re-importing after a partial run produced wrong R² stats

A partial import will be rolled back automatically — the entire
`genome imputation import` happens inside one DuckDB transaction, so a
failure leaves the DB in its prior state. If `genome imputation import`
seemed to complete but `imputation_runs.mean_r2` is `NULL`, something went
wrong silently — file an issue with the structlog output. Re-running is
safe; the second import supersedes the first.

### "External calls are disabled" error

You forgot to flip the master switch. See "Enabling external calls" above.

### Audit log review

The full audit trail for a roundtrip is in `app.db.audit_log`. Convenient view:

```
SELECT timestamp, action_type, resource_id, external_endpoint,
       json_extract(operation_details, '$.phase') AS phase,
       json_extract(operation_details, '$.status') AS status
FROM audit_log
WHERE resource_type IN ('imputation_run', 'user_preference')
ORDER BY timestamp DESC;
```

Every TopMed interaction produces two rows (intent + result). Local
preference changes produce one. The `external_payload_hash` column lets you
verify that the same URL was hit twice without leaking the URL itself —
useful for confirming a script's behavior without revealing what was sent.
