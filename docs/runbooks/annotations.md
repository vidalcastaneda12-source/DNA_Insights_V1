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
serves a 303 redirect to its S3-hosted bucket. The scaffold's
`download_to_cache` injects an `httpx.Client(follow_redirects=True)`
into the audited `ExternalClient` so the redirect chain is followed
transparently and the loader writes the canonical URL into its
constants. Every later loader (CPIC, ClinVar, GWAS, dbSNP, gnomAD)
inherits the same handling for free.

**Troubleshooting.**

* **`ExternalCallsDisabledError`** — `user_preferences.external_calls_enabled`
  is `false`. Run `genome config set external_calls_enabled true`.
  The blocked attempt is still recorded in `audit_log` for review.
* **0-byte `clinicalAnnotations.zip` / `BadZipFile`** — Pre-fix
  symptom: the scaffold's downloader used `follow_redirects=False`
  and wrote the empty redirect body to disk. Fixed in the same PR
  that shipped this loader. If you encounter this on a future
  loader, check whether `download_to_cache` still injects a
  redirect-following client (the regression test
  `test_download_to_cache_follows_303_redirect` pins the contract).
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

### CPIC (sub-phase 5.1b)

**What's loaded.** CPIC's drug-gene clinical guidance, pulled directly
from the CPIC PostgREST API at `api.cpicpgx.org/v1/` and joined
client-side from four endpoints — `/guideline`, `/pair`,
`/recommendation`, `/drug` — into per-row
(gene × drug × phenotype) tuples in `cpic_guidelines`. A single CPIC
recommendation that names `n` genes in its `lookupkey` produces `n`
rows in the table, all sharing the same `cpic_id` (the CPIC
recommendation primary key) and differing only in `gene_symbol` and
`phenotype`. Real-data verification against the 2026-05-14 release
landed 3,591 rows from 2,159 recommendations across 19 genes and 109
drugs.

**Upstream URLs.**

* `https://api.cpicpgx.org/v1/guideline` — guideline metadata
  (id, name, clinpgxid, url).
* `https://api.cpicpgx.org/v1/pair` — gene-drug pair table
  (cpiclevel, citations).
* `https://api.cpicpgx.org/v1/recommendation` — the recommendation
  rows (drugid, guidelineid, lookupkey, classification, population).
* `https://api.cpicpgx.org/v1/drug` — drug metadata (name, rxnormid).
* `https://api.cpicpgx.org/v1/change_log?order=date.desc&limit=1&select=date`
  — the version-resolution canary; one row, one column.

`URL_VERIFIED_DATE` in `backend/src/genome/annotate/loaders/cpic.py`
records when the URLs were last confirmed to work; bump it on any URL
change.

**Version label.** Resolved from the most recent `/change_log` entry's
`date` field, reformatted as `YYYY_MM_DD` to match the
`annotation_source_versions.version` shape (CPIC writes a new
`change_log` row on every data update, so the latest entry's date is
the closest thing CPIC publishes to a "release date"). When the
canary query fails or returns nothing parseable, the loader falls
back to today's UTC date in the same format and logs the fallback
loudly at INFO (`cpic.version.no_metadata_fallback`).

**Provenance shape.** Four data files land in the cache (sizes from
the 2026-05-14 release: guideline ≈ 5.9 KB, pair ≈ 169 KB,
recommendation ≈ 2.3 MB, drug ≈ 73 KB; total ≈ 2.5 MB). The
`annotation_source_versions` row records:

* `source_url = GUIDELINE_URL` — the canonical entrypoint.
* `source_file_hash` — a SHA-256 computed over the sorted
  `(endpoint, sha256)` tuples of the four data endpoints, so the
  fingerprint changes iff any one endpoint's data changes.
* `source_file_size` — the sum of the four data files' byte sizes.
  The version canary's size is not included; per-endpoint sizes are
  available in the structlog `cpic.download.audited` events.

**Runtime + disk.** ~1 GB total download budget under
`~/.cache/genome/annotations/cpic/` (the real archive is ~2.5 MB —
the budget leaves room for future growth). End-to-end refresh on a
laptop is a few seconds: four network round-trips + a client-side
in-memory join over ~3.5 K rows.

