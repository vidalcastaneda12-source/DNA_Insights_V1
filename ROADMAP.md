# Roadmap

This file defines the phased build plan for the DNA Insights app. Each phase has a clear scope; do not pull work from later phases into the current one.

When in doubt, the schema documents in `docs/schemas/` and the locked decisions in `CLAUDE.md` are authoritative. This file is the build sequence.

---

## Phase 1 — Foundation (this phase)

Goal: a working repo with both databases initialized, schema applied from the extracted DDL, and a CLI that proves it.

Scope:
- Repo layout with empty stubs for ingest / annotate / analyze / insights / jobs / api.
- `ddl/*.sql` extracted verbatim from `docs/schemas/`.
- `backend/src/genome/config.py` loading env from `.env`.
- `backend/src/genome/db/duckdb_conn.py`, `sqlite_conn.py`, `init_schema.py`.
- `backend/src/genome/cli.py` exposing `genome init | status | version`.
- `backend/tests/` covering config, schema init, and connections.
- `pyproject.toml`, `.env.example`, `.gitignore`, `README.md`, `CLAUDE.md`, `ROADMAP.md`.

Out of scope (deferred):
- Any ingestion logic.
- Any annotation download.
- Any analysis pipeline (PRS, PGx, carrier, ACMG SF, HLA, ROH, haplogroup, ancestry).
- Insight generation or rendering.
- The FastAPI app and the Next.js frontend.
- External HTTP calls.

Done when:
- `genome init` succeeds on a clean checkout and creates both DBs idempotently.
- `genome status` reports table counts, profile presence, and schema readiness.
- `pytest` is green; `ruff check` and `mypy --strict backend/src` pass.

---

## Phase 2 — Ingestion

Goal: parse a 23andMe and an Ancestry export end-to-end into `variants_master`, `genotype_calls`, `consensus_genotypes`, `discrepancies`, `ingestion_runs`, and `sample_qc`.

Scope:
- File parsers (23andMe and Ancestry text exports) with strand handling.
- Multi-allelic split during ingest.
- Lift-over GRCh37→GRCh38 (chain file pinned and tracked in `archive/`).
- Variant matching strategy from group 1 (primary key, rsID, fuzzy with palindrome handling).
- Discrepancy detection rules (group 1 table).
- Consensus rules (`consensus_v1`).
- Per-ingestion QC: call rate, het rate, sex check, optional concordance.
- CLI: `genome ingest <file> --source 23andme|ancestry`.

Out of scope:
- Imputation (phase 3).
- Annotation joins (phase 4).

Done when a full 23andMe + Ancestry pair ingests, merges, and reports concordance and discrepancy counts; sample QC writes a row.

---

## Phase 3 — Imputation

Goal: closed-loop TopMed imputation roundtrip.

Scope:
- Build TopMed-ready VCF from current consensus.
- Submit / monitor / download via background jobs (`imputation_upload`, `imputation_monitor`, `imputation_download`).
- Ingest imputed variants into `variants_master` + `genotype_calls` with `is_imputed = TRUE` and `imputation_r2`.
- Re-derive `consensus_genotypes` to consume imputed calls per `imputation_r2_threshold`.
- Update `imputation_runs` with volumes and quality stats.

Out of scope:
- Replacing the manual TopMed handoff with a programmatic API (still v1 manual roundtrip per locked stack).

Done when an imputed VCF flows in, consensus updates, and `mean_imputation_r2` reports sensibly.

---

## Phase 4 — Reference annotations

Goal: bulk-load the curated knowledge layer and refresh the per-variant rollup.

Scope:
- `annotation_source_versions` registry; per-source ingest jobs.
- Full bulk load: ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog (scores metadata), genes, traits, pathways.
- Overlapping-only: PGS weights, gnomAD, dbSNP. Build the (user ∪ ClinVar ∪ GWAS ∪ PGS) filter set first.
- Compute VEP locally; capture into `vep_consequences`.
- Refresh job for `variant_annotations_index` (the per-variant rollup).
- CLI: `genome refresh-annotations [--source clinvar|...]`.

Out of scope:
- Derived analyses (phase 5).
- Insight generation (phase 6).

