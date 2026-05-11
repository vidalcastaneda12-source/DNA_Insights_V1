# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
