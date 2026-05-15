# Reference Annotations Runbook

Phase 5 loads reference annotation data from curated public sources into
the analytical DuckDB. Each source has a per-source loader registered
under `genome.annotate.loaders`; the CLI dispatches by `--source` label.
Loaders are independent — refreshing one does not touch any other.

## Overview

The annotate package has three layers:

* **Scaffold** (`genome.annotate`, sub-phase 5.0). The
  `annotation_source_versions` upsert + read helpers, the audited
  download cache, the per-source loader registry, and the
  `genome annotate refresh | status` CLI commands. No source-specific
  logic lives here.
* **Per-source loaders** (`genome.annotate.loaders`, sub-phase 5.1+).
  One module per source. Each module is responsible for: knowing its
  upstream URL, resolving a stable version label, parsing the
  downloaded artifact, allocating IDs, and bulk-inserting into its
  destination table.
* **Refresh job** (`variant_annotations_index`, sub-phase 5.7). Will
  refresh the materialized rollup table after any source loader runs.
  Not present yet.

Each loader registers itself at module-import time. Importing the
parent `genome.annotate` package triggers the loaders subpackage's
side-effect imports, which populate the registry before the CLI
dispatches.

## Privacy posture

Reference annotations are public data — ClinVar, PharmGKB, CPIC, GWAS
Catalog and friends publish their corpora openly. The loaders fetch
them via the audited
`genome.privacy.external_client.ExternalClient`, so every download is:

* Gated on `user_preferences.external_calls_enabled = true` (the
  master switch is fail-closed by default).
* Logged with one intent row and one outcome row in `app.db.audit_log`
  per download attempt; blocked attempts (when the switch is off) also
  produce intent + blocked rows so the privacy-relevant event is
  durably recorded.
* Tagged with `external_endpoint = 'annotations_<source_db>'` (e.g.
  `annotations_pharmgkb`) so audit-log queries can group every
  download for one source.

No genome data leaves the machine during a refresh. The user variants
are only ever consulted at materialization time (5.7), when the
`variant_annotations_index` rollup is rebuilt locally.

## Prerequisites

* `user_preferences.external_calls_enabled = true` for the duration of
  the refresh. Toggle via `genome config set external_calls_enabled
  true`. The setting can be flipped back to `false` after the refresh
  completes; the per-source local data already on disk remains usable.
* ~1 GB free disk under `~/.cache/genome/annotations/` for the
  PharmGKB corpus (a few MB) plus larger sources that ship in 5.2+.

## Workflow overview

| Command                                              | Purpose                                                |
|------------------------------------------------------|--------------------------------------------------------|
| `genome annotate status`                             | Read-only — what's loaded across every known source.   |
| `genome annotate refresh --source <db>`              | Download + parse + load one source (skip-if-current).  |
| `genome annotate refresh --source <db> --force`      | Re-download + reload regardless of cached state.       |

The CLI surface stays stable across sources — only the `<db>` argument
changes. Every refresh:

1. Resolves the on-disk cache path under
   `~/.cache/genome/annotations/<source_db>/`.
2. Downloads the upstream artifact via the audited HTTP client
   (skip-if-already-cached unless `--force`).
3. Resolves a stable version label (from source metadata when present;
   otherwise retrieval date as `YYYY_MM_DD`).
4. Short-circuits if `annotation_source_versions` already names the
   resolved version (idempotent re-runs).
5. Parses the artifact and bulk-loads into the source's destination
   table inside a transaction.
6. Supersedes prior rows for the same source (`is_active = FALSE`).
7. Records the version row at `is_current = TRUE`.

## Per-source notes

### PharmGKB (sub-phase 5.1a)