Done when a snapshot capture lists current versions for every source and `variant_annotations_index` is fresh.

---

## Phase 5 — Derived analyses

Goal: every derived pipeline runs, writes provenance, and supports supersession.

Scope:
- `analysis_runs` table populated for each pipeline.
- PGS via internal calculator over `pgs_score_weights` ∩ user variants.
- PGx via PharmCAT subprocess; write `derived_pgx_phenotypes`.
- Carrier screening (rule-based over genes + ClinVar P/LP).
- ACMG SF over `genes.is_acmg_sf`.
- HLA via HIBAG (R subprocess or rpy2).
- ROH via plink2.
- Haplogroups (Y, mtDNA) via haplogrep (or comparable).
- Global / local / archaic ancestry; genetic distance.
- Compound heterozygosity per gene over P/LP variants.
- Genome-wide QC summary in `derived_genome_qc`.

Out of scope:
- The user-facing insights surface (phase 6).

Done when `derived_summary_v` shows non-zero counts across the expected pipelines.

---

## Phase 6 — Insights & evidence

Goal: every derived row that matters becomes an `insights` row with `evidence` rows and full provenance.

Scope:
- Versioned tier-mapping functions per source (`clinvar_to_unified_v1`, `cpic_to_unified_v1`, `gwas_to_unified_v1`, …).
- Confidence rollup function (`compute_confidence`) that respects conflicting evidence.
- Insight generators per type: `pgx`, `prs`, `carrier`, `clinvar`, `acmg_sf`, `trait`, `hla`, plus the cross-cutting `pleiotropy`, `compound`, `pathway`.
- Supersession workflow on every regenerate.
- `summary_dashboard` refresh job.
- Audience rendering cache (`eli5` / `layperson` / `clinical`) populated lazily.

Out of scope:
- LLM synthesis beyond audience rendering (phase 8).

Done when starring an insight, marking it reviewed, and re-running its generator produces an INSERT-then-supersede sequence — never an UPDATE of active content.

---

## Phase 7 — API and frontend MVP

Goal: a usable local UI over the insights model.

Scope:
- FastAPI app: insights list/detail, gene drill-down, variant detail, PGx checker over medications, snapshots.
- Next.js + Tailwind + shadcn/ui frontend.
- Recharts for standard plots; D3 for karyogram and Manhattan.
- Notes / bookmarks / observations CRUD against `app.db`.
- Audit log viewer.
- Privacy dashboard fed by `external_call_summary_v`.

Out of scope:
- Mobile.
- Multi-user.

Done when a fresh checkout — after `genome init` and a sample run — boots the UI and the home dashboard renders insight counts.

---

## Phase 8 — LLM-assisted features

Goal: NL queries and rich audience rendering.

Scope:
- NL → SQL / NL → tool-chain via Anthropic SDK (`claude-opus-4-7`).
- `saved_queries` with auto-rerun and change detection (`last_result_hash`).
- LLM-driven audience rendering for insights (`eli5` / `layperson` / `clinical`), cached.
- Every LLM call gated by `external_calls_enabled` and audit-logged.

Out of scope:
- Letting the LLM mutate state directly. It can suggest; the worker writes.

Done when an NL query can be saved, replayed on cadence, and surfaces a "result changed" notification when `last_result_hash` shifts.

---

## Phase 9 — Multi-profile + snapshots polish

Goal: support family profiles and reproducible snapshots end-to-end.

Scope:
- Adding profiles, switching profiles, per-profile DuckDB connection pool.
- Snapshot capture / restore against `archive/snapshots/<uuid>.json.zst`.
- Snapshot diff view ("what changed since snapshot X").
- Auto-snapshot cadence per `auto_snapshot_cadence`.

Done when two profiles coexist with isolated DBs and the snapshot diff view lights up after an annotation refresh.

---

## Cross-cutting tracks (active during all phases)

- Audit & privacy: every external call passes through the audited client; `external_call_summary_v` is the truth.
- Versioning: every source/method bump increments `*_version` strings — never silently overwrite.
- Tests: each phase ships with unit + integration tests; integration tests run offline against fixtures, never live external services.
