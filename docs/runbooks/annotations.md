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

### ClinVar (sub-phase 5.2)

**What's loaded.** ClinVar's `variant_summary.txt.gz` (the canonical
tab-delimited per-variant release, ~3M rows in a 400+ MB gzipped TSV)
parsed and chunk-loaded into `clinvar_annotations`. ClinVar publishes
one row per `(VariationID, Assembly)` pair, so a variant carrying both
GRCh37 and GRCh38 positions appears twice. Every row is persisted (no
clinical-significance / variant-type filter at the loader -- that's a
query concern), but only `Assembly == 'GRCh38'` rows populate the
GRCh38-specific columns (`pos_grch38`, `ref_allele`, `alt_allele`).
GRCh37 rows still land in the table with those columns NULL so the
distinct-VariationID drift identifier covers every variant ClinVar
ships.

**Upstream URL.**
`https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz`.
NCBI's FTP host serves the file directly; the scaffold's
redirect-following client absorbs any future redirect transparently.
`URL_VERIFIED_DATE` in `backend/src/genome/annotate/loaders/clinvar.py`
records when the URL was last confirmed to work; bump it on any URL
change. The XML alternative
(`ClinVarVariationRelease_*.xml.gz`) is explicitly out of scope; if a
future sub-phase needs per-submitter SCV detail, that lives in a
separate evidence table, not in `clinvar_annotations`.

**Version label.** Resolved via a HEAD request to the variant_summary
URL. The HTTP `Last-Modified` response header (RFC 822 form, e.g.
`Sun, 10 May 2026 15:15:44 GMT`) is parsed via
`email.utils.parsedate_to_datetime` and rendered as `YYYY_MM_DD` to
match the schema's `annotation_source_versions.version` shape. When
the header is absent or unparseable, the loader falls back to today's
UTC date in the same format and logs the fallback loudly at INFO
(`clinvar.version.last_modified_missing` /
`clinvar.version.last_modified_unparseable`). The HEAD is the
loader's first audited call -- placed before the download so a fresh
refresh against an unchanged release short-circuits before re-fetching
the 400+ MB body.