**Multi-gene split.** A CPIC recommendation whose `lookupkey` carries
multiple gene → phenotype entries (typical for warfarin's CYP2C9 +
VKORC1 guidance, etc.) splits into one row per gene. The split rows
share the same `cpic_id` and `recommendation` text but differ in
`gene_symbol`, `phenotype`, `cpic_level`, and `publication_pmid` —
the last two are looked up per pair, not per recommendation. Real
data: 1,432 of 2,159 recommendations have multi-gene lookupkeys,
yielding 3,591 emitted rows in total.

**Skipped recommendations.** Two structural skip paths:

* `lookupkey == {}` or unparseable — the row carries no phenotype,
  so it cannot satisfy the loader's
  (gene × drug × phenotype) granularity contract. Real-data
  verification shows zero such rows today, but the skip is
  structural, not data-dependent. Skipped rows produce a debug log
  line at `cpic.recommendation.skipped_no_lookupkey` with the
  recommendation id.
* `drugid` not present in `/drug`, or drug entry missing a `name` —
  the schema's NOT NULL `drug_name` would reject the row anyway, so
  the loader drops it at parse time and logs at
  `cpic.recommendation.skipped_unknown_drug` /
  `cpic.recommendation.skipped_no_drugid` with the
  recommendation id.

**Pediatric flag.** Set strictly: `True` iff
`recommendation.population == 'pediatrics'`; otherwise `None`. CPIC's
`population` column overloads two axes (age and condition), and many
recommendations land as `'general'`, `'adults'`, or a condition
label like `'PHT naive'`. None of those are positive pediatric
signals, so they map to `None` (not `False`) — this keeps
`pediatric IS TRUE` semantics free of false negatives downstream.
Real data: 30 of 3,591 rows have `pediatric = TRUE`; the rest are
`NULL`.

**Publication PMID.** Taken as the first entry of the pair's
`citations` array (the canonical guideline publication). Empty
citation lists map to `NULL`. The schema's single-VARCHAR
`publication_pmid` column means additional PMIDs in a pair's
citations are not preserved by this loader; the schema doc reserves
multi-publication queries for the dedicated publication-index work
that will come with a later loader.

**`last_updated` is always NULL.** None of the four data endpoints
carries a per-row update date. CPIC's `change_log` could be joined
in to derive a per-entity date, but that is a 5th endpoint and the
join is too sparse to be worth the audit-log noise; the global
`annotation_source_versions.ingested_at` timestamp is the loader's
durable record of when each snapshot landed.

**Force-mode semantics.** `--force` bypasses the idempotence
short-circuit, re-downloads every endpoint (including the canary),
and blanket-deactivates every existing active CPIC row before
inserting the freshly joined corpus. This mirrors the PharmGKB
loader's force path: the standard
`deactivate_prior_versions` helper's
`source_version_id < new` predicate would skip a same-version
re-run, so the loader does a separate UPDATE to avoid
duplicate-active rows.

**Troubleshooting.**

* **`ExternalCallsDisabledError`** — `user_preferences.external_calls_enabled`
  is `false`. Run `genome config set external_calls_enabled true`.
  The blocked attempt is still recorded in `audit_log` for review.
* **0-byte endpoint file** — Pre-fix symptom of the
  `follow_redirects=False` bug in 5.1a's scaffold; fixed in that
  same PR. If you encounter this on a future loader, check whether
  `download_to_cache` still injects a redirect-following client
  (the regression test
  `test_download_to_cache_follows_303_redirect` pins the contract).
* **`CPIC endpoint payload <file> is not a JSON array`** — the
  PostgREST API returned an error object or single record (e.g.
  when the URL is mistyped or the endpoint was renamed). Inspect
  the cached file at
  `~/.cache/genome/annotations/cpic/<file>` and confirm it is a
  top-level JSON array; if not, the upstream contract has shifted
  and the URL or query string in `cpic.py` needs updating.
* **Version label stuck on a stale date after a CPIC release** —
  the canary file in
  `~/.cache/genome/annotations/cpic/change_log_latest.json` is
  cached. Run with `--force` to re-fetch it.
* **Recovery after a partial-failure refresh.** Same shape as
  PharmGKB: if the bulk insert raises mid-transaction, the loader
  rolls the per-source insert back and best-effort deletes the
  orphan `annotation_source_versions` row that
  `upsert_source_version` had already committed. A subsequent
  `refresh` starts clean. If the cleanup itself fails (the loader
  logs `cpic.cleanup.orphan_version_row_delete_failed`),
  manually `DELETE FROM annotation_source_versions WHERE source_db =
  'cpic' AND version = <the affected version>` before retrying.

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