**What's loaded.** PharmGKB's Clinical Annotations bundle
(`clinicalAnnotations.zip`, ~1.2 MB) parsed into per-row
(annotation × drug) tuples in `pharmgkb_annotations`. A clinical
annotation that lists `n` drugs produces `n` rows in the table, all
sharing the same `pgkb_accession` (PharmGKB's clinical-annotation ID)
and differing only in `drug_name`.

**Upstream URL.**
`https://api.pharmgkb.org/v1/download/file/data/clinicalAnnotations.zip`.
PharmGKB redirects this canonical `api.pharmgkb.org` URL to its
S3-hosted ZIP; the audited client follows the 303 transparently.
`URL_VERIFIED_DATE` in
`backend/src/genome/annotate/loaders/pharmgkb.py` records when the
URL was last confirmed to work; bump it on any URL change.

**Version label.** Read from the ZIP's `CREATED_YYYY-MM-DD.txt`
marker file. PharmGKB ships exactly one such file per release; the
loader reformats the date as `YYYY_MM_DD` for the
`annotation_source_versions.version` column. If no marker is found
(unexpected for the canonical bundle), the loader falls back to
today's UTC date in the same format. The fallback path is logged
loudly at INFO (`pharmgkb.version.no_metadata_fallback`).

**Runtime + disk.** ~1 GB total download budget under
`~/.cache/genome/annotations/pharmgkb/` (the real archive is ~1.2 MB —
the budget leaves room for future growth). End-to-end refresh on a
laptop is a few seconds: parse + load is bounded by the 5 K row TSV.

**Variant-identifier bucketing.** PharmGKB's "Variant/Haplotypes"
column carries one of three shapes:

* An rsID (`rs951439`) — populates `rsid`.
* A star allele or HLA allele (`CYP2D6*4`, `HLA-B*57:01`) — populates
  `star_allele`.
* A descriptive haplotype text (e.g. `G6PD A- 202A_376G, G6PD B
  (reference)`) — also populates `star_allele`, so the field becomes
  "non-rsID variant identifier".

The detection rule is the regex `^rs\d+$`. Anything else lands in
`star_allele` verbatim. The schema has no dedicated descriptive-text
column; the strings are still queryable via LIKE.

**Multi-drug expansion.** The `Drug(s)` cell is `;`-separated. Single
drug names can contain commas (e.g. `"Ace Inhibitors, Plain"`); the
splitter only splits on `;`, never `,`. The 2025-07-05 release had
919 multi-drug rows and 49 single-drug rows with embedded commas, so
this distinction is load-bearing.

**Coordinates.** `chrom` and `pos_grch38` are written as NULL — the
PharmGKB TSV is rsID/haplotype-keyed and does not carry genomic
positions. The dbSNP loader in 5.4 will cross-reference rsID → chrom
+ pos and backfill these columns.

**Force-mode semantics.** `--force` bypasses the idempotence
short-circuit, re-downloads (via the cache's force flag), and
blanket-deactivates every existing active PharmGKB row before
inserting the new corpus. This avoids duplicate-active rows when the
version label is unchanged — the standard
`deactivate_prior_versions` helper's `source_version_id < new`
predicate skips a same-id refresh, so the loader handles the force
path with a separate UPDATE.

**Download mechanism.** PharmGKB's canonical
`api.pharmgkb.org/v1/download/file/data/clinicalAnnotations.zip` URL
serves a 303 redirect to its S3-hosted bucket. The 5.0 scaffold's
`download_to_cache` instantiates `httpx.Client` with the default
`follow_redirects=False`, which would write a 0-byte file on a 303
response and fail downstream with `BadZipFile`. This loader bypasses
`download_to_cache` for the actual download path and uses
`genome.privacy.external_client.ExternalClient` directly with an
injected `httpx.Client(follow_redirects=True)`. The audit row pair,
the `external_calls_enabled` enable-check, SHA-256 hashing,
`0600` chmod, and skip-if-exists cache semantics are all preserved
(see `_download_clinical_annotations_zip` in
`backend/src/genome/annotate/loaders/pharmgkb.py`). Future loaders
that hit redirect-heavy endpoints should mirror this helper until
the scaffold's downloader grows a `follow_redirects` parameter.

**Troubleshooting.**

* **`ExternalCallsDisabledError`** — `user_preferences.external_calls_enabled`
  is `false`. Run `genome config set external_calls_enabled true`.
  The blocked attempt is still recorded in `audit_log` for review.
* **0-byte `clinicalAnnotations.zip` / `BadZipFile`** — Symptom of
  the scaffold's downloader silently swallowing the 303 redirect.
  Should not occur for PharmGKB because the loader uses its own
  redirect-following client; if you see it, confirm the loader is
  actually entering `_download_clinical_annotations_zip` (not
  `download_to_cache`).
* **`PharmGKB clinical_annotations.tsv is missing expected columns`**
  — the TSV header has shifted. Open the cached ZIP at
  `~/.cache/genome/annotations/pharmgkb/clinicalAnnotations.zip` and
  inspect with `python -c "import zipfile; zipfile.ZipFile(...).read('clinical_annotations.tsv')[:200]"`.
  Update `_HEADER_TO_FIELD` in `pharmgkb.py` to match and add a
  CHANGELOG entry.
* **Recovery after a partial-failure refresh.** If the bulk insert
  raises mid-transaction, the loader rolls the per-source insert
  back and best-effort deletes the orphan `annotation_source_versions`
  row that `upsert_source_version` had already committed. A
  subsequent `refresh` starts clean. If the cleanup itself fails (the
  loader logs `pharmgkb.cleanup.orphan_version_row_delete_failed`),
  manually `DELETE FROM annotation_source_versions WHERE source_db =
  'pharmgkb' AND <the affected version>` before retrying.

### CPIC (sub-phase 5.1b — coming soon)

Placeholder section. CPIC's curated drug-gene guidance lands in
`cpic_guidelines`. Will follow the same shape as PharmGKB but with
CPIC's allele-tables source.

### ClinVar (sub-phase 5.2 — deferred)

Placeholder section.

### GWAS Catalog (sub-phase 5.3 — deferred)

Placeholder section.

## Audit log review

The full audit trail for a refresh is in `app.db.audit_log`. Group by
endpoint to see one source's history:

```sql
SELECT timestamp, action_type, resource_id, external_endpoint,
       json_extract(operation_details, '$.phase') AS phase,
       json_extract(operation_details, '$.status') AS status
  FROM audit_log
 WHERE external_endpoint LIKE 'annotations_%'
 ORDER BY timestamp DESC;
```

Each download produces an intent + outcome pair sharing
`external_payload_hash`. Blocked attempts (when
`external_calls_enabled = false`) appear as intent + blocked pairs.
The `external_payload_hash` for a GET is the SHA-256 of the URL —
useful for confirming the same URL was hit across attempts without
leaking the URL itself.
