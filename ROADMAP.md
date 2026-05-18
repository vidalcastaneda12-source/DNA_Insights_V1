# Build Roadmap

Phases are sequential. Do not start phase N+1 until phase N's verification passes.

**Current phase:** Phase 5 (reference annotation loaders) — in progress.

## Phase 1 — Foundation (this is the bootstrap)

**Status:** complete.

Project layout, DDL extraction, DB initialization, config, CLI, basic tests. **Verification:** `genome init` works on a clean checkout; `pytest` green; `mypy --strict` clean.

## Phase 2 — Ingestion

**Status:** complete (see findings 001, 003, 004).
- Parse 23andMe and Ancestry raw exports
- Normalize to GRCh38 (lift-over via `pyliftover` or chain files)
- Strand resolution (with palindrome flagging)
- Multi-allelic split
- Populate `variants_master`, `genotype_calls`, `ingestion_runs`
- Compute `sample_qc`
- CLI: `genome ingest --source 23andme path/to/file.txt`

**Verification:** ingest both fixture files; `variants_master` populated; `sample_qc` row produced; tests cover format edge cases.

## Phase 3 — Merge & discrepancy detection

**Status:** complete (see findings 002, 005).

- Variant matching via three-tier strategy (chr:pos:ref:alt → rsid → fuzzy with strand)
- Compute `consensus_genotypes` via `consensus_v1` rule
- Detect and catalog discrepancies (six types, four severity levels)
- CLI: `genome merge`

**Verification:** known mismatches in fixture data are correctly flagged; concordance rate computed; per-source counts match the Venn-diagram view.

## Phase 4 — Local imputation (Beagle 5.5)

**Status:** complete (see findings 006, 007).

- Export merged consensus calls to per-chromosome VCFs (autosomes + X + Y)
- Run Beagle 5.5 locally against the 1000 Genomes Phase 3 reference
  panel on GRCh38, with the corresponding PLINK genetic map
- Parse imputed VCFs; integrate with imputation_dr2 (Beagle's INFO/DR2)
  per call
- Reference panel management: standard on-disk location under
  ~/.cache/genome/imputation/, validation, optional one-time download
- CLI: `genome imputation prepare | run | import | list` plus
  `genome imputation panel install | status` for one-time setup

**Verification:** end-to-end roundtrip works on chr22 alone first;
`is_imputed` flags correct; DR² distribution sane; full-genome run
completes against real 23andMe + Ancestry corpus.

## Phase 5 — Reference annotation loaders

**Status:** in progress.

- Per-source downloaders (ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog metadata, gnomAD filtered, dbSNP filtered, genes, traits, pathways)
- Each writes to `annotation_source_versions` and the per-source table
- VEP runs locally on user variants
- Refresh `variant_annotations_index`
- CLI: `genome annotate refresh [--source ...]`

Sub-phase status:
- [x] 5.0 — scaffold (PR #33)
- [x] 5.1a — PharmGKB loader (PR #34)
- [x] 5.1b — CPIC loader (PR #35)
- [x] 5.2 — ClinVar loader (PR #36)
- [x] 5.3 — GWAS Catalog loader (PR #38)
- [x] 5.4 — PGS Catalog metadata (PR #39)
- [ ] 5.5 — gnomAD filtered (next)
- [ ] 5.6 — dbSNP filtered
- [ ] 5.7 — genes / traits / pathways
- [ ] 5.8 — VEP local run + variant_annotations_index refresh

**Verification:** all sources loaded; `variant_annotations_index` populated; queries against `variant_full_v` view return joined annotations.

## Phase 6 — Analysis pipelines
- PRS computation against PGS Catalog (overlapping-only weights)
- PharmCAT integration → `derived_pgx_phenotypes`
- Carrier detection rules
- ACMG SF detection
- HIBAG → `derived_hla_typing`
- ROH via plink2
- Y/mtDNA haplogroup assignment
- Global ancestry (RFMix or admixture)
- ROH summary, genome QC
- Each writes an `analysis_runs` row capturing source versions used
- CLI: `genome analyze [pgs|pgx|carrier|acmg|hla|roh|haplogroup|ancestry|qc|all]`

**Verification:** each pipeline produces non-zero output on the merged+imputed dataset; supersession works on re-run.

## Phase 7 — Insight generation
- Per-analysis-type insight generators in `genome.insights.*`
- Versioned tier mapping functions
- Confidence rollup
- Materialized `summary_dashboard` refresh job
- Audience rendering (eli5/layperson/clinical) lazily generated
- CLI: `genome insights regenerate [--type ...]`

**Verification:** an end-to-end run produces insights for every analysis type; every insight has at least one evidence row; tier rollup is consistent.

## Phase 8 — Backend API
- FastAPI app under `genome.api`
- Endpoints: summary dashboard, drill-downs (gene / pathway / trait / variant), discrepancy view, PGx medication checker, ACMG SF dashboard, snapshot list, audit dashboard
- Natural-language query endpoint (Claude tool-use loop over the schemas)
- Job worker process (`genome jobs run-worker`)
- Audit log middleware on every request

**Verification:** OpenAPI spec covers all groups; integration tests exercise the worker; NL query produces correct DuckDB queries on fixture questions.

## Phase 9 — Frontend
- Next.js scaffold
- Home dashboard (the rollup)
- Gene drill-down
- Trait drill-down with Manhattan plot
- Variant detail page
- Discrepancy view
- Karyogram (D3) with notable variants
- Chronotype/nutrition/PGx pages
- Chat/query interface
- Doctor-ready PDF export

**Verification:** clickable end-to-end demo from dashboard to SNP detail to evidence citations.

## Phase 10 — Privacy hardening, polish, snapshots
- External call audit dashboard
- Sanitized export modes
- Snapshot create / restore / diff (the "what changed" feed)
- ClinVar-update notifications
- Performance pass on `variant_annotations_index` refresh
- Optional: `age`-encrypted backup script

**Verification:** privacy dashboard accurate; snapshot restore reproduces a prior state; backup script roundtrips.

## Out of scope for v1
- Multi-profile UI (schema is ready; UI deferred)
- Whole-genome sequencing input
- Drug-drug interaction modeling (DrugBank)
- Cloud sync / sharing
- Mobile native app
