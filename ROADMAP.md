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

- Per-source downloaders (ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog metadata, gnomAD filtered, dbSNP filtered)
- Each writes to `annotation_source_versions` and the per-source table; supersession is via the version-pointer pattern (see CLAUDE.md #7 and [`finding-010`](docs/findings/finding-010-version-pointer-supersession-pattern.md))
- Refresh `variant_annotations_index` rollup across all loaded sources
- Profile-level QC rollup combining per-source `sample_qc` rows into one per-profile answer (finding-005 #2)
- `variants_master.is_acmg_sf` flag enrichment from the curated ACMG SF v3.x gene list intersected with ClinVar rows (finding-005 #5)
- CLI: `genome annotate refresh [--source ...]`

Sub-phase status:
- [x] 5.0 — Loader scaffold (PR #33)
- [x] 5.1a — PharmGKB loader (PR #34)
- [x] 5.1b — CPIC loader (PR #35)
- [x] 5.2 — ClinVar loader (PR #36)
- [x] 5.3 — GWAS Catalog loader (PR #38)
- [x] 5.4 — PGS Catalog metadata loader (PR #39)
- [ ] 5.5 — gnomAD filtered (next)
- [ ] 5.5b — gnomAD PGS extension. Gated on Phase 6 `pgs_score_weights` landing. Extends the active gnomAD source-version's coverage to PGS-component variants. Not a version bump — appends to the same active `source_version_id`. Verification: not applicable until Phase 6 lands. See [`finding-011`](docs/findings/finding-011-gnomad-three-way-intersection.md).
- [ ] 5.6 — dbSNP filtered
- [ ] 5.7 — `variant_annotations_index` refresh
- [ ] 5.8 — Profile-level QC rollup

Follow-ups (small PRs, slot between sub-phases as convenient):
- PharmGKB / CPIC `already_current=True` cosmetic cleanup (finding-010 #12)
- HEAD-request-failure version-label fallback behavior — capture as its own finding (finding-010 #13)
- Cleanup of orphan rows under superseded `source_version_id`s (finding-010 #14)
- Cross-source generalization of the version-pointer pattern (finding-010 #15)
- `MAPPED_TRAIT_URI` truncation entry for finding-005 (deferred from sub-phase 5.3)

Enrichment (depends on ClinVar from 5.2, can slot any time after 5.2):
- `variants_master.is_acmg_sf` flag population — populate via the curated ACMG SF v3.x gene list intersected with ClinVar rows. Phase 3 deferred ACMG SF severity escalation pending this flag (finding-005 #5). Consumed by Phase 6's ACMG SF detection pipeline.

Backfills (require dbSNP from 5.6, slot after 5.6 lands):
- Canonical REF/ALT for strand-flip dedupe (finding-005 #1)
- Tier-2 rsID matching via `variant_aliases` (finding-005 #4)
- Hom-only recovery via canonical REF/ALT (finding-005 #6)

Deferred from Phase 5 to later phases:
- VEP local runner — fits Phase 6's runner pattern (Beagle / PharmCAT / HIBAG); structurally a subprocess tool that runs against user variants, not a download-and-load source. The `variant_annotations_index` ships with the VEP column NULL initially and gets refreshed when VEP lands in Phase 6.
- Genes / traits / pathways dictionary tables — primarily serve insight generation and rendering; defer to Phase 7. The loaders we ship in Phase 5 carry gene symbols and trait IDs inline, so the index does not need the dictionaries to do its joins.

**Verification:** all seven annotation source loaders complete (ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog metadata, gnomAD, dbSNP); `variant_annotations_index` populated with the expected per-variant join across them; queries against `variant_full_v` view return joined annotations; profile-level QC rollup combines per-source `sample_qc` rows into a single per-profile answer that resolves CLAUDE.md "Real-data observations" #1; `variants_master.is_acmg_sf` flag is populated on the expected gene set.

## Phase 6 — Analysis pipelines
- PRS computation against PGS Catalog (overlapping-only weights)
- PharmCAT integration → `derived_pgx_phenotypes`
- Carrier detection rules
- ACMG SF detection
- HIBAG → `derived_hla_typing`
- VEP local runner against user variants → populates VEP columns in `variant_annotations_index` via the rollup refresh.
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
