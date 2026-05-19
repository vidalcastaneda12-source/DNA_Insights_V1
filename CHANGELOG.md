# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Schema
- Added `gnomad_frequencies.af_mid` (`DOUBLE`, nullable) so the upcoming
  sub-phase 5.5 gnomAD loader can store Middle Eastern allele frequencies
  as a distinct population. gnomAD v4 has ten inferred ancestry groups;
  the schema previously had columns for nine. Schema markdown
  (`docs/schemas/schema_group_2_reference_annotations.md`) and the
  extracted DDL (`ddl/group_2_annotations.sql`) are updated together;
  existing local DuckDB files need a rebuild
  (`rm -rf data/ && uv run genome init`) per the CLAUDE.md schema-change
  convention, followed by a re-ingest of 23andMe + Ancestry and a re-run
  of every shipped Phase 5 loader.

### Changed
- **Pre-5.5 — documentation reconcile for the version-pointer
  supersession refactor (PR #43).** Documentation-only follow-up to
  PR #43, which replaced per-row `is_active` / `superseded_by` flips
  on the five Phase-5 annotation tables (ClinVar, GWAS Catalog,
  PharmGKB, CPIC, PGS Catalog) with a single-row pointer in a new
  `annotation_sources` table. New
  [`docs/findings/finding-010-version-pointer-supersession-pattern.md`](docs/findings/finding-010-version-pointer-supersession-pattern.md)
  carries the design rationale, the readers-side reasoning, and the
  follow-up items that survive into the new pattern. CLAUDE.md
  decision #7 is reworded to describe the dual mechanism — version-
  pointer for source-grain supersession, per-row for row-grain
  (`genotype_calls`, future insights / evidence / derived rows).
  `docs/schemas/schema_group_2_reference_annotations.md` is rewritten
  to match the post-PR-#43 DDL (`is_active` / `superseded_by` columns
  removed from the five tables; new `annotation_sources` section
  added; `annotation_source_versions` updated to reflect that
  identity is `source_version_id` alone). Schema groups 1, 3, and 4
  pick up dual-mechanism narrative updates; the existing per-row
  DDL on `genotype_calls` (group 1) and the aspirational
  per-row DDL on derived / insight / evidence tables (groups 3 / 4)
  is unchanged — those tables supersede at the row grain, not the
  source grain, and the version-pointer pattern is documented as the
  convention for new source-grain supersedable tables only.
  `docs/runbooks/annotations.md` reconciled to the new mechanism
  (refresh procedure list, per-loader Force-mode semantics, drift-
  identifier query shapes, disk/recovery notes). `finding-009`
  amended in place with item #17 documenting the resolution and the
  measured ClinVar `--force` refresh time (4 m 56 s end-to-end,
  down from 1,699 s / ~28 min on the per-row path); items #1-#16
  are preserved as the historical audit trail. No code changes; no
  schema rebuild; no re-ingest. (PR #43 follow-up)
- **Pre-5.5 — unified supersession deactivate path + finding-009
  cost-model correction.** Real-data verification of PR #41 against
  the existing ClinVar `2026_05_10` release showed the
  `supersession_update_start` / `_complete` events never fired on
  `--force` re-runs. Root cause: every Phase-5 loader's
  `_deactivate_for_refresh` branched on `force` and ran an inline
  `UPDATE ... WHERE is_active = TRUE` on the force path, bypassing
  the shared helper that emits the events. This PR unifies the path
  in all five loaders (ClinVar, PharmGKB, CPIC, GWAS Catalog, PGS
  Catalog):
  - `genome.annotate.supersession.deactivate_prior_versions` gained
    a keyword-only `force_all_active: bool = False` parameter.
    Default mode (existing behavior) deactivates rows with
    `source_version_id < new_source_version_id AND is_active =
    TRUE`. Force mode deactivates rows with `is_active = TRUE` (no
    version filter), preserving the same-version `--force`
    semantics. Both modes emit `supersession_update_start` /
    `_complete` with `force_all_active` in the payload.
  - Each loader's `_deactivate_for_refresh` is now a one-line
    pass-through that calls
    `deactivate_prior_versions(..., force_all_active=force)`. The
    wrapper stays so each loader has a clean per-source seam.
  - `docs/findings/finding-009` items #4, #5, #13, #14 amended in
    place with correction notes; new items #15 / #16 carry the
    corrected per-phase decomposition (UPDATE ~17-19 min, COMMIT
    ~270s, explicit CHECKPOINT ~1-6 ms — the explicit CHECKPOINT
    measures a no-op because COMMIT already flushed synchronously)
    and the coverage-gap discovery. The supersession atomicity
    contract (CLAUDE.md #7) is unchanged: the UPDATE remains one
    statement in one transaction with the chunked INSERT. No schema
    changes; no `rm -rf data/` rebuild required.

### Added
- **Pre-5.5 — supersession observability + `--skip-if-same-version`.**
  Addresses finding-009 #9, #11, and #14 ahead of sub-phase 5.5
  (gnomAD filtered), which will exercise the supersession path
  against a row count larger than ClinVar's 9M. The supersession
  atomicity contract (CLAUDE.md decision #7) is unchanged: the
  UPDATE remains one statement in one transaction. The change is
  observability-only plus an opt-in CLI short-circuit:
  - `genome.annotate.supersession.deactivate_prior_versions` now
    emits `supersession_update_start` (with `prior_active_rows`)
    and `supersession_update_complete` (with `rows_deactivated`
    and `duration_ms`) around the UPDATE statement.
  - New helper `commit_and_checkpoint(conn, source_name=...)`
    issues `conn.commit()` followed by an explicit `CHECKPOINT`,
    bracketing each phase with start/complete structlog events
    that report `duration_ms`. Per finding-009, the post-COMMIT
    flush dominated the 1,699s same-version ClinVar refresh and
    was previously opaque; the explicit CHECKPOINT moves the
    flush inside the measured wall-clock window without changing
    total cost.
  - All five Phase-5 loaders (ClinVar, GWAS Catalog, PharmGKB,
    CPIC, PGS Catalog) thread `source_name=SOURCE_DB` through
    their `_deactivate_for_refresh` helpers and call
    `commit_and_checkpoint(conn, source_name=SOURCE_DB)` in place
    of the bare `conn.commit()` that previously closed each
    supersession transaction.
  - New `--skip-if-same-version` flag on `genome annotate refresh`.
    Off by default. When set, each loader (via the new shared
    `maybe_skip_same_version` helper) queries
    `annotation_source_versions` after download, and if the
    currently-active row matches `(source_db, version,
    source_file_hash)` emits `supersession_skipped_same_version`
    and returns a `RefreshResult` with `was_already_current=True`
    instead of running the supersession path. The match key
    includes the file hash so a same-label upstream regeneration
    still triggers a re-load. Existing `--force` invocations
    behave identically when the flag is not passed.
  - Registry's `RefreshFn` type and every loader's `refresh`
    signature gain a `skip_if_same_version: bool = False` second
    parameter; the CLI passes the flag through positionally.
  - 9 new tests in `backend/tests/test_annotate_supersession.py`
    cover the per-phase events (update start/complete, commit
    start/complete, checkpoint start/complete), the explicit
    CHECKPOINT (asserted via `MagicMock`), and every branch of
    `maybe_skip_same_version` (flag off, matching active row,
    no active row, version mismatch, hash mismatch, superseded
    row ignored). 3 new tests in
    `backend/tests/test_annotate_cli.py` verify the CLI surface:
    `--skip-if-same-version` appears in `--help`, the flag value
    reaches the loader, and omitting the flag passes `False`.
  - No schema changes; no `rm -rf data/` rebuild required. (PR #41)

### Fixed
- **Sub-phase 5.4 follow-up — `distinct_trait_category=0` bug.**
  Real-data verification of the original 5.4 loader (PR #39)
  passed every locked drift identifier except
  `distinct_trait_category`, which came back at 0 despite the
  loader reading 670 EFO trait rows. Diagnosis: the bundle's
  `pgs_all_metadata_efo_traits.csv` does not ship a category
  column (live header confirmed:
  `Ontology Trait ID,Ontology Trait Label,Ontology Trait
  Description,Ontology URL`). The category is published via a
  separate REST endpoint, `/rest/trait_category/all`, which
  returns ~11 categories totaling ~700 EFO traits. Fix: add a
  third audited download (`PGS_TRAIT_CATEGORY_URL`,
  `_TRAIT_CATEGORY_RESOURCE_ID`,
  `_TRAIT_CATEGORY_CACHE_FILENAME`) that fetches the REST
  payload into `~/.cache/genome/annotations/pgs_catalog/
  trait_categories.json`, plus a `_parse_trait_categories`
  helper that walks the paginated envelope and emits a
  `efo_id` → `category_label` dict. The new dict threads into
  `_join_metadata` as a fifth argument; the existing
  `orphan_trait_refs` counter still keys off the bundle's EFO
  traits file (semantics unchanged), and the category lookup
  is independent -- a score whose EFO ID is missing from the
  bundle (counted as orphan) may still pick up a category from
  the REST payload, and vice versa. The parser raises a clear
  `ValueError` on pagination (the response would silently
  truncate otherwise) and on missing-results-list / non-object
  shapes. New `rows_read_trait_categories` parser stat surfaces
  on the end-of-load summary. Tests: 10 new tests in
  `backend/tests/test_annotate_loader_pgs_catalog.py` covering
  the trait_category JSON parser (happy path, pagination
  guard, missing results, non-object payload, duplicate EFOs
  across categories, malformed entries), the join's
  trait_category population (categories sourced from REST
  dict, orphan-trait counter independence, REST-only EFOs
  don't pollute scores), the URL constant, and an end-to-end
  refresh assertion that `distinct_trait_category > 0` after
  a fixture load. New fixture
  `backend/tests/fixtures/pgs_catalog/trait_categories.json`
  carries 4 categories covering the 10-score fixture's EFO IDs
  (Body measurement, Cardiovascular disease, Other measurement,
  Other trait) plus one orphan EFO not referenced by any
  fixture score. Runbook updated: the URL section now lists
  three audited URLs (release-current + bundle + trait_category),
  the multi-file join contract calls out the REST endpoint as
  the sole `trait_category` source, the drift-identifier note
  expects ~11 categories at the verified date (with `=0`
  treated as a regression signal), and new troubleshooting
  paths cover the pagination / shape-drift / empty-results
  failure modes. The bug was a column-not-present case (option
  3 from the diagnosis tree's "scan other CSVs / endpoints"
  branch); the fix preserves the original loader's atomicity
  and audit-log shape and adds one round-trip per refresh.
  No schema rebuild required. (PR #39 follow-up)

### Added
- **Sub-phase 5.4 — PGS Catalog metadata loader.** New
  `genome.annotate.loaders.pgs_catalog` registers a `refresh`
  function at module-import time that downloads PGS Catalog's
  metadata bundle (`pgs_all_metadata.tar.gz`, ~4 MB gzipped TAR
  carrying eight per-resource CSVs) via the audited scaffold,
  parses the four CSVs relevant to score-level state in memory,
  joins them client-side on the natural keys, and chunk-loads
  one row per PGS into `pgs_catalog_scores` via PyArrow Table
  registration + `INSERT ... SELECT` (the project's locked
  bulk-load convention) at the locked 250K-rows-per-chunk
  setting (PGS Catalog ships ~5-7K rows so the corpus fits in
  one chunk, but the chunked-insert code path stays exercised
  identically across loaders). All chunks land inside one
  DuckDB transaction so a mid-stream failure rolls back the
  deactivation of prior active rows along with every partial
  chunk insert. Version label is resolved via an audited GET
  against the PGS Catalog REST release-current endpoint
  (`https://www.pgscatalog.org/rest/release/current/`); the
  JSON `date` field (defensive: also accepts `release_date` /
  `releasedate`) is rendered as `YYYY_MM_DD` (matching the
  ClinVar / GWAS Catalog convention). Failed version resolution
  propagates instead of silently falling back to today's UTC
  date -- silent fallback would either cause a duplicate load
  or paint a misleading version label onto a release that's
  actually identical. The release GET is the loader's first
  audited call -- placed before the bundle download so a fresh
  refresh against an unchanged release short-circuits without
  re-fetching the bundle body, and a disabled master switch
  surfaces `ExternalCallsDisabledError` after one intent +
  blocked audit pair (matching the 5.1a/b/5.2/5.3 pattern).
  Four-CSV join contract: `pgs_all_metadata_scores.csv` (one
  row per PGS, keyed by `Polygenic Score (PGS) ID`),
  `pgs_all_metadata_publications.csv` (keyed by
  `PGS Publication/Study (PGP) ID`, contributes
  `publication_pmid` / `publication_doi` /
  `publication_year` -- the loader extracts the four-digit year
  out of the publication's `Publication Date` ISO string),
  `pgs_all_metadata_efo_traits.csv` (keyed by
  `Ontology Trait ID`; the upstream CSV does not ship a
  `Trait Category` column at the verified date so the loader
  leaves `trait_category = NULL` for every row -- a future
  schema or loader change can backfill it from an EFO hierarchy
  walk), and `pgs_all_metadata_performance_metrics.csv`
  (multiple rows per PGS, one per evaluation cohort, collapsed
  into the schema's two scalar columns via the max reduction
  documented below). The bundle is opened with
  `tarfile.open(..., mode="r:gz")` (single-layer gzipped TAR --
  the deviation from the GWAS Catalog ZIP-wrapping shape that
  the upstream archive actually uses). TAR member names include
  a leading `/` (absolute paths in the upstream packaging) so
  the helper matches via `endswith` rather than exact equality.
  Performance-metric max reduction: a PGS's per-cohort entries
  in `performance_metrics` collapse via
  `max(non-NULL values)` per column independently --
  `performance_auc = max(e.auc for e in entries if e.auc is not
  None)` and similarly for `performance_or_per_sd`. The "max"
  rule is the simplest auditable reduction at this scale, not
  the most statistically honest one; honest per-cohort
  reporting would require a separate `pgs_catalog_performance`
  table (future schema change, not 5.4 work). The end-of-load
  summary surfaces `multi_cohort_performance` (count of scores
  with > 1 cohort entry) so downstream consumers can see when
  the scalar is the output of a reduction vs a single-entry
  source. Field-level coercions: `Mapped Trait(s) (EFO ID)`
  splits on `,` and the loader keeps the first ID (the
  curators' primary mapping) with `truncated_trait_efo`
  counting the rows where this happened; `Number of Variants`
  parses as INTEGER (`NR` → NULL); the OR / AUROC cells ship
  as `"<estimate> [<lower>,<upper>]"` and the loader extracts
  only the leading point estimate (CI is dropped at this
  layer); empty / `NA` / `NR` / `-` / whitespace-only values
  coerce to NULL. The refresh is idempotent on
  `(source_db='pgs_catalog', version)`: a second call without
  `--force` short-circuits with `was_already_current=True`.
  `--force` blanket-deactivates every prior active PGS Catalog
  row before re-inserting; `pgs_catalog_scores` carries
  `is_active` but **not** `superseded_by` (matches PharmGKB /
  CPIC / GWAS Catalog), so the standard supersession helper's
  `has_superseded_by=False` path is used and the supersession
  chain is followed via the prior rows' `source_version_id`
  column. The supersede + chunked-insert pair runs inside one
  DuckDB transaction; a failure rolls every chunk back along
  with the deactivation, and best-effort deletes the orphan
  `annotation_source_versions` row that `upsert_source_version`
  had committed in its inner transaction. End-of-load
  structlog summary emits the locked drift identifiers
  (`active_total`, `distinct_pgs_id`, `distinct_trait_efo`,
  `distinct_publication_pmid`, `distinct_trait_category`,
  `with_performance_auc`, `with_performance_or_per_sd`) plus
  parser stats (`rows_read_scores`, `rows_read_publications`,
  `rows_read_traits`, `rows_read_performance`,
  `orphan_publication_refs`, `orphan_trait_refs`,
  `scores_without_performance`, `multi_cohort_performance`,
  `truncated_trait_efo`) and elapsed wall-clock so a
  cross-release diff is one log scrape away. CLI invocation:
  `genome annotate refresh --source pgs_catalog`;
  `genome annotate status` now reports pgs_catalog alongside
  clinvar / cpic / gwas_catalog / pharmgkb. Tests: 73 new
  tests in `backend/tests/test_annotate_loader_pgs_catalog.py`
  covering the per-field coercions, the four per-file parsers,
  the in-memory join + max-reduction, the gzipped-TAR bundle
  helper (happy path, missing entry, directory entry skipped,
  leading-slash member-name match), the version-resolution
  path (release-current happy path, `release_date` /
  `releasedate` defensive aliases, malformed-JSON loud-fail,
  HTTP-5xx propagation), the end-to-end refresh against four
  new fixture CSVs in
  `backend/tests/fixtures/pgs_catalog/`, the supersession
  transaction (same-version `--force` and different-version
  round-trip), the audited refusal path with
  `external_calls_enabled=false`, a 5K-row synthetic benchmark
  guard pinned at < 30 s (the project-wide routine-refresh
  ceiling documented in CLAUDE.md), and the CLI smoke.
  Documentation: new PGS Catalog section in
  `docs/runbooks/annotations.md` covering the URL choice
  (two-step release-current + FTP bundle), version-label
  semantics, multi-file join contract, performance-metric max
  reduction with the auditability trade-off called out
  explicitly, per-field coercions, drift identifiers,
  real-data verification commands and expected ranges, and
  troubleshooting paths (`ExternalCallsDisabledError`,
  release-current shape drift, bundle layout drift, header
  drift, orphan-publication / orphan-trait spikes,
  disk-space failure, partial-failure recovery). Real-data
  verification numbers will land in the merge commit once the
  user has run `genome annotate refresh --source pgs_catalog`
  against the current release with
  `external_calls_enabled=true`. Sub-phase 5.4 closes; 5.5
  (gnomAD filtered) follows. Out of scope (deferred per the
  sub-phase plan): `variant_annotations_index` refresh
  (sub-phase 5.8), per-PGS variant weights (`pgs_score_weights`
  is Phase 6 work), finding-009 #11 (explicit `CHECKPOINT`),
  finding-009 #13 (chunked UPDATE), the `MAPPED_TRAIT_URI`
  truncation note in `finding-005-deferred-improvements.md`
  (separate one-line doc edit on a future PR that touches
  `gwas_catalog.py`). (PR #XX)

### Changed
- **Schema correction — `pgs_catalog_scores` surrogate PK.**
  Sub-phase 5.0 created `pgs_catalog_scores` with `pgs_id
  VARCHAR PRIMARY KEY`, which conflicts with the locked
  supersession-over-update pattern (CLAUDE.md decision #7) --
  a refresh that inserts new rows with the same `pgs_id`
  while flipping the prior rows to `is_active=FALSE` would
  violate the PK constraint. The 5.4 schema correction
  introduces a surrogate `score_record_id BIGINT PRIMARY KEY`
  (mirroring the surrogate keys on the other four annotation
  tables: `pharmgkb_id`, `guideline_id`, `clinvar_id`,
  `association_id`) and demotes `pgs_id` to `VARCHAR NOT
  NULL` with `idx_pgs_id ON (pgs_id, is_active)` covering the
  most common lookup. DuckDB's FK requirement that the
  target column carry a `PRIMARY KEY` or `UNIQUE` constraint
  then knocks two prior DB-level FKs out of the schema --
  `pgs_score_weights.pgs_id REFERENCES pgs_catalog_scores
  (pgs_id)` (same-group, group 2) and `derived_pgs.pgs_id
  REFERENCES pgs_catalog_scores(pgs_id)` (cross-group, group
  3 → group 2). Both become application-validated references
  in the "Application-validated references" section of the
  group 2 schema doc, consistent with the existing
  cross-group precedent. The section title is generalized
  from "Cross-group references" since the new same-group
  `pgs_score_weights` → `pgs_catalog_scores` link is the
  first non-cross-group entry. This PR modifies
  `docs/schemas/schema_group_2_reference_annotations.md`,
  `docs/schemas/schema_group_3_derived_analyses.md`,
  `ddl/group_2_annotations.sql`, and `ddl/group_3_derived.sql`.
  Per CLAUDE.md's locked convention, the merge requires
  `rm -rf data/ && uv run genome init` and a re-ingest of any
  data the user had loaded (23andMe + Ancestry exports,
  PharmGKB, CPIC, ClinVar, GWAS Catalog) before running the
  new PGS Catalog loader. (PR #XX)

### Fixed
- **Sub-phase 5.3 follow-up — GWAS Catalog download flow and
  `external_client.download` error-message bug.** Real-data
  verification of the original 5.3 PR (#38) failed for two stacked
  reasons:
  1. The hardcoded `https://www.ebi.ac.uk/gwas/api/search/downloads/full`
     download URL returns HTTP 404 — that endpoint has been retired.
     The current pattern is a two-step: GET
     `https://www.ebi.ac.uk/gwas/api/search/stats` to discover the
     release `date`, then download the dated release ZIP
     (`gwas-catalog-associations_ontology-annotated-full.zip`) from
     the EBI FTP. The loader now uses the
     `https://ftp.ebi.ac.uk/pub/databases/gwas/releases/latest/`
     symlink directory for the download (the stats-endpoint freeze
     date and the FTP publish date typically differ by 1-2 days, so a
     strict `/releases/{YYYY}/{MM}/{DD}/...` template would 404). The
     downloaded artifact is a ZIP carrying a single TSV
     (`gwas-catalog-download-associations-alt-full.tsv`); a new
     `_open_tsv_from_zip` context manager streams the entry through
     `zipfile.ZipFile` without unpacking to disk. The version label is
     now the stats date in `YYYY_MM_DD` form (matching the ClinVar
     convention; the prior `e<NN>_r<YYYY-MM-DD>` filename pattern is
     gone). A failed stats call (network / HTTP 4xx/5xx / malformed
     JSON / missing `date` field) now propagates instead of silently
     falling back to today's UTC date — silent fallback could either
     paint a misleading version label or cause a duplicate load.
  2. `ExternalClient.download` constructed its HTTP-error message by
     reading `response.text[:200]` on a still-streaming response, which
     raises `httpx.ResponseNotRead` and masked the actual HTTP error.
     The bug affected every download path but never fired in the prior
     three loaders because PharmGKB / CPIC / ClinVar all hit endpoints
     that returned 200. Fix: call `response.read()` before `.text`
     access, with a defensive fallback snippet of `<unavailable>` if
     the read itself fails. New regression test in
     `test_privacy_external_client.py` constructs an explicitly
     deferred-read `httpx.SyncByteStream` 404 response and asserts the
     emitted message includes the body snippet and does NOT mention
     `ResponseNotRead`.
  No schema changes. The new tests cover the stats-endpoint happy
  path, the `releasedate` defensive alias, malformed-JSON loud-fail,
  HTTP-5xx propagation, and the ZIP-archive shape checks
  (non-ZIP / missing canonical entry). Real-data verification numbers
  for `genome annotate refresh --source gwas_catalog` will land in the
  merge commit once the user runs the refresh against the current
  release with `external_calls_enabled=true`. (PR #38 follow-up)

### Added
- **Sub-phase 5.3 — GWAS Catalog associations loader.** New
  `genome.annotate.loaders.gwas_catalog` registers a `refresh`
  function at module-import time that downloads EBI's GWAS Catalog
  "all associations" TSV (~600-700K active rows at the current
  release) via the audited scaffold, parses it with a streaming
  `csv.DictReader`, and chunk-loads into `gwas_catalog_associations`
  via PyArrow Table registration + `INSERT ... SELECT` (the
  project's locked bulk-load convention) at the locked 250K rows
  per chunk; all chunks land inside one DuckDB transaction so a
  mid-stream failure rolls back the deactivation of prior active
  rows along with every partial chunk insert. Version label is
  resolved via a HEAD against the canonical URL: the
  `Content-Disposition` header's filename and the final response
  URL both carry the canonical GWAS Catalog
  `e<NN>_r<YYYY-MM-DD>` pattern (Ensembl release number + release
  date, e.g. `e110_r2024-08-30`); when neither is parseable the
  loader falls back to the HTTP `Last-Modified` header rendered as
  `e0_r<YYYY-MM-DD>`, and finally to today's UTC date in the same
  shape. The HEAD is the loader's first audited call — placed
  before the download so a fresh refresh against an unchanged
  release short-circuits without re-fetching the TSV body, and a
  disabled master switch surfaces `ExternalCallsDisabledError`
  after one intent + blocked audit pair (matching the 5.1a/b/5.2
  pattern). Per-row mapping: `SNPS` is split on `;` into
  individual rsIDs, with rows expanding to one DB row per rsID
  (all sharing the same study, PMID, trait, statistics, and
  sample-size context); bare-digit rsIDs get the `rs` prefix, and
  non-rsID tokens (star alleles, HLA, haplotype text) are rejected
  because the schema's `rsid VARCHAR NOT NULL` contract is
  per-row. Rows whose `CHR_ID` or `CHR_POS` is empty / `NA` / `NR`
  / `-` are dropped at parse time and counted in
  `dropped_empty_pos` — the schema's position-based join contract
  has no use for a coordinate-less association.
  `MAPPED_TRAIT_URI` can ship as a comma-separated multi-value
  list when an association is mapped to several EFO terms; the
  loader keeps the first URI (the curators' primary mapping) and
  counts the truncations for the end-of-load summary
  (`truncated_mapped_trait_uri`). `trait_id` is derived from the
  same first URI via a trailing `<PREFIX>_<digits>` regex
  (`http://www.ebi.ac.uk/efo/EFO_0001065` → `EFO_0001065`).
  `STRONGEST SNP-RISK ALLELE` (shape `rsID-allele`) extracts the
  trailing allele into `effect_allele`; the `?` sentinel maps to
  NULL. `RISK ALLELE FREQUENCY` and `OR or BETA` parse via
  `float`, accepting both `E`/`e` scientific-notation forms.
  `P-VALUE` likewise parses sci notation natively. `95% CI (TEXT)`
  is matched as `[lower-upper]` to fill `ci_95_lower` /
  `ci_95_upper`; pure-text values (`[NR] unit decrease`) produce a
  NULL pair. `INITIAL SAMPLE SIZE` / `REPLICATION SAMPLE SIZE` are
  free-form text — the loader extracts the leading comma-grouped
  integer (`"4,512 European ancestry individuals"` → 4512);
  `is_replicated` is set to `True` iff the replication count is a
  positive integer (missing / zero → NULL, not `False`, to keep
  `is_replicated IS TRUE` semantics clean). `effect_size_unit`
  and `ancestry` are intentionally NULL in 5.3 (the OR-vs-BETA
  unit doesn't disambiguate at the row level; ancestry lives in
  a separate GWAS Catalog file this loader does not consume).
  The refresh is idempotent on `(source_db='gwas_catalog',
  version)`: a second call without `--force` short-circuits with
  `was_already_current=True`. `--force` blanket-deactivates every
  prior active GWAS Catalog row before re-inserting;
  `gwas_catalog_associations` carries `is_active` but **not**
  `superseded_by` (schema matches PharmGKB / CPIC; ClinVar is the
  outlier that carries both), so the standard supersession helper's
  `has_superseded_by=False` path is used and the supersession chain
  is followed via the prior rows' `source_version_id` column rather
  than a per-row tag. The supersede + chunked-insert pair runs
  inside one DuckDB transaction; a failure rolls every chunk back
  along with the deactivation, and best-effort deletes the orphan
  `annotation_source_versions` row that `upsert_source_version`
  had committed in its inner transaction. End-of-load structlog
  summary emits the locked drift identifiers (`active_total`,
  `distinct_study_accession`, `distinct_pmid`, `distinct_rsid`,
  `distinct_trait_name`) plus parser stats (`rows_read`,
  `rows_emitted`, `dropped_empty_pos`, `dropped_no_valid_snp`,
  `multi_snp_expansions`, `truncated_mapped_trait_uri`) and
  elapsed wall-clock so a cross-release diff is one log scrape
  away. CLI invocation:
  `genome annotate refresh --source gwas_catalog`;
  `genome annotate status` now reports gwas_catalog alongside
  clinvar / cpic / pharmgkb. Tests: 63 new tests in
  `backend/tests/test_annotate_loader_gwas_catalog.py` covering
  the per-field coercions, the multi-SNP expansion contract, the
  empty-CHR_POS drop, the sci-notation p-value parse, the
  multi-valued MAPPED_TRAIT_URI truncation, the EFO trait-ID
  extraction, the version-string parse (Content-Disposition +
  Last-Modified + final-URL fallback paths), the end-to-end
  refresh against the new 50-row fixture
  (`backend/tests/fixtures/gwas_catalog_sample.tsv`), the
  supersession transaction (same-version `--force` and
  different-version round-trip), the audited refusal path with
  `external_calls_enabled=false`, a 100K-row benchmark guard
  pinned at < 30 s, and the CLI smoke. Documentation: new GWAS
  Catalog section in `docs/runbooks/annotations.md` covering the
  URL choice + version-label semantics, multi-SNP expansion,
  coordinate-less drop, single-value `mapped_trait_uri`, per-field
  coercions, the chunked-insert + supersession shape, drift
  identifiers, real-data verification commands and expected
  ranges, and troubleshooting paths
  (`ExternalCallsDisabledError`, header drift, drop-spike
  diagnosis, disk-space failure, partial-failure recovery). No
  schema rebuild required — `gwas_catalog_associations` and
  `annotation_source_versions` were already created by the 5.0
  scaffold; the schema is unchanged. Real-data verification
  numbers (active row counts, distinct study/PMID/rsID/trait
  counts, wall-clock) will land in the merge commit once the
  user has run `genome annotate refresh --source gwas_catalog`
  against the current release with `external_calls_enabled=true`.
  Sub-phase 5.3 closes; 5.4 (PGS Catalog metadata) follows.
  Out of scope (deferred per the sub-phase plan):
  `variant_annotations_index` refresh (sub-phase 5.8), the
  explicit `CHECKPOINT` change in `supersession.py` from
  finding-009 #11, the chunked-UPDATE design question in
  finding-009 #13, and tier-2 rsid-based matching across
  positions. (PR #XX)
- **Sub-phase 5.2 — ClinVar clinical-significance annotations loader.**
  New `genome.annotate.loaders.clinvar` registers a `refresh` function
  at module-import time that downloads ClinVar's
  `variant_summary.txt.gz` (the canonical tab-delimited per-variant
  release, ~9M rows in a 419 MB gzipped TSV) via the audited scaffold,
  parses it with a streaming `csv.DictReader` over `gzip.open(..., 'rt')`,
  and chunk-loads it into `clinvar_annotations` via PyArrow Table
  registration + `INSERT ... SELECT` (the project's locked bulk-load
  convention). Three orders of magnitude bigger than 5.1's PharmGKB
  (~7K rows) and CPIC (~3.5K rows), so the parser is a generator and
  the bulk insert is chunked at 250,000 rows per chunk; all chunks land
  inside one DuckDB transaction so a mid-stream failure rolls back the
  deactivation of prior active rows along with every partial chunk
  insert. Version label is resolved via a HEAD request against the
  variant_summary URL, parsing the upstream HTTP `Last-Modified` header
  via `email.utils.parsedate_to_datetime` and rendering as `YYYY_MM_DD`;
  unparseable / missing header falls back to today's UTC date in the
  same format. The HEAD is the loader's first audited call -- placed
  before the download so a fresh refresh against an unchanged release
  short-circuits without re-fetching the 419 MB body, and a disabled
  master switch surfaces `ExternalCallsDisabledError` after one intent
  + blocked audit pair (matching 5.1a/b's audited refusal pattern).
  ClinVar publishes one row per `(VariationID, Assembly)` pair, so
  every row lands in the table (no clinical-significance / variant-type
  filtering -- that's a query concern), but `Assembly == 'GRCh38'` rows
  populate the GRCh38-specific columns (`pos_grch38`, `ref_allele`,
  `alt_allele`) while `Assembly == 'GRCh37'` rows leave them NULL --
  the schema's `pos_grch38` column name is constraining and storing
  GRCh37 coordinates under it would mislead position-based joins.
  Per-field coercions: ClinVar's `RS# (dbSNP)` column encodes a
  missing rsID as the literal `"-1"` (an integer sentinel from the
  dbSNP era, not the empty string), so the loader coerces both `"-1"`
  and the standard empty / dash variants to NULL and prefixes
  non-missing digits with `"rs"` to match the project-wide rsID format
  (`variants_master`, `pharmgkb_annotations`, the dbSNP loader landing
  in 5.4); `PhenotypeList` (single pipe `|` separators) populates
  `conditions VARCHAR[]`; `PhenotypeIDS` (two-level `||` between
  phenotypes plus `,` within one phenotype's IDs) flattens into
  `condition_ids VARCHAR[]`; `SubmitterCategories` (a single integer
  per ClinVar docs) wraps as `[str(int)]` in `submitter_categories
  VARCHAR[]`; `LastEvaluated` (e.g. `"Dec 17, 2024"`) parses via
  `datetime.strptime(..., "%b %d, %Y")`; the `Name` column splits on
  the trailing `(p.…)` block into `hgvs_c` + `hgvs_p`; `star_rating`
  derives from `review_status` via the locked
  `_REVIEW_STATUS_TO_STAR` mapping (`practice guideline → 4`,
  `reviewed by expert panel → 3`, etc.) with unmapped review-status
  strings yielding NULL `star_rating` (intentional loud-fail for a
  future ClinVar wording change); `inheritance` is always NULL
  (variant_summary.txt does not carry inheritance pattern). The
  refresh is idempotent on `(source_db='clinvar', version)`: a second
  call without `--force` short-circuits with
  `was_already_current=True`. `--force` blanket-deactivates every
  prior active ClinVar row (tagging them with `superseded_by =
  new_source_version_id`) before re-inserting; `clinvar_annotations`
  carries both `is_active` and `superseded_by` (the only Phase-5
  source loader so far that populates `superseded_by`), so the
  standard supersession helper's `has_superseded_by=True` path is
  used. The supersede + chunked-insert pair runs inside one DuckDB
  transaction; a failure rolls every chunk back along with the
  deactivation, and best-effort deletes the orphan
  `annotation_source_versions` row that `upsert_source_version` had
  committed in its inner transaction. End-of-load structlog summary
  emits the locked drift identifiers (`active_total`,
  `distinct_variation_id`, `distinct_rsid_non_null`, full
  `clinical_significance_distribution`, full
  `review_status_distribution`) plus elapsed wall-clock so a
  cross-release diff is one log scrape away. CLI invocation:
  `genome annotate refresh --source clinvar`; `genome annotate
  status` now reports clinvar alongside cpic and pharmgkb. Real-data
  verification against ClinVar release `2026_05_10`
  (`URL_VERIFIED_DATE = 2026-05-15`): 8,978,989 active rows from
  4,523,355 distinct ClinVar VariationIDs (~2x because each variant
  carries one row per assembly) and 2,645,685 distinct non-NULL
  rsIDs; clinical_significance distribution top-5
  Uncertain significance=4,673,230 / Likely benign=2,182,511 /
  NULL=490,998 / Benign=425,621 / Pathogenic=404,221; review_status
  distribution top-5 criteria provided, single submitter=6,522,322 /
  criteria provided, multiple submitters, no conflicts=1,321,678 /
  NULL=490,998 / criteria provided, conflicting classifications=326,436
  / no assertion criteria provided=257,206; reviewed by expert
  panel=43,740 and practice guideline=116 carrying the 3- and 4-star
  rows respectively. Source provenance: SHA-256 prefix
  `61e2b1fd3123bdc4`, byte size 439,003,062, recorded `record_count =
  8,978,989` on `annotation_source_versions`. First-load wall-clock:
  280 s (~4.7 min), of which ~25 s was the audited streaming download
  and ~255 s was 36 chunks of parse + insert. Same-version `--force`
  re-refresh wall-clock: 1,699 s (~28.3 min), dominated by the 8.98M-
  row deactivate UPDATE on `is_active` (which is part of
  `idx_cv_active` and `idx_cv_significance`, so DuckDB's MVCC rewrites
  it as DELETE+INSERT and the index updates serialize) and the
  multi-million-row checkpoint at commit; the chunked insert phase
  itself remained ~5 min. The `--force` cycle preserved the drift
  identifiers byte-exactly: 8,978,989 active rows / 4,523,355 distinct
  variation_id / 2,645,685 distinct non-NULL rsid match the first run,
  and every clinical_significance and review_status bucket count
  matches. Post-`--force` state: 17,957,978 total rows (8,978,989
  is_active=TRUE under source_version_id=3, 8,978,989
  is_active=FALSE also under source_version_id=3 with superseded_by=3),
  proving the supersession transaction landed atomically at full
  scale. Note: the prompt's "2 source_version rows" expectation
  assumes a real upstream release transition (different
  `Last-Modified` header → different version label → fresh
  source_version_id), which we cannot simulate against the same
  archive; the new-version supersession path is covered exactly at
  fixture scale by `test_refresh_supersedes_prior_rows_on_new_version`
  in the integration suite. PharmGKB / CPIC regression check after
  the ClinVar load: 7,013 active `pharmgkb_annotations` rows
  preserved exactly; 3,591 active `cpic_guidelines` rows preserved
  exactly. Documentation: new ClinVar section in
  `docs/runbooks/annotations.md` covering the URL choice (TSV vs XML
  trade-off), version-label semantics, the assembly-row split, the
  per-field coercion contract, the chunked-insert + supersession
  shape, drift identifiers, and the troubleshooting paths
  (`ExternalCallsDisabledError`, header drift, mid-stream
  `MemoryError`, disk-space failure, partial-failure recovery). No
  schema rebuild required -- `clinvar_annotations` and
  `annotation_source_versions` were already created by the 5.0
  scaffold. Sub-phase 5.2 closes; 5.3 (GWAS Catalog) follows. (PR #36)
- **Sub-phase 5.1b — CPIC clinical guidelines loader.** New
  `genome.annotate.loaders.cpic` registers a `refresh` function at
  module-import time that downloads CPIC's `/guideline`, `/pair`,
  `/recommendation`, and `/drug` JSON endpoints via the audited
  scaffold and joins them client-side into (gene × drug × phenotype)
  rows in `cpic_guidelines`. Multi-gene recommendations split into one
  row per gene, all sharing the same `cpic_id` (the CPIC
  recommendation primary key) and differing only in `gene_symbol` and
  `phenotype`. Recommendations without a parseable lookupkey, without
  a known drug id, or without a drug name are skipped (with a debug
  log line per skip carrying the recommendation id for forensic
  traceability). The `pediatric` flag is set strictly:
  `True` iff `recommendation.population == 'pediatrics'`, otherwise
  `None` — CPIC's `population` column overloads age and condition
  axes, so `None` (not `False`) keeps `pediatric IS TRUE` semantics
  free of false negatives. `publication_pmid` is the first PMID from
  the pair's `citations` array; `cpic_level` and `publication_pmid`
  are per (gene, drug) and therefore differ across multi-gene splits
  of the same recommendation. Version label is resolved from a single
  `/change_log?order=date.desc&limit=1&select=date` canary download
  (separate from the four data endpoints, also audited) and falls
  back to retrieval date in `YYYY_MM_DD` form if the canary fails.
  The `annotation_source_versions` row records `source_url =
  GUIDELINE_URL`, a combined SHA-256 over the four data files' hashes
  (so the fingerprint changes iff any endpoint's data changes), and
  the sum of the four data files' byte sizes; per-endpoint detail is
  in the structlog `cpic.download.audited` events. The refresh is
  idempotent on `(source_db='cpic', version)`; `--force`
  blanket-deactivates every prior active CPIC row before re-inserting
  so re-runs against the same version label don't produce duplicate
  active rows. The supersede + bulk-insert pair runs inside one
  DuckDB transaction; a failure rolls both back and best-effort
  deletes the orphan `annotation_source_versions` row that
  `upsert_source_version` committed in its inner transaction. CLI
  invocation: `genome annotate refresh --source cpic`;
  `genome annotate status` now reports both PharmGKB and CPIC.
  Real-data verification against CPIC release `2026_05_14`
  (`URL_VERIFIED_DATE = 2026-05-15`): 3,591 active rows from 2,159
  distinct CPIC recommendation IDs across 19 distinct genes and 109
  distinct drugs; cpic_level distribution A=1,638 / B=1,828 / C=125;
  classification_strength distribution
  Optional=1,971 / Strong=957 / Moderate=505 / "No Recommendation"=134 /
  n/a=24; 30 rows with `pediatric = TRUE` and 3,561 with `pediatric IS
  NULL`. Idempotent re-refresh: 0 new rows, `already_current=True`.
  `--force` re-refresh: 3,591 prior rows deactivated, 3,591 new rows
  inserted (same `source_version_id`, version label unchanged).
  PharmGKB regression check after the CPIC load: 7,013 active
  `pharmgkb_annotations` rows preserved exactly. Documentation: new
  CPIC section in `docs/runbooks/annotations.md` with the URL list,
  version-label semantics, per-row mapping notes, and troubleshooting
  paths. No schema rebuild required — `cpic_guidelines` was already
  created by the 5.0 scaffold. Sub-phase 5.1 (PharmGKB + CPIC) is
  complete with this PR; 5.2 (ClinVar) follows. (PR #35)
- **Sub-phase 5.1a — PharmGKB clinical annotations loader.** New
  `genome.annotate.loaders.pharmgkb` registers a `refresh` function
  at module-import time that downloads PharmGKB's Clinical Annotations
  ZIP (`clinicalAnnotations.zip`) via the audited external client,
  parses `clinical_annotations.tsv`, and bulk-loads
  (clinical annotation × drug) rows into `pharmgkb_annotations`
  via PyArrow Table registration + `INSERT ... SELECT`. Multi-drug
  rows are split into one row per drug (semicolon-separated; commas
  inside single drug names like `"Ace Inhibitors, Plain"` are
  preserved). The `Variant/Haplotypes` column is bucketed by the
  strict regex `^rs\d+$` — rsIDs land in `rsid`, everything else
  (star alleles like `CYP2D6*4`, HLA alleles like `HLA-B*57:01`,
  descriptive haplotypes like
  `G6PD A- 202A_376G, G6PD B (reference)`) lands in `star_allele`.
  `chrom` and `pos_grch38` are written as NULL; the dbSNP loader in
  sub-phase 5.4 will cross-reference rsIDs into positions. Version
  label is read from the ZIP's `CREATED_YYYY-MM-DD.txt` marker file
  (reformatted as `YYYY_MM_DD`) and falls back to retrieval date in
  the same shape if absent. The refresh is idempotent on
  `(source_db='pharmgkb', version)`: a second call without `--force`
  short-circuits with `was_already_current=True` and no new rows.
  `--force` blanket-deactivates every prior active PharmGKB row
  before re-inserting so re-runs against the same version label do
  not produce duplicate active rows. The supersede + bulk-insert
  pair runs inside one DuckDB transaction; a failure in either step
  rolls both back and best-effort deletes the orphan
  `annotation_source_versions` row that `upsert_source_version`
  committed in its own (scaffold-mandated) inner transaction.
  Establishes the loader template every subsequent Phase 5 source
  loader (CPIC in 5.1b, ClinVar in 5.2, GWAS in 5.3, dbSNP in 5.4,
  etc.) will mirror. CLI invocation:
  `genome annotate refresh --source pharmgkb`; `genome annotate
  status` reports the loaded version, ingested_at, and record_count.
  PharmGKB's canonical `api.pharmgkb.org` URL serves a 303 redirect
  to its S3 host; the scaffold's `download_to_cache` now injects an
  `httpx.Client(follow_redirects=True)` into its `ExternalClient` so
  the loader writes the canonical URL into its constants and the
  redirect is followed transparently (see the matching CHANGELOG
  bullet below). Real-data verification against
  PharmGKB release `2025_07_05` (`URL_VERIFIED_DATE = 2026-05-15`):
  7,013 active rows, 5,186 distinct `pgkb_accession`, evidence-level
  distribution 1A=566 / 1B=25 / 2A=48 / 2B=29 / 3=5,976 / 4=369,
  6,358 rows with non-NULL `rsid`, 655 rows with non-NULL
  `star_allele` (and 7,013 rows with NULL `chrom` — coordinates will
  populate via the 5.4 dbSNP cross-reference). Idempotent
  re-refresh: 0 new rows; `--force` re-refresh: 7,013 prior rows
  deactivated, 7,013 new rows inserted (same `source_version_id`).
  New `docs/runbooks/annotations.md` documents the workflow, the
  per-source PharmGKB notes, and the troubleshooting paths.
  No schema rebuild required — `pharmgkb_annotations` and
  `annotation_source_versions` were already created by the 5.0
  scaffold. (PR #34)
- Scaffold fix: `genome.annotate.downloads.download_to_cache` now
  injects an `httpx.Client(follow_redirects=True)` into its
  `ExternalClient`. Public dataset distribution endpoints (PharmGKB,
  ClinVar, GWAS Catalog, dbSNP, gnomAD) routinely 303-redirect to
  signed S3 / CDN URLs; without redirect-following the scaffold
  silently wrote redirect-response bodies (typically 0 bytes) to
  disk, breaking the downstream ZIP / VCF reads with `BadZipFile` or
  empty-record iteration. PharmGKB surfaced this; ClinVar / GWAS /
  dbSNP / gnomAD downloads in later sub-phases will share the fix.
  `ExternalClient` itself stays redirect-agnostic — other workflows
  (e.g. Phase 4 reference-panel downloads) use final URLs where
  silently following a redirect would mask a misconfiguration. Two
  new tests in `test_annotate_downloads.py` pin the 303 → 200
  end-to-end behaviour. (PR #34)
- **Sub-phase 5.0 — annotation loader scaffold.** New
  `genome.annotate` package containing the
  `annotation_source_versions` upsert helper, the on-disk download
  cache layout under `~/.cache/genome/annotations/`, the audited
  download wrapper over the existing `ExternalClient`, the generic
  supersession helper for evolving sources, the per-source loader
  registry, and the `genome annotate refresh|status` CLI subcommands.
  No source-specific loaders ship in this PR — those land in 5.1+.
  No DDL changes. (PR #33)

### Fixed
- `platform_coverage_v.in_imputed`, `call_comparison_v.gt_imputed`, and
  `call_comparison_v.imputed_r2` filter on `'beagle_imputed'` instead of
  `'topmed_imputed'`. The three filter expressions in
  `docs/schemas/schema_group_1_genotype_data.md` were left pointing at
  `'topmed_imputed'` after the Phase 4 pivot from TopMed to local Beagle
  (see
  `docs/findings/finding-006-topmed-not-viable-for-personal-genomics.md`),
  so the affected columns returned NULL/FALSE for every variant in the
  real corpus despite ~2.37M active `beagle_imputed` calls. The
  `'topmed_imputed'` enum value is retained on `source_enum` per
  finding-006's backward-compat decision; only the view filters change.
  Re-extracted DDL into `ddl/group_1_genotype.sql`. Added a semantic
  test (`backend/tests/test_views_genotype_imputed.py`) that pins both
  the positive case (an active `beagle_imputed` call surfaces in
  `in_imputed` / `gt_imputed` / `imputed_r2`) and the negative case (a
  `topmed_imputed` call does NOT, so the filter cannot regress to an
  `IN ('beagle_imputed', 'topmed_imputed')` shape that would propagate
  dead-enum reads). Schema change: requires the standard
  `rm -rf data/` + `uv run genome init` rebuild per the CLAUDE.md
  schema-change convention.
- `genome imputation prepare` no longer references the abandoned TopMed
  Imputation Server in its `--sample-id` help text or its post-prepare
  "next step" echoes. The command now points the user at
  `genome imputation run <id>` and the `docs/runbooks/imputation.md`
  prepare → run → import flow, matching the Phase 4 pivot to local
  Beagle 5.5 documented in
  `docs/findings/finding-006-topmed-not-viable-for-personal-genomics.md`.
  Adds a stdout-scraping test in `backend/tests/test_cli_phase4.py` so a
  future regression of this text is caught by the suite.

### Documentation
- **Pre-5.5 — ROADMAP refresh and remaining-Phase-5 sequencing.**
  Documentation-only refresh that locks the Option A sub-phase
  plan for the remainder of Phase 5 and pins the outstanding
  follow-ups, enrichment, and backfills to their slots. The new
  ROADMAP Phase 5 scope drops the deferred items (VEP runner →
  Phase 6; genes / traits / pathways dictionary tables → Phase 7)
  and adds the two missing-but-required pieces (profile-level QC
  rollup as 5.8; `variant_annotations_index` refresh as 5.7).
  Sub-phase checklist is rewritten in completion order: 5.0
  (scaffold, PR #33), 5.1a (PharmGKB, PR #34), 5.1b (CPIC,
  PR #35), 5.2 (ClinVar, PR #36), 5.3 (GWAS Catalog, PR #38),
  5.4 (PGS Catalog metadata, PR #39) — shipped; 5.5 (gnomAD
  filtered) next, then 5.6 (dbSNP filtered), 5.7
  (`variant_annotations_index` refresh), 5.8 (profile-level QC
  rollup). Phase 5 supersession is documented as version-pointer
  (CLAUDE.md #7 / `finding-010`), reflecting the post-PR-#43
  state. Follow-up section sequences the four open finding-010
  items (#12 PharmGKB / CPIC `already_current=True` cosmetic
  cleanup, #13 HEAD-request-failure version-label fallback
  capture, #14 orphan-row cleanup under superseded
  `source_version_id`s, #15 cross-source generalization
  opportunity) plus the deferred-from-5.3 `MAPPED_TRAIT_URI`
  truncation entry for finding-005, all as small PRs slotted
  between sub-phases. Enrichment section pins the
  `variants_master.is_acmg_sf` flag population task (finding-005
  #5) as ClinVar-dependent and consumed by Phase 6's ACMG SF
  detection pipeline. Backfills section pins the three
  finding-005 dbSNP-dependent items (#1 canonical REF/ALT for
  strand-flip dedupe, #4 tier-2 rsID matching via
  `variant_aliases`, #6 hom-only recovery via canonical
  REF/ALT) as 5.6-dependent. Phase 6 scope gains one bullet
  for the VEP local runner (clustered with the other runner-
  pattern tools — Beagle / PharmCAT / HIBAG). README's Status
  section is updated to reflect 5.0-5.4 shipped and 5.5
  (gnomAD) as the next substantive sub-phase. No code changes;
  no schema rebuild; no re-ingest; `docs/schemas/` is untouched,
  `finding-005` / `finding-010` are referenced but not edited.
  (PR #45)
- Added `docs/findings/finding-008-phase4-rebuild-and-chrx-observations.md`
  capturing two durable Phase 4 real-data observations surfaced by the
  PR #31 schema-change rebuild: (1) the rebuild-from-preserved-archive
  workflow requires `prepare → run → import` rather than the
  prepare → import shortcut, because the runner is the step that
  flips `imputation_runs.status` from `pending` to `completed` (the
  runner is resumable, so on-disk chromosomes are parse-checked and
  skipped); and (2) chrX Beagle runs fail with
  `IllegalArgumentException: Reference sample HG00096 has an
  inconsistent number of alleles` because the 1000G Phase 3 panel
  represents non-PAR chrX as haploid for males, which Beagle 5.5's
  reference loader rejects — this is the mechanism behind the
  previously-documented "chrX imputed variants: 0 for males" symptom in
  CLAUDE.md "Real-data observations" #3. Two fix options
  (pre-process the panel to fake-diploid male non-PAR X, or a
  sex-aware PAR1/PAR2/non-PAR split) are documented and explicitly
  deferred, as is a `register-existing-result` CLI command that would
  collapse the full-archive-preserved rebuild case to a single command.
- Updated `docs/runbooks/imputation.md` with a "Rebuilding from a
  preserved archive" section walking through the schema-change rebuild
  scenario and the expected wall-clock cost at each preservation level
  (full archive: seconds; partial: minutes; none: ~30 minutes), and a
  "Known issues: chrX hemizygous-haploid Beagle failure" subsection
  under Troubleshooting that documents the
  `java.lang.IllegalArgumentException`, the truncated
  `result/chrX.vcf.gz`, the zero-variant cyvcf2 read on import, and
  the deferred status of the two known fixes. Pure docs: no code,
  schema, DDL, CLI, or test changes.

## [0.4.0] — 2026-05-14

### Added
- **Phase 3 — merge & discrepancy detection.** New `genome.merge` package
  computes `consensus_genotypes` and populates `discrepancies` from the
  active set of `genotype_calls` via the `consensus_v1` rule. The merge
  pipeline:
  - Resolves each `variants_master` row using the documented branch table
    (concordant / single-source / no-call-diff / palindromic-ambiguous /
    non-palindromic-strand-flip / unresolvable-mismatch).
  - Detects tier-3 strand-flip partners across `variants_master` rows at
    the same `(chrom, pos_grch38)` and rewrites both rows' consensus to
    `disagreement_resolved` with `flipped_strand_match` discrepancies.
  - Is idempotent: `DELETE`s both tables and rebuilds inside one
    transaction, so re-running after a re-ingest refreshes the merged view.
  Severity escalation to `critical` for ACMG SF variants is deliberately
  deferred to a Phase 5+ enrichment job (the `is_acmg_sf` flag is not yet
  populated). Tier-2 (rsid-based matching across positions) is deferred to
  Phase 5 once `variant_aliases` lands.
- `genome merge` CLI command that runs the pipeline against the configured
  DuckDB and prints per-method / per-type / per-severity rollups plus the
  shared-call concordance rate.
- 52 new tests covering: strand-helper unit cases (complement and
  palindrome detection across A/T, C/G, and non-DNA tokens), every
  `consensus_v1` branch as a direct `resolve()` call (`both_concordant`,
  `genotype_mismatch` resolvable and unresolvable, `strand_ambiguous`,
  `no_call_diff`, `platform_unique` both directions, double no-call), and
  end-to-end DB round-trips for each discrepancy type plus the tier-3
  cross-row strand-flip, idempotence, the merge-result summary counts,
  the CLI smoke, and the `call_comparison_v` view picking up every
  consensus row.
- **Phase 4 Beagle pivot — local Beagle 5.5 runner.** New
  `genome.imputation.beagle_runner` module pipes the per-chromosome
  upload VCFs (produced by the existing `prepare_run`) through Beagle
  5.5 against the local 1000 Genomes Phase 3 reference panel. Runs one
  `java -jar beagle.jar` subprocess per chromosome with `ref=`, `map=`,
  `gt=`, `out=`, `nthreads=`, `ne=`, and `impute=true` arguments;
  streams Beagle's stderr line-by-line into structlog at INFO so a
  long-running invocation is observable. Defaults: heap `-Xmx8g`,
  `ne=1_000_000` (Beagle's outbred-human default), threads
  `max(1, os.cpu_count() - 1)`. Resumable: a chromosome whose output
  VCF already exists and parses cleanly with cyvcf2 is skipped unless
  `--force` is passed. Partial failures are recoverable — one
  chromosome's failure does not abort the rest of the run; the
  `BeagleRunResult` reports which chromosomes succeeded, failed, and
  were skipped. Status transitions: `pending` → `processing` on first
  chromosome start; `completed` (stamps `completed_at`) when every
  attempted chromosome succeeds; `failed` when every attempted
  chromosome fails; mixed outcomes leave the run at `processing` so
  the user can retry the failures without losing the successes.
  chrY is intentionally skipped (the 1000G high-coverage release omits
  chrY); the runner logs the skip and continues. Output VCFs land at
  `archive/imputation/run_<id>/result/chr<N>.vcf.gz` with 0600 perms,
  the same path the existing `import_result` step picks up via
  `archive.list_result_vcfs()` (whose glob is now `chr*.vcf.gz` so
  Beagle output and any legacy `chr*.dose.vcf.gz` are both found).
  New CLI subcommand
  `genome imputation run <id> [--chromosomes <list>] [--threads <n>] [--memory-gb <n>] [--ne <n>] [--force]`.
  Pre-flight checks: the run row exists and is in `pending` /
  `processing` (or `completed` / `failed` with `--force`), Java 8+ is
  on PATH (parsed from `java -version`), and the reference panel is
  fully populated (the error points the user at `genome imputation
  panel install`).
- **Phase 4 Beagle pivot — reference-panel management.** New
  `genome.imputation.reference_panel` module owns the local Beagle
  reference-panel layout under `~/.cache/genome/imputation/` by default
  (overridable via the new `imputation_panel_root` setting). Manages the
  Beagle 5.5 JAR (`beagle.27Feb25.75f.jar`), the PLINK GRCh38 genetic-map
  archive (`plink.GRCh38.map.zip` — extracted into per-chromosome `.map`
  files), and the per-chromosome 1000 Genomes Phase 3 GRCh38 reference
  panel VCFs (autosomes 1-22 plus X; chrY is not part of the
  high-coverage phased release and `panel_for_chrom('Y')` returns
  `None`). All downloads flow through the existing audited
  `ExternalClient`, so every fetch is gated on `external_calls_enabled`
  and produces intent + result audit rows; downloaded files land with
  `0600` permissions, directories with `0700`. New CLI subcommands
  `genome imputation panel status` (validates the on-disk layout) and
  `genome imputation panel install [--force] [--chromosomes <list>]`
  (downloads anything missing). The `--chromosomes` filter targets the
  per-chrom panels only; the JAR and genetic-map archive are left alone
  for partial-install / recovery flows. Documents the upstream URL
  choice in a constants block at the top of the module: the Beagle
  authors host pre-built bref3 panels for b37 only, so we fetch the
  GRCh38 VCFs from EBI's 1000 Genomes high-coverage phased release —
  Beagle 5.5 accepts either format for its `ref=` argument.
- **Phase 4 — imputation roundtrip (TopMed era; later removed in favor
  of local Beagle, see below).** New `genome.imputation` package and
  `genome.privacy.external_client` introduced the workflow for sending the
  merged genotype set through the TopMed Imputation Server and ingesting the
  ~30M-variant imputed result. The workflow was partially manual — TopMed
  does not expose a programmatic upload API for free-tier users — but the
  local code handled preparation, status polling, download, decryption
  hand-off, and ingest. Highlights:
  - `genome imputation prepare` exports `consensus_genotypes` joined to
    `variants_master` as per-chromosome VCFv4.2 files (gzipped, GRCh38,
    chr-prefixed contigs, dosage-derived genotypes) under
    `archive/imputation/run_<id>/upload/`, plus a JSON manifest, and inserts
    an `imputation_runs` row in `status='pending'`.
  - `genome imputation status <id> --status-url <url>` polled TopMed
    (Cloudgene API) and mapped `state` to the `imputation_runs.status` enum
    (`pending` / `processing` / `completed` / `failed`). Idempotent — safe
    to re-run; stamps `submitted_at` once.
  - `genome imputation download <id> --download-url <url> --password <pw>`
    streamed the encrypted result archive to
    `archive/imputation/run_<id>/result/topmed_result.zip`, recorded the
    SHA-256 on `imputation_runs.output_file_hash_sha256`, and short-
    circuited when the archive was already present with a matching hash.
  - `genome imputation import <id>` streams the decrypted per-chromosome
    VCFs through cyvcf2, batches 50K rows per Arrow Table, and bulk-inserts
    into `variants_master` and `genotype_calls`
    (`source='topmed_imputed'`, `is_imputed=TRUE`, `imputation_panel='topmed_r3'`,
    `imputation_r2` from INFO/R2). Computes a `sample_qc` row for the imputed
    sample and backfills `variants_output`, `mean_r2`,
    `variants_above_r2_0_3`, and `variants_above_r2_0_8` on the run row.
    Memory stays bounded by streaming per chromosome. Operational controls
    on the command:
      - `--r2-threshold <float>` (default `0.3`) skips variants whose
        `INFO/R2` falls below the threshold; the value used is persisted on
        `imputation_runs.r2_threshold` (new column, see schema change below).
      - `--chromosomes <list>` (comma-separated) limits the import to the
        named chromosomes — useful for partial recovery or testing.
      - `--batch-size <int>` (default `50_000`) tunes the Arrow Table batch
        size for memory-constrained machines.
      - `--dry-run` parses each VCF and reports expected variant counts plus
        an estimated wall-clock time, writing nothing to the database.
      - `--force-reimport` is required to re-import against a run whose
        `variants_output` is already populated; without it the command
        aborts with a clear "already imported" error. The existing
        supersession-over-update pattern handles the data-side semantics
        once the flag is present.
  - `genome imputation list` enumerates all `imputation_runs` rows for
    quick state-of-the-world inspection.
  - 30M extrapolation: a 1M-row benchmark test on the streaming ingest
    completes in well under the 60-second guard ceiling, putting the
    full TopMed roundtrip at roughly 30 minutes.
- **Audited external HTTP client** (`genome.privacy.external_client.ExternalClient`).
  Single chokepoint for any network call the app makes. Enforces
  `user_preferences.external_calls_enabled` (raising a clear, actionable
  error when disabled), hashes request bodies SHA-256 and writes the hash to
  `audit_log.external_payload_hash` (never the body — privacy posture is
  locked in `CLAUDE.md`), and produces one intent row plus one outcome row
  per attempt so a process killed mid-call still leaves an audit trace.
  Built on httpx; injectable `httpx.Client` makes mocked transports trivial
  in tests. Future Phase-5+ consumers (MyVariant.info, PubMed, R2, etc.)
  will reuse it untouched.
- **`genome config get` / `genome config set`** CLI subcommands. Read and
  write `user_preferences` rows. Every `set` writes a `config_change` row
  to `audit_log` so preference history is auditable. The most common use is
  flipping `external_calls_enabled` before a network-using flow such as
  `genome imputation panel install`.
- 81 new tests covering: the audited HTTP client's enable-check / hash /
  audit invariants across success, network error, HTTP 4xx, HTTP 5xx,
  retry, and download paths; `imputation_runs` CRUD round-trips; archive
  layout (path shape, permissions, listing); VCF export field shapes and
  TopMed-specific filters (SNV-only, biallelic, genotype rendering, contig
  order); TopMed state-code mapping (integer codes and string labels);
  status polling DB-state transitions and idempotence; download streaming
  and hash-match short-circuit; the streaming ingest's schema-correct
  writes, no-call handling, supersession-over-update on re-import; a 1M-row
  benchmark; and CLI smoke for every new subcommand.
- `LiftoverPyLib` engine, the `liftover`-package wrapper. The `Liftover`
  Protocol is unchanged; `IdentityLiftover` and the renamed
  `PyLiftoverWrapper` (formerly `PyLiftover`) still satisfy it.
- `--liftover-engine {auto|liftover|pyliftover}` CLI flag (and matching
  `liftover_engine=` argument on `ingest_file`). `auto` (default) prefers the
  `liftover` package and falls back loudly to `pyliftover` with an INFO log;
  explicit engine choices raise rather than silently falling back.
- Engine-selection tests and a 100K-position throughput benchmark
  (`< 60 s` ceiling) in `backend/tests/test_ingest_liftover.py`.
- `pyarrow>=17.0.0` as an explicit runtime dependency (previously transitive
  via DuckDB) so the writer's bulk-load path keeps working if DuckDB ever
  drops the implicit dependency.
- `backend/tests/test_ingest_writer.py` with a `_stage_calls` round-trip
  correctness test (preserving `ord`, empty-allele → NULL mapping, list
  columns) and a 100K-row benchmark (< 2 s ceiling) so the bulk-load path
  cannot regress to `executemany` undetected.

### Changed
- **Phase 4 pivots from external TopMed imputation to local Beagle 5.5
  imputation.** Real-data verification showed TopMed rejects single-sample
  submissions in ~50 seconds with a 20-sample minimum that cannot be
  worked around safely; `finding-006-topmed-not-viable-for-personal-genomics.md`
  documents the rationale. `ROADMAP.md` Phase 4 is updated to describe
  the local Beagle workflow (per-chromosome VCFs, 1000 Genomes Phase 3
  reference panel on disk, `imputation_dr2` from Beagle's INFO/DR2, new
  `genome imputation panel install | status` subcommands).
- **`vcf_export` and `ingest` produce/consume Beagle-flavored outputs.**
  Prepare now writes `imputation_runs.imputation_server='beagle'` and
  `reference_panel='1000g_phase3_grch38'`, and the manifest gains an
  `imputation_tool='beagle_5.5'` field (dropping the TopMed-era
  `topmed_recommended_compression` / `compression_note` keys). Import
  writes `genotype_calls.source='beagle_imputed'` and
  `imputation_panel='1000g_phase3_grch38'` by default; the R²
  extractor now tries `INFO/DR2` (Beagle's native dosage-R² field)
  before falling back to `R2` / `Rsq`, so output from Beagle, TopMed,
  and older Minimac releases all parse correctly. The imputable
  chromosome set expands to autosomes + X + Y (Y was excluded under
  TopMed r3, which did not impute it; under Beagle 5.5 it is included
  at the prepare layer, and the runner warns and skips Y if the 1000G
  Phase 3 bref3 release lacks Y coverage).
- Migrated `[tool.uv.dev-dependencies]` to `[dependency-groups]` in
  `pyproject.toml` per uv's deprecation notice. No behavior change.
- Lift-over now uses the [`liftover`](https://pypi.org/project/liftover/) PyPI
  package (C++/CFFI-backed) as the default engine. It runs ~10–50× faster than
  `pyliftover` on whole-array exports and installs cleanly via `uv sync` with
  no system tooling. The previous bcftools `+liftover` plugin direction was
  abandoned because building `freeseek/score` against the user's htslib
  required `-fPIC` rebuilds that were environmentally fragile.

### Fixed
- **Phase 4 cleanup (session B): `consensus_v1` now handles imputed-only
  variants and treats imputation as confirming evidence.** Real-data
  verification after the Phase 4 ingest landed showed 2,267,751 of
  3,210,371 consensus rows misclassified as `unresolvable` because the
  merge step's pivot and resolver only knew about `23andme` and
  `ancestry`. Extended `_fetch_variant_pairs` to also pivot
  `beagle_imputed` (plus its per-variant `imputation_r2`), added a
  third `imputed` field to `VariantPair`, and rewrote
  `consensus.resolve` so (a) chip-only resolutions stand byte-for-byte
  when at least one chip call is active, with the imputed call's
  `call_id` appended to `contributing_calls` as confirming evidence,
  and (b) variants with only a `beagle_imputed` call resolve to
  `consensus_method='imputed_only'` with `is_imputed=True` and the
  imputation R² propagated to `consensus_genotypes.consensus_r2`.
  Tier-3 cross-row strand-flip candidacy now excludes any row that
  carries an active imputed call, since the tier-3 rewrite replaces
  `contributing_calls` with just the paired chip call_ids and would
  otherwise drop the imputed call from the audit trail. Rule label
  stays `consensus_v1` — no schema or version bump. After the fix the
  Phase 3 numbers are preserved exactly (`both_concordant=120,516`,
  `disagreement_resolved=106`, `single_source=821,998`,
  `strand_flip_resolutions=106`, shared-call concordance=1.0000) and
  the new `imputed_only=2,267,751` bucket lands where the
  `unresolvable` rows previously did. See updated `docs/consensus.md`
  and the new "Real-data observations" entry #3 in `CLAUDE.md`.
- **Phase 4 cleanup (session A) second follow-up: remove the htslib
  log-level manipulation entirely.** The previous follow-up's
  `silence_htslib_contig_warnings` context manager imported
  `set_htslib_log_level` from `cyvcf2.cyvcf2`, but that symbol does not
  exist in the installed cyvcf2 version, raising ImportError on every
  read path that loaded the helper. The downstream effect was 35
  pytest failures rooted in the same import, and a real-data chr22
  re-run that left the Beagle output at `0o644` instead of `0o600`
  because `restrict_file` could not run after the silence context
  raised. Deleted `backend/src/genome/imputation/_htslib.py`, removed
  the `with silence_htslib_contig_warnings():` wrappers from
  `ingest._stream_chromosome` and `_count_chromosome_variants`,
  reverted the imports, and deleted the suppression-specific tests.
  The contig warning is now documented as expected log output in
  `docs/runbooks/imputation.md` — it fires once per cyvcf2 file open
  (about 23 lines per full-genome import) and is cosmetic. The
  map-prefix fix and the timestamp fixes from session A remain in
  place.
- **Phase 4 cleanup (session A) follow-up: stop wrapping
  `_vcf_parses_cleanly` with the htslib log-level suppression.** Real-
  data re-run on chr22 after the first cleanup landed showed the
  post-Beagle validator rejecting a 1M-record output as invalid even
  though direct cyvcf2 iteration of the same file succeeded. The
  validator reads at most one record per call (vs. the streaming ingest
  which reads millions), so the contig warning fires at most once per
  chromosome there — not spam. Removed the
  `silence_htslib_contig_warnings()` wrapper from
  `beagle_runner._vcf_parses_cleanly`; the suppression remains in place
  at the per-record streaming sites (`ingest._stream_chromosome`,
  `_count_chromosome_variants`) where it actually matters. Added a
  regression test that asserts `_vcf_parses_cleanly` returns True on a
  Beagle-shaped header-less VCF — the exact provocation that surfaced
  the failure.
- **Phase 4 cleanup (session A): three small Beagle-pipeline defects surfaced
  by real-data verification.** See
  `docs/findings/finding-007-beagle-real-data-cleanup.md` for the full
  write-up.
  - `reference_panel._install_genetic_map` now rewrites each extracted
    PLINK GRCh38 `.map` file so column 1 is `chr`-prefixed
    (`22` → `chr22`, `23` → `chr23`). The upstream Browning Lab archive
    ships bare numeric labels, but Beagle 5.5's reference panels and our
    prepared input VCFs both use `chr`-prefixed contigs, and Beagle does
    exact-string chromosome matching. The rewrite is atomic (write
    `<path>.tmp`, rename), preserves `0600` permissions, and is
    idempotent (already chr-prefixed files are left byte-identical).
  - htslib's per-record `[W::vcf_parse] Contig 'chr<N>' is not defined
    in the header` warning is now suppressed at the imputation module's
    cyvcf2 read sites (`beagle_runner._vcf_parses_cleanly`,
    `imputation/ingest.py` `_stream_chromosome` / dry-run
    `_count_chromosome_variants`). A new private
    `genome.imputation._htslib.silence_htslib_contig_warnings()` context
    manager lowers htslib's global log level to `HTS_LOG_ERROR` for the
    duration of one read and restores `HTS_LOG_WARNING` (htslib default)
    on exit, so real parse errors continue to surface and unrelated
    cyvcf2 readers elsewhere are unaffected.
  - `imputation_runs.submitted_at` is now stamped on the
    `pending` → `processing` transition in the local Beagle runner
    (`beagle_runner._move_to_processing_if_pending`), and
    `imputation_runs.completed_at` is now stamped on the
    `processing` → `completed` transition in the import step
    (`ingest._execute_import`). The `update_status` helper's
    `COALESCE(..., CURRENT_TIMESTAMP)` semantics are unchanged; the bug
    was call sites omitting the `set_submitted=True` / `set_completed=True`
    flags. The invariant ("every transition out of pending stamps
    submitted_at; every transition to completed stamps completed_at") is
    now documented on the helper's docstring and at each transition
    site.
- Seeded default for `user_preferences.external_calls_enabled`. Previously
  seeded as `true`, contradicting the locked privacy decision in CLAUDE.md
  (#9) and the documented schema. New databases now correctly seed this as
  `false`; the schema markdown's suggested-seed table is corrected to
  match. The privacy master switch is fail-closed by default.
- Blocked external-call attempts (when `external_calls_enabled=false`) now
  write audit rows. Previously, the disabled check in
  `genome.privacy.external_client._audited_attempt` raised before the
  intent row was inserted, so blocked attempts left no database trace —
  only stdout. The intent row is now written before the enabled-check, and
  a second `blocked` result row is written before
  `ExternalCallsDisabledError` is raised. Existing success and failure
  audit pairs are unchanged.
- Tier-3 strand-flip resolutions in the merge pipeline were being classified
  as `genotype_mismatch` discrepancies. They are now recorded as a new
  `strand_flip_resolved` discrepancy type with severity `info`. The mechanism
  was already correct — only the classification changed. Real-data
  verification on the 23andMe + Ancestry corpus found 106 such rows that
  should not have been logged as mismatches. The new enum value is added to
  `discrepancy_type_enum` (schema and DDL), the docs/consensus.md catalog
  and severity table are updated, and the merge concordance computation no
  longer needs to subtract resolved flips from the discordant total because
  they now live in their own bucket.

### Removed
- TopMed Imputation Server client and its CLI surface: deleted
  `backend/src/genome/imputation/topmed_client.py` (including
  `TopMedClient`, `TopMedStatus`, `check_status`, `download_result`,
  `TOPMED_ENDPOINT_LABEL`, `TOPMED_PANEL`, and the Cloudgene state-code
  mapping), removed the `genome imputation status` and
  `genome imputation download` subcommands from `genome.cli`, dropped the
  TopMed symbols from `genome.imputation.__all__`, and deleted
  `backend/tests/test_imputation_topmed_client.py` along with the
  corresponding `status`/`download` plumbing checks in
  `test_cli_phase4.py`. Phase 4 pivots from TopMed to local Beagle
  imputation (see
  `docs/findings/finding-006-topmed-not-viable-for-personal-genomics.md`);
  the new flow lands in subsequent commits.

### Schema
- Added `imputation_runs.r2_threshold` (`DOUBLE`, nullable) to record the
  import-time R² filter applied to a run. Rows that predate this column
  remain `NULL`. Schema markdown
  (`docs/schemas/schema_group_1_genotype_data.md`) and the extracted DDL
  (`ddl/group_1_genotype.sql`) are updated together; existing local DuckDB
  files need a rebuild (`rm -rf data/ && uv run genome init`) per the
  CLAUDE.md schema-change convention.
- Added `'beagle_imputed'` to `source_enum` so post-Beagle imputed calls
  carry a distinct source label from the existing `'topmed_imputed'` (which
  is retained for backward compatibility per finding-006 even though
  TopMed-imputed calls never landed in real data). Supports the Phase 4
  pivot to local Beagle 5.5 imputation. Schema markdown
  (`docs/schemas/schema_group_1_genotype_data.md`) and the extracted DDL
  (`ddl/group_1_genotype.sql`) are updated together; existing local DuckDB
  files need a rebuild (`rm -rf data/ && uv run genome init`) per the
  CLAUDE.md schema-change convention.

### Performance
- Rewrote `writer._stage_calls` to bulk-load via PyArrow Table registration +
  `INSERT ... SELECT` instead of DuckDB's `executemany`. `executemany` does
  not batch-bind — it re-prepares and re-executes the statement per row — so
  staging a 631K-variant 23andMe export took ~14 min on Windows and ~32 min
  on macOS even though the surrounding set-based joins and lift-over were
  already optimized. The Arrow path stages the same batch in roughly one
  second, bringing total wall-clock ingest below the < 60s target while
  producing byte-identical rows in `variants_master`, `genotype_calls`,
  `consensus_genotypes`, and `discrepancies`.

### Documentation
- Added `docs/findings/` directory capturing real-data observations from
  Phase 2, Phase 3, and Phase 4: lift-over engine selection, platform
  overlap and concordance, chip composition differences, DuckDB bulk-load
  pattern, deferred improvements list, TopMed-not-viable rationale for
  the Beagle pivot, and the Phase 4 Beagle real-data cleanup write-up.
- Documented schema-change-requires-rebuild convention in `CLAUDE.md`.
- Documented 23andMe v5 vs Ancestry v2 chip composition differences
  (Y-chromosome coverage, heterozygosity rate ranges) in `CLAUDE.md`.
- Added `docs/consensus.md` documenting the `consensus_v1` rule, dosage /
  confidence conventions, and the rule's versioning workflow so future
  rule changes can be tracked cleanly.
- Stubbed `docs/runbooks/imputation.md` ahead of Phase 4 implementation,
  then rewrote it for the local Beagle workflow: panel install (one-time),
  prepare, run, import — with Beagle-specific flags, OOM guidance, and the
  privacy posture that no genome data leaves the machine.

Phase 3 (merge & discrepancy detection) and Phase 4 (local imputation
via Beagle 5.5) closed.

## [0.2.3] — 2026-05-07

Phase 2 ingestion: post-liftover non-canonical contig filter ([PR #6](https://github.com/vidalcastaneda12-source/dna_insights_v1/pull/6)).

### Fixed
- Drop lifted variants whose post-liftover chromosome lands on a non-canonical
  GRCh38 contig (e.g. `chr4` → `4_GL000008v2_random`). The post-lift chromosome
  previously flowed straight into `NormalizedCall.chrom` and exploded at the
  writer's `chromosome_enum` cast. The normalize step now re-runs
  `normalize_chrom` on the post-lift chromosome and drops the row when the
  result is `None`, mirroring the parse-time behavior.

### Added
- `ingestion_runs.variants_dropped_lift_to_non_canonical` counter so the
  parse-time and lift-time failure modes stay distinguishable.
- Pipeline and normalize tests covering canonical-→-non-canonical lift drops.

### Changed
- Moved `normalize_chrom` from `parsers.py` to `models.py` so both the parse
  and normalize stages can call it without a layering inversion.
- CLI `genome ingest` output now reports `dropped_lift_to_non_canonical`.
- CLAUDE.md documents the post-lift filter alongside the parse-time filter.

## [0.2.2] — 2026-05-07

Phase 2 ingestion: generalized non-canonical contig filter ([PR #5](https://github.com/vidalcastaneda12-source/dna_insights_v1/pull/5)).

### Fixed
- Generalized the parse-time chromosome filter from alt-only to all
  non-canonical GRCh38 contigs. Real 23andMe v5 ingests still failed after
  the alt-only fix because exports also carry rows on unlocalized
  (`*_random`), unplaced (`Un_*` / `chrUn_*`), and decoy (`*_decoy`)
  contigs, all of which exploded against the `chromosome_enum` cast.
  The filter now keeps only labels that resolve to `{1..22, X, Y, MT}`
  after the existing 23/24/25/26 alias remap.

### Changed
- Renamed `ParseStats.dropped_alt_contig` and
  `ingestion_runs.variants_dropped_alt_contig` to `*_non_canonical`;
  updated `IngestResult`, writer, pipeline, CLI output, and parser log keys.
- Re-extracted DDL from updated schema markdown.
- Refreshed the CLAUDE.md ingestion convention to describe the broader scope.

## [0.2.1] — 2026-05-07

Phase 2 ingestion: alt-contig variant filter ([PR #4](https://github.com/vidalcastaneda12-source/dna_insights_v1/pull/4)).

### Fixed
- Parsers now drop and count variants on GRCh38 alt contigs (e.g.
  `8_KI270821v1_alt`, `19_KI270938v1_alt`) instead of letting them reach
  the DuckDB `chromosome_enum` cast and crash mid-ingest with
  "Could not convert string '8_KI270821v1_alt' to UINT8".

### Added
- `ingestion_runs.variants_dropped_alt_contig` column surfacing the per-run
  drop count; parsers emit a debug log per row and a single info-level
  end-of-parse summary.
- Documented the filter in CLAUDE.md as intentional and matching standard
  clinical bioinformatics practice.

## [0.2.0] — 2026-05-06

Phase 2: raw-export ingestion for 23andMe and Ancestry ([PR #3](https://github.com/vidalcastaneda12-source/dna_insights_v1/pull/3)).

### Added
- Streaming parsers for 23andMe and Ancestry exports with header / build /
  chip detection, chrom-alias normalization (23/24/25/26 → X/Y/MT),
  no-call handling, and 23andMe indel (I/D) support.
- Normalization pass: alphabetical allele ordering, palindrome flagging
  (A/T, C/G), variant-type classification, and lift-over via an injectable
  `Liftover` protocol (`Identity` for tests / native GRCh38, `PyLiftover`
  wrapper requiring an explicit local chain file; auto-download disabled
  per local-first privacy policy).
- Writer that stages a batch into a temp table, dedups against the
  `variants_master` uniqueness constraint, deactivates prior
  `(variant, source)` active calls (supersession over update), inserts new
  `genotype_calls`, and refreshes the `has_genotyped_call` denormalized
  flag — all in one transaction.
- Sample QC: call rate, autosomal heterozygosity, sex inference from chrX
  het rate + chrY call counts, and pass / warn / fail status rollup.
- Pipeline orchestrator that hashes + archives the input file (`0600`),
  runs parse + normalize + QC, and writes a single `ingestion_runs` row
  with final counts.
- `genome ingest --source {23andme|ancestry} <path> [--chain-file ...]`
  CLI command.
- Tests covering parser edge cases (build detection, chrom aliases,
  no-call, indel, malformed rows), normalization (palindrome, lift
  failures, cross-chromosome lifts), QC (call rate / het / sex), and an
  end-to-end pipeline against both fixtures (archive layout, re-ingest
  deactivation, dual-source concordance, CLI smoke).

## [0.1.1] — 2026-05-06

Schema correction: DuckDB-clean DDL and SQLCipher FTS5 prerequisite docs ([PR #2](https://github.com/vidalcastaneda12-source/dna_insights_v1/pull/2)).

### Fixed
- DuckDB rejected three constructs the schema docs were emitting verbatim:
  partial `CREATE INDEX (WHERE ...)`, cross-group
  `ALTER TABLE ... ADD CONSTRAINT`, and `CREATE VIEW` with ambiguous
  `USING` joins. `init_schema.py` previously silently skipped any failing
  statement on the DuckDB side, which hid bugs (a view referenced a
  non-existent column) and meant several indexes and every view never
  landed.
- `pgs_extremes_v` referenced a non-existent `confidence` column on
  `derived_pgs`; the view now surfaces `low_coverage` instead.

### Changed
- Stripped `WHERE` from DuckDB `CREATE INDEX` statements (groups 1 and 3);
  the SQLite group 5 schema keeps its partial indexes.
- Replaced `USING (variant_id)` / `USING (insight_id)` / `USING (pgs_id)`
  joins with explicit `ON a.col = b.col` across groups 1–4.
- `init_schema.py` now applies every DDL statement and raises on failure
  rather than tolerating unsupported constructs.
- Re-extracted DDL into `ddl/*.sql` from the updated schema markdown.

### Removed
- Cross-group `ALTER TABLE ... ADD CONSTRAINT` blocks (and the
  `ddl/alters_cross_group.sql` file). Those references are now noted as
  application-validated, mirroring the cross-DB pattern in CLAUDE.md.
- The `_PARTIAL_INDEX_RE` / `_ADD_CONSTRAINT_RE` / `_CREATE_VIEW_RE`
  workarounds, the `tolerate_unsupported` flag, and the
  `DUCKDB_ALTERS_FILE` setting in `init_schema.py`.

### Added
- README.md "Prerequisites" section documenting the SQLCipher 4.5.6 +
  FTS5 source build (Ubuntu's `libsqlcipher-dev` ships without FTS5,
  and `app.db.notes_fts` requires it).
- CLAUDE.md "Environment requirements" section codifying the FTS5
  build requirement and explicitly forbidding "fixing" an FTS5 install
  failure by removing `notes_fts` from the schema.
- Tests covering the previously-skipped views, partial indexes, and the
  new "errors propagate" contract for `init_schema`.

## [0.1.0] — 2026-05-06

Phase 1: project bootstrap ([PR #1](https://github.com/vidalcastaneda12-source/dna_insights_v1/pull/1)).

### Added
- Repository layout: `backend/src/genome/{analyze,annotate,api,db,ingest,insights,jobs,privacy}`,
  `backend/tests/`, `ddl/`, `docs/schemas/`, `frontend/`.
- DDL extracted verbatim from `docs/schemas/` into
  `ddl/group_{1..5}_*.sql` plus `ddl/alters_cross_group.sql` for the
  cross-group `ALTER`s.
- DuckDB and SQLCipher connection helpers with `0600` file permissions;
  the SQLCipher wrapper sets `PRAGMA key` first, then `foreign_keys` and
  WAL.
- `init_databases()` applies groups 1–4 to `genome.duckdb`, group 5 to
  `app.db`, and seeds `profiles` + `user_preferences`. Idempotent.
- Typer CLI with `genome init`, `genome status`, and `genome version`.
- Tests covering config loading, schema init, idempotency, FTS5,
  passphrase rejection, and `0600` file permissions.
- Project metadata (`pyproject.toml`), `.env.example`, `.gitignore`,
  README.md, CLAUDE.md, and ROADMAP.md.

[Unreleased]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.2.3...v0.4.0
[0.2.3]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/vidalcastaneda12-source/dna_insights_v1/releases/tag/v0.1.0
