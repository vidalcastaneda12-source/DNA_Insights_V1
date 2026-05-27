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
4. Optional opt-in short-circuit (`--skip-if-same-version`): if
   `annotation_sources` for this source already points at a
   `annotation_source_versions` row whose `(version, source_file_hash)`
   matches the freshly-resolved pair, the loader returns
   `was_already_current=True` and exits without re-writing.
5. Allocates a fresh `source_version_id` in `annotation_source_versions`.
6. Parses the artifact and bulk-loads into the source's destination
   table under the new `source_version_id` (chunked INSERTs inside one
   DuckDB transaction).
7. UPSERTs `annotation_sources.current_source_version_id` for this
   `source_db` to the new id — the single-row pointer flip is the
   supersession event. Prior-version rows remain in the per-source
   table indefinitely, keyed by their older `source_version_id`, and
   are filtered out of reader joins on `annotation_sources`. See
   [finding-010](../findings/finding-010-version-pointer-supersession-pattern.md)
   for the rationale.

## After a schema rebuild

When a PR modifies `docs/schemas/` or `ddl/`, the
project-wide remediation is `rm -rf data/` followed by `uv run genome
init` (see CLAUDE.md "Schema changes require rebuilding local
databases"). `genome init` recreates an empty `genome.duckdb` against
the new DDL — it does **not** re-ingest anything. To return to a
working state, every data source that previously populated the
database needs to be reloaded:

1. Re-ingest chip data: `genome ingest --source 23andme <path>` and
   `genome ingest --source ancestry <path>` (Phase 2 commands).
2. Re-run the merge and (if Phase 4 is in use) imputation pipelines
   per the relevant runbooks.
3. Re-load every annotation source that was previously refreshed:
   ```
   genome annotate refresh --source pharmgkb
   genome annotate refresh --source cpic
   genome annotate refresh --source clinvar
   genome annotate refresh --source gwas_catalog
   genome annotate refresh --source pgs_catalog
   genome annotate refresh --source gnomad
   genome annotate refresh --source dbsnp
   genome annotate refresh-aliases
   genome annotate refresh-index
   ```

The last four are order-sensitive: `gnomad` builds its
`(user ∪ ClinVar ∪ GWAS)` filter from the active ClinVar and GWAS
releases, so both must already be loaded; `refresh-index` rolls up the
four variant-linkable sources (ClinVar, GWAS, gnomAD, PharmGKB) through
their version pointers, so it runs last. `dbsnp` reads only
`variants_master` and is order-independent among the loaders, **but
`refresh-aliases` must run after `--source dbsnp`** — it attaches the
rsID-merge map to the current dbSNP `source_version_id` and must be
re-run after any future dbSNP refresh that flips that pointer.

The annotation refreshes are idempotent on
`(source_db, version, source_file_hash)` — if upstream hasn't moved
since the last refresh, the cached download is reused and the version
pointer is established for the new database. ClinVar is the longest
single source by wall-clock; the rest combined take under a minute on
a warm cache.

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

**Force-mode semantics.** `--force` bypasses the
`--skip-if-same-version` short-circuit (when set) and re-downloads
via the cache's force flag, then runs the same supersession path as
a normal refresh: allocate a fresh `source_version_id`, INSERT the
new corpus under it, and call `flip_to_new_version` to UPSERT
`annotation_sources.current_source_version_id` to the new id. A
same-version `--force` against an unchanged upstream still allocates
a new `source_version_id` (identity in
`annotation_source_versions` is the row, not `(source_db, version)`);
the prior rowset stays in `pharmgkb_annotations` keyed by the old id
and is filtered out by reader joins on `annotation_sources`.

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

**Force-mode semantics.** `--force` bypasses the
`--skip-if-same-version` short-circuit (when set), re-downloads every
endpoint (including the canary), allocates a fresh
`source_version_id`, INSERTs the freshly joined corpus under it, and
calls `flip_to_new_version` to UPSERT
`annotation_sources.current_source_version_id` to the new id.
Mirrors the PharmGKB force path: a same-version `--force` still
allocates a new id (the registry's identity is per row, not per
`(source_db, version)` pair); prior CPIC rows stay in
`cpic_guidelines` under their older `source_version_id`.

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
(`SELECT COUNT(DISTINCT rsid) FROM clinvar_annotations ca
JOIN annotation_sources s ON s.source_db = 'clinvar'
AND s.current_source_version_id = ca.source_version_id
WHERE rsid IS NOT NULL`) is the durable test that the `-1 → NULL`
coercion stayed correct across releases.

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
transaction; the closing `commit_and_checkpoint` and the
`flip_to_new_version` pointer UPSERT against `annotation_sources`
run inside the same transaction. A mid-stream failure rolls every
chunk back together with the pointer-flip step, preserving the
supersession atomicity contract (CLAUDE.md decision #7) — readers
never see a torn state.

**Force-mode semantics.** `--force` bypasses the
`--skip-if-same-version` short-circuit (when set) and re-downloads
via the cache's force flag, then runs the same supersession path as
a normal refresh: allocate a fresh `source_version_id`, INSERT the
new corpus under it, and call `flip_to_new_version`. The pointer
flip is a single-row UPSERT against `annotation_sources` regardless
of upstream version label — a same-version `--force` re-run
allocates a new id, lands a new rowset, and flips the pointer to it.
Prior ClinVar rows stay in `clinvar_annotations` under their older
`source_version_id`; the `clinvar_annotations` table no longer
carries per-row `is_active` / `superseded_by` columns (the
distinguishing feature ClinVar previously held vs PharmGKB / CPIC
is now moot — every supersedable annotation table follows the
version-pointer pattern).

**Drift identifiers (locked).** The end-of-load structlog summary
emits the durable signals real-data verification will compare across
releases. All `active_total` / `distinct_*` counts are computed at
the new `source_version_id` the loader just landed — equivalently
the rows scoped by the canonical
`JOIN annotation_sources s ON s.source_db = 'clinvar' AND
s.current_source_version_id = ca.source_version_id` read pattern:

* `active_total` — `COUNT(*)` of rows at the new
  `source_version_id`
* `distinct_variation_id` — `COUNT(DISTINCT variation_id)` at the new
  `source_version_id`
* `distinct_rsid_non_null` — `COUNT(DISTINCT rsid)` at the new
  `source_version_id` `WHERE rsid IS NOT NULL`
* `clinical_significance_distribution` — group-by-and-count at the
  new `source_version_id`
* `review_status_distribution` — group-by-and-count at the new
  `source_version_id`

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
  transaction holds the prior rowset (still present, keyed by the
  older `source_version_id`) and the new rowset (being inserted
  under the freshly-allocated `source_version_id`) in the same WAL
  window, so the on-disk DuckDB file roughly doubles in size during
  a re-run. Free ~5-10 GB before running a refresh against the
  prior corpus. Prior versions remain in the table after the
  transaction commits — see finding-010 follow-up #14 for the
  open cleanup procedure.
* **Recovery after a partial-failure refresh.** Same shape as
  PharmGKB / CPIC: if any chunk insert or the closing pointer flip
  raises, the loader rolls the per-source insert (every chunk +
  any partially-applied `annotation_sources` UPSERT) back atomically
  and best-effort deletes the orphan `annotation_source_versions`
  row that `upsert_source_version` had already committed. A
  subsequent `refresh` starts clean. If the cleanup itself fails
  (the loader logs `clinvar.cleanup.orphan_version_row_delete_failed`),
  manually `DELETE FROM annotation_source_versions WHERE source_db =
  'clinvar' AND source_version_id = <the affected id>` before
  retrying.

### GWAS Catalog (sub-phase 5.3)

**What's loaded.** EBI's GWAS Catalog "all associations" release —
distributed as a ZIP archive (~60 MB) carrying one TSV
(`gwas-catalog-download-associations-alt-full.tsv`, ~300 MB
uncompressed, ~919K active associations at the current
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
set holds the new ~919K rowset (being inserted under a fresh
`source_version_id`) alongside the prior rowset (still resident
under its older `source_version_id`) in the same WAL window.
End-to-end on a laptop is **under five minutes wall-clock** for a
first-time load against the current release (the locked perf
target). Same-version `--force` re-runs are no slower in the
dominant phase than first-time loads — the supersession event is
a single-row `annotation_sources` UPSERT (finding-010), so the
ClinVar ~17-19 min UPDATE phase finding-009 #15 attributed to the
per-row model does not apply to GWAS Catalog (or any other Phase-5
loader) anymore.

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
match the ClinVar loader. GWAS Catalog at ~919K rows fits in
~4 chunks; the chunked-insert code path is exercised identically
across loaders. All chunks run inside one DuckDB transaction; the
closing `commit_and_checkpoint` and the `flip_to_new_version`
pointer UPSERT against `annotation_sources` run inside the same
transaction. A mid-stream failure rolls every chunk back together
with the pointer-flip step, preserving the supersession atomicity
contract (CLAUDE.md decision #7).

**Force-mode semantics.** `--force` bypasses the
`--skip-if-same-version` short-circuit (when set), re-downloads
via the cache's force flag, allocates a fresh `source_version_id`,
INSERTs the new corpus under it, and calls `flip_to_new_version`
to UPSERT `annotation_sources.current_source_version_id` to the
new id. The per-source table no longer carries `is_active` /
`superseded_by` columns — every supersedable annotation table now
uses the version-pointer pattern uniformly (the
ClinVar-was-the-outlier-that-carried-`superseded_by` distinction
is moot post-PR-#43). Same-version `--force` allocates a new id
rather than reusing the prior one (identity in the registry is
per-row, not `(source_db, version)`).

**Drift identifiers (locked).** The end-of-load structlog summary
emits the durable signals real-data verification compares across
releases. All `active_total` / `distinct_*` counts are computed at
the new `source_version_id` the loader just landed — equivalently
the rows scoped by the canonical
`JOIN annotation_sources s ON s.source_db = 'gwas_catalog' AND
s.current_source_version_id = ga.source_version_id` read pattern:

* `active_total` — `COUNT(*)` at the new `source_version_id`
* `distinct_study_accession` — `COUNT(DISTINCT study_accession)` at
  the new `source_version_id`
* `distinct_pmid` — `COUNT(DISTINCT pmid)` at the new
  `source_version_id`
* `distinct_rsid` — `COUNT(DISTINCT rsid)` at the new
  `source_version_id`
* `distinct_trait_name` — `COUNT(DISTINCT trait_name)` at the new
  `source_version_id`

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
wall-clock. Locked stable numbers:

| Metric | Locked value |
|---|---|
| `active_total` | 919,446 |
| `distinct_study_accession` | 59,310 |
| `distinct_pmid` | 6,627 |
| `distinct_rsid` | 410,192 |
| `distinct_trait_name` | 16,162 |
| First-load wall-clock | < 5 minutes |

**Notes.** The `2026_05_16` release lands ~8.95 distinct study
accessions (GCSTs) per distinct publication (PMID): 59,310 / 6,627.
This is consistent with the modern GWAS Catalog practice of
splitting a single publication into multiple GCSTs by ancestry,
sex, cohort, and meta-analysis stage. As of 1 July 2024 the
catalog-level ratio was ~15.7 GCSTs per PMID (108,850 analyses /
6,921 publications, per the 2024 NAR paper); the lower ratio here
reflects that the loader reads `associations.tsv`, which carries
only studies with curated lead associations passing significance —
a subset of all GCSTs in the catalog. Drift detection: an upstream
release that shifts this ratio by more than ~2× in either
direction is worth investigating before re-locking numbers.

Re-run with `--force` to exercise the same-version supersession
path. Expected deltas: the same `active_total` lands under a fresh
`source_version_id` (a new id is allocated for the re-run);
`annotation_sources.current_source_version_id` flips to point at
that new id. Wall-clock stays inside the same envelope as the
first-load window — the version-pointer flip is O(1) so the
finding-009 ClinVar-scale UPDATE penalty does not apply.

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
  transaction holds the prior rowset (still resident under its
  older `source_version_id`) and the new rowset (being inserted
  under the freshly-allocated `source_version_id`) in the same
  WAL window, so the on-disk DuckDB file grows during a re-run.
  Free ~1-2 GB before running a refresh against the prior corpus.
  Prior versions remain in the table after the transaction
  commits — see finding-010 follow-up #14 for the open cleanup
  procedure.
* **Recovery after a partial-failure refresh.** Same shape as
  PharmGKB / CPIC / ClinVar: if any chunk insert or the closing
  pointer flip raises, the loader rolls the per-source insert
  (every chunk + any partially-applied `annotation_sources`
  UPSERT) back atomically and best-effort deletes the orphan
  `annotation_source_versions` row that `upsert_source_version`
  had already committed. A subsequent `refresh` starts clean. If
  the cleanup itself fails (the loader logs
  `gwas_catalog.cleanup.orphan_version_row_delete_failed`),
  manually `DELETE FROM annotation_source_versions WHERE
  source_db = 'gwas_catalog' AND source_version_id = <the
  affected id>` before retrying.

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
~5K scores at the current release, so a full refresh fits in a
single chunk; the chunked-insert code path is exercised
identically to the larger loaders. This sub-phase loads the
score-level metadata only; the per-score variant weights table
(`pgs_score_weights`) is Phase 6 work.

**Upstream URLs (three-step).**

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
3. `PGS_TRAIT_CATEGORY_URL` =
   `https://www.pgscatalog.org/rest/trait_category/all` -- REST
   endpoint returning JSON of the form
   `{"count": N, "results": [{"label": "Cardiovascular disease",
   "efotraits": [{"id": "EFO_xxx", ...}, ...]}, ...]}`. The bundle's
   EFO traits CSV does not carry a category column, so this third
   audited download supplies the dictionary that populates
   `pgs_catalog_scores.trait_category`. The endpoint returns 10
   categories totaling ~700 EFO traits at the verification date,
   well inside the REST default page size; the loader raises a
   loud-fail error if the response carries a `next` URL so a
   future growth past one page surfaces as a regression rather
   than silently truncated data.

`URL_VERIFIED_DATE` in
`backend/src/genome/annotate/loaders/pgs_catalog.py` records when
all three URLs were last confirmed to work; bump it on any URL
change.

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
~5K active rows (flipped to inactive) and the new ~5K active
rows in the same WAL window. End-to-end on a laptop is **under
30 seconds wall-clock** (the project-wide routine-refresh target
documented in CLAUDE.md). The bundle is small enough that the
finding-009 ClinVar-scale UPDATE+checkpoint cost is not a factor
here -- a same-version `--force` re-run completes well inside
the same target.

**Multi-file join contract.** The bundle contains four CSVs we
join on natural keys, plus a fifth REST payload that supplies
the trait_category column:

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
   term (~696 rows). Joins to the scores via the (possibly-
   truncated) trait EFO ID. The upstream EFO traits CSV does
   **not** ship a category column; this file is parsed only to
   drive the `orphan_trait_refs` counter (a score whose EFO ID
   is missing from the bundle's EFO list is the "orphan"
   signal). The schema's `trait_category` column flows through
   the trait_category REST endpoint instead (see #5 below).
4. `pgs_all_metadata_performance_metrics.csv` -- multiple rows
   per PGS, one per evaluation cohort / sample set. Joins to
   the scores via `Evaluated Score`. The per-cohort entries
   are collapsed into the schema's two scalar columns via the
   max reduction documented below.
5. `/rest/trait_category/all` (cached at `trait_categories.json`)
   -- the REST payload providing the `efo_id` → `category_label`
   dict. The bundle's EFO traits CSV does not carry a category
   column at the verified date, so this REST endpoint is the
   sole source of `trait_category`. 10 categories totaling
   ~700 EFO traits at the verified date. A score whose
   `trait_efo` is in this dict gets the category; otherwise
   `trait_category = NULL`. The lookup is independent of the
   bundle's EFO traits CSV -- a score whose EFO ID is missing
   from the bundle (counted as `orphan_trait_refs`) may still
   pick up a category from the REST payload, and vice versa.

Counters surfaced on the end-of-load summary:

* `orphan_publication_refs` -- a score's PGP ID is missing from
  the publications dict. The row still emits with
  `publication_pmid` / `publication_doi` / `publication_year`
  set to NULL.
* `orphan_trait_refs` -- a score's trait EFO ID is missing from
  the bundle's EFO traits CSV. The row still emits. The category
  lookup is independent of this counter -- a score whose EFO ID
  isn't in the bundle's EFO list may still receive a category if
  it's in the REST trait_category dict.
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
compare across releases. All `active_total` / `distinct_*` counts
are computed at the new `source_version_id` the loader just
landed — equivalently the rows scoped by the canonical
`JOIN annotation_sources s ON s.source_db = 'pgs_catalog' AND
s.current_source_version_id = ps.source_version_id` read pattern:

* `active_total` -- `COUNT(*)` at the new `source_version_id`
* `distinct_pgs_id` -- `COUNT(DISTINCT pgs_id)` at the new
  `source_version_id` (should equal `active_total` post-load if
  no upstream PGS duplicates)
* `distinct_trait_efo`
* `distinct_publication_pmid`
* `distinct_trait_category` -- populated from the
  `/rest/trait_category/all` REST payload (10 categories at
  the verified date). A value of 0 means the trait_category
  download or parse failed (or returned an empty results list)
  -- treat as a regression signal.
* `with_performance_auc` -- count where
  `performance_auc IS NOT NULL` at the new `source_version_id`
* `with_performance_or_per_sd` -- same shape

Plus parser stats: `rows_read_scores`, `rows_read_publications`,
`rows_read_traits`, `rows_read_performance`,
`rows_read_trait_categories`, `orphan_publication_refs`,
`orphan_trait_refs`, `scores_without_performance`,
`multi_cohort_performance`, `truncated_trait_efo`. A drift in
any of the active / distinct counts on a re-run against the
same release is a regression signal; verify against the
captured numbers in the 5.4 CHANGELOG entry once real-data
verification lands.

**Real-data verification commands.**

```
genome config set external_calls_enabled true
genome annotate refresh --source pgs_catalog
```

Capture from the `pgs_catalog.refresh.complete` structlog line:
`active_total`, `distinct_pgs_id`, `distinct_trait_efo`,
`distinct_publication_pmid`, `distinct_trait_category`,
`with_performance_auc`, `with_performance_or_per_sd`, plus
parser stats and wall-clock. Locked stable numbers:

| Metric | Locked value |
|---|---|
| `active_total` | 5,337 |
| `distinct_pgs_id` | 5,337 |
| `distinct_trait_efo` | 696 |
| `distinct_publication_pmid` | 590 |
| `distinct_trait_category` | 10 |
| `with_performance_auc` | 1,517 |
| `with_performance_or_per_sd` | 1,413 |
| `multi_cohort_performance` | 3,089 |
| First-load wall-clock | < 30 seconds |

Re-run with `--force` to exercise the same-version supersession
path. Expected deltas: the same `active_total` lands under a
fresh `source_version_id` (a new id is allocated each `--force`
re-run), and `annotation_sources.current_source_version_id` flips
to point at the new id. Wall-clock stays inside the 30 s target;
the version-pointer flip is O(1) so the finding-009 UPDATE +
checkpoint penalty does not apply anywhere on the supersession
path.

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
* **`PGS Catalog trait_category payload missing 'results'
  list`** / **`PGS Catalog trait_category endpoint returned a
  paginated response`** -- the `/rest/trait_category/all` API
  has shifted shape or grown past one page. Inspect the cached
  payload at
  `~/.cache/genome/annotations/pgs_catalog/trait_categories.json`
  and update `_validate_trait_category_payload` to match; if
  the issue is pagination, update `_parse_trait_categories` to
  follow the `next` URL.
* **`distinct_trait_category=0` in the structlog summary** --
  the trait_category dict came back empty. Either the REST
  endpoint returned an empty `results` list (verify with
  `curl https://www.pgscatalog.org/rest/trait_category/all`)
  or every score's `trait_efo` is missing from the dict (verify
  that the dict's EFO IDs overlap with the scores' EFO IDs). A
  drift away from the locked ~11-category range deserves a
  manual look at the upstream release notes.
* **Unexpected `orphan_publication_refs` spike** -- the
  publications CSV has dropped entries that the scores CSV
  still references. The drift is upstream; if the spike
  persists across releases, contact PGS Catalog support.
* **Unexpected `scores_without_performance` spike** -- a
  release shipped scores without paired performance rows.
  Probably benign; verify against the upstream release notes.
* **Disk space failure mid-supersession** -- the supersession
  transaction holds the prior rowset (still resident under its
  older `source_version_id`) and the new rowset (being inserted
  under the freshly-allocated `source_version_id`) in the same
  WAL window, so the on-disk DuckDB file grows during a re-run.
  PGS Catalog at ~5K rows is much smaller than the ClinVar
  case but free ~100 MB before running a refresh against the
  prior corpus. Prior versions remain in the table after the
  transaction commits — see finding-010 follow-up #14 for the
  open cleanup procedure.
* **Recovery after a partial-failure refresh.** Same shape as
  PharmGKB / CPIC / ClinVar / GWAS Catalog: if the bulk insert
  or the closing pointer flip raises, the loader rolls the
  per-source insert (every chunk + any partially-applied
  `annotation_sources` UPSERT) back atomically and best-effort
  deletes the orphan `annotation_source_versions` row that
  `upsert_source_version` had already committed. A subsequent
  `refresh` starts clean. If the cleanup itself fails (the
  loader logs
  `pgs_catalog.cleanup.orphan_version_row_delete_failed`),
  manually `DELETE FROM annotation_source_versions WHERE
  source_db = 'pgs_catalog' AND source_version_id = <the
  affected id>` before retrying.

### gnomAD (sub-phase 5.5)

**What's loaded.** gnomAD v4.1.1 per-chromosome sites-only VCFs
(exomes + genomes, GRCh38, GCS-hosted), filtered to the three-way
union of distinct `(chrom, pos_grch38)` across `variants_master` ∪
the active ClinVar release ∪ the active GWAS Catalog release.
Autosomes 1-22 + X. Y and MT are intentionally skipped — gnomAD v4
does not ship high-confidence allele frequencies for those
chromosomes in the public per-chromosome VCFs. The loader streams
the remote VCFs via cyvcf2 remote tabix queries and chunk-loads the
matching rows into `gnomad_frequencies` via PyArrow Table
registration + `INSERT ... SELECT` at `DEFAULT_BATCH_SIZE = 50,000`
rows per chunk. Supersession follows the version-pointer pattern
(finding-010): per-chromosome content lands under a freshly-
allocated `source_version_id` and the `annotation_sources` pointer
flips to the new id only when every supported chromosome completes
successfully. See `backend/src/genome/annotate/loaders/gnomad.py`
for the full per-source narrative; the loader's docstring carries
the URL constants, the cyvcf2 streaming contract, the per-record
projection, the htslib HTTP/2 retry mechanism, and the resume /
partial-run semantics.

**Filter shape.** CLAUDE.md "Things never to do" #3 mandates the
broader `(user ∪ ClinVar ∪ GWAS ∪ PGS)` intersection, but PGS
per-variant weights do not yet exist in the DB at PR-B time (they
land in Phase 6 as `pgs_score_weights`). A Phase 6 follow-up gated on
`pgs_score_weights` will extend the active gnomAD source-version's
coverage to PGS-component variants without a version bump. See
[finding-011](../findings/finding-011-gnomad-three-way-intersection.md).

**HTTP/2 retry behavior.** Remote-tabix iteration against gnomAD's
GCS bucket trips libcurl `CURLE_HTTP2` (error 16) framing errors
during BGZF block reads on roughly one in 200,000 range requests at
default settings. The corruption silently zeros the cyvcf2 iterator
and corrupts htslib's read-offset state; the only recovery is to
close + reopen the VCF handle. The loader detects the corruption by
capturing fd-2 stderr writes, scans for htslib error tokens after
each region, and on a hit closes + reopens the VCF and retries.
Bounded by `MAX_REMOTE_REGION_ATTEMPTS = 5` with per-chromosome
`seen_keys` dedup making record re-yields idempotent. At the locked
`--coalesce-distance 50000` default, ~2 reopens per chromosome is
typical (4 on chromosome 1, ~0 on chr2-X). See
[finding-012](../findings/finding-012-coalesce-distance-and-http2-reliability.md)
for the coalesce-distance choice rationale and the 1000 bp →
50,000 bp default bump that took the loader from > 24 h projected
wall-clock to 14.6 h real wall-clock.

**Real-data verification commands.**

```
genome config set external_calls_enabled true
genome annotate refresh --source gnomad
```

Capture from the `gnomad.refresh.complete` structlog line:
`rows_loaded`, `filter_set_composition`,
`distinct_variants_per_chrom`, `match_rate`,
`af_buckets_user_overlap`, `mean_af_user_overlap`,
`pop_af_presence`, plus `chromosomes_succeeded` /
`chromosomes_failed` and wall-clock. Locked stable numbers
(gnomAD v4.1.1, locked 2026-05-22):

| Metric | Locked value |
|---|---|
| `rows_loaded` | 7,275,664 |
| `match_rate` (vs `variants_master`) | 0.988 |
| `mean_af_user_overlap` | 0.1766 |
| First-load wall-clock | ~14.6 h (`--coalesce-distance 50000`) |

Filter set composition (`(user ∪ clinvar ∪ gwas)`, distinct
`(chrom, pos_grch38)`):

| Component | Distinct positions |
|---|---|
| `user` (`variants_master`) | 936,912 |
| `clinvar` | 3,910,450 |
| `gwas` | 409,213 |
| `union_total` | 5,129,731 |

AF buckets on the user-variant overlap (1,272,116 variants in
`gnomad_frequencies` that share a `(chrom, pos_grch38)` with
`variants_master`):

| Bucket | Count |
|---|---|
| `lt_0.001` | 399,321 |
| `0.001_to_0.01` | 64,175 |
| `0.01_to_0.05` | 192,134 |
| `0.05_to_0.5` | 447,860 |
| `gt_0.5` | 168,626 |

Per-population AF presence (rows where `af_<pop> IS NOT NULL`):

| Population | Rows with non-null AF |
|---|---|
| `afr` | 7,209,296 |
| `ami` | 1,876,597 |
| `amr` | 7,194,799 |
| `asj` | 7,194,554 |
| `eas` | 7,205,605 |
| `fin` | 7,230,706 |
| `mid` | 7,188,509 |
| `nfe` | 7,257,386 |
| `sas` | 7,199,524 |
| `oth` | 7,234,074 |

The `ami` (Amish) population is sparser than the other nine by
design: gnomAD v4's Amish subset is small (the public exomes VCF
does not carry `AF_ami` at most sites; `ami` is populated mostly
from the genomes VCF). The expected per-pop count is ~1.9 M vs
~7.2 M for the other populations; a future release that drifts
`ami` outside the 1.5 M-2.3 M envelope is the drift-detection
signal to investigate before re-locking.

Distinct variants per chromosome (also the per-chrom row count by
construction — one row per `(chrom, pos, ref, alt)`):

| Chromosome | Distinct variants |
|---|---|
| `chr1` | 649,575 |
| `chr2` | 588,464 |
| `chr3` | 412,190 |
| `chr4` | 309,152 |
| `chr5` | 356,249 |
| `chr6` | 383,622 |
| `chr7` | 364,034 |
| `chr8` | 290,439 |
| `chr9` | 334,343 |
| `chr10` | 291,269 |
| `chr11` | 415,439 |
| `chr12` | 336,981 |
| `chr13` | 148,493 |
| `chr14` | 230,197 |
| `chr15` | 246,876 |
| `chr16` | 362,352 |
| `chr17` | 373,695 |
| `chr18` | 138,574 |
| `chr19` | 414,036 |
| `chr20` | 177,301 |
| `chr21` | 89,053 |
| `chr22` | 154,080 |
| `chrX` | 209,250 |

A drift in any of the locked counts on a re-run against the same
gnomAD release is a regression signal; verify against this table
before re-locking. The match-rate at 0.988 (~99% of user variants
have a gnomAD AF row) is the headline value-add — a `variants_master`
position without a gnomAD AF after a clean refresh is almost
always a chip-only ancestry-informative marker that gnomAD has not
yet observed.

**Troubleshooting.**

* **`ExternalCallsDisabledError`** —
  `user_preferences.external_calls_enabled` is `false`. Run
  `genome config set external_calls_enabled true`. The blocked
  attempt is still recorded in `audit_log` for review; the
  pre-flight HEAD is the loader's first audited call, so a
  disabled switch surfaces before any cyvcf2 remote-VCF open is
  attempted.
* **`GnomadLibcurlMissingError`** — cyvcf2's bundled htslib was
  built without libcurl support, or the gnomAD GCS bucket is
  unreachable from this host. Rebuild htslib (and cyvcf2 against
  it) with libcurl enabled; the README's "Prerequisites" section
  carries the exact build commands. The pre-flight probe opens a
  known-tiny tabix range against the chr22 exomes VCF, so a
  failure here points at the toolchain rather than at upstream
  data.
* **`GnomadRemoteIterationError`** — the same tabix region tripped
  the htslib HTTP/2 framing detector
  `MAX_REMOTE_REGION_ATTEMPTS = 5` times in a row. Something more
  durable than a transient HTTP/2 blip is at play (URL rotation,
  network outage, server-side range rejection). Inspect the
  loader's structlog `gnomad.chrom.htslib_recover` events for the
  failing region, then verify the URL with `curl -I -L
  https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/vcf/exomes/gnomad.exomes.v4.1.sites.chr<N>.vcf.bgz`.
* **`match_rate` drops below ~0.95** — either upstream gnomAD has
  retired positions the prior release carried (uncommon but
  documented at gnomAD release time) or the filter set is
  contaminated with sentinel rows. The PR-B fix tightened the
  `pos_grch38 > 0` guard on the user / ClinVar / GWAS subqueries
  uniformly to defend against any future sentinel-emitting
  upstream source (see [finding-013](../findings/finding-013-synthetic-fixture-realism.md));
  a drop on a new run warrants an inspection of `_build_filter_set`'s
  composition counts in the structlog summary first.
* **Resume after a partial-failure run.** Re-invoke with
  `genome annotate refresh --source gnomad --resume`. The loader
  reads the in-flight `source_version_id` (an
  `annotation_source_versions` row exists but the
  `annotation_sources` pointer doesn't yet name it), skips the
  chromosomes already populated under that id, and runs the
  remainder. The pointer flips when the full
  `SUPPORTED_CHROMS` set is covered.
* **Recovery after a fully-failed first run.** The
  `source_version_id` row stays in `annotation_source_versions`
  with `record_count` reflecting whatever landed. A subsequent
  `--resume` continues against the same id;
  `--force` allocates a fresh id and starts over (use this if
  the in-flight row's coverage is so partial that resume's
  per-chromosome overhead outweighs starting fresh).

### dbSNP (sub-phase 5.6)

**What's loaded.** NCBI's single multi-chromosome dbSNP GRCh38 VCF
(`GCF_000001405.40.gz`, build 157, ~29.5 GB, bgzipped + tabix-indexed),
filtered to the user's own variant positions — distinct `(chrom,
pos_grch38)` in `variants_master`. Chromosomes 1-22 + X + **Y + MT**
(unlike gnomAD, dbSNP ships rsIDs for every canonical chromosome, and the
user's 23andMe export carries Y + MT positions worth annotating). The
loader streams the remote VCF via cyvcf2 remote tabix and chunk-loads
matching rows into `dbsnp_annotations` via PyArrow Table registration +
`INSERT ... SELECT` at `DEFAULT_BATCH_SIZE = 50,000`. Supersession follows
the version-pointer pattern (finding-010): per-chromosome content lands
under a freshly-allocated `source_version_id`, and the
`annotation_sources` pointer for `dbsnp` flips only when every supported
chromosome completes. `variant_aliases` (the second `dbsnp`-governed
table) is **not** populated in PR B — it pairs with the finding-005 #4
tier-2 backfill.

**Source contracts (ratified by the finding-013 gate, build 157).**

* `rsid` reads from the VCF **ID column** (`record.ID`), never `INFO/RS` —
  build 156+ emits `RS` values exceeding 2³¹ that htslib sets to missing
  (`[W::vcf_parse_info] Extreme INFO/RS value encountered and set to
  missing`). Confirmed live at `NC_012920.1:11` during the MT smoke below.
* `#CHROM` uses RefSeq accessions (`NC_000001.11` … `NC_012920.1`), mapped
  to canonical chroms by the stable GRCh38.p14 (GCF_000001405.40) assembly
  definition, validated against the VCF header `##contig` set at pre-flight
  (a missing/renamed accession raises `DbsnpSourceContigError`).
* Multi-allelic sites are kept as a `VARCHAR[]` array (`alt_alleles`), not
  split. `variant_class` ← `INFO/VC` (`SNV`→`snv`, `MNV`→`mnv`,
  `INS`/`DEL`/`INDEL`/`DIV`→`in-del`). `gene_symbols` ← `INFO/GENEINFO`.
  `is_clinical` ← presence of `INFO/CLNSIG`. `functional_class` and
  `pos_grch37` are left NULL in PR B — build 157 carries only coarse legacy
  function-class flags, not a VEP-grade consequence; populated from VEP in
  Phase 6.

**Filter shape.** `user_only` (distinct `variants_master` positions), not
gnomAD's three-way `(user ∪ ClinVar ∪ GWAS)` intersection. dbSNP annotates
the user's variants (rsID canonicalisation, REF/ALT recovery, tier-2
matching), all of which read `variants_master`; loading dbSNP at
ClinVar/GWAS/PGS positions the user doesn't carry would add rows nothing
reads. The ClinVar/GWAS/PGS legs are deferred — the PGS leg to a Phase 6
follow-up gated on `pgs_score_weights`, mirroring the gnomAD PGS extension.
See
[finding-016](../findings/finding-016-dbsnp-user-only-filter.md).

**HTTP/2 retry behavior.** dbSNP shares the gnomAD remote-tabix machinery,
extracted to `genome.annotate.remote_tabix` per finding-012 #11. NCBI's
FTP host serves the same HTTP/2 reality as gnomAD's GCS bucket; the
`_StderrTap` corruption detector, the `iter_remote_vcf_regions`
open→detect→reopen→retry generator (bounded by
`MAX_REMOTE_REGION_ATTEMPTS = 5`), and the `--coalesce-distance 50000`
default (finding-012 #10) all apply. Events are emitted under the `dbsnp.*`
prefix (`dbsnp.remote_open`, `dbsnp.chrom.htslib_recover`,
`dbsnp.chrom.htslib_recover_summary`). Because dbSNP is one file queried
per chromosome, the (~2.5 MB) `.tbi` is refetched per chrom — acceptable
for a gated long-running op; an open-once optimization is a noted follow-up
if real-data verification shows it dominates wall-clock.

**Real-data verification commands.**

```
genome config set external_calls_enabled true
genome annotate refresh --source dbsnp
```

Capture from the `dbsnp.refresh.complete` structlog line: `rows_loaded`,
`filter_set_composition`, `distinct_variants_per_chrom`, `match_rate` (vs
`variants_master`), `variant_class_distribution`, `gene_symbols_present`,
`multiallelic_rows`, `is_clinical_rows`, plus `chromosomes_succeeded` /
`chromosomes_failed` and wall-clock.

Filter-set composition (`user_only`, distinct `(chrom, pos_grch38)`;
verified 2026-05-25 against the post-rebuild chip-only `variants_master`):

| Component | Distinct positions |
|---|---|
| `user` (`variants_master`) | 942,424 |
| `union_total` | 942,424 |

**Locked stable numbers (full genome, dbSNP build 157, locked 2026-05-25).**
First complete refresh — `--resume` continuing the MT+Y subset version
through chroms 1-22 + X, then flipping the `dbsnp` pointer (all 25
chromosomes under one `source_version_id`):

| Metric | Locked value |
|---|---|
| `rows_loaded` | 1,002,769 |
| `match_rate` (vs `variants_master`) | 0.9977 (940,210 / 942,424 user positions carry ≥ 1 dbSNP rsID) |
| `variant_class_distribution` | `{snv: 940145, in-del: 56040, mnv: 6584}` |
| `multiallelic_rows` (`len(alt_alleles) > 1`) | 435,064 |
| `is_clinical_rows` (`CLNSIG` present) | 46,935 |
| `gene_symbols_present` | 623,616 |
| htslib HTTP/2 reopens | 0 (at `--coalesce-distance 50000`) |
| First-load wall-clock | ~101 min (6,070 s) |

Per chromosome — user positions queried (the filter set) and dbSNP rows
loaded (`distinct_variants_per_chrom`; rows exceed positions on the
autosomes where a position carries multiple dbSNP records — an SNV plus an
indel — and fall short on Y/MT where dbSNP lacks an rsID for some chip
probes):

| Chrom | User pos | Rows | Chrom | User pos | Rows |
|---|---|---|---|---|---|
| 1 | 73,013 | 77,396 | 14 | 29,823 | 31,739 |
| 2 | 75,915 | 80,819 | 15 | 28,360 | 30,166 |
| 3 | 62,886 | 66,865 | 16 | 30,383 | 32,349 |
| 4 | 56,686 | 60,294 | 17 | 27,915 | 30,018 |
| 5 | 55,733 | 59,193 | 18 | 27,247 | 28,976 |
| 6 | 62,300 | 66,165 | 19 | 20,094 | 21,828 |
| 7 | 50,173 | 53,419 | 20 | 23,211 | 24,667 |
| 8 | 47,982 | 51,152 | 21 | 13,085 | 13,970 |
| 9 | 41,132 | 43,856 | 22 | 13,328 | 14,346 |
| 10 | 47,139 | 50,275 | X | 26,965 | 28,956 |
| 11 | 45,612 | 48,553 | Y | 3,177 | 3,117 |
| 12 | 44,337 | 47,299 | MT | 2,335 | 1,379 |
| 13 | 33,593 | 35,972 | | | |

The `match_rate` at 0.9977 (~99.8 % of user positions carry a dbSNP rsID)
is the headline value-add — a `variants_master` position without a dbSNP
row after a clean refresh is almost always a chip probe at a site dbSNP
has not catalogued (concentrated on MT). A drift in `rows_loaded`, the
per-chrom counts, or the composition on a re-run against the same build is
a regression signal.

This run confirmed the full live path against NCBI: the audited HEAD per
chrom, the pre-flight `##contig` validation against all 25 accessions,
accession querying for the Y/MT-only chroms (which gnomAD skips), the
`INFO/RS`-overflow → `record.ID` rsid path (htslib's extreme-RS warning is
emitted while the row still lands from the ID column), multi-allelic
`VARCHAR[]` arrays (incl. 3-allele sites), `CLNSIG`→`is_clinical`,
`GENEINFO`→`gene_symbols`, `functional_class` / `pos_grch37` NULL, and
**zero htslib HTTP/2 reopens** across the full genome at the 50 kb coalesce
default. The fast subset smoke `genome annotate refresh --source dbsnp
--chromosomes MT,Y` (9.4 s, pointer unflipped) lands MT = 1,379 + Y = 3,117
rows and is the quick reachability check.

**Troubleshooting.**

* **`ExternalCallsDisabledError`** — `external_calls_enabled` is `false`.
  Run `genome config set external_calls_enabled true`. The blocked attempt
  is still audit-logged; the pre-flight HEAD is the loader's first audited
  call.
* **`RemoteTabixLibcurlMissingError`** — cyvcf2's bundled htslib was built
  without libcurl support, or NCBI's host is unreachable from this host.
  Rebuild htslib (and cyvcf2 against it) with libcurl enabled; see the
  README "Prerequisites". The pre-flight open fetches the VCF header, so a
  failure here points at the toolchain rather than upstream data.
* **`DbsnpSourceContigError`** — the VCF header `##contig` set is missing a
  canonical RefSeq accession the loader expects. NCBI may have bumped the
  GRCh38 assembly patch and renamed an accession; update
  `_CHROM_TO_ACCESSION` in `loaders/dbsnp.py` to match.
* **`RemoteTabixIterationError`** — a tabix region tripped the htslib HTTP/2
  framing detector `MAX_REMOTE_REGION_ATTEMPTS = 5` times in a row. Inspect
  the `dbsnp.chrom.htslib_recover` events for the failing region and verify
  the URL with `curl -I https://ftp.ncbi.nlm.nih.gov/snp/latest_release/VCF/GCF_000001405.40.gz`.
* **Resume / fresh start.** `genome annotate refresh --source dbsnp
  --resume` continues the in-flight `source_version_id` (skips populated
  chromosomes, flips when the full `SUPPORTED_CHROMS` set lands); `--force`
  allocates a fresh id and starts over.

### Variant Annotations Index Refresh (sub-phase 5.7)

**What's built.** `variant_annotations_index` is the denormalized
one-row-per-variant rollup that `variant_full_v` and the SNP detail page read
from. Sub-phase 5.7 (`genome.annotate.index_refresh`) joins the four
variant-linkable sources — ClinVar, GWAS Catalog, gnomAD, PharmGKB — into one
sparse row per variant that carries at least one annotation. PGS Catalog
(score-level) and dbSNP (rsID canonicalization via the still-empty
`variant_aliases`) are loaded but contribute no rollup column. The four VEP
columns (`most_severe_consequence`, `impact`, `cadd_phred`,
`alphamissense_class`) and `is_acmg_sf` ship NULL — Phase 6's VEP runner / ACMG
SF detection backfill them via a later rollup refresh (finding-017).

**Command.**

```
genome annotate refresh-index
```

No external calls. `--force` is accepted for symmetry but the build is
unconditional (a documented no-op). The builder is a pure in-engine
`INSERT … SELECT`: it `DELETE`s the prior index and re-inserts inside one
transaction, so a concurrent reader sees either the entire old index or the
entire new one — never a torn mix (CLAUDE.md decision #7). `variant_id` is the
table's PRIMARY KEY, so retained-superseded rows are structurally impossible;
the index is not a registered source and has no `annotation_sources` row of its
own.

**Join model.** Each source is filtered to its *currently-active* version via
the `annotation_sources` pointer (the same shape as `user_pgx_variants_v`), so a
superseded release never leaks in. The join key differs by source:

| Source | Key | Grain |
|---|---|---|
| ClinVar | full coords `(chrom, pos, ref, alt)` | allele-specific (≤1:1 vs the `variants_master` UNIQUE key) |
| gnomAD | full coords | allele-specific |
| GWAS Catalog | `rsid` | **locus-level** |
| PharmGKB | `rsid` | **locus-level** |

Two reader-facing caveats the DDL comments don't capture:

1. **`is_curated` is ClinVar ∪ PharmGKB only.** The DDL comment (group_2 line
   494) reads "present in any curated source (ClinVar, PharmGKB, CPIC)", but
   CPIC is gene+drug grain with no rsid/coord/variant_id (group_2 283–305), so
   it cannot contribute at the variant level until a gene→variant mapping lands
   (Phase 6/7). 5.7 computes `is_curated` from ClinVar and PharmGKB only; the
   implementation is narrower than the comment by design.
2. **GWAS traits attach at rsid grain.** A GWAS association is locus-level
   evidence, not allele-level. When two `variants_master` rows share an rsid (a
   multi-allelic split), both carry the same trait — so a "total user variants
   associated with trait X" aggregation across the rollup over-counts at
   multi-allelic loci. Read GWAS (and PharmGKB) columns as locus-level.

**Provenance.** Every row stamps `refresh_versions` — a JSON snapshot of the
`{source_db: version}` map resolved from the pointers at build time, identical
on every row — and `last_refreshed` (the build timestamp).

**Runtime.** A pure vectorized DuckDB scan over the user variants × the loaded
source tables. Target < 30 s (CLAUDE.md performance contract); the real-data
first run completed in ~2.2 s. Per-step structlog events
(`index_refresh.versions_resolved` → `…cleared` → `…inserted` →
`supersession_commit_*` / `…checkpoint_*` → `index_refresh.complete`) make the
window observable. If a future run overshoots 60 s, investigate a missing index,
an accidental cross-product, or a fan-out join before restructuring SQL.

**Drift identifiers (first real-data run; see finding-018).** Against the user's
loaded corpus (ClinVar `2026_05_17`, gnomAD `4.1.1`, GWAS `2026_05_16`, PharmGKB
`2025_07_05`):

| Metric | Value |
|---|---|
| `row_count` | 159,658 |
| `gnomad_matches` | 101,501 |
| `clinvar_matches` | 2,559 |
| `gwas_matches` | 66,726 |
| `pharmgkb_matches` | 1,737 |
| `curated_count` | 4,198 |
| `is_rare` TRUE | 848 |
| `is_ultrarare` TRUE | 421 |

These are **allele-match-gated**, far below the position-level gnomAD↔user
overlap (~1.27M), because 78.3% of `variants_master` is hom-ref (`ref==alt`,
finding-005 #6) and ~50% of the genuine `ref≠alt` variants match gnomAD only
with REF/ALT swapped (un-canonicalized REF/ALT, finding-005 #1). This is
expected, not a regression: re-running `refresh-index` after the post-5.7
canonical-REF/ALT backfill is expected to materially raise the coord-keyed
counts; capture and re-lock then. The rsid-keyed counts (GWAS, PharmGKB) are
unaffected by REF/ALT and are stable now.

**Troubleshooting.**

* **Every annotation column is NULL in `variant_full_v`.** The index has never
  been built (it ships empty from `genome init`). Run `genome annotate
  refresh-index`.
* **`gnomad_matches` / `clinvar_matches` look "too low".** Expected at 5.7 — see
  the drift-identifier note above and finding-018. The coord-join is
  allele-gated; the lift is the post-5.7 canonical-REF/ALT backfill, not a 5.7
  fix.
* **`gene_variant_summary_v` returns 0 pathogenic counts.** The `genes`
  dictionary table is empty (deferred to Phase 7), so the view's
  `genes ⨝ variants_master` join has no left side. Expected until Phase 7 loads
  `genes`; unrelated to the index build.
* **A source's rows are missing after a refresh.** Confirm its
  `annotation_sources` pointer names the version you loaded (`genome annotate
  status`); the builder reads only current-pointer rows.

### variant_aliases backfill (post-5.7)

**What's loaded.** NCBI's dbSNP rs-merge archive
(`RsMergeArch.bcp.gz`, ~146 MB gzipped, frozen 2018-02-07 / build ~151),
filtered to merges touching the user's own rsIDs, into `variant_aliases` as a
canonical `alias_rsid (old) → current_rsid (survivor)` map with
`alias_type = 'merged'`. It fills the table the dbSNP loader (5.6) left empty
(finding-016 #8) and unblocks the deferred tier-2 rsID merge matching
(finding-005 #4). The dbSNP VCF carries no merge history, so this is a separate
download — see [finding-019](../findings/finding-019-variant-aliases-backfill.md)
for the source choice, the both-sided filter, and the staleness rationale.

**Command.**

```
genome annotate refresh-aliases          # dbSNP must already be loaded
genome annotate refresh-aliases --force  # re-download + rebuild for the current epoch
```

**Supersession (same dbSNP epoch, no flip).** `variant_aliases` shares the
`dbsnp` version pointer with `dbsnp_annotations`. The command reads the current
dbSNP `source_version_id` and writes alias rows **under that same id** — it
allocates no new version, does **not** flip the pointer, and does **not** mutate
`annotation_source_versions.record_count` (that belongs to `dbsnp_annotations`).
It therefore requires dbSNP to be loaded first and fails fast (exit 2) with a
clear message otherwise. **Re-run `refresh-aliases` after any future
`refresh --source dbsnp`** that flips the pointer, or the new dbSNP epoch will
carry no aliases. A `--force` re-run does `DELETE` + re-`INSERT` under the same
id inside one transaction (atomic; readers never see a torn map); a re-run
without `--force` short-circuits when the current epoch is already populated
(no re-download).

**Filter + dedup.** Kept when either `rsHigh` (merged-away) or `rsCurrent`
(survivor) is present in `variants_master.rsid` — both directions, because
tier-2 resolves a user's stale rsID *and* an external source's stale rsID
against the user's current rsID. Self-merges, malformed/non-numeric rows, and
duplicate `alias_rsid`s are dropped.

**Runtime.** The ~80M-row scan runs in Python (`csv.reader`) with PyArrow bulk
insert of the small matched set — single-digit minutes, with a
`variant_aliases.scan.progress` line every 5M source rows. This is a named,
gated backfill, deliberately outside the 30 s routine-refresh target.

**Drift identifiers (lock on first real-data run).** Capture from the
`variant_aliases.refresh.complete` structlog line: `rows_loaded`,
`distinct_alias_rsid`, `distinct_current_rsid`, `user_old_rsid_hits` (the
tier-2-lift proxy — user variants carrying a now-mappable stale rsID),
`user_current_rsid_hits`. Verify with:

```sql
SELECT COUNT(*), COUNT(DISTINCT alias_rsid), COUNT(DISTINCT current_rsid)
  FROM variant_aliases va
  JOIN annotation_sources s
    ON s.source_db = 'dbsnp' AND s.current_source_version_id = va.source_version_id;
SELECT COUNT(DISTINCT vm.rsid) AS user_old_rsid_hits
  FROM variants_master vm JOIN variant_aliases va ON va.alias_rsid = vm.rsid;
```

**Troubleshooting.**

* **`exit code 2: load the dbSNP VCF first`** — no active dbSNP pointer. Run
  `genome annotate refresh --source dbsnp` first.
* **`ExternalCallsDisabledError`** — `external_calls_enabled` is `false`. Run
  `genome config set external_calls_enabled true`. The blocked attempt is still
  recorded in `audit_log` (intent + blocked pair for `dbsnp_rsmergearch`).
* **All aliases vanished from reader joins after a dbSNP refresh.** Expected —
  the pointer moved to a new epoch. Re-run `genome annotate refresh-aliases`.

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
