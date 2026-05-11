# Imputation Roundtrip Runbook

Phase 4 of the project. This runbook documents the manual workflow for running
the merged genotype set through the TopMed Imputation Server and ingesting the
imputed result. It is intentionally manual: TopMed does not provide a
programmatic upload API for free-tier users; the upload step happens through
their web UI.

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
- `external_calls_enabled = true` in user preferences.
- You understand TopMed's privacy policy and data retention practices.
- You have read their FAQ on data deletion.

After the roundtrip is complete and the imputed data is ingested locally, you
may delete the uploaded job from TopMed's web UI. The local result is
sufficient; TopMed's copy can be removed.

## Workflow overview

1. Export merged calls to a multi-chromosome VCF (`genome imputation prepare`).
2. Upload the VCF to TopMed's web UI manually.
3. Wait for processing (typically a few hours to overnight).
4. Download the result archive once TopMed emails the link.
5. Decrypt the per-chromosome VCFs using the password TopMed provides.
6. Ingest the imputed VCFs (`genome imputation import`).
7. Re-run merge to refresh consensus genotypes across all sources
   (`genome merge`).

## Detailed steps

### Step 1: Export for upload

TODO (Phase 4 implementation): Document the `genome imputation prepare`
command, where it writes the output VCF, and the validation steps
(per-chromosome split, sort order, build assertion).

### Step 2: TopMed upload

TODO (Phase 4 implementation): Walk through the TopMed web UI form fields:
- Reference panel: TopMed r3 (current as of writing — update if newer panel
  becomes available)
- Array build: GRCh38 (since the project's merged output is GRCh38-native)
- rsq filter / phasing / population: document recommended values
- AES-256 encryption password: TopMed sets this; record it for the decrypt
  step later

### Step 3: Monitor

TODO: Document the status URL, expected processing time, and the audit-log
entries the local `genome imputation status` command produces when polling.

### Step 4: Download

TODO: Document the archive format, where to place it, what files are inside
(per-chromosome zip archives, info files).

### Step 5: Decrypt

TODO: Document the decryption command or workflow (TopMed uses 7zip-compatible
AES; password from step 2).

### Step 6: Ingest

TODO (Phase 4 implementation): Document the `genome imputation import` command
and what `genome status` should show afterward (a third `genotype_calls`
source, ~30M new rows in `variants_master`, an `imputation_runs` row).

### Step 7: Re-run merge

TODO: Re-run `genome merge` to refresh consensus across all three sources.
Expected changes in the merge summary: significant growth in
`consensus_methods.imputed_only` for variants present only in the imputed set.

## Troubleshooting

TODO: Common issues to document as the user encounters them.
