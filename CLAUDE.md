# Project Context — DNA Insights App

## What this is
A local-first personal DNA insights application that ingests 23andMe + Ancestry raw exports, merges + imputes them via TopMed, joins against curated reference annotations, runs analytical pipelines, and surfaces a unified insights model.

## Read this before any work
- The five schema documents in `docs/schemas/` are the source of truth for data design. Read the relevant one(s) before touching any DB-adjacent code.
- `ROADMAP.md` defines build phases. Stay within the current phase unless explicitly directed otherwise.
- This file (`CLAUDE.md`) is the persistent context for every session.

## Architecture — locked decisions

1. Two databases: `genome.duckdb` (DuckDB analytical) and `app.db` (SQLite + SQLCipher, encrypted).
2. Coordinates: GRCh38 primary, GRCh37 stored alongside. `variant_id` is `BIGINT` from a sequence.
3. Multi-allelic variants split into biallelic rows.
4. Imputed variants share `variants_master`; imputation status is on `genotype_calls`.
5. PGS weights are overlapping-only.
6. Encryption: OS FDE + SQLCipher on `app.db`. The DuckDB file is not encrypted; rely on filesystem perms (0600) and FDE.
7. Supersession over update. Insights, evidence, and derived rows have `is_active` + `superseded_by`. Re-runs INSERT-then-deactivate. Never UPDATE active content.
8. Provenance everywhere. Every annotation, derived row, and insight names its source/method version.
9. Local-first privacy. External calls require `external_calls_enabled = true`. Every external call is audit-logged with endpoint + payload hash (not payload).

## Tech stack — locked

- Python 3.12+, DuckDB, SQLite via pysqlcipher3, FastAPI, Typer, Anthropic SDK
- Variant tooling: bcftools, plink2, cyvcf2; PharmCAT (PGx); HIBAG (HLA)
- Frontend: Next.js + React + TypeScript + Tailwind + shadcn/ui; Recharts + D3
- Tests: pytest; lint: ruff; types: mypy strict

## Conventions

- All cross-DB references are application-validated, not enforced by FK.
- Every insight points to one or more evidence rows. Never write an insight with zero evidence.
- The unified evidence-tier scale is `1A | 1B | 2A | 2B | 3 | 4`. Source-specific grades are mapped via versioned functions in `genome.insights.tier_mapping`.
- Insight `confidence_score` is computed from evidence; never set by hand.
- All long-running tasks go through the jobs table — never run them inline in API handlers.
- All external calls go through a single audited HTTP client (`genome.privacy.external_client`).
- Logging: structlog with JSON output. No `print()`.
- Style: ruff defaults plus `--select=ALL --ignore=D,ANN101,ANN102`. Type-annotate everything.
- 23andMe and Ancestry exports may include variants on non-canonical GRCh38 contigs (alt, random, unplaced, decoy). These are filtered at parse time and counted in `ingestion_runs.variants_dropped_non_canonical`. Only canonical chromosomes (1-22, X, Y, MT) are stored. This matches standard clinical bioinformatics practice.

## Environment requirements

- **SQLCipher must be built with FTS5.** `app.db` includes a `notes_fts` virtual
  table that uses FTS5. Most distro packages of SQLCipher (e.g. Ubuntu 24.04's
  `libsqlcipher-dev` 4.5.6) ship without FTS5, so `pysqlcipher3` linked against
  them will fail at `genome init` with `no such module: fts5`. Rebuild SQLCipher
  4.5.6 from source with `--enable-fts5` and reinstall `pysqlcipher3` against
  it; the exact build commands live in `README.md` under "Prerequisites".
- **Never "fix" an FTS5 install failure by removing the `notes_fts` virtual table
  (or its triggers) from `docs/schemas/schema_group_5_app_state.md` /
  `ddl/group_5_app_state.sql`.** Note search is a product requirement; if you
  hit `no such module: fts5` the answer is to rebuild SQLCipher with FTS5, not
  to mutilate the schema. Future sessions: heed this. Also relevant: see
  "Things never to do" — schema files are immutable except via deliberate,
  documented schema corrections.

## Common file locations

- DDL: `ddl/*.sql`
- Schema docs: `docs/schemas/`
- Backend code: `backend/src/genome/`
- Tests: `backend/tests/`
- Frontend: `frontend/`
- Runtime data (gitignored): `data/`
- Raw uploads, snapshots, source dumps (gitignored): `archive/`

## How to run

- Setup: `uv sync && cp .env.example .env && $EDITOR .env`
- Initialize: `genome init`
- Tests: `pytest`
- Lint: `ruff check && ruff format --check`
- Types: `mypy --strict backend/src`
- Dev API (later phases): `uvicorn genome.api.main:app --reload`
- Frontend (later phases): `cd frontend && pnpm dev`

## Things never to do

- Never modify the schema markdown files in `docs/schemas/` or the DDL files extracted from them, except via a deliberate, documented schema change followed by a re-extraction.
- Never UPDATE an active insight or evidence row to change its content. Use the supersession workflow.
- Never bulk-load gnomAD without filtering to the (user ∪ ClinVar ∪ GWAS ∪ PGS) intersection — full gnomAD is too large.
- Never call an external API outside the audited client.
- Never store the body of an external request — only the hash.
- Never embed an API key, passphrase, or other secret in code or tests.
- Never bypass the unified evidence-tier scale by writing a source-specific grade into `insights.evidence_tier`.
