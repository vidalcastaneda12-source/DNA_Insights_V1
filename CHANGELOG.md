# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
    Memory stays bounded by streaming per chromosome.
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

### Fixed
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