**Provenance shape.** One file lands in the cache at
`~/.cache/genome/annotations/clinvar/variant_summary.txt.gz`. The
`annotation_source_versions` row records `source_url =
VARIANT_SUMMARY_URL`, the SHA-256 over the downloaded bytes (computed
during `download_to_cache`'s streaming write), and the byte size from
`stat()`. `record_count` is backfilled at the end of streaming with
the actual inserted row count (the count isn't known up front because
the parser is a generator).

**Runtime + disk.** ~10 GB free recommended under
`~/.cache/genome/annotations/clinvar/` -- the compressed file alone is
~440 MB; the supersession transaction's MVCC working set on a re-run
holds both the prior ~3M active rows (now flipped to inactive) and
the new ~3M active rows in the same WAL window. End-to-end on a
laptop is ~3-5 minutes wall-clock for parse + chunked insert against
the locked 250K-row chunk size, plus the network time to download the
gzipped TSV (a few seconds to a few minutes depending on link speed).

**Two-row-per-variant assembly split.** Each variant ID appears in
two rows (one per assembly) when ClinVar has positions for both
GRCh37 and GRCh38 -- which is the common case. The schema's
`pos_grch38` / `ref_allele` / `alt_allele` columns are only populated
for `Assembly == 'GRCh38'` rows; the GRCh37 row carries the same
identifiers (variation_id, rsid, conditions, clinical interpretation,
HGVS expressions) but NULLs out the position-specific columns. The
schema deliberately does not carry a `pos_grch37` column or an
`assembly` column, so the load contract preserves every TSV row but
keeps `pos_grch38` semantically clean.

**rsID coercion.** ClinVar encodes a missing rsID as the literal
string `"-1"` (an integer sentinel from the dbSNP era), not as the
empty string. The loader coerces both `"-1"` and the standard empty /
dash variants to NULL. Non-missing values are bare digit strings; the
loader prefixes them with `"rs"` to match the project-wide rsID
format (`variants_master`, `pharmgkb_annotations`, the dbSNP loader
that lands in 5.4). The distinct non-NULL rsID drift identifier
(`SELECT COUNT(DISTINCT rsid) ... WHERE is_active AND rsid IS NOT
NULL`) is the durable test that the `-1 → NULL` coercion stayed
correct across releases.

**Phenotype list fields.** Two list columns:

* `conditions VARCHAR[]` ← `PhenotypeList`. Single pipe `|` separates
  phenotype names. Empty / dash maps to NULL.
* `condition_ids VARCHAR[]` ← `PhenotypeIDS`. Two-level ClinVar
  encoding: `||` between phenotypes, `,` within one phenotype's IDs.
  The loader flattens both levels into one list of IDs because the
  schema's `condition_ids` is a flat array; consumers querying "is
  OMIM:613647 in condition_ids?" don't care which phenotype the ID
  belonged to.

**SubmitterCategories encoding.** ClinVar's source value is a single
integer (1-4 in observed releases) that bitmask-encodes which
submitter classes contributed (per ClinVar docs: 1 = literature only,
2 = at least one clinical lab, 3 = at least one expert panel /
practice guideline, 4 = practice guideline). The destination column
`submitter_categories VARCHAR[]` has a comment naming label-form
values like `'expert_panel'` / `'clinical_lab'` / `'lit_only'` but the
source data is integer-encoded. The loader preserves the integer
code as a single-element list (`["3"]`) rather than guessing at a
label mapping; consumers can map to canonical labels via a versioned
function once the label set is formally agreed.

**HGVS split.** ClinVar's `Name` column carries the full HGVS
expression for the variant (e.g.
`NM_014855.3(AP5Z1):c.80_83delinsTGCT… (p.Arg27_Ile28delinsLeuLeuTer)`).
The loader splits on the trailing `(p.…)` block: everything before
goes into `hgvs_c`, the `p.…` body itself goes into `hgvs_p`. When
no protein block is present, `hgvs_p` is NULL and `hgvs_c` is the
full `Name` value.

**`star_rating`.** Derived from `review_status` via the locked
mapping in `_REVIEW_STATUS_TO_STAR` (mirrors the official ClinVar
documentation at https://www.ncbi.nlm.nih.gov/clinvar/docs/review_status/).
Unmapped review-status strings yield NULL `star_rating` -- intentional
loud-fail so a future ClinVar wording change shows up in the post-load
`review_status_distribution` summary alongside a NULL `star_rating`
column instead of silently mapping to a wrong star count.

**`inheritance` is always NULL.** `variant_summary.txt` does not carry
inheritance pattern (the column lives in the per-variation XML
release). Setting it NULL for every row preserves the schema column
for a future XML-based loader that doesn't need to refactor the table.

**Chunked bulk insert.** Locked at 250,000 rows per chunk. The
streaming parser is a generator; `_stream_bulk_insert` drains it,
accumulates a chunk, registers it as a PyArrow Table, runs
`INSERT INTO clinvar_annotations (...) SELECT ... FROM <temp>`,
unregisters the table, and repeats. All chunks run inside one DuckDB
transaction (the same `conn.begin()` ... `conn.commit()` block that
brackets the `_deactivate_for_refresh` UPDATE). A mid-stream failure
rolls every chunk back together with the deactivation, preserving the
supersession-over-update invariant.

**Force-mode semantics.** `--force` bypasses the idempotence
short-circuit, re-downloads (via the cache's force flag), and
blanket-deactivates every existing active ClinVar row before
re-inserting the new corpus. Mirrors PharmGKB and CPIC's force path:
the standard `deactivate_prior_versions` helper's `source_version_id
< new` predicate would skip a same-version re-run, so the loader
issues a separate UPDATE that also tags `superseded_by = new_version`
(ClinVar carries `superseded_by`; PharmGKB and CPIC do not).

**Drift identifiers (locked).** The end-of-load structlog summary
emits the durable signals real-data verification will compare across
releases:

* `active_total` — `COUNT(*) WHERE is_active`
* `distinct_variation_id` — `COUNT(DISTINCT variation_id) WHERE is_active`
* `distinct_rsid_non_null` — `COUNT(DISTINCT rsid) WHERE is_active AND rsid IS NOT NULL`
* `clinical_significance_distribution` — group-by-and-count
* `review_status_distribution` — group-by-and-count

A drift in any of these on a re-run against the same release is a
regression signal; verify against the captured numbers in the 5.2
CHANGELOG entry.

**Troubleshooting.**

* **`ExternalCallsDisabledError`** — `user_preferences.external_calls_enabled`
  is `false`. Run `genome config set external_calls_enabled true`.
  The blocked attempt is still recorded in `audit_log` for review;
  the HEAD request is the loader's first audited call, so a disabled
  switch surfaces before any download bandwidth is spent.
* **`ClinVar variant_summary.txt is missing expected columns`** — the
  TSV header has shifted. Open the cached gz at
  `~/.cache/genome/annotations/clinvar/variant_summary.txt.gz` and
  inspect with
  `zcat .../variant_summary.txt.gz | head -1 | tr '\\t' '\\n' | nl`.
  Update `_REQUIRED_HEADERS` / `_row_to_parsed` in `clinvar.py` to
  match and add a CHANGELOG entry.
* **Mid-stream `MemoryError`** — chunk size is too large for the
  available RAM. The default 250K rows ≈ 125 MB working set; lower
  `_CHUNK_SIZE` if you hit OOM on a small machine.
* **Disk space failure mid-supersession** — the supersession
  transaction holds both the prior active rowset (now flipped to
  inactive) and the new active rowset in the same WAL window, so the
  on-disk DuckDB file roughly doubles in size during a re-run. Free
  ~5-10 GB before running a refresh against the prior corpus.
* **Recovery after a partial-failure refresh.** Same shape as
  PharmGKB / CPIC: if any chunk insert raises, the loader rolls the
  per-source insert (every chunk + the prior-version deactivation)
  back atomically and best-effort deletes the orphan
  `annotation_source_versions` row that `upsert_source_version` had
  already committed. A subsequent `refresh` starts clean. If the
  cleanup itself fails (the loader logs
  `clinvar.cleanup.orphan_version_row_delete_failed`),
  manually `DELETE FROM annotation_source_versions WHERE source_db =
  'clinvar' AND version = <the affected version>` before retrying.

### GWAS Catalog (sub-phase 5.3)

**What's loaded.** EBI's GWAS Catalog "all associations" release —
distributed as a ZIP archive (~60 MB) carrying one TSV
(`gwas-catalog-download-associations-alt-full.tsv`, ~300 MB
uncompressed, ~600-700K active associations at the current
release). The loader streams the TSV out of the ZIP without
unpacking to disk and chunk-loads into `gwas_catalog_associations`.
GWAS Catalog ships one row per curated SNP-trait association; the
loader splits any row whose `SNPS` cell carries multiple
`;`-separated rsIDs into one DB row per rsID (all sharing the
same study, PMID, trait, statistics, and sample-size context),
and drops rows with empty / missing `CHR_ID` or `CHR_POS` (the
schema's position-based join contract has no use for a
coordinate-less association). The schema's
`rsid VARCHAR NOT NULL` reflects that the loader's atomic unit is
(study, SNP), not (study, association entry).

**Upstream URLs (two-step).** The legacy
`api/search/downloads/full` endpoint that returned the canonical
TSV directly has been retired (404 since 2026 Q2). The current
pattern:

1. `GWAS_STATS_URL` = `https://www.ebi.ac.uk/gwas/api/search/stats`
   — returns JSON of the form
   `{"date": "YYYY-MM-DD", "ensemblbuild": "...", ...}`. The
   `date` field is the release-snapshot date and is the version
   label (rendered as `YYYY_MM_DD`, matching the ClinVar
   convention).
2. `GWAS_ASSOCIATIONS_ZIP_URL` =
   `https://ftp.ebi.ac.uk/pub/databases/gwas/releases/latest/
   gwas-catalog-associations_ontology-annotated-full.zip` — the
   "latest" symlink directory always points to the current
   release.

The download URL uses `/latest/` rather than the dated FTP path
because the stats-endpoint `date` (the data freeze date) and the
FTP directory day (the publication day) typically differ by 1-2
days, so a strict
`/releases/{YYYY}/{MM}/{DD}/...` template would 404. The
race window between the stats call and the download is bounded by
the weekly release cadence. `URL_VERIFIED_DATE` in
`backend/src/genome/annotate/loaders/gwas_catalog.py` records when
both URLs were last confirmed to work; bump it on any URL change.

**Version label.** Resolved via an audited GET against
`GWAS_STATS_URL`. The JSON `date` field (defensive: also accepts
`releasedate`) is rendered as `YYYY_MM_DD` (e.g. `2026_04_27`).
Failure modes:

* `ExternalCallsDisabledError` propagates — privacy gate is
  fail-closed.
* Any other `ExternalCallError` (network, HTTP 4xx/5xx)
  propagates. No silent fallback to "today" — that would either
  paint a misleading version label or cause a duplicate load.
  Operator retries instead.
* Malformed JSON or a missing `date` field raises `ValueError`
  with the live payload shape, so a future upstream API change
  surfaces as a fast diagnostic rather than a silent bad write.

The stats GET is the loader's first audited call — placed before
the download so a fresh refresh against an unchanged release
short-circuits before re-fetching the ~60 MB ZIP body, and a
disabled master switch surfaces `ExternalCallsDisabledError` after
one intent + blocked audit pair (matching the 5.1a/b/5.2 audited
refusal pattern).

**Provenance shape.** One file lands at
`~/.cache/genome/annotations/gwas_catalog/gwas-catalog-associations_ontology-annotated-full.zip`.
The `annotation_source_versions` row records `source_url =
GWAS_ASSOCIATIONS_ZIP_URL`, the SHA-256 over the downloaded ZIP
bytes (computed during `download_to_cache`'s streaming write),
and the byte size from `stat()`. `record_count` is backfilled at
the end of streaming with the actual inserted row count (the
count isn't known up front because the parser is a generator and
multi-SNP fan-outs / coordinate-less drops shift it).

**Runtime + disk.** ~1 GB free recommended under
`~/.cache/genome/annotations/gwas_catalog/` — the downloaded ZIP
is ~60 MB on disk (decompresses to a ~300 MB TSV the loader
streams in memory); the supersession transaction's MVCC working
set on a re-run holds both the prior ~600-700K active rows
(flipped to inactive) and the new ~600-700K active rows in the
same WAL window.
End-to-end on a laptop is **under five minutes wall-clock** for a
first-time load against the current release (the locked perf
target). Same-version `--force` re-runs are slower because the
deactivation UPDATE + checkpoint dominate, mirroring the ClinVar
behaviour documented in `docs/findings/finding-009`. The
finding-009 mitigations (explicit `CHECKPOINT`, chunked UPDATE)
are not yet in `supersession.py` and apply to GWAS Catalog
identically once they land.

**Multi-SNP expansion.** A row whose `SNPS` cell carries multiple
`;`-separated rsIDs (haplotype-style entries like
`rs123; rs456`) splits into one DB row per rsID. The loader
counts source rows that expanded (the `multi_snp_expansions`
field on the end-of-load summary). Splitting is on `;` only;
commas and `x` (the haplotype-intersection marker) are
deliberately not split — those forms represent a single combined
association rather than independent rsID-per-row entries, and the
schema's `rsid VARCHAR NOT NULL` contract is per-row so collapsing
them to one row would lose information. Real-data observations:
the current release ships a few hundred multi-SNP entries.

**Coordinate-less rows are dropped.** A row whose `CHR_ID` or
`CHR_POS` is empty (or one of the GWAS Catalog missing tokens
`NA` / `NR` / `-`) cannot satisfy the schema's position-based join
contract; the loader drops the entire row at parse time and
counts it in `dropped_empty_pos`. Real GWAS Catalog releases ship
a few hundred such rows — typically associations the curators
have not yet positionally mapped.

**Single-value `mapped_trait_uri`.** GWAS Catalog's
`MAPPED_TRAIT_URI` cell can carry multiple comma-separated EFO
URIs when an association has been mapped to several EFO terms
(e.g. `"...EFO_0000384,...EFO_0000729"`). The schema's
`mapped_trait_uri VARCHAR` is single-valued, so the loader keeps
the first URI (the curators' primary mapping) and increments
`truncated_mapped_trait_uri` on every truncation so the
end-of-load summary surfaces the total. `trait_id` is derived
from the same first URI via a trailing `<PREFIX>_<digits>` match
(e.g. `http://www.ebi.ac.uk/efo/EFO_0001065` → `EFO_0001065`).

**Field-level coercions.**

* `PUBMEDID` → `pmid VARCHAR`; missing → NULL.
* `STUDY ACCESSION` → `study_accession`; missing → NULL.
* `SNPS` → split on `;` into individual rsIDs; bare-digit tokens
  get the `rs` prefix; non-rsID tokens are rejected.
* `CHR_ID` → `normalize_chrom` (same alias remap as the ingestion
  pipeline: `23/24/25/26 → X/Y/MT`, alt / decoy / unplaced
  contigs filtered).
* `CHR_POS` → `pos_grch38 BIGINT`; non-integer → drop the row.
* `STRONGEST SNP-RISK ALLELE` → trailing `-<allele>` extracted as
  `effect_allele`; `?` and missing tokens → NULL.
* `RISK ALLELE FREQUENCY` → `effect_allele_freq DOUBLE` (accepts
  sci notation); `NR` → NULL.
* `P-VALUE` → `p_value DOUBLE` (sci notation parsed natively via
  `float`); missing → NULL.
* `OR or BETA` → `effect_size DOUBLE`; `effect_size_unit` is
  intentionally NULL in 5.3 (the column doesn't disambiguate at
  the row level; a future sub-phase can derive the unit from the
  free-text `95% CI (TEXT)` annotation).
* `95% CI (TEXT)` → bracket regex `[lower-upper]` extracts the
  two floating-point bounds into `ci_95_lower` / `ci_95_upper`;
  pure-text cells like `[NR] unit decrease` → NULL pair.
* `INITIAL SAMPLE SIZE` / `REPLICATION SAMPLE SIZE` →
  leading-integer extractor pulls the comma-grouped integer
  (`"4,512 European ancestry individuals"` → `4512`) into the
  schema's `INTEGER` columns; missing → NULL.
* `is_replicated` → `True` iff `REPLICATION SAMPLE SIZE` parses
  to a positive integer; missing / zero → NULL (not `False` —
  keeps `is_replicated IS TRUE` semantics free of false
  negatives downstream).
* `DISEASE/TRAIT` / `MAPPED_TRAIT` → `trait_name` (prefers
  MAPPED_TRAIT, falls back to DISEASE/TRAIT when MAPPED_TRAIT is
  empty).
* `ancestry` is intentionally NULL in 5.3. The associations TSV
  does not carry ancestry directly — that lives in a separate
  GWAS Catalog ancestry file that this loader does not consume.

**Chunked bulk insert.** Locked at 250,000 rows per chunk to
match the ClinVar loader. GWAS Catalog at ~600-700K rows fits in
2-3 chunks; the chunked-insert code path is exercised identically
across loaders. All chunks run inside one DuckDB transaction (the
same `conn.begin()` ... `conn.commit()` block that brackets the
`_deactivate_for_refresh` UPDATE). A mid-stream failure rolls
every chunk back together with the deactivation, preserving the
supersession-over-update invariant.

**Force-mode semantics.** `--force` bypasses the idempotence
short-circuit, re-downloads (via the cache's force flag), and
blanket-deactivates every existing active GWAS Catalog row before
re-inserting the new corpus. `gwas_catalog_associations` carries
`is_active` but **not** `superseded_by` (schema matches PharmGKB /
CPIC; ClinVar is the outlier that carries both), so the
deactivation is a pure `is_active = FALSE` flip and the
supersession chain is followed via the prior rows'
`source_version_id` column rather than a per-row tag. Same-version
`--force` re-runs reuse the existing `source_version_id` (the
upsert is idempotent on `(source_db, version)`).

**Drift identifiers (locked).** The end-of-load structlog summary
emits the durable signals real-data verification compares across
releases:

* `active_total` — `COUNT(*) WHERE is_active`
* `distinct_study_accession` — `COUNT(DISTINCT study_accession)`
* `distinct_pmid` — `COUNT(DISTINCT pmid)`
* `distinct_rsid` — `COUNT(DISTINCT rsid)`
* `distinct_trait_name` — `COUNT(DISTINCT trait_name)`

Plus parser stats: `rows_read`, `rows_emitted`,
`dropped_empty_pos`, `dropped_no_valid_snp`,
`multi_snp_expansions`, `truncated_mapped_trait_uri`. A drift in
any of the active / distinct counts on a re-run against the same
release is a regression signal; verify against the captured
numbers in the 5.3 CHANGELOG entry once real-data verification
lands.

**Real-data verification commands.**

```
genome config set external_calls_enabled true
genome annotate refresh --source gwas_catalog
```

Capture from the `gwas_catalog.refresh.complete` structlog line:
`active_total`, `distinct_study_accession`, `distinct_pmid`,
`distinct_rsid`, `distinct_trait_name`, plus parser stats and
wall-clock. Expected ranges (refine after first real-data load):

| Metric | Expected range |
|---|---|
| `active_total` | 600,000 – 750,000 |
| `distinct_study_accession` | 5,000 – 8,000 |
| `distinct_pmid` | 4,500 – 7,500 |
| `distinct_rsid` | 350,000 – 500,000 |
| `distinct_trait_name` | 4,000 – 6,500 |
| First-load wall-clock | < 5 minutes |

Re-run with `--force` to exercise the same-version supersession
path. Expected deltas: the same `active_total` lands under the
same `source_version_id`; an equal count of prior rows flips to
`is_active = FALSE`. Wall-clock will be larger than the first-load
window (the deactivation UPDATE + checkpoint dominate, per
finding-009).

**Troubleshooting.**

* **`ExternalCallsDisabledError`** —
  `user_preferences.external_calls_enabled` is `false`. Run
  `genome config set external_calls_enabled true`. The blocked
  attempt is still recorded in `audit_log` for review; the stats
  GET is the loader's first audited call, so a disabled switch
  surfaces before any download bandwidth is spent.
* **`GWAS Catalog stats response is missing a 'date' / 'releasedate'
  string field`** — the EBI REST API has shifted. Curl
  `https://www.ebi.ac.uk/gwas/api/search/stats` directly to see
  the live payload and update `_parse_stats_release_date` (and
  the runbook) to match.
* **`GWAS Catalog cached download ... is not a ZIP archive`** /
  **`missing expected entry`** — the EBI distribution layout has
  shifted (the file is no longer a ZIP, or the TSV inside has
  been renamed). Inspect the cached file at
  `~/.cache/genome/annotations/gwas_catalog/gwas-catalog-associations_ontology-annotated-full.zip`
  with `python -c "import zipfile; print(zipfile.ZipFile('....zip').namelist())"`
  and update `_ZIP_TSV_MEMBER` plus the loader's docstring.
* **`GWAS Catalog associations TSV is missing expected columns`**
  — the TSV header has shifted. Extract the cached file with
  `python -c "import zipfile; zipfile.ZipFile('....zip').extract('gwas-catalog-download-associations-alt-full.tsv', '/tmp')"`
  and inspect with `head -1 /tmp/...tsv | tr '\\t' '\\n' | nl`.
  Update `_REQUIRED_HEADERS` / `_row_to_parsed_rows` in
  `gwas_catalog.py` to match and add a CHANGELOG entry.
* **Unexpected drop spike (`dropped_empty_pos` jumps)** — the
  curation process at EBI sometimes ships a batch of
  positionally-unmapped associations during a release. Spot-check
  the structlog summary against the prior release's
  `dropped_empty_pos` value; a jump of more than a few hundred
  warrants a manual look at the upstream release notes.
* **Disk space failure mid-supersession** — the supersession
  transaction holds both the prior active rowset (now flipped
  to inactive) and the new active rowset in the same WAL window,
  so the on-disk DuckDB file grows during a re-run. Free
  ~1-2 GB before running a refresh against the prior corpus.
* **Recovery after a partial-failure refresh.** Same shape as
  PharmGKB / CPIC / ClinVar: if any chunk insert raises, the
  loader rolls the per-source insert (every chunk + the
  prior-version deactivation) back atomically and best-effort
  deletes the orphan `annotation_source_versions` row that
  `upsert_source_version` had already committed. A subsequent
  `refresh` starts clean. If the cleanup itself fails (the loader
  logs
  `gwas_catalog.cleanup.orphan_version_row_delete_failed`),
  manually `DELETE FROM annotation_source_versions WHERE
  source_db = 'gwas_catalog' AND version = <the affected
  version>` before retrying.

### PGS Catalog (sub-phase 5.4)

**What's loaded.** PGS Catalog's score-level metadata bundle
(`pgs_all_metadata.tar.gz`, ~4 MB gzipped TAR carrying eight per-
resource CSVs plus a sibling Excel workbook the loader ignores).
The loader parses the four CSVs relevant to score-level state --
scores (one row per PGS), publications (one row per PGP ID),
EFO traits (one row per ontology term), and performance metrics
(multiple rows per PGS, one per evaluation cohort / sample set) --
joins them client-side on the natural keys, and chunk-loads one
joined row per PGS into `pgs_catalog_scores`. PGS Catalog ships
~5-7K scores at the current release, so a full refresh fits in a
single chunk; the chunked-insert code path is exercised
identically to the larger loaders. This sub-phase loads the
score-level metadata only; the per-score variant weights table
(`pgs_score_weights`) is Phase 6 work.

**Upstream URLs (two-step).**

1. `PGS_RELEASE_LATEST_URL` =
   `https://www.pgscatalog.org/rest/release/current/` -- REST
   endpoint returning JSON of the form
   `{"date": "YYYY-MM-DD", "score_count": N, "performance_count":
   N, "publication_count": N, ...}`. The `date` field is the
   release-snapshot date and is the version label (rendered as
   `YYYY_MM_DD`, matching the ClinVar / GWAS Catalog convention).
   Note the endpoint is `/release/current/`, not
   `/release/latest/` -- the latter returned HTTP 500 at the
   verification date.
2. `PGS_METADATA_BUNDLE_URL` =
   `https://ftp.ebi.ac.uk/pub/databases/spot/pgs/metadata/
   pgs_all_metadata.tar.gz` -- the canonical "latest" bundle.
   `download_to_cache` injects an
   `httpx.Client(follow_redirects=True)` so any FTP/CDN redirect
   lands transparently on disk.

`URL_VERIFIED_DATE` in
`backend/src/genome/annotate/loaders/pgs_catalog.py` records when
both URLs were last confirmed to work; bump it on any URL change.

**Version label.** Resolved via an audited GET against
`PGS_RELEASE_LATEST_URL`. The JSON `date` field (defensive: also
accepts `release_date` and `releasedate`) is rendered as
`YYYY_MM_DD` (e.g. `2026_05_07`). Failure modes mirror GWAS
Catalog's stats resolver:

* `ExternalCallsDisabledError` propagates -- privacy gate is
  fail-closed.
* Any other `ExternalCallError` (network, HTTP 4xx/5xx)
  propagates. No silent fallback to "today" -- that would either
  paint a misleading version label or cause a duplicate load.
  Operator retries instead.
* Malformed JSON or a missing `date` field raises `ValueError`
  with the live payload shape, so a future upstream API change
  surfaces as a fast diagnostic rather than a silent bad write.

The release-current GET is the loader's first audited call --
placed before the download so a fresh refresh against an
unchanged release short-circuits before re-fetching the ~4 MB
bundle, and a disabled master switch surfaces
`ExternalCallsDisabledError` after one intent + blocked audit
pair (matching the 5.1a/b/5.2/5.3 audited refusal pattern).

**Provenance shape.** One file lands at
`~/.cache/genome/annotations/pgs_catalog/pgs_all_metadata.tar.gz`.
The `annotation_source_versions` row records `source_url =
PGS_METADATA_BUNDLE_URL`, the SHA-256 over the downloaded TAR
bytes (computed during `download_to_cache`'s streaming write),
and the byte size from `stat()`. `record_count` is backfilled at
the end of streaming with the actual inserted row count (one row
per PGS in the bundle).

**Runtime + disk.** ~1 GB free recommended under
`~/.cache/genome/annotations/pgs_catalog/` -- the bundle is ~4 MB
compressed (decompresses to ~15 MB; the loader holds it in
memory rather than unpacking to disk); the supersession
transaction's MVCC working set on a re-run holds both the prior
~5-7K active rows (flipped to inactive) and the new ~5-7K active
rows in the same WAL window. End-to-end on a laptop is **under
30 seconds wall-clock** (the project-wide routine-refresh target
documented in CLAUDE.md). The bundle is small enough that the
finding-009 ClinVar-scale UPDATE+checkpoint cost is not a factor
here -- a same-version `--force` re-run completes well inside
the same target.

**Multi-file join contract.** The bundle contains four CSVs we
join on natural keys:

1. `pgs_all_metadata_scores.csv` -- one row per PGS, keyed by
   `Polygenic Score (PGS) ID`. The loader's atomic unit. The
   `Mapped Trait(s) (EFO ID)` column can carry multiple comma-
   separated IDs when a score is mapped to several ontology
   terms; the schema's `trait_efo VARCHAR` is single-valued so
   the loader keeps the first ID (the curators' primary
   mapping) and counts the truncations
   (`truncated_trait_efo`).
2. `pgs_all_metadata_publications.csv` -- one row per
   PGS Publication ID (PGP). Joins to the scores via
   `PGS Publication (PGP) ID`. Contributes `publication_pmid`,
   `publication_doi`, and `publication_year` (the loader pulls
   the four-digit year out of the publication's
   `Publication Date` ISO string).
3. `pgs_all_metadata_efo_traits.csv` -- one row per EFO/MONDO/HP
   term. Joins to the scores via the (possibly-truncated)
   trait EFO ID. The upstream EFO traits CSV does **not** ship
   a `Trait Category` column at the verified date; the loader
   leaves `trait_category = NULL` for every row. A future
   schema or loader change can backfill the column from an EFO
   hierarchy walk; that's out of scope for 5.4.
4. `pgs_all_metadata_performance_metrics.csv` -- multiple rows
   per PGS, one per evaluation cohort / sample set. Joins to
   the scores via `Evaluated Score`. The per-cohort entries
   are collapsed into the schema's two scalar columns via the
   max reduction documented below.

Counters surfaced on the end-of-load summary:

* `orphan_publication_refs` -- a score's PGP ID is missing from
  the publications dict. The row still emits with
  `publication_pmid` / `publication_doi` / `publication_year`
  set to NULL.
* `orphan_trait_refs` -- a score's trait EFO ID is missing from
  the EFO traits dict. The row still emits with
  `trait_category = NULL` (which, given the prior point, would
  be NULL anyway today).
* `scores_without_performance` -- a score has no entries in the
  performance dict. Both performance columns emit NULL.

**Performance-metric max reduction (auditability trade-off).**
A single PGS typically has multiple `performance_metrics` rows
(one per evaluation cohort). The schema's `performance_auc` and
`performance_or_per_sd` columns are scalars, so the loader
collapses the per-cohort entries via `max(non-NULL values)` per
column independently:

* `performance_auc = max(e.auc for e in entries if e.auc is not
  None)`, or NULL if all entries lack AUC.
* `performance_or_per_sd = max(e.or_per_sd for e in entries if
  e.or_per_sd is not None)`, or NULL if all entries lack OR.

The max reduction is the simplest auditable rule at this scale,
not the most statistically honest one. Picking the
best-performing cohort always over-states what the typical user
will see, and the cohort selection (European vs East Asian vs
multi-ancestry) is often the bigger contributor to that number
than any modelling choice. Honest per-cohort reporting would
require a separate `pgs_catalog_performance` table -- a future
schema change, not 5.4 work. The end-of-load summary surfaces
`multi_cohort_performance` (count of scores with > 1 cohort
entry) so downstream consumers can see when the scalar is the
output of a reduction vs a single-entry source.

The PGS Catalog OR column ships as `Odds Ratio (OR)` without
the "per SD" qualifier; the schema's column name
`performance_or_per_sd` reflects the typical PRS convention
(report OR per 1-SD increase in score) but the loader does not
enforce that semantics. Consumers querying
`performance_or_per_sd` should expect generic OR for PGS where
the source paper reported a different scaling; the OR column
is a coarse signal, not a calibration target.

**Field-level coercions.**

* `Polygenic Score (PGS) ID` → `pgs_id`; missing → row dropped
  silently (schema's `pgs_id NOT NULL` would reject it anyway).
* `PGS Name` → `pgs_name`; missing → NULL.
* `Reported Trait` → `trait_reported`; missing → NULL.
* `Mapped Trait(s) (EFO ID)` → split on `,`, keep first; count
  the truncation. Empty / `NR` / `-` / `NA` → NULL.
* `Number of Variants` → `variants_total INTEGER`; non-numeric
  / `NR` → NULL.
* `PGS Publication (PGP) ID` → publication join key.
* `Ancestry Distribution (%) - Source of Variant Associations
  (GWAS)` → `ancestry_distribution` (verbatim free-text).
* `Ancestry Distribution (%) - Score Development/Training` →
  `reference_population` (verbatim free-text).
* `Publication Date` → `publication_year INTEGER` (regex match
  against `YYYY-MM-DD` or `YYYY/MM/DD`; non-matching → NULL).
* `Publication (PMID)` and `PubMed ID (PMID)` → string preserved
  (the schema uses VARCHAR even though the source value is an
  integer).
* `digital object identifier (doi)` → `publication_doi`;
  missing → NULL.
* `Odds Ratio (OR)` → leading-number extractor pulls the point
  estimate out of `"<estimate> [<lower>,<upper>]"`. Pure-text
  cells (`NR`, `[NR]`, "Hazard ratio not reported", etc.) →
  NULL.
* `Area Under the Receiver-Operating Characteristic Curve
  (AUROC)` → same extractor.
* `Ontology Trait ID` → EFO/MONDO/HP key; used to look up the
  trait row (the loader does not derive `trait_id` separately
  -- the EFO ID stored on the score IS the trait identifier).
* `weights_storage` → not assigned by the loader; the schema
  default `'overlapping_only'` applies to every inserted row.

**Drift identifiers (locked).** The end-of-load structlog
summary emits the durable signals real-data verification will
compare across releases:

* `active_total` -- `COUNT(*) WHERE is_active`
* `distinct_pgs_id` -- `COUNT(DISTINCT pgs_id) WHERE is_active`
  (should equal `active_total` post-load if no upstream PGS
  duplicates)
* `distinct_trait_efo`
* `distinct_publication_pmid`
* `distinct_trait_category` (zero today; the EFO traits CSV
  doesn't ship a category column)
* `with_performance_auc` -- count where
  `performance_auc IS NOT NULL`
* `with_performance_or_per_sd`

Plus parser stats: `rows_read_scores`, `rows_read_publications`,
`rows_read_traits`, `rows_read_performance`,
`orphan_publication_refs`, `orphan_trait_refs`,
`scores_without_performance`, `multi_cohort_performance`,
`truncated_trait_efo`. A drift in any of the active / distinct
counts on a re-run against the same release is a regression
signal; verify against the captured numbers in the 5.4
CHANGELOG entry once real-data verification lands.

**Real-data verification commands.**

```
genome config set external_calls_enabled true
genome annotate refresh --source pgs_catalog
```

Capture from the `pgs_catalog.refresh.complete` structlog line:
`active_total`, `distinct_pgs_id`, `distinct_trait_efo`,
`distinct_publication_pmid`, `with_performance_auc`,
`with_performance_or_per_sd`, plus parser stats and wall-clock.
Expected ranges (refine after the first real-data load):

| Metric | Expected range |
|---|---|
| `active_total` | 5,000 – 8,000 |
| `distinct_pgs_id` | 5,000 – 8,000 |
| `distinct_trait_efo` | 600 – 1,200 |
| `distinct_publication_pmid` | 500 – 1,200 |
| `with_performance_auc` | 3,000 – 6,000 |
| `with_performance_or_per_sd` | 2,500 – 5,000 |
| `multi_cohort_performance` | 1,500 – 4,000 |
| First-load wall-clock | < 30 seconds |

Re-run with `--force` to exercise the same-version supersession
path. Expected deltas: the same `active_total` lands under the
same `source_version_id`; an equal count of prior rows flips to
`is_active = FALSE`. Wall-clock stays inside the 30 s target
(PGS Catalog is small enough that the finding-009 UPDATE+
checkpoint penalty does not materialize).

**Troubleshooting.**

* **`ExternalCallsDisabledError`** --
  `user_preferences.external_calls_enabled` is `false`. Run
  `genome config set external_calls_enabled true`. The blocked
  attempt is still recorded in `audit_log` for review; the
  release-current GET is the loader's first audited call, so a
  disabled switch surfaces before any download bandwidth is
  spent.
* **`PGS Catalog release response is missing a 'date' /
  'release_date' / 'releasedate' string field`** -- the
  `/rest/release/current/` API has shifted. Curl
  `https://www.pgscatalog.org/rest/release/current/` directly
  to see the live payload and update `_parse_release_payload`
  (and this runbook) to match.
* **`PGS Catalog bundle ... missing expected entry`** -- the
  bundle layout has shifted (a CSV has been renamed or the
  packaging changed). Inspect the cached bundle with
  `tar -tzf ~/.cache/genome/annotations/pgs_catalog/pgs_all_metadata.tar.gz`
  and update the per-file `_*_MEMBER` constants to match.
* **`PGS Catalog CSV ... is missing expected columns`** -- a
  per-file header has shifted. Extract the cached bundle and
  `head -1` the relevant CSV; update the
  `_*_REQUIRED_HEADERS` tuple and any column-name references
  in `pgs_catalog.py` to match, then add a CHANGELOG entry.
* **Unexpected `orphan_publication_refs` spike** -- the
  publications CSV has dropped entries that the scores CSV
  still references. The drift is upstream; if the spike
  persists across releases, contact PGS Catalog support.
* **Unexpected `scores_without_performance` spike** -- a
  release shipped scores without paired performance rows.
  Probably benign; verify against the upstream release notes.
* **Disk space failure mid-supersession** -- the supersession
  transaction holds both the prior active rowset (now flipped
  to inactive) and the new active rowset in the same WAL
  window, so the on-disk DuckDB file grows during a re-run.
  PGS Catalog at ~5-7K rows is much smaller than the ClinVar
  case but free ~100 MB before running a refresh against the
  prior corpus.
* **Recovery after a partial-failure refresh.** Same shape as
  PharmGKB / CPIC / ClinVar / GWAS Catalog: if the bulk insert
  raises, the loader rolls the per-source insert (every chunk
  + the prior-version deactivation) back atomically and
  best-effort deletes the orphan `annotation_source_versions`
  row that `upsert_source_version` had already committed. A
  subsequent `refresh` starts clean. If the cleanup itself
  fails (the loader logs
  `pgs_catalog.cleanup.orphan_version_row_delete_failed`),
  manually `DELETE FROM annotation_source_versions WHERE
  source_db = 'pgs_catalog' AND version = <the affected
  version>` before retrying.

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
