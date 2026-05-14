# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
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
- **Imputation runbook rewritten for the local workflow.**
  `docs/runbooks/imputation.md` now documents the four-step local
  flow — panel install (one-time), prepare, run, import — with
  Beagle-specific flags, OOM guidance, and the privacy posture that
  no genome data leaves the machine.

### Added
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
- **Phase 4 — imputation roundtrip.** New `genome.imputation` package and
  `genome.privacy.external_client` introduce the workflow for sending the
  merged genotype set through the TopMed Imputation Server and ingesting the
  ~30M-variant imputed result. The workflow is partially manual — TopMed
  does not expose a programmatic upload API for free-tier users — but the
  local code handles preparation, status polling, download, decryption
  hand-off, and ingest. Highlights:
  - `genome imputation prepare` exports `consensus_genotypes` joined to
    `variants_master` as per-chromosome VCFv4.2 files (gzipped, GRCh38,
    chr-prefixed contigs, dosage-derived genotypes) under
    `archive/imputation/run_<id>/upload/`, plus a JSON manifest, and inserts
    an `imputation_runs` row in `status='pending'`.
  - `genome imputation status <id> --status-url <url>` polls TopMed
    (Cloudgene API) and maps `state` to the `imputation_runs.status` enum
    (`pending` / `processing` / `completed` / `failed`). Idempotent — safe
    to re-run; stamps `submitted_at` once.
  - `genome imputation download <id> --download-url <url> --password <pw>`
    streams the encrypted result archive to
    `archive/imputation/run_<id>/result/topmed_result.zip`, records the
    SHA-256 on `imputation_runs.output_file_hash_sha256`, and short-circuits
    when the archive is already present with a matching hash.
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
  flipping `external_calls_enabled` before the TopMed roundtrip.
- **Imputation runbook complete.** `docs/runbooks/imputation.md` now walks
  through every step end to end, including the TopMed web-UI form field
  values, encryption password handling, decryption with 7-Zip, common
  failure modes, and an audit-log review query.
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

### Documentation
- Added `docs/findings/` directory with five documents capturing real-data observations from Phase 2 and Phase 3: lift-over engine selection, platform overlap and concordance, chip composition differences, DuckDB bulk-load pattern, and deferred improvements list.
- Documented schema-change-requires-rebuild convention in `CLAUDE.md`.
- Documented 23andMe v5 vs Ancestry v2 chip composition differences (Y-chromosome coverage, heterozygosity rate ranges) in `CLAUDE.md`.
- Stubbed `docs/runbooks/imputation.md` ahead of Phase 4 implementation.

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

### Fixed
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
- `docs/consensus.md` documenting the `consensus_v1` rule, dosage /
  confidence conventions, and the rule's versioning workflow so future
  rule changes can be tracked cleanly.
- 52 new tests covering: strand-helper unit cases (complement and
  palindrome detection across A/T, C/G, and non-DNA tokens), every
  `consensus_v1` branch as a direct `resolve()` call (`both_concordant`,
  `genotype_mismatch` resolvable and unresolvable, `strand_ambiguous`,
  `no_call_diff`, `platform_unique` both directions, double no-call), and
  end-to-end DB round-trips for each discrepancy type plus the tier-3
  cross-row strand-flip, idempotence, the merge-result summary counts,
  the CLI smoke, and the `call_comparison_v` view picking up every
  consensus row.

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

### Changed
- Migrated `[tool.uv.dev-dependencies]` to `[dependency-groups]` in
  `pyproject.toml` per uv's deprecation notice. No behavior change.
- Lift-over now uses the [`liftover`](https://pypi.org/project/liftover/) PyPI
  package (C++/CFFI-backed) as the default engine. It runs ~10–50× faster than
  `pyliftover` on whole-array exports and installs cleanly via `uv sync` with
  no system tooling. The previous bcftools `+liftover` plugin direction was
  abandoned because building `freeseek/score` against the user's htslib
  required `-fPIC` rebuilds that were environmentally fragile.

### Added
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

[Unreleased]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/vidalcastaneda12-source/dna_insights_v1/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/vidalcastaneda12-source/dna_insights_v1/releases/tag/v0.1.0
